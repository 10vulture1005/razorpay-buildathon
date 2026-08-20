# Phase 1 — Tools Layer (Read + Write, Mock Adapters)

**Days 3-4 of 14. Depends on: Phase 0 (schema must exist and be seeded).**

## Context you need

This is the tools layer for a B2B Receivables Recovery Agent. Tools are the
**only** way the agent touches data or takes action — no node in the LangGraph
agent (built in Phase 3) should ever query Postgres directly; it goes through
a tool. This keeps the tool contracts as the single seam where I can swap mock
adapters for real payment/notification APIs later without touching agent logic.

Every tool is a typed Python function with explicit Pydantic input/output
schemas. No tool returns raw dicts or untyped data.

## What to build today

### 1. Read tools (`/app/tools/read_tools.py`)

```python
get_customer_history(customer_id) -> CustomerHistory
get_invoice(invoice_id) -> Invoice
get_past_promises(customer_id) -> list[Promise]
get_communication_log(case_id) -> list[Message]
```

- Pure reads, no side effects, no policy gating needed (read tools are never
  policy-gated — only writes are).
- `CustomerHistory` should include enough for the Context Builder to do its
  job in Phase 3: payment pattern summary (on-time rate, avg days late),
  broken-promise count, opted_out flag. Decide the exact shape, but document
  it in a docstring since Phase 3's context builder depends on this shape.
- These hit Postgres directly (that's fine — reads aren't the risky side).

### 2. Write tools (`/app/tools/write_tools.py`)

```python
send_reminder(case_id, channel, message) -> DeliveryResult       # low risk
send_payment_link(case_id, channel) -> DeliveryResult             # low risk
record_promise_to_pay(case_id, date) -> None                      # low risk
escalate_to_human(case_id, reason, summary) -> EscalationTicket   # high risk
mark_recovered(case_id, amount) -> None                           # high risk
```

**Every write tool takes an explicit `attempt_number` parameter and builds its
idempotency key as `f"{case_id}:{action}:{attempt_number}"`.** Before doing
anything, check the DB for an existing row with that key:
- If found: return the *previously recorded result* without re-executing
  (this is what makes retries, crash recovery, and duplicate events safe —
  a crash after execute-but-before-commit must not double-send on replay).
- If not found: execute, then write the result + idempotency key in the same
  transaction as the audit log entry. Execute-then-log-separately is a bug —
  if the process dies between them you get an unaudited action, which is
  worse than a missing action.

**Every write tool logs to `audit_log`** with `actor="agent"`, the tool name
as `event_type`, and the full input/output as `payload`. This happens inside
the tool, not left to the caller to remember.

**Mocking:** these are "mocked, real-shaped" per the spec — they don't call a
real payment gateway or SMS provider, but their signatures, error modes, and
timeout/retry behavior should look exactly like a real integration would.
Concretely:
- `send_reminder` / `send_payment_link`: simulate a delivery API — return
  `DeliveryResult(status, provider_message_id, sent_at)`, with a configurable
  simulated failure rate (e.g. 5% "delivery failed") so downstream retry logic
  in Phase 3/4 has something real to handle, not a tool that always succeeds.
- `escalate_to_human`: creates an `EscalationTicket` row (add this table if
  Phase 0 didn't include it — flag that you're adding it) and does NOT
  actually page anyone; that's out of scope.
- `mark_recovered`: **this one only fires on a verified payment event** — it
  should require a `verified_by` field (e.g. `"mock_payment_poller"`) and
  should refuse to run if called with no backing payment record. This one
  is the tool most likely to get called wrongly by an LLM hallucinating
  success, so make its precondition an actual assertion, not a comment.

### 3. Timeouts and retries

Wrap all tool calls (even mocked ones) with an explicit timeout and a small
retry count (e.g. 2 retries, exponential backoff) at the tool-call layer —
not left to the LangGraph node to remember. Distinguish retryable failures
(simulated transient delivery failure) from non-retryable ones (bad case_id) —
retrying a bad case_id 3 times is just wasted latency dressed up as
resilience.

### 4. Tool registry / schemas file

Put the Pydantic models (`CustomerHistory`, `Invoice`, `Promise`, `Message`,
`DeliveryResult`, `EscalationTicket`) in `/app/models/` (per the fixed repo
layout), imported by the tools file — don't define schemas inline in the
tools file itself, since Phase 3's LLM structured-output schemas will live in
the same `/app/models/` directory and I want one place to look.

### 5. Unit tests

- One test per read tool against seeded data.
- One test per write tool covering: normal execution, idempotency (call twice
  with same key, assert second call doesn't re-execute and returns identical
  result), and the simulated failure path.
- A specific test for `mark_recovered` refusing to run without a verified
  payment record.

## What NOT to build yet

No policy gating logic inside the tools themselves — that's Phase 2's job,
called by Phase 3's `policy_check` node *before* a write tool is ever
invoked. Tools should be capable of being called, correctly, by anything;
they don't know or care what a "policy" is. Don't blur this boundary even
though it'd be tempting to add an `if opted_out: raise` inside `send_reminder`
— that check belongs in the Policy Engine, and duplicating it here just gives
future-me two places to keep in sync.

## Acceptance checklist

- [ ] All 4 read tools + 5 write tools implemented with Pydantic I/O
- [ ] Idempotency key computed as `case_id:action:attempt_number` and enforced
      via the Phase 0 DB constraint, not just an in-memory check
- [ ] Calling a write tool twice with the same idempotency key is provably
      side-effect-free the second time (test proves this, not just asserts it)
- [ ] Every write tool call produces exactly one `audit_log` row, in the same
      transaction as the state-affecting write
- [ ] `mark_recovered` cannot succeed without a verified payment reference
- [ ] Timeout + retry wrapper exists and distinguishes retryable vs. not
- [ ] No policy logic lives inside `/app/tools/`

## Hand back to me

Show me the idempotency test (the one that calls a write tool twice) and its
output, and the `mark_recovered` precondition test. Those two are the ones
most likely to look done but not actually be enforced.
