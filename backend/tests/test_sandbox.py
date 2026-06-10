"""
Tests for the per-conversation Docker sandbox manager.

All Docker interaction is mocked (``sandbox._docker`` / ``_is_running`` /
``_wait_healthy``), so these run without a Docker daemon.
"""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import sandbox


def _settings(tmp_path):
    return SimpleNamespace(
        sandbox_docker_path="docker",
        sandbox_toolbox_image="toolbox:latest",
        sandbox_home="/home/sandbox",
        sandbox_squid_image="squid:latest",
        sandbox_squid_conf="",
        sandbox_squid_errors="",
        sandbox_proxy_dns="",
        sandbox_env_file="",
        sandbox_sessions_root=str(tmp_path),
        sandbox_creds_root="",
        sandbox_idle_ttl=1800,
        sandbox_provision_timeout=5,
        sandbox_cpu_limit="1.0",
        sandbox_memory_limit="1g",
        sandbox_pids_limit=512,
    )


@pytest.fixture(autouse=True)
def _clear_registry():
    sandbox._sandboxes.clear()
    sandbox._locks.clear()
    yield
    sandbox._sandboxes.clear()
    sandbox._locks.clear()


def test_names_strip_dashes_and_are_unique():
    names = sandbox._names("550e8400-e29b-41d4-a716-446655440000")
    assert names.conv == "inkwest_conv_550e8400e29b41d4a716446655440000"
    assert names.proxy == "inkwest_proxy_550e8400e29b41d4a716446655440000"
    assert names.int_net.startswith("inkwest_intnet_")
    assert names.ext_net.startswith("inkwest_extnet_")
    assert "-" not in names.conv  # Docker-safe


def test_path_helpers(tmp_path):
    with patch.object(sandbox, "get_settings", return_value=_settings(tmp_path)):
        assert sandbox.session_dir("abc") == str(tmp_path / "abc")
        assert sandbox.squid_log_path("abc") == str(
            tmp_path / "abc" / "logs" / "squid" / "access.log"
        )


@pytest.mark.asyncio
async def test_ensure_provisions_once_under_concurrency(tmp_path):
    fake = sandbox.Sandbox("s1", "inkwest_conv_s1", "inkwest_proxy_s1", "i", "e", 0.0)
    prov = AsyncMock(return_value=fake)
    with (
        patch.object(sandbox, "get_settings", return_value=_settings(tmp_path)),
        patch.object(sandbox, "provision", prov),
        patch.object(sandbox, "_is_running", AsyncMock(return_value=True)),
    ):
        results = await asyncio.gather(*(sandbox.ensure("s1") for _ in range(5)))

    # The per-session lock serializes the racers: one provisions, four reuse.
    assert results == ["inkwest_conv_s1"] * 5
    assert prov.await_count == 1


@pytest.mark.asyncio
async def test_ensure_rebuilds_when_pair_not_running(tmp_path):
    fake = sandbox.Sandbox("s2", "inkwest_conv_s2", "inkwest_proxy_s2", "i", "e", 0.0)
    sandbox._sandboxes["s2"] = fake
    prov = AsyncMock(return_value=fake)
    td = AsyncMock()
    with (
        patch.object(sandbox, "get_settings", return_value=_settings(tmp_path)),
        patch.object(sandbox, "provision", prov),
        patch.object(sandbox, "_teardown_unlocked", td),
        patch.object(sandbox, "_is_running", AsyncMock(return_value=False)),
    ):
        name = await sandbox.ensure("s2")

    assert name == "inkwest_conv_s2"
    td.assert_awaited_once_with("s2", remove_dir=False)  # soft: keep dir for resume
    prov.assert_awaited_once()


@pytest.mark.asyncio
async def test_teardown_hard_removes_dir(tmp_path):
    (tmp_path / "sid").mkdir()
    sandbox._sandboxes["sid"] = sandbox.Sandbox("sid", "c", "p", "i", "e", 0.0)
    with (
        patch.object(sandbox, "get_settings", return_value=_settings(tmp_path)),
        patch.object(sandbox, "_docker", AsyncMock(return_value=(0, "", ""))),
    ):
        await sandbox.teardown("sid")

    assert not (tmp_path / "sid").exists()
    assert "sid" not in sandbox._sandboxes


@pytest.mark.asyncio
async def test_teardown_soft_keeps_dir(tmp_path):
    (tmp_path / "sid").mkdir()
    with (
        patch.object(sandbox, "get_settings", return_value=_settings(tmp_path)),
        patch.object(sandbox, "_docker", AsyncMock(return_value=(0, "", ""))),
    ):
        await sandbox.teardown("sid", remove_dir=False)

    assert (tmp_path / "sid").exists()


@pytest.mark.asyncio
async def test_teardown_serializes_on_session_lock(tmp_path):
    """H2: delete-path teardown must take the per-session lock so it can't `docker rm -f`
    a pair while a turn holds the lock provisioning/using the same conversation."""
    settings = _settings(tmp_path)
    sandbox._sandboxes["sid"] = sandbox.Sandbox("sid", "c", "p", "i", "e", 0.0)
    with (
        patch.object(sandbox, "get_settings", return_value=settings),
        patch.object(sandbox, "_docker", AsyncMock(return_value=(0, "", ""))),
    ):
        lock = sandbox._locks["sid"]
        await lock.acquire()  # simulate a turn holding the lock
        try:
            task = asyncio.create_task(sandbox.teardown("sid", remove_dir=False))
            await asyncio.sleep(0.05)
            assert not task.done()  # blocked on the lock
            assert "sid" in sandbox._sandboxes  # teardown hasn't run
        finally:
            lock.release()
        await task
        assert "sid" not in sandbox._sandboxes  # ran once the lock was free


@pytest.mark.asyncio
async def test_provision_fails_fast_when_toolbox_image_missing(tmp_path):
    settings = _settings(tmp_path)
    calls: list[tuple[str, ...]] = []

    async def fake_docker(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("image", "inspect"):
            return (1, "", "No such image: toolbox:latest")
        return (0, "", "")

    with (
        patch.object(sandbox, "get_settings", return_value=settings),
        patch.object(sandbox, "_docker", fake_docker),
    ):
        with pytest.raises(RuntimeError, match="make sandbox-pull"):
            await sandbox.provision("sid")

    # Failed before creating anything — no containers or networks to roll back.
    assert not [a for a in calls if a[0] in ("run", "network")]


@pytest.mark.asyncio
async def test_provision_rolls_back_on_failure(tmp_path):
    settings = _settings(tmp_path)
    calls: list[tuple[str, ...]] = []

    async def fake_docker(*args, **kwargs):
        calls.append(args)
        # Fail when launching the conversation container (run with the toolbox image).
        if args[0] == "run" and settings.sandbox_toolbox_image in args:
            raise RuntimeError("boom")
        return (0, "", "")

    with (
        patch.object(sandbox, "get_settings", return_value=settings),
        patch.object(sandbox, "_docker", fake_docker),
        patch.object(sandbox, "_wait_healthy", AsyncMock()),
    ):
        with pytest.raises(RuntimeError, match="boom"):
            await sandbox.provision("sid")

    # Pre-clean + rollback each remove the pair -> at least two `rm -f` invocations.
    assert sum(1 for a in calls if a[0] == "rm") >= 2


@pytest.mark.asyncio
async def test_provision_binds_shared_creds_volume(tmp_path):
    """With a creds volume configured, the conversation container bind-mounts this
    session's `<id>/{.aws,kube}` read-only (instead of an empty tmpfs), and the source
    dirs are pre-created so the daemon binds an existing path before the user grants."""
    settings = _settings(tmp_path)
    settings.sandbox_creds_root = str(tmp_path / "creds")
    conv_run: list[str] = []

    async def fake_docker(*args, **kwargs):
        if args[0] == "run" and settings.sandbox_toolbox_image in args:
            conv_run.extend(args)
        return (0, "", "")

    with (
        patch.object(sandbox, "get_settings", return_value=settings),
        patch.object(sandbox, "_docker", fake_docker),
        patch.object(sandbox, "_wait_healthy", AsyncMock()),
    ):
        await sandbox.provision("sid")

    aws_src = str(tmp_path / "creds" / "sid" / ".aws")
    kube_src = str(tmp_path / "creds" / "sid" / "kube")
    assert f"{aws_src}:/home/sandbox/.aws:ro" in conv_run
    assert f"{kube_src}:/home/sandbox/.kube:ro" in conv_run
    assert "--tmpfs" not in conv_run  # bind mounts replace the empty tmpfs
    # Source dirs exist and are traversable by the toolbox's (differing) uid.
    assert os.path.isdir(aws_src) and (os.stat(aws_src).st_mode & 0o777) == 0o755
    assert os.path.isdir(kube_src)


@pytest.mark.asyncio
async def test_reap_orphans_removes_labelled_objects(tmp_path):
    docker = AsyncMock(
        side_effect=[
            (0, "c1\nc2", ""),  # ps -aq
            (0, "", ""),  # rm -f c1 c2
            (0, "n1\nn2", ""),  # network ls -q
            (0, "", ""),  # network rm n1
            (0, "", ""),  # network rm n2
        ]
    )
    with (
        patch.object(sandbox, "get_settings", return_value=_settings(tmp_path)),
        patch.object(sandbox, "_docker", docker),
    ):
        await sandbox.reap_orphans()

    rm_calls = [c for c in docker.await_args_list if c.args[0] == "rm"]
    assert rm_calls and "c1" in rm_calls[0].args and "c2" in rm_calls[0].args
    net_rm = [c for c in docker.await_args_list if c.args[:2] == ("network", "rm")]
    assert {c.args[2] for c in net_rm} == {"n1", "n2"}


@pytest.mark.asyncio
async def test_reap_idle_tears_down_idle_sandbox(tmp_path):
    settings = _settings(tmp_path)
    settings.sandbox_idle_ttl = 100
    sandbox._sandboxes["old"] = sandbox.Sandbox("old", "c", "p", "i", "e", last_used=0.0)
    td = AsyncMock()
    with (
        patch.object(sandbox, "get_settings", return_value=settings),
        patch.object(sandbox, "_now", return_value=1000.0),
        patch.object(sandbox, "_teardown_unlocked", td),
    ):
        await sandbox._reap_idle()
    td.assert_awaited_once_with("old", remove_dir=False)  # soft teardown keeps the dir


@pytest.mark.asyncio
async def test_reap_idle_skips_recently_used(tmp_path):
    settings = _settings(tmp_path)
    settings.sandbox_idle_ttl = 100
    sandbox._sandboxes["fresh"] = sandbox.Sandbox("fresh", "c", "p", "i", "e", last_used=950.0)
    td = AsyncMock()
    with (
        patch.object(sandbox, "get_settings", return_value=settings),
        patch.object(sandbox, "_now", return_value=1000.0),
        patch.object(sandbox, "_teardown_unlocked", td),
    ):
        await sandbox._reap_idle()
    td.assert_not_awaited()


@pytest.mark.asyncio
async def test_reap_idle_skips_session_refreshed_under_lock(tmp_path):
    """H1 race: a turn refreshes last_used after the candidate snapshot but before the
    reaper acquires the lock — the under-lock re-check must skip the (now-live) sandbox."""
    settings = _settings(tmp_path)
    settings.sandbox_idle_ttl = 100
    sb = sandbox.Sandbox("racy", "c", "p", "i", "e", last_used=0.0)  # stale at snapshot
    sandbox._sandboxes["racy"] = sb

    class _RefreshingLock:
        async def __aenter__(self) -> "object":
            sb.last_used = 1000.0  # a turn grabbed the lock first and refreshed it
            return self

        async def __aexit__(self, *_: object) -> bool:
            return False

    sandbox._locks["racy"] = _RefreshingLock()  # type: ignore[assignment]
    td = AsyncMock()
    with (
        patch.object(sandbox, "get_settings", return_value=settings),
        patch.object(sandbox, "_now", return_value=1000.0),
        patch.object(sandbox, "_teardown_unlocked", td),
    ):
        await sandbox._reap_idle()
    td.assert_not_awaited()  # re-check under the lock saw the refresh -> no force-kill
