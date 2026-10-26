"""Date, duration, skill and degree normalisation."""

from __future__ import annotations

from datetime import date

import pytest

from app.config import Settings
from app.pipeline.normalize import (
    PRESENT,
    load_alias_maps,
    months_between,
    normalize_date,
    normalize_degree,
    normalize_skill_list,
    total_experience_months,
)

TODAY = date(2026, 10, 24)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Jan 2020", "2020-01"),
        ("January 2020", "2020-01"),
        ("Sept. 2021", "2021-09"),
        ("Jan'20", "2020-01"),
        ("Mar ’19", "2019-03"),
        ("01/2020", "2020-01"),
        ("1-2020", "2020-01"),
        ("2020-03", "2020-03"),
        ("2020/3", "2020-03"),
        ("15/08/2021", "2021-08"),
        ("2019", "2019-01"),
        ("Summer 2022", "2022-06"),
        ("Present", PRESENT),
        ("current", PRESENT),
        ("Till Date", PRESENT),
        ("", None),
        (None, None),
        ("sometime", None),
    ],
)
def test_normalize_date(raw: str | None, expected: str | None) -> None:
    assert normalize_date(raw, TODAY) == expected


def test_months_between_inclusive_and_present() -> None:
    assert months_between("2021-01", "2021-06", TODAY) == 6
    assert months_between("2026-01", PRESENT, TODAY) == 10
    assert months_between("2021-06", "2021-01", TODAY) is None  # end before start
    assert months_between(None, "2021-01", TODAY) is None


def test_total_experience_merges_overlaps() -> None:
    periods = [("2020-01", "2020-12"), ("2020-07", "2021-06"), ("2022-01", "2022-03"), (None, None)]
    # 2020-01..2021-06 = 18 months, plus 3 months
    assert total_experience_months(periods, TODAY) == 21
    assert total_experience_months([(None, None)], TODAY) is None


@pytest.fixture(scope="module")
def maps() -> tuple[dict[str, str], dict[str, str]]:
    return load_alias_maps(str(Settings().normalization_file))


def test_skill_aliases_and_dedup(maps: tuple[dict[str, str], dict[str, str]]) -> None:
    skills, _ = maps
    result = normalize_skill_list(["JS", "Javascript", "ML", "python", "Python", "ReactJS", "Rust"], skills)
    assert result == ["JavaScript", "Machine Learning", "Python", "React", "Rust"]


@pytest.mark.parametrize(
    "degree,expected",
    [
        ("B.Tech", "Bachelor's - Engineering"),
        ("BTech", "Bachelor's - Engineering"),
        ("Bachelor of Technology", "Bachelor's - Engineering"),
        ("B.Tech in Computer Science", "Bachelor's - Engineering"),
        ("B.E. (Mechanical)", "Bachelor's - Engineering"),
        ("M.Sc. Physics", "Master's - Science"),
        ("MBA", "Master's - Business Administration"),
        ("Ph.D.", "Doctorate"),
        ("Underwater Basket Weaving", None),
    ],
)
def test_degree_aliases(maps: tuple[dict[str, str], dict[str, str]], degree: str, expected: str | None) -> None:
    _, degrees = maps
    assert normalize_degree(degree, degrees) == expected
