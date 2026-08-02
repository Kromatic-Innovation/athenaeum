# SPDX-License-Identifier: Apache-2.0
"""Installed-metadata vs. source version-drift check (issue #685).

An editable install (``pip install -e .``) picks up code changes on a git
fast-forward but **not** metadata changes: the ``.dist-info`` version is frozen
at install time. So ``importlib.metadata.version("athenaeum")`` — which is what
:data:`athenaeum.__version__`, and therefore everything that reports, logs, or
gates on the version, reads — can silently lag the deploy tree's
``pyproject.toml`` across a version bump. The live deploy reported ``0.16.1``
while running ``0.16.3`` for exactly this reason.

A drifted install produces no error and degrades nothing, so nothing surfaces
it: the version string is the thing you check to decide whether a fix is live,
and it lies. This module makes the drift **loud**:

- :func:`check_version_drift` compares the installed distribution version against
  the version declared in a deploy tree's ``pyproject.toml``;
- it **fails loudly** (:class:`VersionDriftError`) when it cannot determine
  either side — a comparison that silently passes because one version was
  unreadable would reproduce this bug in a new place;
- :func:`main` is the CLI surface (``python -m athenaeum.deploy_check --check
  <tree>``) the deploy guard runs to decide whether to refresh the metadata.
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path

#: Exit codes, mirroring ``scripts/deploy-guard.sh --check``'s convention so the
#: guard can branch on them directly: 0 in-sync, 10 drift, 20 undetermined.
EXIT_IN_SYNC = 0
EXIT_DRIFT = 10
EXIT_UNDETERMINED = 20

DIST_NAME = "athenaeum"


class VersionDriftError(RuntimeError):
    """A version could not be determined — the fail-loud path.

    Raised when the installed distribution metadata is absent, or the deploy
    tree's ``pyproject.toml`` is missing / unparseable / carries no
    ``[project].version``. Never swallowed into a silent "in sync": an
    undetermined version is a distinct, reportable state from a matching one.
    """


def installed_version(dist: str = DIST_NAME) -> str:
    """Return the installed distribution's version from its ``.dist-info``.

    This is the exact value :data:`athenaeum.__version__` reads, so it is the
    version the runtime *reports*. Raises :class:`VersionDriftError` when the
    distribution is not installed (a bare source checkout), rather than
    returning a placeholder that could mask the drift.
    """
    try:
        return _pkg_version(dist)
    except PackageNotFoundError as exc:
        raise VersionDriftError(
            f"cannot determine the installed version of {dist!r}: distribution "
            "metadata not found — is it `pip install`-ed in this environment?"
        ) from exc


def pyproject_version(tree: Path) -> str:
    """Return the ``[project].version`` declared in ``tree/pyproject.toml``.

    This is the version the deploy tree's *source* declares — what the install
    metadata should match. Raises :class:`VersionDriftError` when the file is
    unreadable, unparseable, or carries no string ``[project].version``.
    """
    path = tree / "pyproject.toml"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise VersionDriftError(f"cannot read {path}: {exc}") from exc
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        raise VersionDriftError(f"cannot parse {path}: {exc}") from exc
    try:
        value = data["project"]["version"]
    except (KeyError, TypeError) as exc:
        raise VersionDriftError(
            f"{path} declares no [project].version (dynamic versioning is not "
            "supported by this check)"
        ) from exc
    if not isinstance(value, str) or not value.strip():
        raise VersionDriftError(f"{path} [project].version is empty or non-string")
    return value.strip()


@dataclass(frozen=True)
class DriftResult:
    """The outcome of a version-drift check."""

    installed: str
    declared: str

    @property
    def in_sync(self) -> bool:
        return self.installed == self.declared


def check_version_drift(tree: Path, *, dist: str = DIST_NAME) -> DriftResult:
    """Compare the installed *dist* version against *tree*'s declared version.

    Both sides are read strictly: either being undeterminable raises
    :class:`VersionDriftError` (the fail-loud contract) rather than producing a
    false in-sync verdict.
    """
    return DriftResult(installed=installed_version(dist), declared=pyproject_version(tree))


def main(argv: list[str] | None = None) -> int:
    """CLI: report installed-vs-declared version drift for a deploy tree.

    ``python -m athenaeum.deploy_check --check <tree>`` prints the two versions
    and exits ``0`` (in-sync), ``10`` (drift), or ``20`` (a version could not be
    determined — printed loudly to stderr). The deploy guard branches on these
    codes to decide whether to refresh the editable install's metadata.
    """
    parser = argparse.ArgumentParser(
        prog="athenaeum.deploy_check",
        description="Detect installed-metadata vs pyproject version drift (#685).",
    )
    parser.add_argument(
        "tree",
        nargs="?",
        default=".",
        help="deploy tree containing pyproject.toml (default: current directory)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report only; never mutate (kept for symmetry with deploy-guard --check)",
    )
    parser.add_argument("--dist", default=DIST_NAME, help="distribution name to check")
    args = parser.parse_args(argv)

    try:
        result = check_version_drift(Path(args.tree), dist=args.dist)
    except VersionDriftError as exc:
        print(f"version-check: UNDETERMINED — {exc}", file=sys.stderr)
        return EXIT_UNDETERMINED

    if result.in_sync:
        print(f"in-sync {result.installed}")
        return EXIT_IN_SYNC
    print(
        f"drift installed={result.installed} declared={result.declared} "
        f"(the runtime reports {result.installed} while the tree is {result.declared})",
        file=sys.stderr,
    )
    return EXIT_DRIFT


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess/CLI
    sys.exit(main())
