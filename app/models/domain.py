"""Core domain state passed through the agent graph."""
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.db.tables import CaseStatus


class PolicyDecision(BaseModel):
    allowed: bool
    reason: str | None = None
    escalate: bool = False
    config_snapshot: dict[str, Any] = {}


class StoppingRulesDecision(BaseModel):
    exhausted: bool
    reason: str | None = None
    escalate: bool = False


class CaseState(BaseModel):
    """The graph's working state. Persisted truth lives in Postgres `cases`;
    this object is rebuilt from DB at each node boundary."""

    case_id: str
    invoice_id: str
    customer_id: str
    case_type: Literal["receivable", "failed_payment"] = "receivable"
    status: CaseStatus = CaseStatus.NEW
    attempt_count: int = 0
    messages_sent_today: int = 0
    detected_at: Any = None
    amount_at_risk: float = 0.0
    opted_out: bool = False
    last_action: str | None = None

    context: dict[str, Any] = {}
    diagnosis: dict[str, Any] | None = None
    proposed_action: dict[str, Any] | None = None
    policy_decision: dict[str, Any] | None = None
    outcome: dict[str, Any] | None = None
    stopping_decision: dict[str, Any] | None = None
    iterations: int = 0
    terminal_reason: str | None = None


class RecoveryMetrics(BaseModel):
    revenue_at_risk: float
    recovered_amount: float
    recovery_rate: float
    active_cases: int
    total_cases: int
    escalated_cases: int
    stopped_cases: int
    recovered_cases: int
    automation_rate: float
    escalation_rate: float
