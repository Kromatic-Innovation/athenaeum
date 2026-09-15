# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#1601 AC4: a `FilesystemStore` run over a tmp git repo
records spend and observations, then snapshots, and `git ls-files` shows
neither ledger.

This is the corpus-containment assertion at the actual write-then-commit
seam (`FilesystemStore.snapshot` is `git add -A` over `knowledge_root` —
`src/athenaeum/store.py:1411`), not just at the resolver level: it is the
test the observed harm this issue is filed about would have failed had the
two ledgers actually migrated into `wiki_root` and a librarian run then
snapshotted the corpus. `tests/test_push_metrics.py`'s
`test_record_push_with_wiki_root_never_writes_into_the_corpus` covers the
analogous ground for `_push_records.jsonl` (issue athenaeum#1591) without
exercising a real git commit; this test goes one step further and actually
snapshots, then reads the commit's tree.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from athenaeum import llm_schemas, spend
from athenaeum.models import TokenUsage
from athenaeum.store import FilesystemStore

_WIKI_SURFACE = "wiki"


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)


def _tracked_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


class TestSnapshotExcludesTelemetryLedgers:
    def test_git_ls_files_shows_neither_ledger_after_snapshot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The autouse cache-dir isolation fixture (tests/conftest.py) already
        # pins ATHENAEUM_SPEND_LEDGER to ITS OWN per-test cache dir, for
        # hermeticity; clear it here so this test's own explicit cache_dir=
        # argument is what actually governs resolution.
        monkeypatch.delenv("ATHENAEUM_SPEND_LEDGER", raising=False)
        # The same autouse fixture defaults schema-observation recording OFF
        # under test (belt-and-braces against a stray write to the operator's
        # real ledger); opt back in explicitly, as tests/test_llm_schemas.py
        # does.
        monkeypatch.setenv("ATHENAEUM_SCHEMA_OBSERVATIONS_ENABLED", "1")
        knowledge_root = tmp_path / "knowledge_root"
        knowledge_root.mkdir()
        _init_git_repo(knowledge_root)
        wiki_root = knowledge_root / "wiki"
        wiki_root.mkdir()
        store = FilesystemStore(knowledge_root, roots={_WIKI_SURFACE: wiki_root})
        assert store.capabilities.versioned is True

        cache_dir = tmp_path / "cache"

        # Record a spend row and an observation row, both passing
        # wiki_root=knowledge_root the way every production caller does.
        usage = TokenUsage()
        usage.add(10, 5, 0, 0, model="corpus-containment-probe")
        assert (
            spend.record_spend(
                usage,
                run_type="librarian",
                provider="claude-cli",
                cache_dir=cache_dir,
                wiki_root=knowledge_root,
            )
            is True
        )
        llm_schemas.record_observation(
            contract="corpus-containment-probe",
            call_site="test",
            outcome="ok",
            cache_dir=cache_dir,
            wiki_root=knowledge_root,
        )

        # Also write an ordinary wiki page, so the commit has real content
        # and `snapshot` has something to stage besides the (absent) ledgers.
        (wiki_root / "page.md").write_text("# hello\n", encoding="utf-8")

        sha = store.snapshot("test: corpus containment probe")
        assert sha is not None, "the wiki page alone should produce a real commit"

        tracked = _tracked_files(knowledge_root)
        assert spend.LEDGER_FILENAME not in tracked
        assert llm_schemas.OBSERVATIONS_FILENAME not in tracked
        assert "wiki/page.md" in tracked

        # Neither ledger should even be ON DISK under knowledge_root — they
        # were never written there in the first place.
        assert not (knowledge_root / spend.LEDGER_FILENAME).exists()
        assert not (wiki_root / spend.LEDGER_FILENAME).exists()
        assert not (knowledge_root / llm_schemas.OBSERVATIONS_FILENAME).exists()
        assert not (wiki_root / llm_schemas.OBSERVATIONS_FILENAME).exists()

        # And the rows genuinely landed in the cache dir, outside the store
        # entirely.
        assert (cache_dir / spend.LEDGER_FILENAME).exists()
        assert (cache_dir / llm_schemas.OBSERVATIONS_FILENAME).exists()
