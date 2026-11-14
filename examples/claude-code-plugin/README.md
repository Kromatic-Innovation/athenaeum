# Claude Code plugin (staged contents)

This directory is the **staged contents** of the athenaeum Claude Code
plugin: the manifest, the MCP server registration, the hooks config, and a
`uv`-only launcher. It is built and tested here, inside athenaeum's own CI
(issue athenaeum#2368).

**What this directory is not.** It is not itself an installable plugin
repository, and nothing here publishes it to a marketplace. Claude Code
plugin marketplaces install from a *repository* (ideally one holding only
the plugin — a "thin repo"), not from a subdirectory of a larger repo.
Creating that separate repo, publishing it from a self-hosted marketplace,
and the clean-machine acceptance test that proves a fresh install actually
works end-to-end are issue athenaeum#2308's job, not this one's. Nothing in
this directory has been exercised against a real Claude Code install or a
real marketplace — only the pieces this repo's own test suite
(`tests/test_claude_code_plugin.py`) can prove without a login or network
access: every JSON file parses, every hook command resolves to a real
script, the version pin matches the release, and the launcher's two
documented failure/pending paths behave as described below.

## Copying this to the thin repo

When athenaeum#2308 creates the separate plugin repository, copy this
directory's contents to that repo's root as a straight, literal copy:

```bash
# from an athenaeum checkout, into the thin repo's working tree
cp -r examples/claude-code-plugin/. /path/to/the-thin-repo/
```

Two things do **not** travel with a plain copy and need a decision at that
point (tracked by athenaeum#2308, not resolved here):

- `hooks/hooks.json` references `${CLAUDE_PLUGIN_ROOT}/../claude-code/...`
  for the four hooks it reuses from this repo's `examples/claude-code/`
  (session-start-recall.sh, user-prompt-recall.sh, wiki-context-inject.sh,
  pending-questions-surface.sh) — see "Why the hooks are referenced, not
  copied" below. That relative path only resolves because, in *this* repo,
  `examples/claude-code-plugin/` and `examples/claude-code/` are siblings.
  The thin repo has no `examples/claude-code/` sibling, so athenaeum#2308
  must either vendor those four scripts into the thin repo at publish time
  (keeping this repo's copies as the single source of truth, re-synced per
  release) or pin the thin repo's hooks.json to fetch them some other way.
  Do not solve this by copying the scripts into the thin repo by hand once
  and letting them drift — that is the exact "second copy that can drift"
  this issue was told to avoid.
- `hooks.json`'s `${CLAUDE_PLUGIN_ROOT}` substitution and `.mcp.json`'s
  `command` path both assume `hooks/launcher.sh` ships at the SAME relative
  location in the thin repo (`hooks/launcher.sh` under the repo root) — a
  straight copy preserves this automatically; a restructure would not.

## Version-bump rule

`.claude-plugin/plugin.json`'s top-level `"version"` field is the **one**
pinned athenaeum release this plugin runs through `uv` (see
`hooks/launcher.sh`) — not an independent plugin-semver counter.
`tests/test_claude_code_plugin.py::test_manifest_version_pin_matches_pyproject`
fails CI the moment this value differs from `pyproject.toml`'s
`[project].version`.

**Bump both together, at the weekly release, and nowhere else.** The plugin
does not follow develop's PATCH-per-merge version churn (see
`docs/guides/install.md`'s Quick start on why the PATCH digit moves on every
merge) — it pins one tested release and advances deliberately. Concretely:
when the weekly release cuts a new `pyproject.toml` version, update
`plugin.json`'s `"version"` to match in the SAME change (or immediately
after, before the next plugin-affecting PR lands) — never let the two drift
for more than one commit, because the drift test above is the only thing
that would catch it.

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
rather than a quote lifted from doctor.py. `tests/test_claude_code_plugin.py`
proves the exit-0-and-names-uv contract with a from-scratch minimal `PATH`
directory that genuinely lacks `uv` — never the real `PATH`.

## First run while the dependency build is pending (AC4)

The pinned athenaeum release is resolved through `uv run --with
athenaeum==<pin>`, which can take real time on a cold cache — longer than a
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
than reinvented. The session is told it is still preparing (recall running
in reduced/FTS5-only mode) until a later session finds the marker.
`tests/test_claude_code_plugin.py` drives this by creating the pending lock
directly, with no real `uv` invocation needed to prove the fast return.

## `userConfig` and secrets — what we could and could not establish

The manifest declares one `userConfig` field, `knowledgePath` (the
`athenaeum serve --path` equivalent), following the shape of a real shipped
Claude Code plugin manifest available locally for inspection (an Anthropic
first-party plugin's `.claude-plugin/plugin.json`): a dict of
`{type, title, description, default, options?}` entries. That local example
pins down the FORM of `userConfig` but not its security properties, and
this repo has no network access to Claude Code's own documentation to
confirm the latter from the primary source. Specifically:

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

## Why the hooks are referenced, not copied

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

## Running the tree's own tests

```bash
python -m pytest tests/test_claude_code_plugin.py -v
```

No network access, login, or real `uv` network call is required — the two
tested launcher paths (`uv` absent, a pending prepare lock already held)
return before touching the filesystem beyond the minimal fixture
`pytest` builds under `tmp_path`, and the inert `uv` stub those tests
construct is never actually executed.
