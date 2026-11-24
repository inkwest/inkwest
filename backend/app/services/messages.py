"""
Message persistence helpers for chat sessions.

Handles loading and saving messages to the database,
and managing session metadata like titles.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import Select, and_, delete, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.message import Message
from app.models.session import Session
from app.services.message_window import merge_consecutive_roles, prune_orphan_tool_calls
from app.services.pubsub import notify

logger = logging.getLogger(__name__)


# Subject/action of the session-event row that marks where the replayed transcript starts.
COMPACTION_SUBJECT = "context"
COMPACTION_ACTION = "compacted"


def is_compaction_boundary(msg: Message) -> bool:
    """Whether a row is a compaction boundary that may cut the replayed transcript.

    A row with no usable summary is not one: cutting there would replay nothing, restore nothing.
    """
    content = msg.content
    if not (
        msg.role == "system"
        and isinstance(content, dict)
        and content.get("type") == "session_event"
        and content.get("subject") == COMPACTION_SUBJECT
        and content.get("action") == COMPACTION_ACTION
    ):
        return False
    summary = content.get("summary")
    return isinstance(summary, str) and bool(summary.strip())


def last_boundary_index(rows: list[Message]) -> int | None:
    """Index of the last compaction boundary in a chronological row list, or None."""
    return max((i for i, msg in enumerate(rows) if is_compaction_boundary(msg)), default=None)


async def delete_messages(
    db: AsyncSession, session_id: str | uuid.UUID, message_ids: list[uuid.UUID]
) -> None:
    """Hard-delete rows by id within one session (undoes an abandoned compaction).

    ``session_id`` is not redundant: a bare id list makes the helper able to delete across
    conversations, which is one careless caller away from an IDOR-shaped data loss.
    """
    if not message_ids:
        return
    if isinstance(session_id, str):
        session_id = uuid.UUID(session_id)
    await db.execute(
        delete(Message).where(Message.session_id == session_id, Message.id.in_(message_ids))
    )
    await db.commit()


async def get_session_messages(
    db: AsyncSession, session_id: str | uuid.UUID
) -> list[dict[str, Any]]:
    """
    Load messages for Claude API format.

    Retrieves all messages for a session and formats them
    for the Claude messages API.

    Args:
        db: Database session
        session_id: UUID of the chat session

    Returns:
        List of message dicts with 'role' and 'content' keys
    """
    if isinstance(session_id, str):
        session_id = uuid.UUID(session_id)

    result = await db.execute(
        select(Message).where(Message.session_id == session_id).order_by(Message.created_at)
    )
    rows = list(result.scalars().all())

    # Compaction boundary: only the rows AFTER the last one are replayed to the model —
    # everything before it is represented by the summary the boundary carries, injected
    # into the system prompt (see app.services.compaction). Sliced by INDEX, not by
    # timestamp, so rows written in the same instant can't land on the wrong side.
    last_boundary = last_boundary_index(rows)
    messages = rows if last_boundary is None else rows[last_boundary + 1 :]

    formatted_messages = []
    for msg in messages:
        # 'system' messages are UI-only event markers (e.g. connector grant/revoke).
        # They are never part of the model's conversation — skip them entirely so the
        # Claude API payload stays valid (no 'system' role in the messages array).
        if msg.role == "system":
            continue
        content = msg.content
        # tool_result messages are stored separately for UI but sent as role="user" per API spec
        api_role = "user" if msg.role == "tool_result" else msg.role

        if isinstance(content, str):
            formatted_messages.append({"role": api_role, "content": content})
        elif isinstance(content, list):
            formatted_messages.append({"role": api_role, "content": content})
        elif isinstance(content, dict):
            if content.get("type") in ("text", "tool_result", "tool_use"):
                formatted_messages.append({"role": api_role, "content": [content]})
            else:
                formatted_messages.append(
                    {"role": api_role, "content": content.get("text", str(content))}
                )
        else:
            logger.warning(f"Unknown content format for message {msg.id}: {type(content)}")
            formatted_messages.append({"role": api_role, "content": str(content)})

    # Strip dangling tool_use / orphan tool_result left by a turn interrupted
    # mid-tool — replaying them would make the AI API reject the whole request.
    formatted_messages = prune_orphan_tool_calls(formatted_messages)
    # Pruning can leave two same-role neighbours (and a compaction/retry can create them
    # directly); both APIs reject a non-alternating transcript, so normalize last.
    formatted_messages = merge_consecutive_roles(formatted_messages)

    logger.debug(f"Loaded {len(formatted_messages)} messages for session {session_id}")
    return formatted_messages


async def save_message(
    db: AsyncSession,
    session_id: str | uuid.UUID,
    role: str,
    content: Any,
    tool_calls: list[dict[str, Any]] | None = None,
    tool_call_id: str | None = None,
    token_count: int | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cache_write_tokens: int | None = None,
    thinking_content: str | None = None,
    latency_ms: int | None = None,
    model: str | None = None,
    engine: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    author_user_id: str | uuid.UUID | None = None,
) -> Message:
    """
    Save a message to the database.

    Args:
        db: Database session
        session_id: UUID of the chat session
        role: Message role (user, assistant)
        content: Message content (string, list, or dict)
        tool_calls: List of tool calls made in this message
        tool_call_id: ID of the tool call this message responds to
        attachments: Files attached to a user turn (descriptor dicts)
        author_user_id: Who sent this user turn (shared conversations); None = the AI
        token_count: Number of tokens in the message (deprecated, use output_tokens)
        input_tokens: Number of fresh (uncached) input tokens for this API call
        output_tokens: Number of output tokens for this API call
        cache_read_tokens: Prompt-cache tokens read for this turn (input-side)
        cache_write_tokens: Prompt-cache tokens written for this turn (input-side)
        thinking_content: AI reasoning/thinking content if extended thinking enabled
        latency_ms: API call latency in milliseconds
        model: Model that produced this turn (assistant messages) for cost attribution
        engine: Agentic engine that produced this turn (assistant messages)

    Returns:
        Created Message object
    """
    if isinstance(session_id, str):
        session_id = uuid.UUID(session_id)
    if isinstance(author_user_id, str):
        author_user_id = uuid.UUID(author_user_id)

    # Normalize content to JSON-serializable format
    if isinstance(content, str):
        # Store simple text messages as-is
        normalized_content = content
    else:
        # Lists and dicts can be stored directly in JSONB
        normalized_content = content

    message = Message(
        session_id=session_id,
        role=role,
        content=normalized_content,
        tool_calls=tool_calls,
        tool_call_id=tool_call_id,
        # Fall back to output_tokens only when token_count is unset; an explicit 0
        # must be preserved (a falsy `or` would silently swap it for output_tokens).
        token_count=token_count if token_count is not None else output_tokens,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        thinking_content=thinking_content,
        latency_ms=latency_ms,
        model=model,
        engine=engine,
        attachments=attachments,
        author_user_id=author_user_id,
    )
    db.add(message)
    # No refresh: a read after commit reopens a transaction left idle for the rest of the stream.
    # Server defaults come back through the INSERT's RETURNING.
    try:
        await db.commit()
    except Exception:
        await db.rollback()  # else the session stays aborted and every later write fails
        raise

    logger.debug(f"Saved {role} message {message.id} to session {session_id}")
    return message


async def save_session_event(
    db: AsyncSession,
    session_id: str | uuid.UUID,
    subject: str,
    action: str,
    targets: list[dict[str, Any]] | None = None,
    payload: dict[str, Any] | None = None,
    strict: bool = False,
) -> Message | None:
    """Record a session activity event as a fully-structured 'system' message.

    Stores subject-agnostic, machine-readable data — ``{type, subject, action, targets}``
    — so any source (AWS, GitLab, MCP, model, …) reuses one path and the wording lives
    only in the frontend. ``targets`` is a structured list of what changed (AWS
    account/role objects, MCP server keys, the new model, …), kept as data so a
    conversation export is a plain ``SELECT`` with no string parsing.

    Surfaced inline in the conversation as a faded note. Excluded from the model's
    context (see :func:`get_session_messages`).

    ``payload`` merges extra top-level keys for events that carry more than a target list
    (the compaction boundary's ``summary``). ``strict`` re-raises instead of swallowing:
    a note about a change is best-effort, but an event the replay logic depends on is not.
    """
    content: dict[str, Any] = {"type": "session_event", "subject": subject, "action": action}
    if targets:
        content["targets"] = targets
    if payload:
        content.update(payload)
    try:
        # Own session: a failed write's rollback must not expire the caller's loaded rows.
        async with AsyncSession(db.bind, expire_on_commit=False) as own:
            return await save_message(own, session_id, "system", content)
    except Exception:  # noqa: BLE001 — the conversation note is non-critical
        logger.warning(
            "session_event_persist_failed",
            extra={"subject": subject, "action": action, "session_id": str(session_id)},
        )
        if strict:
            raise
        return None


# Shown on turns that never produced a reply — killed mid-flight by a server
# restart, rolling deploy, pod eviction, OOM or crash. Cause-agnostic on
# purpose: the same dangling state can also follow a turn that errored without
# persisting anything. Surfaced through the existing retry flow.
INTERRUPTED_TURN_ERROR = (
    "The previous response didn't finish (the turn was interrupted). Retry to regenerate it."
)


async def reconcile_interrupted_turns(db: AsyncSession) -> int:
    """Mark turns left dangling by a process restart as retryable errors.

    A turn persists the user message at the start but the assistant reply only
    at the end. If the process dies in between — rolling deploy, pod eviction,
    OOM, crash — the detached turn task is killed without persisting a reply, so
    the session is left ending on an unanswered ``user`` message (the user
    "comes back to a question with no answer"; see ``turn_runner``). The
    in-memory ``turn_runner`` only survives a WS disconnect, not the process
    dying, so a restart-time sweep is the backstop.

    Marks each such ``user`` message ``error`` + retryable so it surfaces in
    ``/failed-messages`` and the UI offers a retry instead of silently showing
    nothing. Idempotent — skips messages already ``error`` and leaves
    ``retry_count`` untouched (so a re-interrupted retry is re-flagged without
    losing its attempt count).

    Scope / limitations (detection is structural — it flags any session whose
    newest row is a ``user`` message with ``status != 'error'`` — so it cannot
    tell *interrupted* apart from *completed-with-no-persisted-reply*):

    * False positives (currently benign — retry is dormant): the CLI orchestrator
      only persists an assistant row when the turn produced text or tool calls
      (see ``orchestrator_cli``), so a turn that completes *empty* leaves a
      trailing ``user`` row and is wrongly flagged. A turn that errors in-process
      is no longer one of these: ``turn_runner`` persists a trailing ``turn
      aborted`` system row (the abort marker), so the session doesn't end on the
      ``user`` row and a later unrelated restart won't re-flag it.
    * False negatives: the streaming API orchestrators persist intermediate
      ``assistant``/``tool_result`` rows, so one of their turns interrupted
      mid-tool ends on an ``assistant`` (unanswered ``tool_use``) or
      ``tool_result`` row and is NOT detected.
    * The robust fix for both is an explicit per-turn terminal status rather than
      structural inference.
    * Assumes no turn is in flight anywhere when it runs. True on a single
      replica: uvicorn serves no requests until startup completes, so this runs
      before any turn can start. With multiple replicas — including the
      transient two-pod overlap of a default RollingUpdate — a starting replica
      could flag a turn still running on another replica, mislabelling a live
      turn as failed. Deploy single-replica with strategy ``Recreate`` (or
      RollingUpdate ``maxSurge: 0``).

    Returns the number of turns reconciled.
    """
    # An interrupted turn is a "user" message with nothing newer in its session.
    # Expressed as an anti-join (NOT EXISTS of a later message). The OR tie-break
    # can't serve as an index range bound, so Postgres runs this as a hash
    # anti-join over a full scan of `messages` rather than an indexed probe (per
    # EXPLAIN) — fine for a one-shot startup sweep, not for a hot path. Portable
    # across Postgres and SQLite. The id tie-break only matters if created_at
    # collides (it shouldn't: each message is committed in its own transaction,
    # so func.now() differs) — kept for correctness, not performance.
    later = aliased(Message)
    has_newer = (
        select(later.id)
        .where(
            later.session_id == Message.session_id,
            or_(
                later.created_at > Message.created_at,
                and_(later.created_at == Message.created_at, later.id > Message.id),
            ),
        )
        .exists()
    )

    result = await db.execute(
        update(Message)
        .where(
            Message.role == "user",
            Message.status != "error",
            ~has_newer,
        )
        .values(status="error", is_retryable=True, error_message=INTERRUPTED_TURN_ERROR)
        .execution_options(synchronize_session=False)
    )
    await db.commit()

    # execute() is typed Result, but a DML statement yields a CursorResult.
    # The caller (lifespan) logs the count as a structured event.
    return cast("CursorResult[Any]", result).rowcount or 0


def _newest_retryable_query(session_id: uuid.UUID) -> Select[tuple[Message]]:
    """SELECT for the newest retryable ``user`` message in a session (or None)."""
    return (
        select(Message)
        .where(
            Message.session_id == session_id,
            Message.role == "user",
            Message.status == "error",
            Message.is_retryable == True,  # noqa: E712
        )
        .order_by(Message.created_at.desc())
        .limit(1)
    )


async def get_retryable_message(db: AsyncSession, session_id: str | uuid.UUID) -> str | None:
    """Return the content of the session's newest retryable ``user`` message.

    Read-only: the error flag is cleared separately via :func:`clear_retry_state`,
    and only *after* the retry turn is confirmed to start — otherwise a turn that
    never starts (e.g. one already in progress) would leave the message a
    non-retryable success with no reply and no way to retry again.

    Usually this row is the trailing (interrupted) turn — see
    :func:`reconcile_interrupted_turns`. It need not be: a turn interrupted, then
    talked past, then reconciled on a later restart can leave a non-trailing
    retryable row; retrying it replays the whole history and appends the reply at
    the end (a documented limitation, not corruption).

    Returns ``None`` when nothing is retryable.
    """
    if isinstance(session_id, str):
        session_id = uuid.UUID(session_id)

    message = (await db.execute(_newest_retryable_query(session_id))).scalars().first()
    if message is None:
        return None
    if not isinstance(message.content, str):
        # User messages are always stored as plain strings; a structured payload
        # here would replay as a Python repr, so refuse rather than feed garbage.
        logger.warning(
            "retryable_message_non_string_content",
            extra={"session_id": str(session_id), "message_id": str(message.id)},
        )
        return None
    return message.content


async def clear_retry_state(db: AsyncSession, session_id: str | uuid.UUID) -> None:
    """Clear the error flag on the newest retryable ``user`` message and bump its
    retry count. Call only once the retry turn has actually started (see
    :func:`get_retryable_message`). No-op if nothing is retryable.

    Reusing the row (vs. inserting a new turn) means a page reload after a
    successful retry won't re-show the stale error bubble.
    """
    if isinstance(session_id, str):
        session_id = uuid.UUID(session_id)

    message = (await db.execute(_newest_retryable_query(session_id))).scalars().first()
    if message is None:
        return
    message.status = "success"
    message.error_message = None
    message.is_retryable = False
    message.retry_count += 1
    await db.commit()


# A session row is created as soon as a chat is opened (e.g. to attach MCP or
# connector config), so abandoned ones pile up with no messages — invisible in
# the UI (the list endpoint hides them) and undeletable there. A generous age
# keeps any in-progress setup safe across restarts.
EMPTY_SESSION_MAX_AGE = timedelta(hours=24)


async def purge_empty_sessions(db: AsyncSession) -> int:
    """
    Delete sessions older than EMPTY_SESSION_MAX_AGE that never received a message.

    Returns the number of sessions purged.
    """
    # created_at is a naive (timezone-less) column — bind a naive UTC cutoff;
    # an aware one makes asyncpg refuse the TIMESTAMP WITHOUT TIME ZONE bind.
    cutoff = datetime.now(UTC).replace(tzinfo=None) - EMPTY_SESSION_MAX_AGE
    result = await db.execute(
        delete(Session)
        .where(Session.created_at < cutoff, ~Session.messages.any())
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return cast("CursorResult[Any]", result).rowcount or 0


async def update_session_title(db: AsyncSession, session_id: str | uuid.UUID, title: str) -> None:
    """
    Update the session title.

    Args:
        db: Database session
        session_id: UUID of the chat session
        title: New title for the session
    """
    if isinstance(session_id, str):
        session_id = uuid.UUID(session_id)

    # Capture the owner so the change event is fanned out only to that user's tabs.
    owner_id = (
        await db.execute(select(Session.user_id).where(Session.id == session_id))
    ).scalar_one_or_none()
    await db.execute(update(Session).where(Session.id == session_id).values(title=title))
    await db.commit()
    logger.debug(f"Updated title for session {session_id}: {title}")

    await notify(
        {"type": "session_updated", "session_id": str(session_id)},
        user_id=str(owner_id) if owner_id else None,
    )


async def ensure_session_title(
    db: AsyncSession, session_id: str | uuid.UUID, user_message: str
) -> str | None:
    """
    Set session title if not already set.

    Generates a title from the first user message.

    Args:
        db: Database session
        session_id: UUID of the chat session
        user_message: The user's message to generate title from

    Returns:
        The generated title, or None if title was already set
    """
    if isinstance(session_id, str):
        session_id = uuid.UUID(session_id)

    # Check if session already has a title
    result = await db.execute(select(Session).where(Session.id == session_id))
    session = result.scalar_one_or_none()

    if not session:
        logger.warning(f"Session not found: {session_id}")
        return None

    if session.title:
        logger.debug(f"Session {session_id} already has title: {session.title}")
        return None

    # Generate title from first message
    # Take first 60 characters, breaking at word boundary if possible
    title = user_message.strip()

    if len(title) > 60:
        # Try to break at a word boundary
        truncated = title[:60]
        last_space = truncated.rfind(" ")
        if last_space > 40:  # Only break at space if it's reasonably far in
            title = truncated[:last_space] + "..."
        else:
            title = truncated + "..."

    await update_session_title(db, session_id, title)
    return title


async def get_message_count(db: AsyncSession, session_id: str | uuid.UUID) -> int:
    """
    Get the number of conversation messages in a session.

    Excludes 'system' event messages (connector/MCP/model changes) — they are UI-only
    markers, not turns, so they must not inflate a user-facing message count.

    Args:
        db: Database session
        session_id: UUID of the chat session

    Returns:
        Number of conversation messages
    """
    if isinstance(session_id, str):
        session_id = uuid.UUID(session_id)

    from sqlalchemy import func

    result = await db.execute(
        select(func.count(Message.id)).where(
            Message.session_id == session_id,
            Message.role != "system",
        )
    )
    return result.scalar_one()


async def update_session_summary(
    db: AsyncSession, session_id: str | uuid.UUID, summary: str
) -> None:
    """
    Update the session summary.

    Args:
        db: Database session
        session_id: UUID of the chat session
        summary: Summary text
    """
    if isinstance(session_id, str):
        session_id = uuid.UUID(session_id)

    await db.execute(update(Session).where(Session.id == session_id).values(summary=summary))
    await db.commit()
    logger.debug(f"Updated summary for session {session_id}")


async def stamp_message_usage(
    db: AsyncSession,
    message_id: uuid.UUID,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> None:
    """Stamp a turn's token totals onto an already-saved assistant message.

    CANONICAL bubble-fusion rationale (the orchestrators/tests reference this): the
    agent/opencode orchestrators persist assistant rows incrementally while streaming
    (before the terminal usage metadata exists), so the counts are written back onto
    the turn's FIRST assistant row once known — bubble fusion (frontend
    ``groupServerMessages``) keeps the first row's fields, so that row is what the
    per-bubble token line reads. Commits immediately (same granularity as
    save_message).
    """
    # Same clamp rationale as update_session_stats (engine-reported values).
    input_tokens = max(0, int(input_tokens))
    output_tokens = max(0, int(output_tokens))
    cache_read_tokens = max(0, int(cache_read_tokens))
    cache_write_tokens = max(0, int(cache_write_tokens))

    await db.execute(
        update(Message)
        .where(Message.id == message_id)
        .values(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            # Keep token_count coherent with save_message's fallback semantics.
            token_count=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
        )
    )
    await db.commit()


async def update_session_stats(
    db: AsyncSession,
    session_id: str | uuid.UUID,
    input_tokens: int = 0,
    output_tokens: int = 0,
    tool_calls: int = 0,
    iterations: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    commit: bool = True,
) -> None:
    """
    Update session usage statistics.

    Increments the session's cumulative stats.

    Args:
        db: Database session
        session_id: UUID of the chat session
        input_tokens: Number of input tokens to add
        output_tokens: Number of output tokens to add
        tool_calls: Number of tool calls to add
        iterations: Number of iterations to add
        cache_read_tokens: Prompt-cache read tokens to add (input-side)
        cache_write_tokens: Prompt-cache write tokens to add (input-side)
        commit: ``False`` joins the caller's transaction instead of self-committing —
            turn_usage folds the increment into the budget-row txn so its retry guard
            (the uq_usage_records_engine_turn backstop) covers the stats too; a
            self-committed increment could double-apply on an ambiguous-commit retry
    """
    if isinstance(session_id, str):
        session_id = uuid.UUID(session_id)

    # SECURITY: counts come from the sandboxed engine's stdout — clamp so a forged
    # negative can't walk the session totals backwards (same rationale as record_usage).
    input_tokens = max(0, int(input_tokens))
    output_tokens = max(0, int(output_tokens))
    cache_read_tokens = max(0, int(cache_read_tokens))
    cache_write_tokens = max(0, int(cache_write_tokens))

    # Use atomic increment to prevent race conditions
    await db.execute(
        update(Session)
        .where(Session.id == session_id)
        .values(
            total_input_tokens=Session.total_input_tokens + input_tokens,
            total_output_tokens=Session.total_output_tokens + output_tokens,
            total_cache_read_tokens=Session.total_cache_read_tokens + cache_read_tokens,
            total_cache_write_tokens=Session.total_cache_write_tokens + cache_write_tokens,
            total_tool_calls=Session.total_tool_calls + tool_calls,
            total_iterations=Session.total_iterations + iterations,
        )
    )
    if commit:
        await db.commit()
    logger.debug(
        f"Updated stats for session {session_id}: "
        f"+{input_tokens} in, +{output_tokens} out, +{tool_calls} tools, +{iterations} iters"
    )


async def get_session_stats(db: AsyncSession, session_id: str | uuid.UUID) -> dict[str, int]:
    """
    Get session usage statistics.

    Args:
        db: Database session
        session_id: UUID of the chat session

    Returns:
        Dictionary with token counts and other stats
    """
    if isinstance(session_id, str):
        session_id = uuid.UUID(session_id)

    result = await db.execute(select(Session).where(Session.id == session_id))
    session = result.scalar_one_or_none()

    if not session:
        return {
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_cache_read_tokens": 0,
            "total_cache_write_tokens": 0,
            "total_tool_calls": 0,
            "total_iterations": 0,
        }

    return {
        "total_input_tokens": session.total_input_tokens,
        "total_output_tokens": session.total_output_tokens,
        "total_cache_read_tokens": session.total_cache_read_tokens,
        "total_cache_write_tokens": session.total_cache_write_tokens,
        "total_tool_calls": session.total_tool_calls,
        "total_iterations": session.total_iterations,
    }


async def get_cli_session_id(db: AsyncSession, session_id: str | uuid.UUID) -> str | None:
    """
    Get the Claude CLI session ID for --resume functionality.

    Args:
        db: Database session
        session_id: UUID of the Inkwest session

    Returns:
        The CLI session ID if stored, None otherwise
    """
    if isinstance(session_id, str):
        session_id = uuid.UUID(session_id)

    result = await db.execute(select(Session.cli_session_id).where(Session.id == session_id))
    return result.scalar_one_or_none()


async def get_enabled_mcp_servers(db: AsyncSession, session_id: str | uuid.UUID) -> list[str]:
    """
    Get the catalog keys of MCP servers enabled for this conversation.

    Returns an empty list if the session is unknown or has no selection.
    """
    if isinstance(session_id, str):
        session_id = uuid.UUID(session_id)

    result = await db.execute(select(Session.enabled_mcp_servers).where(Session.id == session_id))
    return result.scalar_one_or_none() or []


async def set_cli_session_id(
    db: AsyncSession, session_id: str | uuid.UUID, cli_session_id: str
) -> None:
    """
    Store the Claude CLI session ID for --resume functionality.

    Args:
        db: Database session
        session_id: UUID of the Inkwest session
        cli_session_id: The CLI session ID from Claude CLI init event
    """
    if isinstance(session_id, str):
        session_id = uuid.UUID(session_id)

    await db.execute(
        update(Session).where(Session.id == session_id).values(cli_session_id=cli_session_id)
    )
    await db.commit()
    logger.debug(f"Stored CLI session ID {cli_session_id} for session {session_id}")


async def clear_cli_session_id(db: AsyncSession, session_id: str | uuid.UUID) -> None:
    """Forget a stale Claude CLI session ID so the next turn starts a fresh session.

    Used when ``--resume <id>`` fails because the transcript no longer exists in the
    toolbox (e.g. the sandbox was rebuilt) — the conversation recovers instead of
    erroring on every subsequent turn.
    """
    if isinstance(session_id, str):
        session_id = uuid.UUID(session_id)

    await db.execute(update(Session).where(Session.id == session_id).values(cli_session_id=None))
    await db.commit()
    logger.info(f"Cleared stale CLI session ID for session {session_id}")
