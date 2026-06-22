"""
Synthetic clock tool for the standalone agent.

The Converse loop has no native "what time is it" capability, and open-weight models
guess the date (emitting e.g. 2024 timestamps) instead of grounding it. This exposes a
single synthetic ``current_datetime`` tool returning the agent's wall clock (UTC), so the
model can anchor relative ranges and absolute timestamps on demand — the *accuracy* half
that complements the *reliability* of the date stated up front in the system prompt.

Mirrors the ``skills.py`` convention: the predicate + executor live here; the tool name is
owned by ``converse.py`` (which also builds the toolSpec), so the loop dispatches on it.
"""

from datetime import UTC, datetime

from .converse import CURRENT_DATETIME_TOOL


def is_datetime_tool(name: str) -> bool:
    """Whether a tool call targets the synthetic clock."""
    return name == CURRENT_DATETIME_TOOL


def current_datetime(_tool_input: dict[str, object]) -> tuple[str, bool]:
    """Return the current UTC datetime (ISO 8601, seconds). Takes no input; never errors."""
    return (datetime.now(UTC).isoformat(timespec="seconds"), False)
