# SPDX-License-Identifier: Apache-2.0
"""The promptwitness/athenaeum import-boundary guard (athenaeum#2147).

promptwitness must be harness-agnostic and extractable later as a
standalone OSS library (athenaeum#2162), so it cannot depend on athenaeum.
Two independent checks, per the issue's own acceptance criteria:

1. An AST scan: no ``import athenaeum`` (or ``from athenaeum import ...``)
   anywhere under ``src/promptwitness/``, at any import depth (including a
   function-local/deferred import — the same discipline
   ``tests/test_import_graph_acyclic.py`` applies to athenaeum's own graph).
2. A subprocess that runs ``import promptwitness.cli`` and asserts no
   ``athenaeum*`` module ever entered ``sys.modules`` — proving the boundary
   holds at RUNTIME, not just syntactically (a `TYPE_CHECKING`-guarded
   import, or one reached only via `importlib`, would pass the AST scan but
   not this).
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPTWITNESS_SRC = REPO_ROOT / "src" / "promptwitness"


def _module_files() -> list[Path]:
    files = sorted(PROMPTWITNESS_SRC.rglob("*.py"))
    assert files, f"no .py files found under {PROMPTWITNESS_SRC} -- scan is broken"
    return files


class TestNoAthenaeumImportAnywhere:
    def test_ast_scan_finds_no_athenaeum_import(self) -> None:
        offenders: list[str] = []
        for path in _module_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "athenaeum" or alias.name.startswith("athenaeum."):
                            offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
                elif isinstance(node, ast.ImportFrom):
                    if node.module and (
                        node.module == "athenaeum" or node.module.startswith("athenaeum.")
                    ):
                        offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
        assert not offenders, f"athenaeum import(s) found under src/promptwitness/: {offenders}"

    def test_scan_is_not_vacuous(self) -> None:
        """A sanity floor: the AST walk above must actually visit real
        import statements, or a broken scan would pass by finding nothing."""
        total_imports = 0
        for path in _module_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    total_imports += 1
        assert total_imports >= 5, "expected several real import statements across the package"


class TestNoAthenaeumModuleAtRuntime:
    def test_importing_promptwitness_cli_loads_no_athenaeum_module(self) -> None:
        probe = (
            "import sys, json\n"
            "import promptwitness.cli\n"
            "hits = sorted(\n"
            "    m for m in sys.modules\n"
            "    if m == 'athenaeum' or m.startswith('athenaeum.')\n"
            ")\n"
            "print(json.dumps(hits))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, (
            f"subprocess failed (stdout={result.stdout!r}, stderr={result.stderr!r})"
        )
        import json as _json

        hits = _json.loads(result.stdout.strip().splitlines()[-1])
        assert hits == [], f"athenaeum module(s) loaded by importing promptwitness.cli: {hits}"
