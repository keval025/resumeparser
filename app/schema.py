"""Pydantic models describing the parsed resume.

Two layers:
  * ``ExtractedResume`` - what the LLM is asked to fill in (no pipeline metadata).
  * ``ResumeData``      - the final record = ExtractedResume + ``meta``.

Small "section" wrapper models (ExperienceSection, ...) are used when the resume is long and
we call the LLM once per section.

Dates are kept as strings: the LLM copies them as written ("Jan 2021"), and
``pipeline/normalize.py`` converts them to "YYYY-MM" or "present".
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    """Base class: reject unknown keys so typos in LLM output surface as validation errors."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Resume content
# ---------------------------------------------------------------------------


class Links(_Model):
    linkedin: str | None = Field(None, description="LinkedIn profile URL")
    github: str | None = Field(None, description="GitHub profile URL")
    portfolio: str | None = Field(None, description="Personal website / portfolio URL")
    other: list[str] = Field(default_factory=list, description="Any other URLs")


class Basics(_Model):
    name: str | None = Field(None, description="Candidate's full name")
    email: str | None = None
    phone: str | None = None
    location: str | None = Field(None, description="City / state / country as written")
    links: Links = Field(default_factory=Links)
    summary: str | None = Field(None, description="Profile / objective / summary paragraph")


class Education(_Model):
    institution: str | None = None
    degree: str | None = Field(None, description="Degree exactly as written, e.g. 'B.Tech'")
    degree_normalized: str | None = Field(None, description="Filled by the pipeline; leave null")
    field: str | None = Field(None, description="Field of study / major")
    start: str | None = Field(None, description="Start date as written")
    end: str | None = Field(None, description="End date as written, or 'Present'")
    grade: str | None = Field(None, description="CGPA / GPA / percentage as written")


class Experience(_Model):
    company: str | None = None
    title: str | None = Field(None, description="Job title / role")
    location: str | None = None
    start: str | None = Field(None, description="Start date as written")
    end: str | None = Field(None, description="End date as written, or 'Present'")
    duration_months: int | None = Field(None, description="Filled by the pipeline; leave null")
    bullets: list[str] = Field(default_factory=list, description="Responsibility / achievement bullets")
    skills_used: list[str] = Field(default_factory=list, description="Technologies explicitly mentioned for this job")


class Project(_Model):
    name: str | None = None
    description: str | None = None
    tech_stack: list[str] = Field(default_factory=list)
    link: str | None = None
    outcomes: list[str] = Field(default_factory=list, description="Measurable results / impact")
    start: str | None = None
    end: str | None = None


class Skills(_Model):
    technical: list[str] = Field(default_factory=list, description="Programming languages, frameworks, concepts")
    tools: list[str] = Field(default_factory=list, description="Software, platforms, cloud, databases, IDEs")
    soft: list[str] = Field(default_factory=list, description="Soft skills, e.g. Leadership")
    languages: list[str] = Field(default_factory=list, description="Spoken languages, e.g. English, Hindi")


class Certification(_Model):
    name: str | None = None
    issuer: str | None = None
    date: str | None = None


class ExtractedResume(_Model):
    """Everything the LLM extracts from a resume."""

    basics: Basics = Field(default_factory=Basics)
    education: list[Education] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    skills: Skills = Field(default_factory=Skills)
    certifications: list[Certification] = Field(default_factory=list)
    achievements: list[str] = Field(default_factory=list, description="Awards, honours, publications, competitions")


# ---------------------------------------------------------------------------
# Per-section wrappers (used for long resumes: one LLM call per section)
# ---------------------------------------------------------------------------


class BasicsSection(_Model):
    basics: Basics


class EducationSection(_Model):
    education: list[Education]


class ExperienceSection(_Model):
    experience: list[Experience]


class ProjectsSection(_Model):
    projects: list[Project]


class SkillsSection(_Model):
    skills: Skills


class CertificationsSection(_Model):
    certifications: list[Certification]


class AchievementsSection(_Model):
    achievements: list[str]


# ---------------------------------------------------------------------------
# Final record
# ---------------------------------------------------------------------------


class Meta(_Model):
    source_hash: str
    file_name: str
    parser_version: str
    is_scanned: bool = False
    pages: int = 0
    confidence: dict[str, float] = Field(default_factory=dict)
    needs_review: bool = False
    warnings: list[str] = Field(default_factory=list)
    processing_ms: int = 0
    total_experience_months: int | None = None


class ResumeData(ExtractedResume):
    """The final, validated output record."""

    meta: Meta
