"""Authentication: Supabase access tokens -> ``CurrentUser``, plus the per-user rate limit.

Every data endpoint depends on ``get_current_user``:

* Multi-user mode (SUPABASE_URL set): the request must carry ``Authorization: Bearer <access_token>``
  from Supabase Auth. The JWT is verified with SUPABASE_JWT_SECRET (HS256, legacy projects) or with
  the project's public signing keys from JWKS (ES256 / RS256, newer projects). Issuer, audience
  ("authenticated") and expiry are checked. Missing / invalid token -> 401.
* Local mode (no SUPABASE_URL): a single built-in local user, no login (the original desktop setup).

The user's profile row is created the first time a token is seen. Rate limits (``limits`` storage, the
library slowapi is built on) are per user: RATE_LIMIT_HEAVY for requests that start Gemini work or
render files, RATE_LIMIT_DEFAULT for everything else.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import HTTPException, Request
from limits import parse as parse_limit
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter

from app.config import Settings
from app.db import PLAN_FREE, ProfileRepository

logger = logging.getLogger(__name__)

LOCAL_USER_ID = "00000000-0000-4000-8000-000000000001"

# (method, route path) of requests that start Gemini work, PDF rendering or big uploads.
HEAVY_ROUTES = {
    ("POST", "/resumes"),
    ("POST", "/match"),
    ("POST", "/tailor"),
    ("POST", "/tailor/{tailor_id}/autofit"),
    ("POST", "/tailor/{tailor_id}/export"),
    ("POST", "/compare"),
    ("POST", "/compare/{compare_id}/jobs"),
    ("GET", "/compare/{compare_id}/export"),
    ("POST", "/jd/fetch"),
    ("GET", "/me/export"),
}


@dataclass(frozen=True)
class CurrentUser:
    id: str
    email: str | None
    name: str | None = None
    avatar_url: str | None = None
    plan: str = PLAN_FREE


LOCAL_USER = CurrentUser(id=LOCAL_USER_ID, email="local@localhost", name="Local user", plan="pro")


class AuthError(Exception):
    pass


class TokenVerifier:
    """Verifies Supabase access tokens (HS256 secret or JWKS public keys)."""

    def __init__(self, supabase_url: str, jwt_secret: str | None) -> None:
        self.issuer = f"{supabase_url}/auth/v1"
        self.jwt_secret = jwt_secret
        self._jwks = jwt.PyJWKClient(f"{self.issuer}/.well-known/jwks.json", cache_keys=True, lifespan=3600, timeout=10)

    def verify(self, token: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise AuthError("malformed token") from exc
        alg = header.get("alg")
        options = {"require": ["exp", "sub", "aud"]}
        try:
            if alg == "HS256":
                if not self.jwt_secret:
                    raise AuthError("HS256 token but SUPABASE_JWT_SECRET is not set")
                key: Any = self.jwt_secret
            elif alg in ("ES256", "RS256", "EdDSA"):
                key = self._jwks.get_signing_key_from_jwt(token).key
            else:
                raise AuthError(f"unsupported algorithm {alg!r}")
            claims = jwt.decode(token, key, algorithms=[alg], audience="authenticated", issuer=self.issuer, options=options, leeway=30)
        except jwt.PyJWKClientError as exc:
            raise AuthError("signing key not found") from exc
        except jwt.PyJWTError as exc:
            raise AuthError(type(exc).__name__) from exc
        if claims.get("role") != "authenticated":
            raise AuthError("not an authenticated user token")
        return claims


class RateLimiter:
    def __init__(self, heavy: str, default: str) -> None:
        self._limiter = MovingWindowRateLimiter(MemoryStorage())
        self.heavy = parse_limit(heavy)
        self.default = parse_limit(default)

    def check(self, user_id: str, heavy: bool) -> None:
        item = self.heavy if heavy else self.default
        if not self._limiter.hit(item, "heavy" if heavy else "default", user_id):
            raise HTTPException(
                status_code=429,
                detail=f"Too many requests - please slow down (limit {item.amount} per {item.get_expiry() // 60 or 1} minute).",
                headers={"Retry-After": "60"},
            )


class Authenticator:
    def __init__(self, settings: Settings, profiles: ProfileRepository) -> None:
        self.settings = settings
        self.profiles = profiles
        self.verifier = TokenVerifier(settings.supabase_url, settings.supabase_jwt_secret) if settings.supabase_url else None
        self.rate_limiter = RateLimiter(settings.rate_limit_heavy, settings.rate_limit_default)
        self._known: dict[str, tuple[str | None, str | None, str | None]] = {}  # user id -> profile fields last saved
        self._lock = threading.Lock()

    def user_from_token(self, token: str) -> CurrentUser:
        assert self.verifier is not None
        claims = self.verifier.verify(token)
        meta = claims.get("user_metadata") or {}
        user_id = str(claims["sub"])
        email = claims.get("email") or meta.get("email")
        name = meta.get("full_name") or meta.get("name")
        avatar = meta.get("avatar_url") or meta.get("picture")
        fields = (email, name, avatar)
        with self._lock:
            known = self._known.get(user_id)
        if known != fields:  # first request of this user (in this process), or details changed
            profile = self.profiles.ensure(user_id, email, name, avatar)
            with self._lock:
                self._known[user_id] = fields
            plan = profile.plan
        else:
            profile = self.profiles.get(user_id)
            plan = profile.plan if profile else PLAN_FREE
            if profile is None:  # deleted meanwhile (account deletion): recreate on next request
                with self._lock:
                    self._known.pop(user_id, None)
                raise AuthError("account deleted")
        return CurrentUser(id=user_id, email=email, name=name, avatar_url=avatar, plan=plan)

    def forget(self, user_id: str) -> None:
        with self._lock:
            self._known.pop(user_id, None)


def _unauthorized(message: str) -> HTTPException:
    return HTTPException(status_code=401, detail=message, headers={"WWW-Authenticate": "Bearer"})


def get_current_user(request: Request) -> CurrentUser:
    """FastAPI dependency: the signed-in user (401 if not signed in), with the rate limit applied."""
    auth: Authenticator = request.app.state.authenticator
    if not auth.settings.auth_enabled:
        user = LOCAL_USER
        profile = auth.profiles.get(user.id)
        if profile is None:
            auth.profiles.ensure(user.id, user.email, user.name)
            auth.profiles.update(user.id, plan="pro")
        else:
            user = CurrentUser(id=user.id, email=user.email, name=user.name, plan=profile.plan)
    else:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise _unauthorized("Please sign in.")
        try:
            user = auth.user_from_token(token.strip())
        except AuthError as exc:
            logger.info("auth rejected", extra={"reason": str(exc)})
            raise _unauthorized("Your session is invalid or has expired. Please sign in again.") from exc
    route = request.scope.get("route")
    heavy = (request.method, getattr(route, "path", request.url.path)) in HEAVY_ROUTES
    auth.rate_limiter.check(user.id, heavy)
    request.state.user_id = user.id
    return user
