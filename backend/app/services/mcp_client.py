"""
MCP (Model Context Protocol) Client Service.

Connects to external MCP servers to expand context during debugging sessions.
Supports various MCP servers for cloud providers, git platforms, and observability.

## Supported MCP Servers

### Cloud Providers
- **AWS MCP**: CloudWatch, EKS, ECR, S3, Route53 (read-only)
- **GCP MCP**: GKE, Cloud Logging, Container Registry
- **Azure MCP**: AKS, Container Registry, Monitor

### Git Platforms
- **GitHub MCP**: Repos, PRs, Actions, deployment configs
- **GitLab MCP**: Projects, pipelines, container registry, CI/CD configs

### Observability
- **Prometheus MCP**: Metrics queries, alert rules
- **Grafana MCP**: Dashboards, annotations
- **Datadog MCP**: Metrics, traces, logs

### Secrets Management
- **Vault MCP**: Secret metadata, rotation status (no secret values)

## Example Synergies for K8s Debugging

1. **Pod CrashLoopBackOff Investigation**:
   - K8s events + pod logs (kubectl)
   - Application traces (Datadog MCP)
   - Recent deployments (GitLab MCP)
   - Image scan results (ECR MCP)

2. **Ingress/Networking Issues**:
   - Service/Ingress configs (kubectl)
   - DNS records (Route53 MCP)
   - ALB target health (AWS MCP)
   - Certificate status (cert-manager + Vault MCP)

3. **Resource Exhaustion**:
   - Pod metrics (kubectl top)
   - Historical metrics (Prometheus MCP)
   - Node group scaling (EKS MCP)
   - Cost analysis (CloudWatch MCP)

4. **Deployment Failures**:
   - ArgoCD/Flux status (kubectl)
   - CI/CD pipeline logs (GitLab MCP)
   - Image availability (ECR/GCR MCP)
   - Config changes (GitHub MCP)
"""

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import httpx

from app.core import metrics
from app.core.config import get_settings

logger = logging.getLogger(__name__)


class MCPServerType(str, Enum):
    """Types of MCP servers."""

    # Cloud Providers
    AWS = "aws"
    GCP = "gcp"
    AZURE = "azure"

    # Git Platforms
    GITHUB = "github"
    GITLAB = "gitlab"

    # Observability
    PROMETHEUS = "prometheus"
    GRAFANA = "grafana"
    DATADOG = "datadog"

    # Secrets
    VAULT = "vault"

    # Custom
    CUSTOM = "custom"


@dataclass
class MCPServerConfig:
    """Configuration for an MCP server connection."""

    name: str
    server_type: MCPServerType
    endpoint: str
    api_key: str | None = None
    auth_header: str = "Authorization"
    auth_prefix: str = "Bearer"
    timeout: int = 30
    enabled: bool = True
    read_only: bool = True  # Safety flag
    allowed_tools: list[str] = field(default_factory=list)  # Empty = all
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MCPToolCall:
    """A tool call to an MCP server."""

    server_name: str
    tool_name: str
    arguments: dict[str, Any]


@dataclass
class MCPToolResult:
    """Result from an MCP tool call."""

    server_name: str
    tool_name: str
    success: bool
    result: Any
    error: str | None = None


class MCPClient:
    """
    Client for connecting to MCP servers.

    Manages connections to multiple MCP servers and routes tool calls.
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self._servers: dict[str, MCPServerConfig] = {}
        self._http_client: httpx.AsyncClient | None = None

    async def _get_http_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=30.0)
        return self._http_client

    async def close(self) -> None:
        """Close HTTP client."""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

    def register_server(self, config: MCPServerConfig) -> None:
        """
        Register an MCP server.

        Args:
            config: Server configuration
        """
        self._servers[config.name] = config
        logger.info(f"Registered MCP server: {config.name} ({config.server_type.value})")

    def unregister_server(self, name: str) -> None:
        """Unregister an MCP server."""
        if name in self._servers:
            del self._servers[name]
            logger.info(f"Unregistered MCP server: {name}")

    def get_server(self, name: str) -> MCPServerConfig | None:
        """Get server configuration by name."""
        return self._servers.get(name)

    def list_servers(self) -> list[MCPServerConfig]:
        """List all registered servers."""
        return list(self._servers.values())

    def list_enabled_servers(self) -> list[MCPServerConfig]:
        """List all enabled servers."""
        return [s for s in self._servers.values() if s.enabled]

    async def list_tools(self, server_name: str) -> list[dict[str, Any]]:
        """
        List available tools from an MCP server.

        Args:
            server_name: Name of the server

        Returns:
            List of tool definitions
        """
        server = self._servers.get(server_name)
        if not server:
            raise ValueError(f"Unknown MCP server: {server_name}")

        if not server.enabled:
            return []

        client = await self._get_http_client()
        headers = self._build_headers(server)

        try:
            response = await client.post(
                f"{server.endpoint}/tools/list",
                headers=headers,
                json={},
                timeout=server.timeout,
            )
            response.raise_for_status()
            data = response.json()

            tools: list[dict[str, Any]] = data.get("tools", [])

            # Filter by allowed tools if configured
            if server.allowed_tools:
                tools = [t for t in tools if t.get("name") in server.allowed_tools]

            # Filter out write operations if read_only
            if server.read_only:
                write_keywords = ["create", "update", "delete", "write", "put", "post", "modify"]
                tools = [
                    t
                    for t in tools
                    if not any(kw in t.get("name", "").lower() for kw in write_keywords)
                ]

            return tools

        except httpx.HTTPError as e:
            logger.error(f"Failed to list tools from {server_name}: {e}")
            return []

    async def call_tool(self, call: MCPToolCall) -> MCPToolResult:
        """
        Call a tool on an MCP server.

        Args:
            call: Tool call specification

        Returns:
            Tool result
        """
        server = self._servers.get(call.server_name)
        if not server:
            return MCPToolResult(
                server_name=call.server_name,
                tool_name=call.tool_name,
                success=False,
                result=None,
                error=f"Unknown MCP server: {call.server_name}",
            )

        if not server.enabled:
            return MCPToolResult(
                server_name=call.server_name,
                tool_name=call.tool_name,
                success=False,
                result=None,
                error=f"MCP server {call.server_name} is disabled",
            )

        # Check if tool is allowed
        if server.allowed_tools and call.tool_name not in server.allowed_tools:
            return MCPToolResult(
                server_name=call.server_name,
                tool_name=call.tool_name,
                success=False,
                result=None,
                error=f"Tool {call.tool_name} is not allowed on {call.server_name}",
            )

        # Check read-only mode
        if server.read_only:
            write_keywords = ["create", "update", "delete", "write", "put", "post", "modify"]
            if any(kw in call.tool_name.lower() for kw in write_keywords):
                return MCPToolResult(
                    server_name=call.server_name,
                    tool_name=call.tool_name,
                    success=False,
                    result=None,
                    error="Write operations not allowed (server is read-only)",
                )

        client = await self._get_http_client()
        headers = self._build_headers(server)

        try:
            response = await client.post(
                f"{server.endpoint}/tools/call",
                headers=headers,
                json={
                    "name": call.tool_name,
                    "arguments": call.arguments,
                },
                timeout=server.timeout,
            )
            response.raise_for_status()
            data = response.json()

            return MCPToolResult(
                server_name=call.server_name,
                tool_name=call.tool_name,
                success=True,
                result=data.get("content", data),
            )

        except httpx.HTTPStatusError as e:
            logger.error(f"MCP tool call failed: {e.response.status_code} - {e.response.text}")
            return MCPToolResult(
                server_name=call.server_name,
                tool_name=call.tool_name,
                success=False,
                result=None,
                error=f"HTTP {e.response.status_code}: {e.response.text[:200]}",
            )
        except httpx.HTTPError as e:
            logger.error(f"MCP tool call failed: {e}")
            return MCPToolResult(
                server_name=call.server_name,
                tool_name=call.tool_name,
                success=False,
                result=None,
                error=str(e),
            )

    async def call_tools_parallel(self, calls: list[MCPToolCall]) -> list[MCPToolResult]:
        """
        Call multiple tools in parallel.

        Args:
            calls: List of tool calls

        Returns:
            List of results in same order as calls
        """
        tasks = [self.call_tool(call) for call in calls]
        results = await asyncio.gather(*tasks)
        for r in results:
            metrics.mcp_tool_calls_total.labels(
                server=r.server_name, is_error=str(not r.success)
            ).inc()
        return results

    def _build_headers(self, server: MCPServerConfig) -> dict[str, str]:
        """Build request headers for a server."""
        headers = {"Content-Type": "application/json"}

        if server.api_key:
            if server.auth_prefix:
                headers[server.auth_header] = f"{server.auth_prefix} {server.api_key}"
            else:
                headers[server.auth_header] = server.api_key

        return headers

    def get_tools_for_ai(self) -> list[dict[str, Any]]:
        """
        Get all MCP tools formatted for AI consumption.

        Returns tool definitions that can be added to the AI's tool list.
        """
        all_tools = []

        for server in self.list_enabled_servers():
            # Create a wrapper tool for each server
            all_tools.append(
                {
                    "name": f"mcp_{server.name}",
                    "description": (
                        f"Access {server.server_type.value} resources"
                        f" via MCP server '{server.name}'. "
                        "Use this for cloud resources, git"
                        " operations, or observability data."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "tool": {
                                "type": "string",
                                "description": "The MCP tool to call",
                            },
                            "arguments": {
                                "type": "object",
                                "description": "Arguments for the tool",
                            },
                        },
                        "required": ["tool"],
                    },
                }
            )

        return all_tools


# =============================================================================
# Pre-configured MCP Server Templates
# =============================================================================


def create_aws_mcp_config(
    name: str = "aws",
    endpoint: str = "http://localhost:3000",
    region: str = "us-east-1",
    **kwargs: Any,
) -> MCPServerConfig:
    """
    Create AWS MCP server configuration.

    Provides read-only access to:
    - CloudWatch Logs and Metrics
    - EKS cluster information
    - ECR repositories and images
    - S3 bucket listings (no content)
    - Route53 DNS records
    """
    return MCPServerConfig(
        name=name,
        server_type=MCPServerType.AWS,
        endpoint=endpoint,
        read_only=True,
        metadata={"region": region},
        allowed_tools=[
            # CloudWatch
            "cloudwatch_get_log_events",
            "cloudwatch_describe_log_groups",
            "cloudwatch_get_metric_data",
            # EKS
            "eks_describe_cluster",
            "eks_list_nodegroups",
            "eks_describe_nodegroup",
            # ECR
            "ecr_describe_repositories",
            "ecr_describe_images",
            "ecr_get_scan_findings",
            # S3
            "s3_list_buckets",
            "s3_list_objects",
            # Route53
            "route53_list_hosted_zones",
            "route53_list_resource_record_sets",
        ],
        **kwargs,
    )


def create_gitlab_mcp_config(
    name: str = "gitlab",
    endpoint: str = "http://localhost:3001",
    gitlab_url: str = "https://gitlab.com",
    **kwargs: Any,
) -> MCPServerConfig:
    """
    Create GitLab MCP server configuration.

    Provides read-only access to:
    - Projects and repositories
    - Merge requests
    - CI/CD pipelines and jobs
    - Container registry
    - Deployment environments
    """
    return MCPServerConfig(
        name=name,
        server_type=MCPServerType.GITLAB,
        endpoint=endpoint,
        read_only=True,
        metadata={"gitlab_url": gitlab_url},
        allowed_tools=[
            # Projects
            "gitlab_get_project",
            "gitlab_list_projects",
            "gitlab_get_file",
            "gitlab_list_repository_tree",
            # Merge Requests
            "gitlab_list_merge_requests",
            "gitlab_get_merge_request",
            "gitlab_get_merge_request_changes",
            # Pipelines
            "gitlab_list_pipelines",
            "gitlab_get_pipeline",
            "gitlab_list_pipeline_jobs",
            "gitlab_get_job_log",
            # Registry
            "gitlab_list_registry_repositories",
            "gitlab_list_registry_tags",
            # Environments
            "gitlab_list_environments",
            "gitlab_get_environment",
        ],
        **kwargs,
    )


def create_github_mcp_config(
    name: str = "github",
    endpoint: str = "http://localhost:3002",
    **kwargs: Any,
) -> MCPServerConfig:
    """
    Create GitHub MCP server configuration.

    Provides read-only access to:
    - Repositories and files
    - Pull requests
    - Actions workflows and runs
    - Packages/container registry
    - Deployments
    """
    return MCPServerConfig(
        name=name,
        server_type=MCPServerType.GITHUB,
        endpoint=endpoint,
        read_only=True,
        allowed_tools=[
            # Repos
            "github_get_repository",
            "github_get_file_contents",
            "github_list_commits",
            # PRs
            "github_list_pull_requests",
            "github_get_pull_request",
            "github_get_pull_request_files",
            # Actions
            "github_list_workflows",
            "github_list_workflow_runs",
            "github_get_workflow_run_logs",
            # Packages
            "github_list_packages",
            "github_get_package_versions",
            # Deployments
            "github_list_deployments",
            "github_get_deployment_status",
        ],
        **kwargs,
    )


def create_prometheus_mcp_config(
    name: str = "prometheus",
    endpoint: str = "http://localhost:3003",
    prometheus_url: str = "http://prometheus:9090",
    **kwargs: Any,
) -> MCPServerConfig:
    """
    Create Prometheus MCP server configuration.

    Provides read-only access to:
    - Instant and range queries
    - Alert rules and status
    - Target health
    - Metric metadata
    """
    return MCPServerConfig(
        name=name,
        server_type=MCPServerType.PROMETHEUS,
        endpoint=endpoint,
        read_only=True,
        metadata={"prometheus_url": prometheus_url},
        allowed_tools=[
            "prometheus_query",
            "prometheus_query_range",
            "prometheus_list_alerts",
            "prometheus_list_rules",
            "prometheus_list_targets",
            "prometheus_get_metadata",
        ],
        **kwargs,
    )


def create_datadog_mcp_config(
    name: str = "datadog",
    endpoint: str = "http://localhost:3004",
    **kwargs: Any,
) -> MCPServerConfig:
    """
    Create Datadog MCP server configuration.

    Provides read-only access to:
    - Metrics queries
    - Log queries
    - APM traces
    - Monitors/alerts
    """
    return MCPServerConfig(
        name=name,
        server_type=MCPServerType.DATADOG,
        endpoint=endpoint,
        read_only=True,
        allowed_tools=[
            "datadog_query_metrics",
            "datadog_search_logs",
            "datadog_list_traces",
            "datadog_get_trace",
            "datadog_list_monitors",
            "datadog_get_monitor",
        ],
        **kwargs,
    )


# =============================================================================
# Singleton and Factory
# =============================================================================


_mcp_client: MCPClient | None = None


def get_mcp_client() -> MCPClient:
    """Get the MCP client singleton."""
    global _mcp_client
    if _mcp_client is None:
        _mcp_client = MCPClient()
    return _mcp_client


async def initialize_mcp_from_config() -> MCPClient:
    """
    Initialize MCP client from environment configuration.

    Reads MCP_SERVERS environment variable (JSON) to configure servers.
    """
    import json
    import os

    client = get_mcp_client()

    # Check for MCP configuration
    mcp_config = os.environ.get("MCP_SERVERS")
    if not mcp_config:
        logger.info("No MCP_SERVERS configured")
        return client

    try:
        servers = json.loads(mcp_config)
        for server_config in servers:
            config = MCPServerConfig(
                name=server_config["name"],
                server_type=MCPServerType(server_config.get("type", "custom")),
                endpoint=server_config["endpoint"],
                api_key=server_config.get("api_key"),
                enabled=server_config.get("enabled", True),
                read_only=server_config.get("read_only", True),
                allowed_tools=server_config.get("allowed_tools", []),
            )
            client.register_server(config)

        logger.info(f"Initialized {len(servers)} MCP servers from config")

    except (json.JSONDecodeError, KeyError) as e:
        logger.error(f"Failed to parse MCP_SERVERS config: {e}")

    return client
