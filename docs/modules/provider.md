# The LLM provider seam

**Reference page.** For the task-shaped version, see
[Claude Code integration](../guides/claude-code.md).

## What it does

Every call site that talks to an LLM — Tier-2 classify, Tier-3 merge, contradiction
detection, resolution, the reasoning tiers, recall's query-topics — goes through one
factory, `build_llm_client`, instead of constructing its own SDK client. The factory hides
which backend is actually serving the call behind a shared `messages.create(**params) ->
LLMResponse` surface, so a call site never learns or branches on which backend answered it.

Two backends ship:

- **`api`** (default) — wraps `anthropic.Anthropic` verbatim. Every parameter passes through
  unchanged, so prompt caching, the Batch API, and retry behavior are byte-for-byte identical
  to calling the SDK directly.
- **`claude-cli`** — drives the operator's ambient Claude Code subscription login via
  `claude -p --model <id> --system-prompt <sys> --output-format json`. No credential
  handling of its own — exactly like the librarian's git-push path, it relies on the
  operator's own `claude` login. Token counts from the CLI's JSON envelope still feed
  `TokenUsage`, but `estimated_cost_usd` reports `$0` because spend is subscription-covered.

Each backend **declares** what it can honor via `ProviderCapabilities` (`honors_max_tokens`,
`reports_stop_reason`, `honors_cache_control`, `honors_sampling_params`, `supports_batches`)
rather than silently dropping a parameter it cannot serve. A call site branches on the
declared capability, never on the provider id string — the fix for a bug the capability
table exists specifically to prevent: the CLI backend used to drop `max_tokens` with no CLI
equivalent, so a truncation-recovery retry that only raises `max_tokens` re-sent a
byte-identical request and could never actually recover.

Provider selection is **per-knob**: `resolve_provider(config, knob="write")` checks
`ATHENAEUM_<KNOB>_LLM_PROVIDER` (env) then `llm.providers.<knob>` (yaml) before falling back
to the run's global default (`ATHENAEUM_LLM_PROVIDER` env, then `llm.provider` yaml, then
`"api"`). `LLMClientCache` memoizes constructed clients by `(provider, api_key, max_retries,
timeout)` so several knobs that resolve to the same provider and construction args share one
client rather than each building its own.

## What it reads

- `ATHENAEUM_LLM_PROVIDER` / `llm.provider` — the global default provider.
- `ATHENAEUM_<KNOB>_LLM_PROVIDER` / `llm.providers.<knob>` — a per-knob override for one of
  `classify`, `write`, `resolve`, `reasoning_t1`, `reasoning_t2` (and, on the recall hot
  path, `topic`).
- `ANTHROPIC_API_KEY` (or an explicit `api_key` argument) for the `api` backend.
- `ATHENAEUM_CLAUDE_CLI_BIN` (default `claude`) and `ATHENAEUM_CLAUDE_CLI_TIMEOUT` (default
  300s) for the `claude-cli` backend.
- `ATHENAEUM_BATCH_MODE` — checked against the resolved provider's `supports_batches`
  capability at startup, not per-call.

## What it writes

This module owns transport only — client construction and capability declaration. It writes
nothing to disk itself; token counts it surfaces are recorded into the caller's `TokenUsage`
by the caller, not by this module.

## What it refuses

- **`build_llm_client` returns `None`, not an error, when the `api` backend has no
  `ANTHROPIC_API_KEY` configured** — every deterministic offline fallback in the tiers /
  contradictions / resolutions / reresolve paths depends on this `client is None`
  short-circuit continuing to work.
- **An unrecognized provider id is a loud `ProviderConfigError`**, naming the knob and the
  source it came from (env var or yaml key) — never a silent fallback to a different backend
  or to the global default.
- **`claude-cli` preflights at startup, not per-file.** If the configured binary is not on
  `PATH` and does not exist at the given path, `preflight_provider` returns an error that
  fails the run at rc 1 immediately — the alternative (discovering it per-file) would let a
  misconfigured run silently defer every file and exit 0 with no token summary.
- **Batch mode is API-only.** `ATHENAEUM_BATCH_MODE` combined with a provider whose
  `supports_batches` capability is `False` (i.e. `claude-cli`) is a loud startup error in
  `run_librarian` — the Batch API has no CLI equivalent, and the CLI backend's own
  `messages` facade deliberately exposes no `.batches` attribute at all.
- **`cache_control` is silently stripped, not honored, on `claude-cli`.** The capability
  table declares this explicitly (`honors_cache_control=False`) rather than leaving it an
  unstated side effect of the adapter's implementation.
- **A CLI subprocess timeout is never retried in-run.** It maps straight to the
  give-up `TransientAPIError` type on the first occurrence — retrying it in-run would
  multiply an already-generous per-call timeout across every retry attempt. A rate-limited or
  otherwise transient CLI failure (detected from the exit code, stderr, or the JSON
  envelope's `is_error`/`subtype`) is different: it raises the shared, retryable
  `TransientError` so `with_retry` retries it exactly like an `api`-backend transient error,
  and only surfaces as `TransientAPIError` once retries are exhausted.
- **A resolved client is cached per exact construction args, never just per provider name.**
  Two call sites that both resolve to `"api"` but pass different `timeout`/`max_retries`
  never collide on one memoized client and silently inherit each other's retry/timeout
  behavior.

## See also

- Guides — [Claude Code integration](../guides/claude-code.md)
- Modules — [librarian](librarian.md) · [intake](intake.md)
- Reference — [configuration](../reference/configuration.md)
