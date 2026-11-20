<!-- SPDX-License-Identifier: Apache-2.0 -->

# Push behaviour for self-contained programmatic prompts

**Status:** DESIGN RECORD for athenaeum#2915 (epic athenaeum#2295). The build
slices are at the end. **Chosen rule: Declared Skip, then a measured floor.**

---

## 1. The problem, in numbers

[`../measurements/irrelevant-push-causes-2026-11-19.md`](../measurements/irrelevant-push-causes-2026-11-19.md)
(athenaeum#2804) attributes every irrelevant pushed page to one cause. For the
headless population (772 pages, 119 sessions, all templated routines):

- **41.6% (cause e)**: the turn needed no memory. The prompt carried the whole
  input.
- **49.2% (cause a)**: a relevant page existed and was not delivered, but every
  one of those pages was graded only "helpful", never "required". Counting
  "helpful only" as not a miss, about 91% of headless misses read as "the turn
  needed no memory".
- **7.4% (cause f)**: filler beside an already-delivered relevant page.

That is a precision fault: the push runs at all, or runs with no relevance bar.
Ranking is not the fault, and a missing page explains 1.8%.

## 2. What the code does today

- The per-turn hook is `claude_code_adapter.main`. It parses only `prompt` and
  `session_id` from the hook payload (`claude_code_adapter.py:533-547`). The
  payload carries nothing that says "this run is unattended".
- The one off-switch is `AUTO_RECALL`, read from the process environment
  (`claude_code_adapter.py:430`, gate at `:803`). A launcher environment value
  beats the cached config (`_load_config_env`). It sits after the witness call
  (`:794`), so turning it off does not stop attestation. When it is off the hook
  returns before `build_context_for_turn`, so **nothing is written to the push
  ledger**. A skipped run is indistinguishable from a run that never had the
  hook.
- Retrieval is `context.build_context`. FTS5 returns the top `CANDIDATE_WINDOW`
  rows by rank (`context.py:865`), they are merged and cut to a count with
  `_apply_cap` (`:890`, `:903`). **Nothing between them asks whether a
  candidate is good enough.** Whatever matched the extracted terms is pushed,
  up to the ceiling of 7 (`config.RECALL_CAP_CEILING_DEFAULT`).
- **A relevance score exists at push time.** `Candidate.relevance`
  (`context.py:297`) holds the FTS5 BM25 rank (`:459`). Vector candidates carry
  `None` (`:530`) and the distance is discarded (`:516`, `_score`).
- **A floor already exists, and the push path never consults it.**
  `config.resolve_recall_relevance_floor(config, backend, unprompted=True)`
  (`config.py:3806`) resolves a per-backend, absolute floor from
  `recall.relevance_floor.push.<backend>` or `ATHENAEUM_RECALL_PUSH_MIN_SCORE_*`.
  `search.meets_relevance_floor` (`search.py:3161`) compares it in each
  backend's own direction. Only the MCP `recall_search` path applies it
  (`mcp_server.py:2115-2122`). `context.py` has no reference to either.
  athenaeum#1492 shipped the mechanism and deliberately left it off; it chose
  no value, and it left open whether the push should be stricter.

## 3. The four questions

### 3.1 What is the signal?

**The caller declares it.** Options considered:

| Option | Verdict |
| --- | --- |
| Caller declares it (environment value set by the routine's launcher) | **Chosen.** Host-neutral: no engine code names a host. The party that knows a run is self-contained is the one that wrote the prompt. Already wired: `AUTO_RECALL`. |
| Adapter reports "non-interactive" | Rejected as the engine's signal. The hook payload has no such field (`:533-547`). The only headless marker the project reads is the transcript `entrypoint` field, which is a launch-surface proxy for a measurement after the fact (`follow_through.py:108-113`), not something the hook sees at push time. A host adapter may map its own marker onto the declared signal, but that mapping is host-specific and outside this design. |
| Engine infers from the prompt | Rejected. It would need a classifier on the hot path (3 s budget) and a wrong "self-contained" verdict silently drops a push that was needed. The inferred signal also cannot be audited from the ledger, which stores only a query hash. |

### 3.2 Skip, or floor?

**Both, for different causes, in this order.**

- **Skip for cause e and the optional share of cause a.** A floor cannot remove
  a push the turn never needed. A templated prompt names its subject (a
  classifier, an extractor, a planner), so its terms match pages lexically and
  those pages score like any real hit. The only floors that would reject them
  are floors that also reject real recall. The committed sweep
  (`../measurements/recall-relevance-floor-sweep.md`) shows that shape: on the
  synthetic corpus, the FTS5 floors that make the abstention probes clean
  (-20, 3/3) also take every other probe class to MRR 0.000, and the floors
  that keep recall (-5 to 0) leave abstention at 0/3. That sweep is a synthetic
  corpus and a different question, so it is only a warning that one global
  number is unlikely to separate the two, not a measurement of this problem.
- **Floor for cause f (6.5% interactive, 7.4% headless) and any low-score tail
  in both populations.** Filler beside a delivered page is exactly "a slot
  filled with no bar". It is population-neutral, so it helps interactive
  sessions too, as the issue observes.

Skip is the exact fix for the dominant headless share but only where the caller
says so. The floor is the general fix but it is only as good as its calibration,
and BM25 rank is not comparable across corpora or term counts (the sweep's own
FTS5 and keyword columns use different scales and directions). So the floor is
specified as a **measured, opt-in mechanism whose default is decided by a gate**,
not by this note.

### 3.3 What is the default?

- **Skip: opt-in per run, default off.** `AUTO_RECALL` stays `true` unless the
  routine's launcher sets it. Nothing changes for any existing caller, and the
  explicit `recall` tool is unaffected, so a routine that does want memory can
  still ask for it. A routine that is later found to benefit simply stops
  setting the value.
- **Floor: default inactive (`None`), exactly as athenaeum#1492 ships it.** The
  default flips only if the gate in section 4 passes on the gold set. Until then
  an operator opts in by setting the existing `recall.relevance_floor.push.*`
  key. The same key, removed, reverts the behaviour.

Both are reversible by deleting one setting, and both reuse settings that
already exist.

### 3.4 How is it measured?

Against the same instruments as athenaeum#2804, so before and after compare:

- **Same gold set and metrics** (`recall_metrics`, `recall-metrics.md`), same
  cause labels (a to f), same population split from the transcript `entrypoint`
  field.
- **Primary gates (headless skip):** required-page coverage must not fall
  (headless has 0 required pages, so any required page lost is a hard fail),
  and the pushed-irrelevant share and pushed tokens should fall.
- **Primary gates (floor):** across both populations, the floor must lose zero
  gold-"required" pages that the unfloored push delivered, and it must cut
  pushed-irrelevant pages. The floor value is chosen from a sweep over the real
  gold set, not from the synthetic corpus.
- **The denominator problem.** A skipped turn writes no push row today (section
  2), so skip would raise "coverage miss" by deleting rows from the base.
  Measurement therefore needs a **skip record with a reason**, so skipped turns
  are counted as skipped and not as unobserved. athenaeum#2914 (pseudo-prompt
  skip) needs the same thing; this design reuses whatever record shape that
  issue lands and does not define a second one.
- **Separating extraction failure from ranking.** The floor's effect cannot be
  read cleanly while the extracted terms are unlogged (the measurement's own
  limit: cause a is an upper bound on ranking miss). That depends on
  athenaeum#2916 (logging query terms). The floor slice is gated on it.
- **Evidence limits carried forward.** All labels are model labels with the
  operator spot-check pending, and the evidence is one deployment and one
  window. The gate result is reported with the same caveat.

## 4. Chosen rule

> **Declared Skip, then a measured floor.**
> 1. A caller that knows a run is self-contained declares it by setting
>    `AUTO_RECALL=false` for that run. The hook then does not push, and records
>    one skip row with reason `declared` so the run stays in the denominator.
> 2. The push path applies the existing `unprompted` relevance floor
>    (athenaeum#1492) to candidates before the count cap, default inactive. It
>    becomes default-on, per backend, only if it clears the gold-set gate in
>    3.4.

Why this and not "floor only" or "infer and skip by default": the floor cannot
remove cause e without removing real recall (3.2), and inference cannot be
audited (3.1). Why not "skip every headless push": headless is one host's
property, and a routine elsewhere may need its pushes (skipping also drops the
"helpful" pages that did arrive), so the choice stays with the caller.

Two details the build must respect:

- **Below-floor candidates are dropped, not tallied** into the overflow
  breadcrumb. The breadcrumb says "N more matching results were withheld by the
  relevance cap" (`context.py:44-51`). A page below the floor is not a relevant
  result and must not be advertised as one.
- **The floor applies to the push only.** `build_context` also serves the
  static-refresh adapter and `athenaeum context`, so the floor is passed in
  explicitly by the per-turn path and defaults to none for every other caller.
  Selection stays relevance-alone (athenaeum#1345): the floor removes
  candidates, it never reorders or scores them.

## 5. Build slices

Dependency order: **S1 and S2 are independent; S3 needs athenaeum#2916 and S2;
S4 needs S3.**

**S1. Record a skip row when the per-turn push is declared off**

Depends on: athenaeum#2914 (reuse its skip-record shape; if it lands first,
this is a one-reason addition).

- With `AUTO_RECALL=false`, `claude_code_adapter.main` still skips the push and
  still runs the witness call, and now writes one push-ledger skip row with
  reason `declared` and no page ids.
- A skip row carries no prompt text, query text or page names, only the reason,
  session id and timestamp.
- `athenaeum push-metrics` counts skipped turns in a separate figure, and a
  session with only skip rows is not reported as "no push".
- A negative test: with `AUTO_RECALL` unset, no skip row is written.

**S2. Document the per-run skip for scheduled and scripted runs**

Depends on: none.

- `docs/reference/environment.md` and `docs/guides/sidecar.md` state that a
  routine's launcher sets `AUTO_RECALL=false` for a run whose prompt carries all
  its input, and that the explicit `recall` tool still works.
- The text says the setting is per process and a launcher value beats the cached
  config, matching `_load_config_env`.
- No issue numbers in the guide text.

**S3. Apply the push-path relevance floor in `build_context`**

Depends on: S2 (settings documented), athenaeum#2916 (query terms logged, so the
effect is measurable).

- `build_context_for_turn` resolves `resolve_recall_relevance_floor(config,
  backend, unprompted=True)` and drops candidates that fail
  `meets_relevance_floor` before `_apply_cap`. With no floor set the envelope is
  byte-identical to today's, proven by a test.
- Vector candidates keep their distance internally (not in the envelope) so the
  vector floor can be applied. FTS5 and vector use their own direction.
- Dropped candidates are not counted in the overflow breadcrumb.
- A counter-example test: a candidate exactly at the floor is kept, one past it
  is dropped. The static-refresh caller is unaffected.

**S4. Measure the floor against the gold set and decide the default**

Depends on: S3, athenaeum#2916.

- A sweep over the real gold set reports, per backend and per population,
  required-page coverage, pushed-irrelevant share by cause, and pushed tokens at
  each floor, with session-cluster intervals.
- The report states the floor that loses zero gold-"required" pages, or that no
  such floor exists.
- If one exists, the default flips with that value as a one-line change and the
  report is committed under `docs/measurements/`. If none exists, the floor stays
  opt-in and this is recorded.
- Labels are marked as model labels, with the operator spot-check named as
  pending.
