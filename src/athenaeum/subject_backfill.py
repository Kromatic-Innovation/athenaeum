# SPDX-License-Identifier: Apache-2.0
"""Deterministic ``subject:`` coordinate derivation (issue athenaeum#1244 AC2/AC3).

athenaeum#1244 found Gate 1 of the five-verdict comparator (athenaeum#715) settles
ZERO of the 22,040 candidate wiki-page pairs it consults, because none of the
``valid-time`` / ``scope`` / ``subject`` separator dimensions carries any
coordinate on the live corpus (`dimensions.py`'s `_null_relation`: both sides
null on a dimension is always UNKNOWN, never a separator). This module builds
the ONE coordinate of the three that has a deterministic, zero-judgment
derivation for a whole-page claim: ``subject``.

**What this module deliberately does NOT do: write to the live corpus.**

`docs/memory-model.md` §6.1 defines ``subject`` as "the entity the claim is
about (``uid`` of a wiki entity...) — already produced by entity linking."
For a whole-page claim, the obvious reading is ``subject := this page's own
uid`` — the page IS the entity it describes. That reading is exercised here
as :func:`derive_subject_for_page` and is safe, mechanical, and 100%-coverage
for any page that already carries a ``uid``.

It is also, on inspection, the WRONG population to backfill it onto. Entity
linking's job is to say what a claim's content refers to; two independent
DUPLICATE wiki pages about the same real-world thing have, by construction,
different ``uid``s. Stamping ``subject := uid`` on every page therefore
guarantees that any future ratified-identity resolver (the mechanism
athenaeum#715 gates the ``subject`` dimension's DISJOINT exit on —
:func:`athenaeum.dimensions.compare_identity`'s ``ratified=True`` path, not
yet built) would see every duplicate PAIR as two different subjects and
Gate 1 would exit DISTINCT on every one of them — the exact "confidently
wrong DISJOINT" AC2 warns against, except durable and corpus-wide rather
than a single bad classification. It is safe ONLY because nothing in the
current codebase ever calls ``compare_identity(..., ratified=True)`` (see
that function's docstring, and ``wiki_dedupe.py``'s ``record_comparison``
call site, which passes no ``subject_ratified`` and defaults False) — a
correctness property of TODAY's code, not of the write. The write outlives
the code that currently makes it inert.

So: the derivation function ships, tested, and available for an operator to
invoke once the entity-linking semantics are resolved (does ``subject`` mean
"this page's own identity" for a whole-page claim, or "the canonical
referent this page is a duplicate candidate for" — a question this module's
own docstring cannot answer for the operator, and does not try to). Until
that is resolved, :func:`apply_subject_backfill` writing to the live wiki
store must be an explicit, informed operator action — not something this
issue's own build silently exercises. See the PR body / issue comment for
the recorded flag.

``claimed_scope`` and ``valid_from``/``valid_until`` have NO safe
deterministic derivation at all (see athenaeum#1244's design-proposal comment,
2026-09-03) and are entirely out of this module's scope.

Layering: L2 (domain logic over the wiki tree), mirroring
:mod:`athenaeum.memory_class_backfill` / :mod:`athenaeum.page_description`'s
shape and byte-level-idempotence discipline (a textual INSERTION into the
existing frontmatter block, never a ``parse_frontmatter`` ->
``render_frontmatter`` round trip, which would reflow unrelated keys).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from athenaeum.models import parse_frontmatter

log = logging.getLogger(__name__)

#: Same shape as ``models._FM_RE`` / ``memory_class_backfill._FRONTMATTER_RE``,
#: re-declared rather than imported so this module can insert a line INSIDE
#: the block via the match's own span, without needing parse_frontmatter's
#: collapsed ({}, text) return to distinguish "no delimiter" from
#: "delimiter present, YAML unparseable".
_FRONTMATTER_RE = re.compile(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n", re.DOTALL)


def discover_wiki_pages(wiki_root: Path) -> list[Path]:
    """Every top-level wiki page, sorted, ``_``-prefixed sidecars excluded.

    Deliberately NON-recursive (``wiki_root.glob("*.md")``, not ``rglob``) —
    this mirrors :meth:`athenaeum.models.EntityIndex._load` and
    :mod:`athenaeum.wiki_dedupe`'s own candidate walk, i.e. the actual
    comparator-eligible surface athenaeum#1244 is about. An ``rglob`` walk (as
    :mod:`athenaeum.memory_class_backfill` uses for its own, different,
    purpose) would additionally reach ``wiki/_quarantine/...`` and any other
    ``_``-prefixed SUBDIRECTORY whose per-path segments a filename-only
    ``startswith("_")`` check does not catch — population this backfill must
    not touch.
    """
    if not wiki_root.is_dir():
        return []
    return sorted(p for p in wiki_root.glob("*.md") if p.is_file() and not p.name.startswith("_"))


@dataclass(frozen=True)
class PageOutcome:
    """What the backfill decided for one file, and why.

    ``subject`` is the value that WOULD be (or was) written; ``None`` for
    every skip. ``reason`` is a closed vocabulary: ``derivable`` (the one
    assignment reason) and ``already-set`` / ``no-frontmatter`` /
    ``empty-frontmatter`` / ``unparseable-frontmatter`` / ``no-uid`` (skips).
    """

    path: Path
    subject: str | None
    reason: str

    @property
    def assigned(self) -> bool:
        return self.subject is not None


@dataclass
class SubjectBackfillReport:
    """Counts + per-page outcomes for one dry-run or apply pass."""

    scanned: int = 0
    outcomes: list[PageOutcome] = field(default_factory=list)

    def record(self, outcome: PageOutcome) -> None:
        self.outcomes.append(outcome)

    @property
    def assignments(self) -> list[PageOutcome]:
        return [o for o in self.outcomes if o.assigned]

    def counts_by_reason(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for outcome in self.outcomes:
            counts[outcome.reason] = counts.get(outcome.reason, 0) + 1
        return dict(sorted(counts.items()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "assignable": len(self.assignments),
            "counts_by_reason": self.counts_by_reason(),
        }


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:  # pragma: no cover - defensive
        log.warning("subject backfill: unreadable page %s: %s", path, exc)
        return None


def derive_subject_for_page(meta: dict[str, Any]) -> str | None:
    """The deterministic candidate ``subject`` value for one page's frontmatter.

    ``subject := uid`` — the page's own identifier, restated in the field the
    ``subject`` kernel dimension reads (:func:`athenaeum.dimensions.coordinate_value`).
    Returns ``None`` when *meta* carries no non-empty ``uid`` — this function
    never invents one.
    """
    uid = meta.get("uid")
    if isinstance(uid, str) and uid.strip():
        return uid.strip()
    if uid is not None and not isinstance(uid, (dict, list)):
        text = str(uid).strip()
        if text:
            return text
    return None


def _decide_page(text: str) -> PageOutcome | tuple[None, dict[str, Any]]:
    """Decide one page's outcome, or hand back ``(None, meta)`` when derivable.

    Mirrors ``memory_class_backfill._classify_page``'s two-return shape: a
    concrete :class:`PageOutcome` is final; ``(None, meta)`` means the caller
    should compute :func:`derive_subject_for_page` on *meta* itself (kept
    separate so callers building a report don't need a dummy path arg here).
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return PageOutcome(Path(), None, "no-frontmatter")  # path patched by caller

    meta, _body = parse_frontmatter(text)
    if not meta:
        reason = "empty-frontmatter" if not match.group(1).strip() else "unparseable-frontmatter"
        return PageOutcome(Path(), None, reason)

    existing = meta.get("subject")
    if isinstance(existing, str) and existing.strip():
        return PageOutcome(Path(), None, "already-set")

    return None, meta


def build_subject_report(wiki_root: Path) -> SubjectBackfillReport:
    """Scan *wiki_root* and decide a ``subject`` candidate for every eligible page.

    Pure: reads files, writes nothing. This is the whole of ``--dry-run``.
    """
    report = SubjectBackfillReport()
    for path in discover_wiki_pages(wiki_root):
        text = _read(path)
        if text is None:
            continue
        report.scanned += 1
        decided = _decide_page(text)
        if isinstance(decided, PageOutcome):
            report.record(PageOutcome(path, None, decided.reason))
            continue
        _none, meta = decided
        candidate = derive_subject_for_page(meta)
        if candidate is None:
            report.record(PageOutcome(path, None, "no-uid"))
            continue
        report.record(PageOutcome(path, candidate, "derivable"))
    return report


def insert_subject(text: str, subject: str) -> str | None:
    """Return *text* with a ``subject:`` line appended to its frontmatter.

    Returns ``None`` when *text* has no frontmatter block. Textual insertion
    only — touches no other byte of the file, so a second run (or a run over
    a page this pass never meant to touch) is a byte-level no-op.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return None
    end = match.end(1)
    newline = "\r\n" if "\r\n" in text[: match.end()] else "\n"
    # Render through the YAML dumper rather than an f-string: a uid can be
    # (or coincidentally look like) an all-digit token, and a bare
    # ``subject: 123456`` would round-trip back as an int, breaking every
    # downstream str-typed reader (dimensions.coordinate_value included).
    # ``yaml.dump`` quotes exactly when needed and nothing more.
    line = yaml.dump({"subject": subject}, default_flow_style=False, allow_unicode=True).rstrip(
        "\n"
    )
    return f"{text[:end]}{newline}{line}{text[end:]}"


def apply_subject_backfill(report: SubjectBackfillReport) -> int:
    """Write every assignment in *report*. Returns the number of files changed.

    Re-checks each file's frontmatter at write time rather than trusting the
    scan (the report may be stale). **Not called by this issue's own build
    against the live corpus** — see the module docstring's entity-linking
    hazard. Provided so an operator who has resolved that question can run
    it deliberately, and so the write path itself is under test.
    """
    from athenaeum.atomic_io import atomic_write_text

    changed = 0
    for outcome in report.assignments:
        text = _read(outcome.path)
        if text is None:
            continue
        meta, _body = parse_frontmatter(text)
        existing = meta.get("subject")
        if isinstance(existing, str) and existing.strip():
            continue
        updated = insert_subject(text, outcome.subject or "")
        if updated is None or updated == text:
            continue
        atomic_write_text(outcome.path, updated)
        changed += 1
    return changed


__all__ = [
    "PageOutcome",
    "SubjectBackfillReport",
    "apply_subject_backfill",
    "build_subject_report",
    "derive_subject_for_page",
    "discover_wiki_pages",
    "insert_subject",
]
