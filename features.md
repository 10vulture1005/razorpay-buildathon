# Features — Revenue Recovery Autopilot

B2B receivables recovery agent: **AI proposes, deterministic code decides.**
FastAPI backend (`app/`) · Next.js dashboard (`frontend-next/`) · Postgres/SQLite ledger.

---

## 1. Agent core — bounded recovery loop

- Bounded state-machine graph per case: `build_context → diagnose → select_action → policy_check → execute_action → observe_outcome → check_stopping_rules` (app/agent/graph.py).
- **LLM diagnosis** of why a case is unpaid (forgot / cashflow issue / dispute / unwilling / stale mandate…) via Pydantic-enforced structured output (`DiagnosisResult`).
- **Intervention selection** (`InterventionChoice`) with net-expected-value computed in code — never trusted from model text.
- Stopping rules bound retries; exhaustion produces a clean `ESCALATED`, never a crash.
- Terminal outcomes: `RECOVERED`, `STOPPED`, `ESCALATED`; `AWAITING_OUTCOME` parking (promise holds, contact hours).
- `reasoning` fields are display-only — control flow reads structured enums only (test-guarded).

## 2. Policy engine — the deterministic gate

- YAML-configured rules (app/policy/policy_config.yaml): opted-out customers, max retries, recovery-window expiry, contact-hours enforcement (IST), daily outreach caps, minimum escalation value.
- Every decision audited (`allowed` AND rejected) with reason + config snapshot.
- "Refused" routes to escalate or stop; "The model proposes, this decides."

## 3. Write tools — idempotent, audited actions

- `send_reminder`, `send_payment_link` (Razorpay link + email), `record_promise_to_pay`, `escalate_to_human`.
- Explicit idempotency keys (`case:action:attempt`) checked against the DB; result + audit row committed in one transaction.
- Global kill switch (`WRITE_TOOLS_ENABLED`) and retryable typed errors.

## 4. Event ingestion

- `POST /events/invoice-overdue` — auto-registers unknown customers/invoices, accepts `days_overdue` (clamped so cases never arrive pre-expired), keeps freshest contact info.
- `POST /events/payment-failed` — failed-payment recovery track.
- `POST /webhooks/payment` — HMAC-verified Razorpay payments flip cases to `RECOVERED`.
- `POST /cases/{id}/simulate-payment` — demo/testing hook.

## 5. Failed payment recovery (Phase 7)

- Same graph reused for failed subscriptions/payments: `retry_payment`, `update_payment_method_prompt`, dunning emails — all idempotent write tools.

## 6. Email — multi-provider delivery + inbound replies

- Provider-switched adapter (EMAIL_PROVIDER): `smtp`, `resend`, `sendgrid`, `mailgun`, plus dev-only `console` echo (refused in production).
- **Inbound replies**: signature-verified Mailgun webhook matches sender to an open case and LLM-classifies intent — grants extensions or escalates disputes deterministically.

## 7. Human-in-the-loop escalation

- Policy denials, stopping-rule exhaustion, disputes, and errors open `EscalationTicket`s.
- Ops resolves via `GET /tickets` + `POST /tickets/{id}/resolve`; resolution audited as actor=`human`.

## 8. Recovery Copilot (chat)

- `POST /chat` — context-grounded copilot over a fresh DB snapshot each turn (metrics, funnel, top open cases, policy rejections, recent audit activity, focused-case trail). Stateless; no conversation memory.
- Answers may attach validated chart specs (bar / line / pie) rendered in the UI.
- **Human-gated email drafting**: ask the copilot to email someone and it returns an editable draft (`email_draft`). The draft renders as an editable To/Subject/Body card in chat; nothing is sent until you hit **Confirm & send**, which calls `POST /chat/send-email` (scope `run`) — delivered through the real email adapter and audited as actor=`human`. Discard leaves no trace. The copilot itself never sends mail.

## 9. Metrics & observability

- `GET /metrics/recovery`, `/metrics/funnel`, `/metrics/activity`, `/metrics/timeline`, `/cases/{id}/audit`.
- Structured JSON logging with correlation IDs; provider status banner (live vs mock); per-call LLM usage/cost accounting.

## 10. Dashboard frontend (Next.js)

- **Overview** — money-first metrics (at risk / recovered / rate / active), recovery funnel, live agent activity feed, 14-day timeline; rejections shown as prominently as approvals; polling keeps it live.
- **Case detail** — diagnosis, actions tried, policy decisions, outcome, full ordered audit trail.
- **Tickets** — open escalations with company history; one-click resolve.
- **Chat** — the Recovery Copilot with focus-case picker, suggestion prompts, inline charts, and the email-draft confirm flow.
- **Merchant sandbox** — simulate overdue invoices (with days-overdue) and payments against the live API.
- API-key security stays server-side: Next.js middleware injects `x-api-key` into `/api/*` proxies; friendly error surfacing from backend details.

## 11. Security & production hardening

- Scoped API keys (`read` / `run` / `admin`) on every route; HMAC verification for payment webhooks; signature verification for Mailgun webhooks.
- Production guards refuse mock LLM/email/payment adapters when `ENVIRONMENT=prod`.
- Graceful degradation everywhere: malformed charts/drafts coerce instead of failing replies; LLM failures retry then fall back — never silent defaults.
- LLM providers: OpenRouter primary (any model, tiered frontier/small) + NVIDIA fallback client armed when configured; deterministic MockLLM for dev/tests.

## 12. Evals & tests

- Adversarial suite: prompt injection cannot forge recoveries; reasoning never branches control flow.
- Hermetic test setup (mock providers, pinned policy clock, seeded synthetic data); 99 tests covering tools, policy, agent phases, API adversarial cases, failed payments, production hardening.
