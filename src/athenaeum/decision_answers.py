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
  ``proposed-rule`` (**required**). The three live id spaces are per-type
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

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import yaml

from athenaeum.atomic_io import atomic_write_text
from athenaeum.models import parse_frontmatter

if TYPE_CHECKING:  # pragma: no cover - type-checking only, avoids a hard import
    from athenaeum.runlock import RunLock

log = logging.getLogger(__name__)

#: Registered decision types. ``proposed-rule`` is registered per athenaeum#908's
#: D6 — schema-only, fails closed on apply. See module docstring.
DecisionType = Literal["question", "merge", "audit", "proposed-rule"]
VALID_DECISION_TYPES: frozenset[str] = frozenset(
    ("question", "merge", "audit", "proposed-rule")
)

#: Frontmatter ``source:`` tag stamped on every decision-answer file, distinct
#: from :mod:`athenaeum.answers`'s ``pending_question_answer`` legacy tag so
#: the two formats are distinguishable at a glance even before checking for
#: ``decision_id``.
DECISION_ANSWER_SOURCE_TAG = "decision_answer"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class MalformedDecisionAnswer(ValueError):
    """A ``raw/answers/*.md`` file carries ``decision_id`` but fails schema."""


@dataclass
class DecisionAnswer:
    """Parsed view of one decision-answer raw-intake file."""

    decision_id: str
    decision_type: str
    verdict: str
    note: str
    resolved_at: str
    path: Path


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
) -> str:
    """Render a decision-answer raw-intake record (athenaeum#908 D1 format).

    ``verdict`` and ``note`` are YAML-dumped (not hand-interpolated) so a
    multi-line question answer body round-trips safely through the
    frontmatter block instead of corrupting it.

    Raises:
        ValueError: ``decision_type`` is not one of
            :data:`VALID_DECISION_TYPES`, or ``decision_id``/``verdict`` is
            empty.
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

    meta: dict[str, object] = {
        "source": DECISION_ANSWER_SOURCE_TAG,
        "decision_id": decision_id.strip(),
        "decision_type": decision_type,
        "verdict": verdict,
        "resolved_at": resolved_at or _now_iso(),
    }
    if note and note.strip():
        meta["note"] = note

    frontmatter = yaml.safe_dump(
        meta, sort_keys=False, allow_unicode=True, default_flow_style=False
    )
    return (
        "---\n"
        f"{frontmatter}"
        "---\n\n"
        "(decision answer — applied deterministically at tier 0 by "
        "`athenaeum ingest-answers`; see docs/conflict-resolution.md)\n"
    )


def write_decision_answer(
    raw_root: Path,
    *,
    decision_id: str,
    decision_type: str,
    verdict: str,
    note: str = "",
) -> Path:
    """Render + write one decision-answer file under ``raw_root/answers/``.

    Filename: ``{ISO-TS}-{decision_type}-{decision_id}.md``, retried with a
    numeric suffix on collision (two answers written in the same second) —
    mirrors :func:`athenaeum.answers.ingest_answers`'s collision handling.
    Returns the path written.
    """
    answers_dir = raw_root / "answers"
    answers_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    iso_ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    filename_ts = now.strftime("%Y%m%dT%H%M%SZ")

    text = render_decision_answer(
        decision_id=decision_id,
        decision_type=decision_type,
        verdict=verdict,
        note=note,
        resolved_at=iso_ts,
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
    if decision not in ("approve", "reject"):
        return (
            False,
            "invalid_decision",
            f"decision must be 'approve' or 'reject', got {decision!r}",
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

    return DecisionAnswer(
        decision_id=decision_id.strip(),
        decision_type=decision_type,
        verdict=verdict,
        note=str(note),
        resolved_at=str(resolved_at),
        path=path,
    )


# ---------------------------------------------------------------------------
# Apply (tier 0 — deterministic, no LLM call, ever)
# ---------------------------------------------------------------------------


def _apply_question_answer(pending_path: Path, answer: DecisionAnswer) -> DecisionAnswerOutcome:
    from athenaeum.answers import resolve_by_id

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
    from athenaeum.config import resolve_cache_dir, resolve_verdict_ledger_enabled
    from athenaeum.pending_merges import parse_pending_merges, resolve_merge

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

    cfg = config or {}
    search_backend = cfg.get("search_backend", "fts5") if isinstance(cfg, dict) else "fts5"
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
        record_pair_decision(
            wiki_root,
            source_a=sources[0],
            source_b=sources[1],
            verdict=verdict_value,
            decided_by=f"pipeline:merge-{decision}",
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

    A file with no ``decision_id`` (a legacy answer, or anything else that
    happens to live in ``raw/answers/``) is silently left alone — not
    counted in either ``applied`` or ``skipped``.

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

    for path in sorted(answers_dir.glob("*.md")):
        try:
            answer = _load_decision_answer(path)
        except MalformedDecisionAnswer as exc:
            log.warning("decision_answers: %s", exc)
            outcome = DecisionAnswerOutcome(
                path=path,
                decision_id=None,
                decision_type=None,
                applied=False,
                error_code="malformed",
                message=str(exc),
            )
            report.outcomes.append(outcome)
            report.skipped += 1
            continue

        if answer is None:
            # No decision_id — legacy/non-decision file, not our concern.
            continue

        if answer.decision_type == "question":
            outcome = _apply_question_answer(pending_questions_path, answer)
        elif answer.decision_type == "merge":
            outcome = _apply_merge_answer(
                pending_merges_path, wiki_root, answer, config=config, lock=lock
            )
        elif answer.decision_type == "audit":
            outcome = _apply_audit_answer(wiki_root, answer)
        else:  # "proposed-rule" — the only other member of VALID_DECISION_TYPES
            outcome = _apply_proposed_rule_answer(wiki_root, answer)

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
    "DecisionAnswer",
    "DecisionAnswerOutcome",
    "ApplyReport",
    "render_decision_answer",
    "write_decision_answer",
    "preflight_question",
    "preflight_merge",
    "preflight_audit",
    "apply_decision_answers",
]
