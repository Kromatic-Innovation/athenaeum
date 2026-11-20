# SPDX-License-Identifier: Apache-2.0
"""Intake routing S15: a store's raw intake on its single compiling install (athenaeum#3000)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from athenaeum import store_intake_dispatch as sid
from athenaeum.intake import discover_raw_files, discover_store_raw_dirs, store_raw_dir
from athenaeum.raise_messages import KIND_RAISE, KIND_RETRACT, RaiseMessage
from athenaeum.storage import IndexedSurfaceError, resolve_indexed_surfaces
from athenaeum.store_decisions import load_queue
from tests.test_raise_acceptance import (
    COMPILER,
    MID,
    TARGET,
    Env,
    _git_identity,  # noqa: F401 - autouse fixture
)

OTHER = "st_b6t3kd5wq2mx"


class Kb:
    """A knowledge root with a mounted, declared target store and a raw tree."""

    def __init__(self, tmp: Path) -> None:
        self.env = Env(tmp)
        self.kr = self.env.kr
        self.raw = self.kr / "raw"
        (self.kr / "wiki").mkdir(exist_ok=True)
        self.cache = self.env.cache
        self.config: dict[str, Any] = {
            "storage": {
                "adapters": {
                    "target": {
                        "backing_store": "markdown",
                        "surface_root": "target",
                        "corpus_policy": {"embedded": False},
                    }
                }
            },
            "owner": {"asserter": COMPILER},
        }

    def drop(self, store_id: str, name: str, text: str) -> Path:
        d = store_raw_dir(self.raw, store_id) / "raise"
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        p.write_text(text, encoding="utf-8")
        return p

    def batch(self, *records: dict[str, Any]) -> str:
        lines = [json.dumps({"record": "batch", "schema_version": 1, "submitter": "raise",
                             "batch_id": "b1", "created_at": "2026-11-18T09:30:00Z"})]
        lines += [json.dumps(r) for r in records]
        return "\n".join(lines) + "\n"

    def run(self, **kw: Any) -> sid.DispatchReport:
        return sid.dispatch_store_intake(self.kr, self.config, cache_dir=self.cache, **kw)


@pytest.fixture
def kb(tmp_path: Path) -> Kb:
    return Kb(tmp_path)


# AC1: the directory is the raw intake of the store with that id ------------


def test_store_directory_is_recognised_and_invalid_name_reported_not_read(kb: Kb) -> None:
    kb.drop(TARGET, "a.jsonl", kb.batch(kb.env.msg()))
    bad = kb.raw / "_stores" / "not-a-store" / "raise"
    bad.mkdir(parents=True)
    (bad / "x.jsonl").write_text(kb.batch(kb.env.msg()), encoding="utf-8")
    stores, invalid = discover_store_raw_dirs(kb.raw)
    assert list(stores) == [TARGET] and invalid == ["not-a-store"]
    report = kb.run()
    assert report.invalid_dirs == ["not-a-store"]
    assert report.files_read == 1  # the invalid directory's file was never read


def test_store_raw_dir_refuses_a_name_that_is_not_a_store_id(kb: Kb) -> None:
    with pytest.raises(ValueError):
        store_raw_dir(kb.raw, "../escape")


# AC2: discovery skips raw/_stores/ -----------------------------------------


def test_discover_raw_files_skips_store_raw_intake(kb: Kb) -> None:
    kb.drop(TARGET, "2026-11-18-note.md", "---\nname: x\n---\nbody\n")
    (kb.raw / "_stores" / TARGET / "2026-11-18-top.md").write_text("hi", encoding="utf-8")
    assert [f for f in discover_raw_files(kb.raw, kb.config) if "_stores" in str(f.path)] == []
    assert discover_raw_files(kb.raw, kb.config) == []


# AC6: counter-example ------------------------------------------------------


def test_personal_raw_note_under_raw_is_still_discovered(kb: Kb) -> None:
    src = kb.raw / "session"
    src.mkdir(parents=True)
    note = src / "20261118T093000Z-abcd1234.md"
    note.write_text("---\nname: n\n---\nbody\n", encoding="utf-8")
    kb.drop(TARGET, "2026-11-18-note.md", "---\nname: x\n---\nbody\n")
    found = discover_raw_files(kb.raw, kb.config)
    assert [f.path for f in found] == [note]


# AC3: dispatch by message shape, raise acceptance first --------------------


def test_raise_message_is_handed_to_raise_acceptance(kb: Kb) -> None:
    kb.drop(TARGET, "a.jsonl", kb.batch(kb.env.msg()))
    report = kb.run()
    assert report.handled == 1
    assert [d["kind"] for d in load_queue(kb.kr, TARGET)] == ["raise"]


def test_a_handled_message_is_not_handed_on_twice(kb: Kb) -> None:
    kb.drop(TARGET, "a.jsonl", kb.batch(kb.env.msg()))
    kb.run()
    second = kb.run()
    assert second.handled == 0 and second.skipped == 1
    assert len(load_queue(kb.kr, TARGET)) == 1


def test_message_naming_another_store_is_not_applied_and_is_recorded(kb: Kb) -> None:
    kb.drop(TARGET, "a.jsonl", kb.batch(kb.env.msg(target=OTHER)))
    report = kb.run()
    assert report.misaddressed == 1 and report.handled == 0
    assert load_queue(kb.kr, TARGET) == []
    rows = list(sid._ledger_rows(kb.cache))
    assert [(r["outcome"], r["store_id"], r["message_id"]) for r in rows] == [
        ("misaddressed", TARGET, MID)
    ]
    # recorded once: a second run does not re-record it
    kb.run()
    assert len(list(sid._ledger_rows(kb.cache))) == 1


def test_registry_dispatches_by_kind_and_unregistered_kind_is_deferred(kb: Kb) -> None:
    retract = {
        "record": "retract", "raise_id": "rs_k3v7t2hq4m4xw7pd5n6b", "seq": 2,
        "message_id": "rm_7d2c5w4kq4t3h6vx5pnc", "origin": kb.env.msg()["origin"],
        "target": TARGET, "proposer": kb.env.msg()["proposer"], "via": "proposer",
        "sent_at": "2026-11-18T09:30:00Z", "reason": "withdrawn",
    }
    kb.drop(TARGET, "r.jsonl", kb.batch(retract))
    # an empty registry: the default one gains a retract handler once the intake run loads it
    first = kb.run(registry=sid.HandlerRegistry())
    assert first.unhandled == 1 and first.handled == 0

    seen: list[tuple[str, str]] = []

    def handler(
        ctx: sid.MessageContext, msg: RaiseMessage, record: dict[str, Any]
    ) -> sid.HandlerResult:
        seen.append((ctx.store_id, msg.kind))
        return sid.HandlerResult("ok")

    reg = sid.HandlerRegistry()
    reg.register(KIND_RETRACT, handler)
    with pytest.raises(ValueError):
        reg.register(KIND_RETRACT, handler)
    # not final earlier, so it is read again once a handler exists
    second = kb.run(registry=reg)
    assert seen == [(TARGET, KIND_RETRACT)] and second.handled == 1


def test_default_registry_has_raise_handler() -> None:
    assert KIND_RAISE in sid.DEFAULT_REGISTRY.kinds()


# AC4: non-conforming file is raised as a decision, never classified --------


def test_nonconforming_file_is_raised_as_a_decision_in_the_stores_queue(kb: Kb) -> None:
    p = kb.drop(TARGET, "junk.jsonl", "this is not a batch\n")
    report = kb.run()
    assert report.nonconforming == 1
    queue = load_queue(kb.kr, TARGET)
    assert [d["kind"] for d in queue] == [sid.DECISION_KIND_NONCONFORMING]
    assert p.exists()  # never deleted or moved
    assert discover_raw_files(kb.raw, kb.config) == []  # never in personal classify
    kb.run()
    assert len(load_queue(kb.kr, TARGET)) == 1  # not re-raised every run


def test_markdown_file_in_a_store_dir_is_nonconforming_not_classified(kb: Kb) -> None:
    kb.drop(TARGET, "note.md", "---\nname: x\n---\nbody\n")
    assert kb.run().nonconforming == 1


def test_store_not_mounted_defers_without_a_decision(kb: Kb) -> None:
    kb.drop(OTHER, "a.jsonl", kb.batch(kb.env.msg(target=OTHER)))
    report = kb.run()
    assert report.deferred == 1 and report.handled == 0
    assert load_queue(kb.kr, OTHER) == []


# AC5: the recall-index config check ----------------------------------------


@pytest.mark.parametrize(
    "extra", ["raw/_stores", f"raw/_stores/{TARGET}", f"raw/_stores/{TARGET}/raise"]
)
def test_extra_intake_roots_naming_store_raw_intake_is_refused(kb: Kb, extra: str) -> None:
    (kb.kr / extra).mkdir(parents=True, exist_ok=True)
    cfg = {"recall": {"extra_intake_roots": [extra]}}
    with pytest.raises(IndexedSurfaceError, match="raw intake"):
        resolve_indexed_surfaces(kb.kr, cfg)


def test_a_root_containing_store_raw_intake_is_refused(kb: Kb) -> None:
    (kb.raw / "_stores").mkdir(parents=True)
    with pytest.raises(IndexedSurfaceError):
        resolve_indexed_surfaces(kb.kr, {"recall": {"extra_intake_roots": ["raw"]}})


def test_ordinary_extra_root_is_still_accepted(kb: Kb) -> None:
    (kb.raw / "auto-memory").mkdir(parents=True)
    got = resolve_indexed_surfaces(kb.kr, {"recall": {"extra_intake_roots": ["raw/auto-memory"]}})
    assert [s.prefix for s in got] == ["auto-memory"]
