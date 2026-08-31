# SPDX-License-Identifier: Apache-2.0
"""tier3_create / tier3_entity_from_text must not mint unusable entity names
(issue athenaeum#1173).

Before this fix, the create path applied no minimum specificity, no
common-word check, and no uniqueness check — it minted names like
"develop", "ready", "claim", "cwc", "yml" and bare issue refs ("#453") that
can never be useful as index keys. :func:`athenaeum.tiers.validate_create_name`
closes the two checks this issue owns (AC1 bare issue refs, AC2 short
all-lowercase tokens); :func:`athenaeum.tiers.gate_create_name_classifications`
wires that check into the SAME classification-time seam
:func:`athenaeum.tiers.partition_code_artifact_classifications` (athenaeum#680)
and :func:`athenaeum.tiers.resolve_address_named_classifications` (athenaeum#1126)
already use, at both transports, before any ``EntityAction`` is built.

One test class per rejection class (issue athenaeum#1173 AC6):

- ``TestValidateCreateName`` — the per-name checks in isolation: bare issue
  ref (AC1), short lowercase token (AC2), a legitimate short CAPITALIZED
  name that must still pass (pinning the AC5 false-positive surface), and
  an ordinary multi-token name unaffected.
- ``TestGateCreateNameClassifications`` — the list-level wiring: rejection,
  escalation, ``is_new=False`` (update) is never touched (scope guard —
  gates creation, not matching), and the athenaeum#1171-style proof that a
  bad sibling is skipped as a single action while a good sibling in the
  SAME call still survives.
- ``TestSyncTransportWiring`` — end to end through
  :func:`athenaeum.librarian.process_one`: an escalated name never becomes
  a page, its sibling create still lands, and the raw file's run completes
  normally (not wedged — no exception, no stuck-file retry).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.librarian import process_one
from athenaeum.models import ClassifiedEntity, EntityIndex, RawFile, TokenUsage
from athenaeum.tiers import (
    DEFAULT_CREATE_NAME_ESCALATE_MAX_CHARS,
    CreateNameEscalatedError,
    CreateNameRejectedError,
    gate_create_name_classifications,
    resolve_create_name_escalate_max_chars,
    validate_create_name,
)

VALID_TYPES = ["person", "company", "concept", "reference"]
VALID_ACCESS = ["open", "internal", "confidential", "personal"]


def _classified(name: str, *, is_new: bool = True, observations: str = "") -> ClassifiedEntity:
    return ClassifiedEntity(
        name=name,
        entity_type="concept",
        tags=[],
        access="internal",
        is_new=is_new,
        existing_uid=None if is_new else "existing-uid",
        observations=observations,
    )


def _raw(raw_dir: Path, content: str, filename: str = "note.md") -> RawFile:
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / filename
    path.write_text(content, encoding="utf-8")
    return RawFile(path=path, source=raw_dir.name, timestamp="", uuid8="")


# ---------------------------------------------------------------------------
# validate_create_name — the single per-name validation point (AC3 seam)
# ---------------------------------------------------------------------------


class TestValidateCreateName:
    def test_bare_issue_ref_is_rejected(self) -> None:
        """AC1: `#453`-shaped names are refused unambiguously."""
        with pytest.raises(CreateNameRejectedError) as exc_info:
            validate_create_name("#453")
        assert exc_info.value.name == "#453"

    @pytest.mark.parametrize("name", ["#453", "#454", "#486"])
    def test_each_named_bare_ref_example_is_rejected(self, name: str) -> None:
        """The issue's own three named bare-ref mints all trip AC1."""
        with pytest.raises(CreateNameRejectedError):
            validate_create_name(name)

    def test_issue_ref_embedded_in_longer_text_is_not_rejected(self) -> None:
        """AC1 is anchored: a name that merely CONTAINS a ref is untouched."""
        validate_create_name("Fix #453 crash")  # must not raise

    @pytest.mark.parametrize("name", ["develop", "ready", "claim", "cwc", "yml"])
    def test_each_named_short_lowercase_example_is_escalated(self, name: str) -> None:
        """AC2: every one of the issue's five named short-lowercase mints
        escalates, never silently mints."""
        with pytest.raises(CreateNameEscalatedError) as exc_info:
            validate_create_name(name)
        assert exc_info.value.name == name

    def test_legitimate_short_capitalized_name_passes(self) -> None:
        """AC5 false-positive pin: a real short, capitalized org name
        (picked from the issue's own "do not ban" list) must NOT be
        escalated or rejected — the guard is shape/case-based, not a
        dictionary check."""
        validate_create_name("Ford")  # must not raise

    def test_ordinary_multi_token_name_is_unaffected(self) -> None:
        """A normal multi-word entity name is exempt outright (whitespace
        alone disqualifies both AC1 and AC2)."""
        validate_create_name("Alice Zhang")  # must not raise

    def test_boundary_is_inclusive_of_seven_chars(self) -> None:
        """DEFAULT_CREATE_NAME_ESCALATE_MAX_CHARS must be 7 (inclusive) so
        the issue's own longest named example, "develop" (7 chars), is
        actually covered by the shipped default."""
        assert DEFAULT_CREATE_NAME_ESCALATE_MAX_CHARS == 7
        assert resolve_create_name_escalate_max_chars(None) == 7
        with pytest.raises(CreateNameEscalatedError):
            validate_create_name("develop")  # exactly 7 chars
        validate_create_name("develops")  # 8 chars: must not raise

    def test_uppercase_short_token_is_never_escalated(self) -> None:
        """A short but NON-lowercase token (an acronym, say) is exempt —
        AC2 is a case-sensitive shape check, never a word-list lookup."""
        validate_create_name("CWC")  # must not raise

    def test_short_alnum_with_no_letters_is_not_escalated(self) -> None:
        """A short token with no alphabetic character at all (e.g. a bare
        number) is not the "generic word" shape AC2 targets."""
        validate_create_name("123")  # must not raise

    def test_no_dictionary_word_list_is_consulted(self) -> None:
        """AC5 regression guard, made explicit: common short dictionary
        words that are legitimate real entities in the issue's own
        accounting must all survive when capitalized — proving the check
        is shape-only, not a word-list membership test."""
        for name in ("Amazon", "Ford", "Gap", "Box", "Docker", "Oracle"):
            validate_create_name(name)  # must not raise for any of these


# ---------------------------------------------------------------------------
# gate_create_name_classifications — the list-level wiring (both transports)
# ---------------------------------------------------------------------------


class TestGateCreateNameClassifications:
    def test_bare_issue_ref_dropped_and_recorded_rejected(self) -> None:
        outcome = gate_create_name_classifications(
            [_classified("#453")], "raw/ref.md", "Some text citing #453."
        )
        assert outcome.kept == []
        assert outcome.rejected == ("#453",)
        assert outcome.escalations == ()

    def test_short_lowercase_name_dropped_and_escalated(self) -> None:
        outcome = gate_create_name_classifications(
            [_classified("develop")], "raw/ref.md", "Something about develop happened."
        )
        assert outcome.kept == []
        assert outcome.rejected == ()
        assert len(outcome.escalations) == 1
        item = outcome.escalations[0]
        assert item.raw_ref == "raw/ref.md"
        assert item.entity_name == "develop"
        assert item.conflict_type == "short_name_escalated"
        assert "develop" in item.description
        assert "Something about develop happened." in item.description

    def test_update_classification_is_never_gated(self) -> None:
        """Scope guard: an is_new=False classification names an EXISTING
        page this file is about to merge into — gating it would be gating
        MATCHING, not creation, which this issue puts explicitly out of
        scope. A name that WOULD trip both AC1 and AC2 must still pass
        through completely unchanged when is_new=False."""
        c = _classified("develop", is_new=False)
        outcome = gate_create_name_classifications([c], "raw/ref.md", "")
        assert outcome.kept == [c]
        assert outcome.rejected == ()
        assert outcome.escalations == ()

    def test_legitimate_short_name_kept_unchanged(self) -> None:
        c = _classified("Ford")
        outcome = gate_create_name_classifications([c], "raw/ref.md", "")
        assert outcome.kept == [c]

    def test_ordinary_multi_token_name_kept_unchanged(self) -> None:
        c = _classified("Alice Zhang")
        outcome = gate_create_name_classifications([c], "raw/ref.md", "")
        assert outcome.kept == [c]

    def test_bad_sibling_skipped_good_sibling_still_lands(self) -> None:
        """athenaeum#1171-pattern proof: rejecting/escalating one
        classification in a raw file's batch must be scoped to THAT
        classification, never the whole file. Two creates for the SAME
        raw file — one bare-issue-ref junk, one ordinary — only the junk
        one is dropped."""
        good = _classified("WidgetGood", observations="Facts about WidgetGood.")
        bad = _classified("#453", observations="Citing #453.")
        outcome = gate_create_name_classifications(
            [bad, good], "raw/ref.md", "Citing #453. Facts about WidgetGood."
        )
        assert outcome.kept == [good]
        assert outcome.rejected == ("#453",)
        assert outcome.escalations == ()

    def test_mixed_rejected_and_escalated_siblings(self) -> None:
        good = _classified("WidgetGood")
        bad_rejected = _classified("#453")
        bad_escalated = _classified("ready")
        outcome = gate_create_name_classifications(
            [bad_rejected, bad_escalated, good], "raw/ref.md", "text"
        )
        assert outcome.kept == [good]
        assert outcome.rejected == ("#453",)
        assert len(outcome.escalations) == 1
        assert outcome.escalations[0].entity_name == "ready"


# ---------------------------------------------------------------------------
# Sync-transport wiring — athenaeum.librarian.process_one
# ---------------------------------------------------------------------------


class TestSyncTransportWiring:
    def test_escalated_name_never_becomes_a_page_sibling_still_created(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        wiki.mkdir(parents=True)
        raw = _raw(
            knowledge / "raw" / "producer",
            "Also file this under develop. WidgetGood shipped today.\n",
        )

        def _fake_tier2_classify(*_args: object, **_kwargs: object) -> list[ClassifiedEntity]:
            return [
                _classified("develop", observations="Also file this under develop."),
                _classified("WidgetGood", observations="WidgetGood shipped today."),
            ]

        monkeypatch.setattr("athenaeum.librarian.tier2_classify", _fake_tier2_classify)

        classify_client = MagicMock()
        classify_client.messages.create.side_effect = AssertionError(
            "tier2_classify is monkeypatched — the real classify client must never be called"
        )

        write_response = MagicMock()
        write_response.content = [
            MagicMock(text="# WidgetGood\n\nShipped today.[^1]\n\n[^1]: producer/note.md")
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

        # The raw file's run completed normally — no exception, i.e. not
        # wedged into a stuck-file retry loop.
        assert [e.name for e in result.created] == ["WidgetGood"]
        assert len(result.escalated) == 1
        assert result.escalated[0].entity_name == "develop"

        # No page named/keyed "develop" was ever written.
        page_names = sorted(p.stem for p in wiki.glob("*.md") if not p.name.startswith("_"))
        assert len(page_names) == 1
        assert page_names[0].endswith("widgetgood")
        assert not any("develop" in n for n in page_names)

        pending = (wiki / "_pending_questions.md").read_text(encoding="utf-8")
        assert "develop" in pending
        assert "Also file this under develop." in pending

        # tier3_create was only ever called once (for the surviving sibling).
        assert write_client.messages.create.call_count == 1

    def test_rejected_bare_ref_name_never_becomes_a_page(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        wiki.mkdir(parents=True)
        raw = _raw(
            knowledge / "raw" / "producer",
            "Citing #453 with no other subject.\n",
        )

        def _fake_tier2_classify(*_args: object, **_kwargs: object) -> list[ClassifiedEntity]:
            return [_classified("#453", observations="Citing #453 with no other subject.")]

        monkeypatch.setattr("athenaeum.librarian.tier2_classify", _fake_tier2_classify)

        classify_client = MagicMock()
        classify_client.messages.create.side_effect = AssertionError(
            "tier2_classify is monkeypatched — the real classify client must never be called"
        )
        write_client = MagicMock()
        write_client.messages.create.side_effect = AssertionError(
            "No actions survive the name gate here — tier-3 must never be called"
        )

        result = process_one(
            raw,
            EntityIndex(wiki),
            wiki,
            classify_client,
            valid_types=VALID_TYPES,
            valid_tags=[],
            valid_access=VALID_ACCESS,
            write_client=write_client,
        )

        assert result.created == []
        # Rejected (not escalated): no pending-question entry, no page.
        assert result.escalated == []
        assert list(wiki.glob("*.md")) == []


# ---------------------------------------------------------------------------
# Config idiom (librarian.create_name_escalate_max_chars)
# ---------------------------------------------------------------------------


class TestResolveCreateNameEscalateMaxChars:
    def test_default_is_seven(self) -> None:
        assert resolve_create_name_escalate_max_chars(None) == 7

    def test_configured_value_is_honored(self) -> None:
        config = {"librarian": {"create_name_escalate_max_chars": 4}}
        assert resolve_create_name_escalate_max_chars(config) == 4
        # "cwc" (3 chars) still escalates under a tighter configured bound...
        with pytest.raises(CreateNameEscalatedError):
            validate_create_name("cwc", config)
        # ...but "ready" (5 chars) no longer does, since 5 > the configured 4.
        validate_create_name("ready", config)

    def test_bool_is_rejected_like_every_other_librarian_int_knob(self) -> None:
        config = {"librarian": {"create_name_escalate_max_chars": True}}
        assert resolve_create_name_escalate_max_chars(config) == 7

    def test_non_positive_value_falls_back_to_default(self) -> None:
        config = {"librarian": {"create_name_escalate_max_chars": 0}}
        assert resolve_create_name_escalate_max_chars(config) == 7
