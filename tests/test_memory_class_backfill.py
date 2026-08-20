# SPDX-License-Identifier: Apache-2.0
"""memory_class writer field + backfill command (issue athenaeum#996).

Covers the five ACs: the write model emits the field (create + update),
the mechanical rule map is exact and idempotent, the classifier residual
runs batched against a stubbed client and can never mint ``axiom``, and
frontmatter-less pages are skipped-and-counted rather than synthesized.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from athenaeum.cli import main
from athenaeum.memory_class_backfill import (
    apply_backfill,
    build_backfill_report,
    discover_wiki_pages,
    insert_memory_class,
)
from athenaeum.models import WikiEntity, parse_frontmatter
from athenaeum.schemas import (
    MACHINE_ASSIGNABLE_MEMORY_CLASSES,
    MEMORY_CLASSES,
    TYPE_TO_MEMORY_CLASS,
    memory_class_for_type,
)
from tests.conftest import FakeLLMClient


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


# --- AC1: the write model carries and emits memory_class -------------------


class TestWriterField:
    def test_render_emits_explicit_memory_class(self) -> None:
        entity = WikiEntity(
            uid="10001",
            type="concept",
            name="Sample Concept",
            memory_class="guideline",
        )
        meta, _body = parse_frontmatter(entity.render())
        assert meta["memory_class"] == "guideline"

    def test_render_round_trips_through_the_read_model(self) -> None:
        """AC1's "accepted end-to-end": what render writes, WikiBase reads."""
        from athenaeum.schemas import WikiBase

        entity = WikiEntity(uid="10002", type="reference", name="Sample Reference")
        meta, _body = parse_frontmatter(entity.render())
        assert WikiBase(**meta).memory_class == "reference"

    def test_absent_value_is_derived_from_type(self) -> None:
        """A create path that knows nothing of the taxonomy still lands classed.

        This is the whole point of athenaeum#996 over a one-off backfill: coverage
        would otherwise decay at the new-page rate.
        """
        entity = WikiEntity(uid="10003", type="person", name="Sample Person")
        assert entity.memory_class == "entity"
        assert "memory_class: entity" in entity.render()

    def test_unmapped_type_emits_no_key(self) -> None:
        entity = WikiEntity(uid="10004", type="auto-memory", name="Sample Memory")
        assert entity.memory_class is None
        meta, _body = parse_frontmatter(entity.render())
        assert "memory_class" not in meta

    def test_explicit_value_beats_the_rule(self) -> None:
        entity = WikiEntity(
            uid="10005", type="person", name="Sample Person", memory_class="fact"
        )
        assert entity.memory_class == "fact"

    def test_unknown_value_warns_but_survives(self) -> None:
        """Mirrors WikiBase's warn-and-keep, not a silent drop."""
        with pytest.warns(UserWarning, match="unknown memory_class"):
            entity = WikiEntity(
                uid="10006",
                type="person",
                name="Sample Person",
                memory_class="nonsense",
            )
        assert "memory_class: nonsense" in entity.render()

    def test_rule_map_never_targets_axiom(self) -> None:
        assert "axiom" not in set(TYPE_TO_MEMORY_CLASS.values())
        assert "axiom" not in MACHINE_ASSIGNABLE_MEMORY_CLASSES
        assert MACHINE_ASSIGNABLE_MEMORY_CLASSES == MEMORY_CLASSES - {"axiom"}

    @pytest.mark.parametrize(
        ("page_type", "expected"),
        [
            ("person", "entity"),
            ("company", "entity"),
            ("concept", "entity"),
            ("tool", "entity"),
            ("project", "entity"),
            ("source", "entity"),
            ("user", "entity"),
            ("reference", "reference"),
            ("principle", "guideline"),
            ("auto-memory", None),
            ("preference", None),
            ("", None),
            (None, None),
        ],
    )
    def test_rule_map_is_exactly_as_adopted(
        self, page_type: object, expected: str | None
    ) -> None:
        assert memory_class_for_type(page_type) == expected


# --- AC2: the mechanical backfill ------------------------------------------


class TestMechanicalBackfill:
    def test_assigns_by_rule_and_reports_counts_by_class(self, wiki: Path) -> None:
        _page(wiki, "a.md", "uid: '1'\ntype: person\nname: A")
        _page(wiki, "b.md", "uid: '2'\ntype: principle\nname: B")
        _page(wiki, "c.md", "uid: '3'\ntype: reference\nname: C")

        report = build_backfill_report(wiki)

        assert report.scanned == 3
        assert report.counts_by_class() == {"entity": 1, "guideline": 1, "reference": 1}

    def test_dry_run_writes_nothing(self, wiki: Path) -> None:
        path = _page(wiki, "a.md", "uid: '1'\ntype: person\nname: A")
        before = path.read_text(encoding="utf-8")

        build_backfill_report(wiki)

        assert path.read_text(encoding="utf-8") == before

    def test_apply_then_reapply_is_byte_identical(self, wiki: Path) -> None:
        """Idempotence at the BYTE level, not merely the field level.

        Rich frontmatter on purpose: a parse -> render round trip would
        reflow ``related``/``field_sources`` and make run 2 differ from run 1
        on keys this pass never meant to touch.
        """
        rich = (
            "uid: '1'\n"
            "type: person\n"
            "name: Sample Person\n"
            "aliases:\n"
            "  - sample-person\n"
            "access: internal\n"
            "related:\n"
            "  - name: Sample Company\n"
            "    relation: works_at\n"
            "source: 'conversation:sample-ref'\n"
            "field_sources:\n"
            "  name: 'conversation:sample-ref'\n"
        )
        path = _page(wiki, "a.md", rich.rstrip("\n"))
        original = path.read_text(encoding="utf-8")

        apply_backfill(build_backfill_report(wiki))
        after_first = path.read_text(encoding="utf-8")

        # Exactly one added line, everything else byte-for-byte.
        assert after_first != original
        assert after_first.replace("memory_class: entity\n", "") == original
        assert "aliases:\n  - sample-person" in after_first

        apply_backfill(build_backfill_report(wiki))
        assert path.read_text(encoding="utf-8") == after_first

    def test_never_overwrites_an_existing_value(self, wiki: Path) -> None:
        path = _page(
            wiki, "a.md", "uid: '1'\ntype: person\nname: A\nmemory_class: decision"
        )

        report = build_backfill_report(wiki)
        apply_backfill(report)

        assert report.counts_by_reason()["already-classed"] == 1
        assert "memory_class: decision" in path.read_text(encoding="utf-8")
        assert "memory_class: entity" not in path.read_text(encoding="utf-8")

    def test_unknown_type_is_reported_not_guessed(self, wiki: Path) -> None:
        _page(wiki, "a.md", "uid: '1'\ntype: spaceship\nname: A")

        report = build_backfill_report(wiki)

        assert report.counts_by_reason() == {"unmapped-type": 1}
        assert report.assignments == []

    def test_infra_ledgers_are_not_pages(self, wiki: Path) -> None:
        _page(wiki, "_pending_questions.md", "uid: '1'\ntype: person\nname: A")
        assert discover_wiki_pages(wiki) == []

    def test_makes_no_llm_calls(self, wiki: Path) -> None:
        _page(wiki, "a.md", "uid: '1'\ntype: person\nname: A")
        client = FakeLLMClient(text="[]")

        build_backfill_report(wiki, client=client)

        assert client.calls == []


# --- AC4: pages with no frontmatter ----------------------------------------


class TestFrontmatterlessPages:
    def test_skipped_counted_and_left_untouched(self, wiki: Path) -> None:
        bare = wiki / "bare.md"
        bare.write_text("Just prose, no frontmatter at all.\n", encoding="utf-8")

        report = build_backfill_report(wiki)
        apply_backfill(report)

        assert report.counts_by_reason() == {"no-frontmatter": 1}
        assert (
            bare.read_text(encoding="utf-8") == "Just prose, no frontmatter at all.\n"
        )

    def test_malformed_yaml_is_a_distinct_skip_not_a_crash(self, wiki: Path) -> None:
        path = _page(wiki, "broken.md", "uid: '1'\n  type: [unclosed\nname: A")
        original = path.read_text(encoding="utf-8")

        report = build_backfill_report(wiki)
        apply_backfill(report)

        assert report.counts_by_reason() == {"unparseable-frontmatter": 1}
        assert path.read_text(encoding="utf-8") == original

    def test_empty_frontmatter_is_not_reported_as_unparseable(self, wiki: Path) -> None:
        """A blank block loads fine; naming it "unparseable" sends an operator
        hunting a YAML bug that does not exist."""
        path = wiki / "empty.md"
        path.write_text("---\n\n---\nBody.\n", encoding="utf-8")
        original = path.read_text(encoding="utf-8")

        report = build_backfill_report(wiki)
        apply_backfill(report)

        assert report.counts_by_reason() == {"empty-frontmatter": 1}
        assert path.read_text(encoding="utf-8") == original

    def test_insert_refuses_a_page_without_frontmatter(self) -> None:
        assert insert_memory_class("no frontmatter here\n", "entity") is None


# --- AC3: the classifier-assisted residual ---------------------------------


class TestClassifierResidual:
    def test_residual_is_undecided_without_the_classifier(self, wiki: Path) -> None:
        _page(wiki, "m.md", "uid: '1'\ntype: auto-memory\nname: M")

        report = build_backfill_report(wiki)

        assert report.counts_by_reason() == {"residual-undecided": 1}

    def test_stubbed_client_decides_the_residual(self, wiki: Path) -> None:
        _page(wiki, "m.md", "uid: '1'\ntype: auto-memory\nname: M")
        _page(wiki, "p.md", "uid: '2'\ntype: preference\nname: P")
        client = FakeLLMClient(
            text=json.dumps(
                [
                    {"i": 0, "memory_class": "fact"},
                    {"i": 1, "memory_class": "guideline"},
                ]
            )
        )

        report = build_backfill_report(wiki, use_classifier=True, client=client)

        assert report.classifier_calls == 1
        assert report.counts_by_class() == {"fact": 1, "guideline": 1}

    def test_calls_are_batched(self, wiki: Path) -> None:
        for i in range(5):
            _page(wiki, f"m{i}.md", f"uid: '{i}'\ntype: auto-memory\nname: M{i}")
        client = FakeLLMClient(text="[]")

        report = build_backfill_report(
            wiki, use_classifier=True, client=client, batch_size=2
        )

        assert report.classifier_calls == 3  # 2 + 2 + 1, not 5
        assert len(client.calls) == 3

    def test_untyped_but_frontmattered_pages_reach_the_classifier(
        self, wiki: Path
    ) -> None:
        _page(wiki, "u.md", "uid: '1'\nname: U")
        client = FakeLLMClient(text=json.dumps([{"i": 0, "memory_class": "fact"}]))

        report = build_backfill_report(wiki, use_classifier=True, client=client)

        assert report.counts_by_class() == {"fact": 1}

    def test_axiom_from_the_model_is_refused_and_nothing_is_written(
        self, wiki: Path
    ) -> None:
        """Enforcement, not prompt wording — athenaeum#434 owns axiom promotion."""
        path = _page(wiki, "m.md", "uid: '1'\ntype: auto-memory\nname: M")
        original = path.read_text(encoding="utf-8")
        client = FakeLLMClient(text=json.dumps([{"i": 0, "memory_class": "axiom"}]))

        report = build_backfill_report(wiki, use_classifier=True, client=client)
        changed = apply_backfill(report)

        assert report.classifier_rejected == 1
        assert report.counts_by_reason() == {"residual-undecided": 1}
        assert changed == 0
        assert path.read_text(encoding="utf-8") == original

    def test_out_of_taxonomy_answer_is_refused(self, wiki: Path) -> None:
        _page(wiki, "m.md", "uid: '1'\ntype: auto-memory\nname: M")
        client = FakeLLMClient(text=json.dumps([{"i": 0, "memory_class": "vibes"}]))

        report = build_backfill_report(wiki, use_classifier=True, client=client)

        assert report.classifier_rejected == 1
        assert report.assignments == []

    def test_a_failing_batch_does_not_abort_the_pass(self, wiki: Path) -> None:
        _page(wiki, "m.md", "uid: '1'\ntype: auto-memory\nname: M")
        client = FakeLLMClient(raises=RuntimeError("upstream 500"))

        report = build_backfill_report(wiki, use_classifier=True, client=client)

        assert report.classifier_calls == 0
        assert report.counts_by_reason() == {"residual-undecided": 1}

    def test_retired_pages_are_excluded_by_default(self, wiki: Path) -> None:
        _page(wiki, "m.md", "uid: '1'\ntype: auto-memory\nname: M\nretired: true")
        client = FakeLLMClient(text="[]")

        report = build_backfill_report(wiki, use_classifier=True, client=client)

        assert report.counts_by_reason() == {"retired": 1}
        assert client.calls == []

    def test_include_retired_opts_them_back_in(self, wiki: Path) -> None:
        _page(wiki, "m.md", "uid: '1'\ntype: auto-memory\nname: M\nretired: true")
        client = FakeLLMClient(text=json.dumps([{"i": 0, "memory_class": "fact"}]))

        report = build_backfill_report(
            wiki, use_classifier=True, client=client, include_retired=True
        )

        assert report.counts_by_class() == {"fact": 1}


# --- CLI surface -----------------------------------------------------------


class TestCli:
    def test_dry_run_json_reports_counts(
        self, wiki: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _page(wiki, "a.md", "uid: '1'\ntype: person\nname: A")
        knowledge_root = wiki.parent

        rc = main(["memory-class", "backfill", "--path", str(knowledge_root), "--json"])

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["counts_by_class"] == {"entity": 1}
        assert payload["applied"] is False
        assert payload["files_changed"] == 0

    def test_apply_writes(self, wiki: Path, capsys: pytest.CaptureFixture[str]) -> None:
        path = _page(wiki, "a.md", "uid: '1'\ntype: person\nname: A")

        rc = main(
            [
                "memory-class",
                "backfill",
                "--path",
                str(wiki.parent),
                "--apply",
                "--json",
            ]
        )

        assert rc == 0
        assert json.loads(capsys.readouterr().out)["files_changed"] == 1
        assert "memory_class: entity" in path.read_text(encoding="utf-8")

    def test_dry_run_flag_overrides_apply(self, wiki: Path) -> None:
        """Safe mode wins when both are given."""
        path = _page(wiki, "a.md", "uid: '1'\ntype: person\nname: A")
        original = path.read_text(encoding="utf-8")

        rc = main(
            [
                "memory-class",
                "backfill",
                "--path",
                str(wiki.parent),
                "--apply",
                "--dry-run",
            ]
        )

        assert rc == 0
        assert path.read_text(encoding="utf-8") == original

    def test_missing_subcommand_prints_usage(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["memory-class"]) == 2
        assert "usage: athenaeum memory-class backfill" in capsys.readouterr().err

    def test_missing_wiki_directory_is_an_error(self, tmp_path: Path) -> None:
        assert main(["memory-class", "backfill", "--path", str(tmp_path)]) == 1
