# tests/evals — live-API eval suite (issue athenaeum#331)

Two layers of test that share one recording pipeline.

## Layer 1 — live-API evals (`pytest -m eval`)

Deselected by default (see `pyproject.toml` `addopts = "... -m 'not eval'"`),
so regular contributor test runs and the `develop` CI job never touch the
network. The suite runs only from the `evals.yml` workflow — dispatch,
or push to `main`.

Layers exercised end-to-end against a real Claude API call:

| Layer     | Model                              | Golden set size | Floor  |
| --------- | ---------------------------------- | --------------- | ------ |
| Detector  | Haiku (`ATHENAEUM_CLASSIFY_MODEL`) | 10 clusters     | ≥ 8/10 |
| Resolver  | Opus (`ATHENAEUM_RESOLVE_MODEL`)   | 5 flagged pairs | ≥ 4/5  |
| Recall    | Haiku (`ATHENAEUM_TOPIC_MODEL`)    | 6 prompts       | ≥ 5/6  |
| Classify  | Haiku (`ATHENAEUM_CLASSIFY_MODEL`) | 6 raw intakes   | ≥ 4/6  |
| Merge     | Sonnet (`ATHENAEUM_WRITE_MODEL`)   | 4 merge cases   | ≥ 3/4  |
| Backfill  | deferred until athenaeum#328                | —               | —      |

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
re-derived fixture carries a `rederived` provenance block recording what
was renamed and the pre-rename hash, so it is never mistaken for a fresh
live recording. See the script's module docstring for the full design and
`tests/test_rederive_recorded_fixture.py` for the proof it works both ways.

## Build prerequisites

- CI pulls `ANTHROPIC_API_KEY` from 1Password at run time
  (`op://Infrastructure/anthropic-api-key/credential`, read with the
  existing org `OP_SERVICE_ACCOUNT_TOKEN` secret) — no raw key is stored
  as a GitHub secret. Until that op item is provisioned the load step is a
  no-op and the harness skips every eval case cleanly; the workflow is
  dispatch/main-push only, so it cannot break develop CI.
- Fixtures are safe to commit (synthetic-input only, per the content
  policy above).
