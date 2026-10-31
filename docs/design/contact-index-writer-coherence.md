<!-- SPDX-License-Identifier: Apache-2.0 -->

# Design: a persisted, erasure-reachable `ExcludedRecordIndex` cache, and why `mark_bounced` stays correct underneath it (athenaeum#2182)

**Status: SHIPPED.** Describes `src/athenaeum/contact_index_cache.py` and the
`tests/test_contact_index_cache.py` suite that pins it, plus the one-line
`cache_dir` wiring into `identity_resolution.resolve_handle_query`'s
email-handle branch.

## 1. What athenaeum#2179 deliberately left undone, and why this is safe to finish

`pii.ExcludedRecordIndex` (athenaeum#883) is a single-pass, **in-memory-only**
index over one excluded contacts surface: uid and every listed address, built
from one `iter_contact_records` scan. athenaeum#2179 built it exactly that way
on purpose — its own docstring: "a cache of it would be a copy of excluded PII
that the erasure cascade does not reach." Persisting the map of
`raw address -> record path` the index holds in memory would have created a
second store of PII sitting outside the erasure cascade's reach entirely.

This issue persists the index anyway, closing that gap rather than reopening
it, by changing what gets written to disk:

- **Never a raw address.** The persisted cache stores only the HMAC-SHA256
  digest of each address (keyed under the SAME `_erasure_hmac_key`
  `athenaeum.erasure` already mints/purges — `erasure.py:139` names the file,
  `erasure.py:157` resolves its path), plus the record's `uid` (an opaque
  8-char-hex id, never itself erasure-class content — see
  `models.py:50`'s `generate_uid`). `tests/test_contact_index_cache.py::TestKeyedIdentifiers`
  writes the cache from a fixture and asserts no fixture address, in any
  case-folding, appears anywhere in the cache file's bytes.
- **In the erasure cascade's delete set.** `erase_contact_record` (in the
  same module) deletes the record file and drops its cache entry in one
  call, and — handed the caller's live index — calls the new
  `ExcludedRecordIndex.unregister` so the SAME in-memory index a caller is
  still holding stops resolving the address immediately, with no reload.
  `TestErasureIntegration` pins this directly.

## 2. Why the key-purge property is inherited, not reinvented

`erasure.purge_erasure_key` already documents: "every hash previously
computed with the purged key is now permanently unlinkable from the content
that produced it." The persisted contact-index cache's digests are computed
with `erasure.erasure_content_hash` under that exact key, so the same
property falls out for free: delete `_erasure_hmac_key` and every digest in
every existing `contact-index/*.json` cache file becomes permanently
unmatchable against a freshly-computed one. Nothing in this module re-derives
or weakens that guarantee.

The one piece this module adds on top: a cache file also carries a
`key_fingerprint` header (`erasure_content_hash` of a fixed marker string,
under the live key). A key rotation or a purge-and-remint changes the
fingerprint, and `_read_cache` treats a fingerprint mismatch exactly like a
corrupt cache — discarded wholesale, rebuilt from scratch under the new key.
Reusing an old digest against a new key would silently answer *wrong*
(a digest that happened to collide, or more realistically, simply never
matching again even for addresses that were never erased); discarding the
whole cache on mismatch means the cache can only ever cost a rebuild, never a
wrong answer — the same invariant `entity_index_cache.py` states for its own
corrupt-cache path, extended to cover key rotation too.

## 3. Writer coherence — does `mark_bounced` still see a correct index?

`pii.mark_bounced` (pii.py:5096) and the librarian's compile loop
(`librarian.py:8906-8911`) already solved the athenaeum#850 staleness problem
for the in-memory index with a specific, narrow discipline:

> a ONE-SHOT load, plus an explicit `register` that the WRITER calls after
> every write. ... Nothing else invalidates it: after the load, only
> `register` mutates it, which is what makes resolution stable across a
> batch of interleaved writes.
> — `pii.py:2963-2967`

**This module changes nothing about that discipline — it only changes how
the initial load is populated.** Concretely:

- `CachedExcludedRecordIndex` (this module) is still an
  `ExcludedRecordIndex` subclass. `register`/`unregister` are the base
  class's own methods (the latter newly added by this issue, the former
  untouched) — both operate purely on the base class's plaintext
  `_by_uid`/`_by_identifier` maps, in memory, exactly as before. The
  subclass's own cache-backed maps (`_cached_by_uid`/`_cached_digest`) are
  read-only data from the last persisted-cache load; `register` never
  touches them, and every accessor (`by_uid`, `all_by_uid`,
  `all_by_identifier`, `uids`, `uid_map`) reads the cache maps FIRST and
  whatever `register` has since added SECOND, de-duplicated by path. A
  record `register`-ed mid-batch — whether brand new or an existing one
  `mark_bounced` just merged an identifier onto — is visible to the very
  next lookup in the same process, with the same ordering guarantee
  (first-seen-in-`iter_contact_records`-order wins) the base class already
  provides, because the cache-sourced entries were themselves built in that
  exact scan order.
- **Nothing in this issue wires the librarian's compile loop onto the
  persisted cache.** `librarian.py:8906-8911` still constructs a plain
  `pii.ExcludedRecordIndex(contacts_surface_root(...))` directly, exactly as
  it does today. `resolve_handle_query`'s own docstring already carried this
  exact precedent for the sibling `entity_index_cache`: "The recall CLI and
  MCP tool pass it; the librarian's compile-time caller does not" — this
  issue's `cache_dir` wiring for the contacts surface follows the identical
  rule, for the identical reason: the compile loop already pays the
  O(corpus) scan once per run and amortizes it correctly across every raw
  file in that run (that is what athenaeum#883 built); a persisted cache
  would save it nothing there, while adding a write it would need to
  reconcile against mid-batch writes it is itself producing.
- **Cross-process / cross-run correctness is the SAME stat-invalidation
  discipline `entity_index_cache.py` already uses, not a new one.** When
  `mark_bounced` (or the compile loop's bootstrap-mint path) writes a
  contact record — new or merged — the file's mtime changes. The *next*
  `contact_index_cache.load_with_stats` call (the next `recall`/handle-query
  invocation that passes `cache_dir`) re-globs `iter_contact_records` fresh,
  `stat`s every file, finds that one file's stamp no longer matches the
  cached stamp, and re-parses exactly that file through the same
  `pii.read_bounce_record` the uncached path uses — recomputing its digests
  under the current key and folding the corrected answer back in. A record
  that disappeared (folded/absorbed — `contact_record_fold.py`) is dropped
  the same way `entity_index_cache.py` drops a deleted wiki page: it simply
  does not appear in the fresh glob, so it is never retained from the old
  cache (`removed = len(set(old) - set(fresh))`).

**Net effect:** a single process holding one `CachedExcludedRecordIndex`
across a batch of `mark_bounced` calls (if a future caller ever threads
`cache_dir` into that path — none does today) would be exactly as correct as
holding a plain `ExcludedRecordIndex` is today, for exactly the same reason:
`register` is the only thing that mutates a loaded index's answers, and nothing
about this issue changes `register`. Across process boundaries, the persisted
cache self-heals via per-file stat comparison, the same way `entity_index_cache.py`
already does for the compiled wiki — no new invalidation mechanism was
invented for this property; it is "copy the pattern" in the literal sense the
issue asked for.

## 4. The erasure-delete primitive this issue had to add (a scope note)

No code path in this repository deletes a contact record today. `erasure.py`'s
own module docstring is explicit about why a comparable gap exists for its
AC1-AC9 classification/taint logic: "No production caller was migrated onto
this module in THIS slice" — the mechanism ships fully implemented and fully
tested, with live wiring onto a real write path deferred to a later,
store-dependent issue (`erasure.py`'s own note names athenaeum#1116 as that
slice for its own logic).

`contact_index_cache.erase_contact_record` is the same shape, for the same
reason, deliberately scoped the same way: this issue's two hard ACs (purge-or-
atomically-invalidate the persisted index in the SAME operation as the record
delete, with a test proving the erased address is unresolvable immediately,
no later load needed) require a concrete "delete one contact record" function
to exist and to be tested, and none did. `erase_contact_record` is that
function — delete the file, invalidate the cache entry, and (when handed the
caller's live index) `unregister` it from memory too, all in one call. Wiring
a real GDPR/right-to-erasure CLI or API surface onto it is out of scope here,
exactly as "any change to what `resolve_handle_query` returns" is out of
scope — this issue is the mechanism and its proof, not the operator-facing
erasure workflow.
