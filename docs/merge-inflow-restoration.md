# Merge-inflow restoration: a `max_merge_sources` proposal

Analysis for athenaeum#1030 (blocks athenaeum#787), read against the athenaeum#784
baseline in [`reasoning-tier-measurements.md`](reasoning-tier-measurements.md)
and the suppression code path. **This document changes no default and flips no
config.** The live-config flip belongs to athenaeum#787's arming step, after the
value proposed here is ratified by the operator.

Same evidence discipline as the baseline doc: every claim below cites its
source (file:line, doc section, or issue), and where the recorded data does
not support a figure, that gap is stated plainly rather than filled with an
estimate.

---

## 1. Why inflow is zero today

athenaeum#784 measured **0 merge proposals written on all 9 observed nights**
(2026-08-05 through 2026-08-17), while the `wiki-dedupe` phase logged
**127–132 `SUPPRESSED` events per night** — see
[`reasoning-tier-measurements.md` §"merge proposals generated per
night"](reasoning-tier-measurements.md#ac--merge-proposals-generated-per-night),
lines 74–92. The doc's own framing labels these as over-cluster suppressions
against `max_merge_sources=5`.

The suppression gate itself: `_merge_proposal_suppression_reason` in
`src/athenaeum/merge_type_gate.py:277-329`, called from two sites —
`src/athenaeum/wiki_dedupe.py:469-488` (the wiki-page-cluster path, logged as
`"wiki-page dedup: SUPPRESSED ..."`) and `src/athenaeum/merge.py:2010-2019`
(the raw-source resolver path, logged as `"resolutions: SUPPRESSED ..."`).
Both call sites pass the same four gates, checked **in this fixed,
short-circuiting order** (confirmed by `tests/test_merge_proposal_gates.py`'s
`TestGateOrdering`, lines 360-390, and the docstring at
`merge_type_gate.py:283-291`):

1. **Size cap** — `n_sources > max_merge_sources` (default **5**,
   `config.py:678-720`; resolved via env `ATHENAEUM_MAX_MERGE_SOURCES` > yaml
   `librarian.max_merge_sources` > default). Message:
   `"over-cluster: {n} sources > max_merge_sources={cap}"`
   (`merge_type_gate.py:320-321`).
2. **Complete-linkage / chain gate** — `min_pairwise < cluster_threshold`.
   Largely a dead backstop now: since athenaeum#681, cluster *formation* itself is
   complete-linkage, so "a weak bridging edge can no longer chain
   hundreds/thousands of loosely-related pages into a giant component in the
   first place" (`wiki_dedupe.py:436-450`, comment on the athenaeum#478 call site).
3. **Mean-similarity floor** — `mean_similarity < min_merge_mean_similarity`
   (default **0.6**, active — `config.py`, docs/configuration.md line 66).
4. **Confidence floor** — off by default (`min_merge_confidence=0.0`).

Because the check short-circuits on the first gate that fires, **a cluster
suppressed for size is never also evaluated against the cohesion/confidence
gates**, and vice versa — the recorded reason string names only the first
failing condition, not the cluster's full shape.

## 2. What the recorded data does — and does not — support

**AC1 asks for the over-cluster size distribution: how many candidates/night
would survive at `max_merge_sources = 6, 8, 10, unlimited-with-cap`. That
distribution is NOT derivable from anything committed to this repo, and this
section states that plainly before anything else.**

What is actually recorded, per the "Exact commands" section of
`reasoning-tier-measurements.md` (lines 273-279):

```sh
grep -hE "wiki-page dedup: SUPPRESSED" ~/Library/Logs/pre-dawn-sweep.out.log \
  | awk '{print substr($1,1,10)}' | sort | uniq -c
```

This counts **log lines matching a fixed text prefix, bucketed by calendar
date** — it discards everything after the timestamp except the count. Three
consequences, each a real gap in what AC1 asks for:

1. **No per-cluster `n_sources` was ever recorded.** The suppression message
   text *does* embed `n_sources` when the size gate is the one that fires
   (`"over-cluster: {n} sources > ..."`), but that text was never captured or
   parsed — only the line count was. The raw log line that would let a later
   reader recover it lives at `~/Library/Logs/pre-dawn-sweep.out.log` on the
   operator's host, which this analysis has no access to (out of bounds per
   this issue's own scope, and this lane has no host filesystem access in any
   case).
2. **The 127–132 figure is not filtered to size-gate suppressions
   specifically.** The grep matches *any* `SUPPRESSED` reason — size,
   cohesion, or (if ever configured) confidence — under the same log prefix.
   Given the short-circuit ordering in §1, a candidate in that count could be
   suppressed for size alone, or for size *and* would-also-fail-cohesion; the
   recorded aggregate cannot distinguish the two. `reasoning-tier-
   measurements.md`'s own characterization ("suppressed ... as degenerate
   over-clusters") is the doc author's interpretation, not something the
   shown command isolates.
3. **The figure covers one call site only.** The grep pattern
   `"wiki-page dedup: SUPPRESSED"` matches only `wiki_dedupe.py`'s log line.
   `merge.py`'s resolver path (`"resolutions: SUPPRESSED ..."`,
   `merge.py:2018-2019`) runs the identical gate over raw-source clusters and
   was not counted by this command. The true nightly suppression volume
   under `max_merge_sources` — across both call sites — is unmeasured; only
   the wiki-page-cluster slice was captured.

**Bottom line for AC1:** no committed artifact lets this analysis produce a
survivor count at 6, 8, 10, or any other cap value, and no distribution is
fabricated here. §5 proposes the minimal instrumentation that would make this
answerable after one week of live operation, per the same "measured, not
estimated" discipline athenaeum#784/#787 already hold to.

## 3. What CAN be established without fabrication

Three things are grounded in committed data or in code behavior that is true
regardless of the missing distribution:

**(a) A loose upper bound.** At most 127–132 candidates/night (the full,
unfiltered wiki-dedupe suppression count) could newly reach the queue at *any*
of the proposed values — this is the ceiling if literally every currently
suppressed candidate turned out to be a pure size-gate suppression that also
clears the cohesion floor. It is almost certainly an overcount, per §2.2, but
it bounds the scale of the decision: this is a small-queue-widening knob, not
one that reopens a 1,600-source incident.

**(b) The historical giant-component pathology is structurally closed,
independent of this knob's value.** The athenaeum#400 incident (1,643–1,708-source
proposals at ~0.33 confidence, `merge.py`'s docstring reference and the
original athenaeum#400 issue) was a *formation*-time failure: single-linkage
clustering let one weak bridging edge chain thousands of pages together.
athenaeum#681 changed formation itself to complete-linkage
(`wiki_dedupe.py:440-448`), so that shape can no longer form regardless of
where `max_merge_sources` is set. Raising the cap to 6, 8, 10, or a
considerably higher "unlimited-with-cap" value does not reopen the athenaeum#400
incident — it was closed upstream of this gate.

**(c) A pre-athenaeum#421 historical signal, offered with its caveats stated
up front, not as a distribution.** The 405-entry human queue in
`reasoning-tier-measurements.md`'s "source-page count" table (lines 65-71)
was written when the cap was still 25 (before athenaeum#421 tightened it to 5), yet
**none of the 405 entries exceeds 5 sources** (2→226, 3→95, 4→58, 5→26,
nothing above 5). That is a real, citable fact about what got *written* under
a loose cap — but it describes survivors of the cohesion/confidence floors
too, not the shape of what the size gate alone would admit, and it predates
athenaeum#681's formation change, so it is not a clean analog for today's
complete-linkage clusters. It is context, not a projection.

## 4. T2 safe-class exposure at 6, 8, or 10 — provably zero, not projected

AC2 asks for projected T2 safe-class exposure at the proposed value. Unlike
§2's distribution gap, **this figure does not require the missing data — it
follows deterministically from the gate order in
`reasoning_tiers.safe_class_violation`** (`src/athenaeum/reasoning_tiers.py:844-884`):

```python
SAFE_CLASS_MAX_PAGES = 3          # reasoning_tiers.py:844

def safe_class_violation(views, *, authority_manifest=None):
    if len(views) > SAFE_CLASS_MAX_PAGES:      # checked FIRST, unconditionally
        return SAFE_CLASS_VIOLATION_TOO_MANY_PAGES
    ...
```

The check is page-count first, cheapest-and-most-certain by design
(`reasoning_tiers.py:864-867`, docstring: *"Order of checks is cheapest/most-
certain first ... page count needs no parsing"*). Any proposal admitted
**only because** `max_merge_sources` was raised above 5 has, by construction,
`n_sources` in `[6, new_cap]` — always `> 3`. Such a proposal fails
`too_many_pages` unconditionally, before `memory_class` homogeneity, PII, or
axiom-membership are even consulted.

**Consequence: raising `max_merge_sources` to 6, 8, or 10 adds exactly zero
T2 auto-apply exposure, at any of those values, independent of the athenaeum#714
`memory_class` backfill.** Every newly-admitted proposal is structurally
routed to the human queue (or T1-rejected, since T1 has no approval path
per athenaeum#787's issue body) — T2 cannot auto-apply a >3-source merge no
matter how cohesive or same-class its sources are. This is a code-level
guarantee, not a measurement, and it holds for "unlimited-with-cap" too as
long as the chosen cap stays above 3 (which every candidate value under
discussion does).

**The athenaeum#714 histogram, for completeness (it bears on a different, adjacent
question — the *existing* ≤5-source population's safe-class rate, not on what
this knob newly admits).** athenaeum#714's 2026-08-20 disposition comment records
`memory_class` now populated on **100% of eligible pages (21,919 of 22,367
typed pages: 21,700 by mechanical rule, 219 by classifier residual)**, versus
**0/1099 (0%)** at the athenaeum#784 baseline. Post-backfill class distribution:
entity 20,536 (93.7%), guideline 792, reference 518, fact 45, decision 17,
procedure 11. This means `cross_memory_class` — inert at baseline because
every source read `memory_class: None` (`reasoning-tier-measurements.md`
lines 116-122) — can now actually fire. The baseline's **321/405 (79.3%)**
safe-class figure was computed with that check structurally vacuous
(`safe_class_violation` called with `authority_manifest=None` and, at that
time, no page carrying a real class); a mixed-class small cluster that would
have passed the safe class check at baseline could now fail it on
`cross_memory_class`. **This shifts the *existing* backlog's safe-class rate
in an unknown, presumably-downward direction — recomputing it requires the
live `~/knowledge` corpus, which is out of bounds here.** It is a real
follow-up question but it is orthogonal to this issue's knob: it does not
change the zero-exposure conclusion above, because that conclusion never
depends on `memory_class` at all.

## 5. Proposed value: `max_merge_sources = 8`

Given §2's honest gap and §4's structural guarantee, the choice among 6, 8,
and 10 is **not a risk decision** — T2 exposure is provably identical (zero)
at all three — **it is a stream-restoration/reviewer-capacity decision**:

- **8** sits at the midpoint of the issue's own candidate range, comfortably
  above the current cap (a real, non-trivial widening) and nowhere near
  either the pre-athenaeum#421 default of 25 or the athenaeum#400 incident scale
  (1,600+).
- Every proposal it newly admits lands in the human queue exactly like every
  other non-safe-class proposal today — T1 can reject or pass up, T2 cannot
  approve it (§4) — so the operator's existing review posture is unchanged in
  kind, only in volume.
- **Projected proposals/night at this value: cannot be given as a point
  estimate** (§2). The defensible statement is a bound: somewhere between 0
  (if none of the 127–132 nightly wiki-dedupe suppressions are size-gate-only
  and admissible at 8) and the loose ceiling in §3(a). Treat the first week
  under the new value as the actual measurement, per §6's instrumentation —
  this mirrors exactly the discipline athenaeum#784 already applied to the
  reasoning tiers themselves ("measured, not estimated").
- **Recommendation for the ratification decision:** if the operator wants a
  tighter first step before committing to 8, 6 is a strictly more
  conservative choice with the identical zero-T2-exposure guarantee; nothing
  in this analysis favors 8 over 6 on safety grounds — only on how much
  stream to restore in one step. 10 is defensible on the same grounds but
  widens the aperture further without more evidence to justify the extra
  margin over 8.

## 6. Instrumentation needed to settle AC1's distribution

To make the next ratification cycle a measurement instead of another bounded
guess, extend the suppression log emission (both call sites,
`wiki_dedupe.py:482-488` and `merge.py:2016-2019`) to record `n_sources`
**unconditionally**, not just embedded in the first-matched reason string —
for example a structured field alongside the existing `embedder` field added
by athenaeum#1032 (`wiki_dedupe.py:478-481`), or a durable per-run ledger row
(mirroring the athenaeum#378 spend ledger's shape) keyed by cluster id, night,
`n_sources`, `mean_similarity`, `min_pairwise`, and which gate fired. With
that in place, the existing "Exact commands" recipe in
`reasoning-tier-measurements.md` can be extended to bucket suppressions by
`n_sources` per night, over both call sites, and the actual survivor counts
at any candidate cap become a one-line query instead of a guess.

## 7. Interaction with athenaeum#715

athenaeum#715 (comparator re-run, blocked on athenaeum#713/athenaeum#714) re-runs the five-verdict
comparator **over the existing unresolved entries already in
`wiki/_pending_merges.md`** — "re-run it over the existing pending merge
proposals (order ~31 unresolved at last count — re-count, do not copy the
number) and record a verdict per proposal" (athenaeum#715 issue body). That drains
the **405-item static backlog** athenaeum#784 measured (`reasoning-tier-
measurements.md` lines 96-101 explicitly call it "a static backlog, not a
flowing stream").

`max_merge_sources` is evaluated **upstream of `_pending_merges.md`**, at
proposal-write time in `wiki_dedupe.py`/`merge.py`, before a candidate ever
becomes a queue entry. It governs whether a **new** cluster gets proposed at
all going forward — the **stream**, not the backlog. The two are orthogonal
by construction: raising this cap does not touch any of the 405 existing
entries (they already passed whatever cap was active when written), and
athenaeum#715's re-run does not touch the size-cap suppression path. Ratifying
this knob's value does not need to wait on athenaeum#715, and athenaeum#715's re-run
does not need this knob raised first.

## 8. Out of scope

The live-config flip — actually setting `librarian.max_merge_sources` (or
`ATHENAEUM_MAX_MERGE_SOURCES`) in the operator's `~/knowledge/athenaeum.yaml`
— is **not done by this issue**. It belongs to athenaeum#787's arming step, after
the value proposed in §5 is ratified on athenaeum#1030. This document changes no
runtime behavior and no config default; every figure above is either a code-
verified guarantee (§4) or an explicitly-bounded, explicitly-labeled estimate
(§3, §5) — never a fabricated distribution.

## 9. `docs/configuration.md` default is stale (reported, not fixed here)

While tracing `resolve_max_merge_sources`, `docs/configuration.md` was found
to document the default as **25** in two places — the config-key table (line
61) and the example `athenaeum.yaml` block (line 1899) — while the active
code default is **5** (`config.py:678-720`, confirmed by
`tests/test_merge_proposal_gates.py:31-32`:
`assert resolve_max_merge_sources(None) == 5`). `src/athenaeum/config.py`'s
*own* second example block (its module-level commented sample config,
`config.py:2270-2318`) already shows `max_merge_sources: 5` — it was updated
by the same commit that tightened the default. `git log -p -S"max_merge_sources:
5" -- src/athenaeum/config.py` shows commit `f529efae` (athenaeum#421) changed the
code default and `config.py`'s own inline example, but never touched
`docs/configuration.md`'s two occurrences. This is a real, confirmed doc
defect — left unfixed here per this issue's analysis-only scope; flagging for
the orchestrator to decide whether it warrants its own follow-up.
