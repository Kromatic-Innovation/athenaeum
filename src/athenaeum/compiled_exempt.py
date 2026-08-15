"""Compiled-exempt manifest — the per-file record of raw intake that must
never be compiled into the wiki (issue athenaeum#903, the `retain` disposition).

A `retain` disposition says: *this file is a long-lived SOURCE DOCUMENT, not
intake to be turned into prose.* A daily journal, an operator's running log.
It is not deleted (that is `drop`), and it is not compiled (that is `emit`) —
it stays exactly where it is, and discovery stops offering it to the reasoning
tiers on every subsequent run.

Decisions
---------

**Why a manifest under the knowledge root, and not the athenaeum#895 ingest
stamp.** The ingest stamp (``ingest-manifest.json``) is the obvious candidate —
it is already "the per-file manifest" and it already keys per raw file. It
lives under the CACHE dir, though, and that is disqualifying for this flag
specifically. The two records fail differently:

- Losing an ingest stamp row costs a re-read and a re-hash. Inconvenient.
- Losing a compiled-exempt row means a retained source document is discovered
  again, handed to the reasoning tiers, and **compiled into the wiki** — the
  exact outcome `retain` exists to prevent, arriving silently one cache wipe
  after the operator asked for the opposite.

"Discovery permanently skips it" (athenaeum#903 AC) cannot rest on a cache. So this
manifest lives in the knowledge git repo, next to the content it describes:
versioned, diffable, and recoverable from history like every other durable
decision athenaeum records.

**Why ``RawFile.ref`` as the key.** ``source/filename`` is stable across
absolute-path churn (a moved knowledge root, a test tmpdir) and is the same
identifier the audit ledger and footnotes already use, so an exempt row reads
against a ledger line without translation.

**Fails open, never fatal.** An absent, unreadable or malformed manifest yields
an EMPTY exempt set — discovery behaves exactly as it did before athenaeum#903. The
failure mode of a corrupt manifest is "a retained file is offered to the tiers
again", which is visible and recoverable; refusing to discover anything at all
would not be.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from athenaeum.atomic_io import atomic_write_text

log = logging.getLogger(__name__)

#: Lives under ``knowledge_root`` (the knowledge git repo), NOT the cache dir —
#: see the module docstring's "Decisions" for why a cache is disqualifying.
COMPILED_EXEMPT_FILENAME = "compiled-exempt.json"

SCHEMA_VERSION = 1


def compiled_exempt_path(knowledge_root: Path) -> Path:
    """The manifest path for *knowledge_root*."""
    return knowledge_root / COMPILED_EXEMPT_FILENAME


def load_exempt(knowledge_root: Path) -> set[str]:
    """Load the set of compiled-exempt ``RawFile.ref`` keys.

    Fails open: absent / unreadable / malformed => ``set()``.
    """
    path = compiled_exempt_path(knowledge_root)
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning(
            "compiled-exempt: %s is unreadable or malformed — treating the "
            "exempt set as empty for this run (issue athenaeum#903)",
            path,
        )
        return set()
    if not isinstance(data, dict):
        return set()
    entries = data.get("exempt")
    if not isinstance(entries, list):
        return set()
    return {str(e) for e in entries if isinstance(e, (str, int))}


def mark_exempt(knowledge_root: Path, refs: list[str] | set[str]) -> set[str]:
    """Merge *refs* into the compiled-exempt manifest and persist it.

    Idempotent: re-marking an already-exempt ref rewrites the same set and is a
    no-op on disk content. Returns the full exempt set after the merge.
    """
    current = load_exempt(knowledge_root)
    merged = current | {str(r) for r in refs}
    if merged == current and compiled_exempt_path(knowledge_root).is_file():
        return merged
    payload: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "exempt": sorted(merged),
    }
    path = compiled_exempt_path(knowledge_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return merged
