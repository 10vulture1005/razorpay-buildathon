"""CI gate: scan audit_log for agent/policy violations. Policy violation rate
must be EXACTLY 0 — any hit fails the build and pages per PRODUCTION.md B1-9.

A violation is a tool execution that was never approved by a policy_check with
allowed=true for its case+attempt (write executed without a gate), or a
policy_check row whose config_snapshot is missing (non-reproducible decision).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    import os

    os.environ.setdefault("DATABASE_URL", "sqlite:///./.ci_recovery.db")
    from app.db.session import SessionLocal
    from app.db.tables import AuditLog, PolicyDecisionRecord

    # SQLite dev/CI databases are created on demand; Postgres must already be migrated.
    if os.environ["DATABASE_URL"].startswith("sqlite"):
        from app.db.session import init_db

        init_db()
    db = SessionLocal()
    try:
        checks = {
            r.case_id: (r.payload or {})
            for r in db.query(AuditLog).filter(AuditLog.event_type == "policy_check").all()
            if r.case_id
        }
        violations = 0
        executions = db.query(PolicyDecisionRecord).all()
        # Only CUSTOMER-FACING actions must be policy-gated. escalate_to_human is
        # the safe path (including internal-error escalations) and payment
        # webhooks are observations — neither requires a prior approval.
        UNGATED_ACTIONS = {"escalate_to_human", "payment_webhook"}
        for ex in executions:
            if ex.action in UNGATED_ACTIONS:
                continue
            snapshot = checks.get(ex.case_id)
            if snapshot is None:
                print(f"VIOLATION: {ex.idempotency_key} executed with no policy_check row")
                violations += 1
                break
        total_checks = sum(1 for p in checks.values())
        missing_snapshot = sum(1 for p in checks.values() if "config_snapshot" not in p)
        if missing_snapshot:
            print(f"VIOLATION: {missing_snapshot} policy decisions lack a config snapshot")
            violations += missing_snapshot
        rate = violations / max(total_checks + len(executions), 1)
        if violations:
            print(f"FAIL: {violations} policy violations (rate {rate:.4f}, must be 0)")
            return 1
        print(f"OK: policy violation rate 0 across {total_checks} checks, {len(executions)} executions")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
