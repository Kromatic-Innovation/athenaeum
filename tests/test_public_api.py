# SPDX-License-Identifier: Apache-2.0
"""Regression tests for the stable Python public API surface.

Catches drift between ``athenaeum.__all__``, ``athenaeum.__version__``,
and the ``version`` field in ``pyproject.toml``. These three must agree;
a 0.4.0 release that self-identifies as 0.3.1 is what triggered the
Zenodotus FAIL verdict on the prior candidate.
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

import athenaeum


def test_all_names_are_importable() -> None:
    """Every name in ``__all__`` must actually resolve on the package."""
    for name in athenaeum.__all__:
        assert hasattr(
            athenaeum, name
        ), f"athenaeum.__all__ lists {name!r} but it is not importable"


def test_version_matches_pyproject() -> None:
    """``__version__`` must match the version declared in pyproject.toml."""
    pyproject_path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    with pyproject_path.open("rb") as fh:
        pyproject = tomllib.load(fh)
    declared = pyproject["project"]["version"]
    assert athenaeum.__version__ == declared, (
        f"athenaeum.__version__ ({athenaeum.__version__!r}) "
        f"does not match pyproject.toml ({declared!r})"
    )


def test_package_reimport_is_idempotent() -> None:
    """Reimporting the package must not raise or mutate ``__all__``."""
    before = tuple(athenaeum.__all__)
    importlib.reload(athenaeum)
    after = tuple(athenaeum.__all__)
    assert before == after
