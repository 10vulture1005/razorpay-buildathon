# Phase 2 — Policy Engine (Fully Deterministic)

**Days 5-6 of 14. Depends on: Phase 0 (schema). No dependency on Phase 1 — can
be built in parallel with it if you're compressing the timeline.**

## Context you need

This is the single most scrutinized module in the whole build. In the demo
Q&A, this is what gets probed hardest — "what stops the agent from doing
something dumb/expensive/harassing." The answer has to be: this module, which
contains **zero LLM calls**, ever. The model proposes an action; this decides
whether it's allowed. If an LLM ever "argues around" a policy block, that's a
build-blocking bug, not something to patch with a better prompt.

Policy limits live in YAML, not hardcoded constants, so thresholds are
demo-tunable live without a redeploy.

## What to build today

### 1. `PolicyEngine` class (`/app/policy/policy_engine.py`)

Standalone module — not a function buried inside an agent node file, not a
method on the case model, not a static function scattered across the
codebase. One class, one file, imported by whatever calls it.

```python
class PolicyEngine:
    def check(self, case: CaseState, proposed_action: InterventionChoice) -> PolicyDecision:
        if case.attempt_count >= MAX_RETRIES:
            return PolicyDecision(allowed=False, reason="max_retries_exceeded", escalate=True)
        if case.opted_out:
            return PolicyDecision(allowed=False, reason="customer_opted_out", escalate=False)
        if (utcnow() - case.detected_at).days > MAX_RECOVERY_WINDOW_DAYS:
            return PolicyDecision(allowed=False, reason="window_expired", escalate=True)
        if case.messages_sent_today >= MAX_MESSAGES_PER_DAY:
            return PolicyDecision(allowed=False, reason="daily_message_cap", escalate=False)
        if proposed_action.action == "escalate_human" and case.amount < ESCALATION_MIN_VALUE:
            return PolicyDecision(allowed=False, reason="below_escalation_threshold", escalate=False)
        return PolicyDecision(allowed=True)
```

Build this out fully — the snippet above is the shape, not the complete rule
set. Think through and implement:

- **Rule ordering matters.** Check in the order that produces the most useful
  `reason` — e.g. `opted_out` should probably short-circuit before anything
  else, since nothing else matters if the customer opted out. Decide the
  order deliberately and document why, don't just transcribe the snippet
  order as gospel.
- **`escalate` flag semantics:** some rejections should auto-escalate to a
  human (`max_retries_exceeded`, `window_expired` — the case needs *someone*
  to look at it), others should just silently stop (`opted_out`,
  `daily_message_cap` — no human needs to be paged for these, the case just
  waits or closes). Get this distinction right per rule, it's load-bearing
  for Phase 4's stopping-rules logic.
- **`PolicyDecision` schema:** `allowed: bool`, `reason: str | None`,
  `escalate: bool`, and add a `policy_version` or `config_snapshot` field so
  a decision is reproducible even if the YAML changes later — this ties into
  Phase 0's audit requirement that old decisions stay auditable after a
  config change.

### 2. `policy_config.yaml`

```yaml
max_retries: 3
max_recovery_window_days: 7
max_messages_per_day: 1
escalation_min_value: 100000  # INR
```

Load this at `PolicyEngine` construction time (or per-call if you want live
reload for the demo — your call, but if you pick live reload, make sure the
`config_snapshot` audit field above actually reflects what was loaded at
decision time, not the current file state).

### 3. Unit tests — one per rule, exhaustively

This is the part that needs to be bulletproof, not just present. For **every
rule**, write tests for:
- The boundary case exactly at the threshold (e.g. `attempt_count ==
  MAX_RETRIES` — does the `>=` in the snippet actually block at the limit or
  one past it? Verify the off-by-one doesn't exist.)
- One case just under the threshold (should pass that rule)
- One case just over (should fail that rule)
- A case that trips **two rules at once** — assert the engine returns the
  first rule's reason per your documented ordering, not a random one

Also test:
- A fully-clean case with a policy-compliant action returns `allowed=True`
  with no reason
- The `escalate` flag is correctly `True`/`False` per rule as designed above

### 4. Audit logging integration

Every call to `PolicyEngine.check()` — allowed or rejected — writes a row to
`audit_log` with `actor="policy"`, `event_type="policy_check"`, and a payload
containing the proposed action, the decision, the reason (if any), and the
config snapshot. This should happen at the call site in Phase 3's graph node,
OR inside `PolicyEngine.check()` itself — pick one, document which, and don't
do it in both places (duplicate logging is as bad as missing logging when
someone's trying to reconcile the audit trail later).

## What NOT to build yet

No LangGraph node calling this yet — that's Phase 3. Build and test
`PolicyEngine` entirely standalone, callable from a test file with a
hand-constructed `CaseState` and `InterventionChoice`, no graph required.

## Acceptance checklist

- [ ] `PolicyEngine` is a standalone class in `/app/policy/`, config-driven
      from YAML, zero LLM calls anywhere in the module
- [ ] Every rule from the spec is implemented, plus the `escalate` semantics
      thought through per rule (not just copy-pasted from the snippet)
- [ ] Boundary tests exist for every rule (at-threshold, under, over)
- [ ] A multi-rule-violation test exists and asserts deterministic ordering
- [ ] `PolicyDecision` includes a reproducibility field (version/snapshot)
- [ ] Policy check logging is implemented exactly once (not duplicated,
      not missing)
- [ ] Test suite passes with 100% branch coverage on `check()` — this
      function is small enough that "we didn't quite get to full coverage"
      isn't an acceptable outcome here

## Hand back to me

Show me the full test file output (all boundary tests, pass/fail), and walk
me through what happens when a case has BOTH `attempt_count >= MAX_RETRIES`
AND `opted_out=True` — which reason wins, and why you ordered it that way.
This exact scenario is the kind of thing that gets asked live.
