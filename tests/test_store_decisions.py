# SPDX-License-Identifier: Apache-2.0
"""A decision queue per store, one listing across stores (athenaeum#2934)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from athenaeum import store_decisions as sd
from athenaeum.decisions import list_pending_decisions
from athenaeum.store_decisions import (
    PageQuote,
    PageRef,
    StoreDecisionRefused,
    file_decision,
    load_queue,
    queue_path,
    render_item,
)

ISS = "https://git.example.net"
ME = {"iss": ISS, "sub": "me"}
OTHER = {"iss": ISS, "sub": "someone-else"}
ALPHA = "st_aaaaaaaaaaaa"
BRAVO = "st_bbbbbbbbbbbb"
CHARLIE = "st_cccccccccccc"


def _declare(root: Path, store_id: str, roles: dict) -> None:
    (root / "_store").mkdir(parents=True, exist_ok=True)
    (root / "_store" / "access.yaml").write_text(
        yaml.safe_dump({"version": 1, "store_id": store_id, "roles": roles}), encoding="utf-8"
    )


def _roles(rights: list[str], who: dict = ME) -> dict:
    return {"r": {"rights": rights, "members": [who]}}


def _page(root: Path, uid: str, title: str, desc: str) -> Path:
    path = root / f"{uid}-{title.lower().replace(' ', '-')}.md"
    path.write_text(f"---\nname: {title}\ndescription: {desc}\n---\nbody\n", encoding="utf-8")
    return path


@pytest.fixture
def kr(tmp_path: Path) -> Path:
    """Knowledge root with three declared surfaces: alpha (answer), bravo (answer), charlie."""
    root = tmp_path / "knowledge"
    (root / "wiki").mkdir(parents=True)
    surfaces = {}
    for name in ("alpha", "bravo", "charlie"):
        (root / name).mkdir()
        surfaces[f"s-{name}"] = {"backing_store": "markdown", "surface_root": name}
    (root / "athenaeum.yaml").write_text(
        yaml.safe_dump({"owner": {"asserter": ME}, "storage": {"adapters": surfaces}}),
        encoding="utf-8",
    )
    _declare(root / "alpha", ALPHA, _roles(["read", "compile", "answer"]))
    _declare(root / "bravo", BRAVO, _roles(["read", "compile", "answer"]))
    # The owner holds read and compile but NOT answer on charlie.
    _declare(root / "charlie", CHARLIE, _roles(["read", "compile"]))
    return root


def _mounts(kr: Path) -> dict[str, Path]:
    return {ALPHA: kr / "alpha", BRAVO: kr / "bravo", CHARLIE: kr / "charlie"}


# AC1 ---------------------------------------------------------------------


def test_decision_is_stored_with_exactly_one_owning_store_in_separate_files(kr: Path) -> None:
    a = file_decision(kr, store_id=ALPHA, question="Merge these?", pages=[
        PageQuote(ALPHA, "a1", "Page A1", "gist")])
    b = file_decision(kr, store_id=BRAVO, question="Fix this?", pages=[
        PageQuote(BRAVO, "b1", "Page B1", "gist")])
    assert queue_path(kr, ALPHA) != queue_path(kr, BRAVO)
    assert sd.stores_with_queue(kr) == [ALPHA, BRAVO]
    assert [r["id"] for r in load_queue(kr, ALPHA)] == [a["id"]]
    assert [r["id"] for r in load_queue(kr, BRAVO)] == [b["id"]]
    assert a["store_id"] == ALPHA and b["store_id"] == BRAVO
    # Each file holds only its own store's records.
    for sid in (ALPHA, BRAVO):
        for line in queue_path(kr, sid).read_text().splitlines():
            assert json.loads(line)["store_id"] == sid


def test_queue_path_refuses_a_value_that_is_not_a_store_id(kr: Path) -> None:
    for bad in ("../x", "personal", "st_short", ""):
        with pytest.raises(StoreDecisionRefused):
            queue_path(kr, bad)


def test_filing_is_refused_without_compile(kr: Path) -> None:
    other = {"iss": ISS, "sub": "nobody"}
    with pytest.raises(StoreDecisionRefused):
        file_decision(kr, store_id=ALPHA, question="q", mounts=_mounts(kr), asserter=other)
    with pytest.raises(StoreDecisionRefused):
        file_decision(kr, store_id="st_dddddddddddd", question="q", mounts=_mounts(kr), asserter=ME)
    assert file_decision(kr, store_id=ALPHA, question="q", mounts=_mounts(kr), asserter=ME)


# AC2 ---------------------------------------------------------------------


def test_item_quotes_only_owning_store_and_records_others_by_id_and_uid(kr: Path) -> None:
    rec = file_decision(kr, store_id=ALPHA, question="Do these contradict?", pages=[
        PageQuote(ALPHA, "a1", "Alpha Page", "alpha gist", "pages/a1-alpha-page.md"),
        PageQuote(BRAVO, "b1", "SECRET TITLE", "SECRET GIST", "private/secret-path.md"),
    ])
    assert rec["pages"] == [
        {"uid": "a1", "title": "Alpha Page", "gist": "alpha gist", "path": "pages/a1-alpha-page.md"}
    ]
    assert rec["other_pages"] == [{"store_id": BRAVO, "uid": "b1"}]
    on_disk = queue_path(kr, ALPHA).read_text()
    for leaked in ("SECRET TITLE", "SECRET GIST", "secret-path"):
        assert leaked not in on_disk


def test_stored_paths_are_never_absolute_or_climbing(kr: Path) -> None:
    rec = file_decision(kr, store_id=ALPHA, question="q", pages=[
        PageQuote(ALPHA, "a1", "T", "g", str(kr / "alpha" / "a1.md")),
        PageQuote(ALPHA, "a2", "T", "g", "../escape.md"),
    ])
    assert [p["path"] for p in rec["pages"]] == ["", ""]


def test_pageref_must_name_another_store_and_uid_must_be_safe(kr: Path) -> None:
    with pytest.raises(StoreDecisionRefused):
        file_decision(kr, store_id=ALPHA, question="q", pages=[PageRef(ALPHA, "a1")])
    with pytest.raises(StoreDecisionRefused):
        file_decision(kr, store_id=ALPHA, question="q", pages=[PageRef(BRAVO, "../x")])
    rec = file_decision(kr, store_id=ALPHA, question="q", pages=[
        PageRef(BRAVO, "b1"), PageRef(BRAVO, "b1")])
    assert rec["other_pages"] == [{"store_id": BRAVO, "uid": "b1"}]


# AC3 ---------------------------------------------------------------------


def test_other_store_page_is_filled_in_from_the_viewers_own_mount(kr: Path) -> None:
    _page(kr / "bravo", "b1", "Bravo Page", "what bravo says")
    rec = file_decision(kr, store_id=ALPHA, question="q", pages=[PageQuote(BRAVO, "b1")])
    item = render_item(rec, _mounts(kr), ME)
    assert item["payload"]["other_pages"] == [
        {"store_id": BRAVO, "uid": "b1", "readable": True,
         "title": "Bravo Page", "gist": "what bravo says"}
    ]


def test_viewer_without_the_other_store_sees_only_that_an_unreadable_page_is_involved(
    kr: Path,
) -> None:
    _page(kr / "bravo", "b1", "Bravo Page", "what bravo says")
    rec = file_decision(kr, store_id=ALPHA, question="q", pages=[PageQuote(BRAVO, "b1")])
    # Not mounted at all.
    unmounted = {ALPHA: kr / "alpha"}
    shown = render_item(rec, unmounted, ME)["payload"]["other_pages"]
    assert shown == [{"store_id": BRAVO, "uid": "b1", "readable": False}]
    # Mounted, but the viewer holds no read right there.
    _declare(kr / "bravo", BRAVO, _roles(["read"], who=OTHER))
    shown = render_item(rec, _mounts(kr), ME)["payload"]["other_pages"]
    assert shown == [{"store_id": BRAVO, "uid": "b1", "readable": False}]
    assert "Bravo Page" not in json.dumps(shown)


def test_missing_page_in_a_readable_mount_stays_unreadable(kr: Path) -> None:
    rec = file_decision(kr, store_id=ALPHA, question="q", pages=[PageQuote(BRAVO, "nope")])
    shown = render_item(rec, _mounts(kr), ME)["payload"]["other_pages"]
    assert shown == [{"store_id": BRAVO, "uid": "nope", "readable": False}]


# AC4 / AC5 ---------------------------------------------------------------


def _personal_item(kr: Path) -> None:
    from athenaeum.answers import raise_pending_question

    raise_pending_question(
        kr / "wiki" / "_pending_questions.md", "personal q?", "some context", entity="E"
    )


def test_listing_spans_personal_and_answerable_stores_and_names_each_store(kr: Path) -> None:
    _personal_item(kr)
    file_decision(kr, store_id=ALPHA, question="alpha q", created_at="2026-01-01T00:00:00Z")
    file_decision(kr, store_id=BRAVO, question="bravo q", created_at="2026-01-02T00:00:00Z")
    items = list_pending_decisions(kr / "wiki")
    assert all("store_id" in i for i in items)
    assert {i["store_id"] for i in items} == {"personal", ALPHA, BRAVO}
    store_items = [i for i in items if i["type"] == "store-decision"]
    assert [i["summary"] for i in store_items] == ["alpha q", "bravo q"]
    assert [i["store_id"] for i in items if i["type"] != "store-decision"] == ["personal"]


def test_store_without_answer_contributes_no_items(kr: Path) -> None:
    file_decision(kr, store_id=ALPHA, question="alpha q")
    # Charlie holds compile for this owner, so a decision can be filed, but
    # the owner holds no answer there.
    file_decision(kr, store_id=CHARLIE, question="charlie q", mounts=_mounts(kr), asserter=ME)
    items = list_pending_decisions(kr / "wiki")
    assert {i["store_id"] for i in items} == {ALPHA}
    assert "charlie q" not in json.dumps(items)


def test_collect_never_opens_the_queue_of_a_store_without_answer(
    kr: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_decision(kr, store_id=CHARLIE, question="charlie q")
    opened: list[str] = []
    real = sd.load_queue

    def spy(root: Path, sid: str) -> list[dict]:
        opened.append(sid)
        return real(root, sid)

    monkeypatch.setattr(sd, "load_queue", spy)
    sd.collect_store_decisions(kr, None, ME, mounts=_mounts(kr))
    assert CHARLIE not in opened


def test_store_queue_is_withheld_from_a_restricted_caller(kr: Path) -> None:
    file_decision(kr, store_id=ALPHA, question="alpha q")
    assert list_pending_decisions(kr / "wiki", caller_audience={"someone"}) == []


def test_listing_is_unchanged_when_no_store_is_declared(tmp_path: Path) -> None:
    root = tmp_path / "k"
    (root / "wiki").mkdir(parents=True)
    assert list_pending_decisions(root / "wiki") == []


def test_filing_appends_and_never_rewrites_earlier_lines(kr: Path) -> None:
    # Review on PR athenaeum#2980: filing is one O_APPEND write, so a record a
    # concurrent filer wrote in between is kept, and a torn trailing line is
    # skipped rather than lost-and-rewritten.
    first = file_decision(kr, store_id=ALPHA, question="one")
    path = sd.queue_path(kr, ALPHA)
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"torn": \n')
    second = file_decision(kr, store_id=ALPHA, question="two")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["id"] == first["id"]
    assert lines[1] == '{"torn": '
    assert [r["id"] for r in sd.load_queue(kr, ALPHA)] == [first["id"], second["id"]]
