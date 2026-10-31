# Contributing to Athenaeum

Thank you for your interest in contributing to Athenaeum!

## Development setup

1. Fork and clone the repository
2. Install in development mode: `pip install -e ".[dev,vector]"`
3. Install the repo's git hooks: `bash scripts/install-git-hooks.sh`
4. Run the test suite: `pytest tests/ -v`, or `scripts/run-tests.sh` (see below)
5. Run the linter: `ruff check src/ tests/`

The `[dev,vector]` install matches what CI installs
(`.github/workflows/ci.yml`), so a fresh clone following these steps runs the
same test set CI runs — nothing is silently skipped. The `vector` extra pulls
in `chromadb`, which the vector-search and clustering tests need. If you don't
intend to touch search or clustering, `[dev]` alone works and those tests skip
automatically; MCP server tests need `fastmcp` (already included in `[dev]`).

Read the next section before you file a bug about a red suite: a handful of
tests depend on properties of your MACHINE rather than of athenaeum, and they
name the property they need when they skip.

## Test-suite environment contract

CI is green on `develop`. Your clean checkout may not be, and that is not
automatically a defect — parts of the suite need something from the environment
that CI happens to provide. Those preconditions are a documented contract, not
folklore. They live in one place, [`tests/env_contract.py`](tests/env_contract.py),
and a test whose precondition is unmet **skips with that precondition named**
rather than failing. If you see a bare failure in one of these areas, that is a
bug worth reporting (athenaeum#2136 is the issue that established this).

What the suite assumes:

- **Run from the repository root**, with your virtualenv's `bin/` on `PATH`.
  A number of tests spawn the installed `athenaeum` console script as a
  subprocess; without the venv on `PATH` they resolve a different interpreter.
- **A git committer identity** (`user.name` / `user.email`, or the
  `GIT_AUTHOR_*` / `GIT_COMMITTER_*` variables). Many fixtures `git init` and
  `git commit` a throwaway corpus; with no identity, those commits refuse and
  the tests fail inside the fixture rather than in the code under test.
  `tests/conftest.py`'s `_git_identity` fixture supplies one for the suite's own
  subprocesses.
- **No outbound network.** The default selection is offline *by enforcement*,
  not by convention: `tests/conftest.py` blocks every non-loopback socket
  connection, and routes chromadb's model-downloading embedding function
  through an offline stand-in. A test that needs the network carries the
  `eval`, `live` or `embedding` marker and is deselected by default.
- **`ANTHROPIC_API_KEY` is not needed** — and an EMPTY value is treated the
  same as an absent one. (An environment exporting the variable with no value
  used to fail six `tests/evals/test_write_path.py` tests; see athenaeum#2136.)
- **Host tooling the shell-hook tests shell out to** — `bash`, `git`, `jq`,
  and in places an `sqlite3` *binary* (the Python `sqlite3` module is a
  different thing and does not satisfy those). Each is probed by
  `tests/test_shell_hooks.py`'s `_require`, which skips naming the missing
  tool.
- **You do NOT need to isolate `HOME` or the XDG directories.** The suite
  isolates what it needs to: `tests/conftest.py`'s `pytest_configure` hook
  redirects `ATHENAEUM_CACHE_DIR` and `ATHENAEUM_SPEND_LEDGER` to a throwaway
  directory for the whole session *before collection begins* (a
  session-scoped fixture would be too late for a module that resolves the
  cache dir at import or parametrize time), and the `_isolate_cache_dir`
  fixture narrows that to one directory per test. Running with your real
  `HOME` does not touch your real `~/.cache/athenaeum`.
- **Do NOT, however, carry ambient athenaeum or knowledge configuration in
  your environment.** `KNOWLEDGE_ROOT`, `KNOWLEDGE_WIKI_PATH`,
  `KNOWLEDGE_RAW_PATH` and the `ATHENAEUM_*` knobs are honoured by config
  resolution exactly as they are in production, so an exported value wins
  over a test's own tmp fixture and the test fails on a path it never chose.
  Measured: with `KNOWLEDGE_WIKI_PATH` exported,
  `tests/test_cli.py::TestServe::test_serve_reads_vector_backend_from_config`
  resolves `wiki_root` to that value instead of its fixture and fails; unset
  it and the same test passes. If you keep such variables in your shell
  profile, run the suite with them cleared (`env -u KNOWLEDGE_WIKI_PATH …`).
  This is the one respect in which a *clean* environment is genuinely
  required, and it is the opposite of the HOME/XDG point above — the suite
  can isolate its own outputs, but it cannot tell your deliberate
  configuration apart from an accident.

### Known environment-sensitive modules

If the suite is red, check this list before anything else:

- `tests/test_cli.py` — `TestServe` / `TestRebuildIndex` need the vector
  backend, and `TestServe` is sensitive to ambient `KNOWLEDGE_*` variables.
- `tests/test_search.py` — `TestReindexUnderLiveServer` and eleven
  `pytest.importorskip("chromadb")` sites need the `[vector]` extra.
- `tests/test_runlock.py` — `TestIngestPathHeartbeatWiring`'s `session_end`
  case reindexes, so it needs the vector backend.
- `tests/test_shell_hooks.py` — shells out to `bash`/`git`/`jq`/`sqlite3`,
  isolates its own `HOME`, and its `rebuild-index.sh` cases drive the real
  vector backend.
- `tests/test_surface_divergence.py` — `TestInstalledCli` builds a wheel and
  a child venv; see `runtime_deps_in_base_interpreter` below.
- `tests/evals/test_write_path.py` — drives the real librarian end to end
  against a canned client; needs no credential, but see the
  `ANTHROPIC_API_KEY` note above.
- `tests/test_mcp_server.py`, `tests/test_scoped_access.py`,
  `tests/test_session_end.py`, `tests/test_delta.py`,
  `tests/test_delta_compile_equivalence.py`,
  `tests/test_librarian_clusters.py`, `tests/test_merge_fold_write_paths.py`,
  `tests/test_retrieval_golden_1420.py`, `tests/test_pii_off_corpus.py`,
  `tests/test_type_filter.py` — all carry `pytest.importorskip("chromadb")`
  guards for the `[vector]` extra (44 sites across the suite).

The properties enforced mechanically, by name, are:

- **`vector_backend`** — `chromadb` is importable, i.e. you installed the
  `[vector]` extra. Six tests need the real vector backend and cannot be
  faked; they skip under a `[dev]`-only install.
- **`runtime_deps_in_base_interpreter`** — athenaeum's runtime dependencies
  live in the *base* interpreter's `site-packages`.
  `tests/test_surface_divergence.py`'s fresh-install test builds a wheel and
  installs it into a `--system-site-packages` child venv; PEP 405 venvs do not
  nest, so that child can only import athenaeum's dependencies when they are in
  the base interpreter — true for CI (which `pip install`s straight into the
  runner's Python), false for the virtualenv this page tells you to create. The
  test skips in a virtualenv and runs in CI.
- **`append_only_protection_enforced`** — the host OS actually enforces the
  append-only flag `promptwitness.ledger._apply_append_only_protection`
  applies to a ledger partition (`chflags uappnd` on macOS; `chattr +a` on
  Linux). macOS enforces it for an ordinary process; Linux needs
  `CAP_LINUX_IMMUTABLE`, which a non-root CI/dev process does not hold, so
  `chattr +a` there is a silent no-op. A counter-example test in
  `tests/test_promptwitness_ledger.py` that proves the flag is "really being
  set" by expecting an unguarded rewrite to raise `PermissionError` can only
  be a real check where this property holds; it skips naming this property
  otherwise (see athenaeum#2255).

### The clean-venv CI job

`.github/workflows/ci.yml`'s `clean-venv` job installs exactly what step 2
above installs — `pip install -e ".[dev]"`, **without** `requirements-ci.lock`
— then collects the whole suite, runs a dependency-surface smoke subset, and
runs `scripts/check_declared_imports.py`. It exists because the undeclared-
`httpx` incident (athenaeum#2136) was invisible to every other job: CI installs
with the lock, which pinned an `anthropic` that still happened to carry `httpx`,
so a dependency the tests import directly and pyproject declared nowhere looked
fine for as long as the resolver cooperated.

In that job `ATHENAEUM_TEST_ENV_CONTRACT=strict` is set, which promotes every
contract skip above into a **failure** — otherwise "skipped" could mask
"undeclared", which is exactly the masking this job exists to prevent. A test
added to that job's smoke subset must therefore satisfy every contract property
a `[dev]`-only install provides.

`scripts/check_declared_imports.py` is runnable locally and needs no network:

```bash
python scripts/check_declared_imports.py
```

It fails, naming the import and its file, when `src/` or `tests/` imports a
third-party package that `pyproject.toml` declares nowhere. A genuinely
optional import is exempt when it is guarded by `try: ... except ImportError`;
a *lazy* import inside a function body is not (athenaeum#542 settled that —
deferring the crash does not declare the dependency).

## Git hooks: public-safe-lint at the push boundary

Cloning does not install hooks by itself, which is why step 3 above is a
separate, explicit command (safe to re-run). It sets `core.hooksPath` to
the tracked `.githooks/` directory, which wires in `.githooks/pre-push`
(athenaeum#1104).

That hook runs `public-safe-lint.sh` (the same org-agnostic leak scanner
`.github/workflows/public-safe-lint.yml` runs in CI) against a **clean
`git archive` export** of each commit you're about to push — never your
working tree, so gitignored clutter (`.venv`, `.zenodotus`, ...) can't hide
a real hit or, conversely, can't produce a false one CI would never see
either. It also fails if any rule's suppressions
(`.public-safe-lintignore`) aren't already reviewed and named in
`.public-safe-lint-suppression-allowlist` at the repo root — a rule that
starts being suppressed for the first time is a signal worth a human
noticing before it reaches a public remote, not something CI-after-the-fact
should be the first to catch.

**Bypass**, for a repo that is genuinely private, or a push that carries no
doc/prose change at all:

```bash
SKIP_PUBLIC_LINT=1 SKIP_PUBLIC_LINT_REASON=private-repo   git push ...
SKIP_PUBLIC_LINT=1 SKIP_PUBLIC_LINT_REASON=no-doc-change  git push ...
```

`SKIP_PUBLIC_LINT_REASON` must be one of those two values — an unset or
unrecognized reason is refused rather than silently allowed. Every bypass
is appended (never silently) to the untracked `.git/public-safe-lint-bypass.log`
with a timestamp, branch, remote and reason, and printed to the push
transcript.

**If you're pushing from a container or CI-like environment that forces
`core.hooksPath` to something else** (some sandboxed dev environments do
this workspace-wide, for their own unrelated hook needs — environment-based
git config outranks a repo's own `.git/config` setting, so the hook above
can be silently skipped there), use `scripts/git-push-safe.sh` instead of
`git push`; it forces the correct hooks path via `git -c`, which outranks
even that kind of override.
## Making "the tests passed" a checkable claim

Plain `pytest tests/ -v` is fine for a quick check or a single test
(`pytest tests/test_x.py::test_y -v` — nothing here changes that). For the
"is the full suite green" question that gates a PR, use
`scripts/run-tests.sh` instead (athenaeum#1105):

```bash
scripts/run-tests.sh                 # same as `pytest tests/`, plus a baseline diff
scripts/run-tests.sh tests/test_x.py # narrower target, same diff logic
scripts/run-tests.sh --update-baseline   # after confirming a new CI-environment failure is real, not a mistake
```

Two problems this closes, both hit in the same real session
(athenaeum#1105's own report): a pytest exit code silently masked by piping
through `tee`/`tail` (bash reports the *last* command's exit status, not
pytest's — this script never internally pipes pytest into anything; it
redirects straight to a file), and "the suite passed" being unfalsifiable
because the failing set is environment-sensitive — 0 on CI, a different
handful on each of two other hosts a past session measured.

`tests/known-ci-failures.txt` is the committed baseline this script diffs
against, and it is deliberately the **CI-environment** baseline only (empty,
since CI is green on develop) — not a stand-in for whatever your particular
host happens to fail. So a failure that is really an unmet precondition from
the [environment contract](#test-suite-environment-contract) does **not**
belong in this file: the fix is a named skip at the test, which is what makes
it legible to the next person, where a baseline line would only make it
invisible. The baseline stays a statement about CI.

A failing nodeid the script has never seen in that
file is reported under an **UNRECOGNIZED** heading and always makes the
script exit non-zero, even if you're fairly sure it's a pre-existing quirk
of your host rather than something your change broke — this script can't
tell those apart from a single run, so it surfaces the failure instead of
guessing. See the script's own header comment for the full rationale, and
`scripts/run-tests.sh --help`-style usage at the top of the file.

**If you're piping this script's own output** (`scripts/run-tests.sh | tee
run.log`), a bare `$?` afterward reports `tee`'s exit status, not this
script's — that is a property of the pipe, unrelated to anything this
script can control from inside itself. Read `${PIPESTATUS[0]}` (bash) for
the real exit code, or skip the pipe and redirect instead:
`scripts/run-tests.sh > run.log 2>&1`.

## Supported Python version

Athenaeum supports **one Python version at a time — currently 3.13.** CI
(`.github/workflows/ci.yml`) tests that version only.

This is a maintenance-burden decision, not a cost saving: athenaeum is a
public repo, so GitHub-hosted Linux runner minutes are free and unlimited
here, and the org pays nothing for extra matrix legs. What extra legs cost is
maintenance surface — every dependency bump has to satisfy every pinned
interpreter, every version-conditional code path has to be kept alive, and
every red leg has to be triaged even when the others are green. Read a
single-version matrix as intentional, not an oversight — do not re-add legs
to "restore compatibility" or "save CI cost" (there is none to save).

Bumping the supported version is one change, not three independent ones —
update these together, in the same PR:

- `strategy.matrix.python-version` in `.github/workflows/ci.yml` (plus the
  `types` job's `Set up Python` step and its `[tool.mypy] python_version` in
  `pyproject.toml`, which track the same version)
- `requires-python` in `pyproject.toml`
- the `Programming Language :: Python :: 3.*` trove classifiers in
  `pyproject.toml`

## Pull requests

- Open PRs against the `develop` branch. Never open a PR directly against `main` — `main` is the release branch and is only updated via the promotion workflow.
- Include tests for new functionality
- Add a `CHANGELOG.md` entry under `## [Unreleased]` — see [Changelog rules](#changelog-rules) for which category it belongs in and why a removal has to say so explicitly
- Ensure all existing tests pass
- Follow the existing code style (enforced by ruff)
- Keep mechanical reformat commits separate from behavior changes — a reviewer (or release gate) should never have to dig a logic change out of a formatting diff
- **Write tracker citations as `athenaeum#NNN`, never bare `#NNN`.** Athenaeum is periodically re-exported to a public tree, where a bare `#NNN` (in a CHANGELOG entry, a doc, or a code comment) ties public text back to a private issue tracker with no context. The required `public-safe-lint` check (folded into `CI Required`) flags a bare `#NNN` as `bare-issue-ref`, so qualifying it keeps your first commit green. A reference to another repo's tracker uses that repo's qualifier instead (e.g. `hestia#123`); an already-qualified GitHub link `[#123](https://github.com/owner/repo/issues/123)` is fine as-is.

## Inbound contribution licensing

Athenaeum does **not** require a Developer Certificate of Origin (DCO) sign-off
or a Contributor License Agreement. Inbound contributions are licensed under the
project's Apache-2.0 License by **Section 5** of that license: any contribution
you intentionally submit for inclusion in the work is under the same terms as
the work itself, with no separate agreement required. Opening a pull request
against this repository is that intentional submission — nothing further (no
`Signed-off-by` trailer, no `git commit -s`) is needed to certify it.

## Branch flow and promotion

Athenaeum uses a develop-first flow, matching the rest of the Kromatic repos:

1. **Feature work** — branch from `develop`, open a PR with `--base develop`, merge when CI is green.
2. **Release promotion** — once `develop` is in a shippable state, a maintainer triggers the [`Promote Main`](.github/workflows/promote-main.yml) workflow (`workflow_dispatch`). It validates that `main` is a strict ancestor of `develop`, confirms required CI checks passed on the `develop` SHA, and fast-forwards `main` to that SHA via the GitHub refs API. No merge commits are introduced on `main`, so `main` history stays linear.
3. **If the fast-forward precondition fails** (e.g., commits landed on `main` directly), open a `chore: sync develop with main` PR from `main` → `develop` first, then re-run the promotion.

There is no `staging` branch — unlike our deploy-pipeline repos, athenaeum is a library, and PyPI releases are handled separately via [`release.yml`](.github/workflows/release.yml).

The running MCP server records the commit it is on in `dist/.build-sha`, kept in sync via [`scripts/deploy-guard.sh`](scripts/deploy-guard.sh) (the automated path `hestia redeploy` runs against the main-pinned deploy worktree at `~/local-deploys/athenaeum`) or [`scripts/deploy-sync.sh`](scripts/deploy-sync.sh) (the manual, single-checkout equivalent), so cross-repo deploy-lag reporting can see how far behind `main` the deployed instance is — see [`docs/measurements/deploy-sha-stamp.md`](docs/measurements/deploy-sha-stamp.md). Both scripts also constrain that refresh under `requirements-ci.lock` when it is present in the deploy tree, for dependency parity with CI; after a fast-forward, run the [post-ship re-sync check](docs/measurements/deploy-sha-stamp.md#post-ship-re-sync-athenaeum1960) to confirm it landed.

## Versioning and releases

Athenaeum's version number is **not** a release count, and reading it as one is
the single most common way to misread this project's history.

- **PATCH is a build number.** Every merge to `develop` bumps the PATCH digit.
  Most of those versions are never tagged and never published — `0.22.43`
  means "the forty-third merge since the MINOR bump", not "the forty-third
  release". Do not bump the version by hand in a PR, and do not read a large
  PATCH digit as a long release history.
- **Releases are cut weekly**, from whatever `develop` reads at the time of the
  cut. There is no separate release-numbering step: the cut tags the version
  already in `pyproject.toml`, and the tag is what publishes — pushing a `v*`
  tag is the only trigger on
  [`release.yml`](.github/workflows/release.yml), which builds from the tag and
  publishes to PyPI with provenance attestations. A week with no merges
  produces no cut.
- **MINOR increments when the span being cut removed or broke public surface.**
  Athenaeum is pre-1.0, so under semver a MINOR bump is the breaking-change
  signal (`0.y.z` gives MAJOR no such role). The decision is derived from the
  changelog content for the span, not from a judgement call at cut time: if the
  span's entries contain a `### Removed` item or an entry marked breaking, the
  cut bumps MINOR and resets PATCH; otherwise the PATCH number stands as-is.
- **MAJOR is reserved for 1.0.** Until then it stays at `0`, and no change
  bumps it.

So a reader comparing two published versions should look at the MINOR digit to
ask "could this break me?", and ignore the PATCH digit except as an ordering
key. The same applies to the floor in a dependency pin: pin on MINOR, not on a
PATCH you happened to install.

**Where the bump happens, so you do not go looking for it here.** Only the
publish half is in this repository — `release.yml`, triggered by the tag. The
per-merge PATCH bump and the weekly cut are applied by the maintainer's merge
and release tooling, which is shared across repositories and lives outside this
one; there is deliberately no workflow or script here that does either. The
practical consequence for a contributor is the same either way: leave
`version` in `pyproject.toml` alone, and write the changelog entry — the cut
reads your entry, not your version number.

### Changelog rules

`CHANGELOG.md` is [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
format, with one addition established by `[0.22.43]` and expected of every
release section from here on. A release section is ordered:

1. **An `Upgrading from <previous tag>: breaking and removed` block** —
   collecting every breaking and removed item in the span, so an upgrader does
   not have to read the whole section to find what will bite them. Each bullet
   restates (and is also detailed in) a `### Removed` or `### Changed` entry
   further down; the block is a summary, never the only mention.
2. **A `Highlights` block** — a handful of bullets covering what a reader would
   actually want to know landed, grouped by theme rather than by category.
3. **The Keep-a-Changelog categories** — `### Added` / `### Changed` /
   `### Fixed` / `### Removed`. Because a release section can span many merges,
   these are grouped in merge order (newest first) and so each category may
   appear more than once in a single release section. That is expected; do not
   flatten a published section into one block per category.

For a contributor this means: **append your entry under `## [Unreleased]`**, in
the matching category, creating the category heading if it is not there yet. Do
not add an `Upgrading from` or `Highlights` block to `[Unreleased]` — both are
written at cut time, from the accumulated entries. If your change removes or
breaks public surface, say so explicitly in a `### Removed` entry (or mark the
`### Changed` entry as breaking), because that entry is what the cut reads to
decide whether to bump MINOR. Tracker citations follow the
`athenaeum#NNN` rule above.

## Project continuity

Athenaeum currently has a single primary maintainer, and it is worth saying so plainly: if the project goes quiet for a stretch, issues and PRs may sit unanswered. The mitigations are structural rather than aspirational. The code is Apache-2.0, so anyone can fork it and carry on without permission. The repository and its full history stay public on GitHub. And every release is reproducible from source — tags live on `main` and [`release.yml`](.github/workflows/release.yml) builds and publishes from the tag with provenance attestations — so you are never dependent on the maintainer to keep using or rebuilding what you already run. If you are betting on Athenaeum for something important and want to shrink that risk further, contributing reviews, tests, or docs is the most direct way to widen the bus factor.

**Most commits here are authored by bots, and you should know what that does and does not mean.** The `kromatic-development-agent[bot]` and `dijkstra-kromatic[bot]` identities account for the large majority of commits in a typical release window; the named human commit count is small by comparison. Those bots are autonomous coding agents operating under the maintainer's direction — they are not a second maintainer, and they do not widen the bus factor. Every branch they produce lands through the same pull request, the same required CI, and the same maintainer approval that a human contribution does; none of them can merge to `develop` or `main` on their own authority, and branch protection is what enforces that rather than convention. Read the commit graph as *one maintainer with a lot of automation*, because that is what it is. An external contributor's PR is reviewed by the same person, on the same terms.

## Reporting issues

Please use [GitHub Issues](https://github.com/Kromatic-Innovation/athenaeum/issues) to report bugs or request features.

## License

By contributing, you agree that your contributions will be licensed under the Apache 2.0 License.
