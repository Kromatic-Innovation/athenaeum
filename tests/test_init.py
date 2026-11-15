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
    (target / "wiki" / "_schema" / "types.md").write_text(
        custom_schema, encoding="utf-8"
    )

    # Add a user file in raw/
    (target / "raw" / "my-notes.md").write_text("my notes\n", encoding="utf-8")

    # Run init again
    init_knowledge_dir(target)

    # Verify nothing was overwritten
    assert (target / "wiki" / "_index.md").read_text(encoding="utf-8") == custom_index
    assert (target / "wiki" / "_schema" / "types.md").read_text(
        encoding="utf-8"
    ) == custom_schema
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


def _no_git_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermetically simulate a machine with no git identity."""
    empty = tmp_path / "empty-gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    # Never auto-guess an identity from the host's username/hostname.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.useConfigOnly")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    monkeypatch.setenv("EMAIL", "")
    for var in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    ):
        monkeypatch.delenv(var, raising=False)


def test_no_git_identity_sets_repo_local_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """athenaeum#2593: no identity -> repo-local one is set, init succeeds."""
    import subprocess

    _no_git_identity(tmp_path, monkeypatch)
    target = tmp_path / "knowledge"
    init_knowledge_dir(target)

    assert (target / "wiki" / "_index.md").is_file()
    local = subprocess.run(
        ["git", "config", "--local", "user.email"],
        cwd=target,
        capture_output=True,
        text=True,
    )
    assert local.stdout.strip()
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=target, capture_output=True, text=True
    )
    assert "Initialize knowledge directory" in log.stdout
    # Global config untouched; user told what happened and how to change it.
    assert Path(__import__("os").environ["GIT_CONFIG_GLOBAL"]).read_text() == ""
    err = capsys.readouterr().err
    assert "repo-local" in err and "config user.name" in err


def test_failed_init_leaves_no_partial_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """athenaeum#2593: a failure after scaffolding rolls back a fresh dir."""
    import subprocess

    original_run = subprocess.run

    def mock_run(cmd, **kwargs):
        if cmd[:2] == ["git", "commit"] or cmd[:3] == ["git", "config", "--local"]:
            raise subprocess.CalledProcessError(128, cmd, stderr=b"user.name unknown")
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", mock_run)
    (tmp_path / "fresh").mkdir()
    target = tmp_path / "fresh" / "knowledge"
    with pytest.raises(SystemExit, match="Nothing was left behind"):
        init_knowledge_dir(target)
    assert not target.exists()
    assert (tmp_path / "fresh").exists()  # only what init created is removed


def test_failed_init_preserves_preexisting_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """athenaeum#2593: rollback never touches a pre-existing dir's content."""
    import subprocess

    original_run = subprocess.run

    def mock_run(cmd, **kwargs):
        if cmd[:2] == ["git", "commit"]:
            raise subprocess.CalledProcessError(1, cmd, stderr=b"boom")
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", mock_run)
    target = tmp_path / "knowledge"
    (target / "raw").mkdir(parents=True)
    (target / "raw" / "keep.md").write_text("mine")
    (target / "notes.txt").write_text("mine")
    with pytest.raises(subprocess.CalledProcessError):
        init_knowledge_dir(target)
    assert (target / "raw" / "keep.md").read_text() == "mine"
    assert (target / "notes.txt").read_text() == "mine"
    assert not (target / "wiki").exists()
    assert not (target / ".git").exists()
    assert not (target / "athenaeum.yaml").exists()


def test_force_help_mentions_no_backup(capsys: pytest.CaptureFixture[str]) -> None:
    """`--force` help text should warn that no backup is created (athenaeum#105)."""
    from athenaeum.cli import main

    with pytest.raises(SystemExit):
        main(["init", "--help"])
    captured = capsys.readouterr()
    assert "backup" in captured.out.lower()


def test_templates_dest_without_with_templates_warns(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--templates-dest` without `--with-templates` must emit a stderr
    warning so the user notices the flag was silently ignored (athenaeum#105)."""
    from athenaeum.cli import main

    target = tmp_path / "knowledge"
    custom_dest = tmp_path / "elsewhere"
    exit_code = main(
        [
            "init",
            "--path",
            str(target),
            "--templates-dest",
            str(custom_dest),
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "warning" in captured.err.lower()
    assert "--templates-dest" in captured.err
    assert "--with-templates" in captured.err
    # Templates were not actually copied.
    assert not custom_dest.exists()


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


def test_fresh_init_creates_auto_memory_intake_root(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """init creates raw/auto-memory so the first run doesn't warn.

    ``resolve_extra_intake_roots`` warns "extra_intake_root not found" for
    every configured-but-missing root; the default config points at
    ``raw/auto-memory``, so a freshly-inited knowledge dir used to warn on
    the very first ``athenaeum run`` (v0.7.3 release-gate review).
    """
    import logging

    from athenaeum.config import resolve_extra_intake_roots

    target = tmp_path / "knowledge"
    init_knowledge_dir(target)

    auto_memory = target / "raw" / "auto-memory"
    assert auto_memory.is_dir()

    with caplog.at_level(logging.WARNING):
        roots = resolve_extra_intake_roots(target)
    assert auto_memory.resolve() in roots
    assert not [
        r for r in caplog.records if "extra_intake_root not found" in r.getMessage()
    ]


def test_fresh_init_creates_claude_session_intake_root(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """init creates raw/claude-session so the first run doesn't warn (issue
    athenaeum#2313).

    Same rationale as ``test_fresh_init_creates_auto_memory_intake_root``
    above: the default config now also points at ``raw/claude-session`` (the
    ``remember`` MCP tool's write target), so a freshly-inited knowledge dir
    must not warn "extra_intake_root not found" for it, and a long-running
    ``athenaeum serve`` process started right after ``init`` resolves it into
    its extra-roots list from the first call rather than only after the
    directory happens to be created by a later ``remember`` call.
    """
    import logging

    from athenaeum.config import resolve_extra_intake_roots

    target = tmp_path / "knowledge"
    init_knowledge_dir(target)

    claude_session = target / "raw" / "claude-session"
    assert claude_session.is_dir()

    with caplog.at_level(logging.WARNING):
        roots = resolve_extra_intake_roots(target)
    assert claude_session.resolve() in roots
    assert not [
        r for r in caplog.records if "extra_intake_root not found" in r.getMessage()
    ]
