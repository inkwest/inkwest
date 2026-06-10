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
from app.services.ai_client import (
    AIClientError,
    get_ai_client,
    get_cached_ai_client,
    get_model_name,
    validate_ai_client,
)
from app.services.events import (
    ChatEvent,
    done_event,
    error_event,
    session_info_event,
    text_event,
    tool_result_event,
    tool_start_event,
)
from app.services.messages import (
    ensure_session_title,
    get_message_count,
    get_session_messages,
    save_message,
    update_session_summary,
    update_session_title,
)
from app.services.prompts import (
    ensure_default_system_prompt,
    get_summary_prompt,
    get_system_prompt,
    get_title_prompt,
    resolve_session_system_prompt,
)

# orchestrator is re-exported lazily (PEP 562): it imports the tools layer
# (app.tools.dispatch -> connector_exec), which in turn imports back into
# app.services.connectors. Eager re-export here ran app.services.__init__ while
# connector_exec was still initializing, so importing any app.services.* submodule
# (e.g. connectors.base) tripped a circular import. Deferring it to first access
# keeps the public `from app.services import orchestrate_chat` surface intact.
_LAZY_ORCHESTRATOR: dict[str, str] = {
    "call_claude_with_retry": "orchestrator",
    "orchestrate_chat": "orchestrator",
    "orchestrate_chat_streaming": "orchestrator",
}

if TYPE_CHECKING:  # static analysers still see the full surface
    from app.services.orchestrator import (
        call_claude_with_retry,
        orchestrate_chat,
        orchestrate_chat_streaming,
    )


def __getattr__(name: str) -> Any:
    """Lazily import and cache the orchestrator re-exports on first access."""
    module_name = _LAZY_ORCHESTRATOR.get(name)
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
    "orchestrate_chat_streaming",
    "call_claude_with_retry",
    # Constants (re-exported for backward compatibility)
    "MAX_ORCHESTRATION_ITERATIONS",
]
