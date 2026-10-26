"""Cleaning: unicode, bullets, hyphenation, headers/footers, two-column reading order."""

from __future__ import annotations

from app.pipeline.clean import (
    blocks_to_text,
    clean_blocks,
    fix_hyphenation,
    normalize_unicode,
    remove_headers_footers,
)
from tests.conftest import make_block


def test_ligatures_quotes_and_bullets() -> None:
    assert normalize_unicode("ﬁnance ofﬁce") == "finance office"
    assert normalize_unicode("“Smart” ‘quotes’") == "\"Smart\" 'quotes'"
    for bullet in ("•", "▪", "➢", "●", ""):
        assert normalize_unicode(f"{bullet}  Built APIs") == "- Built APIs"
    assert normalize_unicode("– Built APIs") == "- Built APIs"


def test_fix_hyphenation() -> None:
    assert fix_hyphenation("develop-\nment of APIs") == "development of APIs"
    assert fix_hyphenation("Front-\nEnd") == "Front-\nEnd"  # capitalised next word is kept


def test_remove_headers_footers() -> None:
    blocks = [
        make_block("John Doe - Resume", y0=10, page=0),
        make_block("Body page 1", y0=300, page=0),
        make_block("Page 1 of 2", y0=820, page=0),
        make_block("John Doe - Resume", y0=10, page=1),
        make_block("Body page 2", y0=300, page=1),
        make_block("2", y0=820, page=1),
    ]
    kept = [b.text for b in remove_headers_footers(blocks, page_count=2)]
    assert kept == ["Body page 1", "Body page 2"]


def test_two_column_reading_order() -> None:
    # Full-width header, then a left sidebar (x=40..200) and a right main column (x=240..560).
    blocks = [make_block("Priya Sharma - Backend Engineer - Pune, India", x0=40, y0=30, x1=500, size=18)]
    left = ["SKILLS", "Python", "Django", "SQL", "EDUCATION", "VIT Vellore", "B.Tech 2021"]
    right = [
        "EXPERIENCE",
        "Infosys Ltd - Software Engineer, 2021 to Present",
        "Built REST APIs serving two million requests per day",
        "Reduced report generation time by forty percent",
        "TCS - Software Engineering Intern, Jan to Jun 2021",
        "Developed internal dashboards with React and JS",
        "PROJECTS",
    ]
    for i, text in enumerate(left):
        blocks.append(make_block(text, x0=40, y0=80 + i * 20, x1=190))
    for i, text in enumerate(right):
        blocks.append(make_block(text, x0=240, y0=80 + i * 20, x1=560))

    # Shuffle into "visual row" order, as a naive top-to-bottom reader would produce.
    shuffled = sorted(blocks, key=lambda b: (b.y0, b.x0))
    ordered, two_col_pages = clean_blocks(shuffled, page_count=1)
    texts = [b.text for b in ordered]
    assert two_col_pages == [0]
    assert texts == [blocks[0].text] + left + right


def test_single_column_with_right_aligned_dates_is_not_split() -> None:
    blocks = []
    y = 60
    for job in range(4):
        blocks.append(make_block(f"Company {job} - Engineer", x0=50, y0=y, x1=220))
        blocks.append(make_block("Jan 2020 - Present", x0=460, y0=y, x1=545))
        blocks.append(make_block("- Built a large distributed system used by many teams daily", x0=60, y0=y + 15, x1=540))
        blocks.append(make_block("- Improved latency by 40 percent across all public APIs", x0=60, y0=y + 30, x1=530))
        y += 60
    ordered, two_col_pages = clean_blocks(list(blocks), page_count=1)
    assert two_col_pages == []
    assert blocks_to_text(ordered).splitlines()[:2] == ["Company 0 - Engineer", "Jan 2020 - Present"]
