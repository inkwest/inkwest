"""
Pytest fixtures for Inkwest tests.

Provides mocks for:
- AI client (Claude API)
- Database session
- Kubectl executor
- API route testing (users, clients)
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

# ============================================================================
# Mock Claude API Response Builders
# ============================================================================


def make_text_block(text: str) -> MagicMock:
    """Create a mock text content block."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def make_tool_use_block(tool_id: str, name: str, input_data: dict[str, Any]) -> MagicMock:
    """Create a mock tool_use content block."""
    block = MagicMock()
    block.type = "tool_use"
    block.id = tool_id
    block.name = name
    block.input = input_data
    return block


def make_usage(input_tokens: int = 100, output_tokens: int = 50) -> MagicMock:
    """Create a mock usage object."""
    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens
    return usage


def make_response(
    content: list[MagicMock],
    stop_reason: str = "end_turn",
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> MagicMock:
    """Create a mock Claude API response."""
    response = MagicMock()
    response.content = content
    response.stop_reason = stop_reason
    response.usage = make_usage(input_tokens, output_tokens)
    return response


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def mock_db() -> AsyncMock:
    """Mock async database session."""
    db = AsyncMock(spec=AsyncSession)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.close = AsyncMock()
    return db


@pytest.fixture
def mock_ai_client() -> MagicMock:
    """Mock AsyncAnthropic client."""
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock()
    client.messages.stream = MagicMock()
    return client


@pytest.fixture
def session_id() -> uuid.UUID:
    """Generate a test session UUID."""
    return uuid.uuid4()


@pytest.fixture
def cluster_context() -> dict[str, Any]:
    """Standard cluster context for tests."""
    return {
        "cluster_name": "test-cluster",
        "kubeconfig_path": "/tmp/test-kubeconfig",
        "context_name": "test-context",
        "namespaces": ["default", "kube-system"],
    }


# ============================================================================
# AI Response Scenarios
# ============================================================================


@pytest.fixture
def simple_text_response() -> MagicMock:
    """Claude response with just text, no tool use."""
    return make_response(
        content=[make_text_block("Here's a summary of your cluster.")], stop_reason="end_turn"
    )


@pytest.fixture
def single_tool_response() -> tuple[MagicMock, MagicMock]:
    """Claude response requesting one tool, then finishing."""
    tool_response = make_response(
        content=[
            make_text_block("Let me check the pods."),
            make_tool_use_block(
                "toolu_01", "kubectl_get", {"resource": "pods", "namespace": "default"}
            ),
        ],
        stop_reason="tool_use",
    )

    final_response = make_response(
        content=[make_text_block("I found 3 running pods in the default namespace.")],
        stop_reason="end_turn",
    )

    return tool_response, final_response


@pytest.fixture
def multi_tool_response() -> tuple[MagicMock, MagicMock, MagicMock]:
    """Claude response requesting multiple tools in sequence."""
    first_response = make_response(
        content=[
            make_text_block("Let me check the pods."),
            make_tool_use_block(
                "toolu_01", "kubectl_get", {"resource": "pods", "namespace": "default"}
            ),
        ],
        stop_reason="tool_use",
    )

    second_response = make_response(
        content=[
            make_text_block("Now let me check the events."),
            make_tool_use_block("toolu_02", "kubectl_events", {"namespace": "default"}),
        ],
        stop_reason="tool_use",
    )

    final_response = make_response(
        content=[make_text_block("Analysis complete. No issues found.")], stop_reason="end_turn"
    )

    return first_response, second_response, final_response


@pytest.fixture
def parallel_tools_response() -> tuple[MagicMock, MagicMock]:
    """Claude response requesting multiple tools in parallel."""
    tool_response = make_response(
        content=[
            make_text_block("Let me gather information."),
            make_tool_use_block(
                "toolu_01", "kubectl_get", {"resource": "pods", "namespace": "default"}
            ),
            make_tool_use_block(
                "toolu_02", "kubectl_get", {"resource": "services", "namespace": "default"}
            ),
        ],
        stop_reason="tool_use",
    )

    final_response = make_response(
        content=[make_text_block("Found 3 pods and 2 services.")], stop_reason="end_turn"
    )

    return tool_response, final_response


# ============================================================================
# Mock Helpers
# ============================================================================


@pytest.fixture
def mock_kubectl_success():
    """Patch execute_kubectl to return success.

    Orchestrators route tool calls through app.tools.dispatch.execute_tool, which
    dispatches kubectl tools to execute_kubectl — so we patch it there.
    """
    with patch("app.tools.dispatch.execute_kubectl") as mock:
        mock.return_value = ("NAME  READY  STATUS\npod1  1/1    Running", False)
        yield mock


@pytest.fixture
def mock_kubectl_error():
    """Patch execute_kubectl to return error."""
    with patch("app.tools.dispatch.execute_kubectl") as mock:
        mock.return_value = ("Error: pods not found", True)
        yield mock


@pytest.fixture
def mock_messages_empty():
    """Patch get_session_messages to return empty list."""
    with patch("app.services.orchestrator.get_session_messages") as mock:
        mock.return_value = []
        yield mock


@pytest.fixture
def mock_save_message():
    """Patch save_message to do nothing."""
    with patch("app.services.orchestrator.save_message") as mock:
        mock.return_value = None
        yield mock


@pytest.fixture
def mock_ensure_title():
    """Patch ensure_session_title to return a title."""
    with patch("app.services.orchestrator.ensure_session_title") as mock:
        mock.return_value = "Test Session"
        yield mock


# ============================================================================
# API Route Test Fixtures
# ============================================================================

_USER_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
_ADMIN_ID = uuid.UUID("00000000-0000-4000-8000-000000000002")
_CLUSTER_ID = uuid.UUID("00000000-0000-4000-8000-000000000010")
_SESSION_ID = uuid.UUID("00000000-0000-4000-8000-000000000020")
_PROMPT_TEMPLATE_ID = uuid.UUID("00000000-0000-4000-8000-000000000030")


def _make_user(
    user_id: uuid.UUID,
    email: str,
    roles: list[str],
    display_name: str = "Test User",
) -> MagicMock:
    user = MagicMock()
    user.id = user_id
    user.email = email
    user.display_name = display_name
    user.roles = roles
    user.oidc_subject = f"oidc-{email}"
    user.oidc_issuer = "local"
    user.is_active = True
    user.last_login = datetime.now(UTC)
    user.created_at = datetime.now(UTC)
    user.updated_at = datetime.now(UTC)
    user.is_admin = lambda: "admin" in roles
    user.has_role = lambda r: r in roles
    return user


@pytest.fixture
def mock_user() -> MagicMock:
    """Non-admin user mock."""
    return _make_user(_USER_ID, "user@test.com", ["user"], "Regular User")


@pytest.fixture
def mock_admin_user() -> MagicMock:
    """Admin user mock."""
    return _make_user(_ADMIN_ID, "admin@test.com", ["admin"], "Admin User")


@pytest.fixture
def mock_cluster() -> MagicMock:
    """Mock Cluster with all ClusterResponse fields."""
    cluster = MagicMock()
    cluster.id = _CLUSTER_ID
    cluster.name = "test-cluster"
    cluster.display_name = "Test Cluster"
    cluster.kubeconfig_path = "/tmp/kubeconfig"
    cluster.context_name = "test-ctx"
    cluster.description = "A test cluster"
    cluster.is_active = True
    cluster.created_at = datetime.now(UTC)
    cluster.updated_at = datetime.now(UTC)
    return cluster


@pytest.fixture
def mock_prompt_template() -> MagicMock:
    """Mock PromptTemplate with all PromptTemplateResponse fields."""
    template = MagicMock()
    template.id = _PROMPT_TEMPLATE_ID
    template.name = "test-template"
    template.content = "Investigate pod __________ in namespace __________."
    template.description = "A test template"
    template.is_active = True
    template.created_at = datetime.now(UTC)
    template.updated_at = datetime.now(UTC)
    return template


@pytest.fixture
def mock_session_obj(mock_cluster: MagicMock) -> MagicMock:
    """Mock Session with all fields."""
    session = MagicMock()
    session.id = _SESSION_ID
    session.cluster_id = _CLUSTER_ID
    session.user_id = str(_USER_ID)
    session.title = "Test Session"
    session.summary = "A test summary"
    session.is_archived = False
    session.total_input_tokens = 100
    session.total_output_tokens = 50
    session.total_tool_calls = 2
    session.total_iterations = 1
    session.created_at = datetime.now(UTC)
    session.updated_at = datetime.now(UTC)
    session.cluster = mock_cluster
    session.messages = []
    return session


@pytest.fixture
async def authenticated_client(
    mock_user: MagicMock, mock_db: AsyncMock
) -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP client with regular-user auth and mocked DB."""
    from app.api.dependencies import get_current_user, get_db
    from app.main import app

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_current_user] = lambda: mock_user
    app.dependency_overrides[get_db] = lambda: mock_db

    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)


@pytest.fixture
async def admin_client(
    mock_admin_user: MagicMock, mock_db: AsyncMock
) -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP client with admin auth and mocked DB."""
    from app.api.dependencies import get_current_user, get_db, require_admin
    from app.main import app

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_current_user] = lambda: mock_admin_user
    app.dependency_overrides[require_admin] = lambda: mock_admin_user
    app.dependency_overrides[get_db] = lambda: mock_db

    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)
