# tests/evals — live-API eval suite (issue athenaeum#331)

Two layers of test that share one recording pipeline.

## Layer 1 — live-API evals (`pytest -m eval`)

Deselected by default (see `pyproject.toml` `addopts = "... -m 'not eval'"`),
so regular contributor test runs and the `develop` CI job never touch the
network. The suite runs only from the `evals.yml` workflow, and only on
**manual `workflow_dispatch`** — never on push. Dispatch it when a change
to a prompt, a model tier, the compile pipeline, the recall path, or the
sidecar could move a result; a docs or tooling change does not warrant a
run. (The same workflow's token-free `embedding-suite` job still runs on
push to `main`; see the workflow header.)

### The `Evals:` receipt line (issue athenaeum#1731)

Manual dispatch is easy to forget, so `.github/workflows/eval-receipt-check.yml`
(token-free, no Anthropic key — see that workflow's header) checks every PR
to `develop` for whether it touches the **LLM surface**, the list kept in
`.github/llm-surface.txt`. When it does, the PR body must carry one of:

- **`Evals: <run-url>`** — a link to an `evals.yml` `workflow_dispatch` run
  (e.g. `https://github.com/Kromatic-Innovation/athenaeum/actions/runs/123456789`)
  whose `headSha` matches this PR's head commit, whose workflow is `Evals`
  (evals.yml's own `name:`), and whose `conclusion` is `success`. Get the
  URL from `gh run view <id> --json url -q .url` after dispatching, or copy
  it from the Actions tab — the **run** URL
  (`.../actions/runs/<id>`), not a **job** URL
  (`.../actions/runs/<id>/job/<job-id>`): the job-scoped form is rejected,
  since a job id isn't a run id `gh run view` can look up. A run from a
  stale head SHA, from a different event (`push` instead of
  `workflow_dispatch`), from a different workflow (e.g. a `ci.yml`
  dispatch), from a different repo, or that didn't conclude `success`
  (failed or cancelled) does not satisfy the check — re-dispatch after the
  last push.
- **`Evals: not needed — <reason>`** — for a PR that touches a surface
  file without moving anything an eval would catch (e.g. a comment-only
  edit, or this very check's own CI-only plumbing). The reason after the
  dash (`-`, `–`, or `—`) must be non-empty; a bare `Evals: not needed`
  does not pass.

The check runs on `opened`/`synchronize`/`reopened`/**and `edited`** — the
last one specifically so that adding the receipt line to an already-open
PR (a body edit, not a new commit) re-triggers the check without needing a
fresh push. It is **advisory**: it fails loudly and names the touched
files, but is not in `ci.yml`'s required-checks aggregate (matching
`embedding-pr.yml`'s convention). The decision logic lives in
`scripts/check_llm_surface_receipt.py`, exercised offline by
`tests/test_llm_surface_receipt.py`; `.github/llm-surface.txt`'s own rot
check is `tests/test_llm_surface.py`.

Layers exercised end-to-end against a real Claude API call:

| Layer     | Model                              | Golden set size | Floor  |
| --------- | ---------------------------------- | --------------- | ------ |
| Detector  | Haiku (`ATHENAEUM_CLASSIFY_MODEL`) | 10 clusters     | ≥ 8/10 |
| Resolver  | Opus (`ATHENAEUM_RESOLVE_MODEL`)   | 5 flagged pairs | ≥ 4/5  |
| Recall    | Haiku (`ATHENAEUM_TOPIC_MODEL`)    | 6 prompts       | ≥ 5/6  |
| Classify  | Haiku (`ATHENAEUM_CLASSIFY_MODEL`) | 6 raw intakes   | ≥ 4/6  |
| Merge     | Sonnet (`ATHENAEUM_WRITE_MODEL`)   | 4 merge cases   | ≥ 3/4  |
| Attachment | the whole chain (Haiku + Sonnet)  | 5 routing cases | ≥ 4/5 **(expected RED)** |
| Person-hint | the whole chain (Haiku + Sonnet), person registry ON | 5 person-mention cases | ≥ 4/5 **(expected RED)** |
| Backfill  | deferred until athenaeum#328                | —               | —      |

Attachment (issue athenaeum#1580) and Person-hint (issue athenaeum#1867) are
the two layers whose floors are **aspirational rather than descriptive**. Every
other floor above describes what the shipped librarian already scores; these
two describe what a CORRECT librarian would score, and both are red today by
design — see their module docstrings, issue athenaeum#1580 AC3 and issue
athenaeum#1867. Tuning either down to observed behaviour would make it a rubber
stamp. They are also the only layers that run the WHOLE tier chain
(`librarian.process_one`) against a materialized wiki rather than one tier in
isolation, which is why neither has a single model in the table.

The two are not variants of each other. Attachment passes **no**
`person_registry=`, so tier 0's person-registry consult never engages and that
layer cannot observe it at all; Person-hint passes one, which is what puts the
consult on the path. Person-hint's question is narrower and its trap is
sharper: on the shipped librarian the person page *does* change (it gains a
dated Notes bullet), so a grader asking merely "did the page change" would pass
every case. `tests/evals/person_hint.py` separates four outcomes instead
(`unchanged`, `notes_bullet`, `citation_only`, `footnoted_claim`) and scores
`notes_bullet` as a failure in every case. Its shipped-path baseline is
`docs/measurements/person-hint-baseline-2026-09-19.md` — **0 of 5**, every case
decided deterministically, with **zero** model calls.

Classify and Merge (issue athenaeum#552) cover `tiers.py`'s Tier-2 CLASSIFY and
Tier-3 WRITE/MERGE stages — see `docs/measurements/evals-inventory.md` for the full
module-by-module inventory of which stages warrant a live eval and why.

Each per-case test appends its outcome to the session accumulator; only
the aggregate floor is asserted, so single-case model noise does not
flake main. Per-case outcomes plus the run's `TokenUsage` land in
`eval-summary.json` at repo root, uploaded as a workflow artifact.

A run-level `TokenUsage` guard (`EVAL_TOKEN_CEILING`, see
`harness.py`) asserts the total spend at teardown so a golden set that
grows unnoticed cannot balloon cost silently.

### Content policy

All golden-set inputs (`tests/evals/data/{classify,detector,merge,recall,
resolver,write_tier_compare}/`) are **synthetic small-org scenarios** (the
invented consultancy "Thornhollow Advisory" and its invented tools/vendors —
e.g. Pagemoor, Hostmoor, Tallyfold). Nothing here originates from a live
knowledge tree, and every invented name is checked absent from BOTH the
local knowledge tree and the public web before it is adopted (issue
athenaeum#1496 — a prior invented name, "Meridian Advisory", turned out to
collide with a real firm because only the former was checked; several
vendor mentions also turned out to name real products). `tests/
test_eval_corpus_leakage.py` enforces the mechanical half of this on every
PR — see that module's docstring for exactly what it can and cannot catch.
Adding a case that quotes real client / colleague content is a
review-blocker.

This is a DIFFERENT set from `tests/evals/data/corpus/`, the procedurally
generated synthetic knowledge corpus used by the retrieval/shadow-parity
tests (`tests/test_eval_recall_floor.py`, `tests/test_supersession_recall.py`,
and friends) — same "every entity is invented" policy, but generated from
syllable pools rather than hand-authored, and documented separately in
`tests/evals/data/corpus/README.md`. Both are in scope for the leakage
guard above; neither originates from the other.

Every golden set must contain at least one **pass**, one **contradict**,
and one **escalate** case (per acceptance criteria).

### Running locally

```bash
export ANTHROPIC_API_KEY=sk-...   # a live key metered on your account
pytest -m eval tests/evals/ -v
```

Under the `claude-cli` provider (issue athenaeum#330) a local run costs $0 metered
against your Claude Code subscription. The `api` backend meters at Anthropic
list rates — expect single-digit cents per full run.

## Layer 2 — recorded-response fixtures (regular CI)

`tests/fixtures/recorded/<layer>/<case_id>.json` stores the raw response
body from a live eval run, plus the request's model id and a
**prompt hash** (sha256 of the canonicalised system + messages).

The replay tests live at `tests/test_recorded_fixtures.py` — **no `eval`
marker**, so they run on every PR. They reconstruct the same prompt the
live suite would send, feed a stub client that returns the recorded
response, and assert the parser accepts it.

### Staleness contract

Each replay test's stub client re-computes the prompt hash and compares
it to the fixture's stored hash. On mismatch it raises
`FixtureStaleError` with the exact message

> `fixture stale — re-run evals with --record: tests/fixtures/recorded/<layer>/<case_id>.json`

so a prompt edit fails the corresponding replay tests until the fixtures
are re-recorded.

### Re-recording

```bash
# Via GitHub Actions (preferred — logs live in the workflow run):
gh workflow run evals.yml -f record=true
# Then download the ``recorded-fixtures`` artifact and open a follow-up
# PR committing the drift.

# Locally (needs an ambient API key):
pytest -m eval tests/evals/ --record
git add tests/fixtures/recorded/
git commit -m "evals: re-record fixtures after prompt edit"
```

### Seeding the `decomposition` layer (issue athenaeum#1581, not yet seeded)

The layer ships with its fixture directory empty and **absent from
`tests/fixtures/recorded/seeded-layers.yml`** — the never-seeded state, which
the replay suite passes trivially (athenaeum#551). It has exactly one metered
case, `decomposed_hub_new_intake`; the layer's other three cases are
deterministic and are graded with no key in
`tests/test_eval_decomposition.py`.

Seeding it is a metered operator action. The command:

```bash
gh workflow run evals.yml -f record=true --repo Kromatic-Innovation/athenaeum
```

Then download the `recorded-fixtures` artifact from that run, commit
`tests/fixtures/recorded/decomposition/decomposed_hub_new_intake.json`, and
append to `seeded-layers.yml` in the same PR:

```yaml
  decomposition:
    date: <ISO date of the run>
    run: https://github.com/Kromatic-Innovation/athenaeum/actions/runs/<id>
```

A layer added to that manifest MUST keep a non-empty fixture directory, so
append the key only once the fixture is committed alongside it. Update
`docs/measurements/decomposition-baseline-2026-09-10.md` with Case D's
observed result at the same time — the baseline's Case D row currently reads
"not yet measured".

### Seeding the `person_hint` layer (issue athenaeum#1867, not yet seeded)

Same never-seeded state as `decomposition` above: the fixture directory ships
absent and the layer is **not** a key in
`tests/fixtures/recorded/seeded-layers.yml`, so the replay suite passes
trivially (athenaeum#551) and zero-key CI stays green and honest.

Seeding it is a metered operator action, and for this layer it is also
**premature until athenaeum#1866 lands**. On the shipped librarian every case
is claimed by tier 0 with zero model calls, so a `record=true` run would
capture no responses at all — there is nothing to seed yet. Once the routing
change lands and the cases reach tiers 1-3:

```bash
gh workflow run evals.yml -f record=true --repo Kromatic-Innovation/athenaeum
```

Then download the `recorded-fixtures` artifact, commit
`tests/fixtures/recorded/person_hint/*.json`, and append to
`seeded-layers.yml` in the same PR. A layer added to that manifest MUST keep a
non-empty fixture directory, so append the key only once the fixtures are
committed alongside it. Re-take
`docs/measurements/person-hint-baseline-2026-09-19.md` LIVE at the same time —
the offline instrument that produced it is only valid while the layer makes no
model calls.

### Re-deriving instead of re-recording (no live key available)

A real re-record needs a live `ANTHROPIC_API_KEY` (or the `claude-cli`
provider). When neither is available — as in an offline/CI-only
environment — and the prompt edit is a **pure find-and-replace rename**
(for example: an invented name in a case turned out to collide with a real
one, per issue athenaeum#1496), `scripts/rederive_recorded_fixture.py` can
mechanically re-derive the affected fixtures instead:

```bash
.venv/bin/python scripts/rederive_recorded_fixture.py --apply \
  --rename "OldName=NewName" --rename "oldname=newname"
git add tests/fixtures/recorded/
git commit -m "evals: re-derive fixtures after renaming OldName -> NewName"
```

This is legitimate ONLY for a pure substitution: the tool drives the same
call path the corresponding test drives, then proves the ONLY difference
between the old and new prompt is the declared `--rename` pairs (by
reverting the new prompt and checking it hashes to the fixture's stored
hash) before touching anything. A fixture whose prompt changed for any
other reason — reworded prose, a different scenario, an unrelated prompt
edit — is refused and stays stale; re-record it for real instead. Every
re-derived fixture carries a `rederived` provenance block recording the
pre-rename hash and a digest of the rename map, so it is never mistaken for
a fresh live recording. See the script's module docstring for the full design and
`tests/test_rederive_recorded_fixture.py` for the proof it works both ways.

## Layer 3 — containment harness (`tests/evals/containment.py`, issue athenaeum#1521)

Generic machinery for the (future) north-star arm comparison — a grid of
probes x arms x corpus-scales x replicates. Bradley-Terry fitting and the
actual arm-comparison rollouts are OUT of this layer's scope; it only
provides the containment the real comparison will need:

- **Pre-flight spend gate** (`price_grid`) — prices a planned grid against
  `athenaeum.models.TokenUsage`'s per-model rate table (the same table
  every other cost estimate in this repo uses) and refuses to start
  (`SpendCeilingExceededError`) above a declared `--max-spend`.
- **Append-only result store** (`ResultStore`, `run_grid`) — one JSONL line
  per completed cell, flushed and `fsync`'d immediately, keyed by
  `(probe, arm, corpus_scale, replicate)`. A resume reads the store once
  and never re-executes a cell already present. Appends serialise on a
  per-store lock, so `run_grid(..., workers=N)` (and `north_star_cli.py`'s
  own `--workers`, default 4) can run N cells at a time without two of them
  interleaving a partial line. Above `workers=1` the row ORDER is completion
  order, not grid order — the SET of rows is the contract.
- **Planned-count sidecar** (`write_planned_cells` / `read_planned_cells`) —
  `<store>.planned.json`, written before the first cell runs, is how a
  report tells "all 1392 cells" from "the 300 that fit before the job timed
  out" and renders a `partial: N of M cells` banner. Absent or unreadable
  means "cannot tell", which renders no banner.
- **`--scale` knob** (`build_grid`, `SCALE_BUDGETS`) — `smoke`/`small`/`full`
  cap the same four axes through the SAME function; `smoke` always
  collapses to a single cell, so it is runnable without thinking about cost.
- **Separate rollout ceiling** (`rollout_session.py`, `ROLLOUT_TOKEN_CEILING`)
  — agent-rollout token usage must never accumulate into `EVAL_TOKEN_CEILING`
  (see that module's docstring for the collision this avoids). Its own
  pytest fixture (`rollout_session`, `tests/evals/conftest.py`) and its own
  marker (`rollout`, deselected by default alongside `eval`/`embedding`) are
  registered now so the future rollout-eval work needs no wiring changes.
- **`--max-tokens`, and the ceiling derived from `--max-spend`** (issue
  athenaeum#1754) — the constant above is now only the fallback for a run
  that names neither. `north_star_cli.py --max-tokens N` sets the run's
  ceiling outright; omitted, it is derived from `--max-spend` at `--model`'s
  rate and `NORTH_STAR_CELL_TOKEN_ESTIMATE`'s input/output mix
  (`containment.tokens_for_spend`), so the dollar knob an operator already
  sets governs the token guard too. Run 35200779015 is why: dispatched at
  `--max-spend 75`, it died at 392 of 1392 cells on the 2,000,000-token
  constant with about $73 still authorized.

### Running the cli-mode spot-check (issue athenaeum#1819)

`--mode cli` spawns a real, logged-in `claude -p` for every PULL-family and
native-memory cell instead of driving the Anthropic Messages API directly
(`--mode api`, the default) -- the fidelity check against the actual
production surface. It costs real tokens, needs a local `claude` login, and
is never run in CI.

1. **Isolate the config.** Set `CLAUDE_CONFIG_DIR` to a directory that
   already carries a working `claude` login before dispatching -- cli mode
   REFUSES to start otherwise (`tests.evals.rollout._require_isolated_cli_config`),
   rather than silently falling back to your real `~/.claude.json` and
   firing your own `SessionStart` hooks inside the eval. On macOS the CLI's
   login is keychain-backed, so this directory has to come from a real
   login (for example `claude setup-token`, or copying aside a directory
   you already logged in with) -- there is no way to mint one offline that
   still authenticates; see `tests/evals/rollout.py`'s own module docstring
   for why an auto-seeded directory is not an option here.
2. **Recall is pre-approved automatically.** `build_pull_argv` passes
   `--allowedTools mcp__athenaeum__recall mcp__athenaeum__read_entity`, so a
   non-interactive PULL/PUSH_BREADCRUMB_PULL cell no longer stalls on an
   unresolved MCP permission prompt (defect 1). Nothing to set for this.
3. **On a subscription-only machine (no `ANTHROPIC_API_KEY`), set
   `ATHENAEUM_LLM_PROVIDER=claude-cli`.** `--mode cli` only routes the
   PULL-family and native-memory cells through a spawned `claude -p`; the
   run's single-shot arms (`NONE`, `PUSH_PAGES_UPPER_BOUND`,
   `PUSH_BREADCRUMB`, `ORACLE`) still call `tests.evals.harness.build_live_client`
   directly, which aborts with `RuntimeError: no LLM backend available` if
   neither `ANTHROPIC_API_KEY` nor `ATHENAEUM_LLM_PROVIDER=claude-cli` is
   set (issue athenaeum#1826: this is exactly where the first attempt of
   the 2026-09-18 round aborted). Set both env vars together for a
   subscription-only run to work end to end.
4. **Dispatch**, a full six-probe command covering the single-shot,
   push-breadcrumb, and native arms in one run:

   ```sh
   CLAUDE_CONFIG_DIR=/path/to/isolated-claude-config \
     ATHENAEUM_LLM_PROVIDER=claude-cli \
     python -m tests.evals.north_star_cli \
     --mode cli --scale smoke --corpus-scales medium \
     --search-backend vector \
     --probes abstain_unknown_policy,abstain_unknown_client,pto_allowance,confidentiality_rule,bluewater_terms,onboarding_length \
     --claude-binary claude
   ```

5. **Read the header.** The generated report prints `harness failures: N`
   (cells excluded from correctness/cost because a turn ended on an
   unresolved permission request, a `push_breadcrumb`/`push_breadcrumb_pull`
   cell delivered an empty breadcrumb for a non-abstention probe, or a
   native cell answered as an unauthenticated `claude -p` session -- see
   `RolloutRecord.harness_failure`) and `config isolated: yes|no|mixed` for
   any store carrying a cli-mode row (`RolloutRecord.config_isolated`). A
   harness failure is never graded as an ordinary miss; re-run the affected
   cells (or the whole spot-check) rather than trusting a report with a
   non-zero harness-failure count.

### What `rollout` means (issue athenaeum#1742)

`rollout` means **this test costs tokens** — it constructs a live LLM
client, spawns the real `claude` binary, or is gated on
`ANTHROPIC_API_KEY` / `ATHENAEUM_LIVE_TESTS`. It is NOT a blanket marker
for every test under `tests/evals/rollout.py` and its sibling modules: a
test that renders a report from synthetic fixtures, round-trips a payload,
or drives a stub CLI runs offline and carries no marker at all, even when
it lives in the same file family as a live one. `test_north_star_report.py`,
`test_rollout.py`, `test_north_star_cli.py`, `test_rollout_payload.py`,
`test_rollout_push_breadcrumb_spike.py`, `test_rollout_api_mode.py`,
`test_rollout_api_mode_hardening.py`, `test_rollout_mode_labelling.py`,
`test_north_star_concurrency.py`, and `test_north_star_partial_safety.py`
are all token-free and run in `ci.yml`'s default job;
`test_rollout_pull_spike.py`,
`test_rollout_native_spike.py`, and `test_rollout_native_writer_spike.py`
spawn the real `claude` binary and stay `rollout`-marked.
`tests/evals/test_containment_ci_wiring.py::test_rollout_deselected_tests_are_actually_live`
enforces this mechanically for `rollout`: every module still carrying
`pytest.mark.rollout` must reference a live client, the `claude` binary, or
a live-test env gate in its own source. `eval`- and `embedding`-marked
modules are out of that check's scope — their own `pyproject.toml` marker
reason strings already document the live cost they mean.

Try it locally: `python -m tests.evals.containment_cli` (defaults to
`--scale smoke`, zero cost, zero flags needed). Offline, machine-checked in
`tests/evals/test_containment_*.py` and `tests/evals/test_rollout_ceiling_separation.py`
— none of it runs in `ci.yml` or `evals.yml` (`tests/evals/test_containment_ci_wiring.py`).

## Layer 4 — four-arm rollout runner (`tests/evals/rollout.py`, issue athenaeum#1522)

The runner Layer 3's containment machinery was built for: one probe run
across all four memory-delivery arms — `NONE` / `PUSH` / `ORACLE` / `PULL`
(`tests.evals.rollout.Arm`) — against a materialized corpus at a chosen
scale (`run_probe_all_arms`), captured as a `RolloutRecord` per (probe, arm)
pair with a per-TURN token-usage series (not only a summed-per-task total —
issue athenaeum#1523 needs the series), tool calls, and the raw transcript.

The arms split unevenly on purpose:

- **NONE / PUSH / ORACLE** (`run_none` / `run_push` / `run_oracle`) are
  single-shot completions — only the assembled context differs. They reuse
  `tests/evals/harness.py`'s `EvalSession.observe_response` / provider call
  shape directly rather than a second provider abstraction. PUSH's context
  is exactly what `athenaeum.mcp_server.recall_search` would deliver;
  ORACLE's is the probe's ground-truth pages verbatim (the retrieval
  ceiling); NONE gets nothing.
- **PULL** (`run_pull`) is a real tool-use loop: it spawns `claude -p` with
  a scoped `--mcp-config` exposing only athenaeum's `recall` tool plus
  `--output-format stream-json --verbose`, so whether the model *chooses*
  to call recall is directly observable in the stream
  (`parse_pull_stream`). Not calling recall is a recorded outcome, never an
  error. `src/athenaeum/provider.py`'s own text-only CLI pinning
  (`--tools ""` / unscoped `--strict-mcp-config`, athenaeum#906/#775) is
  untouched — this is a sibling argv builder, not a parameterization of it.

**`ATHENAEUM_EVAL_HOOK` — which `UserPromptSubmit` hook the PUSH_BREADCRUMB
arms spawn.** `run_push_breadcrumb`/`run_push_breadcrumb_pull` build their
context by actually running a real hook subprocess
(`build_push_breadcrumb_context` → `query_hook` →
`tests.evals.rollout.resolve_user_prompt_hook`), never a Python
reimplementation of its ranking/clamp/budget logic. By default that hook is
the packaged adapter console script (`athenaeum-claude-hook`,
`src/athenaeum/claude_code_adapter.py`) — the live path since the
athenaeum#1361 cutover, resolved from the active environment (`PATH`, or a
sibling of the running interpreter), never a hardcoded path. Set
`ATHENAEUM_EVAL_HOOK=shell` to fall back to the retired
`examples/claude-code/user-prompt-recall.sh` instead — a one-release escape
hatch so the two paths can still be compared side by side in this harness;
it does not resurrect the shell implementation as a shipped default.

The PULL spike (proving the subprocess actually reaches the scoped MCP
server and that `stream-json` records the call) is reproduced as a test —
`tests/evals/test_rollout_pull_spike.py` — split into an unauthenticated
half (spawns the real `claude` binary, asserts the `system`/`init` event
names athenaeum connected with `mcp__athenaeum__recall` available; skips
cleanly if `claude` is absent) and a credential-gated half (the actual
tool-use decision; gated on `ATHENAEUM_LIVE_TESTS=1` plus `claude` on
`PATH`, mirroring `tests/regression/test_live_prompt_regression.py`'s
gating idiom). `tests/evals/test_rollout.py` covers the stream-json parser
and all four arms offline, against a committed redacted fixture
(`tests/evals/data/rollout/pull_stream_spike.jsonl`) and
`tests.conftest.FakeLLMClient` — no network, no subprocess.

`test_rollout_pull_spike.py` carries `pytest.mark.rollout` (it spawns the
real `claude` binary); `test_rollout.py` is token-free and unmarked (issue
athenaeum#1742) — see "What `rollout` means" below. Rollout token usage is
recorded on a caller-supplied `EvalSession` (typically the `rollout_session`
fixture), never `harness.EVAL_TOKEN_CEILING`'s own accumulator.

## Layer 5 — Phase 2 write path (`tests/evals/write_path.py`, `north_star_cli.py --phase2`, issue athenaeum#1726/#1830)

Layer 4 above grades a READ over an already-finished store. Phase 2 grades
the WRITE that produced one.

**The arm (issue athenaeum#1830, operator ruling on
Kromatic-Innovation/athenaeum#1791 comment 5732689494):** "the librarian is
not a fact writer and never acts as one in production; it files what Claude
(or an adapter) has already written into raw intake." So the athenaeum
write-path arm is *the native writer's own memory files, compiled by the
librarian* — `tests.evals.write_path.compile_native_memory_files` takes
`NativeWriterResult.memory_files` (`run_native_writer_dispatch`,
`tests/evals/rollout.py`), materialises them under `raw/auto-memory/` in
production intake shape (one file per native memory file; a filename that
misses `athenaeum.intake.AUTO_MEMORY_FILE_RE`'s convention is refiled under
`reference_`, a named adaptation, not a claim about what Claude wrote — see
that function's own docstring), and runs the SAME production entrypoint
`athenaeum.librarian.run` every other compile path in this suite uses.
Retention is scored on the result with
`tests.evals.north_star_report.compute_write_path_stats` — "Claude's
memories versus Claude's memories after filing," not raw observations
compiled directly.

`north_star_cli.run_phase2` always runs `native` before `athenaeum` for a
given corpus scale (the athenaeum group's input IS the native group's
output), auto-including `native` as a dependency even when
`--phase2-systems athenaeum` is passed alone. A scale whose native result is
unavailable (never run, or its materialized memory directory could not be
recovered on resume) marks the athenaeum cell a HARNESS FAILURE, never a
silent zero — see `north_star_cli._run_phase2_group`'s own docstring.

**Filing loss** (AC2): a second, separate retention row
(`tests.evals.north_star_report.FilingLossStats`/`compute_filing_loss_stats`)
scores the compiled store against the tokens the NATIVE writer itself
already retained, not the full observation stream — so filing loss is
distinguishable from Claude's own write loss. A token Claude never wrote
down at all is a native write loss (visible on the ordinary
`WritePathStats` row for `system="native"`), never a filing loss.

**Lost token ids** (AC "name the lost facts"): `WritePathStats.lost_token_ids`
names the durable planted tokens missing from the compiled store, per
system/scale, rather than leaving a reader to infer them from a bare count.

**Transient grading** (`Observation.retain=False`, issue athenaeum#1824/#1830):
a transient (temporary-outage-shaped) observation's planted token grades
CORRECT when absent from the compiled store, or present only on page(s)
carrying a short decay bucket (`daily`/`weekly`, `athenaeum.models.MEMORY_BUCKETS`)
or a near-term `valid_until` (within two weeks of the observation's own
timestamp) — and WRONG only when filed durably (no decay signal, or a
long-horizon `valid_until`). Keeping a transient fact is only a defect if it
is filed as if it will never expire.

**Diagnostic-only path:** `compile_observation_stream` (compiling raw
observations directly, bypassing the native writer entirely) is unchanged
in signature and still exported/tested — it is the job the librarian never
performs in production, so it feeds no default Phase 2 row or report
section anymore; see `docs/measurements/write-path-retention-2026-09-18.md`
for the measurement it originally produced.

Offline coverage: `tests/evals/test_write_path.py` (the compile drivers,
against `tests.conftest.FakeLLMClient`, no network), `tests/evals/test_write_path_stats.py`
(`compute_write_path_stats`/`compute_filing_loss_stats`, pure computation),
`tests/evals/test_phase2_cli.py` (CLI flags, the sibling-JSONL resume
contract, and `main()` end to end with both producers stubbed).

### CI artifacts (issue athenaeum#1834)

`evals.yml` passes `--materialize-root measurements/materialize` (a path
under `$GITHUB_WORKSPACE`, not the CLI's own default `tempfile.mkdtemp()`
outside it — see `north_star_cli.main`), so the grid's per-worker
materialized corpus trees land at
`measurements/materialize/w<slot>/<scale>-<replicate>/` and Phase 2's
groups land at `measurements/materialize/phase2/<system>-<scale>/`
(`north_star_cli._run_phase2_group`'s own `group_root`).

The `north-star-report` artifact (report markdown, result store JSONL,
planned-count sidecar, Phase 2 sibling JSONL) was previously the only
artifact this workflow uploaded, and it carries Phase 2's retention
*statistics* only — a filing-loss row
(`write_path_filing`/`FilingLossStats`, issue athenaeum#1830) names lost
token ids with no way to see which compiled page should have carried them.
A second artifact, **`north-star-phase2-artifacts`**, ships the files
themselves whenever `--phase2` ran:

| Path glob | Contents |
| --- | --- |
| `measurements/materialize/phase2/*/native/memory/**` | Each `native-<scale>` group's memory files — one topic file per page (`run_native_writer`/`run_native_writer_api`'s `memory_dir`), the native writer's raw output. |
| `measurements/materialize/phase2/*/knowledge/**` | Each `athenaeum-<scale>` group's compiled store — the librarian's wiki pages plus its own run log (`compile_native_memory_files`'s `group_root / "knowledge"` target). |

Both globs are deliberately narrower than `phase2/**`: the read grid's own
per-worker wiki trees and search-index caches (including the vector index)
share the SAME `--materialize-root` and would otherwise balloon this
artifact with data a filing-loss diagnosis never reads. The step runs
`if: always()` (a run killed mid-Phase-2 still leaves whichever
`(system, scale)` groups completed) and warns rather than fails when
neither directory exists — the common case, since `--phase2` defaults off.

## Build prerequisites

- CI pulls `ANTHROPIC_API_KEY` from 1Password at run time
  (`op://Infrastructure/anthropic-api-key/credential`, read with the
  existing org `OP_SERVICE_ACCOUNT_TOKEN` secret) — no raw key is stored
  as a GitHub secret. Until that op item is provisioned the load step is a
  no-op and the harness skips every eval case cleanly; the workflow is
  dispatch/main-push only, so it cannot break develop CI.
- Fixtures are safe to commit (synthetic-input only, per the content
  policy above).
