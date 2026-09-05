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

## Primary path: the main-pinned deploy worktree (`scripts/deploy-guard.sh`)

athenaeum#510 gave athenaeum the same fleet-standard deploy shape hestia and
voltaire already have: a dedicated, `main`-pinned deploy worktree, kept in sync
by `scripts/deploy-guard.sh` and stamped after every rebuild.

- **Location:** `$LOCAL_DEPLOYS_DIR/athenaeum` when `$LOCAL_DEPLOYS_DIR` is
  set (the cwc#1422 local-deploys migration), else `~/Code/athenaeum-deploy`
  on a machine that hasn't migrated yet. In the migrated (current) layout this
  is **`~/local-deploys/athenaeum`**. Resolution lives in the shared
  `scripts/lib/local-deploys.sh:local_deploy_dir` helper, matching every other
  repo's guard.
- **What runs there:** the worktree's `.venv` runs the athenaeum MCP stdio
  server (`~/local-deploys/athenaeum/.venv/bin/athenaeum serve`, spawned fresh
  per Claude Code session per `~/.claude.json`) and the nightly librarian. The
  venv installs athenaeum **editable** (`pip install -e ".[mcp,vector]"`), so
  moving the worktree to the deploy ref updates the running code without a
  reinstall; the venv refresh only needs to run when dependencies/entry points
  change.
- **Sync flow (`scripts/deploy-guard.sh`, default mode):** resolve the deploy
  dir → refuse to touch a **dirty** worktree (loud abort; a tree with local
  edits is never force-reset) → resolve `origin/main` (configurable via
  `ATHENAEUM_DEPLOY_REF`) → decide against **HEAD**, the running code, *not*
  the `dist/.build-sha` stamp (a derived output that a stale writer could leave
  matching the ref while HEAD did not): in-sync only when HEAD **and** the
  stamp are already at the ref → otherwise reconcile the worktree to the ref
  with `git reset --hard`, which — unlike `--ff-only` — moves HEAD forward **or
  backward**, so a rewind/rollback to an ancestor is actually applied instead
  of a silent no-op (athenaeum#614) → refresh the `.venv` → **re-read HEAD and
  require it to equal the ref** → re-stamp `dist/.build-sha` via
  `scripts/write_build_sha.py`. Any failure (dirty tree, reconcile failure, a
  HEAD that did not reach the ref, venv refresh, or stamp write) aborts loudly
  with a recovery hint, and the success line reports the **observed** HEAD, not
  the intended target. The hard reset is safe because a clean deploy checkout
  owns no local work; a dirty tree is refused first.
- **`scripts/deploy-guard.sh --check`** reports `pre-activation` / `in-sync` /
  `drift` / `error` and mutates nothing (exit `0` / `0` / `10` / `20`).
- **Why this file exists at all:** `hestia redeploy`'s guard discovery
  (hestia#802) looks for `<deploy>/scripts/deploy-guard.sh` (then
  `<deploy>/deploy-guard.sh`) and did not know about athenaeum's
  `scripts/deploy-sync.sh` — before `deploy-guard.sh` landed,
  `hestia redeploy --repos Kromatic-Innovation/athenaeum` reported
  `no-guard-script` and the deploy worktree only advanced on a manual pull
  (found 2 commits behind `origin/main` during the 2026-07-29 cwc redeploy
  audit, athenaeum#510). `deploy-guard.sh` is now the fleet-standard entrypoint
  `hestia redeploy` runs on the redeploy cadence, so a push to `main` becomes
  the deploy with no hand-rebuild required.

## The lighter-weight equivalent: `scripts/deploy-sync.sh`

`scripts/deploy-sync.sh` remains the operator-facing manual sync for a single
checkout (fast-forward to the deploy ref + stamp), and is what an operator
without the deploy worktree set up locally can run directly against their own
checkout:

- `scripts/write_build_sha.py` — writes `dist/.build-sha` from
  `git rev-parse HEAD`. Refuses to write anything that is not a 40-hex SHA.
  Root override for tests: `ATHENAEUM_BUILD_SHA_ROOT`.
- `scripts/deploy-sync.sh` — fast-forwards a checkout to its deploy ref
  (`ATHENAEUM_DEPLOY_REF`, default `main`) and then runs the stamp writer.
  `scripts/deploy-sync.sh --check` reports `in-sync` / `drift` without
  mutating anything (exit `0` / `10`).

  ```bash
  scripts/deploy-sync.sh          # sync to the deploy ref, rewrite the stamp
  scripts/deploy-sync.sh --check  # report drift only, mutate nothing
  ```

Both share the same `write_build_sha.py` stamp writer and produce the same
stamped, reinstalled checkout; they differ only in how they move the worktree —
`deploy-guard.sh` reconciles to the deploy ref with `git reset --hard` (so a
rewind is applied, athenaeum#614), while `deploy-sync.sh` fast-forwards. `deploy-guard.sh` is what the automated `hestia redeploy`
cadence runs against the worktree above, `deploy-sync.sh` is the manual
single-checkout path.

## Scope

This issue (athenaeum#413) covers only athenaeum's side — producing the stamp. Teaching
the cwc aggregator to read athenaeum's stamp path is tracked separately in
`code-workspace-config#1428`. The tag-triggered PyPI `release.yml` flow is a
different concern (published-package version, not running-instance version) and
is intentionally untouched.
