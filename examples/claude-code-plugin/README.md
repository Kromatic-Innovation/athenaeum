# Claude Code plugin (staged contents)

This directory is the **staged contents** of the athenaeum Claude Code
plugin: the manifest, the MCP server registration, the hooks config, and a
`uv`-only launcher. It is built and tested here, inside athenaeum's own CI
(issue athenaeum#2368).

**The installable plugin lives in a separate thin repo:**
[`Kromatic-Innovation/athenaeum-claude-plugin`](https://github.com/Kromatic-Innovation/athenaeum-claude-plugin).
That repo holds the manifest, the hooks and the launcher, and also serves as
the self-hosted marketplace (`.claude-plugin/marketplace.json`). It is
**private** for now. Publishing it waits on the install bugs athenaeum#2762
and athenaeum#2764, and the public flip is an operator step. Once public, the
install is:

```text
/plugin marketplace add Kromatic-Innovation/athenaeum-claude-plugin
/plugin install athenaeum@athenaeum-claude-plugin
```

Claude Code plugin marketplaces install from a *repository*, not from a
subdirectory of a larger repo, which is why the thin repo exists. This
directory remains the place the plugin is developed and tested; the thin repo
is where it ships. Tracking issue: athenaeum#2308. Nothing here has been
exercised against a real Claude Code install or marketplace by this repo's own
test suite (`tests/test_claude_code_plugin.py`), which proves only what needs
no login or network: every JSON file parses, every hook command resolves to a
real script, the version pin matches the release, and the launcher's two
documented failure/pending paths behave as described below. The clean-machine
acceptance test is part of athenaeum#2308.

## Keeping the thin repo in sync

The thin repo was populated from this directory, with one deliberate
difference: its hooks are self-contained.

`hooks/hooks.json` here references `${CLAUDE_PLUGIN_ROOT}/../claude-code/...`,
which resolves only because `examples/claude-code-plugin/` and
`examples/claude-code/` are siblings in this repo. `hooks.json` references
five reused hook scripts. The thin repo has no such sibling, so:

- **Four are vendored** into the thin repo's `hooks/` as verbatim copies, and
  its `hooks.json` points at `${CLAUDE_PLUGIN_ROOT}/hooks/<script>`:
  `session-start-recall.sh`, `user-prompt-recall.sh`,
  `wiki-context-inject.sh`, `pre-compact-save.sh`.
- **`pending-questions-surface.sh` is omitted** and its `hooks.json` entry
  removed, because athenaeum#2532 plans to remove that hook.

This repo's copies in `examples/claude-code/` remain the single source of
truth. The thin repo's copies must not be edited in place.

**At each release cut**, in the same pass that bumps the version pin (see
"Version-bump rule"), diff the vendored scripts against this repo's copies and
re-sync any that differ:

```bash
# from an athenaeum checkout at the release commit; THIN is the thin repo's clone
for f in session-start-recall user-prompt-recall wiki-context-inject pre-compact-save; do
  diff -q "examples/claude-code/$f.sh" "$THIN/hooks/$f.sh"
done
cp examples/claude-code/<script>.sh "$THIN/hooks/"   # for each one that differs
```

Also copy over any change to `hooks/launcher.sh`, `.mcp.json` and
`.claude-plugin/plugin.json`, apart from the thin-repo-only edits above. In
the thin repo, bump `version` in both `plugin.json` and `marketplace.json`,
and update the "Vendored from athenaeum commit" line in its README. The thin
repo's `CI Required` workflow checks JSON validity, script syntax and hook
path resolution, so a PR there must be green to merge.

`hooks.json`'s `${CLAUDE_PLUGIN_ROOT}` substitution and `.mcp.json`'s
`command` path both assume `hooks/launcher.sh` sits at the same relative
location in the thin repo (`hooks/launcher.sh` under the repo root).

Open items on the thin repo (tracked in athenaeum#2308): the thin repo
must be re-synced from this tree (the launcher, `.mcp.json` and `hooks.json`
changed in athenaeum#3046), the plugin has no icon, and `plugin.json` keeps a `_versionPinNote` field that Claude Code
ignores but `claude plugin validate --strict` warns about.

## Version-bump rule

`.claude-plugin/plugin.json`'s top-level `"version"` field is the **one**
pinned athenaeum release this plugin runs through `uv` (see
`hooks/launcher.sh`) — not an independent plugin-semver counter.

**The pin tracks the latest release, not per-merge bumps.** Every merge to
`develop` bumps `pyproject.toml`'s PATCH digit (see `docs/guides/install.md`'s
Quick start), but only a release tag publishes that version to PyPI. A pin to
an unpublished PATCH version would break plugin installs, so the pin names
the latest released version: the first dated `## [X.Y.Z] - <date>` heading in
`CHANGELOG.md` (the one after `## [Unreleased]`).

`tests/test_claude_code_plugin.py::test_manifest_version_pin_matches_latest_release`
fails CI when:

- the pin differs from that latest released CHANGELOG version, or
- the pin's MAJOR.MINOR differs from `pyproject.toml`'s `[project].version`,
  or the pin is greater than it.

The second check catches a pin that drifts far, for example a MINOR bump in
`pyproject.toml` made without a CHANGELOG cut. Per-merge PATCH bumps on
`develop` do not fail it.

**Bump the pin at the release cut, and nowhere else.** When a release
retitles `[Unreleased]` to `[X.Y.Z] - <date>`, update `plugin.json`'s
`"version"` to `X.Y.Z` in the same change.

## What happens when `uv` is missing (AC3)

`hooks/launcher.sh` requires `uv`, with no pip fallback. If `uv` is not on
`PATH`, it prints a message naming `uv` and what to install, and **exits
0** — a missing optional tool must degrade the session, not kill it, and
must never be a silently-dead hook. `athenaeum doctor`
(`src/athenaeum/doctor.py`) was the obvious place to borrow wording from,
but as of this writing it has **no uv-specific check at all** — it checks
the installed package, interpreter floor, MCP registration, hook presence,
LLM key mode, search index and schedule, never the presence of `uv` itself.
So the launcher's message is new wording, written to stay consistent with
`docs/guides/install.md`'s own "What happens when a prerequisite is
missing" section (the only existing athenaeum prose on this exact failure),
rather than a quote lifted from doctor.py.

The message must reach the person and the model, not just a debug log. In
`--ensure-ready` (the SessionStart hook) the launcher prints it to **stdout**
as hook JSON: `systemMessage` shows it to the user and
`hookSpecificOutput.additionalContext` hands it to the model. `hooks.json`
no longer discards that command's stderr or stdout (it keeps `|| true`, so
the session is never blocked). In the MCP passthrough mode stdout is the
protocol channel, so the same text goes to stderr there.
`tests/test_claude_code_plugin.py`
proves the exit-0-and-names-uv contract with a from-scratch minimal `PATH`
directory that genuinely lacks `uv` — never the real `PATH`.

## First run while the dependency build is pending (AC4)

The pinned athenaeum release is resolved through `uv run --with
'athenaeum[mcp]==<pin>'` (the `[mcp]` extra supplies `fastmcp`, which
`athenaeum serve` imports), which can take real time on a cold cache — longer than a
hook's timeout should ever have to wait. `hooks.json`'s `SessionStart` entry
therefore runs `launcher.sh --ensure-ready` first: it checks a ready marker
under `$ATHENAEUM_CACHE_DIR/claude-code-plugin/` (default
`~/.cache/athenaeum/claude-code-plugin/`, same `ATHENAEUM_CACHE_DIR`
resolution every other shipped hook already uses — no new environment
variable was introduced for this). If the marker is missing, it either
reports that another invocation is already preparing (a lock directory is
present) or takes the lock itself, forks the real `uv run` as a detached
background job, and returns immediately either way — the pattern
`examples/claude-code/session-start-recall.sh` already uses for its own
background vector-index rebuild (issue athenaeum#2204), reused here rather
than reinvented. The MCP start itself (`launcher.sh serve ...`) installs the pinned release and
creates a missing knowledge directory synchronously, so it never depends on
this warm-up having finished: Claude Code caches a failed first MCP connect
for about 15 minutes, and a first-session race must not turn into a dead
server. The session is told it is still preparing (recall running
in reduced/FTS5-only mode) until a later session finds the marker.
`tests/test_claude_code_plugin.py` drives this by creating the pending lock
directly, with no real `uv` invocation needed to prove the fast return.

### Recovering from an interrupted first run

A first-time dependency build is exactly the long operation a user is most
likely to interrupt — SIGKILL, closing the laptop, a reboot mid-build. If
the `--prepare` worker dies before reaching its own cleanup line, the lock
directory can survive it. Without a liveness check, every future
`--ensure-ready` would read the bare presence of that directory as "still
preparing" forever, with no self-recovery and no reason for an operator to
know a stray cache directory is the cause (PR athenaeum#2575 review).

`--ensure-ready` therefore records the worker's PID and a UTC-epoch start
time inside the lock directory (`pid`, `started-at`) and, on seeing an
existing lock, decides live vs. stale: **live** only if the recorded PID is
readable AND currently running (`kill -0`) AND the recorded start time is
within `STALE_LOCK_TIMEOUT_SECONDS` (`hooks/launcher.sh`, currently 1800s /
30 minutes — picked with headroom over a realistic cold `uv` build on a
slow link, deliberately not tight). Any missing or unreadable liveness
datum reads as **stale**, never as live by default — a worker that died
before writing its own PID file does not get mistaken for one still
running. A stale lock is cleared and the prepare worker relaunched
immediately, and the message says so explicitly ("previous attempt ...
appears to have died ... restarting it"), distinct from the plain "still
preparing" an operator sees while a worker is genuinely in flight.
`tests/test_claude_code_plugin.py::test_ensure_ready_recovers_from_a_dead_prepare_worker`
and `::test_ensure_ready_does_not_reclaim_a_genuinely_live_lock` cover both
directions — recovery, and the regression guard against reclaiming (and
thereby stampeding) a worker that is actually still running.

## `userConfig` and secrets — what we could and could not establish

The manifest declares one `userConfig` field, `knowledgePath` (the
`athenaeum serve --path` equivalent), following the shape of a real shipped
Claude Code plugin manifest available locally for inspection (an Anthropic
first-party plugin's `.claude-plugin/plugin.json`): a dict of
`{type, title, description, default, options?}` entries. That local example
pins down the FORM of `userConfig` but not its security properties, and
this repo has no network access to Claude Code's own documentation to
confirm the latter from the primary source. Specifically:

**`knowledgePath` is wired through `.mcp.json`.** The server entry's args
are `["serve", "--path", "${user_config.knowledgePath}"]`, so a configured
value reaches `athenaeum serve --path`. The launcher reads that `--path`
too: when the directory is missing, the first MCP start runs `athenaeum
init --path <dir>` (non-interactive, output sent to stderr) before `serve`,
and an empty or unsubstituted value falls back to `~/knowledge`. The
`${user_config.<key>}` substitution syntax follows Claude Code's plugin
documentation as recalled when this was written; it was not re-verified
against a live Claude Code in this repo's CI. If a Claude Code version does
not substitute it, the launcher sees the literal token and uses the default
path, so the failure mode is the old behaviour, not a dead server. The
default (`~/knowledge`) stays declared in `plugin.json`. Claude Code's
install screen was seen reporting the option as "not set" despite that
default; the launcher does not depend on it.

**The "not yet set" notice at install is expected.** `claude plugin install`
prints `1 userConfig option not yet set`, and `claude plugin configure` lists
`knowledgePath` as `(optional, not set)`. That is Claude Code's own wording and
the plugin cannot change it. The option is optional: leave it unset and the
server uses `~/knowledge`, creating it on first start. Set it only to keep
the knowledge base somewhere else. To make the default visible where Claude
Code does show the option's `title`, the manifest's title is `Knowledge base
path (default ~/knowledge)` and its description opens with `Optional: leave
unset to use ~/knowledge.` Whether the interactive `/plugin` screen shows
either was not checked.

- **Established:** `userConfig` values of `type: "string"`/`"boolean"` are
  the mechanism Claude Code plugins use to collect a user-supplied setting,
  with a `default` and (for enums) `options`. The one real example found
  locally declares several string/boolean settings and uses none of them
  for a credential.
- **Could NOT establish:** whether a `userConfig` value is stored
  encrypted, redacted from logs/telemetry, or otherwise treated as a secret
  by Claude Code itself. No `type: "secret"`/`"password"` variant, and no
  masking behavior, appears anywhere in the one real manifest schema found
  locally, which is at best weak negative evidence, not a confirmed
  "userConfig is not secret-safe" finding — nor a confirmed "it is",
  either.
- **Practical consequence (conservative):** treat `userConfig` as **NOT**
  secret-safe until that is confirmed, and do **not** put a real API key
  (`ANTHROPIC_API_KEY`, `JEV_API_KEY`) into a `userConfig` default or value.
  This plugin does not define a `userConfig` field for either key, on
  purpose — it leaves API-key handling entirely to the credential paths
  `examples/claude-code/README.md` already documents in depth ("Supplying
  an API key"): an inherited environment variable, or the plaintext
  (mode-`600`, not encrypted) `~/.cache/athenaeum/config.env` the
  SessionStart hook reads. Both of those are already documented as
  plaintext-on-disk, not "secret-safe" either — the gap here is narrower
  than it first looks, but `userConfig` specifically is unconfirmed, so it
  gets the more conservative default (don't use it for a key at all) rather
  than being assumed equivalent to a mechanism this repo has already
  described honestly as not encrypted.
- **What would confirm it:** Claude Code's own plugin-authoring
  documentation (unavailable here, no network egress), or a live Claude
  Code session inspecting where a `userConfig` value actually lands on
  disk / in logs after being set once.

## Why the hooks are referenced here, not copied

`hooks/hooks.json` points every recall-hook command at
`examples/claude-code/<script>.sh` via `${CLAUDE_PLUGIN_ROOT}/../claude-code/`,
not at a second copy under this directory. The issue this directory was
built for is explicit about why: a second copy of
`session-start-recall.sh`/`user-prompt-recall.sh`/etc. is a second copy that
can drift from the one actually tested and shipped in the wheel. Only
`hooks/launcher.sh` is new; it owns exactly the two things neither existing
script does — running everything through a pinned `uv` environment with no
pip fallback, and keeping `SessionStart` fast on a cold cache (see AC3/AC4
above). Neither `session-start-recall.sh` nor `user-prompt-recall.sh` was
modified to build this (both are the `.github/llm-surface.txt`-designated
sidecar delivery path; this directory's tests exercise them read-only, by
path resolution, never by editing their contents).

The thin repo is the one place a copy is made, and only as a release-time
vendoring of this repo's scripts (see "Keeping the thin repo in sync"), never
as an independently edited second copy.

## Running the tree's own tests

```bash
python -m pytest tests/test_claude_code_plugin.py -v
```

No network access, login, or real `uv` network call is required — the two
tested launcher paths (`uv` absent, a pending prepare lock already held)
return before touching the filesystem beyond the minimal fixture
`pytest` builds under `tmp_path`, and the inert `uv` stub those tests
construct is never actually executed.
