**Reference:** [recall](../modules/recall.md) · [mcp](../modules/mcp.md)

# Troubleshooting

A symptom-first index. Find what you're seeing, then follow the link.

## Recall and the sidecar

| Symptom | Check |
|---|---|
| Session message shows `0 wiki pages` | `$KNOWLEDGE_ROOT/wiki/` is empty or unreadable — if `raw/` has files, run `athenaeum run` |
| `remember` saves but `recall` finds nothing | Raw observations only compile to wiki when `athenaeum run` (or `ingest`/`session-end`) fires. Check `ls ~/knowledge/raw/` for pending files, then run `athenaeum run --path ~/knowledge`. See [Daily operation](daily-operation.md). |
| No context injected on user turns | Run `sqlite3 ~/.cache/athenaeum/wiki-index.db 'select count(*) from wiki'` — should be > 0 |
| Vector backend silent | Re-run with `ATHENAEUM_HOOK_DEBUG=1` — usually `pip install 'athenaeum[vector]'` is missing. See [Vector search](vector-search.md). |
| `query-topics` not returning topics | Under the default Anthropic provider: `cat ~/.cache/athenaeum/config.env` — should contain `ANTHROPIC_API_KEY=...`. Under `llm.provider: claude-cli`, no key is needed — re-run with `ATHENAEUM_HOOK_DEBUG=1` instead. |
| Hook ran "green" but recall never fires | Check the settings snippet was merged correctly: `grep UserPromptSubmit ~/.claude/settings.json`. See [Passive recall via hooks](sidecar.md). |
| A hook seems to ignore an `athenaeum.yaml` change | `AUTO_RECALL` / `SEARCH_BACKEND` exports in your shell profile beat the cached config — check your shell environment before the yaml. |

## Claude Code auth

| Symptom | Check |
|---|---|
| `401 OAuth authentication is currently not supported` from the Anthropic API | Claude Code's own `CLAUDE_CODE_OAUTH_TOKEN` is scoped to its inference endpoint and is rejected by the Messages API. The pipeline and example hooks need a separate console API key — see the 1Password bootstrap pattern in [Recall architecture](../design/recall-architecture.md#anthropic_api_key-bootstrap-sessionstart). |

## Runs, budgets, and exit codes

| Symptom | Check |
|---|---|
| A run ended with `Done (DEGRADED — budget exhausted)` | The run hit its API-call ceiling (`ATHENAEUM_MAX_API_CALLS`, default 800) or file batch cap and wrote `wiki/_deferred_work.md`. The deferred files stay on disk and are picked up automatically by the next run. A degraded run exits `0` by default; pass `--strict-budget` for exit-code-based alerting. See [Upgrading](upgrading.md) and [reference/exit-codes.md](../reference/exit-codes.md). |
| A script needs to distinguish "graceful stop" from "was killed" | Both used to collapse to exit `124`. They no longer do: `75` is athenaeum's own internal wall-clock deadline (resumable, partial progress committed) or the run lock being held by another process; `124` is reserved for an externally-delivered kill signal. Full contract: [reference/exit-codes.md](../reference/exit-codes.md). |
| Two overlapping invocations (nightly cron + manual) seem to race | `run`, `ingest`, `reindex`, and `session-end` are single-flight against each other on the same knowledge root via a shared run lock — one waits or exits `EXIT_LOCK_HELD` rather than racing wiki writes or the budget. |

## Deploy freshness

| Symptom | Check |
|---|---|
| Unsure whether the athenaeum process actually running is behind `main` | Read the commit SHA stamp Athenaeum writes on every build/deploy — see [Deploy-SHA stamp](../measurements/deploy-sha-stamp.md) for the file's location and format, and how the fleet deploy-lag tooling reads it. |

## Known trade-offs (not bugs)

These are intentional for the current release line, not things to debug:

- **No published retrieval benchmarks.** The hybrid-search claim rests on
  concrete failure modes and production use, not a benchmarked recall@k
  against other memory tools.
- **FTS5 index rebuilds are non-atomic and unlocked** against the example
  shell hook specifically (the CLI commands themselves are single-flight
  via the run lock). The race window is small and single-user wikis don't
  hit it in practice.
- **The `keyword` search backend is a scan-on-query fallback** — it reads
  every wiki page on every query. Fine under ~1,000 entities, painful past
  that. Use `fts5` (the default) or `vector` for any non-trivial wiki.
- **Pending decisions are a file, not a workflow.** Conflicts land in
  `wiki/_pending_questions.md`; there's no PR-opening or chat integration
  — see [Answering pending decisions](decisions.md).

## See also

- Guides — [Daily operation](daily-operation.md) · [Passive recall via hooks](sidecar.md) · [Vector search](vector-search.md) · [Upgrading](upgrading.md) · [Answering pending decisions](decisions.md)
- Modules — [recall](../modules/recall.md) · [mcp](../modules/mcp.md)
- Design — [recall architecture](../design/recall-architecture.md) · [security posture](../design/security-posture.md)
- Reference — [exit codes](../reference/exit-codes.md) · [configuration](../reference/configuration.md)
- Measurements — [Deploy-SHA stamp](../measurements/deploy-sha-stamp.md)
