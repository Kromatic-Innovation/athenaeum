"""CI-versus-deploy dependency parity (athenaeum#1960).

CI installs athenaeum's declared dependency ranges under the full transitive
constraints file `requirements-ci.lock`; the deploy refresh (scripts/
deploy-guard.sh / scripts/deploy-sync.sh) now constrains itself to that same
lock (see _dg_constraints_flag / _ds_constraints_flag and
tests/test_deploy_guard.py). That closes the class of drift where the deploy
picks a DIFFERENT version than CI tested, inside the range the range admits
either way (the athenaeum#1953 incident: mcp 2.2.0 via fastmcp 4.0.8 on the
deploy host vs mcp 1.x in CI, both legal under the then-open range).

This module is the other half: an offline parity test asserting the lock and
the declared ranges cannot silently disagree for any DIRECT dependency the
deploy closure actually needs -- `[project].dependencies` plus the `mcp` and
`vector` extras (not `dev`, which is CI/tooling-only, and not `types`). For
each one it asserts a `requirements-ci.lock` pin exists and that the pinned
version satisfies the declared specifier, honouring environment markers.

Fully offline: no network, no host access, no model spend. Runs in the
existing `pytest` CI job -- no new workflow, no pytestmark deselecting it.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
LOCKFILE = REPO_ROOT / "requirements-ci.lock"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "ci_lock_parity"

# The extras whose direct deps the deploy refresh actually installs --
# `mcp,vector`, matching scripts/deploy-guard.sh's _dg_extras default and
# deploy-sync.sh's ATHENAEUM_DEPLOY_EXTRAS default. NOT `dev` (pulls in
# pytest/ruff/build -- CI/tooling only, never installed on the deploy
# checkout) and NOT `types` (mypy only).
DIRECT_EXTRAS = ("mcp", "vector")

# pip freeze (`--exclude-editable`) output: `name==version`, optionally with
# a trailing comment or environment marker pip itself never emits but a hand
# edit might; tolerate and ignore anything after the version.
_LOCK_LINE_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([^\s#;]+)")


def parse_lock(lock_text: str) -> dict[str, str]:
    """`requirements-ci.lock` text -> {canonical name: pinned version}."""
    pins: dict[str, str] = {}
    for raw_line in lock_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _LOCK_LINE_RE.match(line)
        if not match:
            continue
        name, version = match.group(1), match.group(2)
        pins[canonicalize_name(name)] = version
    return pins


def direct_requirements(pyproject_text: str) -> list[Requirement]:
    """`[project].dependencies` + the `mcp`/`vector` extras, as parsed Requirements."""
    data = tomllib.loads(pyproject_text)
    project = data.get("project", {})
    reqs = [Requirement(s) for s in project.get("dependencies", [])]
    optional = project.get("optional-dependencies", {})
    for extra in DIRECT_EXTRAS:
        for s in optional.get(extra, []):
            reqs.append(Requirement(s))
    return reqs


def applicable_requirements(reqs: list[Requirement]) -> list[Requirement]:
    """Drop requirements whose environment marker does not evaluate true HERE.

    Mirrors the rule requirements-ci.lock's own header already relies on:
    "a constraint for a package not needed on a given interpreter is simply
    ignored." A marker-gated dependency inapplicable to this interpreter /
    platform is not something the lock is obligated to pin on this run.
    """
    return [r for r in reqs if r.marker is None or r.marker.evaluate()]


def check_parity(pyproject_text: str, lock_text: str) -> list[str]:
    """Direct-dependency parity between a pyproject.toml and a lock file.

    Returns a list of human-readable violation messages; empty means parity
    holds -- every applicable direct dependency has a lock pin, and every
    pin satisfies its declared specifier.
    """
    violations: list[str] = []
    pins = parse_lock(lock_text)
    for req in applicable_requirements(direct_requirements(pyproject_text)):
        canon = canonicalize_name(req.name)
        pinned = pins.get(canon)
        if pinned is None:
            violations.append(
                f"{req.name}: declared ({req}) but requirements-ci.lock has no pin"
            )
            continue
        if not req.specifier.contains(Version(pinned), prereleases=True):
            violations.append(
                f"{req.name}: lock pins {pinned}, which does not satisfy "
                f"declared range {req.specifier}"
            )
    return violations


# ---------------------------------------------------------------------------
# The real check: athenaeum's own pyproject.toml against its own lock.
# ---------------------------------------------------------------------------


def test_ci_lock_satisfies_pyproject_ranges_for_direct_deploy_deps() -> None:
    violations = check_parity(PYPROJECT.read_text(), LOCKFILE.read_text())
    assert violations == [], "\n".join(violations)


# ---------------------------------------------------------------------------
# Counter-example fixtures (athenaeum#1960 AC3) -- anti-vacuity controls. A
# parity test that cannot fail proves nothing, so each fixture below must
# fail for its OWN, distinct reason, asserted on the message, not merely
# "raised" / "non-empty".
# ---------------------------------------------------------------------------


def _read_fixture_pair(name: str) -> tuple[str, str]:
    base = FIXTURES / name
    pyproject_text = (base / "pyproject.toml").read_text()
    lock_text = (base / "requirements-ci.lock").read_text()
    return pyproject_text, lock_text


def test_fixture_pin_outside_range_fails_for_the_out_of_range_pin() -> None:
    # (a): mcp==2.2.0 pinned against a declared mcp>=1.24,<2.0 -- the exact
    # shape of the athenaeum#1953 incident this issue closes.
    pyproject_text, lock_text = _read_fixture_pair("pin_outside_range")
    violations = check_parity(pyproject_text, lock_text)
    assert len(violations) == 1, violations
    assert violations[0].startswith("mcp:")
    assert "does not satisfy declared range" in violations[0]
    assert "2.2.0" in violations[0]


def test_fixture_missing_pin_fails_for_the_missing_pin() -> None:
    # (b): a declared direct dependency (mcp) with no pin at all.
    pyproject_text, lock_text = _read_fixture_pair("missing_pin")
    violations = check_parity(pyproject_text, lock_text)
    assert len(violations) == 1, violations
    assert violations[0].startswith("mcp:")
    assert "no pin" in violations[0]


def test_fixture_all_in_range_passes() -> None:
    # Positive control: proves the two failures above are about the
    # deliberate defect in each fixture, not about the harness always
    # failing.
    pyproject_text, lock_text = _read_fixture_pair("all_in_range")
    assert check_parity(pyproject_text, lock_text) == []


# ---------------------------------------------------------------------------
# Environment-marker handling (athenaeum#1960 AC2: "honouring environment
# markers"), both directions.
# ---------------------------------------------------------------------------

_MARKER_PYPROJECT_TEMPLATE = """
[project]
name = "demo"
dependencies = []

[project.optional-dependencies]
mcp = ['marker-demo==1.0; {marker}']
vector = []
"""


def test_marker_false_in_this_environment_is_not_required_to_have_a_pin() -> None:
    # A dependency gated on a marker that evaluates FALSE here must not be
    # required to carry a lock pin -- same rule `pip -c` itself applies
    # (requirements-ci.lock's header: "a constraint for a package not needed
    # on a given interpreter is simply ignored").
    pyproject_text = _MARKER_PYPROJECT_TEMPLATE.format(marker='python_version < "3.0"')
    assert check_parity(pyproject_text, "") == []


def test_marker_true_in_this_environment_still_requires_a_pin() -> None:
    # The flip side: a marker that DOES apply here still needs its pin, so
    # marker handling can't be used to silently exempt a real dependency.
    pyproject_text = _MARKER_PYPROJECT_TEMPLATE.format(marker='python_version >= "3.0"')
    violations = check_parity(pyproject_text, "")
    assert len(violations) == 1, violations
    assert violations[0].startswith("marker-demo:")
    assert "no pin" in violations[0]
