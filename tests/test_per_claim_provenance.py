# SPDX-License-Identifier: Apache-2.0
"""Per-claim provenance through merge (issue athenaeum#1730).

``docs/use-cases.md`` §3.5: provenance must survive compilation, because a
compiled page is itself a source for the level above. Before this issue the
compile appended ``[^src-N]`` footnote DEFINITIONS to a page with nothing in
the prose referring to them — a page-level bibliography wearing footnote
syntax — so a sentence could not be resolved to the source of the claim it
came from.

One class per acceptance criterion, so a failure names the criterion it broke.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from athenaeum.footnote_markers import (
    MarkerCoverage,
    attach_markers,
    iter_prose_sentences,
    marker_label,
    parse_footnote_definitions,
    resolve_markers,
    unmarked_sentence_ratio,
)


def _member(path: Path, *, name: str, session: str, turn: int, ref: str, body: str) -> Path:
    path.write_text(
        "---\n"
        f"name: {name}\n"
        "type: fact\n"
        "sources:\n"
        f"  - session: {session}\n"
        f"    turn: {turn}\n"
        "    source_type: user-stated\n"
        f"    source_ref: {ref}\n"
        "---\n\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# The shared primitive: ONE segmentation, used by both the writer and the
# measurer. These tests exist because two segmenters would silently drift and
# the metric would stop describing the writer.
# ---------------------------------------------------------------------------


class TestFootnoteMarkerPrimitive:
    def test_marker_and_definition_are_distinguished(self) -> None:
        """``[^src-1]`` and ``[^src-1]:`` differ by one character and mean
        opposite things — a reference versus the thing referred to."""
        body = "A claim.[^src-1]\n\n[^src-1]: **Source:** user-stated\n"
        assert parse_footnote_definitions(body) == {"src-1": "**Source:** user-stated"}
        assert [s.markers for s in iter_prose_sentences(body)] == [("src-1",)]

    def test_definition_line_is_not_counted_as_an_uncited_sentence(self) -> None:
        """A page whose only marker-less line IS the footnote definition must
        not be reported as uncited prose — the definition is the citation."""
        body = "A claim.[^src-1]\n\n[^src-1]: **Source:** user-stated\n"
        assert unmarked_sentence_ratio(body) == MarkerCoverage(total=1, unmarked=0)

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param("```\nnot a sentence. really.\n```\n", id="fenced-code"),
            pytest.param("~~~\nalso not. a sentence.\n~~~\n", id="tilde-fence"),
            pytest.param("# A heading is not a sentence\n", id="heading"),
            pytest.param("| a | b |\n| - | - |\n| c | d |\n", id="table"),
            pytest.param("<!-- a comment. with punctuation. -->\n", id="html-comment"),
            pytest.param("[ref]: https://example.com/a\n", id="link-reference"),
            pytest.param("    indented = code()  # not prose.\n", id="indented-code"),
            pytest.param("---\n", id="thematic-break"),
        ],
    )
    def test_non_prose_blocks_are_not_sentences(self, body: str) -> None:
        """Markdown that is not a sentence is skipped by BOTH directions: it is
        never measured as uncited, and never has a marker stapled into it."""
        assert unmarked_sentence_ratio(body).total == 0
        assert attach_markers(body, ["src-1"]) == body

    def test_a_multiline_html_comment_is_skipped_whole(self) -> None:
        """A comment that spans lines must be skipped as a BLOCK. Skipping only
        the opening line would let its interior read as prose and get a marker
        stapled inside a comment."""
        body = (
            "A real claim.\n\n"
            "<!--\n"
            "A commented-out claim. And another.\n"
            "-->\n\n"
            "Another real claim.\n"
        )
        assert unmarked_sentence_ratio(body).total == 2
        assert "A commented-out claim.[^" not in attach_markers(body, ["src-1"])

    def test_html_block_lines_are_not_prose(self) -> None:
        body = "<div class=\"note\">\n"
        assert unmarked_sentence_ratio(body).total == 0

    def test_a_marker_inside_a_code_fence_is_not_a_citation(self) -> None:
        """Otherwise a page documenting the marker syntax would grade itself
        fully cited on the strength of its own example."""
        body = "An uncited claim.\n\n```\nexample.[^src-1]\n```\n"
        assert unmarked_sentence_ratio(body) == MarkerCoverage(total=1, unmarked=1)

    def test_blockquotes_are_prose(self) -> None:
        """Quoted material makes claims, so it needs a citation like anything
        else. This is a deliberate choice, not an oversight in the skip list."""
        assert unmarked_sentence_ratio("> Someone said a thing.\n").total == 1

    def test_writer_and_measurer_agree_by_construction(self) -> None:
        """The property the whole module exists to guarantee: a page the writer
        marked scores ZERO unmarked sentences. If these two ever drift, this is
        the test that fails."""
        body = (
            "First claim here. Second claim here.\n\n"
            "- A bullet claim.\n"
            "- Another bullet claim.\n\n"
            "> A quoted claim.\n"
        )
        assert unmarked_sentence_ratio(body) == MarkerCoverage(total=5, unmarked=5)
        assert unmarked_sentence_ratio(attach_markers(body, ["src-2"])).unmarked == 0

    def test_attach_is_idempotent(self) -> None:
        """Re-running the writer over its own output must be a no-op — the
        compile is re-run over pages it has already written."""
        once = attach_markers("A claim. Another claim.\n", ["src-1"])
        assert attach_markers(once, ["src-1"]) == once

    def test_marker_lands_after_terminal_punctuation(self) -> None:
        """Wikipedia's convention, and the one the rendered definitions read
        naturally against."""
        assert attach_markers("A claim.\n", ["src-1"]) == "A claim.[^src-1]\n"

    def test_empty_labels_leave_the_body_untouched(self) -> None:
        """A member with no resolvable source has nothing to cite. Inventing a
        marker for it would be worse than the page-level union this replaces."""
        assert attach_markers("A claim.\n", []) == "A claim.\n"

    def test_a_page_with_no_prose_is_not_an_uncited_page(self) -> None:
        """``0/0`` is 0.0, not 1.0 — otherwise every stub and index page in the
        corpus flags on the day the guardrail ships."""
        assert unmarked_sentence_ratio("# Title only\n").ratio == 0.0

    def test_resolve_markers_returns_only_what_is_cited(self) -> None:
        """A recall hit renders an excerpt. Resolving the page's WHOLE
        bibliography onto it would reinstate the page-level union."""
        body = (
            "A claim.[^src-1]\n\n"
            "[^src-1]: **Source:** user-stated\n"
            "[^src-2]: **Source:** external\n"
        )
        assert set(parse_footnote_definitions(body)) == {"src-1", "src-2"}
        assert set(resolve_markers(body)) == {"src-1"}

    def test_a_dangling_marker_is_omitted_not_mapped_to_empty(self) -> None:
        """``label in footnotes`` must answer truthfully: a cited label with no
        definition on the page is a real condition worth being able to detect."""
        assert resolve_markers("A claim.[^src-9]\n") == {}

    def test_a_repeated_definition_keeps_the_first(self) -> None:
        """Matches how a markdown renderer resolves a duplicate label — the map
        must not silently report the later one the reader never sees."""
        body = "[^src-1]: **Source:** first\n[^src-1]: **Source:** second\n"
        assert parse_footnote_definitions(body) == {"src-1": "**Source:** first"}

    def test_resolve_markers_honors_an_explicit_label_filter(self) -> None:
        """A recall hit passes the labels its snippet cites; a marker cited
        elsewhere on the page must not ride along."""
        body = (
            "A.[^src-1] B.[^src-2]\n\n"
            "[^src-1]: **Source:** one\n[^src-2]: **Source:** two\n"
        )
        assert set(resolve_markers(body, ["src-2"])) == {"src-2"}

    def test_a_bullet_with_no_text_is_not_a_sentence(self) -> None:
        """Bullet syntax alone is not a claim, so it is neither counted as
        uncited nor given a marker."""
        assert unmarked_sentence_ratio("-\n").total == 0
        assert attach_markers("-\n", ["src-1"]) == "-\n"

    def test_marker_label_is_one_based(self) -> None:
        """Mirrors ``render_source_footnotes``'s own ``src-1``, ``src-2`` order
        — both sides derive the spelling from this one function."""
        assert marker_label(1) == "src-1"


# ---------------------------------------------------------------------------
# AC1: the tier-3 create and merge prompts request inline markers.
# ---------------------------------------------------------------------------


class TestPromptsRequestInlineMarkers:
    """The goldens pin that prompt bytes did not change UNSEEN; they cannot
    say the requirement is still there. These assert the requirement itself,
    so a later prompt rewrite that drops it fails here rather than shipping a
    silently un-marked corpus."""

    @pytest.mark.parametrize(
        "name",
        [
            "tiers.create_system",
            "tiers.create_template",
            "tiers.merge_system",
            "tiers.merge_template",
            "tiers.merge_system_full",
        ],
    )
    def test_prompt_asks_for_an_inline_marker(self, name: str) -> None:
        from athenaeum.prompt_registry import PROMPTS

        text = PROMPTS[name]
        assert "inline" in text
        assert "[^" in text

    def test_full_echo_fallback_protects_existing_markers(self) -> None:
        """The full-echo contract REPLACES the whole body, and the model is not
        given the sources the existing markers cite — so it must be told to
        leave them alone rather than re-derive them."""
        from athenaeum.prompt_registry import PROMPTS

        # Matched short of the line wrap — the rule spans two source lines.
        assert "Leave the markers already on" in PROMPTS["tiers.merge_system_full"]


# ---------------------------------------------------------------------------
# AC2: a two-source merge marks each sentence with ITS OWN source.
# ---------------------------------------------------------------------------


class TestTwoSourceMergeMarksPerClaim:
    def _two_member_entry(self, tmp_path: Path):
        from athenaeum.merge import merge_cluster_row

        a = _member(
            tmp_path / "a.md",
            name="A",
            session="sess-a",
            turn=1,
            ref="sess-a#turn1",
            body="Tristan founded Kromatic in 2011.",
        )
        b = _member(
            tmp_path / "b.md",
            name="B",
            session="sess-b",
            turn=7,
            ref="sess-b#turn7",
            body="Kromatic is based in San Francisco.",
        )
        row = {
            "cluster_id": "c-1730",
            "member_paths": [str(a), str(b)],
            "centroid_score": 0.9,
        }
        entry = merge_cluster_row(row, extra_roots=[tmp_path], am_by_path={})
        assert entry is not None
        return entry

    def test_each_sentence_carries_the_marker_of_its_own_source(
        self, tmp_path: Path
    ) -> None:
        """The issue's headline criterion. Member A's claim cites A's source and
        NOT B's; member B's cites B's and not A's. Before this issue both
        sentences resolved only to the page-level union of both."""
        from athenaeum.merge import render_merged_entry

        rendered = render_merged_entry(self._two_member_entry(tmp_path))
        lines = rendered.splitlines()
        founded = next(line for line in lines if "founded Kromatic" in line)
        based = next(line for line in lines if "based in San Francisco" in line)

        assert founded.endswith("[^src-1]")
        assert "[^src-2]" not in founded
        assert based.endswith("[^src-2]")
        assert "[^src-1]" not in based

    def test_every_marker_resolves_to_a_definition_on_the_page(
        self, tmp_path: Path
    ) -> None:
        """A marker pointing at nothing is worse than no marker — it looks like
        provenance and is not."""
        from athenaeum.merge import render_merged_entry

        rendered = render_merged_entry(self._two_member_entry(tmp_path))
        definitions = parse_footnote_definitions(rendered)
        assert set(resolve_markers(rendered)) <= set(definitions)
        assert "`sess-a#turn1`" in definitions["src-1"]
        assert "`sess-b#turn7`" in definitions["src-2"]
        # Each definition names ONE source — the markers are not aliases
        # for the same union.
        assert "sess-b" not in definitions["src-1"]
        assert "sess-a" not in definitions["src-2"]

    def test_compiled_page_measures_as_fully_cited(self, tmp_path: Path) -> None:
        """The compile is the one writer that can guarantee this, so the
        post-check must report zero uncited prose on its output."""
        entry = self._two_member_entry(tmp_path)
        assert unmarked_sentence_ratio(entry.body).unmarked == 0

    def test_a_member_citing_two_sources_marks_with_both(self, tmp_path: Path) -> None:
        """Honest granularity: a marker attaches at MEMBER granularity, so a
        member citing two sources marks with both. That is a real improvement
        on the page-level union and is not true per-sentence provenance —
        claims as addressable units is athenaeum#709."""
        from athenaeum.merge import merge_cluster_row

        path = tmp_path / "two.md"
        path.write_text(
            "---\nname: Two\ntype: fact\nsources:\n"
            "  - session: sess-a\n    turn: 1\n    source_ref: sess-a#turn1\n"
            "  - session: sess-b\n    turn: 2\n    source_ref: sess-b#turn2\n"
            "---\n\nOne claim from two sources.\n",
            encoding="utf-8",
        )
        entry = merge_cluster_row(
            {"cluster_id": "c-2", "member_paths": [str(path)], "centroid_score": 1.0},
            extra_roots=[tmp_path],
            am_by_path={},
        )
        assert entry is not None
        assert "One claim from two sources.[^src-1][^src-2]" in entry.body

    def test_deduped_paragraph_keeps_the_first_citing_members_marker(
        self, tmp_path: Path
    ) -> None:
        """Dedupe runs on the UNMARKED text — stamping first would make two
        members' identical wording differ and defeat the exact-match compare.
        The survivor carries the first member's marker, matching the first-wins
        rule ``dedupe_sources`` applies to the sources themselves."""
        from athenaeum.merge import merge_cluster_row

        shared = "The same sentence, worded identically."
        a = _member(
            tmp_path / "a.md", name="A", session="sess-a", turn=1,
            ref="sess-a#turn1", body=shared,
        )
        b = _member(
            tmp_path / "b.md", name="B", session="sess-b", turn=2,
            ref="sess-b#turn2", body=shared,
        )
        entry = merge_cluster_row(
            {"cluster_id": "c-3", "member_paths": [str(a), str(b)], "centroid_score": 1.0},
            extra_roots=[tmp_path],
            am_by_path={},
        )
        assert entry is not None
        assert entry.body.count(shared) == 1
        assert f"{shared}[^src-1]" in entry.body
        assert "[^src-2]" not in entry.body

    def test_synthesize_body_without_labels_is_unchanged(self) -> None:
        """Back-compat: every existing caller passes no labels and must get
        byte-identical output."""
        from athenaeum.merge import synthesize_body

        bodies = [("scope", "f.md", "A claim.")]
        assert synthesize_body(bodies) == synthesize_body(bodies, None)
        assert "[^" not in synthesize_body(bodies)


# ---------------------------------------------------------------------------
# AC3: the deterministic post-check, surfaced in `athenaeum status`.
# ---------------------------------------------------------------------------


class TestUncitedSentenceGuardrail:
    def _page(self, wiki: Path, name: str, body: str) -> None:
        (wiki / f"{name}.md").write_text(
            f"---\nuid: u-{name}\ntype: concept\nname: {name}\naccess: internal\n---\n\n{body}",
            encoding="utf-8",
        )

    def test_scan_flags_only_pages_over_the_ratio(self, tmp_path: Path) -> None:
        from athenaeum.status import scan_unmarked_sentences

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        self._page(wiki, "uncited", "One. Two. Three.\n")
        self._page(wiki, "cited", "One.[^src-1] Two.[^src-1] Three.[^src-1]\n")
        flagged = scan_unmarked_sentences(wiki, 0.5)
        assert [name for name, _u, _t in flagged] == ["uncited.md"]
        assert flagged[0] == ("uncited.md", 3, 3)

    def test_underscore_and_frontmatterless_files_are_skipped(
        self, tmp_path: Path
    ) -> None:
        """Same exclusions as the page-size scan: ``_``-prefixed operational
        files and anything without a ``name:`` are not entity pages."""
        from athenaeum.status import scan_unmarked_sentences

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "_pending_questions.md").write_text("Uncited. Prose.\n", encoding="utf-8")
        (wiki / "loose.md").write_text("Uncited. Prose.\n", encoding="utf-8")
        assert scan_unmarked_sentences(wiki, 0.0) == []

    def test_a_page_with_no_prose_is_never_flagged(self, tmp_path: Path) -> None:
        """Nothing to cite is not the same condition as cited nothing — even at
        a zero-tolerance threshold."""
        from athenaeum.status import scan_unmarked_sentences

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        self._page(wiki, "stub", "# Stub\n")
        assert scan_unmarked_sentences(wiki, 0.0) == []

    def test_missing_wiki_root_returns_empty(self, tmp_path: Path) -> None:
        from athenaeum.status import scan_unmarked_sentences

        assert scan_unmarked_sentences(tmp_path / "absent", 0.5) == []

    def test_worst_ratio_sorts_first(self, tmp_path: Path) -> None:
        from athenaeum.status import scan_unmarked_sentences

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        self._page(wiki, "half", "One.[^src-1] Two. Three. Four.\n")
        self._page(wiki, "all", "One. Two.\n")
        assert [name for name, _u, _t in scan_unmarked_sentences(wiki, 0.1)] == [
            "all.md",
            "half.md",
        ]

    def test_status_reports_and_formats_the_scan(self, tmp_path: Path) -> None:
        from athenaeum.status import format_status, status

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (tmp_path / "raw").mkdir()
        self._page(wiki, "uncited", "One. Two. Three.\n")
        info = status(tmp_path)
        assert info["pages_unmarked"] == [("uncited.md", 3, 3)]
        rendered = format_status(info)
        assert "Pages over the uncited-sentence ratio: 1" in rendered
        assert "uncited.md (3/3 sentences uncited, 100%)" in rendered

    def test_format_tolerates_a_pre_issue_status_dict(self, tmp_path: Path) -> None:
        """``.get`` default, same as the oversized-page block above it: a status
        dict written before this issue existed still formats, reporting zero."""
        from athenaeum.status import format_status, status

        (tmp_path / "wiki").mkdir()
        (tmp_path / "raw").mkdir()
        info = dict(status(tmp_path))
        info.pop("pages_unmarked")
        assert "Pages over the uncited-sentence ratio: 0" in format_status(info)  # type: ignore[arg-type]

    def test_threshold_honors_config_and_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum.config import resolve_unmarked_sentence_max_ratio

        assert resolve_unmarked_sentence_max_ratio(None) == 0.5
        assert (
            resolve_unmarked_sentence_max_ratio(
                {"librarian": {"unmarked_sentence_max_ratio": 0.25}}
            )
            == 0.25
        )
        monkeypatch.setenv("ATHENAEUM_UNMARKED_SENTENCE_MAX_RATIO", "0.9")
        assert resolve_unmarked_sentence_max_ratio(None) == 0.9

    def test_ratio_of_one_is_the_off_switch(self, tmp_path: Path) -> None:
        """Warn-only means an operator must be able to silence it entirely;
        ``> max_ratio`` is strict, so 1.0 can never fire."""
        from athenaeum.status import scan_unmarked_sentences

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        self._page(wiki, "uncited", "One. Two.\n")
        assert scan_unmarked_sentences(wiki, 1.0) == []


# ---------------------------------------------------------------------------
# AC4: read_entity and recall expose marker -> source, without markdown parsing.
# ---------------------------------------------------------------------------


class TestFootnotesExposedToCallers:
    def test_entity_read_to_dict_carries_a_footnotes_map(self) -> None:
        from athenaeum.pii import EntityRead

        read = EntityRead(
            uid="u-1",
            page_path=Path("wiki/u-1-thing.md"),
            frontmatter={"name": "Thing"},
            body="A claim.[^src-1]\n\n[^src-1]: **Source:** user-stated — `s#1`\n",
            contact={},
            redactions=(),
            contact_included=False,
            contact_record_path=None,
        )
        payload = read.to_dict()
        assert payload["footnotes"] == {"src-1": "**Source:** user-stated — `s#1`"}
        # JSON-serializable, like every other key: this is what the MCP tool returns.
        assert json.loads(json.dumps(payload))["footnotes"]["src-1"].startswith("**Source:**")

    def test_footnotes_is_derived_not_stored(self) -> None:
        """The page markdown stays the single source of truth — the map is a
        resolution step, so a body with no definitions yields an empty map
        rather than a missing key."""
        from athenaeum.pii import EntityRead

        read = EntityRead(
            uid="u-2",
            page_path=Path("wiki/u-2-thing.md"),
            frontmatter={},
            body="No footnotes here.\n",
            contact={},
            redactions=(),
            contact_included=False,
            contact_record_path=None,
        )
        assert read.to_dict()["footnotes"] == {}

    def test_read_entity_tool_documents_the_map(self) -> None:
        from athenaeum.mcp_server import read_entity_tool_docstring

        assert "footnotes" in read_entity_tool_docstring()

    def test_recall_hit_resolves_only_the_markers_its_snippet_cites(self) -> None:
        from athenaeum.mcp_server import _cited_marker_labels

        assert _cited_marker_labels("A claim.[^src-1] Another.[^src-3]") == [
            "src-1",
            "src-3",
        ]
        # A definition is not a citation, and repeats collapse.
        assert _cited_marker_labels("[^src-1]: **Source:** x") == []
        assert _cited_marker_labels("A.[^src-1] B.[^src-1]") == ["src-1"]


# ---------------------------------------------------------------------------
# AC5: the recorded-only eval hook.
# ---------------------------------------------------------------------------


class TestMarkerResolutionEvalHook:
    def _probe_and_corpus(self, page_body: str):
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from evals.corpus import Corpus, Page, Probe

        page = Page(
            uid="page-a",
            type="note",
            name="Page A",
            body=page_body,
            tier="core",
        )
        probe = Probe(
            id="synthetic_marker",
            probe_class="follow_through",
            query="who founded it?",
            expected_uids=("page-a",),
            answer_tokens=("TokenOne",),
        )
        return probe, Corpus(pages=[page], probes=[probe])

    def _record(self, answer: str):
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from evals.rollout import Arm, RolloutRecord, TurnTokenUsage

        return RolloutRecord(
            arm=Arm.ORACLE,
            probe_id="synthetic_marker",
            probe_class="follow_through",
            corpus_scale="core",
            answer=answer,
            turn_tokens=[TurnTokenUsage(turn=1, input_tokens=10, output_tokens=5)],
            recall_called=False,
            turn_count=1,
            transcript=[],
        )

    @property
    def _grade(self):
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from evals.north_star_report import grade_marker_resolution

        return grade_marker_resolution

    def test_cited_marker_that_resolves_grades_true(self) -> None:
        probe, corpus = self._probe_and_corpus(
            "TokenOne is the fact.[^src-1]\n\n[^src-1]: **Source:** user-stated\n"
        )
        assert self._grade(self._record("The answer is TokenOne.[^src-1]"), probe, corpus) is True

    def test_invented_marker_grades_false(self) -> None:
        """A citation-shaped string that resolves to nothing is the failure this
        check exists to catch."""
        probe, corpus = self._probe_and_corpus(
            "TokenOne is the fact.[^src-1]\n\n[^src-1]: **Source:** user-stated\n"
        )
        assert self._grade(self._record("The answer is TokenOne.[^src-9]"), probe, corpus) is False

    def test_an_answer_citing_nothing_is_none_not_false(self) -> None:
        """A model that never cites cannot mis-cite. Grading that ``False``
        would score every arm zero on today's corpus and make the number
        meaningless."""
        probe, corpus = self._probe_and_corpus(
            "TokenOne is the fact.[^src-1]\n\n[^src-1]: **Source:** user-stated\n"
        )
        assert self._grade(self._record("The answer is TokenOne."), probe, corpus) is None

    def test_a_corpus_planting_no_footnotes_is_none(self) -> None:
        """No ground truth to resolve against is 'nothing to grade', which is
        what lets this ship on a corpus that plants no markers yet."""
        probe, corpus = self._probe_and_corpus("TokenOne is the fact.\n")
        assert self._grade(self._record("TokenOne.[^src-1]"), probe, corpus) is None

    def test_it_feeds_no_verdict(self) -> None:
        """Recorded only (issue athenaeum#1791 §2.1): enrolling a mechanism in a
        decision is an explicit operator ruling, never derived here."""
        import inspect
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from evals import north_star_report

        source = inspect.getsource(north_star_report.compute_verdicts)
        assert "marker_resolution" not in source
