# SPDX-License-Identifier: Apache-2.0
"""Inference-block schema + parser (issue #424, data model only).

A page with ``memory_class: fact`` may contain one or more ``## Inference``
sections in its body — a fact derived FROM other facts rather than observed
directly. Each block declares:

- ``basis:`` — one or more Obsidian-style ``[[slug]]`` / ``[[slug|alias]]``
  wikilinks to the fact page(s) the inference is derived from.
- ``confidence:`` — a float in ``[0, 1]``.

Block grammar (mirrors the ``**Key**: value`` metadata-line convention used
by ``answers.py``'s ``_pending_questions.md`` blocks and
``pending_merges.py``'s ``_pending_merges.md`` blocks)::

    ## Inference
    **Basis**: [[fact-a]], [[fact-b|Fact B alias]]
    **Confidence**: 0.8
    The derived claim goes here, in prose, same as any other body text.

Each parsed block is an ADDRESSABLE unit (:class:`InferenceBlock`) with a
stable ``id`` derived from its content, exposing its ``basis`` list and
``confidence`` value plus enough of the raw block to be located again later.

Scope: this module is ONLY the schema + parser. The retraction machinery —
i.e. actually acting on a retracted basis fact by invalidating or
re-evaluating the dependent inference — is issue #433 and is NOT implemented
here. A malformed block (missing ``**Basis**:``, missing/unparseable
``**Confidence**:``, or a basis line with no recoverable wikilink) is
FLAGGED via :attr:`InferenceBlock.malformed` / :attr:`InferenceBlock.errors`
rather than silently dropped or silently accepted — the block is still
returned (visible) unless it isn't even recognizable as an ``## Inference``
header, since a linter needs to see broken blocks to report them.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

# Same wikilink grammar as ``resolutions._WIKILINK_RE`` / the base pattern
# ``pending_merges._WIKILINK_REWRITE_RE`` wraps (Obsidian-style ``[[slug]]``
# / ``[[slug|alias]]``). Kept as a separate, local pattern object rather
# than importing a private name from another module — matches
# ``pending_merges.py``'s own stated rationale for not sharing the compiled
# regex object across modules.
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|\n]+?)(?:\|[^\[\]\n]*)?\]\]")

_BASIS_RE = re.compile(r"^\*\*Basis\*\*:\s*(.*)$", re.IGNORECASE)
_CONFIDENCE_RE = re.compile(r"^\*\*Confidence\*\*:\s*(.*)$", re.IGNORECASE)


@dataclass
class InferenceBlock:
    """One parsed ``## Inference`` block — an addressable, retractable unit.

    ``id`` is a stable short hash over the raw block text so a caller can
    reference a specific block across a read/re-read cycle (the same
    stability contract ``answers._make_id`` uses for pending-question
    blocks) — NOT over ``basis``/``confidence`` alone, so an edit to the
    inference prose changes the id (a content change is a new unit) while
    re-parsing the same unedited text yields the same id.

    ``malformed`` is ``True`` when the block is missing a ``**Basis**:``
    line, missing/unparseable ``**Confidence**:``, or a ``**Basis**:`` line
    with no recoverable wikilink. ``errors`` lists the specific reasons
    (empty when ``malformed`` is ``False``). Malformed blocks are still
    returned by :func:`parse_inference_blocks` (flagged, not dropped) so a
    linter can report them.
    """

    id: str
    basis: list[str]
    confidence: float | None
    body: str
    raw_block: str
    malformed: bool = False
    errors: list[str] = field(default_factory=list)


def _extract_wikilink_slugs(text: str) -> list[str]:
    """Return the wikilink targets in ``text``, order-preserved, deduped."""
    slugs: list[str] = []
    seen: set[str] = set()
    for m in _WIKILINK_RE.finditer(text):
        raw = m.group(1).strip()
        if not raw or raw in seen:
            continue
        seen.add(raw)
        slugs.append(raw)
    return slugs


def _make_block_id(raw_block: str) -> str:
    """Stable id: a 12-hex-char SHA-1 prefix over the raw block text.

    Mirrors ``answers._make_id``'s stability contract, applied to the whole
    block instead of a header+question pair (an inference block has no
    separate "header line" carrying its identity — the block IS the unit).
    """
    payload = raw_block.strip().encode("utf-8")
    return hashlib.sha1(payload).hexdigest()[:12]


def _parse_one_block(raw_block: str) -> InferenceBlock:
    lines = raw_block.splitlines()
    # First line is the ``## Inference`` header; drop it before scanning
    # for metadata lines.
    body_lines: list[str] = []
    basis_line: str | None = None
    confidence_line: str | None = None

    for line in lines[1:]:
        m_basis = _BASIS_RE.match(line.strip())
        m_conf = _CONFIDENCE_RE.match(line.strip())
        if m_basis is not None:
            basis_line = m_basis.group(1)
            continue
        if m_conf is not None:
            confidence_line = m_conf.group(1)
            continue
        body_lines.append(line)

    errors: list[str] = []

    basis: list[str] = []
    if basis_line is None:
        errors.append("missing **Basis**: line")
    else:
        basis = _extract_wikilink_slugs(basis_line)
        if not basis:
            errors.append(f"**Basis**: line has no recoverable wikilink: {basis_line!r}")

    confidence: float | None = None
    if confidence_line is None:
        errors.append("missing **Confidence**: line")
    else:
        try:
            confidence = float(confidence_line.strip())
        except ValueError:
            errors.append(f"**Confidence**: value does not parse as float: {confidence_line!r}")
        else:
            if not (0.0 <= confidence <= 1.0):
                errors.append(f"**Confidence**: {confidence!r} out of range [0, 1]")

    body = "\n".join(body_lines).strip()

    return InferenceBlock(
        id=_make_block_id(raw_block),
        basis=basis,
        confidence=confidence,
        body=body,
        raw_block=raw_block.rstrip(),
        malformed=bool(errors),
        errors=errors,
    )


def parse_inference_blocks(text: str) -> list[InferenceBlock]:
    """Parse every ``## Inference`` block out of a wiki page body.

    ``text`` is the page body (frontmatter already stripped — see
    :func:`athenaeum.models.parse_frontmatter`), though passing the full
    file with frontmatter is harmless since frontmatter never starts a line
    with ``## ``.

    Blocks are delimited the same way ``answers._split_blocks`` /
    ``pending_merges`` split their sidecar blocks: a new block starts at
    every line beginning with ``## Inference`` (case-sensitive, matching
    the documented header exactly) and runs until the next ``## `` header
    of any kind or end of text. Other ``## `` sections (e.g. ``## Summary``)
    are not inference blocks and are skipped.

    Returns one :class:`InferenceBlock` per ``## Inference`` header found,
    in document order. A block that is missing its basis/confidence
    metadata or has an unparseable confidence is still returned, with
    ``malformed=True`` and ``errors`` populated — callers that want to flag
    malformed blocks (rather than just consume valid ones) can filter on
    that attribute.
    """
    lines = text.splitlines()
    blocks: list[InferenceBlock] = []
    current: list[str] | None = None

    for line in lines:
        if line.startswith("## "):
            if current is not None:
                blocks.append(_parse_one_block("\n".join(current)))
                current = None
            if line.strip() == "## Inference":
                current = [line]
            continue
        if current is not None:
            current.append(line)

    if current is not None:
        blocks.append(_parse_one_block("\n".join(current)))

    return blocks


__all__ = [
    "InferenceBlock",
    "parse_inference_blocks",
]
