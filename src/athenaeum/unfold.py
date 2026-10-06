# SPDX-License-Identifier: Apache-2.0
"""Unfold — the compensating repair path for a fold (issue athenaeum#716, lane C).

A fold (:mod:`athenaeum.pending_merges`'s ``fold-into-existing`` write path)
tombstones a source page into a canonical one: the source's own file is
stamped ``status: folded`` / ``folded_into: <canonical-slug>`` / ``embedded:
false`` (:func:`athenaeum.models.stamp_tombstone`, issue athenaeum#716 lane A)
rather than deleted, inbound wikilinks pointing at the source are rewritten to
the canonical, and a merge-provenance record is appended
(:mod:`athenaeum.provenance`, owned by issue athenaeum#716 lane B).

Unfold is this module's single job: given a tombstone page, decide whether
restoring it is safe to do **directly** or must become a **queued,
human-adjudicated proposal**, then do whichever is correct.

**Unfold is a COMPENSATING operation, never claimed to be a perfect
inverse.** Two separate reasons, both named explicitly rather than papered
over:

1. :func:`athenaeum.models.stamp_tombstone` OVERWRITES whatever ``status:``
   value a page held before the fold (e.g. a pre-existing
   ``"contradiction-flagged"``) with the literal string ``"folded"`` — that
   prior value is recorded nowhere this module can recover it from, so
   :func:`_restore_live_meta` can only CLEAR the tombstone's three stamped
   keys back to "not present," not to whatever they held pre-fold. A page
   that was contested AND later folded loses its contested flag on unfold.
2. The canonical page accumulates independent edits after the fold. The
   longer that goes on, the less a "restore the source" operation means —
   fidelity decays with time, which is exactly why the direct/queued split
   below exists.

**The direct/queued split is a CONTENT-HASH comparison, never a timestamp or
mtime heuristic** (see :func:`can_unfold_directly`): the canonical's current
text hash (:func:`athenaeum.verdicts.content_hash_for_path`) is compared
against the ``canonical_content_hash`` the fold recorded on its own
provenance entry. Equal -> the canonical has not been touched since the fold
-> unfold executes directly (tombstone cleared, inbound links re-pointed,
re-embedded by virtue of the file changing — see "Re-embedding" below).
Unequal, or the record predates issue athenaeum#716's pinned fields entirely
(the real-corpus case: every fold recorded before lane B landed) -> queued,
carrying a best-effort diff for a human (see "Known ledger-contract gap"
below).

**Re-embedding.** This module never calls an embedding backend directly (and
could not: ``ANTHROPIC_API_KEY`` is unset in this lane's test container, and
the local sentence-transformer path is :mod:`athenaeum.search`'s concern, not
this module's). Restoring a tombstone rewrites the page's frontmatter
(clearing ``embedded: false``) and therefore changes both its mtime and its
content hash — the next incremental index build
(:meth:`athenaeum.search.SearchBackend.build_index`) picks up exactly that
delta and re-embeds the page because :func:`athenaeum.storage.page_is_embedded`
now evaluates true for it again. "Re-embeds" is satisfied by restoring the
precondition the indexer already acts on, not by this module reimplementing
indexing.

**Known ledger-contract gap (cross-lane finding, not papered over): the
pinned ``canonical_content_hash`` field is a HASH, not a TEXT SNAPSHOT.**
The issue's own acceptance criterion asks a queued proposal to carry "a diff
of the canonical's current text against its post-fold text" — but the fixed
ledger contract this module reads never stores the canonical's post-fold
text anywhere (only its hash, which is one-way). There is therefore no way
for ANY reader of this ledger, this module included, to reconstruct "the
canonical as it stood right after the fold" to diff against. This module's
best-effort substitute (:func:`_best_effort_diff`) instead diffs the
canonical's CURRENT text against the TOMBSTONED SOURCE's own current text
(i.e. "what you would get back" vs. "what exists today") — a real, useful
diff for a human adjudicating the proposal, but it is explicitly labeled as
such in the queued item's context rather than mislabeled as the diff the
issue describes. Closing this gap for real requires lane B (or a follow-up)
to persist a canonical text snapshot (or a stable reference to one, e.g. a
git blob sha) on the fold record — a ledger-schema change, not something
this module can work around.

**Known ledger-contract gap #2: a shared sibling file linking to two
DIFFERENT sources folded into the SAME canonical cannot always be
disambiguated on unfold.** ``links_rewritten`` entries are
``{path, from_slug, to_slug}`` — once two different sources' links in the
same file are both rewritten to the same ``to_slug``, the post-rewrite text
is byte-identical at each occurrence, and the ledger does not record which
occurrence came from which source (e.g. a line number or occurrence index).
:func:`_rewrite_links_back` detects this exact ambiguity (another
``links_rewritten`` entry for the same ``(path, to_slug)`` pair with a
DIFFERENT ``from_slug``) and refuses to touch that file rather than risk
re-pointing a sibling source's still-folded link — the file is reported in
the result's ``details["links_skipped_ambiguous"]`` for a human to fix by
hand.

Layering: L4 domain/pipeline. Imports :mod:`athenaeum.models` (L1),
:mod:`athenaeum.verdicts` (L2), :mod:`athenaeum.provenance` (L1),
:mod:`athenaeum.atomic_io` (L0), :mod:`athenaeum.config` (L2), and
:mod:`athenaeum.store` (L0/L1) at module scope; :mod:`athenaeum.answers`'s
:func:`~athenaeum.answers.raise_pending_question` (L4) is imported
function-locally in :func:`_queue_unfold_proposal`, mirroring
:mod:`athenaeum.verdict_effects`'s own deferred-import style for its queue
routing. Does NOT import :mod:`athenaeum.pending_merges` — that module owns
the FOLD write path (and is a sibling lane's concurrent surface for this
issue); this module only ever READS its provenance ledger via
:func:`athenaeum.provenance.read_merge_provenance`.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict

from athenaeum import provenance
from athenaeum.atomic_io import atomic_write_text
from athenaeum.config import resolve_cache_dir
from athenaeum.models import (
    is_tombstone,
    parse_frontmatter,
    render_frontmatter,
    slugify,
    tombstone_target,
)
from athenaeum.store import now_iso
from athenaeum.verdicts import content_hash_for_path

#: Mirrors :mod:`athenaeum.pending_merges`'s private ``_WIKILINK_REWRITE_RE``
#: byte-for-byte (``[[slug]]`` / ``[[slug|alias text]]``). Duplicated, not
#: imported: that name is private to a module this lane is forbidden to
#: touch (a sibling lane owns it concurrently for this same issue), and the
#: regex itself is a small, stable, self-documenting primitive -- cheaper to
#: restate here than to destabilize the boundary by reaching into a private
#: name of a module outside this lane's surface.
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|\n]+?)(\|[^\[\]\n]*)?\]\]")

#: :class:`UnfoldResult.action` values.
UNFOLD_DIRECT = "direct"
UNFOLD_QUEUED = "queued"

#: Repair-debt counters sidecar, under the resolved cache dir (mirrors
#: :mod:`athenaeum.zero_yield`'s ``STATE_NAME`` convention exactly — a single
#: small JSON state file, not an append-only ledger, because only the
#: running totals matter, never individual events).
REPAIR_DEBT_COUNTERS_NAME = "repair_debt_counters.json"


@dataclass(frozen=True)
class UnfoldResult:
    """What :func:`unfold_page` did for one tombstone.

    ``action`` is :data:`UNFOLD_DIRECT` (tombstone cleared, links re-pointed
    in place) or :data:`UNFOLD_QUEUED` (a human-adjudicated proposal was
    raised; the tombstone itself is untouched). ``details`` always explains
    why, same "no silent no-ops" discipline
    :mod:`athenaeum.verdict_effects` documents for its own ``EffectResult``.
    """

    action: str
    tombstone_path: str
    canonical_slug: str | None
    details: dict[str, Any] = field(default_factory=dict)


class RepairDebtCounters(TypedDict):
    """Shape returned by :func:`load_repair_debt_counters`."""

    unfold_direct: int
    unfold_queued: int


# ---------------------------------------------------------------------------
# Slug / path helpers
# ---------------------------------------------------------------------------


def _page_slug(path: Path) -> str:
    return slugify(path.stem)


def _wiki_relpath(wiki_root: Path, path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(wiki_root).resolve()))
    except (OSError, ValueError):
        return str(path)


def _path_for_slug(wiki_root: Path, slug: str) -> Path | None:
    """The live wiki page whose slug is *slug*, or ``None``.

    Tries the direct filename first (``<slug>.md``, the common case), then
    falls back to a scan comparing :func:`_page_slug` for every top-level
    ``*.md`` file — mirrors how :mod:`athenaeum.pending_merges`'s own
    inbound-link rewriter resolves a wikilink target to a file.
    """
    direct = wiki_root / f"{slug}.md"
    if direct.is_file():
        return direct
    if not wiki_root.exists():
        return None
    for candidate in sorted(wiki_root.glob("*.md")):
        if candidate.name.startswith("_"):
            continue
        if _page_slug(candidate) == slug:
            return candidate
    return None


def _source_matches(raw: str, wiki_root: Path, tombstone_path: Path) -> bool:
    """Whether a ledger-recorded source path *raw* names *tombstone_path*.

    Defensive against the exact path SHAPE a landed lane-B ``folded_sources``
    / pre-existing ``source_paths`` entry uses (relative to ``wiki_root``,
    absolute, or just a bare filename) — this lane cannot see lane B's
    landed form, so it accepts any of the shapes the pre-existing
    ``source_paths`` field (``list(target_pm.sources)``, verbatim caller
    input) is already known to carry.
    """
    raw_path = Path(raw)
    if raw_path.name == tombstone_path.name:
        return True
    try:
        tombstone_resolved = tombstone_path.resolve()
    except OSError:
        tombstone_resolved = tombstone_path
    try:
        if raw_path.is_absolute():
            return raw_path.resolve() == tombstone_resolved
        return (Path(wiki_root) / raw_path).resolve() == tombstone_resolved
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Ledger lookup + the direct/queued predicate
# ---------------------------------------------------------------------------


def find_fold_record(
    wiki_root: Path, tombstone_path: Path, canonical_slug: str
) -> dict[str, Any] | None:
    """The most recent ``fold-into-existing`` provenance record that folded
    *tombstone_path* into *canonical_slug*, or ``None`` if none names it.

    Matches against the issue athenaeum#716 pinned ``folded_sources`` field
    (canonical excluded) when present, falling back to the pre-existing
    ``source_paths`` field for an older record that lacks it — exactly the
    "older records lack these keys" case the pinned contract names. A tie
    (more than one record matches — should not happen on a well-formed
    ledger, but the ledger is append-only and tolerates a hand-edit) is
    broken by the most recent ``ts``.
    """
    records = provenance.read_merge_provenance(wiki_root, canonical_slug=canonical_slug)
    matches: list[dict[str, Any]] = []
    for record in records:
        if record.get("write_kind") != "fold-into-existing":
            continue
        candidates = record.get("folded_sources")
        if not isinstance(candidates, list):
            candidates = record.get("source_paths")
        if not isinstance(candidates, list):
            continue
        if any(
            isinstance(raw, str) and _source_matches(raw, wiki_root, tombstone_path)
            for raw in candidates
        ):
            matches.append(record)
    if not matches:
        return None
    matches.sort(key=lambda r: str(r.get("ts", "")))
    return matches[-1]


def can_unfold_directly(
    record: dict[str, Any] | None, canonical_path: Path | None
) -> tuple[bool, str]:
    """Whether *record* authorizes a DIRECT unfold. Returns ``(ok, reason)``.

    The untouched-since-fold test is exactly
    ``content_hash_for_path(canonical_path) == record["canonical_content_hash"]``
    — a content-hash comparison, never mtime or any other timestamp (issue
    athenaeum#716 AC). ``reason`` is always populated, success or not, so a
    caller never has to guess why.
    """
    if record is None:
        return False, "no_ledger_record"
    folded_sources = record.get("folded_sources")
    links_rewritten = record.get("links_rewritten")
    canonical_hash = record.get("canonical_content_hash")
    if (
        not isinstance(folded_sources, list)
        or not isinstance(links_rewritten, list)
        or not isinstance(canonical_hash, str)
        or not canonical_hash
    ):
        # Issue athenaeum#716's pinned fields are absent: an older record (it
        # predates lane B's ledger change, or was hand-edited). No rewrite
        # list means inbound links cannot be re-pointed, so this fold is not
        # directly unfoldable even if the canonical happens to be untouched.
        return False, "ledger_record_missing_unfold_fields"
    if canonical_path is None or not canonical_path.is_file():
        return False, "canonical_missing"
    current_hash = content_hash_for_path(canonical_path)
    if current_hash is None:
        return False, "canonical_unreadable"
    if current_hash != canonical_hash:
        return False, "canonical_modified_since_fold"
    return True, "fresh"


# ---------------------------------------------------------------------------
# The direct-unfold write
# ---------------------------------------------------------------------------


def _restore_live_meta(meta: dict[str, object]) -> dict[str, object]:
    """Reverse :func:`athenaeum.models.stamp_tombstone`'s three stamped keys.

    See the module docstring's point 1 ("Unfold is a COMPENSATING
    operation") for why this clears rather than restores a prior value.
    """
    restored = dict(meta)
    restored.pop("folded_into", None)
    if restored.get("status") == "folded":
        restored.pop("status", None)
    if restored.get("embedded") is False:
        restored.pop("embedded", None)
    return restored


def _rewrite_links_back(
    wiki_root: Path, links_rewritten: list[Any], my_slug: str
) -> tuple[list[str], list[str]]:
    """Re-point every inbound wikilink THIS fold rewrote for *my_slug* back.

    Returns ``(touched, skipped_ambiguous)`` — see the module docstring's
    "Known ledger-contract gap #2" for why a file is ever skipped rather
    than rewritten.
    """
    collision_keys: dict[tuple[Any, Any], set[Any]] = {}
    for entry in links_rewritten:
        if not isinstance(entry, dict):
            continue
        key = (entry.get("path"), entry.get("to_slug"))
        collision_keys.setdefault(key, set()).add(entry.get("from_slug"))

    touched: list[str] = []
    skipped: list[str] = []
    for entry in links_rewritten:
        if not isinstance(entry, dict):
            continue
        if entry.get("from_slug") != my_slug:
            continue
        rel = entry.get("path")
        to_slug = entry.get("to_slug")
        if not isinstance(rel, str) or not isinstance(to_slug, str):
            continue
        key = (rel, to_slug)
        if len(collision_keys.get(key, set())) > 1:
            # Another source folded into the same canonical ALSO rewrote a
            # link in this exact file to this exact slug — the two
            # occurrences are textually indistinguishable now. Refuse
            # rather than risk flipping a sibling's still-folded link.
            if rel not in skipped:
                skipped.append(rel)
            continue
        target_path = Path(wiki_root) / rel
        try:
            text = target_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        def _replace(m: "re.Match[str]", _to_slug: str = to_slug, _my_slug: str = my_slug) -> str:
            link_target = m.group(1).strip()
            if slugify(link_target) != _to_slug:
                return m.group(0)
            alias_suffix = m.group(2) or ""
            return f"[[{_my_slug}{alias_suffix}]]"

        new_text = _WIKILINK_RE.sub(_replace, text)
        if new_text != text:
            atomic_write_text(target_path, new_text)
            if rel not in touched:
                touched.append(rel)
    return touched, skipped


# ---------------------------------------------------------------------------
# The queued-proposal path
# ---------------------------------------------------------------------------


def _best_effort_diff(
    canonical_text: str,
    tombstone_text: str,
    *,
    canonical_label: str,
    tombstone_label: str,
) -> str:
    """A unified diff of *tombstone_text* (what would be restored) against
    *canonical_text* (what the canonical says today) — NOT a diff against the
    canonical's post-fold state, which the pinned ledger contract does not
    retain (see the module docstring's "Known ledger-contract gap")."""
    return "".join(
        difflib.unified_diff(
            tombstone_text.splitlines(keepends=True),
            canonical_text.splitlines(keepends=True),
            fromfile=tombstone_label,
            tofile=canonical_label,
        )
    )


_REASON_EXPLANATIONS: dict[str, str] = {
    "canonical_modified_since_fold": (
        "The canonical page has changed since the fold (content-hash "
        "mismatch) -- a direct unfold would silently discard those later "
        "edits. A human must adjudicate the diff below before this source "
        "is restored."
    ),
    "ledger_record_missing_unfold_fields": (
        "The matching merge-provenance record predates issue athenaeum#716's "
        "unfold fields (folded_sources/links_rewritten/"
        "canonical_content_hash) -- there is no rewrite list to re-point "
        "inbound links from, so this cannot be unfolded automatically even "
        "if the canonical is otherwise untouched."
    ),
    "no_ledger_record": (
        "No fold-into-existing provenance record names this page as a "
        "folded source -- it may predate the merge-provenance ledger "
        "(issue athenaeum#425) entirely. Restoring it requires a human to "
        "re-point any inbound links by hand."
    ),
    "canonical_missing": (
        "The fold's canonical target page no longer exists on disk -- "
        "restoring this source requires a human decision about where it "
        "should live now."
    ),
    "canonical_unreadable": (
        "The fold's canonical target page exists but could not be read -- "
        "a human should investigate before this source is restored."
    ),
    "tombstone_missing_fold_target": (
        "This page is stamped as a tombstone (status: folded) but carries "
        "no folded_into target -- a human needs to determine where it was "
        "folded into, if anywhere, before it can be restored."
    ),
}


def _queue_unfold_proposal(
    tombstone_path: Path,
    *,
    canonical_slug: str | None,
    wiki_root: Path,
    pending_path: Path | None,
    reason: str,
    diff_text: str | None,
    now: datetime | None,
) -> dict[str, Any]:
    """Raise a queued unfold-adjudication proposal.

    Issue athenaeum#716 CORRECTIONS #6: reuses the existing ``type:
    "question"`` queue item end to end (via
    :func:`athenaeum.answers.raise_pending_question`, ``kind="question"``,
    its default) rather than adding a new ``decisions.py`` type or payload
    shape — the smallest-footprint option the corrections call out, so issue
    athenaeum#717's future unified-queue schema work has nothing of this
    lane's to absorb. All of this proposal's structured facts (the ledger
    reason, the canonical slug, the diff) are carried in the block's
    ``context``/description text rather than a structured payload field,
    since ``question_to_decision`` has no free-form payload slot today.
    """
    from athenaeum.answers import raise_pending_question

    pending_path = pending_path or (Path(wiki_root) / "_pending_questions.md")
    title = tombstone_path.stem
    target_desc = canonical_slug or "(no fold target recorded)"
    question = f'Restore "{title}" by unfolding it back out of "{target_desc}"?'
    context_lines = [
        f"Tombstone: {tombstone_path}",
        f"Fold target (canonical): {target_desc}",
        f"Not directly unfoldable: {reason}.",
        _REASON_EXPLANATIONS.get(reason, ""),
    ]
    if diff_text:
        context_lines += [
            "",
            "Diff: the page that would be restored vs. the canonical's "
            "CURRENT text (NOT a diff against the canonical's state "
            "immediately after the fold -- the ledger does not retain that "
            "snapshot, only its hash; see athenaeum#716 lane C's reported "
            "ledger-contract gap).",
            "```diff",
            diff_text.rstrip("\n"),
            "```",
        ]
    context = "\n".join(line for line in context_lines if line)
    return raise_pending_question(
        pending_path,
        question,
        context,
        entity=title,
        source=_wiki_relpath(wiki_root, tombstone_path),
        now=now,
        kind="question",
    )


# ---------------------------------------------------------------------------
# Repair-debt counters (issue athenaeum#716 instrumentation AC)
# ---------------------------------------------------------------------------


def load_repair_debt_counters(cache_dir: Path | None = None) -> RepairDebtCounters:
    """Load the persisted unfold outcome counters. Missing/corrupt -> zeros
    (fail-open, mirroring :func:`athenaeum.zero_yield.load_state`)."""
    fresh: RepairDebtCounters = {"unfold_direct": 0, "unfold_queued": 0}
    path = resolve_cache_dir(cache_dir) / REPAIR_DEBT_COUNTERS_NAME
    if not path.exists():
        return fresh
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fresh
    if not isinstance(data, dict):
        return fresh
    direct = data.get("unfold_direct")
    queued = data.get("unfold_queued")
    if not isinstance(direct, int) or isinstance(direct, bool) or direct < 0:
        direct = 0
    if not isinstance(queued, int) or isinstance(queued, bool) or queued < 0:
        queued = 0
    return {"unfold_direct": direct, "unfold_queued": queued}


def _record_unfold_outcome(cache_dir: Path | None, *, direct: bool) -> RepairDebtCounters:
    resolved = resolve_cache_dir(cache_dir)
    counters = load_repair_debt_counters(resolved)
    if direct:
        counters["unfold_direct"] += 1
    else:
        counters["unfold_queued"] += 1
    path = resolved / REPAIR_DEBT_COUNTERS_NAME
    atomic_write_text(
        path,
        json.dumps({**counters, "updated": now_iso()}, indent=2, sort_keys=True) + "\n",
    )
    return counters


# ---------------------------------------------------------------------------
# The single entry point
# ---------------------------------------------------------------------------


def unfold_page(
    tombstone_path: Path,
    *,
    wiki_root: Path,
    pending_path: Path | None = None,
    cache_dir: Path | None = None,
    now: datetime | None = None,
) -> UnfoldResult:
    """Unfold *tombstone_path* — the single entry point (issue athenaeum#716).

    Reads the tombstone's own frontmatter to find its ``folded_into``
    target, looks up the matching merge-provenance record
    (:func:`find_fold_record`), and decides direct vs. queued
    (:func:`can_unfold_directly`) — see the module docstring for the full
    contract, including the two named ledger-contract gaps. Every call
    increments the persisted repair-debt counters
    (:func:`load_repair_debt_counters`) regardless of outcome.

    Raises :class:`ValueError` if *tombstone_path* is not stamped as a
    tombstone (``status: folded``) — there is nothing to unfold.
    """
    wiki_root = Path(wiki_root)
    tombstone_path = Path(tombstone_path)
    text = tombstone_path.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(text)
    if not is_tombstone(meta):
        raise ValueError(
            f"{tombstone_path} is not a tombstone (status != 'folded'); nothing to unfold"
        )

    my_slug = _page_slug(tombstone_path)
    canonical_slug = tombstone_target(meta)

    if canonical_slug is None:
        _queue_unfold_proposal(
            tombstone_path,
            canonical_slug=None,
            wiki_root=wiki_root,
            pending_path=pending_path,
            reason="tombstone_missing_fold_target",
            diff_text=None,
            now=now,
        )
        _record_unfold_outcome(cache_dir, direct=False)
        return UnfoldResult(
            action=UNFOLD_QUEUED,
            tombstone_path=str(tombstone_path),
            canonical_slug=None,
            details={"reason": "tombstone_missing_fold_target"},
        )

    canonical_path = _path_for_slug(wiki_root, canonical_slug)
    record = find_fold_record(wiki_root, tombstone_path, canonical_slug)
    ok, reason = can_unfold_directly(record, canonical_path)

    if ok:
        assert record is not None  # narrows for mypy; can_unfold_directly guarantees it
        links_rewritten = record.get("links_rewritten") or []
        touched, skipped = _rewrite_links_back(wiki_root, links_rewritten, my_slug)
        restored_meta = _restore_live_meta(meta)
        atomic_write_text(tombstone_path, render_frontmatter(restored_meta) + body)
        details: dict[str, Any] = {
            "merge_id": record.get("merge_id"),
            "links_repointed": touched,
        }
        if skipped:
            details["links_skipped_ambiguous"] = skipped
        _record_unfold_outcome(cache_dir, direct=True)
        return UnfoldResult(
            action=UNFOLD_DIRECT,
            tombstone_path=str(tombstone_path),
            canonical_slug=canonical_slug,
            details=details,
        )

    diff_text: str | None = None
    if canonical_path is not None and canonical_path.is_file():
        try:
            canonical_text = canonical_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            canonical_text = None
        if canonical_text is not None:
            diff_text = _best_effort_diff(
                canonical_text,
                text,
                canonical_label=f"{canonical_slug} (current)",
                tombstone_label=f"{tombstone_path.name} (page that would be restored)",
            )

    _queue_unfold_proposal(
        tombstone_path,
        canonical_slug=canonical_slug,
        wiki_root=wiki_root,
        pending_path=pending_path,
        reason=reason,
        diff_text=diff_text,
        now=now,
    )
    _record_unfold_outcome(cache_dir, direct=False)
    details = {"reason": reason}
    if record is not None:
        details["merge_id"] = record.get("merge_id")
    return UnfoldResult(
        action=UNFOLD_QUEUED,
        tombstone_path=str(tombstone_path),
        canonical_slug=canonical_slug,
        details=details,
    )


__all__ = [
    "UNFOLD_DIRECT",
    "UNFOLD_QUEUED",
    "REPAIR_DEBT_COUNTERS_NAME",
    "UnfoldResult",
    "RepairDebtCounters",
    "find_fold_record",
    "can_unfold_directly",
    "load_repair_debt_counters",
    "unfold_page",
]
