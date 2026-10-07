# Raw auto-memory cluster member `subject` coverage (athenaeum#1946)

Fixture-only measurement (no `~/knowledge` access; the live corpus pass is
the follow-up `~operator`/`needs:host-write` issue named in athenaeum#1946's
"Design decision — Live run"). Produced by
`scripts`-equivalent direct calls to `athenaeum.coordinate_coverage` and
`athenaeum.subject_population.build_raw_member_subject_report` /
`run_cluster_comparator` / `retire._move_eligibility` over a two-cluster
synthetic fixture (one release-eligible cluster, one still-held cluster),
not hand-typed counts.

## Fixture

- **`release-1`** — two raw members, CONFLICTING content
  (`the deploy process is X` / `Y`). One side carries `claimed_scope:
  team-x`, the other carries none (one-sided absence reads CONTAINS per
  `null_means=universal`). Neither carries `subject` before the backfill.
- **`hold-1`** — two raw members, CONFLICTING content, same shape, but
  neither side carries `claimed_scope` at all (no dimension reads CONTAINS
  on either side).

## AC1 — raw-member `subject` coverage + within-cluster Gate 1 relation split

`athenaeum measure coordinate-coverage --clusters <fixture>.jsonl` output
equivalent (counts only, no names/uids):

| | present | undeterminable | absent |
|---|---|---|---|
| before backfill | 0 | 0 | 4 |
| after backfill | 4 | 0 | 0 |

Within-cluster Gate 1 `subject` relation split (EQUAL / UNKNOWN / DISJOINT):

| | equal | unknown | disjoint |
|---|---|---|---|
| before backfill | 0 | 2 | 0 |
| after backfill | 2 | 0 | 0 |

## Release AC vs. contradiction-still-HOLD AC — before/after `_move_eligibility`

| cluster | verdict before | verdict after | eligible before | eligible after | reason after (if held) |
|---|---|---|---|---|---|
| `release-1` | underdetermined | **specialization** | False | **True** | — |
| `hold-1` | underdetermined | **contradiction** | False | **False** | comparator verdict is contradiction — not safe to retire |

Split against the move-eligibility outcome buckets named in athenaeum#1946's
measurement AC:

- **Released via specialization:** 1 cluster (`release-1`) — moved from
  `underdetermined`/HOLD to `specialization`/eligible once `subject`
  resolved from UNKNOWN to EQUAL and let `_strict_containment` run.
- **Still held, now for a different (truthful) reason:** 1 cluster
  (`hold-1`) — moved from `underdetermined` (missing `subject`) to
  `contradiction` (a real content conflict with no containing dimension),
  staying HELD but for the correct reason instead of a missing coordinate.
- **Unchanged:** 0 in this fixture (both clusters had exactly one pair and
  both pairs were affected by the stamp).

## Finding: staleness is not automatic on stamp

athenaeum#1946's own "Staleness" design note states stamping `subject`
changes `content_hash` so "`record_comparison`'s existing hash-based
memoization recomputes the pair on its own — no explicit `mark_pairs_stale`
call is needed." Verified against the actual ledger code
(`athenaeum.verdicts.get_verdict_status`): freshness is a **persisted**
`VerdictEntry.stale` boolean, not re-derived from a live hash comparison on
every read. The hash-based rule exists
(`select_stale_for_changed_page` + `mark_pairs_stale`), and this
measurement's "after" figures explicitly invoke it before re-comparing —
but nothing in this repo today calls it automatically when a raw file's
frontmatter changes (no live caller, same "dark" posture
`cluster_comparator.run_cluster_comparator` itself documents). A pair
stays memoized as `underdetermined` until *something* re-marks it stale.
Wiring that trigger is follow-up work, not this issue's scope (see
athenaeum#1946 "Out of scope": no live caller for
`run_cluster_comparator` is added here either) — recorded here so the gap
is visible rather than silently assumed away.
