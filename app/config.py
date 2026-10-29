"""Application settings, loaded from environment variables (and a local .env file).

Every tunable value lives here so the rest of the code never calls os.getenv directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# Project root = the folder that contains the "app" package.
BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env from the project root. Real environment variables win over .env values.
load_dotenv(BASE_DIR / ".env", override=False)


def _env_path(name: str, default: Path) -> Path:
    """Read a path env var; relative paths are resolved against the project root."""
    raw = os.getenv(name)
    path = Path(raw) if raw else default
    return path if path.is_absolute() else BASE_DIR / path


def _env_optional(name: str) -> str | None:
    """Read an env var, treating an empty string as "not set"."""
    value = os.getenv(name)
    return value.strip() if value and value.strip() else None


@dataclass(frozen=True)
class Settings:
    """All configuration for the resume parser."""

    # --- LLM ---
    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "gemini"))
    gemini_api_key: str | None = field(default_factory=lambda: _env_optional("GEMINI_API_KEY"))
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-3.5-flash"))
    # Gemini models "think" before answering and thinking counts toward this limit,
    # so keep it generous.
    gemini_max_output_tokens: int = field(
        default_factory=lambda: int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "32768"))
    )
    # Resumes whose cleaned text is shorter than this are sent in ONE LLM call;
    # longer ones are split into one call per section.
    llm_single_call_max_chars: int = field(
        default_factory=lambda: int(os.getenv("LLM_SINGLE_CALL_MAX_CHARS", "12000"))
    )

    # --- Storage ---
    database_url: str = field(
        # Empty DATABASE_URL= (as in .env.example) means "use the local SQLite file".
        default_factory=lambda: _env_optional("DATABASE_URL") or f"sqlite:///{(BASE_DIR / 'data' / 'resumes.db').as_posix()}"
    )
    # Where the API stores copies of uploaded PDFs (a sub-folder, so it never mixes with the
    # files you put in data/uploads for the CLI).
    upload_dir: Path = field(default_factory=lambda: _env_path("UPLOAD_DIR", BASE_DIR / "data" / "uploads" / "api"))
    output_dir: Path = field(default_factory=lambda: _env_path("OUTPUT_DIR", BASE_DIR / "data" / "output"))

    # --- Ingest ---
    max_file_mb: float = field(default_factory=lambda: float(os.getenv("MAX_FILE_MB", "10")))

    # --- OCR ---
    ocr_min_chars: int = field(default_factory=lambda: int(os.getenv("OCR_MIN_CHARS", "50")))
    ocr_dpi: int = field(default_factory=lambda: int(os.getenv("OCR_DPI", "300")))
    tesseract_cmd: str | None = field(default_factory=lambda: _env_optional("TESSERACT_CMD"))
    poppler_path: str | None = field(default_factory=lambda: _env_optional("POPPLER_PATH"))

    # --- Validation ---
    grounding_threshold: float = field(default_factory=lambda: float(os.getenv("GROUNDING_THRESHOLD", "85")))
    review_threshold: float = field(default_factory=lambda: float(os.getenv("REVIEW_THRESHOLD", "0.7")))
    normalization_file: Path = field(
        default_factory=lambda: _env_path("NORMALIZATION_FILE", BASE_DIR / "app" / "data" / "normalization.json")
    )

    # --- Job match ---
    # rapidfuzz score (0-100) for counting a JD skill as present under a slightly different spelling.
    match_fuzzy_threshold: float = field(default_factory=lambda: float(os.getenv("MATCH_FUZZY_THRESHOLD", "85")))
    # Optional, informational only: cosine similarity of Gemini embeddings (not part of the score).
    match_semantic_similarity: bool = field(
        default_factory=lambda: os.getenv("MATCH_SEMANTIC_SIMILARITY", "true").strip().lower() in {"1", "true", "yes"}
    )
    gemini_embedding_model: str = field(
        default_factory=lambda: os.getenv("GEMINI_EMBEDDING_MODEL", "gemini-embedding-001")
    )

    # --- Compare Jobs ---
    # How many jobs talk to Gemini at the same time (all comparisons together).
    compare_concurrency: int = field(default_factory=lambda: int(os.getenv("COMPARE_CONCURRENCY", "3")))
    # Attempts per job on Gemini rate-limit / 5xx errors; waits 2 s, 4 s, 8 s... between them.
    compare_retry_attempts: int = field(default_factory=lambda: int(os.getenv("COMPARE_RETRY_ATTEMPTS", "4")))
    compare_retry_base_delay: float = field(default_factory=lambda: float(os.getenv("COMPARE_RETRY_BASE_DELAY", "2")))

    # --- Web app / deployment ---
    app_name: str = field(default_factory=lambda: os.getenv("APP_NAME", "Resume Parser"))
    # Shown on the Privacy Policy and Terms pages - replace the placeholder before launch.
    company_name: str = field(default_factory=lambda: os.getenv("COMPANY_NAME", "[Your Company Name]"))
    contact_email: str = field(default_factory=lambda: os.getenv("CONTACT_EMAIL", "privacy@example.com"))
    # "production" refuses to start without Supabase auth (no accidental open, single-user server).
    app_env: str = field(default_factory=lambda: os.getenv("APP_ENV", "development").strip().lower())
    # Public URL of the app, e.g. https://resume-match.onrender.com (CORS, login redirects, Open Graph tags).
    app_base_url: str = field(default_factory=lambda: os.getenv("APP_BASE_URL", "http://127.0.0.1:8000").rstrip("/"))
    # Extra allowed CORS origins, comma separated (localhost is always allowed).
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(o.strip().rstrip("/") for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip())
    )
    max_pdf_pages: int = field(default_factory=lambda: int(os.getenv("MAX_PDF_PAGES", "10")))

    # --- Supabase (auth + storage). Empty SUPABASE_URL = single-user local mode, files on disk. ---
    supabase_url: str | None = field(default_factory=lambda: (_env_optional("SUPABASE_URL") or "").rstrip("/") or None)
    supabase_anon_key: str | None = field(default_factory=lambda: _env_optional("SUPABASE_ANON_KEY"))
    supabase_service_role_key: str | None = field(default_factory=lambda: _env_optional("SUPABASE_SERVICE_ROLE_KEY"))
    # Legacy HS256 secret; projects using the new signing keys are verified with JWKS instead (no secret needed).
    supabase_jwt_secret: str | None = field(default_factory=lambda: _env_optional("SUPABASE_JWT_SECRET"))
    storage_dir: Path = field(default_factory=lambda: _env_path("STORAGE_DIR", BASE_DIR / "data" / "storage"))
    signed_url_seconds: int = field(default_factory=lambda: int(os.getenv("SIGNED_URL_SECONDS", "600")))
    # Signs local-storage download links (local mode only). Random per process if unset.
    secret_key: str | None = field(default_factory=lambda: _env_optional("SECRET_KEY"))

    # --- Free plan limits per calendar month (plan "pro" = unlimited) ---
    free_parses_per_month: int = field(default_factory=lambda: int(os.getenv("FREE_PARSES_PER_MONTH", "5")))
    free_matches_per_month: int = field(default_factory=lambda: int(os.getenv("FREE_MATCHES_PER_MONTH", "10")))
    free_tailors_per_month: int = field(default_factory=lambda: int(os.getenv("FREE_TAILORS_PER_MONTH", "3")))
    free_comparisons_per_month: int = field(default_factory=lambda: int(os.getenv("FREE_COMPARISONS_PER_MONTH", "2")))
    # Per-user request rate limits ("N/minute"). "heavy" = requests that start Gemini work or render files.
    rate_limit_heavy: str = field(default_factory=lambda: os.getenv("RATE_LIMIT_HEAVY", "20/minute"))
    rate_limit_default: str = field(default_factory=lambda: os.getenv("RATE_LIMIT_DEFAULT", "120/minute"))

    # --- Operations ---
    sentry_dsn: str | None = field(default_factory=lambda: _env_optional("SENTRY_DSN"))
    # Daily clean-up (resume auto-delete, old export files). Tests turn it off.
    enable_scheduler: bool = field(
        default_factory=lambda: os.getenv("ENABLE_SCHEDULER", "true").strip().lower() in {"1", "true", "yes"}
    )

    # --- Misc ---
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    parser_version: str = "1.0.0"

    @property
    def max_file_bytes(self) -> int:
        """Maximum upload size in bytes."""
        return int(self.max_file_mb * 1024 * 1024)

    @property
    def auth_enabled(self) -> bool:
        """Supabase login required (multi-user). Off = single-user local mode."""
        return bool(self.supabase_url)

    @property
    def free_limits(self) -> dict[str, int]:
        return {
            "parse": self.free_parses_per_month,
            "match": self.free_matches_per_month,
            "tailor": self.free_tailors_per_month,
            "compare": self.free_comparisons_per_month,
        }

    def check_supabase(self) -> None:
        """Any environment: refuse a half-filled Supabase block instead of silently running without login."""
        keys = (
            ("SUPABASE_ANON_KEY", self.supabase_anon_key),
            ("SUPABASE_SERVICE_ROLE_KEY", self.supabase_service_role_key),
            ("SUPABASE_JWT_SECRET", self.supabase_jwt_secret),
        )
        problems = []
        if not self.supabase_url:
            given = [name for name, value in keys if value]
            if given:
                problems.append(f"{', '.join(given)} set but SUPABASE_URL is empty (login would stay OFF)")
        else:
            if not self.supabase_url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
                problems.append("SUPABASE_URL must look like https://<project-ref>.supabase.co")
            elif "/rest/v1" in self.supabase_url or "/auth/v1" in self.supabase_url:
                problems.append("SUPABASE_URL must be the bare project URL, without /rest/v1 or /auth/v1")
            if not self.supabase_anon_key:
                problems.append("SUPABASE_ANON_KEY is empty (the login page needs it)")
            elif self.supabase_anon_key == self.supabase_service_role_key:
                problems.append("SUPABASE_ANON_KEY and SUPABASE_SERVICE_ROLE_KEY are the same key (one was pasted twice)")
        if problems:
            raise RuntimeError("Supabase settings in .env are incomplete: " + "; ".join(problems) + ". See DEPLOY.md, Step 1.")

    def check_production(self) -> None:
        """Refuse to run a public server without login or with half-configured Supabase."""
        if self.app_env != "production":
            return
        missing = [
            name
            for name, value in (
                ("SUPABASE_URL", self.supabase_url),
                ("SUPABASE_ANON_KEY", self.supabase_anon_key),
                ("SUPABASE_SERVICE_ROLE_KEY", self.supabase_service_role_key),
                ("GEMINI_API_KEY", self.gemini_api_key),
            )
            if not value
        ]
        if self.database_url.startswith("sqlite"):
            missing.append("DATABASE_URL (Postgres)")
        if missing:
            raise RuntimeError("APP_ENV=production but these settings are missing: " + ", ".join(missing))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings object (created once)."""
    return Settings()
