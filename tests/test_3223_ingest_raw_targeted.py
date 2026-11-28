# SPDX-License-Identifier: Apache-2.0
"""``athenaeum ingest --raw`` -- compile named raw files on demand (athenaeum#3223).

One test class per acceptance criterion. The compile is the real one wherever
the named files are structured (Tier 0, no LLM); a stub ``process_one`` stands
in only where a case needs a fate Tier 0 cannot produce (retained, dropped,
failed).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import athenaeum.librarian as lib
from athenaeum.cli import main
from athenaeum.librarian import RawTargetError, ingest, resolve_raw_targets
from athenaeum.models import RawFile

# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _seed(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge"
    schema = root / "wiki" / "_schema"
    schema.mkdir(parents=True)
    (schema / "types.md").write_text("# Types\n\n| Type |\n|------|\n| person |\n")
    (schema / "tags.md").write_text("# Tags\n\n| Tag |\n|-----|\n| active |\n")
    (schema / "access-levels.md").write_text("# Access\n\n| Level |\n|-------|\n| internal |\n")
    (root / "raw" / "sessions").mkdir(parents=True)
    (root / "raw" / "sessions" / ".gitkeep").write_text("")
    (root / "athenaeum.yaml").write_text(
        "search_backend: fts5\nrecall:\n  extra_intake_roots:\n    - raw/auto-memory\n",
        encoding="utf-8",
    )
    (root / "raw" / "auto-memory" / "scope").mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _tier0(root: Path, uid: str, name: str, stamp: str) -> Path:
    path = root / "raw" / "sessions" / f"{stamp}-aabbccdd.md"
    path.write_text(
        f"---\nuid: {uid}\ntype: person\nname: {name}\ntags: [active]\n"
        f"access: internal\n---\n\nNotes about {name}.\n",
        encoding="utf-8",
    )
    return path


def _prose(root: Path, stamp: str) -> Path:
    path = root / "raw" / "sessions" / f"{stamp}-ccddeeff.md"
    path.write_text("Met someone interesting about the new thing.\n", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _fake_llm(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    import anthropic as anthropic_mod

    client = MagicMock()
    monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kw: client)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-key-not-real")
    return client


def _ingest(root: Path, tmp_path: Path, *targets: str, **kw: Any) -> lib.IngestResult:
    return ingest(
        raw_root=root / "raw",
        wiki_root=root / "wiki",
        knowledge_root=root,
        cache_dir=tmp_path / "cache",
        raw_targets=list(targets),
        **kw,
    )


def _stamp(tmp_path: Path) -> dict[str, str]:
    path = tmp_path / "cache" / "ingest-manifest.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text())["hashes"]


def _outcomes(result: lib.IngestResult) -> dict[str, str]:
    assert result.file_outcomes is not None
    return {str(o["path"]): str(o["outcome"]) for o in result.file_outcomes}


def _tree_digest(*roots: Path) -> dict[str, str]:
    """Every file under *roots* (git internals included) -> content hash."""
    out: dict[str, str] = {}
    for base in roots:
        for p in sorted(base.rglob("*")):
            if p.is_file() and ".mypy_cache" not in p.parts:
                out[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


# ---------------------------------------------------------------------------
# AC1 -- only the named files are compiled; the counter-example
# ---------------------------------------------------------------------------


class TestOnlyNamedFilesCompile:
    def test_unnamed_pending_raw_is_not_compiled_deleted_or_stamped(self, tmp_path: Path) -> None:
        root = _seed(tmp_path)
        a = _tier0(root, "p-0001", "Alice Zhang", "20240410T120000Z")
        b = _tier0(root, "p-0002", "Bob Arlen", "20240411T120000Z")
        extra = _tier0(root, "p-0003", "Carol Ng", "20240412T120000Z")
        extra_text = extra.read_text()

        result = _ingest(root, tmp_path, "sessions/20240410T120000Z-*.md", f"sessions/{b.name}")

        assert result.exit_code == 0
        assert not a.exists() and not b.exists()  # consumed as a normal run does
        assert extra.read_text() == extra_text  # untouched
        assert not list((root / "wiki").glob("p-0003-*.md"))  # not compiled
        stamp = _stamp(tmp_path)
        assert f"raw/sessions/{a.name}" in stamp and f"raw/sessions/{b.name}" in stamp
        assert f"raw/sessions/{extra.name}" not in stamp  # not stamped
        assert list((root / "wiki").glob("p-0001-*.md"))
        assert list((root / "wiki").glob("p-0002-*.md"))

    def test_cli_compiles_only_named_and_summary_lists_each_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = _seed(tmp_path)
        a = _tier0(root, "p-0001", "Alice Zhang", "20240410T120000Z")
        extra = _tier0(root, "p-0003", "Carol Ng", "20240412T120000Z")

        rc = main(
            [
                "ingest",
                "--path",
                str(root),
                "--cache-dir",
                str(tmp_path / "cache"),
                "--raw",
                f"sessions/{a.name}",
            ]
        )

        assert rc == 0
        summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert summary["mode"] == "targeted"
        assert [(f["path"], f["outcome"]) for f in summary["files"]] == [
            (f"sessions/{a.name}", "created")
        ]
        assert extra.exists()

    def test_raw_flag_is_repeatable_and_takes_several_values(self, tmp_path: Path) -> None:
        root = _seed(tmp_path)
        a = _tier0(root, "p-0001", "Alice Zhang", "20240410T120000Z")
        b = _tier0(root, "p-0002", "Bob Arlen", "20240411T120000Z")
        c = _tier0(root, "p-0003", "Carol Ng", "20240412T120000Z")

        rc = main(
            [
                "ingest",
                "--path",
                str(root),
                "--cache-dir",
                str(tmp_path / "cache"),
                "--raw",
                f"sessions/{a.name}",
                f"sessions/{b.name}",
                "--raw",
                f"sessions/{c.name}",
            ]
        )

        assert rc == 0
        assert not (a.exists() or b.exists() or c.exists())

    def test_targeted_filter_drops_every_unnamed_file(self, tmp_path: Path) -> None:
        raws = [
            RawFile(path=tmp_path / f"{i}.md", source="s", timestamp="t", uuid8="u")
            for i in range(4)
        ]
        kept, n = lib._prioritize_caller_scoped_raw(
            raws, {tmp_path / "1.md", tmp_path / "3.md"}, targeted=True
        )
        assert [r.path.name for r in kept] == ["1.md", "3.md"] and n == 2
        # An empty name set names nothing, unlike the non-targeted partition.
        assert lib._prioritize_caller_scoped_raw(raws, set(), targeted=True) == ([], 0)
        assert lib._prioritize_caller_scoped_raw(raws, set()) == (raws, 0)


# ---------------------------------------------------------------------------
# AC2 -- resolution against the raw root; refusal outside it
# ---------------------------------------------------------------------------


class TestPathResolution:
    @pytest.mark.parametrize(
        "bad",
        ["../outside.md", "sessions/../../outside.md", "/opt/example/outside.md", "**/../../x.md"],
    )
    def test_path_outside_raw_root_is_refused(self, tmp_path: Path, bad: str) -> None:
        root = _seed(tmp_path)
        with pytest.raises(RawTargetError, match="outside the raw root|after a glob"):
            resolve_raw_targets(root / "raw", [bad])

    def test_cli_refuses_with_error_and_nonzero_exit_and_compiles_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = _seed(tmp_path)
        a = _tier0(root, "p-0001", "Alice Zhang", "20240410T120000Z")
        outside = tmp_path / "outside.md"
        outside.write_text("x")

        rc = main(
            [
                "ingest",
                "--path",
                str(root),
                "--cache-dir",
                str(tmp_path / "cache"),
                "--raw",
                f"sessions/{a.name}",
                "--raw",
                str(outside),
            ]
        )

        assert rc != 0
        assert "outside the raw root" in capsys.readouterr().err
        assert a.exists()  # nothing was compiled
        assert not (tmp_path / "cache" / "ingest-manifest.json").exists()

    def test_symlink_escaping_the_root_is_refused(self, tmp_path: Path) -> None:
        root = _seed(tmp_path)
        outside = tmp_path / "secret.md"
        outside.write_text("x")
        (root / "raw" / "sessions" / "link.md").symlink_to(outside)
        with pytest.raises(RawTargetError):
            resolve_raw_targets(root / "raw", ["sessions/link.md"])

    def test_literal_filename_with_glob_characters_names_itself(self, tmp_path: Path) -> None:
        # Seer review on PR athenaeum#3311: `[1]` is a glob class, not part of the name.
        root = _seed(tmp_path)
        odd = root / "raw" / "sessions" / "notes[1].md"
        odd.write_text("x")
        files, unmatched = resolve_raw_targets(root / "raw", ["sessions/notes[1].md"])
        assert files == [odd.resolve()]
        assert unmatched == []

    def test_absolute_path_through_a_symlink_to_the_root_is_accepted(self, tmp_path: Path) -> None:
        # Seer review on PR athenaeum#3311: the real path decides, not the spelling.
        root = _seed(tmp_path)
        a = _tier0(root, "p-0001", "Alice Zhang", "20240410T120000Z")
        alias = tmp_path / "alias"
        alias.symlink_to(root)
        files, unmatched = resolve_raw_targets(
            root / "raw", [str(alias / "raw" / "sessions" / a.name)]
        )
        assert files == [a.resolve()]
        assert unmatched == []

    def test_relative_paths_and_globs_resolve_against_the_raw_root(self, tmp_path: Path) -> None:
        root = _seed(tmp_path)
        a = _tier0(root, "p-0001", "Alice Zhang", "20240410T120000Z")
        b = _tier0(root, "p-0002", "Bob Arlen", "20240411T120000Z")
        files, unmatched = resolve_raw_targets(
            root / "raw", [f"sessions/{a.name}", "sessions/2024041*", "nope/*.md"]
        )
        assert files == sorted([a.resolve(), b.resolve()])
        assert unmatched == ["nope/*.md"]

    def test_raw_cannot_be_combined_with_session_or_full(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = _seed(tmp_path)
        for flag in (["--session", "s1"], ["--full"], ["--if-triggered"], ["--evaluate-only"]):
            rc = main(["ingest", "--path", str(root), "--raw", "x.md", *flag])
            assert rc == 1
            assert "--raw cannot be combined" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# AC3 -- every named file gets an outcome
# ---------------------------------------------------------------------------


class TestPerFileOutcomes:
    def test_created_not_found_and_not_entity_intake(self, tmp_path: Path) -> None:
        root = _seed(tmp_path)
        a = _tier0(root, "p-0001", "Alice Zhang", "20240410T120000Z")
        auto = root / "raw" / "auto-memory" / "scope" / "project_x.md"
        auto.write_text("---\nname: X\ntype: project\n---\n\nbody\n")

        result = _ingest(
            root,
            tmp_path,
            f"sessions/{a.name}",
            "auto-memory/scope/project_x.md",
            "sessions/does-not-exist.md",
        )

        assert _outcomes(result) == {
            f"sessions/{a.name}": "created",
            "auto-memory/scope/project_x.md": "not-entity-intake",
            "sessions/does-not-exist.md": "not-found",
        }
        assert auto.exists()  # auto-memory is never touched by a targeted run

    def test_a_glob_matching_nothing_reports_not_found_not_silence(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = _seed(tmp_path)
        rc = main(
            [
                "ingest",
                "--path",
                str(root),
                "--cache-dir",
                str(tmp_path / "cache"),
                "--raw",
                "sessions/*.nothing",
            ]
        )
        assert rc == 0
        captured = capsys.readouterr()
        assert (
            json.loads(captured.out.strip().splitlines()[-1])["files"][0]["outcome"] == "not-found"
        )
        assert "not-found" in captured.err  # the human-readable line too

    def test_retained_dropped_failed_follow_the_normal_run_decision(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _seed(tmp_path)
        keep = _prose(root, "20240410T120000Z")
        drop = _tier0(root, "p-0002", "Bob Arlen", "20240411T120000Z")
        boom = _tier0(root, "p-0003", "Carol Ng", "20240412T120000Z")

        def fake(raw: RawFile, *a: Any, **k: Any) -> Any:
            if raw.path == keep:
                return SimpleNamespace(
                    created=[], updated=[], escalated=[], skipped=["refused"], retain_raw=True
                )
            if raw.path == boom:
                raise RuntimeError("model fell over")
            return SimpleNamespace(created=[], updated=[], escalated=[], skipped=[])

        monkeypatch.setattr(lib, "process_one", fake)
        result = _ingest(root, tmp_path, "sessions/*.md")

        assert _outcomes(result) == {
            f"sessions/{keep.name}": "retained",
            f"sessions/{drop.name}": "dropped",
            f"sessions/{boom.name}": "failed",
        }
        # Consumed exactly as a normal run would: unlinked, or kept on retain_raw/failure.
        assert keep.exists() and boom.exists() and not drop.exists()
        # A failed compile leaves the whole stamp untouched (ingest's own rule), so
        # the file that did leave the queue is stamped on the next clean run.
        assert result.exit_code != 0 and _stamp(tmp_path) == {}

    def test_stamp_advances_only_for_files_that_left_the_queue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _seed(tmp_path)
        keep = _prose(root, "20240410T120000Z")
        drop = _tier0(root, "p-0002", "Bob Arlen", "20240411T120000Z")

        def fake(raw: RawFile, *a: Any, **k: Any) -> Any:
            return SimpleNamespace(
                created=[],
                updated=[],
                escalated=[],
                skipped=["refused"],
                retain_raw=raw.path == keep,
            )

        monkeypatch.setattr(lib, "process_one", fake)
        result = _ingest(root, tmp_path, "sessions/*.md")

        assert result.exit_code == 0 and keep.exists() and not drop.exists()
        assert list(_stamp(tmp_path)) == [f"raw/sessions/{drop.name}"]

    def test_files_beyond_max_files_are_deferred_not_dropped_silently(self, tmp_path: Path) -> None:
        root = _seed(tmp_path)
        a = _tier0(root, "p-0001", "Alice Zhang", "20240410T120000Z")
        b = _tier0(root, "p-0002", "Bob Arlen", "20240411T120000Z")

        result = _ingest(root, tmp_path, "sessions/*.md", max_files=1)

        outcomes = _outcomes(result)
        assert sorted(outcomes.values()) == ["created", "deferred"]
        deferred = [o for o in result.file_outcomes or [] if o["outcome"] == "deferred"]
        assert deferred[0]["reason"] == "window"
        assert (a.exists() + b.exists()) == 1  # the deferred one stays, unstamped
        assert len(_stamp(tmp_path)) == 1

    def test_a_stuck_named_file_is_reported_held(self, tmp_path: Path) -> None:
        root = _seed(tmp_path)
        a = _tier0(root, "p-0001", "Alice Zhang", "20240410T120000Z")
        raw = RawFile(path=a, source="sessions", timestamp="20240410T120000Z", uuid8="aabbccdd")
        lib._write_stuck_ledger(
            root / "wiki",
            {
                raw.ref: {
                    "hash": lib._stuck_content_hash(raw),
                    "failures": 99,
                    "last_action": "x",
                    "last_error": "boom",
                }
            },
        )

        result = _ingest(root, tmp_path, f"sessions/{a.name}")

        assert _outcomes(result) == {f"sessions/{a.name}": "held"}
        assert result.file_outcomes and result.file_outcomes[0]["reason"] == "stuck"
        assert a.exists() and not _stamp(tmp_path)

    def test_budget_checks_still_run_and_defer_named_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _seed(tmp_path)
        a = _tier0(root, "p-0001", "Alice Zhang", "20240410T120000Z")
        monkeypatch.setattr(lib.spend, "ceiling_tripped", lambda *a, **k: "tokens 9/1")

        result = _ingest(root, tmp_path, f"sessions/{a.name}")

        assert _outcomes(result) == {f"sessions/{a.name}": "deferred"}
        assert a.exists() and not _stamp(tmp_path)
        # A targeted run never rewrites the whole queue's deferred manifest.
        assert not list((root / "wiki").glob("_deferred*"))


# ---------------------------------------------------------------------------
# AC4 -- dry run
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_runs_the_compile_and_changes_nothing_on_disk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _fake_llm: MagicMock
    ) -> None:
        root = _seed(tmp_path)
        t0 = _tier0(root, "p-0001", "Alice Zhang", "20240410T120000Z")
        llm = _prose(root, "20240411T120000Z")
        cache = tmp_path / "cache"
        before = _tree_digest(root, cache)

        seen: list[dict[str, Any]] = []
        real_run = lib.run

        def spy(**kwargs: Any) -> int:
            seen.append(kwargs)
            return real_run(**kwargs)

        monkeypatch.setattr(lib, "run", spy)
        result = _ingest(root, tmp_path, "sessions/*.md", dry_run=True)

        assert seen and seen[0]["dry_run"] is True and seen[0]["targeted"] is True
        assert result.exit_code == 0
        assert _outcomes(result) == {
            f"sessions/{t0.name}": "created",
            f"sessions/{llm.name}": "would-reach-tier2-3",
        }
        assert _tree_digest(root, cache) == before  # no page, deletion, stamp, index
        _fake_llm.messages.create.assert_not_called()

    @pytest.mark.parametrize(
        ("flags", "expected"),
        [
            ({"handled_no_op": True}, "consumed"),
            ({"retain_raw": True, "retain_raw_by_design": True}, "retained"),
            ({}, "would-reach-tier2-3"),
        ],
    )
    def test_dry_run_reports_a_tier0_decision_before_would_reach_tier2_3(
        self, flags: dict[str, bool], expected: str
    ) -> None:
        # Seer review on PR athenaeum#3311: a Tier 0/1 no-op or retain is not a
        # file that would go on to Tier 2/3, in a dry run as in a real one.
        seen: list[str] = []
        ctx = SimpleNamespace(
            dry_run=True, note_outcome=lambda _raw, outcome, _reason=None: seen.append(outcome)
        )
        result = SimpleNamespace(
            created=[], updated=[], skipped=[], **{"handled_no_op": False, **flags}
        )
        lib._note_processed_outcome(ctx, SimpleNamespace(ref="s/x.md"), result)  # type: ignore[arg-type]
        assert seen == [expected]

    def test_dry_run_cli_reports_and_does_not_refresh_the_index(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = _seed(tmp_path)
        a = _tier0(root, "p-0001", "Alice Zhang", "20240410T120000Z")
        monkeypatch.setattr(
            "athenaeum._cli_shared.refresh_index_after_run",
            lambda *a, **k: pytest.fail("a dry run must not write the index"),
        )
        rc = main(
            [
                "ingest",
                "--path",
                str(root),
                "--cache-dir",
                str(tmp_path / "cache"),
                "--raw",
                f"sessions/{a.name}",
                "--dry-run",
            ]
        )
        assert rc == 0
        assert (
            json.loads(capsys.readouterr().out.strip().splitlines()[-1])["files"][0]["outcome"]
            == "created"
        )
        assert a.exists()


# ---------------------------------------------------------------------------
# AC5 / AC6 -- phases skipped, phases kept
# ---------------------------------------------------------------------------

_SKIPPED = [
    "_run_wiki_dedup_phase",
    "_run_name_collision_phase",
    "_run_qualified_name_phase",
    "_run_pending_batch_collect_phase",
    "_run_auto_memory_phase",
    "_run_page_recompile_phase",
    "_run_shape_rule_phase",
    "_run_correction_phase",
    "_run_store_intake_phase",
    "_run_rule_proposal_phase",
    "_run_audit_nightly_drain_phase",
    "_run_signal_mining_phase",
    "_run_tier_movement_proposals_phase",
    "_run_auto_apply_proposals_phase",
    "_run_attribution_shadow_phase",
]
_KEPT = [
    "_run_preconditions",
    "_resolve_run_config",
    "_run_git_vcs_io",
    "_run_raw_screen_phase",
    "_arm_run_deadline",
    "_run_entity_tier_phase",
    "_run_finalize_phase",
]


def _spy_phases(monkeypatch: pytest.MonkeyPatch, names: list[str]) -> list[str]:
    calls: list[str] = []
    for name in names:
        real = getattr(lib, name)

        def make(n: str, fn: Any) -> Any:
            def wrapper(*a: Any, **k: Any) -> Any:
                calls.append(n)
                return fn(*a, **k)

            return wrapper

        monkeypatch.setattr(lib, name, make(name, real))
    return calls


class TestPhaseGating:
    def test_wiki_dedup_is_not_invoked_in_targeted_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _seed(tmp_path)
        a = _tier0(root, "p-0001", "Alice Zhang", "20240410T120000Z")
        monkeypatch.setattr(lib, "_run_wiki_dedup_phase", lambda ctx: pytest.fail("wiki-dedup ran"))
        assert _ingest(root, tmp_path, f"sessions/{a.name}").exit_code == 0

    def test_every_corpus_wide_phase_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _seed(tmp_path)
        a = _tier0(root, "p-0001", "Alice Zhang", "20240410T120000Z")
        calls = _spy_phases(monkeypatch, _SKIPPED)
        assert _ingest(root, tmp_path, f"sessions/{a.name}").exit_code == 0
        assert calls == []

    def test_the_spies_are_live_a_normal_ingest_does_call_them(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Positive control: without it the skip assertions above could pass on a
        # spy that never fires.
        root = _seed(tmp_path)
        _tier0(root, "p-0001", "Alice Zhang", "20240410T120000Z")
        calls = _spy_phases(monkeypatch, _SKIPPED)
        ingest(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=tmp_path / "cache",
            max_api_calls=50,
        )
        assert "_run_wiki_dedup_phase" in calls
        assert "_run_auto_memory_phase" in calls

    def test_raw_screen_preconditions_deadline_and_finalize_still_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _seed(tmp_path)
        a = _tier0(root, "p-0001", "Alice Zhang", "20240410T120000Z")
        calls = _spy_phases(monkeypatch, _KEPT)
        _ingest(root, tmp_path, f"sessions/{a.name}")
        assert sorted(set(calls)) == sorted(_KEPT)

    def test_batch_mode_is_forced_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _seed(tmp_path)
        a = _tier0(root, "p-0001", "Alice Zhang", "20240410T120000Z")
        seen: list[Any] = []
        real = lib._run_entity_tier_phase

        def spy(ctx: Any) -> Any:
            seen.append((ctx.batch_mode, ctx.batch_classify, ctx.batch_write))
            return real(ctx)

        monkeypatch.setattr(lib, "_run_entity_tier_phase", spy)
        _ingest(root, tmp_path, f"sessions/{a.name}", batch_mode=True)
        assert seen == [(False, False, False)]

    def test_nightly_run_is_unchanged_by_the_new_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _seed(tmp_path)
        _tier0(root, "p-0001", "Alice Zhang", "20240410T120000Z")
        calls = _spy_phases(monkeypatch, ["_run_wiki_dedup_phase", "_run_page_recompile_phase"])
        stats: dict[str, Any] = {}
        rc = lib.run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=50,
            out_run_stats=stats,
        )
        assert rc == 0
        assert calls == ["_run_wiki_dedup_phase", "_run_page_recompile_phase"]
        assert "file_outcomes" not in stats  # a nightly run keeps its stats shape


# ---------------------------------------------------------------------------
# AC7 -- recall finds the page; the refresh is shared, not copied
# ---------------------------------------------------------------------------


class TestIndexRefresh:
    def test_recall_finds_the_page_a_targeted_write_created(self, tmp_path: Path) -> None:
        from athenaeum.config import resolve_extra_intake_roots
        from athenaeum.mcp_server import recall_search

        root = _seed(tmp_path)
        a = _tier0(root, "p-0001", "Zanzibar Quimby", "20240410T120000Z")
        cache = tmp_path / "cache"
        lib.reindex(knowledge_root=root, cache_dir=cache, backend="fts5")

        def recall() -> str:
            return recall_search(
                root / "wiki",
                "Zanzibar Quimby",
                top_k=5,
                search_backend="fts5",
                cache_dir=cache,
                extra_roots=resolve_extra_intake_roots(root),
            )

        assert "p-0001" not in recall()
        rc = main(
            [
                "ingest",
                "--path",
                str(root),
                "--cache-dir",
                str(cache),
                "--raw",
                f"sessions/{a.name}",
            ]
        )

        assert rc == 0
        out = recall()
        assert "p-0001" in out  # found with no further command
        assert a.name not in out

    def test_helper_is_shared_by_run_and_ingest_not_copied(self) -> None:
        import athenaeum._cmd_index as cmd_index
        import athenaeum._cmd_run as cmd_run
        from athenaeum import _cli_shared

        assert not hasattr(cmd_run, "_refresh_index_after_run")
        assert cmd_run.refresh_index_after_run is _cli_shared.refresh_index_after_run
        src = Path(cmd_index.__file__).read_text()
        assert "refresh_index_after_run" in src and "def refresh_index_after_run" not in src


# ---------------------------------------------------------------------------
# AC9 -- documentation
# ---------------------------------------------------------------------------


class TestDocs:
    def test_cli_reference_documents_raw_and_its_dry_run_behaviour(self) -> None:
        text = (
            Path(__file__).resolve().parent.parent / "docs" / "reference" / "cli.md"
        ).read_text()
        row = next(line for line in text.splitlines() if line.startswith("| `--raw`"))
        assert "--dry-run" in row and "no-write" in row and "would-reach-tier2-3" in row
