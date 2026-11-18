<!-- SPDX-License-Identifier: Apache-2.0 -->

# Federation: the raise and retract protocol between stores

**Status:** DESIGN, not built (Vitruvius Specify, 2026-11-18). The design note
athenaeum#2626 asks for, a child of the federation design epic athenaeum#2631.
Nothing here is running code. The note ends in build slices (§14) and in the
operator's rulings on its six questions (§15). None is still open.

Code anchors are `path:symbol` on `develop` at `759e3b64`.

**Builds on, and does not redesign:**

- **The store access model**, [Federation: store access](federation-store-access.md)
  (athenaeum#2623): grants, the inbox, the bench, scheduled review and the
  review criteria file. Its terms are used here unchanged (§3).
- **Multi-writer sync**, [Federation: multi-writer sync](federation-multi-writer-sync.md)
  (athenaeum#2624): the sync sequence, the append-only inbox, record merge,
  uids, the same uid in two stores and the erasure mark.

**Does not decide:**

- **Which store a fact goes to at intake**, a store's `intake` rules and the
  proposer's notice (athenaeum#2625, the intake routing note, written in
  parallel). This note says where it depends on that one and specifies nothing
  of it. It does supply the message that note's propose path sends (§6.3).
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
2. **The copy carries a provenance record and no link.** The part on the copy
   names the origin store by its opaque `store_id`, the proposer as the
   target's own declaration names them, a hash of the copied content, and the
   day. None of those tells a reader of the target anything they could not
   already see. The proposer's own identity and the exact time are the other
   part of the record, kept where only the target's compile role can fetch
   them (§5).
3. **The source page's uid is held in one place: the raise ledger.** The
   raiser keeps it, and it maps an opaque `raise_id` to the source page. No
   message and no page in the target carries the source's uid, path or
   revision. For a raise the origin decided, the ledger is on the origin's
   bench or in the raiser's own state. For a review it sits beside the review
   cursor, which can be the target's bench; there the target's compile role
   can fetch a row that names an origin page by `store_id` and uid, and
   readers of the target cannot (§4.3).
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
| **Projection** | The deterministic function of §4.2 that reduces a page to what a raise carries. It is applied to the source page, and again to the copy. |
| **Payload** | The content that travels: the projection of one page of the origin. |
| **Payload hash** | The hash of a projection (§4.2). |
| **Copy** | The page the target creates from an accepted payload. |
| **`raise_id`** | An opaque random id for one raise, minted by the raiser. It is the only name the two sides share. |
| **Raise ledger** | The raiser's record of what it raised: `raise_id`, source page, target, and the hash it last sent. |
| **Acceptance ledger** | The target compile role's record of what arrived and what it did with it. |
| **Message** | One file in the format of §6: a raise, a propose-fact, a supersede, a retract, a lapse or an attest. |

"Personal store" and "item" are used as athenaeum#2625 defines them: the
install's own default surface, whose only reader is its owner, and one fact
inside a raw note.

"Raise" does not imply a direction. Access is storage RBAC and not a hierarchy
(athenaeum#2631), so a raise goes from any store to any store where the grants
of §4.1 hold.

---

## 4. The raise

### 4.1 Which grants a raise needs

| On | The raiser's principal needs | Why |
|---|---|---|
| The **origin**, for a raise a member makes by hand | Membership of its compile role | Operator ruling 5 (§15). |
| The **origin**, for a raise made by a review | A read grant declared with `purpose: review`, and the review must be one the **target** declares with `source` equal to this origin | Operator ruling 5 (§15). |
| The **target** | Propose or compile | The raise is a proposal to the target (athenaeum#2623 §4). |

Read alone lets a person copy text out of a store by hand, and nothing in
athenaeum can stop that. The question is what the engine will do for them. The
ruling is that the engine raises out of a store only for its compile role, and
for a reviewer the store itself declared.

**The reviewer exemption is narrow.** It covers only the output of a review
that the target's declaration lists, against this origin. A principal who
holds a `purpose: review` grant cannot use it to raise a page by hand: a grant
that a store's members were told is for review does not become a way to
publish their pages one at a time.

**This needs one line in the access note.** athenaeum#2623 §8 says
`purpose: review` "changes nothing about what the grant allows", and its S10
checks only that a read grant exists. Left as it is, a source that grants a
reviewer plain read would have its review run, select pages and then raise
nothing. athenaeum#2772 carries the amendment: a review's output is raised
only when the source's grant says `purpose: review`, and S10 checks it. The
host still hands over the same bytes either way. The rule narrows what
athenaeum does with them, and every athenaeum-side rule only narrows
(athenaeum#2623 §1).

**A store with no declaration** has an implicit one: the install that writes
it is its sole reader and its whole compile role. The personal store is the
usual case. Its owner's install may therefore raise from it, which is what the
smallest case in §9.1 and the propose path of athenaeum#2625 both do. An
install that mounts an undeclared store `read_only` is a reader of it and may
not raise from it; that is the local flag deciding, as athenaeum#2623 §5
says it does. An undeclared store has no `store_id`, so the install mints one
the first time it raises from the store and keeps it in its own state. A
declaration committed later carries that same id.

**A raiser needs no read on the target.** Propose does not imply read
(athenaeum#2623 §4), and the protocol never asks the raiser to look at the
target's pages. A propose-only raiser therefore cannot read the target's
declaration either. It learns the two things it needs, the target's `store_id`
and its `intake` rules, from the proposer's notices athenaeum#2625 defines:
append-only files `_store/propose/notice-<n>.yaml` in the inbox, written by
the compile role, of which the newest wins. A propose-only install with no
notice it can parse sends nothing to that store. Whether it really holds
propose is the host's answer when it pushes, and the compile role's check when
it accepts (§4.4).

### 4.2 The unit and the projection

**The unit of a raise is one whole page.** A group that wants to raise half a
page splits the page, in the same way a group that wants to share half a store
splits the store (athenaeum#2623 §3).

The payload is a deterministic **projection** of the source page. No model
takes part, and no text is written that is not already on the source page.

| Part of the page | In the projection |
|---|---|
| `type`, `name`, `description`, `aliases`, `tags` | Copied. |
| `access`, `audience` | Copied. Sync never widens or narrows them (athenaeum#2624 §5.1), and neither does a raise. |
| Body text | Copied, after link handling (below). |
| `uid` | Dropped. The target derives its own (§4.4). |
| `sources` | Dropped. They name raw files and sessions inside the origin. |
| `parent`, and every `related` row | Dropped. They name origin pages by uid. |
| `created`, `updated`, the `raised_from` block, and every other key | Dropped. The copy's dates are the target's. |
| A `[[wikilink]]` or markdown link to another page | Replaced by its label as plain text. |

Dropping the links is what "no link back down" means in practice. The existing
write guard (`src/athenaeum/store_links.py:guard_entity_store_links`) stays in
place behind it as defence in depth. The target's own compile may later link
the copy to target pages; that is ordinary work inside one store.

**The payload hash.** The projection is rendered as a page that holds only the
kept keys and the body, and the payload hash is
`src/athenaeum/verdicts.py:content_hash` over that rendering.

The hash is never taken over a whole page. `content_hash` leaves out only the
system metadata keys, so it includes `uid`, `sources`, `created` and
`updated`. A copy has all four and a payload has none, so a hash of the whole
copy could never equal the hash that travelled.

**The same projection is applied to the copy.** To ask whether the target has
changed a copy, the target projects the copy as it stands now and compares the
payload hash with the one recorded at acceptance (§7.4). The projection drops
`uid`, `sources`, `created`, `updated` and `raised_from` on that side as well,
so a copy nobody has edited compares equal. S1 pins this with a test: a
payload, accepted into a copy and projected again, gives the same hash.

One consequence is deliberate. Links and `related` rows are not part of the
projection, so a target that links the copy to its own pages has not edited it
in this sense.

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

- **Content:** the payload's body, unchanged, and its keys. The body is
  accepted whole or not at all. The compile role may adjust two keys as it
  accepts, the `type` and the `tags`, because a type vocabulary belongs to a
  store (athenaeum#2625 §4.4). The adjustment is recorded in the acceptance
  ledger and applied again to every later payload for the same `raise_id`.
  Any other editing, merging with an existing target page and de-duplication
  happen afterwards, through the target's ordinary compile and its ordinary
  merge proposals, which never cross a store.
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
- **Provenance:** the `raised_from` block of §5. Its `content_hash` is the
  payload hash of the copy as accepted, that is, after any adjustment of type
  and tags.
- **Record:** the commit that creates the copy carries one line,
  `Athenaeum-Raise: <raise_id> seq=<n> accept`, with ids only. The acceptance
  ledger gets a row with the same ids, the proposer's own identity, the
  location the message arrived through, the exact time, the payload hash as it
  arrived, any adjustment of type and tags, and the blob id of the accepted
  version.

**Checks made by rule before anyone is asked.** The message parses; its
`target` is this store; the proposer holds propose or compile in the target's
declaration at the time the compile role acts; the payload has no link, no
`related` row and no `parent`; the page parses and has `type` and `name`; the
`content_hash` matches the payload; the `raise_id` is not already rejected. A
message that fails is not applied. It is recorded with a reason code and, for
anything other than a duplicate, raised as a decision.

**The store's `intake` rules are applied here too.** athenaeum#2625 §7 gives a
store ordered accept and refuse rules over type and tags, and asks that they
apply to a raised copy, so that a store has one statement of what it takes
whichever way a fact arrives. The check is that note's pure accept function,
run on the payload's type and tags:

- under `auto` (below), a payload the rules refuse is rejected with reason
  `store_refuses`. Nothing is adjusted without a person;
- under `ask`, the person may adjust the type and tags to something the rules
  accept, or reject.

The compiling member's own relevance policy is never applied to a waiting
message (athenaeum#2625 §4.5).

**Whether a person is asked.** The store declares it, once, in its
declaration (athenaeum#2623 §5):

```yaml
raise:
  accept:
    review: auto        # raises made by a review this store declares
    proposer: ask       # raises sent by a member
  always_ask: [person]  # page types that are always shown to a person
  max_silence: 60d      # optional; see §10.4
```

`auto` means a raise that passes the checks above is accepted by the compiling
install without a decision. `ask` means it becomes a decision of a new kind,
`raise`, in the store's decision queue, showing the payload. A page type
listed in `raise.always_ask` is always asked, whatever the route. The defaults
shown are operator ruling 2 (§15).

`raise.always_ask` is its own list. It does not reuse `sync.always_ask`: the
operator's ruling on that list (athenaeum#2624, question 2) is about settling
conflicts between writers, and a store may want different types watched for
the two purposes. The two lists have the same shape and are read by the same
code.

A store with no `raise:` block asks for everything.

**`raise:` is a new top-level key of the declaration.** New top-level keys
arrive only with a higher `version`; an install that does not support that
version treats the store as read-only. That is what the declaration parser
already does with a version it does not know (athenaeum#2772), so an older
install can never accept a raise under rules it cannot read.

---

## 5. The provenance record

**The provenance record has two parts**, split by who may see each.

| Part | Where | Who can fetch it | Holds |
|---|---|---|---|
| **On the copy** | The `raised_from` block in the copy's frontmatter | Every reader of the target | The origin `store_id`, the proposer as the target's declaration names them, the payload hash, the day |
| **In the acceptance ledger** | The bench, or the single compiling install's own state (§6.2) | The target's compile role only | The proposer's own identity, the exact time, the location the message arrived through, any adjustment of type and tags |

Together they record the origin store id, the proposer's identity, the content
hash and the time, which is what athenaeum#2626 asks for. The split is what
lets the published part disclose nothing new.

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

`raised_from` is system metadata. It is left out of the projection (§4.2), and
S3 also leaves it out of `src/athenaeum/verdicts.py:content_hash`, so stamping
or updating it changes neither hash. The `sources` entry uses the existing typed
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
| `content_hash` | The payload hash of the copy as the target accepted it | A reader can compute it themselves: it is the hash of the projection (§4.2) of the version of this page that the accepting commit created, and that version is in the target's own history. It is **not** a hash of the source page. |
| `raised_on` | The day on the proposal | It is a day, not a time, and it is the proposal's day, not a date from the source page. |
| `seq`, `watch` | Protocol state | They describe the copy. |

Three choices in that table are deliberate, and each closes a leak.

- **The proposer is recorded as declared, and not always as an individual.**
  A store may give propose to a group whose members are held in an external
  directory, and athenaeum#2623 gives each proposer their own inbox when a
  proposer cannot read the store. Printing the individual on the published
  page would tell every reader what the separate inboxes were built to keep
  apart. The individual's identity is kept in the acceptance ledger, which
  only the compile role can fetch. This is operator ruling 1 (§15).
- **The hash is of the accepted content, never of the source.** A hash of a
  source page would let a reader test guesses about text they cannot read. The
  hash on the copy covers only what the copy held when it was created. When
  the compile role adjusted the type or tags, the hash of the payload as it
  arrived differs, and it stays in the acceptance ledger.
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
kind: raise                       # raise | propose-fact | supersede | retract | lapse | attest
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
| `raise` | A payload, `seq: 1` | Propose a new copy of a whole page. |
| `propose-fact` | One item, with a suggested type and tags, `seq: 1` | Propose a single routed fact (§6.3). |
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

The envelope has a `kind` key so that a later note can add a kind.

### 6.2 Where a message waits

The table of athenaeum#2623 §8 already answers this for a review. It holds for
every raise, whoever makes it:

| The raiser holds, on the target | The message is written to | Who can fetch it there |
|---|---|---|
| compile, and the target has a bench | The bench, under `raise/` | The compile role |
| compile, and the target has no bench (one compiling install, no stewards) | That install's own state, in the local waiting place athenaeum#2625 defines, `raw/_stores/<store_id>/` | That install |
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

### 6.3 A single routed fact: `propose-fact`

athenaeum#2625 routes each new fact to a store. When the install holds only
propose on the destination, or is a steward of it, intake hands this note
three things: the item, its suggested type and tags, and the destination
`store_id`. The message that carries them is `propose-fact`.

It is not a whole-page raise, and must not be sent as one. Under the
operator's ruling on athenaeum#2625 the sender keeps the fact in their
personal store as well, where it may be one line on a page that holds other
personal facts. Raising that page would send all of them.

```markdown
---
athenaeum_raise: 1
kind: propose-fact
raise_id: rs_p8d3m6wq2t9hx4vk7c5n
seq: 1
message_id: rm_4h8t2k9wq6d3x7vp5cnb
origin: st_b6t3kd9wq2mx
target: st_q7m2xk4vwz9d
proposer: {iss: "https://git.example.net", sub: "1318"}
via: intake
sent_at: 2026-11-18T10:05:00Z
content_hash: "sha256:…"
fact:
  type: person            # a suggestion; the compile role fixes the type
  tags: [hr]
---
Sam Ortega moves to the Leeds office on 1 December.
```

| Aspect | A `raise` | A `propose-fact` |
|---|---|---|
| Carries | The projection of a whole page | One item's text, and a suggested type and tags |
| Type | The page's, adjustable on acceptance (§4.4) | A suggestion. The compile role fixes it (athenaeum#2625 §4.4). |
| What the target makes of it | A new page at a derived uid, with `raised_from` | Whatever its ordinary compile makes: a new page, or an addition to an existing page of that store |
| Provenance on the result | The `raised_from` block | A `raise:<raise_id>` entry in `sources`. The page is the target's own page, not a copy. |
| Acceptance ledger | The copy's uid follows from the `raise_id` | The row records the uids of the pages the compile wrote for it |
| Raise ledger row | The source page's uid and the payload hash | The uid of the personal page that holds the sender's copy, and the hash of the item sent |
| `intake` rules | Applied at acceptance (§4.4) | Applied by the sender before it leaves and by the compile role when it arrives (athenaeum#2625 §7) |

**It has a `raise_id` and a ledger row so that the sender can withdraw it.**
The sender's install writes the row to its raise ledger in its own state, and
a later `retract` names the `raise_id` like any other.

| A retract for a `propose-fact` arrives when | Action |
|---|---|
| The fact is still waiting | Both files are removed. Nothing was compiled. |
| The fact has been compiled | Nothing is removed by rule. The fact is now part of the target's own pages, possibly one sentence among others. One `retraction` decision names the pages the acceptance ledger recorded and the reason code. |

**No supersede is emitted by rule for a fact.** The raiser pass (§7.1)
compares whole pages, and a single fact has no page of its own to compare. A
correction is a retract followed by a new `propose-fact`. The raiser pass does
one thing for a fact row: when the personal page that holds the sender's copy
is removed or erased, it emits a retract with the matching reason.

**A retract for privacy erasure of a compiled fact is not automatic.** No rule
can tell which words on a page came from the fact. The decision is marked
urgent and placed at the head of the store's decision queue, and a person
removes the sentence. That is operator ruling 6 (§15).

The file name rule, the three requirements of athenaeum#2624 §8.1 and the
waiting places of §6.2 apply unchanged. Nothing in the message is read from
the sender's index.

---

## 7. Retract and supersede

### 7.1 Who emits them, and when

**The raiser emits both.** It is the only party that can: the mapping from
`raise_id` to source page exists in the raise ledger and nowhere else (§4.3).

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

The target first asks whether it has changed the copy since it accepted it.
It projects the copy as it stands now (§4.2) and compares that payload hash
with `raised_from.content_hash`. Equal means untouched. The comparison is
never made with a hash of the whole page, which would differ from the moment
of acceptance.

**Retract.**

| The copy is | Action |
|---|---|
| Untouched | Removed by rule, with the existing retirement mechanics (`src/athenaeum/retire_pages.py:apply_retirement`): a snapshot commit, then the removal, with pending merge proposals that name it withdrawn. The removal commit carries `Athenaeum-Raise: <raise_id> seq=<n> retract`. |
| Edited in the target | Not removed by rule. A decision asks the compile role to remove it or to keep it as the target's own page. Kept, its `watch` becomes `ended` and later messages for the `raise_id` are recorded and ignored. |
| Any, and the reason is `erasure` | Removed by rule, edited or not, and the removal commit also carries `Athenaeum-Erasure: <uid>`. Erasure always wins (athenaeum#2624 §5.1). |

These rows are operator ruling 3 (§15). They apply to a copy. A compiled
`propose-fact` is §6.3.

**Supersede with a payload.**

| The copy is | Action |
|---|---|
| Untouched | Its content is replaced by the new payload, with the recorded adjustment of type and tags applied again (§4.4), and `content_hash` and `seq` are updated. Whether a person is asked first follows the same `raise.accept` setting as the original raise (§4.4). |
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

1. The install checks the grants of §4.1. For the origin it reads the
   declaration, or uses the implicit one of an undeclared store. For the
   target it reads the declaration when it holds the pages, and the newest
   proposer's notice when it only proposes. It also applies the target's
   `intake` rules to the payload, as a courtesy that keeps a refused page from
   leaving. A failure stops here with the reason.
2. It builds the payload (§4.2), mints a `raise_id`, and writes the ledger row
   and the message.
3. It syncs the location the message went to (athenaeum#2624 §8). For a
   propose-only raiser that is the inbox and nothing else.
4. From then on, each of the raiser's runs performs the check of §7.1.

The smallest case needs no shared location at all. One person raises from
their personal store to a team store that the same install compiles alone: the
message waits in that install's own state, and the next run accepts it. The
personal store has no declaration, and §4.1 says what that means: its owner's
install is its compile role.

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
   writes no page text. Everything up to and including the selection is
   athenaeum#2623 S11: the model call, the frame prompt, its file and its
   registry entry. Everything after it is this note's S15: turning the
   selection into messages and ledger rows.
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
| **The raiser lost read on the origin.** | The raiser | It cannot learn this from the declaration: once revoked it can no longer pull the origin, so it never sees the declaration that removed it, and a failed pull keeps the last good copy (athenaeum#2623 §9). The trigger is therefore one of two events on the raiser's side. Either the host **refuses** the fetch of the origin, as distinct from being unreachable, or the origin is unmounted because its declared `max_offline` passed (athenaeum#2623 S6). On either, the raiser stops checking its stale copy. It still reaches the target, so it sends one `lapse` message per ledger row, by the usual route. |
| **A review was removed from the target's declaration**, or its source no longer declares the reader. | The target's compile role | The review no longer runs (athenaeum#2623 §8). Copies whose `via` names that review lapse. |
| **The proposer lost propose on the target.** | The target's compile role | The declaration no longer resolves the proposer recorded in the acceptance ledger. Their copies lapse. The host also refuses their pushes, so no further message can arrive. |
| **The origin store, or the raiser's install, is gone.** Nothing announces this. | The target's compile role, only if the store declared `max_silence` | §10.4. |
| **The network is down** for a while | Nobody | Nothing lapses. The raiser checks again on its next run. |

### 10.3 What happens to a lapsed copy

By rule: the copy stays, its `watch` becomes `lapsed`, and **one decision per
origin** is raised in the target's decision queue. It lists the copies, by
uid, and asks the compile role to keep them or withdraw them. No copy is
removed because a grant changed. That is operator ruling 4 (§15).

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
| **Message codec** (new, pure) | Parse, validate and render the six message kinds. | Where a message is written. |
| **Raise ledger and acceptance ledger** (new) | Their record shapes and parsers, registered in `src/athenaeum/store.py:ARTIFACT_REGISTRY` so record merge has a rule for them. | Sync. athenaeum#2624 owns it. |
| **Raiser pass** (new, in the run) | The source check of §7.1 and emitting messages. | Reading the target. |
| **Acceptance pass** (new, in the run) | The checks of §4.4, applying messages, the dependent decision. | Judging whether content is true or fits. |
| **Store access** (athenaeum#2623) | Who holds which grant, the three locations, the review and its criteria. | Message contents. |
| **Sync** (athenaeum#2624) | Moving the inbox, the bench and the pages between installs; the page merge. | What a message means. |
| **Intake routing** (athenaeum#2625) | Which store a fact goes to, a store's `intake` rules and their accept check, the proposer's notices, and compiling a received `propose-fact`. | The message format, the ledgers and retraction. |

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
| The raiser lost read on the origin, or propose on the target | Copies lapse; one decision per origin (§10). The raiser notices a lost read from a refused fetch or the `max_offline` unmount, not from a declaration it can no longer pull. |
| An untouched copy is compared with a hash of the whole page | Cannot happen: the comparison is defined over the projection on both sides (§4.2), and S1 pins it with a test. |
| The target's `intake` rules refuse a payload | Rejected with `store_refuses` under `auto`; a person may adjust type and tags under `ask` (§4.4). |
| A propose-only raiser has no proposer's notice, or cannot parse it | It sends nothing to that store (§4.1). |
| An install meets a declaration with a `version` it does not support | It treats the store as read-only and accepts no raise (§4.4). |
| A retract arrives for a fact already compiled into the target's pages | A decision naming the pages; nothing is removed by rule (§6.3). |
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
   the operator considered and did not choose (§15, ruling 4).

---

## 14. Build slices

Each is one pull request. S1 to S5 depend on nothing outside this note and can
start now. A dependency written `athenaeum#2623 S7` is a slice of that note's
build table, and likewise `athenaeum#2624 S13` and `athenaeum#2625 S5`.

| Slice | What it delivers | Needs |
|---|---|---|
| **S1** | The pure raise projection: page to projection by the table in §4.2, its rendering, the payload hash, `raise_id` and `message_id` minting, and the derived uid. No I/O. Includes the test that a payload accepted into a copy and projected again gives the same hash. | none |
| **S2** | The message codec: parse, validate and render `raise`, `propose-fact`, `supersede`, `retract`, `lapse` and `attest`, the file name rule and the fixed reason codes. Fails closed. | S1 |
| **S3** | The `raised_from` block: parse and validate it, leave it out of `verdicts.py:content_hash`, accept `raise` as a source type, and a lint for a page whose `raised_from` is malformed. No behaviour change. | none |
| **S4** | The raise ledger: record shape, parser, registry entry, kept in the install's own state. | S1 |
| **S5** | The acceptance ledger: record shape (including an adjustment of type and tags, and the page uids written for a fact), parser, registry entry, kept in the install's own state. | S2 |
| **S6** | Push, on one install: `athenaeum store raise`; the grant checks of §4.1, including compile role on the origin, the implicit declaration of an undeclared store and minting its `store_id`; the courtesy `intake` check; the ledger row; and the message written to the install's own state. | S1, S2, S4; athenaeum#2623 S1 and S2; athenaeum#2625 S5 (the `intake` accept check), and S15 (the local waiting place `raw/_stores/<store_id>/`) |
| **S7a** | Acceptance, asking every time: the rule checks of §4.4, the store's `intake` rules, the `raise` decision kind, the adjustment of type and tags and its ledger record, creating the copy at its derived uid with `raised_from`, and the `Athenaeum-Raise` commit line. | S3, S5; athenaeum#2623 S7 (the per-store decision queue); athenaeum#2625 S5 |
| **S7b** | Rejection and holds: reject with a reason code, drop later messages for a rejected `raise_id`, the taken-uid hold, and the decision for a message that fails a check. | S7a |
| **S8** | The `raise:` block in the declaration, with the higher declaration `version` it arrives under: `raise.accept`, `raise.always_ask`, and acceptance without asking. | S7a; athenaeum#2623 S1; athenaeum#2624 S10 (the always-ask list and its type matching, shared by both lists); athenaeum#2772 (an unsupported `version` leaves the store read-only); ruling 2 |
| **S9** | The raiser pass: for each live ledger row, read the source through the store seam, classify it by the table in §7.1, and emit `supersede` or `retract`. Erasure is left to S10. | S2, S4, S6 |
| **S10** | Erasure: recognise a source removed by a commit carrying `Athenaeum-Erasure`, emit `retract` with that reason, and mark the target's removal commit. | S9, S11; athenaeum#2624 S5a (history read) and S16 (the erasure mark) |
| **S11** | Apply a retract in the target: find the copy by derived uid, follow a tombstone, compare by projection, remove an untouched copy with the retirement mechanics, raise a decision for an edited one, set `watch: ended` when kept. | S7a; ruling 3 |
| **S12** | Dependents: the one `retraction` decision per affected copy, listing pages found by the four reference shapes and by the tombstone, and the cascade extension for a merge that absorbed a copy. | S11 |
| **S13** | Apply a supersede in the target: replace an untouched copy, applying the recorded adjustment again; three-way merge for an edited one, with contested units held; the `access` and `audience` hold; the successor form. | S7a; athenaeum#2624 S3 (the page merge) and S5a (read a file at a revision) |
| **S14** | Shared locations: write messages to the target's inbox or bench, keep both ledgers on a bench where one exists, remove a message once recorded, apply the same-location rule of §7.3, and have a propose-only raiser read the target's `store_id` and `intake` rules from the newest proposer's notice. | S6, S7a; athenaeum#2623 S8 (mounting the bench and inbox); athenaeum#2624 S4 (record merge) and S13 (bench and inbox sync); athenaeum#2625 S7 (the proposer's notices) |
| **S15** | Pull, from the selection onward: take the list of page uids a review selected and turn each into a message and a ledger row beside the cursor, run the raiser pass with the review, and emit `no_longer_selected`. It makes no model call. | S9; athenaeum#2623 S10, with the `purpose: review` check athenaeum#2772 adds, and S11 (the review run that produces the selection); S14 for a bench or an inbox |
| **S16** | Lapse: emit and apply the `lapse` message; detect a lost read from a refused fetch or the `max_offline` unmount; set `watch: lapsed` for the causes of §10.2; raise one decision per origin; always apply a delivered retract or lapse; never apply a raise or supersede from a principal without propose. | S11, S13; athenaeum#2623 S2, S4 and S6; ruling 4 |
| **S17** | `raise.max_silence` and the `attest` message. | S14, S16 |
| **S18** | A three-store test harness that proves a retract travels two hops, one per run. | S9, S11; athenaeum#2624 S6a (the two-clone harness) |
| **S19** | A guide: raising between stores, and what a reader sees on a copy. Docs only. | S14, S15 |
| **S20** | The single routed fact (§6.3): the entry point intake calls with an item, its suggested type and tags and a `store_id`, which writes the `propose-fact` message and the sender's ledger row; the acceptance ledger row that records the pages compiled from it; a retract that withdraws a waiting fact or raises a decision for a compiled one, urgent and at the head of the queue when the reason is erasure; and the fact row in the raiser pass. | S2, S4, S5, S11 |

**Who owns what at the two hand-offs.**

- **With athenaeum#2623 S11 (the review run).** That slice owns the model
  call: the frame prompt, the criteria file and the selection it returns. S15
  here owns everything after the selection: the messages, the ledger rows and
  the watch. Neither writes the other's part. athenaeum#2623 S11 lists this
  note as a dependency; what it needs is S15's input, a list of page uids, and
  S1, S2 and S4 underneath it.
- **With athenaeum#2625 S8 and S9 (the propose path).** That note's S8 decides
  that a fact goes to a store this install only proposes to, and calls S20
  here. Its S9 compiles a received `propose-fact` into the store's pages and
  applies the `intake` rules. S20 here owns the message, both ledger rows and
  the retract.

S6, S7a, S7b, S9, S11 and S13 together give one install a working raise,
retract and supersede between two stores it holds, with no shared location and
nothing from athenaeum#2624 except the pure page merge.

---

## 15. Open questions for the operator

No question is open. The note put five questions, and a sixth after review.
The operator accepted the recommended answer to each, recorded on
athenaeum#2631. They are stated below as rulings, with the options that were
not chosen kept for the record.

### Ruling 1. A copy names the proposer as the receiving store's access list does

A person if the list names people, a group if it names groups. Where it is a
group, the individual is kept in the acceptance ledger, which only the people
who compile the store can see (§5). Not chosen: always naming the individual,
which would tell every reader who reported something; and naming nobody.

### Ruling 2. Acceptance depends on how the raise arrived

A raise selected by a scheduled review that the receiving store itself set up
is accepted automatically. A raise sent by a member is shown to a person, and
so is any page type the store lists under `raise.always_ask` (§4.4). Each
store can change either setting. Not chosen: always asking, and never asking.

### Ruling 3. Untouched copies are removed automatically; edited copies ask

When the origin withdraws a fact, a copy nobody has edited is removed and a
person is told which other pages referred to it. An edited copy stays until a
person decides. Removal for privacy erasure is always automatic (§7.4). Not
chosen: always asking, and always removing.

### Ruling 4. A copy that can no longer be kept current stays, marked

It is marked as no longer watched, and one decision per origin store asks the
people who compile the receiving store whether to keep or withdraw the
affected copies (§10.3). Not chosen: withdrawing automatically, and keeping
silently.

### Ruling 5. Only a store's compile role, and reviewers it declares, may raise its pages

A member raising by hand must be in the origin's compile role. A reviewer's
exemption covers only the output of a review that the receiving store
declares against that origin, and only where the origin's grant says
`purpose: review` (§4.1). A reviewer cannot use that grant to raise a page by
hand. The access note takes a one-line amendment to match, in athenaeum#2772.
Not chosen: any reader may raise; and no reviewers at all.

### Ruling 6. Privacy erasure of a single compiled fact is an urgent decision

Ruling 3 makes privacy erasure automatic for a copy, because a copy is a page
of its own that can be removed whole. A single fact sent to a store (§6.3) is
different. Once the receiving store has compiled it, the fact may be one
sentence on a page that holds other things, and no rule can tell which words
came from it. So the retract becomes a decision marked urgent, at the head of
the receiving store's decision queue, and a person there removes the
sentence. Nothing else on the page is lost; the fact stays readable until
that person acts. Not chosen: automatically removing every page the fact was
compiled into, which takes unrelated content with it; and compiling every
single fact onto a page of its own, which fills the store with one-sentence
pages.
