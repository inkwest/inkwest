"""
Tests for kubectl tools module.

Tests validation, command building, and execution logic.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.tools import (
    KUBECTL_TOOLS,
    build_kubectl_command,
    execute_kubectl,
    execute_tools_parallel,
    get_all_tool_names,
    get_tool_by_name,
    truncate_output,
    validate_label_selector,
    validate_tool_input,
    validate_tool_schema,
)


class TestToolDefinitions:
    """Tests for tool definitions."""

    def test_kubectl_tools_not_empty(self):
        """KUBECTL_TOOLS should contain tool definitions."""
        assert len(KUBECTL_TOOLS) > 0

    def test_all_tools_have_required_fields(self):
        """All tools should have name, description, and input_schema."""
        for tool in KUBECTL_TOOLS:
            assert "name" in tool
            assert "description" in tool
            assert "input_schema" in tool

    def test_all_tools_valid_schema(self):
        """All tools should pass schema validation."""
        for tool in KUBECTL_TOOLS:
            assert validate_tool_schema(tool)

    def test_get_tool_by_name_found(self):
        """get_tool_by_name should return tool when found."""
        tool = get_tool_by_name("kubectl_get")
        assert tool is not None
        assert tool["name"] == "kubectl_get"

    def test_get_tool_by_name_not_found(self):
        """get_tool_by_name should return None when not found."""
        tool = get_tool_by_name("nonexistent_tool")
        assert tool is None

    def test_get_all_tool_names(self):
        """get_all_tool_names should return list of tool names."""
        names = get_all_tool_names()
        assert "kubectl_get" in names
        assert "kubectl_describe" in names
        assert "kubectl_logs" in names
        assert "kubectl_events" in names
        assert "kubectl_top" in names
        assert "kubectl_explain" in names
        assert "kubectl_api_resources" in names


class TestValidation:
    """Tests for input validation."""

    def test_validate_valid_input(self):
        """Valid input should pass validation."""
        error = validate_tool_input("kubectl_get", {"resource": "pods"})
        assert error is None

    def test_validate_valid_input_with_namespace(self):
        """Valid input with namespace should pass validation."""
        error = validate_tool_input("kubectl_get", {"resource": "pods", "namespace": "kube-system"})
        assert error is None

    def test_validate_valid_input_with_name(self):
        """Valid input with resource name should pass validation."""
        error = validate_tool_input("kubectl_get", {"resource": "pods", "name": "my-pod-123"})
        assert error is None

    def test_validate_unknown_resource_allowed(self):
        """Unknown but well-formed resource kinds are allowed — RBAC handles access control.

        Only the *shape* is constrained: the value must be a plain kind, never a flag.
        """
        error = validate_tool_input("kubectl_get", {"resource": "hackme"})
        assert error is None

    def test_validate_rejects_secret_reads(self):
        """Secret reads are denied regardless of RBAC (base64 credential exfil path)."""
        for kind in ("secret", "secrets", "secrets.v1.core"):
            error = validate_tool_input("kubectl_get", {"resource": kind})
            assert error is not None and "secret" in error.lower(), kind
        # A well-formed non-secret kind is still allowed.
        assert validate_tool_input("kubectl_get", {"resource": "configmaps"}) is None

    def test_validate_invalid_namespace(self):
        """Invalid namespace format should fail validation."""
        error = validate_tool_input("kubectl_get", {"resource": "pods", "namespace": "INVALID"})
        assert error is not None
        assert "Invalid namespace" in error

    def test_validate_invalid_namespace_special_chars(self):
        """Namespace with special characters should fail validation."""
        error = validate_tool_input(
            "kubectl_get", {"resource": "pods", "namespace": "my_namespace!"}
        )
        assert error is not None
        assert "Invalid namespace" in error

    def test_validate_invalid_name(self):
        """Invalid resource name should fail validation."""
        error = validate_tool_input("kubectl_get", {"resource": "pods", "name": "My-Pod!!"})
        assert error is not None
        assert "Invalid resource name" in error

    def test_validate_invalid_tail_too_high(self):
        """Tail value too high should fail validation."""
        error = validate_tool_input("kubectl_logs", {"pod_name": "my-pod", "tail": 50000})
        assert error is not None
        assert "tail" in error.lower()

    def test_validate_invalid_tail_zero(self):
        """Tail value of zero should fail validation."""
        error = validate_tool_input("kubectl_logs", {"pod_name": "my-pod", "tail": 0})
        assert error is not None
        assert "tail" in error.lower()

    def test_validate_invalid_tail_negative(self):
        """Negative tail value should fail validation."""
        error = validate_tool_input("kubectl_logs", {"pod_name": "my-pod", "tail": -10})
        assert error is not None
        assert "tail" in error.lower()

    def test_validate_valid_tail(self):
        """Valid tail value should pass validation."""
        error = validate_tool_input("kubectl_logs", {"pod_name": "my-pod", "tail": 100})
        assert error is None

    def test_validate_valid_selector(self):
        """Valid label selector should pass validation."""
        error = validate_tool_input(
            "kubectl_get", {"resource": "pods", "selector": "app=nginx,env=prod"}
        )
        assert error is None

    def test_validate_invalid_selector(self):
        """Invalid label selector should fail validation."""
        error = validate_tool_input(
            "kubectl_get", {"resource": "pods", "selector": "invalid selector format"}
        )
        assert error is not None
        assert "Invalid" in error

    def test_validate_top_pods(self):
        """kubectl_top with pods should pass validation."""
        error = validate_tool_input("kubectl_top", {"resource": "pods"})
        assert error is None

    def test_validate_top_nodes(self):
        """kubectl_top with nodes should pass validation."""
        error = validate_tool_input("kubectl_top", {"resource": "nodes"})
        assert error is None

    def test_validate_top_invalid_resource(self):
        """kubectl_top with invalid resource should fail validation."""
        error = validate_tool_input("kubectl_top", {"resource": "deployments"})
        assert error is not None
        assert "only supports" in error.lower()

    def test_validate_valid_since(self):
        """Valid since duration should pass validation."""
        error = validate_tool_input("kubectl_logs", {"pod_name": "my-pod", "since": "1h"})
        assert error is None

    def test_validate_invalid_since(self):
        """Invalid since duration should fail validation."""
        error = validate_tool_input("kubectl_logs", {"pod_name": "my-pod", "since": "invalid"})
        assert error is not None
        assert "Invalid duration" in error

    def test_validate_valid_field_selector(self):
        """Well-formed field selector should pass."""
        error = validate_tool_input(
            "kubectl_events", {"field_selector": "involvedObject.name=my-pod"}
        )
        assert error is None

    def test_validate_invalid_field_selector(self):
        """A field selector that doesn't match the pattern is rejected."""
        error = validate_tool_input("kubectl_events", {"field_selector": "bad selector!"})
        assert error is not None
        assert "field selector" in error

    def test_validate_valid_api_version(self):
        """Recognized API version should pass."""
        error = validate_tool_input("kubectl_explain", {"api_version": "apps/v1"})
        assert error is None

    def test_validate_invalid_api_version(self):
        """Malformed API version should fail."""
        error = validate_tool_input("kubectl_explain", {"api_version": "not a version"})
        assert error is not None
        assert "API version" in error

    def test_validate_invalid_output_format(self):
        """Output format outside the allow-list is rejected."""
        error = validate_tool_input("kubectl_get", {"resource": "pods", "output": "exec"})
        assert error is not None
        assert "output format" in error

    def test_validate_output_name_accepted(self):
        """'name' is a schema-advertised output and must pass validation."""
        error = validate_tool_input("kubectl_get", {"resource": "pods", "output": "name"})
        assert error is None

    def test_validate_invalid_sort_by(self):
        """sort_by outside {cpu, memory} is rejected."""
        error = validate_tool_input("kubectl_top", {"resource": "pods", "sort_by": "disk"})
        assert error is not None
        assert "sort_by" in error

    def test_validate_invalid_event_type(self):
        """Event type outside {Normal, Warning} is rejected."""
        error = validate_tool_input("kubectl_events", {"types": "Critical"})
        assert error is not None
        assert "event type" in error


class TestEnumDriftGuard:
    """The validator's enum allow-lists must match the tool schema enums.

    The validator keeps its own constants (so validation.py stays a pure leaf module
    with no intra-package import), but a schema enum and its validator allow-list that
    disagree silently reject (or admit) values — exactly the gap that blocked
    output='name'. These tests fail if the two ever drift again.

    Note: ALLOWED_TOP_RESOURCES is intentionally NOT pinned here — it is a
    deliberate superset of the kubectl_top 'resource' enum (it also accepts the
    singular 'pod'/'node'), so it is not subject to the drift these tests guard.
    """

    @staticmethod
    def _schema_enum(prop: str) -> set[str]:
        """Collect the `enum` declared for `prop` across all kubectl tool schemas."""
        values: set[str] = set()
        for tool in KUBECTL_TOOLS:
            spec = tool["input_schema"]["properties"].get(prop)
            if spec and "enum" in spec:
                values.update(spec["enum"])
        return values

    def test_output_allow_list_matches_schema(self):
        from app.tools.validation import ALLOWED_OUTPUT_FORMATS

        assert ALLOWED_OUTPUT_FORMATS == self._schema_enum("output")

    def test_sort_by_allow_list_matches_schema(self):
        from app.tools.validation import ALLOWED_SORT_BY

        assert ALLOWED_SORT_BY == self._schema_enum("sort_by")

    def test_event_types_allow_list_matches_schema(self):
        from app.tools.validation import ALLOWED_EVENT_TYPES

        assert ALLOWED_EVENT_TYPES == self._schema_enum("types")


class TestArgumentInjection:
    """Free-string args must never reach the kubectl argv flag-shaped.

    A prompt-injected tool call could otherwise smuggle a flag as a value, e.g. a 'resource'
    of '--kubeconfig=...' (cluster pivot, bypassing check_cluster_access) or '--raw=...'
    (bypassing the -o output allow-list to read any RBAC-permitted API path).
    """

    def test_resource_rejects_raw_flag(self):
        """'--raw=...' smuggled as a resource is rejected before argv assembly."""
        error = validate_tool_input(
            "kubectl_get", {"resource": "--raw=/api/v1/namespaces/kube-system/secrets"}
        )
        assert error is not None
        assert "Invalid resource" in error

    def test_resource_rejects_kubeconfig_flag(self):
        """'--kubeconfig=...' smuggled as a resource is rejected (cluster pivot)."""
        error = validate_tool_input(
            "kubectl_get", {"resource": "--kubeconfig=/etc/inkwest/kubeconfigs/other.yaml"}
        )
        assert error is not None
        assert "Invalid resource" in error

    def test_resource_rejects_bare_leading_dash(self):
        """Any leading dash is rejected, even without an '=' payload."""
        error = validate_tool_input("kubectl_get", {"resource": "-A"})
        assert error is not None
        assert "Invalid resource" in error

    def test_resource_type_rejects_flag(self):
        """The MCP server's 'resource_type' key gets the same guard as 'resource'."""
        error = validate_tool_input("kubectl_top", {"resource_type": "--kubeconfig=/etc/x.yaml"})
        assert error is not None
        assert "Invalid resource" in error

    def test_container_rejects_flag(self):
        """'container' lands after '-c'; a flag-shaped value is rejected."""
        error = validate_tool_input(
            "kubectl_logs", {"pod_name": "my-pod", "container": "--kubeconfig=/x"}
        )
        assert error is not None
        assert "Invalid container" in error

    def test_api_group_rejects_flag(self):
        """'api_group' lands after '--api-group'; a flag-shaped value is rejected."""
        error = validate_tool_input("kubectl_api_resources", {"api_group": "--raw=/x"})
        assert error is not None
        assert "Invalid API group" in error

    def test_verbs_rejects_flag(self):
        """'verbs' lands after '--verbs'; a flag-shaped value is rejected."""
        error = validate_tool_input("kubectl_api_resources", {"verbs": "--raw=/x"})
        assert error is not None
        assert "Invalid verbs" in error

    def test_legitimate_resource_paths_still_pass(self):
        """Valid kinds and explain field paths must not be over-restricted."""
        assert validate_tool_input("kubectl_get", {"resource": "pods"}) is None
        assert validate_tool_input("kubectl_get", {"resource": "networkpolicies"}) is None
        assert validate_tool_input("kubectl_explain", {"resource": "pod.spec.containers"}) is None

    def test_legitimate_container_and_verbs_still_pass(self):
        """Valid container names and verb lists must not be over-restricted."""
        assert (
            validate_tool_input("kubectl_logs", {"pod_name": "my-pod", "container": "nginx-proxy"})
            is None
        )
        assert validate_tool_input("kubectl_api_resources", {"verbs": "get,list,watch"}) is None
        assert (
            validate_tool_input("kubectl_api_resources", {"api_group": "networking.k8s.io"}) is None
        )


class TestLabelSelectorValidation:
    """Tests for label selector validation."""

    def test_validate_simple_selector(self):
        """Simple selector should pass."""
        error = validate_label_selector("app=nginx")
        assert error is None

    def test_validate_multiple_selectors(self):
        """Multiple selectors should pass."""
        error = validate_label_selector("app=nginx,env=prod")
        assert error is None

    def test_validate_not_equals_selector(self):
        """Not-equals selector should pass."""
        error = validate_label_selector("env!=test")
        assert error is None

    def test_validate_invalid_key(self):
        """Invalid label key should fail."""
        error = validate_label_selector("invalid!key=value")
        assert error is not None

    def test_validate_empty_key(self):
        """Empty label key should fail."""
        error = validate_label_selector("=value")
        assert error is not None

    def test_validate_bare_key_without_operator(self):
        """A token with no =/!= operator is rejected (not a valid existence check)."""
        error = validate_label_selector("justakey")
        assert error is not None

    def test_validate_invalid_label_value(self):
        """A value with illegal characters is rejected."""
        error = validate_label_selector("app=ba d!value")
        assert error is not None

    def test_validate_empty_selector(self):
        """Empty selector string should pass."""
        error = validate_label_selector("")
        assert error is None


class TestCommandBuilder:
    """Tests for kubectl command builder."""

    def test_build_get_command_basic(self):
        """Build basic get command."""
        cmd = build_kubectl_command("kubectl_get", {"resource": "pods"}, "/path/to/kubeconfig")
        assert cmd == ["kubectl", "--kubeconfig", "/path/to/kubeconfig", "get", "pods"]

    def test_build_get_command_with_namespace(self):
        """Build get command with namespace."""
        cmd = build_kubectl_command(
            "kubectl_get", {"resource": "pods", "namespace": "kube-system"}, "/kubeconfig"
        )
        assert cmd == ["kubectl", "--kubeconfig", "/kubeconfig", "get", "pods", "-n", "kube-system"]

    def test_build_get_command_with_name(self):
        """Build get command with resource name."""
        cmd = build_kubectl_command(
            "kubectl_get",
            {"resource": "pods", "name": "my-pod", "namespace": "default"},
            "/kubeconfig",
        )
        assert "my-pod" in cmd

    def test_build_get_command_all_namespaces(self):
        """Build get command with all namespaces."""
        cmd = build_kubectl_command(
            "kubectl_get", {"resource": "pods", "all_namespaces": True}, "/kubeconfig"
        )
        assert "-A" in cmd

    def test_build_get_command_with_selector(self):
        """Build get command with label selector."""
        cmd = build_kubectl_command(
            "kubectl_get", {"resource": "pods", "selector": "app=nginx"}, "/kubeconfig"
        )
        assert "-l" in cmd
        assert "app=nginx" in cmd

    def test_build_get_command_with_output(self):
        """Build get command with output format."""
        cmd = build_kubectl_command(
            "kubectl_get", {"resource": "pods", "output": "yaml"}, "/kubeconfig"
        )
        assert "-o" in cmd
        assert "yaml" in cmd

    def test_build_get_command_with_context(self):
        """Build get command with context."""
        cmd = build_kubectl_command(
            "kubectl_get", {"resource": "pods"}, "/kubeconfig", context="my-cluster"
        )
        assert "--context" in cmd
        assert "my-cluster" in cmd

    def test_build_describe_command(self):
        """Build describe command."""
        cmd = build_kubectl_command(
            "kubectl_describe",
            {"resource": "pod", "name": "my-pod", "namespace": "kube-system"},
            "/kubeconfig",
        )
        assert cmd == [
            "kubectl",
            "--kubeconfig",
            "/kubeconfig",
            "describe",
            "pod",
            "my-pod",
            "-n",
            "kube-system",
        ]

    def test_build_logs_command_basic(self):
        """Build basic logs command."""
        cmd = build_kubectl_command("kubectl_logs", {"pod_name": "my-pod"}, "/kubeconfig")
        assert "logs" in cmd
        assert "my-pod" in cmd

    def test_build_logs_command_with_tail(self):
        """Build logs command with tail."""
        cmd = build_kubectl_command(
            "kubectl_logs", {"pod_name": "my-pod", "tail": 50}, "/kubeconfig"
        )
        assert "--tail" in cmd
        assert "50" in cmd

    def test_build_logs_command_with_previous(self):
        """Build logs command with previous flag."""
        cmd = build_kubectl_command(
            "kubectl_logs", {"pod_name": "my-pod", "previous": True}, "/kubeconfig"
        )
        assert "--previous" in cmd

    def test_build_logs_command_with_container(self):
        """Build logs command with container."""
        cmd = build_kubectl_command(
            "kubectl_logs", {"pod_name": "my-pod", "container": "nginx"}, "/kubeconfig"
        )
        assert "-c" in cmd
        assert "nginx" in cmd

    def test_build_logs_command_with_since(self):
        """Build logs command with since."""
        cmd = build_kubectl_command(
            "kubectl_logs", {"pod_name": "my-pod", "since": "1h"}, "/kubeconfig"
        )
        assert "--since" in cmd
        assert "1h" in cmd

    def test_build_events_command_basic(self):
        """Build basic events command."""
        cmd = build_kubectl_command("kubectl_events", {}, "/kubeconfig")
        assert "get" in cmd
        assert "events" in cmd

    def test_build_events_command_with_type_filter(self):
        """Build events command with type filter."""
        cmd = build_kubectl_command("kubectl_events", {"types": "Warning"}, "/kubeconfig")
        assert "--field-selector" in cmd

    def test_build_top_pods_command(self):
        """Build top pods command."""
        cmd = build_kubectl_command(
            "kubectl_top", {"resource": "pods", "namespace": "default"}, "/kubeconfig"
        )
        assert "top" in cmd
        assert "pods" in cmd

    def test_build_top_nodes_command(self):
        """Build top nodes command."""
        cmd = build_kubectl_command("kubectl_top", {"resource": "nodes"}, "/kubeconfig")
        assert "top" in cmd
        assert "nodes" in cmd

    def test_build_top_command_with_sort(self):
        """Build top command with sort."""
        cmd = build_kubectl_command(
            "kubectl_top", {"resource": "pods", "sort_by": "memory"}, "/kubeconfig"
        )
        assert "--sort-by" in cmd
        assert "memory" in cmd

    def test_build_explain_command(self):
        """Build explain command."""
        cmd = build_kubectl_command("kubectl_explain", {"resource": "pod"}, "/kubeconfig")
        assert "explain" in cmd
        assert "pod" in cmd

    def test_build_explain_command_recursive(self):
        """Build explain command with recursive flag."""
        cmd = build_kubectl_command(
            "kubectl_explain", {"resource": "pod.spec", "recursive": True}, "/kubeconfig"
        )
        assert "--recursive" in cmd

    def test_build_api_resources_command(self):
        """Build api-resources command."""
        cmd = build_kubectl_command("kubectl_api_resources", {}, "/kubeconfig")
        assert "api-resources" in cmd

    def test_build_api_resources_command_with_filters(self):
        """Build api-resources command with filters."""
        cmd = build_kubectl_command(
            "kubectl_api_resources", {"api_group": "apps", "namespaced": True}, "/kubeconfig"
        )
        assert "--api-group" in cmd
        assert "apps" in cmd
        assert "--namespaced=true" in cmd

    def test_build_unknown_tool_raises(self):
        """Unknown tool should raise ValueError."""
        with pytest.raises(ValueError, match="Unknown tool"):
            build_kubectl_command("unknown_tool", {}, "/kubeconfig")


class TestTruncateOutput:
    """Tests for output truncation."""

    def test_truncate_short_output(self):
        """Short output should not be truncated."""
        output = "short output"
        result = truncate_output(output, "kubectl_get")
        assert result == output

    def test_truncate_long_output_get(self):
        """Long get output should be truncated from end."""
        output = "a" * 100000
        result = truncate_output(output, "kubectl_get")
        assert len(result) < len(output)
        assert "truncated" in result.lower()

    def test_truncate_long_output_logs(self):
        """Long logs output should keep tail."""
        output = "a" * 100000
        result = truncate_output(output, "kubectl_logs")
        assert len(result) < len(output)
        assert "truncated" in result.lower()
        assert "Earlier logs" in result


class TestExecutor:
    """Tests for kubectl executor."""

    @pytest.mark.asyncio
    async def test_execute_kubectl_validation_error(self):
        """Validation error should return error tuple."""
        output, is_error = await execute_kubectl(
            "kubectl_get", {"resource": "pods", "namespace": "INVALID!"}, "/kubeconfig"
        )
        assert is_error is True
        assert "Validation error" in output

    @pytest.mark.asyncio
    async def test_execute_kubectl_unknown_tool(self):
        """Unknown tool should return error tuple."""
        output, is_error = await execute_kubectl(
            "unknown_tool", {"resource": "pods"}, "/kubeconfig"
        )
        assert is_error is True
        assert "error" in output.lower()

    @pytest.mark.asyncio
    async def test_execute_kubectl_success(self):
        """Successful execution should return output."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "NAME  READY  STATUS\npod1  1/1    Running"
        mock_result.stderr = ""

        with patch("app.tools.executor.asyncio.to_thread") as mock_to_thread:
            mock_to_thread.return_value = mock_result
            output, is_error = await execute_kubectl(
                "kubectl_get", {"resource": "pods"}, "/kubeconfig"
            )
            assert is_error is False
            assert "pod1" in output

    @pytest.mark.asyncio
    async def test_execute_kubectl_command_failed(self):
        """Failed command should return stderr."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "Error: pods not found"

        with patch("app.tools.executor.asyncio.to_thread") as mock_to_thread:
            mock_to_thread.return_value = mock_result
            output, is_error = await execute_kubectl(
                "kubectl_get", {"resource": "pods", "name": "nonexistent"}, "/kubeconfig"
            )
            assert is_error is True
            assert "not found" in output

    @pytest.mark.asyncio
    async def test_execute_kubectl_timeout(self):
        """Timeout should return error tuple."""
        with patch("app.tools.executor.asyncio.to_thread") as mock_to_thread:
            mock_to_thread.side_effect = TimeoutError()
            output, is_error = await execute_kubectl(
                "kubectl_get", {"resource": "pods"}, "/kubeconfig", timeout=1
            )
            assert is_error is True
            assert "timed out" in output.lower()

    @pytest.mark.asyncio
    async def test_execute_kubectl_not_found(self):
        """Missing kubectl should return error tuple."""
        with patch("app.tools.executor.asyncio.to_thread") as mock_to_thread:
            mock_to_thread.side_effect = FileNotFoundError()
            output, is_error = await execute_kubectl(
                "kubectl_get", {"resource": "pods"}, "/kubeconfig"
            )
            assert is_error is True
            assert "kubectl not found" in output


class TestParallelExecution:
    """Tests for parallel tool execution."""

    @pytest.mark.asyncio
    async def test_execute_tools_parallel_empty(self):
        """Empty tool list should return empty results."""
        results = await execute_tools_parallel([], "/kubeconfig")
        assert results == []

    @pytest.mark.asyncio
    async def test_execute_tools_parallel_single(self):
        """Single tool should execute correctly."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "output"
        mock_result.stderr = ""

        with patch("app.tools.executor.asyncio.to_thread") as mock_to_thread:
            mock_to_thread.return_value = mock_result
            results = await execute_tools_parallel(
                [{"id": "1", "name": "kubectl_get", "input": {"resource": "pods"}}], "/kubeconfig"
            )
            assert len(results) == 1
            assert results[0]["tool_use_id"] == "1"
            assert results[0]["is_error"] is False

    @pytest.mark.asyncio
    async def test_execute_tools_parallel_multiple(self):
        """Multiple tools should execute in parallel."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "output"
        mock_result.stderr = ""

        with patch("app.tools.executor.asyncio.to_thread") as mock_to_thread:
            mock_to_thread.return_value = mock_result
            results = await execute_tools_parallel(
                [
                    {"id": "1", "name": "kubectl_get", "input": {"resource": "pods"}},
                    {"id": "2", "name": "kubectl_get", "input": {"resource": "services"}},
                ],
                "/kubeconfig",
            )
            assert len(results) == 2

    @pytest.mark.asyncio
    async def test_execute_tools_parallel_handles_exceptions(self):
        """Exceptions in parallel execution should be handled."""
        with patch("app.tools.executor.asyncio.to_thread") as mock_to_thread:
            mock_to_thread.side_effect = Exception("Test error")
            results = await execute_tools_parallel(
                [{"id": "1", "name": "kubectl_get", "input": {"resource": "pods"}}], "/kubeconfig"
            )
            assert len(results) == 1
            assert results[0]["is_error"] is True


class TestResourceValidation:
    """Resource type validation — RBAC handles access; we constrain the value's shape
    and additionally restrict kubectl_top to pods/nodes."""

    def test_top_rejects_invalid_resource(self):
        """kubectl_top only accepts pods/nodes."""
        result = validate_tool_input("kubectl_top", {"resource": "deployments"})
        assert result is not None
        assert "pods" in result

    def test_top_accepts_valid_resources(self):
        """kubectl_top accepts pods and nodes."""
        assert validate_tool_input("kubectl_top", {"resource": "pods"}) is None
        assert validate_tool_input("kubectl_top", {"resource": "nodes"}) is None

    def test_get_accepts_any_resource(self):
        """kubectl_get accepts any resource type (RBAC enforces access)."""
        assert validate_tool_input("kubectl_get", {"resource": "pods"}) is None
        assert validate_tool_input("kubectl_get", {"resource": "applications"}) is None
        assert validate_tool_input("kubectl_get", {"resource": "certificates"}) is None


class TestMcpServerArgumentInjection:
    """The standalone MCP server enforces the same flag-shape guard as the API path."""

    def test_mcp_rejects_raw_resource(self):
        """'--raw=...' as a resource never reaches kubectl on the MCP path."""
        from app.mcp_server import execute_kubectl as mcp_execute_kubectl

        output, is_error = mcp_execute_kubectl(
            "kubectl_get", {"resource": "--raw=/api/v1/namespaces/kube-system/secrets"}
        )
        assert is_error is True
        assert "Validation error" in output

    def test_mcp_rejects_kubeconfig_resource_type(self):
        """'--kubeconfig=...' as a kubectl_top resource_type is rejected (cluster pivot)."""
        from app.mcp_server import execute_kubectl as mcp_execute_kubectl

        output, is_error = mcp_execute_kubectl(
            "kubectl_top", {"resource_type": "--kubeconfig=/etc/inkwest/kubeconfigs/other.yaml"}
        )
        assert is_error is True
        assert "Validation error" in output

    def test_mcp_rejects_container_flag(self):
        """'--kubeconfig=...' smuggled as a log container is rejected on the MCP path."""
        from app.mcp_server import execute_kubectl as mcp_execute_kubectl

        output, is_error = mcp_execute_kubectl(
            "kubectl_logs", {"pod_name": "my-pod", "container": "--kubeconfig=/x"}
        )
        assert is_error is True
        assert "Validation error" in output


class TestKubectlWithoutCluster:
    """kubectl tools degrade gracefully when no kubeconfig is set."""

    @pytest.mark.asyncio
    async def test_kubectl_returns_clean_error_without_kubeconfig(self):
        import uuid

        from app.tools.dispatch import execute_tool
        from app.tools.exec_context import ToolExecContext

        ctx = ToolExecContext(user_id=None, session_id=uuid.uuid4(), kubeconfig_path=None)

        with patch("app.tools.dispatch.execute_kubectl") as mock_kubectl:
            output, is_error = await execute_tool("kubectl_get", {"resource": "pods"}, ctx)

        assert is_error is True
        assert "No Kubernetes cluster is configured" in output
        # The kubectl executor must not be invoked at all.
        mock_kubectl.assert_not_called()
