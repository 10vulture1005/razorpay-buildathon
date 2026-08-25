import os
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so local `uvicorn` runs see the same config as
    docker-compose. Real environment variables always win over file values."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(APP_ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./recovery.db")
# SQLite needs this for FastAPI's threadpool; Postgres does not.
ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev").strip().lower()
IS_PROD = ENVIRONMENT == "prod"

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "mock")  # mock | openrouter
MODEL_FRONTIER = os.environ.get("MODEL_FRONTIER", "anthropic/claude-sonnet-4.5")
MODEL_SMALL = os.environ.get("MODEL_SMALL", "google/gemini-2.5-flash")

# ---- Real outbound integrations ----
# EMAIL_PROVIDER: smtp | resend | sendgrid | mailgun | console (dev-only echo
# adapter; production startup refuses to boot with console providers configured).
EMAIL_PROVIDER = _env("EMAIL_PROVIDER", "console")
EMAIL_FROM = _env("EMAIL_FROM", "recovery@localhost")
SMTP_HOST = _env("SMTP_HOST")
SMTP_PORT = int(_env("SMTP_PORT", "587") or 587)
SMTP_USER = _env("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
MAILGUN_API_KEY = os.environ.get("MAILGUN_API_KEY", "")
MAILGUN_DOMAIN = _env("MAILGUN_DOMAIN")
# Mailgun has separate EU/US API hosts; the domain's region decides which one.
MAILGUN_BASE_URL = _env("MAILGUN_BASE_URL", "https://api.mailgun.net")
# Inbound reply route signature verification (Mailgun console -> Receiving).
MAILGUN_WEBHOOK_SIGNING_KEY = os.environ.get("MAILGUN_WEBHOOK_SIGNING_KEY", "")

# ---- LLM fallback (secondary OpenAI-compatible provider) ----
# Used when the primary (OpenRouter) exhausts retries — e.g. free-tier 429s.
# Accepts both NVIDIA_API_KEY and the NVDIA_API_KEY typo.
NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY") or os.environ.get("NVDIA_API_KEY", "")
NVIDIA_BASE_URL = _env("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")

# PAYMENT_PROVIDER: razorpay | console (dev-only echo adapter).
PAYMENT_PROVIDER = _env("PAYMENT_PROVIDER", "console")
RAZORPAY_KEY_ID = _env("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
# Razorpay signs webhooks with the webhook secret; falls back to the generic name.
PAYMENT_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET") or os.environ.get(
    "PAYMENT_WEBHOOK_SECRET", ""
)
RAZORPAY_TIMEOUT_S = float(_env("RAZORPAY_TIMEOUT_S", "15") or 15)

POLICY_CONFIG_PATH = APP_ROOT / "app" / "policy" / "policy_config.yaml"

# ---- Security floor (P0) ----
# API_KEYS format: comma-separated "<secret>:<scope1,scope2>" entries.
# Scopes: read | run | admin. Raw secrets are compared via SHA-256 digests only.
API_KEYS_RAW = os.environ.get(
    "API_KEYS",
    # dev-only defaults; ENVIRONMENT=prod REQUIRES explicit API_KEYS (enforced in main.py)
    "dev-admin-key:admin,dev-run-key:run,read,dev-read-key:read",
)

# Explicit CORS allowlist — no wildcard in any environment.
CORS_ORIGINS = [o for o in _env("CORS_ORIGINS", "http://localhost:3000").split(",") if o]

RATE_LIMIT_PER_MINUTE = int(_env("RATE_LIMIT_PER_MINUTE", "120") or 120)
MAX_BODY_BYTES = int(_env("MAX_BODY_BYTES", "65536") or 65536)

# B3 rollback kill switch: flip to false to park all outbound sends.
WRITE_TOOLS_ENABLED = _env_bool("WRITE_TOOLS_ENABLED", True)

# Dev-only escape hatch: console/mock adapters refuse to execute unless true.
# ENVIRONMENT=prod refuses them regardless (app/main.py).
ALLOW_MOCK_ADAPTERS = _env_bool("ALLOW_MOCK_ADAPTERS", False)

# ---- Payment-verification polling fallback ----
# Webhooks are the primary, fast path. If a webhook hasn't produced a
# payment_event row this many seconds after a payment link was sent, the
# worker queries the gateway directly for that link's payments.
# 900s (15 min) is ~10x typical Razorpay webhook latency; long enough to
# never race the webhook, short enough to bound worst-case staleness.
POLL_FALLBACK_AFTER_S = int(_env("POLL_FALLBACK_AFTER_S", "900") or 900)
