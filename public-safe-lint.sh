#!/usr/bin/env bash
# public-safe-lint.sh — a portable, org-agnostic file-shape scanner.
#
# The scanner invoked by the companion public-safe-lint.template.yml
# workflow. This script carries ZERO organization-specific strings -- only
# generic SHAPES that any open-source repository would want to catch in its
# own CI, including on pull requests from outside contributors where no
# repository secret is ever injected:
#   - local absolute filesystem paths (a personal username or home directory
#     leaking into a public file)
#   - home-directory-relative paths
#   - bare numeric issue references with no repository qualifier
#   - plus-addressed email aliases (personal test-account leakage)
#   - personal-attribution phrases
#
# This script must never be pointed at, or combined with, any private
# pattern inventory -- doing so would defeat the point of keeping it
# adoptable on fork pull requests, which receive no injected secrets or
# private config of any kind.
#
# bare-issue-ref and already-qualified links: a `#N` that is the
# link text of an already-fully-qualified GitHub issue/PR URL --
# `[#N](https://github.com/OWNER/REPO/issues/N)` or `.../pull/N` -- is not a
# leak (GitHub already resolved it correctly) and is excluded by construction
# before this rule's hits are reported. This is a fixed logic correction, not
# a suppression a repo opts into.
#
# Suppression mechanism: an adopting repo may commit a
# `.public-safe-lintignore` file at the root of the tree being scanned to
# exempt specific paths from specific named rules -- for example a repo's own
# CHANGELOG.md citing its own already-public issue history, or a leak-
# detector's test fixtures that deliberately contain the shapes this script
# looks for. Format: one `<rule-name> <path-glob>` pair per line (whitespace-
# separated), `#`-prefixed lines and blank lines ignored. Example:
#
#   bare-issue-ref CHANGELOG.md
#   bare-issue-ref README.md
#   bare-issue-ref tests/fixtures/**
#
# This is exclusion by path + rule name ONLY -- never by literal content, org
# name, or company term, so it cannot be used to smuggle a term-allowlist
# through the back door. The file lives in the adopting repo's own committed
# tree (never in public-safe-lint.sh or public-safe-lint.template.yml, and
# never injected as a secret), so it is present identically on a fork PR.
# Deciding what to exempt is each adopting repo's own policy call -- this
# script ships the mechanism, not a default exemption list.
#
# Output discipline: this script reports file:line and a rule name only --
# it never prints the matched substring itself. A linter that prints the
# matched text into a public CI log republishes exactly what a leak-detector
# exists to prevent, so this discipline holds even though every pattern
# here is a generic shape rather than a specific secret.
#
# Every run also asserts, before trusting any clean result, that a known-bad
# seed value actually matches at least one rule (see the canary block
# below) -- this guards against a scanner that silently matches nothing
# while still reporting success.
set -uo pipefail

SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"

usage() {
  cat <<EOF
usage: $SCRIPT_NAME <path-to-scan>

Scans <path-to-scan> for org-agnostic public-safety shapes (local absolute
paths, bare issue refs, +alias emails, personal-attribution phrases). Prints
file:line and rule name only -- never the matched text. Exits non-zero on
any hit, on a canary failure, or on a usage error.

An optional <path-to-scan>/.public-safe-lintignore file exempts specific
paths from specific named rules (one "<rule-name> <path-glob>" pair per
line). See the header comment in this script for the format and rationale.
EOF
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  usage
  exit 0
fi

ROOT="${1:?$(usage)}"

if [ ! -d "$ROOT" ]; then
  echo "FAIL: scan path does not exist or is not a directory: $ROOT" >&2
  exit 1
fi

# Rule table: name<TAB>grep-E-pattern<TAB>word-boundary(0/1)
# Kept as an array of separate entries (never joined with a shell `|` into
# one combined alternation string): a `|` used as both a delimiter and an
# alternation operator inside a pattern can silently abort the expression in
# some tools -- exiting 0 and looking like it ran while matching nothing.
# One pattern per array entry avoids that collision entirely.
RULES=(
  $'local-absolute-path-users\t(^|[^A-Za-z0-9_/])/Users/[A-Za-z0-9._-]+\t0'
  $'local-absolute-path-home\t(^|[^A-Za-z0-9_/])/home/[A-Za-z0-9._-]+\t0'
  $'tilde-rooted-path\t(^|[^A-Za-z0-9_~])~/[A-Za-z0-9._/-]+\t0'
  $'bare-issue-ref\t(^|[^0-9A-Za-z/])#[0-9]{2,5}\\b\t0'
  $'plus-alias-email\t[A-Za-z0-9._%-]+\\+[A-Za-z0-9._%-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}\t0'
  $'personal-attribution-agreed-with\tagreed with [A-Z]{2,4}\\b\t0'
  $'personal-attribution-per-request\tper [A-Z][a-z]+.s (request|call|note)\t0'
)

RULE_COUNT="${#RULES[@]}"

# --- Canary: before trusting a clean verdict, prove the scan path itself
# actually catches a known-bad seed. Guards two fail-open shapes: BSD sed's
# \b silently behaving as a no-op, and a '|'-joined alternation silently
# aborting the whole expression. Both exit 0 and look like they ran.
_canary_dir="$(mktemp -d)"
trap 'rm -rf "$_canary_dir"' EXIT
# The known-bad shapes below are assembled from sub-fragments rather than
# written as a single contiguous literal. This script is copied verbatim
# into adopting repos and scanned along with everything else (the run step
# is fixed: `bash public-safe-lint.sh .`) -- a canary seed that appears as
# a matchable literal in this file's own source would make the script flag
# itself on every adopting repo, permanently failing the required check
# even on an otherwise-clean tree. Splitting the fragments keeps the
# *assembled* runtime string a faithful known-bad seed (it still matches
# each rule once written to canary.txt) while the source line itself does
# not.
_seed_path_prefix='/Us'
_seed_path_suffix='ers/exampleuser/scratch/file.txt'
_seed_hash='#'
_seed_issue_digits='4321'
_seed_email_local='tester'
_seed_email_rest='+canary@example.com'
{
  echo "canary line one: ${_seed_path_prefix}${_seed_path_suffix}"
  echo "canary line two: bare ref ${_seed_hash}${_seed_issue_digits} with no qualifier"
  echo "canary line three: ${_seed_email_local}${_seed_email_rest}"
} > "$_canary_dir/canary.txt"

_canary_hit=0
for rule in "${RULES[@]}"; do
  IFS=$'\t' read -r _name _pattern _wb <<<"$rule"
  flags=(-rIniE)
  [ "$_wb" = "1" ] && flags+=(-w)
  if grep "${flags[@]}" "$_pattern" "$_canary_dir" >/dev/null 2>&1; then
    _canary_hit=1
    break
  fi
done

if [ "$_canary_hit" -ne 1 ]; then
  echo "CANARY FAILED: known-bad seed did not match any rule in $SCRIPT_NAME." >&2
  echo "The scan path itself is broken -- refusing to trust a clean verdict." >&2
  exit 1
fi
echo "canary: ok (known-bad seed matched at least one of $RULE_COUNT loaded rule(s))"
rm -rf "$_canary_dir"
trap - EXIT

# --- Optional path+rule suppression config. Read from the SCAN
# TARGET's own tree -- never from this script or its template -- so a fork
# PR sees identical behavior (the file travels with the repo, not as an
# injected secret). Format: "<rule-name> <path-glob>" per line, whitespace-
# separated, '#'-prefixed and blank lines ignored.
IGNORE_FILE="$ROOT/.public-safe-lintignore"
IGNORE_ENTRIES=()
if [ -f "$IGNORE_FILE" ]; then
  while IFS=$'\t' read -r rule_field pattern_field; do
    [ -n "$rule_field" ] && [ -n "$pattern_field" ] && \
      IGNORE_ENTRIES+=("$rule_field"$'\t'"$pattern_field")
  done < <(grep -vE '^[[:space:]]*(#|$)' "$IGNORE_FILE" 2>/dev/null | awk '{print $1"\t"$2}')
fi

# Path relative to $ROOT, for matching against ignore-file globs.
_relpath() {
  local f="$1" root="${2%/}"
  case "$f" in
    "$root"/*) printf '%s' "${f#"$root"/}" ;;
    *) printf '%s' "$f" ;;
  esac
}

# True (0) iff an ignore-file entry exempts $2 (a path relative to $ROOT)
# from rule $1. Path+rule only -- never content -- by construction.
_is_suppressed() {
  local rule_name="$1" rel_path="$2" entry ename epattern
  for entry in "${IGNORE_ENTRIES[@]:-}"; do
    [ -z "$entry" ] && continue
    IFS=$'\t' read -r ename epattern <<<"$entry"
    if [ "$ename" = "$rule_name" ] && [[ "$rel_path" == $epattern ]]; then
      return 0
    fi
  done
  return 1
}

# bare-issue-ref logic fix: a `#N` that is the link text of an
# already-fully-qualified GitHub issue/PR URL -- [#N](https://github.com/
# OWNER/REPO/issues/N) or .../pull/N, with the SAME N in both places -- is
# not a leak. Redact every such occurrence from the line, then re-check
# whether a genuine bare ref shape still remains. No backreference regex is
# used (portability across grep implementations); the two numbers are
# captured separately and compared as plain strings.
_bare_issue_ref_is_genuine() {
  local content="$1"
  local redacted="$content"
  local whole bracket_num url_num
  # Regex kept in a variable (not inline in [[ =~ ]]) -- bash's own word
  # parser trips over the literal parens/brackets in the pattern otherwise.
  local qualified_link_re='\[#([0-9]{2,5})\]\(https://github\.com/[^/[:space:])]+/[^/[:space:])]+/(issues|pull)/([0-9]{2,5})\)'
  local bare_ref_re='(^|[^0-9A-Za-z/])#[0-9]{2,5}([^0-9A-Za-z]|$)'
  while [[ "$redacted" =~ $qualified_link_re ]]; do
    whole="${BASH_REMATCH[0]}"
    bracket_num="${BASH_REMATCH[1]}"
    url_num="${BASH_REMATCH[3]}"
    if [ "$bracket_num" = "$url_num" ]; then
      redacted="${redacted/"$whole"/QUALIFIEDREF}"
    else
      # Mismatched numbers: not a genuine qualified link (leave as-is and
      # stop trying to redact, so we can't loop forever on the same match).
      break
    fi
  done
  [[ "$redacted" =~ $bare_ref_re ]]
}

# --- Real scan. file:line + rule name only -- never the matched text.
#
# Disclosure contract: a
# suppressed hit is a real match that a human or CI reader chose not to
# fail on -- it must never be silently dropped, and a run with active
# suppressions must never print output indistinguishable from a genuinely
# clean run. Suppressed hits are reported below as `SUPPRESSED [rule]:
# file:line` (same file:line + rule-name-only discipline as `FAIL`, never
# the matched text), and the coverage summary states how many suppression
# entries were loaded and, per rule, how many hits they suppressed. This
# guards the same fail-open shape the canary above exists to catch --
# "a scanner that silently matches nothing while still reporting success"
# -- reopened through a suppression file instead of a broken pattern.
FAIL=0
FILES_SCANNED="$(find "$ROOT" -type f 2>/dev/null | wc -l | tr -d ' ')"
SUPPRESSED_TOTAL=0
SUPPRESSED_COUNTS=()
SUPPRESSED_RULE_NAMES=()

for i in "${!RULES[@]}"; do
  IFS=$'\t' read -r name pattern wb <<<"${RULES[$i]}"
  flags=(-rIniE)
  [ "$wb" = "1" ] && flags+=(-w)
  SUPPRESSED_COUNTS[$i]=0

  while IFS= read -r hitline; do
    [ -z "$hitline" ] && continue
    file_part="${hitline%%:*}"
    rest="${hitline#*:}"
    line_part="${rest%%:*}"
    content_part="${rest#*:}"

    if [ "$name" = "bare-issue-ref" ] && ! _bare_issue_ref_is_genuine "$content_part"; then
      continue
    fi

    if _is_suppressed "$name" "$(_relpath "$file_part" "$ROOT")"; then
      SUPPRESSED_COUNTS[$i]=$(( SUPPRESSED_COUNTS[$i] + 1 ))
      SUPPRESSED_TOTAL=$(( SUPPRESSED_TOTAL + 1 ))
      echo "SUPPRESSED [$name]: ${file_part}:${line_part} (exempted by .public-safe-lintignore)"
      continue
    fi

    FAIL=1
    echo "FAIL [$name]: ${file_part}:${line_part}"
  done < <(grep "${flags[@]}" "$pattern" "$ROOT" \
      --exclude-dir=.git --exclude-dir=node_modules 2>/dev/null)

  if [ "${SUPPRESSED_COUNTS[$i]}" -gt 0 ]; then
    SUPPRESSED_RULE_NAMES+=("$name")
  fi
done

SUPPRESSED_RULE_COUNT="${#SUPPRESSED_RULE_NAMES[@]}"

echo "== public-safe-lint coverage =="
if [ "$SUPPRESSED_RULE_COUNT" -gt 0 ]; then
  EFFECTIVE_COUNT=$(( RULE_COUNT - SUPPRESSED_RULE_COUNT ))
  echo "rules evaluated: $RULE_COUNT ($SUPPRESSED_RULE_COUNT with active suppressions -- fully-enforced coverage this run is ${EFFECTIVE_COUNT}/${RULE_COUNT})"
else
  echo "rules evaluated: $RULE_COUNT"
fi
if [ -f "$IGNORE_FILE" ]; then
  _entry_word="entries"
  [ "${#IGNORE_ENTRIES[@]}" = "1" ] && _entry_word="entry"
  echo "suppression config: .public-safe-lintignore present, ${#IGNORE_ENTRIES[@]} ${_entry_word} loaded"
  if [ "$SUPPRESSED_TOTAL" -gt 0 ]; then
    echo "suppressed hits:    $SUPPRESSED_TOTAL total -- see SUPPRESSED lines above for file:line + rule; per rule:"
    for i in "${!RULES[@]}"; do
      if [ "${SUPPRESSED_COUNTS[$i]}" -gt 0 ]; then
        IFS=$'\t' read -r rname _ _ <<<"${RULES[$i]}"
        echo "  - ${rname}: ${SUPPRESSED_COUNTS[$i]} hit(s) suppressed"
      fi
    done
  else
    echo "suppressed hits:    0 (loaded entries matched no hits this run)"
  fi
else
  echo "suppression config: none (.public-safe-lintignore not present)"
fi
echo "files scanned:   $FILES_SCANNED"
echo "scan target:     $ROOT"

if [ "$FAIL" -ne 0 ]; then
  echo "verdict:         FAIL -- see FAIL lines above (file:line + rule name only)"
  exit 1
fi

if [ "$SUPPRESSED_TOTAL" -gt 0 ]; then
  echo "verdict:         clean against $RULE_COUNT rule(s) over $FILES_SCANNED file(s) -- WITH $SUPPRESSED_TOTAL SUPPRESSED HIT(S) (${SUPPRESSED_RULE_COUNT}/${RULE_COUNT} rule(s) partially suppressed; this is NOT unconditional ${RULE_COUNT}/${RULE_COUNT} coverage -- review the SUPPRESSED lines and .public-safe-lintignore above)"
else
  echo "verdict:         clean against $RULE_COUNT rule(s) over $FILES_SCANNED file(s)"
fi
exit 0
