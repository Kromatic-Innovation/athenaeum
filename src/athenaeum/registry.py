# SPDX-License-Identifier: Apache-2.0
"""Source-handle registry builder (issue athenaeum#453, epic athenaeum#422).

The fact-mining pipeline (``athenaeum-adapters``) needs a canonical mapping
from a wiki entity to the corpus handles it resolves against — the domains,
email aliases, Slack channels/user-ids, LinkedIn URL, partner domains, Drive
folder ids and Mural board ids that identify the same real-world entity across
external sources. Those handles live ON the entity page as frontmatter (the
wiki owns knowledge about entities; adapters consume it), round-tripped
byte-for-byte by tier0 passthrough.

This module compiles that scattered frontmatter into a single ``registry.json``
index — ``entity uid → handle set`` — that adapters can load without walking
the wiki. It is a deterministic, LLM-free read of the wiki tree, exactly like
the ``people`` and ``compile-as-of`` commands.

**Tooling only — no data.** This builder ships in the public OSS repo and is
tested with synthetic fixtures. Populating real client handles is a separate,
operator-only, private-store operation (issue athenaeum#454); the builder must emit a
well-formed registry even when zero handles are populated, so athenaeum#454 is never a
precondition for it working.

Layering: L1 (data model — reads the wiki tree via :mod:`athenaeum.models`,
the L1 hub, for :func:`~athenaeum.models.parse_frontmatter`). No config, no
LLM client. Factoring rule: this module owns ONLY the deterministic
frontmatter → registry compile (extract known handle keys, sort, serialize);
it must never invent new handle keys' semantics or validate the handle
VALUES beyond non-empty-string cleanup — a source handle's meaning belongs to
the adapter that consumes it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from athenaeum.models import parse_frontmatter

#: List-valued source-handle keys — each holds zero or more string handles.
LIST_HANDLE_KEYS: tuple[str, ...] = (
    "domains",
    "alt_emails",
    "slack_channels",
    "slack_user_ids",
    "partner_domains",
    "drive_folder_ids",
    "mural_board_ids",
)

#: Scalar source-handle keys — a single string value, empty when unset.
SCALAR_HANDLE_KEYS: tuple[str, ...] = (
    "linkedin_url",
    "handles_verified",
    "apollo_organization_id",
    # Issue athenaeum#902: Google People API resource name (`people/c123...`) for a
    # contact-sync record. Registered here — the same seeding path
    # `apollo_organization_id` (athenaeum#874) uses — so `collect_handles` picks it
    # up from frontmatter and contact-sync records resolve their target person
    # deterministically through registry.json instead of by reasoning. 89% of
    # resource names recur day over day, so the registry warms almost at once.
    #
    # Unlike `email` (see `corrections.EMAIL_HANDLE_KEY`), a resource name is an
    # opaque provider id, not a contact identifier: it is not PII, so it belongs
    # in the registry rather than on the PII surface.
    "google_resource_name",
)

#: All source-handle keys, in canonical (template) order. This is the
#: contract documented in ``docs/source-handles.md``; keep the three in
#: sync.
SOURCE_HANDLE_KEYS: tuple[str, ...] = LIST_HANDLE_KEYS + SCALAR_HANDLE_KEYS

#: registry.json schema version. Bump when the on-disk shape changes so
#: adapters can detect an incompatible index.
REGISTRY_VERSION = 1


def _clean_list(value: Any) -> list[str]:
    """Coerce a frontmatter list value into a list of non-empty strings.

    Tolerant of the shapes YAML frontmatter actually produces: a real list,
    a lone scalar (authored without brackets), or ``None``/missing. Falsy and
    whitespace-only entries are dropped so ``[""]`` or ``[null]`` count as
    unpopulated rather than smuggling empty handles into the index.
    """
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    out: list[str] = []
    for item in items:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def _clean_scalar(value: Any) -> str:
    """Coerce a frontmatter scalar into a trimmed string ("" when unset)."""
    if value is None:
        return ""
    return str(value).strip()


def collect_handles(meta: dict[str, Any]) -> dict[str, Any]:
    """Extract the populated source-handle keys from one page's frontmatter.

    Returns a dict containing only the keys that carry a real value, in
    canonical :data:`SOURCE_HANDLE_KEYS` order — list keys as cleaned
    ``list[str]``, scalar keys as non-empty ``str``. An entity with no
    populated handles yields ``{}`` (its caller then omits it from the
    registry), which is what makes the zero-handles case well-formed.
    """
    handles: dict[str, Any] = {}
    for key in LIST_HANDLE_KEYS:
        cleaned_list = _clean_list(meta.get(key))
        if cleaned_list:
            handles[key] = cleaned_list
    for key in SCALAR_HANDLE_KEYS:
        cleaned_scalar = _clean_scalar(meta.get(key))
        if cleaned_scalar:
            handles[key] = cleaned_scalar
    return handles


class UnplaceableSourceHandleError(Exception):
    """A raw-intake file's populated source-handle key(s) could not be placed
    as frontmatter on any page written for it (issue athenaeum#1109).

    Silently letting a populated :data:`SOURCE_HANDLE_KEYS` value fall through
    into page-body prose is the exact defect this guards against: the page
    compiles, the intake reports success, and ``registry.json`` is left
    silently missing the handle. Raising here — instead of logging a warning
    and letting the write proceed — surfaces the loss the same way every
    other Tier-2/3 processing failure already is: the raw file is left on
    disk untouched (nothing from this call is written), the run's per-file
    failure/stuck-file ledger records it, and the file is retried next run.

    :func:`athenaeum.librarian.tier0_handle_upsert` (and ``tier0_passthrough``)
    are the deterministic paths that ordinarily place a source handle as
    frontmatter without ever reaching here. This is the backstop for
    whatever those decline onto, or that never reaches tier0 at all.
    """


def assert_handles_placed(
    incoming: dict[str, Any], written_metas: Iterable[dict[str, Any]]
) -> None:
    """Raise :class:`UnplaceableSourceHandleError` if a source handle
    populated in *incoming* is missing from every dict in *written_metas*.

    *incoming* is normally :func:`collect_handles` applied to a raw file's
    OWN frontmatter; *written_metas* is the frontmatter of every page that
    raw's processing is about to write. A no-op when *incoming* is empty —
    the raw carries no source handles at all, the overwhelmingly common
    case — so this costs nothing on the hot path.

    List-valued keys compare as sets (placement, not list order, is the
    guarantee); scalar keys compare by value.
    """
    if not incoming:
        return
    placed: dict[str, Any] = {}
    for meta in written_metas:
        placed.update(collect_handles(meta))
    missing = [
        key
        for key, value in incoming.items()
        if (
            set(value) != set(placed.get(key) or [])
            if key in LIST_HANDLE_KEYS
            else placed.get(key) != value
        )
    ]
    if missing:
        raise UnplaceableSourceHandleError(
            f"source handle key(s) {sorted(missing)} were populated on the raw "
            "intake file but did not land as frontmatter on any page written "
            "for it — refusing to let them fall through to the LLM tiers and "
            "be folded into page-body prose instead (issue athenaeum#1109)"
        )


def build_registry(wiki_root: Path) -> dict[str, Any]:
    """Compile the source-handle registry from a wiki tree.

    Walks ``wiki_root/*.md`` (skipping ``_``-prefixed non-entity pages),
    parses each page's frontmatter, and records every entity that carries at
    least one populated source handle as ``entities[uid]``. Type-agnostic: any
    page with a non-empty ``uid`` and a populated handle is indexed (the keys
    live on the person/company templates, but nothing stops another entity
    type from carrying them).

    The returned dict is deterministic — entities are sorted by uid and each
    handle set preserves canonical key order — so re-running on an unchanged
    wiki produces byte-identical output. When no entity has any populated
    handle (the degenerate seed-not-landed-yet case, issue athenaeum#453/#454), the
    result is still well-formed: ``entities`` is an empty object and
    ``entity_count`` is ``0``.
    """
    entities: dict[str, dict[str, Any]] = {}
    if wiki_root.is_dir():
        for path in sorted(wiki_root.glob("*.md")):
            if path.name.startswith("_"):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            meta, _ = parse_frontmatter(text)
            if not meta:
                continue
            uid = _clean_scalar(meta.get("uid"))
            if not uid:
                continue
            handles = collect_handles(meta)
            if not handles:
                continue
            entities[uid] = {
                "type": _clean_scalar(meta.get("type")),
                "name": _clean_scalar(meta.get("name")),
                "handles": handles,
            }

    ordered = {uid: entities[uid] for uid in sorted(entities)}
    return {
        "version": REGISTRY_VERSION,
        "entity_count": len(ordered),
        "entities": ordered,
    }


def render_registry(registry: dict[str, Any]) -> str:
    """Serialize a registry dict to canonical JSON text (trailing newline)."""
    return json.dumps(registry, indent=2, sort_keys=False, ensure_ascii=False) + "\n"
