<!-- SPDX-License-Identifier: Apache-2.0 -->

# Federation: multi-writer sync, conflict adjudication and wider uids

**Status:** DESIGN, NOT BUILT (Vitruvius Specify, 2026-11-18). The design note
athenaeum#2624 asks for, a child of the federation design epic athenaeum#2631.
Nothing here is running code. The note ends in build slices (§14) and in the
questions only the operator can answer (§15).

Code anchors are `path:symbol` on `develop` at `60f851e8`. Line numbers are
given to make a claim quick to check and will drift.

**Depends on, and does not design:**

- **Who may read, propose and compile**, where a store's decision queue is
  kept and who answers it: athenaeum#2623, the store access model
  note, written in parallel as `federation-store-access.md` in this
  directory. This note uses its terms (§3.4).
- **Trust and poisoning across writers**: athenaeum#2628, held for an external
  review. Out of scope here (§13).
- **Encryption at rest**: athenaeum#2627. This note is written so that it
  holds whether or not the store and the local index are encrypted (§10).

---

## 1. Summary

1. **The remote branch is the only lock.** Each writer compiles locally, then
   pushes without force. A push is an atomic compare-and-swap on the remote
   branch, so of two writers that finish together exactly one wins. The other
   fetches, integrates and pushes again. There is no cross-machine lease.
2. **Athenaeum merges pages; git only stores them.** A page changed by both
   sides is split into units (frontmatter keys, sections, lines) and merged
   three ways from the common ancestor. Git's line merge and conflict markers
   are never used on a page.
3. **Most conflicts need no model.** Changes to different units, list unions,
   append-only sections, record files and regenerated files are all resolved
   by rule (§5).
4. **Where a model adjudicates, it chooses and never writes.** For each unit
   both sides changed differently, the librarian picks the local text, the
   remote text or both in order, or says it cannot decide. It authors no new
   text. Every decision is recorded in the merge commit, and an undecided
   page is held for a person (§6).
5. **The same sequence syncs a store's bench and inbox.** The decision queue
   and its answers are merged on the bench, never in the pages repository,
   and two people who answer one item differently are never overruled by a
   rule (§8).
6. **New pages get 16-hex-character uids (64 random bits).** Existing 8-hex
   uids stay valid and are never rewritten. A collision is still detected at
   integrate time and repaired before it is published (§7).

**Where each acceptance criterion of athenaeum#2624 is answered.**

| Criterion | Where |
|---|---|
| Sync sequence and the rejected push | §4, and §8 for the bench and inbox |
| Handing a page conflict to a librarian: shown, may decide, recorded, cannot decide | §6.1 to §6.4 |
| Conflicts resolved without a model call | §5, §8 |
| Uid width, collision probability, existing uids, same uid minted twice | §7 |
| Two installs compiling at once, and the lease | §9 |
| No plaintext local index assumed | §10 |
| Build slices | §14 |

---

## 2. What is true today

| Behaviour | Anchor |
|---|---|
| The pre-run pull is `git pull --ff-only --autostash`. Any failure, including diverged history and a failed autostash re-apply, logs `athenaeum-pull-failed` and returns `False`. The run continues on the local tree. | `src/athenaeum/librarian.py:git_pull` (1063-1123; the command is line 1095) |
| The pull is called once before the run, for the knowledge repository and then for each surface that is its own repository with a remote. The result for the knowledge repository is discarded. | `src/athenaeum/librarian.py:_maybe_pull_before_run` (817-854), called from `_run_git_vcs_io` (7216) |
| The post-run push is a plain `git push`. A rejection logs `athenaeum-push-failed`, leaves the commits local and counts a failure streak. Nothing fetches or integrates afterwards. | `src/athenaeum/librarian.py:git_push` (960-1060), `_push_surface_repos` (912-937) |
| A page uid is `uuid.uuid4().hex[:8]`: 32 random bits. Nothing at the mint site checks it against existing pages. | `src/athenaeum/models.py:generate_uid` (50-52); mint site `src/athenaeum/tiers.py` (4337) |
| Two pages with one uid inside a single surface keep "last write wins". Only a collision across two surfaces raises. | `src/athenaeum/models.py:PageLocator.check_uid_unique` (3681-3691), `AmbiguousPageUidError` (3610) |
| A page's filename is `<uid>-<slug>.md`. | `src/athenaeum/models.py:WikiEntity.filename` (1921) |
| The run lock is an advisory `flock` on `<knowledge_root>/.athenaeum.lock`, and its docstring says "Scope is single-machine only". | `src/athenaeum/runlock.py` module docstring (8-13); `src/athenaeum/store.py:FilesystemStore.lease` (1798-1822) |
| Commits are made by `git add -A` then `git commit` in the knowledge repository and in each writable surface repository. | `src/athenaeum/store.py:FilesystemStore.snapshot` (1681-1712) |
| Which repositories are pulled and pushed is already computed per surface, and read-only mounts are pulled but never pushed. | `src/athenaeum/store.py:FilesystemStore.sync_repos` (1503), `push_repos` (1482), `read_only_pull_repos` (1494) |
| A configured type can already be merged by rule, with no model: sections are replaced or appended, list keys are unioned, ordered keys only move forward. | `src/athenaeum/structured_upsert.py:_merge_body` (227), `_merge_frontmatter` (462); `src/athenaeum/config.py:StructuredUpsertSpec` (9603) |

**The failure this note removes.** Two installs, run by Jordan Reyes and Priya
Raman at Acme Corp, both compile into one shared store. Jordan pushes first.
Priya's push is rejected and her commits stay local. On her next run the
fast-forward pull fails because her history has diverged, the run continues on
her local tree, and her next push is rejected again. Nothing in the code ever
brings the two histories back together. This is why the per-type stores epic
athenaeum#2630 holds the interim rule of one compiling install per shared
store.

---

## 3. Boundaries

### 3.1 Components

| Component | Owns | Does not own |
|---|---|---|
| **Store seam** (`athenaeum.store`) | Every git operation: fetch, find the common ancestor, read a file at a revision, list paths changed between two revisions, write a merge commit, push without force and report why a push failed. | Any opinion about page content. |
| **Page merge** (new, pure) | Given three versions of one page (ancestor, local, remote) and the type's merge rules, return the merged page and the list of units it could not merge. No git, no file access, no model. | Deciding contested units. |
| **Record merge** (new, pure) | Union of record files through each file's own sanctioned parser (§5.3). | Pages. |
| **Adjudicator** (new) | The model call for contested units, its prompt, its output check and its budget (§6). | Applying the result to git. |
| **Sync orchestrator** (new, called by the run) | The sequence in §4, retries, the merge-commit record, and holding a page for a person. | Compilation. |
| **Librarian run** | Unchanged compile phases. It calls the orchestrator where it calls `_maybe_pull_before_run` and `_maybe_push_after_run` today. | Git, merging. |

Data flows one way through them: store seam (three versions) → page merge →
adjudicator (contested units only) → orchestrator (merged tree) → store seam
(merge commit, push).

### 3.2 Repository split

All of this belongs in the athenaeum repository. It is engine mechanism, it
extends the published store contract, and it has no deployment content. No new
repository is needed.

### 3.3 Capability and configuration

- `StoreCapabilities` gains a `multi_writer` capability. Only a store that is
  versioned by git and has a remote can declare it. Non-git stores are out of
  scope (athenaeum#2624).
- **Integrate mode is declared by the store, not by each install.** The
  store's declaration, `_store/access.yaml` (athenaeum#2623 §5), carries:

  ```yaml
  sync:
    mode: integrate        # fast_forward (default) | integrate
    always_ask: []         # page types whose contested units always go to a person (§15, question 2)
  ```

  Every install then acts on one statement, and a change to it is a commit
  with an author. The access check (athenaeum#2623 §9) accepts more than one
  compile-role install only when the declaration says `integrate`.
- **A local setting may only narrow.** `sync_mode: fast_forward` under
  `storage.adapters.<name>` makes that one install never merge, so it stops
  pushing as soon as it diverges. A local `integrate` on a store that does
  not declare it is ignored and reported.
- **A store with no declaration behaves as today**: fast-forward only. That
  includes a knowledge repository. One person syncing a knowledge repository
  between two machines opts in with the same declaration file.
- The declared mode covers every location of the store: its pages, its bench
  and its inbox (§8).
- A `read_only` mount is unaffected. It is still pulled and never pushed.

### 3.4 Terms

These are the terms of athenaeum#2623, used here unchanged.

| Term | Meaning here |
|---|---|
| **Writer** | An install in a store's **compile role**. Only writers push the pages. |
| **Proposer** | An install that may push the store's inbox and nothing else. Not a writer. |
| **Steward** | A person in the compile role who answers decisions on the bench and does not push the pages. |
| **Pages, inbox, bench** | The store's three separately permissioned locations. |
| **Decision queue** | The store's open decisions. It is kept on the bench, or in the single compiling install's own state when there is no bench. |
| **`_store/`** | The directory in the pages repository for store-level files, including the declaration. |

---

## 4. Sync sequence for a multi-writer store

Each repository of a store in `integrate` mode runs this sequence,
independently of the others. This section describes the pages repository. The
bench and the inbox run the same sequence without the compile step (§8).

```
 1. lock        take the local run lock and the repository lease (§9)
 2. snapshot    commit any uncommitted change as a local commit
 3. fetch       fetch the remote branch
 4. integrate   bring the remote's commits into the local branch (§4.1)
 5. compile     the normal librarian run; commits as today
 6. push        push without force
 7. on reject   fetch, integrate, push again; bounded retries (§4.2)
 8. report      one row per repository and step in the run summary
```

Step 2 replaces `--autostash`. A stash that fails to re-apply after a merge is
one of today's unhandled failures (`librarian.py:git_pull` names it). A commit
cannot fail that way, and it gives the merge a clean local side.

Integrating before compiling (step 4) means the run reads the newest pages, so
it creates fewer duplicates and fewer conflicts than compiling first.

### 4.1 Integrate

1. If the remote has nothing new, do nothing.
2. If the local branch has no commits the remote lacks, fast-forward.
3. Otherwise the histories have diverged. Find the common ancestor, then:
   - List every path changed on each side since the ancestor.
   - A path changed on one side only takes that side's version.
   - A path changed on both sides goes to the merge for its kind of file
     (§5). This applies even when git could have merged the lines cleanly,
     because a clean line merge can still produce a broken page.
   - Check for uid collisions across the merged tree (§7.4).
   - Validate the result (§4.3).
   - Write one merge commit with two parents. Never rebase and never rewrite
     a commit that exists.

**All or nothing.** The merged tree and its commit are built first, and the
local branch is moved to it last. A crash before that point leaves the local
branch where it was. A crash never leaves a half-merged working tree or a
conflict marker in a page.

**Pages are paired by uid, not by path.** A page renamed on one side (its name
changed, so its slug changed) and edited on the other is still one page.

### 4.2 Rejected push

A push is rejected when the remote branch moved after the fetch. Then:

1. Fetch and integrate again (§4.1). This merges; it does not recompile. The
   cost of losing a race is one fetch and one merge.
2. Push again.
3. Stop after three attempts in one run (configurable), with a short random
   wait between attempts so two writers do not retry in step.
4. If every attempt is rejected, the commits stay local, the run reports the
   repository as `push: failed, reason: contended`, and the existing
   `athenaeum-push-failed` warning and failure-streak alert fire
   (`librarian.py:git_push`). The next run starts at step 2 and integrates
   first, so the histories converge instead of staying apart.

A push that fails for any other reason (no network, no permission) is not
retried in the run. It is reported exactly as today.

### 4.3 Validation before push

The orchestrator refuses to push a tree unless all of these hold for every
page the merge touched:

- The frontmatter parses, and `uid`, `type` and `name` are present.
- No uid names two pages (§7.4).
- No conflict marker is present.
- The store-boundary rules still hold: no merge across stores and no link
  from a shared page to a private page (`src/athenaeum/store_boundary.py`).

A page that fails is held (§6.4). If the tree cannot be made valid, nothing is
pushed and the run reports why.

### 4.4 Rewritten remote history

If the fetch shows that the remote branch no longer contains the commit this
install last saw there, someone rewrote the remote's history (for example to
erase personal data). The orchestrator **stops**: it does not merge and does
not push, and it raises a decision. Merging would put the erased content
straight back. How a shared store coordinates an erasure that rewrites history
is not designed here (§13).

---

## 5. Conflicts resolved without a model call

The merge is chosen by the kind of file, using the persistence classes the
artifact registry already declares (`src/athenaeum/store.py:PERSISTENCE_CLASSES`,
`ARTIFACT_REGISTRY`).

| Kind of file | Rule |
|---|---|
| **Page** (`source`) | Three-way page merge by unit (§5.1, §5.2). |
| **Record file** (`operational`, store-durable): the append-only ledgers | Union of records through the file's own parser (§5.3). |
| **Decision queue files** (the pending questions and merges lists and their archives) | Not expected in a shared pages repository. They are merged on the bench (§8.2). They are merged here only in the one case athenaeum#2623 §4 allows the bench to live in the pages repository: every reader is in the compile role. |
| **Regenerated file** (`derived`), and `wiki/_index.md` written by `librarian.py:rebuild_index` | Take either side, then regenerate. Never merged. |
| **Configuration** (`config`), and **every file under `_store/`** | Never merged automatically. The remote version stays and a decision is raised for a person. |
| **Machine-local file** (`operational`, machine-local) | Must not be tracked in a multi-writer repository. A slice adds the check (§14, S6b). |
| **Any path the registry does not name**, changed on both sides | Held by default: the remote version stays and a decision is raised. A new kind of file is never merged by a rule nobody wrote for it. |

The files athenaeum#2623 adds under `_store/` (the declaration and the review
criteria) are not in `ARTIFACT_REGISTRY` today. This note asks athenaeum#2623
S1 to register `_store/` as `config`. Until it does, the default row above
holds them.

### 5.1 Page units

A page is split into:

- one unit per frontmatter key;
- one unit per body section, using the same H2 splitter the structured upsert
  uses (`structured_upsert.py:_split_sections`);
- inside a section, lines.

For each unit, with the common ancestor as the base:

| Case | Result | Model? |
|---|---|---|
| Changed on neither side, on one side only, or identically on both | That value | No |
| Page deleted on both sides | Deleted | No |
| Page deleted on either side by a commit marked as an erasure (below) | Deleted. Erasure always wins. | No |
| `updated` differs | The later date | No |
| A list-valued key differs (`aliases`, `tags`, `sources`, and any key in the type's `union_keys`; never `audience`) | Three-way set merge: everything either side has, minus what either side removed | No |
| A key in the type's `ordered_keys` differs | The value further along the order | No |
| A section in the type's `append_sections` differs | Three-way line merge keeping the ancestor's order, then remote additions, then local additions, with duplicates dropped | No |
| Lines changed in different places of one section | Both changes | No |
| `uid`, `type`, `created`, `access` or `audience` differs | Held for a person. `access` and `audience` decide who may see the page (`src/athenaeum/models.py:effective_audience`, `is_page_authorized`). Neither a rule nor a model may widen or narrow that. | No (held) |
| Any other key changed differently on both sides | Contested | Yes |
| The same lines changed differently, or a `replace_sections` section changed differently | Contested | Yes |
| Page deleted on one side by a retire or a merge, edited on the other | Contested (see §15, question 4) | Yes |

**How an erasure is recognised.** The redaction ledger is in each install's
cache directory (`src/athenaeum/erasure.py:redaction_ledger_path`), so the
other side of a merge cannot read it. The erasing install therefore marks the
commit that deletes the page with a line `Athenaeum-Erasure: <uid>`, which
carries an id and nothing else. Any install that integrates sees the mark in
history. A deletion without the mark is an ordinary retire.

Page audience is its own axis and is not part of the store access model
(athenaeum#2623 §10). This note only guarantees that sync never changes it.

### 5.2 Types with a structured merge

A type listed under `librarian.structured_upsert` already declares, in config,
which sections replace, which append, which keys union and which ratchet
(`config.py:StructuredUpsertSpec`; the replace and drop rules came from
athenaeum#2587). The page merge reads the same declaration, so a type's rule
is stated once. For such a type only two cases can still be contested: a
`replace_sections` section and a `set_keys` scalar that both sides changed to
different values.

`structured_upsert.py:_merge_body` and `_merge_frontmatter` merge two inputs (a
page and a raw). The page merge needs three (ancestor, local, remote), because
only the ancestor tells an addition on one side from a removal on the other.
The slice extracts the shared section and list helpers. It does not call the
two-input functions with a page in the raw's place.

### 5.3 Record files

A record file is merged as a set of records keyed by each record's own id, in
a stable order, through the file's sanctioned parser. For the pending merges
list that parser is `pending_merges.py:parse_pending_merges`; the file is never
merged as text. A record resolved on one side and still open on the other
takes the resolved state. A record resolved differently on the two sides is
held for a person.

The record merge is one function. It runs wherever the record files are kept:
on the bench for the decision queue (§8.2), and in the pages repository for
the ledgers kept there.

### 5.4 What is not a git conflict

Two writers can each create a page for the same real thing, with two uids and
two paths. Git sees no conflict and neither does this merge. That is a
duplicate, and the existing merge-proposal machinery finds it on the next run
after integrate. Likewise, two clean edits that contradict each other are the
contradiction detector's job. This note does not change either.

---

## 6. Librarian adjudication

A contested unit is one that §5 could not settle. Everything in this section
applies only to contested units.

### 6.1 What the librarian is shown

For each page with contested units:

- the page's name and type;
- for each contested unit, three texts: the **common ancestor**, the **local**
  version and the **remote** version (an empty ancestor when both sides
  created the unit);
- the date of the commit that last changed each side.

It is shown only the contested units and their headings, not the whole page.
Uncontested units are already merged and are not open to change. The three
texts are read through the store seam at the three revisions. Page text goes
inside a fence the prompt marks as data, as every other librarian prompt does.

Which writer made each change, and how far to believe them, is not shown.
Weighting by writer is athenaeum#2628.

### 6.2 What it may decide

For each contested unit, exactly one of:

- `local`: keep the local text;
- `remote`: keep the remote text;
- `both_local_first` or `both_remote_first`: keep both, in that order (lines
  and sections only, never a scalar key);
- `cannot_decide`.

Each answer carries a **reason code** from a fixed list (`newer`,
`more_complete`, `correction`, `duplicate`, `compatible`, `unclear`). There is
no free-text reason, because a sentence written by the model can quote the
page. **The librarian writes no page text** and no prose at all. It selects
between texts that already exist in history. A conflict that can only be
settled by rewriting is `cannot_decide`.

Code then checks the answer before using it: every unit in the result is
byte-identical to one side's text, the page still parses, and the identity
keys are unchanged. An answer that fails the check is treated as
`cannot_decide`.

### 6.3 How the decision is recorded

The merge commit is the record. Its message carries one machine-readable line
per page changed on both sides, with ids only and no page text:

```
Athenaeum-Sync: 1
Athenaeum-Page: <uid> resolver=rule base=<blob> local=<blob> remote=<blob> result=<blob>
Athenaeum-Page: <uid> resolver=model prompt=sha256:<hash> model=<model id> units=<n> reasons=<code,code> result=<blob>
Athenaeum-Page: <uid> resolver=held reason=cannot-decide decision=<decision id>
Athenaeum-Uid-Reminted: <old uid> <new uid>
```

The blob ids name the exact ancestor, local, remote and merged versions, all
of which stay in git history because the local commit is a parent of the
merge. Anyone with read access can diff any two of them, and an adjudication
can be undone by an ordinary later commit. The run log gets the same ids and
reason codes and nothing more. No page text and no model-written text leaves
the store, in the log or anywhere else (§10).

### 6.4 When it cannot decide

A page is **held** when a unit is `cannot_decide`, when the answer fails its
check, or when §5 says a person must decide. For a held page:

1. The **remote** version of each contested unit stays on the page. That is
   the version other installs already have. (This default is question 1 in
   §15.)
2. The local version is not lost. It is in history, named by blob id.
3. A decision is raised in the store's decision queue, naming the uid, the
   contested units and the three blob ids. The queue is on the bench, or in
   the single compiling install's own state (athenaeum#2623 §7). It is never
   written into the pages repository, where every reader would fetch it.
4. The push goes ahead. One undecided page does not block the store.
5. When a person decides, the choice is applied as an ordinary new commit.

**Out of budget is not "cannot decide".** If the run's API budget is spent, or
no model is configured, the remaining contested pages are held with
`reason=deferred`. The next run tries them again before any person is asked,
as long as the remote side has not changed since.

### 6.5 Where the prompt lives and how it is versioned

The adjudication prompt is content, not code:

- **Location:** `src/athenaeum/prompts/sync_conflict_adjudicate.md`, a
  packaged prose file, loaded the way the page-recompile prompts are
  (`src/athenaeum/page_recompile_plan.py:_load_prompt`). No prompt text
  appears as a string literal.
- **Registry:** it is listed in `src/athenaeum/prompt_registry.py`, which
  pins its exact bytes in a golden file under `tests/data/prompts/` and
  renders it, with its sha256, into the generated
  [prompt inventory](prompts.md). An edit to the prompt therefore shows up in
  review as a prose diff and fails CI until the golden is regenerated.
- **Version:** the file's git history is its version history, and its sha256
  is its version id. Every model-adjudicated page records that hash in the
  merge commit (§6.3), so a decision can always be tied to the exact
  instructions that produced it.
- **Model:** its own routing knob, `sync_adjudicate`, so a deployment can
  send adjudication to a different model from compilation.

The deterministic rules in §5 are policy encoded as code, which is the right
home for them. Each rule's reason is stated at its site, as in the table
above.

Whether a store may carry its own additional instructions for the adjudicator
is question 3 in §15. It waits on athenaeum#2628.

---

## 7. Wider uids

### 7.1 Width

New pages get a uid of **16 lowercase hex characters: 64 random bits**, minted
with `secrets.token_hex(8)`.

It is not minted as `uuid4().hex[:16]`. The thirteenth hex character of a
version 4 uuid is always `4`, so that slice has only 60 random bits.

### 7.2 Collision probability

For `n` pages and `b` random bits, the chance that any two share a uid is
about `n² / 2^(b+1)`. The stated store size is **one million pages**, counted
across every store one install can see, since a uid must be unique across all
of them (`models.py:AmbiguousPageUidError`).

| Pages | 8 hex (32 bits, today) | 16 hex (64 bits, new) |
|---|---|---|
| 10,000 | 1.2% | 2.7 × 10⁻¹² |
| 100,000 | 69% | 2.7 × 10⁻¹⁰ |
| **1,000,000** | certain (about 116 colliding pairs expected) | **2.7 × 10⁻⁸** (about 1 in 37 million) |
| 100,000,000 | certain | 2.7 × 10⁻⁴ |

Sixty-four bits is enough because a collision is also detected and repaired
(§7.4, §7.5); the width only has to make that path rare. 32 hex characters (128
bits) was rejected (§12).

### 7.3 Existing uids stay valid

- A uid is an opaque string. No existing page is renamed, re-minted or
  migrated. Stores will hold both widths indefinitely.
- Code that assumes eight characters must accept both. Two sites do today:
  `src/athenaeum/erasure.py:_UID_RE` (258) and
  `src/athenaeum/push_metrics.py:_UID_PREFIX_RE` (623). Two others already
  accept a range: `src/athenaeum/viewer_corpus.py:UID_RE` (35) and
  `src/athenaeum/decisions.py:_UID_PREFIX_RE` (151). The slice greps for the
  rest and adds a test that fails on a new fixed-width uid pattern.
- Uids supplied from outside (a pre-structured raw may carry its own `uid`)
  are unaffected. Only `models.py:generate_uid` changes.

### 7.4 When two installs minted the same uid

This subsection covers one store. Two stores are §7.5.

Detected at integrate time, over the merged tree, by reading each page's uid
through the store seam. No index is consulted (§10).

| Case | Meaning | Action |
|---|---|---|
| The uid exists in the common ancestor | One page, edited by both | Page merge (§5) |
| The uid was added on both sides, with the same `type` and the same normalised `name` | The same thing, added twice with a uid supplied from outside | Page merge with an empty ancestor |
| The uid was added on both sides, and `type` or `name` differ | **Collision** | Below |

On a collision the **local, unpublished page is re-minted** with a new 16-hex
uid, and the remote page keeps the uid. Nobody else has ever seen the local
page, so nothing outside this install can refer to it. The orchestrator
renames the file, rewrites the references to the old uid that were made in the
same unpublished commits, and records `Athenaeum-Uid-Reminted` in the merge
commit (§6.3). No model call and no decision is needed.

Because every writer integrates before it pushes, two conforming writers of
**one store** cannot publish a collision. A collision already sitting in a
store (minted before this design, or kept by "last write wins" inside one
surface) is found by a lint (§14, S2) and raised as a decision. It is never
repaired silently, because existing links to that uid are ambiguous.

### 7.5 The same uid in two stores

Integrate compares pages inside one store. Under athenaeum#2623 different
groups write different stores, so two stores can each publish a page with the
same uid and neither writer can see it. The first install to notice is one
that mounts both. Today that install's lookups raise
`models.py:AmbiguousPageUidError`.

| Who notices | What happens |
|---|---|
| **A writer that mounts both stores**, before it publishes | The check in §7.4 runs across every store the install holds, not only the one being integrated. Its own unpublished page is re-minted. Nothing is published. |
| **Any install that mounts both stores**, after both pages are published | Below. |

For two published pages the install **keeps working and keeps both pages**:

1. It does not refuse to mount either store, and it does not raise. One
   shared uid must not take two stores away from a reader.
2. Each page stays reachable by a store-qualified reference, the pair of the
   store's `store_id` (athenaeum#2623 §5) and the uid.
3. A lookup by bare uid returns an explicit *ambiguous* result that names
   both stores. It never picks one.
4. A link from a page resolves inside that page's own store first. Only a
   link that crosses stores to an ambiguous uid is reported as unresolved.
5. Lookup by name and recall are unaffected.
6. The install reports the collision in its run summary and in the lint
   (§14, S15). If it is in the compile role of either store, it also raises a
   decision in that store's queue to re-mint that store's page. Which page
   changes is a person's call, because published links point at both. An
   install that only reads both stores can do no more than report.

At 64 bits this is as rare as §7.2 says. The realistic source is existing
8-hex pages, which is why the case must be survivable and not only unlikely.

---

## 8. The bench and the inbox

A store's bench and inbox are repositories named under `locations:` in its
declaration (athenaeum#2623 §4). Both have more than one install pushing them
by construction: every proposer pushes the inbox, and every steward and
compiling install pushes the bench. They are synced by the sequence in §4,
with no compile step: lock, snapshot, fetch, integrate, push, retry. The same
rejected-push rule applies (§4.2), and so does the rule for a rewritten
remote (§4.4).

Neither location is indexed (athenaeum#2623 §9), and neither merge calls a
model.

### 8.1 The inbox

The inbox is **append-only by construction**. This note places one
requirement on the proposal format (athenaeum#2625, athenaeum#2626): each
proposal is its own file, its name contains a random id, and it is never
edited after it is written.

| Case | Result |
|---|---|
| Two proposers each add a proposal | Different paths. Both are kept. This is the "changed on one side only" case of §4.1, for every file. |
| A compiling install removes a proposal it has acted on, while a proposer adds another | Both changes are kept. |
| Two compiling installs remove the same proposal | Removed. Each has compiled it into pages; the page merge (§5) settles whatever they both wrote. |
| The same proposal path is changed on both sides | Should not happen. Held by the default rule (§5). |

A proposer's install runs this sequence on the inbox and on nothing else. It
is not a writer and never integrates the pages.

### 8.2 The bench

The bench holds the decision queue, the answers to it and the review cursors.

| File | Rule |
|---|---|
| The decision queue (the pending questions and merges lists, their archives) | Record merge by decision id through the sanctioned parser (§5.3). |
| Answer files | Paired by the `decision_id` inside the file, never by path (below). |
| A review cursor (the last source revision reviewed) | If both sides moved it, take the **earlier** revision. Reviewing a change twice is harmless; skipping one is not. |

**Two stewards answer the same item.** An answer file is named from a
timestamp, the decision type and the decision id
(`src/athenaeum/decision_answers.py`, the `stem` built before
`atomic_write_text`), so two answers usually have different paths and git sees
no conflict. If the paths are equal, both files are kept and one is renamed.
Either way the answers are then compared by decision id:

| Case | Result |
|---|---|
| The answers agree | The decision is applied once. Both answer files are kept as the record. |
| The answers differ, and neither has been applied | Neither is applied. The item stays open and is shown to the compile role with both answers and who gave them. It closes when one more answer says which stands. |
| The answers differ, and the first has already been applied to the pages | The applied answer stands. The second is raised as a new decision, to reopen or not. Nothing is undone by rule. |

A rule never chooses between two people. "First" means first to reach the
bench's remote branch, which is the one order every install agrees on.

Only a compiling install applies answers, inside its run and under its lock
(athenaeum#2623 S9). With several compiling installs, two may apply the same
answer. They then make the same change to the same page, which the page merge
treats as identical.

### 8.3 Order within a run

A compiling install syncs the inbox and the bench before it compiles, so it
sees every proposal and every answer, then the pages, then the bench again
after compiling, to publish the decisions it raised. A failure to sync one
location is reported for that location and does not stop the others.

---

## 9. Two installs compiling at the same time

**Across machines there is no lease, by design.** Two installs may compile at
once. Both succeed locally. The remote accepts one push and rejects the other,
and the rejected install integrates and retries (§4.2). The remote branch
update is the single point where the two histories are put in order.

Losing the race costs a fetch and a merge. It never costs a recompile, because
integrate merges finished pages. Model calls are spent again only on units
both installs changed.

**On one machine the existing lock stays and one lease is added.**

- The run lock (`runlock.py:RunLock`, backed by
  `store.py:FilesystemStore.lease`) keeps serialising processes that share a
  knowledge root. The whole sequence in §4 runs inside it.
- That lock is at the knowledge root. A store's pages, bench and inbox are
  different directories, and two installs on one machine could mount the same
  clone. So each repository in `integrate` mode also takes a lease on a lock file
  **inside that repository's git directory** for steps 2 to 7. A file there
  is never committed and never reaches another writer. Each install should
  still have its own clone; the lease makes sharing one safe, not advisable.

The lease primitive is unchanged. It remains a single-machine `flock`, and
this design never asks it to be more.

---

## 10. The local index

**This design does not assume a plaintext local index, and does not read the
index at all.**

- Everything sync needs comes from git and from the store seam: the common
  ancestor, the three versions of a page, the list of changed paths, and the
  uid of every page.
- Uid collisions are found by reading pages through the store seam, not by a
  lookup in any index or cache.
- Sync writes no page text and no model-written text outside the store.
  Conflict records are ids and reason codes in a commit message (§6.3), the
  run log carries the same and nothing more, and the contested text stays in
  the store's history. Held decisions go to the bench, which only the compile
  role can fetch.
- After integrate, the index is brought up to date by the normal index build,
  which is each install's own business. Sync tells it only that the tree
  changed.
- Athenaeum merges page *content* that the store seam hands it. It never asks
  git to merge page text. So if a store is later encrypted at rest and git
  holds only ciphertext, the merge still works on what the seam decrypts.
  That is a property kept open for athenaeum#2627, not something built here.

The commit-message record holds uids and blob ids. Whether those are
acceptable in clear on an encrypted store is for athenaeum#2627 to rule on;
the record format has a version line (§6.3) so it can change.

---

## 11. Failure classes, migration and rollback

### 11.1 Failure classes the design must survive

| Failure | What happens |
|---|---|
| A crash during integrate | The local branch has not moved (§4.1). The next run starts again. |
| A crash between merge and push | The merge commit is local. The next run fetches, integrates anything newer and pushes. |
| A writer loses every push race in a run | Commits stay local, the run reports `contended`, and the streak alert fires (§4.2). |
| The model is unavailable or the budget is spent | Contested pages are held as `deferred`; rule-based merges and the push still happen (§6.4). |
| The model returns text that is on neither side | The check fails and the page is held (§6.2). |
| Two installs minted one uid | The local page is re-minted before it is published (§7.4). |
| Two stores each published a page with one uid | An install that mounts both keeps both pages, answers a bare-uid lookup as ambiguous, and reports it (§7.5). |
| Two stewards answered one item differently | Neither answer is applied by rule; the item stays open with both (§8.2). |
| A file of a kind nobody registered is changed on both sides | Held; the remote version stays (§5). |
| The remote history was rewritten | Stop; no merge, no push (§4.4). |
| A writer on an older release that only fast-forwards | It never merges. Once diverged it stops pushing and reports the failure it reports today. It cannot damage the store. It needs upgrading to write again. |
| One side's page does not parse | The page is held; the parsable side stays on the page. |
| The network is down | No fetch and no push; the run compiles locally as today. |

### 11.2 Migration

1. Wider uids ship first and alone. They need no configuration and change no
   existing page.
2. `integrate` mode ships behind the declaration's `sync.mode`, default
   `fast_forward`. A store with one writer, or with no declaration, never has
   to change anything.
3. A store moves to several writers in one commit to its declaration: set
   `sync.mode: integrate` and name the additional compilers. The host must
   then let them push. The interim rule from athenaeum#2630 (one compiling
   install per shared store) is enforced by the access check, and
   athenaeum#2623 S13 is the one slice that lifts it, for stores that declare
   `integrate`. This note lifts nothing by local configuration.

### 11.3 Rollback

- Setting `sync.mode` back to `fast_forward` in the declaration restores
  today's behaviour, and the access check again refuses more than one writer. The
  history `integrate` wrote is ordinary git history with merge commits, which
  fast-forward mode reads without trouble.
- A wrong adjudication is undone by a later commit that restores the other
  blob named in the record. Nothing is ever force-pushed.
- Wider uids are not rolled back. Pages minted with them stay valid under the
  widened patterns.

---

## 12. Principles applied and alternatives rejected

**Principles.**

- **Git is the audit trail** (operator ruling, athenaeum#2631). Merge, never
  rebase or force. Every decision is a commit a person can diff and undo.
- **Deterministic first.** A model is asked only what a rule cannot answer,
  and its answer is checked by code.
- **One statement of each rule.** Type merge rules come from the same config
  the structured upsert reads.
- **Never lose a side.** Every version stays reachable by id.
- **Git only inside the store seam.** No new caller shells out to git.
- **Prompt text is content.**

**Rejected.**

1. **A lease that spans machines** (a lock branch or lock file on the remote
   with a time limit). It needs clocks to agree, needs a rule for breaking a
   dead holder's lock, and still needs the rejected-push path for the moment
   it fails. The remote's own compare-and-swap already gives the ordering.
2. **Git's own text merge, or a custom git merge driver.** A text merge can
   produce a page that is clean to git and wrong to athenaeum, and it cannot
   work on an encrypted store. A merge driver must be installed in every
   clone's git config, runs outside the run's lock and budget, and fails
   silently when it is missing.
3. **Letting the model write the merged page.** It puts model-authored text
   on a shared page with no source on either side, which cannot be checked by
   code and widens the poisoning surface athenaeum#2628 is reviewing.
4. **Rebasing local commits onto the remote.** It rewrites commits and so
   discards the record of what each install compiled, against the audit-trail
   ruling.
5. **32-hex (128-bit) uids, or uids prefixed with an install id.** The first
   doubles every filename and citation to buy safety §7.4 already provides.
   The second is not random, as the ruling requires. Provenance also belongs
   in frontmatter, where a raised copy records its origin store, its proposer
   and a content hash (athenaeum#2631), and not in an identifier that every
   link repeats.
6. **Rewriting existing 8-hex uids to the new width.** It breaks every link
   and every external reference, against the ruling that existing uids are
   kept.
7. **Each install choosing integrate mode in its own config.** Installs could
   disagree, and the access check cannot read another install's local file.
   athenaeum#2623 rejects per-install policy for the same reason.
8. **Keeping the decision queue in the pages repository.** Every reader would
   fetch it, and a decision is for the compile role only (athenaeum#2623 §7).
9. **Refusing to mount when two stores share a uid.** A reader would lose two
   stores over one page it cannot fix.

---

## 13. Out of scope

- **Trust and poisoning across writers** (athenaeum#2628, held for external
  review): whether one writer's change should be believed over another's,
  what a hostile writer can do, and any control on store-carried
  instructions (§15, question 3). Nothing here weighs writers.
- **Access**: who is in the compile role, who the stewards are and where the
  bench is (athenaeum#2623). Page audience is a separate axis that sync never
  changes (§5.1).
- **The proposal format** in the inbox (athenaeum#2625, athenaeum#2626),
  beyond the one requirement in §8.1.
- **Routing a fact to a store and raising between stores** (athenaeum#2625,
  athenaeum#2626).
- **Encryption** (athenaeum#2627), beyond not blocking it (§10).
- **Erasure that rewrites a shared store's history.** §4.4 only makes sync
  stop safely when it sees one.
- **Non-git backing stores**, and building any of this.

---

## 14. Build slices

Each is one pull request. S1, S2, S3, S4, S5a and S6b depend on nothing and
can start now. Slice numbers are stable so that athenaeum#2623 can cite them.
A dependency written `athenaeum#2623 S7` is a slice of that note.

| Slice | What it delivers | Needs |
|---|---|---|
| **S1** | `generate_uid` mints 16 hex characters with `secrets.token_hex(8)`. The fixed-width uid patterns accept both widths, with a test that fails on a new fixed-width one. | none |
| **S2** | A read-only lint that reports one uid on two pages inside a single surface. | none |
| **S3** | The pure three-way page merge: units, the rules in §5.1 and §5.2, and a list of contested units. No git and no model. Shares helpers with the structured upsert. | none |
| **S4** | The record merge for record files through their parsers, and "take one side and regenerate" for derived files (§5.3). | none |
| **S5a** | Store seam, read side: fetch, common ancestor, read a file at a revision, changed paths between two revisions, and rewritten-history detection. | none |
| **S5b** | Store seam, write side: the all-or-nothing merge commit, a push that reports why it failed, and the `multi_writer` capability. | S5a |
| **S6a** | A two-clone test harness: one bare remote and two installs. | S5b |
| **S6b** | The check that no machine-local file is tracked in a shared repository. | none |
| **S7** | Integrate mode for the pages, rule-based merging only: read `sync.mode` from the declaration with local narrowing (§3.3), the sequence in §4, retries, validation, the default hold for unregistered and `_store/` files, the merge-commit record, run-summary rows. Every contested unit is held. Default stays `fast_forward`. | S3, S4, S5b, S6a; athenaeum#2623 S1 |
| **S8** | The held-page decision: a new kind in the store's decision queue, with list and apply. | S7; athenaeum#2623 S7 (the per-store queue) and, with several compiling installs, athenaeum#2623 S8 (the bench mount) |
| **S9** | Uid collision handling at integrate: detect, re-mint the local page, rewrite local references, record it (§7.4). | S1, S7 |
| **S10** | The adjudicator: the prompt file, its registry entry and golden, the `sync_adjudicate` knob, reason codes, the answer check, budget deferral and the `sync.always_ask` list (§6). | S7, S8 |
| **S11** | The per-repository lease inside the git directory, for pages, bench and inbox (§9). | S7 |
| **S12** | A guide for several installs writing one store. Docs only. The interim one-writer rule is lifted by athenaeum#2623 S13, not here. | S7 to S11, S13, S14 |
| **S13** | Sync for the bench and the inbox: the sequence without a compile step, the append-only inbox rule, and the record merge of the decision queue on the bench (§8.1, §8.2). | S4, S5b, S6a; athenaeum#2623 S8 |
| **S14** | Concurrent answers and review cursors on the bench: pair answers by decision id, the three cases for two answers to one item, and the earlier-cursor rule (§8.2). | S13; athenaeum#2623 S7 |
| **S15** | The same uid in two stores: the cross-store check before publishing, store-qualified lookup, and an ambiguous result in place of `AmbiguousPageUidError` (§7.5). | S2; athenaeum#2623 S1 (for `store_id`) |
| **S16** | The erasure mark: the erasing path writes `Athenaeum-Erasure` on its deletion commit and integrate honours it (§5.1). | S7 |

S7 is the first slice that makes a two-writer store converge. A deployment
could run on S1 to S9 with no model adjudication at all, with every contested
page going to a person.

**What athenaeum#2623 needs from here.** Its S9 (stewards answering on the
bench) needs S13 and S14. Its S13 (lifting the one-writer rule) needs S7 for
the pages and S13 for the bench.

---

## 15. Open questions for the operator

Only calls that depend on what the operator values. Each has a recommended
answer, and the design above is written to that answer.

### 1. While a conflict waits for a person, which version do readers see?

Two installs changed the same sentence in different ways and the librarian
could not choose. A person will decide later. Until then the page has to say
something.

- **Show the version that was already published (recommended).** Other
  installs already have it, so nobody's page changes under them. The other
  version is kept in history and named in the decision. Cost: the newer local
  edit is invisible until someone decides.
- **Show both versions on the page, marked as disputed.** Nothing is hidden.
  Cost: assistants will recall two contradictory statements as if both were
  knowledge, and the page is no longer clean text.
- **Hold back the whole push until a person decides.** No reader ever sees an
  unresolved page. Cost: one disputed sentence stops everything else that
  install compiled from reaching anyone, possibly for days.

### 2. May the librarian settle a conflict on its own, or only propose?

The ruling says a librarian adjudicates. The question is how far that goes
without a person looking.

- **It settles on its own, except for page types the store lists as "always
  ask" (recommended).** Most conflicts clear with no queue. Every decision is
  one recorded, undoable commit, and it can only choose between existing
  texts. A store can name sensitive types (say, HR records) that always go to
  a person, in `sync.always_ask` in its declaration (§3.3). Cost: a wrong
  choice is live until someone notices.
- **It settles everything on its own.** Simplest and least queue. Cost: no
  way to protect a sensitive type.
- **It only proposes; a person approves each one.** Safest. Cost: every
  conflict becomes a queue item, which is the load the ruling was meant to
  remove.

### 3. May a shared store carry its own instructions for settling conflicts?

The adjudication prompt ships with athenaeum. A team might want its own rules
on top, for example "the HR manager's wording wins on policy pages".

**This question is deferred to the external review in athenaeum#2628.** A
file that steers every later decision is a way to poison a store, and what
would make it safe is that review's subject. This note designs no control for
it and builds nothing for it. The options are recorded so the review has
them.

- **Not until the review reports: only the built-in prompt (recommended for
  now).** Nothing in a store can steer the librarian. Cost: no team-specific
  rules; every such case goes to a person.
- **Yes, as a file under `_store/` that every writer's librarian reads.** All
  writers settle conflicts by the same rules, versioned with the store. Cost:
  anyone who can change that file can steer every later decision.
- **Yes, but per install, in each install's own config.** No shared file to
  attack. Cost: two installs can settle the same conflict in opposite ways.

### 4. One install removed a page and another added to it. Which wins?

One librarian retired a page, or merged it into another, while a second
librarian was adding a new fact to it. (Erasure for privacy is separate and
always wins.)

- **Keep the page with the new fact, and raise the removal again as a
  decision (recommended).** Knowledge is never dropped silently. Cost: a page
  someone meant to remove comes back until a person confirms.
- **Honour the removal and attach the new fact to wherever the page was
  merged, or drop it if it was retired.** The store stays tidy. Cost: a fact
  can vanish with nobody told.
- **Let the librarian choose case by case.** Fewer decisions for people.
  Cost: the outcome is less predictable, and a wrong removal is the harder
  mistake to notice.
