"""Fake Gemini for the Compare Jobs tests: three JDs (AI PM, Product Analyst, Python Developer) + the summary."""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from pydantic import BaseModel

from app.match_schema import JobDescription
from app.pipeline.compare import CompareSummary
from app.pipeline.llm import LLMError
from tests.match_fakes import FIXTURES, FakeMatchProvider

ANALYST_JD_TEXT = (FIXTURES / "jd_product_analyst.txt").read_text(encoding="utf-8")

ANALYST_JD: dict[str, Any] = {
    "title": "Product Analyst",
    "company": "Swiggy",
    "seniority": "mid",
    "location": "Bengaluru",
    "employment_type": "Full-time",
    "min_years_experience": 2,
    "max_years_experience": 4,
    "required_skills": ["SQL", "Excel", "Python", "A/B testing", "Tableau / Power BI", "Statistics"],
    "preferred_skills": ["Amplitude / Mixpanel", "BigQuery / Snowflake"],
    "tools": ["Tableau", "Power BI"],
    "education_requirements": {"min_degree": "Bachelor's", "fields": ["Engineering", "Statistics", "Economics"]},
    "certifications": [],
    "responsibilities": [
        "Define and track product metrics and KPIs for the consumer ordering funnel",
        "Design, run and analyse A/B tests with product managers and engineers",
        "Write complex SQL queries on large datasets to answer product questions",
    ],
    "domain_keywords": ["product analytics", "funnel analysis", "e-commerce", "A/B testing"],
    "soft_skills": ["Communication"],
}

SUMMARY_LINES = [
    "Apply first to the top-ranked job: it has the highest score.",
    "Tailor your resume before applying to the next one.",
    "Treat the lowest-scoring job as a stretch.",
]


class FakeCompareProvider(FakeMatchProvider):
    """``FakeMatchProvider`` + the Product Analyst JD + the comparison summary.

    * ``fail_jd_marker``: JD parsing raises a (non-retryable) LLMError when the prompt contains it.
    * ``rate_limited``: the first N calls raise a retryable LLMError (like a Gemini 429).
    * ``summary_lines``: what the summary call returns.
    * ``delay``: seconds each call takes, to observe concurrency (``max_in_flight``).
    """

    def __init__(
        self,
        fail_jd_marker: str | None = None,
        rate_limited: int = 0,
        summary_lines: list[str] | None = None,
        delay: float = 0.0,
    ) -> None:
        super().__init__()
        self.fail_jd_marker = fail_jd_marker
        self.rate_limited = rate_limited
        self.summary_lines = summary_lines or SUMMARY_LINES
        self.delay = delay
        self.lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0
        self.jd_in_flight = 0
        self.max_jd_in_flight = 0

    def complete_json(self, system: str, prompt: str, response_model: type[BaseModel]) -> str:
        with self.lock:
            if self.rate_limited > 0:
                self.rate_limited -= 1
                self.calls.append({"system": system, "prompt": prompt, "response_model": response_model, "rate_limited": True})
                raise LLMError("Gemini rate limit / quota exceeded. Wait a minute and try again.", retryable=True)
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            if response_model is JobDescription:
                self.jd_in_flight += 1
                self.max_jd_in_flight = max(self.max_jd_in_flight, self.jd_in_flight)
        try:
            if self.delay:
                time.sleep(self.delay)
            if response_model is JobDescription and self.fail_jd_marker and self.fail_jd_marker in prompt:
                self.calls.append({"system": system, "prompt": prompt, "response_model": response_model})
                raise LLMError("Gemini rejected the request (400): simulated failure")
            if response_model is JobDescription and "Product Analyst" in prompt:
                self.calls.append({"system": system, "prompt": prompt, "response_model": response_model})
                return json.dumps(ANALYST_JD)
            if response_model is CompareSummary and response_model is not self.fail_on:
                self.calls.append({"system": system, "prompt": prompt, "response_model": response_model})
                return json.dumps({"lines": self.summary_lines})
            return super().complete_json(system, prompt, response_model)
        finally:
            with self.lock:
                self.in_flight -= 1
                if response_model is JobDescription:
                    self.jd_in_flight -= 1
