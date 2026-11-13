# SPDX-License-Identifier: Apache-2.0
"""``athenaeum._cli_shared``'s knowledge-root resolvers (issue athenaeum#1349,
athenaeum#2445).

Pins the contract that used to be re-derived independently in ``_cmd_*.py``
modules (seven ``_resolve_wiki_root`` copies plus two more
``_resolve_knowledge_root`` copies in ``_cmd_storage.py`` and
``_cmd_authority.py``, then another dozen-plus ``args.path``/
``args.knowledge_root`` inline copies collapsed by athenaeum#2445) before
being collapsed onto :func:`athenaeum._cli_shared._resolve_knowledge_root`
and :func:`athenaeum._cli_shared._resolve_wiki_root`. Three cases, matching
the copies' silent assumptions:

- a ``~``-prefixed path is expanded (the load-bearing case: a dropped
  ``.expanduser()`` would only break on a ``~``-relative path, so every
  absolute-path test elsewhere in the suite would keep passing regardless).
- a relative path is resolved to an absolute one.
- an absent/``None`` attribute on ``args`` falls back to
  :data:`athenaeum.config.DEFAULT_KNOWLEDGE_ROOT`.

``TestResolveKnowledgeRoot`` pins this for the default ``attr="path"`` (the
``--path`` flag most subcommands expose); ``TestResolveKnowledgeRootAttrParam``
pins the SAME three cases for ``attr="knowledge_root"`` (the ``--knowledge-root``
flag `_cmd_reconcile.py` / `_cmd_pii_restore.py` / `_cmd_repair.py` expose),
asserting behavioural identity against the inline formula it replaced rather
than merely a plausible-looking result.
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


class TestResolveKnowledgeRootAttrParam:
    """``attr="knowledge_root"`` (issue athenaeum#2445) must resolve IDENTICALLY
    to the five inline ``(args.knowledge_root or DEFAULT_KNOWLEDGE_ROOT)
    .expanduser().resolve()`` copies it replaces (``_cmd_reconcile.py``,
    ``_cmd_pii_restore.py``, ``_cmd_repair.py`` x3) -- not merely produce *a*
    path, but the exact same path the inline formula produced, including the
    ``~``-expansion and relative-resolution behaviour a less careful rewrite
    could silently drop. Each case below asserts equality against that
    inline formula directly, not just against a plausible-looking result.
    """

    def _args(self, value: Path | None) -> argparse.Namespace:
        return argparse.Namespace(knowledge_root=value)

    def test_tilde_knowledge_root_matches_inline_formula(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        args = self._args(Path("~/knowledge-here"))
        expected = (args.knowledge_root or DEFAULT_KNOWLEDGE_ROOT).expanduser().resolve()
        result = _resolve_knowledge_root(args, attr="knowledge_root")
        assert result == expected
        assert result == (tmp_path / "knowledge-here")

    def test_relative_knowledge_root_matches_inline_formula(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        args = self._args(Path("relative-knowledge"))
        expected = (args.knowledge_root or DEFAULT_KNOWLEDGE_ROOT).expanduser().resolve()
        result = _resolve_knowledge_root(args, attr="knowledge_root")
        assert result == expected
        assert result == (tmp_path / "relative-knowledge").resolve()

    def test_none_knowledge_root_matches_inline_formula(self) -> None:
        args = self._args(None)
        expected = (args.knowledge_root or DEFAULT_KNOWLEDGE_ROOT).expanduser().resolve()
        result = _resolve_knowledge_root(args, attr="knowledge_root")
        assert result == expected
        assert result == DEFAULT_KNOWLEDGE_ROOT.expanduser().resolve()

    def test_absent_knowledge_root_attribute_matches_inline_formula(self) -> None:
        """``args`` without a ``knowledge_root`` attribute at all (``getattr`` default)."""
        result = _resolve_knowledge_root(argparse.Namespace(), attr="knowledge_root")
        assert result == DEFAULT_KNOWLEDGE_ROOT.expanduser().resolve()

    def test_matches_attr_path_for_the_same_raw_value(self, monkeypatch, tmp_path: Path) -> None:
        """Cross-check against the default ``attr="path"`` behaviour pinned in
        :class:`TestResolveKnowledgeRoot` above -- the whole point of the
        ``attr`` parameter is that it is the SAME resolution, applied to a
        different ``args`` attribute name, not a second behaviour that merely
        resembles the first.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        raw = Path("~/same-value")
        path_args = argparse.Namespace(path=raw)
        knowledge_root_args = argparse.Namespace(knowledge_root=raw)
        assert _resolve_knowledge_root(path_args) == _resolve_knowledge_root(
            knowledge_root_args, attr="knowledge_root"
        )


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
