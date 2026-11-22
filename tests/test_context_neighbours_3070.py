# SPDX-License-Identifier: Apache-2.0
"""The per-turn push carries the linked neighbours of every hit (athenaeum#3070).

athenaeum#3066 measured it: on tasks that never mention memory, the page that
answers is usually one ``related`` link away from the pages the breadcrumb
names. The push now ends with one block of those pages, pooled across every
hit, deduplicated, never a page the breadcrumb already names, at most six
lines, under a heading that marks them as linked rather than matched. These
tests pin each of the issue's acceptance criteria, including the two bounds
(the push-token budget and the hook output cap) and the push ledger count.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from athenaeum.context import (
    EXCERPT_HEADER,
    HOOK_OUTPUT_CAP_CHARS,
    NEIGHBOUR_HEADER,
    NEIGHBOUR_LINE_CAP,
    NEIGHBOUR_LOOKUP_LIMIT,
    PREAMBLE,
    build_context,
    build_context_for_turn,
    estimate_tokens,
)

_REPO = Path(__file__).resolve().parent.parent
SRC = str(_REPO / "src")

QUERY = "gribwood"


def _write_page(
    wiki_root: Path,
    uid: str,
    name: str,
    *,
    description: str = "",
    related: list[str] | None = None,
    extra: str = "",
) -> str:
    """Write ``<uid>-<slug>.md`` with a real frontmatter block; return its filename."""
    slug = name.lower().replace(" ", "-")
    filename = f"{uid}-{slug}.md"
    fm = [f"uid: {uid}", "type: concept", f"name: {name}"]
    if description:
        fm.append(f"description: {description}")
    if related:
        fm.append("related:")
        for target in related:
            fm += [f"- uid: {target}", "  role: related"]
    if extra:
        fm.append(extra)
    wiki_root.mkdir(parents=True, exist_ok=True)
    (wiki_root / filename).write_text("---\n" + "\n".join(fm) + f"\n---\n\n# {name}\n\nBody.\n")
    return filename


def _build_index(path: Path, pages: list[tuple[str, str, str]]) -> None:
    """FTS5 index of ``(filename, name, description)`` hits, each tagged with
    :data:`QUERY`. Neighbours are deliberately NOT indexed: they reach the push
    only through ``related`` edges, never by matching."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE VIRTUAL TABLE wiki USING fts5(filename, name, tags, aliases, description, "
        'body, audience UNINDEXED, type UNINDEXED, tokenize="porter unicode61")'
    )
    conn.executemany(
        "INSERT INTO wiki VALUES (?,?,?,?,?,?,?,?)",
        [
            (f, n, QUERY, "", d, f"{QUERY} body", "|__access_open__|", "concept")
            for f, n, d in pages
        ],
    )
    conn.commit()
    conn.close()


class Store:
    """A wiki root plus the index over its hit pages."""

    def __init__(self, root: Path) -> None:
        self.wiki = root / "knowledge" / "wiki"
        self.cache = root / "cache"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.hits: list[tuple[str, str, str]] = []

    def hit(self, uid: str, name: str, related: list[str] | None = None, **kw) -> str:
        filename = _write_page(self.wiki, uid, name, related=related, **kw)
        self.hits.append((filename, name, kw.get("description", "")))
        return filename

    def page(self, uid: str, name: str, **kw) -> str:
        return _write_page(self.wiki, uid, name, **kw)

    def index(self) -> None:
        db = self.cache / "wiki-index.db"
        db.unlink(missing_ok=True)
        _build_index(db, self.hits)

    def envelope(self, **kwargs):
        self.index()
        kwargs.setdefault("use_llm", False)
        kwargs.setdefault("wiki_root", self.wiki)
        kwargs.setdefault("neighbour_lines", NEIGHBOUR_LINE_CAP)
        return build_context(QUERY, "sess", cache_dir=self.cache, **kwargs)


def _block(text: str) -> list[str]:
    """The neighbour lines of a rendered push, ``[]`` when it has no block."""
    lines = text.splitlines()
    if NEIGHBOUR_HEADER not in lines:
        return []
    return lines[lines.index(NEIGHBOUR_HEADER) + 1 :]


def _names(text: str) -> list[str]:
    return [ln.removeprefix("  - ").split(" — ")[0] for ln in _block(text)]


# ---------------------------------------------------------------------------
# AC1: pooled, deduplicated, at most six, never a page already named
# ---------------------------------------------------------------------------


class TestPooledNeighbours:
    def _store(self, tmp_path: Path) -> Store:
        s = Store(tmp_path)
        for i in range(1, 9):
            s.page(f"n{i}", f"Neighbour {i}", description=f"about neighbour {i}")
        # Hit b links back to hit a, and to one of a's neighbours.
        s.hit("a", "Alpha", related=["n1", "n2", "b", "n3"], description="first hit")
        s.hit("b", "Beta", related=["a", "n2", "n4", "n5", "n6", "n7", "n8"], description="hit 2")
        return s

    def test_block_follows_the_bullets_under_the_linked_heading(self, tmp_path: Path) -> None:
        text = self._store(tmp_path).envelope()["render"]["text"]
        lines = text.splitlines()
        header_at = lines.index(NEIGHBOUR_HEADER)
        assert all(ln.startswith("  - ") for ln in lines[header_at + 1 :])
        assert "  - Neighbour 1 — about neighbour 1" in lines

    def test_pooled_deduplicated_capped_and_excluding_named_pages(self, tmp_path: Path) -> None:
        env = self._store(tmp_path).envelope()
        names = _names(env["render"]["text"])
        assert len(names) == NEIGHBOUR_LINE_CAP == 6
        assert len(set(names)) == len(names)
        assert not {"Alpha", "Beta"} & set(names)
        assert set(names) <= {f"Neighbour {i}" for i in range(1, 9)}

    def test_smaller_cap_is_honoured(self, tmp_path: Path) -> None:
        env = self._store(tmp_path).envelope(neighbour_lines=2)
        assert len(_names(env["render"]["text"])) == 2

    def test_dangling_inactive_and_nameless_targets_are_dropped(self, tmp_path: Path) -> None:
        s = Store(tmp_path)
        s.page("gone", "Gone Page", extra="retired: true")
        s.page("ok", "Kept Page")
        (s.wiki / "anon-x.md").write_text("---\nuid: anon\ntype: concept\n---\n\nNo name.\n")
        s.hit("a", "Alpha", related=["missing", "gone", "anon", "ok"])
        assert _names(s.envelope()["render"]["text"]) == ["Kept Page"]

    def test_neighbour_without_description_renders_name_only(self, tmp_path: Path) -> None:
        s = Store(tmp_path)
        s.page("n1", "Bare Neighbour")
        s.hit("a", "Alpha", related=["n1"])
        assert _block(s.envelope()["render"]["text"]) == ["  - Bare Neighbour"]

    def test_multiline_description_is_flattened_and_clamped(self, tmp_path: Path) -> None:
        s = Store(tmp_path)
        s.page("n1", "Wordy", description="|\n  line one\n  line two " + "x" * 300)
        s.hit("a", "Alpha", related=["n1"])
        (line,) = _block(s.envelope()["render"]["text"])
        desc = line.split(" — ", 1)[1]
        assert "\n" not in desc and desc.startswith("line one line two")
        assert len(desc) <= 200

    def test_lookups_are_bounded(self, tmp_path: Path) -> None:
        s = Store(tmp_path)
        s.page("late", "Late Neighbour")
        dangling = [f"missing{i}" for i in range(NEIGHBOUR_LOOKUP_LIMIT)]
        s.hit("a", "Alpha", related=[*dangling, "late"])
        assert _block(s.envelope()["render"]["text"]) == []


# ---------------------------------------------------------------------------
# AC3: deterministic, higher-ranked hits' neighbours first
# ---------------------------------------------------------------------------


class TestOrdering:
    def _store(self, tmp_path: Path) -> Store:
        s = Store(tmp_path)
        for uid in ("x1", "x2", "y1", "y2", "z1"):
            s.page(uid, f"Page {uid}")
        s.hit("a", "Alpha", related=["x1", "x2"])
        s.hit("b", "Beta", related=["y1", "y2"])
        s.hit("c", "Gamma", related=["z1"])
        return s

    def test_neighbours_follow_hit_rank_then_edge_order(self, tmp_path: Path) -> None:
        env = self._store(tmp_path).envelope()
        edges = {"Alpha": ["x1", "x2"], "Beta": ["y1", "y2"], "Gamma": ["z1"]}
        expected = [f"Page {uid}" for c in env["candidates"] for uid in edges[c["name"]]]
        assert _names(env["render"]["text"]) == expected

    def test_same_push_twice_renders_the_same_block(self, tmp_path: Path) -> None:
        s = self._store(tmp_path)
        first = s.envelope()["render"]["text"]
        assert s.envelope()["render"]["text"] == first

    def test_a_shared_neighbour_is_listed_under_the_higher_ranked_hit(
        self, tmp_path: Path
    ) -> None:
        s = Store(tmp_path)
        s.page("shared", "Shared")
        s.page("only_b", "Only B")
        s.hit("a", "Alpha", related=["shared"])
        s.hit("b", "Beta", related=["only_b", "shared"])
        env = s.envelope()
        names = _names(env["render"]["text"])
        first_hit = env["candidates"][0]["name"]
        assert names == (["Shared", "Only B"] if first_hit == "Alpha" else ["Only B", "Shared"])


# ---------------------------------------------------------------------------
# AC4: a page with no `related` edges adds nothing
# ---------------------------------------------------------------------------


class TestNoEdges:
    def test_hits_without_related_edges_add_no_block(self, tmp_path: Path) -> None:
        s = Store(tmp_path)
        s.hit("a", "Alpha", description="first")
        s.hit("b", "Beta", description="second")
        with_block = s.envelope()
        without = build_context(
            QUERY, "sess", cache_dir=s.cache, use_llm=False, wiki_root=s.wiki, neighbour_lines=0
        )
        assert NEIGHBOUR_HEADER not in with_block["render"]["text"]
        assert with_block["render"]["text"] == without["render"]["text"]
        assert with_block["budget"] == without["budget"]

    def test_related_edges_pointing_only_at_other_hits_add_nothing(self, tmp_path: Path) -> None:
        s = Store(tmp_path)
        s.hit("a", "Alpha", related=["b"])
        s.hit("b", "Beta", related=["a"])
        assert NEIGHBOUR_HEADER not in s.envelope()["render"]["text"]

    def test_unreadable_hit_page_adds_nothing(self, tmp_path: Path) -> None:
        s = Store(tmp_path)
        s.page("n1", "Neighbour")
        filename = s.hit("a", "Alpha", related=["n1"])
        s.index()
        (s.wiki / filename).unlink()
        env = build_context(
            QUERY, "s", cache_dir=s.cache, use_llm=False, wiki_root=s.wiki, neighbour_lines=6
        )
        assert env["candidates"] and NEIGHBOUR_HEADER not in env["render"]["text"]

    def test_off_by_default_for_a_direct_caller(self, tmp_path: Path) -> None:
        s = Store(tmp_path)
        s.page("n1", "Neighbour")
        s.hit("a", "Alpha", related=["n1"])
        s.index()
        env = build_context(QUERY, "s", cache_dir=s.cache, use_llm=False, wiki_root=s.wiki)
        assert NEIGHBOUR_HEADER not in env["render"]["text"]

    def test_no_wiki_root_means_no_block(self, tmp_path: Path) -> None:
        s = Store(tmp_path)
        s.page("n1", "Neighbour")
        s.hit("a", "Alpha", related=["n1"])
        assert NEIGHBOUR_HEADER not in s.envelope(wiki_root=None)["render"]["text"]


# ---------------------------------------------------------------------------
# AC2: the push budget and the hook output cap
# ---------------------------------------------------------------------------


def _wide_store(tmp_path: Path, description: str = "d" * 200) -> Store:
    s = Store(tmp_path)
    targets = [f"n{i}" for i in range(1, 9)]
    for uid in targets:
        s.page(uid, f"Neighbour {uid}", description=description)
    s.hit("a", "Alpha", related=targets[:4], description="first hit")
    s.hit("b", "Beta", related=targets[4:], description="second hit")
    return s


class TestTokenBudgetBound:
    def test_block_cost_is_metered_on_the_top_candidate(self, tmp_path: Path) -> None:
        s = _wide_store(tmp_path)
        off = s.envelope(neighbour_lines=0)
        on = s.envelope()
        block = "\n".join([NEIGHBOUR_HEADER, *_block(on["render"]["text"])])
        assert on["candidates"][0]["token_cost"] == (
            off["candidates"][0]["token_cost"] + estimate_tokens(f"\n{block}")
        )
        assert [c["token_cost"] for c in on["candidates"][1:]] == [
            c["token_cost"] for c in off["candidates"][1:]
        ]

    @pytest.mark.parametrize("headroom", [0, 30, 70, 130, 400])
    def test_never_pushes_the_turn_over_budget(self, tmp_path: Path, headroom: int) -> None:
        s = _wide_store(tmp_path)
        base = s.envelope(neighbour_lines=0)
        budget = estimate_tokens(PREAMBLE) + base["budget"]["used"] + headroom
        env = s.envelope(budget=budget)
        assert [c["filename"] for c in env["candidates"]] == [
            c["filename"] for c in base["candidates"]
        ]
        assert estimate_tokens(PREAMBLE) + env["budget"]["used"] <= budget
        lines = _block(env["render"]["text"])
        assert len(lines) <= NEIGHBOUR_LINE_CAP
        if headroom == 0:
            assert lines == []

    def test_tight_budget_skips_lines_but_never_cuts_one(self, tmp_path: Path) -> None:
        s = _wide_store(tmp_path)
        base = s.envelope(neighbour_lines=0)
        budget = estimate_tokens(PREAMBLE) + base["budget"]["used"] + 130
        lines = _block(s.envelope(budget=budget)["render"]["text"])
        assert 0 < len(lines) < NEIGHBOUR_LINE_CAP
        assert all(ln.endswith("d" * 200) for ln in lines)

    def test_a_shorter_later_line_still_fits_after_a_long_one_is_skipped(
        self, tmp_path: Path
    ) -> None:
        s = Store(tmp_path)
        s.page("long", "Long", description="l" * 200)
        s.page("short", "Short")
        s.hit("a", "Alpha", related=["long", "short"])
        base = s.envelope(neighbour_lines=0)
        budget = estimate_tokens(PREAMBLE) + base["budget"]["used"] + 15
        assert _block(s.envelope(budget=budget)["render"]["text"]) == ["  - Short"]


class TestHookOutputCap:
    """Hit names are sized so the push, before the block, leaves *room*
    characters under the cap; the block must fit in that room or be dropped."""

    def _store(self, tmp_path: Path, room: int) -> Store:
        s = Store(tmp_path)
        targets = [f"n{i}" for i in range(1, 7)]
        for uid in targets:
            s.page(uid, f"Neighbour {uid}", description="d" * 120)
        s.hit("a", "Alpha", related=targets, description="top")
        for i in range(1, 7):
            s.hit(f"h{i}", f"Hit {i}", description="x")
        # Long names fill the cap (descriptions are clamped at 200 characters).
        # Only the index carries them: a page's filename is built from its name.
        s.hits[1:] = [(f, n + " " + "n" * 1300, d) for f, n, d in s.hits[1:]]
        used = len(f"{PREAMBLE}\n" + "\n".join(f"  - {n} — {d}" for _f, n, d in s.hits))
        pad = HOOK_OUTPUT_CAP_CHARS - room - used
        assert pad > 0
        filename, name, desc = s.hits[-1]
        s.hits[-1] = (filename, name + "n" * pad, desc)
        return s

    def _render(self, tmp_path: Path, room: int, lines: int) -> str:
        env = self._store(tmp_path, room).envelope(n=7, budget=100_000, neighbour_lines=lines)
        return f"{PREAMBLE}\n{env['render']['text']}"

    def test_layout_is_what_this_test_assumes(self, tmp_path: Path) -> None:
        assert len(self._render(tmp_path, 500, 0)) == HOOK_OUTPUT_CAP_CHARS - 500

    def test_block_shrinks_to_fit_under_the_cap(self, tmp_path: Path) -> None:
        rendered = self._render(tmp_path, 500, NEIGHBOUR_LINE_CAP)
        assert NEIGHBOUR_HEADER in rendered
        assert 0 < len(_block(rendered)) < NEIGHBOUR_LINE_CAP
        assert len(rendered) <= HOOK_OUTPUT_CAP_CHARS

    def test_no_room_for_a_line_drops_the_block(self, tmp_path: Path) -> None:
        bare = self._render(tmp_path / "bare", 60, 0)
        rendered = self._render(tmp_path / "x", 60, NEIGHBOUR_LINE_CAP)
        assert rendered == bare

    def test_default_budget_full_block_and_maximum_excerpt_fit_both_bounds(
        self, tmp_path: Path
    ) -> None:
        s = _wide_store(tmp_path)
        env = s.envelope(excerpt_chars=2000)
        rendered = f"{PREAMBLE}\n{env['render']['text']}"
        assert EXCERPT_HEADER.split('"')[0] in rendered
        assert len(_block(rendered)) == NEIGHBOUR_LINE_CAP
        assert len(rendered) <= HOOK_OUTPUT_CAP_CHARS
        assert estimate_tokens(PREAMBLE) + env["budget"]["used"] <= env["budget"]["tokens"]

    def test_block_goes_after_the_excerpt(self, tmp_path: Path) -> None:
        text = _wide_store(tmp_path).envelope(excerpt_chars=300)["render"]["text"]
        assert text.index(EXCERPT_HEADER.split('"')[0]) < text.index(NEIGHBOUR_HEADER)


# ---------------------------------------------------------------------------
# Per-turn path and AC5: the push ledger records the count
# ---------------------------------------------------------------------------


class TestPerTurnPath:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_PUSH_EXCERPT_CHARS", "0")
        monkeypatch.delenv("ATHENAEUM_INSTRUMENTATION", raising=False)

    def _turn(self, s: Store, session: str = "sess", **kwargs):
        s.index()
        return build_context_for_turn(
            f"what about {QUERY}?",
            session,
            cache_dir=s.cache,
            use_llm=False,
            wiki_root=s.wiki,
            **kwargs,
        )

    def _ledger(self, s: Store) -> list[dict]:
        from athenaeum.push_metrics import read_push_records

        return read_push_records(wiki_root=s.wiki, cache_dir=s.cache)

    def test_per_turn_push_carries_the_block_by_default(self, tmp_path: Path) -> None:
        s = _wide_store(tmp_path)
        assert len(_block(self._turn(s)["render"]["text"])) == NEIGHBOUR_LINE_CAP

    def test_ledger_records_how_many_lines_were_pushed(self, tmp_path: Path) -> None:
        s = _wide_store(tmp_path)
        env = self._turn(s, neighbour_lines=4)
        (row,) = self._ledger(s)
        assert row["neighbour_lines"] == len(_block(env["render"]["text"])) == 4

    def test_ledger_records_zero_when_no_hit_has_links(self, tmp_path: Path) -> None:
        s = Store(tmp_path)
        s.hit("a", "Alpha")
        self._turn(s)
        (row,) = self._ledger(s)
        assert row["neighbour_lines"] == 0

    def test_ledger_omits_the_key_when_neighbours_are_off(self, tmp_path: Path) -> None:
        s = _wide_store(tmp_path)
        self._turn(s, neighbour_lines=0)
        (row,) = self._ledger(s)
        assert "neighbour_lines" not in row

    def test_tail_projects_the_count(self, tmp_path: Path) -> None:
        from athenaeum.push_metrics import _shape_tail_push_record

        s = _wide_store(tmp_path)
        self._turn(s)
        (row,) = self._ledger(s)
        assert _shape_tail_push_record(row)["neighbour_lines"] == NEIGHBOUR_LINE_CAP
        del row["neighbour_lines"]
        assert "neighbour_lines" not in _shape_tail_push_record(row)

    def test_neighbours_are_not_added_to_the_seen_list(self, tmp_path: Path) -> None:
        from athenaeum.context import _read_seen, _seen_file

        s = _wide_store(tmp_path)
        env = self._turn(s)
        seen = _read_seen(_seen_file(s.cache, "sess"))
        assert seen == {c["filename"] for c in env["candidates"]}


def test_adapter_output_carries_the_block_within_the_cap(tmp_path: Path) -> None:
    """End to end: the real `athenaeum-claude-hook` module in a subprocess. Its
    wiki root is the default knowledge root under HOME, so the pages written
    there are the ones whose links it follows."""
    s = _wide_store(tmp_path)
    s.index()
    env = {
        "PYTHONPATH": SRC,
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "ATHENAEUM_CACHE_DIR": str(s.cache),
        "SEARCH_BACKEND": "fts5",
        "ATHENAEUM_INSTRUMENTATION": "off",
    }
    result = subprocess.run(
        [sys.executable, "-m", "athenaeum.claude_code_adapter"],
        input=json.dumps({"prompt": f"tell me about {QUERY} please", "session_id": "s1"}),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0
    assert result.stderr == ""
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert context.startswith(PREAMBLE)
    assert len(_block(context)) == NEIGHBOUR_LINE_CAP
    assert len(context) <= HOOK_OUTPUT_CAP_CHARS


def _run_hook_with_knowledge_root(
    tmp_path: Path, *, via_config_env: bool
) -> tuple[str, Store]:
    """Run the real hook with the store somewhere other than the home default.

    HOME points at an empty directory, so a hook that still builds its wiki
    path from ``~/knowledge`` finds no pages. The store carries a child page
    whose parent is a page under that same directory."""
    s = Store(tmp_path / "elsewhere")
    s.page("acme", "Acme Holdings")
    s.page("n1", "Linked Neighbour One", description="a linked page")
    s.hit("a", "Alpha Desk", related=["n1"], description="first hit", extra="parent: acme")
    s.index()
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PYTHONPATH": SRC,
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "ATHENAEUM_CACHE_DIR": str(s.cache),
        "SEARCH_BACKEND": "fts5",
        "ATHENAEUM_INSTRUMENTATION": "off",
    }
    root = str(s.wiki.parent)
    if via_config_env:
        s.cache.joinpath("config.env").write_text(f"KNOWLEDGE_ROOT={root}\n")
    else:
        env["KNOWLEDGE_ROOT"] = root
    result = subprocess.run(
        [sys.executable, "-m", "athenaeum.claude_code_adapter"],
        input=json.dumps({"prompt": f"tell me about {QUERY} please", "session_id": "s1"}),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0
    return result.stdout, s


@pytest.mark.parametrize("via_config_env", [False, True], ids=["process-env", "config-env"])
def test_adapter_reads_the_configured_knowledge_root(tmp_path: Path, via_config_env: bool) -> None:
    """athenaeum#3096: linked notes and parent names come from the configured
    knowledge root (env, or the cache config.env), not ``~/knowledge``."""
    stdout, _s = _run_hook_with_knowledge_root(tmp_path, via_config_env=via_config_env)
    context = json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
    assert NEIGHBOUR_HEADER in context.splitlines()
    assert "Linked Neighbour One" in context
    assert "(under Acme Holdings)" in context
