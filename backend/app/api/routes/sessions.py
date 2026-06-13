"""
Session management routes.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import ColumnElement, String, and_, cast, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.dependencies import CurrentUser, check_cluster_access, get_db
from app.api.routes.chat import close_session_connections
from app.core.config import get_settings
from app.models.cluster import Cluster
from app.models.message import Message
from app.models.schemas import (
    ClusterSummary,
    MessageResponse,
    SearchMessageHit,
    SearchResponse,
    SearchSessionHit,
    SessionCreate,
    SessionList,
    SessionReorder,
    SessionResponse,
    SessionUpdate,
    SessionWithMessages,
)
from app.models.session import Session
from app.models.system_prompt import SystemPrompt
from app.services import attachments
from app.services.model_registry import selectable_models
from app.services.pubsub import notify

router = APIRouter(prefix="/sessions", tags=["sessions"])


def check_model_selectable(model: str | None) -> None:
    """Raise 400 when the requested model is not in the admin-enabled list."""
    if model is not None and model not in selectable_models():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Model '{model}' is not enabled",
        )


def check_engine_selectable(engine: str | None, model: str | None) -> None:
    """Raise 400 when the engine pin can't run in this deployment / for this model.

    The pin is only meaningful with more than one engine enabled (the Engine·Model
    picker). When a model is also given, the pair must be runnable
    (Claude → claude-cli or inkwest-agent; other vendors → inkwest-agent only).
    """
    if engine is None:
        return
    from app.services.engines import enabled_engines, runnable_engines

    if engine not in enabled_engines():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Engine '{engine}' is not enabled",
        )
    if model is not None and engine not in runnable_engines(model):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Engine '{engine}' cannot run model '{model}'",
        )


async def check_session_access(session: Session, user: CurrentUser) -> None:
    """Raise 403 if user doesn't own this session (non-admin)."""
    if not user.is_admin() and session.user_id and str(session.user_id) != str(user.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this session",
        )


@router.get("", response_model=SessionList)
async def list_sessions(
    current_user: CurrentUser,
    cluster_id: UUID | None = None,
    search: str | None = None,
    include_archived: bool = False,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> SessionList:
    query = select(Session).options(selectinload(Session.cluster))
    query = query.where(Session.user_id == str(current_user.id))
    # Sessions with no interaction yet (created on chat open / MCP setup) are
    # hidden from the sidebar. Filtering here, not just client-side, keeps
    # pagination honest — otherwise invisible empties crowd real ones off page 1.
    # 'system' event messages (connector/MCP/model changes) don't count as interaction:
    # granting access on the start screen before the first message must not surface the
    # session.
    query = query.where(Session.messages.any(Message.role != "system"))

    if cluster_id:
        query = query.where(Session.cluster_id == cluster_id)
    if not include_archived:
        query = query.where(Session.is_archived == False)  # noqa: E712
    if search:
        # Same word-independent semantics as global search (see _search_tokens):
        # every token must appear in title or summary, any order, escaped literally.
        # A non-empty search yielding no tokens (e.g. whitespace) matches nothing,
        # mirroring global search — never silently fall back to "all sessions".
        patterns = [f"%{_escape_like(tok)}%" for tok in _search_tokens(search)]
        query = query.where(_title_summary_match(patterns) if patterns else false())

    count_query = select(func.count()).select_from(query.subquery())
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Stable, user-defined order (ASC = top); created_at/id break ties so the
    # arrangement never reshuffles on activity. Reordering is explicit (see below).
    query = query.order_by(Session.position.asc(), Session.created_at.desc(), Session.id)
    query = query.offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(query)
    sessions = result.scalars().all()

    return SessionList(
        items=[SessionResponse.model_validate(s) for s in sessions],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session(
    session_data: SessionCreate,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> SessionResponse:
    check_model_selectable(session_data.model)
    check_engine_selectable(session_data.engine, session_data.model)

    # A cluster is optional: with no cluster configured the session starts
    # without a Kube context. Validate access only when one is requested.
    if session_data.cluster_id is not None:
        cluster_result = await db.execute(
            select(Cluster).where(
                Cluster.id == session_data.cluster_id,
                Cluster.is_active == True,  # noqa: E712
            )
        )
        cluster = cluster_result.scalar_one_or_none()

        if not cluster:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Active cluster with ID '{session_data.cluster_id}' not found",
            )

        await check_cluster_access(current_user, str(cluster.id), db)

    # System prompt selection is admin-only. Non-admins always get the default
    # (resolved at run time from null columns), so reject any explicit choice.
    sp_id = session_data.system_prompt_id
    sp_custom = session_data.system_prompt_custom
    if sp_id is not None or sp_custom is not None:
        if not current_user.is_admin():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only admins may choose a system prompt; non-admins use the default",
            )
        if sp_id is not None and sp_custom is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Provide either system_prompt_id or system_prompt_custom, not both",
            )
        if sp_id is not None:
            sp_result = await db.execute(
                select(SystemPrompt).where(
                    SystemPrompt.id == sp_id,
                    SystemPrompt.is_active == True,  # noqa: E712
                )
            )
            if sp_result.scalar_one_or_none() is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Active system prompt '{sp_id}' not found",
                )

    # New sessions go to the top of the manually-ordered list: one slot above the
    # current minimum. position is never touched by activity, so the list stays put.
    min_pos = await db.scalar(
        select(func.min(Session.position)).where(Session.user_id == str(current_user.id))
    )
    next_pos = (min_pos - 1) if min_pos is not None else 0

    session = Session(**session_data.model_dump(), user_id=str(current_user.id), position=next_pos)
    db.add(session)
    await db.flush()
    await db.refresh(session)
    await db.commit()

    await notify(
        {"type": "session_created", "session_id": str(session.id)}, user_id=str(current_user.id)
    )

    return SessionResponse.model_validate(session)


@router.post("/reorder", status_code=status.HTTP_204_NO_CONTENT)
async def reorder_sessions(
    payload: SessionReorder,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Set the sidebar order from a full top-to-bottom list of session ids.

    Only the caller's own sessions are repositioned (foreign/unknown ids are
    ignored); position is assigned 0..n by request order, so 0 is the top.
    """
    ids = [str(i) for i in payload.session_ids]
    if not ids:
        return None

    result = await db.execute(
        select(Session).where(
            Session.id.in_(ids),
            Session.user_id == str(current_user.id),
        )
    )
    owned = {str(s.id): s for s in result.scalars().all()}

    pos = 0
    for sid in ids:
        session = owned.get(sid)
        if session is not None:
            session.position = pos
            pos += 1

    await db.commit()

    # Propagate the new order to the user's other tabs: same fan-out path as
    # create/update/delete — clients just re-fetch the position-ordered list.
    await notify({"type": "session_reordered"}, user_id=str(current_user.id))
    return None


@router.get("/{session_id}", response_model=SessionWithMessages)
async def get_session(
    session_id: UUID,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> SessionWithMessages:
    result = await db.execute(
        select(Session)
        .where(Session.id == session_id)
        .options(
            selectinload(Session.messages),
            selectinload(Session.cluster),
        )
    )
    session = result.scalar_one_or_none()

    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session with ID '{session_id}' not found",
        )

    await check_session_access(session, current_user)
    return SessionWithMessages.model_validate(session)


@router.patch("/{session_id}", response_model=SessionResponse)
async def update_session(
    session_id: UUID,
    session_data: SessionUpdate,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> SessionResponse:
    result = await db.execute(select(Session).where(Session.id == session_id))
    session = result.scalar_one_or_none()

    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session with ID '{session_id}' not found",
        )

    await check_session_access(session, current_user)

    update_data = session_data.model_dump(exclude_unset=True)
    if "model" in update_data:
        check_model_selectable(update_data["model"])
    # Validate any engine pin against the model that will be in effect after this
    # patch (the incoming model when set, else the session's current one).
    if "engine" in update_data:
        effective = update_data["model"] if "model" in update_data else session.model
        check_engine_selectable(update_data["engine"], effective)
    model_changed = "model" in update_data and update_data["model"] != session.model
    new_model = update_data.get("model")
    for field, value in update_data.items():
        setattr(session, field, value)

    await db.flush()
    await db.refresh(session)
    await db.commit()

    # Record a model switch inline in the conversation. On a reset (model=None) we store
    # the *resolved* default model the session will now use, not null — so the event is a
    # complete, self-contained record. Other field edits aren't surfaced.
    if model_changed:
        from app.services.messages import save_session_event
        from app.services.model_registry import default_model

        effective_model = new_model or default_model()
        await save_session_event(
            db, session.id, "model", "changed", targets=[{"model": effective_model}]
        )

    await notify(
        {"type": "session_updated", "session_id": str(session.id)}, user_id=str(session.user_id)
    )

    return SessionResponse.model_validate(session)


class SessionMcpResponse(BaseModel):
    """The MCP catalog keys enabled for a conversation."""

    enabled: list[str]


class SessionMcpUpdate(BaseModel):
    """Set the MCP catalog keys enabled for a conversation."""

    enabled: list[str]


async def _load_owned_session(session_id: UUID, user: CurrentUser, db: AsyncSession) -> Session:
    """Fetch a session and enforce ownership, or raise 404/403."""
    result = await db.execute(select(Session).where(Session.id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session with ID '{session_id}' not found",
        )
    await check_session_access(session, user)
    return session


@router.get("/{session_id}/mcp", response_model=SessionMcpResponse)
async def get_session_mcp(
    session_id: UUID,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> SessionMcpResponse:
    """Return the MCP servers enabled for this conversation."""
    session = await _load_owned_session(session_id, current_user, db)
    return SessionMcpResponse(enabled=list(session.enabled_mcp_servers or []))


@router.put("/{session_id}/mcp", response_model=SessionMcpResponse)
async def set_session_mcp(
    session_id: UUID,
    body: SessionMcpUpdate,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> SessionMcpResponse:
    """
    Set the MCP servers enabled for this conversation.

    Keys are validated against the catalog (no arbitrary server injection). The
    selection is persisted and merged into the conversation's .claude.json; it
    takes effect on the next message (the CLI reads its config at launch).
    """
    from app.services.mcp_catalog import catalog_keys
    from app.services.mcp_config import apply_session_mcp_selection
    from app.services.mcp_oauth_catalog import oauth_catalog_keys
    from app.services.messages import save_session_event

    session = await _load_owned_session(session_id, current_user, db)
    previous = list(session.enabled_mcp_servers or [])

    # Dedupe while preserving order; reject anything not in either catalog (static or oauth).
    enabled = list(dict.fromkeys(body.enabled))
    known = set(catalog_keys()) | set(oauth_catalog_keys())
    unknown = [k for k in enabled if k not in known]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown MCP server key(s): {', '.join(unknown)}",
        )

    session.enabled_mcp_servers = enabled
    await db.flush()
    await db.commit()

    # Reconcile the conversation's .claude.json (no-op in non-sandbox mode). An enabled oauth
    # server with no active grant is skipped; the reconnect prompt surfaces on the next turn.
    await apply_session_mcp_selection(str(session_id), enabled, user_id=current_user.id, db=db)

    # Surface the change inline in the conversation: one batched event for the servers
    # connected and one for those disconnected (targets carry the structured keys).
    prev_set, new_set = set(previous), set(enabled)
    added = [{"server": k} for k in enabled if k not in prev_set]
    removed = [{"server": k} for k in previous if k not in new_set]
    if added:
        await save_session_event(db, session_id, "mcp", "connected", targets=added)
    if removed:
        await save_session_event(db, session_id, "mcp", "disconnected", targets=removed)

    return SessionMcpResponse(enabled=enabled)


# ---------------------------------------------------------------------------
# Attachments — files uploaded into the conversation's /workdir so the claude-cli
# agent can read them via @/workdir/<file>. Sandbox/claude-cli only.
# ---------------------------------------------------------------------------


class AttachmentDescriptor(BaseModel):
    """A stored attachment, as returned to the client and persisted on the message."""

    filename: str  # on-disk basename in the workdir (slugified, deduped)
    original_name: str
    content_type: str
    size: int


def _require_attachments_enabled() -> None:
    """Guard write endpoints: attachments need the sandbox (claude-cli) workdir."""
    settings = get_settings()
    if not (settings.sandbox_enabled and settings.ai_provider == "claude-cli"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="File attachments require the sandbox (claude-cli) to be enabled",
        )


@router.post("/{session_id}/attachments", response_model=list[AttachmentDescriptor])
async def upload_attachments(
    session_id: UUID,
    current_user: CurrentUser,
    files: list[UploadFile] = File(...),
    db: AsyncSession = Depends(get_db),
) -> list[AttachmentDescriptor]:
    """Store one or more files in the conversation's workdir, returning descriptors."""
    _require_attachments_enabled()
    await _load_owned_session(session_id, current_user, db)

    max_per = get_settings().attachment_max_per_message
    if len(files) > max_per:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"At most {max_per} files per message",
        )

    descriptors: list[AttachmentDescriptor] = []
    for upload in files:
        data = await upload.read()
        try:
            desc = attachments.write_attachment(
                str(session_id),
                upload.filename or "file",
                data,
                upload.content_type or "",
            )
        except attachments.AttachmentError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
        descriptors.append(AttachmentDescriptor.model_validate(desc))
    return descriptors


@router.delete("/{session_id}/attachments/{filename}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_attachment(
    session_id: UUID,
    filename: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove a not-yet-sent attachment (e.g. the user cleared a chip)."""
    _require_attachments_enabled()
    await _load_owned_session(session_id, current_user, db)
    try:
        attachments.delete_attachment(str(session_id), filename)
    except attachments.AttachmentError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e


@router.get("/{session_id}/attachments/{filename}")
async def get_attachment(
    session_id: UUID,
    filename: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    """Stream a stored attachment back (thumbnail re-render on history reload).

    Not gated on the sandbox flag: previously-uploaded files in a conversation's
    history must stay viewable even if the feature is later turned off.
    """
    await _load_owned_session(session_id, current_user, db)
    try:
        path = attachments.resolve_existing(str(session_id), filename)
    except attachments.AttachmentError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    return FileResponse(path)


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: UUID,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    result = await db.execute(select(Session).where(Session.id == session_id))
    session = result.scalar_one_or_none()

    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session with ID '{session_id}' not found",
        )

    await check_session_access(session, current_user)
    session.is_archived = True
    await db.flush()
    await db.commit()

    # Drop any brokered external-access credentials held for this session.
    if session.user_id is not None:
        from app.services.connectors.store import get_credential_store
        from app.tools.connector_exec import cleanup_aws_files

        await get_credential_store().revoke_session(session.user_id, session.id)
        # Remove the session tmpfs (~/.aws projection + generated EKS kubeconfigs).
        cleanup_aws_files(session.id)

    # Cancel any detached turn first — orchestration outlives the socket now, so
    # closing connections alone would leave it running against a deleted session.
    from app.services.turn_runner import turn_runner

    await turn_runner.cancel(str(session.id))

    # Tear down the conversation's sandbox pair + its durable dir (hard teardown:
    # the conversation is gone, so nothing needs to resume).
    settings = get_settings()
    if settings.sandbox_enabled:
        from app.services import sandbox

        await sandbox.teardown(str(session.id))

    await close_session_connections(str(session.id))
    await notify(
        {"type": "session_deleted", "session_id": str(session.id)},
        user_id=str(session.user_id) if session.user_id else None,
    )


@router.get("/{session_id}/stats")
async def get_session_stats(
    session_id: UUID,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(select(Session).where(Session.id == session_id))
    session = result.scalar_one_or_none()

    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session with ID '{session_id}' not found",
        )

    await check_session_access(session, current_user)

    return {
        "session_id": str(session.id),
        "total_input_tokens": session.total_input_tokens,
        "total_output_tokens": session.total_output_tokens,
        "total_tokens": session.total_input_tokens + session.total_output_tokens,
        "total_tool_calls": session.total_tool_calls,
        "total_iterations": session.total_iterations,
    }


@router.get("/{session_id}/failed-messages", response_model=list[MessageResponse])
async def get_failed_messages(
    session_id: UUID,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[MessageResponse]:
    session_result = await db.execute(select(Session).where(Session.id == session_id))
    session = session_result.scalar_one_or_none()
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session with ID '{session_id}' not found",
        )

    await check_session_access(session, current_user)

    # KNOWN LIMITATION: a reconciled (interrupted) user message stays
    # error+retryable even if the user later continued the conversation past it,
    # so this can return a stale, no-longer-trailing message. The retry path
    # (get_retryable_message, over the chat WS) acts on the *newest* such row;
    # when it is not the trailing turn the regenerated reply lands at the end of
    # history rather than after the retried message. The producer is
    # reconcile_interrupted_turns(); both should move to an explicit per-turn
    # terminal status together.
    result = await db.execute(
        select(Message)
        .where(
            Message.session_id == session_id,
            Message.status == "error",
            Message.is_retryable == True,  # noqa: E712
        )
        .order_by(Message.created_at.desc())
    )
    messages = result.scalars().all()

    return [MessageResponse.model_validate(m) for m in messages]


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

search_router = APIRouter(prefix="/search", tags=["search"])

MAX_SESSION_RESULTS = 20
MAX_MESSAGE_HITS_PER_SESSION = 3
SNIPPET_LENGTH = 150
# Cap tokens per query: each token becomes an AND-ed, unindexed ILIKE full-scan, so an
# unbounded word count would let one search amplify into arbitrarily many scans.
MAX_SEARCH_TOKENS = 10


def _escape_like(token: str) -> str:
    # Escape LIKE metacharacters so user input matches literally, not as a pattern
    # (prevents '%'/'_' acting as wildcards and backslash from corrupting the escape).
    return token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _search_tokens(query: str) -> list[str]:
    """Split a query into distinct search tokens (word-independent search).

    Whitespace-separated (order and contiguity irrelevant), case-insensitively
    de-duplicated (ILIKE ignores case, so repeats only add cost), and capped at
    MAX_SEARCH_TOKENS to bound the number of full-scan ILIKE predicates one query
    can generate. Returns raw tokens; callers escape for SQL via _escape_like.
    """
    seen: set[str] = set()
    tokens: list[str] = []
    for token in query.split():
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        tokens.append(token)
        if len(tokens) >= MAX_SEARCH_TOKENS:
            break
    return tokens


def _title_summary_match(patterns: list[str]) -> ColumnElement[bool]:
    """AND of per-token (title OR summary) escaped-ILIKE predicates.

    Caller must pass a non-empty list (an empty and_() matches everything).
    """
    return and_(
        *(
            or_(
                Session.title.ilike(pattern, escape="\\"),
                Session.summary.ilike(pattern, escape="\\"),
            )
            for pattern in patterns
        )
    )


def _make_snippet(text: str, tokens: list[str]) -> str:
    lower = text.lower()
    # Anchor the snippet on the earliest-matching token (word-independent search:
    # tokens may appear in any order, so the first hit isn't necessarily tokens[0]).
    idx = -1
    for token in tokens:
        pos = lower.find(token.lower())
        if pos != -1 and (idx == -1 or pos < idx):
            idx = pos
    if idx == -1:
        return text[:SNIPPET_LENGTH]
    start = max(0, idx - SNIPPET_LENGTH // 3)
    end = start + SNIPPET_LENGTH
    snippet = text[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet


@search_router.get("", response_model=SearchResponse)
async def search_global(
    current_user: CurrentUser,
    q: str = Query(..., min_length=2),
    db: AsyncSession = Depends(get_db),
) -> SearchResponse:
    # Word-independent search: split on whitespace, AND a predicate per token so a
    # multi-word query matches regardless of word order or contiguity (space = AND).
    tokens = _search_tokens(q)
    if not tokens:
        return SearchResponse(query=q, results=[], total=0)
    patterns = [f"%{_escape_like(tok)}%" for tok in tokens]

    # Sessions matching by title/summary
    session_query = (
        select(Session)
        .options(selectinload(Session.cluster))
        .where(
            Session.is_archived == False,  # noqa: E712
            Session.user_id == str(current_user.id),
        )
    )

    # Each token must appear in title OR summary; all tokens must be present.
    title_match_query = session_query.where(_title_summary_match(patterns))
    title_result = await db.execute(
        title_match_query.order_by(Session.updated_at.desc()).limit(MAX_SESSION_RESULTS)
    )
    title_sessions = {s.id: s for s in title_result.scalars().all()}

    # Messages matching by content (cast JSONB to text). Exclude 'system' event
    # messages: their structured JSON would match on metadata (e.g. "connected",
    # account names) — noise — and could surface sessions the sidebar's empty filter hides.
    # Perf note: this is an unindexed sequential scan (cast(content)->text ILIKE); the
    # MAX_SEARCH_TOKENS cap bounds per-row predicates but not the scan. Fuzzy/indexed
    # search (pg_trgm + GIN) is intentionally out of scope here — to be handled in a
    # dedicated follow-up ticket.
    msg_query = (
        select(Message)
        .join(Session, Message.session_id == Session.id)
        .where(
            Session.is_archived == False,  # noqa: E712
            Message.role != "system",
            and_(
                *(cast(Message.content, String).ilike(pattern, escape="\\") for pattern in patterns)
            ),
        )
    )
    msg_query = msg_query.where(Session.user_id == str(current_user.id))

    msg_query = msg_query.order_by(Message.created_at.desc()).limit(500)
    msg_result = await db.execute(msg_query)
    messages = msg_result.scalars().all()

    # Group messages by session, cap per session
    msg_by_session: dict[UUID, list[Message]] = {}
    for m in messages:
        lst = msg_by_session.setdefault(m.session_id, [])
        if len(lst) < MAX_MESSAGE_HITS_PER_SESSION:
            lst.append(m)

    # Fetch sessions for message-only matches we don't already have
    missing_ids = set(msg_by_session.keys()) - set(title_sessions.keys())
    if missing_ids:
        extra_query = (
            select(Session)
            .options(selectinload(Session.cluster))
            .where(Session.id.in_(missing_ids))
        )
        extra_result = await db.execute(extra_query)
        for s in extra_result.scalars().all():
            title_sessions[s.id] = s

    # Build results
    all_session_ids = list(title_sessions.keys()) + [
        sid for sid in msg_by_session if sid not in title_sessions
    ]
    # Deduplicate while preserving order
    seen: set[UUID] = set()
    unique_ids: list[UUID] = []
    for sid in all_session_ids:
        if sid not in seen:
            seen.add(sid)
            unique_ids.append(sid)

    results: list[SearchSessionHit] = []
    for sid in unique_ids[:MAX_SESSION_RESULTS]:
        session = title_sessions.get(sid)
        if not session:
            continue
        matched_msgs = [
            SearchMessageHit(
                id=m.id,
                role=m.role,
                snippet=_make_snippet(
                    m.content if isinstance(m.content, str) else str(m.content), tokens
                ),
                created_at=m.created_at,
            )
            for m in msg_by_session.get(sid, [])
        ]
        # Title hit = every token present in title or summary (word-independent).
        title_l = (session.title or "").lower()
        summary_l = (session.summary or "").lower()
        is_title_match = all(tok.lower() in title_l or tok.lower() in summary_l for tok in tokens)
        cluster_summary = (
            ClusterSummary.model_validate(session.cluster) if session.cluster else None
        )
        results.append(
            SearchSessionHit(
                id=session.id,
                title=session.title,
                cluster=cluster_summary,
                created_at=session.created_at,
                updated_at=session.updated_at,
                matched_messages=matched_msgs,
                title_match=bool(is_title_match),
            )
        )

    return SearchResponse(query=q, results=results, total=len(results))
