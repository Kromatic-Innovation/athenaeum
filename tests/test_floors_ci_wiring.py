# SPDX-License-Identifier: Apache-2.0
"""The `floors` CI job installs exactly the floors pyproject.toml declares (athenaeum#2909).

The job pins `anthropic`, `fastmcp` and `mcp` to their declared minimums. If a
floor is raised in pyproject.toml and the job is not updated, CI keeps testing a
version the package no longer claims to support. Offline: reads two files.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _declared_floors() -> dict[str, str]:
    meta = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    reqs = list(meta["dependencies"]) + list(meta["optional-dependencies"]["mcp"])
    floors: dict[str, str] = {}
    for req in reqs:
        m = re.match(r"(anthropic|fastmcp|mcp)>=([0-9][^,<]*)", req)
        if m:
            floors[m.group(1)] = m.group(2)
    return floors


def _install_script() -> str:
    job = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))["jobs"]["floors"]
    return "\n".join(step.get("run", "") for step in job["steps"])


def test_floors_job_pins_match_pyproject() -> None:
    floors = _declared_floors()
    assert set(floors) == {"anthropic", "fastmcp", "mcp"}
    script = _install_script()
    for name, version in floors.items():
        # `mcp>=2.0` is the release `2.0.0`.
        version = ".".join((version.split(".") + ["0", "0"])[:3])
        assert f"'{name}=={version}'" in script, (
            f"ci.yml `floors` job does not pin {name}=={version}, the floor "
            "declared in pyproject.toml"
        )


def test_floors_job_does_not_gate_the_required_aggregate() -> None:
    # Documented choice: the floors job is an advisory signal.
    needs = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))["jobs"]["ci-required"]["needs"]
    assert "floors" not in needs
