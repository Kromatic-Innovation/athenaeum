# SPDX-License-Identifier: Apache-2.0
"""The shared UTC-ISO timestamp renderer, :func:`now_iso`.

Moved here verbatim from :mod:`athenaeum.store` (athenaeum#3231): 63 modules
used it and most do no storage work, so importing it from the store module
tied them to storage for a clock. ``athenaeum.store.now_iso`` stays as a
re-export of this function until athenaeum#3225.

Layering: tier 0 foundation. Standard library only.
"""

from __future__ import annotations

from datetime import datetime, timezone


def now_iso(when: datetime | None = None) -> str:
    """Render *when* (or the current UTC instant, if omitted) as a UTC ISO-8601
    timestamp at **second precision**: ``"%Y-%m-%dT%H:%M:%SZ"`` (e.g.
    ``"2026-09-03T19:12:33Z"`` — no fractional seconds).

    THE single implementation of the UTC-ISO rendering rule issue athenaeum#1348
    found duplicated across 45 call sites and 13 independent module-private
    ``_now_iso()`` definitions, split between a microsecond-precision
    rendering (``datetime.isoformat()`` with its ``+00:00`` offset suffix
    swapped for a bare ``Z``) and a second-precision one (``datetime.strftime``
    with an explicit second-precision-then-``Z`` format string) — one of
    which, the second-precision form, is also the exact rendering every
    caller across the package that parses this value back with
    ``datetime.strptime("%Y-%m-%dT%H:%M:%SZ")`` expects. Second precision
    is the rendering kept here (not widened to also accept microseconds)
    because pinning the format in ONE place — rather than teaching every
    parser two shapes — is what makes a future drift back to microsecond
    rendering fail loudly instead of silently: any writer that reaches for
    this helper can no longer produce a timestamp a strict parser rejects.
    Mirrors :func:`append_line_durable` (issue athenaeum#980) as the
    package's shared-primitive home for this class of drift.

    A naive *when* (no ``tzinfo``) is assumed to already be UTC — the
    convention every existing caller in this package uses when it constructs
    one via ``datetime.now(timezone.utc)`` and later strips or never sets
    ``tzinfo``. An aware *when* is converted to UTC via
    :meth:`datetime.astimezone` rather than assumed.
    """
    dt = when if when is not None else datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
