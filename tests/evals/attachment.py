# SPDX-License-Identifier: Apache-2.0
"""Structural grading for the intake-attachment eval (issue athenaeum#1580).

The question this layer exists to answer, which no other layer asks: given a
wiki that ALREADY HAS a page for X, and a new raw source that is about X, does
the librarian land the source on X's page -- or mint a second page?

Everything here is offline and deterministic. The metered half (running
``librarian.process_one`` against a real tier chain) lives in
``tests/evals/test_attachment_eval.py``; this module only takes a BEFORE and
an AFTER picture of a materialized wiki and says what moved. Splitting it
that way is what lets the grader itself be unit-tested in ordinary CI
(``tests/evals/test_attachment_grading.py``) rather than only exercised on a
live run.

Three things in here are load-bearing and easy to get wrong:

**1. Delta, not absolute state.** "Did ``related:``/``sources:`` gain an edge
to this source" is not answerable from the final wiki alone -- the corpus
already carries authored edges. It is only answerable as a difference, so
:func:`snapshot_wiki` runs before AND after and :func:`diff_wiki` subtracts.

**2. An edge the ATTACHMENT decision created is not an edge the relatedness
writer stamped.** Since issue athenaeum#1576 a compile-time writer
(``athenaeum.relatedness.stamp_related_edges``, called from
``librarian.py:1694``) stamps ``related:`` rows with
``role: term-overlap`` onto every NEWLY CREATED entity, by mutual k-NN over
corpus-weighted distinctive-term overlap, gated by
``resolve_relatedness_writer_enabled`` (DEFAULT ON) above a 50-page index
floor. The eval corpus's ``core`` scale is ~96 pages, so the writer FIRES on
every run of this layer.

That is exactly the confound this layer must not fall into. A librarian that
does the WRONG thing -- mints a second page for an entity that already has one
-- produces a new page whose ``related:`` block very plausibly points straight
at the page it should have attached to, because the two share the entity's
distinctive vocabulary. Counting that as "the source attached" would grade the
relatedness writer's incidental correctness as an attachment success, and the
eval would pass hardest exactly where the librarian failed worst.

:data:`ROLE_TERM_OVERLAP` edges are therefore excluded from
:attr:`WikiDelta.attachment_edges` and reported separately as
:attr:`WikiDelta.incidental_edges`. Both are surfaced in the observation
string so a reader can see the writer fired and see that it was not what
scored.

**3. Irreversibility is graded as a PROPOSAL (``docs/north-star.md`` §2.8).**
Folding two pages into one, or demoting a compiled page to an uncompiled
source document, are not changes a compile may simply APPLY. The grader
therefore never accepts "the page is gone" as evidence of consolidation --
:attr:`WikiDelta.removed_uids` is a failure signal, and the pass condition for
a consolidation-shaped case is a pending-queue surface having grown.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from athenaeum.relatedness import ROLE_TERM_OVERLAP

#: The pending-decision surfaces a PROPOSAL can land on, and the only ones.
#:
#: Not "every ``wiki/_*.md``". A compile writes other underscore-prefixed
#: bookkeeping into the wiki root -- the athenaeum#1196 type guard parks
#: rejected pages under ``_type_rejected/`` and appends
#: ``_type_rejected.jsonl``, and that fired for real during this layer's own
#: baseline run. A rejected write is not a proposal, and a grader that
#: accepted any growing underscore file as one would let ``requires_proposal``
#: be satisfied by a page the librarian FAILED to write.
PENDING_SURFACES = ("_pending_merges.md", "_pending_questions.md")

__all__ = [
    "PENDING_SURFACES",
    "PageState",
    "WikiSnapshot",
    "WikiDelta",
    "TierAttribution",
    "snapshot_wiki",
    "diff_wiki",
    "attribute_tier",
    "score_case",
]


# ---------------------------------------------------------------------------
# Snapshotting a materialized wiki
# ---------------------------------------------------------------------------


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _parse_page(text: str) -> tuple[dict[str, Any], str]:
    """Split a page into (frontmatter mapping, body).

    Routed through ``athenaeum.models.parse_frontmatter`` -- the same parser
    the librarian itself reads pages with -- so the grader can never disagree
    with production about what a page's frontmatter says.
    """
    from athenaeum.models import parse_frontmatter

    meta, body = parse_frontmatter(text)
    return dict(meta or {}), body or ""


def _edges_of(meta: Mapping[str, Any]) -> frozenset[tuple[str, str]]:
    """Normalise ``related:`` into a ``{(target_uid, role)}`` set.

    Accepts both live shapes: the ``{uid, role}`` mapping
    (``WikiEntity.related: list[dict[str, str]]``) and a bare string target,
    which older pages and some hand-authored fixtures still carry.
    """
    edges: set[tuple[str, str]] = set()
    for entry in meta.get("related") or ():
        if isinstance(entry, dict):
            uid = str(entry.get("uid", "")).strip()
            role = str(entry.get("role", "related")).strip() or "related"
        else:
            uid, role = str(entry).strip(), "related"
        if uid:
            edges.add((uid, role))
    return frozenset(edges)


def _source_refs_of(meta: Mapping[str, Any]) -> frozenset[str]:
    """Every provenance pointer the page declares, as a flat set.

    ``source_ref`` is the single-value spelling and ``sources`` the list one;
    a page may carry either. Both are collected because "did this page gain a
    pointer to the new source" is the question, not which key it landed under.
    """
    refs: set[str] = set()
    single = meta.get("source_ref")
    if isinstance(single, str) and single.strip():
        refs.add(single.strip())
    raw_sources = meta.get("sources")
    if isinstance(raw_sources, str):
        raw_sources = [raw_sources]
    for entry in raw_sources or ():
        if isinstance(entry, dict):
            value = entry.get("ref") or entry.get("source_ref") or entry.get("path")
        else:
            value = entry
        if isinstance(value, str) and value.strip():
            refs.add(value.strip())
    return frozenset(refs)


@dataclass(frozen=True)
class PageState:
    """One page as the grader cares about it.

    ``body_digest`` rather than the body itself: the grader asks whether a
    page CHANGED, never what it now says. Prose assertions are the other
    layers' business (``merge``, ``underdetermined``); conflating them here
    would make an attachment result hostage to model wording.
    """

    uid: str
    stem: str
    name: str
    type: str
    edges: frozenset[tuple[str, str]]
    source_refs: frozenset[str]
    body_digest: str
    body_bytes: int = 0

    @property
    def attachment_edges(self) -> frozenset[tuple[str, str]]:
        """Edges NOT stamped by the athenaeum#1576 relatedness writer."""
        return frozenset((uid, role) for uid, role in self.edges if role != ROLE_TERM_OVERLAP)


@dataclass(frozen=True)
class WikiSnapshot:
    """Every compiled page plus every pending-decision surface.

    The queue surfaces (:data:`PENDING_SURFACES`) are snapshotted alongside
    the pages precisely because AC4 grades irreversible outcomes as
    PROPOSALS: without the queue in the same picture, "proposed a merge" and
    "did nothing" are the same observation.
    """

    pages: Mapping[str, PageState]
    queues: Mapping[str, str]

    def uids(self) -> frozenset[str]:
        return frozenset(self.pages)

    def name_of(self, uid: str) -> str:
        page = self.pages.get(uid)
        return (page.name if page else "") or ""


def snapshot_wiki(wiki_root: Path) -> WikiSnapshot:
    """Read *wiki_root* into a :class:`WikiSnapshot`.

    Pages are keyed by frontmatter ``uid`` where one exists, falling back to
    the file stem. Keying on the uid rather than the filename matters: the
    corpus materializes ``<uid>.md`` while the real librarian writes
    ``<uid>-<slug>.md``, so a filename key would report every page the
    librarian touched as both a removal and a mint.
    """
    pages: dict[str, PageState] = {}
    queues: dict[str, str] = {}
    if not wiki_root.is_dir():
        return WikiSnapshot(pages={}, queues={})

    for path in sorted(wiki_root.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        if path.name.startswith("_"):
            if path.name in PENDING_SURFACES:
                # Raw text, not a digest: diff_wiki (athenaeum#1598) needs the
                # actual appended content to tell WHICH uid a proposal names,
                # not merely that the surface grew.
                queues[path.name] = text
            continue
        meta, body = _parse_page(text)
        uid = str(meta.get("uid") or "").strip() or path.stem
        pages[uid] = PageState(
            uid=uid,
            stem=path.stem,
            name=str(meta.get("name") or ""),
            type=str(meta.get("type") or ""),
            edges=_edges_of(meta),
            source_refs=_source_refs_of(meta),
            body_digest=_digest(body),
            body_bytes=len(body.encode("utf-8")),
        )
    return WikiSnapshot(pages=pages, queues=queues)


# ---------------------------------------------------------------------------
# The delta
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WikiDelta:
    """What one intake run did to the wiki.

    ``touched_uids`` is the attachment signal: a page that already existed and
    whose body, provenance, or non-``term-overlap`` edges changed. ``minted``
    is the counter-signal. A correct answer for cases A/B/C/E has a non-empty
    ``touched_uids`` and an empty ``minted``; case D is the exact inverse, and
    that inversion is why the grader takes bounds per case rather than
    asserting one global invariant.
    """

    minted: frozenset[str]
    removed_uids: frozenset[str]
    touched_uids: frozenset[str]
    attachment_edges: Mapping[str, frozenset[tuple[str, str]]]
    incidental_edges: Mapping[str, frozenset[tuple[str, str]]]
    gained_source_refs: Mapping[str, frozenset[str]]
    grown_queues: frozenset[str]
    minted_names: Mapping[str, str] = field(default_factory=dict)
    proposed_uids: frozenset[str] = frozenset()
    #: athenaeum#1595 (Case C): a mint's TYPE is what tells a legitimate thin
    #: ``type: source`` page apart from a duplicate entity page -- a check
    #: that reads only ``minted_names`` cannot make that distinction.
    minted_types: Mapping[str, str] = field(default_factory=dict)
    #: athenaeum#1595 AC3: body size of each minted page, so a case can pin
    #: a source page's thinness without reading prose.
    minted_body_bytes: Mapping[str, int] = field(default_factory=dict)

    @property
    def proposed(self) -> bool:
        """Whether ANY pending-decision surface grew.

        Whole-run, unscoped -- this is exactly the shape athenaeum#1598 found
        gameable when used to satisfy a per-uid check. It stays as a coarse
        summary signal; ``proposed_uids`` is what per-uid grading must use.
        """
        return bool(self.grown_queues)


def diff_wiki(before: WikiSnapshot, after: WikiSnapshot) -> WikiDelta:
    """Subtract *before* from *after*.

    Edges are partitioned on ``role``: everything that is not
    :data:`~athenaeum.relatedness.ROLE_TERM_OVERLAP` counts as an edge the
    intake decision produced, and ``term-overlap`` rows are set aside as
    incidental. See this module's docstring for why that split is the whole
    reason this function exists rather than a plain set difference.
    """
    minted = frozenset(after.uids() - before.uids())
    removed = frozenset(before.uids() - after.uids())

    touched: set[str] = set()
    attachment_edges: dict[str, frozenset[tuple[str, str]]] = {}
    incidental_edges: dict[str, frozenset[tuple[str, str]]] = {}
    gained_refs: dict[str, frozenset[str]] = {}

    for uid, now in after.pages.items():
        was = before.pages.get(uid)
        new_edges = now.edges - (was.edges if was else frozenset())
        new_attachment = frozenset((t, r) for t, r in new_edges if r != ROLE_TERM_OVERLAP)
        new_incidental = frozenset((t, r) for t, r in new_edges if r == ROLE_TERM_OVERLAP)
        if new_attachment:
            attachment_edges[uid] = new_attachment
        if new_incidental:
            incidental_edges[uid] = new_incidental
        new_refs = now.source_refs - (was.source_refs if was else frozenset())
        if new_refs:
            gained_refs[uid] = new_refs
        if was is None:
            continue
        # A pre-existing page counts as TOUCHED on any change the intake
        # decision could have caused. A page that gained only term-overlap
        # edges is deliberately NOT touched -- but note the athenaeum#1576
        # writer only stamps NEWLY CREATED entities, so that combination
        # should never arise on an existing page; the check is a belt on
        # top of that, not a load-bearing assumption about the writer.
        if (
            now.body_digest != was.body_digest
            or new_attachment
            or new_refs
            or now.name != was.name
            or now.type != was.type
        ):
            touched.add(uid)

    grown = {name for name, text in after.queues.items() if before.queues.get(name, "") != text}

    # athenaeum#1598: which uid(s) a proposal actually NAMES, not merely that
    # some pending-decision surface grew. ``write_pending_merge`` appends
    # (never rewrites) an existing block, so the newly-appended suffix is the
    # proposal text; a name (checked against BEFORE and AFTER pages, so a
    # proposal about a page the run also removed still resolves) appearing in
    # that suffix is the same substring-match idiom
    # ``must_not_mint_name_substrings`` already uses elsewhere in this
    # grader, applied in the other direction.
    known_names: dict[str, str] = {}
    for uid, page in before.pages.items():
        if page.name:
            known_names.setdefault(uid, page.name)
    for uid, page in after.pages.items():
        if page.name:
            known_names[uid] = page.name

    proposed_uids: set[str] = set()
    for name in grown:
        before_text = before.queues.get(name, "")
        after_text = after.queues.get(name, "")
        added = after_text[len(before_text) :] if after_text.startswith(before_text) else after_text
        added_lower = added.lower()
        for uid, page_name in known_names.items():
            if page_name.strip() and page_name.strip().lower() in added_lower:
                proposed_uids.add(uid)

    return WikiDelta(
        minted=minted,
        removed_uids=removed,
        touched_uids=frozenset(touched),
        attachment_edges=attachment_edges,
        incidental_edges=incidental_edges,
        gained_source_refs=gained_refs,
        grown_queues=frozenset(grown),
        proposed_uids=frozenset(proposed_uids),
        minted_names={uid: after.pages[uid].name for uid in minted},
        minted_types={uid: after.pages[uid].type for uid in minted},
        minted_body_bytes={uid: after.pages[uid].body_bytes for uid in minted},
    )


# ---------------------------------------------------------------------------
# Tier attribution (AC2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TierAttribution:
    """Which tier actually made the routing call, OBSERVED not declared.

    Derived from the ordered sequence of models the run called plus the
    ``ProcessingResult`` counters -- never from a field in ``cases.yaml``. A
    fixture that states its own expected tier and gets it echoed back would
    make AC2 vacuous: it would report the author's guess, not what happened.

    ``decided_by`` collapses the counters to one label under a documented
    precedence, most-expensive-wins:

    ``escalation`` > ``write_merge`` > ``classify`` > ``deterministic``

    Most-expensive-wins is the direction that keeps the metric honest. AC2
    exists so "a pass bought with the expensive tier is visible as such"; a
    precedence that reported the CHEAPEST tier involved would hide exactly
    the case the criterion is about, since Tier 1 runs on every file.
    """

    matched: int
    classify_calls: int
    write_calls: int
    other_calls: int
    escalated: int

    @property
    def decided_by(self) -> str:
        if self.escalated:
            return "escalation"
        if self.write_calls:
            return "write_merge"
        if self.classify_calls:
            return "classify"
        if self.matched:
            return "deterministic"
        return "none"

    def describe(self) -> str:
        return (
            f"tier={self.decided_by} matched={self.matched} "
            f"classify_calls={self.classify_calls} write_calls={self.write_calls} "
            f"other_calls={self.other_calls} escalated={self.escalated}"
        )


def attribute_tier(
    call_models: list[str],
    *,
    matched: int,
    escalated: int,
    classify_model: str,
    write_model: str,
) -> TierAttribution:
    """Bucket an observed call sequence into per-tier counts.

    Matching is by model-id PREFIX on the family segment rather than exact
    equality, because ``athenaeum.yaml`` and the ``models:`` config section
    can pin a dated snapshot of the same family (``claude-haiku-4-5`` vs
    ``claude-haiku-4-5-20251001``); an exact match would silently bucket every
    call as ``other`` and report ``deterministic`` for a run that spent real
    money.
    """

    def _family(model: str) -> str:
        parts = model.split("-")
        # Drop a trailing YYYYMMDD snapshot segment, keep the family.
        if parts and len(parts[-1]) == 8 and parts[-1].isdigit():
            parts = parts[:-1]
        return "-".join(parts)

    classify_family = _family(classify_model)
    write_family = _family(write_model)

    classify_calls = write_calls = other_calls = 0
    for model in call_models:
        family = _family(model)
        if family == classify_family:
            classify_calls += 1
        elif family == write_family:
            write_calls += 1
        else:
            other_calls += 1

    return TierAttribution(
        matched=matched,
        classify_calls=classify_calls,
        write_calls=write_calls,
        other_calls=other_calls,
        escalated=escalated,
    )


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def score_case(case: Mapping[str, Any], delta: WikiDelta) -> tuple[bool, str]:
    """Score one attachment case against the observed wiki delta.

    Every check is STRUCTURAL -- page counts, which uids moved, whether a
    queue surface grew. No check reads prose. Supported ``expected`` keys:

    ``min_new_pages`` / ``max_new_pages``
        Bounds on newly minted pages. Case D wants ``min 1``; A/B/C/E want
        ``max 0``.
    ``touch_or_proposal_uids``
        Pages that already existed and must either show a change attributable
        to this source OR have a pending-decision surface grow NAMING that
        uid (``delta.proposed_uids`` -- athenaeum#1598: a proposal about a
        DIFFERENT page no longer satisfies this). The issue's own wording is
        "merge into the existing page (or a proposal to)" -- both arms are
        correct routing, and which arm a librarian takes is a policy
        question this layer deliberately does not settle. The observation
        string names the arm that satisfied it, so a run that passes
        entirely by proposal is visible as such.
    ``must_not_mint_name_substrings``
        A mint whose ``name`` carries one of these is the specific failure
        the case is built to catch (a second page for an entity that has
        one), reported by name rather than as a bare count so the failure
        detail names the page. Exempts ``type: source`` mints (athenaeum#1595):
        a thin source page is BY DESIGN and may legitimately carry the
        subject's name (e.g. "Steepgate onboarding discovery board") without
        being the duplicate-entity-page failure this check exists to catch --
        that failure is a second page of an ENTITY type, never a source page.
    ``mint_types_must_be``
        athenaeum#1595 AC1: every minted page's ``type`` must be in this list,
        or the mint is a failure -- named by type rather than inferred from
        its name, so a duplicate entity page cannot hide behind a name this
        layer's substring list didn't anticipate. Case C's ground truth is
        exactly this: minting a ``type: source`` page is correct; minting any
        other type for an entity that already has a page is not.
    ``source_mint_link_uid``
        athenaeum#1595 AC2: if a ``type: source`` page was minted, it must be
        REACHABLE from this uid -- via a non-term-overlap edge either
        direction, a ``sources:``/``source_ref`` entry this uid GAINED naming
        the minted page, or a pending proposal naming the minted page
        (``delta.proposed_uids``). Deliberately NOT the minted page's own
        ``source_ref``: every page (the mint included) carries one to its raw
        intake file, which is provenance, not a link to the entity it
        evidences -- reading that would make every mint pass this check
        vacuously. A thin source page that nothing links to is an orphan, and
        is a failure even though minting it was legitimate. A no-op when no
        ``type: source`` page was minted.
    ``max_source_body_bytes``
        athenaeum#1595 AC3: caps the body size of any minted ``type: source``
        page. Thinness is the design intent (the trustworthiness-marking,
        progressive-disclosure page shape), not an incidental property.
    ``requires_proposal``
        AC4: the outcome is irreversible, so a pending-decision surface must
        have grown NAMING one of ``touch_or_proposal_uids`` (athenaeum#1598)
        -- a case using this key must also set ``touch_or_proposal_uids``, or
        there is nothing to scope the proposal check against and the check
        fails closed. Never satisfied by an applied change.
    ``forbid_page_removal``
        AC4's other half: no page may VANISH. A consolidation that deleted
        the redundant page applied an irreversible act instead of proposing
        it, and must score as a failure however tidy the result looks.
    """
    expected = dict(case.get("expected") or {})
    reasons: list[str] = []

    min_new = expected.get("min_new_pages")
    if min_new is not None and len(delta.minted) < int(min_new):
        reasons.append(f"minted {len(delta.minted)} pages < min {min_new}")

    max_new = expected.get("max_new_pages")
    if max_new is not None and len(delta.minted) > int(max_new):
        minted_desc = ", ".join(sorted(f"{u}={n!r}" for u, n in delta.minted_names.items()))
        reasons.append(f"minted {len(delta.minted)} pages > max {max_new} ({minted_desc})")

    touch_or_proposal_uids = expected.get("touch_or_proposal_uids", []) or []
    for uid in touch_or_proposal_uids:
        if uid not in delta.touched_uids and uid not in delta.proposed_uids:
            reasons.append(
                f"existing page {uid!r} was neither touched nor proposed against "
                "— the source did not reach the entity it is about"
            )

    for substr in expected.get("must_not_mint_name_substrings", []) or []:
        hits = sorted(
            f"{uid}={name!r}"
            for uid, name in delta.minted_names.items()
            if delta.minted_types.get(uid) != "source" and substr.lower() in (name or "").lower()
        )
        if hits:
            reasons.append(f"minted a page named for {substr!r}: {', '.join(hits)}")

    allowed_mint_types = expected.get("mint_types_must_be")
    if allowed_mint_types is not None:
        bad = sorted(
            f"{uid}={delta.minted_names.get(uid, '')!r} (type={mtype!r})"
            for uid, mtype in delta.minted_types.items()
            if mtype not in allowed_mint_types
        )
        if bad:
            reasons.append(
                f"minted page(s) not of an allowed type {list(allowed_mint_types)!r}: "
                f"{', '.join(bad)}"
            )

    link_target = expected.get("source_mint_link_uid")
    if link_target:
        source_mints = [uid for uid, mtype in delta.minted_types.items() if mtype == "source"]
        for uid in source_mints:
            # NOT ``delta.gained_source_refs.get(uid)`` -- every page (the
            # minted source page included) carries its OWN ``source_ref`` to
            # the raw intake file it was compiled from, which is provenance,
            # not a link to the entity it evidences. What must gain a
            # ``sources:``/``source_ref`` entry NAMING the minted page is the
            # ENTITY side, mirroring the edge direction below.
            reachable = (
                any(target == link_target for target, _role in delta.attachment_edges.get(uid, ()))
                or any(
                    target == uid for target, _role in delta.attachment_edges.get(link_target, ())
                )
                or any(uid in ref for ref in delta.gained_source_refs.get(link_target, ()))
                or uid in delta.proposed_uids
            )
            if not reachable:
                reasons.append(
                    f"minted source page {uid!r} is orphaned — not linked to "
                    f"{link_target!r} by an edge, a source_ref, or a proposal"
                )

    max_source_bytes = expected.get("max_source_body_bytes")
    if max_source_bytes is not None:
        oversize = sorted(
            f"{uid}={nbytes}B"
            for uid, mtype in delta.minted_types.items()
            if mtype == "source"
            and (nbytes := delta.minted_body_bytes.get(uid, 0)) > int(max_source_bytes)
        )
        if oversize:
            reasons.append(
                f"minted source page(s) exceed the {max_source_bytes}B thinness "
                f"ceiling: {', '.join(oversize)}"
            )

    if expected.get("requires_proposal"):
        # athenaeum#1598 AC2: scoped to the uid(s) the case actually names,
        # the same way touch_or_proposal_uids is above -- "a proposal was
        # made" is not "a proposal relevant to THIS case was made". A case
        # that sets requires_proposal without naming any uid has nothing to
        # scope against, so it fails closed rather than falling back to the
        # whole-run boolean athenaeum#1598 exists to retire.
        if not touch_or_proposal_uids:
            reasons.append(
                "requires_proposal has no touch_or_proposal_uids to scope "
                "against — cannot tell which entity the proposal must name"
            )
        elif not any(uid in delta.proposed_uids for uid in touch_or_proposal_uids):
            reasons.append(
                "no pending-decision surface named the entity this outcome is "
                "about — an irreversible outcome must reach the queue as a "
                "proposal naming that entity (docs/north-star.md §2.8)"
            )

    if expected.get("forbid_page_removal", True) and delta.removed_uids:
        reasons.append(
            f"pages removed outright: {sorted(delta.removed_uids)} — an "
            "irreversible act was APPLIED rather than proposed"
        )

    detail = "; ".join(reasons) if reasons else "ok"
    return (not reasons), detail
