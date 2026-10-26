"""File storage: uploaded resume PDFs and generated exports.

Two private buckets:
  * ``resumes`` - uploaded PDFs,               path ``<user_id>/<uuid>.pdf``
  * ``exports`` - generated PDF / DOCX / CSV,   path ``<user_id>/<uuid>.<ext>``

Backends:
  * ``SupabaseStorage`` - Supabase Storage over its REST API with the service-role key (backend only;
    the key never reaches the browser). Downloads use signed URLs that expire (SIGNED_URL_SECONDS, 10 min).
  * ``LocalStorage`` - a folder on disk (local mode and tests). Downloads go through ``/files/...`` with an
    HMAC-signed, expiring link, so the behaviour matches Supabase.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlencode

import httpx

logger = logging.getLogger(__name__)

RESUMES_BUCKET = "resumes"
EXPORTS_BUCKET = "exports"
BUCKETS = (RESUMES_BUCKET, EXPORTS_BUCKET)


class StorageError(RuntimeError):
    pass


def service_headers(key: str) -> dict[str, str]:
    """Auth headers for the service-role key: legacy JWT keys go in both headers; new "sb_secret_..." keys
    are not JWTs and may only be sent as ``apikey``."""
    if key.startswith("sb_"):
        return {"apikey": key}
    return {"Authorization": f"Bearer {key}", "apikey": key}


def object_path(user_id: str, ext: str) -> str:
    return f"{user_id}/{uuid.uuid4().hex}.{ext.lstrip('.')}"


def _safe_path(path: str) -> str:
    parts = path.split("/")
    if len(parts) != 2 or any(not p or p in (".", "..") or "\\" in p for p in parts):
        raise StorageError("invalid object path")
    return path


class Storage(Protocol):
    def upload(self, bucket: str, path: str, data: bytes, content_type: str) -> None: ...
    def download(self, bucket: str, path: str) -> bytes: ...
    def signed_url(self, bucket: str, path: str, download_name: str | None = None) -> str: ...
    def delete(self, bucket: str, paths: list[str]) -> None: ...
    def list(self, bucket: str, prefix: str) -> list[tuple[str, datetime | None]]: ...
    def delete_prefix(self, bucket: str, prefix: str) -> int: ...


# ---------------------------------------------------------------------------
# Local folder
# ---------------------------------------------------------------------------


class LocalStorage:
    def __init__(self, root: Path, secret: str | None = None, expires_seconds: int = 600) -> None:
        self.root = root
        self.secret = (secret or secrets.token_hex(32)).encode()
        self.expires_seconds = expires_seconds

    def _file(self, bucket: str, path: str) -> Path:
        if bucket not in BUCKETS:
            raise StorageError("unknown bucket")
        return self.root / bucket / _safe_path(path)

    def upload(self, bucket: str, path: str, data: bytes, content_type: str) -> None:
        target = self._file(bucket, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def download(self, bucket: str, path: str) -> bytes:
        try:
            return self._file(bucket, path).read_bytes()
        except FileNotFoundError as exc:
            raise StorageError("file not found") from exc

    def _signature(self, bucket: str, path: str, expires: int, name: str) -> str:
        message = f"{bucket}/{path}|{expires}|{name}".encode()
        return hmac.new(self.secret, message, hashlib.sha256).hexdigest()

    def signed_url(self, bucket: str, path: str, download_name: str | None = None) -> str:
        expires = int(time.time()) + self.expires_seconds
        name = download_name or path.rsplit("/", 1)[-1]
        query = urlencode({"exp": expires, "name": name, "sig": self._signature(bucket, path, expires, name)})
        return f"/files/{bucket}/{quote(path)}?{query}"

    def verify(self, bucket: str, path: str, expires: int, name: str, signature: str) -> bool:
        if expires < time.time():
            return False
        return hmac.compare_digest(self._signature(bucket, path, expires, name), signature)

    def delete(self, bucket: str, paths: list[str]) -> None:
        for path in paths:
            try:
                self._file(bucket, path).unlink(missing_ok=True)
            except StorageError:
                continue

    def list(self, bucket: str, prefix: str) -> list[tuple[str, datetime | None]]:
        folder = self.root / bucket / prefix.strip("/") if prefix else self.root / bucket
        if not folder.is_dir():
            return []
        out = []
        for item in folder.iterdir():
            name = f"{prefix.strip('/')}/{item.name}" if prefix else item.name
            out.append((name, datetime.fromtimestamp(item.stat().st_mtime, timezone.utc) if item.is_file() else None))
        return out

    def delete_prefix(self, bucket: str, prefix: str) -> int:
        files = [name for name, created in self.list(bucket, prefix) if created is not None]
        self.delete(bucket, files)
        return len(files)


# ---------------------------------------------------------------------------
# Supabase Storage
# ---------------------------------------------------------------------------


class SupabaseStorage:
    def __init__(self, url: str, service_role_key: str, expires_seconds: int = 600, transport: httpx.BaseTransport | None = None) -> None:
        self.base = f"{url}/storage/v1"
        self.expires_seconds = expires_seconds
        self.client = httpx.Client(timeout=60, transport=transport, headers=service_headers(service_role_key))

    def _check(self, response: httpx.Response, what: str) -> httpx.Response:
        if response.status_code >= 400:
            # Never log the body: it can echo object names; status + operation is enough.
            logger.error("storage request failed", extra={"operation": what, "status": response.status_code})
            raise StorageError(f"File storage error during {what} (HTTP {response.status_code}).")
        return response

    def ensure_buckets(self) -> None:
        """Create the private buckets if they don't exist yet (safe to call on every start)."""
        existing = {b.get("id") for b in self._check(self.client.get(f"{self.base}/bucket"), "list buckets").json()}
        for bucket in BUCKETS:
            if bucket not in existing:
                response = self.client.post(f"{self.base}/bucket", json={"id": bucket, "name": bucket, "public": False})
                if response.status_code not in (200, 409):
                    self._check(response, "create bucket")
                logger.info("storage bucket created", extra={"bucket": bucket})

    def upload(self, bucket: str, path: str, data: bytes, content_type: str) -> None:
        self._check(
            self.client.post(
                f"{self.base}/object/{bucket}/{quote(_safe_path(path))}",
                content=data,
                headers={"Content-Type": content_type, "x-upsert": "true", "cache-control": "no-store"},
            ),
            "upload",
        )

    def download(self, bucket: str, path: str) -> bytes:
        return self._check(self.client.get(f"{self.base}/object/{bucket}/{quote(_safe_path(path))}"), "download").content

    def signed_url(self, bucket: str, path: str, download_name: str | None = None) -> str:
        response = self._check(
            self.client.post(f"{self.base}/object/sign/{bucket}/{quote(_safe_path(path))}", json={"expiresIn": self.expires_seconds}),
            "sign",
        )
        signed = response.json().get("signedURL") or response.json().get("signedUrl")
        if not signed:
            raise StorageError("File storage did not return a download link.")
        url = f"{self.base}{signed}" if signed.startswith("/") else signed
        if download_name:
            url += ("&" if "?" in url else "?") + urlencode({"download": download_name})
        return url

    def delete(self, bucket: str, paths: list[str]) -> None:
        for start in range(0, len(paths), 500):
            chunk = [_safe_path(p) for p in paths[start : start + 500]]
            if chunk:
                self._check(self.client.request("DELETE", f"{self.base}/object/{bucket}", json={"prefixes": chunk}), "delete")

    def list(self, bucket: str, prefix: str) -> list[tuple[str, datetime | None]]:
        out: list[tuple[str, datetime | None]] = []
        offset = 0
        while True:
            body = {"prefix": prefix.strip("/"), "limit": 1000, "offset": offset, "sortBy": {"column": "name", "order": "asc"}}
            items = self._check(self.client.post(f"{self.base}/object/list/{bucket}", json=body), "list").json()
            for item in items:
                name = f"{prefix.strip('/')}/{item['name']}" if prefix.strip("/") else item["name"]
                created = item.get("created_at")
                out.append((name, datetime.fromisoformat(created.replace("Z", "+00:00")) if created else None))
            if len(items) < 1000:
                return out
            offset += 1000

    def delete_prefix(self, bucket: str, prefix: str) -> int:
        files = [name for name, created in self.list(bucket, prefix) if created is not None]
        self.delete(bucket, files)
        return len(files)


class SupabaseAdmin:
    """Supabase Auth admin calls (service-role key; backend only)."""

    def __init__(self, url: str, service_role_key: str, transport: httpx.BaseTransport | None = None) -> None:
        self.base = f"{url}/auth/v1/admin"
        self.client = httpx.Client(timeout=30, transport=transport, headers=service_headers(service_role_key))

    def delete_user(self, user_id: str) -> None:
        response = self.client.delete(f"{self.base}/users/{user_id}")
        if response.status_code not in (200, 204, 404):
            raise StorageError(f"Could not delete the login account (HTTP {response.status_code}).")

    def find_user_id(self, email: str) -> str | None:
        page = 1
        while True:
            response = self.client.get(f"{self.base}/users", params={"page": page, "per_page": 200})
            if response.status_code >= 400:
                raise StorageError(f"Could not list users (HTTP {response.status_code}).")
            users = response.json().get("users", [])
            for user in users:
                if (user.get("email") or "").lower() == email.lower():
                    return user["id"]
            if len(users) < 200:
                return None
            page += 1


def make_storage(settings) -> Storage:
    if settings.supabase_url and settings.supabase_service_role_key:
        return SupabaseStorage(settings.supabase_url, settings.supabase_service_role_key, settings.signed_url_seconds)
    return LocalStorage(settings.storage_dir, settings.secret_key, settings.signed_url_seconds)
