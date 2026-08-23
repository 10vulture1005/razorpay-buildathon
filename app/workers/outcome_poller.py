"""Payment reconciliator. Two modes:
- interval-based: `run_poller(interval_seconds)` worker loop over AWAITING_OUTCOME cases
- synchronous: `check_recovery_now(...)` for eval/batch runs (no sleeping)

Recovery sources: verified `payment_events` rows (webhook-inserted or
reconciliator-confirmed gateway payments) for receivables, and verified
`retry_events` rows for failed-payment cases. mark_recovered only ever fires
with one of those references — never on an agent's word.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import app.config as config
from sqlalchemy.orm import Session

from app.agent.nodes import nodes
from app.db.tables import Case, CaseType, PaymentEvent, PolicyDecisionRecord, RetryEvent
from app.models.domain import CaseState
from app.tools import write_tools

logger = logging.getLogger("app.workers.outcome_poller")


def check_recovery_now(db: Session, state: CaseState) -> dict | None:
    """Synchronous 'check now'. Returns recovery info if a verified payment exists."""
    case = db.get(Case, state.case_id)

    if case.case_type in (CaseType.RECEIVABLE, "receivable"):
        payment = (
            db.query(PaymentEvent)
            .filter(PaymentEvent.invoice_id == case.invoice_id, PaymentEvent.consumed.is_(False))
            .order_by(PaymentEvent.paid_at.desc())
            .first()
        )
        if not payment or payment.amount_paid < case.amount_at_risk * 0.999:
            return None
        result = write_tools.mark_recovered(
            db, state.case_id, payment.amount_paid,
            verified_by="payment_poller", verified_payment_id=payment.id,
            attempt_number=max(state.attempt_count, 1),
        )
        nodes._audit(db, state.case_id, "payment_detected", "system",
                     {"payment_event_id": payment.id, "amount_paid": payment.amount_paid,
                      "gateway_payment_id": payment.gateway_payment_id})
        return result

    # failed-payment extension: verified retry result
    retry = (
        db.query(RetryEvent)
        .filter(RetryEvent.case_id == state.case_id, RetryEvent.consumed.is_(False), RetryEvent.succeeded.is_(True))
        .first()
    )
    if not retry:
        return None
    retry.consumed = True
    from app.db.tables import Subscription

    sub = db.query(Subscription).filter(Subscription.customer_id == case.customer_id).first()
    if sub:
        sub.status = "active"
    nodes._audit(db, state.case_id, "retry_succeeded", "system",
                 {"retry_event_id": retry.id, "amount": retry.amount,
                  "gateway_payment_id": retry.gateway_payment_id})
    return {"case_id": state.case_id, "amount": retry.amount,
            "verified_by": "payment_poller", "verified_payment_id": -retry.id}


def gateway_fetch_fallback(db: Session) -> int:
    """Safety net for missed webhooks (Razorpay only).

    For receivable cases sitting in AWAITING_OUTCOME with NO payment_event row
    past POLL_FALLBACK_AFTER_S after their payment link was sent, query the
    gateway directly via the link_id persisted in the idempotency ledger. A
    captured payment becomes a real PaymentEvent(source="gateway_poll"), then
    converges on the same check_recovery_now → mark_recovered path as
    webhook-triggered recoveries. Webhooks remain the primary fast path.
    """
    if config.PAYMENT_PROVIDER != "razorpay":
        return 0
    from app.integrations import payments as payments_svc

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=config.POLL_FALLBACK_AFTER_S)
    # SQLite (dev/tests) stores naive UTC datetimes; Postgres keeps tz-aware.
    if db.bind is not None and db.bind.dialect.name == "sqlite":
        cutoff = cutoff.replace(tzinfo=None)
    awaiting = (
        db.query(Case)
        .filter(Case.status == "AWAITING_OUTCOME", Case.case_type == CaseType.RECEIVABLE,
                Case.last_action == "send_payment_link",
                Case.last_action_at.isnot(None), Case.last_action_at < cutoff)
        .all()
    )
    recovered = 0
    for case in awaiting:
        has_row = db.query(PaymentEvent).filter(
            PaymentEvent.invoice_id == case.invoice_id,
            PaymentEvent.consumed.is_(False)).first()
        if has_row:
            continue  # normal path handles it this cycle
        rec = (
            db.query(PolicyDecisionRecord)
            .filter(PolicyDecisionRecord.case_id == case.id,
                    PolicyDecisionRecord.action == "send_payment_link")
            .order_by(PolicyDecisionRecord.executed_at.desc())
            .first()
        )
        link_id = (rec.result_payload or {}).get("provider_message_id") if rec else None
        if not link_id:
            continue
        try:
            attempts = payments_svc.fetch_payment_link_payments(link_id)
        except payments_svc.PaymentProviderError as e:
            logger.warning("poller.gateway_fetch_failed", extra={
                "case_id": case.id, "link_id": link_id, "error": str(e)})
            continue
        for pay in attempts:
            if pay.get("status") not in ("captured", "paid"):
                continue
            amount_inr = round((pay.get("amount") or 0) / 100.0, 2)
            if amount_inr < case.amount_at_risk * 0.999:
                continue
            db.add(PaymentEvent(
                invoice_id=case.invoice_id, amount_paid=amount_inr,
                gateway_payment_id=pay.get("id"),
                source="gateway_poll", consumed=False,
            ))
            db.flush()  # sessions run autoflush=False; make the row visible below
            nodes._audit(db, case.id, "gateway_poll_payment_found", "system",
                         {"link_id": link_id, "gateway_payment_id": pay.get("id"),
                          "amount_inr": amount_inr})
            state = CaseState(case_id=case.id, invoice_id=case.invoice_id,
                              customer_id=case.customer_id,
                              attempt_count=max(case.attempt_count, 1))
            if check_recovery_now(db, state):
                from app.db.tables import CaseStatus

                nodes.set_status(db, case.id, CaseStatus.RECOVERED)
                recovered += 1
                # autoflush=False: flush so later cycles/queries in this same
                # session see the terminal status instead of re-processing.
                db.flush()
                logger.info("poller.recovered_via_gateway_fetch", extra={"case_id": case.id})
            break
    return recovered


async def run_poller(db_factory, interval_seconds: float = 5.0):
    """Interval mode: polls AWAITING_OUTCOME cases and recovers verified payments."""
    while True:
        db = db_factory()
        try:
            awaiting = db.query(Case).filter(Case.status == "AWAITING_OUTCOME").all()
            for case in awaiting:
                state = CaseState(case_id=case.id, invoice_id=case.invoice_id, customer_id=case.customer_id,
                                  attempt_count=case.attempt_count)
                if check_recovery_now(db, state):
                    from app.db.tables import CaseStatus

                    nodes.set_status(db, case.id, CaseStatus.RECOVERED)
            gateway_fetch_fallback(db)
            db.commit()
        finally:
            db.close()
        await asyncio.sleep(interval_seconds)


if __name__ == "__main__":
    """Worker entrypoint: `python -m app.workers.outcome_poller [interval_s]`.
    Runs the reconciliator loop against AWAITING_OUTCOME cases (P1-2 backstop)."""
    import os
    import sys

    from app.db.session import SessionLocal

    interval = float(sys.argv[1]) if len(sys.argv) > 1 else float(os.environ.get("POLL_INTERVAL_S", "30"))
    from app.observability.logging_setup import setup_logging

    setup_logging()
    asyncio.run(run_poller(SessionLocal, interval))
