# SPDX-License-Identifier: Apache-2.0
"""Tests for the discover_* family's adjacent-wrong-root guard (issue athenaeum#1134).

Background: ``discover_auto_memory_files(knowledge_root, config)`` and
``discover_raw_files(raw_root, config)`` (plus
``discover_shape_rule_extra_intake_files`` and ``discover_raw_backlog_bytes``,
which delegate to one of the above) take adjacent, type-identical ``Path``
arguments -- ``knowledge_root`` and ``raw_root = knowledge_root / "raw"``.
Before this issue, passing one where the other was expected silently
returned ``[]`` instead of raising, and that plausible zero read exactly
like a truthful "nothing here" (see the issue body's n=3 session retro).

This file covers:

- AC1: the adjacent-wrong-root case raises, naming the argument, for EACH
  ``discover_*`` function in :mod:`athenaeum.intake`.
- AC2: a correctly-identified root with nothing configured still returns
  ``[]`` (also covered by pre-existing tests -- see the docstring on each
  class below for cross-references).
- AC3: ``config={}`` and ``config=None`` carry documented, tested, DISTINCT
  meanings for ``discover_auto_memory_files``.
- AC4: negative tests for every ``discover_*`` function in this module.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.intake import (
    discover_auto_memory_files,
    discover_raw_backlog_bytes,
    discover_raw_files,
    discover_shape_rule_extra_intake_files,
)

# ---------------------------------------------------------------------------
# AC1 + AC4: discover_auto_memory_files(raw_root passed as knowledge_root)
# ---------------------------------------------------------------------------


def _build_real_auto_memory_tree(knowledge_root: Path) -> Path:
    """Build a real ``knowledge_root/raw/auto-memory/<scope>/`` tree with one
    conforming file, mirroring production layout. Returns ``raw/auto-memory``.
    """
    auto = knowledge_root / "raw" / "auto-memory"
    scope = auto / "_unscoped"
    scope.mkdir(parents=True)
    (scope / "project_note.md").write_text(
        "---\nname: a note\ntype: project\n---\nBody.\n", encoding="utf-8"
    )
    return auto


class TestDiscoverAutoMemoryFilesWrongRoot:
    """AC1: passing ``raw_root`` where ``knowledge_root`` is expected raises,
    naming ``knowledge_root`` -- this is the EXACT repro from the issue body
    (``discover_auto_memory_files(raw, cfg)`` silently returning ``0``)."""

    def test_raw_root_passed_as_knowledge_root_raises(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        _build_real_auto_memory_tree(knowledge_root)
        raw_root = knowledge_root / "raw"

        # The wrong-but-plausible call: raw_root has the same Path type as
        # knowledge_root, and prior to this issue this call returned [].
        with pytest.raises(ValueError, match="knowledge_root"):
            discover_auto_memory_files(raw_root)

    def test_error_names_the_offending_path(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        _build_real_auto_memory_tree(knowledge_root)
        raw_root = knowledge_root / "raw"

        with pytest.raises(ValueError) as exc_info:
            discover_auto_memory_files(raw_root)
        assert str(raw_root) in str(exc_info.value)

    def test_correct_call_still_finds_the_files(self, tmp_path: Path) -> None:
        """Sanity: the CORRECT call on the same tree is unaffected."""
        knowledge_root = tmp_path / "knowledge"
        _build_real_auto_memory_tree(knowledge_root)

        files = discover_auto_memory_files(knowledge_root)
        assert len(files) == 1


class TestDiscoverAutoMemoryFilesCorrectlyEmpty:
    """AC2: a correctly-identified knowledge_root with no extra intake
    configured/present still returns ``[]``, never raises. This is the
    bypass this issue must preserve: "a caller that genuinely configures
    zero extra intake roots must still receive an empty list, not an
    exception." Also see the pre-existing
    ``TestDiscoverAutoMemoryFiles::test_missing_knowledge_root_returns_empty``
    in ``tests/test_librarian_auto_memory.py``, which this test deliberately
    parallels.
    """

    def test_bare_fresh_knowledge_root_returns_empty(self, tmp_path: Path) -> None:
        # A brand new knowledge root with NEITHER wiki/ nor raw/ created
        # yet -- a legitimate, if premature, call. No auto-memory/ (or any
        # other) directory sits directly inside it, so there is no
        # shifted-path signature of the raw_root mistake to detect.
        bare = tmp_path / "bare"
        bare.mkdir()
        assert discover_auto_memory_files(bare) == []

    def test_knowledge_root_with_recall_explicitly_disabled_returns_empty(
        self, tmp_path: Path
    ) -> None:
        knowledge_root = tmp_path / "knowledge"
        _build_real_auto_memory_tree(knowledge_root)
        # A genuine config that turns extras off entirely -- must return
        # [], not raise, even though raw/auto-memory exists on disk.
        config = {"recall": {"extra_intake_roots": []}}
        assert discover_auto_memory_files(knowledge_root, config=config) == []


# ---------------------------------------------------------------------------
# AC3: config={} vs config=None are distinct for discover_auto_memory_files
# ---------------------------------------------------------------------------


class TestConfigNoneVersusEmptyDict:
    """Pins the reversible default this issue's PR chose: ``config={}`` is
    taken LITERALLY (never merged with disk/defaults), exactly like every
    other ``resolve_*`` helper in :mod:`athenaeum.config` treats an
    explicit config dict. ``config=None`` loads ``athenaeum.yaml`` merged
    with code defaults, whose default ``recall.extra_intake_roots`` is
    ``["raw/auto-memory"]``.

    Rationale (recorded here, not just in the PR body, so a future reader
    hits the test before hitting the retro again): changing ``{}`` to
    behave like ``None`` would mean a caller who explicitly builds a
    config dict with no ``recall`` key -- e.g. one assembled by a caller
    that intentionally scopes ``recall`` out -- would silently REACTIVATE
    the default extra intake roots. That is a bigger, harder-to-audit
    behavior change than documenting and testing the existing split.
    """

    def test_config_none_loads_defaults_and_finds_files(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        _build_real_auto_memory_tree(knowledge_root)
        # No athenaeum.yaml on disk -> load_config() falls back to the
        # in-code defaults, whose recall.extra_intake_roots is
        # ["raw/auto-memory"].
        files = discover_auto_memory_files(knowledge_root, config=None)
        assert len(files) == 1

    def test_config_empty_dict_disables_extras_and_returns_empty(
        self, tmp_path: Path
    ) -> None:
        knowledge_root = tmp_path / "knowledge"
        _build_real_auto_memory_tree(knowledge_root)
        # config={} is NOT merged with defaults -- it is the whole config,
        # and it has no "recall" key, so resolve_extra_intake_roots finds
        # nothing configured. Distinct from config=None on the SAME tree.
        files = discover_auto_memory_files(knowledge_root, config={})
        assert files == []

    def test_config_recall_less_dict_also_disables_extras(
        self, tmp_path: Path
    ) -> None:
        knowledge_root = tmp_path / "knowledge"
        _build_real_auto_memory_tree(knowledge_root)
        # A non-empty config dict that simply omits "recall" behaves the
        # same as {} -- confirms the distinction is about the presence of
        # the recall/extra_intake_roots key, not dict emptiness per se.
        files = discover_auto_memory_files(
            knowledge_root, config={"vector": {"provider": "chromadb"}}
        )
        assert files == []


# ---------------------------------------------------------------------------
# AC1 + AC4: discover_raw_files(knowledge_root passed as raw_root)
# ---------------------------------------------------------------------------


def _build_knowledge_root_with_wiki_and_raw(tmp_path: Path) -> Path:
    knowledge_root = tmp_path / "knowledge"
    (knowledge_root / "wiki").mkdir(parents=True)
    raw = knowledge_root / "raw"
    sessions = raw / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "20260821T000000Z-aaaaaaaa.md").write_text(
        "An observation.\n", encoding="utf-8"
    )
    return knowledge_root


class TestDiscoverRawFilesWrongRoot:
    def test_knowledge_root_passed_as_raw_root_raises(self, tmp_path: Path) -> None:
        knowledge_root = _build_knowledge_root_with_wiki_and_raw(tmp_path)

        with pytest.raises(ValueError, match="raw_root"):
            discover_raw_files(knowledge_root)

    def test_error_names_the_offending_path(self, tmp_path: Path) -> None:
        knowledge_root = _build_knowledge_root_with_wiki_and_raw(tmp_path)

        with pytest.raises(ValueError) as exc_info:
            discover_raw_files(knowledge_root)
        assert str(knowledge_root) in str(exc_info.value)

    def test_correct_call_still_finds_the_files(self, tmp_path: Path) -> None:
        knowledge_root = _build_knowledge_root_with_wiki_and_raw(tmp_path)

        files = discover_raw_files(knowledge_root / "raw")
        assert len(files) == 1

    def test_correctly_empty_raw_root_still_returns_empty(
        self, tmp_path: Path
    ) -> None:
        """AC2 for discover_raw_files: an existing but empty raw root (no
        wiki/ or raw/ children of its own) returns [], never raises. Also
        see the pre-existing ``test_empty_dir``/``test_nonexistent_dir`` in
        ``tests/test_librarian.py``.
        """
        empty_raw_root = tmp_path / "raw"
        empty_raw_root.mkdir()
        assert discover_raw_files(empty_raw_root) == []


class TestDiscoverShapeRuleExtraIntakeFilesWrongRoot:
    def test_knowledge_root_passed_as_raw_root_raises(self, tmp_path: Path) -> None:
        knowledge_root = _build_knowledge_root_with_wiki_and_raw(tmp_path)

        with pytest.raises(ValueError, match="raw_root"):
            discover_shape_rule_extra_intake_files(knowledge_root)

    def test_correctly_empty_raw_root_still_returns_empty(
        self, tmp_path: Path
    ) -> None:
        empty_raw_root = tmp_path / "raw"
        empty_raw_root.mkdir()
        assert discover_shape_rule_extra_intake_files(empty_raw_root) == []


class TestDiscoverRawBacklogBytesWrongRoot:
    """discover_raw_backlog_bytes delegates entirely to discover_raw_files,
    so the guard fires transitively -- pinned here so a future refactor
    that stops delegating gets caught by this test failing."""

    def test_knowledge_root_passed_as_raw_root_raises(self, tmp_path: Path) -> None:
        knowledge_root = _build_knowledge_root_with_wiki_and_raw(tmp_path)

        with pytest.raises(ValueError, match="raw_root"):
            discover_raw_backlog_bytes(knowledge_root)

    def test_correctly_empty_raw_root_still_returns_zero(
        self, tmp_path: Path
    ) -> None:
        empty_raw_root = tmp_path / "raw"
        empty_raw_root.mkdir()
        assert discover_raw_backlog_bytes(empty_raw_root) == 0
