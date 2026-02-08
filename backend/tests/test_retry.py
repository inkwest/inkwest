"""
Tests for the shared retry utility module.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.core.constants import BASE_RETRY_DELAY_SECONDS
from app.core.retry import (
    RetryExhaustedError,
    calculate_exponential_backoff,
    calculate_linear_backoff,
    retry_with_backoff,
)


class TestBackoffCalculations:
    """Tests for backoff calculation functions."""

    def test_exponential_backoff_attempt_0(self):
        """First attempt should have ~1 second delay."""
        with patch("app.core.retry.random.random", return_value=0.5):
            delay = calculate_exponential_backoff(0, base_delay=1.0)
        assert delay == 1.5  # 2^0 * 1.0 + 0.5

    def test_exponential_backoff_attempt_1(self):
        """Second attempt should have ~2 second delay."""
        with patch("app.core.retry.random.random", return_value=0.5):
            delay = calculate_exponential_backoff(1, base_delay=1.0)
        assert delay == 2.5  # 2^1 * 1.0 + 0.5

    def test_exponential_backoff_attempt_2(self):
        """Third attempt should have ~4 second delay."""
        with patch("app.core.retry.random.random", return_value=0.5):
            delay = calculate_exponential_backoff(2, base_delay=1.0)
        assert delay == 4.5  # 2^2 * 1.0 + 0.5

    def test_linear_backoff_attempt_0(self):
        """First attempt should have 1 second delay."""
        delay = calculate_linear_backoff(0, base_delay=1.0)
        assert delay == 1.0  # 1.0 * (0 + 1)

    def test_linear_backoff_attempt_1(self):
        """Second attempt should have 2 second delay."""
        delay = calculate_linear_backoff(1, base_delay=1.0)
        assert delay == 2.0  # 1.0 * (1 + 1)

    def test_linear_backoff_attempt_2(self):
        """Third attempt should have 3 second delay."""
        delay = calculate_linear_backoff(2, base_delay=1.0)
        assert delay == 3.0  # 1.0 * (2 + 1)


class TestRetryWithBackoff:
    """Tests for the retry_with_backoff function."""

    @pytest.mark.asyncio
    async def test_successful_on_first_try(self):
        """Test operation that succeeds immediately."""
        operation = AsyncMock(return_value="success")

        result = await retry_with_backoff(
            operation=operation,
            max_retries=3,
            retryable_exceptions=(ValueError,),
        )

        assert result == "success"
        assert operation.call_count == 1

    @pytest.mark.asyncio
    async def test_retry_on_retryable_exception(self):
        """Test retry when retryable exception occurs."""
        operation = AsyncMock(side_effect=[ValueError("fail"), "success"])

        with patch("app.core.retry.asyncio.sleep", new_callable=AsyncMock):
            result = await retry_with_backoff(
                operation=operation,
                max_retries=3,
                retryable_exceptions=(ValueError,),
            )

        assert result == "success"
        assert operation.call_count == 2

    @pytest.mark.asyncio
    async def test_exhaust_all_retries(self):
        """Test that all retries are exhausted before raising."""
        operation = AsyncMock(side_effect=ValueError("fail"))

        with patch("app.core.retry.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(ValueError, match="fail"):
                await retry_with_backoff(
                    operation=operation,
                    max_retries=3,
                    retryable_exceptions=(ValueError,),
                )

        assert operation.call_count == 3

    @pytest.mark.asyncio
    async def test_non_retryable_exception_raises_immediately(self):
        """Test that non-retryable exceptions raise immediately."""
        operation = AsyncMock(side_effect=TypeError("bad type"))

        with pytest.raises(TypeError, match="bad type"):
            await retry_with_backoff(
                operation=operation,
                max_retries=3,
                retryable_exceptions=(ValueError,),
            )

        assert operation.call_count == 1

    @pytest.mark.asyncio
    async def test_non_retryable_check_function(self):
        """Test custom non-retryable check function."""

        # ValueError with "skip" message should not be retried
        def should_not_retry(e: Exception) -> bool:
            return isinstance(e, ValueError) and "skip" in str(e)

        operation = AsyncMock(side_effect=ValueError("skip this"))

        with pytest.raises(ValueError, match="skip this"):
            await retry_with_backoff(
                operation=operation,
                max_retries=3,
                retryable_exceptions=(ValueError,),
                non_retryable_check=should_not_retry,
            )

        assert operation.call_count == 1

    @pytest.mark.asyncio
    async def test_linear_backoff_mode(self):
        """Test using linear backoff instead of exponential."""
        operation = AsyncMock(side_effect=[ValueError("fail"), "success"])
        sleep_mock = AsyncMock()

        with patch("app.core.retry.asyncio.sleep", sleep_mock):
            result = await retry_with_backoff(
                operation=operation,
                max_retries=3,
                retryable_exceptions=(ValueError,),
                use_exponential=False,
            )

        assert result == "success"
        # Linear backoff: attempt 0 -> BASE_RETRY_DELAY_SECONDS * (0 + 1)
        sleep_mock.assert_called_once()
        sleep_time = sleep_mock.call_args[0][0]
        assert sleep_time == BASE_RETRY_DELAY_SECONDS

    @pytest.mark.asyncio
    async def test_operation_name_in_logs(self):
        """Test that operation name is used in logging."""
        operation = AsyncMock(side_effect=[ValueError("fail"), "success"])

        with patch("app.core.retry.asyncio.sleep", new_callable=AsyncMock):
            with patch("app.core.retry.logger") as mock_logger:
                await retry_with_backoff(
                    operation=operation,
                    max_retries=3,
                    retryable_exceptions=(ValueError,),
                    operation_name="test_operation",
                )

        # Check that warning was logged with operation name
        mock_logger.warning.assert_called()
        log_message = mock_logger.warning.call_args[0][0]
        assert "test_operation" in log_message


class TestRetryExhaustedError:
    """Tests for RetryExhaustedError exception."""

    def test_error_message(self):
        """Test error carries the message."""
        error = RetryExhaustedError("All retries failed")
        assert str(error) == "All retries failed"
        assert error.last_exception is None

    def test_error_with_last_exception(self):
        """Test error carries the last exception."""
        cause = ValueError("original error")
        error = RetryExhaustedError("All retries failed", last_exception=cause)
        assert str(error) == "All retries failed"
        assert error.last_exception is cause
