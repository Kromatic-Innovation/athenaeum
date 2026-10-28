# SPDX-License-Identifier: Apache-2.0
"""Tests for promptwitness.ledger (athenaeum#2147).

Every path builds filesystem paths from ``tmp_path`` and monkeypatched env
vars only -- never a hardcoded machine-specific home-directory literal (the
public-safe-lint gate rejects that shape even in test fixtures).
"""

from __future__ import annotations

import json
import multiprocessing
import os
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

from promptwitness import canon, ledger

# ---------------------------------------------------------------------------
# resolve_ledger_dir
# ---------------------------------------------------------------------------


class TestResolveLedgerDir:
    def test_override_argument_wins(self, tmp_path: Path) -> None:
        got = ledger.resolve_ledger_dir("claude-code", override=tmp_path / "explicit")
        assert got == tmp_path / "explicit"

    def test_promptwitness_dir_env_wins_over_xdg(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(tmp_path / "from-env"))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
        got = ledger.resolve_ledger_dir("claude-code")
        assert got == tmp_path / "from-env"

    def test_xdg_state_home_used_when_no_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PROMPTWITNESS_DIR", raising=False)
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
        got = ledger.resolve_ledger_dir("claude-code")
        assert got == tmp_path / "xdg" / "promptwitness" / "claude-code"

    def test_falls_back_to_home_local_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PROMPTWITNESS_DIR", raising=False)
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home-dir"))
        got = ledger.resolve_ledger_dir("codex")
        assert got == tmp_path / "home-dir" / ".local" / "state" / "promptwitness" / "codex"


# ---------------------------------------------------------------------------
# append_line basics and kind validation
# ---------------------------------------------------------------------------


def _append_turn(d: Path, **kwargs: object) -> ledger.WitnessLine:
    defaults: dict[str, object] = {
        "kind": "turn",
        "harness": "claude-code",
        "harness_version": "2.1.308",
        "hook_version": "promptwitness 0.1.0",
        "session": "s1",
        "turn": 0,
        "prompt_id": "p0",
        "prompt_kind": "human",
        "prompt_digest": canon.digest(canon.normalize("hi")),
        "raw_digest": canon.digest("hi"),
        "prompt_chars": 2,
        "channel": "interactive",
        "resumed": False,
        "signals": {"entrypoint": "cli"},
    }
    defaults.update(kwargs)
    return ledger.append_line(d, **defaults)  # type: ignore[arg-type]


class TestAppendLineValidation:
    def test_rejects_unknown_kind(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="unknown ledger line kind"):
            ledger.append_line(
                tmp_path,
                kind="bogus",
                harness="h",
                harness_version="1",
                hook_version="v",
            )

    def test_rejects_turn_fields_on_non_turn_kind(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="turn-only fields"):
            ledger.append_line(
                tmp_path,
                kind="gap",
                harness="h",
                harness_version="1",
                hook_version="v",
                session="s1",  # turn-only field on a gap line
                reason="torn-tail",
            )

    def test_rejects_gap_field_on_turn_kind(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="gap-only fields"):
            _append_turn(tmp_path, reason="nope")

    def test_extra_cannot_clobber_reserved_fields(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="collide with reserved fields"):
            _append_turn(tmp_path, extra={"seq": 999})


class TestAppendLineBasics:
    def test_first_line_is_seq_one_and_genesis_meta(self, tmp_path: Path) -> None:
        line = _append_turn(tmp_path)
        assert line.seq == 1
        assert line.ledger_meta is not None
        assert "protection" in line.ledger_meta

    def test_second_line_chains_onto_first(self, tmp_path: Path) -> None:
        l1 = _append_turn(tmp_path, prompt_id="p0")
        l2 = _append_turn(tmp_path, prompt_id="p1", turn=1)
        assert l2.seq == 2
        assert l2.prev == l1.line_hash()
        assert l2.ledger_meta is None

    def test_turn_only_fields_omitted_on_gap_line(self, tmp_path: Path) -> None:
        line = ledger.append_line(
            tmp_path, kind="gap", harness="h", harness_version="1", hook_version="v", reason="x"
        )
        raw = line.to_dict()
        assert "session" not in raw
        assert "prompt_digest" not in raw
        assert raw["reason"] == "x"

    def test_no_key_provider_means_strength_none_and_no_sig(self, tmp_path: Path) -> None:
        line = _append_turn(tmp_path)
        assert line.key.strength == "none"
        assert line.sig is None
        assert "sig" not in line.to_dict()

    def test_ledger_id_is_stable_across_appends(self, tmp_path: Path) -> None:
        l1 = _append_turn(tmp_path, prompt_id="p0")
        l2 = _append_turn(tmp_path, prompt_id="p1", turn=1)
        assert l1.ledger_id == l2.ledger_id
        assert l1.ledger_id.startswith("pw-")

    def test_monthly_partition_file_name(self, tmp_path: Path) -> None:
        _append_turn(tmp_path, ts="2026-03-15T00:00:00.000Z")
        assert ledger.list_partitions(tmp_path) == ["2026-03.jsonl"]

    def test_new_month_chains_onto_previous_months_last_line(self, tmp_path: Path) -> None:
        jan_line = _append_turn(tmp_path, ts="2026-01-31T23:59:59.000Z", prompt_id="jan")
        feb_line = _append_turn(tmp_path, ts="2026-02-01T00:00:00.000Z", prompt_id="feb", turn=1)
        assert ledger.list_partitions(tmp_path) == ["2026-01.jsonl", "2026-02.jsonl"]
        assert feb_line.seq == jan_line.seq + 1
        assert feb_line.prev == jan_line.line_hash()
        v = ledger.verify_chain(tmp_path)
        assert v.ok
        assert v.lines_checked == 2


# ---------------------------------------------------------------------------
# Chain round-trip: mutation, deletion, reorder
# ---------------------------------------------------------------------------


def _read_partition_lines(path: Path) -> list[bytes]:
    return [ln for ln in path.read_bytes().split(b"\n") if ln.strip()]


def _write_partition_lines(path: Path, lines: list[bytes]) -> None:
    path.write_bytes(b"\n".join(lines) + b"\n")


def _single_partition(d: Path) -> Path:
    names = ledger.list_partitions(d)
    assert len(names) == 1
    return d / names[0]


class TestChainRoundTrip:
    def test_clean_chain_verifies(self, tmp_path: Path) -> None:
        for i in range(4):
            _append_turn(tmp_path, prompt_id=f"p{i}", turn=i)
        v = ledger.verify_chain(tmp_path)
        assert v.ok
        assert v.first_break_seq is None
        assert v.lines_checked == 4

    def test_one_byte_mutation_detected_at_next_seq(self, tmp_path: Path) -> None:
        for i in range(4):
            _append_turn(tmp_path, prompt_id=f"p{i}", turn=i)
        partition = _single_partition(tmp_path)
        lines = _read_partition_lines(partition)
        obj = json.loads(lines[1])  # seq == 2
        obj["prompt_chars"] = 999999  # one-field mutation after the fact
        lines[1] = canon.canonical_json(obj)
        _write_partition_lines(partition, lines)

        v = ledger.verify_chain(tmp_path)
        assert not v.ok
        assert v.first_break_seq == 3  # mutating line 2 breaks line 3's prev check

    def test_deleted_line_detected_at_the_seq_following_the_gap(self, tmp_path: Path) -> None:
        for i in range(4):
            _append_turn(tmp_path, prompt_id=f"p{i}", turn=i)
        partition = _single_partition(tmp_path)
        lines = _read_partition_lines(partition)
        del lines[1]  # physically remove seq == 2
        _write_partition_lines(partition, lines)

        v = ledger.verify_chain(tmp_path)
        assert not v.ok
        assert v.first_break_seq == 3

    def test_reordered_pair_detected_at_the_earlier_physical_position(
        self, tmp_path: Path
    ) -> None:
        for i in range(4):
            _append_turn(tmp_path, prompt_id=f"p{i}", turn=i)
        partition = _single_partition(tmp_path)
        lines = _read_partition_lines(partition)
        lines[1], lines[2] = lines[2], lines[1]  # swap seq 2 and seq 3 physically
        _write_partition_lines(partition, lines)

        v = ledger.verify_chain(tmp_path)
        assert not v.ok
        # seq 3 now appears BEFORE seq 2 in file order, and is the first line
        # whose prev no longer matches what physically precedes it.
        assert v.first_break_seq == 3


# ---------------------------------------------------------------------------
# Torn tail -> gap line, never a fork
# ---------------------------------------------------------------------------


class TestTornTail:
    def test_torn_tail_repaired_with_a_gap_line_not_a_fork(self, tmp_path: Path) -> None:
        l1 = _append_turn(tmp_path, prompt_id="p0")
        partition = _single_partition(tmp_path)
        with open(partition, "ab") as fh:
            fh.write(b'{"v":1,"seq":2,"kind":"turn"')  # torn: no closing brace, no newline

        assert not partition.read_bytes().endswith(b"\n")

        l2 = _append_turn(tmp_path, prompt_id="p1", turn=1)

        assert partition.read_bytes().endswith(b"\n")
        kinds = [line.kind for line in ledger.iter_lines(tmp_path)]
        assert kinds == ["turn", "gap", "turn"]
        assert l2.seq == 3
        v = ledger.verify_chain(tmp_path)
        assert v.ok
        assert v.lines_checked == 3
        assert l1.seq == 1

    def test_torn_tail_as_the_very_first_write_still_gets_genesis_meta(
        self, tmp_path: Path
    ) -> None:
        tmp_path.mkdir(exist_ok=True)
        partition = tmp_path / "2026-10.jsonl"
        partition.write_bytes(b'{"v":1,"seq":1,"kind":"turn"')  # torn from the very first write

        _append_turn(tmp_path, ts="2026-10-05T00:00:00.000Z", prompt_id="p0")

        lines = list(ledger.iter_lines(tmp_path))
        assert [line.kind for line in lines] == ["gap", "turn"]
        assert lines[0].seq == 1
        assert lines[0].ledger_meta is not None
        v = ledger.verify_chain(tmp_path)
        assert v.ok


# ---------------------------------------------------------------------------
# Eight concurrent appenders (POSIX)
# ---------------------------------------------------------------------------


def _concurrent_worker(ledger_dir: str, i: int) -> None:
    ledger.append_line(
        ledger_dir,
        kind="turn",
        harness="claude-code",
        harness_version="1",
        hook_version="promptwitness 0.1.0",
        session=f"s{i}",
        turn=0,
        prompt_id=f"p{i}",
        prompt_kind="human",
        channel="interactive",
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only concurrency test (AC)")
class TestEightConcurrentAppenders:
    def test_produces_a_strictly_linear_contiguous_chain(self, tmp_path: Path) -> None:
        n = 8
        procs = [
            multiprocessing.get_context("fork").Process(
                target=_concurrent_worker, args=(str(tmp_path), i)
            )
            for i in range(n)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=30)
        for p in procs:
            assert p.exitcode == 0, f"appender process exited {p.exitcode}"

        v = ledger.verify_chain(tmp_path)
        assert v.ok
        assert v.lines_checked == n
        seqs = sorted(line.seq for line in ledger.iter_lines(tmp_path))
        assert seqs == list(range(1, n + 1))


# ---------------------------------------------------------------------------
# Windows lock path (mocked -- no real Windows needed)
# ---------------------------------------------------------------------------


class TestWindowsLockPathMocked:
    def test_lock_and_unlock_call_msvcrt_locking(self, tmp_path: Path) -> None:
        calls: list[tuple[int, int, int]] = []
        fake_msvcrt = types.SimpleNamespace(
            LK_LOCK=1,
            LK_UNLCK=0,
            locking=lambda fd, mode, nbytes: calls.append((fd, mode, nbytes)),
        )
        real_msvcrt = sys.modules.get("msvcrt")
        sys.modules["msvcrt"] = fake_msvcrt
        try:
            lock_path = tmp_path / "ledger.lock"
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
            try:
                with mock.patch.object(ledger.sys, "platform", "win32"):
                    ledger._lock_exclusive(fd)
                    ledger._unlock_exclusive(fd)
            finally:
                os.close(fd)
        finally:
            if real_msvcrt is not None:
                sys.modules["msvcrt"] = real_msvcrt
            else:
                sys.modules.pop("msvcrt", None)

        assert calls == [(fd, 1, 1), (fd, 0, 1)]


# ---------------------------------------------------------------------------
# No prompt text, ever
# ---------------------------------------------------------------------------


class TestNoPromptTextSchema:
    def test_real_turn_line_has_no_oversized_fields(self, tmp_path: Path) -> None:
        line = _append_turn(tmp_path)
        assert ledger.find_oversized_fields(line.to_dict()) == []

    def test_deliberately_long_field_is_caught(self) -> None:
        leaked = {
            "kind": "turn",
            "signals": {"leak": "x" * 500},
        }
        hits = ledger.find_oversized_fields(leaked)
        assert hits == ["signals.leak"]

    def test_label_fields_are_exempt_even_when_long(self) -> None:
        # harness_version IS an enumerated label field -- exempt even past
        # digest length (a version string is short in practice, but the
        # exemption is by field identity, not by observed length).
        line = {"kind": "turn", "harness_version": "y" * 100}
        assert ledger.find_oversized_fields(line) == []

    def test_non_label_field_past_digest_length_is_flagged(self) -> None:
        line = {"kind": "turn", "some_future_field": "y" * 100}
        assert ledger.find_oversized_fields(line) == ["some_future_field"]

    def test_digest_length_strings_are_not_flagged(self) -> None:
        line = {"prompt_digest": canon.digest("some text"), "raw_digest": canon.digest("x")}
        assert ledger.find_oversized_fields(line) == []

    def test_oversized_string_inside_segments_list_is_caught(self) -> None:
        line = {"segments": [{"kind": "typed", "digest": "z" * 200}]}
        assert ledger.find_oversized_fields(line) == ["segments.digest"]


# ---------------------------------------------------------------------------
# LedgerIndex
# ---------------------------------------------------------------------------


class TestLedgerIndex:
    def test_by_prompt_id(self, tmp_path: Path) -> None:
        _append_turn(tmp_path, prompt_id="p0", turn=0)
        _append_turn(tmp_path, prompt_id="p1", turn=1)
        idx = ledger.LedgerIndex.build(tmp_path)
        assert idx.by_prompt_id("p1") is not None
        assert idx.by_prompt_id("p1").prompt_id == "p1"  # type: ignore[union-attr]
        assert idx.by_prompt_id("missing") is None

    def test_by_session_digest(self, tmp_path: Path) -> None:
        digest = canon.digest(canon.normalize("same text"))
        _append_turn(tmp_path, prompt_id="p0", turn=0, session="sA", prompt_digest=digest)
        hits = ledger.LedgerIndex.build(tmp_path).by_session_digest("sA", digest)
        assert len(hits) == 1
        assert hits[0].prompt_id == "p0"

    def test_human_filter_rejects_headless_and_bridge(self, tmp_path: Path) -> None:
        _append_turn(
            tmp_path, prompt_id="human1", turn=0, channel="interactive", prompt_kind="human"
        )
        _append_turn(
            tmp_path, prompt_id="headless1", turn=1, channel="headless", prompt_kind="human"
        )
        _append_turn(
            tmp_path, prompt_id="bridge1", turn=2, channel="bridge", prompt_kind="human"
        )
        _append_turn(
            tmp_path,
            prompt_id="tasknotif1",
            turn=3,
            channel="interactive",
            prompt_kind="task-notification",
        )
        idx = ledger.LedgerIndex.build(tmp_path)
        human_ids = {line.prompt_id for line in idx.human_lines()}
        assert human_ids == {"human1"}

    def test_slash_command_counts_as_human(self, tmp_path: Path) -> None:
        _append_turn(
            tmp_path, prompt_id="slash1", turn=0, channel="interactive", prompt_kind="slash-command"
        )
        idx = ledger.LedgerIndex.build(tmp_path)
        assert {line.prompt_id for line in idx.human_lines()} == {"slash1"}

    def test_non_turn_lines_are_not_indexed(self, tmp_path: Path) -> None:
        _append_turn(tmp_path, prompt_id="p0", turn=0)
        ledger.append_line(
            tmp_path, kind="gap", harness="h", harness_version="1", hook_version="v", reason="x"
        )
        idx = ledger.LedgerIndex.build(tmp_path)
        assert idx.by_prompt_id("p0") is not None
        assert len(idx.human_lines()) == 1


# ---------------------------------------------------------------------------
# Signing seam (key_provider)
# ---------------------------------------------------------------------------


class _FakeKeyProvider:
    def __init__(self, raise_unavailable: bool = False) -> None:
        self._raise = raise_unavailable

    def key_id(self) -> str:
        return "ed25519:deadbeefdeadbeef"

    def algorithm(self) -> str:
        return "ed25519"

    def strength(self) -> str:
        return "software-file"

    def presence_required(self) -> bool:
        return False

    def sign(self, payload: bytes) -> bytes:
        if self._raise:
            raise ledger.KeyUnavailable("no key configured")
        return b"FAKESIG:" + payload[:8]


class TestSigningSeam:
    def test_signed_line_carries_key_and_sig(self, tmp_path: Path) -> None:
        line = _append_turn(tmp_path, key_provider=_FakeKeyProvider())
        assert line.key.strength == "software-file"
        assert line.key.id == "ed25519:deadbeefdeadbeef"
        assert line.sig is not None

    def test_unavailable_key_degrades_to_unsigned_without_raising(self, tmp_path: Path) -> None:
        line = _append_turn(tmp_path, key_provider=_FakeKeyProvider(raise_unavailable=True))
        assert line.key.strength == "none"
        assert line.key.error == "KeyUnavailable"
        assert line.sig is None

    def test_sig_covers_prev(self, tmp_path: Path) -> None:
        """Changing ``prev`` would change what was signed -- proves the
        signed payload actually includes ``prev``, not just the kind-
        specific fields."""
        provider = _FakeKeyProvider()
        line = _append_turn(tmp_path, key_provider=provider)
        payload_without_sig = {k: v for k, v in line.to_dict().items() if k != "sig"}
        assert payload_without_sig["prev"] == line.prev
        expected_sig_bytes = b"FAKESIG:" + canon.canonical_json(payload_without_sig)[:8]
        import base64

        assert line.sig == base64.b64encode(expected_sig_bytes).decode("ascii")


# ---------------------------------------------------------------------------
# WitnessLine / Segment / KeyInfo round trips
# ---------------------------------------------------------------------------


class TestWitnessLineRoundTrip:
    def test_to_dict_from_dict_round_trip(self, tmp_path: Path) -> None:
        seg = ledger.Segment(kind="typed", start=0, end=5, digest=canon.digest("hello"))
        line = _append_turn(tmp_path, segments=[seg])
        roundtripped = ledger.WitnessLine.from_dict(line.to_dict())
        assert roundtripped.seq == line.seq
        assert roundtripped.segments[0].kind == "typed"
        assert roundtripped.segments[0].digest == seg.digest

    def test_unknown_future_fields_preserved_in_extra(self) -> None:
        raw = {
            "v": 1,
            "ledger_id": "pw-aaaa",
            "seq": 5,
            "kind": "anchor",
            "ts": "2026-10-28T00:00:00.000Z",
            "harness": "h",
            "harness_version": "1",
            "hook_version": "v",
            "prev": "sha256:" + "0" * 64,
            "key": {"id": None, "alg": None, "strength": "none", "presence": False},
            "subject": "witness:pw-aaaa:4",
            "attestation": {"source": "local-clock", "status": "complete"},
        }
        line = ledger.WitnessLine.from_dict(raw)
        assert line.extra == {
            "subject": "witness:pw-aaaa:4",
            "attestation": {"source": "local-clock", "status": "complete"},
        }
        assert line.to_dict()["subject"] == "witness:pw-aaaa:4"

    def test_line_hash_is_stable_for_a_freshly_built_line(self, tmp_path: Path) -> None:
        line = _append_turn(tmp_path)
        expected = "sha256:" + __import__("hashlib").sha256(
            canon.canonical_json(line.to_dict())
        ).hexdigest()
        assert line.line_hash() == expected
