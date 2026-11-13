# SPDX-License-Identifier: Apache-2.0
"""One resolution rule per setting, across every recall read surface
(issue athenaeum#2458).

Two config resolvers already existed and had no ``src`` callers at all:

* :func:`athenaeum.config.resolve_search_backend` -- yaml ``search_backend``
  over the shipped default (``vector``).
* :func:`athenaeum.config.resolve_recall_cap_ceiling` --
  ``ATHENAEUM_RECALL_CAP_CEILING`` env over ``recall.cap.ceiling`` yaml over
  :data:`athenaeum.config.RECALL_CAP_CEILING_DEFAULT`.

Meanwhile ``athenaeum context``, ``athenaeum codex-context``,
:func:`athenaeum.context.build_context` and the Codex adapter pinned
``fts5``, about ten sites read ``search_backend`` inline with an ``fts5``
fallback that fired for any config dict that did not come from
``load_config``, and the Codex adapter used the bare ceiling constant. So the
same query could be ranked three different ways depending on which surface
asked it.

The counter-example every test here defeats is a surface that answers with
its own hardcoded default instead of the deployment's configured value.

Test groups mirror the issue's acceptance criteria:

* ``TestConfiguredBackendReachesEverySurface`` (AC-a) -- ``search_backend:
  vector`` in yaml, no flag.
* ``TestExplicitFlagStillWins`` (AC-b).
* ``TestHandBuiltConfigGetsResolverDefault`` (AC-c) -- the ``fts5``-fallback
  fix; these are the cases that FAIL on the pre-fix tree for the ``recall``
  CLI / library code paths.
* ``TestCodexAdapterCapCeiling`` (AC-d).

Note on AC-a's "each case fails on today's code": that holds for ``context``
and ``codex-context``, which pinned ``fts5`` outright. It does NOT hold for
the ``recall`` CLI or the MCP ``serve`` surface, because ``load_config``
merges ``_DEFAULTS`` and therefore ALWAYS supplies a ``search_backend`` key --
so their inline ``.get("search_backend", "fts5")`` never actually reached its
fallback when the config came from a file. The real defect on those two code
paths is the hand-built-dict case, which is why
``TestHandBuiltConfigGetsResolverDefault`` exists alongside this group rather
than as an afterthought.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from athenaeum.cli import main
from athenaeum.config import RECALL_CAP_CEILING_DEFAULT, resolve_search_backend

#: The resolver's own answer for "no yaml at all". Never typed as a literal
#: here -- the issue puts changing the resolver's default explicitly out of
#: scope, so these tests must track it rather than pin a second copy.
SHIPPED_BACKEND = resolve_search_backend(None)


def _write_wiki(root: Path) -> Path:
    """A minimal knowledge root: ``wiki/`` with one page, plus ``raw/``."""
    wiki = root / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    (root / "raw").mkdir(parents=True, exist_ok=True)
    (wiki / "alpha.md").write_text(
        "---\nname: Alpha Page\ndescription: a zebraquux reference page\n---\n\nbody\n",
        encoding="utf-8",
    )
    return wiki


def _build_fts5_index(path: Path) -> None:
    """The index shape ``athenaeum.context`` queries (see its ``_probe_schema``)."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE VIRTUAL TABLE wiki USING fts5("
        "filename, name, tags, aliases, description, "
        "audience UNINDEXED, type UNINDEXED, "
        'tokenize="porter unicode61")'
    )
    conn.execute(
        "INSERT INTO wiki (filename, name, tags, aliases, description, audience, type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "alpha.md",
            "Alpha Page",
            "zebraquux",
            "",
            "a zebraquux reference page",
            "|__access_open__|",
            "reference",
        ),
    )
    conn.commit()
    conn.close()


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing here may read the host's own deployment.

    ``athenaeum context`` / ``codex-context`` resolve their backend off
    ``load_config(None)``, which reads ``~/knowledge/athenaeum.yaml``; the
    knowledge-root env vars the packaged hooks export would likewise redirect
    ``serve``. Point HOME at an empty tmp dir and drop the rest, so a test
    that wants a configured value writes it itself.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name in (
        "ATHENAEUM_DISABLED",
        "ATHENAEUM_CACHE_DIR",
        "ATHENAEUM_RECALL_CAP_CEILING",
        "KNOWLEDGE_ROOT",
        "KNOWLEDGE_WIKI_PATH",
        "KNOWLEDGE_RAW_PATH",
        "SEARCH_BACKEND",
        "RECALL_CAP_CEILING",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def home_knowledge(tmp_path: Path) -> Path:
    """``$HOME/knowledge`` -- what ``load_config(None)`` reads."""
    root = tmp_path / "home" / "knowledge"
    _write_wiki(root)
    return root


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    d = tmp_path / "cache"
    d.mkdir()
    _build_fts5_index(d / "wiki-index.db")
    return d


# ---------------------------------------------------------------------------
# AC-a: the configured backend reaches every read surface.
# ---------------------------------------------------------------------------


class TestConfiguredBackendReachesEverySurface:
    """``search_backend: vector`` in yaml, no ``--backend`` anywhere."""

    def test_context_cli_reports_the_configured_backend(
        self, home_knowledge: Path, cache_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """FAILS on the pre-fix tree: ``_cmd_context.py``'s ``--backend``
        carried ``default="fts5"``, so the envelope reported ``fts5`` no
        matter what the deployment configured."""
        (home_knowledge / "athenaeum.yaml").write_text("search_backend: vector\n")

        rc = main(
            [
                "context",
                "a zebraquux reference page lookup",
                "--no-llm",
                "--cache-dir",
                str(cache_dir),
            ]
        )

        assert rc == 0
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["backend"] == "vector"

    def test_codex_context_reports_the_configured_backend(
        self, home_knowledge: Path, cache_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """FAILS on the pre-fix tree: both the ``--backend`` default and
        ``codex_adapter.refresh``'s own parameter default were ``fts5``."""
        (home_knowledge / "athenaeum.yaml").write_text("search_backend: vector\n")

        rc = main(
            [
                "codex-context",
                "--seed",
                "zebraquux",
                "--output",
                "-",
                "--cache-dir",
                str(cache_dir),
            ]
        )

        assert rc == 0
        assert "- backend: vector" in capsys.readouterr().out

    def test_recall_cli_queries_the_configured_backend(
        self, tmp_path: Path, cache_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``athenaeum recall`` resolves through ``resolve_search_backend``.

        Passes on the pre-fix tree too (see the module docstring): a
        file-backed config always carries the key, so the inline ``fts5``
        fallback never fired here. Kept because it pins the surface AC-a
        names, and because it is the guard that would catch a future
        re-hardcode.
        """
        root = tmp_path / "kb"
        _write_wiki(root)
        (root / "athenaeum.yaml").write_text("search_backend: vector\n")
        asked: list[str] = []

        class _StubBackend:
            def query(self, *args: Any, **kwargs: Any) -> list[Any]:
                return []

        import athenaeum.search as search_mod

        def _spy(name: str) -> _StubBackend:
            asked.append(name)
            return _StubBackend()

        monkeypatch.setattr(search_mod, "get_backend", _spy)

        rc = main(
            ["recall", "zebraquux", "--path", str(root), "--cache-dir", str(cache_dir)]
        )

        assert rc == 0
        assert asked == ["vector"]

    def test_mcp_serve_receives_the_configured_backend(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The MCP ``recall`` surface, resolved the same way.

        Passes on the pre-fix tree for the same reason as the ``recall`` CLI
        above; see the module docstring.
        """
        root = tmp_path / "kb"
        _write_wiki(root)
        (root / "athenaeum.yaml").write_text("auto_recall: true\nsearch_backend: vector\n")
        captured: dict[str, object] = {}

        class _FakeServer:
            def run(self) -> None:
                raise KeyboardInterrupt

        def _fake_create_server(**kwargs: object) -> _FakeServer:
            captured.update(kwargs)
            return _FakeServer()

        import athenaeum.mcp_server as mcp_mod

        monkeypatch.setattr(mcp_mod, "create_server", _fake_create_server)

        rc = main(["serve", "--path", str(root)])

        assert rc == 0
        assert captured["search_backend"] == "vector"


# ---------------------------------------------------------------------------
# AC-b: an explicit flag still wins.
# ---------------------------------------------------------------------------


class TestExplicitFlagStillWins:
    """``--backend fts5`` against a ``search_backend: vector`` deployment."""

    def test_context_cli_explicit_backend_wins(
        self, home_knowledge: Path, cache_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (home_knowledge / "athenaeum.yaml").write_text("search_backend: vector\n")

        rc = main(
            [
                "context",
                "a zebraquux reference page lookup",
                "--no-llm",
                "--backend",
                "fts5",
                "--cache-dir",
                str(cache_dir),
            ]
        )

        assert rc == 0
        assert json.loads(capsys.readouterr().out)["backend"] == "fts5"

    def test_codex_context_explicit_backend_wins(
        self, home_knowledge: Path, cache_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (home_knowledge / "athenaeum.yaml").write_text("search_backend: vector\n")

        rc = main(
            [
                "codex-context",
                "--seed",
                "zebraquux",
                "--output",
                "-",
                "--backend",
                "fts5",
                "--cache-dir",
                str(cache_dir),
            ]
        )

        assert rc == 0
        assert "- backend: fts5" in capsys.readouterr().out

    def test_recall_cli_explicit_backend_wins(
        self, tmp_path: Path, cache_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "kb"
        _write_wiki(root)
        (root / "athenaeum.yaml").write_text("search_backend: vector\n")
        asked: list[str] = []

        class _StubBackend:
            def query(self, *args: Any, **kwargs: Any) -> list[Any]:
                return []

        import athenaeum.search as search_mod

        monkeypatch.setattr(
            search_mod, "get_backend", lambda name: (asked.append(name), _StubBackend())[1]
        )

        rc = main(
            [
                "recall",
                "zebraquux",
                "--path",
                str(root),
                "--cache-dir",
                str(cache_dir),
                "--backend",
                "fts5",
            ]
        )

        assert rc == 0
        assert asked == ["fts5"]


# ---------------------------------------------------------------------------
# AC-c: a hand-built config dict gets the RESOLVER's default, never `fts5`.
# ---------------------------------------------------------------------------


class TestHandBuiltConfigGetsResolverDefault:
    """The ``fts5``-fallback defect, pinned where it actually bit.

    Every case here FAILS on the pre-fix tree: a config dict that never came
    from ``load_config`` (a library caller, a test, ``decision_answers``
    passing ``cfg=None``) carries no ``search_backend`` key, so the inline
    ``.get("search_backend", "fts5")`` silently overrode a ``vector``
    deployment.
    """

    def test_build_context_resolves_from_a_hand_built_config(
        self, cache_dir: Path
    ) -> None:
        from athenaeum.context import build_context

        envelope = build_context(
            "a zebraquux reference page lookup",
            "sess",
            cache_dir=cache_dir,
            use_llm=False,
            config={"auto_recall": True},
        )

        assert envelope["backend"] == SHIPPED_BACKEND

    def test_build_context_with_no_config_at_all_resolves_the_default(
        self, cache_dir: Path
    ) -> None:
        from athenaeum.context import build_context

        envelope = build_context(
            "a zebraquux reference page lookup",
            "sess",
            cache_dir=cache_dir,
            use_llm=False,
        )

        assert envelope["backend"] == SHIPPED_BACKEND

    def test_librarian_reindex_resolves_from_a_hand_built_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import athenaeum.search as search_mod
        from athenaeum import librarian

        root = tmp_path / "kb"
        wiki = _write_wiki(root)
        monkeypatch.setattr(search_mod, "build_vector_index", lambda *a, **k: 1)
        monkeypatch.setattr(search_mod, "build_fts5_index", lambda *a, **k: 1)

        backend_name, _pages = librarian.reindex(
            root, wiki, cache_dir=tmp_path / "cache2", config={"auto_recall": True}
        )

        assert backend_name == SHIPPED_BACKEND

    def test_cli_shared_rebuild_resolves_from_a_hand_built_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import athenaeum.search as search_mod
        from athenaeum._cli_shared import rebuild_recall_index

        root = tmp_path / "kb"
        _write_wiki(root)
        monkeypatch.setattr(search_mod, "build_vector_index", lambda *a, **k: 1)
        monkeypatch.setattr(search_mod, "build_fts5_index", lambda *a, **k: 1)

        rebuild_recall_index(
            root,
            {"auto_recall": True},
            argparse.Namespace(backend=None, cache_dir=str(tmp_path / "cache3")),
        )

        assert f"recall index rebuilt ({SHIPPED_BACKEND})" in capsys.readouterr().out

    def test_doctor_search_index_check_resolves_from_a_hand_built_config(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.doctor import check_search_index

        result = check_search_index(tmp_path / "empty-cache", {"auto_recall": True})

        assert SHIPPED_BACKEND in result.detail


def test_context_default_backend_constant_matches_the_resolver() -> None:
    """`athenaeum.context` keeps its OWN copy of the shipped backend default.

    Deliberately, and for the reason that module's import-weight contract
    gives: reaching `from athenaeum.config import resolve_search_backend`
    on every `build_context` call that names no backend costs ~15ms, which
    is 30% of the 50ms budget
    `tests/test_context_core.py::test_fts5_path_wall_clock_budget` pins --
    it reds that guard on a CI runner on its own. So `build_context` imports
    the resolver only when there IS a config to resolve from, and answers
    `config=None` from the constant instead.

    A second copy of a value is only safe if something keeps the copies
    equal. This is that something: the constant must equal both the yaml
    default the resolver reads and what the resolver itself returns for "no
    config", so flipping `_DEFAULTS["search_backend"]` without updating
    `context.py` fails here rather than silently splitting the sidecar path
    off from every other recall surface.
    """
    from athenaeum import context as context_mod
    from athenaeum.config import _DEFAULTS

    assert context_mod._DEFAULT_SEARCH_BACKEND == _DEFAULTS["search_backend"]
    assert context_mod._DEFAULT_SEARCH_BACKEND == resolve_search_backend(None)


def test_build_context_does_not_import_config_when_given_no_config() -> None:
    """The import-weight half of the contract above, asserted directly.

    The constant being correct is not enough -- `build_context` must also
    actually take the no-import arm. Checked in a clean subprocess (an
    in-process check is void: `athenaeum.config` is already in `sys.modules`
    by the time any test runs) with an explicit positive control, so a probe
    that simply cannot observe the import can never pass as a clean result.
    """
    import subprocess
    import sys

    probe = (
        "import sys\n"
        "from pathlib import Path\n"
        "from athenaeum.context import build_context\n"
        "assert 'athenaeum.config' not in sys.modules, 'config imported at import time'\n"
        "build_context('a prompt', 's', cache_dir=Path(sys.argv[1]), use_llm=False)\n"
        "print('NO_CONFIG', 'athenaeum.config' in sys.modules)\n"
        "build_context('a prompt', 's', cache_dir=Path(sys.argv[1]), use_llm=False,\n"
        "              config={'auto_recall': True})\n"
        "print('WITH_CONFIG', 'athenaeum.config' in sys.modules)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe, "/nonexistent-cache-dir-for-this-probe"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    lines = result.stdout.split()
    # The no-config call must NOT have pulled athenaeum.config in...
    assert "NO_CONFIG False" in result.stdout, result.stdout
    # ...and the positive control proves the probe can see it when it does.
    assert "WITH_CONFIG True" in result.stdout, (
        "positive control failed: passing a config dict must reach the "
        f"resolver, so athenaeum.config must be importable-and-imported ({lines})"
    )


# ---------------------------------------------------------------------------
# AC-d: the Codex adapter honours `recall.cap.ceiling` from yaml AND env.
# ---------------------------------------------------------------------------


class TestCodexAdapterCapCeiling:
    """``codex_adapter.refresh`` used the bare ``RECALL_CAP_CEILING_DEFAULT``
    constant, so neither the yaml key nor its env override reached it. Every
    test here FAILS on the pre-fix tree."""

    @staticmethod
    def _captured_n(
        home_knowledge: Path,
        cache_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        **kwargs: Any,
    ) -> int:
        from athenaeum import codex_adapter

        captured: dict[str, Any] = {}
        real = codex_adapter.build_context

        def _spy(*args: Any, **kw: Any) -> dict[str, Any]:
            captured.update(kw)
            return real(*args, **kw)

        monkeypatch.setattr(codex_adapter, "build_context", _spy)
        rc = codex_adapter.refresh(
            "zebraquux", cache_dir=cache_dir, output=tmp_path / "snap.md", **kwargs
        )
        assert rc == 0
        return int(captured["n"])

    def test_cap_ceiling_comes_from_yaml(
        self,
        home_knowledge: Path,
        cache_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (home_knowledge / "athenaeum.yaml").write_text("recall:\n  cap:\n    ceiling: 11\n")

        assert self._captured_n(home_knowledge, cache_dir, tmp_path, monkeypatch) == 11

    def test_cap_ceiling_comes_from_env(
        self,
        home_knowledge: Path,
        cache_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_RECALL_CAP_CEILING", "4")

        assert self._captured_n(home_knowledge, cache_dir, tmp_path, monkeypatch) == 4

    def test_env_cap_ceiling_wins_over_yaml(
        self,
        home_knowledge: Path,
        cache_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (home_knowledge / "athenaeum.yaml").write_text("recall:\n  cap:\n    ceiling: 11\n")
        monkeypatch.setenv("ATHENAEUM_RECALL_CAP_CEILING", "4")

        assert self._captured_n(home_knowledge, cache_dir, tmp_path, monkeypatch) == 4

    def test_no_config_falls_through_to_the_shared_default(
        self,
        home_knowledge: Path,
        cache_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        assert (
            self._captured_n(home_knowledge, cache_dir, tmp_path, monkeypatch)
            == RECALL_CAP_CEILING_DEFAULT
        )

    def test_explicit_n_still_wins_over_both(
        self,
        home_knowledge: Path,
        cache_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (home_knowledge / "athenaeum.yaml").write_text("recall:\n  cap:\n    ceiling: 11\n")
        monkeypatch.setenv("ATHENAEUM_RECALL_CAP_CEILING", "4")

        assert (
            self._captured_n(home_knowledge, cache_dir, tmp_path, monkeypatch, n=2) == 2
        )


# ---------------------------------------------------------------------------
# The Claude Code adapter's ceiling goes through the SAME resolver.
# ---------------------------------------------------------------------------


def test_claude_code_adapter_ceiling_delegates_to_the_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_resolve_recall_cap_ceiling`` keeps its documented
    ``ATHENAEUM_RECALL_CAP_CEILING`` > ``RECALL_CAP_CEILING`` >
    default precedence, but the value is now produced by
    :func:`athenaeum.config.resolve_recall_cap_ceiling` -- the hook-local
    cached name is handed to it in the YAML position. Asserted against the
    resolver rather than against literals, so this can never drift from it.
    """
    from athenaeum.claude_code_adapter import _resolve_recall_cap_ceiling
    from athenaeum.config import resolve_recall_cap_ceiling

    monkeypatch.delenv("ATHENAEUM_RECALL_CAP_CEILING", raising=False)
    monkeypatch.setenv("RECALL_CAP_CEILING", "5")
    assert _resolve_recall_cap_ceiling() == resolve_recall_cap_ceiling(
        {"recall": {"cap": {"ceiling": 5}}}
    )

    monkeypatch.setenv("ATHENAEUM_RECALL_CAP_CEILING", "3")
    assert _resolve_recall_cap_ceiling() == resolve_recall_cap_ceiling(
        {"recall": {"cap": {"ceiling": 5}}}
    )
    assert _resolve_recall_cap_ceiling() == 3

    monkeypatch.delenv("ATHENAEUM_RECALL_CAP_CEILING")
    for bad in ("0", "-3", "not-a-number", ""):
        monkeypatch.setenv("RECALL_CAP_CEILING", bad)
        assert _resolve_recall_cap_ceiling() == resolve_recall_cap_ceiling(None)
