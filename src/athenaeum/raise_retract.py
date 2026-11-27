# SPDX-License-Identifier: Apache-2.0
"""Apply a ``retract`` message to a raised copy in the target (athenaeum#3003).

``docs/design/federation-raise-retract.md`` §7.2 (how a message finds the copy),
§7.3 (who may retract), §7.4 (applying a message to the copy) and §14 slice S11,
under operator ruling 3 (§15): a copy nobody edited is removed by rule; an edited
copy stays until a person decides. :func:`apply_retract` is what the target's
compile role does with one:

1. **Find the copy** by the uid derived from the target ``store_id`` and the
   ``raise_id`` (the lookup :mod:`athenaeum.raise_supersede` uses: no index, no
   scan). What is there decides the action (§7.2):

   * the copy: continue below;
   * a tombstone: the copy was folded into another page. The tombstone is the
     copy, and the page it names (:func:`athenaeum.models.tombstone_target`) is a
     dependent that is named in the decision, never touched. A tombstone is by
     definition not what was accepted, so it is treated as edited;
   * nothing, after an accepted raise: the target removed the copy itself. The
     message is recorded (``copy_removed``) and stops;
   * nothing, while the raise still waits for a person: the raise is
     **withdrawn**. Both messages are consumed and recorded ``withdrawn`` and the
     raise's pending decision is settled ``withdrawn``;
   * nothing, for a rejected or unknown raise: recorded and stops;
   * a page with another ``raise_id`` or none: never touched, a decision is raised.

2. **Who may retract** (§7.3). By rule only a message that arrives through the
   same location as the raise and names the same proposer, as the acceptance
   ledger records them. Anything else is held as a decision.
3. **Untouched or edited.** The copy is projected as it stands and its payload
   hash compared with ``raised_from.content_hash`` (never a hash of the whole
   page, so a change to a key outside the projection leaves it untouched).
4. **Untouched**: unless an ``always`` relevance rule protects it (athenaeum#3152: then it
   is kept and a decision asks), removed with
   :func:`athenaeum.retire_pages.apply_retirement` (a snapshot commit, then the
   removal; pending merges naming it are withdrawn). The removal commit carries
   ``Athenaeum-Raise: <raise_id> seq=<n> retract``.
5. **Edited**: not removed. A ``raise_retract`` decision asks the compile role to
   remove it or keep it as the target's own page; :func:`answer_retract` applies
   the answer. Kept, the copy's ``watch`` becomes ``ended`` and every later
   message for that ``raise_id`` is recorded ``copy_ended`` and ignored.

A retract does not check ``seq`` against what was applied: it is the origin's
withdrawal, and it applies to whatever version is there. A retract with reason
``erasure`` is not special-cased here (raise S10), and the pages that referred to
the copy are not listed (raise S12).

:func:`register_retract_handler` registers :func:`handle_retract` for the
``retract`` kind on the store raw-intake dispatcher
(:mod:`athenaeum.store_intake_dispatch`).

Layering: L4 (domain/pipeline). Imports :mod:`athenaeum.retire_pages` (L4),
:mod:`athenaeum.raise_supersede`, :mod:`athenaeum.raise_acceptance` and
:mod:`athenaeum.store_intake_dispatch` (L3), :mod:`athenaeum.raise_acceptance_ledger`,
:mod:`athenaeum.raise_messages`, :mod:`athenaeum.raise_projection`,
:mod:`athenaeum.raise_provenance`, :mod:`athenaeum.store_decisions`,
:mod:`athenaeum.atomic_io`, :mod:`athenaeum.models` and :mod:`athenaeum.store`.
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from athenaeum.atomic_io import atomic_write_text
from athenaeum.models import (
    is_tombstone,
    parse_frontmatter,
    render_frontmatter,
    tombstone_target,
)
from athenaeum.raise_acceptance import (
    ASKED,
    COMMIT_LINE_KEY,
    DECISION_KIND_HELD,
    DECISION_KIND_RAISE,
    DUPLICATE,
    HELD,
    Outcome,
    RaiseAcceptRefused,
    _message_dict,
    _pages_at_uid,
)
from athenaeum.raise_acceptance_ledger import (
    DISPOSITION_ACCEPTED,
    DISPOSITION_HELD,
    DISPOSITION_REJECTED,
    AcceptanceRow,
    disposition_of,
    is_rejected,
    iter_rows,
    record_acceptance,
)
from athenaeum.raise_messages import (
    KIND_RAISE,
    KIND_RETRACT,
    Fallthrough,
    RaiseMessage,
    parse_message,
)
from athenaeum.raise_projection import derived_uid, payload_hash
from athenaeum.raise_provenance import RaisedFrom, RaisedFromError, parse_raised_from
from athenaeum.raise_supersede import APPLIED, IGNORED, _accepted_rows, _has_waiting_raise
from athenaeum.retire_pages import (
    RetireCandidate,
    apply_retirement,
    describe_protection,
    find_policy_protected,
)
from athenaeum.store import commit_paths, repo_head
from athenaeum.store_decisions import file_decision, load_queue, resolve_decision
from athenaeum.store_intake_dispatch import (
    HandlerResult,
    MessageContext,
    register_handler,
)

__all__ = [
    "DECISION_KIND_RETRACT",
    "WITHDRAWN",
    "answer_retract",
    "apply_retract",
    "commit_line",
    "handle_retract",
    "register_retract_handler",
]

#: The decision kind asking the compile role to remove an edited copy or keep it.
DECISION_KIND_RETRACT = "raise_retract"

#: Outcome status: a waiting raise was withdrawn (both messages consumed).
WITHDRAWN = "withdrawn"

_REASON_WITHDRAWN = "withdrawn"
_REASON_KEPT = "copy_kept"


def commit_line(raise_id: str, seq: int) -> str:
    """The one commit line of an applied retract: ids only (§7.4)."""
    return f"{COMMIT_LINE_KEY}: {raise_id} seq={seq} retract"


# -- reading what is at the uid ----------------------------------------------


class _Found:
    """The page at the derived uid: a copy, or a tombstone standing for it."""

    def __init__(
        self,
        path: Path,
        text: str,
        meta: dict[str, Any],
        body: str,
        block: RaisedFrom | None,
        folded_into: str | None,
        tombstone: bool,
    ) -> None:
        self.path = path
        self.text = text
        self.meta = meta
        self.body = body
        self.block = block
        self.folded_into = folded_into
        self.tombstone = tombstone


def _locate(root: Path, uid: str, raise_id: str) -> _Found | str | None:
    """The page at *uid*; ``None`` when none is there; ``"other"`` when it is not
    this raise's copy (another ``raise_id``, none, or more than one page)."""
    pages = _pages_at_uid(Path(root), uid)
    if not pages:
        return None
    if len(pages) != 1:
        return "other"
    path = pages[0]
    try:
        text = path.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(text)
    except (OSError, UnicodeDecodeError, ValueError):
        return "other"
    if not isinstance(meta, dict):
        return "other"
    try:
        block = parse_raised_from(meta)
    except RaisedFromError:
        return "other"
    tombstone = is_tombstone(meta)
    if block is None and not tombstone:
        return "other"
    if block is not None and block.raise_id != raise_id:
        return "other"
    return _Found(path, text, dict(meta), body, block, tombstone_target(meta), tombstone)


# -- the ledger --------------------------------------------------------------


def _record(
    cache_dir: Path, msg: RaiseMessage, target: str, location: str, **kw: Any
) -> AcceptanceRow | None:
    assert msg.raise_id is not None and msg.seq is not None
    try:
        return record_acceptance(
            cache_dir,
            kind=KIND_RETRACT,
            raise_id=msg.raise_id,
            message_id=msg.message_id,
            seq=msg.seq,
            origin=msg.origin,
            target=target,
            proposer=msg.proposer,
            location=location,
            **kw,
        )
    except ValueError:
        return None


def _first_raise_row(cache_dir: Path, raise_id: str, *, accepted: bool) -> AcceptanceRow | None:
    """The row that records where the raise arrived and who proposed it."""
    if accepted:
        return next((r for r in _accepted_rows(cache_dir, raise_id) if r.kind == KIND_RAISE), None)
    return next(
        (
            r
            for r in iter_rows(cache_dir)
            if r.raise_id == raise_id and r.kind == KIND_RAISE and r.reason != "duplicate"
        ),
        None,
    )


def _kept(cache_dir: Path, raise_id: str) -> bool:
    return any(
        r.raise_id == raise_id and r.kind == KIND_RETRACT and r.reason == _REASON_KEPT
        for r in iter_rows(cache_dir)
    )


# -- the entry point ---------------------------------------------------------


def apply_retract(
    knowledge_root: Path,
    record: object,
    *,
    target_store_id: str,
    target_root: Path,
    cache_dir: Path,
    location: str,
    mounts: Any = None,
    asserter: Any = None,
    now: datetime | None = None,
) -> Outcome:
    """Apply one ``retract`` to the target's copy (§7.2, §7.3, §7.4).

    *record* is a decoded message record or an already parsed
    :class:`~athenaeum.raise_messages.RaiseMessage` (such as the retract
    :func:`athenaeum.raise_supersede.retract_for_rejected_successor` builds).

    Returns an :class:`~athenaeum.raise_acceptance.Outcome` whose ``status`` is
    :data:`~athenaeum.raise_supersede.APPLIED` (the copy was removed; ``commit`` is
    the removal), :data:`WITHDRAWN` (a waiting raise was withdrawn),
    :data:`~athenaeum.raise_acceptance.ASKED` (an edited copy: a decision asks
    remove or keep), :data:`~athenaeum.raise_supersede.IGNORED` (recorded, nothing
    to do), ``held`` (recorded, with a decision) or ``duplicate``.
    """
    parsed = record if isinstance(record, RaiseMessage) else parse_message(record)
    if isinstance(parsed, Fallthrough) or parsed.kind != KIND_RETRACT:
        decision = file_decision(
            knowledge_root, store_id=target_store_id,
            question="A raised message could not be applied by rule (reason: unparseable).",
            kind=DECISION_KIND_HELD, mounts=mounts, asserter=asserter,
            detail={"reason": "unparseable"},
        )
        return Outcome(HELD, "unparseable", decision)
    msg = parsed
    assert msg.raise_id is not None and msg.seq is not None
    root = Path(target_root)
    uid = derived_uid(target_store_id, msg.raise_id)

    def stop(reason: str, status: str = IGNORED) -> Outcome:
        row = _record(
            cache_dir, msg, target_store_id, location,
            disposition=DISPOSITION_HELD, reason=reason, now=now,
        )
        return Outcome(status, reason, None, row, uid)

    def hold(reason: str, question: str) -> Outcome:
        decision = file_decision(
            knowledge_root,
            store_id=target_store_id,
            question=question,
            kind=DECISION_KIND_HELD,
            mounts=mounts,
            asserter=asserter,
            detail={
                "reason": reason, "raise_id": msg.raise_id, "message_id": msg.message_id,
                "seq": msg.seq, "message": _message_dict(msg), "location": location,
            },
        )
        row = _record(
            cache_dir, msg, target_store_id, location,
            disposition=DISPOSITION_HELD, reason=reason, now=now,
        )
        return Outcome(HELD, reason, decision, row, uid)

    if disposition_of(cache_dir, msg.message_id) is not None:
        row = _record(
            cache_dir, msg, target_store_id, location,
            disposition=DISPOSITION_HELD, reason="duplicate", now=now,
        )
        return Outcome(DUPLICATE, "duplicate", None, row, uid)
    if msg.target != target_store_id:
        return hold("target_mismatch", "A retract names another store than the one it reached.")
    if is_rejected(cache_dir, msg.raise_id):
        return stop("already_rejected")  # §7.2: rejected -- record the message and stop

    found = _locate(root, uid, msg.raise_id)
    if found is None:
        accepted = _first_raise_row(cache_dir, msg.raise_id, accepted=True)
        if accepted is not None:
            return stop("copy_removed")  # the target removed the copy itself
        if _has_waiting_raise(cache_dir, msg.raise_id):
            return _withdraw(
                knowledge_root, msg, uid, target_store_id=target_store_id, cache_dir=cache_dir,
                location=location, hold=hold, now=now,
            )
        return stop("raise_unknown")
    if isinstance(found, str):
        return hold(
            "uid_taken",
            "The page at the uid of this raise is not its copy; it is never touched.",
        )

    # §7.3: by rule only from the same proposer, through the same location.
    first = _first_raise_row(cache_dir, msg.raise_id, accepted=True)
    if first is None or first.proposer != msg.proposer or first.location != location:
        return hold(
            "sender_mismatch",
            "This retract did not arrive from the proposer and through the location "
            "of the raise it names; it is not applied by rule.",
        )
    if (found.block is not None and found.block.watch == "ended") or _kept(
        cache_dir, msg.raise_id
    ):
        return stop("copy_ended")

    untouched = (
        not found.tombstone
        and found.block is not None
        and payload_hash(found.text) == found.block.content_hash
    )
    protection = _protection(knowledge_root, found, uid)
    if untouched and protection is None:
        commit = _remove(root, found, uid, msg, knowledge_root=knowledge_root)
        row = _record(
            cache_dir, msg, target_store_id, location,
            disposition=DISPOSITION_ACCEPTED, now=now,
        )
        return Outcome(APPLIED, None, None, row, uid, commit)

    folded = found.folded_into
    reason = "folded" if found.tombstone else "edited"
    if untouched:
        reason = "protected"
    question = (
        "The origin withdrew a page that was raised to this store, and the copy here "
        + (
            f"was folded into another page ({folded}); the copy is now a tombstone. "
            if found.tombstone
            else (
                f"is protected from removal by the relevance policy ({protection}); "
                "it was not removed. "
                if untouched
                else "was edited after it was accepted. "
            )
        )
        + "Remove the copy, or keep it as this store's own page? Kept, it is no longer "
        "watched and later messages about it are ignored."
    )
    decision = file_decision(
        knowledge_root,
        store_id=target_store_id,
        question=question,
        kind=DECISION_KIND_RETRACT,
        mounts=mounts,
        asserter=asserter,
        detail={
            "reason": reason,
            "raise_id": msg.raise_id,
            "message_id": msg.message_id,
            "seq": msg.seq,
            "message": _message_dict(msg),
            "location": location,
            "uid": uid,
            **({"folded_into": folded} if folded else {}),
        },
    )
    row = _record(
        cache_dir, msg, target_store_id, location, disposition=DISPOSITION_HELD, now=now
    )
    return Outcome(ASKED, None, decision, row, uid)


def _protection(knowledge_root: Path, found: _Found, uid: str) -> str | None:
    """Why the relevance policy protects the copy from removal, or ``None`` (athenaeum#3152).

    Asks the one shared helper (:func:`athenaeum.retire_pages.find_policy_protected`,
    which delegates to :func:`athenaeum.relevance_policy.protecting_rule`). A retract
    arrives unattended, so a protected copy is never removed by rule: it is
    kept and a decision asks. An invalid policy fails closed the same way.
    """
    from athenaeum.relevance_policy import RelevancePolicyError

    cand = RetireCandidate(uid=uid, path=found.path)
    try:
        hits = find_policy_protected(knowledge_root, [cand])
    except RelevancePolicyError as exc:
        return f"the relevance policy is invalid: {exc}"
    if not hits:
        return None
    return describe_protection(*hits[0])


def _remove(
    root: Path,
    found: _Found,
    uid: str,
    msg: RaiseMessage,
    *,
    knowledge_root: Path,
    forced: bool = False,
) -> str | None:
    """Remove the copy with the retirement mechanics; the removal commit carries the line.

    *forced* is an operator's explicit "remove" on a decision: a copy the relevance
    policy protects is then removed and the override is recorded in the commit.
    """
    from athenaeum.relevance_policy import RelevancePolicyError

    assert msg.raise_id is not None and msg.seq is not None
    cand = RetireCandidate(uid=uid, path=found.path)
    overridden: list[tuple[RetireCandidate, Any]] = []
    if forced:
        try:
            overridden = find_policy_protected(knowledge_root, [cand])
        except RelevancePolicyError:  # an invalid policy: the operator's answer stands
            overridden = []
    report = apply_retirement(
        root,
        [cand],
        reason="raised page withdrawn at its origin",
        trailer=commit_line(msg.raise_id, msg.seq),
        forced_protected=overridden or None,
    )
    if report.errors or not report.committed:
        # Nothing is recorded: the message is read again and the removal retried.
        raise RuntimeError("retract removal failed: " + "; ".join(report.errors or ["no commit"]))
    return repo_head(root)


def _withdraw(
    knowledge_root: Path,
    msg: RaiseMessage,
    uid: str,
    *,
    target_store_id: str,
    cache_dir: Path,
    location: str,
    hold: Any,
    now: datetime | None,
) -> Outcome:
    """A retract for a raise still waiting for a person: both messages are consumed."""
    assert msg.raise_id is not None
    raised = _first_raise_row(cache_dir, msg.raise_id, accepted=False)
    assert raised is not None  # _has_waiting_raise saw one
    if raised.proposer != msg.proposer or raised.location != location:
        return hold(
            "sender_mismatch",
            "This retract did not arrive from the proposer and through the location "
            "of the raise it names; the waiting raise is not withdrawn by rule.",
        )
    for r in iter_rows(cache_dir):
        if r.raise_id == msg.raise_id and r.kind == KIND_RAISE and r.reason != "duplicate":
            record_acceptance(
                cache_dir, disposition=DISPOSITION_REJECTED, kind=KIND_RAISE,
                raise_id=r.raise_id, message_id=r.message_id, seq=r.seq, origin=r.origin,
                target=r.target, proposer=r.proposer, location=r.location,
                reason=_REASON_WITHDRAWN, now=now,
            )
    row = _record(
        cache_dir, msg, target_store_id, location,
        disposition=DISPOSITION_REJECTED, reason=_REASON_WITHDRAWN, now=now,
    )
    for rec in load_queue(knowledge_root, target_store_id):
        message = (rec.get("detail") or {}).get("message") or {}
        if (
            rec.get("kind") == DECISION_KIND_RAISE
            and rec.get("disposition") == "pending"
            and message.get("raise_id") == msg.raise_id
        ):
            resolve_decision(
                knowledge_root, store_id=target_store_id, decision_id=rec["id"],
                disposition=_REASON_WITHDRAWN,
            )
    return Outcome(WITHDRAWN, _REASON_WITHDRAWN, None, row, uid)


# -- the answer to "remove or keep" ------------------------------------------


def answer_retract(
    knowledge_root: Path,
    *,
    target_store_id: str,
    target_root: Path,
    cache_dir: Path,
    decision_id: str,
    keep: bool,
    now: datetime | None = None,
) -> Outcome:
    """Apply the compile role's answer to a pending ``raise_retract`` decision.

    ``keep=False`` removes the copy (edited or not) with the retirement mechanics
    and the same commit line. ``keep=True`` keeps it as the target's own page: its
    ``raised_from.watch`` becomes ``ended`` (a tombstone without a block is only
    recorded), the retract is recorded ``copy_kept`` and later messages for the
    ``raise_id`` are recorded and ignored. Either way the decision is settled
    (``removed`` or ``kept``). Raises :class:`RaiseAcceptRefused` when the decision
    is not a pending ``raise_retract`` one.
    """
    rec = next(
        (r for r in load_queue(knowledge_root, target_store_id) if r.get("id") == decision_id),
        None,
    )
    if rec is None:
        raise RaiseAcceptRefused("no_decision", f"no decision {decision_id} in the queue")
    if rec.get("kind") != DECISION_KIND_RETRACT or rec.get("disposition") != "pending":
        raise RaiseAcceptRefused("not_pending", f"{decision_id} is not a pending retract decision")
    detail = rec.get("detail") or {}
    parsed = parse_message(detail.get("message"))
    if isinstance(parsed, Fallthrough) or parsed.kind != KIND_RETRACT:
        raise RaiseAcceptRefused("unparseable", f"{decision_id} carries no parseable retract")
    msg = parsed
    assert msg.raise_id is not None and msg.seq is not None
    location = str(detail.get("location", ""))
    root = Path(target_root)
    uid = derived_uid(target_store_id, msg.raise_id)
    found = _locate(root, uid, msg.raise_id)
    commit: str | None = None
    if isinstance(found, str):
        raise RaiseAcceptRefused(
            "uid_taken", "the page at the uid of this raise is no longer its copy"
        )
    if found is None:
        row = _record(
            cache_dir, msg, target_store_id, location,
            disposition=DISPOSITION_ACCEPTED, now=now,
        )
        resolve_decision(
            knowledge_root, store_id=target_store_id, decision_id=decision_id,
            disposition="removed",
        )
        return Outcome(IGNORED, "copy_removed", None, row, uid)
    if keep:
        if found.block is not None:
            meta = dict(found.meta)
            block = dict(meta["raised_from"])
            block["watch"] = "ended"
            meta["raised_from"] = block
            atomic_write_text(found.path, render_frontmatter(meta) + found.body)
            try:
                commit = commit_paths(
                    root,
                    [found.path],
                    f"Keep a retracted raised page as the store's own ({uid})\n\n"
                    f"{COMMIT_LINE_KEY}: {msg.raise_id} seq={msg.seq} keep\n",
                )
            except (subprocess.CalledProcessError, OSError):
                atomic_write_text(found.path, found.text)
                raise
        row = _record(
            cache_dir, msg, target_store_id, location,
            disposition=DISPOSITION_HELD, reason=_REASON_KEPT, now=now,
        )
        resolve_decision(
            knowledge_root, store_id=target_store_id, decision_id=decision_id,
            disposition="kept",
        )
        return Outcome(IGNORED, _REASON_KEPT, None, row, uid, commit)
    # An operator answering "remove" on a protected copy is the explicit override
    # (the counterpart of ``retire-pages --force-retire``): it is recorded in the commit.
    commit = _remove(root, found, uid, msg, knowledge_root=knowledge_root, forced=True)
    row = _record(
        cache_dir, msg, target_store_id, location, disposition=DISPOSITION_ACCEPTED, now=now
    )
    resolve_decision(
        knowledge_root, store_id=target_store_id, decision_id=decision_id, disposition="removed"
    )
    return Outcome(APPLIED, None, None, row, uid, commit)


# -- the raw-intake handler --------------------------------------------------


def handle_retract(ctx: MessageContext, msg: RaiseMessage, record: dict[str, Any]) -> HandlerResult:
    """The ``retract`` handler of a store's raw intake (§7.2 to §7.4)."""
    assert ctx.target_root is not None  # the dispatcher never calls without a mount
    outcome = apply_retract(
        ctx.knowledge_root,
        record,
        target_store_id=ctx.store_id,
        target_root=ctx.target_root,
        cache_dir=ctx.cache_dir,
        location=ctx.location,
        mounts=dict(ctx.mounts),
        asserter=ctx.asserter,
    )
    return HandlerResult(detail=f"{outcome.status}:{outcome.reason or ''}")


def register_retract_handler() -> None:
    """Register :func:`handle_retract` for ``retract`` on the default registry (idempotent)."""
    register_handler(KIND_RETRACT, handle_retract, replace=True)
