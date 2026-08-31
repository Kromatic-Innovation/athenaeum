# Contributing to Athenaeum

Thank you for your interest in contributing to Athenaeum!

## Development setup

1. Fork and clone the repository
2. Install in development mode: `pip install -e ".[dev,vector]"`
3. Run the test suite: `pytest tests/ -v`, or `scripts/run-tests.sh` (see below)
4. Run the linter: `ruff check src/ tests/`

The `[dev,vector]` install matches what CI installs
(`.github/workflows/ci.yml`), so a fresh clone following these steps runs the
same test set CI runs — nothing is silently skipped. The `vector` extra pulls
in `chromadb`, which the vector-search and clustering tests need. If you don't
intend to touch search or clustering, `[dev]` alone works and those tests skip
automatically; MCP server tests need `fastmcp` (already included in `[dev]`).

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
host happens to fail. A failing nodeid the script has never seen in that
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

The running MCP server records the commit it is on in `dist/.build-sha`, kept in sync via [`scripts/deploy-guard.sh`](scripts/deploy-guard.sh) (the automated path `hestia redeploy` runs against the main-pinned deploy worktree at `~/local-deploys/athenaeum`) or [`scripts/deploy-sync.sh`](scripts/deploy-sync.sh) (the manual, single-checkout equivalent), so cross-repo deploy-lag reporting can see how far behind `main` the deployed instance is — see [`docs/deploy-sha-stamp.md`](docs/deploy-sha-stamp.md).

## Project continuity

Athenaeum currently has a single primary maintainer, and it is worth saying so plainly: if the project goes quiet for a stretch, issues and PRs may sit unanswered. The mitigations are structural rather than aspirational. The code is Apache-2.0, so anyone can fork it and carry on without permission. The repository and its full history stay public on GitHub. And every release is reproducible from source — tags live on `main` and [`release.yml`](.github/workflows/release.yml) builds and publishes from the tag with provenance attestations — so you are never dependent on the maintainer to keep using or rebuilding what you already run. If you are betting on Athenaeum for something important and want to shrink that risk further, contributing reviews, tests, or docs is the most direct way to widen the bus factor.

## Reporting issues

Please use [GitHub Issues](https://github.com/Kromatic-Innovation/athenaeum/issues) to report bugs or request features.

## License

By contributing, you agree that your contributions will be licensed under the Apache 2.0 License.
