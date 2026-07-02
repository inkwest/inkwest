"""
In-sandbox MCP client for the standalone agent (P1.7).

The Converse loop has no native MCP system (claude-cli reads ``.claude.json`` and
runs MCP itself), so this module connects the conversation's enabled MCP servers,
lists their tools, **merges** them into the Converse toolConfig, and routes tool
calls back to the owning server — translating MCP tool schemas ↔ Converse toolSpecs
and MCP results → tool_result text at the boundary only (the loop stays canonical).

Dependency posture (deliberate): the python ``mcp`` SDK is an **optional** dep. The
toolbox image bakes it in (``[project.optional-dependencies].mcp``), but a build
without it must NOT break the loop — every SDK import is lazy and guarded, and a
missing SDK degrades to *no MCP tools merged* (logged once). A malformed config or a
single unreachable server is likewise isolated: it's skipped, the turn still runs.

Server defs are ``.claude.json``-shaped (the catalog's native format, materialized
by the broker into a sandbox file, creds/headers included — projected one-way like
connector creds, never on argv). Supported transports: stdio, SSE, streamable HTTP.

Tool names are namespaced ``mcp__<server>__<tool>`` to avoid collisions across servers
and with the base tools. The ``mcp__`` prefix mirrors Claude Code's MCP tool naming so
replayed history stays coherent across engines (both engines persist into the same
messages table). A name that still collides (duplicate upstream tool, or a clash with a
base tool) is skipped — Converse rejects a duplicate tool name and would fail the turn.
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import Any

from app.tools.definitions import get_tool_definitions

from .converse import fit_tool_name

logger = logging.getLogger(__name__)

# Prefix + separator for advertised MCP tool names: ``mcp__<server>__<tool>`` (Claude
# Code parity — keeps cross-engine replayed history coherent).
_NS_PREFIX = "mcp"
_NS_SEP = "__"
# Per-server bound so one stalled server can't pin the whole turn (the SDK call runs
# inside the loop's async flow; nothing else can cancel it).
_CONNECT_TIMEOUT = 20.0
_CALL_TIMEOUT = 120.0


def _advertised_name(server_key: str, tool_name: str) -> str:
    """The cross-engine MCP tool name: ``mcp__<server>__<tool>`` (≤64 chars for Converse).

    Folded through ``converse.fit_tool_name`` so a long upstream server/tool pair we don't
    control can't exceed Bedrock Converse's 64-char ``ToolName`` limit. The fold is on the
    full string, so it matches what ``to_converse_message`` produces when the same name is
    replayed from history — advertised toolSpec and history toolUse names stay identical.
    """
    return fit_tool_name(f"{_NS_PREFIX}{_NS_SEP}{server_key}{_NS_SEP}{tool_name}")


def mcp_sdk_available() -> bool:
    """Whether the optional ``mcp`` SDK is importable (gates the whole feature)."""
    import importlib.util

    return importlib.util.find_spec("mcp") is not None


def load_mcp_config(path: str | None) -> dict[str, dict[str, Any]]:
    """Read the broker-materialized MCP server config (``{name: server_def}``).

    Tolerant: a missing/malformed file yields no servers rather than raising — a
    broken config must not break the turn (mirrors the broker's catalog parsing).
    """
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("mcp_config_unreadable", extra={"path": path, "error": str(e)})
        return {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return {}
    return {k: v for k, v in servers.items() if isinstance(v, dict)}


def _mcp_schema_to_converse(name: str, description: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Wrap one upstream MCP tool as a Converse toolSpec.

    Converse requires an object inputSchema with ``properties``; some MCP tools omit
    it (no args), so normalize to a valid empty-object schema. Builds a NEW dict — never
    mutates the upstream tool's schema in place (the SDK may reuse that object).
    """
    if not isinstance(schema, dict) or schema.get("type") != "object":
        normalized = {"type": "object", "properties": {}}
    else:
        normalized = {**schema, "properties": schema.get("properties", {})}
    return {
        "toolSpec": {
            "name": name,
            "description": description or f"MCP tool {name}",
            "inputSchema": {"json": normalized},
        }
    }


def _result_to_text(result: Any) -> tuple[str, bool]:
    """Flatten an MCP ``CallToolResult`` into (text, is_error) for the tool_result.

    Read via getattr/duck-typing so this needs no ``mcp`` import (and stays testable
    with a plain stub). Text content blocks are concatenated; other block kinds are
    summarized so binary payloads don't get force-decoded into the transcript.
    """
    is_error = bool(getattr(result, "isError", False))
    parts: list[str] = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(str(text))
        else:
            parts.append(f"[{getattr(block, 'type', 'non-text')} content]")
    return ("\n".join(parts) if parts else "(no output)", is_error)


class MCPManager:
    """Connects the enabled MCP servers for one turn and routes their tool calls.

    Lifecycle is bound to the turn: ``open()`` connects, ``aclose()`` (or the async
    context manager) tears every connection down. All ``mcp`` imports are inside
    methods so importing this module never requires the SDK.
    """

    def __init__(self, servers: dict[str, dict[str, Any]]) -> None:
        self._servers = servers
        # advertised tool name -> (server key, upstream tool name)
        self._routes: dict[str, tuple[str, str]] = {}
        # server key -> live ClientSession
        self._sessions: dict[str, Any] = {}
        self._converse_tools: list[dict[str, Any]] = []
        # Per-server AsyncExitStack: with CONCURRENT connect we can't share one stack
        # across tasks — anyio raises "cancel scope in a different task" on teardown when
        # a transport entered in task A is exited in task B. One stack per server keeps
        # enter/exit on the same task. Eviction (timeout/error) closes just that stack.
        self._stacks: dict[str, contextlib.AsyncExitStack] = {}

    @property
    def converse_tools(self) -> list[dict[str, Any]]:
        """The merged MCP tools as Converse toolSpecs (empty until ``open``)."""
        return self._converse_tools

    def owns(self, advertised_name: str) -> bool:
        """Whether a tool call should route to an MCP server."""
        return advertised_name in self._routes

    async def __aenter__(self) -> MCPManager:
        await self.open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def open(self) -> None:
        """Connect every configured server CONCURRENTLY and collect their tools.

        Concurrent so the connect budget is max-not-sum (one slow server doesn't add to
        the others' latency). A server that fails to connect/init/list is skipped with a
        warning; the rest still work. Does nothing (and stays empty) if the SDK is absent.
        """
        if not self._servers:
            return
        if not mcp_sdk_available():
            logger.warning(
                "mcp_sdk_missing",
                extra={"servers": sorted(self._servers), "hint": "install the 'mcp' extra"},
            )
            return
        import asyncio

        results = await asyncio.gather(
            *(self._connect_one(k, d) for k, d in self._servers.items()),
            return_exceptions=True,
        )
        for key, result in zip(self._servers, results, strict=True):
            if isinstance(result, Exception):
                logger.warning(
                    "mcp_server_connect_failed", extra={"server": key, "error": str(result)}
                )

    async def _connect_one(self, key: str, server_def: dict[str, Any]) -> None:
        import asyncio

        from mcp import ClientSession

        # Per-server stack so a concurrent connect can be torn down on the SAME task that
        # entered it (anyio cancel-scope safety) and evicted independently.
        stack = contextlib.AsyncExitStack()
        self._stacks[key] = stack
        try:
            transport = await asyncio.wait_for(
                self._open_transport(stack, server_def), timeout=_CONNECT_TIMEOUT
            )
            read, write = transport[0], transport[1]
            session = await stack.enter_async_context(ClientSession(read, write))
            await asyncio.wait_for(session.initialize(), timeout=_CONNECT_TIMEOUT)
            listed = await asyncio.wait_for(session.list_tools(), timeout=_CONNECT_TIMEOUT)
        except BaseException:
            # Roll back this server's transport on failure so a half-open connection
            # doesn't linger; eviction below also drops its routes (there are none yet).
            await self._evict(key)
            raise
        self._sessions[key] = session
        count = 0
        for tool in listed.tools:
            advertised = _advertised_name(key, tool.name)
            # Dedupe on insert (mirror session_skills' slug-collision skip): Converse
            # rejects a duplicate tool name and would fail the whole turn, so skip a name
            # that already routes or clashes with a base tool rather than last-wins.
            if advertised in self._routes or advertised in self._base_tool_names():
                logger.warning(
                    "mcp_tool_name_collision",
                    extra={"server": key, "tool": tool.name, "advertised": advertised},
                )
                continue
            self._routes[advertised] = (key, tool.name)
            schema = tool.inputSchema if isinstance(tool.inputSchema, dict) else {}
            self._converse_tools.append(
                _mcp_schema_to_converse(advertised, tool.description or "", schema)
            )
            count += 1
        logger.info("mcp_server_connected", extra={"server": key, "tools": count})

    @staticmethod
    def _base_tool_names() -> set[str]:
        """Names of the base (kubectl/connector) tools — MCP must not shadow these."""
        return {t["name"] for t in get_tool_definitions()}

    async def _open_transport(
        self, stack: contextlib.AsyncExitStack, server_def: dict[str, Any]
    ) -> Any:
        """Open the right MCP transport for a ``.claude.json``-shaped server def.

        Returns the transport's ``(read, write[, ...])`` tuple; only the first two are
        used. stdio when a ``command`` is given; otherwise SSE/HTTP keyed on ``type``
        (defaulting to streamable HTTP for a bare ``url``). Entered on the caller's
        per-server ``stack``.
        """
        stype = server_def.get("type")
        if "command" in server_def or stype == "stdio":
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client

            params = StdioServerParameters(
                command=server_def["command"],
                args=list(server_def.get("args") or []),
                env=server_def.get("env") or None,
            )
            return await stack.enter_async_context(stdio_client(params))

        url = server_def.get("url")
        if not url:
            raise ValueError("MCP server def has neither 'command' nor 'url'")
        headers = server_def.get("headers") or None
        if stype == "sse":
            from mcp.client.sse import sse_client

            return await stack.enter_async_context(sse_client(url, headers=headers))
        from mcp.client.streamable_http import streamablehttp_client

        return await stack.enter_async_context(streamablehttp_client(url, headers=headers))

    async def _evict(self, key: str) -> None:
        """Drop a dead server: close its stack, remove its session, routes, and tools.

        Called after a timeout/error so the rest of the turn fast-fails that server
        instead of riding a poisoned session.
        """
        self._sessions.pop(key, None)
        self._routes = {n: r for n, r in self._routes.items() if r[0] != key}
        # Drop any tool whose route was just removed (must not stay advertised).
        live = set(self._routes)
        self._converse_tools = [t for t in self._converse_tools if t["toolSpec"]["name"] in live]
        stack = self._stacks.pop(key, None)
        if stack is not None:
            with contextlib.suppress(Exception):
                await stack.aclose()

    async def call(self, advertised_name: str, tool_input: dict[str, Any]) -> tuple[str, bool]:
        """Route a tool call to the owning server. Returns (text, is_error)."""
        import asyncio

        route = self._routes.get(advertised_name)
        if route is None:
            return (f"Error: unknown MCP tool '{advertised_name}'.", True)
        key, upstream = route
        session = self._sessions.get(key)
        if session is None:
            return (f"Error: MCP server '{key}' is not connected.", True)
        try:
            result = await asyncio.wait_for(
                session.call_tool(upstream, tool_input or {}), timeout=_CALL_TIMEOUT
            )
        except TimeoutError:
            # A timed-out session is considered dead: evict the server so subsequent calls
            # this turn fast-fail rather than waiting out the timeout on a poisoned session.
            await self._evict(key)
            return (f"Error: MCP tool '{advertised_name}' timed out.", True)
        except Exception as e:  # noqa: BLE001 — surfaced to the model as an error result
            # A generic transport/protocol error likely poisoned the session too — evict.
            await self._evict(key)
            return (f"Error calling MCP tool '{advertised_name}': {e}", True)
        return _result_to_text(result)

    async def aclose(self) -> None:
        """Tear down every connection (best-effort; bounded to the turn).

        Each server's transport is closed on its own stack (entered concurrently in
        ``open``), suppressing per-stack so one bad teardown can't block the others.
        """
        for stack in list(self._stacks.values()):
            with contextlib.suppress(Exception):
                await stack.aclose()
        self._stacks.clear()
        self._sessions.clear()
        self._routes.clear()
        self._converse_tools = []
