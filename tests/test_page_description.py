# SPDX-License-Identifier: Apache-2.0
"""One-line ``description:`` frontmatter + backfill command (issue athenaeum#1324).

Covers: the write model emits the field and the read model accepts it; the
Tier-3 create path parses the writer's leading ``Description:`` line (and
falls back to the opening paragraph when it is missing); the value's shape
invariant (one line, capped, no quotes/backslashes, no contact data); the
index scanner reads a PyYAML-folded value whole; and the backfill is
dry-run by default, batched, spend-recorded, resumable, never overwrites,
and is a byte-level no-op on a second run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from athenaeum import spend
from athenaeum.cli import main
from athenaeum.models import EntityAction, WikiEntity, parse_frontmatter
from athenaeum.page_description import (
    DESCRIPTION_MAX_CHARS,
    apply_description_backfill,
    build_description_report,
    derive_description_from_body,
    describe_pages,
    insert_description,
    normalize_description,
    split_description_line,
)
from athenaeum.search import _extract_frontmatter_fields
from athenaeum.tiers import tier3_entity_from_text
from tests.conftest import FakeLLMClient, make_llm_response, make_llm_usage


def _page(root: Path, name: str, frontmatter: str, body: str = "Body text.\n") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")
    return path


@pytest.fixture
def wiki(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge" / "wiki"
    root.mkdir(parents=True)
    return root


# --- Write model + read model ----------------------------------------------


class TestWriterField:
    def test_render_emits_description_after_name_block(self) -> None:
        entity = WikiEntity(
            uid="20001", type="concept", name="Sample", description="A sample concept."
        )
        meta, _body = parse_frontmatter(entity.render())
        assert meta["description"] == "A sample concept."

    def test_absent_description_renders_no_key(self) -> None:
        entity = WikiEntity(uid="20002", type="concept", name="Sample")
        assert "description:" not in entity.render()

    def test_read_model_accepts_the_field(self) -> None:
        from athenaeum.schemas import WikiBase

        entity = WikiEntity(uid="20003", type="tool", name="Sample", description="x")
        meta, _body = parse_frontmatter(entity.render())
        assert WikiBase(**meta).model_dump()["description"] == "x"

    def test_index_scanner_reads_a_folded_long_value_whole(self) -> None:
        """PyYAML folds plain scalars past ~80 columns; the FTS5 scanner is
        line-based and used to keep only the first line."""
        long = (
            "A deliberately long summary sentence that PyYAML will fold across "
            "two lines when it renders the frontmatter block."
        )
        entity = WikiEntity(uid="20004", type="tool", name="Sample", description=long)
        text = entity.render()
        assert "\n  " in text.split("---")[1]  # it really did fold
        _name, _tags, _aliases, description = _extract_frontmatter_fields(text)
        assert description == long

    def test_index_scanner_keeps_an_interior_apostrophe_in_a_folded_value(self) -> None:
        """Quote delimiters are stripped once over the joined value, never per
        continuation line, so a line ending in an apostrophe survives."""
        text = "---\nname: Sample\ndescription: \"Kromatic's\n  partners' pilot\n  ran in 2026\"\n---\nBody.\n"
        _name, _tags, _aliases, description = _extract_frontmatter_fields(text)
        assert description == "Kromatic's partners' pilot ran in 2026"


# --- Tier-3 create path -----------------------------------------------------


def _action() -> EntityAction:
    return EntityAction(
        kind="create",
        name="Zorblax Corp",
        entity_type="company",
        tags=["x"],
        access="internal",
        existing_uid=None,
        observations="obs",
    )


class TestCreatePath:
    def test_leading_description_line_is_parsed_off_the_body(self) -> None:
        text = (
            "Description: Fictional widget vendor piloting with Kromatic in 2026.\n\n"
            "# Zorblax Corp\n\nZorblax makes widgets.[^1]\n\n[^1]: src\n"
        )
        entity = tier3_entity_from_text(_action(), text)
        assert entity.description == "Fictional widget vendor piloting with Kromatic in 2026."
        assert entity.body.startswith("# Zorblax Corp")
        assert "Description:" not in entity.body

    def test_bold_label_and_case_are_tolerated(self) -> None:
        text = "**description**: Widget vendor.\n# Zorblax Corp\nBody.\n"
        entity = tier3_entity_from_text(_action(), text)
        assert entity.description == "Widget vendor."
        assert entity.body.startswith("# Zorblax Corp")

    def test_missing_line_falls_back_to_opening_paragraph(self) -> None:
        text = (
            "# Zorblax Corp\n\nZorblax is a widget vendor.[^1] It is based in Utrecht.\n\n"
            "[^1]: src\n"
        )
        entity = tier3_entity_from_text(_action(), text)
        assert entity.description == "Zorblax is a widget vendor. It is based in Utrecht."
        assert entity.body == text.strip()

    def test_description_inside_the_body_is_not_hoisted(self) -> None:
        text = "# Zorblax Corp\n\nDescription: this is body prose, not the lead line.\n"
        entity = tier3_entity_from_text(_action(), text)
        assert "Description: this is body prose" in entity.body

    def test_preamble_after_description_line_is_still_stripped(self) -> None:
        text = (
            "Description: Widget vendor.\n\n"
            "Looking at the observation, I need to write the page.\n\n"
            "# Zorblax Corp\n\nZorblax makes widgets.\n"
        )
        entity = tier3_entity_from_text(_action(), text)
        assert entity.description == "Widget vendor."
        assert entity.body.startswith("# Zorblax Corp")


# --- Shape invariant --------------------------------------------------------


class TestNormalize:
    def test_one_line_no_quotes_no_backslashes(self) -> None:
        assert (
            normalize_description('  A "quoted"\nmulti\\line\tvalue  ')
            == "A 'quoted' multiline value"
        )

    def test_capped_on_word_boundary(self) -> None:
        value = normalize_description("word " * 100)
        assert value is not None
        assert len(value) <= DESCRIPTION_MAX_CHARS
        assert value.endswith("…")
        assert " wor…" not in value

    def test_contact_data_is_scrubbed(self) -> None:
        value = normalize_description("CEO, reach at jane@example.com or +31 6 1234 5678 today")
        assert value == "CEO, reach at or today"

    def test_empty_and_non_string_are_none(self) -> None:
        assert normalize_description("") is None
        assert normalize_description('""') is None
        assert normalize_description(42) is None


class TestSplitAndDerive:
    def test_no_lead_line_returns_text_unchanged(self) -> None:
        assert split_description_line("# H\nbody") == (None, "# H\nbody")

    def test_derive_skips_headings_footnotes_and_tables(self) -> None:
        body = (
            "# Title\n\n| a | b |\n|---|---|\n\n[^1]: src\n\n"
            "First **real** paragraph [link](http://x) here.[^1]\nContinues.\n\nSecond para.\n"
        )
        assert derive_description_from_body(body) == "First real paragraph link here. Continues."

    def test_derive_keeps_whole_sentences_under_the_cap(self) -> None:
        first = "Sentence one is short."
        second = "Sentence two is " + "very " * 60 + "long."
        assert derive_description_from_body(f"# T\n\n{first} {second}\n") == first

    def test_derive_returns_none_without_prose(self) -> None:
        assert derive_description_from_body("# Title\n\n| a |\n|---|\n") is None


# --- Backfill ---------------------------------------------------------------


class TestBackfill:
    def test_mechanical_dry_run_writes_nothing(self, wiki: Path) -> None:
        path = _page(wiki, "a.md", "uid: a\nname: A\ntype: tool", "# A\n\nA is a tool.\n")
        before = path.read_text()
        report = build_description_report(wiki, mechanical=True)
        assert report.counts_by_reason() == {"mechanical": 1}
        assert report.assignments[0].description == "A is a tool."
        assert path.read_text() == before

    def test_apply_inserts_one_line_and_is_idempotent(self, wiki: Path) -> None:
        path = _page(wiki, "a.md", "uid: a\nname: A\ntype: tool", "# A\n\nA is a tool.\n")
        report = build_description_report(wiki, mechanical=True)
        assert apply_description_backfill(report) == 1
        first = path.read_text()
        assert 'description: "A is a tool."\n---\n' in first
        meta, _ = parse_frontmatter(first)
        assert meta["description"] == "A is a tool."
        # Second run: already-described, zero writes, zero bytes changed.
        report2 = build_description_report(wiki, mechanical=True)
        assert report2.counts_by_reason() == {"already-described": 1}
        assert apply_description_backfill(report2) == 0
        assert path.read_text() == first

    def test_never_overwrites_and_never_fabricates_frontmatter(self, wiki: Path) -> None:
        kept = _page(wiki, "k.md", 'uid: k\nname: K\ndescription: "Keep me"', "# K\n\nNew text.\n")
        bare = wiki / "bare.md"
        bare.write_text("# No frontmatter\n\nProse.\n")
        retired = _page(wiki, "r.md", "uid: r\nname: R\nretired: true", "# R\n\nOld.\n")
        report = build_description_report(wiki, mechanical=True)
        assert report.counts_by_reason() == {
            "already-described": 1,
            "no-frontmatter": 1,
            "retired": 1,
        }
        assert apply_description_backfill(report) == 0
        assert "Keep me" in kept.read_text()
        assert not bare.read_text().startswith("---")
        assert "description" not in retired.read_text()

    def test_thin_body_is_skipped_before_any_llm_call(self, wiki: Path) -> None:
        """A heading plus a footnote (the CRM person-stub shape) has nothing to
        summarize; asking the model every pass would waste tokens and clog a
        ``limit`` window with stubs."""
        _page(
            wiki, "stub.md", "uid: s\nname: Dawn B\ntype: person", "# Dawn B\n\n[^1]: CRM export.\n"
        )
        client = FakeLLMClient(response=make_llm_response("[]", make_llm_usage(1, 1)))
        report = build_description_report(wiki, client=client)
        assert report.counts_by_reason() == {"thin-body": 1}
        assert client.calls == []

    def test_limit_makes_successive_runs_drain(self, wiki: Path) -> None:
        for i in range(3):
            _page(wiki, f"p{i}.md", f"uid: p{i}\nname: P{i}", f"# P{i}\n\nPage {i} prose.\n")
        r1 = build_description_report(wiki, mechanical=True, limit=2)
        assert r1.counts_by_reason() == {"mechanical": 2, "undecided": 1}
        assert apply_description_backfill(r1) == 2
        r2 = build_description_report(wiki, mechanical=True, limit=2)
        assert r2.counts_by_reason() == {"already-described": 2, "mechanical": 1}
        assert apply_description_backfill(r2) == 1

    def test_llm_mode_batches_records_spend_and_normalizes(
        self, wiki: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for i in range(3):
            _page(wiki, f"p{i}.md", f"uid: p{i}\nname: P{i}\ntype: tool", f"# P{i}\n\nTool {i}.\n")
        answer = json.dumps(
            [
                {"i": 0, "description": 'Tool zero, "quoted".'},
                {"i": 1, "description": ""},
                {"i": 7, "description": "out of range"},
                {"i": 2, "description": "Tool two."},
            ]
        )
        client = FakeLLMClient(response=make_llm_response(answer, make_llm_usage(300, 40)))
        recorded: list[dict[str, object]] = []
        monkeypatch.setattr(
            spend, "record_spend", lambda usage, **kw: recorded.append({"usage": usage, **kw})
        )
        monkeypatch.setattr(spend, "ceiling_tripped", lambda *a, **k: None)

        decisions, calls = describe_pages(
            [(p, {"name": p.stem}, "body") for p in sorted(wiki.glob("*.md"))],
            client=client,
            batch_size=20,
            wiki_root=wiki,
        )
        assert calls == 1
        assert len(client.calls) == 1
        assert decisions == {
            wiki / "p0.md": "Tool zero, 'quoted'.",
            wiki / "p2.md": "Tool two.",
        }
        assert len(recorded) == 1
        assert recorded[0]["run_type"] == spend.RUN_TYPE_DESCRIPTION_BACKFILL
        assert recorded[0]["wiki_root"] == wiki

    def test_llm_mode_stops_when_ceiling_trips(
        self, wiki: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = FakeLLMClient(response=make_llm_response("[]", make_llm_usage(1, 1)))
        monkeypatch.setattr(spend, "ceiling_tripped", lambda *a, **k: "per-run cap")
        decisions, calls = describe_pages([(wiki / "x.md", {"name": "x"}, "body")], client=client)
        assert decisions == {} and calls == 0 and client.calls == []

    def test_report_without_client_marks_llm_unavailable(self, wiki: Path) -> None:
        _page(wiki, "a.md", "uid: a\nname: A", "# A\n\nProse.\n")
        report = build_description_report(wiki, mechanical=False, client=None)
        assert report.llm_available is False
        assert report.counts_by_reason() == {"undecided": 1}

    def test_insert_returns_none_without_frontmatter(self) -> None:
        assert insert_description("# bare\n", "x") is None


class TestCli:
    def test_dry_run_default_then_apply(
        self, tmp_path: Path, wiki: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = _page(wiki, "a.md", "uid: a\nname: A", "# A\n\nA is a page.\n")
        base = ["description", "backfill", "--path", str(wiki.parent), "--mechanical"]
        assert main([*base, "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["applied"] is False and payload["assigned"] == 1
        assert "description" not in path.read_text()
        assert main([*base, "--apply", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["applied"] is True and payload["files_changed"] == 1
        assert 'description: "A is a page."' in path.read_text()

    def test_dry_run_flag_overrides_apply(
        self, wiki: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = _page(wiki, "a.md", "uid: a\nname: A", "# A\n\nA is a page.\n")
        assert (
            main(
                [
                    "description",
                    "backfill",
                    "--path",
                    str(wiki.parent),
                    "--mechanical",
                    "--apply",
                    "--dry-run",
                ]
            )
            == 0
        )
        assert "description" not in path.read_text()
        assert "dry run" in capsys.readouterr().out
