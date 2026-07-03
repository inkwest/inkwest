"""Tests for the in-process rate limiter (app.core.rate_limit)."""

from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app.core.rate_limit import TokenBucket, client_ip


class _Clock:
    """Injectable monotonic clock for deterministic refill tests."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def test_bucket_allows_up_to_capacity_then_blocks():
    clock = _Clock()
    b = TokenBucket(60, clock=clock)  # capacity 60, refill 1/sec
    assert all(b.allow("ip") for _ in range(60))  # burst up to capacity
    assert b.allow("ip") is False  # 61st in the same instant is blocked


def test_bucket_refills_over_time():
    clock = _Clock()
    b = TokenBucket(60, clock=clock)
    for _ in range(60):
        b.allow("ip")
    assert b.allow("ip") is False
    clock.advance(2.0)  # 2 seconds → 2 tokens back (1/sec)
    assert b.allow("ip") is True
    assert b.allow("ip") is True
    assert b.allow("ip") is False


def test_bucket_keys_are_independent():
    clock = _Clock()
    b = TokenBucket(1, clock=clock)  # 1 token capacity
    assert b.allow("a") is True
    assert b.allow("a") is False
    assert b.allow("b") is True  # different key has its own bucket


def test_zero_rate_disables_bucket():
    b = TokenBucket(0)
    assert all(b.allow("ip") for _ in range(1000))  # always allowed


def test_bucket_evicts_to_stay_bounded():
    clock = _Clock()
    b = TokenBucket(60, max_keys=10, clock=clock)
    for i in range(10):
        b.allow(f"k{i}")
    clock.advance(120)  # all idle buckets fully refilled → evictable
    b.allow("new")  # triggers eviction of idle keys
    assert len(b._buckets) <= 10


def _request(headers: dict[str, str], peer: str | None = "203.0.113.9") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {
        "type": "http",
        "headers": raw,
        "client": (peer, 12345) if peer else None,
    }
    return Request(scope)


def test_client_ip_prefers_socket_peer_when_hops_zero():
    req = _request({"x-forwarded-for": "1.2.3.4"}, peer="10.0.0.1")
    assert client_ip(req, trusted_proxy_hops=0) == "10.0.0.1"


def test_client_ip_takes_alb_appended_entry_with_one_hop():
    # ALB appends the real client IP; a client-spoofed leading value must be ignored.
    req = _request({"x-forwarded-for": "9.9.9.9, 203.0.113.7"}, peer="10.0.0.1")
    assert client_ip(req, trusted_proxy_hops=1) == "203.0.113.7"


def test_client_ip_single_xff_entry():
    req = _request({"x-forwarded-for": "203.0.113.7"}, peer="10.0.0.1")
    assert client_ip(req, trusted_proxy_hops=1) == "203.0.113.7"


def test_client_ip_two_trusted_hops():
    req = _request({"x-forwarded-for": "client, alb, cdn"}, peer="10.0.0.1")
    assert client_ip(req, trusted_proxy_hops=2) == "alb"


def test_client_ip_falls_back_to_peer_without_xff():
    req = _request({}, peer="198.51.100.2")
    assert client_ip(req, trusted_proxy_hops=1) == "198.51.100.2"


@pytest.mark.asyncio
async def test_middleware_returns_429_past_limit(monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.core import rate_limit

    monkeypatch.setattr(
        rate_limit,
        "get_settings",
        lambda: SimpleNamespace(
            rate_limit_enabled=True,
            rate_limit_trusted_proxy_hops=0,
            rate_limit_ip_per_minute=3,
            rate_limit_auth_ip_per_minute=3,
        ),
    )
    app = FastAPI()
    app.add_middleware(rate_limit.RateLimitMiddleware)

    @app.get("/api/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        codes = [(await client.get("/api/ping")).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200]
    assert codes[3] == 429 and codes[4] == 429


@pytest.mark.asyncio
async def test_middleware_exempts_health_and_proxies(monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.core import rate_limit

    monkeypatch.setattr(
        rate_limit,
        "get_settings",
        lambda: SimpleNamespace(
            rate_limit_enabled=True,
            rate_limit_trusted_proxy_hops=0,
            rate_limit_ip_per_minute=1,
            rate_limit_auth_ip_per_minute=1,
        ),
    )
    app = FastAPI()
    app.add_middleware(rate_limit.RateLimitMiddleware)

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        codes = [(await client.get("/health")).status_code for _ in range(5)]
    assert codes == [200] * 5  # never throttled
