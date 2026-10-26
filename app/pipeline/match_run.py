"""Match orchestration: resume (PDF or saved id) + JD text -> stored score, breakdown and suggestions.

Stages (each recorded in ``matches.stage`` so the UI can show progress):
  parsing resume -> reading job description -> rating experience -> scoring -> writing suggestions

Re-use keeps results stable and saves Gemini calls:
  * the resume goes through ``ResumePipeline.parse_ingested`` (file-hash cache),
  * a JD already parsed for an earlier match is re-used (JD-hash cache),
  * the API returns a finished match for the same resume hash + JD hash without re-running anything.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from app.config import Settings
from app.db import STATUS_DONE, STATUS_FAILED, STATUS_PENDING, MatchRepository, ResumeRepository
from app.match_schema import JobDescription, MatchResult, Suggestions
from app.pipeline.ingest import IngestedFile
from app.pipeline.jd_parse import clean_jd, parse_jd
from app.pipeline.llm import LLMError
from app.pipeline.llm_extract import LLMExtractionError
from app.pipeline.match import compute_match, rate_relevance, resume_text, semantic_similarity
from app.pipeline.run import ResumePipeline
from app.pipeline.suggest import suggest
from app.schema import ResumeData

logger = logging.getLogger(__name__)

RESUME_WAIT_SECONDS = 180  # how long a match waits for a resume that is still being parsed


class MatchError(RuntimeError):
    """A user-facing problem with the match inputs (resume failed to parse, etc.)."""


@dataclass
class MatchAnalysis:
    jd: JobDescription
    result: MatchResult
    suggestions: Suggestions
    ratings: dict[str, dict]
    warnings: list[str] = field(default_factory=list)


class MatchService:
    """Runs and stores matches. Create once; shares the LLM provider with the resume pipeline."""

    def __init__(
        self,
        settings: Settings,
        pipeline: ResumePipeline,
        resumes: ResumeRepository,
        matches: MatchRepository,
    ) -> None:
        self.settings = settings
        self.pipeline = pipeline
        self.resumes = resumes
        self.matches = matches

    # ------------------------------------------------------------------ pure analysis

    def analyze(self, data: ResumeData, raw_text: str, jd_text: str, jd: JobDescription | None = None, on_stage=None) -> MatchAnalysis:
        """Parse the JD (unless given), rate relevance, score, and write suggestions."""
        s = self.settings
        stage = on_stage or (lambda name: None)
        provider = self.pipeline.provider

        if jd is None:
            stage("reading job description")
            jd = parse_jd(provider, jd_text, s.normalization_file)
        stage("rating experience")
        ratings = rate_relevance(provider, data, jd)
        similarity = None
        if s.match_semantic_similarity:
            similarity = semantic_similarity(provider, resume_text(data, raw_text), jd_text, s.gemini_embedding_model)

        stage("scoring")
        result = compute_match(data, raw_text, jd, ratings, s.normalization_file, s.match_fuzzy_threshold, similarity)

        stage("writing suggestions")
        suggestions, warnings = suggest(provider, data, raw_text, jd, result)
        return MatchAnalysis(jd=jd, result=result, suggestions=suggestions, ratings=ratings, warnings=warnings)

    # ------------------------------------------------------------------ background job

    def run(
        self, match_id: str, ingested: IngestedFile | None = None, resume_id: str | None = None, storage_path: str | None = None
    ) -> None:
        """Process one pending match and store the outcome. Never raises."""
        started = time.perf_counter()
        record = self.matches.get(match_id)
        if record is None:
            return
        try:
            data, raw_text, resume_id = self._load_resume(match_id, ingested, resume_id, record.user_id, storage_path)
            self.matches.update(match_id, resume_id=resume_id, candidate_name=data.basics.name)
            analysis = self.analyze_and_store(match_id, data, raw_text)
            logger.info(
                "match done",
                extra={
                    "match_id": match_id,
                    "score": analysis.result.score,
                    "processing_ms": int((time.perf_counter() - started) * 1000),
                },
            )
        except (LLMError, LLMExtractionError, MatchError) as exc:
            logger.error("match failed", extra={"match_id": match_id, "error": str(exc)})
            self.matches.mark_failed(match_id, str(exc))
        except Exception as exc:  # any failure must end up in the DB, not crash the worker
            logger.exception("match failed", extra={"match_id": match_id})
            self.matches.mark_failed(match_id, f"{type(exc).__name__}: {exc}")

    def analyze_and_store(self, match_id: str, data: ResumeData, raw_text: str, on_stage=None) -> MatchAnalysis:
        """Analyze a pending match for an already-parsed resume and save the result. Raises on failure.

        Also used by Compare Jobs, which adds its own retries and progress reporting via ``on_stage``.
        """
        record = self.matches.get(match_id)
        if record is None:
            raise MatchError("Match not found.")
        cached_jd = self.matches.find_jd_parsed(record.jd_hash, record.user_id)
        # Re-clean a cached JD so alias-map / cleaning improvements apply to it too.
        jd = clean_jd(JobDescription.model_validate(cached_jd), self.settings.normalization_file) if cached_jd else None

        def stage(name: str) -> None:
            self.matches.update(match_id, stage=name)
            if on_stage is not None:
                on_stage(name)

        analysis = self.analyze(data, raw_text, record.jd_text, jd, on_stage=stage)
        self.matches.update(
            match_id,
            status=STATUS_DONE,
            stage=None,
            error=None,
            jd_title=analysis.jd.title,
            jd_company=analysis.jd.company,
            jd_parsed_json=analysis.jd.model_dump(mode="json"),
            score=analysis.result.score,
            label=analysis.result.label,
            result_json=analysis.result.model_dump(mode="json"),
            suggestions_json=analysis.suggestions.model_dump(mode="json"),
            warnings_json=analysis.warnings,
        )
        return analysis

    def _load_resume(
        self,
        match_id: str,
        ingested: IngestedFile | None,
        resume_id: str | None,
        user_id: str | None = None,
        storage_path: str | None = None,
    ) -> tuple[ResumeData, str, str]:
        """The parsed resume for this match: parse the upload, or load (and if needed wait for) a saved one."""
        if ingested is not None:
            self.matches.update(match_id, stage="parsing resume")
            outcome = self.pipeline.parse_ingested(ingested, user_id=user_id, storage_path=storage_path)
            if outcome.record_id is None:
                raise MatchError("The resume was parsed but could not be saved.")
            return outcome.data, outcome.raw_text, outcome.record_id

        if resume_id is None:
            raise MatchError("No resume given.")
        deadline = time.monotonic() + RESUME_WAIT_SECONDS
        while True:
            record = self.resumes.get(resume_id, user_id)
            if record is None:
                raise MatchError("Resume not found.")
            if record.status == STATUS_DONE and record.data:
                return ResumeData.model_validate(record.data), record.raw_text or "", record.id
            if record.status == STATUS_FAILED:
                raise MatchError(f"The resume could not be parsed: {record.error}")
            if record.status != STATUS_PENDING or time.monotonic() > deadline:
                raise MatchError("The resume is still being parsed; try again in a moment.")
            self.matches.update(match_id, stage="waiting for resume parsing")
            time.sleep(2)
