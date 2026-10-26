"""Account: delete everything (rows + files + login), download my data, auto-delete settings and the daily clean-up,
plus upload validation."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pymupdf
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.cleanup import run_cleanup
from app.config import Settings
from app.db import USER_TABLES, ProfileRecord, ResumeRecord, UsageRecord
from app.main import create_app
from tests.auth_helpers import AuthedClient, User, multi_user
from tests.compare_fakes import ANALYST_JD_TEXT
from tests.match_fakes import PYTHON_JD_TEXT
from tests.test_auth import FakeEverything

PDF = "application/pdf"


class FakeAdmin:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete_user(self, user_id: str) -> None:
        self.deleted.append(user_id)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(multi_user(settings, compare_retry_base_delay=0), provider=FakeEverything())) as c:
        c.app.state.supabase_admin = FakeAdmin()
        yield c


def fill_account(user: AuthedClient, pdf: bytes) -> dict:
    """One of everything, including stored PDFs and export files."""
    rid = user.post("/resumes", files={"file": ("cv.pdf", pdf, PDF)}).json()["id"]
    mid = user.post("/match", data={"jd_text": PYTHON_JD_TEXT, "resume_id": rid}).json()["match_id"]
    tid = user.post("/tailor", json={"match_id": mid}).json()["tailor_id"]
    jobs = json.dumps([{"text": PYTHON_JD_TEXT}, {"text": ANALYST_JD_TEXT}])
    cid = user.post("/compare", data={"resume_id": rid, "jobs": jobs}).json()["compare_id"]
    assert user.get(f"/compare/{cid}/export?format=csv").status_code == 200
    return {"resume_id": rid, "match_id": mid, "tailor_id": tid, "compare_id": cid}


def rows_of(client: TestClient, user_id: str) -> dict[str, int]:
    with Session(client.app.state.engine) as session:
        counts = {m.__tablename__: len(session.exec(select(m).where(m.user_id == user_id)).all()) for m in (*USER_TABLES, UsageRecord)}
        counts["profiles"] = 1 if session.get(ProfileRecord, user_id) else 0
    return counts


def files_of(settings_dir, user_id: str) -> list[str]:
    return [str(p) for bucket in ("resumes", "exports") for p in (settings_dir / bucket / user_id).glob("*")] if settings_dir.exists() else []


def test_delete_account_removes_rows_files_and_login(client: TestClient, settings: Settings, sample_pdf_bytes: bytes) -> None:
    alice = AuthedClient(client, User("alice@example.com"))
    bob = AuthedClient(client, User("bob@example.com"))
    fill_account(alice, sample_pdf_bytes)
    fill_account(bob, sample_pdf_bytes)
    storage_dir = settings.storage_dir
    assert all(rows_of(client, alice.user.id)[t] > 0 for t in ("resumes", "matches", "tailored_resumes", "comparisons", "comparison_jobs", "usage", "profiles"))
    assert len(files_of(storage_dir, alice.user.id)) == 2  # the PDF + the CSV export

    assert alice.delete("/me", json={"confirm": "delete"}).status_code == 400  # must type DELETE exactly
    result = alice.delete("/me", json={"confirm": "DELETE"})
    assert result.status_code == 200 and result.json()["deleted"] is True and result.json()["files"] == 2

    assert set(rows_of(client, alice.user.id).values()) == {0}
    assert files_of(storage_dir, alice.user.id) == []
    assert client.app.state.supabase_admin.deleted == [alice.user.id]

    # Bob is untouched.
    assert all(n > 0 for n in rows_of(client, bob.user.id).values())
    assert len(files_of(storage_dir, bob.user.id)) == 2
    assert len(bob.get("/resumes").json()) == 1


def test_download_all_my_data(client: TestClient, sample_pdf_bytes: bytes) -> None:
    alice = AuthedClient(client, User("alice@example.com"))
    ids = fill_account(alice, sample_pdf_bytes)
    AuthedClient(client, User("bob@example.com")).post("/resumes", files={"file": ("b.pdf", sample_pdf_bytes + b"\n%b\n", PDF)})

    response = alice.get("/me/export")
    assert response.status_code == 200 and "attachment" in response.headers["content-disposition"]
    data = response.json()
    assert data["profile"]["email"] == "alice@example.com"
    assert [r["id"] for r in data["resumes"]] == [ids["resume_id"]]
    assert data["resumes"][0]["parsed_json"]["basics"]["name"] == "Priya Sharma"
    assert ids["match_id"] in [m["id"] for m in data["matches"]]
    assert [t["id"] for t in data["tailored_resumes"]] == [ids["tailor_id"]]
    assert [c["id"] for c in data["comparisons"]] == [ids["compare_id"]] and len(data["comparison_jobs"]) == 2
    assert {u["action"] for u in data["usage"]} >= {"parse", "match", "tailor", "compare", "export"}
    assert all(r["user_id"] == alice.user.id for table in ("resumes", "matches", "usage") for r in data[table])


def test_retention_setting(client: TestClient) -> None:
    alice = AuthedClient(client, User("alice@example.com"))
    assert alice.get("/me").json()["retention_days"] is None
    assert alice.put("/me/settings", json={"retention_days": 30}).json()["retention_days"] == 30
    assert alice.put("/me/settings", json={"retention_days": None}).json()["retention_days"] is None
    assert alice.put("/me/settings", json={"retention_days": 7}).status_code == 400


def test_daily_cleanup_deletes_old_resumes_and_exports(client: TestClient, settings: Settings, sample_pdf_bytes: bytes) -> None:
    state = client.app.state
    alice = AuthedClient(client, User("alice@example.com"))
    bob = AuthedClient(client, User("bob@example.com"))
    old = fill_account(alice, sample_pdf_bytes)
    new_rid = alice.post("/resumes", files={"file": ("new.pdf", sample_pdf_bytes + b"\n%new\n", PDF)}).json()["id"]
    bob_ids = fill_account(bob, sample_pdf_bytes)
    alice.put("/me/settings", json={"retention_days": 30})  # Bob keeps "never"

    # Age Alice's first resume and Bob's to 45 days; age the export files to 2 days.
    old_time = datetime.now(timezone.utc) - timedelta(days=45)
    with Session(state.engine) as session:
        for rid in (old["resume_id"], bob_ids["resume_id"]):
            record = session.get(ResumeRecord, rid)
            record.created_at = old_time
            session.add(record)
        session.commit()
    for path in (settings.storage_dir / "exports").rglob("*.csv"):
        stamp = time.time() - 2 * 86400
        os.utime(path, (stamp, stamp))

    report = run_cleanup(state.profiles, state.accounts, state.storage)
    assert report["resumes_deleted"] == 1 and report["files_deleted"] == 1 and report["exports_deleted"] == 2 and report["errors"] == 0

    assert alice.get(f"/resumes/{old['resume_id']}").status_code == 404
    assert alice.get(f"/match/{old['match_id']}").status_code == 404  # derived data goes too
    assert alice.get(f"/tailor/{old['tailor_id']}").status_code == 404
    assert alice.get(f"/compare/{old['compare_id']}").status_code == 404
    assert alice.get(f"/resumes/{new_rid}").status_code == 200  # newer than 30 days
    assert bob.get(f"/resumes/{bob_ids['resume_id']}").status_code == 200  # "never" auto-delete
    assert len(list((settings.storage_dir / "resumes" / alice.user.id).glob("*.pdf"))) == 1
    assert list((settings.storage_dir / "exports").rglob("*.csv")) == []


# ---------------------------------------------------------------------------
# Upload validation
# ---------------------------------------------------------------------------


def _pdf_with_pages(n: int) -> bytes:
    doc = pymupdf.open()
    for i in range(n):
        doc.new_page().insert_text((72, 72), f"Page {i + 1} Jane Doe, Python developer")
    return doc.tobytes()


def test_upload_validation(client: TestClient, settings: Settings) -> None:
    alice = AuthedClient(client, User("alice@example.com"))

    def upload(data: bytes, name: str = "cv.pdf"):
        return alice.post("/resumes", files={"file": (name, data, PDF)})

    too_many = upload(_pdf_with_pages(11))
    assert too_many.status_code == 400 and "at most 10" in too_many.json()["detail"]
    assert upload(_pdf_with_pages(10)).status_code == 202
    fake = upload(b"<html>this is not a pdf</html>" * 10)
    assert fake.status_code == 400 and "not a valid PDF" in fake.json()["detail"]
    assert upload(b"%PDF-1.4 hello", "cv.docx").status_code == 400
    big = upload(b"%PDF-1.7\n" + b"0" * (settings.max_file_bytes + 10))
    assert big.status_code == 400 and "limit is 10 MB" in big.json()["detail"]
    # Rejected uploads are not charged against the parse limit.
    assert alice.get("/me").json()["usage"]["actions"]["parse"]["used"] == 1
