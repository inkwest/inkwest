"""
Chat event types for streaming responses.

These events are yielded by the orchestrator and streamed to the frontend
via WebSocket or Server-Sent Events.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Literal

EventType = Literal[
    # Content events
    "text_delta",  # Chunk of text from AI response
    "thinking",  # AI is thinking (optional, for streaming)
    # Tool events
    "tool_start",  # AI is executing a tool (after approval or auto-approved)
    "tool_result",  # Result from tool execution
    "tool_proposal",  # AI proposes to use a tool (awaiting approval)
    # Interactive events
    "reasoning",  # AI's reasoning about what to do next
    "confirm_request",  # Request user confirmation (cost, continue, source)
    "status_update",  # Progress update (iteration count, tokens, cost)
    "budget_warning",  # Approaching or exceeding budget limit
    # Session events
    "done",  # Response complete
    "error",  # Error occurred
    "session_info",  # Session metadata (e.g., new session created)
    "usage_stats",  # Internal accounting on a cut stream (consumed by the orchestrator)
    # Web search events
    "search_proposal",  # Proposed web search awaiting approval
    "search_results",  # Web search results for user review
    # Connector events
    "connector_required",  # A tool needs a connector that is not connected/expired
    "mcp_auth_required",  # An enabled OAuth2 MCP server has no active grant (reconnect)
]


@dataclass
class ChatEvent:
    """
    Event streamed to frontend during chat orchestration.

    Attributes:
        type: The type of event
        content: Text content (for text_delta, tool_result, error)
        tool_name: Name of the tool (for tool_start, tool_result)
        tool_input: Input parameters for tool (for tool_start)
        tool_use_id: Unique ID for tool use correlation
        is_error: Whether this represents an error condition
        metadata: Additional event-specific data
    """

    type: EventType
    content: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_use_id: str | None = None
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """
        Convert event to dictionary for JSON serialization.

        Returns:
            Dictionary representation of the event
        """
        result: dict[str, Any] = {
            "type": self.type,
        }

        if self.content is not None:
            result["content"] = self.content

        if self.tool_name is not None:
            result["tool_name"] = self.tool_name

        if self.tool_input is not None:
            result["tool_input"] = self.tool_input

        if self.tool_use_id is not None:
            result["tool_use_id"] = self.tool_use_id

        if self.is_error:
            result["is_error"] = True

        if self.metadata:
            result["metadata"] = self.metadata

        return result

    def to_json(self) -> str:
        """
        Convert event to JSON string.

        Returns:
            JSON string representation of the event
        """
        return json.dumps(self.to_dict())

    def to_sse(self) -> str:
        """
        Format event for Server-Sent Events.

        Returns:
            SSE-formatted string with event type and data
        """
        return f"event: {self.type}\ndata: {self.to_json()}\n\n"


# Factory functions for common events


def text_event(content: str) -> ChatEvent:
    """Create a text delta event."""
    return ChatEvent(type="text_delta", content=content)


def thinking_event(content: str) -> ChatEvent:
    """Create a thinking event for AI reasoning display."""
    return ChatEvent(type="thinking", content=content)


def tool_start_event(tool_name: str, tool_input: dict[str, Any], tool_use_id: str) -> ChatEvent:
    """Create a tool start event."""
    return ChatEvent(
        type="tool_start", tool_name=tool_name, tool_input=tool_input, tool_use_id=tool_use_id
    )


def tool_result_event(
    tool_name: str, content: str, tool_use_id: str, is_error: bool = False
) -> ChatEvent:
    """Create a tool result event."""
    return ChatEvent(
        type="tool_result",
        tool_name=tool_name,
        content=content,
        tool_use_id=tool_use_id,
        is_error=is_error,
    )


def done_event(metadata: dict[str, Any] | None = None, stopped: bool = False) -> ChatEvent:
    """Create a done event."""
    m = metadata or {}
    if stopped:
        m["stopped"] = True
    return ChatEvent(type="done", metadata=m)


def error_event(message: str, metadata: dict[str, Any] | None = None) -> ChatEvent:
    """Create an error event."""
    return ChatEvent(type="error", content=message, is_error=True, metadata=metadata or {})


def session_info_event(session_id: str, title: str | None = None, **kwargs: Any) -> ChatEvent:
    """Create a session info event."""
    return ChatEvent(
        type="session_info", metadata={"session_id": session_id, "title": title, **kwargs}
    )


# =============================================================================
# Interactive Flow Events
# =============================================================================


def reasoning_event(content: str, step: str | None = None) -> ChatEvent:
    """
    Create a reasoning event showing AI's thought process.

    Args:
        content: The reasoning explanation
        step: Optional step name (e.g., "analyzing", "planning", "deciding")
    """
    return ChatEvent(type="reasoning", content=content, metadata={"step": step} if step else {})


def tool_proposal_event(
    proposal_id: str,
    tool_name: str,
    tool_input: dict[str, Any],
    reasoning: str,
    estimated_cost_cents: int | None = None,
) -> ChatEvent:
    """
    Create a tool proposal event awaiting user approval.

    Args:
        proposal_id: Unique ID for this proposal
        tool_name: Name of the proposed tool
        tool_input: Proposed input parameters
        reasoning: Why the AI wants to use this tool
        estimated_cost_cents: Estimated cost for this operation
    """
    return ChatEvent(
        type="tool_proposal",
        tool_name=tool_name,
        tool_input=tool_input,
        tool_use_id=proposal_id,
        content=reasoning,
        metadata={
            "awaiting_approval": True,
            "estimated_cost_cents": estimated_cost_cents,
        },
    )


def confirm_request_event(
    request_id: str,
    request_type: str,
    message: str,
    options: list[str] | None = None,
    context: dict[str, Any] | None = None,
) -> ChatEvent:
    """
    Create a confirmation request event.

    Args:
        request_id: Unique ID for this request
        request_type: Type of confirmation (cost, continue, source, etc.)
        message: Message to display to user
        options: Available response options (e.g., ["approve", "reject", "modify"])
        context: Additional context for the request
    """
    return ChatEvent(
        type="confirm_request",
        content=message,
        metadata={
            "request_id": request_id,
            "request_type": request_type,
            "options": options or ["approve", "reject"],
            "context": context or {},
        },
    )


def status_update_event(
    iteration: int,
    total_tokens: int,
    total_cost_cents: int,
    tool_calls: int,
    message: str | None = None,
) -> ChatEvent:
    """
    Create a status update event.

    Args:
        iteration: Current iteration number
        total_tokens: Total tokens used so far
        total_cost_cents: Total cost in cents so far
        tool_calls: Number of tool calls made
        message: Optional status message
    """
    return ChatEvent(
        type="status_update",
        content=message,
        metadata={
            "iteration": iteration,
            "total_tokens": total_tokens,
            "total_cost_cents": total_cost_cents,
            "tool_calls": tool_calls,
        },
    )


def budget_warning_event(
    usage_percent: float,
    remaining_tokens: int | None,
    remaining_cost_cents: int | None,
    budget_name: str,
    is_hard_limit: bool = False,
) -> ChatEvent:
    """
    Create a budget warning event.

    Args:
        usage_percent: Percentage of budget used
        remaining_tokens: Remaining token allowance
        remaining_cost_cents: Remaining cost allowance in cents
        budget_name: Name of the applicable budget
        is_hard_limit: Whether this is a hard limit (blocking)
    """
    message = f"Budget '{budget_name}': {usage_percent:.1f}% used"
    if is_hard_limit:
        message = f"Budget limit reached for '{budget_name}'"

    return ChatEvent(
        type="budget_warning",
        content=message,
        is_error=is_hard_limit,
        metadata={
            "usage_percent": usage_percent,
            "remaining_tokens": remaining_tokens,
            "remaining_cost_cents": remaining_cost_cents,
            "budget_name": budget_name,
            "is_hard_limit": is_hard_limit,
        },
    )


def search_proposal_event(
    proposal_id: str,
    query: str,
    reasoning: str,
    source_filter: str = "all",
) -> ChatEvent:
    """
    Create a web search proposal event awaiting approval.

    Args:
        proposal_id: Unique ID for this proposal
        query: Proposed search query
        reasoning: Why the AI wants to search
        source_filter: Filter type (official, community, all)
    """
    return ChatEvent(
        type="search_proposal",
        content=reasoning,
        metadata={
            "proposal_id": proposal_id,
            "query": query,
            "source_filter": source_filter,
            "awaiting_approval": True,
        },
    )


def search_results_event(
    results: list[dict[str, Any]],
    query: str,
    requires_approval: bool = False,
) -> ChatEvent:
    """
    Create a web search results event.

    Args:
        results: List of search result dicts
        query: The search query used
        requires_approval: Whether user must approve before AI sees results
    """
    return ChatEvent(
        type="search_results",
        metadata={
            "query": query,
            "results": results,
            "result_count": len(results),
            "requires_approval": requires_approval,
        },
    )


def connector_required_event(connector: str, session_id: str) -> ChatEvent:
    """Signal that a tool needs a connector the session has not granted (or it expired).

    Lets the UI render a first-class "Give access" button instead of relying on the
    model to ask in prose.
    """
    return ChatEvent(
        type="connector_required",
        content=f"Access to {connector} is required for this action.",
        metadata={"connector": connector, "session_id": session_id},
    )


def mcp_auth_required_event(mcp_key: str, session_id: str) -> ChatEvent:
    """Signal that an enabled OAuth2 MCP server has no active grant — the user must reconnect.

    Emitted when reconciling ``.claude.json`` skips a selected oauth server because its grant
    is missing/expired/revoked, so the UI shows a "Reconnect" banner instead of the tools
    silently vanishing.
    """
    return ChatEvent(
        type="mcp_auth_required",
        content=f"MCP server {mcp_key} needs reconnecting.",
        metadata={"mcp_key": mcp_key, "session_id": session_id},
    )


def cli_session_event(cli_session_id: str) -> ChatEvent:
    """
    Create an event with the Claude CLI session ID for --resume.

    Args:
        cli_session_id: The session ID from Claude CLI init event
    """
    return ChatEvent(
        type="session_info",
        metadata={
            "cli_session_id": cli_session_id,
        },
    )
