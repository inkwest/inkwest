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


def prune_orphan_tool_calls(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop ``tool_use`` blocks with no matching ``tool_result`` (and vice versa).

    A turn interrupted *after* the assistant emitted a ``tool_use`` but *before*
    its ``tool_result`` was persisted leaves a dangling ``tool_use`` in history
    (e.g. the user sent a new message instead of letting the tool run). Replaying
    that history makes the Messages/Converse API reject the whole request:
    "tool_use ids were found without tool_result blocks immediately after". This
    is the documented false-negative gap in ``reconcile_interrupted_turns`` —
    streaming orchestrators persist the assistant ``tool_use`` row, so the turn
    doesn't end on a trailing ``user`` message and the startup sweep can't see it.

    Prunes at the block level so an assistant turn carrying both ``text`` and a
    dangling ``tool_use`` keeps its text. Symmetric on the other side so a lone
    ``tool_result`` can't orphan either. Pairing is global (by id), not strictly
    adjacent — the orchestrators only ever write a ``tool_result`` in the message
    right after its ``tool_use``, so global presence is equivalent and simpler.
    A message emptied entirely by pruning is dropped.
    """
    result_ids: set[str] = set()
    use_ids: set[str] = set()
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("id"):
                use_ids.add(block["id"])
            elif block.get("type") == "tool_result" and block.get("tool_use_id"):
                result_ids.add(block["tool_use_id"])

    pruned: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            pruned.append(msg)
            continue
        kept = [
            block
            for block in content
            if not (
                isinstance(block, dict)
                and (
                    (block.get("type") == "tool_use" and block.get("id") not in result_ids)
                    or (
                        block.get("type") == "tool_result"
                        and block.get("tool_use_id") not in use_ids
                    )
                )
            )
        ]
        if not kept:
            continue  # whole message was orphaned tool blocks
        pruned.append(msg if len(kept) == len(content) else {**msg, "content": kept})
    return pruned
