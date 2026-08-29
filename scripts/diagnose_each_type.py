"""Seed one case per LikelyCause value and run the diagnose node on each.

The diagnose node's LLM structured output must produce one of the 9 LikelyCause
values defined in app.models.schemas:

    receivable:  forgot, process_delay, cashflow_issue, unwilling, dispute
    failed_pmt:  card_expired, stale_mandate, insufficient_funds, bank_decline

The mock LLM (app/agent/llm.py:MockLLM) only models 5 of the 9 branches — it's
a heuristic stand-in for tests. The cases shaped here are designed for a real
LLM: each case's `context` carries strong, distinct signals (payer history,
inbound-message language, payment-method status, decline code) that point at
exactly one target cause. Against the mock, 4 of the 9 will mismatch — that
itself is useful evidence of the gap.

Usage:
    python -m scripts.diagnose_each_type                # seed + run
    python -m scripts.diagnose_each_type --skip-seed    # only run against existing cases
    python -m scripts.diagnose_each_type --case-id case_dx_forgot_0  # one case

Each seeded case id is `case_dx_<cause>_0` so it does not collide with the
6-archetype synthetic batch (case_<archetype>_<idx>).
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone

from app.db.session import SessionLocal, engine, init_db
from app.db.tables import (
    Base,
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

NOW = datetime.now(timezone.utc)

# (target_cause, case_type, label)  — 9 cases, one per LikelyCause enum value.
# Order mirrors the LikelyCause Literal in app/models/schemas.py.
TARGET_CASES = [
    ("forgot",            "receivable",     "clean payer with strong history"),
    ("process_delay",     "receivable",     "strong history + PO/approval inbound"),
    ("cashflow_issue",    "receivable",     "moderate history + tight-cashflow inbound"),
    ("unwilling",         "receivable",     "serial promise breaker"),
    ("dispute",           "receivable",     "inbound dispute language"),
    ("card_expired",      "failed_payment", "payment_method.status=expired"),
    ("stale_mandate",     "failed_payment", "payment_method.status=invalid"),
    ("insufficient_funds","failed_payment", "decline_code=insufficient_funds"),
    ("bank_decline",      "failed_payment", "decline_code=bank_decline"),
]


def _id(cause: str) -> str:
    return f"case_dx_{cause}_0"


def _new_customer(cause: str) -> Customer:
    return Customer(
        id=f"cust_dx_{cause}_0",
        name=f"Diagnose {cause.replace('_', ' ').title()} Co",
        email=f"billing+dx-{cause}@example.test",
        segment="smb",
        opted_out=False,
        notes=f"dx_{cause}",  # distinct from archetype labels
    )


def _new_invoice(cust_id: str, cause: str) -> Invoice:
    # failed_payment cases use a small plan amount; receivable cases use a
    # mid-range overdue invoice.
    amount = 25_000.0 if cause in (
        "card_expired", "stale_mandate", "insufficient_funds", "bank_decline",
    ) else 75_000.0
    return Invoice(
        id=f"inv_dx_{cause}_0",
        customer_id=cust_id,
        amount=amount,
        currency="INR",
        due_date=NOW - timedelta(days=10),
    )


def _new_case(cust_id: str, inv_id: str, cause: str) -> Case:
    is_failed = cause in (
        "card_expired", "stale_mandate", "insufficient_funds", "bank_decline",
    )
    return Case(
        id=_id(cause),
        invoice_id=inv_id,
        customer_id=cust_id,
        case_type=CaseType.FAILED_PAYMENT if is_failed else CaseType.RECEIVABLE,
        status=CaseStatus.NEW,
        detected_at=NOW - timedelta(days=4),
        amount_at_risk=25_000.0 if is_failed else 75_000.0,
        messages_sent_date=None,
    )


def _add_history(db, cust_id: str, on_time_rate: float, late_total: int, n: int = 6):
    """On-time-rate is a property of the SYNTHETIC profile (R0): decided at
    generation time, identical regardless of agent behavior."""
    import random
    rng = random.Random(hash(cust_id) & 0xFFFFFFFF)
    for h in range(n):
        was_late = rng.random() > on_time_rate
        days_late = rng.randint(2, 12) if was_late else 0
        late_total_local = late_total // n if was_late else 0
        db.add(Invoice(
            id=f"invhist_dx_{cust_id}_{h}",
            customer_id=cust_id,
            amount=50_000.0,
            currency="INR",
            # Older than the currently-overdue invoice so read_tools excludes
            # the open case from history. 90..150 days back.
            due_date=NOW - timedelta(days=90 + h * 10),
        ))


def _shape_case(db, cause: str):
    """Per-cause signal shaping. Each block carves the customer/invoice/case
    so the diagnose prompt carries a single dominant cause signal."""
    cust = _new_customer(cause)
    inv = _new_invoice(cust.id, cause)
    case = _new_case(cust.id, inv.id, cause)
    db.add_all([cust, inv, case])
    db.flush()  # FKs need parent rows

    is_failed = cause in (
        "card_expired", "stale_mandate", "insufficient_funds", "bank_decline",
    )

    if is_failed:
        # Failed-payment cases: subscription + payment method.
        sub_status = "past_due"
        sub_failed_count = {"card_expired": 1, "stale_mandate": 2,
                            "insufficient_funds": 1, "bank_decline": 3}[cause]
        db.add(Subscription(
            id=f"sub_dx_{cause}_0",
            customer_id=cust.id,
            plan_amount=25_000.0,
            status=sub_status,
            failed_attempt_count=sub_failed_count,
        ))

        if cause == "card_expired":
            pm_status, decline = "expired", "card_expired"
        elif cause == "stale_mandate":
            pm_status, decline = "invalid", "stale_mandate"
        elif cause == "insufficient_funds":
            pm_status, decline = "active", "insufficient_funds"
        else:  # bank_decline
            pm_status, decline = "active", "bank_decline"

        db.add(PaymentMethodRecord(
            id=f"pm_dx_{cause}_0",
            customer_id=cust.id,
            label="card_primary",
            status=pm_status,
            last_decline_code=decline,
        ))
        # Failed-payment cases don't get payment-history invoices — they get
        # nothing else in the ledger. The diagnose node only inspects
        # payment_method_status + decline code for failed_payment.
        return case

    # ---- receivable cases: shape the payer profile + inbound message ----
    if cause == "forgot":
        _add_history(db, cust.id, on_time_rate=0.95, late_total=0)
        # No inbound message. Clean signal: strong history, no dispute, no
        # broken promises -> most plausible cause is "forgot".
    elif cause == "process_delay":
        _add_history(db, cust.id, on_time_rate=0.95, late_total=0)
        db.add(CommunicationMessage(
            case_id=case.id, direction="inbound", channel="email",
            body=(
                "Hi, the invoice is approved on our side. We are waiting on "
                "internal PO release from procurement; payment will be processed "
                "next week. No action needed from your end."
            ),
        ))
    elif cause == "cashflow_issue":
        _add_history(db, cust.id, on_time_rate=0.7, late_total=18)
        db.add(CommunicationMessage(
            case_id=case.id, direction="inbound", channel="email",
            body=(
                "Cash is tight this quarter. Can we split the payment into two "
                "installments over the next 30 days?"
            ),
        ))
    elif cause == "unwilling":
        # 3 broken promises + dodgy history -> serial promise breaker.
        _add_history(db, cust.id, on_time_rate=0.55, late_total=40)
        for p in range(3):
            db.add(Promise(
                case_id=case.id, customer_id=cust.id,
                promised_date=NOW - timedelta(days=30 - p * 7),
                kept=False,
            ))
    elif cause == "dispute":
        _add_history(db, cust.id, on_time_rate=0.85, late_total=8)
        db.add(CommunicationMessage(
            case_id=case.id, direction="inbound", channel="email",
            body=(
                "We dispute this invoice. The services billed were not delivered "
                "as agreed. Please do not contact us about this amount until the "
                "dispute is resolved."
            ),
        ))

    return case


def seed(append: bool = False):
    """Idempotent: re-running rebuilds the dx_* cases from scratch so their
    signal shape is exactly as documented here."""
    init_db()
    db = SessionLocal()
    try:
        if not append:
            # Wipe only the dx_* rows; leave the 6-archetype batch alone.
            db.query(PaymentMethodRecord).filter(
                PaymentMethodRecord.id.like("pm_dx_%")).delete(synchronize_session=False)
            db.query(Subscription).filter(
                Subscription.id.like("sub_dx_%")).delete(synchronize_session=False)
            db.query(CommunicationMessage).filter(
                CommunicationMessage.case_id.like("case_dx_%")).delete(synchronize_session=False)
            db.query(Promise).filter(
                Promise.case_id.like("case_dx_%")).delete(synchronize_session=False)
            db.query(Invoice).filter(Invoice.id.like("inv_dx_%")).delete(synchronize_session=False)
            db.query(Invoice).filter(Invoice.id.like("invhist_dx_%")).delete(synchronize_session=False)
            db.query(Case).filter(Case.id.like("case_dx_%")).delete(synchronize_session=False)
            db.query(Customer).filter(Customer.id.like("cust_dx_%")).delete(synchronize_session=False)
            db.commit()

        for cause, _, _ in TARGET_CASES:
            _shape_case(db, cause)
        db.commit()

        from collections import Counter
        counts = Counter(c.notes for c in db.query(Customer)
                         .filter(Customer.id.like("cust_dx_%")).all())
        print("Seeded dx cases:", dict(counts))
    finally:
        db.close()


def diagnose_one(db, case_id: str) -> dict:
    """Run load -> build_context -> diagnose for one case, return diagnosis
    payload plus the target cause (from case id) for reporting."""
    from app.agent.nodes import nodes
    from app.models.domain import CaseState

    expected = case_id.replace("case_dx_", "").rsplit("_", 1)[0]
    state = nodes.load_case_state(db, case_id)
    state = nodes.build_context(db, state)
    state = nodes.diagnose(db, state)
    db.commit()
    actual = (state.diagnosis or {}).get("likely_cause")
    confidence = (state.diagnosis or {}).get("confidence")
    return {
        "case_id": case_id,
        "expected": expected,
        "actual": actual,
        "match": actual == expected,
        "confidence": confidence,
        "diagnosis_failed": bool((state.diagnosis or {}).get("failed")),
    }


def run(case_ids: list[str] | None = None) -> list[dict]:
    db = SessionLocal()
    try:
        if case_ids is None:
            case_ids = [_id(cause) for cause, _, _ in TARGET_CASES]
        results = [diagnose_one(db, cid) for cid in case_ids]
        return results
    finally:
        db.close()


def _print_report(results: list[dict], provider: str):
    # Compact table — keeps the output readable across providers.
    name_w = max(len(r["case_id"]) for r in results)
    exp_w = max(len(r["expected"]) for r in results)
    print(f"\nLLM_PROVIDER={provider}  ({len(results)} case(s))")
    print(f"  {'case_id':<{name_w}}  {'expected':<{exp_w}}  {'actual':<22}  conf   match")
    print(f"  {'-' * name_w}  {'-' * exp_w}  {'-' * 22}  ----   -----")
    matches = 0
    for r in results:
        ok = "  OK  " if r["match"] else "  MISS"
        conf = f"{r['confidence']:.2f}" if isinstance(r["confidence"], (int, float)) else "  - "
        print(f"  {r['case_id']:<{name_w}}  {r['expected']:<{exp_w}}  "
              f"{(r['actual'] or '-'):<22}  {conf}   {ok}")
        if r["match"]:
            matches += 1
    print(f"\n{matches}/{len(results)} matched expected cause")
    if matches < len(results):
        print("(The mock LLM only models 5 of 9 LikelyCause branches — see"
              " app/agent/llm.py:MockLLM._diagnose. A real LLM is expected to"
              " hit all 9.)\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skip-seed", action="store_true",
                        help="don't (re)seed — just run diagnose on existing dx_* cases")
    parser.add_argument("--case-id", help="diagnose a single case by id")
    parser.add_argument("--append", action="store_true",
                        help="seed without wiping existing dx_* rows first")
    args = parser.parse_args()

    if not args.skip_seed:
        seed(append=args.append)

    import os
    provider = os.environ.get("LLM_PROVIDER", "mock")

    if args.case_id:
        results = run([args.case_id])
    else:
        results = run()
    _print_report(results, provider)

    # Non-zero exit when nothing matched — useful in CI once a real LLM is
    # wired up. With the mock we always exit 0 since mismatches are expected.
    if provider != "mock" and results and not all(r["match"] for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
