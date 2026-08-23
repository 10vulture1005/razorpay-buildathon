"""Run every non-terminal case through the agent graph once.

This is the production entrypoint for scheduled execution — point a cron job,
systemd timer, or task scheduler at it:

    */15 * * * *  cd /srv/recovery && .venv/bin/python -m scripts.run_due_cases

Safe to overlap with itself: each case's state machine is idempotent at the
DB level (attempt counters, idempotency-keyed tools) and cases already in a
terminal state are skipped.
"""
import sys


def main() -> int:
    from datetime import datetime, timezone

    from app.db.session import SessionLocal, init_db
    from app.db.tables import Case, CaseStatus

    init_db()
    db = SessionLocal()
    ran = 0
    try:
        pending = (
            db.query(Case)
            .filter(~Case.status.in_([CaseStatus.RECOVERED, CaseStatus.STOPPED]))
            .order_by(Case.amount_at_risk.desc())
            .all()
        )
        # import after env/config are settled
        from app.agent.graph import run_case

        for case in pending:
            before = case.status.value
            state = run_case(db, case.id)
            ran += 1
            print(
                f"{datetime.now(timezone.utc).isoformat()} "
                f"{case.id}: {before} -> {state.status.value}"
                f"{' (' + state.terminal_reason + ')' if state.terminal_reason else ''}",
                flush=True,
            )
        print(f"Done: {ran} case(s) processed.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
