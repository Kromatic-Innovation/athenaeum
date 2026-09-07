#!/usr/bin/env bash
# Deploy-sync + SHA stamp for athenaeum (issue athenaeum#413).
#
# WHAT: since cwc#1529 (2026-07-25), athenaeum DOES keep a separate
# main-pinned deploy worktree at ~/local-deploys/athenaeum (like hestia/
# voltaire's `<repo>-deploy` checkouts, guarded there by deploy-guard.sh; see
# the hestia#691 audit) — the dev tree at ~/Code/athenaeum is no longer what
# the MCP server or the nightly librarian execute. This script is the
# lighter-weight equivalent of deploy-guard.sh for that shape: fast-forward
# the deploy checkout to its deploy ref, reinstall the editable package (so
# new/changed dependencies and entry points actually take effect — a gap
# discovered 2026-07-25 when two promotions in a row required a manual
# `pip install -e` after this script because it only synced files), then
# stamp the running commit into `dist/.build-sha` via
# scripts/write_build_sha.py. The stamp lets the cross-repo deploy-lag
# aggregator (code-workspace-config#1428) answer "what commit is athenaeum
# actually running" by reading that one file.
#
# `dist/` is gitignored — the stamp is a local build artifact, never committed.
#
# USAGE:
#   scripts/deploy-sync.sh                      fast-forward, reinstall, rewrite the stamp
#   scripts/deploy-sync.sh --check              fetch + compare HEAD to origin/<ref> AND
#                                                the stamp; mutate nothing
#   scripts/deploy-sync.sh --check --no-fetch   same comparison, but skip the network
#                                                fetch (offline / test use only — see
#                                                "--check semantics" below)
#
# NOTE: this script only syncs the LOCAL deploy checkout. It does not
# promote develop -> main on GitHub — run `scripts/promote-and-deploy.sh`
# for the single command that does both, or trigger `promote-main.yml`
# first (GitHub Actions UI or `gh workflow run promote-main.yml -f reason=...`).
#
# --check semantics (athenaeum#1445):
#
# `--check` answers TWO orthogonal questions, both printed on one line, never
# collapsed into a single ambiguous word:
#   sync=...   is the CHECKOUT current with the deploy ref (origin/<ref>)?
#              in-sync | behind <N> | ahead <N> | diverged | unknown
#   stamp=...  is the INSTALL (dist/.build-sha) current with the checkout?
#              current | stale | missing
#
# `sync` is computed by fetching origin/<ref> and comparing HEAD against it
# with `git merge-base --is-ancestor` in both directions:
#   HEAD == origin/<ref>                        -> in-sync
#   HEAD is an ancestor of origin/<ref>          -> behind <N> (N = HEAD..origin/<ref>)
#   origin/<ref> is an ancestor of HEAD          -> ahead <N>  (N = origin/<ref>..HEAD)
#   neither (no common ancestor, e.g. a history
#     rewrite — the athenaeum#1445 incident)     -> diverged
# This was the AC1/AC2 gap this issue closed: before athenaeum#1445, `--check`
# only ever compared HEAD to the STAMP, so a deploy frozen at any commit
# reported "in-sync" indefinitely (the stamp is always right about the thing
# it measures, even when that thing is stale). `sync` is the fix; `stamp` is
# retained unchanged as the second, orthogonal condition — a checkout that IS
# current with origin/<ref> can still have a stale install (skipped reinstall)
# and that must keep being visible too.
#
# `--check` now has a network dependency (the fetch above) it did not have
# before. `--no-fetch` is the explicit opt-out for offline callers (this
# script's own test suite): it skips the fetch and reads whatever
# `origin/<ref>` remote-tracking ref already exists locally, which may be
# stale or absent. Because that is a WEAKER guarantee than a live fetch, every
# `--no-fetch` sync state is prefixed `no-fetch:` (e.g. `no-fetch:in-sync`) so
# a `sync=in-sync` reading is never emitted on the strength of a cached ref
# alone without saying so — a `no-fetch:in-sync` is a "was in sync as of the
# last fetch", not a live confirmation, and no consumer should treat the two
# as equivalent. When no local `origin/<ref>` exists at all (never fetched),
# `sync=no-fetch:unknown` is reported instead — never a green word.
#
# Exit-code contract for `--check` (distinguished in the exit code, not only
# the text, so an automated caller can branch without parsing stdout):
#   0   sync=in-sync   AND stamp=current    (fully healthy)
#   10  sync=in-sync   but stamp is stale/missing (install-only drift)
#   11  sync=behind <N>                     (checkout is behind the deploy ref)
#   12  sync=diverged                       (no common ancestor — needs reset/re-clone)
#   13  sync=ahead <N>                      (checkout has local commits the ref lacks)
#   14  sync=no-fetch:unknown (--no-fetch, no local origin/<ref> to compare against)
#   20  $dir is not a git checkout
#   30  could not fetch origin/<ref> (remote unreachable) — never emits a green
#       word; only reachable when `--no-fetch` was NOT given
#
# TEST/CI HOOKS (offline determinism — never set in production):
#   ATHENAEUM_DEPLOY_DIR    repo root to sync/stamp (default: this script's `..`)
#   ATHENAEUM_DEPLOY_REF    deploy ref to track      (default: main)
#   ATHENAEUM_SYNC_FETCH=0  mutating path only: skip `git fetch` + fast-forward;
#                           stamp the checkout as-is. (For `--check`'s own fetch,
#                           use the `--no-fetch` CLI flag documented above instead.)
#   ATHENAEUM_SYNC_FF_CMD   fast-forward command (default: `git merge --ff-only origin/<ref>`)
#   ATHENAEUM_SYNC_REINSTALL=0  skip the `pip install -e` reinstall step
#   ATHENAEUM_DEPLOY_EXTRAS pip extras to install (default: mcp,vector — what
#                           the MCP server + librarian's vector search need)
#   ATHENAEUM_PYTHON        python interpreter for the stamp script (default: python3)
set -euo pipefail

_ds_script_dir() { ( cd "$(dirname "${BASH_SOURCE[0]}")" && pwd ); }
_ds_dir() {
  if [ -n "${ATHENAEUM_DEPLOY_DIR:-}" ]; then
    printf '%s' "$ATHENAEUM_DEPLOY_DIR"
  else
    ( cd "$(_ds_script_dir)/.." && pwd )
  fi
}
_ds_ref() { printf '%s' "${ATHENAEUM_DEPLOY_REF:-main}"; }
_ds_python() { printf '%s' "${ATHENAEUM_PYTHON:-python3}"; }

# The stamp comparison ("is the install current with the checkout") — unchanged
# from before athenaeum#1445, just factored out so --check can report it
# alongside the new checkout-vs-remote comparison instead of in place of it.
# Echoes "current", "stale", or "missing".
_ds_stamp_state() {
  local dir="$1" head="$2" stamped=""
  if [ -f "$dir/dist/.build-sha" ]; then
    stamped="$(tr -d '[:space:]' < "$dir/dist/.build-sha")"
  fi
  if [ -z "$stamped" ]; then
    printf 'missing'
  elif [ "$stamped" = "$head" ]; then
    printf 'current'
  else
    printf 'stale'
  fi
}

dir="$(_ds_dir)"
ref="$(_ds_ref)"

# --check: report drift without mutating anything.
if [ "${1:-}" = "--check" ]; then
  no_fetch=0
  for arg in "$@"; do
    [ "$arg" = "--no-fetch" ] && no_fetch=1
  done

  if [ ! -e "$dir/.git" ]; then
    echo "error: $dir is not a git checkout"
    exit 20
  fi

  head="$(git -C "$dir" rev-parse HEAD 2>/dev/null || true)"
  stamp_state="$(_ds_stamp_state "$dir" "$head")"

  if [ "$no_fetch" != 1 ]; then
    if ! git -C "$dir" fetch --quiet --no-tags origin "$ref" 2>/dev/null; then
      echo "error: could not fetch origin/${ref} in ${dir} (remote unreachable, or ref does not exist) — stamp=${stamp_state} head=${head:-<unknown>}"
      exit 30
    fi
  fi

  remote="$(git -C "$dir" rev-parse --verify --quiet "origin/$ref" 2>/dev/null || true)"

  prefix=""
  [ "$no_fetch" = 1 ] && prefix="no-fetch:"

  if [ -z "$remote" ]; then
    # --no-fetch and origin/<ref> has never been fetched locally: there is
    # nothing to compare against yet. Never a green word for this case. Only
    # reachable when no_fetch=1 (without --no-fetch, a fetch failure already
    # exited 30 above, and a successful fetch of $ref always leaves
    # origin/$ref resolvable) — so this is always rendered "no-fetch:unknown",
    # same as every other branch here composes "${prefix}...".
    sync_state="${prefix}unknown"
    exit_code=14
  elif [ "$head" = "$remote" ]; then
    sync_state="${prefix}in-sync"
    if [ "$stamp_state" = "current" ]; then
      exit_code=0
    else
      exit_code=10
    fi
  elif git -C "$dir" merge-base --is-ancestor "$head" "$remote" 2>/dev/null; then
    n="$(git -C "$dir" rev-list --count "${head}..${remote}")"
    sync_state="${prefix}behind ${n}"
    exit_code=11
  elif git -C "$dir" merge-base --is-ancestor "$remote" "$head" 2>/dev/null; then
    n="$(git -C "$dir" rev-list --count "${remote}..${head}")"
    sync_state="${prefix}ahead ${n}"
    exit_code=13
  else
    sync_state="${prefix}diverged"
    exit_code=12
  fi

  echo "sync=${sync_state} stamp=${stamp_state} head=${head:-<unknown>} remote=${remote:-<none>}"
  exit "$exit_code"
fi

if [ ! -e "$dir/.git" ]; then
  echo "athenaeum deploy-sync: $dir is not a git checkout" >&2
  exit 1
fi

if [ "${ATHENAEUM_SYNC_FETCH:-1}" != "0" ]; then
  git -C "$dir" fetch --quiet --no-tags origin "$ref"

  # Refuse a no-common-ancestor fast-forward instead of letting `git merge
  # --ff-only` surface its own generic "Not possible to fast-forward, aborting"
  # (athenaeum#1445 AC4 — the second failure of the same incident: after a
  # history rewrite the deploy's `main` and `origin/<ref>` shared no ancestry,
  # and `--ff-only` cannot recover from that on its own). A checkout that is
  # merely BEHIND or AHEAD still has common ancestry, so `--ff-only` handles
  # both correctly (the ahead case is a safe no-op: "Already up to date.");
  # only the true diverged case needs interception here.
  head="$(git -C "$dir" rev-parse HEAD 2>/dev/null || true)"
  remote="$(git -C "$dir" rev-parse --verify --quiet "origin/$ref" 2>/dev/null || true)"
  if [ -n "$head" ] && [ -n "$remote" ] && [ "$head" != "$remote" ] \
    && ! git -C "$dir" merge-base --is-ancestor "$head" "$remote" 2>/dev/null \
    && ! git -C "$dir" merge-base --is-ancestor "$remote" "$head" 2>/dev/null; then
    echo "athenaeum deploy-sync: ${dir} has DIVERGED from origin/${ref} (no common ancestor) — refusing to fast-forward." >&2
    echo "  HEAD=${head} origin/${ref}=${remote}" >&2
    echo "  remedy: git -C \"${dir}\" reset --hard \"origin/${ref}\"   (or re-clone the deploy checkout)" >&2
    exit 21
  fi

  ff_cmd="${ATHENAEUM_SYNC_FF_CMD:-git merge --ff-only "origin/$ref"}"
  ( cd "$dir" && eval "$ff_cmd" )
fi

# Reinstall the editable package so new/changed dependencies and entry points
# actually take effect — a `git merge --ff-only` above only updates files on
# disk, it does not touch whatever the venv's site-packages already resolved
# at the last install. Skippable (ATHENAEUM_SYNC_REINSTALL=0) for callers that
# just want the fetch+stamp (e.g. --check-adjacent tooling, tests).
if [ "${ATHENAEUM_SYNC_REINSTALL:-1}" != "0" ]; then
  venv_pip="$dir/.venv/bin/pip"
  if [ -x "$venv_pip" ]; then
    extras="${ATHENAEUM_DEPLOY_EXTRAS:-mcp,vector}"
    ( cd "$dir" && "$venv_pip" install -q -e ".[${extras}]" )
  else
    echo "deploy-sync: no venv pip at $venv_pip — skipping reinstall" >&2
  fi
fi

# Stamp the deploy checkout ($dir) using the stamp script shipped alongside
# this one — in production they live in the same scripts/ dir; keeping them
# decoupled lets the sync stamp a checkout other than the one it ships from.
ATHENAEUM_BUILD_SHA_ROOT="$dir" "$(_ds_python)" "$(_ds_script_dir)/write_build_sha.py"
