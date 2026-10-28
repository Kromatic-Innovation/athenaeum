# SPDX-License-Identifier: Apache-2.0
"""promptwitness must ship in the BUILT wheel, not just in git (athenaeum#2147 AC).

Same acceptance bar ``tests/test_skill_packaging.py`` already applies to the
bundled ``skills/`` directory: a committed file is not enough, so this
builds a real wheel and inspects its ``RECORD``.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _build_wheel(outdir: str) -> Path:
    pytest.importorskip("build", reason="`build` not installed; `pip install athenaeum[dev]`")
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
    assert len(wheels) == 1, f"expected exactly one wheel, found {wheels}"
    return wheels[0]


def test_promptwitness_listed_in_built_wheel_record() -> None:
    with tempfile.TemporaryDirectory() as outdir:
        wheel_path = _build_wheel(outdir)
        with zipfile.ZipFile(wheel_path) as zf:
            names = zf.namelist()
            record_name = next(n for n in names if n.endswith("RECORD"))
            record_text = zf.read(record_name).decode("utf-8")
        assert "promptwitness" in record_text, "promptwitness is missing from the wheel's RECORD"
        assert any(n.startswith("promptwitness/") for n in names), (
            f"no promptwitness/* entries in the built wheel; saw: {sorted(names)[:20]}..."
        )
        assert "promptwitness/py.typed" in names
        assert "promptwitness/cli.py" in names
        assert "promptwitness/ledger.py" in names
        assert "promptwitness/canon.py" in names


def test_pyproject_declares_promptwitness_as_a_wheel_package() -> None:
    import tomllib

    with (_REPO_ROOT / "pyproject.toml").open("rb") as fh:
        pyproject = tomllib.load(fh)
    packages = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert "src/promptwitness" in packages


def test_promptwitness_console_script_declared() -> None:
    import tomllib

    with (_REPO_ROOT / "pyproject.toml").open("rb") as fh:
        pyproject = tomllib.load(fh)
    scripts = pyproject["project"]["scripts"]
    assert scripts.get("promptwitness") == "promptwitness.cli:main"


def test_import_promptwitness_works_in_this_environment() -> None:
    """The practical form of the 'fresh pip install -e . makes `import
    promptwitness` work' AC: this suite itself runs inside an editable
    install of the current pyproject.toml (CI's `clean-venv`/locked jobs
    both install editable), so a successful import here, run freshly in a
    subprocess with no leftover sys.path hacks, is the same install path.
    """
    probe = "import promptwitness, promptwitness.canon, promptwitness.ledger, promptwitness.cli"
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
