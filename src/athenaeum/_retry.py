# SPDX-License-Identifier: Apache-2.0
"""Bounded exponential-backoff retry for transient LLM-backend errors.

The librarian's per-file classification path (``tiers.py``) calls the
active LLM backend once per tier. When the backend is busy it raises a
transient failure -- HTTP 429 ``RateLimitError``, HTTP 529
``OverloadedError``, a network-level ``APIConnectionError`` on the ``api``
backend, or a rate-limited/overloaded ``claude`` subscription CLI call on the
``claude-cli`` backend. Without retry, each affected file is logged ``Failed
to process`` and deferred to the next run; because the same files land in
the same late position every night, a transient overload window becomes a
permanent, self-perpetuating backlog (issue athenaeum#193).

This module wraps a single backend call with bounded exponential backoff +
jitter on exactly the registered transient classes (see "Backend-agnostic
transient registry" below). Non-transient errors (e.g. 400 ``BadRequestError``
from malformed input) are re-raised immediately so the malformed-file case
stays fast-fail and distinguishable.

On final give-up the wrapper raises :class:`TransientAPIError`, which callers
can catch to log a transient-API give-up distinctly from a malformed-file
failure (acceptance criterion of athenaeum#193).

Backend-agnostic transient registry (issue athenaeum#782). This module used to
hardcode a literal tuple naming three ``anthropic`` SDK exception classes,
which meant a non-Anthropic backend's transients were invisible to
``with_retry`` (silently never retried) and this module required the
``anthropic`` SDK importable at module scope even for a ``claude-cli``-only
deployment. Two currencies now cover any backend without editing this file:

* :class:`TransientError` -- an athenaeum-owned "please retry me" signal any
  backend can raise directly. ``claude-cli`` (``provider.py``) does this: a
  subprocess failure has no anthropic-shaped exception to register.
* :func:`register_transient_types` -- lets a backend register its own native
  third-party exception classes (e.g. the ``api`` backend's real
  ``anthropic.RateLimitError`` / ``OverloadedError`` / ``APIConnectionError``,
  which cannot be retrofitted to inherit from :class:`TransientError`)
  without this module needing to know about them ahead of time.

The ``api`` backend's classes are pre-registered lazily on first use (see
:func:`_ensure_default_registrations`) via a ``try/except ImportError``-guarded
``import anthropic`` performed INSIDE the retry path -- never at module
scope, and never dependent on :func:`athenaeum.provider.build_llm_client`
having run (a fresh process that only ever imports ``athenaeum._retry`` and
calls :func:`with_retry` against a real ``anthropic.RateLimitError`` still
retries it correctly; see the athenaeum#782 issue's "Trap B" for why
construction-time registration is a footgun). :data:`TRANSIENT_ERRORS` stays
importable as a name for backward compatibility, but is now a live view over
the registry rather than a frozen tuple -- see its class for why that
matters.

Layering: L0 primitive (leaf). May import only stdlib -- no athenaeum-internal
imports. The ``anthropic`` SDK is OPTIONAL: imported lazily, guarded by
``try/except ImportError``, only to recognize the ``api`` backend's built-in
transient exception classes on first use -- never at module scope -- so this
module stays importable (and usable) in an SDK-absent (``claude-cli``-only)
deployment. This module owns ONLY the generic retry/backoff mechanics around
a zero-arg callable, plus the transient-type registry; it must not know what
the callable does (classification, embedding, etc.) -- that policy stays with
the caller.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Callable, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

# Defaults -- tuned for the nightly librarian run, overridable per call.
DEFAULT_MAX_ATTEMPTS = 5  # 1 initial try + 4 retries
DEFAULT_BASE_DELAY = 1.0  # seconds; first backoff window
DEFAULT_MAX_DELAY = 60.0  # seconds; cap on any single backoff window


class TransientError(Exception):
    """Shared currency: "this backend call hit a transient, retryable
    failure" (issue athenaeum#782).

    Any backend may raise this directly to request a retry -- the
    ``claude-cli`` backend does (``provider.py``), since a subprocess
    failure has no anthropic-shaped exception to register. A backend whose
    transients are native third-party exception classes instead registers
    those classes via :func:`register_transient_types` so ``with_retry``
    recognizes them without every call site needing to catch-and-rewrap.

    Distinct from :class:`TransientAPIError` on purpose: that is what
    ``with_retry`` raises once retries are EXHAUSTED (the give-up signal).
    This is the per-attempt "please retry me" signal. Keeping them separate
    matters -- if the give-up type doubled as the retry-request type, the
    retry loop could catch its own give-up signal.
    """


class TransientAPIError(Exception):
    """Raised when a backend call exhausts its transient-error retries.

    Carries the last underlying transient exception so callers and logs can
    name the overload type. Catching this lets the librarian distinguish a
    transient-API give-up from a malformed-file failure.
    """

    def __init__(self, attempts: int, last_error: Exception) -> None:
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            f"transient API error after {attempts} attempt(s): "
            f"{type(last_error).__name__}: {last_error}"
        )


# ---------------------------------------------------------------------------
# Transient-type registry (issue athenaeum#782)
# ---------------------------------------------------------------------------

#: backend id -> the native exception classes THAT backend raises to signal a
#: transient failure. Populated via :func:`register_transient_types`; never
#: read directly outside this module -- go through :func:`with_retry` or
#: :data:`TRANSIENT_ERRORS`.
_transient_registry: dict[str, tuple[type[Exception], ...]] = {}

#: Set once :func:`_ensure_default_registrations` has attempted the
#: ``anthropic`` import (success or failure) -- an SDK-absent process would
#: otherwise retry a doomed import on every single :func:`with_retry` call.
_default_registration_attempted = False


def register_transient_types(backend: str, types: tuple[type[Exception], ...]) -> None:
    """Register the exception classes *backend* raises to signal a transient
    (retryable) failure (issue athenaeum#782).

    Adding a new backend to the retry layer is a REGISTRATION, not an edit to
    this module: call this once with the backend's own transient exception
    classes (or skip it entirely and raise :class:`TransientError` directly
    if the backend has no pre-existing native exception types to preserve).
    Re-registering the same *backend* id overwrites its prior entry.
    """
    _transient_registry[backend] = tuple(types)


def _ensure_default_registrations() -> None:
    """Lazily register the ``api`` backend's real ``anthropic`` SDK
    transient classes, on first use -- not at module import, and not tied to
    :func:`athenaeum.provider.build_llm_client` ever having run.

    Guarded by ``try/except ImportError`` so an SDK-absent (``claude-cli``-
    only) process never fails here; it just leaves the ``api`` entry
    unregistered. Idempotent: ``anthropic`` present-or-absent is a
    process-lifetime fact, so the import is attempted at most once per
    process (cached in :data:`_default_registration_attempted`), not on
    every :func:`with_retry` call.
    """
    global _default_registration_attempted
    if _default_registration_attempted:
        return
    _default_registration_attempted = True
    try:
        import anthropic
        from anthropic._exceptions import OverloadedError
    except ImportError:
        return
    register_transient_types(
        "api",
        (anthropic.RateLimitError, OverloadedError, anthropic.APIConnectionError),
    )


def _current_transient_types() -> tuple[type[Exception], ...]:
    """The full set of exception classes ``with_retry`` currently treats as
    transient: the shared :class:`TransientError` currency plus every
    registered backend's own classes."""
    _ensure_default_registrations()
    types: list[type[Exception]] = [TransientError]
    for backend_types in _transient_registry.values():
        types.extend(backend_types)
    return tuple(types)


class _TransientErrorsView:
    """Read-only live view over the transient-type registry.

    Kept as :data:`TRANSIENT_ERRORS` for backward compatibility with the
    pre-athenaeum#782 ``TRANSIENT_ERRORS: tuple[type[Exception], ...]``
    shape -- ``x in TRANSIENT_ERRORS`` and ``for t in TRANSIENT_ERRORS`` both
    still work. Deliberately NOT a plain tuple: a plain tuple captured at
    ``from athenaeum._retry import TRANSIENT_ERRORS`` time would freeze
    whatever was registered at that exact import moment, silently going
    stale the instant a later registration (e.g. the lazy ``api`` SDK
    bootstrap, or a third backend's :func:`register_transient_types` call)
    adds a new class. Every membership/iteration check here re-resolves the
    registry, including re-running the lazy ``api`` bootstrap on first
    access -- so it is correct regardless of import order.
    """

    def __contains__(self, item: object) -> bool:
        return item in _current_transient_types()

    def __iter__(self):
        return iter(_current_transient_types())

    def __len__(self) -> int:
        return len(_current_transient_types())

    def __repr__(self) -> str:
        return f"TRANSIENT_ERRORS{_current_transient_types()!r}"


#: Live view of every exception class ``with_retry`` currently treats as
#: transient. See :class:`_TransientErrorsView`.
TRANSIENT_ERRORS = _TransientErrorsView()


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
    """Run ``call`` with bounded exponential backoff on transient errors.

    Retries only on the currently-registered transient classes (issue
    athenaeum#782) -- the shared :class:`TransientError` currency plus
    whatever each backend has registered via :func:`register_transient_types`
    (the ``api`` backend's 429 / 529 / connection classes are registered
    lazily on first call; see :func:`_ensure_default_registrations`). Any
    other exception propagates unchanged on the first occurrence so malformed
    input still fails fast.

    Args:
        call: Zero-arg callable performing the backend request.
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
    transient_types = _current_transient_types()
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return call()
        except transient_types as exc:
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

    if last_error is None:  # pragma: no cover - only reachable on logic error
        raise RuntimeError("retry loop exited without capturing an error")
    raise TransientAPIError(max_attempts, last_error)
