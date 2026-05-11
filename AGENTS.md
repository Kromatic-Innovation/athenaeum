# Athenaeum — Agent Policy

## GitHub

- **Owner:** Kromatic-Innovation
- **Repo:** athenaeum
- **Package:** [athenaeum](https://pypi.org/project/athenaeum/) on PyPI
- **License:** Apache 2.0

## Project overview

Athenaeum is an entity-centric long-term memory system for AI agents.
Published to PyPI; tagged releases ship to real users.

## Branch policy

- Default branch: `develop`
- Tags cut from `main` after `develop → main` fast-forward
- PyPI publish workflow fires on tag push to `main`
  (`PYPI_RELEASE_ON_MAIN_TAG=true`)

**Repo metadata** (promotion model, traffic tier, Sentry projects, autonomous-loop
opt-in): see `~/Code/docs/project-registry.yaml` entry for
`Kromatic-Innovation/athenaeum`. Do not duplicate that metadata here.

## Release process

Athenaeum is on PyPI; release quality matters in a way that internal-only
repos don't impose. The README, install path, error UX, and public API
are seen by strangers who have no internal context.

Before any release tag (`vX.Y.Z`):

1. Confirm `develop` is at the intended release-candidate tip.
2. Run the workspace `/zenodotus` skill against this repo:
   ```
   /zenodotus --repo . --ref develop --version <X.Y.Z> --prior-tag <vA.B.C>
   ```
3. Zenodotus spawns a 4-persona no-context reviewer panel
   (drive-by installer, production evaluator, maintainer's maintainer,
   drive-by contributor) — each reading only the public surface
   (README, CHANGELOG, LICENSE, CONTRIBUTING, public API, tests, release
   diff) and **nothing else**. The verdict lands in
   `.zenodotus/<version>/verdict.md`.
4. Verdict gates the tag:
   - **Pass** → promote `develop → main` (fast-forward), then create
     `git tag vX.Y.Z` from `main` using the drafted
     `.zenodotus/<version>/tag-message.md` as the tag body. PyPI publish
     fires automatically on the tag push.
   - **Conditional** / **Fail** → fix the must-fix items on `develop`,
     re-run `/zenodotus`, retry.
5. Tagging stays human-triggered. Zenodotus does not run `git tag`.

The `.zenodotus/` directory is gitignored — verdict artifacts are local
record, not durable repo state.

Internal Quine review and CI remain in place; Zenodotus is **additive**, not
a substitute. Internal reviewers cannot unsee design intent; Zenodotus
reviewers cannot read it.

## Testing

- Unit + integration tests under `tests/`
- Run: `pytest`
- Coverage: `pytest --cov=athenaeum`

## Conventions

- Python 3.11+
- Apache 2.0 license; contributor sign-off via DCO
- Public API exposed via `athenaeum/__init__.py`; anything not in `__all__`
  is internal and subject to change without a major-version bump
