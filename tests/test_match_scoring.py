"""Unit tests for the job-match scoring engine, JD cleaning and suggestion grounding (no real LLM)."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.match_schema import JobDescription, MatchResult, Suggestions
from app.pipeline.jd_parse import clean_jd, jd_hash, parse_jd, split_alternatives
from app.pipeline.match import (
    WEIGHTS,
    _in_text,
    build_resume_view,
    compute_match,
    degree_level,
    label_for,
    match_skill,
    rate_relevance,
    responsibility_terms,
    round_half_up,
    score_education,
    score_experience_relevance,
    score_keywords,
    score_quality,
    score_years,
    semantic_similarity,
)
from app.pipeline.run import ResumePipeline
from app.pipeline.suggest import ground_suggestions
from app.schema import ResumeData
from tests.match_fakes import PYTHON_JD, SUGGESTIONS, FakeMatchProvider
from tests.sample_resume import FakeProvider


@pytest.fixture
def parsed(settings: Settings, sample_pdf_bytes: bytes) -> tuple[ResumeData, str]:
    """The sample resume run through the real pipeline (LLM mocked): (ResumeData, raw text)."""
    outcome = ResumePipeline(settings, provider=FakeProvider()).parse_bytes(sample_pdf_bytes, "sample_resume.pdf")
    return outcome.data, outcome.raw_text


@pytest.fixture
def python_jd(settings: Settings) -> JobDescription:
    return clean_jd(JobDescription.model_validate(PYTHON_JD), settings.normalization_file)


RATINGS = {"experience[0]": {"score": 0.9, "reason": "a"}, "experience[1]": {"score": 0.4, "reason": "b"}, "projects[0]": {"score": 0.6, "reason": "c"}}


# ---------------------------------------------------------------------------
# JD parsing / cleaning
# ---------------------------------------------------------------------------


def test_clean_jd_normalizes_aliases_and_removes_duplicates(python_jd: JobDescription) -> None:
    assert python_jd.required_skills == ["Python", "Django / FastAPI", "REST APIs", "SQL", "PostgreSQL", "Git", "Docker"]
    # "Django" (preferred) and "FastAPI" (tool) are already covered by the required "Django / FastAPI".
    assert python_jd.preferred_skills == ["Kubernetes", "Celery", "CI/CD", "GraphQL"]
    assert python_jd.tools == ["Redis", "AWS"]


def test_split_alternatives_keeps_slash_words_whole() -> None:
    assert split_alternatives("Django or FastAPI") == ["Django", "FastAPI"]
    assert split_alternatives("Amplitude / Mixpanel") == ["Amplitude", "Mixpanel"]
    assert split_alternatives("CI/CD") == ["CI/CD"]


def test_jd_hash_ignores_whitespace() -> None:
    assert jd_hash("Python  developer\n\n needed ") == jd_hash("Python developer\nneeded")
    assert jd_hash("Python developer") != jd_hash("Java developer")


def test_parse_jd_uses_llm_and_cleans(settings: Settings) -> None:
    provider = FakeMatchProvider()
    jd = parse_jd(provider, "Python Developer\n" + "x" * 80, settings.normalization_file)
    assert provider.count(JobDescription) == 1
    assert "PostgreSQL" in jd.required_skills and "Postgres" not in jd.required_skills


# ---------------------------------------------------------------------------
# Skill matching
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "skill,how,evidence",
    [
        ("Python", "exact", "Python"),
        ("JS", "alias", "JavaScript"),  # JD alias -> resume canonical
        ("Postgres", "alias", "PostgreSQL"),
        ("Dockers", "fuzzy", "Docker"),  # rapidfuzz ratio >= 85
        ("caching", "text", None),  # only in a bullet
        ("Django / Flask", "exact", "Django"),  # either/or: one alternative is enough
        ("Kubernetes", "none", None),  # the hallucinated Google job was dropped, so no evidence
        ("Go", "none", None),
    ],
)
def test_match_skill(parsed, settings: Settings, skill: str, how: str, evidence: str | None) -> None:
    view = build_resume_view(*parsed)
    result = match_skill(skill, view, settings.normalization_file)
    assert result.how == how
    assert result.matched is (how != "none")
    assert result.skill == skill
    if evidence:
        assert evidence in (result.evidence or "")


def test_short_words_are_not_matched_in_free_text() -> None:
    assert not _in_text("go", " we go live in june ")
    assert _in_text("sql", " wrote sql reports ")
    assert not _in_text("sql", " wrote mysql reports ")


def test_fuzzy_threshold_is_respected(parsed, settings: Settings) -> None:
    view = build_resume_view(*parsed)
    assert match_skill("Dockerr", view, settings.normalization_file, threshold=85).how == "fuzzy"
    assert match_skill("Dockerr", view, settings.normalization_file, threshold=99).how == "none"


# ---------------------------------------------------------------------------
# Years, education, keywords, quality, relevance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "months,min_y,max_y,expected",
    [
        (36, None, None, 10.0),  # no requirement
        (36, 3, 5, 10.0),  # exactly the minimum
        (18, 3, 5, 5.0),  # half the minimum -> half marks (linear)
        (0, 2, None, 0.0),
        (120, 2, 4, 10.0),  # above the max is not penalised
        (None, 2, None, 0.0),  # no dated experience
        (None, 0, None, 10.0),  # entry level: 0 years required
    ],
)
def test_score_years(months, min_y, max_y, expected) -> None:
    assert score_years(months, min_y, max_y).score == expected


def test_score_years_mentions_not_penalised_above_max() -> None:
    assert "not penalised" in score_years(120, 2, 4).details


@pytest.mark.parametrize(
    "text,level",
    [
        ("B.Tech", 3),
        ("Bachelor's - Engineering", 3),
        ("BSc", 3),
        ("Bachelor's", 3),
        ("M.S.", 4),
        ("Master of Science", 4),
        ("MBA", 4),
        ("Master's - Computer Applications", 4),
        ("PhD", 5),
        ("Doctorate", 5),
        ("Diploma", 2),
        ("Higher Secondary (12th)", 1),
        ("Certificate in Cooking", None),
        (None, None),
    ],
)
def test_degree_level(text, level) -> None:
    assert degree_level(text) == level


@pytest.mark.parametrize(
    "min_degree,fields,expected",
    [
        ("Bachelor's", ["Computer Science"], 10.0),  # level 6 + field 4
        ("Bachelor's", ["Computer Science or a related field"], 10.0),
        ("Master's", ["Computer Science"], 7.0),  # one level below -> 3 + 4
        ("PhD", ["Computer Science"], 4.0),  # two levels below -> 0 + 4
        ("Bachelor's", ["Mechanical Engineering"], 6.0),  # wrong field
        ("Bachelor's", ["Mechanical", "related field"], 8.0),  # JD accepts related fields -> half the field points
        (None, [], 10.0),  # no requirement
    ],
)
def test_score_education(parsed, min_degree, fields, expected) -> None:
    jd = JobDescription(education_requirements={"min_degree": min_degree, "fields": fields})
    assert score_education(parsed[0], jd).score == expected


def test_responsibility_terms_are_deterministic_and_skip_filler() -> None:
    resp = PYTHON_JD["responsibilities"]
    terms = responsibility_terms(resp)
    assert terms == responsibility_terms(list(resp))
    assert terms[:3] == ["rest", "apis", "microservices"]  # JD order on ties
    assert not {"design", "build", "write", "efficient"} & set(terms)


def test_score_keywords(parsed, python_jd: JobDescription) -> None:
    view = build_resume_view(*parsed)
    result = score_keywords(view, python_jd)
    rows = {r["term"]: r for r in result.data["keywords"]}
    assert rows["REST APIs"]["present"] and rows["REST APIs"]["weight"] == 2.0
    assert rows["backend development"]["present"]  # stemmed: "Backend developer"
    assert not rows["fintech"]["present"]
    assert 0 < result.score < result.max


def test_score_quality(parsed) -> None:
    result = score_quality(build_resume_view(*parsed))
    assert result.data["bullets"] == 4 and result.data["quantified_bullets"] == 3
    assert result.data["has_email"] and result.data["has_profile_link"]
    assert 0 < result.score <= 5


def test_experience_relevance_weighted_average(parsed) -> None:
    result = score_experience_relevance(parsed[0], RATINGS)
    # jobs count x2: (0.9*2 + 0.4*2 + 0.6*1) / 5 = 0.64 -> 12.8 / 20
    assert result.score == 12.8
    assert result.data["weighted_average"] == 0.64
    missing = score_experience_relevance(parsed[0], {"experience[0]": {"score": 1.0, "reason": "x"}})
    assert missing.data["items"][1]["reason"] == "Not rated."


def test_rate_relevance_rounds_and_drops_unknown_ids(parsed) -> None:
    ratings = rate_relevance(FakeMatchProvider(), parsed[0], JobDescription.model_validate(PYTHON_JD))
    assert ratings["experience[0]"]["score"] == 0.9  # 0.92 -> nearest 0.05
    assert ratings["experience[1]"]["score"] == 0.4  # 0.38 -> 0.40
    assert ratings["projects[0]"]["score"] == 0.6  # 0.61 -> 0.60
    assert "experience[9]" not in ratings  # the model rated an item that does not exist


@pytest.mark.parametrize("score,label", [(100, "Excellent match"), (85, "Excellent match"), (84, "Strong"), (70, "Strong"), (69, "Moderate"), (50, "Moderate"), (49, "Low"), (0, "Low")])
def test_labels(score, label) -> None:
    assert label_for(score) == label


# ---------------------------------------------------------------------------
# Total score
# ---------------------------------------------------------------------------


def test_compute_match_total_and_lists(parsed, python_jd: JobDescription, settings: Settings) -> None:
    result = compute_match(*parsed, python_jd, RATINGS, settings.normalization_file)
    assert set(result.breakdown) == set(WEIGHTS) and sum(WEIGHTS.values()) == 100
    assert all(c.max == WEIGHTS[k] and 0 <= c.score <= c.max for k, c in result.breakdown.items())
    assert result.score == round_half_up(sum(c.score for c in result.breakdown.values()))
    assert result.label == label_for(result.score)
    assert result.breakdown["required_skills"].score == 35.0  # all 7 required skills present
    assert result.missing_required_skills == []
    assert {"Kubernetes", "Celery", "GraphQL"} <= set(result.missing_preferred_skills)
    assert "Redis" in result.matched_skills and len(result.matched_skills) == len(set(result.matched_skills))


def test_scoring_is_deterministic(parsed, python_jd: JobDescription, settings: Settings) -> None:
    first = compute_match(*parsed, python_jd, RATINGS, settings.normalization_file)
    second = compute_match(*parsed, python_jd.model_copy(deep=True), dict(RATINGS), settings.normalization_file)
    assert first.score == second.score
    assert first.model_dump() == second.model_dump()


def test_missing_required_skill_lowers_score(parsed, python_jd: JobDescription, settings: Settings) -> None:
    base = compute_match(*parsed, python_jd, RATINGS, settings.normalization_file)
    harder = python_jd.model_copy(update={"required_skills": python_jd.required_skills + ["Kubernetes"]})
    result = compute_match(*parsed, harder, RATINGS, settings.normalization_file)
    assert result.missing_required_skills == ["Kubernetes"]
    assert result.breakdown["required_skills"].score == 30.6  # 7/8 of 35
    assert result.score < base.score


def test_round_half_up() -> None:
    assert [round_half_up(x) for x in (72.5, 73.5, 72.49)] == [73, 74, 72]  # not banker's rounding


def test_semantic_similarity_is_optional() -> None:
    assert semantic_similarity(FakeProvider(), "a", "b", "m") is None  # provider without embed()
    assert semantic_similarity(FakeMatchProvider(embeddings=True), "a", "b", "m") == 0.5


# ---------------------------------------------------------------------------
# Suggestion grounding
# ---------------------------------------------------------------------------


def test_ground_suggestions(parsed, python_jd: JobDescription, settings: Settings) -> None:
    data, raw = parsed
    result: MatchResult = compute_match(data, raw, python_jd, RATINGS, settings.normalization_file)
    suggestions, warnings = ground_suggestions(Suggestions.model_validate(SUGGESTIONS), data, raw, result)

    rewrites = {p.area: p.example_rewrite for p in suggestions.top_priorities}
    # "14 services" is invented; "2M" and "40%" are in the resume and stay.
    assert rewrites["keywords"] == "Built REST APIs in Python and Django serving 2M requests per day across [X] services."
    assert rewrites["experience"] == "Cut report generation time by 40% with Redis caching."
    assert suggestions.bullets_to_rewrite[0].improved.startswith("Cut report generation time by 40% (from [X] min)")
    assert any(w.startswith("invented_number_removed") for w in warnings)

    # Gains are capped by what the component can still gain, then sorted.
    quality = result.breakdown["resume_quality"]
    formatting = next(p for p in suggestions.top_priorities if p.area == "formatting")
    assert formatting.estimated_score_gain <= quality.max - quality.score + 1
    gains = [p.estimated_score_gain for p in suggestions.top_priorities]
    assert gains == sorted(gains, reverse=True)

    # "Kubernetes" is a real gap (no evidence) -> moved from "add" to "learn"; Celery is not duplicated.
    assert suggestions.missing_keywords_to_add == ["backend development"]
    assert [s.skill for s in suggestions.skills_to_learn] == ["Celery", "Kubernetes"]


def test_rewrite_cannot_attach_unstated_skill_to_a_job(parsed, python_jd: JobDescription, settings: Settings) -> None:
    data, raw = parsed
    result = compute_match(data, raw, python_jd, RATINGS, settings.normalization_file)
    suggestions = Suggestions.model_validate(
        SUGGESTIONS
        | {
            "bullets_to_rewrite": [
                {
                    # TCS job lists React + JavaScript only; Python is a resume skill, but not for this job.
                    "original": "Developed internal dashboards with React and JS",
                    "improved": "Built internal dashboards in React and JavaScript backed by Python services",
                    "reason": "JD language.",
                },
                {
                    # Infosys job does list Redis: nothing to flag.
                    "original": "Reduced report generation time by 40% using Redis caching",
                    "improved": "Cut report generation time by 40% with Redis caching",
                    "reason": "Impact first.",
                },
            ]
        }
    )
    grounded, warnings = ground_suggestions(suggestions, data, raw, result)
    tcs, infosys = grounded.bullets_to_rewrite
    assert tcs.improved == "Built internal dashboards in React and JavaScript backed by [if true: Python] services"
    assert infosys.improved == "Cut report generation time by 40% with Redis caching"
    assert "unsupported_skill_flagged: 'Python' in bullet 'Developed internal dashboards with React and JS'" in warnings
