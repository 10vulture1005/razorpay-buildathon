# Phase 4 — Outcome Observation and Stopping Rules

**Day 10 of 14. Depends on: Phase 3 (graph with stubbed `observe_outcome` and
`check_stopping_rules`).**

## Context you need

Phase 3 left two nodes stubbed: `observe_outcome` (always says "not recovered")
and `check_stopping_rules` (one hardcoded rule, `attempt_count >=
MAX_RETRIES`). Today those become real. This is also the day you run the full
20-30 case synthetic batch through the complete loop for the first time and
sanity-check the numbers before building evals on top of a possibly-broken
loop.

Reminder of the non-negotiable this phase is most likely to violate under
time pressure: **`check_stopping_rules` has zero LLM calls.** It's exhausted-
resources logic, not judgment logic — judgment already happened in
`select_action`.

## What to build today

### 1. Mock payment poller (`/app/workers/outcome_poller.py`)

Per the source spec: `observe_outcome` is async in reality (webhook or bank
statement). For the build, this is a background poller reading a
`mock_payments` table.

- Add a `mock_payments` table if Phase 0 didn't include it (flag it if you're
  adding it now): `invoice_id`, `amount_paid`, `paid_at`, `source` (e.g.
  `"manual_demo_insert"`, `"synthetic_batch"`).
- The poller runs on an interval (or is triggered synchronously in test mode
  — support both: interval-based for a live demo where I manually insert a
  row into `mock_payments` to simulate "customer paid" live, and a
  synchronous "check now" call for the eval batch runs so Phase 5 doesn't
  have to sleep-and-poll for 20-30 cases).
- When a matching `mock_payments` row is found for a case's invoice: this is
  what triggers `mark_recovered` (Phase 1's tool, which — remember — refuses
  to run without a verified payment reference). The poller supplies
  `verified_by="mock_payment_poller"` and the `mock_payments` row id as the
  verification reference.
- `observe_outcome` (the graph node) calls into this poller/checker, gets a
  recovered/not-recovered result, and routes the conditional edge
  accordingly. If recovered: `mark_recovered` fires, case moves to terminal
  `RECOVERED`, and the loop does not continue to `check_stopping_rules`.

### 2. Real `check_stopping_rules` (`/app/agent/nodes/`)

Deterministic, no LLM, callable independent of the graph (same testing
philosophy as Phase 2's `PolicyEngine`). Beyond the one rule Phase 3 stubbed,
implement the full exhaustion logic:
- `attempt_count >= MAX_RETRIES` → exhausted, escalate
- `(utcnow() - detected_at).days > MAX_RECOVERY_WINDOW_DAYS` → exhausted,
  escalate (this overlaps with a Policy Engine rule — decide explicitly
  whether `check_stopping_rules` re-checks window expiry independently or
  trusts that `policy_check` already caught it upstream on the next
  `select_action` attempt; document the decision, since having the same rule
  live in two places with two different owners is exactly the kind of thing
  that drifts out of sync during a demo period)
- Anything else the source spec's Section 18 state machine implies that
  Phase 2 didn't already cover as a policy rule — if you find a gap, flag it
  to me rather than silently inventing a new rule I haven't approved.

Output should mirror `PolicyEngine`'s pattern: a small structured decision
(`exhausted: bool`, `reason: str | None`) written to `audit_log` with
`actor="system"`, `event_type="stopping_rules_check"`, every time it's
called — not just when it trips.

### 3. Wire the real conditional edges

Replace Phase 3's stubs:
- `observe_outcome` → `recovered? yes: terminal RECOVERED / no:
  check_stopping_rules`
- `check_stopping_rules` → `within limits: loop back to select_action /
  exhausted: terminal ESCALATED or STOPPED` (per whatever
  escalate-vs-silent-stop semantics you're carrying from Policy Engine's
  `escalate` flag design — stay consistent with that, don't invent a second
  taxonomy of terminal states)

### 4. Run the full synthetic batch (all 20-30 seeded cases)

This is the actual point of today, not just an afterthought at the end:
- Run every seeded case through the complete graph, start to finish.
- For each, manually insert (or script the insertion of) appropriate
  `mock_payments` rows for the "clean payer" archetype cases so at least
  some of the batch actually reaches `RECOVERED` — otherwise every case
  escalates and you haven't actually exercised the recovered-path logic.
- Sanity-check by archetype (from Phase 0's labeled synthetic data): does
  the clean payer mostly recover? Does the serial promise-breaker mostly
  escalate after repeated broken promises? Does the opted-out customer get
  zero actions taken against them at all (this one specifically — check the
  audit log shows `policy_check` rejecting on `customer_opted_out` on the
  very first attempt, not after some actions already went out)?
- If any archetype's outcome looks wrong (e.g. opted-out customer got a
  reminder sent), stop and fix it — don't carry a broken loop into Phase 5's
  eval harness, since the evals will just measure the bug consistently
  rather than catch it.

## What NOT to build yet

The eval harness itself (pass/fail scoring, adversarial cases) is Phase 5.
Today's batch run is a manual/scripted sanity check, not the formal eval —
though the numbers you get today should roughly match what Phase 5's harness
reports, and a big mismatch between today's manual read and Phase 5's
automated scoring is itself worth flagging when you get there.

## Acceptance checklist

- [ ] Mock payment poller exists, supports both interval mode (live demo)
      and synchronous check (batch runs)
- [ ] `mark_recovered` only ever fires with a real `mock_payments` reference
      behind it (re-verify Phase 1's precondition still holds through this
      new caller)
- [ ] `check_stopping_rules` is fully deterministic, no LLM import in the file
- [ ] The window-expiry double-coverage (Policy Engine vs. stopping rules) is
      an explicit, documented decision, not an accident
- [ ] Every stopping-rules check writes to `audit_log`, trip or no trip
- [ ] Full 20-30 case synthetic batch runs end to end without a crash
- [ ] Opted-out customers show zero non-`policy_check` actions in their
      audit trail, verified by actually looking, not assumed
- [ ] At least some cases reach terminal `RECOVERED` via the real poller path

## Hand back to me

Show me the outcome distribution across the full batch (count of
RECOVERED / ESCALATED / STOPPED, broken down by archetype), and specifically
the full audit trail for one opted-out customer's case and one serial
promise-breaker's case. I want to see the loop actually discriminating
between archetypes, not producing the same outcome regardless of input.
