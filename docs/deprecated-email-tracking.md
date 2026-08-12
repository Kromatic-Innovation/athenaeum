<!-- SPDX-License-Identifier: Apache-2.0 -->

# Design: tracking deprecated / bounced email identifiers (athenaeum#565)

**Status: RATIFIED, then corrected 2026-08-05 (athenaeum#768) against the live
code, and reconciled again the same day against the SHIPPED athenaeum#765
implementation (PR athenaeum#826, merged).** This document was the
deliverable of issue athenaeum#565 (PR athenaeum#652). It originally answered
five design questions; three of the five needed correction after
ratification, and Q3's contract details needed a further pass once
athenaeum#765 actually landed and the shape of the shipped code diverged
from the plan:

- **Q1 and Q2 are superseded** by the v6 memory model direction (athenaeum#712,
  athenaeum#709) — marked in place below, not deleted, per athenaeum#768. The
  shipped athenaeum#765 mechanism does not wait on either: it reuses
  athenaeum#308's existing `valid_until` claim-validity close directly (see
  below), so there is no interim ledger of any kind.
- **Q3 named the wrong actor.** A 2026-08-05 code review established that
  **Voltaire**, not maecenas, detects and reports the bounce fact; see
  "The actor is wrong" below and the corrected Q3.
- **Q3 and Q5 specified a bespoke `type: email_bounce` intake schema.** That
  pattern was cut as not generalizable; the reporter uses the existing
  free-text `remember()` call instead, and Q3 below now describes the exact
  frontmatter/`sources` shape the shipped `librarian.tier0_bounce_mark`
  requires to recognize it.

The implementation issue is athenaeum#765, narrowed to exactly what the
operator asked for: *"The librarian should process it in the wiki by making
sure that email in our PII file is marked as bounced. That's all."* It has
since **shipped** (PR athenaeum#826, merged 2026-08-07) — this document has
been checked against `src/athenaeum/pii.py` (`detect_hard_bounce_fact`,
`mark_bounced`, `is_bounced`) and `src/athenaeum/librarian.py`
(`tier0_bounce_mark`) on `develop` as of that merge, not just the plan.

## The actor is wrong (corrected 2026-08-05, athenaeum#768)

The original text below said maecenas reports the bounce fact into raw
intake. That is wrong, and it contradicted this document's own framing of
maecenas as "a campaign tool" that shouldn't need to know about the librarian's
bookkeeping. Verified against the live code, 2026-08-05:

- **`voltaire/src/tiers/bounce.ts` is the detector.** `detectBounce` gates on
  strong DSN signals (mailer-daemon/postmaster sender and a
  `multipart/report; report-type=delivery-status` reporting `Action: failed`)
  and on status class — `5.x` hard, `4.x` declined to `temp-bounce.ts`.
- **Voltaire invokes `handle_bounce.py`**, which removes the address from
  Google Contacts. That script lives in `maecenas/rsb_campaign/`, but
  maecenas only **hosts** it — it does not run the detection logic.
- **maecenas consumes** suppression state at send time. It neither detects
  nor reports the bounce.

Corrected chain:

> **Voltaire detects → updates Google Contacts → reports the bounce fact to
> athenaeum as ordinary raw intake → the librarian marks the address bounced
> on the PII surface.**

**Outcome-vocabulary gap (unresolved, noted here rather than papered over):**
Voltaire distinguishes **three** outcomes — hard bounce, potentially-stale
after repeated `4.x` past a threshold (voltaire#81), and fine — while Q1
below proposed a **two**-valued `status` (`bounced` / `deprecated`). This
document does not resolve that gap; see athenaeum#765's open question and
voltaire#81 for the source of the third state.

## The operator's model (given, not re-litigated)

- **Google Contacts is authoritative** for "what is this person's *current*
  address." Athenaeum does not compete with it as a source of truth.
- **Email addresses are permanent identifiers** in athenaeum: they persist so
  Gmail history stays findable *even after the person's address changes*. That
  historical linkage is the whole point — an address cannot simply be dropped
  when it stops working.
- Those two facts pull in opposite directions. The resolution is **deprecation**:
  the address stays discoverable as a historical identifier, marked
  no-longer-deliverable.
- **maecenas should not need to know any of this.** It is a campaign tool; it
  neither detects nor reports the bounce, and knows nothing about how the
  librarian records it. **Voltaire** detects the bounce and reports the fact
  into raw intake (corrected 2026-08-05, athenaeum#768 — see "The actor is
  wrong" above); maecenas only consumes the resulting suppression state at
  send time, and hosts the `handle_bounce.py` script Voltaire invokes
  (maecenas#42, maecenas#41).

The recommendation below implements this model; it does not reopen it.

---

## Q1 — Where does deprecation live?

> **SUPERSEDED 2026-08-05 (athenaeum#768), and reconciled again once
> athenaeum#765 shipped (PR athenaeum#826).** The `_deprecations.jsonl`
> ledger proposed below was never built. athenaeum#765 (the narrowed
> implementation issue for this design) cut it on 2026-08-05: athenaeum
> already has `_observations.jsonl` (append-only, with a supersession fold),
> and a second, feature-specific ledger would collide with **athenaeum#712**'s
> planned v6 verdict ledger. athenaeum#689 was re-scoped on 2026-08-02 by
> explicit operator decision — *"one ledger"* — for the same reason. But the
> shipped mechanism does **not** wait on athenaeum#712 either: `pii.mark_bounced`
> encodes the mark as a **valid-time close** — it sets `valid_until` to the
> observed date directly on the identifier's own contact-record frontmatter,
> reusing athenaeum#308's existing `valid_until_expired` claim-validity
> predicate (`pii.is_bounced` is the read-side wrapper). No ledger file of
> any kind — new, `_deprecations.jsonl`-shaped, or v6 — is part of the shipped
> mark. The section below is kept for historical context (why a deprecation
> record is not folded into `Supersession`) but its proposed storage
> mechanism does not exist and is not being built.

**In the excluded contact store, as a new append-only ledger record — NOT in
wiki frontmatter.**

The wiki carries a standing principle (`wiki-contacts-no-email`): *email
addresses belong in Google Contacts; wiki entities link to contacts by UID and
do not store email strings directly.* Any design that adds a `bounced:` flag to
a wiki page would contradict that principle. This proposal **does not** add such
a flag and therefore **does not supersede** the principle — it is consistent
with it.

Deprecation is a fact *about an email identifier*, and email identifiers already
live in the **excluded contact surface** (`athenaeum.pii.contacts_surface_root`,
wired via `storage.mapping: {pii: excluded}`). That surface already holds an
append-only ledger pair:

- `_observations.jsonl` — `Observation(obs_id, identifier, person_id,
  observed_at, source_msg_id)`: "this identifier was seen attributed to this
  person, at this time, from this source."
- `_observation_supersessions.jsonl` — `Supersession(retracts, reason, at)`:
  "retract that observation" (never edits/deletes it).

A deprecation belongs in the **same surface, as its own record type** — proposed
sidecar `_deprecations.jsonl` — mirroring the observation ledger's discipline:
append-only, caller-minted id, schema-versioned, never an in-place rewrite.

**Why a new record type rather than reusing `Supersession`:** the two are
semantically distinct and must not be conflated. A *supersession* says an
attribution was wrong or no longer holds (routing changed) — it retracts an
identity claim. A *deprecation* says the address itself no longer delivers *but
remains a valid historical identifier* — the identity claim stands; only
deliverability lapses. Folding deprecation into supersession would make a
bounced address look retracted, defeating the "stays findable" goal.

Proposed record shape (final field set settled at implementation time):

```
Deprecation(
    dep_id,          # caller-minted, like obs_id
    identifier,      # the email address
    status,          # "bounced" | "deprecated"  (bounced = observed hard failure;
                     #   deprecated = operator/marker-asserted no-longer-current)
    reason,          # the bounce diagnostic or a free-text note
    observed_at,     # ISO-8601 when the bounce/deprecation was observed
    source,          # msg-id / campaign ref / "backfill:gcontacts-bio"
    disposition,     # "archived" | "deleted" — the resolved preference (Q2)
)
```

A read over the contact surface returns the identifier **plus** its deprecation
status, so recall and lint can treat a deprecated address as *present but
non-deliverable* rather than either live or gone.

---

## Q2 — Archive vs delete: the configurable preference

> **SUPERSEDED 2026-08-05 (athenaeum#768), and reconciled again once
> athenaeum#765 shipped (PR athenaeum#826).** The configurable
> archive-vs-delete disposition knob proposed below was never built.
> athenaeum#765 cut it on 2026-08-05: a `delete` disposition is a destructive
> operation, and athenaeum#709's definition of done item 4 requires **"zero
> destructive operations without a ledger entry."** The shipped mechanism
> has no disposition knob at all — `pii.mark_bounced` only ever **upserts**
> fields onto the identifier's existing contact record (idempotent; a
> re-report is a byte-for-byte no-op) and never deletes the record or the
> identifier under any configuration, so the outcome is `archive`-only by
> construction, not by a resolved default. The section below is kept for
> historical context (the archive-over-delete reasoning still holds) but the
> knob it proposes does not exist and is not being built.

**Recommended default: `archive` (preserve). Configurable via a resolver knob,
global with an optional per-source override.**

Two dispositions:

- **`archive`** (default) — the identifier record stays in the excluded contact
  store; a `_deprecations.jsonl` record marks it non-deliverable. It remains a
  historical identifier: Gmail search over it still resolves, recall still links
  it to the person. This is the disposition the operator's rationale points at —
  *the identifier's Gmail-search value is the reason the record exists*, so
  destroying it on a bounce throws away exactly what athenaeum is for.
- **`delete`** — the identifier is removed from the excluded contact store
  entirely (a delete record is still written to `_deprecations.jsonl` for
  audit, so "we deleted X on date Y for reason Z" survives even though X does
  not). For addresses an operator wants gone (a genuinely wrong import, a
  privacy request), not merely deprecated.

**Knob**, following the existing `resolve_model` / `resolve_max_tokens`
convention (env > yaml > code default):

```
resolve_bounce_disposition(config)  ->  "archive" | "delete"
    env:  ATHENAEUM_BOUNCE_DISPOSITION
    yaml: librarian.bounce_disposition   (or contacts.bounce_disposition)
    default (code): "archive"
```

**Granularity: global default, optional per-source override.** The common case
is one global preference. A per-source override (`librarian.bounce_disposition_by_source`)
lets an operator, e.g., archive bounces from one campaign source (default)
while deleting bounces from a source known to over-report transient failures.
Per-*person* granularity is deliberately **not** proposed: deprecation is a
property of an address's deliverability, not of a person, and per-person tiers
would invite exactly the case-by-case decisioning the append-only ledger design
avoids. If a single address genuinely needs a one-off disposition, that is an
operator action on that record, not a new config axis.

Default preserves, because reversing a wrongful *delete* is impossible (the
identifier is gone) whereas reversing a wrongful *archive* is trivial (drop the
deprecation record). Preserve is the safe direction.

---

## Q3 — The raw-intake proposal contract

> **Implementable version: [`tier0-bounce-note-contract.md`](tier0-bounce-note-contract.md)
> (athenaeum#854).** This section states the decision in prose. A producer in
> another repository implementing against it should read that document
> instead: it pins every field, where it must appear, what satisfies it, and
> what happens when it does not — and ships `athenaeum bounce-contract`, a
> read-only check answering "would Tier 0 recognize this note?" for a
> candidate note **before** a bulk submission, naming which condition failed.
> The two are kept in agreement by tests, not by convention.

> **CORRECTED 2026-08-05 (athenaeum#768), and corrected again the same day
> once athenaeum#765 shipped (PR athenaeum#826).** Three things were wrong in
> the original text below, replaced entirely rather than annotated in place:
> (1) it named maecenas as the reporter — it is **Voltaire**; see "The actor
> is wrong" above. (2) it specified a dedicated `type: email_bounce` YAML
> schema with a special-cased librarian intake path — cut as not
> generalizable for an OSS knowledge system with many fact types. (3) the
> first-pass correction's own `remember()` example was still wrong once the
> shipped code could be read: it passed the per-claim provenance value
> through `remember()`'s `source` parameter, but that parameter selects the
> `raw/<session>/` subdirectory (the SESSION identifier) — per-claim
> provenance goes through the separate `sources` parameter instead. The
> example below matches `tests/test_bounce_mark.py::TestNormalIntakePath`,
> which exercises the real `remember_write()` entry point, not a
> hand-crafted fixture.

**Voltaire reports the bounce fact into raw intake as ordinary free text,
via the existing `remember()` call. No dedicated schema, no special-cased
librarian intake path — but two specific frontmatter fields on the note
itself gate whether the deterministic fast path recognizes it (see "How
recognition actually works" below).**

Voltaire detects the bounce (`voltaire/src/tiers/bounce.ts`) and invokes
`handle_bounce.py` to update Google Contacts. That script is hosted in
`maecenas/rsb_campaign/` — maecenas#42 tracks fixing it so it stops writing
directly to the wiki (`write_wiki_bounced()`) and instead reports the bounce
fact through this contract. maecenas itself does not detect or report; it
only consumes the resulting suppression state at send time.

**Contract — an ordinary `remember()` call, evidence embedded in the text,
`observed_at` embedded in the note's own frontmatter, provenance via
`sources`:**

```python
remember(
    content=(
        "---\n"
        "observed_at: 2026-07-15\n"
        "---\n\n"
        "person@example.com hard-bounced. "
        "Diagnostic: 550 5.1.1 user unknown."
    ),
    source="voltaire-bounce-relay",              # session dir, NOT provenance
    sources="campaign:spring-2026/msg-abc123",   # per-claim provenance
)
```

Rules of the contract:

1. The reporter emits **only the fact**, as free text with the evidence
   embedded — address, diagnostic, observed date, and provenance. There is
   no `type:` field and no bespoke YAML schema; `content`, `source`, and
   `sources` are the same parameters every other `remember()` caller uses.
   `source` (bare string) picks the `raw/<session>/` landing directory;
   `sources` is the per-claim provenance that lands as the raw file's own
   `source:` frontmatter key.
2. This is **ordinary raw intake**, like every other fact athenaeum records —
   the reporter calls `remember()` and stops. There is no dedicated ledger
   record or disposition to resolve (Q1/Q2 above are superseded —
   athenaeum#765's narrowed scope is "mark the address bounced on the PII
   surface, that's all").
3. There is no required-field list beyond what `remember()` already takes.
   The diagnostic and observed date are conventions for what to embed in
   `content`, not schema fields the librarian parses specially — with one
   caveat: `observed_at` must appear in the note's OWN frontmatter (either
   embedded by the caller, as above, or already present from an earlier
   merge) for the deterministic fast path in point 4 to fire at all; an
   omitted `observed_at` or `source` falls the note straight through to the
   ordinary reasoning tiers, unmarked as a bounce, with no error.
4. **How recognition actually works (added post-athenaeum#765-merge,
   athenaeum#768):**
   this is NOT "the librarian's normal classification pass" in the LLM
   sense — it is a new, LLM-free **Tier 0** branch,
   `librarian.tier0_bounce_mark`, that runs before Tier 1/2/3 in
   `process_one` and short-circuits them entirely on a match (mirroring
   `tier0_handle_upsert`'s shape). It requires, deterministically:
   - the raw file's own frontmatter carries a non-empty `observed_at` **and**
     `source` (both pre-existing generic per-claim fields, athenaeum#424 /
     athenaeum#90 — not new schema); and
   - the body text matches `pii.detect_hard_bounce_fact`: **exactly one**
     email-shaped token, plus an RFC 3463 `5.x.x` (hard-failure) code
     somewhere in the text. A note naming zero or several addresses, or
     carrying only a `4.x` transient code, is left untouched and falls
     through to the ordinary Tier 1/2/3 reasoning path like any other raw
     file — it is not specially declined or logged as a bounce-adjacent
     case. This is also where the "outcome-vocabulary gap" noted above
     actually bites: a `4.x`/potentially-stale note (voltaire#81) gets no
     bounce handling of any kind today, deterministic or otherwise.
   - On a match, `pii.mark_bounced` upserts `identifier`, `pii: true`,
     `bounce_diagnostic`, `observed_at`, `source`, and `valid_until` (set to
     `observed_at`) onto the identifier's own contact-record frontmatter on
     the excluded contacts surface — never a ledger row. `pii.is_bounced`
     (`valid_until_expired` under the hood) is the single read-side
     predicate a consumer calls to tell "present but non-deliverable" apart
     from "never seen."
5. The reporter learns the librarian's outcome, if at all, only through the
   normal intake result surface — **ideally it learns nothing**, per the
   operator's "maecenas shouldn't need to worry about this" (which extends
   to Voltaire and the hosted script it invokes).

**maecenas#42** tracks the fix to the hosted `handle_bounce.py` script
(stop writing directly to the wiki; report via this contract instead). On
ratification of this correction, this section is copied to maecenas#42 so
that issue's driver note stays current.

---

## Q4 — Reconciliation with Google Contacts

> **Note (2026-08-05, athenaeum#768) — principle unaffected, implementation
> issue closed.** None of the corrections in this document touch Q4's
> principle: surfacing only, never auto-correction; athenaeum is never
> authoritative over gcontacts for a current address. Its optional
> implementation issue, **athenaeum#767** (the
> `deprecated-here-but-live-in-gcontacts` lint), was **closed as not planned**
> on 2026-08-05. Two reasons: (1) the lint's premise was diffing the
> `_deprecations.jsonl` ledger against gcontacts, and that ledger was cut from
> athenaeum#765 the same day (see Q1) — there is no longer anything to diff;
> (2) the lint has athenaeum reading Google Contacts directly, the same
> boundary inversion **voltaire#117** exists to fix on the Voltaire side (the
> read belongs where the gcontacts access already lives). The lint was
> `moscow:could` and genuinely optional, so it was closed rather than
> rewritten against a moving target — see the issue for the full review. The
> text below still describes the `_deprecations.jsonl` record for the same
> historical-context reason as Q1/Q2 above; the actual shipped mark (see Q1,
> Q3) is a `valid_until` close on the identifier's own contact-record
> frontmatter, not a ledger — a rebuilt lint would read that field via
> `pii.is_bounced`, not diff a ledger.

**Steady state: athenaeum legitimately holds identifiers Google Contacts has
dropped. That is what "historical identifier" means. No reverse drift-detection
is required by default.**

Once maecenas#41 removes a bounced address from the authoritative Google
Contact, athenaeum still holds that address — now carrying a `_deprecations.jsonl`
record. This is **expected and correct**: the deprecation record *is* the
reconciliation. It answers "why does athenaeum know an address that Google
Contacts no longer lists?" — because it is a durable historical identifier,
marked non-deliverable, kept for Gmail-history findability.

Directional rules:

- **gcontacts drops a deprecated address** → expected; no action. The
  deprecation record already explains the divergence.
- **gcontacts still lists an address athenaeum marked deprecated** → a weak
  signal the bounce may have been transient (or the person re-acquired the
  address). This is worth *surfacing* but not *acting on*: a lint that reports
  "deprecated-here-but-live-in-gcontacts" identifiers gives the operator a
  review queue without athenaeum second-guessing the authoritative source. This
  lint is a **follow-up**, not part of the core mechanism.

Athenaeum never writes back to Google Contacts and never treats its own
deprecation state as authoritative over gcontacts for *current* address — only
for the *historical* fact that the address once bounced.

---

## Q5 — Retroactive population (backfill)

> **CORRECTED 2026-08-05 (athenaeum#768), contract detail refreshed the same
> day once athenaeum#765 shipped (PR athenaeum#826).** The original text
> below emitted the **same** bespoke `type: email_bounce` intake proposal Q3
> used — cut for the same reason (see Q3). It also implicitly located the
> backfill on the containerized design-work side; the gcontacts read is a
> **Voltaire-side** concern, and the backfill issue has moved there:
> **voltaire#117**.

**Yes — the `BOUNCED <date>: <diagnostic>` markers already recorded in Google
Contact biographies are a backfill source. Tracked as voltaire#117.**

Bounces have been recorded in gcontacts bios as `BOUNCED <date>: <diagnostic>`
for some time. A one-time backfill pass parses those markers and reports each
one through the **same** free-text `remember()` contract as a live bounce
(Q3) — `sources: "backfill:gcontacts-bio"` (per-claim provenance; `source`
remains the session-directory selector), `observed_at` embedded in the
note's own frontmatter set to the marker's date, and `content` embedding the
address and diagnostic — so backfill and live bounces flow through one code
path, not two, and both are recognized by the same deterministic
`librarian.tier0_bounce_mark` gate described in Q3 (not an LLM classification
pass). Because raw intake is append-only, re-running the backfill is safe:
`pii.mark_bounced` treats each `remember()` call as an independent, idempotent
upsert, same as any duplicate report.

The backfill needs read access to the gcontacts bios, which Voltaire already
has and this document's scope (athenaeum-side design) does not cross —
**voltaire#117** tracks it.

---

## Consistency with athenaeum#505 and the PII work

This design treats email addresses as **durable historical identifiers**, which
is exactly the frame athenaeum#505 (migrating `name:`-is-an-email pages) and athenaeum#502 already
adopt: an address is preserved and moved to the excluded contact record rather
than discarded. Deprecation is the natural next state on that same record —
"still a durable identifier, now non-deliverable" — so the two land
consistently. It does not cut across the in-flight PII decisions in athenaeum#428 / athenaeum#437;
it adds a status to records those slices already place in the excluded surface.

---

## What ratification unblocks (implementation slices)

> **CORRECTED 2026-08-05 (athenaeum#768), and item 3 flipped to shipped the
> same day once athenaeum#765 merged (PR athenaeum#826).** This list
> originally named maecenas as the Q3 reporter in items 3-4 and proposed the
> now-superseded Q1/Q2 mechanism in items 1-2. Updated to the actual
> disposition of each slice, below, cross-linked to the issue building it (or
> the issue that closed it, and why).

1. **Superseded** — `_deprecations.jsonl` record type. Not built; the shipped
   mark instead reuses athenaeum#308's `valid_until` close directly (Q1) —
   it does not wait on **athenaeum#712**'s (still separate) v6 verdict ledger.
2. **Superseded** — `resolve_bounce_disposition` knob. Not being built; ruled
   out by **athenaeum#709**'s definition-of-done item 4, "zero destructive
   operations without a ledger entry" (Q2).
3. **Shipped** — the librarian marks the address bounced on the PII surface
   from an ordinary `remember()` call, no dedicated intake path. Built by
   **athenaeum#765**, merged 2026-08-07 in **athenaeum#826** (Q3).
4. **In progress** — the corrected Q3 contract, communicated to
   **maecenas#42**, which tracks fixing the hosted `handle_bounce.py` script
   to stop writing directly to the wiki.
5. **In progress** — backfill pass from gcontacts `BOUNCED` bio markers.
   Moved to the Voltaire side: **voltaire#117** (Q5).
6. **Closed, not planned** — the `deprecated-here-but-live-in-gcontacts`
   reconciliation lint. **athenaeum#767**, closed 2026-08-05: its premise
   (diffing `_deprecations.jsonl`) was cut, and the gcontacts read it
   proposed belongs on the Voltaire side per the same boundary voltaire#117
   exists to fix (Q4).

## Open questions for the ratifying reviewer

> **Note (2026-08-05, athenaeum#768):** the three open questions below were
> about the Q1/Q2 mechanism — default disposition, ledger filename, `status`
> values — and that mechanism was superseded before any of them needed an
> answer (see Q1/Q2 above). Kept for historical record; none is a live
> decision point. The one open question that *is* still live is the
> outcome-vocabulary gap noted under "The actor is wrong": today the shipped
> mechanism only ever marks the `5.x` hard-bounce case (Q3); Voltaire's
> `4.x`/potentially-stale outcome (voltaire#81) has no athenaeum-side handling
> at all yet, deterministic or otherwise — tracked as an open question on
> **athenaeum#765**.

- **Default disposition** — this proposed `archive` (preserve). Superseded —
  see Q2.
- **Ledger filename / surface** — `_deprecations.jsonl` under the contacts
  (excluded) surface was proposed for consistency with `_observations.jsonl`.
  Superseded — see Q1.
- **`status` values** — `bounced` vs `deprecated` were proposed as distinct
  (observed hard failure vs operator/marker assertion). Superseded along with
  the record type that would have carried them — see Q1.
