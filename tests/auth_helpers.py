"""Multi-user test helpers: Supabase-style settings and locally signed access tokens."""

from __future__ import annotations

import time
import uuid
from dataclasses import replace
from typing import Any

import jwt
from fastapi.testclient import TestClient

from app.config import Settings

SUPABASE_URL = "https://test-project.supabase.co"
JWT_SECRET = "test-jwt-secret-that-is-long-enough-for-hs256-0123456789"


def multi_user(settings: Settings, **overrides: Any) -> Settings:
    """Settings with Supabase login on (HS256 secret) but local file storage."""
    return replace(
        settings,
        supabase_url=SUPABASE_URL,
        supabase_anon_key="anon-public-key",
        supabase_service_role_key=None,  # storage stays local in tests
        supabase_jwt_secret=JWT_SECRET,
        app_base_url="https://resume.example.com",
        **overrides,
    )


def make_token(
    user_id: str,
    email: str,
    *,
    secret: str = JWT_SECRET,
    expires_in: int = 3600,
    audience: str = "authenticated",
    issuer: str = f"{SUPABASE_URL}/auth/v1",
    role: str = "authenticated",
    name: str | None = None,
    algorithm: str = "HS256",
    key: Any = None,
    headers: dict | None = None,
) -> str:
    now = int(time.time())
    claims = {
        "sub": user_id,
        "email": email,
        "aud": audience,
        "iss": issuer,
        "role": role,
        "iat": now,
        "exp": now + expires_in,
        "user_metadata": {"full_name": name or email.split("@")[0].title(), "avatar_url": f"https://example.com/{user_id}.png"},
    }
    return jwt.encode(claims, key if key is not None else secret, algorithm=algorithm, headers=headers)


class User:
    """A test user: an id, an e-mail and ready-made Authorization headers."""

    def __init__(self, email: str) -> None:
        self.id = str(uuid.uuid4())
        self.email = email
        self.token = make_token(self.id, email)
        self.headers = {"Authorization": f"Bearer {self.token}"}


class AuthedClient:
    """Wraps a TestClient so every request carries one user's token."""

    def __init__(self, client: TestClient, user: User) -> None:
        self.client = client
        self.user = user

    def __getattr__(self, method: str):
        if method not in {"get", "post", "put", "delete", "options"}:
            raise AttributeError(method)

        def call(url: str, **kwargs: Any):
            headers = {**self.user.headers, **kwargs.pop("headers", {})}
            # request() rather than delete() etc.: DELETE /me sends a JSON body.
            return self.client.request(method.upper(), url, headers=headers, **kwargs)

        return call
