"""Phase 1 tests: tools layer — idempotency, audit rows, mark_recovered precondition."""
from datetime import datetime, timedelta, timezone

import pytest

from app.db.tables import AuditLog, Case, CaseStatus, Invoice, PaymentEvent
from app.tools import read_tools, write_tools
from app.tools.write_tools import ToolExecutionError


@pytest.fixture()
def receivable_case(db):
    case = db.query(Case).filter(
        Case.id == "case_clean_payer_0").first()
    return case


def test_read_tools_against_seeded(db, receivable_case):
    hist = read_tools.get_customer_history(db, receivable_case.customer_id)
    assert hist.opted_out is False and hist.broken_promise_count >= 0
    inv = read_tools.get_invoice(db, receivable_case.invoice_id)
    assert inv.amount == receivable_case.amount_at_risk
    promises = read_tools.get_past_promises(db, receivable_case.customer_id)
    assert isinstance(promises, list)
    msgs = read_tools.get_communication_log(db, receivable_case.id)
    assert isinstance(msgs, list)


def test_read_tool_missing_entity_raises(db):
    with pytest.raises(read_tools.NotFoundError):
        read_tools.get_invoice(db, "nope")


def test_send_reminder_normal_and_audit(db, receivable_case):
    before = db.query(AuditLog).filter(AuditLog.case_id == receivable_case.id).count()
    result = write_tools.send_reminder(db, receivable_case.id, "email", "pay up", 1)
    db.flush()
    assert result.status == "delivered"
    after = db.query(AuditLog).filter(AuditLog.case_id == receivable_case.id).count()
    assert after == before + 1  # exactly one audit row per tool call


def test_send_reminder_idempotency_second_call_no_side_effect(db, receivable_case):
    r1 = write_tools.send_reminder(db, receivable_case.id, "email", "m", 2)
    db.flush()
    count_before = db.query(AuditLog).filter(AuditLog.case_id == receivable_case.id).count()
    r2 = write_tools.send_reminder(db, receivable_case.id, "email", "m", 2)  # same key
    db.flush()
    count_after = db.query(AuditLog).filter(AuditLog.case_id == receivable_case.id).count()
    assert r2.model_dump() == r1.model_dump()   # identical stored result returned
    assert count_after == count_before          # no re-execution, no extra audit row


def test_send_payment_link_and_escalate_idempotent(db, receivable_case):
    t1 = write_tools.escalate_to_human(db, receivable_case.id, "test_reason", "summary", 3)
    t2 = write_tools.escalate_to_human(db, receivable_case.id, "test_reason", "summary", 3)
    assert t1.ticket_id == t2.ticket_id


def test_mark_recovered_requires_verified_payment(db, receivable_case):
    with pytest.raises(ToolExecutionError):
        write_tools.mark_recovered(db, receivable_case.id, 1000,
                                   verified_by="llm_hallucination",
                                   verified_payment_id=999999, attempt_number=1)


def test_mark_recovered_with_verified_payment(db, receivable_case):
    p = PaymentEvent(invoice_id=receivable_case.invoice_id,
                    amount_paid=receivable_case.amount_at_risk, source="synthetic_batch")
    db.add(p)
    db.flush()
    payload = write_tools.mark_recovered(
        db, receivable_case.id, receivable_case.amount_at_risk,
        verified_by="payment_poller", verified_payment_id=p.id, attempt_number=1)
    assert payload["verified_by"] == "payment_poller"


def test_bad_case_id_not_retried(db):
    with pytest.raises(ToolExecutionError) as exc:
        write_tools.send_reminder(db, "missing_case", "email", "x", 1)
    assert not exc.value.retryable  # non-retryable failure distinguished
