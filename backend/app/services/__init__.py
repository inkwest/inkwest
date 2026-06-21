"""
Services module for Inkwest.

Contains the core business logic including:
- AI client management
- Chat orchestration
- Message persistence
- Event streaming
"""

from typing import TYPE_CHECKING, Any

from app.core.constants import MAX_ORCHESTRATION_ITERATIONS
from app.services.events import (
    ChatEvent,
    done_event,
    error_event,
    session_info_event,
    text_event,
    tool_result_event,
    tool_start_event,
)

# Heavy re-exports are deferred (PEP 562) so importing ANY app.services.* submodule
# doesn't drag the DB engine and web stack in through this package __init__. The
# standalone inkwest_agent imports app.services.events / message_window / tool_execution
# (all DB-free); eager re-exports here used to pull ai_client (anthropic), messages
# (sqlalchemy/asyncpg via pubsub→database) and prompts on every such import, defeating
# the carve-out. orchestrator was already lazy for a circular-import reason
# (it imports the tools layer, which imports back into app.services.connectors). events +
# constants stay eager: pure, no third-party deps. The public `from app.services import X`
# surface is unchanged — names resolve through __getattr__ on first access.
_LAZY_REEXPORTS: dict[str, str] = {
    # orchestrator (also breaks the connector_exec circular import — keep deferred)
    "call_claude_with_retry": "orchestrator",
    "orchestrate_chat": "orchestrator",
    # ai_client (pulls anthropic)
    "AIClientError": "ai_client",
    "get_ai_client": "ai_client",
    "get_cached_ai_client": "ai_client",
    "get_model_name": "ai_client",
    "validate_ai_client": "ai_client",
    # messages (pulls sqlalchemy/asyncpg via pubsub→database, fastapi via core.logging)
    "ensure_session_title": "messages",
    "get_message_count": "messages",
    "get_session_messages": "messages",
    "save_message": "messages",
    "update_session_summary": "messages",
    "update_session_title": "messages",
    # prompts
    "ensure_default_system_prompt": "prompts",
    "get_summary_prompt": "prompts",
    "get_system_prompt": "prompts",
    "get_title_prompt": "prompts",
    "resolve_session_system_prompt": "prompts",
}

if TYPE_CHECKING:  # static analysers still see the full surface
    from app.services.ai_client import (
        AIClientError,
        get_ai_client,
        get_cached_ai_client,
        get_model_name,
        validate_ai_client,
    )
    from app.services.messages import (
        ensure_session_title,
        get_message_count,
        get_session_messages,
        save_message,
        update_session_summary,
        update_session_title,
    )
    from app.services.orchestrator import (
        call_claude_with_retry,
        orchestrate_chat,
    )
    from app.services.prompts import (
        ensure_default_system_prompt,
        get_summary_prompt,
        get_system_prompt,
        get_title_prompt,
        resolve_session_system_prompt,
    )


def __getattr__(name: str) -> Any:
    """Lazily import and cache the deferred re-exports on first access."""
    module_name = _LAZY_REEXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    import importlib

    module = importlib.import_module(f".{module_name}", __name__)
    value = getattr(module, name)
    globals()[name] = value  # cache so subsequent lookups skip __getattr__
    return value


__all__ = [
    # AI Client
    "get_ai_client",
    "get_cached_ai_client",
    "validate_ai_client",
    "get_model_name",
    "AIClientError",
    # Prompts
    "get_system_prompt",
    "get_summary_prompt",
    "get_title_prompt",
    "ensure_default_system_prompt",
    "resolve_session_system_prompt",
    # Events
    "ChatEvent",
    "text_event",
    "tool_start_event",
    "tool_result_event",
    "done_event",
    "error_event",
    "session_info_event",
    # Messages
    "get_session_messages",
    "save_message",
    "update_session_title",
    "ensure_session_title",
    "get_message_count",
    "update_session_summary",
    # Orchestrator
    "orchestrate_chat",
    "call_claude_with_retry",
    # Constants (re-exported for backward compatibility)
    "MAX_ORCHESTRATION_ITERATIONS",
]
