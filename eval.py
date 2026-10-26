"""Evaluate parser output against hand-labelled "golden" JSON files.

For every ``tests/golden/<name>.json`` the script looks for ``<pred_dir>/<name>.json`` and
compares field by field:

* scalar fields (name, email, phone, links...) -> exact match after light normalisation
* list fields (experience, education, projects, skills, ...) -> F1 score on item keys
  (e.g. an experience item is identified by company + title)

Usage:
    python cli.py --input data/uploads --output data/output
    python eval.py --pred data/output --golden tests/golden
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable

from rapidfuzz import fuzz


def _norm(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).lower().strip()
    text = re.sub(r"^https?://(www\.)?", "", text).rstrip("/")
    return " ".join(re.sub(r"[^\w@+.#/ ]", " ", text).split())


def _same(a: Any, b: Any) -> bool:
    """Lenient equality: normalised exact match, or near-identical (fuzzy >= 90)."""
    na, nb = _norm(a), _norm(b)
    if na == nb:
        return True
    return bool(na and nb) and fuzz.ratio(na, nb) >= 90


def _get(data: dict, path: str) -> Any:
    for part in path.split("."):
        data = (data or {}).get(part) if isinstance(data, dict) else None
    return data


def f1(pred_items: list[str], gold_items: list[str]) -> float:
    """F1 between two lists of keys, matching items leniently one-to-one."""
    if not pred_items and not gold_items:
        return 1.0
    if not pred_items or not gold_items:
        return 0.0
    remaining = list(gold_items)
    true_pos = 0
    for p in pred_items:
        for g in remaining:
            if _same(p, g):
                true_pos += 1
                remaining.remove(g)
                break
    precision = true_pos / len(pred_items)
    recall = true_pos / len(gold_items)
    return 0.0 if true_pos == 0 else 2 * precision * recall / (precision + recall)


SCALAR_FIELDS = [
    "basics.name", "basics.email", "basics.phone", "basics.location",
    "basics.links.linkedin", "basics.links.github", "basics.links.portfolio",
    "meta.total_experience_months",
]

ListKey = Callable[[dict], str]
LIST_FIELDS: dict[str, tuple[str, ListKey | None]] = {
    "experience": ("experience", lambda x: f"{x.get('company')} | {x.get('title')}"),
    "experience.dates": ("experience", lambda x: f"{x.get('company')} | {x.get('start')} | {x.get('end')}"),
    "education": ("education", lambda x: f"{x.get('institution')} | {x.get('degree_normalized')}"),
    "projects": ("projects", lambda x: str(x.get("name"))),
    "certifications": ("certifications", lambda x: str(x.get("name"))),
    "achievements": ("achievements", None),
    "skills.technical": ("skills.technical", None),
    "skills.tools": ("skills.tools", None),
    "skills.soft": ("skills.soft", None),
    "skills.languages": ("skills.languages", None),
}


def evaluate_pair(pred: dict, gold: dict) -> dict[str, float]:
    """Per-field score (0-1) for one resume."""
    scores: dict[str, float] = {}
    for path in SCALAR_FIELDS:
        gold_value = _get(gold, path)
        pred_value = _get(pred, path)
        if gold_value is None and pred_value is None:
            scores[path] = 1.0
        else:
            scores[path] = 1.0 if _same(pred_value, gold_value) else 0.0
    for label, (path, key) in LIST_FIELDS.items():
        pred_list = _get(pred, path) or []
        gold_list = _get(gold, path) or []
        if key is not None:
            pred_list = [key(x) for x in pred_list]
            gold_list = [key(x) for x in gold_list]
        scores[label] = f1([str(x) for x in pred_list], [str(x) for x in gold_list])
    return scores


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Per-field accuracy against golden JSON files.")
    parser.add_argument("--pred", type=Path, default=Path("data/output"), help="Folder with parser output JSON")
    parser.add_argument("--golden", type=Path, default=Path("tests/golden"), help="Folder with hand-labelled JSON")
    args = parser.parse_args(argv)

    golden_files = sorted(args.golden.glob("*.json"))
    if not golden_files:
        print(f"No golden files in {args.golden}")
        return 1

    per_field: dict[str, list[float]] = {}
    evaluated = 0
    for gold_path in golden_files:
        pred_path = args.pred / gold_path.name
        if not pred_path.exists():
            print(f"[skip] no prediction for {gold_path.name} (expected {pred_path})")
            continue
        gold = json.loads(gold_path.read_text(encoding="utf-8"))
        pred = json.loads(pred_path.read_text(encoding="utf-8"))
        scores = evaluate_pair(pred, gold)
        evaluated += 1
        bad = [f for f, s in scores.items() if s < 1.0]
        print(f"{gold_path.name}: mean {sum(scores.values()) / len(scores):.2f}" + (f"  (imperfect: {', '.join(bad)})" if bad else ""))
        for field, score in scores.items():
            per_field.setdefault(field, []).append(score)

    if not evaluated:
        print("Nothing evaluated.")
        return 1

    print(f"\nPer-field accuracy over {evaluated} resume(s):")
    width = max(len(f) for f in per_field)
    for field, values in per_field.items():
        print(f"  {field.ljust(width)}  {sum(values) / len(values):6.1%}")
    overall = [v for values in per_field.values() for v in values]
    print(f"  {'OVERALL'.ljust(width)}  {sum(overall) / len(overall):6.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
