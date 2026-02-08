"""
Tests for the MCP client service.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.mcp_client import (
    MCPClient,
    MCPServerConfig,
    MCPServerType,
    MCPToolCall,
    create_aws_mcp_config,
    create_github_mcp_config,
    create_gitlab_mcp_config,
    create_prometheus_mcp_config,
)


class TestMCPServerConfig:
    """Tests for MCP server configuration."""

    def test_default_values(self):
        """Should have sensible defaults."""
        config = MCPServerConfig(
            name="test",
            server_type=MCPServerType.CUSTOM,
            endpoint="http://localhost:3000",
        )

        assert config.enabled is True
        assert config.read_only is True
        assert config.timeout == 30
        assert config.allowed_tools == []

    def test_aws_config_template(self):
        """Should create AWS config with correct defaults."""
        config = create_aws_mcp_config(
            endpoint="http://localhost:3000",
            region="us-west-2",
        )

        assert config.name == "aws"
        assert config.server_type == MCPServerType.AWS
        assert config.read_only is True
        assert config.metadata["region"] == "us-west-2"
        assert "cloudwatch_get_log_events" in config.allowed_tools
        assert "eks_describe_cluster" in config.allowed_tools

    def test_gitlab_config_template(self):
        """Should create GitLab config with correct defaults."""
        config = create_gitlab_mcp_config(
            endpoint="http://localhost:3001",
            gitlab_url="https://gitlab.company.com",
        )

        assert config.name == "gitlab"
        assert config.server_type == MCPServerType.GITLAB
        assert config.read_only is True
        assert config.metadata["gitlab_url"] == "https://gitlab.company.com"
        assert "gitlab_get_pipeline" in config.allowed_tools
        assert "gitlab_list_merge_requests" in config.allowed_tools

    def test_github_config_template(self):
        """Should create GitHub config with correct defaults."""
        config = create_github_mcp_config(endpoint="http://localhost:3002")

        assert config.name == "github"
        assert config.server_type == MCPServerType.GITHUB
        assert "github_get_file_contents" in config.allowed_tools
        assert "github_list_workflow_runs" in config.allowed_tools

    def test_prometheus_config_template(self):
        """Should create Prometheus config with correct defaults."""
        config = create_prometheus_mcp_config(
            endpoint="http://localhost:3003",
            prometheus_url="http://prometheus:9090",
        )

        assert config.name == "prometheus"
        assert config.server_type == MCPServerType.PROMETHEUS
        assert "prometheus_query" in config.allowed_tools
        assert "prometheus_list_alerts" in config.allowed_tools


class TestMCPClient:
    """Tests for MCP client."""

    @pytest.fixture
    def client(self):
        """Create a fresh MCP client."""
        return MCPClient()

    @pytest.fixture
    def sample_config(self):
        """Create a sample server config."""
        return MCPServerConfig(
            name="test-server",
            server_type=MCPServerType.CUSTOM,
            endpoint="http://localhost:3000",
            api_key="test-key",
            enabled=True,
        )

    def test_register_server(self, client, sample_config):
        """Should register a server."""
        client.register_server(sample_config)

        assert "test-server" in [s.name for s in client.list_servers()]
        assert client.get_server("test-server") == sample_config

    def test_unregister_server(self, client, sample_config):
        """Should unregister a server."""
        client.register_server(sample_config)
        client.unregister_server("test-server")

        assert client.get_server("test-server") is None

    def test_list_enabled_servers(self, client):
        """Should only list enabled servers."""
        enabled = MCPServerConfig(
            name="enabled",
            server_type=MCPServerType.CUSTOM,
            endpoint="http://localhost:3000",
            enabled=True,
        )
        disabled = MCPServerConfig(
            name="disabled",
            server_type=MCPServerType.CUSTOM,
            endpoint="http://localhost:3001",
            enabled=False,
        )

        client.register_server(enabled)
        client.register_server(disabled)

        enabled_servers = client.list_enabled_servers()
        assert len(enabled_servers) == 1
        assert enabled_servers[0].name == "enabled"

    def test_get_tools_for_ai(self, client, sample_config):
        """Should generate AI-compatible tool definitions."""
        client.register_server(sample_config)

        tools = client.get_tools_for_ai()

        assert len(tools) == 1
        assert tools[0]["name"] == "mcp_test-server"
        assert "input_schema" in tools[0]


class TestMCPToolCalls:
    """Tests for MCP tool call execution."""

    @pytest.fixture
    def client_with_server(self):
        """Create client with a registered server."""
        client = MCPClient()
        config = MCPServerConfig(
            name="test",
            server_type=MCPServerType.CUSTOM,
            endpoint="http://localhost:3000",
            api_key="test-key",
            enabled=True,
            read_only=True,
        )
        client.register_server(config)
        return client

    @pytest.mark.asyncio
    async def test_call_unknown_server(self):
        """Should return error for unknown server."""
        client = MCPClient()

        result = await client.call_tool(
            MCPToolCall(
                server_name="nonexistent",
                tool_name="some_tool",
                arguments={},
            )
        )

        assert result.success is False
        assert "Unknown MCP server" in result.error

    @pytest.mark.asyncio
    async def test_call_disabled_server(self, client_with_server):
        """Should return error for disabled server."""
        client_with_server._servers["test"].enabled = False

        result = await client_with_server.call_tool(
            MCPToolCall(
                server_name="test",
                tool_name="some_tool",
                arguments={},
            )
        )

        assert result.success is False
        assert "disabled" in result.error

    @pytest.mark.asyncio
    async def test_read_only_blocks_write_operations(self, client_with_server):
        """Should block write operations in read-only mode."""
        for tool_name in ["create_resource", "update_config", "delete_item", "write_data"]:
            result = await client_with_server.call_tool(
                MCPToolCall(
                    server_name="test",
                    tool_name=tool_name,
                    arguments={},
                )
            )

            assert result.success is False
            assert "read-only" in result.error

    @pytest.mark.asyncio
    async def test_allowed_tools_filter(self):
        """Should block tools not in allowed list."""
        client = MCPClient()
        config = MCPServerConfig(
            name="restricted",
            server_type=MCPServerType.CUSTOM,
            endpoint="http://localhost:3000",
            enabled=True,
            allowed_tools=["allowed_tool"],
        )
        client.register_server(config)

        result = await client.call_tool(
            MCPToolCall(
                server_name="restricted",
                tool_name="not_allowed_tool",
                arguments={},
            )
        )

        assert result.success is False
        assert "not allowed" in result.error

    @pytest.mark.asyncio
    async def test_successful_tool_call(self, client_with_server):
        """Should execute tool call successfully."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"content": "result data"}

        with patch.object(
            client_with_server,
            "_get_http_client",
            new_callable=AsyncMock,
        ) as mock_get_client:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_client

            result = await client_with_server.call_tool(
                MCPToolCall(
                    server_name="test",
                    tool_name="get_data",
                    arguments={"id": "123"},
                )
            )

            assert result.success is True
            assert result.result == "result data"

    @pytest.mark.asyncio
    async def test_parallel_tool_calls(self, client_with_server):
        """Should execute multiple tool calls in parallel."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"content": "data"}

        with patch.object(
            client_with_server,
            "_get_http_client",
            new_callable=AsyncMock,
        ) as mock_get_client:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_client

            calls = [
                MCPToolCall(server_name="test", tool_name="get_a", arguments={}),
                MCPToolCall(server_name="test", tool_name="get_b", arguments={}),
                MCPToolCall(server_name="test", tool_name="get_c", arguments={}),
            ]

            results = await client_with_server.call_tools_parallel(calls)

            assert len(results) == 3
            assert all(r.success for r in results)


class TestMCPClientHeaders:
    """Tests for header building."""

    def test_headers_with_api_key_bearer(self):
        """Should include bearer token in headers."""
        client = MCPClient()
        config = MCPServerConfig(
            name="test",
            server_type=MCPServerType.CUSTOM,
            endpoint="http://localhost:3000",
            api_key="secret-token",
            auth_header="Authorization",
            auth_prefix="Bearer",
        )

        headers = client._build_headers(config)

        assert headers["Authorization"] == "Bearer secret-token"
        assert headers["Content-Type"] == "application/json"

    def test_headers_with_custom_header(self):
        """Should support custom auth header."""
        client = MCPClient()
        config = MCPServerConfig(
            name="test",
            server_type=MCPServerType.CUSTOM,
            endpoint="http://localhost:3000",
            api_key="api-key-123",
            auth_header="X-API-Key",
            auth_prefix="",
        )

        headers = client._build_headers(config)

        assert headers["X-API-Key"] == "api-key-123"

    def test_headers_without_api_key(self):
        """Should not include auth header without API key."""
        client = MCPClient()
        config = MCPServerConfig(
            name="test",
            server_type=MCPServerType.CUSTOM,
            endpoint="http://localhost:3000",
            api_key=None,
        )

        headers = client._build_headers(config)

        assert "Authorization" not in headers
        assert headers["Content-Type"] == "application/json"
