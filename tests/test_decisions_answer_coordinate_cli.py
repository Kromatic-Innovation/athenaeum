# SPDX-License-Identifier: Apache-2.0
"""CLI-level tests for `athenaeum decisions answer`/`pending-answers` (issue
athenaeum#2544).

`decisions answer --type coordinate` used to write the answer file after
checking only its outer shape (issue athenaeum#717's schema check) — every
check that could actually make it fail (pair membership, page resolution,
dimension registration, ...) ran hours later, at `ingest-answers` apply
time, refusing the WHOLE batch. These tests exercise the CLI boundary
directly: exit codes, stdout/stderr shape, and "nothing is written" on
refusal.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from athenaeum.cli import main as cli_main


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli_main(argv)
    return rc, out.getvalue(), err.getvalue()


def _seed_coordinate_item(wiki_root: Path) -> str:
    """Queue one real `coordinate` item spanning `page-a`/`page-b`;
    returns its decision id."""
    from athenaeum.comparator import CompareOutcome, page_from_text
    from athenaeum.decisions import list_pending_decisions
    from athenaeum.verdict_effects import build_coordinate_request, queue_coordinate_batch

    wiki_root.mkdir(parents=True, exist_ok=True)
    (wiki_root / "page-a.md").write_text("---\nname: Page A\n---\nbody\n", encoding="utf-8")
    (wiki_root / "page-b.md").write_text("---\nname: Page B\n---\nbody\n", encoding="utf-8")
    page_a = page_from_text("page-a", "---\nname: Page A\n---\nbody\n")
    page_b = page_from_text("page-b", "---\nname: Page B\n---\nbody\n")
    outcome = CompareOutcome(verdict="underdetermined", missing=["subject"])
    member = {
        "pair_key": "page-a+page-b",
        "request": build_coordinate_request(page_a, page_b, outcome),
        "conflict_type": "ambiguous",
    }
    queue_coordinate_batch([member], wiki_root=wiki_root, config=None)
    return list_pending_decisions(wiki_root)[0]["id"]


@pytest.fixture
def knowledge_root(tmp_path: Path) -> Path:
    (tmp_path / "wiki").mkdir()
    (tmp_path / "raw").mkdir()
    return tmp_path


class TestWriteTimeCoordinateRefusal:
    """AC1/AC2: the deep applier checks now run at write time, not just
    the outer schema."""

    def test_pair_not_in_batch_refuses_and_writes_nothing(
        self, knowledge_root: Path
    ) -> None:
        wiki_root = knowledge_root / "wiki"
        decision_id = _seed_coordinate_item(wiki_root)
        bad_payload = json.dumps(
            {
                "answers": [
                    {
                        "pair": "not-in-the-batch+also-not",
                        "dimensions": {"subject": {"not-in-the-batch": "x"}},
                    }
                ]
            }
        )

        rc, out, err = _run(
            [
                "decisions",
                "answer",
                "--path",
                str(knowledge_root),
                "--id",
                decision_id,
                "--type",
                "coordinate",
                "--answer",
                json.dumps({"verdict": bad_payload}),
                "--json",
            ]
        )

        assert rc != 0
        payload = json.loads(err)
        assert payload["ok"] is False
        assert payload["error_code"] == "invalid_coordinate_answer"
        # The refusal names the failing pair.
        assert any("not-in-the-batch+also-not" in e for e in payload["errors"])
        assert not (knowledge_root / "raw" / "answers").exists()

    def test_verdict_that_passes_todays_schema_check_is_still_refused(
        self, knowledge_root: Path
    ) -> None:
        """Counter-example (AC2): `verdict: "not json"` satisfies the
        outer non-empty-string schema but is not valid JSON — must still
        be refused, not written."""
        wiki_root = knowledge_root / "wiki"
        decision_id = _seed_coordinate_item(wiki_root)

        rc, out, err = _run(
            [
                "decisions",
                "answer",
                "--path",
                str(knowledge_root),
                "--id",
                decision_id,
                "--type",
                "coordinate",
                "--answer",
                json.dumps({"verdict": "not json"}),
                "--json",
            ]
        )

        assert rc != 0
        payload = json.loads(err)
        assert payload["ok"] is False
        assert payload["error_code"] == "malformed_verdict_json"
        assert not (knowledge_root / "raw" / "answers").exists()

    def test_valid_coordinate_answer_still_succeeds(self, knowledge_root: Path) -> None:
        wiki_root = knowledge_root / "wiki"
        decision_id = _seed_coordinate_item(wiki_root)
        good_payload = json.dumps(
            {"answers": [{"pair": "page-a+page-b", "dimensions": {"subject": {"page-a": "x"}}}]}
        )

        rc, out, err = _run(
            [
                "decisions",
                "answer",
                "--path",
                str(knowledge_root),
                "--id",
                decision_id,
                "--type",
                "coordinate",
                "--answer",
                json.dumps({"verdict": good_payload}),
                "--json",
            ]
        )

        assert rc == 0
        payload = json.loads(out)
        assert payload["ok"] is True
        assert len(list((knowledge_root / "raw" / "answers").glob("*.md"))) == 1


class TestJsonOutputIsSingleLine:
    """AC5: `decisions answer --json` emits one line, like `propose-fold --json`."""

    def test_success_output_is_a_single_line(self, knowledge_root: Path) -> None:
        wiki_root = knowledge_root / "wiki"
        decision_id = _seed_coordinate_item(wiki_root)
        good_payload = json.dumps(
            {"answers": [{"pair": "page-a+page-b", "dimensions": {"subject": {"page-a": "x"}}}]}
        )

        rc, out, _err = _run(
            [
                "decisions",
                "answer",
                "--path",
                str(knowledge_root),
                "--id",
                decision_id,
                "--type",
                "coordinate",
                "--answer",
                json.dumps({"verdict": good_payload}),
                "--json",
            ]
        )
        assert rc == 0
        lines = [line for line in out.splitlines() if line.strip()]
        assert len(lines) == 1
        json.loads(lines[0])  # parses as exactly one JSON object

    def test_refusal_output_is_a_single_line(self, knowledge_root: Path) -> None:
        wiki_root = knowledge_root / "wiki"
        decision_id = _seed_coordinate_item(wiki_root)

        rc, _out, err = _run(
            [
                "decisions",
                "answer",
                "--path",
                str(knowledge_root),
                "--id",
                decision_id,
                "--type",
                "coordinate",
                "--answer",
                json.dumps({"verdict": "not json"}),
                "--json",
            ]
        )
        assert rc != 0
        lines = [line for line in err.splitlines() if line.strip()]
        assert len(lines) == 1
        json.loads(lines[0])


class TestPendingConflictWarnOrRefuse:
    """AC3: an unapplied answer file already on disk for the same id is
    reported before writing a new one -- warn by default, refuse with the
    flag."""

    def test_second_answer_for_same_id_warns_by_default(self, knowledge_root: Path) -> None:
        wiki_root = knowledge_root / "wiki"
        decision_id = _seed_coordinate_item(wiki_root)
        payload = json.dumps(
            {"answers": [{"pair": "page-a+page-b", "dimensions": {"subject": {"page-a": "x"}}}]}
        )

        from athenaeum.decision_answers import render_decision_answer

        answers_dir = knowledge_root / "raw" / "answers"
        answers_dir.mkdir(parents=True, exist_ok=True)
        (answers_dir / "20261101T000000Z-coordinate-pre-existing.md").write_text(
            render_decision_answer(
                decision_id=decision_id,
                decision_type="coordinate",
                verdict=payload,
                resolved_at="2026-11-01T00:00:00+00:00",
            ),
            encoding="utf-8",
        )

        rc, out, err = _run(
            [
                "decisions",
                "answer",
                "--path",
                str(knowledge_root),
                "--id",
                decision_id,
                "--type",
                "coordinate",
                "--answer",
                json.dumps({"verdict": payload}),
                "--json",
            ]
        )

        assert rc == 0
        assert "warning" in err.lower()
        assert "unapplied" in err.lower() or "pending" in err.lower()
        # The new answer WAS written, alongside the pre-existing one.
        assert len(list(answers_dir.glob("*.md"))) == 2

    def test_second_answer_for_same_id_refuses_with_flag(self, knowledge_root: Path) -> None:
        wiki_root = knowledge_root / "wiki"
        decision_id = _seed_coordinate_item(wiki_root)
        payload = json.dumps(
            {"answers": [{"pair": "page-a+page-b", "dimensions": {"subject": {"page-a": "x"}}}]}
        )

        from athenaeum.decision_answers import render_decision_answer

        answers_dir = knowledge_root / "raw" / "answers"
        answers_dir.mkdir(parents=True, exist_ok=True)
        (answers_dir / "20261101T000000Z-coordinate-pre-existing.md").write_text(
            render_decision_answer(
                decision_id=decision_id,
                decision_type="coordinate",
                verdict=payload,
                resolved_at="2026-11-01T00:00:00+00:00",
            ),
            encoding="utf-8",
        )

        rc, out, err = _run(
            [
                "decisions",
                "answer",
                "--path",
                str(knowledge_root),
                "--id",
                decision_id,
                "--type",
                "coordinate",
                "--answer",
                json.dumps({"verdict": payload}),
                "--refuse-on-pending-conflict",
                "--json",
            ]
        )

        assert rc != 0
        payload_out = json.loads(err)
        assert payload_out["ok"] is False
        assert payload_out["error_code"] == "pending_conflict"
        # Nothing new was written -- only the pre-existing file remains.
        assert len(list(answers_dir.glob("*.md"))) == 1


class TestPendingConflictScanIsBestEffort:
    """A stray unreadable file elsewhere in raw/answers/ must not take
    down an otherwise-valid `decisions answer` call -- the pending-
    conflict scan is a WARNING, not the write itself."""

    def test_invalid_utf8_file_does_not_block_a_valid_answer(
        self, knowledge_root: Path
    ) -> None:
        wiki_root = knowledge_root / "wiki"
        decision_id = _seed_coordinate_item(wiki_root)
        good_payload = json.dumps(
            {"answers": [{"pair": "page-a+page-b", "dimensions": {"subject": {"page-a": "x"}}}]}
        )

        answers_dir = knowledge_root / "raw" / "answers"
        answers_dir.mkdir(parents=True, exist_ok=True)
        # A stray file with invalid UTF-8 bytes -- unrelated to the id
        # being answered below, but still in the directory the
        # pending-conflict scan walks.
        (answers_dir / "20261101T000000Z-corrupt.md").write_bytes(
            b"\xff\xfe not valid utf-8 \x80\x81"
        )

        rc, out, err = _run(
            [
                "decisions",
                "answer",
                "--path",
                str(knowledge_root),
                "--id",
                decision_id,
                "--type",
                "coordinate",
                "--answer",
                json.dumps({"verdict": good_payload}),
                "--json",
            ]
        )

        assert rc == 0, err
        payload = json.loads(out)
        assert payload["ok"] is True
        # The warning was surfaced, not silently eaten.
        assert "pending-conflict scan skipped" in err
        # The real answer was still written, alongside the corrupt file.
        assert len(list(answers_dir.glob("*.md"))) == 2


class TestPendingAnswersCli:
    """AC4: a read-only CLI lists unapplied answer files for a decision id
    or a page, with timestamp and verdict."""

    def test_lists_by_decision_id(self, knowledge_root: Path) -> None:
        wiki_root = knowledge_root / "wiki"
        decision_id = _seed_coordinate_item(wiki_root)
        payload = json.dumps(
            {"answers": [{"pair": "page-a+page-b", "dimensions": {"subject": {"page-a": "x"}}}]}
        )
        rc, _out, _err = _run(
            [
                "decisions",
                "answer",
                "--path",
                str(knowledge_root),
                "--id",
                decision_id,
                "--type",
                "coordinate",
                "--answer",
                json.dumps({"verdict": payload}),
            ]
        )
        assert rc == 0

        rc, out, _err = _run(
            [
                "decisions",
                "pending-answers",
                "--path",
                str(knowledge_root),
                "--id",
                decision_id,
                "--json",
            ]
        )
        assert rc == 0
        result = json.loads(out)
        assert result["ok"] is True
        assert len(result["answers"]) == 1
        entry = result["answers"][0]
        assert entry["decision_id"] == decision_id
        assert entry["verdict"] == payload
        assert entry["resolved_at"]

    def test_lists_by_page(self, knowledge_root: Path) -> None:
        wiki_root = knowledge_root / "wiki"
        decision_id = _seed_coordinate_item(wiki_root)
        payload = json.dumps(
            {"answers": [{"pair": "page-a+page-b", "dimensions": {"subject": {"page-a": "x"}}}]}
        )
        _run(
            [
                "decisions",
                "answer",
                "--path",
                str(knowledge_root),
                "--id",
                decision_id,
                "--type",
                "coordinate",
                "--answer",
                json.dumps({"verdict": payload}),
            ]
        )

        rc, out, _err = _run(
            [
                "decisions",
                "pending-answers",
                "--path",
                str(knowledge_root),
                "--page",
                "page-b",
                "--json",
            ]
        )
        assert rc == 0
        result = json.loads(out)
        assert len(result["answers"]) == 1
        assert result["answers"][0]["decision_id"] == decision_id

    def test_empty_result_text_mode(self, knowledge_root: Path) -> None:
        rc, out, _err = _run(
            [
                "decisions",
                "pending-answers",
                "--path",
                str(knowledge_root),
                "--id",
                "no-such-id",
            ]
        )
        assert rc == 0
        assert "no unapplied" in out.lower()

    def test_id_and_page_are_mutually_exclusive(self, knowledge_root: Path) -> None:
        with pytest.raises(SystemExit):
            _run(
                [
                    "decisions",
                    "pending-answers",
                    "--path",
                    str(knowledge_root),
                    "--id",
                    "x",
                    "--page",
                    "y",
                ]
            )
