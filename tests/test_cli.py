"""Tests for the athenaeum CLI command dispatch."""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.cli import main


@pytest.fixture
def knowledge_with_wiki(tmp_path: Path) -> Path:
    knowledge = tmp_path / "knowledge"
    wiki = knowledge / "wiki"
    wiki.mkdir(parents=True)

    (wiki / "lean-startup.md").write_text(
        "---\n"
        "name: Lean Startup\n"
        "tags: [methodology]\n"
        "description: Build-measure-learn methodology\n"
        "---\n\n"
        "The Lean Startup methodology.\n"
    )
    (wiki / "customer-development.md").write_text(
        "---\n"
        "name: Customer Development\n"
        "tags: [methodology]\n"
        "description: Steve Blank's framework\n"
        "---\n\n"
        "Customer development is a four-step framework.\n"
    )
    return knowledge


class TestRebuildIndex:
    def test_builds_fts5_index(
        self,
        knowledge_with_wiki: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        cache = tmp_path / "cache"
        rc = main(
            [
                "rebuild-index",
                "--path",
                str(knowledge_with_wiki),
                "--cache-dir",
                str(cache),
                "--backend",
                "fts5",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "FTS5 index rebuilt: 2 pages" in out
        assert (cache / "wiki-index.db").exists()

    def test_reads_backend_from_config(
        self,
        knowledge_with_wiki: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (knowledge_with_wiki / "athenaeum.yaml").write_text(
            "auto_recall: true\nsearch_backend: fts5\n"
        )
        cache = tmp_path / "cache"
        rc = main(
            [
                "rebuild-index",
                "--path",
                str(knowledge_with_wiki),
                "--cache-dir",
                str(cache),
            ]
        )
        assert rc == 0
        assert "FTS5 index rebuilt" in capsys.readouterr().out

    def test_defaults_to_fts5_when_no_config(
        self,
        knowledge_with_wiki: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        cache = tmp_path / "cache"
        rc = main(
            [
                "rebuild-index",
                "--path",
                str(knowledge_with_wiki),
                "--cache-dir",
                str(cache),
            ]
        )
        assert rc == 0
        assert "FTS5 index rebuilt" in capsys.readouterr().out

    def test_missing_wiki_returns_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        nonexistent = tmp_path / "does-not-exist"
        rc = main(
            [
                "rebuild-index",
                "--path",
                str(nonexistent),
                "--cache-dir",
                str(tmp_path / "cache"),
                "--backend",
                "fts5",
            ]
        )
        assert rc == 1
        assert "Wiki directory not found" in capsys.readouterr().err

    def test_unknown_backend_returns_error(
        self,
        knowledge_with_wiki: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (knowledge_with_wiki / "athenaeum.yaml").write_text(
            "auto_recall: true\nsearch_backend: nonsense\n"
        )
        rc = main(
            [
                "rebuild-index",
                "--path",
                str(knowledge_with_wiki),
                "--cache-dir",
                str(tmp_path / "cache"),
            ]
        )
        assert rc == 1
        assert "Unknown search backend" in capsys.readouterr().err


class TestServe:
    """`athenaeum serve` must forward the configured search_backend + cache_dir
    to create_server so the MCP `recall` tool uses the vector index when the
    user has configured `search_backend: vector`. Regression guard against the
    bug where serve hard-coded defaults (keyword) and silently ignored
    athenaeum.yaml."""

    def test_serve_reads_vector_backend_from_config(
        self, knowledge_with_wiki: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (knowledge_with_wiki / "athenaeum.yaml").write_text(
            "auto_recall: true\nsearch_backend: vector\n"
        )
        captured: dict[str, object] = {}

        class _FakeServer:
            def run(self) -> None:
                raise KeyboardInterrupt

        def _fake_create_server(**kwargs: object) -> _FakeServer:
            captured.update(kwargs)
            return _FakeServer()

        import athenaeum.mcp_server as mcp_mod

        monkeypatch.setattr(mcp_mod, "create_server", _fake_create_server)

        rc = main(["serve", "--path", str(knowledge_with_wiki)])
        assert rc == 0
        assert captured["search_backend"] == "vector"
        assert captured["wiki_root"] == knowledge_with_wiki / "wiki"
        assert captured["raw_root"] == knowledge_with_wiki / "raw"
        assert captured["cache_dir"] is not None

    def test_serve_defaults_to_fts5_from_config_defaults(
        self, knowledge_with_wiki: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        class _FakeServer:
            def run(self) -> None:
                raise KeyboardInterrupt

        def _fake_create_server(**kwargs: object) -> _FakeServer:
            captured.update(kwargs)
            return _FakeServer()

        import athenaeum.mcp_server as mcp_mod

        monkeypatch.setattr(mcp_mod, "create_server", _fake_create_server)

        rc = main(["serve", "--path", str(knowledge_with_wiki)])
        assert rc == 0
        assert captured["search_backend"] == "fts5"

    def test_serve_missing_path_renders_path_in_message(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Regression guard for v0.2.0 bug where the 'run athenaeum init'
        hint printed the literal placeholder `{args.path}` because the
        f-string prefix was missing. First-adopter-facing error message —
        if this line drifts back to a non-f-string, pre-init users see
        the unrendered template and lose trust."""
        missing = tmp_path / "no-such-knowledge"
        rc = main(["serve", "--path", str(missing)])
        out = capsys.readouterr().out
        assert rc == 1
        assert str(missing) in out
        assert "{args.path}" not in out


class TestWarnIfBackendCacheMissing:
    """All four branches of ``_warn_if_backend_cache_missing``.

    First-adopter pain point from the v0.2.0 review: ``search_backend:
    vector`` in ``athenaeum.yaml`` but only an fts5 cache on disk (common
    when a user flips backends but forgets to rebuild). The MCP recall
    tool silently returns zero hits. The warning on ``athenaeum serve``
    startup is the only early signal — if any of these branches stops
    emitting, the silent-zero-hits UX regresses. Capture via ``capsys``
    because the warning goes to stderr.
    """

    def test_keyword_backend_no_op(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from athenaeum.cli import _warn_if_backend_cache_missing

        _warn_if_backend_cache_missing("keyword", tmp_path)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert (
            captured.err == ""
        ), "keyword backend has no on-disk cache — warning would be noise"

    def test_fts5_missing_cache_warns(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from athenaeum.cli import _warn_if_backend_cache_missing

        _warn_if_backend_cache_missing("fts5", tmp_path)
        err = capsys.readouterr().err
        assert "search_backend=fts5" in err
        assert "rebuild-index" in err

    def test_fts5_present_cache_silent(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from athenaeum.cli import _warn_if_backend_cache_missing

        (tmp_path / "wiki-index.db").write_bytes(b"")
        _warn_if_backend_cache_missing("fts5", tmp_path)
        assert capsys.readouterr().err == ""

    def test_vector_missing_cache_warns(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from athenaeum.cli import _warn_if_backend_cache_missing

        _warn_if_backend_cache_missing("vector", tmp_path)
        err = capsys.readouterr().err
        assert "search_backend=vector" in err
        assert "rebuild-index" in err

    def test_vector_present_cache_silent(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from athenaeum.cli import _warn_if_backend_cache_missing

        (tmp_path / "wiki-vectors").mkdir()
        _warn_if_backend_cache_missing("vector", tmp_path)
        assert capsys.readouterr().err == ""

    def test_unknown_backend_warns(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from athenaeum.cli import _warn_if_backend_cache_missing

        _warn_if_backend_cache_missing("sphinx", tmp_path)
        err = capsys.readouterr().err
        assert "unknown search_backend" in err
        assert "'sphinx'" in err


class TestStopwords:
    """`athenaeum stopwords` is the canonical source of the stopword list —
    shell hooks read it instead of hard-coding their own copy. Regression
    guard for issue #46: the two copies must stay in sync."""

    def test_prints_one_word_per_line_sorted(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["stopwords"])
        assert rc == 0
        out = capsys.readouterr().out
        lines = out.strip().splitlines()
        assert lines == sorted(lines), "Output must be sorted for determinism"
        assert len(lines) > 50, "Stopword list should be non-trivial"
        assert "the" in lines
        assert "thanks" in lines

    def test_matches_search_module_constant(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from athenaeum.search import STOPWORDS

        rc = main(["stopwords"])
        assert rc == 0
        out = capsys.readouterr().out
        assert out.strip().splitlines() == list(STOPWORDS), (
            "CLI output must match search.STOPWORDS exactly — divergence "
            "silently degrades the shell-hook fallback extractor."
        )


class TestTestMcp:
    def test_all_steps_pass_with_fastmcp_available(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pytest.importorskip("fastmcp")
        rc = main(["test-mcp"])
        captured = capsys.readouterr()
        assert rc == 0, f"stdout: {captured.out}\nstderr: {captured.err}"
        assert "PASS  remember_write" in captured.out
        assert "PASS  recall_search (keyword)" in captured.out
        assert "PASS  create_server (FastMCP)" in captured.out
        assert "3 passed, 0 failed" in captured.out

    def test_smoke_remember_declares_provenance(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The smoke test passes ``sources``, so it must not trip the
        issue-#90 "no `sources` supplied" warning the server logs for
        provenance-less writes (v0.7.3 release-gate review)."""
        import logging

        pytest.importorskip("fastmcp")
        with caplog.at_level(logging.WARNING, logger="athenaeum.mcp_server"):
            rc = main(["test-mcp"])
        assert rc == 0
        assert not [
            r for r in caplog.records if "no `sources` supplied" in r.getMessage()
        ]

    def test_keep_flag_preserves_temp_dir(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pytest.importorskip("fastmcp")
        rc = main(["test-mcp", "--keep"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "Temp dir preserved at:" in captured.out

        marker = "Temp dir preserved at: "
        line = next(
            line for line in captured.out.splitlines() if line.startswith(marker)
        )
        kept_dir = Path(line[len(marker) :].strip())
        try:
            assert kept_dir.is_dir()
            assert (kept_dir / "wiki" / "test-page.md").is_file()
            assert list((kept_dir / "raw" / "test-mcp").glob("*.md"))
        finally:
            import shutil

            shutil.rmtree(kept_dir, ignore_errors=True)

    def test_reports_fastmcp_missing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import athenaeum.mcp_server as mcp_mod

        def _raise(*_a: object, **_k: object) -> None:
            raise ImportError("no module named 'fastmcp'")

        monkeypatch.setattr(mcp_mod, "create_server", _raise)
        rc = main(["test-mcp"])
        captured = capsys.readouterr()
        assert rc == 1
        assert "FAIL  create_server" in captured.err
        assert "pip install athenaeum[mcp]" in captured.err
        assert "2 passed, 1 failed" in captured.out


class TestRecall:
    """`athenaeum recall <query>` subcommand (issue #71). Shell-accessible
    wrapper around the MCP recall tool — used by validation harnesses and
    operator debugging. Output contract: one tab-separated hit per line,
    ``<score>\\t<filename>\\t<preview>``."""

    def test_keyword_backend_prints_tab_separated_hits(
        self,
        knowledge_with_wiki: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = main(
            [
                "recall",
                "lean startup",
                "--path",
                str(knowledge_with_wiki),
                "--cache-dir",
                str(tmp_path / "cache"),
                "--backend",
                "keyword",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        lines = [line for line in out.splitlines() if line.strip()]
        assert lines, "expected at least one hit"
        # First hit should be the lean-startup page.
        first = lines[0].split("\t")
        assert len(first) == 3, f"expected 3 tab-separated fields, got: {first!r}"
        score_str, filename, preview = first
        float(score_str)  # parseable as float
        assert filename == "lean-startup.md"
        assert "Lean Startup" in preview

    def test_top_k_limits_output(
        self,
        knowledge_with_wiki: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = main(
            [
                "recall",
                "methodology framework",
                "--path",
                str(knowledge_with_wiki),
                "--cache-dir",
                str(tmp_path / "cache"),
                "--backend",
                "keyword",
                "--top-k",
                "1",
            ]
        )
        assert rc == 0
        lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
        assert len(lines) <= 1

    def test_missing_wiki_returns_error(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = main(
            [
                "recall",
                "anything",
                "--path",
                str(tmp_path / "nope"),
                "--cache-dir",
                str(tmp_path / "cache"),
                "--backend",
                "keyword",
            ]
        )
        assert rc == 1
        assert "Wiki directory not found" in capsys.readouterr().err

    def test_fts5_backend_uses_prebuilt_index(
        self,
        knowledge_with_wiki: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Recall with fts5 must use the on-disk cache the user built with
        ``athenaeum rebuild-index``. Regression guard: if the handler
        silently falls back to keyword when the fts5 index is missing,
        validation harnesses reading `athenaeum.yaml: vector` would see
        wrong results."""
        cache = tmp_path / "cache"
        rc = main(
            [
                "rebuild-index",
                "--path",
                str(knowledge_with_wiki),
                "--cache-dir",
                str(cache),
                "--backend",
                "fts5",
            ]
        )
        assert rc == 0
        capsys.readouterr()  # drain rebuild-index output

        rc = main(
            [
                "recall",
                "lean",
                "--path",
                str(knowledge_with_wiki),
                "--cache-dir",
                str(cache),
                "--backend",
                "fts5",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        lines = [line for line in out.splitlines() if line.strip()]
        assert any("lean-startup.md" in line for line in lines)


class TestIngestAnswers:
    """`athenaeum ingest-answers` subcommand (issue #61)."""

    def test_missing_knowledge_dir_returns_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["ingest-answers", "--path", str(tmp_path / "nope")])
        assert rc == 1
        assert "Knowledge directory not found" in capsys.readouterr().err

    def test_noop_on_empty_pending_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "wiki").mkdir()
        (tmp_path / "raw").mkdir()
        rc = main(["ingest-answers", "--path", str(tmp_path)])
        assert rc == 0
        assert "Ingested 0" in capsys.readouterr().out

    def test_ingests_answered_block(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wiki = tmp_path / "wiki"
        raw = tmp_path / "raw"
        wiki.mkdir()
        raw.mkdir()
        (wiki / "_pending_questions.md").write_text(
            "# Pending Questions\n\n"
            '## [2026-04-20] Entity: "Acme Corp" (from sessions/test.md)\n'
            "- [x] Question about Acme?\n"
            "**Conflict type**: principled\n"
            "**Description**: Conflicting Series info.\n"
            "\n"
            "Series B, closed March 2026.\n"
        )
        rc = main(["ingest-answers", "--path", str(tmp_path)])
        assert rc == 0
        assert "Ingested 1" in capsys.readouterr().out
        assert list((raw / "answers").glob("*.md"))
        assert (wiki / "_pending_questions_archive.md").exists()


class TestIngestMerges:
    """`athenaeum ingest-merges` subcommand (issue #299).

    ``ingest_resolved_merges`` existed in ``pending_merges.py`` with zero
    callers and zero test coverage — nothing ever archived a resolved merge
    block, which is why ``_pending_merges.md`` grew unbounded in production
    (5MB/67K lines, 15 of 40 blocks already decided). This wires it up.
    """

    def test_missing_knowledge_dir_returns_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["ingest-merges", "--path", str(tmp_path / "nope")])
        assert rc == 1
        assert "Knowledge directory not found" in capsys.readouterr().err

    def test_noop_on_empty_pending_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "wiki").mkdir()
        rc = main(["ingest-merges", "--path", str(tmp_path)])
        assert rc == 0
        assert "Archived 0" in capsys.readouterr().out

    def test_archives_resolved_block(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "_pending_merges.md").write_text(
            "# Pending Merges\n\n"
            '## [2026-05-29] Merge: "acme-corp"\n'
            "- [x] Approve this merge? Sources: a.md, b.md\n\n"
            "**Rationale**: dup pages\n"
            "**Sources**:\n- a.md\n- b.md\n"
            "**Confidence**: 0.90\n"
            "**Draft**:\n```markdown\nMerged body.\n```\n\n"
            "**Decision**: approve\n"
        )
        rc = main(["ingest-merges", "--path", str(tmp_path)])
        assert rc == 0
        assert "Archived 1" in capsys.readouterr().out
        archive = wiki / "_pending_merges_archive.md"
        assert archive.exists()
        assert "acme-corp" in archive.read_text(encoding="utf-8")
        # Resolved block no longer sits in the live/primary file.
        assert "acme-corp" not in (wiki / "_pending_merges.md").read_text(
            encoding="utf-8"
        )

    def test_leaves_unresolved_block_in_place(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "_pending_merges.md").write_text(
            "# Pending Merges\n\n"
            '## [2026-06-01] Merge: "still-open"\n'
            "- [ ] Approve this merge? Sources: c.md, d.md\n\n"
            "**Rationale**: dup pages\n"
            "**Sources**:\n- c.md\n- d.md\n"
            "**Confidence**: 0.80\n"
            "**Draft**:\n```markdown\nDraft body.\n```\n"
        )
        rc = main(["ingest-merges", "--path", str(tmp_path)])
        assert rc == 0
        assert "Archived 0" in capsys.readouterr().out
        assert "still-open" in (wiki / "_pending_merges.md").read_text(encoding="utf-8")
        assert not (wiki / "_pending_merges_archive.md").exists()


class TestPeopleCommand:
    """Frontmatter-only person filter — deterministic, no LLM, no embeddings."""

    @staticmethod
    def _seed_wiki(wiki: Path) -> None:
        """Three Pearl employees, one Datadog person, one tagless ghost."""
        wiki.mkdir(parents=True, exist_ok=True)
        (wiki / "01-lisa.md").write_text(
            "---\nuid: 01\ntype: person\nname: Lisa Contoyannis\n"
            "tags: [tier:warm-a, role:exec]\ncurrent_company: Pearl\n"
            "current_title: EVP Product\nwarm_score: 286.0\n"
            "meeting_count_24mo: 60\nsent_count_24mo: 100\n"
            "last_touch: '2026-05-29'\n---\n\n# Lisa\n"
        )
        (wiki / "02-andy.md").write_text(
            "---\nuid: 02\ntype: person\nname: Andy Kurtzig\n"
            "tags: [tier:warm-a, role:founder]\ncurrent_company: Pearl.com\n"
            "current_title: CEO\nwarm_score: 150.5\n"
            "meeting_count_24mo: 50\nsent_count_24mo: 65\n"
            "last_touch: '2025-08-26'\n---\n\n# Andy\n"
        )
        (wiki / "03-michael.md").write_text(
            "---\nuid: 03\ntype: person\nname: Michael Gutkowski\n"
            "tags: [tier:warm-b, role:exec]\ncurrent_company: Pearl.com\n"
            "current_title: EVP BD\nwarm_score: 45.2\n"
            "meeting_count_24mo: 30\nsent_count_24mo: 23\n"
            "last_touch: '2025-02-27'\n---\n\n# Michael\n"
        )
        (wiki / "04-datadog.md").write_text(
            "---\nuid: 04\ntype: person\nname: Olivier Pomel\n"
            "tags: [tier:extended]\ncurrent_company: Datadog\n"
            "current_title: CEO\n---\n\n# Olivier\n"
        )
        (wiki / "05-ghost.md").write_text(
            "---\nuid: 05\ntype: person\nname: No-Tag Ghost\n" "---\n\n# Ghost\n"
        )
        (wiki / "06-company.md").write_text(
            "---\nuid: 06\ntype: company\nname: Pearl.com Holdings\n" "---\n\n# Pearl\n"
        )

    def test_company_filter_returns_pearl_employees(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        knowledge = tmp_path / "k"
        self._seed_wiki(knowledge / "wiki")
        rc = main(
            [
                "people",
                "--path",
                str(knowledge),
                "--company",
                "Pearl",
                "--format",
                "tsv",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        names = [line.split("\t", 1)[0] for line in out.strip().splitlines()]
        assert names[:3] == ["Lisa Contoyannis", "Andy Kurtzig", "Michael Gutkowski"]
        assert "Olivier Pomel" not in names

    def test_tier_shorthand_filters_to_warm_a(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        knowledge = tmp_path / "k"
        self._seed_wiki(knowledge / "wiki")
        rc = main(
            ["people", "--path", str(knowledge), "--tier", "warm-a", "--format", "tsv"]
        )
        assert rc == 0
        names = [
            line.split("\t", 1)[0]
            for line in capsys.readouterr().out.strip().splitlines()
        ]
        assert set(names) == {"Lisa Contoyannis", "Andy Kurtzig"}
        assert "Michael Gutkowski" not in names

    def test_top_touch_sorts_by_meeting_plus_email(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        knowledge = tmp_path / "k"
        self._seed_wiki(knowledge / "wiki")
        rc = main(
            [
                "people",
                "--path",
                str(knowledge),
                "--company",
                "Pearl",
                "--top-touch",
                "2",
                "--format",
                "tsv",
            ]
        )
        assert rc == 0
        names = [
            line.split("\t", 1)[0]
            for line in capsys.readouterr().out.strip().splitlines()
        ]
        assert names == ["Lisa Contoyannis", "Andy Kurtzig"]

    def test_company_excludes_non_person_types(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        knowledge = tmp_path / "k"
        self._seed_wiki(knowledge / "wiki")
        rc = main(
            [
                "people",
                "--path",
                str(knowledge),
                "--company",
                "Pearl",
                "--format",
                "tsv",
            ]
        )
        assert rc == 0
        names = [
            line.split("\t", 1)[0]
            for line in capsys.readouterr().out.strip().splitlines()
        ]
        assert "Pearl.com Holdings" not in names

    def test_missing_wiki_returns_error(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = main(["people", "--path", str(tmp_path / "nope")])
        assert rc == 1
        assert "Wiki root not found" in capsys.readouterr().err

    def test_title_regex_matches_role_pattern(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        knowledge = tmp_path / "k"
        self._seed_wiki(knowledge / "wiki")
        rc = main(
            [
                "people",
                "--path",
                str(knowledge),
                "--title-regex",
                r"CEO|EVP",
                "--format",
                "tsv",
            ]
        )
        assert rc == 0
        names = [
            line.split("\t", 1)[0]
            for line in capsys.readouterr().out.strip().splitlines()
        ]
        assert set(names) == {
            "Lisa Contoyannis",
            "Andy Kurtzig",
            "Michael Gutkowski",
            "Olivier Pomel",
        }
        assert "No-Tag Ghost" not in names
        assert "Pearl.com Holdings" not in names

    def test_company_regex_intersects_with_title_regex(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Find CEOs at Pearl-family companies (excluding the Datadog CEO)."""
        knowledge = tmp_path / "k"
        self._seed_wiki(knowledge / "wiki")
        rc = main(
            [
                "people",
                "--path",
                str(knowledge),
                "--title-regex",
                r"CEO",
                "--company-regex",
                r"Pearl",
                "--format",
                "tsv",
            ]
        )
        assert rc == 0
        names = [
            line.split("\t", 1)[0]
            for line in capsys.readouterr().out.strip().splitlines()
        ]
        assert names == ["Andy Kurtzig"]
        assert "Olivier Pomel" not in names


class TestRunMaxApiCallsFlag:
    """Issue #220 fix round — CLI coverage for --max-api-calls."""

    def test_default_passes_none_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`athenaeum run` without --max-api-calls hands None to librarian.run,
        leaving env/yaml/default precedence to the library."""
        import athenaeum.librarian as librarian_mod

        captured: dict[str, object] = {}

        def fake_run(**kwargs):
            captured.update(kwargs)
            return 0

        monkeypatch.setattr(librarian_mod, "run", fake_run)
        rc = main(["run", "--knowledge-root", str(tmp_path), "--dry-run"])
        assert rc == 0
        assert "max_api_calls" in captured
        assert captured["max_api_calls"] is None

    def test_explicit_flag_passes_value_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import athenaeum.librarian as librarian_mod

        captured: dict[str, object] = {}

        def fake_run(**kwargs):
            captured.update(kwargs)
            return 0

        monkeypatch.setattr(librarian_mod, "run", fake_run)
        rc = main(
            [
                "run",
                "--knowledge-root",
                str(tmp_path),
                "--dry-run",
                "--max-api-calls",
                "42",
            ]
        )
        assert rc == 0
        assert captured["max_api_calls"] == 42

    @pytest.mark.parametrize("bad", ["0", "-3", "banana"])
    def test_rejects_zero_negative_and_garbage(
        self, bad: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Zero/negative/non-numeric --max-api-calls is an argparse error (exit 2)."""
        with pytest.raises(SystemExit) as excinfo:
            main(["run", "--max-api-calls", bad])
        assert excinfo.value.code == 2
        assert "--max-api-calls" in capsys.readouterr().err


def _capture_librarian_run(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Replace librarian.run with a kwargs-capturing stub returning 0."""
    import athenaeum.librarian as librarian_mod

    captured: dict[str, object] = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(librarian_mod, "run", fake_run)
    return captured


class TestRunPathAlias:
    """Issue #227 — `run --path` aliases `--knowledge-root`."""

    def test_path_alias_sets_knowledge_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = _capture_librarian_run(monkeypatch)
        rc = main(["run", "--path", str(tmp_path), "--dry-run"])
        assert rc == 0
        assert captured["knowledge_root"] == tmp_path

    def test_knowledge_root_still_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = _capture_librarian_run(monkeypatch)
        rc = main(["run", "--knowledge-root", str(tmp_path), "--dry-run"])
        assert rc == 0
        assert captured["knowledge_root"] == tmp_path

    def test_help_mentions_both_spellings(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main(["run", "--help"])
        assert excinfo.value.code == 0
        out = capsys.readouterr().out
        assert "--knowledge-root" in out
        assert "--path" in out


class TestRunStrictBudgetFlag:
    """Issue #227 — `run --strict-budget` plumbs through to librarian.run."""

    def test_default_is_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = _capture_librarian_run(monkeypatch)
        rc = main(["run", "--knowledge-root", str(tmp_path), "--dry-run"])
        assert rc == 0
        assert captured["strict_budget"] is False

    def test_flag_passes_true(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = _capture_librarian_run(monkeypatch)
        rc = main(
            ["run", "--knowledge-root", str(tmp_path), "--dry-run", "--strict-budget"]
        )
        assert rc == 0
        assert captured["strict_budget"] is True


def _claims_knowledge(tmp_path: Path) -> Path:
    """Knowledge dir with the SAME claim restated across two distinct entities."""
    knowledge = tmp_path / "knowledge"
    wiki = knowledge / "wiki"
    wiki.mkdir(parents=True)
    (wiki / "aaaa1111-profile.md").write_text(
        "---\nuid: aaaa1111\ntype: auto-memory\nname: profile\n"
        "sources:\n  - session: s1\n"
        '    claim: "Kromatic is Tristan\'s primary venture"\n'
        "---\nBody.\n"
    )
    (wiki / "bbbb2222-career.md").write_text(
        "---\nuid: bbbb2222\ntype: auto-memory\nname: career\n"
        "sources:\n  - session: s2\n"
        '    claim: "Tristan\'s primary venture is Kromatic"\n'
        "---\nBody.\n"
    )
    return knowledge


def _deterministic_embed(texts: list[str]) -> list[list[float]]:
    """Offline embedder: the two venture restatements share a vector."""
    venture = {
        "Kromatic is Tristan's primary venture",
        "Tristan's primary venture is Kromatic",
    }
    out: list[list[float]] = []
    for i, t in enumerate(texts):
        out.append([1.0, 0.0] if t.strip() in venture else [0.0, float(i + 1)])
    return out


class TestClaims:
    """`athenaeum claims --find` — cross-entity recurring-claim detector
    (issue #272, slice 1 of #258). READ-ONLY YAML report over the wiki."""

    def test_find_groups_cross_entity_restatement(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import yaml

        # Inject a deterministic offline embedder so the run never touches
        # chromadb / MiniLM (C7). The handler imports embed_texts at call
        # time, so patching the module attribute is sufficient.
        monkeypatch.setattr("athenaeum.search.embed_texts", _deterministic_embed)
        knowledge = _claims_knowledge(tmp_path)

        rc = main(["claims", "--find", "--path", str(knowledge)])
        assert rc == 0
        parsed = yaml.safe_load(capsys.readouterr().out)
        assert parsed["summary"]["threshold"] == 0.85
        assert parsed["summary"]["recurring_claim_count"] == 1
        assert parsed["summary"]["entities_scanned"] == 2
        assert parsed["recurring_claims"][0]["entity_count"] == 2

    def test_threshold_override_is_plumbed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import yaml

        monkeypatch.setattr("athenaeum.search.embed_texts", _deterministic_embed)
        knowledge = _claims_knowledge(tmp_path)

        rc = main(
            ["claims", "--find", "--path", str(knowledge), "--threshold", "0.99"]
        )
        assert rc == 0
        parsed = yaml.safe_load(capsys.readouterr().out)
        # Override reaches the report summary ...
        assert parsed["summary"]["threshold"] == 0.99
        # ... and is the active cutoff: the identical-vector pair (cosine 1.0)
        # still clears 0.99, so the group survives.
        assert parsed["summary"]["recurring_claim_count"] == 1

    def test_missing_wiki_returns_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["claims", "--find", "--path", str(tmp_path / "nope")])
        assert rc == 1
        assert "Wiki root not found" in capsys.readouterr().err

    def test_without_find_prints_usage(
        self, knowledge_with_wiki: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["claims", "--path", str(knowledge_with_wiki)])
        assert rc == 2
        assert "usage: athenaeum claims --find" in capsys.readouterr().err


class TestRunPushFlag:
    """Issue #284: ``athenaeum run --push`` must propagate to ``librarian.run``
    as ``push_after_run=True``, and absence of the flag must leave the value
    at ``None`` (which the resolver then defaults to off)."""

    def test_push_flag_sets_push_after_run_true(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum import librarian

        captured: dict[str, object] = {}

        def fake_run(**kwargs: object) -> int:
            captured.update(kwargs)
            return 0

        monkeypatch.setattr(librarian, "run", fake_run)
        knowledge = tmp_path / "knowledge"
        (knowledge / "wiki").mkdir(parents=True)
        rc = main(["run", "--path", str(knowledge), "--push", "--dry-run"])
        assert rc == 0
        assert captured.get("push_after_run") is True

    def test_no_push_flag_passes_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum import librarian

        captured: dict[str, object] = {}

        def fake_run(**kwargs: object) -> int:
            captured.update(kwargs)
            return 0

        monkeypatch.setattr(librarian, "run", fake_run)
        knowledge = tmp_path / "knowledge"
        (knowledge / "wiki").mkdir(parents=True)
        rc = main(["run", "--path", str(knowledge), "--dry-run"])
        assert rc == 0
        # None lets the resolver default to off — explicit False would
        # collapse a future "yaml-on, no flag" precedence.
        assert captured.get("push_after_run") is None
