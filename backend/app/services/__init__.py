"""
Services module for Inkwest.

Contains the core business logic including:
- AI client management
- Chat orchestration
- Message persistence
- Event streaming
"""

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
from app.services.orchestrator import (
    call_claude_with_retry,
    orchestrate_chat,
    orchestrate_chat_streaming,
)
from app.services.prompts import (
    get_summary_prompt,
    get_system_prompt,
    get_title_prompt,
)

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
