"""Structured (JSON-lines) logging.

Usage:
    logger = logging.getLogger(__name__)
    logger.info("parsed resume", extra={"file_name": "a.pdf", "processing_ms": 812})

Every key passed via ``extra`` ends up as a field in the JSON log line - except personal data:
fields that can hold resume / job content or identity (file names, extracted values, names, e-mails,
URLs, text) are dropped here, centrally, so no call site can leak them. Log ids, counts and timings.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

# Attributes every LogRecord has; anything else came from ``extra=`` and is logged as a field.
_STANDARD_ATTRS = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}
# Never written to the logs (personal data / resume or JD content).
REDACTED_FIELDS = frozenset(
    {"file_name", "value", "title", "name", "email", "candidate_name", "text", "raw_text", "jd_text", "prompt", "url", "summary"}
)
MAX_FIELD_CHARS = 300


class JsonFormatter(logging.Formatter):
    """Format each log record as a single JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key in _STANDARD_ATTRS or key.startswith("_") or key in REDACTED_FIELDS:
                continue
            if isinstance(value, str) and len(value) > MAX_FIELD_CHARS:
                value = value[:MAX_FIELD_CHARS] + "..."
            payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


_configured = False


def setup_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger (safe to call more than once)."""
    global _configured
    root = logging.getLogger()
    root.setLevel(level.upper())
    if _configured:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root.handlers = [handler]
    # Third-party libraries are chatty at INFO; keep them quieter.
    # (httpx logs every request URL, which can include storage object paths and job links.)
    for noisy in ("httpx", "httpx2", "httpcore", "google_genai", "google.genai", "urllib3", "PIL", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True
