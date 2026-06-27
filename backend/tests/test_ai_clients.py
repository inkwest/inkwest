"""
Tests for orchestrator dispatch and model-name resolution.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.ai_client import (
    get_model_name,
    get_orchestrator,
)


class TestGetModelName:
    """get_model_name delegates to model_registry.default_model (covered there)."""

    def test_delegates_to_registry_default(self):
        with patch("app.services.ai_client.default_model", return_value="claude-opus-4-8"):
            assert get_model_name() == "claude-opus-4-8"


class TestGetOrchestrator:
    """There is a single engine-dispatched path — no provider switch."""

    def test_returns_engine_dispatcher(self):
        assert get_orchestrator().__name__ == "_dispatch_engine"


class TestDispatcherSetsTurnContext:
    """Regression: the dispatcher must set engine+model in the task context so deep metric
    sites (record_usage, tools) and turn_runner read the right values. A refactor that drops
    set_turn_engine/model would silently revert every label to 'unknown' with tests green."""

    @pytest.mark.asyncio
    async def test_cli_fallback_sets_context(self):
        """A Claude model with no sandbox falls back to the CLI orchestrator."""
        import asyncio

        from app.services import ai_client, engines

        seen = {}

        async def fake_cli(db, session_id, **kwargs):
            from app.core import metrics

            seen["engine"] = metrics.turn_engine()
            seen["model"] = metrics.turn_model()
            seen["engine_kwarg"] = kwargs.get("engine")
            return
            yield

        mock_settings = MagicMock()
        mock_settings.sandbox_enabled = False
        with (
            patch.object(ai_client, "get_settings", return_value=mock_settings),
            patch.object(ai_client, "resolve_session_model", AsyncMock(return_value="claude-x")),
            patch.object(ai_client, "resolve_session_engine", AsyncMock(return_value=None)),
            patch.object(engines, "configured_default_engine", return_value=None),
            patch.object(engines, "engine_for_turn", return_value="claude-cli"),
            patch("app.services.orchestrator_cli.orchestrate_chat_cli", fake_cli),
        ):
            dispatch = ai_client.get_orchestrator()

            async def run():
                async for _ in dispatch(db=AsyncMock(), session_id="s"):
                    pass

            await asyncio.create_task(run())
        # The resolved engine is both recorded in metrics context and forwarded to the
        # orchestrator so it persists on the turn (save_message engine=).
        assert seen == {"engine": "claude-cli", "model": "claude-x", "engine_kwarg": "claude-cli"}

    @pytest.mark.asyncio
    async def test_routes_to_sandboxed_agent_when_enabled(self):
        """engine==inkwest-agent + enabled + sandbox routes to the sandboxed agent."""
        import asyncio

        from app.services import ai_client, engines

        seen = {}

        async def fake_agent(db, session_id, **kwargs):
            from app.core import metrics

            seen["engine"] = metrics.turn_engine()
            seen["model"] = metrics.turn_model()
            seen["engine_kwarg"] = kwargs.get("engine")
            return
            yield

        mock_settings = MagicMock()
        mock_settings.sandbox_enabled = True
        with (
            patch.object(ai_client, "get_settings", return_value=mock_settings),
            patch.object(ai_client, "resolve_session_model", AsyncMock(return_value="llama-x")),
            patch.object(ai_client, "resolve_session_engine", AsyncMock(return_value=None)),
            patch.object(engines, "configured_default_engine", return_value=None),
            patch.object(engines, "engine_for_turn", return_value="inkwest-agent"),
            patch.object(engines, "inkwest_agent_enabled", return_value=True),
            patch(
                "app.services.orchestrator_inkwest_agent.orchestrate_chat_inkwest_agent",
                fake_agent,
            ),
        ):
            dispatch = ai_client.get_orchestrator()

            async def run():
                async for _ in dispatch(db=AsyncMock(), session_id="s"):
                    pass

            await asyncio.create_task(run())
        assert seen == {
            "engine": "inkwest-agent",
            "model": "llama-x",
            "engine_kwarg": "inkwest-agent",
        }


class TestResolveSessionModelChoice:
    """Turn-time enforcement of the admin-enabled subset."""

    @pytest.mark.asyncio
    async def test_pinned_model_honored_while_enabled(self):
        from app.services.ai_client import resolve_session_model_choice

        db = AsyncMock()
        db.scalar = AsyncMock(return_value="claude-opus-4-8")
        with patch(
            "app.services.ai_client.selectable_models",
            return_value=["claude-opus-4-8", "claude-haiku-4-5"],
        ):
            assert await resolve_session_model_choice(db, "s1") == "claude-opus-4-8"

    @pytest.mark.asyncio
    async def test_pinned_model_falls_back_when_admin_disables_it(self):
        """An admin uncheck must bite existing sessions, not just new pins."""
        from app.services.ai_client import resolve_session_model_choice

        db = AsyncMock()
        db.scalar = AsyncMock(return_value="claude-fable-5")
        with (
            patch(
                "app.services.ai_client.selectable_models",
                return_value=["claude-haiku-4-5"],
            ),
            patch("app.services.ai_client.configured_default_id", return_value=None),
        ):
            assert await resolve_session_model_choice(db, "s1") is None

    @pytest.mark.asyncio
    async def test_admin_default_must_also_be_enabled(self):
        from app.services.ai_client import resolve_session_model_choice

        db = AsyncMock()
        db.scalar = AsyncMock(return_value=None)
        with (
            patch(
                "app.services.ai_client.selectable_models",
                return_value=["claude-haiku-4-5"],
            ),
            patch(
                "app.services.ai_client.configured_default_id",
                return_value="claude-fable-5",
            ),
        ):
            assert await resolve_session_model_choice(db, "s1") is None
