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
call logs the result and holds it in a process-wide table, which the write guard
(access S3, :func:`declared_surface_rights`) consults: an owner who does not hold
compile on a declared store is refused on that store's pages. The same table
answers the mount question (access S4, :func:`declared_mount_state`): a declared
store on which the owner does not hold read is not mounted, so it is not indexed
and not recalled. Nothing on disk is deleted. A process that starts without a
run (a one-shot CLI command, a long-lived server) resolves a surface the first
time a write or the surface gather asks, and holds the answer; it is never
resolved per write or per query, and the recall query itself never reads it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
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
    "declared_surface_rights",
    "reset_run_rights",
    "MountState",
    "declared_mount_state",
    "run_start_mounts",
]

log = logging.getLogger(__name__)

#: What an install holds when the directory cannot answer.
READER_ONLY: frozenset[str] = frozenset({"read"})

_STATIC = "static"

#: Rights held for the process, keyed by (resolved surface root, identity key).
#: Filled at run start and, for a write path with no run, on first write.
_RUN_RIGHTS: dict[tuple[Path, tuple[str, ...] | None], frozenset[str] | None] = {}


def _cache_key(
    root: Path, asserter: Mapping[str, object] | None
) -> tuple[Path, tuple[str, ...] | None]:
    ident = asserter_identity_key(dict(asserter) if asserter else None)
    return (Path(root).resolve(), tuple(ident) if ident else None)


def reset_run_rights() -> None:
    """Forget every held resolution (a new run, or a test)."""
    _RUN_RIGHTS.clear()
    _MOUNTS.clear()
    _REPORTED.clear()


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
    return _resolve(declaration, asserter, adapters)[0]


def _resolve(
    declaration: StoreDeclaration,
    asserter: Mapping[str, object] | None,
    adapters: Mapping[str, DirectoryAdapter] | None,
) -> tuple[frozenset[str], bool]:
    """:func:`resolve_rights` plus whether the directory could not answer."""
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
                    return READER_ONLY, True
                found = directory.members(member.group)
                if found is None:
                    return READER_ONLY, True
                if identity and identity in found:
                    granted |= role.rights
    return frozenset(r for r in RIGHTS if r in granted), False


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


def declared_surface_rights(
    surface_root: Path,
    asserter: Mapping[str, object] | None,
) -> frozenset[str] | None:
    """The rights held for *surface_root* this run: resolved once, then reused.

    ``None`` when the surface has no declaration. Fails closed: a resolution that
    raises is held as :data:`READER_ONLY`, so the write guard refuses and a bad
    declaration never widens anything.
    """
    key = _cache_key(surface_root, asserter)
    if key not in _RUN_RIGHTS:
        try:
            _RUN_RIGHTS[key] = resolve_surface_rights(surface_root, asserter)
        except Exception as exc:  # noqa: BLE001 - fail closed, report why
            log.warning(
                "store rights: %s: resolution failed (%s)", surface_root, type(exc).__name__
            )
            _RUN_RIGHTS[key] = READER_ONLY
    return _RUN_RIGHTS[key]


def record_run_start_rights(
    surface_roots: Iterable[Path],
    asserter: Mapping[str, object] | None,
) -> dict[str, frozenset[str] | None]:
    """Resolve, log and hold the owner's rights on each surface.

    Replaces anything held from an earlier run, so a changed declaration takes
    effect at the next run; the write guard reads what this holds.

    One INFO line per surface (``store rights: <surface>: read,compile`` or
    ``no declaration``). Any failure is logged and swallowed: a broken
    declaration must not stop a run (the guard holds it as reader only). Returns
    the result for callers that want it in memory.
    """
    out: dict[str, frozenset[str] | None] = {}
    seen: set[Path] = set()
    reset_run_rights()
    for root in surface_roots:
        if root in seen:
            continue
        seen.add(root)
        try:
            rights = declared_surface_rights(root, asserter)
        except Exception as exc:  # noqa: BLE001 - never let this stop a run
            log.warning("store rights: %s: resolution failed (%s)", root, type(exc).__name__)
            continue
        out[str(root)] = rights
        if rights is None:
            log.info("store rights: %s: no declaration", root)
        else:
            log.info("store rights: %s: %s", root, ",".join(sorted(rights)) or "none")
    return out


# -- mounting (access S4) ----------------------------------------------------


@dataclass(frozen=True)
class MountState:
    """Whether a configured surface is mounted, and why not (or what to report).

    ``refusal`` is a short reason (``no-read-right`` or ``store-id-mismatch``)
    when the surface is not mounted; ``note`` is set when it IS mounted but the
    declaration could only be read as a reader (invalid, or the directory could
    not answer). Neither carries page text.
    """

    refusal: str | None = None
    note: str | None = None


_MOUNTS: dict[tuple[Path, tuple[str, ...] | None, str | None], MountState] = {}
_REPORTED: set[tuple[str, str]] = set()


def _compute_mount(
    root: Path, asserter: Mapping[str, object] | None, store_id: str | None
) -> MountState:
    declaration = load_store_declaration(root)
    if declaration is None:
        return MountState()
    if isinstance(declaration, StoreDeclarationInvalid):
        return MountState(note="declaration-invalid: mounted as a reader")
    if store_id is not None and store_id != declaration.store_id:
        return MountState(refusal="store-id-mismatch")
    rights, degraded = _resolve(declaration, asserter, None)
    if "read" not in rights:
        return MountState(refusal="no-read-right")
    if degraded:
        return MountState(note="directory-cannot-answer: mounted as a reader")
    return MountState()


def declared_mount_state(
    name: str,
    surface_root: Path,
    asserter: Mapping[str, object] | None,
    store_id: str | None = None,
) -> MountState:
    """Whether the surface *name* at *surface_root* is mounted: resolved once, held.

    A surface with no declaration mounts as it always has. A declaration whose
    ``store_id`` differs from the adapter entry's ``store_id`` (when the entry
    carries one) is refused; so is one whose resolved rights do not include
    read (propose does not imply read). An invalid declaration, or a directory
    that cannot answer, mounts the store as a reader and says so. A refusal is
    logged once per process (store name and reason only); nothing is deleted.
    Failure narrows to read (design note §11), so an unexpected error mounts the
    store as a reader and reports it.
    """
    key = (*_cache_key(surface_root, asserter), store_id)
    state = _MOUNTS.get(key)
    if state is None:
        try:
            state = _compute_mount(surface_root, asserter, store_id)
        except Exception as exc:  # noqa: BLE001 - failure narrows to read, never widens
            state = MountState(
                note=f"resolution-failed ({type(exc).__name__}): mounted as a reader"
            )
        _MOUNTS[key] = state
    for kind, why in (("not mounted", state.refusal), ("mounted", state.note)):
        if why and (name, why) not in _REPORTED:
            _REPORTED.add((name, why))
            log.warning("store %r %s: %s", name, kind, why)
    return state


def run_start_mounts(
    surfaces: Mapping[str, tuple[Path, str | None]],
    asserter: Mapping[str, object] | None,
) -> dict[str, MountState]:
    """Resolve the mount state of each ``{name: (root, store_id)}`` at run start."""
    return {
        name: declared_mount_state(name, root, asserter, store_id)
        for name, (root, store_id) in surfaces.items()
    }
