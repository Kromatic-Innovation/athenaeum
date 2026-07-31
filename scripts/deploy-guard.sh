#!/usr/bin/env bash
# Deterministic-deploy drift guard for athenaeum (athenaeum#510).
#
# Adopts the shared, language-aware deploy-guard contract (cwc#1470, homed in
# cron-fleet; mirrors apollo-enrich/scripts/deploy-guard.sh,
# athenaeum-adapters/scripts/deploy-guard.sh, and hestia/voltaire),
# CONFIGURED FOR ATHENAEUM'S PYTHON DEPLOY: a main-pinned deploy worktree at
# `$LOCAL_DEPLOYS_DIR/athenaeum` whose `.venv` runs the athenaeum MCP stdio
# server (`~/local-deploys/athenaeum/.venv/bin/athenaeum serve`, spawned fresh
# every Claude Code session per `~/.claude.json`) and the nightly librarian.
# Since the venv installs the package EDITABLE (`pip install -e .`), a
# fast-forward updates the running code; the venv only needs a
# `pip install -e ".[mcp,vector]"` refresh to pull new/changed deps and entry
# points. So on drift this guard fast-forwards the deploy worktree to
# origin/main, refreshes the venv with athenaeum's real deploy extras, then
# stamps the running commit into `dist/.build-sha` via
# `scripts/write_build_sha.py` — the same bare-SHA marker deploy-sync.sh writes
# and the cross-repo deploy-lag aggregator (cwc#1428) reads.
#
# WHY IT LIVES AT scripts/deploy-guard.sh (and duplicates deploy-sync.sh's
# work): `hestia redeploy`'s guard discovery (hestia#802) looks for
# `<deploy>/scripts/deploy-guard.sh` first, then `<deploy>/deploy-guard.sh` —
# it does NOT know about athenaeum's bespoke `scripts/deploy-sync.sh`, so before
# this file existed `hestia redeploy --repos Kromatic-Innovation/athenaeum`
# reported `no-guard-script` and the checkout only advanced on a manual pull
# (found 2 commits behind origin/main during the 2026-07-29 cwc redeploy audit,
# athenaeum#510). This guard is the fleet-standard entrypoint `hestia redeploy`
# runs on the redeploy cadence, so pushing to `main` becomes the deploy with no
# hand-rebuild. `deploy-sync.sh` remains the operator-facing manual sync; both
# fast-forward + reinstall + stamp identically, so they are interchangeable.
#
# WHY IT IS SAFE: this is a STANDALONE guard — run as its own process by
# `hestia redeploy` and by an operator's `--check` — NOT a persistent watcher
# and NOT a daemon restarter. athenaeum's only consumer is the on-demand MCP
# stdio spawn, which picks up whatever is on disk at its next spawn, so there is
# nothing to kickstart. On a DIRTY or DIVERGED deploy worktree the guard aborts
# LOUDLY with a recovery hint and NEVER force-resets.
#
# FLOW (default mode):
#   1. Resolve the deploy dir (ATHENAEUM_DEPLOY_DIR override, else the cwc
#      local_deploy_dir contract via scripts/lib/local-deploys.sh:
#      $LOCAL_DEPLOYS_DIR/athenaeum when set, else ~/Code/athenaeum-deploy).
#      Absent -> PRE-ACTIVATION no-op: log + exit 0 (safe to land before the
#      deploy checkout exists on a given machine).
#   2. Refuse to touch a DIRTY deploy worktree (main-pinned, never hand-edited)
#      -> loud abort + recovery hint, never a force-reset.
#   3. Resolve origin/<ref> (default main). Transient fetch/resolve failure ->
#      leave the checkout as-is (never block dispatch on a network blip).
#   4. Decide against HEAD -- the RUNNING code -- NOT the dist/.build-sha stamp.
#      The stamp is a derived output with two writers (this guard and
#      deploy-sync.sh); trusting it as the drift oracle is how a rewind went
#      silently unsynced (athenaeum#614). In-sync == HEAD == origin/<ref> AND
#      the stamp already records it -> exit 0, mutating NOTHING (idempotent).
#   5. Drift (HEAD != origin/<ref>, in EITHER direction -- including a REWIND /
#      rollback to an ANCESTOR, which --ff-only treats as a successful no-op)
#      -> reconcile the checkout to origin/<ref> with `git reset --hard` (the
#      deploy checkout owns no local work, and the dirty-tree refusal in step 2
#      still guards against clobbering local edits), refresh the venv
#      (`pip install -e ".[mcp,vector]"`), then restamp dist/.build-sha.
#   6. POST-CONDITION: re-read HEAD and require it to equal origin/<ref> NOW.
#      The success line reports the OBSERVED HEAD, never the intended target.
#      A reconcile/venv/stamp FAILURE -- or a HEAD that did not actually move to
#      the ref -- aborts LOUDLY (stderr + non-zero exit) with a recovery hint,
#      rather than a false "synced". A DIRTY deploy worktree is still never
#      force-reset (step 2 refuses it first).
#
# --check: print a decision and mutate nothing
#   (pre-activation | in-sync | drift | error  ->  exit 0 | 0 | 10 | 20).
#
# TEST/CI HOOKS (offline determinism -- never set in production):
#   ATHENAEUM_DEPLOY_DIR      explicit deploy-checkout override (shared name with
#                             deploy-sync.sh). Default here: local_deploy_dir athenaeum.
#   ATHENAEUM_DEPLOY_REF      ref to track (default: main; shared with deploy-sync.sh)
#   ATHENAEUM_GUARD_REF_SHA   inject resolved origin/<ref> sha (skip fetch+rev-parse)
#   ATHENAEUM_GUARD_FETCH=0   skip `git fetch`
#   ATHENAEUM_GUARD_RECONCILE_CMD  reconcile command run on drift
#                             (default: git -C <dir> reset --hard origin/<ref> --
#                             moves HEAD forward OR backward, unlike --ff-only).
#                             ATHENAEUM_GUARD_FF_CMD is accepted as a DEPRECATED
#                             alias for back-compat with older callers/tests.
#   ATHENAEUM_GUARD_INSTALL_CMD  venv-refresh command run on drift
#                             (default: python3 -m venv .venv && .venv/bin/pip
#                              install -q -e ".[<extras>]")
#   ATHENAEUM_GUARD_STAMP_CMD    stamp command run after a successful refresh
#                             (default: python3 <scriptdir>/write_build_sha.py against <dir>)
#   ATHENAEUM_DEPLOY_EXTRAS   pip extras for the default install (default: mcp,vector —
#                             what the MCP server + librarian's vector search need;
#                             matches deploy-sync.sh)
#   ATHENAEUM_GUARD_LOG_DIR   append a JSONL abort line here on abort (default: stderr only)

set -u

# --- config ----------------------------------------------------------------
# shellcheck source=lib/local-deploys.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/lib/local-deploys.sh"

_dg_script_dir() { ( cd "$(dirname -- "${BASH_SOURCE[0]}")" && pwd ); }

_dg_deploy_dir() {
  if [ -n "${ATHENAEUM_DEPLOY_DIR:-}" ]; then
    printf '%s' "$ATHENAEUM_DEPLOY_DIR"
    return 0
  fi
  # cwc local_deploy_dir contract (cwc#1459/#1422), via the sourced helper:
  # $LOCAL_DEPLOYS_DIR/athenaeum once the machine has run the cwc#1422 migration,
  # ~/Code/athenaeum-deploy before it. This is how the guard resolves the same
  # deploy checkout the sibling repos' guards resolve (athenaeum#510 AC2).
  local_deploy_dir athenaeum
}
_dg_ref()          { printf '%s' "${ATHENAEUM_DEPLOY_REF:-main}"; }
_dg_extras()       { printf '%s' "${ATHENAEUM_DEPLOY_EXTRAS:-mcp,vector}"; }

_dg_is_checkout()  { [ -d "$1/.git" ] || [ -f "$1/.git" ]; }

# Resolve the deploy-ref SHA. Echoes the sha on success; non-zero on an
# unresolvable ref (transient fetch/network failure or a missing ref).
_dg_resolve_ref_sha() {
  local dir="$1" ref="$2"
  if [ -n "${ATHENAEUM_GUARD_REF_SHA:-}" ]; then
    printf '%s' "$ATHENAEUM_GUARD_REF_SHA"
    return 0
  fi
  if [ "${ATHENAEUM_GUARD_FETCH:-1}" != "0" ]; then
    git -C "$dir" fetch --quiet --no-tags origin "$ref" 2>/dev/null || return 3
  fi
  git -C "$dir" rev-parse --verify --quiet "origin/$ref" 2>/dev/null || return 3
}

# The built marker `hestia redeploy` + deploy-sync.sh read: the trimmed bare SHA
# in dist/.build-sha, or empty when the checkout has never been stamped.
_dg_build_sha() {
  local dir="$1"
  if [ -f "$dir/dist/.build-sha" ]; then
    tr -d '[:space:]' < "$dir/dist/.build-sha"
  fi
}

# The deploy checkout's ACTUAL current HEAD sha -- the running code, and this
# guard's drift oracle (athenaeum#614). Empty when HEAD is unreadable.
_dg_head() { git -C "$1" rev-parse HEAD 2>/dev/null; }

# True when the deploy worktree has no uncommitted changes.
_dg_worktree_clean() { [ -z "$(git -C "$1" status --porcelain 2>/dev/null)" ]; }

# The default venv refresh (the "configured for Python" step): (re)build the
# deploy checkout's own .venv exactly as deploy-sync.sh does. `pip install -e .`
# is idempotent, and the extras pull the MCP server + vector-search deps.
_dg_default_install_cmd() {
  printf 'python3 -m venv .venv && .venv/bin/pip install -q -e ".[%s]"' "$(_dg_extras)"
}

# The default drift reconcile: hard-reset the deploy checkout to origin/<ref>.
# Unlike `merge --ff-only`, this moves HEAD in EITHER direction, so a REWIND /
# rollback to an ancestor is actually applied instead of a silent no-op that
# leaves the deploy stale while the guard reports success (athenaeum#614). Safe
# because the deploy checkout owns no local work and the dirty-tree refusal in
# _dg_sync runs first.
_dg_default_reconcile_cmd() {
  printf 'git -C "%s" reset --hard "origin/%s"' "$1" "$2"
}

# The default stamp step: write the running commit into <dir>/dist/.build-sha
# via the write_build_sha.py that ships alongside this guard (same pattern as
# deploy-sync.sh — decoupled so it can stamp a checkout other than its own).
_dg_default_stamp_cmd() {
  printf 'ATHENAEUM_BUILD_SHA_ROOT="%s" python3 "%s/write_build_sha.py"' \
    "$1" "$(_dg_script_dir)"
}

# Loud abort: always stderr; optionally a JSONL line for a supervising job to
# surface. Keep $summary free of double-quotes so the JSONL stays valid.
_dg_alert() {
  local summary="$1"
  echo "deploy-guard: ABORT -- $summary" >&2
  local logdir="${ATHENAEUM_GUARD_LOG_DIR:-}"
  [ -z "$logdir" ] && return 0
  mkdir -p "$logdir" 2>/dev/null || return 0
  local ts d
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  d="$(date -u +%Y-%m-%d)"
  printf '{"timestamp":"%s","routine":"deploy-guard","repo":"Kromatic-Innovation/athenaeum","stage":"guard","outcome":"abort","escalation":true,"summary":"%s"}\n' \
    "$ts" "$summary" >> "$logdir/$d.jsonl" 2>/dev/null || true
}

# --- modes -----------------------------------------------------------------
_dg_check() {
  local dir ref refsha head marker
  dir="$(_dg_deploy_dir)"
  ref="$(_dg_ref)"
  if ! _dg_is_checkout "$dir"; then
    echo "pre-activation ${dir} (deploy checkout absent)"
    return 0
  fi
  if ! refsha="$(_dg_resolve_ref_sha "$dir" "$ref")" || [ -z "$refsha" ]; then
    echo "error could not resolve origin/${ref} in ${dir}"
    return 20
  fi
  head="$(_dg_head "$dir")"
  marker="$(_dg_build_sha "$dir")"
  # In-sync requires BOTH the running code (HEAD) and the built marker to be at
  # the ref. Keying only on the stamp let a rewound HEAD report a false in-sync
  # while a stale stamp still matched the ref (athenaeum#614).
  if [ -n "$head" ] && [ "$head" = "$refsha" ] && [ "$marker" = "$refsha" ]; then
    echo "in-sync ${refsha}"
    return 0
  fi
  echo "drift ref=${refsha} head=${head:-<none>} stamp=${marker:-<none>}"
  return 10
}

# Default mode: perform the sync. Returns 0 (proceed) or 1 (abort loud).
_dg_sync() {
  local dir ref refsha head_before marker reconcile install stamp head_after
  dir="$(_dg_deploy_dir)"
  ref="$(_dg_ref)"

  if ! _dg_is_checkout "$dir"; then
    echo "deploy-guard: pre-activation -- ${dir} absent; nothing to guard yet" >&2
    return 0
  fi

  # A main-pinned deploy worktree must never be hand-edited. Refuse to dispatch
  # on a dirty tree -- on EITHER the in-sync or the drift path -- rather than
  # risk the reconcile clobbering local edits or running against unknown state.
  # This is the guard that keeps the `reset --hard` below from ever discarding
  # local work: a clean deploy checkout owns none.
  if ! _dg_worktree_clean "$dir"; then
    _dg_alert "deploy worktree ${dir} is dirty -- refusing to touch it (someone edited the deploy checkout?). Recovery: inspect it (git -C ${dir} status), discard stray edits once you understand them, then re-run. The guard never force-resets a dirty tree."
    return 1
  fi

  if ! refsha="$(_dg_resolve_ref_sha "$dir" "$ref")" || [ -z "$refsha" ]; then
    echo "deploy-guard: WARN could not resolve origin/${ref} (offline?); leaving ${dir} as-is" >&2
    return 0
  fi

  head_before="$(_dg_head "$dir")"
  marker="$(_dg_build_sha "$dir")"

  # Decide against HEAD (the running code), NOT the stamp. In-sync only when the
  # checkout is AT the ref AND the build marker already records it.
  if [ -n "$head_before" ] && [ "$head_before" = "$refsha" ] && [ "$marker" = "$refsha" ]; then
    echo "deploy-guard: in-sync ${refsha} (${dir} @ origin/${ref})" >&2
    return 0
  fi

  # Drift. If HEAD is not at the ref -- in EITHER direction, including a REWIND
  # to an ancestor that --ff-only would treat as a successful no-op -- reconcile
  # the checkout to the ref. `reset --hard` moves HEAD forward OR backward; the
  # deploy checkout owns no local work and is clean (checked above).
  if [ "$head_before" != "$refsha" ]; then
    echo "deploy-guard: drift (HEAD=${head_before:-<none>} ref=${refsha}) -- reconciling ${dir} to origin/${ref}" >&2
    reconcile="${ATHENAEUM_GUARD_RECONCILE_CMD:-${ATHENAEUM_GUARD_FF_CMD:-$(_dg_default_reconcile_cmd "$dir" "$ref")}}"
    if ! ( cd "$dir" && eval "$reconcile" ) >/dev/null 2>&1; then
      _dg_alert "reconcile to origin/${ref} failed for ${dir} (cmd: ${reconcile}). Recovery: bring the deploy checkout to origin/${ref} by hand (git -C ${dir} fetch --force origin ${ref} && git -C ${dir} reset --hard origin/${ref}) once you understand why the reset failed."
      return 1
    fi
  else
    # HEAD is already at the ref but the build marker is stale/absent: the code
    # is correct, the venv/stamp may not be. Rebuild them; do not touch git.
    echo "deploy-guard: HEAD already at ${refsha} but unstamped/stale (stamp=${marker:-<none>}) -- refreshing venv + stamp" >&2
  fi

  # POST-CONDITION: re-read the ACTUAL HEAD and require it to be the ref now.
  # This turns a reconcile that did NOT move the checkout (the athenaeum#614
  # rewind no-op) from a silent false "synced" into a loud abort, and it is why
  # the success line below can only ever print an OBSERVED, verified HEAD.
  head_after="$(_dg_head "$dir")"
  if [ "$head_after" != "$refsha" ]; then
    _dg_alert "post-condition FAILED: ${dir} HEAD is ${head_after:-<unreadable>} but expected ${refsha} after reconcile to origin/${ref} -- refusing to report a sync that did not move the checkout. Recovery: git -C ${dir} fetch --force origin ${ref} && git -C ${dir} reset --hard origin/${ref}."
    return 1
  fi

  install="${ATHENAEUM_GUARD_INSTALL_CMD:-$(_dg_default_install_cmd)}"
  if ! ( cd "$dir" && eval "$install" ); then
    _dg_alert "venv refresh failed after reconcile to origin/${ref} (cmd: ${install}). Recovery: rebuild the venv by hand (cd ${dir} && ${install}) and confirm the deploy extras resolve."
    return 1
  fi

  stamp="${ATHENAEUM_GUARD_STAMP_CMD:-$(_dg_default_stamp_cmd "$dir")}"
  if ! ( cd "$dir" && eval "$stamp" ) >/dev/null 2>&1; then
    _dg_alert "build-sha stamp failed after refresh (cmd: ${stamp}). Recovery: re-stamp by hand (${stamp}) so hestia redeploy and the deploy-lag aggregator see the running commit."
    return 1
  fi

  # Report the OBSERVED post-sync HEAD, never the intended target.
  echo "deploy-guard: synced ${dir} to origin/${ref} (observed HEAD ${head_after}) and refreshed .venv" >&2
  return 0
}

# --- entrypoint ------------------------------------------------------------
_dg_main() {
  if [ "${1:-}" = "--check" ]; then
    _dg_check
    return $?
  fi
  _dg_sync
  return $?
}

# Run when executed (hestia redeploy / operator); stay quiet when sourced so the
# helper functions above can be unit-tested in isolation.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  _dg_main "$@"
  exit $?
fi
