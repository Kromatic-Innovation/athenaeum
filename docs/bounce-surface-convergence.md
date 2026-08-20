<!-- SPDX-License-Identifier: Apache-2.0 -->

# Design: converging the pii bounce mark with the wiki `bounced:` field (athenaeum#852)

**Status: SHIPPED.** This document describes behaviour that exists — it landed
with the implementing change (`src/athenaeum/bounce_join.py`,
`tests/test_bounce_join.py`), not ahead of it. It is a sibling of
[`deprecated-email-tracking.md`](deprecated-email-tracking.md), which settles
where a bounce fact lives on the contacts surface (Q1) and how it gets there
(Q3); this document settles how that fact relates to the field consumers
actually read.

Read [How to verify a mark — and the two greps that
lie](deprecated-email-tracking.md#how-to-verify-a-mark--and-the-two-greps-that-lie-athenaeum850)
first if you are checking whether a bounce was recorded. Nothing below makes a
grep-based check reliable.

## The two surfaces

| | Contacts surface (`pii`) | Wiki `bounced:` frontmatter |
|---|---|---|
| **What it records** | A valid-time close: "this address was observed non-deliverable as of *date*" | A bounce verdict from any of several producers, as free text |
| **Key** | Email identifier | Page `uid` |
| **Written by** | `pii.mark_bounced`, via the Tier-0 gate (athenaeum#765) | A producer **outside** athenaeum — see `deprecated-email-tracking.md` Q3 and maecenas#42 |
| **Evidence admitted** | RFC 3463 `5.x.x` permanent-failure codes **only** | A union: list-verification verdicts, DSN-derived replies, bare SMTP codes, transients, CRM markers |
| **Read by** | `pii.is_bounced_identifier` | Downstream consumers at segment time |

**Wiki frontmatter is consumer truth (P6).** That is the architectural given
this design works within, not a conclusion it reaches.

## The join key, and the whole chain

Neither surface could see the other, because they shared no key: a wiki page
carries a `uid` and no address; the athenaeum#765 slug-keyed bounce record
carried an address and no `uid`. The **excluded person record** — written by
the athenaeum#427/#437 migrator — is the only object holding both halves.

That is why athenaeum#850 is a hard dependency rather than a tidiness fix: it
is what puts the mark *onto* that record, and so what creates the key at all.

```
identifier                                   an email address
  ->  person record on the contacts surface  lists it under `emails:` /
                                             `former_emails:` / `alt_emails:`
                                             (pii.resolve_contact_record)
  ->  that record's `uid:`                   written by the athenaeum#427/#437 migrator
  ->  the wiki page carrying the same `uid:` what a consumer holds
                                             (bounce_join.wiki_page_for_uid)
```

`bounce_join.join_identifier` walks it forwards; `bounce_join.deliverability_for_page`
walks it backwards from the page a consumer is holding. Both stop at the first
missing link and report how far they got (`BounceJoin.reached`) rather than
raising — a broken chain is the ordinary case on a real store, not an error.
A person record with no `uid`, or a `uid` with no wiki page, is expected.

## The evidence-class asymmetry

**The two surfaces do not hold the same kind of evidence, and convergence
cannot be an equality check.** The pii mark exists only where
`pii.detect_hard_bounce_fact` matched, which requires a `5.x.x` code. The wiki
field is strictly broader: it also carries list-verification verdicts
(`MailboxDoesNotExist`, `DomainHasNullMx`, `SmtpConnectionTimeout`),
DSN-derived entries whose reply is a bare SMTP `550`/`552` with no enhanced
code, transient `4.x` observations, and CRM-notes markers.

Three consequences, all of them load-bearing:

1. **A difference between the surfaces is normal, not a defect.** An entry on
   the wiki surface with no pii mark is the expected state for every evidence
   class the Tier-0 gate does not admit. A report over the difference
   (athenaeum#853) must be read with that in mind.
2. **Convergence cannot be a replay.** Feeding wiki entries back through raw
   intake would leave every non-`5.x.x` entry failing the Tier-0 gate and
   falling through to the reasoning tiers — compiling an address and a
   diagnostic into the corpus as free-text memories. This is a join on a
   shared key, never a re-recognition pass.
3. **Nothing may promote the broader class to a hard bounce.**
   `bounce_join.Deliverability` therefore reports the two halves **separately**
   and never collapses them into one boolean:

   - `hard_bounced` — the pii mark. `5.x.x` only.
   - `wiki_verdict` — whatever the wiki field says, **verbatim and
     unclassified**.

   A consumer that wants "do not send" may act on either. A consumer that wants
   "this address is permanently dead" may act only on `hard_bounced`.
   Collapsing them here would make that distinction unavailable to every
   consumer — and would promote a transient to a hard bounce. Note that even a
   wiki verdict that *would* match the detector is not promoted: the wiki value
   is never re-recognized, only carried.

## Direction: the mark reaches the consumer at READ time

P6 makes wiki frontmatter consumer truth, so the question this design answers
is *"what does a consumer holding a wiki page know about deliverability?"* —
and the pii mark reaches that answer through the chain above, at read time,
via `deliverability_for_page`.

**athenaeum does not write `bounced:` onto wiki pages.** That is a deliberate
choice between the two directions athenaeum#852 allowed ("feeds **or**
reconciles with"), and the alternative is worth stating:

- *Considered and not chosen:* propagating each pii mark into the page's
  `bounced:` field, so consumers need no join. Rejected on three grounds.
  (a) It contradicts the standing `wiki-contacts-no-email` principle and
  `deprecated-email-tracking.md`'s Q1, which states explicitly that this
  design does **not** add a `bounced:` flag to wiki pages. (b) It would make
  athenaeum a second writer of a field an external producer already writes,
  with no arbitration between them — while maecenas#42 is in flight to stop
  that producer writing the wiki directly. (c) A write direction makes the
  surfaces converge by *copying*, which loses the evidence-class distinction
  the section above exists to preserve.
- *Chosen:* a read-time join. The surfaces stay separately owned, each keeps
  its own evidence class, and a consumer gets both halves plus the key that
  relates them.

The cost of this choice is explicit: a consumer must call
`deliverability_for_page` rather than read one field. That is the price of not
collapsing two evidence classes into one, and of not adding a second writer to
a contested field.

## The re-report contract

`pii.mark_bounced` is an idempotent upsert. Its contract, unchanged by this
design and pinned by `tests/test_bounce_join.py::TestReReportContract`:

- **Same `(address, observed_at, diagnostic, source)`** — the merged
  frontmatter is byte-identical to what is on disk, so nothing is written and
  `changed=False`. Re-reporting the identical fact is a true no-op, never a
  duplicate mark.
- **Different `observed_at`** — the same record, and on a person record the
  same list entry, is updated **in place**: last-writer-wins. The close moves
  to the new date; no second entry appears.

**Last-writer-wins is intended for a re-bounce**, and the reasoning is worth
recording because it is not self-evident. A `valid_until` close is an
assertion about *when deliverability lapsed*, and the latest observation is
the best available estimate of that — an address that bounced in August and
bounced again in September has a September close, not two closes. The
alternative, *first-writer-wins* (keep the earliest observation, treat later
ones as confirmations), was not chosen: it would make the close unmovable
after the first report, so a corrected or re-dated observation could never
take effect, and it would encode "we first noticed" rather than "as of when we
believe it lapsed". The full observation history, where one is needed, belongs
in the append-only observation ledger — not in a field whose whole job is to
carry one upper bound.

Note what this does **not** do: it never *reopens* an address. A later
`observed_at` moves the close later; nothing in this path removes a mark or
asserts an address is deliverable again.

## Provenance of the figures quoted in the issues

athenaeum#849, athenaeum#850 and athenaeum#852 quote counts measured against a
private store on **2026-08-12** — the 189 wiki entries, the 13 of those
carrying a `5.x.x` code, the single contacts-surface mark. Those are
**as-of-that-date observations of one store**, not invariants, expected
values, or test assertions. Nothing in this design or its tests depends on
them, and no code here re-derives or asserts any of them: the design holds for
any store, including an empty one. They appear in the issues as motivation for
why a single-surface count is uninformative, and they are reproduced here only
to state that status explicitly.

## Scope

Not part of this design: backfilling historical wiki entries into anything
(that is `voltaire#123` graph work), widening `detect_hard_bounce_fact` past
`5.x.x`, changing the detector's classification logic, any destructive
operation, and any new ledger file. Reporting the divergence between the two
surfaces is athenaeum#853, which takes its join key and surface definitions
from this document rather than re-deriving them.

## Reporting the divergence

`athenaeum bounce-divergence --path <store-root>` reports the difference
between the two surfaces in both directions, over the `uid` join key defined
above (athenaeum#853). It is read-only, takes the store root as a parameter,
and its output is safe to paste into a public issue — aggregate counts and
opaque handles only.

Read its output with the asymmetry above in mind: **an entry on the wiki
surface with no pii mark is the expected state** for every evidence class the
Tier-0 gate does not admit, so a non-zero divergence is not by itself a
defect. What the report defends is a *moving* number.

**`athenaeum bounce-divergence` never fails on this — that is deliberate for
this command, and it is exactly the property athenaeum#963 generalizes past.**
`athenaeum surface-divergence --field bounced` (issue athenaeum#963, see
[configuration.md](configuration.md#surface-divergence-guard-athenaeum963))
reports the identical two surfaces and the identical numbers, but exits
non-zero when the asymmetry above is violated in the direction it does NOT
excuse: a pii mark with no wiki entry (`marked_not_on_wiki`). A wiki-only
entry (`on_wiki_not_marked`) stays tolerated, unchanged, for the reason
stated above. Use `bounce-divergence` for interactive inspection;
`surface-divergence --field bounced` is the entry point an unattended
caller should use.
