# SPDX-License-Identifier: Apache-2.0
"""Inline per-claim footnote markers (issue athenaeum#1730) — L1 primitive.

Layer 1. Pure text arithmetic over a markdown body: no I/O, no config, no
model, no cost.

``docs/use-cases.md`` §3.5 requires that provenance survive compilation: a
compiled page is itself a source for the level above, so an agent following
breadcrumbs must be able to reach the source of *the sentence it is reading*,
not a page-level bibliography. Before this module the compile step appended
``[^src-N]`` footnote DEFINITIONS to the body
(:func:`athenaeum.merge.render_source_footnotes`) with nothing in the prose
referring to them — definitions with no referents, which is a bibliography
wearing footnote syntax.

This module owns the three text operations that close that gap, in ONE place
so they cannot drift apart:

* :func:`attach_markers` writes an inline ``[^label]`` marker onto every prose
  sentence of a passage.
* :func:`unmarked_sentence_ratio` measures how much of a page's prose carries
  no marker at all — the deterministic post-check surfaced by
  ``athenaeum status``.
* :func:`parse_footnote_definitions` resolves a marker back to the source
  string its definition renders, so a caller can go from a sentence to its
  source in one hop.

Writer and measurer share :func:`iter_prose_sentences`, which is the whole
reason they live together: a page written by :func:`attach_markers` scores
zero unmarked sentences by construction, because the same segmentation
decided what a sentence was in both directions. Two segmenters would drift
and the metric would stop describing the writer.

**Granularity, stated honestly.** A marker attaches at the granularity of the
passage handed to :func:`attach_markers` — for the auto-memory compile that is
one cluster MEMBER, so a member citing two sources marks its sentences with
both. That is a genuine improvement on the page-level union (a sentence now
resolves to the sources of the claim it came from, not to every source on the
page) and it is NOT true per-sentence provenance. Claims as addressable units
with their own coordinates is the dimensional memory model (athenaeum#709);
this module is the slice that does not depend on it.

**What counts as prose.** Markdown that is not a sentence — fenced code,
headings, footnote definitions, link-reference definitions, table rows, HTML
comments and blocks, and indented code — is skipped by both the writer and
the measurer. Blockquotes ARE prose: quoted material makes claims and needs a
citation like anything else.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

#: An inline reference marker — ``[^src-1]`` — as opposed to a DEFINITION
#: (``[^src-1]: ...``). The negative lookahead on ``:`` is what tells them
#: apart, and it is the only thing that does: both start the same way.
INLINE_MARKER_RE = re.compile(r"\[\^([^\]\s]+)\](?!:)")

#: A footnote DEFINITION line: ``[^src-1]: **Source:** ...``. Anchored at the
#: start of a line because a definition is a block-level construct.
FOOTNOTE_DEF_RE = re.compile(r"^\[\^([^\]\s]+)\]:[ \t]*(.*)$", re.MULTILINE)

#: A link-reference definition (``[ref]: https://...``) — NOT a footnote, and
#: not prose either. Kept distinct so a body full of link refs does not read
#: as a body full of uncited claims.
_LINK_REF_RE = re.compile(r"^\[[^\^\]]+\]:[ \t]*\S")

#: Fence openers/closers for code blocks. Both ``` and ~~~ families, any
#: length >= 3, optionally indented up to 3 spaces (CommonMark).
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")

#: An ATX heading (``# Title``). Setext headings (``Title`` / ``=====``) are
#: not detected; the underline line is caught by _RULE_RE below and the title
#: line reads as a one-line sentence, which is the safe direction to err.
_HEADING_RE = re.compile(r"^ {0,3}#{1,6}(\s|$)")

#: A thematic break or setext underline — ``---``, ``===``, ``***``, ``___``.
_RULE_RE = re.compile(r"^ {0,3}([-=*_])\1{2,}\s*$")

#: A markdown table row or delimiter row. A cell is not a sentence.
_TABLE_ROW_RE = re.compile(r"^ {0,3}\|")
_TABLE_DELIM_RE = re.compile(r"^ {0,3}:?-{3,}:?(\s*\|\s*:?-+:?)*\s*$")

#: Leading list-item / blockquote syntax, stripped before segmentation so a
#: bullet's text is measured as prose and a marker lands on the text rather
#: than inside the marker syntax.
_BLOCK_PREFIX_RE = re.compile(r"^(?:\s*(?:[-*+]\s+|\d+[.)]\s+|>\s?))*")

#: Four-space (or one-tab) indented code, outside a list context. Matching
#: this conservatively — a deeply nested list item also indents — is why the
#: check runs only on lines with no list prefix at all.
_INDENTED_CODE_RE = re.compile(r"^(?: {4,}|\t)\S")

#: Sentence boundary: terminal punctuation, then any closing quote/bracket and
#: any inline markers ALREADY present, then the whitespace separating it from
#: the next sentence. Only group 1 (that whitespace) is the separator — the
#: closers and markers belong to the sentence they follow, which is what makes
#: :func:`attach_markers` idempotent and makes an already-marked page measure
#: as marked. A variable-width lookbehind would say this more directly;
#: Python's ``re`` does not have one, hence the explicit group.
#:
#: Deliberately simple and deterministic — no abbreviation dictionary, no
#: model. It over-splits on "e.g." and "Dr. Who", which costs a marker on a
#: fragment; it never merges two claims into one, which would cost a citation.
_SENTENCE_BOUNDARY_RE = re.compile(
    r"""[.!?]                                   # terminal punctuation
        (?:["'’”)\]]|\[\^[^\]\s]+\])*  # closers, existing markers
        (\s+)                                   # the separator itself
    """,
    re.VERBOSE,
)

#: One word character. The test for "is there a claim here at all", applied
#: after block and footnote syntax is removed — see :func:`iter_prose_sentences`.
_WORD_RE = re.compile(r"\w")

#: HTML comment open/close, matched on a line so a multi-line comment can be
#: skipped as a block without a full HTML parser.
_HTML_COMMENT_OPEN = "<!--"
_HTML_COMMENT_CLOSE = "-->"


def _block_prefix(line: str) -> str:
    """The leading list-item / blockquote syntax on *line*, or ``""``.

    :data:`_BLOCK_PREFIX_RE` can match empty, so this never actually returns
    from the ``None`` branch — but the branch is written out rather than
    asserted away, so a later edit that makes the pattern non-empty-matchable
    degrades to "no prefix" instead of raising on a page in the wild.
    """
    match = _BLOCK_PREFIX_RE.match(line)
    return match.group(0) if match is not None else ""


def marker_label(index: int) -> str:
    """The stable footnote label for the *index*-th source (1-based).

    Mirrors :func:`athenaeum.merge.render_source_footnotes`, which labels its
    definitions ``src-1``, ``src-2``, ... over the deterministic deduped
    source order. Both sides derive the label from this function so a body
    marker and its definition can never disagree about the spelling.
    """
    return f"src-{index}"


def render_marker(label: str) -> str:
    """Render *label* as an inline reference marker (``src-1`` -> ``[^src-1]``)."""
    return f"[^{label}]"


@dataclass(frozen=True)
class Sentence:
    """One prose sentence located inside a markdown body.

    ``start`` / ``end`` are character offsets into the ORIGINAL body text, so
    a caller can splice without re-finding the text. ``markers`` is every
    inline footnote label the sentence already carries, in order.
    """

    text: str
    start: int
    end: int
    markers: tuple[str, ...]

    @property
    def is_marked(self) -> bool:
        """True when the sentence already carries at least one inline marker."""
        return bool(self.markers)


@dataclass(frozen=True)
class MarkerCoverage:
    """Deterministic unmarked-sentence measurement for one body.

    ``ratio`` is ``unmarked / total``, or ``0.0`` for a body with no prose
    sentences at all — a page with nothing to cite is not an uncited page, and
    reporting 1.0 there would flag every stub and index page in the corpus.
    """

    total: int
    unmarked: int

    @property
    def ratio(self) -> float:
        if self.total <= 0:
            return 0.0
        return self.unmarked / self.total


def _prose_line_spans(body: str) -> Iterator[tuple[int, int]]:
    """Yield ``(start, end)`` offsets of every PROSE line in *body*.

    Skips fenced code, headings, rules, footnote and link-reference
    definitions, table rows, HTML comments/blocks and indented code. See the
    module docstring for why blockquotes are deliberately NOT skipped.
    """
    offset = 0
    fence: str | None = None
    in_html_comment = False
    for line in body.splitlines(keepends=True):
        start = offset
        offset += len(line)
        stripped_line = line.rstrip("\n\r")
        end = start + len(stripped_line)
        text = stripped_line.strip()

        if in_html_comment:
            if _HTML_COMMENT_CLOSE in stripped_line:
                in_html_comment = False
            continue
        if fence is not None:
            if stripped_line.strip().startswith(fence):
                fence = None
            continue

        fence_open = _FENCE_RE.match(stripped_line)
        if fence_open:
            fence = fence_open.group(1)[:3]
            continue
        if text.startswith(_HTML_COMMENT_OPEN):
            if _HTML_COMMENT_CLOSE not in stripped_line:
                in_html_comment = True
            continue
        if not text:
            continue
        if _HEADING_RE.match(stripped_line) or _RULE_RE.match(stripped_line):
            continue
        if FOOTNOTE_DEF_RE.match(stripped_line) or _LINK_REF_RE.match(stripped_line):
            continue
        if _TABLE_ROW_RE.match(stripped_line) or _TABLE_DELIM_RE.match(stripped_line):
            continue
        if not _block_prefix(stripped_line).strip() and _INDENTED_CODE_RE.match(
            stripped_line
        ):
            continue
        if text.startswith("<") and text.endswith(">"):
            continue
        yield start, end


def _paragraph_spans(body: str) -> Iterator[tuple[int, int]]:
    """Group consecutive prose lines into paragraph spans.

    A sentence may wrap across lines, so segmentation runs over a paragraph
    rather than a line. Consecutive prose lines join; any skipped line (blank,
    code, heading, ...) ends the run.
    """
    run_start: int | None = None
    run_end = 0
    prev_end = -1
    for start, end in _prose_line_spans(body):
        contiguous = run_start is not None and body[prev_end:start].strip() == ""
        if not contiguous:
            if run_start is not None:
                yield run_start, run_end
            run_start = start
        run_end = end
        prev_end = end
    if run_start is not None:
        yield run_start, run_end


def iter_prose_sentences(body: str) -> Iterator[Sentence]:
    """Yield every prose :class:`Sentence` in *body*, in document order.

    The single segmentation both :func:`attach_markers` and
    :func:`unmarked_sentence_ratio` use — see the module docstring for why
    there is exactly one.
    """
    for para_start, para_end in _paragraph_spans(body):
        chunk = body[para_start:para_end]
        cursor = 0
        pieces: list[tuple[int, int]] = []
        for match in _SENTENCE_BOUNDARY_RE.finditer(chunk):
            if match.start(1) < cursor:
                continue
            pieces.append((cursor, match.start(1)))
            cursor = match.end(1)
        pieces.append((cursor, len(chunk)))
        for rel_start, rel_end in pieces:
            piece = chunk[rel_start:rel_end]
            lead = len(piece) - len(piece.lstrip())
            trail = len(piece) - len(piece.rstrip())
            body_start = para_start + rel_start + lead
            body_end = para_start + rel_end - trail
            if body_end <= body_start:
                continue
            text = body[body_start:body_end]
            # A bullet/blockquote marker is syntax, not prose, and neither is
            # an inline footnote marker. A piece with no WORD left after both
            # are removed is not a sentence: an empty list item (``-``) would
            # otherwise be counted as an uncited claim and then have a marker
            # stapled onto bare syntax (``-[^src-1]``).
            prefix = _block_prefix(text)
            remainder = INLINE_MARKER_RE.sub("", text[len(prefix) :])
            if not _WORD_RE.search(remainder):
                continue
            markers = tuple(m.group(1) for m in INLINE_MARKER_RE.finditer(text))
            yield Sentence(text=text, start=body_start, end=body_end, markers=markers)


def unmarked_sentence_ratio(body: str) -> MarkerCoverage:
    """Count prose sentences in *body* that carry no inline footnote marker.

    Deterministic — same bytes in, same numbers out, no model and no network.
    This is the post-check ``athenaeum status`` surfaces; it never blocks
    anything, matching the page-size guardrail's posture
    (``docs/why-athenaeum.md`` §5).
    """
    total = 0
    unmarked = 0
    for sentence in iter_prose_sentences(body):
        total += 1
        if not sentence.is_marked:
            unmarked += 1
    return MarkerCoverage(total=total, unmarked=unmarked)


def attach_markers(body: str, labels: Sequence[str]) -> str:
    """Append ``[^label]`` markers to every prose sentence of *body*.

    A label already present on a sentence is not repeated, so this is
    idempotent: re-running it over its own output is a no-op. Markers land
    AFTER the sentence's terminal punctuation, which is the Wikipedia
    convention and the one the footnote definitions already rendered by
    :func:`athenaeum.merge.render_source_footnotes` read naturally against.

    Returns *body* unchanged when *labels* is empty — a member with no
    resolvable source has nothing to cite, and inventing a marker for it
    would be worse than the page-level union this replaces.
    """
    if not labels:
        return body
    edits: list[tuple[int, str]] = []
    for sentence in iter_prose_sentences(body):
        addition = "".join(
            render_marker(label) for label in labels if label not in sentence.markers
        )
        if addition:
            edits.append((sentence.end, addition))
    if not edits:
        return body
    out: list[str] = []
    cursor = 0
    for position, addition in edits:
        out.append(body[cursor:position])
        out.append(addition)
        cursor = position
    out.append(body[cursor:])
    return "".join(out)


def parse_footnote_definitions(body: str) -> dict[str, str]:
    """Map every footnote label in *body* to the source string it defines.

    ``[^src-1]: **Source:** user-stated — \\`abc123#turn4\\``` yields
    ``{"src-1": "**Source:** user-stated — \\`abc123#turn4\\`"}``. This is the
    resolution step a caller would otherwise have to do by parsing markdown
    itself: ``read_entity`` and ``recall`` expose the result directly so a
    sentence reaches its source in one hop (issue athenaeum#1730).

    A repeated label keeps the FIRST definition, matching how a markdown
    renderer resolves a duplicate. Definitions are not continued across
    lines — the renderers in this codebase emit one line per definition.
    """
    out: dict[str, str] = {}
    for match in FOOTNOTE_DEF_RE.finditer(body):
        label = match.group(1)
        if label in out:
            continue
        out[label] = match.group(2).strip()
    return out


def resolve_markers(body: str, labels: Sequence[str] | None = None) -> dict[str, str]:
    """Resolve the inline markers actually USED in *body* to their sources.

    Unlike :func:`parse_footnote_definitions` (every definition on the page),
    this returns only the labels that some sentence cites, restricted further
    to *labels* when given — which is what a recall hit wants, since its
    snippet is an excerpt and resolving the page's whole bibliography onto it
    would reinstate exactly the page-level union this issue removes.

    A cited label with no definition on the page is omitted rather than mapped
    to an empty string: a dangling marker is a real condition, and a caller
    checking ``label in footnotes`` should get a truthful answer.
    """
    definitions = parse_footnote_definitions(body)
    wanted = set(labels) if labels is not None else None
    out: dict[str, str] = {}
    for match in INLINE_MARKER_RE.finditer(body):
        label = match.group(1)
        if wanted is not None and label not in wanted:
            continue
        if label in out or label not in definitions:
            continue
        out[label] = definitions[label]
    return out
