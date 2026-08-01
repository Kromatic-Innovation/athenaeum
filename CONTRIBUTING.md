# Contributing to Athenaeum

Thank you for your interest in contributing to Athenaeum!

## Development setup

1. Fork and clone the repository
2. Install in development mode: `pip install -e ".[dev,vector]"`
3. Run the test suite: `pytest tests/ -v`
4. Run the linter: `ruff check src/ tests/`

The `[dev,vector]` install matches what CI installs
(`.github/workflows/ci.yml`), so a fresh clone following these steps runs the
same test set CI runs — nothing is silently skipped. The `vector` extra pulls
in `chromadb`, which the vector-search and clustering tests need. If you don't
intend to touch search or clustering, `[dev]` alone works and those tests skip
automatically; MCP server tests need `fastmcp` (already included in `[dev]`).

## Pull requests

- Open PRs against the `develop` branch. Never open a PR directly against `main` — `main` is the release branch and is only updated via the promotion workflow.
- Include tests for new functionality
- Ensure all existing tests pass
- Follow the existing code style (enforced by ruff)
- Keep mechanical reformat commits separate from behavior changes — a reviewer (or release gate) should never have to dig a logic change out of a formatting diff
- Sign off every commit under the DCO (see the next section)

## Developer Certificate of Origin (DCO)

Athenaeum requires every commit to be signed off under the [Developer
Certificate of Origin](https://developercertificate.org/). This is the same
requirement recorded in `AGENTS.md`; the two documents agree, and this section
is the authoritative "how." Signing off certifies that you wrote the change (or
otherwise have the right to submit it) and that it may be distributed under the
project's Apache 2.0 License.

Add the sign-off with the `-s` / `--signoff` flag when you commit:

```bash
git commit -s -m "your message"
```

That appends a trailer in exactly this form, using the real name and email in
your Git config (`git config user.name` / `git config user.email`):

```
Signed-off-by: Jane Doe <jane@example.com>
```

Every commit in a pull request must carry this trailer. If you forget it on the
most recent commit, `git commit --amend -s --no-edit` adds it; to sign off a
whole range, use `git rebase --signoff <base>`.

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
