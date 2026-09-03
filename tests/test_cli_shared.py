# SPDX-License-Identifier: Apache-2.0
"""``athenaeum._cli_shared``'s ``--path`` resolvers (issue athenaeum#1349).

Pins the contract that used to be re-derived independently in nine
``_cmd_*.py`` modules (seven ``_resolve_wiki_root`` copies plus two more
``_resolve_knowledge_root`` copies in ``_cmd_storage.py`` and
``_cmd_authority.py``) before being collapsed onto
:func:`athenaeum._cli_shared._resolve_knowledge_root` and
:func:`athenaeum._cli_shared._resolve_wiki_root`. Three cases, matching the
copies' silent assumptions:

- a ``~``-prefixed ``--path`` is expanded (the load-bearing case: a dropped
  ``.expanduser()`` would only break on a ``~``-relative path, so every
  absolute-path test elsewhere in the suite would keep passing regardless).
- a relative ``--path`` is resolved to an absolute one.
- an absent/``None`` ``path`` attribute on ``args`` falls back to
  :data:`athenaeum.config.DEFAULT_KNOWLEDGE_ROOT`.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from athenaeum._cli_shared import _resolve_knowledge_root, _resolve_wiki_root
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT


def _args(path: Path | None) -> argparse.Namespace:
    return argparse.Namespace(path=path)


class TestResolveKnowledgeRoot:
    def test_tilde_path_is_expanded(self, monkeypatch, tmp_path: Path) -> None:
        """A ``~``-prefixed ``--path`` resolves to the real home directory.

        The counter-example a dropped ``.expanduser()`` would fail: an
        absolute-path test would keep passing even without it, so this is
        the one case that actually exercises the call.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        result = _resolve_knowledge_root(_args(Path("~/knowledge-here")))
        assert result == (tmp_path / "knowledge-here")
        assert result.is_absolute()

    def test_relative_path_is_resolved_to_absolute(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.chdir(tmp_path)
        result = _resolve_knowledge_root(_args(Path("relative-knowledge")))
        assert result == (tmp_path / "relative-knowledge").resolve()
        assert result.is_absolute()

    def test_none_path_falls_back_to_default_knowledge_root(self) -> None:
        result = _resolve_knowledge_root(_args(None))
        assert result == DEFAULT_KNOWLEDGE_ROOT.expanduser().resolve()

    def test_absent_path_attribute_falls_back_to_default_knowledge_root(self) -> None:
        """``args`` without a ``path`` attribute at all (``getattr`` default)."""
        result = _resolve_knowledge_root(argparse.Namespace())
        assert result == DEFAULT_KNOWLEDGE_ROOT.expanduser().resolve()


class TestResolveWikiRoot:
    def test_appends_wiki_to_the_knowledge_root(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        result = _resolve_wiki_root(_args(Path("~/knowledge-here")))
        assert result == (tmp_path / "knowledge-here" / "wiki")

    def test_matches_resolve_knowledge_root_plus_wiki(self, tmp_path: Path) -> None:
        args = _args(tmp_path / "some-root")
        assert _resolve_wiki_root(args) == _resolve_knowledge_root(args) / "wiki"

    def test_none_path_falls_back_to_default_knowledge_root(self) -> None:
        result = _resolve_wiki_root(_args(None))
        assert result == DEFAULT_KNOWLEDGE_ROOT.expanduser().resolve() / "wiki"
