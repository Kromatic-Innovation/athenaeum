# SPDX-License-Identifier: Apache-2.0
"""The bundled skills/ directory must ship in the BUILT artifact (issue #419).

A committed file is not enough: the panelist precedent (a `.claude/agents/`
persona file committed to git but silently excluded by an npm `files` allowlist)
is exactly the failure this guards against. A top-level `skills/` lives outside
`packages = ["src/athenaeum"]`, so it only reaches the wheel via the explicit
`[tool.hatch.build.targets.wheel.force-include]` mapping in pyproject.toml.

These tests assert three things, cheapest first:
1. the skill file exists in git with valid frontmatter,
2. pyproject declares the force-include mapping,
3. a freshly built wheel actually contains `athenaeum/skills/…`.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SKILL = _REPO_ROOT / "skills" / "adapter-authoring" / "SKILL.md"


def test_skill_file_exists_with_frontmatter() -> None:
    assert _SKILL.is_file(), f"missing bundled skill at {_SKILL}"
    text = _SKILL.read_text(encoding="utf-8")
    meta, body = _split_frontmatter(text)
    # A self-describing name/frontmatter so skill-discovery tooling lists it.
    assert meta.get("name") == "adapter-authoring"
    assert meta.get("description"), "skill needs a description for discovery"
    assert body.strip(), "skill body must not be empty"


def test_pyproject_force_includes_skills() -> None:
    with (_REPO_ROOT / "pyproject.toml").open("rb") as fh:
        pyproject = tomllib.load(fh)
    force_include = (
        pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    )
    assert force_include.get("skills") == "athenaeum/skills", (
        "wheel must force-include skills/ -> athenaeum/skills so the bundled "
        "skill ships in the built wheel, not just in git"
    )


def test_built_wheel_contains_skill() -> None:
    """Build a wheel and assert the skill is inside it (the real acceptance bar)."""
    build_spec = pytest.importorskip(
        "build", reason="`build` not installed; `pip install athenaeum[dev]`"
    )
    assert build_spec  # module present

    import tempfile

    with tempfile.TemporaryDirectory() as outdir:
        # --no-isolation reuses the already-installed hatchling backend so the
        # test does not hit the network to provision a build environment.
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--no-isolation",
                "--outdir",
                outdir,
                str(_REPO_ROOT),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"wheel build failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        wheels = list(Path(outdir).glob("*.whl"))
        assert len(wheels) == 1, f"expected one wheel, got {wheels}"
        with zipfile.ZipFile(wheels[0]) as zf:
            names = zf.namelist()
        assert "athenaeum/skills/adapter-authoring/SKILL.md" in names, (
            "bundled skill missing from the built wheel; force-include is broken. "
            "wheel contents (skills):\n"
            + "\n".join(n for n in names if "skills" in n)
        )


def _split_frontmatter(text: str) -> tuple[dict, str]:
    import yaml

    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    return yaml.safe_load(parts[1]) or {}, parts[2]
