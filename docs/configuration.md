# Configuration Reference

This is the canonical reference for every operator-tunable knob in Athenaeum.
Other docs (README, [auto-resolve.md](auto-resolve.md),
[contradiction-detection.md](contradiction-detection.md)) link here instead of
maintaining their own copies of the tables.

## Precedence

Settings resolve in the order:

> **CLI flag > environment variable > `athenaeum.yaml` > code default**

(established in athenaeum#220 for `--max-api-calls` and generalized in athenaeum#232). Not every
knob has every layer — an em dash (—) in a table cell below means that layer
does not exist for that knob. An env override always beats the yaml, so a
one-off shell export changes a single run without editing config.

`athenaeum.yaml` lives at the knowledge root
(`<knowledge_root>/athenaeum.yaml`, default `~/knowledge/athenaeum.yaml`);
`athenaeum init` writes a commented template covering the most common yaml
keys; the full set of knobs is the tables on this page. Keys you do not set
fall through to the code defaults — the loader
deliberately does not seed defaults for keys whose source of truth lives next
to their consumer code (athenaeum#231), so a future change to a code default takes
effect without a config migration.

Every default figure on this page is verified against the code under
`src/athenaeum/`. When another doc and the code disagree, the code is truth.

## Librarian run (`athenaeum run`)

| Knob | CLI flag | Env var | YAML key | Default | What it does |
|---|---|---|---|---|---|
| Intake batch size | `--max-files` | `ATHENAEUM_MAX_FILES` | `librarian.max_files` | `50` | Stop after processing this many raw files per run (athenaeum#232). Env `0` is valid (defer-everything window); the CLI flag rejects `0`. |
| API call budget | `--max-api-calls` | `ATHENAEUM_MAX_API_CALLS` | `librarian.max_api_calls` | `800` | Run-level cap on estimated API calls (athenaeum#220, raised from 200). A budget-tripped run is DEGRADED: it writes `wiki/_deferred_work.md` and defers remaining intake. Env `0` is valid (defers the entire intake); the CLI flag rejects `0`. |
| Wall-clock deadline | `--max-runtime` | `ATHENAEUM_MAX_RUNTIME` | `librarian.max_runtime` | `3600` | Run-level wall-clock deadline in **seconds** (athenaeum#396). Bounds the WHOLE run — the post-compile phases (C4 detector, athenaeum#290 wiki-dedup, C3 merge/resolver) AND the per-file entity loop — checked at file/cluster/phase boundaries. On trip the run commits partial progress, releases the lock, writes `wiki/_deferred_work.md`, and exits `75` (`EXIT_GRACEFUL_PARTIAL`, resumable — see [exit-codes.md](exit-codes.md) for the full table and how it differs from the external-kill `124`). A value `<= 0` disables the deadline entirely (unbounded run). The default gives an un-wrapped manual run the same ~1h bound the nightly run gets from its external `timeout` wrapper. |
| Entity-phase runtime share | — | `ATHENAEUM_ENTITY_RUNTIME_SHARE` | `librarian.entity_runtime_share` | `0.6` | Fraction of `max_runtime` the **entity phase** may spend claiming new raw files (athenaeum#440). `max_runtime` is a single budget shared by every phase, and the entity loop otherwise stops only when the WHOLE budget is gone — measured on the live corpus, entity took 3690s of a 3944s window (93.6%) on 3 files and the C4 contradiction detector downstream of it got **0 seconds on 10+ consecutive nights**. This reserves the remainder for the auto-memory compile and C4: once the share is spent the entity phase stops taking new files, defers the rest to `wiki/_deferred_work.md` (resumable, like the athenaeum#220 budget trip), and the run **continues** into C2-C4 rather than exiting `75`. Checked at the per-file boundary, so a file already in flight may overrun the share by its own duration — this bounds when the phase stops *taking* work, not when it stops working. Any value outside `0 < share < 1` disables the reserve (pre-athenaeum#440 behaviour); inert when `max_runtime <= 0`. |
| Intake-path runtime floor | — | `ATHENAEUM_INTAKE_RUNTIME_FLOOR` | `librarian.intake_runtime_floor` | `0.0` (OFF) | Fraction of `max_runtime` **reserved as a minimum** for the intake path that feeds C4 (athenaeum#1102) — the guarantee `entity_runtime_share` above does not itself make: that knob only *caps* what the entity phase may take, it never *reserves* anything for what runs after it. athenaeum#608 needs an honest per-contract LLM schema-mismatch rate and could not compute one — the `resolution` contract had 7 observations because the resolver made ~1 call on the 2026-08-06 run, a symptom of the entity phase's wall-clock overrun. **Arming this is an operator decision** (out of scope for athenaeum#1102 itself — it needs a value chosen against measured figures, athenaeum#608's own review) — the default is OFF and, unset, phase scheduling is byte-for-byte identical to before this knob existed. When set, the entity phase yields at whichever of `entity_runtime_share`'s own cap and this floor's implied deadline is EARLIER — so the two combine rather than one silently overriding the other. **Unit is wall-clock** (a fraction of `max_runtime`), deliberately mirroring `entity_runtime_share` rather than an LLM-call count: the window itself is wall-clock and the entity phase already stops independently on the `max_api_calls` ceiling regardless of this floor, so a calls-based floor would duplicate an existing cap — what nothing else guarantees is that entity leaves intake any wall-clock time to spend its own calls in. A floor `>= 1.0` (reserving the WHOLE window or more) is **refused, not clamped** — it falls through to disabled exactly like any other out-of-range value, so a misconfigured floor can never starve the ENTITY phase in the opposite direction. Any value outside `0 < floor < 1` (including a malformed one) falls through to disabled, matching `max_merge_sources`'s "0 disables" convention. |

> **Instrumentation — per-phase reason-for-exit, and a durable ledger (athenaeum#1102, paired with `intake_runtime_floor` above).**
> Every phase segment on the `librarian-run-summary` line (and its durable
> ledger sibling below) now carries a `reason=` token distinguishing a clean
> completion (`reason=completed`) from a share/floor-driven yield
> (`reason=entity-share`, the entity phase only — fired whichever of
> `entity_runtime_share` or `intake_runtime_floor` bound first) or a
> deadline/budget trip (`reason=deadline` / `reason=budget`) — the same
> vocabulary `wiki/_deferred_work.md`'s `reason:` header already uses. This
> answers athenaeum#608's original question ("did the resolver get a
> representative window, or was it starved?") from the record itself, not
> from re-deriving it against a WARNING line.
>
> The `librarian-run-summary` prose line is unconditionally emitted every
> run but, unless an operator's deployment happens to pipe logs somewhere
> durable, is not itself queryable across runs — exactly the gap athenaeum#1102's
> own motivation names ("the ~85% figure exists only as a claim in an issue
> body, because nothing emits it"). `run()` now ALSO appends one JSONL
> record per run to `<cache_dir>/run_summary.jsonl`
> (`athenaeum.run_summary_log.write_run_summary_record`) — the same
> `append_line_durable` + `resolve_cache_dir` convention the athenaeum#378 spend
> ledger uses, under the cache dir rather than `wiki_root` for the same
> reason `zero_yield_state.json` is (see that module's docstring): by the
> time `emit_run_summary` runs, the entity phase's own `git_snapshot` commit
> has already happened, so a `wiki_root` write here would leave an
> uncommitted straggler file every run. Each record is
> `{"v": 1, "ts": "...", "total_secs": ..., "phases": {"<name>": {"secs": ..., ...,
> "reason": "..."}, ...}}` — JSON-native fields, not stringified `key=value`
> tokens, so a later run (athenaeum#608, or any consumer) can aggregate across runs
> without re-parsing prose. Purely additive observability: no phase logic,
> ordering, or exit code is affected, and a ledger-write failure is logged at
> debug level and swallowed, never breaking the run it measures.

See [exit-codes.md](exit-codes.md) for the full `athenaeum run` exit-code table (`0` / `1` / `75` / `124`).
| Stuck-file threshold | — | `ATHENAEUM_STUCK_FILE_THRESHOLD` | `librarian.stuck_file_threshold` | `3` | Consecutive-run failure count after which a raw file is treated as **stuck** (athenaeum#663). `tier3_write` is all-or-nothing per raw file (a partial write cannot leave the wiki half-merged), so one reliably-failing LLM call — e.g. an entity page large enough to time out every night — otherwise discards the file's other successful merges and the file is retried WHOLE every run, forever, silently. A file that fails on the **same content** this many runs running is instead SKIPPED (it stops consuming an LLM call each night) and surfaced as machine-detectable run state: `out_run_stats["stuck_files"]` (ref, consecutive failures, failing `kind:name` action, last error) plus a greppable `librarian-stuck-file` WARNING and a `stuck=N` field on the run-summary line. State lives in `wiki/_stuck_files.json` (keyed by ref + content hash, so editing the file resets its count); a run that finally succeeds on the file clears its entry. Must be `>= 1` (below that would quarantine a file on its first transient failure); non-numeric / non-positive / bool values fall back to the default. |
| Raw-file byte bound | — | `ATHENAEUM_RAW_FILE_MAX_BYTES` | `librarian.raw_file_max_bytes` | `5242880` (5 MiB) | Per-raw-file byte bound (athenaeum#898). Enforced by `RawFile.content` — a raw intake file over this is refused BEFORE it is read into memory or handed to the classifier (`RawFileTooLargeError`, checked via `stat()`, so an oversized file costs one syscall to reject, not a full read). Motivated by a measured 9.7MB dry-run artifact that accounted for 93% of timed entity-phase LLM calls for roughly three months. Counts toward the quarantine threshold below (bound `"bytes"`). `bool` / non-int / `<= 0` values fall through to the default. |
| Raw-file LLM-call bound | — | `ATHENAEUM_RAW_FILE_MAX_API_CALLS` | `librarian.raw_file_max_api_calls` | `60` | Per-raw-file LLM-call bound (athenaeum#898, recalibrated athenaeum#994). Checked INCREMENTALLY by `tier3_derive_actions`, after each entity action a raw file drives, against the running LLM-call count THAT FILE has consumed so far. Measured reality (2026-08-15/16 nightly logs) put an ordinary file at 20-46 calls — un-batched `tier3_write` spends one call per entity action — so the old `8` default (assuming ~1-3 calls) rejected normal input; `60` covers the measured distribution with headroom. An over-bound file's completed actions (everything that finished BEFORE the bound tripped) ARE written — durable partial progress, not discarded — and only the unstarted remainder is dropped; the raw file itself is left on disk (not deleted, not counted as processed) so it can accumulate a consecutive-violation count. Counts toward the quarantine threshold below (bound `"llm_calls"`). `bool` / non-int / `<= 0` values fall through to the default. |
| Raw-file wall-clock bound | — | `ATHENAEUM_RAW_FILE_MAX_RUNTIME_SECONDS` | `librarian.raw_file_max_runtime_seconds` | `900` | Per-raw-file wall-clock bound in **seconds** (athenaeum#898, recalibrated athenaeum#994). Checked alongside the LLM-call bound above, incrementally, after each entity action — the wall-clock spent inside one file's processing so far. Measured reality put an ordinary file at 300-690s; the old `120` default rejected normal input, `900` covers the measured distribution with headroom. Same partial-progress-on-trip behavior as the LLM-call bound. Counts toward the quarantine threshold below (bound `"wall_clock"`). `bool` / non-int / `<= 0` values fall through to the default. |
| Quarantine threshold | — | `ATHENAEUM_QUARANTINE_THRESHOLD` | `librarian.quarantine_threshold` | `2` | Consecutive-run count after which a raw file that keeps exceeding ANY of the three bounds above is **quarantined** (athenaeum#898) — physically moved from `raw/<source>/` to `wiki/_quarantine/<source>/`, so it drops out of `discover_raw_files`'s discovery set, plus an audit-ledger record (`wiki/_quarantine.jsonl`) and a `type: "quarantine"` entry in `athenaeum decisions` / `list_pending_decisions`. Mirrors the stuck-file ledger's shape (`wiki/_quarantine_candidates.json`, keyed by ref + content hash, so editing the file resets its count) but is tracked as a SEPARATE ledger — a bound violation is a measured resource fact, not a processing exception, and its disposition (physical removal) is heavier than the stuck-file skip-in-place. Reversible only via an operator decision (`athenaeum.quarantine.release_quarantine`) — there is no automatic un-quarantine. Must be `>= 1`; non-numeric / non-positive / bool values fall back to the default. |
| Junk-match stopwords | — | — | `librarian.junk_match_stopwords` | *(extends the built-in default)* | Extra entity names to treat as **junk** so a Tier-1 match on them never issues a Tier-3 merge LLM call (athenaeum#662). Tier-1 matches any indexed page name ≥ 3 chars, and the index accumulates junk pages (`here`, `get`, `main`, `reach`, `lane a`, …) — each became a ~16-23KB merge call, roughly **half** of the ~15-18 Tier-3 calls per file. A conservative built-in default (the measured junk plus common English function words) is always applied; entries here are **added** to it, case-insensitively on the whole name. Tune per corpus as the junk set changes. |
| Junk-match allowlist | — | — | `librarian.junk_match_allowlist` | `[]` | Entity names that must **never** be treated as junk (athenaeum#662) — the escape hatch for a real entity whose name collides with a default/stopword junk word (e.g. a company literally named "Reach"). Wins over both the built-in default and `junk_match_stopwords`, case-insensitively. |
| Exclude code artifacts | — | — | `librarian.exclude_code_artifacts` | `true` | Whether a candidate whose name is a **filename or path** (`skill.md`, `project-registry.yaml`, `src/athenaeum/librarian.py`) is refused entity creation (athenaeum#680). Code should not be remembered as memory: the repo is the source of truth for its own code, so a wiki page describing a file's *past* state is stale by construction and costs a session to disprove. A **write-side class** exclusion applied at creation — complementary to, and distinct from, the read-side `junk_match_*` stopword gate (athenaeum#662). Set `false` to disable the gate entirely. |
| Code-artifact extensions | — | — | `librarian.code_artifact_extensions` | *(extends the built-in default)* | Extra source/config file extensions that mark a name as a code artifact (athenaeum#680). A conservative built-in default (`.md`, `.py`, `.yaml`, `.ts`, `.json`, `.sh`, …) is always applied; entries here are **added** to it (leading dot optional). A name is file-shaped when it contains a path separator, or is a single whitespace-free token ending in one of these extensions. |
| Code-artifact allowlist | — | — | `librarian.code_artifact_allowlist` | `[]` | Entity names that must **never** be treated as code artifacts (athenaeum#680) — the escape hatch for a deployment that legitimately tracks a document by filename. Wins over the file-shape check, case-insensitively (mirrors `junk_match_allowlist`). |
| Non-intake sources | — | — | `librarian.non_intake_sources` | `[]` | `raw/<source>/` directories excluded from entity intake WHOLE (athenaeum#843). Any tool that writes its own **operational artifacts** into `raw/<source>/` — the same tree `remember()`-authored content uses — otherwise becomes nightly LLM-classification load: action logs and state dumps match the `*.md` / `*.jsonl` glob, are not correction-batch envelopes, and so reach `tier2_classify` → `tier3_write` as if they were memory content (a multi-megabyte log gets read whole and handed to the classifier). Names are matched against the directory name exactly — no globbing, no case folding. Skipped before any glob work, the same way the built-in `answers` skip (athenaeum#414) works; this is a second, operator-controlled mechanism alongside it, not a replacement. Default-empty, so an unconfigured install discovers exactly what it did before. |
| Strict budget exit | `--strict-budget` | — | — | off | Make a budget-tripped (DEGRADED) run exit nonzero instead of `0`, for exit-code-based alerting (athenaeum#227). |
| Batch API mode | `--batch-mode` / `--no-batch-mode` | `ATHENAEUM_BATCH_MODE` | `librarian.batch_mode` | off | Submit tier-2/tier-3 LLM calls via the [Anthropic Messages Batch API](https://platform.claude.com/docs/en/build-with-claude/batch-processing) at a 50% token discount (athenaeum#236). Latency-tolerant: most batches finish within an hour, 24h worst case — intended for the nightly run. Same-page tier-3 merges stay synchronous; the budget cap is enforced at batch-assembly time (re-checked per file at phase-2 assembly and before the synchronous merges). `--no-batch-mode` forces the synchronous path even when env/yaml turn batch mode on. |
| Cluster threshold | — | — | `librarian.cluster_threshold` | `0.55` | Cosine cutoff for auto-memory near-duplicate clustering (C2, athenaeum#196). Higher = tighter clusters. |
| Cluster output | — | — | `librarian.cluster_output` | `raw/_librarian-clusters.jsonl` | Canonical cluster JSONL path, resolved relative to the knowledge root. Each run also writes a timestamped sibling. |
| Rotation retention | — | `ATHENAEUM_ROTATION_RETENTION` | `librarian.rotation_retention` | `30` | Number of timestamped cluster-report rotations to keep; older ones are pruned after each run (athenaeum#311). Rotations are debugging artifacts, not recovery-critical (recovery is git-based). `0` (or negative) disables pruning (keep all). A prune failure is a non-fatal warning. |
| Ephemeral scopes | — | — | `librarian.ephemeral_scopes` | `[]` | Glob patterns (matched against the auto-memory scope) whose raw intake is classified ephemeral and dropped before clustering (athenaeum#280), so operational/throwaway scopes never materialize a durable `wiki/auto-*.md` page. Default-empty (off). |
| Operational markers | — | — | `librarian.operational_markers` | `[]` | Lower-cased content substrings that, when `>= 2` are present in a raw auto-memory file, classify it as ephemeral operational boilerplate (athenaeum#280). Conservative multi-signal gate; default-empty so nothing fires until an operator opts in. Lower-precedence than an explicit `ephemeral: true` frontmatter flag or an `ephemeral_scopes` match. |
| Cluster-cohesion floor | — | — | `librarian.min_cluster_cohesion` | `0.0` | Cohesion floor that suppresses low-cohesion cross-scope over-clusters (athenaeum#281). A cluster is withheld only when its `cluster_centroid_score` is strictly below this value **AND** it spans `>= min_cluster_cohesion_scopes` distinct origin scopes. Default `0.0` = OFF (the cutoff is corpus-specific); `0.47` is recommended for the reference corpus. Suppressed clusters leave their raw members in place (not retired). |
| Cohesion-floor scope count | — | — | `librarian.min_cluster_cohesion_scopes` | `4` | Minimum distinct `origin_scope` count a low-cohesion cluster must span before the `min_cluster_cohesion` floor suppresses it (athenaeum#281). Legitimate pages span 1-3 scopes and over-clusters span 8-17, so `4` is the clean margin — a low-cohesion single-/few-scope cluster is never false-suppressed. Inert while `min_cluster_cohesion` is `0.0`. |
| Merge-proposal source cap | — | `ATHENAEUM_MAX_MERGE_SOURCES` | `librarian.max_merge_sources` | `5` | Source-count cap on resolver merge proposals (athenaeum#400). A `propose_merge` folding more than N sources is a degenerate over-cluster (the incident: 1,600+ sources at ~0.33 confidence, re-proposed every run), not a pairwise/small-group refinement — it is dropped before it reaches `wiki/_pending_merges.md` (neither proposed nor escalated as a pending question). Default `5` (active) — tightened from `25` (athenaeum#421, settled design); set `0` to disable. The cohesion floor above already withholds low-cohesion over-clusters upstream — this catches the *high*-cohesion degenerates it doesn't. |
| Merge-proposal confidence floor | — | `ATHENAEUM_MIN_MERGE_CONFIDENCE` | `librarian.min_merge_confidence` | `0.0` | Optional confidence floor on resolver merge proposals (athenaeum#400): a proposal below this confidence is dropped before `wiki/_pending_merges.md`. Default `0.0` = OFF (the review bar is corpus-specific), opt-in via yaml. Complements `max_merge_sources` — the cap catches over-clusters by shape, this keeps low-confidence small merges out of the queue. A parsed env value is authoritative — `0` disables the floor even when yaml sets one (athenaeum#524); a malformed value logs a WARNING and falls back (athenaeum#528). |
| Merge-proposal cohesion floor | — | `ATHENAEUM_MIN_MERGE_MEAN_SIMILARITY` | `librarian.min_merge_mean_similarity` | `0.6` | Mean-pairwise-cosine floor on resolver merge proposals (athenaeum#421): a proposal whose cluster mean pairwise cosine is strictly below this is suppressed before `wiki/_pending_merges.md`. Unlike the corpus-specific confidence floor above, this cohesion gate is **active by default**. `0` (or negative) disables it. A malformed env value logs a WARNING and falls back (athenaeum#528). |
| Page warn size | — | `ATHENAEUM_PAGE_WARN_BYTES` | `librarian.page_warn_bytes` | `8192` | Soft byte threshold above which a wiki entity page is reported as a **warn**-level oversized page in `athenaeum status` (athenaeum#310). Warn-only: nothing is blocked or modified. A long page usually means poorly-factored knowledge that should be split into linked sub-entities. `bool` / non-int / `<= 0` values fall through to the default. |
| Page flag size | — | `ATHENAEUM_PAGE_FLAG_BYTES` | `librarian.page_flag_bytes` | `16384` | Byte threshold above which a page is **flagged** for splitting — surfaced in `status` and logged as a non-fatal `WARNING` during `athenaeum run` (athenaeum#310). Still warn-only (never blocks; the tier-3 merge body cap is separate and unchanged). A flagged page appears only in the flag bucket, not also in warn. Keep comfortably below the merge body cap. |
| Backlog-drain warn threshold (days) | — | — | `librarian.drain_warn_days` | `3` | Backlog-drain ETA threshold in **days** (athenaeum#470). At the end of any run that leaves raw intake undrained (and in `athenaeum status`), the advisor projects time-to-drain from **observed** throughput (the athenaeum#378 spend ledger; falls back to this run's own rate) and emits a machine-greppable `backlog-drain-advisor:` `WARNING` — naming the copy-pastable `athenaeum drain` remedy — only when the projection **exceeds** this many days; below it the run stays silent. `bool` / non-int / `<= 0` values fall through to the default. |
| Merge-read preview length | — | `ATHENAEUM_MERGE_BODY_PREVIEW_CHARS` | `librarian.merge_body_preview_chars` | `2000` | Read-path bound (athenaeum#431) on the `list_pending_merges` MCP tool: `draft_merged_body` is truncated to this many characters by default (a single oversized pending merge — the withdrawn runaway that prompted this issue had a ~878 KB draft body — otherwise blows out the payload on every call). Each item also carries `draft_merged_body_truncated` and `draft_merged_body_full_length`; pass `full_body=True` to the tool to get the untruncated body on demand. Complements the write-path `max_merge_sources` cap above (athenaeum#400), which suppresses the proposal entirely rather than bounding its rendering. `bool` / non-int / `<= 0` values fall through to the default. |
| Decisions-view source cap | — | `ATHENAEUM_DECISIONS_MAX_SOURCES_PER_MERGE` | `librarian.decisions_max_sources_per_merge` | `20` | Read-path bound (athenaeum#431) on the `decisions` view / `list_pending_decisions` MCP tool: a merge item renders at most this many sources, with the exact remainder in `payload["sources_omitted"]` (and an "… and N more" line in the plain-text CLI rendering). `bool` / non-int / `<= 0` values fall through to the default. |
| T2-approval audit rate | — | `ATHENAEUM_AUDIT_SAMPLE_RATE_T2_APPROVALS` | `librarian.audit_sample_rate_t2_approvals` | `0.075` | Share of T2 (opus) approvals randomly sampled into the human decisions queue as `type: "audit"` calibration items (athenaeum#438) — the false-approve half of the tier calibration loop. Deterministic per `(tier, proposal)`. Clamped to `[0.0, 1.0]` (`0.0` = OFF, `1.0` = audit everything); default `0.075` is the midpoint of the settled 5-10% band. `bool` / non-numeric values fall through to the default. |
| T1-reject audit rate | — | `ATHENAEUM_AUDIT_SAMPLE_RATE_T1_REJECTS` | `librarian.audit_sample_rate_t1_rejects` | `0.075` | Share of T1 (haiku/sonnet) rejects randomly sampled into the human decisions queue as `type: "audit"` calibration items (athenaeum#438) — the false-reject half of the tier calibration loop. Deterministic per `(tier, proposal)`. Clamped to `[0.0, 1.0]` (`0.0` = OFF, `1.0` = audit everything); default `0.075` is the midpoint of the settled 5-10% band. `bool` / non-numeric values fall through to the default. |
| Embedding cache root | — | `ATHENAEUM_CACHE_DIR` | — | `~/.cache/athenaeum` | Cache root used by the librarian's cluster pass (chromadb lives at `<dir>/wiki-vectors/`). The `recall` / `rebuild-index` commands do **not** read this var — they take `--cache-dir` (same default). |
| Post-run git push | `--push` | — | `librarian.push_after_run` | off | Push the knowledge repo to its remote after a successful run that produced at least one commit (athenaeum#284). Closes the move-then-retire recovery gap on multi-machine setups: without it, scheduled nightly runs commit locally but origin silently drifts. Uses the operator's ambient git auth (credential helper / SSH); athenaeum handles no tokens or secrets. `--dry-run` never pushes; a run with no new commits never pushes; a push failure is a non-fatal warning (`athenaeum-push-failed:`) and the next run retries. Remote/branch come from `librarian.push_remote` (default `origin`) and `librarian.push_branch` (default: current branch's upstream). |
| Run-lock wait | `--wait` | `ATHENAEUM_LOCK_TIMEOUT` | `librarian.lock_timeout` | `0` | Default seconds a mutating command blocks for the single-machine run lock before failing (athenaeum#309). `0` = fail-fast (name the holder, exit non-zero). The `--wait` flag overrides per-invocation. See the run-lock note below. |
| Run-lock auto-break age | — | `ATHENAEUM_LOCK_BREAK_STALE_AFTER` | `librarian.lock_break_stale_after` | `21600` (6h) | Seconds of holder-heartbeat age after which a contended acquire auto-breaks a wedged-but-alive holder's lock, without a human passing `--force` (athenaeum#397). Comfortably above any healthy run; lower it once the librarian reliably refreshes the heartbeat. |
| Run-lock stale warning age | — | `ATHENAEUM_LOCK_WARN_STALE_AFTER` | `librarian.lock_warn_stale_after` | `7200` (2h) | Seconds of holder-heartbeat age after which a contended acquire logs a prominent "likely wedged" warning naming the holder (athenaeum#397) — typically lower than the auto-break age, so an operator gets an early heads-up before auto-break fires. |
| Progress-heartbeat interval | — | `ATHENAEUM_HEARTBEAT_INTERVAL` | `librarian.heartbeat_interval` | `60` | Seconds between `librarian-heartbeat` progress ticks emitted by the dark-zone phases (T3 merge, C4 detection, athenaeum#290 wiki-dedup, athenaeum#188 re-resolve) so a stall is visible in the log and to a watchdog (athenaeum#398). `<= 0` emits every tick. |
| Delta-scoped compile | — | — | `librarian.delta.enabled` | `true` | Enable delta-scoped incremental compile on the deterministic (`client=None`) path — `session-end` / `ingest` tier0 (athenaeum#370). When on, re-cluster and re-merge only the clusters a change actually touches instead of the whole auto-memory corpus; byte-equivalent to the whole-corpus path. Set `false` to always compile whole-corpus. The nightly LLM `run` always stays whole-corpus regardless of this flag. `bool` yaml values are honored; anything else falls through to the `true` default. |
| Delta affected-cluster cap | — | — | `librarian.delta.max_affected_clusters` | `8` | If a change would touch more than this many clusters, fall back to a full whole-corpus compile rather than churning most of the corpus through the delta path (athenaeum#370). `bool` / non-positive / non-int values fall through to the default. |
| Delta affected-member cap | — | — | `librarian.delta.max_affected_members` | `200` | If the affected-cluster member pool exceeds this many files, fall back to a full compile (athenaeum#370). Bounds worst-case re-cluster cost so a pathological closure never does more work than a full run. `bool` / non-positive / non-int values fall through to the default. |
| Full-rehash backstop age (days) | — | — | `librarian.reindex.full_rehash_max_age_days` | `7` | Self-healing periodic full re-hash backstop (athenaeum#373). The athenaeum#370 stat pre-filter reuses a stored content hash whenever a file's `(mtime, size)` match the index manifest; when the manifest has not had a full re-hash within this window, the next incremental reindex re-hashes **every** file (catching a content edit that preserved both mtime and size) while still applying only the delta — seconds, not a full re-embed / FTS5 rebuild. `0` or negative = always re-hash; a very large value = effectively never. `bool` / non-numeric values fall through to the default. |
| Reasoning-trigger backlog (files) | `athenaeum ingest --if-triggered` | — | `librarian.reasoning_triggers.backlog_files` | *(unset = OFF)* | Backlog-depth reasoning trigger, by pending raw-intake **file count** (athenaeum#909). When `athenaeum ingest --if-triggered` sees `discover_raw_files` reach or exceed this many files, it runs the normal incremental ingest; otherwise it is a cheap no-op (prints a summary with `"trigger": "none"`, exits 0, never takes the run lock). Unset (the default) disables this trigger entirely — see [reasoning-tier triggers](#reasoning-tier-triggers-athenaeum909) below. `bool` / non-positive / non-int values fall through to disabled. |
| Reasoning-trigger backlog (bytes) | `athenaeum ingest --if-triggered` | — | `librarian.reasoning_triggers.backlog_bytes` | *(unset = OFF)* | Backlog-depth reasoning trigger, by pending raw-intake **byte size** (athenaeum#909) — literal on-disk bytes (`sum(stat().st_size)`), not a cost/token estimate. Fires alongside (not instead of) the file-count trigger above; either reaching its threshold fires. Unset disables. `bool` / non-positive / non-int values fall through to disabled. |
| Reasoning-trigger interval (hours) | `athenaeum ingest --if-triggered` | — | `librarian.reasoning_triggers.interval_hours` | *(unset = OFF)* | Elapsed-interval reasoning trigger (athenaeum#909): fires once at least this many hours have passed since the last completed triggered run, regardless of backlog depth — a quiet night still gets a bounded, incremental look. Unset disables (only the nightly backstop below still applies). `bool` / non-positive / non-int values fall through to disabled. |
| Reasoning-trigger nightly backstop (hours) | `athenaeum ingest --if-triggered` | — | `librarian.reasoning_triggers.nightly_backstop_hours` | `24` | Nightly-backstop reasoning trigger (athenaeum#909) — always on, unlike the three above. Fires once at least this many hours have elapsed since the last completed triggered run **and no other trigger fired this evaluation**; this is what keeps the old "once a night" schedule alive as a demoted fallback rather than the primary path. `bool` / non-positive / non-int values fall through to the default. |
| API key | — | `ANTHROPIC_API_KEY` | — | (required) | Required for Tier 2/3 LLM calls. Optional with `--dry-run`, `--cluster-only`, or `--merge-only`. |

> **Design decision — CLI rejects `0`, env/yaml accept it.** The
> `--max-api-calls` and `--max-files` flags reject `0` at parse time as a
> typo guard at the interactive surface, while `ATHENAEUM_MAX_API_CALLS=0` /
> `librarian.max_api_calls: 0` (and the `max_files` equivalents) are accepted
> as deliberate defer-everything caps for scripted deployments. This
> asymmetry is intentional, not an oversight (decided 2026-06-12; refs athenaeum#235
> and the athenaeum#240 review). A run whose budget resolves to `0` logs a prominent
> warning at start so an accidental zero is diagnosable immediately.

> **Backstop guarantee (athenaeum#373).** The athenaeum#370 stat pre-filter reuses a stored
> content hash when a file's `mtime` and `size` both match the manifest, so a
> content edit that preserves BOTH would otherwise slip past an incremental
> reindex indefinitely. `librarian.reindex.full_rehash_max_age_days` bounds that
> worst case: such an edit is guaranteed to surface within
> `full_rehash_max_age_days` (default ≤ 7 days), even if nothing else triggers a
> re-hash in the meantime.

> **Monitoring contract — entity-phase share yield (athenaeum#669, paired with `entity_runtime_share` above).**
> When the entity phase yields its window share (athenaeum#440) it stops claiming new raw
> files and defers the rest — a *deliberate, correct* stop, but one that ends the
> run well **under** the duration-based cap heuristic (`LIBRARIAN_CAP_DEADLINE`,
> cron-fleet#94), so a athenaeum#440-shaped stall is no longer detectable by duration
> alone. The yield is therefore emitted as **machine-detectable run state** so a
> consumer can tell "entity yielded on purpose" from "API budget exhausted"
> without parsing WARNING text or the `wiki/_deferred_work.md` header. The
> `run()` `out_run_stats` dict (and the run-summary line) carry:
> - `entity_budget_tripped` — `true` when the entity phase yielded on its share
>   this run, `false`/absent otherwise. On the run-summary line the `entity`
>   segment gains `entity_budget_tripped=true` only when it fired (a clean run's
>   line is unchanged), alongside the existing `degraded` / `truncated` / `stuck`
>   flags.
> - `entity_files_claimed` — files the entity phase compiled this run.
> - `entity_files_deferred` — in-window files the yield deferred to the next run.
>   A boolean alone can detect the yield but not judge whether the backlog is
>   growing; the claimed/deferred pair makes that judgeable (combine with the
>   `beyond_window` count, also in `out_run_stats`, for the full backlog).
>
> This is purely additive observability — the yield **behavior** from athenaeum#440 is
> unchanged. The cross-repo consumer (cron-fleet gating on the new field) is a
> separate `Kromatic-Innovation/cron-fleet` change, out of scope here.

Path and mode flags on `athenaeum run` (CLI-only): `--raw-root` and
`--wiki-root` (default under the knowledge root), `--knowledge-root` /
`--path` (default `~/knowledge`), `--dry-run`, `--cluster-only`,
`--merge-only`, `--verbose`.

### Field corrections (`librarian.corrections.*`, athenaeum#797)

The deterministic, LLM-free field-correction fast path documented in
[`field-corrections.md`](field-corrections.md). Runs as its own phase inside
`athenaeum run`, before the entity tiers, and makes zero LLM calls.

| Knob | Env var | YAML key | Default | What it does |
|---|---|---|---|---|
| Attribute allowlist | — | `librarian.corrections.fields` | `{}` | §6.3: maps an attribute name to `{shape: "scalar"\|"list", writers: [...], monotone: bool}`. **Empty by default** — with no entry, every submission takes the reasoning-tier fallthrough (§8) and nothing is written cheaply; a fresh deployment cannot have its wiki written by a mechanical writer until an operator opts a specific attribute in. `writers` bounds blast radius, not trust (§12a) — see the security note there. |
| Sensitivity routing | — | `librarian.corrections.sensitive_fields` | `{}` | §7.1: maps an attribute name to a `storage.mapping` entity-class name (athenaeum#429's existing storage-adapter layer, reused rather than reinvented). A fact bearing on a mapped attribute is routed to that surface REGARDLESS of the destination the correction named — a correction cannot opt out of routing by being specific. Empty by default: sensitivity classification is deployment configuration, never shipped in this repo. |
| Schema-slot routing | — | `librarian.corrections.schema_slots` | `{}` | §7.2: for an attribute that IS on the `fields` allowlist but has no dedicated schema slot, maps it to `{alias_of: "<field>"}` (route to an equivalent slot), `{propose_amendment: true}` (hold a schema-amendment proposal on `_pending_questions.md`), or `{prose: true}` (record as body prose, one-off). An allowlisted attribute with no entry here writes directly as ordinary frontmatter (schemas.py's per-type models already tolerate unknown keys via `extra="allow"`, the same mechanism source-handle keys use). |
| Records per batch | `ATHENAEUM_CORRECTIONS_MAX_RECORDS_PER_BATCH` | `librarian.corrections.max_records_per_batch` | `5000` | §10.2: a batch carrying more records than this is deferred WHOLE to the next run (never refused) and reported as carry-over. |
| Records per run | `ATHENAEUM_CORRECTIONS_MAX_RECORDS_PER_RUN` | `librarian.corrections.max_records_per_run` | `50000` | §10.2: run-level cap on records actually applied/routed-elsewhere. Batches beyond the cap are untouched and retried next run. |
| Batch file size | `ATHENAEUM_CORRECTIONS_MAX_BATCH_BYTES` | `librarian.corrections.max_batch_bytes` | `33554432` (32 MiB) | §10.2: a batch file over this size is deferred whole, same treatment as the per-batch record cap. |
| Escalations per run | `ATHENAEUM_CORRECTIONS_MAX_ESCALATIONS_PER_RUN` | `librarian.corrections.max_escalations_per_run` | `50` | §10.2: flood guard on how many NEW `_pending_questions.md` entries this phase files in one run (a writer with a systematic disagreement could otherwise fill the human queue). Escalations already open (deduped by `correction_id`) do not count against the cap. On hitting the cap the phase keeps applying/deferring normally and emits one summary line naming the submitter + attribute with the highest suppressed count. |
| Phase runtime share | `ATHENAEUM_CORRECTIONS_RUNTIME_SHARE` | `librarian.corrections.runtime_share` | `0.05` | Fraction of `librarian.max_runtime` this phase may spend, mirroring `entity_runtime_share`'s mechanism. Checked at BATCH boundaries only (never mid-batch). Records that raise a tier (§8) join the ordinary intake queue and are subject to the entity phase's budget instead — they cost what reasoning costs, not what this phase's share allows. |

### Shape-rule engine (`librarian.shape_rules.*`, athenaeum#901)

Declarative YAML rules (`<knowledge_root>/rules/*.yaml`, see
[`shape-rules.md`](shape-rules.md)) that recognise a foreign record shape and
compile it into a field-correction batch — consumed unchanged by the field
corrections machinery above. Runs as its own phase inside `athenaeum run`,
immediately before the field-correction phase (so a compiled batch is visible
to that phase's scan in the same run), and makes zero LLM calls. There is no
`fields`/`sensitive_fields`/`schema_slots`-style dict knob here — every rule
is its own self-contained YAML file, not a config-table entry.

| Knob | Env var | YAML key | Default | What it does |
|---|---|---|---|---|
| Records per run | `ATHENAEUM_SHAPE_RULES_MAX_RECORDS_PER_RUN` | `librarian.shape_rules.max_records_per_run` | `50000` | Run-level cap on candidate raw files the engine evaluates against rules, mirroring `librarian.corrections.max_records_per_run`. Files beyond the cap are untouched and retried next run. |
| Phase runtime share | `ATHENAEUM_SHAPE_RULES_RUNTIME_SHARE` | `librarian.shape_rules.runtime_share` | `0.05` | Fraction of `librarian.max_runtime` this phase may spend, mirroring `librarian.corrections.runtime_share`'s mechanism exactly (own budget — an overrun in one deterministic phase never starves the other). Checked at FILE boundaries only (never mid-file). |

### Rule proposals (`librarian.rule_proposals.*`, athenaeum#905 / athenaeum#1063)

The librarian's rule-proposal detector/drafter (`athenaeum.rule_proposals`,
see [`shape-rules.md` §10](shape-rules.md)) — counts records the shape-rule
engine above deferred to the reasoning tiers, drafts a candidate rule from
exemplars once a shape crosses threshold, and puts it on the
`list_pending_decisions` surface for a human to approve or reject. Wired
into the nightly `athenaeum run` loop (issue athenaeum#1063,
`librarian._run_rule_proposal_phase`) — **OFF by default.** Set
`librarian.rule_proposals.enabled: true` (or the env var below) to turn it
on; left off, the phase never builds a client, never reads the disposition
ledger, and makes zero LLM calls.

| Knob | Env var | YAML key | Default | What it does |
|---|---|---|---|---|
| Enabled | `ATHENAEUM_RULE_PROPOSALS_ENABLED` | `librarian.rule_proposals.enabled` | `false` | **Master gate.** With this off, `_run_rule_proposal_phase` returns immediately every run — no new spend, no behavior change. This wiring adds a NEW unattended model-drafting call to the nightly run, so it defaults off until an operator opts in. |
| Threshold | `ATHENAEUM_RULE_PROPOSALS_THRESHOLD` | `librarian.rule_proposals.threshold` | `50` | Deferred-record count (per `(source, key_fingerprint)` shape, within the window below) that must be crossed before a shape is drafted. Also doubles as the phase's cadence control — see the note below. |
| Window | `ATHENAEUM_RULE_PROPOSALS_WINDOW_DAYS` | `librarian.rule_proposals.window_days` | `30` | Only disposition rows whose `at` timestamp falls within this many days of "now" count toward the threshold. |
| Exemplar count (K) | `ATHENAEUM_RULE_PROPOSALS_EXEMPLAR_COUNT` | `librarian.rule_proposals.exemplar_count` | `5` | How many readable raw records of a detected shape are embedded in the one drafting call. |

Cadence note: once enabled, there is no separate once-per-period stamp —
the `threshold` above IS the cadence control, together with the detector's
own per-shape idempotency (a shape already carrying a pending or rejected
proposal is never re-drafted). `run_shape_rule_phase` has no once-per-period
guard beyond its own runtime share either, so there is nothing further to
mirror.

The drafting call's model/`max_tokens`/`thinking` route through the standard
`resolve_model`/`resolve_max_tokens`/`resolve_thinking` knobs under knob name
`rule_proposals` — same precedence as every other stage above (env > yaml >
code default):

| Knob | Env var | YAML key | Default | What it does |
|---|---|---|---|---|
| Model | `ATHENAEUM_RULE_PROPOSALS_MODEL` | `models.rule_proposals` | `claude-opus-4-8` | Drafting-call model — T2's tier: drafting a rule is a judgment call, not cheap classification. |
| Max tokens | `ATHENAEUM_RULE_PROPOSALS_MAX_TOKENS` | `max_tokens.rule_proposals` | `4096` | Output-token budget for the drafting call. |
| Thinking | `ATHENAEUM_RULE_PROPOSALS_THINKING` | `thinking.rule_proposals` | `adaptive` | Thinking posture for the drafting call. |

### Run lock (single-machine concurrency guard, athenaeum#309)

Every **mutating** command acquires an exclusive advisory
[`fcntl.flock`](https://man7.org/linux/man-pages/man2/flock.2.html) on
`<knowledge_root>/.athenaeum.lock` at startup, so overlapping runs (a nightly
cron overlapping a manual invocation, or two editor sessions) cannot race
whole-file wiki writes, interleave block appends to the `_pending_*.md`
sidecars, double-spend the API-call budget, or race the move-then-retire git
ops. The lockfile records the holder's PID, an ISO-8601 timestamp, and the
hostname for diagnostics.

- **Locked commands:** `run`, `ingest`, `ingest-answers`, `ingest-merges`,
  `reresolve-questions`, `rebuild-index`, `session-end`, `drain`,
  `auto-memory prune --apply`, `auto-memory prune-index`,
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

## SessionEnd budget derivation (`athenaeum session-end`, athenaeum#896)

`cmd_session_end` never falls through to the `athenaeum run` defaults above
for its own `max_runtime`/`max_files`/`max_api_calls` — it resolves an
INNER deadline derived from the SessionEnd wrapper's OUTER kill timeout, and
passes fixed, session-scoped-incremental-sized budget caps. Before athenaeum#896 the
inner deadline fell through to `DEFAULT_MAX_RUNTIME` (3600s) — 4x the
wrapper's 900s outer default — so a budget-tripped run was always externally
`SIGTERM`'d instead of exiting through the graceful-stop path.

| Knob | CLI flag | Env var | YAML key | Default | What it does |
|---|---|---|---|---|---|
| Outer kill timeout | — | `KNOWLEDGE_REBUILD_TIMEOUT` | — | `900` | The SessionEnd wrapper's own external `timeout --signal=TERM` value — read from `code-workspace-config/scripts/hooks/knowledge-rebuild-index.sh` (a **different repo**, not this one). `session_end_outer_timeout()` reads the SAME env var with the SAME default so it is the single definition both the wrapper and the derivation share, rather than a constant duplicated in two repos that can drift apart. No YAML key: the wrapper that owns this value lives outside athenaeum's config. |
| Inner-runtime margin | — | `ATHENAEUM_SESSION_END_RUNTIME_MARGIN` | `librarian.session_end_runtime_margin` | `120` | Slack (seconds) subtracted from the outer timeout to get the inner `max_runtime` — time reserved for the graceful-stop commit itself plus CLI startup/lock-acquire overhead. `session_end_max_runtime()` derives `inner = outer - margin`, clamped so the result is always strictly positive and strictly less than a configured outer of `2` or more (a floor of half the outer, minimum 1s, prevents a non-positive result when the margin exceeds the outer). An outer `< 2` (including `<= 0`, meaning the wrapper's own `timeout` is disabled) has no external race to protect and falls back to the `athenaeum run` `DEFAULT_MAX_RUNTIME` (3600s) instead. |
| Per-run file cap | — | — | — | `20` (`SESSION_END_MAX_FILES`) | Fixed `max_files` `cmd_session_end` passes explicitly, well under the nightly `DEFAULT_MAX_FILES` (50) — SessionEnd is scoped to one session's incremental raw intake, not a whole night's backlog. |
| Per-run API call cap | — | — | — | `100` (`SESSION_END_MAX_API_CALLS`) | Fixed `max_api_calls` `cmd_session_end` passes explicitly, well under the nightly `DEFAULT_MAX_API_CALLS` (800), for the same session-scoped-incremental reason. |

## Backlog drain (`athenaeum drain`, athenaeum#470)

When the raw-intake backlog outgrows the nightly caps, `athenaeum run`'s
DEGRADED summary reports **counts**, not time-to-drain, and the supervised
API+batch remedy used to live as tribal knowledge spread across env vars and
flags. Two capabilities close that gap.

**ETA advisor.** At the end of any run that leaves raw intake undrained (and in
`athenaeum status`), an advisor projects nights-to-drain from the **observed**
throughput recorded in the athenaeum#378 spend ledger (files consumed per run — now
persisted as `files_processed`) — never a hardcoded guess; it falls back to this
run's own rate when there is no history. When the projection exceeds
`librarian.drain_warn_days` (default 3) it emits one machine-greppable
`WARNING` naming the copy-pastable drain command, e.g.:

```
backlog-drain-advisor: 202 deferred file(s) ≈ 18 night(s) to drain at current caps/provider (ledger rate) — consider: athenaeum drain --max-usd 20 --yes
```

Below the threshold it stays silent. The estimate promises **cost plus "hours,
not nights"**, never a wall-clock guarantee (same-page merges serialize on the
batch path, the deliberate athenaeum#236 grouping).

**`athenaeum drain` — one-command supervised drain.** A thin orchestration over
the existing `run()` machinery (run-lock, git snapshots, deferred manifest, and
per-phase run summary all reused unchanged — drain is just a caller):

```
athenaeum drain --max-usd 50 --yes
```

| Flag | Required | Default | What it does |
|---|---|---|---|
| `--max-usd N` | **yes** | — | Mandatory cost ceiling in USD applied **cumulatively across the whole drain** (not per window). Maps onto the athenaeum#378 `spend.max_usd_per_run` ceiling for each window as the remaining budget. |
| `--max-files N` | no | `librarian.max_files` / 50 | Intake window size — files compiled per window. The drain loops windows until the backlog empties or the cost ceiling trips. |
| `--yes` | no | off | Proceed without the interactive cost confirmation (**required** to run non-interactively — the drain incurs real API spend). |
| `--path` / `--knowledge-root` | no | `~/knowledge` | Knowledge directory (`--raw-root` / `--wiki-root` override the sub-roots). |

Behavior and guards:

- **Forces the API + Batch path** (`provider=api` + batch mode, the athenaeum#236 path at a 50% token discount) and an **unbounded run** (`max_runtime=0`). Batch mode block-polls the Batch API; a finite deadline is the known cwc#615 failure mode, so the drain **refuses to start** when a finite `max_runtime` is in effect (via `ATHENAEUM_MAX_RUNTIME` / `librarian.max_runtime`).
- **No credential handling** (athenaeum#284/#330): requires `ANTHROPIC_API_KEY` in the environment and errors out naming that requirement if it is absent.
- **Cost guard is mandatory:** prints an up-front cost **estimate** (backlog × observed avg tokens/file × current model prices, batch discount applied) and requires `--yes` to proceed non-interactively.
- **Loops intake windows** until the raw backlog is empty, the cumulative `--max-usd` ceiling trips, or a window makes **zero progress** (stops loudly — never spins).

## Reasoning-tier triggers (athenaeum#909)

There is no shipped nightly cron wrapper in this repo — `athenaeum run` /
`athenaeum ingest` are invoked by an operator's own external cron / launchd
(`librarian.pull_before_run` / `push_after_run`, documented under "Librarian
run" above, make the same "no in-repo scheduler" assumption for git sync).
Tying reasoning to one such nightly window means a bad night is invisible for
24h and a large batch waits a full day. `athenaeum ingest --if-triggered`
(issue athenaeum#909) replaces the single window with a small set of
configurable triggers — backlog depth, an elapsed interval, and on-demand —
plus a nightly **backstop** that only fires when nothing else did:

```
athenaeum ingest --if-triggered
```

Not to be confused with `librarian.full_compile_every_days` (the
whole-corpus C2-C4 auto-memory **compile** cadence, above) or the
"Reasoning-tier screening (T1/T2)" section below (a DIFFERENT pipeline —
haiku/opus screening of merge proposals before they reach a human review
queue). This section is about *when a reasoning run happens at all*, not
what a run does once it starts.

Every trigger — however it fires — runs through the SAME `athenaeum ingest`
call path (`--incremental`, the default; never `--full`), so a fired trigger
is always the existing budgeted, resumable, incremental compile
(`athenaeum.librarian.ingest`) — never a full recompile. Evaluation is
side-effect-free and happens BEFORE the run lock is taken: when nothing
fires, `--if-triggered` is a cheap no-op (prints a one-line JSON summary with
`"trigger": "none"`, exit 0, no lock contention).

| Knob | YAML key | Default | What it does |
|---|---|---|---|
| Backlog trigger (files) | `librarian.reasoning_triggers.backlog_files` | unset = OFF | Fires when the pending raw-intake backlog reaches or exceeds this many files. |
| Backlog trigger (bytes) | `librarian.reasoning_triggers.backlog_bytes` | unset = OFF | Fires when the pending raw-intake backlog reaches or exceeds this many bytes (literal on-disk size, not a cost estimate). |
| Interval trigger | `librarian.reasoning_triggers.interval_hours` | unset = OFF | Fires once this many hours have elapsed since the last completed triggered run, regardless of backlog depth. |
| Nightly backstop | `librarian.reasoning_triggers.nightly_backstop_hours` | `24` | Always on. Fires once this many hours have elapsed since the last completed triggered run **and no other trigger fired this evaluation** — the demoted fallback, not the primary path. |

Without any of the three above configured, `--if-triggered` behaves as a
backstop-only poke: it runs whenever 24h (default) have passed since the
last triggered run, same as the old "once a night" cron, and otherwise no-ops.
Configuring `backlog_files` / `backlog_bytes` / `interval_hours` is what
makes reasoning respond to actual load instead of a fixed clock. `athenaeum
ingest` **without** `--if-triggered` is unaffected by any of this — it is the
pre-existing on-demand poke (issue athenaeum#349) and always compiles.

### Lock-free public evaluation (`--evaluate-only`, athenaeum#1001)

`athenaeum ingest --evaluate-only` runs ONLY the trigger evaluation above —
prints a one-line JSON verdict and returns — and, unlike `--if-triggered`,
**never compiles even when a trigger fires** and never takes
`.athenaeum.lock` (no lock is even considered):

```
athenaeum ingest --evaluate-only
```

```json
{"command": "ingest", "mode": "evaluate-only", "fired": true, "trigger": "backlog-files", "exit_code": 2}
```

Exit codes mirror this repo's existing dry-run-found-something ternary
(`athenaeum decay`, `athenaeum repair`): `0` — no trigger fired, `1` — an
error occurred evaluating, `2` — a trigger fired (`trigger` names which
one, same reasons as the table above). Cannot be combined with
`--if-triggered`.

This is the public surface a deployed evaluator (for example a
launchd/cron unit polling on a schedule this repo does not own, such as
`com.kromatic.athenaeum-reasoning-triggers`) should poll to decide whether
to invoke `athenaeum ingest --if-triggered` next — instead of importing
this module's private stamp symbols
(`athenaeum.librarian.REASONING_TRIGGER_STAMP_NAME`,
`athenaeum.librarian._load_timestamp_stamp`) directly, which breaks
silently on any internal rename of those symbols. Both modes read the
SAME reasoning-trigger stamp through the SAME shared internal helper, so
the two can never drift out of sync — see
`tests/test_ingest_reindex.py::TestIngestEvaluateOnlyCLI` for the tests
proving the lock-free guarantee, the shared stamp path, and that the
public mode's answer survives a simulated rename of those private symbols
(issue athenaeum#1001's ACs).

## Surface-divergence guard (athenaeum#963)

`athenaeum surface-divergence --field <name>` is the generalized, **failing**
guard for the class of defect athenaeum#853/#960/#963 name: two surfaces
(wiki frontmatter and the contacts/excluded surface) record the same fact
under independent write paths with no invariant binding them, so a
divergence is invisible unless something actually checks and can fail. It
supersedes `bounce-divergence` / `do-not-email-divergence` as the entry
point an unattended caller should use (those two commands still exist,
unchanged, for backward compatibility and interactive use).

```
athenaeum surface-divergence --field bounced --path ~/knowledge
athenaeum surface-divergence --field do_not_email --path ~/knowledge
```

| Field | Registered by | Tolerated residual |
|---|---|---|
| `bounced` | issue athenaeum#853 | Wiki-surface entries with no pii mark are TOLERATED — the documented evidence-class asymmetry, [bounce-surface-convergence.md](bounce-surface-convergence.md). Pii marks with no wiki entry are NOT tolerated (zero). |
| `do_not_email` | issue athenaeum#960 | The excluded surface newly carrying the field (`marked_on_excluded_not_wiki`) is NOT tolerated (zero). The wiki carrying a mark the excluded surface does not (`marked_on_wiki_not_excluded`) is the design's only legal steady state and is TOLERATED — the wiki page is the sole authoring surface; athenaeum#960's design forbids backfilling marks onto the excluded surface (narrowed by athenaeum#1039, which had alerted on this legal state). |

Exit codes (shared with `bounce-divergence` / `do-not-email-divergence`):
`0` clean (or `--report-only`, which reports and never fails on divergence —
interactive-inspection only, never for an unattended caller); `2` a surface
could not be read (the difference is not a divergence measurement); `3` a
registered field diverged beyond its declared allowance. An unregistered
`--field` value is rejected by argument parsing rather than guessed at.

**Where this runs unattended:**

- **CI, against fixture stores, on every push/PR** — `tests/test_surface_divergence.py`
  builds both fields' failing/passing/unreadable cases as synthetic
  fixtures and runs through this repo's normal `pytest` step in `ci.yml`,
  so a regression in the checker itself fails the build. GitHub Actions has
  no access to a live store, so this is deliberately the fixture-only half
  (2026-08-20 AC amendment on athenaeum#963).
- **The live store, on an operator's own schedule** — there is no shipped
  nightly cron wrapper in this repo (see "Reasoning-tier triggers" below);
  an operator's external cron/launchd (or a cross-repo nightly sweep, e.g.
  Hestia's, which is out of scope for this repository) should invoke both
  lines above alongside the existing `athenaeum run` / `athenaeum ingest`
  entries and treat a non-zero, non-report-only exit as an alert. This
  in-repo command and its documented exit-code contract are the
  registration artifact athenaeum#963 asks for; whether an operator's own
  external wrapper has actually added these two lines is state on that
  operator's host, outside anything this repository's tests can observe.

## Models

All model values are free-form model-id strings passed to the Anthropic SDK.
All four live under the `models:` yaml block (athenaeum#232, athenaeum#513) and share one
resolver helper (`config.resolve_model`). The resolver model additionally
accepts the pre-athenaeum#232 `resolve.model` key for backward compatibility.

| Knob | Env var | YAML key | Default | Used by |
|---|---|---|---|---|
| Classifier | `ATHENAEUM_CLASSIFY_MODEL` | `models.classify` | `claude-haiku-4-5-20251001` | Tier-2 classifier **and** the C4 contradiction detector — one knob by design. |
| Writer | `ATHENAEUM_WRITE_MODEL` | `models.write` | `claude-sonnet-4-6` | Tier-3 wiki writer. |
| Topic extractor | `ATHENAEUM_TOPIC_MODEL` | `models.topic` | `claude-haiku-4-5-20251001` | `athenaeum query-topics` recall query rewriting. |
| Resolver | `ATHENAEUM_RESOLVE_MODEL` | `models.resolve` (_also_ `resolve.model`¹) | `claude-opus-4-7` | Contradiction resolver (proposes a winner once the detector flags a conflict). |
| Reasoning tier 1 | `ATHENAEUM_REASONING_T1_MODEL` | `models.reasoning_t1` | `claude-haiku-4-5-20251001` | First-pass model for the reasoning-tier chain.² |
| Reasoning tier 2 | `ATHENAEUM_REASONING_T2_MODEL` | `models.reasoning_t2` | `claude-opus-4-1-20250805` | Escalation model for the reasoning-tier chain.² |

> ¹ `resolve.model` is still read post-athenaeum#512/#513 (`athenaeum.resolutions._get_model`), not yet removed. Precedence, highest first: `ATHENAEUM_RESOLVE_MODEL` env var, then `models.resolve` yaml, then `resolve.model` yaml (legacy), then the code default — so if both `models.resolve` and `resolve.model` are set, **`models.resolve` wins**. There is no scheduled removal; it is kept indefinitely so existing `athenaeum.yaml` files keep working unchanged. Prefer `models.resolve` for new configs, for consistency with the other model knobs.
>
> ² The reasoning-tier knobs are read by `athenaeum.reasoning_tiers`. `DEFAULT_TIER_CHAIN` (the pipeline's own default chain) is indeed empty, but both tiers have real production callers in `merge.py` that bypass that default — `t1_screen_rejects_merge_proposal` (athenaeum#518) and `t2_screen_merge_proposal` (athenaeum#602) — gated behind the single `ATHENAEUM_REASONING_TIER_AUDITING_ENABLED` flag, which **defaults off**. With the flag off (a fresh install), these knobs indeed have no runtime effect, matching the code default described above — but that is because the *screen* is opt-in, not because the tiers are unwired or dead code. See [Reasoning-tier screening (T1/T2)](#reasoning-tier-screening-t1t2--off-by-default) for the full picture, including what turning the flag on does.

### Per-MTok pricing (athenaeum#783)

Model pricing is **config-owned**: `athenaeum.yaml`'s `pricing:` section is
the authoritative per-MTok rate table, resolved by `config.resolve_model_rates`
and consumed by `TokenUsage.estimated_cost_usd`/`notional_cost_usd` (the same
cost figures `athenaeum spend` and the run-summary log line report). There is
**no env-var override** for this knob — a whole rate table doesn't fit the
single-value `ATHENAEUM_*` convention the other knobs on this page use.

```yaml
pricing:
  claude-opus-5: [5.0, 25.0]      # [input_usd_per_mtok, output_usd_per_mtok]
  claude-sonnet-5: [3.0, 15.0]
  # ... one entry per model-id PREFIX, matched by LONGEST prefix so a dated
  # id (claude-haiku-4-5-20251001) resolves to the right family.
```

`athenaeum init` writes this section **active** (not commented out, unlike
every other section on this page) and pre-populated with the current rate
table, generated from `athenaeum.models._MODEL_RATES_USD_PER_MTOK` — so a
fresh install is priced correctly out of the box.

**Replace, not overlay.** When `pricing:` is set and non-empty, it REPLACES
the code-default table wholesale — it does not merge on top of it. A model
prefix the operator's yaml doesn't mention is not backfilled from the code
default; it becomes unpriced (see the preflight below). This was a deliberate
choice over an overlay: an overlay would keep the code table as an invisible
second source of truth, and an omission there (the athenaeum#777 Fable/Mythos
incident: two new models silently priced at the blended average, under-
reporting spend 6.67x) would keep going unnoticed. An unset/empty `pricing:`
section (pre-athenaeum#783 configs) falls back to the code-default table
unchanged — existing installs are unaffected until they add one.

**Startup preflight.** `athenaeum run` resolves the model each of the six
LLM-serving knobs (`classify` / `write` / `topic` / `resolve` /
`reasoning_t1` / `reasoning_t2`) will use for the run and, if any has no
price under the resolved table, exits **rc 1** before any file is processed —
naming the unpriced model and the `pricing.<model>` key to set. This mirrors
`preflight_provider`'s pattern: a misconfiguration is a loud startup failure,
not a per-call surprise discovered later. Untagged tokens (accumulated with
no `model=` tag) are unaffected and still price at the blended fallback rate
— that path's original purpose is unchanged; only a *tagged* model used by
the run can no longer silently fall through to it.

**Schema contract: one rate per prefix, no mode dimension.** A prefix key
cannot express a time-boxed promo rate (Sonnet 5's introductory rate through
2026-08-31) or a per-request-mode rate (Opus 5's `speed: "fast"` rate) — both
are deliberately out of scope; encoding either is a schema change, not a
workaround at this layer.

### Per-stage token and thinking tuning (athenaeum#688)

Each LLM stage resolves two knobs through the same seam the model knobs use
(`provider.resolve_max_tokens` / `provider.resolve_thinking`, precedence: env var
→ `athenaeum.yaml` → code default):

- **`…_MAX_TOKENS`** — the stage's output-token ceiling (`max_tokens`). An
  integer; raising it lifts a truncation ceiling at higher spend, lowering it
  caps cost at the risk of clipping long outputs.
- **`…_THINKING`** — the stage's extended-thinking posture: `disabled` (no
  thinking block) or `adaptive` (the model may think first; see athenaeum#578). Stages on
  a thinking-enabled posture emit a leading thinking block before their answer.

These are the primary per-stage **spend levers** — they govern token ceilings
and thinking on a repo that ships explicit spend ceilings
(`resolve_spend_max_tokens_per_run`). They all default safely to code constants,
so an unset value degrades silently to the default rather than failing.

| Stage (what runs there) | `…_MAX_TOKENS` (default) | `…_THINKING` (default) |
|---|---|---|
| Claim-kind classify (epistemic-kind label) | `ATHENAEUM_CLAIM_KIND_MAX_TOKENS` (`64`) | `ATHENAEUM_CLAIM_KIND_THINKING` (`disabled`) |
| Tier-2 classify | `ATHENAEUM_CLASSIFY_MAX_TOKENS` (`4096`) | `ATHENAEUM_CLASSIFY_THINKING` (`disabled`) |
| Tier-2 classify retry (strict-JSON reminder) | `ATHENAEUM_CLASSIFY_RETRY_MAX_TOKENS` (`8192`) | — (no thinking knob) |
| Contradiction detector | `ATHENAEUM_CONTRADICTION_DETECT_MAX_TOKENS` (`1024`) | `ATHENAEUM_CONTRADICTION_DETECT_THINKING` (`disabled`) |
| Free-text edit (resolver amend) | `ATHENAEUM_FREETEXT_EDIT_MAX_TOKENS` (`8192`) | `ATHENAEUM_FREETEXT_EDIT_THINKING` (`adaptive`) |
| Tier-3 merge — create (new page) | `ATHENAEUM_MERGE_CREATE_MAX_TOKENS` (`6144`) | `ATHENAEUM_MERGE_CREATE_THINKING` (`adaptive`) |
| Tier-3 merge — full echo | `ATHENAEUM_MERGE_FULL_MAX_TOKENS` (`12288`) | `ATHENAEUM_MERGE_FULL_THINKING` (`adaptive`) |
| Tier-3 merge — anchored patch | `ATHENAEUM_MERGE_PATCH_MAX_TOKENS` (`6144`) | `ATHENAEUM_MERGE_PATCH_THINKING` (`adaptive`) |
| Resolver (contradiction winner) | `ATHENAEUM_RESOLVE_MAX_TOKENS` (`8192`) | `ATHENAEUM_RESOLVE_THINKING` (`adaptive`) |
| Recall topic extraction | `ATHENAEUM_TOPIC_MAX_TOKENS` (`256`) | `ATHENAEUM_TOPIC_THINKING` (`disabled`) |
| Reasoning tier 1 (Haiku pre-screen)² | `ATHENAEUM_REASONING_T1_MAX_TOKENS` (`256`) | `ATHENAEUM_REASONING_T1_THINKING` (`disabled`) |
| Reasoning tier 2 (escalation)² | `ATHENAEUM_REASONING_T2_MAX_TOKENS` (`4096`) | `ATHENAEUM_REASONING_T2_THINKING` (`adaptive`) |

> `ATHENAEUM_REASONING_TIER_AUDITING_ENABLED` (reasoning-tier decision auditing)
> is documented in [`authority-manifest.md`](authority-manifest.md), where the
> auditing behaviour it gates is described, rather than duplicated here.

**A CI check keeps this section honest.** `scripts/check_env_docs.py` — gated in
CI by `tests/test_env_docs.py` (part of the blocking test suite), and runnable
standalone — diffs every `ATHENAEUM_*` name read by `src/` against the names
documented here, and fails on any undocumented one.
The scan is digit-aware (`ATHENAEUM_[A-Z0-9_]+`) — the earlier `[A-Z_]+` sweeps
silently truncated `ATHENAEUM_REASONING_T1_MAX_TOKENS` to a fragment and
undercounted. Deliberately-internal vars live in that script's allowlist with a
one-line reason each.

### Sampling parameters are absent by design (athenaeum#579)

Athenaeum sends **no sampling parameters** to any LLM call site, and that is
**deliberate — not an oversight to be "fixed."** The three sampling knobs a
contributor might reach for — **`temperature`**, **`top_p`**, and **`top_k`** —
appear in none of the params dicts this codebase builds, and they must stay
absent.

**Why (operational fact first).** These parameters were **removed from the API**
on the current-generation model families and now **return HTTP 400** rather than
being accepted-and-ignored:

| Model family | `temperature` / `top_p` / `top_k` |
|---|---|
| Claude Opus 5 (`claude-opus-5`) | **removed — HTTP 400** |
| Claude Opus 4.8 (`claude-opus-4-8`) | **removed — HTTP 400** |
| Claude Opus 4.7 (`claude-opus-4-7`) | **removed — HTTP 400** |
| Claude Sonnet 5 (`claude-sonnet-5`) | **removed — a non-default value is rejected** |
| Claude Fable 5 (`claude-fable-5`) | **removed — HTTP 400** |
| Claude Sonnet 4.6 (`claude-sonnet-4-6`) | accepted |
| Claude Haiku 4.5 (`claude-haiku-4-5-20251001`) | accepted |

Applied against this repo's current defaults, adding a blanket sampling
parameter today would **400 exactly one stage — the Opus resolver**
(`models.resolve` defaults to `claude-opus-4-7`, the single most consequential
call) — and would break **every** stage the moment an operator points a model
knob at a 4.7+/5-family model. So the absence is load-bearing, not incidental.

**Ownership rule.** Any future model-aware determinism change is owned by
**`src/athenaeum/provider.py`** and is expressed as per-stage *intent* in config,
never as a raw wire parameter at a call site. The provider layer resolves the
model and decides whether a given wire parameter may exist on it — this is
exactly the normalization role `provider.py` already plays for `cache_control`,
which it strips on the `claude-cli` backend that does not accept it. A
determinism request is therefore a per-stage config intent that the provider
translates (or drops) for the resolved model; it is **not** a `temperature` key
that individual stages set. The machine-readable counterpart is the model-level
sampling-capability set colocated with the pricing table in
`src/athenaeum/models.py` (see athenaeum#577) — consult that prefix set programmatically
rather than hard-coding a family list at a call site.

**Where this even matters.** The determinism concern is real **only** for the
stages that run on Haiku 4.5 or Sonnet 4.6 (the classifier, topic extractor, and
writer defaults), where the parameters are still accepted. On the 4.7+/5
families the API default is the **only** available behavior, so determinism
tuning there is moot — there is nothing to set.

**Source of truth, and its limits.** The affected-family list above is the
bundled **`claude-api` skill**, the designated catalog for Anthropic model IDs
and behavior (do **not** answer model-behavior questions from memory; consult it)
— **verified 2026-08-01**. Treat that catalog as a **cached snapshot with its own
cache date, not an oracle**: it can lag real model launches. Concretely, its
2026-06-24 snapshot **omitted `claude-opus-5` for at least five weeks**, which
caused two false `premise-check` kickbacks on this very issue before the model's
existence was confirmed against live state. **Tie-break rule: where the cached
catalog and the live runtime disagree, the live runtime wins.** The `temperature`
/ `top_p` / `top_k` removal itself is stable across that gap and confirmed
against the catalog directly.

> This subsection is a guardrail, not an example. The audit finding **H5** (from
> epic athenaeum#516's precursor) recommended adding a sampling parameter *everywhere* —
> that recommendation was **wrong**, and this note is its correction. A future
> contributor reading H5 should stop here rather than re-open it. A lint or test
> that *enforces* the absence is worth having, but it is a code change and
> belongs in its own issue.

## LLM provider selection (athenaeum#330)

Athenaeum's librarian pipeline talks to Claude through a single **provider
seam** (`athenaeum.provider.build_llm_client`). Two backends ship:

| Knob | CLI flag | Env var | YAML key | Default | Used by |
|---|---|---|---|---|---|
| LLM provider | — | `ATHENAEUM_LLM_PROVIDER` | `llm.provider` | `api` | Selects the LLM backend for the librarian compile path (tiers, contradiction detector, resolver). `api` = the Anthropic SDK; `claude-cli` = the operator's ambient Claude Code subscription login. An unrecognized value is a hard error (no silent fallback). |
| CLI binary | — | `ATHENAEUM_CLAUDE_CLI_BIN` | — | `claude` | Override the `claude` executable used by the `claude-cli` backend (editable installs / non-PATH locations). |
| CLI timeout | — | `ATHENAEUM_CLAUDE_CLI_TIMEOUT` | — | `300` (s) | Per-call subprocess timeout for the `claude-cli` backend. A timeout is treated as a **transient** error — not retried in-run; the affected file is deferred to the next run. |

### `api` (default)

Wraps `anthropic.Anthropic(...)` verbatim: every request parameter — including
`cache_control` prompt-caching breakpoints (athenaeum#230) and the Messages Batch API
(athenaeum#236) — passes through unchanged. Requires `ANTHROPIC_API_KEY` (see below).
Behavior is byte-for-byte identical to pre-athenaeum#330 releases.

### `claude-cli` (subscription)

Drives your logged-in Claude Code via
`claude -p --system-prompt <sys> --model <id> --output-format json`, billing
the LLM work to your Claude **subscription** rather than a per-token API bill.
Athenaeum performs **no credential handling** — it relies on your ambient
`claude` login exactly as the post-run `git push` (athenaeum#284) relies on your ambient
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
- **Rate limits degrade gracefully, and are now retried in-run (athenaeum#782).**
  A subscription rate-limit or other transient CLI error (subprocess exit
  code + stderr, or the JSON envelope's `is_error`/`subtype`) is recognized by
  `with_retry` the same way as the `api` backend's SDK transients — bounded
  exponential backoff, then the SAME give-up as before once retries are
  exhausted: `_retry.TransientAPIError`, caught downstream as a give-up, the
  affected file **deferred to the next run**, single-machine run lock + resume
  making that pickup safe. A **subprocess timeout** is the one exception: it
  still maps directly to `_retry.TransientAPIError` and is NOT retried in-run
  (see the CLI timeout row above) — retrying a call that already burned its
  full `ATHENAEUM_CLAUDE_CLI_TIMEOUT` budget would multiply an already
  generous per-call timeout across `max_attempts`, so a timeout still defers
  straight to the next run.
- **Cost is subscription-covered ($0).** Token COUNTS from the CLI JSON
  envelope are still recorded per model in the run's `TokenUsage` and appear in
  the run summary, but `estimated_cost_usd` reports **$0** — the subscription
  already paid for them.
- **Batch mode is API-only.** `claude-cli` + batch mode
  (`ATHENAEUM_BATCH_MODE` / `librarian.batch_mode` / `--batch-mode`) is a loud
  startup error, not a silent fallback: the Messages Batch API has no CLI
  equivalent. Use `api` for batch runs.
- **The recall hot path routes through the same provider seam** as everything
  else (`athenaeum.query_topics`, a ~3 s hot-path budget) — it is affected by
  `ATHENAEUM_LLM_PROVIDER` / `llm.provider` like any other knob, and (athenaeum#786)
  can be pinned to a DIFFERENT provider via its own `topic` knob — see
  "Per-knob provider routing" below.

### Per-knob provider routing (athenaeum#786)

`llm.provider` / `ATHENAEUM_LLM_PROVIDER` above set the **global default**
provider. Each of the six model knobs (`classify`, `write`, `resolve`,
`topic`, `reasoning_t1`, `reasoning_t2` — the single source of truth is
`prompt_registry._META_ROWS`) can override that default independently:

| Override | Env var | YAML key |
|---|---|---|
| Per-knob provider | `ATHENAEUM_<KNOB>_LLM_PROVIDER` — the six concrete names: `ATHENAEUM_CLASSIFY_LLM_PROVIDER`, `ATHENAEUM_WRITE_LLM_PROVIDER`, `ATHENAEUM_RESOLVE_LLM_PROVIDER`, `ATHENAEUM_TOPIC_LLM_PROVIDER`, `ATHENAEUM_REASONING_T1_LLM_PROVIDER`, `ATHENAEUM_REASONING_T2_LLM_PROVIDER` | `llm.providers.<knob>` |

Precedence, per knob: `ATHENAEUM_<KNOB>_LLM_PROVIDER` env > `llm.providers.<knob>`
yaml > the global default (`ATHENAEUM_LLM_PROVIDER` env > `llm.provider` yaml >
`api`). A knob with neither override set inherits the global default
unchanged — a config with no `llm.providers` section behaves byte-identically
to a pre-athenaeum#786 install. An unrecognized value in a per-knob key is a hard
error naming the knob (no silent fallback), exactly like the global key.

Example — pin the recall sidecar to the Claude subscription while everything
else stays on the metered API:

```yaml
llm:
  provider: api          # global default
  providers:
    topic: claude-cli     # recall query-topic extraction only
```

**What is actually wired today (issue athenaeum#841 — all six knobs are wired):**

Every one of `classify` / `write` / `resolve` / `topic` / `reasoning_t1` /
`reasoning_t2` genuinely routes to the provider it resolves to:

- `topic` (`athenaeum.query_topics`, the recall sidecar) and `resolve` via
  the `athenaeum ingest-answers` / `athenaeum reresolve-questions` CLI
  commands each resolve their own provider independently and construct
  their own client (issue athenaeum#786) — unchanged by athenaeum#841.
- `classify`, `write`, `resolve` (within an `athenaeum run` librarian run —
  distinct from the `resolve` knob's CLI-command path above, wired
  separately), `reasoning_t1`, and `reasoning_t2` are now each threaded
  through the librarian's (`athenaeum run`) entity/merge pipeline as their
  OWN client (issue athenaeum#841): `classify` serves `tiers.classify`
  (page classification), the C4 contradiction detector, and `claim_kind`
  stamping; `write` serves `tiers.tier3_create`/`tier3_merge` (including the
  Batch API's tier-3 batch and its same-page-merge/truncation-retry
  fallbacks); `resolve` serves the C4 resolver (`resolutions.
  propose_resolution`); `reasoning_t1`/`reasoning_t2` serve the merge-phase
  reasoning-tier screen. Clients are constructed per DISTINCT resolved
  provider, not per call or per knob — several knobs sharing one provider
  (the common case: no per-knob overrides) share exactly ONE client, same as
  before athenaeum#841. The athenaeum#786 "accepted but inert" startup warning is
  gone — every override above now has an effect, so there is nothing left
  to warn about.
- In the spend ledger, `athenaeum spend --by-knob` shows the real provider
  split for a librarian run too: when a run's knobs resolve to more than one
  provider, the run writes one ledger row PER distinct provider (each
  carrying only that provider's own token/knob/model attribution and correct
  `billing_mode`) instead of one row assuming a single provider for the
  whole run.
- **Known limitation — knob granularity, not functional-area granularity.**
  The `classify` knob is shared by `tiers.classify` (the librarian's page
  classifier), `contradictions.detect_system` (the C4 contradiction
  detector), and `claim_kind` — routing "the contradiction detector on a
  different provider than the page classifier" is **not** reachable through
  `llm.providers.classify` today; it needs the `classify` knob split into
  separate knobs first, which is a deliberate, separate refactor (unchanged
  by athenaeum#841 — recorded, not solved, exactly as athenaeum#786 originally
  documented it).

Batch mode (`ATHENAEUM_BATCH_MODE` / `librarian.batch_mode`) is served by the
`classify` and `write` knobs only (`batch.py`'s two `execute_batch` call
sites), now via each knob's own client. The startup guard still checks BOTH
knobs' resolved providers before either client is built — batch mode +
`claude-cli` on either one is a loud startup error, matching the
`claude-cli` provider's existing "Batch mode is API-only" constraint above.
In practice the two rarely differ in a batch run: the guard requires BOTH to
resolve to `api` before a batch run can even start.

## Spend ledger and ceiling (athenaeum#378)

Athenaeum spends on two cost models that must **never be blended**: the
`claude-cli` **subscription** path (no invoice — consumes your Claude
subscription quota, constrained in TOKENS) and the metered `anthropic` **API**
path (real dollars — the resolver on the `api` backend, batch mode, and the
per-turn `query-topics` recall extractor). The **durable spend ledger** records
each run's usage so "how much has athenaeum spent, and is any of it real
money?" is answerable from data rather than a code audit.

Each pipeline run appends one JSONL record to `~/.cache/athenaeum/spend.jsonl`:
timestamp, `run_type`, **`provider`** (`claude-cli` vs `anthropic`), model
id(s), the four token counters kept **separate** (input / output / cache-write
/ cache-read — cache-read is ~10x cheaper, so collapsing them destroys the
signal), and a **provider-tagged** `estimated_cost_usd` that is always `0` on
the subscription path (subscription rows can never be summed into a dollar
total). The ledger is append-only and crash-safe (a single `O_APPEND` write per
record; a torn trailing line is skipped on read) and records only counts and
metadata — never prompt/response content or credentials.

Report it with `athenaeum spend`:

```
athenaeum spend --since 7d [--by-model] [--by-provider] [--by-knob] [--reprice] [--json]
```

`--since` accepts a window (`7d` / `24h` / `30m` / `2w`) or an ISO date. The
output keeps **$ (API)** and **tokens (subscription)** on separate rows;
`--json` is the machine-readable shape `/good-morning` consumes.

### `athenaeum spend --reprice` — recompute history at current rates (athenaeum#788)

`tokens_by_model` (schema v2, athenaeum#487) exists so that *`tokens x model` is
the fact and dollars are derived* — a historical row stays repriceable per
model instead of freezing a blended figure. `--reprice` is what consumes it:

```
athenaeum spend --since 30d --reprice [--json]
```

It recomputes each row from its stored per-model token attribution against the
**current** rate table — including the operator's `pricing:` overrides
([Per-MTok pricing](#per-mtok-pricing-athenaeum783), athenaeum#783) — and reports the recomputed
total alongside the stored one, with the delta. This is what makes a rate
correction worth anything **retroactively**: fixing an under-priced model (the
athenaeum#777 Fable/Mythos gap under-reported 6.67x) does not rewrite rows already
written, but `--reprice` tells you what those rows *should* have said.

Three properties worth stating plainly:

- **Read-only.** The ledger is append-only by design. `--reprice` reports a
  corrected figure; it never rewrites history. The file is byte-identical after
  a reprice run (pinned by test). Rewriting the ledger in place would be a
  separate, explicitly-destructive command with its own decision — it does not
  exist, deliberately.
- **Unpriceable rows are reported, not dropped and not zeroed.** A row with no
  per-model attribution (pre-v2, or any run that tagged no model) has an
  *unknown* price, not a zero one. It stays in `record_count` and in its billing
  bucket, is counted in `unpriceable_records`, and its stored dollars are
  surfaced as `unpriceable_stored_usd`. It never contributes to `repriced_usd`.
- **The billing split survives.** Repricing is per bucket. A `subscription` row
  is `$0.00` stored **and** `$0.00` repriced — repricing never turns
  subscription draw into money owed; what it corrects there is the
  counterfactual `notional_usd`. An `unknown` row reprices inside `unknown` and
  is never folded into `api`.

With `--json` the payload carries `since`, `ledger_path`, and a single
`reprice` object: `record_count`, `unpriceable_records`, `repriced_records`,
and a `subscription` / `api` / `unknown` bucket each carrying `stored_usd`,
`stored_notional_usd`, `stored_usd_priceable`, `stored_notional_usd_priceable`,
`repriced_usd`, `repriced_notional_usd`, `delta_usd`, `delta_notional_usd`,
`records`, `repriced_records`, `unpriceable_records`,
`unpriceable_stored_usd`, `unpriceable_stored_notional_usd`.

The delta is computed against `stored_usd_priceable` — the stored value of
**exactly the rows that were repriced** — not against `stored_usd`, which also
covers the rows repricing could not touch. Comparing against the latter would
report unpriceable rows as if they were a rate change.

`--reprice` replaces the default report rather than adding to it; without the
flag, the `--json` contract below is unchanged.

### `athenaeum spend --json` — consumer contract (athenaeum#694)

`athenaeum spend --json` is a **stable contract**, not an incidental dump: a
consumer (e.g. `/good-morning`) reads these fields directly rather than scraping
the rendered report or the source. **athenaeum emits facts; the caller computes
ratios.** The shape:

| Field | Type | Unit | What it asserts — and does not |
|---|---|---|---|
| `since` | string (ISO-8601 `Z`) | — | Lower bound of the window summarised (inclusive). |
| `ledger_path` | string | — | Absolute path of the ledger file read. |
| `record_count` | int | rows | Total ledger rows in the window. Every row is counted here — including `unknown` rows — so a bucket total plus `unknown` reconciles to it. |
| `unpriceable_records` | int | rows | Rows with no per-model attribution (pre-v2, or a v2 run that tagged no model). They are **not dropped** and stay in their billing bucket; the count tells a re-pricing consumer how many rows it must treat as opaque. |
| `knob_unattributed_records` | int | rows | Rows with no per-knob attribution (pre-v3, or a v3 run that tagged no knob — issue athenaeum#781). Same treatment as `unpriceable_records` one level down: **not dropped**, stays in its billing bucket. |
| `subscription` | object (bucket) | **tokens** | The `claude-cli` path. Report its **tokens**. `estimated_cost_usd` is hard-`0.0` here and must be ignored — subscription draw has no invoice. |
| `api` | object (bucket) | **dollars** | The metered `anthropic` path. `estimated_cost_usd` is real money. |
| `unknown` | object (bucket) | **tokens** | Rows whose billing mode could **not** be determined (no known `billing_mode` and no recognised `provider` — a hand-edited or corrupt row). **Always present** (blank when none) so *unknown is a distinct state from zero*: a consumer must never mistake an undeterminable row for API spend, or for no activity. Never folded into `api`/`subscription`; report its **tokens**, since its unit is by definition unknown. |
| `by_model` | object | — | Present only with `--by-model`; each model maps to `{subscription, api, unknown}` sub-buckets. |
| `by_run_type` | object | — | Present only with `--by-provider`; each run type maps to `{subscription, api, unknown}` sub-buckets. |
| `by_knob` | object | — | Present only with `--by-knob` (issue athenaeum#781); each model knob (`classify` / `write` / `resolve` / `topic` / `reasoning_t1` / `reasoning_t2`) maps to `{subscription, api, unknown}` sub-buckets — the subscription/API split stays intact within each knob, never blended. |

Each **bucket** object carries: `input_tokens`, `output_tokens`,
`cache_creation_input_tokens`, `cache_read_input_tokens`, `total_tokens`,
`api_calls`, `records` (all ints), and `estimated_cost_usd` (float).

Two invariants the contract guarantees, and one thing it deliberately omits:

- **`api` and `subscription` are in different units and must NEVER be summed.**
  `api.estimated_cost_usd` is dollars; `subscription.total_tokens` is tokens.
  Adding them, or rendering subscription tokens as a dollar figure, is a
  category error — the subscription bucket's only dollar field is a hard `0.0`
  precisely so it cannot be blended.
- **Unknown ≠ zero.** An undeterminable row appears in `unknown`, explicitly,
  rather than silently defaulting into `api` or being omitted.
- **athenaeum does NOT assert account identity.** No field names which account a
  call was billed to. On the `api` path athenaeum holds only the secret
  `ANTHROPIC_API_KEY`; on `claude-cli` a subscription CLI handle — and in a
  shared deployment its account is used by other workloads it cannot see. A
  consumer that needs to aggregate across accounts **owns that knowledge
  itself**; athenaeum will not emit a partial (and secret-adjacent) account
  identifier per record.

The **spend ceiling** is the actual mitigation — a monitor reports after the
fact, the ceiling stops the burn. When a configured ceiling is reached the
librarian pass stops early and loudly and defers the remaining intake (exactly
like the `max_api_calls` budget), never silently continuing. All ceilings are
**off unless configured**.

| Knob | Env var | YAML key | Default | What it does |
|---|---|---|---|---|
| Ledger enabled | `ATHENAEUM_SPEND_LEDGER_ENABLED` | `spend.ledger_enabled` | `true` | Write the durable spend ledger. Off is a clean no-op. |
| Ledger path | `ATHENAEUM_SPEND_LEDGER` | `spend.ledger_path` | `~/.cache/athenaeum/spend.jsonl` | Override the ledger file location (test/relocation seam). |
| Per-run token ceiling | `ATHENAEUM_SPEND_MAX_TOKENS_PER_RUN` | `spend.max_tokens_per_run` | — (off) | **Subscription path.** Stop the run when its total tokens reach this. |
| Per-day token ceiling | `ATHENAEUM_SPEND_MAX_TOKENS_PER_DAY` | `spend.max_tokens_per_day` | — (off) | **Subscription path.** Ledger tokens since UTC midnight + this run. |
| Per-run dollar ceiling | `ATHENAEUM_SPEND_MAX_USD_PER_RUN` | `spend.max_usd_per_run` | — (off) | **API path.** Stop the run when its estimated USD reaches this. |
| Per-day dollar ceiling | `ATHENAEUM_SPEND_MAX_USD_PER_DAY` | `spend.max_usd_per_day` | — (off) | **API path.** Ledger dollars since UTC midnight + this run. |
| Weekly subscription token limit | `ATHENAEUM_SPEND_WEEKLY_TOKEN_LIMIT` | `spend.weekly_token_limit` | — (off) | **Subscription path.** The operator-declared weekly quota; a denominator, not a ceiling by itself (issue athenaeum#785). |
| Max percent per day | `ATHENAEUM_SPEND_MAX_PCT_PER_DAY` | `spend.max_pct_per_day` | — (off) | **Subscription path.** Paired with the weekly limit above: `weekly_token_limit / 7 * max_pct_per_day / 100` becomes a SECOND, derived per-day token ceiling (issue athenaeum#785). |
| Headroom warning threshold | `ATHENAEUM_SPEND_WARNING_THRESHOLD_PCT` | `spend.warning_threshold_pct` | `75` | **API path.** Log a warning, naming which cap and by how much, once a run ends at/above this percent of EITHER the per-run or per-day dollar ceiling — before either one trips (issue athenaeum#926). Unlike the ceilings above this is not opt-in: it always resolves to a usable value, but it warns only when at least one dollar ceiling is actually configured. |

The subscription path is bounded in **tokens**, the API path in **dollars** —
each ceiling only gates its own path. `bool` / non-numeric / non-positive
values fall through to "off" so a nonsensical value can never silently pin the
pass to a no-op.

**Headroom warning (issue athenaeum#926).** `ceiling_tripped()` reports only a
breach — below either dollar ceiling it returns nothing, so a run at 99% of
the day cap and a run at 1% look identical until the cap actually trips. A
run that ends at or above the warning threshold gets a `log.warning` naming
WHICH cap is close (per-run vs per-day call for different operator actions)
and the remaining dollars, computed by `athenaeum.spend.spend_headroom()` /
`spend_headroom_warning()`. It fires on the exact same path a trip is
surfaced on (`ceiling_tripped()`, called from `librarian.py`, `merge.py`, and
`batch.py`), so a run that ends past the ceiling gets BOTH the warning and the
trip, never only one. An unconfigured dollar ceiling never warns — headroom
reports a distinct "not configured" state rather than reading as 0% or 100%
consumed.

```yaml
spend:
  # ledger_enabled: true          # on by default
  max_tokens_per_run: 2000000     # cap the nightly subscription burn
  max_usd_per_day: 5.00           # cap real API dollars per day
  weekly_token_limit: 700000      # declared weekly subscription quota
  max_pct_per_day: 50             # -> 50,000 token/day derived ceiling
  # warning_threshold_pct: 75     # warn at 75% of either dollar cap (default)
```

The weekly-limit + max-percent-per-day pair (issue athenaeum#785) is a SECOND,
independent way to bound the subscription per-day figure — derived rather
than absolute, and strictly opt-in like every ceiling here: setting only one
of the two does nothing (there is no denominator, or no percentage, to apply
on its own). It reuses the same UTC-midnight day boundary as the per-day
ceilings above (a rolling 7-day window is deliberately out of scope) and
never gates the API path — a token-denominated percentage has no meaning
there, and `subscription` notional tokens and `api` real dollars are two
metrics this ledger never blends (athenaeum#487, cwc#1629).

## Push-precision and coverage instrumentation (athenaeum#711)

The v6 memory-model epic's definition of done requires push precision
("fraction of pushed items actually referenced by the consuming session") to
improve over a baseline recorded **before** any later v6 slice changes what
`recall` pushes. This instrument is that baseline: it is passive measurement,
**on by default**, and records into two durable JSONL ledgers under the cache
dir (`~/.cache/athenaeum/_push_records.jsonl` and
`~/.cache/athenaeum/_push_references.jsonl`) — never inside the wiki/raw
corpus, so a push record can never become a claim or enter the embedded
index.

- A **push record** is written every time `recall` renders a hit into a
  session's response (the `recall` MCP tool / `recall_search`), keyed by the
  ambient `CLAUDE_CODE_SESSION_ID` (the variable Claude Code actually exports to
  the stdio MCP servers it spawns; the older `CLAUDE_SESSION_ID` is accepted as
  a fallback — athenaeum#734). It carries only ids, a tier (the page's
  `access` level), a matched scope (the granted `audience` roles, or `open` /
  `owner`), and an estimated token cost — **never claim content and never
  personal data**. A pushed person page's id is its opaque frontmatter `uid`,
  never the filename (which embeds a name-derived slug).
- A **reference-determination** record is written at `session_end` (the
  SessionEnd-hook / nightly-after-librarian path), marking which of that
  session's pushed ids were actually referenced afterward in the session's
  own transcript. `precision = referenced / pushed`.
- `athenaeum push-metrics baseline` computes precision over a window and
  writes a dated snapshot into `docs/memory-model-measurements.md`.
  `athenaeum push-metrics coverage-audit` samples sessions into a worksheet
  file — but, because push records retain only a query HASH and never the
  raw query text, that worksheet cannot support a per-candidate relevance
  marking or a measured coverage-floor miss rate. It instead reports the
  structural facts hash-only records DO support (candidate-pool size,
  tier/scope concentration, how much a tier/scope filter removes from each
  session's window-mate pool) plus the policy-set bounds those facts imply,
  opening with a plain-language statement of what it cannot establish and
  why (athenaeum#1036, the ruling that withdrew per-candidate marking from
  this command's scope).

| Knob | Env var | YAML key | Default | What it does |
|---|---|---|---|---|
| Instrumentation enabled | `ATHENAEUM_PUSH_METRICS_ENABLED` | `push_metrics.enabled` | `true` | Write push records on every recall push and reference-determination records at session end. Off is a clean no-op (identical recall output either way) — see `athenaeum.push_metrics`. |

```yaml
push_metrics:
  enabled: true    # on by default; passive measurement only
```

## LLM schema-observation ledger (athenaeum#570 / athenaeum#724)

Every in-scope LLM contract's response is validated **observe-only** against a
pydantic model (`athenaeum.llm_schemas.observe`): a mismatch is logged and
counted, but NEVER changes what the pipeline does with the response. athenaeum#724
made the observation **complete and measurable**:

- Every observation — clean or mismatched — is recorded to a durable,
  append-only ledger at `~/.cache/athenaeum/_llm_schema_observations.jsonl`
  (under the cache dir, never in the corpus), so every reported mismatch rate
  has a real **denominator**. This is required because the `query_topics`
  contract runs inside the MCP server, whose Python logging is retained nowhere
  — a log-only marker there is discarded.
- A **total parse failure** (a response that never yields a JSON object) is
  counted as a `parse-fail` mismatch from the parse guard, even though it
  returns before reaching `observe`.
- Each mismatch carries its **class** — `extra-keys`, `missing-required`,
  `parse-fail`, `wrong-type` — so the reject-vs-degrade question (athenaeum#608)
  can be answered per class without re-reading raw logs.
- `athenaeum.llm_schemas.aggregate_observations()` summarises the ledger
  per contract. A contract with **zero** observations is reported as an
  explicit `no_data` row (rate `None`), distinct from 0 mismatches over 400 —
  so a contract with no production caller can never read as a false clean 0.
  (`claim_kind` was such a row until athenaeum#742 wired `stamp_claim_kind` into
  the nightly auto-memory intake phase; it now reports real traffic.)

The ledger records only schema **shape** — field paths, error messages, and
unexpected key *names* — never a field **value**, so no claim content or
personal data reaches it.

| Knob | Env var | Default | What it does |
|---|---|---|---|
| Observation ledger enabled | `ATHENAEUM_SCHEMA_OBSERVATIONS_ENABLED` | `1` | Append one record per LLM-contract observation. Set `0` to disable (behaviour is otherwise unchanged — observation is passive). |

**This ledger is production-only.** Before athenaeum#750, `tests/conftest.py`
isolated nothing, so any test that drove a parse site through
`observe()`/`record_observation()` with no explicit `cache_dir` fell through
`resolve_cache_dir`'s `arg > ATHENAEUM_CACHE_DIR env > default` order to the
real `~/.cache/athenaeum`, silently appending test noise to the operator's
production ledger. As of **2026-08-05**, `tests/conftest.py` carries an
autouse fixture (`_isolate_cache_dir`, function-scoped) that points
`ATHENAEUM_CACHE_DIR` at a per-test tmp dir and defaults
`ATHENAEUM_SCHEMA_OBSERVATIONS_ENABLED=0` for every test — so the ledger is
trustworthy (free of test-run pollution) only for records written from
**2026-08-05 onward**; rows already in an existing ledger from before that
date may include test-suite noise. A test that specifically needs to exercise
the ledger opts back in explicitly (see `tests/test_llm_schemas.py`'s
`_isolate_observation_ledger` fixture): pass an explicit `cache_dir` (which
wins over the env var per `resolve_cache_dir`'s precedence) and/or
`monkeypatch.setenv("ATHENAEUM_SCHEMA_OBSERVATIONS_ENABLED", "1")`.

### Per-contract strictness decision (M17 phase 2a, athenaeum#1035)

Phase 1 above validates every in-scope contract against a uniform
`extra="allow"` posture and never changes pipeline behavior. athenaeum#1035 (split
from athenaeum#608) decides a per-contract posture for the two contracts with a
measured, representative window — `tiers.tier2` and `tiers.tier3-merge` —
recorded in code as `athenaeum.llm_schemas.STRICT_CONTRACTS`. The other four
contracts (`query_topics`, `claim_kind`, `contradictions`, `resolutions`)
remain phase 1 observe-only; the three C4-downstream ones are still starved of
a representative sample and stay on athenaeum#608.

Window measured: `~/.cache/athenaeum/_llm_schema_observations.jsonl`,
2026-08-05T13:12Z to 2026-08-20T10:58Z, 3,634 records, 0 unparseable:

| contract | records | mismatches | rate | mismatch classes |
|---|---:|---:|---:|---|
| `tiers.tier3-merge` | 2,961 | 4 | 0.135% | extra-keys 3 (`[].text2`, `[].append_section` x2), missing-required 1 (`0.op: Field required`) |
| `tiers.tier2` | 150 | 0 | 0% | — |

Decision, applying athenaeum#608's framework ("only missing-required mismatches
justify rejection; extra keys are a different signal"):

- **`tiers.tier2`** — 0% mismatch of any class at n=150: `Tier2Entity` tightens
  from `extra="allow"` to `extra="forbid"`. Forbidding costs nothing observed
  today and gives real protection against a silently-added field going
  unnoticed. `name` stays the only required field.
- **`tiers.tier3-merge`** — the two extra-key shapes observed (`[].text2`,
  `[].append_section`) are real, repeated traffic, not sampling noise — the
  framework's "different signal", not grounds for rejection. `MergeOp` stays
  `extra="allow"`. The single missing-required hit (`0.op: Field required`)
  confirms `op` stays required; that class is already enforced downstream —
  `apply_merge_ops` raises `MergeOpsError` on a missing/unrecognized `op`
  kind, and `parse_merge_ops_response` turns that into the guaranteed
  no-worse-than-status-quo full-echo fallback.

This is a schema-shape decision only: `observe()` for these two contracts
still never raises to its caller and never gates the pipeline — the tightened
`tiers.tier2` schema changes how a *future* mismatch is classified in the
observation log, not whether today's response is accepted. No new
`athenaeum.yaml` key or env var is introduced; the decision is a hardcoded,
documented constant (`STRICT_CONTRACTS`), matching how `INSTRUMENTED_CONTRACTS`
above is expressed.

## Contradiction detection and resolver

Detection knobs live under the `contradiction:` yaml block; resolver behavior
knobs under `resolve:`. The resolver *model* lives under `models.resolve` with
the other model knobs (`resolve.model` is the legacy fallback — see
[Models](#models)). Pipeline walkthrough:
[contradiction-detection.md](contradiction-detection.md); auto-apply lane:
[auto-resolve.md](auto-resolve.md).

| Knob | Env var | YAML key | Default | What it does |
|---|---|---|---|---|
| Cross-scope mode | `ATHENAEUM_CROSS_SCOPE_MODE` | `contradiction.cross_scope_mode` | `ancestor` | `off` / `ancestor` / `similarity` / `both` (athenaeum#125). Invalid env values log a warning and fall back. |
| Cluster size cap | — | `contradiction.cluster_size_cap` | `25` | Pooled-cluster size cap; oversized pools are split into newest-first chunks before detection. |
| Similarity threshold | — | `contradiction.similarity_threshold` | `0.85` | Cosine cutoff for the cross-scope similarity sweep (`similarity` / `both` modes). |
| Resolver cap per run | `ATHENAEUM_RESOLVE_MAX_PER_RUN` | `contradiction.resolve_max_per_run` | `250` | Per-ingest cap on resolver calls (raised from 50 in athenaeum#187). Surplus detections escalate without a proposal. `0` disables the resolver entirely. |
| Resolved-similarity threshold | `ATHENAEUM_RESOLVED_SIMILARITY_THRESHOLD` | `contradiction.resolved_similarity_threshold` | `0.83` | Cosine threshold for matching a new detection against the decision log of previously resolved contradictions (athenaeum#211). |
| Not-a-conflict TTL (days) | `ATHENAEUM_NOT_A_CONFLICT_TTL_DAYS` | `contradiction.not_a_conflict_ttl_days` | `0` | Read-time decay of stale **auto** `not_a_conflict` suppressions (athenaeum#251). `0` disables decay (current behavior — a suppression never expires). When `> 0`, an auto suppression whose `resolved_at` is older than this many days is treated as absent from the confirmation-pass skip set, so the pair re-enters the Opus confirmation. Human verdicts and enacting auto verdicts (`keep_*`/`correct_*`/`forget_*`/`deprecate_both`) never decay; undated rows keep suppressing (fail-safe). The append-only cache is never mutated; re-validation flows through the existing `resolve_max_per_run` cap. |
| Auto-apply | `ATHENAEUM_RESOLVE_AUTO_APPLY` | `resolve.auto_apply` | `true` | Apply high-confidence resolver proposals without human review (athenaeum#156). Env accepts `true`/`false`, `1`/`0`, `yes`/`no` (case-insensitive). |
| Auto-apply threshold (legacy scalar) | `ATHENAEUM_RESOLVE_AUTO_APPLY_THRESHOLD` | `resolve.auto_apply_threshold` | `0.90` | Confidence floor in `[0.0, 1.0]`; out-of-range values raise on read. Since athenaeum#170 this scalar is honored only as a backward-compat fallback for `keep_a` / `keep_b`. |
| Per-action thresholds | — | `resolve.auto_apply_threshold_per_action` | `not_a_conflict: 0.75`, `keep_a`/`keep_b`/`deprecate_both`: `0.90`, `correct_a`/`correct_b`/`forget_a`/`forget_b`: `0.95` | Per-action confidence floors (athenaeum#170, athenaeum#191). `propose_merge` **never** auto-applies regardless of confidence. **As of athenaeum#752, the `correct_a`/`correct_b` entries here are no longer consulted for the auto-apply decision** — see the note immediately below the table. `forget_a`/`forget_b` are unaffected and still gate on their `0.95` floor exactly as before. |
| Full-body token cap | `ATHENAEUM_RESOLVE_FULL_BODY_TOKEN_CAP` | `resolve.full_body_token_cap` | `1500` | Per-side body cap for the resolver's full-body context (athenaeum#168), ~4 chars/token. Must be a positive integer; zero/negative raise — set a large value to effectively disable truncation. |
| Tier-4 escalation dedup | `ATHENAEUM_TIER4_DEDUP` | — | `true` | Dedupe `_pending_questions.md` escalations by source-memory pair (athenaeum#157). Set `false`/`0`/`no`/`off` to restore the legacy always-append behavior. |

#### `correct_a` / `correct_b` auto-apply: authorship, not confidence (issue athenaeum#752)

`correct_a` / `correct_b` are the resolver's most dangerous verdicts —
enactment **deletes** the losing member's raw file (`enact_resolution`,
`src/athenaeum/resolutions.py`). Prior to athenaeum#752 that delete was gated purely
on the `resolve.auto_apply_threshold_per_action.correct_a`/`correct_b`
confidence floor (default `0.95`, shown in the table above). Confidence is
the wrong axis to gate an irreversible delete on — models are poorly
calibrated exactly at the top of their range, where a destructive auto-apply
fires.

**The authorship gate REPLACES the confidence threshold for these two
actions; it does not stack on top of it.** For `correct_a`/`correct_b`:

- The per-action threshold key (`resolve.auto_apply_threshold_per_action.correct_a`
  / `.correct_b`, and the legacy scalar `resolve.auto_apply_threshold`) is
  **not consulted at all** — setting it has no effect on `correct_*`
  auto-apply, regardless of value.
- Instead, auto-apply requires the WINNING member (the side named by the
  resolver's `recommended_winner` — the side asserting the correction) to
  trace to a **transcript-verified human utterance**: at enact time,
  `athenaeum.transcript_verify.classify_backfill_claim` reads the winning
  member's origin-session transcript (`origin_scope` / `origin_session_id` /
  `origin_turn`) and classifies its claim. Auto-apply proceeds ONLY when the
  classified channel is `user-stated`. `agent-observed`, `inferred`, and
  `unavailable` (missing/rolled-off transcript — the most likely production
  outcome) all escalate to the human instead of deleting.
- The check **re-derives the channel from the transcript on every call** and
  never reads the member's own `source_type` frontmatter field — that field
  is self-declared by whoever wrote the memory (possibly the model itself),
  and trusting it would let a model grant itself deletion authority by
  emitting a string.
- The gate decision (channel + ref, e.g. `user-stated sess123#turn4`, or
  `unavailable sess123`) is logged for **every** `correct_*` verdict — permit
  or refuse — via the module logger, so a refusal is diagnosable without
  re-running the resolver.
- This gate is not itself configurable (no yaml knob) — see
  `docs/conflict-resolution.md` §14 for the full rule and its known limits.

Every OTHER action's threshold behavior is unchanged: `not_a_conflict`,
`keep_a`/`keep_b`, `deprecate_both`, `scope_a`/`scope_b`, `attribute_both`,
and `forget_a`/`forget_b` all still gate purely on their per-action
confidence floor exactly as documented in the table above. `forget_a`/
`forget_b` in particular are explicitly OUT OF SCOPE for athenaeum#752 — they
delete a *transient* member with no human assertion behind it (legitimate
janitorial cleanup), so they keep auto-applying on their `0.95` floor with
no transcript check.

### Scoped-claim tree (`scope:`, athenaeum#329)

The org/locale scope dimensions (issue athenaeum#329) read a small **versioned tree** of
the values a claim's `scope: {org, locale}` frontmatter may declare. A value NOT
listed here normalizes to *unscoped* (adds no constraint) with a debug
breadcrumb — authors may not mint scope values (the Cyc-microtheory lesson), and
the fail-open direction is toward detection. There is **no default** (no
`_DEFAULTS` seed, athenaeum#231): a fresh install has empty trees, so scope frontmatter is
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
`serve --scope` caller-context filter is deferred design (athenaeum#314).

## Intake screening (athenaeum#320)

Optional content screening applied to raw intake before it is compiled. The
medical classifier ships **off**; when enabled it routes/withholds medical
intake per the configured action and access.

| Knob | Env var | YAML key | Default | What it does |
|---|---|---|---|---|
| Medical screening | `ATHENAEUM_SCREEN_MEDICAL` | `screening.medical.action` | `off` | Action for medical intake: `off` (default, no screening) or an enabled action per `docs/screening.md`. **Fails loudly** — a mis-set value raises `ScreeningConfigError` rather than serving with a silently inert classifier. This is one of the two deliberate exceptions to the WARN-and-fall-back malformed-env policy (athenaeum#528). |

## Sensitivity classes (athenaeum#910)

The open, deployment-configurable sensitivity-class vocabulary specified in
[`docs/sensitivity-class-vocabulary.md`](sensitivity-class-vocabulary.md).
Athenaeum ships exactly one class today (`pii`); a deployment can define its
own (`hipaa`, a `classified`/`secret`/`top-secret` gradation, …) without
patching this repository. This section documents the **class-vocabulary**
half only — *which classes exist, what detects them, and what read policy
each carries*. The recogniser code-registration contract
(`register_recognizer` / `available_recognizers`) is a Python extension
point, not a YAML knob, and is documented in `sensitivity.py`'s module
docstring rather than here; the class-to-storage-surface mapping is the
**pre-existing, unchanged** `storage.mapping`/`storage.adapters` knobs
documented elsewhere in this file (they are not repeated here — see
`docs/storage-adapter-contract.md`).

| Knob | Env var | YAML key | Default | What it does |
|---|---|---|---|---|
| Class definitions | — | `sensitivity.classes.<name>` | `{}` (operator entries) — the built-in `pii` class is always resolved regardless, see below | Defines a sensitivity class: `recognizers` (a list of recogniser names bound to it — code-registered, never config-defined) and `read_policy` (`access` + `audience`, below). No env override — a dict has no single scalar encoding (`resolve_storage_mapping`'s precedent). |
| Bound recognisers | — | `sensitivity.classes.<name>.recognizers` | `[]` when omitted | The recogniser names (e.g. `email`, `phone`, `street-address`, or a deployment's own code-registered one) whose matches count toward this class. **Not inherited** — only `read_policy` inherits (see below). An empty list — explicit `[]` or the key simply omitted — is honoured literally: the class is never auto-detected, reachable only by explicit operator/agent tagging. Naming a recogniser `available_recognizers()` doesn't know about raises `SensitivityConfigError` at build time. |
| Read policy | — | `sensitivity.classes.<name>.read_policy.access` | none (must resolve, directly or via `inherits`) | One of the four existing `access:` levels (`open` / `internal` / `confidential` / `personal` — the same vocabulary athenaeum#312 already ships, not a new one). An out-of-vocabulary value raises `SensitivityConfigError` rather than defaulting; so does a class whose `access` never resolves at all (no value set anywhere in its `inherits` chain). |
| Read policy audience | — | `sensitivity.classes.<name>.read_policy.audience` | `[]` when omitted | The same opaque role-list mechanism `athenaeum serve --audience` already documents (`docs/security-posture.md` §2.1). **Unvalidated** — any role name is accepted; there is no known-role vocabulary to check against. |
| Inheritance | — | `sensitivity.classes.<name>.inherits` | unset (no parent) | Names another class in the resolved config (built-in or operator-defined) whose `read_policy` this class defaults from. See "Inheritance semantics" below. |

**Built-in `pii` class, unless overridden.** With no `sensitivity:` config at
all, `pii` resolves to `recognizers: [email, phone, street-address]` (the
third, `street-address`, shipped by athenaeum#991 — a fixture-bounded
keyword+regex recogniser, not general-purpose address detection; see
`sensitivity.py`'s `_StreetAddressRecognizer` docstring and
`tests/fixtures/street_address_fixtures.py` for its exact in-scope forms and
non-goals) and `read_policy: {access: personal}`. The `read_policy` is
byte-identical to today's hardcoded `PII_ENTITY_CLASS` behaviour; the
recogniser set is not — a fresh install now detects street-address-shaped
values under `pii` that the pre-athenaeum#991 hardcoded regime never did. The
shipped default lives in `sensitivity._BUILTIN_CLASSES` (a module constant),
**not** in this module's
`_DEFAULTS` — the same "shipped default lives beside the owning domain
module, not seeded into config's defaults dict" pattern
`excluded_read_mapping` already uses, so the code default stays reachable
(issue athenaeum#187's regression is exactly what seeding here would risk). An
operator `sensitivity.classes.pii` entry **overrides the built-in wholesale**
— recognisers and read_policy alike, not merged field-by-field: an override
that omits `recognizers:` gets an *empty* list, not the built-in's two.

**Inheritance semantics (`inherits: <parent-class-name>`).** Field-default-
fill only, resolved parent-first through a chain (not required to be one
level): an unset field on the child's `read_policy` takes the parent's
already-resolved value; an explicitly set child field always wins, in either
direction. There is **no monotonic-restriction floor** — a child may resolve
*looser* than its parent (e.g. `access: open` inheriting from a `personal`
parent) if an operator genuinely configures that; nothing in this design or
`storage.mapping` enforces a ceiling (a lint over the resolved
`(read_policy, storage adapter)` pair is a possible future slice, not
shipped here).

**Partition invariant.** A recogniser name may be bound to **at most one**
class's `recognizers:` list across the whole resolved config (built-ins plus
operator classes together) — including the built-in `pii` class's own
`email`/`phone`. Binding the same recogniser name to two classes raises
`SensitivityConfigError` naming both classes and the recogniser. This makes
every detected match's destination class unambiguous by construction — see
`docs/sensitivity-class-vocabulary.md` §7 Decision D6 for the deliberate
escape hatch (two *different* recogniser names wrapping the same detection
function, each bound to a different class) and how `sensitivity.classify()`
surfaces its consequence.

**Cycle / dangling-parent errors.** Both raise `SensitivityConfigError` at
build time, never a silent fallback:

- **`inherits` cycle** — a class's `inherits` chain loops back on itself
  (`a inherits b inherits a`, or a longer chain), including a class naming
  itself (`a inherits a`). The error names every class in the cycle.
- **Dangling parent** — `inherits` names a class absent from the resolved
  config (not a built-in, not another operator entry). The error names the
  missing parent.

**Example `athenaeum.yaml`: unchanged.** The defaults need no config — a
deployment with no `sensitivity:` block behaves exactly as it does today.
The example at the end of this file is not amended for this section.

### `storage.mapping` completeness lint + the deferred pair check (athenaeum#993)

S5 of the design note's §9 — a lint over `storage.mapping`, not a new config
knob. `src/athenaeum/sensitivity_lint.py` implements two checks the note
defers rather than enforcing inside the resolver:

1. **Completeness** — every sensitivity class name a scanned corpus's
   content still carries (its own `sensitivity_class:` frontmatter field —
   this lint's own scanning convention, see the module docstring) must have
   a live `storage.mapping` entry naming a real adapter
   (`athenaeum.storage.available_adapters`). A class with no entry falls
   through, silently, to the default `wiki-markdown-embedded` surface at
   read time — the exact footgun §6 point 2 (below) names; a class mapped to
   an adapter name that does not exist already raises `StorageConfigError`
   at read time, and this lint catches both earlier, at config-change time.
2. **The deferred `(read_policy, storage adapter)` pair check** (§7
   Decision D4) — a class whose resolved `read_policy.access` is
   `confidential` or `personal` but whose mapped adapter's
   `corpus_policy.embedded` is `True` is reported as advisory: the
   read-policy layer believes the class is restricted while storage routes
   it into the ordinary embedded corpus.

Completeness findings and the D4 pair-check finding are kept as **distinct,
separately-severity kinds** — the pair check is advisory and never blocks a
gate on its own. `athenaeum storage lint-mapping --path <knowledge-root>
[--corpus <root>] [--json]` is the CLI entry point (`src/athenaeum/
_cmd_storage.py`); it exits non-zero only on a completeness finding. It is
standalone — not wired into any existing CI check by this slice. The lint
never scans a hardcoded or environment-derived path (`--corpus` always comes
from the caller, defaulting to `--path`'s knowledge root) and never writes
anything — both checks are read-only over config and the corpus tree.
Committed synthetic fixtures under `tests/fixtures/sensitivity_mapping/`
drive `tests/test_sensitivity_lint.py`. See
[`docs/sensitivity-class-vocabulary.md`](sensitivity-class-vocabulary.md) §6
point 2 / §7 Decision D4 / §9 S5 for the full rationale.

## Sensitivity routing (athenaeum#949, slice 1/4 — config)

The routing config surface specified in
[`docs/sensitivity-value-routing.md`](sensitivity-value-routing.md) §8 — a
deliberately separate axis from the "Sensitivity classes" section above.
That section defines *which classes exist*; this one decides *whether a
matched class gets intercepted at intake*, so a class can be defined without
being routed. **Slice 1 (athenaeum#1022) adds no behavior on its own** —
`resolve_sensitivity_routing` in `config.py` resolves this surface, but
nothing reads it from anywhere reachable in a running deployment yet.

| Knob | Env var | YAML key | Default | What it does |
|---|---|---|---|---|
| Routing switch | `ATHENAEUM_SENSITIVITY_ROUTING_ENABLED` | `sensitivity.routing.enabled` | `false` | Global on/off for the routing stage. `false` (default) is dark by design — byte-identical to pre-athenaeum#949 behavior. **Fails loudly** — a yaml value that isn't a boolean, or an env value that isn't `true`/`false` (case-insensitive), raises `SensitivityRoutingConfigError` rather than silently falling back. |
| Per-class action | — | `sensitivity.routing.classes.<name>.action` | `route` when the class block is present but `action` is unset | Per-class override once routing is globally enabled: `route` or `off`. Defining a class under `sensitivity.routing.classes` and turning routing on is read as "protect it" unless the operator explicitly opts the class out. An unknown action raises `SensitivityRoutingConfigError`. |

**Example `athenaeum.yaml`: unchanged.** The defaults need no config — a
deployment with no `sensitivity.routing` block behaves exactly as it does
today. The example at the end of this file is not amended for this section.

## Sensitivity routing/redaction mechanism (athenaeum#1023, slice 2/4)

`src/athenaeum/sensitivity_routing.py`'s `route_sensitive_values()`
implements the mechanism the design note's §2/§4/§6/§7 specify: scan text
via `sensitivity.classify()` (unchanged, athenaeum#910), route each
configured-on match's value to the secret vault (the built-in `excluded`
storage surface, or an operator's own safe `storage.mapping` target),
substitute a resolvable pointer for the matched span, and return the
redacted text. Wired into `librarian.process_one` by slice 4
(athenaeum#1025, see below) — see that section for the actual raw-sweep
call site and its `dry_run`/fail-closed posture. Reading a pointer's value
back is `resolve_sensitive_record`, covered in the next section (slice 3,
athenaeum#1024).

**Pointer format**, substituted for each routed span, byte-for-byte per the
design note §1:

```
[sensitive:<class>:<record_id> — value withheld; resolve via
athenaeum.sensitivity_routing.resolve_sensitive_record()]
```

`record_id` is a deterministic `uuid5` derived from the raw file's own
reference, the sensitivity class, and the match's character span — **never
from the matched value itself** — so re-scanning identical input mints the
identical id and overwrites the same vault record rather than duplicating
it (§7.1/AC11), and the id cannot be used to infer anything about the value
it names (§2/AC3, §8/AC12). See the module's docstring for the full
disposition of every criterion, including the deliberate choice **not** to
deduplicate a value across raw files: a vault record count is not a proxy
for distinct-secret count.

Fails closed (raises `SensitivityRoutingError`, built only from non-secret
metadata) on: a malformed `sensitivity.*` config; a match with no character
span (a hypothetical frontmatter-`field` recogniser — none ships today); an
operator `storage.mapping` entry routing the class onto an adapter that
participates in the corpus; or any exception writing the vault record
itself. With no explicit `storage.mapping` entry for a routed class, the
vault root resolves directly to the built-in `excluded` adapter — not the
storage layer's own "undeclared maps to wiki" default, which is correct for
every other class but unsafe here.

## Sensitivity routing record-keyed read path (athenaeum#1024, slice 3/4)

`src/athenaeum/sensitivity_routing.py`'s `resolve_sensitive_record()` is the
read half of the pointer above — the design note §2's disposition (b): a
NEW read path, keyed by `(sensitivity_class, record_id)` rather than by an
entity `uid`, because this stage runs before most raw content has a `uid`
to key on (§2 explains why the existing `pii.ExcludedRecordIndex.by_uid`
path cannot be reused here). Not called by `librarian.process_one` itself
(the write half, slice 2, is what that hook calls — see slice 4 below) —
this function's caller is whatever reads a pointer back later (an agent
resolving `[sensitive:<class>:<record_id> ...]`), not the sweep.

**Access control — no new mechanism.** Gates on the matched class's
`read_policy.access`/`audience` (athenaeum#910's vocabulary, unchanged) by
calling `athenaeum.models.is_page_authorized` — the same function the
existing uid-keyed excluded-surface read path's caller already gates
through (`mcp_server`'s Layer C: `is_page_authorized(fm, caller_audience)`,
run *after* the audience predicate, before any excluded-field join is
attempted). Both paths therefore share the one place that decision is
computed; only the policy each supplies to it differs, by design (§2/§8,
AC8: this design ships two permanently-independent read paths onto the
excluded surface, not one unified path).

**Never raises with any content in the exception — resolves to the
original value, or to nothing resolvable**, for: a malformed `record_id`
(validated against the exact 32-character lowercase-hex shape
`route_sensitive_values` mints, `uuid.UUID.hex`); a malformed or
path-traversal-shaped `sensitivity_class`/`record_id` (checked against a
strict identifier charset before any path is built, then re-checked for
containment under the resolved vault root — defense in depth, not either
check alone); an unknown class; a `record_id` with no matching record on
disk; a `record_id` that resolves under a different class than the one
requested; or a caller the class's `read_policy` does not authorize. This
matches design note §2's requirement that "resolution failure cannot be
used to probe the vault's contents" — every failure mode above is
indistinguishable from every other from the return value alone.

## Sensitivity routing wired into the librarian raw sweep (athenaeum#1025, slice 4/4)

The final slice of athenaeum#949's design note: `librarian.process_one` now
calls slice 2's `route_sensitive_values()` first thing — before Tier 0's
passthrough write and before Tier 1/2/3 read `raw.content` at all (design
note §0/§4). This is the ONE dispatch point every tier passes through, so
this single hook protects all four tiers, including both LLM exposures
(Tier 2's classify prompt, Tier 3's `raw.content[:2000]` fallback
observation). Scoped to the raw file's body only — the frontmatter block is
parsed once, untouched, and spliced back onto the redacted body, so a
routed substitution can never corrupt frontmatter.

**This is the first slice in the series with an actual runtime behavior
change** — gated entirely behind `sensitivity.routing.enabled` (§ above,
dark by default per athenaeum#1022). With routing unset or off,
`process_one`'s output is byte-for-byte unchanged from pre-athenaeum#949
behavior.

**Skipped under `--dry-run`**, matching every other side-effecting Tier-0
step in `process_one` (`tier0_passthrough`/`tier0_handle_upsert`/
`tier0_bounce_mark` all take a `dry_run` flag and avoid writes) — a vault
record is itself a disk write a preview run must not make, and dry-run's
own early return before Tier 2/3 means no LLM call is at risk from
skipping this either way.

**Fail-closed at this integration level too**: `SensitivityRoutingError`
propagates out of `process_one` uncaught, into the entity-tier sweep loop's
existing generic exception handler — unmodified by this slice — which
already leaves the raw file untouched on disk and writes no wiki page for
it (§6/AC10).

## Erasure classification and retention packs (athenaeum#985)

The classification and taint-propagation layer for erasure-class content
(`athenaeum.erasure`, split (c) of athenaeum#718's re-scope — see
`docs/whole-store-adapter-design.md` §8). **No production caller is
migrated onto this module in this slice** — it ships fully implemented and
fully tested, mirroring the precedent `docs/configuration.md`'s own
"Sensitivity classes" section above documents for `athenaeum.sensitivity`'s
S1a/S1b ("no caller was migrated... that was slice S3"). With every key
below at its default, nothing in the librarian pipeline or `remember()`
write path changes behavior — these keys govern only the (currently
unwired) classification helpers themselves.

| Knob | Env var | YAML key | Default | What it does |
|---|---|---|---|---|
| Active retention pack | `ATHENAEUM_RETENTION_PACK` | `erasure.retention_pack` | `us-default` | Selects which retention pack (of the two packaged packs, plus any operator-defined ones below) governs `athenaeum.erasure.classify_retention`. Unset/empty at either tier falls through to the next. |
| Retention pack overrides/additions | — | `erasure.retention_packs.<name>` | `{}` (packaged packs only) | Operator-authored pack definitions. Reusing a packaged name (`us-default`, `eu-gdpr`) **overrides it wholesale** (no field-level merge — an override that omits `rules:` gets an empty list, not the packaged pack's table), the same "config wins, wholesale" rule the Sensitivity classes section above documents. A new name adds a new pack. No env override — a pack definition has no single scalar encoding. |
| Erasure HMAC key location | — | — (always `<cache_dir>/_erasure_hmac_key`) | machine-local, under the resolved cache dir | Not independently configurable — resolves through the existing `resolve_cache_dir` precedence (`arg > ATHENAEUM_CACHE_DIR env > ~/.cache/athenaeum`), same as every other cache-dir ledger/key in this repo. See "Reversible defaults taken" in the athenaeum#985 PR for why this key is deliberately machine-local rather than store-durable. |

**Retention-pack shape.** Each pack is YAML data (`src/athenaeum/retention_packs/us-default.yaml`,
`.../eu-gdpr.yaml`), never a Python literal — "packs are data, not code" is
athenaeum#985's own AC9 wording. A pack has a `default_action` (+ optional
`default_period`) and a `rules:` list of `{memory_class, data_class,
jurisdiction, action, period?}` entries. `action` is one of `refuse-write |
store-off-corpus | demote-cold | delete-after | retain-until`; `period`
(an opaque string, e.g. `"P7Y"`) is required for the last two, unused by
the first three. `data_class` names a sensitivity class (`docs/sensitivity-class-vocabulary.md`
— `pii` is the only one athenaeum ships); `memory_class` names an
`athenaeum.memory_class.MEMORY_CLASSES` value.

**Unknown jurisdiction is always erasure-class — no pack can loosen this.**
`athenaeum.erasure.classify_retention` enforces AC3's conservative default
(a data subject whose jurisdiction is unknown at write time is erasure-class)
BEFORE any pack lookup runs — a pack defining a `jurisdiction: unknown`
row of its own is never reached for that case. Neither packaged pack
defines one, for the same reason. Where the data subject's jurisdiction IS
known, a pack keys on the **subject's** jurisdiction, not only the
operator's deployment jurisdiction — `docs/configuration.md`'s two packaged
examples both carry both a `jurisdiction: us` and a `jurisdiction: eu` row
for exactly this reason.

**Reconciling with the decay-bucket mapping (issue athenaeum#969,
`docs/provenance-shape.md` §8.8).** That doc commits `bucket: daily` to
compile to a `delete-after <period>` rule keyed by `(memory_class,
data_class)`, once a pack exists for that key. `athenaeum.erasure.reconcile_bucket_daily_with_pack`
computes that mapping now that a pack exists, but — per that function's own
docstring — this athenaeum#985 PR does **not** wire it into
`athenaeum.decay_sweep`'s live sweep behavior; `decay_sweep.py` is
unmodified and imports nothing from `athenaeum.erasure`.

**Example `athenaeum.yaml`: unchanged.** The defaults need no config — a
deployment with no `erasure:` block behaves exactly as it does today
(nothing calls these functions on any write/read path yet). The example at
the end of this file is not amended for this section.

## Authority manifest (athenaeum#426)

| Knob | Env var | YAML key | Default | What it does |
|---|---|---|---|---|
| Authority manifest path | `ATHENAEUM_AUTHORITY_MANIFEST` | `librarian.authority_manifest_path` | `<knowledge_root>/authority-manifest.yaml` | Path to the authority manifest mapping authoritative LIVE sources. Relative yaml values resolve against the knowledge root; a missing file is treated as "no manifest configured". Full reference: [`docs/authority-manifest.md`](authority-manifest.md). |

### Never-ingest classes (athenaeum#968)

The authority manifest also carries an optional `never_ingest_classes:` list
— write-refusal classes BOTH intake paths (auto-memory and entity tier)
consult, extending the manifest mechanism above rather than adding a second
config surface. Empty or absent by default (a manifest written before
athenaeum#968, or one that never mentions the key, enforces nothing new). Two
classes are recognised:

- `mirror-of-live-source` — reuses the manifest's own topic-index lookup
  (the same one `authority lint` uses) to refuse an intake file whose
  `topics`/`tags`/`name` names a topic a `sources:` entry already owns.
- `pending-state-todo` — refuses an intake file that asserts the current
  presence/absence of something in an external artifact (an explicit
  `pending_state: true` flag, or a phrase like "has it been added" / "still
  needs" / "todo:").

Enforced at each tier's own COMPILE choke point, never at discovery:
auto-memory in `_run_auto_memory_phase` (`src/athenaeum/librarian.py`), right
after `discover_auto_memory_files` and before clustering; entity tier in
`process_one`, right after each raw file's frontmatter is parsed and before
Tier 0 passthrough. Deliberately never inside
`athenaeum.intake.discover_raw_files` itself — that function's return value
is read directly, unmodified, by `backlog_price_sheet.py` /
`ordinary_night_table.py` (issue athenaeum#713's backlog-count instruments, held
pending an operator decision), so gating at discovery would silently move
those numbers. A refused file (either tier) is excluded from that run's
compilation and appended, ids-only, to
`<cache_dir>/_never_ingest_refusals.jsonl` (never deleted from disk; it is
re-evaluated on the next run). See `athenaeum.never_ingest` and
[`docs/authority-manifest.md`](authority-manifest.md#never-ingest-classes-athenaeum968)
for the full mechanism.

## Usage report (athenaeum#968)

A per-claim usage signal computed from the push-metrics ledgers
(`athenaeum#711`/`athenaeum#734`): how many times a pushed id has been pushed, how
many times it was actually referenced afterward, and when it was last
referenced. Purely a REPORT — it makes no tier-movement decision itself.

```
athenaeum usage-report [--claim-id ID] [--since 7d] [--json]
```

ids-only output (same redaction discipline as `push-metrics`): every field
is an id, a count, or a timestamp, never claim content.

**The interface issue athenaeum#718's tier-movement rules must consume** —
`athenaeum.usage_report.get_claim_usage(claim_id, cache_dir=...)` (single
claim) or `compute_usage_report(cache_dir=...)` (bulk). Neither reads
`_push_records.jsonl`/`_push_references.jsonl` directly, and no other module
should either — this is the one seam through which usage data crosses to a
tier-movement consumer.

## Ingestion gate (athenaeum#968) — off by default

| Knob | Env var | YAML key | Default | What it does |
|---|---|---|---|---|
| Ingestion gate enabled | `ATHENAEUM_INGESTION_GATE_ENABLED` | `librarian.ingestion_gate_enabled` | `false` | When `true`, `_run_auto_memory_phase` checks push-metrics precision instrumentation before compiling any auto-memory intake this run — see below for "healthy". |

"Healthy" is a **liveness** check, not a quality bar: push-metrics
instrumentation is enabled (`push_metrics.enabled`) AND at least one
reference-determination record exists in the ledger ever (precision is
therefore computable, whatever its value). A fresh install with the gate
turned on and zero sessions run yet reads as unhealthy until its first
session completes — intentional and self-healing, not a bug; the gate stays
off until an operator opts in. When unhealthy, the WHOLE auto-memory
compilation phase is skipped for that run (no partial-volume throttle is
implemented — the issue names no threshold to throttle against); nothing on
disk is touched, and the same raw intake is re-evaluated next run. See
`athenaeum.ingestion_gate`.

```yaml
librarian:
  ingestion_gate_enabled: false   # off by default; see docs/configuration.md
```

## Reasoning-tier screening (T1/T2) — off by default

A cheap-to-expensive cascade (`src/athenaeum/reasoning_tiers.py`, issues
athenaeum#423/#432) that screens each merge proposal *before* it reaches the
human decision queue (`wiki/_pending_merges.md`). The governing rule: **write
authority increases with tier — cheap tiers only reject and route, never
approve.**

- **T1** — a cheap (haiku-class) model, given only a *bounded* view of each
  candidate source: title, frontmatter, and the first ~100 words of the body
  — never the full text. T1 can only **reject** (with a logged reason) or
  **pass up**; approval is not a representable outcome for T1 at all, at the
  type level. A confident reject drops the proposal before a human — or
  T2 — ever sees it.
- **T2** — an expensive (opus-class) model, consulted only on a T1 pass-up,
  and given each source's **full body**. T2's decision space is broader
  (approve / amend / draft / escalate) — and unlike T1, **T2 can auto-apply a
  merge**: an `approve` verdict inside the *safe class* finalizes the merge
  directly via `pending_merges.resolve_merge(..., auto_applied=True)`
  (`src/athenaeum/merge.py:1408`) — **without human review**. The safe class
  is ALL of: every source shares the same `memory_class`, at most 3 source
  pages, no source carries a truthy `pii` flag, and no source is a
  `memory_class: axiom` member (`reasoning_tiers.safe_class_violation`). Any
  violation — or a model response that pairs `approve` with rewritten
  content — makes the safe-class approval structurally unreachable; the
  decision downgrades to `escalate`/`draft` and falls through to the human
  queue instead, regardless of what the model itself returned.

> **This is why the default is off.** Turning this subsystem on means
> athenaeum can write merges into your wiki without a human ever looking at
> them. That is a reasonable trade for an operator who is drowning in the
> merge queue and opts in with open eyes — it is not something an
> Apache-2.0 package should do to every installer by default. Everything
> below is opt-in.

**When to turn it on.** The recommended trigger is a human merge queue that
has grown beyond what you can triage by hand. The concrete signal to watch
is `athenaeum merges count` (or `athenaeum decisions count` for the unified
question+merge view) — both report the live depth of
`wiki/_pending_merges.md`. (Don't hand-parse that file directly — see
["Never hand-parse `wiki/_pending_merges.md`"](../README.md#one-unified-decisions-needed-list)
in the README.) There is no built-in numeric threshold; "beyond what you can
handle" is an operator judgment call, not a code-enforced ceiling.

**How to enable.** One flag gates *both* tiers — there is no separate
opt-in for T1 vs. T2:

| Knob | Env var | YAML key | Default | What it does |
|---|---|---|---|---|
| Reasoning-tier auditing | `ATHENAEUM_REASONING_TIER_AUDITING_ENABLED` | `librarian.reasoning_tier_auditing_enabled` | `false` (**off**) | Gates the T1 screen in the merge path, the T2 screen it can pass up to, and the `athenaeum calibration` display surface, all together. `1`/`true`/`yes`/`on` (case-insensitive) enable via env; a non-bool yaml value or an unrecognized env string falls through to off. See [`resolve_reasoning_tier_auditing_enabled`](../src/athenaeum/config.py). |

```yaml
librarian:
  reasoning_tier_auditing_enabled: true
```

The T1/T2 *model* and per-stage token/thinking knobs (`ATHENAEUM_REASONING_T1_MODEL` /
`ATHENAEUM_REASONING_T2_MODEL` and friends, see [Models](#models) and
[Per-stage token and thinking tuning](#per-stage-token-and-thinking-tuning-athenaeum688)
above) are read regardless of this flag, but have no runtime effect while it
is off — there is nothing for them to tune until the screen actually runs.

**What it costs.** Enabling this adds LLM calls on a merge path that
currently makes none: every T1 pass-up call and every T2 escalation call is
real spend, landing under the `reasoning_t1` / `reasoning_t2` model knobs.
Use `athenaeum spend --by-knob` to see it broken out — that bucket is keyed
by **knob name** (`reasoning_t1` / `reasoning_t2`, alongside `classify` /
`write` / `resolve` / `topic`), so the two reasoning-tier stages stay
distinguishable even when a knob resolves to the same model id as another
stage (e.g. the shared haiku default) — `--by-model` cannot separate that
case since it keys on model id, not knob name.

**How to observe it.** Every tier decision — at either tier, whatever the
verdict — is appended to `wiki/_reasoning_tier_decisions.jsonl` (append-only
JSONL, same `O_APPEND` + fsync durability as the merge-provenance ledger) as
one record per decision: `tier`, `decision`, `reason`, `reason_code`,
`model`, and `proposal_id`, plus an ISO-8601 UTC timestamp
(`reasoning_tiers._build_log_record_fields`). Read it back with
`reasoning_tiers.read_reasoning_tier_decisions()`, optionally filtered by
`proposal_id` or `tier`.

## Verdict ledger (athenaeum#712) — off by default

An append-only record of pairwise comparison verdicts
(`duplicate | contradiction | specialization | distinct | underdetermined`),
each carrying the exact set of facts — the **basis** — it was justified by
(content hashes, coordinates, epochs, authority), so a change to any one of
those facts can invalidate exactly the verdicts that depended on it. Ships
in `src/athenaeum/verdicts.py`, ahead of the five-verdict comparator that
will populate it (a separate, future child of the memory-model v6 epic,
athenaeum#709) — the comparator's invalidation story depends on the basis
being right from entry one.

**Wiring decision (athenaeum#712 AC).** The comparator does not exist yet, so
this ships dark behind one flag defaulting off. With the flag on, two real
integration points fire — this is not a schema nothing writes to:

- `athenaeum ingest-answers` — a merge **approve**/**reject** decision
  (`athenaeum.decision_answers._apply_merge_answer`) records a
  `duplicate`/`distinct` verdict for the resolved pair, using the same
  sources the pending-merge proposal already named. This is "consumed
  within this issue by writing verdicts for the decisions the current
  pipeline already makes."
- `athenaeum run`'s finalize phase materializes the ledger directory and
  epoch registry (if absent) and advances the per-branch duty-cycle
  counters one night — so a live run genuinely produces a well-formed,
  queryable ledger even before the comparator exists to populate it with
  real content.

| Knob | Env var | YAML key | Default | What it does |
|---|---|---|---|---|
| Verdict ledger | `ATHENAEUM_VERDICT_LEDGER_ENABLED` | `librarian.verdict_ledger_enabled` | `false` (**off**) | Gates the whole subsystem: the `ingest-answers` merge-decision recorder, the `run` finalize advisor, and materializing `wiki/_verdicts/` at all. See [`resolve_verdict_ledger_enabled`](../src/athenaeum/config.py). |
| Epoch batch interval | `ATHENAEUM_VERDICT_EPOCH_BATCH_INTERVAL_DAYS` | `librarian.verdict_epoch_batch_interval_days` | `30` | Comparator-epoch bumps are batched on this interval (days) — passed to `verdicts.open_epoch`'s `batch_interval_days`. |

```yaml
librarian:
  verdict_ledger_enabled: true
  verdict_epoch_batch_interval_days: 30
```

**With the flag off:** `athenaeum run` is byte-identical to before athenaeum#712
— no file appears under `wiki/_verdicts/`, no new run-summary phase, no
exit-code change. `athenaeum ingest-answers` resolves merges exactly as
before; nothing records a verdict.

**Store layout**, under `wiki/_verdicts/`:

- `<YYYY-MM>.jsonl` — one **live** monthly partition, keyed by the month a
  pair's currently-live verdict was decided. Append-only, `O_APPEND` +
  fsync, same durability discipline as `_merge_provenance.jsonl`.
- `_verdicts_history.jsonl` — verdicts superseded by a newer decision for
  the same pair, moved out by `verdicts.compact()`. Invalidation waves scan
  the live partitions only.
- `_verdicts_epochs.json` — the per-branch comparator-epoch registry:
  current version, whether a re-comparison wave is open, and the
  nights-in-wave / nights-total duty-cycle counters (target <=25%,
  reporting only — enforcing the target is out of scope).

**Single-appender.** Every mutating `verdicts.py` function reuses
`athenaeum.runlock.RunLock` rather than a second lock — it takes a required
`lock:` argument and requires it already acquired (raises `LockNotHeld`
otherwise); it never acquires the lock itself. Both integration points above
thread through the SAME lock the CLI command already holds.

**How to inspect it.** `athenaeum verdicts {count,list-by-verdict,show-one-pair,show-stale}`
is the sanctioned read path (mirrors `athenaeum merges`) — hand-parsing
`wiki/_verdicts/*.jsonl` directly is not supported.

## Dimension registry (athenaeum#714)

Root of the memory-model v6 dimension chain (child of epic athenaeum#709;
athenaeum#715/athenaeum#716/athenaeum#719 depend on this). A **dimension** is a
declared, typed axis a claim takes a coordinate on. Two claims' coordinates on
one axis compare to exactly one of `equal | contains | overlaps | disjoint |
unknown` (`src/athenaeum/dimensions.py`). Athenaeum ships **six kernel
dimensions unconditionally** — built in, not deletable, need no config:

| Dimension | Kind | `null_means` | `separates` | Bound to |
|---|---|---|---|---|
| `recorded-time` | `interval` | `unknown` | `false` (precedence-only) | New `recorded_at` frontmatter key; system transaction time, stamped once at first `WikiEntity` construction — never writer-supplied. |
| `observed-time` | `interval` | `unknown` | `false` (sequencer) | Existing `observed_at` (athenaeum#424). |
| `valid-time` | `interval` | `universal` | `true` | Existing `valid_from` / `valid_until` (athenaeum#308). |
| `scope` | `hierarchy` | `universal` | `true` | New `claimed_scope` frontmatter key — see "Ambiguity resolved" below for why not the existing `scope:` key. |
| `subject` | `identity` | `unknown` | `true` | New `subject` frontmatter key. |
| `memory-class` | `enum` | `unknown` | `true` | Existing `memory_class` (athenaeum#424/athenaeum#996). Ships at `state: backfill`, not `enforced` — see the athenaeum#972 disposition comment on athenaeum#714. |

**Comparators.** `interval` is **half-open `[from, until)`** — abutting
windows ("2020–2022" then "2022–") compare `disjoint`, not a zero-width
`overlaps` (a known failure mode of the collapsed/inclusive algebra used
elsewhere in this repo; the on-disk `valid_until` field stays INCLUSIVE and
unchanged for every other reader — the half-open conversion happens only at
the dimension-registry coordinate boundary, `dimensions.parsed_coordinate`).
`hierarchy` is prefix-subsumption (`kromatic` `contains` `kromatic/platform`;
siblings `disjoint`). `enum` is closed-vocabulary same/different. `identity`
returns `disjoint` **only when the caller passes `ratified=True`** (distinct
uids each backed by human confirmation, independent provenance, or a prior
ledgered verdict) — there is no confidence/threshold parameter; an unratified
mismatch is `unknown`, never a false separation.

**Null semantics are per-dimension.** `null_means: universal` — an absent
coordinate asserts "applies across this whole axis"; the comparator treats it
as containing any value. `null_means: unknown` — absence means "not
captured"; the comparator returns `unknown`. Two claims BOTH null on a
dimension are never separable by it (`unknown`) regardless of `null_means`. A
claim may write the literal `*` to explicitly assert universal, independent
of the dimension's configured `null_means`.

**`separates` vs. sequencer.** Separators (`valid-time`, `scope`, `subject`,
`memory-class`) partition territory — a `disjoint` relation there means the
two claims cannot conflict. Sequencers (`recorded-time`, `observed-time`)
order beliefs about ONE territory and never separate, even on `disjoint` —
they feed supersession ordering instead (`dimensions.can_separate`).

**`applies_to` bounds blast radius.** A selector (e.g.
`{memory_class: [entity]}`) determining which claims carry the axis at all —
a dimension outside its `applies_to` on either side of a comparison is simply
not consulted (`unknown`), so a CRM-only axis never touches dev-rig pages.

**Lifecycle: `backfill -> enforced`.** In `backfill`, a dimension is
consulted only for pairs where BOTH sides already carry a coordinate.
`dimensions.maybe_flip_to_enforced` flips a dimension to `enforced` once
coverage crosses `coverage_threshold`, wired to athenaeum#712's targeted
stale-marking (`verdicts.select_stale_for_dimension_change` +
`verdicts.mark_pairs_stale`) so affected ledger entries are marked stale in
the same transition. Retiring a dimension re-nulls its coordinates
(`dimensions.retire_dimension_coordinate`) rather than deleting them —
coordinates are additive metadata.

**Corpus namespacing.** `org:maturity` and `personal:maturity` are different
axes unless a ratified mapping declares a translation
(`dimensions.cross_corpus_compare`); an unmapped or kind-mismatched pair
degrades to `unknown`, never a false `disjoint`/`equal` from colliding
vocabularies that happen to share a name or value set.

**Write-side discipline (the highest-risk item this issue names).** Origin
scope is PROVENANCE (`WikiEntity.provenance_scope` — where/what context wrote
the claim, free the way `source`/`source_type` are; named `provenance_scope`,
not `origin_scope`, to avoid colliding with `AutoMemoryFile.origin_scope`, an
unrelated pre-existing field — see "Ambiguity resolved" below). Claimed scope
is an ASSERTED coordinate (`WikiEntity.claimed_scope` — where the claim
APPLIES). They are never the same field and provenance is never auto-copied
into the coordinate — guarded by a regression test
(`tests/test_dimensions.py::TestProvenanceScopeNeverPopulatesClaimedScope`). A
claim missing a coordinate is not rejected; it lands per the dimension's null
semantics.

**Ambiguity resolved (dispatch aperture, reversible).** The `scope` dimension
reads/writes a NEW `claimed_scope` frontmatter key rather than the existing
`scope:` key (`schemas.WikiBase.scope`, athenaeum#434). `scope:` is already
read as an incompatible nested `{org, locale}` shape by two live consumers
(`athenaeum.scoped_claims`, athenaeum#329, and `athenaeum.contradictions`'
scope-block advisory line) — stacking a third, string-shaped reader onto the
same key would compound an existing collision rather than "match existing
frontmatter conventions." `claimed_scope` is zero-collision, additive, and
trivially reversible: retargeting the reader/writer to a different key is a
one-line change (`dimensions.coordinate_value` / `parsed_coordinate`,
`models.WikiEntity`'s field), with no data migration since coordinates are
additive metadata.

The provenance field went through the same check for a different reason: an
early draft named it `origin_scope`, which collides with a real, heavily-used
PRE-EXISTING field — `AutoMemoryFile.origin_scope` (issue athenaeum#167, the
raw-intake scope-directory identifier consumed across `merge.py`,
`cross_scope.py`, `clusters.py`, `resolutions.py`, `tiers.py`, and more) —
that `resolutions.py` documents as "NEVER stored in frontmatter." Storing a
NEW frontmatter key with that exact identifier would have directly
contradicted that documented invariant for anyone grepping the name, even
though the two fields live on different dataclasses and never share data at
runtime. Renamed to `provenance_scope` before this landed — verified
zero-collision the same way as `claimed_scope`/`subject`/`recorded_at`.

**Intake temporal validation** (`dimensions.validate_intake_temporal`, wired
into `schemas.WikiBase`'s model validator — the same choke point every
intake path already validates frontmatter through): hard-rejects
`observed_at` later than `recorded_at` ("cannot have observed the future");
soft-flags (`UserWarning`) an `observed_at` more than
`DEEP_BACKDATE_THRESHOLD_DAYS` (730, i.e. 2 years) before `recorded_at` for
review — the value is kept (fail-open, matching every other temporal parser
in this repo).

| Knob | Env var | YAML key | Default | What it does |
|---|---|---|---|---|
| Deployment dimensions | — | `dimensions` | _(unset = kernel-only)_ | A list of ADDITIONAL, deployment-declared dimension entries (see field reference below). No env var — this is a structural block, not a scalar knob. See [`resolve_dimensions`](../src/athenaeum/config.py). |
| Registry epoch | `ATHENAEUM_DIMENSION_REGISTRY_EPOCH` | `librarian.dimensions_registry_epoch` | `1` | Bump on a dimension-definition change that should invalidate verdicts justified by the old definition — feeds `verdicts.Basis.registry_epoch`. |
| Tree epoch | `ATHENAEUM_DIMENSION_TREE_EPOCH` | `librarian.dimensions_tree_epoch` | `1` | Bump on a scope-tree reorg (renamed subtree) — feeds `verdicts.Basis.tree_epoch` and athenaeum#712's `select_stale_for_tree_epoch_bump`. |

**`dimensions:` entry field reference** (each entry validated by
`dimensions.parse_dimension_entry`; a malformed entry raises
`DimensionRegistryError` naming the field):

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | kebab-case, unique, must not collide with a kernel dimension name. |
| `kind` | string | yes | one of `interval \| hierarchy \| enum \| identity`. |
| `null_means` | string | no (default `unknown`) | `universal \| unknown`. |
| `values` | list of strings | required when `kind: enum` | closed vocabulary. |
| `separates` | bool | no (default `true`) | separator vs. sequencer. |
| `applies_to` | mapping | no (default `{}` = applies everywhere) | `{frontmatter_key: value_or_list}`, conjunctive. |
| `state` | string | no (default `enforced`) | `backfill \| enforced`. |
| `origin` | string | no (default `operator`) | `builtin \| operator \| proposed:<id>`. |
| `since` | ISO date | no | when the dimension was declared. |
| `coverage_threshold` | number | no (default `1.0`) | consulted only while `state: backfill`. |

```yaml
dimensions:
  - name: engagement          # kebab-case, unique
    kind: identity            # interval | hierarchy | enum | identity
    null_means: unknown       # universal | unknown
    separates: true           # separator vs sequencer
    applies_to:                # selector bounding which claims carry this axis
      memory_class: [entity]
    state: backfill            # backfill | enforced
    origin: operator           # builtin | operator | proposed:<id>
    since: 2026-08-01
librarian:
  dimensions_registry_epoch: 1
  dimensions_tree_epoch: 1
```

**Behavior with no `dimensions:` key set.** `athenaeum run` completes
unchanged — the kernel-only registry participates in nothing the librarian
pipeline runs today beyond `WikiEntity` construction stamping `recorded_at`
(a new, additive frontmatter key on newly-written pages) and the intake
temporal check above (fail-open; a no-op unless `observed_at` is genuinely
set to a future date or a deep back-date). `engagement`, `repo`, `maturity`,
`environment` and similar are deployment-declared EXAMPLES, never shipped
defaults — time is universal, engagements are a CRM's business.

**How to inspect it.** `athenaeum dimensions show <page.md>` prints one
page's coordinates across every registered dimension; `athenaeum dimensions
compare <page-a.md> <page-b.md>` prints the axis-by-axis relation for a pair.
Both accept `--json`. This is the registry's consumer for this issue — the
five-verdict comparator that will consume the full algebra automatically is
a separate, future child of epic athenaeum#709.

## Recall and search

| Knob | CLI flag | Env var | YAML key | Default | What it does |
|---|---|---|---|---|---|
| Auto-recall | — | `AUTO_RECALL` (hook shell env) | `auto_recall` | `true` | Per-turn recall via the UserPromptSubmit hook. The shell env is read by the example hooks and beats the yaml. |
| Search backend | `--backend` (`recall` / `rebuild-index`) | `SEARCH_BACKEND` (hook shell env) | `search_backend` | `fts5` | `fts5` (SQLite FTS5, BM25 + porter stemming) or `vector` (chromadb + `all-MiniLM-L6-v2`, needs `pip install athenaeum[vector]`). `athenaeum recall --backend keyword` additionally exposes the zero-dependency scan-on-query fallback. |
| Extra intake roots | — | — | `recall.extra_intake_roots` | `["raw/auto-memory"]` | Additional directories (relative to the knowledge root) scanned recursively into the recall index. Set `[]` to restrict recall to the compiled wiki. |
| Recall result count | `--top-k` (`recall`) | — | — | `5` | Hits returned by the shell `recall` command. |
| Index cache dir | `--cache-dir` (`recall` / `rebuild-index`) | — | — | `~/.cache/athenaeum` | Where the FTS5 db / chromadb collection live. |
| Read-scope audience (athenaeum#312) | `--audience` (`serve` / `recall`) | `ATHENAEUM_AUDIENCE` | `serve.audience` | _(unset = owner, full access)_ | Pins the `serve`/`recall` process to a RESTRICTED read scope: comma-separated (or yaml-list) opaque role/group ids the operator maps onto an external RBAC (AD group, app role, routine name). A restricted caller receives a page only when it is `access: open` OR its `audience:` list grants one of these roles; untagged / `confidential` / `personal` pages are withheld (fail-closed). The audience is pinned by the operator here — it is NOT a `recall()` tool argument, so a restricted agent can't widen its own scope. Empty/unset = owner = every page. |
| Topic-extraction timeout | `--timeout` (`query-topics`) | — | — | `3.0` | Seconds before `query-topics` gives up and the hook falls back to the regex extractor. |
| Topic-extraction config root | `--knowledge-root` / `--path` (`query-topics`) | — | — | `~/knowledge` | Knowledge root whose `athenaeum.yaml` supplies `models.topic` (athenaeum#232). |

**Reserved keys (not yet read by code).** `vector.provider` (default
`chromadb`) and `vector.collection` (default `wiki`) appear in the loader's
`_DEFAULTS` seed but no code reads them yet — the vector backend hardcodes
chromadb and the `wiki` collection name. Setting either key has no effect
today.

**Ambient telemetry variable.** `CLAUDE_CODE_SESSION_ID` (the variable Claude
Code actually exports) is read by `athenaeum.query_topics` and the recall
push-record path and stamped onto recall telemetry so recall activity is
session-keyed; the older `CLAUDE_SESSION_ID` is accepted as a fallback
(athenaeum#734). It is an **ambient / host-provided** variable (Claude Code
sets it), not an operator knob — you do not set it yourself; it is documented
here only so a reader knows recall telemetry carries a session id. Both names
resolve through the single `push_metrics.resolve_session_id()` helper, so the
name is defined in exactly one place.

## Kill switch (`athenaeum disable` / `enable`, athenaeum#379)

One discoverable, reversible way to stop all athenaeum background work — no
hand-editing of `~/.claude/settings.json` and no `pkill`. Every entry point
(the `session-end` compile pass, the MCP write tools, and the example shell
hooks) checks the same state before doing anything.

| Action | Effect |
|---|---|
| `athenaeum disable` | Turns **everything** off — compile, contradiction detection, recall, notifications (scope `all`). |
| `athenaeum disable --compile` | Granular: stops only the expensive compile/detect pass; recall stays on (scope `compile`). |
| `athenaeum disable --reason "..."` | Records a note shown by `athenaeum status`. |
| `athenaeum enable` | Removes the state file and restores prior behaviour exactly. |
| `athenaeum status` | Reports on/off, scope, and reason (in addition to the knowledge-base summary). |

**State file.** The state lives at `$ATHENAEUM_CACHE_DIR/disabled` (default
`~/.cache/athenaeum/disabled`; `--cache-dir` overrides it on the kill-switch
commands). Its mere presence means "disabled at scope `all`" unless the file
says `compile` — so an emergency `touch ~/.cache/athenaeum/disabled` is a valid
full-off, and `rm` re-enables. The shell hooks read this file directly with
`grep`, so the per-turn recall path pays no Python startup.

**Env override.** `ATHENAEUM_DISABLED` beats the file: `1` / `true` / `yes` /
`on` / `all` force scope `all`; `compile` forces scope `compile`; `0` / `false`
/ `off` / unset defer to the file (an explicit `0` does **not** force-enable
past a state file). `athenaeum enable` warns when the env is still forcing it
off.

| Scope | `session-end` compile / detectors | Recall hooks + MCP writes | Notifications |
|---|---|---|---|
| _(enabled)_ | on | on | on |
| `compile` | **off** | on | on |
| `all` | **off** | **off** | **off** |

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
| `ATHENAEUM_DISABLED` | _(unset)_ | Kill switch (athenaeum#379) — `all`/`1`/`true` no-ops every hook; `compile` stops only the compile pass. Beats the `disabled` state file. See [Kill switch](#kill-switch-athenaeum-disable--enable-379). |
| `ATHENAEUM_CACHE_DIR` | `~/.cache/athenaeum` | Cache dir the hooks look in for the kill-switch `disabled` state file. |

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

push_metrics:
  enabled: true    # on by default; passive push-precision/coverage measurement (athenaeum#711)

librarian:
  cluster_threshold: 0.55
  cluster_output: raw/_librarian-clusters.jsonl
  rotation_retention: 30        # timestamped rotations to keep; 0 = keep all (athenaeum#311)
  max_files: 50
  max_api_calls: 800
  max_runtime: 3600             # run-level wall-clock deadline in seconds; <= 0 disables (athenaeum#396)
  entity_runtime_share: 0.6     # entity phase's share of max_runtime; rest reserved for C4 (athenaeum#440)
  intake_runtime_floor: 0.0     # 0.0 = OFF; minimum max_runtime share reserved for intake/C4 (athenaeum#1102)
  stuck_file_threshold: 3       # consecutive-failure count before a raw file is skipped as stuck (athenaeum#663)
  raw_file_max_bytes: 5242880          # per-raw-file byte bound; 5 MiB (athenaeum#898)
  raw_file_max_api_calls: 60           # per-raw-file LLM-call bound (athenaeum#898, recalibrated athenaeum#994)
  raw_file_max_runtime_seconds: 900    # per-raw-file wall-clock bound in seconds (athenaeum#898, recalibrated athenaeum#994)
  quarantine_threshold: 2              # consecutive bound-violations before quarantine (athenaeum#898)
  junk_match_stopwords: []      # extra entity names filtered before a tier-3 merge call (athenaeum#662)
  junk_match_allowlist: []      # entity names to never treat as junk — escape hatch (athenaeum#662)
  exclude_code_artifacts: true  # refuse entity creation from filename/path names (athenaeum#680)
  code_artifact_extensions: []  # extra source/config extensions counted as code artifacts (athenaeum#680)
  code_artifact_allowlist: []   # entity names to never treat as code artifacts — escape hatch (athenaeum#680)
  batch_mode: false
  non_intake_sources: []        # raw/<source>/ dirs excluded from entity intake whole (athenaeum#843)
  ephemeral_scopes: []          # scope globs dropped as ephemeral intake (athenaeum#280)
  operational_markers: []       # >=2 lower-cased substrings => ephemeral (athenaeum#280)
  min_cluster_cohesion: 0.0     # 0.0 = OFF; cohesion floor (athenaeum#281)
  min_cluster_cohesion_scopes: 4  # scope-span gate for the cohesion floor (athenaeum#281)
  max_merge_sources: 5          # cap on resolver merge-proposal sources; 0 = OFF (athenaeum#400); tightened from 25 (athenaeum#421)
  min_merge_confidence: 0.0     # 0.0 = OFF; merge-proposal confidence floor (athenaeum#400)
  lock_timeout: 0               # run-lock wait seconds; 0 = fail-fast (athenaeum#309)
  page_warn_bytes: 8192         # warn on wiki pages over this size (athenaeum#310)
  page_flag_bytes: 16384        # flag pages over this size for splitting (athenaeum#310)
  drain_warn_days: 3            # backlog-drain ETA WARNING threshold in days (athenaeum#470)
  merge_body_preview_chars: 2000            # list_pending_merges draft_merged_body preview cap (athenaeum#431)
  decisions_max_sources_per_merge: 20       # decisions-view per-merge source fan-out cap (athenaeum#431)
  audit_sample_rate_t2_approvals: 0.075     # share of T2 approvals sampled for human audit (athenaeum#438)
  audit_sample_rate_t1_rejects: 0.075       # share of T1 rejects sampled for human audit (athenaeum#438)
  verdict_ledger_enabled: false              # off by default; verdict ledger + basis (athenaeum#712)
  verdict_epoch_batch_interval_days: 30      # comparator-epoch bump batching interval (athenaeum#712)
  ingestion_gate_enabled: false               # off by default; skip auto-memory phase if push-metrics precision is unhealthy (athenaeum#968)
  delta:
    enabled: true               # delta-scoped incremental compile on client=None path (athenaeum#370)
    max_affected_clusters: 8    # > this many clusters touched => full compile (athenaeum#370)
    max_affected_members: 200   # > this many pooled members => full compile (athenaeum#370)
  reindex:
    full_rehash_max_age_days: 7 # periodic full re-hash backstop; 0 = always re-hash (athenaeum#373)
  reasoning_triggers:           # `athenaeum ingest --if-triggered` (athenaeum#909); all unset = backstop-only
    backlog_files: 25           # unset = OFF; backlog-depth trigger by file count
    backlog_bytes: 5242880      # unset = OFF; backlog-depth trigger by byte size (5 MiB)
    interval_hours: 6           # unset = OFF; elapsed-interval trigger
    nightly_backstop_hours: 24  # always on; fires only when nothing else did

models:
  classify: claude-haiku-4-5-20251001
  write: claude-sonnet-4-6
  topic: claude-haiku-4-5-20251001
  resolve: claude-opus-4-7

pricing:                        # per-MTok rate table (athenaeum#783); athenaeum init
  claude-opus-5: [5.0, 25.0]    # ships this ACTIVE and pre-populated -- see
  claude-sonnet-5: [3.0, 15.0]  # "Per-MTok pricing" above for the full table
  # ... (truncated here; see athenaeum.models._MODEL_RATES_USD_PER_MTOK)

contradiction:
  cross_scope_mode: ancestor
  cluster_size_cap: 25
  similarity_threshold: 0.85
  resolve_max_per_run: 250
  resolved_similarity_threshold: 0.83
  not_a_conflict_ttl_days: 0  # 0 = disabled; >0 decays stale auto not_a_conflict (athenaeum#251)

resolve:
  # model: claude-opus-4-7   # legacy — prefer models.resolve above
  auto_apply: true
  auto_apply_threshold: 0.90
  full_body_token_cap: 1500
  # auto_apply_threshold_per_action:
  #   not_a_conflict: 0.75
  #   keep_a: 0.90
  #   keep_b: 0.90
```
