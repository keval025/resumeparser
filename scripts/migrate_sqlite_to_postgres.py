"""One-time copy of your local SQLite data into the production database, owned by your account.

    python scripts/migrate_sqlite_to_postgres.py --email you@gmail.com

What it does:
  1. Reads data/resumes.db (read-only: the local file is never changed).
  2. Finds your user id by email: in the target database's profiles table (sign in once on the live site
     first), or else through the Supabase admin API.
  3. Runs the migrations on the target (alembic upgrade head) and copies every resume, match, tailored
     resume, comparison and comparison job, setting user_id to your account. Rows that already exist
     (same id) are skipped, so running it twice is safe.
  4. Uploads the original PDFs it can find (data/uploads/api/<sha256>.pdf) to the private "resumes" bucket.

Settings come from .env: DATABASE_URL (target), SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY (user lookup,
PDF upload). Override the target with --target. Use --dry-run to see what would be copied.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlmodel import Session  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import (  # noqa: E402
    ComparisonJobRecord,
    ComparisonRecord,
    MatchRecord,
    ProfileRepository,
    ResumeRecord,
    TailoredRecord,
    init_db,
    make_engine,
)
from app.storage import RESUMES_BUCKET, StorageError, SupabaseAdmin, make_storage, object_path  # noqa: E402

TABLES = (
    ("resumes", ResumeRecord),
    ("matches", MatchRecord),
    ("tailored_resumes", TailoredRecord),
    ("comparisons", ComparisonRecord),
    ("comparison_jobs", ComparisonJobRecord),
)
JSON_COLUMNS = {
    "parsed_json", "jd_parsed_json", "result_json", "suggestions_json", "warnings_json",
    "tailored_json", "user_edits", "new_result_json", "ratings_json",
}
BOOL_COLUMNS = {"needs_review", "cached"}


def _datetime(value: Any) -> Any:
    if isinstance(value, str) and value:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return value


def _convert(row: dict[str, Any], model: Any, user_id: str) -> dict[str, Any]:
    fields = set(model.model_fields)
    out: dict[str, Any] = {}
    for key, value in row.items():
        if key not in fields:
            continue
        if key in JSON_COLUMNS and isinstance(value, str):
            value = json.loads(value) if value else None
        elif key in BOOL_COLUMNS and value is not None:
            value = bool(value)
        elif key in ("created_at", "updated_at"):
            value = _datetime(value)
        out[key] = value
    out["user_id"] = user_id
    return out


def resolve_user(email: str, target_engine, settings) -> str:
    profile = ProfileRepository(target_engine).find_by_email(email)
    if profile is not None:
        return profile.user_id
    if settings.supabase_url and settings.supabase_service_role_key:
        user_id = SupabaseAdmin(settings.supabase_url, settings.supabase_service_role_key).find_user_id(email)
        if user_id:
            return user_id
    raise SystemExit(
        f"No account found for {email}. Sign in once on the live site with that email (Google or magic link), then run this again."
    )


def migrate(sqlite_path: Path, target_url: str, email: str | None, user_id: str | None, uploads: Path, dry_run: bool, storage=None) -> dict[str, Any]:
    settings = get_settings()
    if not sqlite_path.is_file():
        raise SystemExit(f"SQLite database not found: {sqlite_path}")
    source = sqlite3.connect(f"file:{sqlite_path.as_posix()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    existing_tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    target = make_engine(target_url)
    init_db(target)
    if user_id is None:
        if not email:
            raise SystemExit("Give --email (or --user-id).")
        user_id = resolve_user(email, target, settings)
    if storage is None and not dry_run:
        storage = make_storage(settings)

    report: dict[str, Any] = {"user_id": user_id, "copied": {}, "skipped": {}, "pdfs_uploaded": 0, "pdfs_missing": 0}
    with Session(target) as session:
        for table, model in TABLES:
            copied = skipped = 0
            if table not in existing_tables:
                report["copied"][table] = report["skipped"][table] = 0
                continue
            for raw in source.execute(f"SELECT * FROM {table}"):  # noqa: S608 - fixed table names
                row = dict(raw)
                if session.get(model, row["id"]) is not None:
                    skipped += 1
                    continue
                fields = _convert(row, model, user_id)
                if table == "resumes" and not fields.get("storage_path"):
                    pdf = uploads / f"{row['sha256']}.pdf"
                    if pdf.is_file():
                        if not dry_run:
                            path = object_path(user_id, "pdf")
                            try:
                                storage.upload(RESUMES_BUCKET, path, pdf.read_bytes(), "application/pdf")
                                fields["storage_path"] = path
                                report["pdfs_uploaded"] += 1
                            except StorageError as exc:
                                print(f"  could not upload the PDF of resume {row['id']}: {exc}")
                    else:
                        report["pdfs_missing"] += 1
                if not dry_run:
                    session.add(model(**fields))
                copied += 1
            report["copied"][table] = copied
            report["skipped"][table] = skipped
        if dry_run:
            session.rollback()
        else:
            session.commit()
    source.close()
    target.dispose()
    return report


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Copy local SQLite data to the production database for one account.")
    parser.add_argument("--email", help="the e-mail address you sign in with on the live site")
    parser.add_argument("--user-id", help="the Supabase user id (instead of --email)")
    parser.add_argument("--sqlite", type=Path, default=ROOT / "data" / "resumes.db")
    parser.add_argument("--target", default=settings.database_url, help="target DATABASE_URL (default: from .env)")
    parser.add_argument("--uploads", type=Path, default=settings.upload_dir, help="folder with the original PDFs (<sha256>.pdf)")
    parser.add_argument("--dry-run", action="store_true", help="only count what would be copied")
    args = parser.parse_args(argv)

    if args.target.startswith("sqlite") and Path(args.target.split("///", 1)[-1]).resolve() == args.sqlite.resolve():
        raise SystemExit("The target is the same SQLite file. Set DATABASE_URL to your Supabase Postgres URL (or pass --target).")
    report = migrate(args.sqlite, args.target, args.email, args.user_id, args.uploads, args.dry_run)
    print(("DRY RUN - nothing written\n" if args.dry_run else "") + f"Account: {report['user_id']}")
    for table, count in report["copied"].items():
        print(f"  {table:18} copied {count:4}   already there {report['skipped'][table]:4}")
    print(f"  PDFs uploaded: {report['pdfs_uploaded']}   PDFs not found locally: {report['pdfs_missing']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
