# SPDX-License-Identifier: Apache-2.0
"""The persistent stuck-raw-file ledger (athenaeum#663), as a low leaf.

Hoisted out of :mod:`athenaeum.librarian` for athenaeum#1597 AC3: ``status.py``
needs to read the SAME ledger the librarian's entity phase writes (to report
"N of the M pending raw files are permanently held, dominant last_error=X" on
``athenaeum status`` between runs), but ``status.py``'s own module docstring
documents why it must never import ``librarian.py`` at all — doing so would
reopen the ``{librarian, drain, status}`` SCC athenaeum#640 dissolved. This
module is the shared leaf both sides import instead, mirroring the exact
pattern :mod:`athenaeum.intake` (athenaeum#545) and :mod:`athenaeum.zero_yield`
(athenaeum#899) already established: hoist the shared bit DOWN to a leaf
neither hub needs to import the other for.

Depends only on :mod:`athenaeum.models` (for :class:`~athenaeum.models.RawFile`
content hashing) plus the standard library — no edge back to ``librarian``,
``status``, or any other hub module. ``librarian.py`` re-exports
:data:`STUCK_MANIFEST_NAME` and the loader under their original names so
existing call sites and tests (``from athenaeum.librarian import
STUCK_MANIFEST_NAME``) keep working unchanged.

Layering: L2 (primitive/utility) — the same tier as :mod:`athenaeum.zero_yield`,
the other small persisted-state sidecar leaf both ``librarian.py`` (writer)
and ``status.py`` (reader) import at top level.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

# Filename of the persistent stuck-file ledger, written beside the wiki root
# (issue athenaeum#663). Kept ``_``-prefixed + ``.json`` so it stays out of
# ``rebuild_index`` (which only globs ``*.md`` and skips ``_``-prefixed names).
# Removed when empty.
STUCK_MANIFEST_NAME = "_stuck_files.json"

# Issue athenaeum#1597 AC1: last_error values naming an exception class that no
# longer exists in the codebase. A ledger entry escalated against
# ``PersonNeverLLMRewriteError`` is a permanent refusal that can never
# recur -- the guard that raised it (``_refuse_person_rewrite`` in
# ``athenaeum.tiers``, issue athenaeum#1183 AC4) was removed by operator
# ruling on athenaeum#1600 ("LLMs ... should be rewriting everything. There
# should be no prohibition there."). Dropped at LOAD time (not merely
# ignored at read time) so every reader -- the librarian's own retry
# decisions AND status.py's dominant-error warning surface, both of which
# go through this one shared leaf -- sees an identical, already-cleaned
# view with no bespoke migration code, and so the file itself is one
# load-drop-rewrite cycle away from having the stale entries gone from
# disk too (the librarian re-persists whatever load_stuck_ledger handed
# it). A file dropped here is simply re-attempted on the next run, exactly
# like a genuinely-new file -- if it fails again, it starts a fresh
# consecutive-failure count under whatever error actually occurs now.
_RETIRED_LAST_ERRORS = frozenset({"PersonNeverLLMRewriteError"})


def stuck_content_hash(raw: Any) -> str:
    """Stable short hash of a raw file's content (athenaeum#663 ledger key).

    Keying the ledger on (ref, content-hash) means a re-edited raw file starts
    a FRESH consecutive count instead of inheriting the old file's stuck
    verdict. Best-effort: any read error hashes the empty string, which simply
    means the entry never matches and the file is treated as workable (fail
    open, never fail-stuck)."""
    try:
        payload = raw.content
    except Exception:  # noqa: BLE001 — a raw we cannot read is never held stuck
        payload = ""
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def load_stuck_ledger(wiki_root: Path) -> dict[str, dict[str, Any]]:
    """Load the persistent stuck-file ledger (athenaeum#663). Missing/corrupt → empty.

    A corrupt ledger must never wedge a caller — a parse error is treated as
    "no stuck files known", so at worst a genuinely-stuck file gets one more
    retry (in the librarian) or is invisible to a status read, never a crash.

    Issue athenaeum#1597 AC1: an entry whose ``last_error`` names a retired
    class (:data:`_RETIRED_LAST_ERRORS`) is dropped here too — see that
    constant's comment for why a load-time drop, not a one-off migration
    script, is the right mechanism.
    """
    path = wiki_root / STUCK_MANIFEST_NAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        return {}
    # Keep only well-shaped entries; drop anything a future/older schema wrote,
    # and drop any entry escalated against a now-retired error class.
    return {
        ref: entry
        for ref, entry in files.items()
        if isinstance(entry, dict)
        and isinstance(entry.get("failures"), int)
        and entry.get("last_error") not in _RETIRED_LAST_ERRORS
    }


def write_stuck_ledger(wiki_root: Path, ledger: dict[str, dict[str, Any]]) -> None:
    """Persist the stuck-file ledger (athenaeum#663), or remove it when empty.

    Written beside the deferred manifest under wiki_root so it rides the run's
    git snapshot (it is durable cross-run state, exactly like the deferred
    manifest). An empty ledger removes the file so a corpus that has recovered
    leaves no stale stuck record behind."""
    from athenaeum.atomic_io import atomic_write_text
    from athenaeum.store import now_iso

    path = wiki_root / STUCK_MANIFEST_NAME
    if not ledger:
        if path.exists():
            path.unlink()
        return
    payload = {"updated": now_iso(), "files": ledger}
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def held_stuck_summary(
    ledger: dict[str, dict[str, Any]],
    raw_files: list[Any],
) -> dict[str, Any]:
    """Among *raw_files* (candidates on disk right now), how many are permanently
    held by the ledger, and by which ``last_error`` (athenaeum#1597 AC2/AC3).

    A raw file counts as "held" when the ledger has a matching, ``escalated``
    entry keyed on the SAME content hash — the identical predicate
    :func:`athenaeum.librarian._hold_out_unworkable_raw` uses to exclude a file
    from the entity phase's intake window, duplicated here (not imported) so
    this stays a leaf ``status.py`` can read without pulling in ``librarian``.
    A file whose content changed since it was marked stuck (hash mismatch) is
    NOT counted as held — it is workable again, exactly like the librarian's
    own hold-out treats it.

    Returns ``{"considered": N, "held": M, "dominant_error": str | None,
    "dominant_error_count": int, "error_counts": {error: count}}``. Empty
    *raw_files* returns ``considered=0, held=0, dominant_error=None``.
    """
    considered = len(raw_files)
    held = 0
    error_counts: dict[str, int] = {}
    for raw in raw_files:
        entry = ledger.get(raw.ref)
        if not isinstance(entry, dict):
            continue
        if not entry.get("escalated"):
            continue
        if entry.get("hash") != stuck_content_hash(raw):
            continue
        held += 1
        error = entry.get("last_error")
        if isinstance(error, str) and error:
            error_counts[error] = error_counts.get(error, 0) + 1
    dominant_error: str | None = None
    dominant_error_count = 0
    if error_counts:
        dominant_error, dominant_error_count = max(
            error_counts.items(), key=lambda kv: (kv[1], kv[0])
        )
    return {
        "considered": considered,
        "held": held,
        "dominant_error": dominant_error,
        "dominant_error_count": dominant_error_count,
        "error_counts": error_counts,
    }
