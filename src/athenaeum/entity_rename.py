# SPDX-License-Identifier: Apache-2.0
"""The page rename primitive (issue athenaeum#2488).

Before this module, the only way to fix a wrong or too-generic `name:` on a
wiki page was to hand-edit its frontmatter outside the librarian, which (a)
bypasses provenance — nothing records who renamed it, when, or why — (b)
risks a later compile re-deriving the old name, and (c) leaves inbound
`[[wikilinks]]`, the search index and any registry entries pointing at the
old name. This module is the sanctioned primitive that does all three
correctly: it sets `name:`, appends the previous name to `aliases:`
(deduplicated), optionally rewrites the file slug + inbound wikilinks
(`reslug=True`), and appends a durable provenance record.

**Reuse, not reimplementation.** The alias-union and inbound-wikilink-rewrite
mechanics are IDENTICAL to what the fold ("fold-into-existing" merge write
path, :mod:`athenaeum.pending_merges`) already does when it folds a source
page's slug into a canonical page — :func:`athenaeum.pending_merges.
_add_aliases_to_frontmatter` and :func:`athenaeum.pending_merges.
_rewrite_inbound_wikilinks` are imported and called directly (the same
cross-module private-helper reuse :mod:`athenaeum.storage_migrate` already
does for its own name-is-an-email rename path — see that module's
``apply_name_email_rename`` for the precedent this mirrors).

**Why a rename is safe from a later compile reverting it (the issue's
sharpest acceptance criterion).** Every compile-time write path that can
touch an EXISTING page's frontmatter either (a) reads `existing_meta` off
disk and only adds/overrides OTHER keys (:func:`athenaeum.librarian.
tier0_handle_upsert`'s ``merged_meta = dict(existing_meta)``, :mod:`athenaeum.
tiers`'s tier-3 merge-into-existing path, both of which never assign
``meta["name"]`` at all), or (b) explicitly keeps the page's own `name:`
when a raw's differs (:mod:`athenaeum.structured_upsert`'s identity-key
guard, ``_IDENTITY_KEYS = STRUCTURED_UPSERT_PROTECTED_KEYS | {"name"}``).
Nothing in the compile pipeline re-derives `name:` from source text on an
existing page. A renamed page's `name:` therefore already survives a later
compile by construction; this module adds no new protection for that beyond
not itself being party to the bug — ``tests/test_entity_rename.py``'s
compile-does-not-revert case exercises :func:`athenaeum.librarian.
tier0_handle_upsert` (a real, deterministic, LLM-free compile path) against
a renamed page as the regression guard for this invariant.

Layering: L4 domain/pipeline (same tier as :mod:`athenaeum.pending_merges`,
:mod:`athenaeum.retire_pages`, :mod:`athenaeum.storage_migrate` — all of
which this module is a sibling of). Imports :mod:`athenaeum.models` (L1),
:mod:`athenaeum.atomic_io` (L0), :mod:`athenaeum.store` (L1),
:mod:`athenaeum.entity_index_cache` (L2), and :mod:`athenaeum.pending_merges`
(L4, same-layer — same-layer imports are allowed; see
``tests/test_layer_boundary.py`` and :mod:`athenaeum.retire_pages`'s own
docstring for the precedent).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from athenaeum.atomic_io import atomic_write_text
from athenaeum.entity_index_cache import locate_entity_page
from athenaeum.models import parse_frontmatter, render_frontmatter, slugify
from athenaeum.store import append_line_durable, now_iso

log = logging.getLogger(__name__)

#: Default provenance ledger filename, sibling to
#: :data:`athenaeum.provenance.MERGE_PROVENANCE_FILENAME` but its own file —
#: a rename is not a merge and carries a different record shape (no
#: ``write_kind``/``source_paths`` fold semantics; it has ``old_name``/
#: ``new_name``/``by``/``reason`` instead).
RENAME_PROVENANCE_FILENAME = "_rename_provenance.jsonl"


@dataclass
class RenamePreview:
    """The computed effect of a rename, before any write happens.

    ``error`` non-``None`` means the rename cannot proceed at all (unknown
    uid, empty ``--name``, unreadable/unparseable page, or a ``--reslug``
    target that collides with a different existing page) — the caller
    should report it and stop. ``noop`` means the page's `name:` is ALREADY
    *new_name* — the rename is idempotent: nothing is written, no alias is
    added twice, no provenance record is appended on a repeat call with the
    same ``--name``.
    """

    uid: str
    page_path: Path
    old_name: str
    new_name: str
    noop: bool = False
    error: str | None = None
    reslug: bool = False
    old_slug: str = ""
    new_slug: str = ""
    new_page_path: Path | None = None
    aliases_to_add: list[str] = field(default_factory=list)
    rewritten_text: str = ""


@dataclass
class RenameApplyResult:
    """What actually happened when a :class:`RenamePreview` was applied."""

    uid: str
    old_name: str
    new_name: str
    page_path: Path
    aliases_added: list[str] = field(default_factory=list)
    reslugged: bool = False
    links_rewritten: int = 0
    link_details: list[dict[str, str]] = field(default_factory=list)
    provenance_written: bool = False
    error: str | None = None


def resolve_rename_target(
    wiki_root: Path, uid: str, *, cache_dir: Path | None = None
) -> Path | None:
    """Resolve *uid* to its entity page, or ``None`` (unknown uid).

    Thin wrapper over :func:`athenaeum.entity_index_cache.locate_entity_page`
    — the one shared single-uid resolver every other reader (``entity``,
    ``pii.read_entity``, the MCP ``entity_read`` tool) already uses, rather
    than a second uid->path lookup.
    """
    return locate_entity_page(wiki_root, uid, cache_dir=cache_dir)


def default_rename_provenance_path(wiki_root: Path) -> Path:
    """Default provenance ledger path: ``<wiki_root>/_rename_provenance.jsonl``."""
    return Path(wiki_root) / RENAME_PROVENANCE_FILENAME


def build_rename_preview(
    wiki_root: Path,
    uid: str,
    new_name: str,
    *,
    reslug: bool = False,
    cache_dir: Path | None = None,
) -> RenamePreview:
    """Compute what a rename of *uid* to *new_name* would do — pure, no writes.

    Idempotence (AC: "rename idempotence"): when the page's current `name:`
    already equals *new_name* (after stripping), this returns
    ``noop=True`` and nothing else is computed — a second identical rename
    call is always a clean no-op, whether or not the first call actually
    applied.
    """
    new_name = (new_name or "").strip()
    page_path = resolve_rename_target(wiki_root, uid, cache_dir=cache_dir)
    if page_path is None:
        return RenamePreview(
            uid=uid,
            page_path=Path(),
            old_name="",
            new_name=new_name,
            error=f"no page found for uid {uid!r}",
        )
    if not new_name:
        return RenamePreview(
            uid=uid,
            page_path=page_path,
            old_name="",
            new_name=new_name,
            error="--name must be non-empty",
        )

    try:
        text = page_path.read_text(encoding="utf-8")
    except OSError as exc:
        return RenamePreview(
            uid=uid,
            page_path=page_path,
            old_name="",
            new_name=new_name,
            error=f"could not read {page_path}: {exc}",
        )
    meta, body = parse_frontmatter(text)
    if not isinstance(meta, dict) or not meta:
        return RenamePreview(
            uid=uid,
            page_path=page_path,
            old_name="",
            new_name=new_name,
            error=f"{page_path.name} has no parseable frontmatter",
        )

    old_name = str(meta.get("name", "") or "").strip()

    if old_name == new_name:
        return RenamePreview(
            uid=uid,
            page_path=page_path,
            old_name=old_name,
            new_name=new_name,
            noop=True,
        )

    from athenaeum.pending_merges import _add_aliases_to_frontmatter

    new_meta = dict(meta)
    new_meta["name"] = new_name
    aliases_to_add = [old_name] if old_name else []
    new_meta = _add_aliases_to_frontmatter(new_meta, aliases_to_add)

    old_slug = slugify(page_path.stem)
    new_slug = old_slug
    new_page_path = page_path
    if reslug:
        candidate_slug = slugify(new_name)
        if candidate_slug and candidate_slug != old_slug:
            new_slug = candidate_slug
            candidate_path = page_path.with_name(f"{new_slug}.md")
            if candidate_path.exists() and candidate_path != page_path:
                return RenamePreview(
                    uid=uid,
                    page_path=page_path,
                    old_name=old_name,
                    new_name=new_name,
                    error=(
                        f"--reslug target {candidate_path.name!r} already "
                        "exists as a different page — refusing to overwrite it"
                    ),
                )
            new_page_path = candidate_path

    rewritten_text = render_frontmatter(new_meta) + body

    return RenamePreview(
        uid=uid,
        page_path=page_path,
        old_name=old_name,
        new_name=new_name,
        noop=False,
        reslug=reslug,
        old_slug=old_slug,
        new_slug=new_slug,
        new_page_path=new_page_path,
        aliases_to_add=aliases_to_add,
        rewritten_text=rewritten_text,
    )


def apply_rename(
    wiki_root: Path,
    preview: RenamePreview,
    *,
    by: str,
    reason: str = "",
    provenance_path: Path | None = None,
) -> RenameApplyResult:
    """Write *preview*'s plan: frontmatter, optional reslug + wikilink rewrite, provenance.

    Raises ``ValueError`` if *preview* carries an error or is a no-op —
    callers must check :attr:`RenamePreview.error` / :attr:`RenamePreview.noop`
    first (mirrors :func:`athenaeum.storage_migrate.apply_name_email_rename`'s
    "callers must check confidence first" contract).

    Order mirrors the fold write path's crash-safety discipline: the page
    content lands first (at its final path), the old path is removed only
    if reslugging actually moved it, and inbound-wikilink rewriting (when
    reslugging) happens last, after the renamed page itself is stable and
    can be used as the rewrite's ``skip`` target.
    """
    if preview.error is not None:
        raise ValueError(f"cannot apply a rename preview with an error: {preview.error}")
    if preview.noop:
        raise ValueError("cannot apply a no-op rename preview (name unchanged)")

    new_page_path = preview.new_page_path or preview.page_path
    atomic_write_text(new_page_path, preview.rewritten_text)
    if new_page_path != preview.page_path and preview.page_path.is_file():
        preview.page_path.unlink()

    links_rewritten = 0
    link_details: list[dict[str, str]] = []
    if preview.reslug and preview.new_slug != preview.old_slug:
        from athenaeum.pending_merges import _rewrite_inbound_wikilinks

        links_rewritten = _rewrite_inbound_wikilinks(
            wiki_root,
            [preview.old_slug],
            preview.new_slug,
            skip=new_page_path,
            link_details=link_details,
        )

    provenance_written = record_rename_provenance(
        wiki_root,
        uid=preview.uid,
        old_name=preview.old_name,
        new_name=preview.new_name,
        by=by,
        reason=reason,
        old_slug=preview.old_slug if preview.reslug else None,
        new_slug=preview.new_slug if preview.reslug else None,
        links_rewritten=link_details or None,
        provenance_path=provenance_path,
    )

    return RenameApplyResult(
        uid=preview.uid,
        old_name=preview.old_name,
        new_name=preview.new_name,
        page_path=new_page_path,
        aliases_added=list(preview.aliases_to_add),
        reslugged=preview.reslug and preview.new_slug != preview.old_slug,
        links_rewritten=links_rewritten,
        link_details=link_details,
        provenance_written=provenance_written,
    )


def record_rename_provenance(
    wiki_root: Path,
    *,
    uid: str,
    old_name: str,
    new_name: str,
    by: str,
    reason: str = "",
    old_slug: str | None = None,
    new_slug: str | None = None,
    links_rewritten: list[dict[str, str]] | None = None,
    provenance_path: Path | None = None,
    ts: datetime | None = None,
) -> bool:
    """Append one rename-provenance record (who, when, reason, old -> new). Best-effort.

    Mirrors :func:`athenaeum.provenance.record_merge_provenance`'s
    discipline exactly: never raises — a provenance write must not block a
    rename whose file-level side effects have already happened by the time
    this runs. Failures are logged and swallowed. Returns ``True`` when a
    record was written.
    """
    try:
        record: dict[str, Any] = {
            "uid": uid,
            "old_name": old_name,
            "new_name": new_name,
            "by": by,
            "reason": reason,
            "at": now_iso(ts),
        }
        if old_slug is not None:
            record["old_slug"] = old_slug
        if new_slug is not None:
            record["new_slug"] = new_slug
        if links_rewritten:
            record["links_rewritten"] = links_rewritten
        target = (
            provenance_path
            if provenance_path is not None
            else default_rename_provenance_path(wiki_root)
        )
        line = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
        append_line_durable(target, line)
        return True
    except Exception as exc:  # noqa: BLE001 — ledger write must never break a rename
        log.debug("rename provenance ledger write skipped (%s): %s", type(exc).__name__, exc)
        return False


def read_rename_provenance(
    wiki_root: Path, *, provenance_path: Path | None = None
) -> list[dict[str, Any]]:
    """Read every rename-provenance record, oldest first. ``[]`` if none exist."""
    target = (
        provenance_path
        if provenance_path is not None
        else default_rename_provenance_path(wiki_root)
    )
    if not target.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records
