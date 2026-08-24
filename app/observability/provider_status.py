"""Loud, per-boot statement of which providers are live vs mock/console.

Prevents the "silently running on fake adapters while believing it's live"
failure mode: every service logs this at startup.
"""
import logging

import app.config as config

logger = logging.getLogger("app.providers")


def _status(name: str, provider: str) -> str:
    if provider == "console" or (name == "llm" and provider == "mock"):
        return f"{provider} (MOCK)"
    return f"{provider} (live)"


def provider_status() -> dict[str, str]:
    return {
        "llm": _status("llm", config.LLM_PROVIDER),
        "email": _status("email", config.EMAIL_PROVIDER),
        "payments": _status("payments", config.PAYMENT_PROVIDER),
    }


def log_provider_status() -> None:
    s = provider_status()
    logger.warning(
        "provider.status | LLM: %s | Email: %s | Payments: %s%s",
        s["llm"], s["email"], s["payments"],
        "" if all("(live)" in v for v in s.values())
        else "  <<< MOCK ADAPTERS IN USE — NOT SENDING REAL EMAIL / CREATING REAL PAYMENTS",
    )
