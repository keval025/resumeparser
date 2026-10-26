"""Baseline: the single-user schema (resumes, matches, tailored_resumes, comparisons, comparison_jobs).

Exactly what the app created with SQLModel.create_all before Alembic was added. Existing SQLite
databases are stamped at this revision instead of running it (see app/migrate.py).

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False)]


def _indexes(table: str, *columns: str) -> None:
    for column in columns:
        op.create_index(f"ix_{table}_{column}", table, [column])


def upgrade() -> None:
    op.create_table(
        "resumes",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("file_name", sa.String(), nullable=False),
        sa.Column("sha256", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("raw_text", sa.Text()),
        sa.Column("parsed_json", sa.Text()),
        sa.Column("error", sa.Text()),
        sa.Column("name", sa.String()),
        sa.Column("email", sa.String()),
        sa.Column("needs_review", sa.Boolean()),
        *_timestamps(),
    )
    _indexes("resumes", "sha256", "status")

    op.create_table(
        "matches",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("stage", sa.String()),
        sa.Column("error", sa.Text()),
        sa.Column("resume_id", sa.String()),
        sa.Column("resume_sha256", sa.String(), nullable=False),
        sa.Column("jd_hash", sa.String(), nullable=False),
        sa.Column("scoring_version", sa.String(), nullable=False),
        sa.Column("file_name", sa.String()),
        sa.Column("candidate_name", sa.String()),
        sa.Column("jd_text", sa.Text()),
        sa.Column("jd_title", sa.String()),
        sa.Column("jd_company", sa.String()),
        sa.Column("jd_parsed_json", sa.Text()),
        sa.Column("score", sa.Integer()),
        sa.Column("label", sa.String()),
        sa.Column("result_json", sa.Text()),
        sa.Column("suggestions_json", sa.Text()),
        sa.Column("warnings_json", sa.Text()),
        *_timestamps(),
    )
    _indexes("matches", "jd_hash", "resume_id", "resume_sha256", "status")

    op.create_table(
        "tailored_resumes",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("stage", sa.String()),
        sa.Column("error", sa.Text()),
        sa.Column("resume_id", sa.String()),
        sa.Column("match_id", sa.String(), nullable=False),
        sa.Column("jd_title", sa.String()),
        sa.Column("company", sa.String()),
        sa.Column("template", sa.String(), nullable=False),
        sa.Column("page_size", sa.String(), nullable=False),
        sa.Column("tailored_json", sa.Text()),
        sa.Column("user_edits", sa.Text()),
        sa.Column("old_score", sa.Integer()),
        sa.Column("new_score", sa.Integer()),
        sa.Column("new_result_json", sa.Text()),
        sa.Column("ratings_json", sa.Text()),
        sa.Column("warnings_json", sa.Text()),
        sa.Column("page_count", sa.Integer()),
        sa.Column("target_pages", sa.Integer()),
        *_timestamps(),
    )
    _indexes("tailored_resumes", "match_id", "resume_id", "status")

    op.create_table(
        "comparisons",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("resume_id", sa.String()),
        sa.Column("resume_sha256", sa.String()),
        sa.Column("file_name", sa.String()),
        sa.Column("candidate_name", sa.String()),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("stage", sa.String()),
        sa.Column("error", sa.Text()),
        sa.Column("summary", sa.Text()),
        sa.Column("summary_key", sa.String()),
        sa.Column("summary_source", sa.String()),
        *_timestamps(),
    )
    _indexes("comparisons", "resume_id", "resume_sha256", "status", "summary_key")

    op.create_table(
        "comparison_jobs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("comparison_id", sa.String(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("match_id", sa.String()),
        sa.Column("label", sa.String()),
        sa.Column("url", sa.String()),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("source_name", sa.String()),
        sa.Column("jd_text", sa.Text()),
        sa.Column("deadline", sa.String()),
        sa.Column("location", sa.String()),
        sa.Column("salary", sa.String()),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("stage", sa.String()),
        sa.Column("error", sa.Text()),
        sa.Column("cached", sa.Boolean(), nullable=False),
        *_timestamps(),
    )
    _indexes("comparison_jobs", "comparison_id", "match_id", "status")


def downgrade() -> None:
    for table in ("comparison_jobs", "comparisons", "tailored_resumes", "matches", "resumes"):
        op.drop_table(table)
