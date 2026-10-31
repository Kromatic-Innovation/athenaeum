# Librarian email-named-candidate gate: index coherence analysis

**Status:** DESIGN, accepted and implemented under athenaeum#2180. Governing
principle, operator ruling verbatim: "We should not rebuild the index unless
genuinely needed."

## 1. The problem in one paragraph

`athenaeum.tiers.resolve_address_named_classifications` (the athenaeum#1126
email-named-candidate gate) calls
`athenaeum.identity_resolution.resolve_handle_query` once per email-shaped
classification, with an `excluded_index` but no `entity_index` and no
`cache_dir`. Every such call therefore builds a brand-new
`athenaeum.models.EntityIndex` — a full frontmatter parse of every `wiki/*.md`
page (~6s on a 26k-page corpus) — even though both call sites
(`librarian.process_one`, `batch.process_batch_run`) already hold exactly the
run's own `EntityIndex` as their `index` parameter, and already thread it to
sibling calls in the same scope (`pii_attribution.build_attribution_context`).
This is a write path: the librarian creates and edits wiki pages in the same
run that this gate reads, so before picking a fix, cache coherence has to be
checked explicitly rather than assumed.

## 2. The two candidate shapes

athenaeum#2179 gave `resolve_handle_query` two independent opt-in fast paths,
selected by which keyword argument the caller supplies:

1. **`cache_dir`** — on a cache miss, the filename fast path
   (`_page_by_uid_filename`: match `<uid>-*.md` / `<uid>.md`, verify by
   reading the candidate(s), accept only an exact single hit) tried first,
   then the persisted, stat-validated `EntityIndex` cache in
   `athenaeum.entity_index_cache` (keyed by `(mtime_ns, ctime_ns, size,
   inode)` per page, re-parsing only pages whose stat changed). Used today
   by the `recall` CLI and MCP tool, neither of which has a run-scoped index
   of its own to pass.
2. **`entity_index`** — the caller's own already-built `EntityIndex` decides
   outright; no disk access of any kind happens in `_locate_page`.

`identity_resolution._locate_page` makes these mutually exclusive in
practice: `entity_index` is checked first, and `cache_dir` is only consulted
when `entity_index is None`. So "combined" in the issue's proposal describes
the codebase as a whole (two caller shapes, each wired to the fast path that
fits what it already has in hand) — not a single call site using both at
once.

## 3. The four coherence questions

### 3.1 Pages created earlier in the run

**Question:** must a page created by an earlier candidate in the same run be
visible to a later candidate's uid lookup, and does a run-scoped index stay
coherent with that?

**Answer: yes, and it already does.** `EntityIndex.register()`
(`src/athenaeum/models.py:3553`) is the in-memory path both compile
transports already rely on for this: every `tier3_create` writes the page
with `atomic_write_text` and then calls `index.register(entity)` in the same
statement group (`src/athenaeum/librarian.py:2280-2281`,
`src/athenaeum/batch.py:2335`), before control ever returns to a later raw
file's `resolve_address_named_classifications` call. A run-scoped
`EntityIndex` passed as `entity_index` is the *same object*, not a copy —
`register()` mutates `self._by_uid`/`self._by_name`/`self._entity_format_paths`
in place, so a page created for candidate *N* is visible to candidate *N+1*
by construction, with no extra plumbing. The sibling call
`pii_attribution.build_attribution_context(..., entity_index=index)` at
`librarian.py:1936` and `librarian.py:3214` already depends on this same
guarantee for the same run.

### 3.2 Same-tick rewrites (the persisted-cache stat key)

**Question:** does every librarian write — including an atomic temp+rename —
change at least one of `(mtime_ns, ctime_ns, size, inode)`, and does the
stat-before-read ordering hold?

**Answer: yes, unconditionally, for every write through the one sanctioned
write primitive.** `athenaeum.atomic_io.atomic_write_text`
(`src/athenaeum/atomic_io.py:30-66`) is "Every store-path write anywhere in
the codebase" per its own docstring, and its mechanism is: `tempfile.mkstemp`
allocates a brand-new inode in the same directory, the new content is
written and fsynced to *that* inode, and `os.replace(tmp_path, path)`
atomically retargets the directory entry at `path` to the new inode. Two
consequences hold for every single write, not just most:

- `mtime_ns` (and `ctime_ns`) on the resulting file is set at that write's
  instant — it can never equal a prior write's timestamp for a real edit.
- The inode number changes on every rewrite of an *existing* path, because
  `os.replace` retargets the name to the freshly minted temp file's inode,
  never recycling the old file's inode in place. A brand-new page obviously
  gets a fresh inode too.

So `(mtime_ns, ctime_ns, size, inode)` changes on every write, create or
edit, with no exception — there is no write shape in this codebase that
could leave all four fields unchanged. The stat-before-read ordering the
issue asks about is a property of `entity_index_cache.load_with_stats`
itself (`src/athenaeum/entity_index_cache.py:177-179`: "The page is `stat`-ed
BEFORE it is read ... a write racing the read leaves an old stat on the
cached entry and is re-read next time"), and that ordering is unconditional
in that function's own control flow — it isn't something the gate's caller
has to arrange. This question is strictly about shape 1 (the persisted
cache); the run-scoped `EntityIndex` chosen here (§4) never consults that
cache at all.

### 3.3 Which answer wins

**Question:** where would a run-scoped in-memory index answer differently
from a fresh disk scan, and which answer is correct for the gate?

**Two divergence shapes exist, and both resolve in the in-memory index's
favor for this gate:**

- **A page the run has not yet written.** A fresh disk scan, run at the
  moment of the call, cannot see a page a *later* step in the run has not
  written yet either — this is not actually a divergence, it is the same
  answer by definition (neither view can see the future). It only matters
  that the in-memory index see what has *already* been written, which §3.1
  established.
- **A page removed from the live wiki after being registered** (today: the
  `log_demote` oversize-page disposition, `_perform_oversize_log_demote` in
  `src/athenaeum/tiers.py`, moves a page out of `wiki/` entirely into the
  preserved-log area). The in-memory index's `_by_uid` entry for that uid
  still points at the now-gone path. This is a real divergence from a fresh
  scan, which would not find that filename at all. It is harmless for this
  gate: `identity_resolution._locate_page` calls
  `index.get_by_uid(uid)` and then unconditionally checks
  `page_path.exists()` and `index.has_entity_format(page_path)` before
  returning it (`src/athenaeum/identity_resolution.py:591-595`) — a stale
  pointer to a deleted file fails `.exists()` and the gate correctly falls
  through to "orphan uid", the same answer a fresh scan would give. The
  in-memory index can be *stale*, never *wrong*, for this specific check.

No divergence case gives the in-memory index an answer a fresh scan would
contradict in a way this gate acts on. A run-scoped index is therefore safe
to use outright, with no extra invalidation logic needed for the shape of
writes this compile run actually performs today (§3.4 covers the one shape
it does not perform today).

### 3.4 Duplicate uid

**Question:** the fast path prefers a uid-prefixed filename; a page carrying
the same uid *without* that prefix would resolve differently from the full
scan. Does that matter here?

**This is a risk specific to shape 1 (the filename fast path /
`cache_dir`), and it is one of the reasons shape 1 is not used for this call
site.** `_page_by_uid_filename` (`src/athenaeum/identity_resolution.py:536`)
only globs `<uid>-*.md` and the exact `<uid>.md`; a page carrying the same
uid in its frontmatter under any other filename is invisible to that glob
and is never one of its candidates. `EntityIndex._apply_record`
(`src/athenaeum/models.py:3509-3521`) instead walks *every* page in sorted
path order and lets the last one win `_by_uid[uid]` — a different, and
possibly conflicting, selection rule from the filename fast path's "exactly
one verified glob hit or give up" rule. A mid-run rename that transiently
leaves two files carrying the same uid on disk (write the new name, then
unlink the old one — two separate filesystem operations, not one atomic
op) would expose this divergence window to shape 1's filename-pattern scan
and to a fresh `EntityIndex._load()` alike.

The run-scoped `EntityIndex` chosen here is structurally immune to this
specific race: it never re-globs the wiki directory mid-run at all. Its
`_by_uid` mapping reflects only what the run's one real scan found at
compile start, plus explicit `register()` calls for pages this run itself
created — it is never exposed to a transient on-disk duplicate that a fresh
scan (or the filename fast path) could observe mid-rename. No duplicate uids
exist on the deployment today, and no in-run rename path exists in the
current compile transports either (confirmed: the one operator command that
renames a uid-bearing page, `athenaeum storage migrate-pii
--rename-name-email`, is a separate CLI invocation — a different process,
with no run-scoped `index` object at all — never interleaved with
`librarian.process_one` / `batch.process_batch_run`'s own compile loop).
This is recorded as a reason to prefer the run-scoped index over the
filename/cache shape at this call site, not as a claim that the duplicate-uid
case is impossible in general.

## 4. Conclusion: which shape, and why

**Adopt shape 2 — thread the run's own `EntityIndex` through as
`entity_index`.** Concretely: `resolve_address_named_classifications` gained
an `entity_index: EntityIndex | None = None` keyword, threaded straight to
its `resolve_handle_query` call; both call sites
(`librarian.py`, `batch.py`) now pass their existing `index` parameter.

This is the shape the analysis above supports, not a coin flip:

- §3.1 shows the run-scoped index is *already* the authority this run relies
  on for every other identity lookup in the same scope — adding this call
  site to that pattern introduces no new staleness story, only joins an
  existing, already-correct one.
- §3.3 shows the one divergence a run-scoped index can have from a fresh
  scan (a demoted page) is made harmless by a check
  (`_locate_page`'s `.exists()` guard) that already exists and that this
  change does not touch.
- §3.4 shows the filename-fast-path shape (`cache_dir`) has a real,
  qualitatively different failure mode — it can pick a *different* page than
  a full scan under a transient on-disk duplicate uid — that the run-scoped
  index structurally cannot hit, because it never re-scans the directory.
- It costs nothing extra: no new disk cache file, no new `cache_dir`
  plumbing, and `AC2` ("at most one full index build") falls out for free,
  because the "one full index build" already happens once per run
  regardless of this change (`EntityIndex(ctx.wiki_root)` at the top of the
  compile run) — the gate now reuses it instead of building its own.

**Ruled out: `cache_dir` at this call site.** Not because the persisted
cache is unsafe in general — §3.2 confirms its stat-key invalidation is
sound for every write shape the librarian's own write primitive produces —
but because it is the wrong tool for a caller that already has a coherent,
zero-marginal-cost in-memory index in hand. Reaching for the disk cache here
would mean building and maintaining a second coherence story (disk stats)
for a problem the first one (the run's own `EntityIndex` plus `register()`)
already solves exactly, and would reintroduce the one real risk (§3.4)
that using the in-memory index avoids. `cache_dir` remains the correct
choice for the `recall` CLI/MCP callers, which have no run-scoped index to
offer instead — that boundary is unchanged by this issue.

## 5. Test coverage

- `tests/test_address_entity_resolution_1126.py` (`TestEntityIndexCoherence2180`
  as of athenaeum#2180): a fixture corpus that creates a page, renames it
  (via two-step unlink+rewrite, simulating the mid-run case §3.4 discusses),
  and edits another, all within one `process_one`/`process_batch_run`
  invocation, then asserts the gate's `kept`/`resolved`/`declined` outcome
  is identical whether `entity_index` is threaded or not (AC1).
- The same file pins the exact number of full `EntityIndex` builds
  (`EntityIndex.__init__`/`_load`) a compile run with N email-named
  candidates performs — at most one — by counting calls into
  `EntityIndex._load` across the run (AC2).
