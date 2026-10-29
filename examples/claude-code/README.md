# Claude Code integration

Seven hook scripts that wire Athenaeum into Claude Code as a transparent
recall sidecar — including `stop-hook-validate.sh`, a `Stop`-hook template
described in full under "Auto-memory integration" below. The scripts are
plain bash + sqlite3 + (optional) Python; nothing here is Claude-Code-specific
that couldn't be ported to another agent runtime.

| Hook                     | When it fires        | What it does                                                    |
|--------------------------|----------------------|------------------------------------------------------------------|
| `session-start-recall.sh`| Start of each session| Builds the FTS5 (and optional vector) index, caches config       |
| `wiki-context-inject.sh` | Start of each session| Cheap cwd-keyword grep — surfaces wiki pages relevant to the project being opened, before any prompt is submitted |
| `user-prompt-recall.sh`  | Each user turn       | Thin launcher that execs the packaged Claude Code adapter, which runs the hybrid FTS5+vector search and injects top-matching wiki page names |
| `pre-compact-save.sh`    | Before compaction    | Reminds the model to call `remember` on anything load-bearing    |
| `pending-questions-surface.sh` | Start of each session | Surfaces unresolved `_pending_questions.md` entries with a snooze cache |
| `rebuild-index.sh`       | SessionEnd (optional)| Out-of-band index rebuild with atomic dir lock — wire when synchronous SessionStart rebuild becomes painful (large wikis, vector backend) |
| `stop-hook-validate.sh`  | Stop (optional)      | Warns when auto-memory frontmatter is missing citation fields — see "Auto-memory integration" below |

## How the sidecar works (read this first)

Athenaeum has **two phases** and the hooks only handle one of them. Both
phases must run for `remember` → `recall` to close the loop:

1. **Intake (immediate, hook-driven):** when Claude calls `remember`, an
   observation lands in `~/knowledge/raw/claude-session/` as a timestamped
   markdown file. The hooks and MCP server write to `raw/`.
2. **Compile (batched, you schedule it):** `athenaeum run` reads pending
   `raw/` files, passes them through the tiered LLM librarian, and writes
   compiled entity pages to `~/knowledge/wiki/`. The hooks and `recall`
   only search `wiki/`.

There is no automatic bridge. If you `remember` five things and then
`recall` returns nothing, the observations are safe on disk — they just
haven't been compiled yet. Run `athenaeum run` manually, on a cron/launchd
timer, or at session end. See "Compile raw → wiki" below.

## Install

1. Initialise a knowledge base if you don't have one:

   ```bash
   pip install athenaeum
   athenaeum init --path ~/knowledge
   ```

2. Copy (or symlink) the scripts somewhere on disk and mark them executable:

   ```bash
   mkdir -p ~/.claude/hooks/athenaeum
   cp examples/claude-code/*.sh ~/.claude/hooks/athenaeum/
   chmod +x ~/.claude/hooks/athenaeum/*.sh
   ```

3. Merge `settings-snippet.json` into `~/.claude/settings.json`, replacing
   `/path/to/` with the directory from step 2.

4. (Optional, MCP remember/recall) Register the MCP server. Preferred
   — use the Claude Code CLI:

   ```bash
   claude mcp add --scope user athenaeum -- athenaeum serve --path ~/knowledge
   ```

   Or edit `~/.claude/settings.json` directly:

   ```json
   {
     "mcpServers": {
       "athenaeum": { "command": "athenaeum", "args": ["serve"] }
     }
   }
   ```

5. Restart Claude Code. The session-start message should say
   `[Knowledge] FTS5 index: N wiki pages`.

6. Schedule periodic compilation (`raw/` → `wiki/`). The hooks alone will
   never produce wiki pages — you need `athenaeum run` to compile the
   observations Claude saves via `remember`. Three common options:

   ```bash
   # Option A: run it manually whenever you want fresh entities
   athenaeum run --path ~/knowledge

   # Option B: daily cron (macOS/linux) — compile overnight
   echo "0 3 * * * /usr/local/bin/athenaeum run --path $HOME/knowledge" \
     | crontab -

   # Option C: launchd plist on macOS — see docs/ for a template
   ```

   A `remember` call without a subsequent `run` will appear to "work" but
   produce no `recall` hit. Check `~/knowledge/raw/` for pending files if
   recall surprises you.

## Smoke test

Verify the pipeline without a live session:

```bash
# 1. Build the index
bash examples/claude-code/session-start-recall.sh

# 2. Simulate a prompt (stdin is JSON)
echo '{"prompt":"tell me about innovation accounting","session_id":"test"}' \
  | bash examples/claude-code/user-prompt-recall.sh
```

Expected: a single-line JSON object with a `hookSpecificOutput` key listing
matching wiki pages. Empty output means either your wiki has no relevant
pages or the index hasn't been built — check `~/.cache/athenaeum/`.

## Environment variables

| Variable                 | Default                           | Purpose                                                       |
|--------------------------|-----------------------------------|----------------------------------------------------------------|
| `KNOWLEDGE_ROOT`         | `~/knowledge`                     | Knowledge base root                                            |
| `KNOWLEDGE_WIKI_PATH`    | `$KNOWLEDGE_ROOT/wiki`            | Wiki directory (if non-standard layout)                        |
| `ATHENAEUM_CLI`          | `athenaeum`                       | CLI binary (override for editable installs)                    |
| `ATHENAEUM_PYTHON`       | `python3`                         | Python interpreter with athenaeum deps                         |
| `ATHENAEUM_SRC`          | —                                 | Source checkout path (skips `pip install`, runs from source)   |
| `ATHENAEUM_OP_KEY_PATH`  | `op://Agent Tools/Anthropic API Key/credential` | 1Password secret reference for `ANTHROPIC_API_KEY` |
| `ATHENAEUM_OP_JEV_KEY_PATH` | `op://Agent Tools/Jev Athenaeum Key/credential` | 1Password secret reference for `JEV_API_KEY` |
| `ATHENAEUM_OP_INTERACTIVE` | unset           | Set to `1` to opt into 1Password's interactive desktop-app integration: `op` may then run without `OP_SERVICE_ACCOUNT_TOKEN`, and the non-interactive suppression flags below are NOT set (athenaeum#2193) |
| `ATHENAEUM_OP_REFRESH`   | unset                              | Set to `1` to re-fetch a credential from `op` even when a cached line already exists (athenaeum#2193) |
| `ATHENAEUM_OP_TIMEOUT_SECONDS` | `10`                          | Bound, in seconds, on a single `op read` (athenaeum#2193) |
| `ATHENAEUM_FORCE_REBUILD` | `0`                              | Set to `1` to force a vector-index rebuild even if the existing one is fresher than the wiki |
| `ATHENAEUM_INJECT_SKIP_WORDS` | `Code|Users|home|workspace|src|lib|app|var|tmp|usr` | Pipe-separated cwd path segments to ignore in `wiki-context-inject.sh` |
| `ATHENAEUM_INJECT_MAX_RESULTS` | `3`                          | Max wiki pages to surface from `wiki-context-inject.sh`         |
| `ATHENAEUM_PQ_SNOOZE_HOURS` | `24`                          | Snooze TTL for `pending-questions-surface.sh` (consumed by the `resolve-questions` skill when writing the snooze file) |
| `ATHENAEUM_PQ_HOOK_DEBUG` | `0`                              | Set to `1` to log `pending-questions-surface.sh` diagnostics to stderr |
| `SEARCH_BACKEND`         | from `athenaeum.yaml` (`fts5`)    | `fts5` (default) or `vector`                                   |
| `AUTO_RECALL`            | from `athenaeum.yaml` (`true`)    | Set to `false` to disable per-turn recall                      |

## Hybrid recall: why both backends

Short proper-noun queries like "Return Path" embed in vector space
closer to generic pages containing "path" than to a sparse entity page
about the company. FTS5 phrase matching rescues these. Conversely,
purely semantic queries ("iterative feedback loops" → "Innovation
Accounting") have no lexical overlap and need the vector side.

Removing either backend collapses recall for its rescue class. See
`docs/design/recall-architecture.md` for the full walkthrough and the four
invariants a future "simplification" must not remove.

## Supplying an API key (three options)

The LLM topic extractor (`athenaeum query-topics`) needs a real
`ANTHROPIC_API_KEY` for the Messages API when using the default Anthropic
provider — Claude Code's own `CLAUDE_CODE_OAUTH_TOKEN` is rejected with
`401 OAuth authentication is currently not supported`, so that token can't
be reused for the general API. The Jev decision provider
(athenaeum#1997, athenaeum#2054) needs its own key the same way, via
`JEV_API_KEY`. **Athenaeum works fully with no `op` (1Password CLI)
installed or signed in at all** — `op` is the LAST of three co-equal
ways to supply either key, and entirely optional. All three apply to both
`ANTHROPIC_API_KEY` and `JEV_API_KEY` identically.

1. **Export it in your environment.** Add `export ANTHROPIC_API_KEY=sk-…`
   (and/or `export JEV_API_KEY=…`) to your shell rc. `session-start-recall.sh`
   sees a live, already-exported value and never touches `op` or the cache
   for that credential at all — see `_had_live_cred` in the script if you
   want the exact mechanics.
2. **Write it into `~/.cache/athenaeum/config.env`.** One line,
   `ANTHROPIC_API_KEY=sk-…` (or `JEV_API_KEY=…`), file mode `600`. The hook
   preserves a cached line across every run unless a fresh fetch supersedes
   it (see option 3's `ATHENAEUM_OP_REFRESH`) — no `op`, no network, ever,
   for a credential supplied this way.
3. **Let the hook fetch it with `op`.** Opt-in and gated — as of
   athenaeum#2193, `session-start-recall.sh` calls `op` for a given
   credential only when ALL of:
   - `op` is on `PATH`;
   - `OP_SERVICE_ACCOUNT_TOKEN` is set, **or** you set
     `ATHENAEUM_OP_INTERACTIVE=1` to opt into 1Password's interactive
     desktop-app integration instead;
   - nothing is cached for that credential yet, **or** you set
     `ATHENAEUM_OP_REFRESH=1` to force a re-fetch over an existing cached
     line.

   Every `op` call this hook makes is bounded by
   `ATHENAEUM_OP_TIMEOUT_SECONDS` (default 10s) and runs with
   `OP_LOAD_DESKTOP_APP_SETTINGS=false` / `OP_BIOMETRIC_UNLOCK_ENABLED=false`
   set on the `op` subprocess only — unless you opted into
   `ATHENAEUM_OP_INTERACTIVE=1`, in which case those flags are deliberately
   NOT set. See "Repeated permission prompts" below for why those flags
   matter. The fetched value is cached at `~/.cache/athenaeum/config.env`
   (mode 600, owner-only). Silent on any failure — a missing item, an
   expired `op` session, or a timeout all just leave whatever was already
   cached (or live) in place.

Under `llm.provider: claude-cli`, no `ANTHROPIC_API_KEY` is needed at
all — `query-topics` authenticates via the ambient Claude Code login
instead, and none of the three options above come into play for that key.

Without a key (and outside `llm.provider: claude-cli`), the hook falls
back to a regex+stopword extractor — still good, just less topic-aware.

## Auto-memory integration

In addition to the per-turn recall hooks above, Athenaeum can ingest Claude
Code's first-party auto-memory files (`~/.claude/projects/<scope>/memory/`)
as another raw intake source. The librarian then clusters, merges, and
contradiction-checks them alongside anything else in `raw/`.

Files in this directory supporting that integration:

| File                                  | Purpose                                                            |
|---------------------------------------|--------------------------------------------------------------------|
| `setup-symlinks.sh`                   | Bridge `~/.claude/projects/*/memory` into `raw/auto-memory/<scope>/`. Idempotent; supports `--dry-run`. |
| `stop-hook-validate.sh`               | Claude Code `Stop`-hook template that warns when auto-memory frontmatter is missing `originSessionId`/`originTurn`. Non-blocking by default. |
| `auto-memory-frontmatter.example.md`  | Example memory file with a valid citation frontmatter block.        |

Full walkthrough — directory layout, citation policy, how the librarian
ingests and consolidates, and an end-to-end quick start — lives in
[`docs/guides/claude-code.md`](../../docs/guides/claude-code.md).

## Pending-questions surface

When the librarian flags a contradiction or ambiguity, it lands in
`~/knowledge/wiki/_pending_questions.md` as a `- [ ]` checkbox block.
The `pending-questions-surface.sh` SessionStart hook calls
`athenaeum questions count --json` and prints a one-block prompt like:

```
[Pending memory questions] 3 unresolved (oldest: 2026-04-20).
Resolve interactively now, defer to tomorrow, or skip?
Available via: `athenaeum questions next --with-proposal` or the resolve-questions skill.
```

To resolve interactively, invoke the `resolve-questions` skill (shipped in
the athenaeum repo at `.claude/skills/resolve-questions/SKILL.md`). It walks
each entry and calls the `resolve_question` MCP tool with your decision.
"Defer" writes `~/.cache/athenaeum/pending-questions-snoozed-until`; the
hook stays silent until that ISO-8601 timestamp passes.

CLI reference:

```bash
athenaeum questions count --json                    # {"count": N, "oldest": "..."}
athenaeum questions next --with-proposal            # show oldest entry + proposal
athenaeum questions list --with-proposal --limit 5  # all unresolved (capped)
```

## Repeated permission prompts

A macOS "App Data" (or similar) permission dialog that keeps reappearing
for `op`, `bash`, `node`, or `python` is almost always `op` itself, not
anything Athenaeum's hooks do directly — this section explains why, how to
stop it for good, and exactly which recurring paths in this repo were
checked to confirm that (athenaeum#2193).

**Why each prompt appears.** By default, every `op` invocation probes the
1Password desktop app's group container — on macOS this is the App Data
TCC service (`kTCCServiceSystemPolicyAppData`), and the probe can block
indefinitely waiting on a decision. Setting
`OP_SERVICE_ACCOUNT_TOKEN` alone does **not** stop this probe — the token
authenticates `op`, it doesn't change what `op` checks on the way there.
You need `OP_LOAD_DESKTOP_APP_SETTINGS=false` (and, for the matching
biometric-unlock prompt, `OP_BIOMETRIC_UNLOCK_ENABLED=false`) as well.

**Approving the dialog is not a reliable fix for CLI binaries.** TCC grants
are keyed to the exact binary path that asked. Approvals have been
observed not to stick for `/usr/local/bin/op`, `bash`, `node`, and
`python` — the same client reappears as a fresh row in
`System Settings → Privacy & Security` requesting the identical
permission again — and a versioned path (e.g. a Homebrew Cellar `bash`)
re-prompts outright after `brew upgrade` changes the binary's path. So
**the primary remedy is avoiding the probe, not approving it.**

**How to avoid it.**
- Set `OP_LOAD_DESKTOP_APP_SETTINGS=false` (and `OP_BIOMETRIC_UNLOCK_ENABLED=false`)
  in every environment that runs `op` — **including GUI-launched and
  launchd contexts, which do not read `~/.zshenv`/`~/.bashrc`/etc.** A
  shell-rc export is invisible to a LaunchAgent or a `/bin/sh` wrapper
  invoked outside an interactive shell; set the variable in the plist's
  `EnvironmentVariables` key or inside the wrapper script itself instead.
  `session-start-recall.sh` and `receipt_sealers.py` both set these two
  flags automatically on every `op` subprocess they spawn, unless you
  explicitly opted into `ATHENAEUM_OP_INTERACTIVE=1` (see "Supplying an
  API key" above) — but anything outside Athenaeum that also shells out to
  `op` (your own scripts, another tool) needs the same treatment
  independently.
- Or avoid `op` entirely: supply the key via a service-account token
  (`OP_SERVICE_ACCOUNT_TOKEN`, still using `op` but authenticated
  non-interactively), or skip `op` altogether with the env/`config.env`
  options above — no `op` process is ever spawned for a credential
  supplied either of those ways.

**How to reset a bad decision.** `tccutil reset SystemPolicyAppData` (or
the matching service name for whichever permission keeps firing) clears
every existing grant/denial for that service, or use
`System Settings → Privacy & Security` to remove individual rows by hand.

### Recurring-path permission audit

Every path in this repo's `examples/claude-code/` kit that fires more than
once per install (i.e. not a one-time `init`/`setup` script) was checked
against the shipped source at commit `bc146a5` for whether it can reach
`op` or any other OS-permission-gated resource. Columns: how often it
fires, what subprocesses it spawns, and what OS permissions it can
request as a result.

| Path | Fires | Subprocesses it spawns | OS permissions it can request |
|---|---|---|---|
| `session-start-recall.sh` | `SessionStart`, once per session | `$PYTHON` (config read, FTS5 index build, stopwords cache, overflow-breadcrumb template copy, and — when the vector backend needs rebuilding — vector index build), and — only when the athenaeum#2193 gate above opens — `op` | Files & Folders if `KNOWLEDGE_ROOT` lives under `~/Documents`/`~/Desktop`/iCloud Drive; App Data via `op` **only when the gate opens, and suppressed by `OP_LOAD_DESKTOP_APP_SETTINGS=false` unless `ATHENAEUM_OP_INTERACTIVE=1`**. No Keychain access, no Full Disk Access. |
| `athenaeum-claude-hook` (packaged `UserPromptSubmit` adapter, launched by `user-prompt-recall.sh`) | Every user turn | None related to `op` — invokes no 1Password path at all; reads the cached `config.env` only | Files & Folders for the knowledge root. Nothing else. |
| `pre-compact-save.sh`, `pending-questions-surface.sh`, `wiki-context-inject.sh`, `rebuild-index.sh`, `stop-hook-validate.sh` | Various (compaction, SessionStart, optional) | No `op`, no 1Password, no Keychain in any of these five scripts | Files & Folders for the knowledge root where applicable; nothing `op`- or Keychain-related |
| `athenaeum serve` (the MCP server) | Long-running, launched once | No `op` | None beyond the knowledge root |
| The SessionEnd librarian (`athenaeum run`) | SessionEnd / cron / launchd, once per invocation | Reaches 1Password only through `receipt_sealers.OnePasswordServiceAccountKeyProvider`, which requires `OP_SERVICE_ACCOUNT_TOKEN` to even construct a subprocess call (no token ⇒ no `op` process at all), and now forces both suppression flags unconditionally | None beyond the knowledge root |
| Shipped LaunchAgent/cron examples | Every 15 minutes when loaded (`StartInterval` 900s — the Codex example only; `examples/claude-code/` ships none) | `examples/claude-code/` itself ships none. `examples/codex/com.athenaeum.codex-context.plist` ships a LaunchAgent example for the separate Codex integration, invoking `athenaeum codex-context` directly — unrelated to this audit beyond being the one shipped launchd example in the repo | None — it invokes `athenaeum codex-context` directly, with no `op` and no 1Password path |

**Invariant:** no athenaeum path requests an OS permission per recall or
per session once a key is cached or supplied via env/`config.env`; at
most one prompt per install, and none at all when `op` is unused or the
suppression flags are in effect.

**Unresolved lead.** A 2026-10-29 05:48 App Data grant recorded for a
versioned Homebrew `bash` could **not** be attributed from this
repository — no athenaeum hook invokes `op` outside the gate documented
above, and attributing a specific historical grant needs the operator's
local TCC database and the unified log, neither of which this audit (a
static read of the shipped source) can read. An operator who wants to
settle it would run:

```bash
sqlite3 ~/Library/Application\ Support/com.apple.TCC/TCC.db \
  'select client, auth_value, last_modified from access where service="kTCCServiceSystemPolicyAppData"'
log show --predicate 'subsystem == "com.apple.TCC"' --start "2026-10-29 05:40:00"
```

A follow-up issue tracks running these and attributing the grant.

## Troubleshooting

| Symptom                                         | Check                                                                                           |
|-------------------------------------------------|-------------------------------------------------------------------------------------------------|
| Session message shows `0 wiki pages`            | `$KNOWLEDGE_ROOT/wiki/` is empty or unreadable — if `raw/` has files, run `athenaeum run`       |
| `remember` saves but `recall` finds nothing     | Raw observations compile to wiki only when `athenaeum run` fires. Check `ls ~/knowledge/raw/` for pending files, then run `athenaeum run --path ~/knowledge` |
| No `[Knowledge context]` on user turns          | Run `sqlite3 ~/.cache/athenaeum/wiki-index.db 'select count(*) from wiki'` — should be > 0     |
| Vector backend silent                           | `user-prompt-recall.sh` is fail-silent by design; pipe the hook's stdin JSON into `athenaeum context --stdin-json` instead to see the real error — usually `pip install 'athenaeum[vector]'` missing |
| `query-topics` not returning topics             | Under the default Anthropic provider: `cat ~/.cache/athenaeum/config.env` — should contain `ANTHROPIC_API_KEY=...`. Under `llm.provider: claude-cli`, no key is needed — run `athenaeum context --stdin-json` directly to see the real error instead |
| Hook ran "green" but recall never fires         | Check the settings-snippet was merged correctly: `grep UserPromptSubmit ~/.claude/settings.json`|
