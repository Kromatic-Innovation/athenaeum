# SPDX-License-Identifier: Apache-2.0
"""Directory adapter protocol, the ``static`` adapter, and rights resolution.

Layering: L2 (imports :mod:`athenaeum.store_access`, L2, and
:mod:`athenaeum.models`, L1, for ``asserter_identity_key``).

Design: ``docs/design/federation-store-access.md`` §4 (the four rights), §6
(naming principals and groups), §12 slice S2.

Given a parsed declaration and the owner's ``asserter`` block, :func:`resolve_rights`
returns the subset of ``{read, propose, compile, answer}`` the owner holds on the
store, empty when none. It never returns a role name: roles are the store's own
data and no code keys on them.

**Failure narrows; it never widens.** When the directory cannot answer (an
adapter returns ``None``, the declared adapter is not registered, or a group is
unknown) the install resolves as a reader only. A declaration that failed to
validate resolves the same way.

**Never on the recall path.** Resolution runs when a librarian run starts
(:func:`record_run_start_rights`, called from ``librarian.run``) and, later, when
``athenaeum store access check`` runs (access S5 adds that caller). The run-start
call only logs the result; nothing acts on it, so no install behaves differently.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Protocol

from athenaeum.models import asserter_identity_key
from athenaeum.store_access import (
    RIGHTS,
    StoreDeclaration,
    StoreDeclarationInvalid,
    load_store_declaration,
)

__all__ = [
    "DirectoryAdapter",
    "StaticDirectory",
    "READER_ONLY",
    "resolve_rights",
    "resolve_surface_rights",
    "record_run_start_rights",
]

log = logging.getLogger(__name__)

#: What an install holds when the directory cannot answer.
READER_ONLY: frozenset[str] = frozenset({"read"})

_STATIC = "static"


class DirectoryAdapter(Protocol):
    """Maps a group name to the identity keys of its members.

    Returns ``None`` when the directory cannot answer. ``None`` is distinct from
    an empty set: an empty set is an answer ("nobody is in this group").
    """

    def members(self, group: str) -> frozenset[tuple[str, ...]] | None: ...


class StaticDirectory:
    """The built-in adapter: reads the declaration's ``groups:`` block.

    Needs no network and no account. A group the declaration does not define is
    one the directory cannot answer for, so it returns ``None``.
    """

    def __init__(self, declaration: StoreDeclaration) -> None:
        self._groups: dict[str, frozenset[tuple[str, ...]]] = {
            g.name: frozenset(p.key for p in g.members) for g in declaration.groups
        }

    def members(self, group: str) -> frozenset[tuple[str, ...]] | None:
        return self._groups.get(group)


def resolve_rights(
    declaration: StoreDeclaration,
    asserter: Mapping[str, object] | None,
    adapters: Mapping[str, DirectoryAdapter] | None = None,
) -> frozenset[str]:
    """The rights the owner named by *asserter* holds on the declared store.

    *adapters* maps a directory name to an adapter other than ``static``
    (registration is a later slice). A declared directory with no adapter
    resolves as a reader only.
    """
    directory: DirectoryAdapter | None
    if declaration.directory == _STATIC:
        directory = StaticDirectory(declaration)
    else:
        directory = (adapters or {}).get(declaration.directory)

    identity = asserter_identity_key(dict(asserter) if asserter else None)
    granted: set[str] = set()
    for role in declaration.roles:
        for member in role.members:
            if member.kind == "any":
                # "*" defers read to the host. It never carries more than read,
                # whatever role it was written in.
                granted.add("read")
            elif member.kind == "principal" and member.principal is not None:
                if identity and member.principal.key == identity:
                    granted |= role.rights
            elif member.kind == "group" and member.group is not None:
                if directory is None:
                    return READER_ONLY
                found = directory.members(member.group)
                if found is None:
                    return READER_ONLY
                if identity and identity in found:
                    granted |= role.rights
    return frozenset(r for r in RIGHTS if r in granted)


def resolve_surface_rights(
    surface_root: Path,
    asserter: Mapping[str, object] | None,
    adapters: Mapping[str, DirectoryAdapter] | None = None,
) -> frozenset[str] | None:
    """Resolve rights for the store at *surface_root*.

    ``None`` when the surface has no declaration (it behaves as it does today).
    A declaration that is present but invalid resolves as a reader only.
    """
    declaration = load_store_declaration(surface_root)
    if declaration is None:
        return None
    if isinstance(declaration, StoreDeclarationInvalid):
        return READER_ONLY
    return resolve_rights(declaration, asserter, adapters)


def record_run_start_rights(
    surface_roots: Iterable[Path],
    asserter: Mapping[str, object] | None,
) -> dict[str, frozenset[str] | None]:
    """Resolve and log the owner's rights on each surface; act on nothing.

    One INFO line per surface (``store rights: <surface>: read,compile`` or
    ``no declaration``). Any failure is logged and swallowed: a broken
    declaration must not stop or change a run. Returns the result for callers
    that want to hold it in memory.
    """
    out: dict[str, frozenset[str] | None] = {}
    seen: set[Path] = set()
    for root in surface_roots:
        if root in seen:
            continue
        seen.add(root)
        try:
            rights = resolve_surface_rights(root, asserter)
        except Exception as exc:  # noqa: BLE001 - never let this stop a run
            log.warning("store rights: %s: resolution failed (%s)", root, type(exc).__name__)
            continue
        out[str(root)] = rights
        if rights is None:
            log.info("store rights: %s: no declaration", root)
        else:
            log.info("store rights: %s: %s", root, ",".join(sorted(rights)) or "none")
    return out
