"""Every ``read_blob`` call site in src/ must be classified (athenaeum#3127).

``read_blob`` is path-dependent: with ``path`` the blob is smudged through the
repo's filters, without it the raw stored bytes come back. A new caller must be
added below with its classification, so the choice is made deliberately.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "athenaeum"

# module file -> {call count: (has path argument, reason)}
ALLOWED: dict[str, tuple[int, bool]] = {
    # Raw: re-reads the ancestor blob it wrote itself with write_blob (never cleaned).
    "raise_supersede.py": (1, False),
    # Filtered: committed page blob, compared with and written over the checkout.
    "sync_integrate.py": (1, True),
}


def _calls() -> dict[str, list[bool]]:
    found: dict[str, list[bool]] = {}
    for py in sorted(SRC.rglob("*.py")):
        if py.name == "store.py":
            continue
        for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if name == "read_blob":
                has_path = len(node.args) >= 3 or any(k.arg == "path" for k in node.keywords)
                found.setdefault(str(py.relative_to(SRC)), []).append(has_path)
    return found


def test_read_blob_call_sites_are_classified() -> None:
    found = _calls()
    assert {k: (len(v), v[0]) for k, v in found.items()} == ALLOWED
    assert all(len(set(v)) == 1 for v in found.values())
