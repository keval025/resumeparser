"""Pydantic models for the Resume <-> Job Description match feature.

Three groups:
  * ``JobDescription``   - what the LLM extracts from the pasted JD text.
  * ``RelevanceRatings`` / ``Suggestions`` - the other two LLM answers.
  * ``MatchResult``      - the deterministic score computed in Python (``pipeline/match.py``).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schema import _Model

Seniority = Literal["intern", "entry", "mid", "senior", "lead"]
SuggestionArea = Literal["skills", "experience", "projects", "keywords", "education", "formatting", "summary"]
Priority = Literal["high", "medium", "low"]


# ---------------------------------------------------------------------------
# Job description (LLM output)
# ---------------------------------------------------------------------------


class EducationRequirements(_Model):
    min_degree: str | None = Field(None, description="Minimum degree as written, e.g. \"Bachelor's\"; null if none")
    fields: list[str] = Field(default_factory=list, description="Accepted fields of study, e.g. Computer Science")


class JobDescription(_Model):
    title: str | None = Field(None, description="Job title")
    company: str | None = None
    seniority: Seniority | None = Field(None, description="Inferred from title and years required")
    location: str | None = None
    employment_type: str | None = Field(None, description="Full-time / Part-time / Contract / Internship")
    min_years_experience: float | None = Field(None, description="Minimum years required; null if not stated")
    max_years_experience: float | None = Field(None, description="Upper bound of a range like 3-5 years; else null")
    required_skills: list[str] = Field(default_factory=list, description="Must-have technical / domain skills, one per item")
    preferred_skills: list[str] = Field(default_factory=list, description="Nice-to-have / bonus skills")
    tools: list[str] = Field(default_factory=list, description="Software, platforms, cloud services, databases named")
    education_requirements: EducationRequirements = Field(default_factory=EducationRequirements)
    certifications: list[str] = Field(default_factory=list)
    responsibilities: list[str] = Field(default_factory=list, description="One entry per duty, copied close to verbatim")
    domain_keywords: list[str] = Field(default_factory=list, description="Industry / domain terms an ATS would scan for")
    soft_skills: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Experience relevance (LLM output)
# ---------------------------------------------------------------------------


class RelevanceRating(_Model):
    id: str = Field(description="The item id exactly as given, e.g. 'experience[0]' or 'projects[1]'")
    score: float = Field(ge=0, le=1, description="0 = unrelated, 1 = directly does these responsibilities")
    reason: str = Field(description="One short line explaining the score")


class RelevanceRatings(_Model):
    ratings: list[RelevanceRating]


# ---------------------------------------------------------------------------
# Suggestions (LLM output)
# ---------------------------------------------------------------------------


class TopPriority(_Model):
    area: SuggestionArea
    issue: str
    action: str = Field(description="The exact change to make")
    example_rewrite: str = Field(description="Rewritten bullet/summary using ONLY facts from the resume; [X%] placeholders for unknown numbers")
    estimated_score_gain: int = Field(ge=0, le=35)


class BulletRewrite(_Model):
    original: str = Field(description="A bullet copied verbatim from the resume")
    improved: str
    reason: str


class SkillToLearn(_Model):
    skill: str
    why: str
    priority: Priority


class Suggestions(_Model):
    summary: str = Field(description="2-3 line verdict")
    top_priorities: list[TopPriority] = Field(description="5-8 items, highest score impact first")
    missing_keywords_to_add: list[str] = Field(default_factory=list, description="Only keywords the resume already gives evidence for")
    bullets_to_rewrite: list[BulletRewrite] = Field(default_factory=list)
    suggested_summary: str
    skills_to_learn: list[SkillToLearn] = Field(default_factory=list, description="Real gaps the candidate cannot honestly claim")


# ---------------------------------------------------------------------------
# Deterministic score (computed in Python, never by the LLM)
# ---------------------------------------------------------------------------


class ComponentScore(BaseModel):
    """One row of the breakdown. ``details`` is a short human explanation; ``data`` the evidence."""

    model_config = ConfigDict(extra="forbid")

    score: float
    max: float
    details: str
    data: dict = Field(default_factory=dict)


class SkillMatch(BaseModel):
    skill: str
    matched: bool
    how: Literal["exact", "alias", "fuzzy", "text", "none"]
    evidence: str | None = None


class MatchResult(BaseModel):
    score: int
    label: str
    breakdown: dict[str, ComponentScore]
    matched_skills: list[str]
    missing_required_skills: list[str]
    missing_preferred_skills: list[str]
    semantic_similarity: float | None = None
    scoring_version: str
