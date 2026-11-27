"""The routing tables are read only by the storage resolver (athenaeum#3158).

``resolve_excluded_fields_config`` and ``resolve_excluded_read_mapping``
are the resolver's inputs. Any module
other than ``storage.py`` that calls one has reintroduced a second reader of
"where is this field stored"; it must go through ``storage.route_for`` (or the
resolver's own ``configured_*`` accessors that ``pii`` uses for its field
policy) instead. ``config.py`` defines them and never calls them.
"""

from __future__ import annotations

import ast
from pathlib import Path

from athenaeum import storage

RESOLVER_FUNCS = frozenset(
    {
        "resolve_excluded_fields_config",
        "resolve_excluded_read_mapping",
    }
)


def _called_names(tree: ast.AST) -> set[str]:
    """Names called, attribute-accessed or imported -- so an aliased import is caught."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def test_config_resolvers_are_called_only_from_the_resolver() -> None:
    src = Path(storage.__file__).parent
    offenders = []
    for path in sorted(src.rglob("*.py")):
        if path.name == "storage.py":
            continue
        called = _called_names(ast.parse(path.read_text(encoding="utf-8")))
        hit = sorted(called & RESOLVER_FUNCS)
        if hit:
            offenders.append((path.name, hit))
    assert not offenders, offenders
