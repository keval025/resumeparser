"""Stage 7 - LLM extraction.

Design
------
* The vendor-specific code lives in ``app/pipeline/llm/`` behind a tiny interface
  (``LLMProvider.complete_json``). The default provider is Google Gemini; tests use a fake one.
* The provider receives the Pydantic model from ``app/schema.py`` and uses the vendor's
  structured-output mode, so the answer is JSON shaped like that model.
* We still validate with Pydantic; if validation fails we retry ONCE, telling the model what
  was wrong.
* Short resumes: one call for everything. Long resumes: one call per section, run in parallel.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.pipeline.llm import LLMError, LLMProvider, get_provider
from app.schema import (
    AchievementsSection,
    BasicsSection,
    CertificationsSection,
    EducationSection,
    ExperienceSection,
    ExtractedResume,
    ProjectsSection,
    SkillsSection,
)

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Re-exported so existing imports (``from app.pipeline.llm_extract import LLMError``) keep working.
__all__ = [
    "LLMError",
    "LLMExtractionError",
    "LLMProvider",
    "ResumeExtractor",
    "complete_validated",
    "format_hints",
    "get_provider",
]


class LLMExtractionError(RuntimeError):
    """The LLM output could not be validated even after a retry."""


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You extract structured data from resume text into JSON.

Rules:
- Extract ONLY information that is explicitly present in the resume text. Never invent, infer or embellish.
- Copy names, companies, job titles, institutions, project names and skills exactly as written.
- Use null for any missing single value and [] for missing lists. Do not guess.
- Copy dates exactly as written (e.g. "Jan 2021", "2019", "Present"); do not convert them.
- Leave "degree_normalized" and "duration_months" as null; a later step computes them.
- bullets / outcomes: one entry per bullet point, copied verbatim without the bullet symbol.
- skills_used / tech_stack: only technologies explicitly named in that job or project.
- skills.technical = programming languages, frameworks, libraries, concepts;
  skills.tools = software, platforms, cloud services, databases, IDEs;
  skills.soft = interpersonal skills; skills.languages = human spoken languages only.
- achievements: awards, honours, rankings, competitions, publications, positions of responsibility.
- The text was extracted from a PDF and may contain layout noise; ignore page numbers and stray symbols."""


def _build_prompt(task: str, text: str, hints: str = "") -> str:
    hint_block = f"\n\nHints from deterministic parsing (use only if consistent with the text):\n{hints}" if hints else ""
    return f"{task}{hint_block}\n\n<resume_text>\n{text}\n</resume_text>"


def _build_retry_prompt(prompt: str, bad_output: str, error: str) -> str:
    return (
        prompt
        + "\n\nYour previous answer failed validation. Fix the problem and answer again with the full JSON."
        + f"\n<previous_answer>\n{bad_output[:6000]}\n</previous_answer>\n<validation_error>\n{error[:3000]}\n</validation_error>"
    )


def complete_validated(
    provider: LLMProvider, system: str, prompt: str, model: type[T], temperature: float | None = None
) -> T:
    """One LLM call validated against ``model``, with a single corrective retry.

    Shared by resume extraction, JD parsing, relevance rating, suggestions and tailoring.
    ``temperature`` is only passed to the provider when given (default: the provider's 0).
    """
    extra = {"temperature": temperature} if temperature is not None else {}
    output = provider.complete_json(system, prompt, model, **extra)
    try:
        return model.model_validate_json(output)
    except ValidationError as exc:
        logger.warning("llm output failed validation; retrying", extra={"model": model.__name__, "error": str(exc)})
        output = provider.complete_json(system, _build_retry_prompt(prompt, output, str(exc)), model, **extra)
        try:
            return model.model_validate_json(output)
        except ValidationError as exc2:
            raise LLMExtractionError(f"{model.__name__}: output invalid after retry: {exc2}") from exc2


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------

# Section name -> (wrapper model, instruction). Sections not listed are folded into others below.
_SECTION_TASKS: dict[str, tuple[type[BaseModel], str]] = {
    "experience": (ExperienceSection, "Extract every job / internship from this Work Experience section."),
    "education": (EducationSection, "Extract every education entry from this Education section."),
    "projects": (ProjectsSection, "Extract every project from this Projects section."),
    "skills": (SkillsSection, "Extract and categorise the skills from this Skills section."),
    "certifications": (CertificationsSection, "Extract every certification / course from this section."),
    "achievements": (AchievementsSection, "Extract every achievement / award / publication from this section."),
}
# Extra sections whose text is appended to another section's call.
_SECTION_ALIASES = {
    "languages": "skills",
    "publications": "achievements",
    "volunteering": "achievements",
}


class ResumeExtractor:
    """Runs the LLM extraction for a whole resume."""

    def __init__(self, provider: LLMProvider, single_call_max_chars: int = 12000, max_workers: int = 4) -> None:
        self.provider = provider
        self.single_call_max_chars = single_call_max_chars
        self.max_workers = max_workers

    def call(self, model: type[T], task: str, text: str, hints: str = "") -> T:
        """One LLM call validated against ``model``, with a single corrective retry."""
        return complete_validated(self.provider, SYSTEM_PROMPT, _build_prompt(task, text, hints), model)

    def extract(self, sections: dict[str, str], full_text: str, hints: str = "") -> tuple[ExtractedResume, str]:
        """Extract the full resume. Returns ``(resume, mode)`` where mode is 'single' or 'per_section'."""
        if len(full_text) <= self.single_call_max_chars:
            resume = self.call(
                ExtractedResume, "Extract the complete resume below into the JSON schema.", full_text, hints
            )
            return resume, "single"
        return self._extract_per_section(sections, hints), "per_section"

    def _extract_per_section(self, sections: dict[str, str], hints: str) -> ExtractedResume:
        # Group section texts by the call that will handle them.
        grouped: dict[str, list[str]] = {}
        for name, text in sections.items():
            if not text.strip() or name in {"header", "summary"}:
                continue
            target = _SECTION_ALIASES.get(name, name)
            if target in _SECTION_TASKS:
                grouped.setdefault(target, []).append(text)
            else:
                logger.info("section not mapped to an extraction call", extra={"section": name})

        basics_text = "\n\n".join(t for t in (sections.get("header", ""), sections.get("summary", "")) if t.strip())
        jobs: dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            jobs["basics"] = pool.submit(
                self.call,
                BasicsSection,
                "Extract the candidate's basic details (name, contact, location, links, summary) from this resume header.",
                basics_text or "(empty)",
                hints,
            )
            for target, texts in grouped.items():
                model, task = _SECTION_TASKS[target]
                jobs[target] = pool.submit(self.call, model, task, "\n\n".join(texts))
            results = {name: future.result() for name, future in jobs.items()}

        resume = ExtractedResume(basics=results["basics"].basics)
        for target, result in results.items():
            if target != "basics":
                setattr(resume, target, getattr(result, target))
        return resume


def format_hints(name: str | None, email: str | None, phone: str | None, links: dict[str, object]) -> str:
    """Turn regex/layout findings into a short hint block for the prompt."""
    lines = []
    if name:
        lines.append(f"- Name candidate (largest text at top of page 1): {name}")
    if email:
        lines.append(f"- Email: {email}")
    if phone:
        lines.append(f"- Phone: {phone}")
    found_links = {k: v for k, v in links.items() if v}
    if found_links:
        lines.append(f"- Links (some come from hidden hyperlinks): {json.dumps(found_links)}")
    return "\n".join(lines)
