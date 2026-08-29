"""HTTP middleware: request size cap, per-key/IP rate limiting, correlation IDs.

Rate limiting is an in-process sliding window — correct for a single replica
or as a backstop behind an edge/LB limit. For multi-replica deployments move
the counter to Redis (interface here is intentionally tiny to swap).
"""
import threading
import time
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.observability.logging_setup import correlation_id_var
from app.security.auth import API_KEY_HEADER

import app.config as config


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        # Check declared Content-Length first (fast path).
        length = request.headers.get("content-length")
        if length is not None:
            try:
                if int(length) > config.MAX_BODY_BYTES:
                    return JSONResponse(
                        {"detail": "Request body too large"},
                        status_code=413,
                    )
            except ValueError:
                pass  # malformed header — let the framework handle it
        # Also guard against chunked/missing Content-Length by reading the body
        # up to the limit.  If the body exceeds the cap, reject.
        if request.method in ("POST", "PUT", "PATCH"):
            body = await request.body()
            if len(body) > config.MAX_BODY_BYTES:
                return JSONResponse(
                    {"detail": "Request body too large"},
                    status_code=413,
                )
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, limit_per_minute: int | None = None):
        super().__init__(app)
        self.limit = limit_per_minute or config.RATE_LIMIT_PER_MINUTE
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._window_s = 60.0

    def _client_key(self, request: Request) -> str:
        provided = request.headers.get(API_KEY_HEADER, "")
        if provided:
            # Key the bucket on the digest of the presented secret so raw keys
            # never sit in memory as plaintext.
            import hashlib

            return "key:" + hashlib.sha256(provided.encode()).hexdigest()[:16]
        client = request.client.host if request.client else "unknown"
        return f"ip:{client}"

    def _allow(self, bucket_key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[bucket_key]
            while hits and now - hits[0] > self._window_s:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True

    async def dispatch(self, request: Request, call_next) -> Response:
        # Health probes are never throttled.
        if request.url.path in ("/health", "/readyz"):
            return await call_next(request)
        if not self._allow(self._client_key(request)):
            return JSONResponse(
                {"detail": "Rate limit exceeded"}, status_code=429,
                headers={"Retry-After": "60"},
            )
        return await call_next(request)


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        cid = request.headers.get("X-Request-ID") or __import__("uuid").uuid4().hex[:16]
        token = correlation_id_var.set(cid)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = cid
            return response
        finally:
            correlation_id_var.reset(token)
