"""Postgres: enable Row Level Security on every table (Supabase hardening).

Supabase publishes every table in the "public" schema through its REST API (PostgREST), reachable with
the public anon key. With RLS enabled and NO policies, the anon / authenticated roles can read or write
nothing there. The app itself connects as the table owner (DATABASE_URL), which bypasses RLS, and
enforces per-user access in code (every query filters by user_id).

No-op on SQLite.

Revision ID: 0003_row_level_security
Revises: 0002_multi_user
Create Date: 2026-10-26
"""

from __future__ import annotations

from alembic import op

revision = "0003_row_level_security"
down_revision = "0002_multi_user"
branch_labels = None
depends_on = None

TABLES = ("resumes", "matches", "tailored_resumes", "comparisons", "comparison_jobs", "profiles", "usage", "alembic_version")


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        # Belt and braces: the API roles get no table privileges at all (they exist only on Supabase).
        op.execute(
            f"""DO $$ BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                    EXECUTE 'REVOKE ALL ON TABLE "{table}" FROM anon, authenticated';
                END IF;
            END $$"""
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in TABLES:
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
