# SPDX-License-Identifier: Apache-2.0
"""Handshake-latency regression guard for ``athenaeum serve`` (issue athenaeum#1194).

The bug this file exists to prevent: ``create_server`` ran a corpus-proportional
scan (``resolve_entity_classes`` — one YAML parse per page) *before* ``serve``
could answer the MCP ``initialize`` request. Against the real 23.1k-page corpus
that was 28.4s of user CPU, which blew every MCP client's 30s connect budget, so
the athenaeum server failed to connect in EVERY session and ``remember`` /
``recall`` were silently unavailable.

Why the pre-existing coverage could not catch it (issue athenaeum#1194's own
analysis, and the same shape as athenaeum#1167): ``athenaeum test-mcp``'s
``create_server`` case builds a THREE-PAGE synthetic temp dir. The cost is
corpus-proportional, so a check that green-lights against a 3-page corpus is
structurally incapable of observing a 23k-page failure. Hence the
production-scale fixture below — generated, never the operator's live
``~/knowledge``, which CI cannot see and which no test may depend on.

Two independent guards, deliberately:

1. :class:`TestConstructionDoesNotScanTheCorpus` — STRUCTURAL, and the durable
   one. It asserts the resolver is not *called* during construction, by making
   a call fail the test outright. Cannot flake on machine speed, and states the
   invariant ("no corpus-wide work on the handshake path") directly rather than
   through a proxy.
2. :class:`TestHandshakeLatencyAtProductionScale` — the issue's literal AC, in
   wall-clock, against ~20k pages. Post-fix construction is O(1) in corpus size
   — it reads one small ``types.md`` — so the threshold has ~50x headroom and
   does not depend on the CI runner's PyYAML having the libyaml extension.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

#: Page count for the generated corpus. The real corpus that triggered
#: athenaeum#1194 was 23.5k pages; 20k reproduces the failure with margin while
#: keeping fixture generation to a couple of seconds.
PRODUCTION_SCALE_PAGES = 20_000

#: Wall-clock ceiling for ``create_server`` against that corpus. The MCP client
#: budget is 30s for the WHOLE connect; this is deliberately far tighter, and
#: still ~50x the post-fix measurement (~0.1s), because the fix makes
#: construction O(1) in corpus size rather than merely faster. A regression that
#: reintroduces per-page work at construction costs seconds-to-minutes here, not
#: milliseconds — so a generous threshold loses no sensitivity while making the
#: test immune to a slow or loaded CI runner.
CONSTRUCTION_BUDGET_SECONDS = 5.0


@pytest.fixture(scope="module")
def production_scale_wiki(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A generated ~20k-page wiki. Module-scoped: generated once, not per test."""
    root = tmp_path_factory.mktemp("prod_scale")
    wiki = root / "wiki"
    wiki.mkdir()
    (wiki / "_schema").mkdir()
    (wiki / "_schema" / "types.md").write_text(
        "| Type |\n|---|\n| person |\n| company |\n| project |\n", encoding="utf-8"
    )
    types = ("person", "company", "project", "auto-memory")
    for i in range(PRODUCTION_SCALE_PAGES):
        (wiki / f"page-{i:05d}.md").write_text(
            f"---\nuid: u{i:05d}\ntype: {types[i % len(types)]}\n"
            f"name: Page {i}\ntags: [alpha, beta]\n---\n\nBody of page {i}.\n",
            encoding="utf-8",
        )
    return root


def _build(root: Path, **kwargs):
    pytest.importorskip("fastmcp")
    from athenaeum.mcp_server import create_server

    raw = root / "raw"
    raw.mkdir(exist_ok=True)
    return create_server(raw_root=raw, wiki_root=root / "wiki", **kwargs)


def _tool_fn(server, name: str):
    import asyncio

    async def _run():
        return (await server.get_tool(name)).fn

    return asyncio.run(_run())


class TestConstructionDoesNotScanTheCorpus:
    """The structural guard — construction must never touch the resolver."""

    def test_create_server_does_not_resolve_entity_classes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Booby-trap the corpus scan. If `create_server` calls it — directly or
        # through the memo — construction raises and this test fails, which is
        # exactly the athenaeum#1194 regression stated as an invariant rather than
        # as a stopwatch reading.
        def _boom(*args: object, **kwargs: object):
            raise AssertionError(
                "resolve_entity_classes must not run during create_server "
                "(issue athenaeum#1194: it is a corpus-wide scan on the MCP "
                "handshake path)"
            )

        import athenaeum.entity_schema as es

        monkeypatch.setattr(es, "resolve_entity_classes", _boom)

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "a.md").write_text(
            "---\nuid: u1\ntype: person\nname: Alice\n---\n\nBody.\n", encoding="utf-8"
        )
        server = _build(tmp_path)
        assert server is not None

    def test_tools_list_does_not_resolve_entity_classes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `tools/list` is what a client sends immediately after `initialize`, so
        # deferring the scan into a lazily-built TOOL SCHEMA would move the
        # 28s rather than remove it, and the session would still time out.
        import asyncio

        import athenaeum.entity_schema as es

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "a.md").write_text(
            "---\nuid: u1\ntype: person\nname: Alice\n---\n\nBody.\n", encoding="utf-8"
        )
        server = _build(tmp_path)

        def _boom(*args: object, **kwargs: object):
            raise AssertionError(
                "resolve_entity_classes must not run during tools/list"
            )

        monkeypatch.setattr(es, "resolve_entity_classes", _boom)
        tools = asyncio.run(server.list_tools())
        assert {"recall", "entity_schema"} <= {t.name for t in tools}

    def test_entity_schema_tool_does_perform_the_scan(self, tmp_path: Path) -> None:
        # The other half of the contract: the work is DEFERRED, not dropped.
        # An observed-undeclared class can only be known by scanning, so its
        # presence here proves the tool really resolves the corpus.
        from athenaeum.entity_schema import clear_entity_class_cache

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "_schema").mkdir()
        (wiki / "_schema" / "types.md").write_text("| Type |\n|---|\n| person |\n")
        (wiki / "a.md").write_text(
            "---\nuid: u1\ntype: auto-memory\nname: Memory\n---\n\nBody.\n",
            encoding="utf-8",
        )
        clear_entity_class_cache()
        server = _build(tmp_path)
        result = _tool_fn(server, "entity_schema")()
        by_name = {c["name"]: c for c in result["classes"]}
        assert by_name["auto-memory"]["observed"] is True
        assert by_name["auto-memory"]["count"] == 1


class TestHandshakeLatencyAtProductionScale:
    """The issue's literal AC, against a GENERATED production-scale corpus."""

    def test_create_server_is_fast_against_20k_pages(
        self, production_scale_wiki: Path
    ) -> None:
        start = time.perf_counter()
        server = _build(production_scale_wiki)
        elapsed = time.perf_counter() - start
        assert server is not None
        assert elapsed < CONSTRUCTION_BUDGET_SECONDS, (
            f"create_server took {elapsed:.2f}s against "
            f"{PRODUCTION_SCALE_PAGES:,} pages (budget {CONSTRUCTION_BUDGET_SECONDS}s). "
            "Something on the MCP handshake path is doing corpus-proportional "
            "work again — see issue athenaeum#1194."
        )

    def test_declared_classes_still_reach_the_recall_schema(
        self, production_scale_wiki: Path
    ) -> None:
        # Construction got cheap by reading types.md ALONE; prove that the
        # config-derived `type` description athenaeum#964 requires survived it.
        server = _build(production_scale_wiki)
        doc = _tool_fn(server, "recall").__doc__ or ""
        assert "person" in doc
        assert "company" in doc
        assert "project" in doc
