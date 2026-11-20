# SPDX-License-Identifier: Apache-2.0
"""Raise S13: apply a supersede in the target (athenaeum#2940)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from athenaeum import raise_supersede as rs
from athenaeum.models import parse_frontmatter, render_frontmatter
from athenaeum.raise_acceptance import Outcome, accept_raise, submit_raise
from athenaeum.raise_acceptance_ledger import (
    DISPOSITION_ACCEPTED,
    Adjustment,
    disposition_of,
    iter_rows,
    record_acceptance,
)
from athenaeum.raise_messages import (
    KIND_RAISE,
    KIND_RETRACT,
    KIND_SUPERSEDE,
    RaiseMessage,
    render_message,
)
from athenaeum.raise_projection import Projection, derived_uid, payload_hash, render
from athenaeum.raise_provenance import parse_raised_from
from athenaeum.raise_supersede import apply_supersede
from athenaeum.store import read_blob
from athenaeum.store_decisions import load_queue

ISS = "https://git.example.net"
PROPOSER = {"iss": ISS, "sub": "1203"}
COMPILER = {"iss": ISS, "sub": "compiler"}
ORIGIN = "st_f2n6rb5hjc3t"
TARGET = "st_q7m2xk4vwz5d"
RID = "rs_k3v7t2hq4m4xw7pd5n6b"
RID2 = "rs_p4n6b2x5w7d3k3v7t2hq"
BODY = "## Rule\n\nEvery employee files a PTO usage report by 31 December.\n\n## Notes\n\nFirst.\n"
PAGE = {
    "type": "principle",
    "name": "Year-end PTO usage report",
    "description": "Every employee files a PTO report.",
    "aliases": [],
    "tags": ["policy"],
}
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.net",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.net",
}


def _mid(n: int) -> str:
    return "rm_" + "abcdefghij"[n] * 20


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in GIT_ENV.items():
        monkeypatch.setenv(k, v)


class Env:
    def __init__(self, tmp: Path) -> None:
        self.kr = tmp / "knowledge"
        self.root = tmp / "knowledge" / "target"
        self.cache = tmp / "cache"
        self.root.mkdir(parents=True)
        self.cache.mkdir()
        (self.root / "_store").mkdir()
        decl = {
            "version": 2,
            "store_id": TARGET,
            "roles": {
                "writers": {"rights": ["read", "propose"], "members": [PROPOSER]},
                "owners": {"rights": ["read", "compile", "answer"], "members": [COMPILER]},
            },
        }
        (self.root / "_store" / "access.yaml").write_text(yaml.safe_dump(decl), encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "seed"], check=True)

    def msg(self, kind: str = KIND_RAISE, **over: Any) -> dict[str, Any]:
        page = dict(over.pop("page", PAGE))
        body = over.pop("body", BODY)
        fields: dict[str, Any] = dict(
            kind=kind,
            message_id=_mid(0),
            origin=ORIGIN,
            target=TARGET,
            proposer=PROPOSER,
            sent_at="2026-11-18T09:30:00Z",
            raise_id=RID,
            seq=1,
            via="proposer",
        )
        if kind == KIND_SUPERSEDE and "successor" in over:
            pass
        else:
            fields.update(
                page=page,
                body=body,
                content_hash=payload_hash(render(Projection(meta=dict(page), body=body))),
            )
        fields.update(over)
        return json.loads(render_message(RaiseMessage(**fields)))

    def raise_and_accept(
        self, raise_id: str = RID, mid: str | None = None, **kw: Any
    ) -> None:
        over: dict[str, Any] = {"raise_id": raise_id, "message_id": mid or _mid(0)}
        over.update(kw.pop("msg_over", {}))
        out = submit_raise(
            self.kr, self.msg(**over), target_store_id=TARGET, target_root=self.root,
            cache_dir=self.cache, location="install_raw",
        )
        assert out.status == "asked", out
        done = accept_raise(
            self.kr, target_store_id=TARGET, target_root=self.root, cache_dir=self.cache,
            decision_id=out.decision["id"], **kw,  # type: ignore[index]
        )
        assert done.status == "accepted"

    def supersede(self, seq: int = 2, mid: int = 1, **over: Any) -> Outcome:
        return self.apply(self.msg(KIND_SUPERSEDE, seq=seq, message_id=_mid(mid), **over))

    def apply(self, record: object, location: str = "install_raw", **kw: Any) -> Outcome:
        return apply_supersede(
            self.kr, record, target_store_id=TARGET, target_root=self.root,
            cache_dir=self.cache, location=location, **kw,
        )

    def copy_path(self, raise_id: str = RID) -> Path:
        hits = list(self.root.glob(f"{derived_uid(TARGET, raise_id)}*.md"))
        assert len(hits) == 1
        return hits[0]

    def copy(self, raise_id: str = RID) -> tuple[dict[str, Any], str]:
        meta, body = parse_frontmatter(self.copy_path(raise_id).read_text(encoding="utf-8"))
        return dict(meta), body

    def edit(self, mutate: Any) -> None:
        """Edit the copy in the target and commit it, as a person would."""
        path = self.copy_path()
        meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        meta, body = mutate(dict(meta), body)
        path.write_text(render_frontmatter(meta) + body, encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "edit"], check=True)

    def log(self) -> str:
        return subprocess.run(
            ["git", "-C", str(self.root), "log", "--format=%B"],
            capture_output=True, text=True, check=True,
        ).stdout

    def reasons(self) -> list[str | None]:
        return [r.reason for r in iter_rows(self.cache)]

    def kinds(self) -> list[str]:
        return [r["kind"] for r in load_queue(self.kr, TARGET) if r["disposition"] == "pending"]


@pytest.fixture
def env(tmp_path: Path) -> Env:
    e = Env(tmp_path)
    e.raise_and_accept()
    return e


NEW_BODY = BODY.replace("31 December", "15 December")


# AC1 -- untouched is decided by projection, never by a whole-page hash -------


def test_ac1_whole_page_hash_differs_but_projection_equal_is_untouched(env: Env) -> None:
    # Non-projected keys change the whole page, never the projection.
    env.edit(lambda m, b: ({**m, "updated": "2026-11-19", "related": ["other"]}, b))
    out = env.supersede(body=NEW_BODY)
    assert out.status == rs.APPLIED, out
    meta, body = env.copy()
    assert body == NEW_BODY  # replaced outright, not merged
    assert meta["related"] == ["other"] and meta["updated"] == "2026-11-19"  # kept


def test_ac1_an_edit_to_a_projected_key_is_an_edit(env: Env) -> None:
    env.edit(lambda m, b: ({**m, "description": "Reworded here."}, b))
    out = env.supersede(body=NEW_BODY)
    assert out.status == rs.APPLIED  # merged: the sides touched different units
    meta, body = env.copy()
    assert meta["description"] == "Reworded here."  # the target's edit survived
    assert "15 December" in body  # and so did the new version


# AC2 -- untouched copy is replaced, adjustment re-applied, hash and seq updated


def test_ac2_untouched_copy_is_replaced_and_hash_and_seq_are_updated(env: Env) -> None:
    out = env.supersede(body=NEW_BODY, page={**PAGE, "description": "New description."})
    assert out.status == rs.APPLIED and out.commit is not None
    meta, body = env.copy()
    assert body == NEW_BODY and meta["description"] == "New description."
    block = parse_raised_from(meta)
    assert block is not None and block.seq == 2
    new_page = {**PAGE, "description": "New description."}
    assert block.content_hash == payload_hash(render(Projection(meta=new_page, body=NEW_BODY)))
    assert payload_hash(env.copy_path().read_text(encoding="utf-8")) == block.content_hash
    assert f"Athenaeum-Raise: {RID} seq=2 supersede" in env.log()
    row = disposition_of(env.cache, _mid(1))
    assert row is not None and row.disposition == DISPOSITION_ACCEPTED
    assert row.kind == KIND_SUPERSEDE and row.blob_id is not None
    status = subprocess.run(
        ["git", "-C", str(env.root), "status", "--porcelain"], capture_output=True, text=True
    ).stdout
    assert status == ""


def test_ac2_recorded_type_and_tags_adjustment_is_applied_again(tmp_path: Path) -> None:
    e = Env(tmp_path)
    e.raise_and_accept(type="policy", tags=["hr"])
    row = next(r for r in iter_rows(e.cache) if r.adjustment)
    assert row.adjustment == Adjustment(type="policy", tags=("hr",))
    out = e.supersede(body=NEW_BODY)  # the payload still says principle / policy
    assert out.status == rs.APPLIED
    meta, _ = e.copy()
    assert (meta["type"], meta["tags"]) == ("policy", ["hr"])
    # untouched afterwards: the stored hash is of the adjusted copy
    assert payload_hash(e.copy_path().read_text(encoding="utf-8")) == (
        parse_raised_from(meta).content_hash  # type: ignore[union-attr]
    )
    assert e.supersede(seq=3, mid=2, body=NEW_BODY + "More.\n").status == rs.APPLIED


def test_ac2_lower_seq_is_ignored(env: Env) -> None:
    assert env.supersede(seq=3, mid=1, body=NEW_BODY).status == rs.APPLIED
    before = env.copy_path().read_text(encoding="utf-8")
    out = env.supersede(seq=2, mid=2, body="## Rule\n\nOlder.\n")
    assert out.status == rs.IGNORED and out.reason == "stale_seq"
    assert env.copy_path().read_text(encoding="utf-8") == before
    assert env.kinds() == []  # no decision either
    # the same seq again, under another message id, is not applied twice
    assert env.supersede(seq=3, mid=3, body="## Rule\n\nSame seq.\n").status == rs.IGNORED


def test_ac2_redelivery_of_the_same_message_is_a_duplicate(env: Env) -> None:
    rec = env.msg(KIND_SUPERSEDE, seq=2, message_id=_mid(1), body=NEW_BODY)
    assert env.apply(rec).status == rs.APPLIED
    assert env.apply(rec).status == "duplicate"


# AC3 -- an edited copy is merged three-way against the last accepted version --


def test_ac3_ancestor_is_the_accepted_blob_read_through_the_store(env: Env) -> None:
    accepted = next(r for r in iter_rows(env.cache) if r.blob_id)
    data = read_blob(env.root, accepted.blob_id)  # type: ignore[arg-type]
    assert data is not None and b"31 December" in data
    assert read_blob(env.root, "0" * 40) is None
    assert read_blob(env.root, "--help") is None


def test_ac3_target_edit_and_new_payload_in_different_units_both_apply(env: Env) -> None:
    env.edit(lambda m, b: (m, b.replace("First.", "First, plus our own note.")))
    out = env.supersede(body=NEW_BODY)  # changes the Rule section only
    assert out.status == rs.APPLIED
    _, body = env.copy()
    assert "15 December" in body and "our own note" in body


def test_ac3_a_later_supersede_merges_against_the_latest_accepted_version(env: Env) -> None:
    assert env.supersede(body=NEW_BODY).status == rs.APPLIED
    env.edit(lambda m, b: (m, b.replace("First.", "First, ours.")))
    third = NEW_BODY.replace("15 December", "1 December")
    assert env.supersede(seq=3, mid=2, body=third).status == rs.APPLIED
    _, body = env.copy()
    assert "1 December" in body and "First, ours." in body


def test_ac3_contested_unit_holds_the_page_and_raises_a_decision(env: Env) -> None:
    env.edit(lambda m, b: (m, b.replace("31 December", "30 November")))
    published = env.copy_path().read_text(encoding="utf-8")
    out = env.supersede(body=NEW_BODY)  # the same sentence, changed differently
    assert out.status == "held" and out.reason == "edit_contested"
    assert env.copy_path().read_text(encoding="utf-8") == published  # published version stays
    assert "supersede" not in env.log()  # nothing was committed
    pending = [r for r in load_queue(env.kr, TARGET) if r["disposition"] == "pending"]
    assert [r["kind"] for r in pending] == ["raise_held"]
    assert pending[0]["detail"]["units"] == ["section:rule"]
    row = disposition_of(env.cache, _mid(1))
    assert row is not None and row.disposition == "held" and row.reason == "edit_contested"
    # the held supersede is not "applied": a later one is still above the copy's seq
    assert parse_raised_from(env.copy()[0]).seq == 1  # type: ignore[union-attr]


def test_ac3_an_unreadable_ancestor_holds_rather_than_guessing(env: Env) -> None:
    env.edit(lambda m, b: (m, b.replace("First.", "Ours.")))
    # point the ledger's only accepted row at a blob that is not in the repository
    ledger = env.cache / "_raise_acceptance.jsonl"
    row = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
    assert row["disposition"] == "accepted"
    row["blob_id"] = "1" * 40
    ledger.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")
    out = env.supersede(body=NEW_BODY)
    assert out.status == "held" and out.reason == "ancestor_unreadable"


# AC4 -- access / audience change is held, even for an untouched copy ----------


@pytest.mark.parametrize(
    "extra",
    [{"access": "private"}, {"audience": ["finance"]}, {"audience": "all"}],
)
def test_ac4_visibility_change_is_held_even_when_untouched(env: Env, extra: dict) -> None:
    before = env.copy_path().read_text(encoding="utf-8")
    out = env.supersede(body=NEW_BODY, page={**PAGE, **extra})
    assert out.status == "held" and out.reason == "access_changed"
    assert env.copy_path().read_text(encoding="utf-8") == before  # not replaced
    assert out.decision is not None and out.decision["kind"] == "raise_held"
    assert "supersede" not in env.log()


def test_ac4_visibility_change_is_held_for_an_edited_copy_too(env: Env) -> None:
    env.edit(lambda m, b: (m, b.replace("First.", "Ours.")))
    before = env.copy_path().read_text(encoding="utf-8")
    out = env.supersede(body=NEW_BODY, page={**PAGE, "access": "private"})
    assert out.status == "held" and out.reason == "access_changed"
    assert env.copy_path().read_text(encoding="utf-8") == before


def test_ac4_an_unchanged_visibility_is_not_a_hold(tmp_path: Path) -> None:
    e = Env(tmp_path)
    page = {**PAGE, "access": "team", "audience": ["hr"]}
    e.raise_and_accept(msg_over={"page": page})
    assert e.supersede(body=NEW_BODY, page=page).status == rs.APPLIED


# AC5 -- the successor form -------------------------------------------------


def test_ac5_successor_form_stamps_superseded_by_with_the_successor_slug(env: Env) -> None:
    env.raise_and_accept(
        RID2, _mid(5), msg_over={"page": {**PAGE, "name": "Annual leave report"}}
    )
    out = env.apply(env.msg(KIND_SUPERSEDE, seq=2, message_id=_mid(1), successor=RID2))
    assert out.status == rs.APPLIED
    meta, body = env.copy()
    assert meta["superseded_by"] == "annual-leave-report"
    assert body == BODY  # the copy stays on disk, otherwise as it was
    assert parse_raised_from(meta).seq == 2  # type: ignore[union-attr]
    assert f"Athenaeum-Raise: {RID} seq=2 supersede" in env.log()


def test_ac5_successor_whose_raise_is_waiting_keeps_the_message_waiting(env: Env) -> None:
    out = submit_raise(
        env.kr, env.msg(raise_id=RID2, message_id=_mid(5)), target_store_id=TARGET,
        target_root=env.root, cache_dir=env.cache, location="install_raw",
    )
    assert out.status == "asked"
    rows_before = len(iter_rows(env.cache))
    res = env.apply(env.msg(KIND_SUPERSEDE, seq=2, message_id=_mid(1), successor=RID2))
    assert res.status == rs.WAITING
    assert len(iter_rows(env.cache)) == rows_before  # nothing recorded: it waits
    assert "superseded_by" not in env.copy()[0]


def _reject(env: Env, raise_id: str) -> None:
    record_acceptance(
        env.cache, disposition="rejected", reason="declined", kind=KIND_RAISE,
        raise_id=raise_id, message_id=_mid(6), seq=1, origin=ORIGIN, target=TARGET,
        proposer=PROPOSER, location="install_raw",
    )


def test_ac5_rejected_successor_is_treated_as_a_retract(env: Env) -> None:
    _reject(env, RID2)
    seen: list[RaiseMessage] = []

    def retract(m: RaiseMessage) -> Outcome:
        seen.append(m)
        return Outcome("retract-seen")

    out = env.apply(
        env.msg(KIND_SUPERSEDE, seq=2, message_id=_mid(1), successor=RID2), apply_retract=retract
    )
    assert out.status == "retract-seen"
    [m] = seen
    assert m.kind == KIND_RETRACT and m.reason == "superseded_at_source"
    assert (m.raise_id, m.seq, m.message_id) == (RID, 2, _mid(1))
    assert "superseded_by" not in env.copy()[0]  # nothing stamped


def test_ac5_rejected_successor_without_a_retract_applier_holds_the_copy(env: Env) -> None:
    _reject(env, RID2)
    before = env.copy_path().read_text(encoding="utf-8")
    out = env.apply(env.msg(KIND_SUPERSEDE, seq=2, message_id=_mid(1), successor=RID2))
    assert out.status == "held" and out.reason == "superseded_at_source"
    assert out.decision is not None
    assert env.copy_path().read_text(encoding="utf-8") == before  # never removed here


# §7.2 / §7.3 states -------------------------------------------------------


def test_a_page_at_the_uid_with_another_raise_id_is_never_touched(tmp_path: Path) -> None:
    e = Env(tmp_path)
    page = e.root / f"{derived_uid(TARGET, RID)}-mine.md"
    page.write_text("---\nuid: x\ntype: note\nname: Mine\n---\nMy own page.\n", encoding="utf-8")
    out = e.supersede(body=NEW_BODY)
    assert out.status == "held" and out.reason == "uid_taken"
    assert page.read_text(encoding="utf-8").endswith("My own page.\n")


def test_copy_removed_by_the_target_is_recorded_and_stops(env: Env) -> None:
    env.copy_path().unlink()
    out = env.supersede(body=NEW_BODY)
    assert out.status == rs.IGNORED and out.reason == "copy_removed" and out.decision is None


def test_unknown_raise_is_recorded_and_stops(tmp_path: Path) -> None:
    e = Env(tmp_path)
    out = e.supersede(body=NEW_BODY)
    assert out.status == rs.IGNORED and out.reason == "raise_unknown"


def test_a_supersede_for_a_raise_still_waiting_waits(tmp_path: Path) -> None:
    e = Env(tmp_path)
    submit_raise(
        e.kr, e.msg(), target_store_id=TARGET, target_root=e.root, cache_dir=e.cache,
        location="install_raw",
    )
    rows = len(iter_rows(e.cache))
    assert e.supersede(body=NEW_BODY).status == rs.WAITING
    assert len(iter_rows(e.cache)) == rows


def test_a_rejected_raise_is_recorded_and_stops(tmp_path: Path) -> None:
    e = Env(tmp_path)
    _reject(e, RID)
    out = e.supersede(body=NEW_BODY)
    assert out.reason == "already_rejected" and out.decision is None


def test_a_tombstoned_copy_is_not_replaced(env: Env) -> None:
    env.edit(lambda m, b: ({**m, "status": "folded", "folded_into": "elsewhere"}, b))
    out = env.supersede(body=NEW_BODY)
    assert out.status == "held" and out.reason == "copy_folded"


def test_other_proposer_or_location_is_held(env: Env) -> None:
    out = env.apply(env.msg(KIND_SUPERSEDE, seq=2, message_id=_mid(1), body=NEW_BODY), "bench")
    assert out.status == "held" and out.reason == "sender_mismatch"
    assert "15 December" not in env.copy()[1]


def test_proposer_without_the_right_is_held(env: Env) -> None:
    out = env.supersede(body=NEW_BODY, proposer={"iss": ISS, "sub": "9999"})
    assert out.status == "held" and out.reason == "proposer_lacks_right"


def test_hash_mismatch_and_links_are_held(env: Env) -> None:
    out = env.supersede(body=NEW_BODY, content_hash="sha256:" + "0" * 64)
    assert out.reason == "hash_mismatch"
    out = env.supersede(seq=3, mid=2, body="See [[a1b2c3d4e5f60718]].\n")
    assert out.reason == "payload_has_link"


def test_an_ended_watch_ignores_the_message(env: Env) -> None:
    env.edit(lambda m, b: ({**m, "raised_from": {**m["raised_from"], "watch": "ended"}}, b))
    out = env.supersede(body=NEW_BODY)
    assert out.status == rs.IGNORED and out.reason == "copy_ended"


def test_an_unparseable_record_is_a_decision(env: Env) -> None:
    out = env.apply({"record": "nonsense"})
    assert out.status == "held" and out.reason == "unparseable"


def test_a_raise_record_is_not_a_supersede(env: Env) -> None:
    out = env.apply(env.msg())
    assert out.reason == "unparseable"


def test_a_failed_commit_restores_the_copy(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    before = env.copy_path().read_text(encoding="utf-8")

    def boom(*a: object, **k: object) -> None:
        raise subprocess.CalledProcessError(1, "git")

    monkeypatch.setattr(rs, "commit_paths", boom)
    with pytest.raises(subprocess.CalledProcessError):
        env.supersede(body=NEW_BODY)
    assert env.copy_path().read_text(encoding="utf-8") == before
    assert disposition_of(env.cache, _mid(1)) is None


def test_a_copy_in_a_subdirectory_is_committed_from_the_store_root(env: Env) -> None:
    sub = env.root / "notes"
    sub.mkdir()
    moved = sub / env.copy_path().name
    env.copy_path().rename(moved)
    subprocess.run(["git", "-C", str(env.root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(env.root), "commit", "-qm", "move"], check=True)
    assert env.supersede(body=NEW_BODY).status == rs.APPLIED
