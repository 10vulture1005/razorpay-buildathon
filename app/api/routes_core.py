import hmac
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agent import graph as agent_graph
import app.config as config
from app.db.session import get_session
from app.db.tables import (
    AuditLog,
    Case,
    CaseStatus,
    Customer,
    Invoice,
    PaymentEvent,
    PaymentMethodRecord,
    PolicyDecisionRecord,
    Subscription,
)
from app.models.domain import RecoveryMetrics
from app.security.auth import require_prod_disabled, require_scope

router = APIRouter()


@router.get("/health")
def health():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


@router.get("/readyz")
def readyz(db: Session = Depends(get_session)):
    """Readiness: verifies DB connectivity + schema presence."""
    try:
        from sqlalchemy import inspect

        has_schema = inspect(db.connection()).has_table("alembic_version") or \
            inspect(db.connection()).has_table("cases")
        if not has_schema:
            raise RuntimeError("schema missing")
    except Exception:
        raise HTTPException(503, "Database not ready")
    return {"status": "ready", "time": datetime.now(timezone.utc).isoformat()}


class InvoiceOverdueEvent(BaseModel):
    invoice_id: str = Field(max_length=64)
    customer_id: str = Field(max_length=64)
    amount: float = Field(gt=0)
    customer_email: str | None = Field(default=None, max_length=255)


@router.post("/events/invoice-overdue", dependencies=[Depends(require_scope("run"))])
def invoice_overdue(event: InvoiceOverdueEvent, db: Session = Depends(get_session)):
    existing = db.query(Case).filter(Case.invoice_id == event.invoice_id,
                                     ~Case.status.in_([CaseStatus.RECOVERED, CaseStatus.STOPPED])).first()
    if existing:
        return {"case_id": existing.id, "duplicate": True}
    case_id = f"case_{event.invoice_id}"
    if db.get(Case, case_id):
        return {"case_id": case_id, "duplicate": True}
    from app.db.tables import CaseType

    cust = db.get(Customer, event.customer_id)
    if not cust:
        cust = Customer(id=event.customer_id, name=f"Auto-registered {event.customer_id}",
                        opted_out=False, notes="auto_registered", email=event.customer_email)
        db.add(cust)
    elif event.customer_email and not cust.email:
        cust.email = event.customer_email
    if not db.get(Invoice, event.invoice_id):
        db.add(Invoice(id=event.invoice_id, customer_id=event.customer_id, amount=event.amount,
                       currency="INR", due_date=datetime.now(timezone.utc)))
    case = Case(id=case_id, invoice_id=event.invoice_id, customer_id=event.customer_id,
                case_type=CaseType.RECEIVABLE, status=CaseStatus.NEW,
                detected_at=datetime.now(timezone.utc), amount_at_risk=event.amount)
    db.add(case)
    db.add(AuditLog(case_id=case_id, event_type="ingest_case", actor="system",
                    payload={"source": "invoice-overdue event"}))
    db.commit()
    return {"case_id": case_id, "duplicate": False}


class PaymentFailedEvent(BaseModel):
    """Internal event from billing systems when a recurring charge fails.
    Smaller scope than a gateway failure webhook: our own systems call this."""
    customer_id: str = Field(max_length=64)
    amount: float = Field(gt=0)
    subscription_id: str | None = Field(default=None, max_length=64)
    decline_code: str | None = Field(default=None, max_length=64)


@router.post("/events/payment-failed", dependencies=[Depends(require_scope("run"))])
def payment_failed(event: PaymentFailedEvent, db: Session = Depends(get_session)):
    """Phase 7a entrypoint: creates a FAILED_PAYMENT case handed to the same graph,
    policy engine, and write tools as receivables. No new agent logic."""
    from app.db.tables import CaseType

    existing = (
        db.query(Case)
        .filter(Case.customer_id == event.customer_id,
                Case.case_type == CaseType.FAILED_PAYMENT,
                ~Case.status.in_([CaseStatus.RECOVERED, CaseStatus.STOPPED]))
        .first()
    )
    if existing:
        return {"case_id": existing.id, "duplicate": True}

    # Deterministic base id; suffix only on collision (repeat failures after a
    # prior case reached a terminal state must open fresh cases).
    case_id = f"case_fp_{event.customer_id}"
    suffix = 1
    while db.get(Case, case_id):
        case_id = f"case_fp_{event.customer_id}_{suffix}"
        suffix += 1

    inv_id = f"inv_fp_{event.customer_id}"
    if not db.get(Invoice, inv_id):
        db.add(Invoice(id=inv_id, customer_id=event.customer_id, amount=event.amount,
                       currency="INR", due_date=datetime.now(timezone.utc)))

    cust = db.get(Customer, event.customer_id)
    if not cust:
        cust = Customer(id=event.customer_id, name=f"Auto-registered {event.customer_id}",
                        opted_out=False, notes="failed_payment")
        db.add(cust)

    if event.subscription_id and not db.get(Subscription, event.subscription_id):
        db.add(Subscription(id=event.subscription_id, customer_id=event.customer_id,
                            plan_amount=event.amount))
    pm = db.query(PaymentMethodRecord).filter(
        PaymentMethodRecord.customer_id == event.customer_id).first()
    if pm:
        pm.last_decline_code = event.decline_code or pm.last_decline_code
        if pm.status == "active" and event.decline_code in ("card_expired", "stale_mandate"):
            pm.status = "expired"
    elif event.decline_code:
        expired = event.decline_code in ("card_expired", "stale_mandate")
        db.add(PaymentMethodRecord(id=f"pm_{case_id}", customer_id=event.customer_id,
                                   label="card_primary",
                                   status="expired" if expired else "active",
                                   last_decline_code=event.decline_code))

    case = Case(id=case_id, invoice_id=inv_id, customer_id=event.customer_id,
                case_type=CaseType.FAILED_PAYMENT, status=CaseStatus.NEW,
                detected_at=datetime.now(timezone.utc), amount_at_risk=event.amount)
    db.add(case)
    db.add(AuditLog(case_id=case_id, event_type="ingest_case", actor="system",
                    payload={"source": "payment-failed event",
                             "subscription_id": event.subscription_id,
                             "decline_code": event.decline_code}))
    db.commit()
    return {"case_id": case_id, "duplicate": False}


@router.get("/cases", dependencies=[Depends(require_scope("read"))])
def list_cases(status: str | None = None, limit: int = 500, db: Session = Depends(get_session)):
    q = db.query(Case)
    if status:
        try:
            q = q.filter(Case.status == CaseStatus(status.strip().upper()))
        except ValueError:
            return []
    return [
        {"case_id": c.id, "customer_id": c.customer_id, "invoice_id": c.invoice_id,
         "status": c.status.value, "amount_at_risk": c.amount_at_risk,
         "attempt_count": c.attempt_count, "last_action": c.last_action}
        for c in q.order_by(Case.created_at).limit(min(limit, 1000)).all()
    ]


@router.get("/cases/{case_id}", dependencies=[Depends(require_scope("read"))])
def get_case(case_id: str, db: Session = Depends(get_session)):
    c = db.get(Case, case_id)
    if not c:
        raise HTTPException(404, "case not found")
    cust = db.get(Customer, c.customer_id)
    diag = (db.query(AuditLog).filter(AuditLog.case_id == case_id, AuditLog.event_type == "diagnosis")
            .order_by(AuditLog.created_at.desc()).first())
    return {"case_id": c.id, "customer_id": c.customer_id, "invoice_id": c.invoice_id,
            "status": c.status.value, "attempt_count": c.attempt_count,
            "amount_at_risk": c.amount_at_risk, "last_action": c.last_action,
            "opted_out": cust.opted_out if cust else False,
            "archetype": cust.notes if cust else None,
            "diagnosis": diag.payload if diag else None}


@router.post("/agent/run/{case_id}", dependencies=[Depends(require_scope("run"))])
def run_agent(case_id: str, db: Session = Depends(get_session)):
    if not db.get(Case, case_id):
        raise HTTPException(404, "case not found")
    state = agent_graph.run_case(db, case_id)
    return {"case_id": case_id, "status": state.status.value,
            "terminal_reason": state.terminal_reason, "attempt_count": state.attempt_count}


@router.post(
    "/cases/{case_id}/simulate-payment",
    dependencies=[Depends(require_scope("admin")), Depends(require_prod_disabled)],
)
def simulate_payment(case_id: str, db: Session = Depends(get_session)):
    """Demo-only. Double-gated: admin scope AND ENVIRONMENT != prod."""
    c = db.get(Case, case_id)
    if not c:
        raise HTTPException(404, "case not found")
    p = PaymentEvent(invoice_id=c.invoice_id, amount_paid=c.amount_at_risk, source="manual_demo_insert")
    db.add(p)
    db.commit()
    return {"payment_event_id": p.id}


# ---- Payment gateway webhook: HMAC-verified, replay-deduplicated ----
# Supports both the Razorpay wire format (X-Razorpay-Signature header,
# event/entity payload, amounts in paise) and a generic signed format
# (X-Signature header + flat JSON) used by internal tests/tools.


class PaymentEventBody(BaseModel):
    event_id: str = Field(max_length=128)
    invoice_id: str = Field(max_length=64)
    amount_paid: float = Field(gt=0)


def _verify_generic_signature(raw_body: bytes, signature: str) -> bool:
    from app.integrations import payments as payments_svc

    if not config.PAYMENT_WEBHOOK_SECRET:
        raise HTTPException(503, "Webhook not configured")
    if payments_svc.verify_webhook_signature(raw_body, signature):
        return True
    # Legacy fallback: plain HMAC-SHA256 hexdigest under the generic name.
    import hashlib

    expected = hmac.new(config.PAYMENT_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def _parse_razorpay_event(raw: bytes) -> tuple[str | None, str | None, float | None, str | None]:
    """Returns (razorpay_event_type, invoice_reference, amount_inr, payment_entity_id)."""
    import json as _json

    body = _json.loads(raw)
    event_type = body.get("event")
    if event_type not in ("payment_link.paid", "payment.captured"):
        return event_type, None, None, None
    entity = body.get("payload", {}).get("payment", {}).get("entity") or {}
    amount_inr = round((entity.get("amount") or 0) / 100.0, 2)
    reference = (entity.get("notes") or {}).get("reference_id") or entity.get("description")
    return event_type, reference, (amount_inr if amount_inr > 0 else None), entity.get("id")


@router.post("/webhooks/payment")
async def payment_webhook(request: Request, db: Session = Depends(get_session)):
    raw = await request.body()
    razorpay_sig = request.headers.get("X-Razorpay-Signature")
    generic_sig = request.headers.get("X-Signature")

    try:
        if razorpay_sig:
            from app.integrations import payments as payments_svc

            if not payments_svc.verify_webhook_signature(raw, razorpay_sig):
                raise HTTPException(401, "Invalid webhook signature")
            rzp_event, invoice_ref, amount_paid, rzp_entity_id = _parse_razorpay_event(raw)
            if amount_paid is None:
                # Non-payment lifecycle event (e.g. payment_link.created): ack only.
                return {"status": "accepted", "matched": False, "ignored_event": rzp_event}
            event_id = request.headers.get("X-Razorpay-Event-Id", "")
            event = PaymentEventBody(
                event_id=event_id[:128], invoice_id=(invoice_ref or "")[:64], amount_paid=amount_paid
            )
        elif generic_sig is not None:
            if not _verify_generic_signature(raw, generic_sig):
                raise HTTPException(401, "Invalid webhook signature")
            try:
                event = PaymentEventBody.model_validate_json(raw)
            except Exception:
                raise HTTPException(400, "Malformed webhook payload")
        else:
            raise HTTPException(401, "Missing webhook signature")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(401, "Invalid webhook signature")

    if not event.invoice_id:
        raise HTTPException(400, "Webhook payload missing invoice reference")

    # Replay deduplication. The Razorpay path derives its key from stable body
    # fields (event type + payment entity id) because X-Razorpay-Event-Id is
    # optional — a missing header must never collapse distinct payments onto a
    # shared key. With neither an entity id nor the header, we process WITHOUT
    # a ledger row: dropping real payment confirmations is worse than a rare
    # double-processing, and mark_recovered is idempotent downstream anyway.
    if razorpay_sig:
        dedup_key = (
            f"webhook:rzp:{rzp_event}:{rzp_entity_id}" if rzp_entity_id
            else (f"webhook:{request.headers.get('X-Razorpay-Event-Id')}"
                  if request.headers.get("X-Razorpay-Event-Id") else None)
        )
    else:
        dedup_key = f"webhook:{event.event_id}"
    if dedup_key is not None and db.get(PolicyDecisionRecord, dedup_key):
        return {"status": "duplicate"}

    case = db.query(Case).filter(Case.invoice_id == event.invoice_id).first()
    payload = {"matched": False}
    if case:
        p = PaymentEvent(
            invoice_id=event.invoice_id,
            amount_paid=event.amount_paid,
            source="webhook",
            consumed=False,
        )
        db.add(p)
        db.flush()
        db.add(AuditLog(case_id=case.id, event_type="payment_webhook", actor="system",
                        payload={"amount_paid": event.amount_paid, "event_id": event.event_id}))
        payload = {"matched": True, "payment_event_id": p.id}
    # Ledger row records unmatched events too, so gateway replays stay idempotent.
    if dedup_key is not None:
        db.add(PolicyDecisionRecord(
            idempotency_key=dedup_key, case_id=case.id if case else None,
            action="payment_webhook", attempt_number=0, result_payload=payload,
        ))
    db.commit()
    return {"status": "accepted", **payload}
