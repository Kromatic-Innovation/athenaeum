# SPDX-License-Identifier: Apache-2.0
"""Acceptance ledger (athenaeum#2938, raise S5): row shape, parser, lookups."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from athenaeum import raise_acceptance_ledger as led
from athenaeum.raise_acceptance_ledger import (
    ACCEPTANCE_LEDGER_FILENAME,
    AcceptanceRow,
    Adjustment,
    acceptance_ledger_path,
    disposition_of,
    find_adjustment,
    find_rejection,
    is_rejected,
    iter_rows,
    record_acceptance,
)
from athenaeum.store import ARTIFACT_REGISTRY

RID = "rs_k3v7t2hq4m4xw7pd5n6b"
RID2 = "rs_aaaaaaaaaaaaaaaaaaaa"
MID = "rm_7d2c5w4kq4t3h6vx5pnb"
MID2 = "rm_bbbbbbbbbbbbbbbbbbbb"
MID3 = "rm_cccccccccccccccccccc"
ORIGIN = "st_f2n6rb5hjc3t"
TARGET = "st_q7m2xk4vwz5d"
HASH = "sha256:" + "ab" * 32
BLOB = "0123456789abcdef0123456789abcdef01234567"
NOW = datetime(2026, 11, 18, 9, 31, 2, tzinfo=timezone.utc)


def _base(**over: object) -> dict:
    kw: dict = dict(
        disposition="accepted",
        kind="raise",
        raise_id=RID,
        message_id=MID,
        seq=1,
        origin=ORIGIN,
        target=TARGET,
        proposer={"iss": "https://git.example.net", "sub": "1203"},
        location="bench",
        payload_hash=HASH,
        blob_id=BLOB,
        now=NOW,
    )
    kw.update(over)
    return kw


class TestRoundTrip:
    def test_row_with_every_field_round_trips(self, tmp_path: Path) -> None:
        """AC1: a row with every field named in the issue survives write + parse."""
        adj = Adjustment(type="principle", tags=("policy", "hr"))
        row = record_acceptance(tmp_path, **_base(adjustment=adj))
        (parsed,) = iter_rows(tmp_path)
        assert parsed == row
        assert parsed.ts == "2026-11-18T09:31:02Z"
        assert parsed.proposer == {"iss": "https://git.example.net", "sub": "1203"}
        assert parsed.location == "bench"
        assert parsed.payload_hash == HASH
        assert parsed.adjustment == adj
        assert parsed.blob_id == BLOB
        assert (parsed.raise_id, parsed.message_id, parsed.seq) == (RID, MID, 1)
        assert (parsed.origin, parsed.target) == (ORIGIN, TARGET)

    def test_fact_row_records_page_uids(self, tmp_path: Path) -> None:
        """AC1: for a fact the row records the page uids written."""
        record_acceptance(
            tmp_path,
            **_base(
                kind="propose-fact",
                blob_id=None,
                page_uids=("0123456789abcdef", "fedcba9876543210"),
            ),
        )
        (parsed,) = iter_rows(tmp_path)
        assert parsed.page_uids == ("0123456789abcdef", "fedcba9876543210")
        assert parsed.blob_id is None

    def test_held_row_round_trips(self, tmp_path: Path) -> None:
        record_acceptance(
            tmp_path, **_base(disposition="held", reason="uid_taken", blob_id=None)
        )
        assert iter_rows(tmp_path)[0].disposition == "held"

    def test_dict_round_trip_is_exact(self) -> None:
        row = AcceptanceRow.from_dict(
            {
                "ts": "2026-11-18T09:31:02Z",
                "disposition": "rejected",
                "reason": "declined",
                "kind": "raise",
                "raise_id": RID,
                "message_id": MID,
                "seq": 1,
                "origin": ORIGIN,
                "target": TARGET,
                "proposer": {"iss": "i", "sub": "s"},
                "location": "install_raw",
            }
        )
        assert row is not None and AcceptanceRow.from_dict(row.to_dict()) == row

    def test_ledger_is_one_json_line_per_row(self, tmp_path: Path) -> None:
        record_acceptance(tmp_path, **_base())
        record_acceptance(tmp_path, **_base(message_id=MID2, seq=2))
        lines = acceptance_ledger_path(tmp_path).read_text().splitlines()
        assert len(lines) == 2 and all(json.loads(x) for x in lines)

    def test_torn_and_malformed_lines_are_skipped(self, tmp_path: Path) -> None:
        record_acceptance(tmp_path, **_base())
        with acceptance_ledger_path(tmp_path).open("a") as fh:
            fh.write('{"ts": "torn\n[1,2]\n{"a":1}\n')
        assert len(iter_rows(tmp_path)) == 1

    def test_missing_ledger_reads_empty(self, tmp_path: Path) -> None:
        assert iter_rows(tmp_path) == []
        assert not is_rejected(tmp_path, RID)
        assert find_adjustment(tmp_path, RID) is None
        assert disposition_of(tmp_path, MID) is None


class TestValidation:
    @pytest.mark.parametrize(
        "over",
        [
            dict(disposition="shrugged"),
            dict(raise_id="nope"),
            dict(message_id="rs_k3v7t2hq4m4xw7pd5n6b"),
            dict(seq=0),
            dict(origin="x"),
            dict(proposer={"iss": "a"}),
            dict(location="elsewhere"),
            dict(payload_hash="md5:abc"),
            dict(blob_id=None),  # accepted raise needs its blob
            dict(reason="declined"),  # accepted carries no reason
            dict(page_uids=("0123456789abcdef",)),  # a raise names a blob, not uids
            dict(kind="attest"),
            dict(adjustment=Adjustment()),
        ],
    )
    def test_invalid_row_is_refused(self, tmp_path: Path, over: dict) -> None:
        with pytest.raises(ValueError):
            record_acceptance(tmp_path, **_base(**over))
        assert iter_rows(tmp_path) == []

    def test_rejection_needs_a_code_from_the_fixed_list(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            record_acceptance(
                tmp_path, **_base(disposition="rejected", blob_id=None, reason=None)
            )
        with pytest.raises(ValueError):
            record_acceptance(
                tmp_path,
                **_base(disposition="rejected", blob_id=None, reason="because I said so"),
            )

    def test_only_accepted_rows_adjust(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            record_acceptance(
                tmp_path,
                **_base(
                    disposition="rejected",
                    blob_id=None,
                    reason="declined",
                    adjustment=Adjustment(type="x"),
                ),
            )


class TestAdjustmentLookup:
    def test_adjustment_for_raise_id_is_found_for_later_payloads(self, tmp_path: Path) -> None:
        """AC2: a reader finds the adjustment that applies to later payloads."""
        record_acceptance(
            tmp_path, **_base(adjustment=Adjustment(type="principle", tags=("policy",)))
        )
        got = find_adjustment(tmp_path, RID)
        assert got == Adjustment(type="principle", tags=("policy",))
        assert find_adjustment(tmp_path, RID2) is None

    def test_type_only_and_tags_only_adjustments(self, tmp_path: Path) -> None:
        record_acceptance(tmp_path, **_base(adjustment=Adjustment(type="note")))
        assert find_adjustment(tmp_path, RID) == Adjustment(type="note", tags=None)
        record_acceptance(
            tmp_path,
            **_base(message_id=MID2, seq=2, adjustment=Adjustment(tags=())),
        )
        assert find_adjustment(tmp_path, RID) == Adjustment(type=None, tags=())

    def test_latest_adjustment_wins(self, tmp_path: Path) -> None:
        record_acceptance(tmp_path, **_base(adjustment=Adjustment(type="a")))
        record_acceptance(
            tmp_path, **_base(message_id=MID2, seq=2, adjustment=Adjustment(type="b"))
        )
        assert find_adjustment(tmp_path, RID) == Adjustment(type="b")


class TestRejectionLookup:
    def test_rejection_records_reason_and_is_looked_up(self, tmp_path: Path) -> None:
        """AC3: a rejection carries a reason code; a lookup says it is rejected."""
        assert not is_rejected(tmp_path, RID)
        record_acceptance(
            tmp_path,
            **_base(disposition="rejected", blob_id=None, reason="store_refuses"),
        )
        assert is_rejected(tmp_path, RID)
        rej = find_rejection(tmp_path, RID)
        assert rej is not None and rej.reason == "store_refuses"
        assert not is_rejected(tmp_path, RID2)

    def test_held_or_accepted_is_not_rejected(self, tmp_path: Path) -> None:
        record_acceptance(tmp_path, **_base(disposition="held", blob_id=None))
        record_acceptance(tmp_path, **_base(raise_id=RID2, message_id=MID2))
        assert not is_rejected(tmp_path, RID)
        assert not is_rejected(tmp_path, RID2)

    def test_latest_row_is_a_messages_disposition(self, tmp_path: Path) -> None:
        record_acceptance(tmp_path, **_base(disposition="held", blob_id=None))
        record_acceptance(
            tmp_path, **_base(disposition="rejected", blob_id=None, reason="declined")
        )
        got = disposition_of(tmp_path, MID)
        assert got is not None and got.disposition == "rejected"


class TestCounterExample:
    """AC4: no page text, no source page identifier, never in the recall index."""

    def test_row_fields_hold_no_page_text_or_source_identifier(self) -> None:
        banned = {"body", "page", "text", "content", "uid", "path", "revision",
                  "source", "source_uid", "source_path", "source_hash", "raised_from"}
        assert not banned & led._ROW_KEYS

    @pytest.mark.parametrize(
        "extra", ["body", "page", "uid", "path", "source_uid", "revision", "source_hash"]
    )
    def test_row_carrying_such_a_key_is_malformed(self, tmp_path: Path, extra: str) -> None:
        row = record_acceptance(tmp_path, **_base())
        data = row.to_dict()
        data[extra] = "Every employee files a PTO usage report."
        assert AcceptanceRow.from_dict(data) is None

    def test_writer_has_no_text_parameter(self, tmp_path: Path) -> None:
        with pytest.raises(TypeError):
            record_acceptance(tmp_path, **_base(body="page text"))  # type: ignore[arg-type]

    def test_ledger_file_holds_only_ids_and_metadata(self, tmp_path: Path) -> None:
        record_acceptance(tmp_path, **_base(adjustment=Adjustment(type="principle")))
        data = json.loads(acceptance_ledger_path(tmp_path).read_text())
        assert set(data) <= led._ROW_KEYS

    def test_registered_as_operational_state_outside_wiki_and_raw(self) -> None:
        (entry,) = [e for e in ARTIFACT_REGISTRY if e.name == "raise-acceptance-ledger"]
        assert entry.persistence_class == "operational"
        assert entry.operational_scope == "store-durable"
        assert entry.location == "cache dir"
        assert ACCEPTANCE_LEDGER_FILENAME in entry.source_ref

    def test_never_added_to_the_recall_index(self, tmp_path: Path) -> None:
        from athenaeum.search import build_fts5_index, query_fts5_index

        wiki = tmp_path / "wiki"
        cache = tmp_path / "cache"
        wiki.mkdir()
        (wiki / "alpha.md").write_text(
            "---\nname: Alpha\ntype: principle\n---\nUnrelated page about gardening.\n"
        )
        record_acceptance(cache, **_base(adjustment=Adjustment(type="zzquuxtype")))
        build_fts5_index(wiki, cache, incremental=False)
        assert acceptance_ledger_path(cache).exists()
        for term in ("zzquuxtype", RID, "acceptance"):
            assert query_fts5_index(term, cache) == []
