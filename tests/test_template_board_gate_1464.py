# SPDX-License-Identifier: Apache-2.0
"""`mural-board-summary` must not mint a `type: concept` page per reusable
exercise-TEMPLATE board (issue athenaeum#1464).

Before this fix, the create path had no signal for content genericity: a
template board (the same worksheet re-copied across many engagements) was
classified and created exactly like a board describing a real entity, which
produced near-duplicate `type: concept` page fragmentation with zero
informational content.

The corrected, non-obvious plan the issue insists on: do NOT gate on the
adapter's `template_only` flag (it reads `false` for genuine templates,
since a template is dense with instruction text, not empty) and do NOT add
the check inside `validate_create_name` (classification has already
stripped the "TEMPLATE - " prefix off the minted name by the time a
`ClassifiedEntity` exists, so a name-shape check sees nothing). The gate
instead parses the board's own title straight out of the raw record's YAML
frontmatter and runs as a SIBLING of `gate_create_name_classifications`.

One test class per concern (mirrors tests/test_create_name_gate_1173.py's
own organization):

- ``TestTemplateBoardTitle`` — frontmatter title extraction in isolation,
  including every fail-open shape (no frontmatter, malformed YAML, missing
  or non-string ``name``, a non-dict top-level YAML document).
- ``TestResolveTemplateBoardTitlePattern`` — the config-resolved pattern
  knob: documented default, a valid override, and fail-open fallback for a
  bad type or invalid regex.
- ``TestGateTemplateBoardClassifications`` — the list-level wiring: the
  three acceptance-criteria counter-examples (template-token create is
  suppressed and escalated; a non-matching title is byte-identical;
  is_new=False is never touched), plus the submitter-scope guard and a
  mixed-siblings case.
- ``TestSyncTransportWiring`` / ``TestBatchTransportWiring`` — end to end
  through :func:`athenaeum.librarian.process_one` and
  :func:`athenaeum.batch.process_batch_run`, proving the gate is actually
  wired at both call sites, not just unit-tested in isolation.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.librarian import process_one
from athenaeum.models import ClassifiedEntity, EntityIndex, RawFile, TokenUsage
from athenaeum.tiers import (
    DEFAULT_TEMPLATE_BOARD_TITLE_PATTERN,
    TEMPLATE_BOARD_GATE_SOURCE,
    _template_board_title,
    gate_template_board_classifications,
    resolve_template_board_title_pattern,
)

VALID_TYPES = ["person", "company", "concept", "reference"]
VALID_ACCESS = ["open", "internal", "confidential", "personal"]


def _classified(
    name: str,
    *,
    is_new: bool = True,
    observations: str = "",
    entity_type: str = "concept",
) -> ClassifiedEntity:
    return ClassifiedEntity(
        name=name,
        entity_type=entity_type,
        tags=[],
        access="internal",
        is_new=is_new,
        existing_uid=None if is_new else "existing-uid",
        observations=observations,
    )


def _raw(raw_dir: Path, content: str, filename: str = "board.md") -> RawFile:
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / filename
    path.write_text(content, encoding="utf-8")
    return RawFile(path=path, source=raw_dir.name, timestamp="", uuid8="")


_TEMPLATE_BOARD_CONTENT = (
    "---\n"
    "name: TEMPLATE - Retro Exercise\n"
    "description: A reusable retro worksheet.\n"
    "source: external:mural:board-abc123\n"
    "---\n\n"
    "# TEMPLATE - Retro Exercise\n\n"
    "Instructions: copy this board for your team's retro.\n"
)

_ORDINARY_BOARD_CONTENT = (
    "---\n"
    "name: Q3 Planning - Acme Team\n"
    "description: Acme's Q3 planning session notes.\n"
    "source: external:mural:board-xyz789\n"
    "---\n\n"
    "# Q3 Planning - Acme Team\n\n"
    "Notes from the Q3 planning session.\n"
)


# ---------------------------------------------------------------------------
# _template_board_title — frontmatter extraction in isolation
# ---------------------------------------------------------------------------


class TestTemplateBoardTitle:
    def test_extracts_the_name_field_verbatim(self) -> None:
        assert _template_board_title(_TEMPLATE_BOARD_CONTENT) == "TEMPLATE - Retro Exercise"

    def test_no_frontmatter_block_fails_open(self) -> None:
        assert _template_board_title("Just plain text, no frontmatter at all.\n") is None

    def test_malformed_yaml_fails_open(self) -> None:
        # Unbalanced/invalid YAML in the frontmatter block. parse_frontmatter
        # itself swallows yaml.YAMLError and returns ({}, text); this proves
        # the wrapper does not additionally raise.
        content = "---\nname: [unterminated\n---\n\nBody text.\n"
        assert _template_board_title(content) is None

    def test_missing_name_field_fails_open(self) -> None:
        content = "---\ndescription: no name key here\n---\n\nBody.\n"
        assert _template_board_title(content) is None

    def test_non_string_name_field_fails_open(self) -> None:
        # NOTE: an int-shaped `name` (e.g. `name: 12345`) is coerced to
        # `str` by `parse_frontmatter` itself (models.py's identity-field
        # coercion for `uid`/`type`/`name`) -- this exercises a shape that
        # coercion does NOT cover, a list-valued `name`.
        content = "---\nname:\n  - not\n  - a string\n---\n\nBody.\n"
        assert _template_board_title(content) is None

    def test_empty_string_name_field_fails_open(self) -> None:
        content = '---\nname: ""\n---\n\nBody.\n'
        assert _template_board_title(content) is None

    def test_non_dict_top_level_yaml_fails_open(self) -> None:
        # A frontmatter block whose top-level YAML document is a list, not
        # a mapping -- parse_frontmatter's own return type is documented as
        # dict but is not enforced internally, so this wrapper must guard it.
        content = "---\n- one\n- two\n---\n\nBody.\n"
        assert _template_board_title(content) is None

    def test_empty_raw_content_fails_open(self) -> None:
        assert _template_board_title("") is None


# ---------------------------------------------------------------------------
# resolve_template_board_title_pattern — config-resolved knob
# ---------------------------------------------------------------------------


class TestResolveTemplateBoardTitlePattern:
    def test_default_pattern_with_no_config(self) -> None:
        pattern = resolve_template_board_title_pattern(None)
        assert pattern.pattern == DEFAULT_TEMPLATE_BOARD_TITLE_PATTERN

    def test_default_pattern_matches_all_four_documented_tokens(self) -> None:
        pattern = resolve_template_board_title_pattern(None)
        for title in (
            "TEMPLATE - Retro Exercise",
            "Template - Retro Exercise",
            "EXAMPLE - Retro Exercise",
            "Copy of Retro Exercise",
        ):
            assert pattern.match(title), title

    def test_default_pattern_is_anchored_not_a_substring_search(self) -> None:
        pattern = resolve_template_board_title_pattern(None)
        # The token appears, but not as a LEADING token -- must not match.
        assert not pattern.match("Postmortem Template Feedback Round")
        assert not pattern.match("Q3 Planning - Acme Team")

    def test_configured_override_is_honored(self) -> None:
        config = {"librarian": {"template_board_title_pattern": r"^ARCHIVE:"}}
        pattern = resolve_template_board_title_pattern(config)
        assert pattern.match("ARCHIVE: old board")
        assert not pattern.match("TEMPLATE - Retro Exercise")

    def test_wrong_type_falls_back_to_default(self) -> None:
        config = {"librarian": {"template_board_title_pattern": 123}}
        pattern = resolve_template_board_title_pattern(config)
        assert pattern.pattern == DEFAULT_TEMPLATE_BOARD_TITLE_PATTERN

    def test_empty_string_falls_back_to_default(self) -> None:
        config = {"librarian": {"template_board_title_pattern": ""}}
        pattern = resolve_template_board_title_pattern(config)
        assert pattern.pattern == DEFAULT_TEMPLATE_BOARD_TITLE_PATTERN

    def test_invalid_regex_falls_back_to_default(self) -> None:
        config = {"librarian": {"template_board_title_pattern": "(unbalanced["}}
        pattern = resolve_template_board_title_pattern(config)
        assert pattern.pattern == DEFAULT_TEMPLATE_BOARD_TITLE_PATTERN

    def test_missing_librarian_section_falls_back_to_default(self) -> None:
        pattern = resolve_template_board_title_pattern({"other": {}})
        assert pattern.pattern == DEFAULT_TEMPLATE_BOARD_TITLE_PATTERN


# ---------------------------------------------------------------------------
# gate_template_board_classifications — the list-level wiring
# ---------------------------------------------------------------------------


class TestGateTemplateBoardClassifications:
    def test_template_title_create_is_suppressed_and_escalated(self) -> None:
        """AC3 counter-example: a fixture record whose frontmatter title
        carries a template token produces NO kept create, and DOES produce
        one escalation carrying the record's content."""
        c = _classified("Retro Exercise", observations="Instructions for the retro.")
        outcome = gate_template_board_classifications(
            [c],
            f"{TEMPLATE_BOARD_GATE_SOURCE}/board.md",
            _TEMPLATE_BOARD_CONTENT,
        )
        assert outcome.kept == []
        assert len(outcome.escalations) == 1
        item = outcome.escalations[0]
        assert item.raw_ref == f"{TEMPLATE_BOARD_GATE_SOURCE}/board.md"
        assert item.entity_name == "Retro Exercise"
        assert item.conflict_type == "template_board"
        assert "TEMPLATE - Retro Exercise" in item.description
        assert _TEMPLATE_BOARD_CONTENT[:2000] in item.description or (
            _TEMPLATE_BOARD_CONTENT in item.description
        )

    def test_non_matching_title_is_byte_identical(self) -> None:
        """AC4 counter-example: a fixture record whose title carries no
        token is unaffected -- creates its page exactly as today."""
        c = _classified("Q3 Planning - Acme Team", observations="Q3 planning notes.")
        outcome = gate_template_board_classifications(
            [c],
            f"{TEMPLATE_BOARD_GATE_SOURCE}/board.md",
            _ORDINARY_BOARD_CONTENT,
        )
        assert outcome.kept == [c]
        assert outcome.escalations == ()

    def test_merge_bound_classification_is_never_touched(self) -> None:
        """AC5 counter-example: a record whose title carries a token but
        whose classification is a MERGE (is_new=False) is not touched by
        this gate -- create-bound only, per the precedent's own guard."""
        c = _classified("Retro Exercise", is_new=False)
        outcome = gate_template_board_classifications(
            [c],
            f"{TEMPLATE_BOARD_GATE_SOURCE}/board.md",
            _TEMPLATE_BOARD_CONTENT,
        )
        assert outcome.kept == [c]
        assert outcome.escalations == ()

    def test_non_mural_source_is_unaffected_even_with_matching_title(self) -> None:
        """Scope guard: a record from any OTHER submitter must be returned
        completely unchanged, even if its raw content happens to carry a
        title that would match the pattern -- this gate is scoped to
        `mural-board-summary` records only."""
        c = _classified("Retro Exercise", observations="Instructions for the retro.")
        outcome = gate_template_board_classifications(
            [c],
            "some-other-source/board.md",
            _TEMPLATE_BOARD_CONTENT,
        )
        assert outcome.kept == [c]
        assert outcome.escalations == ()

    def test_no_frontmatter_title_is_unaffected(self) -> None:
        c = _classified("Whatever", observations="No frontmatter at all.")
        outcome = gate_template_board_classifications(
            [c],
            f"{TEMPLATE_BOARD_GATE_SOURCE}/board.md",
            "No frontmatter at all, just plain text.\n",
        )
        assert outcome.kept == [c]
        assert outcome.escalations == ()

    def test_mixed_siblings_only_the_create_bound_one_is_dropped(self) -> None:
        """A template-titled record with both a create-bound and a
        merge-bound sibling classification: only the create is suppressed,
        the merge-bound one survives untouched, in the same call."""
        create_bound = _classified("Retro Exercise")
        merge_bound = _classified("Existing Team", is_new=False)
        outcome = gate_template_board_classifications(
            [create_bound, merge_bound],
            f"{TEMPLATE_BOARD_GATE_SOURCE}/board.md",
            _TEMPLATE_BOARD_CONTENT,
        )
        assert outcome.kept == [merge_bound]
        assert len(outcome.escalations) == 1
        assert outcome.escalations[0].entity_name == "Retro Exercise"

    def test_default_raw_content_argument_fails_open(self) -> None:
        """The function signature defaults raw_content="" (mirrors
        gate_create_name_classifications) -- a caller that omits it must
        not raise, and must be a no-op (no frontmatter to key on)."""
        c = _classified("Retro Exercise")
        outcome = gate_template_board_classifications(
            [c], f"{TEMPLATE_BOARD_GATE_SOURCE}/board.md"
        )
        assert outcome.kept == [c]
        assert outcome.escalations == ()

    def test_config_override_changes_which_titles_match(self) -> None:
        c = _classified("Archived Board")
        config = {"librarian": {"template_board_title_pattern": r"^ARCHIVE:"}}
        content = '---\nname: "ARCHIVE: Old Retro"\n---\n\nBody.\n'
        outcome = gate_template_board_classifications(
            [c], f"{TEMPLATE_BOARD_GATE_SOURCE}/board.md", content, config
        )
        assert outcome.kept == []
        assert len(outcome.escalations) == 1


# ---------------------------------------------------------------------------
# Sync transport wiring (athenaeum.librarian.process_one)
# ---------------------------------------------------------------------------


class TestSyncTransportWiring:
    def test_template_board_create_never_becomes_a_page(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        wiki.mkdir(parents=True)
        raw = _raw(knowledge / "raw" / TEMPLATE_BOARD_GATE_SOURCE, _TEMPLATE_BOARD_CONTENT)

        def _fake_tier2_classify(*_args: object, **_kwargs: object) -> list[ClassifiedEntity]:
            return [
                _classified(
                    "Retro Exercise", observations="Instructions for the retro."
                )
            ]

        monkeypatch.setattr("athenaeum.librarian.tier2_classify", _fake_tier2_classify)

        classify_client = MagicMock()
        classify_client.messages.create.side_effect = AssertionError(
            "tier2_classify is monkeypatched — the real classify client must never be called"
        )
        write_client = MagicMock()
        write_client.messages.create.side_effect = AssertionError(
            "tier3_create is never reached — the classification was suppressed pre-create"
        )

        usage = TokenUsage()
        result = process_one(
            raw,
            EntityIndex(wiki),
            wiki,
            classify_client,
            valid_types=VALID_TYPES,
            valid_tags=[],
            valid_access=VALID_ACCESS,
            usage=usage,
            write_client=write_client,
        )

        assert result.created == []
        assert len(result.escalated) == 1
        assert result.escalated[0].entity_name == "Retro Exercise"
        assert result.escalated[0].conflict_type == "template_board"

        page_names = [p.stem for p in wiki.glob("*.md") if not p.name.startswith("_")]
        assert page_names == []

        pending = (wiki / "_pending_questions.md").read_text(encoding="utf-8")
        assert "Retro Exercise" in pending

    def test_non_template_board_create_is_unaffected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC4, end to end: a mural-board-summary record with an ordinary
        (non-template) title still mints its page exactly as today."""
        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        wiki.mkdir(parents=True)
        raw = _raw(knowledge / "raw" / TEMPLATE_BOARD_GATE_SOURCE, _ORDINARY_BOARD_CONTENT)

        def _fake_tier2_classify(*_args: object, **_kwargs: object) -> list[ClassifiedEntity]:
            return [
                _classified(
                    "Q3 Planning - Acme Team", observations="Q3 planning notes."
                )
            ]

        monkeypatch.setattr("athenaeum.librarian.tier2_classify", _fake_tier2_classify)

        classify_client = MagicMock()
        classify_client.messages.create.side_effect = AssertionError(
            "tier2_classify is monkeypatched — the real classify client must never be called"
        )

        write_response = MagicMock()
        write_response.content = [
            MagicMock(
                text="# Q3 Planning - Acme Team\n\nQ3 planning notes.[^1]\n\n"
                f"[^1]: {TEMPLATE_BOARD_GATE_SOURCE}/board.md"
            )
        ]
        write_client = MagicMock()
        write_client.messages.create.return_value = write_response

        usage = TokenUsage()
        result = process_one(
            raw,
            EntityIndex(wiki),
            wiki,
            classify_client,
            valid_types=VALID_TYPES,
            valid_tags=[],
            valid_access=VALID_ACCESS,
            usage=usage,
            write_client=write_client,
        )

        assert [e.name for e in result.created] == ["Q3 Planning - Acme Team"]
        assert result.escalated == []
        page_names = [p.stem for p in wiki.glob("*.md") if not p.name.startswith("_")]
        assert len(page_names) == 1


# ---------------------------------------------------------------------------
# Batch transport wiring (athenaeum.batch.process_batch_run)
# ---------------------------------------------------------------------------


class TestBatchTransportWiring:
    def test_template_board_create_never_becomes_a_page(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum.batch import process_batch_run

        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        wiki.mkdir(parents=True)
        raw = _raw(knowledge / "raw" / TEMPLATE_BOARD_GATE_SOURCE, _TEMPLATE_BOARD_CONTENT)

        def _fake_tier2_classify(*_args: object, **_kwargs: object) -> list[ClassifiedEntity]:
            return [
                _classified(
                    "Retro Exercise", observations="Instructions for the retro."
                )
            ]

        monkeypatch.setattr("athenaeum.batch.tier2_classify", _fake_tier2_classify)

        classify_client = MagicMock()
        classify_client.messages.create.side_effect = AssertionError(
            "tier2_classify is monkeypatched — the real classify client must never be called"
        )
        write_client = MagicMock()
        write_client.messages.create.side_effect = AssertionError(
            "tier3_create is never reached — the classification was suppressed pre-create"
        )

        result = process_batch_run(
            [raw],
            EntityIndex(wiki),
            wiki,
            classify_client,
            valid_types=VALID_TYPES,
            valid_tags=[],
            valid_access=VALID_ACCESS,
            usage=TokenUsage(),
            config=None,
            max_api_calls=100,
            write_client=write_client,
            batch_classify=False,
            batch_write=False,
        )

        page_names = [p.stem for p in wiki.glob("*.md") if not p.name.startswith("_")]
        assert page_names == []
        assert result.created == 0
        assert result.updated == 0
        assert result.escalated == 1

        pending = (wiki / "_pending_questions.md").read_text(encoding="utf-8")
        assert "Retro Exercise" in pending
