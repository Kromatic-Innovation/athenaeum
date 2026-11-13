# SPDX-License-Identifier: Apache-2.0
"""Rejected and archived merge proposals stay dead (issue athenaeum#2514).

Two of this issue's acceptance criteria live here, because both are about
the SAME question -- "has this source set already been ruled on, and does
that ruling still bind?" -- asked by the one proposer that never asked it:

- ``TestNameCollisionScanHonoursPriorRulings`` (AC1) -- the nightly
  name-collision scan (:mod:`athenaeum.name_collisions`) now consults
  :mod:`athenaeum.prior_rulings` with the same
  ``librarian.rejected_merge_cooldown_days`` semantics as the qualified-name
  scan and the wiki-dedupe pair loop. Before this, a human's rejection
  bought exactly one night.
- ``TestArchivedProposalsAreNotReproposed`` (AC2) -- a proposal
  ``athenaeum decisions revalidate --apply`` archived is not re-proposed by
  any proposer unless one of its source pages has changed since the archive.

Rulings are produced through the real ``resolve_name_collisions`` ->
``resolve_merge`` -> ``ingest_resolved_merges`` / ``revalidate_pending_merges``
path, so the archive format under test is the one production writes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.name_collisions import resolve_name_collisions
from athenaeum.name_structure import propose_qualified_name_merges
from athenaeum.pending_merges import (
    SOURCE_DIGEST_LINE_PREFIX,
    ingest_resolved_merges,
    parse_pending_merges,
    resolve_merge,
    revalidate_pending_merges,
    source_content_digest,
)
from athenaeum.prior_rulings import load_prior_rulings, match_rulings

#: A confidence floor the name-collision scan's own proposals always fail:
#: `_content_comparator_confidence` scores 0.0 without an LLM client, and
#: these tests never pass one.
LOW_CONFIDENCE_GATE: dict[str, object] = {"librarian": {"min_merge_confidence": 0.5}}


def _page(wiki: Path, filename: str, *, name: str, body: str = "Some content.\n") -> Path:
    wiki.mkdir(parents=True, exist_ok=True)
    path = wiki / filename
    path.write_text(
        f"---\nname: {name}\ntype: project\n---\n{body}", encoding="utf-8"
    )
    return path


@pytest.fixture
def colliding_wiki(tmp_path: Path) -> Path:
    """Two live pages sharing one ``name:`` -- the collision scan's input."""
    wiki = tmp_path / "wiki"
    _page(wiki, "orca.md", name="ORCA", body="First writeup.\n")
    _page(wiki, "orca-2.md", name="ORCA", body="Second, different writeup.\n")
    return wiki


def _unresolved(wiki: Path) -> list:
    return [pm for pm in parse_pending_merges(wiki / "_pending_merges.md") if not pm.resolved]


def _scan(wiki: Path, config: dict | None = None) -> dict:
    return resolve_name_collisions(
        wiki, auto_merge=False, dry_run=False, config=config
    )


class TestNameCollisionScanHonoursPriorRulings:
    """AC1."""

    def test_rejected_pair_is_not_reproposed_on_the_next_run(
        self, colliding_wiki: Path
    ) -> None:
        wiki = colliding_wiki
        merges = wiki / "_pending_merges.md"
        assert _scan(wiki)["queued"] == 1
        proposal = _unresolved(wiki)[0]

        assert resolve_merge(
            merges,
            proposal.id,
            "reject",
            note="two genuinely different programmes",
            wiki_root=wiki,
            answered_at="2026-11-12T09:00:00Z",
        )["ok"]
        # The resolved block leaves the live sidecar on this tick, exactly
        # as it does nightly -- which is precisely what used to free the
        # scan to re-mint the same proposal.
        ingest_resolved_merges(merges)
        assert _unresolved(wiki) == []

        counts = _scan(wiki)
        assert counts["ruled_out"] == 1
        assert counts["queued"] == 0
        assert _unresolved(wiki) == []

    def test_dry_run_reports_the_ruled_out_group_without_writing(
        self, colliding_wiki: Path
    ) -> None:
        wiki = colliding_wiki
        merges = wiki / "_pending_merges.md"
        assert _scan(wiki)["queued"] == 1
        assert resolve_merge(
            merges,
            _unresolved(wiki)[0].id,
            "reject",
            note="no",
            wiki_root=wiki,
            answered_at="2026-11-12T09:00:00Z",
        )["ok"]
        ingest_resolved_merges(merges)

        counts = resolve_name_collisions(wiki, auto_merge=False, dry_run=True)
        assert counts["ruled_out"] == 1
        assert counts["queued"] == 0
        assert counts["ambiguous"] == 0
        assert _unresolved(wiki) == []

    def test_pair_is_reproposed_once_the_cooldown_lapses(
        self, colliding_wiki: Path
    ) -> None:
        wiki = colliding_wiki
        merges = wiki / "_pending_merges.md"
        assert _scan(wiki)["queued"] == 1
        assert resolve_merge(
            merges,
            _unresolved(wiki)[0].id,
            "reject",
            note="not the same",
            wiki_root=wiki,
            answered_at="2020-01-01T00:00:00Z",
        )["ok"]
        ingest_resolved_merges(merges)

        cfg = {"librarian": {"rejected_merge_cooldown_days": 7}}
        # Still binding inside the window...
        assert _scan(wiki, {"librarian": {"rejected_merge_cooldown_days": 0}})[
            "ruled_out"
        ] == 1
        # ...and a fresh question once it has lapsed, with the earlier
        # ruling named in the rationale the reviewer reads.
        counts = _scan(wiki, cfg)
        assert counts["ruled_out"] == 0
        assert counts["queued"] == 1
        (reproposed,) = _unresolved(wiki)
        assert "previously rejected by a human on 2020-01-01" in reproposed.rationale

    def test_approved_pair_is_never_reproposed(self, colliding_wiki: Path) -> None:
        wiki = colliding_wiki
        merges = wiki / "_pending_merges.md"
        assert _scan(wiki)["queued"] == 1
        proposal = _unresolved(wiki)[0]
        # Record the approval without running the fold write path: this
        # test is about the SUPPRESSION record, and a fold would delete the
        # very pages the next scan must still see.
        from athenaeum.pending_merges import _rewrite_block_resolved, _split_blocks

        text = merges.read_text(encoding="utf-8")
        blocks = [
            (
                _rewrite_block_resolved(
                    b, "approve", "ok", answered_at="2020-01-01T00:00:00Z"
                )
                if proposal.raw_block.splitlines()[0] in b
                else b
            )
            for b in _split_blocks(text)
        ]
        merges.write_text(
            "# Pending Merges\n\n" + "\n\n---\n\n".join(blocks) + "\n", encoding="utf-8"
        )
        ingest_resolved_merges(merges)

        # An approval does not decay, so even a generous cooldown leaves it
        # in force.
        counts = _scan(wiki, {"librarian": {"rejected_merge_cooldown_days": 1}})
        assert counts["ruled_out"] == 1
        assert _unresolved(wiki) == []


class TestArchivedProposalsAreNotReproposed:
    """AC2 -- both branches."""

    def _archive(self, wiki: Path, floor: float = 0.5) -> None:
        result = revalidate_pending_merges(
            wiki / "_pending_merges.md",
            config={"librarian": {"min_merge_confidence": floor}},
            apply=True,
        )
        assert len(result.retired) == 1, result
        assert result.applied

    def test_archive_records_a_source_digest(self, colliding_wiki: Path) -> None:
        wiki = colliding_wiki
        assert _scan(wiki)["queued"] == 1
        sources = _unresolved(wiki)[0].sources
        self._archive(wiki)

        (archived,) = parse_pending_merges(wiki / "_pending_merges_archive.md")
        assert SOURCE_DIGEST_LINE_PREFIX in (
            wiki / "_pending_merges_archive.md"
        ).read_text(encoding="utf-8")
        assert archived.source_digest == source_content_digest(sources, wiki_root=wiki)
        assert archived.archived_at
        assert archived.decision == ""

    def test_unchanged_sources_are_not_reproposed(self, colliding_wiki: Path) -> None:
        wiki = colliding_wiki
        assert _scan(wiki)["queued"] == 1
        self._archive(wiki)
        assert _unresolved(wiki) == []

        counts = _scan(wiki)
        assert counts["ruled_out"] == 1
        assert counts["queued"] == 0
        assert _unresolved(wiki) == []

    def test_a_changed_source_page_reopens_the_question(
        self, colliding_wiki: Path
    ) -> None:
        wiki = colliding_wiki
        assert _scan(wiki)["queued"] == 1
        self._archive(wiki)

        # An operator edits one of the two pages. That -- and only that --
        # makes this a new question.
        _page(wiki, "orca-2.md", name="ORCA", body="Rewritten, with new facts.\n")

        counts = _scan(wiki)
        assert counts["ruled_out"] == 0
        assert counts["queued"] == 1
        (reproposed,) = _unresolved(wiki)
        assert "previously archived as stale" in reproposed.rationale
        assert "by a human" not in reproposed.rationale

    def test_archived_ruling_also_binds_the_qualified_name_scan(
        self, tmp_path: Path
    ) -> None:
        """AC2 is enforced in `prior_rulings`, so every proposer inherits it."""
        wiki = tmp_path / "wiki"
        _page(wiki, "orca.md", name="ORCA")
        qualified = _page(wiki, "orca-coaching.md", name="ORCA (coaching)")
        assert propose_qualified_name_merges(wiki)["queued"] == 1
        # The qualified-name scan stores its own (higher) fixed confidence,
        # so the floor that retires it has to clear that instead.
        self._archive(wiki, floor=1.0)
        assert _unresolved(wiki) == []

        assert propose_qualified_name_merges(wiki)["ruled_out"] == 1
        assert _unresolved(wiki) == []

        # ...until a source changes.
        qualified.write_text(
            qualified.read_text(encoding="utf-8") + "\nNew material.\n",
            encoding="utf-8",
        )
        assert propose_qualified_name_merges(wiki)["queued"] == 1

    def test_archived_block_with_no_digest_keeps_binding(
        self, colliding_wiki: Path
    ) -> None:
        """A block archived before athenaeum#2514 records no digest, so no
        change can be proven -- the archive keeps binding rather than
        re-opening on the first run after deploy."""
        wiki = colliding_wiki
        assert _scan(wiki)["queued"] == 1
        self._archive(wiki)
        archive_path = wiki / "_pending_merges_archive.md"
        archive_path.write_text(
            "\n".join(
                line
                for line in archive_path.read_text(encoding="utf-8").splitlines()
                if not line.startswith(SOURCE_DIGEST_LINE_PREFIX)
            )
            + "\n",
            encoding="utf-8",
        )
        _page(wiki, "orca-2.md", name="ORCA", body="Rewritten anyway.\n")

        (ruling,) = load_prior_rulings(wiki)
        assert ruling.decision == "archived"
        assert ruling.sources_changed is False
        assert _scan(wiki)["ruled_out"] == 1

    def test_every_archive_writer_stamps_a_digest(self, colliding_wiki: Path) -> None:
        """Review finding: `_parse_block` reads ``**Retired**:`` from ANY
        writer, so an archive written without a digest would mint a ruling
        nothing can prove stale -- permanent suppression with no recovery.
        Covers the OTHER retirement path (`withdraw_pending_merges_for_
        retired_pages`); the sweep's two dispositions are covered above."""
        from athenaeum.pending_merges import withdraw_pending_merges_for_retired_pages

        wiki = colliding_wiki
        assert _scan(wiki)["queued"] == 1
        sources = _unresolved(wiki)[0].sources

        result = withdraw_pending_merges_for_retired_pages(
            wiki / "_pending_merges.md",
            [wiki / "orca-2.md"],
            reason="operator retirement",
            apply=True,
        )
        assert len(result.withdrawn) == 1

        (archived,) = parse_pending_merges(wiki / "_pending_merges_archive.md")
        assert archived.source_digest == source_content_digest(sources, wiki_root=wiki)
        (ruling,) = load_prior_rulings(wiki)
        assert ruling.decision == "archived"
        assert ruling.sources_changed is False
        # ...and it stays RECOVERABLE: restoring the page is a source change,
        # so the pair becomes askable again rather than being suppressed by
        # an archive nobody can disprove.
        _page(wiki, "orca-2.md", name="ORCA", body="Restored, with new facts.\n")
        assert load_prior_rulings(wiki)[0].sources_changed is True

    def test_match_rulings_reports_a_lapsed_archive_as_expired(
        self, colliding_wiki: Path
    ) -> None:
        wiki = colliding_wiki
        assert _scan(wiki)["queued"] == 1
        sources = _unresolved(wiki)[0].sources
        self._archive(wiki)

        assert match_rulings(load_prior_rulings(wiki), sources).in_force is not None
        _page(wiki, "orca.md", name="ORCA", body="Edited.\n")
        matched = match_rulings(load_prior_rulings(wiki), sources)
        assert matched.in_force is None
        assert matched.expired is not None
        assert matched.expired.decision == "archived"
