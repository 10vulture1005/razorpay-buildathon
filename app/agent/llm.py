"""LLM client with Pydantic structured-output enforcement.

Providers (LLM_PROVIDER env var):
- mock (default): deterministic heuristic model — key-free, used by tests
- openrouter: any OpenRouter model via the OpenAI-compatible API;
  defaults to NVIDIA Nemotron 3 Ultra (nvidia/nemotron-3-ultra-550b-a55b)

Failure path: schema-validation failure → one retry → second failure raises
StructuredOutputFailure (graph routes to ESCALATED). Free text never flows
downstream: every response must parse into the schema.
"""
import contextvars
import json
import os

import httpx
from pydantic import BaseModel, ValidationError

import app.config as config

# Audit reproducibility (B4): every LLM-driven decision records which prompt
# generation produced it.
PROMPT_VERSION = "2026-08-23"

_last_usage: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "llm_last_usage", default=None)


def get_last_usage() -> dict | None:
    """Usage of the most recent generate_structured call on this thread/context."""
    return _last_usage.get()


def _record_usage(usage: dict):
    _last_usage.set(usage)


class StructuredOutputFailure(Exception):
    pass


class MockLLM:
    """Deterministic stand-in for a frontier model — dev/test ONLY.
    Refused at construction time when ENVIRONMENT=prod."""

    def __init__(self):
        if config.IS_PROD:
            raise RuntimeError(
                "LLM_PROVIDER=mock is not permitted in production; "
                "configure LLM_PROVIDER=openrouter with OPENROUTER_API_KEY"
            )

    def generate_structured(self, schema: type[BaseModel], prompt: dict, tier: str = "frontier") -> BaseModel:
        _record_usage({"model": "mock", "prompt_tokens": 0, "completion_tokens": 0, "cost_est_usd": 0.0})
        if schema.__name__ == "DiagnosisResult":
            return schema(**self._diagnose(prompt))
        if schema.__name__ == "InterventionChoice":
            return schema(**self._select(prompt))
        raise StructuredOutputFailure(f"unknown schema {schema.__name__}")

    def _diagnose(self, prompt: dict) -> dict:
        ctx = prompt["context"]
        if ctx.get("case_type") == "failed_payment":
            cause = {"expired": "card_expired", "invalid": "stale_mandate"}.get(
                ctx.get("payment_method_status"), "insufficient_funds")
            return {"likely_cause": cause, "confidence": 0.8, "reasoning": "mock decline-code mapping"}
        broken = ctx.get("broken_promise_count", 0)
        disputed = any("dispute" in m["body"].lower() for m in ctx.get("messages", []))
        if disputed:
            return {"likely_cause": "dispute", "confidence": 0.9, "reasoning": "dispute language in comms"}
        if broken >= 2:
            return {"likely_cause": "unwilling", "confidence": 0.7, "reasoning": f"{broken} broken promises"}
        if ctx.get("on_time_rate", 0) >= 0.9:
            return {"likely_cause": "forgot", "confidence": 0.75, "reasoning": "strong payer history"}
        return {"likely_cause": "cashflow_issue", "confidence": 0.6, "reasoning": "moderate payment history"}

    def _select(self, prompt: dict) -> dict:
        ctx = prompt["context"]
        attempt = prompt["attempt_number"]
        amount = ctx.get("amount_at_risk", 0)
        diagnosis = prompt["diagnosis"]["likely_cause"]
        if diagnosis == "dispute":
            return {"action": "escalate_human", "expected_recovery_probability": 0.5,
                    "channel": None, "message": None, "reasoning": "disputes need humans"}
        if diagnosis == "unwilling" and amount < 100_000:
            return {"action": "stop", "expected_recovery_probability": 0.1,
                    "channel": None, "message": None, "reasoning": "low value + unwilling"}
        if attempt == 0:
            return {"action": "send_reminder", "expected_recovery_probability": 0.6,
                    "channel": "email", "message": "Friendly reminder about your overdue invoice.",
                    "reasoning": "first touch"}
        if attempt == 1:
            return {"action": "send_payment_link", "expected_recovery_probability": 0.5,
                    "channel": "email", "message": None, "reasoning": "reduce friction"}
        return {"action": "escalate_human", "expected_recovery_probability": 0.4,
                "channel": None, "message": None, "reasoning": "exhausted automated touches"}


class OpenRouterLLM:
    """Any OpenRouter model via https://openrouter.ai/api/v1 (OpenAI-compatible).

    Model-tier routing per the architecture doc: `tier="frontier"` for
    diagnose/select_action (genuine reasoning), `tier="small"` available for
    extraction-style tasks. Both are env-configurable, never hardcoded.

    Structured-output strategy: the JSON Schema is injected into the system
    prompt with a strict JSON-only instruction; responses are parsed and
    Pydantic-validated here. Models that ignore the instruction fail
    validation -> retry -> escalate (never a silent default fallback).
    Cost is read from OpenRouter's usage accounting when available.
    """

    TIER_ENV_KEYS = {"frontier": "MODEL_FRONTIER", "small": "MODEL_SMALL"}

    def __init__(self):
        self.api_key = os.environ.get("OPENROUTER_API_KEY") or ""
        if not self.api_key:
            raise RuntimeError("LLM_PROVIDER=openrouter requires OPENROUTER_API_KEY")
        self.base_url = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        self.timeout_s = float(os.environ.get("OPENROUTER_TIMEOUT_S", "30"))

    def _model_for(self, tier: str) -> str:
        env_key = self.TIER_ENV_KEYS.get(tier)
        # OPENROUTER_MODEL (legacy single-model var) overrides the frontier tier.
        model = ""
        if tier == "frontier":
            model = os.environ.get("OPENROUTER_MODEL", "")
        if not model and env_key:
            model = os.environ.get(env_key, "") or getattr(config, env_key, "") or ""
        if not model or "mock" in model:
            raise RuntimeError(f"no real model configured for tier {tier!r}; set MODEL_FRONTIER/MODEL_SMALL")
        return model

    def generate_structured(self, schema: type[BaseModel], prompt: dict, tier: str = "frontier") -> BaseModel:
        model = self._model_for(tier)
        system = (
            "You are the decision core of a receivables-recovery agent. "
            "Respond with EXACTLY ONE JSON object conforming to this JSON Schema — "
            "no prose, no markdown fences, no explanation:\n"
            f"{json.dumps(schema.model_json_schema())}"
        )
        resp = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "HTTP-Referer": os.environ.get("OPENROUTER_SITE_URL", "http://localhost:8000"),
                "X-Title": "Revenue Recovery Autopilot",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(prompt)},
                ],
                "temperature": 0.2,
                "max_tokens": 512,
                "response_format": {"type": "json_object"},
                # Ask OpenRouter to include authoritative cost accounting.
                "usage": {"include": True},
            },
            timeout=self.timeout_s,
        )
        if resp.status_code != 200:
            raise StructuredOutputFailure(f"openrouter {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        u = body.get("usage") or {}
        cost = u.get("cost")
        if cost is None:  # fallback heuristic only when the API omits cost
            cost = round((u.get("prompt_tokens", 0) * 0.15 + u.get("completion_tokens", 0) * 0.60) / 1e6, 6)
        _record_usage({
            "model": model,
            "prompt_tokens": u.get("prompt_tokens", 0),
            "completion_tokens": u.get("completion_tokens", 0),
            "cost_est_usd": float(cost),
        })
        content = body["choices"][0]["message"]["content"]
        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            raise StructuredOutputFailure(f"non-JSON model output: {content[:200]}") from e
        try:
            return schema.model_validate(data)
        except ValidationError as e:
            raise StructuredOutputFailure(str(e)) from e


def _client(tier: str = "frontier"):
    provider = os.environ.get("LLM_PROVIDER", "mock")
    if provider == "openrouter":
        return OpenRouterLLM()
    if config.IS_PROD:
        raise RuntimeError(
            "ENVIRONMENT=prod requires LLM_PROVIDER=openrouter — the heuristic "
            "mock provider must never drive production recovery decisions"
        )
    return MockLLM()


_CLIENT = _client()


def call_structured(schema: type[BaseModel], prompt: dict, tier: str = "frontier") -> BaseModel:
    """Validate against the schema at the call site. Retry once on failure,
    then raise — never fall through with defaults."""
    last_err: Exception | None = None
    _last_usage.set(None)
    for _attempt in range(2):
        try:
            raw = _CLIENT.generate_structured(schema, prompt, tier=tier)
            return schema.model_validate(raw.model_dump() if isinstance(raw, BaseModel) else raw)
        except (StructuredOutputFailure, ValidationError) as e:
            last_err = e
    raise StructuredOutputFailure(str(last_err))
