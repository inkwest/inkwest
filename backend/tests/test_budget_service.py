"""
Tests for the budget service.
"""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.models.budget import BudgetPeriod
from app.services.budget_service import (
    BudgetStatus,
    UsageSummary,
    get_period_start,
)


class TestGetPeriodStart:
    """Tests for period start calculation."""

    def test_daily_period_start(self):
        """Should return start of current day."""
        with patch("app.services.budget_service.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 15, 14, 30, 45, tzinfo=UTC)
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)

            start = get_period_start(BudgetPeriod.DAILY)

            assert start.hour == 0
            assert start.minute == 0
            assert start.second == 0
            assert start.day == 15

    def test_monthly_period_start(self):
        """Should return start of current month."""
        with patch("app.services.budget_service.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 15, 14, 30, 45, tzinfo=UTC)
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)

            start = get_period_start(BudgetPeriod.MONTHLY)

            assert start.day == 1
            assert start.hour == 0
            assert start.month == 1


class TestBudgetStatus:
    """Tests for BudgetStatus named tuple."""

    def test_budget_status_allowed(self):
        """Should create allowed status."""
        status = BudgetStatus(
            is_allowed=True,
            remaining_tokens=50000,
            remaining_cost_usd=Decimal("1.50"),
            usage_percent=50.0,
            warning_message=None,
            budget_name="default",
        )

        assert status.is_allowed is True
        assert status.remaining_tokens == 50000
        assert status.warning_message is None

    def test_budget_status_blocked(self):
        """Should create blocked status."""
        status = BudgetStatus(
            is_allowed=False,
            remaining_tokens=0,
            remaining_cost_usd=Decimal("0"),
            usage_percent=100.0,
            warning_message="Budget exceeded",
            budget_name="user-budget",
        )

        assert status.is_allowed is False
        assert status.remaining_tokens == 0
        assert "exceeded" in status.warning_message

    def test_budget_status_warning(self):
        """Should create status with warning."""
        status = BudgetStatus(
            is_allowed=True,
            remaining_tokens=10000,
            remaining_cost_usd=None,
            usage_percent=85.0,
            warning_message="Warning: 85% of budget used",
            budget_name="team-budget",
        )

        assert status.is_allowed is True
        assert status.warning_message is not None
        assert "85%" in status.warning_message


class TestBudgetEnumMapping:
    """The Postgres enum types (budgettype/budgetperiod) were created with the
    lowercase enum *values* ('user'), not the member names ('USER'). The model must
    bind the values via values_callable, or every insert/filter raises
    InvalidTextRepresentationError ('invalid input value for enum ...')."""

    def test_budget_type_enum_binds_lowercase_values(self):
        from app.models.budget import Budget

        assert set(Budget.__table__.c.budget_type.type.enums) == {"global", "user", "role"}

    def test_period_enum_binds_lowercase_values(self):
        from app.models.budget import Budget

        assert set(Budget.__table__.c.period.type.enums) == {
            "hourly",
            "daily",
            "weekly",
            "monthly",
        }


class TestUsageSummary:
    """Tests for UsageSummary named tuple."""

    def test_usage_summary_creation(self):
        """Should create usage summary."""
        summary = UsageSummary(
            total_input_tokens=50000,
            total_output_tokens=25000,
            total_tokens=75000,
            total_cost_usd=Decimal("0.375"),
            request_count=10,
        )

        assert summary.total_input_tokens == 50000
        assert summary.total_output_tokens == 25000
        assert summary.total_tokens == 75000
        assert summary.total_cost_usd == Decimal("0.375")
        assert summary.request_count == 10

    def test_usage_summary_zero(self):
        """Should handle zero usage."""
        summary = UsageSummary(
            total_input_tokens=0,
            total_output_tokens=0,
            total_tokens=0,
            total_cost_usd=Decimal("0"),
            request_count=0,
        )

        assert summary.total_tokens == 0
        assert summary.request_count == 0


class TestBudgetServiceIntegration:
    """Integration tests for budget service (mocked DB)."""

    @pytest.mark.asyncio
    async def test_check_budget_disabled(self):
        """Should allow when budget is disabled."""
        from app.services.budget_service import check_budget

        mock_db = AsyncMock()

        with patch("app.services.budget_service.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                budget_enabled=False,
            )

            status = await check_budget(mock_db, user_id=uuid4())

            assert status.is_allowed is True
            assert status.budget_name is None

    @pytest.mark.asyncio
    async def test_check_budget_default_cap_under_limit(self):
        """No budget row + default monthly cap, under limit → allowed, action=block."""
        from app.services import budget_service
        from app.services.budget_service import check_budget

        usage = UsageSummary(
            total_input_tokens=1000,
            total_output_tokens=500,
            total_tokens=1500,
            total_cost_usd=Decimal("1.00"),
            request_count=3,
        )
        with (
            patch.object(budget_service, "is_budget_enabled", return_value=True),
            patch.object(budget_service, "get_applicable_budget", AsyncMock(return_value=None)),
            patch.object(
                budget_service, "default_monthly_cost_limit", return_value=Decimal("5.00")
            ),
            patch.object(budget_service, "get_usage_for_period", AsyncMock(return_value=usage)),
        ):
            status = await check_budget(AsyncMock(), user_id=uuid4())

        assert status.is_allowed is True
        assert status.budget_name == "default"
        assert status.hard_limit_action == "block"
        assert status.remaining_cost_usd == Decimal("4.00")

    @pytest.mark.asyncio
    async def test_check_budget_default_cap_exceeded_blocks(self):
        """No budget row + default monthly cap, over limit → blocked with block action."""
        from app.services import budget_service
        from app.services.budget_service import check_budget

        usage = UsageSummary(
            total_input_tokens=0,
            total_output_tokens=0,
            total_tokens=0,
            total_cost_usd=Decimal("6.00"),
            request_count=10,
        )
        with (
            patch.object(budget_service, "is_budget_enabled", return_value=True),
            patch.object(budget_service, "get_applicable_budget", AsyncMock(return_value=None)),
            patch.object(
                budget_service, "default_monthly_cost_limit", return_value=Decimal("5.00")
            ),
            patch.object(budget_service, "get_usage_for_period", AsyncMock(return_value=usage)),
        ):
            status = await check_budget(AsyncMock(), user_id=uuid4())

        assert status.is_allowed is False
        assert status.hard_limit_action == "block"
        assert status.budget_name == "default"

    @pytest.mark.asyncio
    async def test_check_budget_no_default_cap_allows(self):
        """No budget row and no default cap configured → unlimited allow."""
        from app.services import budget_service
        from app.services.budget_service import check_budget

        with (
            patch.object(budget_service, "is_budget_enabled", return_value=True),
            patch.object(budget_service, "get_applicable_budget", AsyncMock(return_value=None)),
            patch.object(budget_service, "default_monthly_cost_limit", return_value=None),
        ):
            status = await check_budget(AsyncMock(), user_id=uuid4())

        assert status.is_allowed is True
        assert status.budget_name is None

    @pytest.mark.asyncio
    async def test_check_budget_cost_enforced_when_both_limits_set(self):
        """Regression: a budget with BOTH token and cost caps must enforce cost.

        The previous implementation returned at the token branch and never checked
        cost, so an under-token / over-cost user was wrongly allowed.
        """
        from app.services import budget_service
        from app.services.budget_service import check_budget

        budget = MagicMock(
            name="team",  # note: MagicMock(name=...) sets the mock's repr, not .name
            max_total_tokens=10_000,
            max_cost_usd=Decimal("1.00"),
            period=BudgetPeriod.DAILY,
            soft_limit_percent=80,
            hard_limit_action="block",
        )
        budget.name = "team"
        usage = UsageSummary(
            total_input_tokens=400,
            total_output_tokens=100,
            total_tokens=500,  # well under the 10k token cap
            total_cost_usd=Decimal("2.00"),  # but over the $1 cost cap
            request_count=5,
        )
        with (
            patch.object(budget_service, "is_budget_enabled", return_value=True),
            patch.object(budget_service, "get_applicable_budget", AsyncMock(return_value=budget)),
            patch.object(budget_service, "get_usage_for_period", AsyncMock(return_value=usage)),
        ):
            status = await check_budget(AsyncMock(), user_id=uuid4())

        assert status.is_allowed is False
        assert status.hard_limit_action == "block"
        assert "Cost limit" in status.warning_message

    @pytest.mark.asyncio
    async def test_check_budget_warn_action_not_blocking_in_status(self):
        """A 'warn' budget over limit still reports is_allowed=False but action=warn.

        The request path only blocks when hard_limit_action == 'block', so a warn
        budget surfaces the overage for reporting without barring the turn.
        """
        from app.services import budget_service
        from app.services.budget_service import check_budget

        budget = MagicMock(
            max_total_tokens=None,
            max_cost_usd=Decimal("1.00"),
            period=BudgetPeriod.MONTHLY,
            soft_limit_percent=80,
            hard_limit_action="warn",
        )
        budget.name = "soft"
        usage = UsageSummary(
            total_input_tokens=0,
            total_output_tokens=0,
            total_tokens=0,
            total_cost_usd=Decimal("2.00"),
            request_count=1,
        )
        with (
            patch.object(budget_service, "is_budget_enabled", return_value=True),
            patch.object(budget_service, "get_applicable_budget", AsyncMock(return_value=budget)),
            patch.object(budget_service, "get_usage_for_period", AsyncMock(return_value=usage)),
        ):
            status = await check_budget(AsyncMock(), user_id=uuid4())

        assert status.is_allowed is False
        assert status.hard_limit_action == "warn"

    @pytest.mark.asyncio
    async def test_record_usage(self):
        """Should record usage correctly."""
        from app.services.budget_service import record_usage

        mock_db = AsyncMock()
        session_id = uuid4()
        user_id = uuid4()

        # provider defaults to the engine that ran the turn (metrics task-context),
        # aligning the UsageRecord dimension with the token/cost metrics.
        with patch("app.services.budget_service.metrics.turn_engine", return_value="claude-cli"):
            with patch("app.services.budget_service.default_model") as mock_model:
                mock_model.return_value = "claude-sonnet-4-20250514"

                record = await record_usage(
                    db=mock_db,
                    user_id=user_id,
                    session_id=session_id,
                    input_tokens=1000,
                    output_tokens=500,
                )

                # Verify db.add was called
                mock_db.add.assert_called_once()
                mock_db.flush.assert_called_once()

                # Verify record fields
                assert record.user_id == user_id
                assert record.session_id == session_id
                assert record.input_tokens == 1000
                assert record.output_tokens == 500
                assert record.model == "claude-sonnet-4-20250514"
                assert record.provider == "claude-cli"
                # Cost should be calculated
                # 1000 input at $3/M = $0.003
                # 500 output at $15/M = $0.0075
                # Total = $0.0105
                assert record.cost_usd == Decimal("0.0105")
