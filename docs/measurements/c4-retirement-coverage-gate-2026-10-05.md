# C4 retirement coverage gate — live subject backfill (2026-10-05)

Host-write half of athenaeum#1944, run as athenaeum#1945. Records the live
`subject` backfill on comparator-eligible wiki pages, the before/after
coverage and Gate 1 measurements, the shadow-parity re-run, and the GO/NO-GO
verdict on precondition (c) of the 2026-09-15 adjudication
(`measurements/c4-retirement-preconditions-2026-09-15.md`, decision 2).

Related: athenaeum#1945, athenaeum#1944, athenaeum#1256, athenaeum#1714,
athenaeum#1663.

## Scope

Wiki pages of the comparator-eligible types only (`concept`, `principle`,
`reference`), per the operator's same-day scope ruling on athenaeum#1945
(occam:disposition, 2026-10-04). Raw auto-memory cluster members are
explicitly out of scope here and are filed as the follow-up athenaeum#1946.

## Rollback

Tag `pre-1944-subject-backfill-2026-10-05`, pushed to the knowledge repo
before the apply. Apply commit: `d2aa2dd986461ddc3471bcb4e8e5b13efec645ac`.
`git -C <knowledge root> revert d2aa2dd986461ddc3471bcb4e8e5b13efec645ac`, or
reset to the tag, restores the pre-backfill state; the backfill only adds a
`subject:` frontmatter line and never overwrites an existing one, so
reverting it loses nothing else.

## Eligible-page count: 3,330, not 3,318

The athenaeum#1944 baseline (2026-10-04) measured 3,318 comparator-eligible
pages. At run time (2026-10-05) the same `discover_wiki_dedupe_candidates`
call returned 3,330 — a +12 drift from organic ingest (the knowledge store
keeps receiving new pages) between the two measurement dates, not a
discovery-rule change: `git diff --stat 5ea1c6f1 df6b2f7c --
src/athenaeum/wiki_dedupe.py` (the baseline checkout vs. the deployed
checkout) is empty. 3,330 is used as the denominator for the 100%-coverage
criterion below.

Of the 3,411 total `concept`/`principle`/`reference` pages, 81 are not
comparator-eligible, fully explained:

| Reason | Count |
| --- | ---: |
| Tagged `archived` | 80 |
| `superseded_by` set | 1 |
| `pointer_stub` / `pii` / body-floor | 0 |
| **Total excluded** | **81** |

3,330 + 81 = 3,411. These 81 pages are expected to carry no `subject` key
after apply, and they do not count against the 100% criterion.

## Decision pass (step 3)

`ATHENAEUM_LLM_PROVIDER=claude-cli`, `ATHENAEUM_SPEND_MAX_TOKENS_PER_RUN=3000000`,
`athenaeum subject-population --path <knowledge root> --report <host path> --json`.
No ceiling trip; no `--resume` needed.

| | |
| --- | ---: |
| Scanned | 3,330 |
| Matched to an existing subject | 156 |
| Minted new subject | 3,125 |
| `undeterminable-ambiguous` | 49 |
| `undeterminable-degraded` | 0 |
| Confirmer calls | 382 |
| Tokens used | 2,095,565 |

Degraded share: 0 / 3,330 = 0.0%, well under the ~25% stop threshold — the
embedder and confirmer were available throughout. The 49 ambiguous decisions
each raised one pending question in `wiki/_pending_questions.md`.

## Apply (step 5, zero spend)

`athenaeum subject-population --path <knowledge root> --from-report <report> --apply`.
`decisions_replayed=3330`, `files_changed=3330`. Independently verified: the
apply commit's diff touches exactly 3,330 `wiki/*.md` pages, and every
addition in those 3,330 pages is a single `+subject:` line — zero other
frontmatter keys changed. The only other files touched are
`wiki/_pending_questions.md` (+49 ambiguous entries) and the new
`wiki/_subject_registry.json`.

Apply commit: `d2aa2dd986461ddc3471bcb4e8e5b13efec645ac`.

## Coverage before and after

Counting rule per athenaeum#1944 (walk `wiki/**/*.md`, skip `_`-prefixed and
`excluded/`, bucket by `type:`). "Before" is the post-quiesce, post-tag
measurement; "after" is post-apply, pre-release.

| type | pages | subject (before → after) | undeterminable (before → after) | absent (before → after) |
| --- | ---: | ---: | ---: | ---: |
| concept | 1,907 | 0 → 1,870 | 0 → 7 | 1,907 → 30 |
| principle | 773 | 0 → 730 | 0 → 42 | 773 → 1 |
| reference | 731 | 0 → 681 | 0 → 0 | 731 → 50 |
| **all three** | **3,411** | **0 → 3,281** | **0 → 49** | **3,411 → 81** |
| every other type | 21,799 | 0 → 0 | 0 → 0 | unchanged |

`claimed_scope`, `valid_from`, `valid_until` are unchanged for `concept`,
`principle` and `reference` between the two snapshots, as required. (Two
other types — `company` and `reference`'s own `claimed_scope` — moved by a
combined +3 between the snapshots from unrelated, concurrent audit/librarian
activity on the live corpus during the run window; independently confirmed
this is **not** from the subject-population apply, whose own commit diff
contains only `subject:` additions.)

**100% coverage criterion:** 3,281 real id + 49 `undeterminable` = 3,330 =
every comparator-eligible page. The 81 pages left `subject_absent` are
exactly the 81 excluded (not eligible) pages identified above — explained
page-count by page-count, with none unaccounted for.

## Gate 1 — wiki domain

`athenaeum measure coordinate-coverage --pairs-from-report <report> --json`,
over the report's (candidate, top-k) pairs (736 pairs total, unchanged set
before/after):

| | EQUAL | UNKNOWN | DISJOINT |
| --- | ---: | ---: | ---: |
| Before apply | 0 | 736 | 0 |
| After apply | 194 | 542 | 0 |

Before: 100% UNKNOWN, as expected (no page carried a `subject` yet). After:
194 pairs read EQUAL — the count of report pairs whose two uids end with the
same non-`undeterminable` subject id. Zero pairs with an `undeterminable`
side read EQUAL, and zero read DISJOINT (expected: `subject_ratified` is not
wired into any comparator call site, so `compare_identity` cannot yet return
DISJOINT — see athenaeum#1944's "Out of scope").

## Gate 1 — cluster domain

`--clusters raw/_librarian-clusters-20261005T134317Z.jsonl` (pinned to the
same file for both readings), over within-cluster raw auto-memory member
pairs:

| | EQUAL | UNKNOWN | DISJOINT |
| --- | ---: | ---: | ---: |
| Before apply | 0 | 20 | 0 |
| After apply | 0 | 20 | 0 |

**Unchanged, as expected and by design.** This backfill is scoped to wiki
pages of the three comparator-eligible types; it does not touch raw
auto-memory cluster members. The domain finding from athenaeum#1944 stands:
this backfill moves Gate 1 for the wiki-page dedupe comparator but changes
nothing for any pair athenaeum#1256's retire lane would compare. Extending
`subject` to raw cluster members is the separate follow-up athenaeum#1946.

## Shadow-parity re-run (step 7)

Same harness and fixtures as the 2026-09-15 run:
`env -u ANTHROPIC_API_KEY ATHENAEUM_LLM_PROVIDER=claude-cli athenaeum measure
shadow-parity --cases tests/evals/data/detector/cases.subject-scope.yaml
--cases tests/evals/data/resolver/cases.subject-scope.yaml --max-usd 5 --json`.

| | 2026-09-15 | 2026-10-05 |
| --- | ---: | ---: |
| Total cases | 18 | 18 |
| `underdetermined` | 0 | 0 |
| Agreement rate | — | 0.722 (13/18) |

**Still 0 of 18 `underdetermined`**, matching the 2026-09-15 reading. This
result is expected and uninformative about the live backfill: the fixture
corpus is a fixed, annotated set that does not read the live wiki, so
nothing the live apply changed could move it.

## Spend

| | |
| --- | ---: |
| Subscription tokens (decision pass) | 2,095,565 |
| Subscription tokens (shadow-parity re-run) | ~93,974 (68,378 in + 25,596 out) |
| Confirmer/LLM calls (decision pass) | 382 |
| Detector/comparator calls (shadow-parity) | 26 (10 + 16) |
| Direct-API (`anthropic`) dollars spent | **$0** |

Both LLM-spending commands ran under `ATHENAEUM_LLM_PROVIDER=claude-cli`,
which fails closed on any other provider with no override flag (the
shadow-parity run additionally ran with `ANTHROPIC_API_KEY` unset). The
`spend` ledger's `subscription.estimated_cost_usd` reads `$0.0` both before
and after the run, confirming no direct-API dollars were attributed to this
lane. (The ledger's `api` bucket moved during the run window, but that
movement is the ledger's rolling accounting window — `since` advances as old
records age out — combined with unrelated concurrent agent activity on the
same host; it is not attributable to this lane's commands.)

## Quiesce — an operational finding

The quiesce sentinel (`<knowledge root>/.athenaeum-quiesce`) was observed
inactive three separate times during this run despite being freshly
re-issued for a 2-hour window each time, including once immediately before
the apply step. `write_quiesce`/`release_quiesce`
(`src/athenaeum/quiesce.py`) have no per-holder ownership check — a
concurrent process's own quiesce/release cycle (the host's
`com.kromatic.athenaeum-reasoning-triggers` scheduled job) can silently
clear another holder's active sentinel. The apply itself only *warns* (does
not refuse) when no sentinel is active, so this did not block the run, and
the apply's own write-safety checks (git-repo requirement, RunLock, no
uncommitted target-page changes) still held. No corruption resulted — the
apply commit's diff was independently verified to contain only `subject:`
additions — but the sentinel's lack of ownership is a gap worth fixing
before the next lane relies on it as a hard guarantee.

## Verdict: GO on precondition (c)

Precondition (c) — the live `subject`/`claimed_scope` backfill, as scoped to
wiki pages, running before any retirement — is **satisfied** for the scope
this issue covers: comparator-eligible wiki pages now carry either a real
`subject` id or `undeterminable`, with 100% coverage over the eligible
population and zero unexplained absences. This discharges decision 2 of the
2026-09-15 adjudication for the wiki-page half of the backfill.

This verdict does **not** by itself clear athenaeum#1256 for release: the
other unmet preconditions from the 2026-09-15 adjudication (athenaeum#1677,
athenaeum#1678, athenaeum#1679, athenaeum#1680, athenaeum#1682) are
unaffected by this issue, and raw auto-memory cluster members — the
population athenaeum#1256's retire lane actually compares — still carry no
`subject` and still read Gate 1 UNKNOWN, unchanged by this backfill
(athenaeum#1946 is the follow-up for that). The sign-off comment on
athenaeum#1256 states this scope explicitly.
