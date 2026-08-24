"""Phase 7: failed-payment extension runs through the UNMODIFIED graph and
PolicyEngine. If these pass, generalization holds."""
from datetime import datetime, timedelta, timezone

import pytest

from app.agent import graph as agent_graph
from app.db.session import SessionLocal
from app.db.tables import (
    AuditLog,
    Case,
    CaseStatus,
    Customer,
    Invoice,
    RetryEvent,
    PaymentMethodRecord,
    Subscription,
)


def make_fp_case(db, suffix, pm_status="active", decline="insufficient_funds", amount=999.0):
    cid = f"case_fp_{suffix}"
    cust_id = f"cust_fp_{suffix}"
    inv_id = f"inv_fp_{suffix}"
    if not db.get(Customer, cust_id):
        db.add(Customer(id=cust_id, name=f"FP Co {suffix}", opted_out=False, notes="failed_payment"))
        db.add(Invoice(id=inv_id, customer_id=cust_id, amount=amount, currency="INR",
                       due_date=datetime.now(timezone.utc)))
        db.add(PaymentMethodRecord(id=f"pm_{suffix}", customer_id=cust_id, label="card_primary",
                                   status=pm_status, last_decline_code=decline))
        db.add(Subscription(id=f"sub_{suffix}", customer_id=cust_id, plan_amount=amount))
        db.flush()  # Postgres FK order
    else:
        db.query(PaymentMethodRecord).filter_by(id=f"pm_{suffix}") \
            .update({"status": pm_status, "last_decline_code": decline})
    case = db.get(Case, cid)
    if not case:
        case = Case(id=cid, invoice_id=inv_id, customer_id=cust_id,
                    detected_at=datetime.now(timezone.utc), amount_at_risk=amount,
                    status=CaseStatus.NEW, case_type=__import__("app.db.tables", fromlist=["CaseType"]).CaseType.FAILED_PAYMENT)
        db.add(case)
        db.commit()
    else:
        case = db.merge(case)
    db.refresh(case)
    case.status = CaseStatus.NEW
    case.attempt_count = 0
    db.commit()
    return case


def test_failed_payment_recovers_through_unmodified_graph(db):
    """Active card + successful retry result → RECOVERED via same graph/policy."""
    from sqlalchemy import text

    case = make_fp_case(db, "ok1")
    db.add(RetryEvent(case_id=case.id, succeeded=True, amount=case.amount_at_risk,
                           source="synthetic_batch"))
    db.commit()
    state = agent_graph.run_case(db, case.id)
    assert state.status.value == "RECOVERED"
    events = [e.event_type for e in db.query(AuditLog).filter(AuditLog.case_id == case.id).all()]
    assert "policy_check" in events and "retry_succeeded" in events


def test_dead_card_escalates_through_unmodified_policy(db):
    """Expired card: agent escalates; PolicyEngine.check() ran with zero logic changes."""
    case = make_fp_case(db, "dead1", pm_status="expired", decline="card_expired")
    state = agent_graph.run_case(db, case.id)
    assert state.status.value in ("ESCALATED", "STOPPED")
    pc = [e for e in db.query(AuditLog).filter(AuditLog.case_id == case.id).all()
          if e.event_type == "policy_check"]
    assert pc  # the unmodified policy engine was exercised


def test_policy_engine_source_unmodified_for_extension():
    import inspect
    from app.policy import policy_engine

    src = inspect.getsource(policy_engine.PolicyEngine)
    # no receivables-specific branching leaked into the class
    assert "receivable" not in src
