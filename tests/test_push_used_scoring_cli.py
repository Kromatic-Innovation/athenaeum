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


@pytest.fixture(autouse=True)
def _isolate_default_knowledge_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue athenaeum#2452:
    ``test_unsupported_confidence_reports_a_named_error_not_a_traceback``
    omits ``--path``, so ``_cmd_push_metrics_score_used``'s
    ``wiki_root=_resolve_wiki_root(args)`` falls back to
    ``_cmd_push_metrics.DEFAULT_KNOWLEDGE_ROOT`` (``--path``'s argparse
    default, bound in THIS module — unlike ``_cmd_repair.py``, which no
    longer binds the name; see ``tests/test_repair.py``'s equivalent
    fixture). ``push_metrics._pushed_page_texts`` then lists
    ``wiki_root.iterdir()`` looking for a filename matching the test's
    (single-character) push id by PREFIX — on a real, populated
    ``~/knowledge/wiki`` that lists hundreds of unrelated real pages, one of
    them matches the prefix purely by coincidence, and the subsequent
    ``.read_text()`` on it trips the athenaeum#2183 default-root-open guard.
    On an empty/isolated default the directory listing itself raises
    ``OSError`` (caught, returns ``{}``), so the test's actual intent — an
    unsupported ``--confidence`` value reported as a named CLI error rather
    than an uncaught traceback — is never reached with a populated real
    wiki_root in the loop. This sibling test in the same class,
    ``test_scores_against_real_heuristic``, already learned this lesson
    (passes ``--path`` explicitly); this autouse fixture covers the one
    that didn't, the same way as every other per-file fix in this issue.
    `TestSampleForLabelling`'s tests never consult `args.path` at all
    (`sample-for-labelling` has no wiki_root dependency), so this redirect
    cannot affect them.
    """
    from athenaeum import _cmd_push_metrics

    monkeypatch.setattr(
        _cmd_push_metrics,
        "DEFAULT_KNOWLEDGE_ROOT",
        tmp_path / "unused-default-knowledge-root",
    )


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

    def test_bad_exclude_session_value_is_a_named_error_not_a_silent_noop(
        self, tmp_path: Path
    ) -> None:
        """Regression for the PR review finding on athenaeum#2302: the CLI
        help text claims --exclude-session has the same semantics as
        `coverage-audit --exclude-session` (prefix resolution, raise on an
        unmatched/ambiguous value). A value matching no known session id
        must exit 1 with a named error -- never exit 0 having silently
        excluded nothing from a sample that then looks clean."""
        cache_dir = tmp_path / "cache"
        _seed_one_push(cache_dir, "sess-1", "page-1")

        rc, _out, err = _run_capture_stderr(
            [
                "push-metrics",
                "sample-for-labelling",
                "--cache-dir",
                str(cache_dir),
                "--exclude-session",
                "sess-typo-does-not-exist",
            ]
        )
        assert rc == 1
        assert "error:" in err
        assert "matches no known session id" in err


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
        # No push record was ever seeded for sess-1/page-1, so
        # determine_references returns None (no push records for that
        # session) -- UNDETERMINED, not a determined "not used". PR review
        # finding on athenaeum#2302: an earlier version collapsed that None
        # into a heuristic negative (TN here); the fix excludes it from the
        # confusion matrix entirely and reports it as undetermined instead.
        assert payload["confusion"] == {"tp": 0, "fp": 0, "fn": 0, "tn": 0, "total": 0}
        assert payload["undetermined_labels"] == 1
        assert payload["undetermined_sessions"] == 1

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

    def test_missing_labels_file_reports_a_named_error_not_a_traceback(
        self, tmp_path: Path
    ) -> None:
        """Regression for the PR review finding on athenaeum#2302: a
        nonexistent --labels path raised an uncaught OSError (FileNotFoundError)
        all the way out of cli_main. A bad path is the single most likely
        user error here (labels live outside this repo, at a caller-typed
        path), so this must be a one-line `error: ...` + exit 1, not a
        traceback."""
        cache_dir = tmp_path / "cache"
        rc, _out, err = _run_capture_stderr(
            [
                "push-metrics",
                "score-used",
                "--cache-dir",
                str(cache_dir),
                "--labels",
                str(tmp_path / "does-not-exist" / "labels.jsonl"),
            ]
        )
        assert rc == 1
        assert err.startswith("error:")
        assert "Traceback" not in err

    def test_unsupported_confidence_reports_a_named_error_not_a_traceback(
        self, tmp_path: Path
    ) -> None:
        """Regression: --confidence outside the supported Wilson z-score
        table raised an uncaught ValueError out of score_against_heuristic,
        past the --labels try/except (which only wrapped load_labels).

        A DETERMINED verdict is required for this to actually reach the
        Wilson calculation: an all-undetermined sample has n=0 for every
        rate (precision/recall/breakdown all short-circuit before calling
        wilson_score_interval at all -- see _rate_estimate), which would
        make this test pass for the wrong reason. So this seeds a real push
        record plus a locatable (but non-referencing) transcript, which
        determine_references resolves to a definite "not used" -- a TN,
        giving the unused-breakdown's rate calculation a nonzero n.
        """
        cache_dir = tmp_path / "cache"
        projects_root = tmp_path / "projects"
        _seed_one_push(cache_dir, "s", "p")
        scope_dir = projects_root / "-scope-hash"
        scope_dir.mkdir(parents=True)
        (scope_dir / "s.jsonl").write_text(
            json.dumps({"type": "user", "message": {"role": "user", "content": "unrelated"}})
            + "\n"
        )
        labels_path = tmp_path / "labels.jsonl"
        labels_path.write_text(
            json.dumps(
                {
                    "session_id": "s",
                    "push_id": "p",
                    "relevant": False,
                    "changed_work": False,
                }
            )
            + "\n"
        )
        rc, _out, err = _run_capture_stderr(
            [
                "push-metrics",
                "score-used",
                "--cache-dir",
                str(cache_dir),
                "--projects-root",
                str(projects_root),
                "--labels",
                str(labels_path),
                "--confidence",
                "0.5",
            ]
        )
        assert rc == 1
        assert err.startswith("error:")
        assert "unsupported confidence" in err
        assert "Traceback" not in err
