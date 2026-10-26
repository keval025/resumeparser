"""OCR fallback for scanned pages (Tesseract via pytesseract, rendering via pdf2image/Poppler).

OCR output is converted into the same ``TextBlock`` lines as the text extractor, so the rest of
the pipeline does not care where the text came from. Coordinates are converted from image
pixels back to PDF points (1 point = 1/72 inch) so column detection still works.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from pdf2image import convert_from_bytes
from pdf2image.exceptions import PDFInfoNotInstalledError
import pytesseract
from pytesseract import Output, TesseractNotFoundError

from app.pipeline.extract import TextBlock

logger = logging.getLogger(__name__)

# Words recognised with confidence below this (0-100) are ignored as noise.
_MIN_WORD_CONFIDENCE = 30


class OCRUnavailableError(RuntimeError):
    """Tesseract or Poppler is not installed / not found."""


def configure_tesseract(tesseract_cmd: str | None) -> None:
    """Point pytesseract at tesseract.exe when it is not on PATH (common on Windows)."""
    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd


def is_ocr_available(tesseract_cmd: str | None = None) -> bool:
    """True when the Tesseract binary can be executed."""
    configure_tesseract(tesseract_cmd)
    try:
        pytesseract.get_tesseract_version()
        return True
    except (TesseractNotFoundError, OSError):
        return False


def ocr_page(
    pdf_bytes: bytes,
    page_number: int,
    page_width: float,
    page_height: float,
    dpi: int = 300,
    tesseract_cmd: str | None = None,
    poppler_path: str | None = None,
) -> list[TextBlock]:
    """Render one page (0-based ``page_number``) to an image and OCR it into text lines."""
    configure_tesseract(tesseract_cmd)
    try:
        images = convert_from_bytes(
            pdf_bytes, dpi=dpi, first_page=page_number + 1, last_page=page_number + 1, poppler_path=poppler_path
        )
    except PDFInfoNotInstalledError as exc:
        raise OCRUnavailableError(
            "Poppler is not installed or POPPLER_PATH is wrong (needed to render scanned pages)."
        ) from exc
    if not images:
        return []
    image = images[0]

    try:
        data = pytesseract.image_to_data(image, output_type=Output.DICT)
    except TesseractNotFoundError as exc:
        raise OCRUnavailableError("Tesseract is not installed or TESSERACT_CMD is wrong.") from exc

    # Scale factors: image pixels -> PDF points.
    sx = page_width / image.width
    sy = page_height / image.height

    # Group words into lines using Tesseract's (block, paragraph, line) numbering.
    lines: dict[tuple[int, int, int], list[int]] = defaultdict(list)
    for i, word in enumerate(data["text"]):
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if word.strip() and conf >= _MIN_WORD_CONFIDENCE:
            lines[(data["block_num"][i], data["par_num"][i], data["line_num"][i])].append(i)

    blocks: list[TextBlock] = []
    for (block_num, _par, _line), idxs in lines.items():
        left = min(data["left"][i] for i in idxs)
        top = min(data["top"][i] for i in idxs)
        right = max(data["left"][i] + data["width"][i] for i in idxs)
        bottom = max(data["top"][i] + data["height"][i] for i in idxs)
        text = " ".join(data["text"][i].strip() for i in idxs)
        line_height_pt = (bottom - top) * sy
        blocks.append(
            TextBlock(
                text=text,
                x0=left * sx,
                y0=top * sy,
                x1=right * sx,
                y1=bottom * sy,
                # Rough estimate: a line's box is ~1.2x the font size. Good enough to spot
                # large headings / the name.
                font_size=round(line_height_pt / 1.2, 2),
                is_bold=False,  # Tesseract does not report boldness reliably
                page=page_number,
                page_width=page_width,
                page_height=page_height,
                block_no=int(block_num),
                source="ocr",
            )
        )
    logger.info("ocr page done", extra={"page": page_number, "lines": len(blocks)})
    return blocks
