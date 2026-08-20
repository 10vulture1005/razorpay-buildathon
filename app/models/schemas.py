"""Pydantic I/O schemas for tools (Phase 1) and LLM structured outputs (Phase 3).
One place to look for every schema, per the phase spec."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

LikelyCause = Literal["cashflow_issue", "dispute", "forgot", "process_delay", "unwilling",
                      # Phase-7 extension values — same schema shape, wider enum
                      "card_expired", "insufficient_funds", "bank_decline", "stale_mandate"]
ActionType = Literal["send_reminder", "send_payment_link", "escalate_human", "wait", "stop"]

FailedPaymentCause = Literal["card_expired", "insufficient_funds", "bank_decline", "stale_mandate"]


class DiagnosisResult(BaseModel):
    likely_cause: LikelyCause
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str  # audit trail only — never branched on


class InterventionChoice(BaseModel):
    action: ActionType
    expected_recovery_probability: float = Field(ge=0.0, le=1.0)
    channel: Literal["email", "sms"] | None = None
    message: str | None = None
    reasoning: str  # audit trail only


# ---- Read tool outputs ----


class CustomerHistory(BaseModel):
    """Shape the Phase-3 context builder depends on. Docstring is contract:
    payment pattern + broken promises + opt-out flag are the fields diagnose /
    select_action need; nothing else is assembled."""

    customer_id: str
    name: str
    opted_out: bool
    invoices_total: int
    on_time_rate: float
    avg_days_late: float
    broken_promise_count: int


class Invoice(BaseModel):
    id: str
    customer_id: str
    amount: float
    currency: str
    due_date: datetime
    status: str


class Promise(BaseModel):
    promised_date: datetime
    kept: bool | None


class Message(BaseModel):
    direction: str
    channel: str
    body: str
    created_at: datetime


# ---- Write tool outputs ----


class DeliveryResult(BaseModel):
    status: Literal["delivered", "failed"]
    provider_message_id: str | None
    sent_at: datetime
    idempotency_key: str


class EscalationTicket(BaseModel):
    ticket_id: str
    case_id: str
    reason: str
    summary: str


class RecoveryConfirmation(BaseModel):
    case_id: str
    amount: float
    verified_by: str
    verified_payment_id: int
