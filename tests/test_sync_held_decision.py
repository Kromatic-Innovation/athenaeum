# SPDX-License-Identifier: Apache-2.0
"""The held-page decision kind in the store's decision queue (athenaeum#2995).

Two installs are built by the two-clone harness under ``tmp_path``; the remote is a
local bare directory and no model is called. All people and organisations are invented.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from athenaeum import librarian, sync_integrate
from athenaeum.decisions import list_pending_decisions
from athenaeum.models import render_frontmatter
from athenaeum.store_decisions import load_queue, queue_path, resolve_mounts
from athenaeum.sync_integrate import (
    DECISION_KIND_HELD_PAGE,
    HeldDecisionRefused,
    HeldItem,
    PageRoot,
    SyncSettings,
    answer_held_decision,
    integrate_remote,
    make_held_filer,
    push_after_compile,
)
from tests.fixtures.two_clone_harness import TwoCloneHarness, git, make_two_clone_harness

STORE = "st_q7m2xk4vwz5d"
ISS = "https://git.example.net"
ME = {"iss": ISS, "sub": "me"}
UID = "person-jordan-reyes"


def page(uid: str, name: str, body: str = "") -> str:
    meta: dict[str, Any] = {"uid": uid, "type": "person", "name": name, "access": "internal"}
    return render_frontmatter(meta) + (
        body or f"# {name}\n\n## Summary\n\n{name} works at Acme Corp.\n"
    )


BASE = page(UID, "Jordan Reyes")
GLOBEX = BASE.replace("works at Acme Corp.", "works at Globex.")
INITECH = BASE.replace("works at Acme Corp.", "works at Initech.")
DECLARATION = yaml.safe_dump(
    {
        "version": 1,
        "store_id": STORE,
        "roles": {"owner": {"rights": ["read", "compile", "answer"], "members": [ME]}},
        "sync": {"mode": "integrate"},
    }
)


class Env:
    def __init__(self, tmp_path: Path) -> None:
        (tmp_path / "h").mkdir()
        self.h: TwoCloneHarness = make_two_clone_harness(
            tmp_path / "h", seed={"wiki/jordan.md": BASE, "wiki/_store/access.yaml": DECLARATION}
        )
        self.kr = tmp_path / "kr"
        self.kr.mkdir()
        self.config: dict[str, Any] = {
            "owner": {"asserter": ME},
            "storage": {
                "adapters": {
                    "pages": {
                        "backing_store": "markdown",
                        "surface_root": str(self.h.second.root / "wiki"),
                    }
                }
            },
        }
        (self.kr / "athenaeum.yaml").write_text(yaml.safe_dump(self.config), encoding="utf-8")

    def settings(self) -> SyncSettings:
        filer = make_held_filer(
            self.h.second.root,
            (PageRoot("wiki"),),
            knowledge_root=self.kr,
            store_id=STORE,
            mounts=resolve_mounts(self.kr, self.config),
            asserter=ME,
        )
        return SyncSettings(page_roots=(PageRoot("wiki"),), sleep=lambda _s: None, file_held=filer)

    def contested(self) -> None:
        self.h.first.commit("wiki/jordan.md", GLOBEX)
        self.h.second.commit("wiki/jordan.md", INITECH)
        assert push_after_compile(self.h.first.root, "main", SyncSettings()).ok

    def queue(self) -> list[dict]:
        return load_queue(self.kr, STORE)

    def pages_text(self) -> str:
        return (self.h.second.root / "wiki" / "jordan.md").read_text()


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def _page_line(env: Env) -> str:
    message = git(env.h.second.root, "log", "-1", "--format=%B")
    (line,) = [ln for ln in message.splitlines() if ln.startswith("Athenaeum-Page:")]
    return line


# -- AC1, AC2: filed, with ids only, and the merge line carries the decision id --


def test_a_held_page_is_filed_in_the_owning_stores_queue_and_the_line_names_it(env: Env) -> None:
    env.contested()
    result = integrate_remote(env.h.second.root, "main", env.settings())
    assert result.status == "merged"
    (rec,) = env.queue()
    assert rec["kind"] == DECISION_KIND_HELD_PAGE and rec["store_id"] == STORE
    assert rec["disposition"] == "pending"
    d = rec["detail"]
    assert d["uid"] == UID and d["reasons"] == ["contested"]
    assert d["units"] and all(isinstance(u, str) for u in d["units"])
    assert (d["base"], d["local"], d["remote"]) == (
        result.held[0].base,
        result.held[0].local,
        result.held[0].remote,
    )
    for blob in (d["base"], d["local"], d["remote"]):
        assert git(env.h.second.root, "cat-file", "-t", blob) == "blob"
    assert result.held[0].decision == rec["id"]
    assert _page_line(env).endswith(f" decision={rec['id']}")
    assert re.match(r"^Athenaeum-Page: \S+ resolver=held reason=contested ", _page_line(env))
    # only this store's page is quoted; nothing from the page text but title and gist
    assert rec["other_pages"] == [] and [p["uid"] for p in rec["pages"]] == [UID]
    assert "Initech" not in queue_path(env.kr, STORE).read_text()


# -- AC3: one listing, with the store ---------------------------------------


def test_the_decision_is_in_the_one_listing_with_its_store(env: Env) -> None:
    env.contested()
    integrate_remote(env.h.second.root, "main", env.settings())
    items = [i for i in list_pending_decisions(env.kr / "wiki") if i.get("store_id") == STORE]
    assert [i["payload"]["kind"] for i in items] == [DECISION_KIND_HELD_PAGE]
    assert items[0]["payload"]["detail"]["answers"] == ["keep-remote", "take-local", "merged"]


# -- AC4: an answer is one ordinary new commit; the published version stays until then


@pytest.mark.parametrize("answer", ["keep-remote", "take-local", "merged"])
def test_an_answer_is_applied_as_one_new_commit_and_closes_the_decision(
    env: Env, answer: str
) -> None:
    env.contested()
    integrate_remote(env.h.second.root, "main", env.settings())
    assert "Globex" in env.pages_text()  # the published version stays until answered
    head = env.h.second.head()
    (rec,) = env.queue()
    merged = GLOBEX.replace("works at Globex.", "works at Globex and Initech.")
    sha = answer_held_decision(
        env.h.second.root,
        env.kr,
        store_id=STORE,
        decision_id=rec["id"],
        answer=answer,
        text=merged if answer == "merged" else None,
        mounts=resolve_mounts(env.kr, env.config),
        asserter=ME,
    )
    assert git(env.h.second.root, "rev-parse", "HEAD") == sha
    assert git(env.h.second.root, "rev-list", "--parents", "-n1", sha).split()[1:] == [head]
    expected = {"keep-remote": GLOBEX, "take-local": INITECH, "merged": merged}[answer]
    assert env.pages_text() == expected
    assert env.queue()[0]["disposition"] == f"answered-{answer}"
    assert [i for i in list_pending_decisions(env.kr / "wiki") if i.get("store_id") == STORE] == []
    assert push_after_compile(env.h.second.root, "main", SyncSettings()).ok


def test_an_answer_is_refused_when_the_page_moved_or_the_text_is_unusable(env: Env) -> None:
    env.contested()
    integrate_remote(env.h.second.root, "main", env.settings())
    (rec,) = env.queue()
    kw: dict[str, Any] = {"store_id": STORE, "decision_id": rec["id"]}
    with pytest.raises(HeldDecisionRefused) as bad:
        answer_held_decision(env.h.second.root, env.kr, answer="nonsense", **kw)
    assert bad.value.code == "bad_answer"
    with pytest.raises(HeldDecisionRefused) as no_text:
        answer_held_decision(
            env.h.second.root, env.kr, answer="merged", text="no frontmatter", **kw
        )
    assert no_text.value.code == "invalid_text"
    env.h.second.commit("wiki/jordan.md", GLOBEX + "\nlater edit\n")
    head = env.h.second.head()
    with pytest.raises(HeldDecisionRefused) as stale:
        answer_held_decision(env.h.second.root, env.kr, answer="take-local", **kw)
    assert stale.value.code == "stale"
    assert env.h.second.head() == head and env.queue()[0]["disposition"] == "pending"


# -- AC5: deferred is not filed -----------------------------------------------


def test_a_deferred_page_is_not_filed(env: Env) -> None:
    seen: list[HeldItem] = []
    filer = make_held_filer(
        env.h.second.root, (PageRoot("wiki"),), knowledge_root=env.kr, store_id=STORE
    )

    def spy(item: HeldItem, title: str, gist: str) -> str | None:
        seen.append(item)
        return filer(item, title, gist)

    held = [HeldItem("page", ("deferred",), UID, path="wiki/jordan.md")]
    lines = [(UID, "resolver=held reason=deferred base=a local=b remote=c result=c")]
    out = sync_integrate._file_held_pages(env.h.second.root, "HEAD", held, lines, spy)
    assert seen == [] and env.queue() == [] and out == held
    assert "decision=" not in lines[0][1]


# -- AC6: a page settled by rule files nothing ---------------------------------


def test_a_page_settled_by_rule_produces_no_decision(env: Env) -> None:
    env.h.first.commit(
        "wiki/jordan.md", BASE.replace("name: Jordan Reyes", "name: Jordan Reyes\nstage: lead")
    )
    env.h.second.commit("wiki/jordan.md", BASE.replace("Acme Corp.", "Acme Corp, Lisbon."))
    assert push_after_compile(env.h.first.root, "main", SyncSettings()).ok
    result = integrate_remote(env.h.second.root, "main", env.settings())
    assert result.status == "merged" and result.held == ()
    assert env.queue() == []
    assert "decision=" not in git(env.h.second.root, "log", "-1", "--format=%B")


# -- AC7: filing twice does not duplicate --------------------------------------


def test_the_same_held_page_filed_twice_is_one_decision(env: Env) -> None:
    env.contested()
    first = integrate_remote(env.h.second.root, "main", env.settings())
    # A second run before it is answered, with the remote unchanged: the same page held again.
    held = first.held[0]
    filer = env.settings().file_held
    assert filer is not None
    again = filer(held, "Jordan Reyes", "")
    assert again == held.decision
    assert len(env.queue()) == 1


# -- wiring: a librarian run files it -------------------------------------------


def test_a_librarian_run_files_the_decision(env: Env) -> None:
    env.contested()
    state = librarian.SurfaceSyncState()
    librarian._maybe_pull_before_run(
        env.kr, config=env.config, pull_before_run=True, dry_run=False, surface_sync=state
    )
    (rec,) = env.queue()
    assert rec["kind"] == DECISION_KIND_HELD_PAGE and rec["detail"]["uid"] == UID
    assert _page_line(env).endswith(f" decision={rec['id']}")
