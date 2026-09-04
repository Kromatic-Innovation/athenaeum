# SPDX-License-Identifier: Apache-2.0
"""Shared CLI/argparse helpers for ``cli.py`` and the ``_cmd_*`` subcommands.

Contract: the small argparse-type functions (``_positive_int``, ``_iso_date``),
the run-lock flag group, the lock-acquire helper, and the lock-contention exit
code that BOTH ``cli.py`` AND the per-subcommand ``_cmd_*.py`` modules need. Extracted
here (issue athenaeum#545) so a ``_cmd_*`` module can reuse them WITHOUT importing
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

from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT

if TYPE_CHECKING:
    from athenaeum.runlock import RunLock


def _resolve_knowledge_root(args: argparse.Namespace) -> Path:
    """Resolve the ``--path``/``args.path`` knowledge root (issue athenaeum#1349).

    Shared by every ``_cmd_*`` subcommand that accepts ``--path``: falls back
    to :data:`athenaeum.config.DEFAULT_KNOWLEDGE_ROOT` when ``args`` carries
    no ``path`` attribute or it is ``None``, then expands a ``~`` prefix and
    resolves to an absolute path. Formerly re-derived independently in nine
    modules (seven ``_resolve_wiki_root`` copies plus two of this function's
    own name); collapsed here so ``.expanduser().resolve()`` is applied in
    exactly one place.
    """
    return (getattr(args, "path", None) or DEFAULT_KNOWLEDGE_ROOT).expanduser().resolve()


def _resolve_wiki_root(args: argparse.Namespace) -> Path:
    """Resolve the wiki root under the knowledge root (issue athenaeum#1349).

    Built on top of :func:`_resolve_knowledge_root` rather than re-deriving
    it, so the ``.expanduser().resolve()`` call stays written in one place.
    """
    return _resolve_knowledge_root(args) / "wiki"


def _iso_date(value: str) -> date:
    """Argparse type for an ISO-8601 ``YYYY-MM-DD`` ``--as-of`` date (issue athenaeum#308).

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

    Issue athenaeum#220: a zero or negative ``--max-api-calls`` would defer the
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
#: (issue athenaeum#309). Non-zero so cron / alerting sees the contention; distinct
#: from the generic error (1) and dry-run-found (2) codes some commands use.
#:
#: Shares its value (75) with the UNRELATED
#: :data:`~athenaeum.librarian.EXIT_GRACEFUL_PARTIAL`
#: (`src/athenaeum/librarian.py`) — an internal wall-clock-deadline code.
#: The two are not interchangeable: this code fires before any pipeline
#: work starts, so nothing is committed and nothing is deferred to disk,
#: unlike EXIT_GRACEFUL_PARTIAL's "partial progress, resumable" case. See
#: docs/exit-codes.md ("`75` also collides with `EXIT_LOCK_HELD`", issue
#: athenaeum#1379); renumbering either constant is a separate, open decision
#: not made there.
EXIT_LOCK_HELD = 75

#: Exit code for "the requested uid/resource does not exist" (issue athenaeum#1270).
#: This is the existing generic-error code (1), named here so a caller can key
#: off the constant instead of the bare literal, and so it reads as a
#: deliberate, stable contract rather than argparse's incidental default.
#: MUST stay distinct from :data:`EXIT_INTERNAL_ERROR` below — see that
#: constant's docstring for why the distinction is load-bearing.
EXIT_NOT_FOUND = 1

#: Exit code for "the lookup succeeded but something else inside the command
#: failed" (issue athenaeum#1270) — e.g. a page whose frontmatter holds a
#: value the JSON encoder chokes on. Before this constant existed, an
#: uncaught exception fell through to Python's default uncaught-exception
#: exit status, which is ALSO 1 — identical to :data:`EXIT_NOT_FOUND`. That
#: collapse is the defect: a caller keying off the exit code (e.g.
#: ``google-contact-sync``'s ``read_person()``, which maps every nonzero exit
#: that isn't argparse's "invalid choice" to "unknown uid") could not tell an
#: existing-but-broken record from a genuinely absent one. 70 is BSD
#: sysexits.h's ``EX_SOFTWARE`` ("an internal software error"); chosen over an
#: adjacent small integer so it reads as intentional, not incremented.
EXIT_INTERNAL_ERROR = 70


def _add_lock_args(parser: argparse.ArgumentParser) -> None:
    """Add the shared run-lock ``--wait`` / ``--force`` flags (issue athenaeum#309).

    Mutating commands acquire an exclusive lock on
    ``<knowledge_root>/.athenaeum.lock`` so overlapping runs (nightly cron +
    manual) don't race wiki writes, sidecar appends, or the API-call budget.
    """
    group = parser.add_argument_group("run lock (single-machine, issue athenaeum#309)")
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
    """Acquire the run lock or return :data:`EXIT_LOCK_HELD` (issue athenaeum#309).

    Returns an acquired :class:`~athenaeum.runlock.RunLock` on success (the
    caller must ``release()`` it, ideally in a ``finally``), or the
    :data:`EXIT_LOCK_HELD` exit code after printing the holder to stderr.
    The ``--wait`` flag overrides the resolved default timeout.
    """
    from athenaeum.config import (
        resolve_lock_break_stale_after,
        resolve_lock_heartbeat_interval,
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
        # Issue athenaeum#1271: guaranteed background heartbeat-bump interval,
        # independent of caller progress.
        heartbeat_interval=resolve_lock_heartbeat_interval(config),
    )
    try:
        lock.acquire()
    except LockHeld as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_LOCK_HELD
    return lock
