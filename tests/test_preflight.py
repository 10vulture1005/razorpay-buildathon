"""Tests for the production preflight script and the auth parser fix.

Covers the bugs that motivated the new preflight + the regex-based
`API_KEYS` parser in `app.security.auth`:

- `API_KEYS` entries with multiple scopes (`key:run,read`) used to
  silently drop the `read` scope (the old parser split on `,` first).
- The preflight catches every prod-misconfig we know about.
"""
import os
import importlib
import pytest

import app.config as config
import app.security.auth as auth
import scripts.preflight as preflight_mod


# ---- auth parser regression -----------------------------------------------

class TestApiKeysParser:
    def test_single_entry_single_scope(self):
        recs = auth._parse_keys("abc1234567890123:admin")
        assert len(recs) == 1
        assert recs[0].scopes == frozenset({"admin"})

    def test_single_entry_multi_scope(self):
        recs = auth._parse_keys("abc1234567890123:run,read")
        assert len(recs) == 1
        assert recs[0].scopes == frozenset({"run", "read"})

    def test_multi_entry_with_multi_scope(self):
        recs = auth._parse_keys(
            "admin-key-1234567890:admin,run-key-1234567890:run,read,read-key-1234567890:read"
        )
        assert len(recs) == 3
        scopes = sorted(recs[0].scopes | recs[1].scopes | recs[2].scopes)
        assert scopes == ["admin", "read", "run"]
        assert "admin" in recs[0].scopes
        assert "run" in recs[1].scopes
        assert "read" in recs[1].scopes
        assert recs[2].scopes == frozenset({"read"})

    def test_whitespace_tolerated(self):
        recs = auth._parse_keys(" abc1234567890123:admin , def1234567890123:run ")
        assert len(recs) == 2
        assert recs[0].scopes == frozenset({"admin"})
        assert recs[1].scopes == frozenset({"run"})

    def test_malformed_entry_dropped(self):
        recs = auth._parse_keys("abc1234567890123:admin,bare-no-colon,xyz1234567890123:read")
        # `bare-no-colon` is a stray token (no `:`), so the parser drops the
        # whole entry it was attached to, leaving only the `xyz:read` entry.
        # The single surviving record must be the second one, NOT the admin
        # one — this proves the stray token was not silently absorbed as a
        # scope name.
        assert len(recs) == 1
        assert recs[0].scopes == frozenset({"read"})

    def test_unknown_scope_dropped_entirely(self):
        # A token after the `:` that doesn't match the scope regex is treated
        # as a malformed tail and the whole entry is dropped, not silently
        # accepted with a wrong scope set.
        recs = auth._parse_keys("abc1234567890123:admin,evil-scope-xyz")
        assert recs == []


# ---- preflight checks (unit-level) --------------------------------------

@pytest.fixture(autouse=True)
def _restore_modules_after_test(monkeypatch):
    """Reload `app.config` and friends back to the post-conftest baseline
    after every preflight test, so subsequent tests in the suite see the
    same module state regardless of what env mutations happened here.

    The preflight tests deliberately reload these modules to pick up new
    env values. If we don't reload them back, the next test in the suite
    sees the preflight test's last config snapshot — which is why this
    file MUST be marked as having side effects on the module cache.
    """
    yield
    # Reload to the current (post-monkeypatch-revert) env. By the time
    # this teardown runs, monkeypatch has already put the original values
    # back, so the reload restores the baseline.
    importlib.reload(config)
    importlib.reload(auth)
    importlib.reload(preflight_mod)


def _reload_config_with(monkeypatch, **env_overrides):
    """Mutate the process env and reload all modules that snapshot from it.

    `app.config` reads at import time. The preflight module imports `config`
    once at the top of the file, so its check functions reach into a
    snapshot. We must reload it too — and the preflight module itself —
    so the check registry is rebuilt against the new config.
    """
    for k, v in env_overrides.items():
        monkeypatch.setenv(k, v)
    importlib.reload(config)
    importlib.reload(auth)
    importlib.reload(preflight_mod)


class TestPreflightProdRules:

    def test_clean_prod_config_passes(self, monkeypatch):
        _reload_config_with(monkeypatch,
            DATABASE_URL="postgresql+psycopg2://u:p@db.example.com:5432/recovery",
            LLM_PROVIDER="openrouter",
            OPENROUTER_API_KEY="sk-or-abc",
            MODEL_FRONTIER="minimax/minimax-m3:free",
            MODEL_SMALL="minimax/minimax-m3:free",
            EMAIL_PROVIDER="mailgun",
            EMAIL_FROM="recovery@example.com",
            MAILGUN_API_KEY="key-abc",
            MAILGUN_DOMAIN="mg.example.com",
            MAILGUN_WEBHOOK_SIGNING_KEY="key-abc",
            PAYMENT_PROVIDER="razorpay",
            RAZORPAY_KEY_ID="rzp_live_abc",
            RAZORPAY_KEY_SECRET="secret-1234567890abcdef",
            RAZORPAY_WEBHOOK_SECRET="webhook-secret-1234567890",
            PAYMENT_WEBHOOK_SECRET="webhook-secret-1234567890",
            API_KEYS="admin-key-1234567890abcdef:admin,run-key-1234567890abcdef:run,read,read-key-1234567890abcdef:read",
            CORS_ORIGINS="https://dashboard.example.com",
            ALLOW_MOCK_ADAPTERS="false",
            WRITE_TOOLS_ENABLED="true",
        )
        rc, errors = preflight_mod.run(preflight_mod.Env("prod", True))
        assert rc == 0, f"expected pass, got errors: {[e[1] for e in errors]}"

    def test_sqlite_in_prod_caught(self, monkeypatch):
        _reload_config_with(monkeypatch,
            DATABASE_URL="sqlite:///./dev.db",
            LLM_PROVIDER="openrouter",
            OPENROUTER_API_KEY="sk-or-abc",
            MODEL_FRONTIER="minimax/minimax-m3:free",
            EMAIL_PROVIDER="mailgun",
            EMAIL_FROM="recovery@example.com",
            MAILGUN_API_KEY="key", MAILGUN_DOMAIN="mg.example.com", MAILGUN_WEBHOOK_SIGNING_KEY="key",
            PAYMENT_PROVIDER="razorpay", RAZORPAY_KEY_ID="rzp_live_abc",
            RAZORPAY_KEY_SECRET="secret-1234567890abcdef",
            RAZORPAY_WEBHOOK_SECRET="webhook-secret-1234567890",
            PAYMENT_WEBHOOK_SECRET="webhook-secret-1234567890",
            API_KEYS="admin-key-1234567890abcdef:admin",
            CORS_ORIGINS="https://dashboard.example.com",
            ALLOW_MOCK_ADAPTERS="false",
        )
        rc, errors = preflight_mod.run(preflight_mod.Env("prod", True))
        assert rc == 1
        assert any("SQLite" in e for _c, e in errors)

    def test_localhost_db_in_prod_caught(self, monkeypatch):
        _reload_config_with(monkeypatch,
            DATABASE_URL="postgresql+psycopg2://u:p@127.0.0.1:5432/recovery",
            LLM_PROVIDER="openrouter", OPENROUTER_API_KEY="sk-or-abc",
            MODEL_FRONTIER="minimax/minimax-m3:free",
            EMAIL_PROVIDER="mailgun", EMAIL_FROM="recovery@example.com",
            MAILGUN_API_KEY="key", MAILGUN_DOMAIN="mg.example.com", MAILGUN_WEBHOOK_SIGNING_KEY="key",
            PAYMENT_PROVIDER="razorpay", RAZORPAY_KEY_ID="rzp_live_abc",
            RAZORPAY_KEY_SECRET="secret-1234567890abcdef",
            RAZORPAY_WEBHOOK_SECRET="webhook-secret-1234567890",
            PAYMENT_WEBHOOK_SECRET="webhook-secret-1234567890",
            API_KEYS="admin-key-1234567890abcdef:admin",
            CORS_ORIGINS="https://dashboard.example.com",
            ALLOW_MOCK_ADAPTERS="false",
        )
        rc, errors = preflight_mod.run(preflight_mod.Env("prod", True))
        assert rc == 1
        assert any("localhost" in e for _c, e in errors)

    def test_mock_llm_in_prod_caught(self, monkeypatch):
        _reload_config_with(monkeypatch,
            DATABASE_URL="postgresql+psycopg2://u:p@db.example.com/recovery",
            LLM_PROVIDER="mock",  # <-- the mistake
            OPENROUTER_API_KEY="sk-or-abc",
            MODEL_FRONTIER="minimax/minimax-m3:free",
            EMAIL_PROVIDER="mailgun", EMAIL_FROM="recovery@example.com",
            MAILGUN_API_KEY="key", MAILGUN_DOMAIN="mg.example.com", MAILGUN_WEBHOOK_SIGNING_KEY="key",
            PAYMENT_PROVIDER="razorpay", RAZORPAY_KEY_ID="rzp_live_abc",
            RAZORPAY_KEY_SECRET="secret-1234567890abcdef",
            RAZORPAY_WEBHOOK_SECRET="webhook-secret-1234567890",
            PAYMENT_WEBHOOK_SECRET="webhook-secret-1234567890",
            API_KEYS="admin-key-1234567890abcdef:admin",
            CORS_ORIGINS="https://dashboard.example.com",
            ALLOW_MOCK_ADAPTERS="false",
        )
        rc, errors = preflight_mod.run(preflight_mod.Env("prod", True))
        assert rc == 1
        assert any("LLM" in e for _c, e in errors)

    def test_short_api_key_caught(self, monkeypatch):
        _reload_config_with(monkeypatch,
            DATABASE_URL="postgresql+psycopg2://u:p@db.example.com/recovery",
            LLM_PROVIDER="openrouter", OPENROUTER_API_KEY="sk-or-abc",
            MODEL_FRONTIER="minimax/minimax-m3:free",
            EMAIL_PROVIDER="mailgun", EMAIL_FROM="recovery@example.com",
            MAILGUN_API_KEY="key", MAILGUN_DOMAIN="mg.example.com", MAILGUN_WEBHOOK_SIGNING_KEY="key",
            PAYMENT_PROVIDER="razorpay", RAZORPAY_KEY_ID="rzp_live_abc",
            RAZORPAY_KEY_SECRET="secret-1234567890abcdef",
            RAZORPAY_WEBHOOK_SECRET="webhook-secret-1234567890",
            PAYMENT_WEBHOOK_SECRET="webhook-secret-1234567890",
            API_KEYS="abc:admin",  # <-- 3 chars
            CORS_ORIGINS="https://dashboard.example.com",
            ALLOW_MOCK_ADAPTERS="false",
        )
        rc, errors = preflight_mod.run(preflight_mod.Env("prod", True))
        assert rc == 1
        assert any("API_KEY" in e and "short" in e for _c, e in errors)

    def test_dev_placeholder_in_prod_caught(self, monkeypatch):
        _reload_config_with(monkeypatch,
            DATABASE_URL="postgresql+psycopg2://u:p@db.example.com/recovery",
            LLM_PROVIDER="openrouter", OPENROUTER_API_KEY="sk-or-abc",
            MODEL_FRONTIER="minimax/minimax-m3:free",
            EMAIL_PROVIDER="mailgun", EMAIL_FROM="recovery@example.com",
            MAILGUN_API_KEY="key", MAILGUN_DOMAIN="mg.example.com", MAILGUN_WEBHOOK_SIGNING_KEY="key",
            PAYMENT_PROVIDER="razorpay", RAZORPAY_KEY_ID="rzp_live_abc",
            RAZORPAY_KEY_SECRET="secret-1234567890abcdef",
            RAZORPAY_WEBHOOK_SECRET="webhook-secret-1234567890",
            PAYMENT_WEBHOOK_SECRET="webhook-secret-1234567890",
            API_KEYS="dev-admin-key:admin",
            CORS_ORIGINS="https://dashboard.example.com",
            ALLOW_MOCK_ADAPTERS="false",
        )
        rc, errors = preflight_mod.run(preflight_mod.Env("prod", True))
        assert rc == 1
        assert any("placeholder" in e for _c, e in errors)

    def test_cors_wildcard_caught(self, monkeypatch):
        _reload_config_with(monkeypatch,
            DATABASE_URL="postgresql+psycopg2://u:p@db.example.com/recovery",
            LLM_PROVIDER="openrouter", OPENROUTER_API_KEY="sk-or-abc",
            MODEL_FRONTIER="minimax/minimax-m3:free",
            EMAIL_PROVIDER="mailgun", EMAIL_FROM="recovery@example.com",
            MAILGUN_API_KEY="key", MAILGUN_DOMAIN="mg.example.com", MAILGUN_WEBHOOK_SIGNING_KEY="key",
            PAYMENT_PROVIDER="razorpay", RAZORPAY_KEY_ID="rzp_live_abc",
            RAZORPAY_KEY_SECRET="secret-1234567890abcdef",
            RAZORPAY_WEBHOOK_SECRET="webhook-secret-1234567890",
            PAYMENT_WEBHOOK_SECRET="webhook-secret-1234567890",
            API_KEYS="admin-key-1234567890abcdef:admin",
            CORS_ORIGINS="*",  # <-- the mistake
            ALLOW_MOCK_ADAPTERS="false",
        )
        rc, errors = preflight_mod.run(preflight_mod.Env("prod", True))
        assert rc == 1
        assert any("CORS" in e for _c, e in errors)

    def test_mailgun_without_key_caught(self, monkeypatch):
        _reload_config_with(monkeypatch,
            DATABASE_URL="postgresql+psycopg2://u:p@db.example.com/recovery",
            LLM_PROVIDER="openrouter", OPENROUTER_API_KEY="sk-or-abc",
            MODEL_FRONTIER="minimax/minimax-m3:free",
            EMAIL_PROVIDER="mailgun", EMAIL_FROM="recovery@example.com",
            MAILGUN_API_KEY="", MAILGUN_DOMAIN="mg.example.com", MAILGUN_WEBHOOK_SIGNING_KEY="key",
            PAYMENT_PROVIDER="razorpay", RAZORPAY_KEY_ID="rzp_live_abc",
            RAZORPAY_KEY_SECRET="secret-1234567890abcdef",
            RAZORPAY_WEBHOOK_SECRET="webhook-secret-1234567890",
            PAYMENT_WEBHOOK_SECRET="webhook-secret-1234567890",
            API_KEYS="admin-key-1234567890abcdef:admin",
            CORS_ORIGINS="https://dashboard.example.com",
            ALLOW_MOCK_ADAPTERS="false",
        )
        rc, errors = preflight_mod.run(preflight_mod.Env("prod", True))
        assert rc == 1
        assert any("MAILGUN" in e for _c, e in errors)

    def test_console_email_in_prod_caught(self, monkeypatch):
        _reload_config_with(monkeypatch,
            DATABASE_URL="postgresql+psycopg2://u:p@db.example.com/recovery",
            LLM_PROVIDER="openrouter", OPENROUTER_API_KEY="sk-or-abc",
            MODEL_FRONTIER="minimax/minimax-m3:free",
            EMAIL_PROVIDER="console",  # <-- the mistake
            EMAIL_FROM="recovery@example.com",
            PAYMENT_PROVIDER="razorpay", RAZORPAY_KEY_ID="rzp_live_abc",
            RAZORPAY_KEY_SECRET="secret-1234567890abcdef",
            RAZORPAY_WEBHOOK_SECRET="webhook-secret-1234567890",
            PAYMENT_WEBHOOK_SECRET="webhook-secret-1234567890",
            API_KEYS="admin-key-1234567890abcdef:admin",
            CORS_ORIGINS="https://dashboard.example.com",
            ALLOW_MOCK_ADAPTERS="false",
        )
        rc, errors = preflight_mod.run(preflight_mod.Env("prod", True))
        assert rc == 1
        assert any("console" in e for _c, e in errors)

    def test_all_checks_have_unique_names(self):
        names = [c.name for c in preflight_mod.ALL_CHECKS]
        assert len(names) == len(set(names)), "duplicate check name"
