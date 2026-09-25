# SPDX-License-Identifier: Apache-2.0
"""Pure trigger evaluation for reasoning-tier runs (issue athenaeum#909).

NOT :mod:`athenaeum.tiers` (the T0-T4 entity-COMPILATION pipeline) and NOT
:mod:`athenaeum.reasoning_tiers` (the T1/T2 merge-proposal reasoning-tier
CASCADE, haiku-then-opus screening of pending merges before they reach a
human). Both of those names are already taken by different pipelines this
codebase works hard to keep distinct — see :mod:`athenaeum.reasoning_tiers`'s
own module docstring. This module answers a narrower, upstream question:
WHEN should a budgeted, resumable, incremental reasoning run happen at all?

The answer used to be "once a night, whatever an operator's external cron /
launchd invokes" — there is no in-repo scheduler
(:func:`athenaeum.config.resolve_pull_before_run`'s docstring: "There is no
shipped nightly cron wrapper in this repo"). Tying reasoning to that one
nightly window means a bad night is invisible for 24h and a large batch
waits a full day. athenaeum#909 replaces the single window with a small set of
configurable triggers — backlog depth (file count or byte size), an elapsed
interval, and on-demand — with the nightly schedule demoted to a BACKSTOP
that only fires when nothing else did. EVERY trigger, once fired, drives the
exact same call: the existing scoped incremental-ingest CLI poke
(``athenaeum ingest``, wired to :func:`athenaeum.librarian.ingest`) — never a
full recompile. See :mod:`athenaeum._cmd_index`'s ``--if-triggered`` flag on
``cmd_ingest`` for the caller that evaluates these triggers against live
state and, on a fire, runs the ingest; this module contains no I/O at all.

This module is deliberately pure and side-effect-free: it takes already-
gathered facts (backlog counts, elapsed time, an on-demand flag, config, an
already-read quiesce sentinel) and returns a :class:`TriggerDecision` naming
whether and why a run should happen. All I/O — discovering the backlog
(:func:`athenaeum.intake.discover_raw_files` /
:func:`athenaeum.intake.discover_raw_backlog_bytes`), reading the last-
triggered-run stamp, reading the operator quiesce sentinel
(:mod:`athenaeum.quiesce`, issue athenaeum#1898), acquiring the run lock,
actually invoking the ingest — lives in the CLI caller. That split is what
makes the trigger LOGIC trivially unit-testable without a knowledge git
repo, chromadb, or an LLM.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Literal

from athenaeum.config import (
    resolve_reasoning_trigger_backlog_bytes,
    resolve_reasoning_trigger_backlog_files,
    resolve_reasoning_trigger_interval_hours,
    resolve_reasoning_trigger_nightly_backstop_hours,
)

if TYPE_CHECKING:
    # Type-only: keeps this module's real (runtime) import list at zero I/O,
    # per its own "no I/O at all" docstring paragraph — see
    # :mod:`athenaeum.quiesce`'s module docstring for the split this mirrors
    # (the sibling ``_cli_shared.py`` does the identical
    # ``if TYPE_CHECKING: from athenaeum.runlock import RunLock`` trick for
    # the same reason).
    from athenaeum.quiesce import QuiesceState

#: Which trigger fired this evaluation, or ``"none"`` when nothing fired.
#: ``"quiesced"`` (issue athenaeum#1898) is the other non-firing reason — see
#: :func:`evaluate_triggers`'s ``quiesce`` parameter.
TriggerReason = Literal[
    "backlog-files",
    "backlog-bytes",
    "interval",
    "on-demand",
    "nightly-backstop",
    "quiesced",
    "none",
]


@dataclass(frozen=True)
class TriggerDecision:
    """The outcome of one :func:`evaluate_triggers` call.

    ``fired`` is ``True`` iff a reasoning run should happen now; ``reason``
    names WHICH trigger fired. ``fired is False`` pairs with ``reason`` in
    ``{"none", "quiesced"}`` and vice versa — never any other reason value —
    while ``fired is True`` pairs with every other reason (issue athenaeum#1898
    added the second non-firing reason; before it, ``fired is False`` paired
    with ``reason == "none"`` alone). :func:`evaluate_triggers` is the only
    constructor call site and maintains that invariant; it is not
    re-validated here (this dataclass is otherwise a plain, immutable
    value — no behavior of its own).
    """

    fired: bool
    reason: TriggerReason


def evaluate_triggers(
    *,
    backlog_files: int,
    backlog_bytes: int,
    since_last_run: timedelta | None,
    on_demand: bool,
    config: dict[str, Any] | None,
    quiesce: "QuiesceState | None" = None,
) -> TriggerDecision:
    """Decide whether a triggered reasoning run should happen now.

    Pure and side-effect-free: every input is an already-gathered fact
    (a backlog count, an elapsed duration, a flag, an already-read quiesce
    sentinel), never a live filesystem/clock read — the caller gathers those
    and passes them in. Evaluation order (first match wins, so a run
    triggered by more than one condition reports the highest-priority one):

    1. ``on_demand`` — an explicit ask always fires, unconditionally, and is
       checked BEFORE quiesce below — see that step for why.
    2. ``quiesce`` (issue athenaeum#1898) — when not ``None`` (the caller already
       determined the sentinel exists and has not expired — see
       :func:`athenaeum.quiesce.read_quiesce_state`), returns a non-firing
       ``"quiesced"`` verdict UNCONDITIONALLY, before any threshold below is
       even consulted. This sits directly after ``on_demand`` and before
       every other check because a quiesce sentinel is an operator asking to
       pause the SCHEDULER specifically — ``athenaeum ingest --if-triggered``
       (:func:`athenaeum._cmd_index._evaluate_ingest_trigger`) always calls
       this with ``on_demand=False``, so on-demand runs (a direct
       ``athenaeum ingest`` with no ``--if-triggered``, or any future caller
       that legitimately sets ``on_demand=True``) are structurally
       unaffected by a sentinel that suppresses only the scheduler's own
       backlog/interval/backstop checks — the ``on_demand`` check above
       already returned before this one is reached.
    3. Backlog-by-file-count vs ``librarian.reasoning_triggers.backlog_files``
       (:func:`athenaeum.config.resolve_reasoning_trigger_backlog_files`;
       ``None`` — the default, key unset — disables this trigger).
    4. Backlog-by-bytes vs ``librarian.reasoning_triggers.backlog_bytes``
       (:func:`athenaeum.config.resolve_reasoning_trigger_backlog_bytes`;
       ``None`` disables). Literal on-disk bytes, not a cost estimate — see
       :func:`athenaeum.intake.discover_raw_backlog_bytes`.
    5. Elapsed interval vs ``librarian.reasoning_triggers.interval_hours``
       (:func:`athenaeum.config.resolve_reasoning_trigger_interval_hours`;
       ``None`` disables this trigger entirely).
    6. Nightly backstop vs
       ``librarian.reasoning_triggers.nightly_backstop_hours`` (default 24,
       :func:`athenaeum.config.resolve_reasoning_trigger_nightly_backstop_hours`)
       — ONLY reached when none of 1-5 fired. This is AC7's literal wording
       ("the nightly schedule still runs as a backstop when no other trigger
       has fired") encoded directly as evaluation order: the backstop check
       is physically the last ``if`` in this function, so it is structurally
       unreachable once an earlier trigger has already returned.

    ``since_last_run is None`` (no completed triggered run has ever been
    recorded — no stamp) is treated as "infinitely overdue" for BOTH the
    interval and backstop checks, mirroring this codebase's existing
    missing-stamp convention (:func:`athenaeum.librarian._load_full_compile_stamp`
    returning ``None`` makes ``full_compile_due`` ``True`` — see
    :func:`athenaeum.config.resolve_full_compile_every_days`). Without this,
    a fresh install with no prior run would never satisfy either
    ``since_last_run >= threshold`` comparison and neither trigger could
    ever fire on its own — a silent dead trigger, not merely a slow first
    reconciliation. An operator who wants BOTH triggers off from the start
    disables them via config (leave ``interval_hours`` unset; the backstop
    has no "off" — see its resolver's docstring for why).

    Every configured threshold is a "reaches or exceeds" (``>=``) comparison.
    """
    if on_demand:
        return TriggerDecision(fired=True, reason="on-demand")

    if quiesce is not None:
        return TriggerDecision(fired=False, reason="quiesced")

    backlog_files_threshold = resolve_reasoning_trigger_backlog_files(config)
    if (
        backlog_files_threshold is not None
        and backlog_files >= backlog_files_threshold
    ):
        return TriggerDecision(fired=True, reason="backlog-files")

    backlog_bytes_threshold = resolve_reasoning_trigger_backlog_bytes(config)
    if (
        backlog_bytes_threshold is not None
        and backlog_bytes >= backlog_bytes_threshold
    ):
        return TriggerDecision(fired=True, reason="backlog-bytes")

    interval_threshold = resolve_reasoning_trigger_interval_hours(config)
    if interval_threshold is not None and (
        since_last_run is None
        or since_last_run >= timedelta(hours=interval_threshold)
    ):
        return TriggerDecision(fired=True, reason="interval")

    backstop_threshold = resolve_reasoning_trigger_nightly_backstop_hours(config)
    if since_last_run is None or since_last_run >= timedelta(
        hours=backstop_threshold
    ):
        return TriggerDecision(fired=True, reason="nightly-backstop")

    return TriggerDecision(fired=False, reason="none")
