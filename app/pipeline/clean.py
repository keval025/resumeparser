"""Stage 4 - Clean: reading order, unicode normalisation, bullets, headers/footers.

Main ideas
----------
* **Two-column layouts.** We cluster the left-x (x0) positions of the lines on a page. If
  there is a large jump between two clusters in the middle of the page, few lines cross that
  gap, and both sides carry a fair share of the text, the page is two-column. We then read:
  full-width lines above the columns (usually the name/contact header) -> left column ->
  right column -> any remaining full-width lines.
* **Unicode.** NFKC normalisation turns ligatures (ﬁ -> fi) and odd widths into plain text;
  a small table fixes smart quotes and bullet symbols.
* **Headers / footers.** Lines in the top/bottom margin that repeat on several pages, and bare
  page numbers ("Page 2 of 3"), are dropped.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

from app.pipeline.extract import TextBlock

# Characters treated as bullet markers at the start of a line.
BULLET_CHARS = "•▪➢◦●■►▸‣⁃∙·○◆◇❖✓✔➤➔→⦿⚫"
_BULLET_RE = re.compile(rf"^\s*[{re.escape(BULLET_CHARS)}*]+\s*")
# Some PDFs use a dash/en-dash as the bullet; normalise those too ("– Built X" -> "- Built X").
_DASH_BULLET_RE = re.compile(r"^\s*[–—-]\s+")

_SMART_CHARS = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "′": "'", "″": '"',
    " ": " ", " ": " ", " ": " ", " ": " ", " ": " ",
    "​": "", "‌": "", "‍": "", "﻿": "", "­": "",  # zero-width / soft hyphen
}
_SMART_TRANS = str.maketrans(_SMART_CHARS)

_PAGE_NUMBER_RE = re.compile(r"^(page\s*)?\d{1,3}(\s*(of|/)\s*\d{1,3})?$", re.IGNORECASE)

# Fraction of page height treated as header / footer margin.
_MARGIN_FRACTION = 0.07


# ---------------------------------------------------------------------------
# Text-level cleaning
# ---------------------------------------------------------------------------


def normalize_unicode(text: str) -> str:
    """Fix ligatures, smart quotes, odd spaces and bullet symbols in a single line."""
    # Replace bullets first: NFKC would otherwise turn some symbols into other characters.
    if _BULLET_RE.match(text):
        text = _BULLET_RE.sub("- ", text, count=1)
    elif _DASH_BULLET_RE.match(text):
        text = _DASH_BULLET_RE.sub("- ", text, count=1)
    text = unicodedata.normalize("NFKC", text).translate(_SMART_TRANS)
    # Remaining private-use characters are icon-font glyphs (phone/mail icons); drop them.
    text = "".join(ch for ch in text if not 0xE000 <= ord(ch) <= 0xF8FF)
    return re.sub(r"[ \t]+", " ", text).strip()


def fix_hyphenation(text: str) -> str:
    """Join words split across lines: 'develop-\\nment' -> 'development'.

    Only joins when the next line starts with a lowercase letter, so real compounds that end a
    line before a capitalised word ("Front-\\nEnd") are kept.
    """
    return re.sub(r"([A-Za-z])-\n([a-z])", r"\1\2", text)


def _footer_key(text: str) -> str:
    """Normalise a margin line for repeat detection (digits vary: 'Page 1' vs 'Page 2')."""
    return re.sub(r"\d+", "#", text.lower()).strip()


def remove_headers_footers(blocks: list[TextBlock], page_count: int) -> list[TextBlock]:
    """Drop page numbers and lines repeated in the top/bottom margin of multiple pages."""
    def in_margin(b: TextBlock) -> bool:
        return b.y1 <= b.page_height * _MARGIN_FRACTION or b.y0 >= b.page_height * (1 - _MARGIN_FRACTION)

    repeated: set[str] = set()
    if page_count >= 2:
        pages_per_key: dict[str, set[int]] = {}
        for b in blocks:
            if in_margin(b):
                pages_per_key.setdefault(_footer_key(b.text), set()).add(b.page)
        repeated = {k for k, pages in pages_per_key.items() if len(pages) >= 2}

    kept: list[TextBlock] = []
    for b in blocks:
        if _PAGE_NUMBER_RE.match(b.text.strip()) and in_margin(b):
            continue
        if in_margin(b) and _footer_key(b.text) in repeated:
            continue
        kept.append(b)
    return kept


# ---------------------------------------------------------------------------
# Column detection and reading order
# ---------------------------------------------------------------------------


def detect_column_split(blocks: list[TextBlock]) -> float | None:
    """Return the x-coordinate separating two columns on one page, or None for one column."""
    if len(blocks) < 8:
        return None
    width = blocks[0].page_width

    # Cluster x0 values: sort them and look for the biggest jump in the middle of the page.
    xs = sorted({round(b.x0, 1) for b in blocks})
    best_gap, split = 0.0, None
    for left, right in zip(xs, xs[1:]):
        gap = right - left
        if 0.2 * width <= right <= 0.8 * width and gap > best_gap:
            best_gap, split = gap, right - 1.0
    if split is None or best_gap < 0.08 * width:
        return None

    left_lines = [b for b in blocks if b.x0 < split]
    right_lines = [b for b in blocks if b.x0 >= split]
    # Lines that start left of the split but run across it are "spanning" lines.
    crossing = [b for b in left_lines if b.x1 > split + 5]
    columns_left = [b for b in left_lines if b.x1 <= split + 5]

    total_chars = sum(len(b.text) for b in blocks) or 1
    right_chars = sum(len(b.text) for b in right_lines)
    left_chars = sum(len(b.text) for b in columns_left)
    if len(crossing) > 0.2 * len(blocks):
        return None  # body text runs across the "gap": single column with indented/right-aligned bits
    # Sidebars are often narrow, so each side only needs a modest share of the text.
    if right_chars < 0.08 * total_chars or left_chars < 0.08 * total_chars:
        return None
    if len(right_lines) < 3 or len(columns_left) < 3:
        return None

    # The two columns must sit side by side (overlap vertically), not one under the other.
    left_top, left_bottom = min(b.y0 for b in columns_left), max(b.y1 for b in columns_left)
    right_top, right_bottom = min(b.y0 for b in right_lines), max(b.y1 for b in right_lines)
    overlap = min(left_bottom, right_bottom) - max(left_top, right_top)
    if overlap < 0.3 * min(left_bottom - left_top, right_bottom - right_top):
        return None
    return split


def _sort_top_down(blocks: list[TextBlock]) -> list[TextBlock]:
    # Round y so lines on the same baseline are ordered left -> right.
    return sorted(blocks, key=lambda b: (round(b.y0 / 3), b.x0))


def order_page_blocks(blocks: list[TextBlock]) -> tuple[list[TextBlock], bool]:
    """Return the page's lines in reading order and whether it was treated as two-column."""
    split = detect_column_split(blocks)
    if split is None:
        return _sort_top_down(blocks), False

    left = [b for b in blocks if b.x0 < split and b.x1 <= split + 5]
    right = [b for b in blocks if b.x0 >= split]
    spanning = [b for b in blocks if b.x0 < split and b.x1 > split + 5]
    columns_top = min(b.y0 for b in left + right)
    header = [b for b in spanning if b.y1 <= columns_top + 2]
    rest = [b for b in spanning if b.y1 > columns_top + 2]
    ordered = _sort_top_down(header) + _sort_top_down(left) + _sort_top_down(right) + _sort_top_down(rest)
    return ordered, True


def clean_blocks(blocks: list[TextBlock], page_count: int) -> tuple[list[TextBlock], list[int]]:
    """Normalise text, drop headers/footers and put all lines into reading order.

    Returns the cleaned lines and the list of (0-based) pages detected as two-column.
    """
    for b in blocks:
        b.text = normalize_unicode(b.text)
    blocks = [b for b in blocks if b.text]
    blocks = remove_headers_footers(blocks, page_count)

    ordered: list[TextBlock] = []
    two_column_pages: list[int] = []
    by_page = Counter(b.page for b in blocks)
    for page in sorted(by_page):
        page_lines, is_two_col = order_page_blocks([b for b in blocks if b.page == page])
        ordered.extend(page_lines)
        if is_two_col:
            two_column_pages.append(page)
    return ordered, two_column_pages


def blocks_to_text(blocks: list[TextBlock]) -> str:
    """Join lines into plain text and repair hyphenated line breaks."""
    return fix_hyphenation("\n".join(b.text for b in blocks))
