# SPDX-License-Identifier: Apache-2.0
"""One-command supervised API+batch drain of the raw-intake backlog (issue athenaeum#470).

When the raw-intake backlog outgrows the nightly caps, the operator used to find
out only by reading logs, and the API+batch remedy lived as tribal knowledge
spread across env vars and flags. This module is the supervised remedy:

* **Drain orchestrator** — :func:`run_drain`, a thin loop over the existing
  :func:`athenaeum.librarian.run` machinery. It forces the API+batch path (the
  athenaeum#236 50%-token-discount transport) and an UNBOUNDED run (batch block-polls; a
  finite deadline is the known cwc#615 failure mode), and guards a MANDATORY
  cumulative dollar ceiling across every intake window.

The companion **ETA advisor** — the pure estimators over the athenaeum#378 spend ledger
and :func:`~athenaeum.drain_advisor.build_advisory` — moved to the
:mod:`athenaeum.drain_advisor` leaf in issue athenaeum#640. It formerly lived here, but
because :mod:`athenaeum.librarian` and :mod:`athenaeum.status` both need
``build_advisory`` while sitting BELOW this orchestrator (``run_drain`` calls
``librarian.run``), they reached back UP into this module for it — the
``librarian`` <-> ``drain`` / ``status`` -> ``drain`` back-edges of a residual
import SCC. Hoisting the advisor down to a leaf dissolved that cycle.

Athenaeum performs no credential handling (issue athenaeum#284/#330): the drain requires
``ANTHROPIC_API_KEY`` in the environment and errors out naming that requirement
if it is absent — it never mints, guesses, or hardcodes a credential.

Layering: L4 domain/pipeline module — a thin orchestration wrapper ABOVE
:mod:`athenaeum.librarian` (imported via a deferred/function-local import to
avoid a top-level cycle with the librarian's own module graph; keep it deferred,
don't hoist it). Otherwise imports only the L3 :mod:`athenaeum.spend` service.
Factoring rule: only the supervised-loop orchestration belongs here; the ETA
estimation lives in :mod:`athenaeum.drain_advisor`, the actual intake/merge
machinery stays in :mod:`athenaeum.librarian`, and CLI arg parsing/gating stays
in :mod:`athenaeum._cmd_drain`.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from athenaeum import spend

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Drain orchestrator + pre-flight guards (the ETA advisor moved to
# :mod:`athenaeum.drain_advisor` in issue athenaeum#640 to break the librarian/status
# import back-edges into this L4 orchestrator)
# ---------------------------------------------------------------------------


def check_api_key(env: dict[str, str] | None = None) -> str | None:
    """Return an error string when ``ANTHROPIC_API_KEY`` is absent, else None."""
    environ = env if env is not None else os.environ
    if not environ.get("ANTHROPIC_API_KEY"):
        return (
            "athenaeum drain requires ANTHROPIC_API_KEY in the environment: the "
            "drain runs the metered API + Batch path (issue athenaeum#236) and athenaeum "
            "performs no credential handling (issue athenaeum#284/#330). Set "
            "ANTHROPIC_API_KEY and retry."
        )
    return None


def check_batch_deadline(*, max_runtime: int) -> str | None:
    """Return an error string when a finite run deadline is in effect (cwc#615).

    The drain always runs batch mode, which block-polls the Anthropic Batch API;
    a finite wall-clock deadline kills the run mid-batch, wasting the submitted
    (already-billed) batch. The drain therefore requires an UNBOUNDED run.
    """
    if (
        isinstance(max_runtime, int)
        and not isinstance(max_runtime, bool)
        and max_runtime > 0
    ):
        return (
            f"athenaeum drain requires an unbounded run, but a finite run deadline "
            f"is in effect (max_runtime={max_runtime}s via ATHENAEUM_MAX_RUNTIME / "
            f"librarian.max_runtime). Batch mode block-polls the Anthropic Batch "
            f"API; a bounded window is the cwc#615 failure mode (the run is killed "
            f"mid-batch, wasting the submitted batch). Unset the deadline (or set "
            f"it to 0) and retry."
        )
    return None


def resolve_drain_runtime(
    config: dict[str, Any] | None, env: dict[str, str] | None = None
) -> int:
    """Resolve the run deadline the drain would face: env > yaml > 0 (unbounded).

    Unlike :func:`athenaeum.librarian.librarian_max_runtime` (default 3600s), the
    drain default is UNBOUNDED (0): the drain forces ``max_runtime=0``. A finite
    value here means the operator EXPLICITLY set one, which
    :func:`check_batch_deadline` then refuses.
    """
    environ = env if env is not None else os.environ
    raw_env = environ.get("ATHENAEUM_MAX_RUNTIME")
    if raw_env is not None:
        try:
            return int(raw_env)
        except (TypeError, ValueError):
            pass
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("max_runtime")
            if isinstance(raw, int) and not isinstance(raw, bool):
                return raw
    return 0


def _ledger_writable(ledger_path: Path) -> bool:
    """Return True if the spend ledger at *ledger_path* can be appended to.

    Issue athenaeum#568 (H1): :func:`run_drain`'s MANDATORY cumulative dollar ceiling is
    computed by re-reading this ledger every window (:func:`drain_spend_usd`).
    If ledger writes fail silently — bad ``ATHENAEUM_SPEND_LEDGER`` path, wrong
    permissions, a full disk — ``drain_spend_usd`` returns ``0.0`` forever and
    the total spend becomes ``max_usd × number_of_windows``: unbounded real
    dollars, with the guard the docstring calls MANDATORY reading blind. So we
    probe writability up front using the same ``O_APPEND | O_CREAT`` open the
    real writer (:func:`spend._append_line`) uses, and abort loudly on failure
    rather than proceed. Creating an empty ledger file here is harmless — an
    empty ledger is valid and is exactly what the first real write would make.
    """
    try:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(ledger_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        os.close(fd)
        return True
    except OSError as exc:
        log.error(
            "athenaeum drain: spend ledger %s is NOT writable (%s) — the "
            "cumulative dollar ceiling re-reads this ledger every window, so "
            "proceeding would spend up to $max_usd PER WINDOW with no "
            "cumulative bound. Aborting (issue athenaeum#568). Fix ATHENAEUM_SPEND_LEDGER "
            "/ permissions / free disk space and retry.",
            ledger_path,
            exc,
        )
        return False


def drain_spend_usd(ledger_path: Path, *, since: datetime) -> float:
    """Sum metered API dollars recorded in the ledger since *since*.

    The drain-session cumulative-spend accounting: reads the athenaeum#378 ledger
    (tolerating torn lines) and sums ``estimated_cost_usd`` across every
    non-subscription record written since the drain started. Subscription
    (``claude-cli``) rows are always $0 and skipped.
    """
    total = 0.0
    for record in spend.read_ledger(ledger_path, since=since):
        if record.get("provider") == spend.PROVIDER_CLAUDE_CLI:
            continue
        total += float(record.get("estimated_cost_usd", 0.0) or 0.0)
    return total


def run_drain(
    *,
    knowledge_root: Path,
    raw_root: Path,
    wiki_root: Path,
    max_usd: float,
    max_files: int | None,
    config: dict[str, Any] | None = None,
    ledger_path: Path | None = None,
    run_fn: Callable[..., int] | None = None,
    backlog_fn: Callable[[Path], int] | None = None,
    now: datetime | None = None,
) -> int:
    """Loop intake windows through the API+batch path until the backlog drains.

    Stops when (a) the raw backlog is empty, (b) the CUMULATIVE dollar ceiling
    (*max_usd*, mapped across the whole drain — not per window) trips, or (c) a
    window makes zero progress (stopped loudly, never spinning). Assumes the
    pre-flight guards (API key, deadline, confirmation) already passed — the CLI
    handler runs those. Forces ``provider=api``, ``batch_mode=True``, and
    ``max_runtime=0`` on every window. Returns an int exit code (0 on a clean
    completion / ceiling stop; nonzero on a zero-progress stop).

    *run_fn* / *backlog_fn* are injectable seams for testing; they default to
    :func:`athenaeum.librarian.run` and a live ``discover_raw_files`` count.
    """
    from athenaeum.librarian import EXIT_GRACEFUL_PARTIAL, discover_raw_files
    from athenaeum.librarian import run as librarian_run

    run_fn = run_fn or librarian_run
    backlog_fn = backlog_fn or (lambda root: len(discover_raw_files(root, config)))
    ledger_path = ledger_path or spend.resolve_ledger_path(config)

    # Issue athenaeum#568 (H1): the cumulative dollar ceiling below is only as trustworthy
    # as the ledger it reads. Verify the ledger is writable BEFORE spending a
    # cent — abort rather than run a blind drain whose per-window ceiling would
    # never sum to a cumulative bound (see :func:`_ledger_writable`).
    if not _ledger_writable(ledger_path):
        return 1

    drain_start = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    # Force the API path: batch mode is Anthropic-endpoint-only (issue athenaeum#330), so
    # a claude-cli-configured repo must be overridden for the drain. Loud, not
    # silent — the operator asked for a batch drain.
    if os.environ.get("ATHENAEUM_LLM_PROVIDER") != "api":
        log.info("athenaeum drain: forcing provider=api (batch mode is API-only).")
        os.environ["ATHENAEUM_LLM_PROVIDER"] = "api"

    window = 0
    while True:
        spent = drain_spend_usd(ledger_path, since=drain_start)
        remaining = max_usd - spent
        if remaining <= 0:
            log.warning(
                "athenaeum drain: cumulative spend ceiling reached "
                "($%.2f/$%.2f across %d window(s)) — stopping.",
                spent,
                max_usd,
                window,
            )
            return 0
        backlog = backlog_fn(raw_root)
        if backlog <= 0:
            log.info(
                "athenaeum drain: raw backlog empty — done "
                "(%d window(s), $%.2f spent).",
                window,
                spent,
            )
            return 0

        # Map the drain's REMAINING budget onto the per-run dollar ceiling for
        # THIS window (env wins over yaml inside run(), issue athenaeum#378). The
        # cumulative guard is the loop re-reading spend from the ledger each pass.
        os.environ["ATHENAEUM_SPEND_MAX_USD_PER_RUN"] = f"{remaining:.6f}"
        log.info(
            "athenaeum drain: window %d — backlog %d, $%.2f of $%.2f spent, "
            "$%.2f remaining this window.",
            window + 1,
            backlog,
            spent,
            max_usd,
            remaining,
        )

        rc = run_fn(
            raw_root=raw_root,
            wiki_root=wiki_root,
            knowledge_root=knowledge_root,
            max_files=max_files,
            max_runtime=0,
            batch_mode=True,
            install_signal_handlers=False,
        )
        window += 1

        new_backlog = backlog_fn(raw_root)
        if new_backlog >= backlog:
            log.error(
                "athenaeum drain: window %d made ZERO progress (backlog %d → "
                "%d) — stopping loudly to avoid a spin. Inspect "
                "wiki/_deferred_work.md and any failed files.",
                window,
                backlog,
                new_backlog,
            )
            return 1
        if rc not in (0, EXIT_GRACEFUL_PARTIAL):
            # A window that still made progress but exited nonzero (e.g. some
            # files failed): log it and keep draining — the zero-progress guard
            # above terminates the loop once only-undrainable files remain.
            # Issue athenaeum#897: this drain loop forces max_runtime=0 and
            # install_signal_handlers=False on every window, so neither
            # EXIT_GRACEFUL_PARTIAL nor EXIT_EXTERNAL_KILL (124) can actually
            # be returned here today — kept for defensive parity with
            # `run()`'s general exit-code contract (docs/exit-codes.md) should
            # a future caller thread those flags through.
            log.warning(
                "athenaeum drain: window %d run exited %d but made progress "
                "(%d → %d); continuing.",
                window,
                rc,
                backlog,
                new_backlog,
            )
