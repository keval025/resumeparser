"""Regex rules: email, phone, URLs, date ranges, name candidate."""

from __future__ import annotations

import pytest

from app.pipeline.rules import (
    build_links,
    classify_url,
    find_date_ranges,
    find_emails,
    find_phones,
    find_urls,
    guess_name,
    is_valid_email,
    normalize_phone,
)
from tests.conftest import make_block


def test_find_emails() -> None:
    text = "Contact: Priya.Sharma@Example.com | backup: p_s+jobs@mail.co.in."
    assert find_emails(text) == ["priya.sharma@example.com", "p_s+jobs@mail.co.in"]


@pytest.mark.parametrize("value,ok", [("a@b.com", True), ("a@b", False), ("not an email", False), (None, False)])
def test_is_valid_email(value: str | None, ok: bool) -> None:
    assert is_valid_email(value) is ok


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+91 98765 43210", "+919876543210"),
        ("+91-9876543210", "+919876543210"),
        ("09876543210", "+919876543210"),
        ("919876543210", "+919876543210"),
        ("+1 (415) 555-0100", "+14155550100"),
        ("+44 20 7946 0958", "+442079460958"),
        ("98765 43210", "9876543210"),
        ("12345", None),
    ],
)
def test_normalize_phone(raw: str, expected: str | None) -> None:
    assert normalize_phone(raw) == expected


def test_find_phones_ignores_dates_and_years() -> None:
    text = "Phone: +91 98765 43210\nWorked 2019 - 2021 and 01/2020 - 03/2021"
    assert find_phones(text) == ["+919876543210"]


def test_find_urls_with_and_without_scheme() -> None:
    text = (
        "linkedin.com/in/priya | https://github.com/priya, www.priya.dev\n"
        "Skills: ASP.NET, Node.js, B.Tech  email me@gmail.com"
    )
    urls = find_urls(text)
    assert "https://linkedin.com/in/priya" in urls
    assert "https://github.com/priya" in urls
    assert "https://www.priya.dev" in urls
    # Tech names and email domains are not URLs
    assert not any("gmail" in u or "node" in u.lower() or "asp" in u.lower() or "tech" in u.lower() for u in urls)


@pytest.mark.parametrize(
    "url,kind",
    [
        ("https://www.linkedin.com/in/x", "linkedin"),
        ("in.linkedin.com/in/x", "linkedin"),
        ("github.com/x", "github"),
        ("x.github.io", "portfolio"),
        ("https://priya.dev", "portfolio"),
        ("https://leetcode.com/x", "other"),
    ],
)
def test_classify_url(url: str, kind: str) -> None:
    assert classify_url(url) == kind


def test_build_links_dedupes_and_groups() -> None:
    links = build_links(
        ["https://www.linkedin.com/in/x/", "linkedin.com/in/x", "github.com/x", "https://leetcode.com/x", "mailto:a@b.com"]
    )
    assert links["linkedin"] == "https://www.linkedin.com/in/x/"
    assert links["github"] == "https://github.com/x"
    assert links["other"] == ["https://leetcode.com/x"]


def test_find_date_ranges() -> None:
    text = "Jan 2020 - Present\nSept 2018 – Dec 2019\n2015 to 2017\n06/2021 - 08/2021"
    assert find_date_ranges(text) == [
        ("Jan 2020", "Present"),
        ("Sept 2018", "Dec 2019"),
        ("2015", "2017"),
        ("06/2021", "08/2021"),
    ]


def test_guess_name_picks_largest_top_text() -> None:
    blocks = [
        make_block("RESUME", y0=20, size=12, bold=True),
        make_block("Priya Sharma", y0=40, size=22, bold=True),
        make_block("priya@example.com | +91 98765 43210", y0=70, size=10),
        make_block("Big Heading Lower Down", y0=500, size=30),
    ]
    assert guess_name(blocks) == "Priya Sharma"
