# SPDX-License-Identifier: Apache-2.0
"""Guard: only `_cmd_quiesce.py` may write or release the quiesce sentinel
(issue athenaeum#1965, AC3).

athenaeum#1965 added a holder-aware check to both
:func:`athenaeum.quiesce.write_quiesce` and
:func:`athenaeum.quiesce.release_quiesce` -- an active sentinel belonging to
a DIFFERENT holder is refused unless the caller passes ``force=True``. That
check is only a real guarantee, not merely something one caller happens to
do correctly, because :mod:`athenaeum._cmd_quiesce` is today the ONLY module
in ``src/`` that calls either function: it derives the caller's holder once
(``_default_holder()``, or an explicit ``--holder``) and is the single choke
point through which that holder reaches the library. A SECOND write/release
path added anywhere else -- even one that also passes a holder "correctly"
-- would create a second choke point, and the whole point of a choke point
is that there is exactly one: a future module that reached for
``write_quiesce``/``release_quiesce`` directly (instead of shelling out to
``athenaeum quiesce`` or extending the CLI) could reintroduce the exact
holder-blind clobber this issue closes, invisibly, because each individual
call might look correct in isolation -- the SAME failure shape
``test_layer_boundary.py`` describes for a layering inversion ("a single
one-directional edge ... is not a cycle, so it passes [the acyclicity guard]
green").

This test pins the invariant mechanically, following the source-scanning
idiom of ``tests/test_layer_boundary.py`` / ``tests/test_import_budget.py``:
walk every ``src/athenaeum/*.py`` module with :mod:`ast` and assert that none
of them -- except ``quiesce.py`` itself (which DEFINES the two functions)
and ``_cmd_quiesce.py`` (the one authorized caller) -- imports or
attribute-accesses ``write_quiesce`` or ``release_quiesce``.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "athenaeum"

#: The two sentinel-mutating entry points this guard protects.
_GUARDED_NAMES = frozenset({"write_quiesce", "release_quiesce"})

#: `quiesce.py` defines them; `_cmd_quiesce.py` is the sole authorized caller.
_ALLOWED_MODULES = frozenset({"quiesce", "_cmd_quiesce"})


def _guarded_references(path: Path) -> list[str]:
    """Which of `_GUARDED_NAMES` does `path` import or attribute-access?"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "athenaeum.quiesce":
            for alias in node.names:
                if alias.name in _GUARDED_NAMES:
                    found.add(alias.name)
        elif isinstance(node, ast.Attribute) and node.attr in _GUARDED_NAMES:
            found.add(node.attr)
    return sorted(found)


def test_only_cmd_quiesce_writes_or_releases_the_sentinel() -> None:
    violations: dict[str, list[str]] = {}
    for path in sorted(SRC.glob("*.py")):
        if path.stem in _ALLOWED_MODULES:
            continue
        hits = _guarded_references(path)
        if hits:
            violations[path.name] = hits
    assert not violations, (
        "only `_cmd_quiesce.py` may call write_quiesce/release_quiesce "
        "(issue athenaeum#1965's holder-aware check is only a real "
        "guarantee with a single choke point) -- found a second caller: "
        f"{violations}"
    )
