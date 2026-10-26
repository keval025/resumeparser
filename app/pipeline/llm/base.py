"""The LLM provider interface shared by all vendors.

A provider has one job: given a system prompt, a user prompt and a Pydantic model describing
the expected answer, return the model's answer as a JSON string. Validation and the
retry-on-error logic live in ``llm_extract.py``, so every provider gets them for free.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel


class LLMError(RuntimeError):
    """The LLM call failed (missing key, network, blocked, truncated output, ...).

    The message is written for humans: it is shown in the API response and the web UI.
    ``retryable`` is True for transient failures (rate limits, 5xx, network) that are worth retrying later.
    """

    def __init__(self, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class LLMProvider(Protocol):
    """Anything that can turn (system, prompt, response model) into a JSON string."""

    name: str

    def complete_json(self, system: str, prompt: str, response_model: type[BaseModel]) -> str:
        """Return the model's answer as a JSON string that should match ``response_model``.

        Providers may also accept an optional ``temperature`` keyword (default 0); callers only pass it
        when they need a non-zero value.
        """
        ...
