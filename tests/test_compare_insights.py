"""Unit tests for the Compare Jobs insights: recommendation rules, effort, common gaps, strengths, ranking, heatmap."""

from __future__ import annotations

import pytest

from app.pipeline.compare_insights import (
    APPLY_NOW,
    STRETCH,
    TAILOR_THEN_APPLY,
    JobRow,
    build_insights,
    common_gaps,
    effort_to_target,
    rank_jobs,
    recommend,
    rules_summary,
    short_label,
    skill_heatmap,
    strengths,
    summary_facts,
)


def gains(*values: int) -> list[dict]:
    return [{"estimated_score_gain": v} for v in values]


def row(job_id: str, score: int, position: int = 0, **kw) -> JobRow:
    return JobRow(job_id=job_id, position=position, name=kw.pop("name", job_id), score=score, label="Strong", **kw)


# ---------------------------------------------------------------------------
# Recommendation rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "score, effort, expected",
    [
        (100, "High", APPLY_NOW),
        (75, "High", APPLY_NOW),  # >= 75 is always "Apply now", whatever the effort
        (74, "Low", TAILOR_THEN_APPLY),
        (60, "Low", TAILOR_THEN_APPLY),
        (65, "Medium", TAILOR_THEN_APPLY),
        (74, "High", STRETCH),  # 60-74 but high effort
        (59, "Low", STRETCH),  # below 60
        (0, "Low", STRETCH),
    ],
)
def test_recommendation_rules(score: int, effort: str, expected: str) -> None:
    assert recommend(score, effort) == expected


# ---------------------------------------------------------------------------
# Effort to reach 80+
# ---------------------------------------------------------------------------


def test_effort_already_above_target() -> None:
    effort = effort_to_target(85, gains(10, 5))
    assert effort == {"points_needed": 0, "gain": 0, "steps": 0, "reachable": True, "level": "Low"}


def test_effort_uses_biggest_gains_first_and_sums_them() -> None:
    # 72 -> needs 8: the 9-point fix alone is enough (order given does not matter).
    effort = effort_to_target(72, gains(3, 9, 6))
    assert effort == {"points_needed": 8, "gain": 9, "steps": 1, "reachable": True, "level": "Low"}


def test_effort_medium_when_gap_needs_several_fixes() -> None:
    # 65 -> needs 15: 6 + 5 + 4 = 15 in 3 steps.
    effort = effort_to_target(65, gains(4, 6, 5, 2))
    assert effort["gain"] == 15 and effort["steps"] == 3 and effort["level"] == "Medium"


def test_effort_high_when_unreachable_or_big_gap() -> None:
    unreachable = effort_to_target(70, gains(3, 2))  # needs 10, only 5 available
    assert unreachable["reachable"] is False and unreachable["level"] == "High" and unreachable["gain"] == 5
    big_gap = effort_to_target(55, gains(15, 10, 5))  # needs 25 points
    assert big_gap["reachable"] is True and big_gap["level"] == "High"
    many_steps = effort_to_target(70, gains(2, 2, 2, 2, 2))  # 10 points in 5 steps
    assert many_steps["level"] == "High"


def test_effort_ignores_missing_or_negative_gains() -> None:
    effort = effort_to_target(78, [{"estimated_score_gain": None}, {}, {"estimated_score_gain": -4}, {"estimated_score_gain": 2}])
    assert effort["gain"] == 2 and effort["steps"] == 1 and effort["reachable"]


# ---------------------------------------------------------------------------
# Common gaps / strengths
# ---------------------------------------------------------------------------


def test_common_gaps_threshold_frequency_and_order() -> None:
    rows = [
        row("a", 70, name="A", missing_required=["Kubernetes", "SQL"], missing_preferred=["Figma"]),
        row("b", 60, name="B", missing_required=["kubernetes"], missing_preferred=["Figma", "Rust"]),
        row("c", 50, name="C", missing_required=["Kubernetes"], missing_preferred=[]),
        row("d", 40, name="D", missing_required=[], missing_preferred=["GraphQL"]),
    ]
    gaps = common_gaps(rows)
    # Kubernetes 3/4, Figma 2/4 (= 50% counts), SQL / Rust / GraphQL 1/4 (dropped).
    assert [g["skill"] for g in gaps] == ["Kubernetes", "Figma"]
    k8s = gaps[0]
    assert k8s["missing_in"] == 3 and k8s["of"] == 4 and k8s["share"] == 0.75 and k8s["required_in"] == 3
    assert k8s["jobs"] == ["A", "B", "C"]  # case-insensitive merge, most common spelling shown


def test_common_gaps_ties_prefer_required_then_name() -> None:
    rows = [
        row("a", 70, missing_required=["Docker"], missing_preferred=["Airflow"]),
        row("b", 60, missing_required=[], missing_preferred=["Docker", "Airflow"]),
    ]
    assert [g["skill"] for g in common_gaps(rows)] == ["Docker", "Airflow"]


def test_common_gaps_counts_a_skill_once_per_job() -> None:
    rows = [row("a", 70, missing_required=["SQL"], missing_preferred=["sql"]), row("b", 70), row("c", 70)]
    assert common_gaps(rows) == []  # 1 of 3 jobs, even though listed twice in job a


def test_strengths_need_a_majority() -> None:
    rows = [
        row("a", 70, matched_skills=["Python", "SQL", "Jira"]),
        row("b", 60, matched_skills=["python", "SQL"]),
        row("c", 50, matched_skills=["SQL"]),
        row("d", 50, matched_skills=["Jira"]),
    ]
    result = strengths(rows)
    assert [s["skill"] for s in result] == ["SQL"]  # 3/4; Python and Jira are only 2/4
    assert result[0] == {"skill": "SQL", "matched_in": 3, "of": 4}


def test_empty_inputs() -> None:
    assert common_gaps([]) == [] and strengths([]) == []
    insights = build_insights([])
    assert insights["ranking"] == [] and insights["heatmap"]["skills"] == []
    assert rules_summary(summary_facts(insights)) == "No job has been scored yet."


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def _req(score: float) -> dict:
    return {"required_skills": {"score": score, "max": 35}}


def test_ranking_order_and_tie_breaks() -> None:
    rows = [
        row("low", 55, position=0),
        row("tie-fewer-skills", 80, position=1, breakdown=_req(20)),
        row("tie-late-deadline", 80, position=2, breakdown=_req(30), deadline="2026-12-31"),
        row("tie-early-deadline", 80, position=3, breakdown=_req(30), deadline="2026-11-30"),
        row("tie-no-deadline", 80, position=4, breakdown=_req(30)),
        row("best", 91, position=5),
    ]
    assert [r.job_id for r in rank_jobs(rows)] == [
        "best", "tie-early-deadline", "tie-late-deadline", "tie-no-deadline", "tie-fewer-skills", "low",
    ]


def test_build_insights_ranking_rows() -> None:
    rows = [
        row("a", 62, position=0, name="Swiggy - APM", priorities=gains(10, 9), missing_required=["SQL", "Excel"],
            missing_preferred=["Figma", "Rust"], deadline="2026-12-01"),
        row("b", 81, position=1, name="Python Dev", priorities=gains(3)),
        row("c", 40, position=2, name="Stretch", priorities=gains(5)),
    ]
    rows[0].label = "Moderate"
    ranking = build_insights(rows)["ranking"]
    assert [(r["rank"], r["name"], r["recommendation"]) for r in ranking] == [
        (1, "Python Dev", APPLY_NOW), (2, "Swiggy - APM", TAILOR_THEN_APPLY), (3, "Stretch", STRETCH),
    ]
    apm = ranking[1]
    assert apm["label"] == "Moderate" and apm["effort"]["level"] == "Medium" and apm["effort"]["points_needed"] == 18
    assert apm["top_missing"] == ["SQL", "Excel", "Figma"]  # required first, max 3
    assert apm["deadline"] == "2026-12-01"


def test_short_label() -> None:
    assert short_label("Excellent match") == "Excellent" and short_label("Strong") == "Strong" and short_label(None) == "Low"


# ---------------------------------------------------------------------------
# Heatmap
# ---------------------------------------------------------------------------


def test_heatmap_rows_cells_and_have_flag() -> None:
    def breakdown(*matches):
        return {"required_skills": {"data": {"matches": [{"skill": s, "matched": m} for s, m in matches]}}}

    rows = [
        row("a", 70, name="A", jd={"required_skills": ["Python", "SQL"], "preferred_skills": ["Docker"], "tools": []},
            breakdown=breakdown(("Python", True), ("SQL", False))),
        row("b", 60, name="B", jd={"required_skills": ["SQL"], "preferred_skills": [], "tools": ["Python", "Jira"]},
            breakdown=breakdown(("SQL", True))),
    ]
    heat = skill_heatmap(rows)
    assert [j["name"] for j in heat["jobs"]] == ["A", "B"]
    by_skill = {s["skill"]: s for s in heat["skills"]}
    # Order: mentioned by both (SQL required twice before Python required once), then the rest by name.
    assert [s["skill"] for s in heat["skills"]] == ["SQL", "Python", "Docker", "Jira"]
    assert by_skill["SQL"]["cells"] == ["required", "required"] and by_skill["SQL"]["have"] is True  # matched for job b
    assert by_skill["Python"]["cells"] == ["required", "preferred"]
    assert by_skill["Docker"]["cells"] == ["preferred", "none"] and by_skill["Docker"]["have"] is False


def test_heatmap_limited_to_top_20() -> None:
    jd = {"required_skills": [f"Skill{i:02d}" for i in range(30)], "preferred_skills": [], "tools": []}
    assert len(skill_heatmap([row("a", 50, jd=jd)])["skills"]) == 20


# ---------------------------------------------------------------------------
# Rule-based summary (fallback)
# ---------------------------------------------------------------------------


def test_rules_summary_is_grounded_in_facts() -> None:
    rows = [
        row("a", 82, position=0, name="Python Dev", deadline="2026-11-20"),
        row("b", 66, position=1, name="Analyst", priorities=gains(8, 6), missing_required=["Tableau"]),
        row("c", 45, position=2, name="AI PM", missing_required=["Tableau"]),
    ]
    text = rules_summary(summary_facts(build_insights(rows)))
    lines = text.splitlines()
    assert 3 <= len(lines) <= 5
    assert lines[0].startswith("Apply first to Python Dev: best fit at 82/100")
    assert "Tableau (missing in 2 of 3)" in text
    assert "Python Dev closes on 2026-11-20" in text
