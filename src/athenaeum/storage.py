# SPDX-License-Identifier: Apache-2.0
"""Pluggable storage-surface layer — entity class → surface + corpus policy (issue athenaeum#429).

This module generalizes the "PII lives on an excluded path" idea (athenaeum#427) into a
**config-swappable storage-adapter layer**. Each *entity class* (the wiki
frontmatter ``type:`` — ``person``, ``concept``, ``reference``, a future
``pii`` class, …) resolves to a **storage surface**: a ``backing_store`` plus a
``corpus_policy`` declaring whether that surface participates in the embedded /
recallable / merge-eligible corpus. Which surface a class maps to is a
**configuration choice, changeable later** — not a hardcoded path.

Two adapters ship built in:

``wiki-markdown-embedded`` (the default)
    The current behavior, unchanged. Backing store is the flat markdown
    ``wiki/`` tree; corpus policy is all-true (embedded + recallable +
    merge-eligible). **Every entity class maps here unless config says
    otherwise**, so a knowledge base with no ``storage:`` config behaves
    byte-for-byte as it did before this layer existed — "the wiki is just the
    default adapter."

``excluded``
    A surface OUTSIDE ``wiki/`` (default ``excluded/``) whose corpus policy is
    all-false: nothing on it is embedded, recalled, or merged. This is what
    athenaeum#427's PII / archival-contact surface consumes — routed through this
    adapter, **not a hardcoded exclusion path**. Because an excluded surface's
    root lives outside the corpus scanners' search set (``wiki/`` + configured
    ``recall.extra_intake_roots``), its pages are excluded from embed / recall /
    merge **by construction** — the fail-closed property athenaeum#427 requires.

Adding a new surface (a new store or a new corpus policy) is **config + an
adapter registration, no core change**: define it under ``storage.adapters`` in
``athenaeum.yaml`` (or call :func:`register_adapter` from code) and map an
entity class to it under ``storage.mapping``. The deferred skill-file-sync
surface (athenaeum#426's out-of-scope idea) is a future consumer of exactly this seam.

Naming note — this is a STORAGE-surface adapter, a different concept from the
source → raw-intake *adapter* documented in ``docs/extending/adapter-contract.md`` and
the bundled ``adapter-authoring`` skill. An intake adapter turns an external
source into ``raw/`` files; a storage adapter governs where a compiled entity
class is persisted and whether that surface joins the corpus. They sit on
opposite ends of the pipeline and never collide.

Like :class:`athenaeum.search.SearchBackend` and
:func:`athenaeum.provider.build_llm_client`, this is an INTERNAL seam: it is
importable but not part of the stable ``__all__`` surface, and its signatures
may change between minor releases until the extension point is promoted with a
companion ``docs/extending/storage-adapter-contract.md``.

Layering: L1 (data model — storage-surface resolution for wiki entity
classes). Imports :mod:`athenaeum.atomic_io` (L0) for the actual write and
:mod:`athenaeum.config` (L2) to resolve adapters/mapping — the one place in
this module's assigned L0/L1 set that legitimately reaches up to L2, because
"which surface a class maps to" is explicitly a config-owned decision (see
above). Factoring rule: this module owns ONLY surface resolution + the
generic write-through-the-resolved-surface path; it must never itself decide
corpus policy for a specific entity beyond what ``storage.mapping`` says.
"""

from __future__ import annotations

import importlib
import os
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from athenaeum.atomic_io import atomic_write_text
from athenaeum.config import (  # StorageConfigError is defined in config, re-exported here
    HISTORY_VALUES,
    StorageConfigError,
    load_config,
    resolve_excluded_fields_config,
    resolve_excluded_read_mapping,
    resolve_extra_intake_roots,
    resolve_owner_asserter,
    resolve_storage_adapters,
    resolve_storage_history,
    resolve_storage_mapping,
    resolve_storage_routes,
)
from athenaeum.store import FilesystemStore, ReadOnlySurfaceError, Store

# One-directional edge, real (not deferred): athenaeum.store (L0/L1) imports
# NOTHING from this module (see its module docstring's "Layering" note), so
# this module importing athenaeum.store here carries no cycle —
# tests/test_import_graph_acyclic.py's zero-tolerance SCC guard (baseline
# pinned to `[]` since issue athenaeum#640) would fail loudly if it did.


@dataclass(frozen=True)
class CorpusPolicy:
    """Whether a storage surface participates in each corpus capability.

    The three capabilities are orthogonal:

    * ``embedded`` — pages are indexed into the FTS5 / vector store.
    * ``recallable`` — pages are eligible to be returned by ``recall``.
    * ``merge_eligible`` — pages may be proposed for wiki-dedup consolidation.

    The default surface (``wiki``) is all-true; the ``excluded`` surface is
    all-false. A partially-specified custom adapter FAILS CLOSED — an omitted
    capability defaults to ``False`` (see :func:`_policy_from_mapping`) so a
    typo can never accidentally leak a surface into recall.
    """

    embedded: bool
    recallable: bool
    merge_eligible: bool

    @classmethod
    def all(cls) -> "CorpusPolicy":
        """Full corpus participation — the default wiki surface."""
        return cls(embedded=True, recallable=True, merge_eligible=True)

    @classmethod
    def none(cls) -> "CorpusPolicy":
        """No corpus participation — the excluded surface (athenaeum#427)."""
        return cls(embedded=False, recallable=False, merge_eligible=False)

    @property
    def in_corpus(self) -> bool:
        """True when the surface participates in ANY corpus capability."""
        return self.embedded or self.recallable or self.merge_eligible


@dataclass(frozen=True)
class StorageAdapter:
    """A resolved storage surface: where a class lives + its corpus policy.

    ``surface_root`` is a path relative to the knowledge root (``"wiki"`` for
    the default surface, ``"excluded"`` for the built-in excluded surface). An
    absolute value is honored verbatim, so an operator can point a surface at a
    location entirely outside the knowledge tree.
    """

    name: str
    backing_store: str
    surface_root: str
    corpus_policy: CorpusPolicy
    #: Whether a delete on this surface is a true erasure (design note §4.5
    #: R2, athenaeum#2615). Defaults to ``True`` — what every surface has
    #: always been. ``purgeable: false`` in config declares the surface
    #: non-purgeable, so a destructive operation on it is refused unless the
    #: surface is also versioned (inside a git working tree).
    purgeable: bool = True
    #: ``read_only: true`` in config declares the surface a mount this install
    #: pulls and indexes but never writes (athenaeum#2621). Every write path
    #: refuses it with :class:`ReadOnlySurfaceError`. Default ``False``.
    read_only: bool = False
    #: How widely the pages on this surface are shared (athenaeum#2619):
    #: higher = more widely shared. ``0`` (the default, and the built-in wiki
    #: surface's fixed rank) is the most private. A page may not link to a
    #: page on a LOWER-ranked (more private) surface; an unranked deployment
    #: has every surface at 0, so nothing is refused.
    visibility: int = 0
    #: ``sync_mode: fast_forward`` in config narrows a store that declares
    #: ``integrate`` so this install never merges (athenaeum#2935, design note
    #: §3.3). ``integrate`` here can never widen: a store that does not declare
    #: it ignores the local value and reports that. ``None`` (default) = follow
    #: the store's declaration.
    sync_mode: str | None = None
    #: ``store_id`` in config names the store this entry mounts (access S4,
    #: design note §5). When set, a fetched declaration carrying a different id
    #: refuses the mount. ``None`` (default) = no id pinned, nothing compared.
    store_id: str | None = None
    #: ``opaque_filenames: true`` in config makes every NEW page written to this
    #: surface carry a uid-only file name (``<uid>.md``) instead of
    #: ``<uid>-<slug-of-name>.md``, so a listing of the store does not reveal whose
    #: records it holds (athenaeum#3077). Pages already on disk keep their names.
    #: Default ``False`` (today's names).
    opaque_filenames: bool = False
    #: ``sensitive: true`` in config marks the location a vault: a read of a
    #: value held here returns a redaction marker unless the caller sets the
    #: read flag (athenaeum#3161). It is a separate place with a read gate, NOT
    #: encryption. A sensitive location is never in the main index, so every
    #: ``corpus_policy`` capability must be false. Default ``False``; the
    #: built-in :data:`EXCLUDED` location is sensitive.
    sensitive: bool = False
    #: ``history: git | none`` in config overrides ``storage.history`` for this
    #: one location (athenaeum#3160), so a location that is its own git
    #: repository keeps its own history while the rest of the installation has
    #: none, or the reverse. ``None`` (default) = follow the installation.
    history: str | None = None

    def resolve_root(self, knowledge_root: Path) -> Path:
        """Absolute on-disk root for this surface under *knowledge_root*."""
        candidate = Path(self.surface_root).expanduser()
        if candidate.is_absolute():
            return candidate
        return knowledge_root / candidate


# ---------------------------------------------------------------------------
# Built-in adapters
# ---------------------------------------------------------------------------

#: The default adapter every entity class maps to absent explicit config.
#: Backed by the flat ``wiki/`` markdown tree, full corpus participation —
#: expresses today's behavior so an unconfigured base is byte-identical.
DEFAULT_ADAPTER_NAME = "wiki-markdown-embedded"

WIKI_MARKDOWN_EMBEDDED = StorageAdapter(
    name=DEFAULT_ADAPTER_NAME,
    backing_store="wiki-markdown",
    surface_root="wiki",
    corpus_policy=CorpusPolicy.all(),
)

#: The built-in excluded surface athenaeum#427's PII / archival-contact pages consume.
#: Lives OUTSIDE ``wiki/`` (so it is excluded by construction) with no corpus
#: participation.
EXCLUDED = StorageAdapter(
    name="excluded",
    backing_store="markdown",
    surface_root="excluded",
    corpus_policy=CorpusPolicy.none(),
    sensitive=True,
)

_BUILTIN_ADAPTERS: dict[str, StorageAdapter] = {
    WIKI_MARKDOWN_EMBEDDED.name: WIKI_MARKDOWN_EMBEDDED,
    EXCLUDED.name: EXCLUDED,
}

#: Code-registered custom adapters (the in-process extension point). Separate
#: from builtins so :func:`register_adapter` can never clobber the default
#: surface, and from config-defined adapters so precedence stays legible.
_REGISTERED_ADAPTERS: dict[str, StorageAdapter] = {}


def _require_vault_not_in_corpus(adapter: StorageAdapter) -> None:
    """Refuse a sensitive location with any corpus capability: a vault is never indexed."""
    if adapter.sensitive and adapter.corpus_policy.in_corpus:
        raise StorageConfigError(
            f"storage.adapters.{adapter.name} is sensitive but its corpus_policy "
            "has a capability set to true; a sensitive location is never in the "
            "main index -- set embedded, recallable and merge_eligible to false"
        )


def register_adapter(adapter: StorageAdapter, *, replace: bool = False) -> None:
    """Register a custom storage adapter in-process (the code extension point).

    The complement to the ``storage.adapters`` YAML block: a consumer that
    ships an adapter in code (e.g. athenaeum#426's skill-file-sync surface) registers it
    here so it becomes resolvable by name in ``storage.mapping`` — no change to
    the embed / recall / merge core required.

    A built-in adapter name (``wiki-markdown-embedded``, ``excluded``) can never
    be shadowed. Re-registering a custom name raises unless *replace* is set, so
    two consumers silently colliding on a name is a loud error, not a
    last-write-wins surprise.
    """
    _require_vault_not_in_corpus(adapter)
    if adapter.name in _BUILTIN_ADAPTERS:
        raise StorageConfigError(
            f"cannot register adapter {adapter.name!r}: it shadows a built-in adapter"
        )
    if adapter.name in _REGISTERED_ADAPTERS and not replace:
        raise StorageConfigError(
            f"adapter {adapter.name!r} is already registered "
            "(pass replace=True to override)"
        )
    _REGISTERED_ADAPTERS[adapter.name] = adapter


def _coerce_bool(value: Any, *, default: bool) -> bool:
    """Coerce a YAML policy value to bool, falling back to *default*.

    Only real ``bool`` values (and the literal strings ``true``/``false``,
    since YAML sometimes hands back strings for quoted values) are honored;
    anything else uses *default* — the fail-closed ``False`` for an omitted
    capability. ``bool`` is deliberately NOT derived from arbitrary truthiness
    (``1``/``"yes"``) so a malformed value never accidentally opens a surface.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low == "true":
            return True
        if low == "false":
            return False
    return default


def _policy_from_mapping(raw: Any) -> CorpusPolicy:
    """Build a :class:`CorpusPolicy` from a config ``corpus_policy`` mapping.

    FAIL-CLOSED: each capability defaults to ``False`` when omitted or
    malformed. A custom surface participates in the corpus only where it
    explicitly opts in — a half-written policy excludes, it does not leak.
    """
    if not isinstance(raw, dict):
        return CorpusPolicy.none()
    return CorpusPolicy(
        embedded=_coerce_bool(raw.get("embedded"), default=False),
        recallable=_coerce_bool(raw.get("recallable"), default=False),
        merge_eligible=_coerce_bool(raw.get("merge_eligible"), default=False),
    )


def _visibility_from_config(name: str, raw: Any) -> int:
    """The ``visibility`` rank of one adapter entry: absent -> 0, else a non-negative int."""
    if raw is None:
        return 0
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise StorageConfigError(
            f"storage.adapters.{name}.visibility must be a non-negative integer, "
            f"got {raw!r}"
        )
    return raw


# ---------------------------------------------------------------------------
# Store implementation registry (athenaeum#3165)
# ---------------------------------------------------------------------------

#: The ``backing_store`` names that resolve to the filesystem implementation.
#: They cannot be shadowed by :func:`register_store_backend`.
BUILTIN_BACKING_STORES: tuple[str, ...] = ("markdown", "wiki-markdown")


@dataclass(frozen=True)
class StoreBuildSpec:
    """Everything :func:`open_store` hands a registered store factory.

    One spec describes ONE store instance that addresses every location this
    configuration makes available. *roots* maps surface name to resolved root
    directory; implementations with no filesystem backing may ignore it.
    """

    config: dict[str, Any] | None
    knowledge_root: Path
    roots: Mapping[str, Path]
    non_purgeable_surfaces: frozenset[str]
    read_only_surfaces: frozenset[str]
    history: str
    surface_history: Mapping[str, str]
    write_gate: Callable[[str], str | None]


StoreFactory = Callable[[StoreBuildSpec], Store]

#: Code-registered implementations, keyed by ``backing_store`` name. The
#: built-in filesystem names are deliberately absent: they are handled inside
#: :func:`open_store`, the one place ``FilesystemStore`` is constructed.
_STORE_BACKENDS: dict[str, StoreFactory] = {}


def register_store_backend(name: str, factory: StoreFactory) -> None:
    """Register a store implementation under the ``backing_store`` value *name*.

    *factory* receives a :class:`StoreBuildSpec` and returns a
    :class:`~athenaeum.store.Store`. Adapters whose ``backing_store`` is *name*
    then share the one instance :func:`open_store` builds through it. The
    built-in names (``markdown``, ``wiki-markdown``) cannot be shadowed, and a
    name already registered raises rather than being silently replaced.
    """
    if not isinstance(name, str) or not name.strip():
        raise StorageConfigError("store backend name must be a non-empty string")
    name = name.strip()
    if name in BUILTIN_BACKING_STORES:
        raise StorageConfigError(
            f"cannot register store backend {name!r}: it shadows a built-in "
            "filesystem implementation"
        )
    if name in _STORE_BACKENDS:
        raise StorageConfigError(f"store backend {name!r} is already registered")
    _STORE_BACKENDS[name] = factory


def unregister_store_backend(name: str) -> None:
    """Remove a registration made by :func:`register_store_backend` (no-op if absent)."""
    _STORE_BACKENDS.pop(name, None)


def known_backing_stores() -> tuple[str, ...]:
    """Every ``backing_store`` value that currently resolves, built-ins first."""
    return BUILTIN_BACKING_STORES + tuple(sorted(_STORE_BACKENDS))


def _validate_backing_store(value: str, key: str) -> str:
    """*value* (stripped) when it names a known implementation, else a loud error."""
    value = value.strip()
    if value not in BUILTIN_BACKING_STORES and value not in _STORE_BACKENDS:
        known = ", ".join(repr(n) for n in known_backing_stores())
        raise StorageConfigError(
            f"{key} is {value!r}, which is not a known store implementation. "
            f"Known values: {known}. Write 'markdown' for files on a filesystem."
        )
    return value


def _validate_history(raw: Any, key: str) -> str:
    """*raw* when it is a valid history value (``git`` / ``none``), else a loud error."""
    if not isinstance(raw, str) or raw.strip() not in HISTORY_VALUES:
        raise StorageConfigError(f"{key} must be 'git' or 'none', got {raw!r}")
    return raw.strip()


def _adapter_from_config(name: str, raw: Any) -> StorageAdapter:
    """Build a :class:`StorageAdapter` from one ``storage.adapters`` entry."""
    if not isinstance(raw, dict):
        raise StorageConfigError(
            f"storage.adapters.{name} must be a mapping, got {type(raw).__name__}"
        )
    backing_store = raw.get("backing_store")
    surface_root = raw.get("surface_root")
    if not isinstance(backing_store, str) or not backing_store.strip():
        raise StorageConfigError(
            f"storage.adapters.{name}.backing_store must be a non-empty string"
        )
    backing_store = _validate_backing_store(
        backing_store, f"storage.adapters.{name}.backing_store"
    )
    if not isinstance(surface_root, str) or not surface_root.strip():
        raise StorageConfigError(
            f"storage.adapters.{name}.surface_root must be a non-empty string"
        )
    read_only = raw.get("read_only", False)
    if not isinstance(read_only, bool):
        raise StorageConfigError(
            f"storage.adapters.{name}.read_only must be a boolean (true/false), "
            f"got {read_only!r}"
        )
    sync_mode = raw.get("sync_mode")
    if sync_mode is not None and sync_mode not in ("fast_forward", "integrate"):
        raise StorageConfigError(
            f"storage.adapters.{name}.sync_mode must be 'fast_forward' or 'integrate', "
            f"got {sync_mode!r}"
        )
    store_id = raw.get("store_id")
    if store_id is not None and (not isinstance(store_id, str) or not store_id.strip()):
        raise StorageConfigError(
            f"storage.adapters.{name}.store_id must be a non-empty string, got {store_id!r}"
        )
    opaque_filenames = raw.get("opaque_filenames", False)
    if not isinstance(opaque_filenames, bool):
        raise StorageConfigError(
            f"storage.adapters.{name}.opaque_filenames must be a boolean (true/false), "
            f"got {opaque_filenames!r}"
        )
    sensitive = raw.get("sensitive", False)
    if not isinstance(sensitive, bool):
        raise StorageConfigError(
            f"storage.adapters.{name}.sensitive must be a boolean (true/false), "
            f"got {sensitive!r}"
        )
    history = raw.get("history")
    if history is not None:
        history = _validate_history(history, f"storage.adapters.{name}.history")
    adapter = StorageAdapter(
        name=name,
        read_only=read_only,
        history=history,
        opaque_filenames=opaque_filenames,
        sensitive=sensitive,
        sync_mode=sync_mode,
        store_id=store_id.strip() if store_id else None,
        backing_store=backing_store.strip(),
        surface_root=surface_root.strip(),
        corpus_policy=_policy_from_mapping(raw.get("corpus_policy")),
        purgeable=_coerce_bool(raw.get("purgeable"), default=True),
        visibility=_visibility_from_config(name, raw.get("visibility")),
    )
    _require_vault_not_in_corpus(adapter)
    return adapter


def available_adapters(config: dict[str, Any] | None) -> dict[str, StorageAdapter]:
    """Resolve every adapter available to this config, keyed by name.

    Precedence, lowest to highest: built-ins, then code-:func:`register_adapter`
    entries, then ``storage.adapters`` YAML definitions. A config or code
    adapter that reuses a BUILT-IN name raises :class:`StorageConfigError` — the
    default surfaces are protected so config can never silently redefine what
    ``wiki-markdown-embedded`` means.
    """
    adapters: dict[str, StorageAdapter] = dict(_BUILTIN_ADAPTERS)
    for name, adapter in _REGISTERED_ADAPTERS.items():
        adapters[name] = adapter
    for name, raw in resolve_storage_adapters(config).items():
        if name in _BUILTIN_ADAPTERS:
            raise StorageConfigError(
                f"storage.adapters.{name} shadows a built-in adapter — "
                "pick a different name"
            )
        adapters[name] = _adapter_from_config(name, raw)
    return adapters


def resolve_adapter_for_class(
    entity_class: str | None,
    config: dict[str, Any] | None,
) -> StorageAdapter:
    """Resolve the storage adapter for *entity_class* — the layer's entry point.

    Reads the ``storage.mapping`` (entity-class → adapter-name) table; a class
    with no explicit mapping (including ``None`` / empty) resolves to the
    default :data:`WIKI_MARKDOWN_EMBEDDED` surface, so default behavior is
    byte-identical. A mapping that names an unknown adapter raises
    :class:`StorageConfigError` (loud — never a silent fallback that could route
    an excluded class into the corpus).
    """
    cls = (entity_class or "").strip()
    mapping = _full_storage_mapping(config)
    adapter_name = mapping.get(cls, DEFAULT_ADAPTER_NAME) if cls else DEFAULT_ADAPTER_NAME
    adapters = available_adapters(config)
    adapter = adapters.get(adapter_name)
    if adapter is None:
        raise StorageConfigError(
            f"storage.mapping routes class {cls!r} to unknown adapter "
            f"{adapter_name!r}; known adapters: {sorted(adapters)}"
        )
    return adapter


def surface_visibility(name: str, config: dict[str, Any] | None) -> int:
    """The visibility rank of the surface called *name* (athenaeum#2619).

    *name* is a surface name as :class:`~athenaeum.models.PageLocator` reports
    it: ``"wiki"`` for the default surface (an alias of
    :data:`DEFAULT_ADAPTER_NAME`) or an adapter name. The built-in wiki surface
    is rank 0, the most private. An adapter that declares no ``visibility``,
    and a name that matches no adapter, are also 0 -- so an unranked
    deployment is judged identically everywhere and refuses nothing.
    """
    adapter = available_adapters(config).get(
        DEFAULT_ADAPTER_NAME if name == "wiki" else name
    )
    return adapter.visibility if adapter is not None else 0


# ---------------------------------------------------------------------------
# Consumer / writer convenience predicates
# ---------------------------------------------------------------------------


def storage_policy_configured(config: dict[str, Any] | None) -> bool:
    """Whether this config defines any non-default storage policy (issue athenaeum#532).

    ``True`` only when ``storage.mapping`` or ``storage.adapters`` is present and
    non-empty — i.e. when some entity class could resolve to a surface other than
    the all-true default ``wiki-markdown-embedded``. When ``False``, every class
    is guaranteed full corpus participation, so the ``embedded`` / ``recallable``
    enforcement gates (index-build scan, recall render) can short-circuit to
    their pre-athenaeum#532 behavior as a strict no-op. Consulted at the recall render
    layer so a base with no ``storage:`` config pays nothing and behaves exactly
    as before — including for edge cases like an unreadable (stale-index) hit.
    """
    if not config:
        return False
    return bool(
        resolve_storage_mapping(config)
        or resolve_storage_adapters(config)
        or resolve_storage_routes(config)
    )


def corpus_policy_for_class(
    entity_class: str | None,
    config: dict[str, Any] | None,
) -> CorpusPolicy:
    """Corpus policy for *entity_class* (default surface → all-true)."""
    return resolve_adapter_for_class(entity_class, config).corpus_policy


def is_embedded(entity_class: str | None, config: dict[str, Any] | None) -> bool:
    """Whether pages of *entity_class* are embedded into the index."""
    return corpus_policy_for_class(entity_class, config).embedded


def page_embedding_disabled(meta: Mapping[str, Any] | None) -> bool:
    """Whether *meta* carries a page-level ``embedded: false`` override (issue athenaeum#716).

    This is the per-page counterpart to :func:`is_embedded`'s class-level
    corpus policy. The shipped mechanism resolved ``embedded`` on entity
    class + config ONLY — there was no page-level read of an ``embedded:``
    frontmatter key anywhere on the read/index path before this issue (an
    athenaeum#716 external design review finding). Negative-only BY
    CONSTRUCTION: only the literal bool ``False`` triggers an override;
    ``embedded: true``, any other value, or the key's absence all return
    ``False`` here — see :func:`page_is_embedded` for why a page can never
    use this to force itself INTO an index its class is excluded from.
    General-purpose, not fold-specific: a wiki-dedup tombstone
    (:func:`athenaeum.models.stamp_tombstone`) is the first consumer, but
    athenaeum#718's cold-tier demotion is meant to reuse this same seam.
    """
    return isinstance(meta, Mapping) and meta.get("embedded") is False


def page_is_embedded(
    meta: Mapping[str, Any] | None,
    entity_class: str | None,
    config: dict[str, Any] | None,
) -> bool:
    """Final per-page embed decision: class policy AND the page override.

    ``True`` only when the class-level policy (:func:`is_embedded`) allows it
    AND the page itself has not opted out via ``embedded: false``
    (:func:`page_embedding_disabled`). There is no code path here that can
    turn a class-level ``False`` into ``True`` — a page cannot set
    ``embedded: true`` to escape an ``excluded`` adapter, or any class whose
    corpus policy already says no, by setting its own frontmatter (issue
    athenaeum#716 external review: "note the asymmetry deliberately").
    """
    if page_embedding_disabled(meta):
        return False
    return is_embedded(entity_class, config)


def is_recallable(entity_class: str | None, config: dict[str, Any] | None) -> bool:
    """Whether pages of *entity_class* are eligible for recall."""
    return corpus_policy_for_class(entity_class, config).recallable


def is_merge_eligible(entity_class: str | None, config: dict[str, Any] | None) -> bool:
    """Whether pages of *entity_class* may be proposed for wiki-dedup merge."""
    return corpus_policy_for_class(entity_class, config).merge_eligible


def is_excluded(entity_class: str | None, config: dict[str, Any] | None) -> bool:
    """Whether *entity_class* is fully outside the corpus (no capability)."""
    return not corpus_policy_for_class(entity_class, config).in_corpus


def is_sensitive(location: str, config: dict[str, Any] | None) -> bool:
    """Whether the storage *location* (adapter name) is marked sensitive.

    THE one predicate for "is this location a vault" (athenaeum#3161);
    :attr:`RouteDecision.sensitive` is computed from it.

    Raises:
        StorageConfigError: *location* is not a known adapter.
    """
    adapters = available_adapters(config)
    if location not in adapters:
        raise StorageConfigError(
            f"unknown storage location {location!r}; known locations: {sorted(adapters)}"
        )
    return adapters[location].sensitive


# ---------------------------------------------------------------------------
# The one resolver for "where is this field / page type stored" (athenaeum#3158)
# ---------------------------------------------------------------------------
#
# Four configuration tables answer that question: ``storage.excluded_fields``
# with ``storage.excluded_read_mapping`` (which fields of a record are kept off
# the page), ``storage.mapping`` (which class lives outside the corpus),
# and the free-text value-routing class rule in ``sensitivity_routing``
# (athenaeum#3162 removed the fourth, the corrections-only
# ``librarian.corrections.sensitive_fields`` table; corrections route through
# ``route_for(page_type, field)`` like every other writer). This section is the
# ONLY module that calls the config resolvers ``resolve_excluded_fields_config``
# and ``resolve_excluded_read_mapping``;
# ``tests/test_route_for.py`` fails the build if another module does.


@dataclass(frozen=True)
class RouteDecision:
    """Where a field or a whole page type is stored, as :func:`route_for` decides it.

    Attributes:
        targets: Ordered location (adapter) names the value is stored in. Empty
            when a single field is kept on the page, or when a rule discards it
            (see :attr:`discarded`).
        rule: The configuration entry that decided (for example
            ``"storage.mapping.pii"``; ``"storage.routes[2]"`` for a rule with
            ``to: []``), or ``None`` when no entry did and a default applied.
        sensitive: True when a target is a sensitive location
            (:func:`is_sensitive`) -- a vault with a read gate. A location that
            is merely outside the corpus (an archive) is not sensitive.
    """

    targets: tuple[str, ...]
    rule: str | None
    sensitive: bool

    @property
    def discarded(self) -> bool:
        """True when a rule with ``to: []`` decided: the value is stored nowhere.

        Distinct from "kept on the page", which is ``targets == ()`` with
        ``rule is None`` (no rule matched). A discard names its rule.
        """
        return not self.targets and self.rule is not None


def configured_excluded_read_mapping(config: dict[str, Any] | None) -> dict[str, str]:
    """The operator's ``storage.excluded_read_mapping`` -- the resolver's input.

    A pass-through so :func:`athenaeum.pii.surface_class_for_page_class` reads
    the table through this module rather than from ``config`` directly.
    """
    return {**resolve_excluded_read_mapping(config), **_route_tables(config)[2]}


def configured_excluded_fields(config: dict[str, Any] | None) -> dict[str, tuple[str, ...]]:
    """The operator's ``storage.excluded_fields`` -- the resolver's input.

    A pass-through so :func:`athenaeum.pii.resolve_excluded_fields` reads the
    table through this module rather than from ``config`` directly.
    """
    return {**resolve_excluded_fields_config(config), **_route_tables(config)[1]}


# ---------------------------------------------------------------------------
# storage.routes -- one rule per "this goes there" (athenaeum#3166)
# ---------------------------------------------------------------------------
#
# A rule names a page type (optionally a list of its fields) or a sensitivity
# class, and one target location. ``storage.mapping`` stays as the short form of
# a whole-page-type rule. The rules are folded into the same tables the older
# keys fill (the page-type mapping, ``storage.excluded_fields`` and
# ``storage.excluded_read_mapping``) so a configuration written with routes and
# its equivalent written with the old keys take the same read and write paths.

#: Prefix of the surface class a page type's field rules are folded into. A
#: surface class is the name the older tables key a record collection by; a
#: field rule has no such name, so it gets a derived one that no page type or
#: operator-chosen class can collide with in practice.
_ROUTE_SURFACE_PREFIX = "routes."


@dataclass(frozen=True)
class StorageRoute:
    """One validated ``storage.routes`` rule.

    Exactly one of ``page_type`` / ``sensitivity_class`` is set. ``fields`` is
    ``None`` for a whole-page-type rule. ``to`` holds the target location, or
    nothing for a rule that discards (``to: []``, athenaeum#3168). Several names
    (athenaeum#3169) are vaults the value is written to in order; the first is the
    primary. ``index`` is the rule's position in ``storage.routes``.
    """

    to: tuple[str, ...]
    page_type: str | None = None
    sensitivity_class: str | None = None
    fields: tuple[str, ...] | None = None
    index: int = 0

    @property
    def discard(self) -> bool:
        return not self.to

    @property
    def rule_name(self) -> str:
        """The name a discard decision and its counter carry."""
        return f"storage.routes[{self.index}]"


def _route_error(index: int, message: str) -> StorageConfigError:
    return StorageConfigError(f"storage.routes[{index}] {message}")


def _parse_route(index: int, raw: Any) -> StorageRoute:
    if not isinstance(raw, dict):
        raise _route_error(index, f"must be a mapping, got {type(raw).__name__}")
    known = ("page_type", "sensitivity_class", "fields", "to")
    unknown = sorted(str(k) for k in raw if k not in known)
    if unknown:
        raise _route_error(
            index,
            f"has unknown key(s) {unknown}; a rule takes page_type (or "
            "sensitivity_class), fields and to",
        )
    page_type = raw.get("page_type")
    sens_class = raw.get("sensitivity_class")
    if (page_type is None) == (sens_class is None):
        raise _route_error(index, "must name exactly one of page_type or sensitivity_class")
    key_name = "page_type" if page_type is not None else "sensitivity_class"
    key_value = page_type if page_type is not None else sens_class
    if not isinstance(key_value, str) or not key_value.strip():
        raise _route_error(index, f"{key_name} must be a non-empty string, got {key_value!r}")
    to = raw.get("to")
    if not isinstance(to, list) or not all(isinstance(t, str) and t.strip() for t in to):
        raise _route_error(index, f"to must be a list of location names, got {to!r}")
    # The one place the number of targets is read. An empty list discards; two
    # or more name several vaults (athenaeum#3169), checked in configured_routes
    # where the adapters are known.
    fields: tuple[str, ...] | None = None
    if "fields" in raw:
        if sens_class is not None:
            raise _route_error(index, "a sensitivity_class rule takes no fields")
        raw_fields = raw["fields"]
        if (
            not isinstance(raw_fields, list)
            or not raw_fields
            or not all(isinstance(f, str) and f.strip() for f in raw_fields)
        ):
            raise _route_error(
                index, f"fields must be a non-empty list of names, got {raw_fields!r}"
            )
        fields = tuple(f.strip() for f in raw_fields)
    return StorageRoute(
        to=tuple(t.strip() for t in to),
        page_type=key_value.strip() if page_type is not None else None,
        sensitivity_class=key_value.strip() if sens_class is not None else None,
        fields=fields,
        index=index,
    )


def _check_multi_target(route: StorageRoute, adapters: Mapping[str, StorageAdapter]) -> None:
    """Refuse a ``to`` list of several names unless every one is a vault (athenaeum#3169).

    A vault here is :func:`is_sensitive`'s notion: ``sensitive: true``, which
    already requires every corpus capability off. A location in the index is
    refused by name: two writable indexed copies of a page have no rule for which
    one wins. A ``read_only`` target can never accept the write.
    """
    subject = route.page_type or route.sensitivity_class
    if len(set(route.to)) != len(route.to):
        raise StorageConfigError(
            f"storage.routes rule for {subject!r} names a location more than once in `to`: "
            f"{list(route.to)}"
        )
    for name in route.to:
        adapter = adapters[name]
        if adapter.corpus_policy.in_corpus:
            raise StorageConfigError(
                f"storage.routes rule for {subject!r} lists location {name!r}, which is in "
                "the index, among several targets; a list of several targets takes vaults "
                "only (two writable indexed copies of a page have no rule for which one wins)"
            )
        if not adapter.sensitive:
            raise StorageConfigError(
                f"storage.routes rule for {subject!r} lists location {name!r}, which is not "
                "a vault (sensitive: true), among several targets; every target of a list "
                "of several must be a vault"
            )
        if adapter.read_only:
            raise StorageConfigError(
                f"storage.routes rule for {subject!r} lists location {name!r}, which is "
                "read_only, as a target; a read-only location can never accept the write"
            )


def configured_routes(config: dict[str, Any] | None) -> tuple[StorageRoute, ...]:
    """The validated ``storage.routes`` rules (athenaeum#3166); empty when unset.

    A rule with ``to: []`` discards: what it names is stored nowhere
    (athenaeum#3168). Raises :class:`StorageConfigError` for: a malformed rule; a
    ``to`` list of several names unless every one is a vault (``sensitive: true``,
    outside the corpus, not ``read_only``, no name repeated; athenaeum#3169); two rules
    naming the same page type and field, the same whole page type, or the same sensitivity
    class (there is no ordering between rules); a page type named both in ``storage.routes`` and in
    ``storage.mapping``; a field rule on a page type that also has a
    ``storage.excluded_read_mapping`` or ``storage.excluded_fields`` entry; a
    ``to`` that is not a known location; a field rule whose target is not
    ``sensitive`` (:func:`is_sensitive`); and field rules of one page type that
    name different locations.
    """
    raw = resolve_storage_routes(config)
    if not raw:
        return ()
    if not isinstance(raw, list):
        raise StorageConfigError(
            f"storage.routes must be a list of rules, got {type(raw).__name__}"
        )
    routes = tuple(_parse_route(i, item) for i, item in enumerate(raw))

    seen: set[tuple[str, str, str | None]] = set()
    for route in routes:
        kind = "page_type" if route.page_type is not None else "sensitivity_class"
        subject = route.page_type if route.page_type is not None else route.sensitivity_class
        assert subject is not None
        for name in route.fields if route.fields is not None else (None,):
            key = (kind, subject, name)
            if key in seen:
                where = f"{kind} {subject!r}" + (f" field {name!r}" if name else "")
                raise StorageConfigError(
                    f"storage.routes names {where} more than once; there is no ordering "
                    "between rules, so each page type (and field) takes one rule"
                )
            seen.add(key)

    mapping = resolve_storage_mapping(config)
    read_mapping = resolve_excluded_read_mapping(config)
    excluded_fields = resolve_excluded_fields_config(config)
    field_target: dict[str, tuple[str, ...]] = {}
    adapters = available_adapters(config)
    for route in routes:
        for target in route.to:
            if target not in adapters:
                raise StorageConfigError(
                    f"storage.routes routes {route.page_type or route.sensitivity_class!r} to "
                    f"unknown location {target!r}; known locations: {sorted(adapters)}"
                )
        if len(route.to) > 1:
            _check_multi_target(route, adapters)
        if route.page_type is None:
            continue
        if route.page_type in mapping:
            raise StorageConfigError(
                f"page type {route.page_type!r} is named in both storage.routes and "
                "storage.mapping; keep one"
            )
        if route.fields is None or route.discard:
            continue
        for target in route.to:
            if not adapters[target].sensitive:
                raise StorageConfigError(
                    f"storage.routes sends fields of {route.page_type!r} to location "
                    f"{target!r}, which is not marked sensitive: true; a field rule "
                    "needs a sensitive target"
                )
        if field_target.setdefault(route.page_type, route.to) != route.to:
            raise StorageConfigError(
                f"storage.routes sends fields of {route.page_type!r} to more than one "
                "location; a page type's field rules share one target"
            )
        old_surface = _pii_policy().surface_class_for_page_class(route.page_type, None)
        if route.page_type in read_mapping or route.page_type in excluded_fields:
            clash = "storage.excluded_read_mapping" if route.page_type in read_mapping else (
                "storage.excluded_fields"
            )
            raise StorageConfigError(
                f"page type {route.page_type!r} is named in both storage.routes and "
                f"{clash}; keep one"
            )
        if old_surface in excluded_fields:
            raise StorageConfigError(
                f"page type {route.page_type!r} has field rules in storage.routes and its "
                f"surface class {old_surface!r} is listed in storage.excluded_fields; keep one"
            )
    return routes


def _route_surface_class(page_type: str) -> str:
    return f"{_ROUTE_SURFACE_PREFIX}{page_type}"


def _route_tables(
    config: dict[str, Any] | None,
) -> tuple[dict[str, str], dict[str, tuple[str, ...]], dict[str, str]]:
    """What the routes add to (mapping, excluded fields, excluded read mapping)."""
    mapping: dict[str, str] = {}
    fields: dict[str, tuple[str, ...]] = {}
    read_mapping: dict[str, str] = {}
    for route in configured_routes(config):
        if route.page_type is None or route.discard:
            continue
        if route.fields is None:
            mapping[route.page_type] = route.to[0]
            continue
        surface = _route_surface_class(route.page_type)
        mapping[surface] = route.to[0]
        fields[surface] = fields.get(surface, ()) + route.fields
        read_mapping[route.page_type] = surface
    return mapping, fields, read_mapping


def _full_storage_mapping(config: dict[str, Any] | None) -> dict[str, str]:
    """``storage.mapping`` plus what the routes fold in (derived surface classes included)."""
    return {**resolve_storage_mapping(config), **_route_tables(config)[0]}


def routed_storage_mapping(config: dict[str, Any] | None) -> dict[str, str]:
    """Class -> location for every page type and sensitivity class that is routed whole.

    ``storage.mapping`` entries plus the ``storage.routes`` rules without
    ``fields`` and the ``sensitivity_class`` rules (a rule with ``to: []``
    names no location and is left out). For the readers that report
    on where a class goes (doctor, the sensitivity lint, ``storage move-type``);
    the routing itself goes through :func:`route_for`.
    """
    merged = dict(resolve_storage_mapping(config))
    for route in configured_routes(config):
        if route.fields is None and not route.discard:
            name = route.page_type if route.page_type is not None else route.sensitivity_class
            assert name is not None
            merged[name] = route.to[0]
    return merged


def route_for_sensitivity_class(
    sensitivity_class: str, config: dict[str, Any] | None
) -> RouteDecision | None:
    """The ``storage.routes`` rule for a sensitivity class, or ``None`` when none names it.

    ``None`` means "no rule": the caller keeps its own fallback, so a class with
    no rule behaves as it did before ``storage.routes`` existed.
    """
    for route in configured_routes(config):
        if route.sensitivity_class == sensitivity_class:
            if route.discard:
                return RouteDecision((), route.rule_name, False)
            return _decision_for_adapter_names(
                route.to, f"storage.routes.sensitivity_class.{sensitivity_class}", config
            )
    return None


def _discard_rules(
    config: dict[str, Any] | None,
) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """``(page types discarded whole, page type -> {field -> rule})`` from ``to: []`` rules."""
    whole: dict[str, str] = {}
    fields: dict[str, dict[str, str]] = {}
    for route in configured_routes(config):
        if not route.discard or route.page_type is None:
            continue
        if route.fields is None:
            whole[route.page_type] = route.rule_name
        else:
            for name in route.fields:
                fields.setdefault(route.page_type, {})[name] = route.rule_name
    return whole, fields


def discard_rule_for_field_any_type(field: Any, config: dict[str, Any] | None) -> str | None:
    """The first ``to: []`` rule that names *field* on any page type, else ``None``.

    For a writer that has not yet resolved which page type a value belongs to
    and must not let a value some rule discards reach a ledger or a handoff note
    in the meantime. Not a routing answer: :func:`route_for` is.
    """
    if not isinstance(field, str):
        return None
    try:
        discarded_fields = _discard_rules(config)[1]
    except StorageConfigError:
        # A bad table is reported where the write asks route_for; this probe
        # must not raise from a writer that promises not to.
        return None
    for fields in discarded_fields.values():
        if field in fields:
            return fields[field]
    return None


def discarded_page_types(config: dict[str, Any] | None) -> dict[str, str]:
    """Page type -> rule name for every page type a ``to: []`` rule discards whole.

    The capture gates feed this to the intake decision (athenaeum#3168): a page
    type listed here is not captured.
    """
    return dict(_discard_rules(config)[0])


# Per-rule count of values discarded this run (athenaeum#3168). Rule names only:
# never a value, and never anything computed from one.
_DISCARD_COUNTS: dict[str, int] = {}
_DISCARD_LOCK = threading.Lock()


def note_discard(rule: str | None) -> None:
    """Count one discarded value against *rule* (a rule name, never a value)."""
    if not rule:
        return
    with _DISCARD_LOCK:
        _DISCARD_COUNTS[rule] = _DISCARD_COUNTS.get(rule, 0) + 1


def discard_counts() -> dict[str, int]:
    """A snapshot of the per-rule discard counters, for the run summary."""
    with _DISCARD_LOCK:
        return dict(sorted(_DISCARD_COUNTS.items()))


def reset_discard_counts() -> None:
    """Zero the counters (the start of a run)."""
    with _DISCARD_LOCK:
        _DISCARD_COUNTS.clear()



# ---------------------------------------------------------------------------
# Several targets (athenaeum#3169)
# ---------------------------------------------------------------------------
#
# A rule whose ``to`` names several vaults has them written in the same call, in
# order; the first is the primary and is the only one read. No replication
# service: the loop lives at the one place each writer asks the resolver.


class StorageTargetError(RuntimeError):
    """A write, removal or erasure on one target of a multi-target rule failed.

    Names the target and the failure class, never a value. Targets earlier in the
    list may already hold the change; every operation is idempotent per target,
    so re-running completes the rest.
    """

    def __init__(self, target: str, cause: BaseException | str, *, action: str = "write") -> None:
        self.target = target
        detail = cause if isinstance(cause, str) else type(cause).__name__
        super().__init__(
            f"{action} on storage target {target!r} failed ({detail}); targets before it "
            "may already hold the change, re-run to complete the rest"
        )


def apply_to_targets(
    targets: Sequence[str], apply_one: Callable[[str], None], *, action: str = "write"
) -> None:
    """Call *apply_one* for each target in order; the first failure raises, naming it.

    Success means every target accepted the change. Nothing is logged and skipped.
    """
    for name in targets:
        try:
            apply_one(name)
        except StorageTargetError:
            raise
        except Exception as exc:
            raise StorageTargetError(name, exc, action=action) from exc


def target_root(name: str, config: dict[str, Any] | None, knowledge_root: Path) -> Path:
    """Absolute on-disk root of the location called *name*."""
    adapters = available_adapters(config)
    if name not in adapters:
        raise StorageConfigError(
            f"unknown storage location {name!r}; known locations: {sorted(adapters)}"
        )
    return adapters[name].resolve_root(knowledge_root)


def inside_git_repository(path: Path) -> bool:
    """Whether *path* (which need not exist yet) is inside a git working tree."""
    for candidate in (path.resolve(), *path.resolve().parents):
        if (candidate / ".git").exists():
            return True
    return False


def route_target_summary(
    config: dict[str, Any] | None, knowledge_root: Path
) -> list[dict[str, Any]]:
    """One row per ``storage.routes`` rule: its targets and which sit inside git.

    ``in_git`` names the targets inside a git repository, where an erasure does
    not reach history (athenaeum#3169). Read-only; no value is read.
    """
    rows: list[dict[str, Any]] = []
    for route in configured_routes(config):
        rows.append(
            {
                "rule": route.rule_name,
                "subject": route.page_type or route.sensitivity_class,
                "targets": list(route.to),
                "target_count": len(route.to),
                "in_git": [
                    name
                    for name in route.to
                    if inside_git_repository(target_root(name, config, knowledge_root))
                ],
            }
        )
    return rows


def _decision_for_adapter_names(
    names: Sequence[str], rule: str | None, config: dict[str, Any] | None
) -> RouteDecision:
    adapters = available_adapters(config)
    for name in names:
        if name not in adapters:
            raise StorageConfigError(
                f"unknown storage location {name!r}; known locations: {sorted(adapters)}"
            )
    return RouteDecision(
        targets=tuple(names),
        rule=rule,
        sensitive=any(is_sensitive(name, config) for name in names),
    )


def _decision_for_class(
    entity_class: str, rule: str | None, config: dict[str, Any] | None
) -> RouteDecision:
    adapter = resolve_adapter_for_class(entity_class, config)
    # A rule with several vault targets answers with all of them, in order
    # (athenaeum#3169); every other class has the one location it resolved to.
    return _decision_for_adapter_names(
        _multi_targets(entity_class, config) or (adapter.name,), rule, config
    )


def _multi_targets(entity_class: str, config: dict[str, Any] | None) -> tuple[str, ...]:
    """The ordered targets of a ``storage.routes`` rule with several, else ``()``.

    *entity_class* is a page type (a whole-type rule) or the derived surface
    class a page type's field rules were folded into.
    """
    for route in configured_routes(config):
        if len(route.to) < 2 or route.page_type is None:
            continue
        key = route.page_type if route.fields is None else _route_surface_class(route.page_type)
        if key == entity_class:
            return route.to
    return ()


def _pii_policy() -> Any:
    # pii's surface-class and field-list policy (its built-in tables are pii
    # constants) is the input here. pii already imports this module, and the
    # import graph is pinned acyclic (tests/test_import_graph_acyclic.py), so
    # the module is resolved by name at call time instead of by import
    # statement. Moving those tables into the storage layer would remove the
    # indirection.
    return importlib.import_module("athenaeum.pii")


def _excluded_field_names(
    surface_class: str, config: dict[str, Any] | None, record_meta: Mapping[str, Any] | None
) -> tuple[str, ...]:
    return tuple(_pii_policy().resolve_excluded_fields(surface_class, config, record_meta))


def _field_rule(surface_class: str, config: dict[str, Any] | None) -> str:
    if surface_class.startswith(_ROUTE_SURFACE_PREFIX):
        return f"storage.routes.{surface_class[len(_ROUTE_SURFACE_PREFIX):]}"
    if surface_class in configured_excluded_fields(config):
        return f"storage.excluded_fields.{surface_class}"
    return f"storage.mapping.{surface_class}"


def route_for(
    page_type: str | None,
    field: str | None = None,
    *,
    config: dict[str, Any] | None,
    record_meta: Mapping[str, Any] | None = None,
    surface_class: str | None = None,
) -> RouteDecision:
    """The ordered locations a field, or a whole page type, is stored in.

    Pure: reads *config* (and the in-memory *record_meta*) only -- no file
    access, no clock. It computes from the configuration keys that exist today
    and adds none. Three questions, one function:

    * ``route_for(page_type, config=...)`` -- a whole page type. The answer is
      the ``storage.mapping`` entry, as :func:`resolve_adapter_for_class` gives
      it; ``rule`` is ``"storage.mapping.<type>"``, or ``None`` when the type is
      unmapped and the default wiki surface applies.
    * ``route_for(page_type, field, config=...)`` -- one field of a page of that
      type. The surface class comes from
      :func:`athenaeum.pii.surface_class_for_page_class`, the field list from
      :func:`athenaeum.pii.resolve_excluded_fields`, and the class must be
      outside the corpus (:func:`is_excluded`). ``targets`` is empty when the
      value is kept on the page. *record_meta* is only consulted for a class
      with no configured or built-in field list, whose list is the record's own
      frontmatter minus bookkeeping.
    * ``route_for(None, field, config=..., surface_class=...)`` -- the read
      path's form. The excluded-field join receives an ALREADY-RESOLVED surface
      class (never a page type) and its callers decide whether a join should
      happen at all, so this form skips both the page-class mapping and the
      :func:`is_excluded` gate: it answers only "is *field* on this surface
      class's field list", with ``targets`` the class's adapter when it is and
      empty when it is not.
    * ``route_for(None, field, config=...)`` -- no page type and no surface
      class: nothing to route by, so the field is kept on the page.

    **One fallback remains** (the second was removed by athenaeum#3168): the
    FIELD path falls back to the WIKI: a field of a class that is not outside the
    corpus, or not on its field list, is kept on the page (``targets == ()``,
    ``rule is None``). ``sensitivity_routing`` (free-text values) used to fall
    back to the built-in EXCLUDED location for a class with no rule; it now
    refuses such a class at load instead, so there is no value-routing fallback.

    **Discard (athenaeum#3168).** A ``storage.routes`` rule with ``to: []``
    answers with empty ``targets`` AND its ``rule`` set (``discarded`` is
    true): the page type, or the named fields of it, are stored nowhere. This
    is not the "no rule matched" answer, which also has empty ``targets`` but
    ``rule is None``. The read-path form (``surface_class=``) is not affected:
    a discarded value was never stored.

    Raises:
        StorageConfigError: A mapping names a location that is not a known
            adapter.
    """
    if surface_class is not None:
        if field is None or field not in _excluded_field_names(surface_class, config, record_meta):
            return RouteDecision((), None, False)
        return _decision_for_class(surface_class, _field_rule(surface_class, config), config)

    if page_type is None or not page_type.strip():
        return RouteDecision((), None, False)

    # A ``to: []`` rule decides first: its decision has no targets and names the
    # rule (athenaeum#3168), which is what tells it apart from "no rule matched".
    discarded_whole, discarded_fields = _discard_rules(config)
    if page_type.strip() in discarded_whole:
        return RouteDecision((), discarded_whole[page_type.strip()], False)
    if field is not None and field in discarded_fields.get(page_type.strip(), {}):
        return RouteDecision((), discarded_fields[page_type.strip()][field], False)

    if field is None:
        cls = page_type.strip()
        if cls in resolve_storage_mapping(config):
            return _decision_for_class(cls, f"storage.mapping.{cls}", config)
        if cls in _route_tables(config)[0]:
            return _decision_for_class(cls, f"storage.routes.{cls}", config)
        # No mapping entry: the default wiki surface, in the corpus. Answered
        # without resolving the adapter table, exactly as the value-routing
        # path never resolved it for an unmapped class.
        return RouteDecision((DEFAULT_ADAPTER_NAME,), None, False)

    pii = _pii_policy()
    surface_class = pii.surface_class_for_page_class(page_type, config)
    if not surface_class or not is_excluded(surface_class, config):
        return RouteDecision((), None, False)
    if field not in _excluded_field_names(surface_class, config, record_meta):
        return RouteDecision((), None, False)
    return _decision_for_class(surface_class, _field_rule(surface_class, config), config)


def surface_root_for_class(
    entity_class: str | None,
    config: dict[str, Any] | None,
    knowledge_root: Path,
) -> Path:
    """Absolute on-disk root where pages of *entity_class* are persisted.

    The writer-facing entry point athenaeum#427's PII surface consumes: instead of
    hardcoding ``knowledge_root / "contacts"``, a writer asks the layer where a
    class lives and gets the surface root its adapter resolves to (``wiki/`` for
    the default, the configured excluded root for an excluded class).
    """
    return resolve_adapter_for_class(entity_class, config).resolve_root(knowledge_root)


class SurfaceNotIndexedError(StorageConfigError):
    """A compile write was routed to a surface that is not indexed (athenaeum#2618).

    ``storage.mapping`` sends the class to a surface whose corpus policy has
    ``embedded: false`` (e.g. the built-in ``excluded`` surface). The compile
    paths write pages that join the corpus, so they refuse rather than park
    them where recall can never see them; only the excluded-surface writers
    (PII, corrections, storage migration) may write there.
    """


def page_write_root_for_class(
    entity_class: str | None,
    config: dict[str, Any] | None,
    knowledge_root: Path,
) -> Path:
    """Directory a NEW page of *entity_class* is written to (athenaeum#2618).

    The compile write paths (entity creation, batch apply, page recompile)
    call this once per entity instead of assuming ``wiki/``. With no
    ``storage.mapping`` for the class it is the default wiki surface, so an
    unconfigured deployment writes to the same paths as before. Raises
    :class:`SurfaceNotIndexedError` when the class maps to a surface that is
    not indexed, :class:`ReadOnlySurfaceError` when it maps to a surface
    declared ``read_only`` (athenaeum#2621), and :class:`StorageConfigError`
    for an unknown adapter.
    """
    return _write_root_for_adapter(
        resolve_adapter_for_class(entity_class, config), entity_class, config, knowledge_root
    )


def _write_root_for_adapter(
    adapter: StorageAdapter,
    entity_class: str | None,
    config: dict[str, Any] | None,
    knowledge_root: Path,
) -> Path:
    """The write-guard checks shared by the type-derived and store-named routes."""
    if adapter.read_only:
        raise ReadOnlySurfaceError(
            f"entity class {entity_class!r} maps to surface {adapter.name!r}, which is "
            "declared read_only; compile never writes a page there"
        )
    declared = declared_compile_refusal(adapter.name, config, knowledge_root)
    if declared:
        raise ReadOnlySurfaceError(
            f"entity class {entity_class!r} maps to surface {adapter.name!r}, but {declared}"
        )
    if not adapter.corpus_policy.embedded:
        raise SurfaceNotIndexedError(
            f"entity class {entity_class!r} maps to surface {adapter.name!r}, which is "
            "not indexed (embedded: false); compile cannot write pages there"
        )
    return adapter.resolve_root(knowledge_root)


def page_write_root_for_store(
    store: str,
    entity_class: str | None,
    config: dict[str, Any] | None,
    knowledge_root: Path,
) -> Path:
    """Directory a NEW page is written to for the store an intake decision named.

    ``personal`` is the default wiki surface; any other word names a configured
    adapter. Same guards as :func:`page_write_root_for_class` (read-only,
    declared-refusal, not-indexed). A store this install does not hold raises
    :class:`StorageConfigError`. Intake routing S3a (athenaeum#2999).
    """
    adapters = available_adapters(config)
    if "personal" in adapters:
        raise StorageConfigError(
            "storage.adapters.personal collides with the reserved store word "
            "'personal' (the default wiki surface) -- pick a different adapter name"
        )
    name = DEFAULT_ADAPTER_NAME if store == "personal" else store
    adapter = adapters.get(name)
    if adapter is None:
        raise StorageConfigError(
            f"intake decision names store {store!r}, which this install does not hold"
        )
    return _write_root_for_adapter(adapter, entity_class, config, knowledge_root)


def store_opaque_filenames(store: str, config: dict[str, Any] | None) -> bool:
    """Whether new pages written to *store* get uid-only file names (athenaeum#3077).

    *store* is an intake-decision store word (``personal`` is the default wiki
    surface, which cannot opt in). A store this install does not hold is ``False``.
    """
    name = DEFAULT_ADAPTER_NAME if store == "personal" else store
    adapter = available_adapters(config).get(name)
    return bool(adapter is not None and adapter.opaque_filenames)


def resolve_store_for_class(
    entity_class: str | None,
    config: dict[str, Any] | None,
    knowledge_root: Path,
) -> Store:
    """Store-backed counterpart to :func:`surface_root_for_class` (issue athenaeum#976).

    Same fail-closed resolution as :func:`surface_root_for_class` — an
    *entity_class* that ``storage.mapping`` routes to an unknown adapter
    raises :class:`StorageConfigError` here too — but returns a
    :class:`athenaeum.store.Store` handle instead of a bare
    :class:`~pathlib.Path`.

    Lives HERE, alongside :func:`surface_root_for_class` (the issue's
    acceptance criterion), rather than in :mod:`athenaeum.store` the way
    ``docs/extending/whole-store-adapter-design.md`` §6.4's layering paragraph
    describes. That module is deliberately built with no import of this one
    (see its docstring) so this module can import IT — a real, one-directional,
    module-level import — without the two forming a 2-node import-graph SCC
    (``tests/test_import_graph_acyclic.py``, zero-tolerance since issue
    athenaeum#640). This is the one place S1's contract reading departs from
    the design note's stated module split; the resulting shape still honors
    D5 ("the existing seam is extended, never forked") — there is exactly one
    ``resolve_store_for_class`` implementation, defined here, and
    :class:`~athenaeum.store.FilesystemStore` has no second entry point.

    Builds a ``{surface_name: root}`` mapping from every adapter this
    *config* makes available (:func:`available_adapters`) — not just the one
    *entity_class* resolves to — so the returned :class:`Store` can address
    any configured surface via ``StoreKey.surface``, matching the protocol's
    "keys carry surface" shape (design note §6.2 D2) rather than binding one
    store instance to one surface.
    """
    resolve_adapter_for_class(entity_class, config)  # fail-closed validation only
    return open_store(config, knowledge_root)


def open_store(
    config: dict[str, Any] | None,
    knowledge_root: Path,
    *,
    extra_roots: Mapping[str, Path] | None = None,
) -> FilesystemStore:
    """The one place production code builds a :class:`~athenaeum.store.FilesystemStore`
    (issue athenaeum#2614).

    Returns a store whose surface map carries every surface this *config*
    makes available (:func:`available_adapters`, keyed by adapter name), so a
    capability check made against it answers for the configured layout rather
    than the default one. A *config* of ``None`` (or one with no ``storage:``
    section) yields the built-in surfaces only, which is what every caller
    that built ``FilesystemStore(knowledge_root, {})`` by hand effectively had
    — those surfaces are only ever addressed by ``StoreKey.surface``, which
    such callers did not use.

    *extra_roots* adds caller-private surfaces (``{name: root}``) on top of the
    configured ones — for a module that addresses its own internal surface
    names (quarantine, the search index build) and has no config to derive
    them from. An extra root wins over a configured surface of the same name.

    ``tests/test_open_store_sole_constructor.py`` fails the build if any other
    production module constructs a ``FilesystemStore`` directly.
    """
    roots: dict[str, Path] = {
        name: adapter.resolve_root(knowledge_root)
        for name, adapter in available_adapters(config).items()
    }
    if extra_roots:
        roots.update(extra_roots)
    non_purgeable = {
        name for name, adapter in available_adapters(config).items() if not adapter.purgeable
    }
    read_only = {name for name, adapter in available_adapters(config).items() if adapter.read_only}
    surface_history = {
        name: adapter.history
        for name, adapter in available_adapters(config).items()
        if adapter.history is not None
    }
    spec = StoreBuildSpec(
        config=config,
        knowledge_root=knowledge_root,
        roots=roots,
        non_purgeable_surfaces=frozenset(non_purgeable),
        read_only_surfaces=frozenset(read_only),
        history=resolve_storage_history(config),
        surface_history=surface_history,
        write_gate=lambda surface: declared_compile_refusal(surface, config, knowledge_root),
    )
    # Declared FilesystemStore: existing callers use its extras (sync_repos,
    # surface_repo, ...) that the ``Store`` protocol does not carry. A
    # registered implementation reaches them only if it provides the same
    # attributes; narrowing callers to the protocol is a separate change.
    return cast(FilesystemStore, _store_factory_for(available_adapters(config))(spec))


def _store_factory_for(adapters: Mapping[str, StorageAdapter]) -> StoreFactory:
    """The factory that builds the one store addressing *adapters*' locations.

    Adapters sharing an implementation share one instance. The built-in
    surfaces are always present and are filesystem-backed, so a single
    registered implementation takes over the whole store (it addresses every
    location); two distinct registered implementations cannot share one store
    and are refused (mixed implementations are out of scope).
    """
    used: set[str] = set()
    for name, adapter in adapters.items():
        used.add(
            _validate_backing_store(
                adapter.backing_store, f"storage adapter {name!r} backing_store"
            )
        )
    custom = sorted(used - set(BUILTIN_BACKING_STORES))
    if len(custom) > 1:
        raise StorageConfigError(
            f"storage adapters use more than one store implementation "
            f"({', '.join(repr(n) for n in custom)}); one store addresses every "
            "location, so every adapter must use the same non-filesystem "
            "backing_store (or 'markdown')"
        )
    if custom:
        return _STORE_BACKENDS[custom[0]]
    return _filesystem_store_factory


def _filesystem_store_factory(spec: StoreBuildSpec) -> Store:
    return FilesystemStore(
        spec.knowledge_root,
        dict(spec.roots),
        non_purgeable_surfaces=spec.non_purgeable_surfaces,
        read_only_surfaces=spec.read_only_surfaces,
        history=spec.history,
        surface_history=spec.surface_history,
        write_gate=spec.write_gate,
    )


# ---------------------------------------------------------------------------
# Read-only surfaces: the ONE path-based write guard (athenaeum#2621)
# ---------------------------------------------------------------------------


def read_only_surface_roots(
    config: dict[str, Any] | None, knowledge_root: Path
) -> dict[str, Path]:
    """``{surface name: resolved root}`` for every surface declared ``read_only``."""
    if config is None:
        config = load_config(knowledge_root)
    return {
        name: adapter.resolve_root(knowledge_root).resolve()
        for name, adapter in available_adapters(config).items()
        if adapter.read_only
    }


def declared_compile_refusal(
    surface: str, config: dict[str, Any] | None, knowledge_root: Path
) -> str | None:
    """Why the surface named *surface* refuses a write for lack of declared compile.

    The declaration layer of the write guard (design note §9, access S3): a
    surface whose ``_store/access.yaml`` does not grant the owner compile is
    refused, whatever its adapter's ``read_only`` says. It can only narrow:
    ``None`` (no refusal) when the surface has no declaration, or the rights held
    include compile. A declaration that is invalid, or a directory that cannot
    answer, holds the owner as a reader only, so it refuses. Rights come from the
    run-start table (:func:`athenaeum.store_rights.declared_surface_rights`); this
    is called only on a write, never on the recall path.
    """
    from athenaeum.store_rights import declared_surface_rights

    if config is None:
        config = load_config(knowledge_root)
    adapter = available_adapters(config).get(surface)
    if adapter is None:
        return None
    root = adapter.resolve_root(knowledge_root)
    rights = declared_surface_rights(root, resolve_owner_asserter(config))
    if rights is None or "compile" in rights:
        return None
    held = ",".join(sorted(rights)) or "none"
    return (
        "its store declaration does not grant this install compile "
        f"(rights held: {held}); a page there is never written from here"
    )


def declared_compile_refusal_of(
    path: Path, config: dict[str, Any] | None, knowledge_root: Path
) -> tuple[str, str] | None:
    """``(surface, reason)`` when *path* lies on a surface the declaration refuses."""
    if config is None:
        config = load_config(knowledge_root)
    resolved = Path(path).resolve()
    for name, adapter in available_adapters(config).items():
        root = adapter.resolve_root(knowledge_root).resolve()
        if resolved == root or root in resolved.parents:
            reason = declared_compile_refusal(name, config, knowledge_root)
            if reason:
                return name, reason
    return None


def read_only_surface_of(
    path: Path, config: dict[str, Any] | None, knowledge_root: Path
) -> str | None:
    """The read-only surface *path* lives on, else ``None``."""
    roots = read_only_surface_roots(config, knowledge_root)
    if not roots:
        return None
    resolved = Path(path).resolve()
    for name, root in roots.items():
        if resolved == root or root in resolved.parents:
            return name
    return None


def is_read_only_path(
    path: Path, config: dict[str, Any] | None, knowledge_root: Path
) -> bool:
    """Whether *path* lies under a surface declared ``read_only``."""
    return read_only_surface_of(path, config, knowledge_root) is not None


def require_writable_path(
    path: Path,
    knowledge_root: Path,
    config: dict[str, Any] | None = None,
    *,
    op: str = "write",
) -> None:
    """Raise :class:`ReadOnlySurfaceError` when *path* is on a read-only surface.

    The ONE guard for writers that write through a bare ``Path`` instead of
    the store (compile, structured upsert, merge, fold, repair, retire,
    migration): each calls it with the page it is about to change or remove,
    BEFORE the write. A deployment declaring no ``read_only`` surface pays one
    config read and gets a no-op.
    """
    surface = read_only_surface_of(path, config, knowledge_root)
    if surface is not None:
        raise ReadOnlySurfaceError(
            f"{op} refused on {path}: it lives on surface {surface!r}, declared "
            "read_only (a shared mount this install never writes)"
        )
    refused = declared_compile_refusal_of(path, config, knowledge_root)
    if refused is not None:
        raise ReadOnlySurfaceError(
            f"{op} refused on {path}: it lives on surface {refused[0]!r}, and {refused[1]}"
        )


def require_writable_paths(
    paths: Sequence[Path],
    knowledge_root: Path,
    config: dict[str, Any] | None = None,
    *,
    op: str = "write",
) -> None:
    """:func:`require_writable_path` over *paths*, reading the config once.

    Used at the entry of an apply operation, so it refuses whole before the
    first page is changed rather than half-way through.
    """
    if config is None:
        config = load_config(knowledge_root)
    for path in paths:
        require_writable_path(path, knowledge_root, config, op=op)


def refuse_read_only_write(
    path: Path,
    knowledge_root: Path,
    config: dict[str, Any] | None = None,
    *,
    op: str = "write",
) -> str | None:
    """Non-raising form of :func:`require_writable_path` for a batch writer.

    Returns the refusal message (``"ReadOnlySurfaceError: ..."``) when *path*
    is on a read-only surface, else ``None``. A writer that loops over many
    pages (compile) uses this to skip the one page, log the refusal and carry
    on, rather than abort the run; the page is left byte-for-byte untouched.
    """
    try:
        require_writable_path(path, knowledge_root, config, op=op)
    except ReadOnlySurfaceError as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


# ---------------------------------------------------------------------------
# Indexed surfaces: the ONE derivation of "which roots feed the search index"
# ---------------------------------------------------------------------------


class IndexedSurfaceError(StorageConfigError):
    """Raised when the set of indexed roots is unsafe or ambiguous.

    Two cases, both refused at config resolution (never at scan time):

    * an indexed root that equals, contains, or sits inside the root of a
      surface that is NOT indexed (``embedded: false``, e.g. the built-in
      ``excluded`` surface) -- the data that surface keeps out of the index
      would otherwise be one overlapping directory away from entering it;
    * two indexed roots that would produce the same indexed-name prefix, so
      their pages would collide (or silently shadow each other) in the index.
    """


@dataclass(frozen=True)
class IndexedSurface:
    """One non-wiki root the index scans: its indexed-name *prefix* + *root*.

    Indexed names are ``<prefix>/<relpath>``. A configured storage surface
    uses its surface (adapter) name as the prefix, so renaming the folder it
    lives in does not orphan its index rows. A bare ``recall.extra_intake_roots``
    entry keeps its historical prefix, the folder basename.
    """

    prefix: str
    root: Path
    #: True for a raw-intake root (a bare ``recall.extra_intake_roots`` entry),
    #: False for a configured storage surface of compiled pages. Only intake
    #: roots are subject to the relevance-policy ``never`` filter at index time
    #: (athenaeum#2648).
    #: (Excluded from equality: it is a scan hint, not part of a surface's identity.)
    intake: bool = field(default=False, compare=False)


#: What the index scan and the hit-path resolvers accept per extra root: a
#: bare :class:`~pathlib.Path` (prefix = folder basename, the historical shape)
#: or an :class:`IndexedSurface` carrying its own prefix.
IndexedRoot = Path | IndexedSurface


def as_indexed_surface(item: IndexedRoot) -> IndexedSurface:
    """Normalize a bare :class:`~pathlib.Path` to its :class:`IndexedSurface`."""
    # Duck-typed on purpose (not ``isinstance(item, IndexedSurface)``): a test
    # that reloads this module mints a second IndexedSurface class, and an
    # isinstance check would then turn a surface into a bad Path (athenaeum#2616).
    if isinstance(item, (str, os.PathLike)):
        path = Path(item)
        return IndexedSurface(prefix=path.name, root=path, intake=True)
    return item


def _is_within(inner: Path, outer: Path) -> bool:
    return inner == outer or outer in inner.parents


#: Directory under ``raw/`` holding one raw-intake directory per store a single
#: compiling install compiles (``raw/_stores/<store_id>/``; intake routing S15,
#: ``docs/design/federation-intake-routing.md`` §8.1). It is raw intake of that
#: store, never an indexed surface.
STORE_RAW_DIRNAME = "_stores"


def validate_indexed_surfaces(
    indexed: Sequence[IndexedSurface],
    non_indexed: Mapping[str, Path],
    *,
    wiki_root: Path | None = None,
) -> None:
    """Refuse an unsafe or ambiguous indexed-root set (see :class:`IndexedSurfaceError`).

    *indexed* is the list of non-wiki indexed roots; *non_indexed* maps the
    name of each surface that is not indexed to its root. *wiki_root*, when
    given, reserves the ``wiki`` prefix (an indexed root whose prefix is
    ``wiki`` would shadow wiki hits); it is not overlap-checked, because
    surfaces inside ``wiki/`` are filtered per page class at scan time.
    """
    entries = list(indexed)
    # The default wiki surface is deliberately NOT in the overlap check: a
    # non-indexed surface rooted at or inside ``wiki/`` is the supported
    # athenaeum#532 shape, kept out of the index per page class by the
    # scan-time ``is_embedded`` filter (both scans walk the page and drop it).
    # Refusing it here would break existing deployments. *wiki_root* only
    # reserves the ``wiki`` prefix.
    checked: list[tuple[str, Path]] = [(e.prefix, e.root) for e in entries]
    seen: dict[str, Path] = {}
    for entry in entries:
        if "/" in entry.prefix or not entry.prefix:
            raise IndexedSurfaceError(
                f"indexed root {entry.root} has an unusable index prefix "
                f"{entry.prefix!r}: it must be non-empty and contain no '/'"
            )
        if entry.prefix == "wiki":
            raise IndexedSurfaceError(
                f"indexed root {entry.root} would use the reserved index prefix "
                "'wiki' -- rename the surface or folder"
            )
        if entry.prefix in seen:
            raise IndexedSurfaceError(
                f"indexed roots {seen[entry.prefix]} and {entry.root} share the "
                f"index prefix {entry.prefix!r}; their pages would collide in the "
                "index -- rename one"
            )
        seen[entry.prefix] = entry.root
    for label, root in checked:
        resolved = root.resolve()
        for ex_name, ex_root in non_indexed.items():
            ex_resolved = ex_root.resolve()
            if _is_within(resolved, ex_resolved) or _is_within(ex_resolved, resolved):
                raise IndexedSurfaceError(
                    f"indexed root {root} ({label!r}) overlaps the root of the "
                    f"non-indexed surface {ex_name!r} ({ex_root}): it equals, "
                    "contains, or sits inside it, so excluded pages could enter "
                    "the index -- move one of them"
                )
    if wiki_root is not None:
        # Intake routing S15 (§8.1): a store's raw intake is never indexed -- a
        # message waiting for a store is not something the member's assistant
        # recalls. Refuse a root that is, holds or sits inside it, however it
        # got on the list (a surface, or ``recall.extra_intake_roots``).
        store_raw = (wiki_root.parent / "raw" / STORE_RAW_DIRNAME).resolve()
        for label, root in checked:
            if _is_within(root.resolve(), store_raw) or _is_within(store_raw, root.resolve()):
                raise IndexedSurfaceError(
                    f"indexed root {root} ({label!r}) equals, contains, or sits inside "
                    f"{store_raw}, a store's raw intake: messages waiting for a store "
                    "are never indexed or recalled -- remove it (for example from "
                    "recall.extra_intake_roots)"
                )


def gathered_surface_mounts(
    knowledge_root: Path, config: dict[str, Any] | None
) -> dict[str, tuple[Path, str | None]]:
    """``{name: (root, store_id)}`` of the surfaces the index gather mount-checks.

    Exactly the surfaces :func:`resolve_indexed_surfaces` tests for a mount
    refusal: ``embedded: true``, outside ``wiki/``, present on disk. Run-start
    reporting uses this so it never reports a surface the gather does not refuse.
    """
    wiki_resolved = (knowledge_root / "wiki").resolve()
    out: dict[str, tuple[Path, str | None]] = {}
    for name, adapter in available_adapters(config).items():
        root = adapter.resolve_root(knowledge_root)
        resolved = root.resolve()
        if not adapter.corpus_policy.embedded:
            continue
        if resolved == wiki_resolved or wiki_resolved in resolved.parents:
            continue
        if root.is_dir():
            out[name] = (root, adapter.store_id)
    return out


def _declared_unmounted(
    name: str, root: Path, asserter: Mapping[str, Any] | None, store_id: str | None
) -> bool:
    """Whether the surface *name* is a declared store this install refuses to mount."""
    from athenaeum.store_rights import declared_mount_state

    return declared_mount_state(name, root, asserter, store_id).refusal is not None


def resolve_indexed_surfaces(
    knowledge_root: Path,
    config: dict[str, Any] | None,
    *,
    extra_roots: list[Path] | None = None,
) -> list[IndexedSurface]:
    """The ONE resolver for the non-wiki roots the search index scans (athenaeum#2616).

    Used by both index backends' callers and by the hit-path resolvers, so
    what is indexed and what a hit resolves back to can never drift apart.
    Returns, in order: every configured storage surface whose corpus policy is
    ``embedded: true`` (prefix = surface name), then every
    ``recall.extra_intake_roots`` entry (prefix = folder basename, unchanged).
    The default wiki surface is not listed -- its names are bare, so an
    existing index needs no rebuild. With no extra surface configured the
    result is exactly today's extra-roots list, hence a byte-identical index.

    Raises :class:`IndexedSurfaceError` when an indexed root equals/contains/is
    inside a non-indexed surface root (including the case where that root is
    ALSO listed as an extra intake root), or two indexed roots share a prefix.
    The scan-time ``pii:`` flag filter stays as the second layer.

    A surface that is a declared store on which this install does not hold read
    (or whose adapter ``store_id`` differs from the declaration's) is left out
    (access S4, athenaeum#2989), so it is neither indexed nor recalled; nothing
    on disk is deleted. This is the one gather point for both the index build
    and recall, so the two cannot disagree.

    *extra_roots* overrides the config's extra intake roots (callers that have
    already resolved them).
    """
    if config is None:
        config = load_config(knowledge_root)
    wiki_root = knowledge_root / "wiki"
    wiki_resolved = wiki_root.resolve()
    indexed: list[IndexedSurface] = []
    non_indexed: dict[str, Path] = {}
    unmounted: set[Path] = set()
    asserter = resolve_owner_asserter(config)
    for name, adapter in available_adapters(config).items():
        root = adapter.resolve_root(knowledge_root)
        if adapter.corpus_policy.embedded:
            if root.resolve() == wiki_resolved or wiki_resolved in root.resolve().parents:
                # The default wiki surface, or a surface inside it: the wiki
                # walk already scans these pages flat under bare names, so
                # listing them again would index them twice.
                continue
            if root.is_dir():
                # Access S4: a declared store this install cannot read is not
                # mounted -- not indexed, so not recalled. Nothing is deleted.
                if _declared_unmounted(name, root, asserter, adapter.store_id):
                    unmounted.add(root.resolve())
                    continue
                indexed.append(IndexedSurface(prefix=name, root=root.resolve()))
        else:
            non_indexed[name] = root
    if extra_roots is None:
        extra_roots = resolve_extra_intake_roots(knowledge_root, config)
    already = {s_.root for s_ in indexed}
    indexed.extend(
        IndexedSurface(prefix=r.name, root=r, intake=True)
        for r in extra_roots
        if r.resolve() not in already  # a surface also listed as an extra root
        and r.resolve() not in unmounted  # ... or one this install does not mount
    )
    validate_indexed_surfaces(indexed, non_indexed, wiki_root=wiki_root)
    return indexed


def resolve_locator_surfaces(
    knowledge_root: Path, config: dict[str, Any] | None = None
) -> list[IndexedSurface]:
    """The extra surfaces :class:`~athenaeum.models.PageLocator` reads pages from (athenaeum#2617).

    The configured storage surfaces :func:`resolve_indexed_surfaces` includes
    (``embedded: true``, outside ``wiki/``), with its exclusion guards. Bare
    ``recall.extra_intake_roots`` entries are raw-intake folders, not page
    stores, so they are left out: a deployment with no extra storage surface
    gets ``[]`` and the historical wiki-only lookup.
    """
    return resolve_indexed_surfaces(knowledge_root, config, extra_roots=[])


_RAW_INTAKE_FILENAME_RETRIES = 5


def write_raw_intake(target_dir: Path, content: str) -> Path:
    """Persist one raw-intake file atomically under *target_dir* (issue athenaeum#534, M13).

    Mints a UTC-timestamp + short-id ``.md`` filename, creates the directory,
    and writes via :func:`athenaeum.atomic_io.atomic_write_text` so an
    interrupted ``remember()`` never leaves a truncated intake file for the next
    compile to parse. Retries the (astronomically unlikely) filename collision.
    Returns the written path. This keeps the L5 MCP presentation layer out of
    the L0 filesystem: it validates + injects frontmatter, then delegates the
    write here.
    """
    target_dir.mkdir(parents=True, exist_ok=True)

    filepath: Path | None = None
    for _ in range(_RAW_INTAKE_FILENAME_RETRIES):
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        short_id = uuid.uuid4().hex[:8]
        candidate = target_dir / f"{timestamp}-{short_id}.md"
        if not candidate.exists():
            filepath = candidate
            break
    else:
        raise FileExistsError(
            f"could not mint a unique raw-intake filename under {target_dir} "
            f"after {_RAW_INTAKE_FILENAME_RETRIES} attempts"
        )

    atomic_write_text(filepath, content)
    return filepath


def write_raw_intake_screened(
    target_dir: Path,
    content: str,
    *,
    config: dict[str, Any] | None,
    knowledge_root: Path,
    raw_ref: str | None = None,
) -> Path:
    """Screen *content*, then persist it as one raw-intake file (athenaeum#3175).

    The entry point intake code uses for content that is not yet screened: any
    value a ``storage.routes`` / ``sensitivity`` rule routes goes to its vault
    first and the note carries a pointer; a discarded value is replaced by the
    fixed marker (or the field is dropped). Only then is the raw note written
    through :func:`write_raw_intake`. A failed vault write raises
    :class:`athenaeum.sensitivity_routing.SensitivityRoutingError` and writes
    nothing. *raw_ref* is a stable reference to the note (the same value on a
    retry), used to derive the deterministic vault record ids.

    With no rule that applies the bytes written are exactly those
    :func:`write_raw_intake` would write. :func:`write_raw_intake` stays for
    content that was already screened.
    """
    # Deferred: sensitivity_routing imports this module at module scope.
    from athenaeum.sensitivity_routing import screen_intake_content

    screened = screen_intake_content(
        content, raw_ref=raw_ref, config=config, knowledge_root=knowledge_root
    )
    return write_raw_intake(target_dir, screened)
