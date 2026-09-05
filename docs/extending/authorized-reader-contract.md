<!-- SPDX-License-Identifier: Apache-2.0 -->

# The authorized-reader contract for excluded/suppression facts (athenaeum#851)

**Status: SHIPPED.** This document describes behaviour that landed with
`src/athenaeum/pii.py`'s point 7 ("Facts, not verdicts") — `DoNotEmailState` /
`do_not_email_state`, `IdentifierValidity` / `validity_for_value` /
`assemble_excluded_validity`, `ExcludedSurfaceUnavailable`, `IdentifierFacts` /
`read_identifier_facts`. It is a companion to
[`docs/one-way-in-one-way-out.md`](one-way-in-one-way-out.md), whose §3 is the
canonical statement of the one-read-path invariant this document assumes
rather than restates, and to
[`docs/bounce-surface-convergence.md`](bounce-surface-convergence.md), which
settles how a bounce mark relates to the wiki `bounced:` field — a question
this document does not reopen.

## What the excluded read path is

The excluded read path is the same one §3 of
[`docs/one-way-in-one-way-out.md`](one-way-in-one-way-out.md) already names,
now carrying strictly more of what a suppression consumer needs: `recall`
with its excluded-field flag set (`with_pii=True` on the MCP tool,
`--with-pii` on the CLI) when the caller is searching; `read_entity` /
`read_entities` when the caller already holds a uid; and, new for athenaeum#851,
`read_identifier_facts` for the bulk by-address case — a campaign holding a
list of email addresses and no uids at all.

Athenaeum's side of this contract is narrow on purpose: it returns facts, with
provenance, about what it holds. It does not return a verdict about what the
caller should do with those facts. `pii.py`'s module docstring, point 7,
states this as "Facts, not verdicts," and every type introduced for athenaeum#851
follows it — `IdentifierFacts`, `IdentifierValidity`, `DoNotEmailState`, and
`ContactClassification` (pre-existing, athenaeum#866) are all facts records: a
value, how it was obtained, whether it is still open, and on whose word. None
of them carries a field that answers "may I email this person."

`read_identifier_facts` is built on `ExcludedRecordIndex` (athenaeum#883),
which indexes a whole excluded surface — by uid and by address — in one
`iter_contact_records` pass. That is why resolving N identifiers costs ONE
corpus scan rather than N: the same property that took `read_entities` from
~28s per uid (~37 hours for the 4,696-person population `apollo-enrich`'s
weekly job resolves) down to one scan for the whole batch applies here for the
~16.9k-contact campaign case. A caller that looped `validity_for_value` one
address at a time, each against a freshly-built index, would be paying the
old per-call cost back — `read_identifier_facts` exists specifically so no
consumer has to write that loop.

`IdentifierFacts.known` is the field to read first. It states, positively,
whether the address is on the excluded surface at all — never left to be
inferred from an empty result. §3 below explains why that distinction is
load-bearing.

**A note on a different `do_not_email`, so the two are not conflated
(athenaeum#1122).** Everything in this document — `DoNotEmailState`,
`do_not_email_state`, `facts.do_not_email.marked` in §3's example — concerns
the excluded-surface record this document is about, reached only through
`recall`/`read_identifier_facts`/`read_entity` because its lookup KEY is an
email address on that excluded surface. That is a separate mechanism from
the plain `do_not_email:` frontmatter field a wiki page carries as an
ordinary field, which `athenaeum.enumeration` reads and predicates on like
any other field (`current_company`, `warm_score`, ...). The latter was gated
behind `enumerate_entities`'s `with_pii=True` since this module's own
introduction (athenaeum#965 AC amendment 1) until athenaeum#1122, on the
theory that anything email-suppression-shaped needed the same guard as the
excluded-surface record; the operator ruled that
theory wrong — the frontmatter boolean (and its `_reason`/`_date`
companions) has no excluded-surface join and no durable identifier value,
so `enumerate_entities` no longer gates it. This document's
`with_pii=True`, everywhere it appears above and below, refers only to the
excluded-surface record join and is unaffected by that change; see
`athenaeum/enumeration.py`'s "PII-gated fields" docstring section for the
frontmatter-field gate's own rationale.

## 1. The authorized-reader model

`pii: excluded` in `athenaeum.yaml`'s `storage.mapping` keeps a class of data
— contact addresses, or whatever else an operator routes off-corpus — OUT OF
THE COMPILED CORPUS. It answers the question "does this data get embedded,
indexed, and injected into an arbitrary agent prompt by `recall`," and the
answer for an excluded class is no. It has never answered a second question —
"is this data reachable by anyone, ever" — and reading it that way is the
mistake this section exists to correct.

The operator principle is that the restriction is on WRITING, not reading. An
excluded surface is one more piece of the store, and the store has exactly one
egress seam (`docs/one-way-in-one-way-out.md` §3): every read leaves through
it, corpus or excluded, and a caller with a legitimate reason to read excluded
facts is an AUTHORIZED reader, not an intruder. `maecenas#95`'s inert
suppression gate, `maecenas#97`'s "never address a contact with no person
record," and `maecenas#43`'s "stop globbing the wiki" are exactly this
population: three consumers with a real need to know deliverability,
do-not-email state, and provenance for addresses they hold. Before athenaeum#851
that need had no API to meet it — `DO_NOT_EMAIL_FIELD` existed on live records
and was, per its own docstring, "absent from the API surface entirely," so an
authorized reader's only path to it was reading the store's files directly.
That is precisely the failure mode `docs/one-way-in-one-way-out.md` §3 calls a
defect regardless of who is reading or how correct the answer is: a caller who
globs the excluded surface because there is no other way in is not making an
authorization error, athenaeum is making an API-completeness error. The right
fix is for the interface to serve the authorized reader, not for the reader to
route around it.

That framing carries a second, deliberately future-facing justification:
**assume athenaeum's data stores are encrypted, and you have to go through
athenaeum.** They are not encrypted today — both the corpus and the excluded
surfaces are plaintext on a single host (`docs/one-way-in-one-way-out.md` §4
says so explicitly) — but the storage-adapter layer treats the physical
backend as pluggable specifically so a deployment CAN back an excluded surface
with encrypted storage, a database, or a synced filesystem with no caller
change (`docs/north-star.md` §2.11). A consumer that globs plaintext files on
disk works today and breaks the instant that assumption becomes literal,
silently, for every caller who took the shortcut. A consumer that calls
`read_identifier_facts` never notices the day the backend changes underneath
it, because it was never touching the backend.

None of this widens what a search can surface. `with_pii` is a strictly
render-layer join, not a search predicate: `_excluded_block_for_hit` in
`src/athenaeum/mcp_server.py` runs only for a hit that has already survived
audience scoping (the fail-closed read predicate, athenaeum#312/#538) and the
`recallable` drop, and it is skipped entirely — costing zero scans — when the
flag is unset. The `mcp_server` module docstring states the ordering
explicitly: "`recall`'s `with_pii` join runs strictly AFTER that predicate, so
it can never be used to probe whether a record exists behind a page the caller
may not read." An authorized reader gets more FACTS about a hit already in
scope; they never get a wider candidate set than an unauthorized-for-excluded
caller would see from the same query.

## 2. The representation trap

A hard bounce is recorded on the excluded surface as a valid-time CLOSE —
a `valid_until` written onto the identifier, by `mark_bounced` (via
`_merge_identifier_validity` for a person record listing several addresses,
or directly on a slug-keyed record's top-level fields otherwise) — reusing
athenaeum#308's existing claim-validity mechanism. It is deliberately NOT a new
`bounced:` enum field. `pii.py`'s module docstring (point 5) explains why:
"deliverable until the observed date" is exactly what a valid-time close
already expresses, so a hard bounce gets no second, parallel status
representation to drift from the first.

The consequence a verifier needs to know before they go looking: `grep
'^bounced:'` over the excluded contacts surface returns 0 even after
`mark_bounced` succeeded completely and correctly. There is no `bounced:` key
to find, because the fact was never written as one. `IdentifierValidity`'s own
docstring names this directly — "**This type is the answer to the
representation trap**" — and records that it has "already misled one
verification lane, observed during maecenas#73's verification."

**The canonical statement of this trap is
[`docs/deprecated-email-tracking.md` § "How to verify a mark — and the two
greps that lie"](deprecated-email-tracking.md#how-to-verify-a-mark--and-the-two-greps-that-lie-athenaeum850)**,
and a verifier should read it rather than this section. It is canonical
because it covers BOTH failing greps, and the one above is only the first.
The mirror-image error is more dangerous because it looks corroborating:
grepping the **wiki** surface for `bounced:` is a different surface entirely
(written by a producer outside athenaeum — see
[`docs/bounce-surface-convergence.md`](bounce-surface-convergence.md)), where
the field genuinely exists and consumers genuinely read it. The maecenas#73
lane reported that grep returning 0 and concluded there were no bounce marks
in the corpus at all; re-run against the same store on the same day it
returned **189**. Confirmed still 189 on the live store on 2026-08-16, during
athenaeum#851's implementation. Nothing in this document makes a grep-based
check reliable; the point of `validity_for_value` is that you do not need one.

`IdentifierValidity` (via `validity_for_value`) and the `validity` map on
`assemble_excluded_validity` / `EntityRead` exist precisely so a caller never
has to know the encoding to ask the question. A caller reads `closed` (is this
value closed as of the date I asked about), `valid_until` (when),
`reason` (why — the SMTP diagnostic for a bounce), `source` (who says so), and
`recorded` (does the store hold a validity entry for this value at all,
independent of whether it is closed). Which frontmatter key spells that out —
`valid_until` versus a per-identifier entry in `IDENTIFIER_VALIDITY_FIELD`
versus a slug-keyed record's own top-level close — is athenaeum's private
business, and changing it later is a non-event for every caller that went
through `validity_for_value` instead of grepping for a field name.

## 3. The consumer-side eligibility pattern, documented once

Three downstream consumers need the same underlying facts and would, without
a shared pattern, each grow their own reading of them: `maecenas#95`'s inert
suppression gate, `maecenas#97`'s "never address a contact with no person
record," and `maecenas#43`'s "stop globbing the wiki." Three divergent
readings of identical facts is exactly the kind of fork
`docs/one-way-in-one-way-out.md` warns against for the read seam itself
(athenaeum#888 is consolidating a different such fork) — so this section writes
the composition pattern once, here, rather than let each consumer discover it
independently.

The shape: call `read_identifier_facts` with the batch of addresses, and for
each `IdentifierFacts` returned, compose your OWN eligibility decision from
`known`, `do_not_email.marked`, `validity.closed`, and
`classification.outreach_eligible` / `usage_class`. Athenaeum supplies none of
this pre-composed — see §4's non-goal.

```python
from athenaeum.pii import (
    ExcludedSurfaceUnavailable,
    read_identifier_facts,
)

def eligible_for_campaign(knowledge_root, config, addresses):
    """maecenas#95-shaped: which addresses may this campaign contact."""
    try:
        facts_by_address = dict(
            read_identifier_facts(knowledge_root, config, addresses)
        )
    except ExcludedSurfaceUnavailable:
        # Fail closed at the CALLER too: an unreadable surface means "we do
        # not know," never "nothing is suppressed." Abort the send, do not
        # silently treat every address as eligible.
        raise

    decisions = {}
    for address, facts in facts_by_address.items():
        if not facts.known:
            # A stranger is NOT the same as someone cleared to contact.
            # This branch exists precisely so `known=False` cannot be
            # read as "safe to email" by omission.
            decisions[address] = False
            continue

        if facts.do_not_email.marked:
            decisions[address] = False
            continue

        if facts.validity is not None and facts.validity.closed:
            decisions[address] = False
            continue

        classification = facts.classification
        decisions[address] = bool(
            classification is not None and classification.outreach_eligible
        )

    return decisions
```

Three things in this example are load-bearing, not incidental style:

- **`known=False` is its own branch, and it resolves to "not eligible," never
  falls through to a default of "eligible."** `IdentifierFacts.known`'s own
  docstring calls the opposite reading — inferring a stranger from an absence
  — "the exact conflation athenaeum#851 (and `maecenas#97`, which joins on it)
  exists to make impossible." Any consumer that skips this branch and treats
  a `known=False` result the same as a clean `known=True` result with no
  marks has silently reintroduced that conflation.
- **`ExcludedSurfaceUnavailable` is caught and re-raised (or otherwise
  escalated), never swallowed into "treat everyone as eligible."** §4 below is
  the caller-side half of athenaeum's fail-closed contract: athenaeum raises so
  the exception reaches the caller; the caller must not then absorb it into a
  default that undoes the point of raising it.
- **Deliverability (`validity.closed`) and the operator mark
  (`do_not_email.marked`) are checked separately, not folded into one boolean
  by athenaeum.** They are different facts with different provenance, and a
  consumer that needs one but not the other — `maecenas#43`'s "stop globbing
  the wiki" cares about identity and existence more than deliverability, for
  instance — reads only the field it needs rather than being handed a
  pre-mixed answer.

## 4. Fail-closed

`read_identifier_facts` raises `ExcludedSurfaceUnavailable` — via
`_require_readable_surface`, which probes with an actual directory listing
rather than only `Path.is_dir()`, so a mount point that exists but cannot be
listed (permissions, an unmounted volume, a decryption layer not yet up) is
caught too — when the excluded surface cannot be read. It does not fall back
to yielding `known=False` for every identifier in the batch.

The asymmetry that justifies raising rather than degrading gracefully:
**a false skip is recoverable by a human; a false send is not.** An
unreachable surface reported as "nothing recorded" is indistinguishable from
a genuinely clean store where nobody is suppressed, and a caller that cannot
tell the two apart will act on the second reading — sending to an address
that was, in fact, marked do-not-email or closed, because the read that
would have said so never actually happened. `ExcludedSurfaceUnavailable`'s
own docstring states this exactly: "a store that cannot be reached returning
an empty result... a consumer reasonably reads as 'nothing suppressed' and
acts on by sending."

This is deliberately NOT the posture `iter_contact_records` takes. That
function returns `[]` for a missing root and does not raise — and that is
correct for its callers, not an inconsistency to reconcile. `mark_bounced`
resolves through `iter_contact_records` (by way of `resolve_contact_record`)
and, when resolution finds nothing, MINTS the first record on a surface that
may not exist yet — `default_bounce_record_path` and the write that follows
are exactly how a fresh excluded surface gets its first file. A write path
that refused to start against an empty store could never bootstrap one.
Reading and writing want opposite defaults on the same missing-root case:
reading must treat "nothing there" as "we don't know, refuse to guess" while
writing must treat it as "nothing there yet, create it." That is why the
fail-closed contract lives on the read entry point (`read_identifier_facts`,
via `_require_readable_surface`) rather than being pushed down into the
shared `iter_contact_records` scan both paths call — pushing it down would
force one of the two callers to work around a default that is wrong for it.

## 5. Non-goal: no eligibility predicate ships in athenaeum

athenaeum#851 does not, and the next lane building on it should not, ship a
`suppression_state()` / `may_email()` / `is_eligible()` function. "May I email
this person" is not one fact — it folds deliverability (`validity.closed`),
an operator's do-not-email mark (`do_not_email.marked`), provenance
(`classification`), and campaign-specific policy the store has no way to know
about into a single boolean. That boolean is an ACTION decision, and it
belongs to the caller who is about to take the action, not to the memory
layer that stored the facts the decision is made from.

Two concrete reasons this was rejected rather than merely deferred:

1. **It would move policy into storage.** A `may_email()` predicate baked
   into `pii.py` would encode one consumer's campaign policy (which usage
   classes count, whether a closed-but-old bounce still blocks, how a
   `maecenas#97`-style "no person record" case is treated) as though it were a
   fact about the address, when it is really a choice about what one consumer
   is willing to do. `maecenas#95`, `maecenas#97`, and `maecenas#43` do not
   share one policy — that is exactly why §3 documents composition instead of
   a single answer function.
2. **It would fork the read seam athenaeum#888 is consolidating.** Adding a
   second, verdict-shaped read entry point alongside the facts-shaped ones
   this document describes reopens the same "N callers, N paths" problem
   `docs/one-way-in-one-way-out.md` §1 exists to rule out, at the exact moment
   athenaeum#888 is working to remove an existing fork rather than add a new
   one.

The originally-filed `suppression_state()` API for athenaeum#851 was CANCELLED
for this reason — see athenaeum#851's decision comment, and this document as
its permanent record, so a future implementer who rediscovers the appeal of
"just give me one boolean" finds the reasoning here before re-filing it.

One existing function is not a counterexample, and it is worth being
explicit about why: `is_outreach_eligible(meta, identifier)` already exists
in `pii.py` (issue athenaeum#866) and returns a bool. It is not the rejected
predicate. Its own docstring says so directly — it "reports a single value's
usage class and deliberately does NOT consult bounce state: 'may we initiate
contact with this address' and 'is this address still deliverable' are
separate questions with separate predicates." `is_outreach_eligible` answers
exactly one narrow, storage-provenance question (was this value obtained in a
way that permits initiating contact at all, as opposed to being harvested by
a data vendor) and refuses to answer the broader "may I send" question this
section is about — a caller still has to combine it with
`validity_for_value` / `is_bounced_identifier` and `do_not_email_state`
itself, which is precisely the composition §3 documents. It is scoped
narrowly enough to be a fact, not a verdict, and that is what keeps it inside
the boundary this section draws rather than outside it.
