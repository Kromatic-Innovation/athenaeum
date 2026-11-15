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

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from athenaeum.atomic_io import atomic_write_text
from athenaeum.config import (
    load_config,
    resolve_extra_intake_roots,
    resolve_storage_adapters,
    resolve_storage_mapping,
)
from athenaeum.store import FilesystemStore, Store

# One-directional edge, real (not deferred): athenaeum.store (L0/L1) imports
# NOTHING from this module (see its module docstring's "Layering" note), so
# this module importing athenaeum.store here carries no cycle —
# tests/test_import_graph_acyclic.py's zero-tolerance SCC guard (baseline
# pinned to `[]` since issue athenaeum#640) would fail loudly if it did.


class StorageConfigError(ValueError):
    """Raised when the storage layer is misconfigured.

    Loud by design (mirrors :class:`athenaeum.provider.ProviderConfigError`):
    a mapping that names an adapter that does not exist, or a custom adapter
    definition with a malformed policy, must never silently fall back to the
    default surface — that would route a class the operator meant to *exclude*
    straight into the recalled corpus.
    """


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
)

_BUILTIN_ADAPTERS: dict[str, StorageAdapter] = {
    WIKI_MARKDOWN_EMBEDDED.name: WIKI_MARKDOWN_EMBEDDED,
    EXCLUDED.name: EXCLUDED,
}

#: Code-registered custom adapters (the in-process extension point). Separate
#: from builtins so :func:`register_adapter` can never clobber the default
#: surface, and from config-defined adapters so precedence stays legible.
_REGISTERED_ADAPTERS: dict[str, StorageAdapter] = {}


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
    if not isinstance(surface_root, str) or not surface_root.strip():
        raise StorageConfigError(
            f"storage.adapters.{name}.surface_root must be a non-empty string"
        )
    return StorageAdapter(
        name=name,
        backing_store=backing_store.strip(),
        surface_root=surface_root.strip(),
        corpus_policy=_policy_from_mapping(raw.get("corpus_policy")),
    )


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
    mapping = resolve_storage_mapping(config)
    adapter_name = mapping.get(cls, DEFAULT_ADAPTER_NAME) if cls else DEFAULT_ADAPTER_NAME
    adapters = available_adapters(config)
    adapter = adapters.get(adapter_name)
    if adapter is None:
        raise StorageConfigError(
            f"storage.mapping routes class {cls!r} to unknown adapter "
            f"{adapter_name!r}; known adapters: {sorted(adapters)}"
        )
    return adapter


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
    return bool(resolve_storage_mapping(config) or resolve_storage_adapters(config))


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
    return FilesystemStore(knowledge_root, roots)


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


#: What the index scan and the hit-path resolvers accept per extra root: a
#: bare :class:`~pathlib.Path` (prefix = folder basename, the historical shape)
#: or an :class:`IndexedSurface` carrying its own prefix.
IndexedRoot = Path | IndexedSurface


def as_indexed_surface(item: IndexedRoot) -> IndexedSurface:
    """Normalize a bare :class:`~pathlib.Path` to its :class:`IndexedSurface`."""
    if isinstance(item, IndexedSurface):
        return item
    return IndexedSurface(prefix=item.name, root=item)


def _is_within(inner: Path, outer: Path) -> bool:
    return inner == outer or outer in inner.parents


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

    *extra_roots* overrides the config's extra intake roots (callers that have
    already resolved them).
    """
    if config is None:
        config = load_config(knowledge_root)
    wiki_root = knowledge_root / "wiki"
    wiki_resolved = wiki_root.resolve()
    indexed: list[IndexedSurface] = []
    non_indexed: dict[str, Path] = {}
    for name, adapter in available_adapters(config).items():
        root = adapter.resolve_root(knowledge_root)
        if adapter.corpus_policy.embedded:
            if root.resolve() == wiki_resolved or wiki_resolved in root.resolve().parents:
                # The default wiki surface, or a surface inside it: the wiki
                # walk already scans these pages flat under bare names, so
                # listing them again would index them twice.
                continue
            if root.is_dir():
                indexed.append(IndexedSurface(prefix=name, root=root.resolve()))
        else:
            non_indexed[name] = root
    if extra_roots is None:
        extra_roots = resolve_extra_intake_roots(knowledge_root, config)
    already = {s_.root for s_ in indexed}
    indexed.extend(
        IndexedSurface(prefix=r.name, root=r)
        for r in extra_roots
        if r.resolve() not in already  # a surface also listed as an extra root
    )
    validate_indexed_surfaces(indexed, non_indexed, wiki_root=wiki_root)
    return indexed


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
