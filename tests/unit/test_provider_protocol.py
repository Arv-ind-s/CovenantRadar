"""Unit tests for the provider-neutral protocol and adapter guarantees."""

from __future__ import annotations

import ssl
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from covenant_radar.ai import create_provider
from covenant_radar.ai.errors import ProviderConfigurationError, ProviderUnavailable
from covenant_radar.ai.providers.base import trust_context
from covenant_radar.ai.providers.gemini import GeminiProvider
from covenant_radar.ai.providers.recorded import RecordedProvider
from covenant_radar.config.settings import SettingsError, load_settings
from covenant_radar.ports.llm import CompletionRequest, CompletionResponse, LLMProvider


def _self_signed_ca(common_name: str) -> bytes:
    """Build a throwaway PEM CA that certifi cannot already contain."""

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM)


def _request() -> CompletionRequest:
    return CompletionRequest(
        messages=[
            {"role": "system", "content": "Answer briefly."},
            {"role": "user", "content": "What is DSCR?"},
        ],
        model="gemini-3.8-flash",
    )


def test_one_response_shape_across_adapters() -> None:
    seen_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(
            200,
            json={
                "model": "gateway-returned",
                "choices": [{"message": {"role": "assistant", "content": "answer"}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 3},
            },
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    providers: list[LLMProvider] = [
        GeminiProvider(
            endpoint="https://generativelanguage.googleapis.com/v1beta/openai",
            api_key="key",
            http_client=client,
        ),
        RecordedProvider(
            responses={
                "replay": {
                    "text": "recorded answer",
                    "model": "recorded-model",
                    "input_tokens": 4,
                    "output_tokens": 3,
                    "latency_ms": 1,
                }
            }
        ),
    ]
    recorded_request = CompletionRequest(
        messages=[{"role": "user", "content": "replay"}],
        model="gemini-3.8-flash",
        cassette_key="replay",
    )

    try:
        responses = [provider.complete(_request()) for provider in providers[:1]]
        responses.append(providers[1].complete(recorded_request))
    finally:
        client.close()

    assert all(isinstance(response, CompletionResponse) for response in responses)
    assert [response.text for response in responses] == [
        "answer",
        "recorded answer",
    ]
    assert all(response.model for response in responses)
    assert all(response.input_tokens == 4 for response in responses)
    assert all(response.output_tokens == 3 for response in responses)
    assert [request.url.path for request in seen_requests] == [
        "/v1beta/openai/chat/completions",
    ]
    assert seen_requests[0].headers["authorization"] == "Bearer key"


def test_adapter_does_not_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("fixture transport failure", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = GeminiProvider(
        endpoint="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key="key",
        http_client=client,
    )
    try:
        with pytest.raises(ProviderUnavailable):
            provider.complete(_request())
    finally:
        client.close()

    assert calls == 1


def test_unknown_provider_refused_at_startup(tmp_path) -> None:
    config_file = tmp_path / "settings.toml"
    config_file.write_text('[ai]\nprovider = "unknown"\n', encoding="utf-8")

    with pytest.raises(SettingsError) as raised:
        load_settings(config_file, environ={})

    message = str(raised.value)
    assert "unknown" in message
    assert "recorded" in message
    assert "gemini" in message

    with pytest.raises(ProviderConfigurationError, match="Valid providers"):
        create_provider(type("UnknownSettings", (), {"provider": "unknown"})())


@pytest.mark.parametrize(
    "provider_class",
    [GeminiProvider],
)
def test_tls_verification_cannot_be_disabled(provider_class) -> None:
    with pytest.raises(ValueError, match="TLS certificate verification"):
        provider_class(endpoint="https://provider.example", api_key="key", verify=False)


def test_ca_bundle_adds_anchors_without_weakening_verification(tmp_path) -> None:
    """A corporate CA is trusted *as well as* the public roots, never instead."""

    bundle = tmp_path / "corporate-ca.pem"
    bundle.write_bytes(_self_signed_ca("Covenant Radar Test CA"))
    default_anchors = len(httpx.create_ssl_context().get_ca_certs())

    context = trust_context(bundle, provider="gemini")

    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True
    anchors = context.get_ca_certs()
    # httpx's own default is loaded first, so the bundle can only widen trust.
    assert len(anchors) == default_anchors + 1
    assert any(
        ("commonName", "Covenant Radar Test CA") in attribute
        for anchor in anchors
        for attribute in anchor["subject"]
    )


def test_no_ca_bundle_leaves_the_httpx_default_untouched() -> None:
    assert trust_context(None, provider="gemini") is True


def test_unreadable_ca_bundle_is_refused_as_configuration(tmp_path) -> None:
    bundle = tmp_path / "not-a-certificate.pem"
    bundle.write_text("this is not PEM\n", encoding="utf-8")

    with pytest.raises(ProviderConfigurationError, match="CA bundle"):
        GeminiProvider(
            endpoint="https://generativelanguage.googleapis.com/v1beta/openai",
            api_key="key",
            ca_bundle=bundle,
        )


def test_ca_bundle_refused_alongside_an_injected_client(tmp_path) -> None:
    bundle = tmp_path / "corporate-ca.pem"
    bundle.write_bytes(_self_signed_ca("Covenant Radar Test CA"))
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    try:
        with pytest.raises(ValueError, match="adapter-created client"):
            GeminiProvider(
                endpoint="https://generativelanguage.googleapis.com/v1beta/openai",
                api_key="key",
                http_client=client,
                ca_bundle=bundle,
            )
    finally:
        client.close()


def test_gemini_key_enables_default_provider() -> None:
    settings = load_settings(environ={"GEMINI_API_KEY": "test-gemini-key"})
    assert settings.ai.provider == "gemini"
    assert settings.ai.model == "gemini-3.8-flash"
    assert settings.ai.endpoint == "https://generativelanguage.googleapis.com/v1beta/openai"
    assert settings.ai.api_key.get_secret_value() == "test-gemini-key"
    assert "test-gemini-key" not in repr(settings.ai)

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "model": "gemini-3.8-flash",
                "choices": [{"message": {"content": "Gemini answer"}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 3},
            },
        )

    provider = create_provider(settings.ai, transport=httpx.MockTransport(handler))
    try:
        response = provider.complete(_request())
    finally:
        provider.close()
    assert response.text == "Gemini answer"
    assert str(requests[0].url) == (
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    )
    assert requests[0].headers["authorization"] == "Bearer test-gemini-key"


def test_gemini_key_respects_explicit_disabled_provider() -> None:
    settings = load_settings(
        environ={"GEMINI_API_KEY": "test-key", "COVENANT_RADAR_AI__PROVIDER": "none"}
    )
    assert settings.ai.provider == "none"


def test_explicit_gemini_requires_key() -> None:
    with pytest.raises(SettingsError, match="GEMINI_API_KEY"):
        load_settings(environ={"COVENANT_RADAR_AI__PROVIDER": "gemini"})


@pytest.mark.parametrize("provider", ["azure_openai", "anthropic"])
def test_non_gemini_live_provider_refused(provider) -> None:
    with pytest.raises(SettingsError):
        load_settings(environ={"COVENANT_RADAR_AI__PROVIDER": provider, "GEMINI_API_KEY": "key"})
    with pytest.raises(ProviderConfigurationError):
        create_provider(type("LegacySettings", (), {"provider": provider})())


def test_gemini_refuses_foreign_endpoint_and_model() -> None:
    for overrides in [
        {"COVENANT_RADAR_AI__ENDPOINT": "https://old-gateway.example"},
        {"COVENANT_RADAR_AI__MODEL": "gpt-5"},
    ]:
        with pytest.raises(SettingsError):
            load_settings(environ={"GEMINI_API_KEY": "test-key", **overrides})


def test_old_key_cannot_enable_gemini() -> None:
    with pytest.raises(SettingsError):
        load_settings(
            environ={
                "COVENANT_RADAR_AI__PROVIDER": "gemini",
                "COVENANT_RADAR_AI_API_KEY": "old-key",
            }
        )


def test_gemini_key_is_redacted_from_prompt(monkeypatch) -> None:
    from covenant_radar.ai.masking import build_outbound

    monkeypatch.setenv("GEMINI_API_KEY", "unique-gemini-credential")
    prompt = build_outbound({"clause_text": "The credential is unique-gemini-credential."})
    assert "unique-gemini-credential" not in prompt.content


def test_gemini_blocks_key_in_prompt_before_http() -> None:
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={})

    with GeminiProvider(
        api_key="unique-gemini-key", transport=httpx.MockTransport(handler)
    ) as provider:
        with pytest.raises(ProviderConfigurationError, match="credential detected"):
            provider.complete(
                CompletionRequest(
                    messages=[{"role": "user", "content": "unique-gemini-key"}],
                    model="gemini-3.8-flash",
                )
            )
    assert calls == []


def test_nested_key_override_cannot_replace_gemini_key() -> None:
    with pytest.raises(SettingsError, match="GEMINI_API_KEY"):
        load_settings(
            environ={
                "GEMINI_API_KEY": "gemini-credential",
                "COVENANT_RADAR_AI__API_KEY": "different-credential",
            }
        )


def test_gemini_key_works_with_default_config_selected_explicitly() -> None:
    from covenant_radar.config.settings import DEFAULT_CONFIG_PATH

    settings = load_settings(DEFAULT_CONFIG_PATH, environ={"GEMINI_API_KEY": "test-key"})
    assert settings.ai.provider == "gemini"
