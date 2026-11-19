<!-- SPDX-License-Identifier: Apache-2.0 -->

# Federation: the store access model (read, propose and compile)

**Status:** DESIGN, not built (Vitruvius Specify, 2026-11-18). Design note for
athenaeum#2623, a child of the federation design epic athenaeum#2631. The
operator answered the five questions in §13 on 2026-11-18, and the note is
written to those rulings. It was reconciled with the other three federation
notes by athenaeum#2806 (§15). No question in this note is open. The build
slices are in §12.

This note decides who may read a store, who may propose to it, who compiles it,
who answers its decisions, and how one librarian reviews another store on a
schedule. It does **not** decide:

- how several installs write one store (athenaeum#2624, written in parallel);
- how a fact is routed to a store at intake, or what a proposed record looks
  like (athenaeum#2625, athenaeum#2626);
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

1. **Access is enforced by the place the store is kept, not by athenaeum.** The
   engine fixes four rights: *read*, *propose*, *compile* and *answer*. A
   store is kept in up to three separately permissioned locations: its
   *pages*, a *raw location* for the part of its raw intake that proposers
   deliver, and its *bench*. Each right is the ability to fetch or push one of
   them. An install that lacks a right does not have the bytes, so there is
   nothing for athenaeum to filter. There is no inbox: a proposal is a record
   in the store's raw intake, like every other write (§4).
2. **The store carries a declaration of who holds which right.** It names its
   own roles and maps each to rights. The engine ships no role names. It names
   principals and groups, never credentials. Installs read it to decide their
   own behaviour (compile or not, show the decision queue or not, mount or refuse), and a
   check compares it with what the host actually allows. The declaration grants
   nothing by being written.
3. **Every athenaeum-side rule only narrows.** An install writes a store only
   when its local config allows it, the declaration gives it the right and the
   host accepts the push. Any one of the three can say no; none can say yes alone.

## 2. What exists today

Code anchors below, and throughout this note, are symbols at `develop`
`60f851e8`.

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

There is no store-level right, no propose right and no notion of who
compiles a store. athenaeum#2630 holds an interim rule in their place: one
compiling install per shared store.

## 3. Terms

| Term | Meaning |
|---|---|
| **Install** | One knowledge directory with its own librarian and its own index. |
| **Store** | A set of pages with one access boundary. Logically it has raw intake, pages, operational state and configuration ([North star](../north-star.md) §2.11). Physically, a surface in its own repository plus, when needed, a raw location and a bench (§4). |
| **`store_id`** | The one identifier of a store: `st_` followed by 12 random lowercase base32 characters. Every note, rule, record and config entry that names a store uses it (§5). |
| **Right** | One of the four things the engine lets a principal do with a store: read, propose, compile, answer (§4). |
| **Role** | A name a store's declaration gives to a set of rights and the principals who hold them. Role names belong to the deployment. The engine ships none and no code path keys on one. |
| **Principal** | One person, software agent or organisation, named by an `asserter` block. |
| **Group** | A named set of principals, resolved through a directory adapter. |
| **Compile role** | Shorthand, in these four notes only, for the principals who hold the compile right or the answer right on a store: those who turn its raw intake into pages and answer its decisions. It is not a role name in any file or interface. |
| **Steward** | Shorthand for a principal who holds answer and not compile. Also not a name in any file or interface. |
| **Host** | Whatever keeps a store's locations and decides who may fetch or push them. Today a git host. |

The store, not the page and not the type, is the **smallest unit of physical
access**. A group that wants to expose half its pages splits them into two
stores. Page-level `audience:` remains and is a different axis (§10).

## 4. The four rights, and where a store is kept

**The engine fixes four rights.** What a deployment calls the people who hold
them is its own business (operator ruling 2026-11-18, athenaeum#2806 point 2).

| Right | What it lets a principal's install do |
|---|---|
| **read** | Hold a copy of the pages and build them into its own index. |
| **propose** | Deliver a record into the store's raw intake. It is not a page until an install that holds compile has compiled it. |
| **compile** | Read the store's raw intake, write its pages and raise its decisions. Requires read. |
| **answer** | See the store's decision queue and answer the items in it. Requires read, because an item quotes the store's pages. |

Apart from those two requirements the rights are independent. Two consequences
are deliberate:

- **Propose does not require read.** A person may be allowed to send a fact to
  a store they cannot read.
- **Read does not give propose.** The announcements store in the epic's worked
  example is readable by everyone and proposable only by leadership and
  selected department heads.

**There is no inbox** (operator ruling 2026-11-18, athenaeum#2806 point 3). A
suggestion from another person or agent is a write, and every write enters as
raw intake ([North star](../north-star.md) §2.1). A store therefore has no
place where proposals wait under rules of their own. A proposal is a
structured record with provenance in the store's raw intake. A compiling
install's ordinary intake run reads it, and intake handles that kind of
record in its own way (athenaeum#2625 §4.5, athenaeum#2626 §6).

**Where a store is kept.** Logically a store has raw intake, pages,
operational state and configuration. Physically it is kept in up to three
locations. Each is a repository, or anything else with the same fetch and push
semantics.

| Location | Holds | Who fetches | Who pushes |
|---|---|---|---|
| **Pages** | The compiled pages, and the store's configuration under `_store/`, including the declaration | read | compile |
| **Raw location** (`locations.raw`) | The part of the store's raw intake delivered by principals who hold propose and neither compile nor answer | compile | propose |
| **Bench** (`locations.bench`) | The store's operational state: the decision queue, the raise and acceptance ledgers, review cursors. And, under `raw/`, the part of the store's raw intake written by principals who hold compile or answer, including answers under `raw/answers/` | compile, answer | compile, answer |

The raw location is not a second way in. It is where part of the one raw
intake is physically kept. It has to be kept apart from the pages for one
reason only: propose does not require read, so a proposer needs somewhere they
may write that is not somewhere they may read.

A store's raw intake is therefore found in up to three places, and a compiling
install reads all of them with one handler and one set of rules:

| Who wrote the record | Where it is |
|---|---|
| A principal who holds propose, and neither compile nor answer | The raw location |
| A principal who holds compile or answer, on a store with a bench | The bench, under `raw/` |
| The single compiling install of a store with no bench | That install's own raw tree, `raw/_stores/<store_id>/` (athenaeum#2625 §8.1) |

**How a proposer who cannot read the store delivers a record.** The host gives
their principal push on a raw location, and nothing on the pages or the bench.
Their install writes one file per record, never edits a file it has written,
and syncs that location alone (athenaeum#2624 §8.1). The declaration is in the
pages repository, which they cannot fetch, so they learn the two things they
need, the `store_id` and the store's `intake` rules, from the proposer's
notice that the compile role writes into the raw location (athenaeum#2625 §7).

**What a proposer may read back.** From the raw location, and nothing more:

- the proposer's notices;
- their own records, until a compiling install has consumed them. A consumed
  record is removed, and its absence is all the proposer learns. They are not
  told what was done with it (athenaeum#2626 §6.2);
- other proposers' waiting records **only** in a raw location that proposers
  share. A shared raw location is allowed only when every principal who holds
  propose also holds read. As soon as one does not, each proposer has their
  own raw location (operator ruling 1, §13). The check refuses a declaration
  that breaks this rule.

A proposer reads no page, no decision, no ledger and no disposition through
the raw location.

**When a location can be omitted.**

- The raw location is omitted when nobody holds propose without also holding
  compile or answer.
- The bench is omitted only when exactly one install compiles **and** nobody
  holds answer without compile. The decision queue then stays in that
  install's own state, as it does today, and the store's raw intake is that
  install's own raw tree for the store. As soon as a second install compiles
  or answers, the bench is required and the check refuses a declaration
  without one.
- The bench may live in the pages repository when every principal who holds
  read also holds compile or answer, because then nothing in it is shown to
  anyone outside that set.

Keeping the raw location and the bench in step between installs is
multi-writer sync and belongs to athenaeum#2624, which owns sync for both
locations as well as for the pages. This note says only who may fetch and push
each. What a record looks like is athenaeum#2626, and how intake handles one
is athenaeum#2625.

The recipe store that athenaeum#2630 set up is the smallest case: a pages
repository, one compiling install, no raw location and no bench. It needs no
change.

## 5. How a store declares its rights

The declaration is one file in the pages repository, `_store/access.yaml`. The
`_` prefix follows the convention the index scan already skips for the wiki
(`src/athenaeum/search.py:_wiki_relpath_excluded`); slice S1 pins the same skip
for every surface.

```yaml
version: 1
store_id: st_q7m2xk4vwz9d        # the one identifier of this store (below)
directory: static                 # which directory adapter resolves group names
groups:                           # read by the static adapter only
  leadership:
    - {iss: "https://git.example.net", sub: "1042"}
    - {iss: "https://git.example.net", sub: "1077"}
  department-heads:
    - {iss: "https://git.example.net", sub: "1203"}
roles:                            # the names are this store's own; the rights are the engine's
  staff:
    rights: [read]
    members: ["*"]                # anyone the host admits
  contributor:
    rights: [propose]
    members: ["group:department-heads"]
  editor:
    rights: [read, propose, compile, answer]
    members: ["group:leadership"]
sync:
  mode: integrate                 # fast_forward | integrate (athenaeum#2624)
locations:
  raw: "git@git.example.net:acme/announcements-raw.git"
  bench: "git@git.example.net:acme/announcements-bench.git"
reviews: []                       # §8
```

**`store_id` is the one identifier of a store** (operator ruling 2026-11-18,
athenaeum#2806 point 1). It is `st_` followed by 12 lowercase base32
characters (60 random bits), minted once with `secrets` when the declaration
is first written, and never reused. It is opaque: not a name, a URL or a path.
athenaeum#2772 validates the shape. Everything that names a store names it by
this id: a routing rule (athenaeum#2625 §5.1), a review's `source`, a raised
copy's `origin` (athenaeum#2626 §5), a decision that involves another store's
page (§7). An install's own name for a store, the key of its entry under
`storage.adapters`, is local and appears in no rule and no record. The
install's config maps the id to that entry: each adapter entry that mounts a
declared store carries `store_id`, and the install refuses the mount when it
differs from the id in the declaration it fetched.

**Roles are the store's own; the rights are the engine's.** Each entry under
`roles` has a name the deployment chose, a list of `rights` drawn from `read`,
`propose`, `compile` and `answer`, and its `members`. A principal's rights on
the store are the union of the rights of every role that lists them. No code
path reads a role name except to print it. The validator refuses a role whose
rights include `compile` or `answer` without `read`.

Why it lives in the store and not in each install's `athenaeum.yaml`: every
install then acts on the same statement, and every change to it is a commit
with an author and a diff. A per-install copy could disagree with its
neighbours, and a member could name themselves a compiler in their own file.

`sync.mode` is declared here, once for the whole store, and not in each
writer's local config. It says whether the store's writers integrate each
other's work, in the sense athenaeum#2624 defines; the default,
`fast_forward`, is today's behaviour. This note does not define what
integrating means. It uses the key for one thing: the check accepts more than
one compiling install only on a store that declares `integrate` (§7). A local
setting could not serve, for the same reason the rest of the declaration is not
local: installs could disagree, and no other install could verify it.

**The declaration is a statement, not a grant.** Writing a principal into a
role that holds `read` does not give them a clone; only the host does that. Removing
them does not take a clone away. The declaration is used for four things:

1. **Rights resolution.** Each install resolves its owner
   (`src/athenaeum/config.py:resolve_owner_asserter`) against the declaration
   and learns which of the four rights it holds on this store. The result is a
   set of rights, never a role name.
2. **Narrowing.** An install that does not hold compile refuses to write the
   pages even if its local config says `read_only: false` (§9).
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

One reserved word: `"*"` means anyone the host admits. It is valid only among
the `members` of a role whose rights are exactly `[read]`.

`"*"` defers the read decision to the host entirely. Rights resolution gives
read to any install that holds the store, and the check treats `"*"` as
matching every principal the host admits, so it never reports a read
difference for such a store. It is refused in any role that holds propose,
compile or answer, where an unnamed principal would defeat the point of the
declaration.

**Resolution is never on the recall path.** Groups are resolved when a run
starts and when the check runs. An install whose directory cannot answer acts
as a reader only: it does not compile, does not run reviews and does not show
the store's decision queue. Failure narrows; it never widens.

**The install's own identity is self-declared**, and that is acceptable for
rights resolution only. A member who puts someone else's identity in
`owner.asserter` changes what their own install *attempts*; the host still
refuses the push. Whether a proposed record or a commit really came from the principal
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

The 2026-11-14 ruling lets each store choose who runs its librarian: a cloud
librarian, named members, or every member. With rights there is no mode key.
Each choice is a way of writing the roles.

| Who runs the store's librarian | How the declaration says it | Who else holds the pages |
|---|---|---|
| A cloud librarian | A role with `[read, compile]` whose one member is a `software_agent` principal, and a second role with `[read, answer]` for the people who answer its decisions | Every other member mounts the pages read-only |
| Named members | A role that holds compile, and usually answer, listing those principals and groups | Other readers mount read-only |
| Every member | One role that holds all four rights and lists every member | Nobody |

**The compile role owns the store's decision queue.** Concretely:

- **A decision belongs to the store whose page it would change.** Merges never
  cross a store, so a merge proposal always has exactly one owning store.
- **Only installs that hold compile raise decisions about a store's pages.** A
  reader's install runs no merge, contradiction or repair pass over a store it
  cannot write, which is what `read_only` already means.
- **The decision queue lives on the bench.** Where the bench is omitted (one
  compiling install, nobody holding answer without compile) it is that
  install's own state. A declaration whose bench is readable by anyone who
  holds neither compile nor answer is refused by the check.
- **A decision shows only the pages of the store that owns it.** Access is not
  nested: two stores can each be narrow and have no reader in common, so no
  ranking of stores says whose compile role may see both. When a decision
  involves pages from other stores (a contradiction between a page here and a
  page in a store this install also reads, for example), the item written to
  the decision queue quotes the owning store's pages and names every other page
  by `store_id` and uid alone: no text, no title, no path.
- **Other stores' pages are filled in on display, from the viewer's own
  mounts.** A compile-role member whose install holds the other store sees its
  page beside the item, read from their own copy. A member whose install does
  not hold it sees that a page they cannot read is involved, and leaves the item
  to a member who can. Nobody is shown a page through the decision queue that
  they could not already read.
- **A decision changes only the store that owns it.** If the other store's page
  should change too, that is a second decision, raised in that store's decision
  queue by an install that compiles it, or a record proposed to its raw
  intake. It is never a direct write from here.
- **Every member of the compile role sees the whole decision queue.** Sending
  each item to the member most likely to know is athenaeum#2629 and is not
  designed here.

**One listing, across every store an install can answer for** (athenaeum#2806
point 8). An install has one listing of open decisions, retrievable in one
call (`src/athenaeum/decisions.py:list_pending_decisions`;
[North star](../north-star.md) §2.8). With several stores it stays one
listing. It spans the personal store and every store on which the install's
owner holds answer, and each item names the store that owns it by `store_id`.
A store on which the owner does not hold answer contributes nothing to it. The
queues stay separate on disk, one per store, so that nobody fetches a queue
they may not see; the listing joins them on the install that holds them. An
answer goes back the way every write goes: it is a record in the raw intake of
the store that owns the decision (the bench's `raw/answers/`, or the single
compiling install's own raw tree), and a compiling install applies it.

When the librarian is software, a decision is by definition something software
could not settle. The declaration therefore gives the answer right to people:

```yaml
roles:
  librarian:
    rights: [read, compile]
    members: [{iss: "https://git.example.net", sub: "svc-librarian", type: software_agent}]
  steward:                        # this store's own name for the role
    rights: [read, answer]
    members: ["group:leadership"]
```

Principals who hold answer fetch and push the bench. They read the decision
queue there and leave answer records under its `raw/answers/`, in the existing
`raw/answers/` shape. They do not push the pages. Anyone holding answer
without compile therefore makes the bench required (§4). The check refuses a
declaration in which every principal that holds compile is a software agent
and no person holds answer (operator ruling 5, §13).

**Dependency on athenaeum#2624.** Two installs writing the same pages, and two
people answering the same decision, are multi-writer problems, and
athenaeum#2624 owns them: the sync sequence for the pages, sync for the bench
and the raw location, and pairing concurrent answers. The check enforces the boundary from
this side:

- A declaration that puts more than one install in a position to push the
  pages (its roles give compile to more than one principal) is accepted only
  when the store declares `sync.mode: integrate`. Otherwise it is
  refused. That is the athenaeum#2630 interim rule, now stated by the store
  instead of assumed.
- A store with one principal holding compile has one writer of the pages and
  needs nothing from athenaeum#2624 for them. With other principals holding
  answer it still has several writers of the bench, which is why S9 waits for
  bench sync.

## 8. Scheduled cross-store review

A librarian that compiles one store (the *target*) may review another (the
*source*) on a schedule and raise what it finds. The operator's example: the
leadership librarian looks at a lower group's store each week, and a
configurable prompt decides what to escalate.

**Which rights it needs.** Two, and both are ordinary:

- **read on the source**, held by the principal whose install runs the review.
  The install mounts the source read-only, like any other reader. There is no
  review-specific way to read a store and no grant for part of one.
- **compile or propose on the target**, because the review's output is a
  set of records proposed to the target's raw intake. Which of the two it
  holds decides where they are written (below).

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
roles:
  reader:
    rights: [read]
    members: ["group:field-team", {principal: "group:leadership", purpose: review}]
```

`purpose: review` changes nothing about what the read right allows. It is there so
the members of the source store can see who reads their store and why.

**Where the prompt lives.** In two places, split by who owns the words:

- The **criteria** (what counts as worth escalating) are the deployment's. They
  live in a markdown file in the target store, at the path the review names, so
  they are versioned and editable as prose by the people who own the target.
- The **frame** (the instruction to judge source text against the criteria, and
  the output shape) is the engine's. It is a file,
  `src/athenaeum/prompts/store_review_frame.md`, beside the page-recompile
  prompts that already live in that directory, and it is indexed in
  `src/athenaeum/prompt_registry.py` with a golden like every other engine
  prompt.

Source pages are passed to the model as untrusted data, in the same way the
classifier treats raw intake (`src/athenaeum/tiers.py:CLASSIFY_SYSTEM`). What a
hostile source page can do to a reviewing librarian beyond that is
athenaeum#2628.

**What it may write.**

| Where | What | Why it is safe |
|---|---|---|
| The target's raw intake (see below) | Records, in the format athenaeum#2626 defines, with provenance and no link back to the source | They pass through the target's normal intake, compile and decision queue before becoming pages. A review never writes a page directly. |
| The target's bench, or the reviewing install's own state when there is no bench | A cursor (the last source revision reviewed) and a run record (time, counts) | Neither contains source text. |
| The source | **Nothing.** No page, no record, no marker, no cursor. | The reviewing install holds read only, and the host refuses anything else. |

**Where the records go** depends on the right the reviewing install holds on
the target. In every row it is the target's raw intake (§4):

| The reviewing install holds | Records are written to |
|---|---|
| compile on the target, and the target has a bench | The bench, under `raw/` |
| compile on the target, and the target has no bench (one compiling install, nobody else answering) | That install's own raw tree for the target store, `raw/_stores/<store_id>/` |
| propose only | The target's raw location |

The first two rows are the operator's own example: a leadership librarian
reviewing into a leadership-only store, where nobody proposes without
compiling and so no raw location exists. In every row the record waits where
only the target's compile role can fetch it, and in none of them is it a page.

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
| **2. The declaration** | An install mounts a declared store only if its principal holds read, and writes its pages only if it holds compile | A copy that outlived its grant being indexed; a misconfigured install compiling a store it should only read |
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
  This note does not claim today's index has that property; it is built as its
  own slice (S4b), and removal on unmount (S4c) waits for it.
- **A store's derived data must be protectable with that store's key alone**,
  so that losing the key to one store does not expose the rest.

The raw location and the bench are never added to the recall index, and
neither is `raw/_stores/`. A record waiting for a shared store is not part of
any install's recall until it is a page. The config-time root checks that
already keep non-indexed surfaces out of the index
(`src/athenaeum/storage.py:validate_indexed_surfaces`) are extended to refuse a
configuration that would index any of the three.

**The check.** `athenaeum store access check` reports, per mounted store:

- the rights this install holds according to the declaration;
- what the host has actually allowed this install, from evidence and not from
  a probe: whether the last fetch of each location completed, and the recorded
  outcome of this install's last real push to it. The check does not test
  whether a push *would* be accepted. No portable way to ask exists (a dry-run
  push does not reliably exercise the host's permission check), so an install
  that has never pushed reports "push: not yet observed". Asking the host
  directly is left to an external adapter (S12);
- any difference between that evidence and the declared rights;
- declarations the one-writer rule refuses (§7), a principal holding answer
  without compile on a store with no bench, a bench readable beyond the
  compile role, a shared raw location on a store where some proposer does not
  hold read (§4), a software-only librarian with nobody holding answer (§7),
  and reviews with no matching read right in the source;
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
  completed a pull unmounts the store and removes its derived data (§13,
  ruling 2);
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
- **Multi-writer sync** (athenaeum#2624) owns the sync sequence for the pages,
  the bench and the raw location, conflicts, and concurrent answers. This note says
  who is allowed to be a writer of each, keeps the decision queue on the bench,
  and declares `sync.mode` store-wide for that note to act on.
- **Intake routing** (athenaeum#2625) owns which store a fact goes to and how
  intake handles a record addressed to a store. **Raise and retract**
  (athenaeum#2626) owns the record format, provenance and retraction. This
  note supplies the `store_id` they cite and says where a store's raw intake
  is kept.

**Repository split.** The declaration parser, rights resolution, the directory
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
| A decision involves pages from stores with different readers | The decision queue item quotes only its owning store's pages and names the rest by `store_id` and uid. Each viewer's install fills them in only from stores it holds (§7). |
| A review reads a store it was not granted | Impossible by construction: the install does not hold it. A clone held without a declared grant is not mounted. |
| A hostile source page steers a reviewing librarian | Contained to a record in the target's raw intake. Anything further is athenaeum#2628. |

**Migration.** Each step is additive. A store gains a declaration when someone
commits the file; until then it behaves as today. Adding a raw location or a bench
means creating the repository and naming it under `locations`.

**Rollback.** Delete the declaration and the store returns to local-config
behaviour. No page format changes, no uid changes and no index format changes
are introduced by this note, so there is nothing to convert back.

**Principles applied.** The engine ships mechanism and the deployment supplies
policy. Enforcement is physical first and declared second. Every
athenaeum-side rule narrows. Existing seams are reused, not paralleled: the
identity key, the read-only guard, the artifact registry, the answer-file
shape. Storage stays logically fixed and physically pluggable
([North star](../north-star.md) §2.11): nothing in the access model depends on
the host being git, only on separately permissioned locations with fetch and
push. Multi-writer sync is narrower. A store with more than one install
pushing a location needs `sync.mode: integrate`, and athenaeum#2624 designs
that mode for git only (its §3.3). A store kept on anything else has one
writer per location until a sync design for that backing exists
(athenaeum#2806 point 9).

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
   branches for proposed records.** Rejected: a clone holds every branch, so
   propose would imply read and every reader would hold the store's raw
   intake. It also ties the
   model to one host's branch-protection feature.
5. **Have athenaeum provision the host's permissions from the declaration.**
   Rejected: athenaeum would hold administrator credentials for every store and
   become the permission system it is meant to stay out of.
6. **A separate inbox, where proposals wait under their own format and their
   own rules.** This was the first draft of this note. Rejected by operator
   ruling (2026-11-18): it is a second way in. A proposal is a record in the
   store's raw intake (§4).
7. **Role names shipped by the engine** (reader, proposer, compiler, steward
   as values in the declaration and in the resolver's result). Also the first
   draft. Rejected by operator ruling (2026-11-18): the engine fixes rights,
   and a deployment names its own roles.
8. **A review-only grant covering part of a store.** Rejected: there is no way
   to hand over part of a repository. A group that wants to expose part of what
   it keeps splits the store.

## 12. Build slices

Each is one pull request. Slices S1 to S5 depend on nothing outside this note.
Slice numbers given for athenaeum#2624 are those of the build-slice table in
its design note.

| Slice | What it adds | Depends on |
|---|---|---|
| **S1** | Parse and validate `_store/access.yaml` (`version`, `store_id` and its `st_` shape, `directory`, `groups`, `roles` with their `rights` and `members`, `sync`, `locations.raw`, `locations.bench`, `reviews`), failing closed. Register the files under `_store/` in `src/athenaeum/store.py:ARTIFACT_REGISTRY` with persistence class `config`, so multi-writer merge has a rule for them. Pin that `_store/` is skipped by the index scan on every surface. Document the format. No behaviour change. | none |
| **S2** | The directory adapter protocol, the built-in `static` adapter, and rights resolution: given a surface and `owner.asserter`, return the set of rights the owner holds on that store, a subset of read, propose, compile and answer, empty when none. No role name is returned. | S1 |
| **S3** | Fold the declared rights into the write guard: an install that does not hold compile is refused on that store's pages, whatever its local `read_only` says. | S2 |
| **S4** | Refuse to mount a declared store on which this install does not hold read: it is not indexed and not recalled. Nothing is deleted. | S2 |
| **S4b** | Make the index separable by store: one store's derived data can be listed and removed without reading another's. | none |
| **S4c** | Remove a store's derived data when it is unmounted. | S4, S4b |
| **S5** | `athenaeum store access check`: declared rights against local config and against the recorded outcome of this install's last fetch and last real push per location (no push probe); the one-writer rule; answer held without a bench; a software-only librarian with nobody holding answer; a non-zero exit on any difference. | S2 |
| **S6** | `max_offline`: unmount a store whose last completed pull is older than its declared limit. | S4c; ruling 2 (§13) |
| **S7** | A decision queue per store on the decision seam: a decision is filed with the store whose page it would change, quotes only that store's pages, names pages of other stores by `store_id` and uid, and has them filled in on display from the viewer's own mounts. The one listing spans every store on which the owner holds answer, and each item names its store (§7). Single compiling install; the decision queue stays in local state. | S2 |
| **S8** | The `locations` entries: mount the bench and the raw location, refuse to index either, require a bench when anyone holds answer without compile, refuse a bench readable beyond the compile role, and refuse a shared raw location where some proposer does not hold read. | S5 |
| **S9** | Answering from a second install: an install whose owner holds answer shows the store's decision queue from the bench and writes answer records to the bench's `raw/answers/`; the compiling install applies them. | S7, S8; athenaeum#2624 S4 (record merge) and its bench and raw location sync slice |
| **S10** | The `reviews` block: parse it, check the matching read right in the source's declaration, compute "due", keep the cursor. No model call. | S2 |
| **S11** | The review run: the frame prompt file and its registry entry, the criteria file, records written to the target's raw intake (the bench's `raw/`, the install's own raw tree, or the raw location, by the table in §8). | S10, athenaeum#2626; S8 for the bench and raw location rows |
| **S12** | The external directory adapter registration point, and the check's host-side comparison when an adapter can list access or answer whether a push would be accepted. | S5 |
| **S13** | Accept more than one principal holding compile in the check when the store declares `sync.mode: integrate`; refuse it otherwise. | S5; athenaeum#2624 S4 and S7 (`integrate` mode), and its bench and raw location sync slice where the store has either |
| **S14** | A guide: setting up a store with readers, proposers and a compile role. | S5, S8 |

**Trust (athenaeum#2806 point 7).** The trust design (athenaeum#2628) is on
hold. Until it reports, the rule for every slice above is: **no slice may rely
on a writer being trusted or untrusted.** No slice takes a measure of how far
a writer is believed as an input, and no slice's safety argument assumes a
writer is honest. Under that rule every slice in the table may be built: each
is enforced by the host, by a declared right, or by a deterministic check that
treats every writer alike. What may not be built before the trust design
reports has no slice here: verified writer identity on a shared store (§6),
and routing a decision to the member most likely to know (athenaeum#2629).

*Placeholder for the eval that would later show a store resists a hostile
writer.* A fixture store with several writers, one of them hostile. The
hostile writer submits records that carry instructions in their text, values
attributed to the wrong person, and proposer claims naming someone else. The
eval measures three things: how many hostile values reach a published page
with no person deciding; whether any hostile record changes what a librarian
does with another writer's records; and whether every hostile value that was
published can be traced to its writer and undone by one ordinary commit. The
thresholds, and anything that weighs one writer against another, belong to the
trust design and are not set here.

## 13. Operator rulings

No question is open. The note put five questions. The operator accepted the
recommended answer to each on 2026-11-18, recorded on athenaeum#2631. They are
stated below as rulings, with the options that were not chosen kept for the
record. Rulings 1 and 5 were given in the first draft's words, "inbox" and
"stewards"; they carry over unchanged to the raw location and to the answer
right (§4).

### Ruling 1. Proposers see each other's waiting records only when every proposer can read the store

On most hosts the right to push to a repository includes the right to fetch
it, so a shared raw location shows every proposer what the others sent. It is
allowed only when every proposer can also read the store. As soon as one
cannot, each proposer has their own raw location (§4). Not chosen: always one
per proposer, which costs a repository per person per store; and always
shared, which shows a person's report to every other proposer.

### Ruling 2. Each store may declare an offline limit; the engine sets none

A store may declare `max_offline`, after which an install that has not
completed a pull unmounts the store and removes its derived data (§9). A store
that declares nothing keeps today's behaviour. Not chosen: an engine default
for every declared store, which breaks long offline use; and no limit
mechanism at all.

### Ruling 3. Members of a reviewed store see who reviews it and why, not the criteria

The source's own declaration shows the reviewing group and `purpose: review`.
The criteria stay in the reviewing store (§8). Not chosen: showing the
criteria as well, which tells people what to phrase around; and showing
nothing.

### Ruling 4. Every reader of a store can see its declaration

Who can read, propose to, compile and answer for a store is visible to
everyone who can read it (§5). Not chosen: group names only, with membership
in an external directory; and keeping the declaration on the bench.

### Ruling 5. A software librarian's decisions go to named people

When the principal that compiles a store is software, the declaration gives
the answer right to named people, which requires a bench (§7). Not chosen:
nobody answers, so unresolved items accumulate; and any reader may answer,
which removes the point of a separate compile right.

### Rulings that apply to all four notes (2026-11-18)

- **Role names are defined by the deployment.** The engine fixes the rights:
  read, propose, compile, answer (§4, §5).
- **One store identifier:** `store_id`, with an `st_` prefix (§5).
- **There is no inbox.** A proposal enters through raw intake (§4).

## 14. How this note meets the north star

Sections are those of [North star](../north-star.md).

**§2.1, one way in and one way out.** Nothing in the access model adds a way
in. A proposer's record, a review's output and a person's answer are all
records in the store's raw intake (§4, §7, §8), compiled by an install that
holds compile. The raw location is where part of that intake is kept, not a
second entry. Nothing adds a way out either: a reader's install holds the
pages and serves them through its own recall, and an install that may not read
a store does not hold it (§9), so no caller opens a store they were not given.

**§2.2, one ladder; conformance sets depth, never admission.** The rights
decide *who may deliver* a record, which is the host's admission of a
principal, not a judgement of a record's shape. No right, role or location
defines a typed entry point: every record that arrives goes onto the one
ladder, and its shape only sets how low it enters (athenaeum#2625 §4.5,
athenaeum#2626 §6). This note adds no model call to the ladder except the
review's selection (§8), whose tier is chosen by measurement like any other.

**§2.8, one queue of questions, answered through the one way in.** Each
store's decisions are kept apart so that nobody fetches a queue they may not
see, and each install shows one listing that spans every store its owner may
answer for (§7). An answer is a record in the owning store's raw intake,
applied deterministically by a compiling install. No answer writes a queue or
a page directly.

**§2.12, filing is verification.** This note files nothing itself. It fixes
who is the librarian of a store: the installs that hold compile. They own what
they file there, and the filing checks bind them on every path into the
store's pages, whichever location a record arrived through. The checks
themselves, and when they run before a push, are athenaeum#2624 §4.3 and
athenaeum#2626 §4.4.

## 15. Points settled by athenaeum#2806

| Point | Answer | Where |
|---|---|---|
| 1. Store identifier | `store_id`, `st_` and 12 base32 characters, minted once and validated by athenaeum#2772. Rules and records name a store by it; the install's config maps it to an adapter entry. | §3, §5 |
| 2. Role names | The engine fixes four rights. A declaration names its own roles and maps each to rights. Resolution returns a set of rights. | §4, §5, §7, S2 |
| 3. Where proposals wait | No inbox. In the store's raw intake, kept in the raw location, on the bench under `raw/`, or in the single compiling install's own raw tree. How a proposer delivers, and what they may read back, are stated. | §4 |
| 4. Source rank of a raised copy | Not set in any of the four notes. athenaeum#2626 §5.1. | — |
| 5. Checks before a write is published | Proposed, pending operator review: athenaeum#2624 §4.3 and athenaeum#2626 §4.4. | — |
| 6. One conformance format | Proposed, pending operator review: athenaeum#2626 §6.1. | — |
| 7. Writer trust | No slice may rely on a writer being trusted or untrusted. Every slice here may be built. Placeholder eval named. | §12 |
| 8. One queue across stores | One listing spans every store an install's owner may answer for. | §7 |
| 9. Small mismatches | The access model does not require git; multi-writer sync does. | §11 |
