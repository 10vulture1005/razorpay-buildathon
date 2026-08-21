import enum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.config import DATABASE_URL


class Base(DeclarativeBase):
    pass


class CaseStatus(str, enum.Enum):
    NEW = "NEW"
    DIAGNOSED = "DIAGNOSED"
    ACTION_SELECTED = "ACTION_SELECTED"
    EXECUTING = "EXECUTING"
    AWAITING_OUTCOME = "AWAITING_OUTCOME"
    RECOVERED = "RECOVERED"
    ESCALATED = "ESCALATED"
    STOPPED = "STOPPED"


class InvoiceStatus(str, enum.Enum):
    OPEN = "open"
    RECOVERED = "recovered"
    WRITTEN_OFF = "written_off"


class CaseType(str, enum.Enum):
    RECEIVABLE = "receivable"
    FAILED_PAYMENT = "failed_payment"


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    segment: Mapped[str | None] = mapped_column(String(64), nullable=True)
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)  # archetype label
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())
    invoices: Mapped[list["Invoice"]] = relationship(back_populates="customer_rel")
    cases: Mapped[list["Case"]] = relationship(back_populates="customer_rel")


class Invoice(Base):
    __tablename__ = "invoices"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"))
    amount: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), default="INR")
    due_date: Mapped[str] = mapped_column(DateTime(timezone=True))
    status: Mapped[InvoiceStatus] = mapped_column(
        Enum(InvoiceStatus, name="invoice_status"), default=InvoiceStatus.OPEN
    )
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())
    customer_rel: Mapped["Customer"] = relationship(back_populates="invoices")
    case: Mapped["Case | None"] = relationship(back_populates="invoice")


class Case(Base):
    __tablename__ = "cases"
    __table_args__ = (
        CheckConstraint(
            "status IN ('NEW','DIAGNOSED','ACTION_SELECTED','EXECUTING',"
            "'AWAITING_OUTCOME','RECOVERED','ESCALATED','STOPPED')",
            name="ck_case_status",
        ),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id"))
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"))
    case_type: Mapped[CaseType] = mapped_column(
        Enum(CaseType, name="case_type"), default=CaseType.RECEIVABLE
    )
    status: Mapped[CaseStatus] = mapped_column(Enum(CaseStatus, name="case_status"), index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    messages_sent_today: Mapped[int] = mapped_column(Integer, default=0)
    messages_sent_date: Mapped[str | None] = mapped_column(String(10), nullable=True)  # YYYY-MM-DD
    last_action: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_action_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_allowed_action_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)
    detected_at: Mapped[str] = mapped_column(DateTime(timezone=True))
    amount_at_risk: Mapped[float] = mapped_column(Float)
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[str] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    invoice: Mapped["Invoice"] = relationship(back_populates="case", foreign_keys=[invoice_id])
    customer_rel: Mapped["Customer"] = relationship(back_populates="cases", foreign_keys=[customer_id])
    audit_events: Mapped[list["AuditLog"]] = relationship(foreign_keys=lambda: [AuditLog.case_id])
    escalation_tickets: Mapped[list["EscalationTicket"]] = relationship()
    tool_executions: Mapped[list["PolicyDecisionRecord"]] = relationship()
    messages: Mapped[list["CommunicationMessage"]] = relationship()
    retry_results: Mapped[list["RetryEvent"]] = relationship()


class Promise(Base):
    __tablename__ = "promises"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("cases.id"), nullable=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"))
    promised_date: Mapped[str] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())
    kept: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("cases.id"), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    actor: Mapped[str] = mapped_column(
        String(16), CheckConstraint("actor IN ('agent','policy','human','system')")
    )
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PolicyDecisionRecord(Base):
    """Idempotency ledger for write tools. Unique key enforces exactly-once at DB level."""

    __tablename__ = "tool_executions"
    idempotency_key: Mapped[str] = mapped_column(String(256), primary_key=True)
    # Nullable so gateway-webhook ledger rows can record unmatched invoices.
    case_id: Mapped[str | None] = mapped_column(ForeignKey("cases.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64))
    attempt_number: Mapped[int] = mapped_column(Integer)
    result_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    executed_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EscalationTicket(Base):
    __tablename__ = "escalation_tickets"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"))
    reason: Mapped[str] = mapped_column(String(255))
    summary: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="open")
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PaymentEvent(Base):
    """A verified inbound payment against an invoice (webhook or reconciliator)."""

    __tablename__ = "payment_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id"))
    amount_paid: Mapped[float] = mapped_column(Float)
    gateway_payment_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    paid_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())
    source: Mapped[str] = mapped_column(String(64), default="webhook")
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)


class CommunicationMessage(Base):
    __tablename__ = "communication_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"))
    direction: Mapped[str] = mapped_column(String(16))  # inbound | outbound
    channel: Mapped[str] = mapped_column(String(32))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())


# Failed-payment extension tables (Phase 7): same shape discipline.
class PaymentMethodRecord(Base):
    __tablename__ = "payment_methods"
    __table_args__ = (UniqueConstraint("customer_id", "label"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"))
    label: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="active")  # active|expired|invalid
    last_decline_code: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Subscription(Base):
    __tablename__ = "subscriptions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"))
    plan_amount: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), default="INR")
    status: Mapped[str] = mapped_column(String(32), default="past_due")  # active|past_due|cancelled
    failed_attempt_count: Mapped[int] = mapped_column(Integer, default=0)


class RetryEvent(Base):
    """Verified payment-retry outcomes for the failed-payment use case."""

    __tablename__ = "retry_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"))
    succeeded: Mapped[bool] = mapped_column(Boolean)
    amount: Mapped[float] = mapped_column(Float)
    gateway_payment_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source: Mapped[str] = mapped_column(String(64), default="webhook")
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)
