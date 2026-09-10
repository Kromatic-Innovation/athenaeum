"""Tests for the uid-to-page join and the /open boundary (athenaeum#1528).

The refusal tests are the point of this file. ``/open`` launches a process on
the operator's machine, and anything running in their browser can reach
``127.0.0.1`` -- so "it opens the right file" is the easy half, and "it refuses
everything else" is the half that has to be pinned down.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from athenaeum import _cmd_viewer
from athenaeum.viewer_corpus import build_uid_index, load_page_info, resolve_path


def _page(root: Path, uid: str, slug: str, *, name: str, desc: str = "", related: str = "") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    body = ["---", f"uid: {uid}", f"name: {name}"]
    if desc:
        body.append(f'description: "{desc}"')
    if related:
        body.append("related:")
        body.append(f"  - uid: {related}")
        body.append("    role: parent")
    body += ["---", "", "# body", ""]
    path = root / f"{uid}-{slug}.md"
    path.write_text("\n".join(body), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# Resolution (AC1)
# --------------------------------------------------------------------------


def test_index_and_load_page_info(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    _page(wiki, "aaaaaaaa", "orca-faq", name="Orca Partner FAQ", desc="A brainstorm.")
    info = load_page_info("aaaaaaaa", build_uid_index(wiki))
    assert info.resolved is True
    assert info.name == "Orca Partner FAQ"
    assert info.description == "A brainstorm."
    assert info.path.endswith("aaaaaaaa-orca-faq.md")


def test_unresolvable_uid_is_flagged_not_blank(tmp_path: Path) -> None:
    """A ledger row whose page is gone must be visibly unresolved.

    'We pushed something and can no longer say what' is information; a blank
    name would render as an empty cell and read as a display bug.
    """
    info = load_page_info("deadbeef", build_uid_index(tmp_path / "wiki"))
    assert info.resolved is False
    assert info.name == ""


def test_missing_corpus_yields_empty_index_not_an_exception(tmp_path: Path) -> None:
    assert build_uid_index(tmp_path / "nope") == {}


def test_related_uids_are_collected(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    _page(wiki, "aaaaaaaa", "parent", name="Parent", related="bbbbbbbb")
    info = load_page_info("aaaaaaaa", build_uid_index(wiki))
    assert "bbbbbbbb" in info.related


def test_quoted_description_is_unquoted(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    _page(wiki, "aaaaaaaa", "p", name="P", desc="Quoted value.")
    info = load_page_info("aaaaaaaa", build_uid_index(wiki))
    assert info.description == "Quoted value."


# --------------------------------------------------------------------------
# resolve_path -- the security boundary
# --------------------------------------------------------------------------


def test_resolve_path_returns_page_inside_root(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    expected = _page(wiki, "aaaaaaaa", "p", name="P")
    assert resolve_path("aaaaaaaa", wiki_root=wiki) == expected.resolve()


def test_resolve_path_refuses_path_shaped_uid(tmp_path: Path) -> None:
    """Nothing path-shaped is ever looked up, so traversal never gets a turn."""
    wiki = tmp_path / "wiki"
    _page(wiki, "aaaaaaaa", "p", name="P")
    for hostile in ("../../etc/passwd", "..", "/etc/passwd", "aaaaaaaa/../x", ""):
        assert resolve_path(hostile, wiki_root=wiki) is None


def test_resolve_path_refuses_symlink_escaping_the_root(tmp_path: Path) -> None:
    """The check resolves symlinks BEFORE testing containment.

    A textual prefix test would pass this: the symlink's own path is inside the
    corpus. Only resolving it first reveals that the file is not.
    """
    wiki = tmp_path / "wiki"
    wiki.mkdir(parents=True)
    outside = tmp_path / "outside-secret.md"
    outside.write_text("secret", encoding="utf-8")
    link = wiki / "aaaaaaaa-escape.md"
    link.symlink_to(outside)
    assert resolve_path("aaaaaaaa", wiki_root=wiki) is None


def test_filename_shaped_ids_resolve(tmp_path: Path) -> None:
    """The ledger's OTHER id shape.

    `opaque_push_id` falls back to the bare filename when a page has no
    frontmatter uid. On the reference deployment that was 9 of 50 rows -- all
    of which rendered as 'no page found', which reads as corpus rot rather
    than a second id shape.
    """
    wiki = tmp_path / "wiki"
    wiki.mkdir(parents=True)
    (wiki / "auto-athenaeum-complete-phase.md").write_text(
        "---\nname: Complete Phase\ndescription: An auto-memory page.\n---\n",
        encoding="utf-8",
    )
    info = load_page_info("auto-athenaeum-complete-phase.md", build_uid_index(wiki))
    assert info.resolved is True
    assert info.name == "Complete Phase"
    assert resolve_path("auto-athenaeum-complete-phase.md", wiki_root=wiki) is not None


def test_uid_collision_prefers_the_page_that_names_itself(tmp_path: Path) -> None:
    """A uid prefix is not unique, and the wrong winner is silent.

    Found by LOOKING at the rendered page, not from the JSON: `2dbd1b8c`
    matched both `2dbd1b8c-verify-list-parity.md` (the real page) and the merge
    artifact `2dbd1b8c-3ac82004-a90ce50f.md`, which carries no frontmatter.
    First-writer-wins picked the artifact and the row rendered nameless, which
    the payload still reported as resolved:true.
    """
    wiki = tmp_path / "wiki"
    wiki.mkdir(parents=True)
    # Sorts FIRST, so a naive index picks it.
    (wiki / "2dbd1b8c-3ac82004-a90ce50f.md").write_text(
        "## From `wiki/2dbd1b8c-verify-list-parity.md`\n\nmerge artifact\n",
        encoding="utf-8",
    )
    (wiki / "2dbd1b8c-verify-list-parity.md").write_text(
        "---\nuid: 2dbd1b8c\nname: verify-list parity\n---\n\nbody\n",
        encoding="utf-8",
    )
    info = load_page_info("2dbd1b8c", build_uid_index(wiki))
    assert info.name == "verify-list parity"
    assert info.path.endswith("2dbd1b8c-verify-list-parity.md")
    # And a click must open the page whose name is on the row.
    resolved = resolve_path("2dbd1b8c", wiki_root=wiki)
    assert resolved is not None and resolved.name == "2dbd1b8c-verify-list-parity.md"


def test_page_without_frontmatter_falls_back_to_its_heading(tmp_path: Path) -> None:
    """Resolved-but-unnamed must never render as 'no page found'.

    That is a different and wrong claim about a file that exists.
    """
    wiki = tmp_path / "wiki"
    wiki.mkdir(parents=True)
    (wiki / "pypi-87b4fab1-bd6005a6.md").write_text(
        "## From `wiki/87b4fab1-pypi-v0-2-1.md`\n\n# PyPI v0.2.1\n\nbody\n",
        encoding="utf-8",
    )
    info = load_page_info("pypi-87b4fab1-bd6005a6.md", build_uid_index(wiki))
    assert info.resolved is True
    assert info.name == "PyPI v0.2.1"


def test_nameless_and_headingless_page_falls_back_to_the_stem(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir(parents=True)
    (wiki / "auto-nothing.md").write_text("just body text\n", encoding="utf-8")
    info = load_page_info("auto-nothing.md", build_uid_index(wiki))
    assert info.resolved is True
    assert info.name == "auto-nothing"


def test_filename_id_with_a_separator_is_refused(tmp_path: Path) -> None:
    """A filename-shaped id is a BASENAME; anything path-shaped is refused."""
    wiki = tmp_path / "wiki"
    wiki.mkdir(parents=True)
    for hostile in ("../secret.md", "sub/dir.md", "/etc/x.md", "..%2Fx.md"):
        assert resolve_path(hostile, wiki_root=wiki) is None


def test_sibling_surfaces_like_excluded_are_out_of_reach(tmp_path: Path) -> None:
    """A page in `excluded/` must not resolve, and must not be openable.

    `excluded/` is where the operator routes material off-corpus. It is a
    SIBLING of `wiki/`, so scoping the index and the containment check to
    `wiki/` puts it out of reach by construction -- verified here rather than
    left as an incidental property of a path that could be widened later.
    """
    knowledge = tmp_path / "knowledge"
    wiki = knowledge / "wiki"
    excluded = knowledge / "excluded"
    wiki.mkdir(parents=True)
    excluded.mkdir(parents=True)
    (excluded / "auto-private-thing.md").write_text("---\nname: Private\n---\n", encoding="utf-8")
    index = build_uid_index(wiki)
    assert "auto-private-thing.md" not in index
    assert load_page_info("auto-private-thing.md", index).resolved is False
    assert resolve_path("auto-private-thing.md", wiki_root=wiki) is None


# --------------------------------------------------------------------------
# Classification (AC2, AC3)
# --------------------------------------------------------------------------


def test_classification_covers_every_state() -> None:
    pushed = {"p_only", "p_and_pull"}
    pulled = {"p_and_pull", "crumb", "cold"}
    crumbs = {"crumb"}
    unknown = {"legacy"}

    def c(uid: str) -> str:
        return _cmd_viewer.classify(
            uid,
            pushed_ids=pushed,
            pulled_ids=pulled,
            breadcrumb_ids=crumbs,
            unknown_ids=unknown,
        )

    assert c("p_only") == _cmd_viewer.CLASSIFICATION_PUSHED
    assert c("p_and_pull") == _cmd_viewer.CLASSIFICATION_PUSHED_RECALLED
    assert c("crumb") == _cmd_viewer.CLASSIFICATION_BREADCRUMB
    assert c("cold") == _cmd_viewer.CLASSIFICATION_PULLED_COLD
    assert c("legacy") == _cmd_viewer.CLASSIFICATION_UNKNOWN_PROVENANCE


def test_pushed_beats_breadcrumb() -> None:
    """Having been pushed outright is the stronger statement about a page."""
    assert (
        _cmd_viewer.classify(
            "x",
            pushed_ids={"x"},
            pulled_ids=set(),
            breadcrumb_ids={"x"},
            unknown_ids=set(),
        )
        == _cmd_viewer.CLASSIFICATION_PUSHED
    )


def test_unknown_provenance_beats_breadcrumb_and_cold(issue: str = "athenaeum#1542") -> None:
    """Breadcrumb and pulled-cold are both claims that the session PULLED the
    page. A pre-`source` record cannot support either, so unknown wins over
    both -- but loses to `pushed`/`pulled`, which are known facts."""

    def c(uid: str, **kw: object) -> str:
        return _cmd_viewer.classify(uid, unknown_ids={"x"}, **kw)  # type: ignore[arg-type]

    assert (
        c("x", pushed_ids=set(), pulled_ids=set(), breadcrumb_ids={"x"})
        == _cmd_viewer.CLASSIFICATION_UNKNOWN_PROVENANCE
    )
    assert (
        c("x", pushed_ids=set(), pulled_ids=set(), breadcrumb_ids=set())
        == _cmd_viewer.CLASSIFICATION_UNKNOWN_PROVENANCE
    )
    assert (
        c("x", pushed_ids={"x"}, pulled_ids=set(), breadcrumb_ids=set())
        == _cmd_viewer.CLASSIFICATION_PUSHED
    )
    assert (
        c("x", pushed_ids=set(), pulled_ids={"x"}, breadcrumb_ids=set())
        == _cmd_viewer.CLASSIFICATION_PULLED_COLD
    )


def test_breadcrumb_is_one_hop_and_needs_a_pushed_parent(tmp_path: Path) -> None:
    """AC3: related-to-a-pushed-page is dark green; related to nothing is red.

    Also pins the one-hop rule: `grandchild` is reachable only THROUGH the
    breadcrumb page, and must stay red. A transitive walk over a 25k-page
    corpus turns almost everything dark green and the colour stops meaning
    anything.
    """
    wiki = tmp_path / "knowledge" / "wiki"
    _page(wiki, "aaaaaaaa", "pushed", name="Pushed", related="bbbbbbbb")
    _page(wiki, "bbbbbbbb", "crumb", name="Crumb", related="cccccccc")
    _page(wiki, "cccccccc", "grandchild", name="Grandchild")
    _page(wiki, "dddddddd", "cold", name="Cold")

    payload = _cmd_viewer.shape_viewer_payload(
        session_id="s",
        records=[
            {
                "record_type": "push",
                "source": "sidecar",
                "ts": "2026-09-09T10:00:00Z",
                "items": [{"id": "aaaaaaaa"}],
            },
            {
                "record_type": "push",
                "ts": "2026-09-09T10:01:00Z",
                "items": [{"id": "bbbbbbbb"}, {"id": "cccccccc"}, {"id": "dddddddd"}],
            },
        ],
    )
    enriched = _cmd_viewer.enrich_payload(payload, wiki_root=wiki)
    states = {row["id"]: row["classification"] for row in enriched["pages"]}

    assert states["aaaaaaaa"] == _cmd_viewer.CLASSIFICATION_PUSHED
    assert states["bbbbbbbb"] == _cmd_viewer.CLASSIFICATION_BREADCRUMB
    assert states["cccccccc"] == _cmd_viewer.CLASSIFICATION_PULLED_COLD
    assert states["dddddddd"] == _cmd_viewer.CLASSIFICATION_PULLED_COLD


def test_enriched_rows_carry_name_and_description(tmp_path: Path) -> None:
    wiki = tmp_path / "knowledge" / "wiki"
    _page(wiki, "aaaaaaaa", "p", name="Orca Partner FAQ", desc="A brainstorm.")
    payload = _cmd_viewer.shape_viewer_payload(
        session_id="s",
        records=[
            {
                "record_type": "push",
                "source": "sidecar",
                "ts": "2026-09-09T10:00:00Z",
                "items": [{"id": "aaaaaaaa", "memory_tier": "warm"}],
            }
        ],
    )
    row = _cmd_viewer.enrich_payload(payload, wiki_root=wiki)["pages"][0]
    assert row["name"] == "Orca Partner FAQ"
    assert row["description"] == "A brainstorm."
    assert row["resolved"] is True


# --------------------------------------------------------------------------
# Last-turn panel (AC4)
# --------------------------------------------------------------------------


def test_last_turn_is_the_newest_unbidden_push(tmp_path: Path) -> None:
    wiki = tmp_path / "knowledge" / "wiki"
    _page(wiki, "aaaaaaaa", "old", name="Old")
    _page(wiki, "bbbbbbbb", "new", name="New")
    payload = _cmd_viewer.shape_viewer_payload(
        session_id="s",
        records=[
            {
                "record_type": "push",
                "source": "sidecar",
                "ts": "2026-09-09T10:00:00Z",
                "items": [{"id": "aaaaaaaa"}],
            },
            {
                "record_type": "push",
                "source": "sidecar",
                "ts": "2026-09-09T11:00:00Z",
                "backend": "vector",
                "items": [{"id": "bbbbbbbb"}],
            },
        ],
    )
    turn = _cmd_viewer.enrich_payload(payload, wiki_root=wiki)["last_turn"]
    assert turn["present"] is True
    assert turn["ts"] == "2026-09-09T11:00:00Z"
    assert turn["backend"] == "vector"
    assert [i["id"] for i in turn["items"]] == ["bbbbbbbb"]


def test_last_turn_topics_are_explicitly_not_instrumented(tmp_path: Path) -> None:
    """Push records keep only a query hash (athenaeum#711).

    An empty topics box would read as 'the sidecar thought nothing', which is a
    different and wrong claim from 'we never recorded what it thought'.
    """
    wiki = tmp_path / "knowledge" / "wiki"
    payload = _cmd_viewer.shape_viewer_payload(
        session_id="s",
        records=[
            {
                "record_type": "push",
                "source": "sidecar",
                "ts": "2026-09-09T10:00:00Z",
                "items": [{"id": "aaaaaaaa"}],
            }
        ],
    )
    turn = _cmd_viewer.enrich_payload(payload, wiki_root=wiki)["last_turn"]
    assert turn["topics"] is None
    assert turn["topics_status"] == "not_instrumented"


def test_last_turn_topics_render_when_trace_has_them(tmp_path: Path) -> None:
    """Issue athenaeum#1530 AC1/AC6: the sidecar hook writes the topics it
    extracted to a SEPARATE local trace (never to the ledger -- athenaeum#711
    stays intact), keyed by the same ``query_hash`` the push record carries.
    When that trace holds a matching row, the last-turn panel must render the
    real topics instead of the not-instrumented placeholder.
    """
    wiki = tmp_path / "knowledge" / "wiki"
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "_last_turn_topics.jsonl").write_text(
        json.dumps(
            {
                "session_id": "s",
                "ts": "2026-09-09T10:00:00Z",
                "query_hash": "abc123abc123abc1",
                "topics": ["customer development", "lean startup"],
            }
        )
        + "\n"
    )
    payload = _cmd_viewer.shape_viewer_payload(
        session_id="s",
        records=[
            {
                "record_type": "push",
                "source": "sidecar",
                "ts": "2026-09-09T10:00:00Z",
                "query_hash": "abc123abc123abc1",
                "items": [{"id": "aaaaaaaa"}],
            }
        ],
    )
    turn = _cmd_viewer.enrich_payload(payload, wiki_root=wiki, cache_dir=cache_dir)["last_turn"]
    assert turn["topics"] == ["customer development", "lean startup"]
    assert turn["topics_status"] == "ok"


def test_last_turn_topics_not_instrumented_when_hash_does_not_match(tmp_path: Path) -> None:
    """A trace file that exists but holds no row for THIS push record's
    query_hash (e.g. rotated past by the ring buffer) must still degrade to
    the explicit not-instrumented state, never a stale or wrong topic set.
    """
    wiki = tmp_path / "knowledge" / "wiki"
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "_last_turn_topics.jsonl").write_text(
        json.dumps(
            {
                "session_id": "s",
                "ts": "2026-09-09T09:00:00Z",
                "query_hash": "0000000000000000",
                "topics": ["unrelated"],
            }
        )
        + "\n"
    )
    payload = _cmd_viewer.shape_viewer_payload(
        session_id="s",
        records=[
            {
                "record_type": "push",
                "source": "sidecar",
                "ts": "2026-09-09T10:00:00Z",
                "query_hash": "abc123abc123abc1",
                "items": [{"id": "aaaaaaaa"}],
            }
        ],
    )
    turn = _cmd_viewer.enrich_payload(payload, wiki_root=wiki, cache_dir=cache_dir)["last_turn"]
    assert turn["topics"] is None
    assert turn["topics_status"] == "not_instrumented"


def test_last_turn_absent_when_only_deliberate_pulls(tmp_path: Path) -> None:
    payload = _cmd_viewer.shape_viewer_payload(
        session_id="s",
        # Post-cutover ts: since athenaeum#1542 a source-less record must be
        # newer than SOURCE_FIELD_FIRST_SEEN to count as a deliberate pull at
        # all, which is what this test's name asserts is happening.
        records=[
            {"record_type": "push", "ts": "2026-09-09T10:00:00Z", "items": [{"id": "aaaaaaaa"}]}
        ],
    )
    turn = _cmd_viewer.enrich_payload(payload, wiki_root=tmp_path / "wiki")["last_turn"]
    assert turn["present"] is False


def test_legacy_payload_keys_survive(tmp_path: Path) -> None:
    """AC7: `athenaeum demo`'s row probe counts these three."""
    payload = _cmd_viewer.shape_viewer_payload(
        session_id="s",
        records=[
            {
                "record_type": "push",
                "source": "sidecar",
                "ts": "t",
                "items": [{"id": "aaaaaaaa"}],
            }
        ],
    )
    enriched = _cmd_viewer.enrich_payload(payload, wiki_root=tmp_path / "wiki")
    for key in ("pushed_unbidden", "pulled_deliberately", "overlap"):
        assert key in enriched


# --------------------------------------------------------------------------
# POST /open -- AC5. Every refusal gets a test.
# --------------------------------------------------------------------------


class _Recorder:
    """Stands in for the editor so tests never launch a real one."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **_kwargs: object) -> object:
        self.calls.append(list(argv))
        return object()


def _post(base: str, body: object, *, origin: str | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/open", data=data, headers={"Content-Type": "application/json"}
    )
    if origin is not None:
        req.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _serve(tmp_path: Path, monkeypatch, nonce: str = "test-nonce"):
    wiki = tmp_path / "knowledge" / "wiki"
    _page(wiki, "aaaaaaaa", "p", name="P")
    recorder = _Recorder()
    monkeypatch.setattr(_cmd_viewer.subprocess, "Popen", recorder)
    server = _cmd_viewer.make_server(
        session_id="s",
        path=tmp_path / "knowledge",
        cache_dir=tmp_path / "cache",
        port=0,
        nonce=nonce,
        editor_command=("fake-editor",),
    )
    return server, recorder


def test_open_launches_the_editor_with_a_good_nonce(tmp_path: Path, monkeypatch) -> None:
    from tests.test_cmd_viewer import _RunningServer

    server, recorder = _serve(tmp_path, monkeypatch)
    with _RunningServer(server) as running:
        base = f"http://127.0.0.1:{running.server_address[1]}"
        status, body = _post(base, {"uid": "aaaaaaaa", "nonce": "test-nonce"})
    assert status == 200
    assert body["ok"] is True
    assert len(recorder.calls) == 1
    assert recorder.calls[0][0] == "fake-editor"
    assert recorder.calls[0][1].endswith("aaaaaaaa-p.md")


def test_open_refuses_a_bad_nonce(tmp_path: Path, monkeypatch) -> None:
    """The whole reason the route is safe to expose.

    Any site the operator visits can POST to 127.0.0.1; the same-origin policy
    stops it READING our page, so it cannot learn the nonce.
    """
    from tests.test_cmd_viewer import _RunningServer

    server, recorder = _serve(tmp_path, monkeypatch)
    with _RunningServer(server) as running:
        base = f"http://127.0.0.1:{running.server_address[1]}"
        status, _ = _post(base, {"uid": "aaaaaaaa", "nonce": "wrong"})
        status_missing, _ = _post(base, {"uid": "aaaaaaaa"})
    assert status == 403
    assert status_missing == 403
    assert recorder.calls == []


def test_open_refuses_a_foreign_origin(tmp_path: Path, monkeypatch) -> None:
    from tests.test_cmd_viewer import _RunningServer

    server, recorder = _serve(tmp_path, monkeypatch)
    with _RunningServer(server) as running:
        base = f"http://127.0.0.1:{running.server_address[1]}"
        status, _ = _post(
            base,
            {"uid": "aaaaaaaa", "nonce": "test-nonce"},
            origin="https://evil.example",
        )
    assert status == 403
    assert recorder.calls == []


def test_open_accepts_its_own_origin(tmp_path: Path, monkeypatch) -> None:
    from tests.test_cmd_viewer import _RunningServer

    server, recorder = _serve(tmp_path, monkeypatch)
    with _RunningServer(server) as running:
        port = running.server_address[1]
        base = f"http://127.0.0.1:{port}"
        status, _ = _post(base, {"uid": "aaaaaaaa", "nonce": "test-nonce"}, origin=base)
    assert status == 200
    assert len(recorder.calls) == 1


def test_open_refuses_a_uid_outside_the_corpus(tmp_path: Path, monkeypatch) -> None:
    from tests.test_cmd_viewer import _RunningServer

    server, recorder = _serve(tmp_path, monkeypatch)
    with _RunningServer(server) as running:
        base = f"http://127.0.0.1:{running.server_address[1]}"
        status, _ = _post(base, {"uid": "ffffffff", "nonce": "test-nonce"})
        traversal, _ = _post(base, {"uid": "../../../etc/passwd", "nonce": "test-nonce"})
    assert status == 404
    assert traversal in (400, 404)
    assert recorder.calls == []


def test_open_refuses_a_non_string_uid(tmp_path: Path, monkeypatch) -> None:
    from tests.test_cmd_viewer import _RunningServer

    server, recorder = _serve(tmp_path, monkeypatch)
    with _RunningServer(server) as running:
        base = f"http://127.0.0.1:{running.server_address[1]}"
        status, _ = _post(base, {"uid": {"not": "a string"}, "nonce": "test-nonce"})
    assert status == 400
    assert recorder.calls == []


def test_get_open_is_still_not_a_route(tmp_path: Path, monkeypatch) -> None:
    """A GET must not open anything -- otherwise an <img src> would suffice."""
    from tests.test_cmd_viewer import _RunningServer

    server, recorder = _serve(tmp_path, monkeypatch)
    with _RunningServer(server) as running:
        base = f"http://127.0.0.1:{running.server_address[1]}"
        try:
            urllib.request.urlopen(f"{base}/open", timeout=5)
            status = 200
        except urllib.error.HTTPError as exc:
            status = exc.code
    assert status == 404
    assert recorder.calls == []


def test_each_server_mints_a_distinct_nonce(tmp_path: Path) -> None:
    a = _cmd_viewer.make_server(session_id="s", path=tmp_path, port=0)
    b = _cmd_viewer.make_server(session_id="s", path=tmp_path, port=0)
    try:
        handler_a = a.RequestHandlerClass
        handler_b = b.RequestHandlerClass
        assert handler_a.nonce and handler_b.nonce
        assert handler_a.nonce != handler_b.nonce
    finally:
        a.server_close()
        b.server_close()
