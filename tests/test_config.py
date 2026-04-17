"""Tests for the athenaeum config module."""

from __future__ import annotations

from pathlib import Path

from athenaeum.config import load_config, write_default_config


class TestLoadConfig:
    def test_defaults_when_no_file(self, tmp_path: Path) -> None:
        cfg = load_config(tmp_path)
        assert cfg["auto_recall"] is True
        assert cfg["search_backend"] == "fts5"
        assert cfg["vector"]["provider"] == "chromadb"

    def test_reads_yaml(self, tmp_path: Path) -> None:
        (tmp_path / "athenaeum.yaml").write_text(
            "auto_recall: false\nsearch_backend: vector\n"
        )
        cfg = load_config(tmp_path)
        assert cfg["auto_recall"] is False
        assert cfg["search_backend"] == "vector"

    def test_partial_override(self, tmp_path: Path) -> None:
        (tmp_path / "athenaeum.yaml").write_text("search_backend: vector\n")
        cfg = load_config(tmp_path)
        assert cfg["auto_recall"] is True  # default preserved
        assert cfg["search_backend"] == "vector"

    def test_vector_nested_merge(self, tmp_path: Path) -> None:
        (tmp_path / "athenaeum.yaml").write_text(
            "vector:\n  provider: faiss\n"
        )
        cfg = load_config(tmp_path)
        assert cfg["vector"]["provider"] == "faiss"
        assert cfg["vector"]["collection"] == "wiki"  # default preserved

    def test_invalid_yaml_falls_back(self, tmp_path: Path) -> None:
        (tmp_path / "athenaeum.yaml").write_text("{{invalid yaml")
        cfg = load_config(tmp_path)
        assert cfg["auto_recall"] is True  # defaults

    def test_empty_file_falls_back(self, tmp_path: Path) -> None:
        (tmp_path / "athenaeum.yaml").write_text("")
        cfg = load_config(tmp_path)
        assert cfg["auto_recall"] is True


class TestWriteDefaultConfig:
    def test_creates_file(self, tmp_path: Path) -> None:
        path = write_default_config(tmp_path)
        assert path.exists()
        content = path.read_text()
        assert "auto_recall" in content
        assert "search_backend" in content

    def test_idempotent(self, tmp_path: Path) -> None:
        (tmp_path / "athenaeum.yaml").write_text("custom: true\n")
        write_default_config(tmp_path)
        assert "custom: true" in (tmp_path / "athenaeum.yaml").read_text()
