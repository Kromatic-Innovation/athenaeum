# SPDX-License-Identifier: Apache-2.0
"""Pending-batch handle store and raw-file lease (issue athenaeum#1143).

The hazard this store exists to close: :func:`athenaeum.intake.discover_raw_files`
has no in-flight or claim concept, and raw files are only unlinked on finalize
success — so under a submit-and-exit design, run N submits 300 files and exits,
and run N+1 rediscovers the same 300 and resubmits them at full price. Silently.

These tests are organised by the issue's acceptance criteria, and each class
names the one it covers.
"""

from __future__ import annotations

import ast
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from athenaeum import batch_state, config, librarian
from athenaeum.intake import discover_raw_files
from athenaeum.models import RawFile

NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)


def _raw(tmp_path: Path, source: str, name: str, body: str = "hello") -> RawFile:
    """A RawFile on disk under ``<tmp_path>/raw/<source>/<name>``."""
    path = tmp_path / "raw" / source / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    stem = name.removesuffix(".md")
    timestamp, _, uuid8 = stem.rpartition("-")
    return RawFile(path=path, source=source, timestamp=timestamp, uuid8=uuid8)


def _record(cache_dir: Path, raws: dict[str, RawFile], **kw: object) -> object:
    kw.setdefault("batch_id", "msgbatch_1")
    kw.setdefault("knob", "classify")
    kw.setdefault("now", NOW)
    return batch_state.record_handle(cache_dir, refs=raws, **kw)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AC1 — the module, its surface, its store, and its layering
# ---------------------------------------------------------------------------


class TestModuleContract:
    def test_public_surface(self) -> None:
        for name in (
            "load",
            "record_handle",
            "retire_handle",
            "leased_refs",
            "release_lease",
        ):
            assert callable(getattr(batch_state, name)), name

    def test_store_lives_at_pending_batches_json(self, tmp_path: Path) -> None:
        assert batch_state.store_path(tmp_path) == tmp_path / "pending_batches.json"
        _record(tmp_path, {"cid-1": _raw(tmp_path, "s", "2026-01-01T00-00-00-aaaaaaaa.md")})
        assert (tmp_path / "pending_batches.json").exists()

    def test_writes_go_through_atomic_write_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A torn store is a store that fails open into a double-submit."""
        calls: list[Path] = []
        real = batch_state.atomic_write_text

        def spy(path: Path, text: str, **kw: object) -> None:
            calls.append(path)
            real(path, text, **kw)  # type: ignore[arg-type]

        monkeypatch.setattr(batch_state, "atomic_write_text", spy)
        _record(tmp_path, {"cid-1": _raw(tmp_path, "s", "2026-01-01T00-00-00-aaaaaaaa.md")})
        assert calls == [tmp_path / "pending_batches.json"]

    def test_resolve_cache_dir_honors_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(tmp_path))
        assert batch_state.resolve_cache_dir() == tmp_path

    def test_docstring_cites_the_precedent_and_states_the_layering(self) -> None:
        doc = batch_state.__doc__ or ""
        assert "detection_state" in doc
        assert "L3 service" in doc
        assert "never imports" in doc and "librarian" in doc

    def test_never_imports_librarian(self) -> None:
        """Layering is enforced, not merely documented."""
        source = Path(batch_state.__file__).read_text(encoding="utf-8")
        imported: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                if node.module == "athenaeum":
                    imported.update(f"athenaeum.{a.name}" for a in node.names)
        assert "athenaeum.librarian" not in imported
        assert {"athenaeum.config", "athenaeum.atomic_io"} <= imported


# ---------------------------------------------------------------------------
# AC2 — fail-open: a marker store must never break the run it guards
# ---------------------------------------------------------------------------


class TestFailOpen:
    def test_missing_store_loads_empty(self, tmp_path: Path) -> None:
        assert batch_state.load(tmp_path) == {}

    def test_empty_file_loads_empty_and_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        batch_state.store_path(tmp_path).write_text("", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="athenaeum.batch_state"):
            assert batch_state.load(tmp_path) == {}
        assert "pending-batch store" in caplog.text

    def test_corrupt_store_loads_empty_and_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        batch_state.store_path(tmp_path).write_text("{not json", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="athenaeum.batch_state"):
            assert batch_state.load(tmp_path) == {}
        assert "unreadable" in caplog.text

    def test_non_object_json_loads_empty(self, tmp_path: Path) -> None:
        batch_state.store_path(tmp_path).write_text("[1,2,3]", encoding="utf-8")
        assert batch_state.load(tmp_path) == {}

    def test_unknown_version_loads_empty(self, tmp_path: Path) -> None:
        batch_state.store_path(tmp_path).write_text(
            json.dumps({"version": 99, "handles": {"b": {}}}), encoding="utf-8"
        )
        assert batch_state.load(tmp_path) == {}

    def test_malformed_handle_entries_are_dropped_not_raised(self, tmp_path: Path) -> None:
        batch_state.store_path(tmp_path).write_text(
            json.dumps(
                {
                    "version": batch_state.STORE_VERSION,
                    "handles": {
                        "good": {"knob": "classify", "refs": {"c1": {"ref": "s/a.md"}}},
                        "bad": ["not", "a", "dict"],
                        "": {"knob": "write"},
                    },
                }
            ),
            encoding="utf-8",
        )
        loaded = batch_state.load(tmp_path)
        assert set(loaded) == {"good"}
        assert loaded["good"].refs["c1"].ref == "s/a.md"

    def test_unparseable_lease_stamp_does_not_strand_intake(self, tmp_path: Path) -> None:
        """A corrupt stamp reads as NO lease, never an infinite one."""
        batch_state.store_path(tmp_path).write_text(
            json.dumps(
                {
                    "version": batch_state.STORE_VERSION,
                    "handles": {
                        "b": {
                            "knob": "classify",
                            "leased_until": "not-a-timestamp",
                            "refs": {"c1": {"ref": "s/a.md"}},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        assert batch_state.leased_refs(tmp_path, now=NOW) == set()

    def test_unwritable_store_warns_and_does_not_raise(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def boom(*_a: object, **_k: object) -> None:
            raise OSError("read-only filesystem")

        monkeypatch.setattr(batch_state, "atomic_write_text", boom)
        with caplog.at_level(logging.WARNING, logger="athenaeum.batch_state"):
            _record(tmp_path, {"c1": _raw(tmp_path, "s", "2026-01-01T00-00-00-aaaaaaaa.md")})
        assert "write failed" in caplog.text

    def test_a_corrupt_store_does_not_break_the_claim_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The end-to-end fail-open assertion the AC asks for."""
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(cache_dir))
        batch_state.store_path(cache_dir).write_text("{{{ truncated", encoding="utf-8")
        raw = _raw(tmp_path, "s", "2026-01-01T00-00-00-aaaaaaaa.md")
        ctx = SimpleNamespace(raw_files=[raw], dry_run=False)
        librarian._apply_pending_batch_leases(ctx)  # type: ignore[arg-type]
        assert ctx.raw_files == [raw]


# ---------------------------------------------------------------------------
# AC3 — per custom_id: ref, absolute path, content hash taken at CLAIM time
# ---------------------------------------------------------------------------


class TestHandleContents:
    def test_records_ref_absolute_path_and_hash_per_custom_id(self, tmp_path: Path) -> None:
        a = _raw(tmp_path, "src-a", "2026-01-01T00-00-00-aaaaaaaa.md", body="alpha")
        b = _raw(tmp_path, "src-b", "2026-01-02T00-00-00-bbbbbbbb.md", body="beta")
        _record(tmp_path, {"cid-a": a, "cid-b": b})
        handle = batch_state.load(tmp_path)["msgbatch_1"]
        assert set(handle.refs) == {"cid-a", "cid-b"}
        assert handle.refs["cid-a"].ref == a.ref
        assert Path(handle.refs["cid-a"].path).is_absolute()
        assert Path(handle.refs["cid-a"].path) == a.path.resolve()
        assert handle.refs["cid-a"].content_hash == batch_state.content_hash(a.path)
        assert handle.refs["cid-a"].content_hash != handle.refs["cid-b"].content_hash

    def test_hash_is_taken_at_claim_time_not_read_time(self, tmp_path: Path) -> None:
        raw = _raw(tmp_path, "s", "2026-01-01T00-00-00-aaaaaaaa.md", body="original")
        claimed = batch_state.content_hash(raw.path)
        _record(tmp_path, {"cid-a": raw})
        raw.path.write_text("rewritten under the batch", encoding="utf-8")
        stored = batch_state.load(tmp_path)["msgbatch_1"].refs["cid-a"].content_hash
        assert stored == claimed != batch_state.content_hash(raw.path)

    def test_records_batch_id_knob_submitted_at_and_phase(self, tmp_path: Path) -> None:
        _record(
            tmp_path,
            {"cid-a": _raw(tmp_path, "s", "2026-01-01T00-00-00-aaaaaaaa.md")},
            batch_id="msgbatch_write",
            knob="write",
        )
        handle = batch_state.load(tmp_path)["msgbatch_write"]
        assert handle.batch_id == "msgbatch_write"
        assert handle.knob == "write"
        assert handle.submitted_at == "2026-08-26T12:00:00Z"
        assert handle.phase == batch_state.DEFAULT_PHASE == "submitted"

    def test_unreadable_raw_file_hashes_empty_rather_than_raising(self, tmp_path: Path) -> None:
        missing = RawFile(
            path=tmp_path / "gone.md", source="s", timestamp="2026-01-01", uuid8="aaaaaaaa"
        )
        _record(tmp_path, {"cid-a": missing})
        assert batch_state.load(tmp_path)["msgbatch_1"].refs["cid-a"].content_hash == ""

    def test_empty_batch_id_records_nothing(self, tmp_path: Path) -> None:
        assert _record(tmp_path, {}, batch_id="") is None
        assert batch_state.load(tmp_path) == {}

    def test_re_recording_replaces_and_re_leases(self, tmp_path: Path) -> None:
        raw = _raw(tmp_path, "s", "2026-01-01T00-00-00-aaaaaaaa.md")
        _record(tmp_path, {"cid-a": raw})
        first = batch_state.load(tmp_path)["msgbatch_1"].leased_until
        _record(tmp_path, {"cid-a": raw}, now=NOW + timedelta(hours=1))
        second = batch_state.load(tmp_path)["msgbatch_1"].leased_until
        assert second is not None and first is not None and second > first
        assert len(batch_state.load(tmp_path)) == 1


# ---------------------------------------------------------------------------
# AC4 — leased_until, the 72h default, and the <= 0 opt-out
# ---------------------------------------------------------------------------


class TestLeaseResolution:
    def test_default_is_72h(self) -> None:
        assert config.DEFAULT_BATCH_LEASE_SECONDS == 259200.0 == 72 * 3600
        assert config.resolve_batch_lease_seconds(None) == 259200.0

    def test_leased_until_is_now_plus_the_resolved_lease(self, tmp_path: Path) -> None:
        _record(tmp_path, {"cid-a": _raw(tmp_path, "s", "2026-01-01T00-00-00-aaaaaaaa.md")})
        assert batch_state.load(tmp_path)["msgbatch_1"].leased_until == "2026-08-29T12:00:00Z"

    def test_yaml_overrides_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_BATCH_LEASE_SECONDS", raising=False)
        cfg = {"librarian": {"batch_lease_seconds": 60}}
        assert config.resolve_batch_lease_seconds(cfg) == 60.0

    def test_env_wins_over_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_BATCH_LEASE_SECONDS", "120")
        assert (
            config.resolve_batch_lease_seconds({"librarian": {"batch_lease_seconds": 60}}) == 120.0
        )

    @pytest.mark.parametrize("bad", [True, "not-a-number", None])
    def test_bool_and_non_numeric_fall_through_to_default(
        self, bad: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_BATCH_LEASE_SECONDS", raising=False)
        assert (
            config.resolve_batch_lease_seconds({"librarian": {"batch_lease_seconds": bad}})
            == config.DEFAULT_BATCH_LEASE_SECONDS
        )

    @pytest.mark.parametrize("value", [0, -1])
    def test_non_positive_disables_leasing(
        self, value: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_BATCH_LEASE_SECONDS", raising=False)
        cfg = {"librarian": {"batch_lease_seconds": value}}
        assert config.resolve_batch_lease_seconds(cfg) is None

    def test_disabled_leasing_records_a_handle_with_no_lease(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The explicit opt-out: the handle is still recorded, nothing is held."""
        monkeypatch.setenv("ATHENAEUM_BATCH_LEASE_SECONDS", "0")
        raw = _raw(tmp_path, "s", "2026-01-01T00-00-00-aaaaaaaa.md")
        _record(tmp_path, {"cid-a": raw})
        handle = batch_state.load(tmp_path)["msgbatch_1"]
        assert handle.leased_until is None
        assert handle.refs["cid-a"].ref == raw.ref
        assert batch_state.leased_refs(tmp_path, now=NOW) == set()


# ---------------------------------------------------------------------------
# AC5 — the claim loop excludes leased refs; discover_raw_files is UNCHANGED
# ---------------------------------------------------------------------------


class TestClaimLoopFiltering:
    def test_leased_refs_reports_live_leases_only(self, tmp_path: Path) -> None:
        a = _raw(tmp_path, "s", "2026-01-01T00-00-00-aaaaaaaa.md")
        b = _raw(tmp_path, "s", "2026-01-02T00-00-00-bbbbbbbb.md")
        _record(tmp_path, {"c1": a, "c2": b})
        assert batch_state.leased_refs(tmp_path, now=NOW + timedelta(hours=1)) == {a.ref, b.ref}
        assert batch_state.leased_refs(tmp_path, now=NOW + timedelta(days=4)) == set()

    def test_claim_pass_drops_leased_files_and_keeps_the_rest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache_dir = tmp_path / "cache"
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(cache_dir))
        leased = _raw(tmp_path, "s", "2026-01-01T00-00-00-aaaaaaaa.md")
        free = _raw(tmp_path, "s", "2026-01-02T00-00-00-bbbbbbbb.md")
        _record(cache_dir, {"c1": leased})
        ctx = SimpleNamespace(raw_files=[leased, free], dry_run=False)
        librarian._apply_pending_batch_leases(ctx)  # type: ignore[arg-type]
        assert [r.ref for r in ctx.raw_files] == [free.ref]

    def test_discover_raw_files_still_returns_leased_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Discovery stays pure filesystem enumeration — the filter is the
        claim loop's job, never ``discover_raw_files``'s."""
        cache_dir = tmp_path / "cache"
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(cache_dir))
        leased = _raw(tmp_path, "s", "2026-01-01T00-00-00-aaaaaaaa.md")
        _record(cache_dir, {"c1": leased})
        discovered = discover_raw_files(tmp_path / "raw")
        assert [r.ref for r in discovered] == [leased.ref]

    def test_claim_loop_is_wired_immediately_after_discovery(self) -> None:
        """Structural guard: the filter cannot be silently unhooked.

        Scoped to the CLAIM site (the one that assigns ``ctx.raw_files``) —
        the drain advisor's separate ``discover_raw_files`` call is a backlog
        COUNT of what is on disk, not a claim, and is deliberately untouched.
        """
        source = Path(librarian.__file__).read_text(encoding="utf-8").splitlines()
        sites = [
            i
            for i, line in enumerate(source)
            if "ctx.raw_files = discover_raw_files(" in line
        ]
        assert sites, "entity-phase claim call not found"
        for i in sites:
            window = "\n".join(source[i : i + 5])
            assert "_apply_pending_batch_leases(ctx)" in window


# ---------------------------------------------------------------------------
# AC6 — an expired lease is released on the next claim pass
# ---------------------------------------------------------------------------


class TestExpiredLeaseRelease:
    def test_release_expired_leases_clears_only_the_expired(self, tmp_path: Path) -> None:
        old = _raw(tmp_path, "s", "2026-01-01T00-00-00-aaaaaaaa.md")
        new = _raw(tmp_path, "s", "2026-01-02T00-00-00-bbbbbbbb.md")
        _record(tmp_path, {"c1": old}, batch_id="old", now=NOW - timedelta(days=4))
        _record(tmp_path, {"c2": new}, batch_id="new", now=NOW)
        assert batch_state.release_expired_leases(tmp_path, now=NOW) == ["old"]
        handles = batch_state.load(tmp_path)
        assert handles["old"].leased_until is None
        assert handles["new"].leased_until is not None

    def test_expired_handle_is_kept_so_it_can_still_be_collected(self, tmp_path: Path) -> None:
        raw = _raw(tmp_path, "s", "2026-01-01T00-00-00-aaaaaaaa.md")
        _record(tmp_path, {"c1": raw}, now=NOW - timedelta(days=4))
        batch_state.release_expired_leases(tmp_path, now=NOW)
        handle = batch_state.load(tmp_path)["msgbatch_1"]
        assert handle.refs["c1"].ref == raw.ref
        assert handle.batch_id == "msgbatch_1"

    def test_nothing_expired_writes_nothing(self, tmp_path: Path) -> None:
        _record(tmp_path, {"c1": _raw(tmp_path, "s", "2026-01-01T00-00-00-aaaaaaaa.md")})
        before = batch_state.store_path(tmp_path).read_bytes()
        assert batch_state.release_expired_leases(tmp_path, now=NOW) == []
        assert batch_state.store_path(tmp_path).read_bytes() == before

    def test_next_claim_pass_releases_and_the_refs_become_claimable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The AC end to end: an abandoned batch cannot strand its intake."""
        cache_dir = tmp_path / "cache"
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(cache_dir))
        raw = _raw(tmp_path, "s", "2026-01-01T00-00-00-aaaaaaaa.md")
        _record(cache_dir, {"c1": raw}, now=NOW - timedelta(days=4))
        ctx = SimpleNamespace(raw_files=[raw], dry_run=False)
        librarian._apply_pending_batch_leases(ctx)  # type: ignore[arg-type]
        assert [r.ref for r in ctx.raw_files] == [raw.ref]
        assert batch_state.load(cache_dir)["msgbatch_1"].leased_until is None


# ---------------------------------------------------------------------------
# AC7 — every clean non-dry-run exit path leaves no stale residue
# ---------------------------------------------------------------------------


class TestCleanExitPaths:
    #: The clean non-dry-run exits ``_clear_stale_deferred_manifest``'s own
    #: docstring enumerates, which the lease sweep must be paired with.
    ENUMERATED_EXITS = 4

    def test_every_manifest_clear_site_also_sweeps_leases(self) -> None:
        source = Path(librarian.__file__).read_text(encoding="utf-8").splitlines()
        sites = [
            i
            for i, line in enumerate(source)
            if "_clear_stale_deferred_manifest(" in line and not line.lstrip().startswith("def ")
        ]
        assert len(sites) >= self.ENUMERATED_EXITS, sites
        unpaired = [
            i + 1 for i in sites if "_sweep_pending_batch_leases()" not in source[i + 1]
        ]
        assert not unpaired, f"clean exit path(s) at line(s) {unpaired} do not sweep leases"

    def test_sweep_releases_expired_and_persists_live(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache_dir = tmp_path / "cache"
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(cache_dir))
        _record(
            cache_dir,
            {"c1": _raw(tmp_path, "s", "2026-01-01T00-00-00-aaaaaaaa.md")},
            batch_id="stale",
            now=datetime.now(timezone.utc) - timedelta(days=4),
        )
        _record(
            cache_dir,
            {"c2": _raw(tmp_path, "s", "2026-01-02T00-00-00-bbbbbbbb.md")},
            batch_id="live",
            now=datetime.now(timezone.utc),
        )
        librarian._sweep_pending_batch_leases()
        handles = batch_state.load(cache_dir)
        assert handles["stale"].leased_until is None
        assert handles["live"].leased_until is not None

    def test_sweep_on_an_untouched_machine_is_a_no_op(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache_dir = tmp_path / "cache"
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(cache_dir))
        librarian._sweep_pending_batch_leases()
        assert not batch_state.store_path(cache_dir).exists()

    def test_retiring_the_last_handle_removes_the_store(self, tmp_path: Path) -> None:
        """No stale residue: a fully-collected machine keeps no sidecar."""
        _record(tmp_path, {"c1": _raw(tmp_path, "s", "2026-01-01T00-00-00-aaaaaaaa.md")})
        batch_state.retire_handle(tmp_path, "msgbatch_1")
        assert batch_state.load(tmp_path) == {}
        assert not batch_state.store_path(tmp_path).exists()

    def test_retire_is_a_no_op_for_an_unknown_batch(self, tmp_path: Path) -> None:
        _record(tmp_path, {"c1": _raw(tmp_path, "s", "2026-01-01T00-00-00-aaaaaaaa.md")})
        batch_state.retire_handle(tmp_path, "never-recorded")
        batch_state.retire_handle(tmp_path, "")
        assert set(batch_state.load(tmp_path)) == {"msgbatch_1"}

    def test_release_lease_frees_refs_but_keeps_the_handle(self, tmp_path: Path) -> None:
        raw = _raw(tmp_path, "s", "2026-01-01T00-00-00-aaaaaaaa.md")
        _record(tmp_path, {"c1": raw})
        batch_state.release_lease(tmp_path, "msgbatch_1")
        assert batch_state.leased_refs(tmp_path, now=NOW) == set()
        assert batch_state.load(tmp_path)["msgbatch_1"].refs["c1"].ref == raw.ref

    def test_release_lease_is_a_no_op_for_an_unknown_batch(self, tmp_path: Path) -> None:
        batch_state.release_lease(tmp_path, "never-recorded")
        batch_state.release_lease(tmp_path, "")
        assert batch_state.load(tmp_path) == {}


# ---------------------------------------------------------------------------
# AC8 — --dry-run never writes a handle or takes a lease
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_claim_pass_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache_dir = tmp_path / "cache"
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(cache_dir))
        raw = _raw(tmp_path, "s", "2026-01-01T00-00-00-aaaaaaaa.md")
        _record(cache_dir, {"c1": raw}, now=datetime.now(timezone.utc) - timedelta(days=4))
        before = batch_state.store_path(cache_dir).read_bytes()
        ctx = SimpleNamespace(raw_files=[raw], dry_run=True)
        librarian._apply_pending_batch_leases(ctx)  # type: ignore[arg-type]
        assert batch_state.store_path(cache_dir).read_bytes() == before

    def test_dry_run_on_a_clean_machine_creates_no_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache_dir = tmp_path / "cache"
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(cache_dir))
        raw = _raw(tmp_path, "s", "2026-01-01T00-00-00-aaaaaaaa.md")
        ctx = SimpleNamespace(raw_files=[raw], dry_run=True)
        librarian._apply_pending_batch_leases(ctx)  # type: ignore[arg-type]
        assert not batch_state.store_path(cache_dir).exists()

    def test_dry_run_still_sees_the_same_claimable_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not writing must not mean not filtering — a dry run reports what a
        real run would claim."""
        cache_dir = tmp_path / "cache"
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(cache_dir))
        leased = _raw(tmp_path, "s", "2026-01-01T00-00-00-aaaaaaaa.md")
        free = _raw(tmp_path, "s", "2026-01-02T00-00-00-bbbbbbbb.md")
        expired = _raw(tmp_path, "s", "2026-01-03T00-00-00-cccccccc.md")
        _record(cache_dir, {"c1": leased}, batch_id="live", now=datetime.now(timezone.utc))
        _record(
            cache_dir,
            {"c2": expired},
            batch_id="stale",
            now=datetime.now(timezone.utc) - timedelta(days=4),
        )
        dry = SimpleNamespace(raw_files=[leased, free, expired], dry_run=True)
        librarian._apply_pending_batch_leases(dry)  # type: ignore[arg-type]
        wet = SimpleNamespace(raw_files=[leased, free, expired], dry_run=False)
        librarian._apply_pending_batch_leases(wet)  # type: ignore[arg-type]
        assert [r.ref for r in dry.raw_files] == [r.ref for r in wet.raw_files]
        assert [r.ref for r in dry.raw_files] == [free.ref, expired.ref]
