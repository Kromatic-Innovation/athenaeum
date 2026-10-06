# SPDX-License-Identifier: Apache-2.0
"""``access: personal`` reachability by exact name, and the access-withheld
breadcrumb (issue athenaeum#1967).

AC1 traced the omission this issue reported to the RANKING layer, not to
any audience/PII/``recallable`` filter: every one of those is a strict
no-op for the owner/default caller (``caller_audience=None``) in all three
search backends (see ``athenaeum.search.find_personal_page_by_exact_name``'s
docstring for the exact citations). The operator's ruling (occam:disposition,
2026-10-05) settles AC3 against that finding regardless of mechanism: an
``access: personal`` page must be reachable BY NAME in default recall, CLI
and MCP, even when ranking buried it. AC2 adds a withheld-count breadcrumb
whenever a RESTRICTED caller's audience check removes results after
ranking, so a short list is distinguishable from a miss.

Fixtures here are entirely invented (names, uids, content) — never the
operator's live corpus, per this issue's own public-repo constraint.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from athenaeum.mcp_server import recall_search
from athenaeum.models import parse_frontmatter, render_frontmatter, stamp_tombstone
from athenaeum.search import FTS5Backend, find_personal_page_by_exact_name

# Invented name used as the exact-match query throughout — never a real
# person's name.
_TARGET_NAME = "Avery Thistlewood"
_CONTROL_NAME = "Marguerite Oakhollow"


def _noise_pages(wiki: Path, *, around: str, count: int = 6) -> None:
    """Write pages that out-rank ``around`` under ordinary BM25 ranking.

    Each noise page repeats the query terms heavily across the
    heavily-weighted ``name``/``tags``/``aliases`` columns so it reliably
    beats a page that only carries the terms once, in its own ``name:``
    field — the burial this issue's rescue mechanism exists to survive.
    """
    terms = around.lower().split()
    for i in range(count):
        repeated = " ".join(terms * 4)
        (wiki / f"noise-{i}.md").write_text(
            f"---\n"
            f"name: Noise Page {i} {repeated}\n"
            f"type: note\n"
            f"tags:\n  - {terms[0]}\n  - {terms[-1]}\n"
            f"aliases:\n  - {repeated}\n"
            f"---\n\n"
            f"Routine unrelated content, padding padding padding, page {i}.\n"
        )


def _personal_page(wiki: Path, filename: str, name: str, *, extra_tags: str = "") -> None:
    (wiki / filename).write_text(
        f"---\n"
        f"name: {name}\n"
        f"type: person\n"
        f"access: personal\n"
        f"tags:\n  - confidential\n{extra_tags}"
        f"---\n\n"
        f"A short invented bio for this fixture person.\n"
    )


def _confidential_page(wiki: Path, filename: str, name: str) -> None:
    (wiki / filename).write_text(
        f"---\n"
        f"name: {name}\n"
        f"type: person\n"
        f"access: confidential\n"
        f"tags:\n  - confidential\n"
        f"---\n\n"
        f"A short invented bio for this fixture person.\n"
    )


class TestAC1RootCauseIsRankingNotAFilter:
    """Pins AC1's finding: no audience/PII/recallable filter touches the
    owner caller — the FTS5 backend's own query path adds no audience
    predicate at all when ``caller_audience is None``."""

    def test_fts5_query_has_no_audience_predicate_for_owner(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        cache = tmp_path / "cache"
        _personal_page(wiki, "target.md", _TARGET_NAME)
        FTS5Backend().build_index(wiki, cache)

        hits = FTS5Backend().query(_TARGET_NAME, cache, n=10, caller_audience=None)
        assert any(h[0] == "target.md" for h in hits), (
            "owner caller must see an access: personal page through the "
            "backend's own query path with no filter involved"
        )


class TestAC3ReachableByNameForOwner:
    def test_buried_personal_page_is_rescued_for_owner(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        cache = tmp_path / "cache"
        _personal_page(wiki, "target.md", _TARGET_NAME)
        _noise_pages(wiki, around=_TARGET_NAME)
        FTS5Backend().build_index(wiki, cache)

        # Control: prove the burial is real, not assumed. A page with the
        # SAME name but access: confidential (a level the ruling did NOT
        # name) must stay buried — confirming the noise setup genuinely
        # crowds the top_k, and that this issue's fix does not widen
        # reachability for any access level other than `personal`.
        wiki_control = tmp_path / "wiki_control"
        wiki_control.mkdir()
        cache_control = tmp_path / "cache_control"
        _confidential_page(wiki_control, "target.md", _TARGET_NAME)
        _noise_pages(wiki_control, around=_TARGET_NAME)
        FTS5Backend().build_index(wiki_control, cache_control)
        control_hits = FTS5Backend().query(_TARGET_NAME, cache_control, n=3)
        assert not any(h[0] == "target.md" for h in control_hits), (
            "test setup invariant: the confidential control page must be "
            "genuinely buried past top_k=3 by the noise pages, or this test "
            "proves nothing"
        )

        result = recall_search(
            wiki,
            _TARGET_NAME,
            3,
            search_backend="fts5",
            cache_dir=cache,
            caller_audience=None,
        )
        assert "target.md" in result

    def test_confidential_access_is_not_widened(self, tmp_path: Path) -> None:
        """The ruling names `access: personal` only — confidential pages
        buried by ranking stay buried for the owner, exactly as before this
        issue."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        cache = tmp_path / "cache"
        _confidential_page(wiki, "target.md", _TARGET_NAME)
        _noise_pages(wiki, around=_TARGET_NAME)
        FTS5Backend().build_index(wiki, cache)

        result = recall_search(
            wiki,
            _TARGET_NAME,
            3,
            search_backend="fts5",
            cache_dir=cache,
            caller_audience=None,
        )
        assert _TARGET_NAME not in result

    def test_restricted_caller_reachability_is_unchanged(self, tmp_path: Path) -> None:
        """A restricted caller's access to an `access: personal` page is NOT
        widened by this issue — still governed solely by an explicit
        `audience:` grant, same as before."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        cache = tmp_path / "cache"
        _personal_page(wiki, "target.md", _TARGET_NAME)
        FTS5Backend().build_index(wiki, cache)

        result = recall_search(
            wiki,
            _TARGET_NAME,
            10,
            search_backend="fts5",
            cache_dir=cache,
            caller_audience={"some-unrelated-role"},
        )
        assert "target.md" not in result
        # Pins the owner-only gate itself, not just Layer C's filename
        # drop: an exact-name query for a real `access: personal` page must
        # look EXACTLY like a genuine miss to a restricted caller. If the
        # owner-only gate around the rescue helper is ever bypassed for a
        # restricted caller, the helper still finds the page by name
        # (bypassing Layer B's in-query audience predicate), Layer C then
        # drops it as unauthorized, and this breadcrumb line fires -- an
        # existence oracle: an exact personal name now reads differently
        # from a name that matches nothing at all.
        assert "withheld" not in result

    def test_rescue_respects_an_explicit_type_filter(self, tmp_path: Path) -> None:
        """A caller who explicitly narrowed by `type=` must not have the
        rescue silently violate that filter."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        cache = tmp_path / "cache"
        _personal_page(wiki, "target.md", _TARGET_NAME)
        _noise_pages(wiki, around=_TARGET_NAME)
        FTS5Backend().build_index(wiki, cache)

        result = recall_search(
            wiki,
            _TARGET_NAME,
            3,
            search_backend="fts5",
            cache_dir=cache,
            caller_audience=None,
            type_filter="project",
        )
        assert "target.md" not in result


class TestFindPersonalPageByExactNameUnit:
    """Direct unit coverage of the helper itself (cheap SQL lookup, fresh
    on-disk re-check, access-level scoping)."""

    def test_exact_match_only(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        cache = tmp_path / "cache"
        _personal_page(wiki, "target.md", _TARGET_NAME)
        FTS5Backend().build_index(wiki, cache)

        assert find_personal_page_by_exact_name(_TARGET_NAME, cache, wiki) == (
            "target.md",
            _TARGET_NAME,
            "person",
        )
        assert find_personal_page_by_exact_name("Avery", cache, wiki) is None
        assert find_personal_page_by_exact_name("", cache, wiki) is None

    def test_case_insensitive(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        cache = tmp_path / "cache"
        _personal_page(wiki, "target.md", _TARGET_NAME)
        FTS5Backend().build_index(wiki, cache)

        assert find_personal_page_by_exact_name(
            _TARGET_NAME.upper(), cache, wiki
        ) is not None

    def test_non_personal_access_returns_none(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        cache = tmp_path / "cache"
        _confidential_page(wiki, "target.md", _TARGET_NAME)
        FTS5Backend().build_index(wiki, cache)

        assert find_personal_page_by_exact_name(_TARGET_NAME, cache, wiki) is None

    def test_stale_index_does_not_resurrect_a_fold_tombstone(
        self, tmp_path: Path
    ) -> None:
        """Issue athenaeum#716: a folded page must be invisible to recall
        UNCONDITIONALLY, including through this rescue.

        The hole this closes is a real interaction between two changes, not a
        hypothetical: a fold tombstones its source WITHOUT rebuilding the FTS5
        index, so the tombstoned page still has a live row here — and this
        helper's result is PREPENDED by its callers ahead of every backend
        hit, bypassing the index-build exclusion that normally keeps a
        tombstone out of recall. The access-level re-check above already
        proves the Layer-C pass runs; this proves it covers the
        page-level ``embedded: false`` override too.
        """
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        cache = tmp_path / "cache"
        _personal_page(wiki, "target.md", _TARGET_NAME)
        FTS5Backend().build_index(wiki, cache)

        # Positive control: reachable while live, so the assertion below is
        # about the tombstone and not about a broken fixture.
        assert find_personal_page_by_exact_name(_TARGET_NAME, cache, wiki) is not None

        # Fold it, leaving the index deliberately STALE (no rebuild) — the
        # exact state the corpus is in between a fold and the next reindex.
        page = wiki / "target.md"
        text = page.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(text)
        page.write_text(
            render_frontmatter(stamp_tombstone(meta, "canonical-slug")) + body,
            encoding="utf-8",
        )

        assert find_personal_page_by_exact_name(_TARGET_NAME, cache, wiki) is None

    def test_no_index_degrades_to_none(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        cache = tmp_path / "cache"  # never built
        assert find_personal_page_by_exact_name(_TARGET_NAME, cache, wiki) is None

    def test_stale_index_does_not_resurrect_a_changed_access_level(
        self, tmp_path: Path
    ) -> None:
        """Layer-C discipline: the SQL hit is a candidate, re-verified
        against FRESH on-disk frontmatter — a page the index still lists
        but which no longer carries `access: personal` on disk must not be
        rescued."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        cache = tmp_path / "cache"
        _personal_page(wiki, "target.md", _TARGET_NAME)
        FTS5Backend().build_index(wiki, cache)
        # Flip access on disk WITHOUT rebuilding the index.
        _confidential_page(wiki, "target.md", _TARGET_NAME)

        assert find_personal_page_by_exact_name(_TARGET_NAME, cache, wiki) is None


class TestAC2AccessWithheldBreadcrumb:
    """The withheld-count line: honest (derived from the real delta, never a
    second guess), silent at zero, and distinguishes a short list from a
    miss for a restricted caller."""

    def test_zero_withheld_is_silent(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        cache = tmp_path / "cache"
        (wiki / "open.md").write_text(
            f"---\nname: {_CONTROL_NAME}\ntype: note\naccess: open\n---\n\n"
            "An open page everyone may read.\n"
        )
        FTS5Backend().build_index(wiki, cache)

        result = recall_search(
            wiki,
            _CONTROL_NAME,
            5,
            search_backend="fts5",
            cache_dir=cache,
            caller_audience={"some-role"},
        )
        assert "withheld" not in result

    def test_owner_sees_no_withheld_line_even_with_personal_pages(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        cache = tmp_path / "cache"
        _personal_page(wiki, "target.md", _TARGET_NAME)
        FTS5Backend().build_index(wiki, cache)

        result = recall_search(
            wiki,
            _TARGET_NAME,
            5,
            search_backend="fts5",
            cache_dir=cache,
            caller_audience=None,
        )
        assert "withheld" not in result
        assert _TARGET_NAME in result

    def test_withheld_count_matches_the_actual_delta(self, tmp_path: Path) -> None:
        """AC2's breadcrumb is specifically for a POST-RANKING removal —
        this codebase's own Layer A/B/C vocabulary (``search.py``'s module
        docstring) draws that line precisely at Layer C: Layer B's audience
        predicate is pushed INTO the backend query, BEFORE ranking/``LIMIT``
        (so a forbidden row never occupies a ranked slot at all — nothing
        was "removed after ranking" there, it never had a rank), while
        Layer C re-checks FRESH on-disk frontmatter against an ALREADY
        ranked/selected row, at render time. This test drives Layer C
        directly — build the index while two pages are still `access: open`
        (so Layer B's STALE index predicate admits them to the ranked
        window for any caller), then flip them to `access: personal` with
        no grant ON DISK without rebuilding, which is exactly the "a page
        whose audience changed since the last rebuild" scenario Layer C's
        own docstring names. Two pages flipped -> exactly '2', never a
        guess at the filter.
        """
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        cache = tmp_path / "cache"
        (wiki / "target-1.md").write_text(
            f"---\nname: {_TARGET_NAME} One\ntype: person\naccess: open\n---\n\n"
            "An invented bio, initially open.\n"
        )
        (wiki / "target-2.md").write_text(
            f"---\nname: {_TARGET_NAME} Two\ntype: person\naccess: open\n---\n\n"
            "An invented bio, initially open.\n"
        )
        (wiki / "open.md").write_text(
            f"---\nname: {_TARGET_NAME} Open\ntype: note\naccess: open\n---\n\n"
            "An open page naming the same query terms.\n"
        )
        FTS5Backend().build_index(wiki, cache)

        # Flip the two targets to access: personal, no grant — ON DISK only;
        # the FTS5 index still stamps them `open` (stale).
        (wiki / "target-1.md").write_text(
            f"---\nname: {_TARGET_NAME} One\ntype: person\naccess: personal\n---\n\n"
            "An invented bio, initially open.\n"
        )
        (wiki / "target-2.md").write_text(
            f"---\nname: {_TARGET_NAME} Two\ntype: person\naccess: personal\n---\n\n"
            "An invented bio, initially open.\n"
        )

        result = recall_search(
            wiki,
            _TARGET_NAME,
            10,
            search_backend="fts5",
            cache_dir=cache,
            caller_audience={"no-grant-role"},
        )
        assert "withheld 2 result(s)" in result, result
        # The open page is still served — a short list, not a miss.
        assert f"{_TARGET_NAME} Open" in result

    def test_all_withheld_still_distinguishes_from_a_genuine_miss(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        cache = tmp_path / "cache"
        (wiki / "target.md").write_text(
            f"---\nname: {_TARGET_NAME}\ntype: person\naccess: open\n---\n\n"
            "An invented bio, initially open.\n"
        )
        FTS5Backend().build_index(wiki, cache)
        # Stale-index flip, same shape as the test above.
        (wiki / "target.md").write_text(
            f"---\nname: {_TARGET_NAME}\ntype: person\naccess: personal\n---\n\n"
            "An invented bio, initially open.\n"
        )

        withheld_result = recall_search(
            wiki,
            _TARGET_NAME,
            5,
            search_backend="fts5",
            cache_dir=cache,
            caller_audience={"no-grant-role"},
        )
        miss_result = recall_search(
            wiki,
            "ThisTermMatchesNothingAtAllXyz",
            5,
            search_backend="fts5",
            cache_dir=cache,
            caller_audience={"no-grant-role"},
        )
        assert withheld_result != miss_result
        assert "withheld" in withheld_result
        assert "No wiki pages matched" in miss_result
        assert "withheld" not in miss_result


class TestCliRecallSurface:
    """Same two behaviors (AC2/AC3), driven through the CLI entry point —
    the ruling explicitly names both CLI and MCP."""

    def _cli_args(
        self,
        *,
        query: str,
        path: Path,
        cache_dir: Path,
        audience: str | None = None,
        top_k: int = 5,
    ) -> argparse.Namespace:
        return argparse.Namespace(
            query=query,
            top_k=top_k,
            path=path,
            cache_dir=cache_dir,
            backend="fts5",
            audience=audience,
            with_pii=False,
            usage_class=[],
            as_of=None,
            type_filter=[],
        )

    def test_cli_rescues_buried_personal_page_for_owner(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from athenaeum._cmd_query import cmd_recall

        knowledge_root = tmp_path / "knowledge"
        wiki = knowledge_root / "wiki"
        wiki.mkdir(parents=True)
        cache = tmp_path / "cache"
        _personal_page(wiki, "target.md", _TARGET_NAME)
        _noise_pages(wiki, around=_TARGET_NAME)
        FTS5Backend().build_index(wiki, cache)

        rc = cmd_recall(
            self._cli_args(
                query=_TARGET_NAME, path=knowledge_root, cache_dir=cache, top_k=3
            )
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "target.md" in out

    def test_cli_restricted_caller_reachability_is_unchanged(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """CLI sibling of
        ``test_restricted_caller_reachability_is_unchanged``: pins the
        owner-only gate around the rescue helper in ``cmd_recall`` itself,
        not just the filename's absence from stdout. A restricted caller
        querying an exact `access: personal` name must look EXACTLY like a
        genuine miss -- no `withheld` breadcrumb on stderr either, or the
        rescue helper has turned into a per-name existence oracle."""
        from athenaeum._cmd_query import cmd_recall

        knowledge_root = tmp_path / "knowledge"
        wiki = knowledge_root / "wiki"
        wiki.mkdir(parents=True)
        cache = tmp_path / "cache"
        _personal_page(wiki, "target.md", _TARGET_NAME)
        FTS5Backend().build_index(wiki, cache)

        rc = cmd_recall(
            self._cli_args(
                query=_TARGET_NAME,
                path=knowledge_root,
                cache_dir=cache,
                audience="some-unrelated-role",
                top_k=10,
            )
        )
        assert rc == 0
        captured = capsys.readouterr()
        assert "target.md" not in captured.out
        assert "withheld" not in captured.err

    def test_cli_reports_withheld_count_on_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Stale-index flip (see
        # TestAC2AccessWithheldBreadcrumb.test_withheld_count_matches_the_actual_delta
        # for why this is what actually drives a Layer-C, POST-ranking drop
        # rather than Layer B's in-query predicate).
        from athenaeum._cmd_query import cmd_recall

        knowledge_root = tmp_path / "knowledge"
        wiki = knowledge_root / "wiki"
        wiki.mkdir(parents=True)
        cache = tmp_path / "cache"
        (wiki / "target.md").write_text(
            f"---\nname: {_TARGET_NAME}\ntype: person\naccess: open\n---\n\n"
            "An invented bio, initially open.\n"
        )
        FTS5Backend().build_index(wiki, cache)
        (wiki / "target.md").write_text(
            f"---\nname: {_TARGET_NAME}\ntype: person\naccess: personal\n---\n\n"
            "An invented bio, initially open.\n"
        )

        rc = cmd_recall(
            self._cli_args(
                query=_TARGET_NAME,
                path=knowledge_root,
                cache_dir=cache,
                audience="no-grant-role",
            )
        )
        assert rc == 0
        captured = capsys.readouterr()
        assert "target.md" not in captured.out
        assert "withheld" in captured.err
        assert "withheld 1 result(s)" in captured.err
