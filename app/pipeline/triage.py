"""Stage 2 - Triage: decide, page by page, whether a page has a real text layer.

A page with fewer than ``min_chars`` extractable characters is treated as scanned and will be
sent to OCR. Mixed PDFs (some text pages, some scanned pages) are handled naturally.
"""

from __future__ import annotations

from dataclasses import dataclass

import pymupdf


@dataclass(frozen=True)
class PageTriage:
    page_number: int  # 0-based
    char_count: int
    is_scanned: bool


def triage_document(doc: pymupdf.Document, min_chars: int = 50) -> list[PageTriage]:
    """Return one PageTriage per page of ``doc``."""
    results: list[PageTriage] = []
    for page in doc:
        text = page.get_text("text") or ""
        # Count only visible characters; whitespace does not make a page "text-based".
        char_count = sum(1 for ch in text if not ch.isspace())
        results.append(PageTriage(page_number=page.number, char_count=char_count, is_scanned=char_count < min_chars))
    return results
