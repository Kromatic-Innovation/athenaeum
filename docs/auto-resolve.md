# Auto-resolving high-confidence contradictions

When the librarian's cheap detector flags two memory snippets as
contradictory, an Opus-backed resolver weighs them under a source-precedence
taxonomy and writes a proposal block onto `wiki/_pending_questions.md`.
Historically every proposal — even at 0.99 confidence — sat in the queue
until a human flipped the checkbox. The auto-resolve lane (issue #156)
closes that loop: when the resolver is confident enough, the librarian
marks the block as answered itself, leaving a clean audit trail.

## What auto-resolve does

For each detected contradiction:

1. The cheap detector flags a pair of snippets.
2. The resolver (default `claude-opus-4-7`) proposes a winner, action,
   rationale, and confidence under the precedence taxonomy documented
   in `docs/conflict-resolution.md`.
3. If auto-apply is enabled **and** the proposal's confidence is `>=` the
   configured threshold, the rendered pending-question block is rewritten:

   - `- [ ]` becomes `- [x]`.
   - An **Answer:** paragraph is inserted with the resolver's rationale.
   - `**Auto-resolved**: true`, `**Resolver model**: <id>`, and
     `**Resolver confidence**: <0.NN>` lines follow as the audit trail.
   - The original `**Proposed resolution** / **Confidence** / **Rationale** /
     **Source precedence**` block is left in place — the annotation is
     additive, never destructive.

4. On the next `athenaeum ingest-answers` run, the `[x]` block flows
   through the standard archive lane: a raw intake file is written to
   `raw/answers/{TS}-{slug}.md` with `Auto-resolved` carried in the body,
   and the original block is appended to `_pending_questions_archive.md`.

Low-confidence proposals (below the threshold), the deterministic
fallback proposal (`confidence == 0.0`), and items the resolver budget
already exhausted continue to ship as unchecked `[ ]` blocks for human
review — exactly as before.

## Configuration

Three precedence layers, resolved as **env > yaml > default**:

| Setting | Env var | YAML path | Default |
|---|---|---|---|
| Enable auto-apply | `ATHENAEUM_RESOLVE_AUTO_APPLY` | `resolve.auto_apply` | `true` |
| Confidence threshold | `ATHENAEUM_RESOLVE_AUTO_APPLY_THRESHOLD` | `resolve.auto_apply_threshold` | `0.90` |
| Resolver model | `ATHENAEUM_RESOLVE_MODEL` | `resolve.model` | `claude-opus-4-7` |
| Full-body token cap (per side) | `ATHENAEUM_RESOLVE_FULL_BODY_TOKEN_CAP` | `resolve.full_body_token_cap` | `1500` |

Env-var boolean values accept `true`/`false`, `1`/`0`, `yes`/`no` (case-insensitive). An invalid env value falls through to the yaml/default layers — auto-apply is a behavior knob, not a hard validation surface.

Threshold values outside `[0.0, 1.0]` raise on read so a typo (e.g. `9.0` meant as `0.9`) surfaces immediately instead of silently turning auto-apply off for the rest of the run.

The `full_body_token_cap` gates the resolver's per-side full-body context (issue #168). Tokens are measured with a simple character-count heuristic — roughly 4 characters per token for English markdown — so the practical character ceiling is `cap * 4`. Each member's `<member>` block always opens with a `passage:` line containing the detector's exact conflicting region; when the body also fits the cap, a `body:` block follows. When the body exceeds the cap, the body is omitted and a `[truncated — body exceeded {cap}-token budget; passage above is the conflict region]` note is appended below the passage. Asymmetric truncation is expected: a small + large member pair is a normal case, and the small side still gets the full body.

`full_body_token_cap` must be a positive integer. Zero and negative values raise `ValueError` on read — to effectively disable truncation, set a large value (e.g. `1000000`) rather than `0`.

A sample `athenaeum.yaml`:

```yaml
resolve:
  model: claude-opus-4-7
  auto_apply: true
  auto_apply_threshold: 0.90
  full_body_token_cap: 1500
```

## Disabling

Pick whichever scope matches the intent:

- **One-off run:** `ATHENAEUM_RESOLVE_AUTO_APPLY=false athenaeum run ...`
- **Persistent:** set `resolve.auto_apply: false` in `<knowledge_root>/athenaeum.yaml`.

With auto-apply off, every flagged contradiction lands as an unchecked
`[ ]` block exactly as in the pre-#156 era.

## Lowering or raising the threshold

The 0.90 default was picked to be conservative — the resolver has to be
quite sure before the librarian acts on its own. To tolerate more borderline
auto-resolutions for a single ingest:

```bash
ATHENAEUM_RESOLVE_AUTO_APPLY_THRESHOLD=0.75 athenaeum run
```

To require near-certainty across all runs, set the yaml path:

```yaml
resolve:
  auto_apply_threshold: 0.98
```

Anything below the threshold continues to land as an unchecked block for
human review.

## Per-action thresholds (issue #170)

The single scalar `auto_apply_threshold` treats every resolver action the
same, but the cost of an incorrect auto-apply is not symmetric:

- `not_a_conflict` — false-suppress is cheap. If the resolver is wrong,
  the detector re-fires the next run and the conflict re-enters the queue.
  Default threshold: **0.75**.
- `keep_a` / `keep_b` — mutates wiki bodies. A wrong auto-apply requires
  a human to chase down which memory was overwritten. Default
  threshold: **0.90**. Use for a DECISION that was *valid-then-replaced*:
  the loser stays as superseded history.
- `correct_a` / `correct_b` — **enacts** a deletion (see "Enactment"
  below). For a DECISION conflict where the losing side was simply **wrong**
  (a mistake / confusion), not valid-then-replaced — the wrong member is
  removed rather than enshrined as superseded. Default threshold: **0.90**
  (same mutating bar as `keep_*`).
- `forget_a` / `forget_b` — **enacts** a deletion (see "Enactment" below).
  One side is transient / no-longer-relevant / was confusion and should be
  deleted cleanly with **no historical record**. Distinct from supersede
  (keeps history) and from correct (which asserts the other side is the
  right answer). `deprecate_both` is the both-sides analogue. Default
  threshold: **0.90**.
- `propose_merge` — **never auto-applies regardless of confidence**. The
  proposal carries an LLM-drafted merged body that must go through human
  review before it can land in a wiki page. This is a hard rule, not a
  threshold knob.
- `retain_both_with_context` — does not auto-apply (escalates for the
  human). When the resolver hits a FACT/identity conflict it cannot
  confidently resolve and that is *not* two sequential dated snapshots, it
  attaches `disambiguation_options` to the proposal; the pending-question
  block then renders an enumerated question
  (`Which is correct: (a) … (b) … (c) both, (d) neither/other?`) instead of
  a free-text precedence guess.

Configure per action via the new optional map:

```yaml
resolve:
  auto_apply: true
  auto_apply_threshold_per_action:
    not_a_conflict: 0.75
    keep_a: 0.90
    keep_b: 0.90
```

### Backward compatibility

Pre-#170 configs that set only the legacy scalar continue to work:

```yaml
resolve:
  auto_apply_threshold: 0.85
```

is interpreted as:

- `keep_a` / `keep_b` → 0.85 (legacy scalar honored).
- `not_a_conflict` → 0.75 (new per-action default; the legacy scalar is
  explicitly NOT applied here — that would defeat the cheaper-threshold
  rationale).
- `propose_merge` → never auto-applies.

When both fields are present, the per-action map wins for the actions it
explicitly lists, and the legacy scalar fills the rest for `keep_a` /
`keep_b` only:

```yaml
resolve:
  auto_apply_threshold: 0.85           # legacy fallback for keep_b
  auto_apply_threshold_per_action:
    keep_a: 0.99                       # explicit override wins for keep_a
    not_a_conflict: 0.60               # explicit override wins
# Resolved thresholds: keep_a=0.99, keep_b=0.85, not_a_conflict=0.60,
# propose_merge=never.
```

Out-of-range values (`< 0.0` or `> 1.0`) and non-numeric entries raise
`ValueError` on read — same loud-fail discipline as the legacy scalar.

## Enactment: recording vs. mutating state

Marking a pending-question block `[x]` only **records** a verdict — by
itself it changes no memory. For the single-side *mutating* verdicts that
is not enough: the wrong or transient claim must actually leave the corpus,
or the cheap detector re-fires it on the next run. The enactment lane closes
that gap. When a `forget_*` or `correct_*` proposal auto-applies (confidence
`>=` its per-action threshold, default 0.90), the librarian also deletes the
target raw auto-memory member file:

| Action | Recorded | Enacted (state change) |
|---|---|---|
| `forget_a` | block → `[x]` | deletes member **a** (the transient side) |
| `forget_b` | block → `[x]` | deletes member **b** |
| `correct_a` | block → `[x]` | a is correct → deletes member **b** (the wrong claim) |
| `correct_b` | block → `[x]` | b is correct → deletes member **a** (the wrong claim) |
| `keep_a` / `keep_b` | block → `[x]` | **record-only** — both members survive (loser kept as superseded history) |
| `deprecate_both` | block → `[x]` | **record-only** |
| `not_a_conflict` | block → `[x]` | nothing to enact (escalation suppressed upstream) |
| `propose_merge` | never auto-applies | n/a |

A raw auto-memory member is a single atomic snippet, so "remove the wrong
claim" is implemented as deleting that member file. The compiled
`wiki/auto-*.md` entry is regenerated from the surviving members on the next
`athenaeum run`, so the claim disappears from the wiki without a separate
body-rewrite path.

Enactment is best-effort and never crashes the merge pass: a member file
that is already gone is treated as success, and an unlink failure is logged
and swallowed. The labels `a` / `b` map to the resolver's flagged member
order (the order shown in the block's `Members involved:` line).

> Note: `keep_*` and `deprecate_both` remain **record-only** today — there
> is no supersede-marker mechanism wired up to enact them, so the loser
> still survives in the corpus. Enacting those is tracked separately.

## Reversing an auto-resolution

The auto-resolved block flows through the same `ingest-answers` lane as a
hand-answered one, so the original block ends up in two durable places:

- `raw/answers/{TS}-{slug}.md` — the raw intake file with the resolver's
  answer paragraph and `Auto-resolved: true` carried in the body.
- `_pending_questions_archive.md` — the original block verbatim, newest
  first.

To reverse:

1. Open the raw answer file under `raw/answers/` and either delete it (if
   the underlying memory should be left untouched) or edit the answer body
   to reflect the correct resolution.
2. If the librarian has already compiled the answer into wiki state, edit
   the affected wiki page directly — the answer file is intake, not
   binding state.
3. The archive entry is append-only and should be left in place as the
   historical record. If you want to flag the reversal, append a short
   note under the original block in `_pending_questions_archive.md`.

Because every auto-resolution carries the resolver model id and confidence
in the answer body, a grep over `raw/answers/` is enough to audit the lane
end-to-end:

```bash
grep -l "Auto-resolved" ~/knowledge/raw/answers/
```

## Related

- [`docs/conflict-resolution.md`](conflict-resolution.md) — the source
  precedence taxonomy the resolver applies.
- [`docs/contradiction-detection.md`](contradiction-detection.md) — the
  cheap detector that gates whether the resolver runs at all.
- [`docs/provenance-shape.md`](provenance-shape.md) — how `source:` and
  `field_sources:` feed the resolver's precedence comparison.
