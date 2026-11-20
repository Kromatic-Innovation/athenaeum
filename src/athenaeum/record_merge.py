# SPDX-License-Identifier: Apache-2.0
"""Pure record merge and take-one-side for derived files (athenaeum#2929, slice S4).

Slice S4 of the multi-writer sync design, ``docs/design/federation-multi-writer-sync.md``
section 5.3 (record files) and the ``derived`` row of section 5. It opens no
file, runs no git command and calls no model; both functions take text and
return text.

:func:`merge_records` merges two versions of a record file changed on both
sides. The file is read into records by its own parser, never as text; the
records are keyed by their own id, unioned, and written back through the
file's own renderer in a stable order (the record's ``order`` key, then its
id), so the result does not depend on which side is which. For one record:

* on one side only: kept;
* the same on both sides, or open on both: the remote record is kept (two open
  copies of one proposal that differ only in text are reported in
  ``resolvers`` as ``open-differs``, never held);
* resolved on one side, open on the other: the resolved record is taken;
* resolved on both sides with the same outcome: the remote record is kept;
* resolved on both sides with DIFFERENT outcomes: not settled by any rule. It is
  returned in ``held`` and the merged text carries the remote record, so the
  text still parses and never holds a conflict marker.

There is no common ancestor in this function (section 5.3 names none), so a
record removed on one side (archived, say) returns from the other side's copy.
A caller that must honour removals reconciles them before calling.

A file that cannot be read safely is held whole, with the remote text: a block
its parser would skip (a rewrite from the parsed list would delete it), or two
records with one id on one side.

:func:`merge_derived` never merges a regenerated file: it takes one side and
says the caller must regenerate it.

Parsers plug in through :class:`RecordCodec`, a parser and renderer pair keyed
by file kind in :data:`RECORD_CODECS`. Only ``pending-merges`` is registered
(it reads and writes through :mod:`athenaeum.pending_merges`); another record
file adds a codec and nothing else here changes.

Layering: L4, a sibling of :mod:`athenaeum.pending_merges`, whose parser and
renderer it calls. ``tests/test_record_merge.py`` pins that it imports no git
helper, store, provider or file API.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from athenaeum.pending_merges import (
    PendingMerge,
    parse_pending_merges_text,
    render_pending_merges,
    unparsed_pending_merge_block_count,
)

# Resolver names: which rule settled a record.
R_LOCAL_ONLY = "local-only"
R_REMOTE_ONLY = "remote-only"
R_IDENTICAL = "identical"  # the two sides carry the same record
R_OPEN_DIFFERS = "open-differs"  # open on both, text differs: remote kept
R_RESOLVED_WINS = "resolved-wins"  # resolved on one side, open on the other
R_SAME_OUTCOME = "same-outcome"  # resolved the same way on both sides
R_HELD = "held"

REASON_RESOLVED_DIFFERENTLY = "resolved-differently"
REASON_UNPARSEABLE = "unparseable"
REASON_DUPLICATE_ID = "duplicate-id"
REASON_UNKNOWN_KIND = "unknown-kind"

#: The file kind of the pending merges list.
KIND_PENDING_MERGES = "pending-merges"


@dataclass(frozen=True)
class RecordCodec:
    """How to read, key, judge and write one kind of record file.

    ``outcome`` returns ``None`` for an open record and a string naming the
    resolution for a resolved one. ``unparsed`` counts the blocks ``parse``
    would drop, so a rewrite cannot silently delete them.
    """

    parse: Callable[[str], Sequence[Any]]
    render: Callable[[Sequence[Any]], str]
    key: Callable[[Any], str]
    outcome: Callable[[Any], str | None]
    order: Callable[[Any], str]
    unparsed: Callable[[str], int] = lambda _text: 0


def _pending_outcome(record: PendingMerge) -> str | None:
    if not record.resolved:
        return None
    return record.decision or "resolved"


RECORD_CODECS: Mapping[str, RecordCodec] = {
    KIND_PENDING_MERGES: RecordCodec(
        parse=parse_pending_merges_text,
        render=render_pending_merges,
        key=lambda r: r.id,
        outcome=_pending_outcome,
        order=lambda r: r.created_at,
        unparsed=unparsed_pending_merge_block_count,
    ),
}


@dataclass(frozen=True)
class HeldRecord:
    """A record (or, with ``key == ""``, a whole file) that waits for a person."""

    key: str
    reason: str


@dataclass(frozen=True)
class RecordMergeResult:
    """The merged file text and what could not be settled.

    ``resolvers`` maps each record id to the rule that settled it. A non-empty
    ``held`` means a person must decide; the text still parses.
    """

    text: str
    held: tuple[HeldRecord, ...] = ()
    resolvers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DerivedMergeResult:
    """The side taken for a regenerated file.

    ``regenerate`` is True when the two sides differed: the caller must rebuild
    the file from its sources rather than trust ``text``.
    """

    text: str | None
    taken: str  # "local" or "remote"
    regenerate: bool


def _index(codec: RecordCodec, text: str) -> tuple[dict[str, Any], bool]:
    """Records by id, and whether the same id appeared twice."""
    by_key: dict[str, Any] = {}
    duplicate = False
    for record in codec.parse(text):
        k = codec.key(record)
        duplicate = duplicate or k in by_key
        by_key[k] = record
    return by_key, duplicate


def merge_records(kind: str, local: str, remote: str) -> RecordMergeResult:
    """Merge two versions of a record file of *kind* by record id.

    An unregistered *kind* is held whole with the remote text: a new kind of
    file is never merged by a rule nobody wrote for it (design section 5).
    """
    codec = RECORD_CODECS.get(kind)
    if codec is None:
        return RecordMergeResult(remote, (HeldRecord("", REASON_UNKNOWN_KIND),))
    if codec.unparsed(local) or codec.unparsed(remote):
        return RecordMergeResult(remote, (HeldRecord("", REASON_UNPARSEABLE),))
    mine, dup_local = _index(codec, local)
    theirs, dup_remote = _index(codec, remote)
    if dup_local or dup_remote:
        return RecordMergeResult(remote, (HeldRecord("", REASON_DUPLICATE_ID),))

    merged: dict[str, Any] = {}
    resolvers: dict[str, str] = {}
    held: list[HeldRecord] = []
    for k in sorted(mine.keys() | theirs.keys()):
        a, b = mine.get(k), theirs.get(k)
        if b is None:
            merged[k], resolvers[k] = a, R_LOCAL_ONLY
        elif a is None:
            merged[k], resolvers[k] = b, R_REMOTE_ONLY
        elif a == b:
            merged[k], resolvers[k] = b, R_IDENTICAL
        else:
            out_a, out_b = codec.outcome(a), codec.outcome(b)
            if out_a is None and out_b is None:
                merged[k], resolvers[k] = b, R_OPEN_DIFFERS
            elif out_a is None:
                merged[k], resolvers[k] = b, R_RESOLVED_WINS
            elif out_b is None:
                merged[k], resolvers[k] = a, R_RESOLVED_WINS
            elif out_a == out_b:
                merged[k], resolvers[k] = b, R_SAME_OUTCOME
            else:
                merged[k], resolvers[k] = b, R_HELD
                held.append(HeldRecord(k, REASON_RESOLVED_DIFFERENTLY))

    ordered = sorted(merged.values(), key=lambda r: (codec.order(r), codec.key(r)))
    return RecordMergeResult(codec.render(ordered), tuple(held), resolvers)


def merge_derived(
    local: str | None, remote: str | None, *, take: str = "remote"
) -> DerivedMergeResult:
    """Take one side of a regenerated file; never merge the two.

    ``take`` is ``"remote"`` (the default, the side already published) or
    ``"local"``. ``regenerate`` is False only when both sides already agree.
    """
    if take not in ("local", "remote"):
        raise ValueError(f"take must be 'local' or 'remote', got {take!r}")
    text = local if take == "local" else remote
    return DerivedMergeResult(text, take, local != remote)
