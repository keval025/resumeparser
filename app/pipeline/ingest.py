"""Stage 1 - Ingest: validate the upload, hash it, and save the raw file.

Rejects: non-.pdf names, files over the size limit, files that are not PDFs,
encrypted/password-protected PDFs, and corrupt PDFs that PyMuPDF cannot open.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

import pymupdf

logger = logging.getLogger(__name__)


class IngestError(ValueError):
    """Raised when an uploaded file is not acceptable. The message is safe to show to users."""


@dataclass(frozen=True)
class IngestedFile:
    """A validated PDF, ready for parsing."""

    file_name: str
    data: bytes
    sha256: str
    page_count: int
    saved_path: Path | None = None


def sha256_bytes(data: bytes) -> str:
    """Return the hex SHA-256 digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def validate_pdf(data: bytes, file_name: str, max_bytes: int, max_pages: int | None = None) -> int:
    """Check that ``data`` is an acceptable PDF and return its page count.

    Raises:
        IngestError: with a human-readable reason when the file is rejected.
    """
    if not file_name.lower().endswith(".pdf"):
        raise IngestError(f"Only .pdf files are accepted (got '{file_name}').")
    if len(data) == 0:
        raise IngestError("The file is empty.")
    if len(data) > max_bytes:
        raise IngestError(
            f"File is {len(data) / 1_048_576:.1f} MB; the limit is {max_bytes / 1_048_576:.0f} MB."
        )
    # Real PDFs start with "%PDF-" (some generators put a few junk bytes first).
    if b"%PDF-" not in data[:1024]:
        raise IngestError("The file is not a valid PDF (missing %PDF header).")

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # PyMuPDF raises several exception types for broken files
        raise IngestError(f"The PDF is corrupt or unreadable: {exc}") from exc

    with doc:
        if doc.needs_pass or doc.is_encrypted:
            raise IngestError("The PDF is encrypted / password-protected.")
        if doc.page_count == 0:
            raise IngestError("The PDF has no pages.")
        if max_pages is not None and doc.page_count > max_pages:
            raise IngestError(f"The PDF has {doc.page_count} pages; resumes can have at most {max_pages}.")
        # Touch every page once so damaged page trees fail here, not mid-pipeline.
        try:
            for page in doc:
                page.rect  # noqa: B018 - accessing the attribute forces the page to load
        except Exception as exc:
            raise IngestError(f"The PDF is corrupt or unreadable: {exc}") from exc
        return doc.page_count


def save_raw(data: bytes, sha256: str, upload_dir: Path) -> Path:
    """Save the file as ``<upload_dir>/<sha256>.pdf`` (same content -> same file)."""
    upload_dir.mkdir(parents=True, exist_ok=True)
    path = upload_dir / f"{sha256}.pdf"
    if not path.exists():
        path.write_bytes(data)
    return path


def ingest_bytes(
    data: bytes, file_name: str, max_bytes: int, upload_dir: Path | None = None, max_pages: int | None = None
) -> IngestedFile:
    """Validate + hash an uploaded file, optionally saving a copy to ``upload_dir``."""
    page_count = validate_pdf(data, file_name, max_bytes, max_pages)
    digest = sha256_bytes(data)
    saved_path = save_raw(data, digest, upload_dir) if upload_dir is not None else None
    logger.info(
        "ingested file",
        extra={"file_name": file_name, "sha256": digest, "pages": page_count, "bytes": len(data)},
    )
    return IngestedFile(file_name=file_name, data=data, sha256=digest, page_count=page_count, saved_path=saved_path)


def ingest_path(path: Path, max_bytes: int) -> IngestedFile:
    """Validate a PDF already on disk (used by the CLI; the file is not copied)."""
    if path.stat().st_size > max_bytes:
        raise IngestError(
            f"File is {path.stat().st_size / 1_048_576:.1f} MB; the limit is {max_bytes / 1_048_576:.0f} MB."
        )
    return ingest_bytes(path.read_bytes(), path.name, max_bytes, upload_dir=None)
