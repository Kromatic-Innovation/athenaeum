# SPDX-License-Identifier: Apache-2.0
"""Tests for the self-contained ``_yesno_worker`` subprocess (issue athenaeum#2063).

No test here imports torch or transformers -- the shared pure transform
(:func:`p_yes_from_logits`) and :func:`read_temperature` are plain stdlib
math/json, and the real-model load path is exercised only by invoking the
worker as a SUBPROCESS with THIS test venv's interpreter, which has no
``torch``/``transformers`` installed (the ``maya-eval`` extra is
deliberately absent from the default install) -- so that invocation proves
the "import failed" fail-closed path, not the real model.
"""

from __future__ import annotations

import ast
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

from athenaeum import _yesno_worker

_WORKER_PATH = Path(_yesno_worker.__file__).resolve()


class TestPYesFromLogits:
    def test_temperature_one_matches_plain_sigmoid_of_difference(self) -> None:
        p = _yesno_worker.p_yes_from_logits(2.0, 0.5, temperature=1.0)
        assert p == pytest.approx(1.0 / (1.0 + math.exp(-(2.0 - 0.5))))

    def test_temperature_one_point_six_five_is_stricter_at_the_same_logits(self) -> None:
        p_t1 = _yesno_worker.p_yes_from_logits(1.0, 0.0, temperature=1.0)
        p_t165 = _yesno_worker.p_yes_from_logits(1.0, 0.0, temperature=1.65)
        # A positive logit gap is dampened by a temperature > 1 -- P(yes)
        # moves toward 0.5, never away from it (issue athenaeum#2063's
        # "T=1.65 is stricter" finding).
        assert 0.5 < p_t165 < p_t1

    def test_equal_logits_give_point_five_at_any_temperature(self) -> None:
        assert _yesno_worker.p_yes_from_logits(1.0, 1.0, temperature=1.0) == pytest.approx(0.5)
        assert _yesno_worker.p_yes_from_logits(1.0, 1.0, temperature=1.65) == pytest.approx(0.5)

    def test_rejects_non_positive_temperature(self) -> None:
        with pytest.raises(ValueError):
            _yesno_worker.p_yes_from_logits(1.0, 0.0, temperature=0.0)
        with pytest.raises(ValueError):
            _yesno_worker.p_yes_from_logits(1.0, 0.0, temperature=-1.0)


class TestReadTemperature:
    def test_defaults_to_one_when_file_absent(self, tmp_path: Path) -> None:
        assert _yesno_worker.read_temperature(tmp_path) == 1.0

    def test_reads_declared_temperature(self, tmp_path: Path) -> None:
        (tmp_path / "maya_config.json").write_text(
            json.dumps({"temperature": 1.65}), encoding="utf-8"
        )
        assert _yesno_worker.read_temperature(tmp_path) == 1.65

    def test_malformed_json_defaults_to_one(self, tmp_path: Path) -> None:
        (tmp_path / "maya_config.json").write_text("{not json", encoding="utf-8")
        assert _yesno_worker.read_temperature(tmp_path) == 1.0

    def test_non_positive_declared_temperature_defaults_to_one(self, tmp_path: Path) -> None:
        (tmp_path / "maya_config.json").write_text(json.dumps({"temperature": 0}), encoding="utf-8")
        assert _yesno_worker.read_temperature(tmp_path) == 1.0

    def test_non_numeric_declared_temperature_defaults_to_one(self, tmp_path: Path) -> None:
        (tmp_path / "maya_config.json").write_text(
            json.dumps({"temperature": "hot"}), encoding="utf-8"
        )
        assert _yesno_worker.read_temperature(tmp_path) == 1.0


class TestNoAthenaeumImport:
    def test_ast_scan_finds_no_athenaeum_import(self) -> None:
        # Issue athenaeum#2063 AC8: this file must run standalone in an
        # interpreter that may not have athenaeum installed at all.
        tree = ast.parse(_WORKER_PATH.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("athenaeum"), alias.name
            elif isinstance(node, ast.ImportFrom):
                assert node.module is None or not node.module.startswith("athenaeum"), node.module

    def test_offline_env_vars_set_before_any_transformers_import(self) -> None:
        source = _WORKER_PATH.read_text(encoding="utf-8")
        assert "import transformers" not in source.split("def ")[0]
        offline_idx = source.index('os.environ.setdefault("HF_HUB_OFFLINE"')
        first_def_idx = source.index("\ndef ")
        assert offline_idx < first_def_idx


class TestMainSubprocess:
    def test_missing_weights_dir_exits_nonzero_with_one_stderr_line(self, tmp_path: Path) -> None:
        proc = subprocess.run(
            [sys.executable, "-I", str(_WORKER_PATH), str(tmp_path / "nope")],
            input="",
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode != 0
        assert proc.stdout == ""
        assert "yesno worker" in proc.stderr

    def test_no_weights_dir_arg_exits_two(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-I", str(_WORKER_PATH)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 2

    def test_transformers_unavailable_in_this_venv_fails_closed(self, tmp_path: Path) -> None:
        # This test venv deliberately has no torch/transformers (the
        # maya-eval extra is absent from the default install) -- running
        # the worker against a real (empty) weights dir with THIS
        # interpreter exercises the "import failed" fail-closed branch.
        weights_dir = tmp_path / "weights"
        weights_dir.mkdir()
        proc = subprocess.run(
            [sys.executable, "-I", str(_WORKER_PATH), str(weights_dir)],
            input=json.dumps({"q": "is this contact info?", "t": "hello"}) + "\n",
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 1
        assert proc.stdout == ""
        assert "import failed" in proc.stderr
        # Never echoes the input text in the failure message.
        assert "hello" not in proc.stderr
