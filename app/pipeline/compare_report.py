"""Compare Jobs export: CSV of the ranking, and a printable HTML report rendered to PDF with Playwright."""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from typing import Any

from jinja2 import Environment

COMPONENT_NAMES = {
    "required_skills": "Required skills",
    "preferred_skills": "Preferred skills & tools",
    "experience_relevance": "Experience relevance",
    "years_experience": "Years of experience",
    "education": "Education fit",
    "keywords": "Keyword / ATS coverage",
    "resume_quality": "Resume quality",
}

CSV_COLUMNS = [
    "rank", "job", "title", "company", "score", "label", "recommendation", "effort_to_80", "points_to_80",
    "deadline", "location", "salary", "top_missing_skills", "url", "match_id",
]


def to_csv(view: dict[str, Any]) -> str:
    """Ranked jobs first, then failed / unfinished jobs (with their error) so nothing silently disappears."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(CSV_COLUMNS + ["status", "error"])
    for r in view["insights"]["ranking"]:
        writer.writerow([
            r["rank"], r["name"], r["title"] or "", r["company"] or "", r["score"], r["label"], r["recommendation"],
            r["effort"]["level"], r["effort"]["points_needed"], r["deadline"] or "", r["location"] or "", r["salary"] or "",
            "; ".join(r["top_missing"]), r["url"] or "", r["match_id"] or "", "done", "",
        ])
    for j in view["jobs"]:
        if j["status"] != "done":
            writer.writerow(["", j["name"], j["title"] or "", j["company"] or "", "", "", "", "", "",
                             j["deadline"] or "", j["location"] or "", j["salary"] or "", "", j["url"] or "",
                             j["match_id"] or "", j["status"], j["error"] or ""])
    # BOM so Excel opens UTF-8 (₹, é...) correctly.
    return "﻿" + out.getvalue()


def export_filename(view: dict[str, Any], ext: str) -> str:
    who = re.sub(r"[^\w]+", "_", view.get("candidate_name") or "resume").strip("_")[:40] or "resume"
    return f"job_comparison_{who}_{datetime.now():%Y%m%d}.{ext}"


_REPORT = """<!doctype html>
<html><head><meta charset="utf-8"><title>Job comparison</title>
<style>
  * { box-sizing: border-box; }
  body { font: 10.5pt/1.45 "Segoe UI", Roboto, Helvetica, Arial, sans-serif; color: #1d2130; margin: 0; }
  h1 { font-size: 18pt; margin: 0 0 2px; }
  h2 { font-size: 12pt; margin: 18px 0 8px; padding-bottom: 4px; border-bottom: 1px solid #d9dce5; }
  .muted { color: #646b80; }
  .summary { background: #f0f1f6; border-left: 3px solid #4f46e5; padding: 8px 12px; border-radius: 6px; }
  .summary p { margin: 2px 0; }
  table { width: 100%; border-collapse: collapse; font-size: 9.5pt; }
  th, td { text-align: left; padding: 5px 6px; border-bottom: 1px solid #e3e5ee; vertical-align: top; }
  th { font-size: 8pt; text-transform: uppercase; letter-spacing: .03em; color: #646b80; }
  tr { break-inside: avoid; }
  .score { font-weight: 700; }
  .bar { height: 6px; background: #eceef4; border-radius: 3px; overflow: hidden; width: 70px; margin-top: 3px; }
  .bar > div { height: 100%; background: #4f46e5; }
  .tag { display: inline-block; padding: 1px 7px; border-radius: 9px; font-size: 8.5pt; font-weight: 600; white-space: nowrap; }
  .t-apply { background: #e3f5e9; color: #157f3b; } .t-tailor { background: #fff2dc; color: #a15c00; } .t-stretch { background: #fde8e7; color: #b42318; }
  .cols { display: flex; gap: 18px; } .cols > div { flex: 1; }
  ul { margin: 0; padding-left: 16px; }
  .hm td, .hm th { text-align: center; padding: 3px 4px; font-size: 8.5pt; }
  .hm td:first-child, .hm th:first-child { text-align: left; }
  .c-req-have { background: #b9e6c9; } .c-req-miss { background: #f6c3bf; }
  .c-pref-have { background: #e3f5e9; } .c-pref-miss { background: #fff2dc; } .c-none { color: #b0b5c3; }
  .legend span { display: inline-block; padding: 1px 6px; margin-right: 6px; border-radius: 3px; font-size: 8pt; }
</style></head>
<body>
  <h1>Job comparison</h1>
  <div class="muted">{{ view.candidate_name or "Resume" }}{% if view.file_name %} · {{ view.file_name }}{% endif %} · {{ ranking|length }} job{{ "s" if ranking|length != 1 }} ranked · generated {{ generated }}</div>

  {% if view.summary %}
  <h2>Where to apply first and why</h2>
  <div class="summary">{% for line in view.summary.splitlines() %}<p>{{ line }}</p>{% endfor %}</div>
  {% endif %}

  <h2>Ranking</h2>
  <table>
    <thead><tr><th>#</th><th>Job</th><th>Score</th><th>Label</th><th>Recommendation</th><th>Effort to 80+</th><th>Deadline</th><th>Top missing skills</th></tr></thead>
    <tbody>
    {% for r in ranking %}
      <tr>
        <td>{{ r.rank }}</td>
        <td><b>{{ r.name }}</b>{% if r.company and r.company not in r.name %}<br><span class="muted">{{ r.company }}</span>{% endif %}{% if r.location %}<br><span class="muted">{{ r.location }}</span>{% endif %}</td>
        <td><span class="score">{{ r.score }}</span><div class="bar"><div style="width:{{ r.score }}%"></div></div></td>
        <td>{{ r.label }}</td>
        <td><span class="tag {{ tag_class(r.recommendation) }}">{{ r.recommendation }}</span></td>
        <td>{{ r.effort.level }}{% if r.effort.points_needed %} <span class="muted">(+{{ r.effort.points_needed }} pts)</span>{% endif %}</td>
        <td>{{ r.deadline or "—" }}</td>
        <td>{{ r.top_missing|join(", ") or "—" }}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  {% if failed %}<p class="muted">Not scored: {% for j in failed %}{{ j.name }} ({{ j.error or j.status }}){{ "; " if not loop.last }}{% endfor %}</p>{% endif %}

  <h2>Score breakdown per job</h2>
  <table>
    <thead><tr><th>Component</th>{% for r in ranking %}<th>{{ r.rank }}. {{ r.name[:28] }}</th>{% endfor %}</tr></thead>
    <tbody>
    {% for key, name in components.items() %}
      <tr><td>{{ name }}</td>{% for r in ranking %}{% set c = r.components.get(key) %}<td>{% if c %}{{ c.score }} / {{ c.max }}{% else %}—{% endif %}</td>{% endfor %}</tr>
    {% endfor %}
    </tbody>
  </table>

  <div class="cols">
    <div>
      <h2>Learn these first</h2>
      {% if insights.common_gaps %}<ul>{% for g in insights.common_gaps %}<li><b>{{ g.skill }}</b> <span class="muted">- missing in {{ g.missing_in }} of {{ g.of }} jobs</span></li>{% endfor %}</ul>
      {% else %}<p class="muted">No skill is missing in half of the jobs or more.</p>{% endif %}
    </div>
    <div>
      <h2>Your strengths</h2>
      {% if insights.strengths %}<ul>{% for s in insights.strengths %}<li><b>{{ s.skill }}</b> <span class="muted">- matched in {{ s.matched_in }} of {{ s.of }} jobs</span></li>{% endfor %}</ul>
      {% else %}<p class="muted">No skill matched in most of the jobs.</p>{% endif %}
    </div>
  </div>

  {% if insights.heatmap.skills %}
  <h2>Skill heatmap</h2>
  <div class="legend muted"><span class="c-req-have">required · you have it</span><span class="c-req-miss">required · missing</span><span class="c-pref-have">preferred · you have it</span><span class="c-pref-miss">preferred · missing</span></div>
  <table class="hm">
    <thead><tr><th>Skill</th>{% for j in insights.heatmap.jobs %}<th>{{ loop.index }}</th>{% endfor %}</tr></thead>
    <tbody>
    {% for s in insights.heatmap.skills %}
      <tr><td>{{ s.skill }}{% if s.have %} ✓{% endif %}</td>
      {% for cell in s.cells %}<td class="{{ cell_class(cell, s.have) }}">{{ {"required": "Req", "preferred": "Pref"}.get(cell, "·") }}</td>{% endfor %}</tr>
    {% endfor %}
    </tbody>
  </table>
  <p class="muted">Columns: {% for j in insights.heatmap.jobs %}{{ loop.index }} = {{ j.name }}{{ "; " if not loop.last }}{% endfor %}</p>
  {% endif %}
</body></html>
"""


def _tag_class(recommendation: str) -> str:
    return "t-apply" if recommendation.startswith("Apply") else "t-tailor" if recommendation.startswith("Tailor") else "t-stretch"


def _cell_class(cell: str, have: bool) -> str:
    if cell == "none":
        return "c-none"
    return f"c-{'req' if cell == 'required' else 'pref'}-{'have' if have else 'miss'}"


def report_html(view: dict[str, Any]) -> str:
    env = Environment(autoescape=True)
    template = env.from_string(_REPORT)
    return template.render(
        view=view,
        insights=view["insights"],
        ranking=view["insights"]["ranking"],
        failed=[j for j in view["jobs"] if j["status"] != "done"],
        components=COMPONENT_NAMES,
        generated=datetime.now().strftime("%d %b %Y"),
        tag_class=_tag_class,
        cell_class=_cell_class,
    )
