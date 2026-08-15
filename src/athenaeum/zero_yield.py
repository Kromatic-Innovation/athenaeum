# SPDX-License-Identifier: Apache-2.0
"""Cross-run zero-yield state (issue athenaeum#899).

Persists the small run-level record the finalize-phase zero-yield alarm
needs across runs: how many CONSECUTIVE zero-yield runs have occurred, and
which raw-file refs the most recently finalized run deferred (the "no
progress" comparison point the next run's predicate reads).

Mirrors :mod:`athenaeum.detection_state`'s factoring rule exactly: this
module owns the STORE only (load/write the JSON sidecar, fail-open on a
missing or corrupt file). It does not decide when a run is zero-yield —
:mod:`athenaeum.librarian`'s finalize phase evaluates the predicate and
calls :func:`write_state` with the outcome — and it does not format the
``athenaeum status`` output; :mod:`athenaeum.status` calls :func:`load_state`
directly and renders it itself.

**Why the cache dir, not ``wiki_root``:** the athenaeum#663/#898 per-file
ledgers live under ``wiki_root`` because they are written mid-entity-loop,
BEFORE the entity phase's own ``git_snapshot`` commit, so that commit picks
them up for free. This state is different: the predicate can only be
evaluated at FINALIZE, after the entity phase's commit has already happened
(deliberately — the predicate needs the run's WHOLE ``usage.api_calls``,
including the auto-memory C2-C4 spend that accrues AFTER the entity loop,
exactly like the athenaeum#461 run-level spend summary this mirrors). Writing under
``wiki_root`` at that point would leave an uncommitted straggler file in the
knowledge repo's working tree every single run. Mirrors
:mod:`athenaeum.detection_state` and :mod:`athenaeum.spend` instead: both are
cross-run bookkeeping written from a point in the run where a git commit
isn't natural, so both live under the cache dir
(:func:`athenaeum.config.resolve_cache_dir`) rather than the knowledge repo.

**Layering:** L2 leaf. Imports only :mod:`athenaeum.atomic_io` (L0). Consumed
by the L4 :mod:`athenaeum.librarian` finalize phase (write side) and the L4
:mod:`athenaeum.status` module (read side) — routing BOTH through this small
leaf, rather than having ``status.py`` import ``librarian.py`` for the
loader, is what keeps ``status.py`` from reopening the ``{librarian, drain,
status}`` import cycle issue athenaeum#640 dissolved (see
``tests/test_import_graph_acyclic.py``, which walks top-level AND
function-local imports).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from athenaeum.atomic_io import atomic_write_text

log = logging.getLogger(__name__)

#: Sidecar filename, written directly under the resolved cache dir
#: (:func:`athenaeum.config.resolve_cache_dir`) — no leading underscore since,
#: unlike the wiki-root ledgers, this is not competing with a ``*.md`` glob.
STATE_NAME = "zero_yield_state.json"


class ZeroYieldState(TypedDict):
    """Shape returned by :func:`load_state`."""

    #: Number of CONSECUTIVE zero-yield runs up to and including the most
    #: recently finalized run. ``0`` means the most recent run was NOT
    #: zero-yield (or no run has ever finalized).
    consecutive: int
    #: Raw-file refs the most recently finalized run deferred — the set the
    #: NEXT run's "no progress against the previous run" check compares
    #: against.
    deferred_refs: list[str]


def load_state(cache_dir: Path) -> ZeroYieldState:
    """Load the persisted zero-yield state. Missing/corrupt -> fresh (fail-open).

    A run must never fail because its OWN observability state is unreadable
    — a missing or corrupt sidecar reads exactly like "no history yet"
    (``consecutive=0``, no prior deferred refs), mirroring
    :func:`athenaeum.librarian._load_stuck_ledger`'s fail-open contract.
    """
    fresh: ZeroYieldState = {"consecutive": 0, "deferred_refs": []}
    path = cache_dir / STATE_NAME
    if not path.exists():
        return fresh
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fresh
    if not isinstance(data, dict):
        return fresh
    consecutive = data.get("consecutive")
    deferred_refs = data.get("deferred_refs")
    if (
        not isinstance(consecutive, int)
        or isinstance(consecutive, bool)
        or consecutive < 0
    ):
        consecutive = 0
    if not isinstance(deferred_refs, list) or not all(
        isinstance(ref, str) for ref in deferred_refs
    ):
        deferred_refs = []
    return {"consecutive": consecutive, "deferred_refs": deferred_refs}


def write_state(
    cache_dir: Path, *, consecutive: int, deferred_refs: list[str]
) -> None:
    """Persist the zero-yield state.

    Written unconditionally, even ``consecutive=0`` — unlike the athenaeum#663/#898
    per-file ledgers (which shrink to nothing and delete themselves on
    recovery), this is a SINGLE run-level record: a clean (non-zero-yield)
    run still overwrites it with the reset counter and its OWN (possibly
    empty) deferred set, so the NEXT run's "no progress" comparison is always
    against THIS run's outcome, never a stale one.
    """
    path = cache_dir / STATE_NAME
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "updated": now,
        "consecutive": consecutive,
        "deferred_refs": sorted(deferred_refs),
    }
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
