# SPDX-License-Identifier: Apache-2.0
"""The acceptance ledger: one disposition per raise message (athenaeum#2938).

``docs/design/federation-raise-retract.md`` §4.4, §5 and §6.2, build slice S5.
When a store's compile role acts on a waiting message it records exactly one
disposition for it: ``accepted``, ``rejected`` with a reason code, or ``held``
for a decision. This module is the row shape, the parser and the lookups. It
does not act on a message (S7a) and does not place the ledger on a bench (S14).

A row carries ids and metadata only: the ``raise_id``, ``message_id`` and
``seq``; the origin and target ``store_id``; the proposer's own identity
(``iss`` and ``sub``); the location the message arrived through; the exact
time; the payload hash as it arrived; any adjustment of ``type`` and
``tags``; the blob id of the accepted version; and, for a routed fact, the
uids of the target pages written. **No page text and no source page
identifier is ever stored**: the parser accepts a fixed key set and treats a
row with any other key (``body``, ``page``, ``uid``, ``path``, ...) as
malformed, and the writer has no parameter that could carry either.

The ledger is the install's own operational state, in the cache dir beside
the other ledgers (see ``store.ARTIFACT_REGISTRY``, ``raise-acceptance-ledger``).
It is outside the wiki and raw roots, so it is never a page and is never added
to the recall index.

Append-only JSONL. Where one message has several rows (a held message later
accepted or rejected) the latest row is its disposition. A rejection of a
``raise_id`` is permanent: later messages for it are dropped by rule (§4.4).

Layering: L3 (service). Imports :mod:`athenaeum.store` (L1),
:mod:`athenaeum.raise_projection` (L2), :mod:`athenaeum.raise_messages` (L2)
and :mod:`athenaeum.store_access` (L2).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from athenaeum.raise_messages import (
    KIND_ATTEST,
    KIND_PROPOSE_FACT,
    KIND_RAISE,
    KIND_SUPERSEDE,
    RECORD_KINDS,
)
from athenaeum.raise_projection import is_message_id, is_raise_id
from athenaeum.store import append_line_durable, now_iso
from athenaeum.store_access import is_store_id

__all__ = [
    "ACCEPTANCE_LEDGER_FILENAME",
    "AcceptanceRow",
    "Adjustment",
    "DISPOSITIONS",
    "LOCATIONS",
    "REASON_CODES",
    "acceptance_ledger_path",
    "disposition_of",
    "find_adjustment",
    "find_rejection",
    "is_rejected",
    "iter_rows",
    "record_acceptance",
]

#: The ledger file, in the install's cache dir (the install's own state).
ACCEPTANCE_LEDGER_FILENAME = "_raise_acceptance.jsonl"

DISPOSITION_ACCEPTED = "accepted"
DISPOSITION_REJECTED = "rejected"
DISPOSITION_HELD = "held"

#: The three dispositions a message can get (North star §2.3).
DISPOSITIONS = (DISPOSITION_ACCEPTED, DISPOSITION_REJECTED, DISPOSITION_HELD)

#: Where the message arrived through (§6.2): the target's bench, the compiling
#: install's own raw tree for the store, or the target's raw location.
LOCATIONS = ("bench", "install_raw", "target_raw")

#: The fixed reason codes. There is no free-text reason: a sentence about a
#: message can quote it (the same rule as the retract reasons of §6.1). One
#: code per check of §4.4, plus ``declined`` (the compile role rejected it),
#: ``store_refuses`` (the store's ``intake`` rules refuse the type and tags)
#: and ``uid_taken`` (the derived uid belongs to another page).
REASON_CODES = frozenset(
    {
        "declined",
        "store_refuses",
        "unparseable",
        "target_mismatch",
        "proposer_lacks_right",
        "payload_has_link",
        "page_invalid",
        "hash_mismatch",
        "already_rejected",
        "duplicate",
        "uid_taken",
    }
)

#: Kinds that may carry a disposition: every kind that names a ``raise_id``.
_LEDGER_KINDS = RECORD_KINDS - {KIND_ATTEST}
#: Kinds whose accepted row names a blob (a whole page was created or replaced).
_BLOB_KINDS = frozenset({KIND_RAISE, KIND_SUPERSEDE})

_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}\Z")
_BLOB_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_UID_RE = re.compile(r"^[0-9a-f]{16}\Z")
_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")

_ROW_KEYS = frozenset(
    {
        "ts",
        "disposition",
        "reason",
        "kind",
        "raise_id",
        "message_id",
        "seq",
        "origin",
        "target",
        "proposer",
        "location",
        "payload_hash",
        "adjustment",
        "blob_id",
        "page_uids",
    }
)
_REQUIRED_KEYS = frozenset(
    {
        "ts",
        "disposition",
        "kind",
        "raise_id",
        "message_id",
        "seq",
        "origin",
        "target",
        "proposer",
        "location",
    }
)


def _is_str_list(v: object) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) and x for x in v)


@dataclass(frozen=True)
class Adjustment:
    """The compile role's adjustment of ``type`` and ``tags`` (§4.4).

    Each part is ``None`` when it was not adjusted; at least one is set. Tags
    are the whole adjusted list, not a difference.
    """

    type: str | None = None
    tags: tuple[str, ...] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.type is not None:
            out["type"] = self.type
        if self.tags is not None:
            out["tags"] = list(self.tags)
        return out

    @classmethod
    def from_dict(cls, data: object) -> Adjustment | None:
        if not isinstance(data, dict) or not data or set(data) - {"type", "tags"}:
            return None
        typ = data.get("type")
        tags = data.get("tags")
        if "type" in data and not (isinstance(typ, str) and typ):
            return None
        if "tags" in data and not _is_str_list(tags):
            return None
        return cls(type=typ, tags=tuple(tags) if tags is not None else None)


@dataclass(frozen=True)
class AcceptanceRow:
    """One acceptance-ledger row. Ids and metadata only, never page text."""

    ts: str
    disposition: str
    kind: str
    raise_id: str
    message_id: str
    seq: int
    origin: str
    target: str
    proposer: dict[str, str]
    location: str
    reason: str | None = None
    payload_hash: str | None = None
    adjustment: Adjustment | None = None
    blob_id: str | None = None
    page_uids: tuple[str, ...] | None = None

    def problem(self) -> str | None:
        """Why this row is not a valid one, or ``None`` when it is."""
        if not (isinstance(self.ts, str) and _TIME_RE.match(self.ts)):
            return "ts is not a UTC second-precision timestamp"
        if self.disposition not in DISPOSITIONS:
            return "disposition is not accepted, rejected or held"
        if self.kind not in _LEDGER_KINDS:
            return "kind is not a message kind that names a raise_id"
        if not is_raise_id(self.raise_id):
            return "raise_id is not a raise id"
        if not is_message_id(self.message_id):
            return "message_id is not a message id"
        if not isinstance(self.seq, int) or isinstance(self.seq, bool) or self.seq < 1:
            return "seq is not a positive integer"
        if not is_store_id(self.origin) or not is_store_id(self.target):
            return "origin and target must be store ids"
        p = self.proposer
        if not (
            isinstance(p, dict)
            and set(p) == {"iss", "sub"}
            and all(isinstance(v, str) and v for v in p.values())
        ):
            return "proposer must be exactly an iss and a sub"
        if self.location not in LOCATIONS:
            return "location is not bench, install_raw or target_raw"
        if self.payload_hash is not None and not (
            isinstance(self.payload_hash, str) and _HASH_RE.match(self.payload_hash)
        ):
            return "payload_hash is not a sha256 hash"
        accepted = self.disposition == DISPOSITION_ACCEPTED
        if self.disposition == DISPOSITION_REJECTED and self.reason is None:
            return "a rejection needs a reason code"
        if accepted and self.reason is not None:
            return "an accepted row carries no reason code"
        if self.reason is not None and self.reason not in REASON_CODES:
            return "reason is not one of the fixed reason codes"
        if self.adjustment is not None:
            if not accepted:
                return "only an accepted row records an adjustment"
            if self.adjustment.type is None and self.adjustment.tags is None:
                return "an adjustment must set a type or tags"
        wants_blob = accepted and self.kind in _BLOB_KINDS
        wants_uids = accepted and self.kind == KIND_PROPOSE_FACT
        if wants_blob != (self.blob_id is not None):
            return "an accepted raise or supersede names exactly its blob id; nothing else does"
        if self.blob_id is not None and not _BLOB_RE.match(self.blob_id):
            return "blob_id is not a git object id"
        if wants_uids != (self.page_uids is not None):
            return "an accepted fact names the page uids written; nothing else does"
        if self.page_uids is not None and (
            not self.page_uids or not all(_UID_RE.match(u) for u in self.page_uids)
        ):
            return "page_uids must be a non-empty list of 16-hex uids"
        return None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ts": self.ts,
            "disposition": self.disposition,
            "kind": self.kind,
            "raise_id": self.raise_id,
            "message_id": self.message_id,
            "seq": self.seq,
            "origin": self.origin,
            "target": self.target,
            "proposer": dict(self.proposer),
            "location": self.location,
        }
        if self.reason is not None:
            out["reason"] = self.reason
        if self.payload_hash is not None:
            out["payload_hash"] = self.payload_hash
        if self.adjustment is not None:
            out["adjustment"] = self.adjustment.to_dict()
        if self.blob_id is not None:
            out["blob_id"] = self.blob_id
        if self.page_uids is not None:
            out["page_uids"] = list(self.page_uids)
        return out

    @classmethod
    def from_dict(cls, data: object) -> AcceptanceRow | None:
        """Parse one row; ``None`` for anything malformed (never raises).

        A row with a key outside the fixed set, such as ``body`` or ``uid``,
        is malformed: the ledger holds no page text and no source identifier.
        """
        if not isinstance(data, dict):
            return None
        keys = set(data)
        if not _REQUIRED_KEYS <= keys <= _ROW_KEYS:
            return None
        adj = None
        if "adjustment" in data:
            adj = Adjustment.from_dict(data["adjustment"])
            if adj is None:
                return None
        uids = data.get("page_uids")
        if "page_uids" in data and not _is_str_list(uids):
            return None
        proposer = data["proposer"]
        row = cls(
            ts=data["ts"],
            disposition=data["disposition"],
            kind=data["kind"],
            raise_id=data["raise_id"],
            message_id=data["message_id"],
            seq=data["seq"],
            origin=data["origin"],
            target=data["target"],
            proposer=dict(proposer) if isinstance(proposer, dict) else proposer,
            location=data["location"],
            reason=data.get("reason"),
            payload_hash=data.get("payload_hash"),
            adjustment=adj,
            blob_id=data.get("blob_id"),
            page_uids=tuple(uids) if uids is not None else None,
        )
        try:
            return row if row.problem() is None else None
        except TypeError:  # an unhashable or odd value in a field
            return None


def acceptance_ledger_path(cache_dir: Path) -> Path:
    """The acceptance ledger's path under *cache_dir*."""
    return Path(cache_dir) / ACCEPTANCE_LEDGER_FILENAME


def record_acceptance(
    cache_dir: Path,
    *,
    disposition: str,
    kind: str,
    raise_id: str,
    message_id: str,
    seq: int,
    origin: str,
    target: str,
    proposer: dict[str, str],
    location: str,
    reason: str | None = None,
    payload_hash: str | None = None,
    adjustment: Adjustment | None = None,
    blob_id: str | None = None,
    page_uids: tuple[str, ...] | None = None,
    now: datetime | None = None,
) -> AcceptanceRow:
    """Append one disposition row and return it. Raises ``ValueError`` on a bad row."""
    row = AcceptanceRow(
        ts=now_iso(now),
        disposition=disposition,
        kind=kind,
        raise_id=raise_id,
        message_id=message_id,
        seq=seq,
        origin=origin,
        target=target,
        proposer=dict(proposer),
        location=location,
        reason=reason,
        payload_hash=payload_hash,
        adjustment=adjustment,
        blob_id=blob_id,
        page_uids=tuple(page_uids) if page_uids is not None else None,
    )
    problem = row.problem()
    if problem is not None:
        raise ValueError(f"not a valid acceptance row: {problem}")
    line = json.dumps(row.to_dict(), sort_keys=True)
    append_line_durable(acceptance_ledger_path(cache_dir), (line + "\n").encode("utf-8"))
    return row


def iter_rows(cache_dir: Path) -> list[AcceptanceRow]:
    """Every well-formed row, oldest first. A torn or malformed line is skipped."""
    path = acceptance_ledger_path(cache_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    rows: list[AcceptanceRow] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        row = AcceptanceRow.from_dict(data)
        if row is not None:
            rows.append(row)
    return rows


def disposition_of(cache_dir: Path, message_id: str) -> AcceptanceRow | None:
    """The latest row for *message_id*: its current disposition, or ``None``.

    A row with reason ``duplicate`` records a re-delivery of the message; it
    never replaces the disposition of the delivery it duplicates.
    """
    match: AcceptanceRow | None = None
    for row in iter_rows(cache_dir):
        if row.message_id == message_id and row.reason != "duplicate":
            match = row
    return match


def find_adjustment(cache_dir: Path, raise_id: str) -> Adjustment | None:
    """The adjustment of type and tags to apply to a later payload for *raise_id*.

    The latest accepted row that recorded an adjustment wins (§4.4).
    """
    match: Adjustment | None = None
    for row in iter_rows(cache_dir):
        if row.raise_id == raise_id and row.adjustment is not None:
            match = row.adjustment
    return match


def find_rejection(cache_dir: Path, raise_id: str) -> AcceptanceRow | None:
    """The first rejection row for *raise_id*, or ``None`` when it was never rejected."""
    for row in iter_rows(cache_dir):
        if row.raise_id == raise_id and row.disposition == DISPOSITION_REJECTED:
            return row
    return None


def is_rejected(cache_dir: Path, raise_id: str) -> bool:
    """Whether *raise_id* is already rejected (later messages are dropped by rule)."""
    return find_rejection(cache_dir, raise_id) is not None
