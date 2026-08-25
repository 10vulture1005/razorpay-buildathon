import hashlib
import hmac
from datetime import datetime, timedelta, timezone

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
    Promise,
    Subscription,
    CommunicationMessage,
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
    customer_name: str | None = Field(default=None, max_length=255)
    # How far past due the invoice already is. Billing integrations send this
    # when their export lags; 0 means "due today". Clamped to the policy
    # recovery window minus one day so test events never arrive pre-expired.
    days_overdue: int = Field(default=0, ge=0, le=3650)


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
        cust = Customer(id=event.customer_id,
                        name=event.customer_name or f"Auto-registered {event.customer_id}",
                        opted_out=False, notes="auto_registered", email=event.customer_email)
        db.add(cust)
    else:
        # Billing systems re-send the current contact on every event — the
        # freshest email/name wins over whatever we stored before.
        if event.customer_email:
            cust.email = event.customer_email
        if event.customer_name:
            cust.name = event.customer_name
    from datetime import timedelta

    from app.policy.policy_engine import load_policy_config

    now = datetime.now(timezone.utc)
    # Clamp so a case can never be ingested already past the recovery window.
    max_days = max(int(load_policy_config().get("max_recovery_window_days", 7)) - 1, 0)
    effective_days = min(event.days_overdue, max_days)
    due = now - timedelta(days=effective_days)
    if not db.get(Invoice, event.invoice_id):
        db.add(Invoice(id=event.invoice_id, customer_id=event.customer_id, amount=event.amount,
                       currency="INR", due_date=due))
    case = Case(id=case_id, invoice_id=event.invoice_id, customer_id=event.customer_id,
                case_type=CaseType.RECEIVABLE, status=CaseStatus.NEW,
                detected_at=due, amount_at_risk=event.amount)
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


@router.get("/tickets", dependencies=[Depends(require_scope("read"))])
def list_tickets(status: str = "open", limit: int = 100, db: Session = Depends(get_session)):
    """Human-review queue: escalation tickets joined with the company dossier,
    so the reviewer sees WHO they are about to call and how those calls
    usually go — not just a bare case id."""
    from datetime import timedelta

    from app.tools.read_tools import get_customer_history
    from app.db.tables import EscalationTicket

    now = datetime.now(timezone.utc)
    out = []
    q = db.query(EscalationTicket).order_by(EscalationTicket.created_at.desc())
    if status != "all":
        q = q.filter(EscalationTicket.status == status)
    for t in q.limit(min(limit, 500)).all():
        case = db.get(Case, t.case_id)
        if not case:
            continue
        cust = db.get(Customer, case.customer_id)
        try:
            hist = get_customer_history(db, case.customer_id)
        except Exception:
            hist = None
        actions_tried = [
            e.payload.get("action")
            for e in db.query(AuditLog)
            .filter(AuditLog.case_id == t.case_id, AuditLog.event_type == "action_selected")
            .order_by(AuditLog.created_at)
            .all()
            if e.payload and e.payload.get("action")
        ]
        diag = (
            db.query(AuditLog)
            .filter(AuditLog.case_id == t.case_id, AuditLog.event_type == "diagnosis")
            .order_by(AuditLog.created_at.desc())
            .first()
        )
        out.append({
            "ticket_id": t.id,
            "case_id": t.case_id,
            "reason": t.reason,
            "summary": t.summary,
            "ticket_status": t.status,
            "created_at": t.created_at,
            "amount_at_risk": case.amount_at_risk,
            "days_overdue": max(
                (now - (case.detected_at if case.detected_at.tzinfo else case.detected_at.replace(tzinfo=timezone.utc))).days,
                0,
            ),
            "actions_tried": actions_tried,
            "diagnosis": diag.payload if diag else None,
            "company": {
                "name": cust.name if cust else case.customer_id,
                "email": cust.email if cust else None,
                "opted_out": cust.opted_out if cust else False,
                "on_time_rate": hist.on_time_rate if hist else None,
                "avg_days_late": hist.avg_days_late if hist else None,
                "broken_promise_count": hist.broken_promise_count if hist else None,
                "invoices_total": hist.invoices_total if hist else None,
            },
        })
    return out


@router.post("/tickets/{ticket_id}/resolve", dependencies=[Depends(require_scope("run"))])
def resolve_ticket(ticket_id: str, db: Session = Depends(get_session)):
    from app.db.tables import EscalationTicket

    t = db.get(EscalationTicket, ticket_id)
    if not t:
        raise HTTPException(404, "ticket not found")
    t.status = "resolved"
    db.add(AuditLog(case_id=t.case_id, event_type="ticket_resolved", actor="human",
                    payload={"ticket_id": ticket_id}))
    db.commit()
    return {"ticket_id": ticket_id, "status": t.status}


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


# ---- Inbound reply email (Phase C): Mailgun route -> classify -> decide ----
# Mailgun "forward to URL" posts multipart form: sender, subject, body-plain,
# timestamp, token, signature. The LLM classifies intent; the deterministic
# gate below decides what happens. No outreach decision rests on the model.


def _verify_mailgun_signature(form) -> bool:
    """Mailgun signs webhook posts: HMAC-SHA256(timestamp + token, signing key)."""
    if not config.MAILGUN_WEBHOOK_SIGNING_KEY:
        raise HTTPException(503, "Inbound email not configured")
    ts = form.get("timestamp", "")
    token = form.get("token", "")
    sig = form.get("signature", "")
    expected = hmac.new(
        config.MAILGUN_WEBHOOK_SIGNING_KEY.encode(),
        f"{ts}{token}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, sig or "")


def _open_case_for_sender(db: Session, sender_email: str) -> Case | None:
    """Most relevant open case for the replying address: prefer AWAITING_OUTCOME,
    then any non-terminal, most recently detected first."""
    cust = db.query(Customer).filter(Customer.email == sender_email).first()
    if not cust:
        return None
    open_statuses = [CaseStatus.AWAITING_OUTCOME, CaseStatus.DIAGNOSED,
                     CaseStatus.ACTION_SELECTED, CaseStatus.EXECUTING, CaseStatus.NEW]
    for status in open_statuses:
        case = (
            db.query(Case)
            .filter(Case.customer_id == cust.id, Case.status == status)
            .order_by(Case.detected_at.desc())
            .first()
        )
        if case:
            return case
    return None


def _classify_reply(subject: str, body: str, case: Case):
    """LLM structured classification of the reply. Raises on validation failure."""
    from app.agent.llm import call_structured
    from app.models.schemas import ReplyIntent

    prompt = {
        "subject": subject[:300],
        "body": body[:1500],
        "amount_at_risk": case.amount_at_risk,
        "attempt_count": case.attempt_count,
        "today": datetime.now(timezone.utc).date().isoformat(),
    }
    return call_structured(ReplyIntent, prompt)


def _apply_reply_decision(db: Session, case: Case, intent) -> dict:
    """Deterministic gate over the classified intent. Returns an action summary."""
    from app.policy.policy_engine import load_policy_config
    from app.tools import write_tools

    cfg = load_policy_config()

    def _audit(event_type, payload):
        db.add(AuditLog(case_id=case.id, event_type=event_type, actor="agent", payload=payload))

    if intent.intent == "dispute":
        ticket = write_tools.escalate_to_human(
            db, case.id, reason="dispute_reply",
            summary=f"Customer replied disputing the invoice: {intent.reasoning[:200]}",
            attempt_number=case.attempt_count,
        )
        _audit("reply_dispute_escalated",
               {"ticket_id": ticket.ticket_id, "reasoning": intent.reasoning})
        return {"action": "escalated_dispute"}

    if intent.intent == "payment_commitment" and intent.promised_date is not None:
        today = datetime.now(timezone.utc).date()
        max_days = int(cfg.get("max_extension_days", 14))
        max_ext = int(cfg.get("max_extensions_per_case", 1))
        already = (
            db.query(AuditLog)
            .filter(AuditLog.case_id == case.id, AuditLog.event_type == "granted_more_time")
            .count()
        )
        if already >= max_ext:
            _audit("extension_denied",
                   {"reason": "extension_limit_reached", "reasoning": intent.reasoning})
            return {"action": "extension_denied", "why": "limit"}
        # Clamp the promised date into [tomorrow, today + max_days].
        clamped = min(max(intent.promised_date, today + timedelta(days=1)),
                      today + timedelta(days=max_days))
        db.add(Promise(customer_id=case.customer_id, case_id=case.id,
                          promised_date=datetime(clamped.year, clamped.month, clamped.day, tzinfo=timezone.utc),
                          kept=None))
        case.next_allowed_action_at = datetime(clamped.year, clamped.month, clamped.day,
                                               tzinfo=timezone.utc)
        case.status = CaseStatus.AWAITING_OUTCOME
        _audit("granted_more_time",
               {"promised_date": clamped.isoformat(), "requested": intent.promised_date.isoformat(),
                "confidence": intent.confidence, "reasoning": intent.reasoning})
        return {"action": "granted_more_time", "until": clamped.isoformat()}

    _audit("reply_logged",
           {"intent": intent.intent, "confidence": intent.confidence,
            "reasoning": intent.reasoning})
    return {"action": "logged", "intent": intent.intent}


@router.post("/emails/inbound")
async def inbound_reply(request: Request, db: Session = Depends(get_session)):
    """Mailgun Receiving route target. Signature-verified, best-effort match to
    an open case by sender address; always 200s after processing so Mailgun
    does not retry-loop (except on auth/config errors, which SHOULD retry-later
    never — those 4xx immediately)."""
    form = await request.form()
    if not _verify_mailgun_signature(form):
        raise HTTPException(401, "Invalid Mailgun signature")

    sender = (form.get("sender") or "").strip().lower()
    subject = form.get("subject") or ""
    body = (form.get("body-plain") or form.get("stripped-text") or "").strip()
    if not sender or "@" not in sender:
        raise HTTPException(400, "missing sender")

    case = _open_case_for_sender(db, sender)
    if not case:
        # Unknown sender / no open case: ack so Mailgun stops retrying.
        return {"status": "accepted", "matched": False}

    db.add(CommunicationMessage(
        case_id=case.id, direction="inbound", channel="email",
        body=f"[re] {subject}\n{body}"[:4000],
    ))

    try:
        intent = _classify_reply(subject, body, case)
    except Exception as e:
        import logging

        logging.getLogger("app.api.inbound").warning(
            "reply_classify_failed | case=%s err=%s: %s", case.id, type(e).__name__, str(e)[:300])
        _audit_safe(db, case.id, "reply_classify_failed", {})
        db.commit()
        return {"status": "accepted", "matched": True, "classified": False}

    result = _apply_reply_decision(db, case, intent)
    db.commit()
    return {"status": "accepted", "matched": True, **result}


def _audit_safe(db: Session, case_id: str, event_type: str, payload: dict):
    db.add(AuditLog(case_id=case_id, event_type=event_type, actor="system", payload=payload))
