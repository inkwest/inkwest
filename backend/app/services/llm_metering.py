"""
Passive usage metering for the LLM auth broker (budget accuracy at the source).

The broker (:mod:`app.api.routes.llm_proxy`) forwards Bedrock responses byte-for-byte; this
module observes those same bytes to extract the token counts AWS actually meters — including
the prompt-cache tokens every engine-side accounting path historically dropped (the ~10x
undercount). One :class:`MeteringTee` instance rides one proxied call; the route feeds it each
forwarded chunk and reads :meth:`MeteringTee.result` after the stream ends.

Wire truth per op (confirmed live 2026-07-03/04, real fixtures in ``tests/fixtures/bedrock/``):

- ``invoke-with-response-stream`` — AWS eventstream of ``chunk`` events whose payload is
  ``{"bytes": base64(anthropic event JSON)}``. The final ``message_stop`` inner event carries
  ``amazon-bedrock-invocationMetrics`` = the billed counts (``inputTokenCount``,
  ``outputTokenCount``, ``cacheReadInputTokenCount``/``cacheWriteInputTokenCount`` — the cache
  keys are ABSENT when zero). The ``message_start`` Anthropic ``usage`` block is the model's
  self-report (minor divergence vs metrics observed) and is used ONLY to enrich the cache-write
  TTL split (``cache_creation.ephemeral_5m/1h_input_tokens``), never to override billed totals.
- ``converse-stream`` — eventstream whose terminal ``metadata`` event payload carries ``usage``.
- ``converse`` — JSON body ``usage``: ``inputTokens``/``outputTokens``, cache read/write under
  BOTH spellings (``cacheReadInputTokens`` and legacy ``cacheReadInputTokenCount`` — accept
  either), and ``cacheDetails: [{"ttl": "5m"|"1h", "inputTokens": N}]`` for the TTL split.
  ``totalTokens`` INCLUDES cache tokens — never derive anything from it.
- ``invoke`` — JSON body with an Anthropic ``usage`` block.

Failure contract: metering is passive — any parse problem marks the tee failed and
:meth:`result` returns None (caller logs a warning, no usage row); the forwarded bytes are
never touched. botocore is already a backend dependency (boto3), so
``botocore.eventstream.EventStreamBuffer`` decodes incrementally without buffering the body.
"""

import base64
import json
import logging
from dataclasses import dataclass
from typing import Any

from botocore.eventstream import EventStreamBuffer  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

# Streaming ops carry AWS eventstream framing; the other allowed ops return plain JSON.
STREAMING_OPS = frozenset({"invoke-with-response-stream", "converse-stream"})

# Buffer cap for non-stream JSON bodies awaiting parse. A text response tops out well below
# this; past the cap we drop metering for the call (never the forwarded bytes).
_NONSTREAM_BODY_CAP = 8 * 1024 * 1024


@dataclass(frozen=True)
class CallUsage:
    """Billed token counts of one Bedrock call, 4-way split + the 1h-TTL write portion."""

    input_tokens: int  # fresh (uncached) input
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int  # total writes, all TTLs
    cache_write_1h_tokens: int  # portion of cache_write_tokens at 1h TTL (billed 2x)


def _as_int(value: Any) -> int:
    """Missing/None/garbage token field ⇒ 0 (cache keys are legitimately absent when zero)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _from_converse_usage(usage: dict[str, Any]) -> CallUsage:
    """Converse ``usage`` block (non-stream body and stream ``metadata`` event share it).

    Accepts BOTH cache spellings — the agent path emits a ``…TokenCount`` alias alongside the
    documented ``…Tokens`` names (both observed live).
    """
    read = usage.get("cacheReadInputTokens")
    if read is None:
        read = usage.get("cacheReadInputTokenCount")
    write = usage.get("cacheWriteInputTokens")
    if write is None:
        write = usage.get("cacheWriteInputTokenCount")
    write_total = _as_int(write)
    write_1h = sum(
        _as_int(d.get("inputTokens"))
        for d in usage.get("cacheDetails") or []
        if isinstance(d, dict) and d.get("ttl") == "1h"
    )
    return CallUsage(
        input_tokens=_as_int(usage.get("inputTokens")),
        output_tokens=_as_int(usage.get("outputTokens")),
        cache_read_tokens=_as_int(read),
        cache_write_tokens=write_total,
        cache_write_1h_tokens=min(write_1h, write_total),
    )


def _from_anthropic_usage(usage: dict[str, Any]) -> CallUsage:
    """Anthropic ``usage`` block (invoke non-stream body)."""
    write_total = _as_int(usage.get("cache_creation_input_tokens"))
    cache_creation = usage.get("cache_creation") or {}
    write_1h = _as_int(cache_creation.get("ephemeral_1h_input_tokens"))
    return CallUsage(
        input_tokens=_as_int(usage.get("input_tokens")),
        output_tokens=_as_int(usage.get("output_tokens")),
        cache_read_tokens=_as_int(usage.get("cache_read_input_tokens")),
        cache_write_tokens=write_total,
        cache_write_1h_tokens=min(write_1h, write_total),
    )


class MeteringTee:
    """Observes one proxied Bedrock call's response bytes; yields its :class:`CallUsage`.

    Bounded memory: streaming ops retain only the eventstream decode buffer plus the last
    metrics/usage payloads; non-stream ops buffer the JSON body up to a cap.
    """

    def __init__(self, op: str) -> None:
        self._op = op
        self._streaming = op in STREAMING_OPS
        self._failed = False
        self._buffer = EventStreamBuffer() if self._streaming else None
        self._body = bytearray()
        # invoke-stream: billed truth (invocationMetrics) + TTL-split enrichment (usage).
        self._invocation_metrics: dict[str, Any] | None = None
        self._anthropic_cache_creation: dict[str, Any] | None = None
        # converse-stream: the terminal metadata event's usage block.
        self._converse_usage: dict[str, Any] | None = None

    def feed(self, chunk: bytes) -> None:
        """Observe one forwarded chunk. Never raises — a parse problem fails the tee only."""
        if self._failed:
            return
        try:
            if self._streaming:
                self._feed_eventstream(chunk)
            else:
                if len(self._body) + len(chunk) > _NONSTREAM_BODY_CAP:
                    raise ValueError("non-stream body exceeds metering cap")
                self._body.extend(chunk)
        except Exception:
            self._failed = True
            logger.warning("llm_metering_feed_failed", extra={"op": self._op}, exc_info=True)

    def _feed_eventstream(self, chunk: bytes) -> None:
        assert self._buffer is not None
        self._buffer.add_data(chunk)
        for event in self._buffer:
            payload = json.loads(event.payload)
            if not isinstance(payload, dict):
                continue
            if "bytes" in payload:  # invoke-stream: base64-wrapped Anthropic event
                inner = json.loads(base64.b64decode(payload["bytes"]))
                metrics = inner.get("amazon-bedrock-invocationMetrics")
                if isinstance(metrics, dict):
                    self._invocation_metrics = metrics
                # message_start carries the full usage incl. the cache_creation TTL split;
                # later message_delta usage blocks drop the nested split — keep the first seen.
                usage = (inner.get("message") or {}).get("usage") or inner.get("usage")
                if isinstance(usage, dict) and isinstance(usage.get("cache_creation"), dict):
                    self._anthropic_cache_creation = usage["cache_creation"]
            elif isinstance(payload.get("usage"), dict):  # converse-stream metadata event
                self._converse_usage = payload["usage"]

    def result(self) -> CallUsage | None:
        """The call's billed usage, or None when parsing failed / no usage frame was seen
        (e.g. client disconnect before the terminal frame — nothing reliable to record)."""
        if self._failed:
            return None
        try:
            if self._op == "invoke-with-response-stream":
                return self._invoke_stream_result()
            if self._op == "converse-stream":
                return _from_converse_usage(self._converse_usage) if self._converse_usage else None
            return self._nonstream_result()
        except Exception:
            logger.warning("llm_metering_result_failed", extra={"op": self._op}, exc_info=True)
            return None

    def _invoke_stream_result(self) -> CallUsage | None:
        metrics = self._invocation_metrics
        if metrics is None:
            return None
        # invocationMetrics is what AWS bills — cache keys ABSENT when zero. The Anthropic
        # usage block only contributes the 1h-TTL write portion (metrics have no TTL split).
        write_total = _as_int(metrics.get("cacheWriteInputTokenCount"))
        write_1h = _as_int((self._anthropic_cache_creation or {}).get("ephemeral_1h_input_tokens"))
        return CallUsage(
            input_tokens=_as_int(metrics.get("inputTokenCount")),
            output_tokens=_as_int(metrics.get("outputTokenCount")),
            cache_read_tokens=_as_int(metrics.get("cacheReadInputTokenCount")),
            cache_write_tokens=write_total,
            cache_write_1h_tokens=min(write_1h, write_total),
        )

    def _nonstream_result(self) -> CallUsage | None:
        if not self._body:
            return None
        body = json.loads(bytes(self._body))
        usage = body.get("usage") if isinstance(body, dict) else None
        if not isinstance(usage, dict):
            return None
        if self._op == "converse":
            return _from_converse_usage(usage)
        return _from_anthropic_usage(usage)
