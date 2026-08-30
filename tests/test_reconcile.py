# SPDX-License-Identifier: Apache-2.0
"""Tests for the dual-write raw-intake reconcile pass (athenaeum#1143).

Fixture shape mirrors the real incident: a "streak-to-wiki"-style producer
dual-writes a raw/<source>/ file and a wiki page in the SAME commit, then the
raw copy is left pending forever. Every scenario below builds a tiny git
repo with two commits — an import commit (the dual write) and a later
commit representing "now" — so :func:`athenaeum.reconcile.run_reconcile`'s
git-history predicate has real history to read, not just working-tree state.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from athenaeum.reconcile import (
    DIVERGED_AT_IMPORT,
    GENUINELY_NEW,
    MODIFIED_SINCE_IMPORT,
    NOT_IN_IMPORT_COMMIT,
    NOT_VERSIONED,
    WIKI_ABSENT_AT_IMPORT,
    plan_reconcile,
    run_reconcile,
)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True, check=True)


def _init_repo(root: Path) -> None:
    _git(root, "init", "-b", "develop")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Reconcile Test")


def _commit_all(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-m", message)
    return _git(root, "rev-parse", "HEAD").stdout.strip()


RAW_FRONTMATTER = """---
uid: {uid}
type: person
name: {name}
access: confidential
---

# {name}

{body}
"""

WIKI_FRONTMATTER = """---
uid: {uid}
type: person
name: {name}
access: confidential
---

# {name}

{body}
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def kr(tmp_path: Path) -> Path:
    root = tmp_path / "kb"
    (root / "raw" / "drive").mkdir(parents=True)
    (root / "wiki").mkdir(parents=True)
    # Git tracks no empty directories — seed a placeholder so the initial
    # commit below actually has something to commit.
    (root / "raw" / "drive" / ".gitkeep").write_text("", encoding="utf-8")
    (root / "wiki" / ".gitkeep").write_text("", encoding="utf-8")
    _init_repo(root)
    _commit_all(root, "seed")
    return root


IMPORT_COMMIT = "import"  # placeholder resolved to a real SHA per test


class TestByteIdenticalAtImportIsRemoved:
    def test_dual_written_pair_is_removed(self, kr: Path) -> None:
        # The core case: raw + wiki written together, byte-identical, at the
        # import commit. Untouched since. Must be removed.
        raw_path = kr / "raw" / "drive" / "aaaa0001-jane-d.md"
        wiki_path = kr / "wiki" / "aaaa0001-jane-d.md"
        text = RAW_FRONTMATTER.format(uid="aaaa0001", name="Jane D", body="Jane works at Acme.")
        _write(raw_path, text)
        _write(wiki_path, text)
        import_commit = _commit_all(kr, "streak-to-wiki: dual write")

        report = plan_reconcile(kr, source="drive", import_commit=import_commit)

        assert report.removed == ["drive/aaaa0001-jane-d.md"]
        assert report.retained == []
        assert report.genuinely_new == []

    def test_apply_removes_via_git_rm_and_commits(self, kr: Path) -> None:
        raw_path = kr / "raw" / "drive" / "aaaa0002-joe-b.md"
        wiki_path = kr / "wiki" / "aaaa0002-joe-b.md"
        text = RAW_FRONTMATTER.format(uid="aaaa0002", name="Joe B", body="Joe works at Acme.")
        _write(raw_path, text)
        _write(wiki_path, text)
        import_commit = _commit_all(kr, "streak-to-wiki: dual write")

        report = run_reconcile(kr, source="drive", import_commit=import_commit, dry_run=False)

        assert report.removed == ["drive/aaaa0002-joe-b.md"]
        assert report.committed is True
        assert not raw_path.exists()
        # Reversible: the file is recoverable from the parent commit.
        head = _git(kr, "rev-parse", "HEAD").stdout.strip()
        restored = _git(kr, "show", f"{head}^:raw/drive/aaaa0002-joe-b.md").stdout
        assert restored == text

    def test_second_pass_is_idempotent(self, kr: Path) -> None:
        raw_path = kr / "raw" / "drive" / "aaaa0003-amy-c.md"
        wiki_path = kr / "wiki" / "aaaa0003-amy-c.md"
        text = RAW_FRONTMATTER.format(uid="aaaa0003", name="Amy C", body="Amy works at Acme.")
        _write(raw_path, text)
        _write(wiki_path, text)
        import_commit = _commit_all(kr, "streak-to-wiki: dual write")

        first = run_reconcile(kr, source="drive", import_commit=import_commit, dry_run=False)
        assert first.removed == ["drive/aaaa0003-amy-c.md"]

        second = run_reconcile(kr, source="drive", import_commit=import_commit, dry_run=False)
        assert second.removed == []
        assert second.committed is False


class TestTemplateRemovalCaseIsStillRemoved:
    """The athenaeum#279 scenario: wiki content deliberately removed AFTER
    import must not block removal — condition (4) only compares import-time
    snapshots, never current wiki bytes."""

    def test_current_wiki_divergence_after_import_does_not_block_removal(self, kr: Path) -> None:
        raw_path = kr / "raw" / "drive" / "aaaa0004-dawn-b.md"
        wiki_path = kr / "wiki" / "aaaa0004-dawn-b.md"
        text_at_import = RAW_FRONTMATTER.format(
            uid="aaaa0004",
            name="Dawn B",
            body="Dawn B.\n\n## Kromatic Sales Pipeline\n\n- stage: Closed - Won\n",
        )
        _write(raw_path, text_at_import)
        _write(wiki_path, text_at_import)
        import_commit = _commit_all(kr, "streak-to-wiki: dual write")

        # A later, deliberate cleanup (athenaeum#279 stand-in) rewrites the
        # wiki page to drop the template section. The raw copy is untouched.
        cleaned = WIKI_FRONTMATTER.format(uid="aaaa0004", name="Dawn B", body="")
        _write(wiki_path, cleaned)
        _commit_all(kr, "athenaeum#279 stand-in: drop duplicated template")

        report = plan_reconcile(kr, source="drive", import_commit=import_commit)

        assert report.removed == ["drive/aaaa0004-dawn-b.md"]


class TestGenuinelyNewIsPreserved:
    def test_uid_with_no_wiki_entity_is_retained_as_genuinely_new(self, kr: Path) -> None:
        raw_path = kr / "raw" / "drive" / "bbbb0001-no-page.md"
        text = RAW_FRONTMATTER.format(uid="bbbb0001", name="No Page", body="Novel fact.")
        _write(raw_path, text)
        _commit_all(kr, "streak-to-wiki: raw only, no wiki page")

        report = plan_reconcile(kr, source="drive", import_commit="HEAD")

        assert report.removed == []
        assert report.genuinely_new == ["drive/bbbb0001-no-page.md"]
        disp = next(d for d in report.dispositions if d.ref == "drive/bbbb0001-no-page.md")
        assert disp.disposition == GENUINELY_NEW

    def test_name_fallback_resolution_when_uid_absent(self, kr: Path) -> None:
        # No `uid:` field in the raw frontmatter — resolution must fall back
        # to EntityIndex.lookup(name).
        raw_path = kr / "raw" / "drive" / "bbbb0002-name-only.md"
        wiki_path = kr / "wiki" / "cccc0002-name-only.md"
        # Byte-identical dual write, no `uid:` field on EITHER side — proves
        # resolution falls back to name/alias lookup rather than failing
        # closed just because there is no uid to key on.
        shared_text = "---\nname: Name Only\naccess: confidential\n---\n\n# Name Only\n\nBody.\n"
        _write(raw_path, shared_text)
        _write(wiki_path, shared_text)
        import_commit = _commit_all(kr, "streak-to-wiki: dual write, name-keyed")

        report = plan_reconcile(kr, source="drive", import_commit=import_commit)

        assert report.removed == ["drive/bbbb0002-name-only.md"]


class TestModifiedSinceImportIsRetained:
    def test_raw_file_edited_after_import_is_not_removed(self, kr: Path) -> None:
        raw_path = kr / "raw" / "drive" / "aaaa0005-ed-f.md"
        wiki_path = kr / "wiki" / "aaaa0005-ed-f.md"
        text = RAW_FRONTMATTER.format(uid="aaaa0005", name="Ed F", body="Ed works at Acme.")
        _write(raw_path, text)
        _write(wiki_path, text)
        import_commit = _commit_all(kr, "streak-to-wiki: dual write")

        # Someone touched the raw file after import (e.g. manual correction).
        _write(raw_path, text + "\nExtra hand-added line.\n")
        _commit_all(kr, "manual edit after import")

        report = plan_reconcile(kr, source="drive", import_commit=import_commit)

        assert report.removed == []
        disp = next(d for d in report.dispositions if d.ref == "drive/aaaa0005-ed-f.md")
        assert disp.disposition == MODIFIED_SINCE_IMPORT


class TestDivergedAtImportIsRetained:
    def test_raw_and_wiki_differed_even_at_import_time(self, kr: Path) -> None:
        # Not a true dual-write pair — the wiki page pre-existed with
        # different content and the raw file happens to share a uid.
        raw_path = kr / "raw" / "drive" / "aaaa0006-gi-h.md"
        wiki_path = kr / "wiki" / "aaaa0006-gi-h.md"
        _write(
            raw_path,
            RAW_FRONTMATTER.format(uid="aaaa0006", name="Gi H", body="Raw-only detail."),
        )
        _write(
            wiki_path,
            WIKI_FRONTMATTER.format(uid="aaaa0006", name="Gi H", body="Different wiki detail."),
        )
        import_commit = _commit_all(kr, "streak-to-wiki: not actually identical")

        report = plan_reconcile(kr, source="drive", import_commit=import_commit)

        assert report.removed == []
        disp = next(d for d in report.dispositions if d.ref == "drive/aaaa0006-gi-h.md")
        assert disp.disposition == DIVERGED_AT_IMPORT


class TestNotInImportCommitIsRetained:
    def test_raw_file_added_after_import_commit_is_retained(self, kr: Path) -> None:
        # import_commit is the SEED commit, before this raw file ever
        # existed — resolution succeeds (wiki page exists) but the raw path
        # itself has no history at import_commit, so it cannot be proven
        # identical there.
        import_commit = _git(kr, "rev-parse", "HEAD").stdout.strip()

        raw_path = kr / "raw" / "drive" / "aaaa0007-ka-l.md"
        wiki_path = kr / "wiki" / "aaaa0007-ka-l.md"
        text = RAW_FRONTMATTER.format(uid="aaaa0007", name="Ka L", body="Ka works at Acme.")
        _write(raw_path, text)
        _write(wiki_path, text)
        _commit_all(kr, "added later, not part of the named import commit")

        report = plan_reconcile(kr, source="drive", import_commit=import_commit)

        assert report.removed == []
        disp = next(d for d in report.dispositions if d.ref == "drive/aaaa0007-ka-l.md")
        assert disp.disposition == NOT_IN_IMPORT_COMMIT


class TestWikiAbsentAtImportIsRetained:
    def test_wiki_page_created_after_the_named_import_commit(self, kr: Path) -> None:
        raw_path = kr / "raw" / "drive" / "aaaa0008-mo-n.md"
        text = RAW_FRONTMATTER.format(uid="aaaa0008", name="Mo N", body="Mo works at Acme.")
        _write(raw_path, text)
        import_commit = _commit_all(kr, "raw arrives first")

        wiki_path = kr / "wiki" / "aaaa0008-mo-n.md"
        _write(wiki_path, text)
        _commit_all(kr, "wiki page created later, separately")

        report = plan_reconcile(kr, source="drive", import_commit=import_commit)

        assert report.removed == []
        disp = next(d for d in report.dispositions if d.ref == "drive/aaaa0008-mo-n.md")
        assert disp.disposition == WIKI_ABSENT_AT_IMPORT


class TestDuplicateUidHandledPerFile:
    def test_each_copy_of_a_shared_uid_is_judged_independently(self, kr: Path) -> None:
        # 278-uids/565-files case, minimized: two raw files share a uid (the
        # same entity exported from two pipelines). One is a faithful
        # dual-write copy; the other was hand-edited after import. Each is
        # judged on its own bytes.
        uid = "aaaa0009"
        wiki_path = kr / "wiki" / f"{uid}-pat-q.md"
        text = RAW_FRONTMATTER.format(uid=uid, name="Pat Q", body="Pat works at Acme.")
        _write(wiki_path, text)

        raw_a = kr / "raw" / "drive" / f"{uid}-pat-q-pipeline-a.md"
        raw_b = kr / "raw" / "drive" / f"{uid}-pat-q-pipeline-b.md"
        _write(raw_a, text)
        _write(raw_b, text)
        import_commit = _commit_all(kr, "two pipelines export the same person")

        _write(raw_b, text + "\nPipeline-b-only note.\n")
        _commit_all(kr, "pipeline-b copy hand-edited after import")

        report = plan_reconcile(kr, source="drive", import_commit=import_commit)

        assert report.removed == [f"drive/{uid}-pat-q-pipeline-a.md"]
        disp_b = next(d for d in report.dispositions if d.ref == f"drive/{uid}-pat-q-pipeline-b.md")
        assert disp_b.disposition == MODIFIED_SINCE_IMPORT


class TestNotVersionedRefusesRemoval:
    def test_refuses_to_remove_without_a_git_repo(self, tmp_path: Path) -> None:
        root = tmp_path / "no-git-kb"
        (root / "raw" / "drive").mkdir(parents=True)
        (root / "wiki").mkdir(parents=True)
        text = RAW_FRONTMATTER.format(uid="dddd0001", name="No Git", body="x")
        _write(root / "raw" / "drive" / "dddd0001-no-git.md", text)
        _write(root / "wiki" / "dddd0001-no-git.md", text)

        report = run_reconcile(root, source="drive", import_commit="HEAD", dry_run=False)

        assert report.removed == []
        assert report.committed is False
        disp = next(d for d in report.dispositions if d.ref == "drive/dddd0001-no-git.md")
        assert disp.disposition == NOT_VERSIONED
        assert (root / "raw" / "drive" / "dddd0001-no-git.md").exists()


class TestDryRunDefault:
    def test_dry_run_never_touches_disk(self, kr: Path) -> None:
        raw_path = kr / "raw" / "drive" / "aaaa0010-ru-s.md"
        wiki_path = kr / "wiki" / "aaaa0010-ru-s.md"
        text = RAW_FRONTMATTER.format(uid="aaaa0010", name="Ru S", body="Ru works at Acme.")
        _write(raw_path, text)
        _write(wiki_path, text)
        import_commit = _commit_all(kr, "streak-to-wiki: dual write")

        report = run_reconcile(
            kr, source="drive", import_commit=import_commit
        )  # dry_run default True

        assert report.dry_run is True
        assert report.removed == ["drive/aaaa0010-ru-s.md"]
        assert report.committed is False
        assert raw_path.exists()
