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
