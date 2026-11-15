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
from athenaeum.librarian import process_one
from athenaeum.models import EntityIndex, RawFile

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

    def test_quarantine_survives_cross_device_rename(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Seer review on PR athenaeum#2639, Finding 2: the quarantine dir
        (cache dir) and the raw file (knowledge root) are independently
        configurable and can land on different filesystems, where a plain
        rename raises ``OSError(EXDEV)``. Force exactly that error out of
        ``os.rename`` (what ``shutil.move`` tries first) and assert the
        file still ends up quarantined via the copy+unlink fallback —
        proving the fix is the real ``shutil.move`` semantics, not a
        same-filesystem-only illusion of one."""
        import errno
        import os

        cache_dir = tmp_path / "cache"
        raw = self._raw_file(tmp_path, "cross-device content")
        original_path = raw.path

        real_rename = os.rename

        def _rename_raises_exdev(src: object, dst: object, *a: object, **k: object) -> None:
            raise OSError(errno.EXDEV, "Invalid cross-device link")

        monkeypatch.setattr(os, "rename", _rename_raises_exdev)
        try:
            dest = quarantine_raw_file(cache_dir, raw)
        finally:
            monkeypatch.setattr(os, "rename", real_rename)

        assert not original_path.exists()
        assert dest.exists()
        assert dest.read_text(encoding="utf-8") == "cross-device content"

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
# handled_no_op: a deterministic Tier-0 step that positively handled a raw
# file's content without growing created/updated must NOT be mistaken for a
# drop (Seer review on PR athenaeum#2639, Finding 1). Each test below drives
# `process_one` directly through the real Tier-0 function (never a stub),
# reusing the exact fixture shape its own sibling test module already
# exercises for the underlying tier0_* function, and asserts the entity
# loop's actual drop-exemption signal (`result.handled_no_op`) is set.
# ---------------------------------------------------------------------------


class TestHandledNoOpIsNotADrop:
    def _assert_no_entity_change_and_llm_untouched(self, result, client) -> None:
        client.messages.create.assert_not_called()
        assert result.created == []
        assert result.updated == []

    def test_handle_upsert_no_delta_sets_handled_no_op(self, tmp_path: Path) -> None:
        """Mirrors tests/test_librarian.py::TestTier0HandleUpsert::test_reseed_no_delta_is_noop,
        driven through process_one instead of tier0_handle_upsert directly."""
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        page = wiki / "company-x-x.md"
        page.write_text(
            "---\nuid: company-x\ntype: company\nname: X\naccess: internal\n"
            "domains:\n  - x.example\n---\n\n# X\n\nBody.\n",
            encoding="utf-8",
        )
        raw_dir = tmp_path / "raw" / "contact-wiki"
        raw_dir.mkdir(parents=True)
        raw_path = raw_dir / "seed.md"
        raw_path.write_text(
            "---\nuid: company-x\ntype: company\nname: X\n"
            "domains:\n  - x.example\n---\n\n# X\n\nseed\n",
            encoding="utf-8",
        )
        raw = RawFile(path=raw_path, source="contact-wiki", timestamp="", uuid8="")
        client = MagicMock()
        client.messages.create.side_effect = AssertionError("must not reach the LLM tiers")

        result = process_one(
            raw,
            EntityIndex(wiki),
            wiki,
            client,
            valid_types=["company"],
            valid_tags=[],
            valid_access=["open", "internal", "confidential", "personal"],
        )
        self._assert_no_entity_change_and_llm_untouched(result, client)
        assert result.handled_no_op is True

    def test_bounce_mark_sets_handled_no_op(self, tmp_path: Path) -> None:
        """Mirrors tests/test_bounce_mark.py::TestProcessOneShortCircuits::
        test_llm_client_never_called."""
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        raw_dir = tmp_path / "raw" / "voltaire-bounce-relay"
        raw_dir.mkdir(parents=True)
        raw_path = raw_dir / "note.md"
        raw_path.write_text(
            "---\nobserved_at: 2026-08-05\nsource: script:voltaire-bounce-relay\n---\n\n"
            "Alex's address alex@example.org hard-bounced. "
            "Diagnostic: 550 5.1.1 user unknown.\n",
            encoding="utf-8",
        )
        raw = RawFile(path=raw_path, source="voltaire-bounce-relay", timestamp="", uuid8="")
        client = MagicMock()
        client.messages.create.side_effect = AssertionError("must not reach the LLM tiers")

        result = process_one(
            raw,
            EntityIndex(wiki),
            wiki,
            client,
            valid_types=["person", "company"],
            valid_tags=[],
            valid_access=["open", "internal", "confidential", "personal"],
            config={"storage": {"mapping": {"pii": "excluded"}}},
        )
        self._assert_no_entity_change_and_llm_untouched(result, client)
        assert result.handled_no_op is True

    def test_bounce_verdict_mark_no_op_sets_handled_no_op(self, tmp_path: Path) -> None:
        """Companion to tests/test_bounce_verdict_mark.py — that file covers
        the CHANGED=True stamp; this covers the idempotent no-op (the page
        already carries a `bounced:` value), which it does not test."""
        from tests.test_bounce_verdict_mark import _BARE_550_NOTE

        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        page = wiki / "aaaa1111-someone-example-com.md"
        page.write_text(
            "---\nuid: aaaa1111\ntype: person\nname: someone@example.com\n"
            "memory_class: entity\naccess: personal\n"
            "created: '2026-08-20'\nupdated: '2026-08-20'\n"
            "bounced: 'prior verdict'\n---\n\n# someone@example.com\n\nBody.\n",
            encoding="utf-8",
        )
        raw_dir = tmp_path / "raw" / "voltaire-bounce-relay"
        raw_dir.mkdir(parents=True)
        raw_path = raw_dir / "note.md"
        raw_path.write_text(_BARE_550_NOTE, encoding="utf-8")
        raw = RawFile(path=raw_path, source="voltaire-bounce-relay", timestamp="", uuid8="")
        client = MagicMock()
        client.messages.create.side_effect = AssertionError("must not reach the LLM tiers")

        result = process_one(
            raw,
            EntityIndex(wiki),
            wiki,
            client,
            valid_types=["person", "company"],
            valid_tags=[],
            valid_access=["open", "internal", "confidential", "personal"],
        )
        self._assert_no_entity_change_and_llm_untouched(result, client)
        assert result.handled_no_op is True
        # Prior verdict is untouched, never clobbered.
        assert "prior verdict" in page.read_text(encoding="utf-8")

    def test_do_not_email_mark_no_op_sets_handled_no_op(self, tmp_path: Path) -> None:
        """Companion to tests/test_do_not_email_mark.py — that file covers
        the CHANGED=True stamp; this covers the idempotent no-op (the page
        already has do_not_email: true), which it does not test."""
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        page = wiki / "aaaa1111-someone-example-com.md"
        page.write_text(
            "---\nuid: aaaa1111\ntype: person\nname: someone@example.com\n"
            "memory_class: entity\naccess: personal\n"
            "created: '2026-08-20'\nupdated: '2026-08-20'\n"
            "do_not_email: true\n---\n\n# someone@example.com\n\nBody.\n",
            encoding="utf-8",
        )
        raw_dir = tmp_path / "raw" / "producer"
        raw_dir.mkdir(parents=True)
        raw_path = raw_dir / "note.md"
        raw_path.write_text(
            "---\nobserved_at: 2026-08-25\nsource: test\n---\n\n"
            "Do not email someone@example.com. Operator-directed opt-out.\n",
            encoding="utf-8",
        )
        raw = RawFile(path=raw_path, source="producer", timestamp="", uuid8="")
        client = MagicMock()
        client.messages.create.side_effect = AssertionError("must not reach the LLM tiers")

        result = process_one(
            raw,
            EntityIndex(wiki),
            wiki,
            client,
            valid_types=["person", "company"],
            valid_tags=[],
            valid_access=["open", "internal", "confidential", "personal"],
        )
        self._assert_no_entity_change_and_llm_untouched(result, client)
        assert result.handled_no_op is True


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

    def test_quarantine_failure_leaves_no_lying_ledger_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Seer review on PR athenaeum#2639, Finding 2 (ordering half): when
        the on-disk quarantine move fails for ANY reason (simulated here by
        making quarantine_raw_file raise unconditionally -- e.g. the
        destination disk is full, which shutil.move's own cross-device
        fallback cannot route around either), the drop ledger must NOT have
        already recorded a quarantine that never happened. Proves the
        entity loop disposes BEFORE it records, not after."""
        import anthropic as anthropic_mod

        import athenaeum.drops as drops_module
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

        def _quarantine_always_fails(cache_dir: Path, raw: object) -> Path:
            raise OSError("simulated: destination disk full")

        monkeypatch.setattr(drops_module, "quarantine_raw_file", _quarantine_always_fails)

        raw_path = root / "raw" / "sessions" / "20260410T120000Z-aabbccdd.md"
        with pytest.raises(OSError, match="simulated"):
            run(raw_root=root / "raw", wiki_root=root / "wiki", knowledge_root=root)

        # The raw file was never touched -- disposal failed before anything
        # else happened to it.
        assert raw_path.exists()
        assert raw_path.read_text(encoding="utf-8") == "Nothing worth keeping here.\n"
        # And, the point of this test: no ledger row claims a quarantine
        # that never happened. Either the ledger file was never created, or
        # (equally acceptable) it exists but holds no row for this ref.
        target_ref = "sessions/20260410T120000Z-aabbccdd.md"
        rows = [r for r in iter_drops(cache_dir) if r.raw_ref == target_ref]
        assert rows == []
