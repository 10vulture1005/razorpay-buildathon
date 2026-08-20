# Phase 3 — LangGraph Agent: diagnose, select_action, execute

**Days 7-9 of 14. Depends on: Phase 0 (schema), Phase 1 (tools), Phase 2
(policy engine). This is the biggest phase — budget the full 3 days.**

## Context you need

This is the orchestration core. Graph shape (full loop, including Phase 4's
pieces which you'll wire the edges for but not fully implement yet):

```
ingest_case
     ↓
build_context           (assembles customer history, invoice, promises)
     ↓
diagnose                (LLM, structured output: DiagnosisResult)
     ↓
select_action            (LLM, structured output: InterventionChoice)
     ↓
policy_check             (deterministic, NO LLM) ──rejected──→ escalate
     ↓ allowed
execute_action            (idempotent tool call)
     ↓
observe_outcome           (poll or webhook)              [Phase 4 builds this fully]
     ↓
recovered? ──yes──→ RECOVERED (stop)
     │no
check_stopping_rules      (deterministic, NO LLM)         [Phase 4 builds this fully]
     │
     ├─ within limits ──→ loop back to select_action
     └─ exhausted ──→ ESCALATED / STOPPED (terminal, human review)
```

**Today you're building through `execute_action` end to end, plus stubbing
`observe_outcome` and `check_stopping_rules` just enough that the graph
compiles and one full loop iteration runs** (Phase 4 replaces the stubs with
real logic on Day 10). Get one case running through the full loop — including
a retry that then escalates — before touching anything else.

Non-negotiables, repeated because this is where they actually get enforced:
- Every LLM call in this graph returns a Pydantic-validated structured output.
  `reasoning` fields are audit trail only — **never branched on** by any
  conditional edge or downstream node. If you catch yourself writing
  `if "dispute" in result.reasoning:`, stop — that's exactly the failure mode
  this architecture is designed to prevent.
- `policy_check` and `check_stopping_rules` are plain Python. No LLM client
  is imported into either node's file.
- The retry loop is bounded by an explicit counter read from `CaseState`
  (`attempt_count`, from Phase 0's schema) — not solely by LangGraph's
  `recursion_limit`. Hitting the bound must route to a clean `ESCALATED`
  state via a normal conditional edge, not raise/crash and not rely on
  LangGraph's own recursion error.

## What to build today

### 1. Model routing (`/app/agent/nodes/`, one file per node)

Per the architecture doc's task/model-tier mapping:

| Node | Model tier |
|---|---|
| `diagnose` | Frontier (genuine reasoning over conflicting signals) |
| `select_action` | Frontier (needs to weigh value/probability, not just classify) |

(Intent classification and summarization are small/fast per the source doc,
but neither is a graph node in this MVP scope — they'd live inside
`build_context` if you choose to use an LLM there at all. Default to
**non-LLM** context assembly for now — it's a Postgres query plus formatting,
not a reasoning task — and only introduce a small-model call there if you hit
a genuine need, since every LLM call is a cost/latency/failure surface this
build doesn't need yet.)

Make the model client configurable (env var for model name/tier), not
hardcoded per node — I will be swapping providers.

### 2. Structured output schemas (`/app/models/`)

```python
class DiagnosisResult(BaseModel):
    likely_cause: Literal["cashflow_issue", "dispute", "forgot", "process_delay", "unwilling"]
    confidence: float
    reasoning: str  # audit trail only — never used in downstream logic

class InterventionChoice(BaseModel):
    action: Literal["send_reminder", "send_payment_link", "escalate_human", "wait", "stop"]
    expected_recovery_probability: float
    reasoning: str
```

Extend `InterventionChoice` to also carry whatever `select_action` needs to
compute expected net value (Section 3 of the source doc):
`Expected Recovery Value = Amount at Risk × Probability of Recovery`,
`Net Expected Value = Expected Recovery Value − Intervention Cost`. Decide
whether this scoring happens *inside* the LLM call (model returns
probability, code computes the arithmetic) or entirely in code after a
simpler classification call — **the arithmetic must happen in code either
way**, never trust the model to report the multiplication result directly.
Add a per-action cost table (probably a small constant dict — a call costs
more than a reminder) somewhere sensible, `/app/policy/` or `/app/agent/`,
your call, but document where.

Validate every LLM response against the schema at the call site. On schema
validation failure: retry once with a stricter prompt reminder, then on
second failure route to `ESCALATED` with reason `structured_output_failure`
— never let a malformed LLM response silently fall through with a default
value, that's the exact "free text leaking into the policy engine" failure
mode this architecture exists to prevent.

### 3. Node implementations (`/app/agent/nodes/`)

- **`ingest_case`** — loads `CaseState` from Postgres by `case_id`, sets
  status to `NEW` if not already in progress.
- **`build_context`** — calls the Phase 1 read tools (`get_customer_history`,
  `get_invoice`, `get_past_promises`, `get_communication_log`), assembles
  **only the fields relevant to the current decision**: amount, days
  overdue, last 2 attempts, payment pattern summary. Explicitly do NOT dump
  the full audit log or full communication history into context — this is
  a deliberate context-window-discipline decision from the source spec, not
  an oversight if it looks sparse.
- **`diagnose`** — LLM call, `DiagnosisResult` structured output, writes
  result to `audit_log` regardless of outcome.
- **`select_action`** — LLM call, `InterventionChoice` structured output,
  computes net expected value per above, writes to `audit_log`.
- **`policy_check`** — calls Phase 2's `PolicyEngine.check()`. Conditional
  edge: `allowed=True` → `execute_action`; `allowed=False, escalate=True` →
  `escalate_to_human` tool then terminal `ESCALATED`; `allowed=False,
  escalate=False` → terminal `STOPPED` (or loop-safe wait, per Phase 2's
  documented per-rule semantics — don't reinvent that decision here, use
  what Phase 2 already decided).
- **`execute_action`** — calls the matching Phase 1 write tool with the
  correct `attempt_number` (increment `CaseState.attempt_count` in the same
  transaction as the tool call, not before/after it separately).
- **`observe_outcome`** (stub for now) — hardcode/stub a "not yet recovered"
  result so the graph can route onward; Phase 4 replaces this.
- **`check_stopping_rules`** (stub for now) — just check `attempt_count >=
  MAX_RETRIES` inline (duplicating one Policy Engine rule temporarily is fine
  for the stub, but leave a `# TODO Phase 4: replace with full stopping-rules
  logic` comment) so the loop-back edge and the escalate-on-exhaustion edge
  both exist and are exercised today.

### 4. Graph wiring (`/app/agent/graph.py`)

Wire the full graph per the diagram above using LangGraph's conditional
edges. Explicit requirements:
- The loop-back edge (`check_stopping_rules` → `select_action`) must
  increment a counter that's checked *before* `select_action` runs again, so
  a pathological case can't spin past the bound even between
  `check_stopping_rules` calls.
- Set LangGraph's `recursion_limit` as a backstop, but the explicit
  `attempt_count` check is the real bound — the recursion limit hit should
  never be the normal path to `ESCALATED` in a working system, only a
  safety net if something's wrong with the counter logic.
- Every node transition writes a state-transition row to `audit_log`
  (`actor="system"`, `event_type="state_transition"`), so the audit trail
  shows the full path a case took through the graph, not just the
  LLM/policy decisions along the way.

### 5. Get one case running end to end today

Before moving on: run a single seeded case through `ingest_case →
execute_action`, force a retry (e.g. by having the stubbed
`observe_outcome` always say "not recovered" for the first N calls), and
confirm it eventually hits the stubbed `check_stopping_rules` exhaustion
path and lands in `ESCALATED` cleanly — no crash, no infinite loop, full
audit trail present.

## What NOT to build yet

Real `observe_outcome` (payment polling) and real `check_stopping_rules`
(full logic beyond the one-rule stub) — that's Phase 4, tomorrow. Don't
over-build the stubs; a clearly-marked stub that Phase 4 replaces wholesale
is better than a half-real implementation that Phase 4 has to untangle.

## Acceptance checklist

- [ ] Graph compiles and all nodes/edges match the diagram
- [ ] `diagnose` and `select_action` both produce schema-validated structured
      output, with a defined failure path (retry once → escalate) on
      validation failure
- [ ] Net expected value arithmetic happens in code, not trusted from the LLM
- [ ] `policy_check` calls the real Phase 2 `PolicyEngine`, no reimplementation
- [ ] `build_context` demonstrably does NOT pull full audit log / full comms
      history — show the actual field list it assembles
- [ ] `execute_action` correctly increments `attempt_number` transactionally
      with the tool call
- [ ] One full seeded case runs end-to-end through a forced retry → escalate
      path with a complete audit trail
- [ ] The explicit attempt-count bound is checked before every `select_action`
      re-entry, independent of LangGraph's `recursion_limit`

## Hand back to me

Show me: (1) the full audit trail (ordered by `created_at`) for the one case
you ran end-to-end, (2) the exact field list `build_context` assembles for the
LLM (I want to see context-window discipline is real, not just claimed), and
(3) what happens in your implementation if `diagnose` returns malformed JSON
twice in a row — walk me through the actual code path, don't just describe it.
