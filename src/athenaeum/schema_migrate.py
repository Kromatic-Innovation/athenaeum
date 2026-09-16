# SPDX-License-Identifier: Apache-2.0
"""``athenaeum schema migrate`` business logic — issue athenaeum#1628 Plan item 4.

Applies every PENDING ``derivation="rule"`` / ``timing="eager"`` migration
from :mod:`athenaeum.schema_migrations`'s registry, corpus-wide, dry-run by
default. Mirrors :mod:`athenaeum.memory_class_backfill`'s three invariants
for this same shape of sweep:

1. **Never touches a MODEL migration.** Those advance only through the
   audit pass (:mod:`athenaeum.audit`). A page's eager chain, per
   :func:`_plan_page`, walks its :func:`~athenaeum.schema_migrations.pending_migrations`
   in version order and stops the INSTANT it reaches a non-eager-rule
   entry — a page is never advanced past a model migration it has not yet
   been resolved for by an audit, even when a LATER eager migration exists
   further down the registry.
2. **Never overwrite, never fabricate frontmatter.** A page with no YAML
   frontmatter block at all is skipped and counted, never given a
   synthetic one. A field an eager migration's ``apply`` produces is never
   written over an already-populated value on the page.
3. **Byte-level idempotence.** The write is a textual INSERTION of
   ``schema_version: <n>`` (plus any field an eager migration actually
   produced) at the end of the existing frontmatter block — not a
   ``parse_frontmatter`` -> ``render_frontmatter`` round trip, which would
   reflow key order/quoting on unrelated keys. A page with nothing left to
   migrate reports ``next-pending-is-model``/``already-current`` and is
   never opened for writing at all, so a second run makes zero further
   byte changes anywhere.

Layering: L2 (domain logic over the wiki tree). Imports
:mod:`athenaeum.schema_migrations` (L0) and :mod:`athenaeum.models` (L1);
imported by ``_cmd_schema.py`` (L5). Holds no argparse and prints nothing —
the CLI module owns presentation, matching ``memory_class_backfill.py``'s
own factoring rule.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from athenaeum import schema_migrations
from athenaeum.models import parse_frontmatter

log = logging.getLogger(__name__)

#: Same shape as ``models._FM_RE`` but re-declared rather than imported —
#: this module needs the MATCH SPAN (to insert lines inside the block
#: without re-rendering it), which ``parse_frontmatter`` does not return.
#: Mirrors ``memory_class_backfill.py``'s own re-declaration for the same
#: reason (see that module's docstring).
_FRONTMATTER_RE = re.compile(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n", re.DOTALL)


def _is_populated(value: Any) -> bool:
    """Same "already set" test :mod:`athenaeum.audit` uses — any non-``None``,
    non-blank value counts, so an eager migration's produced value never
    clobbers something already on the page."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


@dataclass(frozen=True)
class PageOutcome:
    """What the eager migrate pass decided for one file, and why.

    ``to_version``/``fields`` are ``None``/``{}`` for every skip.
    ``reason`` is the report's grouping key: ``migrated`` (would-be/actual
    write) or one of ``already-current`` / ``next-pending-is-model`` /
    ``no-frontmatter`` / ``empty-frontmatter`` / ``unparseable-frontmatter``.
    """

    path: Path
    to_version: int | None
    fields: dict[str, Any]
    reason: str

    @property
    def migrated(self) -> bool:
        return self.to_version is not None


@dataclass
class MigrateReport:
    """Counts + per-page outcomes for one eager-migrate pass.

    Holds every outcome, not just totals, matching
    ``memory_class_backfill.BackfillReport``'s own rationale: ``--dry-run``
    (the default) is the review surface, so an operator must be able to see
    which pages a count refers to.
    """

    scanned: int = 0
    outcomes: list[PageOutcome] = field(default_factory=list)

    def record(self, outcome: PageOutcome) -> None:
        self.outcomes.append(outcome)

    @property
    def migrations(self) -> list[PageOutcome]:
        return [o for o in self.outcomes if o.migrated]

    def counts_by_reason(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for outcome in self.outcomes:
            counts[outcome.reason] = counts.get(outcome.reason, 0) + 1
        return dict(sorted(counts.items()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "migrated": len(self.migrations),
            "counts_by_reason": self.counts_by_reason(),
        }


def discover_wiki_pages(wiki_root: Path) -> list[Path]:
    """Every ``.md`` page under *wiki_root*, sorted, infra ledgers excluded.

    Matches ``memory_class_backfill.discover_wiki_pages`` /
    ``audit.discover_wiki_pages`` — each domain sweep module re-declares its
    own copy rather than sharing one (see either module's docstring for
    that convention).
    """
    return sorted(
        p for p in wiki_root.rglob("*.md") if p.is_file() and not p.name.startswith("_")
    )


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:  # pragma: no cover - defensive
        log.warning("schema migrate: unreadable page %s: %s", path, exc)
        return None


def _plan_page(meta: dict[str, Any]) -> tuple[int | None, dict[str, Any]]:
    """Decide the eager rule-based outcome for one page's already-parsed
    *meta*. Returns ``(to_version, fields)`` — ``to_version is None`` means
    nothing eager is applicable right now: either the page is already past
    every declared migration, or the very next pending migration is
    model-derivation (out of this command's reach — see the module
    docstring's invariant 1).
    """
    pending = schema_migrations.pending_migrations(meta)
    if not pending:
        return None, {}

    working = dict(meta)
    produced_fields: dict[str, Any] = {}
    reached: int | None = None
    for migration in pending:
        if migration.derivation != "rule" or migration.timing != "eager":
            break
        assert migration.apply is not None  # enforced by validate_migrations at import time
        for name, value in migration.apply(working).items():
            if _is_populated(working.get(name)):
                continue
            working[name] = value
            produced_fields[name] = value
        reached = migration.to_version

    return reached, produced_fields


def build_migrate_report(wiki_root: Path) -> MigrateReport:
    """Scan *wiki_root* and decide the eager-migration outcome for every page.

    Pure with respect to the tree — reads files, makes zero writes. Nothing
    is written here; :func:`apply_migrations` is the only writer, so
    ``--dry-run``/the default is this function called alone.
    """
    report = MigrateReport()
    for path in discover_wiki_pages(wiki_root):
        text = _read(path)
        if text is None:
            continue
        report.scanned += 1

        match = _FRONTMATTER_RE.match(text)
        if match is None:
            report.record(PageOutcome(path, None, {}, "no-frontmatter"))
            continue

        meta, _body = parse_frontmatter(text)
        if not meta:
            reason = (
                "empty-frontmatter" if not match.group(1).strip() else "unparseable-frontmatter"
            )
            report.record(PageOutcome(path, None, {}, reason))
            continue

        to_version, produced_fields = _plan_page(meta)
        if to_version is None:
            pending = schema_migrations.pending_migrations(meta)
            reason = "already-current" if not pending else "next-pending-is-model"
            report.record(PageOutcome(path, None, {}, reason))
            continue

        report.record(PageOutcome(path, to_version, produced_fields, "migrated"))
    return report


def insert_schema_fields(text: str, to_version: int, fields: dict[str, Any]) -> str | None:
    """Return *text* with ``schema_version:`` (and any eager-produced field)
    lines appended to its frontmatter block.

    Returns ``None`` when *text* has no frontmatter block — the caller must
    skip, never synthesize one. The insertion is textual and touches no
    other byte of the file, matching ``memory_class_backfill.insert_memory_class``'s
    same discipline (issue athenaeum#1628 Plan item 4: "byte-level frontmatter
    insertion").
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return None
    end = match.end(1)
    newline = "\r\n" if "\r\n" in text[: match.end()] else "\n"
    lines = [f"schema_version: {to_version}"]
    for name, value in fields.items():
        lines.append(f"{name}: {value}")
    return f"{text[:end]}{newline}{newline.join(lines)}{text[end:]}"


def apply_migrations(report: MigrateReport) -> int:
    """Write every migration outcome in *report*. Returns files-changed count.

    Re-reads each page and re-checks its CURRENT ``schema_version`` at write
    time rather than trusting the scan — a page independently advanced past
    ``outcome.to_version`` between scan and apply (e.g. by a concurrent
    audit) is skipped, never re-stamped or double-inserted, mirroring
    ``audit.apply_audit_report``'s same re-check-before-write discipline.
    """
    from athenaeum.atomic_io import atomic_write_text

    changed = 0
    for outcome in report.migrations:
        assert outcome.to_version is not None  # report.migrations already filters this
        text = _read(outcome.path)
        if text is None:
            continue
        meta, _body = parse_frontmatter(text)
        if schema_migrations.page_schema_version(meta) >= outcome.to_version:
            continue
        updated = insert_schema_fields(text, outcome.to_version, outcome.fields)
        if updated is None or updated == text:
            continue
        atomic_write_text(outcome.path, updated)
        changed += 1
    return changed


__all__ = [
    "MigrateReport",
    "PageOutcome",
    "apply_migrations",
    "build_migrate_report",
    "discover_wiki_pages",
    "insert_schema_fields",
]
