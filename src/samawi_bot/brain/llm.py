"""The one place the bot calls Claude. Structured output only: a reply that does not validate is a
failure, never something to patch up (spec §10.3: "Claude returning malformed JSON")."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """Claude failed, refused, or returned something that does not match the schema."""


OUT_OF_CREDIT = "Anthropic API credit balance is empty"


class LLMOutOfCredit(LLMError):
    """The account has no credit: every call fails until the owner adds some in the Console."""


Role = Literal["writer", "judge"]


class LLM(Protocol):
    def structured(self, system: str, user: str, schema: type[T], effort: str = "high", role: Role = "judge") -> T: ...


@dataclass
class ClaudeLLM:
    """`model` checks (judge, hook judge); `writer_model` drafts (writer, hook writer).

    The role defaults to "judge", so a call that forgets to say what it is runs on the checking model:
    a cheaper writer can only produce drafts that fail more often, never a weaker review."""

    client: anthropic.Anthropic
    model: str = "claude-opus-5-5"
    writer_model: str | None = None
    max_tokens: int = 16000
    usage: list[dict[str, Any]] = field(default_factory=list)

    def model_for(self, role: Role) -> str:
        return self.writer_model if role == "writer" and self.writer_model else self.model

    def structured(self, system: str, user: str, schema: type[T], effort: str = "high", role: Role = "judge") -> T:
        model = self.model_for(role)
        # On a safety-classifier refusal the API retries the same request on a fallback model. Haiku
        # has no server-side fallback, so a Haiku refusal is an ordinary failed attempt.
        fallback: dict[str, Any] = {} if model.startswith("claude-haiku") else {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        try:
            response = self.client.beta.messages.parse(
                model=model,
                max_tokens=self.max_tokens,
                # The system prompt is stable across a day's calls, so cache it.
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                thinking={"type": "adaptive"},
                output_config={"effort": effort},  # type: ignore[arg-type]
                output_format=schema,
                **fallback,
            )
        except anthropic.RateLimitError as exc:
            raise LLMError(f"Claude rate limited: {exc}") from exc
        except anthropic.APIStatusError as exc:
            if "credit balance" in str(exc.message).lower():
                raise LLMOutOfCredit(f"{OUT_OF_CREDIT} (Claude API {exc.status_code})") from exc
            raise LLMError(f"Claude API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"Claude unreachable: {exc}") from exc
        except ValidationError as exc:
            raise LLMError(f"Claude output failed schema validation: {exc.error_count()} errors") from exc
        self.usage.append({"model": model, "input": response.usage.input_tokens, "output": response.usage.output_tokens})
        if response.stop_reason == "refusal":
            raise LLMError("Claude refused the request")
        if response.stop_reason == "max_tokens":
            raise LLMError("Claude hit max_tokens; output truncated")
        parsed = response.parsed_output
        if parsed is None:
            raise LLMError("Claude returned no structured output")
        return parsed
