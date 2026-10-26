"""Gemini provider (HTTP mocked), missing-key behaviour and the web UI route."""

from __future__ import annotations

import json
from typing import Any, Callable

import httpx
import pytest
from fastapi.testclient import TestClient
from google import genai
from google.genai import types

from app.config import Settings
from app.main import create_app
from app.pipeline.llm import LLMError, get_provider
from app.pipeline.llm.gemini_provider import GeminiProvider
from app.schema import SkillsSection


def _provider_with_mock(respond: Callable[[httpx.Request], httpx.Response]) -> tuple[GeminiProvider, list[dict[str, Any]]]:
    """A GeminiProvider whose HTTP calls go to ``respond`` instead of Google."""
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return respond(request)

    provider = GeminiProvider(api_key="test-key", model="gemini-2.5-flash")
    provider.client = genai.Client(
        api_key="test-key",
        http_options=types.HttpOptions(httpx_client=httpx.Client(transport=httpx.MockTransport(handler))),
    )
    return provider, seen


def _answer(text: str, finish: str = "STOP") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": finish}],
            "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 7},
        },
    )


def test_gemini_request_uses_structured_output_and_temperature_zero() -> None:
    payload = '{"skills": {"technical": ["Python"], "tools": [], "soft": [], "languages": []}}'
    provider, seen = _provider_with_mock(lambda req: _answer(payload))

    assert provider.complete_json("SYSTEM", "PROMPT", SkillsSection) == payload
    body = seen[0]
    config = body["generationConfig"]
    assert config["temperature"] == 0
    assert config["responseMimeType"] == "application/json"
    assert "skills" in config["responseJsonSchema"]["properties"]
    assert config["responseJsonSchema"]["additionalProperties"] is False  # extra="forbid" is sent as-is
    assert body["systemInstruction"]["parts"][0]["text"] == "SYSTEM"
    assert body["contents"][0]["parts"][0]["text"] == "PROMPT"


def test_gemini_truncated_answer_raises_clear_error() -> None:
    provider, _ = _provider_with_mock(lambda req: _answer('{"skills": {', finish="MAX_TOKENS"))
    with pytest.raises(LLMError, match="cut off"):
        provider.complete_json("s", "p", SkillsSection)


def test_gemini_bad_key_raises_clear_error() -> None:
    error = {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.", "status": "INVALID_ARGUMENT"}}
    provider, _ = _provider_with_mock(lambda req: httpx.Response(400, json=error))
    with pytest.raises(LLMError, match="rejected the API key"):
        provider.complete_json("s", "p", SkillsSection)


def test_missing_key_raises_readable_error(settings: Settings) -> None:
    with pytest.raises(LLMError, match="GEMINI_API_KEY is not set"):
        get_provider(settings)


def test_app_starts_without_key_and_reports_it(settings: Settings, sample_pdf_bytes: bytes) -> None:
    app = create_app(settings)  # no fake provider: the real Gemini provider, but no key
    with TestClient(app) as client:
        health = client.get("/health").json()
        assert health["llm_api_key_configured"] is False
        assert "GEMINI_API_KEY" in health["llm_message"]

        resume_id = client.post(
            "/resumes", files={"file": ("sample_resume.pdf", sample_pdf_bytes, "application/pdf")}
        ).json()["id"]
        result = client.get(f"/resumes/{resume_id}").json()
        assert result["status"] == "failed"
        assert result["error"].startswith("GEMINI_API_KEY is not set")


def test_web_ui_is_served(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        page = client.get("/app")
        assert page.status_code == 200
        assert "text/html" in page.headers["content-type"]
        assert "Resume Parser" in page.text and "/resumes" in page.text

        # CORS: a page on another localhost port may call the API.
        preflight = client.options(
            "/resumes",
            headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"},
        )
        assert preflight.headers.get("access-control-allow-origin") == "http://localhost:5173"
        blocked = client.get("/health", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in blocked.headers


def test_gemini_daily_quota_has_its_own_message() -> None:
    error = {"error": {"code": 429, "message": "Quota exceeded: Generate content requests per day limit exceeded on Free Tier.", "status": "RESOURCE_EXHAUSTED"}}
    provider, _ = _provider_with_mock(lambda req: httpx.Response(429, json=error))
    with pytest.raises(LLMError, match="daily free-tier quota"):
        provider.complete_json("s", "p", SkillsSection)
