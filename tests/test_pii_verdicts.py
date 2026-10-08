# SPDX-License-Identifier: Apache-2.0
"""Tests for sticky PII-classification verdicts through the athenaeum#712
ledger (issue athenaeum#689 AC3/AC4). All values below are synthetic
fixtures — no real corpus content.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.pii_classification_decision import (
    CLASS_IS_PII,
    CLASS_NOT_PII_ALIAS,
    CLASS_NOT_PII_PAGE_PURPOSE,
)
from athenaeum.pii_verdicts import (
    PiiVerdictError,
    is_marked_is_pii_migrated,
    is_marked_not_pii,
    load_is_pii_values,
    load_not_pii_allowlist,
    lookup_is_pii_verdict,
    lookup_not_pii_verdict,
    record_is_pii,
    record_not_pii,
)
from athenaeum.runlock import RunLock
from athenaeum.verdicts import iter_live_entries

SYNTHETIC_ALIAS_VALUE = "user@example-ssh-host.test"
SYNTHETIC_PERSONAL_VALUE = "firstname.lastname@examplecorp.test"


def _off_corpus_config(off_corpus_dir: Path) -> dict:
    return {
        "off_corpus": {"enabled": True, "adapter": "off-corpus-test"},
        "storage": {
            "adapters": {
                "off-corpus-test": {
                    "backing_store": "markdown",
                    "surface_root": str(off_corpus_dir),
                    "corpus_policy": {
                        "embedded": False,
                        "recallable": True,
                        "merge_eligible": False,
                    },
                },
            },
        },
    }


# ---------------------------------------------------------------------------
# "not PII" direction: sticky, in-git, suppresses re-flagging
# ---------------------------------------------------------------------------


class TestNotPiiDirection:
    def test_record_and_lookup_round_trips(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        wiki_root = knowledge_root / "wiki"
        wiki_root.mkdir(parents=True)

        with RunLock(knowledge_root) as lock:
            entry = record_not_pii(
                wiki_root,
                page_id="auto-example-ssh",
                value=SYNTHETIC_ALIAS_VALUE,
                verdict_class=CLASS_NOT_PII_ALIAS,
                decided_by="human:test",
                lock=lock,
            )

        assert entry.verdict == CLASS_NOT_PII_ALIAS
        found = lookup_not_pii_verdict(
            wiki_root, page_id="auto-example-ssh", value=SYNTHETIC_ALIAS_VALUE
        )
        assert found is not None
        assert found.verdict == CLASS_NOT_PII_ALIAS

    def test_is_marked_not_pii_suppresses_re_flagging(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        wiki_root = knowledge_root / "wiki"
        wiki_root.mkdir(parents=True)

        assert not is_marked_not_pii(
            wiki_root, page_id="auto-example-page", value=SYNTHETIC_ALIAS_VALUE
        )

        with RunLock(knowledge_root) as lock:
            record_not_pii(
                wiki_root,
                page_id="auto-example-page",
                value=SYNTHETIC_ALIAS_VALUE,
                verdict_class=CLASS_NOT_PII_ALIAS,
                decided_by="human:test",
                lock=lock,
            )

        assert is_marked_not_pii(
            wiki_root, page_id="auto-example-page", value=SYNTHETIC_ALIAS_VALUE
        )

    def test_record_not_pii_rejects_is_pii_class(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        wiki_root = knowledge_root / "wiki"
        wiki_root.mkdir(parents=True)
        with RunLock(knowledge_root) as lock:
            with pytest.raises(PiiVerdictError):
                record_not_pii(
                    wiki_root,
                    page_id="auto-example-page",
                    value=SYNTHETIC_PERSONAL_VALUE,
                    verdict_class=CLASS_IS_PII,
                    decided_by="human:test",
                    lock=lock,
                )

    def test_load_not_pii_allowlist_shaped_like_pii_allowlist(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        wiki_root = knowledge_root / "wiki"
        wiki_root.mkdir(parents=True)

        assert load_not_pii_allowlist(wiki_root) == {}

        with RunLock(knowledge_root) as lock:
            record_not_pii(
                wiki_root,
                page_id="auto-example-ssh",
                value=SYNTHETIC_ALIAS_VALUE,
                verdict_class=CLASS_NOT_PII_ALIAS,
                decided_by="human:test",
                lock=lock,
            )

        merged = load_not_pii_allowlist(wiki_root)
        assert SYNTHETIC_ALIAS_VALUE in merged
        assert isinstance(merged[SYNTHETIC_ALIAS_VALUE], str)

    def test_load_not_pii_allowlist_latest_wins(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        wiki_root = knowledge_root / "wiki"
        wiki_root.mkdir(parents=True)

        with RunLock(knowledge_root) as lock:
            record_not_pii(
                wiki_root,
                page_id="auto-example-page-a",
                value=SYNTHETIC_ALIAS_VALUE,
                verdict_class=CLASS_NOT_PII_ALIAS,
                decided_by="human:first",
                lock=lock,
                at="2026-01-01",
            )
            record_not_pii(
                wiki_root,
                page_id="auto-example-page-b",
                value=SYNTHETIC_ALIAS_VALUE,
                verdict_class=CLASS_NOT_PII_PAGE_PURPOSE,
                decided_by="human:second",
                lock=lock,
                at="2026-02-01",
            )

        merged = load_not_pii_allowlist(wiki_root)
        assert "human:second" in merged[SYNTHETIC_ALIAS_VALUE]

    def test_page_purpose_class_is_recorded_plainly_in_git_ledger(self, tmp_path: Path) -> None:
        """A non-PII value is safe to appear in plaintext in the in-git
        partition file — mirrors the existing _pii-allowlist.yml precedent."""
        knowledge_root = tmp_path / "knowledge"
        wiki_root = knowledge_root / "wiki"
        wiki_root.mkdir(parents=True)

        with RunLock(knowledge_root) as lock:
            record_not_pii(
                wiki_root,
                page_id="auto-example-self-record",
                value="operator@example-self.test",
                verdict_class=CLASS_NOT_PII_PAGE_PURPOSE,
                decided_by="human:test",
                lock=lock,
            )

        partitions = list((wiki_root / "_verdicts").glob("*.jsonl"))
        assert partitions
        contents = "\n".join(p.read_text(encoding="utf-8") for p in partitions)
        assert "operator@example-self.test" in contents


# ---------------------------------------------------------------------------
# "is PII" direction: never in-git, routes off-corpus, stays sticky
# ---------------------------------------------------------------------------


class TestIsPiiDirection:
    def test_refuses_without_off_corpus_configured(self) -> None:
        result = record_is_pii(
            page_id="auto-example-client-page",
            value=SYNTHETIC_PERSONAL_VALUE,
            decided_by="human:test",
        )
        assert result == {"ok": False, "error_code": "erasure_class_refused", "pair": None}

    def test_routes_to_off_corpus_when_configured(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        (knowledge_root / "wiki").mkdir(parents=True)
        off_corpus_dir = tmp_path / "off-corpus-store"
        off_corpus_dir.mkdir()
        config = _off_corpus_config(off_corpus_dir)

        result = record_is_pii(
            page_id="auto-example-client-page",
            value=SYNTHETIC_PERSONAL_VALUE,
            decided_by="human:test",
            knowledge_root=knowledge_root,
            config=config,
        )
        assert result["ok"] is True
        assert result["error_code"] is None

    def test_value_never_lands_in_the_in_git_ledger(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        wiki_root = knowledge_root / "wiki"
        wiki_root.mkdir(parents=True)
        off_corpus_dir = tmp_path / "off-corpus-store"
        off_corpus_dir.mkdir()
        config = _off_corpus_config(off_corpus_dir)

        record_is_pii(
            page_id="auto-example-client-page",
            value=SYNTHETIC_PERSONAL_VALUE,
            decided_by="human:test",
            knowledge_root=knowledge_root,
            config=config,
        )

        # Nothing in the in-git ledger at all...
        assert iter_live_entries(wiki_root) == []
        # ...and the value is not sitting anywhere under the git working tree.
        for path in knowledge_root.rglob("*"):
            if path.is_file():
                assert SYNTHETIC_PERSONAL_VALUE not in path.read_text(
                    encoding="utf-8", errors="ignore"
                )
        # It IS present off-corpus, outside the knowledge root.
        off_corpus_contents = "\n".join(
            p.read_text(encoding="utf-8") for p in off_corpus_dir.rglob("*.jsonl")
        )
        assert SYNTHETIC_PERSONAL_VALUE in off_corpus_contents

    def test_is_marked_is_pii_migrated_suppresses_re_proposing(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        (knowledge_root / "wiki").mkdir(parents=True)
        off_corpus_dir = tmp_path / "off-corpus-store"
        off_corpus_dir.mkdir()
        config = _off_corpus_config(off_corpus_dir)

        assert not is_marked_is_pii_migrated(
            page_id="auto-example-client-page",
            value=SYNTHETIC_PERSONAL_VALUE,
            knowledge_root=knowledge_root,
            config=config,
        )

        record_is_pii(
            page_id="auto-example-client-page",
            value=SYNTHETIC_PERSONAL_VALUE,
            decided_by="human:test",
            knowledge_root=knowledge_root,
            config=config,
        )

        assert is_marked_is_pii_migrated(
            page_id="auto-example-client-page",
            value=SYNTHETIC_PERSONAL_VALUE,
            knowledge_root=knowledge_root,
            config=config,
        )

    def test_is_marked_is_pii_migrated_false_without_off_corpus(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        (knowledge_root / "wiki").mkdir(parents=True)
        assert not is_marked_is_pii_migrated(
            page_id="auto-example-client-page",
            value=SYNTHETIC_PERSONAL_VALUE,
            knowledge_root=knowledge_root,
            config=None,
        )

    def test_load_is_pii_values_includes_migrated_value(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        (knowledge_root / "wiki").mkdir(parents=True)
        off_corpus_dir = tmp_path / "off-corpus-store"
        off_corpus_dir.mkdir()
        config = _off_corpus_config(off_corpus_dir)

        assert load_is_pii_values(knowledge_root=knowledge_root, config=config) == frozenset()

        record_is_pii(
            page_id="auto-example-client-page",
            value=SYNTHETIC_PERSONAL_VALUE,
            decided_by="human:test",
            knowledge_root=knowledge_root,
            config=config,
        )

        assert load_is_pii_values(knowledge_root=knowledge_root, config=config) == frozenset(
            {SYNTHETIC_PERSONAL_VALUE}
        )

    def test_load_is_pii_values_empty_without_off_corpus(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        (knowledge_root / "wiki").mkdir(parents=True)
        assert load_is_pii_values(knowledge_root=knowledge_root, config=None) == frozenset()

    def test_lookup_is_pii_verdict_returns_entry(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        (knowledge_root / "wiki").mkdir(parents=True)
        off_corpus_dir = tmp_path / "off-corpus-store"
        off_corpus_dir.mkdir()
        config = _off_corpus_config(off_corpus_dir)

        record_is_pii(
            page_id="auto-example-client-page",
            value=SYNTHETIC_PERSONAL_VALUE,
            decided_by="human:test",
            knowledge_root=knowledge_root,
            config=config,
        )
        entry = lookup_is_pii_verdict(
            page_id="auto-example-client-page",
            value=SYNTHETIC_PERSONAL_VALUE,
            knowledge_root=knowledge_root,
            config=config,
        )
        assert entry is not None
        assert entry.verdict == CLASS_IS_PII
