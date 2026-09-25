# SPDX-License-Identifier: Apache-2.0
"""``athenaeum quiesce`` — operator control over the sentinel that pauses
``ingest --if-triggered`` (issue athenaeum#1898).

Three modes on ONE subcommand, mutually exclusive:

- ``athenaeum quiesce --for <duration> --reason <text>`` — write a new
  sentinel (:func:`athenaeum.quiesce.write_quiesce`), overwriting any
  existing one. ``<duration>`` is ``<number>[h|m|s]`` (default unit ``h`` —
  see :func:`_parse_duration`), e.g. ``2h``, ``90m``, ``3600s``, or a bare
  ``2`` (hours). Rejected with a plain stderr error (exit 1) when the
  duration is non-positive or exceeds the configured maximum
  (``librarian.quiesce.max_hours``, default 6h — see
  :func:`athenaeum.config.resolve_quiesce_max_hours`).
- ``athenaeum quiesce --release`` — remove the sentinel
  (:func:`athenaeum.quiesce.release_quiesce`). Idempotent: releasing an
  already-absent sentinel is a successful no-op, not an error.
- ``athenaeum quiesce --status`` — read-only readout of the current sentinel
  (:func:`athenaeum.quiesce.read_quiesce_state`), or its absence. Never
  writes anything; always exits 0.

Every mode prints one JSON object on stdout, mirroring ``athenaeum ingest``'s
own summary convention (:mod:`athenaeum._cmd_index`).

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` — see
``cli.py``'s module docstring. This module owns presentation and duration
parsing only; every read/write/release of the sentinel itself is
:mod:`athenaeum.quiesce`'s job.
"""

from __future__ import annotations

import argparse
import getpass
import json
import socket
import sys
from datetime import timedelta
from pathlib import Path

from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT, load_config
from athenaeum.quiesce import (
    QuiesceDurationExceeded,
    read_quiesce_state,
    release_quiesce,
    write_quiesce,
)
from athenaeum.store import now_iso


def _parse_duration(value: str) -> timedelta:
    """Argparse type for ``--for``: ``<number>[h|m|s]``, default unit hours.

    Mirrors the argparse-type pattern used elsewhere in this codebase
    (:func:`athenaeum._cli_shared._positive_int`,
    :func:`athenaeum._cli_shared._iso_date`): a malformed value is a loud
    :exc:`argparse.ArgumentTypeError` at PARSE time, not a silent fallback.
    The configured-maximum cap (``librarian.quiesce.max_hours``) is a
    SEPARATE, config-dependent check performed later in :func:`cmd_quiesce`
    — this function only parses shape, it has no config to consult yet.
    """
    text = value.strip().lower()
    if not text:
        raise argparse.ArgumentTypeError("--for must not be empty")
    unit = text[-1]
    if unit in ("h", "m", "s"):
        number_part = text[:-1]
    else:
        unit = "h"
        number_part = text
    try:
        number = float(number_part)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid --for duration (expected <number>[h|m|s], e.g. 2h, "
            f"90m, 3600s): {value!r}"
        ) from None
    if number <= 0:
        raise argparse.ArgumentTypeError(f"--for must be positive, got {value!r}")
    seconds = number * {"h": 3600, "m": 60, "s": 1}[unit]
    return timedelta(seconds=seconds)


def _default_holder() -> str:
    """``<user>@<hostname>``, the auto-derived default for ``--holder``.

    A lane that wants a more specific self-identification (e.g. a hestia
    lane name rather than the OS user) passes ``--holder`` explicitly; most
    interactive/ad hoc invocations — including AC1's literal
    ``athenaeum quiesce --for 2h --reason x`` (issue athenaeum#1898), which
    supplies no ``--holder`` at all — rely on this default.
    """
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 — getuser() can raise in odd environments
        user = "unknown"
    return f"{user}@{socket.gethostname()}"


def cmd_quiesce(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum quiesce``'s three mutually exclusive modes."""
    knowledge_root = args.path.expanduser().resolve() if args.path else DEFAULT_KNOWLEDGE_ROOT

    if (args.release or args.status) and (
        args.for_duration is not None or args.reason is not None or args.holder is not None
    ):
        print(
            "error: --for/--reason/--holder cannot be combined with "
            "--release or --status — pick exactly one of the three "
            "quiesce modes.",
            file=sys.stderr,
        )
        return 1

    if args.release:
        released = release_quiesce(knowledge_root)
        print(
            json.dumps(
                {"command": "quiesce", "action": "release", "released": released}
            )
        )
        return 0

    if args.status:
        state = read_quiesce_state(knowledge_root)
        if state is None:
            payload: dict[str, object] = {
                "command": "quiesce",
                "action": "status",
                "active": False,
            }
        else:
            payload = {
                "command": "quiesce",
                "action": "status",
                "active": True,
                "holder": state.holder,
                "reason": state.reason,
                "created_at": now_iso(state.created_at),
                "expires_at": now_iso(state.expires_at),
            }
        print(json.dumps(payload))
        return 0

    # Default mode: set a new sentinel. --for/--reason are required together
    # here (argparse cannot express "required unless --release/--status" —
    # both flags default to None/False, so this is a runtime check).
    if args.for_duration is None or not args.reason:
        print(
            "error: --for and --reason are required unless --release or "
            "--status is given.",
            file=sys.stderr,
        )
        return 1

    holder = args.holder or _default_holder()
    cfg = load_config(knowledge_root)
    try:
        state = write_quiesce(
            knowledge_root,
            holder=holder,
            reason=args.reason,
            for_duration=args.for_duration,
            config=cfg,
        )
    except QuiesceDurationExceeded as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "command": "quiesce",
                "action": "set",
                "holder": state.holder,
                "reason": state.reason,
                "created_at": now_iso(state.created_at),
                "expires_at": now_iso(state.expires_at),
            }
        )
    )
    return 0


def add_quiesce_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum quiesce`` on ``subparsers`` (issue athenaeum#1898)."""
    parser = subparsers.add_parser(
        "quiesce",
        help="Pause `ingest --if-triggered` without touching the run lock "
        "(issue athenaeum#1898): --for/--reason sets a sentinel, --release "
        "clears it, --status reads it. A cooperative alternative to "
        "`launchctl bootout` for corpus write lanes.",
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=None,
        help="Knowledge directory the sentinel lives next to the run lock "
        "under (default: ~/knowledge).",
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--release",
        action="store_true",
        help="Remove the quiesce sentinel, if present. Idempotent — a "
        "no-op, not an error, when nothing is quiesced. Mutually exclusive "
        "with --status and with --for/--reason.",
    )
    mode.add_argument(
        "--status",
        action="store_true",
        help="Read-only: report whether a sentinel is currently active "
        "(and its holder/reason/expiry if so). Never writes anything, "
        "always exits 0. Mutually exclusive with --release and with "
        "--for/--reason.",
    )

    parser.add_argument(
        "--for",
        dest="for_duration",
        type=_parse_duration,
        default=None,
        metavar="DURATION",
        help="How long to quiesce, as <number>[h|m|s] (default unit hours), "
        "e.g. 2h, 90m, 3600s. Capped at the configured maximum "
        "(librarian.quiesce.max_hours, default 6h) — a longer request is "
        "rejected with a clear error, never silently clamped. Required "
        "(together with --reason) unless --release or --status is given.",
    )
    parser.add_argument(
        "--reason",
        default=None,
        help="Free-text reason recorded in the sentinel, for `--status` and "
        "for the log line `ingest --if-triggered` emits when it finds an "
        "active quiesce. Required (together with --for) unless --release "
        "or --status is given.",
    )
    parser.add_argument(
        "--holder",
        default=None,
        help="Who/what is holding the quiesce, recorded in the sentinel. "
        "Default: '<user>@<hostname>' (see _default_holder) — pass this "
        "explicitly for a lane that wants a more specific self-identification "
        "(e.g. a hestia lane name) than the OS user.",
    )
    parser.set_defaults(func=cmd_quiesce)
