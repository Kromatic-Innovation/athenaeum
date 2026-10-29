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


# ---------------------------------------------------------------------------
# repair --backfill-sources — the SAME rewrite, from the same composer
# ---------------------------------------------------------------------------


def _write_backfill_memory(
    auto_memory_root: Path,
    *,
    filename: str = "feedback_a.md",
    source: str = DEFAULTED_INFERRED_SOURCE,
    turn: int | None = MEMORY_ORIGIN_TURN,
    extra_fm: str = "",
) -> Path:
    scope_dir = auto_memory_root / "scopeA"
    scope_dir.mkdir(parents=True, exist_ok=True)
    lines = ["---", "type: feedback", "originSessionId: sess1"]
    if turn is not None:
        lines.append(f"originTurn: {turn}")
    lines.append(f"source: {source}")
    if extra_fm:
        lines.append(extra_fm.rstrip("\n"))
    lines += ["---", ""]
    path = scope_dir / filename
    path.write_text("\n".join(lines) + "\n" + CLAIM + "\n", encoding="utf-8")
    return path


def _backfill(
    auto_memory_root: Path,
    projects_root: Path,
    *,
    apply: bool = True,
    witness_enabled: bool = True,
    limit: int | None = None,
):
    from athenaeum.repair import backfill_sources

    return backfill_sources(
        auto_memory_root,
        projects_root=projects_root,
        apply=apply,
        limit=limit,
        config={"librarian": {"witness_attestation_enabled": witness_enabled}},
    )


class TestRepairBackfillWitnessedBranch:
    def test_hit_writes_the_witnessed_quartet(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        witness_dir = tmp_path / "witness"
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(witness_dir))
        line = _append_witness_turn(witness_dir)
        auto_memory_root = tmp_path / "knowledge" / "raw" / "auto-memory"
        member = _write_backfill_memory(auto_memory_root)
        _write_transcript(tmp_path / "projects")

        report = _backfill(auto_memory_root, tmp_path / "projects")
        assert report.witnessed == 1
        assert report.user_stated == 0
        assert report.agent_observed == 0
        assert report.errors == []
        assert [s for _p, s in report.changes] == [
            "witnessed: source -> witnessed:sess1#turn3"
        ]

        text = member.read_text(encoding="utf-8")
        assert "source: 'witnessed:sess1#turn3'" in text
        assert "source_type: witnessed" in text
        assert "source_ref: 'sess1#turn3'" in text
        assert f"witness_ref: '{line.ledger_id}:{line.seq}'" in text
        # AC 6, on the repair path too: all three numbers differ.
        assert f"#turn{LEDGER_TURN}" not in text
        assert f"#turn{line.seq}" not in text

    def test_rewrite_is_identical_to_intakes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC 5 — the two writers produce the SAME bytes.

        Both paths run against identical member files and the same witness
        line, then the two results are compared directly. This is the
        assertion the shared `witnessed_source_fields` composer exists to
        make true by construction; a future re-transcription on either side
        fails here.
        """
        witness_dir = tmp_path / "witness"
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(witness_dir))
        line = _append_witness_turn(witness_dir)
        projects_root = tmp_path / "projects"
        _write_transcript(projects_root)

        # --- repair --backfill-sources --apply ---
        repair_root = tmp_path / "repair-knowledge"
        repair_member = _write_backfill_memory(repair_root / "raw" / "auto-memory")
        report = _backfill(repair_root / "raw" / "auto-memory", projects_root)
        assert report.witnessed == 1

        # --- intake's persist, on a byte-identical member ---
        intake_root = tmp_path / "intake-knowledge"
        intake_root.mkdir(parents=True, exist_ok=True)
        intake_member = _write_backfill_memory(intake_root / "raw" / "auto-memory")
        with RunLock(intake_root) as lock:
            assert (
                persist_witnessed_source(
                    intake_member,
                    session_id="sess1",
                    origin_turn=MEMORY_ORIGIN_TURN,
                    witness_ref=f"{line.ledger_id}:{line.seq}",
                    lock=lock,
                )
                is True
            )

        assert repair_member.read_text(encoding="utf-8") == intake_member.read_text(
            encoding="utf-8"
        )

    def test_flag_off_still_writes_the_user_trio(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        witness_dir = tmp_path / "witness"
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(witness_dir))
        _append_witness_turn(witness_dir)  # would hit if the flag were on
        auto_memory_root = tmp_path / "knowledge" / "raw" / "auto-memory"
        member = _write_backfill_memory(auto_memory_root)
        _write_transcript(tmp_path / "projects")

        report = _backfill(
            auto_memory_root, tmp_path / "projects", witness_enabled=False
        )
        assert report.witnessed == 0
        assert report.user_stated == 1
        text = member.read_text(encoding="utf-8")
        assert "source: 'user:sess1#turn3'" in text
        assert "source_type: user-stated" in text
        assert "witness_ref" not in text

    def test_witness_miss_still_writes_the_user_trio(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        witness_dir = tmp_path / "witness"
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(witness_dir))
        _append_witness_turn(witness_dir, channel="headless")  # a MISS
        auto_memory_root = tmp_path / "knowledge" / "raw" / "auto-memory"
        member = _write_backfill_memory(auto_memory_root)
        _write_transcript(tmp_path / "projects")

        report = _backfill(auto_memory_root, tmp_path / "projects")
        assert report.witnessed == 0
        assert report.user_stated == 1
        assert "source: 'user:sess1#turn3'" in member.read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        "source,extra_fm",
        [
            ("user:sess1#turn3", ""),  # not the DEFAULTED scalar
            ("api:apollo", ""),
            ("claude:inferred-but-not-exactly", ""),
            (DEFAULTED_INFERRED_SOURCE, "inferred_verified: true"),
        ],
    )
    def test_candidate_selection_is_unchanged(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        source: str,
        extra_fm: str,
    ) -> None:
        """AC 5's second half: adding the witnessed branch must not widen
        which memories the pass touches. Only the EXACTLY-defaulted
        `claude:inferred` scalar is a candidate, and `inferred_verified`
        still short-circuits — even with a witness line that would hit.
        """
        witness_dir = tmp_path / "witness"
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(witness_dir))
        _append_witness_turn(witness_dir)
        auto_memory_root = tmp_path / "knowledge" / "raw" / "auto-memory"
        member = _write_backfill_memory(
            auto_memory_root, source=source, extra_fm=extra_fm
        )
        _write_transcript(tmp_path / "projects")
        before = member.read_bytes()

        report = _backfill(auto_memory_root, tmp_path / "projects")
        assert report.files_scanned == 1
        assert report.witnessed == 0
        assert report.user_stated == 0
        assert report.changes == []
        assert member.read_bytes() == before

    def test_dry_run_reports_without_writing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        witness_dir = tmp_path / "witness"
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(witness_dir))
        _append_witness_turn(witness_dir)
        auto_memory_root = tmp_path / "knowledge" / "raw" / "auto-memory"
        member = _write_backfill_memory(auto_memory_root)
        _write_transcript(tmp_path / "projects")
        before = member.read_bytes()

        report = _backfill(auto_memory_root, tmp_path / "projects", apply=False)
        assert report.witnessed == 1
        assert member.read_bytes() == before

    def test_second_apply_is_a_no_op(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        witness_dir = tmp_path / "witness"
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(witness_dir))
        _append_witness_turn(witness_dir)
        auto_memory_root = tmp_path / "knowledge" / "raw" / "auto-memory"
        member = _write_backfill_memory(auto_memory_root)
        _write_transcript(tmp_path / "projects")

        _backfill(auto_memory_root, tmp_path / "projects")
        after_first = member.read_bytes()
        second = _backfill(auto_memory_root, tmp_path / "projects")
        # The rewritten scalar is no longer the DEFAULTED one, so the memory
        # is not even a candidate on the second pass.
        assert second.witnessed == 0
        assert member.read_bytes() == after_first

    def test_attestation_index_is_built_once_per_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The athenaeum#2158 read-budget discipline: one index for the whole
        pass, never one per file.
        """
        from athenaeum import witness_attestation

        witness_dir = tmp_path / "witness"
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(witness_dir))
        _append_witness_turn(witness_dir)
        auto_memory_root = tmp_path / "knowledge" / "raw" / "auto-memory"
        for i in range(4):
            _write_backfill_memory(auto_memory_root, filename=f"feedback_{i}.md")
        _write_transcript(tmp_path / "projects")

        calls: list[Path] = []
        real_load = witness_attestation.AttestationIndex.load

        def counting_load(witness_dir_arg: Path) -> object:
            calls.append(witness_dir_arg)
            return real_load(witness_dir_arg)

        monkeypatch.setattr(
            witness_attestation.AttestationIndex, "load", staticmethod(counting_load)
        )
        report = _backfill(auto_memory_root, tmp_path / "projects")
        assert report.files_scanned == 4
        assert report.witnessed == 4
        assert len(calls) == 1, f"index rebuilt per file: {len(calls)} loads"

    def test_flag_off_never_resolves_the_witness_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Zero added I/O on the flag-off path (the `ReceiptRun` contract)."""
        from athenaeum import witness_attestation

        auto_memory_root = tmp_path / "knowledge" / "raw" / "auto-memory"
        _write_backfill_memory(auto_memory_root)
        _write_transcript(tmp_path / "projects")

        def boom(*_a: object, **_k: object) -> object:
            raise AssertionError("the witness ledger must not be touched")

        monkeypatch.setattr(
            witness_attestation.AttestationIndex, "load", staticmethod(boom)
        )
        monkeypatch.setattr(witness_attestation, "resolve_default_witness_dir", boom)
        report = _backfill(
            auto_memory_root, tmp_path / "projects", witness_enabled=False
        )
        assert report.user_stated == 1
