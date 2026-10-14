# SPDX-License-Identifier: Apache-2.0
"""Tests for the collision-triage fixture builder (issue athenaeum#2066).

Everything here operates on a synthetic ``tmp_path`` wiki -- never the real
``~/knowledge`` corpus. :func:`athenaeum.pending_merges.write_pending_merge`
and :func:`athenaeum.pending_merges.resolve_merge` build the sidecar the
same way production does, so these tests exercise the real ledger parser
against real block syntax, not a hand-rolled fixture string.
"""

from __future__ import annotations

import importlib.util
import json
import stat
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "build_collision_triage_fixtures.py"
_spec = importlib.util.spec_from_file_location("build_collision_triage_fixtures", _SCRIPT)
assert _spec and _spec.loader
build_collision_triage_fixtures = importlib.util.module_from_spec(_spec)
sys.modules["build_collision_triage_fixtures"] = build_collision_triage_fixtures
_spec.loader.exec_module(build_collision_triage_fixtures)

from athenaeum.pending_merges import resolve_merge, write_pending_merge  # noqa: E402


def _write_page(wiki_root: Path, filename: str, *, name: str, uid: str, body: str) -> Path:
    path = wiki_root / filename
    path.write_text(
        f"---\nuid: {uid}\nname: {name}\ntype: person\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def _git_init(wiki_root: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-b", "develop"], cwd=wiki_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=wiki_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=wiki_root, check=True, capture_output=True
    )
    subprocess.run(["git", "add", "-A"], cwd=wiki_root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=wiki_root, check=True, capture_output=True)


class TestBuildFixtures:
    def test_reject_decision_becomes_label_false(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        page_a = _write_page(
            wiki_root, "aaa-taylor-kim.md", name="Taylor Kim", uid="aaa", body="Marketing lead."
        )
        page_b = _write_page(
            wiki_root, "bbb-taylor-kim.md", name="Taylor Kim", uid="bbb", body="Illustrator."
        )
        merges_path = wiki_root / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="aaa-taylor-kim",
            sources=[str(page_a), str(page_b)],
            rationale=(
                "athenaeum#1170 nightly name-collision scan: 2 pages share the name "
                "'Taylor Kim' (type: person); classified ambiguous (canonical: aaa-taylor-kim.md)."
            ),
            draft_merged_body=page_a.read_text(encoding="utf-8"),
            confidence=1.0,
            write_kind=None,
        )
        from athenaeum.pending_merges import parse_pending_merges

        merge_id = parse_pending_merges(merges_path)[0].id
        result = resolve_merge(merges_path, merge_id, "reject", wiki_root=wiki_root)
        assert result["ok"] is True

        rows, report = build_collision_triage_fixtures.build_fixtures(wiki_root)
        assert report.rows_written == 1
        assert report.n_different_person == 1
        assert report.n_same_person == 0
        assert rows[0].label is False
        assert "Taylor Kim" in rows[0].text_a or "Taylor Kim" in rows[0].text_b

    def test_approve_fold_becomes_label_true(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        page_a = _write_page(
            wiki_root,
            "ccc-jordan-reyes.md",
            name="Jordan Reyes",
            uid="ccc",
            body="Platform engineer.",
        )
        page_b = _write_page(
            wiki_root,
            "ddd-jordan-reyes.md",
            name="Jordan Reyes",
            uid="ddd",
            body="Also platform engineer.",
        )
        _git_init(wiki_root)
        merges_path = wiki_root / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="ccc-jordan-reyes",
            sources=[str(page_a), str(page_b)],
            rationale=(
                "athenaeum#1170 nightly name-collision scan: 2 pages share the name "
                "'Jordan Reyes' (type: person); classified ambiguous "
                "(canonical: ccc-jordan-reyes.md)."
            ),
            draft_merged_body=page_a.read_text(encoding="utf-8"),
            confidence=1.0,
            write_kind=None,
        )
        from athenaeum.pending_merges import parse_pending_merges

        merge_id = parse_pending_merges(merges_path)[0].id
        result = resolve_merge(merges_path, merge_id, "approve", wiki_root=wiki_root)
        assert result["ok"] is True, result

        rows, report = build_collision_triage_fixtures.build_fixtures(wiki_root)
        assert report.rows_written == 1
        assert report.n_same_person == 1
        assert rows[0].label is True

    def test_auto_applied_excluded(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        page_a = _write_page(wiki_root, "eee-x.md", name="X Person", uid="eee", body="body a")
        page_b = _write_page(wiki_root, "fff-x.md", name="X Person", uid="fff", body="body b")
        _git_init(wiki_root)
        merges_path = wiki_root / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="eee-x",
            sources=[str(page_a), str(page_b)],
            rationale=(
                "athenaeum#1170 nightly name-collision scan: 2 pages share the name "
                "'X Person' (type: person); classified unambiguous (canonical: eee-x.md)."
            ),
            draft_merged_body=page_a.read_text(encoding="utf-8"),
            confidence=1.0,
            write_kind=None,
        )
        from athenaeum.pending_merges import parse_pending_merges

        merge_id = parse_pending_merges(merges_path)[0].id
        resolve_merge(merges_path, merge_id, "approve", wiki_root=wiki_root, auto_applied=True)

        rows, report = build_collision_triage_fixtures.build_fixtures(wiki_root)
        assert report.auto_applied_excluded == 1
        assert report.rows_written == 0

    def test_unresolved_blocks_are_not_fixtures(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        page_a = _write_page(wiki_root, "ggg-y.md", name="Y Person", uid="ggg", body="a")
        page_b = _write_page(wiki_root, "hhh-y.md", name="Y Person", uid="hhh", body="b")
        merges_path = wiki_root / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="ggg-y",
            sources=[str(page_a), str(page_b)],
            rationale=(
                "athenaeum#1170 nightly name-collision scan: 2 pages share the name "
                "'Y Person' (type: person); classified ambiguous (canonical: ggg-y.md)."
            ),
            draft_merged_body=page_a.read_text(encoding="utf-8"),
            confidence=1.0,
            write_kind=None,
        )
        rows, report = build_collision_triage_fixtures.build_fixtures(wiki_root)
        assert report.rows_written == 0
        assert report.resolved == 0

    def test_non_collision_merge_blocks_ignored(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        page_a = _write_page(wiki_root, "iii-z.md", name="Z Person", uid="iii", body="a")
        merges_path = wiki_root / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="z-person",
            sources=[str(page_a)],
            rationale="some other proposal, not a name-collision scan",
            draft_merged_body=page_a.read_text(encoding="utf-8"),
            confidence=0.8,
            write_kind="create-merged",
        )
        from athenaeum.pending_merges import parse_pending_merges

        merge_id = parse_pending_merges(merges_path)[0].id
        resolve_merge(merges_path, merge_id, "approve", wiki_root=wiki_root)
        rows, report = build_collision_triage_fixtures.build_fixtures(wiki_root)
        assert report.name_collision_blocks == 0
        assert report.rows_written == 0

    def test_never_leaks_resolution_frontmatter_into_text(self, tmp_path: Path) -> None:
        """A page already carrying a resolution-time frontmatter key
        (status/folded_into/aliases/merge_rejected_with) must never have
        that key's VALUE show up in the built record text -- only name +
        body excerpt are ever read."""
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        path = wiki_root / "jjj-leak-check.md"
        path.write_text(
            "---\n"
            "uid: jjj\n"
            "name: Leak Check Person\n"
            "type: person\n"
            "status: folded\n"
            "folded_into: some-other-slug-marker\n"
            "aliases: [alias-marker-value]\n"
            "---\n\n"
            "ordinary body text\n",
            encoding="utf-8",
        )
        text = build_collision_triage_fixtures._page_text(path)
        assert text is not None
        assert "some-other-slug-marker" not in text
        assert "alias-marker-value" not in text
        assert "folded" not in text


class TestWriteFixtures:
    def test_writes_mode_600_inside_mode_700_dir(self, tmp_path: Path) -> None:
        FixtureRow = build_collision_triage_fixtures.FixtureRow
        rows = [FixtureRow(text_a="a", text_b="b", label=True)]
        output_dir = tmp_path / "fixtures_out"
        output_path = output_dir / "pairs.jsonl"

        build_collision_triage_fixtures.write_fixtures(rows, output_path)

        dir_mode = stat.S_IMODE(output_dir.stat().st_mode)
        file_mode = stat.S_IMODE(output_path.stat().st_mode)
        assert dir_mode == 0o700
        assert file_mode == 0o600

        lines = output_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        row = json.loads(lines[0])
        assert row == {"text_a": "a", "text_b": "b", "label": True}
