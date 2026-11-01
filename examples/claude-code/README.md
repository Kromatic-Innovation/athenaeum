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
       "athenaeum": {
         "command": "/path/to/venv/bin/athenaeum",
         "args": ["serve", "--path", "/absolute/path/to/knowledge"]
       }
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
| `ATHENAEUM_OP_KEY_PATH`  | `op://Agent Tools/Anthropic API Key/credential` | 1Password item for `ANTHROPIC_API_KEY`, used only if you opt into the `op` fetch (see "Supplying an API key") |
| `ATHENAEUM_OP_JEV_KEY_PATH` | `op://Agent Tools/Jev Athenaeum Key/credential` | 1Password item for `JEV_API_KEY`, used only if you opt into the `op` fetch |
| `ATHENAEUM_OP_INTERACTIVE` | unset           | Set to `1` to opt into 1Password's interactive desktop-app integration: `op` may then run without `OP_SERVICE_ACCOUNT_TOKEN`, and the non-interactive suppression flags below are NOT set (athenaeum#2193) |
| `ATHENAEUM_OP_REFRESH`   | unset                              | Set to `1` to re-fetch a credential from `op` even when a cached line already exists (athenaeum#2193) |
| `ATHENAEUM_OP_TIMEOUT_SECONDS` | `10`                          | Bound, in whole seconds, on a single `op read`, inside the detached background job (athenaeum#2193, athenaeum#2204); a non-positive-integer value (fractional, non-numeric, empty, or zero) silently falls back to `10` |
| `ATHENAEUM_HOOK_BUDGET_SECONDS` | `10`                        | Shared foreground time budget for `session-start-recall.sh` and the packaged `athenaeum-claude-hook` adapter; each still-foreground step is bound by whatever's left of it, not by its own fixed timeout (athenaeum#2204) |
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

## Supplying an API key

### Which parts of athenaeum need a key

Two optional credentials, handled the same way:

- `ANTHROPIC_API_KEY`, for the default Anthropic provider. Two things use it.
  The per-turn LLM topic extractor (`query-topics`) uses it, and so does the
  compile step (`athenaeum run`, `ingest`, `session-end`, `drain`). Claude
  Code's own `CLAUDE_CODE_OAUTH_TOKEN` cannot stand in for it, because the
  Messages API rejects it with
  `401 OAuth authentication is currently not supported`.
- `JEV_API_KEY`, for the optional Jev decision provider.

If you set `llm.provider: claude-cli` in `athenaeum.yaml`, neither the topic
extractor nor the compile step needs `ANTHROPIC_API_KEY`. Both then use your
Claude Code login.

**1Password is not required.** athenaeum works fully with no `op` (the
1Password CLI) installed. It only ever looks for the key in two places: the
environment, and the cache file described below. Put it there with whatever
secret manager you already use (see "Using your own secret manager" below).

### Where the key goes

1. **The environment Claude Code launches with.** Hooks inherit Claude
   Code's environment. If you start `claude` from a terminal, an
   `export ANTHROPIC_API_KEY=sk-…` in your shell startup file reaches the
   hooks. **A GUI-launched app does not read shell startup files**
   (`~/.zshrc`, `~/.bashrc` and so on), whether you open it from the Dock,
   Spotlight or an IDE. The export is then invisible to it, so use option 2.
2. **`~/.cache/athenaeum/config.env`.** Add one line,
   `ANTHROPIC_API_KEY=sk-…` (or `JEV_API_KEY=…`), and set the file to mode
   `600`. The SessionStart hook rewrites this file every session. It keeps
   the last `ANTHROPIC_API_KEY=` and `JEV_API_KEY=` lines and regenerates
   everything else, so don't keep other settings there. The file is
   plaintext: mode `600` keeps other users out, but not other processes
   running as you.

**Order the hooks use.** There is no ranking beyond this:

- a key already exported in the hook's environment wins;
- otherwise the cached `config.env` line is used;
- otherwise, and only if you opted into `op` (see below), the hook fetches
  the key with `op` and caches it.

Neither of the first two ever runs `op`. An exported key is never written
into `config.env`.

**Who reads `config.env`, and who doesn't.** `config.env` is read by:

- the hooks in this directory;
- the packaged `athenaeum-claude-hook` adapter;
- `athenaeum audit`.

In each case a variable that is already set in the process environment wins
over the file. **`athenaeum run`, `ingest`, `session-end`, `drain` and `serve`
never read `config.env`.** A compile started from cron, launchd or a terminal
needs `ANTHROPIC_API_KEY` in its own environment, for example:

- `EnvironmentVariables` in the launchd plist;
- a line in the crontab;
- a wrapper script that reads your secret manager.

Alternatively, set `llm.provider: claude-cli`.

### Using your own secret manager

athenaeum doesn't integrate with any secret manager, and doesn't need to.
Fetch the key once with the tool you already use and write it to
`config.env`:

```bash
mkdir -p ~/.cache/athenaeum && chmod 700 ~/.cache/athenaeum
key="$(security find-generic-password -s anthropic-api-key -a "$USER" -w)"   # replace with your command
( umask 077; printf 'ANTHROPIC_API_KEY=%s\n' "$key" >> ~/.cache/athenaeum/config.env )
chmod 600 ~/.cache/athenaeum/config.env
unset key
```

The command that fetches the key, by tool. The service, item and account
names are placeholders; use your own.

| Secret store | Read command |
|---|---|
| macOS Keychain | `security find-generic-password -s anthropic-api-key -a "$USER" -w` (store it first with `security add-generic-password -s anthropic-api-key -a "$USER" -w`, which prompts for the value) |
| Linux Secret Service (GNOME Keyring, KWallet) | `secret-tool lookup service anthropic-api-key` |
| `pass` | `pass show anthropic/api-key` |
| Bitwarden | `bw get password "Anthropic API Key"` (needs an unlocked `BW_SESSION`) |
| 1Password | `OP_LOAD_DESKTOP_APP_SETTINGS=false OP_BIOMETRIC_UNLOCK_ENABLED=false op read "op://<vault>/<item>/credential"` |

Notes:

- **Apple's Passwords app has no command-line interface.** It is a front end
  to the Keychain, but items saved through it are not reliably readable with
  `security`. Store the API key yourself with `security add-generic-password`
  as shown above, and read it back with `security find-generic-password`.
- **A Keychain read can raise its own access prompt** ("security wants to use
  your confidential information stored in … in your keychain"). That is why
  the recipe above fetches **once** and caches the key. Don't fetch from the
  Keychain inside a per-session hook.
- **When the key changes**, run the recipe again. The last
  `ANTHROPIC_API_KEY=` line in the file wins.

### Optional: let the hook fetch the key with `op`

`session-start-recall.sh` can fetch a missing credential with `op` itself.
It does this only when all of the following hold:

- `op` is on `PATH`;
- `OP_SERVICE_ACCOUNT_TOKEN` is set, **or** you set `ATHENAEUM_OP_INTERACTIVE=1`
  to use 1Password's interactive desktop-app integration instead;
- the key is not already exported, and nothing is cached in `config.env` for
  it. `ATHENAEUM_OP_REFRESH=1` forces a re-fetch over a cached line.

Each `op read` is bounded by `ATHENAEUM_OP_TIMEOUT_SECONDS` (default 10,
whole seconds only). Unless you set `ATHENAEUM_OP_INTERACTIVE=1`, it runs
with `OP_LOAD_DESKTOP_APP_SETTINGS=false` and
`OP_BIOMETRIC_UNLOCK_ENABLED=false`, set on that one subprocess. See
"Repeated permission prompts" below for why. The fetched value is cached in
`config.env` (mode 600). Any failure is silent and leaves the existing
state in place.

The script ships default item paths (`op://Agent Tools/Anthropic API Key/credential`
and `op://Agent Tools/Jev Athenaeum Key/credential`). They are used only if
you opt into `op` as described above. Point them at your own items with
`ATHENAEUM_OP_KEY_PATH` and `ATHENAEUM_OP_JEV_KEY_PATH`. athenaeum#2205
plans to drop these baked-in paths in favour of a generic credential
command.

### What happens with no key

With no key, the default provider and no `claude-cli`:

- **Per-turn recall still works, with less topic awareness.** The topic
  extractor quietly falls back to a regex and stopword extractor.
- **The degradation is durably recorded and surfaced.** Every time a shipped
  hook ends a run with no key cached, it writes a `no_api_key` event to a
  status file under `~/.cache/athenaeum`. `athenaeum status` reports the
  latest one (see "Degraded-session visibility" below), and you also see a
  one-line notice injected into the model's context — at most once per
  session — from whichever hook runs first against it. That notice is on
  the hook's own context channel, not stderr, so it survives the shipped
  `settings-snippet.json`'s `2>/dev/null || true` on every hook command.
  `session-start-recall.sh` additionally still prints
  `[Knowledge] No ANTHROPIC_API_KEY cached — LLM topic extraction will fall back to regex-only extraction …`
  to stderr for anyone running it by hand; that line is thrown away by the
  shipped snippet exactly as before, but it is no longer the only signal.
- **`athenaeum run` refuses to start.** It exits `1` with
  `ANTHROPIC_API_KEY not set`, unless you pass `--dry-run` or use
  `llm.provider: claude-cli`.

To check which state you are in, run `athenaeum status` — or, by hand,
`grep -c '^ANTHROPIC_API_KEY=.' ~/.cache/athenaeum/config.env` (`0` means
nothing is cached yet).

### Degraded-session visibility

`athenaeum status` reports the most recently recorded degradation event
from any shipped hook — a missing credential (`no_api_key`), a search
backend index that has never finished building (`index_not_ready`), or a
foreground step that ran out of its budget and was skipped
(`step_timeout`). The record lives under `~/.cache/athenaeum` alongside
`config.env`. No separate command: extending `status` was the natural
place (there is no `doctor` command), and a dedicated hook also injects a
one-line notice into the model's context the first time it sees a
degradation in a given session — see "What happens with no key" above for
the channel it uses.

### Hook timeouts

Every shipped hook that can do variable-cost work — `session-start-recall.sh`
and the packaged `athenaeum-claude-hook` adapter (`user-prompt-recall.sh`'s
target) — shares ONE foreground time budget,
`ATHENAEUM_HOOK_BUDGET_SECONDS` (default 10 seconds). The shipped
`settings-snippet.json` gives both of them a timeout comfortably above that
budget (60 seconds for SessionStart, matching this script's own header
recommendation; 15 for UserPromptSubmit, covering process-startup overhead
the in-process budget itself doesn't bound) — if the two ever disagree
again, `tests/test_hook_budget_2204.py` fails CI.

The budget bounds the hook's OWN remaining foreground work, not the slow
work that used to dominate it. The two historically expensive steps —
credential refresh (`op read`, up to `ATHENAEUM_OP_TIMEOUT_SECONDS` each)
and a vector index rebuild (about 45 s on ~3,000 pages) — now run as a
single DETACHED background job behind a lock, kicked off by
`session-start-recall.sh` and never awaited by it or by any later turn. A
turn that starts while that job is still running (or before it has ever
run) uses whatever index is currently on disk — immediately, never
blocking — which is also when `index_not_ready` gets recorded (see
"Degraded-session visibility" above). The FTS5 index stays synchronous
(it's cheap, about 1 s per ~3,000 pages) and is itself bound by the same
remaining budget rather than an unbounded call.

Two concurrent sessions never start two background builds: both attempt
the same lock `rebuild-index.sh` already uses, so only one proceeds.

If recall feels weaker than expected, check `athenaeum status` first (see
"Degraded-session visibility") before reading any script header — it names
exactly which of the three degradation kinds is in effect, and when.

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

A macOS "App Data" permission dialog that keeps reappearing, worded
**"“op” would like to access data from other apps"** (or the same wording
naming `bash`, `node`, or `python`), is almost always `op` itself, not
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

An App Data grant that could not be attributed to any athenaeum path from
the shipped source is tracked in athenaeum#2195. It doesn't change any of
the setup guidance above.

## Troubleshooting

| Symptom                                         | Check                                                                                           |
|-------------------------------------------------|-------------------------------------------------------------------------------------------------|
| Session message shows `0 wiki pages`            | `$KNOWLEDGE_ROOT/wiki/` is empty or unreadable — if `raw/` has files, run `athenaeum run`       |
| `remember` saves but `recall` finds nothing     | Raw observations compile to wiki only when `athenaeum run` fires. Check `ls ~/knowledge/raw/` for pending files, then run `athenaeum run --path ~/knowledge` |
| No `[Knowledge context]` on user turns          | Run `sqlite3 ~/.cache/athenaeum/wiki-index.db 'select count(*) from wiki'` — should be > 0     |
| Vector backend silent                           | `user-prompt-recall.sh` is fail-silent by design; pipe the hook's stdin JSON into `athenaeum context --stdin-json` instead to see the real error — usually `pip install 'athenaeum[vector]'` missing |
| `query-topics` not returning topics             | Under the default Anthropic provider: `cat ~/.cache/athenaeum/config.env` — should contain `ANTHROPIC_API_KEY=...`. Under `llm.provider: claude-cli`, no key is needed — run `athenaeum context --stdin-json` directly to see the real error instead |
| Hook ran "green" but recall never fires         | Check the settings-snippet was merged correctly: `grep UserPromptSubmit ~/.claude/settings.json`|
