# Deployment Guide — Revenue Recovery Autopilot

Production deployment playbook for the FastAPI + Next.js + Postgres stack.
The repo is already containerized; this guide covers the three realistic
targets (Render, Fly.io, a single VPS), plus the security preflight
**before** you expose anything.

---

## Quick start (one-click on Render)

The repo ships a `render.yaml` Blueprint that provisions **everything**:
managed Postgres, the API service, the worker, the dashboard, and a
release-phase migration step. One click on Render creates the whole
stack.

1. Fork or import this repo into your GitHub org.
2. In Render, **New → Blueprint**, point it at the repo.
3. Render reads `render.yaml` and offers to create 4 services:
   `recovery-db` (Postgres), `recovery-migrate` (one-shot release),
   `recovery-api` (web), `recovery-worker` (background worker), and
   `recovery-dashboard` (static site).
4. After they're created, open each service → **Environment** → set
   the secrets listed in `render.yaml` (the file is annotated; secrets
   are intentionally left blank and must be set in the dashboard).
5. Click **Manual Deploy** on the migrate service first; once it
   succeeds, the API and worker will start.
6. Visit the API service's URL, hit `/readyz` — should return 200.
7. Run `python -m scripts.preflight --env prod` from your local shell
   with the same env vars to confirm the configuration is sound.

> If you'd rather set up the services by hand, follow §2 below — the
> Blueprint is just a declarative shortcut to the same result.

---

## 0. Security preflight — DO THIS FIRST

Your local `.env` contains live secrets pasted during development
(Razorpay test keys, Mailgun API key, OpenRouter key, NVIDIA key,
webhook signing key). **Rotate all of them before deploying**, even
to a private staging host. The repo's `.gitignore` blocks `.env` from
the commit, but a stolen laptop or a leaked shell history is enough.

Rotation checklist (do these in order):

1. **Razorpay** — Dashboard → Settings → API Keys → Regenerate Test Key.
   Update `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET`.
2. **Razorpay webhook secret** — Dashboard → Settings → Webhooks →
   change the secret, update `RAZORPAY_WEBHOOK_SECRET` /
   `PAYMENT_WEBHOOK_SECRET` to match (the verifier reads the
   `PAYMENT_WEBHOOK_SECRET` env, the dashboard just needs to agree).
3. **Mailgun** — Dashboard → Security → Reset API Key. Update
   `MAILGUN_API_KEY`. Also rotate `MAILGUN_WEBHOOK_SIGNING_KEY`
   (Security → Webhooks → Signing key).
4. **OpenRouter** — dashboard → Keys → Revoke, create new.
   Update `OPENROUTER_API_KEY`.
5. **NVIDIA** — `console.nvidia.com` → API Keys → Regenerate.
   Update `NVIDIA_API_KEY`.
6. **API keys** — replace `dev-admin-key:admin,...` with
   **random 32+ byte secrets** for every scope. The format is
   `<secret>:<scope1,scope2,...>` and secrets are SHA-256 hashed
   at startup, so the raw values only exist in your secret manager.

> **Never** put real secrets in `.env.example` (it's committed). All
> real values must live in the host's secret manager.

---

## 0.5 Configuration preflight — `scripts/preflight.py`

Before deploying, run the in-repo preflight script against the **same
environment variables** the production service will see. The script
catches the most common production-misconfig failure modes *before*
they cause a 3am page:

```bash
python -m scripts.preflight --env prod
```

It validates (against prod rules):

- `API_KEYS` exists, all entries are ≥16 chars, no placeholder/dev values, scopes are valid (`read`/`run`/`admin`), at least one admin scope
- `CORS_ORIGINS` is non-empty, no `*`, no `http://` in prod
- `LLM_PROVIDER=openrouter` and `OPENROUTER_API_KEY` is set
- `EMAIL_PROVIDER != console`, `EMAIL_FROM` looks real, Mailgun/SMTP/Resend/SendGrid credentials are present when their adapter is selected
- `PAYMENT_PROVIDER=razorpay`, `RAZORPAY_KEY_ID` starts with `rzp_live_` or `rzp_test_`, `RAZORPAY_KEY_SECRET` and webhook secrets are set
- `DATABASE_URL` is Postgres (not SQLite) and not pointing at localhost
- `ALLOW_MOCK_ADAPTERS=false`
- `WRITE_TOOLS_ENABLED` set sensibly (warns if `false` in prod)
- `RATE_LIMIT_PER_MINUTE` and `MAX_BODY_BYTES` are sane
- `LOG_LEVEL != DEBUG` in prod

The preflight also runs in CI as a separate job, so misconfigurations
that would have shipped fail the PR instead.

A subset of these checks also runs in-process at API startup
(`_prod_startup_guard` in `app/main.py`) as a last-mile safety net so
a misconfigured container fail-fasts loudly instead of at first
request.

---

## 1. Architecture at a glance

```
                  ┌────────────────────────────────────┐
   Internet ──▶   │  TLS terminator (host/nginx/       │
                  │  cloud LB) → :8000                 │
                  └─────────────┬──────────────────────┘
                                │
                  ┌─────────────▼──────────────────────┐
                  │  API container (uvicorn)           │
                  │  --workers 2 --loop uvloop         │
                  └────┬──────────────────────────┬────┘
                       │                          │
              ┌────────▼────────┐       ┌─────────▼────────┐
              │  Postgres 16    │       │  Worker container│
              │  (managed or    │       │  (outcome poller)│
              │   docker volume)│       └──────────────────┘
              └─────────────────┘
                       ▲
                       │ internal
                  ┌────┴────────────────────────────────┐
                  │  Next.js dashboard (:3000)           │
                  │  - Static export OR Node SSR         │
                  │  - Reverse-proxy /api/* → API:8000   │
                  └──────────────────────────────────────┘
```

Two long-running processes (API + worker), one DB, one web tier.

---

## 2. Target A — Render (recommended for first deploy)

Cheapest path to a real public URL. ~$7/mo for the DB + free web tier
for the API/worker. Already supports Docker.

### 2.1 Create the database

1. New → PostgreSQL → name `recovery-db` → plan `Starter` ($7/mo).
2. Copy the **Internal** connection string — it looks like
   `postgresql+psycopg2://user:pass@host/dbname`. You'll paste this
   as `DATABASE_URL` on the API service.

### 2.2 Create the API service

1. New → Web Service → "Deploy from GitHub" → pick this repo.
2. **Runtime**: Docker.
3. **Region**: same as the DB.
4. **Instance type**: Starter ($0/mo for hobby; $7/mo for always-on).
5. **Health check path**: `/readyz`.
6. **Environment** (set in Render dashboard, never commit):

```
DATABASE_URL=<paste internal Postgres URL from 2.1>
LLM_PROVIDER=openrouter
MODEL_FRONTIER=minimax/minimax-m3:free
MODEL_SMALL=minimax/minimax-m3:free
OPENROUTER_API_KEY=<rotated>
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_TIMEOUT_S=30
ENVIRONMENT=prod
ALLOW_MOCK_ADAPTERS=false
EMAIL_PROVIDER=mailgun
EMAIL_FROM=recovery@yourdomain.com
MAILGUN_API_KEY=<rotated>
MAILGUN_DOMAIN=<your sending domain>
MAILGUN_BASE_URL=https://api.mailgun.net
MAILGUN_WEBHOOK_SIGNING_KEY=<rotated>
PAYMENT_PROVIDER=razorpay
RAZORPAY_KEY_ID=<rotated rzp_live_...>
RAZORPAY_KEY_SECRET=<rotated>
RAZORPAY_WEBHOOK_SECRET=<rotated>
PAYMENT_WEBHOOK_SECRET=<same as above>
API_KEYS=<random-admin>:<random-run>:<random-read>
CORS_ORIGINS=https://<your-frontend>.onrender.com
RATE_LIMIT_PER_MINUTE=120
MAX_BODY_BYTES=65536
WRITE_TOOLS_ENABLED=true
POLL_INTERVAL_S=30
NVIDIA_API_KEY=<rotated, optional fallback>
```

7. **Pre-deploy command** (in Settings → Advanced): `python -m scripts.migrate upgrade head`
8. **Start command** (Render will use the Dockerfile's `CMD`, which is
   `uvicorn app.main:app --host 0.0.0.0 --port $PORT` — already correct).

### 2.3 Create the worker service

1. New → Background Worker → same repo, same Docker image.
2. **Start command override**: `python -m app.workers.outcome_poller 30`
3. Same env vars as the API (minus `CORS_ORIGINS`).

### 2.4 Create the dashboard (optional on Render)

1. New → Static Site → root `frontend-next` → build command
   `npm ci && npm run build` → publish dir `out`.
2. Set env: `NEXT_PUBLIC_API_BASE_URL=https://<api>.onrender.com`,
   `API_KEY_FOR_BROWSER=<a read-scope key — read-only, safe to expose>`.
3. **CORS**: add this static-site origin to the API's `CORS_ORIGINS`.

### 2.5 Razorpay webhook

In Razorpay Dashboard → Webhooks → Add:
- URL: `https://<api>.onrender.com/webhooks/payment`
- Secret: the same string you put in `PAYMENT_WEBHOOK_SECRET`
- Event: `payment_link.paid`

### 2.6 Verify

```bash
curl https://<api>.onrender.com/health      # → {"status":"ok"}
curl https://<api>.onrender.com/readyz      # → 200 only after migrations ran
curl -H "X-API-Key: <admin>" https://<api>.onrender.com/metrics/recovery
```

Then run the eval against prod:

```bash
DATABASE_URL=<prod URL> python -m scripts.run_full_batch --fresh
```

`policy_violation_rate: 0` is the load-bearing assertion. Non-zero = stop
the line and investigate.

---

## 3. Target B — Fly.io

Better if you want closer-to-edge latency or multi-region workers.
The `Dockerfile` is multi-stage-ready; Fly builds it directly.

### 3.1 One-time setup

```bash
brew install flyctl          # or curl -L https://fly.io/install.sh | sh
fly auth signup
fly launch --no-deploy       # creates fly.toml, accepts Dockerfile
```

### 3.2 Add Postgres

```bash
fly postgres create --name recovery-db --region bom
fly postgres attach recovery-db
# prints DATABASE_URL → copy it
```

### 3.3 Set secrets

```bash
fly secrets set \
  LLM_PROVIDER=openrouter \
  MODEL_FRONTIER=minimax/minimax-m3:free \
  MODEL_SMALL=minimax/minimax-m3:free \
  OPENROUTER_API_KEY=<rotated> \
  ENVIRONMENT=prod \
  ALLOW_MOCK_ADAPTERS=false \
  EMAIL_PROVIDER=mailgun \
  EMAIL_FROM=recovery@yourdomain.com \
  MAILGUN_API_KEY=<rotated> \
  MAILGUN_DOMAIN=<your domain> \
  MAILGUN_BASE_URL=https://api.mailgun.net \
  MAILGUN_WEBHOOK_SIGNING_KEY=<rotated> \
  PAYMENT_PROVIDER=razorpay \
  RAZORPAY_KEY_ID=<rotated> \
  RAZORPAY_KEY_SECRET=<rotated> \
  RAZORPAY_WEBHOOK_SECRET=<rotated> \
  PAYMENT_WEBHOOK_SECRET=<rotated> \
  API_KEYS=<rotated> \
  CORS_ORIGINS=https://recovery-dashboard.fly.dev
```

### 3.4 Deploy

```bash
fly deploy
```

### 3.5 Worker

Edit `fly.toml` and add a second process for the worker:

```toml
[[processes]]
  api = "uvicorn app.main:app --host 0.0.0.0 --port 8000"
  worker = "python -m app.workers.outcome_poller 30"
```

```bash
fly deploy --strategy immediate
```

### 3.6 Custom domain + TLS

```bash
fly certs add api.yourdomain.com
fly certs add dashboard.yourdomain.com
```

Point your DNS A/AAAA records to Fly.

---

## 4. Target C — single VPS (cheapest, most ops)

Hetzner/OVH/DO $4-6/mo droplet. Full control, no managed Postgres.

### 4.1 Provision

```bash
# Ubuntu 24.04, 2 vCPU / 2GB RAM
ssh root@<ip>
adduser deploy && usermod -aG sudo deploy
```

### 4.2 Install Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker deploy
```

### 4.3 Clone + env

```bash
sudo -iu deploy
git clone https://github.com/<you>/razorpay-buildathon.git
cd razorpay-buildathon
cp .env.example .env
$EDITOR .env   # paste in rotated secrets; set ENVIRONMENT=prod
```

### 4.4 Caddy as TLS terminator (automatic Let's Encrypt)

```bash
sudo apt install -y caddy
```

`/etc/caddy/Caddyfile`:

```
api.yourdomain.com {
  reverse_proxy localhost:8000
}
dashboard.yourdomain.com {
  reverse_proxy localhost:3000
}
```

```bash
sudo systemctl reload caddy
```

### 4.5 Boot the stack

```bash
docker compose up -d db migrate
docker compose up -d api worker
# dashboard
cd frontend-next && npm ci && npm run build && npm run start &
```

### 4.6 Watchtower for auto-updates (optional)

```bash
docker run -d --name watchtower \
  -v /var/run/docker.sock:/var/run/docker.sock \
  containrrr/watchtower --cleanup --schedule "0 0 4 * * *"
```

---

## 5. Database migrations

`migrate` is an explicit one-shot step — the API never auto-migrates.
On Render this is the **Pre-deploy command**. On Fly and on the VPS,
the `migrate` service in `docker-compose.yml` runs it as a one-shot
container before `api`/`worker` start.

```bash
# Manual
docker compose run --rm migrate
```

To generate a new migration after a model change:

```bash
docker compose run --rm api alembic revision --autogenerate -m "add foo"
docker compose run --rm api alembic upgrade head
```

CI runs `alembic upgrade head && alembic revision --autogenerate -m diff_check`
and fails if the diff is non-empty — see `.github/workflows/ci.yml`.

---

## 6. Post-deploy verification

```bash
# 1. Liveness
curl https://api.yourdomain.com/health
# → {"status":"ok"}

# 2. Readiness (DB + migrations)
curl https://api.yourdomain.com/readyz
# → 200 {"status":"ok"} only if alembic_version is at head

# 3. Metrics
curl -H "X-API-Key: $ADMIN" https://api.yourdomain.com/metrics/recovery
# → numbers live from the DB

# 4. End-to-end smoke
curl -X POST -H "X-API-Key: $ADMIN" -H "Content-Type: application/json" \
  -d '{"invoice_id":"inv_smoke_1","customer_id":"cust_smoke","amount":250000}' \
  https://api.yourdomain.com/events/invoice-overdue

curl -X POST -H "X-API-Key: $ADMIN" \
  https://api.yourdomain.com/agent/run/case_inv_smoke_1

# Insert a payment via dev hook (refused in ENVIRONMENT=prod — use the
# Razorpay webhook flow in production):
curl -X POST -H "X-API-Key: $ADMIN" \
  https://api.yourdomain.com/cases/case_inv_smoke_1/simulate-payment
# → 403 in prod, 200 in staging (ENVIRONMENT=dev)

# 5. Audit trail
curl -H "X-API-Key: $ADMIN" \
  https://api.yourdomain.com/cases/case_inv_smoke_1/audit | jq
```

---

## 7. Observability

- **Logs**: `docker compose logs -f api` or `fly logs`. The app emits
  structured JSON with `X-Request-ID` for correlation.
- **Health**: `/health` (liveness, no DB) and `/readyz` (DB + migrations).
- **Metrics**: `/metrics/recovery` (business) — wire to your scraper.
  LLM cost per node is already in the audit log.
- **Tracing**: not built in. If you need OpenTelemetry, add
  `opentelemetry-instrumentation-fastapi` to `requirements.txt` and
  init in `app/main.py` — it's a 5-line change.

---

## 8. Cost reality check (Render free + MiniMax-M3 free)

| Item | Cost |
|---|---|
| Render Postgres Starter | $7/mo |
| Render Web Service Starter | $0 (free) or $7 (always-on) |
| Render Background Worker | $0 (free) or $7 (always-on) |
| OpenRouter `minimax/minimax-m3:free` | $0 — but rate-limited (daily cap on free models) |
| Mailgun (Flex) | $0 up to 100 emails/day |
| Razorpay test mode | $0 |
| **Total staging** | **$0–14/mo** |

The free model has rate limits (OpenRouter enforces per-minute and
per-day caps on `:free` slugs). The LLM client already handles 429s
with backoff; if a batch run trips the daily cap, switch `MODEL_FRONTIER`
to a paid model for that run and switch back.

---

## 9. Rollback

- **App**: redeploy the previous Docker image (`fly releases rollback`
  or Render's "Rollback" button on the deploy page).
- **DB**: `alembic downgrade -1` — but only one revision; multi-step
  rollbacks are not part of MVP. Forward-fix is preferred.
- **Outbound kill switch**: set `WRITE_TOOLS_ENABLED=false` and
  redeploy (no DB change, no restart of worker needed if it's in the
  same env). All sends park as `AWAITING_OUTCOME` immediately.
- **LLM kill switch**: set `LLM_PROVIDER=mock` temporarily — refused
  in prod, so instead point `OPENROUTER_API_KEY` to an empty value
  and the next call will raise `StructuredOutputFailure` → escalate
  every case (safe, just noisy in the audit log).

---

## 10. What this guide does NOT cover (R0-scope deferrals)

These are the items in `PRODUCTION.md §11` that ship as customer
engagement deepens, not as blockers for an MVP pilot:

- **Multi-tenancy / real auth** — single-tenant deploys work for the
  first 3-5 pilots; each gets its own stack.
- **SMS / WhatsApp** — email-only via Mailgun for now; Razorpay
  sends the link's SMS automatically.
- **ERP integration (Tally / Zoho / QuickBooks)** — webhook
  ingestion is the interim workaround; CSV import covers
  pre-pilot data load.
- **Public payment-failure webhook ingestion** — the internal
  `/events/payment-failed` route is in scope; receiving signed
  webhooks directly from gateways at public exposure is a
  follow-on hardening pass.

A pilot on this stack is safe, observable, and the safety story
holds end-to-end. The items above are what you negotiate into
the pilot SOW as next-step engineering.
