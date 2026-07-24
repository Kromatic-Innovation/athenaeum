# SPDX-License-Identifier: Apache-2.0
"""Retraction cascade (issue #435).

Wires issue #425's merge-provenance ledger to issue #427's observation
supersession (correction) records. When a supersession retracts an
observation that a merge's provenance record lists as a *supporting source*,
this module emits a **review item** into the human decisions queue naming the
dependent merge, the retracted observation, and the retraction reason.

The merge itself is never touched — there is deliberately **no auto-unmerge
path** here. A retracted source that fed a completed merge is a judgement
call ("was this merge still right without that source?") that only a human
can make; the cascade's whole job is to make that call *visible*, not to make
it.

Linkage (settled design, fable 2026-07-23 — see the issue): a merge lists its
supporting sources verbatim in
:data:`athenaeum.provenance.build_merge_provenance_record`'s ``source_paths``
(whatever string shape :class:`athenaeum.pending_merges.PendingMerge.sources`
carried). A supersession retracts an observation by its ``obs_id``
(:class:`athenaeum.pii.Supersession.retracts`). "A merge lists the retracted
observation as supporting" is therefore the **string membership** relation
``supersession.retracts in provenance_record["source_paths"]`` — an exact
match between the retracted reference and one of the merge's recorded source
references. See the PR body for the one reversible design default this
records.

Persistence mirrors the other librarian ledgers (JSONL, ``O_APPEND`` +
fsync, tolerant reader that skips a torn trailing line). Review items live in
``<wiki_root>/_pending_retractions.jsonl``, beside ``_merge_provenance.jsonl``
and the other durable ``wiki/`` sidecars, and are surfaced through
:func:`athenaeum.decisions.list_pending_decisions` as ``type: "retraction"``
items.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from athenaeum.pii import read_supersessions
from athenaeum.provenance import read_merge_provenance

log = logging.getLogger(__name__)

#: Schema version stamped on every review record so a future reader can migrate.
RETRACTION_REVIEW_VERSION = 1

#: Sidecar filename, alongside ``_merge_provenance.jsonl`` under ``wiki/``.
RETRACTION_REVIEW_FILENAME = "_pending_retractions.jsonl"


def default_retraction_review_path(wiki_root: Path) -> Path:
    """Default review ledger path: ``<wiki_root>/_pending_retractions.jsonl``."""
    return Path(wiki_root) / RETRACTION_REVIEW_FILENAME


def review_id(merge_id: str, retracted_ref: str) -> str:
    """Deterministic idempotency key for one ``(merge, retracted source)`` pair.

    A given retracted source flags a given dependent merge exactly once, no
    matter how many times the cascade is re-scanned or how many supersession
    records name the same ``obs_id`` — the id is a stable content hash of the
    pair, so a re-scan recognises an already-emitted review and skips it.
    """
    digest = hashlib.sha1(
        f"{merge_id}\x00{retracted_ref}".encode("utf-8")
    ).hexdigest()
    return digest[:16]


def build_retraction_review_record(
    *,
    merge_id: str,
    canonical_slug: str,
    retracted_ref: str,
    reason: str,
    created_at: str,
) -> dict[str, Any]:
    """Build one retraction-review record (the on-disk JSONL shape).

    ``created_at`` is the supersession's own ``at`` timestamp (when the
    retraction was recorded), so a review item ages from the moment the
    source was retracted rather than from whenever the cascade happened to be
    scanned.
    """
    return {
        "v": RETRACTION_REVIEW_VERSION,
        "id": review_id(merge_id, retracted_ref),
        "created_at": created_at,
        "merge_id": merge_id,
        "canonical_slug": canonical_slug,
        "retracted_ref": retracted_ref,
        "reason": reason,
    }


def _append_jsonl_line(path: Path, line: str) -> None:
    """Append one line to *path* durably (``O_APPEND`` + fsync).

    Same discipline as :func:`athenaeum.provenance._append_jsonl_line` /
    :func:`athenaeum.pii._append_jsonl_line`: a single small ``O_APPEND``
    write is atomic on local filesystems, so a crash can at worst leave a
    torn TRAILING line (which the reader skips), never corrupt an
    already-written record. Duplicated (not imported) because the other
    copies are private helpers of their own ledger modules — mirroring the
    pattern is the explicit house style, not reusing a private symbol.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def read_retraction_reviews(
    wiki_root: Path, *, review_path: Path | None = None
) -> list[dict[str, Any]]:
    """Read every well-formed review record, tolerating a torn trailing line.

    Returns ``[]`` when the ledger does not exist. Malformed lines (a crash
    mid-write, or a hand-edit) are skipped, not fatal.
    """
    target = (
        review_path if review_path is not None else default_retraction_review_path(wiki_root)
    )
    if not target.exists():
        return []
    try:
        raw_text = target.read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # torn trailing write or hand-edit; skip
        if isinstance(record, dict):
            records.append(record)
    return records


def scan_retraction_cascade(
    wiki_root: Path,
    contacts_root: Path,
    *,
    provenance_path: Path | None = None,
    supersession_path: Path | None = None,
    review_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Flag dependent merges for review when a supporting source is retracted.

    Walks every supersession (from the contacts/excluded surface) against the
    merge-provenance ledger (under ``wiki_root``). For each merge whose
    ``source_paths`` contains a retracted observation's reference, appends one
    review record to ``<wiki_root>/_pending_retractions.jsonl`` — unless an
    identical review (same merge + same retracted source) was already emitted
    on a prior scan, in which case it is skipped (idempotent).

    The merge is left completely untouched: this function only reads the two
    ledgers and appends review records. Returns the list of **newly emitted**
    review records (empty when a re-scan finds nothing new to flag).
    """
    target = (
        review_path if review_path is not None else default_retraction_review_path(wiki_root)
    )
    supersessions = read_supersessions(contacts_root, log_path=supersession_path)
    if not supersessions:
        return []
    provenance = read_merge_provenance(wiki_root, provenance_path=provenance_path)
    if not provenance:
        return []

    # Idempotency: an id already on disk (from a prior scan) is never re-emitted,
    # and a pair flagged earlier in THIS scan (e.g. two supersessions retracting
    # the same obs_id that a single merge relied on) is emitted only once.
    seen_ids = {
        str(rec.get("id"))
        for rec in read_retraction_reviews(wiki_root, review_path=review_path)
        if isinstance(rec.get("id"), str)
    }

    newly: list[dict[str, Any]] = []
    for sup in supersessions:
        retracted = sup.retracts
        for rec in provenance:
            source_paths = rec.get("source_paths")
            if not isinstance(source_paths, list) or retracted not in source_paths:
                continue
            merge_id = str(rec.get("merge_id", ""))
            record = build_retraction_review_record(
                merge_id=merge_id,
                canonical_slug=str(rec.get("canonical_slug", "")),
                retracted_ref=retracted,
                reason=sup.reason,
                created_at=sup.at,
            )
            if record["id"] in seen_ids:
                continue
            _append_jsonl_line(target, json.dumps(record, separators=(",", ":")) + "\n")
            seen_ids.add(record["id"])
            newly.append(record)
    return newly


__all__ = [
    "RETRACTION_REVIEW_VERSION",
    "RETRACTION_REVIEW_FILENAME",
    "default_retraction_review_path",
    "review_id",
    "build_retraction_review_record",
    "read_retraction_reviews",
    "scan_retraction_cascade",
]
