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


_KNOWN_SCOPES: frozenset[str] = frozenset({"read", "run", "admin"})


def _parse_keys(raw: str) -> list[ApiKeyRecord]:
    """Parse `API_KEYS` env var.

    Format: comma-separated `<secret>:<scope1,scope2>` entries. Two valid shapes:
      1. `key1:s1,s2,key2:s3`     (one entry, then the next)
      2. `key1:s1,s2, key2:s3`    (whitespace-separated, identical meaning)

    The naive `split(",")` is ambiguous when a scope name is itself a comma
    token (e.g. `key:run,read`) — it cannot tell that `read` is a scope of
    `key`, not a new entry. The fix: split on a regex that matches `<key>:<scopes>`
    greedily, where every token after the `:` is validated as a KNOWN scope.
    Unknown scope-shaped tokens are treated as continuations of the current
    entry's scope list. Tokens with no `:` at all are treated as malformed
    and dropped (the preflight script surfaces this for the operator).
    """
    records: list[ApiKeyRecord] = []
    # Split on a token boundary: a comma followed by an optional space and
    # then a new `<token>:<scopes>` shape. Lookahead so the comma stays in
    # the previous chunk.
    parts = __import__("re").split(r",\s*(?=\S[^,]*:)", raw)
    for i, entry in enumerate(p.strip() for p in parts if p.strip()):
        if ":" not in entry:
            continue
        secret, _, scopes_part = entry.partition(":")
        scopes = frozenset(s.strip() for s in scopes_part.split(",") if s.strip())
        # Validate against the known scope set. Anything else means the
        # operator mistyped a scope name OR included a stray token — both
        # are configuration errors that should fail closed.
        if not scopes or not scopes <= _KNOWN_SCOPES:
            continue
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
