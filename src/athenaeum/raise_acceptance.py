# SPDX-License-Identifier: Apache-2.0
"""Accept a raised page, asking every time (athenaeum#2939).

``docs/design/federation-raise-retract.md`` §4.4 (acceptance), §5.1 (what the
copy carries), §14 slice S7a. A raise is a ``raise`` message waiting in the
target store's raw intake. This module is what the target's compile role does
with it, in two steps:

1. :func:`submit_raise` runs the **rule checks** before anyone is asked (the
   message parses, its ``target`` is this store, the proposer holds propose or
   compile in the target's declaration now, the payload has no link, ``related``
   row or ``parent``, the page has ``type`` and ``name``, the ``content_hash``
   matches, the ``raise_id`` is not rejected), then the store's ``intake`` rules
   on the payload's type and tags. A message that fails is recorded in the
   acceptance ledger with its reason code (``held``) and, unless it is a
   duplicate, raised as a decision. A message that passes is recorded ``held``
   and becomes a ``raise`` decision in the store's own queue, showing the
   payload. In this slice every raise that passes becomes a decision; accepting
   without asking is raise S8.
2. :func:`accept_raise` is the person's answer *accept*, optionally adjusting
   ``type`` and ``tags`` to something the store's ``intake`` rules accept. It
   re-checks (the proposer's right is read at the time the compile role acts),
   creates the copy at the uid derived from the target ``store_id`` and the
   ``raise_id`` with the body unchanged and a ``raised_from`` block, commits it
   with the line ``Athenaeum-Raise: <raise_id> seq=<n> accept``, and records the
   acceptance and any adjustment in the ledger.

**A page is never overwritten.** The copy is created with an exclusive create.
When the derived uid is already taken by a page with a different ``raise_id``,
or none, nothing is written: the raise is held and a decision is raised. When
the page at the uid already carries this ``raise_id`` the message is a
duplicate (one ``raise_id`` gives one uid and so one page, however often it is
delivered).

**The compiling member's own relevance policy is never applied** to a waiting
message (athenaeum#2625 §4.5). Only the store's declared ``intake`` rules are
(:func:`athenaeum.store_intake.intake_accepts`); nothing here reads a relevance
policy.

**No precedence.** Nothing here ranks a raised copy against any other source;
no code may rely on it winning or losing a conflict (§5.1).

**The filing checks run after acceptance, never before** (S21,
:mod:`athenaeum.raise_filing_checks`): once the copy is committed and its
acceptance recorded, :func:`accept_raise` runs them on the copy and a failure
opens a ``filing-check`` item naming it. They are never a reason to hold, ask
about or reject a raise, and the outcome of an acceptance never depends on them.

**Rejection and holds (S7b, athenaeum#3002).** :func:`reject_raise` is the
person's answer *reject* to a ``raise`` or ``raise_held`` decision, with a reason
code from :data:`REJECT_REASONS` (there is no free text). The ledger records the
``raise_id`` as ``rejected`` and the message is removed from the store's raw
intake (:func:`consume_raw_file`: a file goes once the ledger gives every message
in it its final disposition, §6.2). Every later message for a rejected
``raise_id``, whatever its ``seq`` or kind, is **dropped by rule**: recorded
``rejected`` with reason ``already_rejected`` and no decision raised. A raise that
is merely *held* is not rejected, so its later messages are processed. A
``raise_held`` decision names the uid, the reason and the waiting message; it is
answered reject, or accept once a ``uid_taken`` uid is free
(:func:`answer_decision`). Each message ends in exactly one recorded
disposition.

Out of scope here: the ``raise:`` declaration block and acceptance without
asking (S8).

Layering: L3 (service). Imports :mod:`athenaeum.raise_acceptance_ledger` (L3),
:mod:`athenaeum.store_decisions`, :mod:`athenaeum.store_intake`,
:mod:`athenaeum.store_rights`, :mod:`athenaeum.store_access`,
:mod:`athenaeum.raise_messages`, :mod:`athenaeum.raise_projection`,
:mod:`athenaeum.raise_provenance` (L2), :mod:`athenaeum.raise_filing_checks`
(L3), :mod:`athenaeum.models` and :mod:`athenaeum.store` (L1).
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from athenaeum.models import (
    asserter_identity_key,
    parse_frontmatter,
    render_frontmatter,
    slugify,
)
from athenaeum.raise_acceptance_ledger import (
    DISPOSITION_ACCEPTED,
    DISPOSITION_HELD,
    DISPOSITION_REJECTED,
    REASON_CODES,
    AcceptanceRow,
    Adjustment,
    disposition_of,
    is_rejected,
    record_acceptance,
)
from athenaeum.raise_filing_checks import RaiseFilingResult, run_after_publish
from athenaeum.raise_messages import (
    KIND_RAISE,
    Fallthrough,
    RaiseMessage,
    parse_message,
    render_message,
)
from athenaeum.raise_projection import (
    PROJECTED_KEYS,
    Projection,
    derived_uid,
    has_link,
    payload_hash,
    render,
)
from athenaeum.raise_provenance import (
    RAISED_FROM_VERSION,
    RaisedFromError,
    parse_raised_from,
    raise_source_ref,
)
from athenaeum.store import blob_id, commit_paths, repo_head
from athenaeum.store_access import (
    StoreDeclaration,
    StoreDeclarationInvalid,
    load_store_declaration,
)
from athenaeum.store_decisions import (
    ANSWER_ACCEPT,
    ANSWER_REJECT,
    answers_for,
    file_decision,
    load_queue,
    resolve_decision,
)
from athenaeum.store_intake import intake_accepts
from athenaeum.store_rights import StaticDirectory, resolve_rights

__all__ = [
    "DECISION_KIND_RAISE",
    "DECISION_KIND_HELD",
    "COMMIT_LINE_KEY",
    "REJECT_REASONS",
    "Outcome",
    "RaiseAcceptRefused",
    "accept_raise",
    "answer_decision",
    "commit_line",
    "consume_raw_file",
    "reject_raise",
    "run_checks",
    "submit_raise",
]

#: The decision kind a raise that passed the checks is shown under.
DECISION_KIND_RAISE = "raise"
#: The decision kind for a message that failed a check, or whose derived uid is
#: taken. It names the uid, the reason and the waiting message (raise S7b).
DECISION_KIND_HELD = "raise_held"

#: The commit trailer key (§4.4).
COMMIT_LINE_KEY = "Athenaeum-Raise"

#: Outcome statuses.
ASKED = "asked"
HELD = "held"
DUPLICATE = "duplicate"
ACCEPTED = "accepted"
REJECTED = "rejected"
#: A message dropped because its ``raise_id`` is rejected (recorded, no decision).
DROPPED = "dropped"

#: The fixed list of reason codes a person may reject a raise with: the person's
#: own ruling (``declined``), the store's ``intake`` rules refusing the type and
#: tags (``store_refuses``), and one code per rule check or hold the person
#: agrees with (§4.4). ``duplicate`` and ``already_rejected`` are recorded by
#: rule and are not a person's reason.
REJECT_REASONS = frozenset(
    {
        "declined",
        "store_refuses",
        "uid_taken",
        "unparseable",
        "target_mismatch",
        "proposer_lacks_right",
        "payload_has_link",
        "page_invalid",
        "hash_mismatch",
    }
)
assert REJECT_REASONS <= REASON_CODES

_RIGHTS_TO_RAISE = ("propose", "compile")


class RaiseAcceptRefused(ValueError):
    """An accept that cannot proceed; nothing was written."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


@dataclass(frozen=True)
class Outcome:
    """What a call did. ``reason`` is a ledger reason code when the status is
    ``held`` or ``duplicate``."""

    status: str
    reason: str | None = None
    decision: dict | None = None
    row: AcceptanceRow | None = None
    uid: str | None = None
    commit: str | None = None
    #: The filing checks run on the copy after it was published (raise S21); never
    #: a reason the status differs. ``None`` when no copy was published.
    filing: RaiseFilingResult | None = None


def commit_line(raise_id: str, seq: int) -> str:
    """The one commit line of an acceptance: ids only (§4.4)."""
    return f"{COMMIT_LINE_KEY}: {raise_id} seq={seq} accept"


# -- the rule checks ---------------------------------------------------------


def _proposer_rights(
    declaration: StoreDeclaration | StoreDeclarationInvalid | None,
    proposer: dict[str, str],
) -> frozenset[str]:
    if not isinstance(declaration, StoreDeclaration):
        return frozenset()  # no declaration, or an invalid one: fail closed
    return resolve_rights(declaration, proposer)


def _proposer_name(
    declaration: StoreDeclaration, proposer: dict[str, str]
) -> str | dict[str, str]:
    """How the copy names the proposer (operator ruling 1): a person if the
    store's access list names people, a group if it names groups."""
    identity = asserter_identity_key(dict(proposer))
    directory = StaticDirectory(declaration)
    group_name: str | None = None
    for role in declaration.roles:
        if not set(role.rights) & set(_RIGHTS_TO_RAISE):
            continue
        for member in role.members:
            if member.kind == "principal" and member.principal is not None:
                if identity and member.principal.key == identity:
                    return {"iss": proposer["iss"], "sub": proposer["sub"]}
            elif member.kind == "group" and member.group is not None:
                found = directory.members(member.group)
                if found is not None and identity in found and group_name is None:
                    group_name = member.group
    if group_name is not None:
        return f"group:{group_name}"
    return {"iss": proposer["iss"], "sub": proposer["sub"]}


def _payload_text(page: dict[str, Any], body: str) -> str:
    return render(Projection(meta=dict(page), body=body))


def _page_problem(msg: RaiseMessage) -> str | None:
    page = msg.page or {}
    for key in ("type", "name"):
        value = page.get(key)
        if not isinstance(value, str) or not value.strip():
            return f"the page has no {key}"
    if msg.via is not None and msg.via != "proposer" and not msg.via.startswith("review:"):
        return "a raised copy is raised by a proposer or a review"
    return None


def _check_message(
    msg: RaiseMessage,
    *,
    target_store_id: str,
    declaration: StoreDeclaration | StoreDeclarationInvalid | None,
    cache_dir: Path,
) -> str | None:
    """The reason code of the first rule check *msg* fails, in the order of §4.4."""
    if msg.target != target_store_id:
        return "target_mismatch"
    if not set(_proposer_rights(declaration, msg.proposer)) & set(_RIGHTS_TO_RAISE):
        return "proposer_lacks_right"
    page = msg.page or {}
    if "related" in page or "parent" in page or has_link(msg.body or ""):
        return "payload_has_link"
    if _page_problem(msg) is not None:
        return "page_invalid"
    if _payload_hash_of(msg) != msg.content_hash:
        return "hash_mismatch"
    assert msg.raise_id is not None
    if is_rejected(cache_dir, msg.raise_id):
        return "already_rejected"
    return None


def _payload_hash_of(msg: RaiseMessage) -> str:
    return payload_hash(_payload_text(msg.page or {}, msg.body or ""))


def run_checks(
    record: object,
    *,
    target_store_id: str,
    target_root: Path,
    cache_dir: Path,
) -> tuple[RaiseMessage | None, str | None]:
    """Run the rule checks of §4.4 on one decoded record.

    Returns ``(message, reason)``. ``reason`` is the code of the first check
    that fails, or ``None`` when every check passes. ``message`` is ``None``
    when the record does not parse (``unparseable``) or is not a ``raise``.
    Never raises on input.
    """
    parsed = parse_message(record)
    if isinstance(parsed, Fallthrough) or parsed.kind != KIND_RAISE:
        return None, "unparseable"
    declaration = load_store_declaration(target_root)
    return parsed, _check_message(
        parsed, target_store_id=target_store_id, declaration=declaration, cache_dir=cache_dir
    )


# -- the page at the derived uid --------------------------------------------


def _pages_at_uid(root: Path, uid: str) -> list[Path]:
    """Every page of the store whose file stem is the uid or ``<uid>-<slug>``."""
    found: list[Path] = []
    try:
        for hit in sorted(root.rglob(f"{uid}*.md")):
            parts = hit.relative_to(root).parts
            if "_store" in parts or ".git" in parts or not hit.is_file():
                continue
            if hit.stem == uid or hit.stem.startswith(uid + "-"):
                found.append(hit)
    except OSError:
        return found
    return found


def _raise_id_of(path: Path) -> str | None:
    """The ``raise_id`` of the page at *path*, or ``None`` (no block, or a bad one)."""
    try:
        meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        block = parse_raised_from(meta) if isinstance(meta, dict) else None
    except (OSError, UnicodeDecodeError, ValueError, RaisedFromError):
        return None
    return block.raise_id if block is not None else None


def _uid_state(root: Path, uid: str, raise_id: str) -> str:
    """``free``, ``same`` (a copy of this raise exists) or ``taken`` (another page)."""
    pages = _pages_at_uid(root, uid)
    if not pages:
        return "free"
    return "same" if all(_raise_id_of(p) == raise_id for p in pages) else "taken"


# -- ledger and queue helpers -----------------------------------------------


def _record(cache_dir: Path, msg: RaiseMessage, target: str, location: str, **kw: Any) -> Any:
    """Append a ledger row; ``None`` when the message's own ids cannot form a row
    (for example an origin that is not a store id)."""
    assert msg.raise_id is not None and msg.seq is not None
    try:
        return record_acceptance(
            cache_dir,
            kind=msg.kind,
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


def _show(msg: RaiseMessage, intake_note: str) -> str:
    """The question text of a ``raise`` decision: the payload, as it would be filed."""
    page = msg.page or {}
    shown = _payload_text(page, msg.body or "")
    return (
        "A page was raised to this store. Accept it as it is, accept it with a different "
        "type or tags that this store's intake rules take, or reject it.\n"
        f"{intake_note}\n\n{shown}"
    )


def _file_held(
    knowledge_root: Path,
    target_store_id: str,
    *,
    reason: str,
    msg: RaiseMessage | None,
    mounts: Any,
    asserter: Any,
    uid: str | None = None,
    location: str = "",
    source: Path | str | None = None,
) -> dict:
    """File a ``raise_held`` decision: the reason, and (when the message parsed)
    the uid, the ids and the waiting message itself, so the decision can be
    answered reject, or accept once a taken uid is free."""
    detail: dict[str, Any] = {"reason": reason}
    if msg is not None:
        detail.update(raise_id=msg.raise_id, message_id=msg.message_id, seq=msg.seq)
        detail["message"] = _message_dict(msg)
        detail["location"] = location
        if uid is not None:
            detail["uid"] = uid
        if source is not None:
            detail["source"] = str(source)
    question = f"A raised message could not be applied by rule (reason: {reason})."
    if reason == "uid_taken" and uid is not None:
        question = (
            f"A raised page cannot be filed: its uid {uid} belongs to another page, which is "
            "never overwritten (reason: uid_taken). Reject the raise, or accept it once the "
            "uid is free."
        )
    return file_decision(
        knowledge_root,
        store_id=target_store_id,
        question=question,
        kind=DECISION_KIND_HELD,
        mounts=mounts,
        asserter=asserter,
        detail=detail,
    )


def _intake_note(declaration: Any, msg: RaiseMessage) -> tuple[bool, str]:
    page = msg.page or {}
    verdict = intake_accepts(declaration, page.get("type"), tuple(page.get("tags") or ()))
    if verdict.accepted:
        return True, "This store's intake rules accept the type and tags as sent."
    return False, (
        "This store's intake rules refuse the type and tags as sent "
        f"({verdict.reason}); adjust them to something the rules accept, or reject."
    )


# -- step 1: submit ----------------------------------------------------------


def submit_raise(
    knowledge_root: Path,
    record: object,
    *,
    target_store_id: str,
    target_root: Path,
    cache_dir: Path,
    location: str,
    mounts: Any = None,
    asserter: Any = None,
    source: Path | None = None,
) -> Outcome:
    """Run the rule checks on a waiting ``raise`` message and file the decision.

    A pass becomes a ``raise`` decision in the target's queue showing the
    payload (the ledger records the message ``held`` meanwhile). A failure is
    recorded with its reason code and, unless it is a duplicate, raised as a
    decision. Nothing is written to the store's pages.

    A message (of any kind) for a ``raise_id`` that is already rejected is
    dropped by rule: recorded, no decision (status ``dropped``). *source* is the
    raw intake file holding the message; it is kept on the decision so a later
    rejection can remove it (:func:`consume_raw_file`), and a dropped message's
    file is removed when every message in it has its final disposition.
    """
    parsed = parse_message(record)
    if isinstance(parsed, Fallthrough) or (
        parsed.kind != KIND_RAISE
        and not (parsed.raise_id is not None and is_rejected(cache_dir, parsed.raise_id))
    ):
        # No ids to record against: it goes up the ladder as a decision alone.
        decision = _file_held(
            knowledge_root, target_store_id, reason="unparseable", msg=None,
            mounts=mounts, asserter=asserter,
        )
        return Outcome(HELD, "unparseable", decision)
    msg = parsed
    assert msg.raise_id is not None
    uid = derived_uid(target_store_id, msg.raise_id)

    # A re-delivery: the message was already given a disposition, or its copy exists.
    earlier = disposition_of(cache_dir, msg.message_id)
    if earlier is not None or _uid_state(target_root, uid, msg.raise_id) == "same":
        row = _record(
            cache_dir, msg, target_store_id, location,
            disposition=DISPOSITION_HELD, reason="duplicate",
        )
        return Outcome(DUPLICATE, "duplicate", None, row, uid)

    # The drop rule (§4.4): a later message for a rejected raise_id, any seq and
    # any kind, is recorded and dropped; no decision is raised.
    if is_rejected(cache_dir, msg.raise_id):
        row = _record(
            cache_dir, msg, target_store_id, location,
            disposition=DISPOSITION_REJECTED, reason="already_rejected",
        )
        if source is not None:
            consume_raw_file(source, cache_dir)
        return Outcome(DROPPED, "already_rejected", None, row, uid)

    declaration = load_store_declaration(target_root)
    reason = _check_message(
        msg, target_store_id=target_store_id, declaration=declaration, cache_dir=cache_dir
    )
    if reason is None and _uid_state(target_root, uid, msg.raise_id) == "taken":
        reason = "uid_taken"
    if reason is not None:
        row = _record(
            cache_dir, msg, target_store_id, location,
            disposition=DISPOSITION_HELD, reason=reason,
        )
        decision = _file_held(
            knowledge_root, target_store_id, reason=reason, msg=msg,
            mounts=mounts, asserter=asserter, uid=uid, location=location, source=source,
        )
        return Outcome(HELD, reason, decision, row, uid)

    _, note = _intake_note(declaration, msg)
    decision = file_decision(
        knowledge_root,
        store_id=target_store_id,
        question=_show(msg, note),
        kind=DECISION_KIND_RAISE,
        mounts=mounts,
        asserter=asserter,
        detail={
            "message": _message_dict(msg),
            "location": location,
            **({"source": str(source)} if source is not None else {}),
        },
    )
    row = _record(
        cache_dir, msg, target_store_id, location,
        disposition=DISPOSITION_HELD, payload_hash=msg.content_hash,
    )
    return Outcome(ASKED, None, decision, row, uid)


def _message_dict(msg: RaiseMessage) -> dict[str, Any]:
    return json.loads(render_message(msg))


# -- step 2: accept ----------------------------------------------------------


def _create_exclusive(path: Path, text: str) -> None:
    """Create *path* and fail rather than replace an existing file."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def _find_decision(knowledge_root: Path, store_id: str, decision_id: str) -> dict:
    for rec in load_queue(knowledge_root, store_id):
        if rec.get("id") == decision_id:
            return rec
    raise RaiseAcceptRefused("no_decision", f"no decision {decision_id} in {store_id}'s queue")


def accept_raise(
    knowledge_root: Path,
    *,
    target_store_id: str,
    target_root: Path,
    cache_dir: Path,
    decision_id: str,
    type: str | None = None,
    tags: list[str] | None = None,
    mounts: Any = None,
    asserter: Any = None,
    now: datetime | None = None,
) -> Outcome:
    """Accept the waiting raise named by *decision_id*, optionally adjusting
    ``type`` and ``tags``.

    Also answers a ``raise_held`` decision whose reason is ``uid_taken``: accept
    once the uid is free. While the uid is still taken, or a rule check still
    fails, :class:`RaiseAcceptRefused` is raised (reason ``uid_taken`` or the
    check's code), nothing is written and the decision stays pending.

    Raises :class:`RaiseAcceptRefused` (nothing written) when the decision is
    not a pending ``raise`` decision, or when the type and tags that would be
    filed are refused by the store's ``intake`` rules (reason
    ``store_refuses``). When a rule check no longer passes or the derived uid is
    taken, nothing is written: the message is held and a decision is raised.
    """
    rec = _find_decision(knowledge_root, target_store_id, decision_id)
    is_held_kind = rec.get("kind") == DECISION_KIND_HELD
    if (
        rec.get("kind") not in (DECISION_KIND_RAISE, DECISION_KIND_HELD)
        or rec.get("disposition") != "pending"
    ):
        raise RaiseAcceptRefused("not_pending", f"{decision_id} is not a pending raise decision")
    if is_held_kind and ANSWER_ACCEPT not in answers_for(rec):
        raise RaiseAcceptRefused(
            "not_acceptable",
            f"{decision_id} was held for a reason that only a rejection settles",
        )
    detail = rec.get("detail") or {}
    parsed = parse_message(detail.get("message"))
    if isinstance(parsed, Fallthrough) or parsed.kind != KIND_RAISE:
        raise RaiseAcceptRefused("unparseable", f"{decision_id} carries no parseable raise")
    msg = parsed
    location = str(detail.get("location", ""))
    assert msg.raise_id is not None and msg.seq is not None and msg.page is not None
    current = disposition_of(cache_dir, msg.message_id)
    if current is not None and current.disposition == DISPOSITION_ACCEPTED:
        raise RaiseAcceptRefused("already_accepted", f"{msg.message_id} is already accepted")

    declaration = load_store_declaration(target_root)
    uid = derived_uid(target_store_id, msg.raise_id)

    # The right is read at the time the compile role acts (§4.4).
    reason = _check_message(
        msg, target_store_id=target_store_id, declaration=declaration, cache_dir=cache_dir
    )
    if reason is None and _uid_state(target_root, uid, msg.raise_id) == "taken":
        reason = "uid_taken"
    if reason is not None and is_held_kind:
        # Already held and asked about: nothing new to record or raise.
        raise RaiseAcceptRefused(reason, f"{decision_id} cannot be accepted yet ({reason})")
    if reason is not None:
        row = _record(
            cache_dir, msg, target_store_id, location,
            disposition=DISPOSITION_HELD, reason=reason,
        )
        held = _file_held(
            knowledge_root, target_store_id, reason=reason, msg=msg,
            mounts=mounts, asserter=asserter, uid=uid, location=location,
            source=detail.get("source"),
        )
        resolve_decision(
            knowledge_root, store_id=target_store_id, decision_id=decision_id, disposition="held"
        )
        return Outcome(HELD, reason, held, row, uid)
    assert isinstance(declaration, StoreDeclaration)

    # The adjustment: only what differs from the payload is an adjustment.
    final_page = dict(msg.page)
    adjusted_type: str | None = None
    adjusted_tags: tuple[str, ...] | None = None
    if type is not None and type != final_page.get("type"):
        if not isinstance(type, str) or not type.strip():
            raise RaiseAcceptRefused("page_invalid", "an adjusted type must be a non-empty string")
        adjusted_type = type
        final_page["type"] = type
    if tags is not None and list(tags) != list(final_page.get("tags") or []):
        adjusted_tags = tuple(tags)
        final_page["tags"] = list(tags)
    adjustment = (
        Adjustment(type=adjusted_type, tags=adjusted_tags)
        if adjusted_type is not None or adjusted_tags is not None
        else None
    )
    verdict = intake_accepts(
        declaration, final_page.get("type"), tuple(final_page.get("tags") or ())
    )
    if not verdict.accepted:
        raise RaiseAcceptRefused(
            "store_refuses",
            "this store's intake rules refuse that type and tags "
            f"({verdict.reason}); adjust them to something the rules accept, or reject",
        )

    body = msg.body or ""
    copy_hash = payload_hash(_payload_text(final_page, body))
    raised_on = date.fromisoformat(msg.sent_at[:10])
    meta: dict[str, Any] = {"uid": uid}
    for key in PROJECTED_KEYS:
        if key in final_page:
            meta[key] = final_page[key]
    meta["sources"] = [raise_source_ref(msg.raise_id)]
    meta["raised_from"] = {
        "v": RAISED_FROM_VERSION,
        "raise_id": msg.raise_id,
        "origin": msg.origin,
        "proposer": _proposer_name(declaration, msg.proposer),
        "via": msg.via,
        "content_hash": copy_hash,
        "raised_on": raised_on,
        "seq": msg.seq,
        "watch": "active",
    }
    text = render_frontmatter(meta) + body

    existing = _pages_at_uid(Path(target_root), uid)
    commit: str | None = None
    if existing:
        # A copy of this very raise is already there (a crash after the commit,
        # before the ledger row): record it, write nothing.
        path = existing[0]
    else:
        slug = slugify(str(final_page["name"]))
        path = Path(target_root) / (f"{uid}-{slug}.md" if slug else f"{uid}.md")
        try:
            _create_exclusive(path, text)
        except FileExistsError:
            # Lost a race to another writer of the same path: never overwrite.
            if is_held_kind:
                raise RaiseAcceptRefused(
                    "uid_taken", f"{decision_id} cannot be accepted yet (uid_taken)"
                ) from None
            row = _record(
                cache_dir, msg, target_store_id, location,
                disposition=DISPOSITION_HELD, reason="uid_taken",
            )
            held = _file_held(
                knowledge_root, target_store_id, reason="uid_taken", msg=msg,
                mounts=mounts, asserter=asserter, uid=uid, location=location,
                source=detail.get("source"),
            )
            return Outcome(HELD, "uid_taken", held, row, uid)
        try:
            commit = commit_paths(
                Path(target_root),
                [path],
                f"Accept a raised page ({uid})\n\n{commit_line(msg.raise_id, msg.seq)}\n",
            )
        except (subprocess.CalledProcessError, OSError):
            path.unlink(missing_ok=True)  # we created it a moment ago; no half-accept
            raise
    row = record_acceptance(
        cache_dir,
        disposition=DISPOSITION_ACCEPTED,
        kind=KIND_RAISE,
        raise_id=msg.raise_id,
        message_id=msg.message_id,
        seq=msg.seq,
        origin=msg.origin,
        target=target_store_id,
        proposer=msg.proposer,
        location=location,
        payload_hash=msg.content_hash,
        adjustment=adjustment,
        blob_id=blob_id(path),
        now=now,
    )
    resolve_decision(
        knowledge_root, store_id=target_store_id, decision_id=decision_id, disposition="accepted"
    )
    # §6.2: a raw intake file goes once every message in it has its final disposition.
    if detail.get("source"):
        consume_raw_file(detail["source"], cache_dir)
    # The copy is published and its acceptance recorded; only now do the filing
    # checks run (S21). They never change this outcome.
    filing = run_after_publish(
        knowledge_root,
        target_store_id=target_store_id,
        target_root=Path(target_root),
        copy_path=path,
        commit=commit or repo_head(Path(target_root)),
        raise_id=msg.raise_id,
        created=True,  # also after a crash: the copy found at the uid is this raise's
        mounts=mounts,
        asserter=asserter,
    )
    return Outcome(ACCEPTED, None, rec, row, uid, commit, filing)


# -- step 2 (the other answer): reject ---------------------------------------


def consume_raw_file(path: Path | str, cache_dir: Path) -> bool:
    """Remove a raw intake file once every message in it has its final disposition.

    A file is written once and never edited (§6.1), so a message cannot be taken
    out of a file that carries others: the file goes when the ledger records
    ``accepted`` or ``rejected`` for every message in it (§6.2). A line that is
    not a parseable message with a final disposition keeps the file. Returns
    whether the file was removed; never raises on a missing or unreadable file.
    """
    target = Path(path)
    try:
        lines = [ln for ln in target.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except (OSError, UnicodeDecodeError):
        return False
    if not lines:
        return False
    for line in lines:
        try:
            parsed = parse_message(json.loads(line))
        except ValueError:
            return False
        if isinstance(parsed, Fallthrough):
            return False
        row = disposition_of(cache_dir, parsed.message_id)
        if row is None or row.disposition not in (DISPOSITION_ACCEPTED, DISPOSITION_REJECTED):
            return False
    try:
        target.unlink()
    except OSError:
        return False
    return True


def _waiting_message(rec: dict) -> tuple[RaiseMessage | None, dict]:
    detail = rec.get("detail") or {}
    parsed = parse_message(detail.get("message"))
    if isinstance(parsed, Fallthrough) or parsed.raise_id is None or parsed.seq is None:
        return None, detail
    return parsed, detail


def reject_raise(
    knowledge_root: Path,
    *,
    target_store_id: str,
    cache_dir: Path,
    decision_id: str,
    reason: str = "declined",
    now: datetime | None = None,
) -> Outcome:
    """Answer a pending ``raise`` or ``raise_held`` decision *reject*.

    *reason* is a code from :data:`REJECT_REASONS`. The ledger records the
    message ``rejected`` with it (so the ``raise_id`` is rejected, and every
    later message for it is dropped by rule), the decision is settled, and the
    message is removed from the store's raw intake (:func:`consume_raw_file`).
    Any other message of the same ``raise_id`` still waiting on a decision of
    its own is rejected with it, so each message ends in one disposition.

    Raises :class:`RaiseAcceptRefused` (nothing written) for a decision that is
    not pending, a *reason* outside the fixed list, or a message already accepted.
    """
    if reason not in REJECT_REASONS:
        raise RaiseAcceptRefused(
            "bad_reason", f"{reason!r} is not one of the fixed rejection reason codes"
        )
    rec = _find_decision(knowledge_root, target_store_id, decision_id)
    if (
        rec.get("kind") not in (DECISION_KIND_RAISE, DECISION_KIND_HELD)
        or rec.get("disposition") != "pending"
    ):
        raise RaiseAcceptRefused("not_pending", f"{decision_id} is not a pending raise decision")
    msg, detail = _waiting_message(rec)
    if msg is None:
        # A held message that never parsed has no ids to record against.
        resolve_decision(
            knowledge_root, store_id=target_store_id, decision_id=decision_id,
            disposition="rejected",
        )
        return Outcome(REJECTED, reason)
    current = disposition_of(cache_dir, msg.message_id)
    if current is not None and current.disposition == DISPOSITION_ACCEPTED:
        raise RaiseAcceptRefused("already_accepted", f"{msg.message_id} is already accepted")
    assert msg.raise_id is not None
    uid = derived_uid(target_store_id, msg.raise_id)
    row = _record(
        cache_dir, msg, target_store_id, str(detail.get("location", "")),
        disposition=DISPOSITION_REJECTED, reason=reason, now=now,
    )
    resolve_decision(
        knowledge_root, store_id=target_store_id, decision_id=decision_id, disposition="rejected"
    )
    sources = [detail.get("source")]
    # Other waiting messages of this raise_id are rejected with it.
    for other in load_queue(knowledge_root, target_store_id):
        if other.get("id") == decision_id or other.get("disposition") != "pending":
            continue
        if other.get("kind") not in (DECISION_KIND_RAISE, DECISION_KIND_HELD):
            continue
        omsg, odetail = _waiting_message(other)
        if omsg is None or omsg.raise_id != msg.raise_id:
            continue
        ocur = disposition_of(cache_dir, omsg.message_id)
        if ocur is None or ocur.disposition == DISPOSITION_HELD:
            _record(
                cache_dir, omsg, target_store_id, str(odetail.get("location", "")),
                disposition=DISPOSITION_REJECTED, reason=reason, now=now,
            )
        resolve_decision(
            knowledge_root, store_id=target_store_id, decision_id=str(other["id"]),
            disposition="rejected",
        )
        sources.append(odetail.get("source"))
    for src in dict.fromkeys(x for x in sources if x):
        consume_raw_file(src, cache_dir)
    return Outcome(REJECTED, reason, rec, row, uid)


def answer_decision(
    knowledge_root: Path,
    *,
    target_store_id: str,
    target_root: Path,
    cache_dir: Path,
    decision_id: str,
    answer: str,
    reason: str | None = None,
    type: str | None = None,
    tags: list[str] | None = None,
    mounts: Any = None,
    asserter: Any = None,
    now: datetime | None = None,
) -> Outcome:
    """Settle a ``raise`` or ``raise_held`` decision with the person's *answer*.

    ``reject`` takes a *reason* code (default ``declined``). ``accept`` is
    offered on a ``raise`` decision and on a ``raise_held`` decision whose reason
    is ``uid_taken`` (it goes through once the uid is free); *type* and *tags*
    adjust the copy as in :func:`accept_raise`. An answer the decision does not
    offer is refused (:class:`RaiseAcceptRefused`, reason ``unknown_answer``).
    """
    rec = _find_decision(knowledge_root, target_store_id, decision_id)
    if answer not in answers_for(rec):
        raise RaiseAcceptRefused(
            "unknown_answer", f"{decision_id} does not take the answer {answer!r}"
        )
    if answer == ANSWER_REJECT:
        return reject_raise(
            knowledge_root, target_store_id=target_store_id, cache_dir=cache_dir,
            decision_id=decision_id, reason=reason or "declined", now=now,
        )
    return accept_raise(
        knowledge_root, target_store_id=target_store_id, target_root=target_root,
        cache_dir=cache_dir, decision_id=decision_id, type=type, tags=tags,
        mounts=mounts, asserter=asserter, now=now,
    )
