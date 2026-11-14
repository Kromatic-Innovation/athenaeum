# SPDX-License-Identifier: Apache-2.0
"""Page recompile: candidate scan and unit segmenter (issue athenaeum#2534,
slice S1 of the page-recompile design, athenaeum#2525). No LLM call, no page
write.

The design on athenaeum#2525 has code cut a page into units, a model assign
each unit a destination by id, and code move the units. This module is the
deterministic FRONT HALF of that pipeline: a candidate scan
(:func:`find_recompile_candidates`) and a segmenter
(:func:`segment_page`) that cuts a page body into ordered, addressable
units. Assignment and the move itself are later slices' jobs.

**Why a new scan rather than `oversize_split.find_split_candidates`.**
Verified by reading that function and one dry run of `athenaeum
split-oversize` (which listed more candidates than outcomes) at deploy
0.23.5, back when both still existed -- issue athenaeum#2550 later retired
`oversize_split.py` and `athenaeum split-oversize` outright, once this
module's own `athenaeum recompile` surface had superseded them. Two gaps
this scan fixed relative to that retired one:

1. No active-page filter -- a tombstoned/retired/superseded page could
   resurface as a phantom candidate. Fixed by excluding every page
   :func:`athenaeum.page_relations.is_inactive_relation_target` calls
   inactive, the SAME composite predicate athenaeum#2535 already uses to
   refuse a parent attach onto exactly those three states -- not a fresh
   tombstone/retired/superseded check (see that function's own docstring).
2. `split_pages` dropped a candidate with no uid through its `if c.uid`
   dict-build filter, so a blocked page disappeared from the report
   entirely. Fixed by routing every page the pass cannot act on (no uid,
   unreadable, or frontmatter that does not parse to a mapping) into a
   named `blocked` bucket instead of a silent `continue`.

`NEEDS_SPLIT_KEY` (the frontmatter flag :func:`athenaeum.tiers.
check_page_size_gate` sets) was originally imported from
`oversize_split.py`; it is defined locally in this module now that
`oversize_split.py` is gone and this is the key's only reader.

**Title detection (AC5).** The segmenter's heading grouping is
:func:`_split_into_atomic_sections` in this module -- including the
athenaeum#2524 fix that treats a lone top-level heading spanning the whole
body as a page TITLE (folded into the leading no-heading group) rather
than a one-child split group. That function (and the
`_SECTION_HEADING_RE` pattern it matches on) used to live in
:mod:`athenaeum.tiers`, shared with the reactive split path
`check_page_size_gate` drove; issue athenaeum#2550 retired that path and
relocated the function here, to its only remaining caller.

Layering: L4 domain/pipeline, same layer as :mod:`athenaeum.page_decompose`
(its nearest sibling in shape: a read-mostly, dry-run-shaped maintenance
pass over the wiki tree). Imports :mod:`athenaeum.config` (L2),
:mod:`athenaeum.footnote_markers` and :mod:`athenaeum.models` (both L1),
and :mod:`athenaeum.page_relations` (L3) at module scope -- all at or below
this module's own L4, so none of these edges needs an
:data:`tests.test_layer_boundary.ALLOWED_UPWARD_EDGES` entry. Since issue
athenaeum#2550 relocated `_split_into_atomic_sections` here (and inlined
`NEEDS_SPLIT_KEY` locally, above), this module imports nothing at all from
:mod:`athenaeum.tiers` -- not even function-local.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from athenaeum.config import (
    resolve_oversize_hard_ceiling_chars,
    resolve_page_size_threshold_chars,
)
from athenaeum.footnote_markers import (
    FOOTNOTE_DEF_RE,
    INLINE_MARKER_RE,
    parse_footnote_definitions,
)
from athenaeum.models import parse_frontmatter, validity_bound_str
from athenaeum.page_relations import is_inactive_relation_target

#: Frontmatter key :func:`athenaeum.tiers.check_page_size_gate` sets on a
#: page it let a merge into while over the page-size threshold. Formerly
#: imported from :mod:`athenaeum.oversize_split` (retired by issue
#: athenaeum#2550, along with the CLI pass that read it); this module is
#: now the key's only reader, so it is defined locally rather than kept
#: alive in a deleted module just to be re-exported.
NEEDS_SPLIT_KEY = "needs_split"

# ---------------------------------------------------------------------------
# AC1 / AC2 / AC3: candidate scan.
# ---------------------------------------------------------------------------

#: Same frontmatter-fence SHAPE `models._FM_RE` matches, duplicated here
#: deliberately as a narrow, LOCAL positive-signal check rather than
#: reaching into that private regex. `models.parse_frontmatter` fails OPEN
#: on a `yaml.YAMLError` -- returns `({}, text)`, the SAME shape it returns
#: for "no frontmatter at all" and for a genuinely empty frontmatter block
#: (own docstring: "every caller's existing `except yaml.YAMLError`
#: handling is unchanged") -- so a caller cannot tell "unparseable" apart
#: from "absent"/"empty" just by looking at the collapsed `({}, text)`
#: result. `models.py` exposes no public primitive that makes this
#: distinction (checked: `is_tombstone`, `parse_superseded_by`, etc. all
#: operate on an already-parsed `meta` dict), so this module keeps its own
#: check instead of guessing.
_FRONTMATTER_BLOCK_RE = re.compile(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n", re.DOTALL)


def _frontmatter_block_is_malformed(text: str) -> bool:
    """True when *text* opens with a frontmatter-shaped fence block whose
    YAML does not parse at all (issue athenaeum#2534 AC2 -- a Seer finding
    on PR athenaeum#2567 caught this: a page with unparseable YAML between
    the fences used to fall through `find_recompile_candidates` either
    silently dropped or mislabeled `no_uid`, because
    :func:`athenaeum.models.parse_frontmatter`'s `({}, text)` fail-open
    return is indistinguishable from "no frontmatter at all").

    Re-attempts the SAME match-then-parse locally, with its own
    ``yaml.YAMLError`` catch, rather than trusting the already-collapsed
    result. A block that matches the fence shape but parses to ``None`` or
    ``{}`` (a genuinely EMPTY frontmatter block -- valid YAML, just no
    fields) is NOT malformed by this check: only a block that fails to
    parse at all is. No match at all (no ``---`` fence present) is also not
    malformed -- that page never attempted frontmatter in the first place;
    see :func:`find_recompile_candidates`'s own comment for where that
    case is deliberately routed instead.
    """
    match = _FRONTMATTER_BLOCK_RE.match(text)
    if match is None:
        return False
    try:
        yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return True
    return False


@dataclass(frozen=True)
class RecompileCandidate:
    """One active page over the page-size invariant (issue athenaeum#2534 AC1)."""

    path: Path
    uid: str
    chars: int
    flagged: bool  # carries ``needs_split: true``
    over_ceiling: bool  # body above the hard ceiling
    updated: str  # normalized ISO date, or "" if absent/unparseable


@dataclass(frozen=True)
class BlockedPage:
    """One page the pass cannot act on (issue athenaeum#2534 AC2) -- reported,
    never silently dropped. ``reason`` is one of ``no_uid``,
    ``invalid_frontmatter`` (frontmatter present but does not parse to a
    mapping), or ``unreadable`` (the file itself could not be read)."""

    path: Path
    uid: str
    reason: str


@dataclass(frozen=True)
class CandidateScan:
    """The candidate scan's full result: what the pass CAN act on, ordered
    (:attr:`candidates`, see :func:`_candidate_sort_key`), and what it
    cannot (:attr:`blocked`)."""

    candidates: tuple[RecompileCandidate, ...]
    blocked: tuple[BlockedPage, ...]


def _recency_sort_key(updated_iso: str) -> int:
    """Negative ordinal day-number for *updated_iso*; ``""`` sorts as the
    oldest possible date rather than crashing or tying with a real one."""
    if updated_iso:
        try:
            return -date.fromisoformat(updated_iso).toordinal()
        except ValueError:
            pass
    return -date.min.toordinal()


def _candidate_sort_key(candidate: RecompileCandidate) -> tuple[int, int, int]:
    """AC3's ordering rule, stated once at its own call site so the rationale
    travels with the code that enforces it (the AC requires this, not just a
    docstring elsewhere):

    1. Over the hard ceiling first -- a ceiling page already breaches
       ``librarian.oversize_hard_ceiling_chars`` (the same knob the now-
       retired ``athenaeum split-oversize --auto`` used to read), so it is
       the most urgent regardless of how it compares in size or recency to
       a merely-flagged candidate.
    2. Then most recently ``updated`` -- a page someone is actively editing
       is both more likely to grow past the threshold again and more likely
       to still be in a human's or an agent's working set right now.
    3. Then larger first -- among candidates equally urgent and equally
       fresh, the one carrying the most page-size debt goes first.
    """
    return (
        0 if candidate.over_ceiling else 1,
        _recency_sort_key(candidate.updated),
        -candidate.chars,
    )


def sort_candidates(
    candidates: "Iterable[RecompileCandidate]",
) -> tuple[RecompileCandidate, ...]:
    """*candidates* in AC3 order (:func:`_candidate_sort_key`).

    Public so a caller that assembles candidates from its OWN walk --
    :func:`athenaeum.status.scan_page_backlog`, which reads each page once
    and feeds the text to :func:`classify_page_text` below rather than
    walking the wiki a second time (issue athenaeum#2536 AC3) -- gets the
    same ordering as :func:`find_recompile_candidates` without reaching
    into a private sort key.
    """
    return tuple(sorted(candidates, key=_candidate_sort_key))


def classify_page_text(
    path: Path, text: str, *, threshold: int, ceiling: int
) -> "RecompileCandidate | BlockedPage | None":
    """Classify ONE already-read page: a :class:`RecompileCandidate`, a
    :class:`BlockedPage`, or ``None`` for a page this pass has nothing to
    say about (inactive, or under the threshold and not flagged).

    Extracted verbatim from :func:`find_recompile_candidates`'s loop body
    (issue athenaeum#2536 AC3) so a caller that has ALREADY read a page's
    text can reuse the exact same predicates -- the inactive-page filter,
    the malformed-frontmatter split, the no-uid blocked bucket -- instead
    of either walking the wiki a second time or growing a second,
    drift-prone copy of them. Takes *text*, not *path* alone: the caller
    owns the read (and therefore owns the ``unreadable`` outcome, which
    cannot arise here).
    """
    meta, body = parse_frontmatter(text)
    if not isinstance(meta, dict):
        # Frontmatter block present but not a mapping (e.g. a bare YAML
        # list between the ``---`` fences) -- cannot check needs_split,
        # uid, or the inactive-page predicate, so this page is reported
        # rather than silently skipped (AC2).
        return BlockedPage(path=path, uid="", reason="invalid_frontmatter")
    if not meta and _frontmatter_block_is_malformed(text):
        # Genuinely unparseable YAML: parse_frontmatter fails open to
        # ({}, text) here -- the SAME shape as "no frontmatter at all"
        # (see _frontmatter_block_is_malformed's docstring) -- and
        # `body` in this branch is the WHOLE text including the ``---``
        # fences, so a size/threshold check on it would be meaningless.
        # Checked and blocked here, BEFORE the threshold gate below, so
        # it is reported regardless of size rather than only when it
        # happens to be large enough to look like a candidate (AC2).
        return BlockedPage(path=path, uid="", reason="invalid_frontmatter")
    if is_inactive_relation_target(meta):
        return None
    flagged = meta.get(NEEDS_SPLIT_KEY) is True
    over = len(body) > threshold
    if not (flagged or over):
        return None
    uid = str(meta.get("uid") or "")
    if not uid:
        # Deliberate: a page with NO frontmatter at all (meta == {} and
        # _frontmatter_block_is_malformed(text) is False -- no ``---``
        # fence was ever found) lands here too, as an ordinary missing-
        # uid page, not as `invalid_frontmatter` -- nothing about it is
        # malformed, it simply never had a uid to find. It is only
        # reported at all once it already qualifies as a candidate
        # (flagged/over threshold) above; a small plain-text file with
        # no frontmatter is unremarkable and never reaches this line.
        return BlockedPage(path=path, uid="", reason="no_uid")
    return RecompileCandidate(
        path=path,
        uid=uid,
        chars=len(body),
        flagged=flagged,
        over_ceiling=len(body) > ceiling,
        updated=validity_bound_str(meta, "updated"),
    )


def find_recompile_candidates(
    wiki_root: Path, config: dict[str, Any] | None = None
) -> CandidateScan:
    """Active pages over ``librarian.page_size_threshold_chars`` or flagged
    ``needs_split: true`` (issue athenaeum#2534 AC1), ordered per
    :func:`_candidate_sort_key` (AC3). See the module docstring for the two
    gaps this closed relative to the retired ``oversize_split.find_split_candidates``.

    The walk itself lives here; the per-page predicates live in
    :func:`classify_page_text` above, so a caller already holding a page's
    text (issue athenaeum#2536) reuses them rather than re-deriving them.
    """
    threshold = resolve_page_size_threshold_chars(config)
    ceiling = resolve_oversize_hard_ceiling_chars(config)
    candidates: list[RecompileCandidate] = []
    blocked: list[BlockedPage] = []
    for fpath in sorted(wiki_root.glob("*.md")):
        if fpath.name.startswith("_"):
            continue
        try:
            text = fpath.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            blocked.append(BlockedPage(path=fpath, uid="", reason="unreadable"))
            continue
        outcome = classify_page_text(fpath, text, threshold=threshold, ceiling=ceiling)
        if isinstance(outcome, RecompileCandidate):
            candidates.append(outcome)
        elif isinstance(outcome, BlockedPage):
            blocked.append(outcome)
    return CandidateScan(candidates=sort_candidates(candidates), blocked=tuple(blocked))


# ---------------------------------------------------------------------------
# AC4 / AC5 / AC6: segmenter.
# ---------------------------------------------------------------------------

#: A leaf heading group's text over this many characters is cut at paragraph
#: or list-item boundaries into multiple units (AC6). A plain module
#: constant, not a ``librarian.*`` config knob -- the AC asks for a cap, not
#: an operator-tunable one; add a resolver if a later slice needs one.
UNIT_CAP_CHARS = 4000

#: A blank-line run -- a paragraph boundary cut point.
_PARAGRAPH_BREAK_RE = re.compile(r"\n[ \t]*\n+")

#: The start of a list-item line -- a cut point even with no blank line
#: before it, so a long bulleted list can be cut per-item (AC6).
_LIST_ITEM_START_RE = re.compile(r"^[ \t]{0,3}(?:[-*+]|\d+[.)])[ \t]", re.MULTILINE)


@dataclass(frozen=True)
class Unit:
    """One ordered, addressable slice of a page body (issue athenaeum#2534 AC4).

    ``unit_id`` is ``f"{ordinal:04d}-{hash}"`` (hash = first 12 hex chars of
    a sha256 over ``text``): the ordinal makes reading order legible at a
    glance, the hash changes when and only when the content does, which is
    what :func:`check_conservation` keys on.
    """

    unit_id: str
    ordinal: int
    heading_path: tuple[str, ...]
    text: str
    footnote_labels: tuple[str, ...]


@dataclass(frozen=True)
class SegmentResult:
    """A page body cut into :attr:`units` plus the trailing
    :attr:`footnote_block` (verbatim). ``"".join(u.text for u in units) +
    footnote_block`` reproduces the original body byte for byte (AC6)."""

    units: tuple[Unit, ...]
    footnote_block: str


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _split_footnote_block(body: str) -> tuple[str, str]:
    """``(prose, footnote_block)``.

    ``footnote_block`` is the maximal TRAILING run of *body* in which every
    non-blank line is a footnote DEFINITION (:data:`athenaeum.footnote_markers.FOOTNOTE_DEF_RE`)
    -- the layout :func:`athenaeum.merge.render_source_footnotes` already
    writes. When the tail from the first definition onward contains
    anything else (prose interleaved with definitions), this function makes
    no claim about where a "footnote block" would even be and falls back to
    ``(body, "")`` -- *prose* unchanged, no block extracted -- which keeps
    the AC6 byte-identity property trivially true in that case too, rather
    than guessing at a layout it cannot verify.
    """
    matches = list(FOOTNOTE_DEF_RE.finditer(body))
    if not matches:
        return body, ""
    first_start = matches[0].start()
    tail = body[first_start:]
    for line in tail.splitlines():
        if line.strip() == "":
            continue
        if not FOOTNOTE_DEF_RE.match(line):
            return body, ""
    return body[:first_start], tail


def _boundary_offsets(text: str) -> list[int]:
    """Cut points for AC6: right after each blank-line paragraph break, and
    right before each list-item start line. Always includes ``0`` and
    ``len(text)``. Sorted and deduplicated, so the offsets partition *text*
    into contiguous, gap-free, overlap-free pieces -- concatenating the
    pieces in order always reproduces *text* exactly, independent of
    whether :data:`UNIT_CAP_CHARS` is ever exceeded.
    """
    offsets = {0, len(text)}
    for m in _PARAGRAPH_BREAK_RE.finditer(text):
        offsets.add(m.end())
    for m in _LIST_ITEM_START_RE.finditer(text):
        offsets.add(m.start())
    return sorted(offsets)


def _pack_at_boundaries(text: str, cap: int) -> list[str]:
    """Greedily pack *text*'s boundary-delimited pieces (see
    :func:`_boundary_offsets`) into chunks no longer than *cap* where
    possible (AC6). A single piece already over *cap* on its own (a
    paragraph or list item with no internal boundary) is kept whole --
    paragraph/list-item boundaries are the only cut points this function
    may use, so it never cuts mid-paragraph. Concatenating the returned
    chunks in order always reproduces *text* exactly.
    """
    offsets = _boundary_offsets(text)
    pieces = [text[offsets[i] : offsets[i + 1]] for i in range(len(offsets) - 1)]
    if not pieces:
        return [text] if text else []
    chunks: list[str] = []
    current = pieces[0]
    for piece in pieces[1:]:
        if not current or len(current) + len(piece) <= cap:
            current += piece
        else:
            chunks.append(current)
            current = piece
    chunks.append(current)
    return chunks


#: Same heading-match SHAPE :data:`athenaeum.tiers._SECTION_HEADING_RE`
#: uses, duplicated here deliberately (issue athenaeum#2550): this module's
#: own docstring documents importing nothing from :mod:`athenaeum.tiers`'s
#: own (heavier, LLM-chain-adjacent) import graph as a load-bearing
#: property, and after this retirement :func:`_split_into_atomic_sections`
#: below is the symbol's only remaining caller anywhere -- there is no
#: longer a "reuse, not a second copy" relationship to preserve by
#: importing the regex across that edge too.
_SECTION_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*)$", re.MULTILINE)


def _split_into_atomic_sections(body: str) -> list[tuple[str, str]]:
    """Split *body* into ``(heading_text, section_text)`` pairs at its
    TOP-LEVEL markdown heading depth (issue athenaeum#1248, relocated here
    from :mod:`athenaeum.tiers` by issue athenaeum#2550 once
    :func:`segment_page` below became its only caller) -- the shallowest
    ``#``-count actually present in the body, e.g. every ``##`` if the page
    has no ``#``. A deeper heading (a ``###`` under a ``##``) is a
    SUBSECTION and stays folded into its enclosing top-level section's text
    verbatim, so a page using nested heading structure splits into one
    atomic group per top-level section, not one per subsection too.

    A body with no heading at all returns ``[]`` -- :func:`segment_page`
    treats this the same as any other heading-less body (one leaf group
    covering the whole thing), not as an error.

    The presumptive top level is the FIRST heading's own depth, not the
    shallowest depth occurring anywhere in the body. A boundary match is
    any heading AT OR SHALLOWER than that depth (``<=``, not ``==``) --
    so a heading that is merely shallower than the first one but appears
    LATER (e.g. a trailing ``# Appendix`` after ``## Section A`` / ``##
    Section B``) is still an ordinary section boundary, not grounds to
    re-derive the top level from it; it does not get swallowed into the
    preceding section's text (issue athenaeum#2524's review finding on
    this exact shape).

    Issue athenaeum#2524: when there is exactly ONE such boundary match,
    it can only be the first heading itself (every boundary match is, by
    construction, at or shallower than the first heading's own depth, and
    the first heading always qualifies) -- i.e. it genuinely spans the
    whole body. That is a page TITLE (e.g. a lone ``# <Name>`` carrying
    the whole body), not a section to split on -- it has nothing to be
    split *from* at its own depth. In that case the split depth moves one
    level down, to the shallowest depth actually present BENEATH that
    lone heading, so a ``# Name`` page whose real structure lives under
    ``##`` splits on the ``##`` headings instead of yielding one child
    holding the entire body. The lone title heading and any text
    before/after it at that depth folds into the leading ``("",
    preamble_text)`` pair like any other pre-heading content, same as any
    other pre-heading content. If nothing is nested beneath the lone
    heading, the depth is left alone -- a single resulting group is not
    this function's concern; it is purely a splitter, and leaves what to
    do with a single group to its caller.
    """
    matches = list(_SECTION_HEADING_RE.finditer(body))
    if not matches:
        return []
    top_level = len(matches[0].group(1))
    top_matches = [m for m in matches if len(m.group(1)) <= top_level]
    if len(top_matches) == 1:
        deeper = [m for m in matches if len(m.group(1)) > top_level]
        if deeper:
            top_level = min(len(m.group(1)) for m in deeper)
            top_matches = [m for m in deeper if len(m.group(1)) == top_level]
    sections: list[tuple[str, str]] = []
    if top_matches[0].start() > 0:
        sections.append(("", body[: top_matches[0].start()]))
    for i, m in enumerate(top_matches):
        end = top_matches[i + 1].start() if i + 1 < len(top_matches) else len(body)
        sections.append((m.group(2).strip(), body[m.start() : end]))
    return sections


def segment_page(body: str) -> SegmentResult:
    """Cut *body* into ordered, addressable :class:`Unit` objects plus the
    trailing footnote-definitions block (issue athenaeum#2534 AC4/AC5/AC6).

    Heading grouping is :func:`_split_into_atomic_sections` above (AC5) --
    moved into this module by issue athenaeum#2550 once this function
    became its only caller (the reactive split path in
    :mod:`athenaeum.tiers` that used to share it was retired by that same
    issue). A leaf group over :data:`UNIT_CAP_CHARS` is cut into
    multiple units at paragraph/list-item boundaries (AC6,
    :func:`_pack_at_boundaries`); every unit's ``footnote_labels`` is the
    inline ``[^label]`` markers its own text cites
    (:data:`athenaeum.footnote_markers.INLINE_MARKER_RE`), in order of first
    appearance.
    """
    prose, footnote_block = _split_footnote_block(body)
    sections = _split_into_atomic_sections(prose) or [("", prose)]

    units: list[Unit] = []
    ordinal = 0
    for heading, section_text in sections:
        heading_path = (heading,) if heading else ()
        for chunk in _pack_at_boundaries(section_text, UNIT_CAP_CHARS):
            if not chunk:
                continue
            labels = tuple(
                dict.fromkeys(m.group(1) for m in INLINE_MARKER_RE.finditer(chunk))
            )
            units.append(
                Unit(
                    unit_id=f"{ordinal:04d}-{_content_hash(chunk)}",
                    ordinal=ordinal,
                    heading_path=heading_path,
                    text=chunk,
                    footnote_labels=labels,
                )
            )
            ordinal += 1
    return SegmentResult(units=tuple(units), footnote_block=footnote_block)


# ---------------------------------------------------------------------------
# AC7: pure checker functions.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConservationResult:
    """Whether every input unit id was accounted for exactly once (issue
    athenaeum#2534 AC7). ``missing`` is present in the input but absent from
    every output group; ``duplicated`` appears in more than one (or twice in
    one) output group."""

    ok: bool
    missing: tuple[str, ...]
    duplicated: tuple[str, ...]


def check_conservation(
    unit_ids: Sequence[str], assigned: Mapping[str, Sequence[str]]
) -> ConservationResult:
    """Every id in *unit_ids* accounted for EXACTLY once across *assigned*'s
    values (issue athenaeum#2534 AC7) -- the pure check a later slice's
    "a model assigns each unit a destination by id, code moves the units"
    step runs before trusting an assignment. Pure: no I/O, no model.
    """
    seen: dict[str, int] = {}
    for ids in assigned.values():
        for uid in ids:
            seen[uid] = seen.get(uid, 0) + 1
    wanted = set(unit_ids)
    missing = tuple(sorted(uid for uid in wanted if seen.get(uid, 0) == 0))
    duplicated = tuple(
        sorted(uid for uid, count in seen.items() if uid in wanted and count > 1)
    )
    return ConservationResult(
        ok=not missing and not duplicated, missing=missing, duplicated=duplicated
    )


@dataclass(frozen=True)
class MarkerResolutionResult:
    """Whether every inline marker cited on a page resolves to a definition
    on that SAME page (issue athenaeum#2534 AC7). ``dangling`` is every
    cited label with no matching definition."""

    ok: bool
    dangling: tuple[str, ...]


def check_marker_resolution(body: str) -> MarkerResolutionResult:
    """Every inline ``[^label]`` marker in *body* has a ``[^label]: ...``
    definition somewhere on the same page (issue athenaeum#2534 AC7). Pure
    text check reusing :mod:`athenaeum.footnote_markers`'s own parsers --
    no I/O, no model.
    """
    definitions = parse_footnote_definitions(body)
    cited = {m.group(1) for m in INLINE_MARKER_RE.finditer(body)}
    dangling = tuple(sorted(cited - set(definitions)))
    return MarkerResolutionResult(ok=not dangling, dangling=dangling)
