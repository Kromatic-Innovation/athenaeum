# SPDX-License-Identifier: Apache-2.0
"""Source-handle registry builder (issue #453, epic #422).

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
operator-only, private-store operation (issue #454); the builder must emit a
well-formed registry even when zero handles are populated, so #454 is never a
precondition for it working.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
        cleaned = _clean_list(meta.get(key))
        if cleaned:
            handles[key] = cleaned
    for key in SCALAR_HANDLE_KEYS:
        cleaned = _clean_scalar(meta.get(key))
        if cleaned:
            handles[key] = cleaned
    return handles


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
    handle (the degenerate seed-not-landed-yet case, issue #453/#454), the
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
