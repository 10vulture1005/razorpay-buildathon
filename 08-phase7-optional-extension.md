# Phase 7 (Optional) — Extend to Failed Payment Recovery

**Only if time remains after Phase 6 is fully green and rehearsed.**
**Depends on: Phase 6 complete.**

## Context you need

This phase exists to answer "does this scale beyond one use case?" — it's
explicitly the strongest evidence the architecture generalizes rather than
being a one-off script for B2B receivables specifically (Section 1 and
Section 7 of the source doc). The test of success here is architectural:
**how little has to change.**

If this phase requires touching `PolicyEngine`, the LangGraph state machine
shape, or the audit log schema, that's a signal the architecture wasn't as
generalizable as claimed — flag that explicitly rather than quietly making
those changes to get the demo working.

## What should NOT change

- `PolicyEngine` — same rules, same YAML config shape (a subscription/failed-
  payment case might need different threshold *values*, e.g. a different
  `escalation_min_value` for a ₹999/month subscription vs. a ₹5,00,000
  invoice — but the rule *logic* and the class itself should be unchanged.
  If a genuinely new rule type is needed, that's worth a real conversation,
  not a silent addition.)
- The state machine (`NEW → DIAGNOSED → ... → RECOVERED/ESCALATED/STOPPED`)
  and the graph shape (`ingest_case → build_context → diagnose →
  select_action → policy_check → execute_action → observe_outcome →
  check_stopping_rules`) — same nodes, same edges, same conditional logic.
- `audit_log` schema — same table, same event types where applicable.
- The core Pydantic schemas (`DiagnosisResult`, `InterventionChoice`,
  `PolicyDecision`) — the `Literal` enums inside them may need new values
  (e.g. a new `likely_cause` like `"card_expired"` or `"insufficient_funds"`
  alongside the existing five), but the schema shape stays.

## What SHOULD change

- **A new context builder**: failed payments pull different source data —
  card/bank decline codes, subscription status, retry history from the
  payment processor — instead of invoice/customer receivables history. New
  read tools analogous to Phase 1's (`get_payment_method_status`,
  `get_decline_history`, or similar), same typed-function-with-Pydantic-
  schema discipline as the original tools.
- **A new tool set** for the write side: `retry_payment`,
  `update_payment_method_prompt`, `send_dunning_email` or similar — same
  idempotency-key discipline (`case_id + action + attempt_number`), same
  audit logging pattern, same low-risk/high-risk tiering as the original
  write tools.
- **New `likely_cause` / `action` enum values** as needed (card expired,
  insufficient funds, bank decline — these are meaningfully different
  diagnoses from the receivables side's cashflow/dispute/forgot/delay/
  unwilling).
- **New synthetic test data** — a smaller batch than the original 20-30 is
  fine given the time constraint, but cover the equivalent archetype
  breadth: a clean recoverable case, a genuinely dead card, a
  disputed/fraud-flagged charge, high-value vs. low-value.

## What to build (if time allows)

1. New Pydantic schemas for the new context (`/app/models/`) alongside, not
   replacing, the receivables ones.
2. New read/write tools (`/app/tools/`) following Phase 1's exact pattern —
   idempotency, timeouts/retries, audit logging inside the tool.
3. A context-builder variant that the graph can route to based on case type
   (`case.type == "receivable"` vs. `"failed_payment"`, or however you
   choose to discriminate — decide explicitly and document it, since this
   discriminator is the actual seam proving generalization).
4. Confirm `PolicyEngine.check()` runs unmodified against a `CaseState` built
   from failed-payment data — this is the single most important thing to
   verify and demo. If it needs modification to work, that's the finding to
   report, not to quietly patch around.
5. A handful of synthetic failed-payment cases run through the same graph,
   same eval harness from Phase 5 (reused, not rebuilt).
6. One dashboard case-detail view showing a failed-payment case, ideally
   sitting in the same Case Detail component from Phase 6 with no
   use-case-specific branching in the UI beyond field labels.

## Acceptance checklist

- [ ] Zero changes to `PolicyEngine`'s class/logic (config value changes OK,
      logic changes are a flagged finding, not a silent fix)
- [ ] Zero changes to the graph's node/edge shape
- [ ] New tools follow identical idempotency/audit discipline as Phase 1's
- [ ] At least one failed-payment case runs end-to-end through the
      unmodified graph and policy engine
- [ ] Phase 5's eval harness runs against the new cases without
      eval-harness code changes beyond adding the new test cases to the
      JSON file
- [ ] Case Detail UI renders a failed-payment case with no use-case-specific
      component branching

## Hand back to me

The single most important thing to report here: **did `PolicyEngine` and the
graph shape genuinely need zero logic changes, or did you have to modify
either one to make this work?** Tell me the true answer even if it's "I had
to add one thing" — that's a more useful finding for the demo narrative than
a claim of pure generalization that doesn't hold up under a follow-up
question.
