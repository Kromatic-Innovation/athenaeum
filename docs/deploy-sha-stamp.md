# Deploy-SHA stamp (`dist/.build-sha`)

athenaeum records the commit SHA it is currently running in a stamp file so the
Internal Platform deploy-lag aggregator (`code-workspace-config`'s
`scripts/compute-deploy-lag.sh`, cwc#1428) can answer "is what's actually
running behind `main`?" for athenaeum the same way it already does for hestia
and voltaire.

## What and where

- **Path:** `dist/.build-sha`, relative to the running checkout's repo root.
- **Format:** a single 40-character lowercase-hex commit SHA followed by one
  trailing newline — nothing else. This is byte-compatible with hestia's and
  voltaire's stamp: their readers do `tr -d '[:space:]' < dist/.build-sha` and
  expect a bare SHA, so a reader only needs athenaeum's **path** to differ, not
  its content shape.
- **Not committed:** `dist/` is gitignored. The stamp is a local build/deploy
  artifact, regenerated on every sync — never a tracked file.

## Why a single checkout (not a `-deploy` worktree)

hestia and voltaire each run from a dedicated `main`-pinned `<repo>-deploy`
worktree kept in sync by their `scripts/deploy-guard.sh`, which stamps
`dist/.build-sha` after each rebuild. athenaeum's MCP server instead runs
directly from a single source checkout with no separate deploy path (see the
2026-07-21 audit, hestia#691) — so it uses the **lighter-weight equivalent**:
`scripts/deploy-sync.sh` fast-forwards that one checkout to its deploy ref and
stamps the running commit. If athenaeum ever adopts a dedicated deploy worktree,
the same `scripts/write_build_sha.py` stamp writer drops into a guard flow
unchanged (exactly the writer↔guard relationship voltaire already has).

## How it is written

- `scripts/write_build_sha.py` — writes `dist/.build-sha` from
  `git rev-parse HEAD`. Refuses to write anything that is not a 40-hex SHA.
  Root override for tests: `ATHENAEUM_BUILD_SHA_ROOT`.
- `scripts/deploy-sync.sh` — the deploy-sync entrypoint: fast-forwards the
  checkout to its deploy ref (`ATHENAEUM_DEPLOY_REF`, default `main`) and then
  runs the stamp writer. `scripts/deploy-sync.sh --check` reports
  `in-sync` / `drift` without mutating anything (exit `0` / `10`).

  ```bash
  scripts/deploy-sync.sh          # sync to the deploy ref, rewrite the stamp
  scripts/deploy-sync.sh --check  # report drift only, mutate nothing
  ```

## Scope

This issue (#413) covers only athenaeum's side — producing the stamp. Teaching
the cwc aggregator to read athenaeum's stamp path is tracked separately in
`code-workspace-config#1428`. The tag-triggered PyPI `release.yml` flow is a
different concern (published-package version, not running-instance version) and
is intentionally untouched.
