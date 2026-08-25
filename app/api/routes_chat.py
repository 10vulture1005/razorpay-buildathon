"""Recovery Copilot: a context-grounded chat endpoint over the live ledger.

The copilot answers questions about cases, money at risk, policy decisions and
recovery outcomes. Its context is assembled fresh from the DB on every request
(metrics + funnel + open cases + recent audit events, plus the full trail of a
focused case) — it never answers from memory of a previous session.

Safety property: chat output is display-only. It never feeds the policy
engine or any write tool; the copilot cannot execute actions on its own.

The one deliberate exception, still human-gated end to end: when the operator
asks the copilot to email someone, the model may attach an EMAIL DRAFT to its
reply (email_draft). The draft is rendered as an editable card in the UI and
is only transmitted by POST /chat/send-email — an explicit, confirmed call by
the human, audited with actor="human". The copilot itself never sends mail.
"""
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError, model_validator
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


class ChartSeries(BaseModel):
    name: str = Field(max_length=48)
    data: list[float] = Field(max_length=31)


class ChartSpec(BaseModel):
    """Display-only chart payload the copilot may attach to an answer."""
    type: Literal["bar", "line", "pie"]
    title: str = Field(max_length=120)
    unit: str | None = Field(default=None, max_length=12)
    labels: list[str] = Field(min_length=2, max_length=31)
    series: list[ChartSeries] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def _series_match_labels(self):
        for s in self.series:
            if len(s.data) != len(self.labels):
                raise ValueError(
                    f"series '{s.name}' has {len(s.data)} points but there are "
                    f"{len(self.labels)} labels")
        if self.type == "pie" and len(self.series) > 1:
            raise ValueError("pie charts support exactly one series")
        return self


def _coerce_chart(raw) -> ChartSpec | None:
    """Lenient repair of model-emitted chart payloads. Charts are display-only,
    so minor defects (length mismatch, non-numeric points, extra pie series)
    are coerced instead of discarding the whole reply to the text-only path."""
    if not isinstance(raw, dict):
        return None
    ctype = raw.get("type")
    if ctype not in ("bar", "line", "pie"):
        return None
    unit_raw = raw.get("unit")
    unit = str(unit_raw)[:12] if isinstance(unit_raw, str) and unit_raw.strip() else None
    labels_raw = raw.get("labels")
    if not isinstance(labels_raw, list):
        return None
    labels = [str(x) for x in labels_raw if x is not None][:31]
    if len(labels) < 2:
        return None
    n = len(labels)
    series_out: list[dict] = []
    for s in (raw.get("series") or [])[:4]:
        if not isinstance(s, dict):
            continue
        vals = s.get("data")
        if not isinstance(vals, list):
            continue
        nums: list[float] = []
        for v in vals[:n]:
            try:
                nums.append(float(v))
            except (TypeError, ValueError):
                nums.append(0.0)
        while len(nums) < n:  # pad short arrays; truncate long ones
            nums.append(0.0)
        series_out.append({"name": str(s.get("name") or "series")[:48], "data": nums})
    if not series_out:
        return None
    if ctype == "pie":
        series_out = series_out[:1]
    try:
        return ChartSpec(type=ctype, title=str(raw.get("title") or "Chart")[:120],
                         unit=unit, labels=labels, series=series_out)
    except ValidationError:
        return None


class EmailDraft(BaseModel):
    """An email the copilot DRAFTED for the operator. Display-only until the
    operator explicitly confirms via POST /chat/send-email."""
    to: str = Field(max_length=254)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=8000)

    @model_validator(mode="after")
    def _sane_recipient(self):
        # Match the delivery adapter's own minimal validation — full RFC-5322
        # parsing is the provider's job, this just catches model garbage.
        if "@" not in self.to or " " in self.to.strip():
            raise ValueError(f"'to' is not a plausible address: {self.to!r}")
        return self


def _coerce_email_draft(raw) -> EmailDraft | None:
    """Lenient repair of a model-emitted draft. A draft that fails strict
    validation degrades to None (with the prose answer intact) instead of
    discarding the whole reply."""
    if not isinstance(raw, dict):
        return None
    try:
        return EmailDraft(
            to=str(raw.get("to") or "").strip()[:254],
            subject=str(raw.get("subject") or "").strip()[:200],
            body=str(raw.get("body") or "").strip()[:8000],
        )
    except ValidationError:
        return None


class ChatReply(BaseModel):
    answer: str  # display-only output; never branched on downstream
    chart: ChartSpec | None = None  # optional visual companion, display-only
    email_draft: EmailDraft | None = None  # human-gated: sent only on explicit confirm

    @model_validator(mode="before")
    @classmethod
    def _repair_chart(cls, data):
        """Coerce the chart BEFORE strict validation so a malformed chart
        degrades to chart=None instead of failing the entire reply."""
        if isinstance(data, dict):
            # Some models emit the bare chart object instead of the wrapper.
            if "answer" not in data and {"type", "labels"} <= set(data):
                title = str(data.get("title") or "the requested breakdown")
                data = {"answer": f"Chart: {title}.", "chart": data}
            # Same for a bare email draft emitted without the answer wrapper.
            if "answer" not in data and {"to", "subject", "body"} <= set(data):
                data = {"answer": "Here is the draft — review, edit and confirm.",
                        "email_draft": data}
            if data.get("chart") is not None:
                data["chart"] = _coerce_chart(data["chart"])
            # Same lenient path for the email draft: repair or drop it.
            if data.get("email_draft") is not None:
                data["email_draft"] = _coerce_email_draft(data["email_draft"])
        return data


class ChatReplyTextOnly(BaseModel):
    """Degraded fallback schema — no chart field, so the llm.py chart prompt
    hints never apply and the model only has to produce plain prose."""

    answer: str = Field(max_length=4000)


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
    chart = ChartSpec(
        type="bar",
        title="Case pipeline by status",
        unit="cases",
        labels=["active", "escalated", "stopped", "recovered"],
        series=[{"name": "cases",
                 "data": [m.get("active_cases", 0), m.get("escalated_cases", 0),
                          m.get("stopped_cases", 0), m.get("recovered_cases", 0)]}],
    )
    rej = ctx.get("recent_policy_rejections") or []
    if rej:
        lines.append("Recent policy blocks: " +
                     "; ".join(f"{r['case_id']}: {r['reason']}" for r in rej[:3]))
    focus = ctx.get("focused_case")
    if focus:
        lines.append(f"Focused case {focus['case_id']} ({focus['status']}): "
                     f"₹{focus['amount_at_risk']:,.0f}, {focus['attempt_count']} attempts.")
    lines.append("(LLM_PROVIDER=mock heuristic briefing — configure OpenRouter for full answers.)")
    return ChatReply(answer="\n".join(lines), chart=chart)


@router.post("/chat", dependencies=[Depends(require_scope("read"))])
def chat(req: ChatRequest, db: Session = Depends(get_session)):
    context = _briefing(db, req.case_id)
    prompt = {"context": context,
              "messages": [m.model_dump() for m in req.messages]}
    try:
        reply = llm_mod.call_structured(ChatReply, prompt)
    except llm_mod.StructuredOutputFailure:
        if llm_mod._CLIENT.__class__.__name__ == "MockLLM":
            reply = _mock_reply(prompt)
        else:
            # Degrade gracefully rather than 500: retry as plain text (no
            # chart). The chat is display-only, so a chart-less answer is a
            # strictly better failure mode than an error banner.
            try:
                plain = llm_mod.call_structured(ChatReplyTextOnly, prompt)
            except llm_mod.StructuredOutputFailure:
                raise
            reply = ChatReply(answer=plain.answer, chart=None)
    return {"answer": reply.answer,
            "chart": reply.chart.model_dump() if reply.chart else None,
            "email_draft": reply.email_draft.model_dump() if reply.email_draft else None,
            "context_generated_at": context["generated_at"]}


class SendEmailRequest(BaseModel):
    """The operator-confirmed email. The copilot only ever PROPOSES a draft;
    this endpoint is the single place mail leaves the chat, on an explicit
    human confirmation of (possibly edited) content."""
    to: str = Field(max_length=254)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=8000)
    case_id: str | None = Field(default=None, max_length=64)


@router.post("/chat/send-email", dependencies=[Depends(require_scope("run"))])
def send_copilot_email(req: SendEmailRequest, db: Session = Depends(get_session)):
    from app.integrations.email import EmailDeliveryError, send_email

    draft = _coerce_email_draft(req.model_dump())
    if draft is None:
        raise HTTPException(422, "invalid recipient address")

    if req.case_id and not db.get(Case, req.case_id):
        raise HTTPException(404, f"case {req.case_id} not found")

    try:
        result = send_email(draft.to, draft.subject, draft.body)
    except EmailDeliveryError as e:
        # Adapter failures are delivery problems, not client bugs: surface
        # them so the UI can let the operator edit/retry.
        status = 503 if e.retryable else 400
        raise HTTPException(status, f"email delivery failed: {e}") from e

    db.add(AuditLog(case_id=req.case_id, event_type="copilot_email_sent",
                    actor="human",
                    payload={"to": draft.to, "subject": draft.subject,
                             "provider": result["provider"],
                             "provider_message_id": result.get("provider_message_id")}))
    db.commit()
    return {"status": "sent", "to": draft.to,
            "provider": result["provider"],
            "provider_message_id": result.get("provider_message_id")}
