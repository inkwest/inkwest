"""Tests for the MCP catalog + per-session selection + OAuth2 brokering API routes."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient


def _catalog_db(mock_db: AsyncMock, token_rows: list[tuple[str, str]] | None = None) -> None:
    """Stub the per-user token query the catalog route runs (db.execute(...).all())."""
    result = MagicMock()
    result.all.return_value = token_rows or []
    mock_db.execute = AsyncMock(return_value=result)


@pytest.mark.asyncio
async def test_get_catalog_keys_backward_compatible(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    """`keys` keeps the pre-OAuth contract: static catalog keys only."""
    _catalog_db(mock_db)
    with (
        patch("app.api.routes.mcp.catalog_keys", return_value=["coralogix", "gitlab"]),
        patch("app.api.routes.mcp.oauth_catalog_keys", return_value=[]),
    ):
        resp = await authenticated_client.get("/api/mcp/catalog")
    assert resp.status_code == 200
    body = resp.json()
    assert body["keys"] == ["coralogix", "gitlab"]
    assert all(s["kind"] == "static" and s["auth_status"] == "static" for s in body["servers"])


@pytest.mark.asyncio
async def test_get_catalog_auth_status_values(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    """auth_status: connected (active row), needs_auth (expired row), not_connected (no row)."""
    _catalog_db(mock_db, token_rows=[("notion", "active"), ("slack", "expired")])
    with (
        patch("app.api.routes.mcp.catalog_keys", return_value=["coralogix"]),
        patch("app.api.routes.mcp.oauth_catalog_keys", return_value=["notion", "slack", "drive"]),
        patch("app.api.routes.mcp.get_settings") as settings,
    ):
        settings.return_value.sandbox_enabled = True
        resp = await authenticated_client.get("/api/mcp/catalog")
    statuses = {s["key"]: s["auth_status"] for s in resp.json()["servers"]}
    assert statuses == {
        "coralogix": "static",
        "notion": "connected",
        "slack": "needs_auth",
        "drive": "not_connected",
    }


@pytest.mark.asyncio
async def test_get_catalog_oauth_unavailable_without_sandbox(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    _catalog_db(mock_db)
    with (
        patch("app.api.routes.mcp.catalog_keys", return_value=[]),
        patch("app.api.routes.mcp.oauth_catalog_keys", return_value=["notion"]),
        patch("app.api.routes.mcp.get_settings") as settings,
    ):
        settings.return_value.sandbox_enabled = False
        resp = await authenticated_client.get("/api/mcp/catalog")
    statuses = {s["key"]: s["auth_status"] for s in resp.json()["servers"]}
    assert statuses == {"notion": "unavailable"}


@pytest.mark.asyncio
async def test_auth_start_unknown_key_404(authenticated_client: AsyncClient) -> None:
    with patch("app.api.routes.mcp.get_oauth_server", return_value=None):
        resp = await authenticated_client.post("/api/mcp/nope/auth/start")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_auth_start_requires_sandbox(authenticated_client: AsyncClient) -> None:
    with (
        patch("app.api.routes.mcp.get_oauth_server", return_value=MagicMock()),
        patch("app.api.routes.mcp.get_settings") as settings,
    ):
        settings.return_value.sandbox_enabled = False
        resp = await authenticated_client.post("/api/mcp/notion/auth/start")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_auth_start_returns_authorize_url(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    # The route counts the user's in-flight pending states (rate limit) before proceeding.
    mock_db.execute = AsyncMock(return_value=MagicMock(scalar_one=lambda: 0))
    with (
        patch("app.api.routes.mcp.get_oauth_server", return_value=MagicMock()),
        patch("app.api.routes.mcp.get_settings") as settings,
        patch(
            "app.api.routes.mcp.mcp_oauth.build_authorize_url",
            new=AsyncMock(return_value="https://as.example.com/authorize?x=1"),
        ),
    ):
        settings.return_value.sandbox_enabled = True
        resp = await authenticated_client.post("/api/mcp/notion/auth/start")
    assert resp.status_code == 200
    assert resp.json()["authorize_url"].startswith("https://as.example.com/authorize")


@pytest.mark.asyncio
async def test_auth_start_rate_limited_when_too_many_pending(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    """At/over the per-user in-flight cap, auth/start returns 429 and never runs discovery."""
    from app.api.routes.mcp import _MAX_PENDING_PER_USER

    mock_db.execute = AsyncMock(return_value=MagicMock(scalar_one=lambda: _MAX_PENDING_PER_USER))
    build = AsyncMock(return_value="https://as.example.com/authorize?x=1")
    with (
        patch("app.api.routes.mcp.get_oauth_server", return_value=MagicMock()),
        patch("app.api.routes.mcp.get_settings") as settings,
        patch("app.api.routes.mcp.mcp_oauth.build_authorize_url", new=build),
    ):
        settings.return_value.sandbox_enabled = True
        resp = await authenticated_client.post("/api/mcp/notion/auth/start")
    assert resp.status_code == 429
    build.assert_not_awaited()  # rejected before any outbound discovery


@pytest.mark.asyncio
async def test_auth_start_discovery_failure_is_non_retryable_424(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    """A discovery/DCR failure is deterministic → 424 Failed Dependency: a 4xx (kept out of the
    5xx crash bucket, so a connector misconfig can't trip false outage pages) that is also
    non-retryable (∉ the SPA's 502/503/504 retry set, so no false "backend unavailable" banner).
    """
    from app.services.mcp_oauth import McpOauthError

    mock_db.execute = AsyncMock(return_value=MagicMock(scalar_one=lambda: 0))
    with (
        patch("app.api.routes.mcp.get_oauth_server", return_value=MagicMock()),
        patch("app.api.routes.mcp.get_settings") as settings,
        patch(
            "app.api.routes.mcp.mcp_oauth.build_authorize_url",
            new=AsyncMock(side_effect=McpOauthError("no usable authorization-server metadata")),
        ),
    ):
        settings.return_value.sandbox_enabled = True
        resp = await authenticated_client.post("/api/mcp/notion/auth/start")
    assert resp.status_code == 424
    assert "no usable" in resp.json()["detail"]  # the real reason is surfaced, not masked


@pytest.mark.asyncio
async def test_callback_success_audits_and_closes(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    import uuid

    row = MagicMock()
    row.user_id = uuid.uuid4()
    row.mcp_key = "notion"
    with patch("app.api.routes.mcp.mcp_oauth.exchange_code", new=AsyncMock(return_value=row)):
        resp = await authenticated_client.get(
            "/api/mcp/oauth/callback", params={"state": "s", "code": "c"}
        )
    assert resp.status_code == 200
    assert "window.close" in resp.text
    assert "Connected" in resp.text
    # Assert the audit row is the right shape, not just that *something* was added.
    audited = mock_db.add.call_args.args[0]
    assert audited.connector == "mcp"
    assert audited.action == "grant"
    assert audited.scope == "notion"
    assert audited.session_id is None  # user-level event


@pytest.mark.asyncio
async def test_callback_state_replay_rejected(authenticated_client: AsyncClient) -> None:
    """A replayed/forged state surfaces as a failure page, not a connection (CSRF guard)."""
    from app.services.mcp_oauth import McpOauthError

    with patch(
        "app.api.routes.mcp.mcp_oauth.exchange_code",
        new=AsyncMock(side_effect=McpOauthError("unknown or already-used state")),
    ):
        resp = await authenticated_client.get(
            "/api/mcp/oauth/callback", params={"state": "replayed", "code": "c"}
        )
    assert resp.status_code == 200  # popup page, not an API error
    assert "failed" in resp.text.lower()


@pytest.mark.asyncio
async def test_callback_no_code_fails(authenticated_client: AsyncClient) -> None:
    resp = await authenticated_client.get(
        "/api/mcp/oauth/callback", params={"state": "s", "error": "access_denied"}
    )
    assert resp.status_code == 200
    assert "failed" in resp.text.lower()


@pytest.mark.asyncio
async def test_disconnect_revokes_and_audits(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    _catalog_db(mock_db)
    with (
        patch("app.api.routes.mcp.get_oauth_server", return_value=MagicMock()),
        patch("app.api.routes.mcp.catalog_keys", return_value=[]),
        patch("app.api.routes.mcp.oauth_catalog_keys", return_value=["notion"]),
        patch("app.api.routes.mcp.get_settings") as settings,
        patch("app.api.routes.mcp.mcp_oauth.revoke_grant", new=AsyncMock()) as revoke,
    ):
        settings.return_value.sandbox_enabled = True
        resp = await authenticated_client.delete("/api/mcp/notion/auth")
    assert resp.status_code == 200
    revoke.assert_awaited_once()
    mock_db.add.assert_called()  # mcp_disconnect audit row


@pytest.mark.asyncio
async def test_get_session_mcp_returns_enabled(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_session_obj: MagicMock
) -> None:
    mock_session_obj.enabled_mcp_servers = ["coralogix"]
    result = MagicMock()
    result.scalar_one_or_none.return_value = mock_session_obj
    mock_db.execute = AsyncMock(return_value=result)

    resp = await authenticated_client.get(f"/api/sessions/{mock_session_obj.id}/mcp")
    assert resp.status_code == 200
    assert resp.json() == {"enabled": ["coralogix"]}


@pytest.mark.asyncio
async def test_set_session_mcp_persists_valid_keys(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_session_obj: MagicMock
) -> None:
    result = MagicMock()
    result.scalar_one_or_none.return_value = mock_session_obj
    mock_db.execute = AsyncMock(return_value=result)

    with (
        patch("app.services.mcp_catalog.catalog_keys", return_value=["coralogix", "gitlab"]),
        patch(
            "app.services.mcp_config.apply_session_mcp_selection",
            new=AsyncMock(return_value=[]),
        ) as apply,
    ):
        resp = await authenticated_client.put(
            f"/api/sessions/{mock_session_obj.id}/mcp",
            json={"enabled": ["coralogix", "coralogix"]},  # dupes collapse
        )

    assert resp.status_code == 200
    assert resp.json() == {"enabled": ["coralogix"]}
    assert mock_session_obj.enabled_mcp_servers == ["coralogix"]
    apply.assert_awaited_once()
    args, kwargs = apply.await_args
    assert args == (str(mock_session_obj.id), ["coralogix"])
    assert "user_id" in kwargs and "db" in kwargs  # now user-aware + async


@pytest.mark.asyncio
async def test_set_session_mcp_rejects_unknown_key(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_session_obj: MagicMock
) -> None:
    result = MagicMock()
    result.scalar_one_or_none.return_value = mock_session_obj
    mock_db.execute = AsyncMock(return_value=result)

    with (
        patch("app.services.mcp_catalog.catalog_keys", return_value=["coralogix"]),
        patch("app.services.mcp_config.apply_session_mcp_selection") as apply,
    ):
        resp = await authenticated_client.put(
            f"/api/sessions/{mock_session_obj.id}/mcp",
            json={"enabled": ["coralogix", "evil"]},
        )

    assert resp.status_code == 400
    assert "evil" in resp.json()["detail"]
    apply.assert_not_called()


# ---- per-server tool enumeration (GET /api/mcp/{key}/tools) ------------------ //
def _tools_row(tools: list[dict], updated_at: object = None) -> MagicMock:
    """Stand-in for a McpServerTools row (.tools / .updated_at)."""
    from datetime import UTC, datetime

    row = MagicMock()
    row.tools = tools
    row.updated_at = updated_at or datetime(2026, 7, 3, tzinfo=UTC)
    return row


@pytest.mark.asyncio
async def test_get_tools_cache_hit_returns_cached_without_enumerating(
    authenticated_client: AsyncClient,
) -> None:
    cached = _tools_row([{"name": "query", "description": "q", "input_schema": {}}])
    with (
        patch("app.api.routes.mcp.catalog_keys", return_value=["coralogix"]),
        patch("app.api.routes.mcp.oauth_catalog_keys", return_value=[]),
        patch("app.services.mcp_tools.get_cached_tools", AsyncMock(return_value=cached)),
        patch("app.services.mcp_tools.refresh_tools", AsyncMock()) as refresh,
    ):
        resp = await authenticated_client.get("/api/mcp/coralogix/tools")
    assert resp.status_code == 200
    assert [t["name"] for t in resp.json()["tools"]] == ["query"]
    refresh.assert_not_awaited()  # cache hit must not hit the network


@pytest.mark.asyncio
async def test_get_tools_cache_miss_enumerates(authenticated_client: AsyncClient) -> None:
    fresh = [{"name": "search", "description": "", "input_schema": {}}]
    with (
        patch("app.api.routes.mcp.catalog_keys", return_value=[]),
        patch("app.api.routes.mcp.oauth_catalog_keys", return_value=["notion"]),
        patch(
            "app.services.mcp_tools.get_cached_tools",
            AsyncMock(side_effect=[None, _tools_row(fresh)]),
        ),
        patch("app.services.mcp_tools.refresh_tools", AsyncMock(return_value=fresh)) as refresh,
    ):
        resp = await authenticated_client.get("/api/mcp/notion/tools")
    assert resp.status_code == 200
    assert [t["name"] for t in resp.json()["tools"]] == ["search"]
    refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_tools_unknown_key_404(authenticated_client: AsyncClient) -> None:
    with (
        patch("app.api.routes.mcp.catalog_keys", return_value=["coralogix"]),
        patch("app.api.routes.mcp.oauth_catalog_keys", return_value=[]),
    ):
        resp = await authenticated_client.get("/api/mcp/ghost/tools")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_tools_enum_failure_no_cache_returns_500_not_retryable(
    authenticated_client: AsyncClient,
) -> None:
    # An enumeration failure with no cache must be a 500 (application error the UI shows as
    # such), NOT a 502/503/504 — those are the gateway class the client retries ~12x and
    # renders as "backend unavailable", flooding an unreachable server and misleading the user.
    from app.services.mcp_tools import McpEnumError

    with (
        patch("app.api.routes.mcp.catalog_keys", return_value=[]),
        patch("app.api.routes.mcp.oauth_catalog_keys", return_value=["notion"]),
        patch("app.services.mcp_tools.get_cached_tools", AsyncMock(return_value=None)),
        patch(
            "app.services.mcp_tools.refresh_tools",
            AsyncMock(side_effect=McpEnumError("dead")),
        ),
    ):
        resp = await authenticated_client.get("/api/mcp/notion/tools")
    assert resp.status_code == 500
    assert resp.status_code not in (502, 503, 504)  # not the retried backend-unavailable class


@pytest.mark.asyncio
async def test_get_tools_enum_failure_serves_stale_cache(
    authenticated_client: AsyncClient,
) -> None:
    from app.services.mcp_tools import McpEnumError

    stale = _tools_row([{"name": "old", "description": "", "input_schema": {}}])
    with (
        patch("app.api.routes.mcp.catalog_keys", return_value=[]),
        patch("app.api.routes.mcp.oauth_catalog_keys", return_value=["notion"]),
        patch("app.services.mcp_tools.get_cached_tools", AsyncMock(return_value=stale)),
        patch(
            "app.services.mcp_tools.refresh_tools",
            AsyncMock(side_effect=McpEnumError("transient")),
        ),
    ):
        # refresh=true forces a re-enum; it fails, but the stale cache must still serve.
        resp = await authenticated_client.get("/api/mcp/notion/tools?refresh=true")
    assert resp.status_code == 200
    assert [t["name"] for t in resp.json()["tools"]] == ["old"]


# ---- per-tool policy: conversation scope (/api/sessions/{id}/mcp/policy) ----- //
@pytest.mark.asyncio
async def test_get_session_policy_returns_rows(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_session_obj: MagicMock
) -> None:
    result = MagicMock()
    result.scalar_one_or_none.return_value = mock_session_obj
    mock_db.execute = AsyncMock(return_value=result)
    with patch(
        "app.services.mcp_policy.get_session_policies",
        AsyncMock(return_value=[{"server_key": "coralogix", "tool": "*", "verdict": "deny"}]),
    ):
        resp = await authenticated_client.get(f"/api/sessions/{mock_session_obj.id}/mcp/policy")
    assert resp.status_code == 200
    assert resp.json()["policies"] == [{"server_key": "coralogix", "tool": "*", "verdict": "deny"}]


@pytest.mark.asyncio
async def test_set_session_policy_replaces_server(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_session_obj: MagicMock
) -> None:
    result = MagicMock()
    result.scalar_one_or_none.return_value = mock_session_obj
    mock_db.execute = AsyncMock(return_value=result)
    with (
        patch("app.services.mcp_catalog.catalog_keys", return_value=["coralogix"]),
        patch("app.services.mcp_oauth_catalog.oauth_catalog_keys", return_value=[]),
        patch("app.services.mcp_policy.set_session_server_policy", AsyncMock()) as setter,
        patch("app.services.mcp_policy.get_session_policies", AsyncMock(return_value=[])),
    ):
        resp = await authenticated_client.put(
            f"/api/sessions/{mock_session_obj.id}/mcp/policy",
            json={"server_key": "coralogix", "entries": [{"tool": "query", "verdict": "deny"}]},
        )
    assert resp.status_code == 200
    setter.assert_awaited_once()
    _, kwargs = setter.await_args
    assert kwargs["entries"] == [("query", "deny")]


@pytest.mark.asyncio
async def test_set_session_policy_unknown_server_400(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_session_obj: MagicMock
) -> None:
    result = MagicMock()
    result.scalar_one_or_none.return_value = mock_session_obj
    mock_db.execute = AsyncMock(return_value=result)
    with (
        patch("app.services.mcp_catalog.catalog_keys", return_value=["coralogix"]),
        patch("app.services.mcp_oauth_catalog.oauth_catalog_keys", return_value=[]),
        patch("app.services.mcp_policy.set_session_server_policy", AsyncMock()) as setter,
    ):
        resp = await authenticated_client.put(
            f"/api/sessions/{mock_session_obj.id}/mcp/policy",
            json={"server_key": "ghost", "entries": []},
        )
    assert resp.status_code == 400
    setter.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_session_policy_bad_verdict_400(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_session_obj: MagicMock
) -> None:
    from app.services.mcp_policy import InvalidVerdictError

    result = MagicMock()
    result.scalar_one_or_none.return_value = mock_session_obj
    mock_db.execute = AsyncMock(return_value=result)
    with (
        patch("app.services.mcp_catalog.catalog_keys", return_value=["coralogix"]),
        patch("app.services.mcp_oauth_catalog.oauth_catalog_keys", return_value=[]),
        patch(
            "app.services.mcp_policy.set_session_server_policy",
            AsyncMock(side_effect=InvalidVerdictError("bad")),
        ),
    ):
        resp = await authenticated_client.put(
            f"/api/sessions/{mock_session_obj.id}/mcp/policy",
            json={"server_key": "coralogix", "entries": [{"tool": "q", "verdict": "maybe"}]},
        )
    assert resp.status_code == 400


# ---- per-tool policy: global user scope (/api/me/mcp-policy) ----------------- //
@pytest.mark.asyncio
async def test_get_my_mcp_policy_returns_rows(authenticated_client: AsyncClient) -> None:
    with patch(
        "app.services.mcp_policy.get_user_policies",
        AsyncMock(return_value=[{"server_key": "notion", "tool": "search", "verdict": "allow"}]),
    ):
        resp = await authenticated_client.get("/api/me/mcp-policy")
    assert resp.status_code == 200
    assert resp.json()["policies"][0]["tool"] == "search"


@pytest.mark.asyncio
async def test_set_my_mcp_policy_replaces_server(authenticated_client: AsyncClient) -> None:
    with (
        patch("app.services.mcp_catalog.catalog_keys", return_value=[]),
        patch("app.services.mcp_oauth_catalog.oauth_catalog_keys", return_value=["notion"]),
        patch("app.services.mcp_policy.set_user_server_policy", AsyncMock()) as setter,
        patch("app.services.mcp_policy.get_user_policies", AsyncMock(return_value=[])),
    ):
        resp = await authenticated_client.put(
            "/api/me/mcp-policy",
            json={"server_key": "notion", "entries": [{"tool": "*", "verdict": "ask"}]},
        )
    assert resp.status_code == 200
    setter.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_my_mcp_policy_unknown_server_400(authenticated_client: AsyncClient) -> None:
    with (
        patch("app.services.mcp_catalog.catalog_keys", return_value=[]),
        patch("app.services.mcp_oauth_catalog.oauth_catalog_keys", return_value=["notion"]),
        patch("app.services.mcp_policy.set_user_server_policy", AsyncMock()) as setter,
    ):
        resp = await authenticated_client.put(
            "/api/me/mcp-policy", json={"server_key": "ghost", "entries": []}
        )
    assert resp.status_code == 400
    setter.assert_not_awaited()
