"""FastAPI application: JSON API + the web pages.

Pages (public)                        API (signed-in user; every query is limited to the user's own data)
-------------                         ------------------------------------------------------------------
GET /          landing page           POST /resumes, GET /resumes, GET /resumes/{id}, GET /resumes/{id}/file
GET /login     sign in                POST /match, GET /match/{id}, GET /matches
GET /app       the dashboard          POST /tailor, GET|PUT /tailor/{id}, POST /tailor/{id}/preview|autofit|export, GET /tailors
GET /privacy   privacy policy         POST /compare, GET /compare/{id}, POST /compare/{id}/jobs,
GET /terms     terms of service       DELETE /compare/{id}/jobs/{job_id}, GET /compare/{id}/export, GET /compares
GET /health    service status         POST /jd/fetch
                                      GET /me, PUT /me/settings, GET /me/export, DELETE /me

Auth: ``Authorization: Bearer <Supabase access token>`` (see app/auth.py). Without SUPABASE_URL the app runs
in single-user local mode with no login. Records of other users answer 404, exactly like missing ones.
Files live in private storage buckets; downloads are signed links that expire after 10 minutes.

Run locally with:  run.bat   (or:  python serve.py   /   uvicorn app.main:app --reload)
"""

from __future__ import annotations

import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import text

from app import account, web
from app.auth import LOCAL_USER_ID, Authenticator, CurrentUser, get_current_user
from app.cleanup import run_cleanup, start_scheduler
from app.config import Settings, get_settings
from app.db import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    AccountRepository,
    ComparisonRecord,
    ComparisonRepository,
    MatchRecord,
    MatchRepository,
    ProfileRepository,
    ResumeRecord,
    ResumeRepository,
    TailoredRecord,
    TailorRepository,
    UsageRepository,
    init_db,
    make_engine,
)
from app.logging_config import setup_logging
from app.observability import init_sentry
from app.pipeline.compare import (
    ACTIVE_JOB_STATUSES,
    MAX_JOBS,
    MIN_JOBS,
    CompareInputError,
    CompareService,
    JobInput,
    prepare_job,
)
from app.pipeline.compare_report import export_filename, report_html, to_csv
from app.pipeline.ingest import IngestedFile, IngestError, ingest_bytes
from app.pipeline.jd_parse import MAX_JD_CHARS, MIN_JD_CHARS, jd_hash, normalize_jd_text
from app.pipeline.jd_sources import JDSourceError
from app.pipeline.llm import LLMError, LLMProvider
from app.pipeline.match import SCORING_VERSION
from app.pipeline.match_run import MatchService
from app.pipeline.ocr import is_ocr_available
from app.pipeline.render import PAGE_SIZES, TEMPLATES, RenderError, html_to_pdf, render_html
from app.pipeline.run import ResumePipeline
from app.pipeline.tailor import find_placeholders
from app.pipeline.tailor_run import PlaceholdersLeft, TailorError, TailorService
from app.storage import EXPORTS_BUCKET, RESUMES_BUCKET, StorageError, SupabaseAdmin, SupabaseStorage, make_storage, object_path
from app.tailor_schema import TailoredResume
from app.usage import UsageService

logger = logging.getLogger(__name__)

STATIC_DIR = web.STATIC_DIR


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class UploadResponse(BaseModel):
    id: str
    status: str
    cached: bool = False


class ResumeResponse(BaseModel):
    id: str
    file_name: str
    status: str
    data: dict[str, Any] | None = None
    error: str | None = None
    has_file: bool = False


class ResumeSummary(BaseModel):
    id: str
    file_name: str
    status: str
    name: str | None
    email: str | None
    needs_review: bool | None
    has_file: bool = False
    created_at: datetime


class MatchCreated(BaseModel):
    match_id: str
    status: str
    cached: bool = False


class MatchResponse(BaseModel):
    match_id: str
    status: str
    stage: str | None = None
    error: str | None = None
    resume_id: str | None = None
    file_name: str | None = None
    candidate_name: str | None = None
    score: int | None = None
    label: str | None = None
    breakdown: dict[str, Any] | None = None
    matched_skills: list[str] = []
    missing_skills: dict[str, list[str]] = {"required": [], "preferred": []}
    semantic_similarity: float | None = None
    suggestions: dict[str, Any] | None = None
    jd_parsed: dict[str, Any] | None = None
    jd_text: str | None = None
    warnings: list[str] = []
    scoring_version: str
    created_at: datetime


class MatchSummary(BaseModel):
    match_id: str
    status: str
    score: int | None
    label: str | None
    jd_title: str | None
    jd_company: str | None
    candidate_name: str | None
    file_name: str | None
    resume_id: str | None
    created_at: datetime


class TailorRequest(BaseModel):
    match_id: str
    template: str = "modern"
    page_size: str = "A4"
    regenerate: bool = False


class TailorCreated(BaseModel):
    tailor_id: str
    status: str
    existing: bool = False


class TailorEdits(BaseModel):
    document: dict[str, Any]
    template: str | None = None
    page_size: str | None = None


class TailorOptions(BaseModel):
    template: str | None = None
    page_size: str | None = None


class ExportRequest(TailorOptions):
    format: str = "pdf"


class PreviewRequest(TailorOptions):
    document: dict[str, Any] | None = None


class DownloadLink(BaseModel):
    """A generated file saved to the private "exports" bucket, downloadable through a short-lived signed link."""

    url: str
    file_name: str
    expires_in: int
    page_count: int | None = None
    target_pages: int | None = None


class TailorResponse(BaseModel):
    tailor_id: str
    status: str
    stage: str | None = None
    error: str | None = None
    match_id: str
    resume_id: str | None = None
    jd_title: str | None = None
    company: str | None = None
    template: str
    page_size: str
    templates: list[str] = list(TEMPLATES)
    page_sizes: list[str] = list(PAGE_SIZES)
    tailored: dict[str, Any] | None = None  # the validated AI version
    document: dict[str, Any] | None = None  # what the user has now (edits applied)
    changes_log: list[dict[str, Any]] = []
    warnings: list[str] = []
    placeholders: list[dict[str, str]] = []
    old_score: int | None = None
    new_score: int | None = None
    score_change: int | None = None
    old_breakdown: dict[str, Any] | None = None
    new_breakdown: dict[str, Any] | None = None
    page_count: int | None = None
    target_pages: int | None = None
    autofit_steps: list[str] = []
    created_at: datetime


class TailorSummary(BaseModel):
    tailor_id: str
    status: str
    match_id: str
    jd_title: str | None
    company: str | None
    candidate_name: str | None
    old_score: int | None
    new_score: int | None
    placeholders: int
    created_at: datetime


class CompareCreated(BaseModel):
    compare_id: str
    status: str


class JobAdded(BaseModel):
    compare_id: str
    job_id: str
    status: str


class CompareSummaryRow(BaseModel):
    compare_id: str
    status: str
    candidate_name: str | None
    file_name: str | None
    resume_id: str | None
    jobs: int
    done: int
    failed: int
    top_job: str | None
    top_score: int | None
    created_at: datetime


class FetchRequest(BaseModel):
    url: str


class FetchResponse(BaseModel):
    url: str
    text: str


def _tailor_response(record: TailoredRecord, matches: MatchRepository, steps: list[str] | None = None) -> TailorResponse:
    match = matches.get(record.match_id, record.user_id)
    document = record.document
    placeholders = []
    if document:
        placeholders = find_placeholders(TailoredResume.model_validate(document))
    new_result = record.new_result or {}
    return TailorResponse(
        tailor_id=record.id,
        status=record.status,
        stage=record.stage,
        error=record.error,
        match_id=record.match_id,
        resume_id=record.resume_id,
        jd_title=record.jd_title,
        company=record.company,
        template=record.template,
        page_size=record.page_size,
        tailored=record.tailored,
        document=document,
        changes_log=(record.tailored or {}).get("changes_log", []),
        warnings=record.warnings,
        placeholders=placeholders,
        old_score=record.old_score,
        new_score=record.new_score,
        score_change=(record.new_score - record.old_score) if record.new_score is not None and record.old_score is not None else None,
        old_breakdown=(match.result or {}).get("breakdown") if match else None,
        new_breakdown=new_result.get("breakdown"),
        page_count=record.page_count,
        target_pages=record.target_pages,
        autofit_steps=steps or [],
        created_at=record.created_at,
    )


def _match_response(record: MatchRecord) -> MatchResponse:
    result = record.result or {}
    return MatchResponse(
        match_id=record.id,
        status=record.status,
        stage=record.stage,
        error=record.error,
        resume_id=record.resume_id,
        file_name=record.file_name,
        candidate_name=record.candidate_name,
        score=record.score,
        label=record.label,
        breakdown=result.get("breakdown"),
        matched_skills=result.get("matched_skills", []),
        missing_skills={
            "required": result.get("missing_required_skills", []),
            "preferred": result.get("missing_preferred_skills", []),
        },
        semantic_similarity=result.get("semantic_similarity"),
        suggestions=record.suggestions,
        jd_parsed=record.jd_parsed,
        jd_text=record.jd_text,
        warnings=record.warnings,
        scoring_version=record.scoring_version,
        created_at=record.created_at,
    )


# ---------------------------------------------------------------------------
# Background job
# ---------------------------------------------------------------------------


def process_resume(pipeline: ResumePipeline, repository: ResumeRepository, record_id: str, ingested: IngestedFile) -> None:
    """Parse one resume and store the outcome. Never raises: failures are saved on the record."""
    try:
        data, raw_text = pipeline.run(ingested)
        repository.mark_done(record_id, data.model_dump(mode="json"), raw_text)
    except LLMError as exc:
        # Expected, user-fixable problems (missing key, quota...): no stack trace, clear message.
        logger.error("parsing failed", extra={"record_id": record_id, "error": str(exc)})
        repository.mark_failed(record_id, str(exc))
    except Exception as exc:  # any failure must end up in the DB, not crash the worker
        logger.exception("parsing failed", extra={"record_id": record_id})
        repository.mark_failed(record_id, f"{type(exc).__name__}: {exc}")


def _print_banner(settings: Settings) -> None:
    """Friendly startup message in the terminal."""
    url = os.getenv("RESUME_PARSER_URL", settings.app_base_url)
    mode = "multi-user (Supabase login)" if settings.auth_enabled else "single-user local mode (no login)"
    lines = ["", "✅ Resume Parser running", f"🌐 App:      {url}/app", f"📘 API docs: {url}/docs", f"🔐 Mode:     {mode}"]
    if not settings.gemini_api_key:
        lines.append("⚠️  GEMINI_API_KEY is not set - uploads will fail until you add it to .env and restart.")
    if settings.auth_enabled and not settings.supabase_service_role_key:
        lines.append("⚠️  SUPABASE_SERVICE_ROLE_KEY is not set - files are stored on this computer, not in Supabase Storage.")
    if settings.auth_enabled and settings.database_url.startswith("sqlite"):
        lines.append("⚠️  DATABASE_URL is empty - login is on but data goes to the local SQLite file, not Supabase Postgres.")
    lines.append("")
    message = "\n".join(lines)
    try:
        print(message, flush=True)
    except UnicodeEncodeError:  # very old Windows consoles cannot show emoji
        print(message.encode("ascii", "replace").decode("ascii"), flush=True)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(settings: Settings | None = None, provider: LLMProvider | None = None) -> FastAPI:
    """Build the FastAPI app. Tests pass their own settings and a fake LLM provider."""
    settings = settings or get_settings()
    settings.check_supabase()
    settings.check_production()
    init_sentry(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        setup_logging(settings.log_level)
        engine = make_engine(settings.database_url)
        init_db(engine, local_user_id=None if settings.auth_enabled else LOCAL_USER_ID)
        state = app.state
        state.settings = settings
        state.engine = engine
        state.repository = repository = ResumeRepository(engine)
        state.pipeline = ResumePipeline(settings, provider=provider, repository=repository)
        state.matches = MatchRepository(engine)
        state.match_service = MatchService(settings, state.pipeline, repository, state.matches)
        state.tailors = TailorRepository(engine)
        state.tailor_service = TailorService(settings, state.pipeline, repository, state.matches, state.tailors)
        state.comparisons = ComparisonRepository(engine)
        state.compare_service = CompareService(
            settings,
            state.pipeline,
            repository,
            state.matches,
            state.match_service,
            state.comparisons,
            concurrency=settings.compare_concurrency,
            retry_attempts=settings.compare_retry_attempts,
            retry_base_delay=settings.compare_retry_base_delay,
        )
        state.profiles = ProfileRepository(engine)
        state.accounts = AccountRepository(engine)
        state.usage = UsageService(settings, UsageRepository(engine))
        state.authenticator = Authenticator(settings, state.profiles)
        state.storage = make_storage(settings)
        state.supabase_admin = (
            SupabaseAdmin(settings.supabase_url, settings.supabase_service_role_key)
            if settings.supabase_url and settings.supabase_service_role_key
            else None
        )
        if isinstance(state.storage, SupabaseStorage):
            try:
                state.storage.ensure_buckets()
            except Exception:
                logger.exception("could not check / create the storage buckets")
        scheduler = None
        if settings.enable_scheduler:
            scheduler = start_scheduler(lambda: run_cleanup(state.profiles, state.accounts, state.storage))
        logger.info(
            "api started",
            extra={
                "database": settings.database_url.split("://")[0],
                "auth": "supabase" if settings.auth_enabled else "local",
                "storage": type(state.storage).__name__,
                "model": settings.gemini_model,
            },
        )
        _print_banner(settings)
        yield
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        engine.dispose()

    app = FastAPI(title=settings.app_name, version=settings.parser_version, lifespan=lifespan)
    app.state.settings = settings

    # Browsers may call the API only from the app's own domain (and localhost while developing).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.app_base_url, *settings.cors_origins],
        allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["Content-Disposition"],
        max_age=600,
    )
    app.add_middleware(web.SecurityHeaders, settings=settings)
    app.include_router(web.router)
    app.include_router(account.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    Auth = Depends(get_current_user)

    # ------------------------------------------------------------------ helpers

    def _owned(record: Any, what: str) -> Any:
        if record is None:
            raise HTTPException(status_code=404, detail=f"{what} not found")
        return record

    async def _ingest(upload: UploadFile) -> IngestedFile:
        data = await upload.read(settings.max_file_bytes + 1)
        try:
            return ingest_bytes(data, upload.filename or "upload.pdf", settings.max_file_bytes, None, settings.max_pdf_pages)
        except IngestError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _store_resume(request: Request, user: CurrentUser, ingested: IngestedFile) -> str:
        """Save the uploaded PDF to the private "resumes" bucket; returns its object path."""
        path = object_path(user.id, "pdf")
        try:
            request.app.state.storage.upload(RESUMES_BUCKET, path, ingested.data, "application/pdf")
        except StorageError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return path

    def _download_link(request: Request, user: CurrentUser, content: bytes, file_name: str, media_type: str, **extra: Any) -> DownloadLink:
        """Save a generated file to the private "exports" bucket and return a signed link to it."""
        storage = request.app.state.storage
        ext = file_name.rsplit(".", 1)[-1]
        path = object_path(user.id, ext)
        try:
            storage.upload(EXPORTS_BUCKET, path, content, media_type)
            url = storage.signed_url(EXPORTS_BUCKET, path, file_name)
        except StorageError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        request.app.state.usage.record(user, "export")
        return DownloadLink(url=url, file_name=file_name, expires_in=settings.signed_url_seconds, **extra)

    # ------------------------------------------------------------------ resumes

    @app.post("/resumes", response_model=UploadResponse, status_code=202)
    async def upload_resume(
        request: Request, background_tasks: BackgroundTasks, file: UploadFile = File(...), user: CurrentUser = Auth
    ) -> UploadResponse:
        """Upload a PDF. Returns immediately; poll GET /resumes/{id} for the result."""
        repository: ResumeRepository = request.app.state.repository
        ingested = await _ingest(file)
        cached = repository.find_done_by_hash(ingested.sha256, user.id)
        if cached is not None:
            return UploadResponse(id=cached.id, status=STATUS_DONE, cached=True)
        request.app.state.usage.check_and_record(user, "parse")
        path = _store_resume(request, user, ingested)
        record = repository.create_pending(ingested.file_name, ingested.sha256, user.id, path)
        background_tasks.add_task(process_resume, request.app.state.pipeline, repository, record.id, ingested)
        return UploadResponse(id=record.id, status=record.status)

    @app.get("/resumes/{resume_id}", response_model=ResumeResponse)
    def get_resume(resume_id: str, request: Request, user: CurrentUser = Auth) -> ResumeResponse:
        record: ResumeRecord = _owned(request.app.state.repository.get(resume_id, user.id), "Resume")
        return ResumeResponse(
            id=record.id, file_name=record.file_name, status=record.status, data=record.data, error=record.error,
            has_file=bool(record.storage_path),
        )

    @app.get("/resumes/{resume_id}/file", response_model=DownloadLink)
    def resume_file(resume_id: str, request: Request, user: CurrentUser = Auth) -> DownloadLink:
        """A 10-minute signed link to the original PDF."""
        record: ResumeRecord = _owned(request.app.state.repository.get(resume_id, user.id), "Resume")
        if not record.storage_path:
            raise HTTPException(status_code=404, detail="The original PDF of this resume is not stored.")
        try:
            url = request.app.state.storage.signed_url(RESUMES_BUCKET, record.storage_path, record.file_name)
        except StorageError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return DownloadLink(url=url, file_name=record.file_name, expires_in=settings.signed_url_seconds)

    @app.get("/resumes", response_model=list[ResumeSummary])
    def list_resumes(request: Request, limit: int = 100, offset: int = 0, user: CurrentUser = Auth) -> list[ResumeSummary]:
        records = request.app.state.repository.list(user.id, limit=min(limit, 500), offset=max(offset, 0))
        return [
            ResumeSummary(
                id=r.id, file_name=r.file_name, status=r.status, name=r.name, email=r.email,
                needs_review=r.needs_review, has_file=bool(r.storage_path), created_at=r.created_at,
            )
            for r in records
        ]

    # ------------------------------------------------------------------ job match

    @app.post("/match", response_model=MatchCreated, status_code=202)
    async def create_match(
        request: Request,
        background_tasks: BackgroundTasks,
        jd_text: str = Form(..., description="The job description, pasted as plain text"),
        file: UploadFile | None = File(None, description="Resume PDF (or send resume_id instead)"),
        resume_id: str | None = Form(None, description="Id of a resume parsed earlier (GET /resumes)"),
        user: CurrentUser = Auth,
    ) -> MatchCreated:
        """Score a resume against a job description. Poll GET /match/{match_id} for the result."""
        state = request.app.state
        resumes: ResumeRepository = state.repository
        matches: MatchRepository = state.matches

        jd_clean = normalize_jd_text(jd_text or "")
        if len(jd_clean) < MIN_JD_CHARS:
            raise HTTPException(status_code=400, detail=f"Paste the full job description (at least {MIN_JD_CHARS} characters).")
        if len(jd_clean) > MAX_JD_CHARS:
            raise HTTPException(status_code=400, detail=f"The job description is too long (max {MAX_JD_CHARS} characters).")
        has_file = file is not None and bool(file.filename)
        if has_file == bool(resume_id):
            raise HTTPException(status_code=400, detail="Send either a resume PDF (file) or a resume_id, not both or neither.")

        ingested: IngestedFile | None = None
        needs_parse = False
        if file is not None and has_file:
            ingested = await _ingest(file)
            sha256, file_name = ingested.sha256, ingested.file_name
            needs_parse = resumes.find_done_by_hash(sha256, user.id) is None
        else:
            resume = _owned(resumes.get(resume_id or "", user.id), "Resume")
            if resume.status == STATUS_FAILED:
                raise HTTPException(status_code=400, detail=f"That resume failed to parse: {resume.error}")
            sha256, file_name = resume.sha256, resume.file_name

        digest = jd_hash(jd_clean)
        cached = matches.find_done(sha256, digest, SCORING_VERSION, user.id)
        if cached is not None:
            return MatchCreated(match_id=cached.id, status=STATUS_DONE, cached=True)

        if needs_parse:
            state.usage.check(user, "parse")
        state.usage.check(user, "match")
        if needs_parse:
            state.usage.record(user, "parse")
        state.usage.record(user, "match")
        storage_path = _store_resume(request, user, ingested) if ingested is not None and needs_parse else None

        record = matches.create_pending(
            user_id=user.id,
            resume_id=resume_id,
            resume_sha256=sha256,
            file_name=file_name,
            jd_hash=digest,
            jd_text=jd_clean,
            scoring_version=SCORING_VERSION,
            stage="queued",
        )
        background_tasks.add_task(state.match_service.run, record.id, ingested, resume_id, storage_path)
        return MatchCreated(match_id=record.id, status=record.status)

    @app.get("/match/{match_id}", response_model=MatchResponse)
    def get_match(match_id: str, request: Request, user: CurrentUser = Auth) -> MatchResponse:
        return _match_response(_owned(request.app.state.matches.get(match_id, user.id), "Match"))

    @app.get("/matches", response_model=list[MatchSummary])
    def list_matches(request: Request, limit: int = 100, offset: int = 0, user: CurrentUser = Auth) -> list[MatchSummary]:
        records = request.app.state.matches.list(user.id, limit=min(limit, 500), offset=max(offset, 0))
        return [
            MatchSummary(
                match_id=r.id, status=r.status, score=r.score, label=r.label, jd_title=r.jd_title, jd_company=r.jd_company,
                candidate_name=r.candidate_name, file_name=r.file_name, resume_id=r.resume_id, created_at=r.created_at,
            )
            for r in records
        ]

    # ------------------------------------------------------------------ tailor my resume

    def _service(request: Request) -> TailorService:
        return request.app.state.tailor_service

    def _tailor_record(request: Request, tailor_id: str, user: CurrentUser) -> TailoredRecord:
        return _owned(request.app.state.tailors.get(tailor_id, user.id), "Tailored resume")

    def _tailor_call(fn, *args):
        """Run a service call, turning its errors into HTTP errors."""
        try:
            return fn(*args)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Tailored resume not found") from exc
        except PlaceholdersLeft as exc:
            raise HTTPException(status_code=409, detail={"message": str(exc), "placeholders": exc.placeholders}) from exc
        except TailorError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RenderError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/tailor", response_model=TailorCreated, status_code=202)
    def create_tailor(body: TailorRequest, request: Request, background_tasks: BackgroundTasks, user: CurrentUser = Auth) -> TailorCreated:
        """Tailor the match's resume to its JD. Poll GET /tailor/{tailor_id} for the result."""
        service = _service(request)
        _tailor_call(service.check_options, body.template, body.page_size)
        match = _owned(request.app.state.matches.get(body.match_id, user.id), "Match")
        if match.status != STATUS_DONE:
            raise HTTPException(status_code=400, detail="The match is not finished yet.")
        if not body.regenerate:
            existing = request.app.state.tailors.find_for_match(body.match_id)
            if existing is not None:
                return TailorCreated(tailor_id=existing.id, status=existing.status, existing=True)
        request.app.state.usage.check_and_record(user, "tailor")
        record = request.app.state.tailors.create_pending(
            user_id=user.id,
            match_id=match.id,
            resume_id=match.resume_id,
            jd_title=match.jd_title,
            company=match.jd_company,
            template=body.template,
            page_size=body.page_size,
            old_score=match.score,
            stage="queued",
        )
        background_tasks.add_task(service.run, record.id)
        return TailorCreated(tailor_id=record.id, status=record.status)

    @app.get("/tailor/{tailor_id}", response_model=TailorResponse)
    def get_tailor(tailor_id: str, request: Request, user: CurrentUser = Auth) -> TailorResponse:
        return _tailor_response(_tailor_record(request, tailor_id, user), request.app.state.matches)

    @app.put("/tailor/{tailor_id}", response_model=TailorResponse)
    def save_tailor(tailor_id: str, body: TailorEdits, request: Request, user: CurrentUser = Auth) -> TailorResponse:
        """Save the user's accepts / rejects / edits; the score is recomputed (no LLM call)."""
        _tailor_record(request, tailor_id, user)
        _tailor_call(_service(request).save_edits, tailor_id, body.document, body.template, body.page_size)
        return _tailor_response(_tailor_record(request, tailor_id, user), request.app.state.matches)

    @app.post("/tailor/{tailor_id}/preview", response_class=HTMLResponse)
    def preview_tailor(tailor_id: str, body: PreviewRequest, request: Request, user: CurrentUser = Auth) -> HTMLResponse:
        """HTML preview of a (possibly unsaved) document, with placeholders highlighted."""
        record = _tailor_record(request, tailor_id, user)
        doc = _tailor_call(_service(request).preview_document, tailor_id, body.document)
        _tailor_call(_service(request).check_options, body.template, body.page_size)
        html = render_html(doc, body.template or record.template, body.page_size or record.page_size, preview=True)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.post("/tailor/{tailor_id}/autofit", response_model=TailorResponse)
    def autofit_tailor(tailor_id: str, body: TailorOptions, request: Request, user: CurrentUser = Auth) -> TailorResponse:
        _tailor_record(request, tailor_id, user)
        steps = _tailor_call(_service(request).autofit, tailor_id, body.template, body.page_size)
        return _tailor_response(_tailor_record(request, tailor_id, user), request.app.state.matches, steps)

    @app.post("/tailor/{tailor_id}/export", response_model=DownloadLink)
    def export_tailor(tailor_id: str, body: ExportRequest, request: Request, user: CurrentUser = Auth) -> DownloadLink:
        """Render the tailored resume; returns a 10-minute download link. 409 while placeholders remain."""
        _tailor_record(request, tailor_id, user)
        exported = _tailor_call(_service(request).export, tailor_id, body.format, body.template, body.page_size)
        return _download_link(
            request, user, exported.content, exported.file_name, exported.media_type,
            page_count=exported.page_count, target_pages=exported.target_pages,
        )

    @app.get("/tailors", response_model=list[TailorSummary])
    def list_tailors(request: Request, limit: int = 100, offset: int = 0, user: CurrentUser = Auth) -> list[TailorSummary]:
        out = []
        for r in request.app.state.tailors.list(user.id, limit=min(limit, 500), offset=max(offset, 0)):
            document = r.document
            out.append(
                TailorSummary(
                    tailor_id=r.id,
                    status=r.status,
                    match_id=r.match_id,
                    jd_title=r.jd_title,
                    company=r.company,
                    candidate_name=((document or {}).get("basics") or {}).get("name"),
                    old_score=r.old_score,
                    new_score=r.new_score,
                    placeholders=len(find_placeholders(TailoredResume.model_validate(document))) if document else 0,
                    created_at=r.created_at,
                )
            )
        return out

    # ------------------------------------------------------------------ compare jobs

    async def _read_job_file(upload: UploadFile) -> tuple[str, bytes]:
        data = await upload.read(settings.max_file_bytes + 1)
        if len(data) > settings.max_file_bytes:
            raise HTTPException(status_code=400, detail=f"'{upload.filename}' is too large (max {settings.max_file_mb:g} MB).")
        return upload.filename or "job.txt", data

    def _comparison(request: Request, compare_id: str, user: CurrentUser) -> ComparisonRecord:
        return _owned(request.app.state.comparisons.get(compare_id, user.id), "Comparison")

    @app.post("/compare", response_model=CompareCreated, status_code=202)
    async def create_compare(
        request: Request,
        background_tasks: BackgroundTasks,
        jobs: str = Form(
            ...,
            description='JSON list of 2-10 jobs: [{"text" | "url" | "file_index", "label", "deadline", "location", "salary", '
            '"source_url"}]; file_index points into job_files; source_url is an optional link kept with pasted text.',
        ),
        resume_id: str | None = Form(None, description="Id of a resume parsed earlier (GET /resumes)"),
        file: UploadFile | None = File(None, description="Resume PDF (or send resume_id instead)"),
        job_files: list[UploadFile] = File(default=[], description="JD files (.txt / .pdf / .docx) referenced by file_index"),
        user: CurrentUser = Auth,
    ) -> CompareCreated:
        """Score one resume against several jobs. Poll GET /compare/{compare_id} for progress and results."""
        state = request.app.state
        resumes: ResumeRepository = state.repository

        try:
            specs = json.loads(jobs)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="jobs must be a JSON list.") from exc
        if not isinstance(specs, list) or not all(isinstance(j, dict) for j in specs):
            raise HTTPException(status_code=400, detail="jobs must be a JSON list of objects.")
        if not MIN_JOBS <= len(specs) <= MAX_JOBS:
            raise HTTPException(status_code=400, detail=f"Add between {MIN_JOBS} and {MAX_JOBS} jobs to compare.")

        files = [await _read_job_file(f) for f in job_files if f.filename]
        prepared = []
        for number, spec in enumerate(specs, start=1):
            index = spec.get("file_index")
            file_name, file_data = None, None
            if index is not None:
                if not isinstance(index, int) or not 0 <= index < len(files):
                    raise HTTPException(status_code=400, detail=f"Job {number}: file_index {index!r} does not match an uploaded file.")
                file_name, file_data = files[index]
            job = JobInput(
                text=spec.get("text"), url=spec.get("url"), file_name=file_name, file_data=file_data,
                label=spec.get("label"), deadline=spec.get("deadline"), location=spec.get("location"), salary=spec.get("salary"),
                source_url=spec.get("source_url"),
            )
            try:
                prepared.append(prepare_job(job, number))
            except CompareInputError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        has_file = file is not None and bool(file.filename)
        if has_file == bool(resume_id):
            raise HTTPException(status_code=400, detail="Send either a resume PDF (file) or a resume_id, not both or neither.")
        ingested: IngestedFile | None = None
        storage_path: str | None = None
        if file is not None and has_file:
            ingested = await _ingest(file)
            fields: dict[str, Any] = {"resume_sha256": ingested.sha256, "file_name": ingested.file_name}
            known = resumes.find_done_by_hash(ingested.sha256, user.id)
            if known is not None:  # already parsed: no need to parse it again
                fields |= {"resume_id": known.id, "candidate_name": known.name}
                ingested = None
        else:
            resume = _owned(resumes.get(resume_id or "", user.id), "Resume")
            if resume.status == STATUS_FAILED:
                raise HTTPException(status_code=400, detail=f"That resume failed to parse: {resume.error}")
            fields = {"resume_id": resume.id, "resume_sha256": resume.sha256, "file_name": resume.file_name, "candidate_name": resume.name}

        if ingested is not None:
            state.usage.check(user, "parse")
        state.usage.check(user, "compare")
        if ingested is not None:
            state.usage.record(user, "parse")
            storage_path = _store_resume(request, user, ingested)
        state.usage.record(user, "compare")

        record, _ = state.comparisons.create(prepared, stage="queued", user_id=user.id, **fields)
        background_tasks.add_task(state.compare_service.run, record.id, ingested, None, storage_path)
        return CompareCreated(compare_id=record.id, status=record.status)

    @app.get("/compare/{compare_id}")
    def get_compare(compare_id: str, request: Request, user: CurrentUser = Auth) -> dict[str, Any]:
        """Per-job status (pending / parsing / scoring / done / failed), ranking, insights and summary."""
        _comparison(request, compare_id, user)
        return request.app.state.compare_service.view(compare_id)

    @app.post("/compare/{compare_id}/jobs", response_model=JobAdded, status_code=202)
    async def add_compare_job(
        compare_id: str,
        request: Request,
        background_tasks: BackgroundTasks,
        text: str | None = Form(None),
        url: str | None = Form(None),
        source_url: str | None = Form(None, description="Where the pasted text came from (kept as a link)"),
        label: str | None = Form(None),
        deadline: str | None = Form(None),
        location: str | None = Form(None),
        salary: str | None = Form(None),
        file: UploadFile | None = File(None, description="JD file (.txt / .pdf / .docx)"),
        user: CurrentUser = Auth,
    ) -> JobAdded:
        """Add one more job to a comparison; only the new job is scored."""
        repo: ComparisonRepository = request.app.state.comparisons
        record = _comparison(request, compare_id, user)
        if not record.resume_id:
            raise HTTPException(status_code=400, detail="This comparison has no parsed resume; start a new comparison.")
        existing = repo.jobs(compare_id)
        if len(existing) >= MAX_JOBS:
            raise HTTPException(status_code=400, detail=f"A comparison can hold at most {MAX_JOBS} jobs.")
        file_name, file_data = await _read_job_file(file) if file is not None and file.filename else (None, None)
        job = JobInput(text=text, url=url, file_name=file_name, file_data=file_data,
                       label=label, deadline=deadline, location=location, salary=salary, source_url=source_url)
        try:
            fields = prepare_job(job, len(existing) + 1)
        except CompareInputError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        new_job = repo.add_job(compare_id, **fields)
        repo.update(compare_id, status=STATUS_PENDING, stage=None)
        background_tasks.add_task(request.app.state.compare_service.run, compare_id, None, [new_job.id])
        return JobAdded(compare_id=compare_id, job_id=new_job.id, status=new_job.status)

    @app.delete("/compare/{compare_id}/jobs/{job_id}")
    def delete_compare_job(compare_id: str, job_id: str, request: Request, background_tasks: BackgroundTasks, user: CurrentUser = Auth) -> dict[str, Any]:
        """Remove a job (its match stays in Job Match history). Returns the updated comparison."""
        repo: ComparisonRepository = request.app.state.comparisons
        _comparison(request, compare_id, user)
        job = repo.get_job(job_id)
        if job is None or job.comparison_id != compare_id:
            raise HTTPException(status_code=404, detail="Job not found in this comparison")
        if job.status in ACTIVE_JOB_STATUSES:
            raise HTTPException(status_code=409, detail="This job is still being scored; remove it when it has finished.")
        repo.delete_job(job_id)
        service: CompareService = request.app.state.compare_service
        if not any(j.status in ACTIVE_JOB_STATUSES for j in repo.jobs(compare_id)):
            repo.update(compare_id, status=STATUS_PENDING, stage="updating summary")
            background_tasks.add_task(service.refresh_summary, compare_id)
        return service.view(compare_id)

    @app.get("/compare/{compare_id}/export", response_model=DownloadLink)
    def export_compare(compare_id: str, request: Request, format: str = Query("csv", pattern="^(csv|pdf)$"), user: CurrentUser = Auth) -> DownloadLink:
        """The comparison as CSV or a printable PDF report; returns a 10-minute download link (503 if Chromium is missing)."""
        _comparison(request, compare_id, user)
        view = request.app.state.compare_service.view(compare_id)
        name = export_filename(view, format)
        if format == "csv":
            return _download_link(request, user, to_csv(view).encode("utf-8"), name, "text/csv; charset=utf-8")
        try:
            pdf = html_to_pdf(report_html(view), "A4", "classic")
        except RenderError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return _download_link(request, user, pdf, name, "application/pdf")

    @app.get("/compares", response_model=list[CompareSummaryRow])
    def list_compares(request: Request, limit: int = 50, offset: int = 0, user: CurrentUser = Auth) -> list[CompareSummaryRow]:
        """My comparisons, newest first."""
        service: CompareService = request.app.state.compare_service
        out = []
        for record in request.app.state.comparisons.list(user.id, limit=min(limit, 200), offset=max(offset, 0)):
            view = service.view(record.id)
            top = view["insights"]["ranking"][0] if view["insights"]["ranking"] else None
            out.append(
                CompareSummaryRow(
                    compare_id=record.id,
                    status=record.status,
                    candidate_name=record.candidate_name,
                    file_name=record.file_name,
                    resume_id=record.resume_id,
                    jobs=view["progress"]["total"],
                    done=view["progress"]["done"],
                    failed=view["progress"]["failed"],
                    top_job=top["name"] if top else None,
                    top_score=top["score"] if top else None,
                    created_at=record.created_at,
                )
            )
        return out

    @app.post("/jd/fetch", response_model=FetchResponse)
    async def fetch_jd(body: FetchRequest, request: Request, user: CurrentUser = Auth) -> FetchResponse:
        """Read the JD text from a job posting URL (LinkedIn and other login-only pages are refused)."""
        try:
            text = await request.app.state.compare_service.fetcher(body.url)
        except JDSourceError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return FetchResponse(url=body.url, text=text)

    # ------------------------------------------------------------------ status

    @app.get("/health")
    def health(request: Request) -> dict[str, Any]:
        db_ok = True
        try:
            with request.app.state.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:
            db_ok = False
        key_ok = bool(settings.gemini_api_key) or provider is not None
        return {
            "status": "ok" if db_ok else "degraded",
            "database": db_ok,
            "auth": "supabase" if settings.auth_enabled else "local",
            "storage": type(request.app.state.storage).__name__,
            "llm_provider": settings.llm_provider,
            "llm_model": settings.gemini_model,
            "llm_api_key_configured": key_ok,
            "llm_message": None if key_ok else "GEMINI_API_KEY is not set. Add it to .env and restart the server.",
            "ocr_available": is_ocr_available(settings.tesseract_cmd),
            "parser_version": settings.parser_version,
        }

    return app


# Make emoji in the startup banner safe on Windows terminals.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(errors="replace")
    except (ValueError, OSError):
        pass

app = create_app()
