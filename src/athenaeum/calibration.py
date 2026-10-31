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

A third watched population joined this module on the SAME ledger/primitives
(issue athenaeum#2219, design ``docs/design/attribution-verification.md``
§8.3): **above-threshold attribution placements** -- the ``own`` /
``other-known`` / ``alias`` verdicts :mod:`athenaeum.pii_attribution`'s
escalation seam hands to :func:`athenaeum.pii.place_contact_value`. Unlike
T1/T2 (one watched verdict each), :data:`ATTRIBUTION_TIER_NAME` watches all
three of those verdicts at once -- see :data:`_WATCHED_VERDICT`'s shape
below. Its share is config-resolvable via
:func:`athenaeum.config.resolve_audit_sample_rate_attribution`, and its
audit records additionally carry a ``question_class`` (design §6.6), which
:func:`calibration_summary` breaks out per class.

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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from athenaeum.store import append_line_durable, now_iso

log = logging.getLogger(__name__)

#: Schema version stamped on every record so a future reader can migrate.
CALIBRATION_LEDGER_VERSION = 1

#: Sidecar filename, alongside ``_reasoning_tier_decisions.jsonl`` under ``wiki/``.
CALIBRATION_LEDGER_FILENAME = "_calibration.jsonl"

#: Record kinds in the single calibration ledger.
AUDIT_KIND = "audit"
REVIEW_KIND = "review"

#: The watched verdict(s) per tier — the direction(s) that never escalate and
#: so would otherwise go unaudited. A T1 *reject* is the false-reject risk; a
#: T2 *approve* is the false-approve risk; an *attribution* placement watches
#: all three verdicts :func:`athenaeum.pii.place_contact_value` can FILE (or,
#: in observe mode, propose) — ``own``, ``other-known``, ``alias`` — because
#: any of the three can be the model's wrong answer (issue athenaeum#2219,
#: design §8.3). A decision whose ``verdict`` is not in its tier's set here
#: is never an audit candidate.
_WATCHED_VERDICT: dict[str, frozenset[str]] = {
    "T1": frozenset({"reject"}),
    "T2": frozenset({"approve"}),
}

#: The audit-sampled tier for above-threshold ATTRIBUTION placements (issue
#: athenaeum#2219, design §8.3) — see the module docstring. Distinct from
#: :data:`TRIAGE_TIER_NAME` / :data:`DEFAULT_ACCEPTANCE_TIER`: this one DOES
#: use the watched-verdict gate (:data:`_WATCHED_VERDICT`), with three
#: watched verdicts instead of one.
ATTRIBUTION_TIER_NAME = "attribution"

_WATCHED_VERDICT[ATTRIBUTION_TIER_NAME] = frozenset({"own", "other-known", "alias"})

#: Action-id prefix for an attribution-threshold proposal on the SHARED
#: auto-apply-proposals rail (:mod:`athenaeum.auto_apply_proposals`, issue
#: athenaeum#2219, design §8.3: "its own action ids, one per question
#: class"). Lives here, not in ``auto_apply_proposals.py`` (L4), because
#: :mod:`athenaeum.pii_attribution` (L3) needs to name the SAME action id
#: when reading a ledger override back through
#: :func:`athenaeum.config.auto_apply_threshold_ledger_override_for`, and an
#: L3 module importing an L4 one inverts the documented layer direction
#: (``tests/test_layer_boundary.py``). ``calibration.py`` is the L3 module
#: both already import.
ATTRIBUTION_THRESHOLD_ACTION_PREFIX = "attribution-threshold:"


def attribution_threshold_action_id(question_class: str) -> str:
    """The ``auto_apply_proposals`` ``action`` id for one attribution question class.

    Distinct from every pre-existing :func:`athenaeum.resolutions.
    resolve_auto_apply_threshold_for` resolver-action string the SAME ledger
    (``_auto_apply_proposals.jsonl``) already carries, so the two proposal
    populations never collide on the same ``action`` id.
    """
    return f"{ATTRIBUTION_THRESHOLD_ACTION_PREFIX}{question_class}"

#: Issue athenaeum#1996 guard 2: the audit-record ``tier`` value for a
#: second-reviewed human DEFAULT-ACCEPTANCE -- a human accepting a queue
#: item's ``proposed_default`` unmodified (see
#: :func:`athenaeum.decision_framing.is_default_acceptance`). Deliberately
#: NOT ``"T1"``/``"T2"``: this measures a different population (human
#: default-acceptances, not tier verdicts) through the SAME ledger and the
#: SAME :func:`should_sample` / :func:`record_audit_review` primitives --
#: :func:`calibration_summary` already buckets by whatever ``tier`` string
#: appears in the ledger, so this population gets its own summary bucket
#: with no change to that function. The explicit, distinct name also keeps
#: this from ever colliding with issue athenaeum#1995's separate agent-triage-
#: resolution audit, which samples through these same primitives for a
#: DIFFERENT population on a concurrent lane.
DEFAULT_ACCEPTANCE_TIER = "default_acceptance"

#: Issue athenaeum#2079 AC1: ``source:`` frontmatter values that mark a verdict
#: as originating from a HUMAN answer (``athenaeum decisions answer`` and the
#: MCP/triage answer writers all stamp
#: :data:`athenaeum.decision_answers.DECISION_ANSWER_SOURCE_TAG`, which
#: ``tests/test_calibration_default_acceptance_source.py`` pins equal to this).
#: A human cannot overturn their own ruling by reviewing it later, so such an
#: acceptance is never part of the default-acceptance calibration population.
HUMAN_ANSWER_SOURCES = frozenset({"decision_answer"})

#: Issue athenaeum#2079 AC3: ``human_verdict`` stamped on a review record that
#: closes an audit item as not applicable (excluded from every rate).
NOT_APPLICABLE_VERDICT = "not_applicable"

#: ``tier`` value stamped on every agent-triage audit record (issue
#: athenaeum#1995). A distinct string from ``"T1"``/``"T2"`` so
#: :func:`calibration_summary`'s per-tier bucketing separates this
#: measurement from the reasoning-tier one automatically — same ledger, same
#: ``should_sample``/``record_audit_review``/``calibration_summary``
#: primitives, zero shared counters. Deliberately also distinct from
#: whatever tier name a sibling "default-acceptance" sampling lane (issue
#: athenaeum#1996, built concurrently against this same module) picks for
#: itself — "triage" is unambiguous and could not collide with a
#: human-default-acceptance measurement.
TRIAGE_TIER_NAME = "agent-triage"

#: issue athenaeum#1995 AC7: "≥2 confirmed-wrong triage resolutions in a
#: rolling quarter trips a review." A fixed rule, not a config knob — the
#: issue states the number itself, not a tunable rate.
CONFIRMED_WRONG_QUARTER_THRESHOLD = 2

#: "A rolling quarter", for :func:`triage_confirmed_wrong_count`'s window.
#: 92 days (~13 weeks), the common fixed-length approximation of a calendar
#: quarter used where no fiscal calendar is configured.
_QUARTER_WINDOW_DAYS = 92


def default_calibration_ledger_path(wiki_root: Path) -> Path:
    """Default calibration ledger path: ``<wiki_root>/_calibration.jsonl``."""
    return Path(wiki_root) / CALIBRATION_LEDGER_FILENAME


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
    """Append one line to *path* durably (``O_APPEND`` + fsync), via
    :func:`athenaeum.store.append_line_durable` — the single shared
    implementation issue athenaeum#980 (S5) collapsed this module's copy onto
    (design note §2.4 / §6.2)."""
    append_line_durable(path, line.encode("utf-8"))


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
    question_class: str | None = None,
) -> dict[str, Any] | None:
    """Sample one finalized tier decision for human audit, if selected.

    Called right after a T1 reject, a T2 approve, or an above-threshold
    ATTRIBUTION placement (issue athenaeum#2219, design §8.3) is finalized.
    Returns the audit record when the decision is sampled (and appends it to
    the ledger), or ``None`` when the decision is not a watched ``(tier,
    verdict)`` pair or simply isn't selected by the deterministic sampler at
    the configured rate. Idempotent — re-sampling an already-recorded
    decision returns ``None`` rather than duplicating it.

    *reason* must already be safe for the wiki (never the raw contact
    value) — the ATTRIBUTION caller masks it
    (:func:`athenaeum.pii.mask_contact_address`) before calling this, the
    same discipline design §8.1 applies to an escalation's own description.

    ``applied`` (issue athenaeum#602, extended by athenaeum#2219): ``True``
    iff the sampled decision was ALREADY live — a T2 ``approve`` that was
    auto-finalized (see
    :func:`athenaeum.reasoning_screens.t2_screen_merge_proposal`), or an
    attribution placement actually FILED (:data:`athenaeum.pii.
    PLACEMENT_FILED`, i.e. ``apply=True`) rather than merely proposed
    (:data:`athenaeum.pii.PLACEMENT_PROPOSED`, observe mode) — as opposed to
    a T1 reject (never "applied" — a reject never writes anything). Carried
    through unchanged onto a later :func:`record_audit_review` so an
    overturn of this item can be recorded as an overturn of an
    ALREADY-APPLIED write, not merely of a proposal — see that function's
    ``overturned_applied`` field and :func:`calibration_summary`'s
    ``overturned_applied`` count.

    ``question_class`` (issue athenaeum#2219): the attribution question
    class (design §6.6) the placement escalated under. Omitted from the
    stored record when ``None`` (every T1/T2 call site). Carried through
    onto the later review record by :func:`record_audit_review` so
    :func:`calibration_summary` can break the attribution tier out per
    class.
    """
    from athenaeum.config import (
        resolve_audit_sample_rate_attribution,
        resolve_audit_sample_rate_t1_rejects,
        resolve_audit_sample_rate_t2_approvals,
    )

    if verdict not in _WATCHED_VERDICT.get(tier, frozenset()):
        return None
    if tier == "T1":
        rate = resolve_audit_sample_rate_t1_rejects(config)
    elif tier == "T2":
        rate = resolve_audit_sample_rate_t2_approvals(config)
    else:  # tier == ATTRIBUTION_TIER_NAME, the only other _WATCHED_VERDICT key
        rate = resolve_audit_sample_rate_attribution(config)
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
        "created_at": now_iso(),
        "tier": tier,
        "verdict": verdict,
        "proposal_id": proposal_id,
        "reason": reason,
        "sample_rate": rate,
        "applied": applied,
    }
    if question_class is not None:
        record["question_class"] = question_class
    target = (
        ledger_path if ledger_path is not None else default_calibration_ledger_path(wiki_root)
    )
    _append_jsonl_line(target, json.dumps(record, separators=(",", ":")) + "\n")
    return record


def sample_default_acceptance(
    wiki_root: Path,
    *,
    decision_id: str,
    answer_source: str,
    config: dict[str, Any] | None = None,
    ledger_path: Path | None = None,
) -> dict[str, Any] | None:
    """Sample one recorded DEFAULT-ACCEPTANCE for second review (issue
    athenaeum#1996 ratchet guard 2).

    Called right after ``athenaeum decisions answer`` tags an answer as a
    default-acceptance (see
    :func:`athenaeum.decision_framing.is_default_acceptance`) --
    "a human accepted this item's ``proposed_default`` unmodified" is the
    population this measures; calling this function is not itself a verdict
    on whether that acceptance was right, only on whether it gets a second
    look. Deliberately NOT built on :func:`sample_tier_decision`: that
    function's gate is ``(tier, verdict) in _WATCHED_VERDICT``, a T1/T2-only
    predicate this population does not satisfy (and must not be made to, by
    issue athenaeum#1996's own "no second sampling mechanism" instruction cutting
    the other way too -- extending ``_WATCHED_VERDICT`` would blur a tier
    verdict and a human default-acceptance into the same table). Instead
    this writes the SAME :data:`AUDIT_KIND` record shape into the SAME
    ledger, sampled via the SAME :func:`should_sample`, tagged with tier
    :data:`DEFAULT_ACCEPTANCE_TIER` -- reviewed, with zero changes, by the
    EXISTING :func:`record_audit_review` (which is already tier-agnostic:
    it only reads a sampled record's own ``verdict``/``tier``/``applied``
    fields back).

    A sampled record's ``verdict`` is fixed to ``"accept"`` -- that is what
    makes it a default-acceptance in the first place -- so
    :func:`record_audit_review` later classifies a careful reviewer's
    ``human_verdict`` of anything else as an overturn: the human's
    acceptance did not match what the reviewer would have chosen.

    **Human answers are excluded (issue athenaeum#2079 AC1).** *answer_source*
    is the ``source:`` of the answer file the acceptance came from. A verdict
    that originated from a human answer (:data:`HUMAN_ANSWER_SOURCES`) is
    never sampled: the audit would ask the operator to confirm their own
    ruling, the only possible outcome is "confirm", and the measured rate
    would be a constant 100%. An empty/unknown source is treated as human
    (fail closed) -- only an acceptance positively attributed to a model or
    rule producer enters the population.

    Returns the audit record when sampled and newly recorded, ``None`` when
    the source is a human answer, when not selected by :func:`should_sample`
    at the configured rate
    (:func:`athenaeum.config.resolve_default_acceptance_audit_sample_rate`),
    or when this exact *decision_id* was already sampled on a prior run
    (idempotent, same discipline as :func:`sample_tier_decision`).
    """
    from athenaeum.config import resolve_default_acceptance_audit_sample_rate

    source = (answer_source or "").strip()
    if not source or source in HUMAN_ANSWER_SOURCES:
        return None

    rate = resolve_default_acceptance_audit_sample_rate(config)
    if not should_sample(DEFAULT_ACCEPTANCE_TIER, decision_id, rate=rate):
        return None

    item_id = audit_item_id(DEFAULT_ACCEPTANCE_TIER, decision_id)
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
        "created_at": now_iso(),
        "tier": DEFAULT_ACCEPTANCE_TIER,
        "verdict": "accept",
        "proposal_id": decision_id,
        "reason": "default-acceptance sample (issue athenaeum#1996)",
        "sample_rate": rate,
        # Whether the accepted default itself executed a write is a
        # separate, per-decision-type question this measurement does not
        # need: it measures whether the human's answer MATCHES a careful
        # reviewer's, never whether that answer was applied.
        "applied": False,
    }
    target = (
        ledger_path if ledger_path is not None else default_calibration_ledger_path(wiki_root)
    )
    _append_jsonl_line(target, json.dumps(record, separators=(",", ":")) + "\n")
    return record


def default_acceptance_rubber_stamp_rate(
    wiki_root: Path, *, ledger_path: Path | None = None
) -> dict[str, Any]:
    """The measured rubber-stamp rate for human DEFAULT-ACCEPTANCES (issue
    athenaeum#1996 ratchet guard 2): of reviewed samples, the fraction a
    careful reviewer CONFIRMED (matched the human's acceptance) rather than
    overturned.

    Reads :func:`calibration_summary`'s existing, UNCHANGED
    :data:`DEFAULT_ACCEPTANCE_TIER` bucket -- this function adds no new
    counting of its own, only the ratio. This is a MEASUREMENT, never an
    override: nothing here unwinds or re-answers a decision, same posture
    as :func:`record_audit_review`'s own docstring.

    Returns ``{"sampled", "reviewed", "overturned", "rate"}``; ``rate`` is
    ``None`` until at least one sample has been reviewed -- an unreviewed
    sample asserts nothing about accuracy yet, so it must not render as a
    misleading ``0.0`` or ``1.0``.
    """
    bucket = calibration_summary(wiki_root, ledger_path=ledger_path).get(
        DEFAULT_ACCEPTANCE_TIER, _empty_tier_bucket()
    )
    reviewed = bucket["reviewed"]
    rate = ((reviewed - bucket["overturned"]) / reviewed) if reviewed else None
    return {
        "sampled": bucket["sampled"],
        "reviewed": reviewed,
        "overturned": bucket["overturned"],
        "rate": rate,
    }


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
    reviewed_at = now_iso()
    record = {
        "v": CALIBRATION_LEDGER_VERSION,
        "kind": REVIEW_KIND,
        "id": audit_id,
        "created_at": reviewed_at,
        # Issue athenaeum#1990: decision-time metric's resolution-side
        # timestamp. Equal to this record's own ``created_at`` — kept as a
        # separate, explicitly-named key so every budget-instrumentation
        # consumer reads the same ``answered_at`` field name across every
        # decision type instead of a per-type alias.
        "answered_at": reviewed_at,
        "tier": audit.get("tier"),
        "original_verdict": audit.get("verdict"),
        "human_verdict": human_verdict,
        "overturned": overturned,
        "applied": applied,
        "overturned_applied": overturned and applied,
        "note": note,
    }
    if audit.get("question_class") is not None:
        # Issue athenaeum#2219: carried through from the sampled audit record
        # so calibration_summary's per-class attribution breakdown can bucket
        # this review without a second ledger lookup.
        record["question_class"] = audit.get("question_class")
    target = (
        ledger_path if ledger_path is not None else default_calibration_ledger_path(wiki_root)
    )
    _append_jsonl_line(target, json.dumps(record, separators=(",", ":")) + "\n")
    return record


def record_audit_not_applicable(
    wiki_root: Path,
    *,
    audit_id: str,
    note: str,
    ledger_path: Path | None = None,
) -> dict[str, Any]:
    """Close an audit item as NOT APPLICABLE (issue athenaeum#2079 AC3).

    Appends a :data:`REVIEW_KIND` record carrying ``not_applicable: true`` so
    every existing reader (the pending queue, ``preflight_audit``,
    :func:`list_pending_audit`) sees the item as resolved with no change,
    while :func:`calibration_summary` drops it from ``sampled`` and never
    counts it as reviewed or overturned -- it feeds no rate. *note* is the
    ledger rationale and must be non-blank.

    Raises ``ValueError`` for an unknown or already-reviewed id.
    """
    if not note or not note.strip():
        raise ValueError("a not-applicable closure needs a non-blank ledger note")
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
    closed_at = now_iso()
    record = {
        "v": CALIBRATION_LEDGER_VERSION,
        "kind": REVIEW_KIND,
        "id": audit_id,
        "created_at": closed_at,
        "answered_at": closed_at,
        "tier": audit.get("tier"),
        "original_verdict": audit.get("verdict"),
        "human_verdict": NOT_APPLICABLE_VERDICT,
        "not_applicable": True,
        "overturned": False,
        "applied": bool(audit.get("applied")),
        "overturned_applied": False,
        "note": note.strip(),
    }
    if audit.get("question_class") is not None:
        record["question_class"] = audit.get("question_class")
    target = (
        ledger_path if ledger_path is not None else default_calibration_ledger_path(wiki_root)
    )
    _append_jsonl_line(target, json.dumps(record, separators=(",", ":")) + "\n")
    return record


def calibration_summary(
    wiki_root: Path, *, ledger_path: Path | None = None
) -> dict[str, dict[str, Any]]:
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

    **Per-question-class breakdown for the attribution tier** (issue
    athenaeum#2219): :data:`ATTRIBUTION_TIER_NAME`'s bucket carries one
    EXTRA key, ``by_class`` — ``{question_class: {sampled, reviewed,
    overturned, applied, overturned_applied}}``, the SAME five-count shape
    as the tier bucket itself, one per :data:`athenaeum.pii_attribution`
    question class seen in the ledger (a record with no ``question_class``
    buckets under ``"unclassified"``). T1/T2 (and every other tier) keep
    their original five-key shape unchanged — ``by_class`` is added ONLY to
    the attribution bucket, never to theirs, so existing exact-dict-equality
    assertions on a T1/T2 bucket stay valid byte-for-byte.
    """
    # Issue athenaeum#2219: inlined rather than imported from
    # `athenaeum.reasoning_tiers` (`T1_TIER_NAME = "T1"`, `T2_TIER_NAME =
    # "T2"`, unchanged since issue athenaeum#438) -- this module already
    # hardcodes both literals elsewhere (`_WATCHED_VERDICT`), and
    # `pii_attribution.py` now imports THIS module at module scope
    # (the audit sampler / threshold ledger-override action-id helper).
    # `reasoning_tiers -> pending_merges -> answers -> pii_attribution`
    # already existed, so keeping this deferred import would close a NEW
    # cycle back into `calibration` (`tests/test_import_graph_acyclic.py`
    # counts deferred imports as edges too) for two string literals that
    # never change.
    t1_tier_name, t2_tier_name = "T1", "T2"

    records = read_calibration_ledger(wiki_root, ledger_path=ledger_path)
    summary: dict[str, dict[str, Any]] = {
        t1_tier_name: _empty_tier_bucket(),
        t2_tier_name: _empty_tier_bucket(),
    }
    for r in records:
        tier = str(r.get("tier", ""))
        if not tier:
            continue
        is_attribution = tier == ATTRIBUTION_TIER_NAME
        bucket = summary.setdefault(
            tier, _empty_attribution_bucket() if is_attribution else _empty_tier_bucket()
        )
        class_bucket: dict[str, int] | None = None
        if is_attribution:
            qclass = str(r.get("question_class") or "unclassified")
            class_bucket = bucket["by_class"].setdefault(qclass, _empty_tier_bucket())
        applied_flag = bool(r.get("applied"))
        if r.get("kind") == AUDIT_KIND:
            _bump_audit(bucket, applied=applied_flag)
            if class_bucket is not None:
                _bump_audit(class_bucket, applied=applied_flag)
        elif r.get("kind") == REVIEW_KIND and r.get("not_applicable"):
            # Issue athenaeum#2079 AC3: closed as not applicable -- the sample
            # never counts toward ``sampled`` or any rate.
            _bump_not_applicable(bucket, applied=applied_flag)
            if class_bucket is not None:
                _bump_not_applicable(class_bucket, applied=applied_flag)
        elif r.get("kind") == REVIEW_KIND:
            overturned_flag = bool(r.get("overturned"))
            overturned_applied_flag = bool(r.get("overturned_applied"))
            _bump_review(
                bucket, overturned=overturned_flag, overturned_applied=overturned_applied_flag
            )
            if class_bucket is not None:
                _bump_review(
                    class_bucket,
                    overturned=overturned_flag,
                    overturned_applied=overturned_applied_flag,
                )
    return summary


def _empty_tier_bucket() -> dict[str, int]:
    return {
        "sampled": 0,
        "reviewed": 0,
        "overturned": 0,
        "applied": 0,
        "overturned_applied": 0,
    }


def _empty_attribution_bucket() -> dict[str, Any]:
    """:func:`_empty_tier_bucket`'s five counts, PLUS the attribution-only
    ``by_class`` breakdown (issue athenaeum#2219). Never reused for T1/T2 —
    see :func:`calibration_summary`'s docstring."""
    bucket: dict[str, Any] = _empty_tier_bucket()
    bucket["by_class"] = {}
    return bucket


def _bump_audit(bucket: dict[str, Any], *, applied: bool) -> None:
    bucket["sampled"] += 1
    if applied:
        bucket["applied"] += 1


def _bump_not_applicable(bucket: dict[str, Any], *, applied: bool) -> None:
    bucket["sampled"] = max(0, bucket["sampled"] - 1)
    if applied:
        bucket["applied"] = max(0, bucket["applied"] - 1)


def _bump_review(bucket: dict[str, Any], *, overturned: bool, overturned_applied: bool) -> None:
    bucket["reviewed"] += 1
    if overturned:
        bucket["overturned"] += 1
        if overturned_applied:
            bucket["overturned_applied"] += 1


# ---------------------------------------------------------------------------
# Agent-triage sampling (issue athenaeum#1995)
# ---------------------------------------------------------------------------
#
# A SECOND measurement on the SAME primitives above, for a different source:
# :mod:`athenaeum.triage`'s research-resolved answers, rather than the T1/T2
# reasoning-tier verdicts :func:`sample_tier_decision` samples. Deliberately
# does not touch :func:`should_sample`, :func:`record_audit_review`, or
# :func:`calibration_summary` — those three are reused byte-for-byte; only
# :data:`TRIAGE_TIER_NAME` and the sampler/threshold functions below are new.
# This is intentional: issue athenaeum#1996 (a concurrent, sibling lane) adds
# its OWN sampling pass over this same module for a third source (human
# default-acceptances), and the two additions must merge without either one
# touching a line the other also touches.


def sample_triage_decision(
    wiki_root: Path,
    *,
    proposal_id: str,
    verdict: str,
    reason: str,
    config: dict[str, Any] | None = None,
    ledger_path: Path | None = None,
) -> dict[str, Any] | None:
    """Sample one agent-triage resolution for human audit, if selected.

    Mirrors :func:`sample_tier_decision`'s shape on the SAME ledger (an
    ``AUDIT_KIND`` record keyed by ``tier``), but the tier is always
    :data:`TRIAGE_TIER_NAME` here — there is no watched-verdict gate
    (:data:`_WATCHED_VERDICT`) because a triage resolution has only one
    direction to audit: "the agent decided this", not a tier's
    reject-vs-approve split. The sample rate is
    :func:`athenaeum.config.resolve_audit_sample_rate_agent_triage`.

    Called by :mod:`athenaeum.triage` right after one of its research-based
    answers has been submitted through ``athenaeum decisions answer`` and
    applied by the same ``ingest-answers`` tick that applies every other
    decision answer (see that module's ``run_triage``). ``applied`` is
    therefore always recorded ``True`` — unlike a T2 approve, a triage
    answer has no "proposed but not yet live" state by the time this is
    called.

    Idempotent, exactly like :func:`sample_tier_decision`: re-sampling an
    already-recorded ``(tier, proposal_id)`` pair returns ``None`` rather
    than duplicating it. Returns the audit record when sampled and
    appended, else ``None`` (not selected by the deterministic sampler, or
    already sampled on a prior run).
    """
    from athenaeum.config import resolve_audit_sample_rate_agent_triage

    rate = resolve_audit_sample_rate_agent_triage(config)
    if not should_sample(TRIAGE_TIER_NAME, proposal_id, rate=rate):
        return None

    item_id = audit_item_id(TRIAGE_TIER_NAME, proposal_id)
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
        "created_at": now_iso(),
        "tier": TRIAGE_TIER_NAME,
        "verdict": verdict,
        "proposal_id": proposal_id,
        "reason": reason,
        "sample_rate": rate,
        "applied": True,
    }
    target = (
        ledger_path if ledger_path is not None else default_calibration_ledger_path(wiki_root)
    )
    _append_jsonl_line(target, json.dumps(record, separators=(",", ":")) + "\n")
    return record


def _parse_review_day(value: Any) -> date | None:
    """Best-effort ``YYYY-MM-DD`` prefix parse of a ledger timestamp string.

    Mirrors :func:`athenaeum.decision_budget._parse_day`'s exact idiom
    (slice to the first 10 chars, ``date.fromisoformat``, fail-open to
    ``None`` on anything unparseable) rather than inventing a second one —
    this module had no datetime parsing before issue athenaeum#1995.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def triage_confirmed_wrong_count(
    wiki_root: Path,
    *,
    as_of: datetime | None = None,
    ledger_path: Path | None = None,
) -> int:
    """Count agent-triage reviews CONFIRMED WRONG within a trailing quarter.

    Issue athenaeum#1995 AC7's measurement surface: built entirely on
    :func:`read_calibration_ledger` and the ``overturned`` field
    :func:`record_audit_review` already writes — no second ledger, no
    bespoke counter. "Confirmed wrong" is a human's review of a triage
    resolution that OVERTURNED it (``record_audit_review``'s
    ``human_verdict != audit verdict``), restricted to ``tier ==``
    :data:`TRIAGE_TIER_NAME` so a reasoning-tier overturn (T1/T2) or a
    sibling lane's default-acceptance overturn never counts here.

    The window is the trailing :data:`_QUARTER_WINDOW_DAYS` days ending at
    ``as_of`` (default: now), inclusive of both ends, keyed to each REVIEW
    record's own ``created_at`` (when the human recorded the review — not
    when the original resolution was sampled). A review with an unparseable
    timestamp is excluded, not counted as "today" (fail-closed on the
    count, mirroring :func:`athenaeum.decision_budget.items_per_day`'s same
    choice for the same reason: silently inflating a threshold count on bad
    data is the wrong direction to fail).
    """
    ref = as_of or datetime.now(timezone.utc)
    ref_day = ref.date()
    window_start = ref_day - timedelta(days=_QUARTER_WINDOW_DAYS - 1)
    count = 0
    for record in read_calibration_ledger(wiki_root, ledger_path=ledger_path):
        if record.get("kind") != REVIEW_KIND:
            continue
        if record.get("tier") != TRIAGE_TIER_NAME:
            continue
        if not record.get("overturned"):
            continue
        day = _parse_review_day(record.get("created_at"))
        if day is None:
            continue
        if window_start <= day <= ref_day:
            count += 1
    return count


def triage_confirmed_wrong_threshold_breached(
    wiki_root: Path,
    *,
    as_of: datetime | None = None,
    ledger_path: Path | None = None,
) -> bool:
    """Whether the agent-triage confirmed-wrong rate trips a review (issue athenaeum#1995 AC7).

    ``True`` iff :func:`triage_confirmed_wrong_count` is at least
    :data:`CONFIRMED_WRONG_QUARTER_THRESHOLD` (``2``) over the same trailing
    quarter. This function makes no decision and sends no notification —
    exactly like :func:`calibration_summary`, it reports a number for a
    caller (the ``athenaeum triage report`` CLI, an operator briefing) to
    act on. The ratchet guard that would act automatically on a breach is
    issue athenaeum#1996 (slice f), explicitly out of scope here.
    """
    return (
        triage_confirmed_wrong_count(wiki_root, as_of=as_of, ledger_path=ledger_path)
        >= CONFIRMED_WRONG_QUARTER_THRESHOLD
    )


__all__ = [
    "CALIBRATION_LEDGER_VERSION",
    "CALIBRATION_LEDGER_FILENAME",
    "AUDIT_KIND",
    "REVIEW_KIND",
    "ATTRIBUTION_TIER_NAME",
    "ATTRIBUTION_THRESHOLD_ACTION_PREFIX",
    "attribution_threshold_action_id",
    "TRIAGE_TIER_NAME",
    "CONFIRMED_WRONG_QUARTER_THRESHOLD",
    "default_calibration_ledger_path",
    "audit_item_id",
    "sample_probability",
    "should_sample",
    "read_calibration_ledger",
    "sample_tier_decision",
    "sample_triage_decision",
    "triage_confirmed_wrong_count",
    "triage_confirmed_wrong_threshold_breached",
    "list_pending_audit",
    "record_audit_review",
    "calibration_summary",
    "DEFAULT_ACCEPTANCE_TIER",
    "HUMAN_ANSWER_SOURCES",
    "NOT_APPLICABLE_VERDICT",
    "record_audit_not_applicable",
    "sample_default_acceptance",
    "default_acceptance_rubber_stamp_rate",
]
