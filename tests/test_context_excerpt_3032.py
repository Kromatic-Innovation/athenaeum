# SPDX-License-Identifier: Apache-2.0
"""The per-turn push's top-hit excerpt (issue athenaeum#3032).

The push used to carry only ``name — description`` bullets. It now ends with a
short excerpt of the top hit's matching passage, bounded three ways: the
configured length (``resolve_push_excerpt_chars``, default 600, max 2000), the
push-token budget, and the 10,000-character cap Claude Code puts on a hook's
injected context. Each bound has a test here, and so does the adapter's real
output.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from athenaeum.config import (
    PUSH_EXCERPT_CHARS_DEFAULT,
    PUSH_EXCERPT_CHARS_MAX,
    resolve_push_excerpt_chars,
)
from athenaeum.context import (
    EXCERPT_HEADER,
    EXCERPT_MIN_CHARS,
    HOOK_OUTPUT_CAP_CHARS,
    PREAMBLE,
    build_context,
    build_context_for_turn,
    estimate_tokens,
    select_excerpt,
)

_REPO = Path(__file__).resolve().parent.parent
SRC = str(_REPO / "src")

QUERY = "gribwood"

#: The top page's body: an opening paragraph without the query term, a
#: heading, then the paragraph that matches it. The excerpt must start at the
#: heading above the matching passage, not at the page's opening.
TOP_BODY = (
    "# Gribwood Mill\n\n"
    "An opening paragraph about the building's history and its owners.\n\n"
    "## Supply\n\n"
    "The gribwood supplier is Northfield Timber; orders go out every second "
    "Tuesday and the contact is Dana Reyes.\n\n"
    "## Other notes\n\n"
    "Unrelated closing remarks about parking."
)


def _build_index(
    path: Path,
    pages: list[tuple[str, str, str, str]],
    *,
    with_body: bool = True,
) -> Path:
    """FTS5 index of ``(filename, name, description, body)`` pages, every one
    tagged with :data:`QUERY` so FTS5 rank, not the body, orders them.
    *with_body* omits the ``body`` column, standing in for a legacy index."""
    cols = ["filename", "name", "tags", "aliases", "description"]
    if with_body:
        cols.append("body")
    cols += ["audience UNINDEXED", "type UNINDEXED"]
    insert_cols = [c.split()[0] for c in cols]
    conn = sqlite3.connect(path)
    conn.execute(
        f"CREATE VIRTUAL TABLE wiki USING fts5({', '.join(cols)}, tokenize=\"porter unicode61\")"
    )
    rows = []
    for filename, name, description, body in pages:
        row = [filename, name, QUERY, "", description]
        if with_body:
            row.append(body)
        row += ["|__access_open__|", "note"]
        rows.append(tuple(row))
    conn.executemany(
        f"INSERT INTO wiki ({', '.join(insert_cols)}) "
        f"VALUES ({','.join('?' for _ in insert_cols)})",
        rows,
    )
    conn.commit()
    conn.close()
    return path


def _default_pages() -> list[tuple[str, str, str, str]]:
    return [
        ("gribwood-mill.md", "Gribwood Mill", "the mill", TOP_BODY),
        ("second.md", "Second Page", "another page", f"{QUERY} second body text"),
    ]


def _envelope(cache_dir: Path, pages=None, **kwargs):
    cache_dir.mkdir(parents=True, exist_ok=True)
    _build_index(
        cache_dir / "wiki-index.db",
        pages if pages is not None else _default_pages(),
        with_body=kwargs.pop("with_body", True),
    )
    kwargs.setdefault("use_llm", False)
    return build_context(QUERY, "sess", cache_dir=cache_dir, **kwargs)


def _header(name: str) -> str:
    return EXCERPT_HEADER.format(name=name)


# ---------------------------------------------------------------------------
# select_excerpt: which passage, and how long
# ---------------------------------------------------------------------------


class TestSelectExcerpt:
    def test_starts_at_the_heading_above_the_matching_passage(self) -> None:
        out = select_excerpt(TOP_BODY, [QUERY], 600)
        assert out.startswith("## Supply\n\nThe gribwood supplier is Northfield Timber")
        assert "opening paragraph" not in out

    def test_no_term_match_falls_back_to_the_page_opening(self) -> None:
        """The measured shape: the first characters of the page, skipping the
        H1 title, which the bullet already names."""
        out = select_excerpt(TOP_BODY, ["nothing-matches"], 600)
        assert out.startswith("An opening paragraph")

    def test_multi_word_terms_match_by_word(self) -> None:
        """The model extractor returns topics such as "gribwood supply"; the
        whole phrase appears nowhere, its words do."""
        out = select_excerpt(TOP_BODY, ["gribwood supply"], 600)
        assert out.startswith("## Supply")

    def test_a_section_heading_counts_toward_its_passage(self) -> None:
        """"supply" is only in the heading; the passage under it wins over an
        earlier paragraph matching one term."""
        body = "About the gribwood mill.\n\n## Supply\n\nThe gribwood supplier is near."
        assert select_excerpt(body, ["gribwood", "supply"], 600).startswith("## Supply")

    def test_block_with_more_distinct_terms_wins(self) -> None:
        body = "alpha only here.\n\nalpha and beta together.\n\nbeta only."
        assert select_excerpt(body, ["alpha", "beta"], 600).startswith("alpha and beta")

    @pytest.mark.parametrize("limit", [80, 120, 333, 600])
    def test_never_longer_than_the_limit(self, limit: int) -> None:
        body = " ".join(f"word{i}" for i in range(2000))
        out = select_excerpt(body, ["word5"], limit)
        assert len(out) <= limit
        assert out.endswith("…")

    def test_cut_lands_on_a_word_boundary(self) -> None:
        body = " ".join(["gribwood"] + ["lengthyword"] * 200)
        out = select_excerpt(body, [QUERY], 100)
        assert out.endswith("lengthyword…")

    def test_short_body_is_returned_whole_without_an_ellipsis(self) -> None:
        assert select_excerpt("gribwood is short.", [QUERY], 600) == "gribwood is short."

    def test_zero_limit_or_empty_body_is_empty(self) -> None:
        assert select_excerpt(TOP_BODY, [QUERY], 0) == ""
        assert select_excerpt("   \n\n  ", [QUERY], 600) == ""


# ---------------------------------------------------------------------------
# build_context: the excerpt in the envelope
# ---------------------------------------------------------------------------


class TestExcerptInEnvelope:
    def test_off_by_default_for_a_direct_caller(self, tmp_path: Path) -> None:
        """Counter-example: every direct `build_context` caller (the static
        refresh adapter, tests) silently growing a new block."""
        text = _envelope(tmp_path)["render"]["text"]
        assert "Top match" not in text
        assert text.splitlines() == [
            "  - Gribwood Mill — the mill",
            "  - Second Page — another page",
        ]

    def test_excerpt_of_the_top_hit_only_follows_the_bullets(self, tmp_path: Path) -> None:
        env = _envelope(tmp_path, excerpt_chars=600)
        text = env["render"]["text"]
        bullets, _, block = text.partition(f"\n{_header('Gribwood Mill')}\n")
        assert bullets.splitlines() == [
            "  - Gribwood Mill — the mill",
            "  - Second Page — another page",
        ]
        assert block.startswith("## Supply\n\nThe gribwood supplier")
        assert "Second Page\":" not in text
        assert text.count("Top match") == 1

    def test_excerpt_cost_is_metered_on_the_top_candidate(self, tmp_path: Path) -> None:
        bare = _envelope(tmp_path / "a")
        withx = _envelope(tmp_path / "b", excerpt_chars=600)
        block = withx["render"]["text"].split("\n", 2)[2]
        extra = estimate_tokens(f"\n{block}")
        assert extra > 0
        top_bare, top_with = bare["candidates"][0], withx["candidates"][0]
        assert top_with["token_cost"] == top_bare["token_cost"] + extra
        assert withx["candidates"][1] == bare["candidates"][1]
        assert withx["budget"]["used"] == bare["budget"]["used"] + extra

    def test_configured_length_bounds_the_excerpt(self, tmp_path: Path) -> None:
        long_body = f"{QUERY} " + " ".join(f"w{i}" for i in range(3000))
        pages = [("big.md", "Big Page", "big", long_body)]
        text = _envelope(tmp_path, pages, excerpt_chars=150)["render"]["text"]
        excerpt = text.split(f"{_header('Big Page')}\n", 1)[1]
        assert len(excerpt) <= 150

    def test_legacy_index_without_body_degrades_to_no_excerpt(self, tmp_path: Path) -> None:
        text = _envelope(tmp_path, with_body=False, excerpt_chars=600)["render"]["text"]
        assert "Top match" not in text
        assert text.startswith("  - Gribwood Mill")

    def test_page_with_empty_body_gets_no_excerpt(self, tmp_path: Path) -> None:
        pages = [("empty.md", "Empty Page", "nothing inside", "")]
        text = _envelope(tmp_path, pages, excerpt_chars=600)["render"]["text"]
        assert text == "  - Empty Page — nothing inside"

    def test_no_bullets_means_no_excerpt(self, tmp_path: Path) -> None:
        """A turn that renders nothing stays silent, excerpt included."""
        env = _envelope(tmp_path, excerpt_chars=600, budget=1)
        assert env["render"]["text"] == ""


class TestTokenBudgetBound:
    def test_excerpt_never_pushes_the_turn_over_budget(self, tmp_path: Path) -> None:
        bare = _envelope(tmp_path / "bare", budget=120)
        bare_used = estimate_tokens(PREAMBLE) + bare["budget"]["used"]
        for budget in (bare_used + 30, bare_used + 60, 400, 1200):
            env = _envelope(tmp_path / f"b{budget}", budget=budget, excerpt_chars=600)
            assert estimate_tokens(PREAMBLE) + env["budget"]["used"] <= budget, budget

    def test_tight_budget_shrinks_the_excerpt_but_keeps_every_bullet(
        self, tmp_path: Path
    ) -> None:
        bare = _envelope(tmp_path / "bare")
        roomy = _envelope(tmp_path / "roomy", excerpt_chars=600)
        bullets_tokens = estimate_tokens(PREAMBLE) + bare["budget"]["used"]
        tight = _envelope(tmp_path / "tight", budget=bullets_tokens + 40, excerpt_chars=600)
        assert [c["filename"] for c in tight["candidates"]] == [
            c["filename"] for c in bare["candidates"]
        ]
        assert "Top match" in tight["render"]["text"]
        assert len(tight["render"]["text"]) < len(roomy["render"]["text"])

    def test_budget_too_small_for_a_real_passage_drops_it(self, tmp_path: Path) -> None:
        bare = _envelope(tmp_path / "bare")
        bullets_tokens = estimate_tokens(PREAMBLE) + bare["budget"]["used"]
        # Room for fewer than EXCERPT_MIN_CHARS characters after the header.
        env = _envelope(tmp_path / "t", budget=bullets_tokens + 15, excerpt_chars=600)
        assert env["render"]["text"] == bare["render"]["text"]
        assert EXCERPT_MIN_CHARS > 15 * 4 - len(_header("Gribwood Mill")) - 2


class TestHookOutputCap:
    """Bullets are sized so the rendered push leaves *room* characters under
    the cap; the excerpt must fit in that room or be dropped."""

    def _pages_leaving(self, room: int) -> list[tuple[str, str, str, str]]:
        body = f"{QUERY} " + " ".join(f"word{i}" for i in range(2000))
        pages = [("p00.md", "Page 00", "top page", body)]
        # Names are not truncated (descriptions are, at 200 characters), so
        # long names fill the cap. They rank below the short top page.
        for i in range(1, 7):
            pages.append((f"p{i:02d}.md", f"Page {i:02d} " + "n" * 1300, "x", body))
        lines = [f"  - {name} — {desc}" for _f, name, desc, _b in pages]
        used = len(f"{PREAMBLE}\n" + "\n".join(lines))
        pad = HOOK_OUTPUT_CAP_CHARS - room - used
        assert pad > 0
        filename, name, desc, b = pages[-1]
        pages[-1] = (filename, name + "n" * pad, desc, b)
        return pages

    def _render(self, cache_dir: Path, room: int, excerpt_chars: int) -> str:
        env = _envelope(
            cache_dir,
            self._pages_leaving(room),
            n=7,
            budget=100_000,
            excerpt_chars=excerpt_chars,
        )
        return f"{PREAMBLE}\n{env['render']['text']}"

    def test_bullets_fill_the_layout_this_test_assumes(self, tmp_path: Path) -> None:
        rendered = self._render(tmp_path, 500, 0)
        assert rendered.splitlines()[1] == "  - Page 00 — top page"
        assert len(rendered) == HOOK_OUTPUT_CAP_CHARS - 500

    def test_excerpt_shrinks_to_fit_under_the_cap(self, tmp_path: Path) -> None:
        rendered = self._render(tmp_path, 500, PUSH_EXCERPT_CHARS_MAX)
        assert _header("Page 00") in rendered
        assert HOOK_OUTPUT_CAP_CHARS - 100 < len(rendered) <= HOOK_OUTPUT_CAP_CHARS

    def test_no_room_for_a_real_passage_drops_the_excerpt(self, tmp_path: Path) -> None:
        bare = self._render(tmp_path / "bare", 100, 0)
        rendered = self._render(tmp_path / "x", 100, PUSH_EXCERPT_CHARS_MAX)
        assert rendered == bare
        assert len(rendered) <= HOOK_OUTPUT_CAP_CHARS

    def test_default_budget_and_maximum_length_fit_the_cap(self, tmp_path: Path) -> None:
        body = f"{QUERY} " + " ".join(f"word{i}" for i in range(2000))
        pages = [(f"q{i}.md", f"Q {i}", "d" * 200, body) for i in range(7)]
        env = _envelope(tmp_path, pages, n=7, excerpt_chars=PUSH_EXCERPT_CHARS_MAX)
        rendered = f"{PREAMBLE}\n{env['render']['text']}"
        assert "Top match" in rendered
        assert len(rendered) <= HOOK_OUTPUT_CAP_CHARS


# ---------------------------------------------------------------------------
# Resolution: config and the per-turn path
# ---------------------------------------------------------------------------


class TestResolvePushExcerptChars:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_PUSH_EXCERPT_CHARS", raising=False)

    def test_default(self) -> None:
        assert resolve_push_excerpt_chars(None) == PUSH_EXCERPT_CHARS_DEFAULT == 600

    def test_yaml_value_and_zero(self) -> None:
        assert resolve_push_excerpt_chars({"recall": {"push_excerpt_chars": 300}}) == 300
        assert resolve_push_excerpt_chars({"recall": {"push_excerpt_chars": 0}}) == 0

    @pytest.mark.parametrize("bad", [-1, "300", True, 2.5, None])
    def test_bad_yaml_falls_back_to_default(self, bad: object) -> None:
        assert resolve_push_excerpt_chars({"recall": {"push_excerpt_chars": bad}}) == 600

    def test_env_wins_over_yaml_and_zero_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = {"recall": {"push_excerpt_chars": 300}}
        monkeypatch.setenv("ATHENAEUM_PUSH_EXCERPT_CHARS", "0")
        assert resolve_push_excerpt_chars(cfg) == 0
        monkeypatch.setenv("ATHENAEUM_PUSH_EXCERPT_CHARS", "450")
        assert resolve_push_excerpt_chars(cfg) == 450

    def test_malformed_or_negative_env_falls_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = {"recall": {"push_excerpt_chars": 300}}
        for raw in ("lots", "-5"):
            monkeypatch.setenv("ATHENAEUM_PUSH_EXCERPT_CHARS", raw)
            assert resolve_push_excerpt_chars(cfg) == 300

    def test_clamped_to_the_maximum(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert resolve_push_excerpt_chars({"recall": {"push_excerpt_chars": 10**6}}) == 2000
        monkeypatch.setenv("ATHENAEUM_PUSH_EXCERPT_CHARS", "999999")
        assert resolve_push_excerpt_chars(None) == PUSH_EXCERPT_CHARS_MAX


class TestPerTurnPath:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_PUSH_EXCERPT_CHARS", raising=False)
        monkeypatch.setenv("ATHENAEUM_INSTRUMENTATION", "off")

    def _turn(self, cache_dir: Path, **kwargs):
        cache_dir.mkdir(parents=True, exist_ok=True)
        _build_index(cache_dir / "wiki-index.db", _default_pages())
        return build_context_for_turn(
            f"what about {QUERY}?", "sess", cache_dir=cache_dir, use_llm=False, **kwargs
        )

    def test_per_turn_push_carries_the_excerpt_by_default(self, tmp_path: Path) -> None:
        assert _header("Gribwood Mill") in self._turn(tmp_path)["render"]["text"]

    def test_env_zero_turns_it_off(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("ATHENAEUM_PUSH_EXCERPT_CHARS", "0")
        assert "Top match" not in self._turn(tmp_path)["render"]["text"]

    def test_yaml_config_reaches_the_per_turn_path(self, tmp_path: Path) -> None:
        env = self._turn(tmp_path, config={"recall": {"push_excerpt_chars": 0}})
        assert "Top match" not in env["render"]["text"]

    def test_explicit_argument_wins(self, tmp_path: Path) -> None:
        assert "Top match" not in self._turn(tmp_path, excerpt_chars=0)["render"]["text"]


def test_adapter_output_carries_the_excerpt_within_the_cap(tmp_path: Path) -> None:
    """End to end: the real `athenaeum-claude-hook` module, in a subprocess,
    with no config. The injected context has the excerpt and fits the cap."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    _build_index(cache_dir / "wiki-index.db", _default_pages())
    env = {
        "PYTHONPATH": SRC,
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "ATHENAEUM_CACHE_DIR": str(cache_dir),
        "SEARCH_BACKEND": "fts5",
        "ATHENAEUM_INSTRUMENTATION": "off",
    }
    result = subprocess.run(
        [sys.executable, "-m", "athenaeum.claude_code_adapter"],
        input=json.dumps({"prompt": f"tell me about {QUERY} supply", "session_id": "s1"}),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0
    assert result.stderr == ""
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert context.startswith(PREAMBLE)
    assert f"{_header('Gribwood Mill')}\n## Supply" in context
    assert len(context) <= HOOK_OUTPUT_CAP_CHARS


# ---------------------------------------------------------------------------
# The recall instruction line (documented and installed by the setup path)
# ---------------------------------------------------------------------------

#: The one line the setup path adds to the host's instruction file.
RECALL_INSTRUCTION_LINE = (
    "When a `[Knowledge context]` item pushed into your turn looks relevant, "
    "call `recall` yourself with its name or the task's key terms before you "
    "answer, and read what it returns."
)
EXAMPLE_CLAUDE_MD = _REPO / "examples" / "claude-code" / "CLAUDE.md.example"
SIDECAR_GUIDE = _REPO / "docs" / "guides" / "sidecar.md"
CLAUDE_CODE_GUIDE = _REPO / "docs" / "guides" / "claude-code.md"


class TestRecallInstructionLine:
    def test_shipped_example_carries_the_line_on_its_own(self) -> None:
        lines = EXAMPLE_CLAUDE_MD.read_text(encoding="utf-8").splitlines()
        assert lines.count(RECALL_INSTRUCTION_LINE) == 1

    @pytest.mark.parametrize("guide", [SIDECAR_GUIDE, CLAUDE_CODE_GUIDE])
    def test_guides_quote_the_same_line(self, guide: Path) -> None:
        assert RECALL_INSTRUCTION_LINE in guide.read_text(encoding="utf-8")

    def test_documented_setup_command_installs_exactly_the_line_once(
        self, tmp_path: Path
    ) -> None:
        """Run the shell block from the setup steps, twice, against a scratch
        home: the instruction file ends up with the line, once."""
        text = SIDECAR_GUIDE.read_text(encoding="utf-8")
        start = text.index("KEY='pushed into your turn looks relevant'")
        script = text[start : text.index("```", start)]
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        env = {
            "HOME": str(home),
            "PATH": "/usr/bin:/bin",
            "EXAMPLES": str(EXAMPLE_CLAUDE_MD.parent),
        }
        for _ in range(2):
            subprocess.run(["bash", "-c", script], env=env, check=True, timeout=30)
        installed = (home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
        assert installed == RECALL_INSTRUCTION_LINE + "\n"
