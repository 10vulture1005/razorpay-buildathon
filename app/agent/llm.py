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
import logging
import os
import re

import httpx
from pydantic import BaseModel, ValidationError

import app.config as config

logger = logging.getLogger("app.agent.llm")

# Audit reproducibility (B4): every LLM-driven decision records which prompt
# generation produced it.
PROMPT_VERSION = "2026-08-25"

_last_usage: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "llm_last_usage", default=None)


def get_last_usage() -> dict | None:
    """Usage of the most recent generate_structured call on this thread/context."""
    return _last_usage.get()


def _record_usage(usage: dict):
    _last_usage.set(usage)


class StructuredOutputFailure(Exception):
    pass


def _parse_json_lenient(content: str) -> dict:
    """Parse a JSON object out of model output that ignored the JSON-only
    instruction — markdown fences, leading prose, visible reasoning traces.
    Candidates are balanced {...} spans (string-aware); the LAST one wins
    because reasoning models typically think first and answer last."""
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    unfenced = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.M)
    try:
        return json.loads(unfenced)
    except json.JSONDecodeError:
        pass
    candidates: list[str] = []
    depth = start = 0
    in_str = esc = False
    for i, ch in enumerate(content):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0:
                candidates.append(content[start:i + 1])
    for cand in reversed(candidates):
        try:
            parsed = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("no parsable JSON object found")


class MockLLM:
    """Deterministic stand-in for a frontier model — dev/test ONLY.
    Refused at construction time when ENVIRONMENT=prod."""

    def __init__(self):
        if config.IS_PROD or not config.ALLOW_MOCK_ADAPTERS:
            raise RuntimeError(
                "LLM_PROVIDER=mock requires ALLOW_MOCK_ADAPTERS=true (and is never "
                "permitted in production); configure LLM_PROVIDER=openrouter "
                "with OPENROUTER_API_KEY for real inference"
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

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model_map: dict[str, str] | None = None):
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY") or ""
        if not self.api_key:
            raise RuntimeError("LLM_PROVIDER=openrouter requires OPENROUTER_API_KEY")
        self.base_url = (base_url or os.environ.get("OPENROUTER_BASE_URL",
                         "https://openrouter.ai/api/v1")).rstrip("/")
        self.timeout_s = float(os.environ.get("OPENROUTER_TIMEOUT_S", "30"))
        # Provider-override map for fallback clients: {"frontier": "model-id"}
        self._model_overrides = model_map or {}

    def _model_for(self, tier: str) -> str:
        if tier in self._model_overrides:
            return self._model_overrides[tier]
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
            "no prose, no markdown fences, no explanation, no visible reasoning "
            "or thinking steps; output the JSON object and nothing else:\n"
            f"{json.dumps(schema.model_json_schema())}"
        )
        if schema.__name__ == "InterventionChoice":
            system += (
                "\n\nStrategy: escalate intervention intensity as attempts accumulate. "
                "Attempt 0: a polite send_reminder is usually right. If earlier attempts "
                "were reminders that got no payment, or the customer cites cashflow and "
                "needs an easy way to pay, choose send_payment_link to reduce friction — "
                "do not repeat the same outreach twice. Use wait only when payment is "
                "genuinely not yet due. Reserve escalate_human for exhausted retries, "
                "disputes, or high-stakes judgment calls."
            )
        if schema.__name__ == "ChatReply":
            system += (
                "\n\nWhenever your answer reports numeric data (amounts, counts, "
                "status breakdowns, trends), you MUST attach a chart via the 'chart' "
                "field — default to including one; omit it only for purely qualitative "
                "answers. All monetary amounts are INR — always write them with ₹, never $. "
                "Rules: 'labels' are the category/date names; every series' "
                "'data' array must have EXACTLY one value per label. Use type=bar for "
                "comparing categories, type=line for trends over dates, type=pie for "
                "shares of a whole (one series only)."
            )
        if schema.__name__ == "ChatReply":
            system += (
                "\n\nEMAIL DRAFTING: when the operator asks you to send an email "
                "(e.g. 'send this mail to a@b.com as — reason — tell him —'), fill "
                "the 'email_draft' field with {to, subject, body} and keep 'answer' "
                "to one short line asking them to review, edit and confirm before it "
                "is sent. Use the case context (amounts, history, audit trail) to "
                "write a specific, professional body in INR (₹). You are only the "
                "drafter: NEVER claim an email was sent or will be sent — the human "
                "sends it themselves after reviewing your draft."
            )
        # 429 handling: free tiers enforce per-minute AND daily caps. A burst
        # limit clears in seconds, so wait-and-retry here instead of failing
        # the whole agent run. Daily caps (retry-after absent / huge) fail fast.
        import time as _time

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(prompt)},
            ],
            "temperature": 0.2,
            # Reasoning models burn tokens on hidden/visible thinking
            # before emitting the JSON — leave headroom so it never
            # truncates mid-object (both chat schemas).
            "max_tokens": 2048 if schema.__name__.startswith("ChatReply") else 1024,
            "response_format": {"type": "json_object"},
        }
        # `usage.include` is an OpenRouter-only extension; other
        # OpenAI-compatible hosts (e.g. NVIDIA) reject it with 400.
        if "openrouter.ai" in self.base_url:
            # Ask OpenRouter to include authoritative cost accounting.
            payload["usage"] = {"include": True}
        resp = None
        for attempt in range(3):
            resp = httpx.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "HTTP-Referer": os.environ.get("OPENROUTER_SITE_URL", "http://localhost:8000"),
                    "X-Title": "Revenue Recovery Autopilot",
                },
                json=payload,
                timeout=self.timeout_s,
            )
            if resp.status_code != 429:
                break
            retry_after = resp.headers.get("retry-after")
            if retry_after is not None:
                try:
                    delay = min(float(retry_after), 30.0)
                except ValueError:
                    delay = 5.0
            else:
                try:
                    reset = int((resp.json().get("error", {}).get("metadata", {})
                                 .get("headers", {}).get("X-RateLimit-Reset", "0") or 0)) / 1000.0
                except Exception:
                    reset = 0
                delay = min(max(reset - _time.time(), 2.0), 30.0) if 0 < reset <= 120 else 5.0 * (attempt + 1)
            logger.warning("openrouter 429 | backoff %.1fs (attempt %d)", delay, attempt + 1)
            _time.sleep(delay)
        if resp is None or resp.status_code != 200:
            raise StructuredOutputFailure(
                f"openrouter {getattr(resp, 'status_code', '??')}: {getattr(resp, 'text', '')[:300]}")
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
        if not isinstance(content, str) or not content.strip():
            # Reasoning models occasionally return thinking-only responses
            # with null/empty content. Treat as a clean retryable failure,
            # never a crash.
            raise StructuredOutputFailure(
                f"openrouter returned empty content (finish_reason="
                f"{body['choices'][0].get('finish_reason')})")
        # Transport-decode wrapper noise (markdown fences, stray prose around a
        # single JSON object — free models do this constantly), then require a
        # COMPLETE valid JSON object. The safety invariant is unchanged: broken,
        # truncated, or unparseable output still fails validation and never
        # falls through with defaults.
        data = None
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            # Some models emit literal newlines/tabs INSIDE string values
            # (multi-line message bodies). strict=False tolerates them.
            try:
                data = json.loads(content, strict=False)
            except json.JSONDecodeError:
                try:
                    data = _parse_json_lenient(content)
                except ValueError as e:
                    raise StructuredOutputFailure(
                        f"non-JSON model output: {content[:200]}") from e
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
_FALLBACK_CLIENT: OpenRouterLLM | None = None
_FALLBACK_CHECKED = False
_FALLBACK_LOCK = __import__("threading").Lock()


def _fallback_client() -> OpenRouterLLM | None:
    """Lazily-built NVIDIA fallback client (any OpenAI-compatible host works).
    Returns None when no fallback is configured. Built once; model ids come
    from NVIDIA_MODEL / NVIDIA_SMALL_MODEL with MODEL_* as defaults."""
    global _FALLBACK_CLIENT, _FALLBACK_CHECKED
    if _FALLBACK_CHECKED:
        return _FALLBACK_CLIENT
    with _FALLBACK_LOCK:
        if _FALLBACK_CHECKED:  # double-check under lock
            return _FALLBACK_CLIENT
        _FALLBACK_CHECKED = True
        api_key = getattr(config, "NVIDIA_API_KEY", "")
        if not api_key or os.environ.get("LLM_PROVIDER") != "openrouter":
            return None
        base_url = getattr(config, "NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
        # llama-3.3-70b is the tested-fast choice (~20s); deepseek-v4-flash works
        # but has exhibited multi-minute cold starts under congestion.
        frontier = os.environ.get("NVIDIA_MODEL", "meta/llama-3.3-70b-instruct")
        small = os.environ.get("NVIDIA_SMALL_MODEL", frontier)
        try:
            _FALLBACK_CLIENT = OpenRouterLLM(
                api_key=api_key, base_url=base_url,
                model_map={"frontier": frontier, "small": small},
            )
            # Fallback hosts (NVIDIA) can cold-start slowly; only used when the
            # primary already failed, so a long timeout is acceptable here.
            _FALLBACK_CLIENT.timeout_s = 180.0
            logger.warning("llm.fallback_armed | provider=nvidia models=%s/%s", frontier, small)
        except RuntimeError:
            _FALLBACK_CLIENT = None
        return _FALLBACK_CLIENT


def call_structured(schema: type[BaseModel], prompt: dict, tier: str = "frontier") -> BaseModel:
    """Validate against the schema at the call site. Retry once on failure,
    then fall back to the secondary provider (e.g. NVIDIA) when configured.
    Never falls through with defaults."""
    last_err: Exception | None = None
    _last_usage.set(None)
    for _attempt in range(2):
        try:
            raw = _CLIENT.generate_structured(schema, prompt, tier=tier)
            return schema.model_validate(raw.model_dump() if isinstance(raw, BaseModel) else raw)
        except (StructuredOutputFailure, ValidationError) as e:
            last_err = e

    fb = _fallback_client()
    if fb is not None:
        logger.warning("llm.fallback_invoked | schema=%s err=%s",
                       schema.__name__, str(last_err)[:120])
        try:
            raw = fb.generate_structured(schema, prompt, tier=tier)
            return schema.model_validate(raw.model_dump() if isinstance(raw, BaseModel) else raw)
        except (StructuredOutputFailure, ValidationError) as e:
            last_err = e
    raise StructuredOutputFailure(str(last_err))
