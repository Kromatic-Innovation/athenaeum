"""``WikiEntity.opaque_filename`` is applied where the destination is resolved.

athenaeum#3126: a write path that resolves a destination but forgets to copy the
store's ``opaque_filenames`` choice onto the entity would silently leak names
into file names. Every src module must go through
``destination_for_new_entity``; only ``intake_destination`` may call the bare
resolver, and nothing outside it may assign the flag.
"""

from __future__ import annotations

import ast
from pathlib import Path

from athenaeum.intake_destination import destination_for_new_entity
from athenaeum.models import WikiEntity

SRC = Path(__file__).resolve().parent.parent / "src" / "athenaeum"
OWNER = "intake_destination.py"
CFG = {
    "storage": {
        "adapters": {
            "vault": {
                "backing_store": "markdown",
                "surface_root": "vault",
                "corpus_policy": {"embedded": True, "recallable": True},
                "opaque_filenames": True,
            }
        },
        "mapping": {"secret": "vault"},
    }
}


def _offenders(match) -> list[str]:  # type: ignore[no-untyped-def]
    out = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == OWNER:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if match(node):
                out.append(f"{path.name}:{node.lineno}")
    return out


def test_no_module_calls_the_bare_resolver() -> None:
    def is_call(n: ast.AST) -> bool:
        return isinstance(n, ast.Call) and (
            getattr(n.func, "id", None) == "destination_for_entity"
            or getattr(n.func, "attr", None) == "destination_for_entity"
        )

    assert _offenders(is_call) == []


def test_no_module_assigns_the_flag_by_hand() -> None:
    def is_assign(n: ast.AST) -> bool:
        targets = n.targets if isinstance(n, ast.Assign) else [getattr(n, "target", None)]
        return isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign)) and any(
            getattr(t, "attr", None) == "opaque_filename" for t in targets
        )

    assert _offenders(is_assign) == []


def test_helper_applies_the_flag(tmp_path: Path) -> None:
    (tmp_path / "wiki").mkdir()
    opaque = WikiEntity(uid="a1b2c3d4", name="Alice", type="secret", tags=[])
    plain = WikiEntity(uid="e5f6a7b8", name="Bob", type="person", tags=[])
    assert destination_for_new_entity(opaque, CFG, tmp_path).opaque_filenames is True
    assert opaque.opaque_filename is True
    assert destination_for_new_entity(plain, CFG, tmp_path).opaque_filenames is False
    assert plain.opaque_filename is False
