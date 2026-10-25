"""Issue athenaeum#2112: every site reads ``stop_reason`` BEFORE the text.

PR athenaeum#2111 fixed one call site (``tier3_merge``) and the batched patch-merge
branch beside it. Four more sites read the model's text answer FIRST, so a
thinking-only response cut off at ``max_tokens`` — content made up entirely of
thinking blocks, which carry no ``.text`` at all — raised
``response_text``'s ``ValueError`` out of the stage instead of reaching the
truncation handling each stage already implements:

* the Tier-2 classify response and both of its retries,
* ``tier3_create`` (which passed no ``stop_reason`` at all),
* ``tier3_merge_full``, both the full-echo and the append-mode path,
* the batch finalize loop's create branch.

One test per site for the handled ``max_tokens`` case, and one per site
proving a thinking-only response with any OTHER ``stop_reason`` still fails
loudly rather than becoming a silent empty result.

The shared helper (``athenaeum.provider.text_or_truncated``) is what replaced
the hand-rolled ``try/except ValueError`` at each site, so its own unit tests
lead the file.
"""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from athenaeum.models import EntityAction, EntityIndex, RawFile, TokenUsage
from athenaeum.provider import text_or_truncated
from athenaeum.tiers import (
    MAX_PARKED_OBSERVATION_CHARS,
    TIER3_CREATE_TRUNCATED_MARKER,
    Tier2ParseStats,
    TruncatedCreateResponseError,
    tier2_classify,
    tier2_reclassify_larger_budget,
    tier3_create,
    tier3_derive_actions,
    tier3_merge_full,
    truncated_create_escalation,
)


def _thinking_only(stop_reason: str | None) -> MagicMock:
    """A response whose content is ONE thinking block and nothing else.

    ``spec=["type"]`` is what makes it faithful: a real ``ThinkingBlock`` has
    no ``.text`` attribute at all, which is precisely why ``response_text``
    raises on it. A bare ``MagicMock`` would auto-create ``.text`` and the
    test would prove nothing.
    """
    resp = MagicMock()
    resp.content = [MagicMock(spec=["type"], type="thinking")]
    resp.stop_reason = stop_reason
    return resp


def _text_response(text: str, stop_reason: str = "end_turn") -> MagicMock:
    resp = MagicMock()
    resp.content = [MagicMock(text=text)]
    resp.stop_reason = stop_reason
    return resp


def _make_raw(content: str) -> RawFile:
    return RawFile(
        path=Path("/tmp/fake/sessions/20240407T120000Z-aabb0011.md"),
        source="sessions",
        timestamp="20240407T120000Z",
        uuid8="aabb0011",
        _content=content,
    )


def _create_action(name: str) -> EntityAction:
    return EntityAction(
        kind="create",
        name=name,
        entity_type="concept",
        tags=[],
        access="internal",
        existing_uid=None,
        observations=f"Facts about {name}.",
    )


def _merge_action() -> EntityAction:
    return EntityAction(
        kind="update",
        name="Acme Corp",
        entity_type="company",
        tags=[],
        access="internal",
        existing_uid="uid12345",
        observations="Acme raised a Series C.",
    )


@pytest.fixture(autouse=True)
def _pin_api_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the backend so ``reported_stop_reason`` actually reports.

    ``claude-cli`` declares ``reports_stop_reason=False`` (athenaeum#574), so
    every site under test would see ``None`` and refuse to treat a truncation
    as handled — correct behaviour, but it would make the max_tokens cases
    below vacuous. The env var outranks yaml in ``resolve_provider``, so an
    ambient ``ATHENAEUM_LLM_PROVIDER`` must be pinned, not merely defaulted.
    """
    monkeypatch.setenv("ATHENAEUM_LLM_PROVIDER", "api")


# ---------------------------------------------------------------------------
# The shared helper (AC3): one place, not four copies of the same catch
# ---------------------------------------------------------------------------


class TestTextOrTruncated:
    def test_thinking_only_max_tokens_reads_as_empty(self) -> None:
        assert text_or_truncated(_thinking_only("max_tokens"), "max_tokens") == ""

    def test_text_response_is_returned_unchanged(self) -> None:
        assert text_or_truncated(_text_response("the answer"), "end_turn") == (
            "the answer"
        )

    @pytest.mark.parametrize("stop_reason", ["end_turn", "stop_sequence", None])
    def test_other_stop_reasons_still_raise(self, stop_reason: str | None) -> None:
        # Only an output-budget cutoff is the handled truncation. Anything
        # else with no text block is a genuine error, including the ``None``
        # a backend that cannot report stop_reason yields (athenaeum#574).
        with pytest.raises(ValueError, match="no text block found"):
            text_or_truncated(_thinking_only(stop_reason), stop_reason)

    def test_a_truncated_response_that_did_emit_text_keeps_its_text(self) -> None:
        # The helper must not discard a partial answer: only the
        # no-text-block-at-all case reads as empty.
        partial = _text_response("partial body", stop_reason="max_tokens")
        assert text_or_truncated(partial, "max_tokens") == "partial body"


# ---------------------------------------------------------------------------
# Site 1: Tier-2 classify (tiers.py) — the truncated marker path
# ---------------------------------------------------------------------------


class TestTier2ClassifyThinkingOnly:
    def test_thinking_only_max_tokens_takes_the_truncation_retry(self) -> None:
        # athenaeum#2112 AC1: the first response has no text block, so the
        # old code raised here. It must instead be recorded as a truncation
        # and take the athenaeum#476 larger-budget retry, which recovers.
        raw = _make_raw("Acme Corp raised a Series C.")
        client = MagicMock()
        client.messages.create.side_effect = [
            _thinking_only("max_tokens"),
            _text_response(
                '[{"name": "Acme Corp", "type": "company", "access": "internal", '
                '"tags": [], "observations": "Raised a Series C."}]'
            ),
        ]
        stats = Tier2ParseStats()

        entities = tier2_classify(
            raw, [], ["company"], [], ["internal"], client, stats=stats
        )

        assert [e.name for e in entities] == ["Acme Corp"]
        # The retry recovered, so the truncation recorded on the first
        # attempt is cleared again (tier2_classify's documented behaviour).
        assert stats.truncated == 0
        assert stats.degraded == 0
        assert client.messages.create.call_count == 2

    def test_thinking_only_max_tokens_twice_records_truncation_without_raising(
        self,
    ) -> None:
        # The retry can itself be thinking-only-truncated. The documented
        # outcome is "the truncation stays recorded and the file is
        # preserved for the next run" — never an exception.
        raw = _make_raw("Acme Corp raised a Series C.")
        client = MagicMock()
        client.messages.create.side_effect = [
            _thinking_only("max_tokens"),
            _thinking_only("max_tokens"),
        ]
        stats = Tier2ParseStats()

        entities = tier2_classify(
            raw, [], ["company"], [], ["internal"], client, stats=stats
        )

        assert entities == []
        assert stats.truncated == 1
        assert client.messages.create.call_count == 2

    def test_thinking_only_end_turn_still_raises(self) -> None:
        # AC2: a thinking-only response claiming it finished is a real bug,
        # not an output-budget condition — it must not read as "no entities".
        raw = _make_raw("Acme Corp raised a Series C.")
        client = MagicMock()
        client.messages.create.side_effect = [_thinking_only("end_turn")]
        with pytest.raises(ValueError, match="no text block found"):
            tier2_classify(raw, [], ["company"], [], ["internal"], client)
        assert client.messages.create.call_count == 1


class TestTier2ReclassifyLargerBudgetThinkingOnly:
    """The athenaeum#476 retry itself — reached only once the first response
    truncates, which athenaeum#2112 is what makes reachable at all."""

    def test_thinking_only_max_tokens_records_truncation(self) -> None:
        raw = _make_raw("Acme Corp raised a Series C.")
        client = MagicMock()
        client.messages.create.return_value = _thinking_only("max_tokens")

        entities, stats = tier2_reclassify_larger_budget(
            raw, [], ["company"], [], ["internal"], client, owner=None
        )

        assert entities == []
        assert stats.truncated == 1
        assert stats.degraded == 0

    def test_thinking_only_end_turn_still_raises(self) -> None:
        raw = _make_raw("Acme Corp raised a Series C.")
        client = MagicMock()
        client.messages.create.return_value = _thinking_only("end_turn")
        with pytest.raises(ValueError, match="no text block found"):
            tier2_reclassify_larger_budget(
                raw, [], ["company"], [], ["internal"], client, owner=None
            )


# ---------------------------------------------------------------------------
# Site 2: tier3_create — explicit escalation, no partial page persisted
# ---------------------------------------------------------------------------


class TestTier3CreateThinkingOnly:
    def test_thinking_only_max_tokens_raises_the_truncation_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # AC1 for create: there is no fallback contract behind a create, so
        # the handled outcome is an explicit, named escalation signal —
        # never response_text's bare ValueError (which the caller treats as
        # a whole-raw-file failure) and never an empty persisted page.
        client = MagicMock()
        client.messages.create.return_value = _thinking_only("max_tokens")
        with caplog.at_level("WARNING"):
            with pytest.raises(TruncatedCreateResponseError, match="max_tokens"):
                tier3_create(_create_action("Widget"), "ref", client)
        assert TIER3_CREATE_TRUNCATED_MARKER in caplog.text

    def test_thinking_only_end_turn_still_raises_value_error(self) -> None:
        client = MagicMock()
        client.messages.create.return_value = _thinking_only("end_turn")
        with pytest.raises(ValueError, match="no text block found"):
            tier3_create(_create_action("Widget"), "ref", client)

    def test_truncated_response_that_did_emit_text_still_builds_the_page(
        self,
    ) -> None:
        # Pre-existing behaviour, deliberately unchanged: a partial body is
        # persisted exactly as before. Narrowing that is out of scope here.
        client = MagicMock()
        client.messages.create.return_value = _text_response(
            "# Widget\n\nA partial body.", stop_reason="max_tokens"
        )
        entity = tier3_create(_create_action("Widget"), "ref", client)
        assert entity.body.startswith("# Widget")


class TestTier3DeriveActionsEscalatesATruncatedCreate:
    """The stage-level outcome: per-ACTION, never per-file, plus an
    escalation so the unwritten page is visible for human review.

    Mirrors ``TestTier3DeriveActionsPreambleRejectionIsPerAction`` in
    ``tests/test_tiers.py`` — the truncated action is ordered FIRST, so a
    regression that aborts the loop fails on the unconsumed second mock
    rather than passing silently.
    """

    def test_truncated_create_escalates_and_sibling_still_lands(
        self, wiki_dir: Path
    ) -> None:
        raw = _make_raw("WidgetTruncated and WidgetGood both mentioned.")
        index = EntityIndex(wiki_dir)
        client = MagicMock()
        client.messages.create.side_effect = [
            _thinking_only("max_tokens"),
            _text_response("# WidgetGood\n\nFacts.[^1]"),
        ]

        new_entities, pending_updates, updated_uids, escalations = (
            tier3_derive_actions(
                raw,
                [_create_action("WidgetTruncated"), _create_action("WidgetGood")],
                index,
                wiki_dir,
                client,
                usage=TokenUsage(),
            )
        )

        assert [e.name for e in new_entities] == ["WidgetGood"]
        assert pending_updates == []
        assert updated_uids == []
        assert [(e.entity_name, e.conflict_type) for e in escalations] == [
            ("WidgetTruncated", "principled")
        ]
        assert "max_tokens" in escalations[0].description
        # The raw file is unlinked by the caller once this returns, so the
        # parked observation in the escalation is the last copy of the claim.
        assert "Facts about WidgetTruncated." in escalations[0].description
        assert client.messages.create.call_count == 2


class TestTruncatedCreateEscalationShape:
    def test_names_the_entity_and_the_cause(self) -> None:
        esc = truncated_create_escalation(_create_action("Widget"), "sessions/ref")
        assert esc.raw_ref == "sessions/ref"
        assert esc.entity_name == "Widget"
        assert esc.conflict_type == "principled"
        assert "max_tokens" in esc.description

    def test_carries_the_parked_observation_after_a_blank_line(self) -> None:
        # Seer review on PR #2128: both transports unlink the raw file once
        # the tier-3 pass returns without raising, and a truncated create
        # writes no page — so this escalation is the ONLY surviving copy of
        # the claim. The blank-line convention is what tier4_escalate renders
        # as a fenced ``**Observation**:`` field (athenaeum#2078 AC4); an
        # inline description would lose the text on read-back.
        action = _create_action("Widget")
        esc = truncated_create_escalation(action, "sessions/ref")
        head, blank, tail = esc.description.partition("\n\n")
        assert blank == "\n\n"
        assert tail.strip() == action.observations

    def test_an_empty_observation_parks_nothing(self) -> None:
        # No blank line, so tier4_escalate renders no Observation field at
        # all rather than an empty fence.
        action = _create_action("Widget")
        action.observations = "   "
        esc = truncated_create_escalation(action, "sessions/ref")
        assert "\n\n" not in esc.description

    def test_a_long_observation_is_bounded(self) -> None:
        action = _create_action("Widget")
        action.observations = "x" * (MAX_PARKED_OBSERVATION_CHARS + 500)
        esc = truncated_create_escalation(action, "sessions/ref")
        _head, _blank, tail = esc.description.partition("\n\n")
        assert len(tail.strip()) == MAX_PARKED_OBSERVATION_CHARS


# ---------------------------------------------------------------------------
# Site 3: tier3_merge_full — both the full-echo and the append-mode path
# ---------------------------------------------------------------------------


class TestTier3MergeFullThinkingOnly:
    """``tier3_merge_full`` IS the fallback — nothing sits behind it, so a
    thinking-only truncation here must escalate, not raise."""

    def test_full_echo_thinking_only_max_tokens_escalates(self) -> None:
        client = MagicMock()
        client.messages.create.return_value = _thinking_only("max_tokens")

        body, esc = tier3_merge_full(
            _merge_action(), "# Acme Corp\n\nFintech, Series B.", "ref", client
        )

        assert body is None
        assert esc is not None
        assert esc.conflict_type == "principled"
        assert "max_tokens" in esc.description

    def test_full_echo_thinking_only_end_turn_still_raises(self) -> None:
        client = MagicMock()
        client.messages.create.return_value = _thinking_only("end_turn")
        with pytest.raises(ValueError, match="no text block found"):
            tier3_merge_full(
                _merge_action(), "# Acme Corp\n\nFintech, Series B.", "ref", client
            )

    @staticmethod
    def _oversized_body() -> str:
        """An existing body past the merge input window, which routes
        ``tier3_merge_full`` down the append-mode path (athenaeum#2078)."""
        from athenaeum.tiers import _MAX_EXISTING_BODY_CHARS

        return "# Acme Corp\n\n" + ("Fintech. " * (_MAX_EXISTING_BODY_CHARS // 4))

    def test_append_mode_thinking_only_max_tokens_escalates(self) -> None:
        client = MagicMock()
        client.messages.create.return_value = _thinking_only("max_tokens")

        body, esc = tier3_merge_full(
            _merge_action(), self._oversized_body(), "ref", client
        )

        assert body is None
        assert esc is not None
        assert esc.conflict_type == "principled"
        assert "max_tokens" in esc.description

    def test_append_mode_thinking_only_end_turn_still_raises(self) -> None:
        client = MagicMock()
        client.messages.create.return_value = _thinking_only("end_turn")
        with pytest.raises(ValueError, match="no text block found"):
            tier3_merge_full(
                _merge_action(), self._oversized_body(), "ref", client
            )


# ---------------------------------------------------------------------------
# Site 4: the batch finalize loop's create branch (batch.py)
# ---------------------------------------------------------------------------


class TestBatchCreateBranchThinkingOnly:
    """The batched create branch reads the batch result message's own
    ``stop_reason`` (there is no live response to ask), mirroring the merge
    branch beside it."""

    @staticmethod
    def _entity_from_response(stop_reason: str | None) -> Any:
        from athenaeum.tiers import tier3_entity_from_response

        return tier3_entity_from_response(
            _create_action("Widget"),
            _thinking_only(stop_reason),
            "sessions/ref",
            stop_reason,
        )

    def test_thinking_only_max_tokens_raises_the_truncation_error(self) -> None:
        with pytest.raises(TruncatedCreateResponseError, match="max_tokens"):
            self._entity_from_response("max_tokens")

    def test_thinking_only_end_turn_still_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="no text block found"):
            self._entity_from_response("end_turn")

    # The finalize loop's own per-action catch + escalation is proved
    # end-to-end against a real batch run in
    # ``tests/test_batch_mode.py::TestThinkingOnlyTruncationBatchTransport::
    # test_batched_write_thinking_only_max_tokens_create_escalates`` — that
    # surface needs the batch harness, which lives in that module.
