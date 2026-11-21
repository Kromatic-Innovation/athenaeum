<!-- SPDX-License-Identifier: Apache-2.0 -->

# Claude Code plugin, clean-machine install check, Linux half, 2026-11-20

Issue athenaeum#2308 (epic athenaeum#2296). Authorised by operator ruling on
2026-11-20, extending the 2026-11-14 container ruling on athenaeum#2307.
Result: **FAIL as shipped.** Installing the plugin from its self-hosted
marketplace works, but the plugin's MCP server cannot start, so `remember` and
`recall` do not exist in the first session or any later one. With one launcher
fix and two commands the README does not mention, the rest of the chain works.
The "`uv` missing" counter-example also fails its visibility requirement.

## Summary

| # | Acceptance criterion (athenaeum#2308) | Result |
|---|---|---|
| 1 | Clean Linux machine with Claude Code and `uv`: install the plugin, restart, reach the first-recall bar with no hand-edited file | **Fail as shipped.** Three blockers (see [Findings](#findings)). **Pass with workarounds**: after `athenaeum init`, a one-word launcher patch and a cleared failure cache, `remember`, compile and a recall in a new session all worked. The macOS half is the operator's. |
| 2 | Thin repo exists in the organisation, holds only the manifest, hooks and launcher, and is published from a self-hosted marketplace | **Pass, with a note.** `marketplace add` and `install` worked from the private repo. Besides the manifest, hooks and launcher it also holds `README.md`, `LICENSE` and a CI workflow. |
| 3 | The dependency build does not run inside a hook timeout, and the first session says it is still preparing | **Pass.** |
| 4 | Counter-example, `uv` missing: the session still works and says what to install, visibly | **Fail on visibility.** The session worked. The "install uv" text was written but is shown nowhere the user sees it. |
| 5 | Uninstalling leaves the knowledge directory intact and no hooks behind | **Pass.** |

## Host and versions

| | |
|---|---|
| Image | `debian:bookworm-slim`, digest `sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc` (Debian 12.15, aarch64), on Docker 29.7.2 on an arm64 Mac |
| Container | `docker run --rm -d`, **no mounts** (`docker inspect` showed zero), nothing copied in but throwaway shell scripts. No host `~/.claude`, `~/knowledge` or `~/.cache/athenaeum` |
| User | Unprivileged user created in the image; no `uv`, Python or `claude` present beforehand |
| Claude Code | 2.1.330, native installer |
| `uv` | 0.13.9 (container A only); it provided Python 3.13.18 |
| athenaeum | 0.26.0, the version the plugin pins |
| Plugin repo | `athenaeum-claude-plugin` at its only commit, `7481a24f`, still private |
| Clock | Container time is UTC and ran past midnight, so file stamps read 2026-11-21 |

Container A had `uv`. Container B was the same image with `uv` never installed.

## Credentials

Both were passed as `-e` from environment variables and never printed or
written to the report.

- **Claude Code:** the long-lived OAuth token from the Agent Tools 1Password
  vault, read with `op read` into the shell that ran `docker run`. No
  `ANTHROPIC_API_KEY` existed in the container, so the compile ran on the
  Claude Code login (`llm.provider: claude-cli`).
- **Plugin repo:** a GitHub App installation token minted for this run with
  `contents: read` only and a one-hour life. **Deviation from the brief:** the
  workspace minter cannot restrict a token to one repository, so this token
  could read every repository the App is installed on, not only the plugin
  repo. It could not write. The token file was deleted after the run.
- The container's git was given a credential helper that reads the token from
  the environment. That is needed only because the repo is private; a user who
  is signed in to GitHub would not need it.

## Steps to repeat

1. Start a container: `docker run --rm -d -e CLAUDE_CODE_OAUTH_TOKEN -e GITHUB_TOKEN debian:bookworm-slim sleep 14400`.
2. As root: `apt-get install -y curl ca-certificates git`, add an unprivileged user, and set a git credential helper that echoes the `GITHUB_TOKEN` variable.
3. As that user: install Claude Code (`curl -fsSL https://claude.ai/install.sh | bash`) and `uv` (`curl -LsSf https://astral.sh/uv/install.sh | sh`). Container B skips `uv`.
4. Install the plugin. The README's slash commands are not available in print mode (`/plugin isn't available in this environment`), so the equivalent CLI was used:
   `claude plugin marketplace add Kromatic-Innovation/athenaeum-claude-plugin`, then
   `claude plugin install athenaeum@athenaeum-claude-plugin`.
5. Each `claude -p ... --output-format stream-json --verbose --include-hook-events --permission-mode bypassPermissions` call is a fresh process, which stands in for a restart. The stream carries each hook's output and the MCP server's status.
6. Compile with the plugin's own launcher: `hooks/launcher.sh session-end`.

This used print mode, not the interactive terminal screen. What the interactive
screen shows for a hook's output was not looked at (see [Left for
macOS](#left-for-the-operators-macos-run)).

## What happened, in order (container A)

1. **Install.** Both commands succeeded. `claude plugin list` showed version 0.26.0 enabled. The install said `1 userConfig option not yet set`.
2. **First session.** SessionStart printed `still preparing athenaeum 0.26.0 via uv (first run, just started) -- recall runs in reduced/FTS5-only mode until this finishes`. The MCP server was `pending`, then the model reported `CONNECTION_CLOSED`. No athenaeum tool existed.
3. **Why.** Running the launcher by hand: `Knowledge directory not found ... Run 'athenaeum init --path ~/knowledge' first`. The README never says to run `init`.
4. **Ran `init` (workaround 1)** through the launcher. It worked, selected `claude-cli`, set a repo-local git identity because none existed, and chose the `vector` search backend.
5. **Second session, as shipped.** Still failed. The server now died with `ModuleNotFoundError: No module named 'fastmcp'`: the launcher runs `athenaeum==0.26.0` with no `[mcp]` extra. Claude Code also cached the failure and reported it would retry in about 15 minutes.
6. **Patched the container's cached launcher (workaround 2)** to `athenaeum[mcp]==0.26.0` and deleted `~/.claude/mcp-needs-auth-cache.json` (workaround 3).
7. **Next session.** The server connected with 17 tools. `remember` wrote one raw note under `raw/manual/`.
8. **Recall before compile.** `recall` returned `No wiki pages matched`, as expected before a compile.
9. **Compile.** `launcher.sh session-end` took 31 s on the Claude Code login, created 3 pages (the society, its treasurer, its hall), and reindexed with the `fts5` backend because `chromadb` is not installed (a warning, not a failure).
10. **New session recall.** `recall` returned all three pages, with the invented treasurer and meeting day correct.

The fact was invented: a made-up society with a made-up treasurer and meeting hall. No real memory content was used.

## Acceptance criteria in detail

### 1. First-recall bar: fail as shipped, pass with workarounds

As shipped, a user following the plugin README gets a plugin that installs and
a server that never starts. The three blockers:

- `serve` needs a knowledge directory that only `athenaeum init` creates.
- The launcher omits the `[mcp]` extra, so `serve` cannot import its server.
- A failed first connect is cached by Claude Code for about 15 minutes, so even after a fix the next sessions stay dead until the cache entry is cleared or expires.

With those worked around, the epic's bar was met in container A: a fact told to
Claude was saved, compiled without an API key, and read back in a new session.

### 2. Thin repo and marketplace: pass

`marketplace add` cloned over HTTPS, validated and registered the marketplace;
`install` registered the plugin in user scope. The repo's top level is
`.claude-plugin`, `.github`, `.mcp.json`, `LICENSE`, `README.md` and `hooks`,
so it holds more than "only the manifest, hooks and launcher". The extras look
intended, and the criterion's wording may want updating rather than the repo.

### 3. Build not in a hook timeout, first session says preparing: pass

With a cold `uv` cache, no managed Python and the ready marker removed,
`launcher.sh --ensure-ready` returned in 0.004 s against the hook's 5 s budget
and printed `still preparing`. The background build finished 3.0 s later
(66 packages), after which the hook said `is ready`. In the first real session
the message reached the hook output. **Caveat:** the background build only
fetches the base package, not the `[mcp]` set. The heavier first build
therefore happens when the MCP server starts, which is allowed to block, so the
pass covers the hook and says nothing about how long a user waits at first
connect. Also, with `[mcp]` missing, the "ready" message is true of a state in
which `serve` still cannot run.

### 4. `uv` missing: fail on visibility

In container B the session worked (`17 x 3 = 51`), the hooks exited 0, and the
MCP server was `failed` with `CONNECTION_CLOSED`. Asked directly, the model
reported it had seen **no** startup message about installing anything. The
launcher does print `'uv' was not found on PATH ... Install it ... then restart`,
but it goes to stderr and `hooks/hooks.json` discards stderr (`2>/dev/null`) on
every hook. The only place the text appears is Claude Code's MCP debug log
file. A user sees a failed server and no reason. This is the "silently dead
hook" the criterion rules out, one level up.

### 5. Uninstall: pass

Before and after `claude plugin uninstall` and `marketplace remove`, the
knowledge directory hashed identically (12 files outside `.git`, same list of
wiki pages). `settings.json` was left with empty `enabledPlugins` and
`extraKnownMarketplaces`. A session started afterwards ran no hooks and
connected no server. Left behind, outside the knowledge directory: the cache
under `~/.cache/athenaeum` (index, spend and prepare logs) and an unused copy
of the plugin under `~/.claude/plugins/cache/`. Neither fires anything.

## Open items from the last status comment

Recorded only. Nothing was changed in the private repo.

- **`userConfig.knowledgePath`.** Not wired. `.mcp.json` passes only `serve`, so the server used its own default (`~/knowledge`) every time. Claude Code reports the option as `(optional, not set)` and the install prints `1 userConfig option not yet set` even though the manifest declares a default, so a new user is told something is unfinished. Whether a configured value reaches the server was **not tested** (no value was set).
- **Missing icon.** The installed copy has no icon file. Not exercised beyond that.
- **`_versionPinNote`.** `claude plugin validate` warned about it for both the plugin and the marketplace entry, with and without `--strict`: `Unknown field '_versionPinNote'. Claude Code ignores it at load time.` Install and load were unaffected.

## Findings

Owners of these are for the operator and the plugin repo; this report fixes
none of them. Items 1 and 2 also exist in the staged tree
(`examples/claude-code-plugin/hooks/launcher.sh` lines 107 and 160, and its
`hooks.json`), which is where the fix lands before it is copied to the plugin
repo.

1. **Launcher omits the `[mcp]` extra.** Blocks every session. One-word fix in two places.
2. **`uv` missing text is discarded by `2>/dev/null`.** Needs a visible channel: hook stdout, a `systemMessage`, or a message the MCP failure carries.
3. **No step creates the knowledge directory.** Either the README says to run `init`, or the launcher creates it on first run.
4. **A first-connect failure is cached for about 15 minutes.** Together with 1 and 3, a user who hits either waits, or must find and delete a cache file.
5. **`ready` is reported before the server's own dependencies exist.**
6. **The default search backend is `vector` but the plugin installs no vector extra.** Recall falls back to `fts5` with a warning, which is acceptable, but the plugin should say which mode it is in.

## Left for the operator's macOS run

- Everything above on a real Mac with a normal login keychain: the same five criteria, and in particular whether `launcher.sh` behaves the same.
- The interactive terminal screen: what a user sees from a hook's stdout and from a failed MCP server. This run used print mode only.
- The README's slash commands (`/plugin marketplace add ...`) typed by a person; this run used the equivalent CLI.
- A real first cold build timing on the operator's connection, for the `[mcp]` set, including how long the first MCP connect blocks.
- Whether `claude plugin configure` with a path reaches the server once `.mcp.json` is wired.
- Re-run after fixes to findings 1 to 4 against the plugin repo's new head, since this run's pass for criterion 1 depended on patching a cached copy.
- The public flip stays an operator step. The plugin repo remained private throughout.
