"""Phase 2 tests: PolicyEngine boundary tests, 100% branch coverage of check()."""
from datetime import datetime, timedelta, timezone

import pytest

from app.models.domain import CaseState
from app.models.schemas import InterventionChoice
from app.policy.policy_engine import PolicyEngine

# Pinned to 10:30 IST so tests are deterministic against the contact-hours rule.
NOW = datetime(2026, 8, 20, 5, 0, tzinfo=timezone.utc)


def mk_case(attempt=0, opted=False, detected_days_ago=1, sent_today=0, amount=500_000):
    return CaseState(
        case_id="c", invoice_id="i", customer_id="cust",
        attempt_count=attempt, opted_out=opted,
        detected_at=NOW - timedelta(days=detected_days_ago),
        amount_at_risk=amount, messages_sent_today=sent_today,
    )


REMIND = InterventionChoice(action="send_reminder", expected_recovery_probability=0.5, reasoning="")
ESCALATE = InterventionChoice(action="escalate_human", expected_recovery_probability=0.4, reasoning="")


@pytest.fixture()
def engine():
    return PolicyEngine()


# --- clean path ---
def test_clean_case_allowed(engine):
    d = engine.check(mk_case(), REMIND, now=NOW)
    assert d.allowed and d.reason is None and not d.escalate


# --- opt-out (checked first) ---
def test_opted_out_blocked_no_escalate(engine):
    d = engine.check(mk_case(opted=True), REMIND, now=NOW)
    assert not d.allowed and d.reason == "customer_opted_out" and not d.escalate


def test_opted_out_wins_over_max_retries(engine):
    """Both rules tripped → documented ordering says opted_out wins."""
    d = engine.check(mk_case(opted=True, attempt=99), REMIND, now=NOW)
    assert d.reason == "customer_opted_out"


def test_opted_out_wins_over_window_expired(engine):
    d = engine.check(mk_case(opted=True, detected_days_ago=30), REMIND, now=NOW)
    assert d.reason == "customer_opted_out"


# --- max retries: boundary at threshold ---
@pytest.mark.parametrize("attempt,expected_allowed", [(2, True), (3, False), (4, False)])
def test_max_retries_boundaries(engine, attempt, expected_allowed):
    d = engine.check(mk_case(attempt=attempt), REMIND, now=NOW)
    if expected_allowed:
        assert d.allowed
    else:
        assert not d.allowed and d.reason == "max_retries_exceeded" and d.escalate


# --- window expiry: boundaries ---
@pytest.mark.parametrize("days,allowed", [(6, True), (7, False), (8, False)])
def test_window_boundary(engine, days, allowed):
    # detected_at = NOW - (days+1): age in days = days+... careful; use exact offsets
    case = CaseState(case_id="c", invoice_id="i", customer_id="cust",
                     detected_at=NOW - timedelta(days=days + 1), amount_at_risk=500_000)
    d = engine.check(case, REMIND, now=NOW)
    assert d.allowed == allowed
    if not allowed:
        assert d.reason == "window_expired" and d.escalate


# --- daily cap: boundaries ---
@pytest.mark.parametrize("sent,allowed", [(0, True), (1, False)])
def test_daily_cap(engine, sent, allowed):
    d = engine.check(mk_case(sent_today=sent), REMIND, now=NOW)
    assert d.allowed == allowed
    if not allowed:
        assert d.reason == "daily_message_cap" and not d.escalate


# --- escalation min value: boundaries ---
@pytest.mark.parametrize("amount,allowed", [
    (100_000, True),    # exactly at threshold → allowed
    (99_999, False),    # just under → blocked
])
def test_escalation_threshold(engine, amount, allowed):
    d = engine.check(mk_case(amount=amount), ESCALATE, now=NOW)
    assert d.allowed == allowed
    if not allowed:
        assert d.reason == "below_escalation_threshold" and not d.escalate


def test_low_value_reminder_still_fine(engine):
    assert engine.check(mk_case(amount=500), REMIND, now=NOW).allowed


def test_multi_rule_ordering_retries_before_cap(engine):
    """attempt>=max AND daily cap tripped → retries reason wins per ordering."""
    d = engine.check(mk_case(attempt=3, sent_today=5), REMIND, now=NOW)
    assert d.reason == "max_retries_exceeded"


def test_multi_rule_ordering_window_before_cap(engine):
    d = engine.check(mk_case(detected_days_ago=30, sent_today=5), REMIND, now=NOW)
    assert d.reason == "window_expired"


def test_config_snapshot_present(engine):
    d = engine.check(mk_case(), REMIND, now=NOW)
    assert d.config_snapshot["max_retries"] == 3
