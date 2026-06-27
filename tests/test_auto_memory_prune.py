"""Tests for the auto-memory prune driver (issue #278, Part 2).

Acceptance:
  - dry-run (``build_prune_report``) emits a precise kill-list + retained-list
    for human sign-off;
  - ``apply_prune`` removes ONLY the listed files in one git commit;
  - removal is git-recoverable;
  - a legitimate auto-memory page is byte-unaffected.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from athenaeum.auto_memory_prune import (
    apply_prune,
    build_prune_report,
)
from athenaeum.config import _DEFAULT_EPHEMERAL_SCOPES

LEGIT_SCOPE = "-Users-alice-Code-projectx"
EPHEMERAL_SCOPE = "-private-tmp-claude-cctest-abc123"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True
    )


def _git_init(root: Path) -> None:
    _git(root, "init", "-b", "develop")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Prune Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "initial: seed wiki")


def _auto_page(
    scopes: list[str], *, name: str, body: str, ephemeral: bool = False
) -> str:
    fm = [
        "---",
        f"name: {name}",
        "type: auto-memory",
        "cluster_id: c-0001",
        "cluster_centroid_score: 1.0",
        "contradictions_detected: false",
        "origin_scopes:",
    ]
    fm += [f"  - {s}" for s in scopes]
    if ephemeral:
        fm.append("ephemeral: true")
    fm += ["sources: []", "---", "", body, ""]
    return "\n".join(fm)


@pytest.fixture
def wiki_with_auto_pages(tmp_path: Path) -> Path:
    knowledge_root = tmp_path / "knowledge"
    wiki = knowledge_root / "wiki"
    wiki.mkdir(parents=True)

    # Legit auto page (origin scope is a real project dir) -> RETAIN.
    (wiki / "auto-recall-architecture.md").write_text(
        _auto_page(
            [LEGIT_SCOPE],
            name="recall architecture",
            body="The recall hook uses a hybrid FTS5+vector pipeline.",
        ),
        encoding="utf-8",
    )
    # Ephemeral auto page (all origin scopes throwaway) -> KILL.
    (wiki / "auto-cctest-scratch.md").write_text(
        _auto_page(
            [EPHEMERAL_SCOPE, "-private-tmp-claude-cctest-xyz"],
            name="cctest scratch",
            body="Throwaway scratch.",
        ),
        encoding="utf-8",
    )
    # Flagged auto page in a legit scope -> KILL (flag authoritative).
    (wiki / "auto-install-token.md").write_text(
        _auto_page(
            [LEGIT_SCOPE],
            name="install token boilerplate",
            body="Install-token dance.",
            ephemeral=True,
        ),
        encoding="utf-8",
    )
    # Mixed scopes (one real, one throwaway) -> RETAIN (conservative).
    (wiki / "auto-mixed.md").write_text(
        _auto_page(
            [LEGIT_SCOPE, EPHEMERAL_SCOPE],
            name="mixed",
            body="Captured a real fact plus some scratch.",
        ),
        encoding="utf-8",
    )
    # A non-auto-memory page that happens to start with auto- -> RETAIN.
    (wiki / "auto-but-person.md").write_text(
        "---\nname: Someone\ntype: person\n---\n\nA real person.\n",
        encoding="utf-8",
    )

    _git_init(knowledge_root)
    return knowledge_root


def _scopes() -> list[str]:
    return list(_DEFAULT_EPHEMERAL_SCOPES)


class TestBuildPruneReport:
    def test_kill_and_retain_lists(self, wiki_with_auto_pages: Path) -> None:
        wiki = wiki_with_auto_pages / "wiki"
        report = build_prune_report(
            wiki, ephemeral_scopes=_scopes(), operational_markers=[]
        )
        kill_names = {c.path.name for c in report.kill}
        retain_names = {p.name for p, _ in report.retained}

        assert kill_names == {"auto-cctest-scratch.md", "auto-install-token.md"}
        assert "auto-recall-architecture.md" in retain_names
        assert "auto-mixed.md" in retain_names
        assert "auto-but-person.md" in retain_names
        assert report.scanned == 5

    def test_every_kill_has_a_reason(self, wiki_with_auto_pages: Path) -> None:
        wiki = wiki_with_auto_pages / "wiki"
        report = build_prune_report(
            wiki, ephemeral_scopes=_scopes(), operational_markers=[]
        )
        for cand in report.kill:
            assert cand.reason


class TestApplyPrune:
    def test_apply_removes_only_listed(self, wiki_with_auto_pages: Path) -> None:
        knowledge_root = wiki_with_auto_pages
        wiki = knowledge_root / "wiki"
        legit = wiki / "auto-recall-architecture.md"
        legit_bytes = legit.read_bytes()

        report = build_prune_report(
            wiki, ephemeral_scopes=_scopes(), operational_markers=[]
        )
        report = apply_prune(knowledge_root, report)

        assert report.committed is True
        # Killed files gone...
        assert not (wiki / "auto-cctest-scratch.md").exists()
        assert not (wiki / "auto-install-token.md").exists()
        # ...retained files untouched (byte-identical).
        assert legit.exists()
        assert legit.read_bytes() == legit_bytes
        assert (wiki / "auto-mixed.md").exists()
        assert (wiki / "auto-but-person.md").exists()

    def test_removal_is_git_recoverable(self, wiki_with_auto_pages: Path) -> None:
        knowledge_root = wiki_with_auto_pages
        wiki = knowledge_root / "wiki"
        report = build_prune_report(
            wiki, ephemeral_scopes=_scopes(), operational_markers=[]
        )
        apply_prune(knowledge_root, report)

        # The deleted page is recoverable from the parent commit.
        show = _git(knowledge_root, "show", "HEAD~1:wiki/auto-cctest-scratch.md")
        assert "cctest scratch" in show.stdout

    def test_empty_kill_list_is_noop(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        wiki = knowledge_root / "wiki"
        wiki.mkdir(parents=True)
        (wiki / "auto-recall-architecture.md").write_text(
            _auto_page([LEGIT_SCOPE], name="x", body="real fact"),
            encoding="utf-8",
        )
        _git_init(knowledge_root)
        head_before = _git(knowledge_root, "rev-parse", "HEAD").stdout.strip()

        report = build_prune_report(
            wiki, ephemeral_scopes=_scopes(), operational_markers=[]
        )
        report = apply_prune(knowledge_root, report)

        assert report.kill == []
        assert report.committed is False
        head_after = _git(knowledge_root, "rev-parse", "HEAD").stdout.strip()
        assert head_before == head_after

    def test_apply_without_git_errors(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        wiki = knowledge_root / "wiki"
        wiki.mkdir(parents=True)
        (wiki / "auto-cctest-scratch.md").write_text(
            _auto_page([EPHEMERAL_SCOPE], name="x", body="scratch"),
            encoding="utf-8",
        )
        report = build_prune_report(
            wiki, ephemeral_scopes=_scopes(), operational_markers=[]
        )
        report = apply_prune(knowledge_root, report)

        assert report.committed is False
        assert report.errors  # refused: no git repo
        assert (wiki / "auto-cctest-scratch.md").exists()  # nothing removed


class TestPruneCli:
    def test_dry_run_default_prints_lists_exit_2(
        self, wiki_with_auto_pages: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from athenaeum.cli import main

        rc = main(["auto-memory", "prune", "--path", str(wiki_with_auto_pages)])
        out = capsys.readouterr().out
        # Dry-run found candidates -> exit 2 (CI / sign-off signal).
        assert rc == 2
        assert "KILL-LIST" in out
        assert "RETAINED" in out
        assert "auto-cctest-scratch.md" in out
        # Nothing removed on dry-run.
        assert (wiki_with_auto_pages / "wiki" / "auto-cctest-scratch.md").exists()

    def test_missing_target_usage_error(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from athenaeum.cli import main

        assert main(["auto-memory"]) == 2
