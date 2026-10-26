"""Stage 3 - Layout extraction with PyMuPDF.

We work at *line* granularity: each visual line becomes one ``TextBlock`` carrying its text,
bounding box, font size, bold flag and page number. Lines are small enough for column
detection and heading detection, and big enough to keep words together.

Hyperlinks are pulled from link annotations, because LinkedIn / GitHub URLs are often hidden
behind an icon or the word "LinkedIn" and never appear in the visible text.
"""

from __future__ import annotations

from dataclasses import dataclass

import pymupdf

# PyMuPDF span flag bit for bold text.
_BOLD_FLAG = 1 << 4


@dataclass
class TextBlock:
    """One line of text on a page."""

    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    font_size: float
    is_bold: bool
    page: int  # 0-based page number
    page_width: float
    page_height: float
    block_no: int = 0
    source: str = "text"  # "text" (PDF text layer) or "ocr"

    @property
    def width(self) -> float:
        return self.x1 - self.x0


@dataclass(frozen=True)
class PdfLink:
    """A clickable URI link found on a page."""

    uri: str
    page: int
    anchor_text: str


def _is_bold(span: dict) -> bool:
    font_name = str(span.get("font", "")).lower()
    return bool(span.get("flags", 0) & _BOLD_FLAG) or "bold" in font_name or "black" in font_name


def extract_page_blocks(page: pymupdf.Page) -> list[TextBlock]:
    """Extract every non-empty text line on ``page``."""
    blocks: list[TextBlock] = []
    width, height = page.rect.width, page.rect.height
    page_dict = page.get_text("dict", sort=False)

    for block in page_dict.get("blocks", []):
        if block.get("type") != 0:  # 0 = text, 1 = image
            continue
        for line in block.get("lines", []):
            spans = [s for s in line.get("spans", []) if s.get("text", "").strip()]
            if not spans:
                continue
            text = "".join(s["text"] for s in line["spans"]).strip()
            x0, y0, x1, y1 = line["bbox"]
            # Font size of the line = size of its largest span (headings may mix sizes).
            font_size = max(float(s.get("size", 0.0)) for s in spans)
            # A line counts as bold when most of its visible characters are bold.
            bold_chars = sum(len(s["text"].strip()) for s in spans if _is_bold(s))
            total_chars = sum(len(s["text"].strip()) for s in spans) or 1
            blocks.append(
                TextBlock(
                    text=text,
                    x0=x0,
                    y0=y0,
                    x1=x1,
                    y1=y1,
                    font_size=round(font_size, 2),
                    is_bold=bold_chars / total_chars >= 0.6,
                    page=page.number,
                    page_width=width,
                    page_height=height,
                    block_no=int(block.get("number", 0)),
                )
            )
    return blocks


def extract_page_links(page: pymupdf.Page) -> list[PdfLink]:
    """Return all external URI links on ``page`` together with the text under the link."""
    links: list[PdfLink] = []
    for link in page.get_links():
        uri = link.get("uri")
        if link.get("kind") != pymupdf.LINK_URI or not uri:
            continue
        rect = link.get("from")
        anchor = page.get_textbox(rect).strip() if rect is not None else ""
        links.append(PdfLink(uri=uri.strip(), page=page.number, anchor_text=" ".join(anchor.split())))
    return links
