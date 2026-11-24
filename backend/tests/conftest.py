"""
Pytest fixtures for Inkwest tests.

Provides mocks for:
- AI client (Claude API)
- Database session
- Kubectl executor
- API route testing (users, clients)
"""

import os
import time
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

# The suite assumes a deployment with BOTH agentic engines enabled (e.g. test_api_config
# asserts engines == ["claude-cli", "inkwest-agent"]; engine-pin + dispatch tests need the
# agent runnable). Pin it here so tests are hermetic — FORCE it rather than setdefault: a dev
# runner (e.g. running pytest inside the app container) may export an ambient AGENTIC_ENGINES
# with experimental values (`opencode`), which would leak in and make validate_settings raise
# "unknown engine". A test needing a custom value still overrides per-test via monkeypatch.
# Runs before any app import — the app (and its cached Settings) is imported lazily inside
# fixtures, never at this module's top.
os.environ["AGENTIC_ENGINES"] = "claude-cli,inkwest-agent"

# Same hermeticity concern for the other ambient settings the suite asserts defaults on.
# Running pytest inside the app container inherits the deployment env (OIDC on, a shared
# sandbox creds volume), which flips code paths the tests were written against: dev-mode
# auth (OIDC off → auto dev user) and backend-local kubectl paths (no sandbox projection).
# Force the CI/host defaults; a test needing the sandbox/OIDC path overrides via monkeypatch.
os.environ["OIDC_ENABLED"] = "false"
# Pin the code defaults so a local backend/.env (the dev example turns the sandbox off) never
# changes what the suite runs against.
os.environ["SANDBOX_ENABLED"] = "true"
os.environ["LLM_BROKER_ENABLED"] = "true"
os.environ.pop("SANDBOX_CREDS_ROOT", None)

# Disable the per-IP HTTP rate limiter for the suite: API tests fire many requests from a
# single client identity and would otherwise trip 429s that have nothing to do with what they
# assert. The limiter itself is covered directly in test_rate_limit.py (which builds its own
# app with it enabled). `setdefault` lets an explicit env re-enable it. Production default
# stays enabled (config.py).
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

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


@pytest.fixture(autouse=True)
def _frozen_model_catalog():
    """No test may trigger a live catalog fetch (network call / aws subprocess).

    Marking the cache fresh turns refresh_catalog() into a no-op; the catalog
    unit tests bypass it with force=True and patch the fetchers themselves.
    Module-level cache state is reset so it cannot leak between tests.
    """
    from app.services import model_catalog

    model_catalog._live_models = None
    model_catalog._fetched_at = time.monotonic()
    model_catalog._fetched_wall = None
    model_catalog._last_fetch_failed = False
    yield
    model_catalog._live_models = None
    model_catalog._fetched_at = 0.0
    model_catalog._fetched_wall = None
    model_catalog._last_fetch_failed = False


def empty_execute_result() -> MagicMock:
    """An ``execute()`` result that yields no rows, for whatever shape the caller reads.

    Without it a bare ``AsyncMock`` returns a MagicMock whose ``.scalars()`` is not
    iterable, so every query in the code under test raises — and a caller that degrades
    gracefully on failure (``with_compaction_summary``) then silently exercises only its
    fail-open path.
    """
    result = MagicMock()
    result.scalars.return_value = []
    result.scalar_one_or_none.return_value = None
    result.all.return_value = []
    return result


def mock_session(**overrides: Any) -> AsyncMock:
    """A mock DB session whose queries return nothing instead of raising."""
    db = AsyncMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock(return_value=empty_execute_result())
    db.scalar = AsyncMock(return_value=None)
    for name, value in overrides.items():
        setattr(db, name, value)
    return db


@pytest.fixture
def mock_db() -> AsyncMock:
    """Mock async database session."""
    db = AsyncMock(spec=AsyncSession)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.close = AsyncMock()
    # Default to "nothing found" so helpers like resolve_session_system_prompt
    # fall back cleanly; tests that need a row override these explicitly.
    db.get = AsyncMock(return_value=None)
    db.scalar = AsyncMock(return_value=None)
    db.execute = AsyncMock(return_value=empty_execute_result())
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
_SKILL_ID = uuid.UUID("00000000-0000-4000-8000-000000000030")


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
def mock_skill() -> MagicMock:
    """Mock Skill with all SkillResponse fields."""
    template = MagicMock()
    template.id = _SKILL_ID
    template.name = "test-template"
    template.content = (
        "---\nname: test-template\ndescription: A test skill\narguments: [target]\n---\n"
        "Investigate $ARGUMENTS.\n"
    )
    template.description = "A test skill"
    template.is_active = True
    template.created_at = datetime.now(UTC)
    template.updated_at = datetime.now(UTC)
    return template


_SYSTEM_PROMPT_ID = uuid.UUID("00000000-0000-4000-8000-000000000040")


@pytest.fixture
def mock_system_prompt() -> MagicMock:
    """Mock SystemPrompt with all SystemPromptResponse fields (non-default)."""
    prompt = MagicMock()
    prompt.id = _SYSTEM_PROMPT_ID
    prompt.name = "test-system-prompt"
    prompt.content = "You are a test assistant for {cluster_name}."
    prompt.description = "A test system prompt"
    prompt.is_active = True
    prompt.is_default = False
    prompt.created_at = datetime.now(UTC)
    prompt.updated_at = datetime.now(UTC)
    return prompt


@pytest.fixture
def mock_session_obj(mock_cluster: MagicMock) -> MagicMock:
    """Mock Session with all fields."""
    session = MagicMock()
    session.id = _SESSION_ID
    session.cluster_id = _CLUSTER_ID
    session.user_id = str(_USER_ID)
    session.system_prompt_id = None
    session.system_prompt_custom = None
    session.model = None
    session.engine = None
    session.title = "Test Session"
    session.summary = "A test summary"
    session.is_archived = False
    session.total_input_tokens = 100
    session.total_output_tokens = 50
    session.total_cache_read_tokens = 0
    session.total_cache_write_tokens = 0
    session.total_tool_calls = 2
    session.total_iterations = 1
    session.created_at = datetime.now(UTC)
    session.updated_at = datetime.now(UTC)
    session.cluster = mock_cluster
    session.messages = []
    # Collaboration markers (SessionResponse): a MagicMock otherwise yields child
    # mocks that fail bool/str validation. Real ORM rows lack these attrs and fall
    # back to the schema defaults; the routes set them explicitly per caller.
    session.is_owner = True
    session.shared_role = None
    session.owner_display_name = None
    session.share_count = 0
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
