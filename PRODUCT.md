# PRODUCT.md

> Inferred entirely from the repo's own build plan (`plan.md`, `00-README.md`) and the
> existing implementation — no human interview was conducted; assumptions are labeled.

## Product

**Revenue Recovery Autopilot** — a B2B receivables recovery agent MVP. An AI agent chases
overdue invoices (reminders, payment links), a deterministic policy engine gates every action,
and everything the agent does lands in a full audit trail.

## Audience & scene

A technical demonstrator audience: engineering/product evaluators watching a live demo
(the "demo day" scenario in plan.md), and internal ops staff reviewing agent decisions.
Used on desktop in a well-lit room, projected or screen-shared. Secondary: quick checks on
a laptop browser between meetings.

## Job to be done

- **Demo viewers** must *see* the safety story: "AI proposes, code decides." The policy
  rejection log is the headline artifact.
- **Ops reviewers** must answer "why did the agent do X on this case?" in seconds via the
  case detail + audit trail.

## Surfaces

1. **Revenue Overview** — money metrics first (At Risk, Recovered, Recovery %, Active).
2. **Recovery Funnel** — at-risk → eligible → automated actions → recovered → escalated.
3. **Agent Activity** — live feed of audit events; rejections must be as visible as approvals.
4. **Case Detail** — diagnosis, actions, policy decisions, outcome, full ordered audit trail;
   agent `reasoning` always labeled as stated-rationale-only, never decision input.

## Constraints

- Business metrics lead; model metrics are supporting detail.
- Zero hardcoded demo numbers — all data live from the FastAPI API (`/metrics/*`, `/cases*`).
- Live-demo moment: inserting a payment must visibly move the feed within seconds (polling ≤ 5s).
- Empty states must teach, not blank out. No numbers computed client-side that duplicate server logic.
- Mode: **Operate** — scanability, earned familiarity, density where useful; brand lives in precise details.
