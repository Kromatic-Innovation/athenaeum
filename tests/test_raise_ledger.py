# SPDX-License-Identifier: Apache-2.0
"""Raise ledger (athenaeum#3001, raise S4): row shape, parser, readers, registry."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from athenaeum import raise_ledger as led
from athenaeum.raise_ledger import (
    RAISE_LEDGER_FILENAME,
    RaiseRow,
    iter_raise_rows,
    latest_raise_row,
    latest_raise_rows,
    raise_ledger_path,
    read_raise_ledger,
    record_raise,
)
from athenaeum.store import ARTIFACT_REGISTRY

RID = "rs_k3v7t2hq4m4xw7pd5n6b"
RID2 = "rs_aaaaaaaaaaaaaaaaaaaa"
ORIGIN = "st_f2n6rb5hjc3t"
TARGET = "st_q7m2xk4vwz5d"
HASH = "sha256:" + "ab" * 32
NOW = datetime(2026, 11, 18, 9, 30, 0, tzinfo=timezone.utc)


def _row(**over: object) -> dict:
    d: dict = {
        "v": 1,
        "raise_id": RID,
        "seq": 1,
        "origin": ORIGIN,
        "source_uid": "5e1d09c3a7b24f86",
        "target": TARGET,
        "route": "proposer",
        "content_hash": HASH,
        "sent_at": "2026-11-18T09:30:00Z",
        "state": "sent",
    }
    d.update(over)
    return d


def _kw(**over: object) -> dict:
    d = _row(**over)
    d.pop("v")
    d.pop("sent_at")
    return d


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


class TestRowFields:
    def test_row_has_the_fields_of_the_design(self) -> None:
        row = RaiseRow.from_dict(_row())
        assert row is not None
        assert set(row.to_dict()) == {
            "v", "raise_id", "seq", "origin", "source_uid", "target",
            "route", "content_hash", "sent_at", "state",
        }  # fmt: skip
        assert row.to_dict() == _row()

    def test_round_trip(self) -> None:
        row = RaiseRow.from_dict(_row())
        assert row is not None and RaiseRow.from_dict(row.to_dict()) == row

    @pytest.mark.parametrize("extra", ["body", "title", "path", "page", "uid"])
    def test_a_row_with_page_text_or_extra_key_is_malformed(self, extra: str) -> None:
        assert RaiseRow.from_dict(_row(**{extra: "page text"})) is None

    def test_a_missing_key_is_malformed(self) -> None:
        d = _row()
        del d["state"]
        assert RaiseRow.from_dict(d) is None


class TestParserValidatesEachField:
    @pytest.mark.parametrize(
        "field,bad",
        [
            ("v", 2),
            ("v", True),
            ("raise_id", "rs_short"),
            ("raise_id", "rm_aaaaaaaaaaaaaaaaaaaa"),
            ("raise_id", 5),
            ("seq", 0),
            ("seq", "1"),
            ("seq", True),
            ("origin", "rs_aaaaaaaaaaaaaaaaaaaa"),
            ("origin", "st_"),
            ("target", "not-a-store"),
            ("source_uid", "XYZ"),
            ("source_uid", "5e1d09c3a7b24f8"),
            ("source_uid", ["5e1d09c3"]),
            ("route", "bench"),
            ("content_hash", "md5:" + "a" * 32),
            ("content_hash", "sha256:" + "AB" * 32),
            ("content_hash", "sha256:abc"),
            ("sent_at", "2026-11-18"),
            ("sent_at", "2026-11-18T09:30:00+00:00"),
            ("state", "pending"),
            ("state", None),
        ],
    )
    def test_bad_field_is_malformed_not_raised(self, field: str, bad: object) -> None:
        assert RaiseRow.from_dict(_row(**{field: bad})) is None

    @pytest.mark.parametrize("uid", ["5e1d09c3a7b24f86", "5e1d09c3"])
    def test_both_uid_widths_are_accepted(self, uid: str) -> None:
        assert RaiseRow.from_dict(_row(source_uid=uid)) is not None

    @pytest.mark.parametrize("route", led.RAISE_ROUTES)
    def test_every_route_is_accepted(self, route: str) -> None:
        assert RaiseRow.from_dict(_row(route=route)) is not None

    @pytest.mark.parametrize("state", led.RAISE_STATES)
    def test_every_state_is_accepted(self, state: str) -> None:
        assert RaiseRow.from_dict(_row(state=state)) is not None

    @pytest.mark.parametrize("data", [None, [], "x", 3, {}])
    def test_non_row_input_is_none(self, data: object) -> None:
        assert RaiseRow.from_dict(data) is None

    def test_unhashable_field_value_does_not_raise(self) -> None:
        assert RaiseRow.from_dict(_row(route=["proposer"])) is None


class TestLedger:
    def test_append_then_read(self, tmp_path: Path) -> None:
        row = record_raise(tmp_path, now=NOW, **_kw())
        assert row.sent_at == "2026-11-18T09:30:00Z"
        assert iter_raise_rows(tmp_path) == [row]
        assert raise_ledger_path(tmp_path).name == RAISE_LEDGER_FILENAME

    def test_append_only(self, tmp_path: Path) -> None:
        record_raise(tmp_path, now=NOW, **_kw())
        before = raise_ledger_path(tmp_path).read_text()
        record_raise(tmp_path, now=NOW, **_kw(seq=2, state="superseded"))
        assert raise_ledger_path(tmp_path).read_text().startswith(before)
        assert len(iter_raise_rows(tmp_path)) == 2

    def test_writer_refuses_a_bad_row_and_writes_nothing(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            record_raise(tmp_path, **_kw(route="bench"))
        assert not raise_ledger_path(tmp_path).exists()

    def test_writer_has_no_text_parameter(self, tmp_path: Path) -> None:
        with pytest.raises(TypeError):
            record_raise(tmp_path, **_kw(body="page text"))  # type: ignore[arg-type]

    def test_missing_ledger_reads_empty(self, tmp_path: Path) -> None:
        assert read_raise_ledger(tmp_path) == ([], [])
        assert latest_raise_rows(tmp_path) == {}
        assert latest_raise_row(tmp_path, RID) is None

    def test_malformed_rows_are_reported_not_raised(self, tmp_path: Path) -> None:
        path = raise_ledger_path(tmp_path)
        path.write_text(
            json.dumps(_row()) + "\n"
            + "{torn\n"
            + json.dumps(_row(state="bogus")) + "\n"
            + "\n"
            + "[1]\n"
        )  # fmt: skip
        rows, bad = read_raise_ledger(tmp_path)
        assert len(rows) == 1
        assert len(bad) == 3

    def test_non_utf8_and_deeply_nested_lines_are_reported_not_raised(self, tmp_path: Path) -> None:
        path = raise_ledger_path(tmp_path)
        path.write_bytes(
            json.dumps(_row()).encode()
            + b"\n\xff\xfe not utf8\n"
            + b"[" * 100000
            + b"\n"
            + json.dumps(_row(seq=2)).encode()
            + b"\n"
        )
        rows, bad = read_raise_ledger(tmp_path)
        assert [r.seq for r in rows] == [1, 2]
        assert len(bad) == 2

    def test_counter_example_reverse_order_resolves_to_seq_2(self, tmp_path: Path) -> None:
        _write(
            raise_ledger_path(tmp_path),
            [_row(seq=2, state="retracted"), _row(seq=1)],
        )
        latest = latest_raise_row(tmp_path, RID)
        assert latest is not None
        assert latest.seq == 2 and latest.state == "retracted"

    def test_latest_per_raise_id(self, tmp_path: Path) -> None:
        record_raise(tmp_path, now=NOW, **_kw())
        record_raise(tmp_path, now=NOW, **_kw(seq=2, state="superseded"))
        record_raise(tmp_path, now=NOW, **_kw(raise_id=RID2))
        latest = latest_raise_rows(tmp_path)
        assert set(latest) == {RID, RID2}
        assert latest[RID].seq == 2 and latest[RID2].seq == 1

    def test_same_seq_later_row_wins(self, tmp_path: Path) -> None:
        _write(raise_ledger_path(tmp_path), [_row(), _row(state="lapsed")])
        latest = latest_raise_row(tmp_path, RID)
        assert latest is not None and latest.state == "lapsed"

    def test_ledger_file_holds_only_ids_and_a_hash(self, tmp_path: Path) -> None:
        record_raise(tmp_path, now=NOW, **_kw())
        data = json.loads(raise_ledger_path(tmp_path).read_text())
        assert set(data) == led._ROW_KEYS


class TestRegistryAndState:
    def test_registered_as_operational_store_durable_in_cache_dir(self) -> None:
        (entry,) = [e for e in ARTIFACT_REGISTRY if e.name == "raise-ledger"]
        assert entry.persistence_class == "operational"
        assert entry.operational_scope == "store-durable"
        assert entry.location == "cache dir"
        assert RAISE_LEDGER_FILENAME in entry.source_ref

    def test_never_added_to_the_recall_index(self, tmp_path: Path) -> None:
        from athenaeum.search import build_fts5_index, query_fts5_index

        wiki = tmp_path / "wiki"
        cache = tmp_path / "cache"
        wiki.mkdir()
        (wiki / "alpha.md").write_text(
            "---\nname: Alpha\ntype: principle\n---\nUnrelated page about gardening.\n"
        )
        record_raise(cache, **_kw(source_uid="5e1d09c3a7b24f86"))
        build_fts5_index(wiki, cache, incremental=False)
        assert raise_ledger_path(cache).exists()
        assert not raise_ledger_path(cache).is_relative_to(wiki)
        for term in ("5e1d09c3a7b24f86", RID, "proposer"):
            assert query_fts5_index(term, cache) == []

    def test_no_production_module_writes_it_yet(self) -> None:
        src = Path(led.__file__).parent
        users = [
            p.name
            for p in src.glob("*.py")
            if p.name != "raise_ledger.py" and "record_raise" in p.read_text()
        ]
        assert users == []
