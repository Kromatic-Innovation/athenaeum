# PII classification policy

Issue athenaeum#689. This document is the **policy** — the domain judgment
a classifier (human or agent) applies to decide whether a PII-shaped token
is actually contact data. It is deliberately *not* a pipeline: the schema
it feeds is `athenaeum.pii_classification_decision` (AC2), and the verdicts
it produces are recorded through the existing verdict ledger (AC3/AC4,
`athenaeum.pii_verdicts`). Nothing here wires a classifier into
`athenaeum run`; see those modules' docstrings for what is and is not
wired.

## Order: deterministic first

Run this policy's reasoning step **only on what survives the deterministic
exclusions** (issue athenaeum#720's regex-shape rejects: issue-number
lists, year ranges, dotted dates, and `athenaeum.pii.is_service_address`'s
known non-contact shapes). Escalating a value a normalization would have
rejected for free costs a model call to answer a question that needed
none. The residue that reaches this policy should be small; the five
classes below describe what that residue typically contains, generically
(this is a public repository — no real values are used as examples here).

## The five classes

A PII-shaped token (an email-like or phone-like string) falls into one of
five classes. The first four are judged **not PII**; the fifth is **PII**.

1. **A non-contact alias that merely has email syntax.** An SSH host alias
   of the form `user@host` used in a git remote URL, config file, or
   command example. It is a credential/routing string, not a person's
   contact address — nobody receives mail at it.
2. **A calendar or group identifier.** An address at a calendar-service
   domain (e.g. a `group.calendar.google.com`-shaped domain) that routes
   to a calendar resource, not a mailbox a person reads. Already partly
   covered by `SERVICE_ADDRESS_DOMAINS`; this class exists for the
   residue that slips past that list under a near-miss domain.
3. **A page that exists to hold addresses.** A page whose stated purpose
   (frontmatter `type`, title, or opening sentence) is to be the record of
   a set of addresses — a self-record, a distribution-list manifest, a
   "known addresses" index. The page's content is not a *finding*; it is
   the page doing its job. The page-purpose rule below governs this class.
4. **A test or role account.** An address whose local part or domain
   marks it as non-personal by construction — a disposable/throwaway
   address created to exercise a pipeline, or a shared role mailbox
   (`noreply@`, `donotreply@`, a scratch/test prefix tied to no individual).
5. **A genuine personal address.** An address that routes to a real
   person's mailbox, used as their contact data. This is the only class
   the policy marks **is PII**, and the only class migration
   (`athenaeum storage migrate-pii`) should ever act on.

## The page-purpose rule

Classes 1–4 share one shape: the value is not incidental content a page
*mentions*, it is content a page *exists to hold* (classes 1, 2, 4) or
*exists to hold on behalf of its stated purpose* (class 3). The rule:

> Before judging a value, read what the page is *for* — its frontmatter
> `type`, its title, and its first sentence. If the page's own stated
> purpose is to be the record of this value (or a set of values like it),
> the value is not a finding; it is the page functioning as designed.

This is what separates row 3 (`auto-emails-self-tristan.md`-shaped: a page
that exists to hold a set of operator addresses) from row 5 (a page that
merely *mentions* a client's address in passing prose). Both may contain
the identical-looking token. Only the second is a finding.

The page-purpose rule does not override class 5. A page that exists to
hold genuine client contact data is still holding PII — "this page's
purpose is to record addresses" answers *whether a value here is
expected*, not *whether the value is a person's real contact information*.
A self-record of deliberately-retained operator addresses (class 3) is
exempt because the retained values are the operator's own and the
retention is deliberate and declared; a client roster is not exempt on
page-purpose grounds alone.

## Worked examples (synthetic)

| Class | Synthetic value | Context | Verdict |
|---|---|---|---|
| 1 | `git@github.com` | Appears in a cloned-repo remote URL inside a setup doc | not PII — SSH host alias |
| 2 | `abc123@group.calendar.google.com`-shaped | Appears as a calendar resource id in an automation config page | not PII — calendar id |
| 3 | `person@example.com` | Appears on a page whose frontmatter `type` and opening sentence declare it a self-record of operator addresses | not PII — page exists to hold it |
| 4 | `test-account-ci@example.com` | Appears in a changelog entry describing a pipeline test run | not PII — test account |
| 5 | `firstname.lastname@examplecorp.test` | Appears in prose describing a client interaction, on a page that is not a declared address-holding record | **is PII** — migrate |

These are illustrative synthetic values, not corpus content. Applying this
policy against the real residue (the ~70 addresses enumerated by issue
athenaeum#691) is a later, operator-run step (AC5–AC7 of athenaeum#689),
not part of this document or this lane's code.

## Over-restoring is worse than under-restoring

**A wrong "not PII" verdict is a PII regression — the one failure mode
worse than the original bug.** The corpus-wide email axis measured at
99.6% correct before this policy existed; the entire point of adding a
contextual judgment on top of a regex is to fix the small residue without
reintroducing a leak on the other side. Concretely:

- An **under-restoring** error (a genuinely non-PII value wrongly left
  flagged, or a "not PII" verdict withheld when it should have been given)
  costs a human a few extra seconds reviewing a false positive. It is
  cheap, visible, and self-correcting on the next review pass.
- An **over-restoring** error (a genuinely personal address wrongly
  classified as class 1–4, so it is marked "not PII" and never migrated)
  is a silent, sticky privacy failure: the sticky-verdict mechanism this
  policy feeds (AC3/AC4) means a wrong "not PII" verdict does not merely
  fail to fix the leak once, it **actively suppresses the detector from
  ever re-flagging that value again.**

When a classifier (human or agent) is not confident which of the five
classes a value belongs to, the conservative answer is **class 5 (is
PII)**, not one of classes 1–4. An over-cautious "is PII" verdict costs a
redundant migration of an already-harmless value; an over-confident "not
PII" verdict costs a permanent blind spot. The asymmetry is deliberate and
is encoded structurally, not just stated here: see
`athenaeum.pii_verdicts`'s module docstring for why an "is PII" verdict is
treated as erasure-class content and is refused from ever landing
plaintext in the in-git ledger, while a "not PII" verdict is not.

## What this policy does not do

- It does not build a ledger (athenaeum#712 owns the verdict ledger this
  policy's verdicts are recorded through).
- It does not build a decision queue, batching, or triage (athenaeum#717
  owns those; `athenaeum.pii_classification_decision`, AC2, defines the
  schema athenaeum#717 will adopt, dark and unwired, per the 2026-09-03 scoping
  note on athenaeum#689).
- It does not change `lint-pii`'s regex exclusions (athenaeum#720 owns
  those).
- It does not run against the live residue (AC5–AC7 of athenaeum#689 are
  an operator host step, out of scope for this lane).
