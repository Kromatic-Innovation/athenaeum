# SPDX-License-Identifier: Apache-2.0
"""Per-run attribution ledger for the wiki-dedup pass (issue athenaeum#1243) — L3.

Layering: L3 (service). Reads/writes one artifact and reuses
:mod:`athenaeum.clusters`' rotation helpers (L3) and
:mod:`athenaeum.verdicts`' pair-key/id functions (L2); its only consumer,
:mod:`athenaeum.wiki_dedupe`, is L4, so that edge points downward.

Re-sites the two things athenaeum#1142 guaranteed and athenaeum#1227's cut-over
stranded when it routed wiki-page dedup through the five-verdict comparator:

1. **Embedder identity is persisted, not merely computed.** On the cut-over
   branch :func:`athenaeum.wiki_dedupe.find_wiki_page_clusters` resolved
   ``embedder_sources`` and :class:`athenaeum.clusters.Cluster` carried the
   collapsed ``embedder`` value — and nothing read it. It was produced on
   every run and thrown away. Every row written here carries it.
2. **A pair that produces no verdict leaves a row.**
   :func:`athenaeum.comparator.record_comparison` returns ``ok=False`` with
   ``verdict=None`` when Gate 2 is unavailable or Gate 1 cannot settle, and
   its own docstring says "nothing ledgered"; a pair rejected by
   :func:`athenaeum.merge_type_gate.cross_class_precheck` never reaches the
   comparator at all. Both were a bare ``continue``, discarding the reason
   on the floor. Measured against the live corpus (athenaeum#1243's
   measurement comment) that is **100% of 22,040 pairs** — the comparator
   reaches a Gate 1 verdict for exactly zero of them today, so the hole is
   not an edge case, it is the whole pass.

Why a sibling ledger rather than a new :class:`athenaeum.verdicts.Basis`
field
----------------------------------------------------------------------

Two independent reasons, and the second is decisive.

**Semantics.** :func:`athenaeum.verdicts.build_verdict_entry` validates
``verdict`` against :data:`athenaeum.verdicts.VERDICT_VALUES`, so a pair
that reached *no* decision has no honest home in that ledger — writing one
of the five values for a pair the comparator did not settle would make
:func:`athenaeum.verdicts.can_authorize_auto_operation` reason over a
decision nobody made. ``Basis.null_reasons`` is not the home either: its
docstring scopes it to basis fields ("field name -> human-readable reason it
is null"), which explains why ``coords`` is ``None`` and has no vocabulary
for "this pair never became a proposal".

**Retention (AC4).** athenaeum#1243 AC4 asks that ``verdicts.compact()``'s
scheduling be *checked* before deciding where the re-sited row lands. It was
checked: as of this commit ``compact()`` has **no production call site
anywhere in the repo** — the only callers are ``tests/test_verdicts.py`` and
a prose mention in ``docs/reference/configuration.md``. So ``wiki/_verdicts/`` is an
unbounded-append artifact in production *today*, and re-siting a
one-row-per-examined-pair record into it would add ~22,040 rows per nightly
run to an artifact nothing prunes — precisely the athenaeum#1229 failure
mode (a 1.4M-row unbounded ledger) that athenaeum#1142 deliberately
contrasted itself against. (That absent schedule is a real defect in its own
right; it is reported separately rather than fixed here, since scheduling
compaction is a librarian-phase change outside this issue's blast radius.)

So this ledger is bounded **by construction**, reusing athenaeum#1142's
shape verbatim:

- ``wiki/_wiki_dedupe_attribution.jsonl`` — the canonical **snapshot**,
  atomically REPLACED on every real run, so it always reflects *this* run's
  state and never accumulates. Written even when the run examined zero
  pairs (an empty file), so a stale prior run's rows can never be misread
  as current.
- ``wiki/_wiki_dedupe_attribution-<YYYYmmddTHHMMSSZ>.jsonl`` — timestamped
  rotations, pruned to :func:`athenaeum.clusters.resolve_rotation_retention`
  (``librarian.rotation_retention``, default 30) via
  :func:`athenaeum.clusters.prune_cluster_rotations`. The SAME retention
  knob and the SAME pruning helper ``raw/_librarian-clusters.jsonl``
  already uses — reused, not a second retention policy.

A dry run never writes either file: it decides nothing and enacts nothing,
so it has no run state to snapshot.

Access
------

:func:`read_attribution_report` is the sanctioned reader, mirroring this
repo's rule that ``wiki/_verdicts/*.jsonl`` is only read via
:mod:`athenaeum.verdicts` and the pending-merges sidecar only via
:func:`athenaeum.pending_merges.parse_pending_merges` — hand-parsing this
file is not a supported access pattern. :func:`explain_pair` is
athenaeum#1243 AC3's diagnostic: one artifact read, no live host log
access, answering "which embedder produced this pair's candidacy, and why
did it not become a proposal?" — the exact question two independent
diagnostic passes (athenaeum#1005) failed to answer, each reaching a wrong
conclusion.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from athenaeum.atomic_io import atomic_write_text
from athenaeum.clusters import (
    EMBEDDER_UNKNOWN,
    prune_cluster_rotations,
    resolve_rotation_retention,
)
from athenaeum.verdicts import make_pair_key, page_id_for_path

log = logging.getLogger(__name__)

#: Row schema version, stamped on every :class:`AttributionRow` so a future
#: reader can migrate an older on-disk shape. Mirrors
#: :data:`athenaeum.verdicts.SCHEMA_VERSION`'s role for the verdict ledger.
SCHEMA_VERSION = 1

#: Canonical snapshot filename, under ``<wiki_root>/``. Deliberately named
#: for what it records (attribution for every examined pair) rather than
#: athenaeum#1142's narrower ``_wiki_suppressions.jsonl`` — the comparator
#: path has no "suppression" step to name, and this ledger covers decided
#: pairs too so a single read answers AC3 without a second artifact.
DEFAULT_ATTRIBUTION_FILENAME = "_wiki_dedupe_attribution.jsonl"

# --- Outcome vocabulary --------------------------------------------------
#
# Stable, greppable machine codes (never the human sentence), same posture
# as :class:`athenaeum.merge_type_gate.CrossClassRejection.reason`. The
# first four are the athenaeum#1243 AC1 hole; the last two are recorded so
# AC1's "EVERY candidate pair the pass examines leaves a durable row" is
# literally true and AC3 needs exactly one artifact read.

#: ``cross_class_precheck`` rejected the pair before any comparison.
OUTCOME_CROSS_CLASS_REJECTED = "cross-class-rejected"

#: ``record_comparison`` returned ``ok=False``: Gate 2 unavailable, or Gate 1
#: could not settle the pair. ``reason`` carries the comparator's own code.
OUTCOME_NO_VERDICT = "no-verdict"

#: One side (or both) could not be read off disk.
OUTCOME_READ_ERROR = "read-error"

#: The pair reached a verdict on a PRIOR run and is still fresh, so this run
#: deliberately did not re-decide it (``record_comparison``'s memoization).
OUTCOME_FRESH = "fresh"

#: The pair was freshly decided this run and IS in ``wiki/_verdicts/``;
#: ``verdict`` and ``action`` are populated and ``pair`` is the ledger key.
OUTCOME_DECIDED = "decided"

OUTCOME_VALUES: tuple[str, ...] = (
    OUTCOME_CROSS_CLASS_REJECTED,
    OUTCOME_NO_VERDICT,
    OUTCOME_READ_ERROR,
    OUTCOME_FRESH,
    OUTCOME_DECIDED,
)

#: The outcomes that mean "this candidate pair did NOT become a proposal" —
#: the half athenaeum#1243 was filed for. :func:`explain_pair` reports this
#: as ``became_proposal``'s inverse.
NON_PROPOSAL_OUTCOMES: frozenset[str] = frozenset(
    {OUTCOME_CROSS_CLASS_REJECTED, OUTCOME_NO_VERDICT, OUTCOME_READ_ERROR}
)


@dataclass
class AttributionRow:
    """One examined candidate pair's durable attribution record.

    ``pair`` is :func:`athenaeum.verdicts.make_pair_key`'s canonical
    order-independent key, so a row here joins directly to a
    ``wiki/_verdicts/`` entry for the same pair without a second id space.
    """

    pair: str
    outcome: str
    embedder: str = EMBEDDER_UNKNOWN
    reason: str = ""
    detail: str = ""
    sources: list[str] = field(default_factory=list)
    cluster_id: str = ""
    cluster_threshold: float | None = None
    n_cluster_members: int | None = None
    verdict: str | None = None
    action: str | None = None
    at: str = ""
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "pair": self.pair,
            "outcome": self.outcome,
            "embedder": self.embedder,
            "reason": self.reason,
            "detail": self.detail,
            "sources": list(self.sources),
            "cluster_id": self.cluster_id,
            "cluster_threshold": self.cluster_threshold,
            "n_cluster_members": self.n_cluster_members,
            "verdict": self.verdict,
            "action": self.action,
            "at": self.at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AttributionRow:
        return cls(
            pair=str(d.get("pair", "")),
            outcome=str(d.get("outcome", "")),
            embedder=str(d.get("embedder") or EMBEDDER_UNKNOWN),
            reason=str(d.get("reason") or ""),
            detail=str(d.get("detail") or ""),
            sources=list(d.get("sources") or []),
            cluster_id=str(d.get("cluster_id") or ""),
            cluster_threshold=d.get("cluster_threshold"),
            n_cluster_members=d.get("n_cluster_members"),
            verdict=d.get("verdict"),
            action=d.get("action"),
            at=str(d.get("at") or ""),
            schema_version=int(d.get("schema_version", SCHEMA_VERSION)),
        )

    @property
    def became_proposal(self) -> bool:
        """Whether this candidate pair reached a durable verdict.

        ``True`` for a freshly-decided or still-fresh memoized pair (both
        have a ``wiki/_verdicts/`` entry); ``False`` for every
        :data:`NON_PROPOSAL_OUTCOMES` row.
        """
        return self.outcome not in NON_PROPOSAL_OUTCOMES


def build_attribution_row(
    path_a: Path | str,
    path_b: Path | str,
    outcome: str,
    *,
    embedder: str = EMBEDDER_UNKNOWN,
    reason: str = "",
    detail: str = "",
    cluster_id: str = "",
    cluster_threshold: float | None = None,
    n_cluster_members: int | None = None,
    verdict: str | None = None,
    action: str | None = None,
    pair: str | None = None,
    at: str | None = None,
) -> AttributionRow:
    """Build one :class:`AttributionRow`, validating *outcome*.

    *pair* defaults to :func:`athenaeum.verdicts.make_pair_key` over the two
    paths' slugs (:func:`athenaeum.verdicts.page_id_for_path`) — derivable
    from the paths alone, which is what lets a cross-class rejection or a
    read error (neither of which ever builds a
    :class:`athenaeum.comparator.ComparatorPage`) still produce a row keyed
    identically to a decided pair's. Pass it explicitly to reuse the key
    :func:`athenaeum.comparator.record_comparison` already returned rather
    than re-deriving it.
    """
    if outcome not in OUTCOME_VALUES:
        raise ValueError(f"outcome must be one of {OUTCOME_VALUES!r}, got {outcome!r}")
    if pair is None:
        pair = make_pair_key(page_id_for_path(Path(path_a)), page_id_for_path(Path(path_b)))
    return AttributionRow(
        pair=pair,
        outcome=outcome,
        embedder=embedder or EMBEDDER_UNKNOWN,
        reason=reason,
        detail=detail,
        sources=[str(path_a), str(path_b)],
        cluster_id=cluster_id,
        cluster_threshold=cluster_threshold,
        n_cluster_members=n_cluster_members,
        verdict=verdict,
        action=action,
        at=at or datetime.now(timezone.utc).isoformat(),
    )


def attribution_path(
    wiki_root: Path, *, filename: str = DEFAULT_ATTRIBUTION_FILENAME
) -> Path:
    """Canonical snapshot path for *wiki_root*."""
    return Path(wiki_root) / filename


def write_attribution_report(
    rows: list[AttributionRow],
    wiki_root: Path,
    *,
    knowledge_root: Path,
    config: dict[str, Any] | None = None,
) -> tuple[Path, Path | None]:
    """Write *rows* as the canonical snapshot + a pruned timestamped rotation.

    The canonical file is atomically REPLACED (never appended to), so it is
    always exactly this run's state — an empty ``rows`` writes an empty
    file rather than leaving a prior run's rows in place to be misread as
    current. A timestamped rotation sibling is written alongside and the
    rotation set is then pruned to
    :func:`athenaeum.clusters.resolve_rotation_retention`, reusing
    :func:`athenaeum.clusters.prune_cluster_rotations` — the same helper and
    the same ``librarian.rotation_retention`` knob
    ``raw/_librarian-clusters.jsonl`` uses.

    Returns ``(canonical_path, rotation_path)``. Mirrors
    :func:`athenaeum.clusters.write_cluster_report`'s signature and
    rotation idiom deliberately.
    """
    canonical = attribution_path(wiki_root)
    canonical.parent.mkdir(parents=True, exist_ok=True)

    lines = [json.dumps(row.to_dict(), sort_keys=True) for row in rows]
    text = "\n".join(lines) + ("\n" if lines else "")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rotation = canonical.with_name(f"{canonical.stem}-{stamp}{canonical.suffix}")
    atomic_write_text(rotation, text)
    atomic_write_text(canonical, text)

    keep = resolve_rotation_retention(Path(knowledge_root), config=config)
    pruned = prune_cluster_rotations(canonical, keep=keep)
    if pruned:
        log.debug(
            "wiki-dedupe attribution: pruned %d rotation(s) beyond retention %d",
            len(pruned),
            keep,
        )
    return canonical, rotation


def read_attribution_report(
    wiki_root: Path, *, filename: str = DEFAULT_ATTRIBUTION_FILENAME
) -> list[AttributionRow]:
    """The sanctioned reader for the canonical snapshot.

    Returns ``[]`` when the file does not exist (the pass has not run, or
    ran dark). Tolerates a torn trailing line the same way
    :mod:`athenaeum.verdicts`' own JSONL readers do — a crash mid-write can
    at worst cost the last row, never the whole artifact.
    """
    path = attribution_path(wiki_root, filename=filename)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    rows: list[AttributionRow] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(AttributionRow.from_dict(payload))
    return rows


def explain_pair(
    wiki_root: Path,
    pair_key: str,
    *,
    filename: str = DEFAULT_ATTRIBUTION_FILENAME,
) -> dict[str, Any] | None:
    """athenaeum#1243 AC3: answer athenaeum#1005's diagnostic question.

    ONE artifact read, with no live host log access, answering "which
    embedder produced this pair's candidacy, and why did it not become a
    proposal?" for *pair_key* (a
    :func:`athenaeum.verdicts.make_pair_key` key).

    Returns ``None`` when this run's snapshot has no row for the pair —
    which is itself the answer "this pair was never examined by the run
    that wrote this snapshot", distinguishable from "examined and not
    settled" precisely because AC1 guarantees an examined pair always has a
    row.
    """
    for row in read_attribution_report(wiki_root, filename=filename):
        if row.pair == pair_key:
            return {
                "pair": row.pair,
                "embedder": row.embedder,
                "became_proposal": row.became_proposal,
                "outcome": row.outcome,
                "reason": row.reason,
                "detail": row.detail,
                "verdict": row.verdict,
                "sources": list(row.sources),
                "cluster_id": row.cluster_id,
                "cluster_threshold": row.cluster_threshold,
                "at": row.at,
            }
    return None


#: :func:`athenaeum.comparator.record_comparison`'s ``reason`` code for a pair
#: refused because either side is erasure-class (pii-flagged). The ONE
#: deliberate carve-out from athenaeum#1243 AC1's "every examined pair leaves a
#: row": this snapshot lives in-git under ``wiki/`` exactly like
#: ``wiki/_verdicts/``, and :func:`athenaeum.verdicts.refuse_if_erasure_class`'s
#: posture — erasure-class content is never written into an in-git ledger —
#: outranks an observability AC. The comparator already emits a WARNING naming
#: the pair, so the evidence is not lost, only kept out of the artifact.
#: Unreachable in practice today:
#: :func:`athenaeum.wiki_dedupe.discover_wiki_dedupe_candidates` drops
#: pii-flagged pages before clustering.
ERASURE_CLASS_REFUSED_REASON = "erasure_class_refused"
