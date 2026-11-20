# SPDX-License-Identifier: Apache-2.0
"""The ``raised_from`` provenance block of a raised copy (athenaeum#2799).

The public half of a raise's provenance record is the ``raised_from`` block in
the copy's frontmatter (``docs/design/federation-raise-retract.md`` §5, §5.1,
§5.2, §14 slice S3). Every reader of the target store can see it, so its shape
is a disclosure boundary: this module accepts exactly the nine fields of §5.1
and refuses every other key, which is what keeps a later change from quietly
adding a source uid or a path (the "no link back" ruling, athenaeum#2631).

Provided here, all read-only:

* :func:`parse_raised_from` -- parse and validate the block, failing closed;
* :data:`RAISE_SOURCE_TYPE` / :func:`raise_source_ref` -- the ``raise:<raise_id>``
  typed source a copy lists in ``sources``;
* :func:`lint_raised_from` -- reason codes for one page's frontmatter;
* :func:`scan_raised_from` -- the lint over every page surface, behind
  ``athenaeum raised-from-lint``.

Nothing here writes a block onto a page; acceptance does that (S7a).

Layering: L2. Imports :mod:`athenaeum.raise_projection`,
:mod:`athenaeum.store_access`, :mod:`athenaeum.provenance`,
:mod:`athenaeum.models`, :mod:`athenaeum.storage`, :mod:`athenaeum.config`;
nothing at L3+.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from athenaeum.config import load_config
from athenaeum.models import EntityIndex, parse_frontmatter
from athenaeum.provenance import parse_source
from athenaeum.raise_projection import is_raise_id
from athenaeum.storage import resolve_locator_surfaces
from athenaeum.store_access import is_store_id

#: The only block version this build knows (§5.1). Any other value is malformed.
RAISED_FROM_VERSION = 1

#: The ``sources`` type of a raised copy: ``raise:<raise_id>`` (§5.1).
#:
#: The precedence rank of this type is UNSET. ``athenaeum.precedence.source_rank``
#: gives a type that no tier lists the lowest rank; that is the default for an
#: unlisted type and not a decision about raises. The rank belongs to the trust
#: design (athenaeum#2789). No code may rely on a raised copy winning or losing
#: a conflict.
RAISE_SOURCE_TYPE = "raise"

# Reason codes returned by :func:`lint_raised_from`.
MALFORMED_BLOCK = "malformed_block"
UNKNOWN_KEY = "unknown_key"
MISSING_RAISE_SOURCE = "missing_raise_source"
RAISE_SOURCE_MISMATCH = "raise_source_mismatch"

_KEYS = frozenset(
    {
        "v", "raise_id", "origin", "proposer", "via",
        "content_hash", "raised_on", "seq", "watch",
    }
)
_REQUIRED = _KEYS - {"watch"}
_WATCH_STATES = ("active", "lapsed", "ended")
_CONTENT_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}\Z")
_ISO_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\Z")
_GROUP_PREFIX = "group:"
_REVIEW_PREFIX = "review:"


class RaisedFromError(ValueError):
    """A ``raised_from`` block is present and invalid.

    ``codes`` holds the reason codes (``unknown_key`` and/or ``malformed_block``)
    and ``details`` one human sentence per defect.
    """

    def __init__(self, codes: tuple[str, ...], details: tuple[str, ...]) -> None:
        self.codes = codes
        self.details = details
        super().__init__("; ".join(details))


@dataclass(frozen=True)
class RaisedFrom:
    """A validated ``raised_from`` block."""

    v: int
    raise_id: str
    origin: str
    proposer: str | dict[str, str]
    via: str
    content_hash: str
    raised_on: date
    seq: int
    watch: str = "active"


def raise_source_ref(raise_id: str) -> str:
    """The ``sources`` entry a copy of *raise_id* lists: ``raise:<raise_id>``."""
    return f"{RAISE_SOURCE_TYPE}:{raise_id}"


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_proposer(value: object) -> str | dict[str, str] | None:
    if isinstance(value, str):
        if value.startswith(_GROUP_PREFIX) and value[len(_GROUP_PREFIX):].strip():
            return value
        return None
    if isinstance(value, Mapping):
        iss, sub = value.get("iss"), value.get("sub")
        if (
            isinstance(iss, str) and iss.strip() and isinstance(sub, (str, int))
            and not isinstance(sub, bool) and str(sub).strip()
        ):
            return {"iss": iss, "sub": str(sub)}
    return None


def _check_day(value: object) -> date | None:
    if isinstance(value, datetime):
        return None  # a time of day is refused (§5.2: the day, not the second)
    if isinstance(value, date):
        return value
    if isinstance(value, str) and _ISO_DAY_RE.match(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def parse_raised_from(meta: Mapping[str, Any]) -> RaisedFrom | None:
    """Parse the ``raised_from`` block of a page's frontmatter (§5.1).

    ``None`` when the key is absent. :class:`RaisedFromError` when the block is
    present and invalid: not a mapping, a key outside the nine of §5.1, a
    missing required key, or a value that breaks its rule. Fails closed.
    """
    if "raised_from" not in meta:
        return None
    block = meta["raised_from"]
    if not isinstance(block, Mapping):
        raise RaisedFromError((MALFORMED_BLOCK,), ("raised_from is not a mapping",))
    codes: list[str] = []
    details: list[str] = []

    def bad(key: str, why: str) -> None:
        if MALFORMED_BLOCK not in codes:
            codes.append(MALFORMED_BLOCK)
        details.append(f"{key}: {why}")

    extra = sorted(str(k) for k in block if k not in _KEYS)
    if extra:
        codes.append(UNKNOWN_KEY)
        details.append(f"unknown key(s): {', '.join(extra)}")
    for key in sorted(_REQUIRED - set(block)):
        bad(key, "required")

    v = block.get("v")
    if "v" in block and not (_is_int(v) and v == RAISED_FROM_VERSION):
        bad("v", f"must be the integer {RAISED_FROM_VERSION}")
    raise_id = block.get("raise_id")
    if "raise_id" in block and not is_raise_id(raise_id):
        bad("raise_id", "must be rs_ and 20 lowercase base32 characters")
    origin = block.get("origin")
    if "origin" in block and not is_store_id(origin):
        bad("origin", "must be a store id (st_ and 12 lowercase base32 characters)")
    proposer = _check_proposer(block.get("proposer"))
    if "proposer" in block and proposer is None:
        bad("proposer", "must be {iss, sub} or group:<name>")
    via = block.get("via")
    if "via" in block and not (
        via == "proposer"
        or (
            isinstance(via, str)
            and via.startswith(_REVIEW_PREFIX)
            and via[len(_REVIEW_PREFIX):].strip()
        )
    ):
        bad("via", "must be proposer or review:<name>")
    chash = block.get("content_hash")
    if "content_hash" in block and not (
        isinstance(chash, str) and _CONTENT_HASH_RE.match(chash)
    ):
        bad("content_hash", "must be sha256: and 64 lowercase hex characters")
    raised_on = _check_day(block.get("raised_on"))
    if "raised_on" in block and raised_on is None:
        bad("raised_on", "must be a calendar date without a time of day")
    seq = block.get("seq")
    if "seq" in block and not (isinstance(seq, int) and not isinstance(seq, bool) and seq >= 1):
        bad("seq", "must be an integer of 1 or more")
    watch = block.get("watch", "active")
    if watch not in _WATCH_STATES:
        bad("watch", "must be active, lapsed or ended")

    if codes:
        raise RaisedFromError(tuple(codes), tuple(details))
    assert proposer is not None and raised_on is not None and isinstance(seq, int)
    return RaisedFrom(
        v=RAISED_FROM_VERSION,
        raise_id=str(raise_id),
        origin=str(origin),
        proposer=proposer,
        via=str(via),
        content_hash=str(chash),
        raised_on=raised_on,
        seq=seq,
        watch=str(watch),
    )


def _raise_source_ids(meta: Mapping[str, Any]) -> list[str]:
    """The ``<raise_id>`` of every ``raise:`` entry in the page's ``sources``."""
    sources = meta.get("sources")
    if sources is None:
        return []
    items = sources if isinstance(sources, list) else [sources]
    found: list[str] = []
    for item in items:
        try:
            ref = parse_source(item)
        except ValueError:
            continue
        if ref is not None and ref.type == RAISE_SOURCE_TYPE:
            found.append(ref.ref)
    return found


def lint_raised_from(meta: Mapping[str, Any]) -> list[str]:
    """Reason codes for one page's frontmatter; empty when it is fine. Pure.

    A page with no ``raised_from`` block is never reported, even when it lists
    a ``raise:`` source: that is a page compiled from a single routed fact
    (§6.3), the target's own page, which carries no block.
    """
    try:
        block = parse_raised_from(meta)
    except RaisedFromError as exc:
        return list(exc.codes)
    if block is None:
        return []
    ids = _raise_source_ids(meta)
    if not ids:
        return [MISSING_RAISE_SOURCE]
    if any(i != block.raise_id for i in ids):
        return [RAISE_SOURCE_MISMATCH]
    return []


@dataclass(frozen=True)
class RaisedFromFinding:
    """One page whose ``raised_from`` block failed the lint."""

    uid: str
    page: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class RaisedFromReport:
    pages_scanned: int
    findings: tuple[RaisedFromFinding, ...]

    @property
    def ok(self) -> bool:
        return not self.findings


def scan_raised_from(
    knowledge_root: Path, config: dict[str, Any] | None = None
) -> RaisedFromReport:
    """Run :func:`lint_raised_from` over every page of every surface (read-only)."""
    if config is None:
        config = load_config(knowledge_root)
    wiki_root = knowledge_root / "wiki"
    index = EntityIndex(wiki_root, resolve_locator_surfaces(knowledge_root, config))
    scanned = 0
    found: list[RaisedFromFinding] = []
    for _key, path in index.locator.iter_pages():
        scanned += 1
        try:
            meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if not isinstance(meta, dict):
            continue
        reasons = lint_raised_from(meta)
        if reasons:
            uid = meta.get("uid")
            found.append(
                RaisedFromFinding(
                    uid=str(uid) if uid else "",
                    page=path.name,
                    reasons=tuple(reasons),
                )
            )
    return RaisedFromReport(pages_scanned=scanned, findings=tuple(found))
