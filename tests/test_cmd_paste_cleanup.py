# SPDX-License-Identifier: Apache-2.0
"""``athenaeum paste-cleanup --from-report`` CLI surface (issue athenaeum#1903
step 3).

Covers: replaying a prior ``--json`` report writes only the listed uids'
verified ``remove``/``rewrite`` rows and constructs NO LLM client;
mutual-exclusivity with the proposer/verifier knobs; version-mismatch
rejection; ``--from-report`` without ``--apply`` prints a summary and writes
nothing. All fixtures under ``tmp_path`` -- no test reads or writes a live
knowledge store.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from athenaeum.cli import build_parser
from athenaeum.paste_cleanup import PasteCleanupReport, ProposalVerdict, build_paste_cleanup_report
from tests.conftest import FakeLLMClient


def _paste_cleanup_parser() -> argparse.ArgumentParser:
    parser = build_parser()
    subparsers_action = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    return subparsers_action.choices["paste-cleanup"]


def _page(root: Path, name: str, frontmatter: str, body: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")
    return path


@pytest.fixture
def knowledge_root(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge"
    (root / "wiki").mkdir(parents=True)
    return root


def _build_report_file(knowledge_root: Path, tmp_path: Path) -> Path:
    """A real dry-run report (via a fake LLM client), written to a JSON
    file the way ``--json`` would -- the fixture ``--from-report`` replays."""
    content = "Off-topic internal retro content unrelated to the subject." * 10
    _page(
        knowledge_root / "wiki",
        "person1.md",
        "uid: person1\nname: Person One\n",
        f"## Notes\n\n- 2026-01-01: {content}\n",
    )
    client = FakeLLMClient(
        text=json.dumps(
            {"verdict": "remove", "claim": "", "reason": "off-topic", "confidence": "high"}
        )
    )
    report = build_paste_cleanup_report(
        knowledge_root / "wiki",
        client=client,
        verify_client=client,
        model="claude-haiku-4-5-20251001",
        verify_model="claude-sonnet-5",
        verify_rule="all",
        length_threshold=10,
    )
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report.to_dict()), encoding="utf-8")
    return report_path


def _build_pending_report_file(knowledge_root: Path, tmp_path: Path) -> Path:
    """A v2 report, shaped like real ``--json`` output, with ONE proposed
    verdict left ``verify_attempted=False`` -- as if a spend ceiling had
    stopped the verifier pass before reaching it (issue athenaeum#1923). This
    is the actual fixture shape ``--resume-verification`` exists to act on,
    unlike :func:`_build_report_file`'s fully-verified report above.
    """
    content = "Off-topic internal retro content unrelated to the subject." * 10
    page = _page(
        knowledge_root / "wiki",
        "person1.md",
        "uid: person1\nname: Person One\n",
        f"## Notes\n\n- 2026-01-01: {content}\n",
    )
    report = PasteCleanupReport(
        proposed=[
            ProposalVerdict(
                uid="person1",
                path=page,
                date="2026-01-01",
                raw_chunk=f"- 2026-01-01: {content}",
                paste_text=content,
                extraction_status="clean",
                verdict="remove",
                reason="off-topic",
                confidence="high",
                model="claude-haiku-4-5-20251001",
            )
        ],
        verify_rule="all",
        model="claude-haiku-4-5-20251001",
        verify_model="claude-sonnet-5",
    )
    report_path = tmp_path / "pending_report.json"
    report_path.write_text(json.dumps(report.to_dict()), encoding="utf-8")
    return report_path


class TestFromReportReplay:
    def test_apply_writes_verified_rows_and_builds_no_llm_client(
        self,
        knowledge_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        report_path = _build_report_file(knowledge_root, tmp_path)

        def _fail(*args: object, **kwargs: object) -> None:
            raise AssertionError("build_llm_client must not be called during --from-report replay")

        monkeypatch.setattr("athenaeum.provider.build_llm_client", _fail)

        uids_path = tmp_path / "uids.txt"
        uids_path.write_text("person1\n", encoding="utf-8")

        parser = _paste_cleanup_parser()
        args = parser.parse_args(
            [
                "--path",
                str(knowledge_root),
                "--from-report",
                str(report_path),
                "--uids",
                str(uids_path),
                "--apply",
                "--json",
            ]
        )
        rc = args.func(args)
        assert rc == 0

        after = (knowledge_root / "wiki" / "person1.md").read_text(encoding="utf-8")
        assert "Off-topic internal retro content" not in after

    def test_without_apply_prints_replay_summary_and_writes_nothing(
        self, knowledge_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        report_path = _build_report_file(knowledge_root, tmp_path)
        before = (knowledge_root / "wiki" / "person1.md").read_text(encoding="utf-8")

        parser = _paste_cleanup_parser()
        args = parser.parse_args(["--path", str(knowledge_root), "--from-report", str(report_path)])
        rc = args.func(args)
        assert rc == 0

        after = (knowledge_root / "wiki" / "person1.md").read_text(encoding="utf-8")
        assert before == after
        out, _ = capsys.readouterr()
        assert "REPLAY" in out

    def test_mutually_exclusive_with_model_override(
        self, knowledge_root: Path, tmp_path: Path
    ) -> None:
        report_path = _build_report_file(knowledge_root, tmp_path)
        parser = _paste_cleanup_parser()
        args = parser.parse_args(
            [
                "--path",
                str(knowledge_root),
                "--from-report",
                str(report_path),
                "--model",
                "some-other-model",
            ]
        )
        rc = args.func(args)
        assert rc == 1

    def test_version_mismatch_is_rejected(self, knowledge_root: Path, tmp_path: Path) -> None:
        report_path = tmp_path / "stale_report.json"
        report_path.write_text(
            json.dumps({"version": "paste-cleanup-v0-does-not-exist", "proposed": []}),
            encoding="utf-8",
        )
        parser = _paste_cleanup_parser()
        args = parser.parse_args(["--path", str(knowledge_root), "--from-report", str(report_path)])
        rc = args.func(args)
        assert rc == 1


class TestResumeVerificationCli:
    """``--resume-verification`` surface (issue athenaeum#1923)."""

    def test_without_from_report_exits_1_naming_both_flags(
        self, knowledge_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        parser = _paste_cleanup_parser()
        args = parser.parse_args(["--path", str(knowledge_root), "--resume-verification"])
        rc = args.func(args)
        assert rc == 1
        _out, err = capsys.readouterr()
        assert "--resume-verification" in err
        assert "--from-report" in err

    def test_verify_model_alone_still_conflicts_with_from_report(
        self, knowledge_root: Path, tmp_path: Path
    ) -> None:
        report_path = _build_report_file(knowledge_root, tmp_path)
        parser = _paste_cleanup_parser()
        args = parser.parse_args(
            [
                "--path",
                str(knowledge_root),
                "--from-report",
                str(report_path),
                "--verify-model",
                "some-other-model",
            ]
        )
        rc = args.func(args)
        assert rc == 1

    def test_resume_verification_allows_verify_model_override(
        self, knowledge_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report_path = _build_pending_report_file(knowledge_root, tmp_path)
        fake_verify_client = FakeLLMClient(
            text=json.dumps(
                {"verdict": "remove", "claim": "", "reason": "confirmed", "agree": True}
            )
        )

        def _fake_build_llm_client(config: object, *, knob: str | None = None, **kwargs: object):
            assert knob == "verify"
            return fake_verify_client

        monkeypatch.setattr("athenaeum.provider.build_llm_client", _fake_build_llm_client)

        parser = _paste_cleanup_parser()
        args = parser.parse_args(
            [
                "--path",
                str(knowledge_root),
                "--from-report",
                str(report_path),
                "--resume-verification",
                "--verify-model",
                "some-other-model",
                "--json",
            ]
        )
        rc = args.func(args)
        assert rc == 0
        assert len(fake_verify_client.calls) == 1
        assert fake_verify_client.calls[0]["model"] == "some-other-model"

    def test_end_to_end_resume_emits_resume_flag_and_verifies(
        self,
        knowledge_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        report_path = _build_pending_report_file(knowledge_root, tmp_path)
        fake_verify_client = FakeLLMClient(
            text=json.dumps(
                {"verdict": "remove", "claim": "", "reason": "confirmed", "agree": True}
            )
        )

        def _fake_build_llm_client(config: object, *, knob: str | None = None, **kwargs: object):
            return fake_verify_client if knob == "verify" else None

        monkeypatch.setattr("athenaeum.provider.build_llm_client", _fake_build_llm_client)

        parser = _paste_cleanup_parser()
        args = parser.parse_args(
            [
                "--path",
                str(knowledge_root),
                "--from-report",
                str(report_path),
                "--resume-verification",
                "--json",
            ]
        )
        rc = args.func(args)
        assert rc == 0
        out, _ = capsys.readouterr()
        payload = json.loads(out)
        assert payload["resume_verification"] is True
        rows = payload["proposed"]
        assert len(rows) == 1
        assert rows[0]["verify_attempted"] is True
        assert rows[0]["final_verdict"] == "remove"

    def test_exits_1_when_verify_knob_has_no_client(
        self,
        knowledge_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        report_path = _build_pending_report_file(knowledge_root, tmp_path)
        monkeypatch.setattr(
            "athenaeum.provider.build_llm_client",
            lambda config, *, knob=None, **kwargs: None,
        )

        parser = _paste_cleanup_parser()
        args = parser.parse_args(
            [
                "--path",
                str(knowledge_root),
                "--from-report",
                str(report_path),
                "--resume-verification",
            ]
        )
        rc = args.func(args)
        assert rc == 1
        _out, err = capsys.readouterr()
        assert "verify" in err
