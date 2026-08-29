"""Pre-deploy config validation.

Runs every check that the running app would otherwise fail on at startup
or at first request. Use this in CI / as a pre-push hook / as a pre-deploy
gate so misconfigurations are caught at build time, not at 3am in prod.

Exit codes:
  0  - all checks pass
  1  - one or more checks failed (caller should refuse to deploy)
  2  - usage error

Usage:
  python -m scripts.preflight                    # auto-detect: prod if ENVIRONMENT=prod
  python -m scripts.preflight --env prod         # force production rules
  python -m scripts.preflight --env dev          # dev rules (lenient)
  python -m scripts.preflight --strict           # fail on warnings too
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from typing import Callable

import app.config as config


@dataclass
class Check:
    name: str
    fn: Callable[["Env"], str | None]   # returns None on success, error msg on fail
    severity: str                       # "error" | "warn"
    applies_to: set[str]                # {"prod"}, {"dev"}, or {"prod","dev"}


@dataclass
class Env:
    name: str   # "prod" | "dev"
    is_prod: bool


def _check(name: str, applies: set[str], severity: str = "error"):
    def decorator(fn):
        Check(name=name, fn=fn, severity=severity, applies_to=applies)
        return fn
    return decorator


# --- check implementations ---

def c_api_keys(env: Env) -> str | None:
    if not config.API_KEYS_RAW:
        return "API_KEYS is not set"
    raw = config.API_KEYS_RAW
    if env.is_prod:
        if any(s.strip() in ("", "change-me-admin-key", "dev-admin-key", "test-admin-key")
               for s in _iter_api_key_secrets(raw)):
            return "API_KEYS contains a placeholder/dev secret; rotate before prod"
    parsed = []
    secrets = _iter_api_key_secrets(raw)
    if not secrets:
        return "API_KEYS has no valid <secret>:<scopes> entries"
    for secret in secrets:
        if len(secret) < 16:
            return f"API_KEY secret too short (min 16 chars): {secret[:4]}..."
    scopes_per_entry = _iter_api_key_scopes(raw)
    for scopes in scopes_per_entry:
        if not scopes:
            return "an API_KEY entry has no scopes"
        if not scopes <= {"read", "run", "admin"}:
            return f"API_KEY entry has unknown scopes: {scopes}"
    if env.is_prod and not any("admin" in s for s in scopes_per_entry):
        return "no API_KEY has admin scope — production would be unmanageable"
    return None


def _iter_api_key_secrets(raw: str) -> list[str]:
    """Mirror of `app.security.auth._parse_keys` — extract just the secrets."""
    return [s for s in (entry.partition(":")[0].strip()
                        for entry in _split_api_keys(raw)) if s]


def _iter_api_key_scopes(raw: str) -> list[set[str]]:
    """Mirror of `app.security.auth._parse_keys` — extract just the scopes."""
    return [{sc for sc in (s.strip() for s in entry.partition(":")[2].split(",")) if sc}
            for entry in _split_api_keys(raw)]


def _split_api_keys(raw: str) -> list[str]:
    """Split the API_KEYS env var on token boundaries (NOT on commas, since a
    comma may be a scope separator inside an entry)."""
    import re
    return [p.strip() for p in re.split(r",\s*(?=\S[^,]*:)", raw) if p.strip()]


def c_cors(env: Env) -> str | None:
    if not config.CORS_ORIGINS:
        return "CORS_ORIGINS is empty"
    if env.is_prod:
        for o in config.CORS_ORIGINS:
            if o.startswith("http://") and "localhost" not in o:
                return f"CORS origin uses http:// in prod: {o}"
            if "*" in o:
                return "CORS_ORIGINS must not contain a wildcard"
    return None


def c_llm(env: Env) -> str | None:
    if env.is_prod:
        if config.LLM_PROVIDER != "openrouter":
            return f"LLM_PROVIDER must be 'openrouter' in prod, got {config.LLM_PROVIDER!r}"
        if not os.environ.get("OPENROUTER_API_KEY"):
            return "OPENROUTER_API_KEY is required in prod"
    if config.LLM_PROVIDER == "openrouter":
        if not config.MODEL_FRONTIER or "mock" in config.MODEL_FRONTIER.lower():
            return f"MODEL_FRONTIER looks like a mock: {config.MODEL_FRONTIER!r}"
    return None


def c_email(env: Env) -> str | None:
    if env.is_prod:
        if config.EMAIL_PROVIDER == "console":
            return "EMAIL_PROVIDER=console is dev-only"
        if not config.EMAIL_FROM or "@localhost" in config.EMAIL_FROM:
            return f"EMAIL_FROM is not a real address: {config.EMAIL_FROM!r}"
    if config.EMAIL_PROVIDER == "mailgun":
        if not config.MAILGUN_API_KEY:
            return "MAILGUN_API_KEY is required when EMAIL_PROVIDER=mailgun"
        if not config.MAILGUN_DOMAIN:
            return "MAILGUN_DOMAIN is required when EMAIL_PROVIDER=mailgun"
    if config.EMAIL_PROVIDER == "smtp":
        if not config.SMTP_HOST or not config.SMTP_USER or not config.SMTP_PASSWORD:
            return "SMTP_HOST, SMTP_USER, SMTP_PASSWORD required when EMAIL_PROVIDER=smtp"
    if config.EMAIL_PROVIDER == "resend" and not config.RESEND_API_KEY:
        return "RESEND_API_KEY required when EMAIL_PROVIDER=resend"
    if config.EMAIL_PROVIDER == "sendgrid" and not config.SENDGRID_API_KEY:
        return "SENDGRID_API_KEY required when EMAIL_PROVIDER=sendgrid"
    return None


def c_payments(env: Env) -> str | None:
    if env.is_prod:
        if config.PAYMENT_PROVIDER != "razorpay":
            return f"PAYMENT_PROVIDER must be 'razorpay' in prod, got {config.PAYMENT_PROVIDER!r}"
        if not config.RAZORPAY_KEY_ID or not config.RAZORPAY_KEY_SECRET:
            return "RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET are required in prod"
        if not config.PAYMENT_WEBHOOK_SECRET:
            return "RAZORPAY_WEBHOOK_SECRET (or PAYMENT_WEBHOOK_SECRET) is required in prod"
        if not (config.RAZORPAY_KEY_ID.startswith("rzp_live_")
                or config.RAZORPAY_KEY_ID.startswith("rzp_test_")):
            return f"RAZORPAY_KEY_ID looks malformed: {config.RAZORPAY_KEY_ID[:8]}..."
    if config.PAYMENT_PROVIDER == "razorpay" and not config.PAYMENT_WEBHOOK_SECRET:
        return "PAYMENT_WEBHOOK_SECRET is required when PAYMENT_PROVIDER=razorpay"
    return None


def c_database_url(env: Env) -> str | None:
    if not config.DATABASE_URL:
        return "DATABASE_URL is not set"
    if env.is_prod and "sqlite" in config.DATABASE_URL:
        return "DATABASE_URL points to SQLite — use Postgres in prod"
    if env.is_prod and "postgres" in config.DATABASE_URL:
        if "localhost" in config.DATABASE_URL or "127.0.0.1" in config.DATABASE_URL:
            return "DATABASE_URL points at localhost in prod"
    return None


def c_write_tools(env: Env) -> str | None:
    if env.is_prod and not config.WRITE_TOOLS_ENABLED:
        return ("WRITE_TOOLS_ENABLED=false in prod is intentional but worth confirming — "
                "no reminders, payment links, or escalations will be sent")
    return None


def c_mock_adapters(env: Env) -> str | None:
    if env.is_prod and config.ALLOW_MOCK_ADAPTERS:
        return "ALLOW_MOCK_ADAPTERS=true in prod — set to false to refuse mock providers"
    return None


def c_rate_limit(env: Env) -> str | None:
    if config.RATE_LIMIT_PER_MINUTE <= 0:
        return "RATE_LIMIT_PER_MINUTE must be > 0"
    if env.is_prod and config.RATE_LIMIT_PER_MINUTE > 10_000:
        return ("RATE_LIMIT_PER_MINUTE is suspiciously high (>10000) — "
                "confirm this is intentional")
    return None


def c_body_cap(env: Env) -> str | None:
    if config.MAX_BODY_BYTES < 1024:
        return f"MAX_BODY_BYTES too small: {config.MAX_BODY_BYTES} (min 1024)"
    if config.MAX_BODY_BYTES > 10 * 1024 * 1024:
        return f"MAX_BODY_BYTES suspiciously large: {config.MAX_BODY_BYTES}"
    return None


def c_secrets_in_logs(env: Env) -> str | None:
    """Catch a common oops: a real API key accidentally logged via JsonFormatter."""
    # We can't introspect the logger from here without running it, but we can
    # warn if a known-bad var pattern is set (very long key in DEBUG mode).
    if os.environ.get("LOG_LEVEL", "").upper() == "DEBUG":
        return "LOG_LEVEL=DEBUG will dump request/response bodies — disable in prod"
    return None


# --- registry ---

ALL_CHECKS: list[Check] = [
    Check("API_KEYS",            c_api_keys,        "error", {"prod", "dev"}),
    Check("CORS_ORIGINS",        c_cors,            "error", {"prod", "dev"}),
    Check("LLM provider",        c_llm,             "error", {"prod", "dev"}),
    Check("Email provider",      c_email,           "error", {"prod", "dev"}),
    Check("Payment provider",    c_payments,        "error", {"prod", "dev"}),
    Check("DATABASE_URL",        c_database_url,    "error", {"prod", "dev"}),
    Check("Mock adapters",       c_mock_adapters,   "error", {"prod"}),
    Check("Rate limit",          c_rate_limit,      "warn",  {"prod", "dev"}),
    Check("Body cap",            c_body_cap,        "warn",  {"prod", "dev"}),
    Check("WRITE_TOOLS_ENABLED", c_write_tools,     "warn",  {"prod"}),
    Check("Log level",           c_secrets_in_logs, "warn",  {"prod", "dev"}),
]


def run(env: Env, strict: bool = False) -> tuple[int, list[tuple[Check, str]]]:
    errors: list[tuple[Check, str]] = []
    warnings: list[tuple[Check, str]] = []
    for check in ALL_CHECKS:
        if env.name not in check.applies_to:
            continue
        try:
            msg = check.fn(env)
        except Exception as e:  # a check bug must not mask a real failure
            msg = f"check raised {type(e).__name__}: {e}"
        if msg is None:
            continue
        if check.severity == "error":
            errors.append((check, msg))
        else:
            warnings.append((check, msg))

    print(f"\nPreflight — env={env.name} ({len(ALL_CHECKS)} checks)")
    print("=" * 60)
    if not errors and not warnings:
        print("OK — all checks passed.")
    for check, msg in errors:
        print(f"  [FAIL] {check.name}: {msg}")
    for check, msg in warnings:
        print(f"  [WARN] {check.name}: {msg}")
    print("=" * 60)
    print(f"  {len(errors)} error(s), {len(warnings)} warning(s)")

    rc = 0
    if errors:
        rc = 1
    elif strict and warnings:
        rc = 1
    return rc, errors + warnings


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--env", choices=["prod", "dev", "auto"], default="auto",
                        help="target environment (default: auto from ENVIRONMENT env var)")
    parser.add_argument("--strict", action="store_true",
                        help="fail on warnings as well as errors")
    args = parser.parse_args(argv)

    if args.env == "auto":
        env_name = "prod" if config.IS_PROD else "dev"
    else:
        env_name = args.env

    rc, _ = run(Env(name=env_name, is_prod=(env_name == "prod")), strict=args.strict)
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
