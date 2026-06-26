"""Tests for the LLM auth broker route (app.api.routes.llm_proxy).

Upstream Bedrock is stubbed with httpx.MockTransport; settings + the model allowlist are
patched so the focus is the broker contract: JWT auth, model/op restriction, header swap +
passthrough, ARN round-trip, body cap, and the error-code split.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.services import llm_proxy_token

_U = uuid.UUID("00000000-0000-4000-8000-000000000001")
_S = uuid.UUID("00000000-0000-4000-8000-000000000002")

MODEL = "anthropic.claude-opus-4-8"
ARN = "arn:aws:bedrock:eu-west-1:123456789012:inference-profile/eu.anthropic.claude-x"
REGION = "eu-west-1"
REAL_KEY = "real-bedrock-key"  # pragma: allowlist secret


def _streaming(
    status: int, body: bytes = b"", headers: dict[str, str] | None = None
) -> httpx.Response:
    """A genuinely streaming response — the route uses aiter_raw(), which a content=bytes
    MockTransport response would report as already-consumed."""

    async def gen():
        yield body

    return httpx.Response(status, headers=headers or {}, content=gen())


def _settings(bearer: str | None = REAL_KEY, region: str | None = REGION) -> SimpleNamespace:
    return SimpleNamespace(aws_bearer_token_bedrock=bearer, aws_region=region)


def _bearer(token: str | None = None) -> dict[str, str]:
    tok = token if token is not None else llm_proxy_token.mint(_U, _S)
    return {"Authorization": f"Bearer {tok}"}


async def _client() -> AsyncClient:
    from app.main import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _patches(handler, *, settings=None, allowed=None):
    """Patch the route's collaborators; set the upstream MockTransport handler."""
    from app.api.routes import llm_proxy

    llm_proxy._transport = httpx.MockTransport(handler)
    return [
        patch("app.api.routes.llm_proxy.get_settings", return_value=settings or _settings()),
        patch(
            "app.api.routes.llm_proxy._allowed_targets",
            return_value=allowed if allowed is not None else {MODEL, ARN},
        ),
    ]


@pytest.mark.asyncio
async def test_happy_path_swaps_auth_and_forwards() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        seen["bedrock_hdr"] = request.headers.get("x-amzn-bedrock-accept")
        seen["body"] = request.content
        return _streaming(
            200, b"event-bytes", {"content-type": "application/vnd.amazon.eventstream"}
        )

    ps = _patches(handler)
    for p in ps:
        p.start()
    try:
        async with await _client() as c:
            resp = await c.post(
                f"/api/llm-proxy/model/{MODEL}/converse-stream",
                headers={**_bearer(), "X-Amzn-Bedrock-Accept": "application/json"},
                content=b'{"messages":[]}',
            )
        assert resp.status_code == 200
        assert resp.content == b"event-bytes"
        assert seen["auth"] == f"Bearer {REAL_KEY}"  # internal JWT swapped for the real bearer
        assert seen["body"] == b'{"messages":[]}'
        assert seen["bedrock_hdr"] == "application/json"  # bedrock-specific header forwarded
        assert seen["url"] == (
            f"https://bedrock-runtime.{REGION}.amazonaws.com/model/{MODEL}/converse-stream"
        )
        assert resp.headers.get("content-type") == "application/vnd.amazon.eventstream"
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_arn_target_round_trips_and_is_encoded() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return _streaming(200, b"ok")

    ps = _patches(handler)
    for p in ps:
        p.start()
    try:
        async with await _client() as c:
            # boto3 percent-encodes the modelId segment; the op stays a literal trailing path.
            resp = await c.post(
                f"/api/llm-proxy/model/{ARN}/invoke-with-response-stream",
                headers=_bearer(),
                content=b"{}",
            )
        assert resp.status_code == 200
        # The ARN (with ':' and '/') is re-encoded as a single path segment for AWS.
        expected = (
            f"https://bedrock-runtime.{REGION}.amazonaws.com/model/"
            f"{quote(ARN, safe='')}/invoke-with-response-stream"
        )
        assert seen["url"] == expected
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_missing_or_bad_jwt_is_401() -> None:
    ps = _patches(lambda r: _streaming(200))
    for p in ps:
        p.start()
    try:
        async with await _client() as c:
            no_auth = await c.post(f"/api/llm-proxy/model/{MODEL}/converse", content=b"{}")
            # An mcp-proxy token (wrong aud) signed with the same secret must not pass.
            import jwt as _jwt

            from app.core.auth import JWT_ALGORITHM
            from app.core.config import get_settings

            wrong = _jwt.encode(
                {"aud": "mcp-proxy", "type": "mcp-proxy"},
                get_settings().secret_key,
                algorithm=JWT_ALGORITHM,
            )
            wrong_aud = await c.post(
                f"/api/llm-proxy/model/{MODEL}/converse", headers=_bearer(wrong), content=b"{}"
            )
        assert no_auth.status_code == 401
        assert wrong_aud.status_code == 401
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_disallowed_op_is_400() -> None:
    fired = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        fired["n"] += 1
        return _streaming(200)

    ps = _patches(handler)
    for p in ps:
        p.start()
    try:
        async with await _client() as c:
            resp = await c.post(
                f"/api/llm-proxy/model/{MODEL}/delete-model", headers=_bearer(), content=b"{}"
            )
        assert resp.status_code == 400
        assert fired["n"] == 0  # rejected before any upstream call
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_malformed_path_no_op_is_400() -> None:
    ps = _patches(lambda r: _streaming(200))
    for p in ps:
        p.start()
    try:
        async with await _client() as c:
            # No op segment after the model id.
            resp = await c.post(f"/api/llm-proxy/model/{MODEL}", headers=_bearer(), content=b"{}")
        assert resp.status_code == 400
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_model_not_permitted_is_403() -> None:
    fired = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        fired["n"] += 1
        return _streaming(200)

    # Allowlist excludes the requested model.
    ps = _patches(handler, allowed={"some.other-model"})
    for p in ps:
        p.start()
    try:
        async with await _client() as c:
            resp = await c.post(
                f"/api/llm-proxy/model/{MODEL}/converse", headers=_bearer(), content=b"{}"
            )
        assert resp.status_code == 403
        assert fired["n"] == 0  # restriction enforced before upstream
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_not_configured_is_503() -> None:
    fired = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        fired["n"] += 1
        return _streaming(200)

    # Flag enabled but the backend bearer isn't provisioned.
    ps = _patches(handler, settings=_settings(bearer=None))
    for p in ps:
        p.start()
    try:
        async with await _client() as c:
            resp = await c.post(
                f"/api/llm-proxy/model/{MODEL}/converse", headers=_bearer(), content=b"{}"
            )
        assert resp.status_code == 503
        assert fired["n"] == 0
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_missing_region_is_503() -> None:
    ps = _patches(lambda r: _streaming(200), settings=_settings(region=None))
    for p in ps:
        p.start()
    try:
        async with await _client() as c:
            resp = await c.post(
                f"/api/llm-proxy/model/{MODEL}/converse", headers=_bearer(), content=b"{}"
            )
        assert resp.status_code == 503
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_oversized_body_is_413() -> None:
    fired = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        fired["n"] += 1
        return _streaming(200)

    ps = _patches(handler)
    for p in ps:
        p.start()
    try:
        with patch("app.api.routes.llm_proxy.LLM_PROXY_MAX_BODY_BYTES", 4):
            async with await _client() as c:
                resp = await c.post(
                    f"/api/llm-proxy/model/{MODEL}/converse",
                    headers=_bearer(),
                    content=b"WAY-TOO-LONG-BODY",
                )
        assert resp.status_code == 413
        assert fired["n"] == 0  # rejected before the upstream fetch
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_strips_hop_by_hop_response_headers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _streaming(
            200,
            b"x",
            {"transfer-encoding": "chunked", "content-type": "application/vnd.amazon.eventstream"},
        )

    ps = _patches(handler)
    for p in ps:
        p.start()
    try:
        async with await _client() as c:
            resp = await c.post(
                f"/api/llm-proxy/model/{MODEL}/converse-stream", headers=_bearer(), content=b"{}"
            )
        assert resp.status_code == 200
        assert "transfer-encoding" not in {k.lower() for k in resp.headers}
        assert resp.headers.get("content-type") == "application/vnd.amazon.eventstream"
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_upstream_connect_error_is_502() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    ps = _patches(handler)
    for p in ps:
        p.start()
    try:
        async with await _client() as c:
            resp = await c.post(
                f"/api/llm-proxy/model/{MODEL}/converse", headers=_bearer(), content=b"{}"
            )
        assert resp.status_code == 502
    finally:
        for p in ps:
            p.stop()
        from app.api.routes import llm_proxy

        llm_proxy._transport = None


@pytest.mark.asyncio
async def test_query_string_forwarded() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return _streaming(200, b"ok")

    ps = _patches(handler)
    for p in ps:
        p.start()
    try:
        async with await _client() as c:
            resp = await c.post(
                f"/api/llm-proxy/model/{MODEL}/converse?foo=bar",
                headers=_bearer(),
                content=b"{}",
            )
        assert resp.status_code == 200
        assert seen["url"].endswith("/converse?foo=bar")  # query preserved to upstream
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_chunked_oversized_body_is_413() -> None:
    """A chunked request (no Content-Length) over the cap is caught by the in-stream guard,
    not the declared-length one."""
    fired = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        fired["n"] += 1
        return _streaming(200)

    async def _chunks():
        # An async iterator body makes httpx send chunked (omits Content-Length), so the
        # declared-length check can't fire — the per-chunk accumulator must.
        yield b"AAAA"
        yield b"BBBB"

    ps = _patches(handler)
    for p in ps:
        p.start()
    try:
        with patch("app.api.routes.llm_proxy.LLM_PROXY_MAX_BODY_BYTES", 4):
            async with await _client() as c:
                resp = await c.post(
                    f"/api/llm-proxy/model/{MODEL}/converse", headers=_bearer(), content=_chunks()
                )
        assert resp.status_code == 413
        assert fired["n"] == 0  # rejected before the upstream fetch
    finally:
        for p in ps:
            p.stop()


def test_allowed_targets_enabled_subset_and_arns() -> None:
    """The live restriction set = enabled model IDs ∪ their ARNs; disabled rows are excluded."""
    from app.api.routes import llm_proxy

    entries = [
        SimpleNamespace(model_id="m.enabled", enabled=True, arn="arn:enabled"),
        SimpleNamespace(model_id="m.disabled", enabled=False, arn="arn:disabled"),
        SimpleNamespace(model_id="m.noarn", enabled=True, arn=None),
    ]
    with patch("app.api.routes.llm_proxy.model_registry.merged_entries", return_value=entries):
        assert llm_proxy._allowed_targets() == {"m.enabled", "arn:enabled", "m.noarn"}


def test_allowed_targets_none_enabled_falls_back_to_all() -> None:
    """No enabled rows ⇒ the whole catalog is permitted (fresh-deploy default-open)."""
    from app.api.routes import llm_proxy

    entries = [
        SimpleNamespace(model_id="m.a", enabled=False, arn=None),
        SimpleNamespace(model_id="m.b", enabled=False, arn="arn:b"),
    ]
    with patch("app.api.routes.llm_proxy.model_registry.merged_entries", return_value=entries):
        assert llm_proxy._allowed_targets() == {"m.a", "m.b", "arn:b"}
