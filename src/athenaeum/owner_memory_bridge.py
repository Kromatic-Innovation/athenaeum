# SPDX-License-Identifier: Apache-2.0
"""Bridge author-typed owner auto-memory notes into the entity tier (athenaeum#2690).

A first-person auto-memory note ("I prefer oat milk in coffee") compiles only
into ``wiki/auto-*.md``: the auto-memory path makes no tier-2 classify call, so
the owner candidate (athenaeum#2653) is never offered. This module closes the
gap with deterministic code and NO model call:

1. **Select** an auto-memory file when an owner is configured, the owner's
   page resolves, and the file's author-set ``memory_type`` is in
   ``owner.bridge_memory_types`` (default ``["user"]``).
2. **Bridge** each selected file whose routed marker is missing or stale: write
   one derived raw note into ``raw/owner-memory-bridge/`` via
   :func:`athenaeum.storage.write_raw_intake`, carrying the memory body and
   provenance back to the source file. Only after that write succeeds, stamp
   ``owner_routed: <body hash>`` into the source file's frontmatter, so an
   edited body is bridged again and a failed write is retried next run.
3. The entity loop then handles the note like any raw intake: tier 2 offers the
   owner candidate, tier 3 merges with the owner-verify note. Both calls count
   against the run's ``max_api_calls`` and are deferred, never dropped, when it
   is spent.
4. **Owner-only scope**: a bridge note is marked ``owner_only: true``;
   :func:`restrict_to_owner` drops every tier-2 classification except the
   owner's before tier 3, so no other person's or project's page is created or
   edited from an assistant note.

Layering: L3 (service). Imports :mod:`athenaeum.config`, :mod:`athenaeum.models`,
:mod:`athenaeum.storage` and :mod:`athenaeum.person_hints`.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from athenaeum.atomic_io import atomic_write_text
from athenaeum.config import resolve_owner_bridge_memory_types
from athenaeum.models import parse_frontmatter, render_frontmatter
from athenaeum.person_hints import _owner_candidate, owner_uid_from_config
from athenaeum.storage import write_raw_intake

if TYPE_CHECKING:
    from athenaeum.models import AutoMemoryFile, ClassifiedEntity, EntityIndex
    from athenaeum.person_registry import PersonRegistry

log = logging.getLogger(__name__)

#: ``raw/<source>/`` directory the derived notes land in.
BRIDGE_SOURCE = "owner-memory-bridge"
#: Source-file frontmatter key: short body hash the file was last routed at.
ROUTED_KEY = "owner_routed"
#: Bridge-note frontmatter key marking owner-only scope.
OWNER_ONLY_KEY = "owner_only"
#: Bridge-note frontmatter key carrying provenance back to the source file.
BRIDGED_FROM_KEY = "bridged_from"


def body_hash(body: str) -> str:
    """Short, whitespace-edge-insensitive hash of a memory body."""
    return hashlib.sha256(body.strip().encode("utf-8")).hexdigest()[:16]


def is_owner_only_raw(raw_meta: dict[str, object] | None) -> bool:
    """True when a raw note's frontmatter marks it as an owner-only bridge note."""
    if not raw_meta:
        return False
    return raw_meta.get(OWNER_ONLY_KEY) is True


def restrict_to_owner(
    classified: "list[ClassifiedEntity]", owner_uid: str | None, ref: str
) -> "list[ClassifiedEntity]":
    """Keep only the classification that targets the owner's existing page.

    Everything else (new entities, other people, projects) is dropped by code,
    before any tier-3 call. With no owner uid nothing is kept.
    """
    kept = [c for c in classified if owner_uid and not c.is_new and c.existing_uid == owner_uid]
    if len(kept) != len(classified):
        log.info(
            "  T2 owner-only scope (athenaeum#2690): %s dropped %d non-owner classification(s)",
            ref,
            len(classified) - len(kept),
        )
    return kept


def bridge_owner_memories(
    auto_memory_files: "list[AutoMemoryFile]",
    *,
    config: dict[str, Any] | None,
    raw_root: Path,
    index: "EntityIndex",
    person_registry: "PersonRegistry | None",
) -> int:
    """Bridge selected owner notes into ``raw/owner-memory-bridge/``; return the count.

    Never makes a model call and never raises for a per-file failure. Inert
    (returns 0, touches nothing) with no configured owner, an owner whose page
    does not resolve, or an empty ``owner.bridge_memory_types``.
    """
    types = resolve_owner_bridge_memory_types(config)
    if not types or not auto_memory_files:
        return 0
    owner_uid = owner_uid_from_config(config)
    if owner_uid is None or _owner_candidate(owner_uid, index, person_registry) is None:
        return 0

    bridged = 0
    for am in auto_memory_files:
        if getattr(am, "memory_type", "") not in types:
            continue
        path = getattr(am, "path", None)
        if path is None:
            continue
        try:
            meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            log.warning("owner bridge: cannot read %s (%s); skipping", path, exc)
            continue
        if not body.strip():
            continue
        digest = body_hash(body)
        if str((meta or {}).get(ROUTED_KEY, "") or "") == digest:
            continue
        note_meta: dict[str, object] = {
            "source": BRIDGE_SOURCE,
            OWNER_ONLY_KEY: True,
            BRIDGED_FROM_KEY: f"auto-memory/{am.origin_scope}/{path.name}",
            "body_hash": digest,
        }
        if am.origin_session_id:
            note_meta["origin_session_id"] = am.origin_session_id
        try:
            write_raw_intake(raw_root / BRIDGE_SOURCE, render_frontmatter(note_meta) + body)
        except OSError as exc:
            log.warning("owner bridge: write failed for %s (%s); will retry", path, exc)
            continue
        # Stamp only after the bridge write succeeded; a failed stamp means the
        # note is bridged again next run (a duplicate merge the owner-verify
        # note's "never duplicate" rule absorbs), never a lost one.
        new_meta = dict(meta) if meta else {}
        new_meta[ROUTED_KEY] = digest
        try:
            atomic_write_text(path, render_frontmatter(new_meta) + body)
        except OSError as exc:
            log.warning("owner bridge: cannot stamp %s (%s)", path, exc)
        bridged += 1
    if bridged:
        log.info("owner bridge: bridged %d auto-memory note(s) to the owner page", bridged)
    return bridged


__all__ = [
    "BRIDGE_SOURCE",
    "BRIDGED_FROM_KEY",
    "OWNER_ONLY_KEY",
    "ROUTED_KEY",
    "body_hash",
    "bridge_owner_memories",
    "is_owner_only_raw",
    "restrict_to_owner",
]
