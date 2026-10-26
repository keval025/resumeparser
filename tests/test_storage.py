"""Storage backends: the Supabase REST client (mocked HTTP) and the local folder with signed links."""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from app.storage import LocalStorage, StorageError, SupabaseAdmin, SupabaseStorage, object_path

URL = "https://proj.supabase.co"


class FakeSupabase:
    """Records requests and answers like Supabase Storage / Auth admin."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.buckets = ["resumes"]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/storage/v1/bucket" and request.method == "GET":
            return httpx.Response(200, json=[{"id": b} for b in self.buckets])
        if path == "/storage/v1/bucket" and request.method == "POST":
            self.buckets.append(json.loads(request.content)["id"])
            return httpx.Response(200, json={"name": "ok"})
        if path.startswith("/storage/v1/object/sign/"):
            return httpx.Response(200, json={"signedURL": f"/object/sign/{path.split('/sign/')[1]}?token=abc"})
        if path.startswith("/storage/v1/object/list/"):
            return httpx.Response(200, json=[{"name": "a.pdf", "created_at": "2026-10-01T10:00:00Z"}, {"name": "folder", "created_at": None}])
        if path.startswith("/storage/v1/object/") and request.method in ("POST", "DELETE"):
            return httpx.Response(200, json={"Key": path})
        if path.startswith("/auth/v1/admin/users"):
            if request.method == "DELETE":
                return httpx.Response(200, json={})
            return httpx.Response(200, json={"users": [{"id": "u-1", "email": "Me@Example.com"}]})
        return httpx.Response(404)


def test_supabase_storage_requests() -> None:
    fake = FakeSupabase()
    storage = SupabaseStorage(URL, "eyJ.service.jwt", 600, transport=httpx.MockTransport(fake))
    storage.ensure_buckets()
    created = [json.loads(r.content) for r in fake.requests if r.method == "POST" and r.url.path == "/storage/v1/bucket"]
    assert created == [{"id": "exports", "name": "exports", "public": False}]  # private, only the missing one

    storage.upload("resumes", "user-1/abc.pdf", b"%PDF", "application/pdf")
    upload = fake.requests[-1]
    assert upload.url.path == "/storage/v1/object/resumes/user-1/abc.pdf" and upload.content == b"%PDF"
    assert upload.headers["authorization"] == "Bearer eyJ.service.jwt" and upload.headers["x-upsert"] == "true"

    url = storage.signed_url("exports", "user-1/x.pdf", "My Resume.pdf")
    assert url.startswith(f"{URL}/storage/v1/object/sign/exports/user-1/x.pdf?token=abc&download=My+Resume.pdf")
    assert json.loads(fake.requests[-1].content) == {"expiresIn": 600}

    storage.delete("resumes", ["user-1/abc.pdf"])
    assert json.loads(fake.requests[-1].content) == {"prefixes": ["user-1/abc.pdf"]}
    assert storage.delete_prefix("exports", "user-1") == 1  # files only, not sub-folders


def test_new_secret_keys_are_not_sent_as_bearer() -> None:
    fake = FakeSupabase()
    SupabaseStorage(URL, "sb_secret_abc", transport=httpx.MockTransport(fake)).upload("resumes", "u/a.pdf", b"x", "application/pdf")
    assert "authorization" not in fake.requests[-1].headers and fake.requests[-1].headers["apikey"] == "sb_secret_abc"


def test_storage_errors_and_bad_paths() -> None:
    storage = SupabaseStorage(URL, "k", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(StorageError, match="HTTP 500"):
        storage.upload("resumes", "u/a.pdf", b"x", "application/pdf")
    for bad in ("../etc/passwd", "u/../x", "a/b/c.pdf", "u/"):
        with pytest.raises(StorageError):
            storage.signed_url("resumes", bad)


def test_supabase_admin() -> None:
    fake = FakeSupabase()
    admin = SupabaseAdmin(URL, "k", transport=httpx.MockTransport(fake))
    assert admin.find_user_id("me@example.com") == "u-1"
    assert admin.find_user_id("nobody@example.com") is None
    admin.delete_user("u-1")
    assert fake.requests[-1].method == "DELETE" and fake.requests[-1].url.path == "/auth/v1/admin/users/u-1"


def test_local_storage_signed_links(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path, "secret", expires_seconds=600)
    path = object_path("user-1", "pdf")
    assert path.startswith("user-1/") and path.endswith(".pdf")
    storage.upload("resumes", path, b"%PDF", "application/pdf")
    assert storage.download("resumes", path) == b"%PDF"
    url = storage.signed_url("resumes", path, "cv.pdf")
    query = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))
    assert storage.verify("resumes", path, int(query["exp"]), "cv.pdf", query["sig"])
    assert not storage.verify("resumes", path, int(query["exp"]), "other.pdf", query["sig"])  # name is signed too
    assert not storage.verify("exports", path, int(query["exp"]), "cv.pdf", query["sig"])
    assert not storage.verify("resumes", path, int(time.time()) - 1, "cv.pdf", query["sig"])
    assert storage.delete_prefix("resumes", "user-1") == 1 and storage.list("resumes", "user-1") == []
    with pytest.raises(StorageError):
        storage.upload("public", path, b"x", "text/plain")
