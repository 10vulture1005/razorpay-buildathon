"""Agent orchestration as a LangGraph StateGraph.

Graph shape (spec Section 2.4):

    ingest_case -> build_context -> diagnose --> select_action --> policy_check
        policy_check ──allowed──> execute_action -> observe_outcome
        observe_outcome ──not recovered──> check_stopping_rules
        check_stopping_rules ──within limits──> select_action (bounded loop)

Load-bearing invariants preserved under LangGraph:
- policy_check / check_stopping_routes are deterministic Python nodes; no LLM
  client is imported anywhere in this module.
- The retry loop is bounded by the explicit attempt_count/iterations counters
  checked in select_action — hitting the bound routes cleanly to ESCALATED.
  recursion_limit is a backstop only, never the primary bound.
- Every conditional edge routes off structured state fields / DB status,
  never off LLM free text.
"""
from sqlalchemy.orm import Session

from langgraph.graph import END, START, StateGraph

from app.agent.nodes import nodes
from app.db.tables import Case, CaseStatus
from app.models.domain import CaseState

# Backstop against runaway loops. The real bound is attempt_count in state;
# this just guarantees termination even if routing logic regresses.
RECURSION_LIMIT = 40


def _escalate_on_error(db: Session, case_id: str, state: CaseState, exc: Exception) -> CaseState:
    """Malformed context data or unexpected failure: graceful degradation,
    never a crash and never a silently-wrong action. Route to human."""
    try:
        db.rollback()
    except Exception:
        pass
    nodes._audit(db, case_id, "unexpected_error", "system", {"error": str(exc)[:500]})
    ticket = nodes.write_tools.escalate_to_human(
        db, case_id, "internal_error",
        f"Agent hit an unrecoverable error: {str(exc)[:200]}. Human review required.",
        attempt_number=1)
    nodes.set_status(db, case_id, CaseStatus.ESCALATED)
    state.terminal_reason = "internal_error"
    state.outcome = {"escalation_ticket": ticket.ticket_id}
    db.commit()
    return state


def _safe(db_fn, db: Session, case_id: str):
    """Wraps a node so unexpected exceptions escalate instead of crashing the
    run, and so each node's writes are durably committed before routing."""

    def run(state: CaseState) -> CaseState:
        try:
            updated = db_fn(db, state)
            db.commit()
            return updated
        except Exception as e:  # noqa: BLE001 — deliberate catch-all at the boundary
            return _escalate_on_error(db, case_id, state, e)

    return run


def build_case_graph(db: Session):
    """Returns a compile factory binding the DB session + case_id into every node."""

    def status(case_id: str) -> CaseStatus:
        # Postgres row is the source of truth for routing decisions
        return db.get(Case, case_id).status

    # ---- routers (plain Python over structured state + DB status; NO LLM) ----

    def route_after_diagnose(state: CaseState) -> str:
        if state.diagnosis and state.diagnosis.get("failed"):
            return END
        return "select_action"

    def route_after_select_action(state: CaseState) -> str:
        # DB row is truth: nodes set_status on the Case row, not on state.status
        if not state.proposed_action or status(state.case_id) != CaseStatus.ACTION_SELECTED:
            return END  # loop bound hit or structured-output failure → terminal
        return "policy_check"

    def route_after_policy_check(state: CaseState) -> str:
        allowed = (state.policy_decision or {}).get("allowed")
        if allowed and state.terminal_reason is None:
            return "execute_action"
        return END  # ESCALATED / STOPPED / parked (contact hours) via policy semantics

    def route_after_execute_action(state: CaseState) -> str:
        if status(state.case_id) == CaseStatus.STOPPED:
            return END  # agent chose wait/stop — silent stop per policy taxonomy
        return "observe_outcome"

    def route_after_observe_outcome(state: CaseState) -> str:
        if status(state.case_id) == CaseStatus.RECOVERED:
            return END
        return "check_stopping_rules"

    def route_after_stopping_rules(state: CaseState) -> str:
        if status(state.case_id) in (CaseStatus.ESCALATED, CaseStatus.STOPPED):
            return END
        return "select_action"  # within limits → bounded loop-back

    def _bind(case_id: str):
        sg = StateGraph(CaseState)
        sg.add_node("build_context", _safe(nodes.build_context, db, case_id))
        sg.add_node("diagnose", _safe(nodes.diagnose, db, case_id))
        sg.add_node("select_action", _safe(nodes.select_action, db, case_id))
        sg.add_node("policy_check", _safe(nodes.policy_check, db, case_id))
        sg.add_node("execute_action", _safe(nodes.execute_action, db, case_id))
        sg.add_node("observe_outcome", _safe(nodes.observe_outcome, db, case_id))
        sg.add_node("check_stopping_rules", _safe(nodes.check_stopping_rules, db, case_id))

        sg.add_edge(START, "build_context")
        sg.add_edge("build_context", "diagnose")
        sg.add_conditional_edges("diagnose", route_after_diagnose, {
            "select_action": "select_action", END: END})
        sg.add_conditional_edges("select_action", route_after_select_action, {
            "policy_check": "policy_check", END: END})
        sg.add_conditional_edges("policy_check", route_after_policy_check, {
            "execute_action": "execute_action", END: END})
        sg.add_conditional_edges("execute_action", route_after_execute_action, {
            "observe_outcome": "observe_outcome", END: END})
        sg.add_conditional_edges("observe_outcome", route_after_observe_outcome, {
            "check_stopping_rules": "check_stopping_rules", END: END})
        sg.add_conditional_edges("check_stopping_rules", route_after_stopping_rules, {
            "select_action": "select_action", END: END})
        return sg.compile()

    return _bind


def run_case(db: Session, case_id: str) -> CaseState:
    state = nodes.load_case_state(db, case_id)
    if state.status in (CaseStatus.RECOVERED, CaseStatus.ESCALATED, CaseStatus.STOPPED):
        return state

    state = nodes.ingest_case(db, state)
    db.commit()

    app_graph = build_case_graph(db)(case_id)
    try:
        result = app_graph.invoke(state, config={"recursion_limit": RECURSION_LIMIT})
    except Exception as e:
        # Graph-level failure (recursion backstop, framework error): clean
        # ESCALATED, never a crash surfaced to callers.
        return _escalate_on_error(db, case_id, state, e)

    # LangGraph returns the merged channel state as a dict; rebuild the typed
    # model and sync status from the DB row (DB is the source of truth).
    final = CaseState.model_validate(result) if isinstance(result, dict) else result
    final.status = db.get(Case, case_id).status
    return final
