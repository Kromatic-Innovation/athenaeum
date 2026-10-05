# SPDX-License-Identifier: Apache-2.0
"""Tests for :mod:`athenaeum.quiesce` (issue athenaeum#1898).

Three concerns, class-per-concern (mirrors ``tests/test_reasoning_triggers.py``):

- ``TestResolveQuiesceMaxHours`` — the one config resolver
  (:func:`athenaeum.config.resolve_quiesce_max_hours`).
- ``TestWriteQuiesce`` / ``TestReadQuiesceState`` / ``TestReleaseQuiesce`` —
  the sentinel's I/O: write, read (including the "expired == absent" rule
  and tolerant-reader behavior), release (idempotent).

CLI-level coverage (``athenaeum quiesce``) lives in
``tests/test_cmd_quiesce.py``. The ``evaluate_triggers(quiesce=...)``
branch coverage AC2 asks for lives in ``tests/test_reasoning_triggers.py``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from athenaeum.config import resolve_quiesce_max_hours
from athenaeum.quiesce import (
    QUIESCE_FILENAME,
    QuiesceDurationExceeded,
    QuiesceHeldByAnother,
    QuiesceState,
    quiesce_path,
    read_quiesce_state,
    release_quiesce,
    write_quiesce,
)

_FIXED_NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


class TestResolveQuiesceMaxHours:
    def test_default_is_six(self) -> None:
        assert resolve_quiesce_max_hours(None) == 6
        assert resolve_quiesce_max_hours({}) == 6

    def test_explicit_override(self) -> None:
        cfg = {"librarian": {"quiesce": {"max_hours": 12}}}
        assert resolve_quiesce_max_hours(cfg) == 12

    def test_bool_rejected(self) -> None:
        cfg = {"librarian": {"quiesce": {"max_hours": True}}}
        assert resolve_quiesce_max_hours(cfg) == 6

    def test_non_positive_rejected(self) -> None:
        cfg = {"librarian": {"quiesce": {"max_hours": 0}}}
        assert resolve_quiesce_max_hours(cfg) == 6
        cfg = {"librarian": {"quiesce": {"max_hours": -3}}}
        assert resolve_quiesce_max_hours(cfg) == 6


class TestWriteQuiesce:
    def test_writes_a_readable_sentinel(self, tmp_path: Path) -> None:
        state = write_quiesce(
            tmp_path,
            holder="alice@laptop",
            reason="backfilling",
            for_duration=timedelta(hours=2),
            now=_FIXED_NOW,
        )
        assert state.holder == "alice@laptop"
        assert state.reason == "backfilling"
        assert state.created_at == _FIXED_NOW
        assert state.expires_at == _FIXED_NOW + timedelta(hours=2)

        path = quiesce_path(tmp_path)
        assert path.is_file()
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk["holder"] == "alice@laptop"
        assert on_disk["reason"] == "backfilling"
        assert on_disk["created_at"] == "2026-09-25T12:00:00Z"
        assert on_disk["expires_at"] == "2026-09-25T14:00:00Z"

    def test_sentinel_path_is_the_configured_filename_at_root(
        self, tmp_path: Path
    ) -> None:
        write_quiesce(
            tmp_path,
            holder="a",
            reason="b",
            for_duration=timedelta(hours=1),
            now=_FIXED_NOW,
        )
        assert (tmp_path / QUIESCE_FILENAME).is_file()

    def test_same_holder_may_extend_its_own_sentinel(self, tmp_path: Path) -> None:
        # issue athenaeum#1965: a holder may always overwrite/extend its OWN
        # active sentinel -- this is what "last write wins" now means.
        write_quiesce(
            tmp_path,
            holder="first",
            reason="r1",
            for_duration=timedelta(hours=1),
            now=_FIXED_NOW,
        )
        write_quiesce(
            tmp_path,
            holder="first",
            reason="r2",
            for_duration=timedelta(hours=2),
            now=_FIXED_NOW,
        )
        state = read_quiesce_state(tmp_path, now=_FIXED_NOW)
        assert state is not None
        assert state.holder == "first"
        assert state.reason == "r2"
        assert state.expires_at == _FIXED_NOW + timedelta(hours=2)

    def test_foreign_overwrite_without_force_is_refused(self, tmp_path: Path) -> None:
        # issue athenaeum#1965 (AC2): a different holder's write against an
        # ACTIVE sentinel is refused, and the original is left intact.
        write_quiesce(
            tmp_path,
            holder="first",
            reason="r1",
            for_duration=timedelta(hours=1),
            now=_FIXED_NOW,
        )
        with pytest.raises(QuiesceHeldByAnother, match="first"):
            write_quiesce(
                tmp_path,
                holder="second",
                reason="r2",
                for_duration=timedelta(hours=2),
                now=_FIXED_NOW,
            )
        state = read_quiesce_state(tmp_path, now=_FIXED_NOW)
        assert state is not None
        assert state.holder == "first"
        assert state.reason == "r1"

    def test_force_overwrites_a_foreign_sentinel(self, tmp_path: Path) -> None:
        write_quiesce(
            tmp_path,
            holder="first",
            reason="r1",
            for_duration=timedelta(hours=1),
            now=_FIXED_NOW,
        )
        state = write_quiesce(
            tmp_path,
            holder="second",
            reason="r2",
            for_duration=timedelta(hours=2),
            now=_FIXED_NOW,
            force=True,
        )
        assert state.holder == "second"
        assert state.reason == "r2"
        on_disk = read_quiesce_state(tmp_path, now=_FIXED_NOW)
        assert on_disk is not None
        assert on_disk.holder == "second"

    def test_expired_foreign_sentinel_is_overwritable_without_force(
        self, tmp_path: Path
    ) -> None:
        # The expiry carve-out: an expired sentinel is inert, so a DIFFERENT
        # holder may overwrite it with no --force at all.
        write_quiesce(
            tmp_path,
            holder="first",
            reason="r1",
            for_duration=timedelta(hours=1),
            now=_FIXED_NOW,
        )
        after_expiry = _FIXED_NOW + timedelta(hours=2)
        state = write_quiesce(
            tmp_path,
            holder="second",
            reason="r2",
            for_duration=timedelta(hours=1),
            now=after_expiry,
        )
        assert state.holder == "second"

    def test_malformed_sentinel_is_overwritable_without_force(
        self, tmp_path: Path
    ) -> None:
        # The malformed carve-out: no holder is knowable, so any holder may
        # overwrite with no --force.
        quiesce_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        quiesce_path(tmp_path).write_text("not json", encoding="utf-8")
        state = write_quiesce(
            tmp_path,
            holder="anyone",
            reason="r",
            for_duration=timedelta(hours=1),
            now=_FIXED_NOW,
        )
        assert state.holder == "anyone"

    def test_quiesce_held_by_another_is_a_value_error_subclass(self) -> None:
        assert issubclass(QuiesceHeldByAnother, ValueError)

    def test_non_positive_duration_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            write_quiesce(
                tmp_path,
                holder="a",
                reason="b",
                for_duration=timedelta(0),
                now=_FIXED_NOW,
            )
        with pytest.raises(ValueError):
            write_quiesce(
                tmp_path,
                holder="a",
                reason="b",
                for_duration=timedelta(hours=-1),
                now=_FIXED_NOW,
            )

    def test_duration_over_default_max_is_rejected(self, tmp_path: Path) -> None:
        # AC4: a --for longer than the configured maximum is rejected with a
        # clear error. Default max is 6h.
        with pytest.raises(QuiesceDurationExceeded, match="6h"):
            write_quiesce(
                tmp_path,
                holder="a",
                reason="b",
                for_duration=timedelta(hours=7),
                now=_FIXED_NOW,
            )

    def test_duration_at_exactly_the_max_is_accepted(self, tmp_path: Path) -> None:
        state = write_quiesce(
            tmp_path,
            holder="a",
            reason="b",
            for_duration=timedelta(hours=6),
            now=_FIXED_NOW,
        )
        assert state.expires_at == _FIXED_NOW + timedelta(hours=6)

    def test_configured_max_overrides_the_default(self, tmp_path: Path) -> None:
        cfg = {"librarian": {"quiesce": {"max_hours": 12}}}
        state = write_quiesce(
            tmp_path,
            holder="a",
            reason="b",
            for_duration=timedelta(hours=10),
            config=cfg,
            now=_FIXED_NOW,
        )
        assert state.expires_at == _FIXED_NOW + timedelta(hours=10)

        with pytest.raises(QuiesceDurationExceeded, match="12h"):
            write_quiesce(
                tmp_path,
                holder="a",
                reason="b",
                for_duration=timedelta(hours=13),
                config=cfg,
                now=_FIXED_NOW,
            )

    def test_duration_exceeded_is_a_value_error_subclass(self, tmp_path: Path) -> None:
        # A caller that only wants "was this rejected" can catch ValueError
        # alone -- QuiesceDurationExceeded must still be one.
        assert issubclass(QuiesceDurationExceeded, ValueError)


class TestReadQuiesceState:
    def test_missing_file_is_none(self, tmp_path: Path) -> None:
        assert read_quiesce_state(tmp_path) is None

    def test_active_sentinel_is_read_back(self, tmp_path: Path) -> None:
        write_quiesce(
            tmp_path,
            holder="alice",
            reason="r",
            for_duration=timedelta(hours=2),
            now=_FIXED_NOW,
        )
        state = read_quiesce_state(tmp_path, now=_FIXED_NOW + timedelta(hours=1))
        assert state == QuiesceState(
            holder="alice",
            reason="r",
            created_at=_FIXED_NOW,
            expires_at=_FIXED_NOW + timedelta(hours=2),
        )

    def test_expired_sentinel_is_treated_as_absent(self, tmp_path: Path) -> None:
        write_quiesce(
            tmp_path,
            holder="alice",
            reason="r",
            for_duration=timedelta(hours=2),
            now=_FIXED_NOW,
        )
        # Exactly at expiry and past it both read as absent (`<=`, not `<`).
        assert read_quiesce_state(tmp_path, now=_FIXED_NOW + timedelta(hours=2)) is None
        assert read_quiesce_state(tmp_path, now=_FIXED_NOW + timedelta(hours=3)) is None

    def test_expired_sentinel_is_left_on_disk_not_deleted(self, tmp_path: Path) -> None:
        # Deliberate design choice (see the module docstring's "Expiry is
        # read-time, not write-time" section): a read never mutates, even
        # for an expired sentinel -- this read path is also reached by
        # `ingest --evaluate-only`, documented as never mutating anything.
        write_quiesce(
            tmp_path,
            holder="alice",
            reason="r",
            for_duration=timedelta(hours=2),
            now=_FIXED_NOW,
        )
        assert read_quiesce_state(tmp_path, now=_FIXED_NOW + timedelta(hours=3)) is None
        assert quiesce_path(tmp_path).is_file()

    def test_malformed_json_is_none(self, tmp_path: Path) -> None:
        quiesce_path(tmp_path).write_text("not json", encoding="utf-8")
        assert read_quiesce_state(tmp_path) is None

    def test_non_dict_json_is_none(self, tmp_path: Path) -> None:
        quiesce_path(tmp_path).write_text("[1, 2, 3]", encoding="utf-8")
        assert read_quiesce_state(tmp_path) is None

    @pytest.mark.parametrize(
        "missing_field", ["holder", "reason", "created_at", "expires_at"]
    )
    def test_missing_required_field_is_none(
        self, tmp_path: Path, missing_field: str
    ) -> None:
        payload = {
            "holder": "alice",
            "reason": "r",
            "created_at": "2026-09-25T12:00:00Z",
            "expires_at": "2026-09-25T14:00:00Z",
        }
        del payload[missing_field]
        quiesce_path(tmp_path).write_text(json.dumps(payload), encoding="utf-8")
        assert read_quiesce_state(tmp_path) is None

    def test_unparsable_timestamp_is_none(self, tmp_path: Path) -> None:
        payload = {
            "holder": "alice",
            "reason": "r",
            "created_at": "not-a-timestamp",
            "expires_at": "2026-09-25T14:00:00Z",
        }
        quiesce_path(tmp_path).write_text(json.dumps(payload), encoding="utf-8")
        assert read_quiesce_state(tmp_path) is None


class TestReleaseQuiesce:
    def test_release_removes_an_active_sentinel(self, tmp_path: Path) -> None:
        write_quiesce(
            tmp_path,
            holder="a",
            reason="b",
            for_duration=timedelta(hours=1),
            now=_FIXED_NOW,
        )
        assert release_quiesce(tmp_path, holder="a") is True
        assert not quiesce_path(tmp_path).exists()
        assert read_quiesce_state(tmp_path) is None

    def test_release_is_idempotent_on_an_absent_sentinel(self, tmp_path: Path) -> None:
        assert release_quiesce(tmp_path, holder="a") is False
        assert release_quiesce(tmp_path, holder="a") is False

    def test_release_removes_an_already_expired_sentinel(self, tmp_path: Path) -> None:
        # Unlike a read, --release is unconditional on expiry: it removes
        # the FILE, not just "stops honoring it".
        write_quiesce(
            tmp_path,
            holder="a",
            reason="b",
            for_duration=timedelta(hours=1),
            now=_FIXED_NOW,
        )
        # File exists but is expired relative to real "now" (far future
        # fixed timestamp) -- release must still remove it.
        assert quiesce_path(tmp_path).is_file()
        assert release_quiesce(tmp_path, holder="a") is True
        assert not quiesce_path(tmp_path).exists()

    def test_foreign_release_without_force_is_refused(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # issue athenaeum#1965 (AC1): a different holder's --release against
        # an ACTIVE sentinel is refused -- file stays, returns False, and
        # the on-disk holder is named in the log.
        import logging

        # release_quiesce's own activity check uses REAL "now" (it takes no
        # `now` override), so this sentinel must be active relative to the
        # real clock -- unlike every other test in this module, it must NOT
        # pin `now=_FIXED_NOW` (a fixed past date the real clock has long
        # since passed, which would make it read as expired/inert instead
        # of contested).
        write_quiesce(
            tmp_path,
            holder="alice@laptop",
            reason="b",
            for_duration=timedelta(hours=1),
        )
        with caplog.at_level(logging.WARNING):
            result = release_quiesce(tmp_path, holder="bob@laptop")
        assert result is False
        assert quiesce_path(tmp_path).is_file()
        assert read_quiesce_state(tmp_path) is not None
        assert "alice@laptop" in caplog.text

    def test_own_release_succeeds_when_sentinel_has_a_holder(
        self, tmp_path: Path
    ) -> None:
        write_quiesce(
            tmp_path,
            holder="alice@laptop",
            reason="b",
            for_duration=timedelta(hours=1),
            now=_FIXED_NOW,
        )
        assert release_quiesce(tmp_path, holder="alice@laptop") is True
        assert not quiesce_path(tmp_path).exists()

    def test_force_releases_a_foreign_sentinel(self, tmp_path: Path) -> None:
        write_quiesce(
            tmp_path,
            holder="alice@laptop",
            reason="b",
            for_duration=timedelta(hours=1),
            now=_FIXED_NOW,
        )
        assert release_quiesce(tmp_path, holder="bob@laptop", force=True) is True
        assert not quiesce_path(tmp_path).exists()

    def test_expired_foreign_release_is_allowed_without_holder_match(
        self, tmp_path: Path
    ) -> None:
        # The expiry carve-out applies to release too: a stale foreign
        # sentinel must not become unreleasable.
        already_past = _FIXED_NOW - timedelta(hours=5)
        write_quiesce(
            tmp_path,
            holder="alice@laptop",
            reason="b",
            for_duration=timedelta(hours=1),
            now=already_past,
        )
        assert quiesce_path(tmp_path).is_file()
        assert release_quiesce(tmp_path, holder="bob@laptop") is True
        assert not quiesce_path(tmp_path).exists()

    def test_malformed_sentinel_release_is_allowed_without_holder_match(
        self, tmp_path: Path
    ) -> None:
        # No holder is knowable for a malformed sentinel, so any holder may
        # release it.
        quiesce_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        quiesce_path(tmp_path).write_text("not json", encoding="utf-8")
        assert release_quiesce(tmp_path, holder="anyone") is True
        assert not quiesce_path(tmp_path).exists()
