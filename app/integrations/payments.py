"""Razorpay payment-operations adapter (official SDK, razorpay>=2.0).

Capabilities used by the recovery agent:
- payment links: create a branded link for an invoice amount; Razorpay emails/
  SMSes it themselves when notify settings are supplied, and we additionally
  deliver the short_url through our own email adapter for auditability.
- webhook signature verification: Razorpay signs webhook bodies with
  HMAC-SHA256 using the webhook secret.
- payment fetch: authoritative amount/status check before mark_recovered.

PAYMENT_PROVIDER=console is a dev/test-only echo that never touches network;
production startup refuses to boot with it configured.
"""
import hmac
import hashlib
import logging
import uuid

import app.config as config

logger = logging.getLogger("app.integrations.payments")


class PaymentProviderError(Exception):
    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


def _client():
    if not (config.RAZORPAY_KEY_ID and config.RAZORPAY_KEY_SECRET):
        raise PaymentProviderError(
            "PAYMENT_PROVIDER=razorpay requires RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET",
            retryable=False,
        )
    import razorpay

    return razorpay.Client(auth=(config.RAZORPAY_KEY_ID, config.RAZORPAY_KEY_SECRET))


def create_payment_link(
    amount_inr: float,
    reference_id: str,
    customer_email: str | None = None,
    customer_name: str | None = None,
    description: str = "Overdue invoice payment",
) -> dict:
    """Creates a Razorpay Payment Link. Amount is in INR; Razorpay expects paise."""
    if config.PAYMENT_PROVIDER == "console":
        if config.IS_PROD or not config.ALLOW_MOCK_ADAPTERS:
            raise PaymentProviderError(
                "PAYMENT_PROVIDER=console requires ALLOW_MOCK_ADAPTERS=true "
                "(never permitted in production)",
                retryable=False,
            )
        logger.info("payments.console_payment_link", extra={"reference_id": reference_id})
        return {
            "provider": "console",
            "link_id": f"link_console_{uuid.uuid4().hex[:12]}",
            "short_url": f"https://payment-link.invalid/{reference_id}",
            "status": "created",
        }
    if config.PAYMENT_PROVIDER != "razorpay":
        raise PaymentProviderError(f"unknown PAYMENT_PROVIDER {config.PAYMENT_PROVIDER!r}", retryable=False)

    amount_paise = int(round(amount_inr * 100))
    payload: dict = {
        "amount": amount_paise,
        "currency": "INR",
        "accept_partial": False,
        "reference_id": reference_id,
        "description": description[:255],
        "notes": {"reference_id": reference_id},
    }
    if customer_email:
        # Customer presence makes Razorpay email the link itself.
        # NOTE: `notify_info` and `notify_by` are rejected by the live
        # payment-links endpoint ("extra fields sent") — do not add them back.
        payload["customer"] = {"name": customer_name or customer_email, "email": customer_email}
    try:
        link = _client().payment_link.create(payload)
    except Exception as e:  # SDK raises razorpay.errors.* — all mapped here
        message = str(e)
        retryable = "timeout" in message.lower() or "5" == (message[:1] if message[:1].isdigit() else "")
        raise PaymentProviderError(f"razorpay payment_link.create failed: {message}", retryable=retryable) from e
    return {
        "provider": "razorpay",
        "link_id": link.get("id"),
        "short_url": link.get("short_url"),
        "status": link.get("status", "created"),
    }


def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    """Razorpay scheme: HMAC-SHA256 hexdigest of the raw body with the webhook secret."""
    secret = config.PAYMENT_WEBHOOK_SECRET
    if not secret:
        raise PaymentProviderError("webhook secret not configured", retryable=False)
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def fetch_payment(payment_id: str) -> dict:
    """Authoritative fetch used as a second factor before marking recovered."""
    if config.PAYMENT_PROVIDER != "razorpay":
        raise PaymentProviderError("fetch_payment requires PAYMENT_PROVIDER=razorpay", retryable=False)
    try:
        return _client().payment.fetch(payment_id)
    except Exception as e:
        raise PaymentProviderError(f"razorpay payment.fetch failed: {e}") from e


def fetch_payment_link_payments(link_id: str) -> list[dict]:
    """All payment attempts against a payment link. Polling-fallback path:
    used when the paid webhook never arrived and a case sits in
    AWAITING_OUTCOME past POLL_FALLBACK_AFTER_S."""
    if config.PAYMENT_PROVIDER != "razorpay":
        raise PaymentProviderError(
            "gateway polling requires PAYMENT_PROVIDER=razorpay", retryable=False
        )
    try:
        link = _client().payment_link.fetch(link_id)
    except Exception as e:
        raise PaymentProviderError(f"razorpay payment_link.fetch failed: {e}") from e
    return link.get("payments") or []
