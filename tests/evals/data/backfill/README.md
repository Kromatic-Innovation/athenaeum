# Backfill-sidecar eval golden set — DEFERRED until #328 lands

This directory is a deliberately-empty scaffold. Per issue #331:

> The backfill-sidecar golden set is EXPLICITLY deferred until #328 lands —
> build the detector/resolver/recall sets + harness + recording now; leave
> a clearly-marked stub directory for the sidecar set.

## When #328 lands, add here

Following the same shape as `../detector/cases.yaml` and
`../resolver/cases.yaml`:

- `cases.yaml` — ~5 synthetic transcript fixtures covering all three outcome
  classes:
  - **pass** — team member states a fact → upgrade to `user-stated`.
  - **pass** — agent derives from a shared doc → upgrade to
    `agent-observed`.
  - **pass** — claim has no transcript support → stays `inferred`.
  - **escalate** — transcript where two colleagues state conflicting values
    in one session: NO silent upgrade; the sidecar must escalate.

Each case asserts the upgrade decision (target `source_type` + upgrade
rationale category), NOT free-text rationale.

`../../test_backfill_eval.py` will be the parametrized test that consumes
`cases.yaml` and calls the backfill sidecar. Both are gated `pytest.mark.eval`
so they live in the deselected-by-default layer alongside the other three.

## Why the stub is checked in now

Two reasons:
1. The evals.yml workflow already runs `pytest -m eval tests/evals/` — an
   empty directory here means the workflow does not need editing when
   #328 lands, only the case file + test file get added.
2. A discoverable stub tells the #328 implementer where the eval slice
   goes, so it does not get bolted on separately.
