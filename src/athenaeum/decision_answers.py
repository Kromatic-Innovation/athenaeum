# SPDX-License-Identifier: Apache-2.0
"""Unified decision resolution as intake (issue athenaeum#908).

``athenaeum.decisions.list_pending_decisions`` already joins five decision
types (``question``, ``merge``, ``retraction``, ``audit``, ``quarantine``)
into one outbound queue. The path back IN was still per-type: three MCP
tools (``resolve_question``, ``resolve_merge``, ``review_audit_item``) each
mutated their own store directly and immediately. This module makes the
inbound path uniform too, extending the existing ``raw/answers/*.md``
raw-intake convention (:mod:`athenaeum.answers`) with the fields needed to
name *which* decision an answer resolves:

- ``decision_id`` — the id being resolved (from ``list_pending_decisions``).
- ``decision_type`` — one of ``question`` | ``merge`` | ``audit`` |
  ``proposed-rule`` | ``coordinate`` | ``dimension-proposal`` (**required**).
  The live id spaces are per-type
  and unrelated (question ids are ``answers._make_id``, sha1 of
  header+question; merge ids are ``pending_merges._make_id``, sha1 of
  sources+target — same length, different key space, no cross-type
  uniqueness check anywhere), so an id alone cannot tell the applier which
  store to look in.
- ``verdict`` — the per-type decision token (question: the answer body;
  merge: ``approve``/``reject``; audit: the human verdict).
- ``note`` — optional free text.

A file with a ``decision_id`` is a **decision answer**: it names an open
decision and is applied at tier 0 (:func:`apply_decision_answers`) — pure
dispatch on ``decision_type``, no LLM call, ever. A file with NO
``decision_id`` is a legacy ``pending_question_answer`` provenance record
(:mod:`athenaeum.answers`'s existing output format) or any other file that
happens to live alongside them — :func:`apply_decision_answers` leaves it
untouched, exactly as it parses today; there was never a consumer for it and
this module does not become one.

Applying is idempotent and fail-soft by construction, not by extra
bookkeeping: each per-type resolver (:func:`athenaeum.answers.resolve_by_id`,
:func:`athenaeum.pending_merges.resolve_merge`,
:func:`athenaeum.calibration.record_audit_review`) already refuses an
unknown or already-resolved id with a structured error rather than
mutating, so re-applying an already-applied (or malformed, or unknown-id)
answer file on a later tick is simply logged and skipped — the file is
never deleted, so it remains its own audit trail. Wired into the ``athenaeum
ingest-answers`` tick (:mod:`athenaeum._cmd_pending`), not a second CLI
command — that command already run-locks the pending-sidecar pass this
belongs in.

The ``proposed-rule`` decision type dispatches to the real store
(:mod:`athenaeum.rule_proposals`, athenaeum#905 —
:func:`~athenaeum.rule_proposals.approve_rule_proposal` /
:func:`~athenaeum.rule_proposals.reject_rule_proposal`), wired in by
athenaeum#921. Its ``verdict`` reuses the SAME ``approve``/``reject``
vocabulary as ``merge`` (see :func:`_apply_merge_answer`); an unknown or
already-resolved proposal id is caught and converted into a fail-soft
skipped outcome, exactly like every other type — never a raised exception
that would halt the batch.

Layering: L4 domain/pipeline module, mirroring :mod:`athenaeum.decisions` —
it aggregates OTHER L4 modules (:mod:`athenaeum.answers`,
:mod:`athenaeum.pending_merges`, :mod:`athenaeum.calibration`) and may
import L3 services (:mod:`athenaeum.models`, :mod:`athenaeum.atomic_io`)
freely. The per-type imports are deferred (function-local) purely to keep
this module cheap to import when only the render path is needed (e.g. from
``mcp_server.py``), not to break an import cycle.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml

from athenaeum.atomic_io import atomic_write_text
from athenaeum.decision_framing import shape_errors_only, validate_answer
from athenaeum.models import parse_frontmatter, render_frontmatter
from athenaeum.store import now_iso

if TYPE_CHECKING:  # pragma: no cover - type-checking only, avoids a hard import
    from athenaeum.runlock import RunLock

log = logging.getLogger(__name__)

#: Registered decision types. ``proposed-rule`` is registered per athenaeum#908's
#: D6 — schema-only, fails closed on apply. ``coordinate`` (issue athenaeum#1993)
#: writes a comparator coordinate and triggers a mechanical re-compare — see
#: :func:`_apply_coordinate_answer`. ``auto-apply-threshold`` (issue athenaeum#2018)
#: is proposal-only — see :func:`_apply_auto_apply_threshold_proposal_answer`.
#: ``dimension-proposal`` (issue athenaeum#2016) ratifies/renames/rejects a
#: drafted dimension proposal (athenaeum#2015) — see
#: :func:`_apply_dimension_proposal_answer`. See module docstring.
DecisionType = Literal[
    "question",
    "merge",
    "audit",
    "proposed-rule",
    "coordinate",
    "auto-apply-threshold",
    "dimension-proposal",
    "page-recompile",
]
VALID_DECISION_TYPES: frozenset[str] = frozenset(
    (
        "question",
        "merge",
        "audit",
        "proposed-rule",
        "coordinate",
        "auto-apply-threshold",
        "dimension-proposal",
        # Issue athenaeum#2547: a drafted page-recompile plan, answered
        # `approve` / `approve-structure-only` / `reject`. See
        # :func:`_apply_page_recompile_answer`.
        "page-recompile",
    )
)

#: Frontmatter ``source:`` tag stamped on every decision-answer file, distinct
#: from :mod:`athenaeum.answers`'s ``pending_question_answer`` legacy tag so
#: the two formats are distinguishable at a glance even before checking for
#: ``decision_id``.
DECISION_ANSWER_SOURCE_TAG = "decision_answer"


class MalformedDecisionAnswer(ValueError):
    """A ``raw/answers/*.md`` file carries ``decision_id`` but fails schema."""


class CoordinateAnswerRefused(ValueError):
    """Raised by :func:`write_decision_answer` for a ``coordinate`` verdict
    that fails :func:`validate_coordinate_answer` (issue athenaeum#2544) --
    the SAME checks :func:`_apply_coordinate_answer` used to be the only
    place that ran, hours later, refusing the whole batch at apply time.
    ``error_code``/``message`` mirror :class:`DecisionAnswerOutcome`'s
    refusal shape so a caller can report it identically.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


@dataclass
class DecisionAnswer:
    """Parsed view of one decision-answer raw-intake file."""

    decision_id: str
    decision_type: str
    verdict: str
    note: str
    resolved_at: str
    path: Path
    #: Issue athenaeum#1996: the ORIGINAL outbound-queue type before
    #: ``answerable_as`` translation (see ``render_decision_answer``'s
    #: docstring). Equal to ``decision_type`` for every file that does not
    #: record a distinct one — including every file written before this
    #: field existed.
    origin_decision_type: str = ""
    #: Issue athenaeum#2016: required when ``verdict == "rename"`` on a
    #: ``dimension-proposal`` answer — the operator-chosen replacement name
    #: for the drafter's proposed dimension. Empty string for every other
    #: answer (the schema's own ``allOf`` conditional forbids it alongside
    #: ``approve``/``reject`` — see ``decision_framing._approve_rename_reject_schema``).
    name: str = ""
    #: Issue athenaeum#2079: the answer file's ``source:`` frontmatter tag
    #: (:data:`DECISION_ANSWER_SOURCE_TAG` for every file this module writes).
    #: Empty for a file with no ``source`` key.
    source: str = ""
    #: Issue athenaeum#2523: the human answerer's identity, when the answer
    #: surface can supply one. The two human-facing surfaces that can
    #: currently reach :func:`_apply_merge_answer` (the MCP ``resolve_merge``
    #: tool and the ``athenaeum decisions answer`` CLI command) both stamp
    #: the placeholder ``"unattributed"`` here today, because no surface yet
    #: carries a real per-user identity (that is issue athenaeum#2489,
    #: still open) — once it lands, a caller that resolves a real actor
    #: flows straight through to ``human:<actor>`` with no further change
    #: needed here. Empty for a file with no ``actor`` key, which is the
    #: honest default for anything written by an automated/pipeline path
    #: (e.g. :mod:`athenaeum.triage`, which never writes a ``merge`` answer
    #: at all — see that module's "Why only question/coordinate" note).
    actor: str = ""


@dataclass
class DecisionAnswerOutcome:
    """Result of applying (or attempting to apply) one decision-answer file."""

    path: Path
    decision_id: str | None
    decision_type: str | None
    applied: bool
    # ``None`` on success. Otherwise the underlying resolver's own error
    # code when available (``id_not_found``, ``already_answered``,
    # ``already_resolved``, ``invalid_decision``, ...), or this module's
    # own ``malformed`` (schema-invalid answer file).
    error_code: str | None
    message: str
    #: Issue athenaeum#2017 (AC1): the dimension this resolution concerned,
    #: when the applier can name one unambiguously (today ``dimension-
    #: proposal``'s own registered/rejected name, or — issue athenaeum#2076 —
    #: a merge's ``fold_narrows_coordinate``/``fold_widen_impossible`` skip
    #: detail). Empty for every other type and for every pre-athenaeum#2017
    #: call site — an absent dimension name is honest, never a guess.
    dimension_name: str = ""
    #: Issue athenaeum#2076 (AC2): the two coordinate values that could not
    #: be widened, for a ``fold_narrows_coordinate``/``fold_widen_impossible``
    #: skip only. Empty for every other outcome.
    value_a: str = ""
    value_b: str = ""
    #: Issue athenaeum#2514 (AC5): for an ``error_code == "superseded"``
    #: outcome only, the FILENAME of the newer answer file that decided
    #: this same ``decision_id`` and was applied in this file's place.
    #: Empty for every other outcome.
    superseded_by: str = ""


@dataclass
class ApplyReport:
    """Outcome of one :func:`apply_decision_answers` pass."""

    applied: int = 0
    skipped: int = 0
    outcomes: list[DecisionAnswerOutcome] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Render + write (used by the three thin MCP mutators)
# ---------------------------------------------------------------------------


def render_decision_answer(
    *,
    decision_id: str,
    decision_type: str,
    verdict: str,
    note: str = "",
    resolved_at: str | None = None,
    origin_decision_type: str = "",
    name: str = "",
    actor: str = "",
) -> str:
    """Render a decision-answer raw-intake record (athenaeum#908 D1 format).

    ``verdict`` and ``note`` are YAML-dumped (not hand-interpolated) so a
    multi-line question answer body round-trips safely through the
    frontmatter block instead of corrupting it.

    ``origin_decision_type`` (issue athenaeum#1996, Seer finding on PR athenaeum#2005):
    the ORIGINAL outbound-queue type (one of the seven :mod:`athenaeum.
    decision_framing` tags, e.g. ``"confirmation"``) before
    :func:`athenaeum.decision_framing.answerable_as` translates it to its
    inbound APPLIER type (``decision_type`` above, one of the four
    :data:`VALID_DECISION_TYPES`). The two differ for exactly one live
    case today (``confirmation`` -> applier ``question``) — recorded
    separately because a ``confirmation`` item's own
    ``proposed_default`` ("accept the narrowed scope") is a REAL discrete
    default a free-text ``question`` answer has none of, and
    :func:`athenaeum.decision_framing.is_default_acceptance` needs the
    ORIGINAL type to tell the two apart. Defaults to *decision_type* when
    omitted (every caller for which the two are not known to diverge, and
    every answer file written before this field existed, round-trips as
    "origin == applier", which is correct for all three of those).

    ``actor`` (issue athenaeum#2523): the human answerer's identity, when
    known. Omitted (empty string) means "no positive human signal" —
    :func:`_apply_merge_answer` reads this back to decide between a
    ``human:<actor>`` and a ``pipeline:merge-<decision>`` ``decided_by``
    stamp. See :attr:`DecisionAnswer.actor`'s docstring for why the two
    current human surfaces both pass the placeholder ``"unattributed"``.
    """
    if decision_type not in VALID_DECISION_TYPES:
        raise ValueError(
            f"decision_type must be one of {sorted(VALID_DECISION_TYPES)}, "
            f"got {decision_type!r}"
        )
    if not decision_id or not decision_id.strip():
        raise ValueError("decision_id must be a non-empty string")
    if not verdict or not verdict.strip():
        raise ValueError("verdict must be a non-empty string")

    resolved_origin_type = (
        origin_decision_type.strip()
        if origin_decision_type and origin_decision_type.strip()
        else decision_type
    )

    meta: dict[str, object] = {
        "source": DECISION_ANSWER_SOURCE_TAG,
        "decision_id": decision_id.strip(),
        "decision_type": decision_type,
        "origin_decision_type": resolved_origin_type,
        "verdict": verdict,
        "resolved_at": resolved_at or now_iso(),
    }
    if note and note.strip():
        meta["note"] = note
    if name and name.strip():
        meta["name"] = name.strip()
    if actor and actor.strip():
        meta["actor"] = actor.strip()

    frontmatter = yaml.safe_dump(
        meta, sort_keys=False, allow_unicode=True, default_flow_style=False
    )
    return (
        "---\n"
        f"{frontmatter}"
        "---\n\n"
        "(decision answer — applied deterministically at tier 0 by "
        "`athenaeum ingest-answers`; see docs/design/conflict-resolution.md)\n"
    )


def write_decision_answer(
    raw_root: Path,
    *,
    decision_id: str,
    decision_type: str,
    verdict: str,
    note: str = "",
    origin_decision_type: str = "",
    name: str = "",
    actor: str = "",
    config: dict | None = None,
    wiki_root: Path | None = None,
) -> Path:
    """Render + write one decision-answer file under ``raw_root/answers/``.

    Filename: ``{ISO-TS}-{decision_type}-{decision_id}.md``, retried with a
    numeric suffix on collision (two answers written in the same second) —
    mirrors :func:`athenaeum.answers.ingest_answers`'s collision handling.
    Returns the path written.

    ``origin_decision_type`` (issue athenaeum#1996): see
    :func:`render_decision_answer`'s docstring — forwarded verbatim,
    defaulting to *decision_type* when omitted.

    ``actor`` (issue athenaeum#2523): forwarded verbatim to
    :func:`render_decision_answer` — see its docstring and
    :attr:`DecisionAnswer.actor`.

    Issue athenaeum#2544: for ``decision_type == "coordinate"``, *verdict*
    is first run through :func:`validate_coordinate_answer` — the exact
    deep check (JSON decoding, schema, pair/page/dimension membership)
    :func:`_apply_coordinate_answer` runs at apply time, hours later — and
    refused BEFORE anything is written on any failure, naming the failing
    pair. This is the ONE function every current coordinate-answer writer
    (``athenaeum decisions answer --type coordinate`` via
    :func:`athenaeum._cmd_decisions._cmd_answer`,
    :func:`athenaeum.triage.submit_answer`) already calls, and any future
    MCP mutator that writes a coordinate answer must call too to get this
    for free — so the "shape-only, fails hours later" gap this closes stays
    closed without relying on every caller re-implementing it.
    ``wiki_root`` is the directory holding ``_pending_questions.md`` /
    ``_pending_merges.md`` — every real caller already has it in scope (the
    standard ``knowledge_root/{wiki,raw}`` layout). Required to run the
    coordinate check below; when omitted, falls back to
    ``raw_root.parent / "wiki"``, which is correct for that standard
    layout but WRONG for any caller (e.g. a test fixture) that puts
    ``raw_root`` somewhere else — such a caller should pass *wiki_root*
    explicitly rather than rely on the guess. *config* is forwarded to
    resolve the dimension registry (``resolve_dimensions(None)`` — kernel
    dimensions only — when omitted, which can falsely refuse a
    deployment-declared dimension, so a caller with config already loaded
    should pass it).

    Raises:
        CoordinateAnswerRefused: *decision_type* is ``"coordinate"`` and
            *verdict* fails validation. Never raised for any other
            *decision_type*, and never after this point (nothing below
            this check can fail the same way).
    """
    if decision_type == "coordinate":
        effective_wiki_root = wiki_root if wiki_root is not None else raw_root.parent / "wiki"
        pending_path = effective_wiki_root / "_pending_questions.md"
        error_code, message, _plan = validate_coordinate_answer(
            effective_wiki_root,
            pending_path,
            decision_id=decision_id,
            verdict=verdict,
            config=config,
        )
        if error_code is not None:
            raise CoordinateAnswerRefused(error_code, message)

    answers_dir = raw_root / "answers"
    answers_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    iso_ts = now_iso(now)
    filename_ts = now.strftime("%Y%m%dT%H%M%SZ")

    text = render_decision_answer(
        decision_id=decision_id,
        decision_type=decision_type,
        verdict=verdict,
        note=note,
        resolved_at=iso_ts,
        origin_decision_type=origin_decision_type,
        name=name,
        actor=actor,
    )

    stem = f"{filename_ts}-{decision_type}-{decision_id}"
    candidate = answers_dir / f"{stem}.md"
    counter = 1
    while candidate.exists():
        candidate = answers_dir / f"{stem}-{counter}.md"
        counter += 1
    atomic_write_text(candidate, text)
    return candidate


# ---------------------------------------------------------------------------
# Pre-flight lookups (read-only) — used by the three thin MCP mutators to
# preserve their existing immediate structured error_code contract without
# performing the (now-deferred) state mutation.
# ---------------------------------------------------------------------------


def preflight_question(pending_path: Path, decision_id: str) -> tuple[bool, str | None, str]:
    """Whether ``decision_id`` names an unanswered pending question.

    Returns ``(ok, error_code, message)``. Never mutates.
    """
    from athenaeum.answers import parse_pending_questions

    if not pending_path.exists():
        msg = f"pending questions file not found: {pending_path}"
        return False, "file_missing", msg
    target = next(
        (pq for pq in parse_pending_questions(pending_path) if pq.id == decision_id),
        None,
    )
    if target is None:
        return False, "id_not_found", f"question id not found: {decision_id}"
    if target.answered:
        return False, "already_answered", f"question {decision_id} already answered"
    return True, None, "ok"


def preflight_merge(
    merges_path: Path, decision_id: str, decision: str
) -> tuple[bool, str | None, str]:
    """Whether ``decision_id`` names an unresolved pending merge.

    Returns ``(ok, error_code, message)``. Never mutates.
    """
    if decision not in ("approve", "reject", "attach-as-subpage"):
        return (
            False,
            "invalid_decision",
            (
                "decision must be 'approve', 'reject' or 'attach-as-subpage', "
                f"got {decision!r}"
            ),
        )

    from athenaeum.pending_merges import parse_pending_merges

    if not merges_path.exists():
        msg = f"pending merges file not found: {merges_path}"
        return False, "file_missing", msg
    target = next(
        (pm for pm in parse_pending_merges(merges_path) if pm.id == decision_id),
        None,
    )
    if target is None:
        return False, "id_not_found", f"merge id not found: {decision_id}"
    if target.resolved:
        return False, "already_resolved", f"merge {decision_id} already resolved"
    return True, None, "ok"


def preflight_audit(wiki_root: Path, decision_id: str) -> tuple[bool, str | None, str]:
    """Whether ``decision_id`` names an unreviewed sampled audit item.

    Returns ``(ok, error_code, message)``. Never mutates.
    """
    from athenaeum.calibration import AUDIT_KIND, REVIEW_KIND, read_calibration_ledger

    records = read_calibration_ledger(wiki_root)
    has_audit = any(
        r.get("kind") == AUDIT_KIND and str(r.get("id")) == decision_id for r in records
    )
    if not has_audit:
        return False, "id_not_found", f"unknown audit item id: {decision_id}"
    already_reviewed = any(
        r.get("kind") == REVIEW_KIND and str(r.get("id")) == decision_id for r in records
    )
    if already_reviewed:
        return False, "already_resolved", f"audit item already reviewed: {decision_id}"
    return True, None, "ok"


# ---------------------------------------------------------------------------
# Unapplied-answer visibility (issue athenaeum#2544)
#
# A file under ``raw/answers/`` is NEVER deleted by `apply_decision_answers`
# — applied or not, it stays as its own audit trail (see that function's
# docstring and `TestCoordinateAnswerApplierRefusals.
# test_already_answered_item_is_refused`, which asserts exactly this).
# Presence on disk therefore cannot tell "unapplied" apart from "applied
# hours ago" — `_decision_is_open` re-uses each decision type's OWN
# preflight/"already answered" check (the same one `write_decision_answer`
# would be refused by) to tell them apart live, rather than inventing a
# second notion of "applied".
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnappliedAnswerRecord:
    """One answer file on disk whose own decision has not (yet) resolved."""

    path: Path
    decision_id: str
    decision_type: str
    resolved_at: str
    verdict: str


def _decision_is_open(wiki_root: Path, decision_type: str, decision_id: str) -> bool:
    """Whether *decision_id* still awaits apply, per ITS OWN source of truth.

    ``question``/``coordinate`` items both live as a
    :class:`athenaeum.answers.PendingQuestion` entry (a coordinate answer
    resolves via the same ``resolve_by_id`` a question answer does — see
    :func:`_apply_coordinate_answer`), so :func:`preflight_question`
    answers both. ``merge`` reads ``_pending_merges.md`` directly rather
    than through :func:`preflight_merge` — that helper also validates an
    ``approve``/``reject`` *decision* argument irrelevant to a pure status
    check. ``audit`` uses :func:`preflight_audit`. Every other type
    (``proposed-rule``, ``auto-apply-threshold``, ``dimension-proposal``)
    has no cheap live lookup here and is conservatively reported OPEN: a
    false positive (surfacing a long-applied file as noise) is safe, a
    false negative (hiding a genuinely contradicting live answer) is not.

    ``question``/``coordinate`` archival (found empirically, not merely
    assumed): :func:`athenaeum.answers.resolve_by_id` only flips the
    block's checkbox in place, so right after
    :func:`apply_decision_answers` alone, :func:`preflight_question` still
    reports ``already_answered`` for it — but the FULL ``ingest-answers``
    tick runs :func:`athenaeum.answers.ingest_answers` immediately after,
    which (when the block's source resolves — the common case for a real
    question; a coordinate item's synthetic ``coordinate-batch:`` source
    never resolves, so it is NEVER archived this way and stays
    ``already_answered`` forever) MOVES the block out of
    ``_pending_questions.md`` into ``_pending_questions_archive.md``
    entirely. From that point on ``preflight_question`` reports
    ``id_not_found`` for a decision that is in fact long since CLOSED, not
    open — treating every ``id_not_found`` as open (as this function used
    to) defeats the whole point of this check: it would warn/refuse on
    settled decisions forever and train the caller to ignore the signal.
    So ``id_not_found`` is resolved one level deeper: a hit in the archive
    means closed; a miss there too is genuinely ambiguous (never existed,
    or a typo) and stays on the SAFE side (reported open) per this
    function's own stated tie-breaking rule above. ``merge``/``audit``
    need no equivalent check: an already-resolved merge block is left
    byte-for-byte in place by :func:`athenaeum.pending_merges.revalidate_pending_merges`
    (only UNRESOLVED stale blocks are ever archived out), and the
    calibration ledger :func:`preflight_audit` reads is append-only, so
    neither ever loses a resolved id out from under this check.
    """
    if decision_type in ("question", "coordinate"):
        pending_path = wiki_root / "_pending_questions.md"
        _ok, error_code, _message = preflight_question(pending_path, decision_id)
        if error_code != "id_not_found":
            return error_code != "already_answered"
        archive_path = wiki_root / "_pending_questions_archive.md"
        if archive_path.exists():
            from athenaeum.answers import parse_pending_questions

            if any(pq.id == decision_id for pq in parse_pending_questions(archive_path)):
                return False  # genuinely archived — closed, not open
        return True  # never seen anywhere — ambiguous, safe side: report it
    if decision_type == "merge":
        from athenaeum.pending_merges import parse_pending_merges

        merges_path = wiki_root / "_pending_merges.md"
        if not merges_path.exists():
            return True
        pm = next((p for p in parse_pending_merges(merges_path) if p.id == decision_id), None)
        if pm is None:
            return True
        return not pm.resolved
    if decision_type == "audit":
        _ok, error_code, _message = preflight_audit(wiki_root, decision_id)
        return error_code != "already_resolved"
    return True


def scan_unapplied_answers(
    raw_root: Path,
    wiki_root: Path,
    *,
    decision_id: str | None = None,
    page_id: str | None = None,
) -> list[UnappliedAnswerRecord]:
    """Every still-OPEN answer file under ``raw_root/answers/`` matching
    *decision_id* or *page_id* (exactly one must be given) — issue
    athenaeum#2544 AC3/AC4: the visibility the write path and a read-only
    CLI both need into answers already waiting on disk for the same item.

    *page_id* matches:

    - a ``coordinate`` answer whose verdict JSON names a ``pair`` (an
      ``<id>+<id>`` key) containing that page id;
    - a ``merge`` answer whose OWN decision id still has a
      ``_pending_merges.md`` entry (:func:`athenaeum.pending_merges.parse_pending_merges`)
      listing a source path whose comparator id
      (:func:`athenaeum.verdicts.page_id_for_path`) matches.

    A malformed answer file (:class:`MalformedDecisionAnswer`) or one with
    no ``decision_id`` is skipped, same posture as
    :func:`apply_decision_answers`. Results are oldest-first (filename-sort
    order, same as that function).
    """
    if (decision_id is None) == (page_id is None):
        raise ValueError("scan_unapplied_answers requires exactly one of decision_id, page_id")

    answers_dir = raw_root / "answers"
    if not answers_dir.exists():
        return []

    merge_sources_by_id: dict[str, list[str]] = {}
    if page_id is not None:
        from athenaeum.pending_merges import parse_pending_merges

        merges_path = wiki_root / "_pending_merges.md"
        if merges_path.exists():
            merge_sources_by_id = {
                pm.id: pm.sources for pm in parse_pending_merges(merges_path)
            }

    def _matches_page(answer: DecisionAnswer) -> bool:
        if answer.decision_type == "coordinate":
            try:
                payload = json.loads(answer.verdict)
            except (ValueError, TypeError):
                return False
            if not isinstance(payload, dict) or not isinstance(payload.get("answers"), list):
                return False
            for item in payload["answers"]:
                if not isinstance(item, dict):
                    continue
                pair_ids = str(item.get("pair", "")).split("+", 1)
                if page_id in pair_ids:
                    return True
            return False
        if answer.decision_type == "merge":
            from athenaeum.verdicts import page_id_for_path

            for src in merge_sources_by_id.get(answer.decision_id, []):
                try:
                    if page_id_for_path(Path(src)) == page_id:
                        return True
                except (OSError, ValueError):
                    continue
            return False
        return False

    out: list[UnappliedAnswerRecord] = []
    for path in sorted(answers_dir.glob("*.md")):
        try:
            answer = _load_decision_answer(path)
        except MalformedDecisionAnswer:
            continue
        if answer is None:
            continue
        if decision_id is not None:
            if answer.decision_id != decision_id:
                continue
        elif not _matches_page(answer):
            continue
        if not _decision_is_open(wiki_root, answer.decision_type, answer.decision_id):
            continue
        out.append(
            UnappliedAnswerRecord(
                path=path,
                decision_id=answer.decision_id,
                decision_type=answer.decision_type,
                resolved_at=answer.resolved_at,
                verdict=answer.verdict,
            )
        )
    return out


def scan_overlapping_merge_proposals(
    raw_root: Path,
    wiki_root: Path,
    *,
    decision_id: str,
    sources: list[str],
) -> list[UnappliedAnswerRecord]:
    """Every OPEN ``merge`` answer file for a DIFFERENT proposal whose own
    sources overlap *sources* (issue athenaeum#2544 AC3) — e.g. two fold
    proposals that both name the same source page, where approving one
    changes what the other would actually be folding.
    """
    answers_dir = raw_root / "answers"
    if not answers_dir.exists():
        return []

    from athenaeum.pending_merges import parse_pending_merges

    merges_path = wiki_root / "_pending_merges.md"
    sources_by_id: dict[str, set[str]] = {}
    if merges_path.exists():
        sources_by_id = {
            pm.id: set(pm.sources) for pm in parse_pending_merges(merges_path)
        }
    target_sources = set(sources)

    out: list[UnappliedAnswerRecord] = []
    for path in sorted(answers_dir.glob("*.md")):
        try:
            answer = _load_decision_answer(path)
        except MalformedDecisionAnswer:
            continue
        if answer is None or answer.decision_type != "merge":
            continue
        if answer.decision_id == decision_id:
            continue
        other_sources = sources_by_id.get(answer.decision_id)
        if not other_sources or not (other_sources & target_sources):
            continue
        if not _decision_is_open(wiki_root, "merge", answer.decision_id):
            continue
        out.append(
            UnappliedAnswerRecord(
                path=path,
                decision_id=answer.decision_id,
                decision_type=answer.decision_type,
                resolved_at=answer.resolved_at,
                verdict=answer.verdict,
            )
        )
    return out


def check_pending_conflicts(
    raw_root: Path,
    wiki_root: Path,
    *,
    decision_id: str,
    decision_type: str,
) -> list[UnappliedAnswerRecord]:
    """Everything the write path should know about BEFORE writing a NEW
    answer for *decision_id* (issue athenaeum#2544 AC3): every unapplied
    answer file already on disk for the same id, plus — for ``merge`` —
    every unapplied answer for another proposal whose sources overlap.
    Never mutates; callers decide warn vs. refuse.
    """
    conflicts = scan_unapplied_answers(raw_root, wiki_root, decision_id=decision_id)
    if decision_type == "merge":
        from athenaeum.pending_merges import parse_pending_merges

        merges_path = wiki_root / "_pending_merges.md"
        if merges_path.exists():
            pm = next(
                (p for p in parse_pending_merges(merges_path) if p.id == decision_id),
                None,
            )
            if pm is not None:
                conflicts = conflicts + scan_overlapping_merge_proposals(
                    raw_root, wiki_root, decision_id=decision_id, sources=pm.sources
                )
    return conflicts


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------


def _load_decision_answer(path: Path) -> DecisionAnswer | None:
    """Parse one ``raw/answers/*.md`` file as a decision-answer record.

    Returns ``None`` for a file with no ``decision_id`` key — a legacy
    ``pending_question_answer`` provenance record, or any other file that
    happens to live in this directory. These are left exactly as they parse
    today (athenaeum#908 back-compat requirement); this function is not their
    consumer.

    Raises:
        MalformedDecisionAnswer: the file DOES carry a ``decision_id`` but
            fails schema validation (missing/empty ``decision_id`` or
            ``verdict``, or an unrecognized ``decision_type``). The caller
            logs and skips rather than letting this propagate past the
            batch loop (athenaeum#908 D5/AC6).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MalformedDecisionAnswer(f"{path}: unreadable ({exc})") from exc

    meta, _body = parse_frontmatter(text)
    if not isinstance(meta, dict) or "decision_id" not in meta:
        return None

    decision_id = meta.get("decision_id")
    if not isinstance(decision_id, str) or not decision_id.strip():
        raise MalformedDecisionAnswer(f"{path}: decision_id missing or empty")

    decision_type = meta.get("decision_type")
    if decision_type not in VALID_DECISION_TYPES:
        raise MalformedDecisionAnswer(
            f"{path}: unrecognized decision_type {decision_type!r}"
        )

    verdict = meta.get("verdict")
    if not isinstance(verdict, str) or not verdict.strip():
        raise MalformedDecisionAnswer(f"{path}: verdict missing or empty")

    note = meta.get("note", "") or ""
    resolved_at = meta.get("resolved_at", "") or ""

    # Issue athenaeum#1996: a legacy file (written before this field existed)
    # or a non-string/empty value falls back to the APPLIER type — correct
    # for every type except confirmation, whose own answer files are only
    # ever written by the one caller (_cmd_decisions._cmd_answer) that has
    # carried this field from day one.
    raw_origin_type = meta.get("origin_decision_type")
    origin_decision_type = (
        raw_origin_type.strip()
        if isinstance(raw_origin_type, str) and raw_origin_type.strip()
        else decision_type
    )

    raw_name = meta.get("name", "") or ""
    raw_actor = meta.get("actor", "") or ""

    return DecisionAnswer(
        decision_id=decision_id.strip(),
        decision_type=decision_type,
        verdict=verdict,
        note=str(note),
        resolved_at=str(resolved_at),
        path=path,
        origin_decision_type=origin_decision_type,
        name=str(raw_name),
        source=str(meta.get("source") or "").strip(),
        actor=str(raw_actor).strip(),
    )


# ---------------------------------------------------------------------------
# Apply (tier 0 — deterministic, no LLM call, ever)
# ---------------------------------------------------------------------------


def _apply_question_answer(pending_path: Path, answer: DecisionAnswer) -> DecisionAnswerOutcome:
    from athenaeum.answers import parse_pending_questions, resolve_by_id
    from athenaeum.decision_framing import match_widen_candidate, widen_candidates

    # Issue athenaeum#2115: a widen question has a closed answer set; free
    # text is refused here rather than recorded, because the fold-apply path
    # will read the recorded answer back as a coordinate value.
    if pending_path.exists():
        for pq in parse_pending_questions(pending_path):
            if pq.id != answer.decision_id or pq.answered:
                continue
            candidates = widen_candidates(pq.description)
            if candidates and match_widen_candidate(answer.verdict, candidates) is None:
                return DecisionAnswerOutcome(
                    path=answer.path,
                    decision_id=answer.decision_id,
                    decision_type=answer.decision_type,
                    applied=False,
                    error_code="invalid_decision",
                    message=(
                        f"answer {answer.verdict!r} is not one of the offered "
                        f"values {candidates!r}"
                    ),
                )
            break

    result = resolve_by_id(pending_path, answer.decision_id, answer.verdict)
    if result["ok"]:
        return DecisionAnswerOutcome(
            path=answer.path,
            decision_id=answer.decision_id,
            decision_type=answer.decision_type,
            applied=True,
            error_code=None,
            message=(
                "question marked answered; write-back to source + archival "
                "completes on this same ingest-answers pass"
            ),
        )
    return DecisionAnswerOutcome(
        path=answer.path,
        decision_id=answer.decision_id,
        decision_type=answer.decision_type,
        applied=False,
        error_code=result.get("error_code"),
        message=result.get("message", ""),
    )


def _apply_merge_answer(
    merges_path: Path,
    wiki_root: Path,
    answer: DecisionAnswer,
    *,
    config: dict | None,
    lock: "RunLock | None" = None,
) -> DecisionAnswerOutcome:
    from athenaeum.config import (
        resolve_cache_dir,
        resolve_search_backend,
        resolve_verdict_ledger_enabled,
    )
    from athenaeum.pending_merges import parse_pending_merges, resolve_merge

    decision = answer.verdict.strip().lower()
    if decision not in ("approve", "reject", "attach-as-subpage"):
        return DecisionAnswerOutcome(
            path=answer.path,
            decision_id=answer.decision_id,
            decision_type=answer.decision_type,
            applied=False,
            error_code="invalid_decision",
            message=(
                "verdict must be 'approve', 'reject' or 'attach-as-subpage', "
                f"got {answer.verdict!r}"
            ),
        )

    cfg = config or {}
    # Issue athenaeum#2458: `cfg=None` (the common case on this path) used to
    # pin `fts5` here while every `load_config`-backed surface answered on
    # the shipped default — resolve it through `resolve_search_backend`.
    search_backend = resolve_search_backend(cfg if isinstance(cfg, dict) else None)
    cache_dir = resolve_cache_dir(None)

    # Issue athenaeum#712: when the verdict ledger is enabled AND a lock is
    # available (the CLI's `_acquire_or_exit` run lock — see
    # `athenaeum.verdicts`'s single-appender contract), capture the
    # proposal's sources BEFORE resolving. `resolve_merge()` does not return
    # sources, and by the time it returns the block's checkbox has already
    # flipped, so this is the last point they are cheaply available.
    verdict_ledger_enabled = lock is not None and resolve_verdict_ledger_enabled(config)
    sources: list[str] = []
    if verdict_ledger_enabled:
        for pm in parse_pending_merges(merges_path):
            if pm.id == answer.decision_id and not pm.resolved:
                sources = list(pm.sources)
                break

    result = resolve_merge(
        merges_path,
        merge_id=answer.decision_id,
        decision=decision,  # type: ignore[arg-type]
        note=answer.note,
        wiki_root=wiki_root,
        cache_dir=cache_dir,
        search_backend=search_backend,
    )

    # Issue athenaeum#712: record this decision — approve/reject on a proposed
    # pair — as a verdict-ledger entry. This is the "consumed within this
    # same issue by writing verdicts for the decisions the current pipeline
    # already makes" Wiring option; see athenaeum.verdicts.record_pair_decision's
    # docstring. Never lets a ledger write affect the outcome above —
    # record_pair_decision is itself best-effort/non-raising, so this is
    # purely an additional side effect on an already-successful resolve.
    if verdict_ledger_enabled and lock is not None and result.get("ok") and len(sources) >= 2:
        from athenaeum.verdicts import record_pair_decision

        verdict_value = "duplicate" if decision == "approve" else "distinct"
        # Issue athenaeum#2523: a merge resolved from a human answer must be
        # distinguishable from an automated one in the ledger. `answer.actor`
        # is the positive signal — set only by the two human-facing answer
        # surfaces (MCP's `resolve_merge` tool, the `athenaeum decisions
        # answer` CLI command), both of which stamp the placeholder
        # "unattributed" today because no surface yet carries a real
        # per-user identity (that is issue athenaeum#2489, still open).
        # Once it lands, a caller that resolves a real actor flows straight
        # through to `human:<actor>` with no further change needed here.
        # A DecisionAnswer with no actor (e.g. a hypothetical future
        # automated writer that reuses this same raw-intake path) keeps the
        # pre-existing `pipeline:merge-<decision>` stamp unchanged — the
        # human label requires that positive signal, never a default.
        decided_by = f"human:{answer.actor}" if answer.actor else f"pipeline:merge-{decision}"
        record_pair_decision(
            wiki_root,
            source_a=sources[0],
            source_b=sources[1],
            verdict=verdict_value,
            decided_by=decided_by,
            lock=lock,
            # Issue athenaeum#984 (AC3): let record_pair_decision route an
            # erasure-class or cross-boundary pair to the off-corpus ledger
            # shard instead of refusing it. A no-op when off_corpus.enabled
            # is unset (the default) — record_pair_decision falls back to
            # its pre-athenaeum#984 refuse-and-drop behavior.
            config=config,
            knowledge_root=wiki_root.parent,
        )

    return DecisionAnswerOutcome(
        path=answer.path,
        decision_id=answer.decision_id,
        decision_type=answer.decision_type,
        applied=bool(result.get("ok")),
        error_code=result.get("error_code"),
        message=result.get("message", ""),
        # Issue athenaeum#2076 (AC2): ``dimension``/``value_a``/``value_b`` are
        # only ever present on a ``fold_narrows_coordinate`` /
        # ``fold_widen_impossible`` refusal — ``None`` on every other
        # outcome, which `str(None or "")` below honestly renders as empty
        # rather than the literal text "None".
        dimension_name=str(result.get("dimension") or ""),
        value_a=str(result.get("value_a")) if result.get("value_a") is not None else "",
        value_b=str(result.get("value_b")) if result.get("value_b") is not None else "",
    )


def _apply_audit_answer(wiki_root: Path, answer: DecisionAnswer) -> DecisionAnswerOutcome:
    from athenaeum.calibration import record_audit_review

    try:
        record_audit_review(
            wiki_root,
            audit_id=answer.decision_id,
            human_verdict=answer.verdict,
            note=answer.note,
        )
    except ValueError as exc:
        msg = str(exc)
        error_code = "already_resolved" if "already reviewed" in msg else "id_not_found"
        return DecisionAnswerOutcome(
            path=answer.path,
            decision_id=answer.decision_id,
            decision_type=answer.decision_type,
            applied=False,
            error_code=error_code,
            message=msg,
        )
    return DecisionAnswerOutcome(
        path=answer.path,
        decision_id=answer.decision_id,
        decision_type=answer.decision_type,
        applied=True,
        error_code=None,
        message="audit item review recorded",
    )


def _apply_proposed_rule_answer(wiki_root: Path, answer: DecisionAnswer) -> DecisionAnswerOutcome:
    """Apply one ``proposed-rule`` decision answer against the real store
    (:mod:`athenaeum.rule_proposals`, athenaeum#905) — tier 0, deterministic,
    no LLM call: :func:`~athenaeum.rule_proposals.approve_rule_proposal`
    writes an already-drafted, already-stored rule YAML to disk; it makes no
    model call itself, so this dispatch branch stays as LLM-free as every
    other one in this module.

    ``knowledge_root`` is DERIVED as ``wiki_root.parent`` rather than
    threaded through as a new required parameter on
    :func:`apply_decision_answers` (which would force every existing caller
    to update). This mirrors an existing convention in this codebase —
    ``_cmd_pending.cmd_ingest_answers`` sets ``wiki_root = target / "wiki"``
    where ``target`` IS the knowledge root, and ``librarian.py`` derives
    ``contacts_surface_root(wiki_root.parent, config)`` the same way — so
    ``wiki_root.parent`` is already the established knowledge-root
    derivation, not a new one invented for this call site.

    ``answer.verdict`` reuses the SAME ``approve``/``reject`` vocabulary
    (normalized the same way: stripped, lower-cased) as
    :func:`_apply_merge_answer` — not a third spelling — which also matches
    :mod:`athenaeum.rule_proposals`'s own function names
    (:func:`~athenaeum.rule_proposals.approve_rule_proposal` /
    :func:`~athenaeum.rule_proposals.reject_rule_proposal`). An invalid
    verdict is a fail-soft skip with ``error_code="invalid_decision"``,
    matching the merge applier exactly.

    :func:`~athenaeum.rule_proposals.approve_rule_proposal` /
    :func:`~athenaeum.rule_proposals.reject_rule_proposal` raise
    ``ValueError`` for an unknown or already-resolved *proposal_id*; that is
    caught here and converted into a fail-soft skipped outcome (never
    propagated), matching :func:`_apply_audit_answer`'s
    ``id_not_found``/``already_resolved`` split on the resolver's own
    message text.
    """
    from athenaeum.rule_proposals import approve_rule_proposal, reject_rule_proposal

    decision = answer.verdict.strip().lower()
    if decision not in ("approve", "reject"):
        return DecisionAnswerOutcome(
            path=answer.path,
            decision_id=answer.decision_id,
            decision_type=answer.decision_type,
            applied=False,
            error_code="invalid_decision",
            message=f"verdict must be 'approve' or 'reject', got {answer.verdict!r}",
        )

    knowledge_root = wiki_root.parent
    try:
        if decision == "approve":
            approve_rule_proposal(
                knowledge_root,
                wiki_root,
                proposal_id=answer.decision_id,
                note=answer.note,
            )
        else:
            reject_rule_proposal(
                wiki_root,
                proposal_id=answer.decision_id,
                note=answer.note,
            )
    except ValueError as exc:
        msg = str(exc)
        error_code = "already_resolved" if "already resolved" in msg else "id_not_found"
        return DecisionAnswerOutcome(
            path=answer.path,
            decision_id=answer.decision_id,
            decision_type=answer.decision_type,
            applied=False,
            error_code=error_code,
            message=msg,
        )

    return DecisionAnswerOutcome(
        path=answer.path,
        decision_id=answer.decision_id,
        decision_type=answer.decision_type,
        applied=True,
        error_code=None,
        message=f"rule proposal {'approved' if decision == 'approve' else 'rejected'}",
    )


def _resolve_auto_apply_effort_budget_breach(
    wiki_root: Path, config: dict[str, Any] | None
) -> bool:
    """Resolve the effort-budget ``breach`` boolean the SAME way
    :mod:`athenaeum.wiki_dedupe`'s duplicate-fold guard does (around its
    lines 955-1000): :func:`athenaeum.decisions.list_pending_decisions`
    then :func:`athenaeum.decision_budget.budget_report` (``record_shapes=
    False``) against the configured budget resolvers.

    Deliberately does NOT catch read failures itself -- unlike
    ``wiki_dedupe.py``'s own call site, which fails OPEN on exactly this
    read. The ratchet guard in :func:`_apply_auto_apply_threshold_proposal_answer`
    (issue athenaeum#2449) fails CLOSED instead: it lets any exception here
    propagate so the caller can refuse the ratification with
    ``RATCHET_REFUSAL_BREACH_SIGNAL_UNREADABLE`` rather than treating an
    unreadable signal as "no breach".
    """
    from athenaeum.config import (
        load_config,
        resolve_decisions_budget_decision_minutes_p50_max,
        resolve_decisions_budget_decision_minutes_p95_max,
        resolve_decisions_budget_item_age_p95_days_max,
        resolve_decisions_budget_items_per_day_max,
        resolve_decisions_budget_window_days,
        resolve_decisions_max_item_context_tokens,
        resolve_decisions_max_sources_per_merge,
    )
    from athenaeum.decision_budget import budget_report
    from athenaeum.decisions import list_pending_decisions

    resolved_config = config if config is not None else load_config(wiki_root.parent)
    pending_items = list_pending_decisions(
        wiki_root,
        max_sources_per_merge=resolve_decisions_max_sources_per_merge(resolved_config),
        max_item_context_tokens=resolve_decisions_max_item_context_tokens(
            resolved_config
        ),
    )
    budget = budget_report(
        wiki_root,
        pending_items,
        items_per_day_max=resolve_decisions_budget_items_per_day_max(resolved_config),
        decision_minutes_p50_max=resolve_decisions_budget_decision_minutes_p50_max(
            resolved_config
        ),
        decision_minutes_p95_max=resolve_decisions_budget_decision_minutes_p95_max(
            resolved_config
        ),
        item_age_p95_days_max=resolve_decisions_budget_item_age_p95_days_max(
            resolved_config
        ),
        window_days=resolve_decisions_budget_window_days(resolved_config),
        record_shapes=False,
    )
    return bool(budget.get("breach"))


def _apply_auto_apply_threshold_proposal_answer(
    wiki_root: Path, answer: DecisionAnswer, *, config: dict[str, Any] | None = None
) -> DecisionAnswerOutcome:
    """Apply one ``auto-apply-threshold`` decision answer against the real
    store (:mod:`athenaeum.auto_apply_proposals`, issue athenaeum#2018) —
    tier 0, deterministic, no LLM call.

    **Widens config only, never bypasses in-flight decisions**:
    :func:`~athenaeum.auto_apply_proposals.
    approve_auto_apply_threshold_proposal` only appends to this proposal
    type's own ledger — it never touches ``_pending_questions.md`` /
    ``_pending_merges.md`` or any already-decided verdict. Mirrors
    :func:`_apply_proposed_rule_answer`'s shape exactly.

    **Ratchet guard (issue athenaeum#2449, athenaeum#2019):** an ``approve``
    is refused while the decision queue's effort budget is in breach —
    resolved once per call via :func:`_resolve_auto_apply_effort_budget_breach`
    (a batch of answer files typically carries at most one
    ``auto-apply-threshold`` entry, so resolving per-answer rather than
    hoisting a batch-level cache into the shared loop in
    :func:`apply_decision_answers` keeps this guard self-contained and
    independently testable) and checked via
    :func:`athenaeum.decision_budget.ratification_refusal`. **Fails
    CLOSED**, the deliberate opposite of the fail-OPEN duplicate-fold guard
    in ``wiki_dedupe.py`` (operator ruling, 2026-11-12): when the signal
    cannot be read, the approve is refused with
    ``RATCHET_REFUSAL_BREACH_SIGNAL_UNREADABLE`` rather than proceeding as
    if there were no breach. A ``reject`` verdict is never refused by this
    guard, readable signal or not — rejecting a widening is always the
    safe direction, and the proposal stays pending either way so it can be
    ratified once the budget recovers.
    """
    from athenaeum.auto_apply_proposals import (
        approve_auto_apply_threshold_proposal,
        reject_auto_apply_threshold_proposal,
    )
    from athenaeum.decision_budget import (
        RATCHET_REFUSAL_BREACH_SIGNAL_UNREADABLE,
        ratification_refusal,
    )

    decision = answer.verdict.strip().lower()
    if decision not in ("approve", "reject"):
        return DecisionAnswerOutcome(
            path=answer.path,
            decision_id=answer.decision_id,
            decision_type=answer.decision_type,
            applied=False,
            error_code="invalid_decision",
            message=f"verdict must be 'approve' or 'reject', got {answer.verdict!r}",
        )

    if decision == "approve":
        try:
            effort_budget_breach = _resolve_auto_apply_effort_budget_breach(
                wiki_root, config
            )
        except Exception as exc:  # noqa: BLE001 — fails CLOSED on purpose:
            # an unreadable breach signal refuses the approve rather than
            # being treated as "no breach" (operator ruling, 2026-11-12) —
            # the deliberate opposite of wiki_dedupe.py's fail-open catch
            # of this same read.
            log.warning(
                "decision_answers: auto-apply-threshold effort-budget "
                "signal unreadable (%s): %s",
                type(exc).__name__,
                exc,
            )
            return DecisionAnswerOutcome(
                path=answer.path,
                decision_id=answer.decision_id,
                decision_type=answer.decision_type,
                applied=False,
                error_code=RATCHET_REFUSAL_BREACH_SIGNAL_UNREADABLE,
                message=(
                    "auto-apply-threshold proposal refused "
                    f"({RATCHET_REFUSAL_BREACH_SIGNAL_UNREADABLE}): the "
                    "effort-budget breach signal could not be read; the "
                    "proposal stays pending"
                ),
            )

        refusal = ratification_refusal(
            "auto-apply-threshold", effort_budget_breach=effort_budget_breach
        )
        if refusal is not None:
            return DecisionAnswerOutcome(
                path=answer.path,
                decision_id=answer.decision_id,
                decision_type=answer.decision_type,
                applied=False,
                error_code=refusal,
                message=(
                    f"auto-apply-threshold proposal refused ({refusal}): the "
                    "decision queue's effort budget is in breach; the "
                    "proposal stays pending"
                ),
            )

    try:
        if decision == "approve":
            approve_auto_apply_threshold_proposal(
                wiki_root,
                proposal_id=answer.decision_id,
                note=answer.note,
            )
        else:
            reject_auto_apply_threshold_proposal(
                wiki_root,
                proposal_id=answer.decision_id,
                note=answer.note,
            )
    except ValueError as exc:
        msg = str(exc)
        error_code = "already_resolved" if "already resolved" in msg else "id_not_found"
        return DecisionAnswerOutcome(
            path=answer.path,
            decision_id=answer.decision_id,
            decision_type=answer.decision_type,
            applied=False,
            error_code=error_code,
            message=msg,
        )

    return DecisionAnswerOutcome(
        path=answer.path,
        decision_id=answer.decision_id,
        decision_type=answer.decision_type,
        applied=True,
        error_code=None,
        message=(
            f"auto-apply-threshold proposal "
            f"{'approved' if decision == 'approve' else 'rejected'}"
        ),
    )




def _apply_page_recompile_answer(
    wiki_root: Path,
    answer: DecisionAnswer,
    *,
    config: dict[str, Any] | None = None,
    lock: "RunLock | None" = None,
) -> DecisionAnswerOutcome:
    """Apply one ``page-recompile`` decision answer (issue athenaeum#2547 AC8).

    Three verdicts, per that AC and
    :data:`athenaeum.page_recompile_queue.VERDICTS`:

    * ``approve`` / ``approve-structure-only`` -- run the recompile applier
      (:func:`athenaeum.page_recompile_apply.write_apply`) for the plan this
      answer's ``decision_id`` names, then close the queue item. The item is
      closed only AFTER a successful apply, so a refused apply leaves it
      open and answerable again rather than closed with nothing done.
    * ``reject`` -- close the item and suppress the page: nothing is
      written, and :func:`athenaeum.page_recompile_queue.rejected_page_skip_reason`
      keeps the nightly phase off that page until its content hash changes.

    **Requires an active run lock**, the same refusal
    :func:`_apply_coordinate_answer` makes for the same reason: an approve
    WRITES pages in the knowledge store, and a write racing the nightly
    librarian is the single-writer violation the lock exists to prevent. A
    ``reject`` needs no lock -- it appends one ledger line and writes no
    page -- and is deliberately not gated on one, so the safe answer is
    always available.

    **A page that moved since the plan was drafted is not applied, and is
    re-planned** (the AC's own wording). The plan's pinned content hash is
    what detects it: ``write_apply`` refuses a mismatch outright. This
    function then closes the item as
    :data:`~athenaeum.page_recompile_queue.STALE_KIND` rather than as a
    rejection -- so the stale draft leaves the queue (no answer to it could
    ever be applied) while recording NO suppression, which is what lets the
    next nightly pass draft a fresh plan over the new bytes. Recording it as
    a rejection instead would have frozen the page at exactly the moment it
    was being actively edited.
    """
    from athenaeum.page_recompile_queue import (
        VERDICT_APPROVE,
        VERDICT_APPROVE_STRUCTURE_ONLY,
        VERDICT_REJECT,
        PageRecompileItemAlreadyResolved,
        UnknownPageRecompileItem,
        find_queued_plan,
        record_page_recompile_approval,
        record_page_recompile_rejection,
        record_page_recompile_stale,
    )

    def _refuse(error_code: str, message: str) -> DecisionAnswerOutcome:
        return DecisionAnswerOutcome(
            path=answer.path,
            decision_id=answer.decision_id,
            decision_type=answer.decision_type,
            applied=False,
            error_code=error_code,
            message=message,
        )

    verdict = answer.verdict.strip().lower()
    if verdict not in (
        VERDICT_APPROVE,
        VERDICT_APPROVE_STRUCTURE_ONLY,
        VERDICT_REJECT,
    ):
        return _refuse(
            "invalid_decision",
            "verdict must be 'approve', 'approve-structure-only' or "
            f"'reject', got {answer.verdict!r}",
        )

    item = find_queued_plan(wiki_root, plan_id=answer.decision_id)
    if item is None:
        return _refuse(
            "id_not_found",
            f"no queued page-recompile plan with id {answer.decision_id!r}",
        )

    if verdict == VERDICT_REJECT:
        try:
            record_page_recompile_rejection(
                wiki_root, plan_id=answer.decision_id, note=answer.note
            )
        except PageRecompileItemAlreadyResolved as exc:
            return _refuse("already_resolved", str(exc))
        except UnknownPageRecompileItem as exc:
            return _refuse("id_not_found", str(exc))
        return DecisionAnswerOutcome(
            path=answer.path,
            decision_id=answer.decision_id,
            decision_type=answer.decision_type,
            applied=True,
            error_code=None,
            message=(
                "page-recompile plan rejected; the page is skipped until its "
                "content hash changes"
            ),
        )

    if lock is None:
        return _refuse(
            "lock_required",
            "approving a page-recompile plan writes pages and requires an "
            "active run lock",
        )

    def _close_as_stale(reason: str) -> DecisionAnswerOutcome:
        try:
            record_page_recompile_stale(
                wiki_root,
                plan_id=answer.decision_id,
                verdict=verdict,
                reason=reason,
                note=answer.note,
            )
        except (
            PageRecompileItemAlreadyResolved,
            UnknownPageRecompileItem,
        ) as exc:  # pragma: no cover - find_queued_plan already matched
            log.debug("decision_answers: stale record skipped: %s", exc)
        return _refuse(
            "plan_stale",
            "the page changed since this plan was drafted, so nothing was "
            f"applied and it will be re-planned: {reason}",
        )

    # The AC8 pin, checked HERE rather than inferred from the applier's
    # refusal prose: the applier reports a stale plan as a reason string
    # with no machine-readable code, and matching on prose would silently
    # stop detecting it the day that sentence is reworded. The queued item
    # records the content hash the plan was drafted over, so this is a
    # direct comparison against the live page.
    pinned = item.get("content_hash")
    live, live_error = _page_recompile_live_hash(wiki_root, str(item.get("uid") or ""))
    if live_error:
        return _refuse("apply_refused", live_error)
    if isinstance(pinned, str) and pinned and live != pinned:
        return _close_as_stale(
            "the page's content hash no longer matches the one the plan was "
            "pinned to"
        )

    from athenaeum.page_recompile_apply import write_apply

    outcome = write_apply(
        wiki_root,
        plan_id=answer.decision_id,
        config=config,
    )
    if not outcome.ok:
        # Defence in depth: the applier re-checks the same pin under the
        # lock, so it can still catch a page that moved between the check
        # above and the write. Its refusal for that case is the only one
        # prefixed `stale:`. Every OTHER refusal leaves the item OPEN -- it
        # may well be answerable once whatever blocked it is fixed.
        if outcome.reason.startswith(_STALE_PLAN_REASON_PREFIX):
            return _close_as_stale(outcome.reason)
        return _refuse(
            "apply_refused",
            f"page-recompile plan was not applied: {outcome.reason}",
        )

    try:
        record_page_recompile_approval(
            wiki_root,
            plan_id=answer.decision_id,
            verdict=verdict,
            note=answer.note,
            extra={"created_uids": list(outcome.created_uids)},
        )
    except PageRecompileItemAlreadyResolved as exc:
        return _refuse("already_resolved", str(exc))
    except UnknownPageRecompileItem as exc:  # pragma: no cover - found above
        return _refuse("id_not_found", str(exc))

    return DecisionAnswerOutcome(
        path=answer.path,
        decision_id=answer.decision_id,
        decision_type=answer.decision_type,
        applied=True,
        error_code=None,
        message=(
            f"page-recompile plan applied ({verdict}); "
            f"{len(outcome.created_uids)} sub-page(s) created"
        ),
    )


#: Issue athenaeum#2547 AC8: the prefix
#: :func:`athenaeum.page_recompile_apply.write_apply` puts on its OWN
#: content-hash-pin refusal, the one apply refusal that must close the queue
#: item as stale rather than leave it open. Only a BACKSTOP -- the primary
#: check is the direct hash comparison in
#: :func:`_apply_page_recompile_answer`, which needs no agreement about
#: wording at all -- so a reworded refusal degrades to "item stays open",
#: never to "page silently rewritten".
_STALE_PLAN_REASON_PREFIX = "stale:"


def _page_recompile_live_hash(wiki_root: Path, uid: str) -> tuple[str, str]:
    """``(content_hash, "")`` for the live page *uid*, or ``("", error)``.

    The hash is :func:`athenaeum.page_recompile_plan.page_content_hash` over
    the page BODY -- the same function the plan pinned itself with, so the
    comparison in :func:`_apply_page_recompile_answer` is between two values
    produced the same way.
    """
    from athenaeum.models import EntityIndex, parse_frontmatter
    from athenaeum.page_recompile_plan import page_content_hash

    if not uid:
        return "", "the queued page-recompile item names no page uid"
    path = EntityIndex(wiki_root).get_by_uid(uid)
    if path is None:
        return "", f"no page with uid {uid}"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return "", f"unreadable page {uid}: {exc}"
    _meta, body = parse_frontmatter(text)
    return page_content_hash(body), ""


def _apply_dimension_proposal_answer(
    knowledge_root: Path,
    wiki_root: Path,
    answer: DecisionAnswer,
    *,
    config: dict[str, Any] | None,
) -> DecisionAnswerOutcome:
    """Apply one ``dimension-proposal`` decision answer (issue athenaeum#2016,
    the ratification child of athenaeum#2015/athenaeum#719 Plan step 3-4).

    Three verdicts (see ``decision_framing._approve_rename_reject_schema``):

    - ``approve`` — register the drafted dimension AS DRAFTED, at
      ``state: backfill``, ``origin: proposed:<decision_id>``, written into
      the operator's ``athenaeum.yaml`` ``dimensions:`` list via
      :func:`athenaeum.config.write_dimensions_config`.
    - ``rename`` — same, under ``answer.name`` (the human-edited name)
      instead of the drafter's own name. ``answer.name`` is REQUIRED here;
      ``decision_framing.validate_answer`` already refuses a schema-invalid
      rename with no ``name`` before this function is ever reached, but this
      is checked again defensively rather than trusted blindly.
    - ``reject`` — writes a suppression record only (mirrors
      :func:`athenaeum.rule_proposals.reject_rule_proposal`): the underlying
      ``(dimension_name, shape)`` pair is permanently suppressed by
      set-membership in :func:`athenaeum.dimension_proposals.
      list_pending_dimension_proposals`'s own resolved-id filter — the
      drafter never re-proposes it.

    Validated through :func:`athenaeum.dimensions.build_registry` INSIDE
    :func:`write_dimensions_config` — a malformed draft (bad kebab-case
    rename, a name colliding with a kernel or already-registered dimension)
    fails loudly there, before anything is written, converted here into a
    fail-soft ``invalid_dimension_entry`` outcome (never raised out of the
    batch loop), matching every other refusal in this module.

    "Nothing half-lands" ordering: the YAML write happens BEFORE the ledger
    record. If a crash or re-apply lands between the two, this function
    detects an already-written ``origin: proposed:<decision_id>`` entry in
    the operator's config and skips straight to the ledger write rather than
    re-appending a second, name-colliding copy.
    """
    from athenaeum.config import write_dimensions_config
    from athenaeum.dimension_proposals import (
        APPROVE_KIND,
        DIMENSION_PROPOSALS_LEDGER_VERSION,
        PROPOSAL_KIND,
        REJECT_KIND,
        default_dimension_proposals_ledger_path,
        read_dimension_proposals_ledger,
    )
    from athenaeum.dimensions import DimensionRegistryError
    from athenaeum.store import append_line_durable, now_iso

    def _refuse(error_code: str, message: str) -> DecisionAnswerOutcome:
        return DecisionAnswerOutcome(
            path=answer.path,
            decision_id=answer.decision_id,
            decision_type=answer.decision_type,
            applied=False,
            error_code=error_code,
            message=message,
        )

    decision = answer.verdict.strip().lower()
    if decision not in ("approve", "rename", "reject"):
        return _refuse(
            "invalid_decision",
            f"verdict must be 'approve', 'rename' or 'reject', got {answer.verdict!r}",
        )
    if decision == "rename" and not answer.name.strip():
        return _refuse(
            "invalid_decision", "verdict 'rename' requires a non-empty 'name'"
        )

    records = read_dimension_proposals_ledger(wiki_root)
    proposal = next(
        (
            r
            for r in records
            if r.get("kind") == PROPOSAL_KIND and str(r.get("id")) == answer.decision_id
        ),
        None,
    )
    if proposal is None:
        return _refuse(
            "id_not_found", f"unknown dimension-proposal id: {answer.decision_id}"
        )
    resolved_ids = {
        str(r.get("id")) for r in records if r.get("kind") in (APPROVE_KIND, REJECT_KIND)
    }
    if answer.decision_id in resolved_ids:
        return _refuse(
            "already_resolved",
            f"dimension-proposal {answer.decision_id} already resolved",
        )

    ledger_target = default_dimension_proposals_ledger_path(wiki_root)

    if decision == "reject":
        record = {
            "v": DIMENSION_PROPOSALS_LEDGER_VERSION,
            "kind": REJECT_KIND,
            "id": answer.decision_id,
            "created_at": now_iso(),
            "answered_at": now_iso(),
            "note": answer.note,
        }
        append_line_durable(
            ledger_target, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
        )
        rejected_name = str(proposal.get("name", ""))
        return DecisionAnswerOutcome(
            path=answer.path,
            decision_id=answer.decision_id,
            decision_type=answer.decision_type,
            applied=True,
            error_code=None,
            message="dimension proposal rejected; the underlying shape is suppressed",
            dimension_name=rejected_name,
        )

    # approve / rename
    name = answer.name.strip() if decision == "rename" else str(proposal.get("name", ""))
    # Issue athenaeum#2016 fix: read the LIVE dimensions: list off disk, never
    # the caller's *config* dict. apply_decision_answers loads config ONCE
    # before its whole batch loop; a stale in-memory snapshot would make a
    # second approve in the SAME pass overwrite the first approve's just-
    # written yaml entry (list-replace, not append), and config=None would
    # wipe every operator-declared dimension outright. load_config() re-reads
    # athenaeum.yaml fresh on every call, so each approve in a batch sees
    # every prior one's write.
    from athenaeum.config import load_config

    existing_entries = list(load_config(knowledge_root).get("dimensions") or [])
    already_written = any(
        isinstance(e, dict) and e.get("origin") == f"proposed:{answer.decision_id}"
        for e in existing_entries
    )
    if not already_written:
        # Strip the drafter's ``max_pairs`` ask-budget annotation (issue
        # athenaeum#2015's `enforce_ask_budget`) -- it is informational about
        # the proposal's backfill plan, never a real `applies_to` selector
        # key, and writing it in would make `dimension_applies` require a
        # `max_pairs:` frontmatter key on every claim, which nothing sets.
        applies_to = {
            k: v
            for k, v in dict(proposal.get("applies_to") or {}).items()
            if k != "max_pairs"
        }
        entry: dict[str, Any] = {
            "name": name,
            "kind": proposal.get("dimension_kind"),
            "null_means": proposal.get("null_semantics"),
            "separates": bool(proposal.get("separates", True)),
            "applies_to": applies_to,
            "state": "backfill",
            "origin": f"proposed:{answer.decision_id}",
            "since": datetime.now(timezone.utc).date().isoformat(),
        }
        try:
            write_dimensions_config(knowledge_root, [*existing_entries, entry])
        except DimensionRegistryError as exc:
            return _refuse("invalid_dimension_entry", str(exc))

    record = {
        "v": DIMENSION_PROPOSALS_LEDGER_VERSION,
        "kind": APPROVE_KIND,
        "id": answer.decision_id,
        "created_at": now_iso(),
        "answered_at": now_iso(),
        "name": name,
        "note": answer.note,
    }
    append_line_durable(
        ledger_target, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    )

    return DecisionAnswerOutcome(
        path=answer.path,
        decision_id=answer.decision_id,
        decision_type=answer.decision_type,
        applied=True,
        error_code=None,
        dimension_name=name,
        message=f"dimension {name!r} registered at state=backfill",
    )


def _find_page_path_by_id(wiki_root: Path, page_id: str) -> Path | None:
    """Reverse-lookup a comparator page id to its file under *wiki_root*.

    Issue athenaeum#1993: :class:`athenaeum.comparator.ComparatorPage` and
    :func:`athenaeum.verdict_effects.build_coordinate_request` carry only a
    page's id (its slug, :func:`athenaeum.verdicts.page_id_for_path` with no
    *root* — see that function's docstring: "the comparator's own
    (non-cluster) call site intentionally still does not pass root"), never
    its path — so a coordinate answer, arriving in a LATER process with
    only the id (via ``pair`` in the answer payload), has to look the path
    back up. Walks every ``*.md`` file under *wiki_root* (skipping sidecar
    files — ``_pending_questions.md``, ``_pending_merges.md``, etc. — by
    their leading underscore) and returns the first whose
    :func:`~athenaeum.verdicts.page_id_for_path` matches, using the SAME
    bare-stem derivation the comparator used when it built this id in the
    first place. Returns ``None`` (never raises) when nothing matches — a
    resolution miss is a refusal for the caller to report, not a crash.
    """
    from athenaeum.verdicts import page_id_for_path

    for candidate in sorted(wiki_root.rglob("*.md")):
        if candidate.name.startswith("_"):
            continue
        if page_id_for_path(candidate) == page_id:
            return candidate
    return None


def validate_coordinate_answer(
    wiki_root: Path,
    pending_path: Path,
    *,
    decision_id: str,
    verdict: str,
    config: dict | None,
) -> tuple[str | None, str, list[tuple[str, Path, Path, str, str, dict[str, dict[str, Any]]]]]:
    """Pure validation half of a ``coordinate`` decision answer (issue athenaeum#2544).

    Extracted from :func:`_apply_coordinate_answer`, which used to be the
    ONLY caller — and therefore the only point any of this ran, hours
    after a shape-only write at :func:`write_decision_answer` (issue
    athenaeum#2544's whole premise). Runs every check that function's apply
    step ran: JSON-decoding *verdict*, schema
    (:func:`athenaeum.decision_framing.validate_coordinate_payload`), pair
    membership in *decision_id*'s own batch
    (:func:`athenaeum.verdict_effects.parse_coordinate_batch_members`),
    page-id membership in its own pair, each page resolving to a file
    under *wiki_root* (:func:`_find_page_path_by_id`), and each named
    dimension being registered (:func:`athenaeum.config.resolve_dimensions`).
    Reads *pending_path*/*wiki_root*/*config*; never writes anything — a
    pure function of its arguments, exactly like the two lower-level
    checks it calls — which is what lets it run from BOTH
    :func:`write_decision_answer` (before a byte is written) and
    :func:`_apply_coordinate_answer` (at apply time, as a second,
    independent check against whatever ``raw/answers/`` actually holds —
    a hand-placed or pre-athenaeum#2544 file never ran this at write time).

    Returns ``(error_code, message, plan)``:

    - On refusal: ``error_code`` is one of ``malformed_verdict_json``,
      ``schema_invalid``, ``id_not_found``, ``already_resolved``,
      ``no_recoverable_members``, ``invalid_coordinate_answer`` (the last
      NAMES the failing pair in *message* — issue athenaeum#2544 AC1);
      *message* is human-readable; *plan* is empty.
    - On success: ``error_code`` is ``None``, *message* is ``""``, and
      *plan* is the fully-resolved write plan — one
      ``(pair_key, path_a, path_b, id_a, id_b, dims_for_pair)`` tuple per
      answered pair — :func:`_apply_coordinate_answer` writes from.
    """
    from athenaeum.answers import parse_pending_questions
    from athenaeum.config import resolve_dimensions
    from athenaeum.decision_framing import validate_coordinate_payload
    from athenaeum.verdict_effects import parse_coordinate_batch_members

    try:
        payload = json.loads(verdict)
    except (ValueError, TypeError) as exc:
        return "malformed_verdict_json", f"verdict is not valid JSON: {exc}", []

    schema_errors = validate_coordinate_payload(payload)
    if schema_errors:
        return "schema_invalid", "; ".join(schema_errors), []

    questions = parse_pending_questions(pending_path)
    pq = next((q for q in questions if q.id == decision_id), None)
    if pq is None:
        return "id_not_found", f"coordinate item id not found: {decision_id}", []
    if pq.answered:
        return (
            "already_resolved",
            f"coordinate item {decision_id} already answered",
            [],
        )

    batch_members = parse_coordinate_batch_members(pq.description)
    batch_pairs = {m["pair"] for m in batch_members}
    if not batch_pairs:
        return (
            "no_recoverable_members",
            f"coordinate item {decision_id} carries no recoverable batch "
            "members (missing or malformed machine-readable marker)",
            [],
        )

    registry = resolve_dimensions(config)

    # Validate everything and resolve every page path BEFORE writing
    # anything — same "nothing half-lands" posture as the schema-shape
    # refusal above, just one layer deeper (value semantics, not shape).
    errors: list[str] = []
    plan: list[tuple[str, Path, Path, str, str, dict[str, dict[str, Any]]]] = []
    for item in payload["answers"]:
        pair_key = str(item["pair"])
        if pair_key not in batch_pairs:
            errors.append(
                f"pair {pair_key!r} is not a member of batch {decision_id!r}"
            )
            continue
        ids = pair_key.split("+", 1)
        if len(ids) != 2 or not all(ids):
            errors.append(f"pair {pair_key!r} is not a valid '<id>+<id>' pair key")
            continue
        id_a, id_b = ids
        path_a = _find_page_path_by_id(wiki_root, id_a)
        path_b = _find_page_path_by_id(wiki_root, id_b)
        if path_a is None or path_b is None:
            missing = id_a if path_a is None else id_b
            errors.append(
                f"pair {pair_key!r}: could not locate page {missing!r} under {wiki_root}"
            )
            continue
        dims_for_pair: dict[str, dict[str, Any]] = {}
        for dim_name, values in item["dimensions"].items():
            dimension = registry.get(dim_name)
            if dimension is None:
                errors.append(f"pair {pair_key!r}: unknown dimension {dim_name!r}")
                continue
            if not isinstance(values, dict):
                errors.append(
                    f"pair {pair_key!r} dimension {dim_name!r}: value must be an "
                    "object keyed by page id"
                )
                continue
            unknown_ids = sorted(set(values) - {id_a, id_b})
            if unknown_ids:
                errors.append(
                    f"pair {pair_key!r} dimension {dim_name!r}: {unknown_ids} are "
                    "not members of this pair"
                )
                continue
            dims_for_pair[dim_name] = values
        plan.append((pair_key, path_a, path_b, id_a, id_b, dims_for_pair))

    if errors:
        return "invalid_coordinate_answer", "; ".join(errors), []

    return None, "", plan


def _apply_coordinate_answer(
    wiki_root: Path,
    pending_path: Path,
    answer: DecisionAnswer,
    *,
    config: dict | None,
    lock: "RunLock | None" = None,
) -> DecisionAnswerOutcome:
    """Apply one ``coordinate`` decision answer (issue athenaeum#1993).

    Writes the human-supplied coordinate value(s) to the affected page(s)'
    frontmatter — reusing :func:`athenaeum.pending_merges._write_coordinate`
    verbatim, never a second implementation — then mechanically re-compares
    the pair(s) the answer names through the EXISTING comparator entry
    point (:func:`athenaeum.comparator.record_comparison` ->
    :func:`athenaeum.comparator.compare_pages`), never a second, ad hoc
    comparison path. A freshly decided verdict is ledgered by
    ``record_comparison`` itself exactly like any other comparator verdict
    (:func:`athenaeum.verdicts.append_verdict`) — which is what gives it
    provenance (the ledger entry's ``basis``) and revocability (every
    ledger entry already supports the ``stale``/``stale_reason`` mechanism)
    with ZERO new machinery; see module docstring, "per athenaeum#717's own
    AC for this loop".

    ``client=None`` is passed to ``record_comparison`` deliberately, not a
    shortcut: this module's whole contract is "tier 0 — deterministic, no
    LLM call, ever" (module docstring), and a coordinate answer is exactly
    a SEPARATOR-DIMENSION (Gate 1) input — the common case (e.g. two pages
    now disjoint on ``subject``) resolves mechanically, no Gate 2 content
    judgement needed at all. When Gate 2 genuinely is still needed,
    ``compare_pages`` degrades gracefully (its own documented contract:
    "Never raises for an LLM-unavailable Gate 2") and ``record_comparison``
    reports ``ok=False``/``verdict=None`` rather than ledgering anything —
    the pair stays queued for the next LLM-backed comparator pass
    (:mod:`athenaeum.wiki_dedupe` / :mod:`athenaeum.recompare`) to decide.
    That is the honest outcome, not a bug: the coordinate was still
    WRITTEN, so that later pass's own Gate 1 will see it.

    Fails soft and refuses BEFORE writing anything on: malformed ``verdict``
    JSON, a decoded payload that fails
    :func:`athenaeum.decision_framing.validate_coordinate_payload`, an
    unknown ``decision_id``, an already-answered item, a ``pair`` that is
    not a member of THIS batch
    (:func:`athenaeum.verdict_effects.parse_coordinate_batch_members`), a
    page id that is not one of its own named pair's two members, or an
    unknown dimension name — "nothing half-lands", the same posture every
    other applier in this module takes. Issue athenaeum#2544: all of the
    above now runs via :func:`validate_coordinate_answer`, a pure function
    shared with :func:`write_decision_answer` — one implementation, two
    callers — so this is a defense-in-depth re-check (a hand-placed or
    pre-athenaeum#2544 answer file never ran it at write time), never a
    second implementation of the same checks.
    """
    from athenaeum.answers import resolve_by_id
    from athenaeum.comparator import page_from_path, record_comparison
    from athenaeum.config import resolve_dimensions
    from athenaeum.pending_merges import _write_coordinate
    from athenaeum.verdicts import mark_pairs_stale

    def _refuse(error_code: str, message: str) -> DecisionAnswerOutcome:
        return DecisionAnswerOutcome(
            path=answer.path,
            decision_id=answer.decision_id,
            decision_type=answer.decision_type,
            applied=False,
            error_code=error_code,
            message=message,
        )

    error_code, message, plan = validate_coordinate_answer(
        wiki_root,
        pending_path,
        decision_id=answer.decision_id,
        verdict=answer.verdict,
        config=config,
    )
    if error_code is not None:
        return _refuse(error_code, message)

    # validate_coordinate_answer already resolved the registry internally
    # to check dimension membership; re-resolve it here (cheap — not a
    # re-check of anything validate_coordinate_answer already decided) for
    # the record_comparison call below, which needs the registry object
    # itself, not just a pass/fail on it.
    registry = resolve_dimensions(config)

    if lock is None:
        return _refuse(
            "lock_required",
            "coordinate answers require an active run lock to stale-mark and "
            "re-compare the affected pair(s)",
        )

    recompared: list[str] = []
    for pair_key, path_a, path_b, id_a, id_b, dims_for_pair in plan:
        meta_a, body_a = parse_frontmatter(path_a.read_text(encoding="utf-8"))
        meta_b, body_b = parse_frontmatter(path_b.read_text(encoding="utf-8"))
        meta_a = dict(meta_a) if isinstance(meta_a, dict) else {}
        meta_b = dict(meta_b) if isinstance(meta_b, dict) else {}
        for dim_name, values in dims_for_pair.items():
            dimension = registry.get(dim_name)
            if dimension is None:  # pragma: no cover - already refused above
                continue
            if id_a in values:
                _write_coordinate(meta_a, dimension, values[id_a])
            if id_b in values:
                _write_coordinate(meta_b, dimension, values[id_b])
        atomic_write_text(path_a, render_frontmatter(meta_a) + body_a)
        atomic_write_text(path_b, render_frontmatter(meta_b) + body_b)

        # The write above just changed this pair's CLAIM coordinates, but
        # athenaeum.verdicts' staleness sweep (mark_pairs_stale /
        # select_stale_for_*) is a separate maintenance pass, never run
        # automatically on a bare frontmatter write — without this,
        # record_comparison's own memoization (get_verdict_status) would
        # see the OLD verdict as still "fresh" and hand it back UNCHANGED,
        # never re-comparing. We already know the exact pair that needs
        # re-comparison (we just answered it), so this stale-marks it
        # directly rather than reaching for the content-hash-diff rule
        # built for a bulk corpus sweep (select_stale_for_changed_page).
        #
        mark_pairs_stale(
            wiki_root,
            {pair_key: f"coordinate answered via decisions answer {answer.decision_id}"},
            lock=lock,
        )

        # Issue athenaeum#1994: the FRESH entry's basis.coord_origins maps
        # each dimension THIS answer supplied for this pair to the same
        # `answer.decision_id` that member_provenance_for_batch
        # (verdict_effects.py) would stamp as `decided_by:
        # f"human-batch:{answer.decision_id}"` -- the identical id, loop-
        # invariant across every member of `plan` above, so challenging
        # one batch-answer id (verdicts.challenge_coordinate_answer ->
        # select_stale_for_coordinate_challenged) reaches every pair this
        # answer decided, not just this one. A dimension NOT in
        # `dims_for_pair` (this pair's coordinate did not come from this
        # answer) gets no entry here -- an honest absent mapping, never an
        # invented id.
        pair_coord_origins = {dim_name: answer.decision_id for dim_name in dims_for_pair}

        result = record_comparison(
            wiki_root,
            page_from_path(path_a),
            page_from_path(path_b),
            registry=registry,
            client=None,
            config=config,
            lock=lock,
            authority_basis=f"human-batch:{answer.decision_id}",
            coord_origins=pair_coord_origins,
            # Issue athenaeum#1993: Gate 1's ``subject`` (IDENTITY-kind)
            # comparator refuses to separate on anything but ratified
            # evidence — "subjects never separate on a model-reported
            # scalar" (dimensions.compare_identity's own docstring) — a
            # DIFFERENT value on each side is otherwise reported UNKNOWN,
            # not DISJOINT, no matter how different. A human-answered
            # ``subject`` coordinate for THIS pair IS that evidence class
            # ("backed by human confirmation"), so it is ratified exactly
            # when this pair's own answered dimensions named it — never
            # unconditionally, and never for any other dimension kind
            # (every other comparator ignores the flag by construction).
            subject_ratified="subject" in dims_for_pair,
        )
        if result.get("ok") and not result.get("skipped"):
            recompared.append(f"{pair_key}: {result.get('verdict')}")
        elif result.get("skipped") == "fresh":
            recompared.append(f"{pair_key}: fresh (unexpected — not re-compared)")
        else:
            recompared.append(
                f"{pair_key}: not yet decided "
                f"({result.get('reason') or 'Gate 2 unavailable at tier 0'}); "
                "coordinate written, awaiting the next LLM-backed comparator pass"
            )

    answer_summary = "; ".join(recompared)
    resolve_result = resolve_by_id(
        pending_path,
        answer.decision_id,
        f"Coordinate(s) recorded. Re-compare: {answer_summary}",
    )
    if not resolve_result.get("ok"):
        return _refuse(
            resolve_result.get("error_code") or "resolve_failed",
            resolve_result.get("message")
            or (
                f"coordinate(s) written and re-compared ({answer_summary}) but the "
                "queue item could not be marked answered"
            ),
        )

    return DecisionAnswerOutcome(
        path=answer.path,
        decision_id=answer.decision_id,
        decision_type=answer.decision_type,
        applied=True,
        error_code=None,
        message=f"coordinate(s) written; re-compare: {answer_summary}",
    )


def _raised_at_for(
    decision_type: str,
    decision_id: str,
    *,
    pending_questions_path: Path,
    pending_merges_path: Path,
    wiki_root: Path,
) -> str:
    """Best-effort lookup of a decision item's own raise timestamp.

    Issue athenaeum#1990: called BEFORE dispatch (see
    :func:`apply_decision_answers`) — once a decision is applied it no
    longer appears in its source's pending view. Returns ``""`` on any
    miss or lookup error rather than raising; the caller's budget-event
    recording already treats an empty/unparseable ``raised_at`` as
    "exclude this event from decision-time figures", never a fabricated
    zero.
    """
    try:
        if decision_type == "question":
            from athenaeum.answers import parse_pending_questions

            for pq in parse_pending_questions(pending_questions_path):
                if pq.id == decision_id:
                    return pq.raised_at or pq.created_at
            return ""
        if decision_type == "merge":
            from athenaeum.pending_merges import parse_pending_merges

            for pm in parse_pending_merges(pending_merges_path):
                if pm.id == decision_id:
                    return pm.created_at
            return ""
        if decision_type == "audit":
            from athenaeum.calibration import AUDIT_KIND, read_calibration_ledger

            for rec in read_calibration_ledger(wiki_root):
                if rec.get("kind") == AUDIT_KIND and str(rec.get("id")) == decision_id:
                    return str(rec.get("created_at") or "")
            return ""
        if decision_type == "proposed-rule":
            from athenaeum.rule_proposals import PROPOSAL_KIND, read_rule_proposals_ledger

            for rec in read_rule_proposals_ledger(wiki_root):
                if rec.get("kind") == PROPOSAL_KIND and str(rec.get("id")) == decision_id:
                    return str(rec.get("created_at") or "")
            return ""
        if decision_type == "auto-apply-threshold":
            from athenaeum.auto_apply_proposals import (
                PROPOSAL_KIND,
                read_auto_apply_proposals_ledger,
            )

            for rec in read_auto_apply_proposals_ledger(wiki_root):
                if rec.get("kind") == PROPOSAL_KIND and str(rec.get("id")) == decision_id:
                    return str(rec.get("created_at") or "")
            return ""
        if decision_type == "dimension-proposal":
            from athenaeum.dimension_proposals import (
                PROPOSAL_KIND,
                read_dimension_proposals_ledger,
            )

            for rec in read_dimension_proposals_ledger(wiki_root):
                if rec.get("kind") == PROPOSAL_KIND and str(rec.get("id")) == decision_id:
                    return str(rec.get("created_at") or "")
            return ""
        if decision_type == "page-recompile":
            from athenaeum.page_recompile_queue import find_queued_plan

            item = find_queued_plan(wiki_root, plan_id=decision_id)
            if item is None:
                return ""
            return str(item.get("created_at") or item.get("ts") or "")
    except Exception as exc:  # noqa: BLE001 - lookup is best-effort
        log.debug(
            "decision_answers: raised_at lookup skipped (%s): %s", type(exc).__name__, exc
        )
    return ""


def _answer_order_key(path: Path, answer: "DecisionAnswer | None") -> tuple[str, int]:
    """Oldest-first ordering key for two files under ``raw/answers/``
    (issue athenaeum#2514, Seer review finding).

    A raw filename sort gets the SAME-SECOND case exactly backwards.
    :func:`write_decision_answer` names a file
    ``{ISO-TS}-{type}-{id}.md`` and retries a second-level collision as
    ``{ISO-TS}-{type}-{id}-1.md``, ``-2``, ... In ASCII ``-`` (0x2D) sorts
    before ``.`` (0x2E), so ``sorted()`` puts the SUFFIXED file first and
    the unsuffixed one -- the one written FIRST, i.e. the OLDER -- last.
    Under a "last in the walk is newest" rule that inverts the answer: two
    answers given in the same wall-clock second would resolve to the older
    verdict, which is the precise failure athenaeum#2514 AC5 exists to stop.

    The collision index is located via the ``-{type}-{id}`` infix the writer
    mints, NEVER a trailing-``-digits`` regex: a decision id is 12 hex
    characters and can be all digits, which such a regex would silently read
    as a collision index. A file that does not follow the convention at all
    (hand-authored, or a future scheme) simply sorts at index 0 on its own
    name, which is the pre-existing behaviour.
    """
    stem = path.stem
    if answer is not None:
        marker = f"-{answer.decision_type}-{answer.decision_id}"
        head, found, tail = stem.rpartition(marker)
        if found and tail.startswith("-") and tail[1:].isdigit():
            return (head + marker, int(tail[1:]))
        if found and not tail:
            return (stem, 0)
    return (stem, 0)


def apply_decision_answers(
    wiki_root: Path,
    raw_root: Path,
    *,
    config: dict | None = None,
    lock: "RunLock | None" = None,
) -> ApplyReport:
    """Apply every pending decision-answer file at tier 0.

    Walks ``raw_root/answers/*.md`` in filename order (deterministic — the
    ISO-timestamp filename prefix sorts oldest-first) and dispatches each
    conformant decision-answer record on ``decision_type``:

    - ``question`` -> :func:`athenaeum.answers.resolve_by_id` (the block is
      then picked up and fully processed — raw intake write, source
      write-back, archival — by the ``ingest_answers`` pass that runs
      immediately after this one in the same ``ingest-answers`` tick).
    - ``merge`` -> :func:`athenaeum.pending_merges.resolve_merge`.
    - ``audit`` -> :func:`athenaeum.calibration.record_audit_review`.
    - ``proposed-rule`` -> :func:`athenaeum.rule_proposals.approve_rule_proposal`
      / :func:`~athenaeum.rule_proposals.reject_rule_proposal` (athenaeum#905
      store, wired by athenaeum#921). ``knowledge_root`` is derived as
      ``wiki_root.parent`` — see :func:`_apply_proposed_rule_answer`.
    - ``coordinate`` -> :func:`_apply_coordinate_answer` (athenaeum#1993):
      writes the answered coordinate(s) to the affected page(s)'
      frontmatter and mechanically re-compares the pair(s) via
      :func:`athenaeum.comparator.record_comparison`. The ONE dispatch
      branch in this module that is not purely a file/ledger mutation on
      an already-decided value — see that function's docstring for why it
      still makes no LLM call itself.

    A file with no ``decision_id`` (a legacy answer, or anything else that
    happens to live in ``raw/answers/``) is silently left alone — not
    counted in either ``applied`` or ``skipped``.

    Issue athenaeum#2514 (AC5): when SEVERAL files decide the same
    ``decision_id``, only the NEWEST is dispatched; every older one is
    skipped with ``error_code="superseded"`` and
    :attr:`DecisionAnswerOutcome.superseded_by` naming the winner. Before
    this, every file was dispatched in turn and the FIRST one to land won —
    the rest bounced off the resolver's ``already_resolved`` guard — so an
    older answer beat a newer one. Observed: a 2026-10-14 reject overrode a
    2026-11-12 human approval of the same merge on every nightly tick until
    the old file was moved out of the walk by hand.

    "Newest" is the walk's own order: the filename, whose ISO-timestamp
    prefix :func:`write_decision_answer` mints from the clock. Deliberately
    NOT the ``resolved_at`` frontmatter, even though it comes from that same
    clock — this function's chronology has always been the filename, and
    having two sources of truth for "which is newer" is how an answer file
    becomes un-orderable. (``resolved_at`` could not break the same-second
    tie below anyway: both files carry the same value.)

    That order is :func:`_answer_order_key`, not raw ``sorted()``, because
    raw filename sort inverts exactly the same-second case — see that
    function. Two answers to one decision given in the same wall-clock
    second would otherwise resolve to the OLDER verdict.

    A superseded file is never deleted — like every other skip here, it
    stays as its own audit trail. Note the fail-closed corner: if the newest
    file is malformed or schema-invalid it is refused on its own merits AND
    the older ones stay superseded, so nothing applies. That is deliberate.
    Applying a stale ruling because the current one is unreadable is the
    exact failure this AC exists to stop, and both files report their own
    reason, so an operator sees what happened.

    An unknown id, an already-resolved id, an invalid verdict/decision, or a
    schema-malformed answer file is logged and skipped — the file is NEVER
    deleted (it stays as its own audit trail), and no other file in the
    batch is affected (athenaeum#908 D5/AC5/AC6).

    Takes **no LLM client and makes no model call, ever** — every dispatch
    branch above is a mechanical file/ledger operation.

    Args:
        wiki_root: Directory holding ``_pending_questions.md`` /
            ``_pending_merges.md`` / the calibration ledger.
        raw_root: Raw intake root; answer files live under
            ``raw_root/answers/``.
        config: Optional athenaeum config dict, forwarded to the merge
            dispatch for ``search_backend`` resolution (vector-purge
            hygiene on a fold-into-existing approve is opportunistic — see
            :func:`athenaeum.pending_merges.resolve_merge`).
        lock: Issue athenaeum#712 — the caller's ALREADY-ACQUIRED run lock
            (:class:`athenaeum.runlock.RunLock`), forwarded to a merge
            dispatch so it can record a verdict-ledger entry when
            ``librarian.verdict_ledger_enabled`` is on
            (:func:`athenaeum.verdicts.record_pair_decision`'s
            single-appender contract). ``None`` (the default — every
            pre-athenaeum#712 caller) skips the ledger write entirely; this
            function never acquires a lock itself.

    Returns:
        An :class:`ApplyReport` with per-file outcomes.
    """
    report = ApplyReport()
    answers_dir = raw_root / "answers"
    if not answers_dir.exists():
        return report

    pending_questions_path = wiki_root / "_pending_questions.md"
    pending_merges_path = wiki_root / "_pending_merges.md"

    # Issue athenaeum#2514 (AC5): load the whole batch BEFORE dispatching any
    # of it, so "which file is the newest answer to this decision?" is known
    # when the first (possibly stale) one comes up. Parsing is pure and
    # already happened once per file either way, so this costs nothing but
    # the batch held in memory — which the loop below already implied.
    loaded: list[tuple[Path, DecisionAnswer | None, MalformedDecisionAnswer | None]] = []
    for path in sorted(answers_dir.glob("*.md")):
        try:
            parsed = _load_decision_answer(path)
        except MalformedDecisionAnswer as exc:
            loaded.append((path, None, exc))
            continue
        loaded.append((path, parsed, None))
    # Re-sort on the real chronology now that each file's decision type and
    # id are known: a raw filename sort inverts a same-second collision
    # pair. See `_answer_order_key`. Dispatch order is this order too, so
    # "oldest-first" in this function's contract is now actually true.
    loaded.sort(key=lambda entry: _answer_order_key(entry[0], entry[1]))
    newest_answer_for: dict[str, Path] = {}
    for path, parsed, _load_exc in loaded:
        if parsed is not None:
            # The walk is oldest-first, so the last writer wins.
            newest_answer_for[parsed.decision_id] = path

    for path, answer, load_error in loaded:
        if load_error is not None:
            log.warning("decision_answers: %s", load_error)
            outcome = DecisionAnswerOutcome(
                path=path,
                decision_id=None,
                decision_type=None,
                applied=False,
                error_code="malformed",
                message=str(load_error),
            )
            report.outcomes.append(outcome)
            report.skipped += 1
            continue

        if answer is None:
            # No decision_id — legacy/non-decision file, not our concern.
            continue

        winner = newest_answer_for[answer.decision_id]
        if winner != path:
            # Issue athenaeum#2514 (AC5): a newer file decides this same id.
            message = (
                f"superseded by {winner.name}, a newer answer for decision "
                f"{answer.decision_id}; not applied"
            )
            log.info("decision_answers: %s (%s)", message, path.name)
            report.outcomes.append(
                DecisionAnswerOutcome(
                    path=path,
                    decision_id=answer.decision_id,
                    decision_type=answer.decision_type,
                    applied=False,
                    error_code="superseded",
                    message=message,
                    superseded_by=winner.name,
                )
            )
            report.skipped += 1
            continue

        # Issue athenaeum#717: an answer must satisfy the response schema the
        # framed queue item published for its decision type. Validating here —
        # before any per-type dispatch — is what makes the queue a machine
        # interface rather than a free-text inbox: a malformed answer is
        # refused at the boundary with a structured error code, never
        # half-applied. Fail-soft like every other refusal in this loop.
        #
        # Division of labour, so one condition never gets two error codes: the
        # SCHEMA owns the answer's SHAPE (unknown keys, wrong types, a missing
        # verdict), and each per-type resolver keeps owning its verdict's
        # VALUE — it already reports ``invalid_decision`` with a per-type
        # message for an out-of-vocabulary verdict, and that contract is
        # documented on the MCP mutators. :func:`shape_errors_only` is what
        # draws that line.
        # Issue athenaeum#2016: ``name`` is only a DECLARED schema property for
        # ``dimension-proposal`` (and only valid alongside verdict=="rename" —
        # the schema's own conditional forbids it being present at all
        # otherwise), so it is included here ONLY when non-empty rather than
        # unconditionally like ``verdict``/``note``.
        answer_dict: dict[str, str] = {"verdict": answer.verdict, "note": answer.note}
        if answer.name.strip():
            answer_dict["name"] = answer.name.strip()
        schema_errors = shape_errors_only(
            validate_answer(answer.decision_type, answer_dict)
        )
        if schema_errors:
            message = "; ".join(schema_errors)
            log.warning(
                "decision_answers: answer for %s fails its response schema: %s",
                answer.decision_id,
                message,
            )
            report.outcomes.append(
                DecisionAnswerOutcome(
                    path=path,
                    decision_id=answer.decision_id,
                    decision_type=answer.decision_type,
                    applied=False,
                    error_code="schema_invalid",
                    message=message,
                )
            )
            report.skipped += 1
            continue

        # Issue athenaeum#1990: look up the item's OWN raise timestamp before
        # dispatching — once applied, a question/merge/audit/proposed-rule
        # item is resolved and no longer appears in its source's "pending"
        # view, so this is the last point it is cheaply available. Best
        # effort: a lookup miss leaves raised_at empty, which the budget
        # instrumentation's decision-time metric already excludes rather
        # than treats as zero.
        raised_at = _raised_at_for(
            answer.decision_type,
            answer.decision_id,
            pending_questions_path=pending_questions_path,
            pending_merges_path=pending_merges_path,
            wiki_root=wiki_root,
        )

        if answer.decision_type == "question":
            outcome = _apply_question_answer(pending_questions_path, answer)
        elif answer.decision_type == "merge":
            outcome = _apply_merge_answer(
                pending_merges_path, wiki_root, answer, config=config, lock=lock
            )
        elif answer.decision_type == "audit":
            outcome = _apply_audit_answer(wiki_root, answer)
        elif answer.decision_type == "coordinate":
            outcome = _apply_coordinate_answer(
                wiki_root, pending_questions_path, answer, config=config, lock=lock
            )
        elif answer.decision_type == "proposed-rule":
            outcome = _apply_proposed_rule_answer(wiki_root, answer)
        elif answer.decision_type == "auto-apply-threshold":
            outcome = _apply_auto_apply_threshold_proposal_answer(
                wiki_root, answer, config=config
            )
        elif answer.decision_type == "dimension-proposal":
            # Issue athenaeum#2016: converts the prior child's bare
            # ``else: # "proposed-rule" — only other member`` into an
            # explicit branch per type, per this issue's own AC — the
            # misroute-guard test now asserts a dimension-proposal answer
            # reaches THIS branch, never ``_apply_proposed_rule_answer``.
            outcome = _apply_dimension_proposal_answer(
                wiki_root.parent, wiki_root, answer, config=config
            )
        elif answer.decision_type == "page-recompile":
            # Issue athenaeum#2547: its own applier -- an approve runs the
            # recompile applier under the caller's run lock and WRITES
            # pages, which no other branch here does.
            outcome = _apply_page_recompile_answer(
                wiki_root, answer, config=config, lock=lock
            )
        else:  # pragma: no cover - unreachable: _load_decision_answer already
            # refuses any decision_type outside VALID_DECISION_TYPES as
            # MalformedDecisionAnswer before this loop ever dispatches.
            outcome = DecisionAnswerOutcome(
                path=answer.path,
                decision_id=answer.decision_id,
                decision_type=answer.decision_type,
                applied=False,
                error_code="unroutable",
                message=f"no dispatch branch for decision_type {answer.decision_type!r}",
            )

        if outcome.applied:
            # Best-effort: the budget ledger is instrumentation, never a
            # reason to fail an already-successful apply.
            try:
                from athenaeum.decision_budget import record_decision_answered

                record_decision_answered(
                    wiki_root,
                    decision_id=answer.decision_id,
                    decision_type=answer.decision_type,
                    raised_at=raised_at,
                )
            except Exception as exc:  # noqa: BLE001 - must never break apply
                log.debug(
                    "decision_answers: budget-event recording skipped (%s): %s",
                    type(exc).__name__,
                    exc,
                )

            # Issue athenaeum#1996 ratchet guard 2: tag a default-acceptance AT
            # ANSWER TIME -- right here, on the same conformant, newly-applied
            # answer the budget-event write above just recorded -- and offer
            # it to the deterministic sampler. Best-effort, same discipline as
            # the budget-event write immediately above: this is a calibration
            # SIDE channel, never a reason to fail an already-successful
            # apply.
            #
            # Checked against `answer.origin_decision_type` -- the ORIGINAL
            # outbound-queue type -- never `answer.decision_type` (the
            # inbound APPLIER type). Those two differ for a `confirmation`
            # item (applier "question"): checking the applier type would
            # silently and permanently exclude `confirmation` -- the exact
            # Seer finding on PR athenaeum#2005, since `question`'s own free-text
            # schema has no discrete default to match at all.
            try:
                from athenaeum.calibration import sample_default_acceptance
                from athenaeum.decision_framing import is_default_acceptance

                if is_default_acceptance(answer.origin_decision_type, answer.verdict):
                    # Issue athenaeum#2079 AC1: the sampler refuses an answer
                    # whose source is a human decision_answer (the operator
                    # cannot meaningfully re-review their own ruling).
                    sample_default_acceptance(
                        wiki_root,
                        decision_id=answer.decision_id,
                        answer_source=answer.source,
                        config=config,
                    )
            except Exception as exc:  # noqa: BLE001 - must never break apply
                log.debug(
                    "decision_answers: default-acceptance sampling skipped (%s): %s",
                    type(exc).__name__,
                    exc,
                )

            # Issue athenaeum#2017 (AC1): ingest this resolution as a claim with
            # provenance (who/when/which decision id) -- a resolution-claims
            # SIDE channel, same discipline as the two best-effort writes
            # above: never a reason to fail an already-successful apply.
            # ingest_resolution_claim itself is a no-op for a decision_type
            # outside RESOLUTION_CLAIM_DECISION_TYPES, so this call is safe
            # (and a no-op) for "question"/"merge"/"proposed-rule" too.
            try:
                from athenaeum.resolution_claims import ingest_resolution_claim

                ingest_resolution_claim(
                    wiki_root,
                    decision_id=answer.decision_id,
                    decision_type=answer.decision_type,
                    verdict=answer.verdict,
                    dimension_name=outcome.dimension_name,
                    resolved_at=answer.resolved_at,
                    note=answer.note,
                )
            except Exception as exc:  # noqa: BLE001 - must never break apply
                log.debug(
                    "decision_answers: resolution-claim ingestion skipped (%s): %s",
                    type(exc).__name__,
                    exc,
                )

        report.outcomes.append(outcome)
        if outcome.applied:
            report.applied += 1
        else:
            report.skipped += 1
        log.info(
            "decision_answers: decision_id=%s type=%s applied=%s error_code=%s: %s",
            outcome.decision_id,
            outcome.decision_type,
            outcome.applied,
            outcome.error_code,
            outcome.message,
        )

    return report


__all__ = [
    "VALID_DECISION_TYPES",
    "DECISION_ANSWER_SOURCE_TAG",
    "MalformedDecisionAnswer",
    "CoordinateAnswerRefused",
    "DecisionAnswer",
    "DecisionAnswerOutcome",
    "ApplyReport",
    "UnappliedAnswerRecord",
    "render_decision_answer",
    "write_decision_answer",
    "validate_coordinate_answer",
    "preflight_question",
    "preflight_merge",
    "preflight_audit",
    "scan_unapplied_answers",
    "scan_overlapping_merge_proposals",
    "check_pending_conflicts",
    "apply_decision_answers",
]
