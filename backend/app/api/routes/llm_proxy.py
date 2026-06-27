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
``get_current_user`` — the sandbox caller carries no session. In scope: the sandboxed
``claude-cli`` deployment only; the non-sandboxed ``bedrock`` provider holds creds in-process
and never crosses a sandbox boundary (see docs/llm-auth-broker.md §Scope).
"""

import logging
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from app.core.config import get_settings
from app.core.constants import LLM_PROXY_MAX_BODY_BYTES
from app.services import model_registry
from app.services.llm_proxy_token import LlmProxyClaims, ProxyAuthError, verify

logger = logging.getLogger(__name__)

router = APIRouter(tags=["llm-proxy"])

# Injectable for tests (httpx.MockTransport). read is an *idle* (inter-byte) ceiling, not a
# total deadline: a Bedrock streaming response (converse-stream / invoke-with-response-stream)
# emits events with gaps, so a generous read timeout keeps a slow-but-live stream alive while
# a genuinely hung upstream still releases the client + FD instead of pinning them forever.
_transport: httpx.AsyncBaseTransport | None = None
_TIMEOUT = httpx.Timeout(connect=5.0, write=30.0, read=3600.0, pool=5.0)

# Bedrock runtime operations the broker permits (the trailing path segment). converse /
# converse-stream (Converse API, used by orchestrator_converse + the inkwest-agent) and
# invoke / invoke-with-response-stream (InvokeModel, used by the Claude CLI on Bedrock).
_ALLOWED_OPS = frozenset({"converse", "converse-stream", "invoke", "invoke-with-response-stream"})

# Bedrock CONTROL-plane resources the broker proxies (read-only GET allowlist, first path
# segment). Claude Code on Bedrock lists these to resolve a model to its regional inference
# profile (in regions like eu-west-1 the bare foundation-model id isn't invocable on-demand);
# it points those GETs at ANTHROPIC_BEDROCK_BASE_URL too, so the broker must serve them — to
# the `bedrock.<region>` control plane (distinct host from `bedrock-runtime.`).
_CONTROL_PLANE_GETS = frozenset({"inference-profiles", "foundation-models"})

# Hop-by-hop headers (RFC 7230 §6.1) + framing/auth headers httpx/Starlette must own. Unlike
# the MCP proxy we forward every OTHER client header verbatim (the upstream is AWS Bedrock, our
# own constructed host — not a third-party server), so Bedrock-specific headers
# (x-amzn-bedrock-*, accept: application/vnd.amazon.eventstream, …) pass through untouched.
_STRIP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "authorization",  # swapped for the real backend bearer
}

_NOT_CONFIGURED = "LLM broker not configured"


# --- Upstream dispatch seam -------------------------------------------------------------
# Today: bedrock-bearer (JWT → real Bedrock API key, no SigV4). Structured as a dispatch so a
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
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise ProxyAuthError("missing bearer token")
    return header[7:]


def _allowed_targets() -> set[str]:
    """Invoke targets the broker permits: enabled registry model IDs ∪ their ARNs.

    Mirrors ``model_registry.selectable_models`` semantics (no enabled rows ⇒ the whole
    catalog, the fresh-deploy default-open case). Including each entry's ARN covers the
    provisioned/inference-profile path, where the URL carries the ARN, not the friendly ID.
    """
    entries = model_registry.merged_entries()
    enabled = [e for e in entries if e.enabled]
    chosen = enabled or entries
    allowed: set[str] = set()
    for e in chosen:
        allowed.add(e.model_id)
        if e.arn:
            allowed.add(e.arn)
    return allowed


async def _read_capped_body(request: Request) -> bytes:
    """Buffer the request body, rejecting bodies over the cap with 413.

    Buffered (not streamed) so the forwarded request carries a Content-Length — AWS Bedrock
    is stricter about framing than a generic MCP server, and there is no retry here that would
    need a replay. The cap bounds single-worker memory under concurrency (a vision Converse
    turn is large but finite)."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > LLM_PROXY_MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Request body too large")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > LLM_PROXY_MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Request body too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _request_headers(request: Request, auth: dict[str, str]) -> dict[str, str]:
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _STRIP}
    headers.update(auth)
    return headers


def _response_headers(upstream: httpx.Response) -> dict[str, str]:
    return {k: v for k, v in upstream.headers.items() if k.lower() not in _STRIP}


@router.api_route("/llm-proxy/model/{model_path:path}", methods=["POST"])
async def llm_proxy(model_path: str, request: Request) -> StreamingResponse:
    # Split the opaque "<model-or-arn>/<op>" tail: op is the LAST segment, the target is
    # everything before it (an ARN keeps its embedded slashes). Reject a malformed shape.
    target, sep, op = model_path.rpartition("/")
    if not sep or not target or op not in _ALLOWED_OPS:
        raise HTTPException(status_code=400, detail="Invalid LLM proxy path")

    # Auth gate first: an unauthenticated caller learns nothing about model/op validity.
    try:
        claims: LlmProxyClaims = verify(_bearer(request))
    except ProxyAuthError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e

    # Restrict: only enabled registry models (or their ARNs) may be invoked through the broker.
    if target not in _allowed_targets():
        raise HTTPException(status_code=403, detail="Model not permitted")

    bearer, region = _backend_creds()
    body = await _read_capped_body(request)
    url, auth = _UPSTREAMS[_PROVIDER](target, op, request.url.query, region, bearer)
    return await _send(
        request, method="POST", url=url, auth=auth, body=body, session_id=claims.session_id
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

    # Allowlist the first path segment. Checked AFTER auth so an unauthenticated caller learns
    # nothing about which resources are proxied. 404 (looks like an unknown endpoint), not 403.
    if cp_path.split("/", 1)[0] not in _CONTROL_PLANE_GETS:
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


async def _send(
    request: Request,
    *,
    method: str,
    url: str,
    auth: dict[str, str],
    body: bytes | None,
    session_id: str | None,
) -> StreamingResponse:
    """Forward one request to Bedrock and stream the response back untouched.

    Single teardown contract (mirrors mcp_proxy): on success the client + stream outlive this
    handler and are closed by ``_close_stream`` once the body is consumed (or the client
    disconnects); on any failure before that, close the client here."""
    client = httpx.AsyncClient(transport=_transport, timeout=_TIMEOUT)
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
        if upstream is None:
            await client.aclose()

    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers=_response_headers(upstream),
        background=BackgroundTask(_close_stream, upstream, client),
    )


async def _close_stream(upstream: httpx.Response, client: httpx.AsyncClient) -> None:
    """Release the upstream stream + its client once the response body is done (or the client
    disconnects mid-stream — Starlette runs the background task either way)."""
    await upstream.aclose()
    await client.aclose()
