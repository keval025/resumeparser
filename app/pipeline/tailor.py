"""Tailor My Resume: parsed resume + parsed JD + match result -> a validated, editable ``TailoredResume``.

Gemini (temperature 0.2) drafts the rewrite; ``validate_draft`` then enforces the truthfulness rules
in code, whatever the prompt said:

* contact info, education, certifications and achievements are copied from the original, never generated;
* every job / project must point at an original one (``source_index``); company, title, location and
  dates that differ from the original (rapidfuzz < 85) are reverted and logged;
* every bullet must rewrite an original bullet of the SAME job / project; anything else is dropped;
* numbers that appear nowhere in the original become [X] / [X%] placeholders;
* a technology a bullet attaches to a job / project that never mentions it becomes "[if true: X]";
* skills must already be in the resume (rapidfuzz >= 85); new ones are removed, missing ones re-added;
* bullets and projects the model left out are kept, hidden, so the user can switch them back on.

Any bullet containing a placeholder gets ``needs_user_input = True``; export is blocked until the user
fills it in or removes it.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from rapidfuzz import fuzz

from app.match_schema import JobDescription, MatchResult, Suggestions
from app.pipeline.jd_parse import split_alternatives
from app.pipeline.llm import LLMProvider
from app.pipeline.llm_extract import complete_validated
from app.pipeline.match import compute_match, rate_relevance, resume_text
from app.pipeline.normalize import alias_key, normalize_date
from app.pipeline.suggest import flag_unsupported_skills, mentions, numbers_in, strip_invented_numbers
from app.schema import Project, ResumeData
from app.tailor_schema import (
    SECTIONS,
    ChangeLogEntry,
    DraftBullet,
    DraftExperience,
    TailorDraft,
    TailoredBullet,
    TailoredExperience,
    TailoredProject,
    TailoredResume,
    TailoredSkills,
)

logger = logging.getLogger(__name__)

TAILOR_TEMPERATURE = 0.2
FUZZY_SAME = 85  # rapidfuzz ratio for "this is the same company / title / skill / bullet"
PLACEHOLDER = re.compile(r"\[[^\[\]\n]{1,80}\]")

DEFAULT_ORDER = ["summary", "experience", "projects", "skills", "education", "certifications", "achievements"]
FRESHER_ORDER = ["summary", "projects", "experience", "skills", "education", "certifications", "achievements"]

SYSTEM_PROMPT = """You tailor a candidate's resume to one job description (JD). You get the original resume
(every job, project and bullet is numbered), the parsed JD and a match analysis.

Truthfulness rules - breaking them makes the resume dishonest:
1. Never add companies, job titles, dates, degrees, certifications, skills or projects. Every job and project
   you return must point at an original one via source_index, with company / title / dates unchanged.
2. Every bullet you return must rewrite exactly ONE original bullet of the SAME job or project. Copy that
   original bullet verbatim into original_text. Never merge bullets, never write new ones.
3. Never invent numbers or outcomes. If a metric would strengthen a bullet, insert a placeholder such as [X%],
   [N users] or [X ms] for the candidate to fill in.
4. Add a JD keyword to a bullet ONLY when the original bullet already supports it (same activity, same
   technology). A technology may only appear in a bullet if that job/project already names it.
5. Allowed: rephrasing, a stronger action verb at the start, the JD's wording for the same fact, reordering
   bullets (most relevant first), omitting bullets that are irrelevant to this JD.

Within those rules, DO improve the resume: rewrite every bullet relevant to the JD where an honest improvement
exists (lead with a strong action verb, surface the JD-relevant technology or activity the bullet already
describes, use the JD's terminology for the same thing, add a [X%] / [N] placeholder where a metric would help).
Leave a bullet unchanged only when no truthful improvement is possible.

What to return:
- summary: 2-4 lines for this JD built only from resume facts (no years of experience the resume does not show).
- skills: the resume's own skills, reordered so JD-relevant ones come first. Do not add any skill.
- experience: EVERY job, in the original order, each with its most relevant bullets first (2-6 bullets).
- projects: every project, most relevant first, with relevance_score 0-1 for this JD.
- section_order: put projects before experience only for candidates with under ~1 year of experience.
- For each bullet give a short reason (null if unchanged) and the JD requirement it now targets."""


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


def project_source_bullets(project: Project) -> list[str]:
    """A project's tailorable lines: its description (if any) followed by its outcome bullets."""
    return [line for line in [project.description or "", *project.outcomes] if line and line.strip()]


def _numbered_resume(data: ResumeData) -> dict:
    return {
        "summary": data.basics.summary,
        "total_experience_months": data.meta.total_experience_months,
        "experience": [
            {
                "source_index": i,
                "company": job.company,
                "title": job.title,
                "location": job.location,
                "start": job.start,
                "end": job.end,
                "skills_used": job.skills_used,
                "bullets": job.bullets,
            }
            for i, job in enumerate(data.experience)
        ],
        "projects": [
            {"source_index": i, "name": p.name, "tech_stack": p.tech_stack, "bullets": project_source_bullets(p)}
            for i, p in enumerate(data.projects)
        ],
        "skills": data.skills.model_dump(exclude={"languages"}),
        "education": [e.model_dump() for e in data.education],
        "certifications": [c.name for c in data.certifications],
    }


def build_prompt(data: ResumeData, jd: JobDescription, result: MatchResult, suggestions: Suggestions | None) -> str:
    analysis = {
        "score": result.score,
        "missing_required_skills": result.missing_required_skills,
        "missing_preferred_skills": result.missing_preferred_skills,
        "missing_keywords": [k["term"] for k in result.breakdown["keywords"].data.get("keywords", []) if not k["present"]],
    }
    if suggestions is not None:
        analysis["keywords_the_candidate_can_add"] = suggestions.missing_keywords_to_add
        analysis["skills_to_learn_do_not_claim"] = [s.skill for s in suggestions.skills_to_learn]
        analysis["top_priorities"] = [{"issue": p.issue, "action": p.action} for p in suggestions.top_priorities]
    return (
        "Tailor this resume to the job description.\n\n"
        f"<resume>\n{json.dumps(_numbered_resume(data), ensure_ascii=False)}\n</resume>\n\n"
        f"<job_description>\n{jd.model_dump_json()}\n</job_description>\n\n"
        f"<match_analysis>\n{json.dumps(analysis, ensure_ascii=False)}\n</match_analysis>"
    )


# ---------------------------------------------------------------------------
# Validation: the truthfulness rules, enforced in code
# ---------------------------------------------------------------------------


def _same(a: str | None, b: str | None) -> bool:
    if not a and not b:
        return True
    if not a or not b:
        return False
    return fuzz.ratio(a.casefold().strip(), b.casefold().strip()) >= FUZZY_SAME


def entry_context(raw_text: str, *anchors: str | None) -> str:
    """The lines of the original text around an entry (found by company / institution / name)."""
    lines = raw_text.splitlines()
    keys = [a.casefold() for a in anchors if a and len(a) >= 3]
    hits = [i for i, line in enumerate(lines) if any(k in line.casefold() for k in keys)]
    if not hits:
        return raw_text
    return "\n".join(line for i in hits for line in lines[max(0, i - 1) : i + 4])


def display_date(value: str | None, raw_text: str) -> str | None:
    """'2017-01' -> '2017' when the resume only gave a year (the parser stores year-only dates as January).

    Pass the entry's own lines (``entry_context``) as ``raw_text`` so another entry's "Jan 2017" does not count.
    """
    if not value or not raw_text or not re.fullmatch(r"\d{4}-01", value):
        return value
    year = value[:4]
    january = rf"\bjan(?:uary)?\.?,?\s*'?(?:{year}|{year[2:]})\b|\b0?1\s*[/.-]\s*{year}\b|\b{year}\s*[/.-]\s*0?1\b"
    return value if re.search(january, raw_text, re.IGNORECASE) else year


def _same_date(draft: str | None, original: str | None) -> bool:
    return draft == original or normalize_date(draft) == original or _same(draft, original)


@dataclass
class _Context:
    data: ResumeData
    known_numbers: set[str]
    skill_names: list[str]
    warnings: list[str]


def _check_bullet_text(text: str, item_text: str, ctx: _Context, where: str) -> str:
    text, changed = strip_invented_numbers(text, ctx.known_numbers)
    if changed:
        ctx.warnings.append(f"invented_number_replaced: {where}")
    text, flagged = flag_unsupported_skills(text, item_text, ctx.skill_names)
    ctx.warnings.extend(f"unsupported_skill_flagged: '{s}' in {where}" for s in flagged)
    return text


def _norm(text: str) -> str:
    return " ".join(text.split()).strip(" .").casefold()


def _validate_bullets(
    drafts: list[DraftBullet], originals: list[str], item_text: str, ctx: _Context, where: str
) -> list[TailoredBullet]:
    """Map each draft bullet to one original bullet of the same item; keep the rest as hidden originals."""
    used: set[int] = set()
    out: list[TailoredBullet] = []
    for draft in drafts:
        best = None
        for candidate in (draft.original_text, draft.text):
            scores = [(fuzz.ratio(_norm(candidate), _norm(o)), i) for i, o in enumerate(originals) if i not in used]
            score, index = max(scores, default=(0, -1))
            if score >= FUZZY_SAME:
                best = index
                break
        if best is None:
            ctx.warnings.append(f"invented_bullet_removed: {where}: {draft.text[:80]!r}")
            continue
        used.add(best)
        original = originals[best]
        text = _check_bullet_text(draft.text.strip(), item_text, ctx, f"{where} bullet {len(out) + 1}")
        changed = _norm(text) != _norm(original)
        out.append(
            TailoredBullet(
                text=text,
                ai_text=text,
                original_text=original,
                changed=changed,
                reason=(draft.reason or "Rephrased for this job.") if changed else None,
                jd_requirement=draft.jd_requirement if changed else None,
                needs_user_input=bool(PLACEHOLDER.search(text)),
            )
        )
    for i, original in enumerate(originals):
        if i not in used:  # the model left it out: keep it, hidden, so the user can bring it back
            out.append(TailoredBullet(text=original, ai_text=original, original_text=original, hidden=True,
                                      reason="Less relevant to this job; hidden (switch it back on if you like)."))
    return out


def _validate_skills(draft: TailorDraft, data: ResumeData, ctx: _Context) -> TailoredSkills:
    """Only skills already in the resume, re-ordered as Gemini suggested; nothing from the original lost."""
    pool: dict[str, str] = {}
    for name in [*data.skills.technical, *data.skills.tools, *data.skills.soft]:
        pool.setdefault(alias_key(name), name)
    for name in [s for job in data.experience for s in job.skills_used] + [s for p in data.projects for s in p.tech_stack]:
        pool.setdefault(alias_key(name), name)

    def resolve(name: str) -> str | None:
        key = alias_key(name)
        if key in pool:
            return pool[key]
        best = max(((fuzz.ratio(key, k), k) for k in pool), default=(0, ""))
        return pool[best[1]] if best[0] >= FUZZY_SAME else None

    seen: set[str] = set()
    result = TailoredSkills(languages=list(data.skills.languages))
    for category in ("technical", "tools", "soft"):
        for name in getattr(draft.skills, category):
            canonical = resolve(name)
            if canonical is None:
                ctx.warnings.append(f"invented_skill_removed: {name!r}")
                continue
            if canonical.casefold() not in seen:
                seen.add(canonical.casefold())
                getattr(result, category).append(canonical)
    for category in ("technical", "tools", "soft"):  # re-add anything Gemini dropped, at the end
        for name in getattr(data.skills, category):
            if name.casefold() not in seen:
                seen.add(name.casefold())
                getattr(result, category).append(name)
    return result


def _section_order(draft_order: list[str], data: ResumeData) -> list[str]:
    default = FRESHER_ORDER if (data.meta.total_experience_months or 0) < 12 else DEFAULT_ORDER
    order = [s for s in dict.fromkeys(s.strip().lower() for s in draft_order) if s in SECTIONS]
    return order + [s for s in default if s not in order]


def validate_draft(
    draft: TailorDraft,
    data: ResumeData,
    jd: JobDescription,
    experience_relevance: dict[str, float] | None = None,
    raw_text: str = "",
) -> tuple[TailoredResume, list[str]]:
    """Turn Gemini's draft into a ``TailoredResume`` that only contains facts from ``data``.

    ``raw_text`` (the original PDF text) lets year-only dates stay year-only on export.
    """
    source = resume_text(data)
    jd_skills = [p for s in [*jd.required_skills, *jd.preferred_skills, *jd.tools] for p in split_alternatives(s)]
    ctx = _Context(
        data=data,
        known_numbers=numbers_in(source),
        skill_names=[n for n in dict.fromkeys([*data.skills.technical, *data.skills.tools, *jd_skills]) if len(n) >= 2],
        warnings=[],
    )
    relevance = experience_relevance or {}

    # Experience: every original job, in the original order; facts always from the original.
    drafts_by_index: dict[int, DraftExperience] = {}
    for d in draft.experience:
        if 0 <= d.source_index < len(data.experience) and d.source_index not in drafts_by_index:
            drafts_by_index[d.source_index] = d
        else:
            ctx.warnings.append(f"invented_experience_removed: {d.title!r} at {d.company!r}")
    experience: list[TailoredExperience] = []
    for i, job in enumerate(data.experience):
        d = drafts_by_index.get(i)
        where = f"experience[{i}] ({job.title} at {job.company})"
        if d is None:
            ctx.warnings.append(f"experience_restored: {where} was missing from the draft")
        else:
            for field, ok in (
                ("company", _same(d.company, job.company)),
                ("title", _same(d.title, job.title)),
                ("location", not d.location or _same(d.location, job.location)),
                ("start", _same_date(d.start, job.start)),
                ("end", _same_date(d.end, job.end)),
            ):
                if not ok:
                    ctx.warnings.append(
                        f"reverted_{field}: {where}: {getattr(d, field)!r} -> {getattr(job, field)!r}"
                    )
        item_text = "\n".join([*job.bullets, *job.skills_used, job.title or ""])
        bullets = _validate_bullets(d.bullets if d else [], job.bullets, item_text, ctx, where)
        experience.append(
            TailoredExperience(
                source_index=i, company=job.company, title=job.title, location=job.location,
                start=display_date(job.start, ctx_text := entry_context(raw_text, job.company, job.title)),
                end=display_date(job.end, ctx_text),
                bullets=bullets, relevance_score=relevance.get(f"experience[{i}]"),
            )
        )

    # Projects: Gemini's order (most relevant first); left-out projects kept but hidden.
    projects: list[TailoredProject] = []
    seen_projects: set[int] = set()
    for d in draft.projects:
        if not 0 <= d.source_index < len(data.projects) or d.source_index in seen_projects:
            ctx.warnings.append(f"invented_project_removed: {d.name!r}")
            continue
        seen_projects.add(d.source_index)
        original = data.projects[d.source_index]
        where = f"projects[{d.source_index}] ({original.name})"
        if not _same(d.name, original.name):
            ctx.warnings.append(f"reverted_name: {where}: {d.name!r} -> {original.name!r}")
        stack = []
        for tech in d.tech_stack:
            match = next((t for t in original.tech_stack if _same(tech, t)), None)
            if match is None:
                ctx.warnings.append(f"invented_skill_removed: {tech!r} from {where} tech stack")
            elif match not in stack:
                stack.append(match)
        stack += [t for t in original.tech_stack if t not in stack]
        item_text = "\n".join([*project_source_bullets(original), *original.tech_stack, original.name or ""])
        projects.append(
            TailoredProject(
                source_index=d.source_index, name=original.name, tech_stack=stack, link=original.link,
                start=display_date(original.start, entry_context(raw_text, original.name)),
                end=display_date(original.end, entry_context(raw_text, original.name)),
                relevance_score=round(d.relevance_score, 2),
                bullets=_validate_bullets(d.bullets, project_source_bullets(original), item_text, ctx, where),
            )
        )
    for i, original in enumerate(data.projects):
        if i not in seen_projects:
            originals = project_source_bullets(original)
            projects.append(
                TailoredProject(
                    source_index=i, name=original.name, tech_stack=list(original.tech_stack), link=original.link,
                    start=display_date(original.start, entry_context(raw_text, original.name)),
                    end=display_date(original.end, entry_context(raw_text, original.name)),
                    relevance_score=0.0, hidden=True,
                    bullets=[TailoredBullet(text=b, ai_text=b, original_text=b) for b in originals],
                )
            )

    # Summary: same number / skill checks against the whole resume.
    summary = _check_bullet_text(draft.summary.strip(), source, ctx, "summary")
    doc = TailoredResume(
        basics=data.basics.model_copy(deep=True),
        summary=summary,
        ai_summary=summary,
        original_summary=data.basics.summary,
        summary_reason=draft.summary_reason or "Rewritten for this job.",
        skills=_validate_skills(draft, data, ctx),
        experience=experience,
        projects=projects,
        education=[
            e.model_copy(
                update={
                    "start": display_date(e.start, entry_context(raw_text, e.institution, e.degree)),
                    "end": display_date(e.end, entry_context(raw_text, e.institution, e.degree)),
                }
            )
            for e in data.education
        ],
        certifications=[
            c.model_copy(update={"date": display_date(c.date, entry_context(raw_text, c.name))}) for c in data.certifications
        ],
        achievements=list(data.achievements),
        section_order=_section_order(draft.section_order, data),
    )
    doc.changes_log = build_changes_log(doc, data)
    return doc, ctx.warnings


def build_changes_log(doc: TailoredResume, data: ResumeData) -> list[ChangeLogEntry]:
    log: list[ChangeLogEntry] = []
    if _norm(doc.ai_summary) != _norm(doc.original_summary or ""):
        log.append(ChangeLogEntry(section="summary", before=doc.original_summary, after=doc.ai_summary, reason=doc.summary_reason))
    for item, label in [(e, f"experience: {e.title} at {e.company}") for e in doc.experience] + [
        (p, f"projects: {p.name}") for p in doc.projects
    ]:
        if isinstance(item, TailoredProject) and item.hidden:
            log.append(ChangeLogEntry(section=label, before=item.name, after=None, reason="Project hidden: least relevant to this job."))
            continue
        for b in item.bullets:
            if b.changed:
                log.append(ChangeLogEntry(section=label, before=b.original_text, after=b.ai_text, reason=b.reason, linked_jd_requirement=b.jd_requirement))
            elif b.hidden:
                log.append(ChangeLogEntry(section=label, before=b.original_text, after=None, reason=b.reason))
    original_skills = [*data.skills.technical, *data.skills.tools, *data.skills.soft]
    tailored_skills = [*doc.skills.technical, *doc.skills.tools, *doc.skills.soft]
    if original_skills != tailored_skills:
        log.append(ChangeLogEntry(section="skills", before=", ".join(original_skills), after=", ".join(tailored_skills), reason="Most relevant skills first."))
    return log


def tailor(
    provider: LLMProvider,
    data: ResumeData,
    jd: JobDescription,
    result: MatchResult,
    suggestions: Suggestions | None = None,
    raw_text: str = "",
) -> tuple[TailoredResume, list[str]]:
    """Draft with Gemini (temperature 0.2, validated + retried once), then enforce the rules in code."""
    draft = complete_validated(
        provider, SYSTEM_PROMPT, build_prompt(data, jd, result, suggestions), TailorDraft, temperature=TAILOR_TEMPERATURE
    )
    relevance = {
        item["id"]: item["score"] for item in result.breakdown["experience_relevance"].data.get("items", [])
    }
    doc, warnings = validate_draft(draft, data, jd, relevance, raw_text)
    logger.info("resume tailored", extra={"changes": len(doc.changes_log), "warnings": len(warnings)})
    return doc, warnings


# ---------------------------------------------------------------------------
# The effective document (what the user will export after accept / reject / edit / hide)
# ---------------------------------------------------------------------------


def bullet_text(bullet: TailoredBullet) -> str:
    return bullet.original_text if bullet.decision == "rejected" else bullet.text


def summary_text(doc: TailoredResume) -> str:
    return (doc.original_summary or "") if doc.summary_decision == "rejected" else doc.summary


def visible_sections(doc: TailoredResume) -> list[str]:
    return [s for s in doc.section_order if s in SECTIONS and s not in doc.hidden_sections]


def _visible_text(doc: TailoredResume) -> Iterator[tuple[str, str]]:
    """(where, text) for every piece of free text the export will contain."""
    sections = visible_sections(doc)
    if "summary" in sections and summary_text(doc).strip():
        yield "summary", summary_text(doc)
    if "experience" in sections:
        for job in doc.experience:
            for i, b in enumerate(job.bullets):
                if not b.hidden:
                    yield f"{job.title} at {job.company}, bullet {i + 1}", bullet_text(b)
    if "projects" in sections:
        for project in doc.projects:
            if not project.hidden:
                for i, b in enumerate(project.bullets):
                    if not b.hidden:
                        yield f"project {project.name}, bullet {i + 1}", bullet_text(b)


def find_placeholders(doc: TailoredResume) -> list[dict[str, str]]:
    """Unresolved placeholders ([X%], [N users], [if true: Docker]) in the content that would be exported."""
    return [
        {"where": where, "placeholder": m.group(0), "text": text}
        for where, text in _visible_text(doc)
        for m in PLACEHOLDER.finditer(text)
    ]


def refresh_flags(doc: TailoredResume) -> TailoredResume:
    """Recompute ``needs_user_input`` after user edits."""
    for item in [*doc.experience, *doc.projects]:
        for b in item.bullets:
            b.needs_user_input = bool(PLACEHOLDER.search(bullet_text(b)))
    return doc


# ---------------------------------------------------------------------------
# Re-scoring with the existing match scorer
# ---------------------------------------------------------------------------


def _strip_placeholders(text: str) -> str:
    """Unconfirmed content never earns points: '[if true: Docker]' and '[X%]' are removed before scoring."""
    return " ".join(PLACEHOLDER.sub("", text).split())


def as_resume_data(doc: TailoredResume, original: ResumeData, visible_only: bool = True, pages: int | None = None) -> ResumeData:
    """The tailored document as a ``ResumeData`` the scorer understands."""
    data = original.model_copy(deep=True)
    sections = visible_sections(doc) if visible_only else list(SECTIONS)
    data.basics.summary = _strip_placeholders(summary_text(doc)) if "summary" in sections else None

    jobs = sorted(doc.experience, key=lambda j: j.source_index)
    for job in data.experience:
        job.bullets = []
    if "experience" in sections:
        for tailored in jobs:
            data.experience[tailored.source_index].bullets = [
                _strip_placeholders(bullet_text(b)) for b in tailored.bullets if not (visible_only and b.hidden)
            ]

    projects: list[Project] = []
    if "projects" in sections:
        for tailored in sorted(doc.projects, key=lambda p: p.source_index):
            if visible_only and tailored.hidden:
                continue
            base = original.projects[tailored.source_index].model_copy(deep=True)
            base.description = None
            base.outcomes = [_strip_placeholders(bullet_text(b)) for b in tailored.bullets if not (visible_only and b.hidden)]
            base.tech_stack = list(tailored.tech_stack)
            projects.append(base)
    data.projects = projects
    data.skills.technical, data.skills.tools, data.skills.soft = (
        list(doc.skills.technical), list(doc.skills.tools), list(doc.skills.soft)
    )
    if pages:
        data.meta.pages = pages
    return data


def rate_tailored(provider: LLMProvider, doc: TailoredResume, original: ResumeData, jd: JobDescription) -> dict[str, dict]:
    """Gemini relevance ratings for the tailored jobs / projects, keyed by ORIGINAL index (all items)."""
    full = as_resume_data(doc, original, visible_only=False)  # every job / project, in source order
    return rate_relevance(provider, full, jd)


def score_tailored(
    doc: TailoredResume,
    original: ResumeData,
    jd: JobDescription,
    ratings: dict[str, dict],
    alias_file: Path,
    threshold: float = 85,
    pages: int | None = None,
) -> MatchResult:
    """Deterministic: the existing match scorer on the visible tailored content."""
    data = as_resume_data(doc, original, visible_only=True, pages=pages)
    # Ratings are keyed by original index; the visible projects are renumbered.
    visible = [p.source_index for p in sorted(doc.projects, key=lambda p: p.source_index) if not p.hidden]
    remapped = {k: v for k, v in ratings.items() if k.startswith("experience[")}
    if "projects" in visible_sections(doc):
        remapped |= {f"projects[{j}]": ratings[f"projects[{src}]"] for j, src in enumerate(visible) if f"projects[{src}]" in ratings}
    return compute_match(data, resume_text(data), jd, remapped, alias_file, threshold)


# ---------------------------------------------------------------------------
# Auto-fit: hide the least relevant content, one piece at a time
# ---------------------------------------------------------------------------


def trim_least_relevant(doc: TailoredResume) -> str | None:
    """Hide one low-relevance project or bullet. Returns a description, or None when nothing is left to trim.

    Order: projects rated below 0.3, then the weakest bullet (low item relevance, unchanged, late in the
    list) of any job / project that still has more than one visible bullet, then remaining projects.
    """
    visible_projects = [p for p in doc.projects if not p.hidden]
    weak = sorted((p for p in visible_projects if p.relevance_score < 0.3), key=lambda p: p.relevance_score)
    if weak:
        weak[0].hidden = True
        return f"hid project '{weak[0].name}' (relevance {weak[0].relevance_score:.2f})"

    candidates = []
    for item in [*doc.experience, *visible_projects]:
        shown = [b for b in item.bullets if not b.hidden]
        if len(shown) <= 1:
            continue
        relevance = item.relevance_score if item.relevance_score is not None else 0.5
        for position, bullet in enumerate(shown):
            value = relevance + (0.2 if bullet.changed else 0.0) - 0.01 * position
            candidates.append((value, id(bullet), bullet, item))
    if candidates:
        _, _, bullet, item = min(candidates, key=lambda c: c[:2])
        bullet.hidden = True
        name = getattr(item, "name", None) or f"{getattr(item, 'title', '')} at {getattr(item, 'company', '')}"
        return f"hid a bullet of '{name}': {bullet_text(bullet)[:60]!r}"

    if visible_projects:
        least = min(visible_projects, key=lambda p: p.relevance_score)
        least.hidden = True
        return f"hid project '{least.name}'"
    return None


def target_pages(data: ResumeData) -> int:
    """1 page under 3 years of experience, otherwise 2."""
    return 1 if (data.meta.total_experience_months or 0) < 36 else 2
