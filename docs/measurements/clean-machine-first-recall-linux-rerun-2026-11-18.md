<!-- SPDX-License-Identifier: Apache-2.0 -->

# Clean-machine first-recall test, Linux re-run, 2026-11-18

Issue athenaeum#2319 (epic athenaeum#2296). Re-run of
[the 2026-11-14 Linux run](clean-machine-first-recall-linux-2026-11-14.md),
which failed, against the published 0.26.0 wheel and the fixes athenaeum#2592
and athenaeum#2594. Result for the Linux leg: **CONDITIONAL PASS.** The
README quick start now works end to end from PyPI on a stock image, through a
real Claude session, with no hand edits. Two parts of the README's own promises
still fail (the `doctor` "comes back clean" claim, and recall of a note before
its compile in the model-driven case), and macOS is not covered.

The same containers also produced the plan-usage measurement for
athenaeum#2307: see
[clean-machine-first-recall-no-api-key-2026-11-18.md](clean-machine-first-recall-no-api-key-2026-11-18.md)
for environment, commands, token usage and the no-login counter-example. This
page records only the athenaeum#2319 acceptance criteria. All people and facts
are invented.

## Scope

- **Covered:** the two parts the athenaeum#2319 thread named as never run.
  (a) The model-mediated path: `remember` and `recall` through a real Claude
  Code session calling the MCP tools, one `claude -p` process per step.
  (b) The published wheel: `athenaeum[mcp]==0.26.0` from PyPI on a fresh OS
  image.
- **Not covered, macOS:** stays with the operator by ruling.
- **Not covered, plugin/marketplace path:** not published (athenaeum#2308 is
  open), so there is nothing to install.
- **Not covered, Anthropic directory submission:** not attempted, because the
  README and `doctor` gaps below would be visible to a submission reviewer.
- The `[vector]` extra was not installed; this run is the `[mcp]`-only path the
  README documents as sufficient.

## Environment

As in the sibling report: stock `node:22-bookworm` (Debian 12, aarch64),
digest `sha256:e13726ca604184d7a91fcacab1fc09eefd642dc28e79ac34e85540dd93b78cfb`,
non-root `node` user, Claude Code 2.1.327, uv 0.13.9, Python 3.13.18 via uv,
athenaeum **0.26.0** from PyPI. Login supplied as `CLAUDE_CODE_OAUTH_TOKEN`;
`ANTHROPIC_API_KEY` unset. Git present, no git identity configured. Containers
removed afterward.

## Steps

The README quick start for 0.26.0, verbatim, after installing Claude Code and
uv (full command list, token redacted, is in the sibling report):

```bash
uv tool install --python 3.13 'athenaeum[mcp]==0.26.0'
athenaeum init
claude mcp add --scope user athenaeum -- athenaeum serve --path ~/knowledge
athenaeum doctor
athenaeum test-mcp
# round trip
claude -p "Please remember this using your remember tool: <fact>" --allowedTools mcp__athenaeum__remember </dev/null
athenaeum run
athenaeum session-end
claude -p "<question>; use your athenaeum recall tool" --allowedTools mcp__athenaeum__recall </dev/null
```

## Results

| Step | Result vs 2026-11-14 |
| --- | --- |
| Install from PyPI | OK. `athenaeum 0.26.0`, console scripts `athenaeum`, `athenaeum-claude-hook`, `promptwitness` installed. |
| `athenaeum init` with no git identity | **Fixed (athenaeum#2593).** Prints "No git identity is configured, so a repo-local one was set on the knowledge repo only" and completes; the README prerequisite note now matches. |
| `athenaeum init` provider message | Selects `claude-cli` and prints the plan-usage disclosure. |
| `claude mcp add --scope user ...` | OK. |
| `athenaeum doctor` right after | `mcp` is now **OK** ("registered in" the home-level `.claude.json`), fixed by athenaeum#2594. `hooks` and `search_index` FAIL, as the README predicts. `package_version`, `interpreter`, `key_mode` OK. |
| `athenaeum test-mcp` | 3 passed, 0 failed, no key needed. |
| Tell, session 1 (`claude -p`) | Claude called `remember`; one raw file landed. |
| `athenaeum run` | Compiled with `claude-cli`, no key: 3 pages created in 35.9 s. |
| `athenaeum session-end` | **Fixed (athenaeum#2592).** Printed a one-time warning that `search_backend: vector` needs chromadb, fell back to fts5, "reindex complete, 3 pages indexed", exit 0. On 0.23.5 this step errored. |
| Recall in a new `claude -p` process | **Returned the compiled page.** Answer ("prefers green tea", where she works) with a citation to the page. On 0.23.5 this was NOT FOUND. |
| `athenaeum doctor` after the full quick start | **Still 2 FAILs** (`hooks`, `search_index`). See finding 1. |

## Acceptance criteria

The athenaeum#2319 thread asks for a clean-machine pass of the quick start with
the fact recalled in a new session, plus the counter-example. Marked per part:

- **Install from the published wheel on a fresh OS image: PASS.**
  Dependency resolution and console scripts work from PyPI.
- **README quick start, no hand edits, fact recalled in a new session through a
  real Claude session calling the MCP tools: PASS.** Run twice with different
  invented facts, plus further notes compiled in a batch; every compiled fact
  was returned.
- **Counter-example, the fact came back through the shipped path and not the
  same context: PASS.** For each fact a separate `claude -p` process with the
  recall tool disallowed answered **UNKNOWN** before the tell and again after the
  compile, while the process with recall allowed returned the fact. Tell, ask
  and control were three separate processes with no shared context.
- **"Run `doctor` again once you have finished the quick start, and it should
  come back clean" (README): FAIL.** Two FAILs remain; see finding 1.
- **A saved note is recallable before compile (README and install docs, via
  athenaeum#2313): FAIL in the model-driven case.** See finding 2.
- **macOS: not covered.**
- **Plugin/marketplace path: not covered.**

## Findings

1. **`doctor` cannot come back clean after the quick start.** `hooks` fails until
   the optional sidecar hook is wired, which the quick start does not do, so the
   README's "should come back clean" is untrue as written. `search_index` also
   keeps failing ("no vector index found under .../wiki-vectors") even after
   `session-end` built the fts5 index that recall is actually using. The fix text
   it prints (`athenaeum reindex`) cannot clear it, because the backend falls
   back to fts5 whenever chromadb is absent. The fts5 branch of the check exists
   but is selected only when `search_backend: fts5` is set explicitly.
2. **`remember` through a real model lands outside the recall roots.** The tool
   docstring uses `"manual"` as the example `source`; in 6 of 7 calls the model
   passed it. `raw/manual/` is not in `recall.extra_intake_roots`, so the note
   was invisible to `recall` until its compile and the documented `[uncompiled]`
   behaviour never appeared. (Detail in the sibling report, criterion 3.)
3. **Compile with an unusable login: false `key_mode` OK and an opaque error.**
   Sibling report, criterion 4. Not a quick-start regression, but a first-session
   trap for anyone whose login has expired.
4. **README quick start still tells the user to `export ANTHROPIC_API_KEY=...`
   before `athenaeum run`**, with "or configure the claude-cli backend" as an
   aside, although `init` has already selected `claude-cli` on a machine with a
   logged-in `claude`.

## CI

Unchanged from the 2026-11-14 report. The install, `init`, `claude mcp add`,
`doctor` and `test-mcp` steps need no login and could run on every PR in a stock
container. The model-mediated tell and ask need an OAuth token secret and
about six `claude -p` calls plus one compile, so a scheduled or manual workflow
fits. Finding 1 is checkable without a login (run `doctor` after `session-end`
on a `[mcp]`-only install).

## Disposition

The Linux PyPI quick start now passes. athenaeum#2319 stays open for macOS and
the plugin path, and the quick-start docs claim in finding 1 needs either a
code fix or a wording change before the directory submission.
