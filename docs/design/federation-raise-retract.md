<!-- SPDX-License-Identifier: Apache-2.0 -->

# Federation: the raise and retract protocol between stores

**Status:** DESIGN, not built (Vitruvius Specify, 2026-11-18). The design note
athenaeum#2626 asks for, a child of the federation design epic athenaeum#2631.
Nothing here is running code. The note ends in build slices (§14) and in the
questions only the operator can answer (§15).

Code anchors are `path:symbol` on `develop` at `759e3b64`.

**Builds on, and does not redesign:**

- **The store access model**, [Federation: store access](federation-store-access.md)
  (athenaeum#2623): grants, the inbox, the bench, scheduled review and the
  review criteria file. Its terms are used here unchanged (§3).
- **Multi-writer sync**, [Federation: multi-writer sync](federation-multi-writer-sync.md)
  (athenaeum#2624): the sync sequence, the append-only inbox, record merge,
  uids, the same uid in two stores and the erasure mark.

**Does not decide:**

- **Which store a fact goes to at intake**, and where raw intake for a store
  waits (athenaeum#2625, written in parallel). This note says where it depends
  on that one and specifies nothing of it.
- **Which facts deserve raising.** That is the review criteria file of
  athenaeum#2623 §8 for a scheduled review, and a person's choice otherwise.
- **Trust and poisoning across writers** (athenaeum#2628) and **routing a
  decision to the member most likely to know** (athenaeum#2629). Both are held
  for an external review. Nothing here designs for them, and no trust score is
  an input to any rule below. Where a question belongs to them, the note says
  so and stops.
- **Encryption** (athenaeum#2627), beyond not blocking it (§11).

---

## 1. Summary

1. **A raise is a copy.** The raising install takes one page of the *origin*
   store, strips everything that points back into the origin, and sends the
   result to the *target* store as a proposal. If the target's compile role
   accepts it, the target gains a new page with its own uid.
2. **The copy carries a provenance record and no link.** The record names the
   origin store by its opaque `store_id`, the proposer as the target's own
   declaration names them, a hash of the copied content, and the day. None of
   those tells a reader of the target anything they could not already see (§5).
3. **Only the raiser can connect the copy to its source.** It keeps a *raise
   ledger* that maps an opaque `raise_id` to the source page. The target never
   receives the source's uid, path or revision.
4. **Retract and supersede are messages from the raiser**, sent by the same
   route as the raise. Each names the `raise_id` and nothing else about the
   origin. The target finds the copy from the `raise_id` alone, with no index
   and no scan (§7).
5. **Nothing in the target is rewritten on the origin's word except the copy
   itself.** Pages in the target that depend on a withdrawn copy are listed in
   one decision for the target's compile role (§8).
6. **Push and pull are one protocol.** A member raising a page by hand and a
   librarian raising on a schedule write the same proposal to the same three
   places (§9).
7. **This note adds no model call and no prompt.** Every rule below is
   deterministic. The only model call near it is the review of
   athenaeum#2623 §8, which selects pages and writes no page text.

**Where each acceptance criterion of athenaeum#2626 is answered.**

| Criterion | Where |
|---|---|
| The provenance record, and that it discloses nothing a target reader may not see | §5 |
| The proposal format, and where proposals wait | §6 |
| Retract and supersede: who emits, how they find the raised page, dependent pages | §7, §8 |
| The source store unreachable, or the proposer losing their grant | §10 |
| Push-based and scheduled pull-based raising | §9 |
| No plaintext local index assumed | §11 |
| Build slices | §14 |

---

## 2. What is true today

Nothing crosses a store today. What exists is retraction and supersession
inside one store, and the rules that keep stores apart.

| Behaviour | Anchor |
|---|---|
| A page is marked replaced by a `superseded_by` frontmatter pointer, which holds the winner's name slug. The page stays on disk. | `src/athenaeum/models.py:parse_superseded_by` |
| Recall keeps a superseded page reachable, sorts it after the other hits and marks it. | `src/athenaeum/mcp_server.py:_is_superseded` and the hit partition that calls it |
| Unattended supersession stamps `superseded_by` on the losing page and records the retired passage in `superseded_claim`. It raises instead of doing nothing. | `src/athenaeum/supersession.py:enact_supersession` |
| A page folded into another stays on disk as a tombstone: `status: folded`, `folded_into: <slug>`, `embedded: false`. | `src/athenaeum/models.py:stamp_tombstone`, `is_tombstone`, `tombstone_target` |
| Retiring pages by uid is two commits: a snapshot, then `git rm` together with the withdrawal of pending merge proposals that name the page and a rebuild of the wiki index. Recovery is from git. | `src/athenaeum/retire_pages.py:apply_retirement` |
| A correction to an observation is an append-only record `(retracts, reason, at)`. The observation is never edited. | `src/athenaeum/pii.py:Supersession` |
| The retraction cascade compares those records with the merge-provenance ledger. When a completed merge listed a retracted observation among its sources, it appends one review record to `_pending_retractions.jsonl`. It never undoes the merge. The review appears in the decision queue as `type: "retraction"`. | `src/athenaeum/retraction_cascade.py:scan_retraction_cascade` |
| The cascade is exposed as an MCP tool of the same name. | `src/athenaeum/mcp_server.py:scan_retraction_cascade` |
| A page's content hash is SHA-256 over its claim content, with system metadata keys left out. | `src/athenaeum/verdicts.py:content_hash` |
| References to a page take four shapes: a `related` row's `uid`, the `parent:` key, a `[[wikilink]]` and a markdown link. | `src/athenaeum/store_links.py` module docstring; `src/athenaeum/page_recompile_undo.py:find_inbound_links` |
| A link from a more widely shared surface to a more private one is refused at the page write boundary, and the refused write is parked. | `src/athenaeum/store_boundary.py:link_forbidden`; `src/athenaeum/store_links.py:guard_entity_store_links` |
| Merges never cross a surface. | `src/athenaeum/store_boundary.py:assert_same_surface` |
| A source on a page is a typed `<type>:<ref>` descriptor. | `src/athenaeum/provenance.py:SourceRef` |

Two things follow for this note.

- **The cascade's principle carries over.** It makes a dependency visible and
  leaves the judgement to a person. §8 does the same across stores.
- **The cascade's mechanism does not.** It matches a retracted observation id
  against a ledger in the same knowledge directory. Across stores there is no
  shared ledger and, by ruling, no link from the copy to its source.

---

## 3. Terms

The terms of athenaeum#2623 (store, install, principal, group, compile role,
host, pages, inbox, bench, steward, decision queue, review, criteria) and of
athenaeum#2624 (writer, proposer, integrate, record merge) are used unchanged.
This note adds:

| Term | Meaning |
|---|---|
| **Origin** | The store a raised page is copied from. |
| **Target** | The store that receives the copy. |
| **Raiser** | The install that raises. It holds read on the origin and propose or compile on the target. |
| **Payload** | The content that travels: one page of the origin after the projection in §4.2. |
| **Copy** | The page the target creates from an accepted payload. |
| **`raise_id`** | An opaque random id for one raise, minted by the raiser. It is the only name the two sides share. |
| **Raise ledger** | The raiser's record of what it raised: `raise_id`, source page, target, and the hash it last sent. |
| **Acceptance ledger** | The target compile role's record of what arrived and what it did with it. |
| **Message** | One file in the format of §6: a raise, a supersede, a retract, a lapse or an attest. |

"Raise" does not imply a direction. Access is storage RBAC and not a hierarchy
(athenaeum#2631), so a raise goes from any store to any store where the grants
of §4.1 hold.

---

## 4. The raise

### 4.1 Which grants a raise needs

| On | The raiser's principal needs | Why |
|---|---|---|
| The **origin** | Read, and either membership of its compile role or a read grant declared with `purpose: review` | See below. |
| The **target** | Propose or compile | The raise is a proposal to the target (athenaeum#2623 §4). |

Read alone lets a person copy text out of a store by hand, and nothing in
athenaeum can stop that. The question is what the engine will do for them.
This note's answer is that the engine raises out of a store only for its
compile role, or for a reader the store itself declared as a reviewer. That is
§15 question 5.

This is the one place this note asks code to read the `purpose` key of
athenaeum#2623 §8. The grant itself allows exactly what it allowed before: the
host hands over the same bytes. The rule narrows what athenaeum will do with
them, and every athenaeum-side rule only narrows (athenaeum#2623 §1).

A raiser needs no read on the target. Propose does not imply read
(athenaeum#2623 §4), and the protocol never asks the raiser to look at the
target.

### 4.2 The unit and the projection

**The unit of a raise is one whole page.** A group that wants to raise half a
page splits the page, in the same way a group that wants to share half a store
splits the store (athenaeum#2623 §3).

The payload is a deterministic projection of the source page. No model takes
part, and no text is written that is not already on the source page.

| Part of the source page | In the payload |
|---|---|
| `type`, `name`, `description`, `aliases`, `tags` | Copied. |
| `access`, `audience` | Copied. Sync never widens or narrows them (athenaeum#2624 §5.1), and neither does a raise. |
| Body text | Copied, after link handling (below). |
| `uid` | Dropped. The target derives its own (§4.4). |
| `sources` | Dropped. They name raw files and sessions inside the origin. |
| `parent`, and every `related` row | Dropped. They name origin pages by uid. |
| `created`, `updated`, and every other key | Dropped. The copy's dates are the target's. |
| A `[[wikilink]]` or markdown link to another page | Replaced by its label as plain text. |

Dropping the links is what "no link back down" means in practice. The existing
write guard (`src/athenaeum/store_links.py:guard_entity_store_links`) stays in
place behind it as defence in depth. The target's own compile may later link
the copy to target pages; that is ordinary work inside one store.

The **content hash** of a payload is `src/athenaeum/verdicts.py:content_hash`
over the payload.

### 4.3 What the raiser records

For each raise the raiser appends one row to its raise ledger:

```json
{"v": 1, "raise_id": "rs_k3v9t2hq8m4xw7pd5n6b", "seq": 1,
 "origin": "st_f2n8rb5hjc3t", "source_uid": "5e1d09c3a7b24f86",
 "target": "st_q7m2xk4vwz9d", "route": "proposer",
 "content_hash": "sha256:…", "sent_at": "2026-11-18T09:30:00Z",
 "state": "sent"}
```

The ledger holds ids and a hash, and no page text. It is a record file in the
sense of athenaeum#2624 §5.3: append-only, keyed by `raise_id` and `seq`,
merged by record and never as text.

**Where the ledger is kept** follows who decided to raise, because that is who
must later notice the source changing:

| The raise was decided by | Ledger location |
|---|---|
| The origin's compile role, and the origin has a bench | The origin's bench. Every compiling install of the origin then watches the source. |
| The origin's compile role, and the origin has no bench | That install's own state. |
| A review declared by the target (athenaeum#2623 §8), and the target has a bench | The target's bench, beside the review cursor. |
| A review, and the target has no bench | The reviewing install's own state, beside the cursor. |

A raiser never writes the ledger into the origin's pages, and a reviewer
writes nothing at all to the origin (athenaeum#2623 §8).

A ledger on the target's bench names an origin page by `store_id` and uid.
That is the form athenaeum#2623 §7 already allows for a decision that involves
another store's page: an id, and no text, title or path.

### 4.4 Acceptance

A raise waits where §6.2 says until the target's compile role acts on it. The
compile role does one of two things.

**Reject.** The message is removed, and the acceptance ledger records the
`raise_id` as rejected with a reason code. Later messages for that `raise_id`
are dropped by rule.

**Accept.** The target creates a new page:

- **Content:** the payload, unchanged. A raise is accepted whole or not at
  all. Editing, merging with an existing target page and de-duplication happen
  afterwards, through the target's ordinary compile and its ordinary merge
  proposals, which never cross a store.
- **Uid:** derived, not minted at random:
  `sha256("athenaeum-raise-v1" ‖ target store_id ‖ raise_id)`, first 16 hex
  characters. Two reasons. Two compiling installs that accept the same
  proposal at the same moment produce the same page at the same path, which
  athenaeum#2624 §7.4 already merges as "the same thing added twice with a uid
  supplied from outside". And a later message finds the copy from its
  `raise_id` alone (§7.2). The uid is still unpredictable, because the
  `raise_id` is random, and it is 64 bits wide like every new uid
  (athenaeum#2624 §7.1). Because the target's `store_id` is part of the input,
  one raise accepted by two targets gives two different uids, so it cannot
  cause the collision of athenaeum#2624 §7.5.
- **A uid that is already taken** by a page with a different `raise_id`, or
  with none, is never overwritten. The proposal is held and a decision is
  raised.
- **Provenance:** the `raised_from` block of §5.
- **Record:** the commit that creates the copy carries one line,
  `Athenaeum-Raise: <raise_id> seq=<n> accept`, with ids only. The acceptance
  ledger gets a row with the same ids, the proposer's own identity, the
  location the message arrived through, the exact time and the blob id of the
  accepted version.

**Checks made by rule before anyone is asked.** The message parses; its
`target` is this store; the proposer holds propose or compile in the target's
declaration at the time the compile role acts; the payload has no link, no
`related` row and no `parent`; the page parses and has `type` and `name`; the
`content_hash` matches the payload; the `raise_id` is not already rejected. A
message that fails is not applied. It is recorded with a reason code and, for
anything other than a duplicate, raised as a decision.

**Whether a person is asked.** The store declares it, once, in its
declaration (athenaeum#2623 §5):

```yaml
raise:
  accept:
    review: auto        # raises made by a review this store declares
    proposer: ask       # raises sent by a member
  max_silence: 60d      # optional; see §10.4
```

`auto` means a raise that passes the checks above is accepted by the compiling
install without a decision. `ask` means it becomes a decision of a new kind,
`raise`, in the store's decision queue, showing the payload. Page types listed
in the store's `sync.always_ask` (athenaeum#2624 §3.3) are always asked,
whatever the route, so "always ask a person about this type" is stated once
per store. The defaults shown are §15 question 2.

A store with no `raise:` block asks for everything.

---

## 5. The provenance record

### 5.1 What the copy carries

```yaml
uid: 9c41e07ab35d2f68
type: principle
name: Year-end PTO usage report
sources: ["raise:rs_k3v9t2hq8m4xw7pd5n6b"]
raised_from:
  raise_id: rs_k3v9t2hq8m4xw7pd5n6b
  origin: st_f2n8rb5hjc3t
  proposer: "group:department-heads"
  via: proposer                 # proposer | review:<name>
  content_hash: "sha256:…"
  raised_on: 2026-11-18
  seq: 1
  watch: active                 # active | lapsed | ended (§10)
```

`raised_from` is system metadata. It is left out of
`src/athenaeum/verdicts.py:content_hash`, so stamping or updating it does not
change the page's content hash. The `sources` entry uses the existing typed
form (`src/athenaeum/provenance.py:SourceRef`) with a new type, `raise`.

### 5.2 Why each field discloses nothing new

The test for every field: could a principal who holds read on the target, and
nothing else, already see this?

| Field | What it is | Why a target reader may see it |
|---|---|---|
| `raise_id` | A random id minted for this raise | It is derived from nothing. It resolves only through the raiser's ledger, which no target reader holds. |
| `origin` | The origin's `store_id` | athenaeum#2623 §5 makes it opaque, random and never reused. It is not a name, a URL or a path. For a review it already appears in the target's own declaration, which every reader can see (operator ruling, athenaeum#2631). |
| `proposer` | **The entry in the target's declaration through which the proposer held the grant**: a principal if the declaration names the principal, otherwise `group:<name>` | It is copied from the declaration, which every reader of the target can already see. |
| `via` | `proposer`, or the name of a review | A review's name is in the target's declaration. |
| `content_hash` | The hash of the content the target accepted | It is the hash of a version of this page that is in the target's own history, which a reader holds. It is **not** a hash of the source page. |
| `raised_on` | The day on the proposal | It is a day, not a time, and it is the proposal's day, not a date from the source page. |
| `seq`, `watch` | Protocol state | They describe the copy. |

Three choices in that table are deliberate, and each closes a leak.

- **The proposer is recorded as declared, and not always as an individual.**
  A store may give propose to a group whose members are held in an external
  directory, and athenaeum#2623 gives each proposer their own inbox when a
  proposer cannot read the store. Printing the individual on the published
  page would tell every reader what the separate inboxes were built to keep
  apart. The individual's identity is kept in the acceptance ledger, which
  only the compile role can fetch. Whether to print it anyway is §15
  question 1.
- **The hash is of the accepted content, never of the source.** A hash of a
  source page would let a reader test guesses about text they cannot read. The
  payload is the accepted content, so the two hashes are equal for a raise
  accepted whole, and a raise is always accepted whole (§4.4).
- **The day, not the second.** The exact time stays in the acceptance ledger.

**What the record deliberately leaves out:** the source page's uid, path,
revision, dates and sources; the origin's name or location; the raiser's
install; and any text that is not on the copy.

**What a reader can still infer, stated plainly.** Two copies with the same
`origin` came from the same store, and a reader can count them. A reader who
also holds the origin recognises its `store_id`. Neither is new access to
content. A deployment that cannot accept even that correlation should not
raise between those two stores.

**The proposer field is a claim.** athenaeum does not verify that a message
came from the principal it names
([Writer identity](writer-identity.md)). Where each proposer has their own
inbox, the host decides who can push there. Verified writer identity on a
shared store is athenaeum#2628 and is not designed here.

---

## 6. The proposal format and where proposals wait

### 6.1 Format

One message is one file. It is markdown with YAML frontmatter, so the page
text travels as text.

```markdown
---
athenaeum_raise: 1
kind: raise                       # raise | supersede | retract | lapse | attest
raise_id: rs_k3v9t2hq8m4xw7pd5n6b
seq: 1
message_id: rm_7d2c9w4kq8t3h6vx5pnb
origin: st_f2n8rb5hjc3t
target: st_q7m2xk4vwz9d
proposer: {iss: "https://git.example.net", sub: "1203"}
via: proposer
sent_at: 2026-11-18T09:30:00Z
content_hash: "sha256:…"
page:
  type: principle
  name: Year-end PTO usage report
  description: Every employee files a PTO usage report by 31 December.
  aliases: []
  tags: [policy]
  access: open
---
Every employee files a PTO usage report by 31 December. …
```

| Kind | Carries | Meaning |
|---|---|---|
| `raise` | A payload, `seq: 1` | Propose a new copy. |
| `supersede` | A payload and a higher `seq`; or `successor: <raise_id>` and no payload | The raised content has a newer version (§7.1). |
| `retract` | A `reason` code, and no payload | The raise is withdrawn. |
| `lapse` | No payload | "I can no longer watch the source of this raise." The copy stands (§10). |
| `attest` | No `raise_id`; `sent_at` only | "This raiser is still watching." (§10.4) |

Rules of the format:

- **File name:** `raise/<raise_id>-<seq>-<message_id>.md`. An attest is
  `raise/attest-<message_id>.md`. Each name contains a random id, each file is
  written once and never edited. Those are the three requirements
  athenaeum#2624 §8.1 places on the proposal format, and they make the inbox
  append-only by construction.
- **`raise_id` and `message_id`** are a prefix and 20 lowercase base32
  characters (100 random bits), minted with `secrets`.
- **A supersede carries the whole new payload**, never a difference. A missed
  message therefore costs nothing: the target applies the highest `seq` it has
  and ignores anything lower than the one it applied.
- **A retract's `reason` is a code from a fixed list**: `withdrawn`,
  `source_removed`, `source_merged`, `superseded_at_source`,
  `no_longer_selected`, `erasure`. There is no free-text reason, because a
  sentence about the source can quote it. athenaeum#2624 §6.2 makes the same
  choice for the same reason.
- **Nothing in any message names the source page.** No uid, path, revision or
  source hash crosses.

The envelope has a `kind` key so that another note can add a kind. A fact
routed to a shared store at intake (athenaeum#2625) is not a raise: it has no
source page, no ledger and nothing to retract. Whether it uses this envelope
is that note's call. If it uses the inbox, the three requirements above apply
to it as well.

### 6.2 Where a message waits

The table of athenaeum#2623 §8 already answers this for a review. It holds for
every raise, whoever makes it:

| The raiser holds, on the target | The message is written to | Who can fetch it there |
|---|---|---|
| compile, and the target has a bench | The bench, under `raise/` | The compile role |
| compile, and the target has no bench (one compiling install, no stewards) | That install's own raw intake for the target store, at the place athenaeum#2625 defines | That install |
| propose only | The target's inbox, under `raise/`. The proposer's own inbox where proposers have separate ones (athenaeum#2623, operator ruling 1). | The compile role, and whoever may fetch that inbox |

In every row the message waits where only the target's compile role acts on
it, and in none of them is it a page. Neither location is indexed
(athenaeum#2623 §9).

The **acceptance ledger** is kept on the bench, or in the single compiling
install's own state when there is no bench. That is the placement
athenaeum#2623 §7 gives the decision queue, for the same reason: it is for the
compile role only.

**Order in a run.** A compiling install syncs the inbox and the bench before
it compiles (athenaeum#2624 §8.3), so it sees every waiting message. It
processes messages per `raise_id` in `seq` order, then compiles. It removes a
message file once the acceptance ledger records what was done with it. Two
compiling installs that process the same message record the same ledger row
and create the same page, and both merges are by rule (athenaeum#2624 §5.3,
§7.4).

**What the raiser learns.** Its message file disappearing from the inbox means
the compile role acted. It is not told whether the raise was accepted. A
raiser that also reads the target can compute the copy's uid (§4.4) and look.
A raiser that cannot read the target is told nothing about it, which is what
"propose does not imply read" requires.

---

## 7. Retract and supersede

### 7.1 Who emits them, and when

**The raiser emits both.** It is the only party that can: the mapping from
`raise_id` to source page exists in its raise ledger and nowhere else.

The check is part of the raiser's ordinary librarian run. No scheduler is
added. For a review, it runs with the review (§9.2). For each live ledger row
whose origin this install holds, the run reads the source page by uid through
the store seam and compares:

| What the raiser finds at the source | It emits |
|---|---|
| The page is there and its payload hash equals the hash last sent | Nothing. |
| The page is there and its payload hash differs | `supersede`, with the new payload and the next `seq`. |
| The page carries `superseded_by`, and the winner was itself raised to the same target | `supersede` with `successor: <the winner's raise_id>` and no payload. |
| The page carries `superseded_by`, and the winner was not raised | `retract`, reason `superseded_at_source`. |
| The page is a tombstone (`src/athenaeum/models.py:is_tombstone`) | `retract`, reason `source_merged`. |
| The page is gone, and the commit that removed it is not an erasure | `retract`, reason `source_removed`. |
| The page is gone, and the commit that removed it carries `Athenaeum-Erasure` (athenaeum#2624 §5.1) | `retract`, reason `erasure`. |
| A review re-reads the page and its criteria no longer select it | `retract`, reason `no_longer_selected`. |
| A person withdraws the raise by hand | `retract`, reason `withdrawn`. |

**The rule for a source that turned into a different page is: retract, do not
follow.** When a raised page is folded into another, or replaced by a page
that was never raised, the content now lives on a page nobody chose to raise.
Sending that page instead would raise text without a decision to raise it. The
new page is a fresh candidate through the normal route: a member raises it, or
the next review sees it in the difference since its cursor. This is the
narrowest reading of "supersession propagates", and the epic's invariant is to
go narrow when unsure.

The raiser updates its ledger row when it emits, and sends each message by the
same route as the original raise (§6.2).

### 7.2 How a message finds the copy

The target computes the copy's uid from its own `store_id` and the message's
`raise_id` (§4.4) and reads that page through the store seam. No index is
consulted and no page is scanned.

| What is at that uid | Action |
|---|---|
| The copy, with a matching `raised_from.raise_id` | Apply the message (§7.3, §7.4). |
| A tombstone | The copy was folded into another target page. Follow `src/athenaeum/models.py:tombstone_target`. The page it names is a dependent (§8); the tombstone is the copy. |
| Nothing, and the acceptance ledger says the raise was accepted | The target removed the copy itself. Record the message and stop. |
| Nothing, and the raise is still waiting | A retract withdraws the waiting raise: both files are removed. A supersede replaces it: the lower `seq` is dropped. |
| Nothing, and the raise was rejected or is unknown | Record the message and stop. |
| A page with a different `raise_id`, or none | Never touched. A decision is raised. |

### 7.3 Who may retract or supersede

A retract or a supersede is applied by rule only when it arrives **through the
same location as the raise it names, and names the same proposer**. The
acceptance ledger holds both. Anything else becomes a decision.

Where each proposer has their own inbox, the host decides who can push to that
location, so the rule is backed by the host. A shared inbox exists only where
every proposer can also read the store (athenaeum#2623, operator ruling 1),
and there the proposer named in a message is an unverified claim: one proposer
can send a retract that names another's raise. That is a question of verified
writer identity. It is athenaeum#2628's, and this note designs nothing for it.
The damage it can do is bounded by §7.4: a copy is removed by a commit that an
ordinary later commit restores.

### 7.4 Applying a message to the copy

The target first asks whether it has changed the copy since it accepted it:
does the copy's current `content_hash` equal `raised_from.content_hash`?

**Retract.**

| The copy is | Action |
|---|---|
| Untouched | Removed by rule, with the existing retirement mechanics (`src/athenaeum/retire_pages.py:apply_retirement`): a snapshot commit, then the removal, with pending merge proposals that name it withdrawn. The removal commit carries `Athenaeum-Raise: <raise_id> seq=<n> retract`. |
| Edited in the target | Not removed by rule. A decision asks the compile role to remove it or to keep it as the target's own page. Kept, its `watch` becomes `ended` and later messages for the `raise_id` are recorded and ignored. |
| Any, and the reason is `erasure` | Removed by rule, edited or not, and the removal commit also carries `Athenaeum-Erasure: <uid>`. Erasure always wins (athenaeum#2624 §5.1). |

The first two rows are §15 question 3.

**Supersede with a payload.**

| The copy is | Action |
|---|---|
| Untouched | Its content is replaced by the new payload, and `content_hash` and `seq` are updated. Whether a person is asked first follows the same `raise.accept` setting as the original raise (§4.4). |
| Edited in the target | A three-way page merge (athenaeum#2624 §5.1), with the last accepted version as the common ancestor, the target's current page as one side and the new payload as the other. The ancestor is read through the store seam by the blob id in the acceptance ledger. Units the merge settles by rule are applied. Any contested unit holds the page: the published version stays, and a decision is raised (athenaeum#2624 §6.4). |
| Any, and `access` or `audience` differ between the new payload and the copy | Held for a person. Neither a rule nor a model may widen or narrow who sees a page (athenaeum#2624 §5.1). |

**Supersede with a successor.** The copy is stamped
`superseded_by: <the successor copy's name slug>`, which is the single-store
representation (`src/athenaeum/models.py:parse_superseded_by`). It stays on
disk and recall demotes and marks it, as it does today. If the successor's own
raise is still waiting, the message waits with it. If the successor's raise
was rejected, the message is treated as a retract with reason
`superseded_at_source`.

While any message waits for a person, readers see the version already
published (operator ruling on athenaeum#2624, question 1).

---

## 8. Pages in the target that depend on the copy

The origin may withdraw or replace its own content. It may not rewrite the
target's other pages. A decision changes only the store that owns it
(athenaeum#2623 §7), and the target's pages are the target's.

So when a copy is removed or replaced, **the target finds the pages that
depend on it and lists them in one decision for its compile role.** One
decision per affected copy, of the existing `retraction` type, naming the
copy's uid, the reason code and each dependent page's uid. This is the
retraction cascade's rule applied across a store boundary: make the dependency
visible, and automate no judgement
(`src/athenaeum/retraction_cascade.py` module docstring).

| Dependency | How it is found | What happens |
|---|---|---|
| A target page **links to** the copy | A scan of the target's pages through the store seam for the four reference shapes (§2). No index is used. | Listed in the decision. Nothing is rewritten by rule. |
| The copy is the **parent or child** of a target page | The same scan; `parent:` and `related` rows are two of the four shapes. | Listed. The relation is removed only by the existing remover, when a person decides. |
| The copy was **folded into** a target page | The uid lookup of §7.2 lands on a tombstone, whose target is the page that absorbed it. | Listed. The merge is never undone by rule. S12 extends the cascade to treat a withdrawn copy as a retracted source of that merge, after pinning the form in which a fold records its source page. |
| A **pending merge proposal** names the copy | — | Withdrawn in the removal commit. `apply_retirement` already does this. |
| The copy was **raised again**, from the target to a third store | The target's own raise ledger has a row whose source is the copy. | Nothing special. On its next run the target-side raiser finds its source removed or changed and emits its own retract or supersede (§7.1). |

The last row is how a retraction travels through a chain of stores: one hop
per run, each hop driven by that store's own ledger. No install needs to know
the whole chain, and none could: a personal store's librarian cannot see what
a team store raised to an organisation store.

A dependent page that only quotes the copy's text, with no link, is not found.
That is true inside one store today as well.

Each reader's install drops a removed copy from its own index at its next
index build after it pulls. That is the install's own business, as in
athenaeum#2624 §10.

---

## 9. Push and pull

Both produce the messages of §6, written to the places of §6.2, and both keep
the ledger of §4.3. They differ in who decides what to raise.

### 9.1 Push: a member raises a page

A person, or an agent acting for them, names a page and a target:
`athenaeum store raise <uid> --to <store>`.

1. The install checks the grants of §4.1 against both declarations. A failure
   stops here with the reason.
2. It builds the payload (§4.2), mints a `raise_id`, and writes the ledger row
   and the message.
3. It syncs the location the message went to (athenaeum#2624 §8). For a
   propose-only raiser that is the inbox and nothing else.
4. From then on, each of the raiser's runs performs the check of §7.1.

The smallest case needs no shared location at all. One person raises from
their personal store to a team store that the same install compiles alone: the
message goes to that install's own raw intake for the team store, and the next
run accepts it.

The epic's worked example is a push. Dana Whitlock, HR manager at Acme Corp,
sets the rule that every employee files a PTO usage report at year end. It is
compiled into the HR store. Her install, which is in the HR store's compile
role and holds propose on the general wiki, raises that page. The general
wiki's compile role accepts it, and every employee's install receives the copy
at its next pull. When Dana later changes the deadline, her next run emits a
supersede.

### 9.2 Pull: a scheduled review raises

The review is the one athenaeum#2623 §8 defines: declared in the target,
granted read by the source with `purpose: review`, run inside an ordinary
librarian run when it is due. This note supplies what it writes.

1. The review reads the source's changes since its cursor and judges them
   against its criteria file. Its output is a **selection**: page uids. It
   writes no page text. The frame prompt, its file and its registry entry are
   athenaeum#2623 S11.
2. For each selected page, the run builds the payload, mints a `raise_id` and
   writes the ledger row beside the cursor (§4.3) and the message to the place
   §6.2 names.
3. In the same run, before selecting anything new, it performs the check of
   §7.1 for its existing ledger rows. A page the criteria once selected and no
   longer select is retracted with `no_longer_selected`.
4. The cursor moves only after the messages and ledger rows are written. If
   two installs moved it, the earlier revision wins (athenaeum#2624 §8.2), so
   a change can be reviewed twice and never skipped. A page selected twice is
   not raised twice: the ledger already has a live row for that source page
   and target.

A review that holds compile on the target and uses `accept: review: auto`
raises and accepts in one run, with no person in between. That is the
operator's example: the leadership librarian reads a team's store weekly and
escalates what the criteria select.

A raising librarian reads only what its grants allow. In a pull that is the
mounted source store, which the reviewing install holds only because the
source declared it a reader (athenaeum#2623 §8, §9). The review reads nothing
through the target, and the protocol gives it no other way to read the source.

---

## 10. When the origin can no longer be watched

A copy is kept up to date only while its raiser can read the source and can
reach the target. This section defines what happens when either stops.

### 10.1 The three states of a copy

`raised_from.watch` on the copy:

| State | Meaning |
|---|---|
| `active` | The raiser can still tell this store when the source changes. |
| `lapsed` | It no longer can. The copy stands as it was when last accepted. |
| `ended` | The raise is over: the target kept the page as its own after a retract (§7.4). |

A `lapsed` copy is not wrong. It is unmaintained, and a reader should be able
to tell the difference. Recall may show the state; it is frontmatter on a page
the reader already holds.

### 10.2 What makes a copy lapse

| Cause | Who notices | How |
|---|---|---|
| **The raiser lost read on the origin.** The source's declaration no longer grants it, so the install stops mounting the store (athenaeum#2623 S4). | The raiser | Its ledger rows for that origin can no longer be checked. It still reaches the target, so it sends one `lapse` message per row, by the usual route. |
| **A review was removed from the target's declaration**, or its source no longer declares the reader. | The target's compile role | The review no longer runs (athenaeum#2623 §8). Copies whose `via` names that review lapse. |
| **The proposer lost propose on the target.** | The target's compile role | The declaration no longer resolves the proposer recorded in the acceptance ledger. Their copies lapse. The host also refuses their pushes, so no further message can arrive. |
| **The origin store, or the raiser's install, is gone.** Nothing announces this. | The target's compile role, only if the store declared `max_silence` | §10.4. |
| **The network is down** for a while | Nobody | Nothing lapses. The raiser checks again on its next run. |

### 10.3 What happens to a lapsed copy

By rule: the copy stays, its `watch` becomes `lapsed`, and **one decision per
origin** is raised in the target's decision queue. It lists the copies, by
uid, and asks the compile role to keep them or withdraw them. No copy is
removed because a grant changed. That default is §15 question 4.

Two narrower rules cover messages caught in between:

- **A retract or a lapse that was already delivered is always applied**, even
  when its proposer has since lost propose. Neither can widen anything.
- **A raise or a supersede from a principal who no longer holds propose is not
  applied.** It becomes a decision. Failure narrows; it never widens
  (athenaeum#2623 §6).

**The limit that must be stated plainly.** When a raiser loses its grant on
the target, it can still see the source change and has no way to say so. Its
install reports the undelivered retract to its own owner, and that is all the
protocol can do. A retraction for privacy that cannot be delivered must be
handled out of band, by whoever administers the target.

### 10.4 Silence

A raiser that still exists and has nothing to say is indistinguishable, from
the target's side, from one that is gone. A store that cares may declare
`raise.max_silence` (§4.4). Then:

- each raiser sends one `attest` message per period by its usual route. One
  file covers all of that raiser's raises to that target;
- any message from a raiser counts as hearing from it;
- copies whose raiser has not been heard from within the limit lapse (§10.3).

The engine sets no limit. This follows the operator's ruling on the offline
limit of athenaeum#2623: each store may declare one, and a store that declares
none has copies that stay `active` until something else changes them.

### 10.5 What "a copy" means once it has lapsed

The ruling that a raise is a copy is what makes this section short. The target
holds the content. It does not need the origin to keep reading it, and losing
the origin loses only future updates. Under athenaeum#710's original shape, a
coordinate edit with the content left in place, an unreachable origin would
have meant an unreadable page.

---

## 11. The local index

**This design does not assume a plaintext local index, and does not read the
index at all.**

- The raiser finds its source page by uid through the store seam and computes
  the payload hash from the page it is handed.
- The target finds a copy by computing its uid (§4.4). It finds dependents by
  reading pages through the store seam (§8).
- Both ledgers are record files read through their own parsers. Messages are
  files in the inbox, the bench or raw intake. None of those locations is
  indexed (athenaeum#2623 §9).
- Nothing in the protocol asks an index who may see what. An install's index
  is built only from stores that install holds.

Requirements this note places on athenaeum#2627:

- **The inbox needs write without read.** A proposer must be able to seal a
  message that only the compile role can open. With a shared key, every
  proposer could open every other proposer's message, which the separate
  inboxes exist to prevent.
- **Ids in the clear.** The `Athenaeum-Raise` commit line holds a `raise_id`
  and a sequence number. Whether that is acceptable on an encrypted store is
  for athenaeum#2627, as it is for the commit lines of athenaeum#2624 §6.3.
  The line carries no text and no source id.
- **The payload hash is computed over content the seam decrypts**, so it is
  the same whether or not the store is encrypted at rest.

---

## 12. Boundaries, failure classes, migration and rollback

### 12.1 Components

| Component | Owns | Does not own |
|---|---|---|
| **Raise projection** (new, pure) | Page to payload, the payload hash, the derived uid. No I/O. | Choosing what to raise. |
| **Message codec** (new, pure) | Parse, validate and render the five message kinds. | Where a message is written. |
| **Raise ledger and acceptance ledger** (new) | Their record shapes and parsers, registered in `src/athenaeum/store.py:ARTIFACT_REGISTRY` so record merge has a rule for them. | Sync. athenaeum#2624 owns it. |
| **Raiser pass** (new, in the run) | The source check of §7.1 and emitting messages. | Reading the target. |
| **Acceptance pass** (new, in the run) | The checks of §4.4, applying messages, the dependent decision. | Judging whether content is true or fits. |
| **Store access** (athenaeum#2623) | Who holds which grant, the three locations, the review and its criteria. | Message contents. |
| **Sync** (athenaeum#2624) | Moving the inbox, the bench and the pages between installs; the page merge. | What a message means. |
| **Intake routing** (athenaeum#2625) | Which store a fact goes to, and where raw intake for a store waits. | Raises. |

Data flows one way: source page → projection → message → the target's waiting
place → acceptance pass → copy. Nothing flows back except through the raiser's
own reading of a target it happens to hold.

### 12.2 Repository split

All of it belongs in the athenaeum repository. It is engine mechanism with no
deployment content. A deployment supplies its stores, its declarations and its
review criteria, and those live in its stores. No new repository is needed. No
sibling service shares this construct.

### 12.3 Failure classes the design must survive

| Failure | Outcome |
|---|---|
| A crash after the ledger row is written and before the message is | The next run finds a `sent` row with no message at the waiting place and writes it again. The `raise_id` is the same, so the target sees one raise. |
| The same message is delivered twice, or processed by two compiling installs | The same ledger row and the same page, merged by rule (§6.2). |
| A message is lost | A supersede carries the whole payload, so the next one repairs it. A lost retract is sent again while the ledger row is not `retracted`. |
| Messages arrive out of order | Applied by `seq`. Lower than the applied `seq` is ignored. |
| A payload contains a link, or a hash that does not match | Refused by rule before acceptance (§4.4). The write guard is still behind it. |
| The derived uid is already taken | The proposal is held. Nothing is overwritten. |
| The target edited the copy, then a supersede arrives | Three-way merge; a contested unit is held for a person (§7.4). |
| The target edited the copy, then a retract arrives | A decision, unless the reason is erasure (§7.4). |
| The copy was folded into another target page | The tombstone is found by uid; the absorbing page is listed as a dependent (§8). |
| The raiser's own state is lost, and the ledger was kept there | Its copies can no longer be retracted by message. They lapse through `max_silence` if the store declared it. Otherwise they stay `active` and unmaintained. Keeping the ledger on a bench avoids this. |
| The raiser lost read on the origin, or propose on the target | Copies lapse; one decision per origin (§10). |
| The origin is erased | A retract with reason `erasure` removes the copy by rule and marks the commit. |
| A proposer in a shared inbox retracts another proposer's raise | Applied only if the message names the same proposer. A false claim is not detected. Bounded by an ordinary revert; owned by athenaeum#2628 (§7.3). |
| A hostile source page steers a review into selecting it | Contained to a proposal that waits for the target's compile role. Anything further is athenaeum#2628. |

**Erasure reaches only the working tree.** A retract for erasure removes the
copy. The copy's text remains in the target's git history, and the payload
remains in the history of the inbox or bench it travelled through. Erasure
that rewrites a shared store's history is not designed in athenaeum#2624 (its
§13), and it is not designed here. Sync stops safely when it sees a rewritten
remote; that is all that is promised.

### 12.4 Migration

Every step is additive. No existing page gains or loses a key. A store takes
part when its declaration grants someone propose or declares a review, and an
install raises only when asked or when a declared review is due. A page with
no `raised_from` block is untouched by everything in this note.

### 12.5 Rollback

- Stop raising: remove the review from the declaration, or stop running the
  command. Existing copies are ordinary pages.
- Detach one copy: a person decides to keep it, and its `watch` becomes
  `ended`.
- Undo an applied retract or supersede: an ordinary later commit restores the
  version named by the `Athenaeum-Raise` line. Nothing is force-pushed.
- The `raised_from` block is inert to code that does not know it.

### 12.6 What changes in athenaeum#710

athenaeum#710 locked "proposals travel up, orders come down", and said
promotion is "a coordinate edit, never a file move".

- **Kept:** a proposal lands in the receiving store's decision queue and is
  settled by that store. Nothing is consolidated into one store. athenaeum is
  not an identity store.
- **Amended, by the ruling that a raise is a copy:** a raise creates a new
  page in a different store. A coordinate edit cannot do the job, because the
  two stores are separate repositories held by different sets of people, and a
  reader of the target may hold no part of the origin.
- **Not designed here:** "orders come down". A member reads a store by
  mounting it under a read grant (athenaeum#2623). This note pushes nothing
  downward.

---

## 13. Principles applied and alternatives rejected

**Principles.**

- **The copy must stand alone.** A reader of the target needs nothing from the
  origin to use it.
- **Only the raiser knows the source.** The mapping lives on one side and
  never crosses.
- **Deterministic throughout.** No model call, no prompt, no trust score.
- **The origin changes only its own copy.** Everything else in the target is
  the target's decision.
- **Failure narrows.** A delivered retract is always honoured; a raise from a
  principal who lost the grant never is.
- **Reuse the seams.** The inbox, the bench, record merge, the page merge, the
  retirement mechanics, the tombstone, `superseded_by` and the `retraction`
  decision type are all existing or already designed.

**Rejected.**

1. **A link from the copy to the source page**, as a uid or a path. Rejected
   by the ruling, and by the store-boundary rule that already forbids it. A
   reader of the target would learn an identifier in a store they cannot read,
   and the link would dangle for them.
2. **Recording the hash of the source page.** It would let a target reader
   confirm guesses about text they cannot see. The hash of the accepted
   content gives the target what it needs: whether it has changed the copy.
3. **A model that writes the raised text**, as a summary of the source.
   Rejected: text with no source on either side cannot be checked by code, and
   it widens the surface athenaeum#2628 is reviewing. athenaeum#2624 rejects a
   model-written merge for the same reason.
4. **Raising part of a page**, by section. Rejected for now: it needs a second
   identity, the section, on both sides, and a rule for a heading that is
   renamed. Splitting the page achieves it with no new mechanism.
5. **The target polls the origin for changes.** Rejected: the target's compile
   role would need read on every origin, which propose-without-read rules out,
   and the copy would need a pointer to its source.
6. **A random uid for the copy, de-duplicated by `raise_id` when two compiling
   installs both accept.** Rejected: it adds a new rule to integrate, and
   every later message would need a scan of the target's pages to find the
   copy.
7. **A receipt telling the raiser whether its raise was accepted.** Rejected:
   it reports the state of a store to a principal who may not read it.
8. **A secret, sent with the raise, that a later retract must present.**
   Rejected for now: in a shared inbox every proposer can fetch the retract
   and read the secret, so it proves nothing there, and where inboxes are
   separate the host already answers the question. Proof of who sent a message
   is athenaeum#2628.
9. **Removing every copy when its raiser loses a grant.** Rejected as the
   default: the target accepted the content when it was properly offered, and
   a change of staffing would silently delete knowledge. It remains an option
   in §15 question 4.

---

## 14. Build slices

Each is one pull request. S1 to S5 depend on nothing outside this note and can
start now. A dependency written `athenaeum#2623 S7` is a slice of that note's
build table, and `athenaeum#2624 S13` of that one's.

| Slice | What it delivers | Needs |
|---|---|---|
| **S1** | The pure raise projection: page to payload by the table in §4.2, the payload hash, `raise_id` and `message_id` minting, and the derived uid. No I/O. | none |
| **S2** | The message codec: parse, validate and render `raise`, `supersede`, `retract`, `lapse` and `attest`, the file name rule and the fixed reason codes. Fails closed. | S1 |
| **S3** | The `raised_from` block: parse and validate it, leave it out of `verdicts.py:content_hash`, accept `raise` as a source type, and a lint for a page whose `raised_from` is malformed. No behaviour change. | none |
| **S4** | The raise ledger: record shape, parser, registry entry, kept in the install's own state. | S1 |
| **S5** | The acceptance ledger: record shape, parser, registry entry, kept in the install's own state. | S2 |
| **S6** | Push, on one install: `athenaeum store raise`, the grant checks of §4.1 on both declarations, the ledger row, and the message written to the install's own raw intake for the target. | S1, S2, S4; athenaeum#2623 S1 and S2; athenaeum#2625 for where raw intake for a store waits |
| **S7** | Acceptance, asking every time: the rule checks of §4.4, the `raise` decision kind, creating the copy at its derived uid with `raised_from`, the `Athenaeum-Raise` commit line, reject, and the taken-uid hold. | S3, S5; athenaeum#2623 S7 (the per-store decision queue) |
| **S8** | The `raise.accept` block in the declaration and acceptance without asking, honouring `sync.always_ask`. | S7; athenaeum#2623 S1; §15 question 2 |
| **S9** | The raiser pass: for each live ledger row, read the source through the store seam, classify it by the table in §7.1, and emit `supersede` or `retract`. Erasure is left to S10. | S2, S4, S6 |
| **S10** | Erasure: recognise a source removed by a commit carrying `Athenaeum-Erasure`, emit `retract` with that reason, and mark the target's removal commit. | S9, S11; athenaeum#2624 S5a (history read) and S16 (the erasure mark) |
| **S11** | Apply a retract in the target: find the copy by derived uid, follow a tombstone, remove an untouched copy with the retirement mechanics, raise a decision for an edited one, set `watch: ended` when kept. | S7; §15 question 3 |
| **S12** | Dependents: the one `retraction` decision per affected copy, listing pages found by the four reference shapes and by the tombstone, and the cascade extension for a merge that absorbed a copy. | S11 |
| **S13** | Apply a supersede in the target: replace an untouched copy; three-way merge for an edited one, with contested units held; the `access` and `audience` hold; the successor form. | S7; athenaeum#2624 S3 (the page merge) and S5a (read a file at a revision) |
| **S14** | Shared locations: write messages to the target's inbox or bench, keep both ledgers on a bench where one exists, remove a message once recorded, and apply the same-location rule of §7.3. | S6, S7; athenaeum#2623 S8 (mounting the bench and inbox); athenaeum#2624 S4 (record merge) and S13 (bench and inbox sync) |
| **S15** | Pull: a review's selection becomes raises, the ledger sits beside the cursor, the raiser pass runs with the review, and `no_longer_selected` is emitted. | S9; athenaeum#2623 S10 and S11; S14 for a bench or an inbox |
| **S16** | Lapse: emit and apply the `lapse` message, set `watch: lapsed` for the causes of §10.2, raise one decision per origin, always apply a delivered retract or lapse, and never apply a raise or supersede from a principal without propose. | S11, S13; athenaeum#2623 S2 and S4; §15 question 4 |
| **S17** | `raise.max_silence` and the `attest` message. | S14, S16 |
| **S18** | A three-store test harness that proves a retract travels two hops, one per run. | S9, S11; athenaeum#2624 S6a (the two-clone harness) |
| **S19** | A guide: raising between stores, and what a reader sees on a copy. Docs only. | S14, S15 |

**What athenaeum#2623 needs from here.** Its S11 (the review run) writes
proposals in this format. It needs S1, S2 and S4, and S14 for a target with a
bench or an inbox.

S6, S7, S9, S11 and S13 together give one install a working raise, retract and
supersede between two stores it holds, with no shared location and nothing
from athenaeum#2624 except the pure page merge.

---

## 15. Open questions for the operator

Only calls that depend on what the operator values. Each has a recommended
answer, listed first, and the design above is written to that answer.

### 1. Who is named as the proposer on a published copy?

Every copy says who proposed it, and everyone who can read the store sees
that. Some stores let a person send in a fact without being able to read the
store, and give each such person a private inbox so that other proposers
cannot see who sent what.

- **The name the store's own access list uses (recommended).** If the list
  names the person, the copy names the person. If the list names a group, the
  copy names the group, and the person's identity is kept where only the
  people who compile the store can see it. Nothing is shown that readers could
  not already look up. Cost: on a store that grants by group, a reader cannot
  tell which member raised a fact.
- **Always the individual.** The most accountable. Cost: it tells every reader
  who reported something, which is what the private inboxes were meant to
  prevent.
- **Never anyone; show only the origin store.** The most private. Cost: the
  ruling asked for the proposer to be recorded, and readers lose any sense of
  who stands behind a fact.

### 2. When a raise arrives, does the store accept it without asking a person?

A raise that passes the automatic checks (well formed, from someone allowed to
propose, no links back to the origin) still has to be accepted before it
becomes a page.

- **It depends on how it arrived (recommended).** A raise made by a scheduled
  review that the store itself set up is accepted automatically, because the
  store's own criteria already chose it. A raise sent by a member is shown to
  a person. Page types the store marked "always ask" are always shown. Each
  store can change either setting. Cost: a badly written review criteria file
  publishes pages with nobody looking.
- **Always ask a person.** Nothing is published unseen. Cost: a weekly review
  produces a queue of approvals, which is the load scheduled raising was meant
  to remove.
- **Never ask.** The least work. Cost: anyone allowed to propose can publish
  to every reader of the store, which removes the difference between proposing
  and compiling.

### 3. When the origin withdraws a fact, does the copy disappear by itself?

The store that a fact came from has retired it, merged it away or replaced it.
The copy in the receiving store may be exactly as it arrived, or people there
may have edited it since.

- **Untouched copies are removed automatically; edited copies ask
  (recommended).** A copy nobody changed is removed, and a person is told
  which other pages referred to it. A copy that was edited is kept until a
  person decides to remove it or keep it as the store's own page. Removal for
  privacy erasure is always automatic. Cost: a withdrawn fact can stay visible
  on an edited copy until someone answers.
- **Always ask.** Nothing leaves a store without a person agreeing. Cost:
  withdrawn facts stay published until someone gets to them, and the queue
  grows with every correction upstream.
- **Always remove.** Withdrawal is immediate everywhere. Cost: work that
  people in the receiving store added to the copy is removed with it, and is
  recoverable only from history.

### 4. When a copy can no longer be kept up to date, what happens to it?

A copy stays current only while the librarian that raised it can still read
the original and still reach the receiving store. That stops when the person
leaves a team, a review is switched off, or the original store goes away.

- **Keep it, mark it as no longer watched, and ask once (recommended).** The
  copy stays readable and carries a visible mark. One question per origin
  store asks the people who compile the receiving store whether to keep or
  withdraw the affected copies. Cost: an out-of-date fact can remain, though
  marked.
- **Withdraw it automatically.** Nothing unmaintained stays published. Cost: a
  change of staffing silently deletes knowledge that was properly shared, and
  that was correct when it was shared.
- **Keep it and say nothing.** The least machinery. Cost: readers cannot tell
  a maintained copy from an abandoned one.

### 5. Who may raise a page out of a store?

Anyone who can read a store can copy its text by hand, and no software stops
that. The question is whether athenaeum will do it for them, with one command,
into a store that more people read.

- **The people who compile the store, and reviewers the store has declared
  (recommended).** A team's own librarians can raise its pages, and so can a
  reviewing librarian that the team's access list names as a reviewer. An
  ordinary reader cannot. Cost: a reader who spots something worth sharing has
  to ask a compiler to raise it.
- **Any reader.** The most fluid. Cost: one reader of a private store can
  publish its pages to a wider audience, and the store's owners learn about it
  only if they also read the destination.
- **Only the people who compile the store; no reviewers.** The tightest. Cost:
  it removes scheduled review by another store's librarian, which the operator
  ruled in.
