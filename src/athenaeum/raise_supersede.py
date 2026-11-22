# SPDX-License-Identifier: Apache-2.0
"""Apply a ``supersede`` message to a raised copy in the target (athenaeum#2940).

``docs/design/federation-raise-retract.md`` §7.2 (how a message finds the copy),
§7.3, §7.4 (applying a message to the copy) and §14 slice S13. The raiser sent a
``supersede`` because the source page changed (a new payload) or was replaced by
another raised page (a successor). :func:`apply_supersede` is what the target's
compile role does with it:

1. **Find the copy** by the uid derived from the target ``store_id`` and the
   ``raise_id``. Nothing is indexed or scanned. A page there with another
   ``raise_id`` (or none) is never touched: a decision is raised.
2. **Ignore what is stale.** A message whose ``seq`` is not above the one already
   applied (the copy's ``raised_from.seq`` and every accepted ledger row) is
   recorded and ignored.
3. **Hold a change of who may see the page.** A payload whose ``access`` or
   ``audience`` differs from the copy's is held for a person, copy untouched or
   not (athenaeum#2624 §5.1: neither a rule nor a model widens or narrows an
   audience).
4. **Ask whether the copy was edited** by projecting it as it stands and comparing
   that payload hash with ``raised_from.content_hash``. Never a hash of the whole
   page, which differs from the moment of acceptance.
5. **Untouched**: the projected keys and the body are replaced by the new payload,
   with the ledger's recorded adjustment of ``type`` and ``tags`` applied again;
   ``content_hash`` and ``seq`` are updated; the commit carries
   ``Athenaeum-Raise: <raise_id> seq=<n> supersede``.
6. **Edited**: a three-way page merge (:func:`athenaeum.page_merge.merge_page`)
   with the last accepted version as ancestor, read through the store seam by the
   blob id of the latest accepted ledger row, the copy as it stands as the local
   side and the new payload as the remote side. The accepted version a row names
   is the **payload as the raiser sent it**, filed as a fresh copy would be (not
   the copy as merged with the target's edits, which as the next ancestor would
   revert those edits); a version no commit holds is written as a blob and
   pinned under :data:`ANCESTOR_REF_PREFIX` so ``git gc`` keeps it. ``raised_from``
   is not merged: it
   is the target's own bookkeeping and is rewritten afterwards. A merge that
   leaves any contested or held unit writes nothing (the published version stays)
   and raises a decision; otherwise the merged page is committed.
7. **Successor form**: the copy is stamped ``superseded_by: <the successor copy's
   name slug>``. A successor whose raise is still waiting keeps the message
   waiting (:data:`WAITING`, nothing recorded). A successor whose raise was
   rejected makes the message a retract with reason ``superseded_at_source``.

**The retract is not applied here.** Applying a retract is raise S11. This module
builds the retract message and hands it to the ``apply_retract`` callable the
caller supplies. Without one, the safe default holds the copy as it is, records
the message ``held`` with reason ``superseded_at_source`` and raises a decision.

**A message waits, rather than being consumed,** (:data:`WAITING`) when the raise
it names is itself still waiting for a person, or the successor's is: the caller
leaves it where it is and offers it again.

Layering: L3 (service). Imports :mod:`athenaeum.raise_acceptance` (L3),
:mod:`athenaeum.raise_acceptance_ledger` (L3), :mod:`athenaeum.page_merge` (L2),
:mod:`athenaeum.raise_messages`, :mod:`athenaeum.raise_projection`,
:mod:`athenaeum.raise_provenance` (L2), :mod:`athenaeum.store_decisions`,
:mod:`athenaeum.store_intake`, :mod:`athenaeum.store_access`,
:mod:`athenaeum.raise_filing_checks` (L3), :mod:`athenaeum.atomic_io` (L0),
:mod:`athenaeum.models` and :mod:`athenaeum.store` (L1).
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from athenaeum.atomic_io import atomic_write_text
from athenaeum.config import StructuredUpsertSpec
from athenaeum.models import (
    is_tombstone,
    parse_frontmatter,
    render_frontmatter,
    slugify,
)
from athenaeum.page_merge import merge_page
from athenaeum.raise_acceptance import (
    _RIGHTS_TO_RAISE,
    COMMIT_LINE_KEY,
    DECISION_KIND_HELD,
    DUPLICATE,
    HELD,
    Outcome,
    _check_message,
    _file_held,
    _message_dict,
    _pages_at_uid,
    _payload_text,
    _proposer_rights,
)
from athenaeum.raise_acceptance_ledger import (
    DISPOSITION_ACCEPTED,
    DISPOSITION_HELD,
    AcceptanceRow,
    disposition_of,
    find_adjustment,
    is_rejected,
    iter_rows,
    record_acceptance,
)
from athenaeum.raise_filing_checks import run_after_publish
from athenaeum.raise_messages import (
    KIND_RAISE,
    KIND_RETRACT,
    KIND_SUPERSEDE,
    Fallthrough,
    RaiseMessage,
    parse_message,
)
from athenaeum.raise_projection import PROJECTED_KEYS, derived_uid, payload_hash
from athenaeum.raise_provenance import RaisedFrom, RaisedFromError, parse_raised_from
from athenaeum.store import commit_paths, read_blob, write_blob
from athenaeum.store_access import StoreDeclaration, load_store_declaration
from athenaeum.store_decisions import file_decision
from athenaeum.store_intake import intake_accepts

__all__ = [
    "APPLIED",
    "IGNORED",
    "WAITING",
    "apply_supersede",
    "commit_line",
    "retract_for_rejected_successor",
]

#: Outcome statuses added to those of :mod:`athenaeum.raise_acceptance`.
APPLIED = "applied"
IGNORED = "ignored"
#: Nothing was recorded: the message waits for the raise it depends on.
WAITING = "waiting"

#: Refs that keep each accepted payload blob reachable, ``<prefix>/<raise_id>/<message_id>``.
ANCESTOR_REF_PREFIX = "refs/athenaeum/raise-accepted"

#: The retract reason a rejected successor turns the message into (§7.4).
RETRACT_REASON = "superseded_at_source"


def commit_line(raise_id: str, seq: int) -> str:
    """The one commit line of an applied supersede: ids only (§7.4)."""
    return f"{COMMIT_LINE_KEY}: {raise_id} seq={seq} supersede"


# -- reading the copy --------------------------------------------------------


@dataclass(frozen=True)
class _Copy:
    path: Path
    text: str
    meta: dict[str, Any]
    body: str
    block: RaisedFrom


def _read_copy(path: Path, raise_id: str) -> _Copy | str:
    """The copy at *path*, or a state: ``folded`` (a tombstone), ``other`` (a page
    that is not a copy of *raise_id*)."""
    try:
        text = path.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(text)
    except (OSError, UnicodeDecodeError, ValueError):
        return "other"
    if not isinstance(meta, dict):
        return "other"
    if is_tombstone(meta):
        return "folded"
    try:
        block = parse_raised_from(meta)
    except RaisedFromError:
        return "other"
    if block is None or block.raise_id != raise_id:
        return "other"
    return _Copy(path, text, dict(meta), body, block)


def _locate(root: Path, uid: str, raise_id: str) -> _Copy | str | None:
    """The copy of *raise_id* at *uid*; ``None`` when no page is there; else a state."""
    pages = _pages_at_uid(Path(root), uid)
    if not pages:
        return None
    found = [_read_copy(p, raise_id) for p in pages]
    if len(found) == 1:
        return found[0]
    return "other"  # more than one page claims the uid: never touch any


# -- ledger helpers ----------------------------------------------------------


def _record(
    cache_dir: Path,
    msg: RaiseMessage,
    target: str,
    location: str,
    **kw: Any,
) -> AcceptanceRow | None:
    assert msg.raise_id is not None and msg.seq is not None
    try:
        return record_acceptance(
            cache_dir,
            kind=KIND_SUPERSEDE,
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


def _accepted_rows(cache_dir: Path, raise_id: str) -> list[AcceptanceRow]:
    return [
        r
        for r in iter_rows(cache_dir)
        if r.raise_id == raise_id
        and r.disposition == DISPOSITION_ACCEPTED
        and r.kind in (KIND_RAISE, KIND_SUPERSEDE)
    ]


def _has_waiting_raise(cache_dir: Path, raise_id: str) -> bool:
    """The raise was seen and is waiting for a person: held, never accepted or rejected."""
    seen = False
    for r in iter_rows(cache_dir):
        if r.raise_id != raise_id or r.kind != KIND_RAISE or r.reason == "duplicate":
            continue
        seen = True
    return seen and not _accepted_rows(cache_dir, raise_id) and not is_rejected(cache_dir, raise_id)


def _hold(
    knowledge_root: Path,
    msg: RaiseMessage,
    *,
    reason: str,
    target_store_id: str,
    cache_dir: Path,
    location: str,
    uid: str,
    question: str,
    units: tuple[str, ...] = (),
    mounts: Any = None,
    asserter: Any = None,
) -> Outcome:
    """Record the message ``held`` and raise one decision about it."""
    row = _record(
        cache_dir, msg, target_store_id, location, disposition=DISPOSITION_HELD, reason=reason
    )
    detail: dict[str, Any] = {
        "reason": reason,
        "raise_id": msg.raise_id,
        "message_id": msg.message_id,
        "seq": msg.seq,
        "message": _message_dict(msg),
        "location": location,
    }
    if units:
        detail["units"] = list(units)
    decision = file_decision(
        knowledge_root,
        store_id=target_store_id,
        question=question,
        kind=DECISION_KIND_HELD,
        mounts=mounts,
        asserter=asserter,
        detail=detail,
    )
    return Outcome(HELD, reason, decision, row, uid)


def _record_and_stop(
    msg: RaiseMessage, *, reason: str, status: str, target_store_id: str, cache_dir: Path,
    location: str, uid: str,
) -> Outcome:
    row = _record(
        cache_dir, msg, target_store_id, location, disposition=DISPOSITION_HELD, reason=reason
    )
    return Outcome(status, reason, None, row, uid)


# -- building the new copy ---------------------------------------------------


def _visibility(page: Mapping[str, Any]) -> tuple[Any, Any]:
    return page.get("access"), page.get("audience")


def _final_page(msg: RaiseMessage, cache_dir: Path) -> dict[str, Any]:
    """The payload's page with the recorded adjustment of ``type`` and ``tags`` applied again."""
    assert msg.page is not None and msg.raise_id is not None
    page = dict(msg.page)
    adj = find_adjustment(cache_dir, msg.raise_id)
    if adj is not None:
        if adj.type is not None:
            page["type"] = adj.type
        if adj.tags is not None:
            page["tags"] = list(adj.tags)
    return page


def _raised_from_after(copy: _Copy, seq: int, content_hash: str | None) -> dict[str, Any]:
    """The copy's own ``raised_from`` block with ``seq`` (and ``content_hash``) updated."""
    block = dict(copy.meta["raised_from"])
    block["seq"] = seq
    if content_hash is not None:
        block["content_hash"] = content_hash
    return block


def _strip_raised_from(text: str) -> str:
    meta, body = parse_frontmatter(text)
    if not isinstance(meta, dict) or "raised_from" not in meta:
        return text
    return render_frontmatter({k: v for k, v in meta.items() if k != "raised_from"}) + body


def _with_raised_from(text: str, block: dict[str, Any]) -> str:
    meta, body = parse_frontmatter(text)
    meta = dict(meta) if isinstance(meta, dict) else {}
    meta["raised_from"] = block
    return render_frontmatter(meta) + body


def _write_and_commit(
    root: Path, copy: _Copy, new_text: str, msg: RaiseMessage, uid: str, what: str
) -> str | None:
    """Replace the copy atomically and commit it; restore it if the commit fails."""
    atomic_write_text(copy.path, new_text)
    try:
        return commit_paths(
            root,
            [copy.path],
            f"{what} ({uid})\n\n{commit_line(msg.raise_id or '', msg.seq or 0)}\n",
        )
    except (subprocess.CalledProcessError, OSError):
        atomic_write_text(copy.path, copy.text)
        raise


# -- the entry point ---------------------------------------------------------


def apply_supersede(
    knowledge_root: Path,
    record: object,
    *,
    target_store_id: str,
    target_root: Path,
    cache_dir: Path,
    location: str,
    specs: Mapping[str, StructuredUpsertSpec] | None = None,
    apply_retract: Callable[[RaiseMessage], Outcome] | None = None,
    mounts: Any = None,
    asserter: Any = None,
    now: datetime | None = None,
) -> Outcome:
    """Apply one decoded ``supersede`` record to the target's copy (§7.2, §7.4).

    Returns an :class:`~athenaeum.raise_acceptance.Outcome` whose ``status`` is
    :data:`APPLIED` (the copy was replaced, merged or stamped; ``commit`` is its
    commit), :data:`IGNORED` (stale ``seq``, an ended watch, or nothing left to
    apply to; recorded), :data:`WAITING` (nothing recorded: the message waits for a
    raise that is itself waiting), ``held`` (recorded, with a decision unless the
    message is only to be recorded) or ``duplicate``.

    *specs* maps a page type to its :class:`StructuredUpsertSpec` for the page
    merge. *apply_retract* receives the retract a rejected successor turns the
    message into (raise S11); without it the message is held.

    Once a copy is replaced (:data:`APPLIED`), the filing checks run on what changed
    (raise S21, :mod:`athenaeum.raise_filing_checks`); their result is the outcome's
    ``filing`` and never changes its status.
    """
    outcome = _apply_supersede(
        knowledge_root, record, target_store_id=target_store_id, target_root=target_root,
        cache_dir=cache_dir, location=location, specs=specs, apply_retract=apply_retract,
        mounts=mounts, asserter=asserter, now=now,
    )
    if outcome.status != APPLIED or outcome.uid is None:
        return outcome
    parsed = parse_message(record)
    raise_id = parsed.raise_id if not isinstance(parsed, Fallthrough) else None
    copies = _pages_at_uid(Path(target_root), outcome.uid)
    if not copies or raise_id is None:
        return outcome
    filing = run_after_publish(
        knowledge_root,
        target_store_id=target_store_id,
        target_root=Path(target_root),
        copy_path=copies[0],
        commit=outcome.commit,
        raise_id=raise_id,
        mounts=mounts,
        asserter=asserter,
    )
    return replace(outcome, filing=filing)


def _apply_supersede(
    knowledge_root: Path,
    record: object,
    *,
    target_store_id: str,
    target_root: Path,
    cache_dir: Path,
    location: str,
    specs: Mapping[str, StructuredUpsertSpec] | None = None,
    apply_retract: Callable[[RaiseMessage], Outcome] | None = None,
    mounts: Any = None,
    asserter: Any = None,
    now: datetime | None = None,
) -> Outcome:
    """:func:`apply_supersede` before the filing checks."""
    parsed = parse_message(record)
    if isinstance(parsed, Fallthrough) or parsed.kind != KIND_SUPERSEDE:
        decision = _file_held(
            knowledge_root, target_store_id, reason="unparseable", msg=None,
            mounts=mounts, asserter=asserter,
        )
        return Outcome(HELD, "unparseable", decision)
    msg = parsed
    assert msg.raise_id is not None and msg.seq is not None
    uid = derived_uid(target_store_id, msg.raise_id)

    def hold(reason: str, question: str, units: tuple[str, ...] = ()) -> Outcome:
        return _hold(
            knowledge_root, msg, reason=reason, target_store_id=target_store_id,
            cache_dir=cache_dir, location=location, uid=uid, question=question,
            units=units, mounts=mounts, asserter=asserter,
        )

    def stop(reason: str, status: str = IGNORED) -> Outcome:
        return _record_and_stop(
            msg, reason=reason, status=status, target_store_id=target_store_id,
            cache_dir=cache_dir, location=location, uid=uid,
        )

    if disposition_of(cache_dir, msg.message_id) is not None:
        row = _record(
            cache_dir, msg, target_store_id, location,
            disposition=DISPOSITION_HELD, reason="duplicate",
        )
        return Outcome(DUPLICATE, "duplicate", None, row, uid)

    # The rule checks of §4.4 that name a supersede. A successor carries no
    # payload, so only the checks that need none run on it.
    declaration = load_store_declaration(target_root)
    if msg.successor is None:
        reason = _check_message(
            msg, target_store_id=target_store_id, declaration=declaration, cache_dir=cache_dir
        )
    elif msg.target != target_store_id:
        reason = "target_mismatch"
    elif not set(_proposer_rights(declaration, msg.proposer)) & set(_RIGHTS_TO_RAISE):
        reason = "proposer_lacks_right"
    else:
        reason = "already_rejected" if is_rejected(cache_dir, msg.raise_id) else None
    if reason == "already_rejected":
        return stop(reason)  # §7.2: rejected -- record the message and stop
    if reason is not None:
        return hold(reason, f"A supersede could not be applied by rule (reason: {reason}).")

    found = _locate(target_root, uid, msg.raise_id)
    if found is None:
        if _accepted_rows(cache_dir, msg.raise_id):
            return stop("copy_removed")  # the target removed the copy itself
        if _has_waiting_raise(cache_dir, msg.raise_id):
            return Outcome(WAITING, None, None, None, uid)
        return stop("raise_unknown")
    if found == "folded":
        return hold(
            "copy_folded",
            "The copy this supersede names was folded into another page; "
            "it is not replaced by rule.",
        )
    if isinstance(found, str):
        return hold(
            "uid_taken",
            "The page at the uid of this raise is not its copy; it is never touched.",
        )
    copy = found

    # §7.3: by rule only from the same proposer, through the same location.
    accepted = _accepted_rows(cache_dir, msg.raise_id)
    first = next((r for r in accepted if r.kind == KIND_RAISE), None)
    if first is None or first.proposer != msg.proposer or first.location != location:
        return hold(
            "sender_mismatch",
            "This supersede did not arrive from the proposer and through the location "
            "of the raise it names; it is not applied by rule.",
        )

    applied_seq = max([copy.block.seq, *(r.seq for r in accepted)])
    if msg.seq <= applied_seq:
        return stop("stale_seq")
    if copy.block.watch == "ended":
        return stop("copy_ended")

    if msg.successor is not None:
        return _apply_successor(
            msg, copy, uid, target_store_id=target_store_id, target_root=Path(target_root),
            cache_dir=cache_dir, location=location, hold=hold, stop=stop,
            apply_retract=apply_retract, now=now,
        )

    assert msg.page is not None and msg.body is not None
    page = _final_page(msg, cache_dir)
    if not isinstance(declaration, StoreDeclaration):
        return hold("proposer_lacks_right", "The store's declaration no longer allows this.")
    verdict = intake_accepts(declaration, page.get("type"), tuple(page.get("tags") or ()))
    if not verdict.accepted:
        return hold(
            "store_refuses",
            "This store's intake rules refuse the type and tags of this supersede "
            f"({verdict.reason}); the copy is kept as it is.",
        )
    # AC4: who may see the page is never changed by a rule, edited or not.
    if _visibility(page) != _visibility(copy.meta):
        return hold(
            "access_changed",
            "The new version changes who may see this page (access or audience). "
            "The published copy stays as it is until a person decides.",
        )

    new_hash = payload_hash(_payload_text(page, msg.body))
    new_block = _raised_from_after(copy, msg.seq, new_hash)
    # The new payload as a fresh copy of it would be filed. It is the remote side of
    # a merge and, whichever way the copy is updated, the version the ledger records
    # as accepted: the NEXT merge's ancestor is what the raiser last sent, never the
    # copy as merged with the target's own edits (that would revert them).
    remote_meta: dict[str, Any] = {"uid": copy.meta.get("uid", uid)}
    for key in PROJECTED_KEYS:
        if key in page:
            remote_meta[key] = page[key]
    if "sources" in copy.meta:
        remote_meta["sources"] = copy.meta["sources"]
    remote = render_frontmatter(remote_meta) + msg.body
    ancestor_text = _with_raised_from(remote, new_block)
    untouched = payload_hash(copy.text) == copy.block.content_hash

    if untouched:
        meta = {k: v for k, v in copy.meta.items() if k not in PROJECTED_KEYS}
        meta["raised_from"] = new_block
        for key in PROJECTED_KEYS:
            if key in page:
                meta[key] = page[key]
        new_text = render_frontmatter(_ordered(meta, copy.meta)) + msg.body
        return _finish(
            copy, new_text, msg, uid, target_store_id=target_store_id,
            target_root=Path(target_root), cache_dir=cache_dir, location=location, now=now,
            accepted_text=ancestor_text,
        )

    # Edited in the target: a three-way merge, ancestor read by blob id.
    base_blob = max(accepted, key=lambda r: (r.seq, r.ts)).blob_id
    # Raw (athenaeum#3127). Right when the ancestor was written by write_blob in
    # _finish (never cleaned): its stored bytes are the text as accepted. NOT right
    # for an ancestor recorded at raise acceptance, whose id is `git hash-object
    # -- <path>` and so the clean-filtered form: under a clean/smudge filter this
    # read returns the cleaned bytes. With no filter configured, both are the same.
    # Passing `path` instead would smudge write_blob's plaintext, and the successor
    # form copies either kind of id, so the row's kind alone cannot pick the mode;
    # the fix is tracked as a follow-up rather than guessed here.
    base_bytes = read_blob(Path(target_root), base_blob) if base_blob else None
    if base_bytes is None:
        return hold(
            "ancestor_unreadable",
            "The last accepted version of this copy cannot be read back, so the edited "
            "copy cannot be merged with the new version. The copy stays as it is.",
        )
    page_type = page.get("type")
    spec = (specs or {}).get(page_type) if isinstance(page_type, str) else None
    result = merge_page(
        _strip_raised_from(base_bytes.decode("utf-8", errors="replace")),
        _strip_raised_from(copy.text),
        remote,
        spec,
    )
    if result.text is None or result.contested or result.held:
        units = tuple(result.contested) + tuple(h.unit for h in result.held)
        return hold(
            "edit_contested",
            "This copy was edited here and the new version changes the same parts "
            f"({', '.join(units) or 'the page'}). The published version stays; "
            "a person decides.",
            units,
        )
    new_text = _with_raised_from(result.text, new_block)
    return _finish(
        copy, new_text, msg, uid, target_store_id=target_store_id,
        target_root=Path(target_root), cache_dir=cache_dir, location=location, now=now,
        accepted_text=ancestor_text,
    )


def _ordered(meta: dict[str, Any], before: dict[str, Any]) -> dict[str, Any]:
    """*meta* with the keys the copy already had first, in their order."""
    out = {k: meta[k] for k in before if k in meta}
    out.update({k: v for k, v in meta.items() if k not in out})
    return out


def _finish(
    copy: _Copy,
    new_text: str,
    msg: RaiseMessage,
    uid: str,
    *,
    target_store_id: str,
    target_root: Path,
    cache_dir: Path,
    location: str,
    now: datetime | None,
    accepted_text: str | None = None,
    accepted_blob: str | None = None,
) -> Outcome:
    """Write the copy, commit it and record the supersede ``accepted``.

    The ledger names the **accepted version** by blob id: *accepted_text* written
    (and pinned, so no ``gc`` prunes it) as a blob, or *accepted_blob* when the
    payload did not change (the successor form).
    """
    assert msg.raise_id is not None
    if accepted_text is not None:
        accepted_blob = write_blob(
            Path(target_root),
            accepted_text.encode("utf-8"),
            f"{ANCESTOR_REF_PREFIX}/{msg.raise_id}/{msg.message_id}",
        )
    assert accepted_blob is not None
    commit = _write_and_commit(
        Path(target_root), copy, new_text, msg, uid, "Apply a supersede to a raised copy"
    )
    row = _record(
        cache_dir, msg, target_store_id, location,
        disposition=DISPOSITION_ACCEPTED, payload_hash=msg.content_hash,
        blob_id=accepted_blob, now=now,
    )
    return Outcome(APPLIED, None, None, row, uid, commit)


# -- the successor form ------------------------------------------------------


def retract_for_rejected_successor(msg: RaiseMessage) -> RaiseMessage:
    """The retract a successor-form supersede becomes when the successor's raise was
    rejected: same ids, reason ``superseded_at_source`` (§7.4)."""
    return RaiseMessage(
        kind=KIND_RETRACT,
        message_id=msg.message_id,
        origin=msg.origin,
        target=msg.target,
        proposer=dict(msg.proposer),
        sent_at=msg.sent_at,
        raise_id=msg.raise_id,
        seq=msg.seq,
        via=msg.via,
        reason=RETRACT_REASON,
    )


def _apply_successor(
    msg: RaiseMessage,
    copy: _Copy,
    uid: str,
    *,
    target_store_id: str,
    target_root: Path,
    cache_dir: Path,
    location: str,
    hold: Callable[..., Outcome],
    stop: Callable[..., Outcome],
    apply_retract: Callable[[RaiseMessage], Outcome] | None,
    now: datetime | None,
) -> Outcome:
    assert msg.successor is not None and msg.seq is not None
    successor_uid = derived_uid(target_store_id, msg.successor)
    other = _locate(target_root, successor_uid, msg.successor)
    if other is None:
        if is_rejected(cache_dir, msg.successor):
            retract = retract_for_rejected_successor(msg)
            if apply_retract is not None:
                return apply_retract(retract)
            return hold(
                RETRACT_REASON,
                "The page that replaced the source of this copy was rejected here, so "
                "the supersede is a retract (superseded at source). Applying a retract "
                "is not automatic yet; the copy stays as it is until a person decides.",
            )
        if _accepted_rows(cache_dir, msg.successor):
            return stop("copy_removed")  # the successor copy was removed here
        return Outcome(WAITING, None, None, None, uid)  # it waits with the successor's raise
    if other == "folded":
        return hold("copy_folded", "The successor copy was folded into another page.")
    if isinstance(other, str):
        return hold("uid_taken", "The page at the successor's uid is not its copy.")
    slug = slugify(str(other.meta.get("name", "")))
    if not slug:
        return hold("page_invalid", "The successor copy has no name to point at.")
    meta = dict(copy.meta)
    meta["superseded_by"] = slug
    meta["raised_from"] = _raised_from_after(copy, msg.seq, None)
    new_text = render_frontmatter(_ordered(meta, copy.meta)) + copy.body
    latest = max(_accepted_rows(cache_dir, msg.raise_id or ""), key=lambda r: (r.seq, r.ts))
    return _finish(
        copy, new_text, msg, uid, target_store_id=target_store_id,
        target_root=Path(target_root), cache_dir=cache_dir, location=location, now=now,
        accepted_blob=latest.blob_id,
    )
