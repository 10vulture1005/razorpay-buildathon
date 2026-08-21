"""Read tools. Pure reads, no side effects, never policy-gated."""
from datetime import datetime, timezone

from sqlalchemy import func as safunc
from sqlalchemy.orm import Session

from app.db.tables import (
    Case,
    CommunicationMessage,
    Customer,
    Invoice as InvoiceRow,
    PaymentMethodRecord,
    Promise as PromiseRow,
    Subscription,
)
from app.models.schemas import (
    BaseModel,
    CustomerHistory,
    Invoice,
    Message,
    Promise,
)


class NotFoundError(Exception):
    pass


def get_customer_history(db: Session, customer_id: str) -> CustomerHistory:
    cust = db.get(Customer, customer_id)
    if not cust:
        raise NotFoundError(f"customer {customer_id} not found")
    rows = (
        db.query(InvoiceRow)
        .filter(InvoiceRow.customer_id == customer_id)
        .order_by(InvoiceRow.due_date)
        .all()
    )
    now = datetime.now(timezone.utc)
    total = max(len(rows) - 1, 0) or 1
    on_time = 0
    late_days = 0

    def _aware(dt):
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    for r in rows[:-1]:  # exclude the currently-overdue invoice itself
        days_late = (now - _aware(r.due_date)).days
        if days_late <= 0:
            on_time += 1
        else:
            # paid-late proxy: history invoices with no open case are treated settled
            has_case = db.query(Case).filter(Case.invoice_id == r.id).first()
            if not has_case:
                on_time += 1
                late_days += 0
            else:
                late_days += min(days_late, 60)
    broken = db.query(PromiseRow).filter(PromiseRow.customer_id == customer_id,
                                PromiseRow.kept.is_(False)).count()
    return CustomerHistory(
        customer_id=customer_id,
        name=cust.name,
        opted_out=cust.opted_out,
        invoices_total=len(rows),
        on_time_rate=round(on_time / total, 2),
        avg_days_late=round(late_days / total, 1),
        broken_promise_count=broken,
    )


def get_invoice(db: Session, invoice_id: str) -> Invoice:
    inv = db.get(InvoiceRow, invoice_id)
    if not inv:
        raise NotFoundError(f"invoice {invoice_id} not found")
    return Invoice(
        id=inv.id,
        customer_id=inv.customer_id,
        amount=inv.amount,
        currency=inv.currency,
        due_date=inv.due_date,
        status=inv.status.value if hasattr(inv.status, "value") else inv.status,
    )


def get_past_promises(db: Session, customer_id: str) -> list[Promise]:
    rows = (
        db.query(PromiseRow)
        .filter(PromiseRow.customer_id == customer_id)
        .order_by(PromiseRow.promised_date.desc())
        .limit(5)
        .all()
    )
    return [
        Promise(promised_date=p.promised_date, kept=p.kept)
        for p in rows
    ]


def get_communication_log(db: Session, case_id: str) -> list[Message]:
    rows = (
        db.query(CommunicationMessage)
        .filter(CommunicationMessage.case_id == case_id)
        .order_by(CommunicationMessage.created_at)
        .all()
    )
    return [
        Message(direction=m.direction, channel=m.channel, body=m.body, created_at=m.created_at)
        for m in rows
    ]


# ---- Failed-payment extension read tools (Phase 7), same discipline ----


class PaymentMethodStatus(BaseModel):
    customer_id: str
    label: str
    status: str
    last_decline_code: str | None


def get_payment_method_status(db: Session, customer_id: str) -> list[PaymentMethodStatus]:
    rows = db.query(PaymentMethodRecord).filter(PaymentMethodRecord.customer_id == customer_id).all()
    return [
        PaymentMethodStatus(
            customer_id=r.customer_id, label=r.label, status=r.status, last_decline_code=r.last_decline_code
        )
        for r in rows
    ]


class SubscriptionInfo(BaseModel):
    id: str
    plan_amount: float
    currency: str
    status: str
    failed_attempt_count: int


def get_subscription(db: Session, customer_id: str) -> SubscriptionInfo:
    sub = db.query(Subscription).filter(Subscription.customer_id == customer_id).first()
    if not sub:
        raise NotFoundError(f"subscription for {customer_id} not found")
    return SubscriptionInfo(
        id=sub.id,
        plan_amount=sub.plan_amount,
        currency=sub.currency,
        status=sub.status,
        failed_attempt_count=sub.failed_attempt_count,
    )
