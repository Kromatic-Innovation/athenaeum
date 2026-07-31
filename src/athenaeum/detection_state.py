# SPDX-License-Identifier: Apache-2.0
"""Per-cluster "detection incomplete" marker (issue #569, audit finding H6).

When a contradiction DETECTOR (:mod:`athenaeum.contradictions`) or RESOLVER
(:mod:`athenaeum.resolutions`) call gives up after its transient-error retries
(:func:`athenaeum._retry.with_retry` raising
:class:`~athenaeum._retry.TransientAPIError`), the cluster degrades fail-open —
``detected=False`` / ``resolver-unavailable`` — and that verdict is written
DURABLY. On its own that is unsafe: live-delta compile is on by default
(``librarian.delta.live_client``) and only re-examines clusters whose member
files changed, so a cluster that hit one 429 is not looked at again until the
periodic full compile (default 7 days). Net effect: up to a week of a known-
contradictory corpus answering recalls confidently.

This module persists a small sidecar keyed by ``cluster_id`` → the cluster's
member file paths at the time of the incomplete examination.
:func:`athenaeum.librarian._run_cluster_pass` unions those paths into the next
run's ``changed_paths`` so the delta closure treats the cluster as dirty and
re-runs detection REGARDLESS of whether any member file changed. The marker is
cleared the instant a cluster is examined to completion (a clean
detected / not-detected verdict from a call that actually succeeded).

The store lives at ``<cache_dir>/detection_incomplete.json`` and is best-effort:
a missing or corrupt file reads as "no markers" (fail-open — a marker store must
never break the run it guards), and writes go through
:func:`athenaeum.atomic_io.atomic_write_text`.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from pathlib import Path

from athenaeum.atomic_io import atomic_write_text

log = logging.getLogger(__name__)

#: Sidecar filename under the cache dir.
_STORE_NAME = "detection_incomplete.json"


def resolve_cache_dir() -> Path:
    """The cache dir the marker store lives under.

    Mirrors :func:`athenaeum.librarian._run_cluster_pass`'s resolution exactly
    (``ATHENAEUM_CACHE_DIR`` env, else ``~/.cache/athenaeum``) so the WRITE side
    (merge, via :func:`mark_incomplete`) and the READ side (the cluster pass,
    via :func:`incomplete_member_paths`) always agree on the same file across
    runs.
    """
    return Path(
        os.environ.get("ATHENAEUM_CACHE_DIR") or (Path.home() / ".cache" / "athenaeum")
    )


def _store_path(cache_dir: Path) -> Path:
    return cache_dir / _STORE_NAME


def load(cache_dir: Path) -> dict[str, list[str]]:
    """Return the ``cluster_id → member absolute paths`` map, or ``{}``.

    Best-effort: a missing, empty, or corrupt store reads as no markers so a
    damaged sidecar can never break a run (it only means a cluster misses one
    forced re-queue, which the periodic full compile still catches).
    """
    path = _store_path(cache_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning(
            "detection-incomplete store unreadable (%s: %s) — treating as empty",
            type(exc).__name__,
            exc,
        )
        return {}
    if not isinstance(raw, dict):
        return {}
    # Coerce defensively — only keep str→list[str] entries.
    out: dict[str, list[str]] = {}
    for cid, members in raw.items():
        if isinstance(cid, str) and isinstance(members, list):
            out[cid] = [str(m) for m in members]
    return out


def _save(cache_dir: Path, data: dict[str, list[str]]) -> None:
    path = _store_path(cache_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(data, separators=(",", ":"), sort_keys=True))
    except OSError as exc:  # never break a run over a marker write
        log.warning(
            "detection-incomplete store write failed (%s: %s) — a cluster may "
            "miss its forced re-queue until the next full compile",
            type(exc).__name__,
            exc,
        )


def mark_incomplete(
    cache_dir: Path, cluster_id: str, member_paths: Iterable[str | Path]
) -> None:
    """Record that *cluster_id*'s examination did not complete this run.

    Stores the cluster's member paths (absolute) so the next cluster pass can
    fold them into ``changed_paths``. Idempotent — re-marking overwrites.
    """
    if not cluster_id:
        return
    data = load(cache_dir)
    data[cluster_id] = sorted({str(p) for p in member_paths})
    _save(cache_dir, data)


def clear_incomplete(cache_dir: Path, cluster_id: str) -> None:
    """Drop *cluster_id*'s marker — its examination completed. No-op if absent."""
    if not cluster_id:
        return
    data = load(cache_dir)
    if cluster_id in data:
        del data[cluster_id]
        _save(cache_dir, data)


def incomplete_member_paths(cache_dir: Path) -> set[Path]:
    """Absolute member paths of every currently-marked cluster that still exist.

    A stored path whose file no longer exists is dropped: the delta closure
    treats a removed member as a member that left its cluster, and re-queuing a
    vanished file buys nothing. Returned paths are ``.resolve()``-d to match the
    absolute form :func:`athenaeum.delta.compute_affected_clusters` keys on.
    """
    paths: set[Path] = set()
    for members in load(cache_dir).values():
        for m in members:
            p = Path(m)
            if p.exists():
                paths.add(p.resolve())
    return paths
