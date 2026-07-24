# SPDX-License-Identifier: Apache-2.0
"""Pluggable storage-surface layer — entity class → surface + corpus policy (issue #429).

This module generalizes the "PII lives on an excluded path" idea (#427) into a
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
    #427's PII / archival-contact surface consumes — routed through this
    adapter, **not a hardcoded exclusion path**. Because an excluded surface's
    root lives outside the corpus scanners' search set (``wiki/`` + configured
    ``recall.extra_intake_roots``), its pages are excluded from embed / recall /
    merge **by construction** — the fail-closed property #427 requires.

Adding a new surface (a new store or a new corpus policy) is **config + an
adapter registration, no core change**: define it under ``storage.adapters`` in
``athenaeum.yaml`` (or call :func:`register_adapter` from code) and map an
entity class to it under ``storage.mapping``. The deferred skill-file-sync
surface (#426's out-of-scope idea) is a future consumer of exactly this seam.

Naming note — this is a STORAGE-surface adapter, a different concept from the
source → raw-intake *adapter* documented in ``docs/adapter-contract.md`` and
the bundled ``adapter-authoring`` skill. An intake adapter turns an external
source into ``raw/`` files; a storage adapter governs where a compiled entity
class is persisted and whether that surface joins the corpus. They sit on
opposite ends of the pipeline and never collide.

Like :class:`athenaeum.search.SearchBackend` and
:func:`athenaeum.provider.build_llm_client`, this is an INTERNAL seam: it is
importable but not part of the stable ``__all__`` surface, and its signatures
may change between minor releases until the extension point is promoted with a
companion ``docs/storage-adapter-contract.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from athenaeum.config import resolve_storage_adapters, resolve_storage_mapping


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
        """No corpus participation — the excluded surface (#427)."""
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

#: The built-in excluded surface #427's PII / archival-contact pages consume.
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
    ships an adapter in code (e.g. #426's skill-file-sync surface) registers it
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


def corpus_policy_for_class(
    entity_class: str | None,
    config: dict[str, Any] | None,
) -> CorpusPolicy:
    """Corpus policy for *entity_class* (default surface → all-true)."""
    return resolve_adapter_for_class(entity_class, config).corpus_policy


def is_embedded(entity_class: str | None, config: dict[str, Any] | None) -> bool:
    """Whether pages of *entity_class* are embedded into the index."""
    return corpus_policy_for_class(entity_class, config).embedded


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

    The writer-facing entry point #427's PII surface consumes: instead of
    hardcoding ``knowledge_root / "contacts"``, a writer asks the layer where a
    class lives and gets the surface root its adapter resolves to (``wiki/`` for
    the default, the configured excluded root for an excluded class).
    """
    return resolve_adapter_for_class(entity_class, config).resolve_root(knowledge_root)
