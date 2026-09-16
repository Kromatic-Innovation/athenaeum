<!-- SPDX-License-Identifier: Apache-2.0 -->

# Native-memory baseline — the eval that decides whether this project continues

**Status:** DESIGN RECORD, operator decision 2026-09-16. Specifies the
comparison named as the kill criterion in [`../use-cases.md`](../use-cases.md)
§1. Implementation is tracked on the issues linked at the end.

---

## 1. The question

Every host agent that can run Athenaeum already has a memory of its own.
For Claude Code that is **auto memory**: a per-project directory of markdown
topic files plus a `MEMORY.md` index, written by the model itself and loaded
at session start. It costs nothing to set up and nothing to compile.

If that native memory answers the [current use cases](../use-cases.md#2-current-use-cases)
as well as Athenaeum does, then intake, the librarian, provenance, the
decision queue and the sidecar are all cost with no recall value, and the
project should be shelved. Nothing else in the eval suite tests this. The
existing six-arm rollout runner (`tests/evals/rollout.py`) compares ways of
delivering *Athenaeum's* memory; every arm reads an Athenaeum-compiled
corpus. This record adds the arms that read a native store instead.

## 2. What the baseline is

The baseline is Claude Code auto memory as documented at
<https://code.claude.com/docs/en/memory>, verified against the live docs and
Claude Code 2.1.273 on 2026-09-16:

- One directory per project containing `MEMORY.md` and one topic file per
  memory.
- **The first 200 lines or 25KB of `MEMORY.md`, whichever comes first, are
  loaded at the start of every conversation. Nothing past that is loaded.**
- Topic files are never loaded at startup. The model reads them on demand
  with its ordinary file tools.
- The model decides what to write, when, and how to keep the index short.

Not the baseline: `CLAUDE.md` (operator-authored instructions, not memory),
the Anthropic memory tool (a primitive for people building their own agents),
and third-party memory libraries. A third-party system may be added later as
a further arm; see §8.

## 3. What can be established before running anything

The index cap decides the shape of the comparison. One index line per page,
at a realistic name-plus-description length, exhausts 200 lines at roughly
200 pages and 25KB at roughly 150 to 200 pages. A real deployment reaches
tens of thousands of pages. So:

- **Below the cap** both systems have an index and the comparison is about
  quality per token: does compilation plus ranked search beat a flat list
  the model scans itself?
- **Above the cap** native memory has no index for most of the corpus. The
  model must find pages by searching the directory with its file tools. The
  comparison becomes: does the librarian plus FTS5 and vector search beat
  the model grepping a directory of flat files?

The second question is the one that decides shelving, and it is genuinely
open. The synthetic corpus already has the scales to straddle the cap:
`core` (about 130 pages) and `small` (200) sit at or under it, `medium`
(1,000) and `large` (10,000) are well past it (`tests/evals/corpus.py`,
`SCALES`).

## 4. The arms

Two native arms join the existing grid. Both are tool-use loops, run the way
the PULL arm already runs (`claude -p` with a scoped configuration and
`--output-format stream-json`, so every tool call is visible in the
transcript).

| Arm | Store | Index | Model's read path |
|---|---|---|---|
| `NATIVE_INDEX` | corpus pages materialised as topic files in an auto-memory directory | `MEMORY.md` built as one `name — description` line per page, then truncated exactly as Claude Code truncates it | index in context; topic files on demand |
| `NATIVE_GREP` | same files | none | file search and read tools only |

`NATIVE_INDEX` at a scale past the cap is the honest picture of what native
memory becomes at that scale, so it runs at every scale; the truncation is
the finding, not a confound. `NATIVE_GREP` isolates the no-index case so a
result can say whether the index helped at all.

The auto-memory directory is supplied through `autoMemoryDirectory` in a
settings file passed to the subprocess, so Claude Code performs its own
load and its own truncation; the runner never re-implements either.

## 5. Two phases, because the corpus is compiled pages

The synthetic corpus is compiled wiki pages plus probes. It has no raw
observations. That means the arms above test the **read path only**: both
systems are handed the same finished pages and asked the same questions.

That is the right first phase. It is cheap, it reuses every existing arm,
probe and scale, and it is where the index cap bites. But Athenaeum's claimed
invention is the write path (why-athenaeum, "writes are harder than reads"),
and a read-only comparison cannot see it.

**Phase 1 — read path.** Native arms over materialised pages, all scales,
all probe classes. Answers: at each scale, does Athenaeum's delivery beat the
model's own search over the same content?

**Phase 2 — write path.** A raw-observation generator emits the same stream
of observations to both systems. Native: a sequence of Claude Code sessions
with auto memory enabled, fed the observations, writing whatever the model
chooses to write. Athenaeum: the librarian compiles the same stream. Then
Phase 1's probes run against each system's own store. Answers: given the
same inputs, whose memory ends up answering the questions? Phase 2 also
produces the first measurement of the observation filter against a
baseline other than itself.

Phase 2 needs the generator to emit observations whose compiled ground truth
is known, which is a generator change, not a corpus rewrite: the hand-authored
core pages already carry `source_ref`, so the generator inverts them into
dated observations that would compile back to them.

## 6. What is measured

The dimensions already defined for the north-star report
(athenaeum#1523) apply unchanged, plus two:

| Dimension | Why it matters here |
|---|---|
| Correctness (`answer_tokens` match) | the only dimension that can say "worse" |
| Abstention correctness | native memory has no abstention behaviour; a confident wrong answer on an `abstention` probe is a failure direction the north star names |
| Cost per turn, both arms | native pays for the index on every turn; Athenaeum pays for breadcrumbs on every turn and for compilation once. Compilation cost is amortised over the probe set and reported separately so the reader can re-amortise |
| Turns and tool calls to answer | grep over 10,000 files is not free in wall-clock or tokens |
| **Crossover scale** | the smallest scale at which Athenaeum's correctness exceeds native's, per probe class. This is the number the decision turns on |
| **Index coverage** (`NATIVE_INDEX` only) | fraction of the corpus the truncated index still names, so a reader can see the cap bite |

Reported per probe class and per scale, never as one aggregate, for the
reason `tests/evals/data/corpus/README.md` gives: a single number cannot tell
"too big" from "too confusable".

## 7. The decision rule

Athenaeum continues if, at the `medium` scale and above:

1. it beats both native arms on correctness for the relationship use case
   (use-cases §2.1: `single_hop`, `multi_hop`, `disambiguation`, `temporal`
   probes that target person and company pages), and
2. it is not worse than the better native arm on any other current use case, and
3. its cost per turn, with compilation amortised, is within a factor the
   operator sets before the run and records in the report.

Winning only below the cap is not a pass: a system that is only better while
the corpus is small enough not to need it has not earned its complexity.
Losing on the relationship use case is a fail regardless of the others.

The result lands as a dated report under `docs/measurements/`, following the
convention `measurements/README.md` documents: the runner ships in-tree and
needs no credential to test; the live run is a separate operator task,
because the PULL-style arms drive a logged-in `claude` CLI.

## 8. Deliberately not done

- **No separate benchmark repository.** The corpus, runner, probes and report
  already live here; a second repo would duplicate them for a neutrality
  nobody has asked for. Revisit only if a third system (a memory library, a
  hosted service) is added as an arm.
- **No LLM judge in the decision rule.** Correctness is a planted-token
  substring match; abstention is a rule. Judged quality is a later, optional
  layer, as athenaeum#1523 already sequences it.
- **No aggregate score.** See §6.

## 9. Cadence

Every arm in this comparison spends tokens or subscription turns. It runs
**only on explicit dispatch**, when a change to a prompt, a model tier, the
compile pipeline, the recall path, or the sidecar could move the result.
Nothing in this record runs on push. Token-free checks over the runner, the
corpus and the report generator run in ordinary CI.

## 10. Related

- [`../use-cases.md`](../use-cases.md) — the questions being graded.
- [`../north-star.md`](../north-star.md) §1 — the failure directions each dimension maps to.
- [`recall-architecture.md`](recall-architecture.md) — why FTS5 and vector are both load-bearing, which is the half of the bet Phase 1 tests.
- `tests/evals/rollout.py`, `tests/evals/corpus.py`, `tests/evals/north_star_report.py` — the machinery the new arms extend.
