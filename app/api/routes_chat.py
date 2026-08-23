"""Recovery Copilot: a context-grounded chat endpoint over the live ledger.

The copilot answers questions about cases, money at risk, policy decisions and
recovery outcomes. Its context is assembled fresh from the DB on every request
(metrics + funnel + open cases + recent audit events, plus the full trail of a
focused case) — it never answers from memory of a previous session.

Safety property unchanged: chat output is display-only. It never feeds the
policy engine or any write tool; the copilot cannot execute actions.
"""
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func as safunc
from sqlalchemy.orm import Session

from app.agent import llm as llm_mod
from app.api.routes_metrics import _humanize
from app.db.session import get_session
from app.db.tables import AuditLog, Case, CaseStatus
from app.security.auth import require_scope

router = APIRouter()


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=4000)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(max_length=24)
    case_id: str | None = Field(default=None, max_length=64)


class ChatReply(BaseModel):
    answer: str  # display-only output; never branched on downstream


def _briefing(db: Session, case_id: str | None) -> dict:
    """Fresh DB snapshot — the complete context the copilot may speak from."""
    from app.api.routes_metrics import funnel, recovery_metrics

    ctx: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "metrics": recovery_metrics(db).model_dump(),
        "funnel": funnel(db),
    }

    terminal = [CaseStatus.ESCALATED, CaseStatus.STOPPED, CaseStatus.RECOVERED]
    open_cases = (db.query(Case)
                  .filter(~Case.status.in_(terminal))
                  .order_by(Case.amount_at_risk.desc()).limit(10).all())
    ctx["top_open_cases"] = [
        {"case_id": c.id, "status": c.status.value, "amount_at_risk": c.amount_at_risk,
         "attempts": c.attempt_count, "last_action": c.last_action}
        for c in open_cases
    ]
    escalated = (db.query(Case).filter(Case.status == CaseStatus.ESCALATED)
                 .order_by(Case.updated_at.desc()).limit(5).all())
    ctx["escalated_cases"] = [
        {"case_id": c.id, "amount_at_risk": c.amount_at_risk} for c in escalated]

    # JSON path queries differ across SQLite/Postgres — filter in Python instead.
    rows = (db.query(AuditLog).filter(AuditLog.event_type == "policy_check")
            .order_by(AuditLog.id.desc()).limit(80).all())
    rejections = [r for r in rows if not (r.payload or {}).get("allowed")][:8]
    ctx["recent_policy_rejections"] = [
        {"case_id": r.case_id,
         "reason": (r.payload or {}).get("reason"),
         "blocked_action": (r.payload or {}).get("proposed_action")}
        for r in rejections
    ]

    recent = (db.query(AuditLog).order_by(AuditLog.id.desc()).limit(30).all())
    ctx["recent_activity"] = [_humanize(r) for r in reversed(recent)]

    if case_id:
        case = db.get(Case, case_id)
        if not case:
            raise HTTPException(404, f"case {case_id} not found")
        trail = (db.query(AuditLog).filter(AuditLog.case_id == case_id)
                 .order_by(AuditLog.created_at, AuditLog.id).all())
        ctx["focused_case"] = {
            "case_id": case.id, "status": case.status.value,
            "amount_at_risk": case.amount_at_risk,
            "attempt_count": case.attempt_count,
            "audit_trail": [_humanize(r) for r in trail],
        }
    return ctx


def _mock_reply(prompt: dict) -> ChatReply:
    """Dev/test stand-in: deterministic briefing computed FROM the context only."""
    ctx = prompt.get("context", {})
    m = ctx.get("metrics", {})
    lines = [
        f"Snapshot at {ctx.get('generated_at', 'now')}:",
        f"Revenue at risk ₹{m.get('revenue_at_risk', 0):,.0f} across "
        f"{m.get('total_cases', 0)} cases; recovered ₹{m.get('recovered_amount', 0):,.0f} "
        f"({m.get('recovery_rate', 0) * 100:.1f}%).",
        f"Active {m.get('active_cases', 0)} · escalated {m.get('escalated_cases', 0)} · "
        f"stopped {m.get('stopped_cases', 0)}.",
    ]
    rej = ctx.get("recent_policy_rejections") or []
    if rej:
        lines.append("Recent policy blocks: " +
                     "; ".join(f"{r['case_id']}: {r['reason']}" for r in rej[:3]))
    focus = ctx.get("focused_case")
    if focus:
        lines.append(f"Focused case {focus['case_id']} ({focus['status']}): "
                     f"₹{focus['amount_at_risk']:,.0f}, {focus['attempt_count']} attempts.")
    lines.append("(LLM_PROVIDER=mock heuristic briefing — configure OpenRouter for full answers.)")
    return ChatReply(answer="\n".join(lines))


@router.post("/chat", dependencies=[Depends(require_scope("read"))])
def chat(req: ChatRequest, db: Session = Depends(get_session)):
    context = _briefing(db, req.case_id)
    prompt = {"context": context,
              "messages": [m.model_dump() for m in req.messages]}
    try:
        reply = llm_mod.call_structured(ChatReply, prompt)
    except llm_mod.StructuredOutputFailure:
        if llm_mod._CLIENT.__class__.__name__ != "MockLLM":
            raise
        reply = _mock_reply(prompt)
    return {"answer": reply.answer, "context_generated_at": context["generated_at"]}
