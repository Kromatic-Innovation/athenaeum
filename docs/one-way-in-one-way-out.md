<!-- SPDX-License-Identifier: Apache-2.0 -->

# One way in, one way out — the two-path invariant

**Status:** ARCHITECTURAL INVARIANT. Issue athenaeum#863. This document is the
canonical statement of the rule; every other document that touches one half of
it references this page rather than restating it.

Companion to [`docs/adapter-contract.md`](adapter-contract.md) (the source →
intake seam), [`docs/field-corrections.md`](field-corrections.md) (the
conformance fast path on the ingress side),
[`docs/recall-architecture.md`](recall-architecture.md) (how recall assembles a
read) and [`docs/security-posture.md`](security-posture.md) (audience scoping,
one enforcement of the egress half).

---

## 1. The invariant

> **One path in, one path out.**
>
> **In:** every write reaches the knowledge base as raw intake, and exactly one
> writer — the librarian — compiles intake into the store. No source writes the
> store.
>
> **Out:** every read reaches a caller through the recall/read interface. No
> program opens a store directly.

Both halves are one rule seen from two ends: **the store has named seams, and
nothing goes around them.** The value is not secrecy — it is that a rule about
data can be stated, changed, or enforced *in one place*, because there is only
one place data crosses the boundary.

The alternative is not a weaker version of this rule. It is N callers each
having grown their own path, at which point no rule about the data can be
enforced at all without finding and changing every one of them.

## 2. The ingress half — everything enters as intake

A source appends to raw intake. The librarian is the only writer to the wiki.
This is the structural guarantee the rest of the system rests on
([`docs/why-athenaeum.md`](why-athenaeum.md)), and it is what makes provenance,
deduplication and contradiction detection possible at all: if a source could
write the store directly, the compiled record would carry no reliable account of
where it came from.

Conformance changes *how cheaply* a submission is absorbed, never *whether* it
goes through the librarian. A prose note enters at the top of the tier ladder; a
conformant field correction enters at the bottom
([`docs/field-corrections.md`](field-corrections.md) §1.1). Neither one is a
side door — a correction is a file in the ordinary raw-intake tree, recognized
by its shape, and the librarian's routing remains authoritative.

The two failure modes this half rules out:

- **A writer that writes the store directly**, bypassing compilation, and so
  produces records with no provenance and no contradiction check.
- **A per-writer entry point** — one typed interface per feature or consumer.
  There is exactly one conformance format, for the same reason there is one
  read interface: a seam that has been forked is not a seam.

## 3. The egress half — everything leaves through the read interface

Every read goes through the recall/read interface — **one path, not two**.

A store occupies two kinds of surface, and it is tempting to describe them as
two entry points. That framing was this document's own, and it was the problem:
an interface that answers "find me things" and an interface that answers "give
me this person's withheld fields" are two shapes a caller has to know about, and
a caller who has one and needs the other reaches past the seam. So the surfaces
are still two; the way in is one.

| Surface | What lives there | How a read reaches it |
|---|---|---|
| **Corpus surface** — the wiki, plus any configured intake roots | Compiled entity pages, the observation ledger | `recall` / the search path, the MCP read tools, `athenaeum query` |
| **Excluded surfaces** — the roots that entity classes mapped to an excluded-policy adapter resolve to (`storage.mapping`); `pii` is the one this repo ships | Records held off-corpus (athenaeum#427, athenaeum#437) — contact data for a person, and, since athenaeum#883, whatever an operator routes off-corpus for any other class | **The same `recall`**, with its excluded-field parameter set (athenaeum#885) — the read a caller is already making, resolving the excluded record for the hit it already has. `pii.read_entity` / `read_entities` read one by uid when the caller has no query. Each resolves the surface root on the caller's behalf |

**The excluded surface is the one that needs saying out loud**, because it
is the one that fails quietly. Holding contact data off-corpus keeps it out of
recall *by construction* — a scanner never reaches it. But "outside the corpus"
is not "unreachable": it is an ordinary directory on disk, and nothing about
opening it raises an error. An agent session gathering a census on 2026-08-12
read it directly, because nothing said otherwise and no alternative existed.

So, explicitly:

> **A caller that opens a store path itself — corpus or contact-data — is a
> defect, not a shortcut.** It is a defect even when it produces the right
> answer, even when the path is correct, and even when the process is
> authorized to read every byte it touched. The rule is about *where the seam
> is*, not about who is on which side of it.

Excluded fields are resolved through **the read the caller is already making**.
`recall`'s excluded-field parameter (athenaeum#885) takes a hit the corpus
already produced and attaches that entity's excluded record to it — for any
entity class, not only persons. Only `pii.py` and the storage-adapter layer it
delegates to know the surface layout; a caller supplies a query and a flag and
never constructs a path.

Three properties make that one path rather than a second one:

- **It cannot widen what you can see.** The flag is a *render-layer join*, not
  a search predicate. Excluded values are never indexed and are not searchable;
  they are only resolvable on a hit that was already authorized and already
  ranked. The join runs strictly after the fail-closed audience check and after
  the athenaeum#532 `recallable` drop, so a hit either of those removes never
  triggers an excluded lookup at all — the flag cannot be used to probe whether
  a record exists behind a page you may not read.
- **It is free when unused.** With the flag unset, recall performs zero
  excluded-surface scans and returns byte-identical output.
- **Withheld never looks like absent.** A field you did not receive comes back
  as a redaction marker naming the field and how many values exist — the
  distinction the whole surface exists to preserve.

`pii.read_entity` / `read_entities` (athenaeum#883) remain for the caller who
has a uid and no query — the by-uid form of the same read, resolving the same
surface root the same way. `read_entities` is the batch form and exists for a
reason this document cares about: the single-call shape cost a full pass over
the store *per uid* — ~28s each against the live corpus, ~37 hours for the
4,696 people the weekly enrichment job resolves — which is the kind of number
that makes a caller reach for the surface directly and go around the seam.
**A seam that is far too slow for a real workload is one a caller will
eventually route around**, so keeping the batch shape *inside* the interface is
what keeps the invariant true in practice rather than only on paper. It is
item 4 of §5's checklist taken up rather than worked around.

`pii.read_person` / `read_people` (athenaeum#864, athenaeum#877) — and the
`read_person` MCP tool and `athenaeum query person --uid ... [--include-contact]`
CLI command — keep working identically, as person-fixed wrappers over the
generic read. They are retained, not deprecated by this section; `apollo-enrich`'s
weekly job calls `read_people` today.

## 4. The invariant is not authorization

These are two different questions, and it matters that they are answered in that
order:

| | The invariant (this document) | Authorization (deferred) |
|---|---|---|
| Question | *Where does data cross the boundary?* | *Who may receive what, once it does?* |
| Status | **Load-bearing now.** Binding on every caller today. | **Deliberately open.** Not yet decided. |
| Enforced by | Code review, and the fact that the sanctioned entry points are the only ones that resolve store paths | Partially: audience scoping over the MCP tool surface ([`docs/security-posture.md`](security-posture.md) §2.1) |

There is no authentication on the contact-data surface, no capability check, and
no audit trail. Both stores are unencrypted on a single host. **None of that
weakens the rule above** — and a reader who finds no access control must not
conclude there is no rule. The rule is what makes the access-control question
answerable later *at one seam* instead of at N call sites, which is the entire
reason for building the interface before deciding the policy.

Audience scoping (athenaeum#312, athenaeum#538) is an early, partial instance of
that later question, applied to the MCP tool surface. It is **one enforcement of
the egress half, not the whole of it** — its existence should not be read as the
boundary being complete, and the surfaces it does not cover are still governed by
this document.

The corresponding parked policy questions: egress refusal and conditional
redaction on the outbound-LLM path (athenaeum#428), and who may set the
contact-inclusion flag (athenaeum#864, out of scope there for the same reason).

## 5. What this means for a new integrator

If you are writing a client against athenaeum:

1. **To write:** append to raw intake — as prose, or as a conformant correction
   if your writer already knows the entity, field and provenance
   ([`docs/field-corrections.md`](field-corrections.md)). Do not write the store.
2. **To read the corpus:** use `recall` / the MCP read tools / `athenaeum query`.
3. **To read excluded fields** — a person's contact data, or whatever else the
   operator routes off-corpus for any other entity class — use **the one read
   path**, in whichever of its two shapes matches what you already have:

   - **Searching?** `recall` with the excluded-field flag set: the `recall` MCP
     tool's `with_pii=True`, or `athenaeum recall --with-pii` (athenaeum#885,
     athenaeum#886). This is the read you were already making.
   - **Holding a uid?** The `read_entity` MCP tool, `athenaeum query entity
     --uid ... --class ... [--include-excluded]`, or `pii.read_entity`
     (athenaeum#883, athenaeum#886). **Resolving more than one uid in one
     process: use `pii.read_entities`**, which pays the store's O(corpus) scans
     once for the whole batch instead of once per uid — a loop over the
     single-uid form is the shape that measured ~37 hours for one weekly job.

   The person-shaped entry points — the `read_person` MCP tool, `athenaeum
   query person --uid ... [--include-contact]`, and `pii.read_person` /
   `read_people` (athenaeum#864, athenaeum#877) — are **retained wrappers** over
   that same read. They keep working identically and are not going away
   without a separate decision; new integrations should prefer the generic
   path, which answers for every entity class rather than one.

   Whichever you use: do not resolve an excluded surface yourself, and do not
   treat a missing value as "no value" unless the response says so — the
   interface distinguishes *withheld* from *absent* with a redaction marker
   precisely so a caller cannot silently mistake one for the other.
4. **If the interface does not do what you need:** that is an issue against the
   interface, not a licence to go around it. A path that works today and is
   unenforced today is still the thing this document exists to rule out.

## 6. Where the halves are documented

This page is the statement of the rule. These pages carry the detail, and
reference the rule rather than restating it:

- [`docs/field-corrections.md`](field-corrections.md) §1.1 — the ingress half:
  conformance sets tier depth, never whether a submission enters.
- [`docs/security-posture.md`](security-posture.md) §2 / §2.1 — the egress half
  as far as it is currently enforced: MCP tool audience scoping.
- [`docs/recall-architecture.md`](recall-architecture.md) — the read path, and
  the load-bearing-invariants table.
- [`docs/adapter-contract.md`](adapter-contract.md) — the source → intake seam.
- [`docs/storage-adapter-contract.md`](storage-adapter-contract.md) — how an
  entity class resolves to a surface, including the excluded-policy adapter that
  puts the contact-data surface outside the corpus.
