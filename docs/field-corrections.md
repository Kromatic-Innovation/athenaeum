<!-- SPDX-License-Identifier: Apache-2.0 -->

# Deterministic Field Corrections — a conformance fast path for mechanical writers

**Status:** DESIGN LOCK. Issue athenaeum#794. Not yet implemented — the librarian fast path is
a follow-up issue in this repo (athenaeum#797).

Companion to [`docs/adapter-contract.md`](adapter-contract.md) (the source → intake
seam), [`docs/provenance-shape.md`](provenance-shape.md) (how attribution is
represented) and [`docs/conflict-resolution.md`](conflict-resolution.md) (how
disagreements are resolved).

---

## 1. The problem

Some writers know exactly what they want changed. A delivery-status monitor knows that
`alex@example.org` hard-bounced today. A bulk relationship-graph writer knows that page X
should gain a backlink to page Y — for thousands of pages in a single pass. A third-party
enrichment service knows a job title changed.

Today every one of them has the same two options: write prose into raw intake and pay LLM
compilation per fact, or — the shortcut real deployments actually take — write the wiki
directly and bypass the librarian entirely. The first does not scale to bulk writers. The
second breaks the structural guarantee the whole system rests on
([`docs/why-athenaeum.md`](why-athenaeum.md)): a source appends to intake, and exactly one
compiler writes the wiki.

This document specifies a third option that is neither: **a conformance format a writer
MAY use to arrive with its work already done.**

### 1.1 The one rule everything else follows from

> **Conformance sets how deep in the tier ladder a submission enters. It never sets
> whether it enters.**

Athenaeum's pipeline is a cost ladder — cheap mechanical processing first, then simple
reasoning, then expensive reasoning, then a human. The librarian accepts all input; what
varies is how much reasoning that input costs to absorb.

A prose note saying *"Alex's address bounced"* enters at the top of the ladder. The
librarian must classify it, locate the entity, decide which attribute it bears on, notice
that it contradicts the existing "this address is valid" claim, and reason that the
address *was* valid and no longer is. That is several tiers of work, and it is correct
work — the contradiction is real and resolving it is the librarian's job.

A conformant correction saying `set bounced = 2026-08-06 on <entity>, because <source>,
observed <when>` enters at the *bottom* of the ladder. Not because it skipped anything,
but because the submitter already did what the upper tiers would have done: named the
entity, named the attribute, and supplied the provenance that resolves the contradiction
in advance. There is nothing left to reason about, so nothing reasons.

**Three consequences, and they are the spine of this document:**

1. **Nothing is rejected.** Every failure to conform is a *fallthrough*, not a refusal.
   A malformed batch, an unparseable source, a target that does not resolve — each
   degrades to the next tier up, exactly as an ordinary prose note would. A correction
   that cannot be applied cheaply is still a fact somebody reported, and the librarian's
   job is to absorb it.
2. **This is not a new API.** No sibling entry point, no reserved subtree, no
   per-writer interface. A correction is a file in the ordinary raw-intake tree,
   recognized by its shape. Proliferating typed interfaces — one per feature, one per
   consumer — is brittle and unmaintainable; there is exactly one conformance format and
   every writer that wants the cheap tier uses it.
3. **The submitter proposes; the librarian disposes.** A correction may name the target
   record and field — that specificity is the whole point, it is what makes the work
   cheap. But naming a destination is a *proposal*. The librarian's routing (§7) is
   authoritative and may place the fact elsewhere. No conformance format grants the
   right to bypass routing.

### 1.2 Why this is not `tier0_passthrough`

`tier0_passthrough` is the existing LLM-free path, but it is **whole-page**: it promotes
a complete pre-structured page verbatim and declines when the uid already exists. A
writer that knows only "set one field on an existing entity" would have to
read-modify-write the entire page — which races the documented single-writer assumption
(`conflict-resolution.md` §4, "no write-time locking, last-write-wins on disk") and
clobbers fields it never meant to touch.

Corrections are **patches the librarian applies**, not pages a writer submits.

### 1.3 The precedent this generalizes

`librarian.tier0_handle_upsert` (athenaeum#486, athenaeum#692) already does deterministic, LLM-free,
field-level merge onto an *existing* page: it resolves the target by `uid` or by
name/alias through the `EntityIndex`, gates the write on an actual delta (so a re-submit
is byte-for-byte a no-op), schema-validates the merged frontmatter, and writes atomically.

Every one of those properties is a requirement below. This design is that function
widened from one fixed key set to an allowlisted attribute, with a precedence check in
front of the delta gate and a routing step behind it.

---

## 2. Where a correction sits in the ladder

| Tier | Cost | Handles |
|---|---|---|
| **0 — mechanical** | no LLM | A conformant correction: target resolves, attribute is known, precedence decides, delta gate writes. Also `tier0_passthrough` and `tier0_handle_upsert`. |
| **1-2 — cheap reasoning** | small model | A correction that *almost* conforms — target names an entity the index cannot match exactly, or an attribute with no schema slot (§7.2). Also ordinary prose classification. |
| **3 — expensive reasoning** | large model | Genuine contradiction between the correction and the incumbent value that precedence cannot settle. Also ordinary prose merge. |
| **4 — human** | operator | What tier 3 escalates: `wiki/_pending_questions.md`. |

A correction can land at any of these. Conformance is a hint about where it will land,
not a guarantee — an entirely well-formed correction that genuinely contradicts a
human-stated value still climbs to tier 4, and that is correct.

---

## 3. Submission

### 3.1 Location — the ordinary intake tree

```
<knowledge-root>/raw/<source>/<timestamp>-<uuid8>.jsonl
```

The same `raw/<source>/` convention Lane A already specifies
(`adapter-contract.md` §1), with the same timestamp/uuid8 filename and the same atomic
write requirement. **There is no reserved subtree and no separate discovery function.**

That is deliberate. A reserved subtree skipped by `discover_raw_files` would mean a
non-conforming batch is seen by nothing at all — the bypass this design exists to remove.

**Discovery recognizes a correction batch by shape**: a `.jsonl` file whose first line
parses as JSON with `record: "batch"`. Anything else is ordinary intake.

Making that literally true takes one change to `intake.discover_raw_files`, and the
implementation must not skip it. Today that function globs `*.md` and matches
`RAW_FILE_RE = ^(\d{8}T\d{6}Z?)-([0-9a-f]{8})\.md$` — so a `.jsonl` file in `raw/<source>/`
is invisible to it. "Falls through to ordinary intake" would silently mean "is seen by
nothing," which is the exact failure this section claims to have removed. Required:

- `discover_raw_files` globs `*.md` **and** `*.jsonl`, with `RAW_FILE_RE` widened to accept
  either extension.
- It **skips** a `.jsonl` whose first line parses as a valid batch envelope — that file is
  claimed by the correction phase. This is the only correction-shape knowledge the generic
  discovery function carries, and it is one check.
- Every other `.jsonl` is ordinary intake. The tiers classify text; nothing requires the
  body to be markdown.

Note what is NOT skipped by name. `discover_raw_files` skips exactly one directory today
(`answers`, issue athenaeum#414). `raw/auto-memory/` is not skipped — it is simply never matched,
because auto-memory filenames do not satisfy `RAW_FILE_RE`, and
`discover_auto_memory_files` walks it separately. Corrections follow the auto-memory
pattern (claimed by shape, by a different consumer), not the `answers` pattern (excluded
by directory name).

### 3.2 File format — JSONL, envelope-first

One JSON object per line; line 1 is the batch envelope, the rest are corrections. JSONL
because a batch may carry thousands of records and the applier streams it.

```jsonl
{"record":"batch","schema_version":1,"submitter":"graph-writer","batch_id":"20260806T140211Z-9f3ac1d2","created_at":"2026-08-06T14:02:11Z","defaults":{"source":"script:graph-writer","observed_at":"2026-08-06T14:02:11Z"}}
{"record":"correction","correction_id":"…","target":{"uid":"person-alex-doe-a1b2c3d4"},"op":"add","field":"backlinks","value":"company-northwind-77aa11bc"}
```

**Envelope**

| Field | Required | Meaning |
|---|---|---|
| `record` | yes | Literal `"batch"`. |
| `schema_version` | yes | Integer, `1` today. An unknown version is a fallthrough (§8), not an error. |
| `submitter` | yes | Stable name of the writing system; conventionally equals the `<source>` directory. |
| `batch_id` | yes | Unique; conventionally `<timestamp>-<uuid8>`. |
| `created_at` | yes | RFC-3339 UTC. |
| `defaults` | optional | Hoisted into every record: `source`, `observed_at`, `field`, `op`. A record's own key wins. |

**Correction record**

| Field | Required | Meaning |
|---|---|---|
| `record` | yes | Literal `"correction"`. |
| `correction_id` | yes | Content hash (§5.2). |
| `target` | yes | Proposed destination entity (§3.3). A proposal — see §7. |
| `op` | yes* | `set` \| `add` \| `remove` (§4). |
| `field` | yes* | Proposed attribute. A proposal — see §7. |
| `value` | yes | Scalar, or a dict for list-of-dict attributes. |
| `source` | yes* | `SourceRef` shorthand `<type>:<ref>`, parsed by `provenance.parse_source`. The precedence input (§6). |
| `observed_at` | yes* | RFC-3339 UTC — when the submitter *observed* the fact. Breaks same-tier ties. |
| `note` | optional | Free text. Carried into the audit ledger, and into an escalation if one is raised — the *why*, which is what lets a human or an upper tier act without re-deriving context. |

\* or supplied by the envelope's `defaults`.

An unknown key on a correction record makes the record non-conformant, so it takes the
fallthrough path (§8) rather than being silently ignored. A typo'd key must not quietly
drop a constraint.

### 3.3 Target identity

One of three shapes:

| Shape | Resolution |
|---|---|
| `{"uid": "…"}` | `EntityIndex.get_by_uid`. Unambiguous; preferred. |
| `{"type": "…", "name": "…"}` | `EntityIndex.lookup`, with the cross-type and entity-format guards `tier0_handle_upsert` already applies. |
| `{"type": "…", "handle": {"<key>": "<value>"}}` | Resolved through `registry.json` ([`docs/source-handles.md`](source-handles.md)). `<key>` must be a `SOURCE_HANDLE_KEYS` member. |

The handle shape exists because external systems key on their own identifiers — an email
address, a profile URL, a channel id — not on athenaeum uids.

A target that resolves to zero entities, or ambiguously to several, does **not** fail. It
is a correction whose entity identity needs reasoning, so it goes up the ladder (§8).

---

## 4. Operations

| `op` | Applies to | Semantics | Idempotent because |
|---|---|---|---|
| `set` | scalar | Replace the value. | Re-setting the same value is a delta-gate no-op. |
| `add` | list | Union the value in. | Value-identity dedupe — `repr(value)` for dicts, the value itself for scalars, the match key already locked in `provenance-shape.md` §2.2 and used by `dedupe._perform_merge`. |
| `remove` | list | Drop matching values. | Removing an absent value is a no-op. |

There is deliberately no `clear`, `increment`, or `set_if_absent`. A writer needing one
either composes it from `set` — it knows the value it wants — or is doing something that
warrants prose (§9).

**Provenance is maintained on every op.** `add` appends the co-indexed `{value, source}`
entry to `field_sources.<field>` in the per-value shape locked in `provenance-shape.md`
§2.1, upgrading a legacy field-keyed entry per §2.3's writer rule. `set` writes the
scalar `field_sources.<field>`. `remove` prunes the dangling attribution, matching
`_merge_field_sources`.

---

## 5. Idempotency

**5.1 The delta gate (primary).** Before writing, the applier compares the merged
frontmatter to what is on disk. **No delta → no write, no `updated` bump, byte-for-byte
stable page.** This is `tier0_handle_upsert`'s existing `changed = any(...)` gate. A
submitter that cannot cheaply tell whether its correction already landed should just
re-submit; that is the designed behaviour.

**5.2 `correction_id`.**

```
correction_id = sha256(canonical_json([schema_version, target, op, field, value]))[:16]
```

with keys sorted and no insignificant whitespace. **`source` and `observed_at` are
deliberately excluded** — the same factual change proposed twice is the same correction
regardless of when it was observed.

Used for within-batch dedupe, the audit ledger, and naming a record in an escalation.
Not a global applied-once ledger — the delta gate already provides that.

**5.3 Audit ledger.** Each batch appends one line to `wiki/_corrections_applied.jsonl`
with counts by disposition (`applied`, `noop`, `deferred-lower-precedence`, `escalated`,
`raised-tier`) plus per-record ids for everything that was not `applied`/`noop`. Same
append-only-JSONL discipline as `provenance.MERGE_PROVENANCE_FILENAME`. Diagnostics,
not control flow.

---

## 6. The cheap tier's conflict resolution

### 6.1 The deterministic ranker (new)

`conflict-resolution.md` §11 states it plainly: *"the taxonomy is enforced at PROMPT time
only — no deterministic winner-picker runs in-process."* Tier 0 cannot call an LLM, so
this design introduces one.

New module **`src/athenaeum/precedence.py`**:

**A tier may hold more than one source type**, so the structure is one entry per tier
carrying that tier's type tokens — not a flat 9-tuple. Tier 2 of the canonical taxonomy is
*"`linkedin:<username>` / `twitter:<username>` — user-curated public profile"*: two tokens,
one rank. A flat tuple indexed by position cannot express that, and would silently drop
`twitter:` to the unknown-type default of 9 — seven ranks below its documented position.

```python
#: One entry per precedence tier, highest first; index + 1 is the rank.
#: A tier may carry several source-type tokens that rank equally.
SOURCE_PRECEDENCE_TIERS: tuple[tuple[str, ...], ...] = (
    ("user",),                  # 1  user said it directly
    ("linkedin", "twitter"),    # 2  user-curated public profile
    ("api",),                   # 3  third-party authoritative source
    ("wikipedia",),             # 4  consensus public source
    ("agent-observed",),        # 5  derived from an in-session artifact
    ("claude",),                # 6  LLM-generated
    ("script",),                # 7  pipeline-generated, no upstream evidence
    ("model-prior",),           # 8  training-data assertion, no session evidence
    ("unsourced",),             # 9  always loses to any sourced claim
)

def source_rank(source: str | dict | None) -> int:
    """Return the 1-based precedence rank.

    A source type absent from every tier ranks 9, as does ``None`` or an
    unparseable value.
    """
```

Any source type not listed in a tier ranks 9. Because that default is indistinguishable
from a genuine `unsourced`, **an omitted token is a silent seven-rank demotion, not a
visible failure** — which is precisely why the drift-guard test below must compare tier
*membership* against the prompt block, not merely the tier count.

> **DRIFT GUARD.** The tier list — its order **and each tier's membership** — now exists in
> four places that must change together:
> 1. `SOURCE_PRECEDENCE_TIERS` in `src/athenaeum/precedence.py`;
> 2. the `SOURCE-PRECEDENCE TAXONOMY` block of `_RESOLVE_SYSTEM` in
>    `src/athenaeum/resolutions.py` (the canonical prose list);
> 3. the `9-tier` count in `resolutions.py`'s module docstring;
> 4. the golden snapshot `tests/data/resolve_system.txt`, pinned by
>    `tests/test_resolve_system_snapshot.py`.
>
> Plus §11 of `docs/conflict-resolution.md` and this section. The implementation MUST add
> a test asserting the ranker and the prompt block agree — a second unguarded encoding of
> an already-guarded list is how the guard dies.

### 6.2 The policy

For a correction with source `S_in` against the incumbent attribution `S_cur` (from
`field_sources.<field>`, falling back to the page-level `source:`, then `unsourced`):

| Case | Disposition |
|---|---|
| `rank(S_in) < rank(S_cur)` | **Apply.** |
| `rank(S_in) > rank(S_cur)` | **Defer.** No write; recorded in the ledger with both sources named. Not an error — a script proposing over a user-stated fact losing is the system working. |
| Equal rank, equal value | **No-op** (the delta gate catches it first). |
| Equal rank, differing values, distinguishable dates | **Newer `observed_at` wins** — the taxonomy's own tie-break. |
| Equal rank, differing values, undated | **Raise a tier.** Precedence cannot settle it, so reasoning does — exactly as it would for two prose claims. |
| Incumbent is `user:` and the correction is not | **Defer**, always. A human-stated value is never machine-overwritten. |

`op: add` is evaluated **per value**, against that value's own co-indexed attribution —
adding a backlink to a list whose other entries came from a human is not a conflict.

### 6.3 The attribute allowlist and the suppression rule

A correction may only propose an attribute on the allowlist, declared in config (§10.3)
with `shape` (`scalar` | `list`), `writers`, and `monotone`. The `writers` list is a
blast-radius bound, not a trust model — it stops a buggy writer from touching an
attribute because a name collided.

**Suppression attributes resolve deterministically at tier 0.** An attribute marked
`monotone: true` — the safety flags a system uses to *stop* doing something — is set by
any permitted writer regardless of precedence, and unset only at `user:` tier. This is
not a ladder bypass: it is the cheap tier having a complete answer. The precedence ladder
asks "which of these competing facts is true", which is the wrong question for a flag
whose two error directions are not symmetric — failing to set one means continuing to act
on known-bad information; failing to clear one means an action that does not happen.
Every monotone apply is logged distinctly so the rule is auditable.

---

## 7. Routing — the librarian disposes

A correction's `target` and `field` are a **proposal**. Between the conflict decision and
the write, the librarian resolves where the fact actually belongs. **No conformance
format can bypass this step**, and a correction that names a destination the router
disagrees with is routed correctly, not honoured literally.

### 7.1 Sensitivity routing

A deployment classifies some attributes as personally-identifying, and PII has its own
storage surface — which may not be the entity page, and may not be inside the knowledge
store at all. A fact bearing on a PII attribute is routed to that surface.

This is the case that makes routing non-negotiable. If contact identifiers are PII in a
given deployment, then a fact *about* a contact identifier — including a
no-longer-deliverable marker — belongs on the PII surface. A correction proposing to set
that marker as ordinary entity frontmatter is proposing the wrong destination, and the
router corrects it. A submitter cannot opt out by being specific.

The sensitivity classification is deployment configuration, not a constant in this repo.

### 7.2 Schema evolution

A correction may name an attribute the deployment's schema has no slot for. That is not
an error — it is new information arriving, which is the ordinary case for a knowledge
system. Three dispositions, decided by reasoning, not by the submitter:

1. **A slot exists under a different name** → route to it.
2. **No slot, and the attribute looks recurrent** → the librarian **proposes a schema
   amendment** through the existing human-decision surface. The correction is held
   pending the decision.
3. **No slot, and it looks like a one-off** → record it as prose on the entity. Not
   every fact deserves a field.

This is the difference between "the librarian decides what is recorded in what schema"
and merely asserting it. A system that could only write pre-declared fields would push
schema design onto every writer — precisely the per-consumer coupling §1.1 rejects.

---

## 8. Fallthrough — what happens when conformance fails

**Nothing is rejected. Every non-conformance raises the tier.**

| Situation | Disposition |
|---|---|
| First line is not a `record: "batch"` envelope | Not a correction batch. Ordinary raw intake; the existing pipeline handles it. |
| Unknown `schema_version`, malformed envelope | The file is treated as ordinary intake, its content available to reasoning. |
| Unknown key, bad `op`/attribute combination, missing required key | Record is non-conformant → reasoning tier, carrying its own text as the claim. |
| Unparseable `source` | Reasoning tier. **Not** a fail-open downgrade to rank 9 — in Lane A a bad source costs an attribution, but here the source is the authorization to write, so a rank-9 default would still beat an unsourced incumbent. Reasoning decides instead. |
| Attribute not on the allowlist, or writer not permitted | Reasoning tier. The allowlist bounds what may be written *cheaply*, not what may be reported. |
| Target resolves to nothing, or ambiguously | Reasoning tier — entity resolution is exactly what tiers 1-2 exist for. |

A fallthrough is not a failure and never fails a batch: conformant records in the same
batch apply normally, and the ledger counts the rest as `raised-tier`.

### 8.1 How a tier raise actually happens

"Raises a tier" must name a mechanism, or it is an assumption that quietly becomes a
drop. There are two cases and they resolve differently.

**Whole-file** (rows 1-2 above — no valid envelope). Nothing to do: §3.1's widened
`discover_raw_files` sees the file and does not skip it, because the skip is conditional
on a valid envelope. The file is ordinary intake by construction.

**Per-record** (rows 3-6 — the envelope is valid, but individual records are not
cheaply applicable). The generic discovery function has already skipped this file, so
these records reach nothing unless the correction phase hands them over explicitly. It
must: for each batch with at least one raised record, the phase writes **one ordinary
raw-intake file** — canonical `<timestamp>-<uuid8>.md` in the same `raw/<source>/`
directory — whose body states each raised record as a plain claim, carrying its `note`
(the *why*) and the reason it was raised. The next pass classifies it as ordinary prose.

Three properties this must have:

- **Idempotent.** The handoff file is written once per (batch, raised-record-set); a batch
  re-examined on a later run must not re-emit it. Key it on `batch_id` plus the sorted
  `correction_id` set, recorded in the audit ledger (§5.3).
- **Provenance-preserving.** The handoff file carries the original `source` per claim in
  `field_sources`, not `script:correction-handoff`. The submitter's authority is the
  submitter's; the handoff is a transport step, not a new assertion.
- **Visible.** It is a normal intake file with normal provenance, so retirement, dedupe
  and contradiction detection all apply to it unchanged. Nothing about a raised record is
  special downstream — it is just a fact that arrived as prose, which is what it would
  have been had the submitter not tried the cheap path.

**On cost.** Bulk writers propose at thousands-per-pass scale, and in a large corpus a
substantial share of proposed targets may not resolve — so fallthrough is a common path,
not a rare one. That is a budget question, and the pipeline already answers it: the
reasoning tiers run under the existing run budget and deadline, and work that does not
fit is deferred to `wiki/_deferred_work.md` and resumed next run, exactly as ordinary
intake is. Deferral is bounded and resumable; rejection is lossy.

Submitters should still pre-filter targets against `registry.json`
([`docs/source-handles.md`](source-handles.md) §4) — a deterministic, LLM-free index
built for this. But that is a **courtesy that keeps work in the cheap tier**, not an
admission requirement.

---

## 9. Conformant or prose? The submitter's guide

Use the **conformance format** when all hold:

1. You know the target entity — a uid, an exact name, or a registered handle.
2. You know the attribute and value exactly — no interpretation, no summarization.
3. You can state a parseable `source` and an `observed_at`.
4. The change is expressible as `set` / `add` / `remove`.

Use **prose** otherwise — in particular when the fact is narrative or an observation
whose placement is the judgement the reasoning tiers exist to make, when the entity may
not exist yet and should be created, or when the change is relational or structural (a
merge, a retype, a supersession) rather than an attribute value.

**When in doubt, use prose.** The only cost of prose is compilation; the cost of a
wrong-but-conformant correction is a confidently cheap write of the wrong thing. And
**both is fine** — a system may archive a conversation as prose and separately report a
status flag as a correction. They are different facts at different costs.

---

## 10. Volume and scheduling

### 10.1 Where the phase runs

A `_run_correction_phase(ctx)` in `librarian.run()`, ordered after `_arm_run_deadline`
and **before** the entity tier phase. It makes zero LLM calls, consumes zero of
`librarian_max_api_calls`, carries its own runtime share, and stops cleanly at a batch
boundary when the deadline trips.

Ordering is the point: on a corpus where the reasoning tiers routinely exhaust the
wall-clock budget, running the deterministic phase first on a small fixed share means an
overrun degrades the expensive path — already degrading — and never the cheap one.
Records that raise a tier (§8) join the ordinary intake queue and are subject to that
path's budget, which is the correct accounting: they cost what reasoning costs.

### 10.2 Bounds

| Bound | Config key | Default |
|---|---|---|
| Records per batch file | `librarian.corrections.max_records_per_batch` | 5,000 |
| Records applied per run | `librarian.corrections.max_records_per_run` | 50,000 |
| Batch file size | `librarian.corrections.max_batch_bytes` | 32 MiB |
| Escalations per run | `librarian.corrections.max_escalations_per_run` | 50 |
| Phase runtime share | `librarian.corrections.runtime_share` | 0.05 |

Over a size bound, the batch is not refused — it is deferred whole to the next run and
reported as carry-over. Batches apply FIFO by filename across submitters.

The escalation cap is a flood guard: a writer with a systematic disagreement could
otherwise fill the human queue with thousands of near-identical questions in one run. On
hitting the cap the applier keeps applying and deferring normally, and emits one summary
line naming the submitter and attribute with the highest escalation count — which is the
actionable signal anyway.

### 10.3 Config surface

Every key follows athenaeum's existing convention exactly — the `librarian.*` YAML
namespace, an `ATHENAEUM_*` env override that wins over YAML, and a `librarian_<key>()`
accessor with the bool-is-an-int-subclass guard the existing resolvers carry. One level
of nesting under `librarian` matches `librarian.delta.*` and `librarian.reindex.*`.
Env names follow the pattern `ATHENAEUM_CORRECTIONS_<KEY>`.

`librarian.corrections.fields` maps an attribute to `{shape, writers, monotone}`, and is
**empty by default**: with no allowlist entry, no correction is applied cheaply and every
submission takes the reasoning path. A fresh deployment cannot have its wiki written by a
mechanical writer until an operator opts in per-attribute. Sensitivity classification
(§7.1) is likewise deployment config.

The implementation adds these to `docs/configuration.md` in the same change; a key in
code and not in that table is drift.

---

## 11. Worked examples

Submitter names below are **roles**, not products — any system playing the role uses the
same format. Entities and addresses are synthetic.

### 11.1 Delivery-status flag (safety-relevant, latency-tolerant)

A monitor observes that an address is no longer deliverable.

```jsonl
{"record":"batch","schema_version":1,"submitter":"delivery-monitor","batch_id":"20260806T140211Z-9f3ac1d2","created_at":"2026-08-06T14:02:11Z","defaults":{"source":"api:delivery-monitor:2026-08-06","observed_at":"2026-08-06T14:01:55Z"}}
{"record":"correction","correction_id":"3f9a2c81b7d4e065","target":{"type":"person","handle":{"email":"alex@example.org"}},"op":"set","field":"bounced","value":"2026-08-06","note":"permanent delivery failure reported by the receiving server"}
```

Two things happen that the submitter does not control. The attribute is `monotone`, so
tier 0 resolves it without consulting precedence (§6.3). And if the deployment classifies
contact identifiers as PII, **§7.1 routes this to the PII surface**, not to entity
frontmatter — regardless of the fact that the correction named an entity page. The
submitter reported a fact; the librarian decided where it lives.

Compare the prose equivalent: *"Alex's address alex@example.org has bounced."* Same fact,
same eventual destination — but the librarian must classify it, resolve the entity, infer
the attribute, detect that it contradicts the standing "this address is valid" claim, and
reason that the address *was* valid and no longer is. Several tiers of work for the same
outcome. The conformant form is not a different privilege; it is the same journey with
the reasoning already done.

Latency here is batch/eventual, and that is a deliberate design choice: a deployment that
needs a hard real-time suppression window should hold it in the acting system's own
ledger and consult the knowledge store as the durable record. This contract is not
engineered for real-time.

### 11.2 Bulk relationship graph (the volume case)

```jsonl
{"record":"batch","schema_version":1,"submitter":"graph-writer","batch_id":"20260806T030000Z-1a2b3c4d","created_at":"2026-08-06T03:00:00Z","defaults":{"source":"script:graph-writer","observed_at":"2026-08-06T03:00:00Z","op":"add","field":"backlinks"}}
{"record":"correction","correction_id":"a1…","target":{"uid":"person-alex-doe-a1b2c3d4"},"value":"company-northwind-77aa11bc"}
{"record":"correction","correction_id":"b2…","target":{"uid":"person-blair-roe-11ff22ee"},"value":"company-northwind-77aa11bc"}
```

Envelope defaults carry `op`/`field`/`source`, so each record is three keys. `script:` is
rank 7 — near the bottom — which is exactly right: an inferred backlink must lose to
anything a human or an API asserted. Targets that do not resolve raise a tier (§8); at
this volume that is the expected steady state, which is why pre-filtering against
`registry.json` is worth the submitter's trouble.

### 11.3 Third-party enrichment

```jsonl
{"record":"batch","schema_version":1,"submitter":"enrichment-service","batch_id":"20260806T060000Z-5e6f7a8b","created_at":"2026-08-06T06:00:00Z","defaults":{"source":"api:enrichment-vendor:2026-08-06","observed_at":"2026-08-06T05:58:40Z"}}
{"record":"correction","correction_id":"c3…","target":{"uid":"person-alex-doe-a1b2c3d4"},"op":"set","field":"current_title","value":"VP Engineering"}
```

`api:` is rank 3, so it overwrites a `claude:`- or `script:`-sourced title but loses to a
`user:`- or `linkedin:`-sourced one. Had this named an attribute the deployment's schema
does not carry, §7.2 would apply — route to an equivalent slot, propose an amendment, or
record it as prose.

### 11.4 Rolled-up activity counters

```jsonl
{"record":"batch","schema_version":1,"submitter":"cadence-tracker","batch_id":"20260806T070000Z-2c3d4e5f","created_at":"2026-08-06T07:00:00Z","defaults":{"source":"script:cadence-rollup","observed_at":"2026-08-06T07:00:00Z","op":"set"}}
{"record":"correction","correction_id":"e5…","target":{"uid":"person-alex-doe-a1b2c3d4"},"field":"last_contacted_at","value":"2026-08-04"}
{"record":"correction","correction_id":"f6…","target":{"uid":"person-alex-doe-a1b2c3d4"},"field":"contact_count_90d","value":7}
```

Two counters, not the underlying events — which is a design rule in its own right (§12).

---

## 12. Event streams do not belong in the entity record

A recurring question for any system that logs interactions: does the log live in the
knowledge store?

**Recommendation: no. Keep the event stream in its own store and let a rollup cross the
boundary through this contract.** Two properties decide it, and both are checkable
against a given deployment rather than matters of taste.

**Does logging an event force an entity record into existence?** A system typically
interacts with people it has no relationship record for. If events are entity records,
they either dangle — a log entry linked to nothing, useless for the relationship view the
knowledge store exists to hold — or they force a record for everyone ever touched. If a
deployment has a rule like "an entity record means an established relationship, not mere
contact," storing events in the store violates it directly.

**Are the queries semantic or structured?** The knowledge store's distinguishing
capability is semantic recall. Event-log queries are almost always structured — *when was
the last event for this entity*, *how many in the last N days*, *which entities are inside
a cap*. Structured queries buy nothing from a semantic store, and the cost is real: event
volume grows the corpus, and the corpus is what the compile budget is spent on.

When both point the same way, the layering is:

| Layer | Holds |
|---|---|
| Acting system's own ledger | immediate, latency-sensitive checks |
| Event store (outside the knowledge store) | every event: entity key, kind, timestamp, context |
| Entity record | a small rollup — last-event date, a windowed count |
| Sensitive-data surface | identifiers and other PII (§7.1) |

The entity record keeps the *relationship*; the log lives elsewhere; a rollup is the only
thing that crosses — through this contract, as §11.4. No new record type, no new
mechanism.

**The exception worth naming:** if the log genuinely needs semantic recall — *what was
discussed*, not *when did it happen* — that is a different artifact. Narrative content is
prose intake and a relationship record; a frequency counter is a rollup. Routing them
differently is correct, not a gap.

---

## 13. Adoption is forward-only

A deployment adopting this contract is changing its **write paths**, not its existing
content. Correctly-written content already in the store stands; there is no retroactive
cleanup pass and none should be built.

One caveat: pre-existing values often carry no `field_sources` attribution, so they read
as `unsourced` (rank 9) to §6.2 and will lose to essentially any correction. That is the
correct outcome — an unattributed legacy value should yield to an attributed new one —
but it means the first correction batch against a page is likelier to write than a
steady-state one. Expected, not a defect.

---

## 14. Not decided here

- **The librarian fast-path implementation** — athenaeum#797.
- **Body-level corrections.** Frontmatter attributes only; a correction to prose in a page
  body is an ordinary intake submission.
- **Retraction of an applied correction.** The audit ledger (§5.3) makes one
  reconstructible and the store is git-versioned, but no tooling is specified.
- **A deployment's sensitivity classification or attribute allowlist.** Configuration,
  never shipped in this repo.

---

## See also

- [`docs/adapter-contract.md`](adapter-contract.md) — the intake lanes; §3 there is the
  idempotency convention §5 here parallels.
- [`docs/provenance-shape.md`](provenance-shape.md) — §2 per-value `field_sources`
  (the shape §4 writes), §10 the source-type vocabulary.
- [`docs/conflict-resolution.md`](conflict-resolution.md) — §11 the source-precedence
  taxonomy (prompt-only until §6.1 here), §4 the single-writer assumption §1.2 explains
  this contract must not violate.
- [`docs/source-handles.md`](source-handles.md) — `registry.json`, the pre-filter that
  keeps conformant work in the cheap tier.
- [`docs/why-athenaeum.md`](why-athenaeum.md) — why the append-only-intake /
  single-compiler split exists at all.
