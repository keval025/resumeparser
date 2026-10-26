"""Builds a realistic sample resume PDF and a matching fake LLM answer, for tests and demos.

Create the demo PDF used in the README:
    python -m tests.sample_resume data/uploads/sample_resume.pdf
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pymupdf
from pydantic import BaseModel

LINKEDIN_URL = "https://www.linkedin.com/in/priya-sharma-dev"

# (text, font size, bold)
_LINES: list[tuple[str, float, bool]] = [
    ("Priya Sharma", 22, True),
    ("priya.sharma@example.com | +91 98765 43210 | Pune, India | LinkedIn", 10, False),
    ("github.com/priyasharma", 10, False),
    ("", 6, False),
    ("SUMMARY", 13, True),
    ("Backend developer with 3 years of experience building scalable APIs and data pipelines.", 10, False),
    ("", 6, False),
    ("WORK EXPERIENCE", 13, True),
    ("Infosys Ltd - Software Engineer", 11, True),
    ("Jul 2021 - Present | Bengaluru", 10, False),
    ("• Built REST APIs in Python and Django serving 2M requests per day", 10, False),
    ("• Reduced report generation time by 40% using Redis caching", 10, False),
    ("Tata Consultancy Services - Software Engineering Intern", 11, True),
    ("Jan 2021 - Jun 2021 | Mumbai", 10, False),
    ("• Developed internal dashboards with React and JS", 10, False),
    ("", 6, False),
    ("PROJECTS", 13, True),
    ("ResumeRank - AI resume screener", 11, True),
    ("• Ranked 500+ resumes per hour using ML and spaCy", 10, False),
    ("Tech: Python, FastAPI, Postgres", 10, False),
    ("", 6, False),
    ("EDUCATION", 13, True),
    ("Vellore Institute of Technology", 11, True),
    ("B.Tech in Computer Science, CGPA 8.7/10", 10, False),
    ("2017 - 2021", 10, False),
    ("", 6, False),
    ("TECHNICAL SKILLS", 13, True),
    ("Languages: Python, JS, SQL", 10, False),
    ("Tools: Docker, Git, AWS, Postgres", 10, False),
    ("Soft skills: Leadership, Communication", 10, False),
    ("", 6, False),
    ("CERTIFICATIONS", 13, True),
    ("AWS Certified Cloud Practitioner - Amazon Web Services, Mar 2022", 10, False),
    ("", 6, False),
    ("ACHIEVEMENTS", 13, True),
    ("• Winner, Smart India Hackathon 2020", 10, False),
]


def _build_document() -> pymupdf.Document:
    """Lay out the sample resume on one A4 page."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)  # A4 in points
    y = 60.0
    for text, size, bold in _LINES:
        if text:
            page.insert_text((50, y), text, fontsize=size, fontname="hebo" if bold else "helv")
            if text.endswith("LinkedIn"):
                # Hidden hyperlink behind the word "LinkedIn" - only visible via annotations.
                width = pymupdf.get_text_length("LinkedIn", fontname="helv", fontsize=size)
                full = pymupdf.get_text_length(text, fontname="helv", fontsize=size)
                rect = pymupdf.Rect(50 + full - width, y - size, 50 + full, y + 2)
                page.insert_link({"kind": pymupdf.LINK_URI, "from": rect, "uri": LINKEDIN_URL})
        y += size * 1.45
    return doc


def build_sample_pdf(path: Path) -> Path:
    """Write the sample resume to ``path`` and return it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _build_document() as doc:
        doc.save(path)
    return path


def build_sample_pdf_bytes() -> bytes:
    """Return the sample resume as PDF bytes."""
    with _build_document() as doc:
        return doc.tobytes()


# What a well-behaved LLM would return for the sample (raw values, dates as written).
# It also contains ONE invented job ("Google") that the grounding check must remove.
MOCK_EXTRACTION: dict[str, Any] = {
    "basics": {
        "name": "Priya Sharma",
        "email": "priya.sharma@example.com",
        "phone": "+91 98765 43210",
        "location": "Pune, India",
        "links": {"linkedin": None, "github": "github.com/priyasharma", "portfolio": None, "other": []},
        "summary": "Backend developer with 3 years of experience building scalable APIs and data pipelines.",
    },
    "education": [
        {
            "institution": "Vellore Institute of Technology",
            "degree": "B.Tech",
            "degree_normalized": None,
            "field": "Computer Science",
            "start": "2017",
            "end": "2021",
            "grade": "CGPA 8.7/10",
        }
    ],
    "experience": [
        {
            "company": "Infosys Ltd",
            "title": "Software Engineer",
            "location": "Bengaluru",
            "start": "Jul 2021",
            "end": "Present",
            "duration_months": None,
            "bullets": [
                "Built REST APIs in Python and Django serving 2M requests per day",
                "Reduced report generation time by 40% using Redis caching",
            ],
            "skills_used": ["Python", "Django", "Redis"],
        },
        {
            "company": "Tata Consultancy Services",
            "title": "Software Engineering Intern",
            "location": "Mumbai",
            "start": "Jan 2021",
            "end": "Jun 2021",
            "duration_months": None,
            "bullets": ["Developed internal dashboards with React and JS"],
            "skills_used": ["React", "JS"],
        },
        {
            "company": "Google",
            "title": "Staff Engineer",
            "location": None,
            "start": "2015",
            "end": "2016",
            "duration_months": None,
            "bullets": [],
            "skills_used": ["Kubernetes"],
        },
    ],
    "projects": [
        {
            "name": "ResumeRank",
            "description": "AI resume screener",
            "tech_stack": ["Python", "FastAPI", "Postgres"],
            "link": None,
            "outcomes": ["Ranked 500+ resumes per hour using ML and spaCy"],
            "start": None,
            "end": None,
        }
    ],
    "skills": {
        "technical": ["Python", "JS", "SQL"],
        "tools": ["Docker", "Git", "AWS", "Postgres"],
        "soft": ["Leadership", "Communication"],
        "languages": [],
    },
    "certifications": [
        {"name": "AWS Certified Cloud Practitioner", "issuer": "Amazon Web Services", "date": "Mar 2022"}
    ],
    "achievements": ["Winner, Smart India Hackathon 2020"],
}


class FakeProvider:
    """LLM stand-in: answers from MOCK_EXTRACTION based on which schema it is asked for.

    ``bad_first_answers`` makes the first N calls return invalid JSON, to exercise the retry.
    """

    name = "fake"

    def __init__(self, payload: dict[str, Any] | None = None, bad_first_answers: int = 0) -> None:
        self.payload = payload if payload is not None else MOCK_EXTRACTION
        self.bad_first_answers = bad_first_answers
        self.calls: list[dict[str, Any]] = []

    def complete_json(self, system: str, prompt: str, response_model: type[BaseModel]) -> str:
        self.calls.append({"system": system, "prompt": prompt, "response_model": response_model})
        if len(self.calls) <= self.bad_first_answers:
            return '{"basics": "this is not an object"'
        keys = list(response_model.model_fields)
        if len(keys) > 1:  # full ExtractedResume schema
            return json.dumps(self.payload)
        return json.dumps({keys[0]: self.payload[keys[0]]})


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/uploads/sample_resume.pdf")
    print(f"Wrote {build_sample_pdf(target)}")
