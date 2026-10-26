"""Compare Jobs insights, computed in Python from the stored match results (no LLM calls here).

Input: one ``JobRow`` per finished job (score, breakdown, matched / missing skills, suggestions, parsed JD).
Output (``build_insights``): ranking, effort to reach 80+, recommendation tags, common gaps,
strengths and the skill heatmap. ``summary_facts`` is the only thing Gemini sees when it writes
the "Where to apply first and why" summary, and ``rules_summary`` is the deterministic fallback.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

EFFORT_TARGET = 80
# Effort to reach EFFORT_TARGET: points still needed + number of top-priority fixes it takes.
EFFORT_LOW_MAX_POINTS, EFFORT_LOW_MAX_STEPS = 10, 2
EFFORT_HIGH_MIN_POINTS, EFFORT_HIGH_MIN_STEPS = 21, 5

APPLY_NOW_MIN = 75
TAILOR_MIN = 60
APPLY_NOW, TAILOR_THEN_APPLY, STRETCH = "Apply now", "Tailor, then apply", "Stretch / skip"

COMMON_GAP_SHARE = 0.5  # missing in >= 50% of the jobs
HEATMAP_ROWS = 20
TOP_MISSING = 3


@dataclass
class JobRow:
    """One finished job of a comparison, flattened from its comparison_jobs + matches rows."""

    job_id: str
    position: int
    name: str  # user label, or "Title - Company"
    score: int
    label: str
    title: str | None = None
    company: str | None = None
    match_id: str | None = None
    deadline: str | None = None
    location: str | None = None
    salary: str | None = None
    url: str | None = None
    breakdown: dict[str, Any] = field(default_factory=dict)
    matched_skills: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)
    missing_preferred: list[str] = field(default_factory=list)
    priorities: list[dict[str, Any]] = field(default_factory=list)  # suggestions.top_priorities
    jd: dict[str, Any] = field(default_factory=dict)  # parsed JobDescription


# ---------------------------------------------------------------------------
# Per-job numbers
# ---------------------------------------------------------------------------


def short_label(label: str | None) -> str:
    """'Excellent match' -> 'Excellent' (the scorer's labels, shortened for the table)."""
    return (label or "").replace(" match", "").strip() or "Low"


def effort_to_target(score: int, priorities: list[dict[str, Any]], target: int = EFFORT_TARGET) -> dict[str, Any]:
    """Effort to reach ``target``: the suggestions (biggest gain first) whose gains add up to the gap.

    Returns ``points_needed`` (the gap), ``gain`` (sum of estimated_score_gain of the suggestions
    needed), ``steps`` (how many), ``reachable`` and ``level`` (Low / Medium / High).
    """
    needed = max(0, target - score)
    gains = sorted((max(0, int(p.get("estimated_score_gain") or 0)) for p in priorities), reverse=True)
    total = steps = 0
    for gain in gains:
        if total >= needed:
            break
        total += gain
        steps += 1
    reachable = total >= needed
    if needed == 0:
        level = "Low"
    elif not reachable or needed >= EFFORT_HIGH_MIN_POINTS or steps >= EFFORT_HIGH_MIN_STEPS:
        level = "High"
    elif needed <= EFFORT_LOW_MAX_POINTS and steps <= EFFORT_LOW_MAX_STEPS:
        level = "Low"
    else:
        level = "Medium"
    return {"points_needed": needed, "gain": total, "steps": steps, "reachable": reachable, "level": level}


def recommend(score: int, effort_level: str) -> str:
    """Fixed rules: >= 75 apply now; 60-74 unless the effort is High: tailor first; else stretch / skip."""
    if score >= APPLY_NOW_MIN:
        return APPLY_NOW
    if score >= TAILOR_MIN and effort_level != "High":
        return TAILOR_THEN_APPLY
    return STRETCH


def top_missing(row: JobRow, n: int = TOP_MISSING) -> list[str]:
    """Required gaps first, then preferred."""
    return list(dict.fromkeys(row.missing_required + row.missing_preferred))[:n]


def rank_jobs(rows: list[JobRow]) -> list[JobRow]:
    """Highest score first; ties: more required skills, then the earlier deadline, then the order added."""

    def key(row: JobRow) -> tuple:
        required = (row.breakdown.get("required_skills") or {}).get("score") or 0
        return (-row.score, -required, row.deadline or "9999-99-99", row.position)

    return sorted(rows, key=key)


# ---------------------------------------------------------------------------
# Across jobs
# ---------------------------------------------------------------------------


def _key(skill: str) -> str:
    return " ".join(skill.lower().split())


def _display_names(names: list[str]) -> dict[str, str]:
    """Most common spelling per normalised key."""
    by_key: dict[str, Counter] = {}
    for name in names:
        by_key.setdefault(_key(name), Counter())[name] += 1
    return {k: c.most_common(1)[0][0] for k, c in by_key.items()}


def common_gaps(rows: list[JobRow], share: float = COMMON_GAP_SHARE) -> list[dict[str, Any]]:
    """Skills missing in >= ``share`` of the jobs, most frequent first: the "learn these first" list."""
    if not rows:
        return []
    names: list[str] = []
    missing_in: dict[str, list[str]] = {}
    required_in: Counter = Counter()
    for row in rows:
        seen: set[str] = set()
        for skill, required in [(s, True) for s in row.missing_required] + [(s, False) for s in row.missing_preferred]:
            k = _key(skill)
            if k in seen:
                continue
            seen.add(k)
            names.append(skill)
            missing_in.setdefault(k, []).append(row.name)
            required_in[k] += required
    display = _display_names(names)
    out = [
        {
            "skill": display[k],
            "missing_in": len(jobs),
            "of": len(rows),
            "share": round(len(jobs) / len(rows), 2),
            "required_in": required_in[k],
            "jobs": jobs,
        }
        for k, jobs in missing_in.items()
        if len(jobs) / len(rows) >= share - 1e-9
    ]
    out.sort(key=lambda g: (-g["missing_in"], -g["required_in"], g["skill"].lower()))
    return out


def strengths(rows: list[JobRow]) -> list[dict[str, Any]]:
    """Resume skills matched in most (more than half) of the jobs, most frequent first."""
    if not rows:
        return []
    names: list[str] = []
    counts: Counter = Counter()
    for row in rows:
        keys = {_key(s) for s in row.matched_skills}
        counts.update(keys)
        names += row.matched_skills
    display = _display_names(names)
    out = [
        {"skill": display[k], "matched_in": n, "of": len(rows)}
        for k, n in counts.items()
        if n / len(rows) > 0.5 or (len(rows) == 1 and n == 1)
    ]
    out.sort(key=lambda s: (-s["matched_in"], s["skill"].lower()))
    return out


def _resume_has(rows: list[JobRow]) -> dict[str, bool]:
    """Skill key -> did the scorer find it in the resume (for any job that asked for it)."""
    has: dict[str, bool] = {}
    for row in rows:
        for component in ("required_skills", "preferred_skills"):
            for m in ((row.breakdown.get(component) or {}).get("data") or {}).get("matches", []):
                k = _key(m["skill"])
                has[k] = has.get(k, False) or bool(m.get("matched"))
    return has


def skill_heatmap(rows: list[JobRow], limit: int = HEATMAP_ROWS) -> dict[str, Any]:
    """Rows = the top ``limit`` skills across all JDs, columns = jobs (in ``rows`` order).

    Cell = "required" / "preferred" / "none"; each skill row also says whether the resume has it.
    Skills are ranked by how many JDs mention them, then how many require them.
    """
    names: list[str] = []
    levels: list[dict[str, str]] = []
    for row in rows:
        jd = row.jd or {}
        level: dict[str, str] = {}
        for skill in jd.get("required_skills") or []:
            level.setdefault(_key(skill), "required")
        for skill in (jd.get("preferred_skills") or []) + (jd.get("tools") or []):
            level.setdefault(_key(skill), "preferred")
        names += (jd.get("required_skills") or []) + (jd.get("preferred_skills") or []) + (jd.get("tools") or [])
        levels.append(level)
    display = _display_names(names)
    mentioned = Counter(k for level in levels for k in level)
    required = Counter(k for level in levels for k, v in level.items() if v == "required")
    has = _resume_has(rows)
    top = sorted(mentioned, key=lambda k: (-mentioned[k], -required[k], display[k].lower()))[:limit]
    return {
        "jobs": [{"job_id": r.job_id, "name": r.name} for r in rows],
        "skills": [
            {
                "skill": display[k],
                "have": has.get(k, False),
                "mentioned_in": mentioned[k],
                "cells": [level.get(k, "none") for level in levels],
            }
            for k in top
        ],
    }


# ---------------------------------------------------------------------------
# Everything together
# ---------------------------------------------------------------------------


def build_insights(rows: list[JobRow]) -> dict[str, Any]:
    """Ranking + per-job effort / recommendation + cross-job insights for the finished jobs."""
    ranked = rank_jobs(rows)
    ranking = []
    for rank, row in enumerate(ranked, start=1):
        effort = effort_to_target(row.score, row.priorities)
        ranking.append(
            {
                "rank": rank,
                "job_id": row.job_id,
                "match_id": row.match_id,
                "name": row.name,
                "title": row.title,
                "company": row.company,
                "score": row.score,
                "label": short_label(row.label),
                "recommendation": recommend(row.score, effort["level"]),
                "effort": effort,
                "deadline": row.deadline,
                "location": row.location or (row.jd or {}).get("location"),
                "salary": row.salary,
                "url": row.url,
                "top_missing": top_missing(row),
                "components": {
                    k: {"score": c.get("score"), "max": c.get("max")} for k, c in (row.breakdown or {}).items()
                },
            }
        )
    return {
        "ranking": ranking,
        "common_gaps": common_gaps(ranked),
        "strengths": strengths(ranked),
        "heatmap": skill_heatmap(ranked),
    }


def summary_facts(insights: dict[str, Any]) -> dict[str, Any]:
    """The computed numbers the AI summary may use - and nothing else."""
    return {
        "jobs_ranked": [
            {
                "rank": r["rank"],
                "job": r["name"],
                "score": r["score"],
                "label": r["label"],
                "recommendation": r["recommendation"],
                "effort_to_80": r["effort"]["level"],
                "points_to_80": r["effort"]["points_needed"],
                "deadline": r["deadline"],
                "top_missing_skills": r["top_missing"],
            }
            for r in insights["ranking"]
        ],
        "learn_these_first": [{"skill": g["skill"], "missing_in": g["missing_in"], "of": g["of"]} for g in insights["common_gaps"][:6]],
        "strengths": [{"skill": s["skill"], "matched_in": s["matched_in"], "of": s["of"]} for s in insights["strengths"][:6]],
    }


def facts_key(facts: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(facts, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def rules_summary(facts: dict[str, Any]) -> str:
    """Deterministic 3-5 line summary, used when Gemini is unavailable or its answer fails the checks."""
    jobs = facts["jobs_ranked"]
    if not jobs:
        return "No job has been scored yet."
    lines = []
    best = jobs[0]
    lines.append(f"Apply first to {best['job']}: best fit at {best['score']}/100 ({best['label']}), tagged \"{best['recommendation']}\".")
    for job in jobs[1:3]:
        reason = (
            f"{job['points_to_80']} points short of 80, {job['effort_to_80'].lower()} effort"
            if job["points_to_80"]
            else "already 80+"
        )
        lines.append(f"Next: {job['job']} at {job['score']}/100 ({reason}) - {job['recommendation']}.")
    gaps = facts["learn_these_first"][:3]
    if gaps:
        lines.append("Learn first: " + ", ".join(f"{g['skill']} (missing in {g['missing_in']} of {g['of']})" for g in gaps) + ".")
    dated = [j for j in jobs if j["deadline"] and j["recommendation"] != STRETCH]
    if dated:
        soonest = min(dated, key=lambda j: j["deadline"])
        lines.append(f"Watch the deadline: {soonest['job']} closes on {soonest['deadline']}.")
    if len(lines) < 3 and facts["strengths"]:
        lines.append("Your strengths across these jobs: " + ", ".join(s["skill"] for s in facts["strengths"][:4]) + ".")
    return "\n".join(lines[:5])


def ceil_share(n: int, share: float = COMMON_GAP_SHARE) -> int:
    """How many jobs a skill must be missing from to count as a common gap (for UI text)."""
    return max(1, math.ceil(n * share - 1e-9))
