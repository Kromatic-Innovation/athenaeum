#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Fail fast, by name, on a pyproject.toml/requirements-ci.lock exact-pin
duplicate (issue athenaeum#2324).

WHY THIS EXISTS. `pyproject.toml` exact-pins a handful of dev/type-check
tools with `==` (currently `ruff`) so lint/type results are deterministic
run-to-run. Twice now (athenaeum#1922, hand-fixed in athenaeum#1929; and
athenaeum#2284) a Dependabot PR bumped that `==` pin in `pyproject.toml`
without touching `requirements-ci.lock`, which had frozen its own stale copy
of the same package at the old version. `requirements-ci.lock` is applied as
a CONSTRAINTS file (`pip install -e ".[dev,vector]" -c requirements-ci.lock`),
so the two disagreeing `==` pins made every install step in CI die with
`ResolutionImpossible` before a single test ran -- and the check that
actually failed (`CI Required`, via a skipped `Coverage` job) never named the
real cause.

THE FIX this check enforces. For a direct dependency `pyproject.toml` already
exact-pins, `requirements-ci.lock` has nothing to add -- duplicating that pin
only creates a second place for it to go stale. So the rule is: no package
`pyproject.toml` pins with `==` may ALSO appear in `requirements-ci.lock`.
That makes a `ruff`-shaped bump unable to desync by construction: there is
nothing left in the lock to fall out of step with. See
`requirements-ci.lock`'s own header and `docs/design/security-posture.md`
§3.1 for the full rationale and the alternatives this was chosen over.

WHY A STANDALONE SCRIPT, NOT A PYTEST CASE. This has to run BEFORE the
expensive `pip install -e ".[dev,vector]" -c requirements-ci.lock` step --
that install is exactly what died in athenaeum#1922/#2284, so a check that
only runs *after* it (inside the pytest suite those jobs install first) can
never catch this class early. Pure stdlib (`tomllib`, `re`), so the
`.github/workflows/ci.yml` `pin-parity` job needs no install step of its own:
checkout, then `python3 scripts/check_exact_pin_parity.py`.

`tests/test_exact_pin_parity.py` imports `check_parity` straight from this
module (same pattern as `tests/test_declared_imports.py` importing
`check_declared_imports.py`) so the logic has real pytest coverage, including
a deliberate-mismatch counter-example, without duplicating it.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
LOCKFILE = REPO_ROOT / "requirements-ci.lock"

# `pip freeze` output: `name==version`, optionally followed by a comment.
# Mirrors tests/test_ci_lock_parity.py's `_LOCK_LINE_RE` so the two checks
# agree on what counts as a lock pin.
_LOCK_LINE_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([^\s#;]+)")

# A pyproject.toml requirement string that is EXACTLY one `==` pin: a bare
# name (no extras bracket -- none of today's exact pins use one) followed by
# `==<version>`, optionally followed by an environment marker after `;`.
# Anything with a comma (multiple specifiers, e.g. `>=3.5,<4`) or any other
# operator is a range pin and out of scope for this check by definition.
_EXACT_PIN_RE = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([^\s;,<>!~]+)\s*(?:;.*)?$"
)


def canonicalize(name: str) -> str:
    """PyPI name normalization (PEP 503), without needing `packaging`."""
    return re.sub(r"[-_.]+", "-", name).lower()


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
        pins[canonicalize(name)] = version
    return pins


def exact_pins(pyproject_text: str) -> dict[str, tuple[str, str]]:
    """Every `==`-pinned requirement anywhere in `pyproject.toml`.

    Scans `[project].dependencies` plus every `[project.optional-dependencies]`
    extra -- deliberately NOT scoped to any particular extra, since the rule
    this enforces ("an exact pin lives in exactly one place") applies to any
    direct dependency, wherever it is declared.

    Returns {canonical name: (declared name, pinned version)}.
    """
    data = tomllib.loads(pyproject_text)
    project = data.get("project", {})
    raw_reqs: list[str] = list(project.get("dependencies", []))
    for extra_reqs in project.get("optional-dependencies", {}).values():
        raw_reqs.extend(extra_reqs)

    pins: dict[str, tuple[str, str]] = {}
    for raw in raw_reqs:
        match = _EXACT_PIN_RE.match(raw.strip())
        if not match:
            continue
        name, version = match.group(1), match.group(2)
        pins[canonicalize(name)] = (name, version)
    return pins


def check_parity(pyproject_text: str, lock_text: str) -> list[str]:
    """Return one violation message per exact pin duplicated into the lock.

    Empty means parity holds: nothing `pyproject.toml` exact-pins is ALSO
    frozen into `requirements-ci.lock`.
    """
    violations: list[str] = []
    lock_pins = parse_lock(lock_text)
    for canon, (name, pyproject_version) in sorted(exact_pins(pyproject_text).items()):
        lock_version = lock_pins.get(canon)
        if lock_version is None:
            continue
        violations.append(
            f"{name}: pyproject.toml exact-pins {name}=={pyproject_version}, but "
            f"requirements-ci.lock ALSO pins {name}=={lock_version} -- duplicate "
            "exact pins drift out of sync on every bump (athenaeum#1922, "
            "athenaeum#2284, athenaeum#2324). Remove the requirements-ci.lock "
            "entry so pyproject.toml stays the single source of truth for "
            f"{name}'s version (see requirements-ci.lock's header and "
            "docs/design/security-posture.md §3.1)."
        )
    return violations


def main(argv: list[str] | None = None) -> int:
    del argv  # no flags today; kept for parity with scripts/check_declared_imports.py's main(argv) shape
    violations = check_parity(PYPROJECT.read_text(), LOCKFILE.read_text())
    if violations:
        for message in violations:
            print(f"::error::{message}")
        print(f"FAILED: {len(violations)} exact-pin duplicate(s) between pyproject.toml and requirements-ci.lock")
        return 1
    print("OK: no pyproject.toml exact pin is duplicated in requirements-ci.lock")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
