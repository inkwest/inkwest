"""
Shared reverse-proxy primitives for the MCP and LLM brokers.

Both ``mcp_proxy`` and ``llm_proxy`` are squid-fronted, bearer-swapping streaming reverse
proxies with the same teardown discipline: the upstream client + response outlive the request
handler and are closed by a ``BackgroundTask`` once the body is consumed (or the client
disconnects). Keeping the body cap, hop-by-hop strip, bearer extraction, stream teardown, and
timeout here means a framing/leak fix lands in BOTH proxies at once — the two routes drifting
apart was the original bug class this module removes.

Each proxy still owns its auth semantics (which token store ``verify``s, which request headers
forward) — those differ and stay in the route module.
"""

import httpx
from fastapi import HTTPException, Request

# Connect/write/pool bounded. ``read`` is an *idle* (inter-byte) ceiling, NOT a total deadline:
# a streaming upstream (SSE / Bedrock eventstream) emits events with gaps, so a generous read
# timeout keeps a slow-but-live stream alive while a genuinely hung upstream still releases the
# client + FD instead of pinning them forever (an unbounded read would never let go at 3 AM).
# 1 h matches the squid read_timeout. A *total* wall-clock cap is layered on top per route.
TIMEOUT = httpx.Timeout(connect=5.0, write=30.0, read=3600.0, pool=5.0)

# Hop-by-hop headers (RFC 7230 §6.1) plus framing headers httpx/Starlette must own. A proxy
# that swaps the credential adds ``authorization`` to its own strip set on top of this.
HOP_BY_HOP = frozenset(
    {
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
    }
)


def split_bearer(request: Request) -> str | None:
    """The bearer token from the Authorization header, or None when absent/malformed.

    Returns None rather than raising so each proxy can raise its OWN ``ProxyAuthError`` (the two
    token stores define distinct exception classes)."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    return header[7:]


async def read_capped_body(request: Request, max_bytes: int) -> bytes:
    """Buffer the request body, rejecting bodies over ``max_bytes`` with 413.

    Buffered (not streamed) so the forwarded request carries a Content-Length and can be
    replayed if the proxy retries. The cap bounds single-worker memory under concurrency. Reject
    on the declared Content-Length when present, and guard again while streaming for chunked
    requests that omit it."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise HTTPException(status_code=413, detail="Request body too large")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail="Request body too large")
        chunks.append(chunk)
    return b"".join(chunks)


def filter_headers(headers: httpx.Headers, strip: frozenset[str] | set[str]) -> dict[str, str]:
    """Copy headers, dropping any whose lower-cased name is in ``strip``."""
    return {k: v for k, v in headers.items() if k.lower() not in strip}


async def close_stream(upstream: httpx.Response, client: httpx.AsyncClient) -> None:
    """Release the upstream stream + its client once the body is done (or the client disconnects
    mid-stream — Starlette runs the background task either way).

    Each close is independent: if closing the stream raises (an already-reset connection), the
    client must still be closed, else its connection pool leaks."""
    try:
        await upstream.aclose()
    finally:
        await client.aclose()
