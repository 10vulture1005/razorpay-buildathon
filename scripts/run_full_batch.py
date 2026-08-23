"""Full synthetic-batch run: seed (if needed), insert verified payments for
clean-payer archetype cases, run every receivable case through the graph,
print the eval report.

Usage: python -m scripts.run_full_batch [--fresh]

Synthetic evaluation runs are pinned to mid-day IST so the contact-hours
compliance rule (real in production) doesn't park the whole batch when the
script happens to run at night.
"""
import json
import sys
from datetime import datetime, timezone

from app.db.session import SessionLocal, init_db
from app.db.tables import Case, Customer, PaymentEvent
from app.evals.eval_runner import evaluate


def _pin_business_hours_clock():
    from app.policy import policy_engine

    policy_engine._default_now = lambda: datetime(2026, 8, 20, 5, 0, tzinfo=timezone.utc)


def main(fresh=False):
    _pin_business_hours_clock()
    init_db()
    db = SessionLocal()
    try:
        if fresh:
            from app.db.tables import Base
            from app.db.session import engine

            Base.metadata.drop_all(engine)
            init_db()
        if db.query(Case).count() == 0:
            from scripts.seed_synthetic_data import seed

            seed()

        # clean payers actually pay once reminded → verified payment rows exist
        clean = (
            db.query(Case)
            .join(Customer, Case.customer_id == Customer.id)
            .filter(Customer.notes == "clean_payer")
            .all()
        )
        existing_invoices = {p.invoice_id for p in db.query(PaymentEvent).all()}
        for c in clean:
            if c.invoice_id not in existing_invoices and c.status not in ("RECOVERED",):
                db.add(PaymentEvent(invoice_id=c.invoice_id, amount_paid=c.amount_at_risk,
                                   source="synthetic_batch"))
        db.commit()

        ids = [c.id for c in db.query(Case).all()
               if (c.case_type.value if hasattr(c.case_type, "value") else c.case_type) == "receivable"]
        report = evaluate(db, ids)

        print(json.dumps({
            k: v for k, v in report.items() if k != "outcomes"
        }, indent=2, default=str))
    finally:
        db.close()


if __name__ == "__main__":
    main(fresh="--fresh" in sys.argv)
