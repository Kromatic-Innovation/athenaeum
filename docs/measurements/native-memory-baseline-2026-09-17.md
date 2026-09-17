<!-- SPDX-License-Identifier: Apache-2.0 -->

# Native-memory baseline — measurement report, 2026-09-17

Issue athenaeum#1724. First measurement report for the native-memory
comparison specified in
[`../design/native-memory-baseline.md`](../design/native-memory-baseline.md)
(design doc; read its §5 and §7 for the grading contract and the decision
rule this report applies). Phase 1 (read path) only — no Phase 2 write-path
run has happened yet.

## 1. Runs

Five `evals.yml` `workflow_dispatch` grids ran on `develop` on 2026-09-17, all
with the same inputs: `north_star=true`, `north_star_scale=full`, default
corpus scales (`core`, `small`, `medium`, `medium_dense`, `medium_verydense`,
`large`; `xlarge` opt-in excluded), `north_star_max_spend=75`,
`north_star_workers=4`, API mode, FTS5 backend, relevance floor off (as
shipped). Each re-dispatch followed a fixture or grading fix landing on
`develop`; only the fifth run is the measurement.

| # | Workflow run | develop SHA | Wall clock | Rows | Status |
| --- | --- | --- | --- | --- | --- |
| 1 | [35200779015](https://github.com/Kromatic-Innovation/athenaeum/actions/runs/35200779015) | `10175719` | 5m47s | 392 / 1392 | **void — aborted** |
| 2 | [35239792240](https://github.com/Kromatic-Innovation/athenaeum/actions/runs/35239792240) | `af0596bb` | 21m45s | 1392 | **void — control failed** |
| 3 | [35260484135](https://github.com/Kromatic-Innovation/athenaeum/actions/runs/35260484135) | `0b804c64` | 21m8s | 1392 | **void — control partial** |
| 4 | [35266416405](https://github.com/Kromatic-Innovation/athenaeum/actions/runs/35266416405) | `b28392ef` | 23m46s | 1392 | **superseded — probe-authoring defect** |
| 5 | [35272748886](https://github.com/Kromatic-Innovation/athenaeum/actions/runs/35272748886) | `5693c1c7` | 25m6s | 1392 | **the measurement** |

Why each earlier run is void or superseded, one line each:

- **Run 1** aborted at 392 of 1392 cells: `rollout run exceeded token ceiling
  (2018960 > 2000000)`, a hard-coded constant independent of `--max-spend`
  (athenaeum#1754); separately, grading was unsatisfiable as authored because
  no arm repeats an unasked-for reference tag (athenaeum#1753). Resume
  behavior for an aborted run is tracked separately in athenaeum#1751; this
  run was re-dispatched from scratch rather than resumed.
- **Run 2** completed all 1392 rows but failed the oracle positive control:
  `follow_through` graded 0.000-0.167 and `multi_hop` 0.333-0.667 at oracle,
  from two fixture defects fixed by athenaeum#1759 (a wrong field name on the
  follow-through fixture, and 17 oracle cells citing frontmatter `uid:`
  instead of the reference tag).
- **Run 3** cleared oracle on `single_hop`, `temporal`, `disambiguation` but
  still failed on `follow_through` (0/36) and `multi_hop` (24/36), from two
  further fixture defects fixed by athenaeum#1766 (follow-through queries
  answerable from the first page alone; six pages with no reference-tag
  line).
- **Run 4** cleared the oracle positive control on every class at every
  scale for the first time, so its decision block is readable — but two of
  its three `multi_hop` probes stated the answer on the first-hop page while
  the planted token sat on the second page, the same authoring defect
  athenaeum#1766 fixed for `follow_through`. Fixed by athenaeum#1768; run 4 is
  **superseded** by run 5 for the measurement, not void by the same
  mechanism as runs 1-3, but its verdict rows are not the number reported.

**Run 5 (workflow run 35272748886) is the measurement**: positive control
holds on every graded class at every scale, and it runs after the
`multi_hop`-authoring fix. All correctness, cost, crossover and decision
figures below are read from its report unless labelled otherwise.

## 2. Positive control

Oracle (ground-truth pages verbatim) is the positive control: if oracle does
not score at or near 1.000 on the non-abstention gating classes, the grading
contract is not satisfiable and the run's verdict rows must not be read
(design doc §5). Values below are the range across the six corpus scales.

| probe_class | run 2 (35239792240) | run 3 (35260484135) | run 4 (35266416405) | run 5 (35272748886) |
| --- | --- | --- | --- | --- |
| single_hop | 0.500-1.000 | 1.000 | 1.000 | 1.000 |
| multi_hop | 0.333-0.667 | 0.667 | 1.000 | 1.000 |
| temporal | 0.833-1.000 | 1.000 | 1.000 | 1.000 |
| disambiguation | 1.000 | 1.000 | 1.000 | 1.000 |
| follow_through | 0.000-0.167 | 0.000 | 1.000 | 1.000 |

Runs 2 and 3 have a cell below 1.000 in the table above, so their decision
blocks are void per the rule stated above; runs 4 and 5 clear every cell.

## 3. Decision block

### Run 5 (the measurement)

**Cutoff scale: `none`** — no scale at or above `medium` passed all three
conditions. Failing condition per scale, verbatim from the report:

- `core`: condition 1: relationship use case not won -- `push_breadcrumb_pull`
  7/9=0.778 <= best native (`native_grep`) 0.889; condition 2: worse than
  native on `abstention` (0.000 < 0.667); condition 3: undefined for
  `abstention`.
- `large`: condition 1: relationship use case not won -- `push_breadcrumb_pull`
  7/9=0.778 <= best native (`native_grep`) 0.778 (a tie, a fail per ruling
  R2); condition 2: worse than native on `disambiguation` (0.000 < 1.000);
  condition 3: worst reading is `fail` for `multi_hop` (ratio=4.445).
- `medium`: condition 1: **pass**; condition 2: worse than native on
  `disambiguation` (0.000 < 1.000); condition 3: undefined for `redundancy`.
- `medium_dense`: condition 1: **pass**; condition 2: worse than native on
  `disambiguation` (0.000 < 1.000); condition 3: worst reading is `fail` for
  `distractor_robustness` (ratio=4.381).
- `medium_verydense`: condition 1: relationship use case not won --
  `push_breadcrumb_pull` 4/9=0.444 <= best native (`native_index`) 0.778;
  condition 2: worse than native on `distractor_robustness` (0.000 < 1.000);
  condition 3: undefined for `distractor_robustness`.
- `small`: condition 1: relationship use case not won -- `push_breadcrumb_pull`
  7/9=0.778 <= best native (`native_grep`) 0.889; condition 2: worse than
  native on `disambiguation` (0.000 < 1.000); condition 3: undefined for
  `redundancy`.

| scale | cutoff eligible | condition 1 | condition 2 | condition 3 | reading | all pass |
| --- | --- | --- | --- | --- | --- | --- |
| core | no | fail | fail | fail | undefined | fail |
| large | yes | fail | fail | fail | fail | fail |
| medium | yes | pass | fail | fail | undefined | fail |
| medium_dense | no | pass | fail | fail | fail | fail |
| medium_verydense | no | fail | fail | fail | undefined | fail |
| small | no | fail | fail | fail | undefined | fail |

### Run 4 (superseded)

**Cutoff scale: `none`.** Kept here only as the prior reading; the
`multi_hop`-authoring defect (athenaeum#1768) makes this block superseded by
run 5's, above.

- `core`: condition 1: relationship use case not won -- `push_breadcrumb_pull`
  6/9=0.667 <= best native (`native_grep`) 1.000; condition 2: worse than
  native on `disambiguation` (0.000 < 1.000); condition 3: undefined for
  `redundancy`.
- `large`: condition 1: relationship use case not won -- `push_breadcrumb_pull`
  6/9=0.667 <= best native (`native_grep`) 0.667 (a tie); condition 2: worse
  than native on `disambiguation` (0.000 < 1.000); condition 3: undefined
  for `redundancy`.
- `medium`: condition 1: relationship use case not won -- `push_breadcrumb_pull`
  6/9=0.667 <= best native (`native_grep`) 0.778; condition 2: worse than
  native on `disambiguation` (0.000 < 1.000); condition 3: undefined for
  `multi_hop`.
- `medium_dense`: condition 1: relationship use case not won --
  `push_breadcrumb_pull` 5/9=0.556 <= best native (`native_grep`) 0.778;
  condition 2: worse than native on `disambiguation` (0.000 < 1.000);
  condition 3: undefined for `redundancy`.
- `medium_verydense`: condition 1: **pass**; condition 2: worse than native
  on `distractor_robustness` (0.000 < 0.500); condition 3: undefined for
  `distractor_robustness`.
- `small`: condition 1: relationship use case not won -- `push_breadcrumb_pull`
  5/9=0.556 <= best native (`native_grep`) 0.889; condition 2: worse than
  native on `disambiguation` (0.000 < 1.000); condition 3: undefined for
  `redundancy`.

| scale | cutoff eligible | condition 1 | condition 2 | condition 3 | reading | all pass |
| --- | --- | --- | --- | --- | --- | --- |
| core | no | fail | fail | fail | undefined | fail |
| large | yes | fail | fail | fail | undefined | fail |
| medium | yes | fail | fail | fail | undefined | fail |
| medium_dense | no | fail | fail | fail | undefined | fail |
| medium_verydense | no | pass | fail | fail | undefined | fail |
| small | no | fail | fail | fail | undefined | fail |

## 4. Athenaeum versus native at medium (run 5)

### Correctness per class

| probe_class | n | push_breadcrumb_pull | pull | native_grep | native_index | oracle |
| --- | --- | --- | --- | --- | --- | --- |
| single_hop | 4 | 1.000 | 1.000 | 1.000 | 0.750 | 1.000 |
| multi_hop | 3 | 0.667 | 0.333 | 0.667 | 0.333 | 1.000 |
| temporal | 6 | 0.833 | 0.833 | 0.833 | 0.833 | 1.000 |
| disambiguation | 4 | 0.500 | 0.250 | 0.500 | 0.750 | 1.000 |
| follow_through | 6 | 0.667 | 1.000 | 0.667 | 1.000 | 1.000 |
| distractor_robustness | 2 | 1.000 | 1.000 | 0.500 | 1.000 | 1.000 |
| redundancy | 1 | 0.000 | 0.000 | 1.000 | 1.000 | 1.000 |
| abstention | 3 | 0.667 | 1.000 | 0.333 | 0.333 | 0.000 |

(`oracle` grades 0.000 on `abstention` by design — the ground-truth arm is
never handed a reason to decline.)

### Cost per correct answer, verdict arm vs native, at medium

`cost_per_correct = read_tokens / correct_n`, undefined when a cell has zero
correct answers. Ratio is `push_breadcrumb_pull / native_grep`.

| probe_class | push_breadcrumb_pull | native_grep | native_index | ratio (vs native_grep) |
| --- | --- | --- | --- | --- |
| single_hop | 6565.0 | 5217.0 | 36739.3 | 1.26x |
| multi_hop | 50078.0 | 19846.5 | 150577.0 | **2.52x (fail, >2.0x)** |
| temporal | 6393.6 | 7572.4 | 28966.0 | 0.84x |
| disambiguation | 17366.0 | 12748.5 | 49374.3 | 1.36x |
| follow_through | 28328.8 | 20006.2 | 34590.8 | 1.42x |
| distractor_robustness | 5186.0 | 13589.0 | 26057.0 | 0.38x |
| redundancy | undefined | 3621.0 | 27770.0 | undefined (verdict arm scored 0 correct) |
| abstention | 20467.0 | 36635.0 | 46987.0 | 0.56x |

`native_index`'s cost per correct is higher than `native_grep`'s in every
class at medium (this table is scoped to medium only), ranging from about
1.3x (`distractor_robustness`) to about 7.7x (`redundancy`) across the eight
classes above — the index's own token cost never pays for itself against a
plain grep at this scale.

### Crossover scale

Smallest scale, per probe class, at which the best Athenaeum delivery arm's
correctness exceeds the best native arm's (`n/a` = no scale establishes one).

| probe_class | crossover_scale |
| --- | --- |
| abstention | core |
| disambiguation | n/a |
| distractor_robustness | small |
| follow_through | core |
| multi_hop | n/a |
| redundancy | n/a |
| single_hop | n/a |
| temporal | n/a |

### PULL no-call rate

`no_call_rate = count(recall_called=False) / count(PULL rollouts)`. **Zero
everywhere** — every one of the 174 `pull`-arm rows across all eight probe
classes and six scales called `recall` at least once (`no_call_rate = 0.000`
in every row of the report's table). The free data give no evidence for a
non-trivial no-call rate.

**Caveat:** `push_breadcrumb` (hook only, no tool) grades 0.000 on every
non-abstention class by construction — a 200-character-clamped breadcrumb
never carries the reference tag the grading contract requires, so this arm
cannot pass a graded cell regardless of retrieval quality. It is not read
into any of the three §7 conditions (those read `push_breadcrumb_pull` only)
but appears in the report's per-dimension tables.

## 5. Findings and caveats

**Condition 1 (relationship use case) passes at medium and medium_dense in
run 5, and ties at large.** The report prints the pooled relationship-subset
rate (n=9 per scale: the person/company-targeted probes drawn from
single_hop, multi_hop, disambiguation and temporal) only for scales where
condition 1 fails, so no figure is quoted here for medium or medium_dense —
both simply pass, once the `multi_hop` probes require the second hop
(athenaeum#1768) rather than being answerable from the first page. At large
the report does print the figures, and they are an exact tie: `push_breadcrumb_pull`
7/9=0.778 against best native (`native_grep`) 7/9=0.778 — ruling R2 treats a
tie as a fail, so large does not pass condition 1 despite matching native's
rate.

**The two remaining medium fails are both uid-citation-from-snippet cells,
and athenaeum#1793 is still open on how to grade them.** `disambiguation`'s
`repo_not_person` probe and `redundancy`'s `keelbridge_programme_scope` probe
both show the same shape: `push_breadcrumb_pull` answers correctly from a
`recall` snippet without calling `read_entity`, and cites the page's
frontmatter `uid:` because the reference tag sits outside the 400-character
snippet window. Under the athenaeum#1753 tag-only grading ruling this grades
as wrong, even though the answer is correct and the citation identifies the
right page.

**`multi_hop`'s cost ratio is 2.52x at medium, over the 2.0x fail line.**
This is the one class-level cost failure that is not simply "undefined
because the verdict arm scored zero" (redundancy's case) — the verdict arm
did answer two of three `multi_hop` probes correctly, but at roughly 2.5x
`native_grep`'s token cost per correct answer, because the multi-hop tool
loop (breadcrumb, then `recall`, then `read_entity` across two pages) is
inherently more turns than a single grep-and-read.

**The recall-covers-grep results (athenaeum#1770, athenaeum#1771) bound how
far this run's FTS5-backend numbers generalize to the live vector default.**
That measurement found FTS5 recall covering only 6 of 52 probe-relevant
pages against vector's 49 of 52 on the same corpus. This run used FTS5
throughout (see next caveat), so any retrieval-side loss recorded here for
`push_breadcrumb_pull` or `pull` is plausibly a floor, not a ceiling, on what
the shipped vector-backed deployment would show on the same probes.

**FTS5-versus-vector fidelity caveat.** All five dispatches in this report
ran with the FTS5 backend, not the vector backend most deployments actually
use for `recall`. The design doc's arm definitions apply identically to
either backend, but no run in this report exercises vector retrieval, so
this report's Athenaeum-arm numbers should be read as an FTS5-backend
measurement, not a vector-backend one.

**Sample sizes are small and should not be over-read.** The pooled
relationship subset that condition 1 reads is n=9 per scale; several probe
classes (`redundancy` n=1, `distractor_robustness` n=2, `multi_hop` n=3) are
single- or few-probe classes at every scale. A single probe flipping changes
a class's correctness rate by 33-100 percentage points.

**The floor-on pass has not been run.** Design doc §4 calls for a second
pass with the relevance floor active, dispatched via
`north_star_cli.py --relevance-floor-vector` / `--relevance-floor-fts5`
(athenaeum#1761, PR athenaeum#1763). It needs an operator-supplied threshold
before it can be dispatched (athenaeum#1492's production number is still
open), so all five runs in this report used the floor **as shipped**
(inactive). A floor-on pass is additional evidence only — per the design
doc's standing caveat, it can never rescue a fail recorded here.

**The CLI fidelity spot-check has not been run.** Design doc §4 names an
optional `claude -p` CLI-mode spot-check as a way to confirm the API-mode
numbers against Claude Code's own index load and own tools; it has not been
run at any scale, including medium. Every number in this report is API-mode
only.

## 6. What the decision can and cannot read from this

Per the design doc §7 decision rule, **no scale passes all three
conditions** in run 5 — the measurement this report is built on. The cutoff
scale is `none`.

Not every loss recorded here is the same kind of finding, and athenaeum#1736
should read them differently:

- **Losses with an open issue that could change the number**:
  `disambiguation` (repo_not_person) and `redundancy`
  (keelbridge_programme_scope) at medium both fail solely on the
  uid-vs-tag grading question tracked in athenaeum#1793, which is
  unresolved. `surname_is_ambiguous` at medium is flagged separately as one
  of the six FTS5-specific retrieval misses tracked in athenaeum#1789 (a
  distractor page was cited instead of the target), which is a backend
  fidelity gap, not a fixed defect in the verdict arm's behavior. Fixes for
  that class of retrieval gap are pending in athenaeum#1792.
- **A loss that is a genuine miss on its own terms**: `multi_hop`'s cost
  ratio (2.52x at medium) is not attributable to any open grading or
  fixture issue — the tool loop is simply more expensive than a grep, on
  this measurement.
- **Condition 2's `disambiguation` loss recurs at five of six scales** and is
  not scale-specific; it is the same single-probe (`repo_not_person`)
  uid-citation shape named above at every scale where it appears.

This report does not make the go/no-go call — that decision is the
operator's, against athenaeum#1736, informed by which of the above the
operator judges will or will not move with the open issues. It also feeds
the wave 2 epic, athenaeum#1791, which sequences what happens next
regardless of how athenaeum#1736 is decided.

## Addendum: re-grade under the athenaeum#1793 ruling

**Operator ruling (2026-09-18, athenaeum#1793, option 1):** a citation of a
page uid now counts as correct when that uid is in the probe's
`expected_uids` AND appears in the arm's own recall/read-entity (or, for
`native_grep`, its own file-read) tool output for that cell — never merely
because the uid is `expected_uids`. Tag citations (`[ref: TAG]`, the
athenaeum#1753 contract) remain correct exactly as before; this rule is
additive to it, not a replacement. `grade_correctness` implements this in
`tests/evals/north_star_report.py`; see design doc §5 for the contract
statement.

This addendum is a **zero-call re-grade**: run 35272748886's own stored
rows (`north-star-store.jsonl`, 1392 rows, unchanged) were re-graded
offline with the updated `grade_correctness` — no model was called, no new
rollout ran, and the stored answers/transcripts are byte-identical to the
measurement report above. Only the deterministic grading function changed.

**Decision block, re-graded (verbatim from the re-graded report):**

**Cutoff scale: `none`** — unchanged. No scale at or above `medium` passes
all three conditions, before or after this re-grade. Failing condition per
scale:

- `core`: condition 1: relationship use case not won -- `push_breadcrumb_pull`
  7/9=0.778 <= best native (`native_grep`) 0.889; condition 2: worse than
  native on `abstention` (0.000 < 0.667); condition 3: undefined for
  `abstention`.
- `large`: condition 1: relationship use case not won -- `push_breadcrumb_pull`
  7/9=0.778 <= best native (`native_grep`) 0.778; condition 2: worse than
  native on `disambiguation` (0.000 < 1.000); condition 3: worst reading is
  `fail` for `multi_hop` (ratio=4.445).
- `medium`: condition 1: **pass**; condition 2: worse than native on
  `disambiguation` (0.000 < 1.000); condition 3: worst reading is `fail`
  for `multi_hop` (ratio=2.523) — **previously `undefined` for `redundancy`
  (§3's run 5 block), now defined and passing at 1.45x** (see below); the
  worst reading is now `multi_hop`'s pre-existing fail, not a masked
  `undefined`.
- `medium_dense`: condition 1: **pass**; condition 2: worse than native on
  `disambiguation` (0.000 < 1.000); condition 3: worst reading is `fail`
  for `distractor_robustness` (ratio=4.381).
- `medium_verydense`: condition 1: relationship use case not won --
  `push_breadcrumb_pull` 4/9=0.444 <= best native (`native_index`) 0.778;
  condition 2: worse than native on `distractor_robustness` (0.000 < 1.000);
  condition 3: undefined for `distractor_robustness`.
- `small`: condition 1: relationship use case not won -- `push_breadcrumb_pull`
  7/9=0.778 <= best native (`native_grep`) 0.889; condition 2: worse than
  native on `disambiguation` (0.000 < 1.000); condition 3: worst reading is
  `fail` for `disambiguation` (ratio=3.650) — previously `undefined` for
  `redundancy`, now defined and passing (see below).

| scale | cutoff eligible | condition 1 | condition 2 | condition 3 | reading | all pass |
| --- | --- | --- | --- | --- | --- | --- |
| core | no | fail | fail | fail | undefined | fail |
| large | yes | fail | fail | fail | fail | fail |
| medium | yes | pass | fail | fail | fail | fail |
| medium_dense | no | pass | fail | fail | fail | fail |
| medium_verydense | no | fail | fail | fail | undefined | fail |
| small | no | fail | fail | fail | fail | fail |

The only column-level change from §3's run 5 table is the `reading` column
at `medium` and `small` (`undefined` → `fail`); every `condition 1`/`2`/`3`
cell and the `all pass` column are unchanged, and the cutoff scale is still
`none`. The re-grade closes an open grading question, it does not change
the go/no-go reading.

**Medium correctness, verdict arm vs. native, re-graded (athenaeum#1793):**

| probe_class | n | push_breadcrumb_pull | native_grep | native_index | oracle |
| --- | --- | --- | --- | --- | --- |
| single_hop | 4 | 1.000 | 1.000 | 0.750 | 1.000 |
| multi_hop | 3 | 0.667 | 0.667 | 0.333 | 1.000 |
| temporal | 6 | 0.833 | 0.833 | 0.833 | 1.000 |
| disambiguation | 4 | 0.500 | 0.500 | 0.750 | 1.000 |
| follow_through | 6 | 0.667 | 0.667 | 1.000 | 1.000 |
| distractor_robustness | 2 | 1.000 | 0.500 | 1.000 | 1.000 |
| redundancy | 1 | **1.000** | 1.000 | 1.000 | 1.000 |
| abstention | 3 | 0.667 | 0.333 | 0.333 | 0.000 |

Only `redundancy`'s `push_breadcrumb_pull` cell changed (0.000 → **1.000**,
bold above); every other class/arm cell at `medium` is byte-identical to
§4's table. `pull`'s `redundancy` cell also flipped 0.000 → 1.000 (not a
§7 verdict-arm cell, so not in this table; see the row-level detail below).

**Medium cost per correct, verdict arm vs. native, re-graded:**

`cost_per_correct = read_tokens / correct_n`, undefined when a cell has
zero correct answers. Ratio is `push_breadcrumb_pull / native_grep`.

| probe_class | push_breadcrumb_pull | native_grep | native_index | ratio (vs native_grep) |
| --- | --- | --- | --- | --- |
| single_hop | 6565.0 | 5217.0 | 36739.3 | 1.26x |
| multi_hop | 50078.0 | 19846.5 | 150577.0 | **2.52x (fail, >2.0x)** |
| temporal | 6393.6 | 7572.4 | 28966.0 | 0.84x |
| disambiguation | 17366.0 | 12748.5 | 49374.3 | 1.36x |
| follow_through | 28328.8 | 20006.2 | 34590.8 | 1.42x |
| distractor_robustness | 5186.0 | 13589.0 | 26057.0 | 0.38x |
| redundancy | **5260.0** | 3621.0 | 27770.0 | **1.45x (limit, <=2.0x)** — was `undefined` |
| abstention | 20467.0 | 36635.0 | 46987.0 | 0.56x |

**Which cells changed grade, and why.** Re-grading all 1392 stored rows
with the updated `grade_correctness` flips exactly 17 rows from wrong to
correct, never the other direction (the uid-citation rule is additive to
the tag rule, so it can only add correct answers, never remove one).
Fifteen of the seventeen are the `redundancy` class's single probe,
`keelbridge_programme_scope`, across five scales and the `pull`,
`push_breadcrumb_pull` and `push_pages_upper_bound` arms — in every one of
those rows the answer names the target page's literal uid
(`project-keelbridge`), that uid is one of the probe's `expected_uids`, and
it was actually present in that cell's own recall output, so the new rule's
three conditions are all met. The other two flips are `disambiguation`'s
`person_not_repo` probe on `push_pages_upper_bound` at `medium_dense` and
`medium_verydense` (same shape: the delivered page's literal uid, cited and
delivered). **`disambiguation`'s `repo_not_person` probe — the cell named
in §5/§6 as the open uid-vs-tag question — does NOT flip anywhere**: its
answers cite the bare string `rowanwrenfield` (the page's name/tag
vocabulary, truncated into the 400-character recall snippet), never the
page's actual uid `repo-rowanwrenfield`; the ruling requires the literal
uid string, and `rowanwrenfield` is not a substring of `repo-rowanwrenfield`
normalized. That cell's §5/§6 finding is superseded by this addendum: it is
not a case the athenaeum#1793 ruling resolves, and remains a correctness
loss under either grading rule. Three of the seventeen flips land in a
`push_breadcrumb_pull` (verdict-arm) cell -- `redundancy` at `medium`,
`medium_verydense` and `small` -- but only `medium` is a cutoff-eligible
scale (§7's decision reads only `medium` and `large`; `medium_dense`/
`medium_verydense` are density variants of the same size, not "above
medium" in the scale progression the decision rule walks). That single
cutoff-eligible flip is exactly the `medium` `redundancy` cell shown above,
which is why the cutoff scale and every `all pass` reading are unchanged
even though 17 individual rows regraded correct.
