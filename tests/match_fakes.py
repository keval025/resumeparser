"""Fake Gemini answers for the job-match tests (JD parsing, relevance ratings, suggestions)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.match_schema import JobDescription, RelevanceRatings, Suggestions
from tests.sample_resume import FakeProvider

FIXTURES = Path(__file__).parent / "fixtures"
PYTHON_JD_TEXT = (FIXTURES / "jd_python_developer.txt").read_text(encoding="utf-8")
AI_PM_JD_TEXT = (FIXTURES / "jd_ai_product_manager.txt").read_text(encoding="utf-8")

# What Gemini returns for jd_python_developer.txt (deliberately with aliases + duplicates to clean).
PYTHON_JD: dict[str, Any] = {
    "title": "Python Developer (Backend)",
    "company": "FinStack Technologies",
    "seniority": "mid",
    "location": "Pune (Hybrid)",
    "employment_type": "Full-time",
    "min_years_experience": 2,
    "max_years_experience": 4,
    "required_skills": ["Python", "Django / FastAPI", "REST APIs", "SQL", "Postgres", "Git", "Docker"],
    "preferred_skills": ["Kubernetes", "Celery", "CI/CD", "GraphQL", "Django"],
    "tools": ["Redis", "AWS", "FastAPI"],
    "education_requirements": {"min_degree": "Bachelor's", "fields": ["Computer Science", "IT", "related field"]},
    "certifications": ["AWS certification"],
    "responsibilities": [
        "Design, build and maintain REST APIs and microservices in Python (Django / FastAPI)",
        "Write efficient SQL and optimise PostgreSQL queries for high-volume transaction data",
        "Improve performance with caching (Redis) and asynchronous task queues (Celery)",
        "Containerise services with Docker and deploy them to AWS",
        "Write unit and integration tests and take part in code reviews",
    ],
    "domain_keywords": ["fintech", "payments", "backend development", "microservices", "REST APIs"],
    "soft_skills": ["Communication", "Ownership"],
}

AI_PM_JD: dict[str, Any] = {
    "title": "AI Product Manager",
    "company": "Nimbus Health AI",
    "seniority": "senior",
    "location": "Bengaluru",
    "employment_type": "Full-time",
    "min_years_experience": 5,
    "max_years_experience": None,
    "required_skills": ["Product Management", "LLMs", "Prompt Engineering", "RAG", "SQL", "Amplitude / Mixpanel", "PRDs", "Jira"],
    "preferred_skills": ["Healthcare", "HIPAA", "Python", "Figma"],
    "tools": ["Jira", "Amplitude", "Mixpanel"],
    "education_requirements": {"min_degree": "Bachelor's", "fields": ["Engineering", "Computer Science"]},
    "certifications": [],
    "responsibilities": [
        "Own the product roadmap and strategy for generative AI features, from discovery to launch",
        "Translate customer and clinician problems into clear PRDs, user stories and success metrics",
        "Partner with ML engineers to define evaluation criteria for LLM quality, safety and latency",
        "Run A/B tests and analyse product analytics to prioritise the backlog",
    ],
    "domain_keywords": ["generative AI", "LLM", "healthcare", "product roadmap", "A/B testing"],
    "soft_skills": ["Stakeholder management", "Leadership"],
}

RATINGS = {"experience[0]": 0.92, "experience[1]": 0.38, "projects[0]": 0.61, "experience[9]": 1.0}

SUGGESTIONS: dict[str, Any] = {
    "summary": "Strong backend match: every required skill is present. Main gaps are Celery and Kubernetes.",
    "top_priorities": [
        {
            "area": "formatting",
            "issue": "One bullet has no measurable result.",
            "action": "Quantify the dashboards bullet.",
            "example_rewrite": "Developed internal dashboards with React and JavaScript used by [N users].",
            "estimated_score_gain": 9,
        },
        {
            "area": "keywords",
            "issue": "'microservices' never appears.",
            "action": "Describe the Infosys APIs in the JD's language where true.",
            "example_rewrite": "Built REST APIs in Python and Django serving 2M requests per day across 14 services.",
            "estimated_score_gain": 3,
        },
        {
            "area": "experience",
            "issue": "Caching work is buried.",
            "action": "Lead with performance impact.",
            "example_rewrite": "Cut report generation time by 40% with Redis caching.",
            "estimated_score_gain": 6,
        },
    ],
    "missing_keywords_to_add": ["backend development", "Kubernetes"],
    "bullets_to_rewrite": [
        {
            "original": "Reduced report generation time by 40% using Redis caching",
            "improved": "Cut report generation time by 40% (from 12 min) by adding Redis caching",
            "reason": "Leads with impact.",
        }
    ],
    "suggested_summary": "Backend developer with 3 years of experience building Python and Django REST APIs.",
    "skills_to_learn": [{"skill": "Celery", "why": "Required for async task queues.", "priority": "high"}],
}


class FakeMatchProvider(FakeProvider):
    """Answers resume extraction like ``FakeProvider`` and the three match calls with canned JSON."""

    def __init__(self, fail_on: type[BaseModel] | None = None, embeddings: bool = False) -> None:
        super().__init__()
        self.fail_on = fail_on
        if embeddings:
            self.embed = self._embed  # only some tests give the provider an embed() method

    def complete_json(self, system: str, prompt: str, response_model: type[BaseModel]) -> str:
        if response_model is self.fail_on:
            self.calls.append({"system": system, "prompt": prompt, "response_model": response_model})
            raise RuntimeError(f"{response_model.__name__} is down")
        if response_model is JobDescription:
            self.calls.append({"system": system, "prompt": prompt, "response_model": response_model})
            return json.dumps(AI_PM_JD if "Product Manager" in prompt else PYTHON_JD)
        if response_model is RelevanceRatings:
            self.calls.append({"system": system, "prompt": prompt, "response_model": response_model})
            ids = re.findall(r"id=(\w+\[\d+\])", prompt) + ["experience[9]"]  # plus one unknown id
            return json.dumps({"ratings": [{"id": i, "score": RATINGS.get(i, 0.5), "reason": f"reason for {i}"} for i in ids]})
        if response_model is Suggestions:
            self.calls.append({"system": system, "prompt": prompt, "response_model": response_model})
            return json.dumps(SUGGESTIONS)
        return super().complete_json(system, prompt, response_model)

    def _embed(self, texts: list[str], model: str = "") -> list[list[float]]:
        return [[1.0, 0.0, 1.0], [1.0, 1.0, 0.0]]  # cosine = 0.5

    def count(self, model: type[BaseModel]) -> int:
        return sum(call["response_model"] is model for call in self.calls)
