# explain.md — Revenue Recovery Autopilot

A plain-language walkthrough of this AI agent: what it does, where it fits, why you'd
use it, and how its design beats the alternatives.

---

## 1. What is this?

**Revenue Recovery Autopilot** — a B2B receivables-recovery AI agent. When an invoice
goes unpaid (or a payment fails), the agent:

1. **Diagnoses** *why* it's unpaid — forgotten, cash-flow crunch, disputed, unwilling,
   stale mandate… — using an LLM that must return a strict structured schema.
2. **Selects an intervention** — reminder, payment link, promise recording, escalation —
   with net-expected-value (`amount × probability − cost`) computed **in code**, never
   trusted from model text.
3. **Asks permission** from a deterministic Policy Engine before doing anything.
4. **Executes** through idempotent, audited write tools.
5. **Observes outcomes** (verified payments via webhook/poller) and either closes the
   case as `RECOVERED` or loops back under hard stopping rules.

The one-line philosophy: **"AI proposes, deterministic code decides."**

---

## 2. How it works (architecture)

```
Event (invoice-overdue / payment-failed)
        │
        ▼
Agent graph (bounded state machine, app/agent/graph.py)
   build_context → diagnose → select_action → policy_check → execute_action → observe_outcome
        │                                   │
        │ LLM (structured output only)      ▼
        │                          PolicyEngine (zero LLM, YAML rules)
        │                                   │ allowed / refused(escalate) / refused(stop)
        ▼                                   ▼
   Write tools ── idempotency ledger + audit_log (Postgres, one transaction each)
```

Key components:

| Piece | What it does |
|---|---|
| **Agent graph** | Bounded per-case loop; `attempt_count` guarantees exhaustion ends in a clean `ESCALATED`, never an infinite loop or crash |
| **LLM layer** | Two jobs only: diagnose and pick action. Every response is Pydantic-validated; free-text `reasoning` never branches control flow |
| **Policy Engine** | Plain Python + YAML config: opted-out customers, max retries, contact-hours window (08:00–19:00 IST), daily caps, minimum escalation value. Zero LLM calls |
| **Write tools** | `send_reminder`, `send_payment_link`, `record_promise_to_pay`, `retry_payment`, `escalate_to_human` — idempotent on `case:action:attempt`, enforced by DB primary key |
| **Audit log** | Every diagnosis, policy decision (allowed *and* rejected), and action recorded with reason + config snapshot |
| **Dashboard** | Next.js UI: money-first metrics, live activity feed, case audit trails, escalation tickets, chat copilot with human-gated email drafting |

Terminal states are explicit: `RECOVERED`, `ESCALATED` (a human takes over), or
`STOPPED`. A hallucinating LLM cannot mark money recovered — `mark_recovered` requires a
real backing payment row plus `verified_by="payment_poller"`.

Verified on 24 synthetic cases across 6 archetypes: ₹20.7L recovered of ₹79.4L at risk
(26.1%), **zero policy violations**, 54% automation rate.

---

## 3. Where is it used?

- **B2B SaaS / billing teams** chasing overdue invoices automatically instead of a human
  reading aging reports and copy-pasting reminder emails.
- **Subscription businesses** recovering failed card payments / dunning lapsed mandates
  (Phase 7 reuses the exact same graph — proving extensibility).
- **Finance ops** as the safety-first control room: every agent action is visible,
  explainable, and reversible (global kill switch).
- **As a reference architecture** for any domain where you want an LLM agent to act in
  the real world but can't tolerate unsupervised autonomy — collections is just the
  first application.

---

## 4. Why use it? (the problem it solves)

Manual receivables recovery scales badly: humans forget follow-ups, apply inconsistent
tone, don't know which cases deserve attention first, and have no audit trail. Pure-LLM
"autopilots" fix throughput but introduce a worse problem — an unpredictable model
emailing your customers and claiming money was paid when it wasn't.

This project resolves that tension:

1. **Safety is structural, not prompt-hoped.** The LLM literally cannot send anything;
   only deterministic code passes actions to tools.
2. **Idempotency has database teeth.** The idempotency key is a Postgres primary key —
   crash mid-send and replay returns the stored result. Double-sending a customer is
   structurally impossible, not just discouraged.
3. **Full accountability.** "Why did the agent do X?" is answered in seconds via the
   ordered audit trail, including the exact policy-config snapshot at decision time.
4. **Graceful failure everywhere.** Bad LLM output escalates to a human; malformed
   input parks the case; out-of-contact-hours sends wait instead of violating compliance.
5. **Human-in-the-loop by design.** Disputes, opt-outs, exhausted budgets, and copilot
   email drafts all require a human click before anything leaves the building.

---

## 5. How it's better than the alternatives

### vs. naive LLM agents ("give GPT tools and let it run")

| Naive agent | This project |
|---|---|
| Free text drives decisions | Only validated enum fields branch; `reasoning` is display-only (test-enforced) |
| Can loop forever / crash | Explicit `attempt_count` bound → clean `ESCALATED` |
| Duplicate sends on retry/crash | DB-enforced idempotency keys |
| Can self-report success | Recovery requires a verified payment row |
| Unexplainable | Every decision audited with config snapshot |
| Prompt injection may trigger real actions | Adversarial tests prove injected text cannot forge recoveries |

### vs. traditional dunning software / rule-based SaaS

Rule-based tools blast fixed sequences ("day 7: email #2") regardless of *why* the
invoice is unpaid. Here the LLM diagnoses cause first, so a cash-strapped customer gets
a patient extension while an unwilling one gets firm escalation — and expected-value math
in code decides whether acting is even worth it. Rules still exist, but as guardrails,
not brains.

### vs. fully-managed collections agencies / human teams

- ~54% of cases resolved with zero human touch; humans only see genuine disputes and
  high-stakes escalations (29%).
- Consistent, compliant behavior (contact-hour windows, daily caps) that never has a bad day.
- Marginal cost near zero per additional case; the same graph extends to failed-payment
  recovery without modification.

### The core differentiator

Most agent demos optimize for what the model *can* do. This one optimizes for what it's
*allowed* to do — and proves it with 99 tests including adversarial suites, a CI gate
asserting **policy-violation rate == 0**, and an audit trail that reconstructs any
decision after the fact. That's the difference between an impressive demo and something
you'd actually point at your customers' inboxes.

---

## 6. Quick tour of the repo

```
app/agent/       graph.py — the bounded recovery loop; llm.py — swappable providers
app/policy/      PolicyEngine + policy_config.yaml — the deterministic gate
app/tools/       read/write tools, idempotency ledger integration
app/api/         REST routes (events, cases, metrics, audit, chat)
app/workers/     outcome poller (verified payments)
frontend-next/   Next.js ops dashboard + copilot chat
tests/           99 tests incl. adversarial injection & idempotency proofs
EVALS.md         eval methodology; PRODUCTION.md full system docs
```
