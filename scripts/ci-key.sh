#!/usr/bin/env bash
# ci-key.sh — compute the content-addressed CI key for a commit.
#
# Ported from hestia's scripts/ci-key.sh (hestia#1940) for athenaeum#1160,
# which adopts the attestation/CI-key short-circuit hestia piloted (cwc#2755)
# so `push: [develop]` can be restored on this repo's `ci.yml` without
# reintroducing the duplicate full-matrix run athenaeum#1031 removed. Prints
# `v1:<64 hex>` for the tree at <commit-ish>, hashing every path that can
# change a test outcome and omitting the paths listed in that commit's
# `.ci-irrelevant`.
#
# WHY a tree key rather than a commit sha: the tested tree and the promoted
# tree are the same tree whatever commit carries it, so a key over the tree
# makes prior green evidence reachable regardless of which commit stamped it.
# It also reaches a case the merge-commit detector (`ci-detect-merged-pr.sh`)
# structurally cannot: a squash or rebase merge carries no
# `Merge pull request #` subject, so that detector runs full CI even when the
# tree is byte-identical to one already tested.
#
# TWO RULES THIS SCRIPT EXISTS TO KEEP. A false miss costs one CI run; a false
# hit ships untested code. The asymmetry decides every judgement call here.
#
#   1. `.ci-irrelevant` is ITSELF inside the key. It is never filtered out of
#      its own listing, so widening what counts as irrelevant re-keys the tree
#      and cannot retroactively reuse an attestation earned under other rules.
#
#   2. Everything that changes a test outcome is inside the key — source,
#      tests, lockfiles, workflow definitions, and toolchain pins.
#
# The exclusion file is read FROM THE COMMIT BEING KEYED (`git show
# <commit>:.ci-irrelevant`), never from the working tree. Reading it from the
# checkout would make the key a function of two commits — the one being keyed
# and the one checked out — so verification (which recomputes the key on a
# different, older commit) could disagree with the run that published it.
#
# PATTERN SYNTAX — shell globs, matched against the full repo-relative path,
# where `*` also matches `/`. Blank lines and `#` comments are ignored.
#
# Usage:
#   ci-key.sh <commit-ish>          # e.g. HEAD, a sha, a ref
#
# Env knobs (tests use these; CI uses the defaults):
#   CI_KEY_REPO_ROOT   run against this repo instead of the cwd

set -uo pipefail

die() { printf '%s\n' "ci-key.sh: $*" >&2; exit 2; }

[ "$#" -eq 1 ] || die "usage: ci-key.sh <commit-ish>"

case "$1" in
  -h|--help) sed -n '1,60p' "$0"; exit 0 ;;
esac

if [ -n "${CI_KEY_REPO_ROOT:-}" ]; then
  cd "$CI_KEY_REPO_ROOT" || die "cannot enter CI_KEY_REPO_ROOT='$CI_KEY_REPO_ROOT'"
fi

COMMIT=$(git rev-parse --verify --quiet "$1^{commit}") \
  || die "not a commit: $1"

# sha256sum (GNU/coreutils) on CI and in containers; shasum on macOS.
sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 | cut -d' ' -f1
  else
    die "neither sha256sum nor shasum is available"
  fi
}

# Patterns come from the commit under test, not the checkout (see header).
PATTERNS=()
while IFS= read -r pat; do
  pat="${pat%%#*}"                       # strip trailing comments
  pat="${pat#"${pat%%[![:space:]]*}"}"   # ltrim
  pat="${pat%"${pat##*[![:space:]]}"}"   # rtrim
  [ -n "$pat" ] || continue
  PATTERNS+=("$pat")
done < <(git show "$COMMIT:.ci-irrelevant" 2>/dev/null || true)

# `-z` keeps paths unquoted and NUL-terminated, so a path containing a space,
# a quote or a newline is handled rather than silently mangled. `--format`
# pins the field order regardless of git's default output tweaks.
#
# The digest input is NUL-separated for the same reason, and prefixed with a
# version banner so a future key format cannot be confused with this one.
{
  printf 'ci-key-v1\0'
  while IFS= read -r -d '' record; do
    # `<mode> <type> <objectname> <path>` — path is everything after field 3,
    # and may itself contain spaces.
    path="${record#* }"; path="${path#* }"; path="${path#* }"
    excluded=""
    for pat in ${PATTERNS+"${PATTERNS[@]}"}; do
      # Unquoted $pat on purpose: this is glob matching, not equality.
      # shellcheck disable=SC2254
      case "$path" in
        $pat) excluded=1; break ;;
      esac
    done
    [ -n "$excluded" ] && continue
    printf '%s\0' "$record"
  done < <(git ls-tree -r -z \
             --format='%(objectmode) %(objecttype) %(objectname) %(path)' \
             "$COMMIT") \
    | LC_ALL=C sort -z
} | sha256 | sed 's/^/v1:/'
