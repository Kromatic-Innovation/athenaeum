"""Initialize a new knowledge directory with standard structure and schema files."""

from __future__ import annotations

import importlib.resources
import subprocess
from pathlib import Path

# Schema files bundled in the package that get copied to wiki/_schema/
_SCHEMA_FILES = [
    "_entity-template.md",
    "access-levels.md",
    "observation-filter.md",
    "tags.md",
    "types.md",
]

# Standard subdirectories created during init
_SUBDIRS = [
    "raw",
    "raw/sessions",
    "wiki",
    "wiki/_schema",
]

_INDEX_CONTENT = """\
---
title: Master Index
---

<!-- This file is the master index for the knowledge wiki. -->
"""


def init_knowledge_dir(path: Path) -> Path:
    """Create and initialize a knowledge directory at *path*.

    The function is **idempotent**: calling it twice on the same directory
    will not overwrite existing files or lose data.

    Returns the resolved *path* for convenience.
    """
    path = path.expanduser().resolve()

    # 1. Create directory tree
    for subdir in _SUBDIRS:
        (path / subdir).mkdir(parents=True, exist_ok=True)

    # 2. Copy bundled schema files (skip if already present)
    schema_dest = path / "wiki" / "_schema"
    schema_pkg = importlib.resources.files("athenaeum.schema")
    for fname in _SCHEMA_FILES:
        dest = schema_dest / fname
        if dest.exists():
            continue
        source = schema_pkg / fname
        dest.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    # 3. Create master index (skip if already present)
    index_file = path / "wiki" / "_index.md"
    if not index_file.exists():
        index_file.write_text(_INDEX_CONTENT, encoding="utf-8")

    # 4. Initialize git repo and create initial commit
    git_dir = path / ".git"
    if not git_dir.exists():
        subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Initialize knowledge directory"],
            cwd=path,
            check=True,
            capture_output=True,
        )

    return path
