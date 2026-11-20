# SPDX-License-Identifier: Apache-2.0
"""Rights resolution at run start is logged, and held for the write guard (athenaeum#2933)."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest
import yaml

from athenaeum import librarian, store_rights
from athenaeum.librarian import run

ISS = "https://git.example.net"
DECL = {
    "version": 1,
    "store_id": "st_q7m2xk4vwz5d",
    "roles": {"editor": {"rights": ["read", "compile"], "members": [{"iss": ISS, "sub": "1"}]}},
}


def _root(tmp_path: Path, declaration: str | None) -> Path:
    root = tmp_path / "knowledge"
    (root / "raw").mkdir(parents=True)
    (root / "wiki").mkdir()
    (root / "athenaeum.yaml").write_text(
        yaml.safe_dump({"owner": {"asserter": {"iss": ISS, "sub": "1"}}}), encoding="utf-8"
    )
    if declaration is not None:
        (root / "wiki" / "_store").mkdir()
        (root / "wiki" / "_store" / "access.yaml").write_text(declaration, encoding="utf-8")
    for args in (
        ("init", "-b", "develop"),
        ("config", "user.email", "t@example.com"),
        ("config", "user.name", "T"),
        ("add", "-A"),
        ("commit", "-m", "seed", "--allow-empty"),
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return root


def _run(root: Path) -> int:
    return run(raw_root=root / "raw", wiki_root=root / "wiki", knowledge_root=root, merge_only=True)


def _tree(root: Path) -> list[str]:
    return sorted(
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file() and ".git" not in p.parts and "_store" not in p.parts
    )


def test_run_start_logs_resolution_for_a_declared_surface(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = _root(tmp_path, yaml.safe_dump(DECL))
    with caplog.at_level(logging.INFO, logger="athenaeum.store_rights"):
        _run(root)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("store rights:")]
    assert any(m.endswith(": compile,read") and "wiki" in m for m in lines), lines
    assert any(m.endswith(": no declaration") for m in lines), lines


def test_outcome_is_identical_with_none_valid_and_invalid_declaration(tmp_path: Path) -> None:
    results = []
    for name, decl in (
        ("none", None),
        ("valid", yaml.safe_dump(DECL)),
        ("invalid", "version: 99\n"),
        ("garbage", "{{{ not yaml"),
    ):
        root = _root(tmp_path / name, decl)
        results.append((_run(root), _tree(root)))
    assert all(r == results[0] for r in results), results


def test_a_resolution_failure_is_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    root = _root(tmp_path / "ok", None)
    baseline = _run(root)

    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("directory exploded")

    monkeypatch.setattr(store_rights, "resolve_surface_rights", boom)
    root2 = _root(tmp_path / "boom", yaml.safe_dump(DECL))
    with caplog.at_level(logging.WARNING):
        assert _run(root2) == baseline
    assert any("resolution failed" in r.getMessage() for r in caplog.records)


def test_recall_path_never_touches_store_rights() -> None:
    import athenaeum.mcp_server as mcp_server
    import athenaeum.search as search

    for mod in (search, mcp_server):
        assert "store_rights" not in Path(mod.__file__).read_text(encoding="utf-8")
    assert librarian is not None
