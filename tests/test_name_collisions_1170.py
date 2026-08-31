# SPDX-License-Identifier: Apache-2.0
"""Tests for athenaeum.name_collisions and its librarian.run() wiring
(issue athenaeum#1170 AC2-AC6; AC1 lives in tests/test_create_name_gate_1173.py).

One class per AC, plus a scan/classify unit-level class covering the
detector's own logic in isolation:

- ``TestScanNameCollisions`` / ``TestCanonicalPageAndClassify`` — the
  deterministic detector itself: grouping, type-scoping, canonical
  tiebreak, and the unambiguous/ambiguous reduction.
- ``TestRunNameCollisionPhase`` — AC2: the phase exists, is invoked by
  ``run()``, records its own run_profile entry (with a collision count)
  independent of wiki-dedup, makes zero provider/LLM calls, and its entry
  lands in the run-summary ledger record.
- ``TestResolveNameCollisionsIdempotency`` — AC3: re-running over an
  unchanged corpus is a no-op the second time.
- ``TestResolveNameCollisionsReversibility`` — AC4: an auto-merge lands as
  discrete git commits and is recoverable via ``git show``/``git revert``.
- ``TestResolveNameCollisionsAmbiguousQueue`` — AC5: an ambiguous collision
  surfaces via ``decisions.list_pending_decisions`` and nothing merges.
- ``TestResolveNameCollisionsAliasSurvival`` — AC6: the absorbed name
  survives as an alias on the canonical page and still resolves via
  ``EntityIndex.lookup``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from athenaeum.decisions import list_pending_decisions
from athenaeum.librarian import _run_name_collision_phase, _run_wiki_dedup_phase
from athenaeum.models import EntityIndex, parse_frontmatter
from athenaeum.name_collisions import (
    CollisionPage,
    NameCollision,
    canonical_page,
    classify_collision,
    resolve_name_collisions,
    scan_name_collisions,
)
from athenaeum.pending_merges import parse_pending_merges
from athenaeum.run_summary_log import build_run_summary_ledger_record
from tests.conftest import init_git_repo
from tests.test_librarian_run_phases import _make_ctx


def _write_page(
    wiki: Path,
    filename: str,
    *,
    uid: str = "",
    name: str,
    type_: str | None = None,
    aliases: list[str] | None = None,
    body: str = "Some content.\n",
) -> Path:
    """Write a minimal wiki page with frontmatter."""
    wiki.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    if uid:
        lines.append(f"uid: {uid}")
    lines.append(f"name: {name}")
    if type_ is not None:
        lines.append(f"type: {type_}")
    if aliases:
        lines.append("aliases:")
        lines.extend(f"  - {a}" for a in aliases)
    lines.append("---")
    lines.append("")
    lines.append(body)
    path = wiki / filename
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _git_log_messages(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "log", "--format=%s"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


# ---------------------------------------------------------------------------
# scan_name_collisions
# ---------------------------------------------------------------------------


class TestScanNameCollisions:
    def test_two_pages_same_name_same_type_is_a_collision(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "acme.md", uid="u1", name="Acme", type_="company")
        _write_page(wiki, "acme-dup.md", uid="u2", name="Acme", type_="company")
        collisions = scan_name_collisions(wiki)
        assert len(collisions) == 1
        assert collisions[0].name == "Acme"
        assert collisions[0].type == "company"
        assert len(collisions[0].pages) == 2

    def test_case_insensitive_grouping(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "acme.md", uid="u1", name="Acme", type_="company")
        _write_page(wiki, "ACME.md", uid="u2", name="ACME", type_="company")
        collisions = scan_name_collisions(wiki)
        assert len(collisions) == 1

    def test_same_name_different_type_is_not_a_collision(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "tristankromer-p.md", uid="u1", name="tristankromer", type_="project")
        _write_page(wiki, "tristankromer-h.md", uid="u2", name="tristankromer", type_="person")
        assert scan_name_collisions(wiki) == []

    def test_single_page_is_not_a_collision(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "acme.md", uid="u1", name="Acme", type_="company")
        assert scan_name_collisions(wiki) == []

    def test_underscore_prefixed_sidecars_are_skipped(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "acme.md", uid="u1", name="Acme", type_="company")
        (wiki / "_pending_merges.md").write_text("# Pending Merges\n", encoding="utf-8")
        (wiki / "_pending_questions.md").write_text("# Pending Questions\n", encoding="utf-8")
        assert scan_name_collisions(wiki) == []

    def test_unparseable_or_frontmatter_less_file_is_skipped_not_raised(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "plain.md").write_text("Just prose, no frontmatter at all.\n", encoding="utf-8")
        (wiki / "acme.md").write_text(
            "---\nname: Acme\n---\n", encoding="utf-8"
        )  # only one -- no collision, but must not raise on the sibling
        assert scan_name_collisions(wiki) == []

    def test_page_with_no_name_key_is_skipped(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "no-name.md").write_text("---\ntype: company\n---\nbody\n", encoding="utf-8")
        (wiki / "acme.md").write_text("---\nname: Acme\n---\nbody\n", encoding="utf-8")
        assert scan_name_collisions(wiki) == []

    def test_untyped_pages_group_under_none(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "acme.md", uid="u1", name="Acme")
        _write_page(wiki, "acme-dup.md", uid="u2", name="Acme")
        collisions = scan_name_collisions(wiki)
        assert len(collisions) == 1
        assert collisions[0].type is None

    def test_deterministic_ordering(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "zeta-2.md", uid="u1", name="Zeta", type_="concept")
        _write_page(wiki, "zeta-1.md", uid="u2", name="Zeta", type_="concept")
        _write_page(wiki, "alpha-2.md", uid="u3", name="Alpha", type_="concept")
        _write_page(wiki, "alpha-1.md", uid="u4", name="Alpha", type_="concept")
        collisions = scan_name_collisions(wiki)
        assert [c.name for c in collisions] == ["Alpha", "Zeta"]
        assert [str(p.path) for p in collisions[0].pages] == sorted(
            str(p.path) for p in collisions[0].pages
        )


# ---------------------------------------------------------------------------
# canonical_page / classify_collision
# ---------------------------------------------------------------------------


class TestCanonicalPageAndClassify:
    def test_canonical_is_longest_body(self, tmp_path: Path) -> None:
        pages = (
            CollisionPage(Path("b.md"), "u2", "Acme", "company", "short"),
            CollisionPage(Path("a.md"), "u1", "Acme", "company", "a much longer body here"),
        )
        collision = NameCollision("Acme", "company", pages)
        assert canonical_page(collision).path == Path("a.md")

    def test_canonical_tiebreak_is_smallest_path(self, tmp_path: Path) -> None:
        pages = (
            CollisionPage(Path("b.md"), "u2", "Acme", "company", "same length!"),
            CollisionPage(Path("a.md"), "u1", "Acme", "company", "same length!"),
        )
        collision = NameCollision("Acme", "company", pages)
        assert canonical_page(collision).path == Path("a.md")

    def test_unambiguous_when_other_page_is_empty(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "acme.md", uid="u1", name="Acme", type_="company", body="Real content.")
        _write_page(wiki, "acme-dup.md", uid="u2", name="Acme", type_="company", body="")
        collision = scan_name_collisions(wiki)[0]
        assert classify_collision(collision) == "unambiguous"

    def test_unambiguous_when_other_body_is_substring_of_canonical(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        _write_page(
            wiki, "acme.md", uid="u1", name="Acme", type_="company",
            body="Acme is a widget company founded in 1990.",
        )
        _write_page(
            wiki, "acme-dup.md", uid="u2", name="Acme", type_="company",
            body="Acme is a widget company",
        )
        collision = scan_name_collisions(wiki)[0]
        assert classify_collision(collision) == "unambiguous"

    def test_ambiguous_when_two_pages_have_distinct_content(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "acme.md", uid="u1", name="Acme", type_="company", body="Content A.")
        _write_page(
            wiki, "acme-dup.md", uid="u2", name="Acme", type_="company", body="Content B."
        )
        collision = scan_name_collisions(wiki)[0]
        assert classify_collision(collision) == "ambiguous"

    def test_ambiguous_when_type_is_none(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "acme.md", uid="u1", name="Acme", body="")
        _write_page(wiki, "acme-dup.md", uid="u2", name="Acme", body="")
        collision = scan_name_collisions(wiki)[0]
        assert collision.type is None
        assert classify_collision(collision) == "ambiguous"

    def test_ambiguous_when_other_has_frontmatter_key_canonical_lacks(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "acme.md", uid="u1", name="Acme", type_="company", body="Real content.")
        wiki.mkdir(parents=True, exist_ok=True)
        (wiki / "acme-dup.md").write_text(
            "---\nuid: u2\nname: Acme\ntype: company\naccess: confidential\n---\n\n",
            encoding="utf-8",
        )
        collision = scan_name_collisions(wiki)[0]
        assert classify_collision(collision) == "ambiguous"

    def test_ignored_identity_keys_do_not_trigger_ambiguous(self, tmp_path: Path) -> None:
        """uid/name/created/updated/source/aliases differ BY CONSTRUCTION
        between any two pages and must never, alone, force ambiguous."""
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True, exist_ok=True)
        (wiki / "acme.md").write_text(
            "---\nuid: u1\nname: Acme\ntype: company\ncreated: 2026-01-01\n---\n\nReal content.",
            encoding="utf-8",
        )
        (wiki / "acme-dup.md").write_text(
            "---\nuid: u2\nname: Acme\ntype: company\ncreated: 2026-02-02\n"
            "aliases:\n  - AcmeCo\n---\n\n",
            encoding="utf-8",
        )
        collision = scan_name_collisions(wiki)[0]
        assert classify_collision(collision) == "unambiguous"


# ---------------------------------------------------------------------------
# AC2 — the phase, wired into run(), independent of wiki-dedup
# ---------------------------------------------------------------------------


class TestRunNameCollisionPhase:
    def test_phase_records_collision_count(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(
            wiki, "acme.md", uid="u1", name="Acme", type_="company", body="Real content."
        )
        _write_page(wiki, "acme-dup.md", uid="u2", name="Acme", type_="company", body="")
        ctx = _make_ctx(tmp_path, wiki_root=wiki, dry_run=True)
        _run_name_collision_phase(ctx)
        assert len(ctx.run_profile) == 1
        name, _secs, fields = ctx.run_profile[0]
        assert name == "name-collisions"
        assert fields["reason"] == "completed"
        assert fields["collisions"] == 1
        assert fields["unambiguous"] == 1

    def test_skips_when_wiki_root_missing(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)  # wiki_root does not exist on disk
        _run_name_collision_phase(ctx)
        assert ctx.run_profile == []

    def test_disabled_via_config_records_disabled_reason(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        ctx = _make_ctx(tmp_path, wiki_root=wiki, dry_run=True)
        ctx.config = {"librarian": {"name_collision_scan": False}}
        with patch("athenaeum.name_collisions.resolve_name_collisions") as mock_resolve:
            _run_name_collision_phase(ctx)
        mock_resolve.assert_not_called()
        assert ctx.run_profile[0][2] == {"reason": "disabled"}

    def test_failure_is_swallowed_and_still_profiled(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        ctx = _make_ctx(tmp_path, wiki_root=wiki, dry_run=True)
        with patch(
            "athenaeum.name_collisions.resolve_name_collisions",
            side_effect=RuntimeError("boom"),
        ):
            _run_name_collision_phase(ctx)
        assert len(ctx.run_profile) == 1
        assert ctx.run_profile[0][0] == "name-collisions"

    def test_metric_survives_a_wiki_dedup_failure_independence(self, tmp_path: Path) -> None:
        """AC2 independence: a wiki-dedup failure must never suppress this
        phase's already-recorded metric, and vice versa — the two are
        separate try/except/finally blocks, never folded together."""
        wiki = tmp_path / "wiki"
        _write_page(wiki, "acme.md", uid="u1", name="Acme", type_="company", body="")
        _write_page(wiki, "acme-dup.md", uid="u2", name="Acme", type_="company", body="")
        ctx = _make_ctx(tmp_path, wiki_root=wiki, dry_run=True)

        _run_name_collision_phase(ctx)
        with patch(
            "athenaeum.wiki_dedupe.propose_wiki_page_merges",
            side_effect=RuntimeError("wiki-dedup boom"),
        ):
            _run_wiki_dedup_phase(ctx)

        names = [entry[0] for entry in ctx.run_profile]
        assert names == ["name-collisions", "wiki-dedup"]
        nc_fields = ctx.run_profile[0][2]
        assert nc_fields["collisions"] == 1
        assert nc_fields["reason"] == "completed"

    def test_scan_makes_zero_provider_or_llm_calls(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "acme.md", uid="u1", name="Acme", type_="company", body="")
        _write_page(wiki, "acme-dup.md", uid="u2", name="Acme", type_="company", body="")
        ctx = _make_ctx(tmp_path, wiki_root=wiki, dry_run=True)
        with patch(
            "athenaeum.librarian.build_llm_client",
            side_effect=AssertionError("the deterministic scan must never build an LLM client"),
        ) as mock_build:
            _run_name_collision_phase(ctx)
        mock_build.assert_not_called()
        assert ctx.run_profile[0][2]["collisions"] == 1

    def test_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "acme.md", uid="u1", name="Acme", type_="company", body="")
        _write_page(wiki, "acme-dup.md", uid="u2", name="Acme", type_="company", body="")
        ctx = _make_ctx(tmp_path, wiki_root=wiki, dry_run=True)
        _run_name_collision_phase(ctx)
        assert not (wiki / "_pending_merges.md").exists()
        page_names = sorted(p.name for p in wiki.glob("*.md"))
        assert page_names == ["acme-dup.md", "acme.md"]

    def test_entry_lands_in_run_summary_ledger_record(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "acme.md", uid="u1", name="Acme", type_="company", body="")
        _write_page(wiki, "acme-dup.md", uid="u2", name="Acme", type_="company", body="")
        ctx = _make_ctx(tmp_path, wiki_root=wiki, dry_run=True)
        _run_name_collision_phase(ctx)
        record = build_run_summary_ledger_record(ctx.run_profile)
        assert "name-collisions" in record["phases"]
        assert record["phases"]["name-collisions"]["collisions"] == 1


# ---------------------------------------------------------------------------
# Fold-target safety guard: a canonical page not filed at
# wiki_root/slugify(name).md (e.g. an entity-template uid-slug.md page)
# must never be silently auto-merged as a fresh "create-merged" write.
# ---------------------------------------------------------------------------


class TestFoldTargetSafetyGuard:
    def test_entity_template_canonical_is_never_automerged(self, tmp_path: Path) -> None:
        """The unambiguous pair's canonical page is filed as
        ``u1-acme.md`` (an entity-template filename), not the bare
        ``acme.md`` pending_merges' fold machinery would target. Auto-merge
        must be refused (forced ambiguous) rather than creating a spurious
        THIRD page at ``acme.md``."""
        wiki = tmp_path / "wiki"
        _write_page(
            wiki, "u1-acme.md", uid="u1", name="Acme", type_="company",
            body="Acme is a widget company.",
        )
        _write_page(wiki, "u2-acme.md", uid="u2", name="Acme", type_="company", body="")
        init_git_repo(wiki)

        result = resolve_name_collisions(wiki, auto_merge=True, dry_run=False)

        assert result["merged"] == 0
        assert result["ambiguous"] == 1
        # Neither original page was touched, and no THIRD page was created
        # at the bare-slug path the (inapplicable) fold target would use.
        assert (wiki / "u1-acme.md").exists()
        assert (wiki / "u2-acme.md").exists()
        assert not (wiki / "acme.md").exists()


# ---------------------------------------------------------------------------
# AC3 — idempotency
# ---------------------------------------------------------------------------


class TestResolveNameCollisionsIdempotency:
    def test_second_run_over_unchanged_corpus_is_a_no_op(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        # Ambiguous fixture (auto_merge=False path is exercised too) — two
        # distinct-content pages, so nothing auto-resolves; the interesting
        # question is whether the SECOND scan appends a duplicate proposal.
        _write_page(wiki, "acme.md", uid="u1", name="Acme", type_="company", body="Content A.")
        _write_page(
            wiki, "acme-dup.md", uid="u2", name="Acme", type_="company", body="Content B."
        )
        init_git_repo(wiki)

        first = resolve_name_collisions(wiki, auto_merge=False, dry_run=False)
        assert first["collisions"] == 1
        merges_path = wiki / "_pending_merges.md"
        first_text = merges_path.read_text(encoding="utf-8")
        first_blocks = parse_pending_merges(merges_path)
        assert len(first_blocks) == 1

        second = resolve_name_collisions(wiki, auto_merge=False, dry_run=False)
        assert second["collisions"] == 1
        second_text = merges_path.read_text(encoding="utf-8")
        second_blocks = parse_pending_merges(merges_path)

        assert first_text == second_text  # byte-identical: no duplicate block
        assert len(second_blocks) == 1

    def test_second_run_after_automerge_finds_no_collision(self, tmp_path: Path) -> None:
        """The merged case's idempotency is trivial by construction: the
        fold deletes the non-canonical source, so the second scan finds
        zero collisions and does nothing further."""
        wiki = tmp_path / "wiki"
        _write_page(
            wiki, "acme.md", uid="u1", name="Acme", type_="company", body="Real content."
        )
        _write_page(wiki, "acme-dup.md", uid="u2", name="Acme", type_="company", body="")
        init_git_repo(wiki)

        first = resolve_name_collisions(wiki, auto_merge=True, dry_run=False)
        assert first["merged"] == 1
        assert not (wiki / "acme-dup.md").exists()

        corpus_before = (wiki / "acme.md").read_text(encoding="utf-8")
        merges_before = (wiki / "_pending_merges.md").read_text(encoding="utf-8")

        second = resolve_name_collisions(wiki, auto_merge=True, dry_run=False)
        assert second["collisions"] == 0
        assert second["merged"] == 0
        assert (wiki / "acme.md").read_text(encoding="utf-8") == corpus_before
        assert (wiki / "_pending_merges.md").read_text(encoding="utf-8") == merges_before


# ---------------------------------------------------------------------------
# AC4 — reversibility
# ---------------------------------------------------------------------------


class TestResolveNameCollisionsReversibility:
    def test_automerge_produces_discrete_recoverable_commits(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        # Seed the repo with UNRELATED content only, so the target/source
        # pages written below are still UNTRACKED when the merge runs and
        # the provenance-snapshot commit has real new content to capture
        # (mirrors test_merge_fold_write_paths.py's
        # test_fold_lands_as_two_commits_snapshot_then_fold).
        _write_page(wiki, "unrelated.md", uid="u0", name="Unrelated", type_="concept", body="x")
        init_git_repo(wiki)
        seed_messages = _git_log_messages(wiki)
        assert len(seed_messages) == 1

        _write_page(
            wiki, "acme.md", uid="u1", name="Acme", type_="company",
            body="Acme is a widget company.",
        )
        dup_path = _write_page(
            wiki, "acme-dup.md", uid="u2", name="Acme", type_="company", body=""
        )
        original_dup_content = dup_path.read_text(encoding="utf-8")

        result = resolve_name_collisions(wiki, auto_merge=True, dry_run=False)
        assert result["merged"] == 1
        assert not dup_path.exists()

        messages = _git_log_messages(wiki)
        assert len(messages) == 3  # seed + provenance snapshot + fold
        fold_msg, snapshot_msg, seed_msg = messages
        assert seed_msg == seed_messages[0]
        assert "provenance snapshot" in snapshot_msg
        assert "fold" in fold_msg

        # Recover the deleted page's exact pre-fold content via git history.
        fold_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(wiki),
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        recovered = subprocess.run(
            ["git", "show", f"{fold_sha}~1:acme-dup.md"],
            cwd=str(wiki), capture_output=True, text=True, check=True,
        ).stdout
        assert recovered == original_dup_content

        # git revert restores the deleted file to the working tree.
        subprocess.run(
            ["git", "revert", "--no-edit", "HEAD"],
            cwd=str(wiki), capture_output=True, text=True, check=True,
        )
        assert dup_path.exists()
        assert dup_path.read_text(encoding="utf-8") == original_dup_content


# ---------------------------------------------------------------------------
# AC5 — ambiguous collisions surface via the unified decision queue
# ---------------------------------------------------------------------------


class TestResolveNameCollisionsAmbiguousQueue:
    def test_ambiguous_collision_appears_in_pending_decisions_no_merge(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        page_a = _write_page(
            wiki, "acme.md", uid="u1", name="Acme", type_="company", body="Content A."
        )
        page_b = _write_page(
            wiki, "acme-dup.md", uid="u2", name="Acme", type_="company", body="Content B."
        )
        init_git_repo(wiki)

        result = resolve_name_collisions(wiki, auto_merge=True, dry_run=False)
        assert result["ambiguous"] == 1
        assert result["merged"] == 0

        # Both pages are untouched -- no merge occurred despite auto_merge=True.
        assert page_a.exists()
        assert page_b.exists()
        assert "Content A." in page_a.read_text(encoding="utf-8")
        assert "Content B." in page_b.read_text(encoding="utf-8")

        decisions = list_pending_decisions(wiki)
        merge_decisions = [d for d in decisions if d["type"] == "merge"]
        assert len(merge_decisions) == 1
        assert merge_decisions[0]["payload"]["merge_target_name"] == "Acme"


# ---------------------------------------------------------------------------
# AC6 — absorbed name survives as an alias and still resolves
# ---------------------------------------------------------------------------


class TestResolveNameCollisionsAliasSurvival:
    def test_absorbed_name_is_alias_and_still_resolves_via_entity_index(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        canonical_path = _write_page(
            wiki, "acme.md", uid="u1", name="Acme", type_="company",
            body="Acme is a widget company.",
        )
        _write_page(wiki, "acme-corp.md", uid="u2", name="Acme Corp", type_="company", body="")
        # Same lowercase key ("acme corp" vs "acme") would NOT collide by
        # construction -- use an exact-name duplicate under a distinct
        # filename instead, matching this module's own grouping contract.
        dup_path = _write_page(
            wiki, "acme-duplicate.md", uid="u3", name="Acme", type_="company", body=""
        )

        init_git_repo(wiki)
        result = resolve_name_collisions(wiki, auto_merge=True, dry_run=False)
        assert result["merged"] == 1
        assert not dup_path.exists()

        meta, _ = parse_frontmatter(canonical_path.read_text(encoding="utf-8"))
        aliases = meta.get("aliases") or []
        assert "acme-duplicate" in [str(a).lower() for a in aliases]

        index = EntityIndex(wiki)
        hit = index.lookup("acme-duplicate")
        assert hit is not None
        assert hit.path == canonical_path
