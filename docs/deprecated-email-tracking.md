<!-- SPDX-License-Identifier: Apache-2.0 -->

# Design: tracking deprecated / bounced email identifiers (athenaeum#565)

**Status: PROPOSAL — awaiting operator ratification before any implementation.**
This document is the deliverable of issue athenaeum#565. It answers the five design
questions the issue poses, recommends a default, and defines the raw-intake
contract that maecenas#42 is blocked on. Nothing here is built yet; the
acceptance criteria call for the design to be **reviewed before implementation**,
and each downstream implementation slice below is filed separately once this is
ratified.

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
  reports the bounce fact into raw intake and knows nothing about how the
  librarian records it (maecenas#42, maecenas#41).

The recommendation below implements this model; it does not reopen it.

---

## Q1 — Where does deprecation live?

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
lets an operator, e.g., archive bounces reported by maecenas campaigns (default)
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

## Q3 — The raw-intake proposal contract (maecenas#42)

maecenas submits a **bounce fact** into raw intake and nothing more. It does not
know about the ledger, the excluded surface, archive-vs-delete, or the wiki.

**Contract — the minimal bounce-fact proposal maecenas#42 emits:**

```yaml
type: email_bounce            # the intake proposal kind the librarian recognizes
identifier: person@example.com   # the address that bounced (required)
event: bounced                # "bounced" (hard failure observed) — the only value maecenas sends
observed_at: 2026-07-15       # ISO-8601 date the bounce was observed (required)
diagnostic: "550 5.1.1 user unknown"   # the SMTP/provider bounce diagnostic, verbatim (optional but preferred)
source: "campaign:spring-2026/msg-abc123"   # where the bounce came from (required, for provenance)
```

Rules of the contract:

1. maecenas emits **only the fact**. It never sets `disposition`, never decides
   archive-vs-delete, never touches the contact store or the wiki. The librarian
   resolves the disposition (Q2) when it consumes the proposal.
2. The proposal is **append-only intake**, like every other raw-intake record —
   maecenas writes it into the raw intake area and stops. The librarian's normal
   intake pass picks it up, resolves the disposition, and writes the
   `_deprecations.jsonl` record (and, if `delete`, removes the identifier).
3. `identifier`, `observed_at`, and `source` are required; `diagnostic` is
   optional but strongly preferred (it is what distinguishes a hard bounce from
   a transient one at review time).
4. maecenas learns the librarian's outcome, if at all, only through the normal
   intake result surface — **ideally it learns nothing**, per the operator's
   "maecenas shouldn't need to worry about this."

**This is the contract maecenas#42 is blocked on.** On ratification, this
section is copied to maecenas#42 so that issue can proceed.

---

## Q4 — Reconciliation with Google Contacts

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

**Yes — the `BOUNCED <date>: <diagnostic>` markers already recorded in Google
Contact biographies are a backfill source. Scoped as a follow-up slice.**

Bounces have been recorded in gcontacts bios as `BOUNCED <date>: <diagnostic>`
for some time. A one-time backfill pass parses those markers and emits the
**same** `type: email_bounce` intake proposals (Q3) with `source:
"backfill:gcontacts-bio"` and `observed_at` taken from the marker's date — so
backfill and live bounces flow through one code path, not two. Because intake is
append-only and the ledger is idempotent on `dep_id` (caller-minted from the
marker's `identifier + observed_at`), the backfill can be re-run safely.

The backfill is a distinct implementation slice (it needs read access to the
gcontacts bios, a boundary this containerized design work does not cross); it is
filed once this design is ratified.

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

## What ratification unblocks (implementation slices, filed on approval)

1. `_deprecations.jsonl` record type + append/read helpers in `athenaeum.pii`,
   mirroring the observation ledger (append-only, schema-versioned).
2. `resolve_bounce_disposition` knob (env > yaml > default `archive`) + optional
   per-source override.
3. The `type: email_bounce` intake path in the librarian that consumes the Q3
   proposal and writes the deprecation record per the resolved disposition.
4. The maecenas#42 contract (Q3), communicated to that issue.
5. Backfill pass from gcontacts `BOUNCED` bio markers (Q5).
6. (Optional) the `deprecated-here-but-live-in-gcontacts` reconciliation lint (Q4).

## Open questions for the ratifying reviewer

- **Default disposition** — this proposes `archive` (preserve). Confirm, or
  choose `delete` as the global default (not recommended, per Q2's
  reversibility argument).
- **Ledger filename / surface** — `_deprecations.jsonl` under the contacts
  (excluded) surface is proposed for consistency with `_observations.jsonl`.
  Confirm that surface is the intended home.
- **`status` values** — `bounced` vs `deprecated` are proposed as distinct
  (observed hard failure vs operator/marker assertion). Confirm both are wanted,
  or collapse to one.
