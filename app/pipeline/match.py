"""Resume <-> job description scoring engine.

The score is computed in Python so it is explainable and reproducible. Gemini is used for exactly
one input, the experience-relevance ratings, and those are rounded to 0.05 and stored with the
match, so re-scoring the same inputs always gives the same number.

| Component            | Max | How                                                                  |
|----------------------|-----|----------------------------------------------------------------------|
| required_skills      | 35  | share of JD required skills found (exact / alias / fuzzy / in text)  |
| preferred_skills     | 10  | same, on JD preferred skills + tools                                 |
| experience_relevance | 20  | weighted average of per-job (x2) / per-project (x1) Gemini ratings   |
| years_experience     | 10  | linear up to the JD minimum; never penalised above the maximum       |
| education            | 10  | degree level (6) + field of study (4)                                |
| keywords             | 10  | weighted coverage of domain keywords (x2) + top TF-IDF terms (x1)    |
| resume_quality       |  5  | quantified bullets 2, action verbs 1, contact/links 1, length 1      |
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from rapidfuzz import fuzz, process

from app.match_schema import ComponentScore, JobDescription, MatchResult, RelevanceRatings, SkillMatch
from app.pipeline.jd_parse import split_alternatives
from app.pipeline.llm import LLMProvider
from app.pipeline.llm_extract import complete_validated
from app.pipeline.normalize import alias_key, load_alias_maps, normalize_skill
from app.schema import ResumeData

logger = logging.getLogger(__name__)

SCORING_VERSION = "1.0"

WEIGHTS: dict[str, int] = {
    "required_skills": 35,
    "preferred_skills": 10,
    "experience_relevance": 20,
    "years_experience": 10,
    "education": 10,
    "keywords": 10,
    "resume_quality": 5,
}

LABELS = ((85, "Excellent match"), (70, "Strong"), (50, "Moderate"), (0, "Low"))

EXPERIENCE_WEIGHT = 2.0  # a job counts twice as much as a project in the relevance average
PROJECT_WEIGHT = 1.0
TOP_RESPONSIBILITY_TERMS = 15

# Two-letter tokens that are also common English words: never matched as free text.
_SHORT_WORDS = {"go", "is", "it", "in", "on", "as", "at", "be", "do", "me", "we", "us", "an", "or", "to", "of", "no", "so", "up", "by", "my", "if"}

_STOPWORDS = set(
    """a an and are as at be been being but by can could did do does for from had has have how i if in into is it its
    may might more most must not of on or our over per should so such than that the their them then there these they
    this those through to under up upon was we were what when where which while who whom why will with within without
    would you your yours across all also any both each etc using use used via other others new well able ability
    including include includes based work working end day""".split()
)

# Verbs that open most JD responsibilities and carry no matching signal.
_GENERIC_VERBS = set(
    """own owns design designs build builds maintain maintains write writes help helps ensure ensures support supports
    take takes make makes run runs define defines manage manages partner partners translate translates
    improve improves drive drives lead leads deliver delivers develop develops create creates
    efficient engineers features feature quality high end ship part clear strong various multiple key best closely
    them work""".split()
)

ACTION_VERBS = set(
    """achieved accelerated analysed analyzed architected automated boosted built championed collaborated conceived
    configured consolidated coordinated created cut debugged decreased defined delivered deployed designed developed
    devised directed drove eliminated enabled engineered enhanced established evaluated executed expanded facilitated
    generated grew guided identified implemented improved increased initiated integrated introduced launched led
    managed mentored migrated modernized modernised negotiated optimized optimised orchestrated organized organised
    owned pioneered planned prioritized prioritised produced programmed prototyped published ranked raised
    redesigned reduced refactored resolved revamped saved scaled secured shipped simplified spearheaded standardized
    streamlined strengthened tested trained transformed tripled doubled upgraded won wrote""".split()
)

# Degree level ladder used for both the resume and the JD requirement.
_DEGREE_PATTERNS: list[tuple[int, str]] = [
    (5, r"\b(ph\s*d|doctorate|doctoral|d\s*phil)\b"),
    (4, r"\b(master|masters|mba|m\s*tech|m\s*sc|m\s*s|m\s*e|m\s*eng|mca|m\s*com|m\s*a|post\s*graduate|pg)\b"),
    (3, r"\b(bachelor|bachelors|b\s*tech|b\s*e|b\s*eng|b\s*sc|b\s*s|bca|bba|b\s*com|b\s*a|undergraduate|graduate|degree)\b"),
    (2, r"\b(diploma|associate)\b"),
    (1, r"\b(higher secondary|12th|high school|hsc|secondary|10th|ssc)\b"),
]
DEGREE_LEVEL_NAMES = {5: "Doctorate", 4: "Master's", 3: "Bachelor's", 2: "Diploma / Associate", 1: "School"}

_FIELD_ALIASES = {
    "cs": "computer science",
    "cse": "computer science",
    "it": "information technology",
    "ece": "electronics and communication",
    "ee": "electrical engineering",
}


# ---------------------------------------------------------------------------
# Resume view: everything the scorer needs, pre-computed once
# ---------------------------------------------------------------------------


@dataclass
class ResumeView:
    """Searchable view of a parsed resume."""

    data: ResumeData
    raw_text: str
    skill_pool: dict[str, str] = field(default_factory=dict)  # alias_key -> display name
    text_key: str = ""  # alias_key of all resume text
    bullets: list[str] = field(default_factory=list)


def build_resume_view(data: ResumeData, raw_text: str = "") -> ResumeView:
    view = ResumeView(data=data, raw_text=raw_text or "")
    names: list[str] = []
    names += data.skills.technical + data.skills.tools + data.skills.soft
    for job in data.experience:
        names += job.skills_used
    for project in data.projects:
        names += project.tech_stack
    names += [c.name for c in data.certifications if c.name]
    for name in names:
        if name and name.strip():
            view.skill_pool.setdefault(alias_key(name), name.strip())

    view.bullets = [b for job in data.experience for b in job.bullets] + [o for p in data.projects for o in p.outcomes]
    view.text_key = " " + alias_key(resume_text(data, raw_text)) + " "
    return view


def resume_text(data: ResumeData, raw_text: str = "") -> str:
    """All resume content as one string (raw PDF text plus the structured fields)."""
    parts: list[str] = [raw_text, data.basics.summary or ""]
    for job in data.experience:
        parts += [job.title or "", job.company or "", *job.bullets, *job.skills_used]
    for project in data.projects:
        parts += [project.name or "", project.description or "", *project.outcomes, *project.tech_stack]
    for edu in data.education:
        parts += [edu.degree or "", edu.field or "", edu.institution or ""]
    parts += [c.name or "" for c in data.certifications] + data.achievements
    parts += data.skills.technical + data.skills.tools + data.skills.soft
    return "\n".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Skill matching
# ---------------------------------------------------------------------------


@lru_cache(maxsize=8)
def _reverse_aliases(alias_file: str) -> dict[str, tuple[str, ...]]:
    """canonical (lowercase) -> every alias key that maps to it, e.g. 'javascript' -> ('js', 'es6', ...)."""
    skill_map, _ = load_alias_maps(alias_file)
    reverse: dict[str, set[str]] = {}
    for key, canonical in skill_map.items():
        reverse.setdefault(canonical.lower(), set()).add(key)
        reverse[canonical.lower()].add(alias_key(canonical))
    return {k: tuple(sorted(v)) for k, v in reverse.items()}


def _in_text(variant: str, text_key: str) -> bool:
    """Whole-word search of an alias-key variant in the resume text key."""
    if len(variant) < 2 or variant in _SHORT_WORDS or (len(variant) == 2 and not variant.isalpha()):
        return False
    return re.search(rf"(?<![a-z0-9+#]){re.escape(variant)}(?![a-z0-9+#])", text_key) is not None


def _match_one(skill: str, view: ResumeView, alias_file: Path, threshold: float) -> SkillMatch:
    skill_map, _ = load_alias_maps(str(alias_file))
    key = alias_key(skill)
    canonical = normalize_skill(skill, skill_map)
    variants = (key, alias_key(canonical), *_reverse_aliases(str(alias_file)).get(canonical.lower(), ()))

    if key in view.skill_pool:
        return SkillMatch(skill=skill, matched=True, how="exact", evidence=view.skill_pool[key])
    for variant in variants[1:]:
        if variant in view.skill_pool:
            return SkillMatch(skill=skill, matched=True, how="alias", evidence=view.skill_pool[variant])
    for variant in dict.fromkeys(variants):
        if _in_text(variant, view.text_key):
            return SkillMatch(skill=skill, matched=True, how="text", evidence=f"mentioned in resume text ('{variant}')")

    pool_keys = sorted(view.skill_pool)
    if len(key) >= 4 and pool_keys:
        best = process.extractOne(key, pool_keys, scorer=fuzz.ratio, score_cutoff=threshold)
        if best is not None:
            return SkillMatch(skill=skill, matched=True, how="fuzzy", evidence=f"{view.skill_pool[best[0]]} ({best[1]:.0f}%)")
        # A listed resume skill that is a whole word of a longer JD phrase ("CI/CD" in "CI/CD pipelines").
        for pool_key in pool_keys:
            if len(pool_key) >= 3 and pool_key not in _SHORT_WORDS and re.search(rf"(?<![a-z0-9]){re.escape(pool_key)}(?![a-z0-9])", key):
                return SkillMatch(skill=skill, matched=True, how="fuzzy", evidence=f"{view.skill_pool[pool_key]} (part of '{skill}')")
    if len(key) >= 5 and fuzz.partial_ratio(key, view.text_key) >= max(threshold, 90):
        return SkillMatch(skill=skill, matched=True, how="fuzzy", evidence="close spelling found in resume text")
    return SkillMatch(skill=skill, matched=False, how="none")


_HOW_RANK = {"exact": 0, "alias": 1, "text": 2, "fuzzy": 3, "none": 4}


def match_skill(skill: str, view: ResumeView, alias_file: Path, threshold: float = 85) -> SkillMatch:
    """Match one JD skill; 'A / B' alternatives match if either does (the strongest match wins)."""
    results = [_match_one(alt, view, alias_file, threshold) for alt in split_alternatives(skill) or [skill]]
    best = min(results, key=lambda r: _HOW_RANK[r.how])
    return best.model_copy(update={"skill": skill})


def match_skills(skills: list[str], view: ResumeView, alias_file: Path, threshold: float = 85) -> list[SkillMatch]:
    return [match_skill(s, view, alias_file, threshold) for s in skills]


def _skill_component(matches: list[SkillMatch], weight: float, what: str) -> ComponentScore:
    if not matches:
        return ComponentScore(score=weight, max=weight, details=f"The JD lists no {what}; full marks.", data={"matches": []})
    found = sum(m.matched for m in matches)
    return ComponentScore(
        score=_r(weight * found / len(matches)),
        max=weight,
        details=f"{found} of {len(matches)} {what} found in the resume.",
        data={"matches": [m.model_dump() for m in matches]},
    )


# ---------------------------------------------------------------------------
# Experience relevance (Gemini ratings -> deterministic weighted average)
# ---------------------------------------------------------------------------

RELEVANCE_SYSTEM = """You are a strict technical recruiter. Rate how relevant each resume item (job or project)
is to the target job's responsibilities and required skills.

Scale: 1.0 = does the same kind of work with the same skills; 0.7 = clearly related, transferable;
0.4 = partly related; 0.1 = little overlap; 0.0 = unrelated.
Judge ONLY from the item text given. Return one rating per item id, with a one-line reason (max 20 words)."""


def relevance_items(data: ResumeData) -> list[tuple[str, float, str]]:
    """(id, weight, text) for each job and project, in resume order."""
    items: list[tuple[str, float, str]] = []
    for i, job in enumerate(data.experience):
        head = " at ".join(p for p in (job.title, job.company) if p) or "(untitled job)"
        body = "; ".join(job.bullets + ([f"Skills: {', '.join(job.skills_used)}"] if job.skills_used else []))
        items.append((f"experience[{i}]", EXPERIENCE_WEIGHT, f"{head}. {body}"))
    for i, project in enumerate(data.projects):
        head = project.name or "(untitled project)"
        body = "; ".join(
            [project.description or ""] + project.outcomes + ([f"Tech: {', '.join(project.tech_stack)}"] if project.tech_stack else [])
        )
        items.append((f"projects[{i}]", PROJECT_WEIGHT, f"{head}. {body}"))
    return items


def rate_relevance(provider: LLMProvider, data: ResumeData, jd: JobDescription) -> dict[str, dict]:
    """Ask Gemini to rate each job/project. Returns {id: {"score": 0..1 (rounded to .05), "reason": str}}."""
    items = relevance_items(data)
    if not items:
        return {}
    job = {
        "title": jd.title,
        "seniority": jd.seniority,
        "responsibilities": jd.responsibilities,
        "required_skills": jd.required_skills,
    }
    lines = "\n".join(f"- id={item_id}: {text}" for item_id, _, text in items)
    prompt = f"<target_job>\n{json.dumps(job, ensure_ascii=False)}\n</target_job>\n\n<resume_items>\n{lines}\n</resume_items>"
    answer = complete_validated(provider, RELEVANCE_SYSTEM, prompt, RelevanceRatings)
    known = {item_id for item_id, _, _ in items}
    return {
        r.id: {"score": round(round(r.score * 20) / 20, 2), "reason": r.reason.strip()}
        for r in answer.ratings
        if r.id in known
    }


def score_experience_relevance(data: ResumeData, ratings: dict[str, dict], weight: float = 20) -> ComponentScore:
    items = relevance_items(data)
    if not items:
        return ComponentScore(score=0, max=weight, details="No experience or projects to compare with the JD.", data={"items": []})
    rows = []
    for item_id, item_weight, text in items:
        rating = ratings.get(item_id) or {"score": 0.0, "reason": "Not rated."}
        rows.append({"id": item_id, "label": text.split(". ", 1)[0], "weight": item_weight, **rating})
    average = sum(r["score"] * r["weight"] for r in rows) / sum(r["weight"] for r in rows)
    best = max(rows, key=lambda r: r["score"])
    return ComponentScore(
        score=_r(weight * average),
        max=weight,
        details=f"Weighted relevance {average:.0%} (jobs count x2). Most relevant: {best['label']}.",
        data={"items": rows, "weighted_average": round(average, 4)},
    )


# ---------------------------------------------------------------------------
# Years of experience
# ---------------------------------------------------------------------------


def score_years(total_months: int | None, min_years: float | None, max_years: float | None, weight: float = 10) -> ComponentScore:
    """Full marks at or above the minimum, linear below it; being above the maximum is never penalised."""
    have = (total_months or 0) / 12
    data = {"candidate_years": round(have, 1), "min_years": min_years, "max_years": max_years}
    if not min_years:
        return ComponentScore(score=weight, max=weight, details=f"No minimum stated; candidate has {have:.1f} yrs.", data=data)
    if total_months is None:
        return ComponentScore(score=0, max=weight, details=f"JD asks for {min_years:g}+ yrs; no dated experience found.", data=data)
    ratio = min(have / min_years, 1.0)
    note = f"{have:.1f} yrs vs {min_years:g}+ required"
    if max_years and have > max_years:
        note += f" (above the {max_years:g}-yr range; not penalised)"
    return ComponentScore(score=_r(weight * ratio), max=weight, details=note + ".", data=data)


# ---------------------------------------------------------------------------
# Education
# ---------------------------------------------------------------------------


def degree_level(text: str | None) -> int | None:
    """'B.Tech' / "Bachelor's - Engineering" -> 3, 'MBA' -> 4, 'PhD' -> 5; None if unknown."""
    if not text:
        return None
    key = alias_key(text).replace("'", "")
    for level, pattern in _DEGREE_PATTERNS:
        if re.search(pattern, key):
            return level
    return None


_RELATED = re.compile(r"\b(?:or|and)?\s*(?:a|any|other)?\s*related\s*(?:technical\s+)?(?:fields?|disciplines?|areas?|subjects?)?\b", re.IGNORECASE)


def _field_key(text: str) -> str:
    key = alias_key(text)
    return _FIELD_ALIASES.get(key, key)


def score_education(data: ResumeData, jd: JobDescription, threshold: float = 85, weight: float = 10) -> ComponentScore:
    level_max, field_max = weight * 0.6, weight * 0.4
    req = jd.education_requirements
    levels = [lvl for e in data.education if (lvl := degree_level(e.degree_normalized) or degree_level(e.degree))]
    have = max(levels) if levels else None
    need = degree_level(req.min_degree)

    if need is None:
        level_score, level_note = level_max, "no degree level required"
    elif have is None:
        level_score, level_note = 0.0, f"{DEGREE_LEVEL_NAMES[need]} required; no degree found"
    elif have >= need:
        level_score, level_note = level_max, f"{DEGREE_LEVEL_NAMES[have]} meets {DEGREE_LEVEL_NAMES[need]}"
    elif have == need - 1:
        level_score, level_note = level_max / 2, f"{DEGREE_LEVEL_NAMES[have]} is one level below {DEGREE_LEVEL_NAMES[need]}"
    else:
        level_score, level_note = 0.0, f"{DEGREE_LEVEL_NAMES[have]} is below {DEGREE_LEVEL_NAMES[need]}"

    # "Computer Science or a related field" -> want "Computer Science", and related fields earn half marks.
    accepts_related = any("related" in f.lower() for f in req.fields)
    wanted = [w for f in req.fields if (w := _RELATED.sub("", f).strip(" ,;/-"))]
    candidate_fields = [_field_key(t) for e in data.education for t in (e.field, e.degree_normalized, e.degree) if t]
    matched_field = None
    for want in wanted:
        for have_field in candidate_fields:
            if fuzz.token_set_ratio(_field_key(want), have_field) >= threshold:
                matched_field = want
                break
        if matched_field:
            break
    if not wanted:
        field_score, field_note = field_max, "no field required"
    elif matched_field:
        field_score, field_note = field_max, f"field matches {matched_field}"
    elif accepts_related:
        field_score, field_note = field_max / 2, "field not listed, JD accepts related fields"
    else:
        field_score, field_note = 0.0, f"field not in {', '.join(wanted)}"

    return ComponentScore(
        score=_r(level_score + field_score),
        max=weight,
        details=f"{level_note[0].upper()}{level_note[1:]}; {field_note}.",
        data={"candidate_level": have, "required_level": need, "level_score": level_score, "field_score": field_score},
    )


# ---------------------------------------------------------------------------
# Keyword / ATS coverage
# ---------------------------------------------------------------------------


def _stem(word: str) -> str:
    """Tiny suffix stripper so 'tests'/'testing'/'tested' and 'optimise'/'optimising' compare equal."""
    for suffix in ("ations", "ation", "ments", "ment", "ings", "ing", "ies", "ers", "ed", "es", "er", "s", "e"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9][a-z0-9+#/]*", text.lower())


def _stemmed_text(text: str) -> str:
    return " " + " ".join(_stem(t) for t in _tokens(text)) + " "


def responsibility_terms(responsibilities: list[str], top_k: int = TOP_RESPONSIBILITY_TERMS) -> list[str]:
    """Top terms of the responsibilities by TF-IDF (each responsibility = one document).

    Unigrams of 3+ letters, plus bigrams that occur at least twice. Ties keep JD order.
    """
    docs: list[list[str]] = []
    for sentence in responsibilities:
        words = [w for w in _tokens(sentence) if w not in _STOPWORDS and w not in _GENERIC_VERBS and not w.isdigit()]
        grams = [w for w in words if len(w) >= 3]
        grams += [f"{a} {b}" for a, b in zip(words, words[1:])]
        docs.append(grams)
    if not docs:
        return []
    tf = Counter(g for doc in docs for g in doc)
    df = Counter(g for doc in docs for g in set(doc))
    n = len(docs)
    first_seen = {g: i for i, g in reversed(list(enumerate(g for doc in docs for g in doc)))}
    scores = {
        g: tf[g] * (math.log((1 + n) / (1 + df[g])) + 1) * (1.5 if " " in g else 1.0)
        for g in tf
        if " " not in g or tf[g] > 1
    }
    ranked = sorted(scores, key=lambda g: (-scores[g], first_seen[g]))
    chosen: list[str] = []
    for gram in ranked:
        # Skip a unigram already inside a chosen bigram and vice versa, to avoid double counting.
        if any(gram in c.split() or c in gram.split() for c in chosen):
            continue
        chosen.append(gram)
        if len(chosen) == top_k:
            break
    return chosen


def _keyword_present(term: str, stemmed_resume: str, resume_key: str, threshold: float) -> bool:
    stemmed = _stemmed_text(term).strip()
    if stemmed and f" {stemmed} " in stemmed_resume:
        return True
    key = alias_key(term)
    return len(key) >= 6 and " " in key and fuzz.partial_ratio(key, resume_key) >= max(threshold, 90)


def score_keywords(view: ResumeView, jd: JobDescription, threshold: float = 85, weight: float = 10) -> ComponentScore:
    domain = list(dict.fromkeys(jd.domain_keywords))
    domain_lower = {alias_key(d) for d in domain}
    resp = [t for t in responsibility_terms(jd.responsibilities) if alias_key(t) not in domain_lower]
    terms = [(t, 2.0, "domain") for t in domain] + [(t, 1.0, "responsibility") for t in resp]
    if not terms:
        return ComponentScore(score=weight, max=weight, details="No keywords to check; full marks.", data={"keywords": []})

    stemmed_resume = _stemmed_text(resume_text(view.data, view.raw_text))
    rows = [
        {"term": t, "kind": kind, "weight": w, "present": _keyword_present(t, stemmed_resume, view.text_key, threshold)}
        for t, w, kind in terms
    ]
    total = sum(r["weight"] for r in rows)
    covered = sum(r["weight"] for r in rows if r["present"])
    found = sum(r["present"] for r in rows)
    return ComponentScore(
        score=_r(weight * covered / total),
        max=weight,
        details=f"{found} of {len(rows)} JD keywords appear in the resume (domain keywords count x2).",
        data={"keywords": rows, "coverage": round(covered / total, 4)},
    )


# ---------------------------------------------------------------------------
# Resume quality
# ---------------------------------------------------------------------------

_NUMBER = re.compile(r"\d")


def score_quality(view: ResumeView, weight: float = 5) -> ComponentScore:
    """Quantified bullets (2), action verbs (1), contact + links (1), sensible length (1); scaled to ``weight``."""
    data = view.data
    bullets = [b for b in view.bullets if b.strip()]
    quantified = sum(bool(_NUMBER.search(b)) for b in bullets)
    with_verb = sum(_first_word(b) in ACTION_VERBS for b in bullets)
    q_ratio = quantified / len(bullets) if bullets else 0.0
    v_ratio = with_verb / len(bullets) if bullets else 0.0
    quantified_pts = 2 * min(q_ratio / 0.5, 1.0)  # half the bullets with numbers = full marks
    verb_pts = min(v_ratio / 0.7, 1.0)

    links = data.basics.links
    contact_pts = 0.4 * bool(data.basics.email) + 0.3 * bool(data.basics.phone)
    contact_pts += 0.3 * bool(links.linkedin or links.github or links.portfolio)

    words = len(view.raw_text.split()) if view.raw_text else len(resume_text(data).split())
    pages = data.meta.pages or 1
    if 300 <= words <= 1000 and pages <= 2:
        length_pts, length_note = 1.0, f"{words} words"
    elif 150 <= words <= 1500 and pages <= 3:
        length_pts, length_note = 0.5, f"{words} words ({'short' if words < 300 else 'long'})"
    else:
        length_pts, length_note = 0.0, f"{words} words ({'too short' if words < 150 else 'too long'})"

    raw = quantified_pts + verb_pts + contact_pts + length_pts  # out of 5
    return ComponentScore(
        score=_r(raw * weight / 5),
        max=weight,
        details=f"{quantified}/{len(bullets)} bullets quantified, {with_verb}/{len(bullets)} start with an action verb, {length_note}.",
        data={
            "quantified_bullets": quantified,
            "action_verb_bullets": with_verb,
            "bullets": len(bullets),
            "has_email": bool(data.basics.email),
            "has_phone": bool(data.basics.phone),
            "has_profile_link": bool(links.linkedin or links.github or links.portfolio),
            "words": words,
            "pages": pages,
        },
    )


def _first_word(text: str) -> str:
    match = re.match(r"\W*([A-Za-z]+)", text)
    return match.group(1).lower() if match else ""


# ---------------------------------------------------------------------------
# Total
# ---------------------------------------------------------------------------


def _r(value: float) -> float:
    return round(value + 1e-9, 1)


def round_half_up(value: float) -> int:
    return int(math.floor(value + 0.5))


def label_for(score: int) -> str:
    return next(label for floor_, label in LABELS if score >= floor_)


def compute_match(
    data: ResumeData,
    raw_text: str,
    jd: JobDescription,
    ratings: dict[str, dict],
    alias_file: Path,
    fuzzy_threshold: float = 85,
    semantic_similarity: float | None = None,
) -> MatchResult:
    """Pure function: the same inputs always give the same ``MatchResult``."""
    view = build_resume_view(data, raw_text)
    required = match_skills(jd.required_skills, view, alias_file, fuzzy_threshold)
    preferred_list = list(dict.fromkeys(jd.preferred_skills + jd.tools))
    preferred = match_skills(preferred_list, view, alias_file, fuzzy_threshold)

    breakdown = {
        "required_skills": _skill_component(required, WEIGHTS["required_skills"], "required skills"),
        "preferred_skills": _skill_component(preferred, WEIGHTS["preferred_skills"], "preferred skills / tools"),
        "experience_relevance": score_experience_relevance(data, ratings, WEIGHTS["experience_relevance"]),
        "years_experience": score_years(
            data.meta.total_experience_months, jd.min_years_experience, jd.max_years_experience, WEIGHTS["years_experience"]
        ),
        "education": score_education(data, jd, fuzzy_threshold, WEIGHTS["education"]),
        "keywords": score_keywords(view, jd, fuzzy_threshold, WEIGHTS["keywords"]),
        "resume_quality": score_quality(view, WEIGHTS["resume_quality"]),
    }
    total = max(0, min(100, round_half_up(sum(c.score for c in breakdown.values()))))
    return MatchResult(
        score=total,
        label=label_for(total),
        breakdown=breakdown,
        matched_skills=list(dict.fromkeys(m.skill for m in required + preferred if m.matched)),
        missing_required_skills=[m.skill for m in required if not m.matched],
        missing_preferred_skills=[m.skill for m in preferred if not m.matched],
        semantic_similarity=semantic_similarity,
        scoring_version=SCORING_VERSION,
    )


# ---------------------------------------------------------------------------
# Optional semantic similarity (informational only; not part of the score)
# ---------------------------------------------------------------------------


def semantic_similarity(provider: LLMProvider, resume: str, jd_text: str, model: str) -> float | None:
    """Cosine similarity of Gemini embeddings, or None when unavailable (never fails the match)."""
    embed = getattr(provider, "embed", None)
    if embed is None:
        return None
    try:
        vectors = embed([resume[:8000], jd_text[:8000]], model=model)
    except Exception as exc:  # optional metric: log and move on, whatever went wrong
        logger.warning("semantic similarity unavailable", extra={"error": str(exc)})
        return None
    if len(vectors) != 2 or not vectors[0] or not vectors[1]:
        return None
    a, b = vectors
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return round(sum(x * y for x, y in zip(a, b)) / norm, 4) if norm else None
