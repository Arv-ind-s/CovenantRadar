"""OpenAI-model adapter through LiteLLM, used as the live fail-safe for Gemini.

The fallback endpoint is an OpenAI-compatible gateway (by default the TCS
GenAI Lab LiteLLM proxy). LiteLLM is imported lazily: it is slow to import
and only a deployment that configures the fallback key ever needs it.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

from covenant_radar.ai.errors import (
    ProviderAuthError,
    ProviderConfigurationError,
    ProviderRequestRejected,
    ProviderUnavailable,
)
from covenant_radar.ai.providers.base import (
    normalise_openai_payload,
    openai_messages,
    trust_context,
    validate_endpoint,
)
from covenant_radar.ports.llm import CompletionRequest, CompletionResponse

PROVIDER_NAME = "litellm"
_DEFAULT_TIMEOUT_SECONDS = 30.0
# Gateway errors that mean "try again later", as opposed to a bad request.
_UNAVAILABLE_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class LiteLLMProvider:
    """Complete one request against an OpenAI model behind a LiteLLM gateway."""

    provider_name = PROVIDER_NAME

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ProviderConfigurationError(
                "An API key is required for the LiteLLM fallback.", provider=PROVIDER_NAME
            )
        self.base_url = validate_endpoint(base_url)
        self._api_key = api_key.strip()
        # Never let LiteLLM fetch its model price map from the internet at
        # import time; the copy bundled with the package is enough.
        os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
        import litellm
        import openai

        litellm.telemetry = False
        # Errors are mapped below; LiteLLM's own help banner only adds console noise.
        litellm.suppress_debug_info = True
        self._litellm: Any = litellm
        self._http_client = httpx.Client(
            verify=trust_context(None, provider=PROVIDER_NAME),
            timeout=_DEFAULT_TIMEOUT_SECONDS,
            follow_redirects=False,
            # Same rule as the Gemini adapter: credentials never ride an
            # inherited HTTP(S)_PROXY from the developer's shell.
            trust_env=False,
            transport=transport,
        )
        self._client = openai.OpenAI(
            base_url=self.base_url,
            api_key=self._api_key,
            http_client=self._http_client,
            max_retries=0,
        )

    def close(self) -> None:
        self._http_client.close()

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        if any(
            self._api_key.casefold() in message.content.casefold() for message in request.messages
        ):
            raise ProviderConfigurationError(
                "Fallback credential detected in prompt; request refused.", provider=PROVIDER_NAME
            )
        options: dict[str, object] = {
            "max_tokens": request.max_tokens,
            "timeout": request.timeout_seconds or _DEFAULT_TIMEOUT_SECONDS,
        }
        if request.temperature is not None:
            options["temperature"] = request.temperature
        if request.prompt_version is not None:
            # Both application stages require a strict JSON object, checked again locally.
            options["response_format"] = {"type": "json_object"}
        started = time.perf_counter()
        try:
            response = self._litellm.completion(
                # "openai/" routes through LiteLLM's OpenAI-compatible client;
                # the gateway receives the model id that follows unchanged.
                model=f"openai/{request.model}",
                messages=openai_messages(request),
                api_base=self.base_url,
                api_key=self._api_key,
                client=self._client,
                # Reasoning models refuse `temperature`; drop it rather than fail.
                drop_params=True,
                num_retries=0,
                **options,
            )
        except Exception as error:
            raise _provider_error(self._litellm, error) from None
        latency_ms = int((time.perf_counter() - started) * 1000)
        return normalise_openai_payload(response.model_dump(), latency_ms=latency_ms)


def _provider_error(litellm: Any, error: Exception) -> Exception:
    # Never copy gateway text into the error: it can echo request content.
    if isinstance(error, litellm.AuthenticationError):
        return ProviderAuthError(PROVIDER_NAME)
    if isinstance(error, litellm.Timeout | TimeoutError | httpx.TimeoutException):
        return ProviderUnavailable(PROVIDER_NAME, reason="timeout")
    status = getattr(error, "status_code", None)
    if isinstance(status, int):
        if status in {401, 403}:
            return ProviderAuthError(PROVIDER_NAME)
        if status in _UNAVAILABLE_STATUSES:
            return ProviderUnavailable(PROVIDER_NAME, reason=f"http status {status}")
        return ProviderRequestRejected(PROVIDER_NAME, status_code=status)
    return ProviderUnavailable(PROVIDER_NAME, reason="transport failure")


__all__ = ["LiteLLMProvider"]
