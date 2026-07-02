"""In-process token-bucket rate limiting.

Single-replica by design — the app already assumes one process for its in-RAM stores
(main.py fail-fasts on WEB_CONCURRENCY>1), so an in-process limiter is consistent and
needs no Redis. It is a first line of defense for a public endpoint (auth brute-force,
request floods, LLM cost abuse); pair it with an ingress/WAF limit for multi-node scale.

Pure and testable: `TokenBucket` takes an injectable clock, and `client_ip` is a plain
function over request headers. The FastAPI glue (`RateLimitMiddleware`) lives here too.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class _Bucket:
    tokens: float
    last: float


class TokenBucket:
    """Per-key token bucket. `allow(key)` consumes one token, refilling at `rate/min`.

    Memory is bounded: when the key count hits `max_keys`, fully-refilled (idle) buckets
    are dropped first, then the least-recently-used one — so a flood of distinct keys (e.g.
    spoofed source IPs when XFF is untrusted) can't grow the map without bound.
    """

    def __init__(
        self,
        rate_per_minute: int,
        *,
        burst: int | None = None,
        max_keys: int = 20000,
        clock: object = time.monotonic,
    ) -> None:
        # rate <= 0 disables this bucket (always allow) — lets a deployment turn one policy off.
        self._rate = max(0.0, rate_per_minute) / 60.0  # tokens per second
        self._capacity = float(burst if burst is not None else max(1, rate_per_minute))
        self._enabled = rate_per_minute > 0
        self._max_keys = max_keys
        self._clock = clock  # type: ignore[assignment]
        self._buckets: dict[str, _Bucket] = {}

    def _refilled(self, b: _Bucket, now: float) -> float:
        return min(self._capacity, b.tokens + (now - b.last) * self._rate)

    def allow(self, key: str) -> bool:
        if not self._enabled:
            return True
        now = float(self._clock())  # type: ignore[operator]
        b = self._buckets.get(key)
        if b is None:
            if len(self._buckets) >= self._max_keys:
                self._evict(now)
            self._buckets[key] = _Bucket(self._capacity - 1.0, now)
            return True
        b.tokens = self._refilled(b, now)
        b.last = now
        if b.tokens >= 1.0:
            b.tokens -= 1.0
            return True
        return False

    def _evict(self, now: float) -> None:
        idle = [k for k, b in self._buckets.items() if self._refilled(b, now) >= self._capacity]
        for k in idle:
            del self._buckets[k]
        if not idle and self._buckets:
            oldest = min(self._buckets, key=lambda k: self._buckets[k].last)
            del self._buckets[oldest]


def client_ip(request: Request, trusted_proxy_hops: int) -> str:
    """Resolve the client IP for rate-limit keying.

    With `trusted_proxy_hops > 0`, take the IP `n` positions from the RIGHT of
    X-Forwarded-For — the entry the trusted proxy (e.g. ALB, hops=1) appended — so a
    client-supplied leading XFF cannot be used to rotate identities and evade the limit.
    Falls back to the socket peer when there is no XFF or hops == 0.
    """
    if trusted_proxy_hops > 0:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            parts = [p.strip() for p in xff.split(",") if p.strip()]
            if parts:
                return parts[max(0, len(parts) - trusted_proxy_hops)]
    return request.client.host if request.client else "unknown"


# Paths exempt from the per-IP HTTP limiter:
#  - /health*  : liveness/readiness probes (k8s/ALB) must never be throttled.
#  - /api/llm-proxy, /api/mcp-proxy : called by the sandbox via the host-gateway, so every
#    conversation shares ONE source IP — a per-IP limit would throttle all of them together.
#    They carry their own auth (per-turn grant / internal JWT), wall-clock caps and fail-closed
#    gates, so they are governed there, not here.
_EXEMPT_PREFIXES = ("/health", "/api/llm-proxy", "/api/mcp-proxy")


class RateLimitMiddleware:
    """Per-IP token-bucket limiting for HTTP requests (a stricter bucket for /api/auth).

    ASGI middleware (not BaseHTTPMiddleware) so it only touches `http` scopes — WebSocket
    turn-rate limiting is enforced in the chat handler where the user identity is known.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        s = get_settings()
        self._enabled = s.rate_limit_enabled
        self._hops = s.rate_limit_trusted_proxy_hops
        self._general = TokenBucket(s.rate_limit_ip_per_minute)
        self._auth = TokenBucket(s.rate_limit_auth_ip_per_minute)

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        if scope["type"] != "http" or not self._enabled:
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        path = request.url.path
        # Preflight requests carry no credentials and must reach the CORS layer.
        if request.method == "OPTIONS" or path.startswith(_EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return
        key = client_ip(request, self._hops)
        bucket = self._auth if path.startswith("/api/auth") else self._general
        if not bucket.allow(key):
            logger.warning("rate_limited", path=path, client=key)
            response = JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please slow down and retry shortly."},
                headers={"Retry-After": "10"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
