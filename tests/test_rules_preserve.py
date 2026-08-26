# SPDX-License-Identifier: Apache-2.0
"""Tests for the `preserve` disposition — the log-shaped intake family
(issue athenaeum#837).

A preserved log is a SOURCE DOCUMENT: kept whole, moved out of raw intake into
an operator-configured area, never compiled into wiki prose, and cited as the
provenance of any fact the librarian learns from it. That is the operator
decision of 2026-08-14 on athenaeum#837 — *"retain the log as an artifact and
point any facts that we do ingest to that log as the source"* — as opposed to
athenaeum#903's `retain`, which marks a file exempt where it lies.

Each test class is annotated with the acceptance criterion it proves.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError as PydanticValidationError

from athenaeum.compiled_exempt import load_exempt
from athenaeum.config import resolve_preserved_log_adapter, resolve_preserved_log_dir
from athenaeum.corrections import find_correction_batches
from athenaeum.intake import discover_raw_files
from athenaeum.rules import (
    PRESERVED_LOG_SOURCE_SCHEME,
    TERMINAL_DISPOSITIONS,
    PreserveViaStoreOutcome,
    ShapeRule,
    default_shape_rule_dispositions_path,
    preserve_raw_file_via_store,
    preserved_log_source_pointer,
    run_shape_rule_phase,
)
from athenaeum.storage import StorageConfigError
from athenaeum.store import FilesystemStore

PRESERVED = "logs"


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


def _preserve_rule(**overrides) -> dict:
    d = {
        "version": 1,
        "name": "hestia-lane-log",
        "mode": "live",
        "match": {"source": "hestia-lanes", "format": "jsonl"},
        "disposition": "preserve",
    }
    d.update(overrides)
    return d


#: The worked example from the issue: a log line of the shape
#: ``<address>, bounced`` must produce the corresponding bounce fact.
def _bounce_correction() -> dict:
    return {
        "target": {"type": "person", "handle": {"email": "$email"}},
        "op": "set",
        "field": "bounced",
        "value": "$status_date",
        "source": "script:hestia-lane-log",
        "observed_at": "$observed_at",
    }


def _run(tmp_path: Path, *, config: dict | None = None, **kwargs):
    if config is None:
        config = {"librarian": {"preserved_log_dir": PRESERVED}}
    return run_shape_rule_phase(
        raw_root=tmp_path / "raw",
        wiki_root=tmp_path / "wiki",
        knowledge_root=tmp_path,
        config=config,
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


class TestPreservedLogAreaConfig:
    """AC1: the operator can configure a preserved-log area."""

    def test_configured_area_is_resolved(self) -> None:
        assert (
            resolve_preserved_log_dir({"librarian": {"preserved_log_dir": "logs"}})
            == "logs"
        )

    def test_nested_and_trailing_slash_normalize(self) -> None:
        assert (
            resolve_preserved_log_dir(
                {"librarian": {"preserved_log_dir": "archive/logs/"}}
            )
            == "archive/logs"
        )

    def test_unset_is_none_so_the_feature_is_opt_in(self) -> None:
        assert resolve_preserved_log_dir(None) is None
        assert resolve_preserved_log_dir({}) is None
        assert resolve_preserved_log_dir({"librarian": {}}) is None
        assert resolve_preserved_log_dir({"librarian": {"preserved_log_dir": "  "}}) is None

    @pytest.mark.parametrize("bad", ["/etc/passwd", "../outside", "a/../../outside"])
    def test_paths_escaping_the_knowledge_root_are_refused(self, bad: str) -> None:
        # A preserved artifact outside the knowledge git repo is neither
        # versioned nor recoverable -- which defeats preservation entirely.
        assert resolve_preserved_log_dir({"librarian": {"preserved_log_dir": bad}}) is None


class TestPreservedLogAdapterConfig:
    """athenaeum#1132, QA follow-up: direct unit coverage for
    resolve_preserved_log_adapter, mirroring TestPreservedLogAreaConfig
    above -- this key had none until now, and its
    `not isinstance(config, dict)` guard (config.py, the None case below)
    was unreached by the whole suite."""

    def test_configured_adapter_is_resolved(self) -> None:
        assert (
            resolve_preserved_log_adapter(
                {"librarian": {"preserved_log_adapter": "mural-archive"}}
            )
            == "mural-archive"
        )

    def test_surrounding_whitespace_is_stripped(self) -> None:
        assert (
            resolve_preserved_log_adapter(
                {"librarian": {"preserved_log_adapter": "  mural-archive  "}}
            )
            == "mural-archive"
        )

    def test_unset_is_none_so_the_feature_is_opt_in(self) -> None:
        assert resolve_preserved_log_adapter(None) is None
        assert resolve_preserved_log_adapter({}) is None
        assert resolve_preserved_log_adapter({"librarian": {}}) is None
        assert (
            resolve_preserved_log_adapter({"librarian": {"preserved_log_adapter": "  "}})
            is None
        )


class TestPreserveMovesTheFile:
    """AC2: a log-shaped rule MOVES the file into the configured area."""

    def test_file_is_moved_out_of_raw_into_the_preserved_area(
        self, tmp_path: Path
    ) -> None:
        _write_rule(tmp_path / "rules", "r1.yaml", _preserve_rule())
        raw_path = _write_raw_jsonl(
            tmp_path / "raw", "hestia-lanes", "20260815T000000Z-aa.jsonl", {"e": 1}
        )
        summary = _run(tmp_path)

        assert summary["dispositions"] == {"preserve": 1}
        assert not raw_path.exists()  # moved, not left in place
        dest = tmp_path / PRESERVED / "hestia-lanes" / "20260815T000000Z-aa.jsonl"
        assert dest.is_file()
        # Kept WHOLE -- content is byte-identical, not compiled into prose.
        assert json.loads(dest.read_text(encoding="utf-8")) == {"e": 1}

    def test_source_subdir_is_preserved_so_origin_survives(self, tmp_path: Path) -> None:
        _write_rule(tmp_path / "rules", "r1.yaml", _preserve_rule())
        _write_raw_jsonl(
            tmp_path / "raw", "hestia-lanes", "20260815T000000Z-aa.jsonl", {"e": 1}
        )
        _run(tmp_path)
        assert (tmp_path / PRESERVED / "hestia-lanes").is_dir()

    def test_same_named_log_from_a_later_run_is_not_clobbered(
        self, tmp_path: Path
    ) -> None:
        # The contract is that a preserved artifact SURVIVES; a second file of
        # the same name must not overwrite the first.
        _write_rule(tmp_path / "rules", "r1.yaml", _preserve_rule())
        _write_raw_jsonl(tmp_path / "raw", "hestia-lanes", "today.jsonl", {"day": 1})
        _run(tmp_path)
        _write_raw_jsonl(tmp_path / "raw", "hestia-lanes", "today.jsonl", {"day": 2})
        _run(tmp_path)

        area = tmp_path / PRESERVED / "hestia-lanes"
        assert json.loads((area / "today.jsonl").read_text()) == {"day": 1}
        assert json.loads((area / "today-1.jsonl").read_text()) == {"day": 2}

    def test_move_is_committed_when_the_knowledge_root_is_a_git_repo(
        self, tmp_path: Path
    ) -> None:
        _git_init(tmp_path)
        _write_rule(tmp_path / "rules", "r1.yaml", _preserve_rule())
        _write_raw_jsonl(
            tmp_path / "raw", "hestia-lanes", "20260815T000000Z-aa.jsonl", {"e": 1}
        )
        _run(tmp_path)

        dest = tmp_path / PRESERVED / "hestia-lanes" / "20260815T000000Z-aa.jsonl"
        assert dest.is_file()
        tracked = _git(tmp_path, "ls-files", "--", f"{PRESERVED}/").stdout
        assert "20260815T000000Z-aa.jsonl" in tracked
        log = _git(tmp_path, "log", "--oneline").stdout
        assert "preserved as a source document" in log

    def test_unconfigured_area_falls_through_and_leaves_the_file_untouched(
        self, tmp_path: Path
    ) -> None:
        # Opt-in twice over: a rule alone must not move anything.
        _write_rule(tmp_path / "rules", "r1.yaml", _preserve_rule())
        raw_path = _write_raw_jsonl(
            tmp_path / "raw", "hestia-lanes", "20260815T000000Z-aa.jsonl", {"e": 1}
        )
        summary = _run(tmp_path, config={})

        assert summary["dispositions"] == {"preserve-unconfigured": 1}
        assert raw_path.exists()
        assert discover_raw_files(tmp_path / "raw") != []

    def test_observe_mode_moves_nothing(self, tmp_path: Path) -> None:
        _write_rule(tmp_path / "rules", "r1.yaml", _preserve_rule(mode="observe"))
        raw_path = _write_raw_jsonl(
            tmp_path / "raw", "hestia-lanes", "20260815T000000Z-aa.jsonl", {"e": 1}
        )
        summary = _run(tmp_path)

        assert summary["dispositions"] == {"observed-preserve": 1}
        assert raw_path.exists()
        assert not (tmp_path / PRESERVED).exists()


class TestPreserveLedger:
    """AC3: the move is a terminal disposition in the audit ledger and
    participates in the athenaeum#903 denominator invariant."""

    def test_preserve_is_a_terminal_disposition(self) -> None:
        assert "preserve" in TERMINAL_DISPOSITIONS

    def test_move_is_ledgered(self, tmp_path: Path) -> None:
        _write_rule(tmp_path / "rules", "r1.yaml", _preserve_rule())
        _write_raw_jsonl(
            tmp_path / "raw", "hestia-lanes", "20260815T000000Z-aa.jsonl", {"e": 1}
        )
        _run(tmp_path)

        lines = _ledger_lines(tmp_path)
        assert len(lines) == 1
        assert lines[0]["rule"] == "hestia-lane-log@1"
        assert lines[0]["dispositions"] == {"preserve": 1}

    def test_denominator_invariant_holds_across_mixed_outcomes(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write_rule(tmp_path / "rules", "r1.yaml", _preserve_rule())
        for n in range(3):
            _write_raw_jsonl(
                tmp_path / "raw", "hestia-lanes", f"2026081{n}T000000Z-a.jsonl", {"e": n}
            )
        _run(tmp_path)

        line = _ledger_lines(tmp_path)[0]
        assert line["records_seen"] == 3
        assert line["records_total"] == 3
        assert sum(line["dispositions"].values()) == line["records_seen"]
        assert "denominator invariant violated" not in caplog.text

    def test_unconfigured_still_tallies_against_the_denominator(
        self, tmp_path: Path
    ) -> None:
        _write_rule(tmp_path / "rules", "r1.yaml", _preserve_rule())
        _write_raw_jsonl(
            tmp_path / "raw", "hestia-lanes", "20260815T000000Z-aa.jsonl", {"e": 1}
        )
        _run(tmp_path, config={})

        line = _ledger_lines(tmp_path)[0]
        assert sum(line["dispositions"].values()) == line["records_seen"] == 1


class TestPreservedLogIsNotIntake:
    """AC4: a preserved log is never compiled into wiki prose, and discovery
    does not re-consider it on subsequent runs."""

    def test_discovery_does_not_see_the_preserved_file(self, tmp_path: Path) -> None:
        _write_rule(tmp_path / "rules", "r1.yaml", _preserve_rule())
        _write_raw_jsonl(
            tmp_path / "raw", "hestia-lanes", "20260815T000000Z-aa.jsonl", {"e": 1}
        )
        assert len(discover_raw_files(tmp_path / "raw")) == 1
        _run(tmp_path)
        assert discover_raw_files(tmp_path / "raw") == []

    def test_second_run_does_not_re_evaluate_it(self, tmp_path: Path) -> None:
        _write_rule(tmp_path / "rules", "r1.yaml", _preserve_rule())
        _write_raw_jsonl(
            tmp_path / "raw", "hestia-lanes", "20260815T000000Z-aa.jsonl", {"e": 1}
        )
        _run(tmp_path)
        second = _run(tmp_path)
        assert second["files_evaluated"] == 0
        assert second["dispositions"] == {}

    def test_preserve_does_not_use_the_compiled_exempt_manifest(
        self, tmp_path: Path
    ) -> None:
        # The move is the mechanism, NOT an exempt row. Exempting `source/name`
        # would wrongly suppress a future, genuinely-new file that reuses the
        # name -- which a daily log writer does by construction.
        _write_rule(tmp_path / "rules", "r1.yaml", _preserve_rule())
        _write_raw_jsonl(tmp_path / "raw", "hestia-lanes", "today.jsonl", {"day": 1})
        _run(tmp_path)
        assert load_exempt(tmp_path) == set()

        # A NEW file reusing the name is still seen.
        _write_raw_jsonl(tmp_path / "raw", "hestia-lanes", "today.jsonl", {"day": 2})
        assert len(discover_raw_files(tmp_path / "raw")) == 1

    def test_preserved_area_is_outside_the_wiki_so_it_is_not_indexed(
        self, tmp_path: Path
    ) -> None:
        # AC7: whether a preserved log is indexed is decided EXPLICITLY. The
        # recall corpus is built from `wiki_root`; the preserved area lives
        # under the knowledge root and outside it, so nothing embeds it as
        # prose. This test pins that decision so a future change to the
        # preserved-area location has to confront it.
        _write_rule(tmp_path / "rules", "r1.yaml", _preserve_rule())
        _write_raw_jsonl(
            tmp_path / "raw", "hestia-lanes", "20260815T000000Z-aa.jsonl", {"e": 1}
        )
        _run(tmp_path)

        preserved = tmp_path / PRESERVED
        wiki = tmp_path / "wiki"
        assert preserved.is_dir()
        assert wiki not in preserved.parents
        # The audit ledger legitimately lives under `wiki/`; the LOG must not.
        if wiki.exists():
            assert [p.name for p in wiki.rglob("20260815T000000Z-aa.jsonl")] == []
            assert list(wiki.rglob("*.md")) == []


class TestFactCarriesSourcePointer:
    """AC5 + AC6: a fact ingested FROM a preserved log carries a source pointer
    back to it, and the worked example round-trips."""

    def test_pointer_format_has_path_and_locator(self, tmp_path: Path) -> None:
        dest = tmp_path / PRESERVED / "hestia-lanes" / "a.jsonl"
        dest.parent.mkdir(parents=True)
        dest.write_text("{}\n", encoding="utf-8")
        pointer = preserved_log_source_pointer(tmp_path, dest, fmt="jsonl")
        assert pointer == "preserved-log:logs/hestia-lanes/a.jsonl#L1"

    def test_markdown_locator_names_the_frontmatter_record(self, tmp_path: Path) -> None:
        dest = tmp_path / PRESERVED / "j" / "a.md"
        dest.parent.mkdir(parents=True)
        dest.write_text("---\nx: 1\n---\n", encoding="utf-8")
        pointer = preserved_log_source_pointer(tmp_path, dest, fmt="md")
        assert pointer == "preserved-log:logs/j/a.md#frontmatter"

    def test_preserve_without_a_correction_compiles_no_fact(
        self, tmp_path: Path
    ) -> None:
        _write_rule(tmp_path / "rules", "r1.yaml", _preserve_rule())
        _write_raw_jsonl(
            tmp_path / "raw", "hestia-lanes", "20260815T000000Z-aa.jsonl", {"e": 1}
        )
        _run(tmp_path)
        assert find_correction_batches(tmp_path / "raw") == []

    def test_worked_example_bounce_fact_points_back_at_the_log(
        self, tmp_path: Path
    ) -> None:
        # The issue's worked example: `<address>, bounced` produces the bounce
        # fact on the PII surface, and that fact's source resolves to the log.
        _write_rule(
            tmp_path / "rules",
            "r1.yaml",
            _preserve_rule(correction=_bounce_correction()),
        )
        _write_raw_jsonl(
            tmp_path / "raw",
            "hestia-lanes",
            "20260815T000000Z-aa.jsonl",
            {
                "email": "jon@smith.com",
                "status": "bounced",
                "status_date": "2026-08-15",
                "observed_at": "2026-08-15T00:00:00Z",
            },
        )
        summary = _run(tmp_path)
        assert summary["dispositions"] == {"preserve": 1}

        batches = find_correction_batches(tmp_path / "raw")
        assert len(batches) == 1
        batch_path, _source, _envelope = batches[0]
        lines = [
            json.loads(line)
            for line in batch_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        record = next(r for r in lines if r.get("record") == "correction")

        assert record["target"] == {"type": "person", "handle": {"email": "jon@smith.com"}}
        assert record["field"] == "bounced"
        assert record["value"] == "2026-08-15"

        # The source RESOLVES to the preserved artifact...
        source = record["source"]
        assert isinstance(source, dict)
        assert source["ref"] == (
            "preserved-log:logs/hestia-lanes/20260815T000000Z-aa.jsonl#L1"
        )
        # ...and the log it points at actually exists.
        pointed_at = tmp_path / source["ref"].split(":", 1)[1].split("#", 1)[0]
        assert pointed_at.is_file()

    def test_source_pointer_does_not_demote_the_facts_precedence(
        self, tmp_path: Path
    ) -> None:
        # An unknown source TYPE silently falls to the rank-9 default, so
        # replacing the whole scalar would quietly demote every fact a
        # preserved log produces. The machine tier must survive the rewrite.
        from athenaeum.precedence import source_rank

        _write_rule(
            tmp_path / "rules",
            "r1.yaml",
            _preserve_rule(correction=_bounce_correction()),
        )
        _write_raw_jsonl(
            tmp_path / "raw",
            "hestia-lanes",
            "20260815T000000Z-aa.jsonl",
            {
                "email": "jon@smith.com",
                "status": "bounced",
                "status_date": "2026-08-15",
                "observed_at": "2026-08-15T00:00:00Z",
            },
        )
        _run(tmp_path)

        batch_path, _source, _envelope = find_correction_batches(tmp_path / "raw")[0]
        record = next(
            json.loads(line)
            for line in batch_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and json.loads(line).get("record") == "correction"
        )
        assert record["source"]["type"] == "script"
        assert source_rank(record["source"]) == source_rank("script:hestia-lane-log")

    def test_worked_example_round_trips_onto_the_pii_surface(
        self, tmp_path: Path
    ) -> None:
        """AC6 end-to-end: the log record produces the bounce fact ON THE PII
        SURFACE, and that fact's recorded source resolves to the preserved log.

        This runs the shape-rule phase and then the UNMODIFIED correction
        phase, so it proves the whole path rather than just the batch the
        engine writes.
        """
        from athenaeum.corrections import run_correction_phase
        from athenaeum.models import EntityIndex, parse_frontmatter

        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True, exist_ok=True)
        (wiki / "alex.md").write_text(
            "---\nuid: person-alex\ntype: person\nname: Alex Doe\n---\n",
            encoding="utf-8",
        )
        (tmp_path / "registry.json").write_text(
            json.dumps(
                {
                    "entities": {
                        "person-alex": {
                            "type": "person",
                            "handles": {"alt_emails": ["jon@smith.com"]},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        correction = _bounce_correction()
        correction["target"] = {
            "type": "person",
            "handle": {"alt_emails": "$email"},
        }
        _write_rule(
            tmp_path / "rules", "r1.yaml", _preserve_rule(correction=correction)
        )
        _write_raw_jsonl(
            tmp_path / "raw",
            "hestia-lanes",
            "20260815T000000Z-aa.jsonl",
            {
                "email": "jon@smith.com",
                "status": "bounced",
                "status_date": "2026-08-15",
                "observed_at": "2026-08-15T00:00:00Z",
            },
        )

        config = {
            "librarian": {
                "preserved_log_dir": PRESERVED,
                "corrections": {
                    "fields": {
                        "bounced": {
                            "shape": "scalar",
                            "writers": ["shape-rule:hestia-lane-log@1"],
                            "monotone": True,
                        }
                    },
                    "sensitive_fields": {"bounced": "pii"},
                },
            },
            "storage": {"mapping": {"pii": "excluded"}},
        }

        assert _run(tmp_path, config=config)["dispositions"] == {"preserve": 1}

        summary = run_correction_phase(
            raw_root=tmp_path / "raw",
            wiki_root=wiki,
            knowledge_root=tmp_path,
            index=EntityIndex(wiki),
            config=config,
            escalate_one=lambda _r, _o: False,
        )
        assert summary["dispositions"].get("routed-elsewhere") == 1

        # The bounce fact landed on the PII surface, off-corpus.
        surface = tmp_path / "excluded" / "person-alex.md"
        assert surface.is_file()
        meta, _body = parse_frontmatter(surface.read_text(encoding="utf-8"))
        assert meta["bounced"] == "2026-08-15"

        # ...and its provenance resolves to the preserved log, which exists.
        pointer = json.dumps(meta)
        assert "preserved-log:logs/hestia-lanes/20260815T000000Z-aa.jsonl#L1" in pointer
        assert (
            tmp_path / PRESERVED / "hestia-lanes" / "20260815T000000Z-aa.jsonl"
        ).is_file()

    def test_transform_failure_leaves_the_log_in_raw(self, tmp_path: Path) -> None:
        # A fact that cannot be compiled must not strand the log half-moved.
        _write_rule(
            tmp_path / "rules",
            "r1.yaml",
            _preserve_rule(correction=_bounce_correction()),
        )
        raw_path = _write_raw_jsonl(
            tmp_path / "raw", "hestia-lanes", "20260815T000000Z-aa.jsonl", {"e": 1}
        )
        summary = _run(tmp_path)

        assert summary["dispositions"] == {"transform-error": 1}
        assert raw_path.exists()
        assert not (tmp_path / PRESERVED).exists()


class TestPreservedLogAdapterRouting:
    """athenaeum#1132: `librarian.preserved_log_adapter` routes `preserve` through
    the whole-store adapter seam (`athenaeum.store.Store` /
    `athenaeum.storage.available_adapters`) instead of the local, in-repo
    `librarian.preserved_log_dir` area -- the seam that lets a preserved log
    land OUTSIDE the knowledge git repo."""

    ADAPTER_NAME = "mural-archive"

    def _adapter_config(self, outside_root: Path, *, dir_also: str | None = None) -> dict:
        cfg: dict = {
            "librarian": {"preserved_log_adapter": self.ADAPTER_NAME},
            "storage": {
                "adapters": {
                    self.ADAPTER_NAME: {
                        "backing_store": "filesystem",
                        "surface_root": str(outside_root),
                    }
                }
            },
        }
        if dir_also is not None:
            cfg["librarian"]["preserved_log_dir"] = dir_also
        return cfg

    def test_absolute_out_of_repo_adapter_moves_the_file(
        self, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """(a): an absolute, out-of-repo adapter surface -- the file lands
        there and the source is removed from `raw/`."""
        outside = tmp_path_factory.mktemp("mural-outside")
        _write_rule(tmp_path / "rules", "r1.yaml", _preserve_rule())
        raw_path = _write_raw_jsonl(
            tmp_path / "raw", "hestia-lanes", "20260815T000000Z-aa.jsonl", {"e": 1}
        )

        summary = _run(tmp_path, config=self._adapter_config(outside))

        assert summary["dispositions"] == {"preserve": 1}
        assert not raw_path.exists()
        dest = outside / "hestia-lanes" / "20260815T000000Z-aa.jsonl"
        assert dest.is_file()
        assert json.loads(dest.read_text(encoding="utf-8")) == {"e": 1}

    def test_both_keys_set_adapter_wins_and_shadow_warning_is_logged(
        self,
        tmp_path: Path,
        tmp_path_factory: pytest.TempPathFactory,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """(b): both `preserved_log_dir` and `preserved_log_adapter` set --
        the adapter wins, and the shadowing is logged, not silent."""
        outside = tmp_path_factory.mktemp("mural-outside")
        _write_rule(tmp_path / "rules", "r1.yaml", _preserve_rule())
        raw_path = _write_raw_jsonl(
            tmp_path / "raw", "hestia-lanes", "20260815T000000Z-aa.jsonl", {"e": 1}
        )

        with caplog.at_level(logging.WARNING):
            summary = _run(
                tmp_path, config=self._adapter_config(outside, dir_also=PRESERVED)
            )

        assert summary["dispositions"] == {"preserve": 1}
        assert not raw_path.exists()
        assert (outside / "hestia-lanes" / "20260815T000000Z-aa.jsonl").is_file()
        # The directory area was NOT used.
        assert not (tmp_path / PRESERVED).exists()
        assert "shadows" in caplog.text
        assert "preserved_log_dir" in caplog.text
        assert "preserved_log_adapter" in caplog.text

    def test_dir_only_and_neither_set_stay_byte_identical(self, tmp_path: Path) -> None:
        """(c): AC3, no forced migration -- only-dir-set and neither-set are
        unaffected by the adapter seam existing at all. Regression pin."""
        _write_rule(tmp_path / "rules", "r1.yaml", _preserve_rule())

        raw_path = _write_raw_jsonl(
            tmp_path / "raw", "hestia-lanes", "20260815T000000Z-aa.jsonl", {"e": 1}
        )
        # `_run`'s default config sets only `preserved_log_dir`.
        summary = _run(tmp_path)
        assert summary["dispositions"] == {"preserve": 1}
        assert not raw_path.exists()
        assert (
            tmp_path / PRESERVED / "hestia-lanes" / "20260815T000000Z-aa.jsonl"
        ).is_file()

        raw_path2 = _write_raw_jsonl(
            tmp_path / "raw", "hestia-lanes", "20260816T000000Z-bb.jsonl", {"e": 2}
        )
        summary2 = _run(tmp_path, config={})  # neither key set
        assert summary2["dispositions"] == {"preserve-unconfigured": 1}
        assert raw_path2.exists()

    def test_put_failure_leaves_source_untouched_and_tallies_preserve_failed(
        self, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """(d): fail-closed. `Store.put` raising (EXDEV is the expected case
        for a routed adapter on a different filesystem, not an edge case)
        must leave the source provably untouched, tally `preserve-failed`,
        and never let the exception escape `run_shape_rule_phase`."""
        outside = tmp_path_factory.mktemp("mural-outside")
        store = FilesystemStore(tmp_path, {self.ADAPTER_NAME: outside})

        def _raise_exdev(*_args: object, **_kwargs: object) -> str:
            raise OSError(18, "Invalid cross-device link")  # errno.EXDEV

        store.put = _raise_exdev  # type: ignore[method-assign]

        _write_rule(tmp_path / "rules", "r1.yaml", _preserve_rule())
        raw_path = _write_raw_jsonl(
            tmp_path / "raw", "hestia-lanes", "20260815T000000Z-aa.jsonl", {"e": 1}
        )
        original_bytes = raw_path.read_bytes()

        summary = _run(tmp_path, config=self._adapter_config(outside), store=store)

        assert summary["dispositions"] == {"preserve-failed": 1}
        assert raw_path.exists()
        assert raw_path.read_bytes() == original_bytes
        assert not (outside / "hestia-lanes").exists()

    def test_pointer_for_outside_repo_destination_is_a_resolvable_absolute_path(
        self, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """(e): the pointer format for the outside-repo case is a resolvable
        absolute path; the SCHEME does not vary by backend."""
        outside = tmp_path_factory.mktemp("mural-outside")
        dest = outside / "hestia-lanes" / "a.jsonl"
        dest.parent.mkdir(parents=True)
        dest.write_text("{}\n", encoding="utf-8")

        pointer = preserved_log_source_pointer(tmp_path, dest, fmt="jsonl")

        assert pointer == f"{PRESERVED_LOG_SOURCE_SCHEME}:{dest.as_posix()}#L1"
        path_segment = pointer.split(":", 1)[1].split("#", 1)[0]
        assert Path(path_segment).is_absolute()
        assert Path(path_segment).is_file()

    def test_worked_example_pointer_resolves_to_absolute_outside_repo_path(
        self, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """(e), end-to-end: a fact compiled from an adapter-routed preserved
        log carries a pointer that resolves to the real, outside-repo file."""
        outside = tmp_path_factory.mktemp("mural-outside")
        _write_rule(
            tmp_path / "rules", "r1.yaml", _preserve_rule(correction=_bounce_correction())
        )
        _write_raw_jsonl(
            tmp_path / "raw",
            "hestia-lanes",
            "20260815T000000Z-aa.jsonl",
            {
                "email": "jon@smith.com",
                "status": "bounced",
                "status_date": "2026-08-15",
                "observed_at": "2026-08-15T00:00:00Z",
            },
        )

        summary = _run(tmp_path, config=self._adapter_config(outside))
        assert summary["dispositions"] == {"preserve": 1}

        batch_path, _source, _envelope = find_correction_batches(tmp_path / "raw")[0]
        record = next(
            json.loads(line)
            for line in batch_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and json.loads(line).get("record") == "correction"
        )
        dest = outside / "hestia-lanes" / "20260815T000000Z-aa.jsonl"
        assert record["source"]["ref"] == f"{PRESERVED_LOG_SOURCE_SCHEME}:{dest.as_posix()}#L1"
        assert record["source"]["type"] == "script"  # precedence not demoted
        pointed_at = Path(record["source"]["ref"].split(":", 1)[1].split("#", 1)[0])
        assert pointed_at.is_file()

    def test_unknown_adapter_name_raises_storage_config_error(self, tmp_path: Path) -> None:
        """(f): a `preserved_log_adapter` naming an adapter that does not
        exist fails LOUDLY -- never a silent fallback to the directory."""
        _write_rule(tmp_path / "rules", "r1.yaml", _preserve_rule())
        _write_raw_jsonl(
            tmp_path / "raw", "hestia-lanes", "20260815T000000Z-aa.jsonl", {"e": 1}
        )
        config = {"librarian": {"preserved_log_adapter": "does-not-exist"}}

        with pytest.raises(StorageConfigError):
            _run(tmp_path, config=config)

    def test_different_content_collision_still_fails_closed(
        self, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """QA follow-up (1b): a same-named object with DIFFERENT content at
        the destination is a genuine, unrelated collision. Content equality
        is the only signal that authorizes convergence -- a name match
        alone must never be enough, or a genuinely different artifact could
        silently be treated as "already preserved"."""
        outside = tmp_path_factory.mktemp("mural-outside")
        dest_dir = outside / "hestia-lanes"
        dest_dir.mkdir(parents=True)
        (dest_dir / "20260815T000000Z-aa.jsonl").write_text(
            json.dumps({"e": "unrelated pre-existing content"}) + "\n", encoding="utf-8"
        )

        _write_rule(tmp_path / "rules", "r1.yaml", _preserve_rule())
        raw_path = _write_raw_jsonl(
            tmp_path / "raw", "hestia-lanes", "20260815T000000Z-aa.jsonl", {"e": 1}
        )
        original_bytes = raw_path.read_bytes()

        summary = _run(tmp_path, config=self._adapter_config(outside))

        assert summary["dispositions"] == {"preserve-failed": 1}
        assert raw_path.exists()
        assert raw_path.read_bytes() == original_bytes
        # The pre-existing, unrelated destination content was NOT clobbered.
        assert json.loads(
            (dest_dir / "20260815T000000Z-aa.jsonl").read_text(encoding="utf-8")
        ) == {"e": "unrelated pre-existing content"}

    def test_orphaned_source_is_tallied_honestly_not_as_preserve(
        self,
        tmp_path: Path,
        tmp_path_factory: pytest.TempPathFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """QA follow-up (1a): a durable `put` whose source removal fails
        must NEVER be tallied as terminal `preserve` -- that would
        contradict the ledger (the file is still in raw/) and, without the
        convergence path, jam every subsequent run forever. Also pins the
        `_disposition_tier` treatment: `preserve-orphaned-source` is
        deferred (`tier: null`), same as `preserve-unconfigured` /
        `preserve-failed`."""
        import athenaeum.rules as rules_mod

        outside = tmp_path_factory.mktemp("mural-outside")
        monkeypatch.setattr(rules_mod, "_unlink_quietly", lambda _path: False)

        _write_rule(tmp_path / "rules", "r1.yaml", _preserve_rule())
        raw_path = _write_raw_jsonl(
            tmp_path / "raw", "hestia-lanes", "20260815T000000Z-aa.jsonl", {"e": 1}
        )

        summary = _run(tmp_path, config=self._adapter_config(outside))

        assert summary["dispositions"] == {"preserve-orphaned-source": 1}
        # The artifact IS durably written...
        dest = outside / "hestia-lanes" / "20260815T000000Z-aa.jsonl"
        assert dest.is_file()
        assert json.loads(dest.read_text(encoding="utf-8")) == {"e": 1}
        # ...but the source was NOT removed -- unlike a real `preserve`.
        assert raw_path.exists()

        rows = _disposition_rows(tmp_path)
        assert len(rows) == 1
        assert rows[0]["disposition"] == "preserve-orphaned-source"
        assert rows[0]["tier"] is None

    def test_identical_content_collision_converges_on_a_second_run(
        self,
        tmp_path: Path,
        tmp_path_factory: pytest.TempPathFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """QA follow-up (1b): after an orphaned-source run, a second run's
        `put` collides against the SAME bytes at the SAME key -- that must
        read as "already preserved, finish the job" rather than a hard
        failure, so a transient removal glitch does not jam the file
        forever. Also proves no duplicate correction batch is written for
        the converged retry."""
        import athenaeum.rules as rules_mod

        outside = tmp_path_factory.mktemp("mural-outside")
        _write_rule(
            tmp_path / "rules", "r1.yaml", _preserve_rule(correction=_bounce_correction())
        )
        raw_path = _write_raw_jsonl(
            tmp_path / "raw",
            "hestia-lanes",
            "20260815T000000Z-aa.jsonl",
            {
                "email": "jon@smith.com",
                "status": "bounced",
                "status_date": "2026-08-15",
                "observed_at": "2026-08-15T00:00:00Z",
            },
        )

        # First run: force the removal step to fail -- orphaned source, but
        # the fact is still compiled (the artifact is durably in place).
        monkeypatch.setattr(rules_mod, "_unlink_quietly", lambda _path: False)
        first = _run(tmp_path, config=self._adapter_config(outside))
        assert first["dispositions"] == {"preserve-orphaned-source": 1}
        assert raw_path.exists()
        assert len(find_correction_batches(tmp_path / "raw")) == 1

        # Second run: removal works again. The file is still discovered
        # (still in raw/), the rule still matches, `put` collides against
        # the SAME bytes it wrote last time -- converge instead of failing.
        monkeypatch.undo()
        second = _run(tmp_path, config=self._adapter_config(outside))

        assert second["dispositions"] == {"preserve": 1}
        assert not raw_path.exists()
        dest = outside / "hestia-lanes" / "20260815T000000Z-aa.jsonl"
        assert dest.is_file()
        # No duplicate correction batch from the converged retry.
        assert len(find_correction_batches(tmp_path / "raw")) == 1

    def test_unrelated_malformed_adapter_entry_does_not_break_a_run_with_no_adapter_routed_preserve(
        self, tmp_path: Path
    ) -> None:
        """QA follow-up (2): the adapter-covering Store must be resolved
        LAZILY -- a config error in an adapter this run never actually
        routes through (no `preserve` rule sets `preserved_log_adapter` at
        all) must not abort a run that never needed it. Before this fix,
        `resolve_store_for_class` ran unconditionally for every run."""
        _write_rule(tmp_path / "rules", "r1.yaml", _preserve_rule())  # dir-routed only
        raw_path = _write_raw_jsonl(
            tmp_path / "raw", "hestia-lanes", "20260815T000000Z-aa.jsonl", {"e": 1}
        )
        config = {
            "librarian": {"preserved_log_dir": PRESERVED},  # no preserved_log_adapter
            "storage": {
                "adapters": {
                    # Missing the required `surface_root` -- malformed, and
                    # entirely unrelated to this run's dir-routed preserve.
                    "broken": {"backing_store": "filesystem"}
                }
            },
        }

        summary = _run(tmp_path, config=config)

        assert summary["dispositions"] == {"preserve": 1}
        assert not raw_path.exists()
        assert (tmp_path / PRESERVED / "hestia-lanes" / "20260815T000000Z-aa.jsonl").is_file()

    def test_preserve_raw_file_via_store_returns_untouched_outcome_when_source_already_gone(
        self, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """QA follow-up (4): direct unit test for the `raw_path.exists()`
        early return, mirroring `preserve_raw_file`'s own None-return
        contract -- previously uncovered by the whole suite."""
        outside = tmp_path_factory.mktemp("mural-outside")
        store = FilesystemStore(tmp_path, {self.ADAPTER_NAME: outside})
        missing = tmp_path / "raw" / "hestia-lanes" / "does-not-exist.jsonl"

        outcome = preserve_raw_file_via_store(
            tmp_path,
            missing,
            adapter_name=self.ADAPTER_NAME,
            source="hestia-lanes",
            rule_tag="hestia-lane-log@1",
            store=store,
        )

        assert outcome == PreserveViaStoreOutcome(
            dest_path=None, freshly_written=False, orphaned=False
        )
        assert not (outside / "hestia-lanes").exists()  # the store was never touched

    def test_store_object_matches_fails_closed_when_destination_unreadable(
        self, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """Coverage follow-up: `_store_object_matches` -- the convergence
        path's discriminator -- must fail closed (never raise, never claim
        a match) when the existing object at the destination cannot even be
        read. A monkeypatched `put` failure elsewhere in this class never
        exercises THIS function's own except branch, since `put` succeeding
        or failing is orthogonal to whether a later `read` succeeds."""
        from athenaeum.rules import _store_object_matches
        from athenaeum.store import StoreKey

        outside = tmp_path_factory.mktemp("mural-outside")
        store = FilesystemStore(tmp_path, {self.ADAPTER_NAME: outside})

        def _raise_oserror(*_args: object, **_kwargs: object) -> bytes:
            raise OSError("boom")

        store.read = _raise_oserror  # type: ignore[method-assign]

        key = StoreKey(surface=self.ADAPTER_NAME, key="hestia-lanes/a.jsonl")
        assert _store_object_matches(store, key, b"data") is False

    def test_unlink_quietly_returns_false_on_a_real_oserror(self, tmp_path: Path) -> None:
        """Coverage follow-up: the actual filesystem failure path for
        `_unlink_quietly`, not just the monkeypatched full-function
        replacement other tests use to force an orphaned source."""
        from athenaeum.rules import _unlink_quietly

        # unlink() on a directory raises IsADirectoryError (an OSError
        # subclass) -- a portable way to exercise the except branch without
        # depending on filesystem permission semantics.
        directory = tmp_path / "not-a-file"
        directory.mkdir()
        assert _unlink_quietly(directory) is False


class TestPreserveRuleSchema:
    """The `correction` block is OPTIONAL on `preserve` — and only on it."""

    def test_preserve_is_valid_without_a_correction(self) -> None:
        rule = ShapeRule.model_validate(_preserve_rule())
        assert rule.disposition == "preserve"
        assert rule.correction is None

    def test_preserve_is_valid_with_a_correction(self) -> None:
        rule = ShapeRule.model_validate(
            _preserve_rule(correction=_bounce_correction())
        )
        assert rule.correction is not None

    def test_preserve_may_not_carry_a_rollup_block(self) -> None:
        with pytest.raises(PydanticValidationError):
            ShapeRule.model_validate(
                _preserve_rule(rollup={"group_by": "$x", "aggregate": "count"})
            )

    def test_other_dispositions_still_refuse_a_correction(self) -> None:
        # The optionality is scoped to `preserve`; `drop`/`retain` are unchanged.
        for disposition in ("drop", "retain"):
            with pytest.raises(PydanticValidationError):
                ShapeRule.model_validate(
                    _preserve_rule(
                        disposition=disposition, correction=_bounce_correction()
                    )
                )
