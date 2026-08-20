# Revenue Recovery Autopilot — Phase Prompt Index

Source: Track 03 architecture doc (B2B Receivables Recovery Agent MVP, 14-day solo build).

Each phase file in this set is a **standalone prompt** — paste it whole into a coding
agent (Claude Code, Cursor, etc.) as one message. Each one repeats the invariants
that matter (idempotency, policy-gating, structured output) so the agent doesn't
need the other files in context to not screw up the load-bearing decisions.

## Sequencing

| Phase | File | Days (per source doc) | Depends on |
|---|---|---|---|
| 0 | `01-phase0-repo-and-schema.md` | 1-2 | — |
| 1 | `02-phase1-tools-layer.md` | 3-4 | Phase 0 |
| 2 | `03-phase2-policy-engine.md` | 5-6 | Phase 0 |
| 3 | `04-phase3-langgraph-agent.md` | 7-9 | Phase 0, 1, 2 |
| 4 | `05-phase4-outcome-and-stopping.md` | 10 | Phase 3 |
| 5 | `06-phase5-evals-and-audit.md` | 11-12 | Phase 3, 4 |
| 6 | `07-phase6-dashboard-and-demo.md` | 13-14 | Phase 5 |
| — | `08-phase7-optional-extension.md` | if time remains | Phase 6 |

Policy Engine (Phase 2) has no code dependency on Phase 1 (tools) — they can be
built in parallel if you want to compress the timeline — but Phase 3 needs both.

## Non-negotiables that appear in every phase file

These are repeated deliberately in each prompt, not just stated once here,
because an agent working phase-by-phase will not re-read this README:

1. **No free text into the Policy Engine or Action Executor.** Every LLM call
   that feeds downstream logic returns a Pydantic-validated structured output.
   `reasoning` fields are audit-trail only, never branched on.
2. **`policy_check` and `check_stopping_rules` are plain deterministic Python.**
   Zero LLM calls in either. This is the load-bearing safety property of the
   whole system — an agent "arguing around" a policy block is a build-blocking
   bug, not a bug to patch around.
3. **Every write tool is idempotent** on `case_id + action + attempt_number`.
4. **State lives in Postgres, not in the LLM's context window.** The Context
   Builder assembles only what's relevant to the current decision.
5. **The retry loop is bounded by an explicit counter in state**, not just
   LangGraph's `recursion_limit` — hitting the bound must produce a clean
   `ESCALATED` state, never a crash.

## How to use these files

- Feed them one at a time, in order, to whatever coding agent you're using.
- Each file ends with an **"Acceptance checklist"** — don't move to the next
  phase until every box is genuinely checkable, not just plausible.
- Each file also ends with a **"Hand back to me"** note — the specific thing
  the agent should report before you move on (e.g. "show me the failing
  policy test output"), because that's where a coding agent tends to skip
  steps under time pressure.
