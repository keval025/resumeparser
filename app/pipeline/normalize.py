"""Stage 8 - Normalisation.

* Dates      -> "YYYY-MM", or "present" for Present/Current/Now/Till date.
* Durations  -> ``duration_months`` per job (inclusive: Jan-Mar = 3 months) and total experience
                (overlapping jobs are merged so they are not counted twice).
* Skills     -> canonical names from ``app/data/normalization.json`` (JS -> JavaScript), de-duplicated.
* Degrees    -> ``degree_normalized`` from the same file (B.Tech -> Bachelor's - Engineering).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from functools import lru_cache
from pathlib import Path

import dateparser

from app.schema import ExtractedResume

logger = logging.getLogger(__name__)

PRESENT = "present"
_PRESENT_WORDS = {"present", "current", "currently", "now", "till date", "to date", "ongoing", "today", "date", "till now"}
_SEASONS = {"spring": 3, "summer": 6, "fall": 9, "autumn": 9, "winter": 12}
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


def _two_digit_year(yy: int, today: date) -> int:
    """'21 -> 2021, '98 -> 1998 (years more than 1 year in the future roll back a century)."""
    year = 2000 + yy
    return year - 100 if year > today.year + 1 else year


def normalize_date(value: str | None, today: date | None = None) -> str | None:
    """Convert a resume date string to "YYYY-MM" / "present", or None if it cannot be parsed.

    Year-only dates ("2020") become "2020-01".
    """
    if value is None:
        return None
    today = today or date.today()
    text = " ".join(str(value).strip().lower().replace("’", "'").split()).strip(" .,()")
    if not text:
        return None
    if text in _PRESENT_WORDS or text.startswith(("present", "current")):
        return PRESENT

    # Already ISO: 2020-01 / 2020/1 / 2020.01 / 2020-01-15
    m = re.fullmatch(r"((?:19|20)\d{2})[-/.](\d{1,2})(?:[-/.]\d{1,2})?", text)
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    # Month/year numeric: 01/2020, 1-2020, 01.2020
    m = re.fullmatch(r"(\d{1,2})[-/.]((?:19|20)\d{2})", text)
    if m and 1 <= int(m.group(1)) <= 12:
        return f"{m.group(2)}-{int(m.group(1)):02d}"
    # Day/month/year numeric: 15/01/2020 (DMY, the common format in India/UK)
    m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.]((?:19|20)\d{2})", text)
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{m.group(3)}-{int(m.group(2)):02d}"
    # Year only
    m = re.fullmatch(r"((?:19|20)\d{2})", text)
    if m:
        return f"{m.group(1)}-01"
    # Month name + year: "Jan 2020", "January, 2020", "Jan'20", "Sept. 2021"
    m = re.fullmatch(r"([a-z]+)\.?,?\s*'?\s*(\d{2}|\d{4})", text)
    if m:
        word, year_text = m.group(1), m.group(2)
        year = int(year_text) if len(year_text) == 4 else _two_digit_year(int(year_text), today)
        if word in _SEASONS:
            return f"{year}-{_SEASONS[word]:02d}"
        month = _MONTHS.get(word[:4]) or _MONTHS.get(word[:3])
        if month:
            return f"{year}-{month:02d}"

    # Fallback: dateparser handles many other formats and languages.
    try:
        parsed = dateparser.parse(
            text,
            settings={"PREFER_DAY_OF_MONTH": "first", "REQUIRE_PARTS": ["year"], "DATE_ORDER": "DMY"},
        )
    except Exception:  # dateparser can raise on odd input; treat as unparseable
        parsed = None
    if parsed is not None and 1900 <= parsed.year <= today.year + 10:
        return f"{parsed.year:04d}-{parsed.month:02d}"
    return None


def _to_index(ym: str, today: date) -> int:
    """'2021-03' -> month index (year*12 + month-1); 'present' -> today's index."""
    if ym == PRESENT:
        return today.year * 12 + today.month - 1
    year, month = ym.split("-")
    return int(year) * 12 + int(month) - 1


def months_between(start: str | None, end: str | None, today: date | None = None) -> int | None:
    """Inclusive number of months from start to end, or None if unknown/invalid."""
    if not start or not end or start == PRESENT:
        return None
    today = today or date.today()
    diff = _to_index(end, today) - _to_index(start, today)
    return diff + 1 if diff >= 0 else None


def total_experience_months(periods: list[tuple[str | None, str | None]], today: date | None = None) -> int | None:
    """Total months across jobs, merging overlaps. None when no job has usable dates."""
    today = today or date.today()
    intervals: list[tuple[int, int]] = []
    for start, end in periods:
        if months_between(start, end, today) is None:
            continue
        intervals.append((_to_index(start, today), _to_index(end, today)))  # type: ignore[arg-type]
    if not intervals:
        return None
    intervals.sort()
    total = 0
    cur_start, cur_end = intervals[0]
    for s, e in intervals[1:]:
        if s <= cur_end + 1:
            cur_end = max(cur_end, e)
        else:
            total += cur_end - cur_start + 1
            cur_start, cur_end = s, e
    total += cur_end - cur_start + 1
    return total


# ---------------------------------------------------------------------------
# Alias maps (skills / degrees)
# ---------------------------------------------------------------------------


def alias_key(text: str) -> str:
    """Key used for alias lookups: lowercase, dots/commas/hyphens/brackets -> space, single spaces."""
    text = text.lower().replace("’", "'").replace("'s", "s")
    text = re.sub(r"[.,\-()]", " ", text)
    return " ".join(text.split())


@lru_cache(maxsize=8)
def load_alias_maps(path: str) -> tuple[dict[str, str], dict[str, str]]:
    """Load (skills, degrees) alias maps from the editable JSON file."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    skills = {alias_key(k): v for k, v in raw.get("skills", {}).items()}
    degrees = {alias_key(k): v for k, v in raw.get("degrees", {}).items()}
    return skills, degrees


def normalize_skill(skill: str, skill_map: dict[str, str]) -> str:
    """Map a skill alias to its canonical name; unknown skills are returned trimmed."""
    cleaned = " ".join(skill.split()).strip(" ,;.-")
    return skill_map.get(alias_key(cleaned), cleaned)


def normalize_skill_list(skills: list[str], skill_map: dict[str, str]) -> list[str]:
    """Normalise and de-duplicate (case-insensitively) while keeping the original order."""
    out: dict[str, str] = {}
    for skill in skills:
        if not skill or not skill.strip():
            continue
        canonical = normalize_skill(skill, skill_map)
        out.setdefault(canonical.lower(), canonical)
    return list(out.values())


def normalize_degree(degree: str | None, degree_map: dict[str, str]) -> str | None:
    """'B.Tech in Computer Science' -> "Bachelor's - Engineering" (longest alias prefix wins)."""
    if not degree:
        return None
    key = alias_key(degree)
    if key in degree_map:
        return degree_map[key]
    for alias in sorted(degree_map, key=len, reverse=True):
        if key.startswith(alias + " "):
            return degree_map[alias]
    return None


# ---------------------------------------------------------------------------
# Whole-resume normalisation
# ---------------------------------------------------------------------------


def normalize_resume(
    resume: ExtractedResume, alias_file: Path, today: date | None = None
) -> tuple[ExtractedResume, int | None, list[str]]:
    """Normalise dates, durations, skills and degrees in place.

    Returns ``(resume, total_experience_months, warnings)``.
    """
    today = today or date.today()
    skill_map, degree_map = load_alias_maps(str(alias_file))
    warnings: list[str] = []

    def _date(raw: str | None, where: str) -> str | None:
        value = normalize_date(raw, today)
        if raw and value is None:
            warnings.append(f"unparsed_date: {where} = {raw!r}")
        return value

    for i, job in enumerate(resume.experience):
        job.start = _date(job.start, f"experience[{i}].start")
        job.end = _date(job.end, f"experience[{i}].end")
        job.duration_months = months_between(job.start, job.end, today)
        job.skills_used = normalize_skill_list(job.skills_used, skill_map)
        job.bullets = [b.strip().lstrip("-• ").strip() for b in job.bullets if b and b.strip()]

    for i, edu in enumerate(resume.education):
        edu.start = _date(edu.start, f"education[{i}].start")
        edu.end = _date(edu.end, f"education[{i}].end")
        edu.degree_normalized = normalize_degree(edu.degree, degree_map)

    for i, project in enumerate(resume.projects):
        project.start = _date(project.start, f"projects[{i}].start")
        project.end = _date(project.end, f"projects[{i}].end")
        project.tech_stack = normalize_skill_list(project.tech_stack, skill_map)

    for i, cert in enumerate(resume.certifications):
        cert.date = _date(cert.date, f"certifications[{i}].date")

    skills = resume.skills
    skills.technical = normalize_skill_list(skills.technical, skill_map)
    skills.tools = normalize_skill_list(skills.tools, skill_map)
    skills.soft = normalize_skill_list(skills.soft, skill_map)
    skills.languages = normalize_skill_list(skills.languages, skill_map)

    total = total_experience_months([(j.start, j.end) for j in resume.experience], today)
    return resume, total, warnings
