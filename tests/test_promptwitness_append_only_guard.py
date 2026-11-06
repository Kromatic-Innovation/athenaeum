# SPDX-License-Identifier: Apache-2.0
"""Proof pair for the append-only leak guard (athenaeum#2335).

``tests/fixtures/promptwitness_append_only_guard.py`` clears the append-only
flag ``promptwitness.ledger`` sets on throwaway ledger partitions, and fails
the session if one survives uncleared. Both halves need a positive control,
for the same reason ``tests/test_default_root_guard.py`` carries one: a guard
that can only ever pass is indistinguishable from no guard at all, and this
one has to fire on the **Linux** CI runner for a bug that only *bites* on
macOS (an operator ruling on athenaeum#2266 declined to add a ``macos-*``
leg), so "it works on my machine" is not available as evidence.

Nothing here depends on the kernel enforcing the flag. On Linux ``chattr +a``
needs ``CAP_LINUX_IMMUTABLE``, which an ordinary CI process does not hold, so
the flag is never actually set and there is no ``st_flags`` to read back; the
tests below therefore assert over what the *library recorded*, which is
identical on both platforms. ``tests/test_promptwitness_ledger.py``'s
``TestAppendOnlyCounterExample`` is the complementary test that does need real
enforcement, and skips with a named reason where there is none.
"""

from __future__ import annotations

import json
import multiprocessing
import sys
from pathlib import Path

import pytest

from promptwitness import ledger
from tests.fixtures import promptwitness_append_only_guard as guard


def _append_one(ledger_dir: Path | str) -> None:
    ledger.append_line(
        ledger_dir,
        kind="turn",
        harness="claude-code",
        harness_version="1",
        hook_version="promptwitness 0.1.0",
        session="s0",
        turn=0,
        prompt_id="p0",
        prompt_kind="human",
        channel="interactive",
    )


class TestTheLibraryRecordsWhatItProtectedAndCleared:
    """The registry is what makes one portable check cover both platforms."""

    def test_creating_a_ledger_registers_its_partition_as_protected(
        self, tmp_path: Path
    ) -> None:
        _append_one(tmp_path)
        partition = next(tmp_path.glob("*.jsonl"))
        assert partition in ledger.protected_paths()
        assert partition not in ledger.cleared_paths()

    def test_clearing_moves_the_partition_from_protected_to_cleared(
        self, tmp_path: Path
    ) -> None:
        _append_one(tmp_path)
        partition = next(tmp_path.glob("*.jsonl"))

        assert ledger.clear_append_only_protection(partition) is True

        assert partition not in ledger.protected_paths()
        assert partition in ledger.cleared_paths()

    def test_registration_does_not_depend_on_the_flag_being_enforced(
        self, tmp_path: Path
    ) -> None:
        """The whole portability claim, asserted rather than assumed.

        ``_apply_append_only_protection`` reports ``chattr:failed`` on an
        ordinary Linux process and ``chflags:applied`` on macOS. The guard must
        behave the same either way, so the registry has to record the *attempt*.
        """
        _append_one(tmp_path)
        partition = next(tmp_path.glob("*.jsonl"))
        verdict = json.loads(partition.read_text().splitlines()[0])["ledger_meta"]

        assert verdict["protection"].endswith((":applied", ":failed"))
        assert partition in ledger.protected_paths()

    def test_an_unsupported_platform_registers_nothing_to_clear(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ledger.sys, "platform", "sunos5")
        _append_one(tmp_path)
        partition = next(tmp_path.glob("*.jsonl"))

        assert partition not in ledger.protected_paths()
        assert json.loads(partition.read_text().splitlines()[0])["ledger_meta"] == {
            "protection": "unsupported",
            "detail": "no append-only mechanism for 'sunos5'",
        }


class TestTheGuardFires:
    """The positive control: an uncleared partition IS reported as leaked."""

    def test_an_uncleared_protected_partition_is_reported(self, tmp_path: Path) -> None:
        basetemp = tmp_path / "pytest-0"
        (basetemp / "test_something0").mkdir(parents=True)
        _append_one(basetemp / "test_something0")
        partition = next((basetemp / "test_something0").glob("*.jsonl"))

        assert guard.leaked_protected_files(basetemp, cleared=frozenset()) == [str(partition)]

    def test_the_same_partition_is_not_reported_once_cleared(self, tmp_path: Path) -> None:
        basetemp = tmp_path / "pytest-0"
        (basetemp / "test_something0").mkdir(parents=True)
        _append_one(basetemp / "test_something0")
        partition = next((basetemp / "test_something0").glob("*.jsonl"))

        assert guard.leaked_protected_files(basetemp, cleared={partition}) == []

    def test_a_partition_written_by_a_child_process_is_reported(self, tmp_path: Path) -> None:
        """The case the in-process registry provably cannot see.

        A forked worker registers the protected path in its own copy of
        ``_PROTECTION_STATE``, which dies with it, so the parent's teardown has
        nothing to find -- yet the flag is set on disk. This is the leak that
        survived the per-test fixture and needed
        ``clear_append_only_protection_under``; without it,
        ``TestEightConcurrentAppenders`` left a file behind on every run.
        """
        if sys.platform == "win32":  # pragma: no cover - POSIX-only fork path
            pytest.skip("fork start method is POSIX-only")
        basetemp = tmp_path / "pytest-0"
        ledger_dir = basetemp / "test_concurrent0"
        ledger_dir.mkdir(parents=True)

        proc = multiprocessing.get_context("fork").Process(
            target=_append_one, args=(str(ledger_dir),)
        )
        proc.start()
        proc.join(timeout=30)
        assert proc.exitcode == 0

        partition = next(ledger_dir.glob("*.jsonl"))
        assert partition not in ledger.protected_paths()
        assert guard.leaked_protected_files(basetemp, ledger.cleared_paths()) == [str(partition)]

        assert ledger.clear_append_only_protection_under(ledger_dir) >= 1

        assert guard.leaked_protected_files(basetemp, ledger.cleared_paths()) == []


class TestTheGuardIsNotOverBroad:
    """Negative controls: the scan runs over every ``*.jsonl`` under the temp
    base, most of which are ordinary fixture data, so a false positive would
    red the suite for tests that have nothing to do with promptwitness."""

    @pytest.mark.parametrize(
        ("name", "content"),
        [
            ("empty.jsonl", ""),
            ("blank.jsonl", "\n\n"),
            ("not-json.jsonl", "this is not json\n"),
            ("json-but-a-list.jsonl", "[1, 2, 3]\n"),
            ("no-ledger-meta.jsonl", '{"v": 1, "seq": 1}\n'),
            ("ledger-meta-not-a-dict.jsonl", '{"v": 1, "ledger_meta": "applied"}\n'),
            ("protection-not-a-string.jsonl", '{"v": 1, "ledger_meta": {"protection": 1}}\n'),
            (
                "protection-unsupported.jsonl",
                '{"v": 1, "ledger_meta": {"protection": "unsupported"}}\n',
            ),
        ],
    )
    def test_unrelated_or_unprotected_files_are_not_reported(
        self, tmp_path: Path, name: str, content: str
    ) -> None:
        (tmp_path / name).write_text(content, encoding="utf-8")

        assert guard.leaked_protected_files(tmp_path, cleared=frozenset()) == []

    def test_undecodable_bytes_are_not_reported(self, tmp_path: Path) -> None:
        (tmp_path / "binary.jsonl").write_bytes(b"\xff\xfe\x00not utf-8\n")

        assert guard.leaked_protected_files(tmp_path, cleared=frozenset()) == []
