# app/utils/retry_utils.py
"""
Retry utility with exponential backoff for external API calls (OpenAI, email, etc.)
"""

import time
import logging
from typing import Callable, TypeVar, Optional, Any
from functools import wraps
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type
from botocore.exceptions import ClientError
import dns.resolver

logger = logging.getLogger(__name__)

# =========================================================
# Tenacity-based retry logic for AWS and DNS (From HEAD)
# =========================================================

def retry_external_api(
    max_attempts: int = 3,
    multiplier: int = 1,
    min_wait: int = 2,
    max_wait: int = 10,
    exception_types: tuple = (Exception,)
) -> Callable:
    """
    Decorator to retry external API calls with exponential backoff.
    Used for AWS, OpenAI, external HTTP requests, and DNS resolving.
    """
    return retry(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=multiplier, min=min_wait, max=max_wait),
        retry=retry_if_exception_type(exception_types),
        reraise=True,
        before_sleep=lambda retry_state: logger.warning(
            f"[Retry] Attempt {retry_state.attempt_number} failed. Retrying in {retry_state.next_action.sleep}s "
            f"due to {type(retry_state.outcome.exception()).__name__}: {retry_state.outcome.exception()}"
        )
    )

# Pre-configured instances
retry_aws_call = retry_external_api(exception_types=(ClientError,))
retry_dns_query = retry_external_api(
    max_attempts=3, 
    exception_types=(
        dns.resolver.NoAnswer, 
        dns.resolver.NXDOMAIN, 
        dns.resolver.Timeout, 
        dns.exception.DNSException
    )
)

# =========================================================
# Custom Time-based Retry Logic (from feat/Timezonefix)
# =========================================================

T = TypeVar("T")

# Transient errors we should retry on
RETRYABLE_EXCEPTIONS = (
    ConnectionError,
    TimeoutError,
    OSError,
)

# Try to add OpenAI-specific retryable errors if available
try:
    from openai import RateLimitError, APITimeoutError, InternalServerError, APIConnectionError
    RETRYABLE_EXCEPTIONS = RETRYABLE_EXCEPTIONS + (
        RateLimitError,
        APITimeoutError,
        InternalServerError,
        APIConnectionError,
    )
except ImportError:
    pass  # openai not installed — ignore


def retry_with_backoff(
    func: Callable[..., T],
    *args,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    exponential_base: float = 2.0,
    **kwargs,
) -> Optional[T]:
    """
    Call *func* with *args/**kwargs*, retrying on transient errors up to
    *max_attempts* times with exponential backoff.
    """
    last_exception: Optional[Exception] = None

    for attempt in range(1, max_attempts + 1):
        try:
            return func(*args, **kwargs)

        except RETRYABLE_EXCEPTIONS as exc:
            last_exception = exc
            if attempt == max_attempts:
                logger.error(
                    "[retry_with_backoff] All %d attempts exhausted for %s. Last error: %s",
                    max_attempts, getattr(func, "__name__", "func"), exc,
                )
                break

            delay = min(base_delay * (exponential_base ** (attempt - 1)), max_delay)
            logger.warning(
                "[retry_with_backoff] Attempt %d/%d for %s failed (%s: %s). "
                "Retrying in %.1fs …",
                attempt, max_attempts,
                getattr(func, "__name__", "func"),
                type(exc).__name__, exc,
                delay,
            )
            time.sleep(delay)

        except Exception as exc:
            # Non-retryable error — log and surface immediately
            logger.error(
                "[retry_with_backoff] Non-retryable error in %s: %s",
                getattr(func, "__name__", "func"), exc,
            )
            raise

    return None
