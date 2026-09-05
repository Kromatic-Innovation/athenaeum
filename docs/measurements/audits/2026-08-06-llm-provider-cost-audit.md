# Audit — LLM provider seam, model catalog, and cost governance

**Repo:** athenaeum · **Branch:** develop · **SHA:** `eac27294201a8a568a79782c6efcdb06a29e94b4` · **Date:** 2026-08-06
Working tree clean at audit time. **Every finding below is valid at that SHA only.**

**Repo purpose + failure tolerance** (`inferred` — read from repo docs and CLAUDE.md, not elicited from the operator): a personal knowledge-compilation system whose nightly librarian run compiles `~/knowledge` via LLM calls. Single-operator, single-machine, resumable. Failure tolerance is moderate for the compile itself (run-lock + resume make a deferred file safe), but **low for spend accounting** — the ledger is the enforcement point for real-dollar ceilings and feeds a cross-repo accounting contract, so an under-report is a governance failure, not a cosmetic one. Severity below is graded against that.

**Mode:** read-only. Diagnosis only — no audited code was changed.

**Scope (declared up front):** the LLM transport seam and everything downstream of it for cost — `provider.py`, `_retry.py`, `models.py` (pricing + capability tables), `spend.py`, `config.py` (spend/model resolvers), the `messages.create` call sites, and the good-morning consumer of the ledger. **Not audited:** prompt content and prompt quality, clustering/merge cost behavior (athenaeum#764's territory), the storage/vector layer, CI, and security beyond credential handling in the seam. See §F for the per-dimension coverage table.

---

## A. Decision sheet

### Verified bugs

| ID | Severity | One line |
|---|---|---|
| **L1** | **High** | `claude-fable-5` / `claude-mythos-5` are absent from the pricing table but present in its sibling capability table — they price at the blended fallback, **under-reporting spend ~6.7x**, which silently under-enforces the dollar ceilings. |
| **L8** | Low | `spend.py`'s module docstring says query-topics "always talks to the SDK directly"; it routes through the provider seam and can be subscription-served. Cost-attribution doc is stale. |

### Open decisions (yours, not mine)

**D1 — What is the denominator for "max % of subscription"?** *(exclusive; this list may not be exhaustive — a different answer is a valid answer)*

The four existing ceilings are absolute (tokens/run, tokens/day, USD/run, USD/day). A percentage ceiling needs a quota to be a percentage *of*, and **Claude Code subscription limits are rolling-window and not exposed as a readable token quota** — athenaeum cannot discover the denominator. Options:

1. **Operator-declared denominator** — `spend.subscription_budget_tokens: N` in yaml, with `max_pct_of_subscription` resolving against it. Honest, testable, and the only option that works today. You maintain N by hand when your plan changes.
2. **Derive an empirical denominator from the ledger** — treat the observed per-day maximum as the ceiling proxy. No hand-maintenance, but it drifts and silently ratchets upward.
3. **Skip the percentage knob** — the absolute token ceilings already bound subscription consumption; a percentage adds a number that can only ever be a guess.

My recommendation is **(1)**. It is the only form that means what it says.

**D2 — Does the pricing table become config-overridable, or fully config-owned?** *(exclusive)*

You asked for costs not to be hardcoded. Two shapes:

1. **Overlay** — yaml `pricing.<prefix>: [in, out]` merges *on top of* the code table, which stays as the floor. A model you forget to price still lands on a known rate.
2. **Replace** — yaml becomes the sole source. Cleaner conceptually, but a model omitted from yaml falls to the blended fallback and under-reports — **which is exactly the L1 bug, promoted from a one-off slip to a standing hazard.**

Strong recommendation: **(1) overlay**. Same failure class as athenaeum#568 (a silent under-count that disarms a ceiling).

**D3 — athenaeum#774's spend question is still unanswered and still gates that issue.** I did not resolve it; it is `needs-decision` for a reason. Nothing in this audit depends on it, and athenaeum#775 is independent of it.

> **Answered 2026-08-06, after this audit was written:** the operator accepted the move to the
> metered `api` backend. See athenaeum#774 for the recorded decision. The paragraph above stands as
> written at audit time; the L1-is-a-prerequisite interaction noted under athenaeum#774 below is now
> live rather than hypothetical.

### Do-not-touch list (looks wrong, is deliberately correct)

| Thing | Why it stays |
|---|---|
| `batch.py` takes a concrete `anthropic.Anthropic` | Batch is API-only by declared capability (`supports_batches=False` on CLI, enforced by a loud startup guard). Typing it to the generic backend would imply portability that does not exist. **CORRECT-BY-DESIGN.** |
| Sonnet 5 recorded at standard `$3/$15`, not the introductory `$2/$10` | Deliberate, documented decision (Occam 2026-07-31): the table has no time dimension, so encoding a promo would go silently wrong on 2026-09-01. Over-reporting is the safe direction for a financial consumer. **CORRECT-BY-DESIGN.** |
| `estimated_cost_usd` reports `$0` on the subscription path | Not a missing feature — `notional_usd` carries the counterfactual. The two are deliberately never summed. **CORRECT-BY-DESIGN.** |
| Ledger writes swallow every exception | Deliberate (a ledger write must never break the run it measures) **and** already hardened: athenaeum#568 raised it to a loud WARNING precisely because a silent failure disarms the cumulative ceiling. **CORRECT-BY-DESIGN.** |
| `resolve_provider` raises on an unknown id instead of falling back | Deliberate: a typo must never silently route to a different backend. **CORRECT-BY-DESIGN.** |

---

## B. Finding corpus

Each finding is written to survive with no session memory.

---

### L1 — `claude-fable-5` and `claude-mythos-5` are missing from the pricing table, so they price at the blended fallback

| | |
|---|---|
| **Severity** | High |
| **Category** | cost-accounting / boundary-literal |
| **Oracle** | `external-contract` |
| **Effort** | S |
| **Remedy** | DIRECTION |

**Location:** `src/athenaeum/models.py:1350` `"_MODEL_RATES_USD_PER_MTOK: dict[str, tuple[float, float]] = {"` — the table's entries are `src/athenaeum/models.py:1359` `"    \"claude-opus-5\": (5.0, 25.0),"` through `src/athenaeum/models.py:1363` `"    \"claude-haiku-4\": (1.0, 5.0),"`. No `claude-fable-5` or `claude-mythos-5` entry exists.

**Finding.** The sibling capability table *does* list Fable 5 — `src/athenaeum/models.py:1404` `"    \"claude-fable-5\": True,"` — under a comment that explicitly requires the two be kept in step: `src/athenaeum/models.py:1397` `"# maintain the two tables together."` They are not in step. A `claude-fable-5`-tagged token therefore matches no prefix and falls through to `src/athenaeum/models.py:1369` `"_BLENDED_INPUT_USD_PER_MTOK = 1.50"` / `src/athenaeum/models.py:1370` `"_BLENDED_OUTPUT_USD_PER_MTOK = 7.50"`.

Per the `claude-api` skill's model catalog (source, as-of 2026-06-24), Claude Fable 5 and Claude Mythos 5 are **$10.00 / $50.00** per MTok. Against the blended fallback that is a **6.67x under-report on both input and output**.

**Why it matters.** Model ids are operator-configurable through `resolve_model` (env > yaml `models.<knob>` > code default), so setting `models.resolve: claude-fable-5` in `athenaeum.yaml` is a one-line, entirely supported change that silently under-reports spend by 6.67x. Because `resolve_spend_max_usd_per_day` / `..._per_run` compute against `estimated_cost_usd`, a **dollar ceiling set to $10/day would not trip until roughly $67 of real spend**. The table's own comment states the intent this violates: `src/athenaeum/models.py:1352` `"    # BEFORE any DEFAULT_*_MODEL moves to it (athenaeum#580) so a bump can never fall"`.

This is currently **latent, not active** — no `DEFAULT_*_MODEL` is Fable 5 today (`DEFAULT_RESOLVE_MODEL = "claude-opus-5"`, `DEFAULT_WRITE_MODEL = "claude-sonnet-5"`, `DEFAULT_CLASSIFY_MODEL = "claude-haiku-4-5-20251001"`), and no `fable` string appears anywhere in `src/`. It is armed by config, not by code.

**Remedy (DIRECTION).** Add `"claude-fable-5": (10.0, 50.0)` and `"claude-mythos-5": (10.0, 50.0)`, and — to satisfy the "all known models from 4.8 up, listed with their costs" requirement — add explicit `claude-opus-4-8` / `claude-opus-4-7` / `claude-opus-4-6` / `claude-sonnet-4-6` / `claude-haiku-4-5` entries rather than relying on the shorter prefixes to catch them. The prefixes currently resolve those five correctly, so this is legibility and future-proofing, not a second bug.

**Settling check:** a parametrized test asserting `_rates_for_model(m) != (_BLENDED_INPUT_USD_PER_MTOK, _BLENDED_OUTPUT_USD_PER_MTOK)` for every id in the catalog — which would fail today on `claude-fable-5` and pass on the other eight. Pair it with a test asserting the key sets of the two tables agree, so the documented "maintain together" invariant is enforced rather than merely requested.

**Related:** prerequisite-of L2 (externalizing pricing without fixing the floor propagates this failure mode).

---

### L2 — Model pricing is the one cost knob with no config resolver

| | |
|---|---|
| **Severity** | Medium |
| **Category** | configuration-hygiene |
| **Oracle** | `internal-consistency` |
| **Effort** | M |
| **Remedy** | DIRECTION |

**Location:** `src/athenaeum/models.py:1350` `"_MODEL_RATES_USD_PER_MTOK: dict[str, tuple[float, float]] = {"`

**Finding.** Every other cost-relevant value in athenaeum resolves through the `env > yaml > code default` chain: `resolve_model`, `resolve_max_tokens`, `resolve_thinking`, `resolve_provider`, `resolve_spend_max_tokens_per_run`, `resolve_spend_max_tokens_per_day`, `resolve_spend_max_usd_per_run`, `resolve_spend_max_usd_per_day`, `resolve_spend_ledger_enabled`, `resolve_spend_ledger_path`. The per-MTok rate table is a bare module constant with no resolver at all. The file states this deliberately — `src/athenaeum/models.py:1349` `"# else in the codebase hard-codes per-MTok rates."` — but "single update site" is a different property from "configurable", and prices change on a vendor's schedule, not on a release schedule.

**Why it matters.** Correcting a rate today requires a code edit, a version bump, a full test suite run, and a deploy — for a number that Anthropic can change without notice. Between a price change and that release, every ledger row is mispriced and every dollar ceiling is miscalibrated.

**Remedy (DIRECTION).** Add `resolve_model_rates(config)` in `config.py`, mirroring the existing resolver shape, reading yaml `pricing.<prefix>: [input, output]`. **Overlay onto the code table, never replace it**

> **Schema note for whoever builds this.** A prefix key alone cannot express every real rate. Two known cases: Sonnet 5's introductory rate is **time-boxed** (documented decision to ignore — see do-not-touch), and Opus 5 has a **per-request-mode** rate (`$5/$25` standard vs `$10/$50` under `speed: "fast"`). Athenaeum uses neither fast mode nor time-varying rates today, so this is not a finding — but the config schema should either carry a mode dimension or **explicitly declare single-rate-per-prefix as its contract**, rather than discovering the limitation after operators depend on it.

**Overlay, continued:** — see decision D2; a replace-semantics implementation converts L1 from a one-time slip into a permanent hazard. Reject malformed entries with a warning and fall through to the code rate, matching `resolve_max_tokens`'s existing treatment of a bad override.

**Settling check:** a test that sets `pricing.claude-opus-5: [7.0, 35.0]` in a config dict and asserts `TokenUsage.estimated_cost_usd` moves accordingly, plus a test that an omitted model still gets its code rate rather than the blended fallback.

**Related:** depends-on L1; prerequisite-of L3.

---

### L3 — The ledger is repriceable by design but there is no repricing path

| | |
|---|---|
| **Severity** | Medium |
| **Category** | cost-accounting |
| **Oracle** | `internal-consistency` |
| **Effort** | M |
| **Remedy** | DIRECTION |

**Location:** `src/athenaeum/spend.py` — `tokens_by_model()` and the `summarize()` `unpriceable` counter.

**Finding.** Ledger schema v2 stores per-model token attribution specifically so historical rows can be repriced: the module docstring states the fact is `tokens x model` and that dollars are derived, and `summarize()` maintains an `unpriceable_records` count for rows that lack the attribution. That machinery exists and is correct. What does not exist is anything that consumes it — no `athenaeum spend --reprice`, no rate-table-version stamp on a row. `athenaeum spend` reads each row's stored `estimated_cost_usd` verbatim.

**Why it matters.** Two consequences, both bearing directly on this audit's other findings. Fixing L1 does **not** correct already-written Fable-5 rows — they keep the blended price forever. And externalizing pricing (L2) buys nothing retroactively: a corrected rate applies only to future runs. The repricing capability was built and then left with no door.

**Remedy (DIRECTION).** Add an `athenaeum spend --reprice` mode that recomputes from `tokens_by_model` against the current (post-L2, config-aware) rate table, reports the delta against stored values, and counts the `unpriceable` rows it could not touch. Read-only by default — it should report the corrected figure, not rewrite the append-only ledger.

**Settling check:** write a ledger row under one rate table, change the rate, and assert `--reprice` reports the new total while the on-disk row is unchanged.

**Related:** depends-on L2.

---

### L4 — No percentage-of-subscription ceiling, and no discoverable denominator for one

| | |
|---|---|
| **Severity** | Medium |
| **Category** | cost-governance |
| **Oracle** | `external-contract` |
| **Effort** | M |
| **Remedy** | DIRECTION (blocked on decision D1) |

**Location:** `src/athenaeum/spend.py` — `ceiling_tripped()`; `src/athenaeum/config.py` — the four `resolve_spend_max_*` resolvers.

**Finding.** `ceiling_tripped()` implements exactly four ceilings, correctly unit-split by billing path: subscription runs are bounded in tokens (per-run and per-day), metered API runs in dollars (per-run and per-day). All four are absolute. The requested "max % of subscription" has no implementation and, more importantly, **no denominator available to compute against** — Claude Code subscription limits are rolling-window and are not exposed to athenaeum as a readable token quota. There is nothing in the repo, the CLI JSON envelope, or the ledger from which a subscription size could be derived.

**Why it matters.** A percentage ceiling implemented against a guessed denominator is worse than no ceiling: it reads as a calibrated guardrail while being an arbitrary number. This is why it appears here as a finding gated on a decision rather than as a build task.

**Remedy (DIRECTION).** Per decision D1, add `spend.subscription_budget_tokens` as an operator-declared denominator and `spend.max_pct_of_subscription_per_day` resolving against it, following the existing resolver shape. When the denominator is unset the percentage ceiling is inert (matching every other ceiling's strictly-opt-in behavior). The ceiling message must name the declared denominator so the operator can see what the percentage was taken of.

**Settling check:** none available for the denominator itself — the absence claim ("no readable subscription quota exists") was verified against the CLI JSON envelope fields parsed in `provider.py::_parse_envelope` and the ledger schema, both of which carry token counts only, never a quota. It was **not** verified against undocumented Claude Code internals or an Anthropic account API; if such a source exists, it would change this finding. Filed as an open question per Gate 4.

---

### L5 — The provider seam is construction-only; no call site is typed against the declared backend contract

| | |
|---|---|
| **Severity** | Medium |
| **Category** | architecture |
| **Oracle** | `internal-consistency` |
| **Effort** | M |
| **Remedy** | DIRECTION |

**Location:** `src/athenaeum/provider.py` declares the `LLMBackend` / `LLMMessages` / `LLMResponse` / `LLMUsage` Protocol family. Call sites annotate the concrete SDK type instead.

**Full enumeration** — exhaustive, not sampled. Derivation: `grep -rn 'client: *"\?anthropic\.Anthropic' src/athenaeum/*.py`, run at this SHA, **21 total sites**:

| File | Lines | Count |
|---|---|---|
| `tiers.py` | 759, 1137, 1475, 1907, 1957, 2074, 3047 | 7 |
| `merge.py` | 1195, 1286, 1548 | 3 |
| `answers.py` | 626, 809 | 2 |
| `claim_kind.py` | 129, 249 | 2 |
| `librarian.py` | 785, 1463 | 2 |
| `resolutions.py` | 1654, 2775 | 2 |
| `contradictions.py` | 396 | 1 |
| **`batch.py`** | **145, 319** | **2 — CORRECT-BY-DESIGN, excluded from the remedy (batch is API-only)** |

**19 sites require re-annotation**; the 2 in `batch.py` are deliberately concrete.

**Finding.** `build_llm_client` genuinely centralizes *construction* — all four documented `messages.create` call-site modules obtain their client through it, and `ClaudeCliClient` is type-checked against `LLMBackend` under a `TYPE_CHECKING` assertion with no `# type: ignore` escape. That part of the epic (athenaeum#572) landed and is sound. But the contract stops at the factory: no consumer is typed against `LLMBackend`, so the type system still asserts across the codebase that the client *is* an Anthropic SDK object.

**Why it matters.** Directly against your requirement that the module be provider-agnostic. Today the seam supports a second Anthropic-shaped backend, which is the case it was built for. A genuinely different provider (OpenAI, Gemini, a local model) would type-error at 19 signatures, and nothing would flag it until then. "One clear module for all reasoning calls" is true of construction and false of typing.

**Remedy (DIRECTION).** Re-annotate the call-site signatures from `anthropic.Anthropic` to `LLMBackend` (or `LLMBackend | None`). This is mechanical and behavior-free — the Protocol was deliberately defined with read-only properties so concrete backends satisfy it covariantly. Leave `batch.py` concrete (see do-not-touch list).

**Settling check:** run the type checker after re-annotation; a clean pass proves the concrete backends satisfy the declared contract at every consumer, which is the property currently unenforced.

**Related:** prerequisite-of L6 (both are required before a third provider is feasible).

---

### L6 — The retry layer is hard-coupled to the Anthropic SDK's exception types

| | |
|---|---|
| **Severity** | Medium |
| **Category** | architecture / error-handling |
| **Oracle** | `internal-consistency` |
| **Effort** | M |
| **Remedy** | DIRECTION |

**Location:** `src/athenaeum/_retry.py:35` `"import anthropic"`, `src/athenaeum/_retry.py:36` `"from anthropic._exceptions import OverloadedError"`, and the `TRANSIENT_ERRORS` tuple naming `anthropic.RateLimitError` / `anthropic.APIConnectionError`.

**Finding.** `with_retry` catches only Anthropic SDK transient types. `provider.py`'s own module docstring already records the consequence for the second backend: a CLI transient is raised as `TransientAPIError`, `with_retry` does not catch it, and the call is not retried in-run — the file is deferred to the next run instead. That is a documented, accepted trade-off for `claude-cli` (the run-lock plus resume make it safe). It generalizes badly: **any** future backend's transients are equally invisible to the retry layer, and the module imports `anthropic` at module scope, so a non-Anthropic-only deployment still requires the SDK installed.

Note this is a *narrower* constraint than the module-scope import in `provider.py`, which is deliberately lazy — `import anthropic` there sits inside `build_llm_client` precisely so a CLI-only deployment need not have the SDK. `_retry.py` undoes that property for the whole process.

**Why it matters.** This, more than the factory, is the real blocker on genuine multi-provider support. A new backend would appear to work and would silently lose all in-run retry behavior.

**Remedy (DIRECTION).** Have each backend classify its own transients and raise a shared athenaeum-owned transient type; make `TRANSIENT_ERRORS` a registry the provider module populates rather than a literal tuple of SDK classes; move the `anthropic` import behind `TYPE_CHECKING` or into the api backend's registration.

**Settling check:** a test asserting a `ClaudeCliClient` rate-limit failure is retried by `with_retry` — which fails today, and is the observable behavior change this finding predicts.

**Related:** depends-on L5.

---

### L7 — A script constructs the Anthropic SDK directly, bypassing the seam

| | |
|---|---|
| **Severity** | Low |
| **Category** | architecture |
| **Oracle** | `internal-consistency` |
| **Effort** | S |
| **Remedy** | DIRECTION |

**Location:** `scripts/measure_contradiction_baseline.py:82` `"    return anthropic.Anthropic(api_key=api_key, max_retries=3)"`

**Finding.** This is the **only** direct `anthropic.Anthropic(...)` construction outside `provider.py::build_llm_client` in the entire repo (verified by regex sweep over `src/**/*.py` and `scripts/**/*`). Everything in `src/` routes through the factory. As a measurement script it never runs in production and never writes the spend ledger — but it is the one place where "everything needing a reasoning model goes through that module" is literally false, and it silently cannot use the subscription backend.

**Remedy (DIRECTION).** Replace with `build_llm_client(config, max_retries=3)`. One line; the factory already accepts `max_retries` and passes it through byte-for-byte on the api path.

**Settling check:** grep for `anthropic.Anthropic(` outside `provider.py` returns zero hits.

---

### L8 — `spend.py`'s docstring misdescribes which path serves query-topics

| | |
|---|---|
| **Severity** | Low |
| **Category** | documentation / cost-attribution |
| **Oracle** | `internal-consistency` |
| **Effort** | S |
| **Remedy** | DIRECTION |

**Location:** `src/athenaeum/spend.py:10` `"  which always talks to the SDK directly). Constrained in real DOLLARS."` — describing the per-turn query-topics recall extractor as part of the metered API path.

**Finding.** `src/athenaeum/query_topics.py:127` `"        client = build_llm_client(config, timeout=timeout, max_retries=0)"` routes through the provider seam like every other call site, so query-topics is served by whatever `llm.provider` resolves to — `claude-cli` on the current nightly configuration. The docstring's claim is stale.

**Why it matters.** Low blast radius: `build_record` derives `billing_mode` from the actually-resolved provider argument, so **the ledger rows are correct** — only the prose is wrong. But it is prose in the one module whose job is answering "are we spending real money?", and it is the sort of claim a future audit would read as authoritative. (A prior code audit mis-answering exactly this question is what motivated athenaeum#378 in the first place.)

**Remedy (DIRECTION).** Reword to state that query-topics routes through the provider seam and is metered only when the resolved provider is `api`.

**Settling check:** none needed — `query_topics.py:127` is the whole evidence.

---

## C. What is already right (and needs no work)

Stated explicitly so the issue graph is not read as a rewrite. Against your six requirements:

| Requirement | State |
|---|---|
| One module for all reasoning calls | **Mostly built.** Construction is fully centralized (`build_llm_client`); one script bypasses it (L7); typing is not (L5); retry is not (L6). |
| Route to API **or** subscription token | **Built.** `resolve_provider`, env > yaml > `api`, loud on a bad id, with a startup preflight probe for the CLI binary. |
| Configurable model defaults | **Built.** `resolve_model` / `resolve_max_tokens` / `resolve_thinking`, all per-stage, all env > yaml > code default. |
| Configurable token/cost limits | **Built for absolute limits** (4 ceilings, unit-split by billing path). Percentage-of-subscription is L4. |
| Costs not hardcoded | **Not built** — L2. And the current hardcoded table has a gap: L1. |
| good-morning can pull usage/models/cost | **Already architected in, as you suspected.** `cicero/good-morning/sub-skills/athenaeum-spend/` and `.../llm-spend/` both read `spend.jsonl`, and the v2 schema was explicitly shaped to cwc#1629's cross-repo accounting contract (`billing_mode`, `tokens_by_model` as a superset of hestia's `cost-ledger.ts` shape, so one reader serves both). No work needed. |

The capability-declaration layer (athenaeum#573/#574) is genuinely good design and is the thing that makes the rest tractable: backends *declare* what they can honor rather than silently dropping params, and `reported_stop_reason` converts an unreliable CLI value into a safe `None` rather than trusting it.

---

## D. Inventories

### D.1 Boundary-literal census (Gate 3.2)

**Derivation:** regex sweep for `claude-[a-z0-9-]+` and `DEFAULT_.*MODEL` over `src/athenaeum/**/*.py` and `scripts/**/*`, plus full reads of the two prefix tables in `models.py`. Verdicts checked against the `claude-api` skill's model catalog, **source as-of 2026-06-24**.

| Literal | Site | Verdict | Note |
|---|---|---|---|
| `claude-opus-5` | `models.py:1359` rate table | `still accepted` | $5/$25 — matches catalog |
| `claude-sonnet-5` | `models.py:1360` rate table | `still accepted` | $3/$15 standard; intro $2/$10 to 2026-08-31 deliberately not encoded — see do-not-touch |
| `claude-opus-4` (prefix) | `models.py:1361` | `still accepted` | Covers 4-8/4-7/4-6, all $5/$25 — correct |
| `claude-sonnet-4` (prefix) | `models.py:1362` | `still accepted` | Covers 4-6 at $3/$15 — correct |
| `claude-haiku-4` (prefix) | `models.py:1363` | `still accepted` | Covers 4-5 at $1/$5 — correct |
| **`claude-fable-5`** | **absent from rate table** | **`rejected`** | **$10/$50 in catalog; prices at blended $1.50/$7.50 — L1** |
| **`claude-mythos-5`** | **absent from rate table** | **`rejected`** | **$10/$50 in catalog; same failure — L1** |
| `claude-opus-5`, `claude-opus-4-8`, `claude-opus-4-7`, `claude-sonnet-5`, `claude-fable-5`, `claude-haiku-4-5`, `claude-sonnet-4-6` | `models.py:1398-1407` sampling table | `still accepted` | Matches the catalog's sampling-rejection rows exactly |
| `claude-opus-5` | `resolutions.py:113` `DEFAULT_RESOLVE_MODEL` | `still accepted` | Current model, valid id |
| `claude-sonnet-5` | `tiers.py:125` `DEFAULT_WRITE_MODEL` | `still accepted` | Current model, valid id |
| `claude-haiku-4-5-20251001` | `config.py:1428` `DEFAULT_CLASSIFY_MODEL` | `still accepted` | Dated form is the documented full id for Haiku 4.5; catalog prefers the bare alias but both resolve |
| `--output-format json`, `--system-prompt`, `-p`, `--model` | `provider.py::_build_argv` | `still accepted` | Verified against `claude --help` on CLI **2.1.223**, this host, 2026-08-06 |
| `--strict-mcp-config` | **absent** from `_build_argv` | `still accepted` (flag exists; not used) | Confirms athenaeum#775 is buildable on the installed CLI — see §E |

**External-contract findings present:** yes — L1 and L4 (Gate 3.1 declaration satisfied).

### D.2 Spend ceilings

| Ceiling | Resolver | Unit | Present |
|---|---|---|---|
| Per-run subscription | `resolve_spend_max_tokens_per_run` | tokens | yes |
| Per-day subscription | `resolve_spend_max_tokens_per_day` | tokens | yes |
| Per-run API | `resolve_spend_max_usd_per_run` | USD | yes |
| Per-day API | `resolve_spend_max_usd_per_day` | USD | yes |
| % of subscription | — | — | **no — L4** |

All four are strictly opt-in (unset = no ceiling), and the unit split by billing path is correct: subscription rows can never contribute to a dollar total.

### D.3 Direct SDK construction sites

Exhaustive — 2 sites, both enumerated (no sampling).

| Site | Verdict |
|---|---|
| `provider.py::build_llm_client` | Correct — this is the seam |
| `scripts/measure_contradiction_baseline.py:82` | **BUG (L7)** — bypasses the seam |

---

## E. Relationship to the existing issues

**athenaeum#775** (`--strict-mcp-config`) — **verified buildable at this SHA.** The flag exists in the installed Claude Code **2.1.223** (`claude --help` confirms `--strict-mcp-config  Only use MCP servers from --mcp-config`), and `_build_argv` does not currently emit it. Decision-free, keeps the nightly at $0, and independent of athenaeum#774. Nothing in this audit blocks it. It is the cheapest item on the board.

**athenaeum#774** (route nightly to `api`) — correctly `needs-decision`; I did not resolve the spend question. One interaction worth recording: **if athenaeum#774 is ever accepted, L1 and L2 stop being latent and become live.** On the subscription path `estimated_cost_usd` is $0 by construction and the dollar ceilings never engage, so a mispriced model is currently a reporting defect only. On the `api` path those same ceilings become the real spend guard, and a 6.67x under-report disarms them. **L1 should be treated as a prerequisite of athenaeum#774**, not merely as adjacent cleanup.

Neither issue overlaps any finding above; this audit adds to that board rather than restating it.

---

## E2. Addendum — second pass (new requirements, 2026-08-06)

Triggered by four operator requirements added after the first pass: weekly-token-limit-derived percentage ceiling, sidecar cost inclusion, per-architectural-area provider routing, and an issue-graph contradiction review.

### L9 — The test suite writes to the operator's LIVE spend ledger

| | |
|---|---|
| **Severity** | **High** |
| **Category** | cost-accounting / test-isolation |
| **Oracle** | `internal-consistency` |
| **Effort** | S |
| **Remedy** | DIRECTION |

**Location:** `tests/test_config_parity.py` — fixture model ids `yaml-topic-model`, `yaml-classify-model`, `yaml-write-model` at lines 417, 459, 540.

**Finding.** Those literal fixture strings appear as `models` values in **30 rows of the live ledger** at `~/.cache/athenaeum/spend.jsonl`, spanning **2026-07-15T17:03:05Z through 2026-08-02T21:25:32Z**. Full enumeration by run type: 15 `query-topics` rows (each `estimated_cost_usd = 1.1e-05`) and 15 `librarian` rows (each `0.0`). These tests do not override `ATHENAEUM_SPEND_LEDGER` / `ATHENAEUM_CACHE_DIR`, so `resolve_ledger_path` resolves to the operator's real cache dir and `record_spend` appends there.

**Why it matters.** Three distinct harms, all bearing on cost governance:

1. **Synthetic rows inflate real totals.** `athenaeum spend` and the good-morning sub-skills sum `estimated_cost_usd` across rows; ~$0.000165 of fabricated API spend is currently counted as real. Small today — but the mechanism has no bound, and it is running every time the suite runs.
2. **They are permanently unpriceable.** `yaml-topic-model` matches no rate prefix, so it prices at the blended fallback and always will. A repricing pass (L3) can never resolve them.
3. **It contaminates the per-day ceiling.** `spend_today()` sums ledger rows since UTC midnight. A test run on the same UTC day as a real run contributes to the ceiling that guards real spend — so a full local suite run (~12.5 min per the operator's notes) can move a production guardrail.

This is the exact inverse of the operator's stated concern about sidecar costs disappearing: here, costs that never happened are *appearing*.

**Remedy (DIRECTION).** Point the spend ledger at `tmp_path` in these tests (the seam already exists — `resolve_ledger_path` honours `ATHENAEUM_SPEND_LEDGER`, and `record_spend` takes an explicit `ledger_path`). Add an autouse fixture redirecting `ATHENAEUM_CACHE_DIR` for the whole suite so this cannot recur in a future test. Separately, decide whether to purge the 30 known-synthetic rows from the live ledger — an operator action, not a build one, and the ledger is append-only by design.

**Settling check:** run the suite with the live ledger's mtime recorded before and after; it must not change.

### L10 — The reasoning-tier knobs are inert **by default**, but the tiers are wired — CORRECTED

| | |
|---|---|
| **Severity** | Low |
| **Category** | configuration-hygiene |
| **Oracle** | `internal-consistency` |
| **Effort** | S |
| **Remedy** | DIRECTION |

> **Correction.** An earlier draft of this finding (and athenaeum#234's body) described `ATHENAEUM_REASONING_T1_MODEL` / `T2_MODEL` as simply "dead." That is **wrong**, and `reasoning_tiers.py`'s docstring explicitly warns against the claim: *"Do not describe this module as having 'no production caller' (stale as of athenaeum#518) and do not describe T2 as 'unwired' (stale as of athenaeum#602)."* The corrected statement is below.

**Location:** `src/athenaeum/reasoning_tiers.py:1275` `"DEFAULT_TIER_CHAIN: tuple[TierHandler, ...] = ()"`

**Finding.** `DEFAULT_TIER_CHAIN` is genuinely empty, so nothing reaches the tiers *through the pipeline's default path*. But both tiers have real production callers in `merge.py` that bypass that default — `t1_screen_rejects_merge_proposal` (athenaeum#518) builds an explicit one-element chain, and `t2_screen_merge_proposal` (athenaeum#602) calls `run_t2_tier` directly. Both are gated behind the single `resolve_reasoning_tier_auditing_enabled` flag (`ATHENAEUM_REASONING_TIER_AUDITING_ENABLED`), **which defaults OFF**.

So the accurate statement is: the knobs are **inert in the default configuration, and live the moment the auditing flag is turned on.** They are opt-in, not dead.

**Why it matters.** The practical defect is only that an operator setting `ATHENAEUM_REASONING_T1_MODEL` without also setting the auditing flag gets silence rather than a signal. But the correction matters more than the defect: athenaeum#609 (`ready`) proposes retrofitting M17 validation onto T1/T2, and on the "dead code" reading that work looks pointless. It isn't — athenaeum#609's premise is sound.

**Remedy (DIRECTION).** Warn at resolution time when a reasoning-tier model knob is set while the auditing flag is off. Separately, correct athenaeum#234's body, which still carries the stale "currently has no effect" claim.

### L11 — The recall sidecar's LLM path is gated on `ANTHROPIC_API_KEY`, bypassing the provider config

| | |
|---|---|
| **Severity** | Medium |
| **Category** | configuration-hygiene / provider-routing |
| **Oracle** | `internal-consistency` |
| **Effort** | S |
| **Remedy** | DIRECTION |

**Location:** `code-workspace-config/scripts/hooks/knowledge-recall-on-turn.sh` — the topic-extractor gate:

```sh
if [ -x "$ATHENAEUM_CLI" ] && [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  LLM_TOPICS=$("$ATHENAEUM_CLI" query-topics "$PROMPT" --timeout 3 2>/dev/null || echo "")
fi
```

**Finding.** The hook decides whether to make the LLM call by testing for `ANTHROPIC_API_KEY` **in the shell**, before `athenaeum` is invoked at all. But `query_topics` routes through `build_llm_client`, which honours `llm.provider` — and under `provider: claude-cli` it needs **no API key**, authenticating via the ambient Claude Code login.

**Consequence:** with the documented nightly configuration (`llm.provider: claude-cli`, no `ANTHROPIC_API_KEY` exported), the hook **silently skips the LLM topic extractor entirely** and falls back to the regex + stopword extractor — even though the subscription backend would have served it at $0. The shell gate and the provider config disagree, and the shell gate wins because it runs first.

The degradation is invisible by design: the regex fallback returns plausible terms, so recall still works, just with worse named-entity rescue — exactly the case `query_topics`' own docstring says the LLM extractor exists to fix (*"rescues named-entity recall on instruction-heavy prompts where the regex extractor would miss buried proper nouns"*).

**Remedy (DIRECTION).** Drop the `ANTHROPIC_API_KEY` test from the hook and let `athenaeum query-topics` decide — it already returns empty (falling back to regex) when no client can be built, which is the same outcome the gate was reaching for, but computed with the config actually in force.

**Settling check:** with `provider: claude-cli` and `ANTHROPIC_API_KEY` unset, confirm a `query-topics` row appears in the ledger tagged `billing_mode: subscription`. None exists today.

### L12 — Passive recall has been disabled since 2026-07-14 (operator state, not a code defect)

| | |
|---|---|
| **Severity** | Informational — operator state |
| **Category** | operability |
| **Oracle** | `external-contract` (checked against the live runtime, not the repo) |

**Finding.** All three athenaeum hooks in `~/.claude/settings.json` are stubbed to `true # DISABLED 2026-07-14 (athenaeum off, Tristan)`, and `settings.local.json` defines none:

| Hook | Disabled script | What it does |
|---|---|---|
| `SessionStart` | `knowledge-build-index.sh` | Builds the FTS5/vector index the recall hook queries |
| `UserPromptSubmit` | `knowledge-recall-on-turn.sh` | **Passive recall** — the per-turn wiki-context injection |
| `SessionEnd` | `knowledge-rebuild-index.sh` | Rebuilds the index |

This is a deliberate operator action, not a regression — recorded because it is load-bearing context for every cost figure in this audit, and because the effect is invisible from inside the repo.

**Consequences:**

1. **Passive memory has not run for ~3.5 weeks.** The `recall` MCP tool still works (agent-initiated), so knowledge is reachable on request; what is off is the automatic per-turn surfacing.
2. **Sidecar LLM spend is structurally zero**, which resolves the operator's concern about those costs disappearing: they are not being lost in accounting, they are not being incurred.
3. **It corroborates L9.** Ledger provenance for `run_type="query-topics"`: every row carries `session_id=None` except 35 on 2026-08-02 under session `d5774338-7d8` — and **5 of those 35 are `yaml-topic-model`**, a test fixture. A test suite ran inside a live Claude Code session and inherited its session id. With the hook disabled since 2026-07-14 and no other caller of `extract_topics` outside `_cmd_query.py`, **effectively all ~96 `query-topics` and `yaml-*` ledger rows are test-generated.** No genuine passive-recall spend is recorded because none occurred.
4. **The repo's own operating instructions are stale against this.** The user-level `CLAUDE.md` describes the SessionStart hook as live (*"A lightweight SessionStart hook injects a few wiki page names"*); it does not.

**Not a remedy — an operator decision:** whether to re-enable. Note L11 first: re-enabling with `provider: claude-cli` and no `ANTHROPIC_API_KEY` restores passive recall but leaves the LLM topic extractor silently skipped.

---

## G. Issue graph as filed (2026-08-06)

All edges are **native GitHub `blocked_by`** relations, not prose — hestia's `dependency-gather.ts` reads the native relation and is blind to free text.

| # | Finding | Labels | `blocked_by` |
|---|---|---|---|
| **776** | L9 — test suite writes to the live spend ledger | `bug` `moscow:must` | — |
| **777** | L1 — Fable/Mythos missing from the rate table (6.67x under-report) | `bug` `moscow:must` | — |
| **778** | L5 — type 19 call sites to `LLMBackend` | `chore` `moscow:should` | — |
| **779** | Document reasoning-tier screening; default stays OFF | `docs` `moscow:should` | — |
| **780** | L7+L8+L10 — three hygiene fixes | `chore` `moscow:could` | — |
| **781** | Per-knob cost attribution (`tokens_by_knob`, ledger v3) | `feature` `moscow:should` | 776 |
| **782** | L6 — decouple `_retry.py` from Anthropic SDK types | `feature` `moscow:should` | 778 |
| **783** | L2 — config-owned pricing + preflight fail-loud | `feature` `moscow:should` | 777 |
| **784** | Pre-enable reasoning-tier baseline | `chore` `moscow:must` `~operator` | — |
| **785** | Weekly token limit + max-%-per-day ceiling | `feature` `moscow:should` | 783, 781 |
| **786** | Per-knob provider routing | `feature` `moscow:should` | 778, 782, 781 |
| **787** | Enable T1/T2 locally + one-week measurement | `chore` `moscow:should` `~operator` | 784, 781 |
| **788** | L3 — `athenaeum spend --reprice` | `feature` `moscow:could` | 783 |
| **789** | Re-enable the Claude Code hooks (passive recall) | `chore` `moscow:must` `~operator` | cwc#2177 |
| **cwc#2177** | L11 — hook gates the extractor on `ANTHROPIC_API_KEY` | `bug` `moscow:must` | — |
| **234** | Re-scoped `moscow:wont` -> `moscow:should` + `epic`; correction commented | `feature` `moscow:should` `epic` | — |

**Unblocked and buildable now:** 776, 777, 778, 779, 780, plus the pre-existing athenaeum#775. **Operator-gated:** 784, 787, 789.

### Decisions recorded during the graph pass

| Decision | Outcome |
|---|---|
| Pricing: overlay vs replace | **Replace** — yaml authoritative, unpriced model fails at preflight. Chosen over overlay because plain replace has *two* sources of truth (yaml, then the fabricated blended average) and the second errs downward, disarming ceilings. Deleting that fall-through is what makes single-source safe. |
| % -of-subscription denominator | **Operator-declared** `spend.weekly_token_limit`; daily ceiling = `weekly/7 * pct`. No readable subscription quota exists to derive one. |
| Reasoning tiers default | **Stays OFF.** A T2 safe-class approve calls `resolve_merge(auto_applied=True)` (`merge.py:1408`) — merges written with no human review. Acceptable as an informed opt-in; not as a shipped default for an Apache-2.0 package. Documented with a "enable when your merge queue outgrows triage" recommendation instead. |
| Provider routing granularity | **Per knob** (6 knobs). Recorded limitation: `classify` is shared by `tiers.classify`, `contradictions.detect_system`, and `claim_kind`, so detector-level routing needs that knob split first. |
| athenaeum#774 | **Held** pending the provider decision — its premise (Anthropic-api vs Anthropic-subscription) shifts if the librarian moves providers. athenaeum#777 wired as a prerequisite regardless. |

---

## F. What I did not examine

Eleven rows, one per dimension, per the audit contract.

| # | Dimension | Coverage |
|---|---|---|
| 1 | Requirements & intent | `covered` — each audited module's contract is stated in its docstring and each was checked against behavior; the provider/spend/models factoring rules are explicit and held. |
| 2 | Architecture & modularity | `partial` — the LLM seam's import/call graph was traced end to end (L5, L6, L7). The wider repo graph was not; `librarian.py` (215KB), `tiers.py` (147KB), and `resolutions.py` (131KB) are god-file candidates that I did not assess as such. |
| 3 | Dead & vestigial | `skipped` — out of the declared scope. Note Python's dynamic dispatch would weaken any dead-code claim here regardless. |
| 4 | Configuration hygiene | `partial` — every LLM/spend/model knob was inventoried (§C, §D.2) and the one unconfigurable value found (L2). The non-LLM config surface (`config.py` is 92KB) was not inventoried, and no full env-var census was run. |
| 5 | AI/LLM-specific | `partial` — model ids, token limits, sampling params, thinking posture, cost accounting, timeouts, and fallbacks all covered; every literal fed into the §D.1 census. **Prompt content, prompt versioning, and prompt-behavior regression tests were explicitly out of scope** and are unexamined. |
| 6 | Error handling | `partial` — the retry path (L6), CLI transient classification, envelope parsing, and the deliberately-swallowed ledger write (do-not-touch) were traced. Error handling outside the seam was not. |
| 7 | Security & secrets | `partial` — credential handling in the seam was reviewed and is sound: the user prompt goes on stdin, never argv, so notes never appear in `ps` (athenaeum#543); raw model output is redacted before landing in an error message; the ledger records counts only, never content. **No history scan for committed secrets and no dependency-vulnerability check were run.** |
| 8 | Data & state | `partial` — the spend ledger's storage model (append-only JSONL, `O_APPEND` + fsync, torn-trailing-line tolerance) was reviewed and is sound. The knowledge corpus and vector store were not examined. |
| 9 | Tests | `skipped` — no test-quality assessment. Each finding names its own settling check instead. I did not verify whether existing tests would catch L1. |
| 10 | Operability | `partial` — the ledger's good-morning consumers were confirmed to exist and read the right file. **Gate 6 (deployed-artifact drift) was checked and is CLEAN** — see below. CI gates and branch protection were **not** checked. |
| 11 | Consistency | `partial` — the two prefix tables' documented "maintain together" invariant was checked and found violated (L1). No broader style/type-suppression census was run. |

### Gate 6 — deployed artifact vs audited SHA

The librarian and MCP server run from a **main-pinned `~/local-deploys/athenaeum` checkout**, not this dev tree, so the audited SHA is not automatically what is running. Checked rather than assumed:

```
git log main..develop --oneline -- models.py spend.py provider.py _retry.py query_topics.py measure_contradiction_baseline.py
  -> (empty)
```

Per-file content hashes of `main` vs `develop` are **IDENTICAL** for `models.py`, `spend.py`, `provider.py`, and `_retry.py`. **No drift.** Every finding and every line number above therefore holds for the deployed copy as well as the dev tree — in particular, L1's mispricing is live in the running librarian, not merely pending on develop.

**Absence claims made, and their namespace coverage** (Gate 4):

- *"No `claude-fable-5` entry in the rate table"* (L1) — searched `src/athenaeum/models.py` in full plus a `fable|mythos` regex across `src/**/*.py`; zero hits outside the sampling table. Tracked files only; the working tree is clean at this SHA, so tracked and working-tree are identical here. **Confident.**
- *"No direct `anthropic.Anthropic(` construction outside the seam except one script"* (L7) — regex sweep over `src/**/*.py` and `scripts/**/*`. Did not cover `tests/`, where a direct construction would be legitimate anyway. **Confident within the stated namespace.**
- *"No percentage-of-subscription ceiling exists"* (L4) — read `ceiling_tripped()` in full and swept `config.py` for `resolve_spend_*`. **Confident.**
- *"No readable subscription quota denominator exists"* (L4) — verified only against the CLI JSON envelope fields athenaeum parses and the ledger schema. **NOT verified** against undocumented Claude Code internals or any Anthropic account API. Filed as an open question at the lowest severity; per Gate 4 it is **not** treated as a prerequisite of any other finding.
- *"No repricing path consumes `tokens_by_model`"* (L3) — swept `src/` and the CLI subcommand registration for `reprice`. Did not check whether an external consumer (e.g. a cwc script) does its own repricing. **Moderate confidence.**
