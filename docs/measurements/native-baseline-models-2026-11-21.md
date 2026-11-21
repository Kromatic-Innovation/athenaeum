<!-- SPDX-License-Identifier: Apache-2.0 -->

# Native-memory baseline across models, Stage 1, 2026-11-21

Issue athenaeum#3061, Stage 1. Question: does the model change how a session with
only Claude Code's built-in memory reaches a stored fact, compared with
athenaeum's push and pull?

**Result in one line.** Yes, enough to change the Stage 2 plan. The arm ordering
and the `unprompted_push` gap are not the same across models, so Stage 2 runs on
Sonnet with a spot-check on Opus, not on Haiku alone.

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

**Extractor.** The sidecar hook's topic extractor ran in regex-only mode in the P
arm. Making it use the model needs a model API key, which was not available to
the scratch hook home, and routing it through the logged-in `claude` CLI does not
work: that call exceeds the hook's fixed 3 second budget and falls back to regex.
The hook's "running without a cached key" degradation notice is stripped from the
push, so it does not leak into the prompt as it would not on an install with a
key.

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

## 3. Decision

The rule: if the arm ordering and the `unprompted_push` gap are the same across
models, Stage 2 runs on Haiku only; otherwise Sonnet, with a spot-check on Opus.

**They differ.** The gap between P and N on task-shaped probes shrinks from large
on Haiku to near zero on Opus, on either correctness measure, and the
`unprompted_push` result flips from "no arm answers" on Haiku to "native memory
answers some, the push none" on Sonnet and Opus. A design call made on Haiku, such
as "push is clearly better than native", would not carry over.

**Stage 2 runs on Sonnet, with a spot-check on Opus.** Sonnet shows both
behaviours that matter, native recall that works through the index and a push that
does not convert reaching the source into correct answers on `unprompted_push`,
at a fraction of Opus's cost.

**What this means for the native baseline claim.** The measured native baseline is
model-dependent: weak on Haiku, and close to athenaeum's push-and-pull on stronger
models once a fact the index line carries counts. On `unprompted_push` tasks
strong models do better from native memory than from the current push. The claim
that athenaeum beats native memory holds on Haiku and on the grid's strict
delivery rule, and does not hold unconditionally on Sonnet or Opus.

## 4. Limits

An invented corpus whose index lines usually carry the answer in their first line
(a favourable case for native memory); regex-only topic extraction in P; one
Opus replicate; one corpus scale. The `unprompted_push` class has an open
design problem from athenaeum#3057 (the answer is a linked decision page that the
push does not deliver), which affects P in every model.

## 5. Reproducing

Needs a logged-in `claude` under an isolated `CLAUDE_CONFIG_DIR`.

```sh
CLAUDE_CONFIG_DIR=/path/to/isolated-claude-config \
  python -m tests.evals.native_baseline_cli \
  --models haiku --replicates 2 --max-spend 10 --out native-haiku.jsonl
```

Repeat with `--models sonnet --replicates 2 --max-spend 30` and
`--models opus --replicates 1 --max-spend 45`. A rerun with the same `--out`
resumes.

## Method

Offline tests (`tests/evals/test_native_baseline_cli.py`) pin the project-directory
encoding, the renderer, topic-file read detection, the notice stripping and the
aggregation. Per-run rows are not committed.
