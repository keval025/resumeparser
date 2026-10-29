"""Public pages (landing, login, password reset, app shell, privacy, terms), browser config, local file downloads, security headers.

The HTML pages are public; the data they show comes from the API, which requires a Supabase session.
``/config.js`` gives the browser the Supabase URL and the *anon* (public) key only. The service-role
key never leaves the server.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import BASE_DIR, Settings
from app.storage import BUCKETS, LocalStorage

STATIC_DIR = BASE_DIR / "static"

router = APIRouter(include_in_schema=False)

# Content types for downloads (don't trust the OS registry: Windows maps .csv to Excel).
MEDIA_TYPES = {
    "pdf": "application/pdf",
    "csv": "text/csv; charset=utf-8",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

PAGES = {"/": "landing.html", "/login": "login.html", "/reset-password": "reset-password.html", "/app": "index.html", "/privacy": "privacy.html", "/terms": "terms.html"}


@lru_cache(maxsize=16)
def _template(path: Path, mtime: float) -> str:
    return path.read_text(encoding="utf-8")


def render_page(name: str, settings: Settings) -> str:
    path = STATIC_DIR / name
    html = _template(path, path.stat().st_mtime)
    limits = settings.free_limits
    values = {
        "APP_NAME": settings.app_name,
        "COMPANY": settings.company_name,
        "CONTACT_EMAIL": settings.contact_email,
        "BASE_URL": settings.app_base_url,
        "YEAR": str(datetime.now(timezone.utc).year),
        "LIMIT_PARSE": str(limits["parse"]),
        "LIMIT_MATCH": str(limits["match"]),
        "LIMIT_TAILOR": str(limits["tailor"]),
        "LIMIT_COMPARE": str(limits["compare"]),
    }
    for key, value in values.items():
        html = html.replace("{{" + key + "}}", value)
    return html


def _page(name: str):
    def handler(request: Request) -> HTMLResponse:
        return HTMLResponse(render_page(name, request.app.state.settings), headers={"Cache-Control": "no-cache"})

    return handler


for _path, _name in PAGES.items():
    router.add_api_route(_path, _page(_name), methods=["GET"], response_class=HTMLResponse)


@router.get("/config.js")
def browser_config(request: Request) -> Response:
    s: Settings = request.app.state.settings
    config = {
        "authEnabled": s.auth_enabled,
        "supabaseUrl": s.supabase_url,
        "supabaseAnonKey": s.supabase_anon_key,
        "baseUrl": s.app_base_url,
        "appName": s.app_name,
    }
    return Response(f"window.APP_CONFIG = {json.dumps(config)};", media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@router.get("/favicon.ico")
def favicon() -> FileResponse:
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


@router.get("/robots.txt")
def robots() -> Response:
    return Response("User-agent: *\nAllow: /$\nAllow: /privacy\nAllow: /terms\nDisallow: /app\nDisallow: /reset-password\nDisallow: /files\n", media_type="text/plain")


@router.get("/files/{bucket}/{user_id}/{name}")
def local_file(bucket: str, user_id: str, name: str, exp: int, sig: str, request: Request) -> FileResponse:
    """Download through a signed link (local storage only; Supabase serves its own signed URLs)."""
    storage = request.app.state.storage
    path = f"{user_id}/{name}"
    download_name = request.query_params.get("name", name)
    if not isinstance(storage, LocalStorage) or bucket not in BUCKETS or not storage.verify(bucket, path, exp, download_name, sig):
        raise HTTPException(status_code=403, detail="This download link is invalid or has expired.")
    file = storage.root / bucket / path
    if not file.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    media_type = MEDIA_TYPES.get(name.rsplit(".", 1)[-1].lower(), "application/octet-stream")
    return FileResponse(file, filename=download_name, media_type=media_type, headers={"Cache-Control": "no-store"})


class SecurityHeaders(BaseHTTPMiddleware):
    """Standard browser security headers on every response."""

    def __init__(self, app, settings: Settings) -> None:
        super().__init__(app)
        supabase = settings.supabase_url or ""
        self.headers = {
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "SAMEORIGIN",
            "Referrer-Policy": "strict-origin-when-cross-origin",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
            "Cross-Origin-Opener-Policy": "same-origin-allow-popups",
            "Content-Security-Policy": "; ".join(
                [
                    "default-src 'self'",
                    # The UI is plain HTML with inline scripts; libraries come from two pinned CDNs.
                    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com",
                    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com",
                    "font-src 'self' data: https://fonts.gstatic.com",
                    "img-src 'self' data: blob: https:",
                    f"connect-src 'self' {supabase} {supabase.replace('https://', 'wss://')}".strip(),
                    "frame-src 'self' blob:",
                    "frame-ancestors 'self'",
                    "base-uri 'self'",
                    "form-action 'self'",
                    "object-src 'none'",
                ]
            ),
        }
        if settings.app_base_url.startswith("https://"):
            self.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        for key, value in self.headers.items():
            response.headers.setdefault(key, value)
        if request.url.path.startswith(("/resumes", "/match", "/tailor", "/compare", "/me", "/files")):
            response.headers.setdefault("Cache-Control", "no-store")
        return response
