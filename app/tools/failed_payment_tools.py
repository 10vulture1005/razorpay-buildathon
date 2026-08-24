"""Phase-7 extension write tools for failed-payment recovery.
Same idempotency + audit discipline as Phase 1; real provider backends:
retry_payment creates a fresh Razorpay payment link for the subscription
amount; dunning/update-method prompts are delivered via the email adapter."""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

import app.config as config
from app.db.tables import AuditLog, PolicyDecisionRecord, Subscription
from app.integrations import payments as payments_svc
from app.tools.write_tools import (
    ToolExecutionError,
    _deliver_email,
    _get_case,
    _guard_writes,
    _register,
)


def _execute(db, case_id, action, attempt_number, fn):
    key = f"{case_id}:{action}:{attempt_number}"
    existing = db.get(PolicyDecisionRecord, key)
    if existing:
        return existing.result_payload
    payload = fn()
    db.add(PolicyDecisionRecord(idempotency_key=key, case_id=case_id, action=action,
                                attempt_number=attempt_number, result_payload=payload))
    db.add(AuditLog(case_id=case_id, event_type=action, actor="agent", payload=payload))
    case = _get_case(db, case_id)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if case.messages_sent_date != today:
        case.messages_sent_date, case.messages_sent_today = today, 0
    case.messages_sent_today += 1
    case.last_action = action
    return payload


def retry_payment(db: Session, case_id: str, attempt_number: int) -> dict:
    """Submits a fresh charge attempt by creating a Razorpay payment link for
    the outstanding amount (auto-collection flow). The gateway notifies us of
    completion via webhook; nothing here assumes success."""
    from app.integrations.email import EmailDeliveryError

    def do():
        _guard_writes()
        case = _get_case(db, case_id)
        try:
            link = payments_svc.create_payment_link(
                amount_inr=case.amount_at_risk,
                reference_id=case.invoice_id,
                description=f"Retry payment for invoice {case.invoice_id}",
            )
        except payments_svc.PaymentProviderError as e:
            raise ToolExecutionError(f"razorpay retry submission failed: {e}", retryable=e.retryable) from e
        try:
            sent = _deliver_email(
                db, case,
                f"Action needed: complete your payment for invoice {case.invoice_id}",
                f"Your recent payment attempt did not go through. You can complete "
                f"the payment securely here:\n{link['short_url']}\n",
            )
        except ToolExecutionError as e:
            raise ToolExecutionError(str(e), retryable=e.retryable) from e
        except EmailDeliveryError as e:
            raise ToolExecutionError(f"email delivery failed: {e}", retryable=e.retryable) from e
        return {
            "status": "retry_submitted",
            "link_id": link["link_id"],
            "short_url": link["short_url"],
            "email_provider_message_id": sent.get("provider_message_id"),
        }

    from app.tools.write_tools import _with_retries

    return _with_retries(lambda: _execute(db, case_id, "retry_payment", attempt_number, do))


def update_payment_method_prompt(db: Session, case_id: str, attempt_number: int) -> dict:
    def do():
        _guard_writes()
        sent = _deliver_email(
            db, _get_case(db, case_id),
            "Update your payment method to avoid service interruption",
            "Your saved payment method was declined. Please update it so we can "
            "complete your billing without interruption.",
        )
        return {"status": "prompt_sent",
                "provider_message_id": sent.get("provider_message_id")}

    return _execute(db, case_id, "update_payment_method_prompt", attempt_number, do)


def send_dunning_email(db: Session, case_id: str, attempt_number: int) -> dict:
    def do():
        _guard_writes()
        sent = _deliver_email(
            db, _get_case(db, case_id),
            "Your payment is past due",
            "This is a notice that your account payment is past due. Please "
            "settle the outstanding amount to keep your services active.",
        )
        return {"status": "delivered", "provider_message_id": sent.get("provider_message_id")}

    return _execute(db, case_id, "send_dunning_email", attempt_number, do)
