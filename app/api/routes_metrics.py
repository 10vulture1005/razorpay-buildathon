from fastapi import APIRouter, Depends
from sqlalchemy import func as safunc
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.db.tables import AuditLog, Case, CaseStatus, PaymentEvent, PolicyDecisionRecord
from app.models.domain import RecoveryMetrics
from app.policy.policy_engine import load_policy_config
from app.security.auth import require_scope

router = APIRouter()


def _humanize(r: AuditLog) -> dict:
    p = r.payload or {}
    desc = {
        "ingest_case": "Case ingested into the recovery pipeline.",
        "state_transition": f"State changed {p.get('from')} → {p.get('to')}.",
        "diagnosis": f"Agent diagnosed likely cause: {p.get('likely_cause')} (confidence {p.get('confidence')}).",
        "action_selected": f"Agent selected action: {p.get('action')} (net expected value ₹{p.get('net_expected_value', 0)}).",
        "policy_check": ("Policy APPROVED action." if p.get("allowed")
                         else f"Policy REJECTED action '{p.get('proposed_action')}': {p.get('reason')}."),
        "send_reminder": f"Reminder sent via {p.get('channel')} ({p.get('status')}).",
        "send_payment_link": f"Payment link sent via {p.get('channel')} ({p.get('status')}).",
        "escalate_to_human": f"Escalated to human: {p.get('reason')}. Ticket {p.get('ticket_id')}.",
        "mark_recovered": f"Recovery confirmed: ₹{p.get('amount')} verified by {p.get('verified_by')}.",
        "payment_detected": f"Incoming payment detected (₹{p.get('amount_paid')}).",
        "retry_succeeded": "Payment retry succeeded.",
        "stopping_rules_check": (("Stopping rules tripped: " + p["reason"]) if p.get("exhausted")
                                 else "Stopping rules checked — within limits."),
        "loop_bound_hit": "Attempt budget exhausted before re-entry; escalating.",
    }.get(r.event_type, r.event_type)
    return {
        "seq": r.id,
        "case_id": r.case_id,
        "timestamp": r.created_at.isoformat() if r.created_at else None,
        "actor": r.actor,
        "event_type": r.event_type,
        "description": desc,
        "payload": p,
        # agent's STATED rationale — audit trail only, never the decision input
        "agent_reasoning": r.reasoning,
    }


@router.get("/cases/{case_id}/audit", dependencies=[Depends(require_scope("read"))])
def get_audit(case_id: str, db: Session = Depends(get_session)):
    rows = (db.query(AuditLog).filter(AuditLog.case_id == case_id)
            .order_by(AuditLog.created_at, AuditLog.id).all())
    return {"case_id": case_id, "events": [_humanize(r) for r in rows]}


@router.get("/metrics/recovery", response_model=RecoveryMetrics, dependencies=[Depends(require_scope("read"))])
def recovery_metrics(db: Session = Depends(get_session)):
    terminal = [CaseStatus.ESCALATED, CaseStatus.STOPPED, CaseStatus.RECOVERED]
    total = db.query(Case).count()
    active = db.query(Case).filter(~Case.status.in_(terminal)).count()
    recovered_rows = db.query(Case).filter(Case.status == CaseStatus.RECOVERED).all()
    recovered_ids = [c.id for c in recovered_rows]
    recovered_amount = 0.0
    if recovered_ids:
        rec_events = db.query(AuditLog).filter(
            AuditLog.case_id.in_(recovered_ids), AuditLog.event_type == "mark_recovered").all()
        recovered_amount = sum((e.payload or {}).get("amount", 0) for e in rec_events)
    at_risk = db.query(safunc.sum(Case.amount_at_risk)).scalar() or 0.0
    escalated = db.query(Case).filter(Case.status == CaseStatus.ESCALATED).count()
    stopped = db.query(Case).filter(Case.status == CaseStatus.STOPPED).count()
    automated = max(total - escalated - len(recovered_rows) - stopped, 0)
    return RecoveryMetrics(
        revenue_at_risk=at_risk, recovered_amount=recovered_amount,
        recovery_rate=(recovered_amount / at_risk) if at_risk else 0,
        active_cases=active, total_cases=total, escalated_cases=escalated,
        stopped_cases=stopped, recovered_cases=len(recovered_rows),
        automation_rate=(automated / total) if total else 0,
        escalation_rate=(escalated / total) if total else 0)


@router.get("/metrics/funnel", dependencies=[Depends(require_scope("read"))])
def funnel(db: Session = Depends(get_session)):
    load_policy_config()  # validates config loads
    total_at_risk = db.query(safunc.sum(Case.amount_at_risk)).scalar() or 0
    eligible_ids = {
        r[0] for r in db.query(AuditLog.case_id).filter(AuditLog.event_type == "policy_check").all()
        if r[0]
    }
    approved_ids = {
        r[0] for r in db.query(AuditLog.case_id, AuditLog.payload)
        .filter(AuditLog.event_type == "policy_check").all()
        if r[1] and r[1].get("allowed")
    }
    actions = db.query(PolicyDecisionRecord).count()
    recovered = db.query(Case).filter(Case.status == CaseStatus.RECOVERED).count()
    escalated = db.query(Case).filter(Case.status == CaseStatus.ESCALATED).count()
    return {"revenue_at_risk": total_at_risk,
            "total_cases": len(eligible_ids),
            "eligible_cases": len(approved_ids),
            "automated_actions": actions,
            "successful_recovery": recovered,
            "human_escalation": escalated}


@router.get("/metrics/activity", dependencies=[Depends(require_scope("read"))])
def activity(limit: int = 50, db: Session = Depends(get_session)):
    rows = (db.query(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(limit).all())
    return {"events": [_humanize(r) for r in reversed(rows)]}


ACTION_EVENTS = ("send_reminder", "send_payment_link", "record_promise_to_pay",
                 "retry_payment", "update_payment_method_prompt", "send_dunning_email")


@router.get("/metrics/timeline", dependencies=[Depends(require_scope("read"))])
def timeline(days: int = 14, db: Session = Depends(get_session)):
    """Per-day series for dashboard graphs: automated actions, policy rejections,
    verified recovered amount. Computed live from the audit ledger."""
    from datetime import timedelta

    days = max(1, min(days, 90))
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (db.query(AuditLog).filter(AuditLog.created_at >= cutoff)
            .order_by(AuditLog.created_at).all())

    buckets: dict[str, dict] = {}
    for r in rows:
        day = (r.created_at.date().isoformat() if r.created_at else "unknown")
        b = buckets.setdefault(day, {"date": day, "actions": 0, "rejections": 0,
                                     "recovered_amount": 0.0})
        if r.event_type in ACTION_EVENTS:
            b["actions"] += 1
        elif r.event_type == "policy_check" and not (r.payload or {}).get("allowed"):
            b["rejections"] += 1
        elif r.event_type == "mark_recovered":
            b["recovered_amount"] += float((r.payload or {}).get("amount", 0) or 0)

    # fill empty leading days so the chart never lies about gaps
    today = datetime.now(timezone.utc).date()
    series = []
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        base = {"date": d, "actions": 0, "rejections": 0, "recovered_amount": 0.0}
        if d in buckets:
            base.update(buckets[d])
        series.append(base)
    payments = db.query(PaymentEvent).count()
    return {"days": series, "payment_events_total": payments}
