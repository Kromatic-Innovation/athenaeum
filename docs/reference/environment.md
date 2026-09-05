# Environment variables and configuration

**Reference page.** Settings resolve in the order **CLI flag > environment variable >
`<knowledge_root>/athenaeum.yaml` > built-in default**, so a one-off shell export beats the
yaml without requiring an edit.

For the exhaustive per-knob reference, see [configuration](configuration.md).

## The common knobs

The table below covers the common knobs. The exhaustive list — every env var,
yaml key, and CLI flag with its code default and precedence chain — lives in
[`configuration.md`](configuration.md).

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes (unless `--dry-run`) | API key for Tier 2/3 LLM calls |
| `ATHENAEUM_CLASSIFY_MODEL` | No | Override Tier 2 model. Precedence: env > `models.classify` in `athenaeum.yaml` > default `claude-haiku-4-5-20251001` |
| `ATHENAEUM_WRITE_MODEL` | No | Override Tier 3 model. Precedence: env > `models.write` in `athenaeum.yaml` > default `claude-sonnet-4-6` |
| `ATHENAEUM_LLM_PROVIDER` | No | LLM backend for the compile path: `api` (default, metered Anthropic API) or `claude-cli` (run the librarian on a Claude Code **subscription** via the `claude` binary, no API key). Precedence: env > `llm.provider` in `athenaeum.yaml` > `api`. Batch mode is API-only. See [`configuration.md`](configuration.md) → "LLM provider selection" |
| `ATHENAEUM_CLAUDE_CLI_BIN` | No | Path or name of the `claude` binary for the `claude-cli` provider (default: `claude`, resolved on `PATH`) |
| `ATHENAEUM_CLAUDE_CLI_TIMEOUT` | No | Per-call timeout in seconds for the `claude-cli` subprocess (default: `300`) |
| `ATHENAEUM_RESOLVE_MODEL` | No | Override the contradiction-resolver model (default: `claude-opus-4-7`) |
| `ATHENAEUM_RESOLVE_MAX_PER_RUN` | No | Cap resolver calls per ingest run (default: `250`) |
| `ATHENAEUM_MAX_API_CALLS` | No | Run-level API call budget for `athenaeum run`. Precedence: `--max-api-calls` CLI flag > env > `librarian.max_api_calls` in `athenaeum.yaml` > default `800`. Env `0` is valid and defers the entire intake (writes `wiki/_deferred_work.md` and logs the DEGRADED summary); the CLI flag rejects `0` |
| `ATHENAEUM_MAX_FILES` | No | Per-run intake batch size for `athenaeum run`. Precedence: `--max-files` CLI flag > env > `librarian.max_files` in `athenaeum.yaml` > default `50`. Env `0` is valid (defer-everything window); the CLI flag rejects `0` |
| `ATHENAEUM_BATCH_MODE` | No | Opt-in [Batch API](https://platform.claude.com/docs/en/build-with-claude/batch-processing) mode for `athenaeum run`: tier-2/tier-3 calls are submitted as batches at a 50% token discount. Latency-tolerant — most batches finish within an hour, 24h worst case — intended for the nightly run. Precedence: `--batch-mode` / `--no-batch-mode` CLI flags > env > `librarian.batch_mode` in `athenaeum.yaml` > default off (`--no-batch-mode` forces the synchronous path even when env/yaml turn batch mode on) |
| `ATHENAEUM_RESOLVE_AUTO_APPLY` | No | Auto-apply high-confidence resolutions (default: `true`). See [`../design/auto-resolve.md`](../design/auto-resolve.md) |
| `ATHENAEUM_RESOLVE_AUTO_APPLY_THRESHOLD` | No | Confidence floor for auto-apply, in `[0.0, 1.0]` (default: `0.90`) |
| `ATHENAEUM_RESOLVE_FULL_BODY_TOKEN_CAP` | No | Per-side body cap for the resolver's full-body context, ~4 chars/token (default: `1500`; must be positive) |
| `ATHENAEUM_CROSS_SCOPE_MODE` | No | Cross-scope contradiction detection: `off` / `ancestor` / `similarity` / `both` (default: `ancestor`). See [`../design/contradiction-detection.md`](../design/contradiction-detection.md) |
| `ATHENAEUM_RESOLVED_SIMILARITY_THRESHOLD` | No | Cosine threshold for matching new detections against the resolved-decision log (default: `0.83`) |
| `ATHENAEUM_TIER4_DEDUP` | No | Dedupe pending-question escalations by source-memory pair (default: `true`; set `false`/`0`/`no`/`off` for legacy always-append) |
| `ATHENAEUM_CACHE_DIR` | No | Cache root for the librarian's embedding/cluster pass (default: `~/.cache/athenaeum`) |
| `ATHENAEUM_TOPIC_MODEL` | No | Override query-topic model. Precedence: env > `models.topic` in `athenaeum.yaml` > default `claude-haiku-4-5-20251001` |
| `ATHENAEUM_OP_KEY_PATH` | No | 1Password path for the session-start `ANTHROPIC_API_KEY` bootstrap (default: `op://Agent Tools/Anthropic API Key/credential`) |
| `ATHENAEUM_PQ_SNOOZE_HOURS` | No | Snooze TTL in hours for pending-questions surfacing (default: `24`; consumed by the `resolve-questions` skill) |
| `ATHENAEUM_PYTHON` | No | Python interpreter used by the example hooks (default: `python3`) |
| `AUTO_RECALL` | No | Per-turn recall on/off (hook shell env; overrides `athenaeum.yaml`'s `auto_recall`). Default: `true` |
| `SEARCH_BACKEND` | No | `fts5` or `vector` (hook shell env; overrides `athenaeum.yaml`'s `search_backend`). Default: `fts5` |
| `ATHENAEUM_HOOK_DEBUG` | No | Set to `1` to log vector-backend errors from `user-prompt-recall.sh` to stderr |

**Shell-env overrides.** `AUTO_RECALL` and `SEARCH_BACKEND` are read from the
shell environment after the hook sources `~/.cache/athenaeum/config.env`, so
exports in your shell profile beat the cached config. Intentional (lets you
A/B-test a backend without editing `athenaeum.yaml`), but it's the first
thing to check when the hook "ignores" a config change.

**Claude Code auth caveat.** Claude Code's own `CLAUDE_CODE_OAUTH_TOKEN` is
scoped to its inference endpoint, and the Anthropic Messages API rejects it
with `401 OAuth authentication is currently not supported`. The pipeline and
example hooks need a separate console API key — see
[`../design/recall-architecture.md`](../design/recall-architecture.md#anthropic_api_key-bootstrap-sessionstart)
for the 1Password bootstrap pattern.

## Precedence and yaml shape

Settings are resolved in the order **CLI flag > env var > `<knowledge_root>/athenaeum.yaml` > built-in default**, so a one-off shell export beats the yaml without requiring an edit. The canonical reference for every knob — librarian budgets, model selection, contradiction/resolver tuning, recall/search, and hook environment — is [`configuration.md`](configuration.md). As one example, the resolver model lives under the top-level `models:` block, and the rest of the resolver's behavior knobs live under `resolve:`:

```yaml
models:
  resolve: claude-opus-4-7        # ATHENAEUM_RESOLVE_MODEL

resolve:
  auto_apply: true                # ATHENAEUM_RESOLVE_AUTO_APPLY (default: true)
  auto_apply_threshold: 0.90      # ATHENAEUM_RESOLVE_AUTO_APPLY_THRESHOLD, [0.0, 1.0]
  full_body_token_cap: 1500       # ATHENAEUM_RESOLVE_FULL_BODY_TOKEN_CAP, per-side body cap (~4 chars/token)
```

When `auto_apply` is on and a proposal's confidence meets or exceeds `auto_apply_threshold`, the pending-question block is auto-flipped to answered with an `Auto-resolved: true` audit-trail tag. See [`../design/auto-resolve.md`](../design/auto-resolve.md) for the full lane, including how to disable, lower the threshold, or reverse an auto-resolution.

**Alternative model gateways.** All model calls go through the Anthropic SDK, which honors `ANTHROPIC_BASE_URL` — so a LiteLLM proxy or any Anthropic-compatible gateway can serve alternative models with zero code change. Only Claude models are first-party tested; see [`configuration.md`](configuration.md#alternative-model-gateways-anthropic_base_url) for the details.

## See also

- [Configuration](configuration.md) — every key, with defaults and precedence chains.
- [Provider](../modules/provider.md) — what the `api` and `claude-cli` backends each honor.
- [Librarian](../modules/librarian.md) — the budget and lock the run-level knobs bound.
- [Daily operation](../guides/daily-operation.md) — which command reads which knob.
