# Conflict resolution

**Reference page.** The full design record is
[conflict resolution](../design/conflict-resolution.md) and
[contradiction detection](../design/contradiction-detection.md); this page is the
operational summary of the chain from detection to a landed decision.

## What it does

When the librarian pools a cluster of memory snippets, a cheap detector flags pairs that
look contradictory. For each flagged pair, an Opus-class resolver proposes a winner under a
**9-tier source-precedence taxonomy**, highest first:

1. `user` — a direct user statement
2. `linkedin` / `twitter` — a user-curated public profile
3. `api` — a third-party authoritative source
4. `wikipedia` — a consensus public source
5. `agent-observed` — derived from an in-session artifact
6. `claude` — LLM-generated
7. `script` — pipeline-generated, no upstream evidence
8. `model-prior` — a training-data assertion with no session evidence
9. `unsourced` — always loses to any sourced claim

This order is implemented twice on purpose: once as prose in
`resolutions._RESOLVE_SYSTEM` (the live model's instructions) and once as pure data in
`athenaeum.precedence.SOURCE_PRECEDENCE_TIERS` (a deterministic, LLM-free mirror the
tier-0 field-correction applier uses, since it cannot call an LLM). A test binds the two by
parsing the prose block rather than transcribing it, so the two copies cannot silently
drift apart. An unrecognized or missing source ranks last (tier 9), indistinguishable from
a genuine `unsourced` claim — deliberately, so a malformed source never outranks a properly
attributed one. Two claims at the same tier are broken by newer source date; an **opinion**
is never resolved by precedence at all — reasonable people disagree, and both sides are
kept (`attribute_both`), each attributed to its asserter.

A confident resolver proposal is either auto-applied (see below) or written as a
`propose_merge`/verdict block onto the human decision queue, `wiki/_pending_questions.md`.
A separate path — duplicate detection over similar clusters — produces **merge proposals**
onto `wiki/_pending_merges.md` instead; these describe folding two or more pages into one,
not correcting a factual disagreement, and go through their own gates (below) before a
human ever sees them. `list_pending_decisions` (also exposed as an MCP tool) unifies both
queues, plus retraction-cascade reviews, tier-audit samples, and quarantine/rule-proposal
records, into one oldest-first list for a human to work through.

Example: Jordan Reyes's wiki page carries `current_title: VP Engineering` sourced from
`api:apollo-enrich`. A `claude`-sourced note later asserts "Director of Engineering" from an
LLM-summarized email thread. The resolver ranks `api` (tier 3) over `claude` (tier 6) and
proposes keeping the Apollo-sourced title — unless the email note is user-stated and dated
later, in which case supersession applies instead of precedence.

## What it reads

- The detector's flagged pairs and each side's exact conflicting passage (not the full
  body, unless it fits `resolve.full_body_token_cap`, default 1500 tokens / ~6000 chars).
- Each source's `source:` value, parsed into a precedence rank by
  `athenaeum.precedence.source_rank`.
- For a `correct_a`/`correct_b` auto-apply decision, the winning member's origin-session
  transcript (`origin_scope` / `origin_session_id` / `origin_turn`), re-read fresh from the
  transcript on every call — never the member's own `source_type` frontmatter, which is
  self-declared and could let a model grant itself deletion authority by writing a string.
- For a merge proposal, each candidate member's `memory_class:` frontmatter
  (`merge_type_gate.read_memory_class`) and pairwise/mean cosine similarity from the
  clustering pass.
- `librarian.reasoning_tier_auditing_enabled` and
  `librarian.reasoning_tier_t2_auto_apply_enabled` — the two independent opt-ins that gate
  the T1/T2 screens described below.

## What it writes

- `wiki/_pending_questions.md` — one block per unresolved (or auto-resolved) contradiction,
  carrying the proposed winner, action, confidence, rationale, and the precedence
  comparison the resolver leaned on.
- `wiki/_pending_merges.md` — one entry per proposed merge cluster.
- An auto-applied proposal rewrites its own block in place: the checkbox flips to `[x]`, an
  **Answer:** paragraph and ``**Auto-resolved**: true`` / `**Resolver model**` /
  `**Resolver confidence**` lines are appended. The original proposal block is left intact —
  the annotation is additive, never destructive.
- `wiki/_reasoning_tier_decisions.jsonl` — one append-only, fsync'd record per T1/T2
  decision (tier, verdict, reason, reason code), so screening is auditable even though it
  runs unattended.
- A `correct_a`/`correct_b` enactment deletes the losing member's raw file
  (`resolutions.enact_resolution`) — the one destructive path in this chain.

## What it refuses

- **`propose_merge` never auto-applies, at any confidence.** It is listed in
  `_NEVER_AUTO_APPLY_ACTIONS`, so the threshold lookup returns nothing to compare against —
  confidence is not a lever on this verdict. With the default configuration a resolver
  proposal to fold two pages into one lands in the human queue.

  **The exception is T2.** When `reasoning_tier_t2_auto_apply_enabled` is on (off by
  default), a safe-class approve writes the pending-merge block and then immediately
  resolves it `approve` with `auto_applied=True`. The block passes *through* the queue file
  rather than waiting in it, and no human sees it. That is the escape hatch — it is why the
  setting is opt-in and separate from T1's.
- **Per-action confidence floors gate every other auto-apply verdict**, not one global
  threshold: `not_a_conflict` needs `>= 0.75`; `keep_a`/`keep_b`/`deprecate_both` need
  `>= 0.90`; `forget_a`/`forget_b` need `>= 0.95`. Below its floor, a verdict falls through
  to the human queue exactly as if auto-apply were off.
- **`correct_a`/`correct_b` — the two verdicts that delete a file — are not gated by
  confidence at all.** The per-action threshold key is not consulted regardless of its
  value. Instead, auto-apply requires the winning member's transcript to classify as
  `user-stated`; `agent-observed`, `inferred`, and `unavailable` (the most likely production
  outcome, when a transcript has rolled off) all escalate to a human instead of deleting.
  Confidence is deliberately the wrong axis for an irreversible delete — models are worst
  calibrated exactly at the top of their range.
- **A cross-`memory_class` cluster is never merged.** `merge_type_gate.cross_class_precheck`
  rejects any cluster spanning two or more distinct, non-empty `memory_class` values before
  it reaches a write path, and routes it to a non-destructive `propose_cite` instead — no
  source page is folded, deleted, or overwritten; the citing page(s) merely gain a `##
  Cites` section. An untyped page is treated as compatible with everything, so an
  all-untyped corpus never trips this gate.
- **A merge proposal is suppressed before it ever reaches the queue** when it fails any of:
  an over-cluster (`max_merge_sources`, default 5), a mean pairwise cosine below
  `min_merge_mean_similarity` (default 0.6, active by default), a single-linkage chain whose
  minimum pairwise similarity falls below the clustering threshold, or a confidence below
  `min_merge_confidence` (default 0.0, opt-in). Suppression is deterministic in cluster
  shape, so a suppressed over-cluster is not silently re-emitted on the next run.
- **The T1 reasoning-tier screen can only reject or pass up — never approve.** It runs on a
  cheap model over a bounded view (title + frontmatter + ~100 words per source, never full
  bodies) and is off by default behind `librarian.reasoning_tier_auditing_enabled`.
- **The T2 screen can auto-apply a merge with no human review, but only inside a narrow,
  structurally-enforced safe class**: same `memory_class` across every member, three or
  fewer pages, no `pii` flag, no `memory_class: axiom` member, and no live-source duplicate
  in the cluster. Any violation — or a model response that pairs `approve` with rewritten
  content — makes the auto-apply outcome unreachable regardless of what the model returned;
  the pipeline downgrades it to escalate/draft itself. T2 is off by default behind its own,
  separate opt-in, `librarian.reasoning_tier_t2_auto_apply_enabled` — turning on T1's flag
  does **not** turn on T2's, and vice versa.
- **A T1/T2 model response that fails validation never falls through unsafely.** A malformed
  JSON payload, an out-of-vocabulary verdict, or a blank `reason` routes to the tier's
  existing safe fallback (`pass_up` for T1, `escalate` for T2) — never logged-and-ignored.
- **`scan_retraction_cascade` never unmerges a completed merge.** It flags a merge that
  relied on a since-retracted source and reports it; a human decides.
- **An out-of-range `auto_apply_threshold` raises on read** rather than silently disabling
  auto-apply — a typo like `9.0` for `0.9` surfaces immediately instead of quietly turning
  every verdict into a human-review item.

## See also

- Guides — [Answering decisions](../guides/decisions.md) · [Daily operation](../guides/daily-operation.md)
- Modules — [corrections](corrections.md) · [sensitivity](sensitivity.md) · [mcp](mcp.md)
- Design — [conflict resolution](../design/conflict-resolution.md) ·
  [contradiction detection](../design/contradiction-detection.md) ·
  [auto-resolve](../design/auto-resolve.md) · [memory taxonomy](../design/memory-taxonomy.md) ·
  [provenance shape](../design/provenance-shape.md)
- Reference — [configuration](../reference/configuration.md)
