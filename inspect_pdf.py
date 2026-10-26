"""Show what every pre-LLM stage produces for one PDF (no API key needed).

Usage:
    python inspect_pdf.py data/uploads/sample_resume.pdf
    python inspect_pdf.py my_resume.pdf --lines      # also print every extracted line with its font info

Great for debugging: if a section or a phone number is missing in the final JSON, check here
whether the text, the section split or the regex rules already lost it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pymupdf

from app.config import get_settings
from app.pipeline import extract, rules, sections, triage
from app.pipeline.ingest import IngestError, ingest_path
from app.pipeline.run import ResumePipeline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the output of each pre-LLM pipeline stage.")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--lines", action="store_true", help="print every raw line with bbox / font size / bold")
    args = parser.parse_args(argv)
    settings = get_settings()

    print("== 1. INGEST")
    try:
        ingested = ingest_path(args.pdf, settings.max_file_bytes)
    except IngestError as exc:
        print(f"REJECTED: {exc}")
        return 1
    print(f"ok - {ingested.page_count} page(s), sha256 {ingested.sha256[:16]}...")

    print("\n== 2. TRIAGE (chars per page; < OCR_MIN_CHARS means scanned)")
    with pymupdf.open(stream=ingested.data, filetype="pdf") as doc:
        for info in triage.triage_document(doc, settings.ocr_min_chars):
            print(f"page {info.page_number + 1}: {info.char_count} chars -> {'SCANNED (OCR)' if info.is_scanned else 'text'}")
        if args.lines:
            print("\n== 3. RAW LINES (page, x0, y0, font size, bold, text)")
            for page in doc:
                for b in extract.extract_page_blocks(page):
                    print(f"p{b.page + 1} x={b.x0:6.1f} y={b.y0:6.1f} size={b.font_size:5.1f} {'B' if b.is_bold else ' '} {b.text}")

    # Stages 3-4 (+OCR) via the pipeline helper.
    extraction = ResumePipeline(settings).extract_text(ingested.data)
    print(f"\n== 3. LINKS (hyperlink annotations): {[l.uri for l in extraction.links] or 'none'}")
    print(f"\n== 4. CLEAN - two-column pages: {[p + 1 for p in extraction.two_column_pages] or 'none'}")
    for warning in extraction.warnings:
        print(f"warning: {warning}")
    print("-" * 60)
    print(extraction.text)
    print("-" * 60)

    print("\n== 5. SECTIONS")
    result = sections.detect_sections(extraction.blocks)
    print(f"body font size: {result.body_font_size}")
    for name in result.order:
        text = result.sections[name]
        preview = text[:160].replace("\n", " / ")
        print(f"[{name}] ({len(text)} chars) {preview}{'...' if len(text) > 160 else ''}")

    print("\n== 6. RULES")
    found = rules.apply_rules(result.sections.get("header", ""), extraction.text, extraction.blocks, extraction.links)
    print(json.dumps({"name": found.name, "emails": found.emails, "phones": found.phones, "links": found.links}, indent=2))
    print("date ranges:", rules.find_date_ranges(extraction.text))
    return 0


if __name__ == "__main__":
    sys.exit(main())
