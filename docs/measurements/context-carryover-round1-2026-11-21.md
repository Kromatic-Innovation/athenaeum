<!-- SPDX-License-Identifier: Apache-2.0 -->

# Carrying conversation context into per-turn topic extraction, round 1, 2026-11-21

Issue athenaeum#3104. Related: athenaeum#2311 (the gold set) and athenaeum#1530 (the per-turn topic
trace). The build issue that waits on this measurement is athenaeum#3105.

**Results in four lines.**

1. **The candidate pools of the earlier gold set missed about half the relevant pages.** Measured
   against every page the reference marks relevant, production push coverage is 0.32 (strict) to 0.34
   (lenient). Inside the old pools it is 0.505 and 0.533, which is the figure comparable to the 0.53 of
   athenaeum#2311.
2. **Only one context arm clearly beats prompt-only extraction.** A2 (the previous user message plus the
   tail of the assistant's last reply) gains +.035 strict, 95% CI [.008, .064], on Haiku, and +.020
   [.001, .040] on Sonnet.
3. **The two-call arms buy nothing.** The rewrite arm (A3) and the merged two-search arm (A4) cost a
   second extractor call per turn and gain +.002 on both thresholds.
4. **The gate saves little.** 86% of turns qualify for context, so gating removes about one turn in
   seven from the A2 cost and does not improve coverage.

## 1. What was measured and why

The per-turn recall hook extracts search topics from the new user prompt alone. A prompt that leans on
earlier turns ("close it", "what about the second one?", a bare issue number) is searched on its own
words, a prompt under 8 characters gets no search, and a word used only as an example in the prompt is
taken as the topic. The hook's payload carries the path to the conversation transcript, so earlier turns
are cheap to reach. This round asks whether giving the extractor some of that context raises push
coverage, and which cheapest form of it does.

Push coverage here is the share of reference-relevant pages that the push surfaced.

## 2. Method

**Reference labels.** The recorded production push is not a valid baseline, because the corpus has grown
since it was recorded, and many prompts cannot be judged from their own words by a person or a model. So
the reference was built first and every arm is scored against it.

- A strong model read each sampled turn together with the conversation from the session start up to and
  including that turn, and labelled each candidate page `required`, `helpful` or `not_relevant` under the
  athenaeum#2311 rubric. It labelled in two passes, one without and one with the actions the agent took in
  the turn (hindsight), and wrote a one-line statement of what the turn is about.
- **Strict** counts a page as relevant when its final reference label is `required` or `helpful`.
  **Lenient** also counts a page the earlier gold set had labelled relevant, and a borderline page the
  reference labelled with low certainty. The two thresholds bracket the borderline band.
- The candidate pool is every page any arm surfaced, plus pages found by searching on the reference topic
  statement, so a page that no arm found still counts against coverage.
- Arms see only what the hook can see when the prompt is submitted, never the hindsight pass.

**Sample.** 99 interactive tasks from 37 sessions. The issue named 105 tasks from 39 sessions; 2 sessions
(6 tasks) were unrecoverable from the stored transcripts and are out. Of the 99 tasks, 84 follow on from
the previous turn and 15 change subject; 17 prompts are under 40 characters and 2 are under 8.

**Replay.** Each arm is replayed offline against one frozen search index, with the deployed push builder
(at most 7 pages, vector backend, the same token budget, no session de-duplication, no excerpt or linked
notes). The extractor uses the production system prompt and template, run through the Claude CLI on a
subscription. Haiku is the extractor for every arm. Sonnet is repeated for the baseline and the arms
that did best on Haiku. Uncertainty is a bootstrap over sessions, not tasks, because turns in one
session share pages.

**Arms.**

| arm | extractor input |
| --- | --- |
| A0 | the new prompt only (today's behaviour) |
| A1 | the topics extracted on the previous turn, plus the new prompt |
| A2 | the previous user message and the end of the assistant's last reply (each cut to 1,500 characters), plus the new prompt |
| A3 | the extractor rewrites the prompt as a standalone query using the previous turn; the rewrite is then extracted and searched |
| A4 | A0 and A2 run as two searches and merged by reciprocal-rank fusion weighted toward A0 |
| A5 | the last 3 turns, plus the new prompt (the ceiling) |
| G | a derived arm: A2 when the prompt is under 40 characters or points back with a deictic word, otherwise A0 |

## 3. Finding A: the earlier candidate pools missed about half the relevant pages

The production push (what the hook pushed over the turn's window, from the stored records) was scored
against the reference.

| reading | strict | lenient |
| --- | --- | --- |
| relevant pages in the reference | 745 | 1,041 |
| of which inside the athenaeum#2311 candidate pools | 390 | 555 |
| of which outside them | 355 (48%) | 486 (47%) |
| push coverage, against all relevant pages | 0.321 (239 of 745) | 0.337 (351 of 1,041) |
| push coverage, inside the old pools only | 0.505 (197 of 390) | 0.533 (296 of 555) |

The in-pool row is the one comparable to the 0.53 of athenaeum#2311, and it reproduces it. The
all-pages row is the honest one: about half of what a careful reader calls relevant was never in a
candidate pool, so the earlier figure overstated coverage by a factor of about 1.6. Counting only the
prompt-submit window (not the cumulative pushes of later turns), strict coverage is 0.236 against all
pages and 0.451 in-pool.

## 4. Finding B: which context helps

Coverage is the pooled share of reference-relevant pages the arm's push contained. The replay covers only
the prompt-submit push, so these numbers sit below the production readings of section 3 (see section 5).
"Gain" is the arm minus A0 on the same tasks, with its session-cluster 95% CI. Precision is the share of
judged pushed pages that are relevant. Tokens are the extractor's input tokens per turn as the CLI reports
them, which include a fixed instruction overhead of about 1,000 tokens; ms is CLI wall time per turn.

**Haiku extractor, 99 tasks, 37 sessions.**

| arm | strict coverage [CI] | lenient coverage [CI] | gain strict [CI] | gain lenient [CI] | precision strict / lenient | calls per turn | tokens per turn | ms per turn |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A0 | .146 [.115, .175] | .137 [.111, .164] | n/a | n/a | .186 / .249 | 0.98 | 1,006 | 1,611 |
| A1 | .153 [.121, .183] | .145 [.116, .177] | +.007 [-.010, .025] | +.008 [-.006, .023] | .192 / .260 | 1.00 | 1,101 | 1,644 |
| A2 | **.181** [.144, .217] | **.169** [.136, .204] | **+.035 [.008, .064]** | **+.032 [.012, .055]** | .224 / .299 | 1.00 | 1,438 | 1,694 |
| A3 | .148 [.119, .176] | .140 [.113, .167] | +.002 [-.021, .026] | +.003 [-.016, .023] | .184 / .248 | 2.00 | 2,096 | 3,807 |
| A4 | .148 [.117, .176] | .142 [.115, .169] | +.002 [.000, .008] | +.005 [.000, .014] | .185 / .253 | 1.98 | 2,444 | 3,304 |
| A5 | .166 [.132, .202] | .163 [.131, .198] | +.020 [-.006, .049] | +.026 [.003, .048] | .207 / .289 | 1.00 | 1,806 | 1,741 |
| G | .179 [.143, .215] | .164 [.133, .198] | +.033 [.007, .061] | +.027 [.007, .050] | .222 / .291 | 1.00 | 1,397 | 1,694 |

**Sonnet extractor, same tasks (the baseline and the arms that did best on Haiku).**

| arm | strict coverage [CI] | lenient coverage [CI] | gain strict [CI] | gain lenient [CI] | precision strict / lenient | tokens per turn | ms per turn |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A0 | .133 [.106, .159] | .125 [.101, .149] | n/a | n/a | .170 / .227 | 985 | 1,951 |
| A2 | .153 [.119, .186] | .146 [.116, .177] | +.020 [.001, .040] | +.021 [.002, .043] | .191 / .261 | 1,417 | 2,079 |
| A5 | .139 [.107, .171] | .138 [.112, .166] | +.006 [-.023, .034] | +.013 [-.012, .039] | .174 / .246 | 1,785 | 2,116 |
| G | .152 [.119, .184] | .141 [.113, .171] | +.019 [-.002, .039] | +.017 [-.002, .035] | .190 / .252 | 1,376 | 2,078 |

**Reading.**

- **A2 is the only arm whose CI excludes zero on both thresholds and on both extractors.** It adds about
  430 input tokens per turn and no second call. Its precision rises with its coverage (.186 to .224
  strict on Haiku), so the extra pages it finds are not paid for with extra off-topic ones.
- **Sonnet does not beat Haiku.** Sonnet's A0 is a little lower than Haiku's and its A2 gain a little
  smaller, and each sits inside the other's CI. A larger extractor is not what the gap needs.
- **A1 does nothing.** The previous turn's topics carry too little of what a follow-on prompt leans on.
- **A3 and A4 are dropped.** They cost a second call per turn (about 2 to 2.4 times the wall time) and
  gain +.002 strict. A4's CI is narrow only because it equals A0 on most turns by construction.
- **A5 is dropped.** Three turns of context cost more than A2 and read no higher (.166 against .181
  strict on Haiku, .139 against .153 on Sonnet), so extra history dilutes the signal.
- **G is not worth its complexity.** 86% of turns satisfy the gate, so G keeps nearly all of A2's cost
  (1,397 against 1,438 tokens) and gains no coverage over A2 (+.033 against +.035).

**Splits (coverage, Haiku, A0 against A2).**

| split | tasks | A0 strict | A2 strict | A0 lenient | A2 lenient |
| --- | --- | --- | --- | --- | --- |
| follow on from the previous turn | 84 | .141 | .177 | .131 | .166 |
| change of subject | 15 | .166 | .195 | .165 | .184 |
| prompt under 40 characters | 17 | .009 | .078 | .045 | .123 |
| prompt of 40 characters or more | 82 | .167 | .197 | .153 | .177 |
| prompt under 8 characters | 2 | 0 | .222 | 0 | .353 |

- **Follow-on turns carry most of the gain,** +.036 strict across 84 tasks, as the design predicts.
- **Subject changes do not lose coverage.** A2 reads +.029 strict on the 15 subject-change tasks, so
  adding the previous turn did not pull the push toward a topic the user had left. The sample is small
  (section 5).
- **Short prompts move the most in relative terms.** Under 40 characters the baseline is near zero
  (.009) and A2 reaches .078, but 17 tasks is too few to read the size of that. The 2 prompts under 8
  characters get no search under A0 and a search under every context arm; two tasks show only that the
  path works.

**Dropped, and why.**

| arm | verdict | reason |
| --- | --- | --- |
| A1 | dropped | gain +.007 [-.010, .025]; the CI spans zero |
| A3 | dropped | gain +.002; costs a second extractor call and about 2.4 times the wall time |
| A4 | dropped | gain +.002; costs a second call and a merge step for no coverage |
| A5 | dropped | gain +.020 [-.006, .049] strict; costs more tokens than A2 and reads lower |
| G | not preferred | no coverage gain over A2; 86% of turns qualify, so the saving is small |
| A2 | kept for round 2 | the one arm with a CI above zero on both thresholds and both extractors |

## 5. Caveats

- **The replay covers only the prompt-submit push, at most 7 pages.** Production windows hold about 20
  pages per turn, and only 17% of the pages in a production window reappear in the A0 replay. Absolute
  coverage here (about .13 to .18) is therefore not comparable to the production readings of section 3
  (.32 to .34) or to any production window. The comparison that holds is between arms, on the same
  tasks, index and builder.
- **The reference is itself a model's judgement.** Binary agreement with the existing gold labels is
  0.96, with a kappa of 0.60. The disagreement is concentrated on background and standing-policy pages,
  which one reader calls relevant to a turn and another does not. The strict and lenient thresholds
  bracket that band, and the arm gains hold on both. About 19% of reference labels carry a low
  certainty flag. The set of relevant pages also grows slightly as pushed pages get judged, so rates
  shift by a few thousandths between runs.
- **The subject-change sample is small (15 tasks).** The finding that A2 does not hurt a turn that
  changes subject rests on that sample and needs more tasks to bound the cost.
- **The millisecond figures are CLI wall time, not hook latency.** They include the Claude CLI's own start
  up. They compare arms (one call against two) and do not predict what the hook adds to a prompt.
- **Token counts include a fixed overhead,** so the differences between arms are the portable part.
- **Two sessions of the 39 in the issue's sample were unrecoverable,** and the sample is interactive work
  from one deployment. It says nothing about headless runs.

## 6. Next

Round 2 widens the sample to confirm A2 with tighter session-cluster CIs, and checks the small
subject-change and short-prompt cells. If A2 holds, the build is athenaeum#3105: carry the previous user
message and the tail of the assistant's last reply into the extractor, without a gate and without a
second call. If it does not, the recommendation is not to build.
