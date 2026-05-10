# Hook Drift Audit (cwc → athenaeum)

Snapshot 2026-05-10. Closes #129.

Athenaeum's `examples/claude-code/` ships an installable hook kit. The
maintainer's personal Claude Code workspace (`code-workspace-config`, "cwc")
runs an evolved fork of these scripts at `scripts/hooks/`. This audit
catalogues each cwc hook, marks generic-vs-personal, and records what was
ported back into the kit.

This document is the artifact future maintainers consult when deciding what
to lift on the next sync.

## Categorization

| cwc script | Status | Athenaeum equivalent | Notes |
|---|---|---|---|
| `knowledge-sidecar.sh` | **port-as-new** | `wiki-context-inject.sh` (NEW) | Headline generic improvement: cwd-keyword wiki injection at SessionStart, before any prompt. Stripped Tristan-specific symlink health check. Generalised skip-word list and result cap as `ATHENAEUM_INJECT_*` env vars. |
| `knowledge-rebuild-index.sh` | **port-as-new** | `rebuild-index.sh` (NEW) | Atomic dir-lock + stale-lock recovery + log file. Generic out-of-band rebuild for SessionEnd. Useful for large wikis where the synchronous SessionStart rebuild becomes painful. |
| `knowledge-build-index.sh` | **port-generic (partial)** | `session-start-recall.sh` | Ported the vector-index freshness fast-path (skip rebuild when index newer than wiki, override via `ATHENAEUM_FORCE_REBUILD=1`). Did NOT port: 1Password keychain bootstrap (`OP_SERVICE_ACCOUNT_TOKEN`), background-rebuild scheduler with stale-warning UI, or the read-only fast-path that depends on the rebuild hook being on disk — these intertwine with cwc-specific paths and a SessionEnd-rebuild assumption that not all users share. |
| `knowledge-recall-on-turn.sh` | **already-in-athenaeum** | `user-prompt-recall.sh` | Athenaeum's version is already the more polished one (mktemp tmp-file for vector stderr, `ATHENAEUM_HOOK_DEBUG`, `set -a` for child-process env). cwc's version is older. No port. |
| `knowledge-precompact.sh` | **already-in-athenaeum** | `pre-compact-save.sh` | Functionally identical. No port. |
| `gh-agent-session-setup.sh` | **stays-in-cwc** | — | gh-app token machinery; cwc-specific. |
| `session-debris-report.sh` | **stays-in-cwc** | — | Tristan-specific WIP-branch detection. |
| `persona-reinject.sh`, `persona-statusline.sh` | **stays-in-cwc** | — | Tristan persona system. |
| `dangerous-cmd-gate.sh`, `env-file-guard.sh`, `command-guard.sh`, `exploration-limiter.sh` | **stays-in-cwc** | — | Tristan-specific safety hooks. |
| `auto-memory-validate.sh` | **stays-in-cwc** | — | Tristan-specific validation. |

## What was ported in this PR

1. **`wiki-context-inject.sh`** (new). Source: `knowledge-sidecar.sh`. Cheap dependency-free cwd-keyword wiki injection. Generalised:
   - Hardcoded skip-words → `ATHENAEUM_INJECT_SKIP_WORDS` env var.
   - Hardcoded max results → `ATHENAEUM_INJECT_MAX_RESULTS` env var.
   - Dropped the broken-symlink health check (Tristan-specific multi-scope `~/.claude/projects/*/memory` topology).
   - Pairs naturally with `session-start-recall.sh`; both safe to wire as parallel `SessionStart` hooks.

2. **`rebuild-index.sh`** (new). Source: `knowledge-rebuild-index.sh`. Generic out-of-band index rebuild with:
   - `mkdir`-based atomic lock.
   - Stale-lock recovery (>1h reclaim).
   - Log file at `~/.cache/athenaeum/rebuild.log`.
   - Hybrid FTS5+vector when `SEARCH_BACKEND=vector` (FTS5 still rebuilt as secondary so user-prompt-recall.sh's hybrid merge keeps working).

3. **Vector-index freshness fast-path** in `session-start-recall.sh`. Source: `knowledge-build-index.sh` (the generic part of the freshness logic, decoupled from the cwc-specific config-cache and rebuild-scheduler dance). Skips the expensive (~45s) vector rebuild when the existing index is newer than the newest wiki page. FTS5 is unchanged (always rebuilt — it's cheap). Override via `ATHENAEUM_FORCE_REBUILD=1`.

4. **`settings-snippet.json`** updated to wire `wiki-context-inject.sh` alongside `session-start-recall.sh` at `SessionStart`, and to demonstrate the optional `SessionEnd` rebuild wiring.

5. **`README.md`** updated: new hook rows + new env-var rows.

## What was deliberately NOT ported

- **1Password Keychain bootstrap of `OP_SERVICE_ACCOUNT_TOKEN`**. Specific to Tristan's `op-box-claude` keychain entry naming; would require a configurable scheme that 99% of users will never need. Athenaeum's existing `op read` path is sufficient for users with `op` already signed in.
- **Background-rebuild scheduler with stale-warning UI**. Couples `session-start-recall.sh` to `rebuild-index.sh` being installed at a particular path. Users who want this can compose them manually in `settings.json`.
- **Read-only fast-path (don't build at session start, only check freshness)**. Same coupling concern. Users with very large wikis can opt into this manually by replacing the SessionStart hook with `rebuild-index.sh` + a SessionEnd trigger.
- **Broken-symlink health check** in `knowledge-sidecar.sh`. Tied to Tristan's per-scope memory symlink topology under `~/.claude/projects/*/memory/`. Not a general-purpose check.

Future syncs should re-run this audit against the cwc tree before assuming any newly-divergent hook is generic.
