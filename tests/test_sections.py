"""Section heading detection."""

from __future__ import annotations

import pytest

from app.pipeline.sections import detect_sections, match_heading_text
from tests.conftest import make_block


@pytest.mark.parametrize(
    "text,section",
    [
        ("WORK EXPERIENCE", "experience"),
        ("Professional Experience:", "experience"),
        ("Internships", "experience"),
        ("Academic Projects", "projects"),
        ("Case Studies", "projects"),
        ("Academics", "education"),
        ("Tools & Technologies", "skills"),
        ("Core Competencies", "skills"),
        ("Certifications", "certifications"),
        ("Awards & Achievements", "achievements"),
        ("Career Objective", "summary"),
        ("Work Experiance", "experience"),  # OCR typo, fuzzy match
        ("I have experience in Python and many other things", None),
        ("Infosys Ltd", None),
    ],
)
def test_match_heading_text(text: str, section: str | None) -> None:
    assert match_heading_text(text) == section


def test_detect_sections_with_font_signals() -> None:
    blocks = [
        make_block("Priya Sharma", y0=40, size=20, bold=True),
        make_block("priya@example.com", y0=70),
        make_block("Experience", y0=100, size=13, bold=True),
        make_block("Infosys - Engineer", y0=120, bold=True),
        make_block("- Built APIs", y0=135),
        make_block("Technologies", y0=150, bold=True),  # bold sub-label inside experience: NOT a heading
        make_block("Python, Django", y0=165),
        make_block("EDUCATION", y0=190, size=13, bold=True),
        make_block("VIT, B.Tech", y0=210),
        make_block("Skills: Python, SQL", y0=230),  # inline heading
    ]
    result = detect_sections(blocks)
    assert result.order == ["header", "experience", "education", "skills"]
    assert result.sections["header"] == "Priya Sharma\npriya@example.com"
    assert "Technologies\nPython, Django" in result.sections["experience"]
    assert result.sections["education"] == "VIT, B.Tech"
    assert result.sections["skills"] == "Python, SQL"


def test_plain_heading_without_font_signal_is_detected_outside_strict_sections() -> None:
    blocks = [
        make_block("Priya Sharma", y0=40, size=20),
        make_block("Education", y0=100),  # same size, not bold, but an exact short heading
        make_block("VIT", y0=120),
    ]
    assert detect_sections(blocks).sections["education"] == "VIT"


def test_ocr_heading_detected_inside_experience() -> None:
    blocks = [
        make_block("Experience", y0=100, source="ocr"),
        make_block("Infosys", y0=120, source="ocr"),
        make_block("Education", y0=140, source="ocr"),
        make_block("VIT", y0=160, source="ocr"),
    ]
    assert detect_sections(blocks).order == ["header", "experience", "education"]
