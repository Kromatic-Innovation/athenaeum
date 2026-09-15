# SPDX-License-Identifier: Apache-2.0
"""``athenaeum audit`` CLI surface, issue athenaeum#1667 additions.

Covers: ``config.env`` is loaded from ``<cache_dir>/config.env`` before the
LLM client is built (process env always wins, a missing file is a silent
no-op, the key value is never printed); ``--batch --json`` smoke over a
fixture wiki holding a uid page AND a uid-less auto-memory page, via a
mocked batch transport, asserting no synchronous call is made and every
verdict maps back to the right page through the custom_id map. All
fixtures under ``tmp_path`` — no test reads or writes a live knowledge
store.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from athenaeum import _cmd_audit
from athenaeum.cli import build_parser
from tests.conftest import make_llm_response, make_llm_usage


def _audit_parser() -> argparse.ArgumentParser:
    parser = build_parser()
    subparsers_action = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    return subparsers_action.choices["audit"]


def _page(root: Path, name: str, frontmatter: str, body: str = "Body.\n") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")
    return path


@pytest.fixture
def knowledge_root(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge"
    (root / "wiki").mkdir(parents=True)
    return root


# --- config.env loading -----------------------------------------------


class TestConfigEnvLoading:
    def test_missing_process_env_key_is_loaded_from_config_env(
        self,
        knowledge_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        (cache_dir / "config.env").write_text(
            "ANTHROPIC_API_KEY=sk-from-file-not-a-real-secret\n", encoding="utf-8"
        )
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(cache_dir))
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        # Empty wiki: build_audit_report scans zero candidates, so no real
        # network call happens even though a real anthropic.Anthropic client
        # is constructed.
        parser = _audit_parser()
        args = parser.parse_args(["--path", str(knowledge_root), "--json"])
        rc = args.func(args)
        assert rc == 0

        out, err = capsys.readouterr()
        assert "sk-from-file-not-a-real-secret" not in out
        assert "sk-from-file-not-a-real-secret" not in err

    def test_missing_config_env_file_is_a_silent_no_op(
        self, knowledge_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(tmp_path / "no-such-cache-dir"))
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        parser = _audit_parser()
        args = parser.parse_args(["--path", str(knowledge_root), "--json"])
        rc = args.func(args)
        # No key anywhere -> cmd_audit reports the documented "no LLM
        # client configured" error, rather than raising.
        assert rc == 1

    def test_process_env_wins_over_config_env_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        (cache_dir / "config.env").write_text(
            "ANTHROPIC_API_KEY=sk-should-be-ignored\n", encoding="utf-8"
        )
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(cache_dir))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-real-process-value")

        _cmd_audit._load_cache_config_env()

        import os

        assert os.environ["ANTHROPIC_API_KEY"] == "sk-real-process-value"

    def test_comments_and_blank_lines_are_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        (cache_dir / "config.env").write_text(
            "# a comment\n\nANTHENAEUM_TEST_KEY=value1\nMALFORMED_LINE_NO_EQUALS\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(cache_dir))
        monkeypatch.delenv("ANTHENAEUM_TEST_KEY", raising=False)

        _cmd_audit._load_cache_config_env()

        import os

        assert os.environ["ANTHENAEUM_TEST_KEY"] == "value1"


# --- --batch smoke: mocked transport, mixed uid/uid-less pages ------------


class TestBatchSmokeMixedIdentity:
    def test_batch_json_uses_no_synchronous_call_and_maps_every_verdict(
        self,
        knowledge_root: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        wiki_root = knowledge_root / "wiki"
        _page(wiki_root, "uidpage.md", "uid: uidpage1\ntype: concept\nname: Uid Page\n")
        _page(
            wiki_root,
            "auto-longidentity-path-that-exceeds-sixty-four-characters-total.md",
            "type: auto-memory\nname: Cluster X\ncluster_id: c1\n",
        )

        submitted: list[list[dict[str, Any]]] = []
        sync_calls: list[dict[str, Any]] = []

        class _FakeBatches:
            def create(self, *, requests: list[dict[str, Any]]) -> SimpleNamespace:
                submitted.append(list(requests))
                return SimpleNamespace(id="msgbatch_1", processing_status="ended")

            def retrieve(self, batch_id: str) -> SimpleNamespace:
                return SimpleNamespace(id=batch_id, processing_status="ended")

            def results(self, batch_id: str):
                for req in submitted[0]:
                    yield SimpleNamespace(
                        custom_id=req["custom_id"],
                        result=SimpleNamespace(
                            type="succeeded",
                            message=make_llm_response(
                                json.dumps(
                                    {"retirement_candidate": False, "retirement_reason": ""}
                                ),
                                usage=make_llm_usage(10, 5),
                            ),
                        ),
                    )

        class _FakeBatchClient:
            def __init__(self) -> None:
                self.batches = _FakeBatches()

                def create(**params: Any) -> Any:
                    sync_calls.append(params)
                    raise AssertionError("unexpected synchronous call in --batch mode")

                self.messages = SimpleNamespace(create=create, batches=self.batches)

        monkeypatch.setattr(
            "athenaeum.provider.build_llm_client", lambda *a, **k: _FakeBatchClient()
        )

        parser = _audit_parser()
        args = parser.parse_args(["--path", str(knowledge_root), "--batch", "--json"])
        rc = args.func(args)
        assert rc == 0

        assert submitted, "requests must be routed to batches.create"
        assert not sync_calls

        payload = json.loads(capsys.readouterr().out)
        assert payload["audited"] == 2
        for req in submitted[0]:
            import re

            assert re.match(r"^[a-zA-Z0-9_-]{1,64}$", req["custom_id"])

        uids = {v["uid"] for v in payload["verdicts"]}
        assert "uidpage1" in uids
        assert "auto-longidentity-path-that-exceeds-sixty-four-characters-total.md" in uids
