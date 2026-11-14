# Decomposition baseline — 2026-09-10

Issue athenaeum#1581. First measurement of whether the librarian turns a page
that has grown several **facets** into a hub plus linked facet sub-pages, and
whether it leaves alone a page that is merely **long**.

> **Updated 2026-11-14 (issue athenaeum#2550).** Case B's row below changed
> from PASS to FAIL, dropping the layer score from 2/3 to 1/3. This is NOT a
> regression in the librarian's ability to decompose a facet-aligned page —
> it is this slice retiring the one deterministic path (the reactive
> `split` disposition) that used to express a decomposition at all.
> Decomposition now lives entirely in the (LLM-backed)
> `librarian.page_recompile` pipeline, which this no-LLM, no-network measure
> cannot exercise. See the per-case note on B below and
> `tests/test_eval_decomposition.py::test_case_b_no_longer_decomposes` for
> the regression pin.

- Corpus: `tests/evals/data/decomposition/cases.yaml` (invented; leakage lint
  green, no skip, against a real local tree).
- Measure: `tests/evals/decomposition.py`.
- Graded by: `tests/test_eval_decomposition.py` (deterministic, runs in default
  CI) and `tests/evals/test_decomposition_eval.py` (metered, `eval`-marked).
- Librarian under test: `develop` @ `3a3db40d`.
- Threshold in force: `tiers.DEFAULT_PAGE_SIZE_THRESHOLD_CHARS` = **10,000
  characters**.

## Per-case result

| Case | Page | Facets | Expected | Observed | Deciding tier | Pass |
|---|---|---|---|---|---|---|
| **A** `multifacet_under_threshold` | 553 chars | 3 | decompose | **not decomposed**, no escalation, 0 children | deterministic size gate | **FAIL** |
| **B** `multifacet_oversize` | 10,930 chars | 3 | decompose | **not decomposed** (as of athenaeum#2550), flagged `needs_split`, 0 children — see note below | deterministic size gate | **FAIL** (was PASS) |
| **C** `singlefacet_oversize` | 11,013 chars | 1 | no decompose | **not decomposed**, flagged `needs_split`, 0 children | deterministic size gate | PASS |
| **D** `decomposed_hub_new_intake` | 159 chars | 2 | attach to existing facet sub-page | **not yet measured** — needs a recorded fixture | classify | — |

**Layer score on the shipped librarian: 1 / 3 deterministic cases (was 2/3
before athenaeum#2550).** A is the anti-vacuity gate (AC3) and it fails as
predicted. B's flip to FAIL is this slice retiring the mechanism that used
to pass it, not a new defect — see the note on B below.

## What the numbers say

**Every verdict above is reached without a model call.** Until athenaeum#2550,
the only code path that decomposed a page was `tiers.check_page_size_gate` →
`_perform_oversize_page_split` (since deleted), triggered by
`len(existing_body) > threshold`. That issue retired the disposition —
`oversize_page_action: split` now resolves to `review` — so as of this
update there is no code path left that decomposes a page without a model
call; decomposition is the (LLM-backed) `librarian.page_recompile`
pipeline's job now. There was never a tier that asked whether a page had
grown several facets, so AC2's "which tier decided" still has the same
answer for A, B and C: the deterministic size gate, deciding nothing but
`len()`.

**A fails because it is never considered.** 553 characters is 5% of the
threshold, so the gate returns `None`, raises no escalation, and writes no
child. All three of A's facets score `missing`. A reader handed this page
would draw a hub and three sub-pages; the librarian sees a small page.

**B now fails too, and the reason is this slice, not a new defect.** Before
athenaeum#2550, B's top-level markdown headings *being* its facets, one to
one, meant the reactive `split` disposition preserved a facet boundary it
was handed (not that it could find one — A proves it cannot). That
disposition is gone: B is now flagged `needs_split` and left untouched,
identically to A and C, because every disposition now shares the same
flag-and-leave-untouched shape at the size gate. The favourable arrangement
B's fixture describes is still real; it is simply no longer observable
through this no-LLM measure. A page whose headings cross its facets would
have scored `scattered`/`conflated` under the old mechanism — that
distinction no longer has a code path to exercise either.

**C passes for the right verdict, now for an unchanged reason.** It is
genuinely over the threshold, flagged `needs_split`, and left untouched —
previously because it had no markdown heading to cut on (the no-heading
cohort athenaeum#1248 documented itself as declining, athenaeum#1282's
job); now because nothing at the size gate restructures anything. The
OBSERVABLE outcome (no children, flagged) is unchanged either way, which is
why C's row did not need to move.

**Nothing was applied, for every case, now including B.** At the shipped
default (`oversize_page_action: review`) no page in any case is
restructured: the over-threshold cases flag `needs_split` in place and the
page is byte-for-byte unchanged, which the measure re-reads from disk to
confirm (AC4, `docs/north-star.md` §2.7–2.8). The `split` disposition is
still exercised against a throwaway `tmp_path` wiki
(`run_split_disposition`), but as a regression pin that it now behaves
identically to the default, not to see what a proposal would contain —
that question moved to `librarian.page_recompile`'s own eval coverage.

## The consolidate / decompose boundary

The operator's design input of 2026-09-10 on athenaeum#1581 ruled that whether
`name (qualifier)` is a duplicate to merge or a legitimate sub-page is decided
by **page size, not qualifier shape**. This is the threshold that lands.

> **Merge a `name` / `name (qualifier)` family iff the body it would fold into
> stays at or under `resolve_page_size_threshold_chars` — the same constant, in
> the same unit (characters of the frontmatter-stripped body), that the
> oversize gate already uses.**

Implemented as `athenaeum.name_structure.merged_body_within_page_size_
threshold`, and measured:

| Family | Shape | Merged body | vs 10,000 | Verdict |
|---|---|---|---|---|
| athenaeum#1570 Cluster B — `Keelbridge` / `Keelbridge (rollout)` | phase qualifier | **780 chars** | 12.8× under | **consolidate** |
| athenaeum#1581 `oversize_family` — `Halstow Junction` / `Halstow Junction (resignalling)` | phase qualifier | **12,067 chars** | 1.21× over | **decompose** |

Two families of the *identical* name shape, opposite verdicts, separated by
size alone. Neither sits near the line: Cluster B clears it by an order of
magnitude, which is what makes "Cluster B stands unchanged" a result rather
than a coincidence.

### Why this threshold and not another

- **It is not a new number.** Reusing the oversize gate's constant is what
  makes consolidation and decomposition *inverses* rather than two
  independently-tuned rules that can contradict each other on one page pair.
- **The application point differs, and that difference is the justification.**
  `check_page_size_gate` measures `len(existing_body)` — one page, as it
  stands. The boundary measures the **prospective sum** of the fold. That
  makes the pair a fixed point: a merge this predicate rejects would
  immediately produce a page the oversize gate wants to split back apart, so
  the system settles instead of oscillating.
- **Characters, not bytes, and not facet count.** The unit is `len()` on a
  `str`, matching `DEFAULT_PAGE_SIZE_THRESHOLD_CHARS`. Facet count is the
  signal for *where* to cut, not for *whether* to fold — a two-facet 400-char
  pair is still one entity written down twice.

### The open contradiction with athenaeum#1577

`name_structure.scan_qualified_name_splits` is **default ON** and reads name
shape alone. Measured today, it proposes `Halstow Junction (resignalling)` for
a fold into `Halstow Junction` — the exact merge the boundary above says is
wrong, on the exact page pair. Recorded as an assertion
(`test_the_shipped_scan_over_proposes_on_the_long_family`), not as prose.

And it reaches a reviewer, not just the scanner:
`propose_qualified_name_merges` returns `{"splits": 1, "queued": 1,
"unfoldable": 0}` and the block lands in `_pending_merges.md`. Worth
separating, because a split whose fold target fails the slugify round-trip
(`fold_target_resolves`) is found and then dropped — this one is not.

`merged_body_within_page_size_threshold` therefore ships **deliberately
unwired**: this issue is an eval, it states and grades the boundary, and it
does not change what a default-ON scan proposes. Wiring it is a behaviour
change that wants its own issue and its own re-scoring on the live corpus
(`scripts/rescore_name_structure.py` is the tool). Until then the scan
over-proposes on long families and this baseline is the record of it.

**Cluster B did not need re-authoring**, which was the open question this
issue was asked to answer. Its short pair sits 12.8× under the threshold, so
"phase qualifier → merge" and "long multi-facet page → keep the sub-pages"
are both true statements about it under one rule.

## Case D is not yet measured

Case D is the only case whose verdict a size gate cannot reach, and the only
metered one. It needs a recorded fixture from an `evals.yml` `record=true`
run; the layer is deliberately **absent from
`tests/fixtures/recorded/seeded-layers.yml`** until then (athenaeum#1581 AC5,
athenaeum#551's never-seeded-vs-seeded-and-lost contract). The command is in
`tests/evals/README.md`. Update this section, not just the table, when it
lands.

## What would change this baseline

- A facet-aware decomposition path landing → Case A starts passing.
  `test_the_gate_separates_size_from_facets` fails loudly with a message
  naming this file, because a fixed librarian must update its baseline rather
  than silently satisfy an assertion.
- Wiring the boundary into athenaeum#1577's scan → the over-proposal
  assertion inverts.
- Any change to `DEFAULT_PAGE_SIZE_THRESHOLD_CHARS` → both tables move
  together, by construction. That is the point of sharing the constant.
- A future deterministic (no-LLM) decomposition path landing → Case B
  starts passing again. `test_case_b_no_longer_decomposes` fails loudly
  with a message naming this file, mirroring A's convention above.
