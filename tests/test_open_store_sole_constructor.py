# SPDX-License-Identifier: Apache-2.0
"""Guard: ``open_store`` is the only production door to ``FilesystemStore`` (athenaeum#2614).

A ``FilesystemStore`` built by hand with an empty surface map knows nothing
about the configured storage surfaces, so every capability check made against
it answers for the default layout only. Production code must build stores via
:func:`athenaeum.storage.open_store`; only ``store.py`` (the class itself),
``storage.py`` (the factory) and ``off_corpus.py`` may construct one directly.

The scanner is AST-based (a docstring or comment mentioning the name is not a
construction) and is itself proven to detect the pattern by running it over a
fixture module that contains one.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "athenaeum"
ALLOWED = frozenset({"store.py", "storage.py", "off_corpus.py"})


def filesystem_store_constructions(source: str) -> list[int]:
    """Line numbers of every ``FilesystemStore(...)`` call in *source*."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name == "FilesystemStore":
            lines.append(node.lineno)
    return lines


def test_scanner_detects_a_construction_in_a_fixture_module(tmp_path: Path) -> None:
    fixture = tmp_path / "offender.py"
    fixture.write_text(
        '"""FilesystemStore(root, {}) in a docstring is not a call."""\n'
        "from athenaeum.store import FilesystemStore\n"
        "import athenaeum.store as store_mod\n"
        "\n"
        "def build(root):\n"
        "    return FilesystemStore(root, {})\n"
        "\n"
        "def build_qualified(root):\n"
        "    return store_mod.FilesystemStore(root, {})\n",
        encoding="utf-8",
    )
    assert filesystem_store_constructions(fixture.read_text(encoding="utf-8")) == [6, 9]


def test_scanner_ignores_references_that_are_not_constructions() -> None:
    assert filesystem_store_constructions(
        "from athenaeum.store import FilesystemStore\n"
        "def f(store: FilesystemStore) -> FilesystemStore:\n"
        "    return isinstance(store, FilesystemStore) and store\n"
    ) == []


def test_registry_is_the_only_other_door_and_it_lives_in_storage() -> None:
    """The registry (athenaeum#3165) adds no construction site: a registered
    factory is invoked only by ``storage.open_store``, and the sole
    ``FilesystemStore`` construction in ``storage.py`` is the built-in factory
    ``open_store`` selects."""
    source = (SRC / "storage.py").read_text(encoding="utf-8")
    assert len(filesystem_store_constructions(source)) == 1


def test_no_production_module_constructs_a_filesystem_store_directly() -> None:
    offenders = {
        str(path.relative_to(SRC)): hits
        for path in sorted(SRC.rglob("*.py"))
        if path.name not in ALLOWED
        and (hits := filesystem_store_constructions(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "construct stores through athenaeum.storage.open_store(config, "
        f"knowledge_root), not FilesystemStore(...): {offenders}"
    )
