"""
Shared retry utilities for API calls.

Provides consistent retry behavior with exponential backoff and jitter
for various API clients (Anthropic, Azure OpenAI, etc.).
"""

import asyncio
import logging
import random
from collections.abc import Callable
from typing import Any, TypeVar

from app.core.constants import BASE_RETRY_DELAY_SECONDS, MAX_API_RETRIES

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RetryExhaustedError(Exception):
    """Raised when all retry attempts have been exhausted."""

    def __init__(self, message: str, last_exception: Exception | None = None):
        super().__init__(message)
        self.last_exception = last_exception


def calculate_exponential_backoff(
    attempt: int, base_delay: float = BASE_RETRY_DELAY_SECONDS
) -> float:
    """
    Calculate wait time with exponential backoff and jitter.

    Args:
        attempt: Current attempt number (0-indexed)
        base_delay: Base delay in seconds

    Returns:
        Wait time in seconds with jitter
    """
    return float((2**attempt) * base_delay + random.random())


def calculate_linear_backoff(attempt: int, base_delay: float = BASE_RETRY_DELAY_SECONDS) -> float:
    """
    Calculate wait time with linear backoff.

    Args:
        attempt: Current attempt number (0-indexed)
        base_delay: Base delay in seconds

    Returns:
        Wait time in seconds
    """
    return base_delay * (attempt + 1)


async def retry_with_backoff(
    operation: Callable[[], Any],
    max_retries: int = MAX_API_RETRIES,
    retryable_exceptions: tuple[type[Exception], ...] = (),
    non_retryable_check: Callable[[Exception], bool] | None = None,
    use_exponential: bool = True,
    operation_name: str = "operation",
) -> Any:
    """
    Execute an async operation with retry and backoff.

    Args:
        operation: Async callable to execute
        max_retries: Maximum number of retry attempts
        retryable_exceptions: Tuple of exception types to retry
        non_retryable_check: Optional function to check if an exception should not be retried
        use_exponential: Use exponential backoff (True) or linear (False)
        operation_name: Name for logging purposes

    Returns:
        Result of the operation

    Raises:
        The last exception if all retries fail
    """
    last_exception: Exception | None = None
    backoff_fn = calculate_exponential_backoff if use_exponential else calculate_linear_backoff

    for attempt in range(max_retries):
        try:
            return await operation()

        except retryable_exceptions as e:
            last_exception = e

            # Check if this specific exception should not be retried
            if non_retryable_check and non_retryable_check(e):
                logger.error(f"{operation_name} failed (non-retryable): {e}")
                raise

            if attempt < max_retries - 1:
                wait = backoff_fn(attempt)
                logger.warning(
                    f"{operation_name} failed, retrying in {wait:.1f}s "
                    f"(attempt {attempt + 1}/{max_retries}): {e}"
                )
                await asyncio.sleep(wait)
                continue

            logger.error(f"{operation_name} failed after all retries: {e}")
            raise

        except Exception as e:
            # Non-retryable exception
            logger.error(f"{operation_name} failed with non-retryable error: {e}")
            raise

    # Should not reach here, but just in case
    if last_exception:
        raise last_exception
    raise RuntimeError("Unexpected state in retry loop")
