"""Improvement suggestions: parsed resume + parsed JD + score breakdown -> ``Suggestions``.

Gemini writes the advice; Python then keeps it honest:
  * any number in a rewrite that does not appear in the resume becomes a "[X]" placeholder,
  * "missing keywords to add" that are real gaps (no evidence anywhere in the resume) move to
    ``skills_to_learn``, so "add because you have it" and "learn because it's a gap" never mix,
  * a technology a bullet rewrite attaches to a job/project that does not state it becomes "[if true: X]",
  * score gains are capped at what the component can still gain, and priorities are sorted by gain.
"""

from __future__ import annotations

import json
import math
import re

from rapidfuzz import fuzz

from app.match_schema import JobDescription, MatchResult, SkillToLearn, Suggestions
from app.pipeline.jd_parse import split_alternatives
from app.pipeline.llm import LLMProvider
from app.pipeline.llm_extract import complete_validated
from app.pipeline.match import resume_text
from app.schema import ResumeData

SYSTEM_PROMPT = """You are an expert resume coach and technical recruiter. You get a parsed resume, a parsed job
description (JD) and a deterministic match-score breakdown. Write specific, actionable advice that
raises this candidate's score FOR THIS JD.

Hard rules - breaking any of them makes the advice harmful:
1. NEVER invent experience, employers, job titles, projects, metrics, numbers, dates, certifications, skills
   or activities. Every rewrite must use only facts already in the resume:
   - You may rephrase, reorder, lead with a stronger verb, and use the JD's wording for the SAME fact.
   - A rewrite of a job/project bullet may name a technology only if the resume lists it for that same
     job/project (its bullets, skills_used or tech_stack). Skills listed only in the skills section must not
     be attached to a specific job.
   - Do NOT add things the resume does not say the candidate did (e.g. "optimised SQL queries" when the
     resume only says "built REST APIs"). If the JD wants something the candidate may have done, write it as
     a bracketed prompt the candidate must confirm, e.g. "[if true: optimised PostgreSQL queries]".
   - Where a number would help but is not in the resume, use a placeholder: [X%], [N users], [X ms].
2. Separate the two kinds of gaps:
   - missing_keywords_to_add: JD keywords the candidate can honestly add because the resume already shows
     evidence (e.g. JD says "REST APIs" and a bullet says "built APIs with Django"). Only these.
   - skills_to_learn: real gaps the resume gives no evidence for. The candidate must not claim them.
3. Be specific to this JD: name its skills, keywords and responsibilities. No generic advice
   ("tailor your resume", "use action verbs") unless tied to a concrete line of this resume.
4. top_priorities: 5-8 items sorted by estimated_score_gain (highest first). The gain is the number of points
   that fixing the issue could add, and can never exceed what that component is still missing
   (see score_breakdown: max - score). area is one of skills, experience, projects, keywords, education,
   formatting, summary.
5. bullets_to_rewrite: up to 6 bullets. "original" must be copied verbatim from the resume.
   "improved" leads with a strong action verb, works in the JD's language where truthful, and shows impact.
6. suggested_summary: 2-3 sentences tailored to the JD, using only resume facts.
7. summary: a 2-3 line verdict on fit, naming the biggest strength and the biggest gap."""

# Suggestion area -> score components it can improve (for capping estimated gains).
_AREA_COMPONENTS = {
    "skills": ("required_skills", "preferred_skills"),
    "experience": ("experience_relevance",),
    "projects": ("experience_relevance",),
    "keywords": ("keywords", "required_skills", "preferred_skills"),
    "education": ("education",),
    "formatting": ("resume_quality",),
    "summary": ("keywords",),
}

_NUMBER = re.compile(r"(?<![\w\[])\d[\d,.]*(?:\s?%|\+|x\b|k\b|m\b)?", re.IGNORECASE)


def _compact_resume(data: ResumeData) -> dict:
    """The resume without pipeline metadata, to keep the prompt small."""
    return data.model_dump(mode="json", exclude={"meta"}) | {
        "total_experience_months": data.meta.total_experience_months
    }


def _compact_breakdown(result: MatchResult) -> dict:
    return {
        "total": result.score,
        "label": result.label,
        "components": {k: {"score": c.score, "max": c.max, "details": c.details} for k, c in result.breakdown.items()},
        "matched_skills": result.matched_skills,
        "missing_required_skills": result.missing_required_skills,
        "missing_preferred_skills": result.missing_preferred_skills,
        "missing_keywords": [
            k["term"] for k in result.breakdown["keywords"].data.get("keywords", []) if not k["present"]
        ],
    }


def build_prompt(data: ResumeData, jd: JobDescription, result: MatchResult) -> str:
    return (
        "Write improvement suggestions for this resume against this job description.\n\n"
        f"<resume>\n{json.dumps(_compact_resume(data), ensure_ascii=False)}\n</resume>\n\n"
        f"<job_description>\n{jd.model_dump_json()}\n</job_description>\n\n"
        f"<score_breakdown>\n{json.dumps(_compact_breakdown(result), ensure_ascii=False)}\n</score_breakdown>"
    )


def numbers_in(text: str) -> set[str]:
    return {re.sub(r"[\s,]", "", m.group(0)).lower().rstrip(".") for m in _NUMBER.finditer(text)}


def _bare(token: str) -> str:
    """'40%' -> '40', '2m' -> '2', '1,200' -> '1200'."""
    return re.sub(r"[^\d.]", "", token).rstrip(".")


def strip_invented_numbers(text: str, known: set[str]) -> tuple[str, bool]:
    """Replace numbers that are not in the resume with '[X]'. Returns (text, changed)."""
    changed = False
    known_bare = {_bare(k) for k in known}

    def repl(match: re.Match[str]) -> str:
        nonlocal changed
        token = re.sub(r"[\s,]", "", match.group(0)).lower().rstrip(".")
        if token in known or _bare(token) in known_bare:
            return match.group(0)
        changed = True
        return "[X%]" if "%" in token else "[X]"

    return _NUMBER.sub(repl, text), changed


def _item_text(data: ResumeData, bullet: str) -> str:
    """Text of the job / project that contains ``bullet`` (its bullets + named technologies); '' if not found."""
    items = [
        (job.bullets, [*job.bullets, *job.skills_used, job.title or ""]) for job in data.experience
    ] + [
        ([*p.outcomes, p.description or ""], [*p.outcomes, *p.tech_stack, p.description or "", p.name or ""]) for p in data.projects
    ]
    for lines, text in items:
        if any(fuzz.ratio(bullet.lower(), line.lower()) >= 90 for line in lines if line):
            return "\n".join(text)
    return ""


def _skill_names(data: ResumeData, result: MatchResult) -> list[str]:
    """Technology names to police in rewrites: every JD skill and every skill named anywhere in the resume."""
    names: list[str] = [*data.skills.technical, *data.skills.tools]
    for component in ("required_skills", "preferred_skills"):
        for match in result.breakdown[component].data.get("matches", []):
            names += split_alternatives(match["skill"])
    return [n for n in dict.fromkeys(names) if len(n) >= 2]


def mentions(name: str, text: str) -> bool:
    return re.search(rf"(?<![\w+#]){re.escape(name)}(?![\w+#])", text, re.IGNORECASE) is not None


def flag_unsupported_skills(improved: str, item_text: str, skill_names: list[str]) -> tuple[str, list[str]]:
    """Wrap a technology the rewrite attaches to a job/project that does not state it as '[if true: X]'."""
    if not item_text:  # bullet not found in the resume: cannot tell, leave it to the prompt rules
        return improved, []
    flagged: list[str] = []
    for name in sorted(skill_names, key=len, reverse=True):
        if mentions(name, improved) and not mentions(name, item_text) and f"[if true: {name}]".lower() not in improved.lower():
            improved = re.sub(rf"(?<![\w+#\[]){re.escape(name)}(?![\w+#])", f"[if true: {name}]", improved, count=1, flags=re.IGNORECASE)
            flagged.append(name)
    return improved, flagged


def _has_evidence(term: str, text_lower: str) -> bool:
    words = [w for w in re.findall(r"[a-z0-9+#]+", term.lower()) if len(w) > 2]
    return bool(words) and all(w in text_lower for w in words)


def ground_suggestions(suggestions: Suggestions, data: ResumeData, raw_text: str, result: MatchResult) -> tuple[Suggestions, list[str]]:
    """Apply the Python-side honesty checks. Returns (suggestions, warnings)."""
    source = resume_text(data, raw_text)
    known = numbers_in(source)
    text_lower = source.lower()
    warnings: list[str] = []

    for item in suggestions.top_priorities:
        item.example_rewrite, changed = strip_invented_numbers(item.example_rewrite, known)
        if changed:
            warnings.append(f"invented_number_removed: priority '{item.issue[:60]}'")
        headroom = max(
            (result.breakdown[c].max - result.breakdown[c].score for c in _AREA_COMPONENTS.get(item.area, ())),
            default=0,
        )
        item.estimated_score_gain = max(0, min(item.estimated_score_gain, math.ceil(headroom - 1e-9)))
    suggestions.top_priorities.sort(key=lambda p: -p.estimated_score_gain)  # stable: ties keep model order

    skill_names = _skill_names(data, result)
    for rewrite in suggestions.bullets_to_rewrite:
        rewrite.improved, changed = strip_invented_numbers(rewrite.improved, known)
        if changed:
            warnings.append(f"invented_number_removed: bullet '{rewrite.original[:60]}'")
        rewrite.improved, flagged = flag_unsupported_skills(rewrite.improved, _item_text(data, rewrite.original), skill_names)
        warnings.extend(f"unsupported_skill_flagged: '{skill}' in bullet '{rewrite.original[:60]}'" for skill in flagged)
    suggestions.suggested_summary, changed = strip_invented_numbers(suggestions.suggested_summary, known)
    if changed:
        warnings.append("invented_number_removed: suggested_summary")

    # A keyword the scorer found missing, with no supporting words anywhere in the resume, is a gap.
    missing = {s.lower() for s in result.missing_required_skills + result.missing_preferred_skills}
    keep, gaps = [], []
    for keyword in dict.fromkeys(suggestions.missing_keywords_to_add):
        is_gap = keyword.lower() in missing and not _has_evidence(keyword, text_lower)
        (gaps if is_gap else keep).append(keyword)
    suggestions.missing_keywords_to_add = keep
    learn = {s.skill.lower() for s in suggestions.skills_to_learn}
    for gap in gaps:
        warnings.append(f"keyword_moved_to_skills_to_learn: {gap}")
        if gap.lower() not in learn:
            suggestions.skills_to_learn.append(
                SkillToLearn(skill=gap, why="The JD asks for it and the resume shows no evidence of it yet.", priority="medium")
            )
    return suggestions, warnings


def suggest(provider: LLMProvider, data: ResumeData, raw_text: str, jd: JobDescription, result: MatchResult) -> tuple[Suggestions, list[str]]:
    """Ask Gemini for suggestions (validated, retried once) and ground them."""
    suggestions = complete_validated(provider, SYSTEM_PROMPT, build_prompt(data, jd, result), Suggestions)
    return ground_suggestions(suggestions, data, raw_text, result)
