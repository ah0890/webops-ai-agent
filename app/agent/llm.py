"""LLM provider abstraction. The platform runs fully deterministically when no provider is configured."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TypeVar

from pydantic import BaseModel

from app.config.settings import Settings, get_settings

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class LLMUnavailable(Exception):
    """No provider is configured, or the provider declined/failed."""


class LLMProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    def structured(self, system: str, prompt: str, schema: type[T], max_tokens: int = 8000) -> T:
        """Return a schema-validated object."""


class ClaudeProvider(LLMProvider):
    name = "claude"

    def __init__(self, api_key: str, model: str, effort: str = "medium", refusal_fallbacks: bool = True):
        import anthropic

        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=2, timeout=120.0)
        self.model = model
        self.effort = effort
        self.refusal_fallbacks = refusal_fallbacks

    def structured(self, system: str, prompt: str, schema: type[T], max_tokens: int = 8000) -> T:
        kwargs: dict = {}
        if self.refusal_fallbacks:
            # Server-side refusal fallback ("default" routing by refusal category).
            kwargs["extra_headers"] = {"anthropic-beta": "server-side-fallback-2026-07-01"}
            kwargs["extra_body"] = {"fallbacks": "default"}
        try:
            resp = self.client.messages.parse(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_format=schema,
                output_config={"effort": self.effort},
                **kwargs,
            )
        except self._anthropic.RateLimitError as e:
            raise LLMUnavailable("Claude rate limit reached") from e
        except self._anthropic.APIStatusError as e:
            raise LLMUnavailable(f"Claude API error {e.status_code}") from e
        except self._anthropic.APIConnectionError as e:
            raise LLMUnavailable("could not reach Claude API") from e
        if resp.stop_reason == "refusal":
            raise LLMUnavailable("Claude declined the request")
        if resp.stop_reason == "max_tokens" or resp.parsed_output is None:
            raise LLMUnavailable(f"no structured output (stop_reason={resp.stop_reason})")
        return resp.parsed_output


def get_provider(settings: Settings | None = None) -> LLMProvider | None:
    settings = settings or get_settings()
    if not settings.llm_available:
        return None
    return ClaudeProvider(
        api_key=settings.anthropic_api_key.get_secret_value(),  # type: ignore[union-attr]
        model=settings.llm_model,
        effort=settings.llm_effort,
        refusal_fallbacks=settings.llm_refusal_fallbacks,
    )
