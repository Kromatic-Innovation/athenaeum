# Decomposition baseline — 2026-09-10

Issue athenaeum#1581. First measurement of whether the librarian turns a page
that has grown several **facets** into a hub plus linked facet sub-pages, and
whether it leaves alone a page that is merely **long**.

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
| **B** `multifacet_oversize` | 10,930 chars | 3 | decompose | decomposed into 3 children, `conflict_type=oversize_split`, every facet placed on its own child (`scattered=[] conflated=[] missing=[]`) | deterministic size gate | PASS |
| **C** `singlefacet_oversize` | 11,013 chars | 1 | no decompose | **not decomposed**, `conflict_type=oversize_page`, 0 children | deterministic size gate | PASS |
| **D** `decomposed_hub_new_intake` | 159 chars | 2 | attach to existing facet sub-page | **not yet measured** — needs a recorded fixture | classify | — |

**Layer score on the shipped librarian: 2 / 3 deterministic cases.** A is the
anti-vacuity gate (AC3) and it fails as predicted.

## What the numbers say

**Every verdict above is reached without a model call.** The only code path
that decomposes a page is `tiers.check_page_size_gate` → `_perform_oversize_
page_split`, and its trigger is `len(existing_body) > threshold`. There is no
tier that asks whether a page has grown several facets, so AC2's "which tier
decided" has the same answer for A, B and C — and that sameness is the
finding, not a formality.

**A fails because it is never considered.** 553 characters is 5% of the
threshold, so the gate returns `None`, raises no escalation, and writes no
child. All three of A's facets score `missing`. A reader handed this page
would draw a hub and three sub-pages; the librarian sees a small page.

**B passes, and the pass is narrower than it looks.** B's top-level markdown
headings *are* its facets, one to one, and `_split_into_atomic_sections` cuts
at the shallowest heading depth present. So the result says the mechanism
**preserves a facet boundary it is handed**, not that it can find one. A page
whose headings cross its facets would score `scattered` and `conflated`; the
fixture says so explicitly rather than letting a green row imply otherwise.

**C passes for the right verdict and the wrong reason.** It is genuinely over
the threshold, so the split path is genuinely offered it, and declines —
because it has no markdown heading to cut on (the no-heading cohort
athenaeum#1248 documents itself as declining, athenaeum#1282's job), not
because it has one facet. Both halves are pinned so a future facet-aware path
is not credited with a result the heading check is producing.

**Nothing was applied.** At the shipped default (`oversize_page_action:
review`) no page in any case is restructured: the over-threshold cases raise
an `oversize_page` escalation for the pending queue and the page is
byte-for-byte unchanged, which the measure re-reads from disk to confirm
(AC4, `docs/north-star.md` §2.7–2.8). The `split` disposition is exercised
only against a throwaway `tmp_path` wiki, to see *what a proposal would
contain*.

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
