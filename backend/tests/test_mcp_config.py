"""Tests for the MCP -> .claude.json merge (app.services.mcp_config)."""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.services.mcp_config import (
    apply_session_mcp_selection,
    merge_mcp_selection,
)
from app.services.mcp_oauth_catalog import OAuthMcpServer

CORALOGIX = {"command": "npx", "args": ["coralogix-mcp"]}
GITLAB = {"command": "npx", "args": ["gitlab-mcp"]}
_USER = uuid.UUID("00000000-0000-4000-8000-000000000001")
_SID = "00000000-0000-4000-8000-000000000002"


def _oauth_db(status_rows: list[tuple[str, str]]) -> AsyncMock:
    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(all=lambda: status_rows))
    return db


def _setup_oauth(monkeypatch, tmp_path, *, base_url: str = "http://alias:8000"):
    from app.services import mcp_config

    path = tmp_path / "claude" / ".claude.json"
    monkeypatch.setattr(mcp_config, "claude_json_path", lambda _sid: str(path))
    monkeypatch.setattr(mcp_config, "get_mcp_catalog", lambda: {})
    monkeypatch.setattr(
        mcp_config,
        "get_oauth_catalog",
        lambda: {"notion": OAuthMcpServer("notion", {"type": "http", "url": "https://m/mcp"})},
    )
    monkeypatch.setattr(
        mcp_config, "get_settings", lambda: SimpleNamespace(mcp_proxy_base_url=base_url)
    )
    return path


def _servers(path) -> dict:
    """mcpServers from the written config, or {} when nothing was written (file absent)."""
    if not path.exists():
        return {}
    return json.loads(path.read_text()).get("mcpServers", {})


class TestMergeMcpSelection:
    def test_adds_enabled_into_empty_config(self):
        out = merge_mcp_selection({}, {"coralogix": CORALOGIX}, {"coralogix", "gitlab"})
        assert out == {"mcpServers": {"coralogix": CORALOGIX}}

    def test_prunes_deselected_catalog_entry(self):
        config = {"mcpServers": {"coralogix": CORALOGIX, "gitlab": GITLAB}}
        # Only gitlab stays enabled -> coralogix (catalog-managed) is removed.
        out = merge_mcp_selection(config, {"gitlab": GITLAB}, {"coralogix", "gitlab"})
        assert out["mcpServers"] == {"gitlab": GITLAB}

    def test_preserves_non_catalog_entries(self):
        config = {"mcpServers": {"custom": {"command": "x"}}}
        out = merge_mcp_selection(config, {"coralogix": CORALOGIX}, {"coralogix"})
        # 'custom' isn't a catalog key, so it survives untouched alongside the new one.
        assert out["mcpServers"] == {"custom": {"command": "x"}, "coralogix": CORALOGIX}

    def test_keeps_other_top_level_keys(self):
        config = {"theme": "dark", "mcpServers": {}}
        out = merge_mcp_selection(config, {"coralogix": CORALOGIX}, {"coralogix"})
        assert out["theme"] == "dark"

    def test_empty_selection_removes_mcpservers_key(self):
        config = {"mcpServers": {"coralogix": CORALOGIX}}
        out = merge_mcp_selection(config, {}, {"coralogix"})
        assert "mcpServers" not in out


class TestApplySessionMcpSelection:
    async def test_writes_merged_file_in_sandbox_mode(self, tmp_path, monkeypatch):
        from app.services import mcp_config

        path = tmp_path / "claude" / ".claude.json"
        monkeypatch.setattr(mcp_config, "claude_json_path", lambda _sid: str(path))
        monkeypatch.setattr(mcp_config, "get_mcp_catalog", lambda: {"coralogix": CORALOGIX})
        monkeypatch.setattr(mcp_config, "get_oauth_catalog", lambda: {})
        monkeypatch.setattr(
            mcp_config, "build_mcp_servers", lambda keys: {"coralogix": CORALOGIX} if keys else {}
        )

        # user_id=None → oauth path skipped; static-only injection still applies.
        dead = await apply_session_mcp_selection("sess-1", ["coralogix"], user_id=None, db=None)

        assert dead == []
        written = json.loads(path.read_text())
        assert written["mcpServers"] == {"coralogix": CORALOGIX}

    async def test_noop_when_path_is_none(self, monkeypatch):
        from app.services import mcp_config

        monkeypatch.setattr(mcp_config, "claude_json_path", lambda _sid: None)
        # Should not raise even though no file exists / no catalog set.
        dead = await apply_session_mcp_selection("sess-1", ["coralogix"], user_id=None, db=None)
        assert dead == []


class TestOauthInjection:
    async def test_active_grant_injects_proxy_entry(self, tmp_path, monkeypatch):
        path = _setup_oauth(monkeypatch, tmp_path)
        db = _oauth_db([("notion", "active")])

        dead = await apply_session_mcp_selection(_SID, ["notion"], user_id=_USER, db=db)

        assert dead == []
        entry = json.loads(path.read_text())["mcpServers"]["notion"]
        assert entry["type"] == "http"
        assert entry["url"] == "http://alias:8000/api/mcp-proxy/notion/"
        assert entry["headers"]["Authorization"].startswith("Bearer ")
        # The injected file carries the internal JWT, never the real upstream token.
        assert "access_token" not in path.read_text()
        # Integration seam: the injected JWT must verify at the proxy for this (user, key).
        from app.services.mcp_proxy_token import verify

        claims = verify(entry["headers"]["Authorization"][len("Bearer ") :], mcp_key="notion")
        assert claims.user_id == _USER and claims.mcp_key == "notion"

    async def test_inactive_grant_is_skipped_and_reported_dead(self, tmp_path, monkeypatch):
        path = _setup_oauth(monkeypatch, tmp_path)
        db = _oauth_db([("notion", "expired")])

        dead = await apply_session_mcp_selection(_SID, ["notion"], user_id=_USER, db=db)

        assert dead == ["notion"]
        assert "notion" not in _servers(path)

    async def test_no_grant_row_is_dead(self, tmp_path, monkeypatch):
        _setup_oauth(monkeypatch, tmp_path)
        db = _oauth_db([])  # no token row at all

        dead = await apply_session_mcp_selection(_SID, ["notion"], user_id=_USER, db=db)
        assert dead == ["notion"]

    async def test_mint_stable_keeps_file_unchanged_across_applies(self, tmp_path, monkeypatch):
        path = _setup_oauth(monkeypatch, tmp_path)
        db = _oauth_db([("notion", "active")])

        await apply_session_mcp_selection(_SID, ["notion"], user_id=_USER, db=db)
        first = path.read_text()
        await apply_session_mcp_selection(_SID, ["notion"], user_id=_USER, db=db)
        # Stable JWT (>1h left) → identical bytes → no needless rewrite.
        assert path.read_text() == first

    async def test_oauth_skipped_without_proxy_base_url(self, tmp_path, monkeypatch):
        path = _setup_oauth(monkeypatch, tmp_path, base_url="")
        db = _oauth_db([("notion", "active")])

        dead = await apply_session_mcp_selection(_SID, ["notion"], user_id=_USER, db=db)

        # Feature not wired (no base url): skipped silently, not reported as a dead grant.
        assert dead == []
        assert "notion" not in _servers(path)


# --- static HTTP brokering (build_session_static_servers) ------------------------------

_HTTP_DEF = {"type": "http", "url": "https://cx.example/mcp", "headers": {"X-API-Key": "SECRET"}}
_STDIO_DEF = {"command": "npx", "args": ["some-mcp"]}


def _patch_catalog(monkeypatch, catalog: dict) -> None:
    """Patch the catalog at BOTH bindings: mcp_config's imported name (brokering branch) and
    the mcp_catalog source (the verbatim build_mcp_servers fallback reads its own module)."""
    from app.services import mcp_catalog, mcp_config

    monkeypatch.setattr(mcp_config, "get_mcp_catalog", lambda: catalog)
    monkeypatch.setattr(mcp_catalog, "get_mcp_catalog", lambda: catalog)


def test_build_session_static_servers_brokers_http(monkeypatch):
    from app.services import mcp_config

    _patch_catalog(monkeypatch, {"cx": _HTTP_DEF})
    out = mcp_config.build_session_static_servers(
        ["cx"], base="http://alias:8000", user_id=_USER, session_id=_SID, existing={}
    )
    assert out["cx"]["url"] == "http://alias:8000/api/mcp-proxy/cx/"
    assert out["cx"]["type"] == "http"
    assert out["cx"]["headers"]["Authorization"].startswith("Bearer ")
    assert "SECRET" not in json.dumps(out)  # real header never materialized


def test_build_session_static_servers_stdio_verbatim(monkeypatch):
    from app.services import mcp_config

    _patch_catalog(monkeypatch, {"s": _STDIO_DEF})
    out = mcp_config.build_session_static_servers(
        ["s"], base="http://alias:8000", user_id=_USER, session_id=_SID, existing={}
    )
    assert out == {"s": _STDIO_DEF}  # stdio cannot be brokered — injected verbatim


def test_build_session_static_servers_verbatim_when_base_unset(monkeypatch):
    from app.services import mcp_config

    _patch_catalog(monkeypatch, {"cx": _HTTP_DEF})
    out = mcp_config.build_session_static_servers(
        ["cx"], base="", user_id=_USER, session_id=_SID, existing={}
    )
    assert out == {"cx": _HTTP_DEF}  # broker unwired -> today's behavior (cred in container)


def test_build_session_static_servers_verbatim_when_no_user(monkeypatch):
    from app.services import mcp_config

    _patch_catalog(monkeypatch, {"cx": _HTTP_DEF})
    out = mcp_config.build_session_static_servers(
        ["cx"], base="http://alias:8000", user_id=None, session_id=_SID, existing={}
    )
    assert out == {"cx": _HTTP_DEF}  # no user to bind a token to -> verbatim


def test_build_session_static_servers_reuses_prior_jwt(monkeypatch):
    """Stable JWT: an unchanged selection reuses the prior token so the .claude.json write
    stays a no-op across turns."""
    from app.services import mcp_config

    _patch_catalog(monkeypatch, {"cx": _HTTP_DEF})
    first = mcp_config.build_session_static_servers(
        ["cx"], base="http://alias:8000", user_id=_USER, session_id=_SID, existing={}
    )
    second = mcp_config.build_session_static_servers(
        ["cx"], base="http://alias:8000", user_id=_USER, session_id=_SID, existing=first
    )
    assert first["cx"]["headers"] == second["cx"]["headers"]  # token reused, not re-minted
