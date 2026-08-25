"""OpenRouter provider tests: request shape, parsing, failure paths.
HTTP is mocked — no API key or network needed."""
import json

import httpx
import pytest

from app.agent import llm as llm_mod
from app.agent.llm import OpenRouterLLM, StructuredOutputFailure
from app.models.schemas import DiagnosisResult


@pytest.fixture()
def provider(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MODEL_FRONTIER", "nvidia/nemotron-3-ultra-550b-a55b")
    # Hermetic: a developer's .env sets OPENROUTER_MODEL (legacy override) —
    # it must not leak into this test's expected request shape.
    monkeypatch.setenv("OPENROUTER_MODEL", "")
    p = OpenRouterLLM()
    # route the module-level client through the OpenRouter provider for this test
    monkeypatch.setattr(llm_mod, "_CLIENT", p)
    return p


def _capture(monkeypatch, response_json, status=200):
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        req = httpx.Request("POST", url, json=kwargs["json"])
        return httpx.Response(status, json=response_json, request=req)

    monkeypatch.setattr(httpx, "post", fake_post)
    return captured


def test_request_shape_and_parsing(provider, monkeypatch):
    good = DiagnosisResult(likely_cause="forgot", confidence=0.8,
                           reasoning="strong history").model_dump()
    captured = _capture(monkeypatch, {
        "choices": [{"message": {"content": json.dumps(good)}}]
    })
    result = llm_mod.call_structured(
        DiagnosisResult, {"context": {"on_time_rate": 0.95}})
    assert result.likely_cause == "forgot"
    assert captured["url"].endswith("/chat/completions")
    body = captured["json"]
    assert body["model"] == "nvidia/nemotron-3-ultra-550b-a55b"
    assert body["response_format"] == {"type": "json_object"}
    # schema travels in the system prompt so any model can comply
    assert '"likely_cause"' in body["messages"][0]["content"]
    assert captured["headers"]["Authorization"] == "Bearer sk-test"


def test_markdown_fenced_output_parsed(provider, monkeypatch):
    """Fences are transport noise, not semantic content: a COMPLETE valid JSON
    object inside them must parse. Broken/truncated output must still fail."""
    _capture(monkeypatch, {
        "choices": [{"message": {"content": '```json\n{"likely_cause":"forgot","confidence":0.7,"reasoning":"ok"}\n```'}}]
    })
    result = llm_mod.call_structured(DiagnosisResult, {"context": {}})
    assert result.likely_cause == "forgot"

    _capture(monkeypatch, {
        "choices": [{"message": {"content": 'I thought about it ```json\n{"likely_cause":"forgot"}'}}]
    })
    with pytest.raises(StructuredOutputFailure):
        llm_mod.call_structured(DiagnosisResult, {"context": {}})


def test_api_error_surfaces_as_structured_failure(provider, monkeypatch):
    _capture(monkeypatch, {"error": {"message": "rate limited"}}, status=429)
    with pytest.raises(StructuredOutputFailure):
        llm_mod.call_structured(DiagnosisResult, {"context": {}})


def test_missing_key_fails_fast(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        OpenRouterLLM()


def test_default_provider_still_mock():
    """No env config → mock; the suite runs key-free."""
    import os
    assert os.environ.get("LLM_PROVIDER", "mock") == "mock"
