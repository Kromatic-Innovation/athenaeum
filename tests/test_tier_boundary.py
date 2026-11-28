# SPDX-License-Identifier: Apache-2.0
"""Tier boundary guard (athenaeum#3230).

Every athenaeum module sits in one of nine tiers, written down in
``tests/fixtures/tier_map.txt``. Bottom to top:

====  ===================================================
tier  name
====  ===================================================
0     foundation
1     settings
2     policy
3     seams: three groups, ``storage``, ``access``, ``provider``
4     records
5     recall
6     routines
7     orchestration and workflow
8     surfaces
====  ===================================================

A module may import its own tier or a lower one. Inside the seams tier,
``access`` may import ``storage``; ``storage`` never imports ``access``;
``provider`` imports neither.

The import edges come from :func:`tests.test_import_graph_acyclic.scan_imports`,
the scanner the acyclicity guard also uses: module-scope, function-local and
``TYPE_CHECKING``-only imports, plus ``import_module("athenaeum.X")`` with a
constant name. An ``import_module`` call with a computed name is allowed only
in the dispatchers named in :data:`DISPATCHERS`.

Three libraries have a home: ``anthropic`` in the provider group, ``fastmcp``
and ``mcp`` in the surfaces tier, ``urllib.request`` in the seams tier.

What exists today that breaks these rules is listed in
``tests/fixtures/tier_exceptions.txt``. That file only shrinks: a new
violation fails, and so does a listed exception that no longer matches a
violation. Until athenaeum#3225, an edge into the storage code being
reorganised (:data:`TRANSITIONAL_TARGETS`) is reported but never fails,
whether it is new or stale, so storage work never edits the exceptions file.

This replaces ``tests/test_layer_boundary.py`` and its hand-maintained
``tests/fixtures/layer_declarations.py``.
"""

from __future__ import annotations

import ast
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from tests.test_import_graph_acyclic import SRC, ImportScan, build_import_graph, scan_imports

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TIER_MAP_PATH = FIXTURES / "tier_map.txt"
TIER_EXCEPTIONS_PATH = FIXTURES / "tier_exceptions.txt"

#: The group name a module of each tier carries in the map. Tier 3 carries
#: one of :data:`SEAM_GROUPS` instead.
TIER_GROUPS: dict[int, str] = {
    0: "foundation",
    1: "settings",
    2: "policy",
    4: "records",
    5: "recall",
    6: "routines",
    7: "orchestration",
    8: "surfaces",
}
SEAM_TIER = 3
SEAM_GROUPS: tuple[str, ...] = ("storage", "access", "provider")

#: Seam-to-seam imports that are allowed: (importer group, target group).
ALLOWED_SEAM_EDGES: frozenset[tuple[str, str]] = frozenset({("access", "storage")})

#: The ``storage`` submodules that sit at tier 0 rather than in the seam.
STORAGE_FOUNDATION: frozenset[str] = frozenset(
    {"storage.local", "storage.backend", "storage.backends.memory", "storage.backends.files"}
)

#: Edge targets that are reported but never fail, until athenaeum#3225.
#: ``storage`` covers every ``storage.*`` submodule too.
TRANSITIONAL_TARGETS: frozenset[str] = frozenset(
    {"store", "storage", "atomic_io", "runlock", "sync_lease"}
)

#: Where a computed ``import_module`` name is allowed: (module, function).
#: A function of ``None`` allows the whole module.
DISPATCHERS: frozenset[tuple[str, str | None]] = frozenset(
    {
        # The subcommand loader behind ``cli._SUBCOMMAND_LOADERS``.
        ("cli", "_build_lazy_parser"),
        # The prompt registry resolves each prompt's home module by name.
        ("prompt_registry", "_resolve"),
        # The package root's lazy ``__getattr__`` exports.
        ("__init__", "__getattr__"),
        # The storage package resolves its names lazily (storage/__init__.py).
        ("storage", None),
    }
)

SECTIONS: tuple[str, ...] = ("upward", "seam-to-seam", "libraries")


@dataclass(frozen=True)
class Placement:
    tier: int
    group: str


@dataclass
class TierMap:
    placements: dict[str, Placement] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


@dataclass
class Exceptions:
    entries: dict[str, set[tuple[str, str]]] = field(
        default_factory=lambda: {s: set() for s in SECTIONS}
    )
    errors: list[str] = field(default_factory=list)


@dataclass
class TierReport:
    """What the checker found. Only :attr:`failures` fail the test."""

    failures: list[str] = field(default_factory=list)
    reported: list[str] = field(default_factory=list)


def _content_lines(text: str) -> list[tuple[int, str]]:
    out = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if line:
            out.append((number, line))
    return out


def parse_tier_map(text: str) -> TierMap:
    """Parse ``tier_map.txt``: one ``module tier group`` line per module, sorted."""
    result = TierMap()
    order: list[str] = []
    for number, line in _content_lines(text):
        parts = line.split()
        if len(parts) != 3 or not parts[1].isdigit():
            result.errors.append(f"tier_map.txt:{number}: expected `module tier group`: {line!r}")
            continue
        module, tier, group = parts[0], int(parts[1]), parts[2]
        if tier == SEAM_TIER:
            if group not in SEAM_GROUPS:
                result.errors.append(
                    f"tier_map.txt:{number}: {module}: a tier 3 module's group must be one "
                    f"of {', '.join(SEAM_GROUPS)}, not {group!r}"
                )
        elif tier not in TIER_GROUPS:
            result.errors.append(f"tier_map.txt:{number}: {module}: no tier {tier}")
        elif group != TIER_GROUPS[tier]:
            result.errors.append(
                f"tier_map.txt:{number}: {module}: tier {tier} is {TIER_GROUPS[tier]!r}, "
                f"not {group!r}"
            )
        if module in result.placements:
            result.errors.append(f"tier_map.txt:{number}: {module} is listed twice")
        result.placements[module] = Placement(tier, group)
        order.append(module)
    if order != sorted(order):
        result.errors.append("tier_map.txt: lines are not sorted by module name")
    for module, placement in result.placements.items():
        if module.startswith("storage."):
            want = (
                Placement(0, "foundation")
                if module in STORAGE_FOUNDATION
                else Placement(SEAM_TIER, "storage")
            )
            if placement != want:
                result.errors.append(
                    f"tier_map.txt: {module} must be `{want.tier} {want.group}`"
                )
    return result


def parse_exceptions(text: str) -> Exceptions:
    """Parse ``tier_exceptions.txt``: ``importer -> target`` lines under sections."""
    result = Exceptions()
    section: str | None = None
    for number, line in _content_lines(text):
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            if section not in SECTIONS:
                result.errors.append(f"tier_exceptions.txt:{number}: unknown section {line}")
                section = None
            continue
        parts = [p.strip() for p in line.split("->")]
        if section is None or len(parts) != 2 or not all(parts):
            result.errors.append(
                f"tier_exceptions.txt:{number}: expected `importer -> target` "
                f"under one of {', '.join(f'[{s}]' for s in SECTIONS)}: {line!r}"
            )
            continue
        pair = (parts[0], parts[1])
        if pair in result.entries[section]:
            result.errors.append(f"tier_exceptions.txt:{number}: listed twice: {line}")
        result.entries[section].add(pair)
    return result


def is_transitional(target: str) -> bool:
    return target in TRANSITIONAL_TARGETS or target.startswith("storage.")


def _library_allowed(library: str, placement: Placement) -> bool:
    if library == "anthropic":
        return placement.tier == SEAM_TIER and placement.group == "provider"
    if library in ("fastmcp", "mcp"):
        return placement.tier == 8
    if library == "urllib.request":
        return placement.tier == SEAM_TIER
    raise AssertionError(f"no rule for library {library!r}")


def _describe(tier_map: TierMap, importer: str, target: str) -> str:
    a, b = tier_map.placements[importer], tier_map.placements[target]
    return f"{importer} (tier {a.tier} {a.group}) -> {target} (tier {b.tier} {b.group})"


def evaluate(scan: ImportScan, tier_map: TierMap, exceptions: Exceptions) -> TierReport:
    """Check ``scan`` against the map and the exceptions."""
    report = TierReport()
    report.failures += tier_map.errors + exceptions.errors

    placements = tier_map.placements
    for module in sorted(set(scan.modules) - set(placements)):
        report.failures.append(f"{module} is missing from tier_map.txt")
    for module in sorted(set(placements) - set(scan.modules)):
        report.failures.append(f"tier_map.txt names {module}, which is not a module")

    found: dict[str, dict[tuple[str, str], list[str]]] = {s: {} for s in SECTIONS}
    for edge in scan.edges:
        if edge.importer not in placements or edge.target not in placements:
            continue
        a, b = placements[edge.importer], placements[edge.target]
        if a.tier < b.tier:
            section = "upward"
        elif (
            a.tier == b.tier == SEAM_TIER
            and a.group != b.group
            and (a.group, b.group) not in ALLOWED_SEAM_EDGES
        ):
            section = "seam-to-seam"
        else:
            continue
        where = (
            f"{_describe(tier_map, edge.importer, edge.target)} "
            f"at {edge.path}:{edge.line} [{edge.kind}]"
        )
        found[section].setdefault((edge.importer, edge.target), []).append(where)

    for importer, library, path, line, kind in scan.libraries:
        if importer not in placements:
            continue
        placement = placements[importer]
        if not _library_allowed(library, placement):
            where = (
                f"{importer} (tier {placement.tier} {placement.group}) -> {library} "
                f"at {path}:{line} [{kind}]"
            )
            found["libraries"].setdefault((importer, library), []).append(where)

    for section in SECTIONS:
        listed = exceptions.entries[section]
        for pair, wheres in sorted(found[section].items()):
            transitional = section != "libraries" and is_transitional(pair[1])
            if transitional:
                report.reported += [f"{w} (transitional, athenaeum#3225)" for w in wheres]
            elif pair not in listed:
                report.failures += [f"new {section} import: {w}" for w in wheres]
        for pair in sorted(listed - set(found[section])):
            if section != "libraries" and is_transitional(pair[1]):
                report.reported.append(
                    f"stale transitional exception [{section}]: {pair[0]} -> {pair[1]}"
                )
            else:
                report.failures.append(
                    f"stale exception [{section}]: {pair[0]} -> {pair[1]} no longer "
                    "matches a violation; delete the line"
                )

    for computed in scan.computed:
        allowed = (computed.importer, computed.function) in DISPATCHERS or (
            (computed.importer, None) in DISPATCHERS
            and Path(computed.path).name == "__init__.py"
        )
        if not allowed:
            report.failures.append(
                f"computed import_module name in {computed.importer} "
                f"({computed.function or 'module scope'}) at {computed.path}:{computed.line}; "
                "only the dispatchers in DISPATCHERS may compute the name"
            )
    return report


def _live_report() -> TierReport:
    return evaluate(
        scan_imports(),
        parse_tier_map(TIER_MAP_PATH.read_text(encoding="utf-8")),
        parse_exceptions(TIER_EXCEPTIONS_PATH.read_text(encoding="utf-8")),
    )


_HOW_TO_FIX = (
    "\nFix the import, or move the module in tests/fixtures/tier_map.txt if its "
    "tier is wrong. A deliberate exception goes in tests/fixtures/tier_exceptions.txt, "
    "and the pull request needs the `layering-exception` label."
)


def test_tier_boundary_holds() -> None:
    """No import breaks the tier rules beyond the listed exceptions, and no
    listed exception is stale."""
    report = _live_report()
    if report.reported:
        warnings.warn(
            "tier boundary, reported only until athenaeum#3225 ("
            f"{len(report.reported)}):\n  " + "\n  ".join(report.reported),
            stacklevel=1,
        )
    assert not report.failures, (
        "Tier boundary violations:\n  " + "\n  ".join(report.failures) + _HOW_TO_FIX
    )


# --- The checker itself, on synthetic input -------------------------------


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    src = tmp_path / "src" / "athenaeum"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("")
    for rel, body in files.items():
        path = src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return src


_SYNTHETIC_MAP = """\
__init__ 8 surfaces
models 0 foundation
recall_mod 5 recall
routine 6 routines
"""


def test_a_synthetic_upward_import_is_reported(tmp_path: Path) -> None:
    src = _tree(
        tmp_path,
        {
            "models.py": "",
            "recall_mod.py": "def go():\n    from athenaeum import routine\n",
            "routine.py": "from athenaeum.models import X\n",
        },
    )
    report = evaluate(scan_imports(src), parse_tier_map(_SYNTHETIC_MAP), Exceptions())
    assert report.failures == [
        "new upward import: recall_mod (tier 5 recall) -> routine (tier 6 routines) "
        "at src/athenaeum/recall_mod.py:2 [local]"
    ]


def test_a_listed_upward_import_passes(tmp_path: Path) -> None:
    src = _tree(
        tmp_path,
        {"models.py": "", "recall_mod.py": "import athenaeum.routine\n", "routine.py": ""},
    )
    exceptions = parse_exceptions("[upward]\nrecall_mod -> routine\n")
    report = evaluate(scan_imports(src), parse_tier_map(_SYNTHETIC_MAP), exceptions)
    assert report.failures == []


def test_a_synthetic_stale_exception_is_reported(tmp_path: Path) -> None:
    src = _tree(tmp_path, {"models.py": "", "recall_mod.py": "", "routine.py": ""})
    exceptions = parse_exceptions("[upward]\nrecall_mod -> routine\n")
    report = evaluate(scan_imports(src), parse_tier_map(_SYNTHETIC_MAP), exceptions)
    assert report.failures == [
        "stale exception [upward]: recall_mod -> routine no longer matches a "
        "violation; delete the line"
    ]


def test_transitional_targets_are_reported_but_never_fail(tmp_path: Path) -> None:
    src = _tree(
        tmp_path,
        {
            "models.py": "from athenaeum.store import now_iso\n",
            "store.py": "",
            "storage/__init__.py": "",
            "storage/routing.py": "",
            "policy_mod.py": "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n    from athenaeum.storage.routing import R\n",
        },
    )
    tier_map = parse_tier_map(
        "__init__ 8 surfaces\nmodels 0 foundation\npolicy_mod 2 policy\n"
        "storage 3 storage\nstorage.routing 3 storage\nstore 3 storage\n"
    )
    exceptions = parse_exceptions("[upward]\nrunlock_user -> runlock\n")
    report = evaluate(scan_imports(src), tier_map, exceptions)
    assert report.failures == []
    assert report.reported == [
        "models (tier 0 foundation) -> store (tier 3 storage) at "
        "src/athenaeum/models.py:1 [top] (transitional, athenaeum#3225)",
        "policy_mod (tier 2 policy) -> storage.routing (tier 3 storage) at "
        "src/athenaeum/policy_mod.py:3 [type-checking] (transitional, athenaeum#3225)",
        "stale transitional exception [upward]: runlock_user -> runlock",
    ]


def test_seam_rule(tmp_path: Path) -> None:
    src = _tree(
        tmp_path,
        {
            "store.py": "import athenaeum.access\n",
            "access.py": "import athenaeum.store\n",
            "provider.py": "import importlib\nimportlib.import_module('athenaeum.access')\n",
        },
    )
    tier_map = parse_tier_map(
        "__init__ 8 surfaces\naccess 3 access\nprovider 3 provider\nstore 3 storage\n"
    )
    report = evaluate(scan_imports(src), tier_map, Exceptions())
    assert report.failures == [
        "new seam-to-seam import: provider (tier 3 provider) -> access (tier 3 access) "
        "at src/athenaeum/provider.py:2 [import_module]",
        "new seam-to-seam import: store (tier 3 storage) -> access (tier 3 access) "
        "at src/athenaeum/store.py:1 [top]",
    ]


def test_library_homes(tmp_path: Path) -> None:
    src = _tree(
        tmp_path,
        {
            "provider.py": "import anthropic\nfrom urllib import request\n",
            "routine.py": "from anthropic import types\n",
            "server.py": "import fastmcp\nfrom mcp.types import Tool\n",
            "fetch.py": "import urllib.request\n",
        },
    )
    tier_map = parse_tier_map(
        "__init__ 8 surfaces\nfetch 6 routines\nprovider 3 provider\n"
        "routine 6 routines\nserver 8 surfaces\n"
    )
    report = evaluate(scan_imports(src), tier_map, Exceptions())
    assert report.failures == [
        "new libraries import: fetch (tier 6 routines) -> urllib.request "
        "at src/athenaeum/fetch.py:1 [top]",
        "new libraries import: routine (tier 6 routines) -> anthropic "
        "at src/athenaeum/routine.py:1 [top]",
    ]


def test_map_must_name_every_module_and_only_modules(tmp_path: Path) -> None:
    src = _tree(tmp_path, {"models.py": "", "unmapped.py": ""})
    tier_map = parse_tier_map("__init__ 8 surfaces\ngone 6 routines\nmodels 0 foundation\n")
    report = evaluate(scan_imports(src), tier_map, Exceptions())
    assert report.failures == [
        "unmapped is missing from tier_map.txt",
        "tier_map.txt names gone, which is not a module",
    ]


def test_map_lines_are_checked() -> None:
    tier_map = parse_tier_map(
        "b 2 policy\na 3 records\nc 9 nowhere\nd 4 recall\n"
        "storage.local 3 storage\nstorage.routing 3 storage\nd 4 records\nbad line\n"
    )
    assert tier_map.errors == [
        "tier_map.txt:2: a: a tier 3 module's group must be one of storage, access, "
        "provider, not 'records'",
        "tier_map.txt:3: c: no tier 9",
        "tier_map.txt:4: d: tier 4 is 'records', not 'recall'",
        "tier_map.txt:7: d is listed twice",
        "tier_map.txt:8: expected `module tier group`: 'bad line'",
        "tier_map.txt: lines are not sorted by module name",
        "tier_map.txt: storage.local must be `0 foundation`",
    ]


def test_computed_import_module_only_in_dispatchers(tmp_path: Path) -> None:
    body = "import importlib\n\n\ndef load(name):\n    return importlib.import_module(name)\n"
    src = _tree(tmp_path, {"cli.py": body, "routine.py": body})
    tier_map = parse_tier_map("__init__ 8 surfaces\ncli 8 surfaces\nroutine 6 routines\n")
    report = evaluate(scan_imports(src), tier_map, Exceptions())
    assert report.failures == [
        "computed import_module name in cli (load) at src/athenaeum/cli.py:5; "
        "only the dispatchers in DISPATCHERS may compute the name",
        "computed import_module name in routine (load) at src/athenaeum/routine.py:5; "
        "only the dispatchers in DISPATCHERS may compute the name",
    ]


def test_the_live_map_is_well_formed() -> None:
    """The map and the exceptions file parse cleanly on their own."""
    assert parse_tier_map(TIER_MAP_PATH.read_text(encoding="utf-8")).errors == []
    assert parse_exceptions(TIER_EXCEPTIONS_PATH.read_text(encoding="utf-8")).errors == []


# --- Specific edges, kept from tests/test_layer_boundary.py ---------------


def test_rules_does_not_import_intake_audit() -> None:
    """The literal edge an athenaeum#1133 review injected (``rules`` ->
    ``intake_audit``) to prove the acyclicity guard alone would not catch it."""
    assert "intake_audit" not in build_import_graph()["rules"]


# page_merge (athenaeum#2773) is a pure function: no git helper, store, provider
# or file access. It may read the type spec (config), parse a page (models) and
# use the shared section helpers (page_text).
PAGE_MERGE_ALLOWED_ATHENAEUM = frozenset({"config", "models", "page_text"})
PAGE_MERGE_FORBIDDEN_STDLIB = frozenset(
    {"subprocess", "os", "pathlib", "shutil", "tempfile", "io", "socket", "urllib", "http"}
)


def _page_merge_import_violations(athenaeum: set[str], stdlib: set[str]) -> list[str]:
    bad = [f"athenaeum.{m}" for m in sorted(athenaeum - PAGE_MERGE_ALLOWED_ATHENAEUM)]
    bad += sorted(stdlib & PAGE_MERGE_FORBIDDEN_STDLIB)
    return bad


def _stdlib_top_level_imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.add(node.module.split(".")[0])
    return out


def test_page_merge_imports_no_git_store_provider_or_file_module() -> None:
    athenaeum_imports = build_import_graph()["page_merge"]
    stdlib_imports = _stdlib_top_level_imports(SRC / "page_merge.py")
    assert _page_merge_import_violations(athenaeum_imports, stdlib_imports) == [], (
        f"page_merge imports {sorted(athenaeum_imports)} / {sorted(stdlib_imports)}; "
        f"only {sorted(PAGE_MERGE_ALLOWED_ATHENAEUM)} may be imported and no file or process module"
    )


def test_page_merge_import_check_flags_a_git_store_or_file_import() -> None:
    """Positive control: the check above fails on the imports it exists to stop."""
    assert _page_merge_import_violations({"config", "store"}, set()) == ["athenaeum.store"]
    assert _page_merge_import_violations({"provider"}, {"subprocess"}) == [
        "athenaeum.provider",
        "subprocess",
    ]
    assert _page_merge_import_violations({"atomic_io"}, {"pathlib"}) == [
        "athenaeum.atomic_io",
        "pathlib",
    ]
