"""Every runtime dependency carries an explicit upper bound (athenaeum#3080).

``pyproject.toml`` says each third-party runtime dependency is capped at an
explicit upper bound so a surprise release cannot silently break a released
wheel. ``opentimestamps`` in the ``witness-anchor`` extra slipped through with
a bare floor; this pins the rule so the next new extra cannot.

Developer-only groups (test runners, build tools) are exempt: they never ship
inside an installed athenaeum.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.requirements import Requirement

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"

# Extras that only a contributor installs; nothing in them is imported by
# the package at runtime.
DEV_ONLY_EXTRAS = frozenset({"dev", "bench"})

_UPPER_BOUND_OPERATORS = frozenset({"<", "<=", "==", "~=", "==="})


def _runtime_requirements() -> list[tuple[str, Requirement]]:
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    out = [("dependencies", Requirement(r)) for r in project["dependencies"]]
    for extra, reqs in project.get("optional-dependencies", {}).items():
        if extra in DEV_ONLY_EXTRAS:
            continue
        out.extend((extra, Requirement(r)) for r in reqs)
    # Self-references (``athenaeum[mcp]``) are not third-party pins.
    return [(group, req) for group, req in out if req.name != "athenaeum"]


def _has_upper_bound(req: Requirement) -> bool:
    return any(spec.operator in _UPPER_BOUND_OPERATORS for spec in req.specifier)


def test_every_runtime_dependency_has_an_upper_bound() -> None:
    uncapped = [
        f"{group}: {req}" for group, req in _runtime_requirements() if not _has_upper_bound(req)
    ]
    assert not uncapped, (
        "runtime dependencies without an upper bound (cap each at the next "
        f"major boundary, as pyproject.toml's comment requires): {uncapped}"
    )


def test_witness_anchor_caps_opentimestamps() -> None:
    reqs = {req.name: req for group, req in _runtime_requirements() if group == "witness-anchor"}
    assert "opentimestamps" in reqs
    assert _has_upper_bound(reqs["opentimestamps"])


def test_the_check_is_not_vacuous() -> None:
    # The rule must actually be looking at something: the core dependencies
    # and at least one runtime extra.
    groups = {group for group, _ in _runtime_requirements()}
    assert "dependencies" in groups
    assert "witness-anchor" in groups
    assert not _has_upper_bound(Requirement("opentimestamps>=0.4"))
