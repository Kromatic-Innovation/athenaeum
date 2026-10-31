# SPDX-License-Identifier: Apache-2.0
"""Claude Code ``UserPromptSubmit`` hook adapter (issue athenaeum#1621).

A Tier-1 (per-turn push) adapter per
``docs/extending/sidecar-adapter-contract.md`` §3: reads the hook's stdin
JSON (``{"prompt": ..., "session_id": ...}``), calls
:func:`athenaeum.context.build_context_for_turn` — the SAME per-turn
sequence ``athenaeum context --stdin-json`` runs, session-dedup bookkeeping
and push telemetry included, so this adapter can never independently drift
from that CLI's output or side effects — and prints one line of Claude
Code hook-output JSON wrapping the envelope's rendered text.

**Shell-hook parity (issue athenaeum#1661, plus one point closed directly by
the athenaeum#1361 lane).** The athenaeum#1361 cutover was refused
because this adapter drifted from the live shell hook
(``examples/claude-code/user-prompt-recall.sh``) on six points, closed
here:

1. **``config.env`` loading.** :func:`_load_config_env` loads
   ``<cache_dir>/config.env`` into the process env, mirroring the shell
   hook's ``source config.env`` under ``set -a``
   (``user-prompt-recall.sh:127-145``) — most importantly ``ANTHROPIC_API_KEY``,
   without which LLM topic extraction silently degrades to the regex
   fallback (the query-10 regression the athenaeum#1361 lane observed). Per the
   issue's explicit Plan item 1, **existing process env always wins over
   the file** — a key already set is never overwritten, the opposite of
   plain ``source``'s last-write-wins, and duplicated (not shared) from
   :func:`athenaeum._cmd_audit._load_cache_config_env`'s identical
   ``athenaeum#1667`` counterpart, since this module deliberately never
   imports a ``_cmd_*.py`` sibling (see "Entry-point form" below).
2. **``AUTO_RECALL`` kill switch.** :func:`_auto_recall_enabled` mirrors
   ``AUTO_RECALL="${AUTO_RECALL:-true}"`` plus
   ``[ "$AUTO_RECALL" = "true" ] || exit 0``
   (``user-prompt-recall.sh:146,698``): unset defaults to enabled, any
   value other than the exact string ``"true"`` disables — no output, exit
   0.
3. **``SEARCH_BACKEND`` pass-through.** Read from the (now config.env-aware)
   process env and forwarded to :func:`~athenaeum.context.build_context_for_turn`
   as its ``search_backend`` argument, mirroring the shell default/override
   at ``user-prompt-recall.sh:147``. The adapter previously always used the
   function's ``"fts5"`` default, ignoring the knob entirely.
4. **Minimum prompt length.** A prompt shorter than 8 characters produces no
   output, mirroring ``user-prompt-recall.sh:711``
   (``[ -z "$PROMPT" ] || [ ${#PROMPT} -lt 8 ]``).
5. **Preamble.** The rendered ``additionalContext`` is now
   ``f"{envelope['render']['preamble']}\\n{envelope['render']['text']}"``,
   matching the shell hook's final ``printf`` at
   ``user-prompt-recall.sh:1213`` byte-for-byte (the preamble text itself is
   single-sourced from :data:`athenaeum.context.PREAMBLE`, so the two
   callers of that constant can never drift independently).
6. **Recall-cap ceiling.** :func:`_resolve_recall_cap_ceiling` mirrors the
   shell hook's
   ``CEILING="${ATHENAEUM_RECALL_CAP_CEILING:-${RECALL_CAP_CEILING:-7}}"``
   plus its non-numeric/non-positive-value guard, and the result is now
   forwarded to :func:`~athenaeum.context.build_context_for_turn` as its
   ``n`` argument. Found by the athenaeum#1361 lane's own re-replay, not by
   athenaeum#1661: the adapter previously never passed ``n`` at all, so it
   silently rode the core's own ``n=3`` CLI-convenience default — a much
   larger drift than the LLM-topic regression athenaeum#1661 closed, since
   it lost bullets 4 through the ceiling (7 by default) on **every** turn,
   not just LLM-scored ones.

7. **Relevance-cap overflow notice (issue athenaeum#1905).** The shell
   hook's awk ``END`` block appends one line — "memory has N more matching
   results (...) that were withheld by the relevance cap — call ``recall``
   to see them." — whenever candidates fell past the ceiling or the
   push-token budget (``user-prompt-recall.sh``'s ``$OVERFLOW_TMPL``
   render). This adapter emitted no such line, and athenaeum#1894 measured
   the cost: ``push_breadcrumb_pull`` 80.0% -> 70.5% against the
   2026-09-19 shell-hook reading. **The fix is deliberately NOT in this
   module** — counting what a cap withheld is ranking-and-budget
   arithmetic, which AC2 below forbids here. It lives in
   :func:`~athenaeum.context.build_context`, which widens each backend's
   fetch past the ceiling, tallies the withheld candidates by entity type,
   and renders the line (through :mod:`athenaeum.recall_overflow`, shared
   with the MCP ``recall`` surface) into the same
   ``envelope["render"]["text"]`` this module already wraps. So this
   adapter needed no code change at all for point 7 — which is the point:
   a Tier-1 adapter that reads a value the core rendered cannot drift from
   the core, and it is precisely the awk *re*-implementation that drifted.

**No SQL, no ranking, no budget arithmetic lives here** (issue athenaeum#1621
AC2; enforced by ``tests/test_claude_code_adapter.py``'s source-level scan).
Every ranking/dedup/budget decision already happened inside
:func:`~athenaeum.context.build_context` before this module ever sees a
candidate — this module reads ``envelope["render"]["text"]``, a value the
core already rendered, and does nothing to it beyond wrapping.

**Witness ledger wiring (issue athenaeum#2149).** :func:`main` reads stdin
exactly ONCE, before anything else, and passes the raw text to
:func:`promptwitness.harness.claude_code.record_turn` inside its own
``try``/``except`` — BEFORE both the ``AUTO_RECALL`` gate and the
``len(prompt) < 8`` early return, so a witness line is appended even on a
turn the recall path itself would skip. Gated on
``ATHENAEUM_WITNESS_LEDGER_ENABLED`` (:func:`_witness_ledger_enabled`,
default off), read after :func:`_load_config_env` — the same precedence as
``AUTO_RECALL``. The call is a single delegation with no arithmetic of its
own (AC2 forbids ``ast.BinOp`` in this module wholesale, and this call is no
exception): any exception the witness path raises — including a
``promptwitness`` that fails to import in a stale editable install — is
caught here and appended as ``{ts, error: <ExceptionClassName>}`` to
``<cache_dir>/witness_hook_errors.jsonl`` by :func:`_log_witness_error`,
with no text and no stderr, so a witness failure can NEVER surface as
session noise or change this adapter's stdout. :func:`witness_session_end`
is the equivalent in-process call for the ``SessionEnd`` hook, invoked by
:func:`athenaeum._cmd_index.cmd_session_end` rather than from this module's
own ``main`` (``SessionEnd`` has a different entry point entirely —
``athenaeum session-end``, not this console script).

``promptwitness`` is imported lazily, function-local to the ``try`` block
that calls it — never at this module's top level — so a bare
``import athenaeum.claude_code_adapter`` (what
``tests/test_import_budget.py``'s pinned set for this module measures)
never pays for it, mirroring how :mod:`athenaeum.context` is already
deferred into :func:`main` for the same reason.

**Fail-safe axis (issue athenaeum#1621 AC3).** A recall failure must never
block a user's turn: a ``UserPromptSubmit`` hook that exits non-zero, or
writes to stderr, degrades the whole session (a failing hook surfaces to
the user as session noise). So :func:`main` wraps the entire core call in
one broad ``except Exception`` and, on ANY failure — a corrupt cache, a
locked index file, a malformed or absent stdin payload, anything — prints
nothing and exits 0. The same applies when the core legitimately finds no
matching pages: ``render_text([])`` returns ``""``, and wrapping an empty
string in hook-output JSON would inject a visible-but-empty context block
into every turn, so this adapter also prints nothing in that case,
matching AC3's "no matching pages" clause exactly.

**Entry-point form.** Packaged as a ``[project.scripts]`` console-script
(``athenaeum-claude-hook``, see ``pyproject.toml``) rather than a new
``athenaeum <subcommand>``: ``_cmd_context.py``'s own docstring is explicit
that routing a latency-sensitive caller through the installed ``athenaeum``
script pays ``athenaeum.cli.build_parser()``'s cost of importing every
``_cmd_*`` module up front, and that loading ``context.py`` by file path
buys nothing over an ordinary import (measured, not assumed — see that
module's docstring and
``docs/measurements/retrieval-entry-point-measurements.md``). A console
script sidesteps both: installing this package wires a small, dedicated
launcher that imports only this module (which itself imports
:mod:`athenaeum.context` directly, never :mod:`athenaeum.cli`), so a
``UserPromptSubmit`` command in ``settings.json`` is one path — the same
shape as the existing shell hooks under ``examples/claude-code/``, pointed
at a venv's ``bin/`` instead of a ``.sh`` file — with no ``-m`` flag or
``PYTHONPATH`` for an installer to get wrong. It remains invocable as
``python -m athenaeum.claude_code_adapter`` too (the ``__main__`` guard
below), for anyone exercising a source checkout before installing the
package.

Layering: L5 (presentation) — same tier as every ``_cmd_*.py`` module and
for the same reason (a process-level concern: stdin parsing, exit codes,
JSON output), but deliberately NOT one of the ``_cmd_*.py`` siblings
``cli.py``'s factoring rule governs, since it is deliberately unreachable
through ``athenaeum.cli``.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from athenaeum.config import (
    RECALL_CAP_CEILING_DEFAULT,
    resolve_cache_dir,
    resolve_hook_budget_seconds,
)

#: Default periodic in-session anchor interval, in minutes (issue
#: athenaeum#2156), read hook-time via ``ATHENAEUM_WITNESS_ANCHOR_INTERVAL_MINUTES``
#: (see :func:`_witness_anchor_interval_minutes`). ``0`` disables periodic
#: anchoring entirely. Mirrors the yaml default
#: ``librarian.receipt_anchor.anchor_interval_minutes`` resolves to
#: (:func:`athenaeum.config.resolve_receipt_anchor_anchor_interval_minutes`)
#: -- the two are deliberately independent knobs (the hook never loads
#: athenaeum.yaml) but share the same default on purpose.
_WITNESS_ANCHOR_INTERVAL_MINUTES_DEFAULT = 15


def _load_config_env(cache_dir: Path) -> None:
    """Load ``<cache_dir>/config.env`` into the process env (issue athenaeum#1661;
    mirrors the shell hook's ``source config.env`` under ``set -a`` at
    ``examples/claude-code/user-prompt-recall.sh:127-145``, and duplicates
    :func:`athenaeum._cmd_audit._load_cache_config_env`'s identical
    ``athenaeum#1667`` counterpart — see the module docstring's "Shell-hook
    parity" section for why this is a duplication, not a shared helper).

    ``KEY=VALUE`` lines only; blank lines and ``#``-comments are skipped.
    **Existing process env always wins** — a key already set (by the
    launcher's environment, or earlier this same process) is never
    overwritten by the file. A missing or unreadable file is a silent
    no-op. Values are NEVER printed or logged (issue athenaeum#1661 AC6) —
    this function has no ``print``/``log`` call on any path, by
    construction.
    """
    # `.joinpath(...)`, not `cache_dir / "config.env"`: the `/` operator
    # parses to an `ast.BinOp`, which this module's own AC2 guard
    # (`tests/test_claude_code_adapter.py::TestAC2NoRetrievalLogic`) forbids
    # wholesale as a clean proxy for "no budget arithmetic" — a false
    # positive for path-joining specifically, sidestepped here rather than
    # loosening the guard.
    config_env_path = cache_dir.joinpath("config.env")
    try:
        if not config_env_path.is_file():
            return
        lines = config_env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip()


def _resolve_recall_cap_ceiling() -> int:
    """Mirrors the shell hook's ``CEILING`` resolution (issue athenaeum#1361
    replay finding, closing the parity gap athenaeum#1661 did not cover):
    ``user-prompt-recall.sh``'s
    ``CEILING="${ATHENAEUM_RECALL_CAP_CEILING:-${RECALL_CAP_CEILING:-7}}"``
    plus its ``case ''|*[!0-9]*) CEILING=7 ;; 0) CEILING=7 ;;`` guard against
    a non-numeric or non-positive override. ``RECALL_CAP_CEILING`` (no
    ``ATHENAEUM_`` prefix) is the name ``session-start-recall.sh`` caches
    ``recall.cap.ceiling`` into ``config.env`` under, mirroring
    ``PUSH_TOKEN_BUDGET``'s own cached-name shape; ``ATHENAEUM_RECALL_CAP_CEILING``
    is the explicit env override and always wins. Must run AFTER
    :func:`_load_config_env`, same as :func:`_auto_recall_enabled`, so a
    ceiling cached in ``config.env`` is honoured the same as one set
    directly in the launcher's environment.

    The core's own ``n`` default (3, see ``_cmd_context.py``) is a CLI
    convenience unrelated to this ceiling — this adapter must never fall
    back to it, or every turn silently loses whatever the shell hook would
    have rendered past the third bullet.
    """
    raw = os.environ.get("ATHENAEUM_RECALL_CAP_CEILING") or os.environ.get("RECALL_CAP_CEILING")
    if not raw:
        return RECALL_CAP_CEILING_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return RECALL_CAP_CEILING_DEFAULT
    return value if value > 0 else RECALL_CAP_CEILING_DEFAULT


def _auto_recall_enabled() -> bool:
    """Mirrors the shell hook's ``AUTO_RECALL`` default-and-gate
    (``examples/claude-code/user-prompt-recall.sh:146,698``): unset defaults
    to enabled; any value other than the exact string ``"true"`` disables.
    Must run AFTER :func:`_load_config_env`, so a kill switch cached in
    ``config.env`` is honoured the same as one set directly in the
    launcher's environment.
    """
    return os.environ.get("AUTO_RECALL", "true") == "true"


def _witness_ledger_enabled() -> bool:
    """``ATHENAEUM_WITNESS_LEDGER_ENABLED`` gate (issue athenaeum#2149;
    default OFF, the opposite default of :func:`_auto_recall_enabled`, but
    read the same way: must run AFTER :func:`_load_config_env`, so a value
    cached in ``config.env`` is honoured the same as one set directly in
    the launcher's environment). Exact-string match: only ``"true"``
    enables; unset or any other value stays disabled."""
    return os.environ.get("ATHENAEUM_WITNESS_LEDGER_ENABLED", "false") == "true"


def _witness_anchor_interval_minutes() -> int:
    """``ATHENAEUM_WITNESS_ANCHOR_INTERVAL_MINUTES`` (issue athenaeum#2156),
    read the same way as :func:`_auto_recall_enabled` /
    :func:`_witness_ledger_enabled`: must run AFTER :func:`_load_config_env`,
    so a value cached in ``config.env`` is honoured the same as one set
    directly in the launcher's environment -- the hook never loads
    ``athenaeum.yaml`` at all, so this env var (not the yaml resolver
    ``librarian.receipt_anchor.anchor_interval_minutes``,
    :func:`athenaeum.config.resolve_receipt_anchor_anchor_interval_minutes`)
    is this knob's ONLY home at hook time.

    Default 15 minutes; ``0`` disables periodic in-session anchoring
    entirely. A non-numeric or negative override falls back to the
    default rather than raising -- this is a hook-time env knob, not a
    yaml resolver, so it must degrade exactly like
    :func:`_resolve_recall_cap_ceiling` rather than ever becoming a second
    way for this adapter to fail a turn.
    """
    raw = os.environ.get("ATHENAEUM_WITNESS_ANCHOR_INTERVAL_MINUTES")
    if not raw:
        return _WITNESS_ANCHOR_INTERVAL_MINUTES_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return _WITNESS_ANCHOR_INTERVAL_MINUTES_DEFAULT
    return value if value >= 0 else _WITNESS_ANCHOR_INTERVAL_MINUTES_DEFAULT


def _spawn_detached_anchor(ledger_dir: Path) -> None:
    """Spawn a fully detached ``promptwitness anchor`` process anchoring
    the current witness chain head only (issue athenaeum#2156; one
    subject, no Merkle batching, no per-turn proofs). Zero added prompt
    latency: stdio is closed (no pipe for this process to ever read or
    flush), ``start_new_session=True`` detaches the child into its own
    session, and this function never calls ``.wait()``/``.communicate()``
    on the returned :class:`subprocess.Popen` -- it is discarded
    immediately, so the caller can return without waiting on it. This
    process itself does no network I/O; any network call the child makes
    happens entirely after this function has already returned.
    """
    subprocess.Popen(
        [sys.executable, "-m", "promptwitness.cli", "anchor", "--dir", str(ledger_dir)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _check_and_spawn_periodic_anchor(ledger_dir: Path) -> None:
    """The periodic in-session anchor check (issue athenaeum#2156):
    compare the age of the newest anchor activity with the configured
    interval, and spawn at most one detached anchor process per interval
    across concurrent sessions. A single delegation to
    :func:`promptwitness.anchor_schedule.claim_periodic_anchor_slot` plus
    :func:`_spawn_detached_anchor` -- no arithmetic of its own beyond
    reading the interval, matching this module's AC2 guard.
    """
    from promptwitness.anchor_schedule import claim_periodic_anchor_slot

    interval = _witness_anchor_interval_minutes()
    if claim_periodic_anchor_slot(ledger_dir, interval):
        _spawn_detached_anchor(ledger_dir)


def _log_witness_error(cache_dir: Path, exc: BaseException) -> None:
    """Append one ``{ts, error: <ExceptionClassName>}`` line to
    ``<cache_dir>/witness_hook_errors.jsonl`` (issue athenaeum#2149's silent
    no-op guard). Never raises itself — logging a witness failure must
    never become a second failure that reaches this adapter's own
    ``except Exception`` and gets swallowed with no trace at all."""
    try:
        path = cache_dir.joinpath("witness_hook_errors.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()
        record = json.dumps({"ts": ts, "error": type(exc).__name__})
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{record}\n")
    except Exception:  # noqa: BLE001 - logging a failure must never itself raise
        pass


def _read_hook_input(raw: str) -> tuple[str, str]:
    """Parse the ``UserPromptSubmit`` stdin payload.

    Never raises: a malformed or absent payload degrades to an empty
    prompt, which :func:`main` then short-circuits on — the same fail-safe
    contract as a core failure (issue athenaeum#1621 AC3).
    """
    try:
        payload: dict[str, Any] = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    prompt = str(payload.get("prompt") or "")
    session_id = str(payload.get("session_id") or "unknown")
    return prompt, session_id


def _witness_turn(cache_dir: Path, raw: str) -> None:
    """Gate, import, and delegate to
    :func:`promptwitness.harness.claude_code.record_turn` for one
    ``UserPromptSubmit`` turn, then run the periodic in-session anchor
    check (issue athenaeum#2156) AFTER that ledger line has been appended
    -- the age comparison is always against the chain this very turn just
    extended. Any exception from either step — including ``promptwitness``
    failing to import — is caught and logged via :func:`_log_witness_error`;
    this function itself never raises, and a periodic-anchor failure can
    no more block a prompt than the witness write it follows.
    """
    if not _witness_ledger_enabled():
        return
    try:
        from promptwitness import ledger as pw_ledger
        from promptwitness.harness.claude_code import record_turn

        record_turn(raw, os.environ)
        _check_and_spawn_periodic_anchor(pw_ledger.resolve_ledger_dir("claude-code"))
    except Exception as exc:  # noqa: BLE001 - a witness failure must never block a prompt
        _log_witness_error(cache_dir, exc)


def witness_session_end(cache_dir: Path) -> None:
    """The ``SessionEnd`` counterpart to :func:`_witness_turn`, called
    in-process from :func:`athenaeum._cmd_index.cmd_session_end` (issue
    athenaeum#2149) — ``SessionEnd`` has its own entry point
    (``athenaeum session-end``), not this console script's ``main``, but
    the gating and silent-no-op-guard shape are identical.

    The guard covers ``_load_config_env`` and the gate read too, not just
    the witness call. Those two ran outside it, so a malformed
    ``config.env`` -- one stray non-UTF-8 byte is enough for a
    ``UnicodeDecodeError`` -- escaped into
    :func:`athenaeum._cmd_index.cmd_session_end` and aborted the whole
    command. A witness that can break the thing it observes is worse than
    no witness: the entire point of this path is that it cannot affect the
    caller's result or exit code. :func:`main` already wraps its own
    ``_load_config_env`` call for the same reason."""
    try:
        _load_config_env(cache_dir)
        if not _witness_ledger_enabled():
            return
        from promptwitness.harness.claude_code import session_close

        session_close(os.environ)
    except Exception as exc:  # noqa: BLE001 - a witness failure must never block SessionEnd
        _log_witness_error(cache_dir, exc)


def witness_session_end_anchor(cache_dir: Path, wiki_root: Path) -> None:
    """The SessionEnd anchor step (issue athenaeum#2156): anchors the
    current witness chain head, plus every receipt partition under
    *wiki_root* that changed since its own last anchor, as ``anchor``
    lines in the local witness ledger. Takes NO run lock — mirrors the
    design's lock rule that the hook path and SessionEnd both record
    anchors lock-free — and is self-gated/guarded exactly like
    :func:`witness_session_end`: ``ATHENAEUM_WITNESS_LEDGER_ENABLED`` (read
    after :func:`_load_config_env`), and any exception from either the
    gate read or the delegated anchor work is caught and logged via
    :func:`_log_witness_error` rather than ever affecting SessionEnd's own
    exit code.

    The actual work -- budget/deadline tracking, receipt-partition
    digesting, and the anchor calls themselves -- lives in
    :mod:`athenaeum.witness_anchor`, a SEPARATE module from this one,
    deliberately: this module's own AC2 guard
    (``tests/test_claude_code_adapter.py::TestAC2NoRetrievalLogic``)
    forbids any arithmetic expression (``ast.BinOp``) anywhere in its
    source, and a wall-clock budget is unavoidably arithmetic. This
    function is therefore, like :func:`witness_session_end`, a single
    delegation call with no arithmetic of its own.
    """
    try:
        _load_config_env(cache_dir)
        if not _witness_ledger_enabled():
            return
        from athenaeum.witness_anchor import run_session_end_anchor
        from promptwitness import ledger as pw_ledger

        run_session_end_anchor(pw_ledger.resolve_ledger_dir("claude-code"), wiki_root)
    except Exception as exc:  # noqa: BLE001 - a witness failure must never block SessionEnd
        _log_witness_error(cache_dir, exc)


class _HookTimeoutExceeded(BaseException):
    """Raised by :func:`_run_within_timeout`'s alarm handler. Private --
    callers see it only inside their own ``try``/``except``.

    Deliberately ``BaseException``, not ``Exception`` (Seer finding on
    this issue's own PR): the SIGALRM handler can fire while execution is
    anywhere inside :func:`~athenaeum.context.build_context_for_turn`,
    including beneath one of its own broad, best-effort
    ``except Exception`` blocks (the vector leg, the LLM topic-extraction
    leg). A plain ``Exception`` subclass would be swallowed right there --
    no `step_timeout` recorded, and worse, the alarm is one-shot, so
    whatever ran AFTER the swallowed exception would then execute with no
    time bound left at all. Subclassing ``BaseException`` instead is the
    same reason ``KeyboardInterrupt``/``SystemExit`` do: a bare
    ``except Exception`` cannot catch it, so it always reaches this
    module's own ``except _HookTimeoutExceeded`` in :func:`main`,
    regardless of which inner frame it was raised from.
    """


def _run_within_timeout(timeout_seconds: int, fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Run *fn* bound by *timeout_seconds* (issue athenaeum#2204 AC1: this
    adapter SHARES the one foreground budget
    :func:`athenaeum.config.resolve_hook_budget_seconds` resolves, the same
    value ``session-start-recall.sh`` reads).

    Deliberately an OS-level ``signal.alarm`` timer, NOT a Python-side
    deadline/remaining-time computation: this module's own AC2 guard
    (``tests/test_claude_code_adapter.py::TestAC2NoRetrievalLogic``) forbids
    any arithmetic expression in this module's source wholesale, and
    ``signal.alarm(seconds)`` takes an absolute second count directly, so
    bounding a call this way needs no arithmetic at all here (the
    arithmetic, if any, lives inside the resolver this module only calls).

    POSIX-only (``signal.alarm`` doesn't exist on Windows, a declared
    non-goal for this issue) -- falls back to an unbounded call when
    unavailable, matching this module's existing fail-open posture rather
    than refusing to run at all.
    """
    if not hasattr(signal, "alarm"):
        return fn(*args, **kwargs)

    def _on_alarm(signum: int, frame: Any) -> None:
        raise _HookTimeoutExceeded()

    previous_handler = signal.signal(signal.SIGALRM, _on_alarm)
    signal.alarm(timeout_seconds)
    try:
        return fn(*args, **kwargs)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)


def _safe_session_notice(cache_dir: Path, session_id: str) -> str | None:
    """:func:`athenaeum.hook_status.maybe_session_notice`, isolated (issue
    athenaeum#2204 AC9).

    Function-local import, matching this module's existing deferral of
    :mod:`athenaeum.context`/``promptwitness`` -- keeps a plain
    ``import athenaeum.claude_code_adapter`` from paying for
    :mod:`athenaeum.hook_status` (``tests/test_import_budget.py``'s pinned
    set for this module). Wrapped in its own ``try``/``except``, same
    isolation shape as :func:`_witness_turn`: a failure here (corrupt
    status file, read-only cache dir) must never take down the recall call
    that follows it.
    """
    try:
        from athenaeum.hook_status import maybe_session_notice

        return maybe_session_notice(cache_dir, session_id)
    except Exception:  # noqa: BLE001 - a notice-check failure must never block a prompt
        return None


def _safe_record_degradation(cache_dir: Path, hook: str, event: str, detail: str) -> None:
    """:func:`athenaeum.hook_status.record_degradation`, isolated the same
    way as :func:`_safe_session_notice` -- recording a degradation must
    never itself become a second failure."""
    try:
        from athenaeum.hook_status import record_degradation

        record_degradation(cache_dir, hook, event, detail)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 - recording a degradation must never itself degrade
        pass


def _hook_output(additional_context: str) -> str:
    """Wrap rendered text in Claude Code's hook-output envelope.

    Must be wrapped in ``hookSpecificOutput.hookEventName`` — a flat
    ``{"additionalContext": ...}`` payload is silently ignored by Claude
    Code (see ``examples/claude-code/user-prompt-recall.sh``'s own note on
    this same requirement).
    """
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": additional_context,
            }
        }
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point. Always returns 0 — see the module docstring's
    fail-safe axis. ``argv`` is accepted (and ignored) only so this
    matches the zero-argument console-script calling convention; the hook
    input is read from stdin, per the ``UserPromptSubmit`` contract.
    """
    try:
        cache_dir = resolve_cache_dir(None)
        _load_config_env(cache_dir)

        # Read stdin exactly once, before anything else below — the witness
        # call (issue athenaeum#2149) needs the raw text even on a turn the
        # recall path itself is about to skip (AUTO_RECALL off, or a prompt
        # shorter than 8 characters), so it runs BEFORE both of those gates.
        raw = sys.stdin.read()
        _witness_turn(cache_dir, raw)

        # Issue athenaeum#2204 AC9: parsed here, before the AUTO_RECALL and
        # length gates below, so a degradation notice still surfaces on a
        # turn the recall path itself is about to skip — the notice is
        # about the HOOK's health, not about this particular prompt.
        prompt, session_id = _read_hook_input(raw)
        notice = _safe_session_notice(cache_dir, session_id)

        if not _auto_recall_enabled():
            if notice:
                print(_hook_output(notice))
            return 0

        if len(prompt) < 8:
            if notice:
                print(_hook_output(notice))
            return 0

        from athenaeum.context import build_context_for_turn

        # Issue athenaeum#2204 AC1: this adapter shares the ONE foreground
        # budget `session-start-recall.sh` reads, via the same resolver —
        # see `_run_within_timeout`'s docstring for why this is an
        # OS-level alarm rather than a Python-side deadline computation.
        timeout_seconds = resolve_hook_budget_seconds(None)
        text = ""
        preamble = ""
        try:
            envelope = _run_within_timeout(
                timeout_seconds,
                build_context_for_turn,
                prompt,
                session_id,
                cache_dir=cache_dir,
                search_backend=os.environ.get("SEARCH_BACKEND", "vector"),
                n=_resolve_recall_cap_ceiling(),
            )
            text = envelope["render"]["text"]
            preamble = envelope["render"]["preamble"]
        except _HookTimeoutExceeded:
            _safe_record_degradation(
                cache_dir,
                "user-prompt",
                "step_timeout",
                f"build_context_for_turn exceeded the foreground hook budget ({timeout_seconds}s)",
            )

        if text:
            body = f"{preamble}\n{text}"
            print(_hook_output(f"{notice}\n{body}" if notice else body))
        elif notice:
            print(_hook_output(notice))
    except Exception:  # noqa: BLE001 — a recall failure must never block a prompt (AC3)
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
