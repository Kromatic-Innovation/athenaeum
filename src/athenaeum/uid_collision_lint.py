# SPDX-License-Identifier: Apache-2.0
"""Read-only lint: one uid on two pages inside a single surface (athenaeum#2993).

Layering: L2 (imports :mod:`athenaeum.models`, :mod:`athenaeum.storage`,
:mod:`athenaeum.config`, L1 and below).

Design: ``docs/design/federation-multi-writer-sync.md`` §7.4, §14 slice S2.

Integrate keeps two conforming writers from publishing a uid collision, but a
collision can already sit in a store (minted before that design, or kept by
"last write wins" inside one surface). This lint finds them. It is a finding
for a person: nothing is repaired, re-minted or raised here.

* **Inside one surface only.** Pages are grouped by ``(surface, uid)``. The
  same uid on one page in each of two surfaces is not reported (that is the
  cross-store case, sync S15).
* **Tombstones count.** A page folded into another still carries its uid, so
  it is part of a collision; the report marks which of the pages is one.
* **Both uid widths.** A uid is compared as written (lowercased), so an 8-hex
  and a 16-hex uid are two different ids and each width collides with itself.
* **Read-only.** Pages are read through the same surface walk the other lints
  use (:class:`athenaeum.models.PageLocator`); no page, index or ledger is
  written.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from athenaeum.config import load_config
from athenaeum.models import PageLocator, is_tombstone, parse_frontmatter
from athenaeum.storage import resolve_locator_surfaces

__all__ = [
    "CollidingPage",
    "UidCollision",
    "UidCollisionReport",
    "scan_uid_collisions",
]


@dataclass(frozen=True)
class CollidingPage:
    """One page carrying a colliding uid. *path* is relative to its surface root."""

    path: str
    tombstone: bool


@dataclass(frozen=True)
class UidCollision:
    """One uid carried by more than one page of one surface."""

    surface: str
    uid: str
    pages: tuple[CollidingPage, ...]


@dataclass(frozen=True)
class UidCollisionReport:
    pages_scanned: int
    collisions: tuple[UidCollision, ...]

    @property
    def ok(self) -> bool:
        return not self.collisions


def _relative(locator: PageLocator, surface: str, path: Path) -> str:
    roots = dict(locator.surfaces)
    root = roots.get(surface, locator.wiki_root)
    try:
        return path.relative_to(root).as_posix()
    except ValueError:  # pragma: no cover - defensive
        return path.name


def scan_uid_collisions(
    knowledge_root: Path,
    config: dict[str, Any] | None = None,
    surface: str | None = None,
) -> UidCollisionReport:
    """Report every uid carried by more than one page, per surface (read-only).

    *surface* limits the scan to one surface name (``wiki`` or a configured
    surface prefix); ``None`` scans each surface on its own.
    """
    if config is None:
        config = load_config(knowledge_root)
    locator = PageLocator(
        knowledge_root / "wiki", resolve_locator_surfaces(knowledge_root, config)
    )
    by_uid: dict[tuple[str, str], list[CollidingPage]] = defaultdict(list)
    scanned = 0
    for _key, path in locator.iter_pages():
        name = locator.surface_of(path)
        if surface is not None and name != surface:
            continue
        scanned += 1
        try:
            meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if not isinstance(meta, dict):
            continue
        uid = meta.get("uid")
        if not isinstance(uid, str) or not uid.strip():
            continue
        by_uid[(name, uid.strip().lower())].append(
            CollidingPage(_relative(locator, name, path), is_tombstone(meta))
        )
    found = [
        UidCollision(name, uid, tuple(sorted(pages, key=lambda p: p.path)))
        for (name, uid), pages in sorted(by_uid.items())
        if len(pages) > 1
    ]
    return UidCollisionReport(pages_scanned=scanned, collisions=tuple(found))
