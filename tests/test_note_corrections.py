# SPDX-License-Identifier: Apache-2.0
"""`athenaeum correct-notes` (issue athenaeum#1976).

Everything here builds its own wiki fixture under ``tmp_path`` — nothing in
this module reads or writes ``~/knowledge``, and no fixture path is ever a
literal outside ``tmp_path`` (public-safe-lint).

The source fixture page, ``src00001-jamie.md``, carries five Notes bullets
built so one batch exercises every AC6 scenario without a special case:

1. ``Jamie switched teams...``    [^1]  — unique label: drop removes it
   (the orphaned-footnote half of AC2).
2. ``Someone else entirely...``         — uncited: move carries a transport
   footnote (AC2's uncited half).
3. ``Jamie also closed the Beta deal.`` [^2]  — cited, unique label: move
   carries its own footnote across (AC2's cited half, AC3).
4. ``Jamie attended two events...``     [^3]  — cited, label SHARED with
   bullet 5: drop must NOT remove [^3] (the non-orphaned half of AC2).
5. ``Jamie organized a third event...`` [^3]  — shares [^3] with bullet 4,
   stays on the page; its continued reference is what proves bullet 4's
   drop did not orphan the label.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from athenaeum import runlock
from athenaeum.cli import EXIT_LOCK_HELD, main
from athenaeum.note_corrections import (
    ACTIONS,
    DISPOSITIONS,
    BatchEnvelope,
    CorrectionRecord,
    NoteCorrectionError,
    apply_batch,
    dry_run_report,
    load_batch,
    previously_applied,
)

SOURCE_UID = "src00001"
TARGET_UID = "tgt00001"

_SOURCE_BODY = """## Notes

- Jamie switched teams at Acme in March.[^1]
- Someone else entirely did something unrelated.
- Jamie also closed the Beta deal.[^2]
- Jamie attended two events this year.[^3]
- Jamie organized a third event in the fall.[^3]

[^1]: **Source:** `drive/d001-jamie-switch.md`
[^2]: **Source:** `drive/d002-jamie-beta.md`
[^3]: **Source:** `drive/d003-jamie-events.md`
"""


def _bid(raw: str) -> str:
    """The stable id :func:`athenaeum.page_decompose.bullet_id` would
    compute for a top-level bullet whose ordinal and exact text are known
    by construction from :data:`_SOURCE_BODY` above."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


BID_1 = f"1-{_bid('- Jamie switched teams at Acme in March.[^1]')}"
BID_2 = f"2-{_bid('- Someone else entirely did something unrelated.')}"
BID_3 = f"3-{_bid('- Jamie also closed the Beta deal.[^2]')}"
BID_4 = f"4-{_bid('- Jamie attended two events this year.[^3]')}"
BID_5 = f"5-{_bid('- Jamie organized a third event in the fall.[^3]')}"


def _write_page(path: Path, *, uid: str, name: str, body: str) -> None:
    path.write_text(
        f"""---
uid: {uid}
type: person
name: {name}
created: 2026-01-01
updated: 2026-01-01
---

{body}""",
        encoding="utf-8",
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A private knowledge root with a source page and one move target."""
    root = tmp_path / "knowledge"
    wiki = root / "wiki"
    wiki.mkdir(parents=True)
    _write_page(wiki / f"{SOURCE_UID}-jamie.md", uid=SOURCE_UID, name="Jamie", body=_SOURCE_BODY)
    _write_page(
        wiki / f"{TARGET_UID}-jordan.md",
        uid=TARGET_UID,
        name="Jordan",
        body="## Notes\n\n- Jordan likes coffee.[^1]\n\n[^1]: **Source:** `drive/d099-jordan.md`\n",
    )
    return root


def _write_batch(root: Path, batch_id: str, records: list[dict], *, uid: str = SOURCE_UID) -> Path:
    path = root / f"{batch_id}.json"
    path.write_text(
        json.dumps(
            {
                "source_uid": uid,
                "batch_id": batch_id,
                "created_at": "2026-10-06",
                "records": records,
            }
        ),
        encoding="utf-8",
    )
    return path


def _digests(wiki: Path) -> dict[str, str]:
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(wiki.glob("*.md"))
    }


def _cli_argv(root: Path, batch_path: Path, *extra: str) -> list[str]:
    return ["correct-notes", SOURCE_UID, "--batch", str(batch_path), "--path", str(root), *extra]


class TestLoadBatch:
    def test_round_trips_a_well_formed_batch(self, tmp_path: Path) -> None:
        path = _write_batch(tmp_path, "b1", [{"bullet_id": BID_1, "action": "drop"}])
        envelope = load_batch(path)
        assert envelope.batch_id == "b1"
        assert envelope.source_uid == SOURCE_UID
        assert envelope.records == (CorrectionRecord(bullet_id=BID_1, action="drop"),)

    def test_rejects_invalid_json(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("{nope")
        with pytest.raises(NoteCorrectionError):
            load_batch(path)

    def test_rejects_a_non_object(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("[1, 2]")
        with pytest.raises(NoteCorrectionError):
            load_batch(path)

    @pytest.mark.parametrize("key", ["source_uid", "batch_id", "created_at", "records"])
    def test_rejects_a_missing_required_key(self, tmp_path: Path, key: str) -> None:
        payload = {
            "source_uid": SOURCE_UID,
            "batch_id": "b1",
            "created_at": "2026-10-06",
            "records": [{"bullet_id": BID_1, "action": "drop"}],
        }
        del payload[key]
        path = tmp_path / "bad.json"
        path.write_text(json.dumps(payload))
        with pytest.raises(NoteCorrectionError, match=key):
            load_batch(path)

    def test_rejects_an_unknown_action(self, tmp_path: Path) -> None:
        path = _write_batch(tmp_path, "b1", [{"bullet_id": BID_1, "action": "rename"}])
        with pytest.raises(NoteCorrectionError):
            load_batch(path)

    def test_rejects_a_duplicate_bullet_id_in_one_batch(self, tmp_path: Path) -> None:
        path = _write_batch(
            tmp_path,
            "b1",
            [
                {"bullet_id": BID_1, "action": "drop"},
                {"bullet_id": BID_1, "action": "drop"},
            ],
        )
        with pytest.raises(NoteCorrectionError, match="duplicate"):
            load_batch(path)

    def test_rejects_a_move_with_no_target_uid(self, tmp_path: Path) -> None:
        path = _write_batch(tmp_path, "b1", [{"bullet_id": BID_1, "action": "move"}])
        with pytest.raises(NoteCorrectionError, match="requires"):
            load_batch(path)

    def test_rejects_a_drop_that_carries_a_target_uid(self, tmp_path: Path) -> None:
        path = _write_batch(
            tmp_path, "b1", [{"bullet_id": BID_1, "action": "drop", "target_uid": TARGET_UID}]
        )
        with pytest.raises(NoteCorrectionError, match="must not carry"):
            load_batch(path)

    def test_actions_closed_set_is_move_and_drop(self) -> None:
        assert ACTIONS == {"move", "drop"}


class TestMoveCited:
    """AC2/AC3: a cited line carries its OWN footnote across."""

    def test_apply_carries_the_definition_and_renumbers_the_marker(self, workspace: Path) -> None:
        wiki = workspace / "wiki"
        batch_path = _write_batch(
            workspace,
            "b-move-cited",
            [{"bullet_id": BID_3, "action": "move", "target_uid": TARGET_UID}],
        )
        outcome = apply_batch(wiki, load_batch(batch_path))
        assert [r.disposition for r in outcome.results] == ["moved"]

        source_text = (wiki / f"{SOURCE_UID}-jamie.md").read_text(encoding="utf-8")
        assert "Beta deal" not in source_text
        assert "[^2]: **Source:** `drive/d002-jamie-beta.md`" not in source_text

        target_text = (wiki / f"{TARGET_UID}-jordan.md").read_text(encoding="utf-8")
        assert "- Jamie also closed the Beta deal.[^2]" in target_text
        assert "[^2]: **Source:** `drive/d002-jamie-beta.md`" in target_text


class TestMoveUncited:
    """AC2/AC3: an uncited line gets a transport footnote, never dropped
    for lack of a citation."""

    def test_apply_attaches_a_transport_footnote(self, workspace: Path) -> None:
        wiki = workspace / "wiki"
        batch_path = _write_batch(
            workspace,
            "b-move-uncited",
            [{"bullet_id": BID_2, "action": "move", "target_uid": TARGET_UID}],
        )
        outcome = apply_batch(wiki, load_batch(batch_path))
        assert [r.disposition for r in outcome.results] == ["moved"]

        source_text = (wiki / f"{SOURCE_UID}-jamie.md").read_text(encoding="utf-8")
        assert "Someone else entirely" not in source_text

        target_text = (wiki / f"{TARGET_UID}-jordan.md").read_text(encoding="utf-8")
        assert "- Someone else entirely did something unrelated.[^2]" in target_text
        assert (
            f"moved from {SOURCE_UID} by note-correction b-move-uncited/{BID_2} on"
            in target_text
        )


class TestDropOrphanedFootnote:
    """AC2: dropping the only line that cites a label removes the
    definition too."""

    def test_drop_removes_the_now_orphaned_definition(self, workspace: Path) -> None:
        wiki = workspace / "wiki"
        batch_path = _write_batch(
            workspace, "b-drop-orphan", [{"bullet_id": BID_1, "action": "drop"}]
        )
        outcome = apply_batch(wiki, load_batch(batch_path))
        assert [r.disposition for r in outcome.results] == ["dropped"]

        text = (wiki / f"{SOURCE_UID}-jamie.md").read_text(encoding="utf-8")
        assert "Jamie switched teams" not in text
        assert "[^1]: **Source:** `drive/d001-jamie-switch.md`" not in text


class TestDropNonOrphanedFootnote:
    """AC2: dropping ONE of two lines sharing a label keeps the definition,
    because the other line still cites it."""

    def test_drop_keeps_the_definition_a_sibling_line_still_cites(self, workspace: Path) -> None:
        wiki = workspace / "wiki"
        batch_path = _write_batch(
            workspace, "b-drop-shared", [{"bullet_id": BID_4, "action": "drop"}]
        )
        outcome = apply_batch(wiki, load_batch(batch_path))
        assert [r.disposition for r in outcome.results] == ["dropped"]

        text = (wiki / f"{SOURCE_UID}-jamie.md").read_text(encoding="utf-8")
        assert "Jamie attended two events" not in text
        assert "Jamie organized a third event" in text
        assert "[^3]: **Source:** `drive/d003-jamie-events.md`" in text


class TestOrdinalDrift:
    """AC5's single-snapshot rule: a batch that drops an EARLIER bullet and
    moves a LATER one in the same call must resolve the later id against
    the ORIGINAL snapshot, not a re-parsed, already-shifted one — otherwise
    the later id's ordinal would be read against the wrong line."""

    def test_a_drop_and_a_later_move_in_one_batch_both_resolve_correctly(
        self, workspace: Path
    ) -> None:
        wiki = workspace / "wiki"
        batch_path = _write_batch(
            workspace,
            "b-drift",
            [
                {"bullet_id": BID_1, "action": "drop"},
                {"bullet_id": BID_3, "action": "move", "target_uid": TARGET_UID},
            ],
        )
        outcome = apply_batch(wiki, load_batch(batch_path))
        dispositions = {r.bullet_id: r.disposition for r in outcome.results}
        assert dispositions == {BID_1: "dropped", BID_3: "moved"}

        source_text = (wiki / f"{SOURCE_UID}-jamie.md").read_text(encoding="utf-8")
        assert "Jamie switched teams" not in source_text
        assert "Beta deal" not in source_text
        # The untouched bullets (2, 4, 5) must survive intact and in order.
        assert "Someone else entirely" in source_text
        assert "Jamie attended two events" in source_text
        assert "Jamie organized a third event" in source_text

        target_text = (wiki / f"{TARGET_UID}-jordan.md").read_text(encoding="utf-8")
        assert "Beta deal" in target_text


class TestAllOrNothingRefusal:
    """AC5: one bad record refuses the WHOLE batch before any write."""

    def test_an_unknown_bullet_id_refuses_the_whole_batch(self, workspace: Path) -> None:
        wiki = workspace / "wiki"
        before = _digests(wiki)
        batch_path = _write_batch(
            workspace,
            "b-bad-id",
            [
                {"bullet_id": BID_2, "action": "drop"},
                {"bullet_id": "99-deadbeefdead", "action": "drop"},
            ],
        )
        with pytest.raises(NoteCorrectionError, match="unknown bullet id"):
            apply_batch(wiki, load_batch(batch_path))
        assert _digests(wiki) == before
        assert not previously_applied(wiki, "b-bad-id")

    def test_a_missing_move_target_refuses_the_whole_batch(self, workspace: Path) -> None:
        wiki = workspace / "wiki"
        before = _digests(wiki)
        batch_path = _write_batch(
            workspace,
            "b-bad-target",
            [
                {"bullet_id": BID_1, "action": "drop"},
                {"bullet_id": BID_3, "action": "move", "target_uid": "nope00000"},
            ],
        )
        with pytest.raises(NoteCorrectionError, match="names no wiki page"):
            apply_batch(wiki, load_batch(batch_path))
        assert _digests(wiki) == before

    def test_a_refusal_never_writes_the_ledger(self, workspace: Path) -> None:
        wiki = workspace / "wiki"
        batch_path = _write_batch(workspace, "b-refuse", [{"bullet_id": "bogus", "action": "drop"}])
        with pytest.raises(NoteCorrectionError):
            apply_batch(wiki, load_batch(batch_path))
        assert not (wiki / "_note_corrections_applied.jsonl").exists()


class TestSelfMoveRefused:
    """A `move` whose `target_uid` names the batch's OWN source_uid must
    never reach apply_batch's commit phase: target_cache would then hold
    source_path, the commit loop would write it with the appended bullet,
    and the very next line — the unconditional source write — would
    immediately clobber that with new_source_body (computed from the
    ORIGINAL body, which never saw the append), silently destroying the
    moved line. Confirmed true positive from Sentry/Seer review on PR
    athenaeum#1985; this class is the regression test that would have
    caught it before merge."""

    def test_load_batch_rejects_a_move_onto_its_own_source_uid(self, tmp_path: Path) -> None:
        path = _write_batch(
            tmp_path,
            "b-self",
            [{"bullet_id": BID_1, "action": "move", "target_uid": SOURCE_UID}],
        )
        with pytest.raises(NoteCorrectionError, match="same as the batch's own source_uid"):
            load_batch(path)

    def test_cli_apply_refuses_and_leaves_the_source_page_byte_unchanged(
        self, workspace: Path
    ) -> None:
        wiki = workspace / "wiki"
        source_file = wiki / f"{SOURCE_UID}-jamie.md"
        before_bytes = source_file.read_bytes()
        batch_path = _write_batch(
            workspace,
            "b-self-cli",
            [{"bullet_id": BID_3, "action": "move", "target_uid": SOURCE_UID}],
        )
        rc = main(_cli_argv(workspace, batch_path))
        assert rc == 1
        # The actual regression: assert the ON-DISK BYTES, not merely a
        # nonzero exit code. A refusal that still left a half-written page
        # (the exact failure mode here) would pass a raises/rc-only test.
        assert source_file.read_bytes() == before_bytes
        assert not (wiki / "_note_corrections_applied.jsonl").exists()

    def test_apply_batch_refuses_a_self_move_even_when_load_batch_is_bypassed(
        self, workspace: Path
    ) -> None:
        """Belt-and-suspenders: _resolve carries its OWN `target_path ==
        source_path` check (see its docstring), independent of
        load_batch's string check — construct the envelope directly,
        skipping load_batch entirely, and confirm apply_batch still
        refuses before any write."""
        wiki = workspace / "wiki"
        source_file = wiki / f"{SOURCE_UID}-jamie.md"
        before_bytes = source_file.read_bytes()
        envelope = BatchEnvelope(
            source_uid=SOURCE_UID,
            batch_id="b-self-resolve",
            created_at="2026-10-06",
            records=(
                CorrectionRecord(bullet_id=BID_3, action="move", target_uid=SOURCE_UID),
            ),
        )
        with pytest.raises(NoteCorrectionError):
            apply_batch(wiki, envelope)
        assert source_file.read_bytes() == before_bytes
        assert not (wiki / "_note_corrections_applied.jsonl").exists()

    def test_dry_run_reports_a_self_move_as_a_refusal_without_touching_the_page(
        self, workspace: Path
    ) -> None:
        wiki = workspace / "wiki"
        before = _digests(wiki)
        envelope = BatchEnvelope(
            source_uid=SOURCE_UID,
            batch_id="b-self-dry",
            created_at="2026-10-06",
            records=(
                CorrectionRecord(bullet_id=BID_3, action="move", target_uid=SOURCE_UID),
            ),
        )
        report = dry_run_report(wiki, envelope)
        assert report.moved == 0
        assert report.refused_unknown_target == 1
        assert _digests(wiki) == before


class TestSecondApplyIsANoop:
    """AC1: a second apply of the SAME batch_id writes nothing and reports
    every record as 'noop', keyed off the ledger, not off re-resolving
    (now-gone) bullet ids against the mutated page."""

    def test_replaying_the_same_batch_id_is_a_noop(self, workspace: Path) -> None:
        wiki = workspace / "wiki"
        batch_path = _write_batch(workspace, "b-replay", [{"bullet_id": BID_1, "action": "drop"}])
        envelope = load_batch(batch_path)

        first = apply_batch(wiki, envelope)
        assert [r.disposition for r in first.results] == ["dropped"]
        after_first = _digests(wiki)

        second = apply_batch(wiki, envelope)
        assert [r.disposition for r in second.results] == ["noop"]
        assert second.replay is True
        assert _digests(wiki) == after_first

        # Exactly one ledger line for this batch, not two.
        ledger_lines = (wiki / "_note_corrections_applied.jsonl").read_text().splitlines()
        parsed = [json.loads(line) for line in ledger_lines]
        matching = [rec for rec in parsed if rec.get("batch_id") == "b-replay"]
        assert len(matching) == 1

    def test_previously_applied_is_false_before_the_first_apply(self, workspace: Path) -> None:
        assert previously_applied(workspace / "wiki", "never-run") is False


class TestDryRun:
    def test_dry_run_writes_nothing(self, workspace: Path) -> None:
        wiki = workspace / "wiki"
        before = _digests(wiki)
        batch_path = _write_batch(
            workspace, "b-dry", [{"bullet_id": BID_3, "action": "move", "target_uid": TARGET_UID}]
        )
        report = dry_run_report(wiki, load_batch(batch_path))
        assert report.moved == 1
        assert report.dropped == 0
        assert _digests(wiki) == before
        assert not (wiki / "_note_corrections_applied.jsonl").exists()
        assert not (workspace / runlock.LOCKFILE_NAME).exists()

    def test_dry_run_counts_refusals_without_raising(self, workspace: Path) -> None:
        wiki = workspace / "wiki"
        batch_path = _write_batch(
            workspace,
            "b-dry-refuse",
            [
                {"bullet_id": BID_1, "action": "drop"},
                {"bullet_id": "bogus", "action": "drop"},
            ],
        )
        report = dry_run_report(wiki, load_batch(batch_path))
        assert report.refused_unknown_id == 1
        assert report.moved == 0
        assert report.dropped == 0
        assert report.body_chars_before == report.body_chars_after

    def test_dry_run_body_chars_match_what_apply_would_produce(self, workspace: Path) -> None:
        wiki = workspace / "wiki"
        batch_path = _write_batch(
            workspace, "b-dry-match", [{"bullet_id": BID_1, "action": "drop"}]
        )
        envelope = load_batch(batch_path)
        report = dry_run_report(wiki, envelope)
        outcome = apply_batch(wiki, envelope)
        assert report.body_chars_after == outcome.body_chars_after

    def test_dry_run_reports_a_replay_without_touching_the_page(self, workspace: Path) -> None:
        wiki = workspace / "wiki"
        batch_path = _write_batch(
            workspace, "b-dry-replay", [{"bullet_id": BID_1, "action": "drop"}]
        )
        envelope = load_batch(batch_path)
        apply_batch(wiki, envelope)
        before = _digests(wiki)
        report = dry_run_report(wiki, envelope)
        assert report.replay is True
        assert _digests(wiki) == before


class TestCli:
    def test_apply_via_cli_reports_zero_and_writes(self, workspace: Path) -> None:
        batch_path = _write_batch(workspace, "b-cli", [{"bullet_id": BID_1, "action": "drop"}])
        rc = main(_cli_argv(workspace, batch_path))
        assert rc == 0
        text = (workspace / "wiki" / f"{SOURCE_UID}-jamie.md").read_text(encoding="utf-8")
        assert "Jamie switched teams" not in text

    def test_dry_run_via_cli_writes_nothing(self, workspace: Path) -> None:
        wiki = workspace / "wiki"
        before = _digests(wiki)
        batch_path = _write_batch(workspace, "b-cli-dry", [{"bullet_id": BID_1, "action": "drop"}])
        rc = main(_cli_argv(workspace, batch_path, "--dry-run"))
        assert rc == 0
        assert _digests(wiki) == before

    def test_a_missing_wiki_root_is_refused(self, tmp_path: Path) -> None:
        batch_path = _write_batch(tmp_path, "b-nope", [{"bullet_id": BID_1, "action": "drop"}])
        rc = main(
            [
                "correct-notes",
                SOURCE_UID,
                "--batch",
                str(batch_path),
                "--path",
                str(tmp_path / "nowhere"),
            ]
        )
        assert rc == 1

    def test_a_batch_whose_source_uid_does_not_match_the_cli_uid_is_refused(
        self, workspace: Path
    ) -> None:
        batch_path = _write_batch(
            workspace, "b-mismatch", [{"bullet_id": BID_1, "action": "drop"}], uid="other-uid"
        )
        rc = main(_cli_argv(workspace, batch_path))
        assert rc == 1
        text = (workspace / "wiki" / f"{SOURCE_UID}-jamie.md").read_text(encoding="utf-8")
        assert "Jamie switched teams" in text

    def test_a_malformed_batch_file_is_refused(self, workspace: Path) -> None:
        batch_path = workspace / "bad.json"
        batch_path.write_text("{nope")
        rc = main(_cli_argv(workspace, batch_path))
        assert rc == 1


class TestRunLock:
    def test_dry_run_does_not_take_the_lock(self, workspace: Path) -> None:
        batch_path = _write_batch(workspace, "b-lock-dry", [{"bullet_id": BID_1, "action": "drop"}])
        main(_cli_argv(workspace, batch_path, "--dry-run"))
        assert not (workspace / runlock.LOCKFILE_NAME).exists()

    def test_apply_takes_the_lock(self, workspace: Path) -> None:
        batch_path = _write_batch(
            workspace, "b-lock-apply", [{"bullet_id": BID_1, "action": "drop"}]
        )
        assert main(_cli_argv(workspace, batch_path)) == 0
        assert (workspace / runlock.LOCKFILE_NAME).exists()

    def test_apply_refuses_and_writes_nothing_while_the_lock_is_held(self, workspace: Path) -> None:
        wiki = workspace / "wiki"
        before = _digests(wiki)
        batch_path = _write_batch(
            workspace, "b-lock-held", [{"bullet_id": BID_1, "action": "drop"}]
        )
        holder = runlock.RunLock(workspace)
        holder.acquire()
        try:
            assert main(_cli_argv(workspace, batch_path)) == EXIT_LOCK_HELD
        finally:
            holder.release()
        assert _digests(wiki) == before


class TestNoLLM:
    """AC6: this pass is tier-0 — it must not reach the LLM chain at all."""

    def test_no_provider_is_constructed_during_an_apply(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import athenaeum.provider as provider

        def _boom(*args: object, **kwargs: object) -> str:
            raise AssertionError("correct-notes must not resolve an LLM provider")

        monkeypatch.setattr(provider, "resolve_provider", _boom)
        monkeypatch.setattr(provider, "preflight_provider", _boom)
        batch_path = _write_batch(workspace, "b-no-llm", [{"bullet_id": BID_1, "action": "drop"}])
        assert main(_cli_argv(workspace, batch_path)) == 0

    def test_an_apply_never_imports_the_llm_chain(self, workspace: Path) -> None:
        """A subprocess, because the in-process suite has already imported
        half the tree — only a clean interpreter can tell what THIS command
        pulls in."""
        batch_path = _write_batch(
            workspace, "b-no-llm-sub", [{"bullet_id": BID_1, "action": "drop"}]
        )
        argv = _cli_argv(workspace, batch_path)
        probe = (
            "import json, sys\n"
            "from athenaeum.cli import main\n"
            f"rc = main({argv!r})\n"
            "print(json.dumps({'rc': rc, 'llm': sorted(\n"
            "    m for m in sys.modules\n"
            "    if m in ('anthropic', 'athenaeum.librarian', 'athenaeum.tiers',\n"
            "             'athenaeum.batch', 'athenaeum.merge')\n"
            ")}))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=False,
            env={
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                "PATH": "/usr/bin:/bin",
                "HOME": str(workspace.parent),
                "ATHENAEUM_CACHE_DIR": str(workspace.parent / "cache"),
            },
        )
        assert proc.returncode == 0, proc.stderr
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        assert result["rc"] == 0
        assert result["llm"] == []


class TestLedgerShape:
    def test_ledger_dispositions_sum_to_records_total(self, workspace: Path) -> None:
        wiki = workspace / "wiki"
        batch_path = _write_batch(
            workspace,
            "b-ledger",
            [
                {"bullet_id": BID_1, "action": "drop"},
                {"bullet_id": BID_3, "action": "move", "target_uid": TARGET_UID},
            ],
        )
        apply_batch(wiki, load_batch(batch_path))
        lines = (wiki / "_note_corrections_applied.jsonl").read_text().splitlines()
        record = json.loads(lines[-1])
        assert record["batch_id"] == "b-ledger"
        assert record["records_total"] == 2
        assert sum(record["dispositions"].values()) == 2
        assert set(record["dispositions"]) <= set(DISPOSITIONS)
        assert "body_chars_before" in record and "body_chars_after" in record


class TestBatchEnvelopeIsFrozen:
    def test_envelope_and_record_are_immutable(self) -> None:
        envelope = BatchEnvelope(
            source_uid=SOURCE_UID,
            batch_id="b1",
            created_at="2026-10-06",
            records=(CorrectionRecord(bullet_id=BID_1, action="drop"),),
        )
        with pytest.raises(Exception):
            envelope.batch_id = "other"  # type: ignore[misc]
