"""Phases 3-4 tests: graph end-to-end, retry→escalate path, recovery path, stopping rules."""
from datetime import datetime, timezone

import pytest

from app.agent import graph as agent_graph
from app.agent.nodes import nodes
from app.db.tables import AuditLog, Case, CaseStatus, PaymentEvent, RetryEvent
from app.models.schemas import InterventionChoice
from app.policy.policy_engine import PolicyEngine


def reset_case(db, case_id, status="NEW"):
    c = db.get(Case, case_id)
    c.status = CaseStatus.NEW
    c.attempt_count = 0
    db.commit()


def audit_types(db, case_id):
    return [e.event_type for e in db.query(AuditLog).filter(AuditLog.case_id == case_id)
            .order_by(AuditLog.created_at, AuditLog.id).all()]


def test_full_retry_then_escalate_path(db):
    """No payment ever arrives → bounded loop → clean ESCALATED, full audit trail."""
    cid = "case_high_value_low_risk_0"
    # make sure no mock payments exist for its invoice
    inv = db.get(Case, cid).invoice_id
    db.query(PaymentEvent).filter(PaymentEvent.invoice_id == inv).delete()
    db.query(RetryEvent).filter(RetryEvent.case_id == cid).delete()
    reset_case(db, cid)

    state = agent_graph.run_case(db, cid)
    assert state.status.value == "ESCALATED"
    assert state.attempt_count >= PolicyEngine().config["max_retries"]
    events = audit_types(db, cid)
    assert "diagnosis" in events and "action_selected" in events and "policy_check" in events
    assert "stopping_rules_check" in events or "loop_bound_hit" in events


def test_recovery_path_via_poller(db):
    """Clean payer with a verified payment → RECOVERED."""
    from app.db.tables import Invoice

    cid = "case_clean_payer_1"
    case = db.get(Case, cid)
    reset_case(db, cid)
    db.add(PaymentEvent(invoice_id=case.invoice_id, amount_paid=case.amount_at_risk,
                       source="synthetic_batch"))
    db.commit()
    state = agent_graph.run_case(db, cid)
    assert state.status.value == "RECOVERED"
    events = audit_types(db, cid)
    assert "mark_recovered" in events and "payment_detected" in events


def test_opted_out_customer_zero_actions(db):
    """Opted-out customer: first policy_check rejects; zero agent write actions ever."""
    cid = "case_opted_out_0"
    reset_case(db, cid)
    state = agent_graph.run_case(db, cid)
    assert state.status.value == "STOPPED"
    assert state.terminal_reason == "customer_opted_out"
    for e in db.query(AuditLog).filter(AuditLog.case_id == cid).all():
        if e.actor == "agent":
            assert not (e.event_type.startswith("send_") or e.event_type.startswith("retry_")
                        or e.event_type.startswith("record_") or e.event_type.startswith("mark_"))


def test_policy_rejection_audited_with_config_snapshot(db):
    cid = "case_opted_out_1"
    reset_case(db, cid)
    agent_graph.run_case(db, cid)
    pc = [e for e in db.query(AuditLog).filter(AuditLog.case_id == cid).all()
          if e.event_type == "policy_check"][0]
    assert pc.actor == "policy"
    assert pc.payload["allowed"] is False
    assert "max_retries" in pc.payload["config_snapshot"]


def test_context_window_discipline(db):
    """build_context assembles ONLY the documented field list — never the audit log."""
    cid = "case_clean_payer_2"
    reset_case(db, cid)
    state = nodes.load_case_state(db, cid)
    state = nodes.build_context(db, state)
    expected = {"case_type", "amount_at_risk", "days_overdue", "attempt_number",
                "on_time_rate", "avg_days_late", "broken_promise_count", "opted_out",
                "invoice_due_date", "promises_open", "messages"}
    assert set(state.context.keys()) == expected
    assert len(state.context["messages"]) <= 3  # last-3 discipline
    assert "audit" not in json.dumps(state.context) if (json := __import__("json")) else True


def test_net_expected_value_computed_in_code():
    from app.agent.nodes.nodes import net_expected_value
    # 500000 * 0.6 - reminder cost 5 = 299995
    assert net_expected_value("send_reminder", 0.6, 500_000) == 299995.0
    assert net_expected_value("wait", 0.9, 999_999) == 0.0  # wait/stop carry no NEV


def test_structured_output_failure_routes_to_escalated(db, monkeypatch):
    from app.agent import llm as llm_mod

    def bad_call(schema, prompt):
        raise llm_mod.StructuredOutputFailure("simulated malformed output twice")

    cid = "case_low_value_high_risk_0"
    reset_case(db, cid)
    monkeypatch.setattr(nodes.llm_mod, "call_structured", bad_call)
    state = agent_graph.run_case(db, cid)
    assert state.status.value == "ESCALATED"
    assert state.terminal_reason == "structured_output_failure"


def test_stopping_rules_deterministic_no_llm_import():
    import app.agent.nodes.nodes as n
    import inspect
    src = inspect.getsource(n.check_stopping_rules)
    assert "llm" not in src.lower().replace("llm calls", "")
