# SPDX-License-Identifier: Apache-2.0
"""Layer-BOUNDARY guard (issue athenaeum#1280, consolidating an athenaeum#1133 review finding).

``tests/test_import_graph_acyclic.py`` (issue athenaeum#545) proves the intra-package
import graph is acyclic — it runs Tarjan's algorithm and fails on any
multi-node strongly-connected component. That is a real, useful guard, but
it detects only CYCLES. A single one-directional edge that INVERTS the
codebase's documented layering (an L3 module importing an L4 module, where
nothing L4-side imports back) is not a cycle, so it passes that guard green.

This was proven concretely during review of PR athenaeum#1156 (issue athenaeum#1133): the
reviewer injected an ``import athenaeum.intake_audit`` into ``rules.py``
(L3 -> L4 -- ``rules.py`` documents itself as L3, ``intake_audit.py``
documents itself as L4) and watched ``test_import_graph_acyclic.py`` stay
green, because that one edge creates no cycle by itself.

This file is the missing guard: it reuses the SAME import graph builder as
the acyclicity test (so the two guards agree about what counts as an import
edge -- top-level AND function-local/deferred), and checks a DIFFERENT
property over it: for every edge ``A -> B`` where both ``A`` and ``B`` have
a declared layer (:data:`tests.fixtures.layer_declarations.MODULE_LAYER`),
``A``'s layer must be >= ``B``'s layer. A module may import anything at or
below its own layer; importing something strictly ABOVE its own layer
inverts the documented direction.

**Positive control (verified manually before this test was committed, not
re-run by CI -- see the PR description for the actual before/after output):**
a temporary ``import athenaeum.intake_audit`` line added to
``athenaeum/rules.py`` made ``test_no_new_layer_boundary_violations`` below
fail with exactly that edge reported; removing the line again made it pass.
That is the same injection the athenaeum#1133 reviewer performed by hand, now
mechanically re-checkable.

**Baseline is NOT empty.** Unlike the acyclicity guard (pinned to ``[]``
since issue athenaeum#640), this repo's import graph already contains
upward edges that invert the general layering scheme -- every one of them
is an existing, DOCUMENTED, deliberate exception (most are function-local/
deferred imports taken specifically to avoid a cycle; see each cited
module's own "Layering:" docstring paragraph for its stated reason).
:data:`ALLOWED_UPWARD_EDGES` pins the CURRENT set. This is a ratchet, not an
approval: it stops any NEW upward edge from being added silently, but
listing an edge here is not this issue's endorsement of it -- most are
narrow, intentional, and already explained in-line by their own module.
Shrinking this set (fixing one of the existing edges) is welcome and simply
requires deleting the corresponding line the next time it happens to be
true.
"""

from __future__ import annotations

from tests.fixtures.layer_declarations import MODULE_LAYER, UNDECLARED
from tests.test_import_graph_acyclic import SRC, build_import_graph

# Pre-existing upward edges (importer, imported), where the importer's
# declared layer is LOWER than the imported module's declared layer. Every
# entry is a real edge in today's graph, individually explained by the
# importing module's own "Layering:" docstring paragraph. Source line
# numbers below are illustrative, not enforced -- if a module reorganizes
# its imports, the edge (not the line) is what this test tracks.
ALLOWED_UPWARD_EDGES: frozenset[tuple[str, str]] = frozenset(
    {
        # asserter_authority.py (L0) imports models.py (L1) at module scope
        # for the asserter-block dataclass shape; the module's own docstring
        # calls this "nothing else at module scope" -- L0 here means
        # "no config/LLM/network", not "zero athenaeum imports".
        ("asserter_authority", "models"),
        # asserter_authority.py (L0) imports config.py (L2)'s
        # resolve_authority_grant_implications -- function-local/deferred,
        # per the module's own docstring ("the config resolver is imported
        # lazily inside ...").
        ("asserter_authority", "config"),
        # bounce_contract.py self-describes as "L2-ish" and explicitly
        # documents depending on the L3 pii/sensitivity modules directly
        # ("and on nothing above it, so the L5 librarian gate and the L5
        # CLI can both call it").
        ("bounce_contract", "pii"),
        ("bounce_contract", "sensitivity"),
        # config.py (L2) imports screening.py (L3) via a deferred,
        # function-local import -- documented on BOTH sides (config.py's
        # own note, and screening.py's "config (L2) imports THIS module via
        # a deferred, function-local import").
        ("config", "screening"),
        # corrections.py (L2) imports answers.py (L4)'s
        # parse_pending_questions, function-local, for the shared "valid
        # envelope" definition.
        ("corrections", "answers"),
        # corrections.py (L2) imports pii.py (L3), function-local per call
        # site (see corrections.py's own docstring: "_resolve_email_handle
        # and the Sec7.1 sensitivity-routing helpers").
        ("corrections", "pii"),
        # init.py (L1) imports config.py (L2)'s write_default_config,
        # function-local/deferred -- the module's own docstring states this
        # is deliberate: "to avoid a module-level L1->L2 cycle at import
        # time".
        ("init", "config"),
        # memory_class_backfill.py (L2) imports provider.py / push_metrics.py
        # / spend.py (all L3), function-local/deferred, per the module's own
        # docstring (issue athenaeum#1007 -- routes classifier calls through the
        # shared spend-recording path).
        ("memory_class_backfill", "provider"),
        ("memory_class_backfill", "push_metrics"),
        ("memory_class_backfill", "spend"),
        # page_description.py (L2) takes the identical three deferred imports
        # for the identical reason (issue athenaeum#1324 -- the description
        # backfill's batched calls route through the shared spend-recording
        # path); documented in that module's "Layering:" paragraph.
        ("page_description", "provider"),
        ("page_description", "push_metrics"),
        ("page_description", "spend"),
        # merge_type_gate.py ("L0/L1-boundary primitive") imports config.py
        # (L2) at module scope for the librarian.* merge-guardrail knobs --
        # documented in the module's own "Layering:" paragraph.
        ("merge_type_gate", "config"),
        # schemas.py (L1) imports dimensions.py (L1/L2) and pii.py (L3) --
        # the module's own docstring calls the pii import "a deliberate,
        # narrow upward reach for a single flag lookup, not a general
        # license to import service-layer policy here".
        ("schemas", "dimensions"),
        ("schemas", "pii"),
        # storage.py (L1) imports config.py (L2) at module scope to resolve
        # adapters/mapping -- the module's own docstring calls this "the one
        # place in [storage.py] that reaches up".
        ("storage", "config"),
        # verdicts.py (L2) imports off_corpus.py (L3): this is the
        # misconfigured-off_corpus fallback on the erasure-class verdict
        # routing path (issue athenaeum#984 finding A) -- a deferred,
        # function-local import inside the exception handler that decides
        # whether to write to the off-corpus ledger shard. See
        # tests/test_verdicts_off_corpus_fallback.py for the assertion this
        # branch's BEHAVIOUR (not just its existence) is correct.
        ("verdicts", "off_corpus"),
        # verdicts.py (L2) imports pii.py (documented as "L1" in verdicts.py's
        # own docstring, "for the erasure-class refusal guard" -- pii.py's
        # own docstring separately says "L3". This cross-docstring
        # inconsistency about pii.py's true layer is pre-existing and not
        # this issue's to resolve (test-only, offline; see UNRELATED_FINDINGS
        # in the PR that added this guard) -- pinned here using pii.py's own
        # self-declared L3, which is what makes this an upward edge at all.
        ("verdicts", "pii"),
    }
)


def _layer_violations() -> list[tuple[str, int, str, int]]:
    graph = build_import_graph()
    violations: list[tuple[str, int, str, int]] = []
    for importer, targets in graph.items():
        if importer not in MODULE_LAYER:
            continue
        importer_layer = MODULE_LAYER[importer]
        for imported in targets:
            if imported not in MODULE_LAYER:
                continue
            imported_layer = MODULE_LAYER[imported]
            if importer_layer < imported_layer:
                violations.append((importer, importer_layer, imported, imported_layer))
    return violations


def test_no_new_layer_boundary_violations() -> None:
    """Every upward edge (importer's layer < imported's layer) must already
    be in the pinned :data:`ALLOWED_UPWARD_EDGES` baseline. A NEW upward
    edge -- the exact athenaeum#1133-review failure mode, an L3 module
    reaching into an L4 module (or any lower layer reaching into a higher
    one) -- fails this test, whether the import is top-level or
    function-local/deferred (the shared :func:`build_import_graph` counts
    both, matching ``test_import_graph_acyclic.py``)."""
    violations = _layer_violations()
    new_violations = [
        v for v in violations if (v[0], v[2]) not in ALLOWED_UPWARD_EDGES
    ]
    assert not new_violations, (
        "New import(s) invert the documented module layering — an importer "
        "reaches into a module declared at a STRICTLY HIGHER layer than its "
        "own:\n  "
        + "\n  ".join(
            f"{a} (L{la}) -> {b} (L{lb})" for a, la, b, lb in new_violations
        )
        + "\nIf this is deliberate, it needs the same kind of justification "
        "as the existing entries in ALLOWED_UPWARD_EDGES "
        "(tests/test_layer_boundary.py) — most of which are function-local/"
        "deferred imports taken specifically to avoid a cycle. If it is not "
        "deliberate, the offending import is the bug."
    )


def test_rules_does_not_import_intake_audit() -> None:
    """Regression guard for the LITERAL edge the athenaeum#1133 review injected
    (``rules.py`` -> ``intake_audit.py``, L3 -> L4) to prove
    ``test_import_graph_acyclic.py`` alone would not catch it. Kept as its
    own tiny, named test — independent of the general sweep above — so this
    specific historical regression has a test that names it directly,
    mirroring how ``tests/test_store_layering.py`` pins its own specific
    edge alongside a general Tarjan check."""
    graph = build_import_graph()
    assert "athenaeum.intake_audit" not in {
        f"athenaeum.{t}" for t in graph.get("rules", set())
    }


def test_layer_declarations_cover_every_src_module() -> None:
    """Keeps ``tests/fixtures/layer_declarations.py`` honest as the source
    tree grows: every ``src/athenaeum/*.py`` module must be either declared
    (has a layer) or explicitly listed as undeclared. A module silently
    missing from BOTH would make the boundary check blind to any edges it
    participates in without that gap being visible anywhere."""
    modules = {
        "__init__" if f.name == "__init__.py" else f.stem
        for f in SRC.glob("*.py")
    }
    declared = set(MODULE_LAYER)
    accounted_for = declared | UNDECLARED
    missing = modules - accounted_for
    assert not missing, (
        f"Module(s) {sorted(missing)} are in neither MODULE_LAYER nor "
        "UNDECLARED in tests/fixtures/layer_declarations.py — add a layer "
        "entry if the module documents one, otherwise add it to UNDECLARED."
    )
    stale = accounted_for - modules
    assert not stale, (
        f"tests/fixtures/layer_declarations.py references module(s) "
        f"{sorted(stale)} that no longer exist under src/athenaeum/"
    )


def test_declared_layers_are_sane() -> None:
    """Every declared layer is a small non-negative int (L0..L6, where L6 is
    only the package ``__init__``) — catches a transcription typo turning
    e.g. "L3" into 3-with-a-stray-character or similar."""
    for module, layer in MODULE_LAYER.items():
        assert isinstance(layer, int) and 0 <= layer <= 6, (
            f"{module}: declared layer {layer!r} is out of the expected "
            "L0..L6 range"
        )
