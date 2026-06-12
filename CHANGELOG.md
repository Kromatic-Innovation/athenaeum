# Changelog

All notable changes to Athenaeum are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`--max-files` gains env and yaml knobs (#232).** The per-run intake batch
  size now resolves CLI `--max-files` > `ATHENAEUM_MAX_FILES` env >
  `librarian.max_files` yaml > default 50, mirroring the #220
  `--max-api-calls` pattern. The flag also now validates positive integers
  at parse time (zero/negative/non-numeric values are an argparse error,
  matching `--max-api-calls`).
- **New `models:` yaml section for the env-only model knobs (#232).**
  `models.classify` (Tier-2 classifier + C4 contradiction detector, env
  `ATHENAEUM_CLASSIFY_MODEL`), `models.write` (Tier-3 writer, env
  `ATHENAEUM_WRITE_MODEL`), and `models.topic` (recall query-topic
  extraction, env `ATHENAEUM_TOPIC_MODEL`). Per knob: env wins over yaml,
  yaml wins over the code default. The contradiction-resolver model is
  unchanged and stays at `resolve.model`. None of the new keys are seeded
  into config defaults, preserving the #231 fix.

### Fixed

- **Config `_DEFAULTS` no longer shadow module code defaults (#231).**
  `load_config()` seeded concrete values for `contradiction.*` and
  `librarian.cluster_threshold` / `cluster_output` into every loaded
  config, so resolver functions always saw a "user-set" value and their
  own code defaults were unreachable. Most visibly, the #187 resolver-cap
  raise (50 -> 250) never took effect through the config path — the
  default resolver cap was silently 50. Those keys are removed from
  `_DEFAULTS`; the module-level defaults (and their env > yaml > default
  precedence) are now live. The merge also passes through user yaml
  sections absent from `_DEFAULTS` (e.g. `resolve:`) instead of dropping
  them. Explicit yaml settings still win unchanged.

## [0.7.3] - 2026-06-11

Contradiction-pipeline reliability release, closing the silent-loss paths
surfaced by the 2026-06-11 nightly run. LLM JSON responses wrapped in
markdown fences or prose no longer drop clusters, and a run that exhausts
its API budget now says so loudly and leaves a manifest of the deferred
work instead of reporting a clean "Done".

### Added

- **`athenaeum run --path` as an alias for `--knowledge-root` (#227).** `run`
  now accepts the same `--path` spelling as `init`/`status`/`serve`;
  `--knowledge-root` keeps working unchanged.
- **`athenaeum run --strict-budget` (#227).** Opt-in flag that makes a
  budget-tripped (DEGRADED) run exit nonzero for exit-code-based alerting.
  Default behavior is unchanged: exit 0 with the warning summary and the
  `wiki/_deferred_work.md` manifest.

### Changed

- **Run-level API call budget is configurable and counts every call (#220).**
  The budget now resolves `ATHENAEUM_MAX_API_CALLS` env var over
  `librarian.max_api_calls` yaml over the default, and the default is raised
  from 200 to 800 — quadrupling the default per-run API cost ceiling — to fit
  the post-#187 full-coverage confirmation-pass profile. Operators who want
  the previous ceiling should pin it explicitly via the env var, yaml key, or
  CLI flag. The counter now includes merge-phase detector/resolver and
  nightly re-resolve calls, making it a true run-level ceiling. The CLI
  `--max-api-calls` flag validates positive integers and explicitly wins
  over env and yaml. The additive configuration surface here (env var, yaml
  key, deferred-work manifest) ships in a patch release because it exists to
  fix the silent-loss defect below; the cost-default change is the one
  operator-visible behavior shift worth reviewing before upgrading.

### Fixed

- **Lenient JSON extraction for LLM responses (#219).** The contradiction
  detector and resolver parsed model output with a strict first-to-last-brace
  regex and silently dropped clusters when the model wrapped its JSON in
  markdown ```json fences or surrounding prose — 38 silent drops were
  observed in one nightly run. A new shared `extract_json_object()` helper
  (`athenaeum.json_utils`) prefers fenced content, applies an exactly-one
  rule for unfenced text, and returns None on ambiguity so callers keep
  their loud safe fallback. RecursionError is contained and decode
  diagnostics are logged at debug level. (PR #221)
- **Fence pairing is robust to inline backticks, and the last legacy parse
  site is migrated (#222).** Only line-leading ``` (CommonMark, up to three
  spaces of indentation) delimits a fence, so stray inline backticks can no
  longer shift fence pairing. When fences yield no object, the helper falls
  back to a whole-text exactly-one scan. `propose_freetext_source_edits` is
  migrated off the greedy first-to-last-brace regex. (PR #223)
- **Librarian run-level budget exhaustion is no longer silent (#220).** When
  the budget trips, the run now writes a `wiki/_deferred_work.md` manifest
  itemizing deferred intake (in-window and beyond-window, with failed files
  listed separately) and logs a warning-level `Done (DEGRADED — budget
  exhausted)` summary with deferred counts instead of a clean "Done". Stale
  manifests are cleared by the next clean run. (PR #224)
- **`athenaeum init` now creates the `raw/auto-memory/` intake directory**, so
  first runs no longer warn about a missing extra-intake root.
- **Yaml `resolve_max_per_run: yes` (a bool) is no longer accepted as an
  integer cap of 1** — bools fall through to the default, using the same guard
  as `librarian.max_api_calls`.
- **`athenaeum test-mcp` now declares `sources=` on its own `remember()`
  call**, so the smoke test no longer trips the issue-#90 provenance warning.

## [0.7.2] - 2026-06-09

Pending-question recurrence hardening. Free-text human answers now enact a
concrete source-file edit, the resolved-contradiction decision log matches by
member-pair and vector similarity instead of brittle exact-text fingerprints,
and a nightly self-heal pass re-resolves proposal-less escalations. Builds on
the 0.7.0 source write-back.

### Added

- **Vector + member-pair matching for the resolved-contradiction decision log
  (#211).** The decision log keyed each record by a SHA-1 of the exact passage
  text the detector quoted; the detector re-quotes a drifting snippet every
  run, so the key never matched and an already-resolved contradiction
  re-escalated indefinitely. Matching now flows fingerprint → member-pair key
  → embedding cosine, with a configurable
  `contradiction.resolved_similarity_threshold` (default 0.83). Member-pair
  matching is deterministic and works without chromadb; the embedding layer is
  the optional `[vector]` extra and degrades gracefully when absent.
- **Nightly re-resolve pass for open proposal-less questions (#188).** A
  question first escalated without a proposal (resolver budget exhausted or
  offline) previously stayed raw forever, because the open-pair dedup merged
  re-detections into the existing block instead of re-running the resolver.
  `reresolve_open_questions` (wired into `librarian.run` and exposed as
  `athenaeum reresolve-questions`) now re-resolves such blocks on a later,
  budgeted run: `not_a_conflict` drops/archives the question, a real verdict
  annotates it. Budget-aware, idempotent, and a no-op offline.

### Changed

- **Free-text answers enact a source edit, not just an annotation (#210).**
  When a human resolves a contradiction with a free-text ruling (no verdict
  token), the resolver now interprets that ruling into a concrete edit of the
  source memory file(s) via an LLM-backed proposer and applies it through the
  existing write-back path, instead of appending a non-destructive annotation
  that left the contradictory claim in place. Falls back to annotation when no
  client is available or the proposer returns no edit.

### Fixed

- **Write-back resolves the true source files from `Members involved:` (#214,
  follow-up to #210).** The auto-memory contradiction blocks attribute their
  real source via a `Members involved:` line (refs relative to
  `raw/auto-memory/`) while their `source:` header names a compiled wiki page.
  The write-back only parsed `**Member paths**:` and resolved under `raw/` +
  `wiki/`, so on real blocks it resolved nothing and edited nothing. It now
  parses `Members involved:` and resolves under the configured intake roots.
- **Decision-log records persist a non-empty `member_key` and full `pair_text`
  (#216, follow-up to #211).** The human-resolution record site derived
  `member_key` from `pq.source` (a wiki page) and `pair_text` from the
  `**`-truncated `pq.description`, recording empty keys that the matcher could
  never hit. Both are now derived from the full raw block.

## [0.7.1] - 2026-06-08

Patch release addressing two follow-up nits from the #207 Zenodotus review.
No behavior change under normal execution.

### Fixed

- Hardened the transient-retry exhaustion guard in `_retry.py` so it survives
  `python -O` (replaced an `assert` used for control flow with an explicit
  runtime guard); the exhausted-retries path still re-raises the captured
  transient error. (#207)
- Resolved-contradiction cache records now write a single authoritative
  `action` key instead of duplicate `verdict`/`action` keys; the reader still
  tolerates legacy `verdict`-only records. (#207)

## [0.7.0] - 2026-06-08

Pending-question recurrence fix. Answering an adjudicated contradiction now
writes the ratified verdict back to the source-of-truth memory files, the
resolved pair is fingerprinted so the detector stops re-flagging it, and a
newly-detected conflict that matches a prior human verdict is auto-applied
instead of re-escalated. Together these stop resolved contradictions from
regenerating on the next wiki build.

> **Upgrade impact — answering a pending question now edits your source
> memory files.** Before 0.7.0, resolving a pending contradiction only wrote
> a sibling `raw/answers/` provenance doc and left the source memory file
> untouched. As of 0.7.0 the ratified verdict is enacted on the
> source-of-truth file(s): `correct_*` / `forget_*` delete the wrong or
> transient member file, `keep_*` / `deprecate_both` write `superseded_by` /
> `deprecated` frontmatter markers, and `retain_both` / `not_a_conflict` add a
> non-destructive annotation. The `raw/answers/` provenance doc is still
> written. This is the intended fix for contradiction recurrence — but it
> means answering a question now authorizes modification (and, for
> `correct_*` / `forget_*`, deletion) of source memory content. Review
> verdicts accordingly.

### Added

- **Source write-back when answering a pending question (#197).** Answering a
  pending question now writes the ratified verdict back to the source-of-truth
  memory file(s) via the existing `enact_resolution` machinery — `pq.source`
  plus every involved member — rather than only emitting a sibling
  `raw/answers/` provenance doc. `correct_*`, `forget_*`, `keep_*`, and
  `deprecate_both` enact destructively on the source; `retain_both` and
  `not_a_conflict` annotate non-destructively; the provenance doc is still
  written. This stops adjudicated contradictions from regenerating on the next
  wiki build.
- **Resolved-contradiction fingerprint cache (#198).** Adjudicated claim-pairs
  now get a page-independent, order-independent fingerprint persisted to
  `raw/_resolved_contradictions.jsonl` on resolution (human or auto). The
  detector suppresses already-resolved fingerprints and logs the suppression
  count. A material change to a claim changes its fingerprint and re-enables
  escalation.
- **Auto-apply of prior human-ratified verdicts (#199).** A newly-detected
  conflict that matches a prior **human** verdict is auto-applied without
  re-escalation and routed through source write-back. Only human-ratified
  verdicts auto-apply — prior auto-resolutions never do. The match is
  orientation-safe: per-side claim anchors are stored so the verdict is
  flipped to the new conflict's a/b orientation rather than deleting the
  correct claim. Unresolvable orientation or a failed enact falls through to
  escalation.

### Fixed

- **Failed auto-apply enact now escalates instead of silently suppressing
  (#203).** If applying a prior verdict's enact fails (file-op error or no-op),
  the conflict escalates to a pending question rather than being silently
  suppressed.
- **Keep/deprecate verdicts are now enacted on the source (#191).** Resolving a
  contradiction with a keep/deprecate verdict writes supersede/deprecate
  markers to the source memory file rather than only recording the decision.
- **Correct/forget verdicts are now enacted, not just recorded (#166).**
  Resolving with a `correct` or `forget` verdict applies the edit to the source
  memory file. Adds a disambiguation mode for pairs that are distinct entities
  rather than a true contradiction, and routes `correct`/`forget` through the
  tier and merge render paths. Pending-question blocks that lost their checkbox
  line are now recovered rather than dropped.
- **Transient Anthropic overload no longer becomes a permanent librarian
  backlog** (#193). The per-file classification path (`tiers.py` tier2/tier3
  calls) now retries HTTP 429 (`RateLimitError`), 529 (`OverloadedError`),
  and `APIConnectionError` with bounded exponential backoff + jitter (5
  attempts, capped at 60s, honoring `Retry-After` when present) via the new
  `athenaeum._retry.with_retry` helper. Previously a single overload window
  deferred every affected file to the next run, and because the same files
  landed in the same late position every night the backlog never self-healed.
  On final give-up the loop logs `Gave up after N retries (transient API
  overload)` distinctly from the malformed-file `Failed to process` line, so
  health reporting can tell transient-API from a genuinely broken file.
  Non-transient errors (e.g. 400 `BadRequestError`) still fail fast.

### Changed

- Resolver per-run Opus call cap (`DEFAULT_RESOLVE_MAX_PER_RUN`) raised
  50 → 250 (#187). On a full-knowledge-base ingest the detector can flag
  well over 50 contradictions; at the old default the confirmation pass
  ran out of budget partway through and the surplus escalated raw into
  `_pending_questions.md` instead of being suppressed as `not_a_conflict`.
  The cap is a ceiling, not a target — small bases never approach it.
  Override via `contradiction.resolve_max_per_run` (yaml) or
  `ATHENAEUM_RESOLVE_MAX_PER_RUN` (env).
- **Destructive auto-DELETE bar raised to 0.95 confidence (#166).** The
  auto-resolver now requires 0.95 confidence before applying a destructive
  DELETE, and the principled-escalation render path is locked so low-confidence
  conflicts escalate to a human rather than being auto-deleted.
- CI: bumped `dependabot/fetch-metadata` v2→v3 (#194) and
  `1password/load-secrets-action` v2→v4 (#195).

## [0.6.1] - 2026-05-24

Patch bundle: the self-reference lint added in #173 now runs on every
`AutoMemoryFile` construction site, not just `discover_auto_memory_files`.

### Changed

- **Self-reference lint applied to all `AutoMemoryFile` construction sites**
  (#181, #183) — the lint that strips a memory's own name from its
  `refines` / `supersedes` lists (originally added in #173) now also runs
  in the similarity-sweep path (`cross_scope.candidate_to_auto_memory_files`)
  and the cluster-shim path (`merge.merge_cluster_row`). Extracted to
  `athenaeum._lint._strip_self_reference` so all three sites share one
  implementation. Tests tightened to assert against rendered log messages.

## [0.6.0] - 2026-05-24

The librarian-reasoner epic (#166) lands as a single backward-compatible
minor bump. The 0.5.x auto-apply + dedupe foundation now stands on a
richer reasoning surface: declared refines/supersedes relationships
short-circuit the detector, the resolver sees full-body context and
field-level provenance, the prompt taxonomy adds a `propose_merge`
action with a `_pending_merges.md` sidecar, and the auto-apply gate is
asymmetric per action so cheap suppressions auto-apply while wiki-body
mutations stay behind a higher bar. This release also bundles five
follow-up polish items (#172, #173, #175, #177, #179).

### Added

- **Declared refines / supersedes in frontmatter** (#167) — memories
  can now declare a relationship to a sibling memory via
  `refines: [name]` or `supersedes: [{name, as_of, reason}]` in YAML
  frontmatter. The detector short-circuits any pair (or fully-declared
  chunk) covered by a declaration, and the resolver surfaces both lists
  in the prompt so the LLM has the audit context even when the
  short-circuit did not fire.
- **Full-body resolver context with token budget** (#168) — the Opus
  resolver now sees each member's full body (default 1500-token cap
  per side, char-heuristic) plus `created_at` / `updated_at` /
  `originSessionId` frontmatter and one-hop `[[wikilink]]`
  descriptions. Asymmetric truncation is normal; the conflict passage
  is always emitted regardless of body inclusion.
- **`propose_merge` action + `_pending_merges.md` sidecar** (#169) —
  the resolver can propose a merged body for human review rather than
  picking a winner. Proposals land in `wiki/_pending_merges.md`;
  `list_pending_merges` / `resolve_merge` MCP tools triage them.
  Approval writes the draft merged body to wiki; rejection writes a
  `refines:` declaration into the first source so the detector's
  declared-refinement short-circuit suppresses the pair on future runs.
- **Asymmetric per-action auto-apply thresholds** (#170) —
  `not_a_conflict` defaults to 0.75 (false-suppress is cheap; detector
  re-fires next run), `keep_a` / `keep_b` to 0.90 (mutates wiki bodies;
  higher bar), `propose_merge` NEVER auto-applies (the draft body must
  go through human review regardless of confidence). Configurable via
  `resolve.auto_apply_threshold_per_action.<action>`; the legacy scalar
  still applies to `keep_a` / `keep_b` only.

### Changed

- **`_filter_declared_pairs` prunes declared pairs from multi-member
  chunks** (#172) — previously all-or-nothing: one undeclared pair
  sent the whole chunk (including already-declared pairs) to Haiku.
  Now members whose every partner in the chunk is declared are
  dropped before the detector sees the chunk.
- **Self-reference in `refines:` / `supersedes:` is dropped + warned**
  (#173) — a post-load lint pass in `discover_auto_memory_files`
  silently strips entries that name the file's own memory and emits a
  `WARNING` log so the YAML authoring mistake surfaces without
  blocking ingest.
- **Per-call sibling-index cache + clarified `field_sources` semantics**
  (#175) — the resolver now caches `slug → description` per scope dir
  per `_build_user_message` call instead of re-globbing + re-parsing
  per member per conflict (O(N·M·K) → O(N·M+K)). The prompt ships ALL
  `field_sources` keys (the earlier "filter to passage-substring"
  comment was aspirational, never wired up; full shipping is the right
  call for provenance-aware resolution).
- **Threshold error wording + gate-decision threshold returned to
  caller** (#179) — non-numeric threshold values now say "not a numeric
  value" instead of "out of range" (latter implies a numeric typo).
  `_should_auto_apply` returns `(should_apply, threshold)` so the
  caller logs the resolved threshold from the gate decision rather
  than re-resolving via a second lookup.
- **MCP `resolve_merge` emits legacy aliases for symmetry with
  `resolve_question`** (#177) — `block` (= `resolved_block`) and
  `error` (= `message` on failure) are now present on `resolve_merge`'s
  return shape. New callers should still prefer `error_code` +
  `message` + `resolved_block`.

### Backward compatibility

All changes are additive. Configs targeting 0.5.x continue to load
unchanged; the legacy scalar `resolve.auto_apply_threshold` still
applies to `keep_a` / `keep_b` so a pre-#170 deployment behaves the
same after upgrading. Frontmatter without `refines:` / `supersedes:`
keys round-trips byte-for-byte.

## [0.5.0] - 2026-05-23

Closes the daily-backlog problem in the pending-questions queue. The Opus
resolver shipped in 0.4.x drafted proposals but never applied them; the
detector wrote one block per destination entity even when the underlying
source-memory pair was the same. On a 2026-05-23 sweep the queue carried
323 unanswered questions, 92% of which were duplicate firings of three
source-pair conflicts. This release adds the structural fix: auto-apply
high-confidence resolutions and dedupe escalations by source pair.

### Added

- **Auto-apply lane for high-confidence resolutions** (#156, PR #158) —
  when `auto_apply` is enabled and a `ResolutionProposal` reaches the
  threshold, `tier4_escalate` writes the question block as already
  answered (`- [x]`) with an `**Answer:** <rationale>` paragraph and an
  `**Auto-resolved**: true` audit tag. The annotation is additive — the
  resolver's `**Proposed resolution**` / `**Confidence**` /
  `**Rationale**` / `**Source precedence**` block is preserved. The
  rewrite is idempotent and round-trips through `ingest-answers` into
  both `raw/answers/` and `_pending_questions_archive.md`.
- **Configurable model and threshold** (#156) — all three config
  surfaces are honored with precedence env > yaml > defaults:
  - Env: `ATHENAEUM_RESOLVE_MODEL`, `ATHENAEUM_RESOLVE_AUTO_APPLY`,
    `ATHENAEUM_RESOLVE_AUTO_APPLY_THRESHOLD`.
  - YAML: `resolve.model`, `resolve.auto_apply`,
    `resolve.auto_apply_threshold`.
  - Defaults: `claude-opus-4-7`, `auto_apply: true`,
    `auto_apply_threshold: 0.90`. Out-of-range threshold raises with
    the source named (`env` vs `yaml`).
- **Source-pair dedup at escalation time** (#157, PRs #159 and #160) —
  before appending a new question block, `tier4_escalate` checks whether
  the same source-memory pair already has an open block in the file (or
  another item in the current batch). If so, the destination entity is
  appended to a `**Also affects**: a, b, c` line instead of creating a
  duplicate block. Falls back to a normalized passage-hash key when
  `Members involved:` is unsourced. Auto-resolved (`[x]`) blocks are
  excluded from the open-pair index so a resurrected conflict still
  produces a fresh question.
- **Highest-confidence-wins auto-apply on merge** (PR #160) — when items
  collapse into an existing block, auto-apply is evaluated against the
  highest-confidence proposal seen for the source-pair key in the
  current batch, not just the first. Prevents a low-confidence primary
  item from suppressing a high-confidence sibling that would have
  triggered auto-apply on its own. Cross-batch case is also covered:
  an existing open block can be auto-resolved when a fresh batch brings
  a high-confidence proposal for the same pair.
- **Dedup escape hatch** (#157) — `ATHENAEUM_TIER4_DEDUP=false` reverts
  to pre-#157 always-append behavior. Default is ON.
- **`docs/auto-resolve.md`** — explains the audit trail, how to disable
  auto-apply (env or yaml), how to tune the threshold, and how to
  reverse an auto-resolution.
- **`README.md` Configuration section** — documents the three precedence
  layers and lists all three new keys with defaults.

### Changed

- `tier4_escalate` signature accepts a `config: dict | None = None`
  argument so the auto-apply gate can read the resolved model and
  threshold. Pre-#156 callers passing `config=None` retain the prior
  always-append behavior — no auto-apply, no dedup.
- `EscalationItem.proposal` is a new optional attribute carrying the
  resolver's verdict through to `tier4_escalate`. Legacy callers that
  do not populate it are unaffected.
- `PendingQuestion.also_affects: list[str]` exposes the merged-entity
  list to `answers.parse_pending_questions` consumers.

### Internal

- 50 new tests across `tests/test_auto_resolve.py` (25) and
  `tests/test_tier4_dedup.py` (25), including real-`ResolutionProposal`
  integration tests for the highest-confidence-wins rule on both
  in-batch (Path B) and cross-batch (Path A) merge paths. Round-trip
  preservation of `**Also affects**:` through `apply_auto_resolution`
  and `ingest_answers` is asserted explicitly.

## [0.4.1] - 2026-05-22

A patch release hardening the auto-memory contradiction pipeline shipped
in 0.4.0.

### Fixed

- **Contradiction confirmation pass** (#145) — the Opus resolver now runs a
  confirmation pass over Haiku-detector hits and suppresses detector false
  positives before they reach the pending-questions queue. Genuine
  contradictions still escalate, now with the resolver's proposed
  resolution attached.
- **Escalation dedup keyed on source-file set** (#146) — escalations are
  deduplicated by the set of flagged source files rather than the cluster
  slug, so the same conflict surfaced by different clusters escalates once
  per run.
- **`add-to-project` CI workflow** — pinned to `actions/add-to-project@v1.0.2`.
  The previous `@v1` ref was never published by the action (only exact
  `v1.0.x` tags exist), so the workflow errored on every issue and PR.

### Internal

- Test coverage for the confirmation-pass resolver-verdict and
  malformed-response escalation paths (#148), and the contradiction-merge
  `<2`-member fallthrough (#146 review).

## [0.4.0] - 2026-05-11

This release ships three coherent streams of work: (a) the auto-memory
intake → cluster → merge → contradiction-detection pipeline (#195, #196,
#197, #198), which lets the librarian fold per-scope Claude Code memory
and other auto-captured turns into the wiki without manual triage;
(b) per-claim provenance, per-value `field_sources`, cross-uid dedupe,
legacy-slug repair tooling, and an Opus-backed contradiction resolver
(#90, #102, #103, #97, #126, #128); and (c) the Apollo connector
extraction and `athenaeum people` filter CLI (#82, #112). Includes **two
BREAKING changes**; see Removed.

### Added

#### Auto-memory pipeline
- **Auto-memory contradiction detection (C4)** (#198) — see details below.
- **Auto-memory cluster merge (C3)** (#197) — see details below.
- **Auto-memory cluster pass (C2)** (#196) — see details below.
- **Auto-memory ingest path** (#195) — see details below.
- **Claude Code auto-memory integration guide** (#200) — see details below.
- **`raw/auto-memory` indexed as first-class recall source** (#192) — the
  FTS5/vector index now ingests `raw/auto-memory/<scope>/*.md` alongside
  wiki pages so recall surfaces auto-captured turns before they’re merged.

#### Provenance, dedupe, and contradiction tooling
- **Per-claim `source:` on every CLAIM** (#90) — every emitted claim now
  carries a typed `<type>:<ref>` provenance pointer.
- **Per-value `field_sources` for list fields** (#102) — list-valued
  frontmatter (tags, aliases, etc.) carries per-value provenance instead
  of a single field-level source.
- **Tier 3 emits `source`/`field_sources` + `KNOWN_TYPES` allowlist** —
  the Sonnet writer now produces provenance-shaped output natively.
- **Cross-uid reference rewriter for dedupe** (#103) — `athenaeum dedupe
  persons --apply` rewrites every cross-uid reference to the survivor uid
  in one pass; idempotent.
- **Opus-backed contradiction resolver with provenance precedence** (#126)
  — `athenaeum contradictions resolve` calls Opus on flagged clusters and
  applies a deterministic source-precedence tie-breaker.
- **Cross-scope contradiction-detection mode toggle** (#125) — per-scope
  / cross-scope contradiction detection is now configurable.
- **Pending-questions installable sidecar** (#128) — `athenaeum questions`
  CLI (list / next / count) replaces ad-hoc grep against
  `_pending_questions.md`; consumed by the example SessionStart hook and
  the `resolve-questions` skill.
- **Legacy bare-slug repair migration** (#97) — `athenaeum repair
  --legacy-source-slugs` rewrites pre-#90 `source:` slugs to typed
  `script:<slug>` form; the live tree was migrated 2026-05-09 before the
  parser branch was retired (see Removed).
- **`athenaeum repair` CLI** — dry-run-by-default YAML-frontmatter repair
  for tag-indent corruption, missing fields, and legacy slug migration.

#### Tooling and ingest
- **`athenaeum people` CLI** (#82) — frontmatter-only `type:person` filter
  (company / tag / tier / score, plus `--title-regex` / `--company-regex`).
  No LLM, no embeddings — deterministic over the wiki tree.
- **`athenaeum recall <query>` CLI** (#71) — shell-accessible wrapper
  around the MCP recall tool; see details below.
- **MCP `remember(sources=…)` wrappers** (#96) — the MCP `remember` tool
  now accepts an optional list of typed source pointers; the server
  routes them into the same provenance pipeline the librarian uses.
- **Init templates for entity-author markdown** (#89) — `athenaeum init`
  scaffolds example entity templates so first-time authors have a working
  shape to copy.
- **`tier0_passthrough` preserves pre-structured raw-intake** — raw files
  that already carry `uid` + `type` + `name` round-trip byte-for-byte
  through the librarian without LLM tier costs.
- **Pydantic models + write-time validation** (#88) — wiki frontmatter is
  validated against typed schemas at write time.
- **`extra_intake_root` config warns when missing** — stale config paths
  no longer fail silently at discovery time.
- **p95 search-latency benchmark harness** (#69) — see details below.
- **Auto-memory contradiction detection (C4) (#198) [details]** — new
  `athenaeum.contradictions` module runs one claim-level Haiku call per
  merged cluster to decide whether member bodies state or prescribe
  contradictory things (factual or prescriptive). Wires into
  `athenaeum.merge`: flagged clusters carry `status: contradiction-flagged`
  in their wiki frontmatter and append a round-trippable block to
  `wiki/_pending_questions.md` via the existing `tier4_escalate` helper.
  The C3 centroid-cohesion heuristic (`CONTRADICTION_COHESION_THRESHOLD`)
  is retired as the contradiction signal but kept exported for
  backwards-compatibility. Deterministic fallback: when
  `ANTHROPIC_API_KEY` is unset, every cluster reports `detected=False`
  with `rationale="llm-unavailable"`. Includes
  `scripts/measure_contradiction_baseline.py` for local corpus baselining.
- **Claude Code auto-memory integration guide (#200) [details]** — new
  `docs/integrations/claude-code.md` documents the generic symlink-bridge
  pattern from `~/.claude/projects/<scope>/memory/` into
  `raw/auto-memory/<scope>/`, a citation frontmatter policy, and an
  end-to-end quick start. Adds `examples/claude-code/setup-symlinks.sh`
  (idempotent bridge with `--dry-run`),
  `examples/claude-code/stop-hook-validate.sh` (non-blocking citation
  validator), and `examples/claude-code/auto-memory-frontmatter.example.md`
  (reference memory file). `examples/claude-code/README.md` gains an
  "Auto-memory integration" section linking the three.
- **Auto-memory cluster merge (C3) (#197) [details]** — new
  `athenaeum.merge` module consumes the C2 cluster JSONL and emits one
  consolidated wiki entry per cluster at `wiki/auto-<topic-slug>.md` with
  a deduped `sources[]` union (dedupe key: `(session, turn)`), propagated
  `origin_scope` per source, and a `contradictions_detected` heuristic
  flag (`centroid_score < 0.75`) for the C4 review queue. Size-1
  clusters ARE emitted as wiki entries; raw intake files remain
  untouched. New `--merge-only` CLI flag mirrors `--cluster-only` for
  iterating on merge output without re-embedding.
- **p95 search-latency benchmark harness (#69) [details]** — new
  `tests/benchmarks/test_search_bench.py` checks in the ad-hoc benchmark
  used for the Session-2 recall budget as a pytest-benchmark fixture.
  One bench per backend (keyword, fts5; vector opt-in via
  `ATHENAEUM_BENCH_VECTOR=1`), asserts p95 stays within 20% of a locally
  pinned baseline. Ignored by the default `pytest` run (so CI stays
  fast); execute with `pytest tests/benchmarks/ --benchmark-only`.
  `pytest-benchmark` is an optional `[bench]` extra, not a runtime dep.
- **Auto-memory cluster pass (C2) (#196) [details]** — new
  `athenaeum.clusters` module groups `AutoMemoryFile` records into
  near-duplicate clusters using the existing chromadb `VectorBackend`
  embedder (no parallel embedding pipeline). Single-linkage clustering
  with cosine cutoff configurable via `librarian.cluster_threshold`
  (default 0.55, tuned against the voltaire/nanoclaw regression fixture).
  Writes JSONL cluster report to `raw/_librarian-clusters.jsonl` with
  rotated timestamped siblings. New `--cluster-only` CLI flag skips the
  tier pipeline. C3 merge (#197) consumes the JSONL output.
- **Auto-memory ingest path (#195) [details]** — librarian now discovers
  files under `raw/auto-memory/<scope>/*.md` as a parallel intake channel
  alongside the entity-schema `discover_raw_files`. New
  `AUTO_MEMORY_FILE_RE`, `discover_auto_memory_files()`, and
  `AutoMemoryFile` record carry `origin_scope`, `origin_session_id`,
  `origin_turn`, `memory_type`, and `sources` through to downstream
  tiers. Discovery uses `resolve_extra_intake_roots()` so config is
  single-sourced with recall; `MEMORY.md` and `_migration-log.jsonl`
  are excluded; `_unscoped/` is ingested as a first-class scope.
  Clustering (#196) and wiki merge (#197) ship in subsequent lanes.
- **`athenaeum recall <query>` CLI (#71) [details]** — shell-accessible
  wrapper around the MCP `recall` tool for validation harnesses and
  operator debugging. Prints one tab-separated hit per line
  (`<score>\t<filename>\t<preview>`). Respects configured `search_backend`
  and extra intake roots; `--top-k`, `--path`, `--cache-dir`, and
  `--backend` flags supported.

### Removed
- **BREAKING: retired `provenance._LEGACY_SCALAR_RE` and the legacy
  bare-slug `source:` parser branch.** Pre-#90 wikis stored `source:` as
  a bare slug (`extended-tier-build`, `warm-network-detect`); the live
  tree was migrated on 2026-05-09 via
  `athenaeum repair --legacy-source-slugs --apply` (15,403 wikis rewritten
  to `script:<slug>`). The parser branch and matching schema/test fixtures
  retired in this PR. `provenance.parse_source` now raises `ValueError`
  on bare-slug input with a pointer to the typed `<type>:<ref>` form.
  External callers that still emit bare slugs must switch to the typed
  form. The migration tool (`repair.migrate_legacy_source_slugs`) keeps
  its own internal slug regex and ships unchanged for any future tree
  that needs it. Completes athenaeum#97 acceptance criterion "Remove
  legacy regex branch + its tests" which was deferred from #120 pending
  live-tree migration. The field-keyed `field_sources` legacy reader
  (per `docs/provenance-shape.md` §2.3) is a different legacy form and
  remains accepted on read.
- **BREAKING: extracted `enrich` subcommand and `connectors/apollo` module**
  (#112) — the Apollo people-match connector and the `athenaeum enrich
  --persons` CLI subcommand have been removed from the OSS package. Both
  were Kromatic-specific (operator-curated wiki + Apollo API key) and now
  live in the `code-workspace-config` (cwc) personal toolkit alongside the
  rest of the Apollo enrichment scripts. See
  Kromatic-Innovation/code-workspace-config#235 (merged at sha 8fbe5183)
  for the self-contained replacement. The conflict-resolution
  lock document (`docs/conflict-resolution.md`) drops its former section 8
  (`enrich_person` + CLI write path); the conflict-resolution audit suite
  drops `TestEnrichPersonResolution` and `TestCliEnrichWriteFieldSourcesMerge`.
  No other resolver or schema is affected.

## [0.3.1] - 2026-04-21

Quine follow-ups from the 0.3.0 review. Two small hardening fixes on
error-path code; no behavior change under normal execution.

### Fixed
- **`answers._parse_block` no longer relies on `assert` for control flow**
  (#64). Under `python -O` the assert is stripped and the subsequent
  `cb_match.group(...)` would raise `AttributeError` on malformed blocks
  instead of returning `None`. Replaced with an explicit `if cb_match is
  None: return None` guard so `-O` behaves the same as default execution.
- **CLI error output now includes the exception class name** (#65).
  `Error: {msg}` → `Error ({ExceptionClass}): {msg}`. Makes operator
  triage and Sentry correlation meaningfully easier without changing the
  exit-code contract.

## [0.3.0] - 2026-04-20

Headline feature: **the Tier 4 escalation path is no longer write-only.**
`_pending_questions.md` now supports a full answer loop — humans (or
containerized agents via MCP) can resolve a pending question, and the
librarian ingests the answer as raw intake on the next run. This closes
the human-in-the-loop gap that shipped as a known limitation in 0.2.x.

### Added
- **`athenaeum ingest-answers` CLI** — scans `wiki/_pending_questions.md`,
  converts each `[x]`-checked block into a raw intake file under
  `raw/answers/{ISO-TS}-{entity-slug}.md` (with frontmatter linking back
  to the original source), then moves the block into
  `wiki/_pending_questions_archive.md`. Idempotent — re-running with no
  new `[x]` blocks is a no-op. Malformed blocks are preserved in place
  and logged to stderr, so a single corrupt entry cannot poison the
  rest of the file.
- **`athenaeum.answers` module** — new public surface exposing
  `ingest_answers`, `parse_pending_questions`, `list_unanswered`, and
  `resolve_by_id`. The parser tolerates both schema variants (blocks
  split on `## ` headers or `---` dividers) and emits structured
  `PendingQuestion` dataclasses.
- **Two new MCP tools** — `list_pending_questions()` returns unanswered
  blocks as JSON (id, entity, source, question, conflict_type,
  description, created_at), and `resolve_question(id, answer)` flips
  `- [ ]` -> `- [x]` and writes the answer body below the checkbox.
  Archival is intentionally NOT done at resolve-time — it runs on the
  next `ingest-answers` pass so the write path stays small and the
  archive step is atomic.
- **Checkbox render in `tier4_escalate`** — every new escalation now
  renders a leading `- [ ] {question}` line directly under the header.
  The question is derived from the first line of the LLM's description;
  an empty description falls back to
  `"Resolve {conflict_type} conflict for {entity}"`.
- **`tests/test_answers.py`** — new file covering round-trip,
  idempotency, mixed state, malformed-block tolerance, archive
  newest-first ordering, and MCP-helper happy + error paths.

### Changed
- **`src/athenaeum/__init__.py`** `__version__` bumped `0.2.2 -> 0.3.0`
  (was stale; pyproject was 0.2.3 on the prior release).
- **`tier4_escalate` schema** gained the checkbox line. Existing consumers
  that `.count("## [")` on the file for a pending-question total still
  work unchanged (the header format is preserved verbatim).

## [0.2.3] - 2026-04-21

Documentation and ops release accompanying the launch of
["What We Learned Running Our Own Operations on Agentic Memory"](https://kromatic.com/blog/agentic-memory-in-production/).
No library code changes — every existing install keeps working without
modification.

### Added
- **`docs/why-athenaeum.md`** — evergreen design-rationale doc. Covers the
  three problems that motivated Athenaeum, the four questions a production
  memory system has to answer, and a full comparison against Claude's
  built-in memory, Anthropic's memory tool, RAG, Karpathy's wiki gist, and
  the mem0/Letta/Zep/Cognee category. The four design choices (sources as
  first-class objects, the tiered librarian, passive recall, the editable
  observation filter) are mapped to which failure mode each one fixes.
- **`.github/workflows/promote-main.yml`** — fast-forward `develop → main`
  promotion workflow (workflow_dispatch) matching the rest of the Kromatic
  deploy-pipeline repos. Validates that `main` is a strict ancestor of
  `develop` and that required CI checks passed on the `develop` SHA before
  touching `main`. No merge commits are introduced on `main`, so history
  stays linear.

### Changed
- **`README.md` restructured** around the four design choices. Leads with
  "who this is for" and one-line pointers into `docs/why-athenaeum.md`;
  preserves install / quickstart / MCP / hooks / vector / env vars / data
  formats / known limitations below. The standalone Architecture bullet
  list is gone (now covered in `docs/why-athenaeum.md`).
- **`docs/assets/athena.png`** — replaced with the Athena illustration from
  the launch blog post.
- **`CONTRIBUTING.md`** — documents the develop-first branch flow
  explicitly: feature → `develop`, `develop → main` via the promotion
  workflow, no direct PRs to `main`. Explains why there is no staging
  branch (library repo; releases via PyPI/`release.yml`).

### Fixed
- **`SECURITY.md`** supported-branch statement. The supported release lives
  on `main`; fixes land on `develop` first and are promoted. The prior
  wording had these inverted.

## [0.2.2] - 2026-04-17

Pre-blog-post review pass. Non-breaking fixes surfaced by a final
persona-subagent audit (QA / Design / Developer / Operations / Strategy)
before the public write-up. No behaviour changes for existing users; every
item is hardening, doc clarity, or test-quality.

### Fixed
- **`remember_write` path-traversal guard** replaced string-prefix
  comparison (`str.startswith`) with `Path.is_relative_to`. The old form
  accepted `/a/raw` against `/a/raw-sibling` as a "descendant" — a
  traversal the filesystem treats as a sibling directory. The new form
  resolves both paths and uses the containment check.
- **`athenaeum status` pre-init hint** now matches `serve`: prints the
  remediation `Run 'athenaeum init --path ...' first, then retry.` instead
  of bailing with a bare "not found" line.
- **CI matrix installs `[dev,vector]`** so chromadb is in the test env.
  `TestVectorBackend` and the new hybrid rescue-class tests would have
  shown up as `importorskip` skips on GitHub Actions without this change.
- **CI actions pinned by SHA** to match `release.yml` (supply-chain
  hardening against tag retag attacks).

### Added
- **Hook README explains the two-phase pipeline** — new "How the sidecar
  works (read this first)" section makes the raw→wiki compile step
  explicit, plus a step-6 walk-through for scheduling periodic
  `athenaeum run` via cron or launchd, plus a troubleshooting row for
  "`remember` saves but `recall` finds nothing" (which is the expected
  state until compilation runs).
- **Main README "Known limitations (v0.2.x)" section** — no retrieval
  benchmarks yet, FTS5 rebuild non-atomic+unlocked, keyword backend is
  scan-on-query, Tier 4 is a file not a workflow. Sets adopter
  expectations up front instead of surfacing them in issues.
- **Main README vector-search section expanded** with the concrete
  rescue-class evidence (proper-noun collision: `Return Path`; no-overlap
  semantic query: `iterative feedback loops` → Innovation Accounting) and
  a pointer to `docs/recall-architecture.md`.
- **`tests/test_roundtrip.py`** — end-to-end integration pinning
  init → remember → seed wiki → rebuild-index → recall with an explicit
  `--cache-dir` (regression guard for the class of bugs where a path
  default drifts and layers still pass their own unit tests but no
  longer talk to each other). Also pins that the keyword backend works
  without any cache on fresh installs.
- **`TestHybridRescueClasses` in `tests/test_search.py`** — builds a
  synthetic wiki that exposes each backend's blind spot (short proper
  noun for vector; semantic no-overlap query for FTS5) and asserts each
  backend rescues its class while the other misses it. Without this
  pin, a future simplification that drops one backend still passes
  `test_query_finds_match` on obvious lexical queries and quietly
  regresses the rescue path.
- **`TestWarnIfBackendCacheMissing` in `tests/test_cli.py`** — exercises
  all four branches of the startup warning (keyword no-op, fts5 missing,
  vector missing, unknown backend) so the silent-zero-hits failure mode
  can't regress.
- **`tests/test_query_topics.py::test_returns_empty_when_api_raises`**
  extended with a `caplog` assertion pinning the WARNING-level log and
  the exception class name on API failures.

### Changed
- **Test assertions tightened** — several tests used `or` / loose
  containment where `and` / explicit-equality was meant. Most load-bearing
  of these: `test_recall_skips_underscore_files` (was masked by the
  trivial-pass shape `"score:" not in result or "_index" not in result`
  when "No wiki pages matched" was returned); `test_tiers.py:558`
  escalation description now asserts both `fintech` AND `pivot`;
  `test_shell_hooks.py::test_exits_clean_with_no_index` now asserts
  stderr is clean of `Traceback` / `syntax error` / `command not found`
  so "crashed quietly" can't pass as "correctly bailed."
- **`_snippet` offset math now pinned** by two new tests covering the
  match-near-start (trailing ellipsis) and match-near-end (leading
  ellipsis) branches.

## [0.2.1] - 2026-04-17

Final pre-public-announce review pass. No user-visible behaviour changes;
every fix is either first-adopter ergonomics, supply-chain hardening, or
internal abstraction hygiene so a future contributor can't trip on the
same sharp edges the review found.

### Fixed
- **`athenaeum serve` pre-init hint** — the "Run `athenaeum init --path {args.path}`" line printed the literal placeholder because the f-string prefix was missing; pre-init users saw `{args.path}` and lost trust
- `VectorBackend.query` now logs a WARNING with the exception class name when `get_collection` fails, instead of swallowing silently — "vector returns nothing" was the top first-adopter confusion in the v0.2.0 review
- `query_topics` API failures now log at WARNING (was DEBUG) with the exception class name — silent fall-through to the regex extractor hid degraded proper-noun recall

### Changed
- **Release workflow hardened** — all GitHub Actions pinned to full commit SHAs (supply-chain hardening against tag retag attacks); PEP 740 build-provenance attestations enabled
- **Search backend unification** — the in-memory keyword scorer is now a first-class `KeywordBackend` in `athenaeum.search`, registered alongside FTS5 and vector via the same `SearchBackend` Protocol; `mcp_server.recall_search` dispatches all three backends through one code path
- **`athenaeum serve` cache sanity check** — warns on startup when the configured `search_backend` has no index on disk (so recall doesn't silently return zero hits)
- `EntityIndex` now exposes `__iter__`, `__len__`, and `items()`; callers no longer reach into `_by_name` directly
- Public API types tightened: `status()` returns a `StatusInfo` TypedDict; `parse_frontmatter` / `render_frontmatter` use `dict[str, object]`
- Dependency upper bounds added to `anthropic` (<1.0), `fastmcp` (<3.0), `chromadb` (<2.0), `pyyaml` (<7) — surprise majors can't silently break a released wheel
- Wheel build explicitly includes `src/athenaeum/py.typed` and `src/athenaeum/schema/**/*.md` (was implicit via hatchling defaults)
- README links to GitHub-hosted docs instead of relative paths so they render on PyPI
- Claude Code call-out added to the README tagline pointing to the transparent sidecar section
- `examples/claude-code/README.md` leads with `claude mcp add` as the preferred MCP install recipe (matches canonical form)

## [0.2.0] - 2026-04-17

> **Adopter note.** The vector backend and the Claude Code hook flow ship
> working — but the hook flow assumes you follow the hybrid-recall pattern
> described in [`docs/recall-architecture.md`](docs/recall-architecture.md).
> Four load-bearing invariants (`set -a` around config sourcing, hybrid
> FTS5+vector merge, `hookSpecificOutput.hookEventName` wrapper, console
> API key vs `CLAUDE_CODE_OAUTH_TOKEN`) each shipped CI-green the first
> time they broke — every one of them is a silent failure mode. Read
> `docs/recall-architecture.md` before simplifying any of them.

### Added
- **Vector search backend** with chromadb + `all-MiniLM-L6-v2` (#31, #32)
- `athenaeum query-topics` CLI — Haiku-based query preprocessor that extracts substantive topics from instruction-heavy prompts and ignores meta-instructions (#41, #42)
- `athenaeum rebuild-index` CLI for on-demand index rebuilds (#34)
- `athenaeum test-mcp` CLI subcommand for verifying MCP setup (#36)
- Observation filter wired as the tunable capture authority for the sidecar flow (#37)
- `athenaeum.yaml` config with `auto_recall` toggle and `search_backend` selection (#30)
- Example Claude Code hooks — SessionStart builds the FTS5 index, UserPromptSubmit queries it per turn, PreCompact nudges save-before-compact
- FTS5-based per-turn wiki recall hook
- `docs/recall-architecture.md` describing the hybrid FTS5+vector pipeline, why each backend is load-bearing, and the four invariants a future "simplification" must not remove
- Example hooks (`user-prompt-recall.sh`, `session-start-recall.sh`) updated to match production hardening — `set -a`/`set +a` around config sourcing so child processes inherit `ANTHROPIC_API_KEY`; hybrid FTS5+vector merge; optional 1Password bootstrap of `ANTHROPIC_API_KEY` at SessionStart
- README sections for the `[vector]` extra and the `athenaeum query-topics` subcommand
- `SECURITY.md` with private disclosure process
- `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1)
- Issue templates (bug / feature) and PR template with a "hook changes only" live-run checklist
- Apache 2.0 `SPDX-License-Identifier` header on all Python source files

### Fixed
- **UserPromptSubmit hook JSON shape** — must be `{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"..."}}`; a flat `{"additionalContext":...}` payload was silently ignored by Claude Code (#39, #40)
- **Stale chromadb state across rebuilds** — `VectorBackend.build_index` now nukes `vector_dir` and calls `SharedSystemClient.clear_system_cache()` to avoid "Collection already exists" on repeat builds in the same process (#33)
- `serve` CLI now forwards `search_backend` + `cache_dir` to the MCP server (#38)
- Recall now fires after the first message, not only at session start
- Stale "vector backend is a stub for issue #32" comment in `search.py`

## [0.1.0] - 2026-04-16

Initial public release — a genericised extraction of the Kromatic
knowledge librarian.

### Added
- Tiered compilation pipeline (programmatic → Haiku → Sonnet → human escalation)
- Append-only raw intake with frontmatter-driven entity schema
- Configurable schemas and observation filter
- `athenaeum init` CLI to bootstrap a new knowledge directory
- `athenaeum run` pipeline CLI with `--dry-run`, `--max-files`, `--max-api-calls`
- `athenaeum status` — entity counts, pending files, last run
- MCP memory server (`athenaeum serve`) exposing `remember` and `recall` tools
- Token tracking and per-run cost estimates
- Test suite extracted from upstream + CI coverage enforcement (`>=75%`)
- Transactional writes, type-safety hardening, prompt-injection mitigation, API budget caps

[Unreleased]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.7.3...HEAD
[0.7.3]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.7.2...v0.7.3
[0.7.2]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.7.1...v0.7.2
[0.7.1]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.6.1...v0.7.0
[0.6.1]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.2.3...v0.3.0
[0.2.3]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Kromatic-Innovation/athenaeum/releases/tag/v0.1.0
