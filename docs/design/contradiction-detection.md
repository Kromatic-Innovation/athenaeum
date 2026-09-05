# Contradiction Detection — Pipeline, Modes, Precedence, and Configuration

This document describes the full contradiction-detection-and-resolution pipeline
that landed via PRs athenaeum#125 (cross-scope toggle), athenaeum#126 (Opus resolver and
provenance precedence), and athenaeum#128 (pending-questions sidecar surface).

It is the operator reference for: which stage runs when, what each stage
costs, what knobs change behavior, and what the resulting block in
`_pending_questions.md` looks like.

For adjacent material:

- Per-claim provenance and `field_sources` shape — see
  [`docs/design/provenance-shape.md`](provenance-shape.md).
- The full audit-locked catalog of every place in the librarian where two
  values disagree (Tier 0 / Tier 3 / dedupe / merge frontmatter) — see
  [`docs/design/conflict-resolution.md`](conflict-resolution.md). This document
  covers ONLY the auto-memory cluster path; principled Tier 3 contradictions
  flow through `tier3_merge` and live in that adjacent doc.

---

## The five-verdict comparator (athenaeum#715) — what replaces this, and when

Everything below/above describes the **split paths**: duplicate detection
(`merge.py`) and contradiction detection (`contradictions.py`) as two
separate operations over the same clusters. That split is the defect
athenaeum#715 exists to remove — it is how a 2,207-source cluster could be a
merge candidate while the contradiction detector reported zero conflicts.

**The replacement is ONE comparison returning FIVE verdicts**
(`src/athenaeum/comparator.py`):

```
duplicate | contradiction | specialization | distinct | underdetermined
```

with two gates, cheapest first:

- **Gate 1 — typed, free, no LLM.** Consults only the KNOWN coordinates of
  SEPARATOR dimensions (athenaeum#714's registry) that are `enforced` and whose
  `applies_to` matches both sides. Any `disjoint` relation exits immediately
  as DISTINCT. **Sequencers (`observed-time`, `recorded-time`) are excluded
  by construction** — they order beliefs about one territory and feed
  supersession, they never make two claims DISTINCT.
- **Gate 2 — `content_relation`, the ONE LLM judgement, and it runs LAST**,
  only on pairs Gate 1 could not settle. It returns `equivalent |
  conflicting | compatible`, judged **cold** (no exemplar/few-shot channel;
  corpus page bodies are untrusted data), with conflicts **located** to
  passages rather than a page-global verdict. `compatible` is the answer to
  "these answer different questions about the same subject" — the measured
  priority-vs-lifecycle false-conflict class — and yields DISTINCT(coexist).

Every verdict is memoized in the athenaeum#712 verdict ledger, so a pair whose
verdict is fresh is never re-compared and never re-spends an LLM call.
**There are no confidence thresholds anywhere** in the new path: model
self-reported confidence never ranks correctness (the two highest-confidence
historical merge proposals were both wrong, at 0.84 and 0.82, while the one
verified-correct cluster sat at 0.77), and similarity's only remaining job is
proposing which pairs to compare.

### Status: partially cut over

The comparator and its verdict effects (auto-supersession with its
partial-order authority treatment and rate limits, evidence-artifact fold
proposals, the `compatible` TTL re-check, sibling-scope widening probes) are
**built and tested, gated off by default** behind `librarian.comparator_enabled`
(default `false`) — see
[`docs/reference/configuration.md`](../reference/configuration.md#five-verdict-comparator-athenaeum715--off-by-default).

**The pipeline below (raw `auto-memory` intake → C1-C4 → this document's
Haiku-detect/Opus-resolve split) is UNCHANGED and still describes live
behaviour.** athenaeum#715's cut-over so far only replaces the OTHER old
duplicate-detection path — `athenaeum.wiki_dedupe`'s wiki-page-vs-wiki-page
dedup pass (a separate pass from the diagram below: it compares
already-COMPILED `wiki/*.md` pages against each other, not raw intake). That
pass's own confidence/suppression-gate algorithm is deleted outright and
replaced by the comparator, gated on the SAME `librarian.comparator_enabled`
knob. The C1-C4 pipeline's own intra-cluster contradiction detector
(`athenaeum.merge`'s `merge_clusters_to_wiki`, described in full below) is
**still the old path** — deeply interleaved with run-level deadline
checkpointing, the detection-incomplete retry queue, and the shared
API-call/spend budget across multiple `librarian.py` call sites, so
retiring it safely is scoped as its own follow-up rather than folded into
this pass. Until it lands, the other live reader of the new path (besides
`athenaeum.wiki_dedupe` above) is the explicit, opt-in
`athenaeum merges recompare` command, which re-runs the comparator over the
existing pending merge proposals and records a verdict per source pair —
dry-run by default, and with no path to approving a merge at all.

---

## 1. Pipeline overview

```
raw/auto-memory/<scope>/
     │
     │  athenaeum ingest  (librarian)
     ▼
clusters (per-scope by default — clusters.py)
     │
     │  cross-scope mode toggle  (athenaeum#125)
     ▼
pooled clusters / similarity pairs
     │
     │  Haiku detect (per-cluster, fast)
     │  contradictions.py:detect_contradictions
     ▼
ContradictionResult (detected? type? members? passages? rationale?)
     │
     │  Opus resolve (per-detected, capped)  (athenaeum#126)
     │  resolutions.py:propose_resolution
     ▼
ResolutionProposal (winner? action? rationale? confidence? precedence?)
     │
     │  tier4_escalate
     ▼
~/knowledge/wiki/_pending_questions.md
     │
     │  athenaeum questions  /  SessionStart hook  (athenaeum#128)
     ▼
user accepts / overrides / defers
     │
     │  resolve_question MCP tool  (writes a decision-answer file, athenaeum#908)
     ▼
raw/answers/{ISO-TS}-question-{id}.md
     │
     │  athenaeum ingest-answers  (tier 0 — deterministic, no LLM call)
     ▼
answer applied: [x] mark flipped, then ingested back into raw/, source
written back, block archived
```

Stage-by-stage:

| Stage | Module | Cost class | Output |
|-------|--------|-----------|--------|
| Cluster | `athenaeum.clusters` | embedding (free, local chromadb) | per-scope clusters |
| Cross-scope toggle | `athenaeum.cross_scope` | embedding only | pooled clusters / candidate pairs |
| Detect | `athenaeum.contradictions` | Haiku per cluster | `ContradictionResult` |
| Resolve | `athenaeum.resolutions` | Opus per detection (capped) | `ResolutionProposal` |
| Escalate | `athenaeum.tiers.tier4_escalate` | none | block in `_pending_questions.md` |
| Surface | `athenaeum questions` CLI + hook | none | SessionStart prompt |
| Record | `resolve-questions` skill + `resolve_question` MCP | none | decision-answer file under `raw/answers/` (deferred, athenaeum#908) |
| Apply | `athenaeum ingest-answers` (tier 0) | none — no LLM call | `[x]` mark + answer ingested + archived |

Issue athenaeum#908: `resolve_question` no longer flips the checkbox itself. It
validates the id against the CURRENT state of `_pending_questions.md`
(unknown / already-answered fails immediately, nothing written) and then
writes a **decision-answer file** — the same conformant raw-intake record
covered in "Decision-answer files" below. The actual checkbox flip, source
write-back, fingerprint recording, and archival all happen deterministically
on the next `athenaeum ingest-answers` run, in the same run-locked pass that
already did the archival step. This is a **behavior change**: a caller that
previously treated a successful `resolve_question` response as "the state
already changed" must now wait for the next tick — the response's
`deferred: true` field and `answer_file` path make this explicit.

Each stage degrades gracefully when its successor is unavailable. No
`ANTHROPIC_API_KEY` → detector returns `detected=False` with rationale
`llm-unavailable`; resolver returns the deterministic fallback
(`action=retain_both_with_context, confidence=0.0`) which renders to NO
trailing block, so the entry shape stays byte-identical to the pre-athenaeum#126
escalation format. The pipeline never blocks ingest on contradiction work.

---

## 2. Cross-scope detection modes

`athenaeum.cross_scope.resolve_cross_scope_mode` reads
`ATHENAEUM_CROSS_SCOPE_MODE` (env wins) then
`contradiction.cross_scope_mode` from `athenaeum.yaml`. Default is
`ancestor`.

### `off`

Per-scope clusters only. Equivalent to the pre-athenaeum#125 behavior. Use when:

- You're paying explicit attention to detector cost on a noisy ingest.
- You've accepted that two raw entries living in different
  `raw/auto-memory/<scope>/` directories won't be compared even when they
  state opposing things.

### `ancestor` (default)

Each per-scope cluster is pooled with members from any *ancestor* scope
before the detector runs. Scope identifiers follow the
`-Users-tristankromer-Code-foo` convention (slashes replaced with dashes);
ancestors are produced by dropping trailing segments. So a cluster from
`-Users-tristankromer-Code-foo` is pooled with members from
`-Users-tristankromer-Code` and `-Users-tristankromer` and `-Users`.

This catches the most common contradiction shape: a *general rule* the
user stated at the workspace level conflicts with a *project override* the
user stated inside one project.

Pooled cluster size is bounded by `cluster_size_cap` (default 25). Larger
pools are sorted newest-first by frontmatter `created` (or mtime fallback)
and split into chunks of `<= cap`. Each chunk runs ONE Haiku call. Cost:
the same number of Haiku calls per cluster as `off`; modestly larger
prompt payloads.

### `similarity`

Per-scope clusters PLUS a second pass: a cosine-similarity cross-product
over the recall-index embeddings for both `raw/auto-memory/**` AND
`wiki/**`. Any pair above `similarity_threshold` (default 0.85) that is
NOT already inside a single cluster is fed to the detector as a 2-member
pseudo-cluster.

This catches:

- Cross-tree-branch contradictions where two scopes are siblings (no
  ancestor relationship), e.g. `-Users-tristankromer-Code-foo` and
  `-Users-tristankromer-Code-bar`.

**Known limitation (issue athenaeum#262):** wiki-vs-wiki pairs are NOT compared.
`cross_scope_similarity_pairs`'s `require_raw_side` defaults `True` and is
passed `True` at its only call site (`merge.py`), so any candidate pair
where BOTH sides are wiki entries is dropped — this removes the
O(corpus²) wiki-vs-wiki adjudication term. Two compiled `wiki/auto-*.md`
pages that drift apart AFTER their raw originals are merged-and-deleted
will NOT be caught by this sweep; re-detection targets new raw intake
only.

Cost: roughly **2× Haiku calls in the worst case** — one for the per-scope
pass and one per candidate pair. The threshold is the cost lever.
Embeddings themselves are free (local chromadb, already populated by the
recall index build).

### `both`

Ancestor pooling first, similarity sweep second. Pairs already contained
in a single ancestor-pooled cluster are excluded from the sweep
(`covered_pair_keys` in `merge.py`), so `both` is NOT pure superset cost
of `similarity`; it's `ancestor` plus a *reduced* sweep over the residual.
Use when contradiction completeness genuinely matters more than
detector-cost — e.g. on large knowledge bases that have grown
multi-project structure under one user.

### Picking a mode

| Goal | Mode |
|------|------|
| Lowest cost; accept gap | `off` |
| Catch general-rule-vs-project-override | `ancestor` (default) |
| Catch cross-tree-branch (siblings); wiki-vs-wiki NOT covered (athenaeum#262) | `similarity` |
| Maximal coverage | `both` |

---

## 3. Source-precedence taxonomy

When the detector flags a contradiction, the Opus resolver
(`athenaeum.resolutions.propose_resolution`) compares the two members'
`source:` frontmatter values against this taxonomy:

```
1. user:<conversation-ref>      — user said it directly. Highest authority.
2. linkedin:<...> / twitter:<...> — user-curated public profile.
3. api:apollo / api:<vendor>    — third-party authoritative source.
4. wikipedia:<page>             — consensus public source.
5. agent-observed:<model>:<session-ref> — an AI derived it from an in-session
   artifact it READ (file contents, tool output), verifiable against the
   transcript. Ranks below external/consensus sources (not a curated
   authority) but above claude:tier3/inferred (grounded in a real artifact,
   not an unsupported leap).
6. claude:tier3-...             — LLM-generated. Subordinate to any human/external source.
7. script:<slug>                — pipeline-generated, no upstream evidence.
8. model-prior:<model-id>       — asserted from training-data knowledge with
   no session evidence. Unverifiable and silently stale past the model
   cutoff, so ranks below script: (a pipeline slug at least names a
   repeatable in-tree process).
9. unsourced / empty            — always loses to any sourced claim.
```

**Tie-break.** When two claims sit at the same precedence tier, prefer the
*newer* source date.

The resolver receives each member's `source:` value, the relevant
`field_sources.<key>` slice when present, and the conflicting passages.
The full body is also included when it fits under the configured token
budget. Token economy is enforced at prompt assembly.

### Worked example — "Tristan is German"

Two auto-memory files cluster together because both discuss Tristan's
nationality. Their frontmatter:

```yaml
# raw/auto-memory/-Users-tristankromer-Code/auto-tristan-nationality-2026-04-10.md
---
type: claim
name: Tristan nationality
source: claude:tier3-classify-2026-04-08
---
Tristan is German.
```

```yaml
# raw/auto-memory/-Users-tristankromer-Code/auto-tristan-citizenship-2026-04-10.md
---
type: claim
name: Tristan citizenship
source: user:session-2026-04-10-rosie-intake
---
Tristan holds American and British citizenship; not German.
```

Haiku detector emits:

```json
{
  "detected": true,
  "conflict_type": "factual",
  "members_involved": ["-Users-.../auto-tristan-nationality-2026-04-10.md",
                       "-Users-.../auto-tristan-citizenship-2026-04-10.md"],
  "conflicting_passages": ["Tristan is German.",
                           "Tristan holds American and British citizenship; not German."],
  "rationale": "Members state incompatible facts about Tristan's nationality."
}
```

Opus resolver receives the two `source:` values, compares them, and
returns:

```json
{
  "recommended_winner": "b",
  "action": "keep_b",
  "rationale": "User-direct statement (precedence 1) overrides Claude-generated tier3 classification (precedence 6).",
  "confidence": 0.95,
  "source_precedence_used": ["a:claude:tier3-classify-2026-04-08 > b:user:session-2026-04-10-rosie-intake (b wins, tier 1 > tier 6)"]
}
```

Resulting block in `~/knowledge/wiki/_pending_questions.md`:

```markdown
## [2026-04-10] Entity: "tristan-nationality" (from wiki/auto-tristan-nationality.md)
- [ ] Resolve contradiction in cluster auto-tristan-nationality.
**Conflict type**: factual
**Description**: Members state incompatible facts about Tristan's nationality.
Passage 1: Tristan is German.
Passage 2: Tristan holds American and British citizenship; not German.
Members involved: -Users-.../auto-tristan-nationality-2026-04-10.md, -Users-.../auto-tristan-citizenship-2026-04-10.md
**Proposed resolution**: keep_b
**Confidence**: 0.95
**Rationale**: User-direct statement (precedence 1) overrides Claude-generated tier3 classification (precedence 6).
**Source precedence**: a:claude:tier3-classify-2026-04-08 > b:user:session-2026-04-10-rosie-intake (b wins, tier 1 > tier 6)
```

The user remains the final authority. The four `**Proposed resolution**`
keys are advisory only; `resolve-questions` and the MCP `resolve_question`
tool both require explicit user confirmation before applying.

---

## 4. Configuration reference

Detection keys live under `contradiction:` in `athenaeum.yaml`; the detector
model under the top-level `models:` block (`models.classify`, athenaeum#232); the
resolver knobs under the top-level `resolve:` block. Env vars override the
yaml; the yaml overrides built-in defaults. The canonical knob table — every
env var, yaml key, and code default for this pipeline (`models.classify`,
`resolve.model`, `contradiction.cross_scope_mode`, `cluster_size_cap`,
`similarity_threshold`, `resolve_max_per_run`,
`resolved_similarity_threshold`, `ATHENAEUM_TIER4_DEDUP`,
`ATHENAEUM_PQ_SNOOZE_HOURS`, and the auto-apply lane) — lives in
[`docs/reference/configuration.md`](../reference/configuration.md#contradiction-detection-and-resolver).

Notes:

- `ATHENAEUM_RESOLVE_MAX_PER_RUN` accepts non-negative integers. Negative
  or non-numeric values fall back to `250`. Setting it to `0` disables the
  resolver entirely; every detection escalates without a proposal.
- The per-ingest cap is enforced in `merge.py:_maybe_propose` (a closure
  in `merge_clusters_to_wiki`), NOT in `propose_resolution` itself.
  `propose_resolution` is the same function on every call; only the
  orchestrator counts.

Example `athenaeum.yaml`:

```yaml
search_backend: vector

resolve:
  model: claude-opus-4-7

contradiction:
  cross_scope_mode: both          # tightest coverage
  cluster_size_cap: 25
  similarity_threshold: 0.85
  resolve_max_per_run: 250
```

---

## 5. Pending-questions integration (athenaeum#128)

Once the librarian writes a block to `~/knowledge/wiki/_pending_questions.md`,
the question is durable but invisible until the user opens that file.
The athenaeum#128 surface closes that gap.

### CLI

```bash
athenaeum questions count [--json]
athenaeum questions next  [--with-proposal] [--json]
athenaeum questions list  [--with-proposal] [--limit N] [--json]
```

`count` returns `N unresolved (oldest: <iso-date>)`. `next` returns the
oldest unresolved entry as one block. `list` walks them all. With
`--with-proposal`, each block includes the four `**Proposed resolution**`
keys when present. JSON output is stable (`{id, entity, source, question,
conflict_type, description, created_at, proposal}`) so hooks and skills
can rely on the shape.

The CLI is fail-silent on missing or empty `_pending_questions.md` — the
SessionStart hook depends on this.

### SessionStart hook

`examples/claude-code/pending-questions-surface.sh` is a Bash hook that:

1. Honors `~/.cache/athenaeum/pending-questions-snoozed-until` (ISO-8601
   UTC; lexicographic compare against `date -u +%FT%TZ`). If snoozed,
   exits silently.
2. Calls `athenaeum questions count --json`.
3. Prints a one-block prompt to stdout when count > 0.

Wire it into `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [{
      "hooks": [{
        "type": "command",
        "command": "/path/to/pending-questions-surface.sh 2>/dev/null || true",
        "timeout": 5
      }]
    }]
  }
}
```

Every external call uses `|| true`; a malformed pending-questions file
must NEVER block session startup.

### `resolve-questions` skill

`.claude/skills/resolve-questions/SKILL.md` is the interactive walk-through
the SessionStart hook points at. Six-step flow:

1. `athenaeum questions count --json`. If 0 — stop.
2. Loop: `athenaeum questions next --with-proposal --json`, render to user.
3. Ask: **accept** / **override** / **defer** / **stop**.
4. **accept** → call MCP `resolve_question(id, answer=<action-from-proposal>)`.
5. **override** → ask user for answer text → call `resolve_question`.
6. **defer** → write the snooze cache for `ATHENAEUM_PQ_SNOOZE_HOURS`
   (default 24) hours ahead.

Snooze cache write contract — match `date -u +%FT%TZ` exactly:

```bash
mkdir -p ~/.cache/athenaeum
date -u -v+24H +%FT%TZ > ~/.cache/athenaeum/pending-questions-snoozed-until
# GNU date: date -u -d '+24 hours' +%FT%TZ
```

Resolved (`[x]`) entries are archived by `athenaeum ingest-answers` on the
next librarian run; the skill does not handle archival. Issue athenaeum#908: as
of this change the checkbox flip itself is also deferred to that same
`ingest-answers` run — see "Decision-answer files" below.

### Decision-answer files (unified decision resolution as intake, athenaeum#908)

`athenaeum.decisions.list_pending_decisions` already joins six decision
types (`question`, `merge`, `retraction`, `audit`, `quarantine`,
`proposed-rule` — the last added by issue athenaeum#905) into one
outbound queue (`athenaeum decisions` / `list_pending_decisions` MCP). The
path back IN used to be per-type: three MCP tools each mutated their own
store directly. **Decision-answer files** make the inbound path uniform,
extending the existing `raw/answers/*.md` raw-intake convention with the
fields needed to name which decision an answer resolves:

```yaml
---
source: decision_answer
decision_id: 3f2a9c1d0e4b
decision_type: question   # question | merge | audit | proposed-rule
verdict: "keep_a: the 2026 recap was a down-round, not a change of stage."
note: ""                  # optional
resolved_at: 2026-08-14T20:00:00Z
---
```

- **`decision_id`** — the id from `list_pending_decisions`. The live id
  spaces (question ids from `answers._make_id`, merge ids from
  `pending_merges._make_id`) are same-length sha1 prefixes from UNRELATED
  key spaces with no cross-type uniqueness check anywhere.
- **`decision_type`** is REQUIRED — because the id spaces above can
  collide, an id alone cannot tell the applier which store to look in.
- **`verdict`** is the per-type decision token: for `question` the answer
  body (as today); for `merge` and `proposed-rule`, `approve` or `reject`;
  for `audit`, the human verdict compared against the tier's original
  verdict.

A record with **no `decision_id`** is a legacy `pending_question_answer`
provenance file (the pre-athenaeum#908 output of `ingest_answers` — an audit
trail, never an input) or anything else that happens to live in
`raw/answers/`. It parses exactly as it always has; the decision-answer
applier leaves it untouched.

**Applying is tier 0**: `athenaeum ingest-answers` applies every pending
decision-answer file (`athenaeum.decision_answers.apply_decision_answers`)
in the same run-locked pass that already ran the legacy question-answer
ingest — deterministically, with **no LLM call, ever**. Dispatch per
`decision_type`:

| `decision_type` | Applier | Effect |
|---|---|---|
| `question` | `athenaeum.answers.resolve_by_id` | flips the checkbox; the legacy `ingest_answers` pass immediately after completes the write-back + archival |
| `merge` | `athenaeum.pending_merges.resolve_merge` | the full approve/reject apply (wiki write, wikilink rewrite, source deletes, provenance) |
| `audit` | `athenaeum.calibration.record_audit_review` | appends the review record to the calibration ledger |
| `proposed-rule` | `athenaeum.rule_proposals.approve_rule_proposal` / `reject_rule_proposal` | `verdict: approve` writes the stored, already-drafted rule YAML into `<knowledge_root>/rules/` in **observe mode** and appends an `approve` record; `verdict: reject` appends a `reject` record, permanently suppressing that shape (issue athenaeum#905). `knowledge_root` is derived as `wiki_root.parent`. An unknown or already-resolved proposal id is caught and skipped, same fail-soft contract as every other type (issue athenaeum#921). |

**Fail-soft, idempotent, no bookkeeping needed**: an unknown decision id, an
already-resolved decision id, an invalid verdict, or a schema-malformed
answer file is logged and skipped — the file is **never deleted** (it stays
as its own audit trail) and the rest of the batch is unaffected. Re-applying
an already-applied file on a later tick is simply another "already
resolved" skip, because each underlying resolver (`resolve_by_id`,
`resolve_merge`, `record_audit_review`) already refuses to re-mutate an
id it has already settled — no separate "applied" ledger is needed.

**The three mutator MCP tools are now thin conveniences.** `resolve_question`
/ `resolve_merge` / `review_audit_item` each validate the id against
CURRENT state first (so an unknown id, an already-resolved id, or an
invalid verdict/decision still fails immediately with the same
`error_code` contract as before, and nothing is written on that path), then
write a decision-answer file instead of mutating state directly. A
successful response now includes `deferred: true`, `answer_file` (the
path written), and `decision_id` — the state change itself happens on the
next `ingest-answers` tick, not synchronously. `resolve_merge`'s response
in particular no longer carries `folded_sources` / `aliases_added` /
`links_rewritten` on success, since the fold hasn't happened yet — those
appear (via the same wiki-visible state) only after the next tick applies
the file.

**One deliberate divergence**: `athenaeum calibration review` (the CLI
twin of `review_audit_item`) still calls `record_audit_review` directly
and immediately. athenaeum#908's scope named only the three MCP mutators; the CLI
path was left un-deferred on purpose. The end state is identical either
way, just immediate rather than deferred.

### Voltaire briefing surface (Tristan-specific)

For users running a companion async agent, the companion side of the
integration surfaces the same pending-questions stream into the morning briefing. The
two surfaces are independent: the SessionStart hook fires when Claude Code
starts a new session; the Voltaire briefing fires on the morning cron.
Either one (or both) can be enabled.

---

## 6. Cost model

Pricing ranges are bands; consult Anthropic's published rates for the
authoritative current numbers.

### Per-call

- **Detector (Haiku).** `claude-haiku-4-5` at the time of writing prices
  around **$1/MTok input, $5/MTok output**. A typical cluster prompt is
  the system message (~600 tokens) plus 2–25 members at up to
  `PER_MEMBER_BODY_CHARS = 800` chars (~200 tokens) each — call it
  ~3 KB / ~750 input tokens for a 5-member cluster. Output is a small
  JSON object (~150 tokens). Cost: **fraction of a cent per call (~$0.005
  order of magnitude).**

- **Resolver (Opus).** `claude-opus-4-7` is roughly **$15/MTok input,
  $75/MTok output**. The resolver prompt is small by design (sources,
  conflicting passages, and each member's full body when it fits under
  the configured token budget) — a few hundred tokens in, a few
  hundred out. Cost: **~$0.05–$0.10 per call.**

### Per-ingest upper bound

```
total_cost ≈ cluster_count × $0.005
           + min(detected_count, RESOLVE_MAX_PER_RUN) × $0.10
           + similarity_pair_count × $0.005     # only when mode in {similarity, both}
```

Worked example (200-cluster ingest, 5% detection rate, mode=`ancestor`,
cap=50):

```
200 × $0.005          = $1.00   (detector)
10  × $0.10           = $1.00   (resolver — 5% × 200 = 10, well under cap)
similarity_pair_count = 0       (ancestor mode, no sweep)
                       ───────
                        $2.00 / run
```

Same ingest at `mode=both` with similarity_threshold=0.85 yielding ~30
candidate pairs:

```
200 × $0.005 + 10 × $0.10 + 30 × $0.005 = $2.15
```

### Cost levers

- `cross_scope_mode=off` — eliminates the per-scope pool growth and any
  similarity sweep. Worst-case cluster_count, lowest per-call cost.
- `similarity_threshold` (raise from `0.85` to e.g. `0.92`) — reduces
  candidate pair count quadratically against embedding density; the
  primary cost lever for `similarity` and `both` modes.
- `resolve_max_per_run` — caps Opus spend on a noisy ingest at the price
  of degraded escalations (no `**Proposed resolution**` block). The
  detection still happens; just no advisory winner.
- `ATHENAEUM_RESOLVE_MODEL` — substitute Sonnet or Haiku for Opus. Output
  quality on precedence reasoning drops; cost drops 5–20×. Tested only
  with Opus by default.

Embedding I/O is free (local chromadb, already populated by the recall
index build); no extra embedding work runs in this pipeline.

---

## Decisions

### Why `ancestor` is the default (not `off`)

`off` accepts a known coverage gap on the most common contradiction shape
(general workspace rule vs. project override). `ancestor` closes that gap
at the same Haiku call count — the only added cost is modestly bigger
prompt payloads from pooling ancestor-scope members. The trade was
"detect more contradictions for free" vs. "save the prompt-bytes cost on
larger clusters", and detection won.

### Why Opus for resolution (not Sonnet/Haiku)

The resolver is the one place in the pipeline where a small subset of
hard cases — disambiguating "user said this directly" vs. "Claude
classified it" with field-source slices — benefits materially from the
stronger model. Haiku and Sonnet were tested informally and produced
weaker rationales on the precedence-tier comparison. Cost is bounded by
`RESOLVE_MAX_PER_RUN`, so the per-ingest envelope is predictable even on
the most expensive model.

The model is configurable (`ATHENAEUM_RESOLVE_MODEL` /
`resolve.model`) so an operator can substitute a cheaper
model when the cost tradeoff is unacceptable.

### Why the per-run cap defaults to 250

A guard against runaway cost on a noisy ingest. The cap was raised from
50 to 250 in issue athenaeum#187 so a full-knowledge-base ingest no longer
exhausts the confirmation pass partway through. 250 × ~$0.10 = ~$25 of
worst-case Opus spend per run; high enough that real workloads rarely
hit the cap, low enough that a buggy detector returning `detected=true`
on every cluster cannot empty the operator's credit balance overnight.
Set higher (or to a very large value) when working through a backlog;
set to `0` to disable the resolver entirely and accept all escalations
without proposals.

### Why the snooze TTL defaults to 24 h

Aligns with the daily nightly-cron cadence used for librarian rebuilds
and any companion surfaces (e.g. the Voltaire morning briefing). A
snooze written today re-surfaces tomorrow at the next session start —
matching the natural review rhythm of "I'll deal with that in the
morning". Configurable via `ATHENAEUM_PQ_SNOOZE_HOURS` for shorter or
longer review cycles.
