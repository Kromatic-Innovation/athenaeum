<!-- SPDX-License-Identifier: Apache-2.0 -->

# Native-memory baseline across models, Stage 1, 2026-11-21

Issue athenaeum#3061, Stage 1. Question: does the model change how a session with
only Claude Code's built-in memory reaches a stored fact, compared with
athenaeum's push and pull?

**Result in one line.** Stage 1: the arm ordering depends on the model, so Stage 2
ran on Sonnet with an Opus spot-check. Stage 2: no push shape, instruction or
plugin changes the task-shaped result; none makes the push deliver the answer on
`unprompted_push` tasks, where native memory, not the push, is what gets some
right; so the push should stay and its next fix is delivering linked pages, not
rewording the push.

Contents: Stage 1 (sections 1 to 3), the real extractor (section 4), Stage 2 on
Sonnet (section 5), Opus spot-check (section 6), verdicts and recommendation
(section 7), limits, reproduction.

## 1. What was run

Real `claude -p`, fresh empty working directory per session, the `core` probe set
(57 probes: 45 task-shaped, 12 `unprompted_push`), fts5 retrieval, an invented
corpus.

| arm | memory available |
| --- | --- |
| N. native only | the 192-page corpus as Claude Code auto memory: a `MEMORY.md` index of one `- name — first line` entry per page plus one topic file per page, under `$CLAUDE_CONFIG_DIR/projects/<encoded-cwd>/memory/`. No athenaeum MCP server, no hook. |
| P. athenaeum push + pull | `push_breadcrumb_pull` with `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` |

Both arms use the host's default tool deferral (as a real session does), not the
forced-upfront setting of athenaeum#3057's row A.

**Where N's memory goes, verified live.** The directory name is the session's
real working-directory path with every non-alphanumeric character replaced by
`-` (on macOS the real path is under `/private`, which matters). Per session the
harness checks that the stream's `init` event reports that exact path as
`memory_paths.auto`; a session where it does not is excluded as a harness
failure. No N session was excluded. The index is 192 lines, under the 200-line
load cap, so nothing is truncated. The session's memory directory is deleted
afterwards.

**Renderer.** The arm-N renderer takes a list of notes (`MemoryNote`: topic
filename, index name, index description, text). The corpus is one source of
notes; a compiled wiki can be another, for the Stage 2 takeover arm
(athenaeum#3062).

**Metrics.**

- *correct*: the grid's grader (`grade_correctness`). For a non-abstention probe
  it needs the planted fact in the answer and the answer page opened or recalled.
- *content-correct*: the planted fact is in the answer, whether or not the page
  was opened. This credits a fact the one-line `MEMORY.md` entry already carries.
- *reach*: a topic-file `Read` (N) or a `recall` call (P).
- cost per correct: `total_cost_usd` summed over kept runs, divided by correct runs.

**Extractor.** The Stage 1 P rows below ran with the hook's topic extractor in
regex-only mode (no model key in the scratch hook home). Section 4 repeats P with
the model extractor and sets the two side by side; every row from there on ran the
model extractor, and each run records which extractor ran (confirmed from the
hook's own spend ledger, never assumed). The hook's "running without a cached key"
notice is stripped from the push in both modes.

**Models and replicates.** Haiku 4.5 and the current Sonnet and Opus (the
`init` event reported the resolved names), two replicates for Haiku and Sonnet,
one for Opus, run in shuffled order. Spend: Haiku $7.36 (cap $10), Sonnet $18.88
(cap $30), Opus $20.79 (cap $45); about $0.5 more on wiring checks. Two Haiku P
runs ended on a permission request and are excluded.

## 2. Results

| subset | model | arm | runs | correct | content-correct | reach | cost | cost per correct |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| task-shaped | haiku | N | 90 | 0.144 | 0.400 | 0.111 | $3.14 | $0.241 |
| task-shaped | haiku | P | 88 | 0.557 | 0.807 | 1.000 | $2.69 | $0.055 |
| task-shaped | sonnet | N | 90 | 0.200 | 0.811 | 0.178 | $8.09 | $0.449 |
| task-shaped | sonnet | P | 90 | 0.556 | 0.856 | 1.000 | $6.31 | $0.126 |
| task-shaped | opus | N | 45 | 0.578 | 0.711 | 0.778 | $9.23 | $0.355 |
| task-shaped | opus | P | 45 | 0.489 | 0.667 | 1.000 | $6.74 | $0.306 |
| unprompted_push | haiku | N | 24 | 0.000 | 0.000 | 0.042 | $0.90 | n/a |
| unprompted_push | haiku | P | 24 | 0.000 | 0.000 | 0.250 | $0.57 | n/a |
| unprompted_push | sonnet | N | 24 | 0.208 | 0.458 | 0.292 | $2.63 | $0.526 |
| unprompted_push | sonnet | P | 24 | 0.000 | 0.208 | 0.875 | $1.85 | n/a |
| unprompted_push | opus | N | 12 | 0.250 | 0.250 | 0.917 | $2.68 | $0.893 |
| unprompted_push | opus | P | 12 | 0.000 | 0.250 | 1.000 | $2.15 | n/a |

What the table shows:

- **Haiku:** P beats N by a wide margin on task-shaped probes (0.56 against 0.14,
  content 0.81 against 0.40). Haiku rarely opens a topic file (reach 0.11) and
  mostly does not trust the index line alone.
- **Sonnet:** on the grid's rule P still leads (0.56 against 0.20), but on
  content the arms are close (0.86 against 0.81). Sonnet answers from the index
  line without opening the file in 61 of 90 N runs, so the delivery gate, not
  the fact, makes N look weak.
- **Opus:** N matches or edges P on task-shaped probes (0.58 against 0.49
  correct; content 0.71 against 0.67) and reaches the topic file in 78% of runs.
- **`unprompted_push`:** nothing is answered correctly on Haiku in either arm.
  Sonnet and Opus get some right through native memory (0.21 and 0.25) and none
  through the push (0.00), and reach the source far more often through native
  memory on Opus (0.92) than Haiku (0.04). The push arm reaches `recall` more with
  stronger models (0.25, 0.88, 1.00) without turning that into correct answers.
- **Cost:** per correct answer P is cheaper than N on Haiku and Sonnet and about
  equal on Opus.

Samples are small (12 to 24 `unprompted_push` runs per cell, one Opus replicate),
so single-digit differences there are leads, not findings.

## 3. Stage 1 decision

The rule: if the arm ordering and the `unprompted_push` gap are the same across
models, Stage 2 runs on Haiku only; otherwise Sonnet, with a spot-check on Opus.

**They differ.** The P-against-N gap on task-shaped probes shrinks from large on
Haiku to near zero on Opus, on either correctness measure, and `unprompted_push`
flips from "no arm answers" on Haiku to "native memory answers some, the push none"
on Sonnet and Opus. **Stage 2 ran on Sonnet, with a spot-check on Opus.**

## 4. The real extractor

P again with the hook's topic extractor calling the model (Haiku, through an API
key read at spawn time into the hook's environment only), same probes, next to the
regex rows from section 2. The Sonnet model-extractor rows have three replicates on
the 24 Stage 2 probes and two elsewhere.

| subset | model | extractor | runs | correct | content-correct | reach | cost per correct |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| task-shaped | sonnet | regex | 90 | 0.556 | 0.856 | 1.000 | $0.126 |
| task-shaped | sonnet | model | 102 | 0.588 | 0.824 | 1.000 | $0.120 |
| unprompted_push | sonnet | regex | 24 | 0.000 | 0.208 | 0.875 | n/a |
| unprompted_push | sonnet | model | 36 | 0.000 | 0.222 | 0.944 | n/a |
| task-shaped | opus | regex | 45 | 0.489 | 0.667 | 1.000 | $0.306 |
| task-shaped | opus | model | 90 | 0.489 | 0.700 | 1.000 | $0.307 |
| unprompted_push | opus | regex | 12 | 0.000 | 0.250 | 1.000 | n/a |
| unprompted_push | opus | model | 24 | 0.000 | 0.292 | 1.000 | n/a |

The extractor makes no difference that matters. The earlier P rows stand.

## 5. Stage 2 on Sonnet

One factor at a time against P, the 12 `unprompted_push` probes plus a fixed,
seeded sample of 12 task-shaped probes, three replicates per arm and probe (N has
three too after a top-up). All push arms use the model extractor.

| arm | what changes |
| --- | --- |
| P | the breadcrumb of today |
| PE | the breadcrumb plus an excerpt (first 600 characters) of the top hit |
| PH | the breadcrumb plus a line naming the tool, `mcp__athenaeum__recall`, and saying it may need loading |
| PEH | excerpt and hint |
| PI | a `CLAUDE.md` line in the working directory telling the agent to call `recall` before answering |
| PW | the coding plugin on (a scratch copy; its server and tools loaded as in a real install) |
| T | takeover: the corpus pages rendered as auto memory, with each index line naming the page's `related` pages ("see also"), no push, no MCP |
| N | plain native memory (section 2's arm, here on the same probes) |

**What T is.** Building notes from a compiled wiki was left for later. T renders
the same corpus pages athenaeum would compile (their frontmatter, including
`related`, is already in each topic file) and adds the graph edge to the index
line. N is the same render without the edge.

| subset | arm | runs | correct | content-correct | reach | cost | cost per correct |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| task-shaped | N | 36 | 0.111 | 0.861 | 0.167 | $3.43 | $0.857 |
| task-shaped | P | 36 | 0.667 | 0.833 | 1.000 | $2.57 | $0.107 |
| task-shaped | PE | 36 | 0.667 | 0.833 | 0.694 | $2.27 | $0.095 |
| task-shaped | PH | 36 | 0.667 | 0.833 | 1.000 | $2.52 | $0.105 |
| task-shaped | PEH | 36 | 0.667 | 0.833 | 0.750 | $2.28 | $0.095 |
| task-shaped | PI | 36 | 0.667 | 0.833 | 1.000 | $2.63 | $0.110 |
| task-shaped | PW | 36 | 0.667 | 0.833 | 1.000 | $2.97 | $0.124 |
| task-shaped | T | 36 | 0.056 | 0.889 | 0.111 | $3.61 | $1.807 |
| unprompted_push | N | 36 | 0.139 | 0.417 | 0.194 | $3.87 | $0.773 |
| unprompted_push | P | 36 | 0.000 | 0.222 | 0.944 | $2.88 | n/a |
| unprompted_push | PE | 36 | 0.000 | 0.278 | 0.944 | $2.61 | n/a |
| unprompted_push | PH | 36 | 0.000 | 0.222 | 0.917 | $2.75 | n/a |
| unprompted_push | PEH | 36 | 0.000 | 0.306 | 1.000 | $2.51 | n/a |
| unprompted_push | PI | 36 | 0.000 | 0.361 | 1.000 | $2.93 | n/a |
| unprompted_push | PW | 36 | 0.000 | 0.222 | 0.917 | $3.44 | n/a |
| unprompted_push | T | 36 | 0.083 | 0.389 | 0.222 | $4.13 | $1.378 |

Reading it:

- **Task-shaped probes do not move.** Every push arm scores the same, 0.667 strict
  and 0.833 content. The excerpt arms cut the
  `recall` rate to 0.69 and 0.75 (the agent skips the pull when the excerpt
  suffices) at the same correctness and about 11% lower cost per correct. The hint
  changes nothing, because `recall` is already called every time. The plugin does
  not lower the pull rate on Sonnet (0.92 against 0.94 on `unprompted_push`, 1.00
  on task-shaped) and costs about 20% more.
- **`unprompted_push` is not fixed by any push shape.** Strict-correct is 0 in every
  push arm. The grader needs the linked decision page named in the push, and the top
  hit is the system page, so an excerpt of it does not help. Content-correct is
  0.22 for P, 0.28 for PE, 0.22 for PH, 0.31 for PEH and 0.36 for PI. PI's 13 of 36
  against P's 8 of 36 is a lead, not a finding (Fisher exact p = 0.3); the
  others are noise.
- **Native memory gets some of these right and the push gets none.** Strict-correct
  on `unprompted_push`: N 0.14, T 0.08, every push arm 0.00 (N against P, p = 0.054).
  Content-correct: N 0.42 and T 0.39 against P 0.22 (p = 0.13 and 0.2). The agent
  finds the decision page by scanning the index; the push never shows it.
- **T against N.** Adding "see also" links to the index line changed nothing
  measurable (0.39 against 0.42 content, 0.08 against 0.14 strict).
- **Cost.** Native arms cost a third to 40% more per run than the push arms because the
  192-line index sits in context every turn; on task-shaped probes T and N answer
  from the index line without opening the file, which the strict rule does not
  credit (hence 0.06 and 0.11 strict against 0.86 and 0.89 content).

## 6. Opus spot-check

The two best Stage 2 arms by content-correct on `unprompted_push` with equal
task-shaped results (PI and PEH), plus P, and for the takeover verdict T and N.
Two replicates on the same 24 probes; P and N rows are the model-extractor and
Stage 1 rows for those probes.

| subset | arm | runs | correct | content-correct | reach | cost | cost per correct |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| task-shaped | N | 24 | 0.542 | 0.750 | 0.750 | $5.30 | $0.408 |
| task-shaped | P | 24 | 0.500 | 0.667 | 1.000 | $3.80 | $0.317 |
| task-shaped | PEH | 24 | 0.500 | 0.667 | 0.750 | $3.31 | $0.275 |
| task-shaped | PI | 24 | 0.500 | 0.667 | 1.000 | $3.87 | $0.323 |
| task-shaped | T | 24 | 0.542 | 0.708 | 0.708 | $5.55 | $0.427 |
| unprompted_push | N | 24 | 0.292 | 0.292 | 0.917 | $5.36 | $0.765 |
| unprompted_push | P | 24 | 0.000 | 0.292 | 1.000 | $4.32 | n/a |
| unprompted_push | PEH | 24 | 0.000 | 0.208 | 1.000 | $3.70 | n/a |
| unprompted_push | PI | 24 | 0.000 | 0.292 | 1.000 | $4.45 | n/a |
| unprompted_push | T | 24 | 0.250 | 0.250 | 0.958 | $5.86 | $0.977 |

On Opus the arms are indistinguishable on task-shaped probes and on content-correct
in `unprompted_push` (0.21 to 0.29). The one clear difference is again strict
correctness on `unprompted_push`: native memory 0.29 and takeover 0.25 against 0.00
for every push arm (N against P, p = 0.009; T against P, p = 0.02). PEH is the
cheapest arm per correct answer on task-shaped probes ($0.275 against $0.317 for P).

## 7. Verdicts

**Push shape and instruction for athenaeum#3032.**

- Add the top-hit **excerpt** to the push. It is the only shape with a measurable
  effect: same correctness, about 11% cheaper per correct answer on Sonnet and 13%
  on Opus (PEH), and fewer runs that call `recall` at all. It does not help
  `unprompted_push`.
- Add the **`CLAUDE.md` line** telling the agent to call `recall` first. It cost
  nothing, never hurt, and is directionally the best on `unprompted_push` on Sonnet
  (0.36 against 0.22 content, not significant; no effect on Opus). Treat it as
  cheap insurance, not a result.
- Skip the **tool-name hint**: `recall` is already called every time it is offered.
- No plugin-specific work: the plugin did not lower the pull rate on Sonnet, and
  the Haiku effect from athenaeum#3057 was not reproduced here.
- The push does not fix `unprompted_push`, and rewording it will not: the answer is
  a linked decision page that the push never delivers. The next lever to test is a
  push that carries the top hit's linked neighbours.

**Takeover against push.** Takeover does not replace the push. On task-shaped probes
the two give the same answers (content-correct 0.89 against 0.83 on Sonnet, 0.71
against 0.67 on Opus), takeover costs a third to 40% more per run and is graded as lower
strict-correct because the agent answers from the index line without opening the
page. On `unprompted_push` tasks takeover (and plain native memory) is clearly
better on strict-correctness on both models (Opus p = 0.02) and better but not
significantly on content-correct on Sonnet, because the agent scans the whole
index and finds the linked decision page, which a top-three push does not show.
So the native index has something the push lacks, a full view of what exists. That
points to a hybrid, a push that includes a compact whole-corpus index or the linked
pages, rather than a choice of one. Adding "see also" links to the index line did
not help on its own.

**What the claim rests on.** On Haiku and on the grid's strict delivery rule the
push beats native memory on task-shaped probes. On Sonnet and Opus the two are
equal on content, and native memory is better on tasks where the answer is a page
the user never asked about. The claim "athenaeum beats native memory" is therefore
supported for question-shaped tasks on small models and not for the unprompted
class on strong models.

## 8. Spend

`claude -p` runs, which go through the plan: Stage 1 about $47 (Haiku $7.36, Sonnet
$18.88, Opus $20.79; caps at the time $10, $30, $45), model-extractor P re-runs about
$28, Stage 2 on Sonnet about $35 plus $2 for the N top-up, Opus spot-check about
$27 plus $5 for the N top-up. The hook extractor's calls bill an API key: about
200 tokens per call, under $0.001 each, under $1 in total (two of the processes'
ledgers were read directly, $0.03 and $0.02).

## 9. Limits

An invented corpus whose index lines usually carry the answer in their first line
(a favourable case for native memory); the corpus has 192 pages, so the index fits
under the load cap, which a larger store would not; one Opus replicate pair; 24
`unprompted_push` runs per Opus cell and 36 per Sonnet cell, so differences of one
or two answers are noise; a 12-probe task-shaped sample; the takeover arm is
rendered from corpus pages, not a compiled wiki. The `unprompted_push` class has an
open design problem from athenaeum#3057 (the answer is a linked decision page the
push does not deliver), which is what limits every push arm here.

## 10. Reproducing

Needs a logged-in `claude` under an isolated `CLAUDE_CONFIG_DIR`. For the model
extractor, point `ATHENAEUM_EVAL_HOOK_KEY_FILE` at a `config.env`-style file with an
`ANTHROPIC_API_KEY` line; it is read into the hook's environment at spawn time only.
Without it the hook runs regex-only, and each row records which one ran.

```sh
CLAUDE_CONFIG_DIR=/path/to/isolated-claude-config \
  python -m tests.evals.native_baseline_cli \
  --models sonnet --arms N,P --replicates 2 --max-spend 30 --out stage1.jsonl

CLAUDE_CONFIG_DIR=/path/to/isolated-claude-config \
ATHENAEUM_EVAL_HOOK_KEY_FILE=/path/to/config.env \
  python -m tests.evals.native_baseline_cli \
  --models sonnet --arms P,PE,PH,PEH,PI,PW,T --probe-set stage2 \
  --plugin-path /path/to/installed/plugin --replicates 3 --max-spend 120 --out stage2.jsonl
```

Reruns with the same `--out` resume. The spend cap counts rows already in the file.

## Method

Offline tests (`tests/evals/test_native_baseline_cli.py`) pin the project-directory
encoding, the renderer, topic-file read detection, the notice stripping and the
aggregation, and, for Stage 2, the push shapes, the fixed probe sample, the
`CLAUDE.md` line and the hook-key handling. Per-run rows are not committed.
