"""Stage 9 - Validation, grounding and confidence.

Grounding
---------
Every company, job title, institution, project name and skill the LLM returns must appear
(fuzzily, rapidfuzz ``partial_ratio`` >= threshold) in the source text. Anything that does not
is removed and reported as ``possible_hallucination``. Grounding runs on the *raw* LLM values,
before skills are renamed by normalisation ("JS" is in the text, "JavaScript" may not be).

Sanity rules
------------
end >= start, valid email format, plausible phone.

Confidence
----------
A heuristic 0-1 score per top-level field. ``needs_review`` is True when any score < threshold.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from app.pipeline.normalize import PRESENT
from app.pipeline.rules import RuleResult, is_valid_email, normalize_phone
from app.schema import ExtractedResume

logger = logging.getLogger(__name__)

TOP_LEVEL_FIELDS = ("basics", "education", "experience", "projects", "skills", "certifications", "achievements")


# ---------------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------------


def _norm(text: str) -> str:
    """Lowercase, unify quotes/dashes and collapse whitespace for fuzzy comparison."""
    text = text.lower().replace("’", "'").replace("–", "-").replace("—", "-")
    return " ".join(text.split())


class SourceIndex:
    """The resume's source text, pre-normalised for repeated grounding checks."""

    def __init__(self, source_text: str) -> None:
        self.text = _norm(source_text)
        # Version without spaces/punctuation, for things like "Node.js" vs "NodeJS".
        self.compact = re.sub(r"[^a-z0-9+#]", "", self.text)

    def contains(self, value: str, threshold: float = 85) -> bool:
        """True if ``value`` appears in the source text (exactly or fuzzily)."""
        needle = _norm(value)
        if not needle:
            return False
        if len(needle) <= 3:
            # Very short values ("C", "R", "Go", "AWS"): fuzzy matching is meaningless, require a
            # whole-word match.
            return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", self.text) is not None
        if needle in self.text:
            return True
        compact = re.sub(r"[^a-z0-9+#]", "", needle)
        if len(compact) >= 4 and compact in self.compact:
            return True
        return fuzz.partial_ratio(needle, self.text) >= threshold


@dataclass
class GroundingReport:
    """What grounding removed. ``dropped`` holds human-readable paths like experience[0].company."""

    dropped: list[str] = field(default_factory=list)
    # top-level field -> entries with at least one ungrounded value ("experience[2]", "skills.tools[1]")
    flagged_entries: dict[str, set[str]] = field(default_factory=dict)

    def add(self, top_field: str, path: str, value: str) -> None:
        self.dropped.append(f"possible_hallucination: {path} = {value!r}")
        # Count entries, not fields: a fully invented job counts once, not once per field.
        entry = path if top_field == "skills" else path.split(".")[0]
        self.flagged_entries.setdefault(top_field, set()).add(entry)
        logger.warning("possible_hallucination", extra={"path": path, "value": value})

    @property
    def dropped_by_field(self) -> dict[str, int]:
        """Number of flagged entries per top-level field."""
        return {k: len(v) for k, v in self.flagged_entries.items()}


def _filter_list(values: list[str], index: SourceIndex, threshold: float, report: GroundingReport, top: str, path: str) -> list[str]:
    kept = []
    for i, value in enumerate(values):
        if index.contains(value, threshold):
            kept.append(value)
        else:
            report.add(top, f"{path}[{i}]", value)
    return kept


def ground_extraction(resume: ExtractedResume, source_text: str, threshold: float = 85) -> GroundingReport:
    """Remove ungrounded values from ``resume`` in place and report them.

    Entries that lose their identifying field(s) are removed entirely.
    """
    index = SourceIndex(source_text)
    report = GroundingReport()

    def check(value: str | None, top: str, path: str) -> str | None:
        if value is None or not value.strip():
            return None
        if index.contains(value, threshold):
            return value
        report.add(top, path, value)
        return None

    # Name: if the LLM's name is not in the text at all, drop it.
    resume.basics.name = check(resume.basics.name, "basics", "basics.name")

    kept_jobs = []
    for i, job in enumerate(resume.experience):
        job.company = check(job.company, "experience", f"experience[{i}].company")
        job.title = check(job.title, "experience", f"experience[{i}].title")
        job.skills_used = _filter_list(job.skills_used, index, threshold, report, "experience", f"experience[{i}].skills_used")
        if job.company or job.title:
            kept_jobs.append(job)
    resume.experience = kept_jobs

    kept_edu = []
    for i, edu in enumerate(resume.education):
        edu.institution = check(edu.institution, "education", f"education[{i}].institution")
        if edu.institution or edu.degree:
            kept_edu.append(edu)
    resume.education = kept_edu

    kept_projects = []
    for i, project in enumerate(resume.projects):
        project.name = check(project.name, "projects", f"projects[{i}].name")
        project.tech_stack = _filter_list(project.tech_stack, index, threshold, report, "projects", f"projects[{i}].tech_stack")
        if project.name or project.description:
            kept_projects.append(project)
    resume.projects = kept_projects

    for attr in ("technical", "tools", "soft", "languages"):
        values = getattr(resume.skills, attr)
        setattr(resume.skills, attr, _filter_list(values, index, threshold, report, "skills", f"skills.{attr}"))

    kept_certs = []
    for i, cert in enumerate(resume.certifications):
        cert.name = check(cert.name, "certifications", f"certifications[{i}].name")
        if cert.name:
            kept_certs.append(cert)
    resume.certifications = kept_certs

    return report


# ---------------------------------------------------------------------------
# Sanity rules
# ---------------------------------------------------------------------------


def _date_order_ok(start: str | None, end: str | None) -> bool:
    if not start or not end or end == PRESENT:
        return True
    if start == PRESENT:
        return False
    return end >= start  # "YYYY-MM" strings compare correctly as text


@dataclass
class SanityReport:
    warnings: list[str] = field(default_factory=list)
    issues_by_field: dict[str, int] = field(default_factory=dict)

    def add(self, top_field: str, message: str) -> None:
        self.warnings.append(message)
        self.issues_by_field[top_field] = self.issues_by_field.get(top_field, 0) + 1


def sanity_check(resume: ExtractedResume, rules: RuleResult) -> SanityReport:
    """Apply sanity rules (after normalisation). Fixes what it safely can, reports the rest."""
    report = SanityReport()
    basics = resume.basics

    # Contact details: regex results are exact, so prefer them.
    if rules.email:
        basics.email = rules.email
    elif basics.email and not is_valid_email(basics.email):
        report.add("basics", f"invalid_email: {basics.email!r} removed")
        basics.email = None
    if rules.phone:
        basics.phone = rules.phone
    elif basics.phone:
        normalized = normalize_phone(basics.phone)
        if normalized is None:
            report.add("basics", f"invalid_phone: {basics.phone!r} removed")
        basics.phone = normalized

    for i, job in enumerate(resume.experience):
        if not _date_order_ok(job.start, job.end):
            report.add("experience", f"date_order: experience[{i}] end {job.end} is before start {job.start}")
            job.duration_months = None
    for i, edu in enumerate(resume.education):
        if not _date_order_ok(edu.start, edu.end):
            report.add("education", f"date_order: education[{i}] end {edu.end} is before start {edu.start}")
    for i, project in enumerate(resume.projects):
        if not _date_order_ok(project.start, project.end):
            report.add("projects", f"date_order: projects[{i}] end {project.end} is before start {project.start}")
    return report


def merge_links(resume: ExtractedResume, rules: RuleResult) -> None:
    """Fill links from regex/annotation results (exact) and keep any extra LLM-found ones."""
    links = resume.basics.links
    for kind in ("linkedin", "github", "portfolio"):
        rule_value = rules.links.get(kind)
        if rule_value:
            setattr(links, kind, rule_value)
    others = list(rules.links.get("other", []) or [])  # type: ignore[arg-type]
    known = {u for u in (links.linkedin, links.github, links.portfolio) if u}
    for url in links.other:
        if url not in others and url not in known:
            others.append(url)
    links.other = [u for u in others if u not in known]


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------


def _clamp(x: float) -> float:
    return round(max(0.0, min(1.0, x)), 2)


def score_confidence(
    resume: ExtractedResume,
    rules: RuleResult,
    grounding: GroundingReport,
    sanity: SanityReport,
    detected_sections: set[str],
    unparsed_dates: dict[str, int],
    is_scanned: bool,
) -> dict[str, float]:
    """Heuristic confidence per top-level field (0 = surely wrong, 1 = very likely right).

    Penalties: missing key values, values dropped by grounding, sanity issues, unparsed dates,
    a section heading present in the PDF but nothing extracted from it, and OCR input.
    """
    scores: dict[str, float] = {}
    ocr_penalty = 0.1 if is_scanned else 0.0

    # Basics
    b = resume.basics
    s = 1.0 - ocr_penalty
    if not b.name:
        s -= 0.5
    elif rules.name and fuzz.token_sort_ratio(b.name.lower(), rules.name.lower()) < 80:
        s -= 0.15  # LLM and layout disagree about the name
    if not b.email:
        s -= 0.2
    if not b.phone:
        s -= 0.1
    s -= 0.2 * grounding.dropped_by_field.get("basics", 0)
    s -= 0.1 * sanity.issues_by_field.get("basics", 0)
    scores["basics"] = _clamp(s)

    # List-like fields
    counts = {
        "education": len(resume.education),
        "experience": len(resume.experience),
        "projects": len(resume.projects),
        "certifications": len(resume.certifications),
        "achievements": len(resume.achievements),
        "skills": sum(len(getattr(resume.skills, a)) for a in ("technical", "tools", "soft", "languages")),
    }
    section_present = {
        "education": "education" in detected_sections,
        "experience": "experience" in detected_sections,
        "projects": "projects" in detected_sections,
        "certifications": "certifications" in detected_sections,
        "achievements": bool({"achievements", "publications", "volunteering"} & detected_sections),
        "skills": bool({"skills", "languages"} & detected_sections),
    }
    for name, count in counts.items():
        if count == 0:
            # Empty is fine if the resume has no such section; suspicious if it does.
            scores[name] = 0.3 if section_present[name] else 1.0
            continue
        s = 1.0 - ocr_penalty
        dropped = grounding.dropped_by_field.get(name, 0)
        s -= min(0.5, 0.5 * dropped / (count + dropped))
        s -= 0.1 * sanity.issues_by_field.get(name, 0)
        s -= 0.05 * unparsed_dates.get(name, 0)
        if name == "experience":
            s -= 0.05 * sum(1 for j in resume.experience if not j.start)
        if name == "education":
            s -= 0.05 * sum(1 for e in resume.education if not e.institution)
        scores[name] = _clamp(s)

    return {name: scores[name] for name in TOP_LEVEL_FIELDS}


def needs_review(confidence: dict[str, float], threshold: float = 0.7) -> bool:
    return any(score < threshold for score in confidence.values())
