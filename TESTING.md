# Testing & Running

## Test matrix (all key-free, no network)

```bash
source .venv/bin/activate
python -m pytest tests/ -q          # 80 tests
```

| Suite | Covers |
|---|---|
| `test_phase1_tools.py` | read tools, write-tool idempotency, anti-hallucination `mark_recovered` gate |
| `test_phase2_policy.py` | every deterministic policy rule + precedence + config snapshots |
| `test_phase3_4_agent.py` | full LangGraph loop: retry→escalate, recovery via poller, opted-out safety, stopping rules purity |
| `test_phase5_adversarial_api.py` | prompt injection, malformed context, duplicate events, webhook replay/bad signature, partial payment, missing email |
| `test_phase7_failed_payment.py` | failed-payment cases flow through the *unmodified* graph/policy |
| `test_production_hardening.py` | auth scopes, rate limits, contact-hours, kill switch, webhook HMAC |
| `test_openrouter_llm.py` | structured-output enforcement (HTTP mocked) |

CI (`.github/workflows/ci.yml`) runs the suite plus the policy-violation gate and
an Alembic drift check against Postgres 16.

## Local end-to-end run

```bash
# 0. deps already in requirements.txt (langgraph, razorpay SDK included)
python -m scripts.run_full_batch --fresh      # synthetic batch through the real graph

# 1. API with dev providers (console echo for email/payments — refused in prod)
uvicorn app.main:app --reload                 # .env loaded values apply

# 2. exercise it
curl -s localhost:8000/health
curl -s -X POST localhost:8000/events/invoice-overdue \
  -H 'X-API-Key: dev-admin-key' -H 'Content-Type: application/json' \
  -d '{"invoice_id":"inv_demo_1","customer_id":"cust_demo","amount":250000,"customer_email":"billing@demo.example"}'
curl -s -X POST localhost:8000/agent/run/case_inv_demo_1 -H 'X-API-Key: dev-admin-key'
curl -s localhost:8000/cases/case_inv_demo_1/audit -H 'X-API-Key: dev-read-key'
```

Dashboard: `frontend-next/` (`npm run dev`, polls the metrics endpoints; the API
key is injected server-side and never reaches the browser).

## Production mode (real integrations)

The server **refuses to boot** in prod unless all of these are set:

- `LLM_PROVIDER=openrouter` + `OPENROUTER_API_KEY`
- `EMAIL_PROVIDER=smtp|resend|sendgrid` (+ its credentials)
- `PAYMENT_PROVIDER=razorpay` + `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET`
- `RAZORPAY_WEBHOOK_SECRET` (configure the webhook in the Razorpay dashboard:
  event `payment_link.paid` and/or `payment.captured`; Razorpay signs with
  HMAC-SHA256 in the `X-Razorpay-Signature` header — verified natively)
- explicit `API_KEYS`, Postgres `DATABASE_URL`, `ENVIRONMENT=prod`

There is no simulated-success path anywhere: sends go to the configured provider
or raise; recovery requires a verified `payment_events` row from the webhook or
reconciliator.

### Smoke test in prod mode

1. Start api + worker (`docker compose up` runs migrate → api → poller).
2. POST an invoice-overdue event for a test customer with your own email.
3. Run the agent. You should receive a real reminder email.
4. Force `send_payment_link`: you'll get a live Razorpay payment link by email.
5. Pay ₹1 of a small test amount via the link → Razorpay fires the webhook →
   `payment_events` row appears → poller marks RECOVERED → `/metrics/recovery` moves.
