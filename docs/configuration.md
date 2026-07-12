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
| Page warn size | — | `ATHENAEUM_PAGE_WARN_BYTES` | `librarian.page_warn_bytes` | `8192` | Soft byte threshold above which a wiki entity page is reported as a **warn**-level oversized page in `athenaeum status` (#310). Warn-only: nothing is blocked or modified. A long page usually means poorly-factored knowledge that should be split into linked sub-entities. `bool` / non-int / `<= 0` values fall through to the default. |
| Page flag size | — | `ATHENAEUM_PAGE_FLAG_BYTES` | `librarian.page_flag_bytes` | `16384` | Byte threshold above which a page is **flagged** for splitting — surfaced in `status` and logged as a non-fatal `WARNING` during `athenaeum run` (#310). Still warn-only (never blocks; the tier-3 merge body cap is separate and unchanged). A flagged page appears only in the flag bucket, not also in warn. Keep comfortably below the merge body cap. |
| Embedding cache root | — | `ATHENAEUM_CACHE_DIR` | — | `~/.cache/athenaeum` | Cache root used by the librarian's cluster pass (chromadb lives at `<dir>/wiki-vectors/`). The `recall` / `rebuild-index` commands do **not** read this var — they take `--cache-dir` (same default). |
| Post-run git push | `--push` | — | `librarian.push_after_run` | off | Push the knowledge repo to its remote after a successful run that produced at least one commit (#284). Closes the move-then-retire recovery gap on multi-machine setups: without it, scheduled nightly runs commit locally but origin silently drifts. Uses the operator's ambient git auth (credential helper / SSH); athenaeum handles no tokens or secrets. `--dry-run` never pushes; a run with no new commits never pushes; a push failure is a non-fatal warning (`athenaeum-push-failed:`) and the next run retries. Remote/branch come from `librarian.push_remote` (default `origin`) and `librarian.push_branch` (default: current branch's upstream). |
| Run-lock wait | `--wait` | `ATHENAEUM_LOCK_TIMEOUT` | `librarian.lock_timeout` | `0` | Default seconds a mutating command blocks for the single-machine run lock before failing (#309). `0` = fail-fast (name the holder, exit non-zero). The `--wait` flag overrides per-invocation. See the run-lock note below. |
| Delta-scoped compile | — | — | `librarian.delta.enabled` | `true` | Enable delta-scoped incremental compile on the deterministic (`client=None`) path — `session-end` / `ingest` tier0 (#370). When on, re-cluster and re-merge only the clusters a change actually touches instead of the whole auto-memory corpus; byte-equivalent to the whole-corpus path. Set `false` to always compile whole-corpus. The nightly LLM `run` always stays whole-corpus regardless of this flag. `bool` yaml values are honored; anything else falls through to the `true` default. |
| Delta affected-cluster cap | — | — | `librarian.delta.max_affected_clusters` | `8` | If a change would touch more than this many clusters, fall back to a full whole-corpus compile rather than churning most of the corpus through the delta path (#370). `bool` / non-positive / non-int values fall through to the default. |
| Delta affected-member cap | — | — | `librarian.delta.max_affected_members` | `200` | If the affected-cluster member pool exceeds this many files, fall back to a full compile (#370). Bounds worst-case re-cluster cost so a pathological closure never does more work than a full run. `bool` / non-positive / non-int values fall through to the default. |
| Full-rehash backstop age (days) | — | — | `librarian.reindex.full_rehash_max_age_days` | `7` | Self-healing periodic full re-hash backstop (#373). The #370 stat pre-filter reuses a stored content hash whenever a file's `(mtime, size)` match the index manifest; when the manifest has not had a full re-hash within this window, the next incremental reindex re-hashes **every** file (catching a content edit that preserved both mtime and size) while still applying only the delta — seconds, not a full re-embed / FTS5 rebuild. `0` or negative = always re-hash; a very large value = effectively never. `bool` / non-numeric values fall through to the default. |
| API key | — | `ANTHROPIC_API_KEY` | — | (required) | Required for Tier 2/3 LLM calls. Optional with `--dry-run`, `--cluster-only`, or `--merge-only`. |

> **Design decision — CLI rejects `0`, env/yaml accept it.** The
> `--max-api-calls` and `--max-files` flags reject `0` at parse time as a
> typo guard at the interactive surface, while `ATHENAEUM_MAX_API_CALLS=0` /
> `librarian.max_api_calls: 0` (and the `max_files` equivalents) are accepted
> as deliberate defer-everything caps for scripted deployments. This
> asymmetry is intentional, not an oversight (decided 2026-06-12; refs #235
> and the #240 review). A run whose budget resolves to `0` logs a prominent
> warning at start so an accidental zero is diagnosable immediately.

> **Backstop guarantee (#373).** The #370 stat pre-filter reuses a stored
> content hash when a file's `mtime` and `size` both match the manifest, so a
> content edit that preserves BOTH would otherwise slip past an incremental
> reindex indefinitely. `librarian.reindex.full_rehash_max_age_days` bounds that
> worst case: such an edit is guaranteed to surface within
> `full_rehash_max_age_days` (default ≤ 7 days), even if nothing else triggers a
> re-hash in the meantime.

Path and mode flags on `athenaeum run` (CLI-only): `--raw-root` and
`--wiki-root` (default under the knowledge root), `--knowledge-root` /
`--path` (default `~/knowledge`), `--dry-run`, `--cluster-only`,
`--merge-only`, `--verbose`.

### Run lock (single-machine concurrency guard, #309)

Every **mutating** command acquires an exclusive advisory
[`fcntl.flock`](https://man7.org/linux/man-pages/man2/flock.2.html) on
`<knowledge_root>/.athenaeum.lock` at startup, so overlapping runs (a nightly
cron overlapping a manual invocation, or two editor sessions) cannot race
whole-file wiki writes, interleave block appends to the `_pending_*.md`
sidecars, double-spend the API-call budget, or race the move-then-retire git
ops. The lockfile records the holder's PID, an ISO-8601 timestamp, and the
hostname for diagnostics.

- **Locked commands:** `run`, `ingest-answers`, `ingest-merges`,
  `reresolve-questions`, `rebuild-index`, `auto-memory prune --apply`,
  `repair --apply`, `dedupe persons --apply`, and `dedupe wiki-pages`
  (non-`--dry-run`).
- **Never locked:** `status`, `recall`, `serve`, and every `--dry-run`
  (they don't mutate the knowledge base).
- **Default** — fail fast with a message naming the holder (PID + age) and a
  non-zero exit.
- **`--wait <seconds>`** — block up to the timeout for the lock, then fail if
  still held. Default from `librarian.lock_timeout` / `ATHENAEUM_LOCK_TIMEOUT`
  (`0` = fail-fast).
- **`--force`** — break the lock **even if a process is still holding it** (the
  current holder is logged first for an audit trail) and proceed. Use ONLY when
  you are certain the holder is hung or dead, and never run two `--force`
  invocations concurrently. Note: because the kernel releases an `flock` the
  moment its holder dies, a genuinely crashed run never blocks a normal acquire
  — `--force` exists to override a live-but-hung holder.

**Scope is single-machine only.** `flock` is advisory and unreliable across
network filesystems, so this guard makes no attempt at multi-machine
coordination (use `librarian.push_after_run` + a single scheduler host for
multi-machine setups). On non-POSIX platforms without `fcntl`, the lock
degrades gracefully: a warning is logged and the command runs unlocked.

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

## LLM provider selection (#330)

Athenaeum's librarian pipeline talks to Claude through a single **provider
seam** (`athenaeum.provider.build_llm_client`). Two backends ship:

| Knob | Env var | YAML key | Default | Used by |
|---|---|---|---|---|
| LLM provider | — | `ATHENAEUM_LLM_PROVIDER` | `llm.provider` | `api` | Selects the LLM backend for the librarian compile path (tiers, contradiction detector, resolver). `api` = the Anthropic SDK; `claude-cli` = the operator's ambient Claude Code subscription login. An unrecognized value is a hard error (no silent fallback). |
| CLI binary | — | `ATHENAEUM_CLAUDE_CLI_BIN` | — | `claude` | Override the `claude` executable used by the `claude-cli` backend (editable installs / non-PATH locations). |
| CLI timeout | — | `ATHENAEUM_CLAUDE_CLI_TIMEOUT` | — | `300` (s) | Per-call subprocess timeout for the `claude-cli` backend. A timeout is treated as a **transient** error — not retried in-run; the affected file is deferred to the next run. |

### `api` (default)

Wraps `anthropic.Anthropic(...)` verbatim: every request parameter — including
`cache_control` prompt-caching breakpoints (#230) and the Messages Batch API
(#236) — passes through unchanged. Requires `ANTHROPIC_API_KEY` (see below).
Behavior is byte-for-byte identical to pre-#330 releases.

### `claude-cli` (subscription)

Drives your logged-in Claude Code via
`claude -p --system-prompt <sys> --model <id> --output-format json`, billing
the LLM work to your Claude **subscription** rather than a per-token API bill.
Athenaeum performs **no credential handling** — it relies on your ambient
`claude` login exactly as the post-run `git push` (#284) relies on your ambient
git auth. Enable it with:

```yaml
llm:
  provider: claude-cli
```

or `ATHENAEUM_LLM_PROVIDER=claude-cli athenaeum run …`.

Constraints and semantics:

- **No API key needed.** The `ANTHROPIC_API_KEY` requirement is waived for
  `claude-cli`; the run authenticates via your Claude Code login.
- **`cache_control` is stripped.** Caching breakpoints do not apply to the CLI
  transport (they are preserved untouched on the `api` backend).
- **`max_tokens` is advisory (possible truncation on very large merges).** The
  CLI has no per-request output-token flag; the model applies its own cap. A
  tier-3 merge over an unusually large page could therefore truncate its JSON
  answer, which the lenient extractor then rejects → that file degrades to a
  fallback / deferral rather than a bad write. Split oversized pages (see the
  page-size knobs above) if this bites.
- **The tier prompt does not inherit Claude Code's persona.** `--system-prompt`
  fully replaces the default agent persona with athenaeum's tier prompt, and the
  subprocess runs from a neutral cwd so a project `CLAUDE.md` / `.mcp.json`
  cannot perturb it. (A user-global `~/.claude/CLAUDE.md` and user-level MCP
  servers can still load; keep those lean if you use this backend.)
- **A missing / mistyped `claude` binary fails loudly at startup.** The
  `claude-cli` provider probes for its binary before any work and exits rc 1
  with a clear message if it is absent — it never silently no-ops. (A logged-OUT
  CLI still surfaces per-file at call time.)
- **Rate limits / timeouts degrade gracefully.** A subscription rate-limit, a
  transient CLI error, or a subprocess timeout maps to
  `_retry.TransientAPIError`. Unlike the api backend's SDK transients this is
  NOT retried in-run — it is caught downstream as a give-up and the affected
  file is **deferred to the next run**; the single-machine run lock + resume
  make that pickup safe. (Under sustained rate-limiting the CLI backend defers
  files a nightly run would otherwise complete — acceptable at nightly cadence.)
- **Cost is subscription-covered ($0).** Token COUNTS from the CLI JSON
  envelope are still recorded per model in the run's `TokenUsage` and appear in
  the run summary, but `estimated_cost_usd` reports **$0** — the subscription
  already paid for them.
- **Batch mode is API-only.** `claude-cli` + batch mode
  (`ATHENAEUM_BATCH_MODE` / `librarian.batch_mode` / `--batch-mode`) is a loud
  startup error, not a silent fallback: the Messages Batch API has no CLI
  equivalent. Use `api` for batch runs.
- **Recall hot path stays on `api`.** The per-turn recall query-topic extractor
  (`athenaeum.query_topics`, a ~3 s hot-path budget) is intentionally left on
  the direct Anthropic SDK and gated on `ANTHROPIC_API_KEY`; it is unaffected
  by `ATHENAEUM_LLM_PROVIDER`.

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

### Scoped-claim tree (`scope:`, #329)

The org/locale scope dimensions (issue #329) read a small **versioned tree** of
the values a claim's `scope: {org, locale}` frontmatter may declare. A value NOT
listed here normalizes to *unscoped* (adds no constraint) with a debug
breadcrumb — authors may not mint scope values (the Cyc-microtheory lesson), and
the fail-open direction is toward detection. There is **no default** (no
`_DEFAULTS` seed, #231): a fresh install has empty trees, so scope frontmatter is
inert and single-user behavior is unchanged until the operator opts in.

```yaml
scope:
  org:    [kromatic, kromatic/platform, kromatic/marketing]  # "/"-separated tree
  locale: [en, en-US, de-DE]                                 # "-"-separated tree
```

Nodes form a poset by path-prefix (`kromatic/platform ⊑ kromatic`;
`en-US ⊑ en`). The three-way overlap verdict (DISJOINT / OVERRIDE / OVERLAP) and
the `scope_a` / `scope_b` resolver actions are documented in
`docs/conflict-resolution.md` §12 and `docs/provenance-shape.md` §9. The recall
`serve --scope` caller-context filter is deferred design (#314).

## Recall and search

| Knob | CLI flag | Env var | YAML key | Default | What it does |
|---|---|---|---|---|---|
| Auto-recall | — | `AUTO_RECALL` (hook shell env) | `auto_recall` | `true` | Per-turn recall via the UserPromptSubmit hook. The shell env is read by the example hooks and beats the yaml. |
| Search backend | `--backend` (`recall` / `rebuild-index`) | `SEARCH_BACKEND` (hook shell env) | `search_backend` | `fts5` | `fts5` (SQLite FTS5, BM25 + porter stemming) or `vector` (chromadb + `all-MiniLM-L6-v2`, needs `pip install athenaeum[vector]`). `athenaeum recall --backend keyword` additionally exposes the zero-dependency scan-on-query fallback. |
| Extra intake roots | — | — | `recall.extra_intake_roots` | `["raw/auto-memory"]` | Additional directories (relative to the knowledge root) scanned recursively into the recall index. Set `[]` to restrict recall to the compiled wiki. |
| Recall result count | `--top-k` (`recall`) | — | — | `5` | Hits returned by the shell `recall` command. |
| Index cache dir | `--cache-dir` (`recall` / `rebuild-index`) | — | — | `~/.cache/athenaeum` | Where the FTS5 db / chromadb collection live. |
| Read-scope audience (#312) | `--audience` (`serve` / `recall`) | `ATHENAEUM_AUDIENCE` | `serve.audience` | _(unset = owner, full access)_ | Pins the `serve`/`recall` process to a RESTRICTED read scope: comma-separated (or yaml-list) opaque role/group ids the operator maps onto an external RBAC (AD group, app role, routine name). A restricted caller receives a page only when it is `access: open` OR its `audience:` list grants one of these roles; untagged / `confidential` / `personal` pages are withheld (fail-closed). The audience is pinned by the operator here — it is NOT a `recall()` tool argument, so a restricted agent can't widen its own scope. Empty/unset = owner = every page. |
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
  lock_timeout: 0               # run-lock wait seconds; 0 = fail-fast (#309)
  page_warn_bytes: 8192         # warn on wiki pages over this size (#310)
  page_flag_bytes: 16384        # flag pages over this size for splitting (#310)
  delta:
    enabled: true               # delta-scoped incremental compile on client=None path (#370)
    max_affected_clusters: 8    # > this many clusters touched => full compile (#370)
    max_affected_members: 200   # > this many pooled members => full compile (#370)
  reindex:
    full_rehash_max_age_days: 7 # periodic full re-hash backstop; 0 = always re-hash (#373)

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
