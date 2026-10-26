"""Run the Alembic migrations from code (app startup, tests, scripts).

Command line equivalent:  alembic upgrade head   (uses DATABASE_URL from .env)

A SQLite database created before Alembic was added (tables exist, no alembic_version table) is
stamped at the baseline revision first, so its data is kept and only the newer migrations run.
"""

from __future__ import annotations

import logging

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from app.config import BASE_DIR

logger = logging.getLogger(__name__)

BASELINE = "0001_baseline"


def alembic_config() -> Config:
    config = Config(str(BASE_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BASE_DIR / "migrations"))
    return config


def upgrade(engine: Engine, revision: str = "head") -> None:
    config = alembic_config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        tables = set(inspect(connection).get_table_names())
        if "resumes" in tables and "alembic_version" not in tables:
            logger.info("stamping pre-alembic database", extra={"revision": BASELINE})
            command.stamp(config, BASELINE)
        command.upgrade(config, revision)
