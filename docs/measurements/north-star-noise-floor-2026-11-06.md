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

<!-- PROVENANCE TABLE -->

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

<!-- RUN DESIGN -->

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

<!-- VARIANCE -->

## The noise floor

<!-- FLOOR -->

## Counter-example: `oracle` scored below the arm it caps

The epic recorded, by hand, that pooling each arm's correctness across all
classes gives about 0.76 for `oracle` and about 0.84 for `push_breadcrumb_pull`
— a ceiling arm below the arm it should cap, which is the observation that
motivated measuring the floor before any comparison.

<!-- ORACLE -->

## Positive control: does `oracle` answer the new probes?

<!-- POSITIVE CONTROL -->

## Is the probe count high enough for the epic's question?

<!-- REQUIRED N -->

## A floor for the higher-tier model

<!-- HIGHER TIER -->

## Spend

Every dispatch is listed, including failed and partial ones, with its
pre-flight estimate and its actual spend. Where a run does not report an
actual, its pre-flight estimate is counted, per this issue's own rule.

<!-- SPEND -->

## Limits

<!-- LIMITS -->
