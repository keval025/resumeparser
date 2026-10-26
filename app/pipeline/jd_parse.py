"""Job description parsing: pasted JD text -> validated ``JobDescription``.

One Gemini call (structured output, temperature 0), validated with Pydantic and retried once,
then cleaned in Python: skills go through the same alias map as resumes (JS -> JavaScript),
lists are de-duplicated, and a skill listed as both required and preferred stays required.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

from app.match_schema import JobDescription
from app.pipeline.llm import LLMProvider
from app.pipeline.llm_extract import complete_validated
from app.pipeline.normalize import load_alias_maps, normalize_skill_list

logger = logging.getLogger(__name__)

MIN_JD_CHARS = 50
MAX_JD_CHARS = 30000

SYSTEM_PROMPT = """You extract structured data from a job description (JD) into JSON.

Rules:
- Extract ONLY what the JD states. Never invent requirements. Use null / [] when something is not stated.
- required_skills: must-have skills ("required", "must have", "you have", "minimum qualifications").
  preferred_skills: nice-to-have skills ("preferred", "bonus", "plus", "nice to have").
  If the JD does not distinguish, treat the skills in the main requirements list as required.
- One skill per item, short canonical names ("Python", "SQL", "A/B testing"), not sentences.
- Alternatives the JD offers as either/or ("Django or FastAPI", "Amplitude or Mixpanel") stay ONE item,
  joined with " / ": "Django / FastAPI". Do not split them into separate required skills.
- tools: named software, platforms, cloud services, databases (e.g. "Jira", "AWS", "PostgreSQL").
  A tool may also appear in required_skills or preferred_skills if the JD makes it a requirement.
- seniority: one of intern, entry, mid, senior, lead - from the title and years required
  (0-1 yrs entry, 2-4 mid, 5-8 senior, lead/principal/staff/manager-of-managers = lead).
- min_years_experience / max_years_experience: numbers from phrases like "3-5 years" (3, 5) or "5+ years" (5, null).
- education_requirements.min_degree: e.g. "Bachelor's", "Master's", "PhD"; fields: e.g. ["Computer Science"].
- responsibilities: one entry per duty, close to verbatim.
- domain_keywords: 5-15 industry/domain terms a recruiter's ATS would search for (e.g. "LLM", "fintech", "microservices").
- soft_skills: e.g. "Communication", "Stakeholder management"."""


def normalize_jd_text(text: str) -> str:
    """Whitespace-normalised JD text (also the input to the cache hash)."""
    return "\n".join(" ".join(line.split()) for line in text.strip().splitlines() if line.strip())


def jd_hash(text: str) -> str:
    """SHA-256 of the normalised JD text, so re-pasting with different spacing hits the cache."""
    return hashlib.sha256(normalize_jd_text(text).encode("utf-8")).hexdigest()


_ALTERNATIVES = re.compile(r"\s+/\s+|\s+or\s+", re.IGNORECASE)


def split_alternatives(skill: str) -> list[str]:
    """'Django / FastAPI' or 'Django or FastAPI' -> ['Django', 'FastAPI'] ('CI/CD' stays whole)."""
    return [part.strip() for part in _ALTERNATIVES.split(skill) if part.strip()]


def _normalize_skills(skills: list[str], skill_map: dict[str, str]) -> list[str]:
    """Alias-normalise each skill, keeping either/or items together as 'A / B'."""
    joined = [" / ".join(normalize_skill_list(split_alternatives(s), skill_map)) for s in skills if s and s.strip()]
    return normalize_skill_list(joined, {})


def clean_jd(jd: JobDescription, alias_file: Path) -> JobDescription:
    """Normalise skill names with the resume alias map and remove duplicates across lists."""
    skill_map, _ = load_alias_maps(str(alias_file))

    def parts(skills: list[str]) -> set[str]:
        return {p.lower() for s in skills for p in split_alternatives(s)}

    def without(skills: list[str], seen: set[str]) -> list[str]:
        """Drop items already covered (e.g. 'Django' when 'Django / FastAPI' is required)."""
        return [s for s in skills if not {p.lower() for p in split_alternatives(s)} <= seen]

    jd.required_skills = _normalize_skills(jd.required_skills, skill_map)
    jd.preferred_skills = without(_normalize_skills(jd.preferred_skills, skill_map), parts(jd.required_skills))
    jd.tools = without(_normalize_skills(jd.tools, skill_map), parts(jd.required_skills + jd.preferred_skills))
    jd.soft_skills = normalize_skill_list(jd.soft_skills, skill_map)
    jd.certifications = normalize_skill_list(jd.certifications, {})
    jd.domain_keywords = normalize_skill_list(jd.domain_keywords, {})
    jd.education_requirements.fields = normalize_skill_list(jd.education_requirements.fields, {})
    jd.responsibilities = [r.strip(" -•*\t") for r in jd.responsibilities if r and r.strip(" -•*\t")]
    if (
        jd.min_years_experience is not None
        and jd.max_years_experience is not None
        and jd.max_years_experience < jd.min_years_experience
    ):
        jd.min_years_experience, jd.max_years_experience = jd.max_years_experience, jd.min_years_experience
    return jd


def parse_jd(provider: LLMProvider, text: str, alias_file: Path) -> JobDescription:
    """Extract and clean a job description. Raises ``LLMError`` / ``LLMExtractionError`` on failure."""
    prompt = f"Extract the job description below into the JSON schema.\n\n<job_description>\n{normalize_jd_text(text)}\n</job_description>"
    jd = complete_validated(provider, SYSTEM_PROMPT, prompt, JobDescription)
    jd = clean_jd(jd, alias_file)
    logger.info(
        "jd parsed",
        extra={"title": jd.title, "required": len(jd.required_skills), "preferred": len(jd.preferred_skills)},
    )
    return jd
