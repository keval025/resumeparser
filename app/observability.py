"""Error tracking with Sentry (optional: only when SENTRY_DSN is set).

Privacy: no personal data is sent. ``send_default_pii`` is off, request bodies (resume PDFs, job
descriptions) and headers (tokens) are dropped, and only the user id is attached to an error.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import Settings

logger = logging.getLogger(__name__)

_DROP_REQUEST_KEYS = ("data", "cookies", "headers", "query_string", "env")


def scrub_event(event: dict[str, Any], hint: Any = None) -> dict[str, Any]:
    request = event.get("request")
    if isinstance(request, dict):
        for key in _DROP_REQUEST_KEYS:
            request.pop(key, None)
    user = event.get("user")
    if isinstance(user, dict):
        event["user"] = {"id": user["id"]} if user.get("id") else {}
    for crumb in (event.get("breadcrumbs") or {}).get("values", []):
        crumb.pop("data", None)
    return event


def init_sentry(settings: Settings) -> bool:
    if not settings.sentry_dsn:
        return False
    try:
        import sentry_sdk
    except ImportError:
        logger.warning("SENTRY_DSN is set but sentry-sdk is not installed")
        return False
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.app_env,
        release=settings.parser_version,
        send_default_pii=False,
        traces_sample_rate=0.0,
        max_request_body_size="never",
        before_send=scrub_event,
    )
    return True
