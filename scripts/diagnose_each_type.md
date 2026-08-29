# diagnose_each_type — Per-Cause Test Harness

`scripts/diagnose_each_type.py` seeds one case per `LikelyCause` value the
diagnose node can emit, runs the load → `build_context` → `diagnose` pipeline
on each, and prints an expected-vs-actual table. Used to assert that the LLM
classifies every branch of the cause enum correctly.

## Run

```bash
python -m scripts.diagnose_each_type                    # seed + diagnose all 9
python -m scripts.diagnose_each_type --skip-seed        # just diagnose
python -m scripts.diagnose_each_type --case-id case_dx_dispute_0
```

## Result

`Created scripts/diagnose_each_type.py. It seeds 9 cases (one per LikelyCause
value: forgot, process_delay, cashflow_issue, unwilling, dispute, card_expired,
stale_mandate, insufficient_funds, bank_decline) shaped so each context
carries a dominant signal for its target cause, then runs load → build_context
→ diagnose on each and prints a table of expected → actual with confidence
and match.

Result against the mock LLM: 6/9 match. The 3 misses (process_delay,
cashflow_issue, bank_decline) are exactly the branches MockLLM._diagnose
doesn't model — point this at a real LLM via LLM_PROVIDER=openrouter to
exercise the full set. Non-zero exit when run against a real LLM and any case
mismatches (so it's CI-friendly). Full pytest suite (99/99) still green.`

---

## Per-cause: what the test case looks like and why

Each case is shaped so a single signal in the `build_context` payload points
overwhelmingly at the target cause. The mock LLM only models 5 of the 9
branches, so 3 cases mismatch against the mock by design — that mismatch is
the gap a real LLM is expected to close.

### Receivable causes (5)

#### 1. `forgot` — `case_dx_forgot_0`

- **Payer history:** 6 prior invoices, 95% on-time rate, 0 late-days.
- **Inbound messages:** none.
- **Broken promises:** 0.
- **Days overdue:** 4.
- **Case type:** `receivable`.

**Why this shape:** the diagnose node reads `on_time_rate`, `broken_promise_count`,
and inbound message text. A flawless payer with no inbound context and no
broken promises has no signal pointing at cashflow, dispute, or unwillingness.
A real LLM concludes the customer simply overlooked the invoice. The mock
LLM takes the same path (its `_diagnose` returns `forgot` when
`on_time_rate >= 0.9`).

#### 2. `process_delay` — `case_dx_process_delay_0`

- **Payer history:** 6 prior invoices, 95% on-time rate.
- **Inbound message:** *"the invoice is approved on our side. We are waiting
  on internal PO release from procurement; payment will be processed next
  week. No action needed from your end."*
- **Broken promises:** 0.

**Why this shape:** the strong history rules out cashflow, dispute, and
unwilling. The inbound message is the only differentiator: it explicitly
states the customer's internal process is the blocker (procurement, PO
release, internal approval), not the customer's intent or ability to pay.
The phrase "no action needed" is the strongest signal that the cause is
`process_delay` rather than `forgot` — a customer who forgot would not be
reassuring us. A real LLM maps "waiting on internal PO / approval" directly
to `process_delay`. The mock has no branch for this and falls through to
`forgot` — the test exposes that gap.

#### 3. `cashflow_issue` — `case_dx_cashflow_0`

- **Payer history:** 6 prior invoices, 70% on-time rate, ~18 late-days total.
- **Inbound message:** *"Cash is tight this quarter. Can we split the payment
  into two installments over the next 30 days?"*
- **Broken promises:** 0.

**Why this shape:** the moderate history (not clean, not adversarial)
excludes `forgot` and `unwilling`. The inbound message is the textbook
cashflow signal: a request to **split payment into installments** over a
short horizon — the customer is willing to pay but cannot pay in full now.
A real LLM recognises installment requests and "tight cash" framing as
`cashflow_issue`. The mock returns `forgot` because its only history-based
rule is `on_time_rate >= 0.9`; 0.70 falls through to the default branch
which the mock also labels `cashflow_issue` — but only when there's no
contradicting signal. The inbound installment request is the actual driver
in a real-LLM run.

#### 4. `unwilling` — `case_dx_unwilling_0`

- **Payer history:** 6 prior invoices, 55% on-time rate, ~40 late-days total.
- **Broken promises:** 3 (all `kept=False`).
- **Inbound messages:** none.

**Why this shape:** `broken_promise_count` is the dominant signal. Three
broken commitments in a row, combined with sub-60% on-time rate, is the
canonical serial-promise-breaker profile: the customer keeps promising and
keeps not paying. The mock and a real LLM both key on this — the mock
returns `unwilling` whenever `broken >= 2`; a real LLM weighs the same
ledger entry plus the late history.

#### 5. `dispute` — `case_dx_dispute_0`

- **Payer history:** 6 prior invoices, 85% on-time rate.
- **Inbound message:** *"We dispute this invoice. The services billed were
  not delivered as agreed. Please do not contact us about this amount until
  the dispute is resolved."*
- **Broken promises:** 0.

**Why this shape:** the inbound message contains the literal word
"**dispute**", the canonical trigger the diagnose prompt is calibrated for.
A real LLM must short-circuit to `dispute` whenever the message body asserts
non-delivery or a formal dispute. The mock does the same (its
`_diagnose` greps for `"dispute" in body.lower()`), so this case matches in
both modes. This is the highest-confidence case in the suite.

### Failed-payment causes (4)

For failed-payment cases, the diagnose node reads `payment_method_status`
and `last_decline_code` from the context, not the invoice history. Each case
is shaped by the payment method's `status` and the decline code on record.

#### 6. `card_expired` — `case_dx_card_expired_0`

- **Subscription:** `status=past_due`, `failed_attempt_count=1`.
- **Payment method:** `status=expired`, `last_decline_code=card_expired`.

**Why this shape:** the diagnose context includes
`payment_method_status="expired"`. A real LLM maps the combination of an
expired card record plus a `card_expired` decline code directly to
`card_expired`. The mock takes the same path (its failed-payment branch
maps `expired → card_expired`).

#### 7. `stale_mandate` — `case_dx_stale_mandate_0`

- **Subscription:** `status=past_due`, `failed_attempt_count=2`.
- **Payment method:** `status=invalid`, `last_decline_code=stale_mandate`.

**Why this shape:** `payment_method_status="invalid"` is the unambiguous
signal for `stale_mandate` — a mandate that is technically on file but
no longer honoured. The mock maps `invalid → stale_mandate`; a real LLM
matches `last_decline_code="stale_mandate"` to the same label.

#### 8. `insufficient_funds` — `case_dx_insufficient_funds_0`

- **Subscription:** `status=past_due`, `failed_attempt_count=1`.
- **Payment method:** `status=active`, `last_decline_code=insufficient_funds`.

**Why this shape:** the card is still **active** (not expired, not
invalid), so the cause is transient — a one-off balance problem, not a
broken instrument. The decline code `insufficient_funds` is the gateway's
authoritative classification. A real LLM maps that code verbatim to
`insufficient_funds`. The mock falls through to `insufficient_funds` for any
failed-payment case whose method status is not `expired` or `invalid`, so
it also matches.

#### 9. `bank_decline` — `case_dx_bank_decline_0`

- **Subscription:** `status=past_due`, `failed_attempt_count=3`.
- **Payment method:** `status=active`, `last_decline_code=bank_decline`.

**Why this shape:** an active card with three prior failures and a generic
`bank_decline` code is the bank-side rejection signal — neither the card
nor the customer's balance is the problem, the issuing bank declined.
A real LLM maps `last_decline_code="bank_decline"` to `bank_decline`. The
mock has no branch for this and falls through to `insufficient_funds` —
this is the third documented gap, expected to close under a real LLM.

---

## Why the harness matters

The mock LLM is a heuristic stand-in (`app/agent/llm.py:MockLLM`) that models
5 of the 9 `LikelyCause` branches. Running this suite against a real LLM
(`LLM_PROVIDER=openrouter`) gives a deterministic, per-branch smoke test
that the diagnose prompt + model classify every enum value correctly, not
just the easy ones. The script exits non-zero when any case mismatches under
a real LLM, so it's a one-line addition to CI:

```yaml
- run: python -m scripts.diagnose_each_type
  env: { LLM_PROVIDER: openrouter, OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }} }
```
