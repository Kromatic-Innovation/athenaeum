# SPDX-License-Identifier: Apache-2.0
"""Shape mining over the verdict ledger (issue athenaeum#719, Plan step 1).

The self-tuning loop's first signal source: **underdetermined** verdicts
are a decided, memoized outcome (:mod:`athenaeum.comparator`'s
``missing``-naming branch) that means "the comparator could not resolve a
required separator dimension." A single underdetermined pair is normal
— claims routinely differ on an axis nobody declared yet. A *recurring*
shape — the SAME typed combination of missing dimensions, memory class, and
scope pattern, across many distinct pairs — is the signature the issue's
Motivation names: a missing axis the operator has not noticed by hand.

**Typed clustering only (AC2).** A shape key is a tuple of already-typed,
already-declared fields: the verdict type (always ``underdetermined`` for
this source), the sorted tuple of missing dimension names, the two sides'
``memory_class`` coordinates (sorted), and the two sides' ``scope``
coordinates (sorted). Nothing here reads page prose or calls an LLM; an
LLM may *summarize* a shape for a human later (:mod:`athenaeum.dimensions`'
proposal drafter, a later Plan step), but never gate this trigger — see the
AC's own text.

Layering: L4. Reads :mod:`athenaeum.verdicts` (L2) for the ledger and
:mod:`athenaeum.dimensions` (L1/L2) for the two kernel coordinate readers;
walks the wiki tree directly (like :func:`athenaeum.decision_answers._find_page_path_by_id`'s
established idiom) to resolve a pair's two ids back to frontmatter, since
neither id carries memory_class/scope itself. No config writes, no LLM
client, no mutation — this module only reads.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from athenaeum.config import (
    resolve_signal_mining_threshold,
    resolve_signal_mining_window_days,
)
from athenaeum.dimensions import MEMORY_CLASS, SCOPE, coordinate_value
from athenaeum.models import parse_frontmatter
from athenaeum.verdicts import list_by_verdict

log = logging.getLogger(__name__)

#: The verdict type this module mines. Only ``underdetermined`` carries a
#: ``missing`` list naming unresolved dimensions (issue athenaeum#719 AC1);
#: the other four verdict values are out of scope for this signal source.
UNDERDETERMINED = "underdetermined"


@dataclass(frozen=True)
class ShapeKey:
    """Typed cluster key for one recurring underdetermined shape (AC2).

    Every field is already-declared, typed structure — never free text,
    never an LLM's impression of similarity. ``missing_dimensions`` and the
    two coordinate pairs are sorted so that side order never fragments one
    real shape into two keys.
    """

    verdict_type: str
    missing_dimensions: tuple[str, ...]
    memory_classes: tuple[str | None, str | None]
    scopes: tuple[str | None, str | None]


@dataclass
class MinedShape:
    """One shape that crossed (or is approaching) the recurrence trigger.

    ``example_pairs`` holds up to 3 pair keys (issue athenaeum#719's
    ratification AC: "shown against three example pairs") — the drafter
    (a later Plan step) reads no more than this module hands it.
    """

    key: ShapeKey
    count: int
    example_pairs: tuple[str, ...]
    threshold: int
    window_days: int

    @property
    def triggered(self) -> bool:
        return self.count >= self.threshold


def _parse_at(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _within_window(entry: dict[str, Any], *, now: datetime, window_days: int) -> bool:
    at = _parse_at(str(entry.get("at") or ""))
    if at is None:
        # Fail-open to INCLUDED rather than silently dropping an undated
        # entry from the mining population — an absent timestamp must never
        # look like "too old", only like "undated".
        return True
    return (now - at) <= timedelta(days=window_days)


def _meta_for_page_id(
    wiki_root: Path, page_id: str, cache: dict[str, dict[str, Any] | None]
) -> dict[str, Any] | None:
    """Resolve one comparator-side page id back to its frontmatter.

    Same reverse-lookup idiom as
    :func:`athenaeum.decision_answers._find_page_path_by_id` — walks
    ``wiki_root`` once per *page_id* (memoized via *cache* across a single
    mining run) comparing :func:`athenaeum.verdicts.page_id_for_path`.
    Returns ``None`` on a miss (a page since moved/retired) rather than
    raising — a resolution miss just means that side contributes no
    memory_class/scope signal, not a mining failure.
    """
    if page_id in cache:
        return cache[page_id]
    from athenaeum.verdicts import page_id_for_path

    found: dict[str, Any] | None = None
    if wiki_root.is_dir():
        for candidate in sorted(wiki_root.rglob("*.md")):
            if candidate.name.startswith("_"):
                continue
            if page_id_for_path(candidate) == page_id:
                try:
                    text = candidate.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    break
                meta, _ = parse_frontmatter(text)
                found = meta or {}
                break
    cache[page_id] = found
    return found


def shape_key_for_entry(
    entry: dict[str, Any], *, wiki_root: Path, cache: dict[str, dict[str, Any] | None]
) -> ShapeKey:
    """Typed shape key for one underdetermined :class:`~athenaeum.verdicts.VerdictEntry` dict."""
    missing = tuple(sorted(str(m) for m in (entry.get("missing") or [])))
    pair = str(entry.get("pair") or "")
    id_a, _, id_b = pair.partition("+")
    meta_a = _meta_for_page_id(wiki_root, id_a, cache) if id_a else None
    meta_b = _meta_for_page_id(wiki_root, id_b, cache) if id_b else None
    mem_a = coordinate_value(MEMORY_CLASS, meta_a)
    mem_b = coordinate_value(MEMORY_CLASS, meta_b)
    scope_a = coordinate_value(SCOPE, meta_a)
    scope_b = coordinate_value(SCOPE, meta_b)
    memory_classes = tuple(sorted((mem_a, mem_b), key=lambda v: (v is None, v)))
    scopes = tuple(sorted((scope_a, scope_b), key=lambda v: (v is None, v)))
    return ShapeKey(
        verdict_type=UNDERDETERMINED,
        missing_dimensions=missing,
        memory_classes=memory_classes,
        scopes=scopes,
    )


def mine_underdetermined_shapes(
    wiki_root: Path,
    *,
    config: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> list[MinedShape]:
    """AC1's detector: recurring typed shapes among the live ``underdetermined``
    verdict-ledger entries, over :func:`athenaeum.config.resolve_signal_mining_window_days`.

    Pure and side-effect-free — reads the verdict ledger and the wiki tree
    (for memory_class/scope resolution), writes nothing. Returns every
    shape observed at least once, sorted most-frequent-first then by key, so
    a caller bounded by an ask/draft budget (a later Plan step) drains the
    highest-value shapes first; :attr:`MinedShape.triggered` tells the
    caller which ones actually crossed :func:`athenaeum.config.resolve_signal_mining_threshold`.
    """
    wiki_root = Path(wiki_root)
    now = now or datetime.now(timezone.utc)
    threshold = resolve_signal_mining_threshold(config)
    window_days = resolve_signal_mining_window_days(config)

    entries = [
        e
        for e in list_by_verdict(wiki_root, UNDERDETERMINED)
        if _within_window(e, now=now, window_days=window_days)
    ]

    cache: dict[str, dict[str, Any] | None] = {}
    grouped: dict[ShapeKey, list[dict[str, Any]]] = {}
    for entry in entries:
        key = shape_key_for_entry(entry, wiki_root=wiki_root, cache=cache)
        grouped.setdefault(key, []).append(entry)

    shapes: list[MinedShape] = []
    for key, rows in grouped.items():
        pairs = sorted({str(r.get("pair") or "") for r in rows})
        shapes.append(
            MinedShape(
                key=key,
                count=len(pairs),
                example_pairs=tuple(pairs[:3]),
                threshold=threshold,
                window_days=window_days,
            )
        )

    shapes.sort(
        key=lambda s: (
            -s.count,
            s.key.missing_dimensions,
            tuple(v or "" for v in s.key.memory_classes),
            tuple(v or "" for v in s.key.scopes),
        )
    )
    return shapes


def triggered_shapes(shapes: list[MinedShape]) -> list[MinedShape]:
    """The subset of *shapes* that crossed the recurrence threshold."""
    return [s for s in shapes if s.triggered]


# ---------------------------------------------------------------------------
# Decision-queue shape mining (issue athenaeum#2017 AC2/AC3) — the
# decision-queue half this epic's signal-mining child (athenaeum#719 Plan step 1)
# deliberately deferred.
# ---------------------------------------------------------------------------
#
# A recurring human RESOLUTION (ingested as a claim with provenance by
# :mod:`athenaeum.resolution_claims`, issue athenaeum#2017 AC1) is a typed shape
# exactly like a recurring underdetermined verdict is: ``(decision_type,
# verdict, dimension_name)`` is already-declared structure, never free text.
# Reusing :class:`ShapeKey`/:class:`MinedShape` themselves — rather than a
# parallel dataclass pair — is what literally lets
# :func:`athenaeum.dimension_proposals.draft_dimension_proposal` (the SAME
# drafter the verdict-ledger shapes feed) consume a decision shape with zero
# changes on that side: ``verdict_type`` carries ``"decision:<type>:<verdict>"``
# (never collides with the bare ``"underdetermined"`` verdict-ledger shapes),
# ``missing_dimensions`` carries the one axis the resolution named (or ``()``
# when none), and ``memory_classes``/``scopes`` are always ``(None, None)`` —
# a decision-queue resolution carries no page-pair coordinates to cluster on,
# only the two sides already named above.


def _within_claim_window(record: dict[str, Any], *, now: datetime, window_days: int) -> bool:
    """Same fail-open-to-included posture as :func:`_within_window`, reading
    a resolution-claim record's ``created_at`` field instead of a verdict
    entry's ``at``."""
    at = _parse_at(str(record.get("created_at") or ""))
    if at is None:
        return True
    return (now - at) <= timedelta(days=window_days)


def decision_shape_key(record: dict[str, Any]) -> ShapeKey:
    """Typed shape key for one resolution-claim record (AC2)."""
    decision_type = str(record.get("decision_type") or "")
    verdict = str(record.get("verdict") or "")
    dimension_name = str(record.get("dimension_name") or "")
    return ShapeKey(
        verdict_type=f"decision:{decision_type}:{verdict}",
        missing_dimensions=(dimension_name,) if dimension_name else (),
        memory_classes=(None, None),
        scopes=(None, None),
    )


def mine_decision_shapes(
    wiki_root: Path,
    *,
    config: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> list[MinedShape]:
    """AC2's detector: recurring typed shapes among ACTIVE (non-revoked)
    resolution claims (:mod:`athenaeum.resolution_claims`, issue athenaeum#2017
    AC1), over the SAME :func:`athenaeum.config.resolve_signal_mining_window_days`
    window :func:`mine_underdetermined_shapes` uses.

    Pure and side-effect-free. Returns :class:`MinedShape` instances —
    identical type to :func:`mine_underdetermined_shapes`'s output, so a
    caller can mine both sources and feed every triggered shape from either
    into :func:`athenaeum.dimension_proposals.run_dimension_proposal_drafting`
    without branching on which detector produced it (AC3: "mined by the same
    shape-clustering machinery ... feeding the same proposal drafter").
    """
    from athenaeum.resolution_claims import list_active_resolution_claims

    wiki_root = Path(wiki_root)
    now = now or datetime.now(timezone.utc)
    threshold = resolve_signal_mining_threshold(config)
    window_days = resolve_signal_mining_window_days(config)

    claims = [
        c
        for c in list_active_resolution_claims(wiki_root)
        if _within_claim_window(c, now=now, window_days=window_days)
    ]

    grouped: dict[ShapeKey, list[dict[str, Any]]] = {}
    for claim in claims:
        key = decision_shape_key(claim)
        grouped.setdefault(key, []).append(claim)

    shapes: list[MinedShape] = []
    for key, rows in grouped.items():
        ids = sorted({str(r.get("decision_id") or "") for r in rows})
        shapes.append(
            MinedShape(
                key=key,
                count=len(ids),
                example_pairs=tuple(ids[:3]),
                threshold=threshold,
                window_days=window_days,
            )
        )

    shapes.sort(key=lambda s: (-s.count, s.key.verdict_type, s.key.missing_dimensions))
    return shapes


__all__ = [
    "UNDERDETERMINED",
    "ShapeKey",
    "MinedShape",
    "mine_underdetermined_shapes",
    "shape_key_for_entry",
    "triggered_shapes",
    "decision_shape_key",
    "mine_decision_shapes",
]
