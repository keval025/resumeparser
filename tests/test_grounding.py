"""Grounding (hallucination) check, sanity rules and confidence."""

from __future__ import annotations

from app.pipeline.rules import RuleResult
from app.pipeline.validate import SourceIndex, ground_extraction, needs_review, sanity_check, score_confidence
from app.schema import Education, Experience, ExtractedResume, Project, Skills

SOURCE = """Priya Sharma
WORK EXPERIENCE
Infosys Ltd - Software Engineer   Jul 2021 - Present
Tata Consultancy Services - Intern
PROJECTS
ResumeRank - AI resume screener
EDUCATION
Vellore Institute of Technology, B.Tech
SKILLS
Python, JS, C, Node.js, Docker
"""


def test_source_index_matching() -> None:
    index = SourceIndex(SOURCE)
    assert index.contains("Infosys Ltd")
    assert index.contains("infosys ltd.")                        # punctuation/case differences
    assert index.contains("Tata Consultancy Service")            # fuzzy
    assert index.contains("NodeJS")                              # compact match for Node.js
    assert index.contains("C")                                   # whole-word short value
    assert not index.contains("R")                               # 'R' only appears inside words
    assert not index.contains("Google")
    assert not index.contains("Stanford University")


def test_ground_extraction_drops_hallucinations() -> None:
    resume = ExtractedResume(
        experience=[
            Experience(company="Infosys Ltd", title="Software Engineer", skills_used=["Python", "Kubernetes"]),
            Experience(company="Google", title="Staff Engineer"),
        ],
        education=[
            Education(institution="Vellore Institute of Technology", degree="B.Tech"),
            Education(institution="Stanford University", degree=None),
        ],
        projects=[Project(name="ResumeRank"), Project(name="MoonLander")],
        skills=Skills(technical=["Python", "JS", "Rust"], tools=["Docker"]),
    )
    report = ground_extraction(resume, SOURCE, threshold=85)

    assert [j.company for j in resume.experience] == ["Infosys Ltd"]
    assert resume.experience[0].skills_used == ["Python"]
    assert [e.institution for e in resume.education] == ["Vellore Institute of Technology"]
    assert [p.name for p in resume.projects] == ["ResumeRank"]
    assert resume.skills.technical == ["Python", "JS"]
    assert any("Google" in w for w in report.dropped)
    assert all(w.startswith("possible_hallucination") for w in report.dropped)
    # The invented Google job counts as ONE flagged entry, plus the Kubernetes skill on job 0.
    assert report.dropped_by_field["experience"] == 2


def test_sanity_checks_and_confidence() -> None:
    resume = ExtractedResume(
        experience=[Experience(company="Infosys Ltd", title="Engineer", start="2022-05", end="2021-01")],
    )
    resume.basics.name = "Priya Sharma"
    resume.basics.email = "not-an-email"
    rules = RuleResult(name="Priya Sharma", emails=[], phones=["+919876543210"], links={})
    sanity = sanity_check(resume, rules)

    assert resume.basics.email is None
    assert resume.basics.phone == "+919876543210"
    assert any(w.startswith("date_order") for w in sanity.warnings)

    report = ground_extraction(resume, SOURCE + " Engineer")
    confidence = score_confidence(resume, rules, report, sanity, {"experience", "education"}, {}, is_scanned=False)
    assert set(confidence) == {"basics", "education", "experience", "projects", "skills", "certifications", "achievements"}
    assert confidence["education"] == 0.3   # heading present, nothing extracted
    assert confidence["projects"] == 1.0    # no projects section, nothing expected
    assert needs_review(confidence, 0.7) is True
