# SPDX-License-Identifier: Apache-2.0
"""Resolutions-as-claims ingestion (issue athenaeum#2017, athenaeum#719 Plan step 5).

Closure for the self-tuning loop: every human resolution — a `coordinate`
answer writing a coordinate, a `dimension-proposal` ratification, or a
contradiction call (`audit`) — is ingested as a **claim** with provenance
(who/when/which decision id) and revocation, so the recurring SHAPE of what
humans keep resolving becomes mineable (:mod:`athenaeum.signal_mining`'s
:func:`~athenaeum.signal_mining.mine_decision_shapes`) exactly like a
recurring underdetermined verdict already is.

**Provenance + revocation, not a second invalidation mechanism.** This
ledger is append-only JSONL, same discipline as every other sidecar ledger
in this codebase (:mod:`athenaeum.retraction_cascade`,
:mod:`athenaeum.decision_budget`): a claim is never edited or deleted in
place. Revoking a claim (:func:`revoke_resolution_claim`) appends a SECOND
record (``kind: "revocation"``) naming the same ``decision_id`` — mirroring
:mod:`athenaeum.retraction_cascade`'s own "the merge is never touched, only
flagged" posture — and then reuses the EXISTING stale-marking primitive
(:func:`athenaeum.verdicts.select_stale_for_coordinate_challenged` /
:func:`athenaeum.verdicts.mark_pairs_stale`) rather than inventing a second
invalidation mechanism, per the issue's own AC text.

**Proposals, the other half of AC5, live one call away, not inside this
function.** :func:`athenaeum.dimension_proposals.mark_proposals_stale_for_decision`
stale-marks every pending dimension proposal whose backfill plan cited the
same *decision_id* — but :mod:`athenaeum.dimension_proposals` imports
:mod:`athenaeum.signal_mining` (for :func:`mine_decision_shapes`'s own
:class:`~athenaeum.signal_mining.MinedShape`/:class:`~athenaeum.signal_mining.
ShapeKey`), and :func:`athenaeum.signal_mining.mine_decision_shapes` reads
THIS module's active-claims view — so
``resolution_claims -> dimension_proposals -> signal_mining -> resolution_claims``
would be a real import cycle (:mod:`tests.test_import_graph_acyclic` forbids
it), not a false positive from a lazy import. :func:`revoke_resolution_claim`
therefore stale-marks only verdicts; a caller that also wants proposals
stale-marked (every real caller does, per AC5) calls
:func:`~athenaeum.dimension_proposals.mark_proposals_stale_for_decision`
itself, right after, with the SAME *decision_id* and a reason — see
``tests/test_resolution_claims.py``'s own revocation tests for the two-call
shape.

Ledger: ``<wiki_root>/_resolution_claims.jsonl``, beside
``_dimension_proposals.jsonl`` / ``_decision_budget_events.jsonl``.

Layering: L4 domain/pipeline module. Imports :mod:`athenaeum.store` (L3) for
the durable-append primitive, and (function-local, to avoid import-time cost
on the common ``ingest_resolution_claim`` path) :mod:`athenaeum.verdicts`
(L2) from :func:`revoke_resolution_claim` only — never
:mod:`athenaeum.dimension_proposals` (see above).
:mod:`athenaeum.decision_answers` (a peer L4 module) calls
:func:`ingest_resolution_claim` from ``apply_decision_answers``; this module
never imports it back. :mod:`athenaeum.signal_mining` (also L4) imports THIS
module (function-locally, from :func:`~athenaeum.signal_mining.
mine_decision_shapes`) to read the active-claims view; this module never
imports ``signal_mining`` back.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from athenaeum.store import append_line_durable, now_iso

if TYPE_CHECKING:  # pragma: no cover - type-checking only
    from athenaeum.runlock import RunLock

log = logging.getLogger(__name__)

#: Schema version stamped on every record.
RESOLUTION_CLAIMS_VERSION = 1

#: Sidecar filename under ``wiki_root``.
RESOLUTION_CLAIMS_FILENAME = "_resolution_claims.jsonl"

#: Record kinds.
CLAIM_KIND = "claim"
REVOCATION_KIND = "revocation"

#: Decision types whose resolutions become claims (issue athenaeum#2017's own
#: enumeration: "a question answer writing a coordinate" -> ``coordinate``,
#: "a dimension-proposal ratification" -> ``dimension-proposal``, "a
#: contradiction call" -> ``audit``, the calibration review of a sampled
#: contradiction). ``merge``/``question``/``proposed-rule`` are deliberately
#: excluded — not named by the issue's enumeration.
RESOLUTION_CLAIM_DECISION_TYPES: frozenset[str] = frozenset(
    ("coordinate", "dimension-proposal", "audit")
)


def default_resolution_claims_path(wiki_root: Path) -> Path:
    """Default ledger path: ``<wiki_root>/_resolution_claims.jsonl``."""
    return Path(wiki_root) / RESOLUTION_CLAIMS_FILENAME


def claim_id(decision_id: str, decision_type: str) -> str:
    """Deterministic id for one ``(decision_id, decision_type)`` resolution.

    Mirrors :func:`athenaeum.retraction_cascade.review_id`'s content-hash-of-
    the-pair idiom — stable across re-ingestion, so a re-run never double
    counts.
    """
    digest = hashlib.sha1(f"{decision_id}\x00{decision_type}".encode("utf-8")).hexdigest()
    return digest[:16]


def _append_jsonl_line(path: Path, line: str) -> None:
    append_line_durable(path, line.encode("utf-8"))


def read_resolution_claims(
    wiki_root: Path, *, claims_path: Path | None = None
) -> list[dict[str, Any]]:
    """Read every well-formed record (claim or revocation), tolerating a torn
    trailing line. ``[]`` when the ledger does not exist."""
    target = claims_path if claims_path is not None else default_resolution_claims_path(wiki_root)
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


def ingest_resolution_claim(
    wiki_root: Path,
    *,
    decision_id: str,
    decision_type: str,
    verdict: str,
    dimension_name: str = "",
    decided_by: str = "human",
    resolved_at: str | None = None,
    note: str = "",
    claims_path: Path | None = None,
) -> dict[str, Any] | None:
    """Ingest one human resolution as a claim (AC1).

    Returns the written record, or ``None`` (writes nothing) when
    *decision_type* is not one of :data:`RESOLUTION_CLAIM_DECISION_TYPES` —
    a resolution this issue's own enumeration does not name. Idempotent:
    re-ingesting the SAME ``(decision_id, decision_type)`` is a no-op (the
    applier that calls this is itself idempotent per-answer-file, so a
    re-applied answer must not double-count its claim).

    Never raises — callers (``apply_decision_answers``) treat this as a
    best-effort side channel, same discipline as
    :func:`athenaeum.decision_budget.record_decision_answered`.
    """
    if decision_type not in RESOLUTION_CLAIM_DECISION_TYPES:
        return None
    try:
        target = (
            claims_path if claims_path is not None else default_resolution_claims_path(wiki_root)
        )
        cid = claim_id(decision_id, decision_type)
        existing = read_resolution_claims(wiki_root, claims_path=claims_path)
        if any(r.get("kind") == CLAIM_KIND and r.get("id") == cid for r in existing):
            return None
        record = {
            "v": RESOLUTION_CLAIMS_VERSION,
            "kind": CLAIM_KIND,
            "id": cid,
            "decision_id": decision_id,
            "decision_type": decision_type,
            "dimension_name": dimension_name,
            "verdict": verdict,
            "decided_by": decided_by,
            "created_at": resolved_at or now_iso(),
            "note": note,
        }
        _append_jsonl_line(target, json.dumps(record, separators=(",", ":")) + "\n")
        return record
    except OSError as exc:  # pragma: no cover - defensive; must never break apply
        log.warning("resolution_claims: failed to ingest claim for %s: %s", decision_id, exc)
        return None


def is_revoked(decision_id: str, records: list[dict[str, Any]]) -> bool:
    """True if any revocation record names *decision_id*."""
    return any(
        r.get("kind") == REVOCATION_KIND and r.get("decision_id") == decision_id for r in records
    )


def list_active_resolution_claims(
    wiki_root: Path, *, claims_path: Path | None = None
) -> list[dict[str, Any]]:
    """Every claim record whose ``decision_id`` has NOT been revoked.

    Claims are never deleted (see module docstring); this is the "live
    view" filter a reader uses instead of hand-filtering revocations.
    """
    records = read_resolution_claims(wiki_root, claims_path=claims_path)
    revoked_ids = {r.get("decision_id") for r in records if r.get("kind") == REVOCATION_KIND}
    return [
        r
        for r in records
        if r.get("kind") == CLAIM_KIND and r.get("decision_id") not in revoked_ids
    ]


def revoke_resolution_claim(
    wiki_root: Path,
    decision_id: str,
    *,
    reason: str,
    lock: "RunLock",
    claims_path: Path | None = None,
) -> dict[str, Any]:
    """Revoke a previously-ingested resolution claim (AC5).

    Mirrors :mod:`athenaeum.retraction_cascade`'s "never touch the thing
    itself, only flag it for review" posture: the original claim record is
    untouched; a ``revocation`` record is appended naming *decision_id* and
    *reason*. Then, reusing the EXISTING stale-marking machinery rather than
    inventing a second one (per the issue's own AC text):

    :func:`athenaeum.verdicts.select_stale_for_coordinate_challenged` +
    :func:`athenaeum.verdicts.mark_pairs_stale` stale-mark every LIVE
    verdict whose ``basis.coord_origins`` cites *decision_id* — the exact
    mechanism :func:`athenaeum.verdicts.challenge_coordinate_answer` (issue
    athenaeum#1994) already uses for a challenged coordinate answer.

    **Proposals are the caller's job, not this function's** — see the
    module docstring's "Proposals, the other half of AC5" note for why
    (an import cycle, not an oversight). Call
    :func:`athenaeum.dimension_proposals.mark_proposals_stale_for_decision`
    with the SAME *decision_id* right after this returns to stale-mark every
    pending proposal that cited it too.

    Never deletes anything — "stale-marked, never deleted" per the issue's
    AC.

    Returns ``{"ok": True, "decision_id": ..., "marked_verdicts": int}``.
    """
    from athenaeum.verdicts import (
        iter_live_entries,
        mark_pairs_stale,
        select_stale_for_coordinate_challenged,
    )

    target = claims_path if claims_path is not None else default_resolution_claims_path(wiki_root)
    record = {
        "v": RESOLUTION_CLAIMS_VERSION,
        "kind": REVOCATION_KIND,
        "decision_id": decision_id,
        "reason": reason,
        "revoked_at": now_iso(),
    }
    _append_jsonl_line(target, json.dumps(record, separators=(",", ":")) + "\n")

    entries = [e for _, e in iter_live_entries(wiki_root)]
    reasons = select_stale_for_coordinate_challenged(entries, decision_id)
    marked_verdicts = mark_pairs_stale(wiki_root, reasons, lock=lock) if reasons else 0

    return {
        "ok": True,
        "decision_id": decision_id,
        "marked_verdicts": marked_verdicts,
    }


__all__ = [
    "RESOLUTION_CLAIMS_VERSION",
    "RESOLUTION_CLAIMS_FILENAME",
    "CLAIM_KIND",
    "REVOCATION_KIND",
    "RESOLUTION_CLAIM_DECISION_TYPES",
    "default_resolution_claims_path",
    "claim_id",
    "read_resolution_claims",
    "ingest_resolution_claim",
    "is_revoked",
    "list_active_resolution_claims",
    "revoke_resolution_claim",
]
