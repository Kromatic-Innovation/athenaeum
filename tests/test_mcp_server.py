"""Tests for the MCP memory server module."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from athenaeum.mcp_server import (
    _recall_metadata_lines,
    _score_page,
    _snippet,
    _tokenize_query,
    recall_search,
    remember_write,
)

# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------


class TestTokenizeQuery:
    def test_basic_split(self) -> None:
        assert _tokenize_query("hello world") == ["hello", "world"]

    def test_filters_short_tokens(self) -> None:
        assert _tokenize_query("a is the go") == ["is", "the", "go"]

    def test_lowercases(self) -> None:
        assert _tokenize_query("Acme Corp") == ["acme", "corp"]

    def test_splits_on_punctuation(self) -> None:
        assert _tokenize_query("foo-bar/baz") == ["foo", "bar", "baz"]

    def test_empty_string(self) -> None:
        assert _tokenize_query("") == []


# ---------------------------------------------------------------------------
# Score page
# ---------------------------------------------------------------------------


class TestScorePage:
    def test_frontmatter_match_weighted(self) -> None:
        score = _score_page(
            ["acme"], {"name": "Acme Corp", "tags": ["fintech"]}, "Some body text"
        )
        assert score >= 3.0  # frontmatter hit

    def test_body_only_match(self) -> None:
        score = _score_page(["pipeline"], {}, "The pipeline processes raw files")
        assert score == 1.0

    def test_both_match(self) -> None:
        score = _score_page(["acme"], {"name": "Acme Corp"}, "Acme is a company")
        assert score == 4.0  # 3 (frontmatter) + 1 (body)

    def test_no_match(self) -> None:
        score = _score_page(["xyz"], {"name": "Acme"}, "No match here")
        assert score == 0.0

    def test_empty_tokens(self) -> None:
        assert _score_page([], {"name": "Acme"}, "body") == 0.0

    def test_list_tags_scored(self) -> None:
        score = _score_page(["fintech"], {"tags": ["fintech", "client"]}, "body")
        assert score >= 3.0


# ---------------------------------------------------------------------------
# Snippet
# ---------------------------------------------------------------------------


class TestSnippet:
    def test_returns_context_around_match(self) -> None:
        body = "x" * 200 + " KEYWORD " + "y" * 200
        snip = _snippet(body, ["keyword"], max_chars=100)
        assert "keyword" in snip.lower()

    def test_returns_start_when_no_match(self) -> None:
        body = "abcdef" * 50
        snip = _snippet(body, ["zzz"], max_chars=20)
        assert snip.startswith("abcdef")

    def test_short_body(self) -> None:
        snip = _snippet("short", ["short"])
        assert snip == "short"

    def test_match_near_start_trims_tail(self) -> None:
        # Match is early in the body; snippet should keep the match and
        # append an ellipsis for the trimmed tail, not drop the match.
        body = "The KEYWORD appears here. " + "tail " * 200
        snip = _snippet(body, ["keyword"], max_chars=60)
        assert "keyword" in snip.lower()
        assert snip.endswith("…") or len(snip) <= 63

    def test_match_near_end_prefixes_ellipsis(self) -> None:
        # Match is at the end; snippet should prefix an ellipsis so the
        # reader sees the match, not the irrelevant prefix. Uses a
        # realistic max_chars (>=80) so the window can reach the match —
        # the snippet algorithm centers ~80 chars before best_pos, so a
        # smaller max_chars would clip the window before the match.
        body = "lead " * 200 + " KEYWORD tail."
        snip = _snippet(body, ["keyword"], max_chars=200)
        assert "keyword" in snip.lower()
        assert snip.startswith("…")


# ---------------------------------------------------------------------------
# Recall
# ---------------------------------------------------------------------------


class TestRecall:
    def test_recall_finds_matching_page(self, wiki_dir: Path) -> None:
        result = recall_search(wiki_dir, "Acme")
        assert "Acme Corp" in result
        assert "score:" in result

    def test_recall_no_match(self, wiki_dir: Path) -> None:
        result = recall_search(wiki_dir, "xyznonexistent")
        assert "No wiki pages matched" in result

    def test_recall_missing_dir(self, tmp_path: Path) -> None:
        result = recall_search(tmp_path / "nonexistent", "test")
        assert "not found" in result

    def test_recall_short_query(self, wiki_dir: Path) -> None:
        result = recall_search(wiki_dir, "a")
        assert "too short" in result.lower()

    def test_recall_skips_underscore_files(self, wiki_dir: Path) -> None:
        # The fixture contains _index.md which must be skipped by the
        # backend's `startswith("_")` guard. Direct assertion — previous
        # form was `"score:" not in result or "_index" not in result`,
        # which passes trivially when "No wiki pages matched" is returned
        # (the `a or b` shape masked the actual behavior being tested).
        # MEMORY.md is intentionally not skipped by the search backend —
        # only underscore-prefixed files are filtered. EntityIndex (a
        # different consumer) skips MEMORY.md separately.
        result = recall_search(wiki_dir, "Index")
        assert "_index.md" not in result

    def test_recall_top_k(self, wiki_dir: Path) -> None:
        result = recall_search(wiki_dir, "knowledge architecture", top_k=1)
        # After the v0.2.1 backend unification the keyword path no longer
        # reports total-matched; top_k is enforced via a single result block.
        assert result.count("### ") == 1
        assert "Found 1 matching pages" in result

    def test_recall_shows_tags(self, wiki_dir: Path) -> None:
        result = recall_search(wiki_dir, "Acme fintech")
        assert "fintech" in result

    def test_recall_top_k_capped(self, wiki_dir: Path) -> None:
        # top_k > _MAX_TOP_K should be silently capped
        result = recall_search(wiki_dir, "Acme", top_k=1_000_000)
        assert "Acme Corp" in result


# ---------------------------------------------------------------------------
# Recall type filter (issue athenaeum#964)
# ---------------------------------------------------------------------------


class TestRecallTypeFilter:
    def test_no_filter_is_byte_identical(self, wiki_dir: Path) -> None:
        before = recall_search(wiki_dir, "Acme fintech")
        after = recall_search(wiki_dir, "Acme fintech", type_filter=None)
        assert before == after

    def test_matching_type_filters_in(self, wiki_dir: Path) -> None:
        result = recall_search(wiki_dir, "Acme fintech", type_filter="company")
        assert "Acme Corp" in result

    def test_non_matching_type_filters_out(self, wiki_dir: Path) -> None:
        result = recall_search(wiki_dir, "Acme fintech", type_filter="person")
        assert "Acme Corp" not in result

    def test_unrecognized_type_names_known_classes_not_silent(
        self, wiki_dir: Path
    ) -> None:
        result = recall_search(wiki_dir, "Acme fintech", type_filter="no-such-class")
        assert "No wiki pages matched" in result
        assert "not a recognized entity class" in result
        # The deployment's real classes (company/feedback, from the wiki_dir
        # fixture) are named so a typo is diagnosable from the response alone.
        assert "company" in result

    def test_unrecognized_type_is_not_an_error(self, wiki_dir: Path) -> None:
        # Must return a string result, never raise.
        result = recall_search(wiki_dir, "Acme fintech", type_filter="bogus")
        assert isinstance(result, str)

    def test_hit_includes_uid_and_type(self, wiki_dir: Path) -> None:
        result = recall_search(wiki_dir, "Acme fintech")
        assert "**Uid:** a1b2c3d4" in result
        assert "**Type:** company" in result

    def test_hit_includes_outbound_links_when_present(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "a.md").write_text(
            "---\nuid: u1\ntype: person\nname: Alice\n---\n\n"
            "Alice knows [[bob-page]] and cites [[carol-page|Carol]].\n"
        )
        result = recall_search(wiki, "Alice")
        assert "**Links:** bob-page, carol-page" in result

    def test_hit_omits_links_line_when_none(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "a.md").write_text(
            "---\nuid: u1\ntype: person\nname: Alice\n---\n\nNo links here.\n"
        )
        result = recall_search(wiki, "Alice")
        assert "**Links:**" not in result


# ---------------------------------------------------------------------------
# Recall provenance/context header (issue athenaeum#325)
# ---------------------------------------------------------------------------


class TestRecallMetadataLines:
    """Unit tests for the per-hit metadata header builder."""

    def test_full_header_single_line(self) -> None:
        lines = _recall_metadata_lines(
            {
                "source_type": "user-stated",
                "source_ref": "user-stated:2026-04-10",
                "updated": "2026-06-30",
                "valid_from": "2026-04-01",
            }
        )
        assert lines == [
            "**Source:** user-stated (2026-04-10) · "
            "**Updated:** 2026-06-30 · **Valid:** 2026-04-01 → open"
        ]

    def test_valid_until_only_uses_open_lower_bound(self) -> None:
        # (a) a page with valid_until shows its window, open on the missing bound.
        lines = _recall_metadata_lines({"valid_until": "2026-05-01"})
        assert lines == ["**Valid:** open → 2026-05-01"]

    def test_source_from_created_when_ref_dateless(self) -> None:
        # (d) source_type + created date renders the Source line.
        lines = _recall_metadata_lines(
            {"source_type": "document", "created": "2026-02-14T09:00:00"}
        )
        assert lines == ["**Source:** document (2026-02-14)"]

    def test_inferred_source_omitted(self) -> None:
        # Default source_type ("inferred") must not render a Source segment.
        lines = _recall_metadata_lines({"source_type": "inferred"})
        assert lines == []

    def test_no_fields_yields_no_lines(self) -> None:
        assert _recall_metadata_lines({}) == []

    def test_contradiction_flagged_status(self) -> None:
        # (b) a contradiction-flagged page shows the Status line.
        lines = _recall_metadata_lines({"status": "contradiction-flagged"})
        assert lines == [
            "**Status:** contradiction-flagged (see _pending_questions.md)"
        ]

    def test_contradictions_detected_flag_triggers_status(self) -> None:
        lines = _recall_metadata_lines({"contradictions_detected": True})
        assert lines == [
            "**Status:** contradiction-flagged (see _pending_questions.md)"
        ]

    def test_status_capped_at_two_lines(self) -> None:
        lines = _recall_metadata_lines(
            {
                "source_type": "external",
                "source_ref": "https://example.com",
                "updated": "2026-06-30",
                "valid_from": "2026-01-01",
                "valid_until": "2026-12-31",
                "status": "contradiction-flagged",
            }
        )
        assert len(lines) == 2
        assert lines[1].startswith("**Status:**")

    def test_out_of_vocab_source_type_omitted(self) -> None:
        # A typo'd / out-of-vocabulary source_type is not in SOURCE_TYPES and
        # must omit the Source segment, same as the "inferred" default. Pins
        # the "absent/typo'd -> omit" contract the PR body claims.
        assert _recall_metadata_lines({"source_type": "usr-stated"}) == []
        assert _recall_metadata_lines(
            {"source_type": "usr-stated", "source_ref": "usr-stated:2026-04-10"}
        ) == []


class TestRecallHeaderRendering:
    """Integration tests exercising the rendered recall output (athenaeum#325)."""

    def _wiki(self, tmp_path: Path) -> Path:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        return wiki

    def test_valid_until_window_rendered(self, tmp_path: Path) -> None:
        wiki = self._wiki(tmp_path)
        (wiki / "deploy_target.md").write_text(
            "---\n"
            "name: Deploy target\n"
            "tags:\n  - infra\n"
            "valid_from: '2026-04-01'\n"
            "valid_until: '2026-12-31'\n"
            "---\n\n"
            "The deploy target is the staging cluster.\n"
        )
        result = recall_search(wiki, "deploy target")
        assert "**Valid:** 2026-04-01 → 2026-12-31" in result

    def test_contradiction_flagged_status_rendered(self, tmp_path: Path) -> None:
        wiki = self._wiki(tmp_path)
        (wiki / "disputed_fact.md").write_text(
            "---\n"
            "name: Disputed fact\n"
            "tags:\n  - contested\n"
            "status: contradiction-flagged\n"
            "---\n\n"
            "This disputed fact has two conflicting sides.\n"
        )
        result = recall_search(wiki, "disputed fact")
        assert "**Status:** contradiction-flagged (see _pending_questions.md)" in result

    def test_plain_page_no_spurious_metadata(self, tmp_path: Path) -> None:
        # (c) a plain page renders at most one extra metadata line and no
        # spurious blank Source:/Valid: segments.
        wiki = self._wiki(tmp_path)
        (wiki / "plain_note.md").write_text(
            "---\n"
            "name: Plain note\n"
            "tags:\n  - misc\n"
            "updated: '2024-04-06'\n"
            "---\n\n"
            "A plain note about the widget pipeline.\n"
        )
        result = recall_search(wiki, "widget pipeline")
        assert "**Updated:** 2024-04-06" in result
        assert "**Source:**" not in result
        assert "**Valid:**" not in result
        assert "**Status:**" not in result

    def test_source_line_rendered(self, tmp_path: Path) -> None:
        # (d) a page with source_type + created shows the Source line.
        wiki = self._wiki(tmp_path)
        (wiki / "sourced_fact.md").write_text(
            "---\n"
            "name: Sourced fact\n"
            "tags:\n  - traced\n"
            "source_type: user-stated\n"
            "source_ref: 'user-stated:2026-04-10'\n"
            "---\n\n"
            "A user-stated fact about the roadmap.\n"
        )
        result = recall_search(wiki, "roadmap fact")
        assert "**Source:** user-stated (2026-04-10)" in result

    def test_bare_page_matches_pre_325_shape(self, tmp_path: Path) -> None:
        # A page with none of source/updated/valid/status renders the original
        # Tags-then-blank-then-snippet shape (no blank athenaeum#325 metadata line
        # inserted) -- with the issue athenaeum#964 Uid/Type lines (always
        # rendered, unlike the omit-at-default athenaeum#325 header) directly
        # after Tags. Issue athenaeum#718's always-rendered Tier line used to
        # follow Type; issue athenaeum#1514 removed it with the tier
        # vocabulary, so Type is now the last header on a bare page (the
        # Scope segment that shared its line stays omit-at-default).
        wiki = self._wiki(tmp_path)
        (wiki / "terse.md").write_text(
            "---\n"
            "name: Terse page\n"
            "tags:\n  - plain\n"
            "---\n\n"
            "A terse page about migrations.\n"
        )
        result = recall_search(wiki, "migrations terse")
        assert "**Tags:** plain\n**Uid:** —\n**Type:** —\n\n" in result
        assert "**Tier:**" not in result

    def test_withheld_contested_page_leaks_no_status(self, tmp_path: Path) -> None:
        # Safety lock (athenaeum#325 raison d'etre): a restricted caller must not see a
        # withheld page's Status line. The Layer-C fail-closed `continue` runs
        # BEFORE the metadata header is built; this pins that ordering so a
        # future refactor moving the header build above the withhold cannot
        # leak a contested (or any) page's existence to an unauthorized caller.
        wiki = self._wiki(tmp_path)
        (wiki / "secret_dispute.md").write_text(
            "---\n"
            "name: Secret dispute\n"
            "tags:\n  - contested\n"
            "status: contradiction-flagged\n"
            "---\n\n"
            "A restricted, disputed fact about the secret roadmap.\n"
        )
        # Untagged for access -> empty grant set -> fail-closed withheld from
        # any restricted (non-owner) caller.
        result = recall_search(
            wiki, "secret roadmap dispute", caller_audience={"secondary"}
        )
        assert "**Status:**" not in result
        assert "Secret dispute" not in result


# ---------------------------------------------------------------------------
# Tier + push budget (issue athenaeum#718)
# ---------------------------------------------------------------------------


class TestRecallTierAndPushBudget:
    def _wiki(self, tmp_path: Path) -> Path:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        return wiki

    def test_no_tier_segment_for_a_guideline(self, tmp_path: Path) -> None:
        """Issue athenaeum#718 rendered `**Tier:** hot` on a guideline-class
        page; issue athenaeum#1514 retired the vocabulary, so the segment is
        gone. Both former values are asserted absent (here and below) rather
        than just one, because the removal must not leave the header
        rendering one tier and suppressing the other.
        """
        wiki = self._wiki(tmp_path)
        (wiki / "rule.md").write_text(
            "---\nname: A rule\ntype: principle\nmemory_class: guideline\n---\n\n"
            "Always validate input.\n"
        )
        result = recall_search(wiki, "validate input")
        assert "A rule" in result, "the page must still be found"
        assert "**Tier:**" not in result

    def test_no_tier_segment_for_an_entity(self, tmp_path: Path) -> None:
        wiki = self._wiki(tmp_path)
        (wiki / "p.md").write_text(
            "---\nuid: u1\nname: Alice\ntype: person\n---\n\nAlice works here.\n"
        )
        result = recall_search(wiki, "Alice works")
        assert "Alice" in result, "the page must still be found"
        assert "**Tier:**" not in result

    def test_scope_segment_appears_only_with_session_scope(self, tmp_path: Path) -> None:
        wiki = self._wiki(tmp_path)
        (wiki / "p.md").write_text(
            "---\nname: Scoped note\ntype: feedback\nclaimed_scope: org/team\n---\n\n"
            "A scoped note about the rollout.\n"
        )
        without = recall_search(wiki, "scoped rollout")
        assert "**Scope:**" not in without

        withit = recall_search(wiki, "scoped rollout", session_scope="org/team")
        assert "**Scope:** equal" in withit

    def test_unprompted_default_false_is_byte_identical(self, tmp_path: Path) -> None:
        wiki = self._wiki(tmp_path)
        (wiki / "p.md").write_text(
            "---\nname: Plain\ntype: feedback\n---\n\nA plain note about widgets.\n"
        )
        default_call = recall_search(wiki, "plain widgets")
        explicit_false = recall_search(wiki, "plain widgets", unprompted=False)
        assert default_call == explicit_false

    def test_unprompted_includes_warm_tier(self, tmp_path: Path) -> None:
        """Issue athenaeum#1353: `unprompted=True` no longer restricts to the
        `hot` retrieval-cost tier -- the tier-weighted `push_score` formula
        that enforced that gate had no production caller and was deleted.
        A `warm`-by-class-default page (an `entity`) is now included on
        `unprompted=True` exactly as it is on an explicit `recall` call,
        the opposite of this test's pre-athenaeum#1353 assertion."""
        wiki = self._wiki(tmp_path)
        # entity -> warm by class default.
        (wiki / "p.md").write_text(
            "---\nuid: u1\nname: Alice\ntype: person\n---\n\nAlice knows about widgets.\n"
        )
        prompted = recall_search(wiki, "Alice widgets")
        assert "Alice" in prompted

        unprompted = recall_search(wiki, "Alice widgets", unprompted=True)
        assert "Alice" in unprompted

    def test_unprompted_includes_hot_tier(self, tmp_path: Path) -> None:
        wiki = self._wiki(tmp_path)
        (wiki / "rule.md").write_text(
            "---\nname: A rule\ntype: principle\nmemory_class: guideline\n---\n\n"
            "Always validate widgets on input.\n"
        )
        result = recall_search(wiki, "validate widgets", unprompted=True)
        assert "A rule" in result

    @staticmethod
    def _extract_single_block(result: str) -> str:
        """Extract the rendered block for the (only) hit in a 1-result
        `recall_search` output -- the EXACT text a caller's context budget
        actually pays for. This must be the quantity any boundary assertion
        measures against; measuring a sub-component (e.g. just the snippet)
        can pass while the real emitted content overruns the budget."""
        prefix = "Found 1 matching pages:\n\n### 1. "
        assert result.startswith(prefix), result
        return result[len(prefix) :]

    def test_unprompted_enforces_token_budget_at_the_boundary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum.push_metrics import estimate_tokens

        wiki = self._wiki(tmp_path)
        query = "validate widgets shipping"
        body = "Always validate widgets thoroughly before shipping any release.\n"
        (wiki / "rule1.md").write_text(
            "---\nname: Rule widget one\ntype: principle\nmemory_class: guideline\n---\n\n"
            + body
        )
        # A generous budget admits the one hot hit.
        monkeypatch.delenv("ATHENAEUM_PUSH_TOKEN_BUDGET", raising=False)
        admitted = recall_search(wiki, query, unprompted=True)
        assert "Rule widget one" in admitted

        # Compute the EXACT token cost of the FULLY RENDERED block --
        # path/tags/uid/type/meta/tier-scope/links headers plus the snippet,
        # the same quantity the greedy budget-pack in `_recall_via_backend`
        # must budget against (issue athenaeum#718: metering only the
        # snippet undercounts and lets the budget be consistently
        # overrun -- see
        # `test_unprompted_budget_meters_full_block_not_just_snippet` below
        # for the regression case that would have caught it).
        actual_block = self._extract_single_block(admitted)
        block_tokens = estimate_tokens(actual_block)
        assert block_tokens > 0

        # Exactly at the rendered block's token cost: still admitted (`<=` budget).
        monkeypatch.setenv("ATHENAEUM_PUSH_TOKEN_BUDGET", str(block_tokens))
        at_boundary = recall_search(wiki, query, unprompted=True)
        assert "Rule widget one" in at_boundary

        # One token under: excluded -- the boundary itself.
        monkeypatch.setenv("ATHENAEUM_PUSH_TOKEN_BUDGET", str(block_tokens - 1))
        under_boundary = recall_search(wiki, query, unprompted=True)
        assert "No wiki pages matched" in under_boundary

    def test_unprompted_budget_meters_full_block_not_just_snippet(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression test for the metering bug: a page with a SHORT snippet
        but a LONG rendered header (many tags, a claimed_scope, source
        provenance) must have its header overhead counted against the
        budget. A `tokens=estimate_tokens(snip)`-only implementation would
        underestimate this item's cost and let it through at a budget too
        small for what is actually emitted -- this test sets the budget to
        exactly the snippet-only estimate (too small for the real block) and
        asserts the hit is EXCLUDED, which only holds when the full
        rendered block is what gets metered.
        """
        from athenaeum.push_metrics import estimate_tokens

        wiki = self._wiki(tmp_path)
        query = "widgets"
        body = "Widgets ship.\n"  # deliberately tiny snippet
        many_tags = ", ".join(f"tag-{i}" for i in range(30))  # heavy header overhead
        (wiki / "heavy.md").write_text(
            "---\n"
            "name: Heavy header widget page\n"
            "type: principle\n"
            "memory_class: guideline\n"
            f"tags: [{many_tags}]\n"
            "claimed_scope: org/team/project/subproject\n"
            "source_type: user-stated\n"
            "source_ref: 'user-stated:2026-04-10'\n"
            "---\n\n" + body
        )
        monkeypatch.delenv("ATHENAEUM_PUSH_TOKEN_BUDGET", raising=False)
        admitted = recall_search(
            wiki, query, unprompted=True, session_scope="org/team/project/subproject"
        )
        assert "Heavy header widget page" in admitted

        actual_block = self._extract_single_block(admitted)
        block_tokens = estimate_tokens(actual_block)
        snippet_only_tokens = estimate_tokens(_snippet(body, _tokenize_query(query)))

        # The header overhead this page carries (tags/scope/source/tier)
        # must dwarf the snippet alone -- if this assertion ever fails, the
        # page fixture no longer exercises the bug this test guards against.
        assert block_tokens > snippet_only_tokens * 2

        # Budget set to the snippet-only figure: too small for the real
        # block. Correct behavior is EXCLUSION -- if the implementation
        # regresses to metering `snip` alone, this would incorrectly admit
        # the hit (snippet-only cost fits the budget) even though the real
        # emitted content is more than double that.
        monkeypatch.setenv("ATHENAEUM_PUSH_TOKEN_BUDGET", str(snippet_only_tokens))
        under_real_cost = recall_search(
            wiki, query, unprompted=True, session_scope="org/team/project/subproject"
        )
        assert "No wiki pages matched" in under_real_cost


# ---------------------------------------------------------------------------
# Remember
# ---------------------------------------------------------------------------


class TestRemember:
    def test_writes_raw_file(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        raw.mkdir()
        result = remember_write(raw, "Test observation about Acme")
        assert result.startswith("Saved to")
        # Verify file exists and has content. With no `sources` kwarg
        # the server stamps a default-inferred-source frontmatter
        # block (issue athenaeum#90) and the original body lands underneath.
        files = list((raw / "claude-session").glob("*.md"))
        assert len(files) == 1
        text = files[0].read_text()
        assert "source: claude:inferred" in text
        assert "Test observation about Acme" in text

    def test_custom_source(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        raw.mkdir()
        result = remember_write(raw, "content", source="manual")
        assert "Saved to" in result
        assert (raw / "manual").is_dir()

    def test_filename_format(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        raw.mkdir()
        remember_write(raw, "content")
        files = list((raw / "claude-session").glob("*.md"))
        # filename: 20260416T123456Z-abcd1234.md
        import re

        assert re.match(r"\d{8}T\d{6}Z-[0-9a-f]{8}\.md", files[0].name)

    def test_rejects_empty_source(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        raw.mkdir()
        result = remember_write(raw, "content", source="!!!")
        assert "Error" in result

    def test_sanitizes_path_traversal(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        raw.mkdir()
        # "../../../etc" is sanitized to "etc" (dots and slashes stripped),
        # so it writes safely to raw/etc/ — the sanitization IS the defense
        result = remember_write(raw, "content", source="../../../etc")
        assert "Saved" in result
        assert (raw / "etc").is_dir()

    def test_sanitizes_wiki_traversal(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        wiki = tmp_path / "wiki"
        raw.mkdir()
        wiki.mkdir()
        # "../wiki" is sanitized to "wiki", writing to raw/wiki/ (not the
        # actual wiki root) — safely contained inside raw/
        result = remember_write(raw, "content", source="../wiki", wiki_root=wiki)
        assert "Saved" in result
        assert (raw / "wiki").is_dir()

    def test_creates_source_dir(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        raw.mkdir()
        remember_write(raw, "content", source="new-source")
        assert (raw / "new-source").is_dir()

    def test_append_only(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        raw.mkdir()
        r1 = remember_write(raw, "first")
        r2 = remember_write(raw, "second")
        assert "Saved" in r1
        assert "Saved" in r2
        files = list((raw / "claude-session").glob("*.md"))
        assert len(files) == 2

    def test_rejects_oversized_content(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        raw.mkdir()
        huge = "x" * (11 * 1024 * 1024)  # 11 MB
        result = remember_write(raw, huge)
        assert "Error" in result
        assert "limit" in result.lower()


class TestRememberSources:
    """Per-claim provenance (issue athenaeum#90) on ``remember_write``."""

    def test_default_inferred_source_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        raw = tmp_path / "raw"
        raw.mkdir()
        with caplog.at_level("WARNING", logger="athenaeum.mcp_server"):
            result = remember_write(raw, "Some claim")
        assert "Saved to" in result
        text = list((raw / "claude-session").glob("*.md"))[0].read_text()
        assert "source: claude:inferred" in text
        # Warning surfaces on the server logger, NOT on stdout / MCP wire.
        assert any("no `sources` supplied" in r.getMessage() for r in caplog.records)

    def test_scalar_sources_propagates(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        raw.mkdir()
        result = remember_write(
            raw,
            "Some claim",
            sources="claude:session-2026-05-08",
        )
        assert "Saved to" in result
        text = list((raw / "claude-session").glob("*.md"))[0].read_text()
        assert "source: claude:session-2026-05-08" in text
        assert "claude:inferred" not in text

    def test_field_sources_wrapper_propagates(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        raw.mkdir()
        result = remember_write(
            raw,
            "Some claim",
            sources={"_field_sources": {"emails": "api:apollo:2026-05-07"}},
        )
        assert "Saved to" in result
        text = list((raw / "claude-session").glob("*.md"))[0].read_text()
        assert "field_sources:" in text
        assert "emails: api:apollo:2026-05-07" in text

    def test_source_wrapper_structured(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        raw.mkdir()
        result = remember_write(
            raw,
            "Some claim",
            sources={"_source": {"type": "api", "ref": "apollo", "confidence": 0.9}},
        )
        assert "Saved to" in result
        text = list((raw / "claude-session").glob("*.md"))[0].read_text()
        assert "source:" in text
        assert "type: api" in text
        assert "confidence: 0.9" in text

    def test_bare_dict_without_wrappers_rejected(self, tmp_path: Path) -> None:
        # Per design-lock §4: bare dict (no wrapper keys) is no longer
        # accepted. Caller must use _source / _field_sources.
        raw = tmp_path / "raw"
        raw.mkdir()
        result = remember_write(
            raw, "Some claim", sources={"emails": "api:apollo:2026-05-07"}
        )
        assert "Error" in result
        assert "_field_sources" in result

    def test_pathological_type_ref_fields(self, tmp_path: Path) -> None:
        # The locked pathological case: a wiki with frontmatter fields
        # literally named ``type`` and ``ref``. Pre-athenaeum#96 this was
        # misclassified as a structured single-source dict; now it must
        # be passed via ``_field_sources`` and round-trip intact.
        raw = tmp_path / "raw"
        raw.mkdir()

        # Bare form is rejected with a wrapper hint.
        bad = remember_write(
            raw,
            "Some claim",
            sources={"type": "api:x", "ref": "linkedin:y"},
        )
        assert "Error" in bad
        assert "_field_sources" in bad

        # Wrapped form succeeds and writes the per-field map intact.
        good = remember_write(
            raw,
            "Some claim",
            sources={
                "_field_sources": {
                    "type": "api:x",
                    "ref": "linkedin:y",
                }
            },
        )
        assert "Saved to" in good
        text = list((raw / "claude-session").glob("*.md"))[0].read_text()
        assert "field_sources:" in text
        assert "type: api:x" in text
        assert "ref: linkedin:y" in text

    def test_malformed_scalar_rejected(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        raw.mkdir()
        # "Has-Uppercase" matches neither typed nor legacy form.
        result = remember_write(raw, "Some claim", sources="Has-Uppercase")
        assert "Error" in result
        assert "invalid `sources`" in result

    def test_merges_with_existing_frontmatter(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        raw.mkdir()
        body = (
            "---\n"
            "uid: deadbeef\n"
            "type: person\n"
            "name: Already Structured\n"
            "---\n"
            "\n"
            "# Body\n"
        )
        result = remember_write(raw, body, sources="manual:vcard-import")
        assert "Saved to" in result
        text = list((raw / "claude-session").glob("*.md"))[0].read_text()
        # Existing keys preserved; source merged in.
        assert "uid: deadbeef" in text
        assert "name: Already Structured" in text
        assert "source: manual:vcard-import" in text


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


class TestCreateServer:
    def test_creates_server_instance(self, tmp_path: Path) -> None:
        pytest.importorskip("fastmcp")
        from athenaeum.mcp_server import create_server

        raw = tmp_path / "raw"
        wiki = tmp_path / "wiki"
        raw.mkdir()
        wiki.mkdir()
        server = create_server(raw_root=raw, wiki_root=wiki)
        assert server is not None

    def test_import_error_without_fastmcp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib
        import sys

        # Temporarily hide fastmcp
        saved = sys.modules.get("fastmcp")
        monkeypatch.setitem(sys.modules, "fastmcp", None)
        try:
            # Re-import to trigger the ImportError path
            import athenaeum.mcp_server as mod

            importlib.reload(mod)  # force fresh import of the function

            with pytest.raises(ImportError, match="FastMCP is required"):
                mod.create_server(
                    raw_root=tmp_path / "raw", wiki_root=tmp_path / "wiki"
                )
        finally:
            if saved is not None:
                monkeypatch.setitem(sys.modules, "fastmcp", saved)
            else:
                monkeypatch.delitem(sys.modules, "fastmcp", raising=False)


# ---------------------------------------------------------------------------
# entity_schema tool + config-derived recall schema (issue athenaeum#964)
# ---------------------------------------------------------------------------


class TestEntitySchemaToolAndConfigDerivedSchema:
    def _build(self, tmp_path: Path, *, caller_audience: set[str] | None = None):
        pytest.importorskip("fastmcp")
        from athenaeum.mcp_server import create_server

        raw = tmp_path / "raw"
        wiki = tmp_path / "wiki"
        raw.mkdir()
        wiki.mkdir()
        return create_server(
            raw_root=raw, wiki_root=wiki, caller_audience=caller_audience
        ), wiki

    def _get_fn(self, server, name: str):
        import asyncio

        async def _run():
            tool = await server.get_tool(name)
            return tool.fn

        return asyncio.run(_run())

    def test_registers_entity_schema_tool(self, tmp_path: Path) -> None:
        import asyncio

        server, _wiki = self._build(tmp_path)

        async def _run() -> set[str]:
            return {t.name for t in await server.list_tools()}

        names = asyncio.run(_run())
        assert "entity_schema" in names

    def test_recall_schema_names_a_declared_type_absent_from_source(
        self, tmp_path: Path
    ) -> None:
        # Issue athenaeum#964: the recall tool schema is COMPUTED from this
        # deployment's own types.md, not a literal enum in the source.
        raw = tmp_path / "raw"
        wiki = tmp_path / "wiki"
        raw.mkdir()
        wiki.mkdir()
        (wiki / "_schema").mkdir()
        (wiki / "_schema" / "types.md").write_text(
            "| Type |\n|---|\n| zzz-deployment-only-type |\n"
        )
        pytest.importorskip("fastmcp")
        from athenaeum.mcp_server import create_server

        server = create_server(raw_root=raw, wiki_root=wiki)
        recall_fn = self._get_fn(server, "recall")
        assert "zzz-deployment-only-type" in recall_fn.__doc__

    def test_missing_types_md_does_not_prevent_registration(
        self, tmp_path: Path
    ) -> None:
        # No `_schema/` directory at all -- must degrade gracefully, never
        # hard-fail server construction.
        server, _wiki = self._build(tmp_path)
        recall_fn = self._get_fn(server, "recall")
        assert recall_fn is not None

    def test_empty_types_md_does_not_prevent_registration(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        wiki = tmp_path / "wiki"
        raw.mkdir()
        wiki.mkdir()
        (wiki / "_schema").mkdir()
        (wiki / "_schema" / "types.md").write_text("")
        pytest.importorskip("fastmcp")
        from athenaeum.mcp_server import create_server

        server = create_server(raw_root=raw, wiki_root=wiki)
        recall_fn = self._get_fn(server, "recall")
        assert recall_fn is not None

    def test_entity_schema_tool_reports_declared_observed_and_queryable(
        self, tmp_path: Path
    ) -> None:
        raw = tmp_path / "raw"
        wiki = tmp_path / "wiki"
        raw.mkdir()
        wiki.mkdir()
        (wiki / "_schema").mkdir()
        (wiki / "_schema" / "types.md").write_text("| Type |\n|---|\n| person |\n")
        (wiki / "a.md").write_text(
            "---\nuid: u1\ntype: auto-memory\nname: Memory\n---\n\nBody.\n"
        )
        pytest.importorskip("fastmcp")
        from athenaeum.mcp_server import create_server

        server = create_server(raw_root=raw, wiki_root=wiki)
        schema_fn = self._get_fn(server, "entity_schema")
        result = schema_fn()

        assert result["queryable_fields"] == ["type"]
        by_name = {c["name"]: c for c in result["classes"]}
        assert by_name["person"]["declared"] is True
        assert by_name["person"]["observed"] is False
        assert by_name["auto-memory"]["declared"] is False
        assert by_name["auto-memory"]["observed"] is True
        assert by_name["auto-memory"]["count"] == 1

    def test_observed_undeclared_type_is_schema_visible_and_recall_accepts_it(
        self, tmp_path: Path
    ) -> None:
        # Issue athenaeum#964 AC amendment 3 end-to-end: a corpus type absent
        # from types.md is BOTH (a) listed by entity_schema as
        # observed-undeclared and (b) directly usable as a recall(type=...)
        # value -- the schema-authority decision rule is the live corpus,
        # not the declared registry alone.
        raw = tmp_path / "raw"
        wiki = tmp_path / "wiki"
        raw.mkdir()
        wiki.mkdir()
        (wiki / "_schema").mkdir()
        (wiki / "_schema" / "types.md").write_text("| Type |\n|---|\n| person |\n")
        (wiki / "a.md").write_text(
            "---\nuid: u1\ntype: auto-memory\nname: Auto Memory Page\n---\n\n"
            "Some auto-memory content about widgets.\n"
        )
        pytest.importorskip("fastmcp")
        from athenaeum.mcp_server import create_server, recall_search

        server = create_server(raw_root=raw, wiki_root=wiki)
        schema_fn = self._get_fn(server, "entity_schema")
        schema_result = schema_fn()
        by_name = {c["name"]: c for c in schema_result["classes"]}
        assert by_name["auto-memory"]["declared"] is False
        assert by_name["auto-memory"]["observed"] is True

        recall_result = recall_search(wiki, "widgets", type_filter="auto-memory")
        assert "Auto Memory Page" in recall_result

    def test_entity_schema_tool_respects_caller_audience(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        wiki = tmp_path / "wiki"
        raw.mkdir()
        wiki.mkdir()
        (wiki / "a.md").write_text(
            "---\nuid: u1\ntype: person\nname: Alice\naudience: [finance]\n---\n\n"
            "Body.\n"
        )
        pytest.importorskip("fastmcp")
        from athenaeum.mcp_server import create_server

        server = create_server(
            raw_root=raw, wiki_root=wiki, caller_audience={"ops"}
        )
        schema_fn = self._get_fn(server, "entity_schema")
        result = schema_fn()
        by_name = {c["name"]: c for c in result["classes"]}
        assert by_name["person"]["count"] == 0

    def test_instructions_mention_entity_schema(self, tmp_path: Path) -> None:
        server, _wiki = self._build(tmp_path)
        assert "entity_schema" in (server.instructions or "")


# ---------------------------------------------------------------------------
# Pending-questions tools (issue athenaeum#61)
# ---------------------------------------------------------------------------


def _seed_pending_wiki(tmp_path: Path, *, answered: bool = False) -> Path:
    """Build a tmp knowledge dir with a seeded `_pending_questions.md`."""
    raw = tmp_path / "raw"
    wiki = tmp_path / "wiki"
    raw.mkdir()
    wiki.mkdir()
    checkbox = "[x]" if answered else "[ ]"
    pending = wiki / "_pending_questions.md"
    pending.write_text(
        "# Pending Questions\n\n"
        '## [2026-04-20] Entity: "Acme Corp" (from sessions/test.md)\n'
        f"- {checkbox} Is Acme Series A or Series B after 2026?\n"
        "**Conflict type**: principled\n"
        "**Description**: Prior wiki says Series A; new raw implies Series B.\n"
    )
    return tmp_path


class TestPendingQuestionMCPTools:
    """The two tools registered in `create_server` for issue athenaeum#61.

    We exercise the underlying module helpers (same semantics as the tools)
    and verify the tools themselves are registered on the FastMCP server.
    """

    def test_list_and_resolve_happy_path(self, tmp_path: Path) -> None:
        from athenaeum.answers import list_unanswered, resolve_by_id

        root = _seed_pending_wiki(tmp_path)
        pending_path = root / "wiki" / "_pending_questions.md"

        items = list_unanswered(pending_path)
        assert len(items) == 1
        item = items[0]
        assert set(item.keys()) >= {
            "id",
            "entity",
            "source",
            "question",
            "conflict_type",
            "description",
            "created_at",
        }
        assert item["entity"] == "Acme Corp"

        result = resolve_by_id(pending_path, item["id"], "Series B, closed March 2026.")
        assert result["ok"] is True

        # After resolve, list_unanswered no longer returns this item.
        assert list_unanswered(pending_path) == []

    def test_resolve_not_found(self, tmp_path: Path) -> None:
        from athenaeum.answers import resolve_by_id

        root = _seed_pending_wiki(tmp_path)
        pending_path = root / "wiki" / "_pending_questions.md"
        result = resolve_by_id(pending_path, "nope", "answer")
        assert result["ok"] is False
        assert result["error_code"] == "id_not_found"
        assert "not found" in result["message"]

    def test_list_empty_when_file_missing(self, tmp_path: Path) -> None:
        from athenaeum.answers import list_unanswered

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        assert list_unanswered(wiki / "_pending_questions.md") == []

    def test_tools_registered_on_server(self, tmp_path: Path) -> None:
        """Both `list_pending_questions` + `resolve_question` must be exposed."""
        pytest.importorskip("fastmcp")
        import asyncio

        from athenaeum.mcp_server import create_server

        raw = tmp_path / "raw"
        wiki = tmp_path / "wiki"
        raw.mkdir()
        wiki.mkdir()
        server = create_server(raw_root=raw, wiki_root=wiki)

        # FastMCP exposes tools via `get_tool(name)` (async). We only care
        # that both names resolve without raising.
        async def _lookup() -> tuple[object, object]:
            lpq = await server.get_tool("list_pending_questions")
            rq = await server.get_tool("resolve_question")
            return lpq, rq

        lpq, rq = asyncio.run(_lookup())
        assert lpq is not None
        assert rq is not None

    def test_list_pending_decisions_tool(self, tmp_path: Path) -> None:
        """`list_pending_decisions` unifies questions + merges (issue athenaeum#401)."""
        pytest.importorskip("fastmcp")
        import asyncio

        from athenaeum.mcp_server import create_server

        raw = tmp_path / "raw"
        wiki = tmp_path / "wiki"
        raw.mkdir()
        wiki.mkdir()
        src = wiki / "aa11bb22-lean.md"
        src.write_text("---\nname: Lean Startup\n---\nBML loop.\n", encoding="utf-8")
        (wiki / "_pending_merges.md").write_text(
            "# Pending Merges\n\n"
            '## [2026-06-20] Merge: "startup"\n'
            "- [ ] Approve? Sources: aa11bb22-lean.md\n**Rationale**: r\n"
            f"**Sources**:\n- {src}\n**Confidence**: 0.8\n"
            "**Draft**:\n```markdown\nx\n```\n",
            encoding="utf-8",
        )
        (wiki / "_pending_questions.md").write_text(
            "# Pending Questions\n\n"
            '## [2026-07-01] Entity: "Acme" (from sessions/x.md)\n'
            "- [ ] Still Series A?\n**Conflict type**: principled\n"
            "**Description**: d\n",
            encoding="utf-8",
        )
        server = create_server(raw_root=raw, wiki_root=wiki)

        async def _run() -> dict:
            tool = await server.get_tool("list_pending_decisions")
            return tool.fn()

        result = asyncio.run(_run())
        # Issue athenaeum#1431: the tool now returns a bounded envelope, not a bare
        # list — assert the paging fields alongside the existing item checks.
        assert result["total"] == 2
        assert result["next_offset"] is None
        types = [d["type"] for d in result["items"]]
        assert types == ["merge", "question"]  # oldest (merge) first
        merge = result["items"][0]
        assert merge["payload"]["sources"][0]["title"] == "Lean Startup"
        assert "Lean Startup" in merge["summary"]


# ---------------------------------------------------------------------------
# recall_search extra-roots integration
# ---------------------------------------------------------------------------


class TestRecallSearchExtraRoots:
    """End-to-end: recall must render auto-memory hits with their real
    on-disk path, not a fabricated ``wiki/<auto-memory>/...`` label that
    would 404 for a reader following the link.
    """

    def test_renders_auto_memory_hit_with_root_prefix(self, tmp_path: Path) -> None:
        from athenaeum.search import FTS5Backend

        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        wiki.mkdir(parents=True)
        (wiki / "unrelated.md").write_text(
            "---\nname: Unrelated\n---\n\nnothing relevant\n"
        )
        auto_memory = knowledge / "raw" / "auto-memory"
        scope = auto_memory / "-Users-tristankromer-Code"
        scope.mkdir(parents=True)
        (scope / "feedback_develop_first_flow.md").write_text(
            "---\nname: develop-first flow\ntags: [workflow]\n---\n\n"
            "Ship to develop first, promote after CI is green.\n"
        )

        cache = tmp_path / "cache"
        FTS5Backend().build_index(wiki, cache, extra_roots=[auto_memory])

        result = recall_search(
            wiki,
            "develop first flow",
            top_k=3,
            search_backend="fts5",
            cache_dir=cache,
            extra_roots=[auto_memory],
        )
        assert "develop-first flow" in result
        # The rendered path must match the indexed ``<root_name>/<relpath>``
        # shape so a downstream agent can reopen the file.
        assert (
            "auto-memory/-Users-tristankromer-Code/" "feedback_develop_first_flow.md"
        ) in result
        # And must NOT hallucinate a ``wiki/`` prefix for extra-root hits.
        assert "wiki/auto-memory" not in result


# ---------------------------------------------------------------------------
# Push-metrics instrumentation on the recall path (issue athenaeum#711)
# ---------------------------------------------------------------------------


class TestRecallPushMetricsInstrumentation:
    """recall_search is the single point recall assembles a push payload into
    a session (_recall_via_backend's ``blocks`` loop). Instrumentation hooked
    there must record a push AND must never alter what recall renders.
    """

    def test_instrumentation_writes_a_record_from_the_real_env_var(
        self, wiki_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # THE test that would have caught athenaeum#734: with only the variable
        # Claude Code actually exports (CLAUDE_CODE_SESSION_ID) set, and the old
        # name explicitly DELETED, a recall push writes exactly one record
        # carrying that id. The pre-fix code read CLAUDE_SESSION_ID — a name
        # nothing exports — so this would have written nothing.
        from athenaeum import push_metrics

        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "test-session-734")
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("ATHENAEUM_PUSH_METRICS_ENABLED", raising=False)
        cache_dir = tmp_path / "cache"

        recall_search(wiki_dir, "Acme", search_backend="keyword", cache_dir=cache_dir)

        # Issue athenaeum#980 AC4: record_push's production call site now passes
        # wiki_root=, so the record lands behind the seam, not in cache_dir —
        # the read must match with the same wiki_root= to find it.
        rows = push_metrics.read_push_records(cache_dir=cache_dir, wiki_root=wiki_dir)
        assert len(rows) == 1
        assert rows[0]["session_id"] == "test-session-734"
        assert rows[0]["pushed_count"] >= 1
        # The opaque uid is recorded, never a raw filename/name.
        ids = [item["id"] for item in rows[0]["items"]]
        assert "a1b2c3d4" in ids
        # Records still carry NO claim content and NO personal data — ids,
        # tiers, scopes, counts only (athenaeum#711's criterion, unchanged).
        # athenaeum#1345 AC7's additive `memory_tier` was retired by issue
        # athenaeum#1514 and is no longer written; `tier` here is the
        # unrelated ACCESS tier.
        for item in rows[0]["items"]:
            assert set(item) <= {"id", "tier", "scope", "token_cost"}

    def test_code_session_id_wins_over_legacy(
        self, wiki_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Precedence: when BOTH are set, CLAUDE_CODE_SESSION_ID wins.
        from athenaeum import push_metrics

        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "win-code")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "lose-legacy")
        monkeypatch.delenv("ATHENAEUM_PUSH_METRICS_ENABLED", raising=False)
        cache_dir = tmp_path / "cache"

        recall_search(wiki_dir, "Acme", search_backend="keyword", cache_dir=cache_dir)

        rows = push_metrics.read_push_records(cache_dir=cache_dir, wiki_root=wiki_dir)
        assert len(rows) == 1
        assert rows[0]["session_id"] == "win-code"

    def test_falls_back_to_legacy_session_id_var(
        self, wiki_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Fallback: an environment that exports only the OLD name still works.
        from athenaeum import push_metrics

        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "legacy-fallback")
        monkeypatch.delenv("ATHENAEUM_PUSH_METRICS_ENABLED", raising=False)
        cache_dir = tmp_path / "cache"

        recall_search(wiki_dir, "Acme", search_backend="keyword", cache_dir=cache_dir)

        rows = push_metrics.read_push_records(cache_dir=cache_dir, wiki_root=wiki_dir)
        assert len(rows) == 1
        assert rows[0]["session_id"] == "legacy-fallback"

    def test_instrumentation_never_changes_recall_output(
        self, wiki_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum import push_metrics

        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-compare")
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)

        # Issue athenaeum#980 AC4: record_push's production call site now writes
        # BEHIND THE SEAM (wiki_root=), not under cache_dir — so the two
        # sub-runs below can no longer be ledger-isolated by cache_dir alone
        # (both share the one `wiki_dir` fixture). Run the DISABLED case
        # first and assert its empty ledger before the ENABLED case writes
        # anything into that shared wiki_root.
        cache_off = tmp_path / "cache_off"
        monkeypatch.setenv("ATHENAEUM_PUSH_METRICS_ENABLED", "0")
        out_off = recall_search(
            wiki_dir, "Acme", search_backend="keyword", cache_dir=cache_off
        )
        assert push_metrics.read_push_records(cache_dir=cache_off, wiki_root=wiki_dir) == []

        cache_on = tmp_path / "cache_on"
        monkeypatch.delenv("ATHENAEUM_PUSH_METRICS_ENABLED", raising=False)
        out_on = recall_search(
            wiki_dir, "Acme", search_backend="keyword", cache_dir=cache_on
        )

        assert out_on == out_off
        assert push_metrics.read_push_records(cache_dir=cache_on, wiki_root=wiki_dir) != []

    def test_no_push_record_when_no_session_id(
        self, wiki_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # With NEITHER name set, the behaviour is the existing clean no-op:
        # no record, recall output unaffected.
        from athenaeum import push_metrics

        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        cache_dir = tmp_path / "cache"
        recall_search(wiki_dir, "Acme", search_backend="keyword", cache_dir=cache_dir)
        assert push_metrics.read_push_records(cache_dir=cache_dir) == []

    def test_no_push_record_on_no_hits(
        self, wiki_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum import push_metrics

        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-nohits")
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        cache_dir = tmp_path / "cache"
        recall_search(
            wiki_dir, "xyznonexistentquery", search_backend="keyword", cache_dir=cache_dir
        )
        assert push_metrics.read_push_records(cache_dir=cache_dir) == []

    def test_push_metrics_failure_never_breaks_recall(
        self, wiki_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum import push_metrics

        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-broken")

        def _boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("ledger unavailable")

        monkeypatch.setattr(push_metrics, "record_push", _boom)
        # Must not raise despite the instrumentation call failing internally.
        result = recall_search(
            wiki_dir, "Acme", search_backend="keyword", cache_dir=tmp_path / "cache"
        )
        assert "Acme Corp" in result

    # -------------------------------------------------------------------
    # Issue athenaeum#1567: the ledger token_cost must meter the FULLY
    # RENDERED recall block (the same `_RecallRow.tokens` the push-token
    # budget already used, per athenaeum#718) -- not the 400-char `_snippet`
    # clamp, which saturated every sufficiently long page at exactly
    # `estimate_tokens` of 400 chars (100 tokens).
    # -------------------------------------------------------------------

    def test_ledger_token_cost_is_the_full_block_estimate_not_the_snippet(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Counter-example fixture (issue athenaeum#1567's plan step 3): one
        page under the 400-char snippet clamp and one page well over 2000
        chars, each recalled through `recall_search`. Before this fix, the
        long page's ledger `token_cost` was pinned at exactly 100 (the
        400-char snippet's `estimate_tokens`), regardless of the page's real
        rendered size -- this assertion set fails on that code (the `> 100`
        check below never holds, and it never differs from the ceiling other
        long pages would also hit). After the fix each ledger `token_cost`
        must equal `estimate_tokens` of the ACTUAL rendered block
        `recall_search` returned (the same quantity
        `test_unprompted_enforces_token_budget_at_the_boundary` pins for the
        budget path), so the short and long page's costs differ and the long
        one exceeds the old snippet-only ceiling.
        """
        from athenaeum import push_metrics
        from athenaeum.push_metrics import estimate_tokens

        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "test-session-1567")
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("ATHENAEUM_PUSH_METRICS_ENABLED", raising=False)

        wiki = tmp_path / "wiki-1567"
        wiki.mkdir()
        short_body = "Widgetronic1567short is a brief note about a widget.\n"
        long_body = "Widgetronic1567long " + (
            "padding prose to blow well past the four hundred character "
            "snippet clamp so the old snippet-only ceiling would bite. " * 25
        )
        assert len(short_body) < 400
        assert len(long_body) > 2000

        (wiki / "aaaa1111-short-page.md").write_text(
            f"---\nuid: aaaa1111\nname: Short widgetronic1567short page\ntype: concept\n"
            f"---\n\n{short_body}\n"
        )
        (wiki / "bbbb2222-long-page.md").write_text(
            f"---\nuid: bbbb2222\nname: Long widgetronic1567long page\ntype: concept\n"
            f"---\n\n{long_body}\n"
        )

        cache_short = tmp_path / "cache-short"
        short_result = recall_search(
            wiki, "widgetronic1567short", search_backend="keyword", cache_dir=cache_short
        )
        cache_long = tmp_path / "cache-long"
        long_result = recall_search(
            wiki, "widgetronic1567long", search_backend="keyword", cache_dir=cache_long
        )

        short_block = TestRecallTierAndPushBudget._extract_single_block(short_result)
        long_block = TestRecallTierAndPushBudget._extract_single_block(long_result)
        expected_short_tokens = estimate_tokens(short_block)
        expected_long_tokens = estimate_tokens(long_block)

        short_rows = push_metrics.read_push_records(cache_dir=cache_short, wiki_root=wiki)
        long_rows = push_metrics.read_push_records(cache_dir=cache_long, wiki_root=wiki)
        assert len(short_rows) == 1
        assert len(long_rows) == 1
        short_cost = short_rows[0]["items"][0]["token_cost"]
        long_cost = long_rows[0]["items"][0]["token_cost"]

        # AC1: the ledger's token_cost is EXACTLY `estimate_tokens(block)` for
        # the fully rendered block -- the same quantity the budget path uses
        # -- never a re-estimate of the snippet alone.
        assert short_cost == expected_short_tokens
        assert long_cost == expected_long_tokens

        # Counter-example: the long page must exceed the 400-char snippet's
        # 100-token ceiling, and the short page's cost must be lower than the
        # long page's -- both fail under the pre-fix
        # `token_cost=estimate_tokens(snippet_text)` behaviour, which pins
        # the long page at exactly 100.
        assert long_cost > 100
        assert short_cost < long_cost

    def test_ledger_token_cost_no_longer_derived_from_snippet_text(self) -> None:
        """AC2 (grep-verifiable): `build_push_record` must not call
        `estimate_tokens` on hit-supplied text at all -- the caller now
        supplies the token count directly. A regression that reintroduced
        `estimate_tokens(snippet_text)` inside the function body would
        silently reproduce the athenaeum#1567 bug even if every other test
        here still passed with a fixture whose snippet happens to match the
        real block size.
        """
        import inspect

        from athenaeum import push_metrics

        source = inspect.getsource(push_metrics.build_push_record)
        assert "token_cost=estimate_tokens(" not in source
        assert "token_cost=tokens" in source


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


class TestCLIServe:
    def test_serve_missing_dir(self, tmp_path: Path) -> None:
        from athenaeum.cli import main

        code = main(["serve", "--path", str(tmp_path / "nonexistent")])
        assert code == 1


# ---------------------------------------------------------------------------
# M22 (issue athenaeum#554): every registered MCP tool WRAPPER is invoked via tool.fn().
# Before this, only 2 of the 11 registered tool bodies were ever exercised; the
# 9 untested wrappers included every write path (remember, resolve_question,
# resolve_merge, review_audit_item). Argument marshalling and error-to-string
# conversion were unverified on the live production surface.
# ---------------------------------------------------------------------------


class TestAllMcpToolWrappers:
    """Invoke all 15 registered MCP tool wrappers through ``tool.fn()``."""

    # One valid-args invocation per registered tool. Read tools take no args;
    # the write tools are called with a nonexistent id so a single call
    # exercises BOTH the wrapper's argument marshalling and its error-to-string
    # path without a seeded queue. ``remember`` is the one write tool with a
    # trivial success, so it gets real content. ``read_entity`` (issue
    # athenaeum#886) is called with an unknown uid against an empty wiki — a
    # JSON-string not-found message, exercising marshalling without a seeded
    # entity page. (The person-shaped ``read_person`` tool, issue athenaeum#864,
    # was invoked the same way here before its removal in athenaeum#888.)
    _INVOKE = {
        "recall": lambda fn: fn("anything at all"),
        "remember": lambda fn: fn("a note worth remembering", source="test-session"),
        "list_pending_questions": lambda fn: fn(),
        "resolve_question": lambda fn: fn("no-such-id", "an answer body"),
        "raise_decision": lambda fn: fn("a question worth asking", "standalone context"),
        "list_pending_merges": lambda fn: fn(),
        "list_pending_decisions": lambda fn: fn(),
        "list_axiom_audit": lambda fn: fn(),
        "scan_retraction_cascade": lambda fn: fn(),
        "calibration_summary": lambda fn: fn(),
        "review_audit_item": lambda fn: fn("no-such-id", "confirm"),
        "resolve_merge": lambda fn: fn("no-such-id", "reject"),
        "read_entity": lambda fn: fn("no-such-uid", "person"),
        "entity_schema": lambda fn: fn(),
        "enumerate_entities": lambda fn: fn("no-such-type"),
    }
    _EXPECTED_TYPE = {
        "recall": str,
        "remember": str,
        # Issue athenaeum#1431: both now return a bounded {"items", "total",
        # "offset", "limit", "next_offset"} envelope, not a bare list.
        "list_pending_questions": dict,
        "list_pending_merges": list,
        "list_pending_decisions": dict,
        "list_axiom_audit": list,
        "resolve_question": dict,
        "raise_decision": dict,
        "resolve_merge": dict,
        "review_audit_item": dict,
        "scan_retraction_cascade": dict,
        "calibration_summary": dict,
        "read_entity": str,
        "entity_schema": dict,
        "enumerate_entities": dict,
    }

    def _server(self, tmp_path: Path, *, cache_dir: Path | None = None):
        pytest.importorskip("fastmcp")
        from athenaeum.mcp_server import create_server

        raw = tmp_path / "raw"
        wiki = tmp_path / "wiki"
        raw.mkdir(exist_ok=True)
        wiki.mkdir(exist_ok=True)
        return create_server(raw_root=raw, wiki_root=wiki, cache_dir=cache_dir)

    def _registered_names(self, server) -> list[str]:
        import asyncio

        async def _run() -> list[str]:
            return [t.name for t in await server.list_tools()]

        return asyncio.run(_run())

    def _call(self, server, name: str, caller):
        import asyncio

        async def _run():
            tool = await server.get_tool(name)
            return caller(tool.fn)

        return asyncio.run(_run())

    def test_invocation_map_covers_every_registered_tool(self, tmp_path: Path) -> None:
        # The set of registered tools must exactly equal the invocation map —
        # so a newly-registered tool forces a new invocation entry (and thus
        # wrapper coverage) instead of silently going untested.
        server = self._server(tmp_path)
        registered = set(self._registered_names(server))
        assert registered == set(self._INVOKE), (
            "MCP tool set drifted from the invocation map (issue athenaeum#554 M22): "
            f"registered-only={registered - set(self._INVOKE)}, "
            f"map-only={set(self._INVOKE) - registered}"
        )
        # Issue athenaeum#964: +1 for `entity_schema`, the ONE new schema-query tool
        # that issue adds. Issue athenaeum#965 adds one more: `enumerate_entities`,
        # the generalized ENUMERATION primitive (a distinct code path from
        # `recall` — no query text, never routed through ranking). Issue
        # athenaeum#888 removes one: the person-shaped `read_person` tool, once
        # every known consumer had migrated to `read_entity`. Bumping this
        # number is exactly the tripwire this test exists for.
        assert len(registered) == 15

    @pytest.mark.parametrize("name", sorted(_INVOKE))
    def test_wrapper_marshals_args_and_returns_declared_type(
        self, tmp_path: Path, name: str
    ) -> None:
        # Invoking every wrapper with valid args must not raise, and the return
        # value must marshal to the wrapper's declared type — the write tools'
        # error-to-string path (a nonexistent id) returns a value, never raises.
        server = self._server(tmp_path)
        result = self._call(server, name, self._INVOKE[name])
        assert isinstance(result, self._EXPECTED_TYPE[name]), (
            f"{name} returned {type(result).__name__}, "
            f"expected {self._EXPECTED_TYPE[name].__name__}"
        )

    # --- the four WRITE wrappers: marshalling + error-to-string --------------

    def test_remember_marshalling_and_error_to_string(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        cache.mkdir()
        server = self._server(tmp_path, cache_dir=cache)
        # Argument marshalling: a valid call returns the "Saved to" confirmation.
        ok = self._call(server, "remember", lambda fn: fn("hi there", source="s"))
        assert isinstance(ok, str)
        assert ok.startswith("Saved to")
        # Error-to-string: with capture disabled (kill switch), the wrapper
        # returns a message string rather than raising or writing.
        from athenaeum import killswitch

        killswitch.disable(cache_dir=cache)
        blocked = self._call(server, "remember", lambda fn: fn("nope", source="s"))
        assert isinstance(blocked, str)
        assert not blocked.startswith("Saved to")

    def test_resolve_question_marshalling_and_error_to_string(
        self, tmp_path: Path
    ) -> None:
        server = self._server(tmp_path)
        res = self._call(
            server, "resolve_question", lambda fn: fn("no-such-id", "ans")
        )
        # Marshalling: structured keys present.
        assert isinstance(res, dict)
        assert set(res) >= {"ok", "error_code", "message"}
        # Error-to-string: the unknown id is reported, not raised.
        assert res["ok"] is False
        assert res["error_code"]
        assert res["message"]

    def test_resolve_merge_marshalling_and_error_to_string(
        self, tmp_path: Path
    ) -> None:
        server = self._server(tmp_path)
        res = self._call(server, "resolve_merge", lambda fn: fn("no-such-id", "reject"))
        assert isinstance(res, dict)
        assert res["ok"] is False
        assert res.get("error_code")
        # Marshalling: an invalid decision is validated and reported, not raised.
        bad = self._call(server, "resolve_merge", lambda fn: fn("x", "bogus-decision"))
        assert bad["ok"] is False
        assert bad["error_code"] == "invalid_decision"

    def test_raise_decision_marshalling_and_error_to_string(
        self, tmp_path: Path
    ) -> None:
        """Issue athenaeum#912: valid raise succeeds; invalid input reports, not raises."""
        server = self._server(tmp_path)
        # Marshalling: a valid call returns a decision_id and structured keys.
        ok = self._call(
            server,
            "raise_decision",
            lambda fn: fn("Did you mean the stricter reading?", "standalone context"),
        )
        assert isinstance(ok, dict)
        assert set(ok) >= {"ok", "error_code", "message", "decision_id"}
        assert ok["ok"] is True
        assert ok["decision_id"]
        # Error-to-string: an empty question is validated and reported, not raised.
        bad_q = self._call(server, "raise_decision", lambda fn: fn("   ", "context"))
        assert bad_q["ok"] is False
        assert bad_q["error_code"] == "invalid_question"
        # Error-to-string: missing context is validated and reported, not raised.
        bad_ctx = self._call(server, "raise_decision", lambda fn: fn("a question", "  "))
        assert bad_ctx["ok"] is False
        assert bad_ctx["error_code"] == "missing_context"

    def test_review_audit_item_marshalling_and_error_to_string(
        self, tmp_path: Path
    ) -> None:
        server = self._server(tmp_path)
        res = self._call(
            server, "review_audit_item", lambda fn: fn("no-such-id", "confirm")
        )
        assert isinstance(res, dict)
        # Error-to-string: the ValueError for an unknown id becomes a structured
        # error dict, not a raised exception.
        assert res.get("ok") is False
        assert res.get("error")


# ---------------------------------------------------------------------------
# README <-> registered-tool parity (issue athenaeum#1380)
#
# Derives BOTH sides at runtime -- the registered tool names (and their
# read/write classification, from the module's own `_MUTATING_TOOLS`-style
# knowledge -- see below) from the live `create_server()` instance, and the
# documented names/classification from README.md's own table -- and hard-
# codes neither list, so registering a new tool without adding (or removing)
# a matching README row fails this test.
# ---------------------------------------------------------------------------


def _registered_tool_names(tmp_path: Path) -> set[str]:
    import asyncio

    pytest.importorskip("fastmcp")
    from athenaeum.mcp_server import create_server

    raw = tmp_path / "raw"
    wiki = tmp_path / "wiki"
    raw.mkdir()
    wiki.mkdir()
    server = create_server(raw_root=raw, wiki_root=wiki)

    async def _run() -> set[str]:
        return {t.name for t in await server.list_tools()}

    return asyncio.run(_run())


def _readme_mcp_section() -> str:
    """The documented MCP surface.

    Lives in ``docs/modules/mcp.md``, not the README: the tool table moved out
    when the README became a marketing document. The parity guarantee is
    unchanged -- a tool registered in ``create_server()`` without a matching
    documented row still fails this test.
    """
    doc = Path(__file__).resolve().parent.parent / "docs" / "modules" / "mcp.md"
    return doc.read_text()


def _readme_tool_rows() -> dict[str, str]:
    """Parse ``{tool_name: "READ"|"WRITE"}`` out of the documented table.

    Matches ``| `tool_name` | READ | ... |`` / ``| `tool_name` | WRITE | ... |``
    rows -- the same shape every existing row already uses.
    """
    section = _readme_mcp_section()
    rows: dict[str, str] = {}
    for line in section.splitlines():
        m = re.match(r"\|\s*`([a-zA-Z_][a-zA-Z0-9_]*)`\s*\|\s*(READ|WRITE)\s*\|", line)
        if m:
            rows[m.group(1)] = m.group(2)
    return rows


def _readme_stated_counts() -> tuple[int, int, int]:
    """Parse ``(total, read_only, mutating)`` out of the section's prose."""
    section = _readme_mcp_section()
    total_m = re.search(r"exposing \*\*(\d+) tools\*\*", section)
    split_m = re.search(r"(\d+) read-only, (\d+) that mutate", section)
    assert total_m and split_m, (
        "docs/modules/mcp.md prose did not match the expected pattern"
    )
    return int(total_m.group(1)), int(split_m.group(1)), int(split_m.group(2))


class TestReadmeToolTableMatchesRegisteredTools:
    def test_documented_names_equal_registered_names(self, tmp_path: Path) -> None:
        registered = _registered_tool_names(tmp_path)
        documented = set(_readme_tool_rows())
        assert documented == registered, (
            f"README table and create_server() have drifted -- "
            f"documented only: {documented - registered}, "
            f"registered only: {registered - documented}"
        )

    def test_stated_total_equals_registered_count(self, tmp_path: Path) -> None:
        registered = _registered_tool_names(tmp_path)
        total, _read_only, _mutating = _readme_stated_counts()
        assert total == len(registered)

    def test_stated_split_equals_table_rw_counts(self) -> None:
        rows = _readme_tool_rows()
        _total, stated_read, stated_write = _readme_stated_counts()
        assert stated_read == sum(1 for v in rows.values() if v == "READ")
        assert stated_write == sum(1 for v in rows.values() if v == "WRITE")

    def test_table_has_no_duplicate_or_unrecognized_rw_value(self) -> None:
        section = _readme_mcp_section()
        names = []
        for line in section.splitlines():
            m = re.match(r"\|\s*`([a-zA-Z_][a-zA-Z0-9_]*)`\s*\|\s*(\S+)\s*\|", line)
            if m:
                names.append(m.group(1))
                assert m.group(2) in {"READ", "WRITE"}, (
                    f"row for `{m.group(1)}` has an unrecognized R/W value: {m.group(2)!r}"
                )
        assert len(names) == len(set(names)), "README table has a duplicate tool row"


# ---------------------------------------------------------------------------
# Hybrid rank fusion, vector dispatch (issue athenaeum#1792)
# ---------------------------------------------------------------------------


def _hybrid_test_wiki(tmp_path: Path) -> Path:
    """A tiny wiki with pages varied enough that fts5 and vector rank them
    differently -- exercising fusion rather than a single-page corpus where
    both backends trivially agree."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "acme-corp.md").write_text(
        "---\nname: Acme Corp\ntags: [client]\n---\n\nAcme Corp is a client.\n"
    )
    (wiki / "widget-works.md").write_text(
        "---\nname: Widget Works\ntags: [client]\n---\n\nWidget Works builds widgets.\n"
    )
    (wiki / "unrelated.md").write_text(
        "---\nname: Unrelated\n---\n\nNothing relevant here.\n"
    )
    return wiki


class TestRecallSearchFts5ByteIdentical:
    """AC (issue athenaeum#1792): FTS5-only callers must be byte-identical --
    the hybrid block only ever runs for ``search_backend='vector'``, so a
    plain fts5 call must render identically whether ``recall.hybrid`` is on
    (the vector-only default) or explicitly off."""

    def test_fts5_output_unaffected_by_recall_hybrid_config(self, tmp_path: Path) -> None:
        from athenaeum.search import FTS5Backend

        wiki = _hybrid_test_wiki(tmp_path)
        cache = tmp_path / "cache"
        FTS5Backend().build_index(wiki, cache)

        hybrid_on = recall_search(
            wiki, "Acme", top_k=5, search_backend="fts5", cache_dir=cache, config=None
        )
        hybrid_off = recall_search(
            wiki,
            "Acme",
            top_k=5,
            search_backend="fts5",
            cache_dir=cache,
            config={"recall": {"hybrid": False}},
        )
        assert hybrid_on == hybrid_off

    def test_fts5_output_unaffected_when_no_fts5_index_check_would_matter(
        self, tmp_path: Path
    ) -> None:
        """Even a config that WOULD trip the vector path's
        ``fts5_index_available`` warning must not touch the fts5 dispatch
        -- that check lives entirely inside the ``backend_name == 'vector'``
        branch."""
        from athenaeum.search import FTS5Backend

        wiki = _hybrid_test_wiki(tmp_path)
        cache = tmp_path / "cache"
        FTS5Backend().build_index(wiki, cache)

        before = recall_search(wiki, "Acme", top_k=5, search_backend="fts5", cache_dir=cache)
        after = recall_search(
            wiki,
            "Acme",
            top_k=5,
            search_backend="fts5",
            cache_dir=cache,
            config={"recall": {"hybrid": True}},
        )
        assert before == after


class TestRecallSearchHybridFloorBeforeFusion:
    """Regression test (issue athenaeum#1792 review): the relevance floor
    must be applied to each input list BEFORE fusion, never to the fused
    score itself. Backend ``query`` methods are monkeypatched to return
    fixed, known scores so the test pins exact numeric behavior rather
    than depending on the embedding stub's incidental rankings."""

    def test_per_list_floor_independence_and_no_post_fusion_floor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("chromadb")
        from athenaeum.search import FTS5Backend, VectorBackend

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "page-a.md").write_text("---\nname: Page A\n---\n\nbody\n")
        (wiki / "page-b.md").write_text("---\nname: Page B\n---\n\nbody\n")

        cache = tmp_path / "cache"
        FTS5Backend().build_index(wiki, cache)
        VectorBackend().build_index(wiki, cache)

        # page-a: raw vector score 1.0 PASSES a vector floor of 1.7 (lower
        # is better: 1.0 <= 1.7); raw fts5 score -1.0 FAILS an fts5 floor
        # of -5.0 (-1.0 is not <= -5.0). If per-list filtering runs before
        # fusion (correct), page-a is dropped from the fts5 list but
        # SURVIVES via the vector list alone, and still appears in the
        # final fused result -- proving failing ONE backend's floor does
        # not disqualify a hit globally.
        #
        # page-b: raw vector score 1.9 FAILS the same vector floor (1.9 is
        # not <= 1.7); its raw fts5 score -1.0 also FAILS the fts5 floor.
        # page-b is filtered out of BOTH lists before fusion runs at all,
        # so it must be ABSENT from the final result. This is the case
        # that would leak through if a floor were (incorrectly) compared
        # against the FUSED score instead of each raw per-list score: a
        # fused score is always a small number (~0.01-0.03, one or two
        # terms of `1/(k+rank)` with `k=60`), so `fused_score <= 1.7`
        # would trivially be True for ANY hit under vector's own
        # lower-is-better direction -- a post-fusion check using that
        # direction could never reject page-b, so page-b's absence here
        # can only be explained by pre-fusion, per-list filtering.
        def fake_vector_query(self, query, cache_dir, *, n=5, **kwargs):
            del query, cache_dir, n, kwargs
            return [("page-a.md", "Page A", 1.0), ("page-b.md", "Page B", 1.9)]

        def fake_fts5_query(self, query, cache_dir, *, n=5, **kwargs):
            del query, cache_dir, n, kwargs
            return [("page-a.md", "Page A", -1.0), ("page-b.md", "Page B", -1.0)]

        monkeypatch.setattr(VectorBackend, "query", fake_vector_query)
        monkeypatch.setattr(FTS5Backend, "query", fake_fts5_query)

        result = recall_search(
            wiki,
            "irrelevant query text",
            top_k=5,
            search_backend="vector",
            cache_dir=cache,
            config={
                "recall": {
                    "hybrid": True,
                    "relevance_floor": {"vector": 1.7, "fts5": -5.0},
                }
            },
        )

        assert "Page A" in result, (
            "page-a passes the vector floor and must survive via the "
            "vector list alone, even though it fails the fts5 floor -- "
            "per-list floors are independent, not a global AND"
        )
        assert "Page B" not in result, (
            "page-b fails BOTH per-list floors and must never reach "
            "fusion at all -- its absence can only be explained by "
            "pre-fusion filtering, since a fused score is always far "
            "too small (~0.01-0.03) for a post-fusion comparison against "
            "either configured floor (1.7, -5.0) to ever reject it"
        )
        # The rendered score is the FUSED score (small, `k=60` reciprocal
        # rank terms), not either raw input score (1.0 / -1.0) -- direct
        # evidence the surviving hit's displayed score already went
        # through fusion, not a floor comparison against a raw score.
        assert "(score: 1.0)" not in result
        assert "(score: -1.0)" not in result

    def test_hybrid_off_ignores_relevance_floor_hybrid_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sanity companion: with ``recall.hybrid`` off, the same strict
        vector floor is applied the ORDINARY (non-hybrid) way, directly to
        the raw vector score -- page-b (1.9) still fails it, but page-a
        (1.0) passes on the vector list alone with no fts5 involvement."""
        pytest.importorskip("chromadb")
        from athenaeum.search import FTS5Backend, VectorBackend

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "page-a.md").write_text("---\nname: Page A\n---\n\nbody\n")
        (wiki / "page-b.md").write_text("---\nname: Page B\n---\n\nbody\n")

        cache = tmp_path / "cache"
        FTS5Backend().build_index(wiki, cache)
        VectorBackend().build_index(wiki, cache)

        def fake_vector_query(self, query, cache_dir, *, n=5, **kwargs):
            del query, cache_dir, n, kwargs
            return [("page-a.md", "Page A", 1.0), ("page-b.md", "Page B", 1.9)]

        monkeypatch.setattr(VectorBackend, "query", fake_vector_query)

        result = recall_search(
            wiki,
            "irrelevant query text",
            top_k=5,
            search_backend="vector",
            cache_dir=cache,
            config={
                "recall": {
                    "hybrid": False,
                    "relevance_floor": {"vector": 1.7},
                }
            },
        )
        assert "Page A" in result
        assert "Page B" not in result
        assert "(score: 1.0)" in result


class TestRecallSearchHybridOffCorpusInteraction:
    """issue athenaeum#1792 review (Should 1): off_corpus federation and the
    hybrid dispatch's widened vector-only requery don't compose (the
    widened requery doesn't carry off-corpus hits). Hybrid must SKIP with a
    logged warning whenever this call actually federated an off-corpus
    root, falling back to the already off-corpus-federated ``hits`` --
    never silently discard the federated off-corpus hits by overwriting
    them with a fused, off-corpus-blind list."""

    def test_hybrid_skips_and_warns_when_off_corpus_federated(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        from athenaeum import off_corpus
        from athenaeum.search import VectorBackend, build_fts5_index
        from tests.conftest import init_git_repo

        pytest.importorskip("chromadb")

        knowledge_root = tmp_path / "knowledge"
        wiki_root = knowledge_root / "wiki"
        wiki_root.mkdir(parents=True)
        (wiki_root / "ordinary.md").write_text(
            "---\nname: Ordinary Topic\n---\n\nan ordinary corpus claim\n"
        )
        init_git_repo(knowledge_root)

        off_corpus_dir = tmp_path / "off-corpus-store"
        off_corpus_dir.mkdir()
        (off_corpus_dir / "erasure-claim-one.md").write_text(
            "---\nname: Zephyrwidgets Erasure Claim\ntype: erasure-claim\n"
            "---\n\na very specific off-corpus fact\n"
        )
        config = {
            "off_corpus": {"enabled": True, "adapter": "off-corpus-test"},
            "storage": {
                "adapters": {
                    "off-corpus-test": {
                        "backing_store": "markdown",
                        "surface_root": str(off_corpus_dir),
                        "corpus_policy": {
                            "embedded": False,
                            "recallable": True,
                            "merge_eligible": False,
                        },
                    },
                },
                "mapping": {"erasure-claim": "off-corpus-test"},
            },
            "recall": {"hybrid": True},
        }
        cache_dir = tmp_path / "cache"

        build_fts5_index(wiki_root, cache_dir, config=config)
        VectorBackend().build_index(wiki_root, cache_dir)
        counts = off_corpus.build_off_corpus_index(config, knowledge_root, cache_dir)
        assert counts is not None and counts["fts5"] == 1

        with caplog.at_level(logging.WARNING, logger="athenaeum.mcp_server"):
            result = recall_search(
                wiki_root,
                "Zephyrwidgets",
                search_backend="vector",
                cache_dir=cache_dir,
                config=config,
            )

        assert "Zephyrwidgets Erasure Claim" in result, (
            "the off-corpus hit must survive hybrid's skip-fallback, not "
            "be silently dropped by an overwriting fused list"
        )
        assert any(
            "hybrid ranking skipped" in record.message for record in caplog.records
        ), [record.message for record in caplog.records]


class TestRecallSearchVectorHybridDispatch:
    """The vector dispatch path's hybrid block itself -- guarded on
    chromadb exactly like every other vector-backed test in this repo."""

    def test_hybrid_disabled_by_config_skips_fusion(self, tmp_path: Path) -> None:
        """``recall.hybrid: false`` must reach the dispatch, not just the
        resolver -- ``test_config_resolver_parity_generic`` only proves
        ``resolve_recall_hybrid`` itself reads the key, not that
        ``recall_search`` honors it. With fusion off, no FTS5 index is ever
        touched even when one exists at the SAME cache_dir -- proven here by
        pointing ``cache_dir`` at a directory with ONLY a vector index (no
        FTS5 db at all), which would make the hybrid-on path warn-and-fall-
        back but must make the hybrid-off path succeed silently."""
        pytest.importorskip("chromadb")
        from athenaeum.search import VectorBackend

        wiki = _hybrid_test_wiki(tmp_path)
        cache = tmp_path / "vector-only-cache"
        VectorBackend().build_index(wiki, cache)
        assert not (cache / "wiki-index.db").is_file()  # no FTS5 index here

        result = recall_search(
            wiki,
            "Acme",
            top_k=5,
            search_backend="vector",
            cache_dir=cache,
            config={"recall": {"hybrid": False}},
        )
        assert "Acme Corp" in result

    def test_missing_fts5_index_falls_back_to_vector_only_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The issue's FTS5-index-availability choice: a vector-only
        deployment (no FTS5 index ever built at this cache_dir) degrades to
        vector-only ranking with a logged warning, never a raise and never
        a lazily-built index on this read path."""
        pytest.importorskip("chromadb")
        import logging

        from athenaeum.search import VectorBackend

        wiki = _hybrid_test_wiki(tmp_path)
        cache = tmp_path / "vector-only-cache"
        VectorBackend().build_index(wiki, cache)
        assert not (cache / "wiki-index.db").is_file()

        with caplog.at_level(logging.WARNING, logger="athenaeum.mcp_server"):
            result = recall_search(
                wiki, "Acme", top_k=5, search_backend="vector", cache_dir=cache
            )
        assert "Acme Corp" in result
        assert any(
            "no FTS5 index exists" in record.message for record in caplog.records
        )

    def test_hybrid_surfaces_an_fts5_only_hit_vector_alone_would_miss(
        self, tmp_path: Path
    ) -> None:
        """The mechanism end to end, WITH a hybrid-off control (issue
        athenaeum#1792 review): a page that ranks well in fts5 (an exact
        lexical hit on its one rare, discriminating token) but is pushed
        out of a small ``top_k`` by several distractor pages that share
        MORE of the query's common terms -- so a small ``top_k`` vector-
        only ranking genuinely misses it -- still surfaces once hybrid
        fusion runs. The control (hybrid off, same query, same index) must
        show the page ABSENT; without it, vector alone might already rank
        the page first on a trivially small corpus and the "surfaces via
        fusion" claim would be unproven."""
        pytest.importorskip("chromadb")
        from athenaeum.search import FTS5Backend, VectorBackend

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        # FTS5 indexes name/tags/aliases/description ONLY -- never the
        # body (see ``FTS5Backend._CREATE_SQL``) -- while the vector
        # backend embeds the whole file (frontmatter and body both), so
        # the distinguishing text goes in ``description:`` where BOTH
        # backends can see it, and each distractor's ``description:``
        # shares every COMMON query term but not the rare one. Their
        # combined bag-of-words overlap with the query out-scores the
        # (single-rare-term) target page under the test suite's offline
        # hashing-bow embedding stub (tests/offline_embeddings.py), even
        # though the target is the only page whose description contains
        # "Zylofoobar" at all -- verified below by the hybrid-off control.
        query = "Zylofoobar annual review process meeting schedule budget"
        for i in range(6):
            (wiki / f"distractor-{i}.md").write_text(
                f"---\nname: Distractor {i}\ndescription: annual review "
                "process meeting schedule budget planning operations "
                "quarterly summary logistics vendor contract\n---\n\n"
                f"filler{i} filler{i}b filler{i}c body text here.\n"
            )
        (wiki / "target.md").write_text(
            "---\nname: Target Page\ndescription: Zylofoobar annual "
            "review.\n---\n\nShort target body.\n"
        )

        cache = tmp_path / "cache"
        FTS5Backend().build_index(wiki, cache)
        VectorBackend().build_index(wiki, cache)

        vector_only = recall_search(
            wiki,
            query,
            top_k=5,
            search_backend="vector",
            cache_dir=cache,
            config={"recall": {"hybrid": False}},
        )
        assert "Target Page" not in vector_only, (
            "fixture no longer demonstrates the failure mode -- vector "
            "alone already ranks the target page inside top_k; widen the "
            "distractor overlap or shrink top_k further"
        )

        hybrid_result = recall_search(
            wiki,
            query,
            top_k=5,
            search_backend="vector",
            cache_dir=cache,
            config={"recall": {"hybrid": True}},
        )
        assert "Target Page" in hybrid_result


# ---------------------------------------------------------------------------
# Default install without chromadb (issue athenaeum#1825)
# ---------------------------------------------------------------------------


class TestRecallSearchChromadbMissing:
    """``search_backend`` now defaults to ``"vector"``, so a default install
    that never ran ``pip install athenaeum[vector]`` must still answer a
    recall through the real ``recall_search`` MCP tool entry point -- not
    just at the ``VectorBackend.query`` unit level ``tests/test_search.py``
    already covers. This specifically exercises the hybrid dispatch path
    (``recall.hybrid`` on by default for ``search_backend="vector"``),
    where ``backend.query()`` is called TWICE on the SAME backend instance
    (the primary query, then the widened re-query for RRF fusion) -- the AC
    is ONE warning per recall, not one per internal query() call, which a
    unit test calling ``VectorBackend.query`` directly cannot observe."""

    def test_recall_search_falls_back_to_fts5_with_one_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging
        import sys

        from athenaeum.search import FTS5Backend

        wiki = _hybrid_test_wiki(tmp_path)
        cache = tmp_path / "cache"
        # FTS5 is built unconditionally by session-start-recall.sh in every
        # real deployment regardless of search_backend -- reproduce that.
        FTS5Backend().build_index(wiki, cache)
        assert not (cache / "wiki-vectors").is_dir()  # no vector index either

        monkeypatch.setitem(sys.modules, "chromadb", None)

        with caplog.at_level(logging.WARNING, logger="athenaeum.search"):
            result = recall_search(wiki, "Acme", top_k=5, search_backend="vector", cache_dir=cache)

        assert "Acme Corp" in result
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1, warnings
        assert "chromadb" in warnings[0].message
