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
from athenaeum.models import is_tombstone, parse_frontmatter, tombstone_target
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
    """A page whose filename matches neither the bare-slug form nor its OWN
    `<uid>-<slug>.md` form (no `uid:` frontmatter here at all, so the
    uid-prefixed identity form can never match) would classify as
    create-merged (not fold) — refuse at proposal time rather than queue a
    proposal that silently would not delete the sources (athenaeum#748)."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    # filename stem "4c7946d3-maria-springer" != slugify("Maria Springer"),
    # and there is no `uid: 4c7946d3` frontmatter to validate it by identity.
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
    # issue athenaeum#1635: the refusal must no longer recommend a renamer that
    # cannot act on this page (it only feeds the name-is-an-email path).
    assert "migrate-pii" not in payload["error"]
    assert not (wiki / "_pending_merges.md").exists()


# ---------------------------------------------------------------------------
# issue athenaeum#1635 — identity-based resolution: a `<uid>-<slug>.md` canonical
# page (the real corpus convention) is accepted when uid:/name: match the
# filename; two live pages resolving to the same slug stay refused, naming
# both; the stale `migrate-pii --rename-only` recommendation is gone.
# ---------------------------------------------------------------------------


def _uid_wiki_page(path: Path, *, uid: str, name: str, body: str) -> None:
    path.write_text(
        "---\n" f"uid: {uid}\n" f"name: {name}\n" "type: concept\n" "---\n" + body,
        encoding="utf-8",
    )


def test_uid_prefixed_canonical_page_succeeds_in_dry_run(tmp_path: Path) -> None:
    """The corpus's real filename convention, `<uid>-<slug>.md`, is now an
    accepted canonical target when the page's own `uid:`/`name:` frontmatter
    matches the filename (issue athenaeum#1635)."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    canonical = wiki / "f351b6a1-learn-something.md"
    _uid_wiki_page(canonical, uid="f351b6a1", name="Learn Something", body="RICH body\n")
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
            "f351b6a1-learn-something",
            "--source",
            "dup",
        ]
    )
    assert rc == 0
    plan = json.loads(out)
    assert plan["ok"] is True
    assert plan["applied"] is False
    assert plan["merge_target_name"] == "Learn Something"
    assert plan["canonical_page"] == str(canonical)
    assert not (wiki / "_pending_merges.md").exists()


def test_refuses_ambiguous_target_naming_both_colliding_pages(tmp_path: Path) -> None:
    """Two live pages resolving to the same slug stay refused, and the error
    names both (issue athenaeum#1635 AC2) -- e.g. a bare-slug file and a
    uid-prefixed file that both carry the same `name:`."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    bare = wiki / "maria-springer.md"
    _wiki_page(bare, name="Maria Springer", body="bare body\n")
    uid_dup = wiki / "aaaa1111-maria-springer.md"
    _uid_wiki_page(uid_dup, uid="aaaa1111", name="Maria Springer", body="uid body\n")
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
            "maria-springer",
            "--source",
            "dup",
        ]
    )
    assert rc == 2
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "ambiguous" in payload["error"]
    assert "maria-springer.md" in payload["error"]
    assert "aaaa1111-maria-springer.md" in payload["error"]
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
    # Canonical page survived; the two source duplicates were folded away --
    # tombstoned in place, not deleted (issue athenaeum#716).
    assert canonical.exists()
    assert src_b.exists()
    assert src_c.exists()
    for src in (src_b, src_c):
        src_meta, _ = parse_frontmatter(src.read_text(encoding="utf-8"))
        assert is_tombstone(src_meta)
        assert tombstone_target(src_meta) == "maria-springer"
    assert set(result["folded_sources"]) == {str(src_b), str(src_c)}
    # The canonical body prose is preserved (a fold does not rewrite it).
    assert "RICH canonical body" in canonical.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Issue athenaeum#1642 — the APPROVE-time half. athenaeum#1635 taught
# propose-fold to accept a `<uid>-<slug>.md` canonical page, but
# ``classify_write_kind`` / ``resolve_merge`` still resolved the target by
# filename shape, so the proposal queued and then did not fold.
# ---------------------------------------------------------------------------


def test_queued_uid_prefixed_fold_actually_folds_on_approve(tmp_path: Path) -> None:
    """End-to-end reproduction: propose-fold --apply into a `<uid>-<slug>.md`
    canonical page, then approve. The proposal must derive
    ``write_kind="fold-into-existing"`` and the approve must FOLD the source
    pages into the existing canonical file (tombstoning them, issue athenaeum#716)
    -- not create a second page beside it and leave every source untouched."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    canonical = wiki / "f351b6a1-learn-s-i-m-p-l-e.md"
    _uid_wiki_page(
        canonical,
        uid="f351b6a1",
        name="Learn S.I.M.P.L.E.",
        body="RICH canonical body\n",
    )
    src_b = wiki / "learn-simple.md"
    src_c = wiki / "simple-method.md"
    _wiki_page(src_b, name="Learn Simple", body="thin dup b\n")
    _wiki_page(src_c, name="Simple Method", body="thin dup c\n")
    init_git_repo(wiki)

    rc, _out = _run(
        [
            "merges",
            "propose-fold",
            "--path",
            str(tmp_path),
            "--into",
            "f351b6a1-learn-s-i-m-p-l-e",
            "--source",
            "learn-simple",
            "--source",
            "simple-method",
            "--apply",
        ]
    )
    assert rc == 0
    merges_path = wiki / "_pending_merges.md"
    queued = parse_pending_merges(merges_path)[0]
    # The classification itself -- create-merged here was the bug.
    assert queued.write_kind == "fold-into-existing"

    result = resolve_merge(merges_path, queued.id, "approve", wiki_root=wiki)

    assert result["ok"] is True
    # Folded INTO the existing uid-prefixed page: sources tombstoned (never
    # deleted, issue athenaeum#716), canonical kept.
    assert canonical.exists()
    assert src_b.exists()
    assert src_c.exists()
    for src in (src_b, src_c):
        src_meta, _ = parse_frontmatter(src.read_text(encoding="utf-8"))
        assert is_tombstone(src_meta)
        # folded_into carries target_slug (slugify(merge_target_name), here
        # the canonical's human-readable name) -- not its uid-prefixed stem.
        assert tombstone_target(src_meta) == "learn-s-i-m-p-l-e"
    assert set(result["folded_sources"]) == {str(src_b), str(src_c)}
    # No 29th page beside it -- the bare-slug file must never be created.
    assert not (wiki / "learn-s-i-m-p-l-e.md").exists()
    # Aliases pick up the folded-away slugs, and NOT the canonical's own stem
    # (which is what the slug-only exclusion used to let through).
    assert set(result["aliases_added"]) == {"learn-simple", "simple-method"}
    assert "f351b6a1-learn-s-i-m-p-l-e" not in set(result["aliases_added"])


# ---------------------------------------------------------------------------
# Issue athenaeum#2082 — tombstone siblings, uid references, renamed canonical.
# ---------------------------------------------------------------------------


def _tombstone_page(path: Path, *, uid: str, name: str, into: str) -> None:
    path.write_text(
        "---\n"
        f"uid: {uid}\n"
        f"name: {name}\n"
        "type: concept\n"
        "status: folded\n"
        f"folded_into: {into}\n"
        "---\nredirect\n",
        encoding="utf-8",
    )


def _propose(tmp_path: Path, *argv: str) -> tuple[int, dict]:
    rc, out = _run(["merges", "propose-fold", "--path", str(tmp_path), "--json", *argv])
    return rc, json.loads(out)


def test_tombstone_siblings_do_not_make_target_ambiguous(tmp_path: Path) -> None:
    """AC1: pages sharing the slug only as `status: folded` tombstones neither
    collide with the live canonical nor block it; approve folds into it."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    canonical = wiki / "d330fd98-csv-exception.md"
    _uid_wiki_page(canonical, uid="d330fd98", name="CSV Exception", body="LIVE body\n")
    # Tombstones sort both before and after the live page.
    _tombstone_page(
        wiki / "05d6bfad-csv-exception.md", uid="05d6bfad", name="CSV Exception", into="x"
    )
    _tombstone_page(
        wiki / "e94cfb6c-csv-exception.md", uid="e94cfb6c", name="CSV Exception", into="x"
    )
    src = wiki / "dup.md"
    _wiki_page(src, name="Dup", body="d\n")
    init_git_repo(wiki)

    rc, plan = _propose(
        tmp_path, "--into", "d330fd98-csv-exception.md", "--source", "dup", "--apply"
    )
    assert rc == 0, plan
    assert plan["canonical_page"] == str(canonical)
    assert plan["write_kind"] == "fold-into-existing"

    merges_path = wiki / "_pending_merges.md"
    queued = parse_pending_merges(merges_path)[0]
    result = resolve_merge(merges_path, queued.id, "approve", wiki_root=wiki)
    assert result["ok"] is True, result
    assert is_tombstone(parse_frontmatter(src.read_text(encoding="utf-8"))[0])
    assert "LIVE body" in canonical.read_text(encoding="utf-8")


def test_two_live_pages_still_ambiguous_beside_tombstones(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    _uid_wiki_page(wiki / "aaaa1111-csv.md", uid="aaaa1111", name="CSV", body="a\n")
    _uid_wiki_page(wiki / "bbbb2222-csv.md", uid="bbbb2222", name="CSV", body="b\n")
    _tombstone_page(wiki / "cccc3333-csv.md", uid="cccc3333", name="CSV", into="x")
    _wiki_page(wiki / "dup.md", name="Dup", body="d\n")
    rc, payload = _propose(tmp_path, "--into", "aaaa1111-csv", "--source", "dup")
    assert rc == 2
    assert "ambiguous" in payload["error"]
    assert "bbbb2222-csv.md" in payload["error"]
    assert "cccc3333" not in payload["error"]


def test_bare_uid_resolves_the_one_live_page(tmp_path: Path) -> None:
    """AC1: `--into <uid>` / `--source <uid>` name the single live page."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    canonical = wiki / "d330fd98-csv-exception.md"
    _uid_wiki_page(canonical, uid="d330fd98", name="CSV Exception", body="LIVE\n")
    _tombstone_page(
        wiki / "05d6bfad-csv-exception.md", uid="05d6bfad", name="CSV Exception", into="x"
    )
    other = wiki / "9f9f9f9f-other-thing.md"
    _uid_wiki_page(other, uid="9f9f9f9f", name="Other Thing", body="o\n")
    rc, plan = _propose(tmp_path, "--into", "d330fd98", "--source", "9f9f9f9f")
    assert rc == 0, plan
    assert plan["canonical_page"] == str(canonical)
    assert plan["folded_sources"] == [str(other.resolve())]


def test_bare_uid_naming_only_a_tombstone_is_refused(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    _uid_wiki_page(wiki / "d330fd98-csv.md", uid="d330fd98", name="CSV", body="L\n")
    _tombstone_page(wiki / "05d6bfad-csv.md", uid="05d6bfad", name="CSV", into="x")
    rc, payload = _propose(tmp_path, "--into", "d330fd98", "--source", "05d6bfad")
    assert rc == 2
    assert "not an existing wiki page" in payload["error"]
    # An explicit path to a tombstone is refused as a tombstone.
    rc, payload = _propose(tmp_path, "--into", "d330fd98", "--source", "05d6bfad-csv.md")
    assert rc == 2
    assert "tombstone" in payload["error"]


def test_renamed_canonical_accepted_by_uid_and_records_drift(tmp_path: Path) -> None:
    """AC2: `81461ddb-alissa.md` with `name: Alissa Hsu Lynch` is accepted
    (filename prefix == own uid); drift is noted; approve folds into THAT
    page rather than creating a duplicate."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    canonical = wiki / "81461ddb-alissa.md"
    _uid_wiki_page(canonical, uid="81461ddb", name="Alissa Hsu Lynch", body="RICH\n")
    src = wiki / "alissa-lynch.md"
    _wiki_page(src, name="Alissa Lynch", body="thin\n")
    init_git_repo(wiki)

    rc, plan = _propose(
        tmp_path, "--into", "81461ddb-alissa.md", "--source", "alissa-lynch", "--apply"
    )
    assert rc == 0, plan
    assert plan["write_kind"] == "fold-into-existing"
    assert len(plan["notes"]) == 1
    assert "drift" in plan["notes"][0]
    assert "Alissa Hsu Lynch" in plan["notes"][0]

    merges_path = wiki / "_pending_merges.md"
    queued = parse_pending_merges(merges_path)[0]
    assert "drift" in queued.rationale
    result = resolve_merge(merges_path, queued.id, "approve", wiki_root=wiki)
    assert result["ok"] is True, result
    assert is_tombstone(parse_frontmatter(src.read_text(encoding="utf-8"))[0])
    assert not (wiki / "alissa.md").exists()
    assert "RICH" in canonical.read_text(encoding="utf-8")


def test_renamed_canonical_with_foreign_uid_prefix_still_refused(tmp_path: Path) -> None:
    """AC2 guard: the filename prefix must be the page's OWN uid."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    _uid_wiki_page(wiki / "deadbeef-alissa.md", uid="81461ddb", name="Alissa Hsu Lynch", body="b\n")
    _wiki_page(wiki / "dup.md", name="Dup", body="d\n")
    rc, payload = _propose(tmp_path, "--into", "deadbeef-alissa", "--source", "dup")
    assert rc == 2
    assert "slugifies" in payload["error"]


def test_bare_slug_tombstone_beside_live_uid_canonical_folds_on_approve(
    tmp_path: Path,
) -> None:
    """Seer finding on #2085: a bare-slug tombstone must not be picked as the
    approve-time target (fold_target_is_tombstone would wedge the proposal)."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    bare_tomb = wiki / "csv-exception.md"
    _tombstone_page(bare_tomb, uid="05d6bfad", name="CSV Exception", into="x")
    canonical = wiki / "d330fd98-csv-exception.md"
    _uid_wiki_page(canonical, uid="d330fd98", name="CSV Exception", body="LIVE body\n")
    src = wiki / "dup.md"
    _wiki_page(src, name="Dup", body="d\n")
    init_git_repo(wiki)

    rc, plan = _propose(
        tmp_path, "--into", "d330fd98-csv-exception.md", "--source", "dup", "--apply"
    )
    assert rc == 0, plan
    merges_path = wiki / "_pending_merges.md"
    queued = parse_pending_merges(merges_path)[0]
    assert queued.write_kind == "fold-into-existing"
    result = resolve_merge(merges_path, queued.id, "approve", wiki_root=wiki)
    assert result["ok"] is True, result
    assert is_tombstone(parse_frontmatter(src.read_text(encoding="utf-8"))[0])
    assert "LIVE body" in canonical.read_text(encoding="utf-8")
    assert "redirect" in bare_tomb.read_text(encoding="utf-8")
