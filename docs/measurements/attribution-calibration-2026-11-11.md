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

## Token spend

| model | input | output |
| --- | --- | --- |
| claude-haiku-4-5-20251001 | 1,511,812 | 208,340 |
| claude-sonnet-5 | 1,149,644 | 85,870 |
| **total** | **2,661,456** | **294,210** |

No cache creation or cache reads. Inside the 2.5M to 4M estimate from
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
