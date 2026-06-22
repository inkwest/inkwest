"""
Conversation-window shaping — pure, DB-free transcript helpers.

Carved out of ``messages.py`` (which is sqlalchemy/pubsub-laden persistence) so the
DB-free ``inkwest_agent`` loop can bound its transcript without dragging in the DB
engine and the web stack. The backend orchestrators use the same helper,
so the pairing logic lives in one place.
"""

from typing import Any


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
