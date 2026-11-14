# SPDX-License-Identifier: Apache-2.0
"""Tests for the drop ledger (issue athenaeum#2609): the drops module
itself, its two config resolvers, and the entity-loop integration that
writes to it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.config import VALID_DROP_ACTIONS, resolve_drop_action, resolve_drop_log
from athenaeum.drops import (
    DROP_STAGE_CLASSIFY,
    DROP_STAGE_OTHER,
    count_for_run,
    drop_ledger_path,
    find_drop,
    iter_drops,
    latest_run_id,
    quarantine_dir,
    quarantine_raw_file,
    record_drop,
    restore_drop,
    safe_excerpt,
)
from athenaeum.models import RawFile

# ---------------------------------------------------------------------------
# safe_excerpt: PII/excluded-field routing reuse
# ---------------------------------------------------------------------------


class TestSafeExcerpt:
    def test_truncates_to_cap(self) -> None:
        text = "x" * 1000
        excerpt = safe_excerpt(text, max_chars=50)
        assert len(excerpt) == 50

    def test_strips_whitespace_before_capping(self) -> None:
        assert safe_excerpt("   hello world   ", max_chars=50) == "hello world"

    def test_masks_inline_email(self) -> None:
        """AC: no excluded value lands in the drop ledger — a contact
        address is masked via the SAME detector/masker pair
        athenaeum.pii already uses elsewhere (mask_contact_address)."""
        text = "Reach Alex at alex.chen@example.org for details."
        excerpt = safe_excerpt(text)
        assert "alex.chen@example.org" not in excerpt
        assert "a***@example.org" in excerpt

    def test_masks_inline_phone(self) -> None:
        text = "Call +1 (415) 555-0132 tomorrow."
        excerpt = safe_excerpt(text)
        assert "415) 555-0132" not in excerpt

    def test_service_address_left_alone(self) -> None:
        text = "clone via git@github.com:org/repo.git"
        excerpt = safe_excerpt(text)
        assert "git@github.com" in excerpt


# ---------------------------------------------------------------------------
# record_drop / iter_drops / find_drop / count_for_run / latest_run_id
# ---------------------------------------------------------------------------


class TestDropLedger:
    def test_record_and_read_back(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        rec = record_drop(
            cache_dir,
            raw_ref="sessions/20260101T000000Z-aaaaaaaa.md",
            content="nothing worth a page here",
            stage=DROP_STAGE_CLASSIFY,
            reason=None,
            run_id="run-1",
        )
        assert rec.raw_ref == "sessions/20260101T000000Z-aaaaaaaa.md"
        assert rec.stage == DROP_STAGE_CLASSIFY
        assert drop_ledger_path(cache_dir).exists()

        rows = iter_drops(cache_dir)
        assert len(rows) == 1
        assert rows[0].raw_ref == rec.raw_ref
        assert rows[0].excerpt == "nothing worth a page here"

    def test_iter_drops_empty_when_no_ledger(self, tmp_path: Path) -> None:
        assert iter_drops(tmp_path / "nope") == []

    def test_iter_drops_tolerates_torn_line(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        record_drop(
            cache_dir,
            raw_ref="sessions/a.md",
            content="one",
            stage=DROP_STAGE_OTHER,
            reason="T1 old format: x",
            run_id="run-1",
        )
        path = drop_ledger_path(cache_dir)
        with path.open("a", encoding="utf-8") as f:
            f.write('{"raw_ref": "broken,\n')  # torn/malformed trailing line
        rows = iter_drops(cache_dir)
        assert len(rows) == 1

    def test_find_drop_returns_most_recent(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        record_drop(
            cache_dir,
            raw_ref="sessions/a.md",
            content="first",
            stage=DROP_STAGE_CLASSIFY,
            reason=None,
            run_id="run-1",
        )
        record_drop(
            cache_dir,
            raw_ref="sessions/a.md",
            content="second",
            stage=DROP_STAGE_CLASSIFY,
            reason=None,
            run_id="run-2",
        )
        found = find_drop(cache_dir, "sessions/a.md")
        assert found is not None
        assert found.excerpt == "second"

    def test_find_drop_none_when_never_dropped(self, tmp_path: Path) -> None:
        assert find_drop(tmp_path / "cache", "sessions/never.md") is None

    def test_count_for_run_and_latest_run_id(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        record_drop(
            cache_dir, raw_ref="a.md", content="x", stage=DROP_STAGE_CLASSIFY,
            reason=None, run_id="run-1",
        )
        record_drop(
            cache_dir, raw_ref="b.md", content="y", stage=DROP_STAGE_CLASSIFY,
            reason=None, run_id="run-1",
        )
        record_drop(
            cache_dir, raw_ref="c.md", content="z", stage=DROP_STAGE_CLASSIFY,
            reason=None, run_id="run-2",
        )
        assert count_for_run(cache_dir, "run-1") == 2
        assert count_for_run(cache_dir, "run-2") == 1
        assert latest_run_id(cache_dir) == "run-2"

    def test_record_drop_rejects_unknown_stage(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            record_drop(
                tmp_path / "cache",
                raw_ref="a.md",
                content="x",
                stage="bogus-stage",
                reason=None,
                run_id="run-1",
            )

    def test_record_drop_excludes_pii_from_excerpt(self, tmp_path: Path) -> None:
        """AC: a test proving an excluded field does not appear in the
        drop ledger."""
        cache_dir = tmp_path / "cache"
        record_drop(
            cache_dir,
            raw_ref="sessions/a.md",
            content="Contact is priya.k@example.com for follow-up.",
            stage=DROP_STAGE_CLASSIFY,
            reason=None,
            run_id="run-1",
        )
        raw_ledger_text = drop_ledger_path(cache_dir).read_text(encoding="utf-8")
        assert "priya.k@example.com" not in raw_ledger_text
        assert "p***@example.com" in raw_ledger_text


# ---------------------------------------------------------------------------
# quarantine_raw_file / restore_drop
# ---------------------------------------------------------------------------


class TestQuarantineAndRestore:
    def _raw_file(self, tmp_path: Path, content: str) -> RawFile:
        raw_root = tmp_path / "raw"
        (raw_root / "sessions").mkdir(parents=True)
        path = raw_root / "sessions" / "20260101T000000Z-aaaaaaaa.md"
        path.write_text(content, encoding="utf-8")
        return RawFile(path=path, source="sessions", timestamp="20260101T000000Z", uuid8="aaaaaaaa")

    def test_quarantine_moves_file(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        raw = self._raw_file(tmp_path, "dropped content")
        original_path = raw.path
        dest = quarantine_raw_file(cache_dir, raw)
        assert not original_path.exists()
        assert dest.exists()
        assert dest == quarantine_dir(cache_dir) / raw.ref
        assert dest.read_text(encoding="utf-8") == "dropped content"

    def test_restore_raises_when_never_dropped(self, tmp_path: Path) -> None:
        with pytest.raises(LookupError):
            restore_drop(tmp_path / "cache", tmp_path / "raw", "sessions/none.md")

    def test_restore_from_quarantine_recovers_full_content(self, tmp_path: Path) -> None:
        """Restore re-queues the dropped file's content as raw intake —
        when quarantined, the FULL original content (not the capped
        excerpt) is what gets re-queued."""
        cache_dir = tmp_path / "cache"
        raw_root = tmp_path / "raw"
        full_content = "A" * 900  # exceeds DROP_EXCERPT_MAX_CHARS (500)
        raw = self._raw_file(tmp_path, full_content)
        record_drop(
            cache_dir,
            raw_ref=raw.ref,
            content=full_content,
            stage=DROP_STAGE_CLASSIFY,
            reason=None,
            run_id="run-1",
        )
        quarantine_raw_file(cache_dir, raw)

        dest = restore_drop(cache_dir, raw_root, raw.ref)
        assert dest.parent == raw_root / "sessions"
        assert dest.name != raw.path.name  # a fresh re-queue, not an undelete
        assert dest.read_text(encoding="utf-8") == full_content
        # The quarantined copy is consumed on restore.
        assert not (quarantine_dir(cache_dir) / raw.ref).exists()

    def test_restore_from_ledger_excerpt_when_deleted(self, tmp_path: Path) -> None:
        """Default (delete) disposition: only the ledger's own excerpt
        survives, and restore re-queues exactly that."""
        cache_dir = tmp_path / "cache"
        raw_root = tmp_path / "raw"
        record_drop(
            cache_dir,
            raw_ref="sessions/20260101T000000Z-bbbbbbbb.md",
            content="short dropped note",
            stage=DROP_STAGE_CLASSIFY,
            reason=None,
            run_id="run-1",
        )
        dest = restore_drop(cache_dir, raw_root, "sessions/20260101T000000Z-bbbbbbbb.md")
        assert dest.read_text(encoding="utf-8") == "short dropped note"


# ---------------------------------------------------------------------------
# resolve_drop_log / resolve_drop_action
# ---------------------------------------------------------------------------


class TestResolveDropLog:
    def test_default_true(self) -> None:
        assert resolve_drop_log(None) is True

    def test_yaml_false(self) -> None:
        assert resolve_drop_log({"librarian": {"drop_log": False}}) is False

    def test_env_overrides_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_DROP_LOG", "0")
        assert resolve_drop_log({"librarian": {"drop_log": True}}) is False

    def test_env_truthy_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_DROP_LOG", "yes-please")
        assert resolve_drop_log(None) is True


class TestResolveDropAction:
    def test_default_delete(self) -> None:
        assert resolve_drop_action(None) == "delete"

    def test_yaml_quarantine(self) -> None:
        assert resolve_drop_action({"librarian": {"drop_action": "quarantine"}}) == "quarantine"

    def test_invalid_value_raises(self) -> None:
        with pytest.raises(ValueError):
            resolve_drop_action({"librarian": {"drop_action": "nope"}})

    def test_valid_actions_tuple(self) -> None:
        assert set(VALID_DROP_ACTIONS) == {"delete", "quarantine"}


# ---------------------------------------------------------------------------
# Entity-loop integration (mirrors tests/test_librarian.py::TestRunIntegration)
# ---------------------------------------------------------------------------


class TestEntityLoopIntegration:
    def _seed_knowledge_root(self, tmp_path: Path, raw_text: str) -> Path:
        root = tmp_path / "knowledge"
        root.mkdir()
        wiki = root / "wiki"
        (wiki / "_schema").mkdir(parents=True)
        (wiki / "_schema" / "types.md").write_text("# Types\n\n| Type |\n|------|\n| person |\n")
        (wiki / "_schema" / "tags.md").write_text("# Tags\n\n| Tag |\n|-----|\n| active |\n")
        (wiki / "_schema" / "access-levels.md").write_text(
            "# Access\n\n| Level |\n|-------|\n| internal |\n"
        )
        sessions = root / "raw" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / ".gitkeep").write_text("")

        subprocess.run(["git", "init", "-q", "-b", "test-branch"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)

        (sessions / "20260410T120000Z-aabbccdd.md").write_text(raw_text)
        return root

    def _run_with_classify_response(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        raw_text: str,
        classify_entities: list,
        cache_dir: Path,
        extra_env: dict | None = None,
    ) -> Path:
        import anthropic as anthropic_mod

        from athenaeum.librarian import run

        root = self._seed_knowledge_root(tmp_path, raw_text)

        classify_response = MagicMock()
        classify_response.content = [MagicMock(text=json.dumps(classify_entities))]
        create_response = MagicMock()
        create_response.content = [MagicMock(text="# Someone\n\nA fact.")]

        mock_client = MagicMock()
        if classify_entities:
            mock_client.messages.create.side_effect = [classify_response, create_response]
        else:
            mock_client.messages.create.return_value = classify_response
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kwargs: mock_client)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(cache_dir))
        for key, value in (extra_env or {}).items():
            monkeypatch.setenv(key, value)

        run(raw_root=root / "raw", wiki_root=root / "wiki", knowledge_root=root)
        return root

    def test_classify_empty_writes_drop_row_and_deletes_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache_dir = tmp_path / "cache"
        root = self._run_with_classify_response(
            tmp_path,
            monkeypatch,
            "An invented personal preference nobody asked to keep.\n",
            classify_entities=[],
            cache_dir=cache_dir,
        )
        raw_path = root / "raw" / "sessions" / "20260410T120000Z-aabbccdd.md"
        assert not raw_path.exists(), "default disposition is delete"

        rows = iter_drops(cache_dir)
        assert len(rows) == 1
        assert rows[0].stage == DROP_STAGE_CLASSIFY
        assert rows[0].raw_ref == "sessions/20260410T120000Z-aabbccdd.md"

    def test_create_path_writes_no_drop_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache_dir = tmp_path / "cache"
        self._run_with_classify_response(
            tmp_path,
            monkeypatch,
            "Met with Priya Nair about the roadmap.\n",
            classify_entities=[
                {
                    "name": "Priya Nair",
                    "entity_type": "person",
                    "tags": ["active"],
                    "access": "internal",
                    "observations": "Discussed the roadmap.",
                }
            ],
            cache_dir=cache_dir,
        )
        assert iter_drops(cache_dir) == []

    def test_drop_log_disabled_writes_no_rows_and_summary_says_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
    ) -> None:
        import logging

        cache_dir = tmp_path / "cache"
        caplog.set_level(logging.INFO, logger="athenaeum")
        self._run_with_classify_response(
            tmp_path,
            monkeypatch,
            "Another invented preference.\n",
            classify_entities=[],
            cache_dir=cache_dir,
            extra_env={"ATHENAEUM_DROP_LOG": "0"},
        )
        assert iter_drops(cache_dir) == []
        assert any(
            "drops=disabled" in rec.message
            for rec in caplog.records
            if "librarian-run-summary" in rec.message
        ), "expected the entity-phase run-summary segment to render drops=disabled"

    def test_quarantine_action_keeps_file_on_disk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import anthropic as anthropic_mod

        from athenaeum.config import resolve_drop_action
        from athenaeum.librarian import run

        cache_dir = tmp_path / "cache"
        root = self._seed_knowledge_root(tmp_path, "Nothing worth keeping here.\n")
        (root / "athenaeum.yaml").write_text("librarian:\n  drop_action: quarantine\n")

        classify_response = MagicMock()
        classify_response.content = [MagicMock(text=json.dumps([]))]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = classify_response
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kwargs: mock_client)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(cache_dir))

        from athenaeum.config import load_config

        cfg = load_config(root)
        assert resolve_drop_action(cfg) == "quarantine"

        run(raw_root=root / "raw", wiki_root=root / "wiki", knowledge_root=root)

        raw_path = root / "raw" / "sessions" / "20260410T120000Z-aabbccdd.md"
        assert not raw_path.exists()
        quarantined = quarantine_dir(cache_dir) / "sessions" / "20260410T120000Z-aabbccdd.md"
        assert quarantined.exists()
        assert quarantined.read_text(encoding="utf-8") == "Nothing worth keeping here.\n"
