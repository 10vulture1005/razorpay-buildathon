"""Copilot email-draft flow: /chat may return an operator-editable draft;
POST /chat/send-email is the only way it leaves the chat — explicit human
confirmation, audited with actor='human'. The copilot itself never sends."""
import pytest
from fastapi.testclient import TestClient

from app.api.routes_chat import ChatReply, EmailDraft, _coerce_email_draft
from app.db.tables import AuditLog, Case, CaseStatus, Customer, Invoice
from datetime import datetime, timedelta, timezone


@pytest.fixture(scope="module")
def client():
    from tests.conftest import ADMIN_HEADERS
    from app.main import app

    return TestClient(app, headers=ADMIN_HEADERS)


@pytest.fixture()
def fresh_case(db):
    cid = "case_copilot_mail"
    if not db.get(Case, cid):
        cust = Customer(id="cust_copilot_mail", name="Copilot Co", opted_out=False)
        inv = Invoice(id="inv_copilot_mail", customer_id=cust.id, amount=50_000.0,
                      currency="INR",
                      due_date=datetime.now(timezone.utc) - timedelta(days=30))
        db.add_all([cust, inv])
        db.flush()
        db.add(Case(id=cid, invoice_id=inv.id, customer_id=cust.id,
                    detected_at=datetime.now(timezone.utc), amount_at_risk=50_000.0,
                    status=CaseStatus.NEW))
        db.commit()
    return cid


def test_email_draft_rejects_garbage_recipient():
    assert _coerce_email_draft({"to": "not-an-address", "subject": "s", "body": "b"}) is None
    assert _coerce_email_draft({"to": "a b@c.com", "subject": "s", "body": "b"}) is None
    assert _coerce_email_draft(None) is None
    d = _coerce_email_draft({"to": " a@b.com ", "subject": " s ", "body": "hello"})
    assert d is not None and d.to == "a@b.com"


def test_chat_reply_repairs_malformed_draft_instead_of_failing():
    # Model emitted a draft with a bad address: reply survives, draft drops.
    reply = ChatReply.model_validate({
        "answer": "Here is the draft.",
        "email_draft": {"to": "garbage", "subject": "", "body": ""},
    })
    assert reply.email_draft is None

    # Bare-draft wrapper (model skipped the answer field entirely).
    reply = ChatReply.model_validate({
        "to": "ops@example.com", "subject": "Payment overdue", "body": "Please pay ₹50,000.",
    })
    assert reply.answer and reply.email_draft.to == "ops@example.com"


def test_send_email_requires_run_scope(client):
    from tests.conftest import TEST_READ_KEY

    r = client.post("/chat/send-email", headers={"X-API-Key": TEST_READ_KEY},
                    json={"to": "x@y.com", "subject": "s", "body": "b"})
    assert r.status_code == 403


def test_send_email_rejects_invalid_payload(client):
    r = client.post("/chat/send-email",
                    json={"to": "nope", "subject": "s", "body": "b"})
    assert r.status_code == 422
    r = client.post("/chat/send-email",
                    json={"to": "x@y.com", "subject": "", "body": "b"})
    assert r.status_code == 422


def test_send_email_unknown_case_404(client):
    r = client.post("/chat/send-email",
                    json={"to": "x@y.com", "subject": "s", "body": "b",
                          "case_id": "case_does_not_exist"})
    assert r.status_code == 404


def test_send_email_sends_and_audits_as_human(client, db, fresh_case):
    from tests.conftest import TEST_ADMIN_KEY

    r = client.post("/chat/send-email",
                    headers={"X-API-Key": TEST_ADMIN_KEY},
                    json={"to": "debtor@example.com",
                          "subject": "Overdue invoice INV-1",
                          "body": "Friendly nudge: your invoice of ₹50,000 is overdue.",
                          "case_id": fresh_case})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "sent"
    assert data["provider"] == "console"  # hermetic test provider
    row = (db.query(AuditLog)
           .filter(AuditLog.event_type == "copilot_email_sent",
                   AuditLog.case_id == fresh_case)
           .order_by(AuditLog.id.desc()).first())
    assert row is not None
    assert row.actor == "human"
    assert row.payload["to"] == "debtor@example.com"
