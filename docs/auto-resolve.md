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

The `full_body_token_cap` gates the resolver's per-side full-body context (issue #168). Tokens are measured with a simple character-count heuristic — roughly 4 characters per token for English markdown — so the practical character ceiling is `cap * 4`. When a member's body exceeds the cap, the rendered `<member>` block falls back to the detector's passage with a `[truncated — body exceeded {cap}-token budget; showing passage only]` note appended. Asymmetric truncation is expected: a small + large member pair is a normal case, and the small side still gets the full body.

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
