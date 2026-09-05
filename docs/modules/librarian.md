# The librarian

**Reference page.** For the task-shaped version, see
[Claude Code integration](../guides/claude-code.md).

## What it does

`athenaeum run` is the compile pipeline — the only path that turns raw intake into wiki
pages. It walks discovered raw files through tiered phases: Tier-0 passthrough, Tier-1
programmatic matching against known entities, Tier-2 LLM classification of new entities,
Tier-3 LLM-driven merge into existing pages, and auto-memory clustering, with contradiction
detection, correction application, and pending-merge resolution running alongside. A single
run is one `RunContext`: one `TokenUsage` budget, one per-run API-call ceiling, one wall-clock
deadline, and one git snapshot lineage, threaded through every phase.

Every mutating command — `run`, `ingest`, `session-end`, `reindex`, `drain`, and a handful
of `--apply` maintenance commands — acquires the same **run lock** before touching
`wiki/` or the pending-decision sidecars, so two invocations (a cron job and a manual
`athenaeum run`) can never interleave writes.

## What it reads

- `raw/` and its configured extra intake roots, via `discover_raw_files` /
  `discover_auto_memory_files` (see [intake](intake.md)).
- `wiki/` — the existing corpus each Tier-3 merge and each contradiction check compares
  against.
- `librarian.max_api_calls` / `ATHENAEUM_MAX_API_CALLS` / `--max-api-calls` — the per-run
  spend ceiling (env > yaml > default 800, CLI flag wins over both). It is a run-level
  budget: one `TokenUsage` is created at run start and threaded through the
  cluster/merge/reresolve phases, so their combined spend counts against one number.
- `librarian.reasoning_tier_auditing_enabled` / `librarian.reasoning_tier_t2_auto_apply_enabled`
  — the two independent opt-ins that arm the T1/T2 reasoning screens (see below).
- `wiki/_schema/observation-filter.md` / `_entity-template.md` — the operator-tunable
  fragments Tier-2's classify/create prompts read at call time (see
  [intake](intake.md)'s observation-filter section).
- `.athenaeum.lock` under the knowledge root — the run-lock file the CLI reads to report a
  contended holder's PID, acquire time, and heartbeat age.

## What it writes

- New and updated pages under `wiki/`, plus the pending-decision sidecars
  (`wiki/_pending_questions.md`, `wiki/_pending_merges.md`) a phase raises a question or
  merge into.
- A **git commit before processing starts** — `FilesystemStore.snapshot("librarian:
  pre-processing snapshot")` stages and commits the whole knowledge root immediately before
  the entity-writing phase begins, so a run's starting state is always a clean, recoverable
  checkpoint. Further snapshot commits land on a wall-clock-deadline stop, a signal
  (`SIGTERM`/`SIGINT`) received mid-run, and normal completion — each labeled with what
  triggered it and how far the run got (files processed, created/updated/escalated counts).
  A commit that has nothing staged is a no-op (`snapshot` returns `None`), not an error.
- `.athenaeum.lock`, refreshed by a background heartbeat thread roughly every 30 seconds
  while the lock is held, independent of whatever phase the run loop is doing.
- The `librarian-run-summary` log line, one per run, carrying `schema_fragments=`,
  `prompt_manifest=`, `zero_yield=`, and a per-phase breakdown.

## What it refuses

- **Two mutating commands never run concurrently on one machine.** `RunLock.acquire()` fails
  fast with `LockHeld` (naming the holder's PID, host, acquire time, and last heartbeat) when
  another run already holds `.athenaeum.lock`, unless the caller passes `--wait <seconds>` to
  block or `--force` to break a lock it believes is held by a dead or hung process. A lock
  whose holder has died is never actually contended — the kernel releases the `flock` the
  instant the holder exits — so `--force` exists specifically to override a *live but hung*
  holder, never a genuinely stale one.
- **The API-call ceiling stops the run, not the process.** When `usage.api_calls` reaches
  `max_api_calls` mid-file, the entity phase logs "API call budget exhausted" and defers
  every remaining raw file to the next run rather than erroring out. Setting the ceiling to 0
  is accepted but logged loudly as a likely misconfiguration ("API budget is 0 — all LLM
  tiers deferred this run").
- **A wall-clock deadline stops the run at a file boundary,** independent of the call budget,
  exiting with a distinct resumable status rather than silently truncating output.
- **The reasoning-tier screens are OFF by default, independently of each other.** T1
  (cheap-model reject/pass-up screening ahead of the human merge queue) is gated by
  `reasoning_tier_auditing_enabled`; T2 (deep-reasoning auto-apply) is gated by its own
  `reasoning_tier_t2_auto_apply_enabled`. An unconfigured install sees every merge proposal
  reach the human decision queue exactly as if this module did not exist.
  - **T1 can only reject or pass up — never approve.** Its decision type has no field value
    that means "approved"; approval is structurally unrepresentable in what T1 returns.
  - **T2 can auto-apply a merge with no human review, but only inside a narrow, structurally
    enforced safe class**: every source page shares the same `memory_class`, there are at
    most 3 source pages, none carries a truthy `pii` flag, and none has `memory_class:
    axiom`. `safe_class_violation()` is the single gate consulted before an "approve" verdict
    can even be constructed — a violation forces the outcome to `escalate` regardless of what
    the model itself returned, and a response that pairs "approve" with rewritten content
    (`drafted_body`) is downgraded to `draft`, never surfaced as an approval. Rewrite-then-
    self-approve is unrepresentable in the decision type itself (an `approve` verdict can
    never carry a `drafted_body` or a `safe_class_violation` value). Every tier decision —
    reject, pass-up, approve, amend, draft, or escalate — is appended to one queryable JSONL
    log regardless of outcome.
  - A malformed or schema-violating model response at either tier fails to its
    least-authority fallback (T1: `pass_up`; T2: `escalate`) rather than being coerced into
    an approval.
- **A restricted MCP audience cannot touch this at all** — see [mcp](mcp.md)'s refusal
  section for the decision-queue mutators this module's output ultimately feeds.

## See also

- Guides — [Claude Code integration](../guides/claude-code.md)
- Modules — [intake](intake.md) · [corrections](corrections.md) · [mcp](mcp.md) · [provider](provider.md)
- Design — [conflict resolution](../design/conflict-resolution.md) · [auto-resolve](../design/auto-resolve.md) · [contradiction detection](../design/contradiction-detection.md)
- Reference — [configuration](../reference/configuration.md) · [exit codes](../reference/exit-codes.md)
