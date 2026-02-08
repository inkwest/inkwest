"""
Message persistence helpers for chat sessions.

Handles loading and saving messages to the database,
and managing session metadata like titles.
"""

import logging
import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message import Message
from app.models.session import Session
from app.services.pubsub import notify

logger = logging.getLogger(__name__)


def truncate_messages(messages: list[dict[str, Any]], max_messages: int) -> list[dict[str, Any]]:
    """Truncate messages while preserving tool_use/tool_result pairs.

    Naive slicing can orphan a tool_use without its result (or vice versa),
    which causes API validation errors. This helper finds a safe cut point.
    """
    if len(messages) <= max_messages:
        return messages

    truncated = messages[-max_messages:]

    # If the first message is a tool_result (role=user with tool_result content),
    # it's orphaned — drop messages until we find a non-orphan boundary.
    while truncated and _is_tool_result_message(truncated[0]):
        truncated = truncated[1:]

    return truncated


def _is_tool_result_message(msg: dict[str, Any]) -> bool:
    if msg.get("role") == "tool":
        return True
    content = msg.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and first.get("type") == "tool_result":
            return True
    return False


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
    messages = result.scalars().all()

    formatted_messages = []
    for msg in messages:
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
    thinking_content: str | None = None,
    latency_ms: int | None = None,
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
        token_count: Number of tokens in the message (deprecated, use output_tokens)
        input_tokens: Number of input tokens for this API call
        output_tokens: Number of output tokens for this API call
        thinking_content: AI reasoning/thinking content if extended thinking enabled
        latency_ms: API call latency in milliseconds

    Returns:
        Created Message object
    """
    if isinstance(session_id, str):
        session_id = uuid.UUID(session_id)

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
        token_count=token_count or output_tokens,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        thinking_content=thinking_content,
        latency_ms=latency_ms,
    )
    db.add(message)
    await db.commit()
    await db.refresh(message)

    logger.debug(f"Saved {role} message {message.id} to session {session_id}")
    return message


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

    await db.execute(update(Session).where(Session.id == session_id).values(title=title))
    await db.commit()
    logger.debug(f"Updated title for session {session_id}: {title}")

    await notify({"type": "session_updated", "session_id": str(session_id)})


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
    Get the number of messages in a session.

    Args:
        db: Database session
        session_id: UUID of the chat session

    Returns:
        Number of messages
    """
    if isinstance(session_id, str):
        session_id = uuid.UUID(session_id)

    from sqlalchemy import func

    result = await db.execute(
        select(func.count(Message.id)).where(Message.session_id == session_id)
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


async def update_session_stats(
    db: AsyncSession,
    session_id: str | uuid.UUID,
    input_tokens: int = 0,
    output_tokens: int = 0,
    tool_calls: int = 0,
    iterations: int = 0,
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
    """
    if isinstance(session_id, str):
        session_id = uuid.UUID(session_id)

    # Use atomic increment to prevent race conditions
    await db.execute(
        update(Session)
        .where(Session.id == session_id)
        .values(
            total_input_tokens=Session.total_input_tokens + input_tokens,
            total_output_tokens=Session.total_output_tokens + output_tokens,
            total_tool_calls=Session.total_tool_calls + tool_calls,
            total_iterations=Session.total_iterations + iterations,
        )
    )
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
            "total_tool_calls": 0,
            "total_iterations": 0,
        }

    return {
        "total_input_tokens": session.total_input_tokens,
        "total_output_tokens": session.total_output_tokens,
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
