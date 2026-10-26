"""Alembic migrations and the one-time SQLite -> Postgres data copy.

Run against Postgres too by setting TEST_DATABASE_URL (CI does).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlmodel import SQLModel

from app.config import Settings
from app.db import MatchRepository, ProfileRepository, ResumeRepository, init_db, make_engine
from app.migrate import upgrade
from app.storage import LocalStorage
from scripts.migrate_sqlite_to_postgres import migrate
from tests.sample_resume import MOCK_EXTRACTION

USER_TABLES = {"resumes", "matches", "tailored_resumes", "comparisons", "comparison_jobs"}


def test_upgrade_downgrade_upgrade(settings: Settings) -> None:
    engine = make_engine(settings.database_url)
    upgrade(engine)
    tables = set(inspect(engine).get_table_names())
    assert USER_TABLES | {"profiles", "usage", "alembic_version"} <= tables
    for table in USER_TABLES:
        columns = {c["name"] for c in inspect(engine).get_columns(table)}
        indexes = {i["name"] for i in inspect(engine).get_indexes(table)}
        assert "user_id" in columns and f"ix_{table}_user_id" in indexes and f"ix_{table}_created_at" in indexes
    upgrade_to_base(engine)
    assert not (USER_TABLES & set(inspect(engine).get_table_names()))
    upgrade(engine)
    assert "profiles" in inspect(engine).get_table_names()
    engine.dispose()


def upgrade_to_base(engine) -> None:
    from alembic import command

    from app.migrate import alembic_config

    config = alembic_config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "base")


def test_models_match_the_migrations(settings: Settings) -> None:
    """Nobody changed a model without writing a migration (types compared on Postgres only)."""
    engine = make_engine(settings.database_url)
    upgrade(engine)
    with engine.connect() as connection:
        postgres = connection.dialect.name == "postgresql"
        context = MigrationContext.configure(connection, opts={"compare_type": postgres})
        diff = compare_metadata(context, SQLModel.metadata)
    assert diff == [], diff
    if postgres:
        with engine.connect() as connection:
            kinds = dict(connection.execute(text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = 'matches' AND column_name IN ('result_json', 'created_at', 'user_id')"
            )).all())
        assert kinds == {"result_json": "jsonb", "created_at": "timestamp with time zone", "user_id": "uuid"}
        # Supabase hardening: RLS on, so the public anon key can't read any table through the REST API.
        with engine.connect() as connection:
            without_rls = connection.execute(text(
                "SELECT relname FROM pg_class WHERE relnamespace = current_schema()::regnamespace "
                "AND relkind = 'r' AND NOT relrowsecurity"
            )).scalars().all()
        assert without_rls == []
    engine.dispose()


def _legacy_sqlite(path: Path) -> None:
    """A database as the single-user app created it: no alembic_version, JSON stored as text, no user_id."""
    engine = make_engine(f"sqlite:///{path.as_posix()}")
    upgrade(engine, "0001_baseline")
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE alembic_version"))
        parsed = json.dumps({**MOCK_EXTRACTION, "meta": {"needs_review": False}})
        conn.execute(text(
            "INSERT INTO resumes (id, file_name, sha256, status, raw_text, parsed_json, name, created_at, updated_at) "
            "VALUES ('r1', 'cv.pdf', 'abc123', 'done', 'raw text', :parsed, 'Priya Sharma', '2026-10-20 10:00:00.000000', '2026-10-20 10:00:00.000000')"
        ), {"parsed": parsed})
        conn.execute(text(
            "INSERT INTO matches (id, status, resume_id, resume_sha256, jd_hash, scoring_version, jd_text, score, result_json, "
            "warnings_json, created_at, updated_at) VALUES ('m1', 'done', 'r1', 'abc123', 'h', '1.0', 'jd', 71, :result, '[\"w\"]', "
            "'2026-10-21 10:00:00', '2026-10-21 10:00:00')"
        ), {"result": json.dumps({"breakdown": {}, "score": 71})})
    engine.dispose()


def test_existing_local_database_is_stamped_and_upgraded(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    _legacy_sqlite(path)
    engine = make_engine(f"sqlite:///{path.as_posix()}")
    local = "00000000-0000-4000-8000-000000000001"
    init_db(engine, local_user_id=local)
    resumes = ResumeRepository(engine).list(local)
    assert [r.id for r in resumes] == ["r1"] and resumes[0].data["basics"]["name"] == "Priya Sharma"
    match = MatchRepository(engine).get("m1", local)
    assert match.result == {"breakdown": {}, "score": 71} and match.warnings == ["w"]
    engine.dispose()


def test_copy_local_data_to_the_production_database(tmp_path: Path, settings: Settings) -> None:
    source = tmp_path / "legacy.db"
    _legacy_sqlite(source)
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "abc123.pdf").write_bytes(b"%PDF-1.4 original")

    target = make_engine(settings.database_url)
    init_db(target)
    me = "33333333-3333-4333-8333-333333333333"
    ProfileRepository(target).ensure(me, "Keval@Example.com")
    storage = LocalStorage(tmp_path / "storage")

    report = migrate(source, settings.database_url, "keval@example.com", None, uploads, dry_run=False, storage=storage)
    assert report["user_id"] == me
    assert report["copied"]["resumes"] == 1 and report["copied"]["matches"] == 1 and report["pdfs_uploaded"] == 1

    resume = ResumeRepository(target).get("r1", me)
    assert resume is not None and resume.data["basics"]["name"] == "Priya Sharma" and resume.created_at.year == 2026
    assert storage.download("resumes", resume.storage_path) == b"%PDF-1.4 original"
    assert MatchRepository(target).get("m1", me).result["score"] == 71

    again = migrate(source, settings.database_url, "keval@example.com", None, uploads, dry_run=False, storage=storage)
    assert again["copied"]["resumes"] == 0 and again["skipped"]["resumes"] == 1  # safe to re-run
    assert sqlite3.connect(source).execute("SELECT COUNT(*) FROM resumes").fetchone()[0] == 1  # source untouched
    target.dispose()
