"""Google Gemini provider (official ``google-genai`` SDK).

Uses Gemini's structured output: ``response_mime_type="application/json"`` plus
``response_json_schema`` set to the Pydantic model's JSON Schema, with ``temperature=0`` for repeatable output.
"""

from __future__ import annotations

import logging

from google import genai
from google.genai import errors, types
from pydantic import BaseModel

from app.pipeline.llm.base import LLMError

logger = logging.getLogger(__name__)

# Finish reasons that mean "the answer was blocked or cut short".
_BLOCKED_REASONS = {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "LANGUAGE", "OTHER"}


class GeminiProvider:
    """Calls a Gemini model and returns its JSON answer as a string."""

    name = "gemini"

    def __init__(self, api_key: str | None, model: str = "gemini-2.5-flash", max_output_tokens: int = 32768) -> None:
        if not api_key:
            raise LLMError(
                "GEMINI_API_KEY is not set. Add a line GEMINI_API_KEY=your-key to the .env file "
                "in the project folder, then restart the server."
            )
        self.model = model
        self.max_output_tokens = max_output_tokens
        # Retry transient failures (429 rate limits, 5xx) a few times with backoff.
        self.client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=4)),
        )

    def complete_json(self, system: str, prompt: str, response_model: type[BaseModel], temperature: float = 0) -> str:
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature,
            max_output_tokens=self.max_output_tokens,
            response_mime_type="application/json",
            # JSON Schema field (not ``response_schema``): it accepts the ``additionalProperties: false``
            # that ``extra="forbid"`` models produce.
            response_json_schema=response_model.model_json_schema(),
        )
        try:
            response = self.client.models.generate_content(model=self.model, contents=prompt, config=config)
        except errors.ClientError as exc:  # 4xx: our request or our key is the problem
            message = str(exc.message or exc)
            if exc.code in (400, 401, 403) and "api key" in message.lower():
                raise LLMError("Gemini rejected the API key. Check GEMINI_API_KEY in .env.") from exc
            if exc.code == 404:
                raise LLMError(f"Gemini model '{self.model}' was not found. Check GEMINI_MODEL in .env.") from exc
            if exc.code == 429:
                if "per day" in message.lower():
                    raise LLMError(
                        f"Gemini's daily free-tier quota for '{self.model}' is used up. Try again tomorrow, "
                        "or set GEMINI_MODEL in .env to another model (e.g. a -lite model) and restart."
                    ) from exc
                raise LLMError("Gemini rate limit / quota exceeded. Wait a minute and try again.", retryable=True) from exc
            raise LLMError(f"Gemini rejected the request ({exc.code}): {message}") from exc
        except errors.ServerError as exc:  # 5xx after the SDK's retries
            raise LLMError(f"Gemini server error ({exc.code}); try again later.", retryable=True) from exc
        except errors.APIError as exc:
            raise LLMError(f"Gemini API error ({exc.code}): {exc.message}") from exc
        except OSError as exc:  # DNS / connection problems
            raise LLMError(f"Could not reach the Gemini API: {exc}", retryable=True) from exc

        feedback = response.prompt_feedback
        if feedback is not None and feedback.block_reason:
            raise LLMError(f"Gemini blocked the request ({feedback.block_reason}).")
        candidate = response.candidates[0] if response.candidates else None
        finish = candidate.finish_reason.name if candidate and candidate.finish_reason else None
        if finish == "MAX_TOKENS":
            raise LLMError("Gemini's answer was cut off (raise GEMINI_MAX_OUTPUT_TOKENS in .env).")
        if finish in _BLOCKED_REASONS:
            raise LLMError(f"Gemini stopped without a usable answer (finish reason: {finish}).")
        text = response.text
        if not text:
            raise LLMError("Gemini returned an empty answer.")

        usage = response.usage_metadata
        logger.info(
            "llm call done",
            extra={
                "provider": self.name,
                "model": self.model,
                "input_tokens": usage.prompt_token_count if usage else None,
                "output_tokens": usage.candidates_token_count if usage else None,
            },
        )
        return text

    def embed(self, texts: list[str], model: str = "gemini-embedding-001") -> list[list[float]]:
        """Embedding vectors for ``texts`` (used for the optional semantic-similarity metric)."""
        try:
            response = self.client.models.embed_content(
                model=model,
                contents=texts,
                config=types.EmbedContentConfig(task_type="SEMANTIC_SIMILARITY"),
            )
        except errors.APIError as exc:
            raise LLMError(f"Gemini embedding error ({exc.code}): {exc.message}") from exc
        except OSError as exc:
            raise LLMError(f"Could not reach the Gemini API: {exc}") from exc
        return [list(e.values or []) for e in response.embeddings or []]
