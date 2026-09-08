# SPDX-License-Identifier: Apache-2.0
"""Cross-run recovery-yield state for natively-written auto-memory (issue athenaeum#1453).

athenaeum#1452 landed origin-session RECOVERY for auto-memory files Claude
Code's native writer leaves with no ``sources[]`` and no ``originSessionId``
(:mod:`athenaeum.session_recovery`). That mechanism is verified — but its
YIELD (what share of the files that needed recovery actually got it) was not
a measurable, thresholded signal: :func:`athenaeum.intake.discover_auto_memory_files`
logged a one-off ``recovered %d of %d`` line and then discarded both numbers.
A reading taken by hand on one day is true for that day only; this module
makes the reading a persisted, thresholded, machine-readable SIGNAL that is
true (or checkably stale) every day.

**This module owns the STORE (and the pure threshold evaluation) only** —
mirrors :mod:`athenaeum.zero_yield`'s factoring rule exactly: ``load_state``/
``write_state`` load and persist the small JSON sidecar (fail-open on a
missing or corrupt file, written unconditionally so a clean pass is never
read as stale). It does not decide WHEN to write — :mod:`athenaeum.intake`'s
``discover_auto_memory_files`` (the pass that already counts ``uncited`` and
``recovered_origins``, per basis) calls :func:`write_state` at its own pass
end, and separately calls :func:`evaluate` to decide whether to log the
threshold-breach WARNING. It does not format the ``athenaeum recovery-yield``
CLI output either; :mod:`athenaeum._cmd_recovery_yield` calls
:func:`load_state` and :func:`evaluate` directly and renders them itself.

**Why the cache dir, not the knowledge repo.** Same reasoning as
:mod:`athenaeum.zero_yield` and :mod:`athenaeum.detection_state`: this is
cross-run bookkeeping written from a point in the intake pass that has
nothing to do with the knowledge repo's own git history, so it lives under
:func:`athenaeum.config.resolve_cache_dir` rather than under ``wiki_root``
(writing under ``wiki_root`` would leave an uncommitted straggler file on
every pass that has anything to recover).

**Why intake, not librarian, hangs this off its pass end.** The signal is
intake-DERIVED — ``uncited``/``recovered``/basis-split are ``discover_auto_memory_files``'s
own local counters — so intake is the honest home for the write, not
``librarian.py``'s phase wiring (see ``src/athenaeum/librarian.py``'s finalize
phase for the *unrelated* zero-yield predicate, which genuinely does need the
run's whole ``usage.api_calls`` and therefore CAN'T be evaluated until
finalize; recovery yield has no such dependency).

**Rate semantics — why a zero denominator is not a zero rate.** ``uncited``
is the count of files that declared no provenance at all going into recovery;
``recovered`` is how many of those recovery actually resolved. When
``uncited == 0`` there was nothing for the mechanism to do, and reporting a
rate of ``0.0`` would fire a false alarm on every corpus that simply had
nothing to recover that pass. :func:`evaluate` therefore returns ``rate=None``
and verdict ``"no-data"`` for a zero denominator — a THIRD state, distinct
from both "above threshold" and "below threshold".

**The threshold.** :data:`DEFAULT_RECOVERY_YIELD_THRESHOLD` is ``0.5`` — a
floor, not a claim about any observed corpus. Below half, the recovery
mechanism is resolving fewer than a coin-flip's worth of the files that
needed it, which is the honest bar for "an operator should look." It is
deliberately a starting band to be TIGHTENED once real readings accumulate,
not a target the mechanism is expected to just barely clear.

Layering: L2 leaf, exactly like :mod:`athenaeum.zero_yield`. Imports only
:mod:`athenaeum.atomic_io` (L0), :mod:`athenaeum.store` (``now_iso``), and
stdlib — no models, no config-loading, no LLM client, nothing from L3/L4.
Must never import :mod:`athenaeum.intake`, :mod:`athenaeum.librarian`, or any
other SCC-adjacent module (see ``tests/test_import_graph_acyclic.py``, which
walks top-level AND function-local imports and fails the build on ANY new
cycle).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import NamedTuple, TypedDict

from athenaeum.atomic_io import atomic_write_text
from athenaeum.store import now_iso

log = logging.getLogger(__name__)

#: Sidecar filename, written directly under the resolved cache dir
#: (:func:`athenaeum.config.resolve_cache_dir`) — mirrors
#: :data:`athenaeum.zero_yield.STATE_NAME`'s naming convention.
STATE_NAME = "recovery_yield_state.json"

#: Minimum acceptable recovered/uncited rate, in ``[0.0, 1.0]``. See the
#: module docstring's "The threshold" section for why ``0.5``.
DEFAULT_RECOVERY_YIELD_THRESHOLD = 0.5

#: Environment override for :func:`resolve_threshold`, checked before the
#: yaml ``librarian.recovery_yield_threshold`` key. Mirrors
#: ``ATHENAEUM_ZERO_YIELD_ALERT_THRESHOLD``'s precedence exactly.
_ENV_VAR = "ATHENAEUM_RECOVERY_YIELD_THRESHOLD"


class RecoveryYieldState(TypedDict):
    """Shape returned by :func:`load_state`."""

    #: Files that declared no provenance (``origin_session_id is None and not
    #: sources``) going into recovery — the denominator.
    uncited: int
    #: Of those, how many recovery actually resolved (either rung) — the
    #: numerator. Always ``<= uncited``.
    recovered: int
    #: Of the recovered ones, how many resolved via the exact
    #: :data:`athenaeum.session_recovery.BASIS_WRITE_CITED` rung.
    write_cited: int
    #: Of the recovered ones, how many resolved via the windowed
    #: :data:`athenaeum.session_recovery.BASIS_TIME_WINDOW` rung.
    time_window: int


def _coerce_nonneg_int(data: dict[str, object], key: str) -> int:
    """Read *key* from *data* as a non-negative ``int``, or ``0`` (fail-open).

    ``bool`` is explicitly rejected even though it is an ``int`` subclass —
    a corrupted/hand-edited sidecar with ``"uncited": true`` must not read as
    ``1``, mirroring :func:`athenaeum.zero_yield.load_state`'s own guard.
    """
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return 0
    return value


def load_state(cache_dir: Path) -> RecoveryYieldState:
    """Load the persisted recovery-yield state. Missing/corrupt -> fresh (fail-open).

    A run must never fail because its OWN observability state is unreadable
    — a missing file, invalid JSON, a non-dict payload, or an individual
    field of the wrong type/sign all read exactly like "no history yet"
    (every counter ``0``), mirroring :func:`athenaeum.zero_yield.load_state`'s
    fail-open contract field-by-field rather than all-or-nothing per file.
    """
    fresh: RecoveryYieldState = {
        "uncited": 0,
        "recovered": 0,
        "write_cited": 0,
        "time_window": 0,
    }
    path = cache_dir / STATE_NAME
    if not path.exists():
        return fresh
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fresh
    if not isinstance(data, dict):
        return fresh
    return {
        "uncited": _coerce_nonneg_int(data, "uncited"),
        "recovered": _coerce_nonneg_int(data, "recovered"),
        "write_cited": _coerce_nonneg_int(data, "write_cited"),
        "time_window": _coerce_nonneg_int(data, "time_window"),
    }


def write_state(
    cache_dir: Path,
    *,
    uncited: int,
    recovered: int,
    write_cited: int,
    time_window: int,
) -> None:
    """Persist the recovery-yield state, unconditionally, including all-zero.

    Written every pass — even ``uncited=0`` — exactly like
    :func:`athenaeum.zero_yield.write_state`: this is a single run-level
    record, so a pass with nothing to recover still overwrites it with its
    own (truthful, zeroed) counters, and the next reader is never looking at
    a stale prior pass's numbers.
    """
    path = cache_dir / STATE_NAME
    payload = {
        "updated": now_iso(),
        "uncited": uncited,
        "recovered": recovered,
        "write_cited": write_cited,
        "time_window": time_window,
    }
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def resolve_threshold(config: dict[str, object] | None = None) -> float:
    """Resolve the minimum acceptable recovered/uncited rate.

    Precedence: ``ATHENAEUM_RECOVERY_YIELD_THRESHOLD`` env > yaml
    ``librarian.recovery_yield_threshold`` > :data:`DEFAULT_RECOVERY_YIELD_THRESHOLD`
    — mirrors :func:`athenaeum.librarian.librarian_zero_yield_alert_threshold`'s
    precedence and validation shape. ``bool`` is explicitly rejected (it is
    an ``int``/``float``-adjacent subclass in yaml's type model — a stray
    ``recovery_yield_threshold: yes`` must not silently become ``1.0``), as
    is any non-numeric or out-of-``[0.0, 1.0]`` value; each falls back to
    the default rather than raising, since a malformed threshold must never
    take a pass down.
    """
    env = os.environ.get(_ENV_VAR)
    if env is not None:
        try:
            value = float(env)
        except (TypeError, ValueError):
            pass
        else:
            if 0.0 <= value <= 1.0:
                return value
    if config is not None:
        cfg = config.get("librarian") if isinstance(config, dict) else None
        if isinstance(cfg, dict):
            raw = cfg.get("recovery_yield_threshold")
            if (
                isinstance(raw, (int, float))
                and not isinstance(raw, bool)
                and 0.0 <= float(raw) <= 1.0
            ):
                return float(raw)
    return DEFAULT_RECOVERY_YIELD_THRESHOLD


class RecoveryYieldEvaluation(NamedTuple):
    """The derived rate and verdict for one :class:`RecoveryYieldState` reading."""

    #: ``recovered / uncited``, or ``None`` when ``uncited == 0`` (no data —
    #: see the module docstring's "Rate semantics" section).
    rate: float | None
    #: ``"no-data"`` (``uncited == 0``), ``"ok"`` (``rate >= threshold``), or
    #: ``"breach"`` (``rate < threshold``). Exactly at the threshold is
    #: ``"ok"`` — inclusive, so a mechanism performing exactly at the floor
    #: is not flagged.
    verdict: str
    #: ``None`` for ``"no-data"``; otherwise ``rate >= threshold``.
    within_threshold: bool | None


def evaluate(state: RecoveryYieldState, threshold: float) -> RecoveryYieldEvaluation:
    """Derive the recovery-yield rate and verdict for *state* against *threshold*.

    Pure function — no I/O, no logging. Callers (intake's pass-end alarm,
    the ``recovery-yield`` CLI readout) both funnel through this so the
    zero-denominator / at-threshold rules are decided in exactly one place.
    """
    uncited = state["uncited"]
    if uncited == 0:
        return RecoveryYieldEvaluation(rate=None, verdict="no-data", within_threshold=None)
    rate = state["recovered"] / uncited
    within = rate >= threshold
    return RecoveryYieldEvaluation(
        rate=rate,
        verdict="ok" if within else "breach",
        within_threshold=within,
    )
