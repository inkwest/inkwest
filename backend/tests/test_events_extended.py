"""
Tests for extended chat events (interactive flow).
"""

import json

from app.services.events import (
    budget_warning_event,
    confirm_request_event,
    reasoning_event,
    search_proposal_event,
    search_results_event,
    status_update_event,
    tool_proposal_event,
)


class TestReasoningEvent:
    """Tests for reasoning events."""

    def test_reasoning_event_basic(self):
        """Should create basic reasoning event."""
        event = reasoning_event("I need to check the pod status first.")

        assert event.type == "reasoning"
        assert event.content == "I need to check the pod status first."

    def test_reasoning_event_with_step(self):
        """Should include step in metadata."""
        event = reasoning_event("Analyzing cluster state", step="analyzing")

        assert event.type == "reasoning"
        assert event.metadata["step"] == "analyzing"

    def test_reasoning_event_to_dict(self):
        """Should serialize to dict correctly."""
        event = reasoning_event("Planning next action", step="planning")
        d = event.to_dict()

        assert d["type"] == "reasoning"
        assert d["content"] == "Planning next action"
        assert d["metadata"]["step"] == "planning"


class TestToolProposalEvent:
    """Tests for tool proposal events."""

    def test_tool_proposal_event(self):
        """Should create tool proposal event."""
        event = tool_proposal_event(
            proposal_id="prop-123",
            tool_name="kubectl_get",
            tool_input={"resource": "pods"},
            reasoning="Need to check pod status",
            estimated_cost_cents=5,
        )

        assert event.type == "tool_proposal"
        assert event.tool_name == "kubectl_get"
        assert event.tool_input == {"resource": "pods"}
        assert event.tool_use_id == "prop-123"
        assert event.content == "Need to check pod status"
        assert event.metadata["awaiting_approval"] is True
        assert event.metadata["estimated_cost_cents"] == 5

    def test_tool_proposal_without_cost(self):
        """Should handle missing cost estimate."""
        event = tool_proposal_event(
            proposal_id="prop-456",
            tool_name="kubectl_describe",
            tool_input={"resource": "pod", "name": "my-pod"},
            reasoning="Need more details",
        )

        assert event.metadata["estimated_cost_cents"] is None

    def test_tool_proposal_to_json(self):
        """Should serialize to JSON correctly."""
        event = tool_proposal_event(
            proposal_id="prop-789",
            tool_name="kubectl_logs",
            tool_input={"pod_name": "app-1"},
            reasoning="Checking logs",
            estimated_cost_cents=10,
        )

        j = event.to_json()
        parsed = json.loads(j)

        assert parsed["type"] == "tool_proposal"
        assert parsed["tool_name"] == "kubectl_logs"
        assert parsed["metadata"]["awaiting_approval"] is True


class TestConfirmRequestEvent:
    """Tests for confirmation request events."""

    def test_confirm_request_basic(self):
        """Should create basic confirmation request."""
        event = confirm_request_event(
            request_id="req-123",
            request_type="cost",
            message="This operation may cost $0.50. Continue?",
        )

        assert event.type == "confirm_request"
        assert event.content == "This operation may cost $0.50. Continue?"
        assert event.metadata["request_id"] == "req-123"
        assert event.metadata["request_type"] == "cost"
        assert event.metadata["options"] == ["approve", "reject"]

    def test_confirm_request_with_options(self):
        """Should support custom options."""
        event = confirm_request_event(
            request_id="req-456",
            request_type="continue",
            message="Continue debugging?",
            options=["yes", "no", "pause"],
        )

        assert event.metadata["options"] == ["yes", "no", "pause"]

    def test_confirm_request_with_context(self):
        """Should include additional context."""
        event = confirm_request_event(
            request_id="req-789",
            request_type="source",
            message="Approve this documentation source?",
            context={"url": "https://kubernetes.io/docs/", "trust": "official"},
        )

        assert event.metadata["context"]["url"] == "https://kubernetes.io/docs/"
        assert event.metadata["context"]["trust"] == "official"


class TestStatusUpdateEvent:
    """Tests for status update events."""

    def test_status_update_event(self):
        """Should create status update event."""
        event = status_update_event(
            iteration=3,
            total_tokens=15000,
            total_cost_cents=50,
            tool_calls=5,
            message="Making progress...",
        )

        assert event.type == "status_update"
        assert event.content == "Making progress..."
        assert event.metadata["iteration"] == 3
        assert event.metadata["total_tokens"] == 15000
        assert event.metadata["total_cost_cents"] == 50
        assert event.metadata["tool_calls"] == 5

    def test_status_update_without_message(self):
        """Should handle missing message."""
        event = status_update_event(
            iteration=1,
            total_tokens=1000,
            total_cost_cents=5,
            tool_calls=1,
        )

        assert event.content is None
        assert event.metadata["iteration"] == 1


class TestBudgetWarningEvent:
    """Tests for budget warning events."""

    def test_budget_warning_soft_limit(self):
        """Should create soft limit warning."""
        event = budget_warning_event(
            usage_percent=85.0,
            remaining_tokens=15000,
            remaining_cost_cents=150,
            budget_name="daily-budget",
            is_hard_limit=False,
        )

        assert event.type == "budget_warning"
        assert "85.0%" in event.content
        assert event.is_error is False
        assert event.metadata["is_hard_limit"] is False
        assert event.metadata["remaining_tokens"] == 15000

    def test_budget_warning_hard_limit(self):
        """Should create hard limit error."""
        event = budget_warning_event(
            usage_percent=100.0,
            remaining_tokens=0,
            remaining_cost_cents=0,
            budget_name="user-budget",
            is_hard_limit=True,
        )

        assert event.type == "budget_warning"
        assert "limit reached" in event.content
        assert event.is_error is True
        assert event.metadata["is_hard_limit"] is True


class TestSearchProposalEvent:
    """Tests for search proposal events."""

    def test_search_proposal_event(self):
        """Should create search proposal event."""
        event = search_proposal_event(
            proposal_id="search-123",
            query="kubernetes pod crashloopbackoff troubleshooting",
            reasoning="Need to find documentation on this error",
            source_filter="official",
        )

        assert event.type == "search_proposal"
        assert event.content == "Need to find documentation on this error"
        assert event.metadata["proposal_id"] == "search-123"
        assert event.metadata["query"] == "kubernetes pod crashloopbackoff troubleshooting"
        assert event.metadata["source_filter"] == "official"
        assert event.metadata["awaiting_approval"] is True


class TestSearchResultsEvent:
    """Tests for search results events."""

    def test_search_results_event(self):
        """Should create search results event."""
        results = [
            {"title": "K8s Docs", "url": "https://kubernetes.io/", "trust": "official"},
            {"title": "SO Answer", "url": "https://stackoverflow.com/q/123", "trust": "trusted"},
        ]

        event = search_results_event(
            results=results,
            query="pod troubleshooting",
            requires_approval=True,
        )

        assert event.type == "search_results"
        assert event.metadata["query"] == "pod troubleshooting"
        assert event.metadata["result_count"] == 2
        assert event.metadata["requires_approval"] is True
        assert len(event.metadata["results"]) == 2

    def test_search_results_empty(self):
        """Should handle empty results."""
        event = search_results_event(
            results=[],
            query="obscure query",
        )

        assert event.metadata["result_count"] == 0
        assert event.metadata["requires_approval"] is False
