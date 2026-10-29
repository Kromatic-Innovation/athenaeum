# SPDX-License-Identifier: Apache-2.0
"""Persisting the `witnessed` source rewrite (issue athenaeum#2159).

A `witnessed` RECEIPT (issue athenaeum#2158) changes nothing the resolver
sees: source precedence keys on the memory's `source:` SCALAR, not on the
receipt ledger. These tests cover the write that closes that gap —
`intake.persist_witnessed_source` and its wiring into
`transcript_receipts.write_receipts_for_files`.

The four acceptance criteria exercised here:

* AC 4 — the write happens only under an ACQUIRED `RunLock`; an unacquired
  one RAISES; a caller-declared non-default, non-`user:` source is never
  rewritten.
* AC 6 — the `#turn<N>` value is the MEMORY's `originTurn`, never the witness
  ledger's own `turn`/`seq` counter. Every fixture here deliberately makes
  all three numbers different.
* The flag-off path is byte- AND mtime-identical (the
  `TestGateFlagOffByteIdentical` discipline).
* The rewrite is idempotent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from athenaeum.frontmatter_edit import DEFAULTED_INFERRED_SOURCE
from athenaeum.intake import (
    WitnessedSourceWriteNotLocked,
    discover_auto_memory_files,
    persist_witnessed_source,
)
from athenaeum.runlock import RunLock
from athenaeum.transcript_receipts import iter_receipts, write_receipts_for_files
from promptwitness import canon, ledger

# The three numbers that must never be confused (AC 6).
MEMORY_ORIGIN_TURN = 3  # what `#turn<N>` must follow
LEDGER_TURN = 41  # the witness ledger's own per-session counter
# The witness line's `seq` is assigned by the ledger; the first line is 1,
# so it differs from both of the above by construction.

CLAIM = "the winning claim"
UTTERANCE = "the user literally said: the winning claim, right here"


def _write_knowledge_root(tmp_path: Path) -> Path:
    knowledge_root = tmp_path / "knowledge"
    knowledge_root.mkdir(parents=True, exist_ok=True)
    (knowledge_root / "athenaeum.yaml").write_text(
        "recall:\n  extra_intake_roots:\n    - raw/auto-memory\n", encoding="utf-8"
    )
    (knowledge_root / "wiki").mkdir(parents=True, exist_ok=True)
    return knowledge_root


def _write_member(
    knowledge_root: Path,
    *,
    source: str | None = DEFAULTED_INFERRED_SOURCE,
    turn: int | None = MEMORY_ORIGIN_TURN,
    name: str = "feedback_a.md",
) -> Path:
    scope_dir = knowledge_root / "raw" / "auto-memory" / "scopeA"
    scope_dir.mkdir(parents=True, exist_ok=True)
    lines = ["---", "originSessionId: sess1"]
    if turn is not None:
        lines.append(f"originTurn: {turn}")
    if source is not None:
        lines.append(f"source: {source}")
    lines += ["description: a member with provenance to upgrade.", "---", ""]
    path = scope_dir / name
    path.write_text("\n".join(lines) + "\n" + CLAIM + "\n", encoding="utf-8")
    return path


def _write_transcript(projects_root: Path) -> Path:
    scope_dir = projects_root / "scopeA"
    scope_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "type": "user",
        "message": {"role": "user", "content": UTTERANCE},
        "promptId": "p1",
        "isSidechain": False,
        "origin": {"kind": "human"},
        "promptSource": "typed",
    }
    path = scope_dir / "sess1.jsonl"
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


def _append_witness_turn(witness_dir: Path, **overrides: object) -> ledger.WitnessLine:
    defaults: dict[str, object] = {
        "kind": "turn",
        "harness": "claude-code",
        "harness_version": "2.1.308",
        "hook_version": "promptwitness 0.1.0",
        "session": "sess1",
        # Deliberately NOT the memory's originTurn (AC 6).
        "turn": LEDGER_TURN,
        "prompt_id": "p1",
        "prompt_kind": "human",
        "prompt_digest": canon.digest(canon.normalize(UTTERANCE)),
        "raw_digest": canon.digest(UTTERANCE),
        "prompt_chars": len(UTTERANCE),
        "channel": "interactive",
        "resumed": False,
        "signals": {"entrypoint": "cli"},
    }
    defaults.update(overrides)
    return ledger.append_line(witness_dir, **defaults)  # type: ignore[arg-type]


def _run_intake(
    knowledge_root: Path,
    projects_root: Path,
    *,
    witness_enabled: bool = True,
    receipts_enabled: bool = True,
) -> object:
    files = discover_auto_memory_files(knowledge_root, projects_root=projects_root)
    assert len(files) == 1
    config = {
        "librarian": {
            "transcript_receipts_enabled": receipts_enabled,
            "witness_attestation_enabled": witness_enabled,
        }
    }
    lock = RunLock(knowledge_root)
    with lock:
        return write_receipts_for_files(
            knowledge_root / "wiki",
            files,
            config=config,
            projects_root=projects_root,
            lock=lock,
        )


# ---------------------------------------------------------------------------
# The seam: write_receipts_for_files -> intake.persist_witnessed_source
# ---------------------------------------------------------------------------


class TestIntakeSeam:
    def test_witnessed_hit_persists_all_four_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        witness_dir = tmp_path / "witness"
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(witness_dir))
        line = _append_witness_turn(witness_dir)
        knowledge_root = _write_knowledge_root(tmp_path)
        member = _write_member(knowledge_root)
        projects_root = tmp_path / "projects"
        _write_transcript(projects_root)

        stats = _run_intake(knowledge_root, projects_root)
        assert stats.witnessed == 1  # type: ignore[attr-defined]
        receipts = iter_receipts(knowledge_root / "wiki")
        assert [r.channel for r in receipts] == ["witnessed"]

        text = member.read_text(encoding="utf-8")
        assert "source: 'witnessed:sess1#turn3'" in text
        assert "source_type: witnessed" in text
        assert "source_ref: 'sess1#turn3'" in text
        assert f"witness_ref: '{line.ledger_id}:{line.seq}'" in text
        # Untouched bytes: every pre-existing key and the body survive.
        assert "originSessionId: sess1" in text
        assert "description: a member with provenance to upgrade." in text
        assert text.rstrip("\n").endswith(CLAIM)

    def test_turn_ref_follows_origin_turn_not_the_ledger_counter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC 6, end to end, on a fixture where all three numbers differ."""
        witness_dir = tmp_path / "witness"
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(witness_dir))
        line = _append_witness_turn(witness_dir)
        knowledge_root = _write_knowledge_root(tmp_path)
        member = _write_member(knowledge_root)
        _write_transcript(tmp_path / "projects")

        _run_intake(knowledge_root, tmp_path / "projects")
        text = member.read_text(encoding="utf-8")

        assert line.turn == LEDGER_TURN != MEMORY_ORIGIN_TURN
        assert line.seq != MEMORY_ORIGIN_TURN
        assert f"source: 'witnessed:sess1#turn{MEMORY_ORIGIN_TURN}'" in text
        assert f"#turn{LEDGER_TURN}" not in text
        assert f"#turn{line.seq}" not in text
        # The ledger's own numbering reaches the file ONLY as witness_ref's
        # `seq` segment.
        assert f"witness_ref: '{line.ledger_id}:{line.seq}'" in text

    def test_absent_origin_turn_writes_the_bare_session_ref(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        witness_dir = tmp_path / "witness"
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(witness_dir))
        _append_witness_turn(witness_dir)
        knowledge_root = _write_knowledge_root(tmp_path)
        member = _write_member(knowledge_root, turn=None)
        _write_transcript(tmp_path / "projects")

        _run_intake(knowledge_root, tmp_path / "projects")
        text = member.read_text(encoding="utf-8")
        assert "source: 'witnessed:sess1'" in text
        assert "#turn" not in text

    def test_user_source_is_upgraded_at_the_same_rank(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        witness_dir = tmp_path / "witness"
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(witness_dir))
        _append_witness_turn(witness_dir)
        knowledge_root = _write_knowledge_root(tmp_path)
        member = _write_member(knowledge_root, source="user:sess1#turn3")
        _write_transcript(tmp_path / "projects")

        _run_intake(knowledge_root, tmp_path / "projects")
        text = member.read_text(encoding="utf-8")
        assert "source: 'witnessed:sess1#turn3'" in text
        assert "source: user:" not in text

    @pytest.mark.parametrize(
        "declared", ["api:apollo", "linkedin:tkromer", "claude:tier3-write", None]
    )
    def test_caller_declared_source_is_never_rewritten(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        declared: str | None,
    ) -> None:
        """AC 4's negative half — including an ABSENT ``source`` key, which is
        also not a claim this pass gets to attribute.
        """
        witness_dir = tmp_path / "witness"
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(witness_dir))
        _append_witness_turn(witness_dir)  # a genuine HIT is available
        knowledge_root = _write_knowledge_root(tmp_path)
        member = _write_member(knowledge_root, source=declared)
        _write_transcript(tmp_path / "projects")
        before_bytes = member.read_bytes()
        before_mtime = member.stat().st_mtime_ns

        stats = _run_intake(knowledge_root, tmp_path / "projects")
        # The receipt still upgrades — the ledger line really does attest.
        # Only the FILE rewrite is withheld.
        assert stats.witnessed == 1  # type: ignore[attr-defined]
        assert member.read_bytes() == before_bytes
        assert member.stat().st_mtime_ns == before_mtime

    def test_flag_off_leaves_the_member_byte_and_mtime_identical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        witness_dir = tmp_path / "witness"
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(witness_dir))
        _append_witness_turn(witness_dir)  # would hit if the flag were on
        knowledge_root = _write_knowledge_root(tmp_path)
        member = _write_member(knowledge_root)
        _write_transcript(tmp_path / "projects")
        before_bytes = member.read_bytes()
        before_mtime = member.stat().st_mtime_ns

        stats = _run_intake(
            knowledge_root, tmp_path / "projects", witness_enabled=False
        )
        assert stats.witnessed == 0  # type: ignore[attr-defined]
        assert [r.channel for r in iter_receipts(knowledge_root / "wiki")] == [
            "user-stated"
        ]
        assert member.read_bytes() == before_bytes
        assert member.stat().st_mtime_ns == before_mtime

    def test_witness_miss_leaves_the_member_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        witness_dir = tmp_path / "witness"
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(witness_dir))
        _append_witness_turn(witness_dir, channel="headless")  # a MISS
        knowledge_root = _write_knowledge_root(tmp_path)
        member = _write_member(knowledge_root)
        _write_transcript(tmp_path / "projects")
        before_bytes = member.read_bytes()
        before_mtime = member.stat().st_mtime_ns

        stats = _run_intake(knowledge_root, tmp_path / "projects")
        assert stats.witnessed == 0  # type: ignore[attr-defined]
        assert member.read_bytes() == before_bytes
        assert member.stat().st_mtime_ns == before_mtime

    def test_second_run_is_byte_and_mtime_identical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        witness_dir = tmp_path / "witness"
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(witness_dir))
        _append_witness_turn(witness_dir)
        knowledge_root = _write_knowledge_root(tmp_path)
        member = _write_member(knowledge_root)
        _write_transcript(tmp_path / "projects")

        _run_intake(knowledge_root, tmp_path / "projects")
        after_first_bytes = member.read_bytes()
        after_first_mtime = member.stat().st_mtime_ns

        _run_intake(knowledge_root, tmp_path / "projects")
        assert member.read_bytes() == after_first_bytes
        assert member.stat().st_mtime_ns == after_first_mtime


# ---------------------------------------------------------------------------
# intake.persist_witnessed_source — the writer's own contract
# ---------------------------------------------------------------------------


class TestPersistWitnessedSource:
    def test_unacquired_lock_raises_before_any_no_op_check(
        self, tmp_path: Path
    ) -> None:
        """AC 4: the lock check is FIRST — the input below would otherwise be
        a no-op (the file does not even exist), so a lock error here can only
        come from the check preceding every no-op condition.
        """
        with pytest.raises(WitnessedSourceWriteNotLocked):
            persist_witnessed_source(
                tmp_path / "nonexistent.md",
                session_id="sess1",
                origin_turn=MEMORY_ORIGIN_TURN,
                witness_ref="led-a:1",
                lock=RunLock(tmp_path),
            )

    def test_acquired_lock_rewrites_and_returns_true(self, tmp_path: Path) -> None:
        knowledge_root = _write_knowledge_root(tmp_path)
        member = _write_member(knowledge_root)
        with RunLock(knowledge_root) as lock:
            changed = persist_witnessed_source(
                member,
                session_id="sess1",
                origin_turn=MEMORY_ORIGIN_TURN,
                witness_ref="led-a:1",
                lock=lock,
            )
        assert changed is True
        assert "source: 'witnessed:sess1#turn3'" in member.read_text(encoding="utf-8")

    def test_empty_session_id_is_a_no_op_not_a_blank_ref(self, tmp_path: Path) -> None:
        knowledge_root = _write_knowledge_root(tmp_path)
        member = _write_member(knowledge_root)
        before = member.read_bytes()
        with RunLock(knowledge_root) as lock:
            changed = persist_witnessed_source(
                member,
                session_id="",
                origin_turn=MEMORY_ORIGIN_TURN,
                witness_ref="led-a:1",
                lock=lock,
            )
        assert changed is False
        assert member.read_bytes() == before

    def test_file_without_frontmatter_is_never_given_one(self, tmp_path: Path) -> None:
        knowledge_root = _write_knowledge_root(tmp_path)
        member = knowledge_root / "raw" / "auto-memory" / "scopeA" / "feedback_b.md"
        member.parent.mkdir(parents=True, exist_ok=True)
        member.write_text("no frontmatter here\n", encoding="utf-8")
        with RunLock(knowledge_root) as lock:
            changed = persist_witnessed_source(
                member,
                session_id="sess1",
                origin_turn=MEMORY_ORIGIN_TURN,
                witness_ref="led-a:1",
                lock=lock,
            )
        assert changed is False
        assert member.read_text(encoding="utf-8") == "no frontmatter here\n"

    def test_unreadable_file_is_tolerated(self, tmp_path: Path) -> None:
        knowledge_root = _write_knowledge_root(tmp_path)
        with RunLock(knowledge_root) as lock:
            assert (
                persist_witnessed_source(
                    knowledge_root / "absent.md",
                    session_id="sess1",
                    origin_turn=MEMORY_ORIGIN_TURN,
                    witness_ref="led-a:1",
                    lock=lock,
                )
                is False
            )
