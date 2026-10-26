"""Shared pytest fixtures."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app.config import Settings
from app.db import normalize_database_url
from app.pipeline.extract import TextBlock
from tests.sample_resume import build_sample_pdf_bytes


@pytest.fixture
def database_url(tmp_path: Path) -> Iterator[str]:
    """A fresh, empty database per test.

    SQLite in a temp folder by default. With TEST_DATABASE_URL=postgresql://... (CI) every test gets
    its own Postgres schema, dropped afterwards, so the whole suite also runs against real Postgres.
    """
    postgres = os.getenv("TEST_DATABASE_URL")
    if not postgres:
        yield f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
        return
    schema = f"t_{uuid.uuid4().hex[:16]}"
    admin = create_engine(normalize_database_url(postgres))
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    yield f"{postgres}{'&' if '?' in postgres else '?'}options=-csearch_path%3D{schema}"
    with admin.begin() as conn:
        conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()


@pytest.fixture
def settings(tmp_path: Path, database_url: str) -> Settings:
    """Settings that write everything into a temporary folder (never touches real data/)."""
    return replace(
        Settings(),
        gemini_api_key=None,
        database_url=database_url,
        upload_dir=tmp_path / "uploads",
        output_dir=tmp_path / "output",
        storage_dir=tmp_path / "storage",
        llm_single_call_max_chars=12000,
        # Single-user local mode unless a test opts in to Supabase auth (never the real project from .env).
        app_env="test",
        supabase_url=None,
        supabase_anon_key=None,
        supabase_service_role_key=None,
        supabase_jwt_secret=None,
        sentry_dsn=None,
        enable_scheduler=False,
        rate_limit_heavy="1000/minute",
        rate_limit_default="1000/minute",
    )


@pytest.fixture(scope="session")
def sample_pdf_bytes() -> bytes:
    return build_sample_pdf_bytes()


def make_block(
    text: str,
    x0: float = 50,
    y0: float = 100,
    x1: float | None = None,
    size: float = 10,
    bold: bool = False,
    page: int = 0,
    source: str = "text",
) -> TextBlock:
    """Build a TextBlock for unit tests (width ~5.5pt per character when x1 not given)."""
    return TextBlock(
        text=text,
        x0=x0,
        y0=y0,
        x1=x1 if x1 is not None else x0 + 5.5 * len(text),
        y1=y0 + size * 1.2,
        font_size=size,
        is_bold=bold,
        page=page,
        page_width=595,
        page_height=842,
        source=source,
    )
