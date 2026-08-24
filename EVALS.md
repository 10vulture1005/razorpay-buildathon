# Evaluation Guide

How to evaluate this system, what data you need, and where everything plugs in.
The harness already exists (`app/evals/eval_runner.py`); this doc explains how to
extend it and what data to bring when you move past synthetic fixtures.

---

## 1. What is already evaluated (trace-level, per case)

Run the full batch:

```bash
python -m scripts.run_full_batch --fresh     # seeds 24 labeled cases, runs the graph, prints the report
python scripts/check_policy_violations.py    # CI gate: violation rate must be EXACTLY 0
```

The report contains:

| Metric | Meaning | Threshold |
|---|---|---|
| `policy_violation_rate` | Actions executed despite `allowed=False`. Structurally impossible; non-zero = stop-the-line bug. | **0.0, always** |
| `diagnosis_validity_rate` | Diagnoses that parsed into the valid enum (regression guard on schema enforcement). | **1.0** |
| `diagnosis_accuracy_vs_ground_truth` | `likely_cause` vs the labeled expectation per archetype (`ground_truth.json`). | ≥ 0.8 (tune as corpus grows) |
| `cost_per_case_usd.by_node` | LLM spend per node (`diagnosis`, `action_selected`) — proves tier routing saves money. | track trend |
| `tool_success_rate_per_tool` | Success rate **per tool**, not aggregate. One tool at 50% hides behind four at 100%. | ≥ 0.99 per tool |
| `recovery_rate` / `outcome_distribution` | Business outcomes by archetype on the batch. | sanity vs hand-check |

## 2. The data you need

### Already in-repo (synthetic fixtures)

- `scripts/seed_synthetic_data.py` — 24 cases across 6 archetypes
  (`clean_payer`, `serial_promise_breaker`, `disputed_invoice`,
  `high_value_low_risk`, `low_value_high_risk`, `opted_out`), each with payment
  history, promises, communication log, and (for some) subscriptions/payment methods.
- `app/evals/ground_truth.json` — expected diagnoses + forbidden actions per archetype.
- `app/evals/test_cases.json` — adversarial case registry, each wired to an exact test.

### To evaluate against reality, collect this per historical case

1. **Case facts**: invoice id/amount/currency, days overdue, case type.
2. **Customer history**: # invoices, on-time rate, avg days late, # broken promises, opt-out flag.
3. **Ground-truth diagnosis** (the label): what a human collections agent judged
   the actual cause to be (`cashflow_issue | dispute | forgot | process_delay | unwilling`).
   Aim for ≥ 30 labeled cases, ≥ 5 per cause class.
4. **Ground-truth action**: what the human did and whether it worked (recovered?
   how much? how long?). This gives you action-quality labels, not just diagnosis labels.
5. **Policy ground truth**: for a sample of cases, the decision your policy *should*
   have produced (blocked/allowed + reason). Tests the graph honors policy under real data.

Store them in the same shape the seeder produces (customers + invoices + promises +
communication_messages rows) so `evaluate()` works unchanged. Keep real customer PII
out of the repo — load from a secret location via env-configured DB.

### Adversarial data to keep adding

Every entry needs: a description, the **specific safe behavior** to assert, and a
test that asserts exactly that ("didn't crash" is not an assertion). Current set:
prompt injection, malformed context, duplicate events, reasoning-field abuse,
webhook replay, bad signature, partial payment, missing customer email.

Good next candidates:
- injection inside invoice metadata fields (not just messages)
- amount boundary cases (escalation_min_value ± 1 paisa)
- opt-out flipped mid-case between attempts
- clock-boundary: event detected 23:50 IST vs 00:10 IST (contact hours)
- currency mismatch (amount paid in different currency than invoiced)

## 3. How to write a new eval

1. **Fixture** (if synthetic): extend `scripts/seed_synthetic_data.py` with the new
   archetype and label it in `customers.notes`.
2. **Ground truth** (if diagnosable): add the archetype → expected causes mapping in
   `app/evals/ground_truth.json`.
3. **Assertion**: trace-level checks live in `evaluate()` in `app/evals/eval_runner.py`
   (they read `audit_log`). Behavioral adversarial checks live in
   `tests/test_phase5_adversarial_api.py` and must assert the specific safe behavior.
4. **Register**: add the case to `app/evals/test_cases.json` pointing at its test.
5. **Threshold**: document the pass threshold next to the metric — never leave
   "reasonable" vague.

### Metric definitions you should keep precise

- **Policy violation** := any `tool_executions` row whose action is customer-facing
  without a preceding `policy_check` audit row with `allowed=true` for that case.
- **Diagnosis correct** := last `diagnosis` event's `likely_cause` ∈
  `expected_causes[archetype]` (multiple acceptable causes are normal).
- **Cost per node** := sum of `llm_usage.cost_est_usd` recorded on each
  `diagnosis` / `action_selected` audit row. With OpenRouter configured, cost comes
  from the API's usage accounting (falls back to a token heuristic).

## 4. Running evals against the real LLM

The default provider is the deterministic mock (key-free, CI-safe). To score the
real model:

```bash
export LLM_PROVIDER=openrouter
export OPENROUTER_API_KEY=sk-or-...
export MODEL_FRONTIER=anthropic/claude-sonnet-4.5   # or any OpenRouter slug
python -m scripts.run_full_batch --fresh            # real costs land in the report
```

Report accuracy/cost side by side across models — that comparison is the whole
point of tracking cost per node.
