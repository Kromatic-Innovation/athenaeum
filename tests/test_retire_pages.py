# SPDX-License-Identifier: Apache-2.0
"""Tests for the generic wiki-page retirement command (issue athenaeum#1625).

Mirrors ``tests/test_decay_sweep.py``'s structure closely — same
dry-run-report / apply split, same git-recoverability assertions — since
``athenaeum.retire_pages`` deliberately reuses ``decay_sweep.py``'s
two-commit archive mechanics rather than re-implementing them (see that
module's :func:`~athenaeum.decay_sweep.archive_via_two_commit_git_rm`).

Acceptance (issue athenaeum#1625):
  - dry-run (no ``--apply``) leaves a fixture repo's working tree and HEAD
    unchanged;
  - ``--apply`` retires the kill-list via a two-commit git-rm: the pages
    are absent from HEAD, HEAD~1 (or an earlier no-op-Commit-A ancestor,
    exactly like decay-sweep) carries the provenance snapshot, and
    ``git show <that commit>:<page>`` recovers each page byte-identical;
  - an unknown (or ambiguous) uid aborts the WHOLE run before any commit;
  - a pending-merge proposal referencing a retired page (by source OR by
    target) is withdrawn/archived; an unrelated proposal is untouched;
  - a retired page's entry disappears from ``wiki/_index.md``; a
    non-retired page's entry stays;
  - the recall-index rebuild is invoked after apply (fake backend) and a
    rebuild failure is reported without failing the retirement;
  - every test here runs on a fixture git repo under ``tmp_path`` only.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from athenaeum import cli
from athenaeum.pending_merges import parse_pending_merges, write_pending_merge
from athenaeum.retire_pages import apply_retirement, build_retire_report, resolve_uids


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True
    )


def _git_init(root: Path) -> None:
    _git(root, "init", "-b", "develop")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Retire Pages Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "initial: seed wiki")


def _page(*, uid: str, name: str, ptype: str = "project", body: str = "body") -> str:
    lines = ["---", f"uid: {uid}", f"name: {name}", f"type: {ptype}", "---", "", body, ""]
    return "\n".join(lines)


@pytest.fixture
def wiki_repo(tmp_path: Path) -> Path:
    knowledge_root = tmp_path / "knowledge"
    wiki = knowledge_root / "wiki"
    wiki.mkdir(parents=True)
    (wiki / "aaaa1111-alpha.md").write_text(
        _page(uid="aaaa1111", name="Alpha", body="Alpha content, a dud template restatement."),
        encoding="utf-8",
    )
    (wiki / "bbbb2222-beta.md").write_text(
        _page(uid="bbbb2222", name="Beta", body="Beta content, keep this one."),
        encoding="utf-8",
    )
    _git_init(knowledge_root)
    return knowledge_root


class TestResolveUids:
    def test_resolves_unique_uid(self, wiki_repo: Path) -> None:
        candidates, errors = resolve_uids(wiki_repo / "wiki", ["aaaa1111"])
        assert errors == []
        assert len(candidates) == 1
        assert candidates[0].path.name == "aaaa1111-alpha.md"

    def test_unknown_uid_reported(self, wiki_repo: Path) -> None:
        candidates, errors = resolve_uids(wiki_repo / "wiki", ["zzzzzzzz"])
        assert candidates == []
        assert len(errors) == 1
        assert errors[0].reason == "unknown"

    def test_ambiguous_uid_reported(self, wiki_repo: Path) -> None:
        wiki = wiki_repo / "wiki"
        (wiki / "extra-dupe.md").write_text(
            _page(uid="aaaa1111", name="Alpha Duplicate", body="same uid, second page"),
            encoding="utf-8",
        )
        candidates, errors = resolve_uids(wiki, ["aaaa1111"])
        assert candidates == []
        assert len(errors) == 1
        assert errors[0].reason == "ambiguous"
        assert len(errors[0].matches) == 2

    def test_mixed_known_and_unknown_reports_only_the_bad_one(
        self, wiki_repo: Path
    ) -> None:
        # resolve_uids reports per-uid outcomes; it is the CALLER's job (the
        # CLI, tested below in TestUnknownUidAbortsBeforeCommit) to refuse
        # the WHOLE batch whenever `errors` is non-empty, never partially
        # apply the ones that did resolve.
        candidates, errors = resolve_uids(wiki_repo / "wiki", ["aaaa1111", "zzzzzzzz"])
        assert [c.uid for c in candidates] == ["aaaa1111"]
        assert len(errors) == 1
        assert errors[0].uid == "zzzzzzzz"


class TestDryRun:
    def test_dry_run_leaves_tree_and_head_unchanged(self, wiki_repo: Path) -> None:
        head_before = _git(wiki_repo, "rev-parse", "HEAD").stdout.strip()
        status_before = _git(wiki_repo, "status", "--porcelain").stdout

        candidates, errors = resolve_uids(wiki_repo / "wiki", ["aaaa1111"])
        assert errors == []
        report = build_retire_report(wiki_repo, candidates)

        assert len(report.kill) == 1
        head_after = _git(wiki_repo, "rev-parse", "HEAD").stdout.strip()
        status_after = _git(wiki_repo, "status", "--porcelain").stdout
        assert head_before == head_after
        assert status_before == status_after
        assert (wiki_repo / "wiki" / "aaaa1111-alpha.md").exists()


class TestApply:
    def test_apply_retires_page_recoverable_from_git_history(
        self, wiki_repo: Path
    ) -> None:
        candidates, errors = resolve_uids(wiki_repo / "wiki", ["aaaa1111"])
        assert errors == []

        report = apply_retirement(wiki_repo, candidates, reason="dud template page")
        assert report.committed is True
        assert not report.errors

        head_show = subprocess.run(
            ["git", "show", "HEAD:wiki/aaaa1111-alpha.md"],
            cwd=str(wiki_repo),
            capture_output=True,
            text=True,
            check=False,
        )
        assert head_show.returncode != 0  # gone from HEAD

        recovered = _git(wiki_repo, "show", "HEAD~1:wiki/aaaa1111-alpha.md")
        assert "Alpha content, a dud template restatement." in recovered.stdout

        commit_subject = _git(wiki_repo, "log", "-1", "--format=%s").stdout
        assert "dud template page" in commit_subject
        assert "athenaeum#1625" in commit_subject

    def test_apply_without_git_refuses(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        wiki = knowledge_root / "wiki"
        wiki.mkdir(parents=True)
        (wiki / "aaaa1111-alpha.md").write_text(
            _page(uid="aaaa1111", name="Alpha"), encoding="utf-8"
        )
        candidates, errors = resolve_uids(wiki, ["aaaa1111"])
        assert errors == []
        report = apply_retirement(knowledge_root, candidates, reason="x")
        assert report.committed is False
        assert report.errors
        assert (wiki / "aaaa1111-alpha.md").exists()

    def test_uncommitted_page_still_recoverable_via_provenance_snapshot(
        self, wiki_repo: Path
    ) -> None:
        # Edited since the last commit -- Commit A must catch this, not
        # just Commit B's `git rm` (mirrors decay-sweep's own test).
        page = wiki_repo / "wiki" / "aaaa1111-alpha.md"
        page.write_text(
            _page(uid="aaaa1111", name="Alpha", body="EDITED after initial commit."),
            encoding="utf-8",
        )
        candidates, errors = resolve_uids(wiki_repo / "wiki", ["aaaa1111"])
        assert errors == []
        report = apply_retirement(wiki_repo, candidates, reason="x")
        assert report.committed is True

        recovered = _git(wiki_repo, "show", "HEAD~1:wiki/aaaa1111-alpha.md")
        assert "EDITED after initial commit" in recovered.stdout


class TestUnknownUidAbortsBeforeCommit:
    def test_unknown_uid_via_cli_aborts_before_any_commit(
        self, wiki_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        head_before = _git(wiki_repo, "rev-parse", "HEAD").stdout.strip()

        exit_code = cli.main(
            [
                "retire-pages",
                "--path",
                str(wiki_repo),
                "--uids",
                "aaaa1111",
                "zzzzzzzz",
                "--reason",
                "test",
                "--apply",
            ]
        )
        assert exit_code == 1

        head_after = _git(wiki_repo, "rev-parse", "HEAD").stdout.strip()
        assert head_before == head_after
        assert (wiki_repo / "wiki" / "aaaa1111-alpha.md").exists()

        captured = capsys.readouterr()
        assert "zzzzzzzz" in captured.err
        assert "unknown" in captured.err.lower()


class TestPendingMergeWithdrawal:
    def test_source_match_withdrawn_unrelated_untouched(self, wiki_repo: Path) -> None:
        wiki = wiki_repo / "wiki"
        merges_path = wiki / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="Alpha",
            sources=[str(wiki / "aaaa1111-alpha.md")],
            rationale="duplicate of alpha",
            draft_merged_body="merged alpha body",
            confidence=0.9,
        )
        write_pending_merge(
            merges_path,
            merge_target_name="Unrelated",
            sources=[str(wiki / "bbbb2222-beta.md")],
            rationale="totally unrelated to alpha",
            draft_merged_body="merged unrelated body",
            confidence=0.9,
        )

        candidates, errors = resolve_uids(wiki, ["aaaa1111"])
        assert errors == []
        report = apply_retirement(wiki_repo, candidates, reason="dud page")
        assert report.committed is True

        remaining = parse_pending_merges(merges_path)
        assert len(remaining) == 1
        assert remaining[0].merge_target_name == "Unrelated"

        archive_path = wiki / "_pending_merges_archive.md"
        assert archive_path.is_file()
        archived_text = archive_path.read_text(encoding="utf-8")
        assert 'Merge: "Alpha"' in archived_text
        assert "**Retired**:" in archived_text
        assert "dud page" in archived_text

    def test_target_match_withdrawn(self, wiki_repo: Path) -> None:
        # The proposal's TARGET slug (not a source) equals a retired
        # page's filename stem -- issue athenaeum#1625 Plan step 5 says
        # "sources OR target".
        wiki = wiki_repo / "wiki"
        merges_path = wiki / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="aaaa1111-alpha",
            sources=[str(wiki / "bbbb2222-beta.md")],
            rationale="fold beta into the (about-to-be-retired) alpha slug",
            draft_merged_body="merged body",
            confidence=0.9,
        )

        candidates, errors = resolve_uids(wiki, ["aaaa1111"])
        assert errors == []
        report = apply_retirement(wiki_repo, candidates, reason="dud page")
        assert report.committed is True

        remaining = parse_pending_merges(merges_path)
        assert remaining == []
        archive_path = wiki / "_pending_merges_archive.md"
        assert "target page" in archive_path.read_text(encoding="utf-8")


class TestIndexRebuild:
    def test_retired_page_removed_non_retired_stays(self, wiki_repo: Path) -> None:
        candidates, errors = resolve_uids(wiki_repo / "wiki", ["aaaa1111"])
        assert errors == []
        report = apply_retirement(wiki_repo, candidates, reason="dud page")
        assert report.committed is True

        index_text = (wiki_repo / "wiki" / "_index.md").read_text(encoding="utf-8")
        assert "aaaa1111-alpha.md" not in index_text
        assert "Alpha" not in index_text
        assert "bbbb2222-beta.md" in index_text
        assert "Beta" in index_text


class TestRecallIndexRebuildWiring:
    def test_rebuild_invoked_after_apply(
        self, wiki_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import athenaeum.search as search_mod

        calls: list[Path] = []

        def _fake_build_fts5_index(wiki_root: Path, cache_dir: Path, **kwargs: object) -> int:
            calls.append(wiki_root)
            return 1

        monkeypatch.setattr(search_mod, "build_fts5_index", _fake_build_fts5_index)

        cache_dir = wiki_repo.parent / "cache"
        exit_code = cli.main(
            [
                "retire-pages",
                "--path",
                str(wiki_repo),
                "--uids",
                "aaaa1111",
                "--reason",
                "test",
                "--cache-dir",
                str(cache_dir),
                "--apply",
            ]
        )
        assert exit_code == 0
        assert calls == [wiki_repo / "wiki"]

    def test_rebuild_failure_reported_without_failing_retirement(
        self, wiki_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import athenaeum.search as search_mod

        def _boom(wiki_root: Path, cache_dir: Path, **kwargs: object) -> int:
            raise RuntimeError("simulated fake-backend failure")

        monkeypatch.setattr(search_mod, "build_fts5_index", _boom)

        cache_dir = wiki_repo.parent / "cache"
        exit_code = cli.main(
            [
                "retire-pages",
                "--path",
                str(wiki_repo),
                "--uids",
                "aaaa1111",
                "--reason",
                "test",
                "--cache-dir",
                str(cache_dir),
                "--apply",
            ]
        )
        # The retirement itself must still succeed -- only the rebuild failed.
        assert exit_code == 0
        head_show = subprocess.run(
            ["git", "show", "HEAD:wiki/aaaa1111-alpha.md"],
            cwd=str(wiki_repo),
            capture_output=True,
            text=True,
            check=False,
        )
        assert head_show.returncode != 0  # page really was retired

        captured = capsys.readouterr()
        assert "WARN" in captured.err
        assert "recall index rebuild failed" in captured.err


class TestIndexRebuildFailureAborts:
    """An index-rebuild I/O error must abort before the archive commit, not
    escape with the ``git rm`` already staged and nothing committed (review
    finding on this issue's PR)."""

    def test_rebuild_oserror_aborts_before_archive_commit(
        self, wiki_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        candidates, errors = resolve_uids(wiki_repo / "wiki", ["aaaa1111"])
        assert errors == []

        def _boom(_wiki_root: Path) -> None:
            raise OSError("disk went away")

        monkeypatch.setattr("athenaeum.retire_pages.rebuild_index", _boom)

        report = apply_retirement(wiki_repo, candidates, reason="dud template page")

        assert report.committed is False
        assert any("index rebuild failed" in e for e in report.errors), report.errors
        # No archive commit landed, so the page is still reachable from HEAD.
        recovered = _git(wiki_repo, "show", "HEAD:wiki/aaaa1111-alpha.md")
        assert "Alpha content, a dud template restatement." in recovered.stdout
