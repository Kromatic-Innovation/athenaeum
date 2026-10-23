# SPDX-License-Identifier: Apache-2.0
"""Tests for the Codex Tier-2 static context-file adapter (issue athenaeum#1364).

One test group per acceptance criterion; each names the counter-example it
defeats. Every test writes under ``tmp_path`` — never ``~/.codex``.
"""

from __future__ import annotations

import ast
import json
import re
import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from athenaeum import codex_adapter
from athenaeum.codex_adapter import refresh

_REPO = Path(__file__).resolve().parent.parent
ADAPTER_PY = _REPO / "src" / "athenaeum" / "codex_adapter.py"

SEED = "zebraquux"
NOW = datetime(2026, 10, 23, 14, 0, 0, tzinfo=timezone.utc)
PUBLIC = "|__access_open__|"
RESTRICTED = "|team-secret|"


def _clock() -> datetime:
    return NOW


def _build_index(path: Path, rows: list[tuple[str, str, str]]) -> Path:
    """FTS5 fixture index; rows are (filename, name, audience)."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE VIRTUAL TABLE wiki USING fts5("
        "filename, name, tags, aliases, description, "
        "audience UNINDEXED, type UNINDEXED, memory_tier UNINDEXED, "
        'tokenize="porter unicode61")'
    )
    conn.executemany(
        "INSERT INTO wiki (filename, name, tags, aliases, description, "
        "audience, type, memory_tier) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (fn, name, SEED, "", f"{SEED} description of {name}", aud, "reference", "warm")
            for fn, name, aud in rows
        ],
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture(autouse=True)
def _no_env_killswitch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATHENAEUM_DISABLED", raising=False)


@pytest.fixture
def cache(tmp_path: Path) -> Path:
    d = tmp_path / "cache"
    d.mkdir()
    _build_index(
        d / "wiki-index.db",
        [("alpha.md", "Alpha Page", PUBLIC), ("beta.md", "Beta Page", PUBLIC)],
    )
    return d


def _run(cache: Path, out: Path, **kw: Any) -> int:
    return refresh(SEED, cache_dir=cache, output=out, now=_clock, **kw)


def _header(text: str, key: str) -> str:
    m = re.search(rf"^- {key}: (.+)$", text, re.MULTILINE)
    assert m, f"missing header line {key}"
    return m.group(1)


def _ts(text: str, key: str) -> datetime:
    return datetime.strptime(_header(text, key), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _bullets(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.startswith("  - ")]


# AC2 -----------------------------------------------------------------------


def test_snapshot_header_carries_interval_and_staleness(cache: Path, tmp_path: Path) -> None:
    out = tmp_path / "o.md"
    assert _run(cache, out) == 0
    text = out.read_text()
    assert _header(text, "status") == "ok"
    assert int(_header(text, "refresh_interval_seconds")) == 900
    assert _ts(text, "generated_at") == NOW
    assert _ts(text, "stale_after") - _ts(text, "generated_at") == timedelta(seconds=1800)
    assert codex_adapter.FRESHNESS_RULE in text

    out2 = tmp_path / "o2.md"
    _run(cache, out2, interval_seconds=60, max_age_seconds=45)
    text2 = out2.read_text()
    assert int(_header(text2, "refresh_interval_seconds")) == 60
    assert _ts(text2, "stale_after") - _ts(text2, "generated_at") == timedelta(seconds=45)

    out3 = tmp_path / "o3.md"
    _run(cache, out3, interval_seconds=60)
    t3 = out3.read_text()
    assert _ts(t3, "stale_after") - _ts(t3, "generated_at") == timedelta(seconds=120)


# AC3 -----------------------------------------------------------------------


def test_refresh_failure_writes_stale_tombstone(
    cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "o.md"
    assert _run(cache, out) == 0
    assert _bullets(out.read_text())

    def boom(*a: Any, **k: Any) -> dict[str, Any]:
        raise RuntimeError("index corrupt")

    monkeypatch.setattr(codex_adapter, "build_context", boom)
    assert _run(cache, out) == 1
    text = out.read_text()
    assert _header(text, "status") == "refresh-failed"
    assert _header(text, "stale_after") == _header(text, "generated_at")
    assert _bullets(text) == []
    assert "Alpha Page" not in text and "Beta Page" not in text


# AC4 -----------------------------------------------------------------------


def test_kill_switch_overwrites_prior_snapshot(cache: Path, tmp_path: Path) -> None:
    out = tmp_path / "o.md"
    _run(cache, out)
    assert _header(out.read_text(), "status") == "ok"
    (cache / "disabled").write_text("")
    assert _run(cache, out) == 0
    text = out.read_text()
    assert _header(text, "status") == "disabled"
    assert _bullets(text) == []
    assert "Alpha Page" not in text


def test_compile_only_scope_leaves_snapshot_live(cache: Path, tmp_path: Path) -> None:
    out = tmp_path / "o.md"
    (cache / "disabled").write_text("compile")
    _run(cache, out)
    text = out.read_text()
    assert _header(text, "status") != "disabled"
    assert _header(text, "status") == "ok"


def test_empty_when_nothing_matches(cache: Path, tmp_path: Path) -> None:
    out = tmp_path / "o.md"
    assert refresh("nomatchtoken", cache_dir=cache, output=out, now=_clock) == 0
    text = out.read_text()
    assert _header(text, "status") == "empty"
    assert "No wiki pages matched the configured topics at generated_at." in text


# AC5 -----------------------------------------------------------------------


def test_refresh_calls_build_context_with_empty_exclude(
    cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = codex_adapter.build_context
    captured: list[dict[str, Any]] = []

    def spy(*args: Any, **kwargs: Any) -> dict[str, Any]:
        captured.append({"args": args, **kwargs})
        return real(*args, **kwargs)

    monkeypatch.setattr(codex_adapter, "build_context", spy)
    _run(cache, tmp_path / "o.md")
    assert len(captured) == 1
    call = captured[0]
    assert call["exclude"] == frozenset()
    assert isinstance(call["exclude"], frozenset)
    assert call["use_llm"] is False
    assert call["n"] == 7
    assert call["search_backend"] == "fts5"
    assert call["args"] == (SEED, "codex-context-refresh")


def test_consecutive_refreshes_are_stable(cache: Path, tmp_path: Path) -> None:
    out = tmp_path / "o.md"
    _run(cache, out)
    first = _bullets(out.read_text())
    _run(cache, out)
    second = _bullets(out.read_text())
    assert first, "snapshot must be non-empty"
    assert first == second
    assert list(cache.glob("context-seen-*")) == []


# AC6 -----------------------------------------------------------------------


def test_snapshot_body_is_render_text_verbatim(
    cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = codex_adapter.build_context
    seen: dict[str, Any] = {}

    def spy(*args: Any, **kwargs: Any) -> dict[str, Any]:
        env = real(*args, **kwargs)
        # Weird order + overflow line the adapter must not touch.
        env["render"]["text"] = "  - Zed — z\n  - Alpha — a\n  - Alpha — a\n(+3 more withheld)"
        seen["text"] = env["render"]["text"]
        return env

    monkeypatch.setattr(codex_adapter, "build_context", spy)
    out = tmp_path / "o.md"
    _run(cache, out)
    text = out.read_text()
    start = text.index(codex_adapter.PREAMBLE) + len(codex_adapter.PREAMBLE) + 1
    end = text.index(codex_adapter.END_MARKER)
    assert text[start:end] == seen["text"] + "\n"


# AC7 -----------------------------------------------------------------------


def test_seed_and_envelope_are_not_persisted(cache: Path, tmp_path: Path) -> None:
    sentinel = "sentinel9f3a7c1e"
    out = tmp_path / "o.md"
    refresh(f"{SEED} {sentinel}", cache_dir=cache, output=out, now=_clock)
    text = out.read_text()
    assert sentinel not in text
    assert '"candidates"' not in text and '"query"' not in text
    with pytest.raises(json.JSONDecodeError):
        json.loads(text)
    assert _bullets(text), "fixture must actually render bullets"


# AC8 -----------------------------------------------------------------------

_FORBIDDEN_MODULES = {
    "sqlite3",
    "athenaeum.search",
    "athenaeum.push_metrics",
    "athenaeum.provider",
    "athenaeum.claude_code_adapter",
}
_FORBIDDEN_NAMES = {"build_context_for_turn", "record_context_push"}
_FORBIDDEN_LITERALS = ("hookSpecificOutput", "additionalContext")


def _scan(source: str) -> list[str]:
    violations: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            violations += [f"import {a.name}" for a in node.names if a.name in _FORBIDDEN_MODULES]
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod in _FORBIDDEN_MODULES:
                violations.append(f"import-from {mod}")
            for alias in node.names:
                if f"{mod}.{alias.name}" in _FORBIDDEN_MODULES:
                    violations.append(f"import-from {mod}.{alias.name}")
                if alias.name in _FORBIDDEN_NAMES:
                    violations.append(f"imports {alias.name}")
        elif isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
            violations.append(f"name {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_NAMES:
            violations.append(f"attribute {node.attr}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            for lit in _FORBIDDEN_LITERALS:
                if lit in node.value:
                    violations.append(f"literal {lit}")
            for name in _FORBIDDEN_NAMES:
                if name in node.value:
                    violations.append(f"literal mentions {name}")
    return violations


def test_adapter_source_is_clean() -> None:
    assert _scan(ADAPTER_PY.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    "offender",
    [
        "import sqlite3\n",
        "from athenaeum import search\nfrom athenaeum.search import x\n",
        "from athenaeum.push_metrics import record_push\n",
        "from athenaeum.provider import p\n",
        "from athenaeum.claude_code_adapter import main\n",
        "from athenaeum.context import build_context_for_turn\n",
        "x = record_context_push\n",
        "y = ctx.build_context_for_turn(1)\n",
        "z = {'hookSpecificOutput': 1}\n",
        "z = 'additionalContext'\n",
    ],
)
def test_guard_detects_planted_offender(offender: str) -> None:
    assert _scan(offender), offender


def test_guard_is_clean_on_benign_source() -> None:
    assert _scan("import json\nfrom athenaeum.context import build_context\n") == []


# AC9 -----------------------------------------------------------------------


def test_new_snapshot_is_0600(cache: Path, tmp_path: Path) -> None:
    out = tmp_path / "sub" / "o.md"
    _run(cache, out)
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert [p.name for p in out.parent.iterdir()] == ["o.md"]


def test_existing_mode_is_preserved(cache: Path, tmp_path: Path) -> None:
    out = tmp_path / "o.md"
    out.write_text("old")
    out.chmod(0o644)
    _run(cache, out)
    assert stat.S_IMODE(out.stat().st_mode) == 0o644
    assert "Alpha Page" in out.read_text()


# AC12 ----------------------------------------------------------------------


def test_non_public_audience_withholds_whole_snapshot(tmp_path: Path) -> None:
    d = tmp_path / "cache"
    d.mkdir()
    _build_index(
        d / "wiki-index.db",
        [
            ("alpha.md", "Alpha Page", PUBLIC),
            ("secret.md", "Secret Page", RESTRICTED),
        ],
    )
    out = tmp_path / "o.md"
    assert _run(d, out) == 0
    text = out.read_text()
    assert _header(text, "status") == "withheld"
    assert "Snapshot withheld: at least one matching page is not public." in text
    assert _bullets(text) == []
    assert "Alpha Page" not in text and "Secret Page" not in text


def test_all_public_audience_renders(tmp_path: Path) -> None:
    d = tmp_path / "cache"
    d.mkdir()
    _build_index(
        d / "wiki-index.db",
        [("alpha.md", "Alpha Page", PUBLIC), ("both.md", "Both Page", "|__access_open__|x|")],
    )
    out = tmp_path / "o.md"
    _run(d, out)
    text = out.read_text()
    assert _header(text, "status") == "ok"
    assert len(_bullets(text)) == 2


# AC10 ----------------------------------------------------------------------


def test_cli_end_to_end_stdout(cache: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from athenaeum.cli import main

    assert main(["codex-context", "--seed", SEED, "--output", "-", "--cache-dir", str(cache)]) == 0
    assert "status: ok" in capsys.readouterr().out


def test_cli_exits_1_on_refresh_failure(
    cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from athenaeum.cli import main

    def boom(*a: Any, **k: Any) -> dict[str, Any]:
        raise RuntimeError("x")

    monkeypatch.setattr(codex_adapter, "build_context", boom)
    out = tmp_path / "o.md"
    argv = ["codex-context", "--seed", SEED, "--output", str(out), "--cache-dir", str(cache)]
    assert main(argv) == 1
    assert "status: refresh-failed" in out.read_text()


def test_cli_requires_seed() -> None:
    from athenaeum.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["codex-context"])
    assert exc.value.code == 2
