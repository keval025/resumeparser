"""Batch mode: parse every PDF in a folder and write one JSON file per resume.

Usage:
    python cli.py --input data/uploads --output data/output
    python cli.py --input my_pdfs --output out --no-cache

One bad PDF never stops the batch: its error is reported in the summary table instead.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from app.config import get_settings
from app.db import ResumeRepository, init_db, make_engine
from app.logging_config import setup_logging
from app.pipeline.run import ResumePipeline

logger = logging.getLogger("cli")


def _min_confidence(confidence: dict[str, float]) -> str:
    return f"{min(confidence.values()):.2f}" if confidence else "-"


def print_table(rows: list[list[str]], headers: list[str]) -> None:
    """Print a simple fixed-width table."""
    widths = [max(len(str(x)) for x in col) for col in zip(headers, *rows)]
    widths = [min(w, 40) for w in widths]

    def fmt(row: list[str]) -> str:
        return " | ".join(str(cell)[:w].ljust(w) for cell, w in zip(row, widths))

    print(fmt(headers))
    print("-+-".join("-" * w for w in widths))
    for row in rows:
        print(fmt(row))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parse a folder of resume PDFs into JSON files.")
    parser.add_argument("--input", required=True, type=Path, help="Folder containing .pdf files")
    parser.add_argument("--output", required=True, type=Path, help="Folder to write <name>.json files to")
    parser.add_argument("--no-cache", action="store_true", help="Re-parse files even if already in the database")
    parser.add_argument("--no-db", action="store_true", help="Do not read from or write to the database")
    args = parser.parse_args(argv)

    settings = get_settings()
    setup_logging(settings.log_level)

    if not args.input.is_dir():
        print(f"Input folder not found: {args.input}", file=sys.stderr)
        return 2
    args.output.mkdir(parents=True, exist_ok=True)

    repository = None
    if not args.no_db:
        engine = make_engine(settings.database_url)
        init_db(engine)
        repository = ResumeRepository(engine)
    pipeline = ResumePipeline(settings, repository=repository)

    pdfs = sorted(p for p in args.input.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")
    if not pdfs:
        print(f"No PDF files found in {args.input}")
        return 0

    rows: list[list[str]] = []
    failures = 0
    for pdf in pdfs:
        try:
            outcome = pipeline.parse_file(pdf, use_cache=not args.no_cache)
            data = outcome.data
            out_path = args.output / f"{pdf.stem}.json"
            out_path.write_text(json.dumps(data.model_dump(mode="json"), indent=2, ensure_ascii=False), encoding="utf-8")
            rows.append(
                [
                    pdf.name,
                    data.basics.name or "-",
                    str(len(data.experience)),
                    str(len(data.projects)),
                    _min_confidence(data.meta.confidence),
                    "YES" if data.meta.needs_review else "no",
                    "cached" if outcome.cached else "ok",
                ]
            )
        except Exception as exc:  # keep going: one bad PDF must not stop the batch
            failures += 1
            logger.exception("failed to parse", extra={"file_name": pdf.name})
            rows.append([pdf.name, "-", "-", "-", "-", "-", f"FAILED: {type(exc).__name__}: {exc}"])

    print()
    print_table(rows, ["file", "name", "#experience", "#projects", "confidence(min)", "needs_review", "status"])
    print(f"\n{len(pdfs) - failures}/{len(pdfs)} parsed. JSON written to {args.output.resolve()}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
