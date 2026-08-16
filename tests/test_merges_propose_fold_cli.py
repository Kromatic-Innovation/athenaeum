# SPDX-License-Identifier: Apache-2.0
"""Tests for ``athenaeum merges propose-fold`` (issue athenaeum#747).

Before this command, consolidating duplicate entities required hand-building a
``_pending_merges.md`` block through the Python API — exactly how the athenaeum#748
``write_kind`` misclassification got introduced. ``propose-fold`` makes an
operator-decided fold expressible from the CLI, deriving every field the fold
mechanics depend on (target name from the canonical page's ``name:``, write_kind
from the corpus) so no hand construction is needed.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

from athenaeum.cli import main as cli_main
from athenaeum.pending_merges import parse_pending_merges, resolve_merge
from tests.conftest import init_git_repo


def _wiki_page(path: Path, *, name: str, body: str) -> None:
    path.write_text(
        "---\n" f"name: {name}\n" "type: concept\n" "---\n" + body, encoding="utf-8"
    )


def _run(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


def _seed(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """A canonical page (filename == its name slug) + two source duplicates."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    canonical = wiki / "maria-springer.md"
    _wiki_page(canonical, name="Maria Springer", body="RICH canonical body\n")
    src_b = wiki / "springer-maria.md"
    src_c = wiki / "m-springer.md"
    _wiki_page(src_b, name="Springer Maria", body="thin dup b\n")
    _wiki_page(src_c, name="M Springer", body="thin dup c\n")
    return wiki, canonical, src_b, src_c


# ---------------------------------------------------------------------------
# AC 1 / 2 — queues one proposal; sources [A, B, C]; target name = A's name;
# write_kind derived, not an argument.
# ---------------------------------------------------------------------------


def test_apply_queues_one_proposal_with_expected_sources_and_target(
    tmp_path: Path,
) -> None:
    wiki, canonical, src_b, src_c = _seed(tmp_path)
    rc, _out = _run(
        [
            "merges",
            "propose-fold",
            "--path",
            str(tmp_path),
            "--into",
            "maria-springer",
            "--source",
            "springer-maria",
            "--source",
            "m-springer",
            "--apply",
        ]
    )
    assert rc == 0
    pms = parse_pending_merges(wiki / "_pending_merges.md")
    assert len(pms) == 1
    pm = pms[0]
    # target name derived from the canonical page's `name:` frontmatter.
    assert pm.merge_target_name == "Maria Springer"
    # sources are [A, B, C] with A = --into (resolved absolute paths).
    assert pm.sources == [str(canonical), str(src_b), str(src_c)]
    # write_kind derived, not taken from a caller argument.
    assert pm.write_kind == "fold-into-existing"


def test_no_write_kind_argument_exists(tmp_path: Path) -> None:
    """write_kind is derived — there is no flag to set it (AC: not an argument)."""
    import pytest

    _seed(tmp_path)
    # argparse rejects the unknown flag: it calls sys.exit(2) -> SystemExit.
    with pytest.raises(SystemExit) as exc:
        cli_main(
            [
                "merges",
                "propose-fold",
                "--path",
                str(tmp_path),
                "--into",
                "maria-springer",
                "--source",
                "springer-maria",
                "--write-kind",
                "create-merged",
            ]
        )
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# AC — default draft is the canonical page's current text; --draft-file
# overrides; the fold does not rewrite the canonical body.
# ---------------------------------------------------------------------------


def test_default_draft_is_canonical_current_text(tmp_path: Path) -> None:
    wiki, canonical, _src_b, _src_c = _seed(tmp_path)
    canonical_text = canonical.read_text(encoding="utf-8")
    # The canonical body differs from every source (per the AC's fixture).
    rc, _out = _run(
        [
            "merges",
            "propose-fold",
            "--path",
            str(tmp_path),
            "--into",
            "maria-springer",
            "--source",
            "springer-maria",
            "--apply",
        ]
    )
    assert rc == 0
    pm = parse_pending_merges(wiki / "_pending_merges.md")[0]
    # The proposed draft is the canonical page's current text verbatim (the
    # render fence strips a single trailing newline in storage — the only
    # transformation, and one the pre-existing block format already applies).
    assert pm.draft_merged_body == canonical_text.rstrip("\n")


def test_draft_file_overrides(tmp_path: Path) -> None:
    wiki, _canonical, _src_b, _src_c = _seed(tmp_path)
    draft = tmp_path / "merged.md"
    draft.write_text("genuinely content-merged body\n", encoding="utf-8")
    rc, _out = _run(
        [
            "merges",
            "propose-fold",
            "--path",
            str(tmp_path),
            "--into",
            "maria-springer",
            "--source",
            "springer-maria",
            "--draft-file",
            str(draft),
            "--apply",
        ]
    )
    assert rc == 0
    pm = parse_pending_merges(wiki / "_pending_merges.md")[0]
    assert pm.draft_merged_body == "genuinely content-merged body"


# ---------------------------------------------------------------------------
# AC — dry-run writes nothing and prints canonical, sources, write_kind.
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing_and_prints_plan(tmp_path: Path) -> None:
    wiki, _canonical, _src_b, _src_c = _seed(tmp_path)
    rc, out = _run(
        [
            "merges",
            "propose-fold",
            "--path",
            str(tmp_path),
            "--into",
            "maria-springer",
            "--source",
            "springer-maria",
            "--source",
            "m-springer",
        ]
    )
    assert rc == 0
    # Nothing queued.
    assert not (wiki / "_pending_merges.md").exists()
    # The plan names the canonical page, each source, and the derived write_kind.
    assert "maria-springer" in out
    assert "springer-maria" in out
    assert "m-springer" in out
    assert "fold-into-existing" in out


def test_dry_run_json_plan(tmp_path: Path) -> None:
    wiki, canonical, src_b, _src_c = _seed(tmp_path)
    rc, out = _run(
        [
            "merges",
            "propose-fold",
            "--path",
            str(tmp_path),
            "--json",
            "--into",
            "maria-springer",
            "--source",
            "springer-maria",
        ]
    )
    assert rc == 0
    plan = json.loads(out)
    assert plan["ok"] is True
    assert plan["applied"] is False
    assert plan["merge_target_name"] == "Maria Springer"
    assert plan["write_kind"] == "fold-into-existing"
    assert plan["sources"] == [str(canonical), str(src_b)]
    assert not (wiki / "_pending_merges.md").exists()


# ---------------------------------------------------------------------------
# AC — refusals: --into not an existing page; --source equals --into; a
# canonical page whose filename is not its slug.
# ---------------------------------------------------------------------------


def test_refuses_nonexistent_into(tmp_path: Path) -> None:
    _seed(tmp_path)
    rc, _out = _run(
        [
            "merges",
            "propose-fold",
            "--path",
            str(tmp_path),
            "--into",
            "does-not-exist",
            "--source",
            "springer-maria",
        ]
    )
    assert rc == 2


def test_refuses_source_equals_into(tmp_path: Path) -> None:
    _seed(tmp_path)
    rc, _out = _run(
        [
            "merges",
            "propose-fold",
            "--path",
            str(tmp_path),
            "--into",
            "maria-springer",
            "--source",
            "maria-springer",
        ]
    )
    assert rc == 2


def test_refuses_canonical_whose_filename_is_not_its_slug(tmp_path: Path) -> None:
    """A uid-prefixed canonical page (`<uid>-<slug>.md`) would classify as
    create-merged (not fold) — refuse at proposal time rather than queue a
    proposal that silently would not delete the sources (athenaeum#748)."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    # filename stem "4c7946d3-maria-springer" != slugify("Maria Springer").
    uid_page = wiki / "4c7946d3-maria-springer.md"
    _wiki_page(uid_page, name="Maria Springer", body="body\n")
    src = wiki / "dup.md"
    _wiki_page(src, name="Dup", body="d\n")
    rc, out = _run(
        [
            "merges",
            "propose-fold",
            "--path",
            str(tmp_path),
            "--json",
            "--into",
            "4c7946d3-maria-springer",
            "--source",
            "dup",
        ]
    )
    assert rc == 2
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "slugifies" in payload["error"]
    assert not (wiki / "_pending_merges.md").exists()


# ---------------------------------------------------------------------------
# AC — approval via the existing resolve_merge path is unchanged (no second
# write path): a queued proposal folds correctly and preserves the canonical
# body while deleting the sources.
# ---------------------------------------------------------------------------


def test_queued_proposal_folds_correctly_via_resolve_merge(tmp_path: Path) -> None:
    wiki, canonical, src_b, src_c = _seed(tmp_path)
    # Issue athenaeum#947: resolve_merge now refuses a fold-into-existing approve
    # outside a git repo — this is the one test in this module that actually
    # approves (the rest only exercise propose-fold's queueing/dry-run).
    init_git_repo(wiki)
    rc, _out = _run(
        [
            "merges",
            "propose-fold",
            "--path",
            str(tmp_path),
            "--into",
            "maria-springer",
            "--source",
            "springer-maria",
            "--source",
            "m-springer",
            "--apply",
        ]
    )
    assert rc == 0
    merges_path = wiki / "_pending_merges.md"
    pm_id = parse_pending_merges(merges_path)[0].id

    result = resolve_merge(merges_path, pm_id, "approve", wiki_root=wiki)

    assert result["ok"] is True
    # Canonical page survived; the two source duplicates were folded away.
    assert canonical.exists()
    assert not src_b.exists()
    assert not src_c.exists()
    assert set(result["folded_sources"]) == {str(src_b), str(src_c)}
    # The canonical body prose is preserved (a fold does not rewrite it).
    assert "RICH canonical body" in canonical.read_text(encoding="utf-8")
