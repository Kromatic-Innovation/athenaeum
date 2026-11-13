# SPDX-License-Identifier: Apache-2.0
"""``athenaeum repair --fold-targets``: rewrite a dangling bare-slug
``folded_into`` (issue athenaeum#2545).

A fold used to stamp a tombstone's ``folded_into`` with the bare slug of its
canonical's ``name:`` frontmatter (:func:`athenaeum.pending_merges`'s own
``target_slug``), which names no file when the canonical lives at the
corpus's real ``<uid>-<slug>.md`` shape. Issue athenaeum#2545 fixed the fold
write path to stamp the canonical's resolved filename STEM instead, but a
tombstone already on disk from before that fix still carries the old,
dangling bare-slug value — this module is the one-shot repair for those.

Dry-run by default, same contract as ``athenaeum repair``'s other modes
(``--bounce-fold``, ``--contact-records``): pass ``apply=True`` /
``--apply`` to write. A dangling value whose bare slug resolves to more than
one (or zero) live page is left completely untouched and reported
separately — this module never guesses at an ambiguous rewrite.

Issue athenaeum#2545 (review follow-up): a rewritten tombstone's
``folded_into`` must stay in lockstep with the merge-provenance ledger's
``canonical_slug`` for the SAME reason the fold write path itself does
(see ``pending_merges.resolve_merge``'s own ``canonical_slug=
target_path.stem`` alignment) — :func:`athenaeum.unfold.find_fold_record`
looks the ledger up by an EXACT match on that value, so patching only the
tombstone would silently re-break the direct-unfold path this repair is
meant to restore. :func:`_rewrite_ledger_canonical_slugs` below rewrites
every ledger record sharing the OLD bare slug to the NEW stem — safe
because that bare slug was always derived identically
(``slugify(merge_target_name)``) for every fold into the one canonical
identity it names, so a shared value never spans two different
canonicals. This is a deliberate, narrow exception to the ledger's
otherwise append-only discipline (:mod:`athenaeum.provenance`'s own
``read_merge_provenance`` already documents tolerating a hand-edit for the
same reason this repair tool exists at all: fixing a value a fold wrote
wrong before issue athenaeum#2545).

Layering: L4 domain/pipeline. Imports :mod:`athenaeum.models` (L1),
:mod:`athenaeum.atomic_io` (L0), :mod:`athenaeum.provenance` (L1), and
:mod:`athenaeum.pending_merges` (L4, for
:func:`athenaeum.pending_merges.find_identity_pages` — the SAME identity
resolver a fold's approve path uses to pick its target, so this repair can
never resolve a slug to a different page than a real fold would have) at
module scope.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from athenaeum.atomic_io import atomic_write_text
from athenaeum.models import (
    FOLDED_INTO_FIELD,
    is_tombstone,
    parse_frontmatter,
    render_frontmatter,
    tombstone_target,
)
from athenaeum.pending_merges import find_identity_pages
from athenaeum.provenance import default_merge_provenance_path


@dataclass
class FoldTargetRepairReport:
    """Result of one :func:`repair_dangling_fold_targets` pass."""

    pages_scanned: int = 0
    #: Tombstones whose ``folded_into`` names no file directly.
    dangling: int = 0
    #: Rewrites made (``apply=True``) or that WOULD be made (dry-run).
    changes: list[tuple[Path, str, str]] = field(default_factory=list)
    #: Dangling values left untouched: (tombstone name, folded_into value,
    #: live-candidate filenames — 0 or >1).
    ambiguous: list[tuple[str, str, list[str]]] = field(default_factory=list)
    errors: list[tuple[Path, str]] = field(default_factory=list)
    #: Merge-provenance ledger records whose ``canonical_slug`` was (or, on
    #: a dry run, would be) rewritten to match — see the module docstring.
    ledger_records_updated: int = 0

    @property
    def repaired(self) -> int:
        """How many of ``changes`` were actually written."""
        return len(self.changes)


def _rewrite_ledger_canonical_slugs(
    wiki_root: Path, rewrites: dict[str, str], *, apply: bool
) -> int:
    """Rewrite ``_merge_provenance.jsonl``'s ``canonical_slug`` for every
    record whose current value is a key of *rewrites*, to the matching
    value. Returns the number of records that were (``apply=True``) or
    would be (``apply=False``) changed. A blank, malformed, or non-dict
    JSON line is kept byte-for-byte — the same fail-open tolerance
    :func:`athenaeum.provenance.read_merge_provenance` already extends to
    a torn trailing write or a hand-edit.
    """
    if not rewrites:
        return 0
    ledger_path = default_merge_provenance_path(wiki_root)
    if not ledger_path.is_file():
        return 0
    try:
        raw_text = ledger_path.read_text(encoding="utf-8")
    except OSError:
        return 0
    out_lines: list[str] = []
    changed = 0
    for line in raw_text.splitlines():
        stripped = line.strip()
        record: object = None
        if stripped:
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                record = None
        if isinstance(record, dict):
            old = record.get("canonical_slug")
            if isinstance(old, str) and old in rewrites:
                changed += 1
                if apply:
                    record["canonical_slug"] = rewrites[old]
                    out_lines.append(json.dumps(record))
                    continue
        out_lines.append(line)
    if apply and changed:
        trailing_newline = "\n" if raw_text.endswith("\n") else ""
        atomic_write_text(ledger_path, "\n".join(out_lines) + trailing_newline)
    return changed


def repair_dangling_fold_targets(
    wiki_root: Path, *, apply: bool = False
) -> FoldTargetRepairReport:
    """Scan every tombstone under *wiki_root* for a dangling ``folded_into``
    and rewrite it to the resolved live page's filename stem.

    A value is "dangling" when ``<value>.md`` does not exist directly —
    the shape every pre-athenaeum#2545 fold-into-existing produced whenever
    its canonical lived at ``<uid>-<slug>.md``. For each dangling value,
    :func:`athenaeum.pending_merges.find_identity_pages` is asked for every
    live page owning that slug (by name, bare-slug or uid-prefixed, exactly
    as a fold's approve path resolves a target): exactly one match is
    rewritten to that page's filename stem; zero or more than one is left
    untouched and reported on :attr:`FoldTargetRepairReport.ambiguous`.

    ``apply=False`` (the default) computes and reports every change without
    writing anything. ``apply=True`` writes each non-ambiguous rewrite via
    :func:`athenaeum.atomic_io.atomic_write_text`, preserving every other
    frontmatter key and the page body verbatim — the same "stamp copies,
    never replaces" discipline :func:`athenaeum.models.stamp_tombstone`
    itself follows. It also rewrites the merge-provenance ledger's matching
    ``canonical_slug`` entries to the same new value
    (:func:`_rewrite_ledger_canonical_slugs`), so a repaired tombstone's
    ``folded_into`` and the ledger record a later unfold looks it up by
    never fall back out of sync — the exact coupling issue athenaeum#2545's
    own fold-path fix had to preserve.
    """
    report = FoldTargetRepairReport()
    wiki_root = Path(wiki_root)
    if not wiki_root.is_dir():
        return report
    for path in sorted(wiki_root.glob("*.md")):
        if path.name.startswith("_"):
            continue
        report.pages_scanned += 1
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            report.errors.append((path, str(exc)))
            continue
        meta, body = parse_frontmatter(text)
        if not is_tombstone(meta):
            continue
        folded_into = tombstone_target(meta)
        if not folded_into:
            continue
        # Already resolvable directly (the post-athenaeum#2545 stem form, or
        # a bare slug that happens to BE a real filename) — nothing to do.
        if (wiki_root / f"{folded_into}.md").is_file():
            continue
        report.dangling += 1
        candidates = find_identity_pages(folded_into, wiki_root)
        if len(candidates) != 1:
            report.ambiguous.append(
                (path.name, folded_into, [c.name for c in candidates])
            )
            continue
        new_value = candidates[0].stem
        if new_value == folded_into:
            continue
        report.changes.append((path, folded_into, new_value))
        if apply:
            new_meta = dict(meta) if isinstance(meta, dict) else {}
            new_meta[FOLDED_INTO_FIELD] = new_value
            try:
                atomic_write_text(path, render_frontmatter(new_meta) + body)
            except OSError as exc:
                report.errors.append((path, str(exc)))
                report.changes.pop()

    rewrites = {old: new for _path, old, new in report.changes}
    try:
        report.ledger_records_updated = _rewrite_ledger_canonical_slugs(
            wiki_root, rewrites, apply=apply
        )
    except OSError as exc:
        report.errors.append((default_merge_provenance_path(wiki_root), str(exc)))
    return report
