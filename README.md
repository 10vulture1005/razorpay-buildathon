# Revora — Revenue Recovery Autopilot

**B2B Receivables Recovery Agent** — AI proposes, deterministic code decides and executes.
An agent diagnoses why an invoice is unpaid, picks interventions (reminders, payment links,
escalation), a hard-gated Policy Engine approves or refuses every action, and every decision
lands in a queryable audit trail. Architected to extend to Failed Payment Recovery through the
same graph — proven by Phase 7.

See `PRODUCTION.md` for full system documentation plus the production-readiness
gap analysis and phased rollout plan.

---

## Architecture

### System overview

```mermaid
flowchart LR
    subgraph Clients
        FE["Next.js dashboard :3000"]
        CURL["API clients"]
    end

    subgraph Backend ["FastAPI :8000"]
        API["REST API\nevents · cases · metrics · audit"]
        GRAPH["Agent graph\nbounded state-machine loop"]
    end

    subgraph Deterministic ["Deterministic core — zero LLM"]
        PE["PolicyEngine\nYAML-configured rules"]
        SR["Stopping rules"]
    end

    subgraph Intelligence ["LLM layer — structured output only"]
        DX["diagnose\nDiagnosisResult"]
        SA["select_action\nInterventionChoice"]
    end

    subgraph Data ["PostgreSQL"]
        CASES["cases · customers · invoices\npromises · subscriptions"]
        AUDIT[("audit_log")]
        IDEM[("tool_executions\nidempotency ledger")]
        PAY["payment_events ·\nretry_events"]
    end

    subgraph Tools ["Tools layer — the only data seam"]
        RT["read_tools"]
        WT["write_tools\nidempotent · audited"]
    end

    POLLER["outcome poller\ninterval + sync modes"]

    FE -->|"/api proxy"| API --> GRAPH
    GRAPH --> DX & SA
    DX & SA -->|Pydantic-validated| GRAPH
    GRAPH --> PE & SR
    GRAPH --> RT & WT
    RT & WT --> CASES
    WT --> IDEM
    GRAPH --> AUDIT
    PE --> AUDIT
    POLLER --> PAY --> WT
```

The load-bearing rule: **`reasoning` fields never branch anything.** Control flow reads only
structured enum fields (`likely_cause`, `action`, `allowed`) — guarded by tests.

### Agent graph (the case lifecycle)

```mermaid
flowchart TD
    INGEST["ingest_case"] --> CTX["build_context\nonly decision-relevant fields:\namount · days overdue · last 3 messages\npayment pattern · promise count"]
    CTX --> DIAG{"diagnose\nLLM structured output"}
    DIAG -->|validation fails twice| ESC
    DIAG --> SELECT{"select_action\nLLM structured output\n+ net expected value computed in code"}
    SELECT -->|"attempt_count ≥ max_retries"| ESC["ESCALATED\nterminal"]
    SELECT --> POLICY{"policy_check\ndeterministic"}
    POLICY -->|allowed| EXEC["execute_action\nidempotent write tool\nattempt++ transactionally"]
    POLICY -->|"refused + escalate"| ESC
    POLICY -->|"refused, no escalate\n(opted_out · daily cap)"| STOP["STOPPED\nterminal"]
    EXEC -->|"agent chose wait/stop"| STOP
    EXEC --> AWAIT["observe_outcome\nverified payment?"]
    AWAIT -->|yes| REC["RECOVERED\nterminal"]
    AWAIT -->|no| STOPRULES{"check_stopping_rules\ndeterministic"}
    STOPRULES -->|within limits| SELECT
    STOPRULES -->|exhausted| ESC
```

The loop is bounded by the explicit `attempt_count` column re-checked before every
`select_action` re-entry — hitting the bound produces a clean `ESCALATED`, never a crash.
LangGraph's recursion-limit pattern is replaced by plain conditional edges (FastAPI-native).

### Case lifecycle sequence

```mermaid
sequenceDiagram
    participant E as "Event (invoice-overdue)"
    participant G as "Agent graph"
    participant L as "LLM (mock/swappable)"
    participant P as "PolicyEngine"
    participant T as "Write tools"
    participant W as "Outcome poller"
    participant DB as "Postgres audit_log"

    E->>G: ingest case
    G->>DB: build_context (reads via tools only)
    G->>L: diagnose(context) → DiagnosisResult
    L-->>G: {likely_cause, confidence, reasoning}
    G->>DB: audit diagnosis
    G->>L: select_action → InterventionChoice
    L-->>G: {action, probability, reasoning}
    G->>G: net_expected_value = amount × p − cost (in code)
    G->>P: check(case, action)
    P-->>G: PolicyDecision(allowed, reason, escalate, config_snapshot)
    G->>DB: audit policy decision (allowed AND rejected)
    alt allowed
        G->>T: send_reminder(case_id, channel, attempt=1)
        T->>DB: result + idempotency key + audit row (one transaction)
        G->>W: observe_outcome()
        W->>DB: payment_events row found?
        W->>T: mark_recovered(verified_by="payment_poller")
        T-->>G: case → RECOVERED
    else refused + escalate
        G->>T: escalate_to_human(reason)
        T-->>G: case → ESCALATED
    else refused, silent
        Note over G: case → STOPPED (opted-out, daily cap)
    end
```

### Data model

```mermaid
erDiagram
    CUSTOMERS ||--o{ INVOICES : has
    CUSTOMERS ||--o{ CASES : has
    CUSTOMERS ||--o{ PROMISES : made
    CUSTOMERS ||--o{ PAYMENT_METHODS : owns
    CUSTOMERS ||--o{ SUBSCRIPTIONS : pays
    INVOICES ||--o| CASES : triggers
    CASES ||--o{ AUDIT_LOG : "full trail"
    CASES ||--o{ COMMUNICATION_MESSAGES : thread
    CASES ||--o{ ESCALATION_TICKETS : spawns
    CASES ||--o{ TOOL_EXECUTIONS : "idempotency ledger"
    CASES ||--o{ MOCK_RETRY_RESULTS : "failed-payment"
    INVOICES ||--o{ MOCK_PAYMENTS : "verified payments"

    CASES {
        string id PK
        string status "NEW→DIAGNOSED→ACTION_SELECTED→EXECUTING→AWAITING_OUTCOME→RECOVERED/ESCALATED/STOPPED (CHECK-constrained)"
        int attempt_count "explicit loop bound"
        int messages_sent_today "daily-capped"
        float amount_at_risk
    }
    AUDIT_LOG {
        int id PK
        string actor "agent | policy | human | system"
        jsonb payload "includes config_snapshot per decision"
        text reasoning "audit-only, never branched on"
    }
    TOOL_EXECUTIONS {
        string idempotency_key PK "case_id:action:attempt_number"
        jsonb result_payload "replay returns stored result verbatim"
    }
```

Idempotency has **database teeth**: `tool_executions.idempotency_key` is a primary key, so a
crash between execute-and-commit replays into the stored result — double-sends are
structurally impossible, not just discouraged.

---

## Verified batch results (24 synthetic cases)

Run: `.venv/bin/python -m scripts.run_full_batch --fresh` against PostgreSQL.

| Archetype | Cases | Outcomes |
|---|---|---|
| clean_payer | 4 | **4 RECOVERED** (reminder → payment detected → closed) |
| serial_promise_breaker | 4 | 3 STOPPED (agent chose stop), 1 ESCALATED |
| disputed_invoice | 4 | 4 STOPPED (dispute → escalate-to-human proposal refused / stop) |
| high_value_low_risk | 4 | 4 ESCALATED after exhausting the 3-attempt budget |
| low_value_high_risk | 4 | 2 ESCALATED, 2 STOPPED |
| opted_out | 4 | 4 STOPPED — **zero actions ever taken**, first-touch refusal |

| Metric | Value |
|---|---|
| Revenue at risk | ₹79,38,900 |
| Recovered | ₹20,70,000 (**26.1%** recovery rate) |
| Policy violation rate | **0** (any non-zero value is a stop-the-line bug) |
| Diagnosis validity | 100% (schema-enforced enums) |
| Tool success rate | 100% per tool (27 actions, no failures) |
| Escalation rate | 29% |
| Automation rate | 54% |

Every number above is computed live from the DB — `GET /metrics/recovery` moves when you
insert a payment during a demo.

---

## Testing

```bash
# 0. One-time setup
python -m venv .venv && .venv/bin/pip install -r requirements.txt
export DATABASE_URL='postgresql+psycopg2://postgres:postgres@127.0.0.1:5432/recovery'

# 1. Unit + integration suite (76 tests: tools idempotency, policy boundaries,
#    full-loop paths, adversarial injection/duplicate/malformed cases, API, Phase-7)
.venv/bin/python -m pytest tests/ -q

# 2. Full synthetic batch (seeds if needed, inserts verified clean-payer payments,
#    runs every case through the graph, prints the eval report)
.venv/bin/python -m scripts.run_full_batch --fresh

# 3. Migration integrity (autogenerate diff must be empty == models match migrations)
.venv/bin/alembic upgrade head && .venv/bin/alembic revision --autogenerate -m diff_check
#   → expect no "Detected" lines; delete the generated file afterwards

# 4. Live smoke test
.venv/bin/uvicorn app.main:app --port 8000 &
curl localhost:8000/health && curl localhost:8000/metrics/recovery

# 5. Dashboard
cd frontend-next && npm install && npm run dev   # http://localhost:3000 (proxies /api → :8000)
```

**What each test layer proves:** Phase 1 tests prove double-invocation side-effect freedom and
the `mark_recovered` precondition; Phase 2 tests prove every policy boundary (at/under/over
each threshold) and multi-rule ordering; Phase 3–4 tests prove the retry→escalate path, the
recovered path, opted-out zero-action guarantee, and context-window discipline (exact field
list); Phase 5 tests prove prompt injection can't trigger recovery, duplicate events are
idempotent, malformed context escalates gracefully, and nothing branches on `reasoning`;
Phase 7 tests prove the failed-payment use case runs through the *unmodified* graph and engine.

### Live demo script

Every route except `/health` and `/readyz` requires an API key with the right scope
(`read` / `run` / `admin`; `admin` implies both). Keys are configured via the `API_KEYS`
env var as `<secret>:<scope1,scope2>` entries — see `.env.example`.

```bash
K='dev-admin-key'
curl -X POST localhost:8000/events/invoice-overdue \
  -H "X-API-Key: $K" -H 'Content-Type: application/json' \
  -d '{"invoice_id":"inv_live_1","customer_id":"cust_clean_payer_0","amount":250000}'
curl -X POST -H "X-API-Key: $K" localhost:8000/agent/run/case_inv_live_1   # reminder goes out
curl -X POST -H "X-API-Key: $K" localhost:8000/cases/case_inv_live_1/simulate-payment  # verified payment (dev only)
curl -H "X-API-Key: $K" localhost:8000/cases/case_inv_live_1/audit         # watch the trail close the loop

# Payment gateway webhook (HMAC-SHA256 of the raw body over PAYMENT_WEBHOOK_SECRET):
SIG=$(printf '{"event_id":"evt_1","invoice_id":"inv_live_1","amount_paid":250000}' \
  | openssl dgst -sha256 -hmac "$PAYMENT_WEBHOOK_SECRET" -hex | cut -d' ' -f2)
```

### Production hardening (implemented)

- **AuthN/AuthZ** — hashed API keys + scopes on every route; unauthenticated → 401,
  wrong scope → 403. `simulate-payment` additionally refuses in `ENVIRONMENT=prod`.
- **Edge hygiene** — CORS allowlist, 64 KB body cap, per-key/IP rate limiting (429),
  sanitized error bodies, structured JSON logs with `X-Request-ID` correlation.
- **Payment webhooks** — HMAC-verified, replay-deduplicated via the idempotency ledger.
- **Compliance** — India contact-hours rule (08:00–19:00 IST): out-of-window outbound
  sends park cases (`AWAITING_OUTCOME`) instead of terminating them.
- **Rollback** — `WRITE_TOOLS_ENABLED=false` parks all outbound sends instantly.
- **Audit reproducibility** — every LLM-driven decision records model usage, prompt
  version, and policy-config version.
- **CI** — gitleaks secret scan, full pytest, policy-violation-rate==0 assertion,
  alembic autogenerate-drift check (`.github/workflows/ci.yml`).
- **Migrations are explicit** — startup never auto-migrates Postgres; run
  `python -m scripts.migrate upgrade head` (the compose `migrate` service does this).

---

## Database migrations (Alembic)

Schema changes go through Alembic — never edit tables by hand:

```bash
export DATABASE_URL='postgresql+psycopg2://postgres:postgres@127.0.0.1:5432/recovery'
.venv/bin/alembic upgrade head                              # apply
.venv/bin/alembic revision --autogenerate -m "add foo"      # new migration from model edits
.venv/bin/alembic downgrade -1                              # roll back one step
```

Migrations run as an explicit pipeline step (`python -m scripts.migrate upgrade head`,
or the `migrate` service in docker-compose) — the API no longer auto-migrates at startup;
it fail-fasts with a pointer to that command if the schema is missing. Empty DBs upgrade;
legacy `create_all` DBs (no `alembic_version`) get stamped. Tests still use `create_all`
directly (fast, disposable SQLite).

---

## Layout

```
/app
  models/       # domain state + tool/LLM structured-output schemas (one place to look)
  db/           # SQLAlchemy tables + session
  tools/        # read_tools.py · write_tools.py · failed_payment_tools.py (mock adapters)
  policy/       # PolicyEngine (deterministic, zero LLM) + policy_config.yaml
  agent/        # graph.py (bounded loop) · nodes/ · llm.py (swappable provider)
  evals/        # eval_runner.py trace-level checks + test_cases.json adversarial set
  api/          # routes_core.py (events/cases/run) · routes_metrics.py (audit/funnel/feed)
  workers/      # outcome_poller.py (interval for demos, sync for batches)
/frontend-next  # Next.js dashboard (audit-tape UI) on :3000
/migrations     # Alembic
/scripts        # seed_synthetic_data.py · run_full_batch.py
/tests          # 76 tests across phases 1–5 + 7 + production hardening
```

## Key invariants

1. No free text into the Policy Engine or Action Executor — every LLM call returns a
   Pydantic-validated schema; `reasoning` is audit-trail only.
2. `policy_check` and `check_stopping_rules` are plain Python. Zero LLM calls.
3. Every write tool is idempotent on `case_id:action:attempt_number`, enforced by a DB
   unique key, executed-and-audited in one transaction.
4. State lives in Postgres; the Context Builder assembles exactly eleven decision-relevant
   fields — never the full audit log (test-enforced).
5. The retry loop is bounded by an explicit counter in state; exhaustion produces a clean
   `ESCALATED`, never a crash.
6. `mark_recovered` requires `verified_by="payment_poller"` plus a real backing payment
   row — a hard assertion, so a hallucinating LLM cannot mark money recovered.
