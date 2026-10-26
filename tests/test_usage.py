"""Free-plan monthly limits (checked before Gemini, HTTP 429 with a friendly message) and per-user rate limits."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.config import Settings
from app.db import UsageRecord
from app.main import create_app
from app.match_schema import JobDescription
from app.usage import month_window
from tests.auth_helpers import AuthedClient, User, multi_user
from tests.compare_fakes import ANALYST_JD_TEXT
from tests.match_fakes import PYTHON_JD_TEXT
from tests.test_auth import FakeEverything

PDF = "application/pdf"


def pdf_variant(pdf: bytes, n: int) -> bytes:
    """The same resume with a different file hash (a PDF comment appended after %%EOF)."""
    return pdf + f"\n% variant {n}\n".encode()


def jd_variant(n: int) -> str:
    return f"{PYTHON_JD_TEXT}\nRequisition number {n}"


@pytest.fixture
def provider() -> FakeEverything:
    return FakeEverything()


@pytest.fixture
def client(settings: Settings, provider: FakeEverything) -> Iterator[TestClient]:
    with TestClient(create_app(multi_user(settings, compare_retry_base_delay=0), provider=provider)) as c:
        yield c


@pytest.fixture
def user(client: TestClient) -> AuthedClient:
    return AuthedClient(client, User("free.user@example.com"))


def resume_id(user: AuthedClient, pdf: bytes) -> str:
    return user.post("/resumes", files={"file": ("r.pdf", pdf, PDF)}).json()["id"]


def test_match_limit_message_and_no_gemini_call(user: AuthedClient, provider: FakeEverything, sample_pdf_bytes: bytes) -> None:
    rid = resume_id(user, sample_pdf_bytes)
    for n in range(10):
        assert user.post("/match", data={"jd_text": jd_variant(n), "resume_id": rid}).status_code == 202
    jd_calls = provider.count(JobDescription)

    blocked = user.post("/match", data={"jd_text": jd_variant(99), "resume_id": rid})
    assert blocked.status_code == 429
    detail = blocked.json()["detail"]
    assert detail["message"].startswith("You've used all 10 free matches this month.")
    assert detail["used"] == 10 and detail["limit"] == 10 and detail["action"] == "match"
    assert provider.count(JobDescription) == jd_calls  # checked before calling Gemini

    # A cached result (same resume + JD) costs nothing and still works at the limit.
    again = user.post("/match", data={"jd_text": jd_variant(3), "resume_id": rid})
    assert again.status_code == 202 and again.json()["cached"] is True
    assert user.get("/me").json()["usage"]["actions"]["match"]["used"] == 10


def test_parse_limit(user: AuthedClient, sample_pdf_bytes: bytes) -> None:
    for n in range(5):
        assert user.post("/resumes", files={"file": ("r.pdf", pdf_variant(sample_pdf_bytes, n), PDF)}).status_code == 202
    # The same file again is a cache hit - free.
    same = user.post("/resumes", files={"file": ("r.pdf", pdf_variant(sample_pdf_bytes, 0), PDF)})
    assert same.status_code == 202 and same.json()["cached"] is True
    blocked = user.post("/resumes", files={"file": ("r.pdf", pdf_variant(sample_pdf_bytes, 9), PDF)})
    assert blocked.status_code == 429
    assert blocked.json()["detail"]["message"].startswith("You've used all 5 free resume parses this month.")
    # Uploading a new PDF to Job Match needs a parse too -> blocked as well.
    via_match = user.post("/match", data={"jd_text": PYTHON_JD_TEXT}, files={"file": ("r.pdf", pdf_variant(sample_pdf_bytes, 8), PDF)})
    assert via_match.status_code == 429 and "resume parses" in via_match.json()["detail"]["message"]


def test_tailor_limit(user: AuthedClient, sample_pdf_bytes: bytes) -> None:
    rid = resume_id(user, sample_pdf_bytes)
    match_ids = [user.post("/match", data={"jd_text": jd_variant(n), "resume_id": rid}).json()["match_id"] for n in range(4)]
    for mid in match_ids[:3]:
        assert user.post("/tailor", json={"match_id": mid}).status_code == 202
    # Re-opening an existing tailored resume is free.
    assert user.post("/tailor", json={"match_id": match_ids[0]}).json()["existing"] is True
    blocked = user.post("/tailor", json={"match_id": match_ids[3]})
    assert blocked.status_code == 429
    assert blocked.json()["detail"]["message"].startswith("You've used all 3 free tailored resumes this month.")


def test_compare_limit(user: AuthedClient, sample_pdf_bytes: bytes) -> None:
    rid = resume_id(user, sample_pdf_bytes)
    jobs = json.dumps([{"text": PYTHON_JD_TEXT}, {"text": ANALYST_JD_TEXT}])
    for _ in range(2):
        assert user.post("/compare", data={"resume_id": rid, "jobs": jobs}).status_code == 202
    blocked = user.post("/compare", data={"resume_id": rid, "jobs": jobs})
    assert blocked.status_code == 429
    assert blocked.json()["detail"]["message"].startswith("You've used all 2 free comparisons this month.")


def test_pro_plan_is_unlimited(client: TestClient, user: AuthedClient, sample_pdf_bytes: bytes) -> None:
    user.get("/me")  # creates the profile
    client.app.state.profiles.update(user.user.id, plan="pro")
    rid = resume_id(user, sample_pdf_bytes)
    jobs = json.dumps([{"text": PYTHON_JD_TEXT}, {"text": ANALYST_JD_TEXT}])
    for _ in range(3):
        assert user.post("/compare", data={"resume_id": rid, "jobs": jobs}).status_code == 202
    usage = user.get("/me").json()["usage"]
    assert usage["plan"] == "pro" and usage["actions"]["compare"] == {"used": 3, "limit": None, "label": "comparisons"}


def test_limits_reset_each_month_and_come_from_config(settings: Settings, sample_pdf_bytes: bytes) -> None:
    custom = multi_user(settings, free_comparisons_per_month=1)
    with TestClient(create_app(custom, provider=FakeEverything())) as client:
        user = AuthedClient(client, User("monthly@example.com"))
        rid = resume_id(user, sample_pdf_bytes)
        # Two comparisons last month don't count this month.
        last_month = month_window()[0] - timedelta(days=3)
        with Session(client.app.state.engine) as session:
            session.add_all([UsageRecord(user_id=user.user.id, action="compare", created_at=last_month) for _ in range(2)])
            session.commit()
        jobs = json.dumps([{"text": PYTHON_JD_TEXT}, {"text": ANALYST_JD_TEXT}])
        assert user.post("/compare", data={"resume_id": rid, "jobs": jobs}).status_code == 202
        blocked = user.post("/compare", data={"resume_id": rid, "jobs": jobs})
        assert blocked.json()["detail"]["message"].startswith("You've used your 1 free comparison this month.")


def test_usage_is_per_user(client: TestClient, user: AuthedClient, sample_pdf_bytes: bytes) -> None:
    for n in range(5):
        user.post("/resumes", files={"file": ("r.pdf", pdf_variant(sample_pdf_bytes, n), PDF)})
    other = AuthedClient(client, User("other@example.com"))
    assert other.post("/resumes", files={"file": ("r.pdf", pdf_variant(sample_pdf_bytes, 7), PDF)}).status_code == 202


def test_month_window() -> None:
    start, end = month_window(datetime(2026, 12, 18, 9, 30, tzinfo=timezone.utc))
    assert start == datetime(2026, 12, 1, tzinfo=timezone.utc) and end == datetime(2027, 1, 1, tzinfo=timezone.utc)


def test_rate_limit_per_user(settings: Settings, sample_pdf_bytes: bytes) -> None:
    limited = multi_user(settings, rate_limit_heavy="3/minute", rate_limit_default="5/minute")
    with TestClient(create_app(limited, provider=FakeEverything())) as client:
        alice = AuthedClient(client, User("alice@example.com"))
        for _ in range(3):
            assert alice.post("/jd/fetch", json={"url": "ftp://not-allowed"}).status_code == 422
        blocked = alice.post("/jd/fetch", json={"url": "ftp://not-allowed"})
        assert blocked.status_code == 429 and blocked.headers["retry-after"] == "60"
        # Light requests have their own (higher) budget, and other users are unaffected.
        assert alice.get("/resumes").status_code == 200
        bob = AuthedClient(client, User("bob@example.com"))
        assert bob.post("/jd/fetch", json={"url": "ftp://not-allowed"}).status_code == 422
        for _ in range(4):
            alice.get("/resumes")
        assert alice.get("/resumes").status_code == 429
