# SPDX-License-Identifier: Apache-2.0
"""Cross-run git-push failure state (issue athenaeum#1229 part 4).

Persists the small run-level record :func:`athenaeum.librarian.git_push`
needs across separate process invocations: how many CONSECUTIVE pushes have
failed for the SAME reason, and what that reason text was. A push that keeps
failing (e.g. a blob over GitHub's 100 MB limit, `GH001`) was previously
reported as nothing more than a per-attempt ``WARNING`` -- non-fatal by
design (a one-off network blip must not fail the run), but with no cross-run
memory that "non-fatal" degraded into "silently retried forever": one
deployment stranded 1,527 commits locally for four days before anyone
noticed, because every single retry looked, on its own, like an ordinary
transient failure.

Mirrors :mod:`athenaeum.zero_yield`'s factoring rule exactly: this module
owns the STORE only (load/write the JSON sidecar, fail-open on a missing or
corrupt file). It does not decide when a push has failed, or for how many
consecutive attempts, or when that streak is loud enough to escalate --
:func:`athenaeum.librarian.git_push` evaluates all of that and calls
:func:`write_state` with the outcome.

**Why the cache dir, not ``wiki_root``:** :mod:`athenaeum.zero_yield`'s own
docstring explains the split for FINALIZE-phase state (the entity phase's
commit has already happened by the time finalize runs, so a write under
``wiki_root`` would leave an uncommitted straggler every run). The push hook
is stronger evidence for the SAME choice: it runs strictly AFTER every commit
site in a `run()` invocation (issue athenaeum#284's `_maybe_push_after_run` is the
LAST thing `run()` does before returning), so writing this sidecar under
``wiki_root`` would never even get committed until some LATER run happened to
produce a fresh commit -- an indefinite delay exactly as unbounded as the
failure this module exists to surface. The cache dir has no such dependency.

**Layering:** L2 leaf. Imports only :mod:`athenaeum.atomic_io` (L0). Consumed
by the L4 :mod:`athenaeum.librarian` module (:func:`~athenaeum.librarian.git_push`).
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
#: (:func:`athenaeum.config.resolve_cache_dir`) -- mirrors
#: :data:`athenaeum.zero_yield.STATE_NAME`'s no-leading-underscore convention
#: (this is not competing with a ``*.md``/``_*.jsonl`` wiki-root glob).
STATE_NAME = "push_failure_state.json"

#: Cap on the persisted reason string's length -- a git stderr blob can be
#: arbitrarily long (a full refspec-rejection dump); only enough is needed to
#: distinguish "the same failure" from "a different one" on the next run, not
#: the whole diagnostic. Comparisons and storage both use the truncated form
#: so a reason is compared consistently with what was actually persisted.
MAX_REASON_LENGTH = 500


class PushFailureState(TypedDict):
    """Shape returned by :func:`load_state`."""

    #: Number of CONSECUTIVE pushes that failed for the SAME `last_reason`,
    #: up to and including the most recent attempt. ``0`` means the most
    #: recent attempt succeeded (or no attempt has ever been recorded).
    consecutive: int
    #: The most recent failure's reason text (truncated to
    #: :data:`MAX_REASON_LENGTH`), or ``""`` when `consecutive` is 0.
    last_reason: str


def _truncate(reason: str) -> str:
    return reason[:MAX_REASON_LENGTH]


def load_state(cache_dir: Path) -> PushFailureState:
    """Load the persisted push-failure state. Missing/corrupt -> fresh (fail-open).

    A run must never fail because its OWN observability state is unreadable
    -- a missing or corrupt sidecar reads exactly like "no prior failure"
    (``consecutive=0``), mirroring :func:`athenaeum.zero_yield.load_state`'s
    fail-open contract.
    """
    fresh: PushFailureState = {"consecutive": 0, "last_reason": ""}
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
    last_reason = data.get("last_reason")
    if not isinstance(consecutive, int) or isinstance(consecutive, bool) or consecutive < 0:
        consecutive = 0
    if not isinstance(last_reason, str):
        last_reason = ""
    return {"consecutive": consecutive, "last_reason": _truncate(last_reason)}


def write_state(cache_dir: Path, *, consecutive: int, last_reason: str) -> None:
    """Persist the push-failure state.

    Written unconditionally, even ``consecutive=0`` -- a successful push
    still overwrites the sidecar with the reset counter, so the NEXT
    failure's "is this the same reason as last time" comparison is always
    against the run's ACTUAL most recent outcome, never a stale streak from
    before an intervening success.
    """
    path = cache_dir / STATE_NAME
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "updated": now,
        "consecutive": consecutive,
        "last_reason": _truncate(last_reason),
    }
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
