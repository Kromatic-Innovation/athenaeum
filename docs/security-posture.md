# Security & Dependency Maintenance Posture

Last reviewed: 2026-05-12. Next scheduled review: **2026-08-06**.

This document records the threat model and dependency-upgrade policy for `athenaeum`. Patterned after the Kromatic games-and-tools workspace posture (origin: `plinkromatic/docs/security-posture.md`), adapted for this repo's published-library shape.

## 1. Shape of this repo (the load-bearing fact)

Athenaeum is an **open-source Python library** (Apache 2.0) published to PyPI. It is not a deployed service.

- Built with `hatchling` from `pyproject.toml`.
- Released via `.github/workflows/release.yml` on git-tag push (`v*`).
- Tested across Python 3.11 / 3.12 / 3.13 in CI.
- Critical deps have **explicit upper-bound caps** documented in pyproject.toml comments:
  - `anthropic>=0.30.0,<1.0` — "pre-1.0 and ships breaking changes freely"
  - `pydantic>=2.0,<3.0`
  - `chromadb>=0.5.0,<2.0` — "has shipped sqlite schema migrations in minor bumps"
  - `fastmcp>=2.0.0,<3.0` — pre-1.0
- Third-party GitHub Actions are **pinned to SHAs** in both CI and release workflows for supply-chain hygiene.

The threat model is "**library consumer drift**" — a dep that ships a subtle behavior change can affect every athenaeum user's deployment without them noticing. Patches are usually safe; minors on the explicitly-flagged deps need a human eye.

## 2. What this library does and doesn't do

| Surface | Present? | Notes |
|---|---|---|
| User-facing service | **No** | Athenaeum is a library, run by consumers in their own processes. |
| Direct network access | Via consumers' use | When wrapping the Anthropic API, requests go from the consumer's process. |
| Local file/SQLite operations | Yes (chromadb optional extra) | SQLite schema migrations have happened in chromadb minors — hence the hold list. |
| Build-time secret handling | At release time only | Trusted-publishing identity uses GitHub OIDC; no long-lived PyPI token. |

## 3. Dependency-upgrade policy

This repo follows the Kromatic maintenance-posture playbook, with one critical adaptation: **the package's own upper-bound caps in `pyproject.toml` define what Dependabot can propose at all.** Auto-merge eligibility is layered on top of those caps.

### 3.1 Patch and minor bumps (Dependabot)

Auto-merge is wired in `.github/workflows/dependabot-auto-merge.yml`. The policy:

- **All patch updates** (`x.y.Z`) auto-merge when CI is green (matrix across Python 3.11/3.12/3.13).
- **Minor updates** (`x.Y.z`) auto-merge **except** when the PR touches a hold-list package:
  - `anthropic` — pre-1.0, breaking changes ship freely per pyproject.toml comment.
  - `fastmcp` — pre-1.0, MCP protocol may shift.
  - `chromadb` — minor bumps have migrated SQLite schemas (data loss on downgrade).
- **Major updates** never auto-merge — and most would exceed the pyproject.toml upper bounds anyway, so Dependabot can't propose them within current caps.

### 3.2 Major version bumps + cap bumps

A "major bump" here is sometimes Dependabot proposing to raise the upper-bound cap in `pyproject.toml` (rather than the dep itself). Disposition:

- **Anthropic 1.0** when it ships → schedule a focused PR. Audit breaking changes. Bump the cap and run the full test matrix.
- **Pydantic 3.0** → same.
- **Chromadb 2.0** → review SQLite migration path; consumers' existing databases must keep working.
- **Fastmcp 3.0** → audit MCP protocol changes.

For all of these, the worktree-internal version cap shift is the actual change; the dependency bump is a consequence.

### 3.3 Quarterly review checkpoint

Quarterly (next: 2026-08-06):

1. Confirm `anthropic` is still < 1.0 (or update strategy if 1.0 ships).
2. Confirm `chromadb` minor releases haven't introduced an SQLite migration that would break consumers downgrading.
3. Run `pip-audit` against the resolved lockfile in CI logs.
4. Confirm action SHA pins are still up-to-date with their semver tags.
5. Re-evaluate hold list. If any pre-1.0 dep has stabilized (e.g. Anthropic 1.0), remove from hold list.

## 4. What CI already enforces

- **`ci.yml`** — pytest across Python 3.11/3.12/3.13. Matrix testing catches Python-version-specific dep breakage.
- **`release.yml`** — gated on tag push; trusted-publishing OIDC.
- Action SHA pinning provides supply-chain protection beyond what Dependabot offers.

## 5. Coverage snapshot

Test suite is pytest with pytest-cov. Coverage was not measured in this session due to disk constraints; should be captured at Q3 review.

The 3-Python-version test matrix is itself meaningful coverage — many dep regressions show up version-specifically.

## 6. Pointers

- Maintenance-posture origin: [plinkromatic#371](https://github.com/Kromatic-Innovation/plinkromatic/issues/371) and `plinkromatic/docs/security-posture.md`.
- Auto-merge workflow: `.github/workflows/dependabot-auto-merge.yml`.
- Dependabot grouping config: `.github/dependabot.yml`.
- Distribution: PyPI (`pyproject.toml` + `hatchling`).
- Releases: `.github/workflows/release.yml` (triggered by `v*` tags).
- Supply-chain hygiene: action SHA pinning (see workflow comments).
