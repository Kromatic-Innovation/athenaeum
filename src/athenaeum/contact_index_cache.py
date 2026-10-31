# SPDX-License-Identifier: Apache-2.0
"""A persisted, HMAC-keyed, erasure-reachable cache for :class:`~athenaeum.pii.ExcludedRecordIndex`.

athenaeum#2179 built :class:`~athenaeum.pii.ExcludedRecordIndex` as a
single-pass, in-memory-only index over the excluded contacts surface,
deliberately NOT persisted: "a cache of it would be a copy of excluded PII
that the erasure cascade does not reach." This module is the persisted cache
athenaeum#2182 builds anyway, closing exactly that gap rather than reopening
it:

- **Never a raw address on disk.** The cache stores, per contact-record
  file: its ``uid`` (an opaque 8-char-hex id — :data:`athenaeum.models.generate_uid`
  — never itself erasure-class content) and the HMAC-SHA256 digest of every
  address the record lists, keyed under the SAME per-corpus
  ``_erasure_hmac_key`` :mod:`athenaeum.erasure` already mints and purges
  (:func:`athenaeum.erasure.load_or_create_erasure_key`). No plaintext
  address is ever written to the cache file. Purging that key (
  :func:`athenaeum.erasure.purge_erasure_key`) makes every digest in an
  existing cache file permanently unmatchable against a freshly-computed
  one — the SAME "purge renders it inert" property :mod:`athenaeum.erasure`
  already documents for its own AC1 hashes, inherited here rather than
  reinvented. A cache file whose ``key_fingerprint`` header no longer
  matches the current key (because the key rotated or was purged and
  re-minted) is treated as fully invalid and rebuilt from scratch under the
  new key — reusing an old digest under a new key would silently answer
  wrong, never merely slow.
- **In the erasure cascade's delete set.** :func:`erase_contact_record`
  deletes the record file AND drops its entry from the persisted cache, in
  one call — never two steps a caller could complete only the first of.
  When handed the caller's already-loaded index too, it also
  :meth:`~athenaeum.pii.ExcludedRecordIndex.unregister`\\ s the path from
  it, so a lookup made immediately afterward in the SAME process — no
  reload — already cannot resolve the erased address. No production caller
  deletes a contact record for erasure purposes yet in this repo (mirroring
  :mod:`athenaeum.erasure`'s own precedent: its module docstring states "No
  production caller was migrated onto this module in THIS slice" for the
  classification logic, with live wiring deferred to a later, store-
  dependent issue). This function is that same shape: the erasure-delete
  primitive, fully implemented and fully tested now, with wiring a future
  GDPR/right-to-erasure caller onto it left for when that caller exists.
- **Invalidation discipline copied from :mod:`athenaeum.entity_index_cache`**,
  not reinvented: every record is ``stat``-ed on every load
  (``(st_mtime_ns, st_ctime_ns, st_size, st_ino)``); a cache entry is
  reused only when its stamp still matches; anything else is re-read
  through :func:`athenaeum.pii.read_bounce_record`, the same function the
  unindexed scan uses; a corrupt, wrong-version, wrong-root or
  wrong-key-fingerprint cache is discarded wholesale and rebuilt; writes are
  atomic (temp file + ``os.replace``, via :func:`athenaeum.atomic_io.atomic_write_text`);
  records are applied in :func:`athenaeum.pii.iter_contact_records`'s own
  sorted order, preserving first-match-wins. The cache can only cost a
  re-parse, never change an answer (AC1).
- **Modes at least as tight as the PII store it caches.** ``0600`` inside a
  ``0700`` directory, on create AND on rewrite (the rewrite path narrows an
  unexpectedly-wide existing file before replacing it — see :func:`_write_cache`
  — because :func:`athenaeum.atomic_io.atomic_write_text` otherwise
  preserves whatever mode the file already had).
- **Writer coherence (the athenaeum#850 staleness concern, restated for the
  persisted layer).** See `docs/design/contact-index-writer-coherence.md`
  for the full analysis; the short version is that nothing changes for
  :func:`athenaeum.pii.mark_bounced`'s existing one-shot-load-plus-explicit-
  ``register`` discipline — this module only changes how the *initial* load
  is populated (from the persisted cache instead of a fresh scan), never
  what ``register``/``unregister`` do, which stay purely in-memory exactly
  as :class:`~athenaeum.pii.ExcludedRecordIndex` already defines them.

**A corrupt or unavailable erasure key never falls back to an unkeyed
hash.** If :func:`athenaeum.erasure.load_or_create_erasure_key` raises (a
corrupt key file, or an unwritable cache dir), :func:`load_with_stats`
catches it, logs a warning, and returns a plain, uncached
:class:`~athenaeum.pii.ExcludedRecordIndex` for THIS call only (no reads or
writes to the cache file at all) — mirroring :mod:`athenaeum.entity_index_cache`'s
"the cache can only cost a rebuild, never a wrong answer" contract, extended
to cover the key as well as the index file.

Opt-in, exactly like :mod:`athenaeum.entity_index_cache`: nothing builds a
cached index unless the caller passes a cache dir.
``pii.ExcludedRecordIndex(contacts_root)`` is unchanged.

Layering: L3, peer to :mod:`athenaeum.pii` and :mod:`athenaeum.erasure` (both
imported at module scope — this module's whole job is joining them), plus
:mod:`athenaeum.atomic_io` (L0).
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

from athenaeum import pii
from athenaeum.atomic_io import atomic_write_text
from athenaeum.erasure import ErasureKeyError, erasure_content_hash, load_or_create_erasure_key

log = logging.getLogger(__name__)

#: Bump when the on-disk shape changes, so an old cache is discarded instead
#: of being read under new rules.
CACHE_FORMAT = 1

#: Subdirectory of the athenaeum cache dir that holds the cache files.
CACHE_SUBDIR = "contact-index"

#: Fixed marker HMAC'd under the live erasure key to fingerprint it, so a
#: cache file can detect "this was built under a DIFFERENT key" (rotation or
#: purge-and-remint) without ever storing the key itself. Never used for
#: anything but this comparison.
_KEY_FINGERPRINT_MARKER = "athenaeum.contact_index_cache.key-fingerprint.v1"

#: A valid HMAC-SHA256 hex digest shape — defense in depth against a hand-
#: edited or truncated cache file smuggling a non-digest string through.
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

_Stamp = tuple[int, int, int, int]


@dataclass(frozen=True)
class _CachedRecord:
    """What is kept, per contact-record file, between loads."""

    uid: str | None
    identifier_hmacs: tuple[str, ...]


@dataclass(frozen=True)
class LoadStats:
    """What one :func:`load_with_stats` call did (for tests and timing)."""

    records: int
    reparsed: int
    removed: int
    cache_was_valid: bool
    wrote: bool
    #: True iff the erasure key could not be loaded/minted at all, in which
    #: case *records*/*reparsed*/*removed* are always ``0`` and no cache was
    #: read or written — the call answered from a fresh, uncached
    #: :class:`~athenaeum.pii.ExcludedRecordIndex` instead.
    key_unavailable: bool = False


def cache_path(contacts_root: Path, cache_dir: Path) -> Path:
    """The cache file for *contacts_root*: one file per resolved surface root."""
    digest = hashlib.sha256(str(Path(contacts_root).resolve()).encode("utf-8")).hexdigest()[:16]
    return Path(cache_dir) / CACHE_SUBDIR / f"{digest}.json"


def _stamp(st: os.stat_result) -> _Stamp:
    return (st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino)


def _key_fingerprint(key: bytes) -> str:
    return erasure_content_hash(_KEY_FINGERPRINT_MARKER, key=key)


def _record_key(contacts_root: Path, record_path: Path) -> str:
    """*record_path* rendered relative to *contacts_root*, POSIX-style.

    The dict key both :func:`load_with_stats` and :func:`invalidate_cached_record`
    use to identify one record — the same string either way, because both
    resolve through this one function. Falls back to the bare filename for a
    path outside *contacts_root* entirely (should not happen in practice;
    never raises).
    """
    root = Path(contacts_root).resolve()
    try:
        return Path(record_path).resolve().relative_to(root).as_posix()
    except ValueError:
        return Path(record_path).name


def _read_cache(
    path: Path, contacts_root: Path, key_fingerprint: str
) -> dict[str, tuple[_Stamp, _CachedRecord]] | None:
    """Parse the cache file, or ``None`` when missing, malformed, or built under a different key."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("contact index cache %s unreadable (%s); rebuilding", path, exc)
        return None
    try:
        if (
            raw["format"] != CACHE_FORMAT
            or raw["contacts_root"] != str(Path(contacts_root).resolve())
            or raw["key_fingerprint"] != key_fingerprint
        ):
            return None
        records: dict[str, tuple[_Stamp, _CachedRecord]] = {}
        for rel, entry in raw["records"].items():
            stamp_raw, uid_raw, hmacs_raw = entry
            if not isinstance(rel, str):
                raise ValueError("bad key")
            stamp = tuple(int(v) for v in stamp_raw)
            if len(stamp) != 4:
                raise ValueError("bad stamp")
            if uid_raw is not None and not isinstance(uid_raw, str):
                raise ValueError("bad uid")
            if not isinstance(hmacs_raw, list) or not all(
                isinstance(h, str) and _HEX64_RE.match(h) for h in hmacs_raw
            ):
                raise ValueError("bad hmacs")
            records[rel] = (
                (stamp[0], stamp[1], stamp[2], stamp[3]),
                _CachedRecord(uid=uid_raw, identifier_hmacs=tuple(hmacs_raw)),
            )
        return records
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("contact index cache %s malformed (%s); rebuilding", path, exc)
        return None


def _write_cache(
    path: Path,
    contacts_root: Path,
    key_fingerprint: str,
    records: dict[str, tuple[_Stamp, _CachedRecord]],
) -> bool:
    payload: dict[str, Any] = {
        "format": CACHE_FORMAT,
        "contacts_root": str(Path(contacts_root).resolve()),
        "key_fingerprint": key_fingerprint,
        "records": {
            rel: [list(stamp), rec.uid, list(rec.identifier_hmacs)]
            for rel, (stamp, rec) in records.items()
        },
    }
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # atomic_write_text keeps an existing file's mode; narrow a widened
        # one first so the rewrite lands 0600 (a new file is created 0600).
        try:
            if stat.S_IMODE(path.stat().st_mode) != 0o600:
                os.chmod(path, 0o600)
        except FileNotFoundError:
            pass
        atomic_write_text(path, json.dumps(payload, separators=(",", ":")))
        return True
    except OSError as exc:
        log.warning("contact index cache %s not written (%s)", path, exc)
        return False


def _build_record(fpath: Path, key: bytes) -> _CachedRecord:
    """Read *fpath* fresh and reduce it to what the cache keeps: uid + HMAC digests.

    Reads exactly the fields :class:`~athenaeum.pii.ExcludedRecordIndex._index_record`
    reads (``uid``, and :func:`athenaeum.pii.identifiers_on_record`) so a
    cached answer and a freshly-scanned one are built from the same facts.
    Deduplicates digests (a record cannot usefully list the same address
    twice) but preserves first-seen order, matching the normalize-then-append
    discipline :func:`athenaeum.pii.identifiers_on_record` callers rely on.
    """
    meta = pii.read_bounce_record(fpath)
    uid = str(meta.get("uid", "")).strip() or None
    digests: list[str] = []
    seen: set[str] = set()
    for identifier in pii.identifiers_on_record(meta):
        normalized = pii.normalize_identifier(identifier)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        digests.append(erasure_content_hash(normalized, key=key))
    return _CachedRecord(uid=uid, identifier_hmacs=tuple(digests))


class CachedExcludedRecordIndex(pii.ExcludedRecordIndex):
    """An :class:`~athenaeum.pii.ExcludedRecordIndex` answered from the persisted cache.

    Never built directly — :func:`load_with_stats` constructs it from a
    just-validated cache plus whatever records needed re-parsing. Every
    accessor below is overridden to read the HMAC-digest-keyed cache maps
    instead of the base class's plaintext ``_by_uid``/``_by_identifier``
    (which stay EMPTY here at construction — ``_loaded`` is pre-set so the
    base class's expensive :meth:`~athenaeum.pii.ExcludedRecordIndex._ensure_loaded`
    scan never runs). :meth:`register` is intentionally NOT overridden: it
    is inherited unchanged, and continues to populate the base class's
    plaintext maps in memory exactly as it always has — see
    `docs/design/contact-index-writer-coherence.md`. Every accessor below
    therefore reads BOTH the pre-loaded cache maps and whatever ``register``
    has since added, cache-first, de-duplicated by path, which preserves
    first-match-wins (the cache was itself built in
    :func:`athenaeum.pii.iter_contact_records` order; anything ``register``
    adds is logically later).
    """

    def __init__(
        self,
        contacts_root: Path,
        by_uid: dict[str, list[Path]],
        by_digest: dict[str, list[Path]],
        erasure_key: bytes,
    ) -> None:
        super().__init__(contacts_root)
        self._loaded = True  # the cache already did the scan.
        self._cached_by_uid = by_uid
        self._cached_digest = by_digest
        self._erasure_key = erasure_key

    def by_uid(self, uid: str) -> Path | None:
        wanted = str(uid).strip()
        if not wanted:
            return None
        cached = self._cached_by_uid.get(wanted) or []
        extra = self._by_uid.get(wanted) or []
        combined = cached + [p for p in extra if p not in cached]
        return combined[0] if combined else None

    def all_by_uid(self, uid: str) -> list[Path]:
        wanted = str(uid).strip()
        if not wanted:
            return []
        cached = [p for p in (self._cached_by_uid.get(wanted) or ()) if p.exists()]
        extra = [p for p in (self._by_uid.get(wanted) or ()) if p.exists() and p not in cached]
        return cached + extra

    def all_by_identifier(self, identifier: str) -> list[Path]:
        key = pii.normalize_identifier(identifier)
        if not key:
            return []
        digest = erasure_content_hash(key, key=self._erasure_key)
        cached = list(self._cached_digest.get(digest) or ())
        extra = [p for p in (self._by_identifier.get(key) or ()) if p not in cached]
        return cached + extra

    def uids(self) -> list[str]:
        seen = list(self._cached_by_uid)
        seen += [u for u in self._by_uid if u not in self._cached_by_uid]
        return seen

    def uid_map(self) -> dict[str, Path]:
        result = {u: ps[0] for u, ps in self._cached_by_uid.items() if ps}
        for u, ps in self._by_uid.items():
            if u not in result and ps:
                result[u] = ps[0]
        return result

    def unregister(self, path: Path) -> None:
        """Drop *path* from BOTH the cached maps and whatever ``register`` added.

        The erasure-cascade counterpart to ``register`` (issue athenaeum#2182):
        a caller erasing a record calls this so an index already held across
        the erasure stops resolving through it immediately, with no reload.
        """
        super().unregister(path)
        target = Path(path)
        for mapping in (self._cached_by_uid, self._cached_digest):
            for key in list(mapping):
                if target in mapping[key]:
                    kept = [p for p in mapping[key] if p != target]
                    if kept:
                        mapping[key] = kept
                    else:
                        del mapping[key]


def load_with_stats(
    contacts_root: Path, cache_dir: Path
) -> tuple[pii.ExcludedRecordIndex, LoadStats]:
    """Build an :class:`~athenaeum.pii.ExcludedRecordIndex` over *contacts_root*.

    Re-parses only records whose ``stat`` changed since the last call.
    """
    contacts_root = Path(contacts_root)
    try:
        key = load_or_create_erasure_key(cache_dir)
    except (OSError, ErasureKeyError) as exc:
        log.warning(
            "contact index cache: erasure key unavailable (%s); answering uncached", exc
        )
        return pii.ExcludedRecordIndex(contacts_root), LoadStats(
            records=0, reparsed=0, removed=0, cache_was_valid=False, wrote=False,
            key_unavailable=True,
        )

    fingerprint = _key_fingerprint(key)
    path = cache_path(contacts_root, cache_dir)
    cached = _read_cache(path, contacts_root, fingerprint)
    cache_was_valid = cached is not None
    old = cached or {}

    fresh: dict[str, tuple[_Stamp, _CachedRecord]] = {}
    by_uid: dict[str, list[Path]] = {}
    by_digest: dict[str, list[Path]] = {}
    reparsed = 0
    for fpath in pii.iter_contact_records(contacts_root):
        rel = _record_key(contacts_root, fpath)
        try:
            stamp = _stamp(fpath.stat())
        except OSError:
            # Unstat-able (broken symlink, racing delete): the full scan's
            # read would fail too and skip it. Not cached, retried next load.
            continue
        prior = old.get(rel)
        if prior is not None and prior[0] == stamp:
            record = prior[1]
        else:
            record = _build_record(fpath, key)
            reparsed += 1
        fresh[rel] = (stamp, record)
        if record.uid:
            by_uid.setdefault(record.uid, []).append(fpath)
        for digest in record.identifier_hmacs:
            by_digest.setdefault(digest, []).append(fpath)

    removed = len(set(old) - set(fresh))
    wrote = False
    if not cache_was_valid or reparsed or removed:
        wrote = _write_cache(path, contacts_root, fingerprint, fresh)

    uid_collisions = sum(1 for paths in by_uid.values() if len(paths) > 1)
    address_collisions = sum(1 for paths in by_digest.values() if len(paths) > 1)
    pii._log_collision_summary(uid_collisions, address_collisions)

    index = CachedExcludedRecordIndex(contacts_root, by_uid, by_digest, key)
    return index, LoadStats(
        records=len(fresh),
        reparsed=reparsed,
        removed=removed,
        cache_was_valid=cache_was_valid,
        wrote=wrote,
    )


def load_contact_index(contacts_root: Path, cache_dir: Path) -> pii.ExcludedRecordIndex:
    """:func:`load_with_stats` without the stats."""
    return load_with_stats(contacts_root, cache_dir)[0]


def invalidate_cached_record(contacts_root: Path, cache_dir: Path, record_path: Path) -> bool:
    """Drop *record_path*'s entry from the persisted cache file, atomically.

    Called by :func:`erase_contact_record` so the cache never outlives a
    deleted record. Returns ``True`` iff the cache file was rewritten.
    ``False`` is not an error: no cache file, an unreadable/corrupt one, an
    unavailable erasure key, or one that never carried this record all mean
    there is nothing stale to remove — the next :func:`load_with_stats` call
    starts from (or rebuilds) a cache that already reflects the record's
    absence, since the live directory listing it re-globs every time no
    longer lists a deleted file.
    """
    try:
        key = load_or_create_erasure_key(cache_dir)
    except (OSError, ErasureKeyError):
        return False
    path = cache_path(contacts_root, cache_dir)
    cached = _read_cache(path, contacts_root, _key_fingerprint(key))
    if cached is None:
        return False
    rel = _record_key(contacts_root, record_path)
    if rel not in cached:
        return False
    del cached[rel]
    return _write_cache(path, contacts_root, _key_fingerprint(key), cached)


def erase_contact_record(
    contacts_root: Path,
    record_path: Path,
    *,
    cache_dir: Path,
    index: "pii.ExcludedRecordIndex | None" = None,
) -> bool:
    """Delete *record_path* and invalidate the persisted cache, in one call.

    The erasure-cascade primitive issue athenaeum#2182 requires: "An erasure
    must purge, or atomically invalidate, the persisted index in the SAME
    operation that deletes the records." The file is unlinked first, then
    the cache entry is dropped (:func:`invalidate_cached_record`) — ordered
    so a crash between the two steps leaves, at worst, a stale cache entry
    pointing at an already-deleted file, which the next load silently drops
    (it is never re-read as present, since ``iter_contact_records`` would no
    longer list it and :meth:`~athenaeum.pii.ExcludedRecordIndex.all_by_uid`-style
    callers already filter on existence). Both steps run unconditionally,
    even when the file was already gone, so a stale cache entry left by an
    out-of-band delete is still cleaned up.

    *index*: an already-loaded index (persisted or plain) to invalidate
    in-memory too, via :meth:`~athenaeum.pii.ExcludedRecordIndex.unregister` —
    so a caller holding *index* across this call sees the drop immediately,
    with NO reload (the hard requirement: an erased address must be
    unresolvable through the index right after this call returns). Omit it
    and only the on-disk cache is updated now; any live in-memory index
    (this process's or another's) catches up at its own next reload.

    Returns ``True`` iff *record_path* existed and was deleted.
    """
    record_path = Path(record_path)
    existed = record_path.exists()
    if existed:
        record_path.unlink()
    invalidate_cached_record(contacts_root, cache_dir, record_path)
    if index is not None:
        index.unregister(record_path)
    return existed
