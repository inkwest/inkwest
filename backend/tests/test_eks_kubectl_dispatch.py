"""
EKS → kubectl tool-path wiring.

Granting AWS access that can reach EKS generates a per-session merged kubeconfig
(``eks_refresh``). The shell agent gets it via ``KUBECONFIG``, but the tool-based engines
target kubectl by an explicit ``--kubeconfig`` path — so ``tools.dispatch.execute_tool``
must fall back to the discovered kubeconfig when no cluster is statically configured, and
let the agent pick a discovered cluster with a per-call ``context``.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

from app.tools import definitions
from app.tools.dispatch import execute_tool
from app.tools.exec_context import ToolExecContext
from app.tools.executor import execute_kubectl
from app.tools.validation import validate_tool_input

_SID = __import__("uuid").uuid4()


def _settings(creds_root: str = ""):
    """A stand-in settings object exposing just sandbox_creds_root (the M1 gate input)."""
    return MagicMock(sandbox_creds_root=creds_root)


def _ctx(**kw):
    base = dict(user_id=__import__("uuid").uuid4(), session_id=_SID)
    base.update(kw)
    return ToolExecContext(**base)


# --------------------------------------------------------------------------- #
# Dispatch fallback to the session's EKS kubeconfig
# --------------------------------------------------------------------------- #


async def test_static_cluster_path_unchanged():
    """A statically-configured cluster still wins — no EKS resolution, no extra env."""
    with patch(
        "app.tools.dispatch.execute_kubectl", new=AsyncMock(return_value=("ok", False))
    ) as ek:
        out, err = await execute_tool(
            "kubectl_get", {"resource": "pods"}, _ctx(kubeconfig_path="/legacy", context_name="ctx")
        )
    assert (out, err) == ("ok", False)
    args, kwargs = ek.call_args
    assert args[2] == "/legacy"  # kubeconfig
    assert args[3] == "ctx"  # context
    assert kwargs["extra_env"] is None


async def test_agent_falls_back_to_kubeconfig_env_without_touching_store():
    """Sandboxed agent (projected_creds): use the broker-set KUBECONFIG, never call the
    portal credential store (one-way handoff)."""
    store = AsyncMock()
    with (
        patch.dict(os.environ, {"KUBECONFIG": "/home/sandbox/.kube/config"}),
        patch("app.tools.dispatch.get_or_renew_cred", new=store),
        patch(
            "app.tools.dispatch.execute_kubectl", new=AsyncMock(return_value=("ok", False))
        ) as ek,
    ):
        out, err = await execute_tool(
            "kubectl_get", {"resource": "pods"}, _ctx(projected_creds=True)
        )
    assert (out, err) == ("ok", False)
    assert ek.call_args.args[2] == "/home/sandbox/.kube/config"
    assert ek.call_args.kwargs["extra_env"] is None
    store.assert_not_called()  # portal store must never be reached from the agent


async def test_agent_without_kubeconfig_env_reports_no_cluster():
    """Agent with no EKS kubeconfig in env → the graceful 'no cluster' message."""
    env = {k: v for k, v in os.environ.items() if k != "KUBECONFIG"}
    with (
        patch.dict(os.environ, env, clear=True),
        patch("app.tools.dispatch.execute_kubectl", new=AsyncMock()) as ek,
    ):
        out, err = await execute_tool(
            "kubectl_get", {"resource": "pods"}, _ctx(projected_creds=True)
        )
    assert err is True
    assert "No Kubernetes cluster is configured" in out
    ek.assert_not_called()


async def test_backend_resolves_session_kubeconfig_and_projects_aws_env():
    """Backend-run providers: resolve the session's merged config and project ~/.aws so
    the exec plugin can mint a token (renew-on-read keeps the creds fresh)."""
    cred = MagicMock()
    cred.secret = {"profiles": [{"name": "acme"}]}
    with (
        patch("app.tools.dispatch.get_settings", return_value=_settings(creds_root="")),
        patch("app.tools.dispatch.session_kubeconfigs", return_value=["/shm/sid/kube/config"]),
        patch("app.tools.dispatch.get_or_renew_cred", new=AsyncMock(return_value=cred)),
        patch("app.tools.dispatch.write_aws_profiles", return_value={"AWS_PROFILE": "acme"}) as wap,
        patch(
            "app.tools.dispatch.execute_kubectl", new=AsyncMock(return_value=("ok", False))
        ) as ek,
    ):
        out, err = await execute_tool("kubectl_get", {"resource": "pods"}, _ctx())
    assert (out, err) == ("ok", False)
    assert ek.call_args.args[2] == "/shm/sid/kube/config"
    assert ek.call_args.kwargs["extra_env"] == {"AWS_PROFILE": "acme"}
    wap.assert_called_once()


async def test_backend_with_sandbox_creds_volume_does_not_attempt_eks():
    """M1: with a shared sandbox creds volume the merged kubeconfig's exec block carries
    in-container ~/.aws paths the backend can't read — so a backend-run provider must NOT
    attempt EKS (it would fail get-token); report 'no cluster' without touching the store."""
    store = AsyncMock()
    with (
        patch("app.tools.dispatch.get_settings", return_value=_settings(creds_root="/creds")),
        patch("app.tools.dispatch.session_kubeconfigs") as skc,
        patch("app.tools.dispatch.get_or_renew_cred", new=store),
        patch("app.tools.dispatch.execute_kubectl", new=AsyncMock()) as ek,
    ):
        out, err = await execute_tool("kubectl_get", {"resource": "pods"}, _ctx())
    assert err is True
    assert "No Kubernetes cluster is configured" in out
    skc.assert_not_called()  # gated before resolving — no store, no kubeconfig lookup
    store.assert_not_called()
    ek.assert_not_called()


async def test_backend_without_discovered_clusters_reports_no_cluster():
    """No legacy cluster and no discovered EKS kubeconfig → graceful 'no cluster'."""
    with (
        patch("app.tools.dispatch.get_settings", return_value=_settings(creds_root="")),
        patch("app.tools.dispatch.session_kubeconfigs", return_value=[]),
        patch("app.tools.dispatch.execute_kubectl", new=AsyncMock()) as ek,
    ):
        out, err = await execute_tool("kubectl_get", {"resource": "pods"}, _ctx())
    assert err is True
    assert "No Kubernetes cluster is configured" in out
    ek.assert_not_called()


async def test_per_call_context_overrides_session_context():
    """A `context` on the tool input targets a specific discovered cluster, overriding
    the session's configured context."""
    with patch(
        "app.tools.dispatch.execute_kubectl", new=AsyncMock(return_value=("ok", False))
    ) as ek:
        await execute_tool(
            "kubectl_get",
            {"resource": "pods", "context": "acme--eu-west-1--prod"},
            _ctx(kubeconfig_path="/legacy", context_name="default-ctx"),
        )
    assert ek.call_args.args[3] == "acme--eu-west-1--prod"


# --------------------------------------------------------------------------- #
# `context` schema + validation
# --------------------------------------------------------------------------- #


def test_every_kubectl_tool_exposes_context():
    """All kubectl tools must advertise the optional `context` field (DRY injection)."""
    for tool in definitions.KUBECTL_TOOLS:
        assert "context" in tool["input_schema"]["properties"], tool["name"]


def test_context_accepts_eks_alias():
    """A region-qualified EKS alias (and a hash-suffixed one) must validate."""
    ok = validate_tool_input(
        "kubectl_get", {"resource": "pods", "context": "acme--eu-west-1--prod"}
    )
    assert ok is None
    assert (
        validate_tool_input(
            "kubectl_get", {"resource": "pods", "context": "acme--eu-west-1--prod--1a2b3c4d"}
        )
        is None
    )


def test_context_rejects_flag_shaped_value():
    """`context` lands after '--context'; a flag-shaped value must be rejected."""
    err = validate_tool_input("kubectl_get", {"resource": "pods", "context": "--kubeconfig=/x"})
    assert err is not None
    assert "Invalid context" in err
    assert validate_tool_input("kubectl_get", {"resource": "pods", "context": "-A"}) is not None


# --------------------------------------------------------------------------- #
# session_kube_contexts helper
# --------------------------------------------------------------------------- #


def test_session_kube_contexts_reads_sorted_names(tmp_path):
    """Returns the merged config's context names, sorted; [] when the file is absent."""
    import yaml

    from app.tools.connector_exec import session_kube_contexts

    sid = __import__("uuid").uuid4()
    with patch("app.tools.connector_exec.session_kube_dir", return_value=str(tmp_path)):
        assert session_kube_contexts(sid) == []  # no config yet
        doc = {"contexts": [{"name": "b--r--c2"}, {"name": "a--r--c1"}]}
        (tmp_path / "config").write_text(yaml.safe_dump(doc))
        assert session_kube_contexts(sid) == ["a--r--c1", "b--r--c2"]


# --------------------------------------------------------------------------- #
# execute_kubectl env wiring (the exec plugin's AWS creds ride extra_env)
# --------------------------------------------------------------------------- #


async def test_execute_kubectl_merges_extra_env_onto_process_env():
    """extra_env is merged onto os.environ for the subprocess (so the exec plugin sees the
    session's AWS profile env) without dropping the inherited PATH/proxy vars."""
    captured: dict = {}

    def fake_run(cmd, **kw):
        captured["env"] = kw.get("env")
        return MagicMock(returncode=0, stdout="ok", stderr="")

    with patch("app.tools.executor.subprocess.run", side_effect=fake_run):
        out, err = await execute_kubectl(
            "kubectl_get", {"resource": "pods"}, "/kc", None, extra_env={"AWS_PROFILE": "acme"}
        )
    assert (out, err) == ("ok", False)
    assert captured["env"]["AWS_PROFILE"] == "acme"
    assert "PATH" in captured["env"]  # inherited env preserved, not replaced


async def test_execute_kubectl_inherits_env_when_no_extra():
    """The legacy statically-configured path passes env=None (inherit unchanged)."""
    captured: dict = {}

    def fake_run(cmd, **kw):
        captured["env"] = kw.get("env")
        return MagicMock(returncode=0, stdout="ok", stderr="")

    with patch("app.tools.executor.subprocess.run", side_effect=fake_run):
        await execute_kubectl("kubectl_get", {"resource": "pods"}, "/kc")
    assert captured["env"] is None


# --------------------------------------------------------------------------- #
# _safe_seg keeps generated context aliases targetable (matches CONTEXT_PATTERN)
# --------------------------------------------------------------------------- #


def test_safe_seg_forces_leading_alphanumeric():
    """A display name beginning with '.'/'-'/'_' must not yield a context alias that
    CONTEXT_PATTERN (and so the kubectl `context` arg) would reject."""
    from app.services.eks_refresh import _safe_seg
    from app.tools.validation import CONTEXT_PATTERN

    for raw in ("-acme", ".hidden", "_x", "/", "...", "9-fine", "normal"):
        seg = _safe_seg(raw)
        assert seg and seg[0].isalnum(), raw
        # The assembled alias (seg--seg--seg) must validate as a targetable context.
        assert CONTEXT_PATTERN.match(f"{seg}--eu-west-1--cluster-blue"), raw
