# SPDX-License-Identifier: Apache-2.0
"""Raise S7a: accept a raised page, asking every time (athenaeum#2939)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from athenaeum import raise_acceptance as ra
from athenaeum.models import parse_frontmatter
from athenaeum.raise_acceptance import (
    RaiseAcceptRefused,
    accept_raise,
    run_checks,
    submit_raise,
)
from athenaeum.raise_acceptance_ledger import (
    DISPOSITION_ACCEPTED,
    Adjustment,
    disposition_of,
    iter_rows,
    record_acceptance,
)
from athenaeum.raise_messages import KIND_RAISE, RaiseMessage, render_message
from athenaeum.raise_projection import (
    Projection,
    derived_uid,
    payload_hash,
    render,
)
from athenaeum.raise_provenance import parse_raised_from
from athenaeum.store_decisions import collect_store_decisions, load_queue

ISS = "https://git.example.net"
PROPOSER = {"iss": ISS, "sub": "1203"}
STRANGER = {"iss": ISS, "sub": "9999"}
COMPILER = {"iss": ISS, "sub": "compiler"}
ORIGIN = "st_f2n6rb5hjc3t"
TARGET = "st_q7m2xk4vwz5d"
RID = "rs_k3v7t2hq4m4xw7pd5n6b"
MID = "rm_7d2c5w4kq4t3h6vx5pnb"
BODY = "Every employee files a PTO usage report by 31 December.\n\nSecond paragraph.\n"
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


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in GIT_ENV.items():
        monkeypatch.setenv(k, v)


def _declare(root: Path, **over: Any) -> None:
    decl: dict[str, Any] = {
        "version": 2,
        "store_id": TARGET,
        "roles": {
            "writers": {"rights": ["read", "propose"], "members": [PROPOSER]},
            "owners": {"rights": ["read", "compile", "answer"], "members": [COMPILER]},
        },
    }
    decl.update(over)
    (root / "_store").mkdir(parents=True, exist_ok=True)
    (root / "_store" / "access.yaml").write_text(yaml.safe_dump(decl), encoding="utf-8")


class Env:
    def __init__(self, tmp: Path) -> None:
        self.kr = tmp / "knowledge"
        self.root = tmp / "knowledge" / "target"
        self.cache = tmp / "cache"
        self.root.mkdir(parents=True)
        self.cache.mkdir()
        _declare(self.root)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "seed"], check=True)

    def msg(self, **over: Any) -> dict[str, Any]:
        page = dict(over.pop("page", PAGE))
        body = over.pop("body", BODY)
        fields: dict[str, Any] = dict(
            kind=KIND_RAISE,
            message_id=MID,
            origin=ORIGIN,
            target=TARGET,
            proposer=PROPOSER,
            sent_at="2026-11-18T09:30:00Z",
            raise_id=RID,
            seq=1,
            via="proposer",
            page=page,
            body=body,
        )
        fields["content_hash"] = payload_hash(render(Projection(meta=dict(page), body=body)))
        fields.update(over)
        return json.loads(render_message(RaiseMessage(**fields)))

    def submit(self, record: object, location: str = "install_raw") -> ra.Outcome:
        return submit_raise(
            self.kr,
            record,
            target_store_id=TARGET,
            target_root=self.root,
            cache_dir=self.cache,
            location=location,
        )

    def accept(self, decision_id: str, **kw: Any) -> ra.Outcome:
        return accept_raise(
            self.kr,
            target_store_id=TARGET,
            target_root=self.root,
            cache_dir=self.cache,
            decision_id=decision_id,
            **kw,
        )

    def copy_path(self) -> Path:
        hits = list(self.root.glob(f"{derived_uid(TARGET, RID)}*.md"))
        assert len(hits) == 1
        return hits[0]


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def _reasons(env: Env) -> list[str | None]:
    return [r.reason for r in iter_rows(env.cache)]


def _kinds(env: Env) -> list[str]:
    return [r["kind"] for r in load_queue(env.kr, TARGET)]


# AC1 -- the rule checks run first ------------------------------------------


def test_ac1_passing_message_runs_every_check_and_asks(env: Env) -> None:
    msg, reason = run_checks(
        env.msg(), target_store_id=TARGET, target_root=env.root, cache_dir=env.cache
    )
    assert msg is not None and reason is None
    out = env.submit(env.msg())
    assert out.status == "asked" and out.decision is not None
    assert _kinds(env) == ["raise"]
    # nothing was written to the store before anyone answered
    assert not list(env.root.glob("*.md"))


@pytest.mark.parametrize(
    "record, reason",
    [
        ("not a mapping", "unparseable"),
        ({"record": "raise"}, "unparseable"),
    ],
)
def test_ac1_unparseable_is_raised_as_a_decision(env: Env, record: object, reason: str) -> None:
    out = env.submit(record)
    assert (out.status, out.reason) == ("held", reason)
    assert _kinds(env) == ["raise_held"]
    assert load_queue(env.kr, TARGET)[0]["detail"]["reason"] == reason


def test_ac1_target_must_be_this_store(env: Env) -> None:
    out = env.submit(env.msg(target="st_zzzzzzzzzzzz"))
    assert out.reason == "target_mismatch"
    assert _reasons(env) == ["target_mismatch"]
    assert _kinds(env) == ["raise_held"]


def test_ac1_proposer_must_hold_propose_or_compile(env: Env) -> None:
    out = env.submit(env.msg(proposer=STRANGER))
    assert out.reason == "proposer_lacks_right"
    assert _reasons(env) == ["proposer_lacks_right"]
    assert _kinds(env) == ["raise_held"]
    # a reader alone is not enough
    _declare(
        env.root,
        roles={"readers": {"rights": ["read"], "members": [PROPOSER]}},
    )
    assert env.submit(env.msg(message_id="rm_aaaaaaaaaaaaaaaaaaaa")).reason == (
        "proposer_lacks_right"
    )


def test_ac1_compile_alone_suffices(env: Env) -> None:
    assert env.submit(env.msg(proposer=COMPILER)).status == "asked"


def test_ac1_no_declaration_fails_closed(env: Env) -> None:
    (env.root / "_store" / "access.yaml").unlink()
    assert env.submit(env.msg()).reason == "proposer_lacks_right"


@pytest.mark.parametrize(
    "body",
    ["See [[some-page]] for more.\n", "See [the page](other-page.md).\n"],
)
def test_ac1_payload_with_a_link_fails(env: Env, body: str) -> None:
    out = env.submit(env.msg(body=body))
    assert out.reason == "payload_has_link"
    assert _kinds(env) == ["raise_held"]


def test_ac1_payload_with_no_type_or_name_fails(env: Env) -> None:
    no_type = {k: v for k, v in PAGE.items() if k != "type"}
    assert env.submit(env.msg(page=no_type)).reason == "page_invalid"
    blank_name = dict(PAGE, name="  ")
    assert (
        env.submit(env.msg(page=blank_name, message_id="rm_aaaaaaaaaaaaaaaaaaaa")).reason
        == "page_invalid"
    )


def test_ac1_content_hash_must_match(env: Env) -> None:
    out = env.submit(env.msg(content_hash="sha256:" + "0" * 64))
    assert out.reason == "hash_mismatch"
    assert _reasons(env) == ["hash_mismatch"]


def test_ac1_rejected_raise_id_fails(env: Env) -> None:
    record_acceptance(
        env.cache,
        disposition="rejected",
        reason="declined",
        kind=KIND_RAISE,
        raise_id=RID,
        message_id="rm_bbbbbbbbbbbbbbbbbbbb",
        seq=1,
        origin=ORIGIN,
        target=TARGET,
        proposer=PROPOSER,
        location="install_raw",
    )
    out = env.submit(env.msg())
    assert out.reason == "already_rejected"
    assert _kinds(env) == ["raise_held"]


def test_ac1_duplicate_is_recorded_and_not_raised(env: Env) -> None:
    first = env.submit(env.msg())
    again = env.submit(env.msg())
    assert (again.status, again.reason) == ("duplicate", "duplicate")
    assert _kinds(env) == ["raise"]  # no second decision
    assert _reasons(env)[-1] == "duplicate"
    # the duplicate row does not replace the first delivery's disposition
    current = disposition_of(env.cache, MID)
    assert current is not None and current.reason is None
    assert first.status == "asked"


def test_ac1_checks_run_before_anyone_is_asked(env: Env) -> None:
    for bad in (env.msg(proposer=STRANGER), env.msg(target="st_zzzzzzzzzzzz")):
        env.submit(bad)
    assert "raise" not in _kinds(env)


# AC2 -- a raise that passes becomes a decision showing the payload ---------


def test_ac2_decision_shows_the_payload(env: Env) -> None:
    env.submit(env.msg())
    rec = load_queue(env.kr, TARGET)[0]
    assert rec["kind"] == "raise" and rec["disposition"] == "pending"
    assert PAGE["name"] in rec["question"]
    assert "31 December" in rec["question"]
    assert "Second paragraph." in rec["question"]
    assert rec["detail"]["message"]["raise_id"] == RID


def test_ac2_decision_is_listed_for_an_owner_who_may_answer(env: Env) -> None:
    env.submit(env.msg())
    items = collect_store_decisions(
        env.kr,
        {"owner": {"asserter": COMPILER}},
        COMPILER,
        mounts={TARGET: env.root},
    )
    assert [i["payload"]["kind"] for i in items] == ["raise"]
    assert items[0]["payload"]["detail"]["message"]["body"] == BODY


def test_ac2_person_may_accept_as_it_is(env: Env) -> None:
    out = env.submit(env.msg())
    done = env.accept(out.decision["id"])  # type: ignore[index]
    assert done.status == "accepted"
    assert load_queue(env.kr, TARGET)[0]["disposition"] == "accepted"


def test_ac2_person_may_adjust_type_and_tags_to_what_intake_accepts(env: Env) -> None:
    _declare(
        env.root,
        intake={
            "default": "refuse",
            "rules": [{"verdict": "accept", "type": "policy", "tag": "hr"}],
        },
    )
    out = env.submit(env.msg())
    # the decision says the rules refuse the payload as sent
    assert "refuse" in load_queue(env.kr, TARGET)[0]["question"]
    did = out.decision["id"]  # type: ignore[index]
    with pytest.raises(RaiseAcceptRefused) as exc:
        env.accept(did)
    assert exc.value.reason == "store_refuses"
    assert not list(env.root.glob("*.md"))
    with pytest.raises(RaiseAcceptRefused):
        env.accept(did, type="policy", tags=["finance"])  # still refused
    done = env.accept(did, type="policy", tags=["hr"])
    assert done.status == "accepted"
    meta, body = parse_frontmatter(env.copy_path().read_text(encoding="utf-8"))
    assert (meta["type"], meta["tags"]) == ("policy", ["hr"])
    assert body == BODY
    # the adjustment is recorded in the acceptance ledger
    row = disposition_of(env.cache, MID)
    assert row is not None and row.disposition == DISPOSITION_ACCEPTED
    assert row.adjustment == Adjustment(type="policy", tags=("hr",))


def test_ac2_intake_rules_accepting_as_sent_need_no_adjustment(env: Env) -> None:
    _declare(
        env.root,
        intake={"default": "refuse", "rules": [{"verdict": "accept", "type": "principle"}]},
    )
    out = env.submit(env.msg())
    assert "accept the type and tags as sent" in load_queue(env.kr, TARGET)[0]["question"]
    env.accept(out.decision["id"])  # type: ignore[index]
    row = disposition_of(env.cache, MID)
    assert row is not None and row.adjustment is None


# AC3 -- accept creates the copy ---------------------------------------------


def test_ac3_copy_at_derived_uid_body_unchanged_raised_from_and_commit_line(env: Env) -> None:
    out = env.submit(env.msg())
    done = env.accept(out.decision["id"])  # type: ignore[index]
    uid = derived_uid(TARGET, RID)
    assert done.uid == uid
    path = env.copy_path()
    assert path.name.startswith(uid)
    text = path.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(text)
    assert meta["uid"] == uid
    assert body == BODY  # unchanged
    assert meta["sources"] == [f"raise:{RID}"]
    block = parse_raised_from(meta)
    assert block is not None
    assert (block.raise_id, block.origin, block.via, block.seq, block.watch) == (
        RID, ORIGIN, "proposer", 1, "active",
    )
    assert block.proposer == PROPOSER
    assert block.raised_on.isoformat() == "2026-11-18"
    # content_hash is the hash of the copy as accepted
    assert block.content_hash == payload_hash(text)
    assert block.content_hash == env.msg()["content_hash"]  # no adjustment: as it arrived
    # the commit line, ids only
    log = subprocess.run(
        ["git", "-C", str(env.root), "log", "-1", "--format=%B"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert f"Athenaeum-Raise: {RID} seq=1 accept" in log.splitlines()
    assert done.commit is not None
    # only the copy was committed
    changed = subprocess.run(
        ["git", "-C", str(env.root), "show", "--name-only", "--format=", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    assert changed == [path.name]


def test_ac3_hash_after_adjustment_is_of_the_adjusted_copy(env: Env) -> None:
    out = env.submit(env.msg())
    env.accept(out.decision["id"], type="policy", tags=["hr", "pto"])  # type: ignore[index]
    path = env.copy_path()
    meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    block = parse_raised_from(meta)
    assert block is not None
    assert block.content_hash == payload_hash(path.read_text(encoding="utf-8"))
    assert block.content_hash != env.msg()["content_hash"]
    row = disposition_of(env.cache, MID)
    assert row is not None
    assert row.payload_hash == env.msg()["content_hash"]  # as it arrived
    assert row.adjustment == Adjustment(type="policy", tags=("hr", "pto"))


def test_ac3_ledger_row_has_ids_identity_location_time_and_blob(env: Env) -> None:
    out = env.submit(env.msg(), location="bench")
    env.accept(out.decision["id"])  # type: ignore[index]
    row = disposition_of(env.cache, MID)
    assert row is not None
    assert (row.raise_id, row.seq, row.origin, row.target) == (RID, 1, ORIGIN, TARGET)
    assert row.proposer == PROPOSER and row.location == "bench" and row.ts
    blob = subprocess.run(
        ["git", "-C", str(env.root), "rev-parse", f"HEAD:{env.copy_path().name}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert row.blob_id == blob


def test_ac3_copy_names_a_group_when_the_access_list_names_groups(env: Env) -> None:
    _declare(
        env.root,
        groups={"department-heads": [PROPOSER]},
        roles={
            "writers": {"rights": ["read", "propose"], "members": ["group:department-heads"]},
            "owners": {"rights": ["read", "compile", "answer"], "members": [COMPILER]},
        },
    )
    out = env.submit(env.msg())
    env.accept(out.decision["id"])  # type: ignore[index]
    meta, _ = parse_frontmatter(env.copy_path().read_text(encoding="utf-8"))
    block = parse_raised_from(meta)
    assert block is not None and block.proposer == "group:department-heads"
    # the individual is kept in the ledger
    row = disposition_of(env.cache, MID)
    assert row is not None and row.proposer == PROPOSER


def test_ac3_proposer_right_is_read_when_the_compile_role_acts(env: Env) -> None:
    out = env.submit(env.msg())
    _declare(env.root, roles={"owners": {"rights": ["read", "compile"], "members": [COMPILER]}})
    done = env.accept(out.decision["id"])  # type: ignore[index]
    assert (done.status, done.reason) == ("held", "proposer_lacks_right")
    assert not list(env.root.glob("*.md"))
    assert "raise_held" in _kinds(env)
    assert load_queue(env.kr, TARGET)[0]["disposition"] == "held"


def test_ac3_accepting_twice_is_refused(env: Env) -> None:
    out = env.submit(env.msg())
    did = out.decision["id"]  # type: ignore[index]
    env.accept(did)
    with pytest.raises(RaiseAcceptRefused):
        env.accept(did)
    assert len(list(env.root.glob("*.md"))) == 1


def test_ac3_a_crash_before_the_ledger_row_reconciles_without_rewriting(env: Env) -> None:
    out = env.submit(env.msg())
    did = out.decision["id"]  # type: ignore[index]
    env.accept(did)
    before = env.copy_path().read_bytes()
    # simulate: copy committed, ledger row and queue resolution lost
    (env.cache / "_raise_acceptance.jsonl").unlink()
    env.submit(env.msg(message_id="rm_cccccccccccccccccccc"))  # a re-delivery: a duplicate
    assert env.copy_path().read_bytes() == before


# AC4 -- counter-example: a taken uid is never overwritten --------------------


@pytest.mark.parametrize("carries", [None, "rs_zzzzzzzzzzzzzzzzzzzz"])
def test_ac4_taken_uid_is_never_overwritten_the_raise_is_held(
    env: Env, carries: str | None
) -> None:
    uid = derived_uid(TARGET, RID)
    other = env.root / f"{uid}-someone-elses-page.md"
    meta: dict[str, Any] = {"uid": uid, "type": "note", "name": "Someone else's page"}
    if carries is not None:
        meta["sources"] = [f"raise:{carries}"]
        meta["raised_from"] = {
            "v": 1, "raise_id": carries, "origin": ORIGIN, "proposer": PROPOSER,
            "via": "proposer", "content_hash": "sha256:" + "ab" * 32,
            "raised_on": "2026-11-01", "seq": 1,
        }
    original = "---\n" + yaml.safe_dump(meta, sort_keys=False) + "---\nOriginal body.\n"
    other.write_text(original, encoding="utf-8")

    out = env.submit(env.msg())  # held at submit
    assert (out.status, out.reason) == ("held", "uid_taken")
    assert other.read_text(encoding="utf-8") == original
    assert _kinds(env) == ["raise_held"]
    assert _reasons(env) == ["uid_taken"]


def test_ac4_uid_taken_after_the_decision_was_filed_is_held_on_accept(env: Env) -> None:
    out = env.submit(env.msg())
    uid = derived_uid(TARGET, RID)
    other = env.root / f"{uid}.md"
    other.write_text("---\nname: Taken\ntype: note\n---\nMine.\n", encoding="utf-8")
    done = env.accept(out.decision["id"])  # type: ignore[index]
    assert (done.status, done.reason) == ("held", "uid_taken")
    assert other.read_text(encoding="utf-8") == "---\nname: Taken\ntype: note\n---\nMine.\n"
    assert len(list(env.root.glob("*.md"))) == 1
    assert "raise_held" in _kinds(env)


def test_ac4_exclusive_create_never_replaces_a_file_that_appears_late(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = env.submit(env.msg())
    uid = derived_uid(TARGET, RID)
    real = ra._create_exclusive

    def racing(path: Path, text: str) -> None:
        # another writer lands a file at the very path between check and create
        path.write_text("theirs\n", encoding="utf-8")
        real(path, text)

    monkeypatch.setattr(ra, "_create_exclusive", racing)
    done = env.accept(out.decision["id"])  # type: ignore[index]
    assert (done.status, done.reason) == ("held", "uid_taken")
    (page,) = env.root.glob(f"{uid}*.md")
    assert page.read_text(encoding="utf-8") == "theirs\n"


# AC5 -- the compiling member's own relevance policy is never applied --------


def test_ac5_relevance_policy_is_never_consulted(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import athenaeum.relevance_policy as rp

    def boom(*a: object, **k: object) -> None:
        raise AssertionError("the compiling member's relevance policy was consulted")

    for name in (
        "load_relevance_policy",
        "parse_relevance_policy",
        "gate_raw_content",
        "filter_relevance_never",
        "policy_vocabulary",
    ):
        monkeypatch.setattr(rp, name, boom)
    out = env.submit(env.msg())
    done = env.accept(out.decision["id"])  # type: ignore[index]
    assert done.status == "accepted"


def test_ac5_a_policy_that_would_refuse_the_type_does_not_stop_acceptance(
    env: Env,
) -> None:
    wiki = env.kr / "wiki"
    wiki.mkdir()
    (wiki / "_relevance_policy.md").write_text(
        "| verdict | type | tag |\n|---|---|---|\n| never | principle | policy |\n",
        encoding="utf-8",
    )
    out = env.submit(env.msg())
    assert env.accept(out.decision["id"]).status == "accepted"  # type: ignore[index]


def test_module_does_not_import_the_relevance_policy() -> None:
    import ast

    tree = ast.parse(Path(ra.__file__).read_text(encoding="utf-8"))
    imported = {
        n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module
    } | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert "athenaeum.relevance_policy" not in imported


# Out-of-scope guard ---------------------------------------------------------


def test_no_precedence_is_assigned_to_a_raised_copy() -> None:
    import ast

    tree = ast.parse(Path(ra.__file__).read_text(encoding="utf-8"))
    imported = {
        n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module
    }
    assert "athenaeum.precedence" not in imported


# Additive helpers -----------------------------------------------------------


def test_resolved_decision_is_no_longer_pending(env: Env) -> None:
    from athenaeum.store_decisions import resolve_decision

    out = env.submit(env.msg())
    did = out.decision["id"]  # type: ignore[index]
    resolve_decision(env.kr, store_id=TARGET, decision_id=did, disposition="accepted")
    (rec,) = load_queue(env.kr, TARGET)
    assert rec["disposition"] == "accepted" and "resolves" not in rec
    with pytest.raises(ValueError):
        resolve_decision(env.kr, store_id=TARGET, decision_id=did, disposition="pending")


def test_has_link() -> None:
    from athenaeum.raise_projection import has_link

    assert has_link("see [[page]]") and has_link("[x](a.md)")
    assert not has_link("plain text") and not has_link("[x](https://example.net)")


def test_a_failed_commit_leaves_no_copy_and_nothing_staged(env: Env) -> None:
    # Review on PR athenaeum#2984: when git refuses the commit, the copy is
    # removed AND unstaged, so no later whole-index commit picks it up.
    out = env.submit(env.msg())
    hooks = env.root.parent / "refusing-hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    subprocess.run(
        ["git", "-C", str(env.root), "config", "core.hooksPath", str(hooks)], check=True
    )
    with pytest.raises(subprocess.CalledProcessError):
        env.accept(out.decision["id"])  # type: ignore[index]
    assert not list(env.root.glob(f"{derived_uid(TARGET, RID)}*.md"))
    staged = subprocess.run(
        ["git", "-C", str(env.root), "diff", "--cached", "--name-only"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert staged == ""
