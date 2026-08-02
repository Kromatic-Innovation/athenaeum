# Evals inventory (issue athenaeum#552)

Operator decision (hard constraint, do not relitigate): the project is
deliberately **eval-light**. Live-API evals under `tests/evals/` stay
deselected by default (`-m 'not eval'`), run only via `evals.yml`
(workflow_dispatch / push:main), and are never a required per-PR check.
This document inventories which `src/athenaeum/` modules *warrant* a basic
eval under that philosophy, so the answer to "should X get one" is written
down once instead of re-litigated per module.

**Criterion for "warrants an eval":** the module's correctness depends on
the LLM's **output shape** in a way that can silently drift (a live model
starts emitting a structurally-different-but-plausible response the
hand-rolled parser doesn't expect), **or** on a **scoring/threshold
judgment** (does the model's classification/verdict land in the right
bucket often enough) that a unit test with a stubbed, fixed response
cannot pin — because a stub only proves the parser is well-behaved *for a
canned reply*, not that the live model still tends to produce a reply the
parser and the downstream policy can act on correctly.

Modules that call the Anthropic API but are already fully exercised by an
existing eval, or that are deterministic and merely *consume* an LLM's
prior output, are marked accordingly rather than re-covered.

## Inventory table

| Module / call site | Calls LLM? | Warrants eval? | Already covered? | Reasoning |
|---|---|---|---|---|
| `contradictions.detect_contradictions` | Yes | Yes | **Yes** — `test_detector_eval.py` | Detects contradiction + classifies `conflict_type`; output-shape + judgment-quality dependent. |
| `resolutions.propose_resolution` | Yes | Yes | **Yes** — `test_resolver_eval.py` | Action-class judgment (`not_a_conflict` / `keep_*` / `disambiguation` / `propose_merge`); same reasoning. |
| `query_topics.extract_topics` | Yes | Yes | **Yes** — `test_recall_eval.py` | Free-text-to-topic-list extraction feeding keyword recall; shape + relevance judgment. |
| `tiers.tier2_classify` (CLASSIFY) | Yes | Yes | **No — added here** | Extracts a JSON array of structured entities (name/type/tags/access/observations) from raw, unstructured intake text. The entity SET and per-entity fields the model chooses to extract is a judgment call no unit test (which only feeds a canned response) can validate; a live floor is the only way to know the classifier still extracts the right entities from novel prose. |
| `tiers.tier3_create` / `tier3_merge` / `tier3_merge_full` (WRITE / MERGE) | Yes | Yes | **No — added here** | Two coupled judgment calls: (1) the CREATE prose must read as a faithful, well-formed wiki entry; (2) the MERGE ops list (anchored `replace`/`insert_after`/`append_section` operations, or an `ESCALATE:` branch) has a shape a live model can drift away from in ways existing stub-based unit tests (`test_tiers.py`) cannot detect, since they only ever feed hand-authored canned responses. Ops-list SHAPE (does it stay a small, appliable patch) and the escalate-vs-merge judgment both matter and are exactly the "output shape / threshold judgment" criterion. |
| `tiers.tier1_programmatic_match` | No | No | — | Pure regex/word-boundary matching over a pre-built index. Deterministic, unit-testable exactly. |
| `tiers.tier4_escalate` / human-escalation queue mechanics | No | No | — | Markdown rendering, fingerprint-based dedup, auto-apply threshold gating — no LLM call at all. |
| `tiers.reresolve_open_questions` | Indirect | No (no new eval) | Covered transitively | Delegates the actual judgment entirely to `resolutions.propose_resolution` / `contradictions.detect_contradictions` (already evaled); its own logic (block reconstruction, ordering, budget) is deterministic. |
| `claim_kind.classify_claim_kind` | Yes | **Borderline — declined for now** | No | Single-label classification from a closed 6-item vocabulary (`fact/observation/opinion/decision/policy/definition`), gating the resolver's `attribute_both` short-circuit. This *is* a scoring/threshold-shaped judgment call, and a case could be made for a small eval. Declined here to stay conservative/eval-light: (a) it is a much narrower judgment surface than CLASSIFY/MERGE (one label vs. a whole structured extraction or edit-ops list), (b) it already has thorough fail-open unit coverage (`tests/test_claim_kind.py`) for every shape-drift failure mode the parser defends against, and (c) the issue's own scope named CLASSIFY/WRITE-MERGE as the concrete gaps to fill, asking only that this module be *checked*, not presumptively covered. Flagged here explicitly as the one call I'm least certain about — see report footer. |
| `reasoning_tiers.py` (T1 reject/pass-up, T2 approve/amend/draft/escalate) | Yes | **Borderline — declined for now** | No | A real fuzzy judgment (T1's "different entities" reject call; T2's safe-class approve/amend/draft/escalate) that gates auto-applying a merge. Not added because: (a) `DEFAULT_TIER_CHAIN` is empty and both tiers are opt-in behind a config flag that defaults OFF — the module is not on the default judgment path the way CLASSIFY/MERGE are; (b) the codebase's own `llm_schemas.py` observability layer explicitly defers T1/T2 schema-drift instrumentation to a separate tracked issue (athenaeum#609), i.e. the project has already decided *when* to invest further here, and that decision point hasn't arrived; (c) extensive stubbed-response unit tests already exist (`test_reasoning_tiers.py`, `test_t2_reasoning_tier.py`). Revisit if athenaeum#609 lands or the tier chain becomes non-empty by default. |
| `resolutions.propose_freetext_source_edits` | Yes | **Borderline — declined for now** | No | Proposes `{path: new_body}` edits for a human free-text ruling; has its own JSON shape and an explicit diff-size sanity bound (audit H8) as a deterministic backstop against a runaway edit. Declined for the same eval-light reasoning as `claim_kind`: it is a narrower, already deterministic-backstopped surface (the H8 ratio bound catches the worst shape drift mechanically) with solid unit coverage (`test_freetext_writeback.py`), and was not named in the issue's scope. |
| `merge_type_gate.py` | No | No | — | Pure frontmatter reads + `memory_class` set comparison to route cite-vs-merge. No LLM call anywhere in the module. |
| `cross_scope.py` | No | No | — | Cosine-similarity-over-precomputed-embeddings + scope-graph arithmetic. No LLM call. |
| `delta.py` | No | No | — | Precomputed-embedding closure/caching logic for incremental reindex. No LLM call. |
| `tiers.py` (backfill-sidecar concept) | N/A | Deferred | `test_backfill_eval.py` (stub, `@pytest.mark.skip`) | Explicitly deferred to athenaeum#328 per the existing stub's own docstring. Left untouched — not un-skipped, not re-scoped here. |
| storage/config/pii/spend/provenance modules, deterministic merge mechanics (`merge.py`, `apply_merge_ops`, `wiki_dedupe.py`, `pending_merges.py`, `authority.py`, …), all CLI command modules (`_cmd_*.py`) | No | No | — | Deterministic I/O, config resolution, redaction, ledger bookkeeping, and argument parsing. Fully pinned by ordinary unit tests; no LLM output shape or fuzzy judgment involved anywhere in this group. Not enumerated exhaustively here — none of them call the Anthropic client. |

## New evals added

Two new eval layers, mirroring the existing detector/resolver/recall structure exactly (synthetic "Meridian Advisory" cases, per-case tests that never fail individually, one aggregate-floor test, `@pytest.mark.eval`):

1. **`classify` layer** (`tests/evals/test_classify_eval.py`, `tests/evals/data/classify/cases.yaml`) — exercises `tiers.tier2_classify`. Golden set spans the entity-extraction outcome classes: a clean single-entity extraction, a multi-entity file, a placeholder-label rejection (`"Member 1"`-style), a procedural/no-entity-worthy file (expects `[]`), and an already-matched entity that should be skipped.
2. **`merge` layer** (`tests/evals/test_merge_eval.py`, `tests/evals/data/merge/cases.yaml`) — exercises `tiers.tier3_merge`. Golden set spans: a clean anchored insert, a re-confirmation that should fold into an existing footnote (not a new bullet, per athenaeum#297 dedup policy), a factual contradiction (keep-more-reliable + note discrepancy), and a principled-tension case that must produce the `ESCALATE:` branch.

Both stay fully deselected by the existing `-m 'not eval'` addopts (no `pyproject.toml` change needed — the marker registration and addopts already exist and apply to any test carrying `pytestmark = pytest.mark.eval`). Neither is wired into `ci.yml`; `evals.yml`'s `pytest tests/evals/ -m eval` picks them up automatically because it selects by marker across the whole `tests/evals/` directory, not by an enumerated file list.
