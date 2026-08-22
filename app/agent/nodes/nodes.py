"""Graph nodes. One file per node group; each node is a plain function over
CaseState + DB session, so the graph runs identically in-process (FastAPI),
in tests, and in the eval runner.

Non-negotiables enforced here:
- diagnose/select_action: LLM structured output only; `reasoning` is audit-only.
- policy_check / check_stopping_rules: deterministic Python, no LLM import.
- attempt bound checked before every select_action re-entry.
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

import app.config as config
from app.agent import llm as llm_mod
from app.db.tables import AuditLog, Case, CaseStatus
from app.models.domain import CaseState, PolicyDecision, StoppingRulesDecision
from app.models.schemas import DiagnosisResult, InterventionChoice
from app.policy.policy_engine import ACTION_COSTS, PolicyEngine
from app.tools import read_tools, write_tools


def _audit(db: Session, case_id: str, event_type: str, actor: str, payload: dict, reasoning=None):
    db.add(AuditLog(case_id=case_id, event_type=event_type, actor=actor,
                    payload=payload, reasoning=reasoning))


def set_status(db: Session, case_id: str, new_status: CaseStatus):
    case = db.get(Case, case_id)
    old = case.status
    case.status = new_status
    _audit(db, case_id, "state_transition", "system",
           {"from": old.value if hasattr(old, "value") else old,
            "to": new_status.value})


def load_case_state(db: Session, case_id: str) -> CaseState:
    case = db.get(Case, case_id)
    if not case:
        raise write_tools.ToolExecutionError(f"case {case_id} not found", retryable=False)
    cust = read_tools  # noqa — tools are the only data seam for agent logic
    from app.db.tables import Customer

    opted_out = bool(db.get(Customer, case.customer_id).opted_out)
    return CaseState(
        case_id=case.id,
        invoice_id=case.invoice_id,
        customer_id=case.customer_id,
        case_type=case.case_type.value if hasattr(case.case_type, "value") else case.case_type,
        status=case.status,
        attempt_count=case.attempt_count,
        messages_sent_today=case.messages_sent_today,
        detected_at=case.detected_at,
        amount_at_risk=case.amount_at_risk,
        last_action=case.last_action,
        opted_out=opted_out,
    )


# ---- nodes ----


def ingest_case(db: Session, state: CaseState) -> CaseState:
    if db.get(Case, state.case_id).status == CaseStatus.NEW:
        set_status(db, state.case_id, CaseStatus.DIAGNOSED)  # NEW is transient; context next
        db.flush()
    return state


def build_context(db: Session, state: CaseState) -> CaseState:
    """Context-window discipline: assemble ONLY these fields — never the full
    audit log or full communication history."""
    history = read_tools.get_customer_history(db, state.customer_id)
    invoice = read_tools.get_invoice(db, state.invoice_id)
    promises = read_tools.get_past_promises(db, state.customer_id)
    messages = [
        m.model_dump(mode="json")
        for m in read_tools.get_communication_log(db, state.case_id)[-3:]  # last 3 only
    ]
    ctx = {
        "case_type": state.case_type,
        "amount_at_risk": state.amount_at_risk,
        "days_overdue": max((datetime.now(timezone.utc) - _tz(state.detected_at)).days, 0),
        "attempt_number": state.attempt_count,
        "on_time_rate": history.on_time_rate,
        "avg_days_late": history.avg_days_late,
        "broken_promise_count": history.broken_promise_count,
        "opted_out": history.opted_out,
        "invoice_due_date": invoice.due_date.isoformat(),
        "promises_open": sum(1 for p in promises if p.kept is None),
        "messages": messages,
    }
    if state.case_type == "failed_payment":
        pms = read_tools.get_payment_method_status(db, state.customer_id)
        sub = read_tools.get_subscription(db, state.customer_id)
        ctx["payment_methods"] = [pm.model_dump() for pm in pms]
        ctx["payment_method_status"] = pms[0].status if pms else "unknown"
        ctx["subscription"] = sub.model_dump()
    state.context = ctx
    return state


def _tz(dt):
    return dt if dt and dt.tzinfo else (dt.replace(tzinfo=timezone.utc) if dt else datetime.now(timezone.utc))


def diagnose(db: Session, state: CaseState) -> CaseState:
    try:
        result: DiagnosisResult = llm_mod.call_structured(DiagnosisResult, {"context": state.context})
    except llm_mod.StructuredOutputFailure:
        state.diagnosis = {"failed": True}
        state.terminal_reason = "structured_output_failure"
        set_status(db, state.case_id, CaseStatus.ESCALATED)
        _audit(db, state.case_id, "diagnose_failed", "agent",
               {"error": "structured output validation failed twice"})
        return state
    # arithmetic/branching never touches result.reasoning — audit trail only
    state.diagnosis = result.model_dump()
    state.diagnosis["prompt_version"] = llm_mod.PROMPT_VERSION
    state.diagnosis["llm_usage"] = llm_mod.get_last_usage()
    _audit(db, state.case_id, "diagnosis", "agent", state.diagnosis, reasoning=result.reasoning)
    return state


def net_expected_value(action: str, probability: float, amount: float) -> float:
    """Arithmetic happens in code, never trusted from the LLM."""
    if action in ("wait", "stop"):
        return 0.0
    expected = amount * probability
    return round(expected - ACTION_COSTS[action], 2)


def select_action(db: Session, state: CaseState) -> CaseState:
    # Explicit bound re-checked BEFORE every select_action entry (loop-back guard).
    engine = PolicyEngine()
    if state.attempt_count >= engine.config["max_retries"]:
        state.proposed_action = None
        state.terminal_reason = "max_retries_exceeded_pre_loop"
        set_status(db, state.case_id, CaseStatus.ESCALATED)
        _audit(db, state.case_id, "loop_bound_hit", "system",
               {"attempt_count": state.attempt_count})
        return state

    prompt = {
        "context": state.context,
        "attempt_number": state.attempt_count,
        "diagnosis": state.diagnosis,
    }
    try:
        choice: InterventionChoice = llm_mod.call_structured(InterventionChoice, prompt)
    except llm_mod.StructuredOutputFailure:
        state.terminal_reason = "structured_output_failure"
        set_status(db, state.case_id, CaseStatus.ESCALATED)
        _audit(db, state.case_id, "select_action_failed", "agent", {})
        return state

    payload = choice.model_dump()
    payload["prompt_version"] = llm_mod.PROMPT_VERSION
    payload["llm_usage"] = llm_mod.get_last_usage()
    amount = state.context["amount_at_risk"]
    payload["net_expected_value"] = (
        net_expected_value(choice.action, choice.expected_recovery_probability, amount)
        if choice.action not in ("wait", "stop") else 0.0
    )
    state.proposed_action = payload
    set_status(db, state.case_id, CaseStatus.ACTION_SELECTED)
    _audit(db, state.case_id, "action_selected", "agent", payload, reasoning=choice.reasoning)
    return state


def policy_check(db: Session, state: CaseState) -> CaseState:
    """Deterministic. Calls the real Phase-2 PolicyEngine. Audit logged HERE at
    the call site, exactly once (per Phase-2 spec decision)."""
    engine = PolicyEngine()
    choice = InterventionChoice(**{k: v for k, v in state.proposed_action.items()
                                   if k in InterventionChoice.model_fields})
    decision: PolicyDecision = engine.check(state, choice, detected_at=state.detected_at)
    state.policy_decision = decision.model_dump()
    _audit(db, state.case_id, "policy_check", "policy", {
        "proposed_action": state.proposed_action["action"],
        "allowed": decision.allowed,
        "reason": decision.reason,
        "escalate": decision.escalate,
        "config_snapshot": decision.config_snapshot,
    })
    if not decision.allowed:
        if decision.reason == "outside_contact_hours":
            # Transient compliance throttle — NOT terminal. Park the case until
            # the contact window opens; a scheduler/poller run resumes it.
            case = db.get(Case, state.case_id)
            state.terminal_reason = None
            set_status(db, state.case_id, CaseStatus.AWAITING_OUTCOME)
            _audit(db, state.case_id, "parked_contact_hours", "policy",
                   {"resume_after": _next_contact_window_open(decision.config_snapshot)})
            return state
        if decision.escalate:
            ticket = write_tools.escalate_to_human(
                db, state.case_id, decision.reason,
                f"Policy blocked action '{state.proposed_action['action']}' "
                f"({decision.reason}); human review required.", attempt_number=max(state.attempt_count, 1))
            state.outcome = {"escalation_ticket": ticket.ticket_id}
            state.terminal_reason = decision.reason
            set_status(db, state.case_id, CaseStatus.ESCALATED)
        else:
            state.terminal_reason = decision.reason
            set_status(db, state.case_id, CaseStatus.STOPPED)
    return state


def _next_contact_window_open(cfg: dict) -> str:
    """ISO instant when the IST contact window next opens (for parked cases)."""
    from datetime import timedelta

    from app.policy.policy_engine import _default_now, _ist_time

    ist = _ist_time(_default_now())
    start_hour = int(cfg.get("contact_hours_start", 8))
    open_local = ist.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    if ist.hour >= int(cfg.get("contact_hours_end", 19)):
        open_local += timedelta(days=1)
    utc_open = open_local - timedelta(hours=5, minutes=30)
    return utc_open.replace(tzinfo=timezone.utc).isoformat()


def execute_action(db: Session, state: CaseState) -> CaseState:
    action = state.proposed_action["action"]
    set_status(db, state.case_id, CaseStatus.EXECUTING)
    attempt = state.attempt_count + 1  # incremented transactionally with the tool call below
    try:
        if action == "send_reminder":
            result = write_tools.send_reminder(
                db, state.case_id, state.proposed_action.get("channel") or "email",
                state.proposed_action.get("message") or "Payment reminder", attempt)
        elif action == "send_payment_link":
            result = write_tools.send_payment_link(
                db, state.case_id, state.proposed_action.get("channel") or "email", attempt)
        elif action == "record_promise_to_pay":
            result = write_tools.record_promise_to_pay(
                db, state.case_id, state.proposed_action.get("promised_date")
                or datetime.now(timezone.utc).isoformat(), attempt)
        elif action == "retry_payment":
            from app.tools.failed_payment_tools import retry_payment

            result = retry_payment(db, state.case_id, attempt)
        elif action in ("update_payment_method_prompt", "send_dunning_email"):
            from app.tools.failed_payment_tools import send_dunning_email, update_payment_method_prompt

            fn = update_payment_method_prompt if action == "update_payment_method_prompt" else send_dunning_email
            result = fn(db, state.case_id, attempt)
        elif action in ("wait", "stop"):
            result = {"status": action}
            state.terminal_reason = f"agent_chose_{action}"
            set_status(db, state.case_id, CaseStatus.STOPPED)
            state.attempt_count = attempt
            db.flush()
            return state
        else:
            raise write_tools.ToolExecutionError(f"unknown action {action}", retryable=False)
    except write_tools.ToolExecutionError as e:
        state.outcome = {"tool_error": str(e), "retryable": e.retryable}
        state.attempt_count = attempt
        db.flush()
        return state

    state.attempt_count = attempt
    state.outcome = result if isinstance(result, dict) else result.model_dump(mode="json")
    set_status(db, state.case_id, CaseStatus.AWAITING_OUTCOME)
    db.commit()
    return state


# ---- Phase 4 nodes ----


def observe_outcome(db: Session, state: CaseState) -> CaseState:
    """Synchronous check against mock_payments / mock_retry_results via the poller.
    If recovered: mark_recovered fires with a verified payment reference."""
    from app.workers.outcome_poller import check_recovery_now

    recovered = check_recovery_now(db, state)
    if recovered:
        state.outcome = recovered
        set_status(db, state.case_id, CaseStatus.RECOVERED)
        db.commit()
    return state


def check_stopping_rules(db: Session, state: CaseState) -> CaseState:
    """Deterministic exhaustion logic, zero LLM calls.

    Window-expiry double-coverage decision: policy_check already blocks
    window_expired upstream on the NEXT select_action pass; stopping rules does
    NOT re-check it independently here to avoid two owners of one rule drifting
    apart (documented per Phase-4 spec). Stopping rules owns: retry budget and
    consecutive tool failures."""
    cfg = PolicyEngine().config
    decision = StoppingRulesDecision(exhausted=False)

    if state.attempt_count >= cfg["max_retries"]:
        decision = StoppingRulesDecision(exhausted=True, reason="max_retries_exceeded", escalate=True)
    elif isinstance(state.outcome, dict) and state.outcome.get("tool_error") and not state.outcome.get("retryable"):
        decision = StoppingRulesDecision(exhausted=True, reason="non_retryable_tool_failure", escalate=True)

    state.stopping_decision = decision.model_dump()
    _audit(db, state.case_id, "stopping_rules_check", "system", state.stopping_decision)

    if decision.exhausted:
        ticket = write_tools.escalate_to_human(
            db, state.case_id, decision.reason, "Stopping rules exhausted; human review required.",
            attempt_number=max(state.attempt_count, 1))
        state.terminal_reason = decision.reason
        state.outcome = {"escalation_ticket": ticket.ticket_id}
        set_status(db, state.case_id, CaseStatus.ESCALATED)
        db.commit()
    return state
