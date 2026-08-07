# Deterministic Field Corrections — the mechanical-writer intake contract

**Status:** DESIGN LOCK. Issue [athenaeum#794](https://github.com/Kromatic-Innovation/athenaeum/issues/794);
enabler for P1 of the ratified federated outreach architecture
(`code-workspace-config/docs/outreach-architecture.md`). Not yet implemented — the
librarian fast path is a follow-up issue in this repo. Consumers code against this
document.

Companion to [`docs/adapter-contract.md`](adapter-contract.md) (the source → intake
seam), [`docs/provenance-shape.md`](provenance-shape.md) (how attribution is
represented) and [`docs/conflict-resolution.md`](conflict-resolution.md) (how
disagreements are resolved). This document adds a **third intake lane** to the first,
and the **first in-process implementation** of the taxonomy the third only ever
enforced at prompt time.

---

## 1. The problem, stated precisely

Athenaeum's structural rule is that a source may only *append* to raw intake, and the
librarian is the sole wiki writer (`docs/why-athenaeum.md`). Outreach-architecture P1
restores that rule across the fleet: maecenas, athenaeum-adapters and apollo-enrich all
currently write the wiki directly and must stop.

The existing intake lanes cannot absorb what those writers produce:

- **Lane A (entity intake)** compiles prose through the tiered LLM pipeline. A single
  adapters pass can propose backlink writes at the order of *thousands* of pages (the
  2026-07-12 `contact-sync` measurement: ~9,135 of ~16,896 person records structurally
  unlinkable, so the linkable remainder is in the thousands). Per-correction LLM
  compilation is not affordable at that volume.
- **Lane B (auto-memory)** is the agent-session-memory bridge. Wrong shape entirely.
- **`tier0_passthrough`** is LLM-free but **whole-page**: it promotes a complete
  pre-structured page verbatim and *declines* (falls through to the LLM tiers) when the
  uid already exists. A writer that knows only "set `bounced: 2026-08-06` on uid X"
  would have to read-modify-write the entire page to use it — which races the
  librarian's documented single-writer assumption (`conflict-resolution.md` §4,
  "no write-time locking, last-write-wins on disk") and clobbers fields the writer
  never intended to touch.

So the contract below specifies **patches the librarian applies**, not pages a writer
submits. That distinction is load-bearing; every consumer issue is built against it.

### 1.1 The precedent this generalizes

This is not a new idea in the codebase — it is the generalization of one that already
ships. `librarian.tier0_handle_upsert` (issues athenaeum#486, athenaeum#692) deterministically merges
the source-handle keys of a seed onto an **existing** page, LLM-free: it resolves the
target by `uid` or by name/alias through the `EntityIndex`, gates the write on an actual
delta (so a re-seed is byte-for-byte a no-op), schema-validates the merged frontmatter,
writes atomically, and *declines loudly* rather than degrading to prose when the target
does not resolve.

Every one of those properties is a requirement below. The field-correction applier is
`tier0_handle_upsert` widened from one fixed key set to an arbitrary declared field, with
a precedence check added in front of the delta gate.

---

## 2. Decision summary

| # | Decision | Rationale |
|---|---|---|
| **D1** | Corrections are a **third intake lane (Lane C)** with its own reserved subtree `raw/corrections/`, its own discovery function, and its own applier phase. | Lane A/B discovery must not see them; the applier must not be reachable from the LLM tiers. |
| **D2** | The applier runs as a **dedicated LLM-free phase ordered BEFORE the entity tier phase**, with its own runtime share and its own budget. | An overrun must degrade the expensive path, never the cheap one (see §8 and athenaeum#764). |
| **D3** | A new module `src/athenaeum/precedence.py` provides the **first in-process implementation of the 9-tier source-precedence taxonomy** — a pure `source_rank()` over the `<type>:<ref>` shorthand. | Today the taxonomy is prompt-only (`conflict-resolution.md` §11). A deterministic applier cannot call an LLM, so it needs a deterministic ranker. |
| **D4** | Three operations only: `set` (scalar), `add` (list union), `remove` (list removal). | Covers every named consumer. Anything richer is prose (§9). |
| **D5** | **The applier NEVER creates a wiki page.** An unresolvable target is dead-lettered, never escalated to prose. | Enforces P5's "no wiki records for mere list subscribers" and keeps the majority case (§7) cheap. |
| **D6** | Sibling entry point: a new MCP tool `propose_corrections` plus a direct file drop. **Not** `remember`. | `remember` writes one file per call with per-call sensitive screening and prose provenance injection. A thousand corrections is one file, not a thousand calls. |
| **D7** | P5's direct-outreach interaction log lives in a **separate store outside the wiki** (option b), with a small rollup written onto the person page **through this contract**. | See §11. |

---

## 3. Lane C — location and batch envelope

```
<knowledge-root>/raw/corrections/<submitter>/<timestamp>-<uuid8>.jsonl
```

- **`<submitter>`** is the stable name of the writing system, in the same character set
  as a Lane A `<source>` directory (`[A-Za-z0-9_-]`). Examples: `voltaire`,
  `athenaeum-adapters`, `apollo-enrich`, `maecenas`.
- **`<timestamp>`** is UTC `YYYYMMDDTHHMMSSZ`; **`<uuid8>`** is 8 hex chars
  (`athenaeum.generate_uid()`). Batches are applied in filename order, so this is also
  the FIFO key.
- `raw/corrections/` is a **reserved subtree**, skipped by `discover_raw_files` exactly
  as `raw/answers/` and `raw/auto-memory/` are today. A correction batch must never be
  discovered as a Lane A entity intake.
- Write atomically (same-directory temp + `os.replace`), as Lane A requires. A
  half-written batch must never be parseable.

### 3.1 File format — JSONL, envelope-first

One JSON object per line. **Line 1 MUST be the batch envelope**; every subsequent line
is a correction record. JSONL because a batch can carry thousands of records and the
applier streams it rather than loading it whole.

```jsonl
{"record":"batch","schema_version":1,"submitter":"athenaeum-adapters","batch_id":"20260806T140211Z-9f3ac1d2","created_at":"2026-08-06T14:02:11Z","defaults":{"source":"script:backlink-writer","observed_at":"2026-08-06T14:02:11Z"}}
{"record":"correction","correction_id":"…","target":{"uid":"person-jane-doe-a1b2c3d4"},"op":"add","field":"backlinks","value":"company-acme-77aa11bc"}
{"record":"correction","correction_id":"…","target":{"uid":"person-john-roe-11ff22ee"},"op":"add","field":"backlinks","value":"company-acme-77aa11bc"}
```

**Envelope fields**

| Field | Required | Meaning |
|---|---|---|
| `record` | yes | Literal `"batch"`. |
| `schema_version` | yes | Integer. `1` today. An unknown version parks the whole batch (§8.3) rather than applying a guess. |
| `submitter` | yes | Must equal the containing directory name. A mismatch parks the batch. |
| `batch_id` | yes | Unique; conventionally `<timestamp>-<uuid8>` matching the filename. |
| `created_at` | yes | RFC-3339 UTC. |
| `defaults` | optional | Values hoisted out of every record: `source`, `observed_at`, `field`, `op`. A record's own key always wins. |
| `note` | optional | Free text for the operator; never read by the applier. |

### 3.2 The correction record

```json
{
  "record": "correction",
  "correction_id": "3f9a2c81b7d4e065",
  "target": {"uid": "person-jane-doe-a1b2c3d4"},
  "op": "set",
  "field": "bounced",
  "value": "2026-08-06",
  "source": "api:voltaire:2026-08-06",
  "observed_at": "2026-08-06T14:02:11Z",
  "note": "hard bounce 550 5.1.1 on tristan@example.com"
}
```

| Field | Required | Meaning |
|---|---|---|
| `record` | yes | Literal `"correction"`. |
| `correction_id` | yes | See §5. Stable content hash; the batch-level dedupe and audit key. |
| `target` | yes | Entity identity. See §3.3. |
| `op` | yes (or envelope default) | `set` \| `add` \| `remove`. See §4. |
| `field` | yes (or envelope default) | Frontmatter key. Must pass the field allowlist (§6.3). |
| `value` | yes for `set`/`add`/`remove` | Scalar, or a dict for list-of-dict fields. |
| `source` | yes (or envelope default) | A `SourceRef` shorthand `<type>:<ref>`, parsed by `provenance.parse_source`. **This is the precedence input** (§6). An unparseable source is a park, not a fail-open downgrade — see §6.4. |
| `observed_at` | yes (or envelope default) | RFC-3339 UTC — when the submitter *observed* the fact, not when it wrote the batch. Feeds the same-tier tie-break. |
| `note` | optional | Carried into the audit ledger and into a pending question if one is raised. |

Unknown keys on a correction record are **rejected**, not ignored. Lane A's frontmatter
is deliberately open (tier-0 round-trips unknown keys); Lane C is a closed machine
protocol where a typo'd key silently dropping a constraint is a data-integrity bug.

### 3.3 Target identity

Exactly one of these three shapes:

| Shape | Example | Resolution |
|---|---|---|
| `{"uid": "…"}` | `{"uid": "person-jane-doe-a1b2c3d4"}` | `EntityIndex.get_by_uid`. Preferred — unambiguous. |
| `{"type": "person", "name": "…"}` | `{"type":"person","name":"Jane Doe"}` | `EntityIndex.lookup`, then the same cross-type guard `tier0_handle_upsert` applies: the matched page's `type` must equal the declared `type`, and the page must be in entity format. |
| `{"type": "person", "handle": {"<key>": "<value>"}}` | `{"type":"person","handle":{"email":"jane@acme.example"}}` | Resolved through `registry.json` (`docs/source-handles.md`). `<key>` must be a `SOURCE_HANDLE_KEYS` member or `emails`. A handle matching **more than one** entity is ambiguous → dead-letter (§7), never a guess. |

The handle shape exists because the outreach writers key on email addresses, not on
athenaeum uids. It is the shape voltaire and maecenas will actually use.

---

## 4. Operations

| `op` | Applies to | Semantics | Idempotent because |
|---|---|---|---|
| `set` | scalar field | Replace the field's value. | Re-setting the same value is a delta-gate no-op. |
| `add` | list field | Union the value into the list. | Value-identity dedupe — `repr(value)` for dicts, the value itself for scalars, matching the match key already locked in `provenance-shape.md` §2.2 and used by `dedupe._perform_merge`'s list-union. |
| `remove` | list field | Drop matching values from the list. | Removing an absent value is a no-op. |

`set` on a list field, or `add`/`remove` on a scalar field, is a **record-level reject**
(§8.3) — not a coercion. The list/scalar split per field comes from the allowlist (§6.3).

There is deliberately no `clear`, no `increment`, no `set_if_absent`. A writer needing
one of those either composes it from `set` (it knows the value it wants) or is doing
something that warrants prose (§9).

**`add` also maintains provenance.** When a correction adds a value to a list field, the
applier appends the co-indexed `{value, source}` entry to `field_sources.<field>` in the
per-value shape locked in `provenance-shape.md` §2.1, upgrading a legacy field-keyed
entry per §2.3's writer rule. `set` writes `field_sources.<field> = <source>` scalar.
`remove` prunes the dangling attribution, matching `_merge_field_sources`.

---

## 5. Idempotency

Two mechanisms, with distinct jobs.

**5.1 Semantic idempotency — the delta gate (primary).** Before writing, the applier
computes the merged frontmatter and compares it to what is on disk. **No delta → no
write, no `updated` bump, byte-for-byte stable page.** This is exactly
`tier0_handle_upsert`'s `changed = any(...)` gate, and it is what makes re-submitting a
correction free. A submitter that cannot cheaply tell whether its correction already
landed should simply re-submit; that is the designed behaviour, not a fallback.

**5.2 `correction_id` — the batch/audit key.** 

```
correction_id = sha256(
    canonical_json([schema_version, target_canonical, op, field, value_canonical])
).hexdigest()[:16]
```

where `target_canonical` is the target dict with keys sorted, and `value_canonical` is
the value rendered by a canonical JSON encoder (sorted keys, no insignificant
whitespace). **`source` and `observed_at` are deliberately NOT in the key** — the same
factual change proposed twice by the same system is the same correction regardless of
when it was observed.

The id is used for: (a) within-batch dedupe (a batch may legitimately contain the same
correction twice; the applier collapses them), (b) the applied-audit ledger, (c) naming
the record in a dead-letter or a pending question. It is **not** a global
applied-once ledger lookup — the delta gate already gives that, and a persistent
seen-set at this volume is cost without benefit.

**5.3 Audit ledger.** Every batch's disposition is appended to
`wiki/_corrections_applied.jsonl` — one line per batch with counts by disposition
(`applied`, `noop`, `dropped-lower-precedence`, `escalated`, `dead-lettered`,
`rejected`), plus the per-record ids for every non-`applied`/`noop` disposition. This is
the same append-only-JSONL discipline as `_merge_provenance.jsonl`
(`provenance.MERGE_PROVENANCE_FILENAME`). It is diagnostics, not control flow.

---

## 6. Conflict policy

### 6.1 The deterministic ranker (new)

`conflict-resolution.md` §11 states plainly: *"the taxonomy is enforced at PROMPT time
only — no deterministic winner-picker runs in-process."* The applier cannot call an LLM,
so this design introduces one.

New module **`src/athenaeum/precedence.py`**:

```python
SOURCE_PRECEDENCE_TIERS: tuple[str, ...] = (
    "user", "linkedin", "api", "wikipedia", "agent-observed",
    "claude", "script", "model-prior", "unsourced",
)

def source_rank(source: str | dict | None) -> int:
    """Return the 1-based precedence rank; 9 (``unsourced``) for None/unparseable."""
```

`twitter:` ranks with `linkedin:` at tier 2, as the taxonomy's own tier-2 line already
states. Any `<type>` not in the list ranks 9 — the same fail-open posture the taxonomy
takes for `unsourced` — **but see §6.4: for a correction that outcome is a park, not a
silent low-rank apply.**

> **DRIFT GUARD.** The tier list now exists in four places that must change together:
> 1. `SOURCE_PRECEDENCE_TIERS` in `src/athenaeum/precedence.py` (this design);
> 2. the `SOURCE-PRECEDENCE TAXONOMY` block of `_RESOLVE_SYSTEM` in
>    `src/athenaeum/resolutions.py` (the canonical prose list);
> 3. the `9-tier` count in `resolutions.py`'s module docstring;
> 4. the byte-exact golden snapshot `tests/data/resolve_system.txt`, pinned by
>    `tests/test_resolve_system_snapshot.py`.
>
> Plus §11 of `docs/conflict-resolution.md` and this section. The implementation issue
> MUST add a test asserting that `SOURCE_PRECEDENCE_TIERS` and the prompt block agree,
> so the two cannot drift silently — the ranker is a *second* encoding of a list that
> already had a drift guard, and adding an unguarded copy is how the guard dies.

### 6.2 The policy

For a correction targeting field `F` with source `S_in`, against the incumbent
attribution `S_cur` (read from `field_sources.<F>`, falling back to the page-level
`source:` when the field carries no attribution, and to `unsourced` when neither exists):

| Case | Disposition |
|---|---|
| `rank(S_in) < rank(S_cur)` (strictly more authoritative) | **Apply.** |
| `rank(S_in) > rank(S_cur)` (strictly less authoritative) | **Drop.** No write. Counted as `dropped-lower-precedence` in the ledger with both sources named. Not an error — this is the normal, expected outcome of a script proposing over a user-stated fact. |
| Equal rank, values equal | **No-op** (the delta gate catches it first). |
| Equal rank, differing values, distinguishable dates | **Newer `observed_at` wins** — the taxonomy's own tie-break. The incumbent's date comes from its structured `SourceRef.ts` when present, else the page's `updated`. |
| Equal rank, differing values, undated or equal-dated | **Escalate** — a `_pending_questions.md` entry (§6.5). Never a coin-flip. |
| Incumbent is `user:` (rank 1) and the correction is not | **Drop**, always. A human-stated field is never overwritten by a machine. This is the `rank(S_in) > rank(S_cur)` row restated because it is the row that matters most. |

`op: add` on a list field is evaluated **per value**, against that value's own co-indexed
`field_sources` entry if one exists — not against the list as a whole. Adding a backlink
to a list whose other entries came from a human is not a conflict.

### 6.3 The field allowlist, and the monotone-safety carve-out

A correction may only touch a field on the allowlist, declared in config
(`corrections.fields`) with three properties: `shape` (`scalar` | `list`), `writers`
(the submitters permitted to touch it), and `monotone` (bool).

The `writers` list is a blast-radius bound, not a trust model — the structural guarantee
is still that the librarian is the only writer. It stops an adapter bug from writing
`do_not_email` because a field name collided.

**Monotone-safety carve-out.** A field marked `monotone: true` (`bounced`,
`do_not_email`) may be **set** by any permitted writer regardless of precedence, and may
be **unset** only by a `user:`-tier correction. Rationale: these fields are suppression
flags where the two error directions are not symmetric. Failing to set one means mailing
a dead or opted-out address; failing to clear one means an email that does not go out.
The precedence ladder is designed for "which of these competing facts is true", which is
the wrong question for a safety flag. Every monotone apply is logged distinctly so the
carve-out is auditable rather than invisible.

### 6.4 Unparseable source — park, do not degrade

Lane A provenance validation is deliberately **fail-open**: a malformed `source_type`
downgrades to `inferred` with a breadcrumb rather than wedging the nightly compile
(`adapter-contract.md` §2). **Lane C inverts this for one specific reason:** in Lane A a
downgrade costs an attribution, while here the source *is* the authorization to write.
A correction whose `source` does not parse would rank 9 and thus lose every conflict —
but it would still win against an `unsourced` incumbent and write. So an unparseable
`source` is a **record-level reject** (§8.3), reported loudly to the submitter's batch
disposition. The fail-open posture is preserved everywhere the source is only
descriptive; it is refused where the source is load-bearing.

### 6.5 Escalation shape

An escalated correction writes a `_pending_questions.md` entry naming: the target page,
the field, the incumbent value + source, the proposed value + source + `observed_at`, the
submitter, the `correction_id`, and the record's `note`. It reuses the existing
pending-questions surface (and therefore `list_pending_questions` /
`resolve_question` on the MCP side) rather than inventing a parallel queue.

The escalated correction is **not** re-queued on disk. It has been surfaced; the human's
answer is the resolution path. If the submitter still believes it, it re-submits — which
is free (§5.1).

**Escalation is rate-capped** (`corrections.max_escalations_per_run`, default 50). A
mechanical writer with a systematic disagreement could otherwise flood the human queue
with thousands of identical questions in one night. On hitting the cap the applier stops
escalating, keeps applying and dropping normally, and emits a single loud summary line
naming the submitter and field with the highest escalation count — which is the
actionable signal anyway.

---

## 7. Unresolvable targets — the majority case

Issue athenaeum#794's own volume note implies most adapter-proposed backlinks will *not* resolve:
~9,135 of ~16,896 person records were measured structurally unlinkable on 2026-07-12. So
non-resolution is the common path and must be cheap.

**The applier never creates a page.** A correction whose target does not resolve —
unknown uid, name matching nothing, name matching a non-entity or cross-type page, handle
matching zero or more than one entity — is **dead-lettered**:

```
<knowledge-root>/raw/corrections/_unresolved/<submitter>/<batch_id>.jsonl
```

One line per unresolved record, each with a `reason` field appended
(`unknown-uid` | `name-no-match` | `name-cross-type` | `name-non-entity` |
`handle-no-match` | `handle-ambiguous`). `_unresolved/` is inside the reserved subtree
and is likewise skipped by Lane A discovery.

Dead-lettering is **not** an error and does not fail a batch. Dispositions are counted
and reported; the batch's `dead-lettered` count is the submitter's feedback channel.

**A dead-lettered correction is never escalated to prose intake.** Falling back to Lane A
would convert the majority case into per-record LLM compilation — precisely the cost this
contract exists to avoid, and it would do so at exactly the moment volume is highest.

The intended remedy is upstream: submitters pre-filter against `registry.json`
(`docs/source-handles.md` §4), which is a deterministic LLM-free index built for this
purpose. A submitter proposing corrections for entities absent from the registry is
asking a question the registry already answers. `_unresolved/` is swept and pruned by the
operator; it is a diagnostic surface, not a retry queue.

---

## 8. Volume envelope and scheduling

### 8.1 Where the phase runs

A new `_run_correction_phase(ctx)` in `librarian.run()`, ordered **after**
`_arm_run_deadline` and **before** `_run_entity_tier_phase`. It:

- makes **zero LLM calls** and consumes **zero** of `librarian_max_api_calls`;
- carries its own runtime share, `corrections.runtime_share` (default `0.05` of
  `librarian_max_runtime`), parallel to the existing `librarian_entity_runtime_share`;
- checks `ctx.deadline_exceeded()` between batches, and stops cleanly at a batch
  boundary — never mid-batch.

**Ordering is the point.** Issue [athenaeum#764](https://github.com/Kromatic-Innovation/athenaeum/issues/764)
records that C4 exhausts the wall-clock deadline every night and the corpus has not fully
compiled since 2026-08-02. Dropping thousands of corrections into an already-blown budget
would make it worse. Running the deterministic phase *first*, on a small fixed share,
means an overrun degrades the expensive LLM path — which is already degrading — and never
the cheap deterministic one. Corrections are the work most likely to be safety-relevant
(suppression flags) and least likely to be affordable to retry, so they go first.

### 8.2 The numbers

| Bound | Default | On exceeding |
|---|---|---|
| Records per batch file | 5,000 | Batch **parked** (§8.3). Split it. |
| Records applied per run | 50,000 | Remaining batches stay on disk; the run reports `carried-over: N batches`. Next run resumes FIFO by filename. |
| Batch file size | 32 MiB | Parked. |
| Escalations per run | 50 | Cap, with a summary line (§6.5). |

These are configuration defaults, not constants. The applied-per-run ceiling is a
safety rail against a runaway submitter, not a throughput target — 50,000 deterministic
frontmatter merges is minutes of work, not hours.

**Carry-over is FIFO and never starves.** Batches apply in filename (timestamp) order
across submitters, so a submitter that files one enormous nightly batch cannot indefinitely
delay a latency-sensitive one filed later — but it *can* delay it by one run, which is why
voltaire's bounce path is explicitly designed as batch/eventual (§10.1).

### 8.3 Park vs reject

- **Park (batch-level):** unknown `schema_version`, missing/malformed envelope,
  submitter/directory mismatch, oversized batch. The file is moved to
  `raw/corrections/_parked/<submitter>/` with a `_parked/<batch_id>.reason` sidecar and
  the run continues. Nothing in the batch is applied — a batch whose envelope cannot be
  trusted cannot have its records trusted either.
- **Reject (record-level):** unknown key, bad `op`/`field` combination, field not on the
  allowlist, submitter not in the field's `writers`, unparseable `source` (§6.4), missing
  required key. The record is skipped and written to the dead-letter file with its reason;
  the rest of the batch applies normally.

Neither ever raises. One malformed batch must not wedge the nightly compile — the same
posture the stuck-file ledger takes for Lane A.

---

## 9. Deterministic or prose? The submitter's decision rule

Submit a **field correction** when all of these hold:

1. You know the **exact target** (a uid, an exact name, or a registered handle).
2. You know the **exact field and value** — no interpretation, no summarization, no
   judgement about where the fact belongs on the page.
3. The field is on the allowlist and you are one of its `writers`.
4. The change is expressible as `set` / `add` / `remove`.

Submit **prose to Lane A** otherwise — in particular when:

- the fact is a narrative or an observation ("Jane mentioned she's leaving Acme") whose
  field placement is exactly the judgement the LLM tiers exist to make;
- the target may not exist yet and **should** be created (only Lane A creates pages);
- the change is relational or structural (a merge, a retype, a supersession) rather than
  a field value.

**If both apply, submit both.** They are not exclusive: voltaire's conversation archive
(voltaire#124) is prose, while the bounce flag from the same mailbox is a correction.
Both land, through different lanes, at different cost.

---

## 10. Worked examples

### 10.1 Bounce flag — voltaire (latency-tolerant by design)

`raw/corrections/voltaire/20260806T140211Z-9f3ac1d2.jsonl`

```jsonl
{"record":"batch","schema_version":1,"submitter":"voltaire","batch_id":"20260806T140211Z-9f3ac1d2","created_at":"2026-08-06T14:02:11Z","defaults":{"source":"api:voltaire:2026-08-06","observed_at":"2026-08-06T14:01:55Z"}}
{"record":"correction","correction_id":"3f9a2c81b7d4e065","target":{"type":"person","handle":{"email":"jane@acme.example"}},"op":"set","field":"bounced","value":"2026-08-06","note":"hard bounce 550 5.1.1"}
```

`bounced` is `monotone: true`, so this applies regardless of the incumbent attribution
(§6.3). Latency is batch/eventual — hours — and that is correct: outreach-architecture P2
puts the *hard* suppression window sender-side in voltaire's bounce-event ledger, which
maecenas consults at send time. The wiki is the durable record, not the low-latency one.
This contract is deliberately not engineered for real-time.

### 10.2 Backlink — athenaeum-adapters (the volume case)

```jsonl
{"record":"batch","schema_version":1,"submitter":"athenaeum-adapters","batch_id":"20260806T030000Z-1a2b3c4d","created_at":"2026-08-06T03:00:00Z","defaults":{"source":"script:backlink-writer","observed_at":"2026-08-06T03:00:00Z","op":"add","field":"backlinks"}}
{"record":"correction","correction_id":"a1…","target":{"uid":"person-jane-doe-a1b2c3d4"},"value":"company-acme-77aa11bc"}
{"record":"correction","correction_id":"b2…","target":{"uid":"person-john-roe-11ff22ee"},"value":"company-acme-77aa11bc"}
```

Envelope defaults carry `op`/`field`/`source`, so each record is three keys. At 5,000
records a batch is a few hundred KB. `script:` is rank 7 — near the bottom — which is
exactly right: an adapter-inferred backlink must lose to anything a human or an API
asserted. Targets are pre-filtered against `registry.json`; the residue dead-letters
(§7). This is issue [athenaeum-adapters#95](https://github.com/Kromatic-Innovation/athenaeum-adapters/issues/95).

### 10.3 Warm-tier enrichment field — apollo-enrich

```jsonl
{"record":"batch","schema_version":1,"submitter":"apollo-enrich","batch_id":"20260806T060000Z-5e6f7a8b","created_at":"2026-08-06T06:00:00Z","defaults":{"source":"api:apollo:2026-08-06","observed_at":"2026-08-06T05:58:40Z"}}
{"record":"correction","correction_id":"c3…","target":{"uid":"person-jane-doe-a1b2c3d4"},"op":"set","field":"current_title","value":"VP Engineering"}
{"record":"correction","correction_id":"d4…","target":{"uid":"person-jane-doe-a1b2c3d4"},"op":"add","field":"emails","value":"j.doe@acme.example"}
```

`api:apollo` is rank 3, so it overwrites a `claude:`/`script:`-sourced title but loses to
a `user:`- or `linkedin:`-sourced one. The `add` on `emails` writes the co-indexed
per-value `field_sources` entry (§4) — the exact shape `provenance-shape.md` §2.1 locks,
and the case that motivated it. This is issue
[apollo-enrich#29](https://github.com/Kromatic-Innovation/apollo-enrich/issues/29).

### 10.4 Contact-frequency rollup — maecenas (P5; see §11)

```jsonl
{"record":"batch","schema_version":1,"submitter":"maecenas","batch_id":"20260806T070000Z-2c3d4e5f","created_at":"2026-08-06T07:00:00Z","defaults":{"source":"script:maecenas-frequency-rollup","observed_at":"2026-08-06T07:00:00Z","op":"set"}}
{"record":"correction","correction_id":"e5…","target":{"uid":"person-jane-doe-a1b2c3d4"},"field":"last_contacted_at","value":"2026-08-04"}
{"record":"correction","correction_id":"f6…","target":{"uid":"person-jane-doe-a1b2c3d4"},"field":"contact_count_90d","value":7}
```

Two fields, not seven interaction events. The events live in the P5 store; only the
rollup reaches the page. Note the shape: this is the *whole* wiki-facing surface of P5 —
which is why the storage question resolves the way it does.

---

## 11. P5 — where the direct-outreach interaction log lives

**Decision: option (b) — a separate store outside the wiki — plus a per-person rollup
written onto the person page through the contract above.**

Outreach-architecture P5 poses the choice: (a) a separate linked interaction-log record
type *in* the wiki, or (b) a separate database. Two constraints already in the ratified
document discriminate, without needing to argue the general case.

**Discriminator 1 — does logging an interaction create a wiki record for the person?**
Maecenas can email someone who has no wiki page: a conference contact, a cold prospect, a
list subscriber who replied. Under option (a) the interaction-log record is a wiki record,
and it either dangles (a log record linked to nothing, which is worse than useless for the
relationship view the wiki exists to hold) or it forces creation of the person page.
Forcing creation violates P5's second constraint — *"no wiki records for mere list
subscribers"* — directly. Option (b) has no such coupling: the store keys on an email
address or a Contacts id, and a wiki page is created only when a relationship is
established, which is the rule P5 states.

This is the same rule as §5/§7 of this document, arrived at independently: **the applier
never creates a page.** That is not a coincidence — it is the same constraint expressed at
two layers.

**Discriminator 2 — are the queries semantic or structured?** The wiki's distinguishing
capability is semantic recall. Every P5 query is structured: *when did we last contact
this person*, *how many times in the last 90 days*, *which segment members are inside the
cadence cap*. Maecenas answers these at segment time over a person set, by
`(person, channel, date-range)`. Semantic recall buys nothing here, and the cost is real:
an interaction-log record type would add records to the corpus at direct-outreach volume,
on top of the compile budget athenaeum#764 already reports as exhausted. Option (a) charges the
most expensive part of the system for a capability the query pattern does not use.

**The shape that satisfies both.**

| Layer | Holds | Owner | Written via |
|---|---|---|---|
| Per-routine append-only ledger | Immediate send-time/segment-time checks | each sending routine | routine-internal |
| **P5 interaction store** (separate DB, outside `~/knowledge`) | Every direct-outreach event: person key, channel, date, campaign | maecenas (schema: cwc#2195) | routine → store, directly |
| **Person wiki page** | `last_contacted_at`, `contact_count_90d` — a rollup, two fields | librarian | **this contract** (§10.4) |
| Google Contacts | PII / contact truth | athenaeum-adapters, voltaire | Contacts API |

The person page keeps the *relationship*; the log lives elsewhere; Contacts keeps the PII
— which is precisely the three-way split P5's review note asks for, reached without a
fourth record type.

And this is why P1 and P5 belong in one design pass: the rollup is not a special mechanism.
It is the fourth worked example of the field-correction contract (§10.4). P5 needs no wiki-side
machinery that P1 does not already build.

**What (b) does not resolve, and who owns it:** the store's own schema, technology, and
location are cwc#2195's, not this document's. Two constraints bind it from here: it must
live outside a guard-rebuilt deploy checkout (architecture P8), and it must be able to
emit the rollup batch in §10.4.

**If a future requirement makes the log semantically recallable** — "what did we talk
about with Jane" rather than "when did we contact Jane" — that is voltaire's conversation
archive (voltaire#124, `EPIC voltaire#126`), which already goes to Lane A prose intake as a
*relationship* record. It is a different artifact from the frequency ledger, and routing
it differently is correct, not a gap.

---

## 12. Entry point — a sibling to `remember`, not `remember`

**Decision: a new MCP tool `propose_corrections`, plus the direct file drop of §3.**

`remember` is the wrong shape for this traffic on three counts: it writes **one file per
call** (a thousand corrections would be a thousand files and a thousand tool round-trips);
it runs **per-call sensitive-content screening** to stamp an `access:` label, which is
meaningful for prose and meaningless for `{"field":"bounced","value":"2026-08-06"}`; and
it **injects prose provenance frontmatter**, a shape corrections do not have. Overloading
it would mean a `sources=`-style disambiguation branch on a boundary that
`provenance-shape.md` §4.2 already had to de-ambiguate once.

```text
propose_corrections(
  submitter="voltaire",
  corrections=[{...}, {...}],   # the §3.2 record shape
  defaults={"source": "api:voltaire:2026-08-06"},
)
```

It validates the envelope and each record synchronously, writes **one** batch file, and
returns `{batch_id, accepted, rejected: [{index, reason}]}`. Validation is synchronous and
application is not: the tool tells a caller its batch is *well-formed*, never that it was
*applied*. Application happens in the next `athenaeum run`.

Cron writers with no MCP client use the direct file drop, exactly as Lane A permits
(`adapter-contract.md` §5b). The two paths produce byte-identical files; the MCP tool is
validation convenience, not a privileged channel.

---

## 13. Migration is forward-only

Per the ratified architecture (TK, 2026-08-06): **correctly-executed writes already in the
wiki stand.** The P1 debt is the write *paths*, not the content they produced. No
consumer's migration issue includes a retroactive cleanup pass, and none should be filed.
A page written directly by adapters last month is not defective; the mechanism that wrote
it is what changes.

The one caveat worth stating: those existing values largely carry no `field_sources`
attribution, so they read as `unsourced` (rank 9) to §6.2 and will lose to essentially any
correction. That is the correct outcome — an unattributed legacy value *should* yield to an
attributed new one — but it means the first correction batch against a page is more likely
to write than a steady-state one. Expected, not a defect.

---

## 14. What this document does not decide

- **The librarian fast-path implementation.** Follow-up issue in this repo (athenaeum#794 AC #7).
- **The frequency-ledger schema.** cwc#2195. This contract only carries the rollup.
- **Per-consumer migration.** Each has its own issue in its own repo, `blocked_by` athenaeum#794:
  athenaeum-adapters#95, apollo-enrich#29, voltaire#122, voltaire#124, maecenas#59, maecenas#64.
- **Body-level corrections.** This contract is frontmatter-only. A correction to prose in a
  page body is a Lane A submission.
- **Retraction/undo of an applied correction.** The applied ledger (§5.3) makes one
  reconstructible, and the knowledge store is git-versioned, but no tooling is specified
  here.

---

## See also

- [`docs/adapter-contract.md`](adapter-contract.md) — Lanes A and B; §3 there is the
  Lane A idempotency rule this document's §5 parallels.
- [`docs/provenance-shape.md`](provenance-shape.md) — §2 per-value `field_sources`
  (the shape §4 writes), §4 the `remember` boundary (the one §12 declines to overload),
  §10 the source-type vocabulary.
- [`docs/conflict-resolution.md`](conflict-resolution.md) — §11 the source-precedence
  taxonomy (prompt-only until §6.1 of this document), §4 the last-write-wins
  single-writer assumption that §1 explains this contract must not violate.
- [`docs/source-handles.md`](source-handles.md) — `registry.json`, the pre-filter that
  keeps §7's dead-letter rate down.
- `code-workspace-config/docs/outreach-architecture.md` — P1 (this contract's mandate),
  P2 (bounce ownership), P5 (§11), P8 (the store-location constraint on cwc#2195).
