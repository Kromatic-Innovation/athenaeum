# SPDX-License-Identifier: Apache-2.0
"""The raise ledger: what the raiser records for each raise (athenaeum#3001).

``docs/design/federation-raise-retract.md`` §4.3 and §7.1, build slice S4. The
raiser is the only party that can later retract or supersede a raise: the
mapping from a ``raise_id`` to its source page exists here and nowhere else.
This module is the row shape, the parser, the append and the readers. Nothing
writes the ledger yet (push is S6, the raiser pass S9, the routed fact S20).

A row holds ids and a hash and **no page text**: ``v``, ``raise_id``, ``seq``,
the ``origin`` and ``target`` ``store_id``, the ``source_uid``, the ``route``,
the ``content_hash`` (a payload hash, not a hash of the source page), the
``sent_at`` time and the ``state``. The parser accepts exactly that key set and
treats a row with any other key (``body``, ``title``, ``path`` ...) as
malformed.

The ledger is the install's own operational state, in the cache dir beside the
acceptance ledger (see ``store.ARTIFACT_REGISTRY``, ``raise-ledger``). It is
outside the wiki and raw roots, so it is never a page of any store and never
enters the recall index. Keeping it on a bench is a later slice (S14).

Append-only JSONL keyed by ``raise_id`` and ``seq``. A raise's later states
(superseded, retracted) are new rows with a higher ``seq``; the latest row per
``raise_id``, by ``seq``, is its current record.

Layering: L3 (service). Imports :mod:`athenaeum.store` (L1),
:mod:`athenaeum.raise_projection` (L2) and :mod:`athenaeum.store_access` (L2).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from athenaeum.raise_projection import is_raise_id
from athenaeum.store import append_line_durable, now_iso
from athenaeum.store_access import is_store_id

__all__ = [
    "RAISE_LEDGER_FILENAME",
    "RAISE_LEDGER_VERSION",
    "RAISE_ROUTES",
    "RAISE_STATES",
    "RaiseRow",
    "iter_raise_rows",
    "latest_raise_row",
    "latest_raise_rows",
    "raise_ledger_path",
    "read_raise_ledger",
    "record_raise",
]

#: The ledger file, in the install's cache dir (the install's own state).
RAISE_LEDGER_FILENAME = "_raise_ledger.jsonl"

#: The row schema version.
RAISE_LEDGER_VERSION = 1

#: How the raise reached the target (§4.3, the ``via`` of §4.4): through the
#: proposer's grant, or through a review the target declared.
RAISE_ROUTES = ("proposer", "review")

#: Where the raise stands: ``sent``, then ``superseded`` (a newer payload was
#: sent), ``retracted`` (withdrawn) or ``lapsed`` (the raiser lost read on the
#: origin, §9).
RAISE_STATES = ("sent", "superseded", "retracted", "lapsed")

_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}\Z")
#: A page uid in either width: 16 hex, or the 8 hex older pages carry.
_UID_RE = re.compile(r"^(?:[0-9a-f]{16}|[0-9a-f]{8})\Z")
_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")

_ROW_KEYS = frozenset(
    {
        "v",
        "raise_id",
        "seq",
        "origin",
        "source_uid",
        "target",
        "route",
        "content_hash",
        "sent_at",
        "state",
    }
)


@dataclass(frozen=True)
class RaiseRow:
    """One raise-ledger row. Ids and a hash only, never page text."""

    raise_id: str
    seq: int
    origin: str
    source_uid: str
    target: str
    route: str
    content_hash: str
    sent_at: str
    state: str
    v: int = RAISE_LEDGER_VERSION

    def problem(self) -> str | None:
        """Why this row is not a valid one, or ``None`` when it is."""
        if not isinstance(self.v, int) or isinstance(self.v, bool) or self.v != 1:
            return "v is not a known schema version"
        if not is_raise_id(self.raise_id):
            return "raise_id is not a raise id"
        if not isinstance(self.seq, int) or isinstance(self.seq, bool) or self.seq < 1:
            return "seq is not a positive integer"
        if not is_store_id(self.origin) or not is_store_id(self.target):
            return "origin and target must be store ids"
        if not (isinstance(self.source_uid, str) and _UID_RE.match(self.source_uid)):
            return "source_uid is not a page uid"
        if self.route not in RAISE_ROUTES:
            return "route is not proposer or review"
        if not (isinstance(self.content_hash, str) and _HASH_RE.match(self.content_hash)):
            return "content_hash is not a sha256 hash"
        if not (isinstance(self.sent_at, str) and _TIME_RE.match(self.sent_at)):
            return "sent_at is not a UTC second-precision timestamp"
        if self.state not in RAISE_STATES:
            return "state is not one of the fixed states"
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": self.v,
            "raise_id": self.raise_id,
            "seq": self.seq,
            "origin": self.origin,
            "source_uid": self.source_uid,
            "target": self.target,
            "route": self.route,
            "content_hash": self.content_hash,
            "sent_at": self.sent_at,
            "state": self.state,
        }

    @classmethod
    def from_dict(cls, data: object) -> RaiseRow | None:
        """Parse one row; ``None`` for anything malformed (never raises)."""
        if not isinstance(data, dict) or set(data) != _ROW_KEYS:
            return None
        row = cls(
            v=data["v"],
            raise_id=data["raise_id"],
            seq=data["seq"],
            origin=data["origin"],
            source_uid=data["source_uid"],
            target=data["target"],
            route=data["route"],
            content_hash=data["content_hash"],
            sent_at=data["sent_at"],
            state=data["state"],
        )
        try:
            return row if row.problem() is None else None
        except TypeError:  # an unhashable or odd value in a field
            return None


def raise_ledger_path(cache_dir: Path) -> Path:
    """The raise ledger's path under *cache_dir*."""
    return Path(cache_dir) / RAISE_LEDGER_FILENAME


def record_raise(
    cache_dir: Path,
    *,
    raise_id: str,
    seq: int,
    origin: str,
    source_uid: str,
    target: str,
    route: str,
    content_hash: str,
    state: str = "sent",
    now: datetime | None = None,
) -> RaiseRow:
    """Append one row and return it. Raises ``ValueError`` on a bad row."""
    row = RaiseRow(
        raise_id=raise_id,
        seq=seq,
        origin=origin,
        source_uid=source_uid,
        target=target,
        route=route,
        content_hash=content_hash,
        sent_at=now_iso(now),
        state=state,
    )
    problem = row.problem()
    if problem is not None:
        raise ValueError(f"not a valid raise row: {problem}")
    line = json.dumps(row.to_dict(), sort_keys=True)
    append_line_durable(raise_ledger_path(cache_dir), (line + "\n").encode("utf-8"))
    return row


def read_raise_ledger(cache_dir: Path) -> tuple[list[RaiseRow], list[str]]:
    """Every well-formed row, oldest first, and the malformed lines.

    A torn or malformed line is reported (returned as its text), never raised.
    """
    try:
        text = raise_ledger_path(cache_dir).read_text(encoding="utf-8")
    except OSError:
        return [], []
    rows: list[RaiseRow] = []
    malformed: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            malformed.append(line)
            continue
        row = RaiseRow.from_dict(data)
        if row is None:
            malformed.append(line)
        else:
            rows.append(row)
    return rows, malformed


def iter_raise_rows(cache_dir: Path) -> list[RaiseRow]:
    """Every well-formed row, oldest first. A malformed line is skipped."""
    return read_raise_ledger(cache_dir)[0]


def latest_raise_rows(cache_dir: Path) -> dict[str, RaiseRow]:
    """The latest row per ``raise_id``: the highest ``seq``, whatever the file order.

    Two rows with the same ``seq`` resolve to the one written later.
    """
    latest: dict[str, RaiseRow] = {}
    for row in iter_raise_rows(cache_dir):
        cur = latest.get(row.raise_id)
        if cur is None or row.seq >= cur.seq:
            latest[row.raise_id] = row
    return latest


def latest_raise_row(cache_dir: Path, raise_id: str) -> RaiseRow | None:
    """The current record for *raise_id*, or ``None`` when it was never raised."""
    return latest_raise_rows(cache_dir).get(raise_id)
