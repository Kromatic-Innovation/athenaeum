# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#1182 — the page-size invariant. Issue athenaeum#1248 extends this
suite for the ``split``/``log_demote`` dispositions.

Atomic pages must not be merged into indefinitely: a page whose existing
body crosses ``librarian.page_size_threshold_chars`` must be refused a
Tier-3 merge BEFORE the merge prompt is built or any model call is made,
and must route to one of THREE dispositions instead of accepting another
merge (issue athenaeum#1248): the shipped ``review`` default (escalate, leave
the page unmodified), ``split`` (decompose into a hub + linked atomic
pages), or ``log_demote`` (move the page into the preserved-log area via
the same mechanism the ``preserve`` shape-rule disposition already uses).

This suite covers:
  - the config resolvers (``librarian.page_size_threshold_chars`` /
    ``librarian.oversize_page_action``), mirroring the athenaeum#1168
    mention-density resolvers' validation contract exactly;
  - ``check_page_size_gate`` itself (under/at/over threshold; ``review``;
    ``split`` — a real multi-section fixture, the no-heading fallback, and
    an induced mid-write failure proving atomicity; ``log_demote`` — a real
    move, the unconfigured fallback, and an induced move failure proving
    atomicity);
  - the real dispatch site, ``tier3_derive_actions``'s "update" branch —
    proving the suppression is REAL: no LLM call is made, and the page's
    pending_updates/updated_uids stay empty, for all three dispositions;
  - the run-summary counters (``ProcessingResult.oversize_suppressed`` /
    ``oversize_split`` / ``oversize_log_demoted``, via
    ``athenaeum.librarian._apply_tier3_results``), proving the three
    dispositions are counted disjointly;
  - the read-only AC3 enumeration helper, ``enumerate_oversize_pages``.

No LLM, no network.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from athenaeum import rules as rules_module
from athenaeum import tiers as tiers_module
from athenaeum.librarian import _apply_tier3_results
from athenaeum.models import (
    EntityAction,
    EntityIndex,
    EscalationItem,
    ProcessingResult,
    RawFile,
    parse_frontmatter,
)
from athenaeum.tiers import (
    DEFAULT_OVERSIZE_PAGE_ACTION,
    DEFAULT_PAGE_SIZE_THRESHOLD_CHARS,
    VALID_OVERSIZE_PAGE_ACTIONS,
    check_page_size_gate,
    demote_oversize_pages,
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


def _make_split_fixture_page(wiki: Path, *, n_sections: int = 3) -> tuple[Path, dict, str]:
    """A real multi-entity-shaped oversized page (issue athenaeum#1248): several
    ``##`` sections, each carrying its own unique, greppable detail, summing
    well over :data:`DEFAULT_PAGE_SIZE_THRESHOLD_CHARS` -- the fixture the
    split tests below use to prove nothing is lost across the split."""
    sections = "\n\n".join(
        f"## Section {i}\n\nUnique detail for section {i}. " + ("Filler prose. " * 400)
        for i in range(n_sections)
    )
    body = "Intro paragraph about the page, before any heading.\n\n" + sections + "\n"
    frontmatter = (
        "---\n"
        "uid: aaaa1111\n"
        "type: project\n"
        "name: Big Project\n"
        "access: internal\n"
        "tags:\n"
        "  - active\n"
        "---\n\n"
    )
    path = wiki / "aaaa1111-big-project.md"
    path.write_text(frontmatter + body)
    meta, existing_body = parse_frontmatter(path.read_text())
    return path, meta, existing_body


def _make_flat_fixture_page(wiki: Path) -> tuple[Path, dict, str]:
    """A real oversized page with NO markdown heading at all (issue
    athenaeum#1248) -- the shape ``split`` explicitly refuses (leaving it to
    athenaeum#1282) and ``log_demote`` moves whole."""
    body = "Detailed log content, one long undifferentiated stream. " * 300
    frontmatter = "---\nuid: cccc3333\ntype: session\nname: Huge Log\n---\n\n"
    path = wiki / "cccc3333-huge-log.md"
    path.write_text(frontmatter + body)
    meta, existing_body = parse_frontmatter(path.read_text())
    return path, meta, existing_body


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
    def test_reserved_actions_without_path_degrade_to_review(
        self, reserved_action: str
    ) -> None:
        """split/log_demote are now IMPLEMENTED (issue athenaeum#1248), but both
        need existing_path/wiki_root to do anything -- a caller that omits
        them (like the bare 4-positional-arg calls throughout this class)
        gets exactly ``review``'s behaviour, unchanged from before athenaeum#1248:
        no raise, ever."""
        action = _update_action(name="Big Page")
        body = "x" * (DEFAULT_PAGE_SIZE_THRESHOLD_CHARS + 1)
        config = {"librarian": {"oversize_page_action": reserved_action}}
        result = check_page_size_gate(action, body, "sessions/x.md", config)
        assert isinstance(result, EscalationItem)
        assert result.conflict_type == "oversize_page"

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


# ---------------------------------------------------------------------------
# Issue athenaeum#1248: check_page_size_gate — the "split" disposition
# ---------------------------------------------------------------------------


class TestOversizePageSplit:
    def test_split_creates_hub_and_linked_child_pages(self, tmp_path: Path) -> None:
        """A real multi-section oversized fixture (issue athenaeum#1248): every
        section becomes its own atomic page, the original becomes a hub
        with the SAME uid/name (so existing index keys/references still
        resolve), and nothing from the original body is lost."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        path, meta, existing_body = _make_split_fixture_page(wiki, n_sections=3)
        assert len(existing_body) > DEFAULT_PAGE_SIZE_THRESHOLD_CHARS

        action = _update_action(name="Big Project", existing_uid="aaaa1111")
        config = {"librarian": {"oversize_page_action": "split"}}

        result = check_page_size_gate(
            action,
            existing_body,
            "sessions/x.md",
            config,
            existing_path=path,
            existing_meta=meta,
            wiki_root=wiki,
        )

        assert isinstance(result, EscalationItem)
        assert result.conflict_type == "oversize_split"
        assert "athenaeum#1248" in result.description

        # The hub keeps the ORIGINAL identity — existing index keys and
        # references (anything pointing at uid aaaa1111) still resolve.
        assert path.exists()
        hub_meta, hub_body = parse_frontmatter(path.read_text())
        assert hub_meta["uid"] == "aaaa1111"
        assert hub_meta["name"] == "Big Project"
        assert len(hub_body) < len(existing_body)
        assert "athenaeum#1248" in hub_body
        # The intro paragraph (content before the first heading) is kept on
        # the hub verbatim — nothing before the first heading is dropped.
        assert "Intro paragraph about the page" in hub_body

        children = [p for p in wiki.glob("*.md") if p != path]
        assert len(children) == 3

        # Round trip: every section's unique detail survives somewhere in
        # the split output, and each child links back to the hub.
        all_child_text = "\n".join(c.read_text() for c in children)
        for i in range(3):
            assert f"Unique detail for section {i}" in all_child_text

        child_metas = [parse_frontmatter(c.read_text())[0] for c in children]
        child_uids = {m["uid"] for m in child_metas}
        for cm in child_metas:
            assert any(
                r.get("uid") == "aaaa1111" and r.get("role") == "split-from"
                for r in cm.get("related", [])
            )
        hub_related_uids = {r["uid"] for r in hub_meta.get("related", [])}
        assert child_uids <= hub_related_uids

    def test_split_without_headings_falls_back_to_review_untouched(
        self, tmp_path: Path
    ) -> None:
        """No markdown heading to split on (issue athenaeum#1248's explicit
        call: require headings, leave the no-heading cohort to athenaeum#1282) —
        the page must be left COMPLETELY untouched, degrading to review."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        path, meta, existing_body = _make_flat_fixture_page(wiki)
        before = path.read_text()

        action = _update_action(name="Huge Log", existing_uid="cccc3333")
        config = {"librarian": {"oversize_page_action": "split"}}

        result = check_page_size_gate(
            action,
            existing_body,
            "sessions/x.md",
            config,
            existing_path=path,
            existing_meta=meta,
            wiki_root=wiki,
        )

        assert isinstance(result, EscalationItem)
        assert result.conflict_type == "oversize_page"
        assert path.read_text() == before
        assert list(wiki.glob("*.md")) == [path]

    def test_split_failure_partway_rolls_back_and_leaves_page_byte_identical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Induces a mid-split write failure (issue athenaeum#1248 AC: 'every
        write either completes or leaves the page byte-identical'). The
        SECOND ``atomic_write_text`` call (the second child page) raises —
        the first child, already written, must be rolled back (unlinked)
        and the original page must NEVER have been touched at all, since
        the hub is written last. The gate must degrade to review."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        path, meta, existing_body = _make_split_fixture_page(wiki, n_sections=3)
        before = path.read_text()

        real_atomic_write_text = tiers_module.atomic_write_text
        calls = {"n": 0}

        def _flaky_atomic_write_text(target: Path, text: str, **kwargs: object) -> None:
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("simulated disk failure mid-split")
            real_atomic_write_text(target, text, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(tiers_module, "atomic_write_text", _flaky_atomic_write_text)

        action = _update_action(name="Big Project", existing_uid="aaaa1111")
        config = {"librarian": {"oversize_page_action": "split"}}

        result = check_page_size_gate(
            action,
            existing_body,
            "sessions/x.md",
            config,
            existing_path=path,
            existing_meta=meta,
            wiki_root=wiki,
        )

        assert isinstance(result, EscalationItem)
        assert result.conflict_type == "oversize_page"  # degraded, did NOT split
        assert path.read_text() == before  # byte-identical — hub never touched
        # The rollback removed the one child that WAS written — no orphans.
        assert list(wiki.glob("*.md")) == [path]
        assert calls["n"] == 2  # confirms the induced failure actually fired


# ---------------------------------------------------------------------------
# Issue athenaeum#1248: check_page_size_gate — the "log_demote" disposition
# ---------------------------------------------------------------------------


class TestOversizePageLogDemote:
    def test_log_demote_moves_page_and_preserves_content_byte_identical(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        path, meta, existing_body = _make_flat_fixture_page(wiki)
        assert len(existing_body) > DEFAULT_PAGE_SIZE_THRESHOLD_CHARS
        original_full_text = path.read_text()

        action = _update_action(name="Huge Log", existing_uid="cccc3333")
        config = {
            "librarian": {
                "oversize_page_action": "log_demote",
                "preserved_log_dir": "logs",
            }
        }

        result = check_page_size_gate(
            action,
            existing_body,
            "sessions/x.md",
            config,
            existing_path=path,
            existing_meta=meta,
            wiki_root=wiki,
        )

        assert isinstance(result, EscalationItem)
        assert result.conflict_type == "oversize_log_demote"
        assert "athenaeum#1248" in result.description
        assert not path.exists()  # moved OUT of wiki/ — no longer discoverable

        dest_candidates = list((tmp_path / "logs").rglob("*.md"))
        assert len(dest_candidates) == 1
        assert dest_candidates[0].read_text() == original_full_text  # no content lost

    def test_log_demote_via_reactive_gate_also_writes_a_retired_record(
        self, tmp_path: Path
    ) -> None:
        """issue athenaeum#1406: the reactive ``check_page_size_gate`` call
        site shares :func:`_perform_oversize_log_demote` with the operator
        entrypoint (:func:`demote_oversize_pages`), so it gets the
        retired-name guard for free -- pinned here so the two call sites
        cannot silently drift."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        path, meta, existing_body = _make_flat_fixture_page(wiki)
        action = _update_action(name="Huge Log", existing_uid="cccc3333")
        config = {
            "librarian": {
                "oversize_page_action": "log_demote",
                "preserved_log_dir": "logs",
            }
        }

        check_page_size_gate(
            action,
            existing_body,
            "sessions/x.md",
            config,
            existing_path=path,
            existing_meta=meta,
            wiki_root=wiki,
        )

        payload = yaml.safe_load((wiki / "_retired_names.yaml").read_text())
        assert payload["retired"][0] == {
            "uid": "cccc3333",
            "name": "Huge Log",
            "aliases": [],
            "demoted_to": payload["retired"][0]["demoted_to"],
            "demoted_on": payload["retired"][0]["demoted_on"],
        }
        assert (tmp_path / "logs").exists()

    def test_log_demote_unconfigured_falls_back_to_review_untouched(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        path, meta, existing_body = _make_flat_fixture_page(wiki)
        before = path.read_text()

        action = _update_action(name="Huge Log", existing_uid="cccc3333")
        # No librarian.preserved_log_dir configured.
        config = {"librarian": {"oversize_page_action": "log_demote"}}

        result = check_page_size_gate(
            action,
            existing_body,
            "sessions/x.md",
            config,
            existing_path=path,
            existing_meta=meta,
            wiki_root=wiki,
        )

        assert isinstance(result, EscalationItem)
        assert result.conflict_type == "oversize_page"
        assert path.exists()
        assert path.read_text() == before

    def test_log_demote_move_failure_leaves_page_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Induces a failure INSIDE the reused ``preserve_raw_file`` move
        itself (issue athenaeum#1248 AC) — the page must be left completely
        untouched and the gate must degrade to review."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        path, meta, existing_body = _make_flat_fixture_page(wiki)
        before = path.read_text()

        def _flaky_move(*_a: object, **_kw: object) -> None:
            raise OSError("simulated move failure")

        monkeypatch.setattr(rules_module.shutil, "move", _flaky_move)

        action = _update_action(name="Huge Log", existing_uid="cccc3333")
        config = {
            "librarian": {
                "oversize_page_action": "log_demote",
                "preserved_log_dir": "logs",
            }
        }

        result = check_page_size_gate(
            action,
            existing_body,
            "sessions/x.md",
            config,
            existing_path=path,
            existing_meta=meta,
            wiki_root=wiki,
        )

        assert isinstance(result, EscalationItem)
        assert result.conflict_type == "oversize_page"
        assert path.exists()
        assert path.read_text() == before
        assert not (tmp_path / "logs").exists() or not list(
            (tmp_path / "logs").rglob("*.md")
        )


# ---------------------------------------------------------------------------
# Issue athenaeum#1248: split/log_demote through the REAL dispatch site
# ---------------------------------------------------------------------------


class TestTier3DeriveActionsSplitAndLogDemote:
    def test_split_via_real_dispatch_site_no_llm_call(self, wiki_dir: Path) -> None:
        oversized_sections = "\n\n".join(
            f"## Section {i}\n\n" + ("Detail prose. " * 400) for i in range(3)
        )
        (wiki_dir / "a1b2c3d4-acme-corp.md").write_text(
            "---\nuid: a1b2c3d4\ntype: company\nname: Acme Corp\n---\n\n"
            + oversized_sections
            + "\n"
        )
        index = EntityIndex(wiki_dir)
        raw = _make_raw("New note about Acme Corp.")
        actions = [_update_action()]
        client = MagicMock()
        config = {"librarian": {"oversize_page_action": "split"}}

        new_entities, pending_updates, updated_uids, escalations = tier3_derive_actions(
            raw, actions, index, wiki_dir, client, config=config
        )

        client.messages.create.assert_not_called()
        assert new_entities == []
        assert pending_updates == []
        assert updated_uids == []
        assert len(escalations) == 1
        assert escalations[0].conflict_type == "oversize_split"

        children = []
        for p in wiki_dir.glob("*.md"):
            m, _ = parse_frontmatter(p.read_text())
            related = m.get("related", [])
            if isinstance(related, list) and any(
                isinstance(r, dict) and r.get("uid") == "a1b2c3d4" and r.get("role") == "split-from"
                for r in related
            ):
                children.append(p)
        assert len(children) == 3

    def test_log_demote_via_real_dispatch_site_no_llm_call(self, wiki_dir: Path) -> None:
        oversized_body = "Fintech startup, Series B. " * 500
        (wiki_dir / "a1b2c3d4-acme-corp.md").write_text(
            "---\nuid: a1b2c3d4\ntype: company\nname: Acme Corp\n---\n\n" + oversized_body
        )
        index = EntityIndex(wiki_dir)
        raw = _make_raw("New note about Acme Corp.")
        actions = [_update_action()]
        client = MagicMock()
        knowledge_root = wiki_dir.parent
        config = {
            "librarian": {
                "oversize_page_action": "log_demote",
                "preserved_log_dir": "logs",
            }
        }

        new_entities, pending_updates, updated_uids, escalations = tier3_derive_actions(
            raw, actions, index, wiki_dir, client, config=config
        )

        client.messages.create.assert_not_called()
        assert pending_updates == []
        assert updated_uids == []
        assert len(escalations) == 1
        assert escalations[0].conflict_type == "oversize_log_demote"
        assert not (wiki_dir / "a1b2c3d4-acme-corp.md").exists()
        assert list((knowledge_root / "logs").rglob("*.md"))


# ---------------------------------------------------------------------------
# Issue athenaeum#1248: run-summary counters distinguish split/log_demote
# from plain oversize_suppressed
# ---------------------------------------------------------------------------


class TestOversizeDispositionCountersDistinguished:
    def test_apply_tier3_results_counts_disjointly(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        index = EntityIndex(wiki_root)
        raw = _make_raw("irrelevant")
        result = ProcessingResult(raw_file=raw)

        escalations = [
            EscalationItem(
                raw_ref="r1", entity_name="A", conflict_type="oversize_page", description="d"
            ),
            EscalationItem(
                raw_ref="r2", entity_name="B", conflict_type="oversize_split", description="d"
            ),
            EscalationItem(
                raw_ref="r3",
                entity_name="C",
                conflict_type="oversize_log_demote",
                description="d",
            ),
            EscalationItem(
                raw_ref="r4", entity_name="D", conflict_type="oversize_split", description="d"
            ),
            EscalationItem(
                raw_ref="r5", entity_name="E", conflict_type="ambiguous", description="d"
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
        assert result.oversize_split == 2
        assert result.oversize_log_demoted == 1
        assert len(result.escalated) == 5


# ---------------------------------------------------------------------------
# Issue athenaeum#1214: demote_oversize_pages — the operator entrypoint that
# log_demotes a NAMED set of pages directly, instead of waiting for a merge
# attempt to trip check_page_size_gate reactively.
# ---------------------------------------------------------------------------


class TestDemoteOversizePages:
    def test_demotes_named_page_and_leaves_others_untouched(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        target, _meta, _body = _make_flat_fixture_page(wiki)
        original_full_text = target.read_text()
        other = wiki / "deadbeef-other.md"
        other.write_text("---\nuid: deadbeef\ntype: person\nname: Other\n---\n\nshort\n")

        config = {"librarian": {"preserved_log_dir": "logs"}}

        results = demote_oversize_pages([target], wiki, config)

        assert len(results) == 1
        assert results[0].demoted is True
        assert results[0].reason == "demoted"
        assert not target.exists()
        assert other.exists()  # untouched — only the named page moved

        dest_candidates = list((tmp_path / "logs").rglob("*.md"))
        assert len(dest_candidates) == 1
        assert dest_candidates[0].read_text() == original_full_text

    def test_relative_path_resolves_against_wiki_root(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        target, _meta, _body = _make_flat_fixture_page(wiki)
        config = {"librarian": {"preserved_log_dir": "logs"}}

        results = demote_oversize_pages([target.name], wiki, config)

        assert results[0].demoted is True
        assert not target.exists()

    def test_missing_page_reported_not_raised(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        config = {"librarian": {"preserved_log_dir": "logs"}}

        results = demote_oversize_pages([wiki / "nope.md"], wiki, config)

        assert len(results) == 1
        assert results[0].demoted is False
        assert results[0].reason == "missing"

    def test_unconfigured_preserved_log_dir_leaves_page_untouched(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        target, _meta, _body = _make_flat_fixture_page(wiki)
        before = target.read_text()

        results = demote_oversize_pages([target], wiki, config=None)

        assert results[0].demoted is False
        assert results[0].reason == "not_configured_or_move_failed"
        assert target.exists()
        assert target.read_text() == before

    def test_dry_run_moves_nothing(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        target, _meta, _body = _make_flat_fixture_page(wiki)
        before = target.read_text()
        config = {"librarian": {"preserved_log_dir": "logs"}}

        results = demote_oversize_pages([target], wiki, config, dry_run=True)

        assert results[0].demoted is False
        assert results[0].reason == "dry_run"
        assert target.exists()
        assert target.read_text() == before
        assert not (tmp_path / "logs").exists()

    def test_multiple_pages_each_reported_independently(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        target_ok, _m, _b = _make_flat_fixture_page(wiki)
        missing = wiki / "absent.md"
        config = {"librarian": {"preserved_log_dir": "logs"}}

        results = demote_oversize_pages([target_ok, missing], wiki, config)

        by_path = {r.path: r for r in results}
        assert by_path[target_ok].demoted is True
        assert by_path[missing].demoted is False
        assert by_path[missing].reason == "missing"


# ---------------------------------------------------------------------------
# Issue athenaeum#1406: log_demote must leave a retired-name record a
# subsequent validate_create_name() call can see -- otherwise the name is
# silently re-mintable the next time it is mentioned.
# ---------------------------------------------------------------------------


def _make_person_fixture_page(
    wiki: Path,
    *,
    uid: str = "aaaa1111",
    name: str = "dijkstra",
    aliases: list[str] | None = None,
) -> Path:
    """A small ``type: person`` page -- ``demote_oversize_pages`` (the
    operator entrypoint under test here) demotes exactly the paths it is
    given, with no size check of its own, so this fixture does not need to
    be oversize like ``_make_flat_fixture_page``."""
    wiki.mkdir(parents=True, exist_ok=True)
    aliases_yaml = ""
    if aliases:
        aliases_yaml = "aliases:\n" + "".join(f"  - {a}\n" for a in aliases)
    path = wiki / f"{uid}-{name.lower()}.md"
    path.write_text(
        f"---\nuid: {uid}\ntype: person\nname: {name}\n{aliases_yaml}---\n\n"
        "Some persona/session-log content mistyped as person.\n"
    )
    return path


class TestLogDemoteRetiredNameGuard:
    def test_demote_writes_a_retired_name_record(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        target = _make_person_fixture_page(
            wiki, uid="aaaa1111", name="dijkstra", aliases=["Dijkstra the Developer"]
        )
        config = {"librarian": {"preserved_log_dir": "logs"}}

        results = demote_oversize_pages([target], wiki, config)

        assert results[0].demoted is True
        sidecar = wiki / "_retired_names.yaml"
        assert sidecar.exists()
        payload = yaml.safe_load(sidecar.read_text())
        [record] = payload["retired"]
        assert record["uid"] == "aaaa1111"
        assert record["name"] == "dijkstra"
        assert record["aliases"] == ["Dijkstra the Developer"]
        assert record["demoted_to"] == str(results[0].dest)

    def test_retired_sidecar_is_excluded_from_entity_index(self, tmp_path: Path) -> None:
        """The sidecar's leading underscore keeps :meth:`EntityIndex._load`
        from ever reading it (that method skips any ``wiki_root.glob("*.md")``
        match starting with ``_``) -- so a retired record can never re-enter
        :meth:`EntityIndex.items`'s raw-text MENTION-matching fan-out merely
        by having been written (issue athenaeum#1406 AC3)."""
        wiki = tmp_path / "wiki"
        target = _make_person_fixture_page(wiki)
        config = {"librarian": {"preserved_log_dir": "logs"}}
        demote_oversize_pages([target], wiki, config)

        index = EntityIndex(wiki)

        assert index.lookup("dijkstra") is None
        assert list(index.items()) == []
        assert len(index) == 0

    def test_demote_move_failure_leaves_no_retired_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Atomicity (issue athenaeum#1406 AC4 / Plan step 3): a fixture in
        which the move fails leaves neither a retired-name record nor a
        half-moved page. ``preserve_raw_file`` fails closed internally (an
        ``OSError`` from ``shutil.move`` is caught there and turned into a
        ``None`` return, never raised) -- this pins that "failed move" and
        "no record written" are the exact same branch."""
        wiki = tmp_path / "wiki"
        target = _make_person_fixture_page(wiki)
        before = target.read_text()

        def _flaky_move(*_a: object, **_kw: object) -> None:
            raise OSError("simulated move failure")

        monkeypatch.setattr(rules_module.shutil, "move", _flaky_move)
        config = {"librarian": {"preserved_log_dir": "logs"}}

        results = demote_oversize_pages([target], wiki, config)

        assert results[0].demoted is False
        assert target.exists()
        assert target.read_text() == before
        assert not (wiki / "_retired_names.yaml").exists()

    def test_demote_with_no_uid_or_name_frontmatter_still_moves_unguarded(
        self, tmp_path: Path
    ) -> None:
        """A page with no wiki-entity frontmatter at all has no name to
        guard -- it still demotes (unchanged pre-athenaeum#1406 behaviour),
        just without a retired-name record."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        path = wiki / "no-frontmatter.md"
        path.write_text("Just prose, no frontmatter at all.\n")
        config = {"librarian": {"preserved_log_dir": "logs"}}

        results = demote_oversize_pages([path], wiki, config)

        assert results[0].demoted is True
        assert not (wiki / "_retired_names.yaml").exists()

    @pytest.mark.parametrize(
        "uid,name",
        [
            ("d1", "dijkstra"),
            ("d2", "cicero"),
            ("d3", "lane"),
            ("d4", "unknown"),
            ("d5", "owner"),
        ],
    )
    def test_each_pr_1395_demoted_name_gets_a_record(
        self, tmp_path: Path, uid: str, name: str
    ) -> None:
        """issue athenaeum#1406 AC: the five names PR athenaeum#1395 demoted
        (dijkstra/cicero/lane/unknown/owner) are each covered."""
        wiki = tmp_path / "wiki"
        target = _make_person_fixture_page(wiki, uid=uid, name=name)
        config = {"librarian": {"preserved_log_dir": "logs"}}

        results = demote_oversize_pages([target], wiki, config)

        assert results[0].demoted is True
        payload = yaml.safe_load((wiki / "_retired_names.yaml").read_text())
        assert payload["retired"][0]["name"] == name
