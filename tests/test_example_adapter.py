# SPDX-License-Identifier: Apache-2.0
"""Tests for the synthetic minimal source adapter (issue #419).

Exercises ``examples/adapters/minimal_adapter.py`` against a scratch knowledge
root and asserts it honours the Lane-A raw-intake contract documented in
``docs/adapter-contract.md``: correct location, canonical filename, valid
frontmatter with declared provenance, path-safety, and discoverability by the
librarian's ``discover_raw_files``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from athenaeum import parse_frontmatter
from athenaeum.librarian import RAW_FILE_RE, discover_raw_files

_ADAPTER_PATH = (
    Path(__file__).resolve().parent.parent
    / "examples"
    / "adapters"
    / "minimal_adapter.py"
)


def _load_adapter():
    """Import the example module by path (it lives outside the package)."""
    spec = importlib.util.spec_from_file_location("minimal_adapter", _ADAPTER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_adapter_file_exists() -> None:
    assert _ADAPTER_PATH.is_file(), f"missing example adapter at {_ADAPTER_PATH}"


def test_write_raw_intake_lands_in_lane_a(tmp_path: Path) -> None:
    adapter = _load_adapter()
    written = adapter.write_raw_intake(
        tmp_path,
        source="press-releases",
        name="acme-widget-launch",
        description="Synthetic fact.",
        source_type="external",
        source_ref="https://example.com/press/widget-launch",
        body="# Acme Widget launch\n\nAcme announced the Widget on 2026-01-15.",
    )

    # Location convention: raw/<source>/<file>.
    assert written.parent == tmp_path / "raw" / "press-releases"
    # Canonical filename the librarian recognises.
    assert RAW_FILE_RE.match(written.name), written.name

    meta, body = parse_frontmatter(written.read_text(encoding="utf-8"))
    # Provenance is declared and cites the ultimate source, not the raw file.
    assert meta["source"] == "external:https://example.com/press/widget-launch"
    assert "raw/" not in str(meta["source"])
    assert meta["name"] == "acme-widget-launch"
    assert "Acme announced the Widget" in body


def test_written_file_is_discovered_by_librarian(tmp_path: Path) -> None:
    adapter = _load_adapter()
    adapter.write_raw_intake(
        tmp_path,
        source="notes",
        source_type="external",
        source_ref="https://example.com/a",
        body="A synthetic fact.",
    )

    found = discover_raw_files(tmp_path / "raw")
    assert len(found) == 1
    raw = found[0]
    assert raw.source == "notes"
    assert raw.timestamp and raw.uuid8  # parsed from the canonical filename


def test_repeated_writes_are_append_only(tmp_path: Path) -> None:
    """Two writes of the same fact produce two distinct files, never a clobber."""
    adapter = _load_adapter()
    first = adapter.write_raw_intake(
        tmp_path,
        source="notes",
        source_type="external",
        source_ref="https://example.com/a",
        body="Same fact.",
    )
    second = adapter.write_raw_intake(
        tmp_path,
        source="notes",
        source_type="external",
        source_ref="https://example.com/a",
        body="Same fact.",
    )
    assert first != second
    assert first.exists() and second.exists()
    assert len(discover_raw_files(tmp_path / "raw")) == 2


def test_empty_source_name_is_rejected(tmp_path: Path) -> None:
    adapter = _load_adapter()
    with pytest.raises(ValueError):
        adapter.write_raw_intake(
            tmp_path,
            source="///",  # sanitizes to empty
            source_type="external",
            source_ref="https://example.com/a",
            body="x",
        )


def test_main_writes_and_reports(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    adapter = _load_adapter()
    rc = adapter.main([str(tmp_path), "--source", "notes"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "wrote raw intake file:" in out
    files = list((tmp_path / "raw" / "notes").glob("*.md"))
    assert len(files) == 1
    assert RAW_FILE_RE.match(files[0].name)
