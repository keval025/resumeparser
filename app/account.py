"""Account endpoints: who am I, usage, settings, "download all my data" and "delete my account".

GET    /me             profile, plan, usage this month, settings
PUT    /me/settings    {retention_days: 30 | 90 | null}
GET    /me/export      every row the user owns, as one JSON file
DELETE /me             {confirm: "DELETE"} -> database rows + stored files + the login account
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from app.auth import CurrentUser, get_current_user
from app.db import RETENTION_CHOICES
from app.storage import EXPORTS_BUCKET, RESUMES_BUCKET

logger = logging.getLogger(__name__)

router = APIRouter(tags=["account"])


class SettingsUpdate(BaseModel):
    retention_days: int | None = None


class DeleteRequest(BaseModel):
    confirm: str


@router.get("/me")
def me(request: Request, user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
    profile = request.app.state.profiles.get(user.id)
    return {
        "user": {"id": user.id, "email": user.email, "name": user.name, "avatar_url": user.avatar_url},
        "auth_enabled": request.app.state.settings.auth_enabled,
        "plan": user.plan,
        "retention_days": profile.retention_days if profile else None,
        "retention_choices": list(RETENTION_CHOICES),
        "member_since": profile.created_at if profile else None,
        "usage": request.app.state.usage.summary(user),
    }


@router.put("/me/settings")
def update_settings(body: SettingsUpdate, request: Request, user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
    if body.retention_days not in RETENTION_CHOICES:
        raise HTTPException(status_code=400, detail="Auto-delete must be 30 days, 90 days or never.")
    request.app.state.profiles.update(user.id, retention_days=body.retention_days)
    return me(request, user)


@router.get("/me/export")
def export_my_data(request: Request, user: CurrentUser = Depends(get_current_user)) -> Response:
    data = request.app.state.accounts.export(user.id)
    data["exported_at"] = datetime.now(timezone.utc).isoformat()
    data["note"] = "Everything this app stores about your account. Uploaded PDFs can be downloaded from My resumes."
    body = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    return Response(
        body,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="my_data_{datetime.now(timezone.utc):%Y%m%d}.json"',
            "Cache-Control": "no-store",
        },
    )


@router.delete("/me")
def delete_account(body: DeleteRequest, request: Request, user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
    """Permanently delete the account: all rows, all stored files, and (multi-user mode) the login itself."""
    if body.confirm != "DELETE":
        raise HTTPException(status_code=400, detail='Type DELETE to confirm.')
    state = request.app.state
    storage = state.storage
    files = 0
    paths = state.accounts.storage_paths(user.id)
    if paths:
        storage.delete(RESUMES_BUCKET, paths)
        files += len(paths)
    files += storage.delete_prefix(RESUMES_BUCKET, user.id)
    files += storage.delete_prefix(EXPORTS_BUCKET, user.id)
    rows = state.accounts.delete_all(user.id)
    if state.supabase_admin is not None and state.settings.auth_enabled:
        state.supabase_admin.delete_user(user.id)
    state.authenticator.forget(user.id)
    logger.info("account deleted", extra={"user_id": user.id, "rows": sum(rows.values()), "files": files})
    return {"deleted": True, "rows": rows, "files": files}
