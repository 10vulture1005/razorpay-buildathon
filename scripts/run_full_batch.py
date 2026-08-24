"""Full synthetic-batch run: seed (if needed), run every receivable case through
the graph, print the eval report.

Usage: python -m scripts.run_full_batch [--fresh]

R0 eval integrity: this script NO LONGER pre-inserts payment rows for
clean-payer cases — that pre-baked "recovery" into the numbers. Self-cure is
now a property of the synthetic case itself (`cases.will_self_cure` +
`self_cure_day_offset`, set at Phase-0 generation time) and is applied
identically by the eval harness to every such case regardless of what the
agent did or which experiment arm it sits in.

Synthetic evaluation runs are pinned to mid-day IST so the contact-hours
compliance rule (real in production) doesn't park the whole batch when the
script happens to run at night.
"""
import json
import sys
from datetime import datetime, timezone

from app.db.session import SessionLocal, init_db
from app.db.tables import Case
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
