# Contradiction Detection — Pipeline, Modes, Precedence, and Configuration

This document describes the full contradiction-detection-and-resolution pipeline
that landed via PRs #125 (cross-scope toggle), #126 (Opus resolver and
provenance precedence), and #128 (pending-questions sidecar surface).

It is the operator reference for: which stage runs when, what each stage
costs, what knobs change behavior, and what the resulting block in
`_pending_questions.md` looks like.

For adjacent material:

- Per-claim provenance and `field_sources` shape — see
  [`docs/provenance-shape.md`](provenance-shape.md).
- The full audit-locked catalog of every place in the librarian where two
  values disagree (Tier 0 / Tier 3 / dedupe / merge frontmatter) — see
  [`docs/conflict-resolution.md`](conflict-resolution.md). This document
  covers ONLY the auto-memory cluster path; principled Tier 3 contradictions
  flow through `tier3_merge` and live in that adjacent doc.

---

## 1. Pipeline overview

```
raw/auto-memory/<scope>/
     │
     │  athenaeum ingest  (librarian)
     ▼
clusters (per-scope by default — clusters.py)
     │
     │  cross-scope mode toggle  (#125)
     ▼
pooled clusters / similarity pairs
     │
     │  Haiku detect (per-cluster, fast)
     │  contradictions.py:detect_contradictions
     ▼
ContradictionResult (detected? type? members? passages? rationale?)
     │
     │  Opus resolve (per-detected, capped)  (#126)
     │  resolutions.py:propose_resolution
     ▼
ResolutionProposal (winner? action? rationale? confidence? precedence?)
     │
     │  tier4_escalate
     ▼
~/knowledge/wiki/_pending_questions.md
     │
     │  athenaeum questions  /  SessionStart hook  (#128)
     ▼
user accepts / overrides / defers
     │
     │  resolve_question MCP tool
     ▼
answer ingested back into raw/  (next librarian run archives [x] entries)
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
| Resolve | `resolve-questions` skill + `resolve_question` MCP | none | `[x]` mark + answer ingested |

Each stage degrades gracefully when its successor is unavailable. No
`ANTHROPIC_API_KEY` → detector returns `detected=False` with rationale
`llm-unavailable`; resolver returns the deterministic fallback
(`action=retain_both_with_context, confidence=0.0`) which renders to NO
trailing block, so the entry shape stays byte-identical to the pre-#126
escalation format. The pipeline never blocks ingest on contradiction work.

---

## 2. Cross-scope detection modes

`athenaeum.cross_scope.resolve_cross_scope_mode` reads
`ATHENAEUM_CROSS_SCOPE_MODE` (env wins) then
`contradiction.cross_scope_mode` from `athenaeum.yaml`. Default is
`ancestor`.

### `off`

Per-scope clusters only. Equivalent to the pre-#125 behavior. Use when:

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
- Wiki-vs-wiki gaps that emerge AFTER raw originals are merged-and-deleted.
  Two compiled `wiki/auto-*.md` pages can still contradict each other; the
  raw pass would never see them again, but the similarity sweep picks up
  the embedded text directly from chromadb.

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
| Catch cross-tree-branch + wiki-vs-wiki | `similarity` |
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
5. claude:tier3-...             — LLM-generated. Subordinate to any human/external source.
6. script:<slug>                — pipeline-generated, no upstream evidence.
7. unsourced / empty            — always loses to any sourced claim.
```

**Tie-break.** When two claims sit at the same precedence tier, prefer the
*newer* source date.

The resolver receives ONLY each member's `source:` value, the relevant
`field_sources.<key>` slice when present, and the conflicting passages —
NOT the full body. Token economy is enforced at prompt assembly.

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
  "rationale": "User-direct statement (precedence 1) overrides Claude-generated tier3 classification (precedence 5).",
  "confidence": 0.95,
  "source_precedence_used": ["a:claude:tier3-classify-2026-04-08 > b:user:session-2026-04-10-rosie-intake (b wins, tier 1 > tier 5)"]
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
**Rationale**: User-direct statement (precedence 1) overrides Claude-generated tier3 classification (precedence 5).
**Source precedence**: a:claude:tier3-classify-2026-04-08 > b:user:session-2026-04-10-rosie-intake (b wins, tier 1 > tier 5)
```

The user remains the final authority. The four `**Proposed resolution**`
keys are advisory only; `resolve-questions` and the MCP `resolve_question`
tool both require explicit user confirmation before applying.

---

## 4. Configuration reference

All keys live under `contradiction:` in `athenaeum.yaml` (see
`src/athenaeum/config.py` for the loaded defaults). Env vars override the
yaml; the yaml overrides built-in defaults.

| Env var | YAML key | Default | Effect |
|---------|----------|---------|--------|
| `ATHENAEUM_CLASSIFY_MODEL` | `classify_model` (top-level) | `claude-haiku-4-5-20251001` | Detector model. Shared with Tier 2 classifier — one knob, not a C4-only dial. |
| `ATHENAEUM_RESOLVE_MODEL` | `contradiction.resolve_model` | `claude-opus-4-7` | Resolver model. Configurable per-operator so cheaper models can be substituted. |
| `ATHENAEUM_RESOLVE_MAX_PER_RUN` | `contradiction.resolve_max_per_run` | `50` | Per-ingest cap on Opus calls. Surplus contradictions escalate WITHOUT a proposal (degraded mode). |
| `ATHENAEUM_CROSS_SCOPE_MODE` | `contradiction.cross_scope_mode` | `ancestor` | `off` / `ancestor` / `similarity` / `both` — see § 2. |
| n/a | `contradiction.cluster_size_cap` | `25` | Pooled-cluster size threshold; oversized pools split into newest-first chunks of `<= cap`. |
| n/a | `contradiction.similarity_threshold` | `0.85` | Cosine cutoff for the cross-scope similarity sweep. |
| `ATHENAEUM_PQ_SNOOZE_HOURS` | n/a | `24` | Skill-side default for snooze TTL writes. The hook ONLY reads the snooze cache; the env var is consumed by the `resolve-questions` skill when it writes the cache file. |

Notes:

- `ATHENAEUM_RESOLVE_MAX_PER_RUN` accepts non-negative integers. Negative
  or non-numeric values fall back to `50`. Setting it to `0` disables the
  resolver entirely; every detection escalates without a proposal.
- The per-ingest cap is enforced in `merge.py:_maybe_propose` (a closure
  in `merge_clusters_to_wiki`), NOT in `propose_resolution` itself.
  `propose_resolution` is the same function on every call; only the
  orchestrator counts.

Example `athenaeum.yaml`:

```yaml
search_backend: vector

contradiction:
  cross_scope_mode: both          # tightest coverage
  cluster_size_cap: 25
  similarity_threshold: 0.85
  resolve_model: claude-opus-4-7
  resolve_max_per_run: 50
```

---

## 5. Pending-questions integration (#128)

Once the librarian writes a block to `~/knowledge/wiki/_pending_questions.md`,
the question is durable but invisible until the user opens that file.
The #128 surface closes that gap.

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
next librarian run; the skill does not handle archival.

### Voltaire briefing surface (Tristan-specific)

For users running NanoClaw / Voltaire as an async companion, the
[code-workspace-config side of the integration](https://github.com/TriKro/code-workspace-config/issues/245)
surfaces the same pending-questions stream into the morning briefing. The
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
  $75/MTok output**. The resolver prompt is small by design (sources +
  conflicting passages, NOT bodies) — a few hundred tokens in, a few
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
`contradiction.resolve_model`) so an operator can substitute a cheaper
model when the cost tradeoff is unacceptable.

### Why the per-run cap defaults to 50

A guard against runaway cost on a noisy ingest. 50 × ~$0.10 = ~$5 of
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
