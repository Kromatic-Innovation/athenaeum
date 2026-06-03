# SPDX-License-Identifier: Apache-2.0
"""Bounded exponential-backoff retry for transient Anthropic API errors.

The librarian's per-file classification path (``tiers.py``) calls the
Anthropic API once per tier. When the API is busy it raises transient errors
-- HTTP 429 ``RateLimitError``, HTTP 529 ``OverloadedError``, or a network-level
``APIConnectionError``. Without retry, each affected file is logged
``Failed to process`` and deferred to the next run; because the same files land
in the same late position every night, a transient overload window becomes a
permanent, self-perpetuating backlog (issue #193).

This module wraps a single API call with bounded exponential backoff + jitter
on exactly those transient classes. Non-transient errors (e.g. 400
``BadRequestError`` from malformed input) are re-raised immediately so the
malformed-file case stays fast-fail and distinguishable.

On final give-up the wrapper raises :class:`TransientAPIError`, which callers
can catch to log a transient-API give-up distinctly from a malformed-file
failure (acceptance criterion of #193).
"""

from __future__ import annotations

import logging
import random
import time
from typing import Callable, TypeVar

import anthropic
from anthropic._exceptions import OverloadedError

log = logging.getLogger("athenaeum")

T = TypeVar("T")

# Defaults -- tuned for the nightly librarian run, overridable per call.
DEFAULT_MAX_ATTEMPTS = 5  # 1 initial try + 4 retries
DEFAULT_BASE_DELAY = 1.0  # seconds; first backoff window
DEFAULT_MAX_DELAY = 60.0  # seconds; cap on any single backoff window

# Transient classes worth retrying. 429 (RateLimitError) and 529
# (OverloadedError) are server-side "try again"; APIConnectionError is a
# network blip. Everything else (400/401/403/404/422, malformed responses)
# is non-transient and must surface immediately.
TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
    anthropic.RateLimitError,
    OverloadedError,
    anthropic.APIConnectionError,
)


class TransientAPIError(Exception):
    """Raised when an Anthropic call exhausts its transient-error retries.

    Carries the last underlying transient exception so callers and logs can
    name the overload type. Catching this lets the librarian distinguish a
    transient-API give-up from a malformed-file failure.
    """

    def __init__(self, attempts: int, last_error: Exception) -> None:
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            f"transient Anthropic API error after {attempts} attempt(s): "
            f"{type(last_error).__name__}: {last_error}"
        )


def _retry_after_seconds(error: Exception) -> float | None:
    """Return the server-provided ``Retry-After`` (seconds) if present.

    Anthropic transient errors carry the originating ``response``; honor its
    ``Retry-After`` header when set so we don't hammer ahead of the server's
    own backoff hint. Returns ``None`` when absent or unparseable.
    """
    response = getattr(error, "response", None)
    if response is None:
        return None
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def _backoff_delay(
    attempt: int,
    error: Exception,
    base_delay: float,
    max_delay: float,
) -> float:
    """Compute the sleep before the next attempt (capped, with jitter).

    Honors ``Retry-After`` when the server provided one; otherwise uses
    exponential backoff (``base * 2**(attempt-1)``) with full jitter, capped
    at ``max_delay``.
    """
    retry_after = _retry_after_seconds(error)
    if retry_after is not None:
        return min(retry_after, max_delay)
    window = min(base_delay * (2 ** (attempt - 1)), max_delay)
    # Full jitter: pick uniformly in [0, window] to spread retries and avoid
    # a thundering herd when many files retry against the same overload window.
    return random.uniform(0.0, window)


def with_retry(
    call: Callable[[], T],
    *,
    description: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run ``call`` with bounded exponential backoff on transient API errors.

    Retries only on :data:`TRANSIENT_ERRORS` (429 / 529 / connection). Any
    other exception propagates unchanged on the first occurrence so malformed
    input still fails fast.

    Args:
        call: Zero-arg callable performing the Anthropic request.
        description: Human-readable label for logs (e.g. ``"tier2_classify"``).
        max_attempts: Total attempts including the first (default 5).
        base_delay: First backoff window in seconds (default 1.0).
        max_delay: Cap on any single backoff window in seconds (default 60.0).
        sleep: Injectable sleep, patched in tests so they don't wait.

    Returns:
        Whatever ``call`` returns on success.

    Raises:
        TransientAPIError: when all attempts hit transient errors.
    """
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return call()
        except TRANSIENT_ERRORS as exc:
            last_error = exc
            if attempt >= max_attempts:
                break
            delay = _backoff_delay(attempt, exc, base_delay, max_delay)
            log.warning(
                "Transient API error on %s (attempt %d/%d): %s -- "
                "retrying in %.1fs",
                description,
                attempt,
                max_attempts,
                type(exc).__name__,
                delay,
            )
            sleep(delay)

    assert last_error is not None  # only reachable after a transient failure
    raise TransientAPIError(max_attempts, last_error)
