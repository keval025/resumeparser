"""Pipeline orchestration: PDF bytes in -> validated ``ResumeData`` out.

Order of stages:
  ingest -> triage -> extract (or OCR per scanned page) -> clean -> sections -> rules
  -> LLM extraction -> grounding -> normalize -> sanity + links -> confidence -> ResumeData

Grounding runs before normalisation on purpose (see validate.py).
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from app.config import Settings
from app.db import ResumeRepository
from app.pipeline import clean, extract, ocr, rules, sections, triage
from app.pipeline.ingest import IngestedFile, ingest_bytes, ingest_path
from app.pipeline.llm_extract import LLMProvider, ResumeExtractor, format_hints, get_provider
from app.pipeline.normalize import normalize_resume
from app.pipeline.validate import (
    ground_extraction,
    merge_links,
    needs_review,
    sanity_check,
    score_confidence,
)
from app.schema import Meta, ResumeData

logger = logging.getLogger(__name__)


@dataclass
class ParseOutcome:
    """Result of running the pipeline on one file."""

    data: ResumeData
    raw_text: str
    sha256: str
    cached: bool = False
    record_id: str | None = None


@dataclass
class TextExtraction:
    """Output of the text stages (everything before the LLM)."""

    blocks: list[extract.TextBlock]
    links: list[extract.PdfLink]
    text: str
    is_scanned: bool
    scanned_pages: list[int]
    two_column_pages: list[int]
    warnings: list[str]


class ResumePipeline:
    """Runs every stage. Create once and reuse (the LLM client is reused between calls)."""

    def __init__(
        self,
        settings: Settings,
        provider: LLMProvider | None = None,
        repository: ResumeRepository | None = None,
    ) -> None:
        self.settings = settings
        self._provider = provider
        self.repository = repository

    @property
    def provider(self) -> LLMProvider:
        # Created lazily so the API can start (and /health can report) without an API key.
        if self._provider is None:
            self._provider = get_provider(self.settings)
        return self._provider

    # ------------------------------------------------------------------ entry points

    def parse_file(self, path: Path, use_cache: bool = True) -> ParseOutcome:
        """Parse a PDF from disk."""
        ingested = ingest_path(path, self.settings.max_file_bytes)
        return self.parse_ingested(ingested, use_cache=use_cache)

    def parse_bytes(self, data: bytes, file_name: str, use_cache: bool = True) -> ParseOutcome:
        """Parse uploaded bytes (validated + saved to UPLOAD_DIR)."""
        ingested = ingest_bytes(data, file_name, self.settings.max_file_bytes, self.settings.upload_dir)
        return self.parse_ingested(ingested, use_cache=use_cache)

    def parse_ingested(
        self, ingested: IngestedFile, use_cache: bool = True, user_id: str | None = None, storage_path: str | None = None
    ) -> ParseOutcome:
        """Parse an already-validated file, returning a cached result when this user already parsed it.

        ``user_id`` owns the saved record (and scopes the cache: users never share parse results).
        """
        if use_cache and self.repository is not None:
            cached = self.repository.find_done_by_hash(ingested.sha256, user_id)
            if cached is not None and cached.data:
                logger.info("cache hit", extra={"sha256": ingested.sha256, "record_id": cached.id})
                data = ResumeData.model_validate(cached.data)
                return ParseOutcome(data=data, raw_text=cached.raw_text or "", sha256=ingested.sha256, cached=True, record_id=cached.id)

        data, raw_text = self.run(ingested)
        record_id = None
        if self.repository is not None:
            record = self.repository.create_pending(ingested.file_name, ingested.sha256, user_id, storage_path)
            self.repository.mark_done(record.id, data.model_dump(mode="json"), raw_text)
            record_id = record.id
        return ParseOutcome(data=data, raw_text=raw_text, sha256=ingested.sha256, record_id=record_id)

    # ------------------------------------------------------------------ stages

    def extract_text(self, pdf_bytes: bytes) -> TextExtraction:
        """Triage + layout extraction (+ OCR) + cleaning."""
        s = self.settings
        warnings: list[str] = []
        blocks: list[extract.TextBlock] = []
        links: list[extract.PdfLink] = []
        scanned_pages: list[int] = []

        with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
            page_count = doc.page_count
            page_info = triage.triage_document(doc, s.ocr_min_chars)
            for info in page_info:
                page = doc[info.page_number]
                links.extend(extract.extract_page_links(page))
                if not info.is_scanned:
                    blocks.extend(extract.extract_page_blocks(page))
                    continue
                scanned_pages.append(info.page_number)
                try:
                    blocks.extend(
                        ocr.ocr_page(
                            pdf_bytes,
                            info.page_number,
                            page.rect.width,
                            page.rect.height,
                            dpi=s.ocr_dpi,
                            tesseract_cmd=s.tesseract_cmd,
                            poppler_path=s.poppler_path,
                        )
                    )
                except ocr.OCRUnavailableError as exc:
                    warnings.append(f"ocr_unavailable: page {info.page_number + 1}: {exc}")
                    # Use whatever little text the page has rather than nothing.
                    blocks.extend(extract.extract_page_blocks(page))

        cleaned, two_col = clean.clean_blocks(blocks, page_count)
        text = clean.blocks_to_text(cleaned)
        if scanned_pages:
            warnings.append(f"ocr_used: pages {[p + 1 for p in scanned_pages]}")
        return TextExtraction(
            blocks=cleaned,
            links=links,
            text=text,
            is_scanned=bool(scanned_pages),
            scanned_pages=scanned_pages,
            two_column_pages=two_col,
            warnings=warnings,
        )

    def run(self, ingested: IngestedFile) -> tuple[ResumeData, str]:
        """Run the whole pipeline (no cache). Returns ``(ResumeData, cleaned_text)``."""
        s = self.settings
        started = time.perf_counter()
        log_ctx = {"file_name": ingested.file_name, "sha256": ingested.sha256}

        extraction = self.extract_text(ingested.data)
        warnings = list(extraction.warnings)
        if not extraction.text.strip():
            raise ValueError("No text could be extracted from the PDF (is OCR installed for scanned files?).")
        logger.info(
            "text extracted",
            extra={**log_ctx, "chars": len(extraction.text), "two_column_pages": extraction.two_column_pages},
        )

        section_result = sections.detect_sections(extraction.blocks)
        detected = {name for name, text in section_result.sections.items() if text and name != "header"}
        logger.info("sections detected", extra={**log_ctx, "sections": section_result.order})

        rule_result = rules.apply_rules(
            section_result.sections.get("header", ""), extraction.text, extraction.blocks, extraction.links
        )
        hints = format_hints(rule_result.name, rule_result.email, rule_result.phone, rule_result.links)

        extractor = ResumeExtractor(self.provider, s.llm_single_call_max_chars)
        resume, mode = extractor.extract(section_result.sections, extraction.text, hints)
        logger.info("llm extraction done", extra={**log_ctx, "mode": mode})

        # Grounding source = cleaned text + hyperlink anchors/URIs (links are not always visible text).
        grounding_source = extraction.text + "\n" + "\n".join(f"{l.anchor_text} {l.uri}" for l in extraction.links)
        grounding = ground_extraction(resume, grounding_source, s.grounding_threshold)
        warnings.extend(grounding.dropped)

        # Fall back to the layout name if the LLM gave none (or a hallucinated one).
        if not resume.basics.name and rule_result.name:
            resume.basics.name = rule_result.name
            warnings.append("name_from_layout: LLM name missing, used largest text at top of page 1")

        resume, total_months, norm_warnings = normalize_resume(resume, s.normalization_file)
        warnings.extend(norm_warnings)
        unparsed = Counter(w.split(": ", 1)[1].split("[")[0] for w in norm_warnings if w.startswith("unparsed_date"))

        merge_links(resume, rule_result)
        sanity = sanity_check(resume, rule_result)
        warnings.extend(sanity.warnings)

        confidence = score_confidence(
            resume, rule_result, grounding, sanity, detected, dict(unparsed), extraction.is_scanned
        )
        processing_ms = int((time.perf_counter() - started) * 1000)
        meta = Meta(
            source_hash=ingested.sha256,
            file_name=ingested.file_name,
            parser_version=s.parser_version,
            is_scanned=extraction.is_scanned,
            pages=ingested.page_count,
            confidence=confidence,
            needs_review=needs_review(confidence, s.review_threshold),
            warnings=warnings,
            processing_ms=processing_ms,
            total_experience_months=total_months,
        )
        data = ResumeData(**resume.model_dump(), meta=meta)
        logger.info(
            "resume parsed",
            extra={**log_ctx, "processing_ms": processing_ms, "needs_review": meta.needs_review, "warnings": len(warnings)},
        )
        return data, extraction.text
