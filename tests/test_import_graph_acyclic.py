# SPDX-License-Identifier: Apache-2.0
"""Import-graph SCC guard (issue #545).

This test walks EVERY ``src/athenaeum/*.py`` module with :mod:`ast`, collecting
BOTH top-level AND function-local (deferred) ``import athenaeum.X`` /
``from athenaeum.X import ...`` edges, then runs Tarjan's algorithm over the
full graph. It exists to lock in the #545 refactor, which hoisted three shared
raw-intake primitives (``discover_raw_files``, ``discover_auto_memory_files``,
``tier0_passthrough``) DOWN to the :mod:`athenaeum.intake` leaf so the eight
formerly mutually-recursive modules (librarian, merge, tiers, pending_merges,
batch, status, retire, wiki_dedupe) no longer form one giant strongly-connected
component (SCC), and broke the ``cli`` <-> ``_cmd_drain`` 2-node cycle.

CONTEXT ON "8 vs 14": the #545 audit prose described "a single 8-node SCC". The
REAL pre-#545 full-graph SCC (top-level + function-local) was larger — 14 nodes
— because those eight hub modules were additionally tangled, through their OWN
top-level and deferred edges, with ``answers``, ``calibration``,
``contradictions``, ``drain``, ``reasoning_tiers``, and ``resolutions``. #545's
NAMED, in-scope goal was to dissolve the librarian-centered named-8 coupling
(hoist the 3 primitives + break cli/_cmd_drain), and that is done: ``batch``,
``status``, ``retire``, ``wiki_dedupe``, ``cli``, and ``_cmd_drain`` are now
free of their former cycles.

The THREE residual SCCs below are PRE-EXISTING, were explicitly OUT of #545's
named scope (the issue says "do NOT hunt for a sixth [pair]"), and are pinned
here as a DELIBERATELY-SMALLER allowed baseline — the acceptance criteria's
sanctioned escape hatch ("an explicitly-documented, deliberately-smaller
allowed set"). The guard FAILS if:

  * any SCC appears that is not a subset of an allowed baseline entry, OR
  * any allowed SCC GROWS (gains a member).

So the graph can only shrink from here, never regrow past this baseline. A
follow-up issue (filed by the orchestrator, not here) tracks dissolving the
residuals; when one is dissolved, tighten the corresponding entry below.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "athenaeum"

# The pinned, deliberately-smaller allowed baseline of residual SCCs (issue
# #545). Each frozenset is the MAXIMUM allowed membership for a residual cycle;
# the actual measured SCC must be a subset of exactly one of these (or a
# singleton, which Tarjan reports but we ignore as "not a cycle"). See the
# module docstring for why these three are out of #545's scope.
ALLOWED_SCCS: list[frozenset[str]] = [
    # tiers<->contradictions (top-level DEFAULT_CLASSIFY_MODEL import) plus
    # tiers' deferred reresolve imports of resolutions/answers.
    frozenset({"tiers", "contradictions", "resolutions", "answers"}),
    # merge<->pending_merges (the _merge_proposal_suppression_reason back-edge,
    # NOT one of #545's 3 hoisted primitives) plus reasoning_tiers/calibration
    # joining via their top-level tiers + pending_merges imports.
    frozenset({"merge", "pending_merges", "reasoning_tiers", "calibration"}),
    # librarian<->drain deferred cycle, with status joining via its own
    # deferred drain-advisor import.
    frozenset({"librarian", "drain", "status"}),
]


def _athenaeum_targets(node: ast.AST, self_mod: str) -> set[str]:
    """Return the ``athenaeum.<X>`` submodule names an import node references."""
    targets: set[str] = set()
    if isinstance(node, ast.Import):
        for alias in node.names:
            parts = alias.name.split(".")
            if parts[0] == "athenaeum" and len(parts) >= 2:
                targets.add(parts[1])
    elif isinstance(node, ast.ImportFrom):
        # Only absolute ``athenaeum...`` imports (level 0). The package uses no
        # intra-package relative imports, but guard anyway.
        if node.level == 0 and node.module:
            parts = node.module.split(".")
            if parts[0] == "athenaeum" and len(parts) >= 2:
                targets.add(parts[1])
            elif node.module == "athenaeum":
                # ``from athenaeum import X`` — X may be a submodule.
                for alias in node.names:
                    if (SRC / f"{alias.name}.py").exists():
                        targets.add(alias.name)
    targets.discard(self_mod)
    return targets


def build_import_graph() -> dict[str, set[str]]:
    """Full intra-package import graph: top-level AND function-local edges."""
    modules = {
        ("__init__" if f.name == "__init__.py" else f.stem)
        for f in SRC.glob("*.py")
    }
    graph: dict[str, set[str]] = {}
    for f in sorted(SRC.glob("*.py")):
        mod = "__init__" if f.name == "__init__.py" else f.stem
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        edges: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                edges |= _athenaeum_targets(node, mod)
        # Keep only edges to real package modules.
        graph[mod] = {e for e in edges if e in modules}
    return graph


def tarjan_scc(graph: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan's strongly-connected-components (iterative, recursion-safe)."""
    index_of: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: dict[str, bool] = {}
    stack: list[str] = []
    result: list[list[str]] = []
    counter = 0

    for root in graph:
        if root in index_of:
            continue
        # Iterative DFS: work items are (node, iterator over successors).
        work: list[tuple[str, list[str]]] = [(root, list(graph.get(root, ())))]
        index_of[root] = lowlink[root] = counter
        counter += 1
        stack.append(root)
        on_stack[root] = True
        while work:
            node, succs = work[-1]
            recursed = False
            while succs:
                succ = succs.pop()
                if succ not in graph:
                    continue
                if succ not in index_of:
                    index_of[succ] = lowlink[succ] = counter
                    counter += 1
                    stack.append(succ)
                    on_stack[succ] = True
                    work.append((succ, list(graph.get(succ, ()))))
                    recursed = True
                    break
                if on_stack.get(succ):
                    lowlink[node] = min(lowlink[node], index_of[succ])
            if recursed:
                continue
            # Done with node: settle lowlink against children, pop SCC if root.
            if lowlink[node] == index_of[node]:
                comp: list[str] = []
                while True:
                    w = stack.pop()
                    on_stack[w] = False
                    comp.append(w)
                    if w == node:
                        break
                result.append(comp)
            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
    return result


def _multi_node_sccs(graph: dict[str, set[str]]) -> list[frozenset[str]]:
    return [frozenset(c) for c in tarjan_scc(graph) if len(c) > 1]


def test_no_import_scc_outside_allowed_baseline() -> None:
    """Every cyclic SCC must fit within the pinned, documented baseline.

    Pre-#545 this FAILS: the full graph had a 14-node SCC spanning the named-8
    hub modules plus answers/calibration/contradictions/drain/reasoning_tiers/
    resolutions — no baseline entry contains it. Post-#545 it PASSES: the only
    remaining cyclic SCCs are the three residuals, each a subset of an
    ALLOWED_SCCS entry.
    """
    graph = build_import_graph()
    sccs = _multi_node_sccs(graph)

    violations: list[str] = []
    for scc in sccs:
        covering = [allowed for allowed in ALLOWED_SCCS if scc <= allowed]
        if not covering:
            violations.append(
                f"SCC {sorted(scc)} is not a subset of any allowed baseline entry"
            )

    allowed_repr = "; ".join(str(sorted(a)) for a in ALLOWED_SCCS)
    assert not violations, (
        "New/grown import SCC(s) detected — the import graph regressed past the "
        "#545 baseline:\n  " + "\n  ".join(violations)
        + f"\nAllowed baseline: {allowed_repr}"
    )


def test_named_eight_scc_is_dissolved() -> None:
    """The #545 payload: no SCC may couple the named-8 hub modules together.

    Guards the concrete value #545 delivered — batch, status, retire,
    wiki_dedupe (and cli/_cmd_drain) are freed, and no single SCC spans the
    former 8-module cluster. A member may still appear in a SMALL residual SCC
    (pinned in ALLOWED_SCCS), but never in one that re-tangles the whole named
    set.
    """
    named_eight = {
        "librarian",
        "merge",
        "tiers",
        "pending_merges",
        "batch",
        "status",
        "retire",
        "wiki_dedupe",
    }
    graph = build_import_graph()
    for scc in _multi_node_sccs(graph):
        overlap = scc & named_eight
        # No residual SCC may contain more than 2 of the named-8 modules; the
        # former single 8-node coupling is gone. (The largest allowed residual
        # touching the named-8 is {merge, pending_merges, ...} with 2.)
        assert len(overlap) <= 2, (
            f"SCC {sorted(scc)} re-couples {sorted(overlap)} of the named-8 hub "
            "modules — the #545 dissolution regressed."
        )

    # These four must be entirely free of any cycle.
    freed = {"batch", "retire", "wiki_dedupe", "_cmd_drain", "cli"}
    for scc in _multi_node_sccs(graph):
        assert not (scc & freed), (
            f"SCC {sorted(scc)} contains a module #545 freed of all cycles: "
            f"{sorted(scc & freed)}"
        )


def test_intake_leaf_imports_no_scc_member() -> None:
    """The hoist target (:mod:`athenaeum.intake`) must stay a low leaf.

    If ``intake`` ever imports one of the SCC members back, the whole hoist is
    undone and the cycle returns. Guard it explicitly.
    """
    graph = build_import_graph()
    forbidden = {
        "librarian",
        "merge",
        "tiers",
        "pending_merges",
        "batch",
        "status",
        "retire",
        "wiki_dedupe",
        "drain",
    }
    assert "intake" in graph, "athenaeum.intake module is missing"
    leaked = graph["intake"] & forbidden
    assert not leaked, (
        f"athenaeum.intake must not import SCC members, but imports: {sorted(leaked)}"
    )
