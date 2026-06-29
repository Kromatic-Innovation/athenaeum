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

    def test_commit_is_scoped_to_kill_list(self, wiki_with_auto_pages: Path) -> None:
        # An unrelated pre-staged change must NOT be swept into the prune
        # commit (Quine SHOULD): the commit pathspec is scoped to the
        # kill-list deletions only.
        knowledge_root = wiki_with_auto_pages
        wiki = knowledge_root / "wiki"
        unrelated = knowledge_root / "unrelated.md"
        unrelated.write_text("pre-staged work\n", encoding="utf-8")
        _git(knowledge_root, "add", "unrelated.md")

        report = build_prune_report(
            wiki, ephemeral_scopes=_scopes(), operational_markers=[]
        )
        report = apply_prune(knowledge_root, report)
        assert report.committed is True

        # The prune commit names ONLY the kill-list files.
        names = _git(knowledge_root, "show", "--name-only", "--format=", "HEAD")
        assert "unrelated.md" not in names.stdout
        assert "auto-cctest-scratch.md" in names.stdout
        # The unrelated file is still staged (uncommitted).
        staged = _git(knowledge_root, "diff", "--cached", "--name-only")
        assert "unrelated.md" in staged.stdout

    def test_git_failure_is_clean_error_not_traceback(
        self, wiki_with_auto_pages: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import athenaeum.auto_memory_prune as amp

        def _boom(root: Path, *args: str) -> object:
            raise subprocess.CalledProcessError(
                returncode=1, cmd=["git", *args], stderr="locked index"
            )

        monkeypatch.setattr(amp, "_git", _boom)
        knowledge_root = wiki_with_auto_pages
        wiki = knowledge_root / "wiki"
        report = amp.build_prune_report(
            wiki, ephemeral_scopes=_scopes(), operational_markers=[]
        )
        report = amp.apply_prune(knowledge_root, report)
        assert report.committed is False
        assert report.errors  # routed to report.errors, no traceback

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

    def test_dry_run_no_candidates_exit_0(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Documented contract: dry-run with an empty kill-list exits 0
        # (the candidates-exist case is the exit-2 test above).
        from athenaeum.cli import main

        knowledge_root = tmp_path / "knowledge"
        wiki = knowledge_root / "wiki"
        wiki.mkdir(parents=True)
        (wiki / "auto-recall-architecture.md").write_text(
            _auto_page([LEGIT_SCOPE], name="x", body="a real durable fact"),
            encoding="utf-8",
        )
        _git_init(knowledge_root)

        rc = main(["auto-memory", "prune", "--path", str(knowledge_root)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "kill:     0" in out
        # Nothing removed.
        assert (wiki / "auto-recall-architecture.md").exists()

    def test_apply_via_argv_removes_only_kill_list_and_rebuilds(
        self,
        wiki_with_auto_pages: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Drive the DESTRUCTIVE --apply path through main()'s argv dispatch
        # (the module-level apply_prune is covered by TestApplyPrune; this
        # asserts the CLI wiring git rm's only the kill-list and fires the
        # recall-index rebuild). The rebuild is stubbed so the test stays
        # hermetic (no chromadb / on-disk index build).
        import athenaeum.cli as cli

        rebuild_calls: list[Path] = []
        monkeypatch.setattr(
            cli,
            "_rebuild_recall_index",
            lambda knowledge_root, cfg, args: rebuild_calls.append(knowledge_root),
        )

        knowledge_root = wiki_with_auto_pages
        wiki = knowledge_root / "wiki"
        legit = wiki / "auto-recall-architecture.md"
        legit_bytes = legit.read_bytes()

        rc = cli.main(
            ["auto-memory", "prune", "--apply", "--path", str(knowledge_root)]
        )
        out = capsys.readouterr().out

        assert rc == 0
        assert "APPLY" in out
        # Only the kill-list files were git rm'd.
        assert not (wiki / "auto-cctest-scratch.md").exists()
        assert not (wiki / "auto-install-token.md").exists()
        # Non-listed pages are byte-untouched.
        assert legit.exists()
        assert legit.read_bytes() == legit_bytes
        assert (wiki / "auto-mixed.md").exists()
        assert (wiki / "auto-but-person.md").exists()
        # The recall-index rebuild fired exactly once, on the prune root.
        assert rebuild_calls == [knowledge_root]
        # The removal landed as a git commit (recoverable).
        head = _git(knowledge_root, "show", "--name-only", "--format=", "HEAD")
        assert "auto-cctest-scratch.md" in head.stdout
        assert "auto-recall-architecture.md" not in head.stdout

    def test_missing_target_usage_error(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from athenaeum.cli import main

        assert main(["auto-memory"]) == 2
