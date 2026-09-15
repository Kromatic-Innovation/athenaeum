# SPDX-License-Identifier: Apache-2.0
"""Tests for the Claude Code ``UserPromptSubmit`` adapter (issue athenaeum#1621).

One test class per acceptance criterion, mirroring ``tests/test_context_core.py``'s
own convention of naming the counter-example each test defeats.
"""

from __future__ import annotations

import ast
import io
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
SRC = str(_REPO / "src")
ADAPTER_PY = _REPO / "src" / "athenaeum" / "claude_code_adapter.py"

_BASE_ENV = {"PYTHONPATH": SRC, "PATH": "/usr/bin:/bin"}


def _build_index(
    path: Path,
    pages: int,
    *,
    extra_rows: list[tuple] | None = None,
) -> Path:
    """Minimal FTS5 fixture index, mirroring ``tests/test_context_core.py``'s
    ``_build_index`` (not imported from there — that module has no public
    fixture surface of its own; each test file builds its own tiny index).
    """
    ddl = (
        "CREATE VIRTUAL TABLE wiki USING fts5("
        "filename, name, tags, aliases, description, "
        "audience UNINDEXED, type UNINDEXED, memory_tier UNINDEXED, "
        'tokenize="porter unicode61")'
    )
    conn = sqlite3.connect(path)
    conn.execute(ddl)
    rows = []
    for i in range(pages):
        rows.append(
            (
                "filler-page-%d.md" % i,
                "Filler Page %d" % i,
                "filler",
                "",
                "filler description %d" % i,
                "|__access_open__|",
                "reference",
                "warm",
            )
        )
    if extra_rows:
        rows.extend(extra_rows)
    conn.executemany(
        "INSERT INTO wiki (filename, name, tags, aliases, description, "
        "audience, type, memory_tier) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    return path


def _run_adapter(
    stdin_json: str, *, cache_dir: Path, timeout: float = 30
) -> subprocess.CompletedProcess:
    env = dict(_BASE_ENV)
    env["ATHENAEUM_CACHE_DIR"] = str(cache_dir)
    return subprocess.run(
        [sys.executable, "-m", "athenaeum.claude_code_adapter"],
        input=stdin_json,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def _run_cli_context(
    stdin_json: str, *, cache_dir: Path, timeout: float = 30
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "athenaeum.cli",
            "context",
            "--stdin-json",
            "--cache-dir",
            str(cache_dir),
        ],
        input=stdin_json,
        capture_output=True,
        text=True,
        env=dict(_BASE_ENV),
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# AC1 — additionalContext equals `athenaeum context --stdin-json`'s render.text
# ---------------------------------------------------------------------------


class TestAC1MatchesCliRender:
    """Counter-example this defeats: an adapter that re-renders from
    ``candidates[]`` with its own bullet format, drifting from the core's
    ``render.text`` the moment either implementation changes independently.
    """

    def test_additional_context_equals_cli_render_text(self, tmp_path: Path) -> None:
        # Two independently-built, identical fixture indexes under separate
        # cache dirs — NOT one shared cache dir — so the CLI call's
        # session-dedup bookkeeping can never suppress a candidate the
        # adapter call would otherwise also see (both are Tier-1 callers of
        # the same session-dedup mechanism; sharing a cache dir would make
        # whichever call runs second see fewer candidates than the first,
        # for a reason that has nothing to do with this equality claim).
        extra_row = (
            "gribwood-page.md",
            "Gribwood Widget",
            "gribwood",
            "",
            "the only gribwood page in this fixture",
            "|__access_open__|",
            "reference",
            "warm",
        )
        cache_a = tmp_path / "cache-a"
        cache_b = tmp_path / "cache-b"
        cache_a.mkdir()
        cache_b.mkdir()
        _build_index(cache_a / "wiki-index.db", 0, extra_rows=[extra_row])
        _build_index(cache_b / "wiki-index.db", 0, extra_rows=[extra_row])

        stdin_payload = json.dumps({"prompt": "gribwood widget", "session_id": "ac1-sess"})

        cli_result = _run_cli_context(stdin_payload, cache_dir=cache_a)
        assert cli_result.returncode == 0, cli_result.stderr
        cli_envelope = json.loads(cli_result.stdout)
        expected_text = cli_envelope["render"]["text"]
        # Sanity: the fixture actually produced a hit, or this test would
        # pass vacuously on two empty strings.
        assert expected_text != "", "fixture produced no candidates — test is vacuous"

        adapter_result = _run_adapter(stdin_payload, cache_dir=cache_b)
        assert adapter_result.returncode == 0, adapter_result.stderr
        assert adapter_result.stderr == ""
        adapter_lines = [line for line in adapter_result.stdout.splitlines() if line.strip()]
        assert len(adapter_lines) == 1, f"expected one JSON line, got: {adapter_result.stdout!r}"
        hook_output = json.loads(adapter_lines[0])

        assert hook_output == {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": expected_text,
            }
        }


# ---------------------------------------------------------------------------
# AC2 — no SQL, no ranking, no budget arithmetic in the adapter's own source
# ---------------------------------------------------------------------------

_FORBIDDEN_IMPORT_MODULES = {
    "sqlite3",
    "athenaeum.search",
    "athenaeum.push_metrics",
    "athenaeum.provider",
}

# A real SQL statement shape, not a bare keyword — "from" and "where" are
# ordinary English words and would false-positive a naive substring check
# (the exact failure mode this issue warns against: "too weak" for a bare
# `SELECT` grep, but a bare-keyword grep would itself be "too broad").
_SQL_STATEMENT_RE = re.compile(
    r"\bSELECT\b.{0,200}?\bFROM\b|\bINSERT\s+INTO\b|\bDELETE\s+FROM\b|\bCREATE\s+(VIRTUAL\s+)?TABLE\b",
    re.IGNORECASE | re.DOTALL,
)

# Identifiers that name ranking/scoring/budget concepts. Checked against
# every Name/Attribute the adapter's AST references — not a text grep, so
# `additionalContext`/`hookSpecificOutput` (this file's own necessary
# vocabulary) can't collide with it.
_RETRIEVAL_IDENTIFIER_RE = re.compile(
    r"^(rank\w*|score\w*|sort\w*|relevance\w*|bm25\w*|budget\w*|token_cost\w*|estimate_tokens\w*)$",
    re.IGNORECASE,
)


def _scan_for_retrieval_logic(source: str) -> list[str]:
    """Return a list of violation descriptions, or ``[]`` if clean.

    Three independent checks, each aimed at one clause of AC2:
    - **No SQL**: no import of ``sqlite3`` or another module that itself
      talks to the index/telemetry directly, and no string literal shaped
      like a SQL statement.
    - **No ranking**: no identifier from the rank/score/sort/relevance
      vocabulary, and no call to the builtin ``sorted`` or a ``.sort()``
      method.
    - **No budget arithmetic**: no arithmetic expression (``BinOp``) at
      all — a thin adapter that only parses JSON and wraps a string needs
      literally zero arithmetic, so this is a clean, non-heuristic proxy
      for "no budget arithmetic" specifically (budget packing is
      necessarily arithmetic; forbidding all arithmetic forbids it as a
      strict subset without having to name every budget-shaped variable).
    """
    violations: list[str] = []
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _FORBIDDEN_IMPORT_MODULES or alias.name == "sqlite3":
                    violations.append(f"forbidden import: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod in _FORBIDDEN_IMPORT_MODULES:
                violations.append(f"forbidden import-from: {mod}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _SQL_STATEMENT_RE.search(node.value):
                violations.append(f"SQL-shaped string literal: {node.value!r}")
        elif isinstance(node, ast.Name):
            if _RETRIEVAL_IDENTIFIER_RE.match(node.id):
                violations.append(f"ranking/budget identifier: {node.id}")
        elif isinstance(node, ast.Attribute):
            if _RETRIEVAL_IDENTIFIER_RE.match(node.attr):
                violations.append(f"ranking/budget attribute: {node.attr}")
            if node.attr == "sort":
                violations.append("in-place .sort() call")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "sorted"
        ):
            violations.append("sorted() call")
        elif isinstance(node, ast.BinOp) and not isinstance(
            node.op, (ast.BitOr, ast.BitAnd, ast.BitXor, ast.LShift, ast.RShift)
        ):
            # Excludes the bitwise-op family: `from __future__ import
            # annotations` still parses a PEP 604 `list[str] | None` type
            # hint as `ast.BinOp(op=BitOr)` — a type annotation, not
            # arithmetic. Everything else (`+ - * / // % **`) is real
            # arithmetic and stays forbidden.
            violations.append(f"arithmetic expression ({type(node.op).__name__})")

    return violations


class TestAC2NoRetrievalLogic:
    def test_adapter_source_is_clean(self) -> None:
        source = ADAPTER_PY.read_text(encoding="utf-8")
        violations = _scan_for_retrieval_logic(source)
        assert violations == [], (
            f"the adapter's own source must contain no SQL, ranking, or budget "
            f"arithmetic (issue athenaeum#1621 AC2); found: {violations}"
        )

    def test_guard_detects_sql_offender(self) -> None:
        offender = "import sqlite3\nQ = 'SELECT filename FROM wiki WHERE 1'\n"
        assert any("SQL" in v or "sqlite3" in v for v in _scan_for_retrieval_logic(offender))

    def test_guard_detects_ranking_offender(self) -> None:
        offender = "candidates = []\ncandidates.sort(key=lambda c: c['rank'])\n"
        assert any("sort" in v.lower() for v in _scan_for_retrieval_logic(offender))

    def test_guard_detects_sorted_call_offender(self) -> None:
        offender = "candidates = sorted([], key=lambda c: c['relevance'])\n"
        assert any("sorted" in v for v in _scan_for_retrieval_logic(offender))

    def test_guard_detects_budget_arithmetic_offender(self) -> None:
        offender = "budget = 1200\nused = budget - 400\n"
        assert any("arithmetic" in v for v in _scan_for_retrieval_logic(offender))

    def test_guard_is_clean_on_the_thin_shape(self) -> None:
        """Negative control on the checker itself: legitimate adapter-shaped
        code (JSON parse + dict wrap, no retrieval vocabulary) must NOT
        trip any of the three checks."""
        clean = (
            "import json\n"
            "def f(payload):\n"
            "    data = json.loads(payload)\n"
            "    return json.dumps({'hookSpecificOutput': {'additionalContext': data}})\n"
        )
        assert _scan_for_retrieval_logic(clean) == []


# ---------------------------------------------------------------------------
# AC3 — fail-safe: exit 0 and print nothing on no-match, core failure, or
# malformed input
# ---------------------------------------------------------------------------


class TestAC3FailSafe:
    def test_no_matching_pages_prints_nothing(self, tmp_path: Path) -> None:
        """No fixture row matches the prompt at all — an empty FTS5 index."""
        _build_index(tmp_path / "wiki-index.db", 0)
        stdin_payload = json.dumps({"prompt": "zzzznonexistentqueryzzzz", "session_id": "s"})
        result = _run_adapter(stdin_payload, cache_dir=tmp_path)
        assert result.returncode == 0
        assert result.stdout == ""
        assert result.stderr == ""

    def test_core_raising_prints_nothing(self, tmp_path: Path) -> None:
        """A genuinely corrupt index — not a code fixture, a real failure
        mode `build_context` does not itself catch (an unreadable/not-a-
        database file raises `sqlite3.DatabaseError` out of
        `_probe_schema`, uncaught by the core — see that module's own
        try/except boundaries, which stop at `sqlite3.OperationalError`
        inside `_query_fts5`, not at connection/schema-probe time)."""
        (tmp_path / "wiki-index.db").write_bytes(b"not a sqlite database\x00\x01\x02")
        stdin_payload = json.dumps({"prompt": "some ordinary prompt words", "session_id": "s"})
        result = _run_adapter(stdin_payload, cache_dir=tmp_path)
        assert result.returncode == 0
        assert result.stdout == ""
        assert result.stderr == ""

    def test_core_raising_prints_nothing_in_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Same claim as above, exercised in-process against `main()`
        directly so the failure is unambiguously attributed to the
        adapter's own `except Exception` (not to some other process-level
        accident like a segfault swallowing output)."""
        sys.path.insert(0, SRC)
        try:
            import athenaeum.claude_code_adapter as adapter
            import athenaeum.context as context_mod

            def _raise(*a, **k):
                raise RuntimeError("simulated core failure")

            monkeypatch.setattr(context_mod, "build_context_for_turn", _raise)
            monkeypatch.setattr(
                sys, "stdin", io.StringIO(json.dumps({"prompt": "hello there", "session_id": "s"}))
            )
            import contextlib

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = adapter.main()
            assert rc == 0
            assert buf.getvalue() == ""
        finally:
            sys.path.remove(SRC)

    def test_malformed_stdin_json_prints_nothing(self, tmp_path: Path) -> None:
        result = _run_adapter("{not valid json at all", cache_dir=tmp_path)
        assert result.returncode == 0
        assert result.stdout == ""
        assert result.stderr == ""

    def test_absent_stdin_prints_nothing(self, tmp_path: Path) -> None:
        result = _run_adapter("", cache_dir=tmp_path)
        assert result.returncode == 0
        assert result.stdout == ""
        assert result.stderr == ""

    def test_missing_prompt_key_prints_nothing(self, tmp_path: Path) -> None:
        result = _run_adapter(json.dumps({"session_id": "s"}), cache_dir=tmp_path)
        assert result.returncode == 0
        assert result.stdout == ""
        assert result.stderr == ""
