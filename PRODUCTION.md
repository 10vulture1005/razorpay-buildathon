# Revenue Recovery Autopilot — System Documentation & Production Plan

**Status of this document:** complements `README.md` (what is built and
verified today) and `DEPLOY.md` (the deploy playbook). This file has two halves:

- **Part A — System Documentation:** what the system is, how it works, how to run it.
- **Part B — Production-Readiness Plan:** gap analysis of the current MVP vs. a deployable
  service, and a phased plan to close each gap.

---

# Part A — System Documentation

## A.1 What this system does

Revenue Recovery Autopilot is an agentic B2B receivables recovery service. When an invoice
goes overdue, the system:

1. Builds a decision-relevant context for the case (amount, days overdue, payment history,
   promises made/broken, recent messages).
2. **Diagnoses** why the invoice is unpaid (LLM, structured output only).
3. **Selects an intervention** (LLM, structured output only; net expected value computed
   in deterministic code).
4. **Gates the action through a deterministic Policy Engine** (no LLM — hard rules in
   YAML-configured Python).
5. Executes the action through **idempotent write tools** (reminder, payment link,
   escalation, recovery-marking).
6. Observes the outcome (verified payment) and loops with a **bounded attempt budget**,
   terminating in exactly one of `RECOVERED`, `ESCALATED`, or `STOPPED`.

Everything lands in a queryable audit trail. Failed-payment recovery (Phase 7) runs through
the same graph and policy engine, proving the architecture generalizes.

**The one rule that holds the whole system together:** the LLM proposes, code validates and
executes. `reasoning` fields are audit-trail only and are never branched on — enforced by
tests, not convention.

## A.2 Architecture

```
Client (Next.js :3000) ──/api proxy──▶ FastAPI :8000
                                        │ routes_core  (events, cases, agent/run)
                                        │ routes_metrics (audit, metrics, funnel, feed)
                                        ▼
                                   Agent graph (bounded state-machine loop)
                                        │
              ┌─────────────────────────┼─────────────────────────┐
              ▼                         ▼                         ▼
     LLM layer (structured       Deterministic core          Tools layer
     output only, Pydantic)      (zero LLM)                  (only data seam)
     · diagnose                  · PolicyEngine              · read_tools
     · select_action             · check_stopping_rules      · write_tools (idempotent)
                                        │                         │
                                        ▼                         ▼
                              audit_log (every decision)    PostgreSQL
                            (allowed AND rejected +      cases · customers · invoices
                             config_snapshot per          promises · subscriptions
                             decision)                    tool_executions (idempotency PK)
                                                          payment_events · retry_events
```

Case lifecycle (per case):

```
ingest → build_context → diagnose → select_action → policy_check
            ▲                                              │
            │                                    allowed ──┤
            │                                              ▼
      within limits ◀── check_stopping_rules ◀── observe_outcome ◀── execute_action
            │
      exhausted ──▶ ESCALATED (terminal, human review)
```

Terminal states: `RECOVERED` (verified payment observed), `ESCALATED` (policy refusal with
escalation, or attempt budget exhausted), `STOPPED` (opted-out, daily cap, agent chose stop).

## A.3 Components

| Component | Location | Responsibility |
|---|---|---|
| Domain + tool/LLM schemas | `app/models/` | One place for all Pydantic contracts (`DiagnosisResult`, `InterventionChoice`, `CaseState`, tool I/O). |
| DB tables + session | `app/db/` | SQLAlchemy models; `tool_executions.idempotency_key` is a **primary key** — double-sends are structurally impossible. |
| Read tools | `app/tools/read_tools.py` | Typed, read-only fetchers; the graph reads data only through these. |
| Write tools | `app/tools/write_tools.py` | Reminder, payment link, promise, escalation, `mark_recovered`. Each is idempotent on `case_id:action:attempt_number`, executed + audited in one transaction. |
| Failed-payment tools | `app/tools/failed_payment_tools.py` | Phase-7 extension set through the same graph. |
| Policy Engine | `app/policy/policy_engine.py` + `policy_config.yaml` | Deterministic gate: max retries, recovery window, daily message cap, opt-out, escalation value threshold. Logs allow AND reject with the full config snapshot. |
| Agent graph | `app/agent/graph.py`, `app/agent/nodes/` | Bounded loop: conditional edges only (no recursion-limit crashes); `attempt_count` re-checked before every re-entry. |
| LLM client | `app/agent/llm.py` | `LLM_PROVIDER=mock` (deterministic, key-free, used by tests) or `openrouter`. Schema-validation failure → one retry → raise → clean `ESCALATED`. Never falls through with defaults. |
| Outcome poller | `app/workers/outcome_poller.py` | Interval mode (demos) and sync mode (batches); calls `mark_recovered` only for verified payments. |
| API | `app/api/routes_core.py`, `routes_metrics.py` | Events intake, case queries, manual agent run, payment simulation, audit feed, metrics/funnel. |
| Evals | `app/evals/eval_runner.py` | Trace-level checks: schema validity, policy compliance (violation rate must be 0), tool success, adversarial cases (prompt injection, duplicate events, malformed context). |
| Migrations | `migrations/` (Alembic) | Startup handles empty DB → `upgrade head`, legacy `create_all` DB → `stamp head`, versioned DB → upgrade. |

## A.4 Non-negotiable invariants

1. **No free text into policy or execution.** Every LLM call returns Pydantic-validated
   structured output. `reasoning` never branches control flow (test-enforced).
2. **Policy and stopping rules are plain Python.** Zero LLM calls. An agent "arguing around"
   a policy block is a stop-the-line bug; policy violation rate must stay at exactly 0.
3. **All writes are idempotent** on `case_id:action:attempt_number`, enforced by a DB primary
   key, committed with the audit row in one transaction.
4. **State lives in Postgres**, never the context window. The Context Builder assembles
   exactly eleven decision-relevant fields (explicit list test-enforced) — never the audit log.
5. **The loop is bounded by an explicit counter in state.** Exhaustion → clean `ESCALATED`,
   never a crash or hung case.
6. **`mark_recovered` requires verification** — `verified_by` plus a real backing payment row;
   a hallucinating LLM cannot mark money recovered.

## A.5 API surface

| Method & path | Purpose |
|---|---|
| `POST /events/invoice-overdue`, `POST /events/payment-failed` | Case intake events |
| `GET /cases`, `GET /cases/{id}` | List/filter and fetch case detail |
| `POST /agent/run/{case_id}` | Run the graph for a case (one loop turn) |
| `POST /cases/{id}/simulate-payment` | Demo-only: insert a verified mock payment |
| `GET /cases/{id}/audit` | Full ordered audit trail for the case |
| `GET /metrics/recovery`, `GET /metrics/funnel`, `GET /metrics/activity` | Business metrics, funnel, live agent feed |
| `GET /health` | Liveness |

Configuration (env): `DATABASE_URL`, `LLM_PROVIDER` (`mock`|`openrouter`),
`OPENROUTER_API_KEY` / `_MODEL` / `_BASE_URL` / `_TIMEOUT_S`,
`ENVIRONMENT`, `EMAIL_PROVIDER` (smtp | resend | sendgrid), `PAYMENT_PROVIDER=razorpay`, `RAZORPAY_KEY_ID`/`RAZORPAY_KEY_SECRET`/`RAZORPAY_WEBHOOK_SECRET`. See `.env.example`.

## A.6 Running locally

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
export DATABASE_URL='postgresql+psycopg2://postgres:postgres@127.0.0.1:5432/recovery'

.venv/bin/python -m pytest tests/ -q                     # 76 tests across phases
.venv/bin/python -m scripts.run_full_batch --fresh       # seeded 24-case synthetic batch
.venv/bin/uvicorn app.main:app --port 8000               # API + legacy /dashboard
cd frontend-next && npm install && npm run dev           # Next.js dashboard on :3000
```

Verified latest batch: 24 cases, ₹79.4L at risk, 26.1% recovered, 0 policy violations,
100% diagnosis validity, 100% tool success (README has the per-archetype breakdown).

---

# Part B — Production-Readiness Plan

> ## Implementation status (2026-08-23)
>
> **P0 — Security floor: DONE.**
> - Secrets scrubbed from `.env` / `.env.example` (placeholders only); gitleaks in CI
>   (`.github/workflows/ci.yml`, `.gitleaks.toml`). ⚠️ The previously committed OpenRouter
>   key must still be **rotated** out-of-band.
> - API-key auth with SHA-256-hashed keys and `read`/`run`/`admin` scopes on every route
>   (`app/security/auth.py`, `Depends(require_scope(...))`); admin is a scope superset.
> - `simulate-payment` double-gated: admin scope AND `ENVIRONMENT != prod`.
> - CORS allowlist, 64 KB body cap, per-key/IP sliding-window rate limiter (429),
>   sanitized global exception handler with correlation IDs; prod startup refuses to boot
>   without explicit `API_KEYS`; `/docs` disabled in prod.
> - Containerized: multi-service `docker-compose.yml` (db → migrate → api + worker),
>   non-root Dockerfile, `/readyz` healthcheck.
>
> **P1 — Operable service: core items DONE.**
> - Payment webhook `POST /webhooks/payment`: HMAC-SHA256 verification + replay dedupe via
>   the `tool_executions` ledger (nullable `case_id` records unmatched invoices too).
> - Startup auto-migrate REMOVED for Postgres (fail-fast schema check instead);
>   explicit `python -m scripts.migrate upgrade head`; index migration
>   (`audit_log(case_id/event_type)`, `cases(status)`, `tool_executions(case_id)`).
> - Structured JSON logs + `X-Request-ID` correlation middleware (`app/observability/`).
> - LLM usage (model/tokens/est-cost) + prompt version recorded in every diagnosis /
>   action_selected audit row; policy config carries a releaseable `version` in snapshots.
> - India contact-hours rule (08:00–19:30 IST window in `policy_config.yaml`): outbound
>   sends outside the window PARK the case (`AWAITING_OUTCOME` + resume-after audit row),
>   never terminate it; internal actions exempt. Eval/batch runners pin a business-hours
>   clock so simulations are deterministic.
> - `WRITE_TOOLS_ENABLED=false` kill switch parks all outbound sends as retryable failures
>   (B3 rollback flag).
> - CI gate: full pytest + policy-violation-rate==0 assertion + alembic autogenerate-drift
>   check.
>
> Still open: queue-based async execution (P1-1), real channel adapters (P1-3), OTEL
> traces + Prometheus/alerts (rest of P1-5), all of P2.

Today's system is a **verified demo-grade MVP**: the safety architecture (policy gating,
idempotency, bounded loops, structured output) is production-shaped and test-backed, but the
deployment surface is not. The gaps below are ranked by blast radius, then by effort.

> ⚠️ **Immediate action (P0-0):** `.env.example` currently contains what appears to be a
> **live OpenRouter API key**. Rotate it now, purge it from git history and the example file
> (`OPENROUTER_API_KEY=sk-or-...` placeholder), and add `.env.sequence`/secret-scanning
> (gitleaks) to CI. Secrets belong in a manager, never in any `*.example` file.

## B1. Gap analysis (current → required)

| # | Area | Current | Production requirement | Phase |
|---|---|---|---|---|
| 1 | **Secrets** | Real-looking key committed in `.env.example` | Rotate; secrets manager (AWS SM/Vault); no secrets in repo; secret scanning in CI | P0 |
| 2 | **AuthN/AuthZ** | Every endpoint is unauthenticated, including `simulate-payment` and `agent/run` | API-key/OAuth2 machine auth for services + SSO/JWT for humans; RBAC scopes (read vs. run vs. simulate); `simulate-payment` disabled outside `ENVIRONMENT=dev` | P0 |
| 3 | **CORS / network** | No CORS middleware, Next.js proxy open | Explicit allowlist origins; terminate TLS at LB; internal services on private subnets | P0 |
| 4 | **Input hardening** | Pydantic validation only | Request size limits, rate limiting (per-key and per-IP), structured error responses without internal detail, audit logging of rejected/invalid requests | P0 |
| 5 | **Runtime model** | Synchronous graph runs inside FastAPI request handler; in-process interval poller | Job queue (SQS / Redis Streams / Celery) — `agent/run` enqueues, workers consume at-least-once (safe: idempotency has DB teeth); poller becomes a worker or is replaced by webhooks (§7) | P1 |
| 6 | **Real integrations** | All write tools are mock-shaped adapters to Postgres | Real channels behind the same tool contracts: email (SES/Postmark), SMS/WhatsApp (TRAI-DLT-registered templates — mandatory in India), payment links (Razorpay API), ticketing (Jira/Freshdesk). **The tool contracts and idempotency design do not change — only adapters swap.** | P1 |
| 7 | **Outcome observation** | Poller reads `payment_events` | Webhook ingestion from the payment gateway with HMAC signature verification, replay-deduplication (feed through `tool_executions`), and a fallback reconciliator job | P1 |
| 8 | **LLM layer** | Single global provider (`mock`/`openrouter`); no per-task model routing, no fallbacks | Model routing per the original plan §2.1 (small model for classification/summarization, frontier for diagnosis/drafting); provider fallback; timeout + circuit-breaker; per-case LLM cost and token tracking in audit log (needed for the Recovery Cost metric to be real) | P1 |
| 9 | **Observability** | Print-level logging; DB-backed audit for agent decisions only | Structured JSON logs with correlation ID (`case_id`); OpenTelemetry traces (spans per graph node and tool call); RED metrics per endpoint; alerts on: policy-violation ≠ 0 (page immediately), structured-output-failure rate, queue depth, escalation backlog aging | P1 |
| 10 | **DB operations** | Alembic starter + startup auto-migrate | Remove `upgrade head` from app startup (migrations become an explicit CI/CD step — auto-migrating multi-replica startups race); index audit on hot queries; audit_log retention + archival; PII encryption at rest; backups + restore drill | P1 |
| 11 | **Compliance & safety policy** | Generic caps (messages/day, window) | India-specific collections rules in `policy_config.yaml`: contact-hours windows (RBI fair-practice norms: no outreach 19:00–08:00), DND registry checks, verified opt-out propagation, immutable append-only audit (agent decisions must be reproducible in a dispute — keep model+prompt+config version in each audit row — today only config_snapshot is stored) | P1 |
| 12 | **Delivery guarantees** | Fire-and-forget writes | DLQ + retry with backoff for outbound channel calls; delivery-status callbacks updating attempt outcomes; never count a message as "sent" on enqueue | P2 |
| 13 | **Scale & tenancy** | Single tenant, synchronous everything | Per-tenant policy configs and data isolation (`tenant_id` on all tables or schema-per-tenant); horizontal API + worker scaling; per-tenant LLM spend caps | P2 |
| 14 | **Testing/CI/CD** | 43 local tests, manual migration check | CI gate: full pytest + eval suite + `policy violation rate == 0` + migration-diff-empty check on every PR; staged rollouts: policy config changes ship behind a canary + instant rollback (config versions are already logged, so rollback is auditable); blue/green for app deploys | P1 |
| 15 | **Human-in-the-loop** | `escalate_to_human` writes a ticket row | Escalation review UI with approve/reject, SLA timers, and reassignment — ops staff can act on escalations, not just view them | P2 |

**What does NOT need changing for production:** the six invariants in §A.4, the tool
contract + DB-enforced idempotency, the bounded-loop state machine, the Policy-Engine-as-
standalone-module boundary, and the audit-everything discipline. These are the parts that
were designed production-shaped from day one — the plan below hardens around them rather
than redesigning them.

## B2. Phased plan

### Phase P0 — Security floor (Week 1-2) — "safe to expose at all"
Goal: nothing in this repo can leak a secret, be hit by an anonymous caller, or run demo
endpoints in production.

1. Rotate the exposed OpenRouter key; scrub `.env.example`; add gitleaks to CI.
2. FastAPI dependency-injected auth: `X-API-Key` (hashed, env-managed) for machine callers;
   scopes `read`, `run`, `admin`. All routes gain `Depends(require_scope(...))`.
3. Gate `simulate-payment` behind `ENVIRONMENT != "prod"` *and* `admin` scope.
4. `CORSMiddleware` with an explicit origin allowlist; request size cap; slowapi or
   equivalent rate limiter; global exception handler returning sanitized error bodies.
5. Containerize the backend + worker (extend `docker-compose.yml` first — it's already
   there for Postgres), add health/readiness endpoints.

**Exit criteria:** unauthenticated requests → 401; `simulate-payment` returns 403 in
`ENVIRONMENT=prod` even with a valid read-scope key; CI blocks a PR that reintroduces a
secret-shaped string; `pytest` still green (auth-related tests added).

### Phase P1 — Operable service (Week 3-6) — "safe to run on real customers"
Goal: async execution, real channels, real money-events, visibility.

1. **Move execution off the request path.** `POST /agent/run/{id}` → enqueue job
   `{case_id, attempt, enqueued_at}`; worker consumes with at-least-once semantics
   (idempotency key design already tolerates redelivery). Start with Redis Streams or SQS;
   keep the in-process path behind a `SYNC_EXECUTION=true` dev flag used by tests.
2. **Webhook endpoint for payment events** (`POST /webhooks/payment`): HMAC verification
   → dedupe via `tool_executions`-style ledger → insert verified payment row → existing
   poller/`mark_recovered` logic consumes it unchanged. Reconciliator cron backstops missed
   webhooks.
3. **Real channel adapters behind existing contracts** (`send_reminder`, `send_payment_link`,
   `escalate_to_human`): email first, then DLT-registered SMS. Adapter failures retry with
   backoff into a DLQ; delivery callbacks update outcomes.
4. **LLM tiering + resilience:** router selects small/frontier per node
   (`MODEL_FRONTIER`/`MODEL_SMALL` already exist in env); provider fallback on 5xx/timeout;
   record tokens + estimated cost per call in the audit row payload.
5. **Observability:** structlog JSON logs + `case_id` correlation; OTEL traces; Prometheus
   metrics; alert definitions for the four P0 signals in B1-9.
6. **DB ops:** startup auto-migration removed; migrations run as a pipeline step; indexes on
   `audit_log(case_id)`, `cases(status)`, `tool_executions(case_id)`; audit retention policy
   + export job.
7. **Compliance policy pack:** contact-hours window, DND check stub, opt-out hardening —
   all as additions to `policy_config.yaml` with unit tests at every boundary (same pattern
   as Phase-2 tests).
8. **CI/CD:** PR gate = pytest + eval runner + policy-violation-zero assertion + empty
   autogenerate-diff check; blue/green deploy; policy config versioned and release-able
   independently of app code.

**Exit criteria:** a real invoice-overdue event flows end-to-end through the queue to a
real email send; a Razorpay-staging webhook flips the matching case to `RECOVERED`;
policy-config change deploys without app redeploy; traces show one span per graph node;
eval suite runs on green.

### Phase P2 — Scale & recourse (Week 7-10) — "safe to grow"
1. Escalation review UI (approve/reject/reassign) with SLA timers fed by the metrics API.
2. Multi-tenancy: `tenant_id` column + row-level scoping, per-tenant policy configs,
   per-tenant LLM spend caps and dashboards.
3. Outcome-based optimization: A/B intervention strategies per segment, with policy engine
   as the immutable outer constraint (bandit chooses among *allowed* actions only).
4. Capacity: load-test 10k concurrent cases; worker autoscaling on queue depth; Postgres
   connection pooling tuning + read replica for metrics endpoints.
5. DR: documented RPO ≤ 1h (WAL/archive), RTO ≤ 30m, quarterly restore drill.

## B3. Production rollout strategy

| Stage | Traffic | LLM provider | Write tools | Gate to next stage |
|---|---|---|---|---|
| 1. Shadow | Real events ingested, **no actions sent** (agent decisions logged as "would-do") | Real (tiered) | Disabled at adapter exit | 2 weeks, 0 policy violations, eval parity with batch |
| 2. Canary | Opted-in friendly customers, low amounts | Real | Email only, daily cap 1 | 2 weeks, recovery rate ≥ shadow predictions ±15% |
| 3. General | Full segment | Real | Email + SMS | On-call runbook complete; rollback tested |

Rollback at any stage = flip `WRITE_TOOLS_ENABLED=false` (a runtime config flag to be added
in P1-3) — pending cases park in `AWAITING_OUTCOME`, no state is lost, nothing else needs
to roll back.

## B4. Definition of "production ready" (acceptance checklist)

- [ ] Zero secrets in repo; gitleaks green in CI
- [ ] All endpoints authz-scoped; demo endpoints inert in prod
- [ ] Every customer-facing action passes through Policy Engine with config snapshot logged
- [ ] Payment events arrive via verified webhooks; reconciliator drifts < 1%
- [ ] Policy violation rate = 0 enforced as a CI assertion and a paging alert
- [ ] Every audit row reproducible: model version + prompt version + config version recorded
- [ ] p95 `agent/run` job completion < 60s at 10k pending cases
- [ ] RTO/RPO drill passed once
- [ ] India outreach compliance rules live in policy config with boundary tests
- [ ] Runbook: escalation paging, LLM provider outage (falls back, cases park cleanly),
      DB failover

## B5. Effort summary

| Phase | Calendar estimate | Headline risk |
|---|---|---|
| P0 Security floor | 1–2 weeks | Secret rotation breaking existing keys (coordinate before rotation) |
| P1 Operable service | 3–4 weeks | DLT template registration lead time (SMS) — start paperwork at P0 |
| P2 Scale & recourse | 3–4 weeks | Bandit optimization must never bypass the policy engine — keep it as an outer gate |

The safety core is done and proven. Everything in this plan is standard service hardening
*around* that core — which is exactly the position the original architecture was designed
to put you in.
