# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#2765 -- ``doctor``'s ``search_index`` on an ``[mcp]``-only
install, and the README's claim about what ``doctor`` shows after the quick
start.

chromadb absence is simulated in-test (``importlib.util.find_spec``), so these
run the same in CI whether or not the ``[vector]`` extra is installed.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from tests.test_doctor import _run_healthy, healthy_install  # noqa: F401

_README = Path(__file__).resolve().parent.parent / "README.md"


@pytest.fixture
def no_chromadb(monkeypatch: pytest.MonkeyPatch) -> None:
    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *a, **k: None if name == "chromadb" else real(name, *a, **k),
    )


def _vector_config(knowledge_root: Path) -> None:
    (knowledge_root / "athenaeum.yaml").write_text("search_backend: vector\n", encoding="utf-8")


def test_vector_backend_without_chromadb_accepts_fts5_index(
    healthy_install: dict, no_chromadb: None  # noqa: F811
) -> None:
    _vector_config(healthy_install["knowledge_root"])
    result = next(r for r in _run_healthy(healthy_install) if r.name == "search_index")
    assert result.status == "ok"
    assert "fts5" in result.detail
    assert "[vector]" in result.detail


def test_vector_backend_without_chromadb_and_no_index_is_pending(
    healthy_install: dict, no_chromadb: None  # noqa: F811
) -> None:
    _vector_config(healthy_install["knowledge_root"])
    (healthy_install["cache_dir"] / "wiki-index.db").unlink()
    result = next(r for r in _run_healthy(healthy_install) if r.name == "search_index")
    assert result.status == "warn"


def test_readme_doctor_claim_matches_a_stock_install(
    healthy_install: dict, no_chromadb: None  # noqa: F811
) -> None:
    """A stock quick-start state: ``[mcp]``-only (no chromadb), default
    ``vector`` config, MCP registered, ``session-end`` has built the fts5
    index, no hook wired. The README must say what doctor then shows."""
    _vector_config(healthy_install["knowledge_root"])
    settings = healthy_install["claude_dir"] / "settings.json"
    settings.unlink()  # the quick start wires no hook

    results = _run_healthy(healthy_install)
    failing = {r.name for r in results if r.status == "warn"}
    assert failing == {"hooks"}
    assert not [r for r in results if r.status == "fail"]

    readme = _README.read_text(encoding="utf-8")
    para = re.search(r"Run it again\s+once you have finished the quick start.*?\n\n", readme, re.S)
    assert para is not None, "README doctor paragraph moved; update this pin"
    text = " ".join(para.group(0).split())
    assert "come back clean" not in text
    for name in failing:
        assert f"`{name}` stays a `WARN`" in text
    for ok_name in ("mcp", "search_index"):
        assert ok_name in text
        assert next(r for r in results if r.name == ok_name).status == "ok"
