# SPDX-License-Identifier: Apache-2.0
"""Merge-engine type integration (issue #433).

The #424 taxonomy (``memory_class:`` — see :mod:`athenaeum.schemas` and
``docs/memory-taxonomy.md`` §3) defined merge-vs-cite semantics but shipped
no enforcement. This module is that enforcement, consumed by the mechanical
merge/proposal engine (:mod:`athenaeum.merge`, :mod:`athenaeum.wiki_dedupe`)
alongside the #421 guardrail chain (``_merge_proposal_suppression_reason``,
``_classify_merge_write_kind``):

1. **Type-compatibility precheck** (:func:`cross_class_precheck`) — a merge
   proposal may not cluster pages across incompatible ``memory_class``
   values. Same-class (or untyped-compatible) clusters pass unchanged;
   cross-class clusters are rejected at proposal time with a
   machine-readable :class:`CrossClassRejection` reason record, mirroring
   the ``provenance.py`` reason-record pattern.
2. **Merge-vs-cite routing** (:func:`build_cite_proposal`) — a rejected
   cross-class cluster is not simply dropped: a :class:`CiteProposal` is
   built in its place, naming the citing page(s) and the cited fact
   page(s). Unlike a merge, a cite proposal is NEVER destructive — no
   source page is folded, deleted, or overwritten; the citing page(s)
   gain a ``## Cites`` wikilink section pointing at the surviving fact
   page(s). Approving/enacting a cite proposal is a distinct, later
   concern (not wired into :func:`athenaeum.pending_merges.resolve_merge`'s
   create/fold dispatch) — see the module docstring note below.

Untyped-page policy (conservative default, documented per #433's
constraint): a page with NO ``memory_class`` (legacy/untyped) is treated as
compatible with any other class, INCLUDING other untyped pages and typed
pages — i.e. the precheck only fires when it can see two members carrying
*distinct, non-empty* ``memory_class`` values. This preserves every
pre-#433 untyped-page merge byte-for-byte (a corpus with no typed pages
never trips the new gate) while still catching the concrete case #433
targets: an explicitly-typed ``fact`` page clustering with an explicitly-
typed ``guideline`` page.

Inference-block retraction (the third #433 deliverable) lives in
:mod:`athenaeum.inference_blocks` (:func:`retract_inference_block`)
alongside the parser it retracts blocks parsed by, not here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from athenaeum.models import parse_frontmatter

log = logging.getLogger(__name__)


def read_memory_class(path: Path) -> str | None:
    """Read a page's ``memory_class:`` frontmatter value, or ``None``.

    ``None`` covers both "file unreadable" and "no (non-empty)
    ``memory_class``" — both are the untyped/unknown case for precheck
    purposes (see module docstring's untyped-page policy). Mirrors
    :func:`athenaeum.schemas.is_untyped_memory_class`'s predicate but reads
    straight off disk (the precheck runs on member *paths*, before/without
    a validated :class:`~athenaeum.schemas.WikiBase` instance).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    meta, _ = parse_frontmatter(text)
    if not isinstance(meta, dict):
        return None
    value = meta.get("memory_class")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


@dataclass
class CrossClassRejection:
    """Machine-readable reason record for a precheck rejection (#433).

    Mirrors the field shape of
    :func:`athenaeum.provenance.build_merge_provenance_record` (a flat,
    JSON-serializable dict of primitives) so a caller logging or persisting
    a rejection can treat it uniformly with the existing provenance
    records. ``reason`` is a short machine-readable code (stable, greppable
    — NOT the human-readable sentence); ``detail`` is the human-readable
    explanation for logs/UI.
    """

    reason: str
    classes_seen: dict[str, list[str]]
    detail: str

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict form (dict-of-primitives, like #433's other
        machine-readable records)."""
        return {
            "reason": self.reason,
            "classes_seen": dict(self.classes_seen),
            "detail": self.detail,
        }


#: Machine-readable rejection reason code: an incompatible cross-class cluster.
CROSS_CLASS_REJECTED = "cross_class_incompatible"


def cross_class_precheck(member_paths: list[str | Path]) -> CrossClassRejection | None:
    """Return a rejection record when *member_paths* span >1 ``memory_class``.

    Issue #433, part 1. Reads each member's ``memory_class:`` frontmatter
    (:func:`read_memory_class`). Members with NO (or unreadable/empty)
    ``memory_class`` are treated as compatible with everything (the
    conservative untyped policy — see module docstring) and do not
    themselves trigger a rejection, nor do they widen the "classes seen"
    set used to decide compatibility.

    Returns ``None`` (precheck PASSES) when:
    - fewer than 2 members, or
    - every member is untyped, or
    - every TYPED member shares the same ``memory_class`` value.

    Returns a :class:`CrossClassRejection` when 2+ DISTINCT non-empty
    ``memory_class`` values are present among the members — a cross-class
    cluster, which must not be merged (see ``docs/memory-taxonomy.md`` §3).
    """
    if len(member_paths) < 2:
        return None

    classes_seen: dict[str, list[str]] = {}
    for raw_path in member_paths:
        path = Path(raw_path)
        cls = read_memory_class(path)
        if cls is None:
            continue
        classes_seen.setdefault(cls, []).append(str(path))

    distinct = sorted(classes_seen)
    if len(distinct) <= 1:
        return None

    detail = (
        f"cluster spans {len(distinct)} distinct memory_class values "
        f"({', '.join(distinct)}); cross-class merges are not allowed "
        "(same-class only — see docs/memory-taxonomy.md #3)"
    )
    log.info("merge_type_gate: REJECTED cross-class cluster (%s)", detail)
    return CrossClassRejection(
        reason=CROSS_CLASS_REJECTED,
        classes_seen=classes_seen,
        detail=detail,
    )


@dataclass
class CiteProposal:
    """A merge-vs-cite consolidation proposal — NEVER destructive (#433).

    Distinct proposal kind from :class:`athenaeum.resolutions.MergeProposal`
    / :class:`athenaeum.pending_merges.PendingMerge`: a cite proposal never
    folds, deletes, or overwrites any source page. It records that one or
    more "citing" pages (typically the higher-order class — a ``guideline``
    or ``decision``) should carry a wikilink/``## Cites`` reference to one
    or more "cited" pages (typically ``fact`` pages) that justify them. All
    named pages SURVIVE unchanged; only a citation section on the citing
    page(s) is proposed, and even that is not auto-applied by
    :func:`athenaeum.pending_merges.resolve_merge` — enacting a cite
    proposal is a distinct, later write path, deliberately not implemented
    here (mirrors how the #421 ``write_kind`` precheck was classification-
    only, with the write path landing separately).

    Fields:
        citing: Paths of the page(s) that should CITE the others (the page
            whose class is either not a ``fact``, or — when all members are
            the same non-fact class — the newest/primary member; see
            :func:`route_cross_class_cluster`).
        cited: Paths of the page(s) being cited (survive untouched).
        rationale: One-sentence human-readable justification.
        rejection: The precheck rejection record that produced this
            proposal (the "why not a merge" reason), so a human reviewing
            the cite proposal sees the SAME machine-readable reason that
            was logged when the merge was rejected.
    """

    citing: list[str]
    cited: list[str]
    rationale: str
    rejection: CrossClassRejection
    action: str = "propose_cite"


def build_cite_proposal(
    member_paths: list[str | Path],
    rejection: CrossClassRejection,
) -> CiteProposal:
    """Build the cite-proposal shape for a cross-class cluster (#433 part 2).

    Routing rule: pages classed ``fact`` are always the CITED side (facts
    survive, never absorbed). Every other class present (``guideline``,
    ``decision``, ``axiom``, ``reference``, ``entity``, ``procedure``, or an
    untyped member) is the CITING side — it is the higher-order/derived
    memory that depends on the fact(s). When no member is classed ``fact``
    (e.g. a ``guideline``/``decision`` cross-class pair with no fact present),
    falls back to: the single largest class-group cites every other member
    (deterministic, alphabetically-first class wins a tie) — still never
    destructive, just a proposal for a human to confirm or redirect.
    """
    by_class: dict[str, list[str]] = {}
    untyped: list[str] = []
    for raw_path in member_paths:
        path_str = str(raw_path)
        cls = read_memory_class(Path(raw_path))
        if cls is None:
            untyped.append(path_str)
        else:
            by_class.setdefault(cls, []).append(path_str)

    cited = list(by_class.get("fact", []))
    if cited:
        citing = [
            p
            for cls, paths in by_class.items()
            if cls != "fact"
            for p in paths
        ] + untyped
    else:
        # No fact-classed member: deterministic fallback — alphabetically
        # first class present cites the rest.
        classes = sorted(by_class)
        primary_cls = classes[0]
        citing = list(by_class[primary_cls])
        cited = [
            p
            for cls, paths in by_class.items()
            if cls != primary_cls
            for p in paths
        ] + untyped

    rationale = (
        f"cross-class cluster ({rejection.detail}); citing page(s) should "
        "reference the surviving fact/source page(s) rather than merge "
        "with them (see docs/memory-taxonomy.md #3)"
    )
    return CiteProposal(
        citing=citing,
        cited=cited,
        rationale=rationale,
        rejection=rejection,
    )


__all__ = [
    "CROSS_CLASS_REJECTED",
    "CrossClassRejection",
    "CiteProposal",
    "read_memory_class",
    "cross_class_precheck",
    "build_cite_proposal",
]
