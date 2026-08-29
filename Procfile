# Heroku-style process definitions (also honored by Render).
# `web`         — HTTP service, must listen on $PORT (Render injects it).
# `worker`      — long-running poller.
# `migrate`     — one-shot DB migration; run as a release phase or pre-deploy.
#
# Render Blueprint (render.yaml) takes precedence over this file when present.
# This file is for platforms that don't read render.yaml.

web:        uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-2} --proxy-headers --forwarded-allow-ips='*'
worker:     python -m app.workers.outcome_poller 30
migrate:    python -m scripts.migrate upgrade head
release:    python -m scripts.migrate upgrade head
