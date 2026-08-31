# SPDX-License-Identifier: Apache-2.0
"""Deterministic name-collision detection over the compiled wiki (issue athenaeum#1170 AC2).

Before this issue, the create-path gate (:mod:`athenaeum.tiers`'s
``validate_create_name`` / ``gate_create_name_classifications``, this same
issue's AC1) is the ONLY thing that stops a colliding ``name:`` from being
minted going forward — it has no way to find collisions that already exist
on disk (from before the gate shipped, or from a hand-authored page). This
module is that other half: a nightly, deterministic, EXACT-``name:``-match
scan over ``wiki/*.md`` — no LLM, no vectors, no network — that groups pages
by ``(name, type)`` and reports every group with more than one page as a
:class:`NameCollision`.

Deliberately separate from the comparator-based wiki-page dedup pass
(:mod:`athenaeum.wiki_dedupe`): that pass is expensive (vector/LLM-based)
and, per its own module docstring, under repair. A same-name collision is
categorically different from a semantic-similarity cluster — two pages
literally sharing a ``name:`` key make the wiki's own name index ambiguous
regardless of what an embedding says about their content — and detecting it
costs nothing (a glob + a dict grouping). Keeping this scan as its own
phase, called independently from :mod:`athenaeum.librarian`, means a
wiki-dedup failure can never suppress this detector and vice versa.

This module only DETECTS and CLASSIFIES. Resolution reuses the existing
decision-queue and fold machinery in :mod:`athenaeum.pending_merges` — see
:func:`resolve_name_collisions`, which is the only function here that
writes anything.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from athenaeum.models import parse_frontmatter, resolve_page_type, slugify
from athenaeum.pending_merges import parse_pending_merges, resolve_merge, write_pending_merge

log = logging.getLogger(__name__)

#: Frontmatter keys ignored when comparing an "adds nothing" page's
#: frontmatter against the canonical page's (issue athenaeum#1170 AC2):
#: identity/provenance fields that differ between two pages BY
#: CONSTRUCTION (every page has its own ``uid``, its own ``created``/
#: ``updated`` stamps, etc.) and therefore carry no signal about whether the
#: page adds substantive content the canonical page lacks.
_IGNORED_FRONTMATTER_KEYS = frozenset({"uid", "name", "created", "updated", "source", "aliases"})


@dataclass(frozen=True)
class CollisionPage:
    """One page participating in a :class:`NameCollision`."""

    path: Path
    uid: str
    name: str
    type: str | None
    body: str


@dataclass(frozen=True)
class NameCollision:
    """A group of >= 2 wiki pages sharing a ``(name, type)`` key."""

    name: str
    type: str | None
    pages: tuple[CollisionPage, ...]


def _normalize_body(body: str) -> str:
    """Whitespace-trimmed body text, for empty/substring comparisons.

    Deliberately minimal (``strip()`` only, no whitespace collapsing) — the
    inputs being compared are two on-disk page bodies rendered by the same
    pipeline, not free-form user text, so an over-eager normalization would
    risk masking a real content difference. Err toward :func:`classify_collision`
    reporting ``ambiguous``, per this module's own stated bias.
    """
    return body.strip()


def scan_name_collisions(wiki_root: Path) -> list[NameCollision]:
    """Scan ``wiki_root`` for pages that share a ``(name, type)`` key.

    Mirrors :meth:`athenaeum.models.EntityIndex._load`'s traversal exactly: a
    flat (non-recursive) glob of ``*.md`` directly under ``wiki_root``,
    skipping ``_``-prefixed sidecars (``_pending_merges.md``,
    ``_pending_questions.md``, ...); a page that cannot be read (``OSError``/
    ``UnicodeDecodeError``) or carries no parseable/no frontmatter at all is
    silently skipped, never raised — exactly that index's own tolerance, so
    this scan never trips over the same malformed file the rest of the
    pipeline already tolerates. A page with no ``name:`` frontmatter key is
    likewise skipped (nothing to key a collision group on).

    Grouping key is ``(name.strip().lower(), resolved_type)`` — case-
    insensitive on the name (matching :meth:`EntityIndex.lookup`'s own
    case-folding), and *scoped by type* via :func:`athenaeum.models.resolve_page_type`
    (the same canonical type resolver ``EntityIndex._load`` uses) so a
    same-name-different-type pair — the ``type: project`` repo vs. the
    ``type: person`` example this issue's AC1 also carves out — is never
    treated as a collision. A page with no ``type:`` frontmatter groups
    under the ``None`` type key, same "explicit sentinel, not an empty
    string" convention :class:`~athenaeum.models.IndexEntry` uses.

    Returns only groups with more than one page, fully deterministically
    ordered: pages within a group sorted by path, and groups themselves
    sorted by ``(name, type)``.
    """
    groups: dict[tuple[str, str | None], list[CollisionPage]] = {}
    for fpath in sorted(wiki_root.glob("*.md")):
        if fpath.name.startswith("_"):
            continue
        try:
            text = fpath.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, body = parse_frontmatter(text)
        if not meta:
            continue

        uid_raw = meta.get("uid", "")
        name_raw = meta.get("name", "")
        if not name_raw:
            continue
        uid = str(uid_raw)
        name = str(name_raw)

        page_type = resolve_page_type(meta)
        entry_type = page_type if page_type else None

        key = (name.strip().lower(), entry_type)
        groups.setdefault(key, []).append(
            CollisionPage(path=fpath, uid=uid, name=name, type=entry_type, body=body)
        )

    collisions: list[NameCollision] = []
    for (_name_key, type_key), pages in groups.items():
        if len(pages) < 2:
            continue
        pages_sorted = tuple(sorted(pages, key=lambda p: str(p.path)))
        collisions.append(
            NameCollision(name=pages_sorted[0].name, type=type_key, pages=pages_sorted)
        )
    collisions.sort(key=lambda c: (c.name.strip().lower(), c.type or ""))
    return collisions


def canonical_page(collision: NameCollision) -> CollisionPage:
    """Pick the substantive page in *collision*.

    Deterministic tiebreak (issue athenaeum#1170 AC2): longest normalized body
    first, then lexicographically smallest path — never LLM/heuristic
    judgment, so the same corpus always yields the same canonical page.
    """

    def _sort_key(page: CollisionPage) -> tuple[int, str]:
        return (-len(_normalize_body(page.body)), str(page.path))

    return sorted(collision.pages, key=_sort_key)[0]


def _page_frontmatter(page: CollisionPage) -> dict[str, object]:
    """Re-read *page*'s frontmatter from disk for :func:`classify_collision`.

    :class:`CollisionPage` deliberately carries only ``body`` (the content
    that matters for the empty/substring check) — frontmatter is read here,
    on demand, only when a classification actually needs to compare it.
    Any read/parse failure degrades to ``{}`` (an empty frontmatter can
    never look like it's missing a key the canonical page has, so this
    never manufactures a false ``ambiguous`` from a page that briefly failed
    to read — though a genuinely different frontmatter shape elsewhere in
    the comparison still catches real disagreement).
    """
    try:
        text = page.path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    meta, _ = parse_frontmatter(text)
    return meta if isinstance(meta, dict) else {}


def classify_collision(collision: NameCollision) -> Literal["unambiguous", "ambiguous"]:
    """Classify *collision* as safe to auto-resolve or needing a human.

    ``unambiguous`` ONLY when exactly one page (the :func:`canonical_page`)
    is substantive and every OTHER page in the group adds nothing:

    - its normalized body is empty, or is a substring of the canonical
      page's normalized body, AND
    - its frontmatter carries no key with a value absent from the
      canonical's, ignoring identity/provenance keys
      (:data:`_IGNORED_FRONTMATTER_KEYS`).

    Everything else is ``ambiguous`` — two or more pages with distinct
    content, a group whose resolved type is ``None`` (an untyped page
    cannot be confidently disambiguated the way a typed one can — mirrors
    AC1's escalate-on-unknown-type posture), or any group this function
    cannot confidently reduce. Erring toward ``ambiguous`` is deliberate: a
    false "ambiguous" costs a human glance; a false "unambiguous" destroys
    content.
    """
    if collision.type is None:
        return "ambiguous"
    if len(collision.pages) < 2:
        # Defensive — scan_name_collisions never returns a singleton group.
        return "ambiguous"

    canonical = canonical_page(collision)
    canonical_norm = _normalize_body(canonical.body)
    canonical_meta = _page_frontmatter(canonical)

    for other in collision.pages:
        if other.path == canonical.path:
            continue
        other_norm = _normalize_body(other.body)
        if other_norm and other_norm not in canonical_norm:
            return "ambiguous"
        other_meta = _page_frontmatter(other)
        for key, value in other_meta.items():
            if key in _IGNORED_FRONTMATTER_KEYS:
                continue
            if key not in canonical_meta or canonical_meta[key] != value:
                return "ambiguous"
    return "unambiguous"


def _fold_target_matches_canonical(wiki_root: Path, canonical: CollisionPage) -> bool:
    """Whether ``pending_merges``'s fold-into-existing target path is
    actually the canonical page's own file (issue athenaeum#1170 safety guard).

    :func:`athenaeum.pending_merges.classify_write_kind` / ``resolve_merge``
    derive the fold TARGET purely from ``wiki_root / f"{slugify(merge_target_name)}.md"``
    — they have no notion of "the page that already carries this name might
    be filed under a different filename". A ``compiled`` wiki page (the
    convention :mod:`athenaeum.wiki_dedupe` / this reused fold machinery was
    built for) IS named exactly that bare slug, but an entity-template page
    minted via the create path is not — its filename is
    ``<uid>-<slug>.md`` (see :attr:`athenaeum.models.WikiEntity.filename`).

    When the canonical page's own file is not at the derived target path,
    routing its collision through :func:`~athenaeum.pending_merges.write_pending_merge`'s
    ``write_kind=None`` derivation would silently classify it as
    ``create-merged`` (the bare-slug path doesn't exist yet) and, on
    auto-merge, WRITE A NEW near-duplicate page at that bare-slug path
    instead of folding into the canonical page that already exists under a
    different name — worse than doing nothing. Rather than teaching
    ``pending_merges`` a second target-resolution convention, this function
    lets :func:`resolve_name_collisions` force such a collision to
    ``ambiguous`` (queued for a human, never auto-merged) regardless of what
    :func:`classify_collision` would otherwise say.
    """
    target_path = wiki_root / f"{slugify(canonical.name)}.md"
    try:
        return target_path.resolve() == canonical.path.resolve()
    except OSError:
        return False


def _find_open_merge_id(merges_path: Path, sources: list[str], target_name: str) -> str | None:
    """Find the unresolved :class:`~athenaeum.pending_merges.PendingMerge` id
    matching *sources* + *target_name* (issue athenaeum#1170 AC3).

    Deliberately re-reads and matches by value instead of reaching into
    :mod:`athenaeum.pending_merges`'s private ``_make_id`` from this module —
    :func:`write_pending_merge` already computed and stored that id; this
    just looks it back up the same way any other reader of the sidecar
    would. Returns ``None`` when no UNRESOLVED block matches (either nothing
    was ever written for this pair, or a prior run's block was already
    resolved by a human) — the caller must not attempt to re-approve an
    already-resolved block.
    """
    wanted_sources = sorted(sources)
    for pm in parse_pending_merges(merges_path):
        if (
            not pm.resolved
            and pm.merge_target_name == target_name
            and sorted(pm.sources) == wanted_sources
        ):
            return pm.id
    return None


def resolve_name_collisions(
    wiki_root: Path,
    *,
    auto_merge: bool,
    dry_run: bool,
) -> dict[str, int]:
    """Scan, propose, and (when enabled) auto-resolve name collisions.

    Issue athenaeum#1170 AC3-AC6, entirely via the EXISTING decision-queue and
    fold machinery in :mod:`athenaeum.pending_merges` — this function writes
    no new merge/fold logic of its own:

    - For EVERY collision (ambiguous and unambiguous alike),
      :func:`~athenaeum.pending_merges.write_pending_merge` appends one
      proposal block naming the :func:`canonical_page` as the merge target
      and every page in the group as a source, with ``write_kind=None`` so
      the derived classification (``fold-into-existing`` — the canonical
      slug already exists) is trusted rather than asserted (issue athenaeum#748).
      That function is already idempotent on the source-set + target-name
      id, so re-running this scan over an unchanged corpus never appends a
      duplicate block (AC3).
    - **Ambiguous** collisions stop there — the unresolved block is
      surfaced through :func:`athenaeum.decisions.list_pending_decisions`
      (which already reads every unresolved ``_pending_merges.md`` block),
      i.e. the unified decision queue, with no new queue plumbing (AC5).
    - **Unambiguous** collisions, only when *auto_merge* is true and not
      *dry_run*, are resolved immediately via
      :func:`~athenaeum.pending_merges.resolve_merge` with
      ``auto_applied=True`` — the same non-human-approve marker
      (issue athenaeum#602) :func:`athenaeum.merge.t2_screen_merge_proposal`
      already uses for its own auto-finalize path. This dispatches to
      ``resolve_merge``'s ``fold-into-existing`` write path, which performs
      the provenance-snapshot commit then the fold commit (AC4, git-
      reversible by construction), unions the absorbed slugs into the
      canonical page's ``aliases:`` (AC6), and deletes the folded-away
      source pages via ``git rm`` — which is exactly why re-scanning after a
      successful auto-merge finds no collision at all: the source pages are
      gone, so idempotency (AC3) holds trivially on the next run.

    *dry_run* short-circuits before any write: only :func:`scan_name_collisions`
    and :func:`classify_collision` run, so a dry-run reports accurate counts
    without touching ``_pending_merges.md`` or merging anything.

    Concurrency (issue athenaeum#947 AC3, extended by athenaeum#1170): this
    function's approve path reaches
    :func:`athenaeum.pending_merges._apply_fold_into_existing`, whose own
    docstring's concurrency contract now also names this caller — see that
    docstring for why the run lock covers it. In production this function is
    only ever called from :func:`athenaeum.librarian._run_name_collision_phase`,
    which — like :func:`athenaeum.librarian._run_wiki_dedup_phase` — is only
    reached from a non-``--dry-run`` :func:`athenaeum.librarian.run`, itself
    only invoked (for a real, mutating run) after
    :func:`athenaeum._cli_shared._acquire_or_exit` has taken the single-
    machine run lock (see :func:`athenaeum._cmd_run.cmd_run`).

    Returns a dict with ``collisions``, ``unambiguous``, ``ambiguous``,
    ``merged``, and ``queued`` counts (``queued`` = collisions whose
    proposal is still sitting unresolved in the pending-merges sidecar after
    this call — every ambiguous collision, plus any unambiguous collision
    not auto-merged this run).
    """
    collisions = scan_name_collisions(wiki_root)
    unambiguous = 0
    ambiguous = 0
    merged = 0

    if dry_run:
        for collision in collisions:
            canonical = canonical_page(collision)
            verdict = classify_collision(collision)
            if verdict == "unambiguous" and not _fold_target_matches_canonical(
                wiki_root, canonical
            ):
                verdict = "ambiguous"
            if verdict == "unambiguous":
                unambiguous += 1
            else:
                ambiguous += 1
        return {
            "collisions": len(collisions),
            "unambiguous": unambiguous,
            "ambiguous": ambiguous,
            "merged": 0,
            "queued": len(collisions),
        }

    merges_path = wiki_root / "_pending_merges.md"
    for collision in collisions:
        canonical = canonical_page(collision)
        sources = [str(page.path) for page in collision.pages]
        verdict = classify_collision(collision)
        if verdict == "unambiguous" and not _fold_target_matches_canonical(
            wiki_root, canonical
        ):
            log.warning(
                "name-collision-fold-target-mismatch name=%r canonical=%s — "
                "canonical page's filename does not match the fold "
                "machinery's derived target (slugify(name).md); forcing "
                "ambiguous so this is queued for a human instead of "
                "auto-merged incorrectly",
                collision.name,
                canonical.path.name,
            )
            verdict = "ambiguous"
        type_note = f" (type: {collision.type})" if collision.type else " (type: unknown)"
        rationale = (
            f"athenaeum#1170 nightly name-collision scan: {len(collision.pages)} pages "
            f"share the name {collision.name!r}{type_note}; classified {verdict} "
            f"(canonical: {canonical.path.name})."
        )
        # Pass the canonical page's FULL raw text (frontmatter included),
        # not just its body. _apply_fold_into_existing's step 1 overwrites
        # target_path verbatim with draft_merged_body, and step 3 only
        # carries the PRIOR target's uid/type/etc. forward when the draft
        # itself already has frontmatter to parse (see that function's own
        # step-3 comment) — passing body-only would silently drop the
        # canonical page's own uid/type/name on every auto-merge. Since the
        # canonical page IS the fold target, writing its own unchanged text
        # back is a content no-op; only the aliases: line changes.
        try:
            draft_full_text = canonical.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            draft_full_text = canonical.body
        write_pending_merge(
            merges_path,
            merge_target_name=canonical.name,
            sources=sources,
            rationale=rationale,
            draft_merged_body=draft_full_text,
            confidence=1.0,
            write_kind=None,
        )
        if verdict == "unambiguous":
            unambiguous += 1
            if auto_merge:
                merge_id = _find_open_merge_id(merges_path, sources, canonical.name)
                if merge_id is not None:
                    result = resolve_merge(
                        merges_path,
                        merge_id,
                        "approve",
                        note="athenaeum#1170 nightly name-collision auto-merge",
                        wiki_root=wiki_root,
                        auto_applied=True,
                    )
                    if result.get("ok"):
                        merged += 1
                        log.info(
                            "name-collision-auto-merged name=%r canonical=%s sources=%s",
                            collision.name,
                            canonical.path.name,
                            sources,
                        )
                    else:
                        log.warning(
                            "name-collision-auto-merge-failed name=%r error=%s",
                            collision.name,
                            result.get("error_code"),
                        )
        else:
            ambiguous += 1

    return {
        "collisions": len(collisions),
        "unambiguous": unambiguous,
        "ambiguous": ambiguous,
        "merged": merged,
        "queued": len(collisions) - merged,
    }
