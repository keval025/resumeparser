"""Database tables and repositories.

SQLModel (SQLAlchemy underneath). DATABASE_URL picks the database:
  * local development: SQLite (the default, ``data/resumes.db``),
  * production: Supabase Postgres (``postgresql://...`` - normalised to the psycopg 3 driver).

The schema is created and upgraded by Alembic (``migrations/``); ``init_db`` runs ``alembic upgrade head``.

Multi-user: every user-owned table has a ``user_id`` (the Supabase auth user id, a UUID). Listings and
every cache lookup filter by it, and the API checks ownership before it touches a record by id
(``get(record_id, user_id)`` returns None for someone else's record -> HTTP 404).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, Column, DateTime, Index, Text, Uuid, delete, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlmodel import Field, Session, SQLModel, create_engine, select

STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

# JSON documents: JSONB on Postgres, JSON (text) on SQLite. Python None is stored as SQL NULL.
JSONType = JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")
# Supabase user ids are UUIDs: native uuid on Postgres, CHAR(32) on SQLite; Python sees "xxxxxxxx-xxxx-..." strings.
UUIDType = Uuid(as_uuid=False)
TZDateTime = DateTime(timezone=True)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex


def _obj(value: Any) -> Any:
    """JSON column value as a Python object (rows written before JSON columns may hold JSON text)."""
    if isinstance(value, str):
        return json.loads(value) if value else None
    return value


def _user_id_field() -> Any:
    return Field(default=None, sa_type=UUIDType, index=True)


def _created_at_field() -> Any:
    return Field(default_factory=_utcnow, sa_type=TZDateTime, index=True)


def _updated_at_field() -> Any:
    return Field(default_factory=_utcnow, sa_type=TZDateTime)


def _json_field() -> Any:
    return Field(default=None, sa_type=JSONType)


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

PLAN_FREE = "free"
PLAN_PRO = "pro"
RETENTION_CHOICES = (30, 90, None)  # days; None = never auto-delete


class ProfileRecord(SQLModel, table=True):
    """One row per user, created on first login."""

    __tablename__ = "profiles"

    user_id: str = Field(sa_type=UUIDType, primary_key=True)
    email: str | None = Field(default=None, index=True)
    name: str | None = None
    avatar_url: str | None = None
    plan: str = PLAN_FREE
    # "Auto-delete my resumes after": 30 / 90 days, or None = never.
    retention_days: int | None = None
    created_at: datetime = _created_at_field()
    updated_at: datetime = _updated_at_field()


class UsageRecord(SQLModel, table=True):
    """One row per metered action (parse, match, tailor, compare, export)."""

    __tablename__ = "usage"
    __table_args__ = (Index("ix_usage_user_action_created", "user_id", "action", "created_at"),)

    id: str = Field(default_factory=_new_id, primary_key=True)
    user_id: str = Field(sa_type=UUIDType, index=True)
    action: str
    created_at: datetime = _created_at_field()


# ---------------------------------------------------------------------------
# Resumes
# ---------------------------------------------------------------------------


class ResumeRecord(SQLModel, table=True):
    """One uploaded resume and its parsing result."""

    __tablename__ = "resumes"

    id: str = Field(default_factory=_new_id, primary_key=True)
    user_id: str | None = _user_id_field()
    file_name: str
    sha256: str = Field(index=True)
    storage_path: str | None = None  # "<user_id>/<uuid>.pdf" in the private "resumes" bucket
    status: str = Field(default=STATUS_PENDING, index=True)
    raw_text: str | None = Field(default=None, sa_column=Column(Text))
    parsed_json: dict | None = _json_field()
    error: str | None = Field(default=None, sa_column=Column(Text))
    # Denormalised columns so the list endpoint does not need to parse JSON.
    name: str | None = None
    email: str | None = None
    needs_review: bool | None = None
    created_at: datetime = _created_at_field()
    updated_at: datetime = _updated_at_field()

    @property
    def data(self) -> dict[str, Any] | None:
        """Parsed JSON as a dict (None until parsing is done)."""
        return _obj(self.parsed_json)


def normalize_database_url(url: str) -> str:
    """postgres:// and postgresql:// (as Supabase / Render show them) -> the psycopg 3 driver."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def make_engine(database_url: str) -> Engine:
    """Create a SQLAlchemy engine; creates the folder for SQLite files if needed."""
    database_url = normalize_database_url(database_url)
    connect_args: dict[str, Any] = {}
    kwargs: dict[str, Any] = {}
    if database_url.startswith("sqlite"):
        # FastAPI background tasks run in other threads; SQLite needs this flag for that.
        connect_args["check_same_thread"] = False
        db_path = database_url.split("///", 1)[-1]
        if db_path and db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    else:
        # Supabase's poolers (PgBouncer / Supavisor) can't use server-side prepared statements.
        connect_args["prepare_threshold"] = None
        kwargs.update(pool_pre_ping=True, pool_size=5, max_overflow=5, pool_recycle=1800)
    return create_engine(database_url, connect_args=connect_args, **kwargs)


def init_db(engine: Engine, local_user_id: str | None = None) -> None:
    """Bring the schema up to date (``alembic upgrade head``).

    ``local_user_id``: single-user local mode - rows created before accounts existed (user_id NULL)
    are given to the local user so they stay visible.
    """
    from app.migrate import upgrade

    upgrade(engine)
    if local_user_id:
        with Session(engine) as session:
            for model in (ResumeRecord, MatchRecord, TailoredRecord, ComparisonRecord, ComparisonJobRecord):
                session.execute(
                    model.__table__.update().where(model.__table__.c.user_id.is_(None)).values(user_id=local_user_id)
                )
            session.commit()


class _Repository:
    model: type[SQLModel]

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def _get(self, record_id: str, user_id: str | None) -> Any:
        """The record, or None if it does not exist or (with ``user_id``) belongs to someone else."""
        with Session(self.engine) as session:
            record = session.get(self.model, record_id)
            if record is None or (user_id is not None and record.user_id != user_id):  # type: ignore[attr-defined]
                return None
            return record

    def _list(self, user_id: str, limit: int, offset: int) -> list[Any]:
        with Session(self.engine) as session:
            statement = (
                select(self.model)
                .where(self.model.user_id == user_id)  # type: ignore[attr-defined]
                .order_by(self.model.created_at.desc())  # type: ignore[attr-defined]
                .offset(offset)
                .limit(limit)
            )
            return list(session.exec(statement).all())

    def _set(self, record_id: str, fields: dict[str, Any]) -> None:
        with Session(self.engine) as session:
            record = session.get(self.model, record_id)
            if record is None:
                raise KeyError(record_id)
            for key, value in fields.items():
                setattr(record, key, value)
            record.updated_at = _utcnow()  # type: ignore[attr-defined]
            session.add(record)
            session.commit()


class ResumeRepository(_Repository):
    """All database reads/writes for resumes, in one place."""

    model = ResumeRecord

    def create_pending(self, file_name: str, sha256: str, user_id: str | None = None, storage_path: str | None = None) -> ResumeRecord:
        """Insert a new record with status=pending and return it."""
        record = ResumeRecord(file_name=file_name, sha256=sha256, status=STATUS_PENDING, user_id=user_id, storage_path=storage_path)
        with Session(self.engine) as session:
            session.add(record)
            session.commit()
            session.refresh(record)
        return record

    def mark_done(self, record_id: str, parsed: dict[str, Any], raw_text: str) -> None:
        """Store a successful parse result."""
        basics = parsed.get("basics") or {}
        self._set(
            record_id,
            {
                "status": STATUS_DONE,
                "parsed_json": parsed,
                "raw_text": raw_text,
                "error": None,
                "name": basics.get("name"),
                "email": basics.get("email"),
                "needs_review": (parsed.get("meta") or {}).get("needs_review"),
            },
        )

    def mark_failed(self, record_id: str, error: str, raw_text: str | None = None) -> None:
        """Store a failure message."""
        fields: dict[str, Any] = {"status": STATUS_FAILED, "error": error}
        if raw_text is not None:
            fields["raw_text"] = raw_text
        self._set(record_id, fields)

    def update(self, record_id: str, **fields: Any) -> None:
        self._set(record_id, fields)

    def get(self, record_id: str, user_id: str | None = None) -> ResumeRecord | None:
        return self._get(record_id, user_id)

    def find_done_by_hash(self, sha256: str, user_id: str | None = None) -> ResumeRecord | None:
        """The most recent successfully parsed record of this user for this file hash, if any."""
        with Session(self.engine) as session:
            statement = select(ResumeRecord).where(ResumeRecord.sha256 == sha256, ResumeRecord.status == STATUS_DONE)
            if user_id is not None:
                statement = statement.where(ResumeRecord.user_id == user_id)
            return session.exec(statement.order_by(ResumeRecord.created_at.desc())).first()

    def list(self, user_id: str, limit: int = 100, offset: int = 0) -> list[ResumeRecord]:
        return self._list(user_id, limit, offset)


# ---------------------------------------------------------------------------
# Resume <-> job description matches
# ---------------------------------------------------------------------------


class MatchRecord(SQLModel, table=True):
    """One resume-vs-JD analysis: the parsed JD, the deterministic score and the suggestions."""

    __tablename__ = "matches"

    id: str = Field(default_factory=_new_id, primary_key=True)
    user_id: str | None = _user_id_field()
    status: str = Field(default=STATUS_PENDING, index=True)
    stage: str | None = None  # progress note while pending: "parsing resume", "scoring", ...
    error: str | None = Field(default=None, sa_column=Column(Text))
    resume_id: str | None = Field(default=None, index=True)
    resume_sha256: str = Field(index=True)
    jd_hash: str = Field(index=True)
    scoring_version: str
    file_name: str | None = None
    candidate_name: str | None = None
    jd_text: str = Field(sa_column=Column(Text))
    jd_title: str | None = None
    jd_company: str | None = None
    jd_parsed_json: dict | None = _json_field()
    score: int | None = None
    label: str | None = None
    # Full MatchResult: breakdown per component + matched / missing skills + semantic similarity.
    result_json: dict | None = _json_field()
    suggestions_json: dict | None = _json_field()
    warnings_json: list | None = _json_field()
    created_at: datetime = _created_at_field()
    updated_at: datetime = _updated_at_field()

    @property
    def jd_parsed(self) -> dict[str, Any] | None:
        return _obj(self.jd_parsed_json)

    @property
    def result(self) -> dict[str, Any] | None:
        return _obj(self.result_json)

    @property
    def suggestions(self) -> dict[str, Any] | None:
        return _obj(self.suggestions_json)

    @property
    def warnings(self) -> list[str]:
        return _obj(self.warnings_json) or []


class MatchRepository(_Repository):
    """All database reads/writes for matches."""

    model = MatchRecord

    def create_pending(self, **fields: Any) -> MatchRecord:
        record = MatchRecord(status=STATUS_PENDING, **fields)
        with Session(self.engine) as session:
            session.add(record)
            session.commit()
            session.refresh(record)
        return record

    def update(self, match_id: str, **fields: Any) -> None:
        self._set(match_id, fields)

    def mark_failed(self, match_id: str, error: str) -> None:
        self.update(match_id, status=STATUS_FAILED, stage=None, error=error)

    def get(self, match_id: str, user_id: str | None = None) -> MatchRecord | None:
        return self._get(match_id, user_id)

    def find_done(self, resume_sha256: str, jd_hash: str, scoring_version: str, user_id: str | None = None) -> MatchRecord | None:
        """Cache lookup: this user's finished match for the same resume file, JD text and scoring version."""
        with Session(self.engine) as session:
            statement = select(MatchRecord).where(
                MatchRecord.resume_sha256 == resume_sha256,
                MatchRecord.jd_hash == jd_hash,
                MatchRecord.scoring_version == scoring_version,
                MatchRecord.status == STATUS_DONE,
            )
            if user_id is not None:
                statement = statement.where(MatchRecord.user_id == user_id)
            return session.exec(statement.order_by(MatchRecord.created_at.desc())).first()

    def find_jd_parsed(self, jd_hash: str, user_id: str | None = None) -> dict | None:
        """Parsed JD from this user's earlier match of the same JD text (saves an LLM call, keeps it stable)."""
        with Session(self.engine) as session:
            statement = select(MatchRecord.jd_parsed_json).where(
                MatchRecord.jd_hash == jd_hash, MatchRecord.jd_parsed_json.is_not(None)  # type: ignore[union-attr]
            )
            if user_id is not None:
                statement = statement.where(MatchRecord.user_id == user_id)
            return _obj(session.exec(statement.order_by(MatchRecord.created_at.desc())).first())

    def list(self, user_id: str, limit: int = 100, offset: int = 0) -> list[MatchRecord]:
        return self._list(user_id, limit, offset)


# ---------------------------------------------------------------------------
# Tailored resumes
# ---------------------------------------------------------------------------


class TailoredRecord(SQLModel, table=True):
    """One "Tailor My Resume" run: the validated AI version, the user's edits and both scores."""

    __tablename__ = "tailored_resumes"

    id: str = Field(default_factory=_new_id, primary_key=True)
    user_id: str | None = _user_id_field()
    status: str = Field(default=STATUS_PENDING, index=True)
    stage: str | None = None
    error: str | None = Field(default=None, sa_column=Column(Text))
    resume_id: str | None = Field(default=None, index=True)
    match_id: str = Field(index=True)
    jd_title: str | None = None
    company: str | None = None
    template: str = "modern"
    page_size: str = "A4"
    tailored_json: dict | None = _json_field()  # validated AI version
    user_edits: dict | None = _json_field()  # the edited document (None = untouched)
    old_score: int | None = None
    new_score: int | None = None
    new_result_json: dict | None = _json_field()  # MatchResult of the tailored resume
    ratings_json: dict | None = _json_field()  # relevance ratings of tailored items
    warnings_json: list | None = _json_field()
    page_count: int | None = None
    target_pages: int | None = None
    created_at: datetime = _created_at_field()
    updated_at: datetime = _updated_at_field()

    @property
    def tailored(self) -> dict[str, Any] | None:
        return _obj(self.tailored_json)

    @property
    def document(self) -> dict[str, Any] | None:
        """What the user currently has: their edits, or the AI version."""
        return _obj(self.user_edits) or self.tailored

    @property
    def new_result(self) -> dict[str, Any] | None:
        return _obj(self.new_result_json)

    @property
    def ratings(self) -> dict[str, Any]:
        return _obj(self.ratings_json) or {}

    @property
    def warnings(self) -> list[str]:
        return _obj(self.warnings_json) or []


class TailorRepository(_Repository):
    """All database reads/writes for tailored resumes."""

    model = TailoredRecord

    def create_pending(self, **fields: Any) -> TailoredRecord:
        record = TailoredRecord(status=STATUS_PENDING, **fields)
        with Session(self.engine) as session:
            session.add(record)
            session.commit()
            session.refresh(record)
        return record

    def update(self, tailor_id: str, **fields: Any) -> None:
        self._set(tailor_id, fields)

    def mark_failed(self, tailor_id: str, error: str) -> None:
        self.update(tailor_id, status=STATUS_FAILED, stage=None, error=error)

    def get(self, tailor_id: str, user_id: str | None = None) -> TailoredRecord | None:
        return self._get(tailor_id, user_id)

    def find_for_match(self, match_id: str) -> TailoredRecord | None:
        """The newest tailored resume for a match that is done or still running."""
        with Session(self.engine) as session:
            statement = (
                select(TailoredRecord)
                .where(TailoredRecord.match_id == match_id, TailoredRecord.status != STATUS_FAILED)
                .order_by(TailoredRecord.created_at.desc())
            )
            return session.exec(statement).first()

    def list(self, user_id: str, limit: int = 100, offset: int = 0) -> list[TailoredRecord]:
        return self._list(user_id, limit, offset)


# ---------------------------------------------------------------------------
# Compare Jobs: one resume scored against several job descriptions
# ---------------------------------------------------------------------------

# comparison_jobs.status values, in order.
JOB_PENDING = "pending"
JOB_PARSING = "parsing"
JOB_SCORING = "scoring"
JOB_DONE = "done"
JOB_FAILED = "failed"


class ComparisonRecord(SQLModel, table=True):
    """One "Compare Jobs" run. status is pending while any job (or the summary) is still being worked on."""

    __tablename__ = "comparisons"

    id: str = Field(default_factory=_new_id, primary_key=True)
    user_id: str | None = _user_id_field()
    resume_id: str | None = Field(default=None, index=True)
    resume_sha256: str | None = Field(default=None, index=True)
    file_name: str | None = None
    candidate_name: str | None = None
    status: str = Field(default=STATUS_PENDING, index=True)
    stage: str | None = None
    error: str | None = Field(default=None, sa_column=Column(Text))
    summary: str | None = Field(default=None, sa_column=Column(Text))  # "Where to apply first and why"
    summary_key: str | None = Field(default=None, index=True)  # hash of the numbers the summary was written from
    summary_source: str | None = None  # "gemini" or "rules" (fallback when Gemini fails / invents numbers)
    created_at: datetime = _created_at_field()
    updated_at: datetime = _updated_at_field()


class ComparisonJobRecord(SQLModel, table=True):
    """One job description inside a comparison; the score itself lives in the linked ``matches`` row."""

    __tablename__ = "comparison_jobs"

    id: str = Field(default_factory=_new_id, primary_key=True)
    user_id: str | None = _user_id_field()
    comparison_id: str = Field(index=True)
    position: int = 0  # order the user added the jobs in
    match_id: str | None = Field(default=None, index=True)
    label: str | None = None
    url: str | None = None
    source: str = "text"  # text | url | file
    source_name: str | None = None  # uploaded file name
    jd_text: str | None = Field(default=None, sa_column=Column(Text))
    deadline: str | None = None  # ISO date (YYYY-MM-DD)
    location: str | None = None
    salary: str | None = None
    status: str = Field(default=JOB_PENDING, index=True)
    stage: str | None = None  # finer progress note: "fetching link", "rating experience", "retrying in 4 s"...
    error: str | None = Field(default=None, sa_column=Column(Text))
    cached: bool = False  # the match result came from the resume-hash + JD-hash cache
    created_at: datetime = _created_at_field()
    updated_at: datetime = _updated_at_field()


class ComparisonRepository(_Repository):
    """All database reads/writes for comparisons and their jobs."""

    model = ComparisonRecord

    def create(self, jobs: list[dict[str, Any]], **fields: Any) -> tuple[ComparisonRecord, list[ComparisonJobRecord]]:
        record = ComparisonRecord(status=STATUS_PENDING, **fields)
        job_records = [
            ComparisonJobRecord(comparison_id=record.id, user_id=record.user_id, position=i, **job) for i, job in enumerate(jobs)
        ]
        with Session(self.engine) as session:
            session.add(record)
            session.add_all(job_records)
            session.commit()
            session.refresh(record)
            for job in job_records:
                session.refresh(job)
        return record, job_records

    def add_job(self, comparison_id: str, **fields: Any) -> ComparisonJobRecord:
        with Session(self.engine) as session:
            parent = session.get(ComparisonRecord, comparison_id)
            last = session.exec(
                select(ComparisonJobRecord.position)
                .where(ComparisonJobRecord.comparison_id == comparison_id)
                .order_by(ComparisonJobRecord.position.desc())  # type: ignore[union-attr]
            ).first()
            job = ComparisonJobRecord(
                comparison_id=comparison_id,
                user_id=parent.user_id if parent else None,
                position=(last + 1) if last is not None else 0,
                **fields,
            )
            session.add(job)
            session.commit()
            session.refresh(job)
        return job

    def update(self, comparison_id: str, **fields: Any) -> None:
        self._set(comparison_id, fields)

    def update_job(self, job_id: str, **fields: Any) -> None:
        with Session(self.engine) as session:
            record = session.get(ComparisonJobRecord, job_id)
            if record is None:
                raise KeyError(job_id)
            for key, value in fields.items():
                setattr(record, key, value)
            record.updated_at = _utcnow()
            session.add(record)
            session.commit()

    def get(self, comparison_id: str, user_id: str | None = None) -> ComparisonRecord | None:
        return self._get(comparison_id, user_id)

    def get_job(self, job_id: str) -> ComparisonJobRecord | None:
        with Session(self.engine) as session:
            return session.get(ComparisonJobRecord, job_id)

    def jobs(self, comparison_id: str) -> list[ComparisonJobRecord]:
        with Session(self.engine) as session:
            statement = (
                select(ComparisonJobRecord)
                .where(ComparisonJobRecord.comparison_id == comparison_id)
                .order_by(ComparisonJobRecord.position)
            )
            return list(session.exec(statement).all())

    def delete_job(self, job_id: str) -> None:
        with Session(self.engine) as session:
            job = session.get(ComparisonJobRecord, job_id)
            if job is not None:
                session.delete(job)
                session.commit()

    def find_summary(self, summary_key: str, user_id: str | None = None) -> str | None:
        """A Gemini summary this user got earlier from exactly the same numbers (saves a call on re-runs)."""
        with Session(self.engine) as session:
            statement = select(ComparisonRecord.summary).where(
                ComparisonRecord.summary_key == summary_key,
                ComparisonRecord.summary.is_not(None),  # type: ignore[union-attr]
                ComparisonRecord.summary_source == "gemini",
            )
            if user_id is not None:
                statement = statement.where(ComparisonRecord.user_id == user_id)
            return session.exec(statement.order_by(ComparisonRecord.updated_at.desc())).first()

    def list(self, user_id: str, limit: int = 100, offset: int = 0) -> list[ComparisonRecord]:
        return self._list(user_id, limit, offset)


# ---------------------------------------------------------------------------
# Profiles, usage and whole-account operations
# ---------------------------------------------------------------------------

USER_TABLES = (ComparisonJobRecord, ComparisonRecord, TailoredRecord, MatchRecord, ResumeRecord)


class ProfileRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def get(self, user_id: str) -> ProfileRecord | None:
        with Session(self.engine) as session:
            return session.get(ProfileRecord, user_id)

    def ensure(self, user_id: str, email: str | None, name: str | None = None, avatar_url: str | None = None) -> ProfileRecord:
        """The user's profile, created on first login; name / avatar refreshed from the identity provider."""
        with Session(self.engine) as session:
            profile = session.get(ProfileRecord, user_id)
            if profile is None:
                profile = ProfileRecord(user_id=user_id, email=email, name=name, avatar_url=avatar_url)
            elif (profile.email, profile.name, profile.avatar_url) == (email, name or profile.name, avatar_url or profile.avatar_url):
                return profile
            else:
                profile.email = email or profile.email
                profile.name = name or profile.name
                profile.avatar_url = avatar_url or profile.avatar_url
                profile.updated_at = _utcnow()
            session.add(profile)
            try:
                session.commit()
            except Exception:  # two first requests at once: the other one created it
                session.rollback()
                existing = session.get(ProfileRecord, user_id)
                if existing is None:
                    raise
                return existing
            session.refresh(profile)
            return profile

    def update(self, user_id: str, **fields: Any) -> None:
        with Session(self.engine) as session:
            profile = session.get(ProfileRecord, user_id)
            if profile is None:
                raise KeyError(user_id)
            for key, value in fields.items():
                setattr(profile, key, value)
            profile.updated_at = _utcnow()
            session.add(profile)
            session.commit()

    def find_by_email(self, email: str) -> ProfileRecord | None:
        with Session(self.engine) as session:
            return session.exec(select(ProfileRecord).where(func.lower(ProfileRecord.email) == email.lower())).first()

    def with_retention(self) -> list[ProfileRecord]:
        with Session(self.engine) as session:
            return list(session.exec(select(ProfileRecord).where(ProfileRecord.retention_days.is_not(None))).all())  # type: ignore[union-attr]


class UsageRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def record(self, user_id: str, action: str) -> None:
        with Session(self.engine) as session:
            session.add(UsageRecord(user_id=user_id, action=action))
            session.commit()

    def counts_since(self, user_id: str, since: datetime) -> dict[str, int]:
        with Session(self.engine) as session:
            rows = session.exec(
                select(UsageRecord.action, func.count())
                .where(UsageRecord.user_id == user_id, UsageRecord.created_at >= since)
                .group_by(UsageRecord.action)
            ).all()
        return {action: count for action, count in rows}


class AccountRepository:
    """Operations on all of one user's data at once (export, delete, retention clean-up)."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def export(self, user_id: str) -> dict[str, Any]:
        """Every row the user owns, as JSON-ready dicts ("Download all my data")."""
        out: dict[str, Any] = {}
        with Session(self.engine) as session:
            profile = session.get(ProfileRecord, user_id)
            out["profile"] = profile.model_dump(mode="json") if profile else None
            for model in (ResumeRecord, MatchRecord, TailoredRecord, ComparisonRecord, ComparisonJobRecord, UsageRecord):
                rows = session.exec(select(model).where(model.user_id == user_id).order_by(model.created_at)).all()  # type: ignore[attr-defined]
                out[model.__tablename__] = [row.model_dump(mode="json") for row in rows]
        return out

    def storage_paths(self, user_id: str) -> list[str]:
        with Session(self.engine) as session:
            return [
                p
                for p in session.exec(
                    select(ResumeRecord.storage_path).where(ResumeRecord.user_id == user_id, ResumeRecord.storage_path.is_not(None))  # type: ignore[union-attr]
                ).all()
                if p
            ]

    def delete_all(self, user_id: str) -> dict[str, int]:
        """Delete every row of the user, including the profile and usage. Returns deleted counts per table."""
        counts: dict[str, int] = {}
        with Session(self.engine) as session:
            for model in (*USER_TABLES, UsageRecord, ProfileRecord):
                result = session.execute(delete(model).where(model.user_id == user_id))  # type: ignore[attr-defined]
                counts[model.__tablename__] = result.rowcount or 0
            session.commit()
        return counts

    def delete_resumes_before(self, user_id: str, cutoff: datetime) -> tuple[int, list[str]]:
        """Auto-delete: the user's resumes created before ``cutoff`` and everything derived from them.

        Returns (number of resumes deleted, their storage paths to remove from the bucket).
        """
        with Session(self.engine) as session:
            old = session.exec(
                select(ResumeRecord).where(ResumeRecord.user_id == user_id, ResumeRecord.created_at < cutoff)
            ).all()
            if not old:
                return 0, []
            resume_ids = [r.id for r in old]
            paths = [r.storage_path for r in old if r.storage_path]
            match_ids = list(session.exec(select(MatchRecord.id).where(MatchRecord.user_id == user_id, MatchRecord.resume_id.in_(resume_ids))).all())  # type: ignore[union-attr]
            comparison_ids = list(
                session.exec(select(ComparisonRecord.id).where(ComparisonRecord.user_id == user_id, ComparisonRecord.resume_id.in_(resume_ids))).all()  # type: ignore[union-attr]
            )
            session.execute(delete(ComparisonJobRecord).where(ComparisonJobRecord.comparison_id.in_(comparison_ids)))  # type: ignore[attr-defined]
            session.execute(delete(ComparisonRecord).where(ComparisonRecord.id.in_(comparison_ids)))  # type: ignore[attr-defined]
            session.execute(delete(TailoredRecord).where(TailoredRecord.user_id == user_id, TailoredRecord.match_id.in_(match_ids)))  # type: ignore[attr-defined]
            session.execute(delete(MatchRecord).where(MatchRecord.id.in_(match_ids)))  # type: ignore[attr-defined]
            session.execute(delete(ResumeRecord).where(ResumeRecord.id.in_(resume_ids)))  # type: ignore[attr-defined]
            session.commit()
        return len(resume_ids), paths
