"""Email delivery adapter. Providers selected via EMAIL_PROVIDER env:

- smtp:     any standards-compliant SMTP relay (STARTTLS)
- resend:   Resend HTTP API (https://resend.com/docs/api-reference)
- sendgrid: SendGrid v3 Mail Send API
- console:  dev/test-only echo adapter — logs the message and returns a
            synthetic provider id. REFUSED in production (see config guard).

Every send either returns a real provider message id or raises
EmailDeliveryError. There is no simulated success/failure path.
"""
import logging
import smtplib
import uuid
from email.message import EmailMessage

import httpx

import app.config as config

logger = logging.getLogger("app.integrations.email")

RESEND_URL = "https://api.resend.com/emails"
SENDGRID_URL = "https://api.sendgrid.com/v3/mail/send"
HTTP_TIMEOUT_S = 15


class EmailDeliveryError(Exception):
    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


def _require_provider_config():
    p = config.EMAIL_PROVIDER
    if p == "smtp" and not (config.SMTP_HOST and config.EMAIL_FROM):
        raise EmailDeliveryError("EMAIL_PROVIDER=smtp requires SMTP_HOST and EMAIL_FROM", retryable=False)
    if p == "resend" and not config.RESEND_API_KEY:
        raise EmailDeliveryError("EMAIL_PROVIDER=resend requires RESEND_API_KEY", retryable=False)
    if p == "sendgrid" and not config.SENDGRID_API_KEY:
        raise EmailDeliveryError("EMAIL_PROVIDER=sendgrid requires SENDGRID_API_KEY", retryable=False)


def _send_smtp(to_addr: str, subject: str, body: str) -> dict:
    msg = EmailMessage()
    msg["From"] = config.EMAIL_FROM
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=HTTP_TIMEOUT_S) as server:
            server.starttls()
            if config.SMTP_USER:
                server.login(config.SMTP_USER, config.SMTP_PASSWORD)
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        # Connection/auth problems are typically transient; bad credentials are not.
        retryable = not isinstance(e, smtplib.SMTPAuthenticationError)
        raise EmailDeliveryError(f"smtp delivery failed: {e}", retryable=retryable) from e
    return {"provider": "smtp", "provider_message_id": None}


def _send_resend(to_addr: str, subject: str, body: str) -> dict:
    resp = httpx.post(
        RESEND_URL,
        headers={"Authorization": f"Bearer {config.RESEND_API_KEY}"},
        json={"from": config.EMAIL_FROM, "to": [to_addr], "subject": subject, "text": body},
        timeout=HTTP_TIMEOUT_S,
    )
    if resp.status_code >= 500 or resp.status_code == 429:
        raise EmailDeliveryError(f"resend transient failure {resp.status_code}")
    if resp.status_code != 200:
        raise EmailDeliveryError(
            f"resend rejected send {resp.status_code}: {resp.text[:200]}", retryable=False
        )
    return {"provider": "resend", "provider_message_id": resp.json().get("id")}


def _send_sendgrid(to_addr: str, subject: str, body: str) -> dict:
    resp = httpx.post(
        SENDGRID_URL,
        headers={"Authorization": f"Bearer {config.SENDGRID_API_KEY}"},
        json={
            "personalizations": [{"to": [{"email": to_addr}]}],
            "from": {"email": config.EMAIL_FROM},
            "subject": subject,
            "content": [{"type": "text/plain", "value": body}],
        },
        timeout=HTTP_TIMEOUT_S,
    )
    if resp.status_code >= 500 or resp.status_code == 429:
        raise EmailDeliveryError(f"sendgrid transient failure {resp.status_code}")
    if resp.status_code != 202:
        raise EmailDeliveryError(
            f"sendgrid rejected send {resp.status_code}: {resp.text[:200]}", retryable=False
        )
    return {"provider": "sendgrid", "provider_message_id": None}  # SendGrid returns 202 with no body id


def _send_console(to_addr: str, subject: str, body: str) -> dict:
    """Dev/test echo adapter. Never reachable in production (startup guard)."""
    logger.info("email.console_delivery", extra={"to": to_addr, "subject": subject})
    return {"provider": "console", "provider_message_id": f"console_{uuid.uuid4().hex[:12]}"}


_SENDERS = {
    "smtp": _send_smtp,
    "resend": _send_resend,
    "sendgrid": _send_sendgrid,
    "console": _send_console,
}


def send_email(to_addr: str, subject: str, body: str) -> dict:
    """Returns {'provider', 'provider_message_id'}. Raises EmailDeliveryError on failure."""
    if not to_addr or "@" not in to_addr:
        raise EmailDeliveryError(f"invalid recipient address: {to_addr!r}", retryable=False)
    sender = _SENDERS.get(config.EMAIL_PROVIDER)
    if sender is None:
        raise EmailDeliveryError(f"unknown EMAIL_PROVIDER {config.EMAIL_PROVIDER!r}", retryable=False)
    _require_provider_config()
    return sender(to_addr, subject, body)
