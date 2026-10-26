"""Unit tests for the tailoring engine: truthfulness validator, placeholders, effective document, re-scoring."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.match_schema import JobDescription
from app.pipeline.jd_parse import clean_jd
from app.pipeline.run import ResumePipeline
from app.pipeline.tailor import (
    as_resume_data,
    bullet_text,
    find_placeholders,
    refresh_flags,
    score_tailored,
    tailor,
    target_pages,
    trim_least_relevant,
    validate_draft,
)
from app.schema import ResumeData
from app.tailor_schema import TailorDraft
from tests.match_fakes import PYTHON_JD
from tests.sample_resume import FakeProvider
from tests.tailor_fakes import CLEAN_DRAFT, DISHONEST_DRAFT, INFOSYS_1, INFOSYS_2, TCS_1, FakeTailorProvider


@pytest.fixture
def parsed(settings: Settings, sample_pdf_bytes: bytes) -> tuple[ResumeData, str]:
    outcome = ResumePipeline(settings, provider=FakeProvider()).parse_bytes(sample_pdf_bytes, "sample_resume.pdf")
    return outcome.data, outcome.raw_text


@pytest.fixture
def jd(settings: Settings) -> JobDescription:
    return clean_jd(JobDescription.model_validate(PYTHON_JD), settings.normalization_file)


def _validate(draft: dict, data: ResumeData, jd: JobDescription):
    return validate_draft(TailorDraft.model_validate(draft), data, jd)


# ---------------------------------------------------------------------------
# The validator reverts every invention
# ---------------------------------------------------------------------------


def test_validator_reverts_invented_facts(parsed, jd) -> None:
    data, _ = parsed
    doc, warnings = _validate(DISHONEST_DRAFT, data, jd)

    # Contact info, education, certifications, achievements: copied, never generated.
    assert doc.basics == data.basics
    assert doc.education == data.education and doc.certifications == data.certifications

    # Jobs: the invented one is gone; company / title / dates come from the original.
    assert [(j.company, j.title, j.start, j.end) for j in doc.experience] == [
        ("Infosys Ltd", "Software Engineer", "2021-07", "present"),
        ("Tata Consultancy Services", "Software Engineering Intern", "2021-01", "2021-06"),
    ]
    for expected in ("invented_experience_removed", "reverted_title", "reverted_company", "reverted_end"):
        assert any(w.startswith(expected) for w in warnings), expected
    assert not any(w.startswith("reverted_start") for w in warnings)  # "Jul 2021" == "2021-07"


def test_validator_handles_bullets(parsed, jd) -> None:
    data, _ = parsed
    doc, warnings = _validate(DISHONEST_DRAFT, data, jd)
    infosys, tcs = doc.experience

    # Invented number -> placeholder, flagged for the user.
    first = infosys.bullets[0]
    assert first.text == "Designed and built REST APIs in Python and Django serving 2M requests per day for [X] enterprise clients"
    assert first.original_text == INFOSYS_1 and first.changed and first.needs_user_input
    assert first.jd_requirement == "Design, build and maintain REST APIs"

    # The invented bullet is dropped; the bullet Gemini left out comes back hidden.
    assert [b.original_text for b in infosys.bullets] == [INFOSYS_1, INFOSYS_2]
    assert infosys.bullets[1].hidden and not infosys.bullets[1].changed
    assert any(w.startswith("invented_bullet_removed") for w in warnings)

    # A technology the job never mentions -> "[if true: ...]".
    assert tcs.bullets[0].text == "Developed internal dashboards with React, JavaScript and [if true: Kubernetes]"
    assert tcs.bullets[0].original_text == TCS_1 and tcs.bullets[0].needs_user_input
    assert any("unsupported_skill_flagged: 'Kubernetes'" in w for w in warnings)


def test_validator_handles_skills_projects_summary_and_order(parsed, jd) -> None:
    data, _ = parsed
    doc, warnings = _validate(DISHONEST_DRAFT, data, jd)

    all_skills = doc.skills.technical + doc.skills.tools + doc.skills.soft
    assert "Kubernetes" not in all_skills
    assert "Leadership" in doc.skills.soft  # dropped by Gemini, re-added
    assert doc.skills.technical[:2] == ["Python", "SQL"]  # Gemini's order kept
    assert any(w == "invented_skill_removed: 'Kubernetes'" for w in warnings)

    project = doc.projects[0]
    assert project.tech_stack == ["Python", "FastAPI", "PostgreSQL"]
    assert any("invented_skill_removed: 'Kubernetes' from projects[0]" in w for w in warnings)
    assert project.bullets[0].needs_user_input  # Gemini's own [X%] placeholder
    assert project.bullets[1].original_text == "AI resume screener" and project.bullets[1].hidden

    assert doc.summary.startswith("Backend engineer with [X] years")  # "5 years" is not in the resume
    assert doc.section_order == ["summary", "experience", "projects", "skills", "education", "certifications", "achievements"]


def test_clean_draft_passes_without_warnings(parsed, jd) -> None:
    doc, warnings = _validate(CLEAN_DRAFT, parsed[0], jd)
    assert warnings == []
    assert find_placeholders(doc) == []
    assert doc.changes_log and doc.changes_log[0].section == "summary"
    changed = [b for b in doc.experience[0].bullets if b.changed]
    assert len(changed) == 2 and all(b.reason for b in changed)


def test_tailor_uses_temperature_0_2(parsed, jd, settings: Settings) -> None:
    from app.pipeline.match import compute_match

    provider = FakeTailorProvider()
    result = compute_match(*parsed, jd, {}, settings.normalization_file)
    tailor(provider, parsed[0], jd, result)
    assert provider.temperatures == [0.2]
    assert "source_index" in provider.calls[0]["prompt"]


# ---------------------------------------------------------------------------
# Placeholders block export until resolved
# ---------------------------------------------------------------------------


def test_placeholders_block_until_resolved(parsed, jd) -> None:
    doc, _ = _validate(DISHONEST_DRAFT, parsed[0], jd)
    found = {p["placeholder"] for p in find_placeholders(doc)}
    assert found == {"[X]", "[if true: Kubernetes]", "[X%]"}

    # Fill one in, reject one (back to the original text), hide the project: nothing left.
    doc.summary = "Backend engineer with 3 years of experience building Python and Django REST APIs at scale."
    doc.experience[0].bullets[0].text = "Designed and built REST APIs in Python and Django serving 2M requests per day"
    doc.experience[0].bullets[0].decision = "edited"
    assert find_placeholders(doc)  # still two left
    doc.experience[1].bullets[0].decision = "rejected"
    assert bullet_text(doc.experience[1].bullets[0]) == TCS_1
    doc.projects[0].hidden = True
    assert find_placeholders(doc) == []
    assert not refresh_flags(doc).experience[0].bullets[0].needs_user_input


def test_hidden_section_placeholders_do_not_block(parsed, jd) -> None:
    doc, _ = _validate(DISHONEST_DRAFT, parsed[0], jd)
    doc.hidden_sections = ["summary", "experience", "projects"]
    assert find_placeholders(doc) == []


# ---------------------------------------------------------------------------
# Re-scoring and auto-fit
# ---------------------------------------------------------------------------

RATINGS = {"experience[0]": {"score": 0.9, "reason": "a"}, "experience[1]": {"score": 0.4, "reason": "b"}, "projects[0]": {"score": 0.6, "reason": "c"}}


def test_rescore_is_deterministic_and_ignores_placeholders(parsed, jd, settings: Settings) -> None:
    data, _ = parsed
    doc, _ = _validate(DISHONEST_DRAFT, data, jd)
    first = score_tailored(doc, data, jd, RATINGS, settings.normalization_file)
    second = score_tailored(doc.model_copy(deep=True), data, jd, dict(RATINGS), settings.normalization_file)
    assert first.model_dump() == second.model_dump()
    # "[if true: Kubernetes]" must not count as having Kubernetes.
    assert "Kubernetes" in first.missing_preferred_skills
    tailored = as_resume_data(doc, data)
    assert all("[" not in b for job in tailored.experience for b in job.bullets)


def test_hidden_project_ratings_are_remapped(parsed, jd, settings: Settings) -> None:
    data, _ = parsed
    doc, _ = _validate(CLEAN_DRAFT, data, jd)
    shown = score_tailored(doc, data, jd, RATINGS, settings.normalization_file)
    doc.projects[0].hidden = True
    hidden = score_tailored(doc, data, jd, RATINGS, settings.normalization_file)
    assert len(shown.breakdown["experience_relevance"].data["items"]) == 3
    assert len(hidden.breakdown["experience_relevance"].data["items"]) == 2


def test_trim_least_relevant_order(parsed, jd) -> None:
    data, _ = parsed
    doc, _ = _validate(CLEAN_DRAFT, data, jd)
    doc.projects[0].relevance_score = 0.1
    assert trim_least_relevant(doc).startswith("hid project 'ResumeRank'")
    # Next: the weakest bullet of a job with more than one visible bullet (never the last one).
    step = trim_least_relevant(doc)
    assert step and "Infosys" in step
    assert sum(not b.hidden for b in doc.experience[0].bullets) == 1
    assert trim_least_relevant(doc) is None  # one bullet per job left, no projects: nothing more to trim


def test_target_pages(parsed) -> None:
    data, _ = parsed
    assert target_pages(data) == (1 if (data.meta.total_experience_months or 0) < 36 else 2)
    young = data.model_copy(deep=True)
    young.meta.total_experience_months = 20
    assert target_pages(young) == 1
