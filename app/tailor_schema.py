"""Pydantic models for "Tailor My Resume".

Two layers:
  * ``TailorDraft``    - what Gemini returns. Untrusted: every field is checked against the original
                         resume by ``pipeline/tailor.py`` before it is used.
  * ``TailoredResume`` - the validated, editable document. The review UI edits it (accept / reject /
                         edit / hide / reorder) and the exporters render it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schema import Basics, Certification, Education, _Model

SECTIONS = ("summary", "experience", "projects", "skills", "education", "certifications", "achievements")
Decision = Literal["pending", "accepted", "rejected", "edited"]


# ---------------------------------------------------------------------------
# Gemini output (untrusted)
# ---------------------------------------------------------------------------


class DraftBullet(_Model):
    original_text: str = Field(description="The original bullet this one rewrites, copied VERBATIM from the resume")
    text: str = Field(description="The tailored bullet (or the original unchanged)")
    reason: str | None = Field(None, description="Why it changed, one short line; null if unchanged")
    jd_requirement: str | None = Field(None, description="The JD requirement / keyword this bullet now targets")


class DraftExperience(_Model):
    source_index: int = Field(description="Index of this job in the original resume's experience list")
    company: str | None = None
    title: str | None = None
    location: str | None = None
    start: str | None = None
    end: str | None = None
    bullets: list[DraftBullet] = Field(default_factory=list, description="Most relevant first; irrelevant ones may be omitted")


class DraftProject(_Model):
    source_index: int = Field(description="Index of this project in the original resume's project list")
    name: str | None = None
    tech_stack: list[str] = Field(default_factory=list)
    bullets: list[DraftBullet] = Field(default_factory=list)
    relevance_score: float = Field(0.5, ge=0, le=1, description="How relevant this project is to the JD")


class DraftSkills(_Model):
    technical: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    soft: list[str] = Field(default_factory=list)


class TailorDraft(_Model):
    summary: str = Field(description="2-4 line professional summary for this JD, using only resume facts")
    summary_reason: str | None = None
    skills: DraftSkills = Field(default_factory=DraftSkills)
    experience: list[DraftExperience] = Field(default_factory=list)
    projects: list[DraftProject] = Field(default_factory=list, description="Most relevant first")
    section_order: list[str] = Field(default_factory=list, description=f"Order of sections, from: {', '.join(SECTIONS)}")


# ---------------------------------------------------------------------------
# Validated, editable document
# ---------------------------------------------------------------------------


class TailoredBullet(BaseModel):
    text: str  # current text: the AI rewrite, or the user's edit
    original_text: str
    ai_text: str  # the validated AI rewrite (kept so "reject" then "accept" restores it)
    changed: bool = False
    reason: str | None = None
    jd_requirement: str | None = None
    needs_user_input: bool = False  # contains a placeholder such as [X%] or [if true: Docker]
    decision: Decision = "pending"  # pending counts as accepted when previewing / exporting
    hidden: bool = False


class TailoredExperience(BaseModel):
    source_index: int
    company: str | None = None
    title: str | None = None
    location: str | None = None
    start: str | None = None
    end: str | None = None
    bullets: list[TailoredBullet] = Field(default_factory=list)
    relevance_score: float | None = None


class TailoredProject(BaseModel):
    source_index: int
    name: str | None = None
    tech_stack: list[str] = Field(default_factory=list)
    link: str | None = None
    start: str | None = None
    end: str | None = None
    bullets: list[TailoredBullet] = Field(default_factory=list)
    relevance_score: float = 0.5
    hidden: bool = False


class TailoredSkills(BaseModel):
    technical: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    soft: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)


class ChangeLogEntry(BaseModel):
    section: str
    before: str | None = None
    after: str | None = None
    reason: str | None = None
    linked_jd_requirement: str | None = None


class TailoredResume(BaseModel):
    basics: Basics  # copied from the original; contact info is never changed
    summary: str = ""
    original_summary: str | None = None
    ai_summary: str = ""
    summary_reason: str | None = None
    summary_decision: Decision = "pending"
    skills: TailoredSkills = Field(default_factory=TailoredSkills)
    experience: list[TailoredExperience] = Field(default_factory=list)
    projects: list[TailoredProject] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    achievements: list[str] = Field(default_factory=list)
    section_order: list[str] = Field(default_factory=lambda: list(SECTIONS))
    hidden_sections: list[str] = Field(default_factory=list)
    changes_log: list[ChangeLogEntry] = Field(default_factory=list)
