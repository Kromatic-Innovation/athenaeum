# SPDX-License-Identifier: Apache-2.0
"""Tests for the `drop` / `retain` / `rollup` dispositions, compiled-exempt
retirement, the denominator invariant (issue athenaeum#903), and the
per-record disposition ledger (issue athenaeum#975).

Organized to map onto each issue's acceptance criteria — each test class
below is annotated with the AC it proves. The engine half (`emit`,
`fallthrough`, matching, transform, observe mode) is athenaeum#901 and stays in
``tests/test_rules.py``; this file covers what athenaeum#903 and athenaeum#975 add.
"""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError as PydanticValidationError

from athenaeum.compiled_exempt import (
    COMPILED_EXEMPT_FILENAME,
    load_exempt,
    mark_exempt,
)
from athenaeum.corrections import find_correction_batches
from athenaeum.intake import discover_raw_files
from athenaeum.intake_audit import discover_unclaimed_shape_rule_candidates
from athenaeum.rules import (
    TERMINAL_DISPOSITIONS,
    ShapeRule,
    default_shape_rule_dispositions_path,
    record_key_fingerprint,
    run_shape_rule_phase,
)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True
    )


def _git_init(root: Path) -> None:
    _git(root, "init", "-b", "develop")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Shape Rule Test")
    (root / ".gitkeep").write_text("", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")


def _write_rule(rules_dir: Path, filename: str, rule: dict) -> Path:
    rules_dir.mkdir(parents=True, exist_ok=True)
    path = rules_dir / filename
    path.write_text(yaml.safe_dump(rule), encoding="utf-8")
    return path


def _write_raw_jsonl(raw_root: Path, source: str, name: str, record: dict) -> Path:
    d = raw_root / source
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


def _drop_rule(**overrides) -> dict:
    d = {
        "version": 1,
        "name": "skip-noop",
        "mode": "live",
        "match": {"source": "contact-sync", "format": "jsonl"},
        "disposition": "drop",
    }
    d.update(overrides)
    return d


def _retain_rule(**overrides) -> dict:
    d = {
        "version": 1,
        "name": "daily-journal",
        "mode": "live",
        "match": {"source": "journal", "format": "jsonl"},
        "disposition": "retain",
    }
    d.update(overrides)
    return d


def _rollup_rule(**overrides) -> dict:
    d = {
        "version": 1,
        "name": "interaction-count",
        "mode": "live",
        "match": {"source": "events", "format": "jsonl"},
        "disposition": "rollup",
        "rollup": {"group_by": "$person_uid", "aggregate": "count"},
        "correction": {
            "target": {"uid": "$person_uid"},
            "op": "set",
            "field": "interaction_count",
            "value": 0,
            "source": "script:event-stream",
        },
    }
    d.update(overrides)
    return d


_BASE_CORRECTION = {
    "target": {"type": "person", "handle": {"email": "$email"}},
    "op": "set",
    "field": "bounced",
    "value": "$status_date",
    "source": "script:test-rule",
    "observed_at": "$observed_at",
}


def _emit_rule(**overrides) -> dict:
    d = {
        "version": 1,
        "name": "contact-bounce",
        "mode": "live",
        "match": {"source": "delivery-monitor", "format": "jsonl"},
        "disposition": "emit",
        "correction": dict(_BASE_CORRECTION),
    }
    d.update(overrides)
    return d


def _fallthrough_rule(**overrides) -> dict:
    d = {
        "version": 1,
        "name": "unrecognized-export",
        "mode": "live",
        "match": {"source": "misc-export", "format": "jsonl"},
        "disposition": "fallthrough",
    }
    d.update(overrides)
    return d


def _record(**overrides) -> dict:
    d = {
        "status": "bounced",
        "email": "alex@example.org",
        "status_date": "2026-08-06",
        "observed_at": "2026-08-06T14:01:55Z",
    }
    d.update(overrides)
    return d


def _run(tmp_path: Path, **kwargs):
    return run_shape_rule_phase(
        raw_root=tmp_path / "raw",
        wiki_root=tmp_path / "wiki",
        knowledge_root=tmp_path,
        config=None,
        **kwargs,
    )


def _ledger_lines(tmp_path: Path) -> list[dict]:
    from athenaeum.rules import default_shape_rules_ledger_path

    path = default_shape_rules_ledger_path(tmp_path / "wiki")
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _disposition_rows(tmp_path: Path) -> list[dict]:
    path = default_shape_rule_dispositions_path(tmp_path / "wiki")
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# Schema: the new dispositions and their required/forbidden blocks.
# ---------------------------------------------------------------------------


class TestDispositionSchema:
    @pytest.mark.parametrize("disposition", ["drop", "retain"])
    def test_bare_dispositions_need_no_correction(self, disposition: str) -> None:
        rule = ShapeRule.model_validate(
            {
                "version": 1,
                "name": "r",
                "match": {"source": "s"},
                "disposition": disposition,
            }
        )
        assert rule.disposition == disposition
        assert rule.correction is None

    @pytest.mark.parametrize("disposition", ["drop", "retain"])
    def test_bare_dispositions_reject_a_correction_block(
        self, disposition: str
    ) -> None:
        with pytest.raises(PydanticValidationError, match="must not carry"):
            ShapeRule.model_validate(
                {
                    "version": 1,
                    "name": "r",
                    "match": {"source": "s"},
                    "disposition": disposition,
                    "correction": {
                        "target": {"uid": "u"},
                        "op": "set",
                        "field": "f",
                        "value": 1,
                        "source": "script:x",
                    },
                }
            )

    def test_rollup_requires_a_rollup_block(self) -> None:
        bad = _rollup_rule()
        del bad["rollup"]
        with pytest.raises(PydanticValidationError, match="requires a 'rollup' block"):
            ShapeRule.model_validate(bad)

    def test_rollup_requires_a_correction_block(self) -> None:
        bad = _rollup_rule()
        del bad["correction"]
        with pytest.raises(PydanticValidationError, match="requires a 'correction'"):
            ShapeRule.model_validate(bad)

    def test_non_rollup_rejects_a_rollup_block(self) -> None:
        bad = _drop_rule(rollup={"group_by": "$x", "aggregate": "count"})
        with pytest.raises(PydanticValidationError, match="must not carry a 'rollup'"):
            ShapeRule.model_validate(bad)

    def test_last_requires_of(self) -> None:
        bad = _rollup_rule(rollup={"group_by": "$person_uid", "aggregate": "last"})
        with pytest.raises(PydanticValidationError, match="requires 'of'"):
            ShapeRule.model_validate(bad)

    def test_count_forbids_of(self) -> None:
        bad = _rollup_rule(
            rollup={"group_by": "$person_uid", "aggregate": "count", "of": "$ts"}
        )
        with pytest.raises(PydanticValidationError, match="must not carry an 'of'"):
            ShapeRule.model_validate(bad)

    def test_terminal_disposition_vocabulary_stays_in_sync(self) -> None:
        # Same discipline athenaeum#901 applies to KNOWN_FUNCTIONS/_FUNCTIONS: the
        # documented vocabulary and the schema's own Literal are asserted in
        # sync, so adding a disposition to one without the other fails here
        # rather than drifting silently.
        from typing import get_args

        literal = set(get_args(ShapeRule.model_fields["disposition"].annotation))
        assert literal == set(TERMINAL_DISPOSITIONS)

    def test_unknown_disposition_is_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            ShapeRule.model_validate(
                {
                    "version": 1,
                    "name": "r",
                    "match": {"source": "s"},
                    "disposition": "incinerate",
                }
            )


# ---------------------------------------------------------------------------
# AC1: a `drop` increments an audit counter, retires the raw file through the
# existing retirement convention, and leaves the content recoverable from
# history.
# ---------------------------------------------------------------------------


class TestDropDisposition:
    def test_drop_retires_the_file_and_counts_it(self, tmp_path: Path) -> None:
        # issue athenaeum#978 (S3): retirement now refuses against a store that
        # is not versioned rather than falling back to a silent unlink, so
        # this needs a real git repo to observe the retired raw file.
        _git_init(tmp_path)
        _write_rule(tmp_path / "rules", "r1.yaml", _drop_rule())
        raw_path = _write_raw_jsonl(
            tmp_path / "raw",
            "contact-sync",
            "20260806T140211Z-9f3ac1d2.jsonl",
            {"kind": "skip_no_change"},
        )
        summary = _run(tmp_path)

        assert summary["dispositions"] == {"drop": 1}
        assert not raw_path.exists()
        # And it writes NO correction -- a drop is a discard, not a compile.
        assert find_correction_batches(tmp_path / "raw") == []

    def test_dropped_content_is_recoverable_from_git_history(
        self, tmp_path: Path
    ) -> None:
        _git_init(tmp_path)
        _write_rule(tmp_path / "rules", "r1.yaml", _drop_rule())
        payload = {"kind": "skip_no_change", "marker": "recover-me"}
        raw_path = _write_raw_jsonl(
            tmp_path / "raw", "contact-sync", "20260806T140211Z-9f3ac1d2.jsonl", payload
        )
        rel = str(raw_path.relative_to(tmp_path))

        _run(tmp_path)

        assert not raw_path.exists()
        # The provenance snapshot commit is what makes this true: the blob is
        # still in history even though the file is gone from the worktree.
        log = _git(tmp_path, "log", "--all", "--diff-filter=D", "--name-only")
        assert rel in log.stdout
        recovered = _git(tmp_path, "show", f"HEAD~1:{rel}")
        assert "recover-me" in recovered.stdout

    def test_drop_commit_message_names_the_rule(self, tmp_path: Path) -> None:
        _git_init(tmp_path)
        _write_rule(tmp_path / "rules", "r1.yaml", _drop_rule())
        _write_raw_jsonl(
            tmp_path / "raw",
            "contact-sync",
            "20260806T140211Z-9f3ac1d2.jsonl",
            {"kind": "skip_no_change"},
        )
        _run(tmp_path)
        log = _git(tmp_path, "log", "--oneline")
        assert "dropped as information-free by skip-noop@1" in log.stdout

    def test_observe_mode_drop_writes_nothing(self, tmp_path: Path) -> None:
        _write_rule(tmp_path / "rules", "r1.yaml", _drop_rule(mode="observe"))
        raw_path = _write_raw_jsonl(
            tmp_path / "raw",
            "contact-sync",
            "20260806T140211Z-9f3ac1d2.jsonl",
            {"kind": "skip_no_change"},
        )
        summary = _run(tmp_path)
        assert summary["dispositions"] == {"observed-drop": 1}
        assert raw_path.exists()  # observe mode never removes anything


# ---------------------------------------------------------------------------
# AC2 + AC3: a `retain` marks the file compiled-exempt in the per-file
# manifest and does NOT delete it; a compiled-exempt file is skipped by
# discovery on every subsequent run.
# ---------------------------------------------------------------------------


class TestRetainDisposition:
    def test_retain_marks_exempt_without_deleting(self, tmp_path: Path) -> None:
        _write_rule(tmp_path / "rules", "r1.yaml", _retain_rule())
        raw_path = _write_raw_jsonl(
            tmp_path / "raw", "journal", "20260806T140211Z-9f3ac1d2.jsonl", {"e": 1}
        )
        summary = _run(tmp_path)

        assert summary["dispositions"] == {"retain": 1}
        assert raw_path.exists()  # AC2: NOT deleted
        assert load_exempt(tmp_path) == {"journal/20260806T140211Z-9f3ac1d2.jsonl"}
        assert (tmp_path / COMPILED_EXEMPT_FILENAME).is_file()

    def test_compiled_exempt_file_is_skipped_by_discovery(
        self, tmp_path: Path
    ) -> None:
        _write_rule(tmp_path / "rules", "r1.yaml", _retain_rule())
        _write_raw_jsonl(
            tmp_path / "raw", "journal", "20260806T140211Z-9f3ac1d2.jsonl", {"e": 1}
        )
        # Visible to discovery BEFORE the retain...
        assert len(discover_raw_files(tmp_path / "raw")) == 1
        _run(tmp_path)
        # ...and invisible on every subsequent run (AC3).
        assert discover_raw_files(tmp_path / "raw") == []

    def test_exemption_survives_across_runs(self, tmp_path: Path) -> None:
        _write_rule(tmp_path / "rules", "r1.yaml", _retain_rule())
        raw_path = _write_raw_jsonl(
            tmp_path / "raw", "journal", "20260806T140211Z-9f3ac1d2.jsonl", {"e": 1}
        )
        first = _run(tmp_path)
        assert first["dispositions"] == {"retain": 1}

        # A second run does not even SEE the file, so the rule cannot match it
        # again -- the exemption is durable, not a per-run flag.
        second = _run(tmp_path)
        assert second["files_evaluated"] == 0
        assert second["dispositions"] == {}
        assert raw_path.exists()

    def test_observe_mode_retain_marks_nothing(self, tmp_path: Path) -> None:
        _write_rule(tmp_path / "rules", "r1.yaml", _retain_rule(mode="observe"))
        _write_raw_jsonl(
            tmp_path / "raw", "journal", "20260806T140211Z-9f3ac1d2.jsonl", {"e": 1}
        )
        summary = _run(tmp_path)
        assert summary["dispositions"] == {"observed-retain": 1}
        assert load_exempt(tmp_path) == set()
        assert len(discover_raw_files(tmp_path / "raw")) == 1


def _write_unclaimed_txt(raw_root: Path, source: str, name: str, content: str = "plain\n") -> Path:
    d = raw_root / source
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    path.write_text(content, encoding="utf-8")
    return path


class TestUnclaimedDrop:
    """issue athenaeum#1133 AC2: `drop` works on an audit-unclaimed file (a
    `.txt` export `discover_raw_files` would never claim)."""

    def test_drop_retires_an_unclaimed_txt_file(self, tmp_path: Path) -> None:
        _git_init(tmp_path)
        _write_rule(
            tmp_path / "rules",
            "r1.yaml",
            {
                "version": 1,
                "name": "drop-stray-txt",
                "mode": "live",
                "match": {"unclaimed": True, "source": "daily-activity"},
                "disposition": "drop",
            },
        )
        raw_path = _write_unclaimed_txt(tmp_path / "raw", "daily-activity", "dump.txt")
        candidates = discover_unclaimed_shape_rule_candidates(tmp_path / "raw", tmp_path)

        summary = _run(tmp_path, unclaimed_candidates=candidates)

        assert summary["dispositions"] == {"drop": 1}
        assert not raw_path.exists()

    def test_ordinary_drop_rule_never_matches_an_unclaimed_candidate(
        self, tmp_path: Path
    ) -> None:
        _git_init(tmp_path)
        _write_rule(tmp_path / "rules", "r1.yaml", _drop_rule(match={"source": "daily-activity"}))
        raw_path = _write_unclaimed_txt(tmp_path / "raw", "daily-activity", "dump.txt")
        candidates = discover_unclaimed_shape_rule_candidates(tmp_path / "raw", tmp_path)

        summary = _run(tmp_path, unclaimed_candidates=candidates)

        assert summary["files_matched"] == 0
        assert raw_path.exists()


class TestUnclaimedRetain:
    """issue athenaeum#1133 AC2: `retain` works on an audit-unclaimed file."""

    def test_retain_marks_an_unclaimed_txt_file_exempt(self, tmp_path: Path) -> None:
        _write_rule(
            tmp_path / "rules",
            "r1.yaml",
            {
                "version": 1,
                "name": "retain-stray-txt",
                "mode": "live",
                "match": {"unclaimed": True, "source": "daily-activity"},
                "disposition": "retain",
            },
        )
        raw_path = _write_unclaimed_txt(tmp_path / "raw", "daily-activity", "dump.txt")
        candidates = discover_unclaimed_shape_rule_candidates(tmp_path / "raw", tmp_path)

        summary = _run(tmp_path, unclaimed_candidates=candidates)

        assert summary["dispositions"] == {"retain": 1}
        assert raw_path.exists()
        assert load_exempt(tmp_path) == {"daily-activity/dump.txt"}

    def test_observe_mode_writes_nothing(self, tmp_path: Path) -> None:
        _write_rule(
            tmp_path / "rules",
            "r1.yaml",
            {
                "version": 1,
                "name": "retain-stray-txt",
                "mode": "observe",
                "match": {"unclaimed": True, "source": "daily-activity"},
                "disposition": "retain",
            },
        )
        raw_path = _write_unclaimed_txt(tmp_path / "raw", "daily-activity", "dump.txt")
        candidates = discover_unclaimed_shape_rule_candidates(tmp_path / "raw", tmp_path)

        summary = _run(tmp_path, unclaimed_candidates=candidates)

        assert summary["dispositions"] == {"observed-retain": 1}
        assert raw_path.exists()
        assert load_exempt(tmp_path) == set()


class TestCompiledExemptManifest:
    def test_absent_manifest_is_an_empty_set(self, tmp_path: Path) -> None:
        assert load_exempt(tmp_path) == set()

    def test_malformed_manifest_fails_open(self, tmp_path: Path) -> None:
        (tmp_path / COMPILED_EXEMPT_FILENAME).write_text("{not json", encoding="utf-8")
        assert load_exempt(tmp_path) == set()

    def test_mark_is_idempotent_and_merges(self, tmp_path: Path) -> None:
        assert mark_exempt(tmp_path, ["a/1.md"]) == {"a/1.md"}
        assert mark_exempt(tmp_path, ["a/1.md"]) == {"a/1.md"}
        assert mark_exempt(tmp_path, ["b/2.md"]) == {"a/1.md", "b/2.md"}
        assert load_exempt(tmp_path) == {"a/1.md", "b/2.md"}

    def test_manifest_lives_in_the_knowledge_repo_not_a_cache(
        self, tmp_path: Path
    ) -> None:
        # The durability property the `retain` disposition rests on: a cache
        # wipe must never resurrect a preserved source document into the wiki.
        mark_exempt(tmp_path, ["a/1.md"])
        assert (tmp_path / COMPILED_EXEMPT_FILENAME).is_file()


# ---------------------------------------------------------------------------
# AC4: a `rollup` aggregates N matching records into ONE correction record in
# the conformance format.
# ---------------------------------------------------------------------------


class TestRollupDisposition:
    def _three_events(self, tmp_path: Path, uid: str = "person-1") -> None:
        for i, ts in enumerate(
            ["2026-08-01T00:00:00Z", "2026-08-03T00:00:00Z", "2026-08-02T00:00:00Z"]
        ):
            _write_raw_jsonl(
                tmp_path / "raw",
                "events",
                f"20260806T1402{i:02d}Z-9f3ac1d{i}.jsonl",
                {"person_uid": uid, "ts": ts},
            )

    def test_n_records_become_one_correction(self, tmp_path: Path) -> None:
        _write_rule(tmp_path / "rules", "r1.yaml", _rollup_rule())
        self._three_events(tmp_path)
        summary = _run(tmp_path)

        assert summary["dispositions"] == {"rollup": 3}
        assert summary["rollups_written"] == 1

        batches = find_correction_batches(tmp_path / "raw")
        assert len(batches) == 1
        path, _source, envelope = batches[0]
        assert envelope["record"] == "batch"
        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()[1:]
        ]
        assert len(records) == 1  # ONE correction, not three
        assert records[0]["field"] == "interaction_count"
        assert records[0]["value"] == 3  # the windowed count
        assert records[0]["target"] == {"uid": "person-1"}
        assert "rollup of 3 record(s)" in records[0]["note"]

    def test_all_members_are_retired(self, tmp_path: Path) -> None:
        # issue athenaeum#978 (S3): retirement now refuses against a store
        # that is not versioned rather than falling back to a silent
        # unlink, so this needs a real git repo to observe the rolled-up
        # raw files retired.
        _git_init(tmp_path)
        _write_rule(tmp_path / "rules", "r1.yaml", _rollup_rule())
        self._three_events(tmp_path)
        _run(tmp_path)
        # Every rolled-up raw file is compiled away; only the batch remains,
        # and a batch is claimed by the correction phase, not ordinary intake.
        assert discover_raw_files(tmp_path / "raw") == []

    def test_distinct_groups_produce_distinct_corrections(
        self, tmp_path: Path
    ) -> None:
        _write_rule(tmp_path / "rules", "r1.yaml", _rollup_rule())
        for i, uid in enumerate(["person-1", "person-2", "person-1"]):
            _write_raw_jsonl(
                tmp_path / "raw",
                "events",
                f"20260806T1402{i:02d}Z-9f3ac1d{i}.jsonl",
                {"person_uid": uid, "ts": "2026-08-01T00:00:00Z"},
            )
        summary = _run(tmp_path)
        assert summary["rollups_written"] == 2

        values = {}
        for path, _s, _e in find_correction_batches(tmp_path / "raw"):
            for line in path.read_text(encoding="utf-8").splitlines()[1:]:
                rec = json.loads(line)
                values[rec["target"]["uid"]] = rec["value"]
        assert values == {"person-1": 2, "person-2": 1}

    def test_last_aggregate_takes_the_maximum(self, tmp_path: Path) -> None:
        _write_rule(
            tmp_path / "rules",
            "r1.yaml",
            _rollup_rule(
                rollup={
                    "group_by": "$person_uid",
                    "aggregate": "last",
                    "of": "$ts",
                },
                correction={
                    "target": {"uid": "$person_uid"},
                    "op": "set",
                    "field": "last_event",
                    "value": 0,
                    "source": "script:event-stream",
                },
            ),
        )
        self._three_events(tmp_path)
        _run(tmp_path)

        path, _s, _e = find_correction_batches(tmp_path / "raw")[0]
        record = json.loads(path.read_text(encoding="utf-8").splitlines()[1])
        assert record["field"] == "last_event"
        assert record["value"] == "2026-08-03T00:00:00Z"  # the LAST event date

    def test_observe_mode_rollup_writes_nothing(self, tmp_path: Path) -> None:
        _write_rule(tmp_path / "rules", "r1.yaml", _rollup_rule(mode="observe"))
        self._three_events(tmp_path)
        summary = _run(tmp_path)

        assert summary["dispositions"] == {"observed-rollup": 3}
        assert summary["rollups_written"] == 0
        assert find_correction_batches(tmp_path / "raw") == []
        assert len(discover_raw_files(tmp_path / "raw")) == 3

    def test_unresolvable_group_by_degrades_to_transform_error(
        self, tmp_path: Path
    ) -> None:
        _write_rule(
            tmp_path / "rules",
            "r1.yaml",
            _rollup_rule(rollup={"group_by": "$missing", "aggregate": "count"}),
        )
        self._three_events(tmp_path)
        summary = _run(tmp_path)
        # Not silently dropped, not written -- tallied under its own name and
        # the raw files are left for the reasoning tiers.
        assert summary["dispositions"] == {"transform-error": 3}
        assert find_correction_batches(tmp_path / "raw") == []
        assert len(discover_raw_files(tmp_path / "raw")) == 3


# ---------------------------------------------------------------------------
# AC5 + AC6: every intake file reaches exactly ONE terminal disposition per
# run, written to the audit ledger; ledger lines carry denominators, and
# dispositions sum to records seen.
# ---------------------------------------------------------------------------


class TestDenominatorInvariant:
    def test_dispositions_sum_to_records_seen(self, tmp_path: Path) -> None:
        _write_rule(tmp_path / "rules", "drop.yaml", _drop_rule())
        for i in range(4):
            _write_raw_jsonl(
                tmp_path / "raw",
                "contact-sync",
                f"20260806T1402{i:02d}Z-9f3ac1d{i}.jsonl",
                {"kind": "skip_no_change"},
            )
        summary = _run(tmp_path)

        lines = _ledger_lines(tmp_path)
        assert len(lines) == 1
        line = lines[0]
        assert line["rule"] == "skip-noop@1"
        assert line["records_seen"] == 4
        assert sum(line["dispositions"].values()) == line["records_seen"]
        assert summary["files_matched"] == 4

    def test_invariant_holds_across_mixed_dispositions(self, tmp_path: Path) -> None:
        _write_rule(tmp_path / "rules", "a-drop.yaml", _drop_rule())
        _write_rule(tmp_path / "rules", "b-retain.yaml", _retain_rule())
        _write_rule(tmp_path / "rules", "c-rollup.yaml", _rollup_rule())

        _write_raw_jsonl(
            tmp_path / "raw",
            "contact-sync",
            "20260806T140200Z-9f3ac1d0.jsonl",
            {"kind": "skip_no_change"},
        )
        _write_raw_jsonl(
            tmp_path / "raw", "journal", "20260806T140201Z-9f3ac1d1.jsonl", {"e": 1}
        )
        for i in (2, 3):
            _write_raw_jsonl(
                tmp_path / "raw",
                "events",
                f"20260806T1402{i:02d}Z-9f3ac1d{i}.jsonl",
                {"person_uid": "p1", "ts": "2026-08-01T00:00:00Z"},
            )

        summary = _run(tmp_path)
        assert summary["dispositions"] == {"drop": 1, "retain": 1, "rollup": 2}

        lines = _ledger_lines(tmp_path)
        assert len(lines) == 3
        for line in lines:
            assert sum(line["dispositions"].values()) == line["records_seen"], line
        # Every matched file reached exactly one terminal disposition.
        assert sum(line["records_seen"] for line in lines) == summary["files_matched"]

    def test_every_matched_file_reaches_exactly_one_disposition(
        self, tmp_path: Path
    ) -> None:
        _write_rule(tmp_path / "rules", "r1.yaml", _drop_rule())
        for i in range(3):
            _write_raw_jsonl(
                tmp_path / "raw",
                "contact-sync",
                f"20260806T1402{i:02d}Z-9f3ac1d{i}.jsonl",
                {"kind": "skip_no_change"},
            )
        summary = _run(tmp_path)
        total_dispositions = sum(summary["dispositions"].values())
        assert total_dispositions == summary["files_matched"] == 3

    def test_ledger_line_still_carries_the_rule_at_version_tag(
        self, tmp_path: Path
    ) -> None:
        _write_rule(tmp_path / "rules", "r1.yaml", _retain_rule())
        _write_raw_jsonl(
            tmp_path / "raw", "journal", "20260806T140211Z-9f3ac1d2.jsonl", {"e": 1}
        )
        _run(tmp_path)
        assert _ledger_lines(tmp_path)[0]["rule"] == "daily-journal@1"


# ---------------------------------------------------------------------------
# athenaeum#975 AC1: every intake record processed by the shape-rules pass
# appends one per-record disposition row to `_shape_rule_dispositions.jsonl`
# -- source_ref, key_fingerprint, tier, rule_id (or none), disposition, at.
# ---------------------------------------------------------------------------


class TestPerRecordDispositionRows:
    _ROW_FIELDS = {
        "schema_version",
        "at",
        "source",
        "source_ref",
        "key_fingerprint",
        "tier",
        "rule_id",
        "disposition",
    }

    def test_emit_appends_a_tier_0_row_with_the_exact_field_set(
        self, tmp_path: Path
    ) -> None:
        _write_rule(tmp_path / "rules", "r1.yaml", _emit_rule())
        _write_raw_jsonl(
            tmp_path / "raw",
            "delivery-monitor",
            "20260806T140211Z-9f3ac1d2.jsonl",
            _record(),
        )
        _run(tmp_path)

        rows = _disposition_rows(tmp_path)
        assert len(rows) == 1
        row = rows[0]
        assert set(row.keys()) == self._ROW_FIELDS
        assert row["disposition"] == "emit"
        assert row["tier"] == 0
        assert row["rule_id"] == "contact-bounce@1"
        assert row["source"] == "delivery-monitor"
        assert row["source_ref"] == "delivery-monitor/20260806T140211Z-9f3ac1d2.jsonl"
        assert row["key_fingerprint"] == record_key_fingerprint(_record())
        assert row["schema_version"] == 1

    def test_observed_emit_appends_a_tier_0_row(self, tmp_path: Path) -> None:
        _write_rule(tmp_path / "rules", "r1.yaml", _emit_rule(mode="observe"))
        _write_raw_jsonl(
            tmp_path / "raw",
            "delivery-monitor",
            "20260806T140211Z-9f3ac1d2.jsonl",
            _record(),
        )
        _run(tmp_path)

        rows = _disposition_rows(tmp_path)
        assert len(rows) == 1
        assert rows[0]["disposition"] == "observed-emit"
        assert rows[0]["tier"] == 0
        assert rows[0]["rule_id"] == "contact-bounce@1"

    def test_fallthrough_appends_a_deferred_row(self, tmp_path: Path) -> None:
        _write_rule(tmp_path / "rules", "r1.yaml", _fallthrough_rule())
        _write_raw_jsonl(
            tmp_path / "raw",
            "misc-export",
            "20260806T140211Z-9f3ac1d2.jsonl",
            {"a": 1},
        )
        _run(tmp_path)

        rows = _disposition_rows(tmp_path)
        assert len(rows) == 1
        assert rows[0]["disposition"] == "fallthrough"
        assert rows[0]["tier"] is None
        assert rows[0]["rule_id"] == "unrecognized-export@1"

    def test_drop_appends_a_tier_0_row(self, tmp_path: Path) -> None:
        _git_init(tmp_path)
        _write_rule(tmp_path / "rules", "r1.yaml", _drop_rule())
        _write_raw_jsonl(
            tmp_path / "raw",
            "contact-sync",
            "20260806T140211Z-9f3ac1d2.jsonl",
            {"kind": "skip_no_change"},
        )
        _run(tmp_path)

        rows = _disposition_rows(tmp_path)
        assert len(rows) == 1
        assert rows[0]["disposition"] == "drop"
        assert rows[0]["tier"] == 0
        assert rows[0]["rule_id"] == "skip-noop@1"

    def test_retain_appends_a_tier_0_row(self, tmp_path: Path) -> None:
        _write_rule(tmp_path / "rules", "r1.yaml", _retain_rule())
        _write_raw_jsonl(
            tmp_path / "raw", "journal", "20260806T140211Z-9f3ac1d2.jsonl", {"e": 1}
        )
        _run(tmp_path)

        rows = _disposition_rows(tmp_path)
        assert len(rows) == 1
        assert rows[0]["disposition"] == "retain"
        assert rows[0]["tier"] == 0

    def test_rollup_appends_one_tier_0_row_per_member(self, tmp_path: Path) -> None:
        _write_rule(tmp_path / "rules", "r1.yaml", _rollup_rule())
        for i in (0, 1):
            _write_raw_jsonl(
                tmp_path / "raw",
                "events",
                f"20260806T1402{i:02d}Z-9f3ac1d{i}.jsonl",
                {"person_uid": "p1", "ts": "2026-08-01T00:00:00Z"},
            )
        _run(tmp_path)

        rows = _disposition_rows(tmp_path)
        assert len(rows) == 2
        assert all(row["disposition"] == "rollup" for row in rows)
        assert all(row["tier"] == 0 for row in rows)

    def test_no_match_appends_a_deferred_row_with_no_rule(
        self, tmp_path: Path
    ) -> None:
        # A rule is loaded (so the phase actually runs), but it matches a
        # different source -- this record matches no rule at all.
        _write_rule(tmp_path / "rules", "r1.yaml", _emit_rule())
        _write_raw_jsonl(
            tmp_path / "raw",
            "unrelated-source",
            "20260806T140211Z-9f3ac1d2.jsonl",
            {"whatever": "shape"},
        )
        _run(tmp_path)

        rows = _disposition_rows(tmp_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["disposition"] == "no-match"
        assert row["rule_id"] is None
        assert row["tier"] is None
        assert row["source"] == "unrelated-source"
        assert set(row.keys()) == self._ROW_FIELDS

    def test_dry_run_writes_no_rows(self, tmp_path: Path) -> None:
        _write_rule(tmp_path / "rules", "r1.yaml", _emit_rule())
        _write_raw_jsonl(
            tmp_path / "raw",
            "delivery-monitor",
            "20260806T140211Z-9f3ac1d2.jsonl",
            _record(),
        )
        _run(tmp_path, dry_run=True)

        assert _disposition_rows(tmp_path) == []
        # The aggregate ledger is dry-run-suppressed too -- unaffected by
        # this change (AC3's "additive" guarantee, just verified for the
        # dry-run path specifically).
        assert _ledger_lines(tmp_path) == []


# ---------------------------------------------------------------------------
# athenaeum#975 AC2: fingerprints, never raw values -- no raw sensitive value
# or name-derived string appears anywhere in a disposition row.
# ---------------------------------------------------------------------------


class TestNoRawValuesInDispositionRows:
    def test_sentinel_value_never_appears_in_the_ledger_bytes(
        self, tmp_path: Path
    ) -> None:
        _write_rule(tmp_path / "rules", "r1.yaml", _emit_rule())
        sentinel = "personal@example.com"
        record = _record(email=sentinel)
        _write_raw_jsonl(
            tmp_path / "raw",
            "delivery-monitor",
            "20260806T140211Z-9f3ac1d2.jsonl",
            record,
        )
        _run(tmp_path)

        path = default_shape_rule_dispositions_path(tmp_path / "wiki")
        raw_bytes = path.read_bytes()
        assert sentinel.encode("utf-8") not in raw_bytes

        expected_fingerprint = record_key_fingerprint(record)
        rows = _disposition_rows(tmp_path)
        assert len(rows) == 1
        assert rows[0]["key_fingerprint"] == expected_fingerprint
        assert expected_fingerprint.encode("utf-8") in raw_bytes


# ---------------------------------------------------------------------------
# athenaeum#975 AC3: the existing per-rule aggregate keeps its current shape
# and consumers -- an additive change only.
# ---------------------------------------------------------------------------


class TestAggregateShapeUnchanged:
    def test_aggregate_field_set_is_unchanged(self, tmp_path: Path) -> None:
        _write_rule(tmp_path / "rules", "r1.yaml", _emit_rule())
        _write_raw_jsonl(
            tmp_path / "raw",
            "delivery-monitor",
            "20260806T140211Z-9f3ac1d2.jsonl",
            _record(),
        )
        _run(tmp_path)

        lines = _ledger_lines(tmp_path)
        assert len(lines) == 1
        assert set(lines[0].keys()) == {
            "schema_version",
            "run_at",
            "rule",
            "mode",
            "records_seen",
            "records_total",
            "dispositions",
        }
        assert lines[0]["dispositions"] == {"emit": 1}


# ---------------------------------------------------------------------------
# athenaeum#975 AC4: forward-only -- running the phase twice appends new
# disposition rows and never rewrites/truncates earlier ones.
# ---------------------------------------------------------------------------


class TestForwardOnlyAppend:
    def test_running_twice_appends_without_touching_earlier_rows(
        self, tmp_path: Path
    ) -> None:
        # A real git repo so `emit`'s retirement actually removes the first
        # raw file -- otherwise it would still be present (unretired) on the
        # second run and get re-disposed, defeating the point of this test.
        _git_init(tmp_path)
        _write_rule(tmp_path / "rules", "r1.yaml", _emit_rule())
        _write_raw_jsonl(
            tmp_path / "raw",
            "delivery-monitor",
            "20260806T140211Z-9f3ac1d2.jsonl",
            _record(),
        )
        _run(tmp_path)
        first_rows = _disposition_rows(tmp_path)
        assert len(first_rows) == 1

        _write_raw_jsonl(
            tmp_path / "raw",
            "delivery-monitor",
            "20260807T140211Z-9f3ac1d3.jsonl",
            _record(),
        )
        _run(tmp_path)
        second_rows = _disposition_rows(tmp_path)

        assert len(second_rows) == 2
        assert second_rows[0] == first_rows[0]


# ---------------------------------------------------------------------------
# athenaeum#975 AC5: athenaeum#905's shape-frequency-by-key-fingerprint-and-source
# detector question is answerable from the rows alone. This is the point of
# the issue -- written as a small, readable reduction athenaeum#905's lane can lift
# directly.
# ---------------------------------------------------------------------------


class TestShapeFrequencyQuery:
    def test_unmatched_shape_frequency_by_source_and_fingerprint(
        self, tmp_path: Path
    ) -> None:
        # An unrelated rule keeps `rules` non-empty so the phase actually
        # runs, but it matches nothing in "orphan-export" -- every record
        # from that source falls through as "no-match" (tier: null).
        _write_rule(tmp_path / "rules", "r1.yaml", _emit_rule())

        widget_shape = {"widget_id": "placeholder", "status": "new"}
        for i in range(3):
            _write_raw_jsonl(
                tmp_path / "raw",
                "orphan-export",
                f"20260806T1402{i:02d}Z-9f3ac1d{i}.jsonl",
                {"widget_id": f"w{i}", "status": "new"},
            )
        _write_raw_jsonl(
            tmp_path / "raw",
            "orphan-export",
            "20260806T140299Z-9f3ac1d9.jsonl",
            {"totally_different_shape": True},
        )
        _run(tmp_path)

        rows = _disposition_rows(tmp_path)

        # athenaeum#905's worked query: shape frequency by (source, key_fingerprint),
        # restricted to rows the deterministic (tier 0) layer did NOT
        # handle -- exactly the shapes the reasoning ladder still has to
        # cover, which is what makes them interesting to a detector looking
        # for shapes the deterministic layer keeps failing on.
        shape_frequency: Counter[tuple[str, str]] = Counter(
            (row["source"], row["key_fingerprint"])
            for row in rows
            if row["tier"] is None
        )

        widget_key = ("orphan-export", record_key_fingerprint(widget_shape))
        other_key = (
            "orphan-export",
            record_key_fingerprint({"totally_different_shape": True}),
        )
        assert shape_frequency[widget_key] == 3
        assert shape_frequency[other_key] == 1
        assert sum(shape_frequency.values()) == 4
