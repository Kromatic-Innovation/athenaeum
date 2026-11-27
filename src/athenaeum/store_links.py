# SPDX-License-Identifier: Apache-2.0
"""Link direction across stores: write-boundary guard + lint (athenaeum#2619).

The operator's rule: a link from a page on a more widely shared surface to a
page on a more private one is refused; the other direction is allowed (see
:mod:`athenaeum.store_boundary` for the rank/predicate). This module finds a
page's links, resolves each target's surface through the multi-surface
:class:`~athenaeum.models.EntityIndex` / :class:`~athenaeum.models.PageLocator`
(athenaeum#2617), and applies the rule in two places:

* :func:`guard_entity_store_links` -- the page write boundary, called next to
  the type / field-constraint guards in :mod:`athenaeum.librarian`. A refused
  write is parked under ``<wiki_root>/_store_link_rejected/`` and ledgered in
  ``<wiki_root>/_store_link_rejected.jsonl``; the log line names both page uids
  and both surfaces.
* :func:`scan_store_links` -- the read-only lint behind
  ``athenaeum store-link-lint``: every EXISTING link that breaks the rule.

A "link" is any of the four shapes the corpus uses (the ones
``page_recompile_undo.find_inbound_links`` already scans): a
``related:`` row's ``uid``, the ``parent:`` key, a ``[[wikilink]]`` and a
markdown ``[label](page.md)`` link. A link whose target does not resolve to a
page is not judged. With no extra surface configured, or a source page on a
rank-0 surface, nothing can be forbidden and the work is skipped.

Layering: L2. Imports :mod:`athenaeum.store_boundary`, :mod:`athenaeum.models`,
:mod:`athenaeum.storage`, :mod:`athenaeum.store`; nothing at L3+.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from athenaeum.atomic_io import atomic_write_text
from athenaeum.config import load_config
from athenaeum.models import EntityIndex, PageLocator, parse_frontmatter
from athenaeum.storage import resolve_locator_surfaces, surface_visibility, vault_page_roots
from athenaeum.store import append_line_durable, now_iso
from athenaeum.store_boundary import (
    StoreBoundaryLinkError,
    link_forbidden,
    surface_name_of,
)

log = logging.getLogger(__name__)

#: The corpus' wikilink and markdown-inline-link grammar, restated exactly as
#: :mod:`athenaeum.page_recompile_undo` states it (the convention every reader
#: of this grammar follows; importing that L4 module would be an import cycle
#: with the write paths that call this guard).
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|\n]+?)(?:\|[^\[\]\n]*)?\]\]")
_MD_LINK_RE = re.compile(r"\]\(\s*<?([^)<>\s]+)>?\s*\)")

STORE_LINK_REJECTED_DIR_NAME = "_store_link_rejected"
STORE_LINK_REJECTED_LEDGER_NAME = "_store_link_rejected.jsonl"


@dataclass(frozen=True)
class LinkViolation:
    """One link from a page to a page on a more private surface."""

    source_uid: str
    source_page: str
    source_surface: str
    target_uid: str
    target_page: str
    target_surface: str
    kind: str
    ref: str

    def describe(self) -> str:
        return (
            f"{self.source_uid or self.source_page} [{self.source_surface}] -> "
            f"{self.target_uid or self.target_page} [{self.target_surface}] "
            f"({self.kind}: {self.ref})"
        )


def extract_link_refs(meta: Mapping[str, Any], body: str) -> list[tuple[str, str]]:
    """Every ``(kind, ref)`` link a page makes, in a stable order, deduped."""
    refs: list[tuple[str, str]] = []
    related = meta.get("related")
    for row in related if isinstance(related, list) else []:
        if isinstance(row, dict) and isinstance(row.get("uid"), str) and row["uid"]:
            refs.append(("related-row", row["uid"]))
    parent = meta.get("parent")
    if isinstance(parent, str) and parent:
        refs.append(("parent-key", parent))
    for m in _WIKILINK_RE.finditer(body):
        refs.append(("wikilink", m.group(1).strip()))
    for m in _MD_LINK_RE.finditer(body):
        target = m.group(1).strip()
        if not target or "://" in target or target.startswith("#"):
            continue
        target = target.split("#", 1)[0].split("?", 1)[0]
        if target:
            refs.append(("markdown-link", PurePosixPath(target).name))
    return list(dict.fromkeys(refs))


class _TargetResolver:
    """Resolve a link ref to the page file it names, across every surface."""

    def __init__(self, index: EntityIndex) -> None:
        self.index = index
        self._by_file: dict[str, Path] = {}
        for key, path in index.locator.iter_pages():
            for k in (PurePosixPath(key).name, PurePosixPath(key).stem):
                self._by_file.setdefault(k, path)  # wiki is iterated first

    def resolve(self, ref: str) -> Path | None:
        path = self.index.get_by_uid(ref)
        if path is not None:
            return path
        entry = self.index.lookup(ref)
        if entry is not None:
            return Path(entry.path)
        return self._by_file.get(ref) or self._by_file.get(PurePosixPath(ref).stem)


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:  # pragma: no cover - defensive
        return path


def _page_uid(path: Path) -> str:
    try:
        meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return ""
    return str(meta.get("uid") or "") if isinstance(meta, dict) else ""


def _violations_for(
    *,
    source_path: Path,
    meta: Mapping[str, Any],
    body: str,
    index: EntityIndex,
    resolver: _TargetResolver,
    config: dict[str, Any] | None,
    skip: set[tuple[str, str]] | None = None,
    surface_override: Mapping[Path, str] | None = None,
    involving: set[Path] | None = None,
) -> list[LinkViolation]:
    locator = index.locator
    override = surface_override or {}

    def surface_of(path: Path) -> str:
        return override.get(_resolved(path)) or surface_name_of(locator, path)

    source_surface = surface_of(source_path)
    if surface_visibility(source_surface, config) <= 0:
        return []  # nothing is more private than rank 0
    found: list[LinkViolation] = []
    for kind, ref in extract_link_refs(meta, body):
        if skip and (kind, ref) in skip:
            continue
        target = resolver.resolve(ref)
        if target is None:
            continue
        if involving is not None and not (
            _resolved(source_path) in involving or _resolved(target) in involving
        ):
            continue
        target_surface = surface_of(target)
        if link_forbidden(source_surface, target_surface, config):
            found.append(
                LinkViolation(
                    source_uid=str(meta.get("uid") or ""),
                    source_page=source_path.name,
                    source_surface=source_surface,
                    target_uid=_page_uid(target),
                    target_page=target.name,
                    target_surface=target_surface,
                    kind=kind,
                    ref=ref,
                )
            )
    return found


def guard_entity_store_links(
    wiki_root: Path,
    filename: str,
    rendered: str,
    meta: dict[str, Any],
    *,
    page_path: Path,
    index: EntityIndex,
    config: dict[str, Any] | None,
    previous_text: str | None = None,
    source: str = "unknown",
) -> bool:
    """Admit (``True``) or refuse (``False``) a page write by its link directions.

    *page_path* is where the page is being written (its surface is the link
    source's). *previous_text*, for an update to an existing page, limits the
    judgement to links the write ADDS: a pre-existing bad link is the lint's
    business, not a reason to block an unrelated edit. A refusal parks
    *rendered* under ``_store_link_rejected/``, appends a ledger record, and
    logs both uids and both surface names; the caller skips its write.
    """
    if not index.locator.surfaces:
        return True
    skip: set[tuple[str, str]] | None = None
    if previous_text:
        prev_meta, prev_body = parse_frontmatter(previous_text)
        skip = set(extract_link_refs(prev_meta if isinstance(prev_meta, dict) else {}, prev_body))
    _, body = parse_frontmatter(rendered)
    violations = _violations_for(
        source_path=page_path,
        meta=meta,
        body=body,
        index=index,
        resolver=_TargetResolver(index),
        config=config,
        skip=skip,
    )
    if not violations:
        return True
    _refuse_write(wiki_root, filename, rendered, violations, source)
    return False


def _refuse_write(
    wiki_root: Path,
    filename: str,
    rendered: str,
    violations: list[LinkViolation],
    source: str,
) -> None:
    """Park *rendered*, ledger the *violations* and log them (the refusal's side effects)."""
    rejected_dir = wiki_root / STORE_LINK_REJECTED_DIR_NAME
    rejected_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(rejected_dir / filename, rendered)
    for v in violations:
        log.warning(
            "write REFUSED (%s): page %s on surface %r links to page %s on the "
            "more private surface %r (%s %s)",
            StoreBoundaryLinkError.__name__,
            v.source_uid or v.source_page,
            v.source_surface,
            v.target_uid or v.target_page,
            v.target_surface,
            v.kind,
            v.ref,
        )
    record = {
        "ts": now_iso(),
        "filename": filename,
        "source": source,
        "violations": [v.__dict__ for v in violations],
    }
    append_line_durable(
        wiki_root / STORE_LINK_REJECTED_LEDGER_NAME,
        (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8"),
    )


def _vault_link_violations(
    page_path: Path,
    meta: Mapping[str, Any],
    body: str,
    vault_roots: Mapping[str, Path],
) -> list[LinkViolation]:
    """Links from a page outside every vault to a page inside one (athenaeum#3174).

    A vault page is outside every index, so the surface-rank rule never
    resolves a link to it. The pull request for athenaeum#3174 chose to
    REFUSE such a link rather than render the uid without a title: even a bare
    uid tells every reader of the main store that a withheld record exists and
    which one. A page inside a vault may link anywhere.
    """
    resolved = _resolved(page_path)
    if any(_resolved(root) in resolved.parents for root in vault_roots.values()):
        return []
    found: list[LinkViolation] = []
    refs = extract_link_refs(meta, body)
    for name, root in vault_roots.items():
        if not refs or not root.is_dir():
            continue
        index = EntityIndex(root)
        for kind, ref in refs:
            target = index.get_by_uid(ref)
            if target is None:
                entry = index.lookup(ref)
                target = Path(entry.path) if entry is not None else None
            if target is None:
                target = next(
                    (
                        p
                        for p in (root / ref, root / f"{PurePosixPath(ref).stem}.md")
                        if p.is_file()
                    ),
                    None,
                )
            if target is None:
                continue
            found.append(
                LinkViolation(
                    source_uid=str(meta.get("uid") or ""),
                    source_page=page_path.name,
                    source_surface="wiki",
                    target_uid=_page_uid(target),
                    target_page=target.name,
                    target_surface=name,
                    kind=kind,
                    ref=ref,
                )
            )
    return found


def admit_page_write(
    wiki_root: Path,
    page_path: Path,
    content: str,
    *,
    index: EntityIndex | None = None,
    config: Mapping[str, Any] | None = None,
    source: str = "unknown",
) -> bool:
    """The ONE call every page-writing path makes before it writes (athenaeum#2619).

    ``True`` admits the write; ``False`` refuses it (parked, ledgered and logged
    by :func:`guard_entity_store_links`) and the caller must skip it -- and, if
    the write came from a raw intake file, retain that raw rather than delete
    it. Reads the page's current bytes itself, so only links the write ADDS are
    judged. Cheap when it cannot matter: no extra surface configured, or a page
    on a rank-0 surface, returns before any index is built.
    """
    cfg: dict[str, Any] | None = dict(config) if config is not None else None
    knowledge_root = Path(wiki_root).parent
    if cfg is None:
        cfg = load_config(knowledge_root)
    surfaces = (
        index.locator.surfaces if index is not None and index.locator.surfaces else None
    )
    vault_roots = vault_page_roots(cfg, knowledge_root)
    if vault_roots:
        _vmeta, _vbody = parse_frontmatter(content)
        vault_violations = _vault_link_violations(
            page_path, _vmeta if isinstance(_vmeta, dict) else {}, _vbody, vault_roots
        )
        if vault_violations:
            _refuse_write(wiki_root, page_path.name, content, vault_violations, source)
            return False
    specs = resolve_locator_surfaces(knowledge_root, cfg)
    if not specs:
        return True
    locator = PageLocator(wiki_root, specs)
    if surface_visibility(surface_name_of(locator, page_path), cfg) <= 0:
        return True
    if surfaces is None:
        index = EntityIndex(wiki_root, specs)
    assert index is not None
    previous: str | None = None
    if page_path.is_file():
        try:
            previous = page_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            previous = None
    meta, _ = parse_frontmatter(content)
    return guard_entity_store_links(
        wiki_root,
        page_path.name,
        content,
        meta if isinstance(meta, dict) else {},
        page_path=page_path,
        index=index,
        config=cfg,
        previous_text=previous,
        source=source,
    )


@dataclass(frozen=True)
class StoreLinkReport:
    pages_scanned: int
    violations: tuple[LinkViolation, ...]

    @property
    def ok(self) -> bool:
        return not self.violations


def _scan(
    knowledge_root: Path,
    config: dict[str, Any],
    surface_override: Mapping[Path, str] | None,
    involving: set[Path] | None = None,
) -> StoreLinkReport:
    wiki_root = knowledge_root / "wiki"
    index = EntityIndex(wiki_root, resolve_locator_surfaces(knowledge_root, config))
    resolver = _TargetResolver(index)
    override = {_resolved(p): s for p, s in (surface_override or {}).items()}
    scanned = 0
    found: list[LinkViolation] = []
    for _key, path in index.locator.iter_pages():
        scanned += 1
        source_surface = override.get(_resolved(path)) or surface_name_of(index.locator, path)
        if surface_visibility(source_surface, config) <= 0:
            continue
        try:
            meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        found.extend(
            _violations_for(
                source_path=path,
                meta=meta if isinstance(meta, dict) else {},
                body=body,
                index=index,
                resolver=resolver,
                config=config,
                surface_override=override,
                involving=involving,
            )
        )
    return StoreLinkReport(pages_scanned=scanned, violations=tuple(found))


def scan_store_links(knowledge_root: Path, config: dict[str, Any] | None = None) -> StoreLinkReport:
    """Every existing link that breaks the visibility rule (read-only)."""
    if config is None:
        config = load_config(knowledge_root)
    return _scan(knowledge_root, config, None)


def scan_links_after_move(
    knowledge_root: Path,
    moves: Mapping[Path, str],
    config: dict[str, Any] | None = None,
) -> StoreLinkReport:
    """The links that WOULD break the visibility rule once *moves* happened (athenaeum#2620).

    *moves* maps each page that is about to move to the name of the surface it
    will live on afterwards. Every page is judged with moved pages taken at
    their post-move surface -- the same resolution and rule as
    :func:`scan_store_links` -- and only violations that involve a moved page
    (as source or target) are returned: a link that is already bad and untouched
    by the move is the lint's business. Read-only.
    """
    if config is None:
        config = load_config(knowledge_root)
    return _scan(knowledge_root, config, moves, involving={_resolved(p) for p in moves})
