"""Write tools backed by real outbound providers (email + Razorpay).

Every tool:
- takes an explicit attempt_number, builds idempotency key f"{case_id}:{action}:{attempt_number}"
- enforces exactly-once via the DB-level unique key in `tool_executions`
- logs one audit_log row in the SAME transaction as the state-affecting write
- contains zero policy logic (policy gating happens upstream in the graph)

Delivery outcomes are never simulated: each tool either returns a real
provider result or raises ToolExecutionError (retryability preserved from
the underlying adapter).
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

import app.config as config
from app.db.tables import AuditLog, Case, Customer, EscalationTicket, Invoice, InvoiceStatus, PaymentEvent, PolicyDecisionRecord
from app.integrations import email as email_svc

logger = logging.getLogger("app.tools.write_tools")
from app.integrations import payments as payments_svc
from app.models.schemas import DeliveryResult, EscalationTicket as EscalationResult
from app.tools.read_tools import NotFoundError

RETRYABLE_ATTEMPTS = 2


class ToolExecutionError(Exception):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def _with_retries(fn, *, retryable_check=None):
    """Timeout+retry wrapper at the tool-call layer. Retries only retryable failures."""
    delay = 0.01
    for attempt in range(1 + RETRYABLE_ATTEMPTS):
        try:
            return fn()
        except ToolExecutionError as e:
            if not e.retryable or attempt == RETRYABLE_ATTEMPTS:
                raise
            import time

            time.sleep(delay)
            delay *= 2


def _idempotent_execute(db: Session, case_id: str, action: str, attempt_number: int):
    """Returns (record | None). If a record exists, the caller must return it verbatim."""
    key = f"{case_id}:{action}:{attempt_number}"
    db.flush()  # make same-transaction prior executions visible to the lookup
    existing = db.get(PolicyDecisionRecord, key)
    if existing:
        return existing, key
    return None, key


def _audit(db: Session, case_id: str, event_type: str, payload: dict, reasoning: str | None = None):
    db.add(
        AuditLog(
            case_id=case_id,
            event_type=event_type,
            actor="agent",
            payload=payload,
            reasoning=reasoning,
        )
    )


def _get_case(db: Session, case_id: str) -> Case:
    case = db.get(Case, case_id)
    if not case:
        raise ToolExecutionError(f"case {case_id} not found", retryable=False)
    return case


def _guard_writes():
    # B3 rollback kill switch: flipping WRITE_TOOLS_ENABLED=false parks outbound
    # sends as retryable failures — cases stay recoverable, nothing is lost.
    if not config.WRITE_TOOLS_ENABLED:
        raise ToolExecutionError("write tools disabled by config (WRITE_TOOLS_ENABLED=false)", retryable=True)


def _customer_email(db: Session, case: Case) -> tuple[str, str]:
    cust = db.get(Customer, case.customer_id)
    if not cust or not cust.email:
        raise ToolExecutionError(
            f"customer {case.customer_id} has no email on file; cannot deliver outbound message",
            retryable=False,
        )
    return cust.email, cust.name


def _deliver_email(db: Session, case: Case, subject: str, body: str) -> dict:
    """Sends via the configured EMAIL_PROVIDER. Maps adapter errors onto the
    tool error contract so the graph's retry/escalation logic keeps working."""
    to_addr, _ = _customer_email(db, case)
    try:
        result = email_svc.send_email(to_addr, subject, body)
    except email_svc.EmailDeliveryError as e:
        raise ToolExecutionError(f"email delivery failed: {e}", retryable=e.retryable) from e
    except Exception as e:  # unexpected transport failure — treat as transient
        raise ToolExecutionError(f"email delivery failed: {e}", retryable=True) from e
    return result


def _send_reminder_delivery(db: Session, case: Case, channel: str, message: str) -> DeliveryResult:
    """channel=email only. There is deliberately no simulated-success path."""
    _guard_writes()
    invoice = db.get(Invoice, case.invoice_id)
    subject = f"Payment reminder: invoice {case.invoice_id} ({invoice.currency} {case.amount_at_risk:,.0f})"
    if channel == "sms":
        raise ToolExecutionError("sms delivery requires an SMS provider configuration", retryable=False)
    if channel != "email":
        raise ToolExecutionError(f"unsupported channel {channel!r}", retryable=False)
    sent = _deliver_email(db, case, subject, message)
    return DeliveryResult(
        status="delivered",
        provider_message_id=sent.get("provider_message_id"),
        sent_at=datetime.now(timezone.utc),
        idempotency_key=f"{case.id}:send_reminder:{case.attempt_count}",
    )


def send_reminder(db: Session, case_id: str, channel: str, message: str, attempt_number: int) -> DeliveryResult:
    case = _get_case(db, case_id)

    def do():
        record, key = _idempotent_execute(db, case_id, "send_reminder", attempt_number)
        if record:
            return DeliveryResult(**record.result_payload)
        result = _send_reminder_delivery(db, case, channel, message)
        result.idempotency_key = key
        _register(db, key, case_id, "send_reminder", attempt_number, result.model_dump(mode="json"))
        _audit(db, case_id, "send_reminder", {"channel": channel, "message": message[:500]} | result.model_dump(mode="json"))
        _bump_case(db, case, "send_reminder")
        return result

    return _with_retries(do)


def send_payment_link(db: Session, case_id: str, channel: str, attempt_number: int) -> DeliveryResult:
    case = _get_case(db, case_id)

    def do():
        record, key = _idempotent_execute(db, case_id, "send_payment_link", attempt_number)
        if record:
            return DeliveryResult(**record.result_payload)
        _guard_writes()
        to_addr, cust_name = _customer_email(db, case)
        try:
            link = payments_svc.create_payment_link(
                amount_inr=case.amount_at_risk,
                reference_id=case.invoice_id,
                customer_email=to_addr,
                customer_name=cust_name,
                description=f"Payment for overdue invoice {case.invoice_id}",
            )
        except payments_svc.PaymentProviderError as e:
            raise ToolExecutionError(f"payment link creation failed: {e}", retryable=e.retryable) from e
        sent = _deliver_email(
            db, case,
            f"Pay your overdue invoice {case.invoice_id} securely",
            f"Hi {cust_name},\n\nYou can settle invoice {case.invoice_id} "
            f"(INR {case.amount_at_risk:,.2f}) using this secure payment link:\n{link['short_url']}\n",
        )
        result = DeliveryResult(
            status="delivered",
            provider_message_id=link["link_id"],
            sent_at=datetime.now(timezone.utc),
            idempotency_key=key,
        )
        _register(db, key, case_id, "send_payment_link", attempt_number,
                  result.model_dump(mode="json") | {"short_url": link["short_url"], "email_provider_message_id": sent.get("provider_message_id")})
        _audit(db, case_id, "send_payment_link", {"channel": channel, "link_id": link["link_id"], "short_url": link["short_url"]} | result.model_dump(mode="json"))
        _bump_case(db, case, "send_payment_link")
        return result

    return _with_retries(do)


def record_promise_to_pay(db: Session, case_id: str, promised_date: str, attempt_number: int) -> dict:
    from app.db.tables import Promise

    case = _get_case(db, case_id)

    def do():
        record, key = _idempotent_execute(db, case_id, "record_promise_to_pay", attempt_number)
        if record:
            return record.result_payload
        payload = {"promised_date": promised_date}
        db.add(
            Promise(
                case_id=case_id,
                customer_id=case.customer_id,
                promised_date=datetime.fromisoformat(promised_date),
                kept=None,
            )
        )
        _register(db, key, case_id, "record_promise_to_pay", attempt_number, payload)
        _audit(db, case_id, "record_promise_to_pay", payload)
        _bump_case(db, case, "record_promise_to_pay")
        return payload

    return _with_retries(do)


def escalate_to_human(db: Session, case_id: str, reason: str, summary: str, attempt_number: int) -> EscalationResult:
    case = _get_case(db, case_id)

    def do():
        record, key = _idempotent_execute(db, case_id, "escalate_to_human", attempt_number)
        if record:
            return EscalationResult(**record.result_payload)
        ticket = EscalationResult(
            ticket_id=f"tick_{uuid.uuid4().hex[:10]}",
            case_id=case_id,
            reason=reason,
            summary=summary,
        )
        db.add(
            EscalationTicket(
                id=ticket.ticket_id, case_id=case_id, reason=reason[:255], summary=summary, status="open"
            )
        )
        # The ticket is the load-bearing artifact; the internal notification is
        # best-effort. An email outage must never block an escalation.
        _register(db, key, case_id, "escalate_to_human", attempt_number, ticket.model_dump(mode="json"))
        _audit(db, case_id, "escalate_to_human", ticket.model_dump(mode="json"), reasoning=summary)
        # Escalation notification goes to the internal collections inbox, not the customer.
        escalation_recipient = config.EMAIL_FROM
        if config.EMAIL_PROVIDER != "console":
            try:
                email_svc.send_email(
                    escalation_recipient,
                    f"[ESCALATION] case {case_id}: {reason}",
                    f"Case {case_id} requires human review.\n\nReason: {reason}\n\n{summary}",
                )
            except email_svc.EmailDeliveryError as e:
                logger.warning("escalation.notify_failed | case=%s err=%s", case_id, e)
        return ticket

    return _with_retries(do)


def mark_recovered(
    db: Session, case_id: str, amount: float, verified_by: str, verified_payment_id: int, attempt_number: int
) -> dict:
    """Only fires on a verified payment event. The precondition is an actual assertion —
    an LLM hallucinating success must fail here, hard."""
    if verified_by != "payment_poller":
        raise ToolExecutionError(
            f"mark_recovered requires verified_by='payment_poller', got {verified_by!r}",
            retryable=False,
        )
    payment = db.get(PaymentEvent, verified_payment_id)
    case = _get_case(db, case_id)
    if payment is None or payment.invoice_id != case.invoice_id:
        raise ToolExecutionError(
            f"no backing payment_events row id={verified_payment_id} for case {case_id}",
            retryable=False,
        )

    def do():
        record, key = _idempotent_execute(db, case_id, "mark_recovered", attempt_number)
        if record:
            return record.result_payload
        payment.consumed = True

        inv = db.get(Invoice, _get_case(db, case_id).invoice_id)
        inv.status = InvoiceStatus.RECOVERED
        payload = {
            "amount": amount,
            "verified_by": verified_by,
            "verified_payment_id": verified_payment_id,
            "gateway_payment_id": payment.gateway_payment_id,
            "status": "recovered",
        }
        _register(db, key, case_id, "mark_recovered", attempt_number, payload)
        _audit(db, case_id, "mark_recovered", payload)
        return payload

    return _with_retries(do)


# ---- helpers ----


def _register(db, key, case_id, action, attempt_number, result_payload):
    db.add(
        PolicyDecisionRecord(
            idempotency_key=key,
            case_id=case_id,
            action=action,
            attempt_number=attempt_number,
            result_payload=result_payload,
        )
    )


def _bump_case(db, case: Case, action: str):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if case.messages_sent_date != today:
        case.messages_sent_date = today
        case.messages_sent_today = 0
    case.messages_sent_today += 1
    case.last_action = action
    case.last_action_at = datetime.now(timezone.utc)
    case.next_allowed_action_at = datetime.now(timezone.utc) + timedelta(hours=6)
