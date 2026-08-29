import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

import app.config as config
from app.api.routes_core import router as core_router
from app.api.routes_chat import router as chat_router
from app.api.routes_metrics import router as metrics_router
from app.db.session import engine
from app.observability.logging_setup import new_correlation_id, correlation_id_var, setup_logging
from app.observability.middleware import (
    BodySizeLimitMiddleware,
    CorrelationIdMiddleware,
    RateLimitMiddleware,
)
from app.security.auth import _parse_keys

logger = logging.getLogger("app.main")


def _prod_startup_guard():
    """Refuse to boot production without explicit credentials and REAL providers.
    Dev-only echo adapters (console) and the heuristic mock LLM are forbidden.
    The full preflight is run via `python -m scripts.preflight`; this in-process
    guard is the last-mile safety net so a misconfigured container fail-fasts
    loudly at startup rather than at first request."""
    if not config.IS_PROD:
        return
    if "API_KEYS" not in __import__("os").environ:
        raise RuntimeError("ENVIRONMENT=prod requires explicit API_KEYS")
    if not _parse_keys(config.API_KEYS_RAW):
        raise RuntimeError("ENVIRONMENT=prod requires at least one configured API key")
    if config.LLM_PROVIDER != "openrouter" or not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError(
            "ENVIRONMENT=prod requires LLM_PROVIDER=openrouter with OPENROUTER_API_KEY"
        )
    if config.EMAIL_PROVIDER == "console":
        raise RuntimeError(
            "ENVIRONMENT=prod requires a real EMAIL_PROVIDER (smtp | resend | sendgrid | mailgun)"
        )
    if config.PAYMENT_PROVIDER != "razorpay":
        raise RuntimeError("ENVIRONMENT=prod requires PAYMENT_PROVIDER=razorpay")
    if not (config.RAZORPAY_KEY_ID and config.RAZORPAY_KEY_SECRET):
        raise RuntimeError("ENVIRONMENT=prod requires RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET")
    if not config.PAYMENT_WEBHOOK_SECRET:
        raise RuntimeError("ENVIRONMENT=prod requires RAZORPAY_WEBHOOK_SECRET")
    # Catch the "shipped with localhost DATABASE_URL" failure mode here too —
    # the preflight script covers it, but the in-process guard is the only
    # thing that runs in every container regardless of CI discipline.
    if "sqlite" in config.DATABASE_URL:
        raise RuntimeError("ENVIRONMENT=prod requires a Postgres DATABASE_URL")
    if "localhost" in config.DATABASE_URL or "127.0.0.1" in config.DATABASE_URL:
        raise RuntimeError(
            "ENVIRONMENT=prod DATABASE_URL points at localhost — set it to the "
            "managed Postgres instance"
        )


def _verify_schema():
    """Migrations are an EXPLICIT pipeline step now (auto-migrating multi-replica
    startups race). Startup only verifies the schema is present and fails fast
    with a runbook pointer if it is not."""
    if "sqlite" in config.DATABASE_URL:
        from app.db.session import init_db

        init_db()
        return
    from sqlalchemy import inspect

    insp = inspect(engine)
    if not insp.has_table("alembic_version"):
        raise RuntimeError(
            "Database is not migrated. Run `python -m scripts.migrate upgrade head` "
            "(or the migrate service in docker-compose) before starting the API."
        )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _prod_startup_guard()
    _verify_schema()
    from app.observability.provider_status import log_provider_status

    log_provider_status()
    logger.info("startup.complete", extra={"environment": config.ENVIRONMENT})
    yield
    logger.info("shutdown.complete")


app = FastAPI(
    title="Revenue Recovery Autopilot",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if not config.IS_PROD else None,
    redoc_url=None,
)

# Middleware order: outermost first in the stack below (last added runs first).
app.add_middleware(CorrelationIdMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(BodySizeLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,  # explicit allowlist; never "*"
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["X-API-Key", "X-Request-ID", "Content-Type"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Sanitized error body — no internals leak to callers; full traceback goes
    to structured logs under the request's correlation ID."""
    cid = correlation_id_var.get()
    logger.error(
        "unhandled_exception",
        extra={"path": request.url.path, "correlation_id": cid},
        exc_info=exc,
    )
    return JSONResponse(
        {"detail": "Internal server error", "correlation_id": cid},
        status_code=500,
    )


@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    return JSONResponse({"detail": "Not found"}, status_code=404)


app.include_router(core_router)
app.include_router(metrics_router)
app.include_router(chat_router)

setup_logging(logging.getLogger("app").level or logging.INFO)
