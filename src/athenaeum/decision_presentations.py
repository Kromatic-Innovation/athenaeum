# SPDX-License-Identifier: Apache-2.0
"""Persisted presentation store for the decision queue (athenaeum#2528).

Design athenaeum#2489 section 2.1: the framing a reader is shown for a queue
item is STORED with the item, so every host sees the same wording and a
fingerprint can name exactly what was shown. This module owns the sidecar --
``_decision_presentations.jsonl`` under the knowledge root's wiki directory, a
runtime corpus file -- and nothing else. It never frames anything; that is
:func:`athenaeum.decision_framing.frame_decision`.

One JSON object per line: ``{"id", "state_digest", "presentation"}``. An
entry is valid only for its exact ``(id, state_digest)``; when a
decision-relevant field changes the digest changes, the entry no longer
matches, and the item is re-framed. At most one entry is kept per id.

Writes are fail-soft and atomic, like the unified queue store this sits
beside: a read-only store degrades persistence, never the read.

Layering: L4 domain module, sibling of :mod:`athenaeum.decision_framing`;
imports only :mod:`athenaeum.atomic_io` (L0) and stdlib.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from athenaeum.atomic_io import atomic_write_text

log = logging.getLogger(__name__)

#: Filename under the knowledge root's wiki directory.
PRESENTATIONS_FILENAME = "_decision_presentations.jsonl"


def load_presentations(wiki_root: Path) -> dict[str, dict[str, Any]]:
    """``{id: {"state_digest", "presentation"}}``; ``{}`` when absent.

    Malformed lines are skipped: a damaged sidecar re-frames, it never breaks
    the read.
    """
    path = wiki_root / PRESENTATIONS_FILENAME
    out: dict[str, dict[str, Any]] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(rec, dict)
            and isinstance(rec.get("id"), str)
            and isinstance(rec.get("state_digest"), str)
            and isinstance(rec.get("presentation"), dict)
        ):
            out[rec["id"]] = {
                "state_digest": rec["state_digest"],
                "presentation": rec["presentation"],
            }
    return out


def stored_presentation(
    stored: dict[str, dict[str, Any]], item_id: Any, state_digest: str
) -> dict[str, Any] | None:
    """The stored presentation for ``item_id`` iff it was framed at ``state_digest``."""
    rec = stored.get(item_id) if isinstance(item_id, str) else None
    if rec is not None and rec["state_digest"] == state_digest:
        return rec["presentation"]
    return None


def save_presentations(
    wiki_root: Path,
    stored: dict[str, dict[str, Any]],
    updates: dict[str, dict[str, Any]],
    *,
    prune: bool,
    live_ids: set[str],
) -> bool:
    """Merge ``updates`` into ``stored`` and rewrite the sidecar if it changed.

    ``prune`` drops entries whose id is not in ``live_ids`` (only safe when the
    caller saw the whole queue). Returns ``True`` when the file is current,
    ``False`` when the write failed (logged, never raised).
    """
    merged = dict(stored)
    merged.update(updates)
    if prune:
        merged = {k: v for k, v in merged.items() if k in live_ids}
    if merged == stored:
        return True
    text = "".join(
        json.dumps(
            {"id": k, "state_digest": v["state_digest"], "presentation": v["presentation"]},
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n"
        for k, v in sorted(merged.items())
    )
    path = wiki_root / PRESENTATIONS_FILENAME
    try:
        atomic_write_text(path, text)
    except OSError as exc:
        log.warning("decisions: failed to persist presentation store %s: %s", path, exc)
        return False
    return True
