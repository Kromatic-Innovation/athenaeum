# SPDX-License-Identifier: Apache-2.0
"""Athenaeum — open source knowledge management pipeline.

Contract: this is the package root; its ``__all__`` is the ONLY import
surface covered by semver compatibility guarantees. Everything exported here
is a thin re-export (init, top-level librarian entry points, and the handful
of :mod:`athenaeum.models` shapes/helpers callers need) — no logic lives in
this file itself.

Factoring rule: adding a name to ``__all__`` is a public-API commitment: do it
only for something a library consumer (not the CLI) genuinely needs, and keep
the list in sync with the imports above it. Most functionality is exercised
through the CLI subcommands (``athenaeum enumerate``, ``athenaeum recall``,
``athenaeum repair``, ``athenaeum dedupe``, ``athenaeum questions``,
``athenaeum ingest-answers``, ``athenaeum ingest-merges``, etc.) — see
``README.md`` and ``athenaeum --help``. Internal modules (``contradictions``,
``merge``, ``clusters``, ``delta``, ``dedupe``, ``repair``, ``answers``,
``provenance``, ``resolutions``, ``json_utils``, ``retire``, ``owner``,
``transcript_verify``, ``wiki_dedupe``, ``storage``) are importable but not
part of the stable public surface; their signatures may change between minor
releases.

``athenaeum.store`` (issue athenaeum#983, S8 of the whole-store adapter design
lock, ``docs/whole-store-adapter-design.md`` §6/§9.2) is promoted here only
IN PART: the ``Store`` protocol's data/error types and the shipped
:class:`~athenaeum.store.FilesystemStore` adapter are the published contract
(see ``docs/store-contract.md``), so those names are below. The rest of
``athenaeum.store`` — the S4 lease-primitive internals
(:func:`~athenaeum.store.lease_open_fd` and siblings, :class:`~athenaeum.store.FileLease`),
the S5 artifact-registry catalogue (:data:`~athenaeum.store.ARTIFACT_REGISTRY`
and :class:`~athenaeum.store.ArtifactDeclaration`), and
:func:`~athenaeum.store.append_line_durable` — stays internal, same as the
module's own docstring states. The public conformance harness a third-party
adapter author runs against their own implementation,
:mod:`athenaeum.store_conformance`, is intentionally NOT re-exported here:
it depends on ``pytest`` (a ``dev``-extra-only dependency), so pulling it
into this always-imported package root would make ``pytest`` a hard runtime
dependency of every ``athenaeum`` import; import it directly
(``from athenaeum.store_conformance import StoreConformanceTests``) instead.

Import cost: every name above is re-exported **lazily** (PEP 562 module
``__getattr__``), and so is ``__version__``. This is load-bearing, not a
micro-optimisation. Eagerly importing :mod:`athenaeum.librarian` here pulled the
``anthropic`` SDK into *every* ``import athenaeum`` — ~440 ms on every invocation
of every CLI entry point, whether or not the command touches an LLM, and
inherited by :mod:`athenaeum.search` / :mod:`athenaeum.push_metrics` purely by
their living under this package root (issue athenaeum#1360). Because a submodule
import executes its package root first, that cost could not be escaped by
importing the submodule directly, nor by loading it via
``importlib.util.spec_from_file_location`` — measured and recorded in
``docs/retrieval-entry-point-measurements.md`` (issue athenaeum#1357).

The contract is unchanged: every name in ``__all__`` still resolves on the
package, ``from athenaeum import ingest`` still works, and ``dir(athenaeum)``
still lists the full surface. Only the *timing* of the underlying module import
moved — from import time to first attribute access. ``tests/test_import_budget.py``
pins the transitive import set so the chain cannot silently return; adding a
module-scope ``from athenaeum.librarian import run`` to this file, or to
:mod:`athenaeum.search`, fails it.

Layering: sits above L5 by necessity (it imports the CLI-adjacent
:mod:`athenaeum.librarian` pipeline entry points as well as the L1
:mod:`athenaeum.models` hub, L1 :mod:`athenaeum.init`, and L1
:mod:`athenaeum.store` contract types) — this file is the one place layering
is deliberately collapsed, since a package root must expose the whole stack's
public surface in one namespace.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Re-stated for type checkers and IDEs only: these names resolve at runtime
    # through ``__getattr__`` below, but a static reader cannot follow that, and
    # mypy checks this block as if the imports were real.
    from athenaeum.init import init_knowledge_dir
    from athenaeum.librarian import (
        IngestResult,
        SessionEndResult,
        discover_raw_files,
        ingest,
        process_one,
        rebuild_index,
        reindex,
        run,
        session_end,
    )
    from athenaeum.models import (
        ClassifiedEntity,
        EntityAction,
        EntityIndex,
        EscalationItem,
        ProcessingResult,
        RawFile,
        WikiEntity,
        generate_uid,
        parse_frontmatter,
        render_frontmatter,
        slugify,
    )
    from athenaeum.store import (
        FilesystemStore,
        Lease,
        LeaseHeldError,
        ObjectMeta,
        Record,
        Store,
        StoreCapabilities,
        StoreConflictError,
        StoreKey,
        StoreKeyError,
        UnknownSurfaceError,
    )

__all__ = [
    "ClassifiedEntity",
    "EntityAction",
    "EntityIndex",
    "EscalationItem",
    "FilesystemStore",
    "IngestResult",
    "Lease",
    "LeaseHeldError",
    "ObjectMeta",
    "ProcessingResult",
    "RawFile",
    "Record",
    "SessionEndResult",
    "Store",
    "StoreCapabilities",
    "StoreConflictError",
    "StoreKey",
    "StoreKeyError",
    "UnknownSurfaceError",
    "WikiEntity",
    "discover_raw_files",
    "generate_uid",
    "ingest",
    "init_knowledge_dir",
    "parse_frontmatter",
    "process_one",
    "rebuild_index",
    "reindex",
    "render_frontmatter",
    "run",
    "session_end",
    "slugify",
]

# Every public name, mapped to the module that defines it. Resolved on first
# attribute access and then cached into ``globals()``, so the second access is
# an ordinary dict lookup and the module is imported at most once.
_LAZY_EXPORTS: dict[str, str] = {
    # athenaeum.init
    "init_knowledge_dir": "athenaeum.init",
    # athenaeum.librarian
    "IngestResult": "athenaeum.librarian",
    "SessionEndResult": "athenaeum.librarian",
    "discover_raw_files": "athenaeum.librarian",
    "ingest": "athenaeum.librarian",
    "process_one": "athenaeum.librarian",
    "rebuild_index": "athenaeum.librarian",
    "reindex": "athenaeum.librarian",
    "run": "athenaeum.librarian",
    "session_end": "athenaeum.librarian",
    # athenaeum.models
    "ClassifiedEntity": "athenaeum.models",
    "EntityAction": "athenaeum.models",
    "EntityIndex": "athenaeum.models",
    "EscalationItem": "athenaeum.models",
    "ProcessingResult": "athenaeum.models",
    "RawFile": "athenaeum.models",
    "WikiEntity": "athenaeum.models",
    "generate_uid": "athenaeum.models",
    "parse_frontmatter": "athenaeum.models",
    "render_frontmatter": "athenaeum.models",
    "slugify": "athenaeum.models",
    # athenaeum.store
    "FilesystemStore": "athenaeum.store",
    "Lease": "athenaeum.store",
    "LeaseHeldError": "athenaeum.store",
    "ObjectMeta": "athenaeum.store",
    "Record": "athenaeum.store",
    "Store": "athenaeum.store",
    "StoreCapabilities": "athenaeum.store",
    "StoreConflictError": "athenaeum.store",
    "StoreKey": "athenaeum.store",
    "StoreKeyError": "athenaeum.store",
    "UnknownSurfaceError": "athenaeum.store",
}


def __getattr__(name: str) -> Any:
    """Resolve a public re-export on first access (PEP 562).

    ``__version__`` is included: reading it costs an ``importlib.metadata``
    import (~20 ms measured), which is real money against a per-invocation
    budget and is wasted on every command that never asks for the version.
    """
    if name == "__version__":
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as _pkg_version

        try:
            # Single-sourced from the installed distribution's metadata (issue
            # athenaeum#555, L12) -- hatchling populates this from
            # pyproject.toml's `version` at build time, so this constant and
            # pyproject.toml can never drift apart the way a hand-duplicated
            # literal here could (and once did: athenaeum#555).
            value: Any = _pkg_version("athenaeum")
        except PackageNotFoundError:
            # Editable/source checkout with no installed dist metadata (e.g.
            # running straight from a git clone without `pip install`). Not
            # expected in this repo's supported workflows (the dev extra always
            # installs the package), but fail soft rather than raising at
            # attribute-access time.
            value = "0.0.0+unknown"
        globals()["__version__"] = value
        return value

    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    from importlib import import_module

    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Keep the package's advertised surface complete under lazy resolution.

    Without this, ``dir(athenaeum)`` would list only what has already been
    touched, so tab-completion and surface-extraction tooling
    (``tests/public_surface.py``) would see a surface that shrinks and grows
    depending on what ran first.
    """
    return sorted(set(globals()) | set(_LAZY_EXPORTS) | {"__version__"})
