<!-- SPDX-License-Identifier: Apache-2.0 -->
---
title: "Contact-attribution calibration — 2026-11-11"
---

# Contact-attribution calibration — 2026-11-11

Issue athenaeum#2260, under epic athenaeum#2192. **What this is:** the first
live calibration of the contact-value attribution judge, and the record
(`src/athenaeum/calibration_data/contact_attribution.yaml`) it produced.
**What this is not:** a decision to turn enforcement on. That is
athenaeum#2220, which additionally needs an acknowledged clean observe report on
a real store.

## Provenance

| | value |
| --- | --- |
| workflow | `evals.yml`, `suite=calibration` (workflow_dispatch) |
| run | Actions 42959041029 (conclusion: success) |
| head sha | `0d63b08c` on `develop` |
| models | `claude-haiku-4-5-20251001`, `claude-sonnet-5` |
| `prompt_sha256` | `334d2d8dce0ef8c4c341213f4d080b1531db5aa023c40d675231bc30dfa4a668` |
| `dataset_sha256` | `bd6994cb9a2d12f894608fae7ab21cddb35bee03f86a2dea4c450dfdf8457016` |
| cases | 3177 of 3434 attribution case results passed (both models, both signals, fit and held-out) |
| pass bar | one-sided Wilson lower bound >= 0.95 on the held-out split, abstain share <= 0.30, no `observed` usage class (athenaeum#2344) |

Both shas were re-derived against `develop` at `d62bc553` before this record was
committed: the judge prompt hashes to the same `334d2d8d...` and
`tests/evals/data/attribution/cases.yaml` to the same `bd6994cb...`. The
resolver-prompt change in athenaeum#2332 does not touch the attribution prompt, so
the record is not stale on commit. A later change to either input invalidates
it and the `auto` preflight will treat it as stale.

## Result per class

The record keeps one entry per class: the model the harness selected, or, when
none clears the bar, the failing measurement.

| class | recorded model | signal | n fit / holdout | threshold | holdout accuracy above | Wilson LB | abstain share | passes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| held-elsewhere | haiku-4-5 | class-level | 78 / 52 | 1.0 | 1.0 | 0.9505 | 0.0 | yes |
| source-context | haiku-4-5 | agreement-k3 | 79 / 53 | 1.0 | 1.0 | 0.9514 | 0.0 | yes |
| non-person-target | haiku-4-5 | class-level | 78 / 52 | 1.0 | 1.0 | 0.9505 | 0.0 | yes |
| role-unmatched | **sonnet-5** | class-level | 79 / 53 | 1.0 | 1.0 | 0.9514 | 0.0 | yes |
| names-other | haiku-4-5 | class-level | 79 / 53 | 0.0 | 0.0 | 0.0 | 1.0 | **no** |

Two readings worth stating. `role-unmatched` passes only on sonnet-5: haiku-4-5
failed it (Wilson LB 0.0), so that class's recommendation carries the more
expensive model. And a 52-case held-out split can reach a one-sided lower bound
of 0.95 only at a perfect score, which is why every passing class sits at
accuracy 1.0 with a lower bound of 0.9505 or 0.9514; there is no headroom.

## Per-model, per-class, per-signal measurements

The table above is the record: one row per class. This section is the full
reading athenaeum#2260's second acceptance criterion asks for, for every
(model, class, signal) the run measured. `self-reported` and `class-level` ran
for every class on both models; `agreement-k3` ran only where the self-reported
fit split gave a reason to (AUROC below 0.75, or undefined for a reason other
than "no wrong cases", athenaeum#2342), so it is absent elsewhere.

Every figure is computed by the repo's own metric code
(`tests/evals/metrics.py` and `tests/evals/attribution_eval.py`), not
reimplemented, from the run's `calibration-summary.json` and
`calibration-recorded-fixtures` artifact, by
[`scripts/derive_attribution_calibration_tables.py`](../../scripts/derive_attribution_calibration_tables.py).
The script also re-runs the harness's signal selection over the rebuilt runs and
checks model, signal, threshold, held-out accuracy, held-out Wilson lower bound,
abstain share and `passes` against the committed record; all five classes
match.

How to read the columns:

- **accuracy / Wilson 95%**: over all cases of that (model, class), fit plus
  held-out, with the two-sided 95% Wilson interval (`wilson_interval`).
- **AUROC (fit)**: AUROC of score against correctness on the fit split, the
  split the harness uses to choose a signal (`AUROC_BAR` 0.75).
- **threshold, held-out acc above, Wilson LB, abstain share**: the threshold is
  derived on the fit split (`derive_threshold`); the other three are on the
  held-out split. The lower bound is the one-sided bound the pass bar uses
  (`PASS_BAR_LOWER_BOUND`), so it is not the lower end of the two-sided interval
  in the first table.
- **ECE (held-out)**: over the five equal-count bins of the next table.
- **$/case**: mean cost per case from the existing `TokenUsage` accounting
  (`cost_per_case`), summed over that signal's own calls. `agreement-k3` is the
  three K3 calls only; the self-reported call that precedes it is a separate
  row, so the all-in cost of the agreement path is the sum of both rows.

| model | class | signal | n | accuracy | Wilson 95% | AUROC (fit) | $/case |
| --- | --- | --- | --- | --- | --- | --- | --- |
| haiku-4-5 | held-elsewhere | self-reported | 130 | 1.000 | [0.971, 1.000] | undefined (no-wrong-cases) | 0.00170 |
| haiku-4-5 | held-elsewhere | class-level | 130 | 1.000 | [0.971, 1.000] | n/a (constant score) | 0.00170 |
| sonnet-5 | held-elsewhere | self-reported | 130 | 1.000 | [0.971, 1.000] | undefined (no-wrong-cases) | 0.00473 |
| sonnet-5 | held-elsewhere | class-level | 130 | 1.000 | [0.971, 1.000] | n/a (constant score) | 0.00473 |
| haiku-4-5 | names-other | self-reported | 132 | 0.811 | [0.735, 0.868] | 0.323 | 0.00139 |
| haiku-4-5 | names-other | agreement-k3 | 132 | 0.833 | [0.761, 0.887] | 0.619 | 0.00416 |
| haiku-4-5 | names-other | class-level | 132 | 0.811 | [0.735, 0.868] | n/a (constant score) | 0.00139 |
| sonnet-5 | names-other | self-reported | 132 | 0.841 | [0.769, 0.894] | 0.555 | 0.00447 |
| sonnet-5 | names-other | agreement-k3 | 132 | 0.833 | [0.761, 0.887] | 0.500 | 0.01343 |
| sonnet-5 | names-other | class-level | 132 | 0.841 | [0.769, 0.894] | n/a (constant score) | 0.00447 |
| haiku-4-5 | source-context | self-reported | 132 | 0.985 | [0.946, 0.996] | 0.623 | 0.00154 |
| haiku-4-5 | source-context | agreement-k3 | 132 | 1.000 | [0.972, 1.000] | undefined (no-wrong-cases) | 0.00453 |
| haiku-4-5 | source-context | class-level | 132 | 0.985 | [0.946, 0.996] | n/a (constant score) | 0.00154 |
| sonnet-5 | source-context | self-reported | 132 | 1.000 | [0.972, 1.000] | undefined (no-wrong-cases) | 0.00466 |
| sonnet-5 | source-context | class-level | 132 | 1.000 | [0.972, 1.000] | n/a (constant score) | 0.00466 |
| haiku-4-5 | non-person-target | self-reported | 130 | 1.000 | [0.971, 1.000] | undefined (no-wrong-cases) | 0.00131 |
| haiku-4-5 | non-person-target | class-level | 130 | 1.000 | [0.971, 1.000] | n/a (constant score) | 0.00131 |
| sonnet-5 | non-person-target | self-reported | 130 | 1.000 | [0.971, 1.000] | undefined (no-wrong-cases) | 0.00456 |
| sonnet-5 | non-person-target | class-level | 130 | 1.000 | [0.971, 1.000] | n/a (constant score) | 0.00456 |
| haiku-4-5 | role-unmatched | self-reported | 132 | 0.947 | [0.895, 0.974] | 0.500 | 0.00119 |
| haiku-4-5 | role-unmatched | agreement-k3 | 132 | 0.962 | [0.914, 0.984] | 0.660 | 0.00358 |
| haiku-4-5 | role-unmatched | class-level | 132 | 0.947 | [0.895, 0.974] | n/a (constant score) | 0.00119 |
| sonnet-5 | role-unmatched | self-reported | 132 | 1.000 | [0.972, 1.000] | undefined (no-wrong-cases) | 0.00416 |
| sonnet-5 | role-unmatched | class-level | 132 | 1.000 | [0.972, 1.000] | n/a (constant score) | 0.00416 |

| model | class | signal | threshold (fit) | held-out acc above | Wilson LB | abstain share | ECE (held-out) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| haiku-4-5 | held-elsewhere | self-reported | 0.95 | 1.000 | 0.9505 | 0.000 | 0.0398 |
| haiku-4-5 | held-elsewhere | class-level | 1 | 1.000 | 0.9505 | 0.000 | n/a (class-level, §12.3) |
| sonnet-5 | held-elsewhere | self-reported | 0.85 | 1.000 | 0.9505 | 0.000 | 0.0679 |
| sonnet-5 | held-elsewhere | class-level | 1 | 1.000 | 0.9505 | 0.000 | n/a (class-level, §12.3) |
| haiku-4-5 | names-other | self-reported | 0.98 | 0.000 | 0.0000 | 1.000 | 0.1770 |
| haiku-4-5 | names-other | agreement-k3 | none reachable | n/a | 0 (no threshold) | 1.0 (no threshold) | n/a (no threshold) |
| haiku-4-5 | names-other | class-level | none reachable | n/a | 0 (no threshold) | 1.0 (no threshold) | n/a (class-level, §12.3) |
| sonnet-5 | names-other | self-reported | none reachable | n/a | 0 (no threshold) | 1.0 (no threshold) | n/a (no threshold) |
| sonnet-5 | names-other | agreement-k3 | none reachable | n/a | 0 (no threshold) | 1.0 (no threshold) | n/a (no threshold) |
| sonnet-5 | names-other | class-level | none reachable | n/a | 0 (no threshold) | 1.0 (no threshold) | n/a (class-level, §12.3) |
| haiku-4-5 | source-context | self-reported | 0.75 | 1.000 | 0.9514 | 0.000 | 0.0394 |
| haiku-4-5 | source-context | agreement-k3 | 1 | 1.000 | 0.9514 | 0.000 | 0.0000 |
| haiku-4-5 | source-context | class-level | 1 | 1.000 | 0.9514 | 0.000 | n/a (class-level, §12.3) |
| sonnet-5 | source-context | self-reported | 0.6 | 1.000 | 0.9514 | 0.000 | 0.0919 |
| sonnet-5 | source-context | class-level | 1 | 1.000 | 0.9514 | 0.000 | n/a (class-level, §12.3) |
| haiku-4-5 | non-person-target | self-reported | 0.95 | 1.000 | 0.9505 | 0.000 | 0.0500 |
| haiku-4-5 | non-person-target | class-level | 1 | 1.000 | 0.9505 | 0.000 | n/a (class-level, §12.3) |
| sonnet-5 | non-person-target | self-reported | 0.85 | 1.000 | 0.9505 | 0.000 | 0.1004 |
| sonnet-5 | non-person-target | class-level | 1 | 1.000 | 0.9505 | 0.000 | n/a (class-level, §12.3) |
| haiku-4-5 | role-unmatched | self-reported | none reachable | n/a | 0 (no threshold) | 1.0 (no threshold) | n/a (no threshold) |
| haiku-4-5 | role-unmatched | agreement-k3 | 0.6667 | 0.962 | 0.8922 | 0.000 | 0.0252 |
| haiku-4-5 | role-unmatched | class-level | none reachable | n/a | 0 (no threshold) | 1.0 (no threshold) | n/a (class-level, §12.3) |
| sonnet-5 | role-unmatched | self-reported | 0.75 | 1.000 | 0.9514 | 0.000 | 0.1289 |
| sonnet-5 | role-unmatched | class-level | 1 | 1.000 | 0.9514 | 0.000 | n/a (class-level, §12.3) |

### Reliability tables

Five equal-count bins on the held-out split, each as `[lowest score, highest
score] observed accuracy`. Where a class has no wrong held-out case the
observed accuracy is 1.000 in every bin, so the table shows only the score
spread.

| model | class | signal | bins |
| --- | --- | --- | --- |
| haiku-4-5 | held-elsewhere | self-reported | [0.95, 0.95] 1.000 ; [0.95, 0.95] 1.000 ; [0.95, 0.95] 1.000 ; [0.95, 0.98] 1.000 ; [0.98, 0.99] 1.000 |
| sonnet-5 | held-elsewhere | self-reported | [0.85, 0.90] 1.000 ; [0.90, 0.95] 1.000 ; [0.95, 0.97] 1.000 ; [0.97, 0.97] 1.000 ; [0.97, 0.97] 1.000 |
| haiku-4-5 | names-other | self-reported | [0.85, 0.95] 0.909 ; [0.95, 0.95] 1.000 ; [0.95, 0.95] 0.909 ; [0.95, 0.95] 0.600 ; [0.95, 0.95] 0.500 |
| haiku-4-5 | source-context | self-reported | [0.85, 0.95] 1.000 ; [0.95, 0.95] 1.000 ; [0.95, 0.98] 1.000 ; [0.98, 0.99] 1.000 ; [0.99, 0.99] 1.000 |
| haiku-4-5 | source-context | agreement-k3 | [1.00, 1.00] 1.000 ; [1.00, 1.00] 1.000 ; [1.00, 1.00] 1.000 ; [1.00, 1.00] 1.000 ; [1.00, 1.00] 1.000 |
| sonnet-5 | source-context | self-reported | [0.60, 0.90] 1.000 ; [0.90, 0.95] 1.000 ; [0.95, 0.95] 1.000 ; [0.97, 0.97] 1.000 ; [0.97, 0.98] 1.000 |
| haiku-4-5 | non-person-target | self-reported | [0.95, 0.95] 1.000 ; [0.95, 0.95] 1.000 ; [0.95, 0.95] 1.000 ; [0.95, 0.95] 1.000 ; [0.95, 0.95] 1.000 |
| sonnet-5 | non-person-target | self-reported | [0.85, 0.90] 1.000 ; [0.90, 0.90] 1.000 ; [0.90, 0.90] 1.000 ; [0.90, 0.90] 1.000 ; [0.90, 0.95] 1.000 |
| haiku-4-5 | role-unmatched | agreement-k3 | [0.67, 1.00] 0.818 ; [1.00, 1.00] 1.000 ; [1.00, 1.00] 1.000 ; [1.00, 1.00] 1.000 ; [1.00, 1.00] 1.000 |
| sonnet-5 | role-unmatched | self-reported | [0.75, 0.80] 1.000 ; [0.80, 0.85] 1.000 ; [0.90, 0.90] 1.000 ; [0.90, 0.93] 1.000 ; [0.93, 0.95] 1.000 |

### Metrics that are undefined, and why

- **AUROC, `undefined (no-wrong-cases)`**: every fit-split case was correct, so
  there is no negative class to rank against (`tests/evals/metrics.py::auroc`
  raises). This is the case design §12.2 says needs agreement-K3 least, and the
  harness routes it straight to the class-level signal. It applies to every
  self-reported row whose accuracy is 1.000 and to haiku `source-context`
  agreement-k3.
- **AUROC, `n/a (constant score)`, and ECE, `n/a (class-level, §12.3)`** for the
  class-level signal: the score is the constant 1.0, so AUROC has nothing to
  rank and there are no distinct bins to calibrate. Design §12.3 gives the
  class-level signal no ECE; its calibration is the threshold-or-nothing choice.
- **Threshold, held-out accuracy above, Wilson LB, abstain share and ECE, `none
  reachable` / `no threshold`**: no score threshold on the fit split clears the
  0.95 accuracy floor, so `derive_threshold` returns `None`. The harness then
  records a Wilson lower bound of 0 and an abstain share of 1.0 (the row is a
  failure, not a measurement of accuracy above a threshold) and computes no
  held-out ECE. `haiku-4-5 names-other self-reported` is the one row with a
  threshold (0.98) that still fails: held-out accuracy above it is 0.000.
- `role-unmatched` on haiku has an `agreement-k3` row with a threshold (0.6667)
  but a Wilson lower bound of 0.8922, below the 0.95 floor, so it fails on
  accuracy rather than on reachability.

### Readings

- **Self-reported confidence is not a usable signal where it matters.** On
  `names-other` the AUROC is 0.323 (haiku) and 0.555 (sonnet), below chance for
  haiku: the judge is most confident on the cases it gets wrong. The
  reliability table shows it directly, with haiku's top two bins (both at score
  0.95) at observed accuracies 0.600 and 0.500. Agreement-K3 does little better
  (AUROC 0.619 haiku, 0.500 sonnet). This is the same finding the
  `names-other` section below reaches from the miss list.
- **ECE would fail the 0.05 bar on self-reported for most sonnet rows**
  (0.068 to 0.129), because sonnet reports scores of 0.6 to 0.97 on cases it
  gets right every time. That never decided a class here, since those rows have
  an undefined AUROC and the class-level signal is used instead, but it is why a
  future caller should not wire a self-reported score straight to the 0.05 bar.
- **Cost.** Class-level and self-reported cost the same, one call per case: about
  $0.0012 to $0.0017 per case on haiku and $0.0042 to $0.0047 on sonnet.
  Agreement-K3 is roughly three times that again, up to $0.0134 per case for
  sonnet `names-other`, which is why it only ran where self-reported gave a reason.

## Token spend

| model | input | output |
| --- | --- | --- |
| claude-haiku-4-5-20251001 | 1,511,812 | 208,340 |
| claude-sonnet-5 | 1,149,644 | 85,870 |
| **total** | **2,661,456** | **294,210** |

Priced with `TokenUsage.estimated_cost_usd` at the rate table in
`src/athenaeum/models.py` (haiku-4-5 $1 / $5, sonnet-5 $3 / $15 per million
input / output tokens, the standard rates recorded there), that is about $2.55
for haiku and $4.74 for sonnet, **$7.29** in all, or about $0.0025 per API
call across the 2,896 calls run (656 self-reported and 792 agreement-K3 per model). No cache creation or cache reads. Inside the 2.5M to 4M estimate from
athenaeum#2343 and well under the 6.5M ceiling.

## names-other failed

`names-other` does not clear the bar on either model. With no confidence
threshold reachable on the fit split, the entry records `threshold: 0.0`,
`abstain_share: 1.0` and a Wilson lower bound of 0, with `passes: false` and
`measured: true`. This is a genuine measured failure, not the unmeasured stub.

Operationally, in `auto` mode that class stays in observe: it has no
recommended model (`recommended_model_for_class` returns `None` for a
non-passing entry) and no usable threshold, so every `names-other` item goes to
a human. That is the safe direction, and it is also the cost: the abstain share
for this class is effectively 100%, far above the 0.30 the bar allows. Three of the other four
classes (`held-elsewhere`, `non-person-target`, `role-unmatched`) are eligible
for enforcement subject to athenaeum#2220's own gates.

`source-context` is recorded as passing on the `agreement-k3` signal, but the
runtime makes one call per suspect and has no caller that computes a K=3
agreement score. `calibrated_probability` therefore treats an `agreement-k3`
entry as no map for a single self-reported score and returns 0.0, so every
`source-context` item goes to a human until a caller supplies an agreement
score. Wiring K=3 into the escalation changes the production call count and
cost, so it is a separate decision.

### The failing cases

Self-reported signal, all 44 cases per scenario per model (the held-out split
alone: haiku 42 of 53 correct, sonnet 44 of 53):

| scenario | haiku pass | sonnet pass | misses (expected -> observed) |
| --- | --- | --- | --- |
| correct_unusual_address | 44 / 44 | 44 / 44 | none |
| surname_initial_mismatch | 37 / 44 | 43 / 44 | haiku 7 x alias -> own; sonnet 1 x alias -> own |
| two_surnames_former_name | 26 / 44 | 24 / 44 | haiku 10 x alias -> own, 6 x same-entity -> own, 2 x same-entity -> alias; sonnet 10 x alias -> own, 10 x same-entity -> alias |

The whole failure sits in `two_surnames_former_name`, and in one direction: the
judge reads an address built from a former or alternate surname as the contact's
own address (`alias -> own`), or splits `same-entity` against `alias`. Both
models fail it at about the same rate (roughly 45%), so it is not a haiku-versus-
sonnet capability gap, and a larger model does not fix it. The agreement-K3 signal fails 22 of 132 cases on each model, close to the
self-reported count, which points at consistent error rather than noise, and
suggests no confidence signal separates right from wrong here.

Scope for a follow-up: the `alias` / `same-entity` / `own` boundary for
former-name addresses is either under-specified in the judge prompt or
inconsistently labelled in the dataset (the same ambiguity athenaeum#2341 fixed
for the former-name cases once already). Deciding which is a new issue; the
prompt and dataset are out of scope for athenaeum#2260, and changing either
changes the shas this record is pinned to. `role-unmatched` shows a smaller
form of the same `alias -> own` miss on haiku (12 of 264 results).

## Recorded fixtures

The `calibration-recorded-fixtures` artifact was not committed.
`tests/evals/README.md` directs only the `calibration-results` record to be
committed ("commit the record in a follow-up PR"); the raw responses are for
offline replay from the run's own artifact, which has 30-day retention.
