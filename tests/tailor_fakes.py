"""A deliberately dishonest Gemini tailoring draft for the sample resume (Priya Sharma), and a fake provider."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from app.tailor_schema import TailorDraft
from tests.match_fakes import FakeMatchProvider

INFOSYS_1 = "Built REST APIs in Python and Django serving 2M requests per day"
INFOSYS_2 = "Reduced report generation time by 40% using Redis caching"
TCS_1 = "Developed internal dashboards with React and JS"

# Every kind of violation the validator must catch, mixed with honest rewrites.
DISHONEST_DRAFT: dict[str, Any] = {
    "summary": "Backend engineer with 5 years of experience building Python and Django REST APIs at scale.",
    "summary_reason": "Targets the backend role.",
    "skills": {
        "technical": ["Python", "SQL", "Kubernetes", "JavaScript"],  # Kubernetes is not in the resume
        "tools": ["PostgreSQL", "Docker", "Redis", "Git", "AWS"],  # Redis only appears as a job skill: allowed
        "soft": ["Communication"],  # Leadership dropped: must be re-added
    },
    "experience": [
        {
            "source_index": 0,
            "company": "Infosys Ltd",
            "title": "Senior Software Engineer",  # promoted: must be reverted
            "location": "Bengaluru",
            "start": "Jul 2021",  # same date, different format: fine
            "end": "Present",
            "bullets": [
                {
                    "original_text": INFOSYS_1,
                    "text": "Designed and built REST APIs in Python and Django serving 2M requests per day for 30 enterprise clients",
                    "reason": "Leads with design; JD wording.",
                    "jd_requirement": "Design, build and maintain REST APIs",
                },
                {
                    "original_text": "Led a team of 6 engineers to migrate the monolith to microservices",  # invented
                    "text": "Led a team of 6 engineers to migrate the monolith to microservices",
                    "reason": "Shows leadership.",
                    "jd_requirement": "microservices",
                },
                # INFOSYS_2 left out: must come back hidden
            ],
        },
        {
            "source_index": 1,
            "company": "Google",  # wrong company: must be reverted to TCS
            "title": "Software Engineering Intern",
            "location": "Mumbai",
            "start": "2021-01",
            "end": "2023-01",  # wrong end date: must be reverted
            "bullets": [
                {
                    "original_text": TCS_1,
                    "text": "Developed internal dashboards with React, JavaScript and Kubernetes",  # Kubernetes unsupported
                    "reason": "Adds a JD keyword.",
                    "jd_requirement": "Kubernetes",
                }
            ],
        },
        {
            "source_index": 7,  # does not exist
            "company": "Amazon",
            "title": "SDE II",
            "bullets": [],
        },
    ],
    "projects": [
        {
            "source_index": 0,
            "name": "ResumeRank",
            "tech_stack": ["Python", "FastAPI", "PostgreSQL", "Kubernetes"],  # Kubernetes not in this project
            "bullets": [
                {
                    "original_text": "Ranked 500+ resumes per hour using ML and spaCy",
                    "text": "Ranked 500+ resumes per hour with an ML pipeline built on spaCy, cutting screening time by [X%]",
                    "reason": "Adds impact placeholder.",
                    "jd_requirement": "Python",
                }
            ],
            "relevance_score": 0.7,
        }
    ],
    "section_order": ["summary", "experience", "projects", "skills", "education", "hobbies"],
}

# A clean draft: what a well-behaved model would return.
CLEAN_DRAFT: dict[str, Any] = {
    "summary": "Backend developer with 3 years of experience building Python and Django REST APIs and data pipelines.",
    "summary_reason": "Leads with the JD's core stack.",
    "skills": {"technical": ["Python", "SQL", "JavaScript"], "tools": ["PostgreSQL", "Docker", "AWS", "Git"], "soft": ["Communication", "Leadership"]},
    "experience": [
        {
            "source_index": 0, "company": "Infosys Ltd", "title": "Software Engineer", "location": "Bengaluru",
            "start": "2021-07", "end": "present",
            "bullets": [
                {"original_text": INFOSYS_1, "text": "Designed and built REST APIs in Python and Django serving 2M requests per day", "reason": "Stronger verb; JD wording.", "jd_requirement": "REST APIs"},
                {"original_text": INFOSYS_2, "text": "Improved performance by cutting report generation time 40% with Redis caching", "reason": "JD: performance + caching.", "jd_requirement": "Redis caching"},
            ],
        },
        {
            "source_index": 1, "company": "Tata Consultancy Services", "title": "Software Engineering Intern",
            "location": "Mumbai", "start": "2021-01", "end": "2021-06",
            "bullets": [{"original_text": TCS_1, "text": TCS_1, "reason": None, "jd_requirement": None}],
        },
    ],
    "projects": [
        {
            "source_index": 0, "name": "ResumeRank", "tech_stack": ["Python", "FastAPI", "PostgreSQL"], "relevance_score": 0.8,
            "bullets": [
                {"original_text": "AI resume screener", "text": "AI resume screener built with Python, FastAPI and PostgreSQL", "reason": "Names the JD stack the project used.", "jd_requirement": "FastAPI"},
                {"original_text": "Ranked 500+ resumes per hour using ML and spaCy", "text": "Ranked 500+ resumes per hour using ML and spaCy", "reason": None, "jd_requirement": None},
            ],
        }
    ],
    "section_order": ["summary", "experience", "projects", "skills", "education", "certifications", "achievements"],
}


class FakeTailorProvider(FakeMatchProvider):
    """FakeMatchProvider that also answers the tailoring call (and records the temperature it was given)."""

    def __init__(self, draft: dict[str, Any] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.draft = draft if draft is not None else CLEAN_DRAFT
        self.temperatures: list[float] = []

    def complete_json(self, system: str, prompt: str, response_model: type[BaseModel], temperature: float = 0) -> str:
        self.temperatures.append(temperature)
        if response_model is TailorDraft and response_model is not self.fail_on:
            self.calls.append({"system": system, "prompt": prompt, "response_model": response_model})
            return json.dumps(self.draft)
        return super().complete_json(system, prompt, response_model)
