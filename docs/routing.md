# Provider / Model / Batch Routing — One Page Per Function

Athenaeum routes every LLM-serving call through three independently
configurable axes: **provider** (`llm.providers.<knob>` / [LLM provider
selection](configuration.md#llm-provider-selection-athenaeum330)), **model**
(`models.<knob>` / [Models](configuration.md#models)), and **batch mode**
(`librarian.batch.<knob>` / [Backlog drain](configuration.md#backlog-drain-athenaeum-drain-athenaeum470)
and the librarian-run section). Each axis is documented in full on
[configuration.md](configuration.md), under its own `yaml` parent. **This
page answers the one question none of those sections answers alone: what
provider, model, and batch mode does *this function* actually use** — and
states the one precedence rule that is easy to get backwards.

Nothing on this page is new routing behavior. Provider and model resolution
are both already genuinely per-knob and fully wired (issues athenaeum#786 /
athenaeum#841 / athenaeum#232) — this is assembly and documentation of
existing resolvers, plus a read-only CLI preview
(`athenaeum explain-routing`, below) that prints exactly what those
resolvers return (issue athenaeum#1176).

## The knob is the unit of routing, not the function

Every LLM-serving call site resolves one of the **model knobs** —
`prompt_registry.KNOBS`, currently `classify`, `reasoning_t1`,
`reasoning_t2`, `resolve`, `rule_proposals`, `topic`, `write` (sorted; the
source of truth is `prompt_registry._META_ROWS`, so this list grows
automatically as new call sites are registered there — see athenaeum#781 /
athenaeum#1174). Several *functions* share one knob by design; overriding
that knob's provider or model affects every function that shares it.

| Function | Knob | Batch-eligible? |
|---|---|---|
| Tier-2 page classifier (`tiers.tier2_classify`) | `classify` | yes — shares `classify`'s batch state |
| C4 contradiction detector (`contradictions.detect_system`) | `classify` | yes — shares `classify`'s batch state |
| `claim_kind` stamping | `classify` | yes — shares `classify`'s batch state |
| Tier-3 wiki writer, create + merge (`tiers.tier3_create`/`tier3_merge`) | `write` | yes |
| C4 resolver (`resolutions.propose_resolution`) | `resolve` | no — never reaches the Batch transport |
| Recall query-topic extraction (`athenaeum query-topics`, the recall hot path) | `topic` | no |
| Reasoning-tier T1 screen (`merge.t1_screen_rejects_merge_proposal`) | `reasoning_t1` | no |
| Reasoning-tier T2 auto-apply gate (`merge.t2_screen_merge_proposal`) | `reasoning_t2` | no |
| Rule-proposal drafting (`rule_proposals.build_rule_proposal_request_params`, athenaeum#1174) | `rule_proposals` | no |

Only `classify` and `write` are ever batched — `batch.py`'s `execute_batch`
has exactly two call sites (`BATCHABLE_KNOBS` in `librarian.py`). Setting
`librarian.batch.<knob>` on any other knob is a config error the run refuses
to start with, not a silent no-op.

For the env var / yaml key / default / CLI flag of each axis, see the
tables already on [configuration.md](configuration.md) — this page does not
duplicate them, to avoid a second copy drifting out of sync.

## The precedence rule: per-knob yaml beats global env

**A `llm.providers.<knob>` (or `models.<knob>`) yaml setting is NOT
overridden by the corresponding GLOBAL environment variable
(`ATHENAEUM_LLM_PROVIDER`).** This is the opposite of the usual "env beats
yaml" convention most operators bring from elsewhere, and it is easy to
misread the general precedence statement at the top of
[configuration.md](configuration.md#precedence) ("env override always beats
the yaml") as applying here — that statement is about a single knob's OWN
env vs. its OWN yaml, not about a global env var vs. a *different* knob's
yaml key.

Verified against the resolvers themselves
(`provider.resolve_provider`, `config.resolve_model`): the **per-knob**
chain is `ATHENAEUM_<KNOB>_LLM_PROVIDER` env > `llm.providers.<knob>` yaml >
the **global default**. The global default is consulted ONLY when the knob
has neither its own env override nor its own yaml override — and the global
env var (`ATHENAEUM_LLM_PROVIDER`) is itself just one layer *inside* that
global-default resolution. So a per-knob yaml key is checked, and wins,
**before** the global env var is ever read for that knob. Concretely:

```yaml
llm:
  provider: claude-cli     # global default
  providers:
    write: api             # write's OWN yaml override
```

With `ATHENAEUM_LLM_PROVIDER=claude-cli` set in the environment (or omitted
— it makes no difference here), `write` still resolves to `api`: its own
yaml key is checked before the global chain (env-or-yaml-or-default) is
consulted at all. Only a knob with **no** override of its own — no
`ATHENAEUM_<KNOB>_LLM_PROVIDER`, no `llm.providers.<knob>` — falls through
to the global default, and only THEN does the global env var have any say.

The same shape holds for models: `config.resolve_model` reads a per-knob env
var and a per-knob yaml key; there is no global model env var at all for it
to be overridden by.

## Subscription-only (`claude-cli`-for-everything) install recipe

> ## ⚠️ NOT YET SAFE TO FOLLOW — precondition unarmed (athenaeum#1153)
>
> **Do not follow this recipe until athenaeum#1153 closes.** That issue arms
> the subscription spend ceilings; until it does, the operator's Max 20x
> weekly allowance has **no guard**, and routing the nightly librarian's
> traffic onto the subscription (this recipe) would consume the operator's
> own interactive headroom with nothing stopping it. Verified 2026-08-31:
> athenaeum#1153 is OPEN (`ready`, `needs:host-write`) — **not armed**. Check
> its state before following the steps below; this banner is stale the
> moment that issue closes, and should be deleted then (a one-line diff).

Once athenaeum#1153 is armed, a fully `claude-cli` install needs no
`ANTHROPIC_API_KEY` at all (`provider.build_llm_client` returns a
`ClaudeCliClient` for every knob when nothing overrides the global
provider):

```yaml
llm:
  provider: claude-cli
```

or `ATHENAEUM_LLM_PROVIDER=claude-cli` in the environment. See
[claude-cli (subscription)](configuration.md#claude-cli-subscription) for
the backend's full constraints (no `cache_control`, `max_tokens` advisory,
batch mode unavailable, etc.) — this section only adds the two holes below,
which apply specifically to a **subscription-only** install and must not be
silently omitted from a recipe that recommends one.

### Hole 1 — preflight only checks the GLOBAL provider

`athenaeum run`'s startup preflight (`provider.preflight_provider`) is
called exactly once, against the resolved **global** provider
(`ctx.provider = resolve_provider(ctx.config)`, no `knob=`) — verified at
`librarian.py`'s `_run_preconditions` (the `preflight_err =
preflight_provider(ctx.provider)` call). Per-knob provider overrides are
validated for being a *recognized* value at the same preflight gate, but
their reachability (does this knob's resolved backend actually have what it
needs to run) is **not** re-checked per knob. Concretely:

```yaml
llm:
  provider: claude-cli      # preflight checks: is the `claude` binary present? yes -> passes.
  providers:
    write: api              # NOT preflight-checked for an ANTHROPIC_API_KEY.
```

This config **passes preflight** (the global provider's binary is present)
and then **degrades to a null client at run time** the moment `write`'s
client is constructed — `build_llm_client` returns `None` for the `api`
backend with no key (by design, so the existing `client is None`
short-circuits keep working), and the tier-3 writer defers every file
instead of writing. Preflight is not a guarantee here — this hole is
recorded, not fixed (fixing it is explicitly out of scope for athenaeum#1176;
see that issue for the follow-up).

### Hole 2 — `athenaeum drain` requires a key unconditionally

`athenaeum drain`'s `check_api_key` (`drain.py`) requires
`ANTHROPIC_API_KEY` in the environment **unconditionally, by design** — it
forces the API + Batch path regardless of the configured provider, because
draining at the Batch API's 50% discount has no `claude-cli` equivalent. **A
subscription-only install cannot drain.** If backlog draining is part of an
operator's workflow, a subscription-only recipe does not cover it; the
operator needs `ANTHROPIC_API_KEY` set for `drain` specifically even if
`athenaeum run` never needs one.

## `athenaeum explain-routing` — read-only preview

```
$ athenaeum explain-routing --path ~/knowledge
knob | provider | model | batch | price ($/MTok in,out)
classify | claude-cli | claude-haiku-4-5-20251001 | not batched (eligible) | 1.0/5.0
reasoning_t1 | claude-cli | claude-haiku-4-5-20251001 | never batched | 1.0/5.0
reasoning_t2 | claude-cli | claude-opus-4-8 | never batched | 5.0/25.0
resolve | claude-cli | claude-opus-5 | never batched | 5.0/25.0
topic | claude-cli | claude-haiku-4-5-20251001 | never batched | 1.0/5.0
write | api | claude-opus-5 | batched | 5.0/25.0
```

Add `--json` for machine-readable output (one object per knob: `knob`,
`provider`, `model`, `batch_eligible`, `batched_this_run`,
`price_input_usd_per_mtok`, `price_output_usd_per_mtok`,
`price_is_blended_fallback`).

**This command changes nothing** — no LLM call, no file processed, no
config written. It calls the exact same resolvers a real `athenaeum run`
calls (`provider.resolve_provider`, the per-knob model getters via
`librarian._resolve_run_models`, `librarian.librarian_batch_knob`) for the
same `athenaeum.yaml` + environment, so its output is guaranteed to match
what a real run actually uses rather than being a second, potentially
drifting description of the routing rules — see
`tests/test_cmd_explain_routing.py` for the test asserting that equality
directly against those same resolvers.

The knob list this command prints is read from `prompt_registry.KNOBS` at
run time, so it grows automatically as new knobs are registered there — it
never needs updating by hand when a new model knob is added.
