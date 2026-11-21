<!-- SPDX-License-Identifier: Apache-2.0 -->

# Native memory against the push at realistic corpus size, 2026-11-21

Issue athenaeum#3066, following athenaeum#3061. Question: does native memory's
advantage on `unprompted_push` tasks survive once the store is big enough that
Claude Code's `MEMORY.md` index no longer fits under its load cap, and does a push
that carries linked pages close the gap?

**Result in one line.** The native advantage does not survive. At 3,000 pages
Sonnet's native session answers none of the `unprompted_push` tasks (0 of 36), while a push that carries the linked
neighbours of every hit answers 13 of 36, and it does at every size tested. On Opus
native memory holds up because it searches the memory directory itself, and the
neighbour push still matches or beats it. **Build the neighbour push.**

## 1. What was run

Sonnet (Opus for the spot-check), real `claude -p`, fresh empty working directory
per session, the model extractor for the push hook, the 24-probe Stage 2 set from
athenaeum#3061 (all 12 `unprompted_push` probes plus the fixed 12-probe
task-shaped sample), three replicates per arm and probe (two on Opus). Invented
corpus at three sizes: 192 pages (`core`, the earlier size), 1,000 (`medium`) and
3,000 (`medium3k`, a scale the harness registers for this run). The probes and their answer
pages are the same at every size; the extra pages are invented distractors.

**Native index past the cap, verified live.** At 192 pages Claude Code loads the
whole index (192 lines, no warning). At 1,000 and 3,000 pages it loads exactly 200
lines and prints its own truncation warning: all 72 native sessions at each
size (36 per subset), read from the session's memory attachment, not recomputed. Because the corpus
lists the answer pages first, the index is shuffled with a fixed seed whenever it
exceeds 200 lines, so the pages a probe needs land anywhere in it (about 7% of them
visible at 3,000 pages).

**Arms** (all but N and P are in the harness only; no product code was touched):

| arm | what the agent gets |
| --- | --- |
| N | native memory only: the index plus a topic file per page, no athenaeum |
| P | today's push: the breadcrumb |
| PEI | breadcrumb plus an excerpt of the top hit plus a `CLAUDE.md` line telling the agent to call `recall` first (the excerpt-and-instruction build of athenaeum#3032) |
| PL | breadcrumb plus the top hit's linked pages (`related` edges), name and one-line description, at most three |
| PLEI | PL plus the excerpt and the instruction line |
| PLA | breadcrumb plus the linked pages of **every** hit, pooled, deduplicated, never a page the breadcrumb already names, at most six lines |
| PLAEI | PLA plus the excerpt and the instruction line |

PLA was added after the first results: at scale the page that links to the answer is
often not the top hit (below), so linking only the top hit misses it.

**Measures.** *correct* is the grid's strict rule (the answer page was delivered:
named in the push, shown by `recall`, or opened). *content* is the planted fact in
the answer, delivered or not. *touched* (native arms) is any tool call on the memory
directory, a `Read`, `grep` or `ls`; it is wider than a topic-file `Read`, because a
native session can search the files on disk without opening one.

## 2. Results on `unprompted_push` tasks (Sonnet)

Strict-correct, with content-correct in brackets. 36 runs per cell.

| arm | 192 pages | 1,000 pages | 3,000 pages |
| --- | --- | --- | --- |
| N | 0.167 (0.389) | 0.028 (0.250) | **0.000 (0.000)** |
| P | 0.000 (0.222) | 0.000 (0.194) | 0.000 (0.250) |
| PEI | 0.000 (0.250) | 0.000 (0.306) | 0.000 (0.333) |
| PL | 0.361 (0.361) | 0.028 (0.167) | 0.028 (0.306) |
| PLEI | 0.278 (0.278) | 0.000 (0.306) | 0.111 (0.333) |
| **PLA** | **0.278 (0.278)** | **0.389 (0.389)** | **0.361 (0.361)** |
| PLAEI | 0.250 (0.250) | 0.278 (0.278) | 0.278 (0.278) |

What native memory does at size: it answers from the index when the index fits;
once truncated, the agent has to look in the memory directory for itself, and on a
coding-shaped task it does not. The share of native runs that touched the memory
directory on these tasks falls from 100% at 192 pages to 67% at 1,000 and 11% at
3,000 (96% on Opus at 3,000).

Why only linking the top hit fails at scale: the page that links to the answer is
the top hit in 8 of 12 probes at 192 pages but only 3 of 12 at 3,000 (second in 3,
third in 5, absent in 1). The answer page itself is never in the push at any size.
PLA links every hit, so it still finds it.

Sizes: 13 of 36 for PLA against 0 of 36 for N at 3,000 pages, and 14 of 36 against 1
of 36 at 1,000 (Fisher exact p < 0.001 for both); at 192 pages 10 of 36 against 6 of
36 is not a difference. PLA against P is 13 of 36 against 0 of 36 at 3,000 pages
(p < 0.001). PLAEI is no better than PLA (10 of 36 against 13 of 36 at 3,000 pages,
not significant).

## 3. Results on task-shaped probes (Sonnet)

Strict-correct, content-correct in brackets, 36 runs per cell.

| arm | 192 pages | 1,000 pages | 3,000 pages |
| --- | --- | --- | --- |
| N | 0.028 (0.833) | 0.000 (0.889) | 0.083 (0.917) |
| P | 0.667 (0.833) | 0.694 (0.861) | 0.667 (0.833) |
| PEI | 0.667 (0.833) | 0.694 (0.861) | 0.667 (0.833) |
| PL | 0.750 (0.833) | 0.778 (0.861) | 0.806 (0.889) |
| PLEI | 0.750 (0.833) | 0.750 (0.833) | 0.778 (0.861) |
| PLA | 0.750 (0.833) | 0.750 (0.833) | 0.833 (0.917) |
| PLAEI | 0.750 (0.833) | 0.778 (0.861) | 0.750 (0.833) |

Native memory still answers these on content (0.83 to 0.92) at every size, by
searching the memory directory, but it rarely opens a topic file, so it scores low
under the strict rule. Cost per run is about $0.11 for N and $0.07 for the push arms
(about 35% less); cost per correct answer for PLA at 3,000 pages is $0.087 against
$0.111 for P. The neighbour arms never cost more than P.

## 4. Opus spot-check at 3,000 pages

N, P and the best Sonnet arm (PLA), two replicates, 24 runs per cell.

| subset | arm | correct | content | reach | touched memory | cost per correct |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| unprompted_push | N | 0.292 | 0.292 | 0.792 | 0.958 | $0.949 |
| unprompted_push | P | 0.000 | 0.250 | 1.000 | n/a | n/a |
| unprompted_push | PLA | 0.375 | 0.375 | 1.000 | n/a | $0.469 |
| task-shaped | N | 0.333 | 0.750 | 0.500 | 1.000 | $0.773 |
| task-shaped | P | 0.500 | 0.667 | 1.000 | n/a | $0.319 |
| task-shaped | PLA | 0.625 | 0.708 | 1.000 | n/a | $0.265 |

Opus native memory does not collapse at size: it searches the memory directory in
96% of runs. PLA still scores higher than N (9 of 24 against 7 of 24, not
significant) at about half the cost per correct answer, and far above P (p = 0.001).

## 5. Build recommendation

**Put the linked neighbours of every hit in the push.** For each page the breadcrumb
names, list the pages its `related` edges point to (name and one-line description),
pooled, deduplicated, skipping pages already named, at most six lines. It measured
across a 15-fold range of corpus size on two models:

- It is the only arm that answers `unprompted_push` tasks under the strict rule at
  every size (0.28 to 0.39 on Sonnet, 0.375 on Opus), and the only one that beats
  native memory once the index is truncated.
- It does not hurt question-shaped tasks (0.75 to 0.83 against 0.67 for P on Sonnet)
  and is cheaper per correct answer than P and far cheaper than native.
- It is small: the mean push grows from about 310 to 380 bytes to about 560 to 580
  bytes (maximum about 1.2 KB), well inside the default push budget of 1,200 tokens
  per turn.

**Do not add the excerpt and the instruction line on top for this purpose.** PEI
(the athenaeum#3032 build) gave no gain on `unprompted_push` at any size (strict
0.00, content 0.25 to 0.33 against 0.19 to 0.25 for P), and adding them to the
neighbour push (PLAEI) did slightly worse than PLA. They stay justified on other
grounds (the excerpt cut cost per correct answer by about 11% on Sonnet in
athenaeum#3061), but they do not fix this problem; the neighbours do.

**A compact index is not needed at these sizes.** The truncated native index loses
to the push even when the model searches the directory itself, and the push with
neighbours beats it at about half the cost. The fall in native performance is the
agent not looking when the index is cut, which a compact index might repair, but the
push already does the job without carrying the whole corpus; it was not tested.

**Where it needs care.** Neighbours come from the `related` edges the compiler
writes. A store whose important pages are not linked gets no benefit, and a store
with very many links per page needs the six-line cap.

## 6. Limits

An invented corpus whose answer is always one `related` edge away from a page the
push finds; real stores will have weaker links. The distractors are generated pages,
not the long tail of a real store. 36 runs per Sonnet cell and 24 per Opus cell, so
differences of one or two answers are noise. One model per scale for the main grid
and one scale for Opus. The `core`-size N and P rows were re-run or reused from
athenaeum#3061 with the same harness. The index shuffle is seeded and fixed.

## 7. Spend

About $160 of `claude -p` runs, billed to the plan: the three scales of Sonnet
(about $116 including the native re-runs that add the memory-touched measure), and
Opus $29. The hook extractor's model calls bill an API key and came to under $0.5
in total (about 200 tokens per call, read from the hook's ledger).

## 8. Reproducing

Needs a logged-in `claude` under an isolated `CLAUDE_CONFIG_DIR`; for the model
extractor, `ATHENAEUM_EVAL_HOOK_KEY_FILE` names a `config.env`-style file (read into
the hook's environment at spawn time only).

```sh
CLAUDE_CONFIG_DIR=/path/to/isolated-claude-config \\
ATHENAEUM_EVAL_HOOK_KEY_FILE=/path/to/config.env \\
  python -m tests.evals.native_baseline_cli \\
  --models sonnet --arms N,P,PEI,PL,PLEI,PLA,PLAEI --probe-set stage2 \\
  --corpus-scale medium3k --replicates 3 --out scale-3k.jsonl
```

Repeat with `--corpus-scale medium` and `core`; for Opus use `--models opus
--arms N,PLA,P --replicates 2`.

## Method

Offline tests (`tests/evals/test_native_baseline_cli.py`) pin the shuffle, the
truncation read-back, the memory-touched check and the neighbour shapes (top hit and
every hit). Per-run rows are not committed.
