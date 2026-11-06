<!-- SPDX-License-Identifier: Apache-2.0 -->
---
title: "North-star grid noise floor — 2026-11-06"
---

# North-star grid noise floor — 2026-11-06

Issue athenaeum#2309, under epic athenaeum#2295. **What this is:** a bound on
replicate-to-replicate variance in the north-star grid, and the smallest
difference the grid can therefore detect, so that the three conditions ruled on
athenaeum#2312 have an absolute value to be read against. **What this is not:** a
comparison of any arm against any other for the purpose of the epic's decision —
that is athenaeum#2310's job, and the shipped-default ruling is athenaeum#2312's.

## Provenance

Every noise floor below is readable only against ONE model, corpus digest and
grader revision. They are stated here, per run, because a floor measured under
one triple says nothing about a comparison measured under another — the
precondition athenaeum#2310's conditions may only be read under.

| | value |
| --- | --- |
| model | `claude-haiku-4-5-20251001` |
| corpus_digest[core] | `3c5ea876688b089e` |
| corpus_digest[medium] | `42e6ce0dfdc53e02` |
| grader_revision | `athenaeum#1935` |
| search_backend | `vector` (hybrid on, no relevance floor) |
| athenaeum_version | 0.23.4 |
| runs pooled | Actions 42169666857 (partial, 1360 cells) + 42174214012 (480 cells) |
| rows | 1824 after de-duplication; 1764 gradable, 60 harness failures, 16 duplicate, 0 torn |
| grid | 57 probes x 8 arms x {core, medium} x replicates {0, 1} -- complete |

**Neither corpus digest matches the 2026-09-19 run** (`edcd8dd3286d0135` /
`d00353d3d4b95644`), because athenaeum#2303 grew `unprompted_push` from 3 to 12
probes per scale, and the grader moved from `athenaeum#1843` to
`athenaeum#1935`. Nothing in this document may be compared figure-for-figure
with that run, including the pooled correctness the epic quoted from it.

### How the model is attested for run 42169666857

The model that produced this run's rows is `claude-haiku-4-5-20251001`, and the
attestation does not rest on prose:

1. The dispatch passed `north_star_model=claude-haiku-4-5-20251001`
   explicitly, recorded in the run-design comment posted on athenaeum#2309
   **before** the run completed
   (<https://github.com/Kromatic-Innovation/athenaeum/issues/2309#issuecomment-6677918433>).
2. The argv that input produces was verified by executing the workflow's own
   run step against a `python` shim that echoes its arguments, which printed
   `--model claude-haiku-4-5-20251001` in position.
3. The claim is robust even if the input plumbing had failed: the value passed
   is byte-identical to `rollout.DEFAULT_ROLLOUT_MODEL`
   (`config.DEFAULT_CLASSIFY_MODEL`), so a dropped flag yields the same model.

This run's `head_sha` is `6d8dbbd7`, which predates the `RolloutRecord.model`
stamp this issue added, so its rows carry no model of their own and its
rendered report reads `model: unstamped`. **Workflow-dispatch inputs are not
readable from the Actions API** (`.inputs` is absent on the run object), which
is precisely why that stamp was added: from the next run onward, the model
travels with the data and the chain above is unnecessary.

## Run design, and why this grid

Two replicates of the whole 57-probe, 8-arm grid at both `core` and `medium`:
1824 cells. Two replicates is the minimum that yields a replicate-to-replicate
estimate at all, and the alternative that fits the same money -- three
replicates at `core` only -- would have left this issue's "variance per class
**and scale**" criterion unmeasured. Shrinking the probe list instead would
have gutted the small classes (`distractor_robustness` n=2,
`abstention`/`multi_hop`/`negative_knowledge` n=3), which is exactly where the
floor turns out to be widest and therefore most load-bearing.

It took two dispatches, not one, because the first tripped its own token
ceiling at 1360 of 1824 cells (see Spend). The gap was **not** a clean tail of
the probe order -- workers take `(probe, corpus_scale, replicate)` groups
concurrently -- and it swallowed the entire `unprompted_push` class, which is
the one class the epic's first condition is read on. `tests/evals/noise_floor.py`
names the exact missing `(scale, replicate, arm)` triples for that reason; the
second dispatch bought precisely those 480 cells, and the two stores are pooled
by concatenation (the store reader de-duplicates by cell key, last row winning,
so the 16 cells both runs carry resolve to one).

## Method

Three distinct quantities are reported, and conflating them is the easiest way
to misread this document.

### 1. Replicate-to-replicate variance

Two replicates of the same cell key `(probe, arm, corpus_scale)` differ only by
model nondeterminism and by whatever the harness samples per replicate. For a
class of `n` probes measured at `R = 2` replicates, the honest estimator is the
**paired per-probe discordance**: the fraction of probes whose two replicates
disagree on correctness. Writing `d` for the number of discordant probes out of
`n`, the per-probe flip probability is `d / n`, and the variance of a
class-level rate built from one replicate is bounded by the binomial term plus
the flip term. Degrees of freedom: `n`, not `n * R` — the two replicates of one
probe are not two independent probes, and reporting `n * R` would understate
every interval below.

### 2. The minimum detectable difference (the noise floor)

The comparison athenaeum#2312's rule needs is **one arm against
`push_breadcrumb_pull` on the same probes** — a paired comparison, so the
paired (McNemar) form is the correct one and the unpaired normal approximation
is reported only as a cross-check. For a two-sided test at `alpha = 0.05` with
`80%` power, the detectable difference is driven by the discordant pairs, not
by `n` alone: a class where both arms almost always agree can detect a smaller
difference at the same `n` than one where both flip often.

Both values are reported per `(probe_class, corpus_scale)`. **Where `n` is too
small for a normal approximation to mean anything — `distractor_robustness`
(n=2) and the three-probe classes — the number is reported as not supported by
the sample size rather than printed as if it were a measurement.** A
normal-approximation MDE at `n=2` is arithmetic, not evidence.

### 3. Required probe count

Inverting (2) at the measured discordance gives the probes per class needed to
detect differences of 0.05 / 0.10 / 0.15 / 0.20. This is what answers whether
the corpus athenaeum#2303 grew to 12 probes per scale in `unprompted_push` is
large enough for the epic's question, and if not, by how much it falls short.

## Variance per class and scale

Full tables: `docs/measurements/relayed/haiku-merged/noise-floor-haiku-merged.md`
(section 1), rendered by `tests/evals/noise_floor.py` from the pooled store.
Read per arm, because grader noise is not a property of the grid alone -- it is
a property of an (arm, class, scale) cell.

The shape of the result, for the shipped arm `push_breadcrumb_pull`:

| probe_class | scale | n probes | rate rep0 | rate rep1 | discordant probes | SD(class rate) |
| --- | --- | --- | --- | --- | --- | --- |
| abstention | core | 3 | 0.667 | 1.000 | 1 | 0.192 |
| abstention | medium | 3 | 1.000 | 1.000 | 0 | 0.000 |
| disambiguation | core | 4 | 1.000 | 1.000 | 0 | 0.000 |
| disambiguation | medium | 4 | 1.000 | 1.000 | 0 | 0.000 |
| distractor_robustness | core | 2 | 1.000 | 1.000 | 0 | 0.000 |
| follow_through | core | 6 | 0.833 | 1.000 | 0 | 0.000 |
| follow_through | medium | 6 | 1.000 | 0.833 | 1 | 0.108 |
| multi_hop | medium | 3 | 0.333 | 0.667 | 1 | 0.192 |
| redundancy | core | 4 | 0.750 | 0.750 | 0 | 0.000 |
| redundancy | medium | 4 | 1.000 | 0.750 | 1 | 0.153 |
| single_hop | core | 8 | 0.875 | 0.750 | 1 | 0.083 |
| single_hop | medium | 8 | 1.000 | 1.000 | 0 | 0.000 |
| temporal | core | 6 | 1.000 | 1.000 | 0 | 0.000 |
| temporal | medium | 6 | 1.000 | 1.000 | 0 | 0.000 |

Two things to take from it, both of which matter more than any single number:

- **A zero is not evidence of no noise.** Nine of these fourteen cells show
  zero discordant probes, at `n` between 2 and 8. Zero discordance at n=2 is
  what a fair coin looks like four times out of eight; it bounds the flip rate
  loosely from above, and it does not license "this cell is noiseless".
- **Where the class rate sits near 1.000, replication cannot see noise at all.**
  Seven classes read 1.000 in both replicates for the shipped arm. A ceiling
  effect hides variance rather than removing it: the same cell at a harder
  probe set would flip. That is why the floor below is quoted from the arms
  that actually move, and why the `unprompted_push` figures -- the one class
  with rates far from both ends -- are the ones the decision rule should use.

## The noise floor

**The headline number: on `unprompted_push`, the class athenaeum#2312's first
condition is read on, this grid can detect a difference of about 0.30 at
`medium` and 0.34 at `core`, and no smaller** (paired/McNemar form,
alpha=0.05, two-sided, 80% power, at n=20 and n=24 probe-replicates
respectively). Both are in the regime where the normal approximation is
defensible (`n * p_disc >= 5`); almost nothing else in this grid is.

| probe_class | scale | n | MDE (paired) | MDE (unpaired cross-check) | normal approx reliable |
| --- | --- | --- | --- | --- | --- |
| unprompted_push | core | 24 | **0.340** | 0.350 | yes |
| unprompted_push | medium | 20 | **0.299** | 0.370 | yes |
| single_hop | core | 16 | 0.420 | 0.429 | yes |
| single_hop | medium | 16 | 0.303 | 0.328 | no |
| follow_through | core | 11 | 0.243 | 0.249 | no |
| follow_through | medium | 12 | 0.224 | 0.229 | no |
| redundancy | core / medium | 8 | 0.429 | 0.678 / 0.607 | no |
| abstention | core | 6 | 0.426 | 0.447 | no |
| multi_hop | core / medium | 5 / 6 | 0.792 / 0.852 | 0.709 / 0.762 | no |
| aggregation | core / medium | 6 / 6 | 0.852 / 0.539 | 0.809 / 0.762 | no |
| contradiction, disambiguation, distractor_robustness, temporal | both | 4-12 | 0.000 | 0.000 | no |

(Quoted for the `oracle`-vs-`push_breadcrumb_pull` pairing, the widest
ceiling-to-shipped comparison available. The per-arm spread is in the relayed
section 2: for `unprompted_push`/core it runs 0.189 to 0.340 across arms.)

The `0.000` rows are the ceiling effect named above, not a floor of zero:
those cells have zero discordant probes at n=4-12, and the right reading is
"this grid cannot measure noise in this class", not "this class is noiseless".

### Reading athenaeum#2312's rule against this

The ruling fixes multipliers, not absolutes: routed content must beat
`push_breadcrumb_pull` on `unprompted_push` by **more than 2x the noise floor**,
and lose no more than **1x** on any enrolled class. Substituting the floor
above:

| condition | threshold at this floor |
| --- | --- |
| 1 (beat, `unprompted_push`) | **> 0.68** at `core`, **> 0.60** at `medium` |
| 2 (no loss, per enrolled class) | 0.24 to 0.43 for the classes with a measurable floor; unmeasurable for the four at 0.000 |

**Condition 1 cannot be satisfied by any arm on this corpus, and that is a
finding about the grid, not about routed content.** The ceiling arm `oracle` --
handed the ground-truth pages verbatim -- scores 0.375 at `core` and 0.400 at
`medium` on `unprompted_push`, against `push_breadcrumb_pull`'s 0.125 and
0.050. The largest difference any arm could show against the shipped arm is
therefore about 0.25-0.35, which is **below** the 0.60-0.68 the rule demands.
A null result on condition 1 at this probe count would say nothing about
whether routed content works.

## Counter-example: `oracle` scored below the arm it caps

The epic recorded, by hand, that pooling each arm's correctness across all
classes gives about 0.76 for `oracle` and about 0.84 for `push_breadcrumb_pull`
— a ceiling arm below the arm it should cap, which is the observation that
motivated measuring the floor before any comparison.

**It does not reproduce here.** Pooled over all 12 classes, both scales and
both replicates, n-weighted, with harness failures excluded rather than scored:

| arm | n gradable cells | correct | rate |
| --- | --- | --- | --- |
| `oracle` | 228 | 173 | **0.759** |
| `push_breadcrumb_pull` | 217 | 153 | **0.705** |
| `pull` | 217 | 149 | 0.687 |
| `native_grep` | 204 | 118 | 0.578 |
| `native_index` | 222 | 121 | 0.545 |
| `push_pages_upper_bound` | 228 | 99 | 0.434 |
| `none` | 228 | 12 | 0.053 |
| `push_breadcrumb` | 220 | 9 | 0.041 |

The ceiling arm is **above** the arm it caps, by 0.054 -- which is itself well
inside the noise floor above, so the right statement is "no measurable
difference between `oracle` and `push_breadcrumb_pull` pooled", not "`oracle`
wins". The epic's 0.76 / 0.84 inversion was computed by hand over run
35443944736, which is a **different corpus digest and a different grader
revision** (see Provenance); it is not evidence about this corpus, and this
measurement supersedes it for that reason rather than contradicting it.

What survives the change is narrower and still worth naming: **`oracle` is
below `push_breadcrumb_pull` in two specific classes**, `redundancy` (0.500 vs
0.750 at `core`; 0.625 vs 0.875 at `medium`) and `single_hop` at `core` (0.688
vs 0.812). Both gaps are inside those classes' own floors (0.429 and 0.420), so
neither is measurable at this n -- but the mechanism is visible in the
per-probe listing (relayed section 4): across 55 cells where `oracle` was
graded incorrect, `push_breadcrumb_pull` was correct on the same probe,
scale and replicate in **17**, concentrated in `single_hop` (8 of 9) and
`redundancy` (4 of 7). An arm handed the exact answer pages losing to an arm
handed three 200-character breadcrumbs, on a single-hop lookup, is a grader or
probe-specification signal rather than a retrieval one.

**No grader change is made here.** This issue's own criterion is "explain or
fix", and the explanation above is specific enough to act on separately; a
grader change would invalidate every figure in this document and require the
whole measurement again (which the USD 20 cap cannot buy). The 30 of those 55
`oracle`-incorrect cells that sit in `unprompted_push` are the positive-control
failure below, which is the sharper version of the same signal.

## Positive control: does `oracle` answer the new probes?

**No. The ceiling arm answers fewer than half of them.**

| scale | replicate | n | correct | rate | harness failures |
| --- | --- | --- | --- | --- | --- |
| core | 0 | 12 | 4 | 0.333 | 0 |
| core | 1 | 12 | 5 | 0.417 | 0 |
| medium | 0 | 12 | 6 | 0.500 | 0 |
| medium | 1 | 12 | 3 | 0.250 | 0 |

`oracle` is handed the ground-truth pages verbatim, so a probe it cannot answer
is not a retrieval failure and not a delivery failure. For the 12
`unprompted_push` probes athenaeum#2303 added, the ceiling sits at 0.25-0.50,
and every other arm is at or near zero (`push_breadcrumb_pull` 0.125 / 0.050;
`none`, `pull`, `push_breadcrumb`, `push_pages_upper_bound` all 0.000 at
`core`).

This is the positive control failing, and it is the most consequential finding
in this document: **the class the epic's first condition is read on currently
has a measurable range of about 0.4, against a decision threshold of 0.6-0.68.**
Two readings are consistent with the data and this grid cannot separate them:

1. The probes are task-shaped by design (the model must volunteer a pushed fact
   unprompted), so even perfect delivery does not compel the answer -- in which
   case 0.4 is the honest ceiling of the class and the rule's multipliers need
   an absolute floor that acknowledges it.
2. The grading criterion for this class is stricter than the probes' own
   ground truth supports, in which case the ceiling is an artifact.

Separating them needs a hand-read of the 30 `oracle`-incorrect
`unprompted_push` cells against their ground truth -- cheap, unpaid, and not in
this issue's scope. It is the natural next slice under the epic.

## Is the probe count high enough for the epic's question?

**No, by roughly an order of magnitude.** Inverting the paired formula at the
measured discordance, for the `oracle`-vs-`push_breadcrumb_pull` pairing:

| probe_class | scale | n achieved | n needed for 0.20 | for 0.15 | for 0.10 | for 0.05 |
| --- | --- | --- | --- | --- | --- | --- |
| unprompted_push | core | 24 | 70 | 124 | 278 | 1112 |
| unprompted_push | medium | 20 | 45 | 80 | 179 | 715 |
| single_hop | core | 16 | 71 | 126 | 283 | 1129 |
| single_hop | medium | 16 | 37 | 66 | 148 | 589 |
| redundancy | core / medium | 8 | 37 | 66 | 148 | 589 |
| follow_through | core | 11 | 17 | 29 | 65 | 260 |
| follow_through | medium | 12 | 15 | 27 | 60 | 240 |

`n` here counts probe-replicates, so at two replicates the probe counts are
half. Concretely, for `unprompted_push`: detecting a 0.10 difference at `medium`
needs about **90 probes per scale** against the 12 athenaeum#2303 built, and
detecting 0.20 needs about **23**. athenaeum#2303 chose 12 as a fixed working
floor explicitly so that this issue could ask for more; the ask is **12 -> at
least 23 probes per scale** to make a 0.20 difference detectable, and about 90
for 0.10.

The cheaper lever is the ceiling, not the count: raising `oracle`'s own
`unprompted_push` rate from 0.4 toward 1.0 (reading 1 or 2 above) would widen
the measurable range far more than tripling the probe count does, and costs no
paid runs.

## A floor for the higher-tier model

**Not measured here, and the reason is the cap, not an oversight.** A
replicated Sonnet-class grid prices at 3x Haiku (`claude-sonnet-5` is
$3/$15 per MTok against Haiku 4.5's $1/$5), and the Haiku grid above consumed
USD 17.28 of this issue's USD 20. There was no room.

The higher-tier floor is measured inside athenaeum#2310's own cap instead, from
the same store as its comparison: that dispatch runs `--replicates 0,1`, so its
floor and its comparison share one model, one corpus digest and one grader
revision -- which is exactly the precondition athenaeum#2310's own AC5 requires
before its conditions may be read. See
`docs/measurements/routed-section-arm-2026-11-06.md`.

One finding from that dispatch belongs here, because it bounds what any
higher-tier floor can cost: the Sonnet-class grid **cannot currently run to
completion at all**. It aborted at 810 of 864 cells with
`ValueError: response_text: no text block found among 1 content block(s);
block types were ['thinking']` (`src/athenaeum/provider.py:617`) -- the
provider has no fallback for a thinking-only response, which a Sonnet-class
model emits and Haiku does not. Filed separately; it is not fixable inside
this issue's blast radius (`provider.py` is designated reasoning surface and a
change there needs its own eval receipt).

## Spend

Every dispatch is listed, including failed and partial ones, with its
pre-flight estimate and its actual spend. Where a run does not report an
actual, its pre-flight estimate is counted, per this issue's own rule.

| dispatch | what | pre-flight estimate | actual | cells |
| --- | --- | --- | --- | --- |
| [42169666857](https://github.com/Kromatic-Innovation/athenaeum/actions/runs/42169666857) | replicated grid, both scales | $15.60 + $0.07 pre-flight | **$12.39** | 1360 of 1824 (ceiling trip) |
| 42169666857 | `eval` job on the same dispatch | n/a | **$0.61** (cited, see below) | n/a |
| [42174214012](https://github.com/Kromatic-Innovation/athenaeum/actions/runs/42174214012) | the 480 missing cells | $4.10 + $0.07 pre-flight | **$4.89** | 480 of 480 |
| 42174214012 | `eval` job on the same dispatch | n/a | **$0.61** (cited) | n/a |
| | **total** | | **$18.50** | |
| | **cap** | | $20.00 | |
| | **unspent, released to athenaeum#2310** | | **$1.50** | |

Grid figures are computed by `tests/evals/noise_floor.py` from each store's own
persisted `turn_tokens`, priced at `claude-haiku-4-5-20251001` rates with the
cache multipliers `athenaeum.models` documents, counting **every** persisted row
including the 60 harness-failed cells (a turn-cap kill is billed exactly like an
answered cell). The pooled union prices at $17.15 rather than the $17.28 the two
runs sum to: the 16 cells both runs carry were paid for twice and the
de-duplicated union counts them once. The larger figure is the one against the
cap.

The two `eval`-job figures are **not** this dispatch's own receipts. The
`eval` job runs on every non-`calibration` dispatch of `evals.yml` and its
`eval-summary.json` is not reachable from a build lane;
`docs/design/eval-trigger-surface.md` §2 prices that job at about USD 0.61,
"identical within 3% across three sampled runs", and that is the figure counted
here. This issue's rule is to count a pre-flight estimate when a run does not
report an actual; a measured figure from three samples of the same job is
strictly better than that, and it is cited rather than asserted.

### Both pre-flight estimates were low, in the same direction

`--dry-run` prices a cell at 5150 tokens. The observed cost was 8580
tokens/cell over run 42169666857 and 8581 over 42174214012, so the dollar
projection ran 7% low on the first run and 19% low on the second (the
`unprompted_push` probes the second run carried are more expensive than the
grid average). **The token ceiling errs the other way**, and that is what
actually bit: `--max-spend 18.00` derives its ceiling at a blended
$1.66/MTok, assuming about 16.5% of tokens are output, where this grid's
actual output share is 3.4%. The ceiling therefore binds at about 69% of the
nominal dollar cap -- run 42169666857 stopped at a true $12.39 against an
"$18.00" ceiling. Sizing a replicated grid off `--max-spend` alone will keep
truncating it; `--max-tokens` (which overrides the derivation) is the knob to
set, as the second dispatch and athenaeum#2310's did.

## Limits

- **One model, one grader, one backend.** Everything here is
  `claude-haiku-4-5-20251001`, grader `athenaeum#1935`, `--search-backend
  vector`. A floor does not transfer across any of the three.
- **Two replicates bound noise loosely.** Every cell's variance rests on one
  paired difference per probe; `df` runs 1-11. The `0.000` cells mean "not
  measurable here", never "noiseless".
- **Rate ceilings hide variance.** Seven classes read 1.000 in both replicates
  for the shipped arm, which makes their floors unmeasurable rather than small.
- **The model is attested, not stamped, for run 42169666857.** Its rows predate
  the `RolloutRecord.model` field this issue added (see Provenance); run
  42174214012's rows carry it. The pooled report names the arms whose model is
  not independently verifiable from the data.
- **This measures grader noise, not model noise.** Replicates differ by model
  nondeterminism AND by whatever the harness samples per replicate; the
  estimator does not separate them.
- **60 harness failures (3.3% of cells) are excluded from every rate** and
  reported separately. They are concentrated in `native_grep` (24) and the
  `pull`/`push_breadcrumb_pull` turn caps, so arms are not equally represented:
  `n gradable` runs 204-228 across arms.
