# Athenaeum documentation

Everything in `docs/` is reachable from this page.

| If you are… | Start here |
|---|---|
| **Evaluating** — deciding whether to care | [Why Athenaeum](why-athenaeum.md), then the [README](../README.md) quick start |
| **Operating** — running it | [Guides](#guides) and [Reference](#reference) |
| **Extending** — writing an adapter | [Extending](#extending) |
| **Excavating** — why is it like this | [Design records](#design-records) |

---

## Start here

- [**Why Athenaeum**](why-athenaeum.md) — the problem, the four questions a production
  memory has to answer, how this differs from Claude's built-in memory, Anthropic's memory
  tool, RAG, and the agent-memory libraries.
- [**North star**](north-star.md) — the purpose and operating principles every design
  decision is checked against, including what the project deliberately does not do.

## Guides

Task-shaped. *"I want to ___."* Each guide names the module page that backs it.

- [Install](guides/install.md) — Python 3.13+, extras, first `init`.
- [Daily operation](guides/daily-operation.md) — `run`, `ingest`, `reindex`, `session-end`,
  `status`, and which one to reach for.
- [Answering decisions](guides/decisions.md) — working the pending-questions and
  pending-merges queue.
- [Sidecar](guides/sidecar.md) — passive recall on every turn.
- [Claude Code integration](guides/claude-code.md) — wiring the MCP server and the example hooks.
- [Vector search](guides/vector-search.md) — optional embedding backends and query-topic extraction.
- [Upgrading](guides/upgrading.md) — data lifecycle and what a version bump does to an existing wiki.
- [Troubleshooting](guides/troubleshooting.md) — symptom → cause.

## Modules

One page per component, each answering the same four questions: **what it does, what it
reads, what it writes, what it refuses.** The last one is the section to read before
integrating anything.

- [Intake](modules/intake.md) — append-only raw, the observation filter, tier-0 passthrough.
- [Librarian](modules/librarian.md) — the compile pipeline, the run lock, the spend ceiling.
- [Corrections](modules/corrections.md) — the armed-attribute allowlist and every way a
  correction is refused.
- [Shape](modules/shape.md) — shape rules, claim kinds, provenance shape.
- [Routing](modules/routing.md) — entity routing and classification.
- [Conflicts](modules/conflicts.md) — contradiction detection, source precedence, merges,
  the decision queue.
- [Retention](modules/retention.md) — decay, the `preserve` disposition, preserved logs.
- [Recall](modules/recall.md) — the query path, FTS5 and vector backends, enumerate vs recall.
- [Sensitivity](modules/sensitivity.md) — access levels, fail-closed audience scoping, PII exclusion.
- [MCP surface](modules/mcp.md) — the 15 tools.
- [Provider](modules/provider.md) — the `api` and `claude-cli` backends.

## Reference

- [Configuration](reference/configuration.md) — every configurable key.
- [Exit codes](reference/exit-codes.md).

> **Not yet generated.** A CLI reference and a config reference generated from the
> `resolve_*` functions with a CI staleness check are planned; today
> `reference/configuration.md` is hand-maintained.

## Extending

Contracts an adapter or storage author must satisfy.

- [Adapter contract](extending/adapter-contract.md)
- [Storage adapter contract](extending/storage-adapter-contract.md)
- [Sidecar adapter contract](extending/sidecar-adapter-contract.md)
- [Store contract](extending/store-contract.md)
- [Whole-store adapter design](extending/whole-store-adapter-design.md)
- [Tier-0 bounce-note contract](extending/tier0-bounce-note-contract.md)
- [Authority manifest](extending/authority-manifest.md)
- [Authorized reader contract](extending/authorized-reader-contract.md)
- [Source handles](extending/source-handles.md)

## Design records

The *why*. These are decision records — read them when a module page raises a question it
does not answer.

- [Memory taxonomy](design/memory-taxonomy.md)
- [Provenance shape](design/provenance-shape.md)
- [One way in, one way out](design/one-way-in-one-way-out.md) — the two-path invariant.
- [Field corrections](design/field-corrections.md)
- [Shape rules](design/shape-rules.md)
- [Routing](design/routing.md)
- [Conflict resolution](design/conflict-resolution.md)
- [Contradiction detection](design/contradiction-detection.md)
- [Auto-resolve](design/auto-resolve.md)
- [Merge inflow restoration](design/merge-inflow-restoration.md)
- [Recall architecture](design/recall-architecture.md)
- [Prompts](design/prompts.md)
- [Sensitivity class vocabulary](design/sensitivity-class-vocabulary.md)
- [Sensitivity value routing](design/sensitivity-value-routing.md)
- [Security posture](design/security-posture.md)
- [Bounce surface convergence](design/bounce-surface-convergence.md)
- [Deprecated email tracking](design/deprecated-email-tracking.md)

## Measurements

Evidence, kept out of the reading path.

- [Evals inventory](measurements/evals-inventory.md)
- [Memory-model measurements](measurements/memory-model-measurements.md)
- [Reasoning-tier measurements](measurements/reasoning-tier-measurements.md)
- [Retrieval entry-point measurements](measurements/retrieval-entry-point-measurements.md)
- [Deploy SHA stamp](measurements/deploy-sha-stamp.md)
- [LLM provider cost audit](measurements/audits/2026-08-06-llm-provider-cost-audit.md)

---

## Limitations

Stated plainly, and in one place.

- **Anthropic only.** The provider seam has two backends, the Anthropic API and the `claude`
  CLI. There is no OpenAI, local-model, or Ollama backend. The seam is extensible; no other
  provider ships. → [provider](modules/provider.md)
- **Single-owner, not multi-tenant.** Audience scoping is a read filter the operator pins at
  serve time. It is not a multi-user ACL, and there is no per-user memory isolation.
  → [sensitivity](modules/sensitivity.md)
- **The librarian is a batch job.** Compilation is nightly by default; `session-end` closes
  the same-day gap. There is no streaming write path to the wiki.
  → [librarian](modules/librarian.md)
- **Storage is markdown on disk.** The storage-adapter seam exists and resolves each entity
  class to a backing store and corpus policy, but only two adapters ship — the default
  markdown surface and an `excluded` surface for PII. No database-backed wiki exists in-tree,
  and only one corpus-policy bit has a real call site.
  → [storage adapter contract](extending/storage-adapter-contract.md)
- **Reasoning-tier screening is off by default.** The T1 and T2 screens sit behind separate
  opt-ins, both off. T2 can auto-apply a safe-class merge with no human review, which is why
  it stays opt-in. → [conflicts](modules/conflicts.md)
- **The observation filter does not tune itself.** It is a prompt fragment you edit. The
  shipped default's "Pattern Detection" and "Decay Rules" sections describe behavior that is
  not implemented. → [intake](modules/intake.md)
