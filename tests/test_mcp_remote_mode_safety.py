# SPDX-License-Identifier: Apache-2.0
"""The MCP server as a long-lived, multi-client process (issue athenaeum#2385).

Under stdio one process serves one conversation and one client, and four
assumptions ride on that. Under ``--transport http`` one process serves many
conversations concurrently and all four need fixing or proving. One class per
acceptance criterion:

- :class:`TestInferredSourceDefaultPerTransport` (AC1) — the defaulted
  wiki-level ``source`` scalar. Asserted against
  ``repair._DEFAULTED_INFERRED_SOURCE`` itself rather than against a literal,
  because the stdio value staying byte-identical to that scalar is what keeps
  ``repair --backfill-sources`` selecting the right memories.
- :class:`TestSessionSubdirPerTransport` (AC2) — the ``raw/<name>/``
  subdirectory, and that a note written under HTTP defaults is actually
  RECALLABLE (the athenaeum#2313 extra-root refresh), not merely filed under a
  different name.
- :class:`TestToolUseIdNoneIsInert` (AC3) — ``ToolUseSessionResolver`` and
  push-metrics recording across many calls from two simulated sessions when
  ``active_tool_use_id()`` is ``None``. Carries its own positive control: the
  same calls on a stdio server DO write a ledger row, so "no rows" is a real
  finding and not a disabled-instrumentation artifact.
- :class:`TestConcurrentRememberAndRecall` (AC4) — N parallel ``remember``
  calls interleaved with parallel ``recall`` calls against ONE server. The
  file writes never raced; the post-write index refresh did, and
  :data:`athenaeum.mcp_server._INDEX_REFRESH_LOCK` is what fixes it. Both the
  durable artifact and the serialization mechanism are pinned, the mechanism
  deterministically.
- :class:`TestSessionResolverCommentStatesTheHttpCase` (AC5) — the comment
  above ``_session_resolver``.
- :class:`TestServedSurfaceIsTransportInvariant` — the issue's "branch only
  the defaults, never the tool schema" constraint, as a test: the two
  transports serve byte-identical ``tools/list`` payloads.

Threads, never ``fork()``: ``tests/test_promptwitness_ledger.py``'s
eight-appender test is flaky under ``fork()`` from a multi-threaded parent,
and every assertion here is on a durable artifact (files on disk, manifest
contents, what ``recall`` returns) rather than on a process exit code or a
wall-clock interleaving.

Unmarked: no network, no LLM call, and no corpus beyond a one-page temp wiki.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import threading
from pathlib import Path

import pytest

from athenaeum.models import parse_frontmatter

#: Session-id variables ``push_metrics.resolve_session_id`` reads. Cleared or
#: set explicitly by :func:`_claude_session_env` — never inherited, or a
#: developer running the suite inside a Claude Code session would get
#: different answers from the same assertions.
SESSION_ENV_VARS = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")

#: Two simulated client sessions, for the AC3 and AC4 "from two sessions"
#: wording. They are DISTINCT labels in the written content so a cross-session
#: mixup is visible in the artifact rather than only in a count.
SESSION_LABELS = ("alpha", "bravo")

#: How many ``remember``/``recall`` pairs the concurrency test races. Sized to
#: reproduce the manifest race it pins on a contended machine while keeping
#: one FTS5 incremental build per write affordable in the default suite.
CONCURRENT_WRITES = 24

#: Generous, because this suite shares a CI runner with a parallel shard: a
#: thread that has not finished in this long has hung, not merely lost the
#: scheduler. Asserted on explicitly, so a timeout fails loudly rather than
#: silently weakening the assertions below it.
JOIN_TIMEOUT_S = 180.0


@pytest.fixture(autouse=True)
def _claude_session_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every ambient Claude session id for the whole module.

    Individual tests that need one present set it themselves — a later
    ``monkeypatch.setenv`` in the same stack wins over this earlier
    ``delenv``.
    """
    for name in SESSION_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """A knowledge root with one compiled wiki page and an empty ``raw/``."""
    knowledge = tmp_path / "knowledge"
    (knowledge / "wiki").mkdir(parents=True)
    (knowledge / "raw").mkdir(parents=True)
    (knowledge / "wiki" / "widget.md").write_text(
        "---\nuid: widget\nname: Widget Co\ntype: organization\n---\n\n"
        "Widget Co builds widgets and runs widget calibration.\n",
        encoding="utf-8",
    )
    return knowledge


def _config(*roots: str) -> dict[str, object]:
    """An FTS5 config whose extra intake roots are exactly *roots*.

    Pinned explicitly rather than inherited from
    ``athenaeum.config._DEFAULTS`` so a test asserting "the note is
    recallable" fails if the ROOT wiring breaks, instead of passing because
    some other default root happened to cover it.
    """
    return {
        "search_backend": "fts5",
        "recall": {"extra_intake_roots": list(roots)},
    }


def _server(corpus_root: Path, **kwargs: object):
    pytest.importorskip("fastmcp")
    from athenaeum.mcp_server import create_server

    kwargs.setdefault("config", {})
    return create_server(
        raw_root=corpus_root / "raw",
        wiki_root=corpus_root / "wiki",
        cache_dir=corpus_root.parent / "cache",
        **kwargs,
    )


def _tool(server, name: str):
    """The registered tool's plain Python callable.

    The same ``get_tool(...).fn`` route ``tests/test_mcp_remote_profile.py``
    takes — what a client reaches, without standing up a transport.
    """

    async def _run():
        return (await server.get_tool(name)).fn

    return asyncio.run(_run())


def _written(corpus_root: Path, subdir: str) -> list[Path]:
    return sorted((corpus_root / "raw" / subdir).glob("*.md"))


def _only_write(corpus_root: Path, subdir: str) -> dict:
    """Parsed frontmatter of the single file under ``raw/<subdir>/``."""
    files = _written(corpus_root, subdir)
    assert len(files) == 1, f"expected exactly one write under raw/{subdir}, got {files}"
    meta, _ = parse_frontmatter(files[0].read_text(encoding="utf-8"))
    return meta


def _build_fts5_index(corpus_root: Path, *extra_roots: Path) -> None:
    from athenaeum.search import FTS5Backend

    FTS5Backend().build_index(
        corpus_root / "wiki",
        corpus_root.parent / "cache",
        extra_roots=list(extra_roots),
    )


def _manifest_entries(corpus_root: Path, prefix: str) -> list[str]:
    manifest = corpus_root.parent / "cache" / "fts5-manifest.json"
    if not manifest.is_file():
        return []
    hashes = json.loads(manifest.read_text(encoding="utf-8")).get("hashes") or {}
    return sorted(name for name in hashes if name.startswith(f"{prefix}/"))


class TestInferredSourceDefaultPerTransport:
    """AC1: the defaulted wiki-level ``source`` scalar, per transport."""

    def test_stdio_stamps_the_scalar_repair_selects_on(self, corpus: Path) -> None:
        from athenaeum.repair import _DEFAULTED_INFERRED_SOURCE

        server = _server(corpus)
        _tool(server, "remember")(content="Widget Co ships on Fridays.")

        meta = _only_write(corpus, "claude-session")
        # The equality `repair`'s candidate gate actually performs
        # (`repair.py`: `meta.get("source") != _DEFAULTED_INFERRED_SOURCE`),
        # asserted against the scalar itself rather than a literal copy.
        assert meta["source"] == _DEFAULTED_INFERRED_SOURCE

    def test_http_stamps_the_host_neutral_scalar_instead(self, corpus: Path) -> None:
        from athenaeum.repair import _DEFAULTED_INFERRED_SOURCE

        server = _server(corpus, transport="http")
        _tool(server, "remember")(content="Widget Co ships on Fridays.")

        meta = _only_write(corpus, "remote-session")
        assert meta["source"] == "remote:inferred"
        # The point of the criterion: a write from an unknown host is not a
        # `repair --backfill-sources` candidate, because there is no Claude
        # transcript for that pass to go and read.
        assert meta["source"] != _DEFAULTED_INFERRED_SOURCE

    def test_the_two_transports_disagree(self, corpus: Path, tmp_path: Path) -> None:
        """Both defaults in one assertion, so neither can drift into the other.

        A separate corpus per transport: the whole point is that the two
        writes land in different places, so one corpus could not hold both
        under :func:`_only_write`'s single-file check.
        """
        other = tmp_path / "knowledge-http"
        (other / "wiki").mkdir(parents=True)
        (other / "raw").mkdir(parents=True)

        _tool(_server(corpus), "remember")(content="A fact.")
        _tool(_server(other, transport="http"), "remember")(content="A fact.")

        assert _only_write(corpus, "claude-session")["source"] == "claude:inferred"
        assert _only_write(other, "remote-session")["source"] == "remote:inferred"

    @pytest.mark.parametrize("transport", [None, "http"])
    def test_an_explicit_source_is_never_overridden(
        self, corpus: Path, transport: str | None
    ) -> None:
        """The branch is on the DEFAULT only — a declared source wins on both."""
        server = _server(corpus, transport=transport)
        _tool(server, "remember")(
            content="A fact.", sources="api:apollo:2026-05-09"
        )

        subdir = "claude-session" if transport is None else "remote-session"
        assert _only_write(corpus, subdir)["source"] == "api:apollo:2026-05-09"

    def test_remember_write_defaults_to_the_stdio_scalar(self, corpus: Path) -> None:
        """Every library caller of ``remember_write`` is unchanged.

        The transport branch lives in the TOOL, which passes
        ``inferred_source=``; the module-level function keeps the stdio
        default, so the CLI and every existing test path are byte-identical.
        """
        from athenaeum.mcp_server import remember_write
        from athenaeum.repair import _DEFAULTED_INFERRED_SOURCE

        remember_write(corpus / "raw", "A fact.")
        assert _only_write(corpus, "claude-session")["source"] == (
            _DEFAULTED_INFERRED_SOURCE
        )


class TestEnvDerivedProvenanceExtrasPerTransport:
    """AC1's other half, found by the async review bot on PR 2418.

    The defaulted scalar was not the only thing claiming a Claude origin.
    ``_default_inferred_extras`` also stamps ``model`` and ``originSessionId``
    (issue athenaeum#2148), both read from the SERVER process's environment.
    Under stdio that environment is the one conversation being served. Under
    http it is the shell the operator ran ``athenaeum serve`` in, so a remote
    host's note was stamped with a specific, real, wrong session id — a
    sharper version of the mislabel AC1 exists to stop, since it names an
    identifiable session rather than guessing a vendor.

    The issue body's claim that ``_resolve_inferred_session_id`` "already
    declines to stamp a session under HTTP" is true only of the WIDER
    ``CLAUDE_SESSION_ID`` alias; it reads ``CLAUDE_CODE_SESSION_ID`` on every
    transport. These tests are what distinguishes the two.
    """

    #: Set in the server's environment for both tests, so the ONLY difference
    #: between them is the transport.
    OPERATOR_ENV = {
        "CLAUDE_CODE_SESSION_ID": "operator-shell-session",
        "ANTHROPIC_MODEL": "claude-model-of-the-operators-shell",
    }

    def _write(
        self, corpus: Path, monkeypatch: pytest.MonkeyPatch, transport: str | None
    ) -> dict:
        for name, value in self.OPERATOR_ENV.items():
            monkeypatch.setenv(name, value)
        _tool(_server(corpus, transport=transport), "remember")(content="A fact.")
        subdir = "claude-session" if transport is None else "remote-session"
        return _only_write(corpus, subdir)

    def test_http_stamps_neither_model_nor_origin_session(
        self, corpus: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        meta = self._write(corpus, monkeypatch, "http")
        assert "originSessionId" not in meta
        assert "model" not in meta
        # Absent, not stamped `unknown`: an absent key already means
        # "provenance unknown", while a present one asserts something.
        assert self.OPERATOR_ENV["CLAUDE_CODE_SESSION_ID"] not in str(meta)
        assert self.OPERATOR_ENV["ANTHROPIC_MODEL"] not in str(meta)

    def test_stdio_still_stamps_both(
        self, corpus: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Positive control, and the athenaeum#2148 behaviour unchanged.

        Without this, the assertion above would pass on a tree where the
        extras stopped being stamped at all.
        """
        meta = self._write(corpus, monkeypatch, None)
        assert meta["originSessionId"] == self.OPERATOR_ENV["CLAUDE_CODE_SESSION_ID"]
        assert meta["model"] == self.OPERATOR_ENV["ANTHROPIC_MODEL"]

    def test_the_helper_gate_is_the_only_difference(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The two answers straight off ``_default_inferred_extras``."""
        from athenaeum.mcp_server import _default_inferred_extras

        for name, value in self.OPERATOR_ENV.items():
            monkeypatch.setenv(name, value)
        assert _default_inferred_extras("A fact.") == {
            "originSessionId": self.OPERATOR_ENV["CLAUDE_CODE_SESSION_ID"],
            "model": self.OPERATOR_ENV["ANTHROPIC_MODEL"],
        }
        assert (
            _default_inferred_extras("A fact.", stamp_env_provenance=False) == {}
        )

    def test_a_caller_declared_value_is_still_never_overridden(
        self, corpus: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pre-stamped frontmatter wins on both transports, as before."""
        for name, value in self.OPERATOR_ENV.items():
            monkeypatch.setenv(name, value)
        _tool(_server(corpus, transport="http"), "remember")(
            content="---\noriginSessionId: declared-by-the-caller\n---\n\nA fact.\n"
        )
        meta = _only_write(corpus, "remote-session")
        assert meta["originSessionId"] == "declared-by-the-caller"


class TestSessionSubdirPerTransport:
    """AC2: the default ``raw/`` subdirectory, and that it is recallable."""

    def test_stdio_writes_to_claude_session(self, corpus: Path) -> None:
        _tool(_server(corpus), "remember")(content="A fact.")
        assert len(_written(corpus, "claude-session")) == 1
        assert not (corpus / "raw" / "remote-session").exists()

    def test_http_writes_to_remote_session(self, corpus: Path) -> None:
        _tool(_server(corpus, transport="http"), "remember")(content="A fact.")
        assert len(_written(corpus, "remote-session")) == 1
        # The criterion is "not `claude-session`", so assert the absence
        # directly: a change that wrote to BOTH would satisfy the positive
        # half alone.
        assert not (corpus / "raw" / "claude-session").exists()

    def test_an_explicit_source_still_picks_the_subdirectory(
        self, corpus: Path
    ) -> None:
        _tool(_server(corpus, transport="http"), "remember")(
            content="A fact.", source="manual"
        )
        assert len(_written(corpus, "manual")) == 1
        assert not (corpus / "raw" / "remote-session").exists()

    @pytest.mark.parametrize("transport", [None, "http"])
    def test_an_empty_source_still_errors(
        self, corpus: Path, transport: str | None
    ) -> None:
        """Byte-identity guard for the stdio path, applied to both.

        ``source`` became ``None``-defaulted to carry the transport branch.
        An empty STRING is a different input and must keep returning
        ``remember_write``'s existing error rather than quietly landing in
        the default subdirectory, which a ``source or <default>`` resolution
        would have done.
        """
        result = _tool(_server(corpus, transport=transport), "remember")(
            content="A fact.", source=""
        )
        assert "at least one alphanumeric character" in result
        assert list((corpus / "raw").iterdir()) == []

    def test_remote_session_is_a_default_extra_intake_root(self, tmp_path: Path) -> None:
        """Documented AND wired: the chosen name is a default recall root."""
        from athenaeum.config import load_config

        assert load_config(tmp_path)["recall"]["extra_intake_roots"] == [
            "raw/auto-memory",
            "raw/claude-session",
            "raw/remote-session",
        ]

    def test_init_creates_the_remote_session_root(self, tmp_path: Path) -> None:
        """``athenaeum init`` seeds it, so a long-running HTTP server resolves
        it into its extra-roots list from first start rather than only after
        a `remember` call happens to create it."""
        from athenaeum.init import init_knowledge_dir

        root = init_knowledge_dir(tmp_path / "knowledge")
        assert (root / "raw" / "remote-session").is_dir()

    def test_an_http_note_is_recallable_from_the_remote_session_root(
        self, corpus: Path
    ) -> None:
        """The athenaeum#2313 half of the criterion, end to end.

        Writes under HTTP defaults and then recalls the hit through the SAME
        server instance, with the FTS5 index built BEFORE ``raw/remote-session``
        existed — the pre-existing-install shape, where only the post-write
        extra-root refresh can make the note findable. Asserts the note's own
        body comes back, not merely a non-empty result.
        """
        _build_fts5_index(corpus)
        server = _server(
            corpus,
            transport="http",
            search_backend="fts5",
            config=_config("raw/remote-session"),
        )
        needle = "Zarquon calibration protocol"

        saved = _tool(server, "remember")(content=f"The {needle} is quarterly.")
        assert saved.startswith("Saved to")

        rendered = _tool(server, "recall")(query=needle, top_k=5)
        assert needle in rendered
        assert _manifest_entries(corpus, "remote-session")


class TestToolUseIdNoneIsInert:
    """AC3: ``active_tool_use_id()`` is ``None`` for every host that does not
    set Claude Code's ``claudecode/toolUseId`` meta key. Across many calls,
    from two sessions, nothing raises and nothing is recorded."""

    CALLS_PER_SESSION = 60

    def test_active_tool_use_id_is_none_with_no_active_request(self) -> None:
        """The premise of this whole class, asserted rather than assumed.

        An in-process call has no FastMCP request context at all, which is
        the same answer a host that sets no such key produces.
        """
        from athenaeum.mcp_server import active_tool_use_id

        assert active_tool_use_id() is None

    def test_the_resolver_touches_no_cache_across_many_calls(self) -> None:
        from athenaeum.push_metrics import (
            ATTRIBUTION_ENV_UNRESOLVED,
            ToolUseSessionResolver,
        )

        resolver = ToolUseSessionResolver(env_fallback=False)
        for _ in SESSION_LABELS:
            for _ in range(self.CALLS_PER_SESSION):
                attribution = resolver.resolve(None)
                assert attribution.session_id == ""
                assert attribution.attribution == ATTRIBUTION_ENV_UNRESOLVED

        # `resolve(None)` short-circuits BEFORE the scope-directory and
        # last-transcript lookups, so none of the three caches the
        # `_session_resolver` comment calls sound was ever populated. This is
        # what makes the resolver inert on a non-Claude host rather than
        # merely harmless.
        assert resolver._scope_dir is None
        assert resolver._last_transcript is None
        assert resolver._joined == {}

    def test_a_stdio_resolver_is_equally_silent_with_no_session_in_the_env(
        self,
    ) -> None:
        """Default ``env_fallback=True`` is not what makes it inert — an
        absent id is. Pinned so the two reasons stay distinguishable."""
        from athenaeum.push_metrics import ToolUseSessionResolver

        assert ToolUseSessionResolver().resolve(None).session_id == ""

    def test_http_recall_records_nothing_even_with_a_stale_session_in_the_env(
        self, corpus: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The defect this criterion turns up, and its fix.

        An operator plausibly starts ``athenaeum serve --transport http`` from
        inside a Claude Code session, which leaves that conversation's id in
        the server's environment for the life of the process. Every client's
        recall was then recorded under that one unrelated id. Under HTTP the
        env fallback is off, so the ledger stays absent.
        """
        from athenaeum.push_metrics import push_records_path

        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "stale-operator-session")
        _build_fts5_index(corpus)
        server = _server(
            corpus,
            transport="http",
            search_backend="fts5",
            config=_config("raw/remote-session"),
        )
        recall = _tool(server, "recall")

        for label in SESSION_LABELS:
            for _ in range(self.CALLS_PER_SESSION):
                # No raise, and a real answer: an exception swallowed into an
                # empty render would pass a "nothing recorded" assertion for
                # the wrong reason.
                assert "Widget Co" in recall(query=f"widget calibration {label}")

        ledger = push_records_path(corpus.parent / "cache")
        assert not ledger.exists(), ledger.read_text(encoding="utf-8")

    def test_stdio_recall_does_record_under_the_env_id(
        self, corpus: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Positive control for the test above.

        Same corpus, same calls, same `None` toolUseId — only the transport
        differs. Instrumentation is on by default
        (``config.resolve_push_metrics_enabled``), so if this did not write a
        row the HTTP assertion would be proving nothing.
        """
        from athenaeum.push_metrics import push_records_path

        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "stdio-session")
        _build_fts5_index(corpus)
        server = _server(
            corpus,
            search_backend="fts5",
            config=_config("raw/claude-session"),
        )
        _tool(server, "recall")(query="widget calibration")

        ledger = push_records_path(corpus.parent / "cache")
        assert ledger.is_file()
        rows = [json.loads(line) for line in ledger.read_text().splitlines() if line]
        assert [row["session_id"] for row in rows] == ["stdio-session"]


class TestConcurrentRememberAndRecall:
    """AC4: N parallel ``remember`` calls interleaved with parallel ``recall``
    calls against ONE server instance."""

    def test_the_index_refresh_is_serialized(
        self, corpus: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The mechanism, deterministically.

        ``_refresh_extra_root_index`` LOADS the FTS5 manifest, diffs the
        corpus against it and WRITES it back, so two concurrent refreshes diff
        the same pre-image and the second writer drops the first's entry.
        Measured on this corpus before the lock: 24 files on disk, 14-22 of
        them in the manifest, against 24/24 run serially.

        Pinned by observing overlap directly rather than by hoping the
        manifest race reproduces: a stand-in backend records how many builds
        are inside ``build_index`` at once, and a barrier releases every
        thread together so they all reach it at the same moment.

        The asymmetry is deliberate and is why this is not a flaky test. With
        the lock held, ``peak == 1`` is guaranteed by mutual exclusion, not by
        timing — no scheduling order can break it, which is the direction that
        decides pass/fail in CI. The brief sleep only sharpens the FAILING
        direction on a tree where the lock has been removed; the worst a
        contended runner can do is make such a tree pass, never make this one
        fail.
        """
        import time

        from athenaeum import mcp_server

        # Created so the refresh resolves a real root and the run leaves no
        # "extra_intake_root not found" warnings for a reader to wade through.
        (corpus / "raw" / "remote-session").mkdir(parents=True)

        threads_n = 8
        inside = 0
        peak = 0
        bookkeeping = threading.Lock()
        at_the_door = threading.Barrier(threads_n)

        class _CountingBackend:
            def build_index(self, *_args: object, **_kwargs: object) -> None:
                nonlocal inside, peak
                with bookkeeping:
                    inside += 1
                    peak = max(peak, inside)
                time.sleep(0.02)
                with bookkeeping:
                    inside -= 1

        monkeypatch.setattr(
            "athenaeum.search.get_backend", lambda _name: _CountingBackend()
        )

        def _refresh() -> None:
            at_the_door.wait(timeout=JOIN_TIMEOUT_S)
            mcp_server._refresh_extra_root_index(
                "fts5",
                wiki_root=corpus / "wiki",
                cache_dir=corpus.parent / "cache",
                config=_config("raw/remote-session"),
            )

        workers = [threading.Thread(target=_refresh) for _ in range(threads_n)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(JOIN_TIMEOUT_S)
            assert not worker.is_alive(), "index-refresh thread did not finish"

        assert peak == 1, f"{peak} concurrent index refreshes — the lock is not held"

    def test_every_write_lands_once_and_the_index_stays_consistent(
        self, corpus: Path
    ) -> None:
        """The durable artifact, under real contention.

        ``CONCURRENT_WRITES`` ``remember`` calls and as many ``recall`` calls,
        all against one server, from two simulated sessions. Three assertions,
        each on something that survives the run:

        1. every write landed EXACTLY once — one file per call, each carrying
           its own marker, no marker written twice and none lost;
        2. no ``recall`` raised, and none returned an empty render;
        3. the index is consistent afterwards — the FTS5 manifest names
           every one of the notes, and every note is retrievable by
           ``recall``.

        The MANIFEST is the load-bearing half of (3), not the retrieval.
        Each refresh re-diffs the whole root, so a LATER `remember`'s refresh
        picks up whatever an earlier racing one dropped: retrieval after all
        N writes have landed is self-healing and passes even on a tree with
        no lock. What does not heal is the state a client is left in when the
        dropped write is one of the last — the note stays invisible to
        `recall` until some future write or compile, which is exactly the
        athenaeum#2313 same-session guarantee broken. That window is only
        observable in the index contents, so that is what is asserted.
        """
        # Pre-created, and in the starting index: this test is about what
        # concurrency does to a server already serving the root, not about
        # the first-write-creates-it case `TestSessionSubdirPerTransport`
        # covers. Starting from a manifest that already knows the root is
        # also the shape the race was measured in.
        remote_root = corpus / "raw" / "remote-session"
        remote_root.mkdir(parents=True)
        _build_fts5_index(corpus, remote_root)
        server = _server(
            corpus,
            transport="http",
            search_backend="fts5",
            extra_roots=[remote_root],
            config=_config("raw/remote-session"),
        )
        remember = _tool(server, "remember")
        recall = _tool(server, "recall")

        failures: list[str] = []
        saved: list[str] = []
        recalls: list[str] = []
        guard = threading.Lock()
        # Every worker waits here and is released together. Without it,
        # `Thread.start()` returning for worker 0 before worker 23 is even
        # constructed means the calls can finish nearly serially on a busy
        # runner — "parallel" in shape only, which is not what the criterion
        # asks for and not what would have caught the manifest race.
        at_the_door = threading.Barrier(CONCURRENT_WRITES * 2)

        def _marker(index: int) -> str:
            # One token per note, unique and FTS5-friendly, carrying the
            # session label so a cross-session mixup is visible on disk.
            return f"Zarquonmarker{index}{SESSION_LABELS[index % 2]}"

        def _do_remember(index: int) -> None:
            try:
                at_the_door.wait(timeout=JOIN_TIMEOUT_S)
                result = remember(content=f"{_marker(index)} concerns calibration.")
                with guard:
                    saved.append(result)
            except Exception as exc:  # noqa: BLE001 — reported, not swallowed
                with guard:
                    failures.append(f"remember {index}: {exc!r}")

        def _do_recall(index: int) -> None:
            try:
                at_the_door.wait(timeout=JOIN_TIMEOUT_S)
                result = recall(query="widget calibration", top_k=5)
                with guard:
                    recalls.append(result)
            except Exception as exc:  # noqa: BLE001 — reported, not swallowed
                with guard:
                    failures.append(f"recall {index}: {exc!r}")

        workers: list[threading.Thread] = []
        for index in range(CONCURRENT_WRITES):
            workers.append(threading.Thread(target=_do_remember, args=(index,)))
            workers.append(threading.Thread(target=_do_recall, args=(index,)))
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(JOIN_TIMEOUT_S)
            assert not worker.is_alive(), "a concurrent tool call did not finish"

        assert failures == []

        # 1. Exactly once.
        files = _written(corpus, "remote-session")
        assert len(files) == CONCURRENT_WRITES
        assert len(saved) == CONCURRENT_WRITES
        assert all(result.startswith("Saved to") for result in saved)
        bodies = [path.read_text(encoding="utf-8") for path in files]
        found = sorted(
            int(match.group(1))
            for body in bodies
            for match in [re.search(r"Zarquonmarker(\d+)", body)]
            if match
        )
        assert found == list(range(CONCURRENT_WRITES))

        # 2. Every recall answered.
        assert len(recalls) == CONCURRENT_WRITES
        assert all("Widget Co" in rendered for rendered in recalls)

        # 3a. Consistent index: the manifest names every note.
        indexed = _manifest_entries(corpus, "remote-session")
        assert len(indexed) == CONCURRENT_WRITES, (
            f"{len(indexed)} of {CONCURRENT_WRITES} notes reached the FTS5 "
            f"manifest; concurrent refreshes dropped the rest"
        )

        # 3b. And every note is retrievable, not just on disk.
        unfindable = [
            index
            for index in range(CONCURRENT_WRITES)
            if _marker(index) not in recall(query=_marker(index), top_k=10)
        ]
        assert unfindable == [], (
            f"notes written but missing from the index: {unfindable}"
        )


class TestSessionResolverCommentStatesTheHttpCase:
    """AC5: the comment above ``_session_resolver``.

    A source-text assertion, deliberately: the criterion IS about what the
    comment says, and the defect it records is a comment that justified the
    resolver's caches from a premise ("a stdio MCP server is spawned once per
    conversation") that ``--transport http`` falsifies. Scoped to the comment
    block immediately above the construction so ordinary editing elsewhere in
    ``create_server`` cannot fail it.
    """

    def _comment_block(self) -> str:
        from athenaeum.mcp_server import create_server

        source = inspect.getsource(create_server)
        head, sep, _ = source.partition("_session_resolver = ")
        assert sep, "could not locate the `_session_resolver` construction"
        lines: list[str] = []
        for line in reversed(head.splitlines()):
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                lines.append(stripped)
                continue
            if stripped.startswith("from athenaeum import push_metrics"):
                continue
            if stripped.startswith("_transport_defaults = "):
                continue
            break
        return "\n".join(reversed(lines))

    def test_the_comment_names_the_http_case(self) -> None:
        block = self._comment_block()
        assert "HTTP" in block
        assert "many conversations" in block

    def test_the_comment_does_not_rest_on_the_stdio_premise_alone(self) -> None:
        """The stdio sentence may stay — it is true of stdio. What may not
        stay is it standing as the whole justification, so the block must say
        what makes the caches sound when it does NOT hold."""
        block = self._comment_block()
        assert "spawned once per conversation" in block, (
            "the stdio half is still the right explanation for stdio"
        )
        assert "premise is false" in block


class TestServedSurfaceIsTransportInvariant:
    """The issue's "branch only the defaults, never the tool schema"
    constraint. ``transport`` is a ``create_server`` argument, so there is
    nothing in it a client could read a host identity out of — and the two
    transports must serve the same ``tools/list``."""

    def _served(self, server) -> str:
        async def _run() -> str:
            tools = sorted(await server.list_tools(), key=lambda tool: tool.name)
            return json.dumps(
                [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    }
                    for tool in tools
                ],
                indent=2,
                sort_keys=True,
                default=str,
            )

        return asyncio.run(_run())

    def test_both_transports_serve_the_same_tools_list(self, corpus: Path) -> None:
        stdio = self._served(_server(corpus))
        assert "recall" in stdio, "sanity: the server registered no tools"
        assert stdio == self._served(_server(corpus, transport="http"))

    def test_omitting_transport_equals_passing_stdio(self, corpus: Path) -> None:
        from athenaeum.mcp_server import (
            STDIO_TRANSPORT_DEFAULTS,
            transport_defaults,
        )

        assert transport_defaults(None) is STDIO_TRANSPORT_DEFAULTS
        assert transport_defaults("stdio") is STDIO_TRANSPORT_DEFAULTS

    def test_an_unknown_transport_falls_back_to_the_stdio_defaults(self) -> None:
        """Conservative by construction: a transport nobody taught
        ``transport_defaults`` about must not silently inherit "assume
        multi-client" and change the `remember` write path sight unseen."""
        from athenaeum.mcp_server import STDIO_TRANSPORT_DEFAULTS, transport_defaults

        unknown = transport_defaults("websocket-of-the-future")
        assert unknown is STDIO_TRANSPORT_DEFAULTS

    def test_no_tool_exposes_a_transport_shaped_parameter(self, corpus: Path) -> None:
        async def _properties(server) -> set[str]:
            names: set[str] = set()
            for tool in await server.list_tools():
                names |= set((tool.parameters or {}).get("properties") or {})
            return names

        served = asyncio.run(_properties(_server(corpus, transport="http")))
        assert served
        assert not served & {
            "transport",
            "host_label",
            "inferred_source",
            "session_subdir",
            "env_session_attribution",
        }
