<!-- SPDX-License-Identifier: Apache-2.0 -->

# Federation: the store access model (read, propose and compile)

**Status:** DESIGN, not built (Vitruvius Specify, 2026-11-18). Design note for
athenaeum#2623, a child of the federation design epic athenaeum#2631. Nothing
here is build-ready until the operator has answered §13; the build slices are
in §12.

This note decides who may read a store, who may propose to it, who compiles it,
who owns its decision queue, and how one librarian reviews another store on a
schedule. It does **not** decide:

- how several installs write one store (athenaeum#2624, written in parallel);
- how a fact is routed to a store at intake, or what a proposal looks like
  (athenaeum#2625, athenaeum#2626);
- encryption (athenaeum#2627);
- trust between writers and poisoning of a shared store (athenaeum#2628) and
  routing a decision to the member most likely to know (athenaeum#2629). Both
  are held for an external review. They are named here only where this note
  hands a question to them; nothing about them is designed here;
- what a remote caller of one install may see (athenaeum#2607).

---

## 1. Summary

A **store** is a set of pages with one access boundary. The model has three
parts.

1. **Access is enforced by the place the store is kept, not by athenaeum.** A
   store is made of up to three separately permissioned locations: its *pages*,
   its *inbox* and its *bench*. Each permission is a right on one of them. An
   install that lacks a right does not have the bytes, so there is nothing for
   athenaeum to filter.
2. **The store carries a declaration of who holds which permission.** It names
   principals and groups, never credentials. Installs read it to decide their
   own behaviour (compile or not, show the queue or not, mount or refuse), and a
   check compares it with what the host actually allows. The declaration grants
   nothing by being written.
3. **Every athenaeum-side rule only narrows.** An install writes a store only
   when its local config allows it, the declaration grants it and the host
   accepts the push. Any one of the three can say no; none can say yes alone.

## 2. What exists today

- **A per-page audience check, per process.** `src/athenaeum/models.py:is_page_authorized`
  returns true for the owner (`caller_audience=None`) and otherwise requires
  `access: open` or a shared role between the caller's pinned audience and the
  page's `audience:` list (`src/athenaeum/models.py:effective_audience`). The
  pin is set once per process (`src/athenaeum/config.py:resolve_audience`). It
  is a filter over pages the process already holds.
- **Surfaces in their own git repositories** (athenaeum#2630). A surface is a
  named entry under `storage.adapters` (`src/athenaeum/storage.py:StorageAdapter`).
  One declared `read_only` is pulled before each index build
  (`src/athenaeum/librarian.py:pull_read_only_surfaces`) and refused by every
  write path (`src/athenaeum/storage.py:require_writable_path`,
  `src/athenaeum/store.py:ReadOnlySurfaceError`).
- **The store-boundary rule.** Merges never cross a surface
  (`src/athenaeum/store_boundary.py:assert_same_surface`), and a link may not
  point at a surface with a lower `visibility` rank
  (`src/athenaeum/store_boundary.py:link_forbidden`).
- **Two layers of write protection for a second reader.** The shared-store
  guide states that repository permission is the protection and `read_only` is
  defence in depth ([Reading a shared store](../guides/shared-store-reader.md)).
- **One decision queue per install, in its wiki.** For example
  `src/athenaeum/_cmd_decisions.py:_cmd_raise_confirmation` writes to
  `wiki_root / "_pending_questions.md"`. Answers come back as files under
  `raw/answers/` (`src/athenaeum/decision_answers.py`).
- **A durable identity key.** `src/athenaeum/models.py:asserter_identity_key`
  derives `(iss, sub)` (or the tenant-scoped pair for one provider) from an
  `asserter` block, and `src/athenaeum/config.py:resolve_owner_asserter` reads
  the install owner's block from `owner.asserter`.

There is no store-level permission, no propose permission and no notion of who
compiles a store. athenaeum#2630 holds an interim rule in their place: one
compiling install per shared store.

## 3. Terms

| Term | Meaning |
|---|---|
| **Install** | One knowledge directory with its own librarian and its own index. |
| **Store** | A set of pages with one access boundary. Physically, a surface in its own repository plus, when needed, an inbox and a bench. |
| **Principal** | One person, software agent or organisation, named by an `asserter` block. |
| **Group** | A named set of principals, resolved through a directory adapter. |
| **Compile role** | The principals that turn a store's intake into its pages and answer its decisions. |
| **Host** | Whatever keeps a store's locations and decides who may fetch or push them. Today a git host. |

The store, not the page and not the type, is the **smallest unit of physical
access**. A group that wants to expose half its pages splits them into two
stores. Page-level `audience:` remains and is a different axis (§10).

## 4. The three permissions

A store has up to three locations. Each is a repository, or anything else with
the same fetch and push semantics.

| Location | Holds | Who fetches | Who pushes |
|---|---|---|---|
| **Pages** | The compiled pages and the access declaration | read, compile | compile |
| **Inbox** | Proposals waiting for the compile role | compile | propose |
| **Bench** | The decision queue, its answers, review cursors | compile | compile |

| Permission | What it lets a principal's install do |
|---|---|
| **read** | Hold a copy of the pages and build them into its own index. |
| **propose** | Put a candidate into the inbox. It is not a page until the compile role acts on it. |
| **compile** | Read the inbox, write pages, own the decision queue. Implies read. |

The three are independent, apart from compile implying read. Two consequences
are deliberate:

- **Propose does not imply read.** A person may be allowed to send a fact to a
  store they cannot read. The inbox is a separate location for exactly this
  reason.
- **Read does not imply propose.** The announcements store in the epic's worked
  example is readable by everyone and proposable only by leadership and selected
  department heads.

This note fixes what the inbox is *for* and who may reach it. Its contents, the
proposal format and where raw intake for a shared store waits are
athenaeum#2625 and athenaeum#2626. The two constraints this note places on them
are: the inbox must be permissioned separately from the pages, and nothing a
proposer can fetch from the inbox may exceed what §13 question 1 allows.

**When a location can be omitted.**

- The inbox is omitted when nobody holds propose without compile.
- The bench is omitted when exactly one install compiles: the queue stays in
  that install's own state, as it does today.
- The bench may live in the pages repository when every reader is in the compile
  role, because then nothing in it is shown to anyone outside that role.

The recipe store that athenaeum#2630 set up is the smallest case: a pages
repository, one compiling install, no inbox and no bench. It needs no change.

## 5. How a store declares its permissions

The declaration is one file in the pages repository, `_store/access.yaml`. The
`_` prefix follows the convention the index scan already skips for the wiki
(`src/athenaeum/search.py:_wiki_relpath_excluded`); slice S1 pins the same skip
for every surface.

```yaml
version: 1
store_id: st_q7m2xk4vwz9d        # opaque, random, never reused; athenaeum#2626 cites it
directory: static                 # which directory adapter resolves group names
groups:                           # read by the static adapter only
  leadership:
    - {iss: "https://git.example.net", sub: "1042"}
    - {iss: "https://git.example.net", sub: "1077"}
  department-heads:
    - {iss: "https://git.example.net", sub: "1203"}
grants:
  read: ["*"]                                     # anyone the host admits
  propose: ["group:leadership", "group:department-heads"]
compile:
  mode: named                                     # cloud | named | members
  by: ["group:leadership"]
locations:
  inbox: "git@git.example.net:acme/announcements-inbox.git"
  bench: "git@git.example.net:acme/announcements-bench.git"
reviews: []                                       # §8
```

Why it lives in the store and not in each install's `athenaeum.yaml`: every
install then acts on the same statement, and every change to it is a commit
with an author and a diff. A per-install copy could disagree with its
neighbours, and a member could name themselves a compiler in their own file.

**The declaration is a statement, not a grant.** Writing a principal into
`grants.read` does not give them a clone; only the host does that. Removing
them does not take a clone away. The declaration is used for four things:

1. **Role selection.** Each install resolves its owner
   (`src/athenaeum/config.py:resolve_owner_asserter`) against the declaration
   and learns whether it reads, proposes or compiles this store.
2. **Narrowing.** An install that is not in the compile role refuses to write
   the pages even if its local config says `read_only: false` (§9).
3. **Drift detection.** A check compares the declaration with what the host
   allows and reports every difference (§9).
4. **Disclosure.** Anyone who can read the store can see who else can, and who
   reviews it (§8).

Who may change the declaration follows from where it lives: whoever the host
lets push the pages. athenaeum adds no separate administrator permission. The
host's own access settings are the administration plane.

**A store with no declaration behaves exactly as today.** Its local
`read_only` flag decides everything. A declaration that is present but cannot
be parsed is treated as granting nothing beyond read to this install, and is
reported.

## 6. Naming principals and groups

athenaeum does not become an identity store. It keeps no credentials,
authenticates nobody and has no user lifecycle. athenaeum#710 set that rule and
it stands.

**Principals** are written as the existing `asserter` block and keyed by
`src/athenaeum/models.py:asserter_identity_key`. No second key derivation is
introduced. Because the key is `(iss, sub)`, a team with no identity provider
can name people by their account on the host that keeps the store
(`iss` the host's URL, `sub` the stable account id). That is the same identity
the host enforces against, which makes the drift check a direct comparison.

**Groups** are written `group:<name>` and resolved through a **directory
adapter**, the seam athenaeum#710 named, shaped like the storage adapter:

```python
class DirectoryAdapter(Protocol):
    def members(self, group: str) -> frozenset[tuple[str, ...]] | None: ...
```

It returns identity keys, or `None` when the directory cannot answer. Two
adapters are in scope:

- `static`, built in: reads the `groups:` block of the declaration. It needs no
  network and no account.
- an external adapter, registered by a deployment: maps a group name to a team
  on the host or a group in an identity provider. The seam ships in this
  repository; adapters for particular hosts do not.

Two reserved words: `"*"` means anyone the host admits, and is valid only under
`grants.read`; `members` as a compile mode means every principal that holds
read.

**Resolution is never on the recall path.** Groups are resolved when a run
starts and when the check runs. An install whose directory cannot answer acts
as a reader only: it does not compile, does not run reviews and does not show
the store's queue. Failure narrows; it never widens.

**The install's own identity is self-declared**, and that is acceptable for
role selection only. A member who puts someone else's identity in
`owner.asserter` changes what their own install *attempts*; the host still
refuses the push. Whether a proposal or a commit really came from the principal
it names is a question of verified writer identity, which
[Writer identity](writer-identity.md) leaves unverified today and
athenaeum#2628 owns for shared stores. This note does not rely on it.

**What athenaeum#710's grant shape contributes, and what is dropped.** Kept:
principals as `{iss, sub}`, groups, the directory adapter, and the rule that
athenaeum is never an identity store. Dropped: the scope prefix (the unit of
access is now a store, by the ruling that access is storage RBAC and not a
strict hierarchy), the optional memory class (placement by type is already
`storage.mapping`), resolution "at decision time" (read is resolved by the host
when the store is fetched), and authorising a pair operation at the common
ancestor of two scopes (merges never cross a store, so no pair spans two).

## 7. The compile role and the decision queue

`compile.mode` has three settings.

| Mode | Who runs the store's librarian | Who else holds the pages |
|---|---|---|
| `cloud` | One headless install, named in `compile.by` as a `software_agent` principal | Every other member mounts the pages read-only |
| `named` | The installs of the principals and groups in `compile.by` | Other readers mount read-only |
| `members` | Every install that holds read | Nobody |

**The compile role owns the store's decision queue.** Concretely:

- **A decision belongs to the store whose page it would change.** Merges never
  cross a store, so a merge proposal always has exactly one owning store.
- **Only compile-role installs raise decisions about a store's pages.** A
  reader's install runs no merge, contradiction or repair pass over a store it
  cannot write, which is what `read_only` already means.
- **The queue is kept where only the compile role can fetch it:** in the single
  compiling install's own state, or on the bench. A declaration whose bench is
  readable more widely than its compile role is refused by the check.
- **A decision that involves pages from several stores goes to the least widely
  shared one**, by the existing `visibility` rank. It may change only that
  store's pages. Changing the other store is a proposal to it, never a direct
  write. This is the epic's "narrowest store" rule applied to decisions, and it
  stops a private page being quoted into a shared queue.
- **Every member of the compile role sees the whole queue.** Sending each item
  to the member most likely to know is athenaeum#2629 and is not designed here.

In `cloud` mode the librarian is software, and a queue item is by definition
something software could not settle. The declaration therefore names the people
who answer it:

```yaml
compile:
  mode: cloud
  by: [{iss: "https://git.example.net", sub: "svc-librarian", type: software_agent}]
  stewards: ["group:leadership"]
```

Stewards are part of the compile role. They fetch and push the bench, where
they read the queue and leave answer files in the existing `raw/answers/`
shape; they do not push the pages. Whether `cloud` mode should have human
stewards at all is §13 question 5.

**Dependency on athenaeum#2624.** Two installs writing the same pages, and two
stewards answering the same item, are multi-writer problems. Until that note is
built, the check refuses any declaration that puts more than one install in a
position to push the pages: `members`, and `named` resolving to more than one
principal. `cloud`, and `named` with one principal, need nothing from it. That
is the athenaeum#2630 interim rule, now stated by the store instead of assumed.

## 8. Scheduled cross-store review

A librarian that compiles one store (the *target*) may review another (the
*source*) on a schedule and raise what it finds. The operator's example: the
leadership librarian looks at a lower group's store each week, and a
configurable prompt decides what to escalate.

**Which grants it needs.** Two, and both are ordinary:

- **read on the source**, held by the principal whose install runs the review.
  The install mounts the source read-only, like any other reader. There is no
  review-specific way to read a store and no grant for part of one.
- **compile or propose on the target**, because the review's output is a
  proposal to the target.

**Both stores must agree.** The target declares the review; the source declares
the reader. A review whose principal is not granted read in the source's
declaration does not run, even if the clone is on disk.

```yaml
# in the TARGET store's _store/access.yaml
reviews:
  - name: weekly-field-notes
    source: st_f2n8rb5hjc3t          # the source's store_id
    every: 7d
    criteria: _store/reviews/weekly-field-notes.md
```

```yaml
# in the SOURCE store's _store/access.yaml
grants:
  read: ["group:field-team", {principal: "group:leadership", purpose: review}]
```

`purpose: review` changes nothing about what the grant allows. It is there so
the members of the source store can see who reads their store and why.

**Where the prompt lives.** In two places, split by who owns the words:

- The **criteria** (what counts as worth escalating) are the deployment's. They
  live in a markdown file in the target store, at the path the review names, so
  they are versioned and editable as prose by the people who own the target.
- The **frame** (the instruction to judge source text against the criteria, and
  the output shape) is the engine's. It is registered in
  `src/athenaeum/prompt_registry.py` like every other engine prompt.

Source pages are passed to the model as untrusted data, in the same way the
classifier treats raw intake (`src/athenaeum/tiers.py:CLASSIFY_SYSTEM`). What a
hostile source page can do to a reviewing librarian beyond that is
athenaeum#2628.

**What it may write.**

| Where | What | Why it is safe |
|---|---|---|
| The target's inbox | Proposals, in the format athenaeum#2626 defines, with provenance and no link back to the source | They pass through the target's normal compile and queue before becoming pages. A review never writes a page directly. |
| The target's bench or the reviewing install's own state | A cursor (the last source revision reviewed) and a run record (time, counts) | Neither contains source text. |
| The source | **Nothing.** No page, no proposal, no marker, no cursor. | The reviewing install holds read only, and the host refuses anything else. |

"Recent changes" means the difference between the cursor and the source's
current revision. A review that is due runs inside the ordinary librarian run;
no second scheduler is added.

## 9. A librarian reads only what its grants allow

The invariant from athenaeum#2631: **a librarian reads only what its grants
allow, and an install does not hold a store it may not read.**

It is enforced in three layers. Only the first is a security boundary.

| Layer | Mechanism | Stops |
|---|---|---|
| **1. The host** | Fetch and push rights on each location | An install obtaining bytes, or changing them, without the right |
| **2. The declaration** | An install mounts a declared store only if its principal is granted read, and writes it only if in the compile role | A copy that outlived its grant being indexed; a misconfigured install compiling a store it should only read |
| **3. Local config** | `read_only: true` on the adapter | An install writing a store its owner chose to treat as a mount |

Effective write is the conjunction of all three. Each can only narrow.

**What "does not hold" covers.** The pages, and everything derived from them
on that install: index rows, embeddings, snapshots, cached copies. When an
install stops mounting a store (the declaration no longer grants it read, the
operator removed the adapter, or the offline limit below passed) it removes the
derived data for that store and reports the store as unmounted.

**No plaintext index is assumed.** The model never asks the index who may see
what. An install's index is built only from stores that install holds, so
store-level access needs no filter at query time, encrypted or not. Two
requirements follow for whoever builds the index and athenaeum#2627:

- **The index must be separable by store.** Removing one store's derived data
  must not require reading another store's. A segment per store satisfies it.
- **A store's derived data must be protectable with that store's key alone**,
  so that losing the key to one store does not expose the rest.

The inbox and the bench are never indexed. The config-time root checks that
already keep non-indexed surfaces out of the index
(`src/athenaeum/storage.py:validate_indexed_surfaces`) are extended to refuse a
configuration that would index either.

**The check.** `athenaeum store access check` reports, per mounted store:

- this install's role according to the declaration;
- what the host actually allows this install (can it fetch, would a push be
  accepted), and any difference from the declared role;
- declarations the interim rule refuses (§7), a bench readable beyond the
  compile role, and reviews with no matching read grant in the source;
- with an external directory adapter that can list the host's access, every
  principal the host admits that the declaration does not name, and the reverse.

It is read-only and changes nothing on the host. athenaeum never provisions
access.

**The limit that must be stated plainly.** Revoking read stops future fetches.
It does not remove the copy a member already has, and today a failed pull is
not fatal: the install keeps serving its last good copy
(`src/athenaeum/librarian.py:pull_read_only_surfaces`). After revocation the
host refuses the pull, so the revoked install never sees the new declaration
either. Two things narrow this, and neither closes it:

- a store may declare `max_offline`, after which an install that has not
  completed a pull unmounts the store and removes its derived data (§13
  question 2);
- encryption with key rotation (athenaeum#2627) stops a retained copy being
  read once its key is gone.

Neither recovers what a person already read.

## 10. Boundaries with neighbouring work

- **Page audience** (`src/athenaeum/models.py:is_page_authorized`) is
  unchanged. Store access decides which stores an install holds; audience
  decides what a restricted process sees among the pages it holds. Neither
  widens the other. Group names and `audience:` role ids are both opaque names
  the operator aligns with one external directory, so the same name may be used
  for both, but neither check consults the other. No default audience per
  surface is introduced: athenaeum#2630 deferred it to "store-level access",
  and this model supplies that by keeping the store off installs that may not
  read it.
- **Remote-caller ceilings** (athenaeum#2607) decide what one install shows a
  remote caller. This note asks only that its predicate be able to key on the
  store a page lives on, so an install can decline to re-serve a shared store.
- **Multi-writer sync** (athenaeum#2624) owns the sync sequence, conflicts and
  concurrent answers. This note says who is allowed to be a writer.
- **Intake routing** (athenaeum#2625) owns which store a fact goes to and where
  raw intake waits. **Raise and retract** (athenaeum#2626) owns the proposal
  format, provenance and retraction. This note supplies the `store_id` they
  cite and the inbox they fill.

**Repository split.** The declaration parser, role resolution, the directory
adapter seam with its static adapter, the guards and the check belong in this
repository. Adapters for a particular host or identity provider are registered
by a deployment and do not. A deployment's declarations live in its own stores.

## 11. Failure classes, migration and rejected alternatives

**Failure classes the design must survive.**

| Failure | Outcome |
|---|---|
| The declaration and the host disagree | The host wins for what is possible; the declaration narrows what the install attempts; the check reports the difference. |
| A reader's install is configured writable | Refused by the declaration layer before any write, and by the host on push. |
| A member claims another identity locally | Their install attempts a role the host refuses. No other install is affected. |
| The directory cannot answer | The install acts as a reader only. |
| The declaration is malformed | The install acts as a reader only, and reports it. |
| A compiler widens the declaration | Nothing changes until the host changes. The commit is in history and the check reports the drift. |
| A revoked member keeps a clone | Not prevented. Bounded by `max_offline` and, later, by key rotation (§9). |
| A private page is drawn into a shared queue | Prevented by filing cross-store decisions in the least widely shared store (§7). |
| A review reads a store it was not granted | Impossible by construction: the install does not hold it. A clone held without a declared grant is not mounted. |
| A hostile source page steers a reviewing librarian | Contained to a proposal in the target's inbox. Anything further is athenaeum#2628. |

**Migration.** Each step is additive. A store gains a declaration when someone
commits the file; until then it behaves as today. Adding an inbox or a bench
means creating the repository and naming it under `locations`.

**Rollback.** Delete the declaration and the store returns to local-config
behaviour. No page format changes, no uid changes and no index format changes
are introduced by this note, so there is nothing to convert back.

**Principles applied.** The engine ships mechanism and the deployment supplies
policy. Enforcement is physical first and declared second. Every
athenaeum-side rule narrows. Existing seams are reused, not paralleled: the
identity key, the `visibility` rank, the read-only guard, the answer-file
shape. Storage stays logically fixed and physically pluggable
([North star](../north-star.md) §2.11): nothing above depends on the host being
git, only on separately permissioned locations with fetch and push.

**Rejected alternatives.**

1. **Check store access inside athenaeum at query time**, extending the page
   audience check to stores over one combined index. Rejected: the install
   would hold stores it may not read, the check would have to read the index to
   filter it, and one process would see everything. athenaeum#710 already ruled
   out a consolidated store.
2. **athenaeum#710's scope-prefix grants over a tree.** Rejected by the ruling
   that access is storage RBAC, not a strict hierarchy. A scope prefix is also
   not something a host can enforce, so it could only be a filter.
3. **Keep the declaration in each install's local config.** Rejected: installs
   could disagree, there would be no shared history of changes, and membership
   of the compile role would be whatever each member typed.
4. **One repository per store, with protected branches for pages and open
   branches for proposals.** Rejected: a clone holds every branch, so propose
   would imply read and every reader would hold the inbox. It also ties the
   model to one host's branch-protection feature.
5. **Have athenaeum provision the host's permissions from the declaration.**
   Rejected: athenaeum would hold administrator credentials for every store and
   become the permission system it is meant to stay out of.
6. **A review-only grant covering part of a store.** Rejected: there is no way
   to hand over part of a repository. A group that wants to expose part of what
   it keeps splits the store.

## 12. Build slices

Each is one pull request. Slices S1 to S5 depend on nothing outside this note.

| Slice | What it adds | Depends on |
|---|---|---|
| **S1** | Parse and validate `_store/access.yaml` (`store_id`, `grants`, `compile`, `locations`, `groups`), failing closed. Pin that `_store/` is skipped by the index scan on every surface. Document the format. No behaviour change. | none |
| **S2** | The directory adapter protocol, the built-in `static` adapter, and role resolution: given a surface and `owner.asserter`, return reader, proposer, compiler or none. | S1 |
| **S3** | Fold the declared role into the write guard: an install outside the compile role is refused on that store's pages, whatever its local `read_only` says. | S2 |
| **S4** | Refuse to mount a declared store whose declaration does not grant this install read, and remove a store's derived data when it is unmounted. | S2 |
| **S5** | `athenaeum store access check`: declared role against what the host allows this install; the interim one-writer rule; a non-zero exit on any difference. | S2 |
| **S6** | `max_offline`: unmount a store whose last completed pull is older than its declared limit. | S4, §13 question 2 |
| **S7** | A decision queue per store on the decision seam: an item is filed with the store whose page it would change, and a cross-store item with the least widely shared one. Single compiling install; the queue stays in local state. | S2 |
| **S8** | The `locations` entries: mount the bench and the inbox, refuse to index either, and refuse a bench readable beyond the compile role. | S5 |
| **S9** | `cloud` mode stewards: a steward's install shows the store's queue from the bench and writes answer files to it; the compiling install applies them. | S7, S8, athenaeum#2624 |
| **S10** | The `reviews` block: parse it, check the matching read grant in the source's declaration, compute "due", keep the cursor. No model call. | S2 |
| **S11** | The review run: the frame prompt in the prompt registry, the criteria file, proposals written to the target's inbox. | S10, S8, athenaeum#2626 |
| **S12** | The external directory adapter registration point, and the check's host-side comparison when an adapter can list access. | S5 |
| **S13** | Lift the interim one-writer rule for `named` (several) and `members`. | athenaeum#2624 |
| **S14** | A guide: setting up a store with readers, proposers and a compile role. | S5, S8 |

## 13. Open questions for the operator

Each is a values call the design cannot settle. The recommended answer is
listed first.

**1. May people who propose to the same store see each other's pending
proposals?** On most hosts, the right to push to a repository includes the
right to fetch it, so a shared inbox shows every proposer what the others sent,
including proposals later rejected.

- **Recommended: decide by rule.** A shared inbox when every proposer can also
  read the store; one inbox per proposer as soon as any proposer cannot. Cheap
  where little is at stake, private where the store is.
- Always one inbox per proposer. Nothing leaks between proposers, at the cost of
  a repository per person per store.
- Always a shared inbox. Simplest to run; a person reporting something to a
  store they cannot read is visible to every other proposer.

**2. How long may an install keep answering from a shared store it can no
longer reach?** Today the answer is forever, which keeps an offline laptop
useful and lets a removed member keep recalling the store.

- **Recommended: each store may declare a limit; the engine sets none.** A
  sensitive store declares, say, 14 days; a recipe store declares nothing.
- An engine default (for example 30 days) for every store with a declaration.
  Safer when nobody thought about it, and it breaks long offline use by
  default.
- No limit mechanism. Wait for encryption with key rotation. Least to build,
  and removal has no effect on a retained copy until then.

**3. What do the members of a reviewed store get to see about the review?**
The leadership librarian reading a team's store is a form of oversight.

- **Recommended: who reads and why, not the criteria.** The team's own
  declaration shows the reviewing group and `purpose: review`; the criteria
  stay in the reviewing store.
- Who, why and the criteria. The most transparent; it also tells people exactly
  what to phrase around.
- Nothing. This would need read grants kept outside the store's own
  declaration, and members could not tell who reads what they write.

**4. May every reader of a store see who else can read, propose to and compile
it?** The declaration is in the store, so by default they can.

- **Recommended: yes.** People should be able to tell who will read what they
  put in a store. Small teams can list members directly and need no directory.
- Group names only, with membership held in an external directory. Hides
  individuals; requires every deployment to run a directory adapter.
- Keep the declaration on the bench, visible to the compile role only. Readers
  learn nothing about each other, and cannot check their own access.

**5. When a cloud librarian compiles a store, who answers its decisions?** The
ruling gives the queue to the compile role, and in this mode that role is
software.

- **Recommended: named human stewards.** The declaration names the people who
  answer; the cloud librarian applies their answers and never settles such an
  item itself.
- Nobody. Items the librarian cannot settle stay open. No human load, and the
  store accumulates unresolved contradictions.
- Any reader may answer. Spreads the load widely and lets anyone who can read a
  store change it, which removes the point of a separate compile permission.
