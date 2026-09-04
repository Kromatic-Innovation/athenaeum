# SPDX-License-Identifier: Apache-2.0
"""Machine-readable module -> layer assignment (issue athenaeum#1280).

Why this file exists: athenaeum#1280 (consolidating a finding from athenaeum#1133's
review) observed that ``tests/test_import_graph_acyclic.py`` only detects
import CYCLES — an L3 module importing an L4 module ("inverting" the
layering direction the codebase already documents module-by-module) passes
that guard green, because a single upward edge with no path back to its
source is not a cycle. The reviewer proved this by injecting exactly such an
edge (``rules.py`` -> ``intake_audit.py``, L3 -> L4) during review of
PR athenaeum#1156 and watching the SCC guard stay green.

There was no PRE-EXISTING single machine-readable "L0..L5" table anywhere in
the repo. What DOES exist, and has existed since long before this issue, is
a per-module ``Layering:`` (or equivalent "this module is L<N>") declaration
in nearly every ``src/athenaeum/*.py`` module's OWN docstring — e.g.
``athenaeum/rules.py``: "Layering: L3. Imports :mod:`athenaeum.intake` and
:mod:`athenaeum.corrections` (both L2)...", ``athenaeum/intake_audit.py``:
"Layering: L4 (imports :mod:`athenaeum.answers`, which is L4 ...)". The
general L0-L5 scheme itself (L0 stdlib-only primitive/leaf, L1 data model,
L2 primitive/utility, L3 service, L4 domain/pipeline, L5 presentation/CLI,
plus package ``__init__`` sitting above L5) is spelled out authoritatively
in ``docs/store-contract.md``'s "Layering" section and echoed consistently
by every module that documents its own layer.

THIS FILE is that per-module scatter, collected into one place, so a test
can consume it mechanically. **Nothing here is invented** — :data:`MODULE_LAYER`
below is a direct transcription of each module's own stated layer, read
from its module docstring. Two transcription rules, applied uniformly:

1. Where a module states a single number ("L3", "L4 domain/pipeline
   module", "L1 (data model...)"), that number is used as-is.
2. Where a module states a boundary/range ("L0/L1-boundary primitive",
   "L1/L2", "L2-ish"), the UPPER number is used — these modules describe
   themselves as sitting between two tiers, and every one of them goes on
   to say it imports only AT OR BELOW that upper tier, so the upper bound
   is the accurate ceiling for "what may this module import".

Modules with no stated layer anywhere in their own docstring are left OUT
of this table entirely (see :data:`UNDECLARED` for the list) rather than
guessed at — an omitted module is simply invisible to the boundary test in
``tests/test_layer_boundary.py``, not silently assigned a wrong layer. As
more modules grow an explicit ``Layering:`` note, extend this table; do not
infer one from import patterns alone.

Package ``athenaeum/__init__.py`` is keyed here as ``"__init__"``, matching
the module-name convention ``tests/test_import_graph_acyclic.py`` already
uses for it.
"""

from __future__ import annotations

# module name (as build_import_graph() keys it, i.e. the bare stem, or
# "__init__" for the package root) -> declared layer (int).
MODULE_LAYER: dict[str, int] = {
    # L0 -- stdlib-only primitives/leaves.
    "_lint": 0,
    "_retry": 0,
    "asserter_authority": 0,
    "atomic_io": 0,
    "ephemeral": 0,
    "json_utils": 0,
    "logconf": 0,
    "memory_class": 0,
    "owner": 0,
    "progress": 0,
    "vecmath": 0,
    # L1 -- data model.
    "authority": 1,
    "init": 1,
    "merge_type_gate": 1,  # "L0/L1-boundary primitive" -> upper bound
    "models": 1,           # models.py: "It is the L1 hub"
    "precedence": 1,
    "provenance": 1,
    "registry": 1,
    "schemas": 1,
    "scoped_claims": 1,    # "L0/L1-boundary primitive" -> upper bound
    "storage": 1,
    "store": 1,            # "L0/L1 (design note Sec6.4)" -> upper bound
    "transcript_verify": 1,  # "L0/L1-boundary primitive" -> upper bound
    # L2 -- primitives/utilities/services one tier up.
    "bounce_contract": 2,  # "L2-ish" -> upper (non-boundary) bound stated
    "config": 2,
    "corrections": 2,
    "dimensions": 2,       # "L1/L2" -> upper bound
    "intake": 2,
    "measurement_docs": 2,
    "memory_class_backfill": 2,
    "never_ingest": 2,
    "page_description": 2,
    "person_registry": 2,
    "subject_backfill": 2,
    "push_state": 2,
    "run_summary_log": 2,
    "verdicts": 2,
    "wiki_write_guard": 2,
    "zero_yield": 2,
    # L3 -- services.
    "batch_state": 3,
    "calibration": 3,
    "clusters": 3,
    "contradictions": 3,
    "cross_scope": 3,
    "delta": 3,
    "detection_state": 3,
    "erasure": 3,
    "fingerprint": 3,
    "inference_blocks": 3,
    "ingestion_gate": 3,
    "killswitch": 3,
    "llm_schemas": 3,
    "memory_index": 3,
    "off_corpus": 3,
    "outbound_pii": 3,
    "pii": 3,
    "prompt_registry": 3,
    "prompt_safety": 3,
    "provider": 3,
    "push_metrics": 3,
    "query_topics": 3,
    "rules": 3,
    "screening": 3,
    "search": 3,
    "self_resolving": 3,
    "sensitivity": 3,
    "spend": 3,
    "usage_report": 3,
    "wiki_dedupe_attribution": 3,
    # L4 -- domain/pipeline modules.
    "answers": 4,
    "auto_memory_prune": 4,
    "axiom_governance": 4,
    "backlog_price_sheet": 4,
    "batch": 4,
    "bounce_divergence": 4,
    "bounce_join": 4,
    "cluster_comparator": 4,
    "comparator": 4,
    "comparator_instruments": 4,
    "decay_sweep": 4,
    "decision_answers": 4,
    "decisions": 4,
    "dedupe": 4,
    "drain": 4,
    "filename_entity_prune": 4,
    "intake_audit": 4,
    "librarian": 4,
    "merge": 4,
    "ordinary_night_table": 4,
    "pending_merges": 4,
    "pii_restore": 4,
    "quarantine": 4,
    "recompare": 4,
    "reconcile": 4,
    "reasoning_screens": 4,
    "recurring_claims": 4,
    "repair": 4,
    "retire": 4,
    "retraction_cascade": 4,
    "rule_proposals": 4,
    "sensitivity_lint": 4,
    "shadow_linkage": 4,
    "shadow_parity": 4,
    "status": 4,
    "storage_migrate": 4,
    "surface_divergence": 4,
    "tiers": 4,
    "verdict_effects": 4,
    "wiki_dedupe": 4,
    # L5 -- presentation (CLI).
    "_cli_shared": 5,
    "_cmd_authority": 5,
    "_cmd_axiom": 5,
    "_cmd_bounce_contract": 5,
    "_cmd_calibration": 5,
    "_cmd_curate": 5,
    "_cmd_decay": 5,
    "_cmd_decisions": 5,
    "_cmd_dimensions": 5,
    "_cmd_drain": 5,
    "_cmd_enumerate": 5,
    "_cmd_explain_routing": 5,
    "_cmd_index": 5,
    "_cmd_lifecycle": 5,
    "_cmd_measure": 5,
    "_cmd_description": 5,
    "_cmd_memory_class": 5,
    "_cmd_merges": 5,
    "_cmd_subject": 5,
    "_cmd_outbound": 5,
    "_cmd_pending": 5,
    "_cmd_pii_restore": 5,
    "_cmd_push_metrics": 5,
    "_cmd_query": 5,
    "_cmd_questions": 5,
    "_cmd_reconcile": 5,
    "_cmd_repair": 5,
    "_cmd_run": 5,
    "_cmd_serve": 5,
    "_cmd_storage": 5,
    "_cmd_surface_divergence": 5,
    "_cmd_usage_report": 5,
    "_cmd_verdicts": 5,
    "cli": 5,
    # Above L5 -- the package root, which the design's own docstring says
    # "sits above L5 by necessity" (imports the CLI-adjacent librarian
    # pipeline entry points).
    "__init__": 6,
}

# Modules with NO ``Layering:``-shaped declaration anywhere in their own
# module docstring, as of this issue. Not assigned a layer (see module
# docstring above for why) and therefore invisible to the boundary check --
# listed here only so the omission is a visible, intentional fact rather
# than a silent gap. Regenerate by diffing ``src/athenaeum/*.py`` module
# stems against :data:`MODULE_LAYER`'s keys.
UNDECLARED: frozenset[str] = frozenset(
    {
        "claim_kind",
        "compiled_exempt",
        "deploy_check",
        "do_not_email_divergence",
        "drain_advisor",
        "entity_schema",
        "enumeration",
        "identity_resolution",
        "mcp_server",
        "memory_tiers",
        "name_collisions",
        "pending_merges_pii",
        "reasoning_tiers",
        "reasoning_triggers",
        "resolutions",
        "runlock",
        "sensitivity_routing",
        "sidecar_blocks",
        "store_conformance",
        "supersession",
    }
)
