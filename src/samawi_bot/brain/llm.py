"""The one place the bot calls Claude. Structured output only: a reply that does not validate is a
failure, never something to patch up (spec §10.3: "Claude returning malformed JSON")."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """Claude failed, refused, or returned something that does not match the schema."""


class LLM(Protocol):
    def structured(self, system: str, user: str, schema: type[T], effort: str = "high") -> T: ...


@dataclass
class ClaudeLLM:
    client: anthropic.Anthropic
    model: str = "claude-opus-5-5"
    max_tokens: int = 16000
    usage: list[dict[str, Any]] = field(default_factory=list)

    def structured(self, system: str, user: str, schema: type[T], effort: str = "high") -> T:
        try:
            response = self.client.beta.messages.parse(
                model=self.model,
                max_tokens=self.max_tokens,
                # The system prompt is stable across a day's calls, so cache it.
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                thinking={"type": "adaptive"},
                output_config={"effort": effort},  # type: ignore[arg-type]
                output_format=schema,
                # On a safety-classifier refusal the API retries the same request on a fallback model.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.RateLimitError as exc:
            raise LLMError(f"Claude rate limited: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Claude API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"Claude unreachable: {exc}") from exc
        except ValidationError as exc:
            raise LLMError(f"Claude output failed schema validation: {exc.error_count()} errors") from exc
        self.usage.append({"input": response.usage.input_tokens, "output": response.usage.output_tokens})
        if response.stop_reason == "refusal":
            raise LLMError("Claude refused the request")
        if response.stop_reason == "max_tokens":
            raise LLMError("Claude hit max_tokens; output truncated")
        parsed = response.parsed_output
        if parsed is None:
            raise LLMError("Claude returned no structured output")
        return parsed
