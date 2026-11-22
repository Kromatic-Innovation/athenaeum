<!-- SPDX-License-Identifier: Apache-2.0 -->

# Claude Code plugin, clean-machine install check repeated on 0.29.0, Linux half, 2026-11-21

Issue athenaeum#3053 (epic athenaeum#2296; the criteria are those of athenaeum#2308).
This repeats `plugin-clean-install-linux-2026-11-20.md` against the plugin repo
re-synced from the staged tree at the published v0.29.0 tag.
Result: **the three blockers of the 2026-11-20 run are gone.** The MCP server
starts on the first session, `remember` and `recall` are available in it, a
configured `knowledgePath` reaches the server, and a missing `uv` is now
reported where the user sees it. Two things still fail: the install screen does
not show the option default, and a failed first connect is still cached for
about 15 minutes after the user fixes its cause.

## Readings asked for by athenaeum#3053

| Question | Reading |
|---|---|
| Does the MCP server start on the first session? | **Yes.** In the very first session after install the server was `pending` at start and the model then had all 18 tools. No hand edit, no `athenaeum init`, no cache clearing. The session took 11 s. |
| Are `remember` and `recall` available? | **Yes.** Both were called in a new session and returned without error. `remember` wrote a raw note. After `launcher.sh session-end`, `recall` in a later session returned the compiled pages with the right facts. |
| Is a configured `knowledgePath` used? | **Yes.** After `claude plugin configure ... --values-stdin` set a path that did not exist, the next session created it and `remember` wrote under it. Left unset, the server used `~/knowledge`, creating it on first start. |
| Does the install screen show the option default? | **No.** `claude plugin install` printed `1 userConfig option not yet set` and `claude plugin configure` printed `knowledgePath ... (optional, not set)`, though the manifest declares a default. The interactive `/plugin` screen was not looked at (print mode and CLI only). Filed as athenaeum#3098. |

## Summary against the athenaeum#2308 Linux criteria

| # | Criterion | Result |
|---|---|---|
| 1 | Clean Linux machine with Claude Code and `uv`: install the plugin, restart, reach the first-recall bar with no hand-edited file | **Pass.** Down from "fail as shipped" on 2026-11-20. The fact was saved, compiled on the Claude Code login with no API key, and read back in a new session. The macOS half is the operator's. |
| 2 | Thin repo exists, holds only the manifest, hooks and launcher, and is published from a self-hosted marketplace | **Pass, with the same note as before.** `marketplace add` and `install` worked. The repo also holds `README.md`, `LICENSE`, a CI workflow and four vendored hook scripts. |
| 3 | The dependency build does not run inside a hook timeout, and the first session says it is still preparing | **Pass.** |
| 4 | Counter-example, `uv` missing: the session still works and says what to install, visibly | **Pass on visibility.** The message now arrives as hook JSON on stdout. Recovery after installing `uv` fails for about 15 minutes. Filed as athenaeum#3097. |
| 5 | Uninstalling leaves the knowledge directory intact and no hooks behind | **Pass.** |

## Host and versions

| | |
|---|---|
| Image | `debian:bookworm-slim`, digest `sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc` (Debian 12.15, aarch64), on Docker 29.7.2 on an arm64 Mac |
| Container | `docker run --rm -d`, **no mounts** (`docker inspect` showed zero), nothing copied in but throwaway shell scripts. No host `~/.claude`, `~/knowledge` or `~/.cache/athenaeum` |
| User | Unprivileged user created in the image; no `uv`, Python or `claude` present beforehand |
| Claude Code | 2.1.330, native installer (same as 2026-11-20) |
| `uv` | 0.13.9 (container A; container B installed it late, see criterion 4) |
| athenaeum | 0.29.0, the version the plugin pins |
| Plugin repo | `athenaeum-claude-plugin`, still private. See [Which plugin-repo ref](#which-plugin-repo-ref) |

Container A had `uv`. Container B was the same image with `uv` withheld until
the recovery check.

## Which plugin-repo ref

The re-sync commit was built from `examples/claude-code-plugin/` at v0.29.0
(release commit `7f499c55`). It was **not on the plugin repo's `main` when the
run was made**: `main` is protected by a repository rule that requires a pull
request and the `CI Required` check, and the automation identity has no bypass.
The previous sync landed on `main` by an operator bypass. The commit was
pushed to the branch `sync/athenaeum-0.29.0` instead, and the run installed
from that branch: `claude plugin marketplace add
'Kromatic-Innovation/athenaeum-claude-plugin#sync/athenaeum-0.29.0'`, then
`claude plugin install athenaeum@athenaeum-claude-plugin`. The files are what
`main` will hold once the branch is merged, so the readings stand for `main`;
merging the branch is an operator step.

What the sync changed in the plugin repo, following the "Keeping the thin repo
in sync" steps of the staged README: `plugin.json`, `.mcp.json` and
`hooks/launcher.sh` copied verbatim; `hooks/hooks.json` copied with the two
thin-repo-only edits (hook paths point at the vendored scripts, the
`pending-questions-surface.sh` entry omitted); the version in `marketplace.json`
bumped to 0.29.0; the "Vendored from athenaeum commit" line set to the release
commit. The four vendored scripts already matched this tree. The thin repo's
`marketplace.json`, `LICENSE`, CI workflow and README are not part of the staged
tree and were kept, because `marketplace add` needs `marketplace.json`.

## Credentials

Both were passed as `-e` environment variables and never printed or written to
this record.

- **Claude Code:** a long-lived Claude Code OAuth token read from the secrets
  vault at run time. No `ANTHROPIC_API_KEY` existed in either container, so the
  compile ran on the Claude Code login.
- **Plugin repo:** a short-lived, read-only GitHub token, discarded after the
  run. The container's git got a credential helper that reads it from the
  environment, needed only because the repo is private.

Captured session output was scanned for token patterns before this record was
written; none were found.

## Steps to repeat

1. Start a container: `docker run --rm -d -e CLAUDE_CODE_OAUTH_TOKEN -e GITHUB_TOKEN debian:bookworm-slim sleep 14400`.
2. As root: `apt-get install -y curl ca-certificates git jq`, add an unprivileged user, set a git credential helper that echoes `GITHUB_TOKEN`.
3. As that user: install Claude Code (`curl -fsSL https://claude.ai/install.sh | bash`) and `uv` (`curl -LsSf https://astral.sh/uv/install.sh | sh`). Container B skips `uv`.
4. `claude plugin marketplace add Kromatic-Innovation/athenaeum-claude-plugin` (with `#<branch>` while the sync is unmerged), then `claude plugin install athenaeum@athenaeum-claude-plugin`.
5. Each `claude -p ... --output-format stream-json --verbose --include-hook-events --permission-mode bypassPermissions` call is a fresh process, which stands in for a restart. The stream carries each hook's output and the server's status.
6. Compile with the plugin's launcher: `hooks/launcher.sh session-end`.
7. Set the path: `echo '{"knowledgePath": "<dir>"}' | claude plugin configure athenaeum@athenaeum-claude-plugin --values-stdin`.

This used print mode and the CLI, not the interactive terminal screen.

## What happened, in order (container A)

1. **Install.** Both commands succeeded. `claude plugin list` showed 0.29.0 enabled. The install said `1 userConfig option not yet set`.
2. **First session.** No knowledge directory existed. SessionStart printed `still preparing athenaeum 0.29.0 via uv (first run, just started)`. The server was `pending` at init, then connected, and the model listed 18 athenaeum tools including `remember` and `recall`. `~/knowledge` now existed, created by the launcher's `init`. No `mcp-needs-auth-cache.json` was written.
3. **Remember and recall.** In the next session the server was `connected`. `remember` saved a raw note and `recall` returned it, marked `[uncompiled]`.
4. **Compile, first try.** `session-end` finished in 10 s and reindexed with the `fts5` backend (a warning: `chromadb` is not installed). It created no pages: the entity step dropped the one note (`drops=1`) and the raw note was removed from the working tree. That note had described its own subject as "an invented club" in its text. This looks like the observation filter judging a self-declared invented fact not worth keeping, so it is recorded here as a probe artefact and not filed as a bug.
5. **Compile, second try.** A note with no such wording (an invented chess society, its meeting hall and its treasurer) compiled to 2 pages (`created=2`, `drops=0`), again with no API key.
6. **New session recall.** `recall` returned both pages, with the invented hall, meeting day and treasurer correct.
7. **`knowledgePath`.** `configure` first showed `(optional, not set)`. After the value was saved the option read `(optional, set)`. The next session connected, created the nonexistent directory, and `remember` reported `Saved to <that directory>/raw/...`. Nothing new appeared under `~/knowledge`.

The facts were invented; no real memory content was used.

## Acceptance criteria in detail

### 1. First-recall bar: pass

The three blockers of 2026-11-20 are fixed in the staged tree and held in the
installed copy: the launcher now installs `athenaeum[mcp]`, creates a missing
knowledge directory before `serve`, and no failed first connect occurred, so the
15-minute cache was not triggered on the happy path. No workaround of the
earlier run was needed.

### 2. Thin repo and marketplace: pass

Unchanged from before. `marketplace add` cloned over HTTPS, validated and
registered the marketplace; `install` registered the plugin in user scope.

### 3. Build not in a hook timeout, first session says preparing: pass

With the ready marker, the lock files and `uv`'s cache and managed Python all
removed, `launcher.sh --ensure-ready` returned in 0.004 s against the hook's 5 s
budget and printed `still preparing`. A minute later it printed `is ready`. The
real first session carried the same message in its hook output. The marker is
now `ready-mcp-<version>`, so "ready" is no longer reported for a state that
lacks the `[mcp]` set.

### 4. `uv` missing: pass on visibility, fail on recovery

In container B the session worked (`17 x 3 = 51`) and the server was `failed`
with `CONNECTION_CLOSED`. Unlike 2026-11-20, the model quoted the full
"`uv` was not found on PATH ... Install it ... then restart" text: the launcher
now emits SessionStart hook JSON on stdout (`systemMessage` for the user,
`additionalContext` for the model). Whether the interactive screen draws
`systemMessage` was not looked at.

Then `uv` was installed, as the message says, and a new session was started.
The server was `failed` again with `Skipping connection (recent failure cached
retries automatically in 15 min, or edit the plugin config to retry now)`, and
no athenaeum tool existed. Claude Code's cache is the cause; the plugin's
advice leads into it. Filed as athenaeum#3097.

### 5. Uninstall: pass

The knowledge directories hashed identically before and after `claude plugin
uninstall` and `marketplace remove`. `settings.json` was left with empty
`enabledPlugins`, `extraKnownMarketplaces` and `pluginConfigs`. A session
started afterwards connected no server and ran no athenaeum hook.

## Open items from the last status comment

- **`userConfig.knowledgePath`.** Wired, and works (see above). The display of its default is the open bug.
- **Icon and `_versionPinNote`.** Not exercised in this run. The staged `plugin.json` still carries `_versionPinNote`.

## Bugs filed

- athenaeum#3097: after installing `uv` following a failed first session, the MCP server stays dead for about 15 minutes.
- athenaeum#3098: install and configure output do not show the `knowledgePath` default and call the option unset.

## Left for the operator

- Merge `sync/athenaeum-0.29.0` into the plugin repo's `main` (it needs a pull request with `CI Required`, or the operator bypass used last time).
- The same five criteria on a real Mac, and the interactive `/plugin` screen: what it shows for the option default, for a hook's `systemMessage`, and for a failed server.
- The public flip stays an operator step. The plugin repo remained private throughout.
