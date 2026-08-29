import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{ROOT}/.pytest_recovery.db")
# Tests are hermetic: always the deterministic mock provider, never the real LLM —
# even when a developer's .env configures openrouter.
os.environ.setdefault("LLM_PROVIDER", "mock")
# Test credentials (P0): admin key covers all scopes; webhook secret for HMAC tests.
os.environ.setdefault("ENVIRONMENT", "dev")
os.environ.setdefault(
    "API_KEYS",
    "test-admin-key-1234567890ab:admin,test-run-key-1234567890ab:run,read,test-read-key-1234567890ab:read",
)
# Hard-set (not setdefault) and pin BOTH webhook-secret names: config prefers
# RAZORPAY_WEBHOOK_SECRET, and a developer's .env must never leak into tests.
os.environ["PAYMENT_WEBHOOK_SECRET"] = "test-webhook-secret-1234567890ab"
os.environ["RAZORPAY_WEBHOOK_SECRET"] = "test-webhook-secret-1234567890ab"
# Hermetic providers: a dev .env with PAYMENT_PROVIDER=razorpay / real email
# credentials would otherwise make graph tests call live APIs.
os.environ["PAYMENT_PROVIDER"] = "console"
os.environ["EMAIL_PROVIDER"] = "console"
for _k in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET"):
    os.environ.pop(_k, None)
os.environ.setdefault("ALLOW_MOCK_ADAPTERS", "true")

TEST_ADMIN_KEY = "test-admin-key-1234567890ab"
TEST_RUN_KEY = "test-run-key-1234567890ab"
TEST_READ_KEY = "test-read-key-1234567890ab"
ADMIN_HEADERS = {"X-API-Key": TEST_ADMIN_KEY}

import pytest

from app.db.session import SessionLocal, init_db, engine
from app.db.tables import Base


@pytest.fixture(scope="session", autouse=True)
def pinned_policy_clock():
    """Pin the PolicyEngine clock to 10:30 IST for every suite that exercises the
    live graph, so contact-hours enforcement never makes results time-of-day
    dependent. Dedicated contact-hours tests pass `now=` explicitly instead."""
    from datetime import datetime, timezone

    from app.policy import policy_engine

    original = policy_engine._default_now
    policy_engine._default_now = lambda: datetime(2026, 8, 20, 5, 0, tzinfo=timezone.utc)
    yield
    policy_engine._default_now = original


@pytest.fixture(scope="session", autouse=True)
def seeded_db(pinned_policy_clock):
    Base.metadata.drop_all(engine)
    init_db()
    from scripts.seed_synthetic_data import seed
    seed()
    yield


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
        s.rollback()
    finally:
        s.close()


@pytest.fixture(autouse=True)
def _reset_case_states(seeded_db):
    """Reset cases to NEW before each test so runs are repeatable."""
    yield
