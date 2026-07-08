# tests/evals — live-API eval suite (issue #331)

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
| Backfill  | deferred until #328                | —               | —      |

Each per-case test appends its outcome to the session accumulator; only
the aggregate floor is asserted, so single-case model noise does not
flake main. Per-case outcomes plus the run's `TokenUsage` land in
`eval-summary.json` at repo root, uploaded as a workflow artifact.

A run-level `TokenUsage` guard (`EVAL_TOKEN_CEILING`, see
`harness.py`) asserts the total spend at teardown so a golden set that
grows unnoticed cannot balloon cost silently.

### Content policy

All golden-set inputs are **synthetic small-org scenarios** (the invented
consultancy "Meridian Advisory"). Nothing here originates from a live
knowledge tree. Adding a case that quotes real client / colleague content
is a review-blocker.

Every golden set must contain at least one **pass**, one **contradict**,
and one **escalate** case (per acceptance criteria).

### Running locally

```bash
export ANTHROPIC_API_KEY=sk-...   # a live key metered on your account
pytest -m eval tests/evals/ -v
```

Under the `claude-cli` provider (issue #330) a local run costs $0 metered
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

## Build prerequisites

- CI runs on the **Claude Code subscription** backend ($0), not the paid
  API. `evals.yml` pulls the subscription OAuth token from 1Password
  (`op://Infrastructure/claude-code-oauth-token/credential`, read with the
  existing `OP_SERVICE_ACCOUNT_TOKEN` repo secret) and selects
  `ATHENAEUM_LLM_PROVIDER=claude-cli`. Until that op item is provisioned
  the load step is a no-op and the harness skips every eval case cleanly;
  the workflow is dispatch/main-push only, so it cannot break develop CI.
- Fixtures are safe to commit (synthetic-input only, per the content
  policy above).
