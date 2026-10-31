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

**The single-uid fast path** (issue athenaeum#2179, broadened to a shared helper
by athenaeum#2181): :func:`page_by_uid_filename`, :func:`locate_entity_page` and
:func:`entity_index_for_uid` are the ONE implementation of "a page named
``<uid>-*.md``/``<uid>.md`` whose frontmatter confirms *uid*, used only when
exactly one such page qualifies, else fall back to the persisted cache above."
Originally private to :mod:`athenaeum.identity_resolution`
(``_page_by_uid_filename``/``_locate_page``), moved here rather than copied
into each of the three single-uid readers that now share it
(:func:`athenaeum.mcp_server.entity_read`, :func:`athenaeum.pii.read_entity`,
:func:`athenaeum.pii.read_identifier_facts`) — this module already owns the
cache-fallback tier these functions complete, is L2 (below both ``pii`` and
``identity_resolution``), and needs no import neither of those two modules
would otherwise require.

Layering: L2. Imports only :mod:`athenaeum.models` (L1) and
:mod:`athenaeum.atomic_io` (L0); the caller resolves the cache dir.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
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
            parsed = EntityIndex.read_page_record(fpath)
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


#: A uid safe to splice into a filename glob: no path separator, no glob
#: metacharacter. Anything else skips the filename fast path.
_FILENAME_SAFE_UID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


def page_by_uid_filename(wiki_root: Path, uid: str) -> Path | None:
    """The one page named ``<uid>-*.md`` / ``<uid>.md`` whose frontmatter carries *uid*.

    Returns a path only when EXACTLY one such page exists and it indexes as an
    entity page (readable, a ``name:``, and a frontmatter ``uid:`` equal to
    *uid*: what :meth:`EntityIndex.has_entity_format` guarantees). Zero hits
    (a page whose filename does not carry its uid) or several hits return
    ``None`` so the caller falls back to a full index. Reads only the
    candidate pages.
    """
    wiki_root = Path(wiki_root)
    if not _FILENAME_SAFE_UID.fullmatch(uid):
        return None
    candidates = sorted(wiki_root.glob(f"{uid}-*.md"))
    exact = wiki_root / f"{uid}.md"
    if exact.is_file():
        candidates.append(exact)
    verified: list[Path] = []
    for fpath in candidates:
        parsed = EntityIndex.read_page_record(fpath)
        if parsed is not None and parsed[0].uid == uid:
            verified.append(fpath)
    return verified[0] if len(verified) == 1 else None


def locate_entity_page(
    wiki_root: Path,
    uid: str,
    *,
    entity_index: EntityIndex | None = None,
    cache_dir: Path | None = None,
) -> Path | None:
    """Resolve a known *uid* to its entity page, or ``None`` (orphan).

    - *entity_index* given: that index decides, exactly as before.
    - *cache_dir* given (and *entity_index* not): the filename fast path
      (:func:`page_by_uid_filename`), then the persisted index above when the
      fast path is not conclusive.
    - neither: a fresh full :class:`EntityIndex`, the pre-cache behaviour.
    """
    wiki_root = Path(wiki_root)
    if entity_index is None and cache_dir is not None:
        fast = page_by_uid_filename(wiki_root, uid)
        if fast is not None:
            return fast
        entity_index = load_entity_index(wiki_root, cache_dir)
    index = entity_index if entity_index is not None else EntityIndex(wiki_root)
    page_path = index.get_by_uid(uid)
    if page_path is None or not page_path.exists() or not index.has_entity_format(page_path):
        return None
    return page_path


def entity_index_for_uid(
    wiki_root: Path,
    uid: str,
    *,
    cache_dir: Path | None = None,
) -> EntityIndex:
    """An :class:`EntityIndex` whose ``get_by_uid(uid)`` answers correctly,
    built as cheaply as the fast-path/cache mechanism allows (issue athenaeum#2181).

    For a caller that needs an actual index object rather than a bare path —
    :func:`athenaeum.mcp_server.entity_read` and :func:`athenaeum.pii.read_entity`
    both pass the result onward to code that calls ``.get_by_uid`` again, and
    the former also registers a non-wiki-root fallback hit (issue athenaeum#1394)
    directly into the returned index's ``_by_uid`` map.

    - *cache_dir* given and exactly one filename-fast-path match verifies for
      *uid*: returns a single-entry index built with
      :meth:`EntityIndex.from_page_records` (no directory scan beyond the one
      glob, no frontmatter parse beyond that one page) — this is the case
      AC2 pins as "no full ``EntityIndex`` build on a fast-path hit".
    - *cache_dir* given and the fast path is not conclusive: the persisted,
      stat-validated cache (:func:`load_entity_index`).
    - *cache_dir* ``None``: a fresh full :class:`EntityIndex`, the pre-cache
      behaviour, byte-identical to callers that have not opted in.
    """
    wiki_root = Path(wiki_root)
    if cache_dir is not None:
        fast = page_by_uid_filename(wiki_root, uid)
        if fast is not None:
            parsed = EntityIndex.read_page_record(fast)
            if parsed is not None:
                return EntityIndex.from_page_records(wiki_root, [(fast, parsed[0])])
        return load_entity_index(wiki_root, cache_dir)
    return EntityIndex(wiki_root)
