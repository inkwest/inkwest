"""
MCP per-tool permission policy: resolution + read/write.

Resolution order (most specific wins), used by the proxy gate and the context-hygiene
pruning:

    conv/tool -> conv/server('*') -> global/tool -> global/server('*') -> system default (ask)

Writes are per-server full-state replaces: the UI owns a server's full set of tool verdicts
and PUTs it; we replace all rows for that (scope, server_key). An empty set clears the
server's policy so everything inherits the next scope / the system default.
"""

import uuid
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mcp_tool_policy import (
    DEFAULT_VERDICT,
    SERVER_DEFAULT_TOOL,
    VERDICTS,
    SessionMcpToolPolicy,
    UserMcpToolPolicy,
)


class InvalidVerdictError(ValueError):
    """A verdict outside the allowed set was supplied."""


def _validate(verdict: str) -> str:
    if verdict not in VERDICTS:
        raise InvalidVerdictError(f"invalid verdict {verdict!r}; allowed: {', '.join(VERDICTS)}")
    return verdict


async def resolve_verdict(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    session_id: uuid.UUID | None,
    server_key: str,
    tool: str,
) -> str:
    """Resolve the effective verdict for one tool call. Never raises; defaults to 'ask'.

    Two small queries (session scope, user scope), each fetching only the specific-tool and
    server-default('*') rows for this server — the hot path the proxy calls per tool call.
    """
    if session_id is not None:
        sess = await _scope_rows(
            db, SessionMcpToolPolicy, SessionMcpToolPolicy.session_id, session_id, server_key, tool
        )
        if tool in sess:
            return sess[tool]
        if SERVER_DEFAULT_TOOL in sess:
            return sess[SERVER_DEFAULT_TOOL]

    usr = await _scope_rows(
        db, UserMcpToolPolicy, UserMcpToolPolicy.user_id, user_id, server_key, tool
    )
    if tool in usr:
        return usr[tool]
    if SERVER_DEFAULT_TOOL in usr:
        return usr[SERVER_DEFAULT_TOOL]

    return DEFAULT_VERDICT


async def _scope_rows(
    db: AsyncSession,
    model: Any,  # UserMcpToolPolicy | SessionMcpToolPolicy — dynamic ORM attrs, hence Any
    owner_col: Any,
    owner_id: uuid.UUID,
    server_key: str,
    tool: str,
) -> dict[str, str]:
    """{tool -> verdict} for the specific tool + the server-default row, one scope, one server."""
    rows = (
        await db.execute(
            select(model.tool, model.verdict).where(
                owner_col == owner_id,
                model.server_key == server_key,
                model.tool.in_((tool, SERVER_DEFAULT_TOOL)),
            )
        )
    ).all()
    return {t: v for t, v in rows}


# ---- read (whole scope, for the settings UI) -------------------------------- //


async def get_user_policies(db: AsyncSession, user_id: uuid.UUID) -> list[dict[str, str]]:
    """All of a user's global policy rows: [{server_key, tool, verdict}]."""
    rows = (
        await db.execute(
            select(
                UserMcpToolPolicy.server_key, UserMcpToolPolicy.tool, UserMcpToolPolicy.verdict
            ).where(UserMcpToolPolicy.user_id == user_id)
        )
    ).all()
    return [{"server_key": s, "tool": t, "verdict": v} for s, t, v in rows]


async def get_session_policies(db: AsyncSession, session_id: uuid.UUID) -> list[dict[str, str]]:
    """All of a conversation's policy override rows: [{server_key, tool, verdict}]."""
    rows = (
        await db.execute(
            select(
                SessionMcpToolPolicy.server_key,
                SessionMcpToolPolicy.tool,
                SessionMcpToolPolicy.verdict,
            ).where(SessionMcpToolPolicy.session_id == session_id)
        )
    ).all()
    return [{"server_key": s, "tool": t, "verdict": v} for s, t, v in rows]


# ---- write (per-server full-state replace) ---------------------------------- //


async def set_user_server_policy(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    server_key: str,
    entries: list[tuple[str, str]],
) -> None:
    """Replace all of (user, server_key)'s tool verdicts with ``entries`` (tool, verdict)."""
    for _, verdict in entries:
        _validate(verdict)
    await db.execute(
        delete(UserMcpToolPolicy).where(
            UserMcpToolPolicy.user_id == user_id, UserMcpToolPolicy.server_key == server_key
        )
    )
    db.add_all(
        UserMcpToolPolicy(user_id=user_id, server_key=server_key, tool=tool, verdict=verdict)
        for tool, verdict in entries
    )
    await db.commit()


async def set_session_server_policy(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    server_key: str,
    entries: list[tuple[str, str]],
) -> None:
    """Replace all of (session, server_key)'s tool verdicts with ``entries`` (tool, verdict)."""
    for _, verdict in entries:
        _validate(verdict)
    await db.execute(
        delete(SessionMcpToolPolicy).where(
            SessionMcpToolPolicy.session_id == session_id,
            SessionMcpToolPolicy.server_key == server_key,
        )
    )
    db.add_all(
        SessionMcpToolPolicy(
            session_id=session_id, server_key=server_key, tool=tool, verdict=verdict
        )
        for tool, verdict in entries
    )
    await db.commit()
