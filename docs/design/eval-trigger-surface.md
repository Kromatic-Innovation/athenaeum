<!-- SPDX-License-Identifier: Apache-2.0 -->

# Eval trigger surface: when a paid eval run is required

**Status:** DESIGN OF RECORD (Vitruvius Specify, 2026-10-30). Amends
[`native-memory-baseline.md` §9](native-memory-baseline.md#9-cadence) — the
cadence rule ("dispatch only when a change could move the result") is unchanged;
this page makes "could move the result" mechanical instead of self-attested.
Builds on athenaeum#2206 (content-based receipt matching, in progress at the
time of writing) and does not duplicate it.

---

## 1. Summary

A paid eval run (the `eval` job of `.github/workflows/evals.yml`) should be
required only when a change could alter a **reasoning outcome**: prompt text,
model/tier/effort routing, the code that builds a model request, the code that
parses or acts on a model response, and thresholds that gate what reaches a
model.

**Recommendation (hybrid):** designate the reasoning surface at **symbol**
granularity (`path::symbol` entries in `.github/llm-surface.txt`, most of them
machine-derived by the rot check), and decide whether a PR needs an eval by a
**static structural fingerprint** of those symbols — a docstring-stripped `ast`
hash computed from `git show <sha>:<path>`, no package install, no import. When
the fingerprint does change but the author believes no outcome can move, the
escape is a **structured** `Evals: not needed — <category>: <symbols>` line
whose symbol set the gate checks against the changed set, so a reviewer
verifies the claim by reading only those symbols' diffs.

Separately, and worth more paid runs than the surface change: **dispatch once,
at the final reviewed head** (§7), on top of athenaeum#2206's content-based
receipts and a develop-merge refinement (§5.4).

## 2. Evidence (30 days to 2026-10-30)

| Measure | Value | Source |
| --- | --- | --- |
| PRs merged to `develop` | 141 | `gh pr list --state merged --search merged:>=2026-09-30` |
| ...touching a `.github/llm-surface.txt` path | 68 (48%) | intersect with today's surface file |
| ...of those, `Evals: <run-url>` receipts | 2 | PR bodies |
| ...of those, free-text `Evals: not needed — ...` | 66 | PR bodies |
| Paid `workflow_dispatch` runs of `Evals` | 23 | `gh run list --workflow evals.yml --event workflow_dispatch` |
| ...on three PR branches (athenaeum#2187, the athenaeum#2043 branch, athenaeum#2078 branch) | 19 | same |
| Token cost per run | about USD 0.61–0.63 | three `eval-summary.json` artifacts, priced below |
| Wall clock per run | about 6–7 min (one 14 min outlier) | run timestamps |

Two separate problems hide in those numbers:

1. **Paid-run churn is re-receipting, not gate fire rate.** The gate fired on
   68 PRs but only 2 of them paid; 19 of 23 runs went to three branches. On
   athenaeum#2187, no surface path differs between the first passing head and
   any of the five later heads: each was a `Merge develop` with zero surface
   delta, re-dispatched because `check_receipt` demanded `headSha == head`.
   The athenaeum#2043 branch shows the other two causes: the same head dispatched three
   times (all red) and another twice (both green), plus a rebase that brought
   in another PR's surface changes.
2. **The exemption path is unverified free text.** 66 of 68 surface-touching
   PRs passed on a self-written reason. Nothing checks it. Several read as
   reasoning-relevant by the definition in §3 — e.g. a PR whose reason is
   "control-flow fix only; a thinking-only `max_tokens` response now takes the
   existing truncation fallback" (that is code acting on a model response), and
   one changing the vector index search width (a recall threshold). The file-level
   surface makes the gate fire so often that "not needed" became the default
   answer, which is exactly when a wrong claim stops being noticed.

### Cost per run

`eval-summary.json`'s `token_usage.per_model` (written by the eval suite's
summary plugin), priced with `src/athenaeum/models.py:_MODEL_RATES_USD_PER_MTOK`
and the 1.25x cache-write / 0.1x cache-read multipliers that module documents:
Sonnet 5 about USD 0.41, Opus 5 about USD 0.11, Haiku 4.5 about USD 0.09 — about
USD 0.61 per run, identical within 3% across three sampled runs. None of the
sampled dispatches enabled the `north_star` input (its steps were skipped), which
would add up to its `north_star_max_spend` ceiling. 23 runs is about USD 14 a
month. **The money is small; the cost that matters is a 6–7 minute serial wait
per dispatch inside a lane, plus the turns spent dispatching and re-receipting.**
That sets child priority at `moscow:should`, not `must`.

## 3. What counts as the reasoning surface

A change is reasoning-relevant when it can change what a model is asked, which
model is asked, how its answer is read, or what is done with that answer:

| Class | Concrete members today |
| --- | --- |
| R1 Prompt text | every constant in `src/athenaeum/prompt_registry.py:PROMPT_META`; every file under `src/athenaeum/prompts/`; inline prompts outside the registry (e.g. the propose prompt in `src/athenaeum/paste_cleanup.py`) |
| R2 Routing | `src/athenaeum/config.py:resolve_model` and the `DEFAULT_*_MODEL` constants; per-module `_get_*_model` wrappers (`tiers.py`, `resolutions.py`, `contradictions.py`, `claim_kind.py`, `decay_bucket.py`, `query_topics.py`, `rule_proposals.py`); `src/athenaeum/provider.py:resolve_max_tokens` and `resolve_thinking`; `prompt_registry.py:_META_ROWS` (knob and `max_tokens` per prompt) |
| R3 Request building | every function that encloses a model call (e.g. `tiers.py` calls through `_timed_llm_call(lambda: client.messages.create(**params), ...)`; `batch.py` through `client.messages.batches.create`), plus named builders such as `rule_proposals.py:build_rule_proposal_request_params` |
| R4 Response handling | named parsers such as `tiers.py:parse_merge_ops_response`, and the call-site functions themselves (truncation fallbacks, retries, verdict mapping usually live there) |
| R5 Gating thresholds | constants and resolvers that decide whether or what reaches a model (relevance floors, candidate caps, similarity cutoffs) and the sidecar delivery path (`examples/claude-code/user-prompt-recall.sh`, `context.py`, `sidecar_blocks.py`, the ranking/floor functions in `search.py`) |

Not reasoning-relevant, even inside the same files: CLI plumbing, I/O and
ledger bookkeeping, observability, new config namespaces unrelated to models,
refactors that leave every designated symbol's structure intact, comments and
docstrings.

## 4. Options compared

| | (a) Finer surface | (b) Fingerprint | (c) File-level + structured exemptions |
| --- | --- | --- | --- |
| Fire precision | high where designated | high, and ignores comments/docstrings/formatting | unchanged (48% of PRs fire) |
| Who decides "needs eval" | the list | the hash | the author, then a reviewer |
| Main risk | under-designation (a helper nobody listed) | same as (a): it hashes *something*, so it inherits whatever designation it is given | exemption fatigue; already the observed state |
| Gaming resistance | moderate (drop a designation) | high if the designation is a union of base and head | low unless verified |
| Cost to build | rot-check derivation + grammar | small hashing module | vocabulary + review step |
| Fits athenaeum#2206 | needs its predicate to understand symbols | drop-in replacement for its path-intersection predicate | orthogonal |

Sub-variants considered and rejected inside (a) and (b) are in §10.

(a) and (b) are not really alternatives: a fingerprint needs a designation to
hash, and a symbol list needs a change test. (c) is required regardless, as
the escape for designated symbols that changed without behavioural effect.
Hence the hybrid.

## 5. Recommended mechanism

### 5.1 Surface file grammar

`.github/llm-surface.txt` keeps its one-entry-per-line format and gains one
form. Existing forms keep their meaning (back-compatible):

```text
src/athenaeum/prompts/              # prefix: every path under it
examples/claude-code/user-prompt-recall.sh   # whole file
src/athenaeum/tiers.py::parse_merge_ops_response   # top-level def/class
src/athenaeum/search.py::VectorBackend.build_index # method
src/athenaeum/prompt_registry.py::_META_ROWS       # module-level assignment target
```

Both readers must change in the same PR: the gate's
`scripts/check_llm_surface_receipt.py:load_surface` / `_matches` and the rot
check's `tests/test_llm_surface.py:_load_surface` parse plain paths today.

### 5.2 Designation: derived first, curated second

The rot check (`tests/test_llm_surface.py`, ordinary CI, may import
`athenaeum`) enforces **membership** — each derived set must be a subset of the
surface file, never an equality or a count — keeping today's subset-check
convention:

- **D1 call sites (new).** AST-scan `src/athenaeum/**/*.py` for calls whose
  function is an attribute chain ending in `.messages.create` or
  `.messages.batches.create`. The enclosing top-level function or method must be
  designated (or its file whole-file listed). This closes a gap the current
  oracle misses: `paste_cleanup.py` and `memory_class_backfill.py` call
  `client.messages.create` but do not mention `LLMBackend`, so
  `tests/test_llm_surface.py:_llm_backend_importers` never requires them, and
  neither is on the surface today.
- **D2 prompts.** Every `PROMPT_META` row must appear as `path::CONSTANT`
  (replaces `_prompt_registry_modules`' whole-module requirement).
- **D3 routing seam.** `config.py::resolve_model`, every module-level
  `DEFAULT_*_MODEL` assignment, every `_get_*model` function,
  `provider.py::resolve_max_tokens`, `provider.py::resolve_thinking`,
  `prompt_registry.py::_META_ROWS` — derived by AST name-pattern, not
  transcribed.
- **D4 LLMBackend importers (kept, loosened).** Each importing module must have
  at least one entry (any form), instead of a whole-file entry.
- **D5 no stale pointers.** Every `path::symbol` must resolve at HEAD.

Hand-curated entries (allowed, never required by a test) cover R4 parsers, R3
builders and R5 thresholds that no pattern finds. Each carries a one-line
comment naming why it is designated.

**Honest limit.** Designation is not transitive. An edit to an undesignated
helper that a call site uses slips through. Static call-graph closure over
`lambda: client.messages.create(**params)` in 9k–12k-line modules is either
unsound or covers everything (§10). The backstops are D1 (moving logic *out*
of a call site changes the call site's AST, so the move itself fires), review,
and curating a helper once it has been missed.

### 5.3 The fingerprint

For a designated symbol `s` at commit `c`, `fp(s, c)` is
`sha256(ast.dump(normalize(node), include_attributes=False))` where `node` is
parsed from `git show c:path` and `normalize` removes docstrings (the leading
string-constant expression of a module, class or function body). Comments and
formatting never reach the AST. Annotations are kept (dataclass fields depend
on them). For a whole-file `.py` entry, `node` is the module; for a non-`.py`
whole-file or prefix entry, `fp` is the byte hash of each file.

Sentinels: `MISSING` when the symbol or file is absent, `UNPARSEABLE` when
`ast.parse` fails. `UNPARSEABLE` at head always counts as changed.

Computed with the standard library only, inside the existing
`eval-receipt-check.yml` job (which installs nothing). The registry's own
`prompt_manifest_hash` is not used by the gate: it needs an install and an
import at two commits, and it does not see prompts outside the registry.
`ast.dump` output differs across Python minor versions, so fingerprints are
compared only within one process and are never persisted.

### 5.4 Two predicates

Let `D(c)` be the designations in the surface file at commit `c`, and
`D = D(merge_base) ∪ D(head)` — the **union**, so deleting or renaming a
designation cannot hide a change. The merge base is the same
three-dot semantics `scripts/check_llm_surface_receipt.py:_git_changed_files`
already uses.

- **FIRE(base, head):** an eval is required iff some `s ∈ D` has
  `fp(s, merge_base) ≠ fp(s, head)`. Replaces `intersect_surface` on the
  changed path list.
- **COVERS(receipt X, head H):** a passing run at `X` satisfies `H` iff for every
  `s ∈ D(X) ∪ D(H)` either
  1. `fp(s, X) = fp(s, H)` — the symbol-level form of athenaeum#2206's
     "no surface path differs" — or
  2. the PR changed `s` at neither point: `fp(s, X) = fp(s, mb(X))` and
     `fp(s, H) = fp(s, mb(H))`. The difference came from `develop`, where it was
     evaluated or exempted under its own PR.

  Clause 2 is the develop-merge refinement. It would have covered the rebase on
  the athenaeum#2043 branch that brought in another PR's `tiers.py`/`resolutions.py` changes,
  which athenaeum#2206's two-dot tree diff still treats as stale. **Accepted
  risk:** an interaction between this PR's change to `s1` and develop's
  concurrent change to `s2` is not re-evaluated. Every merge to `develop` already
  carries that risk.

athenaeum#2206's `receipt_covers_head` keeps its signature and injectable
lookup; only its predicate body changes. Hence the integration child is
blocked on athenaeum#2206.

### 5.5 Structured exemptions

When FIRE is true and no covering receipt exists, the only other passing line is

```text
Evals: not needed — <category>: <symbol>[, <symbol>...]
```

- **Closed vocabulary:**
  - `rename-only` — local or private identifiers renamed, no structural change.
  - `log-or-error-text` — only logging, warning or exception message strings
    changed.
  - `dead-code` — designated code removed that nothing reachable called.
  - `refactor-equivalent` — restructured with identical inputs and outputs to the
    model.
  - `gate-plumbing` — the change is to the gate, the surface file or the rot
    check itself.

  An unknown category fails.
- **Exact set.** The named symbols must equal the set FIRE found changed — no
  missing names, no extras. The gate prints the difference when they disagree.
- **Machine-checked where cheap.** `rename-only` re-hashes with local names
  alpha-normalised; `log-or-error-text` re-hashes with string arguments of
  `logging`/`warnings` calls and `raise` expressions blanked. The category is
  rejected if the re-hash still differs. `dead-code`, `refactor-equivalent` and
  `gate-plumbing` are reviewer-checked.
- **The legacy free-text form stops passing** once this lands. Under FIRE it is
  needed rarely enough that a structured line costs little.

### 5.6 Gaming and escape: how the claim is verified cheaply

| Escape | Defence | Verifier cost |
| --- | --- | --- |
| Wrong category on a real change | exact-set check; machine re-hash for two categories | none for machine categories |
| Unverifiable category (`refactor-equivalent`) used to dodge | gate writes each changed symbol's normalised before/after (`ast.unparse`) to `$GITHUB_STEP_SUMMARY`; the merger (author ≠ merger, enforced by `gh-orch-pr-merge.sh`) reads only those symbols | reading N function diffs, not N files |
| Drop or rename a designation | union rule (§5.4); D5 stale-pointer check | none |
| Move logic into an undesignated helper | the call site's AST changes, so the move fires | one review |
| Weaken the gate in the same PR | run the gate script from the **base** commit (`git show base:scripts/check_llm_surface_receipt.py`), so a PR's gate changes take effect only after merge | none |
| Paste a receipt from another branch or run | unchanged existing checks: same repo, `Evals` workflow, `workflow_dispatch`, `success`, plus COVERS | none |

The check is labelled advisory in `eval-receipt-check.yml`'s header (not in
`ci.yml`'s `ci-required` aggregate). However, `gh-orch-pr-merge.sh`'s check
classification counts any failed check run as failing, so in practice the check
blocks the wrapper path. This design assumes that stays true and does not
change it.

## 6. Engineering principles applied

- **Derive, don't transcribe:** designations come from AST scans and
  `PROMPT_META`, bound by membership tests. Hand-curated entries are explicitly
  non-required.
- **Fail closed at the boundary:** missing, renamed and unparseable symbols
  count as changed; the designation union makes removal visible.
- **One list, two readers:** `.github/llm-surface.txt` stays the single source
  for both the gate and the rot check.
- **Prompt text is content** (workspace policy): this design indexes prompts
  wherever they live and does not move them. `paste_cleanup.py`'s inline
  prompt is surfaced by D1 regardless of whether it is later moved to a file.

## 7. Agent workflow rule: dispatch once, at the final reviewed head

**The rule.** Dispatch `evals.yml` only when all three hold:

1. Review is complete, and no further change to a designated symbol is
   planned.
2. The gate reports FIRE with no covering receipt.
3. No passing run already covers the head (COVERS).

Never re-dispatch:

- because of a `develop` merge or rebase — COVERS handles it;
- for a head that already has a run in flight or completed — at most one retry
  of a red run, then treat red as a finding;
- per push while iterating on a red eval, unless the push changes a designated
  symbol.

Iterating on a genuinely red eval is legitimate, at one run per
reasoning-changing push.

**Where it lives.** One canonical statement, then pointers:

| Location | Content |
| --- | --- |
| `tests/evals/README.md` (athenaeum) | **Canonical.** The rule, the COVERS semantics, the exemption vocabulary. |
| `scripts/check_llm_surface_receipt.py` failure text | The rule in one sentence. This is what the dispatching agent actually reads at decision time. Every athenaeum#2187 dispatch came from an implementation-lane bot, and no workspace skill mentions eval dispatch, so the gate's current "the dispatch is stale, re-run it" message is the most likely driver of per-push re-dispatch. |
| code-workspace-config: `promotion-runner` skill, `pr-shepherd` agent, `dijkstra` skill | A pointer that names the trigger and the stakes, and leaves the rule in athenaeum, per that repo's skill-edit-review policy. |
| hestia: `hestia-ops` skill | The same pointer, for overnight builders. |

## 8. Expected reduction

Classifying the 23 runs from §2 against what each mechanism would have removed:

| Mechanism | Runs removed | Which |
| --- | --- | --- |
| athenaeum#2206 (content-based receipt) | about 8 | athenaeum#2187: 5 develop merges + 1 tests-only push; athenaeum#2043 branch: 2 heads with zero surface delta from a passing head |
| §7 dispatch rule | about 4 | athenaeum#2043 branch: 3 same-head duplicates; athenaeum#2078 branch: pre-review dispatch |
| COVERS clause 2 | 1 | athenaeum#2043 branch: rebase onto another PR's surface change |
| Symbol-level FIRE | about 0 removed, 2–5 added | every paid PR genuinely changed prompts or call sites; some free-text exemptions would now fire (audited in the designation child's ACs) |

**Net: about 23 down to roughly 12 a month (≈ 50%), most of it from athenaeum#2206 and the
dispatch rule.** The symbol-level surface buys a different thing: about 60 of
66 monthly free-text claims become mechanical non-fires needing no line at all,
and the handful that remain are structured and checked. Two baseline
dispatches on `develop` are left as they are.

One more observation for the operator: three runs went red on heads that had
no reasoning delta from a green head, so the suite appears to have a flake
rate. A retry policy should be decided deliberately (§11).

## 9. Failure classes the design must survive

| Failure | Behaviour |
| --- | --- |
| Symbol renamed or deleted | `MISSING` at head is not equal to the base hash, so FIRE triggers; D5 fails the rot check until the list is updated |
| Syntax error at head | `UNPARSEABLE` triggers FIRE; ordinary CI fails anyway |
| Receipt commit orphaned by force-push | athenaeum#2206 fetches it by SHA; unreadable means not covered (fail closed) |
| Python minor-version bump changes `ast.dump` | both sides hashed in one process; nothing persisted |
| Version-bump commit from the merge wrapper | `pyproject.toml` is not designated, so COVERS clause 1 holds |
| Designated helper missed | slips through; caught by review or a later eval; then curated (honest limit) |
| Gate script edited in a PR | runs from base, so it does not affect its own PR |

## 10. Rejected alternatives

1. **Transitive AST call-graph closure (pure (a)).** Dynamic dispatch, `lambda`
   wrappers, `**params` and cross-module calls make a static closure either
   unsound (misses calls) or near-whole-file (follows every utility). It is more
   complex than the curated list, without being more correct.
2. **Runtime fingerprint via `prompt_registry.prompt_manifest_hash()` plus
   resolved config (pure (b)).** It needs `pip install` and an import at two
   commits in a job that installs nothing today, and it misses prompts outside
   the registry. The rot check already binds the registry to the surface file,
   so the static hash gets the same coverage without the import.
3. **`# llm-surface` region markers in source.** Markers drift with edits,
   are invisible to `git show` diffs of the list, and split the "one list"
   invariant across dozens of files.
4. **Workflow `paths:` filter.** It would be a second copy of the list, and a
   skipped job reports no status — the workflow header already rejects this.
5. **Status quo with free text (pure (c)).** 66 of 68 is the measured outcome:
   the exemption becomes the default and stops being read.

## 11. Migration and rollback

Order:

1. Fingerprint module and grammar, back-compatible, with no list change.
2. Rot-check derivation and list migration.
3. COVERS predicate on top of athenaeum#2206.
4. Structured exemptions.
5. Base-commit gate execution.
6. Documentation and pointers.

Each step is independently revertible. **Rollback** is restoring a whole-file
`.github/llm-surface.txt`: the grammar stays a superset, so whole-file entries
work unchanged, and FIRE on a whole-file `.py` entry degrades to "module AST
changed", strictly coarser than today's path match minus comment-only edits.

### Open operator questions

1. **Flake retries.** Allow one automatic retry of a red run on the same
   head, or treat every red as a finding?
2. **Promotion-time backstop.** Should the promotion lane dispatch one run on
   `develop` before `develop → main`, to catch anything a non-transitive
   designation missed? About 1 run per promotion; it is still a dispatch,
   consistent with the 2026-09-16 decision.
3. **Machine-checked categories.** Ship `rename-only` and `log-or-error-text`
   re-hashing in the first exemption child, or start with exact-set checking
   only?

## 12. Children

Filed as hestia-buildable issues, each `moscow:should` (the monthly cost does
not justify `must`), with native `blocked_by` links where the blocker is an
issue. athenaeum#2206 is a pull request, which native `blocked_by` cannot
target, so the issues that depend on it are held at `~planning` with a comment
until it merges.

| Issue | Scope | Blocked by |
| --- | --- | --- |
| athenaeum#2223 | `path::symbol` grammar, static fingerprint, FIRE predicate (§5.1, §5.3, §5.4) | athenaeum#2206 (PR, held) |
| athenaeum#2224 | rot-check derivation D1–D5, list migration, 30-day replay measurement (§5.2) | athenaeum#2223 |
| athenaeum#2225 | COVERS predicate with develop-merge clause (§5.4) | athenaeum#2206 (PR, held), athenaeum#2223 |
| athenaeum#2226 | structured exemption line, set check, step-summary diffs (§5.5) | athenaeum#2223 |
| athenaeum#2227 | run the gate script from the base commit (§5.6) | — |
| athenaeum#2228 | dispatch rule in `tests/evals/README.md` and the gate message (§7) | athenaeum#2206 (PR, held) |
| code-workspace-config#3552 | pointers in promotion-runner, pr-shepherd, dijkstra (§7) | athenaeum#2228 |
| hestia#2834 | pointer in hestia-ops (§7) | athenaeum#2228 |
