# Recall metrics: did push give the agent what the task needed

Status: defined by athenaeum#2611 (epic athenaeum#2295). The scorer is
`src/athenaeum/recall_metrics.py`; the committed example gold set is
`tests/evals/data/recall_metrics/gold-set-example.yaml`. Nothing here changes
the shipped hook.

## Why raw push-use is not the target

The share of pushed pages marked `used` (athenaeum#2306) is close to
meaningless alone. A baseline that handed the agent a list of every page would
use about 0.001% of what it pushes, so a low rate does not mean push is
failing, and a high one does not mean it is working: pushing one page per
session can read 100%. The rate also mixes two causes with opposite fixes: the
push was irrelevant (fix retrieval), or it was relevant and the pointer was
ignored (fix delivery form). Raw push-use and push volume per session are still
reported, as context and never as targets.

The goal is to push exactly what is needed, which breaks into three
principles, each with a metric that needs a gold set to compute:

1. Relevant information is sent, so the agent does not redo work memory holds.
2. Relevant information is recalled in full; summaries are good enough to tell
   a page matters.
3. Recalled information is used in the task, which shows the relevance
   judgement was right.

## The four metrics

All are computed per task, then aggregated. A rate with a zero denominator is
reported as undefined, never as 0.

| Metric | Definition | Principle |
| --- | --- | --- |
| Push coverage | Relevant pages that were pushed / relevant pages | 1 |
| Recall-through | Relevant pages the agent recalled or opened / relevant pages | 2 |
| Use precision | Recalled-or-opened pages that were used / recalled-or-opened pages | 3 |
| Session use rate | Sessions that used at least one delivered item / sessions | all |

The scorer also reports `recall_through_of_pushed` (relevant pages that were
pushed and then opened strictly after the push, over relevant pages that were
pushed), `opened_without_push`, and push coverage restricted to pages labelled
`reach: linked`.

"Delivered item" for session use rate means a page that was pushed or
recalled. "Recalled or opened" (the engaged set) means the MCP `recall` or
`read_entity` tools, or a file read of a page, in the session transcript. An
open of a pushed page counts only when it comes after the push.

### Aggregation and intervals

The aggregate rate pools counts across tasks. Each carries two 95% intervals:
a Wilson score interval over the pooled counts, and a bootstrap interval that
resamples whole tasks (seeded, 2000 resamples). Pages within a task are not
independent, so the Wilson interval alone is optimistic; the bootstrap is the
one to trust when they disagree, and it is omitted for a single task. A task
whose rate is undefined drops out of that metric's pool (so `use_precision`
can have fewer tasks than `push_coverage`).

## What each metric can and cannot show

- **Push coverage** shows retrieval misses, but only against the labels. A page
  nobody labelled relevant is invisible to it; a missing label reads as a
  precision loss elsewhere, never as a coverage loss. It says nothing about
  whether the agent noticed the push.
- **Recall-through** shows whether a pushed summary was enough for the agent to
  go and read the page. It cannot separate "the pointer worked" from "the agent
  would have searched anyway": a relevant page opened without ever being pushed
  counts here. That is why `opened_without_push` is reported next to it and
  `recall_through_of_pushed` exists. It also overstates deliberate reads (see
  limits).
- **Use precision** shows whether pages the agent engaged with mattered. It is
  only as good as the definition of "used" below, and it is blind to pages the
  agent never engaged with, so it is not a coverage measure.
- **Session use rate** is the health check: a system whose sessions never use
  anything is broken whatever the other three say. It is not a quality measure;
  a session can use one irrelevant page and score 1.

## Gold set

A gold set is a YAML file validated by `recall_metrics.parse_gold_set`:

```yaml
version: 1                    # required, must be 1
corpus: tests/evals/data/corpus/core   # optional, free text
tasks:
  - id: relay-make-sync       # required, unique
    prompt: "..."             # required, task-shaped text the agent starts from
    session_id: "..."         # optional: binds the task to a real session
    relevant:                 # required, non-empty
      - uid: system-fulfillment-relay   # id as the push ledger records it
        source: tests/evals/data/corpus/core/13-unprompted-push.yaml
        relevance: required   # required (default) | helpful
        reach: lexical        # lexical (default) | linked
```

- Relevant pages are labelled **independently of what was pushed**, so pages a
  push missed are in the set. That is what makes push coverage computable.
- `reach` says whether a push built from the task text could plausibly find the
  page (`lexical`) or only a link from another page could (`linked`). Linked
  pages are the likely non-pushed ones; the scorer reports coverage on them
  separately. Decoy pages a corpus plants are left unlabelled.
- `relevance: helpful` pages count in all four metrics; the field exists so a
  later analysis can restrict to `required`.

**Two sources, one committed.** Tasks drawn from real sessions carry a
`session_id` and their labels live outside this repository (athenaeum#2311);
real transcript content never enters it. The committed example is 14 invented
tasks over the synthetic eval corpus, with no `session_id`: it fixes the
format and gives the tests something realistic, and it is not a measurement.
For the corpus example the `uid` is the corpus slug; against a real ledger the
`uid` must be the id form the ledger records (the eight-character id of a
compiled page), so a real gold set is built from the live pages, not copied
from the example.

## Definition of "used", and its error rate

A delivered page is **used** when its id appears as a whole token in
user or assistant text in the session transcript. Text inside a tool result
never counts, because the push's own rendering contains the id and would mark
every push used. This is the citation signal of
`push_metrics.determine_references` with the echo fallback removed, the same
choice `follow_through.py` makes.

Known error modes:

- **False negatives**: a page that shaped the answer and was never quoted (the
  agent applied a rule without naming its id). Use precision is biased low.
- **False positives**: an id copied into text for a reason other than use (the
  user pasted a list of ids; the agent restated a page it then ignored).
- **Content-only use** (the agent reproduced the page's wording without its
  id) is out of scope here; the content signal needs page text.

**The error rate is not yet measured on real sessions.** The only published
figure, 0% false-negative and 0% false-positive in
`docs/measurements/used-column-heuristic-accuracy-2026-09-15.md`, is five
synthetic fixtures and describes the fixture design, not real accuracy. The
tooling that measures it against human labels is `push_used_scoring.py`
(athenaeum#2302); the labelling is athenaeum#2311. Until that report exists,
every use-precision and session-use-rate figure should be quoted with the
caveat that "used" is an unvalidated citation proxy. Model or human labels are
the alternative definition, and are what that report would calibrate against.

## Limits carried over from the athenaeum#2306 report

Both are documented limits of the scorer, not fixed by it:

- **Opened without push.** A relevant page the agent found by itself is a
  recall-through success and says nothing about push. The scorer counts it in
  `recall_through`, reports the count as `opened_without_push`, and keeps
  `recall_through_of_pushed` free of it. Read recall-through together with
  those two.
- **"Opened" counts a listing.** An id appearing in a `recall` result is scored
  as recalled even when it sat in a many-hit listing the agent never read, so
  recall-through is an **upper bound** on deliberate reads. Listing-inflated
  engagement is not hidden: it enlarges the engaged set and lowers use
  precision. A deliberate-read-only variant (`read_entity`, file read) would
  need the open-event reader to return the kind of open; that reader belongs
  to `follow_through.py` and is not changed here.

Also: sessions with no transcript, or tasks with no `session_id`, are listed as
unobserved and left out, never scored as zero; and the interactive-versus-
headless split does not exist in the ledger today, so none of these metrics is
segmented by it.

## Using the scorer

```python
from athenaeum import recall_metrics as rm

gold = rm.load_gold_set(path_to_gold_set)          # path is always caller-given
report = rm.score_from_ledger(gold)                # push ledger + transcripts
print(report.to_dict())                            # counts and rates only
```

`rm.score(gold, observations)` takes prepared `TaskObservation` sets directly.

## Measuring recall on a host with no per-turn hook

Claude Code has a per-turn hook, so the metrics above see every push. ChatGPT,
the Grok apps and Cursor do not: the model gets recall only by pulling (a
`recall` or `read_entity` call it chooses to make, perhaps steered by an
instruction), or from a session-start snapshot. Each host's design note
([ChatGPT](host-chatgpt.md), [Grok](host-grok.md), [Cursor](host-cursor.md))
says what to call the mechanism; this section says how to measure it, once, so
the three results can be compared with each other and with Claude Code. It
changes no code and runs no measurement.

### Two words, defined once

A host **works** when its probe passes: the host connects, and the tools it
lists respond. A host is **supported** when its recall measurement, made by the
method below, is published under `docs/measurements/`. A host can work without
being supported. [`clients.md`](../guides/clients.md) uses the words this way.
Neither word says anything about how good the result is.

### Method

- **Prompt set.** Use the gold-set format above. Tasks use invented subjects,
  are fixed before the run, and are committed with the result so it can be
  repeated. The committed example,
  `tests/evals/data/recall_metrics/gold-set-example.yaml`, is the default set.
  Add a short list of control prompts for which the corpus holds no relevant
  page; they sit beside the gold set, not in it, because a gold task needs a
  relevant page.
- **Corpus.** A seeded throwaway corpus, never a live one. The example set's
  corpus is `tests/evals/data/corpus/core`. Every gold task has its relevant
  pages in it, and decoys stay unlabelled.
- **Runs.** Each task starts in a fresh conversation on the host, with the
  athenaeum connection and any standing instruction held the same across the
  prompt set. When a host offers several configurations (for example no
  instruction, a user instruction, a server instruction, a session-start
  snapshot), each is its own arm over the same prompts, and arms are reported
  side by side, never pooled. The Claude Code arm runs the same prompts and
  corpus with the hook on, and is scored with the scorer above.
- **Evidence.** The model's own `recall` and `read_entity` calls and their
  results, and the conversation text. Where the host does not expose a
  transcript, the operator copies the conversation out by hand. Where that is
  not possible either, "used" is not measured for that host and the result says
  so.
- **How a pull is counted.** A pull is a delivery, not a push. A page returned
  by the model's `recall` call, or opened by `read_entity`, is a *delivered*
  and *engaged* item, which is how the definitions above already treat the
  recalled set. A pull is never added to push coverage, and never counted as a
  push, even when an instruction made the model do it.

### Which metrics apply

| Metric | On a host with no hook |
| --- | --- |
| Recall-through | Applies. The headline number: relevant pages the model recalled or opened / relevant pages. |
| Use precision | Applies, with the same "used" definition and the same unvalidated-proxy caveat. |
| Session use rate | Applies, with a pulled page as the delivered item. |
| Push coverage | Cannot be measured: nothing is pushed. Reported as undefined, never 0. |
| `recall_through_of_pushed` | Cannot be measured, for the same reason. |
| `opened_without_push` | Not meaningful: every open is without a push. Not reported. |
| Raw push-use and push volume | Cannot be measured. Not reported. |

In place of the unmeasurable ones, report two rates from the same runs:

- **Pull rate**: turns on which the model called `recall` / turns whose task
  has a relevant page in the corpus.
- **Needless pull rate**: turns on which the model called `recall` / control
  turns.

With a session-start snapshot arm, also report the added latency at session
start, and whether each relevant page was already in the snapshot.

Report sample sizes (tasks and turns), the same two intervals as above where
they can be computed, the date, the host app or client version, and the model
if the host shows it. Hosted models change without notice, so a result is a
dated observation of that day's model and is not carried forward.

### What this sets, and what it does not

This section sets no pass bar and says nothing about what number is good
enough. Where a threshold applies, it comes from the decision record
(athenaeum#2312). A measurement note reports numbers and the arm they came
from; the wording a client guide may use for the host follows from the
published result and the host's design note.
