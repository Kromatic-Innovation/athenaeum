# SPDX-License-Identifier: Apache-2.0
"""The ledger and the handoff note carry opaque references (an ordinal, never a digest of the value)."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from athenaeum import corrections
from athenaeum.corrections import (
    default_corrections_ledger_path,
    find_correction_batches,
    process_batch_file,
    run_correction_phase,
)
from athenaeum.models import EntityIndex

SUBMITTER = "enrichment-service"
BATCH_ID = "20260806T030000Z-1a2b3c4d"
MARKER = "zz-sentinel-value-zz"
HANDLE = "someone.private@example.org"


def _seed(tmp_path: Path) -> tuple[Path, Path]:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "person-a.md").write_text(
        "---\nuid: person-a\ntype: person\nname: A\n---\nBody.\n", encoding="utf-8"
    )
    raw = tmp_path / "raw" / SUBMITTER
    raw.mkdir(parents=True)
    records = [
        # ordinal 0: ordinary raised-tier (target does not resolve)
        {"record": "correction", "target": {"uid": "nobody"}, "op": "set",
         "field": "current_title", "value": "x"},
        # ordinal 1: a field off the allowlist, on a resolving target
        {"record": "correction", "target": {"uid": "person-a"}, "op": "set",
         "field": "favourite_colour", "value": MARKER},
        # ordinal 2: off the allowlist, target given as an email handle
        {"record": "correction", "target": {"type": "person", "handle": {"email": HANDLE}},
         "op": "set", "field": "favourite_colour", "value": MARKER + "-2"},
    ]
    envelope = {
        "record": "batch", "schema_version": 1, "submitter": SUBMITTER,
        "batch_id": BATCH_ID, "created_at": "2026-08-06T03:00:00Z",
        "defaults": {"source": "api:vendor", "observed_at": "2026-08-06T03:00:00Z"},
    }
    (raw / f"{BATCH_ID}.jsonl").write_text(
        "\n".join([json.dumps(envelope)] + [json.dumps(r) for r in records]) + "\n"
    )
    return wiki, tmp_path / "raw"


def _config() -> dict[str, Any]:
    return {
        "librarian": {
            "corrections": {
                "fields": {"current_title": {"shape": "scalar", "writers": [SUBMITTER]}}
            }
        }
    }


def _phase(tmp_path: Path, wiki: Path, raw: Path) -> None:
    run_correction_phase(
        raw_root=raw,
        wiki_root=wiki,
        knowledge_root=tmp_path,
        index=EntityIndex(wiki),
        config=_config(),
        escalate_one=lambda r, o: True,
        now=date(2026, 8, 6),
    )


def _computed_ids(tmp_path: Path, wiki: Path, raw: Path) -> list[str]:
    ((path, source, envelope),) = find_correction_batches(raw)
    outcome = process_batch_file(
        path, envelope, source, index=EntityIndex(wiki), knowledge_root=tmp_path,
        config=_config(), dry_run=True,
    )
    assert [r.ordinal for r in outcome.results] == [0, 1, 2]
    return [r.correction_id for r in outcome.results]


def _notes(raw: Path) -> list[Path]:
    return sorted((raw / SUBMITTER).glob("*.md"))


def test_new_line_carries_refs_not_values_or_ids(tmp_path: Path) -> None:
    wiki, raw = _seed(tmp_path)
    ids = _computed_ids(tmp_path, wiki, raw)
    _phase(tmp_path, wiki, raw)
    line = default_corrections_ledger_path(wiki).read_text()
    rec = json.loads(line)
    assert rec["raised_tier_refs"] == [0, 1, 2]
    assert rec["non_trivial_refs"] == [0, 1, 2]
    assert rec["withheld"] == [
        {"ref": 1, "stored": "nowhere"},
        {"ref": 2, "stored": "nowhere"},
    ]
    for old_key in (
        "redacted_correction_payloads",
        "raised_tier_correction_ids",
        "non_trivial_correction_ids",
    ):
        assert old_key not in rec
    assert '"value"' not in line
    assert MARKER not in line and HANDLE not in line
    for cid in ids:
        assert cid not in line
    assert not [t for t in re.findall(r"[0-9a-f]{16}", line) if t in ids]


def test_note_carries_refs_and_withholds_target_and_value(tmp_path: Path) -> None:
    wiki, raw = _seed(tmp_path)
    ids = _computed_ids(tmp_path, wiki, raw)
    _phase(tmp_path, wiki, raw)
    (note,) = _notes(raw)
    text = note.read_text()
    assert "correction_id=" not in text
    for ref in (0, 1, 2):
        assert f"ref={BATCH_ID}#{ref}" in text
    assert text.count("value withheld; not recorded: no vault is configured") == 2
    assert 'target={"uid": "person-a"}' in text  # resolved target: the entity uid
    assert "target withheld" in text  # the email handle is never printed
    assert MARKER not in text and HANDLE not in text
    assert all(cid not in text for cid in ids)
    assert "audit ledger" not in corrections.REDACTED_VALUE_PLACEHOLDER


def test_duplicate_handoff_guard_works_on_refs(tmp_path: Path) -> None:
    wiki, raw = _seed(tmp_path)
    _phase(tmp_path, wiki, raw)
    # Not a git repository: retirement is refused, so the batch is carried over.
    assert (raw / SUBMITTER / f"{BATCH_ID}.jsonl").exists()
    _phase(tmp_path, wiki, raw)
    assert len(_notes(raw)) == 1


def test_old_format_line_still_guards_the_batch(tmp_path: Path) -> None:
    wiki, raw = _seed(tmp_path)
    ids = _computed_ids(tmp_path, wiki, raw)
    old = {
        "batch_id": BATCH_ID,
        "records_total": 3,
        "dispositions": {"raised-tier": 3},
        "raised_tier_correction_ids": ids,
        "redacted_correction_payloads": [],
    }
    default_corrections_ledger_path(wiki).write_text(json.dumps(old) + "\n")
    _phase(tmp_path, wiki, raw)
    assert _notes(raw) == []  # every record was already handed off
    # The old line is not rewritten.
    first = default_corrections_ledger_path(wiki).read_text().splitlines()[0]
    assert json.loads(first) == old


def test_parked_row_keeps_the_original_ordinal(tmp_path: Path) -> None:
    wiki, raw = _seed(tmp_path)
    ((path, source, envelope),) = find_correction_batches(raw)
    outcome = process_batch_file(
        path, envelope, source, index=EntityIndex(wiki), knowledge_root=tmp_path,
        config=_config(), dry_run=True,
    )
    parked = outcome.results[1]
    parked.disposition = "parked"
    corrections._park_new_results(
        wiki, [parked], envelope=envelope, outcome=outcome, today=date(2026, 8, 6)
    )
    (row,) = corrections.load_parked_corrections(wiki).values()
    assert row["ordinal"] == 1


def test_a_vaulted_value_is_named_in_the_note_by_reference(tmp_path: Path) -> None:
    from athenaeum.corrections import BatchOutcome, CorrectionRecordResult

    result = CorrectionRecordResult(
        correction_id="0123456789abcdef",
        disposition="raised-tier",
        reason=corrections.ALLOWLIST_REJECTION_REASON,
        target={"uid": "person-a"},
        field="f",
        op="set",
        value=MARKER,
        ordinal=4,
        withheld_stored="vault",
    )
    outcome = BatchOutcome(
        path=tmp_path / "b.jsonl",
        source=SUBMITTER,
        envelope={"submitter": SUBMITTER, "batch_id": BATCH_ID},
        records_total=1,
        results=[result],
    )
    note = corrections.write_correction_handoff(
        outcome, [result], raw_root=tmp_path / "raw", knowledge_root=tmp_path
    )
    text = note.read_text()
    assert f"value withheld; stored in the vault under reference {BATCH_ID}#4" in text
    assert MARKER not in text and "0123456789abcdef" not in text
    assert corrections.build_ledger_record(outcome)["withheld"] == [
        {"ref": 4, "stored": "vault"}
    ]
