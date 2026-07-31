# Athenaeum — Agent Policy

<!-- BEGIN generated:promotion -->
<!-- Generated from code-workspace-config/docs/project-registry.yaml by
     scripts/sync-agents-md.mjs. DO NOT HAND-EDIT inside these markers —
     edit the registry and let the scheduled sync re-render (cwc#1624). -->

## Promotion routine (generated)

- **Promotion model:** `develop-main`
- **Branch flow:** develop → main
- **Family:** `platform`
- **Promotion policy:** `immediate`
- **DELIVERY_TARGET (ceiling):** `main`
- **Auto-promotes to `main`:** yes (no human gate on `staging`→`main`)
- **No `staging` branch:** this repo has no staging environment — the merge to `develop` is the staged state and `main` is the deploy ref.
<!-- END generated:promotion -->

<!-- BEGIN generated:principles -->
<!-- Generated from code-workspace-config/docs/engineering-principles.yaml by
     scripts/sync-agents-md.mjs. DO NOT HAND-EDIT inside these markers —
     edit the registry and let the scheduled sync re-render (cwc#1625). -->

## Engineering principles (generated)

Principles whose `scope` applies to this repo (global, its family, or itself).
Scope is an attribute; widening it is an edit to the record, not a file move.

- **clean-exit-does-not-verify-work** `(global)`: A job exiting 0 with fresh output has not proven it did its work — a script whose error handling misattributes an internal defect to an external dependency reports a benign skip and stays green; capture the last known-good run before a cutover, diff the new run against it, and read the whole log, not the last line.
- **closed-blocker-is-not-delivered-value** `(global)`: When an epic dependency edge closes, verify delivery before closing the epic; a closed blocker satisfies the edge but not necessarily the dependent acceptance criterion — check the closure reason (NOT_PLANNED work was abandoned, not done) and verify even COMPLETED blockers when the epic AC is broader than the child.
- **enum-return-branch-coverage** `(global)`: A function returning an enum-like result set (PASS/FAIL/NOT_PASS and friends) needs one unit test per discrete outcome; fixture-only end-to-end tests do not reliably exercise every decision branch.
- **external-contract-verify-before-build** `(global)`: An external-system integration (API, vault, deploy target, dialect-specific DB operator) needs an explicit contract-verification step — dry-run, smoke query, type/schema check, or fail-fast assertion — before the main operation; verify column types, auth mechanisms and API shapes rather than assuming the contract matches the issue description.
- **launchd-loaded-is-not-runs** `(global)`: A launchd/cron job showing loaded (or state=running) proves only that the OS accepted the plist, not that it executes from its installed location; drive one real run from the migrated path (kickstart, or the producer with --dry-run) and confirm a clean exit PLUS fresh output — a manual shell invocation bypasses the plist env and does not count.
- **liveness-guard-before-force-remove** `(global)`: Before force-removing a worktree or locked resource, run a liveness guard to confirm no other active holder owns it; on a live foreign lock, STOP and report the holder — never force-remove over another operator live work.
- **new-call-path-self-reentry** `(global)`: When a change introduces a NEW call path (a loopback, a proxy hop, a new cross-service call) rather than only touching an existing one, ask explicitly whether that new path can call back into itself or create a new failure surface — do not only check whether the OLD pattern recurs elsewhere.
- **no-catch-all-in-contract-drift-e2e** `(global)`: A happy-path E2E spec meant to catch API contract drift must not use a silent catch-all route handler; register explicit routes for every API call, or fail an assertion when an unmatched call appears, so a new unhandled endpoint cannot be swallowed.
- **nth-fix-same-failure-class** `(global)`: Before calling a bug fixed, check git history for prior fixes of the same failure class (git log --grep); the Nth fix for one class signals incomplete root-cause work and warrants widening scope to the real cause rather than patching another symptom.
- **operator-questions-inline-not-delivered** `(global)`: A question put to the operator must be asked inline, directly, in plain language during the conversation — not delivered as a document, file, or report to go read later; the operator cannot act on a delivered document, and this has been reported twice in one arc.
- **read-timeline-before-reverting-state** `(global)`: Before removing or reverting an unexpected label or state on an issue, read the event timeline to identify the actor; a human change is presumed intentional — surface the discrepancy and ask rather than revert — and only a misfired automated corrector warrants silent remediation.
- **realistic-input-shape-testing** `(global)`: A function resolving a repo/user identifier, path, or slug must be tested against the realistic shapes real callers pass — full owner/repo slugs as well as short names, absolute as well as relative paths, canonical as well as mixed case; a suite exercising only the simple form passes green while the function is broken for the shape production uses.
- **sibling-repo-same-framework-grep** `(global)`: A bug found in one repo running a shared framework is a prompt to grep the sibling repos running that same framework for the same class of bug — the same defect usually recurs wherever the pattern was copied.
- **tests-written-after-code-echo-bugs** `(global)`: Treat a unit test that echoes the implementation rather than the contract as a smell — test names mirroring function names, assertions reproducing internal data structures, setup copying implementation sequencing; such tests ossify whatever the code does, bugs included.
- **unfired-conditional-path-is-unverified** `(global)`: A conditional branch gated on an input that has never arrived can be correct, deployed, and dead for weeks with every structural signal green; when a change touches such a path, replay a real captured payload through the DEPLOYED binary or state plainly that the path is unexercised and say what would trigger it.
<!-- END generated:principles -->

## GitHub

- **Owner:** Kromatic-Innovation
- **Repo:** athenaeum
- **Package:** [athenaeum](https://pypi.org/project/athenaeum/) on PyPI
- **License:** Apache 2.0

## Project overview

Athenaeum is an entity-centric long-term memory system for AI agents.
Published to PyPI; tagged releases ship to real users.

## Branch policy

- Default branch: `develop`
- Tags cut from `main` after `develop → main` fast-forward
- PyPI publish workflow fires on tag push to `main`
  (`PYPI_RELEASE_ON_MAIN_TAG=true`)

**Repo metadata** (promotion model, traffic tier, Sentry projects, autonomous-loop
opt-in): see `~/Code/docs/project-registry.yaml` entry for
`Kromatic-Innovation/athenaeum`. Do not duplicate that metadata here.

## Release process

Athenaeum is on PyPI; release quality matters in a way that internal-only
repos don't impose. The README, install path, error UX, and public API
are seen by strangers who have no internal context.

Before any release tag (`vX.Y.Z`):

1. Confirm `develop` is at the intended release-candidate tip.
2. Run the workspace `/zenodotus` skill against this repo:
   ```
   /zenodotus --repo . --ref develop --version <X.Y.Z> --prior-tag <vA.B.C>
   ```
3. Zenodotus spawns a 4-persona no-context reviewer panel
   (drive-by installer, production evaluator, maintainer's maintainer,
   drive-by contributor) — each reading only the public surface
   (README, CHANGELOG, LICENSE, CONTRIBUTING, public API, tests, release
   diff) and **nothing else**. The verdict lands in
   `.zenodotus/<version>/verdict.md`.
4. Verdict gates the tag:
   - **Pass** → promote `develop → main` (fast-forward), then create
     `git tag vX.Y.Z` from `main` using the drafted
     `.zenodotus/<version>/tag-message.md` as the tag body. PyPI publish
     fires automatically on the tag push.
   - **Conditional** / **Fail** → fix the must-fix items on `develop`,
     re-run `/zenodotus`, retry.
5. Tagging stays human-triggered. Zenodotus does not run `git tag`.

The `.zenodotus/` directory is gitignored — verdict artifacts are local
record, not durable repo state.

Internal Quine review and CI remain in place; Zenodotus is **additive**, not
a substitute. Internal reviewers cannot unsee design intent; Zenodotus
reviewers cannot read it.

## Testing

- Unit + integration tests under `tests/`
- Run: `pytest`
- Coverage: `pytest --cov=athenaeum`

## Bundled skills

- `skills/adapter-authoring/SKILL.md` — **adapter-authoring**: how to build a
  custom source → raw-intake adapter against the contract in
  `docs/adapter-contract.md`. Ships inside the published package (under
  `skills/`) so any consumer — human or agent — can invoke it. Point a session
  at it whenever someone wants to feed a new external source into a knowledge
  base.

## Conventions

- Python 3.11+
- Apache 2.0 license; contributor sign-off via DCO
- Public API exposed via `athenaeum/__init__.py`; anything not in `__all__`
  is internal and subject to change without a major-version bump
