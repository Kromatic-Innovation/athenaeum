# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#1182 — the page-size invariant.

Atomic pages must not be merged into indefinitely: a page whose existing
body crosses ``librarian.page_size_threshold_chars`` must be refused a
Tier-3 merge BEFORE the merge prompt is built or any model call is made,
and must route to the shipped ``review`` action (escalate, leave the page
unmodified) rather than accepting another merge.

This suite covers:
  - the config resolvers (``librarian.page_size_threshold_chars`` /
    ``librarian.oversize_page_action``), mirroring the athenaeum#1168
    mention-density resolvers' validation contract exactly;
  - ``check_page_size_gate`` itself (under/at/over threshold, the shipped
    "review" action, and the reserved "split"/"log_demote" actions raising
    NotImplementedError instead of silently falling back);
  - the real dispatch site, ``tier3_derive_actions``'s "update" branch —
    proving the suppression is REAL: no LLM call is made, and the page's
    pending_updates/updated_uids stay empty;
  - the run-summary counter (``ProcessingResult.oversize_suppressed``, via
    ``athenaeum.librarian._apply_tier3_results``);
  - the read-only AC3 enumeration helper, ``enumerate_oversize_pages``.

No LLM, no network.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.librarian import _apply_tier3_results
from athenaeum.models import EntityAction, EscalationItem, ProcessingResult, RawFile
from athenaeum.tiers import (
    DEFAULT_OVERSIZE_PAGE_ACTION,
    DEFAULT_PAGE_SIZE_THRESHOLD_CHARS,
    VALID_OVERSIZE_PAGE_ACTIONS,
    check_page_size_gate,
    enumerate_oversize_pages,
    resolve_oversize_page_action,
    resolve_page_size_threshold_chars,
    tier3_derive_actions,
)


def _make_raw(content: str) -> RawFile:
    return RawFile(
        path=Path("/tmp/fake/sessions/20240407T120000Z-aabb0011.md"),
        source="sessions",
        timestamp="20240407T120000Z",
        uuid8="aabb0011",
        _content=content,
    )


def _update_action(name: str = "Acme Corp", existing_uid: str = "a1b2c3d4") -> EntityAction:
    return EntityAction(
        kind="update",
        name=name,
        entity_type="",
        tags=[],
        access="",
        existing_uid=existing_uid,
        observations="A brand-new observation to merge in.",
    )


# ---------------------------------------------------------------------------
# Config resolvers
# ---------------------------------------------------------------------------


class TestResolvePageSizeThresholdChars:
    def test_default_is_well_under_20000_and_above_p99(self) -> None:
        """The default must sit strictly between the corpus's p99 (8,468
        chars, per the issue's re-measurement) and the 20,000-char
        merge-input window -- otherwise it either misses genuine anomalies
        or catches ordinary pages."""
        assert 8_468 < DEFAULT_PAGE_SIZE_THRESHOLD_CHARS < 20_000

    def test_none_config_returns_default(self) -> None:
        assert resolve_page_size_threshold_chars(None) == DEFAULT_PAGE_SIZE_THRESHOLD_CHARS

    def test_yaml_override_wins(self) -> None:
        config = {"librarian": {"page_size_threshold_chars": 12_345}}
        assert resolve_page_size_threshold_chars(config) == 12_345

    def test_bool_rejected_as_int_subclass(self) -> None:
        """``page_size_threshold_chars: yes`` must not silently become 1."""
        config = {"librarian": {"page_size_threshold_chars": True}}
        assert resolve_page_size_threshold_chars(config) == DEFAULT_PAGE_SIZE_THRESHOLD_CHARS

    def test_non_positive_falls_back(self) -> None:
        config = {"librarian": {"page_size_threshold_chars": 0}}
        assert resolve_page_size_threshold_chars(config) == DEFAULT_PAGE_SIZE_THRESHOLD_CHARS

    def test_non_int_falls_back(self) -> None:
        config = {"librarian": {"page_size_threshold_chars": "big"}}
        assert resolve_page_size_threshold_chars(config) == DEFAULT_PAGE_SIZE_THRESHOLD_CHARS

    def test_missing_librarian_section_falls_back(self) -> None:
        assert resolve_page_size_threshold_chars({}) == DEFAULT_PAGE_SIZE_THRESHOLD_CHARS


class TestResolveOversizePageAction:
    def test_default_is_review(self) -> None:
        assert DEFAULT_OVERSIZE_PAGE_ACTION == "review"
        assert resolve_oversize_page_action(None) == "review"

    @pytest.mark.parametrize("action", VALID_OVERSIZE_PAGE_ACTIONS)
    def test_valid_values_round_trip(self, action: str) -> None:
        config = {"librarian": {"oversize_page_action": action}}
        assert resolve_oversize_page_action(config) == action

    def test_unknown_value_falls_back_to_review(self) -> None:
        config = {"librarian": {"oversize_page_action": "delete"}}
        assert resolve_oversize_page_action(config) == "review"

    def test_wrong_type_falls_back_to_review(self) -> None:
        config = {"librarian": {"oversize_page_action": 1}}
        assert resolve_oversize_page_action(config) == "review"


# ---------------------------------------------------------------------------
# check_page_size_gate
# ---------------------------------------------------------------------------


class TestCheckPageSizeGate:
    def test_under_threshold_returns_none(self) -> None:
        action = _update_action()
        assert check_page_size_gate(action, "short body", "sessions/x.md", None) is None

    def test_exactly_at_threshold_returns_none(self) -> None:
        """The threshold is inclusive on the "still mergeable" side -- a
        page exactly AT the limit is not yet an anomaly."""
        action = _update_action()
        body = "x" * DEFAULT_PAGE_SIZE_THRESHOLD_CHARS
        assert check_page_size_gate(action, body, "sessions/x.md", None) is None

    def test_over_threshold_default_review_returns_escalation(self) -> None:
        action = _update_action(name="Big Page")
        body = "x" * (DEFAULT_PAGE_SIZE_THRESHOLD_CHARS + 1)
        result = check_page_size_gate(action, body, "sessions/x.md", None)
        assert isinstance(result, EscalationItem)
        assert result.conflict_type == "oversize_page"
        assert result.entity_name == "Big Page"
        assert result.raw_ref == "sessions/x.md"
        assert "athenaeum#1182" in result.description
        assert action.observations in result.description

    def test_review_action_explicit_config_matches_default(self) -> None:
        action = _update_action()
        body = "x" * (DEFAULT_PAGE_SIZE_THRESHOLD_CHARS + 1)
        config = {"librarian": {"oversize_page_action": "review"}}
        result = check_page_size_gate(action, body, "sessions/x.md", config)
        assert isinstance(result, EscalationItem)
        assert result.conflict_type == "oversize_page"

    @pytest.mark.parametrize("reserved_action", ["split", "log_demote"])
    def test_reserved_actions_raise_not_implemented(self, reserved_action: str) -> None:
        """split/log-demotion are EXPRESSIBLE (a recognized config value)
        but not implemented -- a deliberate NotImplementedError, never a
        silent fallback to "review" and never a half-implemented
        restructuring."""
        action = _update_action(name="Big Page")
        body = "x" * (DEFAULT_PAGE_SIZE_THRESHOLD_CHARS + 1)
        config = {"librarian": {"oversize_page_action": reserved_action}}
        with pytest.raises(NotImplementedError, match=reserved_action):
            check_page_size_gate(action, body, "sessions/x.md", config)

    def test_custom_threshold_via_config(self) -> None:
        action = _update_action()
        config = {"librarian": {"page_size_threshold_chars": 20}}
        assert check_page_size_gate(action, "x" * 20, "ref", config) is None
        result = check_page_size_gate(action, "x" * 21, "ref", config)
        assert isinstance(result, EscalationItem)


# ---------------------------------------------------------------------------
# The real dispatch site: tier3_derive_actions's "update" branch
# ---------------------------------------------------------------------------


class TestTier3DeriveActionsPageSizeGate:
    def test_oversize_page_never_dispatches_a_merge_call(self, wiki_dir: Path) -> None:
        """The core proof: an over-threshold existing page gets NO LLM call
        at all -- not a patch-mode attempt, not a full-echo fallback. The
        mock client has no configured response/side_effect, so any call
        would raise immediately and fail this test."""
        oversized_body = "Fintech startup, Series B. " * 500  # well over 10,000 chars
        assert len(oversized_body) > DEFAULT_PAGE_SIZE_THRESHOLD_CHARS
        (wiki_dir / "a1b2c3d4-acme-corp.md").write_text(
            textwrap.dedent(f"""\
                ---
                uid: a1b2c3d4
                type: company
                name: Acme Corp
                ---

                {oversized_body}
            """)
        )
        from athenaeum.models import EntityIndex

        index = EntityIndex(wiki_dir)
        raw = _make_raw("New note about Acme Corp.")
        actions = [_update_action()]

        client = MagicMock()  # no return_value/side_effect configured

        new_entities, pending_updates, updated_uids, escalations = tier3_derive_actions(
            raw, actions, index, wiki_dir, client
        )

        client.messages.create.assert_not_called()
        assert new_entities == []
        assert pending_updates == []
        assert updated_uids == []
        assert len(escalations) == 1
        assert escalations[0].conflict_type == "oversize_page"
        assert escalations[0].entity_name == "Acme Corp"

        # The page itself is untouched (tier3_derive_actions never writes,
        # but pending_updates being empty above already proves nothing WILL
        # be written for it either).
        on_disk = (wiki_dir / "a1b2c3d4-acme-corp.md").read_text()
        assert oversized_body.strip() in on_disk

    def test_under_threshold_page_merges_normally(self, wiki_dir: Path) -> None:
        """Regression guard: the default fixture page (well under 10,000
        chars) must merge exactly as it did before athenaeum#1182."""
        import json

        from athenaeum.models import EntityIndex

        index = EntityIndex(wiki_dir)
        raw = _make_raw("New note about Acme Corp.")
        actions = [_update_action()]

        client = MagicMock()
        response = MagicMock()
        response.content = [
            MagicMock(
                text=json.dumps(
                    {
                        "ops": [
                            {"op": "append_section", "text": "New info landed."}
                        ]
                    }
                )
            )
        ]
        response.stop_reason = "end_turn"
        client.messages.create.return_value = response

        _new, pending_updates, updated_uids, escalations = tier3_derive_actions(
            raw, actions, index, wiki_dir, client
        )

        client.messages.create.assert_called_once()
        assert updated_uids == ["a1b2c3d4"]
        assert len(pending_updates) == 1
        assert not any(e.conflict_type == "oversize_page" for e in escalations)


# ---------------------------------------------------------------------------
# Run-summary counter: ProcessingResult.oversize_suppressed
# ---------------------------------------------------------------------------


class TestOversizeSuppressedCounter:
    def test_apply_tier3_results_counts_only_oversize_escalations(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.models import EntityIndex

        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        index = EntityIndex(wiki_root)
        raw = _make_raw("irrelevant")
        result = ProcessingResult(raw_file=raw)

        escalations = [
            EscalationItem(
                raw_ref="ref-1",
                entity_name="Big Page",
                conflict_type="oversize_page",
                description="over threshold",
            ),
            EscalationItem(
                raw_ref="ref-2",
                entity_name="Someone",
                conflict_type="ambiguous",
                description="unrelated escalation",
            ),
        ]

        _apply_tier3_results(
            result,
            new_entities=[],
            pending_updates=[],
            updated_uids=[],
            escalations=escalations,
            wiki_root=wiki_root,
            index=index,
            config=None,
        )

        assert result.oversize_suppressed == 1
        assert len(result.escalated) == 2

    def test_zero_when_no_oversize_escalations(self, tmp_path: Path) -> None:
        from athenaeum.models import EntityIndex

        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        index = EntityIndex(wiki_root)
        raw = _make_raw("irrelevant")
        result = ProcessingResult(raw_file=raw)

        _apply_tier3_results(
            result,
            new_entities=[],
            pending_updates=[],
            updated_uids=[],
            escalations=[],
            wiki_root=wiki_root,
            index=index,
            config=None,
        )

        assert result.oversize_suppressed == 0


# ---------------------------------------------------------------------------
# AC3 — read-only enumeration
# ---------------------------------------------------------------------------


class TestEnumerateOversizePages:
    def test_only_pages_over_threshold_are_returned(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "small.md").write_text(
            "---\nuid: aaaa1111\ntype: person\nname: Small\n---\n\nShort body.\n"
        )
        big_body = "x" * (DEFAULT_PAGE_SIZE_THRESHOLD_CHARS + 50)
        (wiki / "big.md").write_text(
            f"---\nuid: bbbb2222\ntype: project\nname: Big\n---\n\n{big_body}\n"
        )

        results = enumerate_oversize_pages(wiki)

        assert [p.path.name for p in results] == ["big.md"]
        assert results[0].chars > DEFAULT_PAGE_SIZE_THRESHOLD_CHARS
        assert results[0].entity_type == "project"

    def test_underscore_prefixed_files_are_skipped(self, tmp_path: Path) -> None:
        """Mirrors EntityIndex._load and models.py's _-prefix exclusion —
        explicitly kept unchanged by athenaeum#1182 (the genuinely huge
        _-prefixed files are already correctly excluded from the entity
        index and never merged into)."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        big_body = "x" * (DEFAULT_PAGE_SIZE_THRESHOLD_CHARS + 50)
        (wiki / "_pending_merges_archive.md").write_text(big_body)

        results = enumerate_oversize_pages(wiki)

        assert results == []

    def test_missing_type_reports_empty_string(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        big_body = "x" * (DEFAULT_PAGE_SIZE_THRESHOLD_CHARS + 50)
        (wiki / "no-type.md").write_text(
            f"---\nuid: cccc3333\nname: No Type\n---\n\n{big_body}\n"
        )

        results = enumerate_oversize_pages(wiki)

        assert len(results) == 1
        assert results[0].entity_type == ""

    def test_sorted_largest_first(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        small_big = "x" * (DEFAULT_PAGE_SIZE_THRESHOLD_CHARS + 50)
        large_big = "x" * (DEFAULT_PAGE_SIZE_THRESHOLD_CHARS + 5000)
        (wiki / "a-small-big.md").write_text(
            f"---\nuid: dddd4444\nname: A\n---\n\n{small_big}\n"
        )
        (wiki / "b-large-big.md").write_text(
            f"---\nuid: eeee5555\nname: B\n---\n\n{large_big}\n"
        )

        results = enumerate_oversize_pages(wiki)

        assert [p.path.name for p in results] == ["b-large-big.md", "a-small-big.md"]

    def test_custom_threshold_overrides_config(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "page.md").write_text(
            "---\nuid: ffff6666\nname: Page\n---\n\n" + ("x" * 100) + "\n"
        )

        assert enumerate_oversize_pages(wiki, threshold=50) != []
        assert enumerate_oversize_pages(wiki, threshold=1_000_000) == []

    def test_empty_wiki_returns_empty_list(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        assert enumerate_oversize_pages(wiki) == []
