# SPDX-License-Identifier: Apache-2.0
"""Raise S11: apply a retract in the target (athenaeum#3003)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from athenaeum import raise_retract as rr
from athenaeum import store_intake_dispatch as sid
from athenaeum.models import parse_frontmatter, render_frontmatter
from athenaeum.raise_acceptance import Outcome, RaiseAcceptRefused, submit_raise
from athenaeum.raise_acceptance_ledger import disposition_of, is_rejected, iter_rows
from athenaeum.raise_messages import KIND_RAISE, KIND_RETRACT, KIND_SUPERSEDE
from athenaeum.raise_projection import derived_uid
from athenaeum.raise_provenance import parse_raised_from
from athenaeum.store_decisions import load_queue
from tests.test_raise_supersede import (
    ORIGIN,
    PROPOSER,
    RID,
    RID2,
    TARGET,
    Env,
    _git_identity,  # noqa: F401 - autouse fixture
    _mid,
    _reject,
)
from tests.test_store_intake_dispatch import Kb


class RetractEnv(Env):
    def retract(self, seq: int = 2, mid: int = 3, raise_id: str = RID, **over: Any) -> Outcome:
        return self.apply_retract(self.retract_msg(seq, mid, raise_id, **over))

    def retract_msg(self, seq: int = 2, mid: int = 3, raise_id: str = RID, **over: Any) -> dict:
        fields: dict[str, Any] = dict(
            record=KIND_RETRACT, raise_id=raise_id, seq=seq, message_id=_mid(mid),
            origin=ORIGIN, target=TARGET, proposer=PROPOSER, via="proposer",
            sent_at="2026-11-19T09:30:00Z", reason="withdrawn",
        )
        fields.update(over)
        return fields

    def apply_retract(self, record: object, location: str = "install_raw") -> Outcome:
        return rr.apply_retract(
            self.kr, record, target_store_id=TARGET, target_root=self.root,
            cache_dir=self.cache, location=location,
        )

    def gone(self, raise_id: str = RID) -> bool:
        return not list(self.root.glob(f"{derived_uid(TARGET, raise_id)}*.md"))

    def subjects(self) -> list[str]:
        return subprocess.run(
            ["git", "-C", str(self.root), "log", "--format=%s"],
            capture_output=True, text=True, check=True,
        ).stdout.splitlines()


@pytest.fixture
def env(tmp_path: Path) -> RetractEnv:
    e = RetractEnv(tmp_path)
    e.raise_and_accept()
    return e


@pytest.fixture
def waiting(tmp_path: Path) -> RetractEnv:
    e = RetractEnv(tmp_path)
    out = submit_raise(
        e.kr, e.msg(), target_store_id=TARGET, target_root=e.root, cache_dir=e.cache,
        location="install_raw",
    )
    assert out.status == "asked"
    return e


# AC1 -- the copy is found by the derived uid, nothing is scanned ------------


def test_ac1_copy_is_found_by_the_uid_derived_from_store_id_and_raise_id(
    env: RetractEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []
    real = rr._pages_at_uid

    def spy(root: Path, uid: str) -> list[Path]:
        asked.append(uid)
        return real(root, uid)

    monkeypatch.setattr(rr, "_pages_at_uid", spy)
    out = env.retract()
    assert out.status == rr.APPLIED and out.uid == derived_uid(TARGET, RID)
    assert asked == [derived_uid(TARGET, RID)]  # one lookup at the derived uid, no scan
    assert env.gone()


# AC2 -- the rows of the §7.2 table --------------------------------------------


def test_ac2_tombstone_is_followed_and_is_the_copy(env: RetractEnv) -> None:
    env.edit(lambda m, b: ({**m, "status": "folded", "folded_into": "the-other-page"}, b))
    out = env.retract()
    assert out.status == "asked", out
    [rec] = [r for r in load_queue(env.kr, TARGET) if r["kind"] == rr.DECISION_KIND_RETRACT]
    assert rec["detail"]["reason"] == "folded"
    assert rec["detail"]["folded_into"] == "the-other-page"  # named, never touched
    assert not env.gone()


def test_ac2_nothing_at_the_uid_after_an_accepted_raise_is_recorded_and_stops(
    env: RetractEnv,
) -> None:
    env.copy_path().unlink()
    out = env.retract()
    assert out.status == rr.IGNORED and out.reason == "copy_removed"
    assert disposition_of(env.cache, _mid(3)).reason == "copy_removed"  # type: ignore[union-attr]
    assert load_queue(env.kr, TARGET) == [] or env.kinds() == []


def test_ac2_a_waiting_raise_is_withdrawn_and_both_messages_are_consumed(
    waiting: RetractEnv,
) -> None:
    assert waiting.kinds() == ["raise"]
    out = waiting.retract()
    assert out.status == rr.WITHDRAWN
    assert disposition_of(waiting.cache, _mid(0)).reason == "withdrawn"  # type: ignore[union-attr]
    assert disposition_of(waiting.cache, _mid(3)).reason == "withdrawn"  # type: ignore[union-attr]
    assert waiting.kinds() == []  # the raise's pending decision is settled
    assert [r["disposition"] for r in load_queue(waiting.kr, TARGET)] == ["withdrawn"]
    assert is_rejected(waiting.cache, RID)  # later messages for it are dropped
    assert waiting.gone()


def test_ac2_a_later_supersede_for_a_withdrawn_raise_stops(waiting: RetractEnv) -> None:
    waiting.retract()
    res = waiting.supersede(seq=3, mid=4)
    assert res.status == rr.IGNORED and res.reason == "already_rejected"


def test_ac2_a_rejected_raise_is_recorded_and_stops(env: RetractEnv) -> None:
    _reject(env, RID2)
    out = env.retract(raise_id=RID2)
    assert out.status == rr.IGNORED and out.reason == "already_rejected"


def test_ac2_an_unknown_raise_is_recorded_and_stops(env: RetractEnv) -> None:
    out = env.retract(raise_id=RID2)
    assert out.status == rr.IGNORED and out.reason == "raise_unknown"
    assert not env.gone()  # the other raise's copy is untouched


def test_ac2_a_page_with_another_raise_id_is_never_touched_and_a_decision_is_raised(
    env: RetractEnv,
) -> None:
    path = env.copy_path()
    meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    block = dict(meta["raised_from"])
    block["raise_id"] = RID2
    meta["raised_from"] = block
    meta["sources"] = [f"raise:{RID2}"]
    path.write_text(render_frontmatter(meta) + body, encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    out = env.retract()
    assert out.status == "held" and out.reason == "uid_taken" and out.decision is not None
    assert path.read_text(encoding="utf-8") == before


def test_ac2_a_page_with_no_raised_from_is_never_touched(env: RetractEnv) -> None:
    path = env.copy_path()
    meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    del meta["raised_from"]
    path.write_text(render_frontmatter(meta) + body, encoding="utf-8")
    out = env.retract()
    assert out.status == "held" and out.reason == "uid_taken"
    assert path.exists()


# AC3 -- same location and same proposer, else a decision ----------------------


def test_ac3_a_different_proposer_is_a_decision_not_a_removal(env: RetractEnv) -> None:
    out = env.retract(proposer={"iss": PROPOSER["iss"], "sub": "9999"})
    assert out.status == "held" and out.reason == "sender_mismatch" and out.decision
    assert not env.gone()


def test_ac3_a_different_location_is_a_decision_not_a_removal(env: RetractEnv) -> None:
    out = env.apply_retract(env.retract_msg(), location="bench")
    assert out.status == "held" and out.reason == "sender_mismatch" and out.decision
    assert not env.gone()


def test_ac3_a_waiting_raise_is_not_withdrawn_by_another_proposer(waiting: RetractEnv) -> None:
    out = waiting.retract(proposer={"iss": PROPOSER["iss"], "sub": "9999"})
    assert out.status == "held" and out.reason == "sender_mismatch"
    assert waiting.kinds().count("raise") == 1  # still waiting


def test_ac3_same_location_and_proposer_is_applied_by_rule(env: RetractEnv) -> None:
    assert env.retract().status == rr.APPLIED


# AC4 -- an untouched copy is removed with the retirement mechanics ------------


def test_ac4_untouched_copy_is_removed_with_snapshot_then_removal_and_the_commit_line(
    env: RetractEnv,
) -> None:
    # An uncommitted change outside the projection: the snapshot commit keeps it.
    path = env.copy_path()
    text = path.read_text(encoding="utf-8").replace("---\n", "---\nupdated: 2026-11-19\n", 1)
    path.write_text(text, encoding="utf-8")
    out = env.retract(seq=2)
    assert out.status == rr.APPLIED and out.commit
    assert env.gone()
    subjects = env.subjects()
    assert subjects[0].startswith("chore(retire-pages): retire 1 page(s)")
    assert subjects[1].startswith("chore(retire-pages): provenance snapshot")  # snapshot first
    msg = subprocess.run(
        ["git", "-C", str(env.root), "log", "-1", "--format=%B", out.commit],
        capture_output=True, text=True, check=True,
    ).stdout
    assert f"\nAthenaeum-Raise: {RID} seq=2 retract\n" in "\n" + msg
    assert disposition_of(env.cache, _mid(3)).disposition == "accepted"  # type: ignore[union-attr]
    assert not (env.root / "wiki").exists()  # a store root gets no index of its own


def test_ac4_pending_merges_naming_the_copy_are_withdrawn(env: RetractEnv) -> None:
    from athenaeum.pending_merges import parse_pending_merges, write_pending_merge

    wiki = env.root / "wiki"
    wiki.mkdir()
    merges = wiki / "_pending_merges.md"
    write_pending_merge(
        merges, merge_target_name="Other", sources=[str(env.copy_path())],
        rationale="names the copy", draft_merged_body="merged", confidence=0.9,
    )
    subprocess.run(["git", "-C", str(env.root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(env.root), "commit", "-qm", "wiki"], check=True)
    assert env.retract().status == rr.APPLIED
    assert env.gone()
    assert parse_pending_merges(merges) == []
    assert (wiki / "_pending_merges_archive.md").is_file()


def test_ac4_a_failed_removal_records_nothing_so_it_is_retried(
    env: RetractEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Report:
        errors = ["boom"]
        committed = False

    monkeypatch.setattr(rr, "apply_retirement", lambda *a, **k: Report())
    with pytest.raises(RuntimeError):
        env.retract()
    assert disposition_of(env.cache, _mid(3)) is None
    assert not env.gone()


# AC5 -- an edited copy asks; kept, the watch ends ------------------------------


def _edit_projected(env: RetractEnv) -> None:
    env.edit(lambda m, b: ({**m, "description": "Reworded here."}, b))


def test_ac5_an_edited_copy_is_not_removed_and_a_decision_asks(env: RetractEnv) -> None:
    _edit_projected(env)
    out = env.retract()
    assert out.status == "asked" and out.decision
    assert not env.gone()
    assert env.kinds() == [rr.DECISION_KIND_RETRACT]
    assert "keep it as this store's own page" in out.decision["question"]


def test_ac5_keeping_ends_the_watch_and_later_messages_are_recorded_and_ignored(
    env: RetractEnv,
) -> None:
    _edit_projected(env)
    out = env.retract()
    done = rr.answer_retract(
        env.kr, target_store_id=TARGET, target_root=env.root, cache_dir=env.cache,
        decision_id=out.decision["id"], keep=True,  # type: ignore[index]
    )
    assert done.reason == "copy_kept"
    meta, _ = env.copy()
    assert parse_raised_from(meta).watch == "ended"  # type: ignore[union-attr]
    assert meta["description"] == "Reworded here."  # the page is otherwise as it was
    assert env.kinds() == []
    assert [
        r["disposition"]
        for r in load_queue(env.kr, TARGET)
        if r["kind"] == rr.DECISION_KIND_RETRACT
    ] == ["kept"]
    later = env.retract(seq=3, mid=4)
    assert later.status == rr.IGNORED and later.reason == "copy_ended"
    assert disposition_of(env.cache, _mid(4)).reason == "copy_ended"  # type: ignore[union-attr]
    sup = env.supersede(seq=4, mid=5)
    assert sup.status == rr.IGNORED and sup.reason == "copy_ended"
    assert not env.gone()


def test_ac5_removing_an_edited_copy_on_the_answer_carries_the_commit_line(
    env: RetractEnv,
) -> None:
    _edit_projected(env)
    out = env.retract()
    done = rr.answer_retract(
        env.kr, target_store_id=TARGET, target_root=env.root, cache_dir=env.cache,
        decision_id=out.decision["id"], keep=False,  # type: ignore[index]
    )
    assert done.status == rr.APPLIED and env.gone()
    msg = subprocess.run(
        ["git", "-C", str(env.root), "log", "-1", "--format=%B", done.commit],
        capture_output=True, text=True, check=True,
    ).stdout
    assert f"Athenaeum-Raise: {RID} seq=2 retract" in msg
    assert [
        r["disposition"]
        for r in load_queue(env.kr, TARGET)
        if r["kind"] == rr.DECISION_KIND_RETRACT
    ] == ["removed"]


# athenaeum#3152 -- a retract is a removal path: an ``always`` rule protects the copy --


def _protect_copies(env: RetractEnv, rule_line: str = "| always | principle | policy | |") -> None:
    schema = env.kr / "wiki" / "_schema"
    schema.mkdir(parents=True, exist_ok=True)
    (schema / "types.md").write_text("| Type |\n|---|\n| principle |\n")
    (schema / "tags.md").write_text("| Tag |\n|---|\n| policy |\n")
    (schema / "relevance-policy.md").write_text(
        "| Verdict | Type | Tag | Subject |\n|---|---|---|---|\n" + rule_line + "\n"
    )


def test_retract_consults_the_shared_helper(
    env: RetractEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Any] = []

    def fake(knowledge_root: Path, candidates: list[Any], config: Any = None) -> list[Any]:
        seen.append((knowledge_root, [c.uid for c in candidates]))
        return []

    monkeypatch.setattr(rr, "find_policy_protected", fake)
    assert env.retract().status == rr.APPLIED
    assert seen == [(env.kr, [derived_uid(TARGET, RID)])]


def test_retract_keeps_a_policy_protected_copy_and_asks(env: RetractEnv) -> None:
    _protect_copies(env)
    head = env.subjects()
    out = env.retract()
    assert out.status == "asked" and out.decision
    assert not env.gone()
    assert env.subjects() == head  # nothing removed, nothing committed
    assert env.kinds() == [rr.DECISION_KIND_RETRACT]
    assert out.decision["detail"]["reason"] == "protected"
    assert "protected by rule 1" in out.decision["question"]


def test_retract_without_a_policy_removes_as_before(env: RetractEnv) -> None:
    assert not (env.kr / "wiki" / "_schema" / "relevance-policy.md").exists()
    assert env.retract().status == rr.APPLIED and env.gone()


def test_retract_ignores_a_rule_that_does_not_match_the_copy(env: RetractEnv) -> None:
    _protect_copies(env, "| always | principle | other | |")
    (env.kr / "wiki" / "_schema" / "tags.md").write_text("| Tag |\n|---|\n| other |\n")
    assert env.retract().status == rr.APPLIED and env.gone()


def test_retract_answered_remove_overrides_protection_and_records_it(env: RetractEnv) -> None:
    _protect_copies(env)
    out = env.retract()
    done = rr.answer_retract(
        env.kr, target_store_id=TARGET, target_root=env.root, cache_dir=env.cache,
        decision_id=out.decision["id"], keep=False,  # type: ignore[index]
    )
    assert done.status == rr.APPLIED and env.gone()
    msg = subprocess.run(
        ["git", "-C", str(env.root), "log", "-1", "--format=%B", done.commit],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "Forced past relevance-policy protection" in msg
    assert "protected by rule 1" in msg


def test_ac5_an_answer_needs_a_pending_retract_decision(env: RetractEnv) -> None:
    _edit_projected(env)
    out = env.retract()
    rr.answer_retract(
        env.kr, target_store_id=TARGET, target_root=env.root, cache_dir=env.cache,
        decision_id=out.decision["id"], keep=True,  # type: ignore[index]
    )
    with pytest.raises(RaiseAcceptRefused):
        rr.answer_retract(
            env.kr, target_store_id=TARGET, target_root=env.root, cache_dir=env.cache,
            decision_id=out.decision["id"], keep=True,  # type: ignore[index]
        )


# AC6 -- counter-example: a change outside the projection is still untouched ----


def test_ac6_a_change_outside_the_projection_leaves_the_copy_untouched_and_it_is_removed(
    env: RetractEnv,
) -> None:
    before = env.copy_path().read_text(encoding="utf-8")
    env.edit(lambda m, b: ({**m, "updated": "2026-11-19", "related": ["other"]}, b))
    assert env.copy_path().read_text(encoding="utf-8") != before  # whole page differs
    out = env.retract()
    assert out.status == rr.APPLIED
    assert env.gone()


# other behaviours ---------------------------------------------------------------


def test_redelivery_of_the_same_retract_is_a_duplicate(env: RetractEnv) -> None:
    env.retract()
    again = env.retract()
    assert again.status == "duplicate"


def test_a_retract_for_a_rejected_successor_is_applied_through_apply_supersede(
    env: RetractEnv,
) -> None:
    from athenaeum.raise_supersede import apply_supersede

    _reject(env, RID2)
    out = apply_supersede(
        env.kr, env.msg(KIND_SUPERSEDE, seq=2, message_id=_mid(1), successor=RID2),
        target_store_id=TARGET, target_root=env.root, cache_dir=env.cache,
        location="install_raw",
        apply_retract=lambda m: rr.apply_retract(
            env.kr, m, target_store_id=TARGET, target_root=env.root,
            cache_dir=env.cache, location="install_raw",
        ),
    )
    assert out.status == rr.APPLIED
    assert env.gone()
    assert [r.kind for r in iter_rows(env.cache)][-1] == KIND_RETRACT


# the store raw-intake handler -----------------------------------------------------


@pytest.fixture
def registered() -> Any:
    saved = dict(sid.DEFAULT_REGISTRY._handlers)
    rr.register_retract_handler()
    rr.register_retract_handler()  # idempotent
    yield
    sid.DEFAULT_REGISTRY._handlers.clear()
    sid.DEFAULT_REGISTRY._handlers.update(saved)


def test_the_retract_handler_is_called_from_a_stores_raw_intake(
    tmp_path: Path, registered: Any
) -> None:
    kb = Kb(tmp_path)
    # raise through the same dispatcher, accept it, then a retract arrives
    kb.drop(TARGET, "a.jsonl", kb.batch(kb.env.msg()))
    first = kb.run()
    assert first.handled == 1
    [decision] = load_queue(kb.kr, TARGET)
    kb.env.accept(decision["id"])
    retract = {
        "record": "retract", "raise_id": RID, "seq": 2, "message_id": _mid(3),
        "origin": ORIGIN, "target": TARGET, "proposer": PROPOSER, "via": "proposer",
        "sent_at": "2026-11-19T09:30:00Z", "reason": "withdrawn",
    }
    kb.drop(TARGET, "b.jsonl", kb.batch(retract))
    report = kb.run()
    assert report.unhandled == 0 and report.handled == 1
    assert not list(kb.env.root.glob(f"{derived_uid(TARGET, RID)}*.md"))
    rows = list(sid._ledger_rows(kb.cache))
    assert rows[-1]["kind"] == "retract" and rows[-1]["outcome"] == "handled"
    assert sid.DEFAULT_REGISTRY.get(KIND_RAISE) is not None
