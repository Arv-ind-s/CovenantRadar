"""Gemini presentation preflight uses the actual guarded extraction and memo paths."""

import json
from types import SimpleNamespace

import httpx
import pytest

import covenant_radar.ai as ai
import covenant_radar.config.settings as settings_module
from covenant_radar.demo.readiness import check_live_gemini

pytestmark = pytest.mark.integration


EXTRACTION = {
    "definition": "dscr",
    "custom_formula": None,
    "threshold": "1.25x",
    "direction": "above",
    "unit": "ratio",
    "currency": None,
    "frequency": "quarterly",
    "effective_from": "2026-04-01",
    "effective_to": None,
    "exceptions": [],
    "cure_period_days": 30,
    "source_quote": "DSCR shall not fall below 1.25 times.",
}
MEMO = {
    "headline": "Debt service coverage requires review.",
    "summary": "The recorded value is 1.25 against a threshold of 1.10, with headroom of 0.15. "
    "The recorded risk is 0.42 at confidence 0.88.",
    "drivers": ["ROLE_DRIVER_1"],
    "actions": [{"id": "CREDIT-REDUCE", "role_tag": "credit"}],
    "recommended_next_step": "Review and reduce funded exposure.",
    "disclaimer": "human credit review is required before action",
}


def _install(monkeypatch, *, bad_extraction=False):
    settings = settings_module.load_settings(environ={"GEMINI_API_KEY": "judge-test-credential"})
    monkeypatch.setattr(settings_module, "get_settings", lambda: settings)
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append((request, body))
        reply = ({"invalid": True} if bad_extraction else EXTRACTION) if len(calls) == 1 else MEMO
        return httpx.Response(
            200,
            json={
                "model": "gemini-3.8-flash",
                "choices": [{"message": {"content": json.dumps(reply)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 100},
            },
        )

    original = ai.create_provider
    monkeypatch.setattr(
        ai,
        "create_provider",
        lambda config: original(config, transport=httpx.MockTransport(handler)),
    )
    return calls


def test_preflight_proves_gemini_extraction_and_grounded_memo(monkeypatch):
    calls = _install(monkeypatch)
    check_live_gemini()
    assert len(calls) == 2
    for request, body in calls:
        assert (
            str(request.url)
            == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        )
        assert request.headers["authorization"] == "Bearer judge-test-credential"
        assert body["model"] == "gemini-3.8-flash"
        assert body["response_format"] == {"type": "json_object"}
        assert body["reasoning_effort"] == "low"
        assert "temperature" not in body
        assert "judge-test-credential" not in request.content.decode()


def test_bad_extraction_stops_preflight_before_memo(monkeypatch):
    calls = _install(monkeypatch, bad_extraction=True)
    with pytest.raises(RuntimeError, match="known sample terms"):
        check_live_gemini()
    assert len(calls) == 1


def test_offline_provider_cannot_pass_as_live_gemini(monkeypatch):
    monkeypatch.setattr(
        settings_module,
        "get_settings",
        lambda: SimpleNamespace(ai=SimpleNamespace(provider="recorded")),
    )
    with pytest.raises(RuntimeError, match="require Gemini"):
        check_live_gemini()
