<!-- SPDX-License-Identifier: Apache-2.0 -->

# Federation: routing a fact to a store at intake

**Status:** DESIGN, not built (Vitruvius Specify, 2026-11-18). Design note for
athenaeum#2625, a child of the federation design epic athenaeum#2631. The
operator answered the four questions in §14 on 2026-11-18, and the note is
written to those rulings. The build slices are in §13.

Code anchors are `path:symbol` at `develop` `759e3b64`.

This note decides, for each new fact, whether it is kept, which store it goes
to and which type it is filed as. It is written on top of the two root notes
and uses their terms unchanged: [store access](federation-store-access.md)
(athenaeum#2623) and [multi-writer sync](federation-multi-writer-sync.md)
(athenaeum#2624). It does **not** decide:

- the capture rules themselves (athenaeum#2598, shipped) or how a page of a
  type is written (athenaeum#2590, shipped);
- the message format, provenance, and raising or retracting between stores
  (athenaeum#2626, written in parallel as
  `federation-raise-retract.md` in this directory). This note says where
  intake hands a fact to it and stops there;
- encryption (athenaeum#2627);
- trust between writers and poisoning of a shared store (athenaeum#2628), and
  routing a decision to the member most likely to know (athenaeum#2629). Both
  are held for an external review. Nothing about them is designed here, and
  no measure of how far a writer is believed is an input to routing.

---

## 1. Summary

1. **One decision, three answers, in order.** Every fact that enters an
   install gets one intake decision: *capture or not*, then *which store*, then
   *which type*. It is one pure function over one rule file, called at the
   three points where capture rules are already enforced. There is no second
   classifier and no second model call.
2. **The rule shape gains a destination.** The relevance policy table gets a
   `Store` column and a `route` verdict. The same matcher and the same
   last-match-wins order answer both "keep it?" and "where?".
3. **A fact reaches a shared store only by a rule its owner wrote, or because
   its owner asked.** A classifier's guess never widens where a fact goes.
4. **Unsure means the personal store.** Whenever the destination cannot be
   established with confidence, or cannot be used, the fact is filed in the
   writer's personal store. That store always exists and has one reader, so it
   is the narrowest store without any ranking of shared stores.
5. **A store limits what enters it, and can only narrow.** Its declaration
   carries ordered accept and refuse rules. The sending install applies them
   before anything leaves, and the compile role applies them again when it
   reads its intake.
6. **Unrouted intake never leaves the install that received it.** Raw intake
   is local and readable only by its owner until the decision is made. What
   reaches a shared location afterwards is a page (when this install compiles
   the store) or one fact in a `propose-fact` message of the raise protocol
   (athenaeum#2626). A routed fact travels as a single fact, never as a page.

**Where each acceptance criterion of athenaeum#2625 is answered.**

| Criterion | Where |
|---|---|
| One intake decision: capture or not, which store, which type | §4 |
| Extends the athenaeum#2598 rule shape with a destination store; deployment versus audience scope | §5 |
| Default when classification is unsure: the narrowest store the writer has | §6 |
| How a store's own policy limits what its members may propose to it | §7 |
| Where raw intake for a shared store is kept before compile, and who can read it | §8 |
| Does not assume a plaintext local index | §10 |
| Build slices | §13 |

## 2. What exists today

| Behaviour | Anchor |
|---|---|
| `remember` writes one raw file under `raw/<session>/` of the single knowledge root. It has no destination argument. | `src/athenaeum/mcp_server.py:remember_write`, `src/athenaeum/storage.py:write_raw_intake` |
| A note saved by `remember` is added to the local recall index at once, before any compile. The default pre-compile roots are `raw/auto-memory`, `raw/claude-session` and `raw/remote-session`. | `src/athenaeum/mcp_server.py:_refresh_extra_root_index`; `recall.extra_intake_roots` in `src/athenaeum/config.py:_DEFAULTS` |
| Capture rules are one markdown table, `wiki/_schema/relevance-policy.md`, with columns `Verdict`, `Type`, `Tag`, `Subject`. Verdicts are `always` and `never`. The last matching rule wins. | `src/athenaeum/relevance_policy.py:RelevanceRule`, `RelevancePolicy.match`, `VERDICTS` |
| Capture rules are enforced at three points, each called identically by both pipeline transports: on declared metadata before any tier, on classified entities after Tier 2, and on update actions before Tier 3. | `src/athenaeum/relevance_policy.py:gate_raw_content`; `src/athenaeum/relevance_classify.py:gate_classified_entities`, `gate_update_actions`; called from `src/athenaeum/librarian.py:process_one` and `src/athenaeum/batch.py:process_batch_run` |
| The classify call already returns a category and a confidence when a rule names a tag. An unsure `never` match escalates to the reasoning tier and, if still unsure, the fact is dropped. | `src/athenaeum/relevance_policy.py:policy_categories`; `src/athenaeum/relevance_classify.py:_decide`, `escalate_category`; `src/athenaeum/models.py:ClassifiedEntity` (`category`, `category_confidence`) |
| A fact a person explicitly asked to keep is exempt from `never` rules, and each such override is audit-logged. | `src/athenaeum/relevance_policy.py:EXPLICIT_REMEMBER_KEY`; `src/athenaeum/mcp_server.py:_audit_relevance_override` |
| A rule is edited conversationally. Over local stdio it is applied at once; over HTTP it is filed as a proposal for the operator. | `src/athenaeum/mcp_server.py` (`set_capture_rule` tool); `src/athenaeum/relevance_policy_edit.py:apply_rule_edit`, `file_rule_edit_proposal` |
| A raw note covered by a `never` rule is kept out of the pre-compile index. | `src/athenaeum/search.py:_load_intake_relevance_policy` and the extra-roots loop that calls it |
| Placement is by type only. A new page is written to the surface `storage.mapping` names for its type, and to the wiki otherwise. | `src/athenaeum/storage.py:resolve_adapter_for_class`, `page_write_root_for_class` |
| The wiki surface is the most private: visibility rank 0. | `src/athenaeum/storage.py:surface_visibility` |
| The configured owner is offered to every classify call, so a first-person fact attaches to the owner's page. | `src/athenaeum/person_hints.py:build_person_hints`, `_owner_candidate` (athenaeum#2653) |
| Auto-memory files are discovered under `raw/auto-memory/<scope>/` and gated on declared metadata only. A file is refused only when every item in it is covered. | `src/athenaeum/intake.py:discover_auto_memory_files`; `src/athenaeum/relevance_policy.py:filter_relevance_never` |
| Merges never cross a surface. | `src/athenaeum/store_boundary.py:assert_same_surface` |

Type alone cannot tell "I like peas" from a team fact of the same type, and
nothing today can send a fact to a store the install does not compile.

## 3. Terms

The terms of athenaeum#2623 §3 and athenaeum#2624 §3.4 are used unchanged:
install, store, principal, compile role, writer, proposer, steward, pages,
inbox, bench, decision queue, host, `store_id`, and the declaration
`_store/access.yaml`. This note adds four.

| Term | Meaning |
|---|---|
| **Personal store** | The install's own default surface: the wiki under its knowledge root. Its only reader is the install's owner. Every install has one. |
| **Item** | One fact inside a raw note, in the sense the capture gate already uses: a `.jsonl` record, one frontmatter document, or one classified entity. Items are decided independently. |
| **Description** | What is known about an item when the decision runs: a candidate type, tags (including the classified category), a subject uid, and the confidence of anything classified. It is evidence, not a decision. |
| **Destination** | The store the decision picks for an item. |

## 4. The intake decision

### 4.1 One function

```
decide(description, rules, stores) -> IntakeDecision
    capture : bool
    store   : "personal" or a store_id
    type    : the type the item is filed as, or a suggestion (§4.4)
    reason  : a code from a fixed list, plus the rule number when a rule decided
```

It is pure: no file access, no clock, no model. `rules` is the parsed
relevance policy (§5). `stores` is what this install knows about each store it
is configured for: its role from the declaration (athenaeum#2623 S2) and the
store's own intake rules (§7).

The three answers are given in this order, and each can end the decision.

| Step | Question | Ends the decision when |
|---|---|---|
| 1 | **Capture or not.** The last matching `always` or `never` rule, exactly as today. | The verdict is `never` and the item carries no explicit-remember marker. The item is dropped. Nothing about it travels anywhere. |
| 2 | **Which store.** The last matching `route` rule, else the `storage.mapping` entry for the candidate type, else the personal store. The candidate is then checked (§6). | Never. A candidate that fails any check becomes the personal store. |
| 3 | **Which type.** The candidate type, confirmed against the chosen store (§4.4). | Never. A type the chosen store does not take sends the item back to the personal store under its candidate type. |

**Why this order.** Capture is first because a fact the owner said never to
keep must not reach any store, and the check is cheap and already fails
closed. The store is second because every later step depends on it: which
pages a name may match, which type vocabulary applies, and who will read the
result. The type is last because a type vocabulary belongs to a store. The
same sentence can be a section of the owner's page in a personal store and a
page of its own in a team store.

Rules match on the *candidate* type in the description. Step 3 fixes the type
the item is actually filed as. The two are usually equal.

### 4.2 Where it is called

No new call site is added. The three existing enforcement points each call
`decide` with what is known at that point, in both transports.

| Point | Description comes from | What can be decided there |
|---|---|---|
| Before any tier (`gate_raw_content` today) | Declared frontmatter only: `type`, `tags`, `uid`, and a declared `store` (§9.2) | Capture. A destination, when a rule or mapping matches on declared keys alone. This is the only point the Tier 0 passthrough and structured-upsert paths reach. |
| After Tier 2 classify (`gate_classified_entities` today) | The classified type, tags, category, confidence and hinted subject | Capture and destination for everything else. |
| Before Tier 3 (`gate_update_actions` today) | The declared type, tags and uid of the page an update would land on | Capture, as today. It never changes the destination (§4.3). |

The classify call is the one that runs today. A `route` rule that names a tag
adds that tag to the category list the call is already given
(`relevance_policy.py:policy_categories` reads every rule's tag). No routing
prompt and no routing model knob are introduced.

A raw note that mixes items is split per item, as the capture gate already
does. Each item gets its own decision and its own destination.

### 4.3 The target page is looked up inside the chosen store only

Once step 2 has picked a store, every lookup for a page to update (the Tier 1
name match, a person hint, a uid bind) is made against the pages of that store
alone. A match in another store is ignored for this item.

Two consequences:

- **No cross-store merge can arise from intake.** The page an item updates is
  always in the item's destination. This is the intake side of the rule
  `store_boundary.py:assert_same_surface` enforces.
- **Where a page already lives does not decide where a fact goes.** An item
  that names a person with a page in a shared store is not appended there
  merely because the name matched. It needs a rule or a mapping that sends it
  there. Without one it goes to the personal store, where it updates or
  creates a personal page.

An install that only proposes to a store holds none of its pages, so it makes
no lookup at all for that store.

### 4.4 Which type

| The install | Type |
|---|---|
| Files the item in its personal store | The candidate type, checked against the install's own type vocabulary, as today. |
| Compiles the destination store | The candidate type, which must be a type the store's intake rules accept (§7). Otherwise the item goes to the personal store. |
| Proposes to the destination store, or is a steward of it | The candidate type and tags travel as a *suggestion*. The receiving compile role may adjust either when it accepts the fact, and the adjustment is recorded with the acceptance (athenaeum#2626). |

The page is then written to the destination the decision chose. Slice S3a
changes the compile write path to take the store from the decision, where
today `storage.py:page_write_root_for_class` derives it from the type.

### 4.5 A received message is not routed again

The decision runs once, on the install where the fact entered. Anything that
arrives through the raise protocol is outside it. That covers every message
kind of athenaeum#2626 (a `propose-fact`, a raise, a supersede, a retract and
the rest), **wherever the message waits**: an inbox, a bench, or the
compile-role install's own waiting place for that store (§8.1). For such a
message:

- the destination is fixed: the store the message is addressed to;
- the only rules applied are that store's intake rules (§7) and the
  acceptance rules of athenaeum#2626;
- `decide` is never called on it, and the compiling member's own relevance
  policy is **not** applied. A member's personal `never` rule must not drop a
  team's proposal or raise, and a member's personal `route` rule must not
  move one into another store they happen to hold.

This is structural, not a flag on the decision. The waiting places are not
raw-intake sources: ordinary raw discovery never visits them (§8.1), so no
message can reach the three enforcement points of §4.2.

Sending a fact onward from one store to another is a raise, and belongs to
athenaeum#2626.

## 5. Rules: the athenaeum#2598 shape with a destination

### 5.1 The shape

The policy file stays where it is, `wiki/_schema/relevance-policy.md`, and
stays one table read top to bottom. It gains one column and one verdict.

```
| Verdict | Type   | Tag             | Subject | Store    |
|---------|--------|-----------------|---------|----------|
| never   |        | personal        |         |          |
| always  |        | personal/travel |         |          |
| route   |        | hr              |         | hr       |
| route   | policy |                 |         | general  |
| route   |        | hr/health       |         | personal |
```

- **`route`** names a destination. Its `Type`, `Tag` and `Subject` cells mean
  what they mean on any other row and are matched by the same code
  (`relevance_policy.py:RelevanceRule.matches`).
- **`Store`** is the install's name for the store: an entry under
  `storage.adapters`, or the reserved word `personal`. It is required on a
  `route` row and must be blank on an `always` or `never` row. A table without
  the column is valid and has no routes, so every existing file parses as
  before.
- **Two questions, one pass.** Capture is the last matching row among
  `always` and `never`. Destination is the last matching row among `route`.
  Neither kind of row affects the other's answer. An exception is written the
  way it already is: the general row first, the exception after it. The last
  row above keeps health facts personal although the `hr` row would have sent
  them on.
- **`never` beats `route`.** Capture is decided first (§4.1).
- **Validation.** A `route` row whose store is not configured on this install
  fails validation and names the row, like any other bad rule. A policy that
  fails validation routes nothing: every captured item goes to the personal
  store.
- **Hash and read-back.** The `Store` cell is part of
  `RelevancePolicy.canonical_hash`, so a routing change shows up in the run
  summary's policy token.

The edit path is unchanged in behaviour. `set_capture_rule` gains a `store`
argument. Over local stdio the rule is applied and audit-logged. Over HTTP it
is filed as a proposal for the owner, which matters more for a `route` rule
than for any other: a remote client cannot widen where facts go.

`storage.mapping` stays. It is the standing destination for a whole type, and
it also places types on surfaces that are not shared stores. A `route` rule is
the finer instrument: it can tell two facts of one type apart, and it wins
over the mapping when both match.

### 5.2 Per deployment, or per audience?

athenaeum#2598 asked whether capture policy is per deployment only, or also
per audience. Its per-audience `restrict` verdict was deferred to the store
access model. Operator ruling 4 (§14) fixes the store side of the answer: a
store may only refuse.

**The policy is per install. There is no per-audience verdict, and `restrict`
is not added.** Three reasons.

1. **Each person runs their own install**, and an install has one owner. A
   policy file per install is already a policy per person.
2. **The case behind the question is answered by the destination.** "A work
   audience that never sees personal pages" is a statement about where
   personal facts are kept. A `route` rule keeps them in the personal store,
   and the installs of the work audience do not hold that store
   (athenaeum#2623 §9). Nothing has to be filtered for anyone.
3. **A per-audience verdict would be a filter over pages an install already
   holds.** athenaeum#2623 rejected that shape for store access (its rejected
   alternative 1).

The scope a single install cannot express is "what this *store* takes". That
is the store's own intake rules (§7), carried by the store. So there are two
scopes, each with one owner:

| Scope | Lives in | Owner | May it widen? |
|---|---|---|---|
| Install | `wiki/_schema/relevance-policy.md` | The install's owner | Yes, within the grants the owner holds |
| Store | The `intake` block of `_store/access.yaml` | Whoever may push the store's pages | No. It only refuses. |

Page-level `audience:` remains a separate axis (athenaeum#2623 §10). Routing
never sets or changes it.

## 6. The default: the narrowest store the writer has

**The narrowest store a writer has is their personal store.** athenaeum#2623
§7 states that access is not nested: two shared stores can each be narrow and
have no reader in common, so no ranking of shared stores exists. The personal
store needs no ranking. Its only reader is the writer, and every install has
one. Any shared store the writer can reach has at least that reader.

An item goes to a shared store only when **all** of the following hold. If
any one fails, the item is filed in the personal store and the reason code is
recorded.

| Check | Fails when | Reason code |
|---|---|---|
| A rule or the owner chose the store | No `route` rule and no mapping matched, and the owner named no store | `default` |
| The match is confident | The deciding rule or mapping used a classified key (the category, or the type) and its confidence is not high | `unsure` |
| The install may write there | The declaration does not give this install compile or propose on the store, the declaration cannot be parsed, or the directory cannot answer (athenaeum#2623 §6) | `no-grant` |
| The store takes it | The store's intake rules refuse the item's type and tags, or cannot be read (§7) | `store-refuses` |
| The entry path allows it | The item came through a path that never leaves the personal store (§9) | `path` |

**What "unsure" means.** Declared metadata is not classified, so a match on
declared keys alone is confident. A match that depends on what the classifier
said is confident only when the classifier reported high confidence. Today the
classify call reports a confidence for the category only
(`models.py:ClassifiedEntity.category_confidence`). Slice S4 adds the same
field for the type, in the same call, so that a rule or mapping keyed on type
alone has a signal too.

**No escalation for a destination.** An unsure `never` match escalates to the
reasoning tier because the alternative is losing a fact. An unsure destination
does not: the alternative is the personal store, which loses nothing. By
operator ruling 1 (§14) the owner is not interrupted: such facts are listed in
a report the owner reads when they choose (slice S12).

**Why narrow is the safe mistake.** A fact filed too narrowly can be raised
later (athenaeum#2626). A fact filed too widely has been read, and a
retraction does not undo that.

**What changes for stores that exist today.** A surface with no declaration
behaves exactly as it does now, as both root notes require: `storage.mapping`
places its types whatever the classifier's confidence. The confidence check
applies to `route` rules, which are new, and to mappings whose target is a
store with a declaration.

## 7. A store's own policy

A store limits what enters it in two ways. Both are in its declaration.

**Who.** `grants.propose` and the compile role (athenaeum#2623 §5, §7). This
note adds nothing to them.

**What.** A new `intake` block in `_store/access.yaml`:

```yaml
intake:
  default: refuse            # accept | refuse: the answer when no rule matches
  rules:                     # ordered; the last matching rule wins
    - {verdict: accept, type: person}
    - {verdict: accept, type: policy, tag: hr}
    - {verdict: refuse, tag: personal}
```

- **The block arrives with a higher declaration `version`.** The declaration
  already carries a `version` key (athenaeum#2623 §5). New top-level keys,
  such as `intake:` here or the `raise:` block of athenaeum#2626, arrive only
  with a higher `version`. An install that does not support that version
  treats the store as read-only: it neither compiles nor proposes there. That
  is explicit and fails closed, so an older install can never write to a
  store whose intake rules it cannot apply, and it is never left guessing
  whether an unknown key is an error. athenaeum#2772 implements the parser,
  which treats an unsupported version as invalid.
- A rule has a `type`, a `tag`, or both, matched the way the relevance policy
  matches them (a tag covers its `/`-separated children). It has **no
  `subject`**: a subject is the uid of a page, and a proposer may not be able
  to read the store's pages.
- The verdicts are `accept` and `refuse` only. A store cannot write a `route`
  rule, an `always` rule, or anything that pulls a fact toward itself. **A
  store's policy only narrows.** It decides what the store will take from
  what members choose to send, and nothing else.
- A store with no `intake` block takes whatever its writers and proposers
  send, which is today's behaviour. A block that is present and cannot be
  parsed is treated as refusing everything from this install, and is
  reported.
- The rules are evaluated by code against the type and tags the decision
  already has. They add no category to any prompt and are never shown to a
  model. Nothing a store carries steers a member's classifier.

**It is applied twice.**

| Where | By whom | Standing |
|---|---|---|
| In `decide`, on the sending install, before anything is written to the store's pages, inbox or bench | The writer's or proposer's own install | A courtesy that keeps refused facts from leaving. It is self-policing, like the install's self-declared identity (athenaeum#2623 §6). |
| When the compile role reads a `propose-fact` message, wherever it waits (§8.1) | A compile-role install | Authoritative. A fact the rules refuse is not compiled into a page. The check uses the type and tags as adjusted on acceptance (§4.4). |

The second application also covers a message written by an older or
misconfigured install that skipped the first.

**The proposer's notice.** Propose does not imply read (athenaeum#2623 §4),
and the declaration lives in the pages repository. A principal who may only
propose therefore cannot see the store's `store_id` or its intake rules. So
the compile role publishes exactly those two things in the inbox, as a
**notice**:

```yaml
# _store/propose/notice-3.yaml, in the inbox repository
version: 2                      # the declaration's version
store_id: st_q7m2xk4vwz9d
intake:
  default: refuse
  rules:
    - {verdict: accept, type: person}
```

- **Notices are append-only and versioned.** Each is its own file,
  `_store/propose/notice-<n>.yaml`, written once and never edited. The one
  with the highest `<n>` is in force. A new notice is written only when the
  `store_id` or the `intake` block of the declaration has changed since the
  newest notice, not on every run.
- **Only the compile role writes them.** The declaration stays the single
  source; a notice is a derived copy. With one inbox per proposer (operator
  ruling 1 on athenaeum#2623), each inbox gets its own.
- **This keeps the inbox append-only**, as athenaeum#2624 §8.1 requires. A
  new notice is a new path, so it falls in that note's "changed on one side
  only" case, exactly like a new proposal. A notice's bytes are a pure
  function of the declaration, so two compiling installs that write the same
  number from the same declaration write identical files, which is not a
  conflict. If they ever differ, athenaeum#2624 §5 holds the file as it holds
  anything under `_store/`, and the next notice supersedes it. No file in the
  inbox is ever rewritten, and this note asks athenaeum#2624 for no new sync
  rule.
- It discloses the store's id and what the store accepts. It does not disclose
  members, roles, reviews or any page. That is less than the declaration,
  which every reader of the store may already see (operator ruling 4).
- A proposer who adds or alters a notice changes nothing: the compile role
  applies the rules from the declaration.
- A proposing install with no notice, or whose newest notice it cannot parse
  or carries a `version` it does not support, sends nothing to that
  store.

This note asks athenaeum#2626 to apply the same intake rules to a raised
copy, so that a store has one statement of what it takes, whichever way a
fact arrives.

## 8. Where intake waits before compile, and who can read it

**Unrouted raw intake never leaves the install that received it.** `remember`
keeps writing to the install's own `raw/` tree. The destination is decided
during that install's librarian run. Until then the note is readable by the
install's owner and by nobody else. There is no shared raw directory, and no
raw note is ever written to a shared location.

Shipping raw notes to a store for its compile role to classify was rejected
(§12): a raw note is unfiltered, may mix items meant for different stores, and
has not yet passed the capture rules.

After the decision, what exists depends on the install's role in the
destination.

| This install | What the item becomes | Where it waits | Who can read it there |
|---|---|---|---|
| Files it in the personal store | A page in the personal store, in the same run | Nowhere | The owner |
| Is a writer of the destination (pushes its pages) | A page in the destination, compiled by this install in the same run, then synced by the sequence in athenaeum#2624 §4 | Nowhere shared. Until the push it is a local commit. | The owner, until the push; then the store's readers |
| Holds propose only | One fact in a `propose-fact` message of the raise protocol (athenaeum#2626) | The store's inbox | The compile role. In a shared inbox, also the other proposers, which operator ruling 1 on athenaeum#2623 allows only when every proposer can read the store. |
| Is a steward (pushes the bench, not the pages) | One fact in a `propose-fact` message | The store's bench, as intake waiting for the compiling install | The compile role |

In every row the raw note itself is consumed by the run and deleted, as raw
notes are today.

**A writer compiles its own intake.** A writer's item does not pass through
the bench, even when the store has one. The install that received the fact
compiles it, so each fact is compiled once and by one librarian. Putting it on
the bench would let two compiling installs each write a page for it, and
athenaeum#2624 §5.4 notes that git sees no conflict between two such pages.
This differs from the review output in athenaeum#2623 §8, which goes to the
bench when one exists. That table is unchanged. Review output is a proposal
for the compile role to judge; a writer's own intake is something that writer
is already entitled to compile.

**The same install uses different waiting places for a routed fact and for a
raise, on purpose.** A writer's routed fact skips the bench and becomes a page
at once. A raise of an existing page by that same install is a message, and
athenaeum#2626 §6.2 sends it to the bench when the store has one. The
difference is what each needs. A raise must be accepted by the compile role
before it is a page, and its copy has a uid derived from the raise, so two
compiling installs that process one message create the same page. A routed
fact needs no acceptance from a role its writer already holds, and it has no
derived uid, so waiting on the bench would only give two librarians the
chance to write two different pages for it.

**The hand-off to athenaeum#2626: a routed fact travels as one fact, never as
a page.** Intake never writes a message file itself. For the last two rows of
the table it hands the raise protocol exactly this: the item's own text, its
suggested type and tags, and the destination `store_id`. athenaeum#2626 owns
the message kind that carries it, `propose-fact`: one fact with its suggested
type and tags, a `raise_id`, and a row in the raiser's ledger so that the
sender can withdraw it later. The envelope, provenance, the ledger and
everything that happens to the message afterwards are that note's.

The unit matters. In the sender's personal store an item is often not a page
of its own: a first-person fact becomes a section of the owner's page (§9.6),
and a fact about a colleague becomes a line on a personal page about them.
The message is therefore built from the **item**, as the decision saw it, and
never from the personal page the item was filed on. Nothing else on that page
leaves the install. A routed fact is not a page raise, and the page
projection of athenaeum#2626 §4.2 is not used for it.

This note places three requirements on the `propose-fact` kind:

- it follows the three rules athenaeum#2624 §8.1 sets for anything in an
  inbox: one file per message, a random id in its name, never edited after it
  is written;
- it carries the suggested type and tags, so the compile role can apply the
  store's intake rules without a model call;
- it carries one fact and nothing else: no other text from the sender's
  pages, no uid or path of a personal page, and nothing read from the sending
  install's index.

**The sender keeps a copy** (operator ruling 2, §14). The item is also
compiled into the sender's personal store in the same run, exactly as an
unrouted fact would be. The personal copy and the message are made from the
same item independently; neither is derived from the other.

**If the inbox or bench cannot be reached,** the message waits in the sending
install's own state and is sent on a later run. It is never written anywhere
wider in the meantime.

### 8.1 The compile-role install's own waiting place for a store

athenaeum#2623 §8 and athenaeum#2626 §6.2 both send a message to "that
install's own raw intake for the target store" when the target has one
compiling install and no bench, and leave the place to this note. It is:

```
<knowledge root>/raw/_stores/<store_id>/
```

- **One directory per store, named by `store_id`.** Messages sit in it with
  the layout they have on a bench (athenaeum#2626 §6.1), so the receiving pass
  reads all three waiting places with one reader.
- **Only this install reads it.** It is on this machine, in no shared
  location, and is never pushed. It exists only on an install that is the
  single compiler of that store.
- **It is not a raw-intake source.** Ordinary raw discovery skips
  `raw/_stores/` whole, the way `src/athenaeum/intake.py:discover_raw_files`
  already skips the `answers` directory and any directory named in
  `librarian.non_intake_sources`. Nothing in it reaches classify, the capture
  gates or `decide` (§4.5).
- **It is never indexed.** It is not among `recall.extra_intake_roots`, and
  the config check that refuses to index an inbox or a bench (athenaeum#2623
  §9) refuses this directory too.
- **It is read by the receiving pass of the raise protocol only**, which
  applies the store's intake rules and athenaeum#2626's acceptance rules.

It is not where a writer's own routed facts wait. Those do not wait (the
table above).

## 9. Entry paths

### 9.1 `remember`

Unchanged: one raw file in the install's own `raw/<session>/`. The destination
is decided later, by the librarian run. Deciding it inside `remember` was
rejected (§12).

### 9.2 A destination the owner names

`remember` gains an optional `store` argument, stamped into the raw note's
frontmatter as a declared key, beside `type` and `tags`.

- `store: personal` is always honoured. Narrowing needs no permission.
- A shared store is honoured only together with `explicit_request: true`,
  that is, when the person asked for it. Without the marker the named store is
  ignored and the rules decide. The calling model's own choice of a wider
  store is a guess, and a guess does not widen.
- An honoured destination still passes the grant check and the store's intake
  rules (§6). Those only narrow, and nothing overrides them.
- A fact kept by `explicit_request` against a `never` rule goes to the
  personal store unless the person also named a store.

### 9.3 The remote transport

A fact written over the HTTP transport comes from a caller the install cannot
identify today. By operator ruling 3 (§14) such a fact does not leave the
personal store on its own. It is filed there, and the owner forwards it.

### 9.4 Auto-memory

Auto-memory intake always goes to the personal store. Its files belong to the
host tool, are gated on declared metadata only, and are compiled by a cluster
merge across files. A merge cannot cross a store, so sending some of a
cluster's files elsewhere would split it. An auto-memory fact that belongs in
a shared store gets there by a raise.

### 9.5 The deterministic paths

The Tier 0 passthrough and the structured upsert never reach the classifier.
They are routed at the first enforcement point, on declared keys, and the
match is confident by definition (§6). A structured-upsert raw that binds to a
page by uid is looked up in its destination only (§4.3).

### 9.6 First-person facts

athenaeum#2653 attaches a first-person fact to the owner's page. The owner's
page is in the personal store, and with no rule the destination is the
personal store, so "I like peas" stays where it is today. A `route` rule the
owner wrote can still send a first-person fact elsewhere: the store is chosen
first, and the page lookup then runs inside it (§4.3), where the owner hint
matches only if that store has a page for the owner.

## 10. The local index

**This design does not assume a plaintext local index, and the decision does
not read the index at all.**

- `decide` takes a description, the rule file and the store declarations.
  None comes from the recall index.
- Looking up the page an item updates reads the pages of the destination
  store. An install that only proposes to a store makes no lookup for it.
- A decision record holds ids and codes: the store, the type, the rule number
  and a reason code. It holds no text from the fact. The capture gate keeps a
  short excerpt of a dropped item for the local drop ledger
  (`relevance_policy.py:RelevanceDrop`); a routing record does not, and
  nothing derived from the fact other than the `propose-fact` message leaves the
  install.
- The inbox and the bench are never indexed (athenaeum#2623 §9).

One existing behaviour needs a rule. `remember` adds each new raw note to the
local index before compile (§2), so unrouted text is in the index of the
install that received it. That is within the invariant, because only the
owner reads it, but two requirements follow:

- **Pending intake belongs to the personal store's part of the index.** When
  the index is made separable by store (athenaeum#2623 S4b), rows for raw
  intake are kept with the personal store and protected with its key alone
  (athenaeum#2627).
- **A fact that left for a store this install cannot read leaves no index
  rows behind.** When its raw note is consumed, its pre-compile rows go with
  it. The sender's own copy (operator ruling 2, §14) is an
  ordinary personal page and is indexed as one.

## 11. Boundaries with neighbouring work

| Component | Owns | Does not own |
|---|---|---|
| **Relevance policy** (`athenaeum.relevance_policy`) | The rule file, its parser and validator, the `route` verdict and the `Store` column, and the matcher. | Which stores exist or what this install may do in them. |
| **Intake decision** (new, pure) | `decide`: the three answers, the fallback to the personal store, the reason codes. | Any I/O, any model call, classification. |
| **Classify** (existing Tier 2 call) | The description: type, tags, category, confidence. | The decision. |
| **Store declaration** (athenaeum#2623) | Roles, and the `intake` block this note adds to it. | The install's own rules. |
| **Compile write path** | Writing the page to the store the decision chose; lookups scoped to it. | Choosing the store. |
| **Raise protocol** (athenaeum#2626) | Every message kind including `propose-fact`, provenance, the ledgers, sending, acceptance, retraction. | Whether and where a new fact is routed. |

- **athenaeum#2623** supplies the role of this install in each store, the
  inbox and the bench, and the declaration that carries the `intake` block.
  Its review output (§8 there) keeps its own destination table.
- **athenaeum#2624** syncs the pages, the inbox and the bench. This note adds
  no sync step. The proposer's notice follows its rule for `_store/` files.
- **athenaeum#2626** takes over at the hand-off in §8 with its `propose-fact`
  kind, uses the waiting place of §8.1, and is asked to apply
  the store's intake rules to raised copies (§7).
- **athenaeum#2598** and **athenaeum#2590** are shipped and unchanged in
  behaviour. The capture verdicts, their escalation and their audit trail work
  as they do today.
- **athenaeum#2607** (remote-caller ceilings) decides what a remote caller may
  read. §9.3 is the matching rule for what a remote caller's writes may reach.

**Repository split.** The rule shape, the decision, the `intake` block, the
notice and the scoped lookup are engine mechanism and belong in this
repository. A deployment's rules live in its own installs and stores.

## 12. Failure classes, migration and rejected alternatives

**Failure classes the design must survive.**

| Failure | Outcome |
|---|---|
| The classifier is unsure which rule applies | Personal store, reason `unsure`. |
| A `route` rule names a store this install may not write | Personal store, reason `no-grant`. |
| The store's intake rules refuse the item | Personal store, reason `store-refuses`. |
| The policy file fails validation | Nothing is routed; every captured item goes to the personal store. Capture rules behave as they do today on an invalid file. |
| A grant is withdrawn between the decision and the push | The host refuses the push. The item stays local, the run reports it, and a later run decides again against the new declaration. |
| A routed fact was filed as a section of a personal page | Only the fact is sent. The message is built from the item, never from the page (§8). |
| A message of the raise protocol waits on an install whose owner has a matching `never` or `route` rule | The rule is not consulted. Messages never pass through `decide` (§4.5, §8.1). |
| An install older than the declaration's `version` meets a store with an `intake` block | It treats the store as read-only (§7). |
| The inbox or bench is unreachable | The message waits locally and is sent later (§8). |
| A proposing install skips or alters the notice | The compile role applies the declared rules and does not compile a refused proposal (§7). |
| One raw note holds items for several stores | Each item is decided on its own. No item travels with another. |
| A name matches a page in a store the item was not routed to | The match is ignored; the lookup runs in the destination only (§4.3). |
| A `never` rule and a `route` rule both match | The item is dropped. Capture is decided first. |
| A fact was filed too narrowly | It is in the personal store and can be raised (athenaeum#2626). |
| A fact was filed too widely | It can be retracted (athenaeum#2626). What was already read is not recovered. |
| A raw note is written to steer the classifier toward a wide store | Bounded by the checks in §6 and, for a proposal, by the compile role. Anything further is athenaeum#2628. |

**Migration.** Each step is additive.

1. A policy file with no `Store` column and no `route` row parses as before
   and routes nothing. An install with no such row behaves exactly as today.
2. A store with no `intake` block takes what it takes today.
3. A surface with no declaration keeps today's placement by type (§6).

**Rollback.** Remove the `route` rows and placement returns to
`storage.mapping`. Remove a store's `intake` block and it accepts as before.
Pages already written to a store stay there; moving them back is a retraction
under athenaeum#2626, not something this note automates. No page format, uid
or index format changes.

**Principles applied.** One decision point, not two: the destination is a
column on the rule that already exists. Deterministic first: the model
supplies a description, code decides. Failure narrows: every fallback is the
personal store. A store's policy only refuses. Existing seams are reused: the
capture gates, the rule matcher, the declaration, the storage adapters. The
engine ships mechanism; each owner and each store supply their own rules.

**Rejected alternatives.**

1. **A separate routing classifier, or a second rules file for
   destinations.** Rejected: two classifiers can disagree about the same fact,
   and two files can disagree about the same category. athenaeum#2625 names
   this risk directly.
2. **Let the caller of `remember` choose the store.** Rejected as the
   mechanism: the calling model's choice is a guess that would widen, remote
   callers are unidentified, and imports and auto-memory have no caller. It is
   kept only as the owner's explicit request (§9.2).
3. **Rank shared stores and pick the narrowest by rank or by reader count.**
   Rejected: access is not nested, so no rank exists, and counting readers
   would need the directory at intake time. The personal store is always
   defined.
4. **A per-audience capture verdict (`restrict`).** Rejected: it filters pages
   an install already holds, which the access model exists to avoid (§5.2).
5. **Send raw notes to the store and let its compile role classify them.**
   Rejected: raw text would reach a wider audience before the capture rules
   and before routing, and a mixed note would carry items meant for other
   stores.
6. **Let a store ship routing rules or prose guidance to its members.**
   Rejected for now: a file in a store that steers every member's librarian is
   the kind of control athenaeum#2628 is reviewing. Operator ruling 4 (§14).
7. **Pass a writer's intake through the bench.** Rejected: two compiling
   installs could each write a page for the same waiting item (§8).

## 13. Build slices

Each is one pull request. S1, S2 and S3a depend on nothing outside this note.
A
dependency written `athenaeum#2623 S2` or `athenaeum#2624 S13` is a slice of
that note's build-slice table, and `athenaeum#2626 S2` of the table in
`federation-raise-retract.md`. The one exception is the athenaeum#2626 slice
that adds the `propose-fact` kind: it is cited by description, because it had
no number when this note was written.

| Slice | What it adds | Depends on |
|---|---|---|
| **S1** | The `Store` column and the `route` verdict in the policy parser, validator, read-back and hash. `set_capture_rule` gains `store`, through the existing stdio and HTTP edit paths. A file without the column parses byte-identically. No routing. | none |
| **S2** | The pure `decide` function and its reason codes. The three existing enforcement points call it in both transports, with a parity test. With no `route` row its capture answer is byte-identical to today's gates. No behaviour change. | S1 |
| **S3a** | The compile write path takes the store from the decision, where it derives it from the type today. `storage.mapping` is the default destination, so with no `route` row every page lands where it lands now. No routing change. | S2 |
| **S3b** | Routing on declared keys, for stores this install compiles and for the personal store: `route` rows take effect on declared metadata, page lookup is scoped to the destination (§4.3), and auto-memory is pinned to the personal store. | S3a; athenaeum#2623 S2 (role resolution) and S3 (the declared role in the write guard) for stores with a declaration |
| **S4** | Routing on classified keys: `route` tags join the category list of the existing classify call, a type confidence is added to the same call, and anything not high-confidence goes to the personal store with reason `unsure`. No new model call. | S3b |
| **S5** | The `intake` block of `_store/access.yaml`: parse, validate, fail closed, and a pure accept check. The block is accepted only in a declaration whose `version` is the one that introduces it; an install that does not support that version treats the store as read-only (§7). Documented with the declaration format. | athenaeum#2623 S1; athenaeum#2772 (the declaration parser and its `version` rule) |
| **S6** | Apply the store's intake rules in `decide` for stores this install compiles, with reason `store-refuses`. | S3b, S5 |
| **S7** | The proposer's notice: a compile-role install appends `_store/propose/notice-<n>.yaml` to each inbox when the declaration's `store_id` or `intake` block has changed, and a proposing install reads the newest. Includes the local config entry by which an install that only proposes names a store's inbox, in the `storage.adapters` family, never indexed. | S5; athenaeum#2623 S8 (the inbox mount); athenaeum#2624 S13 (inbox sync) |
| **S8** | The propose path: for a store where this install holds propose only, hand the item alone (never the personal page it was filed on), its suggested type and tags, and the `store_id` to the raise protocol as a `propose-fact`; compile the sender's own copy into the personal store; queue locally when the inbox is unreachable. | S4, S6, S7; athenaeum#2623 S2; athenaeum#2626 S2 (the codec), S4 (the raise ledger), S14 (writing to an inbox or bench), and its slice that adds the `propose-fact` kind |
| **S9** | The receiving side: a compile-role install applies the store's intake rules to each `propose-fact` it reads, using the type and tags as adjusted on acceptance, compiles accepted ones into that store only, and never calls `decide` or applies its owner's personal policy to any raise-protocol message (§4.5). | S5, S15; athenaeum#2623 S8; athenaeum#2624 S13; athenaeum#2626 S5 (the acceptance ledger), S7 (acceptance), and its slice that adds the `propose-fact` kind |
| **S10** | The steward path: the bench as the waiting place for a steward's `propose-fact` messages. | S8, S9; athenaeum#2623 S9 (stewards) |
| **S11** | The owner's explicit destination: the `store` argument of `remember`, honoured for `personal` always and for a shared store only with `explicit_request`; the remote-transport rule of §9.3 (operator ruling 3). | S3b |
| **S12** | Reporting: a routing token in the run summary (counts by store and reason code, no fact text) and a read-only report of facts held in the personal store with reason `unsure` (operator ruling 1). | S4 |
| **S13** | Index hygiene: pre-compile rows for raw intake are kept with the personal store's part of the index, and are removed when the raw note is consumed. | athenaeum#2623 S4b |
| **S14** | A guide: writing routing rules, and giving a store intake rules. Docs only. | S6, S8 |
| **S15** | The compile-role install's own waiting place for a store, `raw/_stores/<store_id>/` (§8.1): skipped whole by raw discovery, refused by the index config check, and read only by the raise protocol's receiving pass. This is the place athenaeum#2623 §8 and athenaeum#2626 S6 depend on. | athenaeum#2623 S1 (for `store_id`) |

S1 to S4 are useful on their own: one person with a personal store and a
store they compile gets rule-based routing with no inbox, no bench and no
second install.

## 14. Open questions for the operator

Each was a values call the design could not settle. **The operator answered
all four on 2026-11-18, accepting the recommended answer each time**; the
rulings are recorded on the design epic athenaeum#2631. The accepted answer is
listed first and marked *Ruled*, the design above is written to it, and the
other options are kept as the record of what was weighed. No question is
open.

**1. When intake is unsure and keeps a fact in your personal store, should it
tell you?** A rule says facts about staff go to the HR store. The assistant
hears something that might be about staff but is not confident. The fact is
kept in your personal store, which is safe. The question is whether you hear
about it.

- **Ruled: keep it quietly, and list such facts in a report you can
  read when you choose.** Nothing interrupts you, and nothing is lost: the
  report shows each fact and the store it might have gone to. Cost: a fact
  the team needed can sit unseen until you look.
- Raise a decision for each one, asking whether to send it. Nothing sits
  unseen. Cost: every uncertain fact becomes an item in your decision queue.
- Ask the stronger model first, and only keep the fact personal if it is also
  unsure. Fewer facts held back. Cost: a model's second guess, not a rule,
  would be sending facts to a wider audience, and each one costs a model call.

**2. When you send a fact to a store you cannot read, does your own assistant
keep a copy?** Some people may propose to a store without being able to read
it, for example an employee reporting something to HR. Once the fact is sent,
their own assistant either still knows it or does not.

- **Ruled: keep a copy in the personal store.** What is sent is the one fact,
  never the personal page it was filed on (§8). A person does not
  forget what they themselves said, and the copy lets a later correction or
  withdrawal find what was sent. Cost: the fact exists in two places, and the
  sender's copy is outside the receiving store's control.
- Keep a receipt only: that something was sent, to which store and when, but
  not the text. The sender can see their history without holding the content.
  Cost: their assistant can no longer answer questions about it.
- Keep nothing. The receiving store holds the only copy. Cost: the sender's
  assistant has no memory of it at all, and a withdrawal has nothing to start
  from.

**3. May a fact that arrives through a remote connection be routed to a
shared store?** An install can be reached remotely, for example from a chat
application on a phone. The install cannot tell today who is on the other end
of that connection.

- **Ruled: no. It stays in the personal store, and the owner forwards it from
  there.** Nothing written by an unidentified caller reaches a team store
  without the owner acting. Cost: facts captured away from the desk need a
  second step to reach the team.
- Yes, by the same rules as local facts. Capture works the same everywhere.
  Cost: anyone who can reach the remote connection can put text in front of a
  team store's compile role under the owner's name.
- Only when the remote request says the person explicitly asked. A middle
  path. Cost: that flag is set by the calling application, which is the party
  the install cannot identify.

**4. May a shared store tell its members' assistants what to send it?** With
this design each person writes their own routing rules. A team might prefer
the store itself to carry them, so that joining the HR store brings "facts
about staff belong here" with it.

**The ruling holds until the external review in athenaeum#2628 reports; the
question itself is deferred to that review.** A
file in a store that changes what every member's assistant sends is a way to
steer those assistants, and what would make that safe is the review's
subject. This note designs no control for it and builds nothing for it. The
options are recorded so the review has them.

- **Ruled for now: no. A store may only refuse; each person writes
  their own rules.** Nothing in a store can pull facts out of a member's
  install. Cost: every member sets up their own rules, and members' rules can
  differ.
- Yes, as suggested rules each member must accept before they apply. Members
  stay in control and setup is one confirmation. Cost: a suggestion accepted
  without reading works like an instruction.
- Yes, applied automatically to every member. The least setup. Cost: anyone
  who can change that file can change what every member's assistant sends.
