"""Tailor orchestration: match -> tailored resume -> re-score -> page check; plus edits, auto-fit and export.

Stages of a new tailoring (stored in ``tailored_resumes.stage`` for the UI):
  tailoring resume -> re-scoring -> checking length
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.db import STATUS_DONE, MatchRepository, ResumeRepository, TailoredRecord, TailorRepository
from app.match_schema import JobDescription, MatchResult, Suggestions
from app.pipeline.llm import LLMError
from app.pipeline.llm_extract import LLMExtractionError
from app.pipeline.render import PAGE_SIZES, TEMPLATES, RenderError, export_filename, render_docx, render_pdf
from app.pipeline.run import ResumePipeline
from app.pipeline.tailor import (
    find_placeholders,
    rate_tailored,
    refresh_flags,
    score_tailored,
    tailor,
    target_pages,
    trim_least_relevant,
)
from app.schema import ResumeData
from app.tailor_schema import SECTIONS, TailoredResume

logger = logging.getLogger(__name__)

MAX_BULLET_CHARS = 600
MAX_SUMMARY_CHARS = 1200
MAX_AUTOFIT_RENDERS = 30


class TailorError(RuntimeError):
    """A user-facing problem (match not finished, unknown template, ...)."""


class PlaceholdersLeft(TailorError):
    """Export refused: the document still contains placeholders."""

    def __init__(self, placeholders: list[dict[str, str]]) -> None:
        super().__init__(f"{len(placeholders)} placeholder(s) still need your input before download.")
        self.placeholders = placeholders


@dataclass
class TailorContext:
    data: ResumeData
    raw_text: str
    jd: JobDescription
    match: MatchResult
    suggestions: Suggestions | None


@dataclass
class ExportedFile:
    content: bytes
    file_name: str
    media_type: str
    page_count: int | None
    target_pages: int


# ---------------------------------------------------------------------------
# Merging user edits (only wording, decisions, visibility and order may change)
# ---------------------------------------------------------------------------


def _merge_bullets(base: list, edited: list[dict[str, Any]]) -> list:
    by_original = {e.get("original_text"): e for e in edited if isinstance(e, dict)}
    for bullet in base:
        e = by_original.get(bullet.original_text)
        if e is None:
            continue
        if isinstance(e.get("text"), str) and e["text"].strip():
            bullet.text = e["text"].strip()[:MAX_BULLET_CHARS]
        if e.get("decision") in ("pending", "accepted", "rejected", "edited"):
            bullet.decision = e["decision"]
        bullet.hidden = bool(e.get("hidden", bullet.hidden))
    order = [e.get("original_text") for e in edited if isinstance(e, dict)]
    if sorted(order, key=str) == sorted((b.original_text for b in base), key=str):
        base.sort(key=lambda b: order.index(b.original_text))
    return base


def apply_user_edits(base: TailoredResume, edited: dict[str, Any]) -> TailoredResume:
    """The validated AI document with the user's allowed changes applied. Facts always come from ``base``."""
    doc = base.model_copy(deep=True)
    if isinstance(edited.get("summary"), str):
        doc.summary = edited["summary"].strip()[:MAX_SUMMARY_CHARS]
    if edited.get("summary_decision") in ("pending", "accepted", "rejected", "edited"):
        doc.summary_decision = edited["summary_decision"]

    order = [s for s in dict.fromkeys(edited.get("section_order") or []) if s in SECTIONS]
    if order:
        doc.section_order = order + [s for s in doc.section_order if s not in order]
    if isinstance(edited.get("hidden_sections"), list):
        doc.hidden_sections = [s for s in dict.fromkeys(edited["hidden_sections"]) if s in SECTIONS]

    skills = edited.get("skills") or {}
    for category in ("technical", "tools", "soft", "languages"):
        new = skills.get(category)
        if isinstance(new, list) and sorted(new) == sorted(getattr(doc.skills, category)):
            setattr(doc.skills, category, list(new))  # reorder only

    edited_jobs = {j.get("source_index"): j for j in edited.get("experience") or [] if isinstance(j, dict)}
    for job in doc.experience:
        if job.source_index in edited_jobs:
            _merge_bullets(job.bullets, edited_jobs[job.source_index].get("bullets") or [])

    edited_projects = [p for p in edited.get("projects") or [] if isinstance(p, dict)]
    by_index = {p.get("source_index"): p for p in edited_projects}
    for project in doc.projects:
        e = by_index.get(project.source_index)
        if e is None:
            continue
        project.hidden = bool(e.get("hidden", project.hidden))
        _merge_bullets(project.bullets, e.get("bullets") or [])
    project_order = [p.get("source_index") for p in edited_projects]
    if sorted(project_order, key=str) == sorted((p.source_index for p in doc.projects), key=str):
        doc.projects.sort(key=lambda p: project_order.index(p.source_index))
    return refresh_flags(doc)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class TailorService:
    def __init__(
        self,
        settings: Settings,
        pipeline: ResumePipeline,
        resumes: ResumeRepository,
        matches: MatchRepository,
        tailors: TailorRepository,
    ) -> None:
        self.settings = settings
        self.pipeline = pipeline
        self.resumes = resumes
        self.matches = matches
        self.tailors = tailors

    # ------------------------------------------------------------------ loading

    def context(self, match_id: str) -> TailorContext:
        match = self.matches.get(match_id)
        if match is None:
            raise TailorError("Match not found.")
        if match.status != STATUS_DONE or not match.result or not match.jd_parsed:
            raise TailorError("The match is not finished yet.")
        resume = self.resumes.get(match.resume_id or "", match.user_id)
        if resume is None or not resume.data:
            raise TailorError("The resume for this match is no longer available.")
        return TailorContext(
            data=ResumeData.model_validate(resume.data),
            raw_text=resume.raw_text or "",
            jd=JobDescription.model_validate(match.jd_parsed),
            match=MatchResult.model_validate(match.result),
            suggestions=Suggestions.model_validate(match.suggestions) if match.suggestions else None,
        )

    @staticmethod
    def document(record: TailoredRecord) -> TailoredResume:
        if not record.tailored:
            raise TailorError("This tailored resume is not ready yet.")
        return TailoredResume.model_validate(record.document)

    @staticmethod
    def check_options(template: str | None, page_size: str | None) -> None:
        if template is not None and template not in TEMPLATES:
            raise TailorError(f"Unknown template '{template}' (choose from {', '.join(TEMPLATES)}).")
        if page_size is not None and page_size not in PAGE_SIZES:
            raise TailorError(f"Unknown page size '{page_size}' (choose from {', '.join(PAGE_SIZES)}).")

    # ------------------------------------------------------------------ create

    def run(self, tailor_id: str) -> None:
        """Background job: tailor, re-score, check length. Never raises."""
        started = time.perf_counter()
        record = self.tailors.get(tailor_id)
        if record is None:
            return
        try:
            ctx = self.context(record.match_id)
            self.tailors.update(tailor_id, stage="tailoring resume")
            doc, warnings = tailor(self.pipeline.provider, ctx.data, ctx.jd, ctx.match, ctx.suggestions, ctx.raw_text)

            self.tailors.update(tailor_id, stage="re-scoring")
            ratings = rate_tailored(self.pipeline.provider, doc, ctx.data, ctx.jd)

            self.tailors.update(tailor_id, stage="checking length")
            pages = None
            try:
                pages = render_pdf(doc, record.template, record.page_size)[1]
            except RenderError as exc:
                warnings.append(f"pdf_unavailable: {exc}")
            target = target_pages(ctx.data)
            if pages and pages > target:
                warnings.append(f"too_long: {pages} pages; the target is {target} (use Auto-fit)")

            result = score_tailored(
                doc, ctx.data, ctx.jd, ratings, self.settings.normalization_file, self.settings.match_fuzzy_threshold, pages
            )
            self.tailors.update(
                tailor_id,
                status=STATUS_DONE,
                stage=None,
                error=None,
                tailored_json=doc.model_dump(mode="json"),
                ratings_json=ratings,
                warnings_json=warnings,
                new_score=result.score,
                new_result_json=result.model_dump(mode="json"),
                page_count=pages,
                target_pages=target,
            )
            logger.info(
                "tailor done",
                extra={
                    "tailor_id": tailor_id,
                    "old_score": record.old_score,
                    "new_score": result.score,
                    "processing_ms": int((time.perf_counter() - started) * 1000),
                },
            )
        except (LLMError, LLMExtractionError, TailorError) as exc:
            logger.error("tailor failed", extra={"tailor_id": tailor_id, "error": str(exc)})
            self.tailors.mark_failed(tailor_id, str(exc))
        except Exception as exc:  # any failure must end up in the DB, not crash the worker
            logger.exception("tailor failed", extra={"tailor_id": tailor_id})
            self.tailors.mark_failed(tailor_id, f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ edits / re-score

    def _rescore(self, record: TailoredRecord, doc: TailoredResume, pages: int | None) -> MatchResult:
        ctx = self.context(record.match_id)
        return score_tailored(
            doc, ctx.data, ctx.jd, record.ratings, self.settings.normalization_file, self.settings.match_fuzzy_threshold, pages
        )

    def save_edits(self, tailor_id: str, edited: dict[str, Any], template: str | None, page_size: str | None) -> None:
        record = self._get_done(tailor_id)
        self.check_options(template, page_size)
        doc = apply_user_edits(TailoredResume.model_validate(record.tailored), edited)
        result = self._rescore(record, doc, record.page_count)
        self.tailors.update(
            tailor_id,
            user_edits=doc.model_dump(mode="json"),
            template=template or record.template,
            page_size=page_size or record.page_size,
            new_score=result.score,
            new_result_json=result.model_dump(mode="json"),
        )

    def preview_document(self, tailor_id: str, edited: dict[str, Any] | None) -> TailoredResume:
        record = self._get_done(tailor_id)
        base = TailoredResume.model_validate(record.tailored)
        return apply_user_edits(base, edited) if edited else self.document(record)

    # ------------------------------------------------------------------ auto-fit

    def autofit(self, tailor_id: str, template: str | None, page_size: str | None) -> list[str]:
        """Switch to Compact, then hide the least relevant content until the PDF fits the target pages."""
        record = self._get_done(tailor_id)
        self.check_options(template, page_size)
        template = template or record.template
        page_size = page_size or record.page_size
        doc = self.document(record)
        target = record.target_pages or 1
        steps: list[str] = []

        pages = render_pdf(doc, template, page_size)[1]
        if pages > target and template != "compact":
            template = "compact"
            pages = render_pdf(doc, template, page_size)[1]
            steps.append(f"switched to the Compact template ({pages} page{'s' if pages > 1 else ''})")
        renders = 0
        while pages > target and renders < MAX_AUTOFIT_RENDERS:
            # Far over the target: hide a proportional batch per render; close to it: one piece at a time.
            shown = sum(not b.hidden for item in [*doc.experience, *doc.projects] for b in item.bullets)
            batch = max(1, shown * (pages - target) // (pages * 3))
            trimmed = [s for s in (trim_least_relevant(doc) for _ in range(batch)) if s]
            if not trimmed:
                steps.append("nothing left to trim without removing whole jobs")
                break
            pages = render_pdf(doc, template, page_size)[1]
            renders += 1
            summary = trimmed[0] if len(trimmed) == 1 else f"hid {len(trimmed)} least relevant bullets / projects"
            steps.append(f"{summary} -> {pages} page{'s' if pages > 1 else ''}")
        if not steps:
            steps.append(f"already fits: {pages} page{'s' if pages > 1 else ''}")

        result = self._rescore(record, doc, pages)
        self.tailors.update(
            tailor_id,
            user_edits=doc.model_dump(mode="json"),
            template=template,
            page_size=page_size,
            page_count=pages,
            new_score=result.score,
            new_result_json=result.model_dump(mode="json"),
        )
        return steps

    # ------------------------------------------------------------------ export

    def export(self, tailor_id: str, fmt: str, template: str | None, page_size: str | None) -> ExportedFile:
        record = self._get_done(tailor_id)
        self.check_options(template, page_size)
        template = template or record.template
        page_size = page_size or record.page_size
        doc = self.document(record)
        placeholders = find_placeholders(doc)
        if placeholders:
            raise PlaceholdersLeft(placeholders)

        if fmt == "pdf":
            content, pages = render_pdf(doc, template, page_size)
            media_type = "application/pdf"
            if pages != record.page_count:
                result = self._rescore(record, doc, pages)
                self.tailors.update(tailor_id, page_count=pages, new_score=result.score, new_result_json=result.model_dump(mode="json"))
        elif fmt == "docx":
            content, pages = render_docx(doc, template, page_size), None  # Word decides DOCX pagination
            media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        else:
            raise TailorError("format must be 'pdf' or 'docx'.")
        if template != record.template or page_size != record.page_size:
            self.tailors.update(tailor_id, template=template, page_size=page_size)
        return ExportedFile(
            content=content,
            file_name=export_filename(doc, record.company, record.jd_title, fmt),
            media_type=media_type,
            page_count=pages,
            target_pages=record.target_pages or 1,
        )

    def _get_done(self, tailor_id: str) -> TailoredRecord:
        record = self.tailors.get(tailor_id)
        if record is None:
            raise KeyError(tailor_id)
        if record.status != STATUS_DONE:
            raise TailorError("This tailored resume is not ready yet.")
        return record
