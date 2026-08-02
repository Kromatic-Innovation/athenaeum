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

## Checking the merge queue

Never hand-parse `wiki/_pending_merges.md` with grep/awk — it's a hand-rolled
markdown sidecar with nested code fences and multi-line fields, and doing
this by hand has already cost a full investigation arc (four failed attempts
on one question `athenaeum merges revalidate` answers in one call).
`parse_pending_merges()` (`src/athenaeum/pending_merges.py`) is the only
sanctioned reader; `athenaeum merges {list,next,count,revalidate,provenance}`
(source: `src/athenaeum/_cmd_merges.py`) is the sanctioned CLI surface built
on it. Full flag reference: [README.md § One unified "decisions needed"
list](README.md#one-unified-decisions-needed-list).

**"Is the merge queue healthy?" → run `athenaeum merges revalidate` first.**
It re-validates every unresolved proposal against the *current* suppression
gate, reports per-proposal `n_sources` and the suppression reason, and is
**dry-run by default** — `--apply` archives stale proposals to
`wiki/_pending_merges_archive.md` (moved, never deleted). Verified
2026-08-01: it retired 5 stale over-cluster proposals (36 → 31).

The MCP `list_pending_decisions` view carries **derived fields that do not
exist in the file** — `sources_omitted` (computed in `decisions.py`) is the
known example. Don't assume a field seen in that view is present in the raw
markdown or in `athenaeum merges` JSON output.

## Bundled skills

- `skills/adapter-authoring/SKILL.md` — **adapter-authoring**: how to build a
  custom source → raw-intake adapter against the contract in
  `docs/adapter-contract.md`. Ships inside the published package (under
  `skills/`) so any consumer — human or agent — can invoke it. Point a session
  at it whenever someone wants to feed a new external source into a knowledge
  base.

## Conventions

- Python 3.11+
- Apache 2.0 license; inbound contributions are covered by Apache-2.0 §5 (no
  DCO sign-off or CLA required)
- Public API exposed via `athenaeum/__init__.py`; anything not in `__all__`
  is internal and subject to change without a major-version bump
- **Tracker citations are written `athenaeum#NNN`, never bare `#NNN`** (a
  cross-repo ref uses that repo's qualifier, e.g. `hestia#123`). This repo is
  periodically re-exported to a public tree, where a bare `#NNN` ties public
  text back to a private tracker with no context; the required
  `public-safe-lint` check (`.github/workflows/public-safe-lint.yml`, folded
  into `CI Required`) flags a bare `#NNN` as `bare-issue-ref`. Writing the
  qualifier keeps your CHANGELOG line and doc/code comments green on their
  first commit — the check is not a formality, and a lane that writes a bare
  `#NNN` reds it.
