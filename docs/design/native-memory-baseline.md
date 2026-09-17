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

It follows that there is almost certainly a corpus size **below which
Athenaeum is not worth using**: while everything fits the native index, a
compiler and a search stack are overhead. Finding that cutoff is an explicit
output of the comparison (§6, crossover scale), and once measured it belongs
in the README's "is this for me" gate as a number, so a reader with a small
corpus is told plainly to use their agent's own memory.

## 4. The arms

Two native arms join the existing grid. Both are genuine tool-use loops. The
**primary implementation is API-backed**: the Anthropic Messages API with a
`recall` tool for the PULL-style arms and a `grep`/`read` tool pair served by
the harness over the materialised directory for the native arms. That lets
the whole grid run from the evals workflow's manual dispatch with the key it
already loads, and keeps the live run off the operator's plate (operator
preference, 2026-09-16). The existing `claude -p` path stays as an optional
fidelity spot-check: it is the only way to observe Claude Code's own index
load and its own tools, so it is worth one run to confirm the API-mode
numbers, not the path the decision rests on. The report states which mode
produced each cell.

Selected via `run_probe_all_arms(mode=...)` or `tests/evals/north_star_cli.py
--mode` (falls back to the `ATHENAEUM_EVAL_MODE` env var, default `api`) —
`"api"` or `"cli"`, the same two values named wherever mode appears
(issue athenaeum#1733).

| Arm | Store | Index | Model's read path |
|---|---|---|---|
| `NATIVE_INDEX` | corpus pages materialised as topic files in an auto-memory directory | `MEMORY.md` built as one `name — description` line per page, then truncated exactly as Claude Code truncates it | index in context; topic files on demand |
| `NATIVE_GREP` | same files | none | file search and read tools only |

`NATIVE_INDEX` at a scale past the cap is the honest picture of what native
memory becomes at that scale, so it runs at every scale; the truncation is
the finding, not a confound. `NATIVE_GREP` isolates the no-index case so a
result can say whether the index helped at all.

In API mode the harness builds the index and applies the documented
truncation itself (first 200 lines, then a further cut by STRING LENGTH --
not UTF-8 bytes -- within that window, matching the real loader), pinned by
a test, and injects it as the first user turn to mirror Claude Code's load,
appending a reproduction of Claude Code 2.1.274's own truncation-notice
template when the cap actually bound (N/X/M/L filled from the actual
truncation performed; recorded as `truncated_by_harness`, since a harness
truncation is a distinct fact from `truncated_by_claude_code`). In CLI mode
the auto-memory directory is supplied through `autoMemoryDirectory`
in a settings file passed to the subprocess, so Claude Code performs its own
load and truncation.

Each API-mode arm's system prompt mirrors what its real counterpart actually
gives the model: the `PULL`-style arms are told a `recall` tool exists over
the knowledge base and to use it before concluding it does not know, using
the MCP server's own `recall` tool description and parameters
(`query`/`top_k`/`with_pii`/`history`/`type`) rather than a paraphrase; the
native arms are told their memory directory's path and that `MEMORY.md` (when
present) is an index whose topic files are opened on demand with `grep`/
`read`, mirroring Claude Code's own auto-memory instructions rather than the
single-shot arms' "answer using only the context supplied" prompt.

The recall relevance floor ships inactive and the shell hook does not apply
it even when set (athenaeum#1665). The first run uses the floor **as
shipped**, because that is what the deployment delivers today; a second pass
with the floor active is reported alongside once the hook honours it.

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

Phase 2 is also where write cost becomes comparable. Native memory is not
free to build: the model spends tokens deciding what to save and writing
the files, inside the sessions that see the observations. That is its
compile step, paid inline. So the librarian's spend is compared against the
native writer sessions' spend on memory-saving tool calls, not against zero.
In Phase 1 neither side pays a write cost, because both are handed finished
pages, and the cost comparison there is per-turn read cost only.

**Probes must include follow-through.** A single-shot probe whose answer sits
in the surfaced page's first lines cannot tell breadcrumb delivery from
full-page delivery, and cannot tell either from a good grep. The realistic
case is a question whose complete answer needs the agent to notice a
breadcrumb, open the page behind it, and follow a link from that page to a
technical detail on a second page that the query never named. The
comparison therefore needs a probe class of that shape — answer tokens
split across two or more pages, at least one reachable only by link from a
surfaced page — run as a genuine multi-turn tool-use loop in every arm that
has tools. The existing `multi_hop` class is the starting point; the new
requirement is that the second hop is a link, not a second query term.

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
| Read cost per turn, both arms | native pays for the index (up to 25KB) on every turn plus its grep and read calls; Athenaeum pays for breadcrumbs on every turn plus its recall calls. Phase 1 compares this alone |
| Write cost, both systems (Phase 2) | the librarian's spend versus the native writer sessions' spend on memory-saving tool calls, for the same observation stream. Amortised over the full probe set at that scale (one compile serves one day's questions) and also reported raw, so a reader can re-amortise for their own turn volume |
| Turns and tool calls to answer | grep over 10,000 files is not free in wall-clock or tokens |
| **Crossover scale** | the smallest scale at which Athenaeum's correctness exceeds native's, per probe class. This is the number the decision turns on |
| **Index coverage** (`NATIVE_INDEX` only) | fraction of the corpus the truncated index still names, so a reader can see the cap bite |
| **Cost per correct answer** (per arm, scale, probe class) | total input+output tokens for the cell divided by correct answers in the cell -- read cost in Phase 1; Phase 2 adds write cost amortised over the full probe set at that scale, with the raw write spend printed alongside. Undefined (never infinite or zero) when a cell has zero correct answers |
| **Verdicts and cutoff scale** | the three §7 conditions read per scale (relationship use case won, no other use case lost, cost within 2×/1×/0.5× of the better native arm), and the smallest scale at or above `medium` where all three hold, or "none" with the failing condition named per scale |

Reported per probe class and per scale, never as one aggregate, for the
reason `tests/evals/data/corpus/README.md` gives: a single number cannot tell
"too big" from "too confusable".

## 7. The decision rule

Athenaeum continues if, at the `medium` scale and above:

1. it beats both native arms on correctness for the relationship use case
   (use-cases §2.1: `single_hop`, `multi_hop`, `disambiguation`, `temporal`
   probes that target person and company pages), and
2. it is not worse than the better native arm on any other current use case, and
3. its total cost per correct answer — read cost per turn, plus write cost
   amortised over the full probe set at that scale (Phase 2 only) — is
   within **2×** the better native arm's.

The cost factor was set by the operator on 2026-09-16, before any run, and
has three readings: **2× is the limit** (above it, condition 3 fails);
**1× is the target** (parity: provenance, the decision queue and passive
delivery come at no premium); **0.5× would be the outcome that changes the
positioning** (cheaper *and* better, which is a claim the README could then
make). The report states which reading was reached, per scale.

Winning only below the cap is not a pass: a system that is only better while
the corpus is small enough not to need it has not earned its complexity.
Losing on the relationship use case is a fail regardless of the others.

The report also states the **cutoff**: the smallest scale at which all three
conditions hold. Below it the recommendation is the agent's own memory, and
that number goes into the README.

All three conditions read the SAME Athenaeum arm -- `push_breadcrumb_pull`,
the shipped configuration -- never a different, most-favourable arm picked
per condition; other Athenaeum arms still appear in the report's per-dimension
tables, just never in these verdicts (`--verdict-arm` overrides the default).
"Better native arm" means two different things across these conditions, both
the harshest reading available to Athenaeum: for condition 1, the native arm
with the higher pooled correctness rate; for condition 3, the native arm with
the cheaper *defined* cost per correct -- a native arm that scored zero
correct answers has no defined cost and is never picked as "cheaper" by that
alone. When the better native arm scored zero correct answers on a probe
class while the verdict arm scored at least one, condition 3 reads
`native-zero`, a pass stated in words rather than a fabricated ratio; if both
sides scored zero, it is `undefined` and fails. When a probe class has no
native rows at a scale at all, condition 3 skips that class (named, counted)
rather than failing the scale outright -- the same treatment condition 2
gives a one-sided class; the scale fails condition 3 only when no class at
it has any native cost data to compare.

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
