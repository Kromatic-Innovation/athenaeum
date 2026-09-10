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
from athenaeum.answers import ingest_answers
from athenaeum.librarian import _apply_tier3_results, process_one
from athenaeum.models import (
    ClassifiedEntity,
    EntityAction,
    EntityIndex,
    EscalationItem,
    ProcessingResult,
    RawFile,
    TokenUsage,
    parse_frontmatter,
)
from athenaeum.tiers import (
    DEFAULT_OVERSIZE_PAGE_ACTION,
    DEFAULT_PAGE_SIZE_THRESHOLD_CHARS,
    OVERSIZE_ESCALATION_CONFLICT_TYPES,
    VALID_OVERSIZE_PAGE_ACTIONS,
    check_page_size_gate,
    collapse_oversize_escalation_duplicates,
    demote_oversize_pages,
    enumerate_oversize_pages,
    resolve_oversize_page_action,
    resolve_page_size_threshold_chars,
    tier3_derive_actions,
    tier4_escalate,
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


# ---------------------------------------------------------------------------
# Issue athenaeum#1430: oversize-page escalation dedup
# ---------------------------------------------------------------------------


class TestOversizeEscalationDedup:
    """The "review" disposition (issue athenaeum#1182) used to write a brand-new
    ``oversize_page`` block into ``_pending_questions.md`` on EVERY suppressed
    merge -- ~10 hub pages generated 8,502 duplicate blocks on the live
    corpus (issue athenaeum#1430). ``tier4_escalate`` now suppresses a new
    oversize-page-family escalation (:data:`OVERSIZE_ESCALATION_CONFLICT_TYPES`)
    for an entity that already has one UNANSWERED, keyed on "currently
    unanswered" so an answered/archived block lets a fresh one through.
    """

    def test_second_suppressed_merge_does_not_duplicate_the_block(
        self, wiki_dir: Path
    ) -> None:
        """AC2's counter-example, run for real through check_page_size_gate +
        tier4_escalate: two separate suppressed merges against the same
        over-threshold page leave exactly ONE unanswered oversize_page block,
        not two -- an explicit BEFORE/AFTER block-count assertion."""
        pending = wiki_dir / "_pending_questions.md"
        action = _update_action(name="Kromatic")
        body = "x" * (DEFAULT_PAGE_SIZE_THRESHOLD_CHARS + 1)

        # First suppressed merge -- creates the one durable block.
        first = check_page_size_gate(action, body, "sessions/one.md", None)
        assert isinstance(first, EscalationItem)
        tier4_escalate([first], pending)

        before_count = pending.read_text().count("**Conflict type**: oversize_page")
        assert before_count == 1

        # Second suppressed merge, same entity, same over-threshold page --
        # this is the defect: pre-fix, this appended a SECOND block.
        second = check_page_size_gate(action, body, "sessions/two.md", None)
        assert isinstance(second, EscalationItem)
        suppressed = tier4_escalate([second], pending)

        after_content = pending.read_text()
        after_count = after_content.count("**Conflict type**: oversize_page")
        assert before_count == 1
        assert after_count == 1  # NOT 2
        assert suppressed == 1
        # The suppressed observation's own source ref must not silently
        # replace the original -- the first block's ref is preserved.
        assert "sessions/one.md" in after_content
        assert "sessions/two.md" not in after_content

    def test_first_occurrence_unchanged(self, wiki_dir: Path) -> None:
        """AC3: an entity with no existing unanswered escalation still gets
        one created exactly as before -- no regression to the happy path."""
        pending = wiki_dir / "_pending_questions.md"
        action = _update_action(name="Fresh Entity")
        body = "x" * (DEFAULT_PAGE_SIZE_THRESHOLD_CHARS + 1)

        item = check_page_size_gate(action, body, "sessions/x.md", None)
        assert isinstance(item, EscalationItem)
        suppressed = tier4_escalate([item], pending)

        assert suppressed == 0
        content = pending.read_text()
        assert content.count("**Conflict type**: oversize_page") == 1
        assert 'Entity: "Fresh Entity"' in content

    def test_distinct_entities_each_get_their_own_block(self, wiki_dir: Path) -> None:
        """Two DIFFERENT oversized entities must not collapse into one --
        dedup is per-entity, not global."""
        pending = wiki_dir / "_pending_questions.md"
        body = "x" * (DEFAULT_PAGE_SIZE_THRESHOLD_CHARS + 1)
        item_a = check_page_size_gate(
            _update_action(name="Entity A"), body, "sessions/a.md", None
        )
        item_b = check_page_size_gate(
            _update_action(name="Entity B"), body, "sessions/b.md", None
        )
        assert isinstance(item_a, EscalationItem)
        assert isinstance(item_b, EscalationItem)
        tier4_escalate([item_a, item_b], pending)
        content = pending.read_text()
        assert content.count("**Conflict type**: oversize_page") == 2

    def test_answered_block_lets_a_fresh_escalation_through(
        self, wiki_dir: Path
    ) -> None:
        """AC4: dedup is keyed on CURRENTLY unanswered, not "ever seen". Once
        the existing escalation is answered/archived, the next suppressed
        merge against the same entity creates a fresh escalation again."""
        pending = wiki_dir / "_pending_questions.md"
        raw_root = wiki_dir.parent / "raw"
        action = _update_action(name="Kromatic")
        body = "x" * (DEFAULT_PAGE_SIZE_THRESHOLD_CHARS + 1)

        first = check_page_size_gate(action, body, "sessions/one.md", None)
        assert isinstance(first, EscalationItem)
        tier4_escalate([first], pending)
        assert pending.read_text().count("**Conflict type**: oversize_page") == 1

        # Answer + archive the block (same mechanism a human answer takes,
        # via athenaeum.answers.ingest_answers -- the "existing archive-on-
        # resolve path" AC5 also reuses).
        text = pending.read_text().replace("- [ ]", "- [x]", 1)
        text = text.replace("- [x]", "- [x]\n\nSplit the page.\n", 1)
        pending.write_text(text)
        ingested = ingest_answers(pending, raw_root)
        assert ingested == 1
        assert "**Conflict type**: oversize_page" not in pending.read_text()

        # Next suppressed merge against the SAME entity creates a FRESH block.
        second = check_page_size_gate(action, body, "sessions/two.md", None)
        assert isinstance(second, EscalationItem)
        suppressed = tier4_escalate([second], pending)
        assert suppressed == 0
        content = pending.read_text()
        assert content.count("**Conflict type**: oversize_page") == 1
        assert "sessions/two.md" in content

    def test_oversize_family_cross_type_dedup(self, wiki_dir: Path) -> None:
        """An unanswered ``oversize_page`` block for an entity also blocks a
        NEW ``oversize_split``/``oversize_log_demote`` escalation for that
        same entity (the "family" dedup the issue's Plan step 1 asks for),
        not just a second ``oversize_page`` -- avoids double review noise if
        an operator flips ``librarian.oversize_page_action`` mid-flight."""
        assert OVERSIZE_ESCALATION_CONFLICT_TYPES == {
            "oversize_page",
            "oversize_split",
            "oversize_log_demote",
        }
        pending = wiki_dir / "_pending_questions.md"
        first = check_page_size_gate(
            _update_action(name="Kromatic"),
            "x" * (DEFAULT_PAGE_SIZE_THRESHOLD_CHARS + 1),
            "sessions/one.md",
            None,
        )
        assert isinstance(first, EscalationItem)
        tier4_escalate([first], pending)

        split_item = EscalationItem(
            raw_ref="sessions/two.md",
            entity_name="Kromatic",
            conflict_type="oversize_split",
            description="Page 'Kromatic' was split into linked atomic pages.",
        )
        suppressed = tier4_escalate([split_item], pending)
        assert suppressed == 1
        content = pending.read_text()
        assert content.count("## [") == 1
        assert "oversize_split" not in content

    def test_two_new_suppressions_in_the_same_batch_collapse_too(
        self, wiki_dir: Path
    ) -> None:
        """No existing file yet -- two oversize items for the SAME entity in
        ONE ``tier4_escalate`` call must still collapse to one block
        (in-batch collapse, not just cross-call)."""
        pending = wiki_dir / "_pending_questions.md"
        body = "x" * (DEFAULT_PAGE_SIZE_THRESHOLD_CHARS + 1)
        item1 = check_page_size_gate(
            _update_action(name="Kromatic"), body, "sessions/one.md", None
        )
        item2 = check_page_size_gate(
            _update_action(name="Kromatic"), body, "sessions/two.md", None
        )
        assert isinstance(item1, EscalationItem)
        assert isinstance(item2, EscalationItem)
        suppressed = tier4_escalate([item1, item2], pending)
        assert suppressed == 1
        content = pending.read_text()
        assert content.count("**Conflict type**: oversize_page") == 1


# ---------------------------------------------------------------------------
# Issue athenaeum#1430 AC5: collapse_oversize_escalation_duplicates (migration)
# ---------------------------------------------------------------------------


def _oversize_block(entity: str, ref: str, *, date: str = "2026-09-01") -> str:
    return (
        f'## [{date}] Entity: "{entity}" (from {ref})\n'
        "- [ ] some question?\n\n"
        "**Conflict type**: oversize_page\n"
        f"**Description**: over threshold, ref={ref}\n"
    )


def _answered_block(entity: str, ref: str, *, date: str = "2026-09-01") -> str:
    return (
        f'## [{date}] Entity: "{entity}" (from {ref})\n'
        "- [x] some question?\n\nAlready answered.\n\n"
        "**Conflict type**: oversize_page\n"
        f"**Description**: over threshold, ref={ref}\n"
    )


def _write_pending(wiki: Path, blocks: list[str]) -> Path:
    pending = wiki / "_pending_questions.md"
    pending.write_text("# Pending Questions\n\n" + "\n\n---\n\n".join(blocks) + "\n")
    return pending


class TestCollapseOversizeEscalationDuplicates:
    def test_no_file_returns_zero(self, tmp_path: Path) -> None:
        assert collapse_oversize_escalation_duplicates(tmp_path / "missing.md") == 0

    def test_no_duplicates_is_a_noop(self, wiki_dir: Path) -> None:
        pending = _write_pending(
            wiki_dir,
            [_oversize_block("Alpha", "sessions/a.md"), _oversize_block("Beta", "sessions/b.md")],
        )
        before = pending.read_text()
        archived = collapse_oversize_escalation_duplicates(pending)
        assert archived == 0
        assert pending.read_text() == before
        assert not (wiki_dir / "_pending_questions_archive.md").exists()

    def test_collapses_duplicates_keeping_the_newest(self, wiki_dir: Path) -> None:
        pending = _write_pending(
            wiki_dir,
            [
                _oversize_block("Kromatic", "sessions/0.md", date="2026-08-01"),
                _oversize_block("Kromatic", "sessions/1.md", date="2026-08-15"),
                _oversize_block("Kromatic", "sessions/2.md", date="2026-09-01"),  # newest
            ],
        )
        before_count = pending.read_text().count("**Conflict type**: oversize_page")
        assert before_count == 3

        archived = collapse_oversize_escalation_duplicates(pending)

        after_text = pending.read_text()
        after_count = after_text.count("**Conflict type**: oversize_page")
        assert archived == 2
        assert after_count == 1
        assert "sessions/2.md" in after_text  # newest kept
        assert "sessions/0.md" not in after_text
        assert "sessions/1.md" not in after_text

        # Never deleted -- archived instead.
        archive_text = (wiki_dir / "_pending_questions_archive.md").read_text()
        assert "sessions/0.md" in archive_text
        assert "sessions/1.md" in archive_text
        assert archive_text.count("**Archived reason**") == 2
        assert "athenaeum#1430" in archive_text

    def test_is_idempotent(self, wiki_dir: Path) -> None:
        pending = _write_pending(
            wiki_dir,
            [
                _oversize_block("Kromatic", "sessions/0.md"),
                _oversize_block("Kromatic", "sessions/1.md"),
            ],
        )
        first = collapse_oversize_escalation_duplicates(pending)
        second = collapse_oversize_escalation_duplicates(pending)
        assert first == 1
        assert second == 0
        assert pending.read_text().count("**Conflict type**: oversize_page") == 1

    def test_distinct_entities_are_independent(self, wiki_dir: Path) -> None:
        pending = _write_pending(
            wiki_dir,
            [
                _oversize_block("Alpha", "sessions/a1.md"),
                _oversize_block("Alpha", "sessions/a2.md"),
                _oversize_block("Beta", "sessions/b1.md"),
            ],
        )
        archived = collapse_oversize_escalation_duplicates(pending)
        assert archived == 1
        content = pending.read_text()
        assert content.count('Entity: "Alpha"') == 1
        assert content.count('Entity: "Beta"') == 1

    def test_answered_blocks_are_never_touched(self, wiki_dir: Path) -> None:
        """Only UNANSWERED oversize blocks are collapsed -- an already
        answered ``[x]`` block (even a duplicate-looking one) is left
        exactly where it is; that's ``ingest_answers``'s job, not this
        migration's."""
        pending = _write_pending(
            wiki_dir,
            [
                _answered_block("Kromatic", "sessions/0.md"),
                _oversize_block("Kromatic", "sessions/1.md"),
                _oversize_block("Kromatic", "sessions/2.md"),
            ],
        )
        archived = collapse_oversize_escalation_duplicates(pending)
        # Only ONE unanswered duplicate collapsed; the answered block is not
        # part of the unanswered group at all.
        assert archived == 1
        content = pending.read_text()
        assert "sessions/0.md" in content  # answered block untouched, kept
        assert content.count("- [x]") == 1

    def test_non_oversize_blocks_are_untouched(self, wiki_dir: Path) -> None:
        principled_block = (
            '## [2026-09-01] Entity: "Gamma" (from sessions/g.md)\n'
            "- [ ] unrelated question?\n\n"
            "**Conflict type**: principled\n"
            "**Description**: not an oversize escalation at all\n"
        )
        pending = _write_pending(
            wiki_dir,
            [
                principled_block,
                _oversize_block("Kromatic", "sessions/0.md"),
                _oversize_block("Kromatic", "sessions/1.md"),
            ],
        )
        archived = collapse_oversize_escalation_duplicates(pending)
        assert archived == 1
        content = pending.read_text()
        assert 'Entity: "Gamma"' in content
        assert "principled" in content

    def test_malformed_block_is_preserved_verbatim_not_dropped(
        self, wiki_dir: Path
    ) -> None:
        """Safety property: this migration uses ``_split_blocks``/``_parse_block``
        directly (not ``parse_pending_questions``, which silently DROPS
        unparseable blocks from its returned list) specifically so a
        corrupt block already on disk is never lost by this rewrite.

        A block with no header at all is preamble/leader text that
        ``_split_blocks`` itself discards (matching ``ingest_answers``'s own
        behavior -- not something this migration changes). The interesting
        case is a block that STARTS with a valid header (so the splitter
        keeps it as its own block) but has neither a checkbox line nor a
        ``**Description**:`` line, so ``_parse_block`` genuinely cannot
        recover a question from it and returns ``None``."""
        malformed = (
            '## [2026-09-01] Entity: "Broken" (from sessions/broken.md)\n'
            "Stray text with no checkbox line and no Description field.\n"
        )
        pending = _write_pending(
            wiki_dir,
            [
                malformed.rstrip("\n"),
                _oversize_block("Kromatic", "sessions/0.md"),
                _oversize_block("Kromatic", "sessions/1.md"),
            ],
        )
        archived = collapse_oversize_escalation_duplicates(pending)
        assert archived == 1
        content = pending.read_text()
        assert "Broken" in content
        assert "Stray text with no checkbox line" in content

    def test_cross_conflict_type_family_collapses_together(self, wiki_dir: Path) -> None:
        """The "family" dedup: an unanswered oversize_page block and an
        unanswered oversize_split block for the SAME entity count as
        duplicates of each other, not two independent groups."""
        split_block = (
            '## [2026-09-01] Entity: "Kromatic" (from sessions/1.md)\n'
            "- [ ] split question?\n\n"
            "**Conflict type**: oversize_split\n"
            "**Description**: page was split\n"
        )
        pending = _write_pending(
            wiki_dir,
            [_oversize_block("Kromatic", "sessions/0.md"), split_block],
        )
        archived = collapse_oversize_escalation_duplicates(pending)
        assert archived == 1
        content = pending.read_text()
        assert content.count("## [") == 1

    def test_re_appending_to_an_existing_archive_prepends_newest_first(
        self, wiki_dir: Path
    ) -> None:
        archive_path = wiki_dir / "_pending_questions_archive.md"
        archive_path.write_text("# Answered Questions\n\nSOME OLDER ARCHIVED ENTRY\n")
        pending = _write_pending(
            wiki_dir,
            [
                _oversize_block("Kromatic", "sessions/0.md"),
                _oversize_block("Kromatic", "sessions/1.md"),
            ],
        )
        collapse_oversize_escalation_duplicates(pending)
        archive_text = archive_path.read_text()
        assert "sessions/0.md" in archive_text
        assert "SOME OLDER ARCHIVED ENTRY" in archive_text
        # newest-first: the new archive entry comes before the old one.
        assert archive_text.index("sessions/0.md") < archive_text.index(
            "SOME OLDER ARCHIVED ENTRY"
        )


# ---------------------------------------------------------------------------
# Issue athenaeum#1488 AC3/AC4 — full raw-file pipeline coverage for the
# retired-name guard athenaeum#1406 built (CreateNameDemotedError /
# _retired_names.yaml, see athenaeum.tiers.validate_create_name and
# gate_create_name_classifications). tests/test_create_name_gate_1173.py
# already proves the guard at the validate_create_name()/
# gate_create_name_classifications() unit level (TestValidateCreateNameRetiredGuard,
# TestGateCreateNameClassificationsDemoted). This class closes the same
# claim one layer up, through athenaeum.librarian.process_one end to end,
# exactly matching athenaeum#1488 AC3's literal wording: a demoted name
# followed by a fresh raw file mentioning it must not produce a new
# ``type: person`` entity page, and (AC4) an unrelated, never-demoted name
# must still mint normally in the SAME wiki (proving the guard does not
# over-suppress).
# ---------------------------------------------------------------------------


class TestDemotedNameFullPipelineReMint:
    def test_demoted_name_mention_does_not_mint_a_new_person_page(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        wiki.mkdir(parents=True)
        target = wiki / "aaaa1111-dijkstra.md"
        target.write_text(
            "---\nuid: aaaa1111\ntype: person\nname: dijkstra\n---\n\n"
            "Some persona/session-log content mistyped as person.\n"
        )
        config = {"librarian": {"preserved_log_dir": "logs"}}
        demoted = demote_oversize_pages([target], wiki, config)
        assert demoted[0].demoted is True
        # The demoted page is gone -- nothing left in the wiki under that name.
        assert list(wiki.glob("*.md")) == []

        raw_dir = knowledge / "raw" / "sessions"
        raw_dir.mkdir(parents=True)
        raw_path = raw_dir / "note.md"
        raw_path.write_text("dijkstra reviewed the PR today.\n", encoding="utf-8")
        raw = RawFile(path=raw_path, source="sessions", timestamp="", uuid8="")

        def _fake_tier2_classify(*_a: object, **_kw: object) -> list[ClassifiedEntity]:
            return [
                ClassifiedEntity(
                    name="dijkstra",
                    entity_type="person",
                    tags=[],
                    access="internal",
                    is_new=True,
                    existing_uid=None,
                    observations="dijkstra reviewed the PR today.",
                )
            ]

        monkeypatch.setattr("athenaeum.librarian.tier2_classify", _fake_tier2_classify)

        classify_client = MagicMock()
        classify_client.messages.create.side_effect = AssertionError(
            "tier2_classify is monkeypatched -- the real classify client must never be called"
        )
        write_client = MagicMock()
        write_client.messages.create.side_effect = AssertionError(
            "a demoted name must escalate before tier-3 write is ever called"
        )

        result = process_one(
            raw,
            EntityIndex(wiki),
            wiki,
            classify_client,
            valid_types=["person", "company", "concept"],
            valid_tags=[],
            valid_access=["open", "internal", "confidential", "personal"],
            usage=TokenUsage(),
            write_client=write_client,
            config=config,
        )

        assert result.created == []
        assert len(result.escalated) == 1
        assert result.escalated[0].entity_name == "dijkstra"
        # Still no page in the wiki -- the mention did not re-mint "dijkstra"
        # (the escalation writes only "_pending_questions.md", which the
        # leading-underscore filter here excludes, same convention as the
        # rest of this suite).
        assert [p for p in wiki.glob("*.md") if not p.name.startswith("_")] == []

    def test_never_demoted_name_mention_still_mints_normally(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC4 (negative case, and the over-suppression counter-example): a
        wiki carrying a retired-name record for a DIFFERENT identity
        (``cicero``) must still mint an unrelated, never-demoted name
        (``Widget Inc``) normally.

        Uses ``entity_type="company"`` rather than ``"person"`` for the
        surviving create: ``type: person`` pages are UNCONDITIONALLY refused
        a tier-3 LLM-authored create by ``PersonNeverLLMRewriteError``
        (issue athenaeum#1183 AC4, ``_refuse_person_rewrite`` in
        ``athenaeum.tiers``) — a restriction that applies to every person
        create regardless of this guard, demoted or not, so it is orthogonal
        to what this test proves. The retired-name guard's own over-
        suppression risk is about NAME identity, not entity TYPE, so a
        same-run, different-type create is the faithful negative case.
        """
        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        wiki.mkdir(parents=True)
        other_target = wiki / "bbbb2222-cicero.md"
        other_target.write_text(
            "---\nuid: bbbb2222\ntype: person\nname: cicero\n---\n\nPersona content.\n"
        )
        config = {"librarian": {"preserved_log_dir": "logs"}}
        demoted = demote_oversize_pages([other_target], wiki, config)
        assert demoted[0].demoted is True

        raw_dir = knowledge / "raw" / "sessions"
        raw_dir.mkdir(parents=True)
        raw_path = raw_dir / "note.md"
        raw_path.write_text("Widget Inc shipped a new release today.\n", encoding="utf-8")
        raw = RawFile(path=raw_path, source="sessions", timestamp="", uuid8="")

        def _fake_tier2_classify(*_a: object, **_kw: object) -> list[ClassifiedEntity]:
            return [
                ClassifiedEntity(
                    name="Widget Inc",
                    entity_type="company",
                    tags=[],
                    access="internal",
                    is_new=True,
                    existing_uid=None,
                    observations="Widget Inc shipped a new release today.",
                )
            ]

        monkeypatch.setattr("athenaeum.librarian.tier2_classify", _fake_tier2_classify)

        classify_client = MagicMock()
        classify_client.messages.create.side_effect = AssertionError(
            "tier2_classify is monkeypatched -- the real classify client must never be called"
        )
        write_response = MagicMock()
        write_response.content = [
            MagicMock(
                text="# Widget Inc\n\nShipped a new release today.[^1]\n\n[^1]: sessions/note.md"
            )
        ]
        write_client = MagicMock()
        write_client.messages.create.return_value = write_response

        result = process_one(
            raw,
            EntityIndex(wiki),
            wiki,
            classify_client,
            valid_types=["person", "company", "concept"],
            valid_tags=[],
            valid_access=["open", "internal", "confidential", "personal"],
            usage=TokenUsage(),
            write_client=write_client,
            config=config,
        )

        assert [e.name for e in result.created] == ["Widget Inc"]
        assert result.escalated == []
        page_names = sorted(p.stem for p in wiki.glob("*.md") if not p.name.startswith("_"))
        assert any("widget" in n.lower() for n in page_names)
