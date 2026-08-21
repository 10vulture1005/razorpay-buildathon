"""API-key authentication with hashed secrets and scope-based authorization.

- Keys arrive via the `X-API-Key` header.
- Configured keys are held as SHA-256 digests; raw secrets are compared in
  constant time and never logged or persisted.
- Scopes: `read` (query endpoints), `run` (events + agent execution),
  `admin` (demo/simulation + everything else).
"""
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass, field

from fastapi import HTTPException, Request, status

import app.config as config

logger = logging.getLogger("app.auth")

API_KEY_HEADER = "X-API-Key"


@dataclass(frozen=True)
class ApiKeyRecord:
    key_id: str
    digest: bytes
    scopes: frozenset[str] = field(default_factory=frozenset)


def _parse_keys(raw: str) -> list[ApiKeyRecord]:
    records = []
    for i, entry in enumerate(raw.split(",")):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        secret, _, scopes_part = entry.partition(":")
        scopes = frozenset(s.strip() for s in scopes_part.split(",") if s.strip())
        records.append(
            ApiKeyRecord(
                key_id=f"key_{i}",
                digest=hashlib.sha256(secret.encode()).digest(),
                scopes=scopes,
            )
        )
    return records


# Parsed once at import; tests can reload via reload_keys().
_KEYS: list[ApiKeyRecord] = _parse_keys(config.API_KEYS_RAW)


def reload_keys():
    global _KEYS
    _KEYS = _parse_keys(config.API_KEYS_RAW)


def authenticate(request: Request) -> ApiKeyRecord:
    """Resolve + verify the caller's key. Raises 401 on any failure."""
    provided = request.headers.get(API_KEY_HEADER, "")
    if not provided:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    digest = hashlib.sha256(provided.encode()).digest()
    for record in _KEYS:
        if hmac.compare_digest(record.digest, digest):
            return record
    logger.warning("auth.failed", extra={"path": request.url.path})
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")


def require_scope(*required: str):
    """Dependency factory: authenticate then enforce ANY-of the given scopes."""

    def dependency(request: Request) -> ApiKeyRecord:
        record = authenticate(request)
        if "admin" not in record.scopes and not record.scopes & set(required):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"Requires one of scopes: {', '.join(required)}",
            )
        return record

    return dependency


def require_prod_disabled(request: Request) -> None:
    """Demo endpoints: hard-refuse in production regardless of credentials."""
    if config.IS_PROD:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Disabled in production")
