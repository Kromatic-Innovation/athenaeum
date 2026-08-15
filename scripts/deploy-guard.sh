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
#                             (default: <py> -m venv .venv && .venv/bin/python
#                              -m pip install -q -e ".[<extras>]", where <py>
#                              is an interpreter that actually satisfies the
#                              deploy tree's requires-python — see
#                              _dg_resolve_python; routed through the venv's
#                              own python rather than bare .venv/bin/pip for
#                              the same single-interpreter reason as
#                              ATHENAEUM_GUARD_METADATA_REFRESH_CMD, athenaeum#894)
#   ATHENAEUM_GUARD_PYTHON    force the venv-build interpreter (issue athenaeum#832).
#                             Skips probing; still VERIFIED against
#                             requires-python, and a chosen interpreter that
#                             does not satisfy it aborts loudly rather than
#                             silently falling back.
#   ATHENAEUM_GUARD_STAMP_CMD    stamp command run after a successful refresh
#                             (default: python3 <scriptdir>/write_build_sha.py against <dir>)
#   ATHENAEUM_GUARD_VERSION_CHECK_CMD  metadata-drift check run against the deploy
#                             venv (issue athenaeum#685; exit 0 in-sync / 10 drift / 20
#                             undetermined). Default:
#                             <dir>/.venv/bin/python -m athenaeum.deploy_check --check <dir>
#   ATHENAEUM_GUARD_METADATA_REFRESH_CMD  metadata-only editable reinstall run on
#                             drift (default: <dir>/.venv/bin/python -m pip
#                             install -q -e . --no-deps — routed through the
#                             venv's OWN python, never bare .venv/bin/pip, so
#                             it can never target a different interpreter tree
#                             than the version-check reads; see athenaeum#894)
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

# --- interpreter selection (issue athenaeum#832) -----------------------------
# The venv build used to hardcode bare `python3`. That is whatever the machine's
# $PATH happens to resolve — on the deploy host a pyenv shim to 3.11 — so once
# pyproject.toml moved to `requires-python = ">=3.13"` (athenaeum#815) every
# drift-triggered rebuild failed deterministically with
#   ERROR: Package 'athenaeum' requires a different Python: 3.11.15 not in '>=3.13'
# leaving the checkout reconciled but the stamp stale. The guard must not assume
# `python3` is fixed elsewhere on the box — it resolves an interpreter that
# actually satisfies the DEPLOY TREE'S OWN declared constraint instead.

# The `>=MAJ.MIN` floor declared by <dir>/pyproject.toml's requires-python.
# Echoes "MAJ.MIN"; non-zero when there is no readable/parseable constraint (in
# which case the caller keeps the historical bare-`python3` behavior — a tree
# that declares no floor has none to enforce).
_dg_requires_python() {
  local pyproject="$1/pyproject.toml" spec floor
  [ -f "$pyproject" ] || return 1
  # The raw requires-python value, e.g. ">=3.13" or ">=3.13,<4.0".
  spec="$(sed -n 's/^[[:space:]]*requires-python[[:space:]]*=[[:space:]]*["'"'"']\([^"'"'"']*\)["'"'"'].*/\1/p' \
    "$pyproject" | head -1)"
  [ -n "$spec" ] || return 1
  # The `>=` clause, normalized to MAJ.MIN (a bare `>=3` floor means 3.0).
  floor="$(printf '%s' "$spec" | tr ',' '\n' \
    | sed -n 's/^[[:space:]]*>=[[:space:]]*\([0-9][0-9]*\)\.\([0-9][0-9]*\).*/\1.\2/p' | head -1)"
  if [ -z "$floor" ]; then
    floor="$(printf '%s' "$spec" | tr ',' '\n' \
      | sed -n 's/^[[:space:]]*>=[[:space:]]*\([0-9][0-9]*\)[[:space:]]*$/\1.0/p' | head -1)"
  fi
  [ -n "$floor" ] || return 1
  printf '%s' "$floor"
}

# A candidate interpreter's own MAJ.MIN, asked of the interpreter itself rather
# than inferred from its name — a `python3.13` on $PATH may be a shim for
# anything, which is the whole failure mode this guards against.
_dg_python_version() {
  "$1" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null
}

# True when interpreter $1 is at or above the MAJ.MIN floor $2.
_dg_python_satisfies() {
  local got rmaj rmin gmaj gmin
  got="$(_dg_python_version "$1")" || return 1
  case "$got" in [0-9]*.[0-9]*) ;; *) return 1 ;; esac
  rmaj="${2%%.*}"; rmin="${2#*.}"
  gmaj="${got%%.*}"; gmin="${got#*.}"
  [ "$gmaj" -gt "$rmaj" ] && return 0
  [ "$gmaj" -eq "$rmaj" ] && [ "$gmin" -ge "$rmin" ]
}

# Interpreters to probe, in preference order: bare `python3` first (so a healthy
# machine keeps today's exact behavior and cost), then explicitly-versioned
# names from the floor UPWARD (nearest-the-floor wins — the most conservative
# satisfying choice, never a bleeding-edge prerelease that merely sorts highest),
# then bare `python` as a last resort.
_dg_python_candidates() {
  local rmaj="${1%%.*}" rmin="${1#*.}" n
  printf 'python3\n'
  n="$rmin"
  while [ "$n" -le $((rmin + 20)) ]; do
    printf 'python%s.%s\n' "$rmaj" "$n"
    n=$((n + 1))
  done
  printf 'python\n'
}

# Echo an absolute path to an interpreter satisfying <dir>'s requires-python.
# Non-zero (and silent) when none is available, so the caller can abort LOUDLY
# with a recovery hint instead of handing pip an interpreter it will reject.
_dg_resolve_python() {
  local dir="${1:-.}" floor cand path
  floor="$(_dg_requires_python "$dir")" || floor=""

  # An operator's explicit choice is the ONLY candidate when set: silently
  # probing past it would hide a misconfiguration rather than report it.
  if [ -n "${ATHENAEUM_GUARD_PYTHON:-}" ]; then
    path="$(command -v "$ATHENAEUM_GUARD_PYTHON" 2>/dev/null)" || return 1
    [ -n "$path" ] || return 1
    if [ -n "$floor" ] && ! _dg_python_satisfies "$path" "$floor"; then
      return 1
    fi
    printf '%s' "$path"
    return 0
  fi

  # No declared floor to enforce -> historical behavior, unchanged.
  if [ -z "$floor" ]; then
    path="$(command -v python3 2>/dev/null)" || return 1
    [ -n "$path" ] || return 1
    printf '%s' "$path"
    return 0
  fi

  while IFS= read -r cand; do
    path="$(command -v "$cand" 2>/dev/null)" || continue
    [ -n "$path" ] || continue
    if _dg_python_satisfies "$path" "$floor"; then
      printf '%s' "$path"
      return 0
    fi
  done <<EOF
$(_dg_python_candidates "$floor")
EOF
  return 1
}

# The default venv refresh (the "configured for Python" step): (re)build the
# deploy checkout's own .venv exactly as deploy-sync.sh does. `pip install -e .`
# is idempotent, and the extras pull the MCP server + vector-search deps.
#
# Two differences from the pre-athenaeum#832 command: the interpreter is a
# RESOLVED absolute path known to satisfy requires-python (never bare `python3`),
# and an EXISTING .venv built by an interpreter that no longer satisfies the
# constraint is rebuilt with `--clear` — otherwise `venv` reuses the stale
# tree and pip fails exactly as before. A satisfying .venv is reused as always,
# so the common path costs nothing extra. Non-zero when no interpreter
# satisfies the constraint; the caller aborts loudly.
_dg_default_install_cmd() {
  local dir="${1:-.}" py floor clear=""
  py="$(_dg_resolve_python "$dir")" || return 1
  floor="$(_dg_requires_python "$dir")" || floor=""
  if [ -n "$floor" ] && [ -x "$dir/.venv/bin/python" ] \
    && ! _dg_python_satisfies "$dir/.venv/bin/python" "$floor"; then
    clear=" --clear"
  fi
  # `.venv/bin/python -m pip`, never bare `.venv/bin/pip` (issue athenaeum#894
  # AC2b): a freshly built venv's own pip normally matches its own python, but
  # routing through the interpreter here closes the same class of split this
  # guard's OTHER pip invocation (_dg_default_metadata_refresh_cmd) had to
  # close, so no path is left able to reintroduce it.
  printf '"%s" -m venv%s .venv && .venv/bin/python -m pip install -q -e ".[%s]"' \
    "$py" "$clear" "$(_dg_extras)"
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

# --- metadata-drift reconcile (issue athenaeum#685) ---------------------------------
# An editable install picks up code on a fast-forward but NOT metadata: the
# .dist-info version is frozen at install time, so importlib.metadata.version()
# (== athenaeum.__version__) can silently lag pyproject.toml across a version
# bump. A git fast-forward that DID reinstall the venv refreshes it; but an
# in-sync HEAD with stale metadata never does. These steps close that gap.

# The read-only version-drift check, run in the deploy venv so it reads that
# venv's installed metadata. Exit: 0 in-sync, 10 drift, 20 undetermined.
_dg_default_version_check_cmd() {
  printf '"%s/.venv/bin/python" -m athenaeum.deploy_check --check "%s"' "$1" "$1"
}

# The metadata-only editable reinstall: refreshes .dist-info WITHOUT touching
# the dependency tree (--no-deps), so a version bump is picked up cheaply.
#
# Routed through `.venv/bin/python -m pip`, NOT bare `.venv/bin/pip` (issue
# athenaeum#894 AC2). A venv can end up with `bin/python` and `bin/pip` bound
# to DIFFERENT interpreters (a stray second Python layered onto the venv after
# it was created); `_dg_default_version_check_cmd` above always reads through
# `.venv/bin/python`, so a refresh that writes through a differently-resolved
# `bin/pip` can land in a site-packages tree the check never sees -- metadata
# "still drifted after refresh", forever, no matter how many times the refresh
# reruns. `python -m pip` cannot diverge from `python`: it is always the same
# interpreter's own pip, so check and refresh are pinned together regardless
# of what `bin/pip` happens to resolve to.
_dg_default_metadata_refresh_cmd() {
  printf '"%s/.venv/bin/python" -m pip install -q -e . --no-deps' "$1"
}

# --- multi-tree detection (issue athenaeum#894) -----------------------------
# Even with the refresh above pinned to `python -m pip`, a venv can ALREADY
# carry a stray extra Python tree (e.g. an operator's manual `python3.14 -m
# venv`/pip run against a checkout whose `pyvenv.cfg` declares 3.13) from
# before this fix landed, or from tooling outside the guard's control. That
# condition explains "still drifted after refresh" far better than a generic
# drift message, so name it explicitly rather than leaving the operator to
# rediscover the incident by hand.
#
# Two independent signals, either sufficient on its own:
#   1. more than one lib/python*/ tree under the venv
#   2. bin/python and bin/pip report DIFFERENT interpreter versions
# Echoes a human-readable reason and returns 0 when found. Silent non-zero
# when the venv looks single-tree, or does not exist yet (nothing to detect
# before a venv has been built) -- the caller falls back to the generic
# message in that case.
_dg_venv_multi_tree_reason() {
  local dir="${1:-.}" venv trees pyver pipver
  venv="$dir/.venv"
  [ -d "$venv" ] || return 1

  trees="$(cd "$venv" && ls -d lib/python*/ 2>/dev/null)"
  if [ "$(printf '%s\n' "$trees" | grep -c .)" -gt 1 ]; then
    printf 'multiple Python trees under %s/lib (%s)' \
      "$venv" "$(printf '%s' "$trees" | tr '\n' ' ' | sed 's/ *$//')"
    return 0
  fi

  if [ -x "$venv/bin/python" ] && [ -x "$venv/bin/pip" ]; then
    pyver="$(_dg_python_version "$venv/bin/python")"
    pipver="$("$venv/bin/pip" --version 2>/dev/null \
      | sed -n 's/.*(python \([0-9][0-9]*\.[0-9][0-9]*\)).*/\1/p')"
    if [ -n "$pyver" ] && [ -n "$pipver" ] && [ "$pyver" != "$pipver" ]; then
      printf 'bin/python (%s) and bin/pip (%s) resolve to different interpreters under %s' \
        "$pyver" "$pipver" "$venv"
      return 0
    fi
  fi

  return 1
}

# Reconcile the editable install's metadata to the tree's declared version.
# On drift (check exits 10) refresh the metadata and re-verify; a refresh that
# fails or does not clear the drift aborts LOUDLY. An UNDETERMINED check (20) or
# a check that cannot run (e.g. the deploy venv predates this module, before the
# one-off `pip install -e .` in AC5) is WARNED loudly but does NOT block the
# redeploy — the standalone `python -m athenaeum.deploy_check` surface still
# reports it. Returns 0 (ok/warned) or 1 (loud abort).
_dg_reconcile_metadata() {
  local dir="$1" check refresh rc reason
  check="${ATHENAEUM_GUARD_VERSION_CHECK_CMD:-$(_dg_default_version_check_cmd "$dir")}"
  ( cd "$dir" && eval "$check" ) >/dev/null 2>&1
  rc=$?
  if [ "$rc" -eq 0 ]; then
    return 0  # installed metadata already matches the tree
  fi
  if [ "$rc" -ne 10 ]; then
    # Undetermined (20) or unrunnable: loud, but do not block the redeploy.
    echo "deploy-guard: WARN version-check could not confirm metadata (rc=${rc}) in ${dir} — run '(cd ${dir} && .venv/bin/python -m pip install -e .)' to seed the check; leaving metadata as-is" >&2
    return 0
  fi
  echo "deploy-guard: metadata drift (installed != pyproject) — refreshing editable install metadata in ${dir}" >&2
  refresh="${ATHENAEUM_GUARD_METADATA_REFRESH_CMD:-$(_dg_default_metadata_refresh_cmd "$dir")}"
  if ! ( cd "$dir" && eval "$refresh" ) >/dev/null 2>&1; then
    _dg_alert "metadata refresh failed in ${dir} (cmd: ${refresh}). Recovery: refresh by hand (cd ${dir} && ${refresh}) and re-run the version check ((cd ${dir} && ${check})) to confirm it reports in-sync."
    return 1
  fi
  ( cd "$dir" && eval "$check" ) >/dev/null 2>&1
  rc=$?
  if [ "$rc" -ne 0 ]; then
    # AC3: name the multi-tree condition explicitly when it explains the
    # failure, instead of a generic drift message + a recovery command that
    # provably cannot fix it (athenaeum#894 — the incident this guards
    # against: "recovery succeeds, the very next check fails identically").
    if reason="$(_dg_venv_multi_tree_reason "$dir")"; then
      _dg_alert "metadata still drifted after refresh in ${dir} (version-check rc=${rc}) -- CAUSE: ${reason}. The version-check and the refresh are reading/writing DIFFERENT interpreter trees, so re-running the refresh cannot clear this no matter how many times you try. Recovery: rebuild ${dir}/.venv clean on a single interpreter (athenaeum#924) -- a metadata-only reinstall will not fix a multi-tree venv."
    else
      _dg_alert "metadata still drifted after refresh in ${dir} (version-check rc=${rc}). Recovery: (cd ${dir} && .venv/bin/python -m pip install -e . --no-deps) and re-run the version check ((cd ${dir} && ${check})); if it drifts again, suspect a multi-tree venv (more than one lib/python*/ under .venv, or bin/python and bin/pip resolving to different interpreters)."
    fi
    return 1
  fi
  echo "deploy-guard: metadata refreshed — installed version now matches pyproject in ${dir}" >&2
  return 0
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
  local dir ref refsha head_before marker reconcile install stamp head_after floor
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
    # Git is in-sync, but the editable install's metadata can still be stale
    # (issue athenaeum#685) — reconcile it here since no venv refresh runs on this path.
    _dg_reconcile_metadata "$dir" || return 1
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

  # Resolve the venv-build interpreter against the DEPLOY TREE's requires-python
  # (issue athenaeum#832). A machine with no satisfying interpreter aborts here,
  # LOUDLY and with the constraint named, instead of handing pip a too-old
  # `python3` and surfacing pip's opaque 'requires a different Python' error
  # after the checkout has already been reconciled.
  install="${ATHENAEUM_GUARD_INSTALL_CMD:-}"
  if [ -z "$install" ]; then
    if ! install="$(_dg_default_install_cmd "$dir")"; then
      floor="$(_dg_requires_python "$dir")" || floor=""
      if [ -n "$floor" ]; then
        _dg_alert "no Python interpreter satisfying requires-python >=${floor} found on PATH for ${dir} (bare python3 is $(_dg_python_version python3 2>/dev/null || echo absent)). Recovery: install a Python ${floor}+ interpreter, or point the guard at one you already have via ATHENAEUM_GUARD_PYTHON=/path/to/python${floor} (see ATHENAEUM_GUARD_INSTALL_CMD to override the whole command)."
      else
        _dg_alert "no usable python3 on PATH to build the venv in ${dir} (and ${dir}/pyproject.toml declares no requires-python floor to probe against). Recovery: install python3, or set ATHENAEUM_GUARD_PYTHON=/path/to/python."
      fi
      return 1
    fi
  fi
  if ! ( cd "$dir" && eval "$install" ); then
    _dg_alert "venv refresh failed after reconcile to origin/${ref} (cmd: ${install}). Recovery: rebuild the venv by hand (cd ${dir} && ${install}) and confirm the deploy extras resolve."
    return 1
  fi

  stamp="${ATHENAEUM_GUARD_STAMP_CMD:-$(_dg_default_stamp_cmd "$dir")}"
  if ! ( cd "$dir" && eval "$stamp" ) >/dev/null 2>&1; then
    _dg_alert "build-sha stamp failed after refresh (cmd: ${stamp}). Recovery: re-stamp by hand (${stamp}) so hestia redeploy and the deploy-lag aggregator see the running commit."
    return 1
  fi

  # The venv refresh above (`pip install -e .`) also refreshes .dist-info, so
  # metadata is normally fresh here — but confirm (and refresh if a partial
  # install left it stale) so a synced report can never hide a lying version
  # string (issue athenaeum#685).
  _dg_reconcile_metadata "$dir" || return 1

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
