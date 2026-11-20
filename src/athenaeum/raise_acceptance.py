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

Out of scope here: rejection mechanics, dropping later messages and the detail
of the taken-uid hold (S7b), the ``raise:`` declaration block and acceptance
without asking (S8), and the filing checks after acceptance (S21).

Layering: L3 (service). Imports :mod:`athenaeum.raise_acceptance_ledger` (L3),
:mod:`athenaeum.store_decisions`, :mod:`athenaeum.store_intake`,
:mod:`athenaeum.store_rights`, :mod:`athenaeum.store_access`,
:mod:`athenaeum.raise_messages`, :mod:`athenaeum.raise_projection`,
:mod:`athenaeum.raise_provenance` (L2) and :mod:`athenaeum.models` (L1).
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
    AcceptanceRow,
    Adjustment,
    disposition_of,
    is_rejected,
    record_acceptance,
)
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
from athenaeum.store_access import (
    StoreDeclaration,
    StoreDeclarationInvalid,
    load_store_declaration,
)
from athenaeum.store_decisions import file_decision, load_queue, resolve_decision
from athenaeum.store_intake import intake_accepts
from athenaeum.store_rights import StaticDirectory, resolve_rights

__all__ = [
    "DECISION_KIND_RAISE",
    "DECISION_KIND_HELD",
    "COMMIT_LINE_KEY",
    "Outcome",
    "RaiseAcceptRefused",
    "accept_raise",
    "commit_line",
    "run_checks",
    "submit_raise",
]

#: The decision kind a raise that passed the checks is shown under.
DECISION_KIND_RAISE = "raise"
#: The decision kind for a message that failed a check, or whose derived uid is
#: taken. The detail of that decision is raise S7b.
DECISION_KIND_HELD = "raise_held"

#: The commit trailer key (§4.4).
COMMIT_LINE_KEY = "Athenaeum-Raise"

#: Outcome statuses.
ASKED = "asked"
HELD = "held"
DUPLICATE = "duplicate"
ACCEPTED = "accepted"

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
            kind=KIND_RAISE,
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
) -> dict:
    detail: dict[str, Any] = {"reason": reason}
    if msg is not None:
        detail.update(raise_id=msg.raise_id, message_id=msg.message_id, seq=msg.seq)
    return file_decision(
        knowledge_root,
        store_id=target_store_id,
        question=f"A raised message could not be applied by rule (reason: {reason}).",
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
) -> Outcome:
    """Run the rule checks on a waiting ``raise`` message and file the decision.

    A pass becomes a ``raise`` decision in the target's queue showing the
    payload (the ledger records the message ``held`` meanwhile). A failure is
    recorded with its reason code and, unless it is a duplicate, raised as a
    decision. Nothing is written to the store's pages.
    """
    parsed = parse_message(record)
    if isinstance(parsed, Fallthrough) or parsed.kind != KIND_RAISE:
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
            mounts=mounts, asserter=asserter,
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
        detail={"message": _message_dict(msg), "location": location},
    )
    row = _record(
        cache_dir, msg, target_store_id, location,
        disposition=DISPOSITION_HELD, payload_hash=msg.content_hash,
    )
    return Outcome(ASKED, None, decision, row, uid)


def _message_dict(msg: RaiseMessage) -> dict[str, Any]:
    return json.loads(render_message(msg))


# -- step 2: accept ----------------------------------------------------------


def _git(root: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def _repo_of(root: Path) -> Path | None:
    try:
        return Path(_git(root, "rev-parse", "--show-toplevel"))
    except (subprocess.CalledProcessError, OSError):
        return None


def _blob_id(path: Path) -> str:
    return _git(path.parent, "hash-object", "--", str(path))


def _create_exclusive(path: Path, text: str) -> None:
    """Create *path* and fail rather than replace an existing file."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def _commit_copy(root: Path, path: Path, message: str) -> str | None:
    """Commit only *path*; ``None`` when the store is not in a git repository."""
    repo = _repo_of(root)
    if repo is None:
        return None
    _git(repo, "add", "--", str(path))
    _git(repo, "commit", "-m", message, "--only", "--", str(path))
    return _git(repo, "rev-parse", "HEAD")


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

    Raises :class:`RaiseAcceptRefused` (nothing written) when the decision is
    not a pending ``raise`` decision, or when the type and tags that would be
    filed are refused by the store's ``intake`` rules (reason
    ``store_refuses``). When a rule check no longer passes or the derived uid is
    taken, nothing is written: the message is held and a decision is raised.
    """
    rec = _find_decision(knowledge_root, target_store_id, decision_id)
    if rec.get("kind") != DECISION_KIND_RAISE or rec.get("disposition") != "pending":
        raise RaiseAcceptRefused("not_pending", f"{decision_id} is not a pending raise decision")
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
    if reason is not None:
        row = _record(
            cache_dir, msg, target_store_id, location,
            disposition=DISPOSITION_HELD, reason=reason,
        )
        held = _file_held(
            knowledge_root, target_store_id, reason=reason, msg=msg,
            mounts=mounts, asserter=asserter,
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
            row = _record(
                cache_dir, msg, target_store_id, location,
                disposition=DISPOSITION_HELD, reason="uid_taken",
            )
            held = _file_held(
                knowledge_root, target_store_id, reason="uid_taken", msg=msg,
                mounts=mounts, asserter=asserter,
            )
            return Outcome(HELD, "uid_taken", held, row, uid)
        try:
            commit = _commit_copy(
                Path(target_root),
                path,
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
        blob_id=_blob_id(path),
        now=now,
    )
    resolve_decision(
        knowledge_root, store_id=target_store_id, decision_id=decision_id, disposition="accepted"
    )
    return Outcome(ACCEPTED, None, rec, row, uid, commit)
