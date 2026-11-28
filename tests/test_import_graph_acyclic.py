# SPDX-License-Identifier: Apache-2.0
"""Import-graph SCC guard (issue athenaeum#545).

This test walks EVERY module under ``src/athenaeum/`` with :mod:`ast`, at any
depth (a module inside a subpackage is a node of its own, named dotted:
``storage.routing``), collecting top-level, function-local (deferred) and
``TYPE_CHECKING``-only ``import athenaeum.X`` / ``from athenaeum.X import ...``
edges plus ``importlib.import_module("athenaeum.X")`` calls with a constant
name, then runs Tarjan's algorithm over the full graph. It exists to lock in
the athenaeum#545 refactor, which hoisted three shared raw-intake primitives
(``discover_raw_files``, ``discover_auto_memory_files``,
``tier0_passthrough``) DOWN to the :mod:`athenaeum.intake` leaf so the eight
formerly mutually-recursive modules (librarian, merge, tiers, pending_merges,
batch, status, retire, wiki_dedupe) no longer form one giant strongly-connected
component (SCC), and broke the ``cli`` <-> ``_cmd_drain`` 2-node cycle.

CONTEXT ON "8 vs 14": the athenaeum#545 audit prose described "a single 8-node SCC". The
REAL pre-athenaeum#545 full-graph SCC (top-level + function-local) was larger — 14 nodes
— because those eight hub modules were additionally tangled, through their OWN
top-level and deferred edges, with ``answers``, ``calibration``,
``contradictions``, ``drain``, ``reasoning_tiers``, and ``resolutions``. athenaeum#545's
NAMED, in-scope goal was to dissolve the librarian-centered named-8 coupling
(hoist the 3 primitives + break cli/_cmd_drain), and that is done: ``batch``,
``status``, ``retire``, ``wiki_dedupe``, ``cli``, and ``_cmd_drain`` are now
free of their former cycles.

The three residual SCCs that athenaeum#545 left in place ({answers, contradictions,
resolutions, tiers}, {calibration, merge, pending_merges, reasoning_tiers}, and
{drain, librarian, status}) were dissolved in issue athenaeum#640, so the full-graph SCC
was then EMPTY. Counting constant ``import_module`` calls as edges (athenaeum#3230)
made two real cycles visible that the earlier scan could not see: each is
closed by an ``import_module`` call taken to keep a module-scope cycle out of
the graph. :data:`ALLOWED_SCCS` pins those two. The baseline only shrinks: any
SCC outside it fails, and a baseline entry that no longer matches a real SCC
exactly fails too, so a cycle that gets smaller or goes away is written down.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "athenaeum"

# The allowed baseline of residual import SCCs. Issue athenaeum#640 dissolved the last
# three residuals that athenaeum#545 left in place. The three hoists that got
# there (each "move the shared primitive DOWN to a leaf, then drop
# the back-edge", the pattern athenaeum#545 established with intake.py/vecmath.py):
#   * {tiers, contradictions, resolutions, answers} — DEFAULT_CLASSIFY_MODEL
#     moved tiers -> config, dropping the contradictions -> tiers back-edge.
#   * {merge, pending_merges, calibration, reasoning_tiers} —
#     _merge_proposal_suppression_reason moved merge -> merge_type_gate,
#     dropping the pending_merges -> merge back-edge.
#   * {librarian, drain, status} — build_advisory (the ETA advisor) moved
#     drain -> drain_advisor, dropping the librarian/status -> drain back-edges.
#
# The two entries below are the cycles that became visible when athenaeum#3230
# started counting constant ``import_module`` calls as edges. Both are closed by
# such a call: ``storage`` and ``raw_screening`` resolve ``pii`` and
# ``sensitivity_routing`` by name at call time, and ``librarian`` resolves
# ``raise_retract`` the same way. Shrink-only: delete or narrow an entry when its
# cycle shrinks; never add one.
ALLOWED_SCCS: list[frozenset[str]] = [
    frozenset({"pii", "raw_screening", "sensitivity", "sensitivity_routing", "storage"}),
    frozenset({"librarian", "raise_retract", "retire_pages"}),
]


#: The kinds of import edge the scanner records. ``top`` is a module-scope
#: import; ``local`` one inside a function (a deferred import); ``type-checking``
#: one under ``if TYPE_CHECKING:``; ``import_module`` an
#: ``importlib.import_module("athenaeum.X")`` call with a constant name.
EDGE_KINDS: tuple[str, ...] = ("top", "local", "type-checking", "import_module")


@dataclass(frozen=True)
class ImportEdge:
    """One import of an athenaeum module by another, where it happens."""

    importer: str
    target: str
    path: str
    line: int
    kind: str


@dataclass(frozen=True)
class ComputedImport:
    """An ``import_module`` call whose name is not a constant."""

    importer: str
    function: str | None
    path: str
    line: int


@dataclass
class ImportScan:
    """Everything the scanner found in one source tree."""

    modules: dict[str, Path] = field(default_factory=dict)
    edges: list[ImportEdge] = field(default_factory=list)
    libraries: list[tuple[str, str, str, int, str]] = field(default_factory=list)
    computed: list[ComputedImport] = field(default_factory=list)


#: Third-party or standard-library imports the tier test assigns to a tier
#: group. Matched on the dotted name or its prefix.
TRACKED_LIBRARIES: tuple[str, ...] = ("anthropic", "fastmcp", "mcp", "urllib.request")


def module_name(path: Path, src: Path = SRC) -> str:
    """The dotted module name of ``path`` inside ``src``.

    A package's ``__init__.py`` is named for the package
    (``storage/__init__.py`` is ``storage``). The root package's
    ``__init__.py`` keeps the historical name ``__init__``.
    """
    parts = list(path.relative_to(src).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or "__init__"


def discover_modules(src: Path = SRC) -> dict[str, Path]:
    """Every ``.py`` file under ``src``, at any depth, keyed by module name."""
    return {
        module_name(f, src): f
        for f in sorted(src.rglob("*.py"))
        if "__pycache__" not in f.parts
    }


def _resolve(dotted: str, modules: Iterable[str]) -> str | None:
    """The deepest known module named by ``athenaeum.<dotted>`` or a prefix."""
    known = set(modules)
    parts = dotted.split(".")
    for end in range(len(parts), 0, -1):
        candidate = ".".join(parts[:end])
        if candidate in known:
            return candidate
    return None


def _tracked_library(name: str) -> str | None:
    for lib in TRACKED_LIBRARIES:
        if name == lib or name.startswith(lib + "."):
            return lib
    return None


def _is_type_checking_test(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "TYPE_CHECKING"
    return isinstance(node, ast.Attribute) and node.attr == "TYPE_CHECKING"


def _is_import_module_call(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr == "import_module"
    return isinstance(func, ast.Name) and func.id == "import_module"


class _Scanner(ast.NodeVisitor):
    def __init__(self, scan: ImportScan, importer: str, path: str) -> None:
        self.scan = scan
        self.importer = importer
        self.path = path
        self.functions: list[str] = []
        self.type_checking = 0
        self.seen: set[ImportEdge] = set()

    def _kind(self) -> str:
        if self.type_checking:
            return "type-checking"
        return "local" if self.functions else "top"

    def _edge(self, dotted: str, line: int, kind: str) -> None:
        target = _resolve(dotted, self.scan.modules)
        if target is None or target == self.importer:
            return
        edge = ImportEdge(self.importer, target, self.path, line, kind)
        # ``from athenaeum.x import a, b`` is one import of ``x``, not two.
        if edge not in self.seen:
            self.seen.add(edge)
            self.scan.edges.append(edge)

    def _library(self, name: str, line: int) -> None:
        lib = _tracked_library(name)
        if lib is not None:
            self.scan.libraries.append((self.importer, lib, self.path, line, self._kind()))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    def visit_If(self, node: ast.If) -> None:
        if _is_type_checking_test(node.test):
            self.type_checking += 1
            for child in node.body:
                self.visit(child)
            self.type_checking -= 1
            for child in node.orelse:
                self.visit(child)
            return
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name.startswith("athenaeum."):
                self._edge(alias.name[len("athenaeum.") :], node.lineno, self._kind())
            else:
                self._library(alias.name, node.lineno)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # Only absolute imports (level 0). The package uses no intra-package
        # relative imports.
        if node.level != 0 or not node.module:
            return
        kind = self._kind()
        if node.module == "athenaeum":
            # ``from athenaeum import X`` -- X may be a submodule.
            for alias in node.names:
                if alias.name in self.scan.modules:
                    self._edge(alias.name, node.lineno, kind)
            return
        if node.module.startswith("athenaeum."):
            base = node.module[len("athenaeum.") :]
            for alias in node.names:
                # ``from athenaeum.pkg import sub`` names the submodule when
                # there is one, otherwise the package itself.
                self._edge(f"{base}.{alias.name}", node.lineno, kind)
            return
        if node.module == "urllib" and any(a.name == "request" for a in node.names):
            self._library("urllib.request", node.lineno)
            return
        self._library(node.module, node.lineno)

    def visit_Call(self, node: ast.Call) -> None:
        if _is_import_module_call(node) and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                if arg.value.startswith("athenaeum."):
                    self._edge(arg.value[len("athenaeum.") :], node.lineno, "import_module")
            else:
                function = self.functions[-1] if self.functions else None
                self.scan.computed.append(
                    ComputedImport(self.importer, function, self.path, node.lineno)
                )
        self.generic_visit(node)


def scan_imports(src: Path = SRC) -> ImportScan:
    """Scan every module under ``src`` for its athenaeum and library imports.

    Counts module-scope, function-local and ``TYPE_CHECKING``-only imports and
    ``import_module`` calls with a constant ``"athenaeum.X"`` name. An
    ``import_module`` call with a computed name is recorded separately in
    :attr:`ImportScan.computed`; it names no edge.
    """
    scan = ImportScan(modules=discover_modules(src))
    root = src.parent.parent
    for name, path in scan.modules.items():
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = str(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        _Scanner(scan, name, rel).visit(tree)
    return scan


def build_import_graph(src: Path = SRC) -> dict[str, set[str]]:
    """Full intra-package import graph over every module under ``src``.

    Nodes are dotted module names, so a module inside a subpackage
    (``storage.routing``) is a node of its own. Edges are every kind the
    scanner records: module-scope, function-local, ``TYPE_CHECKING``-only and
    constant ``import_module``.
    """
    scan = scan_imports(src)
    graph: dict[str, set[str]] = {name: set() for name in scan.modules}
    for edge in scan.edges:
        graph[edge.importer].add(edge.target)
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

    :data:`ALLOWED_SCCS` holds the two cycles that are closed by a constant
    ``import_module`` call (athenaeum#3230). Any other multi-node SCC, or one of
    those two growing, is a violation. Pre-athenaeum#545 the full graph had a
    14-node SCC spanning the named-8 hub modules plus answers/calibration/
    contradictions/drain/reasoning_tiers/resolutions; athenaeum#545 shrank it to
    three residuals and athenaeum#640 removed those.
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
        "pinned baseline:\n  " + "\n  ".join(violations)
        + f"\nAllowed baseline: {allowed_repr}"
    )


def test_allowed_scc_baseline_has_no_stale_entry() -> None:
    """The baseline only shrinks: each entry must still be a real SCC, exactly.

    When a pinned cycle loses a member or disappears, this fails and names the
    entry, so the baseline is narrowed in the same change that shrank it.
    """
    sccs = set(_multi_node_sccs(build_import_graph()))
    stale = [sorted(allowed) for allowed in ALLOWED_SCCS if allowed not in sccs]
    assert not stale, (
        "ALLOWED_SCCS entries that no longer match a real SCC exactly (narrow "
        f"or delete them): {stale}; real SCCs: {sorted(sorted(c) for c in sccs)}"
    )


def test_a_module_inside_a_subpackage_is_a_graph_node(tmp_path: Path) -> None:
    """A module in a subpackage is named dotted and its edges are counted."""
    src = tmp_path / "src" / "athenaeum"
    (src / "storage" / "backends").mkdir(parents=True)
    (src / "__init__.py").write_text("")
    (src / "models.py").write_text("")
    (src / "storage" / "__init__.py").write_text("")
    (src / "storage" / "backends" / "__init__.py").write_text("")
    (src / "storage" / "routing.py").write_text(
        "from athenaeum.storage.backends import files\n"
    )
    (src / "storage" / "backends" / "files.py").write_text(
        "import importlib\n\n\ndef load():\n"
        "    return importlib.import_module('athenaeum.models')\n"
    )
    graph = build_import_graph(src)
    assert "storage.routing" in graph
    assert "storage.backends.files" in graph
    assert graph["storage.routing"] == {"storage.backends.files"}
    assert graph["storage.backends.files"] == {"models"}


def test_the_real_tree_includes_its_subpackages() -> None:
    """The live scan sees the packaged subpackages, not only top-level files."""
    graph = build_import_graph()
    assert "schema" in graph
    assert "rule_examples" in graph


def test_named_eight_scc_is_dissolved() -> None:
    """The athenaeum#545 payload: no SCC may couple the named-8 hub modules together.

    Guards the concrete value athenaeum#545 delivered — batch, status, retire,
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
            "modules — the athenaeum#545 dissolution regressed."
        )

    # These four must be entirely free of any cycle.
    freed = {"batch", "retire", "wiki_dedupe", "_cmd_drain", "cli"}
    for scc in _multi_node_sccs(graph):
        assert not (scc & freed), (
            f"SCC {sorted(scc)} contains a module athenaeum#545 freed of all cycles: "
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
