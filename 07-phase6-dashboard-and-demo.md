# Phase 6 — Dashboard and Demo Polish

**Days 13-14 of 14. Depends on: Phase 5 (evals + audit API working, real
numbers available).**

## Context you need

This is the last phase before demo day. The dashboard's job is to make the
architecture's safety story *visible*, not just claimed — the source doc's
design principle (Section 10) is "AI proposes, code validates and executes,"
and this dashboard should make that separation legible to someone watching,
especially the Policy Engine's rejection log (Section 6: "here is every time
the agent wanted to act and we said no, and why" is explicitly called out as
"a strong demo artifact").

Lead with business metrics, not model metrics, per Section 9 — money
recovered and recovery rate up front, eval scores and token costs are
supporting detail, not the headline.

## What to build today

### 1. Four dashboard views (React + Tailwind, per Section 8)

**A. Revenue Overview**
- At Risk (sum of `amount_at_risk` for open cases)
- Recovered (sum from `mark_recovered` events)
- Recovery % 
- Active Cases (count, non-terminal status)
- Pull these live from `GET /metrics/recovery` (Phase 5) — no hardcoded
  numbers, no numbers computed client-side that duplicate server logic.

**B. Recovery Funnel**
Revenue at Risk → Eligible Cases → Automated Actions → Successful Recovery
→ Human Escalation. This needs a funnel-shaped query against `cases` +
`audit_log` — build the backing endpoint if Section 5's API surface doesn't
already have one that fits (it doesn't explicitly list a funnel endpoint;
add one, e.g. `GET /metrics/funnel`, and flag that you added it).

**C. Agent Activity (live feed)**
Real-time-feeling feed of recent `audit_log` events across all cases:
invoice analyzed, recovery strategy selected, policy approved [or
rejected], reminder sent, promise recorded, payment detected, case closed.
- Poll `audit_log` on an interval, or use a simple SSE/websocket if you want
  it to feel live during the demo — your call, but if you're doing a live
  demo where I manually insert a `mock_payments` row, this feed needs to
  visibly update within a few seconds of that insert, since that's the
  moment in the demo that matters most.
- **Show policy rejections in this feed, not just approvals** — a feed that
  only shows things the agent successfully did undersells the exact
  guardrail story this system is supposed to demonstrate.

**D. Case Detail**
Amount at risk, reason (diagnosis), customer history summary, agent
reasoning summary (the `reasoning` fields, clearly labeled as "agent's
stated reasoning, not used for the decision itself" — since those fields
are audit-trail-only per Section 2.1, the UI should reflect that distinction
rather than presenting reasoning as if it drove the outcome), action taken,
policy decision (including any prior rejections before an eventual
approval), outcome, full audit trail. This is a direct render of Phase 5's
`GET /cases/{case_id}/audit` — don't reshape the data significantly between
API and UI, since divergence there is exactly the kind of thing that breaks
during live demo debugging.

### 2. Demo narrative rehearsal

Before Day 14 ends:
- Pick 3-4 specific cases from the synthetic batch to walk through live —
  one clean recovery, one that got policy-blocked and shows why, one that
  escalated after exhausting retries, and (if you built Phase 7's
  extension) one from the second use case.
- Write the actual numbers from your real synthetic batch run into the demo
  script — Recovery Rate, Automation Rate, Escalation Rate, Recovery Cost
  (Section 9's formulas), computed from Phase 5's real eval output, not
  placeholders or estimates. If a number looks bad (e.g. escalation rate is
  high), don't hide it — figure out if it's a genuine finding (the synthetic
  batch skews toward hard cases) or a bug, and know which one it is before
  someone asks.
- Rehearse the opted-out-customer case specifically — it's the cleanest
  "the system refused to act" story, and clean negative examples are rarer
  and more convincing than positive ones in this kind of demo.

### 3. Polish pass

- Error states: what does the dashboard show if a query returns nothing
  (empty DB, no cases yet)? Don't let it render a blank or crash — this is
  the kind of thing that only shows up if you refresh the page mid-demo.
- Loading states for the live feed and metrics.
- Make sure `docker-compose up` (from Phase 0) plus one seed command plus
  one frontend start command is the *entire* setup story — if there are more
  steps than that at this point, consolidate them into a single setup script
  today, since "it works but takes 6 manual steps" is a demo-day risk.

## What NOT to build yet

Don't start Phase 7 (Failed Payment Recovery extension) until this phase's
acceptance checklist is fully green and you've done at least one full
rehearsal run-through. Section 7 of the source doc is explicit: extend only
"if time remains" after this — a working, polished single-use-case demo beats
a half-working two-use-case demo.

## Acceptance checklist

- [ ] All four dashboard views implemented, backed by live API calls, zero
      hardcoded demo numbers
- [ ] Agent Activity feed visibly shows policy rejections, not just approvals
- [ ] Case Detail clearly distinguishes "agent's stated reasoning" from "what
      actually drove the decision" (policy + net expected value)
- [ ] Live feed updates within a few seconds of a manually-inserted
      `mock_payments` row, tested by actually doing it, not assumed
- [ ] Full setup is `docker-compose up` + seed + frontend start, verified by
      running it from a clean checkout
- [ ] 3-4 demo cases picked and rehearsed, including the opted-out case
- [ ] Real batch numbers (not placeholders) are in the demo script
- [ ] Empty-state and loading-state handling verified, not just assumed

## Hand back to me

Walk me through the opted-out-customer demo case exactly as you'd present it
live — what you'd click, what the dashboard shows at each step, and the
specific policy rejection reason it surfaces. Also show me the real numbers
(Recovery Rate, Automation Rate, Escalation Rate, Recovery Cost) from your
actual batch run that are going in the demo script.
