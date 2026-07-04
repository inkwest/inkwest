"""Tests for the LLM auth broker route (app.api.routes.llm_proxy).

Upstream Bedrock is stubbed with httpx.MockTransport; settings + the model allowlist are
patched so the focus is the broker contract: JWT auth, model/op restriction, header swap +
passthrough, ARN round-trip, body cap, and the error-code split.
"""

import asyncio
import contextlib
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
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


@pytest.fixture(autouse=True)
def _reset_proxy_state():
    """The route's upstream transport is a module global and the grant store is process-wide;
    reset both around every test so a handler/grant from one test can't leak into the next
    (order-dependent flakiness)."""
    from app.api.routes import llm_proxy

    llm_proxy._transport = None
    llm_proxy_token._grants.clear()
    llm_proxy._persist_tasks.clear()
    yield
    llm_proxy._transport = None
    llm_proxy_token._grants.clear()
    llm_proxy._persist_tasks.clear()


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
        assert seen["auth"] == f"Bearer {REAL_KEY}"  # grant swapped for the real bearer
        # The real backend bearer must NEVER appear in what's streamed back to the sandbox.
        assert REAL_KEY.encode() not in resp.content
        assert REAL_KEY not in " ".join(f"{k}:{v}" for k, v in resp.headers.items())
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
async def test_missing_or_unknown_grant_is_401() -> None:
    ps = _patches(lambda r: _streaming(200))
    for p in ps:
        p.start()
    try:
        async with await _client() as c:
            no_auth = await c.post(f"/api/llm-proxy/model/{MODEL}/converse", content=b"{}")
            # An opaque token that was never minted (or already burned) resolves to no grant.
            unknown = await c.post(
                f"/api/llm-proxy/model/{MODEL}/converse",
                headers=_bearer("never-minted-token"),
                content=b"{}",
            )
        assert no_auth.status_code == 401
        assert unknown.status_code == 401
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_burned_grant_is_401() -> None:
    """A grant valid mid-turn is rejected once the orchestrator burns it at turn end."""
    ps = _patches(lambda r: _streaming(200, b"ok"))
    for p in ps:
        p.start()
    try:
        tok = llm_proxy_token.mint(_U, _S)
        async with await _client() as c:
            live = await c.post(
                f"/api/llm-proxy/model/{MODEL}/converse", headers=_bearer(tok), content=b"{}"
            )
            llm_proxy_token.burn(tok)  # turn ends
            dead = await c.post(
                f"/api/llm-proxy/model/{MODEL}/converse", headers=_bearer(tok), content=b"{}"
            )
        assert live.status_code == 200
        assert dead.status_code == 401
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


def test_allowed_targets_none_enabled_is_fail_closed() -> None:
    """No enabled rows ⇒ the broker permits NOTHING. The model picker is default-OPEN on a fresh
    deploy, but the broker is a credential gate — defaulting open would let any grant invoke any
    discovered model on an unconfigured backend."""
    from app.api.routes import llm_proxy

    entries = [
        SimpleNamespace(model_id="m.a", enabled=False, arn=None),
        SimpleNamespace(model_id="m.b", enabled=False, arn="arn:b"),
    ]
    with patch("app.api.routes.llm_proxy.model_registry.merged_entries", return_value=entries):
        assert llm_proxy._allowed_targets() == set()


def test_allowed_targets_includes_inference_profile_ids() -> None:
    """An enabled model's regional inference-profile id (which the CLI auto-upgrades a bare id to
    before invoking) is allowed alongside the friendly id + ARN."""
    from app.api.routes import llm_proxy

    entries = [SimpleNamespace(model_id="anthropic.claude-x", enabled=True, arn="arn:eu-x")]
    with (
        patch("app.api.routes.llm_proxy.model_registry.merged_entries", return_value=entries),
        patch(
            "app.api.routes.llm_proxy.model_catalog.profile_ids_for_arn",
            return_value={"eu.anthropic.claude-x"},
        ),
    ):
        assert llm_proxy._allowed_targets() == {
            "anthropic.claude-x",
            "arn:eu-x",
            "eu.anthropic.claude-x",
        }


# --- control plane (read-only GET allowlist → bedrock.<region>) -------------------------


@pytest.mark.asyncio
async def test_control_plane_inference_profiles_forwarded() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["method"] = request.method
        return _streaming(200, b'{"inferenceProfileSummaries":[]}')

    ps = _patches(handler)
    for p in ps:
        p.start()
    try:
        async with await _client() as c:
            resp = await c.get("/api/llm-proxy/inference-profiles", headers=_bearer())
        assert resp.status_code == 200
        assert seen["method"] == "GET"
        assert seen["auth"] == f"Bearer {REAL_KEY}"  # grant swapped for the real bearer
        # CONTROL plane host (bedrock.), not the data-plane bedrock-runtime.
        assert seen["url"] == f"https://bedrock.{REGION}.amazonaws.com/inference-profiles"
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_control_plane_query_and_subpath_forwarded() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return _streaming(200, b"{}")

    ps = _patches(handler)
    for p in ps:
        p.start()
    try:
        async with await _client() as c:
            resp = await c.get(
                "/api/llm-proxy/foundation-models?byProvider=anthropic", headers=_bearer()
            )
        assert resp.status_code == 200
        assert seen["url"] == (
            f"https://bedrock.{REGION}.amazonaws.com/foundation-models?byProvider=anthropic"
        )
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_control_plane_unknown_resource_is_404_after_auth() -> None:
    fired = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        fired["n"] += 1
        return _streaming(200)

    ps = _patches(handler)
    for p in ps:
        p.start()
    try:
        async with await _client() as c:
            # A non-allowlisted GET resource (and a model-path GET) must not reach upstream.
            unknown = await c.get("/api/llm-proxy/custom-models", headers=_bearer())
            model_get = await c.get(f"/api/llm-proxy/model/{MODEL}/converse", headers=_bearer())
        assert unknown.status_code == 404
        assert model_get.status_code == 404  # "model" is not an allowlisted control-plane resource
        assert fired["n"] == 0
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_control_plane_requires_grant_then_config() -> None:
    ps = _patches(lambda r: _streaming(200))
    for p in ps:
        p.start()
    try:
        async with await _client() as c:
            no_auth = await c.get("/api/llm-proxy/inference-profiles")
        assert no_auth.status_code == 401
    finally:
        for p in ps:
            p.stop()
    # Not provisioned ⇒ 503 (after auth passes).
    ps = _patches(lambda r: _streaming(200), settings=_settings(bearer=None))
    for p in ps:
        p.start()
    try:
        async with await _client() as c:
            resp = await c.get("/api/llm-proxy/inference-profiles", headers=_bearer())
        assert resp.status_code == 503
    finally:
        for p in ps:
            p.stop()


# --- passthrough / hardening regressions ------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 429, 500])
async def test_upstream_non_2xx_passthrough(status: int) -> None:
    """A Bedrock error status + body is forwarded verbatim — the broker is a transparent
    passthrough, not an error remapper (only connection-level failures become 502)."""
    body = b'{"message":"ValidationException"}'

    def handler(request: httpx.Request) -> httpx.Response:
        return _streaming(status, body)

    ps = _patches(handler)
    for p in ps:
        p.start()
    try:
        async with await _client() as c:
            resp = await c.post(
                f"/api/llm-proxy/model/{MODEL}/converse", headers=_bearer(), content=b"{}"
            )
        assert resp.status_code == status
        assert resp.content == body
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_disallowed_op_rejected_before_auth() -> None:
    """Op/path validity is checked BEFORE auth (400), so an unauthenticated probe still can't
    reach upstream. Pins the chosen ordering."""
    fired = {"n": 0}

    ps = _patches(lambda r: (fired.__setitem__("n", fired["n"] + 1), _streaming(200))[1])
    for p in ps:
        p.start()
    try:
        async with await _client() as c:
            # No Authorization header + a disallowed op.
            resp = await c.post(f"/api/llm-proxy/model/{MODEL}/delete-everything", content=b"{}")
        assert resp.status_code == 400
        assert fired["n"] == 0
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_amz_credential_headers_stripped_bedrock_kept() -> None:
    """A compromised sandbox must not inject AWS SigV4/credential headers (x-amz-*) onto the
    broker's authenticated call; Bedrock-specific x-amzn-bedrock-* headers still pass."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["amz_sec"] = request.headers.get("x-amz-security-token")
        seen["amz_date"] = request.headers.get("x-amz-date")
        seen["bedrock"] = request.headers.get("x-amzn-bedrock-accept")
        return _streaming(200, b"ok")

    ps = _patches(handler)
    for p in ps:
        p.start()
    try:
        async with await _client() as c:
            resp = await c.post(
                f"/api/llm-proxy/model/{MODEL}/converse",
                headers={
                    **_bearer(),
                    "X-Amz-Security-Token": "injected",
                    "X-Amz-Date": "20260101T000000Z",
                    "X-Amzn-Bedrock-Accept": "application/json",
                },
                content=b"{}",
            )
        assert resp.status_code == 200
        assert seen["amz_sec"] is None  # SigV4/credential header dropped
        assert seen["amz_date"] is None
        assert seen["bedrock"] == "application/json"  # Bedrock functional header preserved
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_control_plane_rejects_dot_segments() -> None:
    """A path that tries to traverse out of the resource allowlist (`..`) is 400'd before the
    allowlist check, never reaching upstream — the allowlist can't be escaped."""
    fired = {"n": 0}

    ps = _patches(lambda r: (fired.__setitem__("n", fired["n"] + 1), _streaming(200))[1])
    for p in ps:
        p.start()
    try:
        async with await _client() as c:
            resp = await c.get("/api/llm-proxy/inference-profiles/..%2ffoo", headers=_bearer())
        assert resp.status_code == 400
        assert fired["n"] == 0
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_stream_wall_clock_deadline_releases() -> None:
    """The total wall-clock cap stops a stream whose upstream dribbles forever (slowloris),
    instead of pinning the worker for the full inter-byte idle ceiling."""
    from app.api.routes import llm_proxy

    def handler(request: httpx.Request) -> httpx.Response:
        async def gen():
            yield b"first"
            await asyncio.sleep(60)  # would otherwise hold the connection
            yield b"never"

        return httpx.Response(200, content=gen())

    ps = _patches(handler)
    for p in ps:
        p.start()
    try:
        with patch.object(llm_proxy, "LLM_PROXY_STREAM_MAX_SECONDS", 0.05):
            async with await _client() as c:
                resp = await c.post(
                    f"/api/llm-proxy/model/{MODEL}/converse-stream",
                    headers=_bearer(),
                    content=b"{}",
                )
                # The deadline fires mid-stream: first chunk arrives, then the bounded iterator
                # stops cleanly rather than hanging on the 60s sleep.
                assert resp.status_code == 200
                assert resp.content == b"first"
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_close_stream_closes_client_even_if_aclose_raises() -> None:
    """Teardown robustness: if the upstream stream aclose() raises (already-reset connection),
    the client must still be closed or its pool leaks."""
    from app.api.routes._proxy_stream import close_stream

    class _Boom:
        async def aclose(self):
            raise RuntimeError("already reset")

    closed = {"client": False}

    class _Client:
        async def aclose(self):
            closed["client"] = True

    with pytest.raises(RuntimeError):
        await close_stream(_Boom(), _Client())
    assert closed["client"] is True  # closed despite the stream aclose raising


# --- Broker metering (P1): passive tee + post-stream usage persistence -------------------
#
# Upstream bytes are the REAL captured fixtures from tests/fixtures/bedrock/ (see README
# there) — the tests assert byte-identical forwarding AND one UsageRecord per call with the
# correct 4-way token split. record_usage / the DB session factory are patched at the route's
# namespace; the tee itself is exercised for real.

_FIXTURES = Path(__file__).parent / "fixtures" / "bedrock"

_ENGINE = "claude-cli"
_TURN = uuid.UUID("00000000-0000-4000-8000-00000000aaaa")


def _fixture_streaming(name: str, chunk_size: int = 1024) -> httpx.Response:
    """A streaming MockTransport response replaying a real fixture in several chunks."""
    data = (_FIXTURES / name).read_bytes()

    async def gen():
        for i in range(0, len(data), chunk_size):
            yield data[i : i + chunk_size]

    return httpx.Response(
        200, headers={"content-type": "application/vnd.amazon.eventstream"}, content=gen()
    )


def _metering_patches(handler, record_usage_mock):
    """The standard patch set + a stubbed DB layer for the post-stream persist."""
    db = AsyncMock()

    @contextlib.asynccontextmanager
    async def session_maker():
        yield db

    return db, _patches(handler) + [
        patch("app.api.routes.llm_proxy.record_usage", record_usage_mock),
        patch("app.api.routes.llm_proxy.async_session_maker", session_maker),
        patch("app.api.routes.llm_proxy._PERSIST_RETRY_DELAY_SECONDS", 0),
    ]


async def _drain():
    """The persist runs in a detached task — settle it before asserting."""
    from app.api.routes import llm_proxy

    await llm_proxy.drain_metering_persists()


def _metered_bearer() -> dict[str, str]:
    tok = llm_proxy_token.mint(_U, _S, engine=_ENGINE, turn_id=_TURN)
    return {"Authorization": f"Bearer {tok}"}


def _failures(reason: str) -> float:
    """Current value of the metering-failure counter for one reason label, via the
    public collect() API (never-incremented label ⇒ 0.0)."""
    from app.core import metrics

    for metric in metrics.llm_metering_failures_total.collect():
        for sample in metric.samples:
            if sample.name.endswith("_total") and sample.labels.get("reason") == reason:
                return sample.value
    return 0.0


def _all_failures() -> float:
    return sum(_failures(r) for r in ("no_usage", "no_session", "persist_failed"))


_METER_CASES = [
    # (fixture, op, in, out, cache_read, cache_write)
    ("invoke_stream_nocache.bin", "invoke-with-response-stream", 365, 17, 0, 0),
    ("invoke_stream_cachewrite.bin", "invoke-with-response-stream", 10, 349, 0, 26797),
    ("invoke_stream_cacheread.bin", "invoke-with-response-stream", 10, 885, 27082, 3242),
    ("converse_stream_nocache.bin", "converse-stream", 6472, 4, 0, 0),
    ("converse_nonstream_cachekeys.bin", "converse", 11183, 461, 0, 0),
    ("converse_nonstream_cachewrite.bin", "converse", 7, 16, 0, 12608),
    ("converse_nonstream_cacheread.bin", "converse", 7, 16, 12608, 0),
    ("invoke_nonstream.bin", "invoke", 8, 1, 0, 0),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fixture", "op", "in_tok", "out_tok", "read_tok", "write_tok"),
    _METER_CASES,
    ids=[c[0] for c in _METER_CASES],
)
async def test_metering_forwards_bytes_and_persists_one_row(
    fixture, op, in_tok, out_tok, read_tok, write_tok
) -> None:
    data = (_FIXTURES / fixture).read_bytes()
    record = AsyncMock()
    db, ps = _metering_patches(lambda request: _fixture_streaming(fixture), record)
    failures_before = _all_failures()
    for p in ps:
        p.start()
    try:
        async with await _client() as c:
            resp = await c.post(
                f"/api/llm-proxy/model/{MODEL}/{op}",
                headers=_metered_bearer(),
                content=b"{}",
            )
        assert resp.status_code == 200
        assert resp.content == data  # forwarded byte-identical
        await _drain()
        # No false alarms: a fully-metered call must leave the failure counter flat.
        assert _all_failures() == failures_before
        record.assert_awaited_once()
        kwargs = record.await_args.kwargs
        assert kwargs["input_tokens"] == in_tok
        assert kwargs["output_tokens"] == out_tok
        assert kwargs["cache_read_tokens"] == read_tok
        assert kwargs["cache_write_tokens"] == write_tok
        assert kwargs["cache_write_1h_tokens"] == 0  # only 5m TTL observed live
        assert kwargs["source"] == "broker"
        assert kwargs["provider"] == _ENGINE
        assert kwargs["turn_id"] == _TURN
        assert kwargs["session_id"] == _S
        assert kwargs["user_id"] == _U
        # target → model normalization ran: the row aggregates under the registry's
        # friendly id (route prefix stripped), not the raw invoke target.
        from app.services import model_registry

        assert kwargs["model"] == model_registry.resolve_invoke_target(MODEL)
        assert kwargs["model"] == "claude-opus-4-8"
        assert kwargs["record_id"] is not None  # fixed PK = the retry idempotency key
        db.commit.assert_awaited_once()
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_metering_truncated_invoke_stream_bills_self_report(caplog) -> None:
    """Truncated REAL invoke-stream fixture (no terminal metrics frame — the abort shape):
    bytes still forwarded byte-identical, and the tee's self-report fallback persists a
    row (AWS bills the cut call; see llm_metering) instead of dropping it."""
    data = (_FIXTURES / "invoke_stream_cachewrite.bin").read_bytes()[:-500]

    def handler(request: httpx.Request) -> httpx.Response:
        async def gen():
            yield data

        return httpx.Response(200, content=gen())

    record = AsyncMock()
    db, ps = _metering_patches(handler, record)
    for p in ps:
        p.start()
    try:
        async with await _client() as c:
            resp = await c.post(
                f"/api/llm-proxy/model/{MODEL}/invoke-with-response-stream",
                headers=_metered_bearer(),
                content=b"{}",
            )
        assert resp.status_code == 200
        assert resp.content == data
        await _drain()
        record.assert_awaited_once()
        kwargs = record.await_args.kwargs
        # Self-report values (message_start), not the billed metrics the cut swallowed.
        assert kwargs["input_tokens"] == 10
        assert kwargs["cache_write_tokens"] == 26797
        assert kwargs["source"] == "broker"
        db.commit.assert_awaited_once()
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_metering_truncated_converse_stream_no_row_bumps_counter(caplog) -> None:
    """Truncated REAL converse-stream fixture: usage only rides the terminal metadata
    event, so a cut stream yields no row — the documented alert counter must move
    (reason=no_usage), the bytes still forward byte-identical."""
    data = (_FIXTURES / "converse_stream_nocache.bin").read_bytes()[:-200]

    def handler(request: httpx.Request) -> httpx.Response:
        async def gen():
            yield data

        return httpx.Response(200, content=gen())

    record = AsyncMock()
    db, ps = _metering_patches(handler, record)
    before = _failures("no_usage")
    for p in ps:
        p.start()
    try:
        with caplog.at_level("WARNING"):
            async with await _client() as c:
                resp = await c.post(
                    f"/api/llm-proxy/model/{MODEL}/converse-stream",
                    headers=_metered_bearer(),
                    content=b"{}",
                )
        assert resp.status_code == 200
        assert resp.content == data
        record.assert_not_awaited()
        db.commit.assert_not_awaited()
        assert "llm_proxy_metering_no_usage" in caplog.text
        assert _failures("no_usage") == before + 1
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_metering_skips_non_2xx() -> None:
    """An errored Bedrock call (4xx/5xx) has no billable tokens — no row."""
    record = AsyncMock()
    db, ps = _metering_patches(
        lambda request: _streaming(429, b'{"message":"Too many requests"}'), record
    )
    for p in ps:
        p.start()
    try:
        async with await _client() as c:
            resp = await c.post(
                f"/api/llm-proxy/model/{MODEL}/converse",
                headers=_metered_bearer(),
                content=b"{}",
            )
        assert resp.status_code == 429
        record.assert_not_awaited()
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_metering_persist_failure_never_breaks_the_stream(caplog) -> None:
    record = AsyncMock(side_effect=RuntimeError("db down"))
    db, ps = _metering_patches(
        lambda request: _fixture_streaming("converse_nonstream_cacheread.bin"), record
    )
    before = _failures("persist_failed")
    for p in ps:
        p.start()
    try:
        with caplog.at_level("WARNING"):
            async with await _client() as c:
                resp = await c.post(
                    f"/api/llm-proxy/model/{MODEL}/converse",
                    headers=_metered_bearer(),
                    content=b"{}",
                )
            await _drain()
        assert resp.status_code == 200
        assert resp.content == (_FIXTURES / "converse_nonstream_cacheread.bin").read_bytes()
        assert record.await_count == 2  # one retry before giving up
        assert "llm_proxy_metering_persist_failed" in caplog.text
        assert _failures("persist_failed") == before + 1
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_metering_persist_retries_transient_failure() -> None:
    """A one-off DB blip (broker rows are the turn's ONLY budget record) must not drop
    the row: attempt 2 lands it, no failure counted."""
    record = AsyncMock(side_effect=[RuntimeError("blip"), AsyncMock()])
    db, ps = _metering_patches(
        lambda request: _fixture_streaming("converse_nonstream_cacheread.bin"), record
    )
    failures_before = _all_failures()
    for p in ps:
        p.start()
    try:
        async with await _client() as c:
            resp = await c.post(
                f"/api/llm-proxy/model/{MODEL}/converse",
                headers=_metered_bearer(),
                content=b"{}",
            )
        await _drain()
        assert resp.status_code == 200
        assert record.await_count == 2
        # Same PK on both attempts: an ambiguous first commit can only collide, not double.
        first, second = record.await_args_list
        assert first.kwargs["record_id"] == second.kwargs["record_id"]
        db.commit.assert_awaited_once()
        assert _all_failures() == failures_before
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_metering_persist_retry_pk_collision_is_success(caplog) -> None:
    """Ambiguous first commit (landed server-side, ack lost): the retry's re-INSERT hits
    the fixed record_id PK — logged as already-persisted, NOT a failure."""
    from sqlalchemy.exc import IntegrityError

    record = AsyncMock(
        side_effect=[
            RuntimeError("commit ack lost"),
            IntegrityError("dup pk", params=None, orig=Exception("duplicate key")),
        ]
    )
    db, ps = _metering_patches(
        lambda request: _fixture_streaming("converse_nonstream_cacheread.bin"), record
    )
    before = _failures("persist_failed")
    for p in ps:
        p.start()
    try:
        with caplog.at_level("INFO"):
            async with await _client() as c:
                resp = await c.post(
                    f"/api/llm-proxy/model/{MODEL}/converse",
                    headers=_metered_bearer(),
                    content=b"{}",
                )
            await _drain()
        assert resp.status_code == 200
        assert record.await_count == 2
        assert "llm_proxy_metering_already_persisted" in caplog.text
        assert _failures("persist_failed") == before  # not a failure
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_finish_stream_teardown_failure_still_persists(caplog) -> None:
    """An already-reset connection makes close_stream raise inside the BackgroundTask —
    the call's usage row must still land (billing can't hinge on teardown hygiene)."""
    record = AsyncMock()
    db, ps = _metering_patches(
        lambda request: _fixture_streaming("converse_nonstream_cacheread.bin"), record
    )
    ps.append(
        patch(
            "app.api.routes.llm_proxy.close_stream",
            AsyncMock(side_effect=RuntimeError("already reset")),
        )
    )
    for p in ps:
        p.start()
    try:
        with caplog.at_level("WARNING"):
            async with await _client() as c:
                resp = await c.post(
                    f"/api/llm-proxy/model/{MODEL}/converse",
                    headers=_metered_bearer(),
                    content=b"{}",
                )
            await _drain()
        assert resp.status_code == 200
        assert "llm_proxy_stream_teardown_failed" in caplog.text
        record.assert_awaited_once()
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_metering_persist_overflow_drops_and_counts(caplog) -> None:
    """The pending-task cap (DB-outage backstop): past it the row is dropped and alarmed,
    never queued unboundedly."""
    record = AsyncMock()
    db, ps = _metering_patches(
        lambda request: _fixture_streaming("converse_nonstream_cacheread.bin"), record
    )
    ps.append(patch("app.api.routes.llm_proxy._MAX_PENDING_PERSISTS", 0))
    before = _failures("persist_failed")
    for p in ps:
        p.start()
    try:
        with caplog.at_level("WARNING"):
            async with await _client() as c:
                resp = await c.post(
                    f"/api/llm-proxy/model/{MODEL}/converse",
                    headers=_metered_bearer(),
                    content=b"{}",
                )
            await _drain()
        assert resp.status_code == 200
        assert "llm_proxy_metering_persist_overflow" in caplog.text
        record.assert_not_awaited()
        assert _failures("persist_failed") == before + 1
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_metering_legacy_grant_defaults(caplog) -> None:
    """A grant minted without engine/turn_id (pre-P1 callers) still meters: provider falls
    back to 'unknown', turn_id stays None."""
    record = AsyncMock()
    db, ps = _metering_patches(lambda request: _fixture_streaming("invoke_nonstream.bin"), record)
    for p in ps:
        p.start()
    try:
        async with await _client() as c:
            resp = await c.post(
                f"/api/llm-proxy/model/{MODEL}/invoke",
                headers=_bearer(),  # legacy mint(_U, _S)
                content=b"{}",
            )
        assert resp.status_code == 200
        await _drain()
        record.assert_awaited_once()
        kwargs = record.await_args.kwargs
        assert kwargs["provider"] == "unknown"
        assert kwargs["turn_id"] is None
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_metering_skips_grant_without_session(caplog) -> None:
    """A grant minted with no session can't be attributed — warning + failure counter,
    never an exception into the response lifecycle."""
    record = AsyncMock()
    db, ps = _metering_patches(lambda request: _fixture_streaming("invoke_nonstream.bin"), record)
    for p in ps:
        p.start()
    try:
        tok = llm_proxy_token.mint(None, None, engine=_ENGINE, turn_id=_TURN)
        before = _failures("no_session")
        with caplog.at_level("WARNING"):
            async with await _client() as c:
                resp = await c.post(
                    f"/api/llm-proxy/model/{MODEL}/invoke",
                    headers={"Authorization": f"Bearer {tok}"},
                    content=b"{}",
                )
        assert resp.status_code == 200
        record.assert_not_awaited()
        assert "llm_proxy_metering_no_session" in caplog.text
        assert _failures("no_session") == before + 1
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_control_plane_gets_are_never_metered() -> None:
    """Only data-plane calls carry a tee: a control-plane GET (non-Bedrock-usage JSON)
    must produce no usage row and no parse warning."""
    record = AsyncMock()
    db, ps = _metering_patches(
        lambda request: _streaming(200, b'{"inferenceProfileSummaries": []}'), record
    )
    for p in ps:
        p.start()
    try:
        async with await _client() as c:
            resp = await c.get(
                "/api/llm-proxy/inference-profiles",
                headers=_metered_bearer(),
            )
        assert resp.status_code == 200
        record.assert_not_awaited()
    finally:
        for p in ps:
            p.stop()
