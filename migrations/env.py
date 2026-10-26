"""Alembic environment: uses the app's engine settings and SQLModel metadata."""

from __future__ import annotations

from alembic import context
from sqlmodel import SQLModel

import app.db  # noqa: F401 - registers every table on SQLModel.metadata
from app.config import get_settings
from app.db import make_engine

config = context.config
target_metadata = SQLModel.metadata


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=connection.dialect.name == "sqlite",  # SQLite can't ALTER most things
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:  # called from app.migrate.upgrade()
        _run(connection)
        return
    engine = make_engine(get_settings().database_url)
    with engine.connect() as connection:
        _run(connection)
        connection.commit()


if context.is_offline_mode():
    raise SystemExit("Offline (--sql) mode is not supported; run against a database.")
run_migrations_online()
