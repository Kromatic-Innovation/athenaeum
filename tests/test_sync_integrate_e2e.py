# SPDX-License-Identifier: Apache-2.0
"""One whole integrate run across two clones of a store (athenaeum#3085).

The unit files each pin one rule of ``sync.mode: integrate`` on one clone. This
module drives a single scenario through the same entry points a librarian run
uses (``_maybe_pull_before_run`` then ``_maybe_push_after_run``) on BOTH installs
and asserts the final state of both: a page changed on both sides is held, two
pages that share a uid are renumbered, and an erasure mark propagates. The
remote is a local bare directory; no model is called. People are invented.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from athenaeum import librarian
from athenaeum.store_decisions import load_queue
from athenaeum.sync_integrate import DECISION_KIND_HELD_PAGE
from tests.fixtures.two_clone_harness import Install, TwoCloneHarness, git, make_two_clone_harness
from tests.test_sync_held_decision import DECLARATION, ME, STORE
from tests.test_sync_integrate import page

TWIN = "a1b2c3d4"
JORDAN = "person-jordan-reyes"
KATE = "person-kate-lindqvist"
HEX16 = re.compile(r"^[0-9a-f]{16}$")

JORDAN_BASE = page(JORDAN, "Jordan Reyes")
KATE_BASE = page(KATE, "Kate Lindqvist")


class Run:
    """One install's librarian-side wiring: its own knowledge root and config."""

    def __init__(self, tmp_path: Path, inst: Install) -> None:
        self.inst = inst
        self.kr = tmp_path / f"kr-{inst.name}"
        self.kr.mkdir()
        self.config: dict[str, Any] = {
            "owner": {"asserter": ME},
            "storage": {
                "adapters": {
                    "pages": {
                        "backing_store": "markdown",
                        "surface_root": str(inst.root / "wiki"),
                    }
                }
            },
        }
        (self.kr / "athenaeum.yaml").write_text(yaml.safe_dump(self.config), encoding="utf-8")

    def sync(self) -> librarian.SurfaceSyncState:
        state = librarian.SurfaceSyncState()
        librarian._maybe_pull_before_run(
            self.kr, config=self.config, pull_before_run=True, dry_run=False, surface_sync=state
        )
        librarian._maybe_push_after_run(
            self.kr,
            config=self.config,
            push_after_run=True,
            dry_run=False,
            head_at_start=None,
            surface_sync=state,
        )
        return state

    def queue(self) -> list[dict]:
        return load_queue(self.kr, STORE)

    def wiki(self) -> Path:
        return self.inst.root / "wiki"


def _tree(inst: Install) -> str:
    return git(inst.root, "rev-parse", "HEAD^{tree}")


def _outcomes(state: librarian.SurfaceSyncState) -> dict[str, str]:
    return {r["op"]: r["outcome"] for r in state.results if r["surface"] == "pages"}


def test_a_full_integrate_run_holds_renumbers_and_erases_and_both_clones_converge(
    tmp_path: Path,
) -> None:
    (tmp_path / "h").mkdir()
    h: TwoCloneHarness = make_two_clone_harness(
        tmp_path / "h",
        seed={
            "wiki/jordan.md": JORDAN_BASE,
            "wiki/kate.md": KATE_BASE,
            "wiki/_store/access.yaml": DECLARATION,
        },
    )
    first, second = Run(tmp_path, h.first), Run(tmp_path, h.second)

    # first install: a conflicting edit, a new page with the shared uid, an erasure
    h.first.commit("wiki/jordan.md", JORDAN_BASE.replace("Acme Corp.", "Globex."))
    h.first.commit(f"wiki/{TWIN}-priya-raman.md", page(TWIN, "Priya Raman"))
    from athenaeum.erasure import erase_pages_and_commit

    assert erase_pages_and_commit(h.first.root, [(h.first.root / "wiki/kate.md", KATE)])
    # second install: the conflicting edit, a different page under the same uid,
    # and an edit of the page the first install erased
    h.second.commit("wiki/jordan.md", JORDAN_BASE.replace("Acme Corp.", "Initech."))
    h.second.commit(f"wiki/{TWIN}-sam-ortiz.md", page(TWIN, "Sam Ortiz"))
    h.second.commit("wiki/kate.md", KATE_BASE.replace("Acme Corp.", "Acme Corp, in Lisbon."))
    second_before = h.second.head()

    # run 1: first install syncs (nothing to integrate) and publishes
    out = _outcomes(first.sync())
    assert out["push"] == "ok"
    assert h.remote_tip() == h.first.head()

    # run 2: second install integrates, then pushes the merge
    state = second.sync()
    out = _outcomes(state)
    assert out["integrate"] == "ok" and out["push"] == "ok"
    assert state.as_profile_fields()["uid_reminted"] == 1
    parents = git(h.second.root, "rev-list", "--parents", "-n1", "HEAD").split()[1:]
    assert parents[0] == second_before and len(parents) == 2
    assert h.remote_tip() == h.second.head()

    # run 3: first install fast-forwards onto the merge
    out = _outcomes(first.sync())
    assert out["integrate"] == "ok"
    assert h.first.head() == h.second.head() == h.remote_tip()
    assert _tree(h.first) == _tree(h.second)

    for run in (first, second):
        wiki = run.wiki()
        # held: the remote version stays on disk, the local one is only in history
        jordan = (wiki / "jordan.md").read_text()
        assert "Globex" in jordan and "Initech" not in jordan
        assert "<<<<<<<" not in jordan
        # renumbered: the remote page keeps the uid, the local one is re-minted
        assert f"uid: {TWIN}" in (wiki / f"{TWIN}-priya-raman.md").read_text()
        assert not (wiki / f"{TWIN}-sam-ortiz.md").exists()
        (renamed,) = [p for p in wiki.glob("*-sam-ortiz.md")]
        new_uid = renamed.name.removesuffix("-sam-ortiz.md")
        assert HEX16.match(new_uid) and f"uid: {new_uid}" in renamed.read_text()
        # erased: gone from both clones despite the local edit
        assert not (wiki / "kate.md").exists()

    # the merge commit records all three outcomes, ids only
    message = git(h.second.root, "log", "-1", "--format=%B")
    assert f"Athenaeum-Erasure: {KATE}" in message.splitlines()
    assert f"Athenaeum-Uid-Reminted: {TWIN} {new_uid}" in message.splitlines()
    assert "resolver=held reason=contested" in message
    for forbidden in ("Kate Lindqvist", "Lisbon", "Initech", "Globex", "Sam Ortiz"):
        assert forbidden not in message

    # the held page is a pending decision in the integrating install's own queue
    (rec,) = second.queue()
    assert rec["kind"] == DECISION_KIND_HELD_PAGE and rec["disposition"] == "pending"
    assert rec["detail"]["uid"] == JORDAN and rec["detail"]["reasons"] == ["contested"]
    for blob in (rec["detail"]["base"], rec["detail"]["local"], rec["detail"]["remote"]):
        assert git(h.second.root, "cat-file", "-t", blob) == "blob"
    assert first.queue() == []  # the other install integrated nothing, so filed nothing

    # a further run on both installs is a no-op: nothing new is held or renumbered
    for run in (second, first):
        again = run.sync()
        assert _outcomes(again)["integrate"] == "ok"
        assert again.as_profile_fields().get("uid_reminted", 0) == 0
    assert h.first.head() == h.second.head() == h.remote_tip()
    assert len(second.queue()) == 1
