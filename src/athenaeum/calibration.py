# SPDX-License-Identifier: Apache-2.0
"""Tier audit sampler + calibration ledger (issue athenaeum#438).

The **calibration loop** for the tiered reasoning pass (athenaeum#423 T1 reject-and-
route, athenaeum#432 T2 approve/amend/draft/escalate). Escalations already reach the
human queue; what they DON'T catch is a tier that is quietly *wrong* in the
direction that never escalates — a T1 that wrongly rejects a good merge (a
false-reject) or a T2 that wrongly approves a bad one (a false-approve). This
module surfaces a random audit share of exactly those two verdicts for human
calibration review:

- a config-resolvable share of **T1 rejects**
  (:func:`athenaeum.config.resolve_audit_sample_rate_t1_rejects`), and
- a config-resolvable share of **T2 approvals**
  (:func:`athenaeum.config.resolve_audit_sample_rate_t2_approvals`).

Sampled decisions become **audit items** in the human decisions queue,
distinguishable from ordinary escalations by ``type: "audit"`` (see
:func:`athenaeum.decisions.list_pending_decisions`). Reviewing an audit item
feeds calibration; it does **not** re-execute the merge decision — a human
who *overturns* an audit item records the overturn as a calibration signal,
nothing more (no merge is written or unwound here). A *confirm* leaves the
original decision entirely untouched.

Sampling is **deterministic** ("seeded randomness"): whether a given
``(tier, proposal_id)`` is sampled is a stable hash of that pair against the
rate, so re-processing the same decision samples it identically (idempotent)
and a test can assert exactly which proposals a given rate selects without
mocking a global RNG.

Persistence mirrors the other librarian ledgers (JSONL, ``O_APPEND`` +
fsync, tolerant reader). One ledger, ``<wiki_root>/_calibration.jsonl``,
carries two record kinds: ``audit`` (a sampled decision) and ``review`` (a
human's confirm/overturn of an audit item).

**Layering:** L3 service. Module scope imports only stdlib — ``config``
(:func:`athenaeum.config.resolve_audit_sample_rate_t1_rejects` etc.) and
``reasoning_tiers`` (:data:`T1_TIER_NAME` / :data:`T2_TIER_NAME`) are both
deferred inside their one call site so this ledger stays importable without
either. Consumed by the L4 tier pipeline (:mod:`athenaeum.reasoning_tiers`)
right after a T1 reject / T2 approve is finalized — never imports it back at
module scope.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Schema version stamped on every record so a future reader can migrate.
CALIBRATION_LEDGER_VERSION = 1

#: Sidecar filename, alongside ``_reasoning_tier_decisions.jsonl`` under ``wiki/``.
CALIBRATION_LEDGER_FILENAME = "_calibration.jsonl"

#: Record kinds in the single calibration ledger.
AUDIT_KIND = "audit"
REVIEW_KIND = "review"

#: The one watched verdict per tier — the direction that never escalates and
#: so would otherwise go unaudited. A T1 *reject* is the false-reject risk; a
#: T2 *approve* is the false-approve risk. A decision whose ``(tier, verdict)``
#: is not in this map is never an audit candidate.
_WATCHED_VERDICT: dict[str, str] = {"T1": "reject", "T2": "approve"}


def default_calibration_ledger_path(wiki_root: Path) -> Path:
    """Default calibration ledger path: ``<wiki_root>/_calibration.jsonl``."""
    return Path(wiki_root) / CALIBRATION_LEDGER_FILENAME


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def audit_item_id(tier: str, proposal_id: str) -> str:
    """Deterministic idempotency key for one ``(tier, proposal)`` audit item.

    A given tier decision on a given proposal is sampled at most once no
    matter how many times the sampler re-runs over it — the id is a stable
    content hash of the pair, so a re-sample recognises an already-recorded
    audit item and skips it.
    """
    digest = hashlib.sha1(f"{tier}\x00{proposal_id}".encode("utf-8")).hexdigest()
    return digest[:16]


def sample_probability(tier: str, proposal_id: str) -> float:
    """Deterministic sampling coordinate in ``[0.0, 1.0)`` for a decision.

    A stable hash of ``(tier, proposal_id)`` mapped into the unit interval.
    The decision is sampled iff this coordinate is strictly below the
    configured rate — so a rate of ``0.0`` samples nothing and a rate of
    ``1.0`` samples everything, and any given decision's fate is reproducible.
    """
    digest = hashlib.sha256(f"{tier}\x00{proposal_id}".encode("utf-8")).digest()
    # Use the first 8 bytes as a 64-bit unsigned int, scaled into [0, 1).
    value = int.from_bytes(digest[:8], "big")
    return value / float(1 << 64)


def should_sample(tier: str, proposal_id: str, *, rate: float) -> bool:
    """Whether ``(tier, proposal_id)`` is sampled at *rate* (deterministic)."""
    if rate <= 0.0:
        return False
    if rate >= 1.0:
        return True
    return sample_probability(tier, proposal_id) < rate


def _append_jsonl_line(path: Path, line: str) -> None:
    """Append one line to *path* durably (``O_APPEND`` + fsync).

    Same discipline as the provenance / pii / reasoning-tier ledgers: a
    single small ``O_APPEND`` write is atomic on local filesystems, so a
    crash can at worst leave a torn TRAILING line (which the reader skips).
    Duplicated (not imported) per this codebase's per-module-ledger house
    style.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def read_calibration_ledger(
    wiki_root: Path, *, ledger_path: Path | None = None
) -> list[dict[str, Any]]:
    """Read every well-formed ledger record, tolerating a torn trailing line.

    Returns ``[]`` when the ledger does not exist. Malformed lines (a crash
    mid-write, or a hand-edit) are skipped, not fatal.
    """
    target = (
        ledger_path if ledger_path is not None else default_calibration_ledger_path(wiki_root)
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


def sample_tier_decision(
    wiki_root: Path,
    *,
    tier: str,
    verdict: str,
    proposal_id: str,
    reason: str,
    config: dict[str, Any] | None = None,
    ledger_path: Path | None = None,
    applied: bool = False,
) -> dict[str, Any] | None:
    """Sample one finalized tier decision for human audit, if selected.

    Called right after a T1 reject or T2 approve is finalized. Returns the
    audit record when the decision is sampled (and appends it to the ledger),
    or ``None`` when the decision is not a watched ``(tier, verdict)`` pair or
    simply isn't selected by the deterministic sampler at the configured rate.
    Idempotent — re-sampling an already-recorded decision returns ``None``
    rather than duplicating it.

    ``applied`` (issue athenaeum#602): ``True`` iff the sampled decision was a T2
    ``approve`` that was ALREADY auto-finalized (the merge is live in the
    wiki by the time this is called — see
    :func:`athenaeum.merge.t2_screen_merge_proposal`), as opposed to a T1
    reject (never "applied" — a reject never writes anything) or a
    hypothetical future watched verdict that only proposes. Carried through
    unchanged onto a later :func:`record_audit_review` so an overturn of
    this item can be recorded as an overturn of an ALREADY-APPLIED merge,
    not merely of a proposal — see that function's ``overturned_applied``
    field and :func:`calibration_summary`'s ``overturned_applied`` count.
    """
    from athenaeum.config import (
        resolve_audit_sample_rate_t1_rejects,
        resolve_audit_sample_rate_t2_approvals,
    )

    if _WATCHED_VERDICT.get(tier) != verdict:
        return None
    if tier == "T1":
        rate = resolve_audit_sample_rate_t1_rejects(config)
    else:  # tier == "T2"
        rate = resolve_audit_sample_rate_t2_approvals(config)
    if not should_sample(tier, proposal_id, rate=rate):
        return None

    item_id = audit_item_id(tier, proposal_id)
    existing = {
        str(r.get("id"))
        for r in read_calibration_ledger(wiki_root, ledger_path=ledger_path)
        if r.get("kind") == AUDIT_KIND
    }
    if item_id in existing:
        return None  # already sampled on a prior run

    record = {
        "v": CALIBRATION_LEDGER_VERSION,
        "kind": AUDIT_KIND,
        "id": item_id,
        "created_at": _now_iso(),
        "tier": tier,
        "verdict": verdict,
        "proposal_id": proposal_id,
        "reason": reason,
        "sample_rate": rate,
        "applied": applied,
    }
    target = (
        ledger_path if ledger_path is not None else default_calibration_ledger_path(wiki_root)
    )
    _append_jsonl_line(target, json.dumps(record, separators=(",", ":")) + "\n")
    return record


def _reviewed_ids(records: list[dict[str, Any]]) -> set[str]:
    return {
        str(r.get("id")) for r in records if r.get("kind") == REVIEW_KIND
    }


def list_pending_audit(
    wiki_root: Path, *, ledger_path: Path | None = None
) -> list[dict[str, Any]]:
    """Audit items awaiting a human review (sampled but not yet confirmed/overturned)."""
    records = read_calibration_ledger(wiki_root, ledger_path=ledger_path)
    reviewed = _reviewed_ids(records)
    return [
        r
        for r in records
        if r.get("kind") == AUDIT_KIND and str(r.get("id")) not in reviewed
    ]


def record_audit_review(
    wiki_root: Path,
    *,
    audit_id: str,
    human_verdict: str,
    note: str = "",
    ledger_path: Path | None = None,
) -> dict[str, Any]:
    """Record a human's review of an audit item (confirm or overturn).

    Looks up the sampled audit item by ``audit_id``; ``human_verdict`` is
    compared against the tier's original verdict — an audit item is
    *overturned* when they differ, *confirmed* when they match. Recording is
    the whole effect: a confirm leaves the original decision untouched, and an
    overturn is a calibration signal only (no merge is executed or unwound
    here — automated unwinding is explicitly OUT OF SCOPE, issue athenaeum#602).
    Returns the review record.

    ``overturned_applied`` (issue athenaeum#602): ``True`` iff this is an overturn
    (``overturned`` is ``True``) of an audit item that was itself
    ``applied`` (a T2 approve that had ALREADY auto-finalized a live wiki
    write — see :func:`sample_tier_decision`'s ``applied`` param). This is
    the distinct signal the issue calls for: overturning an APPLIED merge is
    a materially bigger deal than overturning a mere proposal (nothing was
    written for the latter), so it is recorded and surfaced separately
    (:func:`calibration_summary`'s ``overturned_applied`` count) rather than
    folded into the plain ``overturned`` count. Always ``False`` when
    ``overturned`` is ``False``, and always ``False`` for a T1 item (a T1
    reject is never ``applied`` — nothing is ever written on a reject).

    Raises ``ValueError`` if ``audit_id`` is unknown or already reviewed —
    each audit item is reviewed at most once.
    """
    records = read_calibration_ledger(wiki_root, ledger_path=ledger_path)
    audit = next(
        (
            r
            for r in records
            if r.get("kind") == AUDIT_KIND and str(r.get("id")) == audit_id
        ),
        None,
    )
    if audit is None:
        raise ValueError(f"unknown audit item id: {audit_id!r}")
    if audit_id in _reviewed_ids(records):
        raise ValueError(f"audit item already reviewed: {audit_id!r}")

    overturned = human_verdict != audit.get("verdict")
    applied = bool(audit.get("applied"))
    record = {
        "v": CALIBRATION_LEDGER_VERSION,
        "kind": REVIEW_KIND,
        "id": audit_id,
        "created_at": _now_iso(),
        "tier": audit.get("tier"),
        "original_verdict": audit.get("verdict"),
        "human_verdict": human_verdict,
        "overturned": overturned,
        "applied": applied,
        "overturned_applied": overturned and applied,
        "note": note,
    }
    target = (
        ledger_path if ledger_path is not None else default_calibration_ledger_path(wiki_root)
    )
    _append_jsonl_line(target, json.dumps(record, separators=(",", ":")) + "\n")
    return record


def calibration_summary(
    wiki_root: Path, *, ledger_path: Path | None = None
) -> dict[str, dict[str, int]]:
    """Per-tier calibration counts:
    ``{tier: {sampled, reviewed, overturned, applied, overturned_applied}}``.

    Always includes the two known tiers (``T1``, ``T2``) with zero counts when
    a tier has no audit history yet, plus any other tier that appears in the
    ledger — so the summary is a stable shape a human (or the ``calibration
    summary`` CLI / MCP tool) can read directly.

    ``applied`` / ``overturned_applied`` (issue athenaeum#602): ``applied`` counts
    sampled items that were ALREADY auto-finalized (a live wiki write) at
    sample time — always 0 for T1 (a reject never writes anything).
    ``overturned_applied`` is the subset of ``overturned`` that were also
    ``applied`` — the SINGLE most important number in this summary once T2
    auto-finalize is enabled: it is a human catching a bad merge that is
    ALREADY LIVE in the wiki (not merely a proposal nobody acted on yet).
    Surfaced as its own top-level count (not folded into ``overturned``) so
    it cannot be silently averaged away in a summary that also has ordinary
    proposal overturns.
    """
    from athenaeum.reasoning_tiers import T1_TIER_NAME, T2_TIER_NAME

    records = read_calibration_ledger(wiki_root, ledger_path=ledger_path)
    summary: dict[str, dict[str, int]] = {
        T1_TIER_NAME: _empty_tier_bucket(),
        T2_TIER_NAME: _empty_tier_bucket(),
    }
    for r in records:
        tier = str(r.get("tier", ""))
        if not tier:
            continue
        bucket = summary.setdefault(tier, _empty_tier_bucket())
        if r.get("kind") == AUDIT_KIND:
            bucket["sampled"] += 1
            if r.get("applied"):
                bucket["applied"] += 1
        elif r.get("kind") == REVIEW_KIND:
            bucket["reviewed"] += 1
            if r.get("overturned"):
                bucket["overturned"] += 1
                if r.get("overturned_applied"):
                    bucket["overturned_applied"] += 1
    return summary


def _empty_tier_bucket() -> dict[str, int]:
    return {
        "sampled": 0,
        "reviewed": 0,
        "overturned": 0,
        "applied": 0,
        "overturned_applied": 0,
    }


__all__ = [
    "CALIBRATION_LEDGER_VERSION",
    "CALIBRATION_LEDGER_FILENAME",
    "AUDIT_KIND",
    "REVIEW_KIND",
    "default_calibration_ledger_path",
    "audit_item_id",
    "sample_probability",
    "should_sample",
    "read_calibration_ledger",
    "sample_tier_decision",
    "list_pending_audit",
    "record_audit_review",
    "calibration_summary",
]
