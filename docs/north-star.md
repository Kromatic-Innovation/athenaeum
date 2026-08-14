<!-- SPDX-License-Identifier: Apache-2.0 -->

# North star — what athenaeum is for

**Status:** PURPOSE DOCUMENT. This is the canonical statement of athenaeum's
intent and operating principles, ratified by the operator in the 2026-08-14
intake-architecture review. Other documents implement pieces of it and
reference this page rather than restating it. When a design dispute has no
obvious winner, this page is the tie-breaker.

Companion to [`docs/why-athenaeum.md`](why-athenaeum.md) (why this system
exists at all, and what it replaces),
[`docs/one-way-in-one-way-out.md`](one-way-in-one-way-out.md) (the two-path
invariant), and [`docs/field-corrections.md`](field-corrections.md) (the
conformance fast path and the tier ladder).

---

## 1. The north star

> **Surface the right information at the right time.**

A memory system is judged at its recall moments, not at its ingestion
moments. Nothing about compiling, indexing, deduplicating, or
contradiction-checking is a goal in itself — each exists only insofar as it
makes the next recall more likely to return the right thing, promptly, at
acceptable cost.

The north star fails in three directions, and every architectural decision
should be checked against all three:

- **Wrong information** — stale facts presented as current, one side of an
  unresolved contradiction presented without its dispute marker, a guess
  presented as a human-stated fact.
- **Right information, too late** — a backlog between what a source reported
  and what recall can see. Staleness of the compiled corpus is a direct
  north-star failure, not an operational detail.
- **Right information, drowned** — telemetry, no-ops, and short-term state
  competing for the same recall slots and compile budget as durable
  knowledge.

## 2. Operating principles

Each principle carries a one-line statement, the reasoning, and a pointer to
the document that implements it.

### 2.1 One way in, one way out

Every write enters as raw intake compiled by a single writer (the
librarian); every read leaves through the recall/read interface. No caller
opens a store directly. This is what makes any rule about the data statable
and enforceable in one place. Canonical:
[`docs/one-way-in-one-way-out.md`](one-way-in-one-way-out.md).

### 2.2 One ladder — conformance sets depth, never admission

All input flows one tiered escalation: deterministic handling first, then
cheap reasoning, then expensive reasoning, then a human. A submission that
already conforms to a shape the librarian understands enters low and costs
nearly nothing; one that does not climbs until something can absorb it.
There are no per-writer entry points and no typed interfaces at the gate —
there is exactly one conformance format, and the librarian owns routing and
schema. Canonical: [`docs/field-corrections.md`](field-corrections.md) §1.1.

### 2.3 Everything is dispositioned — and not everything is memory

Nothing is silently lost, but not everything is absorbed into the wiki.
Every intake file ends in exactly one **audited terminal disposition**:
compiled, applied as a correction, dropped as information-free, retained as
a source document without compilation, quarantined as unprocessable within
budget, or escalated to a human. "Dropped" is a counted, attributable,
reversible decision — distinguishable from "never seen."

The distinction this preserves: short-term state does not belong in
long-term memory. A record that says "the light was 'walk' when we crossed"
is information-free the moment the crossing is over; absorbing it is a cost
with no recall value — the *drowned* failure direction. Likewise, a source
can be an **archive**: kept on disk, greppable, citable by provenance, and
never compiled — a daily journal is the canonical example.

### 2.4 Breadcrumbs over bulk

Continuous minimal processing beats massive batch workloads. The
deterministic tier runs continuously in small, cheap increments; the
reasoning tiers run on configurable triggers — backlog depth, elapsed
interval, or an explicit on-demand request — with a scheduled nightly run as
the backstop, not the definition. Every run is budgeted, resumable, and
incremental; **the corpus is never recompiled whole.** Corpus-spanning
passes (contradiction detection) scope to what changed since their last
completed sweep, with the full sweep demoted to a rare, explicitly-invoked
audit.

### 2.5 Memory has duration

Information may carry a suggested decay: a bucket (daily / weekly /
durable) and/or a validity horizon. Recall ranks currency — an expired
daily-status page should not compete with durable knowledge unless the
query asks for history — and a deterministic sweep retires expired
short-bucket pages to git history. Everything lives in the one wiki, marked
differently; duration is metadata, never a second store.

### 2.6 Facts, not verdicts

Athenaeum is a memory system. Recall returns what is known — values,
per-value provenance and usage classification, validity dates, dispute
markers — as clear, parseable fields. What a caller may *do* with those
facts is the caller's policy: an outreach system decides for itself whether
an address may be used for a campaign; athenaeum only reports what kind of
address it is and how that is known. No action-policy predicate ships on
athenaeum's API surface.

The same principle governs the sensitive surface: an external caller is an
agent that can parse structured or unstructured text, and it needs exactly
one knob — *with* or *without* excluded fields. All mechanics behind that
knob (surfaces, joins, identity resolution) are athenaeum's internal
business. See [`docs/one-way-in-one-way-out.md`](one-way-in-one-way-out.md)
§3 and [`docs/security-posture.md`](security-posture.md) §2.

### 2.7 Rules are data; humans adopt them

Deterministic handling of a recurring input shape is declared in
user-editable rules (data with a closed vocabulary — never code), which
compile foreign shapes into the one conformance format and can also assign
dispositions (§2.3). Athenaeum ships sensible defaults as examples and is
never stuck with them: rules differ per user because wikis differ per user.

The librarian may *propose* a rule when it notices repeated reasoning-tier
work over one shape — but a proposed rule is **always adopted by a human**,
through the same pending-decisions surface as every other question, and
enters in observe-only mode first. This is an architectural decision, not
caution: raw intake is untrusted, and an auto-adopted rule would be
persistent, deterministic write influence granted by the very content it
processes.

### 2.8 One queue of questions, answered through the one way in

Everything that needs human or external input — contradictions, merge
proposals, rule proposals, schema amendments, quarantine reviews — surfaces
through one listing, retrievable in one call. Resolutions travel the same
path every other write travels: an answer is a conformant record in raw
intake, applied deterministically. Even human answers use the one way in.

### 2.9 Producers conform out of courtesy, never obligation

A producer whose output is expensive or wrong-shaped is asked — via an
issue on the producer, framed as a courtesy — to emit something cheaper.
Athenaeum absorbs what arrives regardless. Conformance buys the producer a
cheaper tier; it is never an admission requirement (§2.2).

### 2.10 Sensitivity is the deployment's vocabulary; athenaeum ships the shapes

Sensitive-data handling is automatic, and the *classes* are configuration:
athenaeum ships recognizers for the obvious shapes (email, phone, address)
wired to off-corpus routing by default, and a deployment may define its own
classes and tiers — medical-privacy categories, classification gradations —
each mapped to a storage surface and read policy through the same routing
seam. The vocabulary is open; the defaults are only defaults.

### 2.11 Storage is logically fixed, physically pluggable

The logical model — raw + wiki as source of truth, indexes derived — is a
fixed boundary. The physical layer is an adapter seam: a deployment may back
the wiki or any excluded surface with encrypted storage, a database, or a
synced filesystem, and no caller can tell, because callers only ever touch
intake and recall. See
[`docs/storage-adapter-contract.md`](storage-adapter-contract.md).

## 3. What we deliberately do not do

Durable rejections — each was considered and declined for a reason that
outlives the session that considered it:

- **Per-source parsers or typed interfaces in code.** A handler per producer
  forks the seam: N interfaces to keep in sync and per-user wiki structure
  hardcoded into a shipped library. Recurring shapes get rules (§2.7).
- **Auto-adopted rules.** Persistent-influence injection path (§2.7).
- **An LLM as the shape detector.** Deterministic fingerprint matching is
  free and sufficient for machine-emitted shapes; a model in the match path
  reintroduces per-file cost exactly where the design removes it — and puts
  untrusted content in charge of routing.
- **A separate short-term memory store.** Duration is frontmatter in the one
  wiki (§2.5); a second store forks the read seam.
- **Indexing raw intake directly.** Raw is unresolved and
  contradiction-unchecked; surfacing it is the *wrong information* failure
  direction.
- **Action-policy predicates in the memory layer.** Consumers own their
  policies (§2.6).
- **Excluding a semantically live source from intake.** If processing a
  source is too expensive, the answer is a cheaper tier for it — rules, a
  courtesy request to the producer, or both — never a locked door. Exclusion
  is reserved for content that is not memory at all (§2.3).

## 4. Using this document

A proposed change that advances one principle while moving recall further
from the north star is wrong, whatever its local elegance. The order of
appeal is: the north star (§1), then the failure directions it names, then
the principles (§2) — and when a principle seems to demand something the
north star forbids, the principle is being misread or needs amending, and
either way it is a discussion, not a build.
