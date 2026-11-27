# SPDX-License-Identifier: Apache-2.0
"""Where the compile write path writes a new page: the decision's store (athenaeum#2999).

``docs/design/federation-intake-routing.md`` sections 4.1 and 4.4, slice S3a of
athenaeum#2625. The compile write path used to derive a page's destination from
its type alone (:func:`athenaeum.storage.page_write_root_for_class`). It now asks
:func:`athenaeum.intake_decision.decide` for the store and writes under that
store's root. ``storage.mapping`` is the default destination, fed to ``decide``
as its ``mapping`` input, so with no decision naming anything else every page
lands where it landed before: no routing change.

Both pipeline transports (:mod:`athenaeum.batch` and :mod:`athenaeum.librarian`)
call :func:`destination_for_entity` at the one place they resolve a write root,
so they cannot diverge.

Fail closed: a decision (other than the mapping default, which keeps today's
refusal and retained raw) that names a store this install does not hold, or
cannot write (read-only, declared refusal, not indexed), writes to the personal
store instead and says so in :attr:`Destination.fallback`. It never writes into
a read-only surface.

Layering: L3. Imports :mod:`athenaeum.storage` (L1) and
:mod:`athenaeum.intake_decision` (L2) and :mod:`athenaeum.relevance_policy` (L2).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from athenaeum.intake_decision import (
    PERSONAL,
    REASON_MAPPING,
    Description,
    IntakeDecision,
    StoreInfo,
    decide,
)
from athenaeum.models import EntityIndex
from athenaeum.relevance_policy import RelevancePolicy
from athenaeum.storage import (
    DEFAULT_ADAPTER_NAME,
    ReadOnlySurfaceError,
    StorageConfigError,
    SurfaceNotIndexedError,
    available_adapters,
    page_write_root_for_store,
    resolve_adapter_for_class,
    store_opaque_filenames,
    vault_route_for_page_type,
)

if TYPE_CHECKING:
    from athenaeum.models import WikiEntity

log = logging.getLogger(__name__)

_WRITE = frozenset({"read", "compile"})


#: A policy with no rules: S3a routes on ``storage.mapping`` alone. ``route``
#: rows taking effect is slice S3b, so the write path decides with the empty
#: policy rather than the capture policy.
_MAPPING_ONLY_POLICY = RelevancePolicy()


@dataclass(frozen=True)
class Destination:
    """The root a page is written under, and why it is not the decision's store."""

    root: Path
    store: str
    #: ``None`` when the decision's store was used; else why personal was used.
    fallback: str | None = None
    #: The store opted in to uid-only file names for NEW pages (athenaeum#3077).
    opaque_filenames: bool = False
    #: The vault name when the page is a whole page of a type a ``storage.routes``
    #: rule sends to a vault (athenaeum#3174): outside the main index and name
    #: lookup, so callers scope lookups to it and keep it out of the main index.
    vault: str | None = None


def _store_word(adapter_name: str) -> str:
    return PERSONAL if adapter_name == DEFAULT_ADAPTER_NAME else adapter_name


def decide_destination(
    entity_type: str | None,
    tags: tuple[str, ...] = (),
    config: dict[str, Any] | None = None,
) -> IntakeDecision:
    """The decision for a kept fact, with ``storage.mapping`` as the default store.

    Raises :class:`StorageConfigError` when the mapping names an unknown
    adapter, exactly as the type-derived route did.
    """
    if "personal" in available_adapters(config):
        raise StorageConfigError(
            "storage.adapters.personal collides with the reserved store word "
            "'personal' (the default wiki surface) -- pick a different adapter name"
        )
    mapping: Mapping[str, str] = {}
    if entity_type:
        mapping = {entity_type: _store_word(resolve_adapter_for_class(entity_type, config).name)}
    stores = {_store_word(n): StoreInfo(rights=_WRITE) for n in available_adapters(config)}
    return decide(
        Description(type=entity_type or None, tags=tags), _MAPPING_ONLY_POLICY, stores, mapping
    )


def resolve_destination(
    decision: IntakeDecision,
    entity_type: str | None,
    config: dict[str, Any] | None,
    knowledge_root: Path,
) -> Destination:
    """The write root for *decision*'s store.

    The mapping default raises the storage guards' errors unchanged (the caller
    retains the raw, as before). A store named any other way that this install
    does not hold or cannot write falls back to personal, reported.
    """
    store = decision.store or PERSONAL
    try:
        return Destination(
            page_write_root_for_store(store, entity_type, config, knowledge_root),
            store,
            opaque_filenames=store_opaque_filenames(store, config),
            vault=store if vault_route_for_page_type(entity_type, config) == store else None,
        )
    except (ReadOnlySurfaceError, SurfaceNotIndexedError, StorageConfigError) as exc:
        if store == PERSONAL or decision.reason == REASON_MAPPING:
            raise
        reason = f"store {store!r} not writable here ({exc}); wrote to the personal store"
        log.warning("  Intake destination fail-closed: %s", reason)
        return Destination(
            page_write_root_for_store(PERSONAL, entity_type, config, knowledge_root),
            PERSONAL,
            reason,
            store_opaque_filenames(PERSONAL, config),
        )


def destination_for_entity(
    entity_type: str | None,
    config: dict[str, Any] | None,
    knowledge_root: Path,
    decision: IntakeDecision | None = None,
    tags: tuple[str, ...] = (),
) -> Destination:
    """The compile write path's one call: *decision* when handed, else decided here."""
    if decision is None:
        decision = decide_destination(entity_type, tags, config)
    return resolve_destination(decision, entity_type, config, knowledge_root)


def destination_for_new_entity(
    entity: WikiEntity,
    config: dict[str, Any] | None,
    knowledge_root: Path,
    *,
    entity_type: str | None = None,
    decision: IntakeDecision | None = None,
) -> Destination:
    """Resolve *entity*'s destination AND apply its file-name policy to it.

    The one call every write path that creates a NEW page uses
    (athenaeum#3126): the store's ``opaque_filenames`` choice is set on
    ``entity.opaque_filename`` here, where the destination is resolved, so a
    caller cannot resolve a root and forget the flag. *entity_type* overrides
    ``entity.type`` when the caller routes by a different type. Raises exactly
    what :func:`destination_for_entity` raises, before touching *entity*.
    """
    dest = destination_for_entity(
        entity_type if entity_type is not None else entity.type,
        config,
        knowledge_root,
        decision,
        tags=tuple(entity.tags),
    )
    entity.opaque_filename = dest.opaque_filenames
    return dest


def vault_page_exists(dest: Destination, name: str) -> bool:
    """Whether *dest*'s vault already holds a page named (or aliased) *name*.

    The lookup for an update to a vault page runs inside the vault only
    (athenaeum#3174): the index is built over the vault's own root, so a name
    that matches a page in the main store or in another vault is not a match,
    and a vault page's name never matches a lookup in the main store. ``False``
    for a destination that is not a vault.
    """
    return dest.vault is not None and EntityIndex(dest.root).lookup(name) is not None
