#!/usr/bin/env python3
"""Deploy-SHA stamp for athenaeum (issue #413).

Mirrors voltaire's ``scripts/write-build-sha.mjs`` (cwc#1102) and hestia's
deploy stamp (hestia#258): writes the running commit SHA to ``dist/.build-sha``
as a single 40-char lowercase-hex line + trailing newline. That byte shape is
what makes the file readable by hestia/voltaire's ``deploy-guard.sh`` and by
the cross-repo deploy-lag aggregator ``compute-deploy-lag.sh``
(code-workspace-config#1428) — those readers do ``tr -d '[:space:]'`` and
expect a bare SHA, so the format must match exactly.

Unlike voltaire (which stamps as an npm ``postbuild`` step after ``tsc``),
athenaeum's MCP server runs from a single source checkout with no separate
build step (see the hestia#691 audit) — so this stamp is written by
``scripts/deploy-sync.sh`` on every deploy-sync, not by a compile step.

``dist/`` is gitignored: the stamp is a local build artifact, never committed.

Root resolution: the repo root defaults to this script's parent's parent
(``scripts/..``). Tests override it via ``ATHENAEUM_BUILD_SHA_ROOT`` so they can
stamp a throwaway git fixture without touching the real ``dist/``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

# A resolved git commit SHA: 40 lowercase hex chars. The stamp readers
# (deploy-guard.sh, compute-deploy-lag.sh) treat the file content as a bare
# SHA, so we refuse to write anything that isn't one.
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

STAMP_RELPATH = Path("dist") / ".build-sha"


def _default_root() -> Path:
    """Repo root: ``ATHENAEUM_BUILD_SHA_ROOT`` if set, else ``scripts/..``."""
    env = os.environ.get("ATHENAEUM_BUILD_SHA_ROOT")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parent.parent


def write_build_sha(root: Path | str | None = None) -> str:
    """Stamp ``<root>/dist/.build-sha`` with ``git -C <root> rev-parse HEAD``.

    Returns the stamped SHA. Raises ``subprocess.CalledProcessError`` when
    ``root`` is not a git checkout and ``ValueError`` when ``git`` returns
    something that is not a 40-char hex SHA (both are surfaced as a non-zero
    exit by :func:`main`).
    """
    resolved = Path(root).resolve() if root is not None else _default_root()

    sha = subprocess.run(
        ["git", "-C", str(resolved), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    if not _SHA_RE.match(sha):
        raise ValueError(f"write_build_sha: unexpected HEAD sha {sha!r}")

    stamp_path = resolved / STAMP_RELPATH
    stamp_path.parent.mkdir(parents=True, exist_ok=True)
    stamp_path.write_text(sha + "\n", encoding="utf-8")
    return sha


def main(argv: list[str] | None = None) -> int:
    root = _default_root()
    try:
        sha = write_build_sha(root)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        print(f"write_build_sha: git rev-parse failed in {root}: {stderr}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"build-sha {sha} -> {root / STAMP_RELPATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
