# SPDX-License-Identifier: Apache-2.0
"""CLI-level tests for ``athenaeum pii-restore`` (issue athenaeum#1037).

Exercises :mod:`athenaeum._cmd_pii_restore` end to end through
``athenaeum.cli.main`` against the same synthetic fixture repo
:mod:`tests.test_pii_restore_tool` builds -- every root is passed explicitly
(``--knowledge-root``/``--wiki-root``), nothing here reaches ``~/knowledge``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.cli import main
from athenaeum.config import load_config
from athenaeum.librarian import reindex
from athenaeum.pii_restore import MARKER
from athenaeum.search import query_fts5_index
from tests.test_pii_restore_tool import _build_fixture_repo


def _run(root: Path, *extra: str) -> int:
    return main(["pii-restore", "--knowledge-root", str(root), *extra])


def test_dry_run_is_the_default_and_never_writes(tmp_path: Path, capsys) -> None:
    root = _build_fixture_repo(tmp_path)
    before = (root / "wiki" / "renamed-page-new-name.md").read_text()

    rc = _run(root)

    after = (root / "wiki" / "renamed-page-new-name.md").read_text()
    assert before == after
    assert MARKER in after
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "restorable by method" in out
    # AC: honest residue enumeration -- unrestorable classes are NAMED.
    assert "kept:real-pii" in out
    assert "no-pre-image:page-created-after-migration" in out
    # CI-gate signal: dry-run found restorable markers.
    assert rc == 2


def test_apply_writes_and_returns_zero(tmp_path: Path) -> None:
    root = _build_fixture_repo(tmp_path)
    rc = _run(root, "--apply")
    assert rc == 0
    restored = (root / "wiki" / "renamed-page-new-name.md").read_text()
    assert MARKER not in restored
    assert "2025-08-26 05:07:15 UTC" in restored
    # Residue is left exactly as it was.
    assert MARKER in (root / "wiki" / "person-a.md").read_text()


def test_apply_without_reindex_warns_index_still_dirty(tmp_path: Path, capsys) -> None:
    """AC: --apply must not report an unqualified success while the restored
    text is still unreachable via recall -- mirrors ``migrate-pii``'s own
    ``_post_apply_index_step`` contract."""
    root = _build_fixture_repo(tmp_path)
    rc = _run(root, "--apply")
    assert rc == 0
    err = capsys.readouterr().err
    assert "still carries the pre-restore (corrupted) page text" in err
    assert "athenaeum reindex" in err


def test_apply_with_reindex_makes_restored_text_recallable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """AC: --apply --reindex triggers the reindex step so restored text
    replaces corrupted text in the search index -- a real recall smoke
    test, not just a file-on-disk check.

    ``801`` (from the retro filename's issue-number list) is a positive
    control: it is COMPLETELY ABSENT from the corrupted corpus (the marker
    ate it) and present only once restored, so a query for it is unreachable
    before the reindex and reachable after.
    """
    root = _build_fixture_repo(tmp_path)
    # Preserve the fixture's storage.mapping (pii -> excluded) while adding
    # search_backend -- overwriting it entirely would collapse contacts_root
    # back onto wiki_root and make every wiki page look "excluded".
    (root / "athenaeum.yaml").write_text(
        "storage:\n  mapping:\n    pii: excluded\nsearch_backend: fts5\n"
    )
    cache = tmp_path / "cache"
    monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(cache))  # type: ignore[attr-defined]

    # Baseline: the token is unreachable before any restore.
    reindex(root, config=load_config(root))
    assert query_fts5_index("801", cache, n=5) == []

    rc = _run(root, "--apply", "--reindex")
    assert rc == 0
    out = capsys.readouterr().out
    assert "reindexed (" in out

    results = query_fts5_index("801", cache, n=5)
    assert results, "restored token '801' should be reachable via recall after --reindex"
    assert any("retro-citing-page" in r[0] for r in results)


def test_wiki_root_not_found_returns_1(tmp_path: Path, capsys) -> None:
    empty_root = tmp_path / "empty-knowledge"
    empty_root.mkdir()
    rc = _run(empty_root)
    assert rc == 1
    assert "Wiki root not found" in capsys.readouterr().err


def test_missing_git_repository_fails_loudly_instead_of_reporting_false_zero(
    tmp_path: Path, capsys
) -> None:
    """athenaeum#1228 AC1/AC3: a knowledge root with no ``.git`` at all must
    never silently produce a plausible-looking ``TOTAL RESTORABLE = 0`` --
    the tool must refuse to report a plan and fail loudly instead."""
    root = tmp_path / "knowledge"
    wiki = root / "wiki"
    wiki.mkdir(parents=True)
    (wiki / "page.md").write_text(f"---\nuid: 1\n---\nEmail: {MARKER}\n")
    # Separate the excluded surface from wiki/ (mirrors the fixture repo's
    # own athenaeum.yaml) -- without this, the default contacts_root
    # collapses onto wiki_root and every page looks excluded/unscanned.
    (root / "athenaeum.yaml").write_text("storage:\n  mapping:\n    pii: excluded\n")
    # Deliberately no `git init`.

    rc = _run(root)

    captured = capsys.readouterr()
    assert rc == 1
    assert "git history could not be consulted" in captured.err
    assert "TOTAL RESTORABLE" not in captured.out


def test_apply_honours_configured_safe_email_exact(tmp_path: Path) -> None:
    """issue athenaeum#1284: pii.restore.safe_email_exact in athenaeum.yaml
    must actually be honoured end to end through the INSTALLED CLI, not
    just at the library layer -- this is the exact capability regression
    the issue names against scripts/pii-restore.py."""
    root = _build_fixture_repo(tmp_path)
    (root / "athenaeum.yaml").write_text(
        "storage:\n  mapping:\n    pii: excluded\n"
        "pii:\n  restore:\n    safe_email_exact:\n      - jane.doe@example.com\n"
    )

    rc = _run(root, "--apply")

    assert rc == 0
    restored = (root / "wiki" / "person-a.md").read_text()
    assert MARKER not in restored
    assert "jane.doe@example.com" in restored
    # Phone axis is untouched by this key -- still redacted.
    assert MARKER in (root / "wiki" / "person-b.md").read_text()


def test_apply_without_config_still_redacts_real_person_email(tmp_path: Path) -> None:
    """Control: with no pii.restore block at all, person-a.md's real
    address stays redacted exactly as before this issue's fix."""
    root = _build_fixture_repo(tmp_path)

    rc = _run(root, "--apply")

    assert rc == 0
    assert MARKER in (root / "wiki" / "person-a.md").read_text()


def test_clean_corpus_dry_run_returns_zero(tmp_path: Path) -> None:
    """A corpus with no markers at all is a clean 0, not the CI-gate 2."""
    root = tmp_path / "knowledge"
    (root / "wiki").mkdir(parents=True)
    (root / "wiki" / "clean.md").write_text("---\nuid: 1\n---\nNothing redacted here.\n")
    rc = _run(root)
    assert rc == 0
