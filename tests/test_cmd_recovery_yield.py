# SPDX-License-Identifier: Apache-2.0
"""Tests for ``athenaeum recovery-yield`` (issue athenaeum#1453).

Covers AC3 (a read-only, bounded, side-effect-free JSON readout, exit 0
regardless of verdict) and the AC4 corpus cross-check (the ``type:
auto-memory`` / ``sources: []`` share, derived from a scan rather than
hand inspection).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from athenaeum.cli import main
from athenaeum.recovery_yield import STATE_NAME, write_state


def _run(*args: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, object]]:
    rc = main(["recovery-yield", *args])
    out = capsys.readouterr().out
    return rc, json.loads(out)


class TestSignalReadout:
    def test_prints_valid_json_and_exits_zero_above_threshold(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cache = tmp_path / "cache"
        write_state(cache, uncited=10, recovered=8, write_cited=6, time_window=2)
        rc, payload = _run(
            "--cache-dir", str(cache), "--path", str(tmp_path / "no-knowledge"), capsys=capsys
        )
        assert rc == 0
        assert payload["uncited"] == 10
        assert payload["recovered"] == 8
        assert payload["write_cited"] == 6
        assert payload["time_window"] == 2
        assert payload["rate"] == pytest.approx(0.8)
        assert payload["within_threshold"] is True
        assert payload["verdict"] == "ok"
        assert isinstance(payload["updated"], str)

    def test_exits_zero_below_threshold_too(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The breach is carried in the JSON, never the exit code (AC3)."""
        cache = tmp_path / "cache"
        write_state(cache, uncited=10, recovered=1, write_cited=1, time_window=0)
        rc, payload = _run(
            "--cache-dir", str(cache), "--path", str(tmp_path / "no-knowledge"), capsys=capsys
        )
        assert rc == 0
        assert payload["within_threshold"] is False
        assert payload["verdict"] == "breach"

    def test_no_data_when_nothing_recovered_yet(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cache = tmp_path / "cache"
        rc, payload = _run(
            "--cache-dir", str(cache), "--path", str(tmp_path / "no-knowledge"), capsys=capsys
        )
        assert rc == 0
        assert payload["uncited"] == 0
        assert payload["rate"] is None
        assert payload["within_threshold"] is None
        assert payload["verdict"] == "no-data"
        assert payload["updated"] is None

    def test_threshold_env_override_is_reflected(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_RECOVERY_YIELD_THRESHOLD", "0.9")
        cache = tmp_path / "cache"
        write_state(cache, uncited=10, recovered=8, write_cited=8, time_window=0)
        rc, payload = _run(
            "--cache-dir", str(cache), "--path", str(tmp_path / "no-knowledge"), capsys=capsys
        )
        assert rc == 0
        assert payload["threshold"] == 0.9
        assert payload["within_threshold"] is False  # 0.8 < 0.9

    def test_read_does_not_mutate_the_state_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cache = tmp_path / "cache"
        write_state(cache, uncited=4, recovered=3, write_cited=2, time_window=1)
        before = (cache / STATE_NAME).read_text(encoding="utf-8")
        _run("--cache-dir", str(cache), "--path", str(tmp_path / "no-knowledge"), capsys=capsys)
        after = (cache / STATE_NAME).read_text(encoding="utf-8")
        assert before == after


class TestCorpusScan:
    def test_missing_knowledge_dir_reports_null_corpus_fields(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cache = tmp_path / "cache"
        rc, payload = _run(
            "--cache-dir", str(cache), "--path", str(tmp_path / "does-not-exist"), capsys=capsys
        )
        assert rc == 0
        assert payload["auto_memory_pages"] is None
        assert payload["auto_memory_pages_empty_sources"] is None
        assert payload["auto_memory_empty_sources_share"] is None
        # The signal half still renders even with no corpus mounted.
        assert payload["verdict"] == "no-data"

    def _write_page(
        self, wiki_root: Path, name: str, *, page_type: str, sources: list[object] | None
    ) -> None:
        meta_lines = [f"type: {page_type}"]
        if sources is None:
            body_sources = ""
        else:
            rendered = "[]" if not sources else str(sources)
            body_sources = f"sources: {rendered}\n"
        text = "---\n" + "\n".join(meta_lines) + "\n" + body_sources + "---\nBody.\n"
        (wiki_root / name).write_text(text, encoding="utf-8")

    def test_counts_empty_and_nonempty_sources_separately(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        knowledge_root = tmp_path / "knowledge"
        wiki_root = knowledge_root / "wiki"
        wiki_root.mkdir(parents=True)
        self._write_page(wiki_root, "auto-empty-one.md", page_type="auto-memory", sources=[])
        self._write_page(wiki_root, "auto-empty-two.md", page_type="auto-memory", sources=None)
        self._write_page(
            wiki_root,
            "auto-cited.md",
            page_type="auto-memory",
            sources=[{"session": "abc"}],
        )
        # A non-auto-memory page must not be counted at all.
        self._write_page(wiki_root, "auto-other-type.md", page_type="person", sources=[])
        # A non-`auto-*` page must not be counted (discover_auto_pages is
        # scoped to the `auto-` prefix, same as auto_memory_prune).
        (wiki_root / "not-auto-prefixed.md").write_text(
            "---\ntype: auto-memory\nsources: []\n---\nBody.\n", encoding="utf-8"
        )

        cache = tmp_path / "cache"
        rc, payload = _run("--cache-dir", str(cache), "--path", str(knowledge_root), capsys=capsys)
        assert rc == 0
        assert payload["auto_memory_pages"] == 3
        assert payload["auto_memory_pages_empty_sources"] == 2
        assert payload["auto_memory_empty_sources_share"] == pytest.approx(2 / 3)

    def test_no_auto_memory_pages_reports_zero_not_null(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An existing but empty/irrelevant wiki is a real zero, not 'no corpus'."""
        knowledge_root = tmp_path / "knowledge"
        wiki_root = knowledge_root / "wiki"
        wiki_root.mkdir(parents=True)
        self._write_page(wiki_root, "auto-other.md", page_type="person", sources=[])

        cache = tmp_path / "cache"
        rc, payload = _run("--cache-dir", str(cache), "--path", str(knowledge_root), capsys=capsys)
        assert rc == 0
        assert payload["auto_memory_pages"] == 0
        assert payload["auto_memory_pages_empty_sources"] == 0
        assert payload["auto_memory_empty_sources_share"] is None


class TestHelp:
    def test_help_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main(["recovery-yield", "--help"])
        assert excinfo.value.code == 0


class TestCorpusScanRejectsNonListSources:
    """Issue athenaeum#1453 review follow-up: a falsy test alone is not enough.

    Frontmatter is an OPEN schema (``models.parse_frontmatter`` deliberately
    round-trips non-core keys), so a hand-edited page can carry ``sources`` as
    a scalar. A bare ``if not sources:`` disagrees with itself across those —
    it counts ``0``/``false`` as empty but a non-empty string as provenance —
    even though none of them is a provenance LIST. All three belong in the
    empty-sources cohort, which is what makes the reported share a truthful
    answer to athenaeum#1452's AC2.
    """

    def _write_raw_page(self, wiki_root: Path, name: str, sources_line: str) -> None:
        (wiki_root / name).write_text(
            f"---\ntype: auto-memory\n{sources_line}\n---\nBody.\n", encoding="utf-8"
        )

    def test_scalar_sources_values_all_count_as_empty(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        knowledge_root = tmp_path / "knowledge"
        wiki_root = knowledge_root / "wiki"
        wiki_root.mkdir(parents=True)
        # Three non-list scalars: a falsy int, a falsy bool, and a TRUTHY
        # string. The string is the discriminating case — a bare falsy test
        # would call it provenance.
        self._write_raw_page(wiki_root, "auto-scalar-zero.md", "sources: 0")
        self._write_raw_page(wiki_root, "auto-scalar-false.md", "sources: false")
        self._write_raw_page(wiki_root, "auto-scalar-string.md", "sources: abc")
        # One genuinely cited page, as a positive control.
        self._write_raw_page(wiki_root, "auto-really-cited.md", "sources:\n  - session: abc")

        cache = tmp_path / "cache"
        rc, payload = _run("--cache-dir", str(cache), "--path", str(knowledge_root), capsys=capsys)
        assert rc == 0
        assert payload["auto_memory_pages"] == 4
        assert payload["auto_memory_pages_empty_sources"] == 3
        assert payload["auto_memory_empty_sources_share"] == pytest.approx(3 / 4)
