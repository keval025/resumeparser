"""End-to-end tests for the job-match API with Gemini mocked: upload/resume_id + JD -> score + suggestions."""

from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import MatchRepository, ResumeRepository, init_db, make_engine
from app.main import create_app
from app.match_schema import JobDescription, RelevanceRatings, Suggestions
from app.pipeline.match_run import MatchService
from app.pipeline.run import ResumePipeline
from tests.match_fakes import AI_PM_JD_TEXT, PYTHON_JD_TEXT, FakeMatchProvider

PDF = "application/pdf"


def _post_file(client: TestClient, pdf: bytes, jd_text: str):
    return client.post("/match", data={"jd_text": jd_text}, files={"file": ("sample_resume.pdf", pdf, PDF)})


def test_match_upload_end_to_end(settings: Settings, sample_pdf_bytes: bytes) -> None:
    provider = FakeMatchProvider(embeddings=True)
    with TestClient(create_app(settings, provider=provider)) as client:
        created = _post_file(client, sample_pdf_bytes, PYTHON_JD_TEXT)
        assert created.status_code == 202
        body = created.json()
        assert body["status"] == "pending" and body["cached"] is False

        # TestClient runs background tasks before returning, so the result is ready.
        res = client.get(f"/match/{body['match_id']}").json()
        assert res["status"] == "done", res["error"]
        assert res["candidate_name"] == "Priya Sharma" and res["resume_id"]
        assert isinstance(res["score"], int) and 0 <= res["score"] <= 100
        assert res["label"] in {"Excellent match", "Strong", "Moderate", "Low"}

        breakdown = res["breakdown"]
        assert list(breakdown) == [
            "required_skills", "preferred_skills", "experience_relevance", "years_experience",
            "education", "keywords", "resume_quality",
        ]
        assert sum(c["max"] for c in breakdown.values()) == 100
        assert all({"score", "max", "details"} <= set(c) for c in breakdown.values())
        assert res["score"] == int(sum(c["score"] for c in breakdown.values()) + 0.5)

        assert "Python" in res["matched_skills"]
        assert res["missing_skills"]["required"] == []
        assert "Kubernetes" in res["missing_skills"]["preferred"]
        assert res["semantic_similarity"] == 0.5
        assert res["jd_parsed"]["title"] == "Python Developer (Backend)"
        assert res["jd_parsed"]["required_skills"][4] == "PostgreSQL"  # alias-normalised

        s = res["suggestions"]
        assert s["summary"] and s["suggested_summary"] and s["top_priorities"]
        assert "Kubernetes" in [k["skill"] for k in s["skills_to_learn"]]  # moved out of "add"
        assert any(w.startswith("invented_number_removed") for w in res["warnings"])

        # The parsed resume is saved too, so it can be re-used by id.
        assert client.get(f"/resumes/{res['resume_id']}").json()["status"] == "done"

        history = client.get("/matches").json()
        assert len(history) == 1 and history[0]["score"] == res["score"]
        assert history[0]["jd_title"] == "Python Developer (Backend)"


def test_match_cache_and_resume_id(settings: Settings, sample_pdf_bytes: bytes) -> None:
    provider = FakeMatchProvider()
    with TestClient(create_app(settings, provider=provider)) as client:
        first = _post_file(client, sample_pdf_bytes, PYTHON_JD_TEXT).json()
        match = client.get(f"/match/{first['match_id']}").json()
        calls_after_first = len(provider.calls)

        # Same file + same JD (different whitespace) -> stored result, no LLM calls.
        again = _post_file(client, sample_pdf_bytes, "  " + PYTHON_JD_TEXT.replace("\n", "\n\n") + "  ").json()
        assert again == {"match_id": first["match_id"], "status": "done", "cached": True}
        # Same resume by id + same JD -> also cached (keyed on the resume file hash).
        by_id = client.post("/match", data={"jd_text": PYTHON_JD_TEXT, "resume_id": match["resume_id"]}).json()
        assert by_id["cached"] is True and by_id["match_id"] == first["match_id"]
        assert len(provider.calls) == calls_after_first

        # Same resume, new JD -> a new match; the resume is not parsed again.
        other = client.post("/match", data={"jd_text": AI_PM_JD_TEXT, "resume_id": match["resume_id"]}).json()
        assert other["cached"] is False
        pm = client.get(f"/match/{other['match_id']}").json()
        assert pm["status"] == "done" and pm["jd_parsed"]["title"] == "AI Product Manager"
        assert pm["score"] < match["score"]  # a backend developer is a weaker fit for an AI PM role
        assert "LLMs" in pm["missing_skills"]["required"]
        new_calls = provider.calls[calls_after_first:]
        assert [c["response_model"] for c in new_calls] == [JobDescription, RelevanceRatings, Suggestions]
        assert len(client.get("/matches").json()) == 2


def test_parsed_jd_is_reused_for_another_resume(settings: Settings, sample_pdf_bytes: bytes) -> None:
    import pymupdf

    with pymupdf.open(stream=sample_pdf_bytes, filetype="pdf") as doc:
        doc.set_metadata({"title": "copy"})  # same content, different bytes -> different file hash
        other_pdf = doc.tobytes()
    provider = FakeMatchProvider()
    with TestClient(create_app(settings, provider=provider)) as client:
        _post_file(client, sample_pdf_bytes, PYTHON_JD_TEXT)
        second = _post_file(client, other_pdf, PYTHON_JD_TEXT).json()
        assert second["cached"] is False
        assert client.get(f"/match/{second['match_id']}").json()["status"] == "done"
    assert provider.count(JobDescription) == 1  # the JD was parsed once


def test_match_validation_errors(settings: Settings, sample_pdf_bytes: bytes) -> None:
    with TestClient(create_app(settings, provider=FakeMatchProvider())) as client:
        short = client.post("/match", data={"jd_text": "Python dev", "resume_id": "x"})
        assert short.status_code == 400 and "at least" in short.json()["detail"]

        neither = client.post("/match", data={"jd_text": PYTHON_JD_TEXT})
        assert neither.status_code == 400

        both = client.post(
            "/match", data={"jd_text": PYTHON_JD_TEXT, "resume_id": "x"},
            files={"file": ("a.pdf", sample_pdf_bytes, PDF)},
        )
        assert both.status_code == 400

        assert client.post("/match", data={"jd_text": PYTHON_JD_TEXT, "resume_id": "nope"}).status_code == 404
        bad_file = client.post("/match", data={"jd_text": PYTHON_JD_TEXT}, files={"file": ("a.txt", b"hello", "text/plain")})
        assert bad_file.status_code == 400
        assert client.get("/match/does-not-exist").status_code == 404
        assert client.get("/matches").json() == []


def test_match_failure_is_recorded(settings: Settings, sample_pdf_bytes: bytes) -> None:
    with TestClient(create_app(settings, provider=FakeMatchProvider(fail_on=Suggestions))) as client:
        match_id = _post_file(client, sample_pdf_bytes, PYTHON_JD_TEXT).json()["match_id"]
        res = client.get(f"/match/{match_id}").json()
        assert res["status"] == "failed"
        assert "Suggestions is down" in res["error"]
        assert res["score"] is None
        # A failed match is never served from the cache: retrying runs it again.
        retry = _post_file(client, sample_pdf_bytes, PYTHON_JD_TEXT).json()
        assert retry["cached"] is False and retry["match_id"] != match_id


def test_service_analysis_is_deterministic(settings: Settings, sample_pdf_bytes: bytes) -> None:
    settings = replace(settings, match_semantic_similarity=False)
    engine = make_engine(settings.database_url)
    init_db(engine)
    pipeline = ResumePipeline(settings, provider=FakeMatchProvider(), repository=ResumeRepository(engine))
    service = MatchService(settings, pipeline, ResumeRepository(engine), MatchRepository(engine))
    outcome = pipeline.parse_bytes(sample_pdf_bytes, "sample_resume.pdf")

    first = service.analyze(outcome.data, outcome.raw_text, PYTHON_JD_TEXT)
    second = service.analyze(outcome.data, outcome.raw_text, PYTHON_JD_TEXT)
    assert first.result.score == second.result.score
    assert first.result.model_dump() == second.result.model_dump()
    assert first.result.semantic_similarity is None


def test_web_ui_has_job_match_tab(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        page = client.get("/app").text
        assert "Job Match" in page and "/match" in page and "Analyze Match" in page
