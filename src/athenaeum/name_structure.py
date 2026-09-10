# SPDX-License-Identifier: Apache-2.0
"""Qualified-name entity-split detection over the compiled wiki (issue athenaeum#1577).

The shape this catches
----------------------

A bare entity name and the SAME name carrying a parenthetical qualifier —
``Keelbridge`` and ``Keelbridge (rollout)``, ``JTBD`` and
``JTBD (Jobs to Be Done)``, ``ORCA`` and
``ORCA (Online Resources & Coaching Awesomeness)`` — living as two pages of
the same ``type``. That is one entity written down twice, and the split
costs RETRIEVAL, not merely tidiness: issue athenaeum#1570's fixture measures
recall@5 = 0.5 over the consolidated entity, because the bare page ranks and
the qualified page holding the other half of the facts does not appear.

Why a NAME-structure signal, and not the existing paths
-------------------------------------------------------

* :mod:`athenaeum.name_collisions` (issue athenaeum#1170) matches ``name:``
  EXACTLY. ``Keelbridge`` != ``Keelbridge (rollout)``, so it never fires.
* :mod:`athenaeum.wiki_dedupe` proposes pairs from MiniLM embeddings. Two
  separate reasons it never fires on this shape, both measured rather than
  assumed:

  1. Its :data:`~athenaeum.wiki_dedupe.DEDUPE_CANDIDATE_TYPES` is
     ``{concept, reference, principle}``. On the live corpus ``project``
     (848 pages), ``company`` (1,887) and ``tool`` (716) are outside it
     entirely, and the observed instance (issue athenaeum#1568) was a
     ``project``.
  2. Issue athenaeum#1251 measured the merge-worthiness gate on 28,951 live
     candidate pairs and found 99.6% at ZERO containment — the corpus is
     prose-summarising, so verbatim-echo signals do not fire. A bare page
     and its qualified sibling describe DIFFERENT facts about one entity;
     there is no echo to contain.

So the signal has to be structural, and it is deliberately cheap: a glob and
a regex. No LLM, no vectors, no network — the same posture as
:mod:`athenaeum.name_collisions`, and for the same reason (a wiki-dedupe
failure must never suppress this detector, and vice versa).

This module PROPOSES; it never decides
---------------------------------------

athenaeum#715's doctrine — "similarity's only job is proposing pairs" —
applies with more force here, not less, because a parenthetical is a WEAKER
signal than an exact name match. Measured on the live corpus, the rule
produces two genuinely different populations that it cannot tell apart:

* **expansions / synonyms** — ``JTBD (Jobs to Be Done)``,
  ``Electronic Arts (EA)``, ``Center for Creative Leadership (CCL)``. One
  entity, twice. These want a merge.
* **scope or phase qualifiers** — ``Fujitsu (2nd Contract)``,
  ``Credit Sesame (Retainer)``. Arguably one company and several
  engagements; a human may well resolve these as ``specialization`` or
  reject them outright.

Distinguishing those two is a judgement about the WORLD, not about the
string, and this module does not pretend to make it. Every hit is therefore
written to ``wiki/_pending_merges.md`` and left there. Nothing in this
module calls :func:`athenaeum.pending_merges.resolve_merge`, and it has no
auto-merge switch to turn on — contrast
:func:`athenaeum.name_collisions.resolve_name_collisions`, whose exact-name
collisions CAN be auto-folded under an operator flag. ``docs/north-star.md``
§2.8: humans adopt anything irreversible through the one queue.

Confidence is written as :data:`QUALIFIED_NAME_CONFIDENCE` (0.5) rather than
name-collision's 1.0, and that number is a claim about this signal's
measured precision, not a knob: roughly half the live hits are the
scope/phase population above.

The draft body is CONCATENATED, never synthesised
--------------------------------------------------

:func:`~athenaeum.pending_merges.write_pending_merge` requires a
``draft_merged_body``. Unlike :mod:`athenaeum.name_collisions` — where the
absorbed pages "add nothing" by construction, so the canonical page's own
text IS the merged text — the qualified page here carries facts the bare
page lacks. That is the entire point of the retrieval asymmetry.

:func:`build_draft_body` therefore appends the qualified page's body
VERBATIM under its own heading. No summarisation, no rewriting, no model
call: a reviewer sees both halves and writes the real fold. A merged body
that dropped the qualified page's facts would enact exactly the data loss
this issue exists to stop.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from athenaeum.models import parse_frontmatter

log = logging.getLogger(__name__)

#: Page ``type`` values this scan considers.
#:
#: A POSITIVE set rather than an exclusion list, so a new entity type is
#: silently out of scope until someone opts it in deliberately. Derived from
#: the live-corpus type histogram (25,503 pages, 2026-09-10):
#:
#: * ``concept`` / ``reference`` / ``principle`` — the three
#:   :data:`athenaeum.wiki_dedupe.DEDUPE_CANDIDATE_TYPES` already treat as
#:   merge candidates on the embedding path.
#: * ``company`` / ``project`` / ``tool`` / ``incident`` / ``source`` /
#:   ``preference`` / ``team`` — entity classes where a parenthetical
#:   qualifier is the observed split shape. ``project`` is the class the
#:   originating report (issue athenaeum#1568) was in.
#:
#: ``person`` is EXCLUDED on two independent grounds, either of which would
#: be sufficient: it is 17,381 of the 25,503 pages, and person contact data
#: is routed to a separate excluded storage surface (issues athenaeum#864,
#: athenaeum#883). Its parenthetical hits are also the wrong shape —
#: ``Betsy Kochanski (Streeter)``, ``Patrick Cutliffe (帕特里克·卡特利夫)``
#: are née-names and transliterations, which are ALIAS material, not entity
#: splits. ``auto-memory`` is excluded because it is raw intake awaiting
#: compilation rather than a compiled entity page.
NAME_STRUCTURE_CANDIDATE_TYPES: frozenset[str] = frozenset(
    {
        "concept",
        "reference",
        "principle",
        "company",
        "project",
        "tool",
        "team",
        "incident",
        "source",
        "preference",
    }
)

#: Confidence stamped on every proposal this module writes. See the module
#: docstring: deliberately below :mod:`athenaeum.name_collisions`'s 1.0,
#: because an exact name match is certain and a parenthetical is not.
QUALIFIED_NAME_CONFIDENCE = 0.5

#: ``Base Name (qualifier)``.
#:
#: Non-greedy base plus ``[^()]+`` inside the parens means a name carrying
#: NESTED or multiple parenthetical groups matches only on its final,
#: innermost group — and a name that is nothing BUT a parenthetical
#: (``"(draft)"``) fails, since ``.+?`` needs at least one character before
#: the ``\s*\(``. Anchored at both ends: a parenthetical in the MIDDLE of a
#: name (``Marsa Maroc (500 Startups) notes``) is not a qualifier, it is
#: part of the name.
_QUALIFIER_RE = re.compile(r"^(?P<base>.+?)\s*\((?P<qualifier>[^()]+)\)$")

#: Qualifiers that name an ASSOCIATED GROUP OF PEOPLE, not a facet of the
#: entity. ``X (Team)`` is the people who work on X; it is not X.
#:
#: This is issue athenaeum#1570's negative control, generalised. The fixture
#: states the principle — ``Keelbridge Desk`` is "a standing staffing
#: arrangement, not a phase of any programme, and it continues after the last
#: office has moved", so "a consolidation pass that swallows it has
#: over-merged" — and guards it with a ``team`` ``type:``, which the same-type
#: requirement in :func:`scan_qualified_name_splits` catches for free.
#:
#: The LIVE corpus carries the harder variant the fixture does not: ``Heart``
#: / ``Heart (Team)`` and ``Bell`` / ``Bell (Team)``, both typed ``project``,
#: where the type check cannot help because the split is only in the name.
#: Those were the only two clear false positives in the 37-hit hand review
#: recorded on this issue's PR, and they are the fixture's own negative
#: control wearing a different ``type:``. Encoding the principle rather than
#: leaning on the fixture's incidental typing is what makes the control mean
#: something outside the fixture.
#:
#: Deliberately short, lower-cased, and matched WHOLE — not a substring, so
#: ``Keelbridge (rollout team briefing)`` is unaffected. A qualifier this set
#: does not name is not thereby endorsed; it is merely proposed, and a human
#: still decides.
_GROUP_QUALIFIERS: frozenset[str] = frozenset(
    {"team", "desk", "rota", "crew", "squad", "staff", "the team"}
)


def normalize_name(name: str) -> str:
    """Case- and whitespace-insensitive comparison key for a ``name:`` value.

    ``casefold`` rather than ``lower`` (Unicode-correct for the non-ASCII
    names the live corpus actually carries), and internal whitespace runs
    collapse to a single space so ``"Keelbridge  (rollout)"`` and
    ``"Keelbridge (rollout)"`` compare equal.
    """
    return " ".join(name.split()).casefold()


def split_qualifier(name: str) -> tuple[str, str] | None:
    """``"Keelbridge (rollout)"`` -> ``("Keelbridge", "rollout")``.

    Returns ``None`` when *name* carries no trailing parenthetical qualifier,
    when stripping it would leave an empty base, or when the qualifier names
    an associated GROUP rather than a facet of the entity
    (:data:`_GROUP_QUALIFIERS` — the negative control). See
    :data:`_QUALIFIER_RE` for the string shapes deliberately not matched.
    """
    match = _QUALIFIER_RE.match(name.strip())
    if match is None:
        return None
    base = match.group("base").strip()
    qualifier = match.group("qualifier").strip()
    if not base or not qualifier:
        return None
    if normalize_name(qualifier) in _GROUP_QUALIFIERS:
        return None
    return base, qualifier


@dataclass(frozen=True)
class QualifiedNameSplit:
    """One ``bare`` / ``bare (qualifier)`` pair of same-typed wiki pages.

    ``bare_path`` is the proposal's canonical side: the base name is the
    wider scope, so the qualified page folds INTO it rather than the other
    way round. That direction is structural, not a heuristic — it is read
    straight off which of the two names is a prefix of the other.
    """

    page_type: str
    bare_path: Path
    bare_name: str
    qualified_path: Path
    qualified_name: str
    qualifier: str

    @property
    def sources(self) -> list[str]:
        """Both member paths, canonical side first."""
        return [str(self.bare_path), str(self.qualified_path)]


@dataclass(frozen=True)
class _CandidatePage:
    path: Path
    name: str
    page_type: str
    text: str
    body: str


def _load_candidates(wiki_root: Path) -> list[_CandidatePage]:
    """Every ``wiki/<slug>.md`` page eligible for this scan.

    Mirrors :meth:`athenaeum.models.EntityIndex._load`'s traversal — a flat
    (non-recursive) glob skipping ``_``-prefixed sidecars — plus the
    merge-eligibility exclusions
    :func:`athenaeum.wiki_dedupe.discover_wiki_dedupe_candidates` already
    applies, so a page an operator archived, superseded, hand-flagged
    ``pii``, or marked a ``pointer_stub`` is never proposed here either.
    Those predicates are IMPORTED from their existing homes rather than
    re-implemented: a new exclusion added there must not silently fail to
    apply here.
    """
    from athenaeum.authority import is_pointer_stub
    from athenaeum.pii import is_pii_flagged

    pages: list[_CandidatePage] = []
    for path in sorted(wiki_root.glob("*.md")):
        if path.name.startswith("_") or path.name.startswith("auto-"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, body = parse_frontmatter(text)
        if not isinstance(meta, dict) or not meta:
            continue
        page_type = str(meta.get("type") or "")
        if page_type not in NAME_STRUCTURE_CANDIDATE_TYPES:
            continue
        if is_pii_flagged(meta) or is_pointer_stub(meta):
            continue
        if meta.get("superseded_by"):
            continue
        tags_raw = meta.get("tags") or []
        tags = [str(t).lower() for t in tags_raw] if isinstance(tags_raw, list) else []
        if "archived" in tags:
            continue
        name = str(meta.get("name") or "").strip()
        if not name:
            continue
        pages.append(
            _CandidatePage(path=path, name=name, page_type=page_type, text=text, body=body)
        )
    return pages


def scan_qualified_name_splits(wiki_root: Path) -> list[QualifiedNameSplit]:
    """Every ``bare`` / ``bare (qualifier)`` same-type pair under *wiki_root*.

    Both sides must share a ``type``. That requirement is what keeps the
    negative control in issue athenaeum#1570's Cluster B out: the
    ``Keelbridge Desk`` team page is a standing staffing arrangement that
    outlives the programme, and a consolidation pass that swallows it has
    over-merged. It is excluded here for the more basic reason that its name
    is not ``Keelbridge (...)`` at all — the type check is the second,
    independent guard, not the only one.

    A qualified page with SEVERAL same-type bare twins (the live corpus has
    three ``ORCA`` pages) yields one pair per twin: each is an independent
    decision for a reviewer, and collapsing them would silently pick a
    canonical side on the reviewer's behalf.

    Deterministic ordering (by qualified path, then bare path) so a re-run
    over an unchanged corpus proposes in the same order.
    """
    if not wiki_root.is_dir():
        return []

    pages = _load_candidates(wiki_root)
    by_key: dict[tuple[str, str], list[_CandidatePage]] = {}
    for page in pages:
        by_key.setdefault((normalize_name(page.name), page.page_type), []).append(page)

    splits: list[QualifiedNameSplit] = []
    for page in pages:
        parts = split_qualifier(page.name)
        if parts is None:
            continue
        base, qualifier = parts
        for twin in by_key.get((normalize_name(base), page.page_type), []):
            if twin.path == page.path:
                continue
            splits.append(
                QualifiedNameSplit(
                    page_type=page.page_type,
                    bare_path=twin.path,
                    bare_name=twin.name,
                    qualified_path=page.path,
                    qualified_name=page.name,
                    qualifier=qualifier,
                )
            )
    splits.sort(key=lambda s: (str(s.qualified_path), str(s.bare_path)))
    return splits


def build_draft_body(bare_text: str, qualified_name: str, qualified_body: str) -> str:
    """The bare page's full raw text with the qualified page's body appended.

    *bare_text* is the FULL raw markdown (frontmatter included), because
    :func:`athenaeum.pending_merges._apply_fold_into_existing` overwrites the
    fold target verbatim with this string — passing a body alone would drop
    the canonical page's own ``uid``/``type``/``name``. Same reasoning, and
    the same trap, as :func:`athenaeum.name_collisions.resolve_name_collisions`
    documents at its ``draft_full_text`` read.

    The qualified body follows under an ``## From "<name>"`` heading, copied
    VERBATIM. See the module docstring: this is a concatenation a reviewer
    edits, not a synthesised merge. Nothing here drops a fact, and nothing
    here invents one.
    """
    return (
        bare_text.rstrip("\n")
        + f'\n\n## From "{qualified_name}"\n\n'
        + qualified_body.strip()
        + "\n"
    )


def propose_qualified_name_merges(
    wiki_root: Path,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Scan for qualified-name splits and QUEUE each as a pending merge.

    Every hit is appended to ``<wiki_root>/_pending_merges.md`` via
    :func:`athenaeum.pending_merges.write_pending_merge` — which is already
    idempotent on the ``(sources, target-name)`` id, so re-running over an
    unchanged corpus never appends a duplicate block — and left UNRESOLVED.

    This function enacts nothing. It does not call ``resolve_merge``, it
    takes no ``auto_merge`` parameter, and it writes no page. The proposal
    surfaces through :func:`athenaeum.decisions.list_pending_decisions` /
    ``list_pending_merges`` and waits for a human, per ``docs/north-star.md``
    §2.8. Issue athenaeum#1577 AC2 pins that as an invariant rather than a
    default.

    ``merge_target_name`` is the bare page's own FILENAME STEM, never its
    ``name:`` frontmatter value — the trap
    :func:`athenaeum.name_collisions._fold_target_matches_canonical` exists
    to catch (issue athenaeum#1170 code review). ``display_name`` carries the
    human-readable name so a reviewer is not shown a slug as the question.

    *dry_run* short-circuits before any write, returning accurate counts.

    Returns ``{"splits": n, "queued": n}`` — ``queued`` is 0 on a dry run.
    """
    from athenaeum.pending_merges import write_pending_merge

    splits = scan_qualified_name_splits(wiki_root)
    if dry_run or not splits:
        return {"splits": len(splits), "queued": 0}

    merges_path = wiki_root / "_pending_merges.md"
    by_path = {page.path: page for page in _load_candidates(wiki_root)}
    queued = 0
    for split in splits:
        bare = by_path.get(split.bare_path)
        qualified = by_path.get(split.qualified_path)
        if bare is None or qualified is None:  # pragma: no cover - raced deletion
            continue
        rationale = (
            f"athenaeum#1577 qualified-name scan: {split.qualified_name!r} is "
            f"{split.bare_name!r} plus the qualifier {split.qualifier!r}, and both "
            f"are type {split.page_type!r} — the entity-split signature. The "
            "qualifier may mark a synonym/expansion (one entity, twice) or a "
            "scope/phase (a genuinely narrower thing); this scan cannot tell "
            "those apart, so a human decides. The draft below concatenates both "
            "bodies verbatim — nothing was summarised."
        )
        write_pending_merge(
            merges_path,
            merge_target_name=split.bare_path.stem,
            display_name=split.bare_name,
            sources=split.sources,
            rationale=rationale,
            draft_merged_body=build_draft_body(bare.text, split.qualified_name, qualified.body),
            confidence=QUALIFIED_NAME_CONFIDENCE,
            write_kind=None,
        )
        queued += 1

    log.info(
        "qualified-name scan: %d split(s) found, %d queued to %s (never auto-applied)",
        len(splits),
        queued,
        merges_path,
    )
    return {"splits": len(splits), "queued": queued}


def summarize(splits: list[QualifiedNameSplit]) -> dict[str, Any]:
    """Counts by page type, for the re-scoring script and the run profile."""
    by_type: dict[str, int] = {}
    for split in splits:
        by_type[split.page_type] = by_type.get(split.page_type, 0) + 1
    return {"total": len(splits), "by_type": dict(sorted(by_type.items()))}
