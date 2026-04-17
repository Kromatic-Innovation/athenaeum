"""Tests for ``athenaeum init`` command and ``init_knowledge_dir``."""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.init import _INDEX_CONTENT, _SCHEMA_FILES, _SUBDIRS, init_knowledge_dir


def test_fresh_init_creates_dirs(tmp_path: Path) -> None:
    """A fresh init creates all expected subdirectories."""
    target = tmp_path / "knowledge"
    init_knowledge_dir(target)

    for subdir in _SUBDIRS:
        assert (target / subdir).is_dir(), f"missing subdir: {subdir}"


def test_fresh_init_creates_schema_files(tmp_path: Path) -> None:
    """Schema files are copied into wiki/_schema/."""
    target = tmp_path / "knowledge"
    init_knowledge_dir(target)

    schema_dir = target / "wiki" / "_schema"
    for fname in _SCHEMA_FILES:
        f = schema_dir / fname
        assert f.is_file(), f"missing schema file: {fname}"
        assert f.read_text(encoding="utf-8").strip(), f"schema file is empty: {fname}"


def test_fresh_init_creates_index(tmp_path: Path) -> None:
    """wiki/_index.md is created with default content."""
    target = tmp_path / "knowledge"
    init_knowledge_dir(target)

    index = target / "wiki" / "_index.md"
    assert index.is_file()
    assert index.read_text(encoding="utf-8") == _INDEX_CONTENT


def test_fresh_init_creates_git_repo(tmp_path: Path) -> None:
    """A git repo is initialized with an initial commit."""
    target = tmp_path / "knowledge"
    init_knowledge_dir(target)

    assert (target / ".git").is_dir()

    import subprocess

    result = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=target,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Initialize knowledge directory" in result.stdout


def test_idempotent_preserves_content(tmp_path: Path) -> None:
    """Running init twice does not overwrite user content."""
    target = tmp_path / "knowledge"
    init_knowledge_dir(target)

    # Add custom content to existing files
    custom_index = "# My Custom Index\n"
    (target / "wiki" / "_index.md").write_text(custom_index, encoding="utf-8")

    custom_schema = "# Custom types\n"
    (target / "wiki" / "_schema" / "types.md").write_text(custom_schema, encoding="utf-8")

    # Add a user file in raw/
    (target / "raw" / "my-notes.md").write_text("my notes\n", encoding="utf-8")

    # Run init again
    init_knowledge_dir(target)

    # Verify nothing was overwritten
    assert (target / "wiki" / "_index.md").read_text(encoding="utf-8") == custom_index
    assert (target / "wiki" / "_schema" / "types.md").read_text(encoding="utf-8") == custom_schema
    assert (target / "raw" / "my-notes.md").read_text(encoding="utf-8") == "my notes\n"


def test_custom_path(tmp_path: Path) -> None:
    """--path flag directs init to a custom location."""
    custom = tmp_path / "my" / "custom" / "dir"
    result = init_knowledge_dir(custom)

    assert result == custom.resolve()
    assert (custom / "wiki" / "_schema").is_dir()
    assert (custom / "raw" / "sessions").is_dir()


def test_cli_init_default(tmp_path: Path, monkeypatch) -> None:
    """CLI ``athenaeum init --path <dir>`` creates a knowledge dir."""
    from athenaeum.cli import main

    target = tmp_path / "knowledge"
    exit_code = main(["init", "--path", str(target)])

    assert exit_code == 0
    assert (target / "wiki" / "_index.md").is_file()


def test_git_identity_error_gives_helpful_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #10: When git identity is not configured, init should raise
    SystemExit with a helpful message instead of a raw CalledProcessError."""
    import subprocess

    original_run = subprocess.run
    call_count = 0

    def mock_run(cmd, **kwargs):
        nonlocal call_count
        call_count += 1
        # Let git init and git add succeed; fail on git commit (3rd call)
        if call_count == 3:
            raise subprocess.CalledProcessError(
                128, cmd,
                stderr=b"Author identity unknown\n"
                b"*** Please tell me who you are.\n"
                b"  git config --global user.name ...\n"
                b"  git config --global user.email ...\n",
            )
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", mock_run)

    target = tmp_path / "knowledge"

    with pytest.raises(SystemExit, match="Git identity not configured"):
        init_knowledge_dir(target)


def test_schema_files_match_bundled(tmp_path: Path) -> None:
    """Copied schema files match the bundled originals byte-for-byte."""
    import importlib.resources

    target = tmp_path / "knowledge"
    init_knowledge_dir(target)

    schema_pkg = importlib.resources.files("athenaeum.schema")
    for fname in _SCHEMA_FILES:
        bundled = (schema_pkg / fname).read_text(encoding="utf-8")
        copied = (target / "wiki" / "_schema" / fname).read_text(encoding="utf-8")
        assert copied == bundled, f"schema mismatch: {fname}"
