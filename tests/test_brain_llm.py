"""ClaudeLLM.structured against a fake anthropic client: request shape and every failure → LLMError."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import anthropic
import pytest
from pydantic import BaseModel, ValidationError

from samawi_bot.brain.llm import ClaudeLLM, LLMError
from samawi_bot.brain.models import JudgeReport

try:  # anthropic ≥ 2 builds its exceptions on httpx2
    import httpx2 as hx
except ImportError:  # pragma: no cover
    import httpx as hx  # type: ignore[no-redef]


class Tiny(BaseModel):
    answer: str


def _usage() -> SimpleNamespace:
    return SimpleNamespace(input_tokens=11, output_tokens=7)


class FakeMessages:
    def __init__(self, result: Any):
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class FakeClient:
    def __init__(self, result: Any):
        self.messages = FakeMessages(result)
        self.beta = SimpleNamespace(messages=self.messages)


def _ok(parsed: Any = None, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(stop_reason=stop_reason, parsed_output=parsed, usage=_usage())


def _request() -> Any:
    return hx.Request("POST", "https://api.anthropic.com/v1/messages")


def _response(status: int) -> Any:
    return hx.Response(status, request=_request(), json={"type": "error", "error": {"type": "x", "message": "boom"}})


def _validation_error() -> ValidationError:
    try:
        Tiny.model_validate({"answer": 3, "extra": 1})
    except ValidationError as exc:
        return exc
    raise AssertionError("expected a ValidationError")


def test_structured_request_shape_and_usage() -> None:
    client = FakeClient(_ok(Tiny(answer="ya")))
    llm = ClaudeLLM(client)  # type: ignore[arg-type]
    out = llm.structured("SYSTEM PROMPT", "user text", Tiny, effort="medium")
    assert out == Tiny(answer="ya")
    (kw,) = client.messages.calls
    assert kw["model"] == "claude-opus-5-5"
    assert kw["max_tokens"] == 16000
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["output_config"] == {"effort": "medium"}
    assert kw["output_format"] is Tiny
    assert kw["system"] == [{"type": "text", "text": "SYSTEM PROMPT", "cache_control": {"type": "ephemeral"}}]
    assert kw["messages"] == [{"role": "user", "content": "user text"}]
    assert kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert kw["fallbacks"] == "default"
    assert llm.usage == [{"model": "claude-opus-5-5", "input": 11, "output": 7}]


def test_structured_default_effort_is_high_and_schema_passed_through() -> None:
    client = FakeClient(_ok(Tiny(answer="x")))
    ClaudeLLM(client).structured("s", "u", JudgeReport)  # type: ignore[arg-type]
    kw = client.messages.calls[0]
    assert kw["output_config"] == {"effort": "high"}
    assert kw["output_format"] is JudgeReport


def test_writer_calls_use_the_writer_model_and_judge_calls_the_judge_model() -> None:
    client = FakeClient(_ok(Tiny(answer="x")))
    llm = ClaudeLLM(client, model="claude-opus-5-5", writer_model="claude-haiku-5-5")  # type: ignore[arg-type]
    llm.structured("s", "u", Tiny, role="writer")
    llm.structured("s", "u", Tiny, role="judge")
    llm.structured("s", "u", Tiny)  # no role: a judge call, never the cheaper model
    writer, judge, unlabelled = client.messages.calls
    assert writer["model"] == "claude-haiku-5-5"
    assert judge["model"] == unlabelled["model"] == "claude-opus-5-5"
    assert [u["model"] for u in llm.usage] == ["claude-haiku-5-5", "claude-opus-5-5", "claude-opus-5-5"]


def test_haiku_gets_no_server_side_fallback_but_opus_does() -> None:
    """Haiku 5.5 has no server-side refusal fallback; sending the parameter would fail every writer call."""
    client = FakeClient(_ok(Tiny(answer="x")))
    llm = ClaudeLLM(client, writer_model="claude-haiku-5-5")  # type: ignore[arg-type]
    llm.structured("s", "u", Tiny, role="writer")
    llm.structured("s", "u", Tiny, role="judge")
    writer, judge = client.messages.calls
    assert "fallbacks" not in writer and "betas" not in writer
    assert judge["fallbacks"] == "default" and judge["betas"] == ["server-side-fallback-2026-07-01"]


def test_without_a_writer_model_every_call_uses_the_one_model() -> None:
    client = FakeClient(_ok(Tiny(answer="x")))
    ClaudeLLM(client).structured("s", "u", Tiny, role="writer")  # type: ignore[arg-type]
    assert client.messages.calls[0]["model"] == "claude-opus-5-5"


def test_custom_model_is_used() -> None:
    client = FakeClient(_ok(Tiny(answer="x")))
    ClaudeLLM(client, model="claude-other").structured("s", "u", Tiny)  # type: ignore[arg-type]
    assert client.messages.calls[0]["model"] == "claude-other"


@pytest.mark.parametrize(
    ("stop_reason", "parsed", "match"),
    [
        ("refusal", Tiny(answer="x"), "refused"),
        ("max_tokens", Tiny(answer="x"), "max_tokens"),
        ("end_turn", None, "no structured output"),
    ],
)
def test_bad_responses_raise(stop_reason: str, parsed: Any, match: str) -> None:
    llm = ClaudeLLM(FakeClient(_ok(parsed, stop_reason)))  # type: ignore[arg-type]
    with pytest.raises(LLMError, match=match):
        llm.structured("s", "u", Tiny)
    # usage is still recorded for a billed but unusable reply
    assert llm.usage == [{"model": "claude-opus-5-5", "input": 11, "output": 7}]


@pytest.mark.parametrize(
    ("exc", "match"),
    [
        (lambda: anthropic.RateLimitError("slow down", response=_response(429), body=None), "rate limited"),
        (lambda: anthropic.APIStatusError("server sad", response=_response(500), body=None), "API error 500"),
        (lambda: anthropic.InternalServerError("overloaded", response=_response(529), body=None), "API error 529"),
        (lambda: anthropic.APIConnectionError(request=_request()), "unreachable"),
        (lambda: anthropic.APITimeoutError(request=_request()), "unreachable"),
        (_validation_error, "schema validation"),
    ],
)
def test_exceptions_become_llm_error(exc: Any, match: str) -> None:
    err = exc()
    llm = ClaudeLLM(FakeClient(err))  # type: ignore[arg-type]
    with pytest.raises(LLMError, match=match) as info:
        llm.structured("s", "u", Tiny)
    assert info.value.__cause__ is err
    assert llm.usage == []


def test_rate_limit_is_reported_as_rate_limit_not_generic_status() -> None:
    llm = ClaudeLLM(FakeClient(anthropic.RateLimitError("slow", response=_response(429), body=None)))  # type: ignore[arg-type]
    with pytest.raises(LLMError) as info:
        llm.structured("s", "u", Tiny)
    assert "rate limited" in str(info.value)
    assert "API error" not in str(info.value)


def test_unexpected_exception_is_not_swallowed() -> None:
    llm = ClaudeLLM(FakeClient(KeyError("bug")))  # type: ignore[arg-type]
    with pytest.raises(KeyError):
        llm.structured("s", "u", Tiny)


def test_an_empty_credit_balance_is_its_own_error() -> None:
    from samawi_bot.brain.llm import OUT_OF_CREDIT, LLMOutOfCredit

    exc = anthropic.BadRequestError("Your credit balance is too low to access the Anthropic API.", response=_response(400), body=None)
    with pytest.raises(LLMOutOfCredit, match=OUT_OF_CREDIT):
        ClaudeLLM(FakeClient(exc)).structured("s", "u", Tiny)  # type: ignore[arg-type]
    other = anthropic.BadRequestError("max_tokens is too large", response=_response(400), body=None)
    with pytest.raises(LLMError) as info:
        ClaudeLLM(FakeClient(other)).structured("s", "u", Tiny)  # type: ignore[arg-type]
    assert not isinstance(info.value, LLMOutOfCredit)
