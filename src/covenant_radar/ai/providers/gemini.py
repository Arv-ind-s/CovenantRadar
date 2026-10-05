"""Direct Google Gemini adapter using its OpenAI-compatible REST API."""

from __future__ import annotations

from pathlib import Path

import httpx

from covenant_radar.ai.errors import ProviderConfigurationError
from covenant_radar.ai.providers.base import (
    BaseHttpProvider,
    append_path,
    normalise_openai_payload,
    openai_body,
)
from covenant_radar.ports.llm import CompletionRequest, CompletionResponse


class GeminiProvider(BaseHttpProvider):
    """Adapter for the default Google Gemini API."""

    def __init__(
        self,
        *,
        endpoint: str = "https://generativelanguage.googleapis.com/v1beta/openai",
        api_key: str,
        http_client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        verify: bool = True,
        ca_bundle: Path | str | None = None,
    ) -> None:
        super().__init__(
            provider_name="gemini",
            endpoint=endpoint,
            api_key=api_key,
            http_client=http_client,
            transport=transport,
            verify=verify,
            ca_bundle=ca_bundle,
        )

        if self.endpoint != "https://generativelanguage.googleapis.com/v1beta/openai":
            self.close()
            raise ProviderConfigurationError(
                "Gemini must use the official Google Gemini API endpoint.", provider="gemini"
            )

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        if not request.model.startswith("gemini-"):
            raise ProviderConfigurationError("A Gemini model ID is required.", provider="gemini")
        if any(
            self._api_key.casefold() in message.content.casefold() for message in request.messages
        ):
            raise ProviderConfigurationError(
                "Gemini credential detected in prompt; request refused.", provider="gemini"
            )
        payload, latency_ms = self._post_json(
            request,
            url=append_path(self.endpoint, "chat/completions"),
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            body=_gemini_body(request),
        )
        return normalise_openai_payload(payload, latency_ms=latency_ms)


__all__ = ["GeminiProvider"]


def _gemini_body(request: CompletionRequest) -> dict[str, object]:
    body = openai_body(request)
    if request.model.startswith(("gemini-3.", "gemini-3-")):
        # Gemini 3 migration guidance removes legacy sampling parameters.
        body.pop("temperature", None)
        body["reasoning_effort"] = "low"
    if request.prompt_version is not None:
        # Both application stages require a strict JSON object, checked again locally.
        body["response_format"] = {"type": "json_object"}
    return body
