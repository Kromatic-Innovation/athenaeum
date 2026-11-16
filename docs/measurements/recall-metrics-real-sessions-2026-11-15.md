# Recall metrics on a real-session gold set, 2026-11-15

Measurement for athenaeum#2311, part of the epic athenaeum#2295. It applies the
four recall metrics defined in athenaeum#2611 (`docs/design/recall-metrics.md`)
to a stratified sample of real tasks from one deployment's interactive and
headless sessions. It also scores the athenaeum#2302 `used` heuristic against
the gold labels.

**The relevance labels were made by an LLM, not by a person.** The operator
ruled on 2026-11-15 that a model labels and the operator spot-checks about
10%. The spot-check has not been done yet, so the operator's agreement rate
with the model is **pending operator spot-check**. Until it is filled in, read
every number below as "against LLM labels".

This report holds rates, counts and method only. It contains no session
identifiers, page or entity names, page ids, query or prompt text, paths or
transcript content. The label files, sample manifest and spot-check sheet are
stored outside the repository.

## Headline

| Metric | Interactive (105 tasks, 39 sessions) | Headless (120 tasks, 120 sessions) |
| --- | --- | --- |
| Push coverage | **0.527** (203/385) [0.444, 0.600] | **0.233** (30/129) [0.138, 0.331] |
| Recall-through | **0.036** (14/385) [0.014, 0.062] | **0.000** (0/129) [0.000, 0.029] |
| Use precision (citation rule) | **0.000** (0/28) [0.000, 0.121] | **undefined** (nothing engaged) |
| Session use rate (citation rule) | **0.038** (4/105) [0.000, 0.087] | **0.000** (0/120) [0.000, 0.031] |

Brackets hold a 95% interval. For interactive it is the session-cluster
bootstrap. For headless (one task per session) it is the task bootstrap, or
the Wilson interval where the bootstrap collapses to a point. Every interval
appears in the full tables below.

- About half the pages the labeller judged relevant to an interactive turn had
  been pushed to that session by the end of the turn. For headless sessions it
  is about a quarter.
- The agent almost never went on to recall or open a relevant page: 14 of 385
  in interactive turns, none in headless. Headless sessions made no recall or
  read calls at all in this sample.
- Under the shipped citation rule for "used", no recalled or opened page was
  used. Under the LLM's `changed_work` judgement, 3 of 21 were.
- Most pushed pages were judged irrelevant: 92.4% of interactive pushes and
  96.3% of headless pushes. That reads as a retrieval problem first. It is not
  mainly an ignored-pointer problem.
- The athenaeum#2302 `used` heuristic scores precision **0.14** (7/50, Wilson
  0.07 to 0.26) and recall **0.44** (7/16, Wilson 0.23 to 0.67) against the LLM
  `changed_work` labels. There are only 16 positives, so the recall interval is
  wide.

## Sample

**Unit.** A task is one authored user prompt that started a turn, together with
everything up to the next authored user prompt (the turn window). It is
counted only if at least one proactive push record falls in that window.
Harness-injected pseudo-prompts are not task starts: command echoes,
task notifications, system reminders and interrupt markers. Headless sessions
contribute their first pushed turn only, so for headless a task is a session.

**Population.** Interactive or headless comes from the `entrypoint` field of
the session transcript, with the same mapping as the athenaeum#2306 report:
`cli` and `claude-desktop` are interactive, and `sdk-cli` is headless. Sessions
with no value, several values or an unknown value were excluded. That came to
0 among sessions that otherwise qualified.

**Window.** Push records and sessions dated before 2026-11-15 00:00 UTC. This
cutoff also keeps the labelling session itself out of the pool. The sampled
turns run from 2026-09-26 to 2026-11-14. The push ledger was frozen as a copy
at the start of the run, and all analysis read that copy.

**Eligible universe.** 701 interactive turns across 39 sessions, and 2,150
headless sessions. Sessions whose transcripts are no longer on the host
cannot be sampled, which is the same limit the athenaeum#2306 report
documents.

**Strata and sizes.**

| Stratum | Rule | Universe (tasks) | Sampled tasks | Sessions in sample |
| --- | --- | ---: | ---: | ---: |
| Interactive, session under 500 records | transcript record count | 18 | 14 | 8 |
| Interactive, 500 to 1,999 records | | 108 | 37 | 13 |
| Interactive, 2,000 to 4,999 records | | 88 | 21 | 7 |
| Interactive, 5,000 or more records | | 487 | 33 | 11 |
| Headless, under 50 records | transcript record count | 1,465 | 60 | 60 |
| Headless, 50 or more records | | 685 | 60 | 60 |
| **Total** | | 2,851 | **225** | 159 |

The record-count bands are those of the athenaeum#2306 report.

- **Interactive.** Up to 3 turns were drawn at random from every eligible
  session, so all 39 sessions are in the sample. The universe has only 39
  sessions, so the number of sessions, not turns, limits precision. Capping
  turns per session spreads the sample across sessions instead of letting
  the longest sessions dominate it.
- **Headless.** 60 sessions were drawn at random from each length band.
- **Why this size.** With about 100 to 120 tasks per population, the Wilson
  half-width for a task-level rate near 0.5 is about ±0.09 to ±0.10. For a
  rate near 0 the upper bound is about 0.03. Page-level rates pool several
  hundred labelled pages per population, and they are reported with
  session-cluster intervals so that pages from one session are not treated
  as independent.
- **Weighting.** Strata were sampled at different rates, but all pooled
  figures are **unweighted** across strata. Per-stratum weighting would need
  more than 3 turns per interactive session, which is the binding constraint.
  Interactive figures therefore over-represent short sessions compared with
  the turn universe.

## How candidate relevant pages were found

Each task's candidate set is the union of three sources.

1. **Pushed pages.** Every page the proactive push path recorded in the task's
   turn window. These are the ledger rows with source `sidecar` or `hook`. Rows
   written by the agent's own MCP `recall` calls are not pushes, and they appear
   as engagement instead. Push ids that no longer resolve to a page were dropped
   and counted: 64 of 2,220 interactive pushed pages and 38 of 840 headless
   pushed pages, because those pages were renamed or retired after the push.
2. **Semantic search of the corpus** for the task's request text, top 8.
3. **Keyword (FTS5) search of the corpus** for the same text, top 8.

Both searches ran read-only against copies. The FTS5 index was built from the
live wiki into a private scratch directory, and the vector store was copied
there. The deployment's own cache and ledgers were never queried or written.
Search hits were normalized to the ledger's id form with
`push_metrics.opaque_push_id_from_filename`, the same derivation the push path
uses.

The 225 tasks produced 6,027 candidate pages. That is a median of 24 per
interactive task and 21 per headless task. A few long interactive turns had
over 300, because pushes repeat across a long autonomous turn. Of the 514
pages labelled relevant, 208 had been pushed in the turn window, 25 had been
pushed earlier in the same session, and 281 were found only by search.

**What this misses.** Push coverage is only as complete as the candidate set. A
relevant page that was never pushed and that neither search surfaced is
invisible, so measured push coverage is an **upper bound**. Two pieces of
reachability information were not recorded. The `reach: linked` field was not
labelled, so the scorer's linked-page coverage is not reported. The labeller
was also not told how each page was found.

## Labelling

- **Labeller.** Model `claude-opus-5-5` (the id the API reported on every
  response), called through the plain Anthropic Python SDK at effort `low`.
  Nothing went through athenaeum's own LLM helpers, and nothing was written to
  the deployment.
- **Calls.** 256 calls with up to 40 candidates each. There were no refusals,
  no parse failures and no unlabelled tasks. Tokens: 4,367,688 in and 160,062
  out, about USD 20.70 at list price.
- **Blinding.** The labeller saw the request text, a turn excerpt and each
  page's title plus the first 1,400 characters. Candidate order was shuffled.
  It was not told which pages were pushed, opened or cited. Eight-character
  hex identifiers in the excerpt and pages were masked as `[id]`, so a
  `changed_work` label cannot simply follow a citation.
- **Excerpt.** The excerpt was user and assistant text, plus tool calls and the
  first 400 characters of each tool result. It was capped at 24,000
  characters, with the middle of very long turns omitted.
- **Residual circularity.** A page whose wording the agent reproduced is
  visible to both the labeller and the heuristic's content signal.

**Prompt.** The system prompt was used verbatim:

```text
You label relevance for an evaluation of a memory system that serves an AI coding/operations agent.
You get one TASK: the user's request that started a turn of an agent session, plus an excerpt of what the agent then did in that turn. You also get CANDIDATE PAGES from the agent's long-term memory corpus.

For each candidate page decide:
1. "relevance": one of
   - "required": the page holds information the agent needed for this task; without it the agent would have to rediscover it, ask, or would likely get something wrong.
   - "helpful": the page holds information that would have helped with this task, but the task could be done well without it.
   - "not_relevant": the page does not bear on this task (generic overlap of words or topic is not enough).
2. "changed_work": true only if the excerpt shows the agent's actions or answers actually reflect information specific to this page (the agent applied, quoted, or acted on what the page says). Shared general knowledge is not enough. If the excerpt gives no evidence either way, answer false.

Judge only from the task, the excerpt and the page text. Identifiers have been masked as [id]; do not try to infer them.
Answer with JSON only: {"labels": [{"key": "<candidate key>", "relevance": "...", "changed_work": true|false}, ...]} with exactly one entry per candidate key.
```

The user-message template had placeholders only:

```text
TASK REQUEST:
<request>
{prompt}
</request>

EXCERPT OF THE AGENT'S TURN:
<excerpt>
{excerpt}
</excerpt>

CANDIDATE PAGES:
{pages}
```

`{pages}` holds one `<page key="pNN">` block per candidate, containing
`TITLE:` and the page text.

**Label mapping.** Labels `required` and `helpful` both count as relevant, as
in the gold-set format. `changed_work` is the LLM's "used" judgement.

**Label distribution.**

| Population | Items | required | helpful | not_relevant | changed_work = true |
| --- | ---: | ---: | ---: | ---: | ---: |
| Interactive | 3,528 | 29 | 356 | 3,143 | 42 |
| Headless | 2,499 | 0 | 129 | 2,370 | 0 |

Tasks with no relevant page were 12 of 105 interactive and 47 of 120 headless.
They are left out of the per-page rates, because the denominator is zero and
the rate is undefined. They stay in session use rate.

**Operator agreement: pending operator spot-check.** The spot-check sheet has
580 items in total. 540 come from a two-stage random draw: 10% of labelled
tasks (11 interactive, 12 headless), then all items in each, capped at a
random 30 per task. That is 9.0% of the 6,027 labelled items, and 51 of them
are LLM-relevant. A further 40 items complete the set of all 42 LLM
`changed_work = true` items, because the heuristic's precision and recall rest
on them. A stdlib script computes the rate once the sheet is filled in: raw
agreement with a Wilson interval, Cohen's kappa and per-class agreement, per
population. The figure to quote here is the binary relevant-versus-not
agreement on the 540 drawn items.

| Field | Value |
| --- | --- |
| Operator agreement, relevant vs not (540 drawn items) | **pending operator spot-check** |
| Cohen's kappa, relevant vs not | **pending operator spot-check** |
| Operator agreement on LLM `changed_work = true` (42 items) | **pending operator spot-check** |

## Metric definitions as applied

The scorer is `src/athenaeum/recall_metrics.py` (`observe_task`, `score_task`,
`_estimate`), run at commit `0b6cffd8`. The stock session-level entry point
(`score_from_ledger`) was not used. It reads a whole session's pushes and
transcript, which would credit a page pushed in any turn to every turn of a
long interactive session. Instead, each task was observed with:

- **Pushed.** Proactive push records with a timestamp before the end of the
  turn window. That means pushes in this turn plus earlier turns of the same
  session, because the push path does not re-push a page already pushed to the
  session. A window-only variant is reported as well.
- **Transcript.** Only the turn window's records. Recall or open events and
  citations outside the turn do not count for it.

The gold set was built in memory, not through the YAML loader, because tasks
with an empty relevant set must stay in session use rate and the loader
rejects them.

Two definitions of "used" are reported. The **citation rule** is the
athenaeum#2611 definition: the page id appears as a whole token in user or
assistant text in the window. The **LLM rule** is `changed_work = true`, and it
only exists for pages in the candidate set.

Intervals:

- **Wilson.** Over the pooled counts.
- **Task bootstrap.** The scorer's own: resampling tasks, seeded, 2,000
  resamples.
- **Session-cluster bootstrap.** Added here: resampling sessions, 2,000
  resamples. For interactive, several turns share a session, so the task
  bootstrap is optimistic. Trust the session-cluster interval.
- **Degenerate bootstraps.** When every task has numerator 0, both bootstraps
  collapse to [0, 0]. Use the Wilson upper bound there.

## Results

Each cell gives the rate, the counts, the number of tasks and sessions with a
defined rate, and the Wilson, task-bootstrap and session-cluster-bootstrap
intervals.

### Interactive (105 tasks, 39 sessions)

| Metric | Rate | Counts | Tasks / sessions | Wilson | Task bootstrap | Session bootstrap |
| --- | ---: | ---: | ---: | --- | --- | --- |
| Push coverage | 0.527 | 203/385 | 93 / 39 | 0.477 to 0.577 | 0.447 to 0.602 | 0.444 to 0.600 |
| Push coverage, required pages only | 0.310 | 9/29 | 20 / 17 | 0.173 to 0.492 | 0.167 to 0.464 | 0.200 to 0.448 |
| Push coverage, window-only pushes | 0.462 | 178/385 | 93 / 39 | 0.413 to 0.512 | 0.377 to 0.542 | 0.370 to 0.542 |
| Recall-through | 0.036 | 14/385 | 93 / 39 | 0.022 to 0.060 | 0.013 to 0.065 | 0.014 to 0.062 |
| Recall-through of pushed | 0.049 | 10/203 | 67 / 30 | 0.027 to 0.088 | 0.009 to 0.107 | 0.010 to 0.093 |
| Use precision, citation rule | 0.000 | 0/28 | 14 / 11 | 0.000 to 0.121 | degenerate | degenerate |
| Use precision, LLM rule | 0.143 | 3/21 | 10 / 9 | 0.050 to 0.346 | 0.000 to 0.294 | 0.031 to 0.316 |
| Session use rate, citation rule (per task) | 0.038 | 4/105 | 105 / 39 | 0.015 to 0.094 | 0.010 to 0.076 | 0.000 to 0.087 |
| Session use rate, LLM rule (per task) | 0.114 | 12/105 | 105 / 39 | 0.067 to 0.189 | 0.057 to 0.181 | 0.064 to 0.165 |

Context:

- Pushed pages per task: median 14.
- Opened without push: 5.
- Engaged (recalled or opened) pages: 28, of which 21 were in the candidate
  set, so only 21 had an LLM "used" label.
- At session level, 3 of 39 sessions used a delivered page in a sampled turn
  under the citation rule, and 12 of 39 under the LLM rule.

### Headless (120 tasks, 120 sessions)

| Metric | Rate | Counts | Tasks | Wilson | Task bootstrap |
| --- | ---: | ---: | ---: | --- | --- |
| Push coverage | 0.233 | 30/129 | 73 | 0.168 to 0.313 | 0.138 to 0.338 |
| Push coverage, required pages only | undefined | 0/0 | 0 | n/a | n/a |
| Recall-through | 0.000 | 0/129 | 73 | 0.000 to 0.029 | degenerate |
| Recall-through of pushed | 0.000 | 0/30 | 19 | 0.000 to 0.114 | degenerate |
| Use precision, both rules | undefined | 0/0 | 0 | n/a | n/a |
| Session use rate, both rules | 0.000 | 0/120 | 120 | 0.000 to 0.031 | degenerate |

- Use precision is undefined, not 0: no headless task recalled or opened any
  page, so its denominator is empty.
- No headless page was labelled `required`.
- Pushed pages per task: median 7 (a single push).
- The window-only variant equals the main figure, because a headless task is
  the session's first pushed turn.

### Both populations pooled (225 tasks, 159 sessions; unweighted)

| Metric | Rate | Counts | Wilson | Session bootstrap |
| --- | ---: | ---: | --- | --- |
| Push coverage | 0.453 | 233/514 | 0.411 to 0.497 | 0.382 to 0.515 |
| Recall-through | 0.027 | 14/514 | 0.016 to 0.045 | 0.010 to 0.047 |
| Use precision, citation rule | 0.000 | 0/28 | 0.000 to 0.121 | degenerate |
| Session use rate, citation rule | 0.018 | 4/225 | 0.007 to 0.045 | 0.000 to 0.041 |

The pooled figures mix two very different populations and are weighted by the
sample design. Prefer the per-population tables.

## The athenaeum#2302 `used` heuristic against the labels

**What was scored.** Every pushed page in a task's turn window that resolved
to a page and was labelled: 2,156 interactive and 802 headless. The ground
truth is the LLM `changed_work` label, and the heuristic's verdict is the
positive prediction.

**How the verdicts were produced.** Each verdict comes from the real
`push_metrics.determine_references`, through
`push_used_scoring.compute_heuristic_verdicts` and `score_labels`. Each task
ran in a private sandbox holding only that turn window's ledger rows (all
sources, as production reads them) and that window's transcript records. The
live wiki was passed as `wiki_root`, read-only, so the content-overlap signal
was on, as it is in the production `session_end` call. Every verdict was
determinate: no labels were undetermined.

| Population | n | TP | FP | FN | TN | Precision (Wilson) | Recall (Wilson) |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| Interactive | 2,156 | 7 | 42 | 9 | 2,098 | 0.143 (0.071 to 0.267) | 0.438 (0.231 to 0.668) |
| Headless | 802 | 0 | 1 | 0 | 801 | 0.000 (0/1) | undefined (no positives) |
| Both | 2,958 | 7 | 43 | 9 | 2,899 | 0.140 (0.070 to 0.262) | 0.438 (0.231 to 0.668) |

**Unused-push breakdown.** This covers pushed pages the labels call unused
(`changed_work = false`):

- **Interactive.** 162 of 2,140 (7.6%, Wilson 6.5% to 8.8%) were relevant but
  not used, which is an ignored pointer. 1,978 (92.4%) were irrelevant, which
  is a retrieval miss.
- **Headless.** 30 of 802 (3.7%) were relevant but not used, and 772 (96.3%)
  were irrelevant.

**Reading it.** When the heuristic says "used", the LLM agrees about one time
in seven. When the LLM says a pushed page changed the work, the heuristic
catches a little under half. In other words, it over-calls use and misses
about half of real use. The positive class is tiny (16 pages), so both rates
carry wide intervals. They are also only as good as the LLM `changed_work`
labels, which is why every one of those items is in the spot-check sheet.

## What this does and does not say

- **Interactive.** Push reaches about half of what the labeller judged
  relevant, and about 31% of the few pages judged required, from a wide
  interval. The agent then almost never follows a pointer: recall-through of
  pushed is 4.9%.
- **Headless.** Headless sessions receive one small push and never recall or
  open anything. Their recall-through and use are zero by construction of
  their tool use, as athenaeum#2306 found.
- **Most pushes are irrelevant** to the task in front of the agent, by these
  labels: over 92% in both populations. That shows up as a retrieval-precision
  problem before it is a delivery-form problem.
- **Use precision under the citation rule is 0/28.** Engaged pages are few, so
  the Wilson upper bound is 12%. The LLM rule gives 3/21. Read the two
  together: the citation rule misses use that does not quote an id.
- **The labels are LLM-made, and the operator's agreement rate is pending.**
  The labeller saw a capped excerpt and the first 1,400 characters of each
  page. It can miss relevance that sits deeper in a page, and it can miss use
  in the omitted middle of a very long turn.
- **Push coverage is an upper bound.** The candidate set is pushed pages plus
  two top-8 searches, so a relevant page that none of the three surfaced is
  invisible.
- **Scope.** This is a single deployment, one window, and only sessions whose
  transcripts survive on the host. Interactive figures rest on 39 sessions.
  This report makes no decision. The decision rule on athenaeum#2295 consumes
  it.

## Method summary

The run was read-only against the deployment. It read the push ledger, which
was frozen as a copy first, along with the session transcripts and the wiki.
It wrote nothing to the deployment: no corpus change, no ledger row and no
cache write. All search ran against private copies. Model calls went only to
the labeller. Scripts, the frozen ledger copy, labels, the sample manifest and
the spot-check sheet live outside the repository and are not published.
Scorer code: commit `0b6cffd8`.
