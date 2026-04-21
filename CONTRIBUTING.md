# Contributing to Athenaeum

Thank you for your interest in contributing to Athenaeum!

## Development setup

1. Fork and clone the repository
2. Install in development mode: `pip install -e ".[dev]"`
3. Run the test suite: `pytest tests/ -v`
4. Run the linter: `ruff check src/ tests/`

## Pull requests

- Open PRs against the `develop` branch. Never open a PR directly against `main` — `main` is the release branch and is only updated via the promotion workflow.
- Include tests for new functionality
- Ensure all existing tests pass
- Follow the existing code style (enforced by ruff)

## Branch flow and promotion

Athenaeum uses a develop-first flow, matching the rest of the Kromatic repos:

1. **Feature work** — branch from `develop`, open a PR with `--base develop`, merge when CI is green.
2. **Release promotion** — once `develop` is in a shippable state, a maintainer triggers the [`Promote Main`](.github/workflows/promote-main.yml) workflow (`workflow_dispatch`). It validates that `main` is a strict ancestor of `develop`, confirms required CI checks passed on the `develop` SHA, and fast-forwards `main` to that SHA via the GitHub refs API. No merge commits are introduced on `main`, so `main` history stays linear.
3. **If the fast-forward precondition fails** (e.g., commits landed on `main` directly), open a `chore: sync develop with main` PR from `main` → `develop` first, then re-run the promotion.

There is no `staging` branch — unlike our deploy-pipeline repos, athenaeum is a library, and PyPI releases are handled separately via [`release.yml`](.github/workflows/release.yml).

## Reporting issues

Please use [GitHub Issues](https://github.com/Kromatic-Innovation/athenaeum/issues) to report bugs or request features.

## License

By contributing, you agree that your contributions will be licensed under the Apache 2.0 License.
