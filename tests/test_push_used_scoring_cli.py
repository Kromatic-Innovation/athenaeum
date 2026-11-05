# SPDX-License-Identifier: Apache-2.0
"""Tests for `athenaeum push-metrics {sample-for-labelling,score-used}`
(issue athenaeum#2302)."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from athenaeum import push_metrics
from athenaeum.cli import main as cli_main


def _run(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


def _run_capture_stderr(argv: list[str]) -> tuple[int, str, str]:
    out_buf, err_buf = io.StringIO(), io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = cli_main(argv)
    return rc, out_buf.getvalue(), err_buf.getvalue()


def _seed_one_push(cache_dir: Path, session_id: str, push_id: str) -> None:
    push_metrics.record_push(
        push_metrics.build_push_record(
            session_id=session_id,
            query="q",
            backend="fts5",
            hits=[(f"{push_id}.md", {"uid": push_id}, push_metrics.estimate_tokens("b"))],
        ),
        cache_dir=cache_dir,
    )


class TestSampleForLabelling:
    def test_writes_worksheet_and_reports_count(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        _seed_one_push(cache_dir, "sess-1", "page-1")
        output_path = tmp_path / "out" / "worksheet.jsonl"

        rc, out = _run(
            [
                "push-metrics",
                "sample-for-labelling",
                "--cache-dir",
                str(cache_dir),
                "--output",
                str(output_path),
                "--seed",
                "1",
            ]
        )
        assert rc == 0
        assert "sampled 1 pushed page" in out
        row = json.loads(output_path.read_text().splitlines()[0])
        assert row["session_id"] == "sess-1"
        assert row["push_id"] == "page-1"
        assert row["relevant"] is None
        assert row["changed_work"] is None

    def test_json_output(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        _seed_one_push(cache_dir, "sess-1", "page-1")
        output_path = tmp_path / "worksheet.jsonl"

        rc, out = _run(
            [
                "push-metrics",
                "sample-for-labelling",
                "--cache-dir",
                str(cache_dir),
                "--output",
                str(output_path),
                "--json",
            ]
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["sampled_count"] == 1
        assert payload["output_path"] == str(output_path.resolve())

    def test_default_output_is_outside_repo(self, tmp_path: Path) -> None:
        """No --output given: the default must resolve under the cache dir,
        never inside this repo's working tree (athenaeum#2302 AC)."""
        cache_dir = tmp_path / "cache"
        _seed_one_push(cache_dir, "sess-1", "page-1")

        rc, out = _run(
            [
                "push-metrics",
                "sample-for-labelling",
                "--cache-dir",
                str(cache_dir),
                "--json",
            ]
        )
        assert rc == 0
        payload = json.loads(out)
        repo_root = Path(__file__).resolve().parent.parent
        written_path = Path(payload["output_path"])
        assert repo_root not in written_path.parents
        assert written_path == (cache_dir / "push_used_scoring" / "sample-for-labelling.jsonl")
        assert written_path.exists()


class TestScoreUsed:
    def test_labels_is_required(self) -> None:
        # argparse's own `required=True` handling calls sys.exit(2) directly
        # (never returns), so this is a SystemExit, not a return code --
        # unlike every other error path this module reports via a 1 return.
        err_buf = io.StringIO()
        with redirect_stderr(err_buf), pytest.raises(SystemExit) as exc_info:
            cli_main(["push-metrics", "score-used"])
        assert exc_info.value.code == 2
        assert "--labels" in err_buf.getvalue()

    def test_scores_against_real_heuristic(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        projects_root = tmp_path / "projects"
        _seed_one_push(cache_dir, "sess-1", "cited11")

        scope_dir = projects_root / "-scope-hash"
        scope_dir.mkdir(parents=True)
        (scope_dir / "sess-1.jsonl").write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Answered using cited11."}],
                    },
                }
            )
            + "\n"
        )

        labels_path = tmp_path / "outside-repo-labels" / "labels.jsonl"
        labels_path.parent.mkdir(parents=True)
        labels_path.write_text(
            json.dumps(
                {
                    "session_id": "sess-1",
                    "push_id": "cited11",
                    "relevant": True,
                    "changed_work": True,
                }
            )
            + "\n"
        )

        output_path = tmp_path / "report.json"
        rc, out = _run(
            [
                "push-metrics",
                "score-used",
                "--cache-dir",
                str(cache_dir),
                "--path",
                str(tmp_path / "knowledge"),
                "--projects-root",
                str(projects_root),
                "--labels",
                str(labels_path),
                "--output",
                str(output_path),
                "--json",
            ]
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["confusion"] == {"tp": 1, "fp": 0, "fn": 0, "tn": 0, "total": 1}
        report_on_disk = json.loads(output_path.read_text())
        assert report_on_disk["confusion"]["tp"] == 1

    def test_default_output_is_outside_repo(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        labels_path = tmp_path / "outside-repo-labels" / "labels.jsonl"
        labels_path.parent.mkdir(parents=True)
        labels_path.write_text(
            json.dumps(
                {
                    "session_id": "sess-1",
                    "push_id": "page-1",
                    "relevant": False,
                    "changed_work": False,
                }
            )
            + "\n"
        )
        rc, out = _run(
            [
                "push-metrics",
                "score-used",
                "--cache-dir",
                str(cache_dir),
                "--labels",
                str(labels_path),
                "--json",
            ]
        )
        assert rc == 0
        payload = json.loads(out)
        repo_root = Path(__file__).resolve().parent.parent
        # The score report itself carries no output_path echo (rates/counts
        # only), so assert directly against the known default location.
        written_path = cache_dir / "push_used_scoring" / "score-report.json"
        assert repo_root not in written_path.parents
        assert written_path.exists()
        assert payload["confusion"]["tn"] == 1

    def test_empty_labels_file_is_an_error(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        labels_path = tmp_path / "labels.jsonl"
        labels_path.write_text("")
        rc, _out, err = _run_capture_stderr(
            [
                "push-metrics",
                "score-used",
                "--cache-dir",
                str(cache_dir),
                "--labels",
                str(labels_path),
            ]
        )
        assert rc == 1
        assert "zero fully-labelled rows" in err

    def test_malformed_labels_file_reports_error(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        labels_path = tmp_path / "labels.jsonl"
        labels_path.write_text("not json\n")
        rc, _out, err = _run_capture_stderr(
            [
                "push-metrics",
                "score-used",
                "--cache-dir",
                str(cache_dir),
                "--labels",
                str(labels_path),
            ]
        )
        assert rc == 1
        assert "invalid JSON" in err
