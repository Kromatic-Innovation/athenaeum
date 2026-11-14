# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#1182 — the page-size invariant.

Atomic pages must not be merged into indefinitely: a page whose existing
body crosses ``librarian.page_size_threshold_chars`` must be refused a
Tier-3 merge BEFORE the merge prompt is built or any model call is made.
Since issue athenaeum#2078's operator ruling, the shipped ``review``
default no longer refuses the merge at all: it flags the page
``needs_split: true`` in place and lets the merge proceed, leaving the
actual restructuring to a later pass. ``log_demote`` (move the page into
the preserved-log area via the same mechanism the ``preserve`` shape-rule
disposition already uses, issue athenaeum#1248) is the one disposition
that still refuses the merge, by moving the page out of the wiki
entirely. The THIRD disposition issue athenaeum#1248 introduced,
``split`` (decompose into a hub + linked atomic pages), was retired by
issue athenaeum#2550 in favour of the ``librarian.page_recompile``
pipeline; ``librarian.oversize_page_action: split`` now reads as
``review`` (one deprecation warning per run, covered in
``tests/test_config.py`` against ``load_config``) -- see
``tests/test_page_recompile.py`` for the relocated
``_split_into_atomic_sections`` coverage and
``tests/test_cli.py``/``tests/test_oversize_merge_2078.py`` for the
removed ``athenaeum split-oversize`` command and its ``oversize_split``
module.

This suite covers:
  - the config resolvers (``librarian.page_size_threshold_chars`` /
    ``librarian.oversize_page_action``), mirroring the athenaeum#1168
    mention-density resolvers' validation contract exactly;
  - ``check_page_size_gate`` itself (under/at/over threshold; ``review``'s
    flag-and-allow-merge path; ``log_demote`` — a real move, the
    unconfigured fallback, and an induced move failure proving
    atomicity; the retired ``split`` value degrading to the same
    flag-and-allow-merge path as ``review``, even when the real-split
    call shape -- ``existing_path``/``wiki_root`` supplied -- is used);
  - the real dispatch site, ``tier3_derive_actions``'s "update" branch —
    proving the merge suppression is REAL for ``log_demote`` (no LLM
    call, pending_updates/updated_uids stay empty) and that ``review``/
    retired-``split`` instead let the merge (and its LLM call) proceed,
    flagged;
  - the run-summary counter (``ProcessingResult.oversize_log_demoted``,
    via ``athenaeum.librarian._apply_tier3_results``) -- the sibling
    ``oversize_suppressed``/``oversize_split`` counters were removed by
    issue athenaeum#2550 along with the escalation types they counted.

The former read-only AC3 enumeration helper, ``enumerate_oversize_pages``,
was removed (issue athenaeum#2436 -- 0 src callers; it duplicated
``oversize_split.find_split_candidates``'s walk, itself since retired).

No LLM, no network.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from athenaeum import pii
from athenaeum import rules as rules_module
from athenaeum.answers import ingest_answers
from athenaeum.config import DEFAULT_PAGE_SIZE_THRESHOLD_CHARS
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
    OVERSIZE_ESCALATION_CONFLICT_TYPES,
    VALID_OVERSIZE_PAGE_ACTIONS,
    _perform_oversize_log_demote,
    check_page_size_gate,
    collapse_oversize_escalation_duplicates,
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
# Issue athenaeum#2550 AC3: the retired reactive split's two
# fully-removable helpers have no remaining caller anywhere in src/ --
# proven by scanning the real source text, not a hand-maintained list of
# known call sites (which would only prove nobody ADDED a new call site
# since the list was last updated, not that the functions are actually
# gone).
# ---------------------------------------------------------------------------


class TestRetiredSplitHelpersHaveNoCaller:
    """``_perform_oversize_page_split`` and ``_build_split_child_pages``
    (issue athenaeum#1248) were deleted outright, not merely disconnected,
    because nothing outside the reactive split path itself ever called
    them (grep evidence in the issue's own implementation). Contrast
    ``_split_into_atomic_sections``, their shared section-grouping helper,
    which survives -- :func:`athenaeum.page_recompile.segment_page` is a
    real, live caller -- so it is asserted present, not absent, and
    relocated to :mod:`athenaeum.page_recompile` rather than deleted."""

    _SRC_DIR = Path(__file__).resolve().parents[1] / "src" / "athenaeum"

    def _call_sites(self, name: str) -> dict[str, int]:
        """Lines that actually INVOKE/CONSTRUCT *name* (``name(`` as a whole
        word, immediately followed by an opening paren) -- not a comment or
        docstring mentioning the retired name for historical context.
        Mirrors ``tests/test_exit_codes_doc_parity.py``'s
        ``_modules_calling_acquire_or_exit`` convention exactly."""
        needle = f"{name}("
        hits: dict[str, int] = {}
        for path in sorted(self._SRC_DIR.glob("*.py")):
            count = 0
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                count += stripped.count(needle)
            if count:
                hits[path.name] = count
        return hits

    def test_perform_oversize_page_split_has_no_caller(self) -> None:
        assert self._call_sites("_perform_oversize_page_split") == {}

    def test_build_split_child_pages_has_no_caller(self) -> None:
        assert self._call_sites("_build_split_child_pages") == {}

    def test_split_child_page_dataclass_has_no_construction_site(self) -> None:
        assert self._call_sites("_SplitChildPage") == {}

    def test_split_into_atomic_sections_moved_not_deleted(self) -> None:
        """Positive control: the shared helper these two called is a real
        symbol with a real caller elsewhere, so the three asserts above
        cannot pass merely because this test's own grep is broken."""
        hits = self._call_sites("_split_into_atomic_sections")
        assert hits.get("page_recompile.py", 0) >= 1
        assert "tiers.py" not in hits  # relocated, not merely re-exported


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

    def test_retired_split_value_resolves_to_review(self) -> None:
        """Issue athenaeum#2550: the reactive ``split`` disposition is
        retired -- a stale ``librarian.oversize_page_action: split`` now
        resolves to ``"review"``, the same value an unrecognized action
        falls back to, but via the dedicated, by-name mapping (see
        ``_RETIRED_OVERSIZE_PAGE_ACTION_SPLIT``) rather than the generic
        unknown-value path. The one-per-run deprecation warning this value
        also triggers is covered in ``tests/test_config.py`` against
        ``load_config``, not here -- this function is called once per
        oversized page, not once per run."""
        config = {"librarian": {"oversize_page_action": "split"}}
        assert resolve_oversize_page_action(config) == "review"


# ---------------------------------------------------------------------------
# check_page_size_gate
# ---------------------------------------------------------------------------


class TestCheckPageSizeGate:
    def test_under_threshold_returns_none(self) -> None:
        action = _update_action()
        meta: dict = {}
        assert (
            check_page_size_gate(action, "short body", "sessions/x.md", None, existing_meta=meta)
            is None
        )
        assert "needs_split" not in meta

    def test_exactly_at_threshold_returns_none(self) -> None:
        """The threshold is inclusive on the "still mergeable" side -- a
        page exactly AT the limit is not yet an anomaly."""
        action = _update_action()
        body = "x" * DEFAULT_PAGE_SIZE_THRESHOLD_CHARS
        meta: dict = {}
        assert (
            check_page_size_gate(action, body, "sessions/x.md", None, existing_meta=meta) is None
        )
        assert "needs_split" not in meta

    def test_over_threshold_default_review_flags_needs_split_and_allows_merge(self) -> None:
        """Issue athenaeum#2078 AC1 (operator ruling): an over-threshold page
        under the default ``review`` action is no longer suppressed -- the
        merge is allowed to proceed and the page is flagged ``needs_split``
        in place on the caller's own ``existing_meta`` dict."""
        action = _update_action(name="Big Page")
        body = "x" * (DEFAULT_PAGE_SIZE_THRESHOLD_CHARS + 1)
        meta: dict = {}
        result = check_page_size_gate(action, body, "sessions/x.md", None, existing_meta=meta)
        assert result is None
        assert meta["needs_split"] is True

    def test_review_action_explicit_config_matches_default(self) -> None:
        action = _update_action()
        body = "x" * (DEFAULT_PAGE_SIZE_THRESHOLD_CHARS + 1)
        config = {"librarian": {"oversize_page_action": "review"}}
        meta: dict = {}
        result = check_page_size_gate(action, body, "sessions/x.md", config, existing_meta=meta)
        assert result is None
        assert meta["needs_split"] is True

    def test_log_demote_without_path_degrades_to_flag_and_merge(self) -> None:
        """log_demote (issue athenaeum#1248) needs existing_path/wiki_root
        to do anything -- a caller that omits them (like the bare calls
        throughout this class) gets exactly the same athenaeum#2078
        flag-and-allow-merge degraded behaviour as ``review``, never a
        raise."""
        action = _update_action(name="Big Page")
        body = "x" * (DEFAULT_PAGE_SIZE_THRESHOLD_CHARS + 1)
        config = {"librarian": {"oversize_page_action": "log_demote"}}
        meta: dict = {}
        result = check_page_size_gate(action, body, "sessions/x.md", config, existing_meta=meta)
        assert result is None
        assert meta["needs_split"] is True

    def test_split_degrades_to_flag_and_merge_even_with_path_supplied(
        self, tmp_path: Path
    ) -> None:
        """Issue athenaeum#2550: unlike ``log_demote``, ``split`` degrades
        to the flag-and-allow-merge path EVEN WHEN *existing_path* and
        *wiki_root* are supplied -- the retired reactive split never runs
        again, regardless of call-site shape. See
        ``tests/test_page_size_gate.py::TestOversizePageSplit`` (removed by
        this same issue) for the real-split fixtures this disposition used
        to drive."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        path, meta, existing_body = _make_split_fixture_page(wiki, n_sections=3)
        before = path.read_text()

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

        assert result is None
        assert meta["needs_split"] is True
        assert path.read_text() == before  # byte-identical -- no split ran
        assert list(wiki.glob("*.md")) == [path]  # no child pages written

    def test_custom_threshold_via_config(self) -> None:
        action = _update_action()
        config = {"librarian": {"page_size_threshold_chars": 20}}
        assert (
            check_page_size_gate(action, "x" * 20, "ref", config, existing_meta={}) is None
        )
        meta: dict = {}
        result = check_page_size_gate(action, "x" * 21, "ref", config, existing_meta=meta)
        assert result is None
        assert meta["needs_split"] is True


# ---------------------------------------------------------------------------
# The real dispatch site: tier3_derive_actions's "update" branch
# ---------------------------------------------------------------------------


class TestTier3DeriveActionsPageSizeGate:
    def test_oversize_page_is_merged_and_flagged_needs_split(self, wiki_dir: Path) -> None:
        """Issue athenaeum#2078 AC1 (operator ruling 2026-10-15): an
        over-threshold existing page is no longer refused. The merge runs
        and the page is flagged ``needs_split: true`` for the split pass."""
        import json

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

        client = MagicMock()
        response = MagicMock()
        response.content = [
            MagicMock(
                text=json.dumps(
                    {"ops": [{"op": "append_section", "text": "New info landed."}]}
                )
            )
        ]
        response.stop_reason = "end_turn"
        client.messages.create.return_value = response

        _new, pending_updates, updated_uids, escalations = tier3_derive_actions(
            raw, actions, index, wiki_dir, client
        )

        client.messages.create.assert_called_once()
        assert escalations == []
        assert updated_uids == ["a1b2c3d4"]
        meta, body = parse_frontmatter(pending_updates[0][1])
        assert meta["needs_split"] is True
        assert "New info landed." in body
        assert oversized_body.strip() in body  # nothing dropped

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
        site drives :func:`_perform_oversize_log_demote` (the shared move
        helper), so it gets the retired-name guard for free -- pinned here
        directly against that helper's own contract."""
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

        # athenaeum#2078: the degraded path flags the page and lets the merge run.
        assert result is None
        assert meta["needs_split"] is True
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

        # athenaeum#2078: the degraded path flags the page and lets the merge run.
        assert result is None
        assert meta["needs_split"] is True
        assert path.exists()
        assert path.read_text() == before
        assert not (tmp_path / "logs").exists() or not list(
            (tmp_path / "logs").rglob("*.md")
        )


# ---------------------------------------------------------------------------
# Issue athenaeum#1248: split/log_demote through the REAL dispatch site
# ---------------------------------------------------------------------------


class TestTier3DeriveActionsSplitAndLogDemote:
    def test_retired_split_via_real_dispatch_site_merges_and_flags_instead(
        self, wiki_dir: Path
    ) -> None:
        """Issue athenaeum#2550: ``librarian.oversize_page_action: split``
        through the REAL dispatch site now behaves exactly like ``review``
        -- the merge (and its LLM call) proceeds, the page is flagged
        ``needs_split``, and no escalation is raised. Contrast with
        ``test_log_demote_via_real_dispatch_site_no_llm_call`` below, the
        one disposition that still suppresses the merge."""
        import json

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
        response = MagicMock()
        response.content = [
            MagicMock(
                text=json.dumps(
                    {"ops": [{"op": "append_section", "text": "New info landed."}]}
                )
            )
        ]
        response.stop_reason = "end_turn"
        client.messages.create.return_value = response
        config = {"librarian": {"oversize_page_action": "split"}}
        before_files = set(wiki_dir.glob("*.md"))

        _new, pending_updates, updated_uids, escalations = tier3_derive_actions(
            raw, actions, index, wiki_dir, client, config=config
        )

        client.messages.create.assert_called_once()
        assert escalations == []
        assert updated_uids == ["a1b2c3d4"]
        meta, body = parse_frontmatter(pending_updates[0][1])
        assert meta["needs_split"] is True
        assert "New info landed." in body
        # No split children were written -- the retired disposition never ran.
        assert set(wiki_dir.glob("*.md")) == before_files

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
# Issue athenaeum#1182/#1248: the run-summary counter for log_demote.
#
# Issue athenaeum#2550 removed the sibling ``oversize_suppressed`` (the
# retired ``review``-escalates-and-counts shape, dead since athenaeum#2078)
# and ``oversize_split`` (the retired reactive-split shape) counters and
# fields from ``models.py``/``batch.py``/``librarian.py`` and the run
# summary. ``oversize_page``/``oversize_split`` remain valid
# ``EscalationItem.conflict_type`` VALUES -- the archived-block parser
# stays tolerant of them (see ``tests/test_oversize_merge_2078.py`` and
# ``OVERSIZE_ESCALATION_CONFLICT_TYPES``) -- they are simply no longer
# counted, and ``check_page_size_gate`` no longer produces them.
# ---------------------------------------------------------------------------


class TestOversizeLogDemotedCounter:
    def test_apply_tier3_results_counts_only_log_demote(self, tmp_path: Path) -> None:
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

        # Every escalation is still collected regardless of conflict_type --
        # only the counting is gone for the two retired types.
        assert not hasattr(result, "oversize_suppressed")
        assert not hasattr(result, "oversize_split")
        assert result.oversize_log_demoted == 1
        assert len(result.escalated) == 5


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
    """A small ``type: person`` page -- ``_perform_oversize_log_demote`` (the
    shared move helper under test here, called directly below) demotes
    exactly the path it is given, with no size check of its own, so this
    fixture does not need to be oversize like ``_make_flat_fixture_page``."""
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

        dest = _perform_oversize_log_demote(target, wiki, config)

        assert dest is not None
        sidecar = wiki / "_retired_names.yaml"
        assert sidecar.exists()
        payload = yaml.safe_load(sidecar.read_text())
        [record] = payload["retired"]
        assert record["uid"] == "aaaa1111"
        assert record["name"] == "dijkstra"
        assert record["aliases"] == ["Dijkstra the Developer"]
        assert record["demoted_to"] == str(dest)

    def test_retired_sidecar_is_excluded_from_entity_index(self, tmp_path: Path) -> None:
        """The sidecar's leading underscore keeps :meth:`EntityIndex._load`
        from ever reading it (that method skips any ``wiki_root.glob("*.md")``
        match starting with ``_``) -- so a retired record can never re-enter
        :meth:`EntityIndex.items`'s raw-text MENTION-matching fan-out merely
        by having been written (issue athenaeum#1406 AC3)."""
        wiki = tmp_path / "wiki"
        target = _make_person_fixture_page(wiki)
        config = {"librarian": {"preserved_log_dir": "logs"}}
        _perform_oversize_log_demote(target, wiki, config)

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

        dest = _perform_oversize_log_demote(target, wiki, config)

        assert dest is None
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

        dest = _perform_oversize_log_demote(path, wiki, config)

        assert dest is not None
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

        dest = _perform_oversize_log_demote(target, wiki, config)

        assert dest is not None
        payload = yaml.safe_load((wiki / "_retired_names.yaml").read_text())
        assert payload["retired"][0]["name"] == name


# ---------------------------------------------------------------------------
# Issue athenaeum#2217: log_demote settles the moved page's contact record,
# only after the move itself succeeded.
# ---------------------------------------------------------------------------


class TestOversizeLogDemoteContactRecordSettle:
    """``_perform_oversize_log_demote`` calls the page-removal settle helper
    with no successor, strictly in the post-success branch -- the exact
    branch :class:`TestLogDemoteRetiredNameGuard` above already pins for the
    retired-name record, reused here (issue athenaeum#2217 AC4: a case for
    the move succeeding, a case for it failing)."""

    def _config(self) -> dict:
        return {
            "librarian": {"preserved_log_dir": "logs"},
            "storage": {"mapping": {"pii": "excluded"}},
        }

    def _contact_record(self, knowledge_root: Path, uid: str) -> Path:
        root = pii.contacts_surface_root(knowledge_root, self._config())
        root.mkdir(parents=True, exist_ok=True)
        record = root / f"{uid}.md"
        record.write_text(
            f"---\nuid: {uid}\npii: true\nemails:\n  - 'a@example.org'\n---\nSynthetic.\n",
            encoding="utf-8",
        )
        return record

    def test_move_success_settles_the_contact_record(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        target, _meta, _body = _make_flat_fixture_page(wiki)
        record = self._contact_record(tmp_path, "cccc3333")

        dest = _perform_oversize_log_demote(target, wiki, self._config())

        assert dest is not None
        assert not record.exists()
        assert pii.resolve_contact_record_for_uid(record.parent, "cccc3333") is None

    def test_uid_without_a_name_still_settles_the_contact_record(
        self, tmp_path: Path
    ) -> None:
        """A page carrying a uid but NO ``name`` still MOVED, so its contact
        record is just as orphaned — even though the retired-name record is
        correctly skipped for it (there is no name to guard against re-mint).
        The settle must not ride on that early return."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        body = "Detailed log content, one long undifferentiated stream. " * 300
        target = wiki / "cccc3333-huge-log.md"
        target.write_text("---\nuid: cccc3333\ntype: session\n---\n\n" + body)
        record = self._contact_record(tmp_path, "cccc3333")

        dest = _perform_oversize_log_demote(target, wiki, self._config())

        assert dest is not None
        assert not record.exists()
        assert pii.resolve_contact_record_for_uid(record.parent, "cccc3333") is None

    def test_move_failure_leaves_the_contact_record_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        target, _meta, _body = _make_flat_fixture_page(wiki)
        record = self._contact_record(tmp_path, "cccc3333")

        def _flaky_move(*_a: object, **_kw: object) -> None:
            raise OSError("simulated move failure")

        monkeypatch.setattr(rules_module.shutil, "move", _flaky_move)

        dest = _perform_oversize_log_demote(target, wiki, self._config())

        assert dest is None
        assert record.exists()


# ---------------------------------------------------------------------------
# Issue athenaeum#1430: oversize-page escalation dedup
# ---------------------------------------------------------------------------


def _oversize_page_item(entity_name: str, ref: str) -> EscalationItem:
    """Build an ``oversize_page``-conflict :class:`EscalationItem` directly.

    Since issue athenaeum#2078, ``check_page_size_gate`` itself never returns
    this conflict type any more (every real caller passes ``existing_meta``,
    so the gate always flags ``needs_split`` and lets the merge proceed
    instead of escalating) -- this mirrors the construction
    :class:`TestOversizeSuppressedCounter` already uses to exercise the
    ``oversize_page`` FAMILY value (:data:`OVERSIZE_ESCALATION_CONFLICT_TYPES`)
    still carried by :func:`tier4_escalate`'s dedup and by
    :func:`collapse_oversize_escalation_duplicates`'s migration path, without
    pretending the now-dead escalation path still fires.
    """
    return EscalationItem(
        raw_ref=ref,
        entity_name=entity_name,
        conflict_type="oversize_page",
        description=f"Page {entity_name!r} is over the page-size threshold (ref={ref}).",
    )


class TestOversizeEscalationDedup:
    """The "review" disposition (issue athenaeum#1182) used to write a brand-new
    ``oversize_page`` block into ``_pending_questions.md`` on EVERY suppressed
    merge -- ~10 hub pages generated 8,502 duplicate blocks on the live
    corpus (issue athenaeum#1430). ``tier4_escalate`` now suppresses a new
    oversize-page-family escalation (:data:`OVERSIZE_ESCALATION_CONFLICT_TYPES`)
    for an entity that already has one UNANSWERED, keyed on "currently
    unanswered" so an answered/archived block lets a fresh one through.

    These tests build ``oversize_page`` items directly via
    :func:`_oversize_page_item` rather than through ``check_page_size_gate``
    (issue athenaeum#2078 retired that return path from the real gate --
    see that helper's docstring); the dedup/collapse invariant under test
    here is unchanged and still asserted end to end through the real
    ``tier4_escalate``.
    """

    def test_second_suppressed_merge_does_not_duplicate_the_block(
        self, wiki_dir: Path
    ) -> None:
        """AC2's counter-example: two separate suppressed merges against the
        same over-threshold entity leave exactly ONE unanswered
        oversize_page block, not two -- an explicit BEFORE/AFTER
        block-count assertion."""
        pending = wiki_dir / "_pending_questions.md"

        # First suppressed merge -- creates the one durable block.
        first = _oversize_page_item("Kromatic", "sessions/one.md")
        tier4_escalate([first], pending)

        before_count = pending.read_text().count("**Conflict type**: oversize_page")
        assert before_count == 1

        # Second suppressed merge, same entity -- this is the defect:
        # pre-fix, this appended a SECOND block.
        second = _oversize_page_item("Kromatic", "sessions/two.md")
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

        item = _oversize_page_item("Fresh Entity", "sessions/x.md")
        suppressed = tier4_escalate([item], pending)

        assert suppressed == 0
        content = pending.read_text()
        assert content.count("**Conflict type**: oversize_page") == 1
        assert 'Entity: "Fresh Entity"' in content

    def test_distinct_entities_each_get_their_own_block(self, wiki_dir: Path) -> None:
        """Two DIFFERENT oversized entities must not collapse into one --
        dedup is per-entity, not global."""
        pending = wiki_dir / "_pending_questions.md"
        item_a = _oversize_page_item("Entity A", "sessions/a.md")
        item_b = _oversize_page_item("Entity B", "sessions/b.md")
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

        first = _oversize_page_item("Kromatic", "sessions/one.md")
        tier4_escalate([first], pending)
        assert pending.read_text().count("**Conflict type**: oversize_page") == 1

        # Issue athenaeum#1804: the raw_ref ("sessions/one.md") must resolve, or
        # this non-empty-answer/detector-raised block is now HELD instead of
        # archived — this test is about dedup-on-answer, not the hold path.
        (raw_root / "sessions").mkdir(parents=True, exist_ok=True)
        (raw_root / "sessions" / "one.md").write_text("Kromatic notes.\n", encoding="utf-8")

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
        second = _oversize_page_item("Kromatic", "sessions/two.md")
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
        first = _oversize_page_item("Kromatic", "sessions/one.md")
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
        item1 = _oversize_page_item("Kromatic", "sessions/one.md")
        item2 = _oversize_page_item("Kromatic", "sessions/two.md")
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
# Issue athenaeum#2550 AC2: archived blocks of the retired ``oversize_page``/
# ``oversize_split`` types must still parse. Neither type is produced by
# live code any more (``check_page_size_gate`` never returns ``oversize_page``
# since athenaeum#2078, and never returns ``oversize_split`` since this
# issue retired the reactive split) -- but a corpus that ran an older
# release can carry archived blocks of both, and the issue's own Plan says
# to "keep the parser tolerant by leaving the type names in the set of
# known archived types" (:data:`OVERSIZE_ESCALATION_CONFLICT_TYPES`,
# unchanged by this issue).
# ---------------------------------------------------------------------------


class TestArchivedOversizeBlocksStillParse:
    def test_one_of_each_retired_type_parses_from_the_archive_file(
        self, tmp_path: Path
    ) -> None:
        """A real ``_pending_questions_archive.md`` shape (issue athenaeum#1430's
        ``_render_archive_block`` trailer), carrying one answered
        ``oversize_page`` block and one answered ``oversize_split`` block
        side by side -- both parse, with ``conflict_type`` intact, and
        neither is silently dropped the way an unparseable block would be
        by :func:`athenaeum.answers.parse_pending_questions`."""
        from athenaeum.answers import parse_pending_questions

        archive = tmp_path / "_pending_questions_archive.md"
        oversize_page_block = (
            '## [2026-09-01] Entity: "Old Hub Page" (from sessions/one.md)\n'
            "- [x] over threshold?\n\nSplit it.\n\n"
            "**Conflict type**: oversize_page\n"
            "**Description**: over threshold, ref=sessions/one.md\n\n"
            "**Archived**: 2026-09-02T00:00:00Z\n"
        )
        oversize_split_block = (
            '## [2026-09-01] Entity: "Old Hub Page 2" (from sessions/two.md)\n'
            "- [x] already split?\n\nAcknowledged.\n\n"
            "**Conflict type**: oversize_split\n"
            "**Description**: was split into linked atomic pages, "
            "ref=sessions/two.md\n\n"
            "**Archived**: 2026-09-02T00:00:00Z\n"
        )
        archive.write_text(
            "# Answered Questions\n\n"
            + oversize_page_block
            + "\n\n---\n\n"
            + oversize_split_block
            + "\n",
            encoding="utf-8",
        )

        parsed = parse_pending_questions(archive)

        assert len(parsed) == 2
        by_entity = {pq.entity: pq for pq in parsed}
        assert by_entity["Old Hub Page"].conflict_type == "oversize_page"
        assert by_entity["Old Hub Page"].answered is True
        assert by_entity["Old Hub Page 2"].conflict_type == "oversize_split"
        assert by_entity["Old Hub Page 2"].answered is True
        # Both retired types are still members of the known-archived-types
        # set the issue's Plan asked to leave untouched.
        assert "oversize_page" in OVERSIZE_ESCALATION_CONFLICT_TYPES
        assert "oversize_split" in OVERSIZE_ESCALATION_CONFLICT_TYPES


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
        dest = _perform_oversize_log_demote(target, wiki, config)
        assert dest is not None
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

        Uses ``entity_type="company"`` rather than ``"person"`` to keep this
        test orthogonal to person-specific behavior: the never-tier-3-rewrite
        guard that once refused every ``type: person`` create unconditionally
        (``PersonNeverLLMRewriteError`` / ``_refuse_person_rewrite`` in
        ``athenaeum.tiers``, issue athenaeum#1183 AC4) was withdrawn by
        operator ruling and removed (athenaeum#1597 AC1) — ``type: person``
        creates now go through the same path as any other entity type. The
        retired-name guard's own over-suppression risk is about NAME
        identity, not entity TYPE, so a same-run, different-type create is
        still the faithful negative case.
        """
        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        wiki.mkdir(parents=True)
        other_target = wiki / "bbbb2222-cicero.md"
        other_target.write_text(
            "---\nuid: bbbb2222\ntype: person\nname: cicero\n---\n\nPersona content.\n"
        )
        config = {"librarian": {"preserved_log_dir": "logs"}}
        dest = _perform_oversize_log_demote(other_target, wiki, config)
        assert dest is not None

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
