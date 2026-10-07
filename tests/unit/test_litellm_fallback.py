"""The LiteLLM fail-safe: the adapter itself and the client's switch to it."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast

import httpx
import pytest

from covenant_radar.ai import create_fallback_provider
from covenant_radar.ai.budget import BudgetLimits
from covenant_radar.ai.client import (
    CallContext,
    InMemoryModelCallWriter,
    MaskedPrompt,
    ModelClient,
)
from covenant_radar.ai.errors import (
    ProviderAuthError,
    ProviderConfigurationError,
    ProviderRequestRejected,
    ProviderUnavailable,
)
from covenant_radar.ai.providers.litellm_proxy import LiteLLMProvider
from covenant_radar.config.settings import load_settings
from covenant_radar.core.clock import FixedClock
from covenant_radar.ports.llm import CompletionRequest, CompletionResponse, LLMProvider

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 10, 7, 8, 0, tzinfo=UTC)
_BASE = "https://genailab.tcs.in"
_MODEL = "genailab-maas-gpt-4o"


def _gateway_reply(text: str = '{"ok": true}') -> dict[str, object]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": _MODEL,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16},
    }


def _litellm(handler: object) -> LiteLLMProvider:
    return LiteLLMProvider(
        base_url=_BASE, api_key="sk-fallback", transport=httpx.MockTransport(handler)
    )


def _request(**overrides: object) -> CompletionRequest:
    values: dict[str, object] = {
        "messages": [{"role": "user", "content": "masked clause"}],
        "model": _MODEL,
        "max_tokens": 64,
        "prompt_version": "v1",
    }
    values.update(overrides)
    return CompletionRequest(**values)  # type: ignore[arg-type]


def test_litellm_sends_the_openai_model_to_the_gateway() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_gateway_reply())

    response = _litellm(handler).complete(_request())

    assert str(seen[0].url) == f"{_BASE}/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer sk-fallback"
    body = json.loads(seen[0].content)
    assert body["model"] == _MODEL
    assert body["max_tokens"] == 64
    assert body["response_format"] == {"type": "json_object"}
    assert response.text == '{"ok": true}'
    assert (response.model, response.input_tokens, response.output_tokens) == (_MODEL, 11, 5)


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, ProviderAuthError),
        (429, ProviderUnavailable),
        (503, ProviderUnavailable),
        (400, ProviderRequestRejected),
    ],
)
def test_litellm_maps_gateway_failures(status: int, error: type[Exception]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "echoed prompt text"}})

    with pytest.raises(error) as raised:
        _litellm(handler).complete(_request())

    assert "echoed prompt text" not in str(raised.value)


def test_litellm_refuses_a_prompt_containing_its_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not be sent")

    with pytest.raises(ProviderConfigurationError):
        _litellm(handler).complete(
            _request(messages=[{"role": "user", "content": "key sk-fallback"}])
        )


# --------------------------------------------------------------------------- client


class _Provider:
    def __init__(self, name: str, responses: Sequence[object]) -> None:
        self.provider_name = name
        self.requests: list[CompletionRequest] = []
        self._responses = list(responses)

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return cast(CompletionResponse, response)


def _response(model: str) -> CompletionResponse:
    return CompletionResponse(
        text="draft", model=model, input_tokens=8, output_tokens=4, latency_ms=2, raw_payload={}
    )


def _prompt() -> MaskedPrompt:
    return MaskedPrompt(
        messages=[
            {"role": "system", "content": "prompt-version: v1"},
            {"role": "user", "content": "masked ratio value"},
        ],
        version="v1",
    )


def _client(
    primary: LLMProvider, fallback: LLMProvider | None, writer: InMemoryModelCallWriter
) -> ModelClient:
    return ModelClient(
        primary,
        model="gemini-3.5-flash",
        budget=BudgetLimits(calls_per_hour=10, calls_per_day=20),
        model_calls=writer,
        clock=FixedClock(_NOW),
        fallback_provider=fallback,
        fallback_model=_MODEL if fallback is not None else None,
    )


@pytest.mark.parametrize(
    ("failures", "primary_calls"),
    [
        # Unavailable is retried once on Gemini before switching.
        ([ProviderUnavailable("gemini", reason="http status 503")] * 2, 2),
        ([ProviderAuthError("gemini")], 1),
        ([ProviderRequestRejected("gemini", status_code=404)], 1),
    ],
)
def test_client_switches_to_the_fallback_when_gemini_fails(
    failures: list[Exception], primary_calls: int
) -> None:
    gemini = _Provider("gemini", failures)
    litellm = _Provider("litellm", [_response(_MODEL)])
    writer = InMemoryModelCallWriter()

    result = _client(gemini, litellm, writer).call(
        7, _prompt(), "v1", CallContext(request_id="rq-fallback")
    )

    assert result.text == "draft"
    assert result.from_cassette is False
    assert len(gemini.requests) == primary_calls
    assert [request.model for request in litellm.requests] == [_MODEL]
    assert litellm.requests[0].messages == gemini.requests[0].messages
    final = writer.records[-1]
    assert (final.provider, final.model_version, final.check_verdict) == (
        "litellm",
        _MODEL,
        "not_checked",
    )
    assert final.from_cassette is False


def test_client_does_not_fall_back_on_a_safety_refusal() -> None:
    gemini = _Provider("gemini", [ProviderConfigurationError("credential in prompt")])
    litellm = _Provider("litellm", [_response(_MODEL)])

    with pytest.raises(ProviderConfigurationError):
        _client(gemini, litellm, InMemoryModelCallWriter()).call(
            7, _prompt(), "v1", CallContext(request_id="rq-safety")
        )

    assert litellm.requests == []


def test_client_raises_the_primary_failure_when_the_fallback_also_fails() -> None:
    gemini = _Provider("gemini", [ProviderAuthError("gemini")])
    litellm = _Provider("litellm", [ProviderUnavailable("litellm", reason="http status 503")])
    writer = InMemoryModelCallWriter()

    with pytest.raises(ProviderAuthError):
        _client(gemini, litellm, writer).call(
            7, _prompt(), "v1", CallContext(request_id="rq-both")
        )

    assert [record.check_verdict for record in writer.records] == [
        "provider_auth_refused",
        "fallback_unavailable",
    ]


def test_without_a_fallback_the_client_behaves_as_before() -> None:
    gemini = _Provider("gemini", [ProviderAuthError("gemini")])

    with pytest.raises(ProviderAuthError):
        _client(gemini, None, InMemoryModelCallWriter()).call(
            7, _prompt(), "v1", CallContext(request_id="rq-none")
        )


# --------------------------------------------------------------------------- settings


def _settings(**environment: str) -> object:
    return load_settings(
        environ={
            "COVENANT_RADAR_DATABASE__URL": "sqlite:///:memory:",
            "COVENANT_RADAR_SECURITY_SESSION_SECRET": "s" * 32,
            "COVENANT_RADAR_AI__PROVIDER": "gemini",
            **environment,
        }
    )


def test_fallback_is_off_until_its_key_is_set() -> None:
    settings = _settings(GEMINI_API_KEY="gemini-key")

    assert create_fallback_provider(settings.ai) is None  # type: ignore[attr-defined]


def test_fallback_key_alone_lets_the_app_start_without_gemini() -> None:
    settings = _settings(COVENANT_RADAR_AI_FALLBACK_API_KEY="sk-fallback")
    ai = settings.ai  # type: ignore[attr-defined]

    assert ai.api_key is None
    assert ai.fallback_base_url == _BASE
    fallback = create_fallback_provider(ai)
    assert fallback is not None
    provider, model = fallback
    assert isinstance(provider, LiteLLMProvider)
    assert model == _MODEL
