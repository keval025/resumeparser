"""Usage metering and free-plan limits (per calendar month, UTC).

Limits come from config (FREE_PARSES_PER_MONTH, FREE_MATCHES_PER_MONTH, FREE_TAILORS_PER_MONTH,
FREE_COMPARISONS_PER_MONTH); plan "pro" is unlimited. ``check`` runs before any Gemini call and raises
HTTP 429 with a friendly message. Only work that would call Gemini is counted: a cached result
(same file / same resume + JD) is free.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.auth import CurrentUser
from app.config import Settings
from app.db import PLAN_PRO, UsageRepository

ACTIONS = ("parse", "match", "tailor", "compare", "export")
# action -> (singular, plural) for messages and the usage meter.
NAMES = {
    "parse": ("resume parse", "resume parses"),
    "match": ("match", "matches"),
    "tailor": ("tailored resume", "tailored resumes"),
    "compare": ("comparison", "comparisons"),
    "export": ("export", "exports"),
}


def month_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    now = now or datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start, end


class UsageService:
    def __init__(self, settings: Settings, repository: UsageRepository) -> None:
        self.settings = settings
        self.repository = repository

    def limit(self, user: CurrentUser, action: str) -> int | None:
        if user.plan == PLAN_PRO:
            return None
        return self.settings.free_limits.get(action)

    def check(self, user: CurrentUser, action: str, amount: int = 1) -> None:
        """Raise 429 if ``amount`` more ``action``s would go over the user's monthly limit."""
        limit = self.limit(user, action)
        if limit is None:
            return
        start, end = month_window()
        used = self.repository.counts_since(user.id, start).get(action, 0)
        if used + amount > limit:
            singular, plural = NAMES[action]
            used_up = f"You've used your 1 free {singular} this month." if limit == 1 else f"You've used all {limit} free {plural} this month."
            raise HTTPException(
                status_code=429,
                detail={
                    "message": f"{used_up} Your limit resets on {end:%B} 1.",
                    "action": action,
                    "used": used,
                    "limit": limit,
                    "resets_at": end.isoformat(),
                },
            )

    def record(self, user: CurrentUser, action: str) -> None:
        self.repository.record(user.id, action)

    def check_and_record(self, user: CurrentUser, action: str) -> None:
        self.check(user, action)
        self.record(user, action)

    def summary(self, user: CurrentUser) -> dict[str, Any]:
        start, end = month_window()
        counts = self.repository.counts_since(user.id, start)
        return {
            "plan": user.plan,
            "period_start": start.isoformat(),
            "resets_at": end.isoformat(),
            "actions": {
                action: {
                    "used": counts.get(action, 0),
                    "limit": self.limit(user, action),
                    "label": NAMES[action][1],
                }
                for action in ACTIONS
            },
        }
