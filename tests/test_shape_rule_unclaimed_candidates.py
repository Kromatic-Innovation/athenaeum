# SPDX-License-Identifier: Apache-2.0
"""Tests for audit-unclaimed files reaching shape-rule evaluation (issue
athenaeum#1133): the wiring that lets an operator rule dispose of a file the
intake audit (issue athenaeum#836) would otherwise only ever raise a
pending decision about.

Structurally modeled on ``tests/test_shape_rule_extra_intake_descent.py`` --
the direct athenaeum#1096 precedent for "a separate discovery function,
appended to the shape-rule phase's candidate set by the caller". Discovery-
level coverage (``discover_unclaimed_shape_rule_candidates`` itself) and
per-disposition coverage (drop/retain/preserve, `MatchSpec.unclaimed`'s
load-time guards) live in ``tests/test_intake_audit.py`` / ``tests/test_rules.py``
/ ``tests/test_rules_dispositions.py`` / ``tests/test_rules_preserve.py``.
This file covers the properties that only show up when discovery, matching,
and the intake audit run TOGETHER, mirroring how `athenaeum.librarian`
wires them:

- AC3: with no matching rule, behaviour is byte-for-byte identical to
  athenaeum#836's today -- proven directly, not just by counting
  dispositions.
- The partition, exercised through the full phase (not just `MatchSpec`
  unit-level): a non-`unclaimed` rule never matches an unclaimed candidate
  and vice versa, even with an otherwise-empty `match:` block.
- Observe mode writes nothing for an unclaimed candidate.
- A file dispositioned by a rule this run is not re-raised by the
  subsequent intake-audit phase in the SAME run.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from athenaeum.intake import discover_raw_files
from athenaeum.intake_audit import (
    discover_unclaimed_shape_rule_candidates,
    find_unclaimed_raw_files,
    run_intake_audit,
)
from athenaeum.rules import run_shape_rule_phase


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


def _write(root: Path, rel: str, content: str = "hello\n") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _write_rule(rules_dir: Path, filename: str, rule: dict) -> Path:
    rules_dir.mkdir(parents=True, exist_ok=True)
    path = rules_dir / filename
    path.write_text(yaml.safe_dump(rule), encoding="utf-8")
    return path


def _run_shape_rules(tmp_path: Path, **kwargs):
    """Mirrors `librarian._run_shape_rule_phase`'s wiring: resolve
    unclaimed candidates and pass them into `run_shape_rule_phase`."""
    candidates = discover_unclaimed_shape_rule_candidates(
        tmp_path / "raw", tmp_path, kwargs.get("config")
    )
    return run_shape_rule_phase(
        raw_root=tmp_path / "raw",
        wiki_root=tmp_path / "wiki",
        knowledge_root=tmp_path,
        config=kwargs.pop("config", None),
        unclaimed_candidates=candidates,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# AC3: default behaviour (no matching rule) is byte-for-byte unchanged
# ---------------------------------------------------------------------------


class TestDefaultBehaviourByteForByte:
    def _seed_backlog(self, tmp_path: Path) -> None:
        _write(tmp_path / "raw", "daily-activity/only.bak", "{}\n")
        _write(tmp_path / "raw", "auto-memory/scope-a/lane_foo.md", "---\n---\nbody\n")
        _write(tmp_path / "raw", "README.md", "not intake\n")

    def test_pending_questions_byte_identical_with_and_without_wiring(
        self, tmp_path_factory
    ) -> None:
        # Run A: today's behaviour -- intake audit only, no shape-rule
        # phase involved at all.
        root_a = tmp_path_factory.mktemp("no-wiring")
        self._seed_backlog(root_a)
        run_intake_audit(
            raw_root=root_a / "raw", wiki_root=root_a / "wiki", knowledge_root=root_a
        )
        pending_a = (root_a / "wiki" / "_pending_questions.md").read_text(encoding="utf-8")

        # Run B: the new plumbing wired in, but with an EMPTY rules/ dir --
        # zero rules loaded, `run_shape_rule_phase` returns before touching
        # any candidate, so the shape-rule phase must be a complete no-op.
        root_b = tmp_path_factory.mktemp("empty-rules-wired")
        self._seed_backlog(root_b)
        (root_b / "rules").mkdir()
        shape_summary = _run_shape_rules(root_b)
        assert shape_summary["files_evaluated"] == 0  # no rules -> early return
        run_intake_audit(
            raw_root=root_b / "raw", wiki_root=root_b / "wiki", knowledge_root=root_b
        )
        pending_b = (root_b / "wiki" / "_pending_questions.md").read_text(encoding="utf-8")

        assert pending_a == pending_b

    def test_find_unclaimed_raw_files_output_byte_identical(
        self, tmp_path_factory
    ) -> None:
        root_a = tmp_path_factory.mktemp("no-wiring")
        self._seed_backlog(root_a)
        before = find_unclaimed_raw_files(root_a / "raw", root_a)

        root_b = tmp_path_factory.mktemp("wired")
        self._seed_backlog(root_b)
        (root_b / "rules").mkdir()
        _run_shape_rules(root_b)
        after = find_unclaimed_raw_files(root_b / "raw", root_b)

        # Same reasons, same group keys, same relative paths -- not moved,
        # not dropped, not exempted by the shape-rule phase running first.
        before_tuples = sorted(
            (u.reason, u.group_key, str(u.path.relative_to(root_a)))
            for u in before
        )
        after_tuples = sorted(
            (u.reason, u.group_key, str(u.path.relative_to(root_b))) for u in after
        )
        assert before_tuples == after_tuples

    def test_no_side_effects_when_a_rule_exists_but_does_not_match(
        self, tmp_path: Path
    ) -> None:
        # A loaded rule that targets a DIFFERENT source must leave an
        # unclaimed candidate exactly where discovery found it.
        self._seed_backlog(tmp_path)
        _write_rule(
            tmp_path / "rules",
            "r1.yaml",
            {
                "version": 1,
                "name": "unrelated",
                "mode": "live",
                "match": {"unclaimed": True, "source": "nonexistent-source"},
                "disposition": "drop",
            },
        )
        before = {u.path for u in find_unclaimed_raw_files(tmp_path / "raw", tmp_path)}
        summary = _run_shape_rules(tmp_path)
        assert summary["files_matched"] == 0
        after = {u.path for u in find_unclaimed_raw_files(tmp_path / "raw", tmp_path)}
        assert before == after


# ---------------------------------------------------------------------------
# The partition, at full-phase level
# ---------------------------------------------------------------------------


class TestPartitionAtPhaseLevel:
    def test_empty_match_block_unclaimed_true_never_matches_ordinary_candidate(
        self, tmp_path: Path
    ) -> None:
        # An ordinary (claimed) candidate alongside an unclaimed one -- the
        # bare `{unclaimed: true}` rule (every OTHER match key optional)
        # must match ONLY the unclaimed candidate.
        _write(tmp_path / "raw", "sessions/20260810T120000Z-abcdef01.jsonl", '{"a": 1}\n')
        _write(tmp_path / "raw", "daily-activity/only.bak", "{}\n")
        _write_rule(
            tmp_path / "rules",
            "r1.yaml",
            {
                "version": 1,
                "name": "catch-all-unclaimed",
                "mode": "live",
                "match": {"unclaimed": True},
                "disposition": "fallthrough",
            },
        )
        summary = _run_shape_rules(tmp_path)
        assert summary["files_evaluated"] == 2
        assert summary["files_matched"] == 1  # only the unclaimed .bak

    def test_empty_match_block_unclaimed_false_never_matches_unclaimed_candidate(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path / "raw", "sessions/20260810T120000Z-abcdef01.jsonl", '{"a": 1}\n')
        _write(tmp_path / "raw", "daily-activity/only.bak", "{}\n")
        _write_rule(
            tmp_path / "rules",
            "r1.yaml",
            {
                "version": 1,
                "name": "catch-all-ordinary",
                "mode": "live",
                "match": {},
                "disposition": "fallthrough",
            },
        )
        summary = _run_shape_rules(tmp_path)
        assert summary["files_evaluated"] == 2
        assert summary["files_matched"] == 1  # only the ordinary .jsonl


# ---------------------------------------------------------------------------
# Observe mode writes nothing for an unclaimed candidate
# ---------------------------------------------------------------------------


class TestObserveModeUnclaimed:
    def test_observe_mode_drop_writes_zero_filesystem_changes(
        self, tmp_path: Path
    ) -> None:
        raw_path = _write(tmp_path / "raw", "daily-activity/only.bak", "{}\n")
        _write_rule(
            tmp_path / "rules",
            "r1.yaml",
            {
                "version": 1,
                "name": "observe-drop-unclaimed",
                "mode": "observe",
                "match": {"unclaimed": True, "source": "daily-activity"},
                "disposition": "drop",
            },
        )
        summary = _run_shape_rules(tmp_path)
        assert summary["dispositions"] == {"observed-drop": 1}
        assert raw_path.exists()


# ---------------------------------------------------------------------------
# A file dispositioned by a rule is not re-raised by the intake-audit phase
# in the same run
# ---------------------------------------------------------------------------


class TestDispositionedFileNotReRaised:
    def test_drop_this_run_means_no_pending_decision_this_run(
        self, tmp_path: Path
    ) -> None:
        _git_init(tmp_path)
        _write(tmp_path / "raw", "daily-activity/only.bak", "{}\n")
        _write_rule(
            tmp_path / "rules",
            "r1.yaml",
            {
                "version": 1,
                "name": "drop-stray",
                "mode": "live",
                "match": {"unclaimed": True, "source": "daily-activity"},
                "disposition": "drop",
            },
        )

        shape_summary = _run_shape_rules(tmp_path)
        assert shape_summary["dispositions"] == {"drop": 1}

        # Mirrors `librarian.run`'s phase order: shape rules, THEN intake
        # audit, in the same run.
        audit_summary = run_intake_audit(
            raw_root=tmp_path / "raw",
            wiki_root=tmp_path / "wiki",
            knowledge_root=tmp_path,
        )
        assert audit_summary["unclaimed_files"] == 0
        assert audit_summary["raised_groups"] == 0
        assert not (tmp_path / "wiki" / "_pending_questions.md").exists()

    def test_without_a_matching_rule_the_file_is_still_raised_as_before(
        self, tmp_path: Path
    ) -> None:
        # Control: same setup, but the rule targets a different source, so
        # it never disposes of the file -- the ordinary pending-decision
        # path must still fire.
        _write(tmp_path / "raw", "daily-activity/only.bak", "{}\n")
        _write_rule(
            tmp_path / "rules",
            "r1.yaml",
            {
                "version": 1,
                "name": "drop-stray",
                "mode": "live",
                "match": {"unclaimed": True, "source": "nonexistent-source"},
                "disposition": "drop",
            },
        )

        _run_shape_rules(tmp_path)
        audit_summary = run_intake_audit(
            raw_root=tmp_path / "raw",
            wiki_root=tmp_path / "wiki",
            knowledge_root=tmp_path,
        )
        assert audit_summary["unclaimed_files"] == 1
        assert audit_summary["raised_groups"] == 1


# ---------------------------------------------------------------------------
# Discovery disjointness (documented, proven by regression test rather than
# set-intersection logic in production code)
# ---------------------------------------------------------------------------


class TestCandidateSetsAreDisjoint:
    def test_no_file_appears_in_both_ordinary_and_unclaimed_candidate_sets(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path / "raw", "sessions/20260810T120000Z-abcdef01.md", "---\n---\nbody\n")
        _write(tmp_path / "raw", "daily-activity/only.bak", "{}\n")
        _write(tmp_path / "raw", "auto-memory/scope-a/lane_foo.md", "---\n---\nbody\n")

        # `config=None` on both calls so each resolves the SAME default
        # `recall.extra_intake_roots` (["raw/auto-memory"]) -- an explicit
        # `{}` on one side only would desync which source dirs count as
        # extra-intake roots between the two calls, which is a test-config
        # bug, not a production one (both calls thread the SAME `ctx.config`
        # in `librarian.py`).
        ordinary = {r.path for r in discover_raw_files(tmp_path / "raw", None)}
        unclaimed = {
            r.path
            for r in discover_unclaimed_shape_rule_candidates(tmp_path / "raw", tmp_path)
        }
        assert ordinary & unclaimed == set()
        assert ordinary == {tmp_path / "raw" / "sessions" / "20260810T120000Z-abcdef01.md"}
        assert unclaimed == {
            tmp_path / "raw" / "daily-activity" / "only.bak",
            tmp_path / "raw" / "auto-memory" / "scope-a" / "lane_foo.md",
        }
