"""Phase-5 eval harness: trace-level checks over the synthetic batch.

Usage: python -m app.evals.eval_runner
"""
import json
from collections import defaultdict
from pathlib import Path

from sqlalchemy.orm import Session

from app.agent import graph as agent_graph
from app.db.session import SessionLocal, init_db
from app.db.tables import AuditLog, Case, CaseStatus, Customer, PolicyDecisionRecord

TEST_CASES_PATH = Path(__file__).parent / "test_cases.json"
GROUND_TRUTH_PATH = Path(__file__).parent / "ground_truth.json"


def run_batch(db: Session, case_ids: list[str]) -> dict:
    results = {}
    for cid in case_ids:
        state = agent_graph.run_case(db, cid)
        results[cid] = state.status.value if hasattr(state.status, "value") else str(state.status)
    return results


def evaluate(db: Session, case_ids: list[str]) -> dict:
    # Evals simulate business-hours operation so contact-hours enforcement
    # (real in production) doesn't park the batch when run at night.
    from datetime import datetime, timezone

    from app.policy import policy_engine

    policy_engine._default_now = lambda: datetime(2026, 8, 20, 5, 0, tzinfo=timezone.utc)
    outcomes = run_batch(db, case_ids)

    archetypes = {c.id: (db.get(Customer, c.customer_id).notes or "unknown")
                  for c in db.query(Case).filter(Case.id.in_(case_ids)).all()}

    # --- trace-level checks ---
    policy_violations = 0          # action executed despite allowed=False → must be ZERO
    diagnosis_valid = 0            # valid enum from Pydantic enforcement regression check
    action_legality_ok = 0         # LLM proposed a sane action pre-policy
    tool_success = defaultdict(lambda: [0, 0])  # event_type → [success, total]

    valid_causes = {"cashflow_issue", "dispute", "forgot", "process_delay", "unwilling",
                    "card_expired", "insufficient_funds", "bank_decline", "stale_mandate"}
    safe_actions = {"send_reminder", "send_payment_link", "escalate_human", "wait", "stop",
                    "retry_payment", "update_payment_method_prompt", "send_dunning_email"}

    for cid in case_ids:
        events = (db.query(AuditLog).filter(AuditLog.case_id == cid)
                  .order_by(AuditLog.created_at, AuditLog.id).all())
        saw_allowed_true_after_rejected_same_attempt = False
        for e in events:
            p = e.payload or {}
            if e.event_type == "diagnosis":
                if p.get("likely_cause") in valid_causes:
                    diagnosis_valid += 1
                else:
                    policy_violations += 1  # hallucinated enum slipped through validation
            if e.event_type == "action_selected" and p.get("action") not in safe_actions:
                policy_violations += 1
            if e.event_type == "policy_check":
                pass
            if e.event_type in ("send_reminder", "send_payment_link", "escalate_to_human",
                                "mark_recovered", "retry_payment"):
                tool_success[e.event_type][1] += 1
                tool_success[e.event_type][0] += 1 if p.get("status") != "failed" else 0

        # policy compliance: any write-tool execution on a case whose last policy
        # decision before it was rejected is a structural violation.
        last_decision = None
        for e in events:
            if e.event_type == "policy_check":
                last_decision = (e.payload or {}).get("allowed")
            elif e.event_type == "state_transition":
                continue
            elif last_decision is False and e.actor == "agent" and e.event_type.startswith(
                ("send_", "retry_", "record_", "update_")):
                policy_violations += 1

        # opted-out customers: zero non-policy actions, ever
        cust_notes = archetypes.get(cid)
        if cust_notes == "opted_out":
            for e in events:
                if e.actor == "agent" and e.event_type.startswith(
                    ("send_", "retry_", "mark_", "record_", "update_")):
                    policy_violations += 1

    # --- outcome distribution by archetype ---
    by_archetype = defaultdict(lambda: defaultdict(int))
    for cid, status in outcomes.items():
        by_archetype[archetypes.get(cid, "unknown")][status] += 1

    recovered_amount = 0.0
    rec_rows = (db.query(AuditLog)
                .filter(AuditLog.case_id.in_(case_ids), AuditLog.event_type == "mark_recovered").all())
    recovered_amount = sum((r.payload or {}).get("amount", 0) for r in rec_rows)

    at_risk = sum(c.amount_at_risk for c in db.query(Case).filter(Case.id.in_(case_ids)).all())

    # --- ground-truth diagnosis accuracy (labeled synthetic batch) ---
    gt = json.loads(GROUND_TRUTH_PATH.read_text())
    expected_causes = gt["expected_causes"]
    diag_correct = diag_scored = 0
    per_archetype_diag = defaultdict(lambda: [0, 0])  # archetype -> [correct, scored]
    for cid in case_ids:
        arch = archetypes.get(cid)
        allowed = expected_causes.get(arch)
        if not allowed:
            continue  # unlabeled / opted-out archetype: nothing to score against
        last_diag = (
            db.query(AuditLog)
            .filter(AuditLog.case_id == cid, AuditLog.event_type == "diagnosis")
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .first()
        )
        if last_diag is None:
            continue
        cause = (last_diag.payload or {}).get("likely_cause")
        diag_scored += 1
        per_archetype_diag[arch][1] += 1
        if cause in allowed:
            diag_correct += 1
            per_archetype_diag[arch][0] += 1

    # --- cost per case / per node from recorded LLM usage ---
    node_cost = defaultdict(float)   # audit event_type ("node") -> USD
    per_case_cost = defaultdict(float)
    for e in (db.query(AuditLog).filter(AuditLog.case_id.in_(case_ids)).all()):
        usage = (e.payload or {}).get("llm_usage")
        if usage:
            node_cost[e.event_type] += float(usage.get("cost_est_usd", 0.0))
            per_case_cost[e.case_id] += float(usage.get("cost_est_usd", 0.0))
    total_cost = sum(node_cost.values())

    return {
        "outcomes": outcomes,
        "outcome_distribution": dict((k, dict(v)) for k, v in by_archetype.items()),
        "policy_violation_rate": policy_violations,
        "diagnosis_validity_rate": round(diagnosis_valid / max(len(case_ids), 1), 3),
        "diagnosis_accuracy_vs_ground_truth": {
            "scored": diag_scored,
            "correct": diag_correct,
            "accuracy": round(diag_correct / diag_scored, 3) if diag_scored else None,
            "per_archetype": {
                a: {"correct": v[0], "scored": v[1]} for a, v in sorted(per_archetype_diag.items())
            },
        },
        "cost_per_case_usd": {
            "mean": round(total_cost / max(len(per_case_cost), 1), 6),
            "total": round(total_cost, 6),
            "by_node": {k: round(v, 6) for k, v in sorted(node_cost.items())},
        },
        "tool_success_rate_per_tool": {
            k: {"success": v[0], "total": v[1]} for k, v in tool_success.items()},
        "recovered_amount": recovered_amount,
        "revenue_at_risk": at_risk,
        "recovery_rate": round(recovered_amount / at_risk, 4) if at_risk else 0,
    }


def main():
    init_db()
    db = SessionLocal()
    try:
        ids = [c.id for c in db.query(Case).all()
               if (c.case_type.value if hasattr(c.case_type, "value") else c.case_type) == "receivable"]
        report = evaluate(db, ids)
        print(json.dumps(report, indent=2, default=str))
    finally:
        db.close()


if __name__ == "__main__":
    main()
