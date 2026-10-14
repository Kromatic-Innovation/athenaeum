# Maya and Jev PII-hazard eval (athenaeum#2049, athenaeum#2009)

Evaluates two candidate classifiers for the PII-hazard question "is this
string a way to contact a specific human?" against the SAME 822-row
fixture set, so the comparison is direct rather than drawn from two
separate samples:

- **Maya** (athenaeum#2049): a local 435M-parameter yes/no cross-encoder
  (`VishalMysore/maya` on Hugging Face). No network egress — weights run
  entirely on this machine.
- **Jev** (athenaeum#2009): TypeSafe AI's hosted Noul (yes/no) question
  type, called over `https://api.typesafe.ai/v1/systemone` via
  `athenaeum.decision_provider.JevDecisionBackend`. Real network egress —
  every fixture string crosses the network to a third party.

The regex gate in `athenaeum.pii` / `athenaeum.sensitivity` stays the live
gate regardless of this eval's outcome; this is a measurement, not a
deployed change.

## Unredacted, by design — local for Maya, real egress for Jev

Both classifiers receive the fixture string exactly as typed, with no
redaction first — redacting a contact-shaped string before asking "is this
a way to contact a specific human?" would erase the exact signal the
question is about. This is deliberate per both issues' acceptance
criteria, not an oversight.

- **Maya:** that unredacted string never leaves this machine — Maya's
  weights run entirely locally, so there is no network egress for the
  fixture to travel over.
- **Jev:** that unredacted string DOES leave this machine. All 822
  fixtures — the 275 real contact-shaped positives and the 547 adjudicated
  allowlist negatives alike — were sent over the network to TypeSafe AI's
  hosted endpoint, unredacted, exactly as the issue's acceptance criteria
  specify. This is real egress of corpus-derived values to a third-party
  API, a materially different exposure than Maya's no-egress path above.

## Licence note

Maya is Apache 2.0, but its base model's training data reportedly includes
non-commercial datasets. This eval is research use. Production adoption of
Maya as a live classifier needs a licence check recorded on athenaeum#2049
first — this measurement does not constitute that check.

## How to run it (host-side only)

```sh
# Maya (no-egress, local weights)
export ATHENAEUM_MAYA_WEIGHTS_PATH=/path/to/local/maya/weights
pip install -e ".[maya-eval]"

python scripts/eval_maya_pii_hazard.py \
    --fixtures /path/to/host-side/positives.jsonl \
    --allowlist ~/knowledge/wiki/_pii-allowlist.yml \
    --output /path/to/scratch/maya-raw.md
```

```sh
# Jev (hosted, real egress and real spend -- see "Unredacted" above)
JEV_API_KEY="$(op read 'op://Agent Tools/Jev Athenaeum Key/credential')" \
    python scripts/eval_jev_pii_hazard.py \
    --fixtures /path/to/host-side/positives.jsonl \
    --allowlist ~/knowledge/wiki/_pii-allowlist.yml \
    --output /path/to/scratch/jev-raw.json \
    --abstain-low 0.3 --abstain-high 0.7 \
    --max-spend-usd 5
```

- `--fixtures` takes a JSON-Lines file of `{"text": ..., "label": true|false}`
  rows (true = a way to contact a specific human) — host-side only, never
  committed to this public repo.
- `--allowlist` reads the adjudicated PII allowlist through the sanctioned
  loader (`athenaeum.pii.load_pii_allowlist`); each exact-`value` entry
  becomes a `label=false` row (a human has already ruled it is not a way to
  contact a specific human).
- Both scripts' `--output` is overwritten in place — point it at a scratch
  path, never at this doc; the tables below were filled in by hand from
  each script's output, not generated directly into this file.
- `eval_jev_pii_hazard.py` never reads `JEV_API_KEY` from
  `~/.cache/athenaeum/config.env` (that file is periodically rewritten/wiped
  by other tooling) — fetch the key fresh from 1Password into the shell
  environment for the duration of the run, per the key's own handling rules.

## Fixture provenance (counts only — no fixture content in this repo)

- **Positives (275, label=true):** real contact-shaped values pulled through
  the sanctioned `athenaeum entity --uid <uid> --include-excluded` CLI path
  (the same path `recall --with-pii` uses), sampled from 251 of 16,923
  enumerated `person` entities within a 30-minute host-side time budget.
  270 emails, 5 phones. 62 of the 251 sampled entities had no contact record
  and were skipped. This is a small, randomly-sampled slice of the person
  corpus, not a census — read the metrics below as directional for this
  corpus's contact-value shapes, not a definitive bound.
- **Negatives (547, label=false):** every exact-`value` entry in the live
  adjudicated PII allowlist (`~/knowledge/wiki/_pii-allowlist.yml`), loaded
  through `athenaeum.pii.load_pii_allowlist`. The allowlist held 0 pattern
  (shape-only) entries at measurement time, so none were skipped. Of the 547
  exact values, roughly 500 are digit-shaped (ticket IDs, epoch timestamps,
  numeric slugs — already-adjudicated hard negatives) and 46 are
  email-shaped service/placeholder addresses; this comfortably covers the
  50–100 hard-negative target without hand-picking a subset.
- Fixture files exist only under this host's scratch directory and were
  never committed or inlined into this PR, per the acceptance criteria.

## Results

All three columns below score the SAME 822-row fixture set (275 positive /
547 negative). Maya: CPU inference, `maya-eval` extra installed in a
dedicated venv, two runs. Jev: one live run against the hosted endpoint,
metrics for both the default threshold and the abstention band computed
from that single run's recorded per-row `p_yes` (no second network pass —
see "Jev run notes" below).

### Run 1 — default threshold (0.5)

| metric | Maya | Jev | Regex gate |
|---|---|---|---|
| scored rows | 822 / 822 | 822 / 822 | 822 / 822 |
| accuracy | 0.951 | 0.870 | 0.824 |
| precision | 0.904 | 0.732 | 0.659 |
| recall | 0.956 | 0.964 | 0.982 |
| TP / FP / TN / FN | 263 / 28 / 519 / 12 | 265 / 97 / 450 / 10 | 270 / 140 / 407 / 5 |
| Brier score | 0.0897 | 0.0901 | n/a |
| mean latency (s/row) | 0.1296 | 0.4319 | 0.0000 |
| median latency (s/row) | n/a | 0.4220 | 0.0000 |
| p95 latency (s/row) | 0.1421 | 0.4764 | 0.0001 |
| wall clock (822 rows) | 112 s | ~355 s | (included above) |

### Run 2 — abstention band [0.3, 0.7]

181 of 822 Maya rows (22.0%) and 171 of 822 Jev rows (20.8%) fell inside the
abstention band and were excluded from the confusion counts below (they
would route to a fallback/manual path in a deployed gate rather than
receive an automatic verdict). The two models abstain on different rows
(different score distributions), so these are each model's own
scored-subset, not a shared subset:

| metric | Maya (scored rows only) | Jev (scored rows only) | Regex gate |
|---|---|---|---|
| scored rows | 641 / 822 | 651 / 822 | 822 / 822 |
| accuracy | 0.970 | 0.954 | 0.824 |
| precision | 0.929 | 0.922 | 0.659 |
| recall | 0.996 | 0.970 | 0.982 |
| TP / FP / TN / FN | 236 / 18 / 386 / 1 | 260 / 22 / 361 / 8 | 270 / 140 / 407 / 5 |
| Brier score (all 822 rows, not band-filtered)\* | 0.0897 | n/a | n/a |
| Brier score, scored rows only (`brier_scored`) | n/a | 0.0527 | n/a |
| Brier score, all rows (`brier_all`) | n/a | 0.0901 | n/a |
| mean latency (s/row) | 0.1293 | 0.4319 | 0.0000 |
| p95 latency (s/row) | 0.1426 | 0.4764 | 0.0001 |
| wall clock (822 rows) | 110 s | ~355 s | (included above) |

\* This figure predates the athenaeum#2052 fix. At the time of this run,
`compute_metrics` did not exclude abstained rows from the Brier calculation,
unlike the confusion-matrix metrics above it, so this number is identical to
Run 1's `brier_all` and was never a scored-rows-only figure. The script now
reports `brier_scored` (scored rows only) and `brier_all` (every row)
separately when an abstention band is active; no rerun was performed for
athenaeum#2052 on the Maya side, so no scored-rows-only Brier score exists
for Maya on this fixture set. Jev's run (this PR) used the current script
and reports both figures above.

### Jev run notes

- **Egress and spend are real.** This was ONE live pass over all 822
  fixtures against `https://api.typesafe.ai/v1/systemone`
  (`athenaeum.decision_provider.JevDecisionBackend`, Noul question type),
  each fixture sent unredacted (see "Unredacted" above). 0 of 822 rows
  errored; 0 retries were needed.
- **Tokens and cost:** 236,464 total input tokens, 18,906 total output
  tokens (output is free per the 2026-10-10 athenaeum#2009 spend
  disposition), for an estimated cost of **USD 0.0099** — call it one cent
  — against the disposition's USD 5 lane cap (and the USD 20 total shared
  across athenaeum#1998/#2008/#2009).
- **Latency was measured over a slow, high-latency link** (the operator
  was on a hotel connection abroad at measurement time), with a fresh
  per-call HTTP connection (`urllib`, no keep-alive/connection pooling) —
  mean 432 ms, median 422 ms, p95 476 ms per call, tightly clustered (no
  long tail), consistent with network RTT dominating over Jev's own
  processing time. A faster/closer link would likely lower these figures;
  they should not be read as Jev's floor latency.
- Both Run 1 and Run 2 above are computed from the SAME single live run's
  per-row `p_yes` values (one network pass, two threshold/abstention
  policies applied after the fact) — nothing here doubles Jev's egress or
  spend.

## Recommendation

**Candidate for the hazard gate**, conditional on the licence precondition
above being resolved first (Apache 2.0 code licence, but base-model training
data reportedly includes non-commercial sets — needs an explicit legal/licence
check recorded on athenaeum#2049 before any production adoption).

Supporting observations:

- At the default threshold, Maya cuts the regex gate's false-positive rate by
  roughly 5x (28 vs. 140 on this fixture set) while recall stays comparable
  (0.956 vs. 0.982), i.e. materially higher precision for a similar miss
  rate — exactly the asymmetry a hazard gate wants (fewer good values wrongly
  withheld, without letting materially more real contact values through).
- Adding a [0.3, 0.7] abstention band pushes both precision and recall higher
  on the rows it does score (0.929 / 0.996), at the cost of routing 22% of
  rows to a fallback path — worth sizing against how expensive that fallback
  is before picking a deployed abstention width.
- Per-row latency (~130 ms mean on CPU, no GPU used) is far higher than the
  regex gate's but well within range for a batch/offline gate; a live
  per-request gate would need to confirm this against its own latency budget.
- This measurement draws positives from a 251-entity sample (of 16,923
  person entities) gathered in a bounded time window — a larger or
  differently-sampled positive set could shift these numbers; re-run with a
  larger sample before treating this as final.

Not "not yet" or "inconclusive": the margin over the regex gate is large
enough on this fixture set, and consistent across both runs, to justify
moving forward — contingent on the licence check above.

## Maya vs. Jev vs. regex

All three were measured on the identical 822-row fixture set, so the
comparison below is direct.

- **Accuracy/precision/recall:** at the default threshold, Maya
  (0.951/0.904/0.956) beats both Jev (0.870/0.732/0.964) and the regex gate
  (0.824/0.659/0.982) on accuracy and precision; Jev's recall (0.964) sits
  between Maya's (0.956) and the regex gate's (0.982), with its precision
  closer to the regex gate's than to Maya's. With the [0.3, 0.7] abstention
  band applied, the gap narrows but does not close: Maya 0.970/0.929/0.996
  vs. Jev 0.954/0.922/0.970 on each model's own scored subset. On this
  fixture set, Maya is the stronger classifier at both operating points.
- **Latency:** Maya (~130 ms mean, CPU, no network) is roughly 3x faster
  than Jev (~432 ms mean, real network round trip over a slow link) and
  both are far slower than the regex gate's effectively-zero latency. Jev's
  latency is also link-dependent in a way Maya's is not — see "Jev run
  notes" above.
- **Cost:** the regex gate and Maya cost nothing per call (Maya's cost is
  the one-time weights download plus local compute). Jev cost an estimated
  USD 0.0099 total for all 822 calls — negligible in absolute terms, but a
  real per-call charge that scales with volume, unlike the other two.
- **Egress:** the regex gate and Maya never send the fixture string
  anywhere. Jev sends every fixture string — contact-shaped positives and
  adjudicated-negative values alike — unredacted to a third-party API. This
  is the one axis where Jev is categorically worse than Maya, independent
  of any accuracy numbers: a live deployment choosing Jev over Maya is
  trading egress for whatever operational benefit a hosted, maintained
  model offers over a locally-hosted one.
- **Net read:** Jev does not clear a bar that would justify its egress and
  latency cost over Maya on this fixture set — Maya is both more accurate
  and has no egress. Jev's numbers are still well clear of the regex gate's
  on precision, so if a deployment specifically could not run local
  inference (no local compute budget, no acceptable licence for Maya's
  base model), Jev would still be an improvement over the regex gate
  alone. This eval's recommendation is unchanged from athenaeum#2049: Maya
  is the stronger candidate, Jev is a viable fallback if local inference is
  unavailable, and the regex gate stays the live gate either way.

## Temperature correction (athenaeum#2063)

The numbers above were measured through this script's own ``p_yes`` method,
which computed a plain two-class softmax over Maya's logits (temperature
1). The Vitruvius design for athenaeum#2063 found that the weights' own
``maya_config.json`` defines ``P(yes)`` at a different temperature (1.65),
and that production's shared transform (now `athenaeum._yesno_worker.p_yes_from_logits`,
imported by this script too — see the "one shared scoring function" note
in the issue's acceptance criteria) must use that declared value, not 1.

**What is unaffected:** accuracy, precision, and recall AT THE 0.5
threshold are monotonic-transform invariant, so the headline numbers above
are unaffected by this correction.

**What is NOT yet re-measured:** the [0.3, 0.7] abstention band, its 22%
abstention rate, the 0.970 scored-row accuracy, and the Brier score above
were all computed at temperature 1 and are **transform-dependent**. No
abstention-band default may be quoted from this document until the 822-row
eval is re-run through the production transform at temperature 1.65. At
the same 0.3 cut, temperature 1.65 is stricter (it clears fewer tokens),
so the expected direction of the error is safe, but that is not itself a
measurement — the re-run is athenaeum#2063's shadow-mode exit criterion,
not a step this document's own measurement run performed.
