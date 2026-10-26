"""End-to-end tests with the LLM mocked: ingest -> ... -> ResumeData, CLI, eval and the API."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import cli
import eval as evaluator
from app.config import Settings
from app.db import ResumeRepository, init_db, make_engine
from app.main import create_app
from app.pipeline.ingest import IngestError, ingest_bytes
from app.pipeline.llm_extract import LLMExtractionError, ResumeExtractor
from app.pipeline.run import ResumePipeline
from app.schema import ExtractedResume
from tests.sample_resume import LINKEDIN_URL, FakeProvider, build_sample_pdf

GOLDEN_DIR = Path(__file__).parent / "golden"


def test_full_pipeline_with_mocked_llm(settings: Settings, sample_pdf_bytes: bytes) -> None:
    provider = FakeProvider()
    pipeline = ResumePipeline(settings, provider=provider)
    outcome = pipeline.parse_bytes(sample_pdf_bytes, "sample_resume.pdf")
    data = outcome.data

    assert len(provider.calls) == 1  # short resume -> single call
    assert "Priya Sharma" in provider.calls[0]["prompt"]  # name hint + text were sent

    assert data.basics.name == "Priya Sharma"
    assert data.basics.email == "priya.sharma@example.com"
    assert data.basics.phone == "+919876543210"
    assert data.basics.links.linkedin == LINKEDIN_URL  # only available as a hidden hyperlink
    assert data.basics.links.github == "https://github.com/priyasharma"

    # The invented "Google" job was removed by grounding and reported.
    assert [j.company for j in data.experience] == ["Infosys Ltd", "Tata Consultancy Services"]
    assert any("Google" in w for w in data.meta.warnings)

    infosys, tcs = data.experience
    assert (infosys.start, infosys.end) == ("2021-07", "present")
    assert (tcs.start, tcs.end, tcs.duration_months) == ("2021-01", "2021-06", 6)
    assert tcs.skills_used == ["React", "JavaScript"]
    assert data.skills.tools == ["Docker", "Git", "AWS", "PostgreSQL"]
    assert data.education[0].degree_normalized == "Bachelor's - Engineering"
    assert data.certifications[0].date == "2022-03"

    assert data.meta.pages == 1 and data.meta.is_scanned is False
    assert set(data.meta.confidence) >= {"basics", "experience", "skills"}
    assert data.meta.needs_review is False
    assert outcome.raw_text.startswith("Priya Sharma")


def test_per_section_mode_and_retry(settings: Settings, sample_pdf_bytes: bytes) -> None:
    # Force per-section calls, and make the first answer invalid to trigger the retry.
    provider = FakeProvider(bad_first_answers=1)
    pipeline = ResumePipeline(replace(settings, llm_single_call_max_chars=0), provider=provider)
    data = pipeline.parse_bytes(sample_pdf_bytes, "sample_resume.pdf").data

    asked = {tuple(call["response_model"].model_fields) for call in provider.calls}
    assert ("experience",) in asked and ("skills",) in asked and ("basics",) in asked
    assert any("previous answer failed validation" in call["prompt"] for call in provider.calls)
    assert len(data.experience) == 2
    assert data.skills.technical == ["Python", "JavaScript", "SQL"]


def test_extractor_gives_up_after_one_retry() -> None:
    extractor = ResumeExtractor(FakeProvider(bad_first_answers=5))
    with pytest.raises(LLMExtractionError):
        extractor.call(ExtractedResume, "task", "text")


def test_cache_returns_previous_result(settings: Settings, sample_pdf_bytes: bytes) -> None:
    engine = make_engine(settings.database_url)
    init_db(engine)
    provider = FakeProvider()
    pipeline = ResumePipeline(settings, provider=provider, repository=ResumeRepository(engine))
    first = pipeline.parse_bytes(sample_pdf_bytes, "a.pdf")
    second = pipeline.parse_bytes(sample_pdf_bytes, "a-copy.pdf")
    assert not first.cached and second.cached
    assert len(provider.calls) == 1
    assert second.data == first.data


@pytest.mark.parametrize(
    "data,name,message",
    [
        (b"hello", "notes.txt", "Only .pdf"),
        (b"not a pdf at all", "fake.pdf", "not a valid PDF"),
        (b"%PDF-1.7\n garbage garbage", "broken.pdf", "corrupt"),
    ],
)
def test_ingest_rejects_bad_files(data: bytes, name: str, message: str) -> None:
    with pytest.raises(IngestError, match=message):
        ingest_bytes(data, name, max_bytes=10 * 1024 * 1024)


def test_ingest_rejects_large_and_encrypted(sample_pdf_bytes: bytes) -> None:
    with pytest.raises(IngestError, match="limit"):
        ingest_bytes(sample_pdf_bytes, "big.pdf", max_bytes=100)

    import pymupdf

    with pymupdf.open(stream=sample_pdf_bytes, filetype="pdf") as doc:
        encrypted = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="owner")
    with pytest.raises(IngestError, match="encrypted"):
        ingest_bytes(encrypted, "locked.pdf", max_bytes=10 * 1024 * 1024)


def test_cli_batch_survives_bad_pdf(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    in_dir, out_dir = tmp_path / "in", tmp_path / "out"
    build_sample_pdf(in_dir / "sample_resume.pdf")
    (in_dir / "broken.pdf").write_bytes(b"%PDF-1.4 this is not really a pdf")

    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "ResumePipeline", lambda s, repository=None: ResumePipeline(s, FakeProvider(), repository))
    exit_code = cli.main(["--input", str(in_dir), "--output", str(out_dir)])

    printed = capsys.readouterr().out
    assert exit_code == 1  # one failure
    assert "broken.pdf" in printed and "FAILED" in printed
    assert "Priya Sharma" in printed
    assert (out_dir / "sample_resume.json").exists()

    # The output matches the hand-labelled golden file perfectly.
    pred = json.loads((out_dir / "sample_resume.json").read_text(encoding="utf-8"))
    gold = json.loads((GOLDEN_DIR / "sample_resume.json").read_text(encoding="utf-8"))
    pred["meta"]["total_experience_months"] = gold["meta"]["total_experience_months"]  # depends on today's date
    scores = evaluator.evaluate_pair(pred, gold)
    assert all(score == 1.0 for score in scores.values()), scores


def test_api_upload_and_fetch(settings: Settings, sample_pdf_bytes: bytes) -> None:
    app = create_app(settings, provider=FakeProvider())
    with TestClient(app) as client:
        assert client.get("/health").json()["database"] is True

        bad = client.post("/resumes", files={"file": ("notes.txt", b"hi", "text/plain")})
        assert bad.status_code == 400

        resp = client.post("/resumes", files={"file": ("sample_resume.pdf", sample_pdf_bytes, "application/pdf")})
        assert resp.status_code == 202
        resume_id = resp.json()["id"]

        # TestClient runs background tasks before returning, so the result is ready.
        result = client.get(f"/resumes/{resume_id}").json()
        assert result["status"] == "done", result["error"]
        assert result["data"]["basics"]["name"] == "Priya Sharma"

        listing = client.get("/resumes").json()
        assert listing[0]["name"] == "Priya Sharma" and listing[0]["needs_review"] is False

        again = client.post("/resumes", files={"file": ("copy.pdf", sample_pdf_bytes, "application/pdf")}).json()
        assert again == {"id": resume_id, "status": "done", "cached": True}

        assert client.get("/resumes/does-not-exist").status_code == 404


def test_api_records_failures(settings: Settings, sample_pdf_bytes: bytes) -> None:
    class BrokenProvider:
        name = "broken"

        def complete_json(self, system: str, prompt: str, response_model: type) -> str:
            raise RuntimeError("LLM is down")

    app = create_app(settings, provider=BrokenProvider())
    with TestClient(app) as client:
        resume_id = client.post(
            "/resumes", files={"file": ("sample_resume.pdf", sample_pdf_bytes, "application/pdf")}
        ).json()["id"]
        result = client.get(f"/resumes/{resume_id}").json()
        assert result["status"] == "failed"
        assert "LLM is down" in result["error"]
