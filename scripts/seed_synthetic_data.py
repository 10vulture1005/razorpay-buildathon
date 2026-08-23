"""Synthetic data generator: cases across 6 archetypes.

Archetypes are labeled in customers.notes / case notes for Phase 5 eval labeling.
Run: python -m scripts.seed_synthetic_data                    # default 4/archetype = 24
     python -m scripts.seed_synthetic_data --per-archetype 500 --append   # 3000-case load test
"""
import argparse
import random
from datetime import datetime, timedelta, timezone

from app.db.session import SessionLocal, init_db
from app.db.tables import (
    Case,
    CaseStatus,
    CaseType,
    CommunicationMessage,
    Customer,
    Invoice,
    PaymentMethodRecord,
    Promise,
    Subscription,
)

random.seed(42)
NOW = datetime.now(timezone.utc)

ARCHETYPES = [
    "clean_payer",
    "serial_promise_breaker",
    "disputed_invoice",
    "high_value_low_risk",
    "low_value_high_risk",
    "opted_out",
]


def make_case(session, archetype: str, idx: int):
    amount = random.choice([500, 2_500, 12_000, 45_000, 120_000, 350_000, 800_000])
    if archetype == "high_value_low_risk":
        amount = random.choice([250_000, 500_000, 1_200_000])
    if archetype == "low_value_high_risk":
        amount = random.choice([500, 1_200, 3_400])
    days_overdue = random.choice([1, 2, 4, 6, 9]) if archetype != "disputed_invoice" else 5

    cust = Customer(
        id=f"cust_{archetype}_{idx}",
        name=f"{archetype.title().replace('_', ' ')} Co {idx}",
        # Synthetic eval-fixture address (RFC 2606 .example TLD) — never deliverable.
        email=f"billing+{archetype}-{idx}@example.test",
        segment=random.choice(["smb", "mid_market", "enterprise"]),
        opted_out=(archetype == "opted_out"),
        notes=archetype,
    )
    session.add(cust)

    inv = Invoice(
        id=f"inv_{archetype}_{idx}",
        customer_id=cust.id,
        amount=float(amount),
        currency="INR",
        due_date=NOW - timedelta(days=days_overdue + 30),
    )
    session.add(inv)
    session.flush()  # Postgres enforces FKs; ensure parent rows exist first

    case = Case(
        id=f"case_{archetype}_{idx}",
        invoice_id=inv.id,
        customer_id=cust.id,
        case_type=CaseType.RECEIVABLE,
        status=CaseStatus.NEW,
        detected_at=NOW - timedelta(days=days_overdue),
        amount_at_risk=float(amount),
        messages_sent_date=None,
    )
    session.add(case)

    # Payment history: prior invoices with on-time/late pattern.
    on_time_rate = {
        "clean_payer": 0.95,
        "serial_promise_breaker": 0.6,
        "disputed_invoice": 0.8,
        "high_value_low_risk": 0.98,
        "low_value_high_risk": 0.4,
        "opted_out": 0.75,
    }[archetype]
    n_hist = random.randint(4, 8)
    late_days_total = 0
    for h in range(n_hist):
        was_late = random.random() > on_time_rate
        days_late = random.randint(1, 20) if was_late else 0
        late_days_total += days_late
        session.add(
            Invoice(
                id=f"invhist_{cust.id}_{h}",
                customer_id=cust.id,
                amount=float(random.choice([10_000, 50_000, 100_000])),
                currency="INR",
                due_date=NOW - timedelta(days=90 + h * 30),
            )
        )

    if archetype == "serial_promise_breaker":
        for p in range(3):
            session.add(
                Promise(
                    customer_id=cust.id,
                    promised_date=NOW - timedelta(days=30 - p * 7),
                    kept=False,
                )
            )
    elif random.random() < 0.4:
        session.add(Promise(customer_id=cust.id, promised_date=NOW + timedelta(days=3), kept=None))

    if archetype == "disputed_invoice":
        session.add(
            CommunicationMessage(
                case_id=case.id,
                direction="inbound",
                channel="email",
                body=(
                    f"We dispute invoice {inv.id}: services billed were not delivered as agreed. "
                    "Do not contact us about this amount until the dispute is resolved."
                ),
            )
        )
    if archetype == "clean_payer" and idx == 0:
        session.add(
            CommunicationMessage(
                case_id=case.id,
                direction="inbound",
                channel="email",
                body="Sorry for the delay, processing payment this week.",
            )
        )

    # Failed-payment extension data (Phase 7): subscriptions + payment methods.
    if idx <= 1 or random.random() < 0.1:
        sub = Subscription(
            id=f"sub_{archetype}_{idx}", customer_id=cust.id, plan_amount=float(min(amount, 99_999))
        )
        pm_status = "active"
        decline = None
        if archetype in ("low_value_high_risk", "opted_out"):
            pm_status, decline = ("expired" if idx == 0 else "invalid"), "card_expired"
        elif idx == 1:
            pm_status, decline = "active", "insufficient_funds"
        session.add(sub)
        session.add(
            PaymentMethodRecord(
                id=f"pm_{archetype}_{idx}",
                customer_id=cust.id,
                label="card_primary",
                status=pm_status,
                last_decline_code=decline,
            )
        )


def seed(per_archetype: int = 4, append: bool = False):
    init_db()
    session = SessionLocal()
    existing = session.query(Case).count()
    if existing > 0 and not append:
        print(f"Already seeded ({existing} cases); skipping. Use --append to add more.")
        return
    start_idx = 0
    if append and existing > 0:
        # continue the id sequence so ids stay unique across append runs
        for arch in ARCHETYPES:
            prefix = f"cust_{arch}_"
            max_idx = max(
                (int(c.id[len(prefix):]) for c in session.query(Customer)
                 .filter(Customer.id.like(f"{prefix}%")).all()),
                default=-1,
            )
            start_idx = max(start_idx, max_idx + 1)
    total = len(ARCHETYPES) * per_archetype
    print(f"Seeding {total} cases "
          f"({len(ARCHETYPES)} archetypes x {per_archetype}, idx offset {start_idx})...")
    for arch in ARCHETYPES:
        for i in range(start_idx, start_idx + per_archetype):
            make_case(session, arch, i)
        session.flush()
    session.commit()
    from collections import Counter

    counts = Counter(c.notes for c in session.query(Customer).all())
    print("Seeded:", dict(counts))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--per-archetype", type=int, default=4,
                        help="cases per archetype (default 4 = the 24-case eval batch)")
    parser.add_argument("--append", action="store_true",
                        help="add cases even if the DB already has data")
    args = parser.parse_args()
    seed(per_archetype=args.per_archetype, append=args.append)
