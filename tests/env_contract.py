# SPDX-License-Identifier: Apache-2.0
"""The test suite's environment contract, as code (issue athenaeum#2136).

Parts of this suite depend on properties of the MACHINE, not of athenaeum: an
optional extra being installed, or dependencies living somewhere a child venv
can reach. CI satisfies all of them, so CI is green; a contributor following
CONTRIBUTING.md's `pip install -e ".[dev]"` does not, and used to meet a wall
of hard FAILURES that look like a broken repo. The Zenodotus 0.22.43 release
panel hit exactly that: 23 failures on a clean venv, every one of them an
unmet environment precondition rather than a defect.

A test that cannot run because the environment does not meet a documented
precondition must **skip, with that precondition named** -- never fail. This
module is the one place those preconditions are written down, so the suite's
behaviour and CONTRIBUTING.md's "Test-suite environment contract" section
cannot drift apart.

Usage, at the top of a test (or in a class-scoped fixture)::

    from tests.env_contract import require

    def test_something_needing_the_vector_backend() -> None:
        require("vector_backend")

STRICT MODE (``ATHENAEUM_TEST_ENV_CONTRACT=strict``) turns every skip here
into a FAILURE. That exists for CI's `clean-venv` job: that job's whole
purpose is to prove the unconstrained-range install can still run the suite,
and a silent skip there would let a genuinely missing dependency hide behind
"skipped" -- the exact masking the undeclared-`httpx` incident produced. Any
test added to that job's smoke subset must therefore satisfy every contract
property a `[dev]`-only install provides; see the job's own comment.

This module deliberately does NOT replace the 44 existing
``pytest.importorskip("chromadb")`` call sites. Those are the same idea and
remain correct; this adds the strict-mode-aware variant for the sites that
were MISSING a guard, and gives the contract a documented name.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable

import pytest

#: Env var that promotes a contract skip to a failure. See the module docstring.
STRICT_ENV_VAR = "ATHENAEUM_TEST_ENV_CONTRACT"


def _vector_backend_unmet() -> str | None:
    try:
        import chromadb  # noqa: F401  # PLC0415: probe, deliberately lazy
    except ImportError:
        return (
            "the vector backend needs `chromadb`, which only the `[vector]` extra "
            'installs -- `pip install -e ".[dev,vector]"` (what CI installs)'
        )
    return None


def _runtime_deps_in_base_interpreter_unmet() -> str | None:
    # A `--system-site-packages` child venv chains to sys.base_prefix's
    # site-packages, NOT to an intermediate venv's: PEP 405 venvs do not nest.
    # So a test that builds such a child and expects athenaeum's RUNTIME deps
    # to be importable there only works when those deps live in the base
    # interpreter -- true for CI (`pip install` straight into the
    # actions/setup-python interpreter), false for the venv CONTRIBUTING.md
    # tells contributors to create.
    try:
        import yaml  # noqa: F401  # PLC0415: probe, deliberately lazy
    except ImportError:  # pragma: no cover - pyyaml is a core dependency
        return "pyyaml is not installed at all"
    import yaml as _yaml

    location = Path(_yaml.__file__).resolve()
    if location.is_relative_to(Path(sys.base_prefix).resolve()):
        return None
    return (
        f"athenaeum's runtime dependencies are installed in {sys.prefix} (a "
        f"virtualenv), not in the base interpreter at {sys.base_prefix}, so a "
        "`--system-site-packages` child venv cannot import them (PEP 405 venvs "
        "do not nest). CI installs into the interpreter itself, where this holds"
    )


#: name -> (one-line description for the docs, probe returning the unmet reason)
PROPERTIES: dict[str, tuple[str, Callable[[], str | None]]] = {
    "vector_backend": (
        "`chromadb` is importable (the `[vector]` extra).",
        _vector_backend_unmet,
    ),
    "runtime_deps_in_base_interpreter": (
        "athenaeum's runtime dependencies live in the base interpreter's "
        "site-packages, so a `--system-site-packages` child venv can import them.",
        _runtime_deps_in_base_interpreter_unmet,
    ),
}


def strict() -> bool:
    """True when an unmet contract property must FAIL rather than skip."""
    return os.environ.get(STRICT_ENV_VAR, "").strip().lower() == "strict"


def unmet(name: str) -> str | None:
    """The reason *name* is unmet in this environment, or ``None`` if it is met."""
    try:
        _, probe = PROPERTIES[name]
    except KeyError:
        raise KeyError(
            f"unknown environment-contract property {name!r}; "
            f"known: {sorted(PROPERTIES)}"
        ) from None
    return probe()


def require(name: str) -> None:
    """Skip the current test when the *name* contract property is unmet.

    Fails instead of skipping under ``ATHENAEUM_TEST_ENV_CONTRACT=strict``.
    """
    reason = unmet(name)
    if reason is None:
        return
    message = f"environment contract {name!r} unmet: {reason}"
    if strict():
        pytest.fail(
            f"{message}\n(a contract SKIP is a failure under "
            f"{STRICT_ENV_VAR}=strict -- see tests/env_contract.py)"
        )
    pytest.skip(message)
