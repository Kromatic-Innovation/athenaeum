# SPDX-License-Identifier: Apache-2.0
"""A persisted, stat-validated cache for :class:`~athenaeum.models.EntityIndex`.

Building an ``EntityIndex`` parses the frontmatter of every ``wiki/*.md``
page. On a corpus of tens of thousands of pages that is most of the wall time
of a one-shot CLI call that only needs one uid's page. This module keeps the
per-page facts the index is built from (:class:`~athenaeum.models.PageIndexRecord`)
on disk, and re-parses only the pages whose file changed.

**Correctness first.** Every load lists the wiki exactly as
:meth:`EntityIndex._load` does (``sorted(wiki_root.glob("*.md"))``, skipping
``_``-prefixed names) and ``stat``s every page. A cached record is reused only
when ``(st_mtime_ns, st_ctime_ns, st_size, st_ino)`` all match. Anything else
is re-read through :meth:`EntityIndex.read_page_record`, the same function the
full scan uses. Pages that disappeared are dropped. Records are then applied in
sorted path order, because name and alias keys are last-write-wins. A directory
mtime check alone is not enough: an in-place edit does not change it. The page
is ``stat``-ed BEFORE it is read, so a write racing the read leaves an old stat
on the cached entry and is re-read next time. The cache can only cost a re-parse,
never change an answer.

**Missing or corrupt cache:** any read, decode, version or shape problem
discards the whole cache and rebuilds from the pages. It never raises because
of the cache, and a failed cache write only costs the next call a rebuild.

**Concurrency:** writes go through :func:`athenaeum.atomic_io.atomic_write_text`
(a unique same-directory temp file, then ``os.replace``). Several processes may
load and rebuild at once. Readers see a complete old or new file, never a torn
one. A slower writer may replace a newer snapshot with an older one, which is
still correct because every entry is re-validated against ``stat`` on the
next load.

**What is stored, and its mode:** only identity fields: uid, name, resolved
type and aliases, keyed by page filename. No body text and no other
frontmatter. Contact data lives on the excluded surface, and this cache never
reads it. The file is written ``0600`` inside a ``0700`` directory under the
athenaeum cache dir. That is the same posture as the search indexes, which
already hold full page text there. A page deleted from the wiki leaves the
cache at the next load of this cache.

Opt-in: nothing builds an ``EntityIndex`` through here unless the caller
passes a cache dir. ``EntityIndex(wiki_root)`` is unchanged.

Layering: L2. Imports only :mod:`athenaeum.models` (L1) and
:mod:`athenaeum.atomic_io` (L0); the caller resolves the cache dir.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from athenaeum.atomic_io import atomic_write_text
from athenaeum.models import EntityIndex, PageIndexRecord

log = logging.getLogger(__name__)

#: Bump when the on-disk shape or the per-page indexing rules change, so an
#: old cache is discarded instead of being read under new rules.
CACHE_FORMAT = 1

#: Subdirectory of the athenaeum cache dir that holds the cache files.
CACHE_SUBDIR = "entity-index"

_Stamp = tuple[int, int, int, int]


@dataclass(frozen=True)
class LoadStats:
    """What one :func:`load_with_stats` call did (for tests and timing)."""

    pages: int
    reparsed: int
    removed: int
    cache_was_valid: bool
    wrote: bool


def cache_path(wiki_root: Path, cache_dir: Path) -> Path:
    """The cache file for *wiki_root*: one file per resolved wiki directory."""
    digest = hashlib.sha256(str(Path(wiki_root).resolve()).encode("utf-8")).hexdigest()[:16]
    return Path(cache_dir) / CACHE_SUBDIR / f"{digest}.json"


def _stamp(st: os.stat_result) -> _Stamp:
    return (st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino)


def _read_cache(
    path: Path, wiki_root: Path
) -> dict[str, tuple[_Stamp, PageIndexRecord | None]] | None:
    """Parse the cache file, or ``None`` when it is missing or malformed in any way."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("entity index cache %s unreadable (%s); rebuilding", path, exc)
        return None
    try:
        if raw["format"] != CACHE_FORMAT or raw["wiki_root"] != str(Path(wiki_root).resolve()):
            return None
        pages: dict[str, tuple[_Stamp, PageIndexRecord | None]] = {}
        for name, entry in raw["pages"].items():
            stamp_raw, rec_raw = entry
            stamp = tuple(int(v) for v in stamp_raw)
            if len(stamp) != 4 or not isinstance(name, str):
                raise ValueError("bad stamp")
            record: PageIndexRecord | None = None
            if rec_raw is not None:
                uid, rname, rtype, aliases = rec_raw
                if not all(isinstance(v, str) for v in (uid, rname, rtype)) or not rname:
                    raise ValueError("bad record")
                if not isinstance(aliases, list) or not all(
                    isinstance(a, str) and a for a in aliases
                ):
                    raise ValueError("bad aliases")
                record = PageIndexRecord(uid, rname, rtype, tuple(aliases))
            pages[name] = (stamp, record)
        return pages
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("entity index cache %s malformed (%s); rebuilding", path, exc)
        return None


def _write_cache(
    path: Path, wiki_root: Path, pages: dict[str, tuple[_Stamp, PageIndexRecord | None]]
) -> bool:
    payload: dict[str, Any] = {
        "format": CACHE_FORMAT,
        "wiki_root": str(Path(wiki_root).resolve()),
        "pages": {
            name: [
                list(stamp),
                None if rec is None else [rec.uid, rec.name, rec.type, list(rec.aliases)],
            ]
            for name, (stamp, rec) in pages.items()
        },
    }
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # atomic_write_text keeps an existing file's mode; narrow a widened one
        # first so the rewrite lands 0600 (a new file is created 0600).
        try:
            if stat.S_IMODE(path.stat().st_mode) != 0o600:
                os.chmod(path, 0o600)
        except FileNotFoundError:
            pass
        atomic_write_text(path, json.dumps(payload, separators=(",", ":")))
        return True
    except OSError as exc:
        log.warning("entity index cache %s not written (%s)", path, exc)
        return False


def load_with_stats(wiki_root: Path, cache_dir: Path) -> tuple[EntityIndex, LoadStats]:
    """Build an :class:`EntityIndex` for *wiki_root*, re-parsing only changed pages."""
    wiki_root = Path(wiki_root)
    path = cache_path(wiki_root, cache_dir)
    cached = _read_cache(path, wiki_root)
    cache_was_valid = cached is not None
    old = cached or {}

    fresh: dict[str, tuple[_Stamp, PageIndexRecord | None]] = {}
    ordered: list[tuple[Path, PageIndexRecord]] = []
    reparsed = 0
    for fpath in sorted(wiki_root.glob("*.md")):
        if fpath.name.startswith("_"):
            continue
        try:
            stamp = _stamp(fpath.stat())
        except OSError:
            # Unstat-able (a broken symlink, a race with a delete): the full
            # scan's read would fail too and skip it. Not cached, so it is
            # retried next load.
            continue
        prior = old.get(fpath.name)
        if prior is not None and prior[0] == stamp:
            record = prior[1]
        else:
            # Issue athenaeum#2178: this call site only ever reads
            # ``parsed[0]`` (the PageIndexRecord) below -- never ``meta`` --
            # so it is safe to opt into the selective parse EntityIndex's
            # own ``_load`` uses, unlike callers that unpack ``meta`` too.
            parsed = EntityIndex.read_page_record(fpath, keys=EntityIndex._INDEX_KEYS)
            record = parsed[0] if parsed is not None else None
            reparsed += 1
        fresh[fpath.name] = (stamp, record)
        if record is not None:
            ordered.append((fpath, record))

    removed = len(set(old) - set(fresh))
    wrote = False
    if not cache_was_valid or reparsed or removed:
        wrote = _write_cache(path, wiki_root, fresh)

    index = EntityIndex.from_page_records(wiki_root, ordered)
    return index, LoadStats(
        pages=len(fresh),
        reparsed=reparsed,
        removed=removed,
        cache_was_valid=cache_was_valid,
        wrote=wrote,
    )


def load_entity_index(wiki_root: Path, cache_dir: Path) -> EntityIndex:
    """:func:`load_with_stats` without the stats."""
    return load_with_stats(wiki_root, cache_dir)[0]
