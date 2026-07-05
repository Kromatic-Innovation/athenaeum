# Configuration Reference

This is the canonical reference for every operator-tunable knob in Athenaeum.
Other docs (README, [auto-resolve.md](auto-resolve.md),
[contradiction-detection.md](contradiction-detection.md)) link here instead of
maintaining their own copies of the tables.

## Precedence

Settings resolve in the order:

> **CLI flag > environment variable > `athenaeum.yaml` > code default**

(established in #220 for `--max-api-calls` and generalized in #232). Not every
knob has every layer — an em dash (—) in a table cell below means that layer
does not exist for that knob. An env override always beats the yaml, so a
one-off shell export changes a single run without editing config.

`athenaeum.yaml` lives at the knowledge root
(`<knowledge_root>/athenaeum.yaml`, default `~/knowledge/athenaeum.yaml`);
`athenaeum init` writes a commented template covering the most common yaml
keys; the full set of knobs is the tables on this page. Keys you do not set
fall through to the code defaults — the loader
deliberately does not seed defaults for keys whose source of truth lives next
to their consumer code (#231), so a future change to a code default takes
effect without a config migration.

Every default figure on this page is verified against the code under
`src/athenaeum/`. When another doc and the code disagree, the code is truth.

## Librarian run (`athenaeum run`)

| Knob | CLI flag | Env var | YAML key | Default | What it does |
|---|---|---|---|---|---|
| Intake batch size | `--max-files` | `ATHENAEUM_MAX_FILES` | `librarian.max_files` | `50` | Stop after processing this many raw files per run (#232). Env `0` is valid (defer-everything window); the CLI flag rejects `0`. |
| API call budget | `--max-api-calls` | `ATHENAEUM_MAX_API_CALLS` | `librarian.max_api_calls` | `800` | Run-level cap on estimated API calls (#220, raised from 200). A budget-tripped run is DEGRADED: it writes `wiki/_deferred_work.md` and defers remaining intake. Env `0` is valid (defers the entire intake); the CLI flag rejects `0`. |
| Strict budget exit | `--strict-budget` | — | — | off | Make a budget-tripped (DEGRADED) run exit nonzero instead of `0`, for exit-code-based alerting (#227). |
| Batch API mode | `--batch-mode` / `--no-batch-mode` | `ATHENAEUM_BATCH_MODE` | `librarian.batch_mode` | off | Submit tier-2/tier-3 LLM calls via the [Anthropic Messages Batch API](https://platform.claude.com/docs/en/build-with-claude/batch-processing) at a 50% token discount (#236). Latency-tolerant: most batches finish within an hour, 24h worst case — intended for the nightly run. Same-page tier-3 merges stay synchronous; the budget cap is enforced at batch-assembly time (re-checked per file at phase-2 assembly and before the synchronous merges). `--no-batch-mode` forces the synchronous path even when env/yaml turn batch mode on. |
| Cluster threshold | — | — | `librarian.cluster_threshold` | `0.55` | Cosine cutoff for auto-memory near-duplicate clustering (C2, #196). Higher = tighter clusters. |
| Cluster output | — | — | `librarian.cluster_output` | `raw/_librarian-clusters.jsonl` | Canonical cluster JSONL path, resolved relative to the knowledge root. Each run also writes a timestamped sibling. |
| Rotation retention | — | `ATHENAEUM_ROTATION_RETENTION` | `librarian.rotation_retention` | `30` | Number of timestamped cluster-report rotations to keep; older ones are pruned after each run (#311). Rotations are debugging artifacts, not recovery-critical (recovery is git-based). `0` (or negative) disables pruning (keep all). A prune failure is a non-fatal warning. |
| Ephemeral scopes | — | — | `librarian.ephemeral_scopes` | `[]` | Glob patterns (matched against the auto-memory scope) whose raw intake is classified ephemeral and dropped before clustering (#280), so operational/throwaway scopes never materialize a durable `wiki/auto-*.md` page. Default-empty (off). |
| Operational markers | — | — | `librarian.operational_markers` | `[]` | Lower-cased content substrings that, when `>= 2` are present in a raw auto-memory file, classify it as ephemeral operational boilerplate (#280). Conservative multi-signal gate; default-empty so nothing fires until an operator opts in. Lower-precedence than an explicit `ephemeral: true` frontmatter flag or an `ephemeral_scopes` match. |
| Cluster-cohesion floor | — | — | `librarian.min_cluster_cohesion` | `0.0` | Cohesion floor that suppresses low-cohesion cross-scope over-clusters (#281). A cluster is withheld only when its `cluster_centroid_score` is strictly below this value **AND** it spans `>= min_cluster_cohesion_scopes` distinct origin scopes. Default `0.0` = OFF (the cutoff is corpus-specific); `0.47` is recommended for the reference corpus. Suppressed clusters leave their raw members in place (not retired). |
| Cohesion-floor scope count | — | — | `librarian.min_cluster_cohesion_scopes` | `4` | Minimum distinct `origin_scope` count a low-cohesion cluster must span before the `min_cluster_cohesion` floor suppresses it (#281). Legitimate pages span 1-3 scopes and over-clusters span 8-17, so `4` is the clean margin — a low-cohesion single-/few-scope cluster is never false-suppressed. Inert while `min_cluster_cohesion` is `0.0`. |
| Embedding cache root | — | `ATHENAEUM_CACHE_DIR` | — | `~/.cache/athenaeum` | Cache root used by the librarian's cluster pass (chromadb lives at `<dir>/wiki-vectors/`). The `recall` / `rebuild-index` commands do **not** read this var — they take `--cache-dir` (same default). |
| Post-run git push | `--push` | — | `librarian.push_after_run` | off | Push the knowledge repo to its remote after a successful run that produced at least one commit (#284). Closes the move-then-retire recovery gap on multi-machine setups: without it, scheduled nightly runs commit locally but origin silently drifts. Uses the operator's ambient git auth (credential helper / SSH); athenaeum handles no tokens or secrets. `--dry-run` never pushes; a run with no new commits never pushes; a push failure is a non-fatal warning (`athenaeum-push-failed:`) and the next run retries. Remote/branch come from `librarian.push_remote` (default `origin`) and `librarian.push_branch` (default: current branch's upstream). |
| API key | — | `ANTHROPIC_API_KEY` | — | (required) | Required for Tier 2/3 LLM calls. Optional with `--dry-run`, `--cluster-only`, or `--merge-only`. |

> **Design decision — CLI rejects `0`, env/yaml accept it.** The
> `--max-api-calls` and `--max-files` flags reject `0` at parse time as a
> typo guard at the interactive surface, while `ATHENAEUM_MAX_API_CALLS=0` /
> `librarian.max_api_calls: 0` (and the `max_files` equivalents) are accepted
> as deliberate defer-everything caps for scripted deployments. This
> asymmetry is intentional, not an oversight (decided 2026-06-12; refs #235
> and the #240 review). A run whose budget resolves to `0` logs a prominent
> warning at start so an accidental zero is diagnosable immediately.

Path and mode flags on `athenaeum run` (CLI-only): `--raw-root` and
`--wiki-root` (default under the knowledge root), `--knowledge-root` /
`--path` (default `~/knowledge`), `--dry-run`, `--cluster-only`,
`--merge-only`, `--verbose`.

## Models

All model values are free-form model-id strings passed to the Anthropic SDK.
The first three live under the `models:` yaml block (#232); the resolver model
is configured separately under `resolve:`.

| Knob | Env var | YAML key | Default | Used by |
|---|---|---|---|---|
| Classifier | `ATHENAEUM_CLASSIFY_MODEL` | `models.classify` | `claude-haiku-4-5-20251001` | Tier-2 classifier **and** the C4 contradiction detector — one knob by design. |
| Writer | `ATHENAEUM_WRITE_MODEL` | `models.write` | `claude-sonnet-4-6` | Tier-3 wiki writer. |
| Topic extractor | `ATHENAEUM_TOPIC_MODEL` | `models.topic` | `claude-haiku-4-5-20251001` | `athenaeum query-topics` recall query rewriting. |
| Resolver | `ATHENAEUM_RESOLVE_MODEL` | `resolve.model` | `claude-opus-4-7` | Contradiction resolver (proposes a winner once the detector flags a conflict). |

## Contradiction detection and resolver

Detection knobs live under the `contradiction:` yaml block; resolver knobs
under `resolve:`. Pipeline walkthrough:
[contradiction-detection.md](contradiction-detection.md); auto-apply lane:
[auto-resolve.md](auto-resolve.md).

| Knob | Env var | YAML key | Default | What it does |
|---|---|---|---|---|
| Cross-scope mode | `ATHENAEUM_CROSS_SCOPE_MODE` | `contradiction.cross_scope_mode` | `ancestor` | `off` / `ancestor` / `similarity` / `both` (#125). Invalid env values log a warning and fall back. |
| Cluster size cap | — | `contradiction.cluster_size_cap` | `25` | Pooled-cluster size cap; oversized pools are split into newest-first chunks before detection. |
| Similarity threshold | — | `contradiction.similarity_threshold` | `0.85` | Cosine cutoff for the cross-scope similarity sweep (`similarity` / `both` modes). |
| Resolver cap per run | `ATHENAEUM_RESOLVE_MAX_PER_RUN` | `contradiction.resolve_max_per_run` | `250` | Per-ingest cap on resolver calls (raised from 50 in #187). Surplus detections escalate without a proposal. `0` disables the resolver entirely. |
| Resolved-similarity threshold | `ATHENAEUM_RESOLVED_SIMILARITY_THRESHOLD` | `contradiction.resolved_similarity_threshold` | `0.83` | Cosine threshold for matching a new detection against the decision log of previously resolved contradictions (#211). |
| Not-a-conflict TTL (days) | `ATHENAEUM_NOT_A_CONFLICT_TTL_DAYS` | `contradiction.not_a_conflict_ttl_days` | `0` | Read-time decay of stale **auto** `not_a_conflict` suppressions (#251). `0` disables decay (current behavior — a suppression never expires). When `> 0`, an auto suppression whose `resolved_at` is older than this many days is treated as absent from the confirmation-pass skip set, so the pair re-enters the Opus confirmation. Human verdicts and enacting auto verdicts (`keep_*`/`correct_*`/`forget_*`/`deprecate_both`) never decay; undated rows keep suppressing (fail-safe). The append-only cache is never mutated; re-validation flows through the existing `resolve_max_per_run` cap. |
| Auto-apply | `ATHENAEUM_RESOLVE_AUTO_APPLY` | `resolve.auto_apply` | `true` | Apply high-confidence resolver proposals without human review (#156). Env accepts `true`/`false`, `1`/`0`, `yes`/`no` (case-insensitive). |
| Auto-apply threshold (legacy scalar) | `ATHENAEUM_RESOLVE_AUTO_APPLY_THRESHOLD` | `resolve.auto_apply_threshold` | `0.90` | Confidence floor in `[0.0, 1.0]`; out-of-range values raise on read. Since #170 this scalar is honored only as a backward-compat fallback for `keep_a` / `keep_b`. |
| Per-action thresholds | — | `resolve.auto_apply_threshold_per_action` | `not_a_conflict: 0.75`, `keep_a`/`keep_b`/`deprecate_both`: `0.90`, `correct_a`/`correct_b`/`forget_a`/`forget_b`: `0.95` | Per-action confidence floors (#170, #191). `propose_merge` **never** auto-applies regardless of confidence. |
| Full-body token cap | `ATHENAEUM_RESOLVE_FULL_BODY_TOKEN_CAP` | `resolve.full_body_token_cap` | `1500` | Per-side body cap for the resolver's full-body context (#168), ~4 chars/token. Must be a positive integer; zero/negative raise — set a large value to effectively disable truncation. |
| Tier-4 escalation dedup | `ATHENAEUM_TIER4_DEDUP` | — | `true` | Dedupe `_pending_questions.md` escalations by source-memory pair (#157). Set `false`/`0`/`no`/`off` to restore the legacy always-append behavior. |

## Recall and search

| Knob | CLI flag | Env var | YAML key | Default | What it does |
|---|---|---|---|---|---|
| Auto-recall | — | `AUTO_RECALL` (hook shell env) | `auto_recall` | `true` | Per-turn recall via the UserPromptSubmit hook. The shell env is read by the example hooks and beats the yaml. |
| Search backend | `--backend` (`recall` / `rebuild-index`) | `SEARCH_BACKEND` (hook shell env) | `search_backend` | `fts5` | `fts5` (SQLite FTS5, BM25 + porter stemming) or `vector` (chromadb + `all-MiniLM-L6-v2`, needs `pip install athenaeum[vector]`). `athenaeum recall --backend keyword` additionally exposes the zero-dependency scan-on-query fallback. |
| Extra intake roots | — | — | `recall.extra_intake_roots` | `["raw/auto-memory"]` | Additional directories (relative to the knowledge root) scanned recursively into the recall index. Set `[]` to restrict recall to the compiled wiki. |
| Recall result count | `--top-k` (`recall`) | — | — | `5` | Hits returned by the shell `recall` command. |
| Index cache dir | `--cache-dir` (`recall` / `rebuild-index`) | — | — | `~/.cache/athenaeum` | Where the FTS5 db / chromadb collection live. |
| Topic-extraction timeout | `--timeout` (`query-topics`) | — | — | `3.0` | Seconds before `query-topics` gives up and the hook falls back to the regex extractor. |
| Topic-extraction config root | `--knowledge-root` / `--path` (`query-topics`) | — | — | `~/knowledge` | Knowledge root whose `athenaeum.yaml` supplies `models.topic` (#232). |

**Reserved keys (not yet read by code).** `vector.provider` (default
`chromadb`) and `vector.collection` (default `wiki`) appear in the loader's
`_DEFAULTS` seed but no code reads them yet — the vector backend hardcodes
chromadb and the `wiki` collection name. Setting either key has no effect
today.

## Hook / sidecar environment (examples/claude-code)

These are read by the example shell hooks, not by the Python package. Setup
guide: [`examples/claude-code/README.md`](../examples/claude-code/README.md).

| Variable | Default | Purpose |
|---|---|---|
| `KNOWLEDGE_ROOT` | `~/knowledge` | Knowledge base root |
| `KNOWLEDGE_WIKI_PATH` | `$KNOWLEDGE_ROOT/wiki` | Wiki directory (non-standard layouts) |
| `ATHENAEUM_CLI` | `athenaeum` | CLI binary (override for editable installs) |
| `ATHENAEUM_PYTHON` | `python3` | Python interpreter with athenaeum deps |
| `ATHENAEUM_SRC` | — | Source checkout path (skips `pip install`, runs from source) |
| `ATHENAEUM_OP_KEY_PATH` | `op://Agent Tools/Anthropic API Key/credential` | 1Password secret reference for the `ANTHROPIC_API_KEY` bootstrap |
| `ATHENAEUM_HOOK_DEBUG` | `0` | `1` logs vector-backend errors to stderr |
| `ATHENAEUM_FORCE_REBUILD` | `0` | `1` forces a vector-index rebuild even when fresh |
| `ATHENAEUM_INJECT_SKIP_WORDS` | `Code\|Users\|home\|workspace\|src\|lib\|app\|var\|tmp\|usr` | Pipe-separated cwd segments ignored by `wiki-context-inject.sh` |
| `ATHENAEUM_INJECT_MAX_RESULTS` | `3` | Max wiki pages surfaced by `wiki-context-inject.sh` |
| `ATHENAEUM_PQ_SNOOZE_HOURS` | `24` | Snooze TTL for pending-questions surfacing. Consumed by the `resolve-questions` skill when writing the snooze file; the SessionStart hook only reads the file. |
| `ATHENAEUM_PQ_HOOK_DEBUG` | `0` | `1` logs `pending-questions-surface.sh` diagnostics to stderr |
| `AUTO_RECALL` | from `athenaeum.yaml` (`true`) | Shell-env override for per-turn recall |
| `SEARCH_BACKEND` | from `athenaeum.yaml` (`fts5`) | Shell-env override for the search backend |

## Alternative model gateways (`ANTHROPIC_BASE_URL`)

Athenaeum makes all model calls through the Anthropic Python SDK, and the SDK
honors the standard `ANTHROPIC_BASE_URL` environment variable. Pointing it at
a [LiteLLM](https://docs.litellm.ai/) proxy — or any Anthropic-compatible
gateway — therefore lets you serve alternative models behind the model knobs
above with zero code change: set `ANTHROPIC_BASE_URL` (plus whatever
`ANTHROPIC_API_KEY` the gateway expects) and map the configured model ids to
the gateway's upstream targets. The honest caveat: only Claude models are
first-party tested. The classifier, writer, and resolver prompts are tuned
against the defaults in the Models table, and output quality on other models
is yours to evaluate. Native multi-provider support is tracked in
[#234](https://github.com/Kromatic-Innovation/athenaeum/issues/234) — if you
want it, register your use case there.

## Example `athenaeum.yaml`

```yaml
auto_recall: true
search_backend: fts5

recall:
  extra_intake_roots:
    - raw/auto-memory

librarian:
  cluster_threshold: 0.55
  cluster_output: raw/_librarian-clusters.jsonl
  rotation_retention: 30        # timestamped rotations to keep; 0 = keep all (#311)
  max_files: 50
  max_api_calls: 800
  batch_mode: false
  ephemeral_scopes: []          # scope globs dropped as ephemeral intake (#280)
  operational_markers: []       # >=2 lower-cased substrings => ephemeral (#280)
  min_cluster_cohesion: 0.0     # 0.0 = OFF; cohesion floor (#281)
  min_cluster_cohesion_scopes: 4  # scope-span gate for the cohesion floor (#281)

models:
  classify: claude-haiku-4-5-20251001
  write: claude-sonnet-4-6
  topic: claude-haiku-4-5-20251001

contradiction:
  cross_scope_mode: ancestor
  cluster_size_cap: 25
  similarity_threshold: 0.85
  resolve_max_per_run: 250
  resolved_similarity_threshold: 0.83
  not_a_conflict_ttl_days: 0  # 0 = disabled; >0 decays stale auto not_a_conflict (#251)

resolve:
  model: claude-opus-4-7
  auto_apply: true
  auto_apply_threshold: 0.90
  full_body_token_cap: 1500
  # auto_apply_threshold_per_action:
  #   not_a_conflict: 0.75
  #   keep_a: 0.90
  #   keep_b: 0.90
```
