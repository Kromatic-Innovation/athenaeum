# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#519/#521 (H9 + L3): a single cache-dir constant + resolver.

``athenaeum serve`` used to hardcode ``~/.cache/athenaeum`` and ignore
``ATHENAEUM_CACHE_DIR`` — so the compiler could write the index to one place
while the MCP server read another, and ``recall`` silently served a stale or
empty index (H9). The literal was constructed by hand at ~13 sites across 8
modules (L3), so "honours the env var" was a per-site property new code got
wrong by default. These tests pin the resolver's precedence, prove ``serve``
now honours the env var and the new ``--cache-dir`` flag, and guard that the
literal is constructed in exactly one module.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from athenaeum.config import DEFAULT_CACHE_DIR, resolve_cache_dir


class TestResolveCacheDirPrecedence:
    """arg > ATHENAEUM_CACHE_DIR env > ~/.cache/athenaeum default."""

    def test_default_when_unset(self, monkeypatch) -> None:
        monkeypatch.delenv("ATHENAEUM_CACHE_DIR", raising=False)
        assert resolve_cache_dir() == DEFAULT_CACHE_DIR.expanduser()

    def test_env_override(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(tmp_path / "envcache"))
        assert resolve_cache_dir() == tmp_path / "envcache"

    def test_arg_beats_env(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(tmp_path / "envcache"))
        assert resolve_cache_dir(tmp_path / "argcache") == tmp_path / "argcache"

    def test_empty_env_falls_back_to_default(self, monkeypatch) -> None:
        # An empty string is not a usable path — treat it as unset.
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", "")
        assert resolve_cache_dir() == DEFAULT_CACHE_DIR.expanduser()

    def test_env_value_is_expanduser_ed(self, monkeypatch) -> None:
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", "~/somewhere/cache")
        assert resolve_cache_dir() == (Path.home() / "somewhere" / "cache")


class TestServeHonoursCacheDir:
    """H9: ``serve`` routes its cache dir through the resolver."""

    @staticmethod
    def _make_knowledge(tmp_path: Path) -> Path:
        knowledge = tmp_path / "knowledge"
        (knowledge / "raw").mkdir(parents=True)
        (knowledge / "wiki").mkdir(parents=True)
        return knowledge

    def _run_serve(self, monkeypatch, knowledge: Path, cache_dir_arg):
        import athenaeum._cmd_serve as cmd_serve_mod
        import athenaeum.mcp_server as mcp

        captured: dict[str, object] = {}

        class _FakeServer:
            def run(self) -> None:  # pragma: no cover - trivial
                return None

        def _fake_create_server(**kwargs):
            captured.update(kwargs)
            return _FakeServer()

        # Keep the serve-root resolution deterministic regardless of any
        # KNOWLEDGE_*_PATH set in the ambient environment.
        monkeypatch.delenv("KNOWLEDGE_RAW_PATH", raising=False)
        monkeypatch.delenv("KNOWLEDGE_WIKI_PATH", raising=False)
        monkeypatch.setattr(mcp, "create_server", _fake_create_server)
        args = argparse.Namespace(
            path=knowledge,
            audience=None,
            cache_dir=cache_dir_arg,
            verbose=False,
        )
        rc = cmd_serve_mod.cmd_serve(args)
        assert rc == 0
        return captured

    def test_serve_uses_env_cache_dir(self, monkeypatch, tmp_path: Path) -> None:
        knowledge = self._make_knowledge(tmp_path)
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(tmp_path / "envcache"))
        captured = self._run_serve(monkeypatch, knowledge, cache_dir_arg=None)
        assert captured["cache_dir"] == tmp_path / "envcache"

    def test_serve_flag_beats_env(self, monkeypatch, tmp_path: Path) -> None:
        knowledge = self._make_knowledge(tmp_path)
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(tmp_path / "envcache"))
        captured = self._run_serve(
            monkeypatch, knowledge, cache_dir_arg=tmp_path / "flagcache"
        )
        assert captured["cache_dir"] == tmp_path / "flagcache"

    def test_serve_subparser_declares_cache_dir_flag(self) -> None:
        # The serve subparser must accept --cache-dir (previously it did not).
        import athenaeum._cmd_serve as cmd_serve_mod
        import athenaeum.cli as cli

        rc_holder: dict[str, object] = {}

        def _fake_serve(args: argparse.Namespace) -> int:
            rc_holder["cache_dir"] = args.cache_dir
            return 0

        # main() rebuilds the parser (and re-binds set_defaults(func=...))
        # on every call, resolving add_serve_subparser's `cmd_serve` name from
        # this module's globals at call time — so patching the module-level
        # name here is visible to the next cli.main() dispatch.
        orig = cmd_serve_mod.cmd_serve
        cmd_serve_mod.cmd_serve = _fake_serve  # type: ignore[assignment]
        try:
            rc = cli.main(
                ["serve", "--path", "/tmp/kb", "--cache-dir", "/tmp/custom"]
            )
        finally:
            cmd_serve_mod.cmd_serve = orig  # type: ignore[assignment]
        assert rc == 0
        assert rc_holder["cache_dir"] == Path("/tmp/custom")


def test_cache_dir_literal_constructed_in_exactly_one_module() -> None:
    """L3 guard: only ``config.py`` constructs the ``~/.cache/athenaeum`` literal.

    Matches the two code shapes the literal took across the tree —
    ``Path("~/.cache/athenaeum")`` and ``Path.home() / ".cache" / "athenaeum"``.
    Docstrings / help text mentioning the path are fine; this targets code.
    """
    src_root = Path(__file__).resolve().parent.parent / "src" / "athenaeum"
    construction = re.compile(
        r'Path\(\s*["\']~/\.cache/athenaeum["\']\s*\)'
        r'|Path\.home\(\)\s*/\s*["\']\.cache["\']\s*/\s*["\']athenaeum["\']'
    )
    offenders: list[str] = []
    for py in src_root.rglob("*.py"):
        if py.name == "config.py":
            continue  # the single canonical home of DEFAULT_CACHE_DIR
        text = py.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            # The regex only matches an actual Path construction, which never
            # appears in the prose docstrings in this tree — help text and
            # docstrings mentioning the path are fine.
            if construction.search(line):
                offenders.append(f"{py.name}:{lineno}")
    assert offenders == [], (
        "The ~/.cache/athenaeum literal must be constructed only in config.py; "
        f"found constructions at: {offenders}"
    )


def _find_env_literal_spans(text: str) -> list[tuple[int, str]]:
    """Return ``(line_no, literal_text)`` for each ``env = {...}`` / ``env={...}``
    dict-literal in *text*, via balanced-brace matching from the ``{``
    immediately after the ``=``. Deliberately targets only hand-built LITERAL
    dicts (``env = {"HOME": ...}``) — not derived envs like ``dict(os.environ)``,
    which don't set ``HOME`` as a literal key and are out of scope here.
    """
    spans: list[tuple[int, str]] = []
    for m in re.finditer(r"\benv\s*=\s*\{", text):
        start = m.end() - 1  # index of the opening brace
        depth = 0
        i = start
        while i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        literal = text[start : i + 1]
        line_no = text.count("\n", 0, m.start()) + 1
        spans.append((line_no, literal))
    return spans


def test_env_literal_setting_home_also_sets_cache_dir() -> None:
    """Guard (athenaeum#791): a hand-built ``env = {...}`` dict literal that sets
    ``HOME`` must also set ``ATHENAEUM_CACHE_DIR`` explicitly.

    ``resolve_cache_dir()``'s precedence is ``arg > ATHENAEUM_CACHE_DIR env >
    ~/.cache/athenaeum default``. A subprocess env dict that redirects ``HOME``
    but omits ``ATHENAEUM_CACHE_DIR`` is protected only INCIDENTALLY — by
    ``HOME`` no longer pointing at the real home directory. Any athenaeum
    Python code such a subprocess shells out to, that resolves its cache dir
    with no explicit argument, falls through straight to the operator's real
    ``~/.cache/athenaeum`` the moment a future edit changes what ``HOME``
    points to while leaving the rest of the dict unchanged — exactly the
    escape route the athenaeum#791 correction identified in
    ``tests/test_shell_hooks.py``'s ``hook_env`` fixture (fixed alongside this
    guard, which is why it now passes). Requiring ``ATHENAEUM_CACHE_DIR``
    explicitly is belt-and-braces, not a behavior change: the example shell
    hooks derive their own cache dir from ``HOME``, so this does not change
    any hook's resolved cache dir today — it only protects the Python-side
    resolver against a future dict that stops redirecting ``HOME``.
    """
    tests_root = Path(__file__).resolve().parent
    self_path = Path(__file__).resolve()
    offenders: list[str] = []
    for py in sorted(tests_root.rglob("*.py")):
        if py.resolve() == self_path:
            continue  # this module's own docstrings quote the flagged shape as an example
        text = py.read_text(encoding="utf-8")
        for line_no, literal in _find_env_literal_spans(text):
            if '"HOME"' not in literal and "'HOME'" not in literal:
                continue
            if "ATHENAEUM_CACHE_DIR" in literal:
                continue
            offenders.append(f"{py.relative_to(tests_root)}:{line_no}")
    assert offenders == [], (
        "env literal(s) set HOME without also setting ATHENAEUM_CACHE_DIR — a "
        "hand-built subprocess env dict must not rely on HOME's redirect alone "
        f"(athenaeum#791): {offenders}"
    )
