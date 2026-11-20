# Why pushed pages are irrelevant: cause attribution, 2026-11-19

Measurement for athenaeum#2804, part of the epic athenaeum#2295. The report
`recall-metrics-real-sessions-2026-11-15.md` (athenaeum#2311) found that 91.7%
of interactive and 96.3% of headless pushed pages were irrelevant to the task.
It did not say why. Retrieval and intake are a paired problem, and an irrelevant
push can mean the right page existed and ranking missed it, the right page was
never compiled, the query did not describe the turn, or the turn needed nothing.
This report assigns every one of those irrelevant pushed pages to exactly one
cause, so the next investment is chosen on evidence.

**All labels were made by reading, by a model, not by a person.** The relevance
labels it builds on are the athenaeum#2311 LLM labels, whose operator spot-check
is still pending (athenaeum#2311). The cause labels are a second layer on top of
them. The second pass was a fresh model reader, not a human. Read every number
as "against LLM labels".

This report holds rates, counts and method only. It contains no session
identifiers, page or entity names, page ids, query or prompt text, paths or
transcript content. The cause labels, evidence tables and scripts are stored
outside the repository.

## Headline

| Cause | Interactive (1,978 pushed pages, 39 sessions) | Headless (772 pushed pages, 119 sessions) |
| --- | --- | --- |
| d1. Push driven by harness text, not by the turn | **63.7%** (1,259) [61.5, 65.7] | 0.0% (0) |
| a. Relevant page existed, was not delivered | **16.9%** (334) [15.3, 18.6] | **49.2%** (380) [45.7, 52.7] |
| d2. Turn's own prompt could not describe the turn | 9.5% (188) [8.3, 10.9] | 0.0% (0) |
| f. Relevant page already delivered; slot was filler | 6.5% (128) [5.5, 7.6] | 7.4% (57) [5.7, 9.4] |
| c. No relevant information existed anywhere | 2.8% (55) [2.1, 3.6] | 1.8% (14) [1.1, 3.0] |
| e. Turn needed no memory | 0.7% (14) [0.4, 1.2] | **41.6%** (321) [38.2, 45.1] |
| b. Information in raw intake, no compiled page | 0.0% (0) [0.0, 0.2] | 0.0% (0) [0.0, 0.5] |

Brackets hold the Wilson 95% interval over pages. Pages cluster in sessions and
tasks (one interactive task holds 286 of the 1,978), so the Wilson interval is
too narrow. The session-cluster bootstrap intervals are in the full tables
below, and they are the ones to trust.

- **Interactive: most irrelevant pushes were not about the turn at all.** Almost
  two thirds were fired by an injected notification (a background task or
  sub-agent finishing), not by what the person asked. They are not a ranking
  failure and no ranking change addresses them.
- **Headless: two causes cover 91% of misses.** Half of the pages were pushed
  while a relevant page existed, but every one of those relevant pages was
  graded only "helpful", never "required". Another 42% were pushed to
  programmatic tasks that carry all their input in the prompt and need no memory.
- **A missing page explains almost none of them.** Cause (c) is 2.8% of
  interactive and 1.8% of headless irrelevant pushes. Cause (b) was not
  observed. On this evidence intake is not why most pushes are irrelevant.
- **The one judgement-heavy cause is weak.** Cause (d2) agreed in 1 of 5 cells on
  the second pass. See the sensitivity below.

## Unit and sample

**Population.** Every irrelevant pushed page in the athenaeum#2311 sample:
1,978 interactive (105 tasks, 39 sessions) and 772 headless (119 tasks and
sessions). "Irrelevant" is the athenaeum#2311 label `not_relevant`. A page is
counted once per task, at its first push in the turn window. Page counts match
the athenaeum#2311 report exactly.

**This is a census, not a sample.** The task allowed a stratified sample of at
least 150 per population if labelling every page was too much. It was not
needed, because the cause is decided per push, not per page. The unit of
labelling is the **cell**: one task and one push record (a push record carries
up to 7 pages). There are 435 cells (316 interactive, 119 headless). Each cell
was labelled once and the label applies to all its pages. Pages in one cell
share the query, the turn and the delivery state, so the cause is the same for
all of them. The cost is that pages are not independent, which the cluster
intervals reflect.

## The causes and how each was decided

| Code | Cause | Decided by |
| --- | --- | --- |
| a | A relevant page existed in the compiled wiki at push time and was not ranked into the push | Mechanical, from the athenaeum#2311 relevance labels plus store history, see below |
| b | No relevant compiled page, but the information was in raw intake | Raw-intake search, run only on cells that reached this branch |
| c | No relevant information existed anywhere in the store | Same search, and raw sources dated after the push |
| d1 | The push was driven by harness text, not by the turn | Mechanical, from the push's query hash, see below |
| d2 | The push was driven by the turn's own prompt, but the prompt alone cannot describe the work | Reading |
| e | The turn needed no memory | Reading, plus an independent search |
| f | Every relevant page that existed had already been delivered, so the slot was filler | Mechanical |

Two causes were added because the data forced them. **d1/d2 split cause (d).**
The query hash in the push ledger is the hash of the whole prompt the hook
received. Hashing every message in each session's transcript recovered the text
behind every interactive push record except one that covers 7 pages. It showed that
1,259 of 1,978 interactive pages came from a different message than the one that
started the task. Almost all were machine-generated completion notices from
background tasks and sub-agents. The fixes differ (do not fire the hook on
harness text, versus improve how a real prompt is turned into a query), so they
are reported apart. **f is new.** When the relevant page had already been
delivered, the slot was not a miss of any kind. It is a precision problem (a cap
with no relevance floor), not a ranking miss, and folding it into (a) would
overstate ranking failure.

**Order of decision**, applied to each cell:

1. **Trigger first.** If the push was driven by an injected notification or
   agent message, the cause is d1, whatever else is true. The query could not
   have asked for a relevant page.
2. If driven by the turn's authored prompt (or a message the person typed
   mid-turn) and that prompt, read alone, names no subject that identifies the
   work (a bare acknowledgement, a reference to earlier context or an
   attachment, a pasted blob with no stated aim), the cause is d2.
3. If the earlier run found no relevant page for the task, read the turn and
   search the live store. If the work was self-contained (a programmatic task
   whose input is the prompt, a general-knowledge question, a live status
   check), the cause is e. Otherwise it is (b) or (c) by the raw-intake search.
4. If relevant pages were found, test each one **at that push's time**:
   - did it exist? Existence is the page's first appearance in the store's git
     history on or before the push time;
   - had it already been delivered to that session by that push or an earlier
     one?
   If any relevant page existed and had not been delivered, the cause is (a). If
   all existing ones had been delivered, the cause is f. If none existed yet,
   check the raw sources the page was compiled from. In every such cell, all of
   them were captured after the push, so the information did not exist yet, and
   the cause is (c).

**Independent search.** The athenaeum#2311 candidate set came from the same
retrieval stack this report is diagnosing, so it could not be trusted to show
that no page existed. For every cell with no relevant page, the live wiki was
searched with a full-text index and raw intake was searched by text, both
read-only through the deploy install's `athenaeum` tooling, and the agent's own
`recall` queries in the turn were replayed where there were any. That search
found no relevant page for the one interactive task that needed memory (cause c)
and none for the headless programmatic tasks, bar the one case noted under
limits.

## Results

Counts are pages. Shares have two intervals: Wilson over pages, and a
session-cluster bootstrap (2,000 resamples, seed 2804) that resamples sessions.

### Interactive (1,978 pages, 316 cells, 105 tasks, 39 sessions)

| Cause | Pages | Share | Wilson | Session bootstrap |
| --- | ---: | ---: | --- | --- |
| d1. Harness text drove the push | 1,259 | 63.7% | 61.5 to 65.7 | 50.9 to 73.3 |
| a. Relevant page existed, not delivered | 334 | 16.9% | 15.3 to 18.6 | 11.5 to 24.2 |
| d2. Prompt could not describe the turn | 188 | 9.5% | 8.3 to 10.9 | 5.2 to 15.6 |
| f. Filler beside delivered relevant page | 128 | 6.5% | 5.5 to 7.6 | 2.5 to 12.3 |
| c. No relevant information anywhere | 55 | 2.8% | 2.1 to 3.6 | 0.9 to 5.6 |
| e. Turn needed no memory | 14 | 0.7% | 0.4 to 1.2 | 0.0 to 1.9 |
| b. In raw intake, never compiled | 0 | 0.0% | 0.0 to 0.2 | 0.0 to 0.0 |

- d1 split: 1,233 from background-task notices and 26 from agent messages.
- Within (a), 68 pages sit in cells where a missed page was graded "required"
  and 266 where every missed page was only "helpful".
- Weighted by task (each task's pages sum to one) the shares are d1 35.8%, a
  31.2%, d2 14.4%, f 11.0%, c 6.2%, e 1.4%. The pooled figure is dominated by a
  few long autonomous turns, so the task-weighted view lowers d1 and raises a.
  d1 stays the largest cause under either weighting.

### Headless (772 pages, 119 cells, 119 tasks and sessions)

| Cause | Pages | Share | Wilson | Session bootstrap |
| --- | ---: | ---: | --- | --- |
| a. Relevant page existed, not delivered | 380 | 49.2% | 45.7 to 52.7 | 40.2 to 58.4 |
| e. Turn needed no memory | 321 | 41.6% | 38.2 to 45.1 | 32.8 to 50.5 |
| f. Filler beside delivered relevant page | 57 | 7.4% | 5.7 to 9.4 | 3.5 to 11.8 |
| c. No relevant information anywhere | 14 | 1.8% | 1.1 to 3.0 | 0.0 to 4.6 |
| d1, d2, b | 0 | 0.0% | 0.0 to 0.5 | n/a |

- Every headless task is a programmatic routine with a templated prompt: a
  daemon planning routine (458 pages), a job-posting extractor (184), an email
  classifier (90), and a few others (40). None has a person's own prompt.
- Within (e): 210 pages from the planning routine, 83 from the email classifier,
  28 from the extractor. All carry their whole input in the prompt.
- Within (a): 209 planning routine, 150 extractor, 14 other, 7 classifier. **No
  headless page was ever graded "required"**, so all 380 sit in cells where the
  missed page was only "helpful", a page the athenaeum#2311 rubric says the task
  "could be done well without".
- Weighted by task: a 49.6%, e 39.5%, f 9.2%, c 1.7%.

### Sensitivity to the existence test

Existence was dated by the page's first appearance in the store's git history.
Fusion and move-then-retire renames can make that later than the page's real
age, so the variant "existed if either the git date or the page's own `created`
date was on or before the push" was also run.

| Population | a | f | c |
| --- | ---: | ---: | ---: |
| Interactive, git date | 16.9% | 6.5% | 2.8% |
| Interactive, earlier of the two | 20.3% | 4.8% | 1.1% |
| Headless, git date | 49.2% | 7.4% | 1.8% |
| Headless, earlier of the two | 51.0% | 7.4% | 0.0% |

No ranking of causes changes. (c) shrinks, so the no-information share is, if
anything, over-stated here.

## Second pass and agreement

A seeded draw (seed 2804) took 15% of cells within every population and cause,
at least 2 per group: **74 of 435 cells (17.0%), covering 473 of 2,750 pages
(17.2%)**. A fresh model reader, given none of my labels and not the order of
decision, labelled them from an evidence table (the trigger, the prompt, the
pushed titles, and each relevant page's grade, existence and delivery state)
with the independent search helper available. The first pass was mine. The
second reader is a model, not a human, and the operator's own check of these
labels is not done.

| Subset | Cells agreeing | Pages agreeing |
| --- | ---: | ---: |
| All | 68 of 74 (91.9%, Wilson 83.4 to 96.2) | 440 of 473 (93.0%) |
| Interactive | 47 of 53 (88.7%, 77.4 to 94.7) | 298 of 331 (90.0%) |
| Headless | 21 of 21 (100%, 84.5 to 100) | 142 of 142 (100%) |

Cohen's kappa over the seven causes is 0.89. By first-pass cause: a 18 of 18,
d1 31 of 31, e 10 of 10, f 6 of 6, c 2 of 4, d2 1 of 5.

**The disagreements are all in judgement calls.** Four of five d2 cells were
re-read as descriptive prompts and relabelled (a). One (c) cell was read as (e)
and one as d2. The mechanical causes agreed completely. For the sensitivity: if
the d2 disagreement rate (4 of 5) held across all of d2, up to 150 of the 188
interactive d2 pages would move to (a), giving (a) about 24.5% and d2 about
1.9%. Cause (d2) is the least certain label here, and the interactive (a)/d2
boundary should be read as "a combined 26.4%" until a person checks it.

## Recommendation

**The causes that account for most misses are different in the two
populations, and neither is a missing page.**

1. **Interactive: stop pushing on harness text (d1, 63.7%).** Two thirds of
   irrelevant pushes were fired by injected completion notices and agent
   messages. This is a trigger fix: do not run the push on pseudo-prompts, or
   build the query from the turn's task instead of the injected text. It is not
   a ranking change. Better ranking would not make a notice describe the turn.
   The second cause, a relevant page that existed and was not delivered (a,
   16.9%, up to about 26% with d2), is the genuine retrieval case, and is where
   a retrieval change belongs.
2. **Headless: suppress or gate the push for self-contained programmatic prompts
   (e, 41.6%), and treat the large (a) share (49.2%) with care.** No
   headless relevant page was "required", so the (a) cell is mostly the
   rubric's optional pages. If "helpful only" is not counted as a miss, the
   headless (a) share would read as (e), and 91% of headless misses would be
   "the turn needed no memory". A relevance floor or an opt-out for templated
   routines addresses both. This is a precision fix, not a ranking fix.

**No retrieval change is recommended for the misses whose cause is a missing
page.** They are 2.8% of interactive and 1.8% of headless pushes (c), and
nothing was observed under (b). Across 435 cells only 8 interactive cells and
the 2 headless cells reached a stage where a missing page could be the cause;
the rest were decided earlier by the push's trigger or by a page that already
existed. So this report shows intake is not a main cause of irrelevant pushes.
It does not show that intake is healthy for relevant recall, which is a
separate measurement.

The follow-on issues the acceptance criteria call for (one per named cause,
linked from athenaeum#2295 and athenaeum#2312) are athenaeum#2914 (harness-text
triggers, d1), athenaeum#2915 (self-contained programmatic prompts, e) and
athenaeum#2916 (record the extracted query terms). The retrieval case (a) is
athenaeum#2952, blocked by athenaeum#2916 so that extraction misses can be told
apart from ranking misses.

## Limits

- **LLM labels on LLM labels.** Relevance is the athenaeum#2311 model label, with
  its operator spot-check pending. Cause labels add a second layer, and the
  second reader is also a model.
- **Query terms were not logged.** The push builds its query from topic terms
  extracted from the prompt, and only a hash of the prompt is recorded. For
  prompts that do describe the work, an extraction failure cannot be told apart
  from a ranking miss, so both land in (a). Cause (a) therefore includes any
  unobservable extraction error, and is an upper bound on pure ranking miss. A
  replay with a regex-only extractor reproduced the exact pushed set in only 28
  of 117 single-push headless tasks, which fits either index drift or a
  different extractor, and shows nothing either way. An exact replay needs the
  paid extraction model and was not run.
- **Not every cell reached the (b)/(c) test.** The trigger and delivery tests
  decide first, so "(b) was not observed" holds for the 10 cells that reached
  it, not for the whole population.
- **Existence is an approximation.** It uses the store's git history as it
  stands, and the current page text and index. Renames, fusions and later edits
  can move a page's first appearance. A variant is reported.
- **d1 is a trigger claim, not a content claim.** A notification's summary line
  names the sub-task, so some notices may be topically near the turn. The
  analysis cannot tell, because the extracted terms are not logged.
- **Interactive figures rest on 39 sessions.** Cluster intervals are wide, and
  the long-turn dominance of d1 is why the task-weighted view is given.
- **The candidate pool came from the stack under test.** A relevant page that
  neither search nor a push surfaced was only found where the independent search
  looked, so (a) may be an under-count for tasks with no labelled relevant page.
  For one headless class (an email classifier), the sender's own contact page
  existed in the wiki and was not in the candidate pool. It was judged not
  relevant to the classification and left in (e). A stricter reading would move
  those pages (4 of the 12 classifier tasks) to (a).
- **Scope.** One deployment, one window (2026-09-26 to 2026-11-14), and only
  sessions whose transcripts survive on the host.

## Method summary

The run was read-only against the deployment. It read the frozen athenaeum#2311
ledger copy, label files and sample manifest, the session transcripts, the live
wiki and raw intake, and the store's git history. It wrote nothing to the
deployment. No corpus or transcript content went to any outside service, and no
paid model call was made: the labelling was done by reading. Scripts, evidence
tables, cause labels and the second-pass pack live outside the repository and
are not published.
