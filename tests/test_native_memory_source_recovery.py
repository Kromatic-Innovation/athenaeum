# SPDX-License-Identifier: Apache-2.0
"""Origin-session recovery for natively-written auto-memory (issue athenaeum#1452).

Claude Code's NATIVE memory writer emits ``name`` / ``description`` /
``metadata.type`` and nothing else — no ``sources[]``, no ``originSessionId``.
Both of the librarian's provenance paths were therefore dead by construction
for those files, and 783 of 963 compiled ``type: auto-memory`` pages carried
``sources: []``.

These tests pin the four acceptance criteria:

1. A page compiled from a natively-written member carries a NON-EMPTY
   ``sources[]`` (``TestCompiledPageCarriesSources``).
2. That is a real reduction, not a fixture artifact — the same corpus compiles
   with empty sources when the recovery input is absent
   (``test_same_corpus_without_transcripts_still_empty``).
3. The recovered ``source_type`` is the honest ``inferred``, never an
   unearned ``user-stated`` / ``external``
   (``test_recovered_source_type_is_inferred``).
4. The ultimate-source invariant holds — no footnote or source ref cites a
   raw ``auto-memory/...`` filename (``test_no_source_cites_raw_filename``).

Every test injects a synthetic ``projects_root`` and knowledge tree under
``tmp_path``; the real ``~/.claude`` and ``~/knowledge`` are never touched.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from athenaeum.intake import discover_auto_memory_files
from athenaeum.merge import AUTO_WIKI_PREFIX, merge_clusters_to_wiki
from athenaeum.models import DEFAULT_SOURCE_TYPE
from athenaeum.session_recovery import (
    BASIS_TIME_WINDOW,
    BASIS_WRITE_CITED,
    SessionRecoverer,
    default_projects_root,
    written_at_from_frontmatter,
)

SCOPE = "-Users-alice-Code-projectx"
MEMORY_NAME = "athenaeum-egress-is-unfiltered.md"
WRITER_SESSION = "11111111-2222-3333-4444-555555555555"
OTHER_SESSION = "99999999-8888-7777-6666-555555555555"

T0 = datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc)


def _ts(offset_minutes: int) -> str:
    return (T0 + timedelta(minutes=offset_minutes)).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Fixture builders


def _write_transcript(
    projects_root: Path,
    session_id: str,
    records: list[dict[str, object]],
    scope: str = SCOPE,
) -> Path:
    scope_dir = projects_root / scope
    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / f"{session_id}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _stamp(record: dict[str, object], offset_minutes: int) -> dict[str, object]:
    record["timestamp"] = _ts(offset_minutes)
    return record


def _tool_use_record(
    tool: str,
    path: str,
    offset_minutes: int,
    session_id: str = WRITER_SESSION,
) -> dict[str, object]:
    """An assistant turn invoking a tool against ``path``."""
    return _stamp(
        {
            "type": "assistant",
            "sessionId": session_id,
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": tool,
                        "input": {"file_path": path, "content": "..."},
                    }
                ],
            },
        },
        offset_minutes,
    )


def _native_path(projects_root: Path, filename: str = MEMORY_NAME, scope: str = SCOPE) -> str:
    """The path Claude Code's native writer targets, as a transcript records it.

    Derived from the INJECTED ``projects_root`` rather than written as an
    absolute literal — that is both what a real transcript would contain for
    this fixture and the only form that keeps a hard-coded home directory out
    of a public repo.
    """
    return f"{projects_root / scope / 'memory' / filename}"


def _text_record(text: str, offset_minutes: int) -> dict[str, object]:
    return _stamp({"type": "user", "message": {"role": "user", "content": text}}, offset_minutes)


def _native_memory_text(name: str = "athenaeum-egress-is-unfiltered") -> str:
    """Exactly the frontmatter Claude Code's native writer emits — nothing more.

    No ``sources``, no ``originSessionId``, no ``modified``. Copied in shape
    from a real file in the corpus that motivated this issue.
    """
    return (
        "---\n"
        f"name: {name}\n"
        "description: box-claude containers have NO egress filtering on either path.\n"
        "metadata:\n"
        "  type: reference\n"
        "---\n"
        "Measured 2026-08-15 on this host, recorded at cwc#2508.\n"
    )


def _write_native_memory(
    knowledge_root: Path,
    filename: str = MEMORY_NAME,
    *,
    scope: str = SCOPE,
    text: str | None = None,
    mtime_offset_minutes: int | None = None,
) -> Path:
    scope_dir = knowledge_root / "raw" / "auto-memory" / scope
    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / filename
    path.write_text(
        text if text is not None else _native_memory_text(Path(filename).stem),
        encoding="utf-8",
    )
    if mtime_offset_minutes is not None:
        # In production each scope in the raw intake tree is a SYMLINK to the
        # live ``~/.claude/projects/<scope>/memory/`` directory, so ``stat``
        # follows through and the mtime is the real write time. Emulate that.
        stamp = (T0 + timedelta(minutes=mtime_offset_minutes)).timestamp()
        os.utime(path, (stamp, stamp))
    return path


def _write_config(knowledge_root: Path) -> None:
    (knowledge_root / "athenaeum.yaml").write_text(
        "recall:\n  extra_intake_roots:\n    - raw/auto-memory\n",
        encoding="utf-8",
    )
    (knowledge_root / "wiki").mkdir(parents=True, exist_ok=True)


def _write_cluster(knowledge_root: Path, member_paths: list[str]) -> None:
    (knowledge_root / "raw" / "_librarian-clusters.jsonl").write_text(
        json.dumps(
            {
                "cluster_id": "native-0001",
                "member_paths": member_paths,
                "centroid_score": 1.0,
                "rationale": "singleton",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.fixture
def native_corpus(tmp_path: Path) -> tuple[Path, Path]:
    """A knowledge root holding ONE natively-written memory, plus transcripts.

    The writing session shows a ``Write`` tool-use naming the memory file; a
    second, concurrent session in the same scope does not — so the exact
    (``write-cited``) rung is what resolves it, and a naive "any overlapping
    session" match would be ambiguous.
    """
    knowledge_root = tmp_path / "knowledge"
    projects_root = tmp_path / "projects"
    _write_native_memory(knowledge_root, mtime_offset_minutes=5)
    _write_config(knowledge_root)
    _write_cluster(knowledge_root, [f"{SCOPE}/{MEMORY_NAME}"])

    native_path = _native_path(projects_root)
    _write_transcript(
        projects_root,
        WRITER_SESSION,
        [
            _text_record("remember the egress finding", 0),
            _tool_use_record("Write", native_path, 5),
            _text_record("thanks", 8),
        ],
    )
    _write_transcript(
        projects_root,
        OTHER_SESSION,
        [
            _text_record("unrelated work in the same project", 0),
            _text_record("still unrelated", 9),
        ],
    )
    return knowledge_root, projects_root


# ---------------------------------------------------------------------------
# AC1 + AC2: the compiled page carries sources, and only because of recovery


class TestCompiledPageCarriesSources:
    def test_natively_written_member_compiles_with_sources(
        self, native_corpus: tuple[Path, Path]
    ) -> None:
        knowledge_root, projects_root = native_corpus
        (entry,) = merge_clusters_to_wiki(knowledge_root, projects_root=projects_root)
        assert entry.sources, "AC1: a natively-written member must not compile to sources: []"
        assert entry.sources[0]["session"] == WRITER_SESSION

    def test_same_corpus_without_transcripts_still_empty(self, tmp_path: Path) -> None:
        """AC2 control: the drop is caused by recovery, not by the fixture.

        Byte-identical corpus, but the transcript root is empty — which is
        exactly the pre-athenaeum#1452 world. It must still compile to
        ``sources: []``, otherwise the positive test above proves nothing.
        """
        knowledge_root = tmp_path / "knowledge"
        empty_projects = tmp_path / "empty-projects"
        empty_projects.mkdir()
        _write_native_memory(knowledge_root, mtime_offset_minutes=5)
        _write_config(knowledge_root)
        _write_cluster(knowledge_root, [f"{SCOPE}/{MEMORY_NAME}"])

        (entry,) = merge_clusters_to_wiki(knowledge_root, projects_root=empty_projects)
        assert entry.sources == []

    def test_intake_stamps_the_recovered_session(self, native_corpus: tuple[Path, Path]) -> None:
        knowledge_root, projects_root = native_corpus
        (am,) = discover_auto_memory_files(knowledge_root, projects_root=projects_root)
        assert am.origin_session_id == WRITER_SESSION
        # No turn is invented — the native writer stamps none.
        assert am.origin_turn is None


# ---------------------------------------------------------------------------
# AC3: honest source_type


class TestHonestSourceType:
    def test_recovered_source_type_is_inferred(self, native_corpus: tuple[Path, Path]) -> None:
        knowledge_root, projects_root = native_corpus
        (entry,) = merge_clusters_to_wiki(knowledge_root, projects_root=projects_root)
        (source,) = entry.sources
        assert source["source_type"] == DEFAULT_SOURCE_TYPE == "inferred"

    def test_recovery_never_upgrades_a_declared_type(self, tmp_path: Path) -> None:
        """Recovery supplies an ORIGIN, never a verdict.

        A file that declares its own ``source_type`` keeps it verbatim — the
        recovered session must not overwrite, upgrade, or downgrade it.
        """
        knowledge_root = tmp_path / "knowledge"
        projects_root = tmp_path / "projects"
        text = (
            "---\n"
            "name: declared-type-memory\n"
            "description: A memory that declares its own provenance type.\n"
            "metadata:\n"
            "  type: reference\n"
            "source_type: user-stated\n"
            "---\n"
            "The user said so.\n"
        )
        _write_native_memory(
            knowledge_root,
            "declared-type-memory.md",
            text=text,
            mtime_offset_minutes=5,
        )
        _write_config(knowledge_root)
        _write_cluster(knowledge_root, [f"{SCOPE}/declared-type-memory.md"])
        _write_transcript(
            projects_root,
            WRITER_SESSION,
            [
                _text_record("hello", 0),
                _tool_use_record(
                    "Write",
                    _native_path(projects_root, "declared-type-memory.md"),
                    5,
                ),
            ],
        )

        (entry,) = merge_clusters_to_wiki(knowledge_root, projects_root=projects_root)
        (source,) = entry.sources
        assert source["source_type"] == "user-stated"


# ---------------------------------------------------------------------------
# AC4: the ultimate-source invariant


class TestUltimateSourceInvariant:
    def test_no_source_cites_raw_filename(self, native_corpus: tuple[Path, Path]) -> None:
        knowledge_root, projects_root = native_corpus
        (entry,) = merge_clusters_to_wiki(knowledge_root, projects_root=projects_root)
        for source in entry.sources:
            ref = str(source.get("source_ref", ""))
            assert not ref.endswith(".md"), ref
            assert "auto-memory" not in ref
            assert MEMORY_NAME not in ref
        assert source["source_ref"] == WRITER_SESSION

    def test_rendered_page_footnote_cites_no_filename(
        self, native_corpus: tuple[Path, Path]
    ) -> None:
        knowledge_root, projects_root = native_corpus
        merge_clusters_to_wiki(knowledge_root, projects_root=projects_root)
        (page,) = list((knowledge_root / "wiki").glob(f"{AUTO_WIKI_PREFIX}*.md"))
        rendered = page.read_text(encoding="utf-8")
        assert WRITER_SESSION in rendered
        assert "raw/auto-memory" not in rendered


# ---------------------------------------------------------------------------
# The recovery primitive itself


class TestWriteCitedRung:
    def test_exact_attribution_beats_an_overlapping_session(
        self, native_corpus: tuple[Path, Path]
    ) -> None:
        _knowledge_root, projects_root = native_corpus
        recovered = SessionRecoverer(projects_root).recover(Path(MEMORY_NAME), SCOPE)
        assert recovered is not None
        assert recovered.session_id == WRITER_SESSION
        assert recovered.basis == BASIS_WRITE_CITED

    @pytest.mark.parametrize(
        "tool", ["Write", "Edit", "MultiEdit", "NotebookEdit", "mcp__plugin_woz_code__Edit"]
    )
    def test_every_writing_tool_attributes(self, tmp_path: Path, tool: str) -> None:
        projects_root = tmp_path / "projects"
        _write_transcript(
            projects_root,
            WRITER_SESSION,
            [
                _tool_use_record(
                    tool,
                    _native_path(projects_root),
                    3,
                )
            ],
        )
        recovered = SessionRecoverer(projects_root).recover(Path(MEMORY_NAME), SCOPE)
        assert recovered is not None and recovered.basis == BASIS_WRITE_CITED

    def test_a_read_does_not_attribute(self, tmp_path: Path) -> None:
        """Reading a memory proves nothing about who wrote it.

        Memory files are injected into context constantly, so a read-inclusive
        match would attribute a memory to whichever session merely looked at
        it. With only a ``Read``, nothing resolves (no window overlap either,
        since the file's mtime is ``now``).
        """
        projects_root = tmp_path / "projects"
        memory = _write_native_memory(tmp_path / "knowledge", mtime_offset_minutes=3)
        _write_transcript(
            projects_root,
            WRITER_SESSION,
            [
                _tool_use_record(
                    "Read",
                    _native_path(projects_root),
                    2,
                ),
                _text_record("done", 4),
            ],
        )
        recovered = SessionRecoverer(projects_root).recover(memory, SCOPE)
        # The window rung still matches (single session), but NOT via citation.
        assert recovered is not None
        assert recovered.basis == BASIS_TIME_WINDOW

    def test_another_projects_memory_write_does_not_attribute(self, tmp_path: Path) -> None:
        """A same-named memory in a DIFFERENT project must not be claimed here.

        Agents do reach across projects, so a session whose transcript lives in
        this scope can legitimately write another scope's memory file. Matching
        on a bare ``/memory/`` substring would index that basename here and
        attribute this scope's same-named memory to a session that never wrote
        it — fabricated provenance, the worst outcome this module can produce.
        """
        projects_root = tmp_path / "projects"
        other_scope = "-Users-alice-Code-otherproject"
        # ONE transcript, living in THIS scope, writing two memory files: one
        # belonging to another project, one belonging to this one.
        _write_transcript(
            projects_root,
            WRITER_SESSION,
            [
                _tool_use_record(
                    "Write",
                    _native_path(projects_root, MEMORY_NAME, other_scope),
                    5,
                ),
                _tool_use_record("Write", _native_path(projects_root, "own-memory.md", SCOPE), 6),
            ],
        )
        # Both memories sit outside the transcript's window (so a citation is
        # the ONLY thing that could resolve either) but well inside
        # MAX_CITATION_LAG of the cited writes, so the staleness bound is not
        # what is doing the work in this test.
        knowledge_root = tmp_path / "knowledge"
        foreign = _write_native_memory(knowledge_root, mtime_offset_minutes=600)
        own = _write_native_memory(knowledge_root, "own-memory.md", mtime_offset_minutes=600)
        recoverer = SessionRecoverer(projects_root)
        # The cross-project write must NOT be claimed by this scope...
        assert recoverer.recover(foreign, SCOPE) is None
        # ...while the same transcript's write into THIS scope still resolves,
        # so the anchoring narrowed the match rather than disabling it.
        recovered = recoverer.recover(own, SCOPE)
        assert recovered is not None and recovered.basis == BASIS_WRITE_CITED

    def test_latest_writer_wins(self, tmp_path: Path) -> None:
        """The file's CURRENT content is what the last write left behind."""
        projects_root = tmp_path / "projects"
        native_path = _native_path(projects_root)
        _write_transcript(
            projects_root,
            OTHER_SESSION,
            [_tool_use_record("Write", native_path, 1, session_id=OTHER_SESSION)],
        )
        _write_transcript(
            projects_root,
            WRITER_SESSION,
            [_tool_use_record("Edit", native_path, 30)],
        )
        recovered = SessionRecoverer(projects_root).recover(Path(MEMORY_NAME), SCOPE)
        assert recovered is not None and recovered.session_id == WRITER_SESSION


class TestStaleCitationBound:
    """A citation only attributes the content the file HOLDS NOW."""

    def test_stale_citation_does_not_attribute_a_later_rewrite(self, tmp_path: Path) -> None:
        """The regression this bound exists for.

        Session A wrote the memory and its transcript SURVIVES. Session B
        genuinely rewrote it much later and its transcript has ROLLED OFF. The
        only surviving citation is A's — and attributing B's content to A is a
        confident wrong answer, strictly worse than the honest ``None`` the
        window rung would return.
        """
        projects_root = tmp_path / "projects"
        _write_transcript(
            projects_root,
            OTHER_SESSION,
            [_tool_use_record("Write", _native_path(projects_root), 0, session_id=OTHER_SESSION)],
        )
        # Rewritten 30 days after the surviving citation.
        memory = _write_native_memory(tmp_path / "knowledge", mtime_offset_minutes=60 * 24 * 30)
        assert SessionRecoverer(projects_root).recover(memory, SCOPE) is None

    def test_a_citation_within_the_lag_still_attributes(self, tmp_path: Path) -> None:
        """The bound must not disable the rung it guards."""
        projects_root = tmp_path / "projects"
        _write_transcript(
            projects_root,
            WRITER_SESSION,
            [_tool_use_record("Write", _native_path(projects_root), 0)],
        )
        memory = _write_native_memory(tmp_path / "knowledge", mtime_offset_minutes=60)
        recovered = SessionRecoverer(projects_root).recover(memory, SCOPE)
        assert recovered is not None and recovered.basis == BASIS_WRITE_CITED

    def test_citation_newer_than_the_mtime_is_accepted(self, tmp_path: Path) -> None:
        """Only an implausibly OLD citation is disqualifying.

        An mtime settling marginally before the transcript record (or plain
        clock drift) must not throw away a good attribution.
        """
        projects_root = tmp_path / "projects"
        _write_transcript(
            projects_root,
            WRITER_SESSION,
            [_tool_use_record("Write", _native_path(projects_root), 90)],
        )
        memory = _write_native_memory(tmp_path / "knowledge", mtime_offset_minutes=0)
        recovered = SessionRecoverer(projects_root).recover(memory, SCOPE)
        assert recovered is not None and recovered.basis == BASIS_WRITE_CITED

    def test_undatable_write_time_still_attributes(self, tmp_path: Path) -> None:
        """With no write time knowable, the citation is the only evidence there is."""
        projects_root = tmp_path / "projects"
        _write_transcript(
            projects_root,
            WRITER_SESSION,
            [_tool_use_record("Write", _native_path(projects_root), 0)],
        )
        # A path that does not exist: no frontmatter, no stat-able mtime.
        recovered = SessionRecoverer(projects_root).recover(
            tmp_path / "absent" / MEMORY_NAME, SCOPE
        )
        assert recovered is not None and recovered.basis == BASIS_WRITE_CITED


class TestWindowBoundsComeFromRecords:
    def test_a_timestamp_inside_written_content_does_not_move_the_window(
        self, tmp_path: Path
    ) -> None:
        """The window must be read from the RECORD, not from the raw line text.

        A ``Write`` whose CONTENT embeds a literal ``"timestamp"`` key — normal
        for JSON, YAML or log text — would pollute a regex-derived bound. The
        window feeds the ``time-window`` rung, so a polluted bound is a
        fabrication risk, not a cosmetic one.
        """
        projects_root = tmp_path / "projects"
        poisoned = _stamp(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Write",
                            "input": {
                                "file_path": str(tmp_path / "notes.md"),
                                "content": '{"timestamp": "2001-01-01T00:00:00Z"}',
                            },
                        }
                    ],
                },
            },
            0,
        )
        _write_transcript(projects_root, WRITER_SESSION, [poisoned, _text_record("end", 9)])
        from athenaeum.session_recovery import _scan_transcript

        _writes, first, last = _scan_transcript(
            projects_root / SCOPE / f"{WRITER_SESSION}.jsonl", SCOPE
        )
        assert first == T0
        assert last == T0 + timedelta(minutes=9)

    def test_a_truncated_final_line_falls_back_to_the_regex(self, tmp_path: Path) -> None:
        """A live transcript's last line is often mid-append."""
        from athenaeum.session_recovery import _scan_transcript

        projects_root = tmp_path / "projects"
        scope_dir = projects_root / SCOPE
        scope_dir.mkdir(parents=True)
        # Cut the closing brace only: JSON can no longer parse it, but the
        # record's own timestamp is intact for the regex fallback to find.
        truncated = json.dumps(_text_record("end", 9))[:-1]
        (scope_dir / f"{WRITER_SESSION}.jsonl").write_text(
            json.dumps(_text_record("start", 0)) + "\n" + truncated + "\n",
            encoding="utf-8",
        )
        _writes, first, last = _scan_transcript(scope_dir / f"{WRITER_SESSION}.jsonl", SCOPE)
        assert first == T0
        assert last == T0 + timedelta(minutes=9)


class TestMergeIgnoresProjectsRootWhenMembersSupplied:
    def test_supplied_members_win_and_projects_root_is_inert(
        self, native_corpus: tuple[Path, Path]
    ) -> None:
        """Pins the documented contract, so a future caller cannot regress it.

        ``merge_clusters_to_wiki`` documents that ``projects_root`` is ignored
        when ``auto_memory_files`` is supplied — that list was already
        discovered, recovery and all. Passing members discovered WITHOUT a
        transcript root must therefore still compile to ``sources: []`` even
        though a perfectly good ``projects_root`` is also passed.
        """
        knowledge_root, projects_root = native_corpus
        no_transcripts = knowledge_root / "no-transcripts"
        no_transcripts.mkdir(exist_ok=True)
        unrecovered = discover_auto_memory_files(knowledge_root, projects_root=no_transcripts)
        (entry,) = merge_clusters_to_wiki(
            knowledge_root,
            auto_memory_files=unrecovered,
            projects_root=projects_root,
        )
        assert entry.sources == []
        # ...and the same corpus DOES recover when merge does its own discovery.
        (recovered_entry,) = merge_clusters_to_wiki(knowledge_root, projects_root=projects_root)
        assert recovered_entry.sources


class TestTimeWindowRung:
    def test_unique_containing_window_resolves(self, tmp_path: Path) -> None:
        projects_root = tmp_path / "projects"
        memory = _write_native_memory(tmp_path / "knowledge", mtime_offset_minutes=5)
        _write_transcript(
            projects_root,
            WRITER_SESSION,
            [_text_record("start", 0), _text_record("end", 9)],
        )
        recovered = SessionRecoverer(projects_root).recover(memory, SCOPE)
        assert recovered is not None
        assert recovered.session_id == WRITER_SESSION
        assert recovered.basis == BASIS_TIME_WINDOW

    def test_two_overlapping_sessions_is_unresolved(self, tmp_path: Path) -> None:
        """Concurrent sessions in one project must NOT be coin-flipped."""
        projects_root = tmp_path / "projects"
        memory = _write_native_memory(tmp_path / "knowledge", mtime_offset_minutes=5)
        _write_transcript(
            projects_root,
            WRITER_SESSION,
            [_text_record("start", 0), _text_record("end", 9)],
        )
        _write_transcript(
            projects_root,
            OTHER_SESSION,
            [_text_record("start", 2), _text_record("end", 11)],
        )
        assert SessionRecoverer(projects_root).recover(memory, SCOPE) is None

    def test_rolled_off_transcript_is_unresolved(self, tmp_path: Path) -> None:
        projects_root = tmp_path / "projects"
        memory = _write_native_memory(tmp_path / "knowledge", mtime_offset_minutes=5)
        _write_transcript(
            projects_root,
            WRITER_SESSION,
            [_text_record("start", 600), _text_record("end", 700)],
        )
        assert SessionRecoverer(projects_root).recover(memory, SCOPE) is None

    def test_missing_scope_dir_is_unresolved(self, tmp_path: Path) -> None:
        memory = _write_native_memory(tmp_path / "knowledge", mtime_offset_minutes=5)
        recoverer = SessionRecoverer(tmp_path / "nonexistent")
        assert recoverer.recover(memory, SCOPE) is None
        assert recoverer.recover(memory, "") is None

    def test_frontmatter_modified_outranks_mtime(self, tmp_path: Path) -> None:
        """``modified`` (Claude Code 2.1.214+) is honored when present.

        It is absent on every file in the corpus that motivated athenaeum#1452,
        so mtime is the load-bearing signal — but a writer that DOES stamp it
        must win over an mtime the intake copy could have disturbed.
        """
        projects_root = tmp_path / "projects"
        memory = _write_native_memory(tmp_path / "knowledge", mtime_offset_minutes=900)
        _write_transcript(
            projects_root,
            WRITER_SESSION,
            [_text_record("start", 0), _text_record("end", 9)],
        )
        recoverer = SessionRecoverer(projects_root)
        assert recoverer.recover(memory, SCOPE) is None
        recovered = recoverer.recover(memory, SCOPE, written_at=T0 + timedelta(minutes=5))
        assert recovered is not None and recovered.session_id == WRITER_SESSION


class TestFrontmatterModifiedParsing:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("2026-09-07T10:05:00Z", datetime(2026, 9, 7, 10, 5, tzinfo=timezone.utc)),
            ("2026-09-07T10:05:00+00:00", datetime(2026, 9, 7, 10, 5, tzinfo=timezone.utc)),
            ("2026-09-07T12:05:00+02:00", datetime(2026, 9, 7, 10, 5, tzinfo=timezone.utc)),
        ],
    )
    def test_parses_iso_forms(self, value: str, expected: datetime) -> None:
        assert written_at_from_frontmatter({"modified": value}) == expected

    @pytest.mark.parametrize(
        "meta", [None, {}, {"modified": ""}, {"modified": "not-a-date"}, {"modified": 17}]
    )
    def test_absent_or_unparseable_is_none(self, meta: dict | None) -> None:
        assert written_at_from_frontmatter(meta) is None

    def test_naive_datetime_is_treated_as_utc(self) -> None:
        parsed = written_at_from_frontmatter({"modified": datetime(2026, 9, 7, 10, 5)})
        assert parsed == datetime(2026, 9, 7, 10, 5, tzinfo=timezone.utc)


class TestScanResilience:
    def test_malformed_lines_do_not_abort_the_scan(self, tmp_path: Path) -> None:
        projects_root = tmp_path / "projects"
        scope_dir = projects_root / SCOPE
        scope_dir.mkdir(parents=True)
        good = _tool_use_record("Write", _native_path(projects_root), 5)
        (scope_dir / f"{WRITER_SESSION}.jsonl").write_text(
            "\n".join(
                [
                    json.dumps(_text_record("start", 0)),
                    "{not json at all /memory/x.md",
                    "",
                    json.dumps(good),
                    json.dumps(_text_record("end", 9)),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        recovered = SessionRecoverer(projects_root).recover(Path(MEMORY_NAME), SCOPE)
        assert recovered is not None and recovered.session_id == WRITER_SESSION

    def test_scope_transcripts_are_scanned_once_per_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The per-scope index is what keeps recovery O(transcripts), not O(N x M)."""
        import athenaeum.session_recovery as sr

        projects_root = tmp_path / "projects"
        _write_transcript(
            projects_root, WRITER_SESSION, [_text_record("start", 0), _text_record("end", 9)]
        )
        calls: list[Path] = []
        real = sr._scan_transcript
        monkeypatch.setattr(
            sr,
            "_scan_transcript",
            lambda path, scope="": (calls.append(path), real(path, scope))[1],
        )
        recoverer = sr.SessionRecoverer(projects_root)
        for index in range(5):
            recoverer.recover(Path(f"memory-{index}.md"), SCOPE)
        assert len(calls) == 1


class TestProjectsRootDefault:
    def test_claude_config_dir_is_honored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/tmp/elsewhere")
        assert default_projects_root() == Path("/tmp/elsewhere/projects")
        monkeypatch.delenv("CLAUDE_CONFIG_DIR")
        assert default_projects_root() == Path.home() / ".claude" / "projects"


class TestDeclaredProvenanceIsUntouched:
    def test_a_file_declaring_its_session_is_left_verbatim(self, tmp_path: Path) -> None:
        """The file's own claim always outranks a recovered one."""
        knowledge_root = tmp_path / "knowledge"
        projects_root = tmp_path / "projects"
        text = (
            "---\n"
            "name: declared-session-memory\n"
            "description: Declares its own origin session.\n"
            "metadata:\n"
            "  type: reference\n"
            "originSessionId: declared-sess\n"
            "originTurn: 12\n"
            "---\n"
            "Body.\n"
        )
        _write_native_memory(
            knowledge_root,
            "declared-session-memory.md",
            text=text,
            mtime_offset_minutes=5,
        )
        _write_config(knowledge_root)
        _write_transcript(
            projects_root,
            WRITER_SESSION,
            [
                _tool_use_record(
                    "Write",
                    _native_path(projects_root, "declared-session-memory.md"),
                    5,
                )
            ],
        )
        (am,) = discover_auto_memory_files(knowledge_root, projects_root=projects_root)
        assert am.origin_session_id == "declared-sess"
        assert am.origin_turn == 12

    def test_a_file_with_sources_is_not_touched(self, tmp_path: Path) -> None:
        """``sources[]`` is merge's source of truth; recovery must not intrude."""
        knowledge_root = tmp_path / "knowledge"
        projects_root = tmp_path / "projects"
        text = (
            "---\n"
            "name: cited-memory\n"
            "description: Already cites its origin.\n"
            "metadata:\n"
            "  type: reference\n"
            "sources:\n"
            "  - session: cited-sess\n"
            "    turn: 3\n"
            "---\n"
            "Body.\n"
        )
        _write_native_memory(knowledge_root, "cited-memory.md", text=text, mtime_offset_minutes=5)
        _write_config(knowledge_root)
        _write_cluster(knowledge_root, [f"{SCOPE}/cited-memory.md"])
        _write_transcript(
            projects_root,
            WRITER_SESSION,
            [
                _tool_use_record(
                    "Write",
                    _native_path(projects_root, "cited-memory.md"),
                    5,
                )
            ],
        )
        (am,) = discover_auto_memory_files(knowledge_root, projects_root=projects_root)
        assert am.origin_session_id is None
        (entry,) = merge_clusters_to_wiki(knowledge_root, projects_root=projects_root)
        assert [s["session"] for s in entry.sources] == ["cited-sess"]
