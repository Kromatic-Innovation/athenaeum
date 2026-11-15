# SPDX-License-Identifier: Apache-2.0
"""Store-boundary rules: no merge across surfaces, no link to a more private one (athenaeum#2619).

Two rules, both about the line between per-type stores (surfaces):

* **Merges never cross a surface.** :func:`assert_same_surface` raises
  :class:`CrossStoreMergeError` for a set of pages that do not all live on one
  surface; :func:`same_surface` is the boolean form pair discovery uses.
* **A link may not point at a more private surface.** Every surface has a
  ``visibility`` rank (:func:`athenaeum.storage.surface_visibility`; higher =
  more widely shared, the default wiki is 0). A link from a page on surface S
  to a page on surface T is forbidden iff ``rank(T) < rank(S)``
  (:func:`link_forbidden`). A link from a private page to a shared page is fine.

This module holds the surface resolution and the two predicates; the link
scan, the write-boundary guard and the lint live in
:mod:`athenaeum.store_links`.

Layering: L2. Imports only :mod:`athenaeum.models` (:class:`PageLocator`) and
:mod:`athenaeum.storage` (L1).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from athenaeum.config import load_config
from athenaeum.models import PageLocator
from athenaeum.storage import resolve_locator_surfaces, surface_visibility


class StoreBoundaryError(ValueError):
    """Base of the named store-boundary refusals."""


class CrossStoreMergeError(StoreBoundaryError):
    """A merge was asked to combine pages that live on different surfaces."""


class StoreBoundaryLinkError(StoreBoundaryError):
    """A link from a more widely shared page to a more private page."""


def locator_for(wiki_root: Path, config: dict[str, Any] | None = None) -> PageLocator:
    """The multi-surface :class:`PageLocator` for the knowledge root holding *wiki_root*."""
    knowledge_root = Path(wiki_root).parent
    if config is None:
        config = load_config(knowledge_root)
    return PageLocator(wiki_root, resolve_locator_surfaces(knowledge_root, config))


#: The surface name :func:`surface_name_of` gives a path under no known surface root.
UNPLACED_SURFACE = "(unplaced)"


def extra_surface_roots(wiki_root: Path, config: dict[str, Any] | None = None) -> list[Path]:
    """The roots of the extra page surfaces beside *wiki_root* (``[]`` when none)."""
    return [Path(root) for _, root in locator_for(wiki_root, config).surfaces]


def surface_name_of(locator: PageLocator, path: Path) -> str:
    """The surface *path* lives on (symlinks resolved, as the locator's roots are).

    A path under neither ``wiki/`` nor an extra surface root (e.g. a page on a
    non-indexed surface) is :data:`UNPLACED_SURFACE`, never silently ``wiki`` --
    so a merge that reaches it is refused rather than waved through.
    """
    try:
        path = path.resolve()
    except OSError:  # pragma: no cover - defensive
        pass
    name = locator.surface_of(path)
    if name == PageLocator.WIKI_SURFACE:
        try:
            wiki = locator.wiki_root.resolve()
        except OSError:  # pragma: no cover - defensive
            wiki = locator.wiki_root
        if path != wiki and wiki not in path.parents:
            return UNPLACED_SURFACE
    return name


def same_surface(locator: PageLocator, *paths: Path) -> bool:
    """Whether every one of *paths* lives on a single surface."""
    return len({surface_name_of(locator, p) for p in paths}) <= 1


def assert_same_surface(locator: PageLocator, paths: Iterable[Path]) -> None:
    """Raise :class:`CrossStoreMergeError` unless every path is on one surface."""
    listed = list(paths)
    by_surface: dict[str, list[str]] = {}
    for p in listed:
        by_surface.setdefault(surface_name_of(locator, p), []).append(Path(p).name)
    if len(by_surface) > 1:
        detail = "; ".join(f"{s}: {', '.join(sorted(n))}" for s, n in sorted(by_surface.items()))
        raise CrossStoreMergeError(
            f"refusing to merge pages across stores ({detail}) -- a merge "
            "never crosses a surface"
        )


def link_forbidden(
    source_surface: str, target_surface: str, config: dict[str, Any] | None
) -> bool:
    """Whether a link from *source_surface* to *target_surface* breaks the rule."""
    return surface_visibility(target_surface, config) < surface_visibility(
        source_surface, config
    )
