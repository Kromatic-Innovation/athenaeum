# Jev vs. current-model eval: `claim_kind` (athenaeum#1998)

Offline comparison of Jev (TypeSafe AI) against athenaeum's current `classify` knob on the `claim_kind` classification task -- the knob this issue's dispatch brief named as the expected highest-volume candidate, pending confirmation against athenaeum#1987's own table (see "Volume confirmation" below). No fixture text, filename, or `~/knowledge` path appears below -- only aggregate counts.

## Method

- Sample: 43 case(s), requested 50, stratified across the six `claim_kind` labels with a fixed seed (`--seed 42`).
- Answer key: memory files under the sample directory that already carry an author-supplied `claim_kind:` in frontmatter, treated as human-labelled per this issue's dispatch framing.
- Both arms see the identical frontmatter-stripped snippet (`athenaeum.claim_kind._snippet`); Arm A strips it internally, Arm B's `state` is built with the same helper.
- Arm A (current model): `athenaeum.claim_kind.classify_claim_kind` unchanged, via `athenaeum.provider.build_llm_client` -- the live classify-knob call path, never `stamp_claim_kind` (no corpus write).
- Arm B (Jev): `JevDecisionBackend.decide(kind="choice")` with the six labels as criteria and instructions paraphrased from `CLAIM_KIND_SYSTEM` (not copied verbatim).
- Call order alternates per case (even index: Anthropic first; odd: Jev first) to spread any one-sided warm-up/ordering effect across both arms. One untimed warm-up call precedes each arm's timed loop.
- Network context: measured from the lane host (Tristan's primary workstation), same session, same link for both arms; no VPN/proxy observed

### Answer-key limitations

- Available labelled pool by kind: fact=9, observation=26, opinion=0, decision=4, policy=4, definition=0.
- Only 43 labelled case(s) were available against a request for 50 -- the sample below is every eligible case, not a 50-case draw from a larger pool.
- Zero labelled cases exist for: opinion, definition. The `claim_kind` prompt's own stated load-bearing distinction (`opinion` must never be overridden by another opinion on authority alone) is UNTESTED by this run wherever `opinion` is among the zero-count kinds.
- Provenance of the "human-labelled" framing is not independently verified here: a file carrying `claim_kind:` in frontmatter is indistinguishable, from file content alone, between a value an authoring session chose at write time and one a prior nightly `stamp_claim_kind` backfill pass wrote onto an originally-unclassified file -- both leave the identical final frontmatter. This run treats every such file as the dispatching issue instructed; a case drawn from the second category would make the Anthropic arm's accuracy on that case partly self-agreement rather than agreement with an independent human judgment.

### Volume confirmation

athenaeum#1987's inventory bundles `claim_kind` inside the `classify` knob's `librarian` run_type rows and marks per-knob volume **not separable** from that table -- it does not itself state a `claim_kind`-specific call count. The dispatching issue named `claim_kind` as the *expected* highest-volume knob pending this confirmation; this run does not independently establish that ranking and reports it as unconfirmed from #1987's table, not as verified.

## Results

- Majority-class baseline: `observation` is 0.605 of the sample -- a classifier scoring at or below this rate is not beating a single-label guess.
- Agreement between arms (both non-empty, equal): 0.698

### Arm A: current model (classify knob)

| metric | value |
|---|---|
| accuracy | 0.814 |
| unclassified (`""`) | 0 / 43 |
| mean latency (s) | 0.8259 |
| median latency (s) | 0.6854 |
| p95 latency (s) | 1.3837 |
| min / max latency (s) | 0.5956 / 1.4508 |
| input / output tokens | 28597 / 1148 |
| estimated cost (USD, configured model price) | 0.03434 |

Confusion matrix (rows: true label, cols: prediction):

| true \ pred | fact | observation | opinion | decision | policy | definition | (unclassified) |
|---|---|---|---|---|---|---|---|
| fact | 2 | 5 | 0 | 0 | 2 | 0 | 0 |
| observation | 0 | 25 | 0 | 1 | 0 | 0 | 0 |
| opinion | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| decision | 0 | 0 | 0 | 4 | 0 | 0 | 0 |
| policy | 0 | 0 | 0 | 0 | 4 | 0 | 0 |
| definition | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

### Arm B: Jev (choice)

| metric | value |
|---|---|
| accuracy | 0.581 |
| unclassified / out-of-vocabulary | 0 / 43 |
| mean latency (s) | 0.4293 |
| median latency (s) | 0.4235 |
| p95 latency (s) | 0.4645 |
| min / max latency (s) | 0.4002 / 0.5637 |
| input / output tokens | 29991 / 2749 |
| estimated cost (USD, $0.042/MTok input, output free) | 0.00126 |

Confusion matrix (rows: true label, cols: prediction):

| true \ pred | fact | observation | opinion | decision | policy | definition | (unclassified) |
|---|---|---|---|---|---|---|---|
| fact | 3 | 4 | 0 | 0 | 2 | 0 | 0 |
| observation | 4 | 16 | 0 | 1 | 5 | 0 | 0 |
| opinion | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| decision | 0 | 0 | 0 | 2 | 2 | 0 | 0 |
| policy | 0 | 0 | 0 | 0 | 4 | 0 | 0 |
| definition | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## Recommendation: keep `claim_kind` on the current model

On this 43-case sample, Jev is materially less accurate than the current
model (0.581 vs. 0.814 — a 23-point gap, against a 0.605 majority-class
baseline that Jev barely clears and the current model clears by a wide
margin) and the two arms agree only 69.8% of the time. Jev is faster
(median 0.42s vs. 0.69s) and far cheaper (`$0.00126` vs. `$0.03434` for this
sample — both negligible in absolute terms at `claim_kind`'s per-call size),
but per athenaeum#1987's own framing, a cheaper or faster wrong answer is not
a saving. **Recommendation: keep the current model on `claim_kind`.** This
is not inconclusive — the accuracy gap is large relative to the sample size
and shows up broadly across both represented majority kinds (`fact`,
`observation`), not in one noisy cell — but it comes with two scope caveats
stated above and repeated here because they bound how far the recommendation
generalizes: (1) `opinion` and `definition` have zero cases in this answer
key, so the prompt's own stated load-bearing distinction is untested in
either direction; (2) athenaeum#1987 could not separate `claim_kind`'s volume
from the bundled `classify`/`librarian` figures, so this result answers "is
Jev accurate enough on this knob" without independently confirming the
dispatching issue's volume premise for *why* this knob was chosen first.
Read together with #1987: that issue already recommended **keep** for the
bundled `classify`/`write` core pending call-site-level cost instrumentation;
this eval's accuracy result reinforces the same keep recommendation from the
quality side rather than cutting against it.

### Note: a live API shape gap found while building this eval

The live Jev endpoint rejects a `choice` question's `criteria` as a list
(`HTTP 422`, `"criteria" must be a valid dictionary`) — it requires a
`{label: description}` mapping. `JevDecisionBackend`'s own byte-pinned unit
test (`tests/test_decision_provider.py::test_build_payload_choice_pinned`,
from athenaeum#2054) pins only the list-of-ids form, which this run found
does not work against the live endpoint for `choice`. This script sends a
dict and works around the gap at the call site; the adapter and its test
are unchanged here — fixing `JevDecisionBackend`'s own shape handling (or
its test coverage) is outside this issue's scope and is reported upward
rather than patched in this PR.
