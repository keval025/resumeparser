"""Stage 5 - Section detection.

A line is a section heading when its text matches a synonym from ``SECTION_SYNONYMS`` *and*
it looks like a heading: larger font than the body, bold, or ALL CAPS. (OCR lines have no font
info, so for them an exact synonym match on a short line is enough.)

Everything before the first heading is returned as the ``header`` section (name + contact).
Inline headings such as "Skills: Python, SQL" are split into heading + content.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from app.pipeline.clean import fix_hyphenation
from app.pipeline.extract import TextBlock

# Canonical section name -> phrases that introduce it (compared lowercase, '&' -> 'and').
SECTION_SYNONYMS: dict[str, list[str]] = {
    "summary": [
        "summary", "professional summary", "profile", "professional profile", "about me", "about",
        "objective", "career objective", "career summary", "executive summary", "overview",
    ],
    "experience": [
        "experience", "work experience", "employment", "employment history", "professional experience",
        "work history", "internships", "internship", "internship experience", "relevant experience",
        "career history", "experience and internships",
    ],
    "projects": [
        "projects", "academic projects", "key projects", "case studies", "personal projects",
        "project experience", "notable projects", "projects undertaken", "major projects",
    ],
    "education": [
        "education", "academics", "qualifications", "academic qualifications", "educational qualifications",
        "academic background", "education and training", "academic details", "educational background",
    ],
    "skills": [
        "skills", "technical skills", "tools and technologies", "core competencies", "key skills",
        "skills and tools", "technologies", "tech stack", "competencies", "skill set", "skillset",
        "areas of expertise", "expertise", "skills and abilities", "it skills",
    ],
    "certifications": [
        "certifications", "certificates", "licenses and certifications", "courses", "certifications and courses",
        "training", "trainings", "courses and certifications",
    ],
    "achievements": [
        "achievements", "awards", "honors", "honours", "awards and achievements", "accomplishments",
        "honors and awards", "key achievements", "extracurricular activities", "extra curricular activities",
        "positions of responsibility",
    ],
    "publications": ["publications", "research", "papers", "research publications"],
    "volunteering": ["volunteering", "volunteer experience", "volunteer work", "community service"],
    "languages": ["languages", "language proficiency", "languages known", "spoken languages"],
}

# Reverse lookup: normalised phrase -> section name.
_PHRASE_TO_SECTION: dict[str, str] = {
    phrase: section for section, phrases in SECTION_SYNONYMS.items() for phrase in phrases
}

_MAX_HEADING_WORDS = 6


@dataclass
class SectionResult:
    """Output of section detection."""

    sections: dict[str, str] = field(default_factory=dict)  # includes "header"
    order: list[str] = field(default_factory=list)          # sections in document order
    body_font_size: float = 0.0


def normalize_heading(text: str) -> str:
    """'TECHNICAL SKILLS:' -> 'technical skills';  'Tools & Technologies' -> 'tools and technologies'."""
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def match_heading_text(text: str, fuzzy_threshold: float = 90) -> str | None:
    """Return the section name for a heading-like string, or None."""
    norm = normalize_heading(text)
    if not norm or len(norm.split()) > _MAX_HEADING_WORDS:
        return None
    if norm in _PHRASE_TO_SECTION:
        return _PHRASE_TO_SECTION[norm]
    # Fuzzy match handles OCR typos and small variants ("Work Experiences").
    best_section, best_score = None, 0.0
    for phrase, section in _PHRASE_TO_SECTION.items():
        score = fuzz.ratio(norm, phrase)
        if score > best_score:
            best_section, best_score = section, score
    return best_section if best_score >= fuzzy_threshold else None


def body_font_size(blocks: list[TextBlock]) -> float:
    """Most common font size, weighted by number of characters."""
    counter: Counter[float] = Counter()
    for b in blocks:
        counter[round(b.font_size)] += len(b.text)
    return float(counter.most_common(1)[0][0]) if counter else 0.0


def _looks_like_heading(block: TextBlock, body_size: float, strict: bool = False) -> bool:
    """Heading style = larger font, ALL CAPS, or (unless ``strict``) bold."""
    letters = [c for c in block.text if c.isalpha()]
    is_caps = bool(letters) and all(c.isupper() for c in letters)
    if block.font_size >= body_size + 1.0 or is_caps:
        return True
    return block.is_bold and not strict


# "Skills: Python, SQL" -> heading "Skills", rest "Python, SQL"
_INLINE_HEADING_RE = re.compile(r"^\s*([A-Za-z &/]{3,40}?)\s*[:|–—-]\s+(.+)$")

# Inside these sections, bold sub-labels ("Technologies", "Languages: Python") are common, so
# only larger-font / ALL CAPS headings start a new section and inline headings are ignored.
_STRICT_SECTIONS = {"experience", "projects", "skills"}


def classify_line(block: TextBlock, body_size: float, current_section: str = "header") -> tuple[str | None, str]:
    """Decide whether a line is a heading.

    Returns ``(section_name, remaining_text)``. ``section_name`` is None for normal lines;
    ``remaining_text`` is the content after an inline heading (or "" for a pure heading).
    """
    strict = current_section in _STRICT_SECTIONS
    text = block.text.strip().rstrip(":").strip()
    section = match_heading_text(text)
    if section is not None and section != current_section:
        exact = normalize_heading(text) in _PHRASE_TO_SECTION
        if _looks_like_heading(block, body_size, strict) or (
            exact and len(text) <= 40 and (block.source == "ocr" or not strict)
        ):
            return section, ""

    m = _INLINE_HEADING_RE.match(block.text)
    if m and not strict:
        label = m.group(1)
        inline_section = match_heading_text(label)
        if inline_section is not None and normalize_heading(label) in _PHRASE_TO_SECTION:
            return inline_section, m.group(2).strip()
    return None, block.text


def detect_sections(blocks: list[TextBlock]) -> SectionResult:
    """Split ordered, cleaned lines into ``{section_name: text}``."""
    body_size = body_font_size(blocks)
    lines_by_section: dict[str, list[str]] = {"header": []}
    order: list[str] = ["header"]
    current = "header"

    for block in blocks:
        section, rest = classify_line(block, body_size, current)
        if section is not None:
            current = section
            if section not in lines_by_section:
                lines_by_section[section] = []
                order.append(section)
            if rest:
                lines_by_section[section].append(rest)
        else:
            lines_by_section[current].append(rest)

    sections = {name: fix_hyphenation("\n".join(lines)).strip() for name, lines in lines_by_section.items()}
    return SectionResult(sections=sections, order=order, body_font_size=body_size)
