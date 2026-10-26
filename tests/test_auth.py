"""Authentication and data isolation: tokens are verified, every data endpoint needs one, and
user A's resumes / matches / tailored resumes / comparisons / exports are invisible to user B."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import ProfileRepository
from app.main import create_app
from tests.auth_helpers import JWT_SECRET, SUPABASE_URL, AuthedClient, User, make_token, multi_user
from tests.compare_fakes import ANALYST_JD_TEXT, FakeCompareProvider
from tests.match_fakes import PYTHON_JD_TEXT
from tests.tailor_fakes import FakeTailorProvider

PDF = "application/pdf"


class FakeEverything(FakeTailorProvider, FakeCompareProvider):
    """Answers parsing, JD parsing, ratings, suggestions, tailoring and comparison summaries."""


@pytest.fixture
def app_settings(settings: Settings) -> Settings:
    return multi_user(settings, compare_retry_base_delay=0, match_semantic_similarity=False)


@pytest.fixture
def client(app_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(app_settings, provider=FakeEverything())) as c:
        yield c


# Every endpoint that touches user data, with a placeholder id.
PROTECTED = [
    ("GET", "/resumes"),
    ("POST", "/resumes"),
    ("GET", "/resumes/x"),
    ("GET", "/resumes/x/file"),
    ("POST", "/match"),
    ("GET", "/match/x"),
    ("GET", "/matches"),
    ("POST", "/tailor"),
    ("GET", "/tailor/x"),
    ("PUT", "/tailor/x"),
    ("POST", "/tailor/x/preview"),
    ("POST", "/tailor/x/autofit"),
    ("POST", "/tailor/x/export"),
    ("GET", "/tailors"),
    ("POST", "/compare"),
    ("GET", "/compare/x"),
    ("POST", "/compare/x/jobs"),
    ("DELETE", "/compare/x/jobs/y"),
    ("GET", "/compare/x/export"),
    ("GET", "/compares"),
    ("POST", "/jd/fetch"),
    ("GET", "/me"),
    ("PUT", "/me/settings"),
    ("GET", "/me/export"),
    ("DELETE", "/me"),
]


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


def test_every_data_endpoint_needs_a_token(client: TestClient) -> None:
    routes = {(m, r.path) for r in client.app.routes for m in getattr(r, "methods", set())}
    for method, path in PROTECTED:
        response = client.request(method, path)
        assert response.status_code == 401, (method, path, response.status_code)
        assert response.headers["www-authenticate"] == "Bearer"
    # Guard against new endpoints being added without a test here.
    public = {"/", "/login", "/app", "/privacy", "/terms", "/config.js", "/favicon.ico", "/robots.txt", "/health",
              "/files/{bucket}/{user_id}/{name}", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    covered = {p.replace("/x", "/{}").replace("/y", "/{}") for _, p in PROTECTED}
    for method, path in routes:
        if method == "HEAD" or path in public:
            continue
        generic = "/".join("{}" if part.startswith("{") else part for part in path.split("/"))
        assert generic in covered, f"{method} {path} is not in the auth test list"


@pytest.mark.parametrize(
    "token",
    [
        "not-a-jwt",
        make_token("11111111-1111-4111-8111-111111111111", "a@x.com", secret="wrong-secret-wrong-secret-wrong-secret-0000"),
        make_token("11111111-1111-4111-8111-111111111111", "a@x.com", expires_in=-120),
        make_token("11111111-1111-4111-8111-111111111111", "a@x.com", audience="someone-else"),
        make_token("11111111-1111-4111-8111-111111111111", "a@x.com", issuer="https://evil.supabase.co/auth/v1"),
        make_token("11111111-1111-4111-8111-111111111111", "a@x.com", role="anon"),
        make_token("11111111-1111-4111-8111-111111111111", "a@x.com", algorithm="none", key=""),
    ],
    ids=["garbage", "wrong-secret", "expired", "wrong-audience", "wrong-issuer", "anon-role", "alg-none"],
)
def test_invalid_tokens_get_401(client: TestClient, token: str) -> None:
    response = client.get("/resumes", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert "sign in" in response.json()["detail"].lower()


def test_wrong_scheme_gets_401(client: TestClient) -> None:
    user = User("a@example.com")
    assert client.get("/resumes", headers={"Authorization": f"Basic {user.token}"}).status_code == 401


def test_es256_tokens_are_verified_with_the_project_keys(client: TestClient) -> None:
    """Newer Supabase projects sign with ES256; the key comes from the project's JWKS."""
    private = ec.generate_private_key(ec.SECP256R1())
    verifier = client.app.state.authenticator.verifier

    class Key:
        key = private.public_key()

    verifier._jwks.get_signing_key_from_jwt = lambda token: Key()  # no network in tests
    token = make_token("22222222-2222-4222-8222-222222222222", "es@example.com", algorithm="ES256", key=private, headers={"kid": "k1"})
    assert client.get("/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200

    other = ec.generate_private_key(ec.SECP256R1())
    forged = make_token("22222222-2222-4222-8222-222222222222", "es@example.com", algorithm="ES256", key=other, headers={"kid": "k1"})
    assert client.get("/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401


def test_profile_created_on_first_login(client: TestClient) -> None:
    user = User("new.person@example.com")
    profiles: ProfileRepository = client.app.state.profiles
    assert profiles.get(user.id) is None
    me = client.get("/me", headers=user.headers).json()
    assert me["user"]["email"] == "new.person@example.com" and me["plan"] == "free" and me["auth_enabled"] is True
    profile = profiles.get(user.id)
    assert profile is not None and profile.email == "new.person@example.com" and profile.name == "New.Person"
    assert me["usage"]["actions"]["match"] == {"used": 0, "limit": 10, "label": "matches"}


def test_public_pages_need_no_login(client: TestClient) -> None:
    for path in ("/", "/login", "/app", "/privacy", "/terms", "/health", "/config.js", "/static/site.css", "/static/og-image.png", "/robots.txt"):
        assert client.get(path).status_code == 200, path
    landing = client.get("/").text
    assert "Get your resume match score for any job in 30 seconds" in landing
    assert 'property="og:image" content="https://resume.example.com/static/og-image.png"' in landing
    assert "{{" not in landing and "{{" not in client.get("/privacy").text


def test_browser_config_never_contains_the_service_key(app_settings: Settings) -> None:
    settings = replace(app_settings, supabase_service_role_key="service-role-SECRET")
    with TestClient(create_app(settings, provider=FakeEverything())) as c:
        body = c.get("/config.js").text
    assert "service-role-SECRET" not in body
    assert "anon-public-key" in body and SUPABASE_URL in body and JWT_SECRET not in body


def test_security_headers_and_cors(client: TestClient) -> None:
    response = client.get("/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in response.headers["content-security-policy"]
    assert SUPABASE_URL in response.headers["content-security-policy"]
    assert response.headers["strict-transport-security"].startswith("max-age=")

    def preflight(origin: str):
        return client.options("/resumes", headers={"Origin": origin, "Access-Control-Request-Method": "POST"})

    assert preflight("https://resume.example.com").headers.get("access-control-allow-origin") == "https://resume.example.com"
    assert preflight("http://localhost:5173").headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert "access-control-allow-origin" not in preflight("https://evil.example.org").headers


def test_production_refuses_to_start_without_supabase(settings: Settings) -> None:
    with pytest.raises(RuntimeError, match="SUPABASE_URL"):
        create_app(replace(settings, app_env="production"))


# ---------------------------------------------------------------------------
# Isolation between users
# ---------------------------------------------------------------------------


def _done(res) -> dict:
    assert res.status_code in (200, 202), res.text
    return res.json()


@pytest.fixture
def owner_data(client: TestClient, sample_pdf_bytes: bytes) -> dict:
    """User A with one of everything: resume, match, tailored resume, comparison, exports."""
    a = AuthedClient(client, User("alice@example.com"))
    resume = _done(a.post("/resumes", files={"file": ("alice.pdf", sample_pdf_bytes, PDF)}))
    match = _done(a.post("/match", data={"jd_text": PYTHON_JD_TEXT, "resume_id": resume["id"]}))
    assert a.get(f"/match/{match['match_id']}").json()["status"] == "done"
    tailor = _done(a.post("/tailor", json={"match_id": match["match_id"]}))
    assert a.get(f"/tailor/{tailor['tailor_id']}").json()["status"] == "done"
    jobs = json.dumps([{"text": PYTHON_JD_TEXT}, {"text": ANALYST_JD_TEXT}])
    compare = _done(a.post("/compare", data={"resume_id": resume["id"], "jobs": jobs}))
    view = a.get(f"/compare/{compare['compare_id']}").json()
    assert view["status"] == "done"
    export = _done(a.get(f"/compare/{compare['compare_id']}/export?format=csv"))
    return {
        "client": a,
        "resume_id": resume["id"],
        "match_id": match["match_id"],
        "tailor_id": tailor["tailor_id"],
        "compare_id": compare["compare_id"],
        "job_id": view["jobs"][0]["job_id"],
        "export_url": export["url"],
    }


def test_user_b_cannot_read_or_touch_user_a_data(client: TestClient, owner_data: dict, sample_pdf_bytes: bytes) -> None:
    a = owner_data["client"]
    b = AuthedClient(client, User("bob@example.com"))
    rid, mid, tid, cid, jid = (owner_data[k] for k in ("resume_id", "match_id", "tailor_id", "compare_id", "job_id"))

    # A sees their own data.
    assert a.get(f"/resumes/{rid}").status_code == 200
    # 2 matches: the Job Match, plus the comparison's Analyst JD (its Python JD re-used the cached match).
    assert len(a.get("/resumes").json()) == 1 and len(a.get("/matches").json()) == 2

    # Lists are empty for B.
    for path in ("/resumes", "/matches", "/tailors", "/compares"):
        assert b.get(path).json() == [], path

    # Direct access by id: 404, exactly like a record that does not exist.
    for method, path, kwargs in [
        ("get", f"/resumes/{rid}", {}),
        ("get", f"/resumes/{rid}/file", {}),
        ("get", f"/match/{mid}", {}),
        ("get", f"/tailor/{tid}", {}),
        ("put", f"/tailor/{tid}", {"json": {"document": {}}}),
        ("post", f"/tailor/{tid}/preview", {"json": {}}),
        ("post", f"/tailor/{tid}/autofit", {"json": {}}),
        ("post", f"/tailor/{tid}/export", {"json": {"format": "pdf"}}),
        ("get", f"/compare/{cid}", {}),
        ("get", f"/compare/{cid}/export?format=csv", {}),
        ("post", f"/compare/{cid}/jobs", {"data": {"text": ANALYST_JD_TEXT}}),
        ("delete", f"/compare/{cid}/jobs/{jid}", {}),
        # Using A's ids inside B's own requests.
        ("post", "/match", {"data": {"jd_text": PYTHON_JD_TEXT, "resume_id": rid}}),
        ("post", "/tailor", {"json": {"match_id": mid}}),
        ("post", "/compare", {"data": {"resume_id": rid, "jobs": json.dumps([{"text": PYTHON_JD_TEXT}, {"text": ANALYST_JD_TEXT}])}}),
    ]:
        response = getattr(b, method)(path, **kwargs)
        assert response.status_code == 404, (method, path, response.status_code, response.text)

    # Nothing of A's changed.
    assert a.get(f"/compare/{cid}").json()["progress"]["total"] == 2
    assert a.get(f"/tailor/{tid}").json()["status"] == "done"

    # B uploading the very same PDF gets their own record (no cross-user cache hit) and is charged a parse.
    mine = b.post("/resumes", files={"file": ("bob.pdf", sample_pdf_bytes, PDF)}).json()
    assert mine["cached"] is False and mine["id"] != rid
    assert b.get("/me").json()["usage"]["actions"]["parse"]["used"] == 1
    # ... and the same JD on it is a new match, not A's cached one.
    match_b = b.post("/match", data={"jd_text": PYTHON_JD_TEXT, "resume_id": mine["id"]}).json()
    assert match_b["cached"] is False and match_b["match_id"] != mid


def test_export_links_are_signed_and_expire(client: TestClient, owner_data: dict) -> None:
    a = owner_data["client"]
    url = owner_data["export_url"]
    assert a.client.get(url).status_code == 200  # the signed link itself is the permission
    tampered = url.replace("sig=", "sig=0")
    assert client.get(tampered).status_code == 403
    other_file = url.replace("/files/exports/", "/files/resumes/")
    assert client.get(other_file).status_code == 403
    storage = client.app.state.storage
    storage.expires_seconds = -5  # already expired
    fresh = a.get(f"/compare/{owner_data['compare_id']}/export?format=csv").json()["url"]
    assert client.get(fresh).status_code == 403
    assert a.get(f"/resumes/{owner_data['resume_id']}/file").json()["url"].startswith("/files/resumes/")
