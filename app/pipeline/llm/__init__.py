"""LLM providers. To add a vendor: write a class with ``name`` and ``complete_json`` (see
``base.py``) and register it in ``get_provider`` below."""

from __future__ import annotations

from app.config import Settings
from app.pipeline.llm.base import LLMError, LLMProvider


def get_provider(settings: Settings) -> LLMProvider:
    """Build the provider named by LLM_PROVIDER. Raises LLMError with a readable message."""
    if settings.llm_provider == "gemini":
        from app.pipeline.llm.gemini_provider import GeminiProvider

        return GeminiProvider(
            api_key=settings.gemini_api_key,
            model=settings.gemini_model,
            max_output_tokens=settings.gemini_max_output_tokens,
        )
    raise LLMError(f"Unknown LLM_PROVIDER '{settings.llm_provider}' (supported: gemini).")


__all__ = ["LLMError", "LLMProvider", "get_provider"]
