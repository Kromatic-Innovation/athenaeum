# SPDX-License-Identifier: Apache-2.0
"""Tests for the unrecognised-raw-intake audit (issue athenaeum#836).

Organized by acceptance criterion:

- ``TestFindUnclaimedRawFiles`` -> AC1 + AC3: every file neither
  ``discover_raw_files`` nor ``discover_auto_memory_files`` would claim is
  found and reason-tagged; a recognised file (either shape) is never
  flagged; the legitimate entity-schema fall-through inside an auto-memory
  scope dir stays silent.
- ``TestRaiseGroupsNotFiles`` -> AC2: 88-files-of-one-reason arrive as ONE
  raised item, naming the reason, the count, and a bounded path sample.
- ``TestAutoMemoryNamingConvention`` -> AC4.
- ``TestIdempotence`` -> the "Idempotence is mandatory" design constraint:
  two consecutive runs raise the group at most once.
- ``TestResolutionClearsTheFlag`` -> AC6: resolving through the EXISTING
  ``athenaeum.answers.resolve_by_id`` path stops the group from being
  re-raised on the next run, with no hand-edited state.
- ``TestExactlyOneVsZero`` -> AC7: an unrecognised file raises exactly one
  queue item; a recognised file raises none.
- ``TestSummaryCounters`` -> AC5: the run-summary counters this module
  returns are internally consistent (unclaimed/groups/raised/already-open).
"""

from __future__ import annotations

import json
from pathlib import Path

from athenaeum.answers import list_unanswered, parse_pending_questions, resolve_by_id
from athenaeum.compiled_exempt import mark_exempt
from athenaeum.intake_audit import (
    REASON_MISSING_NAMING_CONVENTION,
    REASON_UNMATCHED_EXTENSION,
    REASON_UNRECOGNISED_SHAPE,
    UnclaimedFile,
    find_unclaimed_raw_files,
    raise_unclaimed_files,
    run_intake_audit,
)


def _write(root: Path, rel: str, content: str = "hello\n") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _pending(tmp_path: Path) -> Path:
    return tmp_path / "wiki" / "_pending_questions.md"


def _archive(tmp_path: Path) -> Path:
    return tmp_path / "wiki" / "_pending_questions_archive.md"


# ---------------------------------------------------------------------------
# AC1 + AC3: discovery-level classification
# ---------------------------------------------------------------------------


class TestFindUnclaimedRawFiles:
    def test_unmatched_extension_entity_tier_flagged(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        _write(raw, "daily-activity/20260410T090000Z.csv", "a,b,c\n")
        found = find_unclaimed_raw_files(raw, tmp_path)
        assert len(found) == 1
        assert found[0].reason == REASON_UNMATCHED_EXTENSION
        assert found[0].group_key == "daily-activity"

    def test_recognised_md_entity_file_not_flagged(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        _write(raw, "sessions/20260810T120000Z-abcdef01.md", "---\n---\nbody\n")
        assert find_unclaimed_raw_files(raw, tmp_path) == []

    def test_recognised_jsonl_entity_file_not_flagged(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        _write(raw, "events/20260810T120000Z-abcdef02.jsonl", '{"a": 1}\n')
        assert find_unclaimed_raw_files(raw, tmp_path) == []

    def test_gitkeep_never_flagged(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        _write(raw, "daily-activity/.gitkeep", "")
        assert find_unclaimed_raw_files(raw, tmp_path) == []

    def test_answers_dir_never_flagged(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        _write(raw, "answers/weird-name.txt", "resolved answer body\n")
        assert find_unclaimed_raw_files(raw, tmp_path) == []

    def test_non_intake_source_excluded(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        _write(raw, "tool-logs/dump.bin", "binary-ish\n")
        config = {"librarian": {"non_intake_sources": ["tool-logs"]}}
        assert find_unclaimed_raw_files(raw, tmp_path, config) == []

    def test_exempt_retained_file_excluded(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        _write(raw, "journal/notes.jsonl", '{"e": 1}\n')
        # `.jsonl` is a recognised extension so it would not be flagged
        # anyway -- exercise a wrong-extension file marked exempt instead,
        # so this test actually proves the exempt check, not the extension
        # check.
        _write(raw, "journal/notes.txt", "plain text log\n")
        mark_exempt(tmp_path, ["journal/notes.txt"])
        found = find_unclaimed_raw_files(raw, tmp_path)
        assert found == []

    def test_correction_batch_jsonl_excluded(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        envelope = {
            "record": "batch",
            "schema_version": 1,
            "submitter": "test",
            "batch_id": "b1",
            "created_at": "2026-08-10T00:00:00Z",
        }
        _write(raw, "contact-sync/20260810T120000Z-abcdef03.jsonl", json.dumps(envelope) + "\n")
        assert find_unclaimed_raw_files(raw, tmp_path) == []

    def test_loose_file_at_raw_root_flagged_unrecognised_shape(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        _write(raw, "README.md", "not intake\n")
        found = find_unclaimed_raw_files(raw, tmp_path)
        assert len(found) == 1
        assert found[0].reason == REASON_UNRECOGNISED_SHAPE
        assert found[0].group_key == "(raw root)"

    def test_deeply_nested_file_flagged_unrecognised_shape(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        _write(raw, "sessions/2026/08/stray.md", "nested\n")
        found = find_unclaimed_raw_files(raw, tmp_path)
        assert len(found) == 1
        assert found[0].reason == REASON_UNRECOGNISED_SHAPE

    def test_auto_memory_missing_naming_convention_flagged(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        _write(raw, "auto-memory/scope-a/lane_foo.md", "---\n---\nbody\n")
        found = find_unclaimed_raw_files(raw, tmp_path)
        assert len(found) == 1
        assert found[0].reason == REASON_MISSING_NAMING_CONVENTION
        assert found[0].group_key == "auto-memory/scope-a"

    def test_auto_memory_recognised_file_not_flagged(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        _write(raw, "auto-memory/scope-a/feedback_something.md", "---\n---\nbody\n")
        assert find_unclaimed_raw_files(raw, tmp_path) == []

    def test_auto_memory_entity_schema_fallthrough_stays_silent(self, tmp_path: Path) -> None:
        """A legitimate fall-through -- must NOT raise noise (design constraint)."""
        raw = tmp_path / "raw"
        _write(raw, "auto-memory/scope-a/20260810T120000Z-abcdef04.md", "---\n---\nbody\n")
        assert find_unclaimed_raw_files(raw, tmp_path) == []

    def test_auto_memory_wrong_extension_flagged(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        _write(raw, "auto-memory/scope-a/feedback_dump.txt", "plain text\n")
        found = find_unclaimed_raw_files(raw, tmp_path)
        assert len(found) == 1
        assert found[0].reason == REASON_UNMATCHED_EXTENSION
        assert found[0].group_key == "auto-memory/scope-a"

    def test_memory_md_index_file_never_flagged(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        _write(raw, "auto-memory/scope-a/MEMORY.md", "index\n")
        assert find_unclaimed_raw_files(raw, tmp_path) == []


# ---------------------------------------------------------------------------
# AC2: sibling files of one reason arrive as ONE raised item
# ---------------------------------------------------------------------------


class TestRaiseGroupsNotFiles:
    def test_many_sibling_files_raise_exactly_one_item(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        for i in range(12):
            _write(raw, f"daily-activity/f{i:02d}.bak", "{}\n")
        unclaimed = find_unclaimed_raw_files(raw, tmp_path)
        assert len(unclaimed) == 12
        summary = raise_unclaimed_files(
            _pending(tmp_path), unclaimed, raw_root=raw, archive_path=_archive(tmp_path)
        )
        assert summary["groups"] == 1
        assert summary["raised_groups"] == 1
        assert summary["raised_files"] == 12

        items = list_unanswered(_pending(tmp_path))
        assert len(items) == 1
        item = items[0]
        assert "12" in item["question"]
        assert "unmatched extension" in item["question"]
        assert "daily-activity" in item["description"] or "daily-activity" in item["question"]

    def test_sample_paths_bounded_with_remainder_note(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        for i in range(20):
            _write(raw, f"daily-activity/f{i:02d}.bak", "{}\n")
        unclaimed = find_unclaimed_raw_files(raw, tmp_path)
        raise_unclaimed_files(
            _pending(tmp_path), unclaimed, raw_root=raw, archive_path=_archive(tmp_path)
        )
        item = list_unanswered(_pending(tmp_path))[0]
        # Bounded sample: not all 20 paths are embedded verbatim.
        path_lines = [ln for ln in item["description"].splitlines() if ln.strip().startswith("- ")]
        assert len(path_lines) <= 6  # <=5 sample paths + 1 "+N more" line
        assert "+" in item["description"] and "more" in item["description"]

    def test_different_reasons_in_same_dir_are_separate_items(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        _write(raw, "auto-memory/scope-a/lane_foo.md", "---\n---\nbody\n")  # naming
        _write(raw, "auto-memory/scope-a/lane_bar.txt", "plain\n")  # extension
        unclaimed = find_unclaimed_raw_files(raw, tmp_path)
        assert len(unclaimed) == 2
        summary = raise_unclaimed_files(
            _pending(tmp_path), unclaimed, raw_root=raw, archive_path=_archive(tmp_path)
        )
        assert summary["groups"] == 2
        assert summary["raised_groups"] == 2
        assert len(list_unanswered(_pending(tmp_path))) == 2


# ---------------------------------------------------------------------------
# AC4: auto-memory naming-convention drop is raised, not silently skipped
# ---------------------------------------------------------------------------


class TestAutoMemoryNamingConvention:
    def test_misnamed_auto_memory_file_is_raised(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        _write(raw, "auto-memory/scope-a/notes.md", "---\n---\nbody\n")
        summary = run_intake_audit(
            raw_root=raw, wiki_root=tmp_path / "wiki", knowledge_root=tmp_path
        )
        assert summary["raised_groups"] == 1
        items = list_unanswered(_pending(tmp_path))
        assert len(items) == 1
        assert "missing naming convention" in items[0]["question"]

    def test_well_named_auto_memory_file_raises_nothing(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        _write(raw, "auto-memory/scope-a/project_notes.md", "---\n---\nbody\n")
        summary = run_intake_audit(
            raw_root=raw, wiki_root=tmp_path / "wiki", knowledge_root=tmp_path
        )
        assert summary["unclaimed_files"] == 0
        assert not _pending(tmp_path).exists()


# ---------------------------------------------------------------------------
# Idempotence: the single most important correctness property
# ---------------------------------------------------------------------------


class TestIdempotence:
    def test_two_consecutive_runs_raise_the_group_once(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        for i in range(88):
            _write(raw, f"daily-activity/{i:03d}.bak", "{}\n")

        run1 = run_intake_audit(
            raw_root=raw, wiki_root=tmp_path / "wiki", knowledge_root=tmp_path
        )
        assert run1["raised_groups"] == 1
        assert run1["already_open_groups"] == 0

        run2 = run_intake_audit(
            raw_root=raw, wiki_root=tmp_path / "wiki", knowledge_root=tmp_path
        )
        assert run2["raised_groups"] == 0
        assert run2["already_open_groups"] == 1

        # Still exactly one open item after two runs -- never re-raised.
        assert len(list_unanswered(_pending(tmp_path))) == 1

    def test_growing_backlog_does_not_produce_a_second_raise(self, tmp_path: Path) -> None:
        """A group's fingerprint is independent of file count (design
        constraint) -- new siblings arriving between runs must not defeat
        dedup."""
        raw = tmp_path / "raw"
        _write(raw, "daily-activity/001.bak", "{}\n")
        run_intake_audit(raw_root=raw, wiki_root=tmp_path / "wiki", knowledge_root=tmp_path)
        assert len(list_unanswered(_pending(tmp_path))) == 1

        # More siblings show up before the next run.
        for i in range(2, 10):
            _write(raw, f"daily-activity/{i:03d}.bak", "{}\n")
        run2 = run_intake_audit(
            raw_root=raw, wiki_root=tmp_path / "wiki", knowledge_root=tmp_path
        )
        assert run2["raised_groups"] == 0
        assert len(list_unanswered(_pending(tmp_path))) == 1


# ---------------------------------------------------------------------------
# AC6: resolving through the EXISTING resolve path clears the flag
# ---------------------------------------------------------------------------


class TestResolutionClearsTheFlag:
    def test_resolve_by_id_then_rerun_does_not_reraise(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        for i in range(3):
            _write(raw, f"daily-activity/{i}.bak", "{}\n")

        run1 = run_intake_audit(
            raw_root=raw, wiki_root=tmp_path / "wiki", knowledge_root=tmp_path
        )
        assert run1["raised_groups"] == 1
        pending = _pending(tmp_path)
        decision_id = list_unanswered(pending)[0]["id"]

        # Resolve through the EXISTING resolve path -- no hand-edited state,
        # no bespoke unlock mechanism.
        result = resolve_by_id(pending, decision_id, "These are pipeline logs; ignore them.")
        assert result["ok"] is True
        assert list_unanswered(pending) == []

        # The raw files are untouched (out of scope: what to DO with them is
        # a separate decision) -- but the decision must not come back.
        run2 = run_intake_audit(
            raw_root=raw, wiki_root=tmp_path / "wiki", knowledge_root=tmp_path
        )
        assert run2["raised_groups"] == 0
        assert run2["already_open_groups"] == 1
        assert list_unanswered(pending) == []

        # The file itself is provably still on disk and still unrecognised
        # -- AC6 is about the DECISION not resurfacing, not about the file
        # vanishing.
        assert (raw / "daily-activity" / "0.bak").exists()

    def test_resolve_survives_archival_via_ingest_answers(self, tmp_path: Path) -> None:
        """The stronger form of AC6: after a REAL ``ingest-answers`` tick
        moves the resolved block out of the primary file and into the
        archive, a later run still must not re-raise."""
        from athenaeum.answers import ingest_answers

        raw = tmp_path / "raw"
        wiki = tmp_path / "wiki"
        _write(raw, "daily-activity/only.bak", "{}\n")

        run_intake_audit(raw_root=raw, wiki_root=wiki, knowledge_root=tmp_path)
        pending = _pending(tmp_path)
        decision_id = list_unanswered(pending)[0]["id"]
        resolve_by_id(pending, decision_id, "Ignore -- pipeline logs.")

        ingested = ingest_answers(pending, raw)
        assert ingested == 1
        assert list_unanswered(pending) == []
        assert _archive(tmp_path).exists()

        run2 = run_intake_audit(raw_root=raw, wiki_root=wiki, knowledge_root=tmp_path)
        assert run2["raised_groups"] == 0
        assert run2["already_open_groups"] == 1
        assert list_unanswered(pending) == []


# ---------------------------------------------------------------------------
# AC7: an unrecognised file raises exactly one item; a recognised file
# raises none.
# ---------------------------------------------------------------------------


class TestExactlyOneVsZero:
    def test_unrecognised_file_raises_exactly_one(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        _write(raw, "daily-activity/only.bak", "{}\n")
        summary = run_intake_audit(
            raw_root=raw, wiki_root=tmp_path / "wiki", knowledge_root=tmp_path
        )
        assert summary["raised_groups"] == 1
        assert len(list_unanswered(_pending(tmp_path))) == 1

    def test_recognised_file_raises_none(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        _write(raw, "sessions/20260810T120000Z-abcdef05.md", "---\n---\nbody\n")
        _write(raw, "auto-memory/scope-a/user_pref.md", "---\n---\nbody\n")
        summary = run_intake_audit(
            raw_root=raw, wiki_root=tmp_path / "wiki", knowledge_root=tmp_path
        )
        assert summary["raised_groups"] == 0
        assert summary["unclaimed_files"] == 0
        assert not _pending(tmp_path).exists()


# ---------------------------------------------------------------------------
# AC5: summary counters are internally consistent (this module's own
# denominator-invariant-style bookkeeping).
# ---------------------------------------------------------------------------


class TestSummaryCounters:
    def test_first_run_raised_files_equals_unclaimed_files(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        for i in range(5):
            _write(raw, f"daily-activity/{i}.bak", "{}\n")
        _write(raw, "auto-memory/scope-a/lane_foo.md", "---\n---\nbody\n")

        summary = run_intake_audit(
            raw_root=raw, wiki_root=tmp_path / "wiki", knowledge_root=tmp_path
        )
        assert summary["unclaimed_files"] == 6
        assert summary["groups"] == 2
        # Nothing was already open -- every unclaimed file's group got
        # raised this run, so raised_files must account for all of them.
        assert summary["raised_files"] == 6
        assert summary["already_open_groups"] == 0

    def test_dry_run_writes_nothing_but_still_counts(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        _write(raw, "daily-activity/only.bak", "{}\n")
        summary = run_intake_audit(
            raw_root=raw, wiki_root=tmp_path / "wiki", knowledge_root=tmp_path, dry_run=True
        )
        assert summary["unclaimed_files"] == 1
        assert summary["groups"] == 1
        assert summary["raised_groups"] == 0
        assert not _pending(tmp_path).exists()

    def test_denominator_invariant_holds_across_a_mixed_second_run(
        self, tmp_path: Path, caplog
    ) -> None:
        """AC5: the counter participates in an athenaeum#903-style denominator
        invariant -- every file `raise_unclaimed_files` was given is
        accounted for by exactly one of "raised this call" or "already
        open" (an already-answered/archived group also counts as
        accounted-for, never double-counted, never dropped). No violation
        is ever logged for well-formed input."""
        raw = tmp_path / "raw"
        for i in range(3):
            _write(raw, f"daily-activity/{i}.bak", "{}\n")
        run1 = run_intake_audit(
            raw_root=raw, wiki_root=tmp_path / "wiki", knowledge_root=tmp_path
        )
        assert run1["raised_files"] == run1["unclaimed_files"]

        # A second, DIFFERENT reason/group appears alongside the now-open
        # first group.
        _write(raw, "auto-memory/scope-a/lane_foo.md", "---\n---\nbody\n")
        with caplog.at_level("ERROR"):
            run2 = run_intake_audit(
                raw_root=raw, wiki_root=tmp_path / "wiki", knowledge_root=tmp_path
            )
        assert run2["unclaimed_files"] == 4  # 3 daily-activity + 1 auto-memory
        assert run2["raised_groups"] == 1  # only the new group
        assert run2["raised_files"] == 1
        assert run2["already_open_groups"] == 1  # the daily-activity group
        assert not any("denominator invariant violated" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# UnclaimedFile is a plain, hashable/comparable dataclass -- sanity check
# used by the grouping logic above.
# ---------------------------------------------------------------------------


def test_unclaimed_file_is_a_frozen_dataclass(tmp_path: Path) -> None:
    a = UnclaimedFile(path=tmp_path / "x", reason="unmatched extension", group_key="g")
    b = UnclaimedFile(path=tmp_path / "x", reason="unmatched extension", group_key="g")
    assert a == b
    assert hash(a) == hash(b)


# ---------------------------------------------------------------------------
# Sanity: parse_pending_questions still parses our raised block cleanly
# (fingerprint recovered, description free of the fingerprint line).
# ---------------------------------------------------------------------------


def test_raised_block_parses_with_clean_description_and_fingerprint(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    _write(raw, "daily-activity/only.bak", "{}\n")
    run_intake_audit(raw_root=raw, wiki_root=tmp_path / "wiki", knowledge_root=tmp_path)
    pq = parse_pending_questions(_pending(tmp_path))[0]
    assert pq.fingerprint.startswith("unclaimed-")
    assert "**Fingerprint**" not in pq.description
    assert pq.raised_by == "agent"
