# SPDX-License-Identifier: Apache-2.0
"""Tests for the ``YesNoScorer`` seam (issue athenaeum#2063).

Every fake worker here is a small stdlib script run with ``sys.executable``
-- no test imports torch or transformers, and no test launches the real
``_yesno_worker.py`` against real weights.
"""

from __future__ import annotations

import stat
import sys
import textwrap
from pathlib import Path

import pytest

from athenaeum.yesno import (
    SubprocessYesNoScorer,
    YesNoScorerUnavailable,
    preflight_yesno_scorer,
)


def _write_fake_worker(path: Path, body: str) -> Path:
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


_OK_WORKER = """\
import json
import sys

print(json.dumps({"load_secs": 0.01}))
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    row = json.loads(line)
    # Deterministic score from input length -- never echoes the text.
    print(json.dumps({"p": min(1.0, len(row["t"]) / 100.0)}))
"""


class TestSubprocessYesNoScorerHappyPath:
    def test_scores_each_text_in_order(self, tmp_path: Path) -> None:
        worker = _write_fake_worker(tmp_path / "worker.py", _OK_WORKER)
        scorer = SubprocessYesNoScorer(
            interpreter=sys.executable,
            weights_path=tmp_path,
            worker_path=worker,
        )
        out = scorer.score("q?", ["a" * 10, "a" * 50])
        assert out == [pytest.approx(0.1), pytest.approx(0.5)]
        assert scorer.last_load_secs == pytest.approx(0.01)

    def test_empty_texts_short_circuits_with_no_subprocess(self, tmp_path: Path) -> None:
        # Worker path does not even need to exist -- score([]) never launches.
        scorer = SubprocessYesNoScorer(
            interpreter=sys.executable,
            weights_path=tmp_path,
            worker_path=tmp_path / "does-not-exist.py",
        )
        assert scorer.score("q?", []) == []

    def test_argv_never_carries_text(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}
        import subprocess as subprocess_mod

        real_run = subprocess_mod.run

        def spy_run(argv, **kwargs):
            captured["argv"] = argv
            return real_run(argv, **kwargs)

        monkeypatch.setattr(subprocess_mod, "run", spy_run)
        worker = _write_fake_worker(tmp_path / "worker.py", _OK_WORKER)
        scorer = SubprocessYesNoScorer(
            interpreter=sys.executable, weights_path=tmp_path, worker_path=worker
        )
        scorer.score("is this a secret-token-shaped string?", ["sentinel-value@example.invalid"])
        for arg in captured["argv"]:
            assert "sentinel-value@example.invalid" not in str(arg)
            assert "secret-token-shaped" not in str(arg)


class TestSubprocessYesNoScorerFailClosed:
    def test_nonzero_exit_raises_unavailable(self, tmp_path: Path) -> None:
        worker = _write_fake_worker(
            tmp_path / "worker.py", 'import sys\nprint("boom", file=sys.stderr)\nsys.exit(1)\n'
        )
        scorer = SubprocessYesNoScorer(
            interpreter=sys.executable, weights_path=tmp_path, worker_path=worker
        )
        with pytest.raises(YesNoScorerUnavailable) as exc_info:
            scorer.score("q?", ["x"])
        # Stderr content ("boom") is never relayed into the exception text.
        assert "boom" not in str(exc_info.value)

    def test_timeout_raises_unavailable(self, tmp_path: Path) -> None:
        worker = _write_fake_worker(tmp_path / "worker.py", "import time\ntime.sleep(5)\n")
        scorer = SubprocessYesNoScorer(
            interpreter=sys.executable,
            weights_path=tmp_path,
            worker_path=worker,
            timeout_secs=0.2,
        )
        with pytest.raises(YesNoScorerUnavailable):
            scorer.score("q?", ["x"])

    def test_wrong_row_count_raises_unavailable(self, tmp_path: Path) -> None:
        body = """\
        import json
        print(json.dumps({"load_secs": 0.0}))
        print(json.dumps({"p": 0.5}))
        """
        worker = _write_fake_worker(tmp_path / "worker.py", body)
        scorer = SubprocessYesNoScorer(
            interpreter=sys.executable, weights_path=tmp_path, worker_path=worker
        )
        with pytest.raises(YesNoScorerUnavailable):
            scorer.score("q?", ["x", "y"])

    def test_non_float_row_raises_unavailable(self, tmp_path: Path) -> None:
        body = """\
        import json
        print(json.dumps({"load_secs": 0.0}))
        print(json.dumps({"p": "not-a-float"}))
        """
        worker = _write_fake_worker(tmp_path / "worker.py", body)
        scorer = SubprocessYesNoScorer(
            interpreter=sys.executable, weights_path=tmp_path, worker_path=worker
        )
        with pytest.raises(YesNoScorerUnavailable):
            scorer.score("q?", ["x"])

    def test_out_of_range_row_raises_unavailable(self, tmp_path: Path) -> None:
        body = """\
        import json
        print(json.dumps({"load_secs": 0.0}))
        print(json.dumps({"p": 1.5}))
        """
        worker = _write_fake_worker(tmp_path / "worker.py", body)
        scorer = SubprocessYesNoScorer(
            interpreter=sys.executable, weights_path=tmp_path, worker_path=worker
        )
        with pytest.raises(YesNoScorerUnavailable):
            scorer.score("q?", ["x"])

    def test_missing_meta_row_raises_unavailable(self, tmp_path: Path) -> None:
        body = """\
        import json
        print(json.dumps({"p": 0.5}))
        """
        worker = _write_fake_worker(tmp_path / "worker.py", body)
        scorer = SubprocessYesNoScorer(
            interpreter=sys.executable, weights_path=tmp_path, worker_path=worker
        )
        with pytest.raises(YesNoScorerUnavailable):
            scorer.score("q?", ["x"])

    def test_no_such_interpreter_raises_unavailable(self, tmp_path: Path) -> None:
        scorer = SubprocessYesNoScorer(
            interpreter="/no/such/interpreter-binary-xyz",
            weights_path=tmp_path,
            worker_path=tmp_path / "worker.py",
        )
        with pytest.raises(YesNoScorerUnavailable):
            scorer.score("q?", ["x"])


class TestPreflightYesNoScorer:
    def test_unset_weights_path(self) -> None:
        msg = preflight_yesno_scorer(interpreter="python3", weights_path=None)
        assert msg is not None
        assert "weights_path" in msg

    def test_weights_path_not_a_directory(self, tmp_path: Path) -> None:
        f = tmp_path / "not-a-dir"
        f.write_text("x", encoding="utf-8")
        msg = preflight_yesno_scorer(interpreter="python3", weights_path=str(f))
        assert msg is not None
        assert "directory" in msg

    def test_unset_interpreter(self, tmp_path: Path) -> None:
        msg = preflight_yesno_scorer(interpreter=None, weights_path=str(tmp_path))
        assert msg is not None
        assert "interpreter" in msg

    def test_interpreter_not_found(self, tmp_path: Path) -> None:
        msg = preflight_yesno_scorer(
            interpreter="/no/such/interpreter-binary-xyz", weights_path=str(tmp_path)
        )
        assert msg is not None

    def test_interpreter_found_by_absolute_path(self, tmp_path: Path) -> None:
        fake_interp = tmp_path / "fake-python"
        fake_interp.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_interp.chmod(fake_interp.stat().st_mode | stat.S_IEXEC)
        msg = preflight_yesno_scorer(interpreter=str(fake_interp), weights_path=str(tmp_path))
        assert msg is None

    def test_interpreter_found_on_path(self, tmp_path: Path) -> None:
        msg = preflight_yesno_scorer(interpreter=sys.executable, weights_path=str(tmp_path))
        assert msg is None

    def test_none_message_is_never_raised_never_logged_as_exception(self, tmp_path: Path) -> None:
        # Loud but non-raising: a caller can safely treat any non-None
        # return as "print and fall back", never as an exception path.
        assert preflight_yesno_scorer(interpreter=None, weights_path=None) is not None
