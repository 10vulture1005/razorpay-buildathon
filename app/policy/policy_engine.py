"""PolicyEngine — fully deterministic, zero LLM calls. The model proposes; this decides.

Rule ordering (documented, deliberate):
1. opted_out          — short-circuits everything; nothing matters after opt-out.
                        escalate=False (silently stop; no human paging needed).
2. max_retries        — attempt budget exhausted; a human must take over → escalate=True.
3. window_expired     — too old to chase automatically; needs human judgment → escalate=True.
4. daily_message_cap  — transient throttle; wait until tomorrow → escalate=False.
5. escalation_min_value — low-value cases don't justify human time → escalate=False.

Multi-rule violations return the FIRST matching rule's reason in the order above.

Audit logging happens at the CALL SITE (graph node), exactly once — NOT inside
check(), so standalone unit tests stay pure and no duplicate rows occur.
"""
import yaml
from datetime import datetime, timezone

from pydantic import BaseModel

import app.config as config
from app.models.domain import PolicyDecision
from app.models.schemas import InterventionChoice

DEFAULT_CONFIG = {
    "version": "unset",
    "max_retries": 3,
    "max_recovery_window_days": 7,
    "max_messages_per_day": 1,
    "escalation_min_value": 100000,
    # India fair-practice norms (P1-7): no outbound collection outreach
    # before 08:00 or after 19:00 IST. Internal actions (escalate/stop/wait)
    # are exempt — the window gates customer-facing sends only.
    "contact_hours_start": 8,
    "contact_hours_end": 19,
    "enforce_contact_hours": True,
}

OUTBOUND_MESSAGE_ACTIONS = {"send_reminder", "send_payment_link", "retry_payment",
                            "update_payment_method_prompt", "send_dunning_email"}

ACTION_COSTS = {  # intervention cost table (INR), used for net-expected-value math
    "send_reminder": 5,
    "send_payment_link": 10,
    "escalate_human": 500,
    "wait": 0,
    "stop": 0,
}


def load_policy_config(path=None) -> dict:
    p = str(path or config.POLICY_CONFIG_PATH)
    try:
        with open(p) as f:
            loaded = yaml.safe_load(f) or {}
    except FileNotFoundError:
        loaded = {}
    merged = dict(DEFAULT_CONFIG)
    merged.update(loaded)
    return merged


def _ist_time(now: datetime) -> datetime:
    """Convert a UTC instant to IST (UTC+5:30) without a tzdata dependency."""
    from datetime import timedelta

    utc = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return (utc + timedelta(hours=5, minutes=30)).replace(tzinfo=None)


def _default_now() -> datetime:
    """Injectable clock seam. Tests pin this (see conftest) so suites are
    deterministic against the contact-hours window."""
    return datetime.now(timezone.utc)


class PolicyEngine:
    def __init__(self, config_path=None):
        self.config = load_policy_config(config_path)

    def check(
        self,
        case,  # app.models.domain.CaseState
        proposed_action: InterventionChoice,
        detected_at: datetime | None = None,
        now: datetime | None = None,
    ) -> PolicyDecision:
        cfg = self.config
        snapshot = dict(cfg)
        now = now or _default_now()
        detected_at = detected_at or case.detected_at

        if getattr(case, "opted_out", False):
            return PolicyDecision(allowed=False, reason="customer_opted_out", escalate=False, config_snapshot=snapshot)
        if cfg.get("enforce_contact_hours", True) and proposed_action.action in OUTBOUND_MESSAGE_ACTIONS:
            ist_now = _ist_time(now)
            start, end = int(cfg["contact_hours_start"]), int(cfg["contact_hours_end"])
            if not (start <= ist_now.hour < end):
                return PolicyDecision(
                    allowed=False, reason="outside_contact_hours", escalate=False, config_snapshot=snapshot
                )
        if case.attempt_count >= cfg["max_retries"]:
            return PolicyDecision(allowed=False, reason="max_retries_exceeded", escalate=True, config_snapshot=snapshot)
        if detected_at is not None:
            dt = detected_at if detected_at.tzinfo else detected_at.replace(tzinfo=timezone.utc)
            if (now - dt).days > cfg["max_recovery_window_days"]:
                return PolicyDecision(allowed=False, reason="window_expired", escalate=True, config_snapshot=snapshot)
        if case.messages_sent_today >= cfg["max_messages_per_day"]:
            return PolicyDecision(allowed=False, reason="daily_message_cap", escalate=False, config_snapshot=snapshot)
        if (
            proposed_action.action == "escalate_human"
            and case.amount_at_risk < cfg["escalation_min_value"]
        ):
            return PolicyDecision(
                allowed=False, reason="below_escalation_threshold", escalate=False, config_snapshot=snapshot
            )
        return PolicyDecision(allowed=True, config_snapshot=snapshot)
