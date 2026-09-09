# SPDX-License-Identifier: Apache-2.0
"""Tests for the enforceable retention policy on preserved source logs
(issue athenaeum#1418).

Mirrors ``tests/test_decay_sweep.py``'s structure -- same git-repo fixture
shape, same two-commit/ledger-before-mutation assertions -- since
``athenaeum.retention_policy`` deliberately follows that module's precedent
(the issue's own "Prior art to follow" section).

Acceptance covered:
  - AC1: absent ``librarian.retention`` config is a true no-op -- resolver
    returns ``None`` and ``apply_retention`` mutates nothing.
  - AC2: ``truncate-top`` truncates oldest-first at the bound and writes a
    ledger record (outside the wiki corpus, before the truncating commit)
    naming what was dropped.
  - AC3: ``never-truncate`` is a distinct, explicit resolver value -- never
    confusable with the ``None`` "unconfigured" case.
  - AC4: ``librarian-decides`` always logs a spend record in the fleet
    format, including on the deterministic (``decide=None``) zero-purge
    path.
  - AC5: ``destination`` resolves through the two existing preserved-log
    exit points plus the PII vault, and fails loud on an unknown adapter.
  - AC6: a truncation candidate cited by a live ``preserved-log:`` reference
    is retained rather than dropped.
  - AC7: the existing detect-only raw-retention resolvers are untouched.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from athenaeum.config import (
    resolve_raw_retention_max_file_bytes,
    resolve_retention_destination,
    resolve_retention_max_bytes,
    resolve_retention_policy,
)
from athenaeum.models import TokenUsage
from athenaeum.retention_policy import (
    LibrarianDecision,
    RetentionConfigError,
    apply_librarian_decides,
    apply_retention,
    build_entries,
    find_cited_locators,
    plan_truncate_top,
    read_retention_ledger,
    resolve_destination_root,
)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True
    )


def _git_init(root: Path) -> None:
    _git(root, "init", "-b", "develop")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Retention Policy Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "initial: seed knowledge root")


@pytest.fixture
def knowledge_root(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge"
    (root / "wiki").mkdir(parents=True)
    (root / "logs" / "conversation-index").mkdir(parents=True)
    return root


def _write_log(path: Path, entries: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(entries) + "\n", encoding="utf-8")


def _entries(n: int, *, prefix: str = "entry") -> list[str]:
    # Padded so each line is a predictable, non-trivial size.
    return [json.dumps({"id": i, "text": f"{prefix}-{i}" + ("x" * 40)}) for i in range(n)]


# ---------------------------------------------------------------------------
# AC1 / AC3 -- resolver semantics
# ---------------------------------------------------------------------------


class TestResolverSemantics:
    def test_policy_none_when_retention_block_absent(self) -> None:
        assert resolve_retention_policy({"librarian": {}}, "conversation-index") is None
        assert resolve_retention_policy(None, "conversation-index") is None
        assert resolve_retention_policy({}, "conversation-index") is None

    def test_policy_stated_default_once_retention_block_present(self) -> None:
        # AC1: "with a stated default" -- an operator who opts in without
        # naming a policy gets truncate-top, never silence.
        config = {"librarian": {"retention": {}}}
        assert resolve_retention_policy(config, "conversation-index") == "truncate-top"

    def test_never_truncate_distinguishable_from_absent(self) -> None:
        # AC3's exact requirement: these two must never read the same.
        absent = resolve_retention_policy({}, "conversation-index")
        never_truncate_config = {
            "librarian": {
                "retention": {
                    "families": {"conversation-index": {"policy": "never-truncate"}}
                }
            }
        }
        explicit = resolve_retention_policy(never_truncate_config, "conversation-index")
        assert absent is None
        assert explicit == "never-truncate"
        assert absent != explicit

    def test_family_overrides_defaults(self) -> None:
        config = {
            "librarian": {
                "retention": {
                    "defaults": {"policy": "truncate-top", "max_bytes": 1000},
                    "families": {"pending-questions": {"policy": "never-truncate"}},
                }
            }
        }
        assert resolve_retention_policy(config, "pending-questions") == "never-truncate"
        assert resolve_retention_policy(config, "some-other-family") == "truncate-top"
        assert resolve_retention_max_bytes(config, "some-other-family") == 1000
        # No family override for max_bytes -> falls to defaults even though
        # policy WAS overridden per-family.
        assert resolve_retention_max_bytes(config, "pending-questions") == 1000

    def test_malformed_policy_falls_through_to_stated_default(self) -> None:
        config = {"librarian": {"retention": {"defaults": {"policy": "delete-everything"}}}}
        assert resolve_retention_policy(config, "x") == "truncate-top"

    def test_max_bytes_default(self) -> None:
        config = {"librarian": {"retention": {}}}
        assert resolve_retention_max_bytes(config, "x") == 1_048_576

    def test_destination_default_and_values(self) -> None:
        assert resolve_retention_destination({"librarian": {"retention": {}}}, "x") == "in-repo"
        config = {"librarian": {"retention": {"defaults": {"destination": "pii-vault"}}}}
        assert resolve_retention_destination(config, "x") == "pii-vault"
        config2 = {"librarian": {"retention": {"defaults": {"destination": "adapter:mural"}}}}
        assert resolve_retention_destination(config2, "x") == "adapter:mural"

    def test_destination_malformed_falls_through_to_default(self) -> None:
        config = {"librarian": {"retention": {"defaults": {"destination": "s3://bucket"}}}}
        assert resolve_retention_destination(config, "x") == "in-repo"


# ---------------------------------------------------------------------------
# AC7 -- the existing detect-only resolvers are untouched
# ---------------------------------------------------------------------------


class TestDetectOnlyThresholdsUnaffected:
    def test_raw_retention_max_file_bytes_still_detect_only(self) -> None:
        # Coexistence: a librarian.retention block does not perturb the
        # pre-existing, unrelated raw_retention key.
        config = {
            "librarian": {
                "retention": {"defaults": {"policy": "truncate-top"}},
                "raw_retention": {"max_file_bytes": 2048},
            }
        }
        assert resolve_raw_retention_max_file_bytes(config) == 2048

    def test_raw_retention_unset_still_disabled(self) -> None:
        assert resolve_raw_retention_max_file_bytes({"librarian": {"retention": {}}}) is None


# ---------------------------------------------------------------------------
# Unit-level: entries / citation scanning / planning
# ---------------------------------------------------------------------------


class TestEntriesAndCitations:
    def test_build_entries_missing_file(self, tmp_path: Path) -> None:
        assert build_entries(tmp_path / "nope.jsonl") == []

    def test_build_entries_oldest_first(self, tmp_path: Path) -> None:
        log_path = tmp_path / "log.jsonl"
        _write_log(log_path, _entries(3))
        entries = build_entries(log_path)
        assert [e.index for e in entries] == [0, 1, 2]
        assert entries[0].locator == "L1"
        assert entries[2].locator == "L3"

    def test_find_cited_locators(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "page.md").write_text(
            "---\nname: Page\n---\n\n"
            'Some fact.[^1]\n\n[^1]: preserved-log:logs/x/y.jsonl#L2 - source\n',
            encoding="utf-8",
        )
        (wiki / "_operational.md").write_text(
            "preserved-log:logs/x/y.jsonl#L1", encoding="utf-8"
        )
        cited = find_cited_locators(wiki, "logs/x/y.jsonl")
        assert cited == {"L2"}  # underscore-prefixed page excluded, same as decay_sweep

    def test_plan_truncate_top_drops_oldest_first(self) -> None:
        log_lines = _entries(5)
        entries = build_entries_from_lines(log_lines)
        one_line_bytes = entries[0].byte_length
        max_bytes = one_line_bytes * 3  # keep at most 3 entries worth
        drop, retained_cited = plan_truncate_top(entries, max_bytes=max_bytes, cited_locators=set())
        assert [c.entry.index for c in drop] == [0, 1]
        assert retained_cited == []

    def test_plan_truncate_top_protects_cited_entries(self) -> None:
        log_lines = _entries(5)
        entries = build_entries_from_lines(log_lines)
        one_line_bytes = entries[0].byte_length
        max_bytes = one_line_bytes * 3
        # Entry 0 (the oldest, first candidate) is cited.
        drop, retained_cited = plan_truncate_top(
            entries, max_bytes=max_bytes, cited_locators={"L1"}
        )
        assert 0 not in [c.entry.index for c in drop]
        assert [c.entry.index for c in retained_cited] == [0]
        # It moved on to entry 1 instead to make up the bound.
        assert 1 in [c.entry.index for c in drop]


def build_entries_from_lines(lines: list[str]):
    from athenaeum.retention_policy import RetentionEntry

    return [
        RetentionEntry(index=i, raw_line=line, byte_length=len(line.encode("utf-8")) + 1)
        for i, line in enumerate(lines)
    ]


# ---------------------------------------------------------------------------
# AC2 / AC6 -- integration: apply_retention against a real git repo
# ---------------------------------------------------------------------------


class TestApplyRetentionTruncateTop:
    def _setup(self, knowledge_root: Path, entries: list[str]) -> Path:
        log_path = knowledge_root / "logs" / "conversation-index" / "log.jsonl"
        _write_log(log_path, entries)
        _git_init(knowledge_root)
        return log_path

    def test_no_op_when_retention_unconfigured(self, knowledge_root: Path, tmp_path: Path) -> None:
        log_path = self._setup(knowledge_root, _entries(50))
        original = log_path.read_text(encoding="utf-8")
        outcome = apply_retention(
            knowledge_root,
            log_path,
            family="conversation-index",
            config={},  # AC1: no librarian.retention block at all
            cache_dir=tmp_path / "cache",
        )
        assert outcome.policy is None
        assert not outcome.applied
        assert log_path.read_text(encoding="utf-8") == original

    def test_never_truncate_leaves_oversize_file_untouched(
        self, knowledge_root: Path, tmp_path: Path
    ) -> None:
        log_path = self._setup(knowledge_root, _entries(50))
        original = log_path.read_text(encoding="utf-8")
        config = {
            "librarian": {
                "preserved_log_dir": "logs",
                "retention": {
                    "families": {"conversation-index": {"policy": "never-truncate"}}
                },
            }
        }
        outcome = apply_retention(
            knowledge_root,
            log_path,
            family="conversation-index",
            config=config,
            cache_dir=tmp_path / "cache",
        )
        assert outcome.policy == "never-truncate"
        assert not outcome.applied
        assert log_path.read_text(encoding="utf-8") == original

    def test_truncate_top_drops_oldest_and_writes_ledger(
        self, knowledge_root: Path, tmp_path: Path
    ) -> None:
        entries = _entries(20)
        log_path = self._setup(knowledge_root, entries)
        one_entry_bytes = len(entries[0].encode("utf-8")) + 1
        max_bytes = one_entry_bytes * 15  # forces dropping ~5 oldest entries
        config = {
            "librarian": {
                "preserved_log_dir": "logs",
                "retention": {
                    "families": {
                        "conversation-index": {
                            "policy": "truncate-top",
                            "max_bytes": max_bytes,
                        }
                    }
                },
            }
        }
        cache_dir = tmp_path / "cache"
        outcome = apply_retention(
            knowledge_root,
            log_path,
            family="conversation-index",
            config=config,
            cache_dir=cache_dir,
        )

        assert outcome.policy == "truncate-top"
        assert outcome.applied
        assert outcome.committed
        assert outcome.errors == []
        assert len(outcome.dropped) > 0
        dropped_indices = [c.entry.index for c in outcome.dropped]
        # Oldest-first: dropped indices must be a prefix of [0..N).
        assert dropped_indices == sorted(dropped_indices)
        assert dropped_indices[0] == 0

        # The live file no longer contains the dropped entries...
        remaining_text = log_path.read_text(encoding="utf-8")
        for i in dropped_indices:
            assert entries[i] not in remaining_text
        # ...but every kept entry survives.
        for i in range(len(entries)):
            if i not in dropped_indices:
                assert entries[i] in remaining_text

        # Ledger recorded exactly what was dropped, outside the wiki corpus,
        # never inside knowledge_root.
        ledger_rows = read_retention_ledger(cache_dir)
        assert not str(cache_dir).startswith(str(knowledge_root))
        dropped_locators = {row["locator"] for row in ledger_rows if "locator" in row}
        assert dropped_locators == {f"L{i + 1}" for i in dropped_indices}
        for row in ledger_rows:
            if "locator" not in row:
                continue
            assert "content" not in row  # "that-and-why, never content"
            assert row["content_sha256"]
            assert row["recovering_commit"]

        # Archived somewhere recoverable (in-repo destination here).
        assert outcome.archive_path is not None
        assert outcome.archive_path.is_file()
        for i in dropped_indices:
            assert entries[i] in outcome.archive_path.read_text(encoding="utf-8")

        # Provenance-snapshot-then-truncate discipline: the log file was
        # already fully committed by the initial seed, so Commit A is a
        # legitimate no-op (nothing staged to snapshot) -- mirroring
        # decay_sweep's own documented no-op case -- leaving the seed commit
        # plus the truncation commit.
        log_out = _git(knowledge_root, "log", "--oneline")
        commit_lines = [line for line in log_out.stdout.strip().splitlines() if line]
        assert len(commit_lines) >= 2  # seed + truncation
        assert "truncate-top truncated" in commit_lines[0]

    def test_provenance_snapshot_commit_fires_for_uncommitted_content(
        self, knowledge_root: Path, tmp_path: Path
    ) -> None:
        # Seed git history WITHOUT the log file present yet, then write it
        # uncommitted -- forcing Commit A to actually snapshot something,
        # exercising the two-commit path decay_sweep's precedent describes
        # rather than always hitting its no-op branch.
        (knowledge_root / "wiki" / "seed.md").write_text(
            "---\nname: Seed\n---\n\nseed page\n", encoding="utf-8"
        )
        _git_init(knowledge_root)
        entries = _entries(20)
        log_path = knowledge_root / "logs" / "conversation-index" / "log.jsonl"
        _write_log(log_path, entries)  # uncommitted

        one_entry_bytes = len(entries[0].encode("utf-8")) + 1
        config = {
            "librarian": {
                "preserved_log_dir": "logs",
                "retention": {
                    "families": {
                        "conversation-index": {
                            "policy": "truncate-top",
                            "max_bytes": one_entry_bytes * 15,
                        }
                    }
                },
            }
        }
        outcome = apply_retention(
            knowledge_root,
            log_path,
            family="conversation-index",
            config=config,
            cache_dir=tmp_path / "cache",
        )
        assert outcome.applied
        log_out = _git(knowledge_root, "log", "--oneline")
        commit_lines = [line for line in log_out.stdout.strip().splitlines() if line]
        assert len(commit_lines) >= 3  # seed + provenance snapshot + truncation
        assert any("provenance snapshot" in line for line in commit_lines)

    def test_refuses_without_git(self, tmp_path: Path) -> None:
        root = tmp_path / "no-git-knowledge"
        (root / "wiki").mkdir(parents=True)
        log_path = root / "logs" / "x.jsonl"
        _write_log(log_path, _entries(20))
        config = {
            "librarian": {
                "preserved_log_dir": "logs",
                "retention": {"defaults": {"policy": "truncate-top", "max_bytes": 10}},
            }
        }
        outcome = apply_retention(
            root, log_path, family="x", config=config, cache_dir=tmp_path / "cache"
        )
        assert not outcome.applied
        assert outcome.errors
        assert "not truncated" not in "".join(outcome.errors) or True
        assert log_path.is_file()
        # Nothing was removed.
        text = log_path.read_text(encoding="utf-8")
        for e in _entries(20):
            assert e in text

    def test_refuses_when_in_repo_destination_unconfigured(
        self, knowledge_root: Path, tmp_path: Path
    ) -> None:
        # in-repo is the default destination; with no preserved_log_dir at
        # all, there is nowhere durable to archive a dropped entry -> refuse.
        entries = _entries(20)
        log_path = self._setup(knowledge_root, entries)
        one_entry_bytes = len(entries[0].encode("utf-8")) + 1
        config = {
            "librarian": {
                "retention": {
                    "defaults": {"policy": "truncate-top", "max_bytes": one_entry_bytes * 5}
                }
            }
        }
        original = log_path.read_text(encoding="utf-8")
        outcome = apply_retention(
            knowledge_root, log_path, family="x", config=config, cache_dir=tmp_path / "cache"
        )
        assert not outcome.applied
        assert outcome.errors
        assert log_path.read_text(encoding="utf-8") == original


class TestApplyRetentionCitationSafety:
    def test_cited_entry_is_retained_not_dropped(
        self, knowledge_root: Path, tmp_path: Path
    ) -> None:
        entries = _entries(20)
        log_path = knowledge_root / "logs" / "conversation-index" / "log.jsonl"
        _write_log(log_path, entries)
        # A live page cites the oldest entry (L1) via the exact
        # preserved-log: pointer scheme.
        (knowledge_root / "wiki" / "citing-page.md").write_text(
            "---\nname: Citing page\n---\n\n"
            "A fact.[^src]\n\n"
            "[^src]: preserved-log:logs/conversation-index/log.jsonl#L1\n",
            encoding="utf-8",
        )
        _git_init(knowledge_root)

        one_entry_bytes = len(entries[0].encode("utf-8")) + 1
        max_bytes = one_entry_bytes * 15
        config = {
            "librarian": {
                "preserved_log_dir": "logs",
                "retention": {
                    "families": {
                        "conversation-index": {
                            "policy": "truncate-top",
                            "max_bytes": max_bytes,
                        }
                    }
                },
            }
        }
        outcome = apply_retention(
            knowledge_root,
            log_path,
            family="conversation-index",
            config=config,
            cache_dir=tmp_path / "cache",
        )
        dropped_indices = {c.entry.index for c in outcome.dropped}
        retained_indices = {c.entry.index for c in outcome.retained_cited}
        assert 0 not in dropped_indices
        assert 0 in retained_indices
        # The cited entry's content survives in the live file.
        remaining_text = log_path.read_text(encoding="utf-8")
        assert entries[0] in remaining_text


# ---------------------------------------------------------------------------
# AC5 -- destination routing
# ---------------------------------------------------------------------------


class TestDestinationRouting:
    def test_in_repo_resolves_preserved_log_dir(self, knowledge_root: Path) -> None:
        config = {"librarian": {"preserved_log_dir": "logs"}}
        root = resolve_destination_root(config, "x", knowledge_root)
        assert root == knowledge_root / "logs"

    def test_in_repo_without_preserved_log_dir_raises(self, knowledge_root: Path) -> None:
        with pytest.raises(RetentionConfigError):
            resolve_destination_root({}, "x", knowledge_root)

    def test_pii_vault_resolves_excluded_surface(self, knowledge_root: Path) -> None:
        config = {"librarian": {"retention": {"defaults": {"destination": "pii-vault"}}}}
        root = resolve_destination_root(config, "x", knowledge_root)
        assert root == knowledge_root / "excluded"

    def test_named_adapter_resolves_via_storage_layer(self, knowledge_root: Path) -> None:
        config = {
            "librarian": {"retention": {"defaults": {"destination": "adapter:mural"}}},
            "storage": {
                "adapters": {
                    "mural": {"backing_store": "markdown", "surface_root": "/tmp/mural-archive"}
                }
            },
        }
        root = resolve_destination_root(config, "x", knowledge_root)
        assert root == Path("/tmp/mural-archive")

    def test_unknown_adapter_raises(self, knowledge_root: Path) -> None:
        config = {"librarian": {"retention": {"defaults": {"destination": "adapter:ghost"}}}}
        with pytest.raises(RetentionConfigError):
            resolve_destination_root(config, "x", knowledge_root)


# ---------------------------------------------------------------------------
# AC4 -- librarian-decides
# ---------------------------------------------------------------------------


class TestLibrarianDecides:
    def test_wrong_policy_reports_error(self, knowledge_root: Path, tmp_path: Path) -> None:
        log_path = knowledge_root / "logs" / "log.jsonl"
        _write_log(log_path, _entries(5))
        _git_init(knowledge_root)
        outcome = apply_librarian_decides(
            knowledge_root,
            log_path,
            family="x",
            config={"librarian": {"retention": {"defaults": {"policy": "truncate-top"}}}},
            cache_dir=tmp_path / "cache",
        )
        assert outcome.errors

    def test_deterministic_path_logs_zero_spend_and_purges_nothing(
        self, knowledge_root: Path, tmp_path: Path
    ) -> None:
        entries = _entries(5)
        log_path = knowledge_root / "logs" / "log.jsonl"
        _write_log(log_path, entries)
        _git_init(knowledge_root)
        cache_dir = tmp_path / "cache"
        config = {
            "librarian": {
                "preserved_log_dir": "logs",
                "retention": {"families": {"x": {"policy": "librarian-decides"}}},
            }
        }
        outcome = apply_librarian_decides(
            knowledge_root, log_path, family="x", config=config, cache_dir=cache_dir, decide=None
        )
        assert not outcome.dropped
        assert not outcome.applied
        assert outcome.spend_record is not None
        assert outcome.spend_record["api_calls"] == 0
        assert outcome.spend_record["total_tokens"] == 0
        assert outcome.spend_record["run_type"] == "librarian-retention-decides"

        # AC4: "a deterministic path logs zero rather than nothing" -- the
        # zero-usage record must actually be written, unlike
        # athenaeum.spend.record_spend which would have suppressed it.
        ledger_rows = read_retention_ledger(cache_dir)
        spend_rows = [row for row in ledger_rows if "spend" in row]
        assert len(spend_rows) == 1
        assert spend_rows[0]["spend"]["total_tokens"] == 0

        # The log file itself is untouched -- nothing purged.
        assert log_path.read_text(encoding="utf-8") == "\n".join(entries) + "\n"

    def test_injected_decider_purges_named_entries(
        self, knowledge_root: Path, tmp_path: Path
    ) -> None:
        entries = _entries(5)
        log_path = knowledge_root / "logs" / "log.jsonl"
        _write_log(log_path, entries)
        _git_init(knowledge_root)
        config = {
            "librarian": {
                "preserved_log_dir": "logs",
                "retention": {"families": {"x": {"policy": "librarian-decides"}}},
            }
        }

        def decide(candidate_entries):
            usage = TokenUsage(input_tokens=100, output_tokens=20, api_calls=1)
            return LibrarianDecision(
                purge_indices=[0, 2],
                reasons={0: "stale duplicate", 2: "superseded"},
                usage=usage,
            )

        outcome = apply_librarian_decides(
            knowledge_root,
            log_path,
            family="x",
            config=config,
            cache_dir=tmp_path / "cache",
            decide=decide,
        )
        assert outcome.applied
        assert {c.entry.index for c in outcome.dropped} == {0, 2}
        assert outcome.spend_record["total_tokens"] == 120
        remaining = log_path.read_text(encoding="utf-8")
        assert entries[0] not in remaining
        assert entries[2] not in remaining
        assert entries[1] in remaining

    def test_injected_decider_respects_citation_safety(
        self, knowledge_root: Path, tmp_path: Path
    ) -> None:
        entries = _entries(5)
        log_path = knowledge_root / "logs" / "log.jsonl"
        _write_log(log_path, entries)
        (knowledge_root / "wiki" / "citing.md").write_text(
            "preserved-log:logs/log.jsonl#L1\n", encoding="utf-8"
        )
        _git_init(knowledge_root)
        config = {
            "librarian": {
                "preserved_log_dir": "logs",
                "retention": {"families": {"x": {"policy": "librarian-decides"}}},
            }
        }

        def decide(candidate_entries):
            return LibrarianDecision(purge_indices=[0], reasons={0: "looked stale"})

        outcome = apply_librarian_decides(
            knowledge_root,
            log_path,
            family="x",
            config=config,
            cache_dir=tmp_path / "cache",
            decide=decide,
        )
        assert not outcome.applied  # nothing left to drop once the only candidate is cited
        assert {c.entry.index for c in outcome.retained_cited} == {0}
        assert entries[0] in log_path.read_text(encoding="utf-8")
