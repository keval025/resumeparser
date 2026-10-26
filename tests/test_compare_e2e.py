"""End-to-end tests for Compare Jobs with Gemini mocked: 3 sample JDs (AI PM, Product Analyst, Python Developer)."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.match_schema import JobDescription, RelevanceRatings, Suggestions
from app.pipeline.compare import CompareSummary
from app.pipeline.jd_sources import URL_ERROR, JDSourceError
from tests.compare_fakes import ANALYST_JD_TEXT, SUMMARY_LINES, FakeCompareProvider
from tests.match_fakes import AI_PM_JD_TEXT, PYTHON_JD_TEXT
from tests.downloads import follow
from tests.test_render import needs_chromium

PDF = "application/pdf"
GOOD_URL = "https://jobs.example.com/python-developer"
BAD_URL = "https://www.linkedin.com/jobs/view/999"


@pytest.fixture
def fast_settings(settings: Settings) -> Settings:
    return replace(settings, compare_retry_base_delay=0, match_semantic_similarity=False)


async def fake_fetcher(url: str) -> str:
    if url == GOOD_URL:
        return PYTHON_JD_TEXT
    raise JDSourceError(URL_ERROR)


def start(provider: FakeCompareProvider, settings: Settings) -> TestClient:
    client = TestClient(create_app(settings, provider=provider))
    client.__enter__()
    client.app.state.compare_service.fetcher = fake_fetcher
    return client


def parse_resume(client: TestClient, pdf: bytes) -> str:
    created = client.post("/resumes", files={"file": ("sample_resume.pdf", pdf, PDF)}).json()
    assert client.get(f"/resumes/{created['id']}").json()["status"] == "done"
    return created["id"]


def three_jobs() -> list[dict]:
    return [
        # Text that the UI fetched from a link: scored as text, the link is kept.
        {"text": AI_PM_JD_TEXT, "source_url": "careers.nimbus.example/ai-pm", "label": "Nimbus - AI PM",
         "deadline": "2026-12-15", "location": "Bengaluru"},
        {"file_index": 0, "label": "Swiggy - Product Analyst", "deadline": "2026-11-30", "salary": "18-24 LPA"},
        {"url": GOOD_URL, "label": "FinStack - Python Dev"},
    ]


def compare(client: TestClient, resume_id: str, jobs: list[dict], files: list[tuple] | None = None) -> dict:
    response = client.post(
        "/compare",
        data={"resume_id": resume_id, "jobs": json.dumps(jobs)},
        files=[("job_files", f) for f in (files or [])] or None,
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert set(body) == {"compare_id", "status"}
    # TestClient runs the background task before returning, so the comparison has finished.
    return client.get(f"/compare/{body['compare_id']}").json()


ANALYST_FILE = ("swiggy_analyst.txt", ANALYST_JD_TEXT.encode("utf-8"), "text/plain")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_compare_three_jobs_end_to_end(fast_settings: Settings, sample_pdf_bytes: bytes) -> None:
    provider = FakeCompareProvider()
    client = start(provider, fast_settings)
    try:
        resume_id = parse_resume(client, sample_pdf_bytes)
        res = compare(client, resume_id, three_jobs(), [ANALYST_FILE])

        assert res["status"] == "done", res
        assert res["candidate_name"] == "Priya Sharma"
        assert res["progress"] == {"total": 3, "done": 3, "failed": 0}
        assert [j["status"] for j in res["jobs"]] == ["done"] * 3
        assert [j["source"] for j in res["jobs"]] == ["url", "file", "url"]
        assert res["jobs"][0]["url"] == "https://careers.nimbus.example/ai-pm"
        assert res["jobs"][1]["source_name"] == "swiggy_analyst.txt" and res["jobs"][1]["salary"] == "18-24 LPA"

        ranking = res["insights"]["ranking"]
        assert [r["rank"] for r in ranking] == [1, 2, 3]
        scores = [r["score"] for r in ranking]
        assert scores == sorted(scores, reverse=True)
        # The sample resume is a Python backend developer: that job must rank first.
        assert ranking[0]["name"] == "FinStack - Python Dev" and ranking[0]["company"] == "FinStack Technologies"
        assert {r["name"] for r in ranking} == {"Nimbus - AI PM", "Swiggy - Product Analyst", "FinStack - Python Dev"}
        for r in ranking:
            assert r["label"] in {"Excellent", "Strong", "Moderate", "Low"}
            assert r["recommendation"] in {"Apply now", "Tailor, then apply", "Stretch / skip"}
            assert r["effort"]["level"] in {"Low", "Medium", "High"}
            assert r["effort"]["points_needed"] == max(0, 80 - r["score"])
            assert len(r["top_missing"]) <= 3
            assert set(r["components"]) == {
                "required_skills", "preferred_skills", "experience_relevance", "years_experience",
                "education", "keywords", "resume_quality",
            }
        ai_pm = next(r for r in ranking if r["name"] == "Nimbus - AI PM")
        assert ai_pm["recommendation"] == "Stretch / skip" and ai_pm["deadline"] == "2026-12-15"

        insights = res["insights"]
        assert all(g["missing_in"] / g["of"] >= 0.5 for g in insights["common_gaps"])
        assert all(s["matched_in"] / s["of"] > 0.5 for s in insights["strengths"])
        assert "SQL" in [s["skill"] for s in insights["strengths"]]  # in all three JDs and on the resume
        heat = insights["heatmap"]
        assert [j["name"] for j in heat["jobs"]] == [r["name"] for r in ranking]
        assert 0 < len(heat["skills"]) <= 20
        assert all(len(s["cells"]) == 3 and set(s["cells"]) <= {"required", "preferred", "none"} for s in heat["skills"])

        assert res["summary"] == "\n".join(SUMMARY_LINES) and res["summary_source"] == "gemini"
        assert res["summary_stale"] is False
        summary_prompt = next(c["prompt"] for c in provider.calls if c["response_model"] is CompareSummary)
        assert "FinStack - Python Dev" in summary_prompt and "learn_these_first" in summary_prompt

        # Each job is a normal Job Match: "View full match" and Tailor work on it.
        match = client.get(f"/match/{ranking[0]['match_id']}").json()
        assert match["status"] == "done" and match["score"] == ranking[0]["score"]
        assert client.post("/tailor", json={"match_id": ranking[0]["match_id"]}).status_code == 202

        history = client.get("/compares").json()
        assert len(history) == 1 and history[0]["jobs"] == 3 and history[0]["top_job"] == "FinStack - Python Dev"
        assert history[0]["top_score"] == ranking[0]["score"]
    finally:
        client.__exit__(None, None, None)


def test_compare_with_resume_upload(fast_settings: Settings, sample_pdf_bytes: bytes) -> None:
    provider = FakeCompareProvider()
    client = start(provider, fast_settings)
    try:
        jobs = [{"text": PYTHON_JD_TEXT}, {"text": ANALYST_JD_TEXT}]
        created = client.post(
            "/compare", data={"jobs": json.dumps(jobs)}, files={"file": ("sample_resume.pdf", sample_pdf_bytes, PDF)}
        ).json()
        res = client.get(f"/compare/{created['compare_id']}").json()
        assert res["status"] == "done" and res["resume_id"] and res["candidate_name"] == "Priya Sharma"
        assert res["progress"]["done"] == 2
        # Unlabelled jobs are named after the parsed title / company.
        assert {r["name"] for r in res["insights"]["ranking"]} == {
            "Python Developer (Backend) - FinStack Technologies", "Product Analyst - Swiggy",
        }
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Failure isolation, cache, retries, concurrency
# ---------------------------------------------------------------------------


def test_failing_jobs_do_not_break_the_batch(fast_settings: Settings, sample_pdf_bytes: bytes) -> None:
    provider = FakeCompareProvider(fail_jd_marker="Nimbus Health AI")  # Gemini fails on the AI PM JD
    client = start(provider, fast_settings)
    try:
        resume_id = parse_resume(client, sample_pdf_bytes)
        jobs = [
            {"url": BAD_URL, "label": "LinkedIn job"},
            {"text": AI_PM_JD_TEXT, "label": "AI PM"},
            {"text": ANALYST_JD_TEXT, "label": "Analyst"},
            {"text": PYTHON_JD_TEXT, "label": "Python"},
        ]
        res = compare(client, resume_id, jobs)
        status = {j["label"]: (j["status"], j["error"]) for j in res["jobs"]}
        assert status["LinkedIn job"] == ("failed", URL_ERROR)
        assert status["AI PM"][0] == "failed" and "simulated failure" in status["AI PM"][1]
        assert status["Analyst"] == ("done", None) and status["Python"] == ("done", None)

        assert res["status"] == "done" and res["progress"] == {"total": 4, "done": 2, "failed": 2}
        assert [r["name"] for r in res["insights"]["ranking"]] == ["Python", "Analyst"]
        assert res["summary"]  # still summarised from the jobs that worked
        # The failed job's match is marked failed too (it shows up correctly in Job Match history).
        failed_match = next(j["match_id"] for j in res["jobs"] if j["label"] == "AI PM")
        assert client.get(f"/match/{failed_match}").json()["status"] == "failed"

        csv_file = follow(client, client.get(f"/compare/{res['compare_id']}/export?format=csv"))
        csv_rows = list(csv.DictReader(io.StringIO(csv_file.text.lstrip("﻿"))))
        assert [r["job"] for r in csv_rows] == ["Python", "Analyst", "LinkedIn job", "AI PM"]
        assert csv_rows[2]["status"] == "failed" and csv_rows[2]["error"] == URL_ERROR
    finally:
        client.__exit__(None, None, None)


def test_rerunning_the_same_comparison_makes_no_gemini_calls(fast_settings: Settings, sample_pdf_bytes: bytes) -> None:
    provider = FakeCompareProvider()
    client = start(provider, fast_settings)
    try:
        resume_id = parse_resume(client, sample_pdf_bytes)
        first = compare(client, resume_id, three_jobs(), [ANALYST_FILE])
        calls_after_first = len(provider.calls)
        assert provider.count(JobDescription) == 3 and provider.count(CompareSummary) == 1

        second = compare(client, resume_id, three_jobs(), [ANALYST_FILE])
        assert len(provider.calls) == calls_after_first  # no JD parsing, ratings, suggestions or summary calls
        assert second["compare_id"] != first["compare_id"]
        assert all(j["cached"] for j in second["jobs"])
        assert [j["match_id"] for j in second["jobs"]] == [j["match_id"] for j in first["jobs"]]
        def without_ids(ranking: list[dict]) -> list[dict]:
            return [{k: v for k, v in r.items() if k != "job_id"} for r in ranking]

        assert without_ids(second["insights"]["ranking"]) == without_ids(first["insights"]["ranking"])
        assert second["summary"] == first["summary"]
    finally:
        client.__exit__(None, None, None)


def test_same_jd_twice_in_one_comparison_is_scored_once(fast_settings: Settings, sample_pdf_bytes: bytes) -> None:
    provider = FakeCompareProvider()
    client = start(provider, fast_settings)
    try:
        resume_id = parse_resume(client, sample_pdf_bytes)
        res = compare(client, resume_id, [{"text": PYTHON_JD_TEXT}, {"text": "  " + PYTHON_JD_TEXT + "\n\n"}])
        assert res["progress"]["done"] == 2 and provider.count(JobDescription) == 1
        assert res["jobs"][0]["match_id"] == res["jobs"][1]["match_id"]
    finally:
        client.__exit__(None, None, None)


def test_rate_limits_are_retried_with_backoff(fast_settings: Settings, sample_pdf_bytes: bytes) -> None:
    provider = FakeCompareProvider()
    client = start(provider, fast_settings)
    try:
        resume_id = parse_resume(client, sample_pdf_bytes)
        provider.rate_limited = 3  # the next 3 Gemini calls answer "429"
        res = compare(client, resume_id, [{"text": PYTHON_JD_TEXT}, {"text": ANALYST_JD_TEXT}])
        assert res["progress"] == {"total": 2, "done": 2, "failed": 0}
        assert sum(1 for c in provider.calls if c.get("rate_limited")) == 3
    finally:
        client.__exit__(None, None, None)


def test_rate_limit_gives_up_after_the_last_attempt(fast_settings: Settings, sample_pdf_bytes: bytes) -> None:
    provider = FakeCompareProvider()
    client = start(provider, replace(fast_settings, compare_retry_attempts=2, compare_concurrency=1))
    try:
        resume_id = parse_resume(client, sample_pdf_bytes)
        provider.rate_limited = 2  # both attempts of the first job hit the limit
        res = compare(client, resume_id, [{"text": PYTHON_JD_TEXT, "label": "first"}, {"text": ANALYST_JD_TEXT, "label": "second"}])
        by_label = {j["label"]: j for j in res["jobs"]}
        assert by_label["first"]["status"] == "failed" and "rate limit" in by_label["first"]["error"]
        assert by_label["second"]["status"] == "done"
    finally:
        client.__exit__(None, None, None)


def test_at_most_three_jobs_use_gemini_at_once(fast_settings: Settings, sample_pdf_bytes: bytes) -> None:
    provider = FakeCompareProvider(delay=0.05)
    client = start(provider, fast_settings)
    try:
        resume_id = parse_resume(client, sample_pdf_bytes)
        jobs = [{"text": f"{PYTHON_JD_TEXT}\nRequisition {i}"} for i in range(6)]
        res = compare(client, resume_id, jobs)
        assert res["progress"]["done"] == 6
        assert 1 < provider.max_in_flight <= 3
        assert provider.max_jd_in_flight <= 3
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Summary safety net
# ---------------------------------------------------------------------------


def test_summary_with_invented_numbers_falls_back_to_rules(fast_settings: Settings, sample_pdf_bytes: bytes) -> None:
    provider = FakeCompareProvider(summary_lines=["Apply to FinStack: 97% fit.", "Then Swiggy.", "Skip the rest."])
    client = start(provider, fast_settings)
    try:
        resume_id = parse_resume(client, sample_pdf_bytes)
        res = compare(client, resume_id, [{"text": PYTHON_JD_TEXT}, {"text": ANALYST_JD_TEXT}])
        assert res["summary_source"] == "rules"
        assert res["summary"].startswith("Apply first to Python Developer (Backend) - FinStack Technologies")
        assert "97" not in res["summary"]
    finally:
        client.__exit__(None, None, None)


def test_summary_falls_back_when_gemini_fails(fast_settings: Settings, sample_pdf_bytes: bytes) -> None:
    provider = FakeCompareProvider()
    provider.fail_on = CompareSummary  # FakeMatchProvider raises for this model
    client = start(provider, fast_settings)
    try:
        resume_id = parse_resume(client, sample_pdf_bytes)
        res = compare(client, resume_id, [{"text": PYTHON_JD_TEXT}, {"text": ANALYST_JD_TEXT}])
        assert res["status"] == "done" and res["summary_source"] == "rules" and res["summary"]
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Add / remove jobs
# ---------------------------------------------------------------------------


def test_add_and_remove_jobs(fast_settings: Settings, sample_pdf_bytes: bytes) -> None:
    provider = FakeCompareProvider()
    client = start(provider, fast_settings)
    try:
        resume_id = parse_resume(client, sample_pdf_bytes)
        res = compare(client, resume_id, [{"text": PYTHON_JD_TEXT, "label": "Python"}, {"text": ANALYST_JD_TEXT, "label": "Analyst"}])
        cid = res["compare_id"]
        jd_calls = provider.count(JobDescription)

        added = client.post(f"/compare/{cid}/jobs", data={"text": AI_PM_JD_TEXT, "label": "AI PM", "deadline": "2026-12-01"})
        assert added.status_code == 202
        after_add = client.get(f"/compare/{cid}").json()
        assert provider.count(JobDescription) == jd_calls + 1  # only the new job was scored
        assert [j["label"] for j in after_add["jobs"]] == ["Python", "Analyst", "AI PM"]
        assert after_add["progress"]["done"] == 3 and len(after_add["insights"]["ranking"]) == 3
        assert after_add["status"] == "done" and not after_add["summary_stale"]

        # Add from a file, and a URL that cannot be read.
        files = {"file": ("analyst.txt", ANALYST_JD_TEXT.encode(), "text/plain")}
        assert client.post(f"/compare/{cid}/jobs", data={"label": "Analyst (file)"}, files=files).status_code == 202
        assert client.post(f"/compare/{cid}/jobs", data={"url": BAD_URL}).status_code == 202
        res = client.get(f"/compare/{cid}").json()
        assert res["jobs"][-1]["status"] == "failed" and res["jobs"][-1]["error"] == URL_ERROR

        bad_job = res["jobs"][-1]["job_id"]
        removed = client.delete(f"/compare/{cid}/jobs/{bad_job}")
        assert removed.status_code == 200 and len(removed.json()["jobs"]) == 4
        ai_pm = next(j["job_id"] for j in res["jobs"] if j["label"] == "AI PM")
        client.delete(f"/compare/{cid}/jobs/{ai_pm}")
        final = client.get(f"/compare/{cid}").json()
        # Same JD text as "Analyst" -> same cached score; the tie goes to the job added first.
        assert [r["name"] for r in final["insights"]["ranking"]] == ["Python", "Analyst", "Analyst (file)"]
        assert final["status"] == "done" and final["summary_stale"] is False

        assert client.delete(f"/compare/{cid}/jobs/{ai_pm}").status_code == 404
        assert client.delete(f"/compare/nope/jobs/{ai_pm}").status_code == 404
    finally:
        client.__exit__(None, None, None)


def test_add_job_respects_the_limit(fast_settings: Settings, sample_pdf_bytes: bytes) -> None:
    provider = FakeCompareProvider()
    client = start(provider, fast_settings)
    try:
        resume_id = parse_resume(client, sample_pdf_bytes)
        jobs = [{"text": f"{PYTHON_JD_TEXT}\nRequisition {i}"} for i in range(10)]
        res = compare(client, resume_id, jobs)
        response = client.post(f"/compare/{res['compare_id']}/jobs", data={"text": ANALYST_JD_TEXT})
        assert response.status_code == 400 and "at most 10" in response.json()["detail"]
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Validation, fetch preview, export
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "jobs, message",
    [
        ([{"text": PYTHON_JD_TEXT}], "between 2 and 10"),
        ([{"text": PYTHON_JD_TEXT}] * 11, "between 2 and 10"),
        ([{"text": PYTHON_JD_TEXT}, {"text": "too short"}], "Job 2: paste the full job description"),
        ([{"text": PYTHON_JD_TEXT}, {"text": PYTHON_JD_TEXT, "url": GOOD_URL}], "Job 2: give exactly one of"),
        ([{"text": PYTHON_JD_TEXT}, {}], "Job 2: give exactly one of"),
        ([{"text": PYTHON_JD_TEXT}, {"url": "ftp://x"}], "Job 2: Couldn't read this link"),
        ([{"text": PYTHON_JD_TEXT}, {"text": ANALYST_JD_TEXT, "deadline": "next friday"}], "deadline must be a date"),
        ([{"text": PYTHON_JD_TEXT}, {"file_index": 3}], "does not match an uploaded file"),
        ("not json", "JSON list"),
    ],
)
def test_input_validation(fast_settings: Settings, sample_pdf_bytes: bytes, jobs, message: str) -> None:
    client = start(FakeCompareProvider(), fast_settings)
    try:
        resume_id = parse_resume(client, sample_pdf_bytes)
        payload = jobs if isinstance(jobs, str) else json.dumps(jobs)
        response = client.post("/compare", data={"resume_id": resume_id, "jobs": payload})
        assert response.status_code == 400 and message in response.json()["detail"]
    finally:
        client.__exit__(None, None, None)


def test_resume_and_file_type_validation(fast_settings: Settings, sample_pdf_bytes: bytes) -> None:
    client = start(FakeCompareProvider(), fast_settings)
    try:
        jobs = json.dumps([{"text": PYTHON_JD_TEXT}, {"file_index": 0}])
        bad_file = [("job_files", ("jd.rtf", b"{\\rtf1 hello}", "application/rtf"))]
        assert "must be .txt, .pdf or .docx" in client.post("/compare", data={"resume_id": "x", "jobs": jobs}, files=bad_file).json()["detail"]
        two_jobs = json.dumps([{"text": PYTHON_JD_TEXT}, {"text": ANALYST_JD_TEXT}])
        assert client.post("/compare", data={"jobs": two_jobs}).status_code == 400  # no resume
        assert client.post("/compare", data={"resume_id": "missing", "jobs": two_jobs}).status_code == 404
        assert client.get("/compare/missing").status_code == 404
    finally:
        client.__exit__(None, None, None)


def test_fetch_preview_endpoint(fast_settings: Settings) -> None:
    client = start(FakeCompareProvider(), fast_settings)
    try:
        ok = client.post("/jd/fetch", json={"url": GOOD_URL})
        assert ok.status_code == 200 and "FinStack" in ok.json()["text"]
        bad = client.post("/jd/fetch", json={"url": BAD_URL})
        assert bad.status_code == 422 and bad.json()["detail"] == URL_ERROR
    finally:
        client.__exit__(None, None, None)


def test_export_csv(fast_settings: Settings, sample_pdf_bytes: bytes) -> None:
    client = start(FakeCompareProvider(), fast_settings)
    try:
        resume_id = parse_resume(client, sample_pdf_bytes)
        res = compare(client, resume_id, three_jobs(), [ANALYST_FILE])
        link = client.get(f"/compare/{res['compare_id']}/export?format=csv")
        assert link.json()["file_name"].startswith("job_comparison_Priya_Sharma_")
        response = follow(client, link)
        assert response.headers["content-type"].startswith("text/csv")
        rows = list(csv.DictReader(io.StringIO(response.text.lstrip("﻿"))))
        assert [int(r["rank"]) for r in rows] == [1, 2, 3]
        assert rows[0]["job"] == "FinStack - Python Dev" and rows[0]["recommendation"]
        assert {r["deadline"] for r in rows} == {"2026-12-15", "2026-11-30", ""}
        assert client.get(f"/compare/{res['compare_id']}/export?format=xls").status_code == 422
    finally:
        client.__exit__(None, None, None)


@needs_chromium
def test_export_pdf(fast_settings: Settings, sample_pdf_bytes: bytes) -> None:
    import pymupdf

    client = start(FakeCompareProvider(), fast_settings)
    try:
        resume_id = parse_resume(client, sample_pdf_bytes)
        res = compare(client, resume_id, three_jobs(), [ANALYST_FILE])
        response = follow(client, client.get(f"/compare/{res['compare_id']}/export?format=pdf"))
        assert response.content.startswith(b"%PDF")
        with pymupdf.open(stream=response.content, filetype="pdf") as doc:
            text = "\n".join(page.get_text() for page in doc)
        assert "Job comparison" in text and "FinStack - Python Dev" in text and "Learn these first" in text
    finally:
        client.__exit__(None, None, None)


def test_existing_features_untouched(fast_settings: Settings, sample_pdf_bytes: bytes) -> None:
    """A plain Job Match still works the same way next to Compare Jobs."""
    provider = FakeCompareProvider()
    client = start(provider, fast_settings)
    try:
        created = client.post("/match", data={"jd_text": PYTHON_JD_TEXT}, files={"file": ("r.pdf", sample_pdf_bytes, PDF)}).json()
        match = client.get(f"/match/{created['match_id']}").json()
        assert match["status"] == "done" and provider.count(RelevanceRatings) == 1 and provider.count(Suggestions) == 1
        assert client.get("/compares").json() == []
    finally:
        client.__exit__(None, None, None)
