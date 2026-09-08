# SPDX-License-Identifier: Apache-2.0
"""Read-only audit for :data:`~athenaeum.storage_migrate.INLINE_REDACTION_MARKER`
appearing on a page's H1 heading line (issue athenaeum#1461).

``storage_migrate._redact_inline_tokens`` is a whole-body ``str.replace`` with
no line handling, so a page whose H1 heading itself contained an inline
contact token (an email-titled page being the population that actually
matters) had that heading rewritten to a redaction notice. That is a real
defect on some pages and expected, harmless behaviour on others — see
:func:`classify_h1_marker` for the discriminating signal — so a flat "marker
present on an H1" scan is wrong on both ends: it under-reports nothing, but
it over-reports every heading whose marker merely replaced one inline token
inside an otherwise-intact title.

**The audit rule is deliberately NOT "marker on H1 AND frontmatter ``name:``
unredacted".** An unredacted ``name:`` is a deliberate durable-identifier
preservation (issue athenaeum#502) — :mod:`athenaeum.storage_migrate` never
rewrites the name field — so treating it as a defect signal would report
intended behaviour as a bug on every hit. This module records whether the
name field is unredacted purely as INFORMATIONAL context on the finding; see
:attr:`H1MarkerFinding.name_field_unredacted`. It never feeds
:func:`classify_h1_marker`.

The discriminating signal instead reuses the "residual" idea
:func:`athenaeum.storage_migrate._migrate_str_value` already applies to a
frontmatter scalar: strip the marker out of the heading text and look at
what is left.

* Nothing but punctuation/whitespace survives -> the marker replaced the
  ENTIRE heading subject (class :data:`MARKER_LEADING` — heading-subject-
  consumed; the real defect population).
* Any word character survives -> the heading still reads as a real title
  with one inline token substituted (class :data:`MARKER_MID_HEADING` — the
  marker doing its documented job; not a defect).

**Read-only.** :func:`find_h1_marker_pages` only ``read_text``s pages under
*wiki_root*; nothing in this module opens a page for writing. Repairing an
already-affected page is out of scope for this issue (filed against the
deployment's own tracker, not this engine repo) — this module reports, it
never rewrites.

Layering: L4 domain/pipeline module, alongside :mod:`athenaeum.storage_migrate`
(whose :data:`~athenaeum.storage_migrate.INLINE_REDACTION_MARKER` this module
reuses rather than redefining) and :mod:`athenaeum.pii_restore` (the sibling
audit this one is modeled on — see its module docstring's marker-discovery
section). May import L3 services (:mod:`athenaeum.pii`) and L1
(:mod:`athenaeum.models`) freely. The CLI layer
(:mod:`athenaeum._cmd_storage`, L5) owns argument parsing and report
rendering.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from athenaeum.models import parse_frontmatter
from athenaeum.pii import name_field_pii_values
from athenaeum.storage_migrate import INLINE_REDACTION_MARKER

#: Re-exported from :mod:`athenaeum.storage_migrate` (the module that writes
#: it) so this file has no marker text of its own to drift out of sync.
MARKER = INLINE_REDACTION_MARKER

#: Class (a) — the marker consumed the entire heading subject; only residual
#: punctuation/whitespace survives. This is the real defect population.
MARKER_LEADING = "marker_leading"

#: Class (b) — the marker replaced one inline token inside a heading that
#: still reads as a real title. Not a defect; the marker did its documented
#: job.
MARKER_MID_HEADING = "marker_mid_heading"

_H1_RE = re.compile(r"^#[ \t]+(.*?)[ \t]*$")
_WORD_CHAR_RE = re.compile(r"\w", re.UNICODE)


@dataclass(frozen=True)
class H1MarkerFinding:
    """One page whose H1 heading line carries :data:`MARKER`."""

    #: Page path, relative to *wiki_root*'s parent (matching
    #: :class:`athenaeum.pii_restore.MarkerHit`'s convention).
    page_relpath: str
    #: The full H1 line as it appears in the page (``"# ..."``).
    heading_line: str
    #: The heading text with the leading ``"# "`` stripped, marker still in.
    heading_text: str
    #: *heading_text* with every :data:`MARKER` occurrence removed and the
    #: result stripped — what :func:`classify_h1_marker` classifies on.
    residual: str
    #: :data:`MARKER_LEADING` or :data:`MARKER_MID_HEADING`.
    classification: str
    #: True when this page's ``name:``/``preferred_name:`` is itself
    #: email/phone-shaped (issue athenaeum#502's deliberate carve-out).
    #: INFORMATIONAL ONLY — never an input to *classification* (see module
    #: docstring: this must never be read as a defect signal).
    name_field_unredacted: bool


def classify_h1_marker(heading_text: str) -> str:
    """Classify one H1 heading's :data:`MARKER` occurrence.

    Reuses the "residual" idea :func:`athenaeum.storage_migrate._migrate_str_value`
    already applies to a frontmatter scalar: strip the marker out of
    *heading_text* and look at what survives. No word character surviving
    means the marker consumed the entire heading subject
    (:data:`MARKER_LEADING`); any word character surviving means the heading
    still reads as a real title (:data:`MARKER_MID_HEADING`).
    """
    residual = heading_text.replace(MARKER, "").strip()
    if not residual or not _WORD_CHAR_RE.search(residual):
        return MARKER_LEADING
    return MARKER_MID_HEADING


def _first_h1_line(body: str) -> str | None:
    """The first markdown H1 (``"# ..."``) line in *body*, or ``None``."""
    for line in body.split("\n"):
        if _H1_RE.match(line):
            return line
    return None


def find_h1_marker_pages(
    wiki_root: Path,
    *,
    limit: int | None = None,
) -> list[H1MarkerFinding]:
    """Every page under *wiki_root* whose H1 line carries :data:`MARKER`.

    Read-only: only ``Path.read_text`` is called anywhere in this function.
    Sorted by page path for deterministic output. A page under an
    ``excluded`` surface path component is skipped — it is an archival
    contact record, not a corpus-visible page a heading defect could be
    reported against.
    """
    findings: list[H1MarkerFinding] = []
    if not wiki_root.is_dir():
        return findings
    for page in sorted(wiki_root.rglob("*.md")):
        if "excluded" in page.resolve().parts:
            continue
        text = page.read_text(encoding="utf-8", errors="replace")
        if MARKER not in text:
            continue
        meta, body = parse_frontmatter(text)
        if not isinstance(meta, dict):
            meta = {}
        h1_line = _first_h1_line(body)
        if h1_line is None or MARKER not in h1_line:
            continue
        match = _H1_RE.match(h1_line)
        assert match is not None  # guaranteed by _first_h1_line's own filter
        heading_text = match.group(1)
        residual = heading_text.replace(MARKER, "").strip()
        findings.append(
            H1MarkerFinding(
                page_relpath=str(page.relative_to(wiki_root.parent)),
                heading_line=h1_line,
                heading_text=heading_text,
                residual=residual,
                classification=classify_h1_marker(heading_text),
                name_field_unredacted=bool(name_field_pii_values(meta)),
            )
        )
        if limit is not None and len(findings) >= limit:
            break
    return findings


def render_report(findings: list[H1MarkerFinding]) -> str:
    """Human-readable report, findings grouped by classification.

    Class (a) (:data:`MARKER_LEADING`) is printed first as the defect
    population; class (b) (:data:`MARKER_MID_HEADING`) is printed second,
    explicitly labeled as NOT a defect.
    """
    leading = [f for f in findings if f.classification == MARKER_LEADING]
    mid = [f for f in findings if f.classification == MARKER_MID_HEADING]
    lines: list[str] = []
    lines.append(
        f"H1 heading-marker audit: {len(findings)} page(s) carry {MARKER!r} "
        "on their H1 line."
    )
    lines.append("")
    lines.append(
        f"[DEFECT] marker-leading / heading-subject-consumed: {len(leading)} page(s)"
    )
    for f in leading:
        name_note = " (name field also unredacted — not evidence of a defect)" if (
            f.name_field_unredacted
        ) else ""
        lines.append(f"  {f.page_relpath}: {f.heading_line!r}{name_note}")
    lines.append("")
    lines.append(
        f"[NOT A DEFECT] marker-mid-heading / title intact: {len(mid)} page(s)"
    )
    for f in mid:
        lines.append(f"  {f.page_relpath}: {f.heading_line!r}")
    return "\n".join(lines)
