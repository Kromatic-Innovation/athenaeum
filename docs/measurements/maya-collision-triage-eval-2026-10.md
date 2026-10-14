# Maya as an ordering signal for the name-collision queue — 2026-10

Issue athenaeum#2066, following the Vitruvius survey comment on
athenaeum#2049 (name-collision section): of every other yes/no-shaped
decision surveyed, name-collision triage was the one candidate with real
queue volume (about 200 ambiguous collisions queued per nightly run) and an
existing labelled source — the resolved blocks already sitting in
`_pending_merges.md` / `_pending_merges_archive.md`. This is that eval,
read against the live ledger at the time this lane ran (2026-10-14).

**Scope: ordering only. Maya never authorises a merge.** The same survey's
identity-confirmation entry is explicit about why: a wrong merge welds two
subjects' histories together, and `athenaeum.dimensions.compare_identity`
deliberately carries no confidence parameter for exactly that reason. This
eval measures whether Maya's P(yes) would be a good PRIORITY key for a
human reviewer's existing queue — never a replacement for the reviewer's
decision.

## Headline result: inconclusive — the resolved population is empty

| metric | value |
|---|---|
| AUROC | n/a (no usable labelled pairs) |
| precision@k (any k) | n/a (no usable labelled pairs) |

**Why.** Reading both ledger files
(`athenaeum.pending_merges.parse_pending_merges`) and filtering to blocks
whose rationale carries the name-collision scan's marker
(`athenaeum#1170 nightly name-collision scan`):

| count | value |
|---|---|
| total pending-merge blocks read (both files) | 1401 |
| name-collision-marked blocks | 357 |
| of those, resolved by a human (not auto-applied) | 2 |
| resolved blocks with exactly 2 sources | 2 |
| resolved blocks' source pages still present on disk | 0 |
| usable fixture rows written | 0 |
| — same-person (label=True) | 0 |
| — different-person (label=False) | 0 |

The 2 resolved blocks are two different proposals for the SAME underlying
collision (one per candidate canonical page), both `reject`, both naming
the same unordered source pair — one human judgment, recorded twice. Both
source pages have since been retired from the live wiki (the files no
longer exist at the paths the ledger recorded), so the fixture builder
(`scripts/build_collision_triage_fixtures.py`) correctly excludes them
rather than fabricating text for a page that is gone. Net usable
population: **0 pairs, 0 positive, 0 negative.**

This is not a bug in the eval harness — it is the honest count. 355 of the
357 name-collision blocks are still unresolved (queued), and the two that
were resolved happen to point at pages that no longer exist. **Rebuild the
fixture set and re-run once more collisions have been resolved by a human**
(`scripts/build_collision_triage_fixtures.py --wiki-root ~/knowledge/wiki
--output <host-side path>`, reported class counts only).

## What was still measured: latency and the model path

With 0 real-ledger pairs, there was nothing to score for AUROC or
precision@k. The real Maya adapter was still exercised end to end, against
the real local weights, to confirm the integration itself works and to get
a latency reading (two synthetic, non-corpus pairs — never logged or
written to disk):

| metric | value |
|---|---|
| Maya weights | local directory, `maya_config.json` present |
| Temperature (`athenaeum._yesno_worker.read_temperature`) | 1.65 |
| First call (cold, includes weight materialization) | ~4-6 s |
| Second call (warm) | ~0.2 s |
| Interpreter | a dedicated local venv with `torch`/`transformers` installed |

**Scorer seam.** athenaeum#2063 (the production `YesNoScorer` seam,
`src/athenaeum/yesno.py`) merged during this lane. This eval's own
`MayaAdapter` reuse (via the PII-hazard sibling eval) was updated to apply
the SAME shared, temperature-aware transform
(`athenaeum._yesno_worker.p_yes_from_logits` / `read_temperature`) the
production worker uses, rather than a second hand-rolled one — eval and
production can no longer disagree on the weights' own `maya_config.json`
temperature (1.65).

**Temperature cannot move AUROC or precision@k.** Dividing every logit by
the same positive constant before the yes/no softmax is monotonic in the
resulting P(yes) — it changes how confident the score LOOKS, never the
RELATIVE ORDER of two pairs' scores, and both of this eval's headline
metrics are rank-based. Recorded here for calibration honesty, not because
it could have changed either number above.

## Over-length fraction

Not measurable against the real ledger (0 pairs). Mechanically, the
combined-pair text built by `build_pair_text()` caps each record at 800
characters before concatenation specifically so a long record A cannot
push record B out of Maya's 512-token input window; the `over_length`
flag in `compute_metrics()` reports what fraction of UNTRUNCATED
`(question, record_a + record_b)` pairs would have exceeded that cap. This
number is reported as a counts-only rate (no fixture text) once the
ledger produces a non-empty fixture set.

## Current queue order, for context

`athenaeum.decisions.list_pending_decisions` sorts the unified decision
queue oldest-first by `created_at` — no other sort key exists on this
path. `precision_at_k_current_order()` uses that same order (the fixture
builder preserves ledger order when it writes rows) as the baseline
Maya's ordering would be compared against. No default page size was found
on `list_pending_decisions_page` or its MCP callers, so `k = 10, 25, 50`
are reported together rather than one invented default — moot this run
since even `k=1` exceeds the available population.

## Recommendation

- **Maya never authorises a merge, regardless of this or any future
  reading of this eval.** This is an ordering-only signal for a human
  reviewer's queue; identity confirmation is out of scope per the survey
  that proposed this eval.
- **Do not wire Maya into the name-collision queue yet.** The resolved
  population needed to validate it as an ordering signal does not exist
  today (0 usable labelled pairs). Revisit once a meaningful number of
  ambiguous collisions have been resolved by a human — the harness
  (`scripts/eval_maya_collision_triage.py` +
  `scripts/build_collision_triage_fixtures.py`) is ready to re-run the
  moment that population exists.
- If a future run DOES produce a usable population, read AUROC and
  precision@k alongside the over-length fraction before recommending
  P(yes)-ordering for the live queue — a non-trivial over-length fraction
  would mean a meaningful share of pairs are scored on truncated evidence.
