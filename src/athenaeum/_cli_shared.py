# SPDX-License-Identifier: Apache-2.0
"""Shared CLI/argparse helpers for ``cli.py`` and the ``_cmd_*`` subcommands.

Contract: the small argparse-type functions (``_positive_int``, ``_iso_date``),
the run-lock flag group, the lock-acquire helper, and the lock-contention exit
code that BOTH ``cli.py`` AND the per-subcommand ``_cmd_*.py`` modules need. Extracted
here (issue #545) so a ``_cmd_*`` module can reuse them WITHOUT importing
``cli.py`` — the ``cli`` <-> ``_cmd_drain`` back-edge that formed a 2-node import
cycle. ``_cmd_drain`` was the only ``_cmd_*`` module reaching back into ``cli``
(``_add_lock_args``/``_positive_int``/``_acquire_or_exit``); the other eight import
nothing from ``cli`` and are the shape this move restores ``_cmd_drain`` to.

Layering: L5 presentation-support leaf. Imports only stdlib plus
:mod:`athenaeum.config` and :mod:`athenaeum.runlock` (both low, neither imports
``cli`` or any ``_cmd_*`` module back). ``cli.py`` re-exports these names, so its
own internal call sites (``_add_lock_args(run_parser)`` etc.) keep working
unchanged.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from athenaeum.runlock import RunLock


def _iso_date(value: str) -> date:
    """Argparse type for an ISO-8601 ``YYYY-MM-DD`` ``--as-of`` date (issue #308).

    Unlike the fail-open frontmatter date parse, an operator explicitly
    requesting an as-of view with a malformed date gets a loud parse error
    rather than a silent today-view.
    """
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid ISO date (expected YYYY-MM-DD): {value!r}"
        ) from None


def _positive_int(value: str) -> int:
    """Argparse type for flags that must be a strictly positive integer.

    Issue #220: a zero or negative ``--max-api-calls`` would defer the
    entire intake while exiting 0 — reject it at parse time instead.
    """
    try:
        ivalue = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid positive int value: {value!r}"
        ) from None
    if ivalue <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer (got {value!r})")
    return ivalue


#: Exit code returned when a mutating command cannot acquire the run lock
#: (issue #309). Non-zero so cron / alerting sees the contention; distinct
#: from the generic error (1) and dry-run-found (2) codes some commands use.
EXIT_LOCK_HELD = 75


def _add_lock_args(parser: argparse.ArgumentParser) -> None:
    """Add the shared run-lock ``--wait`` / ``--force`` flags (issue #309).

    Mutating commands acquire an exclusive lock on
    ``<knowledge_root>/.athenaeum.lock`` so overlapping runs (nightly cron +
    manual) don't race wiki writes, sidecar appends, or the API-call budget.
    """
    group = parser.add_argument_group("run lock (single-machine, issue #309)")
    group.add_argument(
        "--wait",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Block up to SECONDS for the run lock instead of failing fast. "
        "Default: ATHENAEUM_LOCK_TIMEOUT env, then athenaeum.yaml "
        "librarian.lock_timeout, then 0 (fail fast).",
    )
    group.add_argument(
        "--force",
        action="store_true",
        help="Break the run lock even if a process is still holding it (the "
        "current holder is logged first) and proceed. Use ONLY when you are "
        "certain the holder is hung or dead; never run two --force invocations "
        "concurrently.",
    )


def _acquire_or_exit(
    knowledge_root: Path,
    args: argparse.Namespace,
    config: dict[str, Any] | None = None,
) -> "RunLock | int":
    """Acquire the run lock or return :data:`EXIT_LOCK_HELD` (issue #309).

    Returns an acquired :class:`~athenaeum.runlock.RunLock` on success (the
    caller must ``release()`` it, ideally in a ``finally``), or the
    :data:`EXIT_LOCK_HELD` exit code after printing the holder to stderr.
    The ``--wait`` flag overrides the resolved default timeout.
    """
    from athenaeum.config import (
        resolve_lock_break_stale_after,
        resolve_lock_timeout,
        resolve_lock_warn_stale_after,
    )
    from athenaeum.runlock import LockHeld, RunLock

    wait = getattr(args, "wait", None)
    if wait is None:
        wait = resolve_lock_timeout(config)
    lock = RunLock(
        knowledge_root,
        wait=wait,
        force=getattr(args, "force", False),
        break_stale_after=resolve_lock_break_stale_after(config),
        warn_stale_after=resolve_lock_warn_stale_after(config),
    )
    try:
        lock.acquire()
    except LockHeld as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_LOCK_HELD
    return lock
