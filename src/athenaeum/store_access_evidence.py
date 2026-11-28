# SPDX-License-Identifier: Apache-2.0
"""What this install's last fetch and last real push to each location did.

Layering: L2 (imports :mod:`athenaeum.atomic_io`, L0, :mod:`athenaeum.config`,
L2, and :mod:`athenaeum.store`, L1, for ``now_iso``).

Design: ``docs/design/federation-store-access.md`` §9 ("The check") and §12
slice S5.

The declaration is a statement, not a grant: the host decides what is possible.
``athenaeum store access check`` therefore compares the declaration with
*evidence* and never probes. This module is where the evidence is kept: one small
JSON file in the cache dir, one entry per git repository, holding the outcome
of the last fetch and of the last real push. The file is machine-local (it
describes this machine's access to a host) and is not part of any store.

**Never raises.** The record calls sit on the sync path; a failure to write
evidence is logged at debug level and changes nothing about the sync.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from athenaeum.atomic_io import atomic_write_text
from athenaeum.clock import now_iso
from athenaeum.config import resolve_cache_dir

log = logging.getLogger(__name__)

__all__ = ["STATE_NAME", "load_evidence", "record_fetch", "record_push", "location_key"]

#: Sidecar filename, directly under the cache dir.
STATE_NAME = "store_access_evidence.json"

MAX_REASON_LENGTH = 500


def location_key(repo: Path) -> str:
    """The key an entry is stored under: the repository's resolved path."""
    return str(Path(repo).resolve())


def load_evidence(cache_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    """``{repo path: {"remote", "fetch", "push"}}``; missing or corrupt reads as empty."""
    path = resolve_cache_dir(cache_dir) / STATE_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict)}


def record_fetch(repo: Path, remote: str, ok: bool, reason: str = "") -> None:
    """Record that the last fetch of *repo* from *remote* completed or failed."""
    _record(repo, remote, "fetch", ok, reason)


def record_push(repo: Path, remote: str, ok: bool, reason: str = "") -> None:
    """Record that the last real push of *repo* to *remote* was accepted or not."""
    _record(repo, remote, "push", ok, reason)


def _record(repo: Path, remote: str, kind: str, ok: bool, reason: str) -> None:
    try:
        cache_dir = resolve_cache_dir(None)
        entries = load_evidence(cache_dir)
        entry = entries.setdefault(location_key(repo), {})
        entry["remote"] = remote
        entry[kind] = {
            "ok": bool(ok),
            "at": now_iso(),
            "reason": (reason or "")[:MAX_REASON_LENGTH],
        }
        cache_dir.mkdir(parents=True, exist_ok=True)
        text = json.dumps(entries, indent=2, sort_keys=True) + "\n"
        atomic_write_text(cache_dir / STATE_NAME, text)
    except Exception as exc:  # noqa: BLE001 - evidence must never affect a sync
        log.debug("store access evidence: not recorded (%s)", type(exc).__name__)
