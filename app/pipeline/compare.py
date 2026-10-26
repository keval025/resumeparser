"""Compare Jobs: score one resume against 2-10 job descriptions and rank them.

Batch processing
----------------
* Every job runs concurrently with ``asyncio``; the Gemini work (JD parsing, relevance ratings,
  suggestions) is limited to ``concurrency`` jobs at a time (default 3) by a semaphore shared by all
  comparisons, so a 10-job run does not trip Gemini's rate limits.
* Each job is an ordinary Job Match underneath (a ``matches`` row), so "View full match" and
  "Tailor resume for this job" reuse the existing flows. The match cache applies: the same resume
  hash + JD hash re-uses the stored result with no Gemini calls.
* Rate-limit / 5xx errors (``LLMError.retryable``) are retried with exponential backoff.
* Progress is stored per job (pending -> parsing -> scoring -> done | failed). A failing job
  (unreadable URL, Gemini error...) is marked failed; the others carry on.

The ranking and insights are computed in Python (``compare_insights.py``). Gemini only writes the
short "Where to apply first and why" summary from those numbers; if it fails, or uses a number that
is not in the facts, a rule-based summary is used instead.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Awaitable, Callable

from pydantic import Field

from app.config import Settings
from app.db import (
    JOB_DONE,
    JOB_FAILED,
    JOB_PARSING,
    JOB_PENDING,
    JOB_SCORING,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    ComparisonJobRecord,
    ComparisonRecord,
    ComparisonRepository,
    MatchRepository,
    ResumeRepository,
)
from app.pipeline.compare_insights import JobRow, build_insights, facts_key, rules_summary, summary_facts
from app.pipeline.ingest import IngestedFile
from app.pipeline.jd_parse import MAX_JD_CHARS, MIN_JD_CHARS, jd_hash, normalize_jd_text
from app.pipeline.jd_sources import JDSourceError, check_url, fetch_jd_from_url, jd_text_from_file
from app.pipeline.llm import LLMError, LLMProvider
from app.pipeline.llm_extract import LLMExtractionError, complete_validated
from app.pipeline.match import SCORING_VERSION
from app.pipeline.match_run import MatchError, MatchService
from app.pipeline.run import ResumePipeline
from app.pipeline.suggest import _bare, numbers_in
from app.schema import ResumeData, _Model

logger = logging.getLogger(__name__)

MIN_JOBS = 2
MAX_JOBS = 10
RESUME_WAIT_SECONDS = 180
ACTIVE_JOB_STATUSES = (JOB_PENDING, JOB_PARSING, JOB_SCORING)

# MatchService stage names -> comparison job status.
_STAGE_STATUS = {"reading job description": JOB_PARSING}

Fetcher = Callable[[str], Awaitable[str]]


class CompareInputError(ValueError):
    """Invalid comparison input; the message is shown to the user (HTTP 400)."""


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


@dataclass
class JobInput:
    """One job as submitted: exactly one of text / url / file."""

    text: str | None = None
    url: str | None = None
    file_name: str | None = None
    file_data: bytes | None = None
    label: str | None = None
    deadline: str | None = None
    location: str | None = None
    salary: str | None = None
    source_url: str | None = None  # where pasted text came from (the UI's "Fetch" preview); metadata only


def _clean(value: Any, limit: int = 120) -> str | None:
    text = " ".join(str(value).split()) if value is not None else ""
    return text[:limit] or None


def prepare_job(job: JobInput, number: int) -> dict[str, Any]:
    """Validate one job and turn it into comparison_jobs columns. Files are read now; URLs are fetched later."""
    where = f"Job {number}"
    sources = [s for s in ("text", "url", "file") if (job.file_data is not None if s == "file" else (getattr(job, s) or "").strip())]
    if len(sources) != 1:
        raise CompareInputError(f"{where}: give exactly one of: pasted text, a URL or a file.")
    fields: dict[str, Any] = {
        "label": _clean(job.label),
        "location": _clean(job.location),
        "salary": _clean(job.salary, 60),
        "deadline": None,
        "source": sources[0],
    }
    if job.deadline and str(job.deadline).strip():
        try:
            fields["deadline"] = date.fromisoformat(str(job.deadline).strip()[:10]).isoformat()
        except ValueError as exc:
            raise CompareInputError(f"{where}: the deadline must be a date like 2026-12-31.") from exc

    if sources[0] == "text":
        text = normalize_jd_text(job.text or "")
        if len(text) < MIN_JD_CHARS:
            raise CompareInputError(f"{where}: paste the full job description (at least {MIN_JD_CHARS} characters).")
        if len(text) > MAX_JD_CHARS:
            raise CompareInputError(f"{where}: the job description is too long (max {MAX_JD_CHARS} characters).")
        fields["jd_text"] = text
        if job.source_url and job.source_url.strip():
            try:
                fields["url"] = check_url(job.source_url, resolve=False)
                fields["source"] = "url"
            except JDSourceError:
                pass  # just a reference link; the text is what gets scored
    elif sources[0] == "file":
        try:
            fields["jd_text"] = jd_text_from_file(job.file_data or b"", job.file_name or "")
        except JDSourceError as exc:
            raise CompareInputError(f"{where}: {exc}") from exc
        fields["source_name"] = _clean(job.file_name, 200)
    else:
        try:
            fields["url"] = check_url(job.url or "", resolve=False)
        except JDSourceError as exc:
            raise CompareInputError(f"{where}: {exc}") from exc
    return fields


# ---------------------------------------------------------------------------
# AI summary
# ---------------------------------------------------------------------------


class CompareSummary(_Model):
    lines: list[str] = Field(min_length=3, max_length=5, description="3-5 short lines, one point per line")


SUMMARY_SYSTEM = """You are a career coach. You get the computed results of comparing ONE resume against several jobs.
Write 3-5 short lines telling the candidate where to apply first and why.

Rules:
- Use ONLY the facts given: job names, scores, labels, recommendations, effort, points to 80, deadlines,
  missing skills, common gaps and strengths. Never invent companies, skills, numbers or dates.
- Every number you write must appear in the facts.
- Line 1: the job to apply to first and the main reason (score / recommendation).
- Then the order for the rest, naming the jobs to tailor for and the ones to skip or treat as a stretch.
- Mention the 1-3 "learn these first" skills if there are any, and an upcoming deadline if relevant.
- Plain sentences, no markdown, no bullet symbols."""


_ALWAYS_OK_NUMBERS = {"80", "100", "1", "2", "3"}


def write_summary(provider: LLMProvider, facts: dict[str, Any]) -> tuple[str, str]:
    """Gemini's summary, or the rule-based one if it uses a number that is not in the facts.

    Returns ``(summary, source)`` with source "gemini" or "rules". Raises ``LLMError`` if Gemini fails.
    """
    prompt = f"Write the summary from these computed results.\n\n<facts>\n{json.dumps(facts, ensure_ascii=False)}\n</facts>"
    answer = complete_validated(provider, SUMMARY_SYSTEM, prompt, CompareSummary)
    text = "\n".join(line.strip(" -•*\t") for line in answer.lines if line.strip(" -•*\t"))
    known = {_bare(n) for n in numbers_in(json.dumps(facts, ensure_ascii=False))} | _ALWAYS_OK_NUMBERS
    invented = {_bare(n) for n in numbers_in(text)} - known
    if not text or invented:
        logger.warning("compare summary rejected", extra={"invented_numbers": sorted(invented)})
        return rules_summary(facts), "rules"
    return text, "gemini"


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class CompareService:
    """Runs comparisons in the background and builds the comparison view for the API."""

    def __init__(
        self,
        settings: Settings,
        pipeline: ResumePipeline,
        resumes: ResumeRepository,
        matches: MatchRepository,
        match_service: MatchService,
        comparisons: ComparisonRepository,
        fetcher: Fetcher | None = None,
        concurrency: int = 3,
        retry_attempts: int = 4,
        retry_base_delay: float = 2.0,
    ) -> None:
        self.settings = settings
        self.pipeline = pipeline
        self.resumes = resumes
        self.matches = matches
        self.match_service = match_service
        self.comparisons = comparisons
        self.fetcher: Fetcher = fetcher or fetch_jd_from_url
        self.concurrency = concurrency
        self.retry_attempts = retry_attempts
        self.retry_base_delay = retry_base_delay
        self._loop: asyncio.AbstractEventLoop | None = None
        self._slots: asyncio.Semaphore | None = None
        self._hash_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------ concurrency helpers

    def _gemini_slots(self) -> asyncio.Semaphore:
        """The shared semaphore (re-created if the event loop changed, e.g. between test clients)."""
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop, self._slots, self._hash_locks = loop, asyncio.Semaphore(self.concurrency), {}
        assert self._slots is not None
        return self._slots

    def _hash_lock(self, key: str) -> asyncio.Lock:
        """One lock per resume + JD pair, so the same JD twice is scored once and then cache-hit."""
        self._gemini_slots()
        return self._hash_locks.setdefault(key, asyncio.Lock())

    async def _with_retry(self, call: Callable[[], Awaitable[Any]], on_retry: Callable[[str], None]) -> Any:
        """Await ``call()``, retrying retryable Gemini errors with exponential backoff (+ jitter).

        ``on_retry`` gets a progress note like "Gemini busy - retry 2 of 4 in 4 s".
        """
        for attempt in range(1, self.retry_attempts + 1):
            try:
                return await call()
            except LLMError as exc:
                if not exc.retryable or attempt == self.retry_attempts:
                    raise
                delay = self.retry_base_delay * 2 ** (attempt - 1)
                delay += random.uniform(0, delay / 4)
                logger.warning("gemini busy; retrying", extra={"attempt": attempt, "delay_s": round(delay, 1), "error": str(exc)})
                on_retry(f"Gemini busy - retry {attempt + 1} of {self.retry_attempts} in {delay:.0f} s")
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    # ------------------------------------------------------------------ background run

    async def run(
        self,
        comparison_id: str,
        ingested: IngestedFile | None = None,
        job_ids: list[str] | None = None,
        storage_path: str | None = None,
    ) -> None:
        """Score the comparison's unfinished jobs (or only ``job_ids``), then refresh the summary. Never raises."""
        started = time.perf_counter()
        try:
            data, raw_text = await self._load_resume(comparison_id, ingested, storage_path)
        except Exception as exc:
            message = str(exc) if isinstance(exc, (LLMError, LLMExtractionError, MatchError)) else f"{type(exc).__name__}: {exc}"
            logger.error("compare resume failed", extra={"comparison_id": comparison_id, "error": message})
            for job in self.comparisons.jobs(comparison_id):
                if job.status in ACTIVE_JOB_STATUSES:
                    self.comparisons.update_job(job.id, status=JOB_FAILED, stage=None, error="The resume could not be parsed.")
            self.comparisons.update(comparison_id, status=STATUS_FAILED, stage=None, error=message)
            return

        comparison = self.comparisons.get(comparison_id)
        jobs = [
            j for j in self.comparisons.jobs(comparison_id)
            if (j.id in job_ids if job_ids is not None else j.status != JOB_DONE)
        ]
        await asyncio.gather(*(self._run_job(comparison, job, data, raw_text) for job in jobs))  # type: ignore[arg-type]
        await self.refresh_summary(comparison_id)
        logger.info(
            "comparison done",
            extra={"comparison_id": comparison_id, "jobs": len(jobs), "processing_ms": int((time.perf_counter() - started) * 1000)},
        )

    async def _load_resume(
        self, comparison_id: str, ingested: IngestedFile | None, storage_path: str | None = None
    ) -> tuple[ResumeData, str]:
        if ingested is not None:
            self.comparisons.update(comparison_id, stage="parsing resume")
            comparison = self.comparisons.get(comparison_id)
            outcome = await asyncio.to_thread(
                self.pipeline.parse_ingested, ingested, True, comparison.user_id if comparison else None, storage_path
            )
            self.comparisons.update(
                comparison_id, resume_id=outcome.record_id, candidate_name=outcome.data.basics.name, stage=None
            )
            return outcome.data, outcome.raw_text

        comparison = self.comparisons.get(comparison_id)
        if comparison is None or not comparison.resume_id:
            raise MatchError("No resume given.")
        deadline = time.monotonic() + RESUME_WAIT_SECONDS
        while True:
            record = self.resumes.get(comparison.resume_id, comparison.user_id)
            if record is None:
                raise MatchError("Resume not found.")
            if record.status == STATUS_DONE and record.data:
                self.comparisons.update(comparison_id, stage=None, candidate_name=record.name)
                return ResumeData.model_validate(record.data), record.raw_text or ""
            if record.status == STATUS_FAILED:
                raise MatchError(f"The resume could not be parsed: {record.error}")
            if time.monotonic() > deadline:
                raise MatchError("The resume is still being parsed; try again in a moment.")
            self.comparisons.update(comparison_id, stage="waiting for resume parsing")
            await asyncio.sleep(2)

    async def _run_job(self, comparison: ComparisonRecord, job: ComparisonJobRecord, data: ResumeData, raw_text: str) -> None:
        """Fetch (if URL) -> cache lookup -> parse + score under the semaphore. Never raises."""
        match_id: str | None = None
        try:
            text = job.jd_text
            if not text and job.url:
                self.comparisons.update_job(job.id, status=JOB_PARSING, stage="fetching link", error=None)
                text = normalize_jd_text(await self.fetcher(job.url))
                self.comparisons.update_job(job.id, jd_text=text)
            text = normalize_jd_text(text or "")
            if len(text) < MIN_JD_CHARS:
                raise JDSourceError(f"The job description is too short (at least {MIN_JD_CHARS} characters).")
            text = text[:MAX_JD_CHARS]
            digest = jd_hash(text)
            sha = comparison.resume_sha256 or ""

            async with self._hash_lock(f"{sha}:{digest}"):
                cached = self.matches.find_done(sha, digest, SCORING_VERSION, comparison.user_id)
                if cached is not None:
                    self.comparisons.update_job(job.id, status=JOB_DONE, stage=None, error=None, match_id=cached.id, cached=True)
                    return
                match = self.matches.create_pending(
                    user_id=comparison.user_id,
                    resume_id=comparison.resume_id,
                    resume_sha256=sha,
                    file_name=comparison.file_name,
                    candidate_name=data.basics.name,
                    jd_hash=digest,
                    jd_text=text,
                    scoring_version=SCORING_VERSION,
                    stage="queued",
                )
                match_id = match.id
                self.comparisons.update_job(job.id, match_id=match.id, status=JOB_PENDING, stage="waiting for a free Gemini slot", error=None)

                def on_stage(name: str) -> None:
                    self.comparisons.update_job(job.id, status=_STAGE_STATUS.get(name, JOB_SCORING), stage=name)

                async with self._gemini_slots():
                    self.comparisons.update_job(job.id, status=JOB_PARSING, stage="reading job description")
                    await self._with_retry(
                        lambda: asyncio.to_thread(self.match_service.analyze_and_store, match.id, data, raw_text, on_stage),
                        lambda note: self.comparisons.update_job(job.id, stage=note),
                    )
            self.comparisons.update_job(job.id, status=JOB_DONE, stage=None, error=None)
        except (JDSourceError, LLMError, LLMExtractionError, MatchError) as exc:
            logger.error("compare job failed", extra={"job_id": job.id, "error": str(exc)})
            self._fail_job(job.id, match_id, str(exc))
        except Exception as exc:  # one broken job must never stop the batch
            logger.exception("compare job failed", extra={"job_id": job.id})
            self._fail_job(job.id, match_id, f"{type(exc).__name__}: {exc}")

    def _fail_job(self, job_id: str, match_id: str | None, error: str) -> None:
        self.comparisons.update_job(job_id, status=JOB_FAILED, stage=None, error=error)
        if match_id is not None:
            self.matches.mark_failed(match_id, error)

    async def refresh_summary(self, comparison_id: str) -> None:
        """Re-write the summary if the numbers changed, then mark the comparison done. Never raises."""
        try:
            jobs = self.comparisons.jobs(comparison_id)
            if any(j.status in ACTIVE_JOB_STATUSES for j in jobs):
                return  # another run on this comparison is still working; it refreshes when it ends
            record = self.comparisons.get(comparison_id)
            if record is None:
                return
            insights = self.view(comparison_id)["insights"]
            facts = summary_facts(insights)
            key = facts_key(facts)
            fields: dict[str, Any] = {"status": STATUS_DONE, "stage": None, "error": None}
            if record.summary_key != key:
                if not insights["ranking"]:
                    fields |= {"summary": None, "summary_key": key, "summary_source": None}
                elif (cached := self.comparisons.find_summary(key, record.user_id)) is not None:
                    fields |= {"summary": cached, "summary_key": key, "summary_source": "gemini"}
                else:
                    self.comparisons.update(comparison_id, stage="writing summary")
                    try:
                        summary, source = await self._with_retry(
                            lambda: asyncio.to_thread(write_summary, self.pipeline.provider, facts),
                            lambda note: self.comparisons.update(comparison_id, stage=note),
                        )
                    except Exception as exc:  # the summary is optional: never let it block the results
                        logger.warning("compare summary fell back to rules", extra={"error": f"{type(exc).__name__}: {exc}"})
                        summary, source = rules_summary(facts), "rules"
                    fields |= {"summary": summary, "summary_key": key, "summary_source": source}
            self.comparisons.update(comparison_id, **fields)
        except Exception:
            logger.exception("compare summary failed", extra={"comparison_id": comparison_id})
            self.comparisons.update(comparison_id, status=STATUS_DONE, stage=None)

    # ------------------------------------------------------------------ views

    def view(self, comparison_id: str) -> dict[str, Any]:
        """Jobs with their status + insights computed from the finished ones."""
        record = self.comparisons.get(comparison_id)
        if record is None:
            raise KeyError(comparison_id)
        jobs_out: list[dict[str, Any]] = []
        rows: list[JobRow] = []
        for job in self.comparisons.jobs(comparison_id):
            match = self.matches.get(job.match_id) if job.match_id else None
            title = match.jd_title if match else None
            company = match.jd_company if match else None
            name = job.label or " - ".join(p for p in (title, company) if p) or f"Job {job.position + 1}"
            done = job.status == JOB_DONE and match is not None and match.status == STATUS_DONE
            jobs_out.append(
                {
                    "job_id": job.id,
                    "position": job.position,
                    "name": name,
                    "label": job.label,
                    "title": title,
                    "company": company,
                    "source": job.source,
                    "source_name": job.source_name,
                    "url": job.url,
                    "deadline": job.deadline,
                    "location": job.location,
                    "salary": job.salary,
                    "status": job.status if (done or job.status != JOB_DONE) else JOB_FAILED,
                    "stage": job.stage,
                    "error": job.error if (done or job.status != JOB_DONE) else "The match result is missing.",
                    "match_id": job.match_id,
                    "cached": job.cached,
                    "score": match.score if done else None,
                }
            )
            if done:
                result = match.result or {}
                rows.append(
                    JobRow(
                        job_id=job.id,
                        position=job.position,
                        name=name,
                        score=match.score or 0,
                        label=match.label or "",
                        title=title,
                        company=company,
                        match_id=match.id,
                        deadline=job.deadline,
                        location=job.location,
                        salary=job.salary,
                        url=job.url,
                        breakdown=result.get("breakdown") or {},
                        matched_skills=result.get("matched_skills") or [],
                        missing_required=result.get("missing_required_skills") or [],
                        missing_preferred=result.get("missing_preferred_skills") or [],
                        priorities=(match.suggestions or {}).get("top_priorities") or [],
                        jd=match.jd_parsed or {},
                    )
                )
        insights = build_insights(rows)
        facts_current = facts_key(summary_facts(insights))
        return {
            "compare_id": record.id,
            "status": record.status,
            "stage": record.stage,
            "error": record.error,
            "resume_id": record.resume_id,
            "file_name": record.file_name,
            "candidate_name": record.candidate_name,
            "jobs": jobs_out,
            "progress": {
                "total": len(jobs_out),
                "done": sum(j["status"] == JOB_DONE for j in jobs_out),
                "failed": sum(j["status"] == JOB_FAILED for j in jobs_out),
            },
            "insights": insights,
            "summary": record.summary,
            "summary_source": record.summary_source,
            "summary_stale": bool(insights["ranking"]) and record.summary_key != facts_current,
            "created_at": record.created_at,
        }
