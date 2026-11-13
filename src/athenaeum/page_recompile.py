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
0.23.5. Two gaps, both fixed here:

1. No active-page filter -- a tombstoned/retired/superseded page could
   resurface as a phantom candidate. Fixed by excluding every page
   :func:`athenaeum.page_relations.is_inactive_relation_target` calls
   inactive, the SAME composite predicate athenaeum#2535 already uses to
   refuse a parent attach onto exactly those three states -- not a fresh
   tombstone/retired/superseded check (see that function's own docstring).
2. `split_pages` drops a candidate with no uid through its `if c.uid`
   dict-build filter, so a blocked page disappears from the report
   entirely. Fixed by routing every page the pass cannot act on (no uid,
   unreadable, or frontmatter that does not parse to a mapping) into a
   named `blocked` bucket instead of a silent `continue`.

`oversize_split.py` itself is left untouched by this slice (its retirement
is a later one); this module imports its `NEEDS_SPLIT_KEY` constant rather
than redefining the same frontmatter key under a second name.

**Title detection (AC5) reuses, not duplicates.** The segmenter's heading
grouping is :func:`athenaeum.tiers._split_into_atomic_sections` verbatim --
including the athenaeum#2524 fix that treats a lone top-level heading
spanning the whole body as a page TITLE (folded into the leading no-heading
group) rather than a one-child split group. Imported function-local inside
:func:`segment_page`, mirroring `oversize_split.py`'s own deferred import of
the same `tiers.py` symbol, so merely importing this module never pays
`tiers.py`'s own (unrelated, LLM-chain) import weight.

Layering: L4 domain/pipeline, same layer as :mod:`athenaeum.oversize_split`
and :mod:`athenaeum.page_decompose` (its nearest sibling in shape: a
read-mostly, dry-run-shaped maintenance pass over the wiki tree). Imports
:mod:`athenaeum.config` (L2), :mod:`athenaeum.footnote_markers` and
:mod:`athenaeum.models` (both L1), and :mod:`athenaeum.page_relations` (L3)
at module scope -- all at or below this module's own L4, so none of these
edges needs an :data:`tests.test_layer_boundary.ALLOWED_UPWARD_EDGES`
entry. The one same-layer edge (:mod:`athenaeum.oversize_split`, also L4,
for its `NEEDS_SPLIT_KEY` constant only) is permitted by that same guard
(an importer's layer must be `>=`, not `>`, the imported module's layer).
:mod:`athenaeum.tiers` (also L4) is imported function-local inside
:func:`segment_page` only, for the reason stated above.
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
from athenaeum.oversize_split import NEEDS_SPLIT_KEY
from athenaeum.page_relations import is_inactive_relation_target

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

    1. Over the hard ceiling first -- a ceiling page already breaches the
       write-time merge gate (:mod:`athenaeum.oversize_split`'s own ``--auto``
       threshold), so it is the most urgent regardless of how it compares in
       size or recency to a merely-flagged candidate.
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
    gaps this closes relative to :func:`athenaeum.oversize_split.find_split_candidates`.

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


def segment_page(body: str) -> SegmentResult:
    """Cut *body* into ordered, addressable :class:`Unit` objects plus the
    trailing footnote-definitions block (issue athenaeum#2534 AC4/AC5/AC6).

    Heading grouping reuses :func:`athenaeum.tiers._split_into_atomic_sections`
    verbatim (AC5) -- see the module docstring for why this is a reuse, not a
    second copy. A leaf group over :data:`UNIT_CAP_CHARS` is cut into
    multiple units at paragraph/list-item boundaries (AC6,
    :func:`_pack_at_boundaries`); every unit's ``footnote_labels`` is the
    inline ``[^label]`` markers its own text cites
    (:data:`athenaeum.footnote_markers.INLINE_MARKER_RE`), in order of first
    appearance.
    """
    from athenaeum.tiers import _split_into_atomic_sections

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
