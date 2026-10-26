"""Daily clean-up job (APScheduler, inside the app process).

1. Resume auto-delete: for every user with "Auto-delete my resumes after 30 / 90 days", delete resumes
   older than that - with their matches, tailored resumes, comparisons and stored PDFs.
2. Export files: generated PDF / DOCX / CSV files are only needed while their 10-minute download link
   is valid; anything older than EXPORT_MAX_AGE is removed from the "exports" bucket.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.db import AccountRepository, ProfileRepository
from app.storage import EXPORTS_BUCKET, RESUMES_BUCKET, Storage

logger = logging.getLogger(__name__)

EXPORT_MAX_AGE = timedelta(days=1)


def run_cleanup(profiles: ProfileRepository, accounts: AccountRepository, storage: Storage, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    report = {"users": 0, "resumes_deleted": 0, "files_deleted": 0, "exports_deleted": 0, "errors": 0}
    for profile in profiles.with_retention():
        try:
            cutoff = now - timedelta(days=int(profile.retention_days or 0))
            count, paths = accounts.delete_resumes_before(profile.user_id, cutoff)
            if paths:
                storage.delete(RESUMES_BUCKET, paths)
            report["users"] += 1
            report["resumes_deleted"] += count
            report["files_deleted"] += len(paths)
        except Exception:
            report["errors"] += 1
            logger.exception("retention clean-up failed", extra={"user_id": profile.user_id})
    try:
        for folder, _ in storage.list(EXPORTS_BUCKET, ""):
            old = [name for name, created in storage.list(EXPORTS_BUCKET, folder) if created and now - created > EXPORT_MAX_AGE]
            if old:
                storage.delete(EXPORTS_BUCKET, old)
                report["exports_deleted"] += len(old)
    except Exception:
        report["errors"] += 1
        logger.exception("export clean-up failed")
    logger.info("daily clean-up done", extra=report)
    return report


def start_scheduler(job) -> Any:
    """Run ``job`` every day at 03:15 UTC. Returns the scheduler (call ``.shutdown()`` on exit)."""
    from apscheduler.schedulers.background import BackgroundScheduler

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(job, "cron", hour=3, minute=15, id="daily-cleanup", coalesce=True, max_instances=1, misfire_grace_time=3600)
    scheduler.start()
    return scheduler
