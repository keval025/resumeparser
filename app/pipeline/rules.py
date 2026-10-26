"""Stage 6 - Deterministic rules: email, phone, URLs, date ranges and the name candidate.

Regex results are trusted over the LLM for contact details: they are exact and cannot be
hallucinated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from app.pipeline.extract import PdfLink, TextBlock
from app.pipeline.sections import match_heading_text

# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
EMAIL_FULLMATCH_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def find_emails(text: str) -> list[str]:
    """All email addresses in ``text`` (lowercased, de-duplicated, in order)."""
    seen: dict[str, None] = {}
    for match in EMAIL_RE.findall(text):
        seen.setdefault(match.lower().rstrip("."), None)
    return list(seen)


def is_valid_email(value: str | None) -> bool:
    return bool(value) and bool(EMAIL_FULLMATCH_RE.match(value.strip()))


# ---------------------------------------------------------------------------
# Phone
# ---------------------------------------------------------------------------

# Candidate: optional +country code, then digit groups separated by space . - or ( ).
PHONE_CANDIDATE_RE = re.compile(r"(?<![\w/])(\+?\(?\d[\d\s().-]{7,}\d)(?![\w/])")


def normalize_phone(raw: str) -> str | None:
    """Normalise a phone string or return None if it is not a plausible phone number.

    Rules:
      * 10-15 digits in total.
      * Indian numbers become ``+91XXXXXXXXXX``: "+91 98765 43210", "091-9876543210",
        "09876543210" and a bare 10-digit number starting with 6-9 next to a +91 hint.
      * Other numbers with a leading '+' keep their country code: "+1 (415) 555-0100" -> "+14155550100".
      * Numbers without '+' are returned as digits only.
    """
    has_plus = raw.strip().startswith("+")
    digits = re.sub(r"\D", "", raw)
    if not 10 <= len(digits) <= 15:
        return None
    if has_plus:
        return "+" + digits
    if len(digits) == 12 and digits.startswith("91") and digits[2] in "6789":
        return "+" + digits
    if len(digits) == 13 and digits.startswith("091") and digits[3] in "6789":
        return "+" + digits[1:]
    if len(digits) == 11 and digits.startswith("0") and digits[1] in "6789":
        return "+91" + digits[1:]
    return digits


def _looks_like_date_range(raw: str) -> bool:
    """'2019 - 2021' or '01/2020 - 03/2021' should not be read as a phone number."""
    return bool(re.fullmatch(r"[\s(]*(19|20)\d{2}\s*[-–/.]\s*(\d{1,2}[\s/.-]*)?(19|20)\d{2}[\s)]*", raw)) or bool(
        re.search(r"\b\d{1,2}/(19|20)\d{2}\b", raw)
    )


def find_phones(text: str) -> list[str]:
    """All plausible phone numbers in ``text``, normalised and de-duplicated."""
    results: dict[str, None] = {}
    for match in PHONE_CANDIDATE_RE.finditer(text):
        raw = match.group(1)
        if _looks_like_date_range(raw):
            continue
        phone = normalize_phone(raw)
        if phone:
            results.setdefault(phone, None)
    return list(results)


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------

_URL_WITH_SCHEME_RE = re.compile(r"\b(?:https?://|www\.)[^\s<>()\"'|,]+", re.IGNORECASE)
# Bare domains like "github.com/user" - lowercase only, to avoid "ASP.NET", "B.Tech" etc.
_BARE_DOMAIN_RE = re.compile(
    r"(?<![@\w.])((?:[a-z0-9-]{2,}\.)+(?:com|in|dev|me|org|net|co|ai|app|xyz|page|io)(?:/[^\s<>()\"'|,]*)?)(?![\w@])"
)
_PROFILE_HOSTS = ("linkedin.com", "github.com", "gitlab.com", "bitbucket.org")
_OTHER_PLATFORM_HOSTS = (
    "leetcode.com", "kaggle.com", "medium.com", "stackoverflow.com", "twitter.com", "x.com",
    "codechef.com", "hackerrank.com", "codeforces.com", "youtube.com", "drive.google.com",
    "docs.google.com", "instagram.com", "facebook.com", "dev.to", "hashnode.com", "credly.com",
    "coursera.org", "udemy.com", "huggingface.co", "scholar.google.com", "orcid.org",
)


def normalize_url(url: str) -> str:
    """Add https:// when missing and strip trailing punctuation."""
    url = url.strip().rstrip(".,;:)]}>\"'")
    if url.lower().startswith("mailto:") or url.lower().startswith("tel:"):
        return url
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    return url


def _host(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def classify_url(url: str) -> str:
    """Return 'linkedin', 'github', 'portfolio' or 'other'."""
    host = _host(normalize_url(url))
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        return "linkedin"
    if host == "github.com":
        return "github"
    if host.endswith("github.io") or host.endswith("vercel.app") or host.endswith("netlify.app"):
        return "portfolio"
    if any(host == h or host.endswith("." + h) for h in _OTHER_PLATFORM_HOSTS + _PROFILE_HOSTS):
        return "other"
    return "portfolio" if host else "other"


def find_urls(text: str) -> list[str]:
    """URLs in visible text (with or without scheme), normalised, emails excluded."""
    text_wo_emails = EMAIL_RE.sub(" ", text)
    found: dict[str, None] = {}
    for m in _URL_WITH_SCHEME_RE.finditer(text_wo_emails):
        found.setdefault(normalize_url(m.group(0)), None)
    for m in _BARE_DOMAIN_RE.finditer(text_wo_emails):
        candidate = m.group(1)
        if any(candidate in existing for existing in found):
            continue
        found.setdefault(normalize_url(candidate), None)
    return list(found)


def _url_key(url: str) -> str:
    """Comparison key so 'https://www.github.com/x/' and 'github.com/x' count as the same."""
    parsed = urlparse(normalize_url(url))
    return (_host(url) + parsed.path.rstrip("/")).lower()


def build_links(urls: list[str]) -> dict[str, object]:
    """Group URLs into the Links schema shape (first of each kind wins; extras go to 'other')."""
    links: dict[str, object] = {"linkedin": None, "github": None, "portfolio": None, "other": []}
    seen: set[str] = set()
    for url in urls:
        if url.lower().startswith(("mailto:", "tel:")):
            continue
        url = normalize_url(url)
        key = _url_key(url)
        if key in seen:
            continue
        seen.add(key)
        kind = classify_url(url)
        if kind != "other" and links[kind] is None:
            links[kind] = url
        else:
            links["other"].append(url)  # type: ignore[union-attr]
    return links


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

_MONTH = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_DATE = rf"(?:{_MONTH}\.?\s*['’]?\s*\d{{2,4}}|\d{{1,2}}[/.-]\d{{4}}|\d{{4}}[/.-]\d{{1,2}}|(?:19|20)\d{{2}})"
_END = rf"(?:{_DATE}|present|current|now|till date|to date|ongoing)"
DATE_RANGE_RE = re.compile(rf"({_DATE})\s*(?:-|–|—|to|until|till)\s*({_END})", re.IGNORECASE)


def find_date_ranges(text: str) -> list[tuple[str, str]]:
    """All 'start - end' date ranges, e.g. [('Jan 2020', 'Present')]."""
    return [(m.group(1).strip(), m.group(2).strip()) for m in DATE_RANGE_RE.finditer(text)]


# ---------------------------------------------------------------------------
# Name
# ---------------------------------------------------------------------------

_NAME_BAD_CHARS_RE = re.compile(r"[@\d/\\|:]|https?|www\.", re.IGNORECASE)


def guess_name(blocks: list[TextBlock]) -> str | None:
    """Largest-font text in the top 15% of page 1 that looks like a person's name."""
    candidates = [
        b
        for b in blocks
        if b.page == 0
        and b.y0 <= b.page_height * 0.15
        and not _NAME_BAD_CHARS_RE.search(b.text)
        and 1 <= len(b.text.split()) <= 5
        and sum(c.isalpha() for c in b.text) >= 3
        and match_heading_text(b.text) is None
        and b.text.lower() not in {"resume", "curriculum vitae", "cv"}
    ]
    if not candidates:
        return None
    best = max(candidates, key=lambda b: (b.font_size, -b.y0))
    return " ".join(best.text.split()).strip(" ,-")


# ---------------------------------------------------------------------------
# Combined
# ---------------------------------------------------------------------------


@dataclass
class RuleResult:
    """Everything the regex rules found."""

    name: str | None = None
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    links: dict[str, object] = field(default_factory=dict)

    @property
    def email(self) -> str | None:
        return self.emails[0] if self.emails else None

    @property
    def phone(self) -> str | None:
        return self.phones[0] if self.phones else None


def apply_rules(header_text: str, full_text: str, blocks: list[TextBlock], pdf_links: list[PdfLink]) -> RuleResult:
    """Run all rules. Contact details are taken from the header first, then the whole text."""
    emails = find_emails(header_text) or find_emails(full_text)
    phones = find_phones(header_text) or find_phones(full_text)
    # Annotation links first (they are exact), then URLs visible in the text.
    annotation_urls = [link.uri for link in pdf_links if not link.uri.lower().startswith(("mailto:", "tel:"))]
    # mailto: links can reveal an email hidden behind an icon.
    for link in pdf_links:
        if link.uri.lower().startswith("mailto:"):
            address = link.uri[7:].split("?")[0].strip().lower()
            if is_valid_email(address) and address not in emails:
                emails.append(address)
    urls = annotation_urls + find_urls(full_text)
    return RuleResult(name=guess_name(blocks), emails=emails, phones=phones, links=build_links(urls))
