# SPDX-License-Identifier: Apache-2.0
"""Offline proof that ``evals.yml`` can choose the north-star model, replicate
set and probe subset -- issue athenaeum#2309.

Before this issue the ``north-star`` dispatch declared no ``--model``,
``--replicates`` or ``--probes`` input at all, so every dispatch ran the CLI's
default tier, over every probe, at one replicate. That made two things
impossible to dispatch: a replicate-to-replicate noise floor (needs more than
one replicate index in ONE store) and a higher-tier floor for the comparison
on athenaeum#2310 (needs a model other than the default).

Each test asserts BOTH halves of the wiring -- the input exists AND the run
step actually passes it to ``north_star_cli`` -- because an input declared and
never read is the failure mode a prose-only check misses.

UNMARKED -- no network, no credential; reads the committed workflow file.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
EVALS_YML = REPO_ROOT / ".github" / "workflows" / "evals.yml"


def _workflow() -> dict:
    return yaml.safe_load(EVALS_YML.read_text(encoding="utf-8"))


def _dispatch_inputs() -> dict:
    # `on:` parses to the boolean True under YAML 1.1, which is why this is
    # keyed by True and not by the string "on".
    return _workflow()[True]["workflow_dispatch"]["inputs"]


def _north_star_run_step() -> dict:
    steps = _workflow()["jobs"]["north-star"]["steps"]
    for step in steps:
        if step.get("name") == "Run north-star grid":
            return step
    raise AssertionError("north-star job has no 'Run north-star grid' step")


@pytest.mark.parametrize(
    ("input_name", "env_name", "cli_flag"),
    [
        ("north_star_model", "NORTH_STAR_MODEL", "--model"),
        ("north_star_replicates", "NORTH_STAR_REPLICATES", "--replicates"),
        ("north_star_probes", "NORTH_STAR_PROBES", "--probes"),
    ],
)
def test_input_is_declared_and_reaches_the_cli(
    input_name: str, env_name: str, cli_flag: str
) -> None:
    inputs = _dispatch_inputs()
    assert input_name in inputs, f"{input_name} is not a workflow_dispatch input"
    # Blank default: a dispatch that names none of the three must be
    # byte-identical to one predating these inputs.
    assert inputs[input_name].get("default", "") == ""
    assert inputs[input_name].get("required", False) is False

    step = _north_star_run_step()
    assert step["env"][env_name] == ("${{ github.event.inputs." + input_name + " }}"), (
        f"{env_name} is not routed from the {input_name} input through env:"
    )

    script = step["run"]
    # Routed through env: and quoted into an argv array, never interpolated
    # into the run: text (the athenaeum#1733 Quine review rule).
    assert f'"${env_name}:-"' in script or f'"${{{env_name}:-}}"' in script
    assert f'{cli_flag} "${env_name}"' in script
    assert "${{" not in script, "dispatch inputs must not be interpolated into run:"


def test_optional_flags_are_left_off_when_blank() -> None:
    """A blank input adds NO argv element, so the CLI's own default applies."""
    script = _north_star_run_step()["run"]
    for array in ("MODEL_FLAG", "REPLICATES_FLAG", "PROBES_FLAG"):
        assert f"{array}=()" in script
        assert f'"${{{array}[@]}}"' in script
