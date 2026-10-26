"""Multi-user: user_id on every user-owned table, profiles, usage, storage paths, JSONB and timestamptz.

* user_id (UUID, indexed) on resumes, matches, tailored_resumes, comparisons, comparison_jobs
* created_at indexes on the same tables
* resumes.storage_path (object path in the private "resumes" bucket)
* profiles (one row per user) and usage (one row per metered action)
* Postgres only: JSON text columns -> JSONB, timestamps -> timestamptz (SQLite has no such types)

Revision ID: 0002_multi_user
Revises: 0001_baseline
Create Date: 2026-10-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_multi_user"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

USER_TABLES = ("resumes", "matches", "tailored_resumes", "comparisons", "comparison_jobs")
JSON_COLUMNS = {
    "resumes": ("parsed_json",),
    "matches": ("jd_parsed_json", "result_json", "suggestions_json", "warnings_json"),
    "tailored_resumes": ("tailored_json", "user_edits", "new_result_json", "ratings_json", "warnings_json"),
}


def upgrade() -> None:
    postgres = op.get_bind().dialect.name == "postgresql"

    for table in USER_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("user_id", sa.Uuid(as_uuid=False), nullable=True))
            if table == "resumes":
                batch.add_column(sa.Column("storage_path", sa.String(), nullable=True))
        op.create_index(f"ix_{table}_user_id", table, ["user_id"])
        op.create_index(f"ix_{table}_created_at", table, ["created_at"])

    if postgres:
        for table, columns in JSON_COLUMNS.items():
            for column in columns:
                op.alter_column(
                    table, column, type_=postgresql.JSONB(), postgresql_using=f"NULLIF({column}, '')::jsonb"
                )
        for table in USER_TABLES:
            for column in ("created_at", "updated_at"):
                op.alter_column(
                    table, column, type_=sa.DateTime(timezone=True), postgresql_using=f"{column} AT TIME ZONE 'UTC'"
                )

    op.create_table(
        "profiles",
        sa.Column("user_id", sa.Uuid(as_uuid=False), primary_key=True),
        sa.Column("email", sa.String()),
        sa.Column("name", sa.String()),
        sa.Column("avatar_url", sa.String()),
        sa.Column("plan", sa.String(), nullable=False, server_default="free"),
        sa.Column("retention_days", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_profiles_email", "profiles", ["email"])
    op.create_index("ix_profiles_created_at", "profiles", ["created_at"])

    op.create_table(
        "usage",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_usage_user_id", "usage", ["user_id"])
    op.create_index("ix_usage_created_at", "usage", ["created_at"])
    op.create_index("ix_usage_user_action_created", "usage", ["user_id", "action", "created_at"])


def downgrade() -> None:
    postgres = op.get_bind().dialect.name == "postgresql"
    op.drop_table("usage")
    op.drop_table("profiles")
    if postgres:
        for table, columns in JSON_COLUMNS.items():
            for column in columns:
                op.alter_column(table, column, type_=sa.Text(), postgresql_using=f"{column}::text")
        for table in USER_TABLES:
            for column in ("created_at", "updated_at"):
                op.alter_column(table, column, type_=sa.DateTime(), postgresql_using=f"{column} AT TIME ZONE 'UTC'")
    for table in USER_TABLES:
        op.drop_index(f"ix_{table}_created_at", table)
        op.drop_index(f"ix_{table}_user_id", table)
        with op.batch_alter_table(table) as batch:
            if table == "resumes":
                batch.drop_column("storage_path")
            batch.drop_column("user_id")
