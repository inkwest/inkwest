"""
LLM auth broker (port of the OAuth2 MCP reverse proxy to the model call itself).

The sandboxed engines (Claude CLI, inkwest-agent) reach Bedrock ONLY through squid, and now
carry a short-lived, opaque per-turn grant instead of the real Bedrock API key. They POST to
``/api/llm-proxy/model/{id}/{op}`` — the exact path shape both the Claude CLI
(``ANTHROPIC_BEDROCK_BASE_URL``) and boto3 (``endpoint_url``) construct. This route verifies
the grant, RESTRICTS the call (``model_id`` ∈ enabled registry models/ARNs, ``op`` ∈ allowed
Bedrock operations), swaps the ``Authorization`` header for the backend-held real bearer, and
forwards to real Bedrock — streaming the response back untouched. The real credential lives
only in the backend process; a compromised sandbox holds only a model-scoped grant that the
orchestrator BURNS when the turn ends (see :mod:`app.services.llm_proxy_token`).

With ``AWS_BEARER_TOKEN_BEDROCK`` both the CLI and boto3 send ``Authorization: Bearer …`` (no
SigV4), so the broker is a pure header swap + transparent byte passthrough — no re-signing.

Two legs: the data plane (``POST /model/{id}/{op}`` → ``bedrock-runtime.<region>``, model/op
restricted) and a read-only control plane (``GET`` on ``_CONTROL_PLANE_GETS`` → ``bedrock.
<region>``) that Claude Code uses to resolve a model to its regional inference profile — both
point at ``ANTHROPIC_BEDROCK_BASE_URL``, so the broker serves both or model resolution breaks.

Auth on this route IS the internal grant (:mod:`app.services.llm_proxy_token`); there is no
``get_current_user`` — the sandbox caller carries no session. The grant is the ONLY
application-level credential, so the route MUST NOT be exposed to untrusted networks: it is
reachable from the sandbox solely via the host-gateway alias (the same squid-allowlisted
``mcp_proxy_base_url`` host), and the deployment must keep ``/api/llm-proxy`` off any
public ingress / restrict it to the host-gateway interface — a leaked grant alone should not be
usable off-host for its lifetime. In scope: the sandboxed ``claude-cli`` deployment only; the
non-sandboxed ``bedrock`` provider holds creds in-process and never crosses a sandbox boundary
(see docs/llm-auth-broker.md §Scope).
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from urllib.parse import quote
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from app.api.routes._proxy_stream import (
    HOP_BY_HOP,
    TIMEOUT,
    close_stream,
    filter_headers,
    read_capped_body,
    split_bearer,
)
from app.core import metrics
from app.core.config import get_settings
from app.core.constants import LLM_PROXY_MAX_BODY_BYTES, LLM_PROXY_STREAM_MAX_SECONDS
from app.core.database import async_session_maker
from app.services import model_catalog, model_registry
from app.services.budget_service import record_usage
from app.services.llm_metering import STREAMING_OPS, MeteringTee
from app.services.llm_proxy_token import LlmProxyClaims, ProxyAuthError, verify

logger = logging.getLogger(__name__)

router = APIRouter(tags=["llm-proxy"])

# Injectable for tests (httpx.MockTransport). Timeout shared with the MCP proxy; see _proxy_stream.
_transport: httpx.AsyncBaseTransport | None = None

# Bedrock runtime operations the broker permits (the trailing path segment). converse /
# converse-stream (Converse API, used by the inkwest-agent) and
# invoke / invoke-with-response-stream (InvokeModel, used by the Claude CLI on Bedrock).
# Composed from the metering module's STREAMING_OPS so the tee's stream/JSON detection
# can never silently desync from the ops the route accepts.
_ALLOWED_OPS = frozenset({"converse", "invoke"}) | STREAMING_OPS

# Bedrock CONTROL-plane resources the broker proxies (read-only GET allowlist, first path
# segment). Claude Code on Bedrock lists these to resolve a model to its regional inference
# profile (in regions like eu-west-1 the bare foundation-model id isn't invocable on-demand);
# it points those GETs at ANTHROPIC_BEDROCK_BASE_URL too, so the broker must serve them — to
# the `bedrock.<region>` control plane (distinct host from `bedrock-runtime.`).
_CONTROL_PLANE_GETS = frozenset({"inference-profiles", "foundation-models"})

# Request headers the broker drops before forwarding. Hop-by-hop (framing) + ``authorization``
# (swapped for the real backend bearer). Unlike the MCP proxy we forward every OTHER client
# header verbatim — the upstream is AWS Bedrock, our own constructed host — so Bedrock-specific
# headers (x-amzn-bedrock-*, accept: application/vnd.amazon.eventstream, …) pass through. But
# any ``x-amz-*`` SigV4/credential/identity header is stripped too (see _AMZ_CRED_PREFIX): the
# broker owns the upstream credential, so a compromised sandbox must not inject AWS signing or
# session-token headers onto the broker's authenticated call. ``x-amzn-bedrock-*`` is a distinct
# prefix and is preserved.
_REQ_STRIP = HOP_BY_HOP | {"authorization"}
_AMZ_CRED_PREFIX = "x-amz-"
# Response headers the broker drops on the way back: hop-by-hop framing only (the upstream sets
# no auth header). A separate set from _REQ_STRIP so request-vs-response intent stays explicit.
_RESP_STRIP = HOP_BY_HOP

_NOT_CONFIGURED = "LLM broker not configured"


# --- Upstream dispatch seam -------------------------------------------------------------
# Today: bedrock-bearer (grant → real Bedrock API key, no SigV4). Structured as a dispatch so a
# Mantle (Messages-API) or direct-Anthropic-key upstream can be added as a sibling later
# without touching the route body — each just builds its own (url, auth-headers) pair.
def _bedrock_bearer_upstream(
    target: str, op: str, query: str, region: str, bearer: str
) -> tuple[str, dict[str, str]]:
    # Re-encode the target as a single path segment: boto3/the CLI percent-encode the modelId
    # (ARNs carry ':' and '/'), Starlette decoded it into `target`, so AWS expects it encoded
    # again. safe='' encodes ':' and '/' so a provisioned/imported-model ARN round-trips.
    url = f"https://bedrock-runtime.{region}.amazonaws.com/model/{quote(target, safe='')}/{op}"
    if query:
        url = f"{url}?{query}"
    return url, {"Authorization": f"Bearer {bearer}"}


_PROVIDER = "bedrock-bearer"
_UPSTREAMS = {"bedrock-bearer": _bedrock_bearer_upstream}


def _bearer(request: Request) -> str:
    token = split_bearer(request)
    if token is None:
        raise ProxyAuthError("missing bearer token")
    return token


def _allowed_targets() -> set[str]:
    """Invoke targets the broker permits: enabled registry model IDs ∪ their ARNs ∪ profile ids.

    FAIL-CLOSED: if no registry row is enabled the broker permits NOTHING (empty set ⇒ every
    invoke 403s). The model picker's ``selectable_models`` is default-OPEN on a fresh deploy
    (offer everything), but the broker is a credential gate — defaulting open there would let any
    grant invoke any discovered model on an unconfigured backend. An operator must enable the
    models the broker may reach. Each entry contributes its friendly ID, its invoke ARN (the
    provisioned/inference-profile path, where the URL carries the ARN), and any regional
    inference-profile id that resolves to that ARN — Claude Code auto-upgrades a bare foundation
    id to its ``eu.``/``us.`` profile id before invoking, and that id must also pass.
    """
    enabled = [e for e in model_registry.merged_entries() if e.enabled]
    allowed: set[str] = set()
    for e in enabled:
        allowed.add(e.model_id)
        if e.arn:
            allowed.add(e.arn)
            allowed.update(model_catalog.profile_ids_for_arn(e.arn))
    return allowed


def _request_headers(request: Request, auth: dict[str, str]) -> dict[str, str]:
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _REQ_STRIP and not k.lower().startswith(_AMZ_CRED_PREFIX)
    }
    headers.update(auth)
    return headers


@router.api_route("/llm-proxy/model/{model_path:path}", methods=["POST"])
async def llm_proxy(model_path: str, request: Request) -> StreamingResponse:
    # Split the opaque "<model-or-arn>/<op>" tail: op is the LAST segment, the target is
    # everything before it (an ARN keeps its embedded slashes). Reject a malformed shape /
    # disallowed op up front (400) — this is a static path-shape check, not a secret: it leaks
    # only which Bedrock ops the broker proxies, which is fixed and non-sensitive. Model/ARN
    # validity (the part worth hiding) IS gated behind auth below.
    target, sep, op = model_path.rpartition("/")
    if not sep or not target or op not in _ALLOWED_OPS:
        raise HTTPException(status_code=400, detail="Invalid LLM proxy path")

    # Auth gate before any model-validity signal: an unauthenticated caller can't tell an
    # enabled model from a disabled/unknown one (both never get past here).
    try:
        claims: LlmProxyClaims = verify(_bearer(request))
    except ProxyAuthError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e

    # Restrict: only enabled registry models (or their ARNs/profile ids) may be invoked.
    if target not in _allowed_targets():
        raise HTTPException(status_code=403, detail="Model not permitted")

    bearer, region = _backend_creds()
    body = await read_capped_body(request, LLM_PROXY_MAX_BODY_BYTES)
    url, auth = _UPSTREAMS[_PROVIDER](target, op, request.url.query, region, bearer)
    return await _send(
        request,
        method="POST",
        url=url,
        auth=auth,
        body=body,
        session_id=claims.session_id,
        meter=MeteringTee(op),
        claims=claims,
        target=target,
    )


@router.api_route("/llm-proxy/{cp_path:path}", methods=["GET"])
async def llm_proxy_control_plane(cp_path: str, request: Request) -> StreamingResponse:
    """Bedrock CONTROL-plane reads — a read-only GET allowlist (see ``_CONTROL_PLANE_GETS``).

    Claude Code resolves models via control-plane listing (ListInferenceProfiles / list models)
    and points those GETs at ``ANTHROPIC_BEDROCK_BASE_URL`` too. The data-plane route only serves
    ``/model/{id}/{op}``, so without this the discovery GET 404s and the CLI falls back to the
    bare model id — which Bedrock then rejects in regions that require an inference profile.
    Forwarded to the ``bedrock.<region>`` control plane (distinct host from ``bedrock-runtime.``;
    both already squid-allowlisted)."""
    try:
        claims: LlmProxyClaims = verify(_bearer(request))
    except ProxyAuthError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e

    # Reject dot-segments BEFORE the allowlist check: ``quote(safe='/')`` below preserves ``..``
    # verbatim, so without this a path like ``inference-profiles/../<other-op>`` would pass the
    # first-segment allowlist yet normalize to an arbitrary GET on the control-plane host. Mirror
    # the mcp_proxy dot-segment guard so the allowlist can't be traversed out of.
    segments = [seg for seg in cp_path.split("/") if seg]
    if any(seg in ("..", ".") for seg in segments):
        raise HTTPException(status_code=400, detail="Invalid LLM proxy path")

    # Allowlist the first path segment. Checked AFTER auth so an unauthenticated caller learns
    # nothing about which resources are proxied. 404 (looks like an unknown endpoint), not 403.
    if not segments or segments[0] not in _CONTROL_PLANE_GETS:
        raise HTTPException(status_code=404, detail="Unknown LLM proxy resource")

    bearer, region = _backend_creds()
    # cp_path is a resource path that may carry sub-segments (e.g. inference-profiles/<id>); keep
    # its slashes, encode the rest. No body on a control-plane GET.
    url = f"https://bedrock.{region}.amazonaws.com/{quote(cp_path, safe='/')}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    return await _send(
        request,
        method="GET",
        url=url,
        auth={"Authorization": f"Bearer {bearer}"},
        body=None,
        session_id=claims.session_id,
    )


def _backend_creds() -> tuple[str, str]:
    """The backend's real Bedrock bearer + region, or 503 when the broker isn't provisioned.

    503 (transient/misconfig), never leaking which of the two is missing.
    """
    settings = get_settings()
    if not settings.aws_bearer_token_bedrock or not settings.aws_region:
        raise HTTPException(status_code=503, detail=_NOT_CONFIGURED)
    return settings.aws_bearer_token_bedrock, settings.aws_region


async def _bounded_aiter(upstream: httpx.Response) -> AsyncIterator[bytes]:
    """Stream the upstream body with a TOTAL wall-clock cap.

    ``read`` in the httpx timeout is only an inter-byte idle ceiling, so a malicious/buggy
    upstream that dribbles one byte just under that ceiling could pin a worker slot for an
    unbounded wall-clock time (slowloris). This caps the whole response to a generous ceiling
    (longer than any real turn) so a single replica can't be held hostage. On expiry the
    BackgroundTask still runs, closing the client + stream."""
    try:
        async with asyncio.timeout(LLM_PROXY_STREAM_MAX_SECONDS):
            async for chunk in upstream.aiter_raw():
                yield chunk
    except TimeoutError:
        logger.warning("llm_proxy_stream_deadline_exceeded")


async def _metered_aiter(
    upstream: httpx.Response, meter: MeteringTee | None
) -> AsyncIterator[bytes]:
    """Passive tee over :func:`_bounded_aiter`: every chunk is observed, then yielded
    unchanged. ``MeteringTee.feed`` never raises (it fails itself), so metering can neither
    corrupt nor stall the forwarded stream."""
    async for chunk in _bounded_aiter(upstream):
        if meter is not None:
            meter.feed(chunk)
        yield chunk


async def _finish_stream(
    upstream: httpx.Response,
    client: httpx.AsyncClient,
    meter: MeteringTee | None,
    claims: LlmProxyClaims | None,
    target: str | None,
) -> None:
    """Post-stream BackgroundTask: tear down the upstream, then persist the metered usage.

    Runs after the response body was fully sent (or the client vanished) — decoupled from the
    orchestrator's control flow, so a call completed before an engine crash is still billed
    (AWS bills it too). Persistence is best-effort: any failure logs a warning and never
    propagates into the response lifecycle."""
    await close_stream(upstream, client)
    if meter is None or claims is None or target is None:
        return
    usage = meter.result()
    if usage is None:
        # Parse failure or no terminal usage frame (e.g. disconnect mid-stream). The bytes
        # were forwarded fine; we just can't account this call.
        logger.warning(
            "llm_proxy_metering_no_usage",
            extra={"target": target, "session_id": claims.session_id},
        )
        return
    if not claims.session_id:
        logger.warning("llm_proxy_metering_no_session", extra={"target": target})
        return
    try:
        # Label the sink's Prometheus counters with the real engine: this background task
        # runs outside the orchestrator's turn context, so turn_engine() would be "unknown".
        metrics.set_turn_engine(claims.engine or "unknown")
        model = model_registry.resolve_invoke_target(target)
        # The route has no request-scoped DB session — open a fresh one for the write.
        async with async_session_maker() as db:
            await record_usage(
                db,
                user_id=UUID(claims.user_id) if claims.user_id else None,
                session_id=UUID(claims.session_id),
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                model=model,
                provider=claims.engine or "unknown",
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                cache_write_1h_tokens=usage.cache_write_1h_tokens,
                source="broker",
                turn_id=UUID(claims.turn_id) if claims.turn_id else None,
            )
            await db.commit()
    except Exception:
        logger.warning(
            "llm_proxy_metering_persist_failed",
            extra={"target": target, "session_id": claims.session_id},
            exc_info=True,
        )


async def _send(
    request: Request,
    *,
    method: str,
    url: str,
    auth: dict[str, str],
    body: bytes | None,
    session_id: str | None,
    meter: MeteringTee | None = None,
    claims: LlmProxyClaims | None = None,
    target: str | None = None,
) -> StreamingResponse:
    """Forward one request to Bedrock and stream the response back untouched.

    Single teardown contract (mirrors mcp_proxy): on success the client + stream outlive this
    handler and are closed by ``close_stream`` once the body is consumed (or the client
    disconnects); on any failure OR cancellation before the StreamingResponse is handed off,
    close the client here. Data-plane calls additionally carry a :class:`MeteringTee` that
    observes the forwarded bytes and persists a usage row post-stream (HTTP 200 only — an errored
    Bedrock call has no billable tokens)."""
    client = httpx.AsyncClient(transport=_transport, timeout=TIMEOUT)
    upstream: httpx.Response | None = None
    try:
        req = client.build_request(
            method, url, headers=_request_headers(request, auth), content=body
        )
        upstream = await client.send(req, stream=True)
    except httpx.HTTPError:
        logger.exception("llm_proxy_upstream_error", extra={"session_id": session_id})
        raise HTTPException(status_code=502, detail="Bedrock upstream error") from None
    finally:
        # Close the client on every pre-handoff exit: a 502-raise, or a CancelledError (client
        # disconnect / shutdown) during send(). Both leave ``upstream`` None — once send()
        # returns there is no await before the StreamingResponse hands ownership to close_stream,
        # so ``upstream is None`` is the complete "not yet handed off" condition.
        if upstream is None:
            await client.aclose()

    # Unreachable with upstream None: send() failure raised above. Asserted for the type checker.
    assert upstream is not None
    if upstream.status_code != 200:
        meter = None  # non-2xx carries no invocationMetrics/usage — nothing billable
    return StreamingResponse(
        _metered_aiter(upstream, meter),
        status_code=upstream.status_code,
        headers=filter_headers(upstream.headers, _RESP_STRIP),
        background=BackgroundTask(_finish_stream, upstream, client, meter, claims, target),
    )
