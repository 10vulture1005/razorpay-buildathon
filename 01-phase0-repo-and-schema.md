# Phase 0 — Repo Scaffold, Postgres Schema, Synthetic Data

**Days 1-2 of 14. Depends on: nothing. Everything else depends on this.**

## Context you need

I'm building a **B2B Receivables Recovery Agent** — an AI system that chases
overdue B2B invoices via reminders/payment links, decides when to escalate to
a human, and stops automatically per policy. It's architected so the same
graph can later extend to Failed Payment Recovery (different context builder
and tools, same Policy Engine and state machine) — don't build anything today
that would block that, but don't build the extension either.

Repo layout, fixed for the whole project:

```
/app
  /models        # Pydantic schemas: CaseState, DiagnosisResult, InterventionChoice
  /db            # SQLAlchemy models + migrations
  /tools         # read_tools.py, write_tools.py (mock adapters)
  /policy        # policy_engine.py + policy_config.yaml
  /agent         # graph.py (LangGraph), nodes/ (one file per node)
  /evals         # test_cases.json, eval_runner.py
  /api           # FastAPI routes
  /workers       # outcome_poller.py (mock payment watcher)
/frontend        # React + Tailwind dashboard
```

Tech stack: FastAPI, PostgreSQL, SQLAlchemy, LangGraph, Pydantic. Frontend is
React + Tailwind but that's Phase 6 — don't scaffold it yet beyond a placeholder
directory.

## What to build today

### 1. Repo scaffold

Create the full directory tree above. Set up:
- `pyproject.toml` or `requirements.txt` (your call, pick one and be consistent)
- `alembic` for migrations (or your preferred SQLAlchemy migration tool — state
  the choice explicitly, don't silently pick one and leave me to discover it)
- `.env.example` with `DATABASE_URL` and placeholders for model API keys
- A `docker-compose.yml` with just Postgres, so I can run this without installing
  Postgres locally
- `README.md` with setup steps (`docker-compose up`, migrate, seed)

### 2. Postgres schema

Tables required — do not add tables beyond these without flagging it to me first,
since every extra table is extra surface area for the audit story to cover:

- **`customers`** — id, name, segment (optional), opted_out (bool), created_at
- **`invoices`** — id, customer_id (FK), amount, currency, due_date, status
  (`open`/`recovered`/`written_off`), created_at
- **`cases`** — id, invoice_id (FK), customer_id (FK), status (state machine —
  see below), attempt_count (int, default 0), messages_sent_today (int, default
  0, resets daily — decide and document the reset mechanism), last_action,
  last_action_at, next_allowed_action_at, detected_at, amount_at_risk,
  created_at, updated_at
- **`promises`** — id, case_id (FK), customer_id (FK), promised_date,
  recorded_at, kept (bool, nullable — null until resolved)
- **`audit_log`** — id, case_id (FK), event_type, actor (`agent`/`policy`/
  `human`/`system`), payload (JSONB), reasoning (text, nullable — LLM
  reasoning when applicable), created_at. **This table is the demo artifact.**
  Every policy decision (allowed or rejected), every LLM call's structured
  output, every tool execution, every state transition gets a row here.
- **`policy_config`** — either a single-row table mirroring the YAML, or skip
  the table and load YAML directly at runtime (your call — but if you skip the
  table, still log which config values were active at decision-time into
  `audit_log.payload` so a policy change doesn't retroactively make old
  decisions unauditable)

Case status enum (the state machine from the architecture doc):
`NEW → DIAGNOSED → ACTION_SELECTED → EXECUTING → AWAITING_OUTCOME → RECOVERED`
(terminal) or `→ ESCALATED` (terminal) or `→ STOPPED` (terminal, e.g. opted-out
or window-expired). Encode this as a proper enum type or a CHECK constraint,
not a free-text column — invalid states should be a DB-level impossibility,
not just an application bug.

Idempotency: every write tool will key on `case_id + action + attempt_number`.
Add whatever unique constraint or index makes that enforceable at the DB
level, not just in application code — this is a place where "policy compliance"
type properties need infra teeth, not just LLM good behavior.

### 3. Synthetic data generator

Write `scripts/seed_synthetic_data.py` generating **20-30 realistic B2B
receivables cases** covering these archetypes explicitly (label which archetype
each generated case belongs to, in a comment or a `notes` field — you'll need
this for Phase 5 eval labeling):

- Clean payer (pays promptly once reminded)
- Serial promise-breaker (makes promises, doesn't keep them, 2-3 broken
  promises in history)
- Disputed invoice (customer has flagged a dispute in communication history)
- High-value / low-risk (large amount, good payment history)
- Low-value / high-risk (small amount, poor payment history)
- Opted-out customer (should never be actioned — `customers.opted_out = true`)

Vary: amount (₹500 to ₹10,00,000+, so the value-based escalation threshold in
Phase 2 has real range to bite on), days overdue, payment history length,
whether there's a prior promise.

## What NOT to build yet

- No tools, no policy engine, no LangGraph, no API routes beyond a `GET
  /health` sanity check. Resist the urge to stub these — a stub you forget
  about is worse than a missing file, because it looks done.

## Acceptance checklist

- [ ] `docker-compose up` gets Postgres running with zero manual steps
- [ ] Migrations run clean from empty DB
- [ ] Seed script produces 20-30 cases with all six archetypes represented at
      least twice each
- [ ] Every table above exists with the FKs and constraints described
- [ ] Case status is enum/CHECK-constrained, not free text
- [ ] The idempotency-key uniqueness constraint exists at the DB level (even
      though no write tool exists yet to test it against)
- [ ] `GET /health` returns 200 against the seeded DB

## Hand back to me

Show me: the `CREATE TABLE` (or equivalent ORM model) for `cases` and
`audit_log` specifically, the idempotency constraint you chose, and the
distribution of archetypes in the seeded data (a quick count per archetype).
I want to sanity-check the schema before anything gets built on top of it.
