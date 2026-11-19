# SPDX-License-Identifier: Apache-2.0
"""Scaffold a new knowledge directory: standard subdirs, schema files, git init.

Contract: :func:`init_knowledge_dir` is the ONE place that creates a fresh
``<knowledge_root>`` from nothing — subdirectory tree, bundled ``wiki/_schema``
files, the master index, a default ``athenaeum.yaml`` (via
:mod:`athenaeum.config`), and an initial git commit. It is idempotent: safe to
call again on an existing root without overwriting or losing data.
:func:`copy_templates` is a separate, opt-in step (``--with-templates``) that
copies user-facing entity-authoring scaffolds — distinct from the LLM-tier
``wiki/_schema`` files this module also ships.

Factoring rule: this module owns ONLY one-time scaffolding of a new/blank
root. It must not gain per-run pipeline logic (that is
:mod:`athenaeum.librarian`) or config *semantics* (defaults/resolution live in
:mod:`athenaeum.config`; this module only calls
:func:`athenaeum.config.write_default_config`).

Layering: L1 (data model — operates on the on-disk knowledge-root shape).
Imports stdlib plus a lazy, function-local import of
:mod:`athenaeum.config` (L2) to avoid a module-level L1→L2 cycle at import
time.

Non-obvious invariants: a missing git identity is handled by setting a
repo-local ``user.name``/``user.email`` on the new knowledge repo (never
global) and saying so; any other failure becomes a clear ``SystemExit`` rather
than a raw ``CalledProcessError``. A failed init rolls back exactly what it
created, never pre-existing content — a first-run UX concern.
"""

from __future__ import annotations

import importlib.resources
import shutil
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

# Entity-author templates shipped with the wheel. Copied into
# `<knowledge_root>/templates/` only when the user passes
# `athenaeum init --with-templates`. These are user-facing scaffolds
# (one per common entity type) — distinct from the LLM-tier `schema/`
# files above, which describe the wiki contract for compilation.
_TEMPLATE_FILES = [
    "person.md",
    "company.md",
    "project.md",
    "concept.md",
    "source.md",
]

# Example shape rules shipped with the wheel (issue athenaeum#901). Copied
# into `<knowledge_root>/rules/` only when the user passes
# `athenaeum init --with-rules` — the shape-rule engine
# (:mod:`athenaeum.rules`) never loads from this subpackage directly and
# never ships a rule as an engine default; see
# `athenaeum.rule_examples`'s module docstring.
_RULE_EXAMPLE_FILES = [
    "contact-bounce.yaml",
    # Issue athenaeum#902: the default contact-sync ruleset. Packaged EXAMPLES the
    # installer copies in, never engine defaults -- both ship `mode: observe`.
    "contact-sync-email-removal.yaml",
    "contact-sync-skip.yaml",
    "unrecognized-export-fallthrough.yaml",
]

# Standard subdirectories created during init
_SUBDIRS = [
    "raw",
    "raw/sessions",
    # Default auto-memory intake root (mirrors config.py's default
    # recall.extra_intake_roots) — created up front so the first
    # `athenaeum run` on a fresh root doesn't warn
    # "extra_intake_root not found".
    "raw/auto-memory",
    # Issue athenaeum#2313: the `remember` MCP tool's own write target
    # (default `source="claude-session"`). Also a `recall.extra_intake_roots`
    # default entry — created up front, same rationale as `raw/auto-memory`
    # above, so a long-running `athenaeum serve` process resolves it into
    # its extra-roots list from first start rather than only after the
    # directory happens to be created by a later `remember` call.
    "raw/claude-session",
    # Issue athenaeum#2385: the same write target for a server started with
    # `--transport http`, where the caller is not known to be Claude Code.
    # Also a `recall.extra_intake_roots` default entry, and created up front
    # for the same reason as `raw/claude-session` above.
    "raw/remote-session",
    "wiki",
    "wiki/_schema",
]

_INDEX_CONTENT = """\
---
title: Master Index
---

<!-- This file is the master index for the knowledge wiki. -->
"""


def _is_identity_error(exc: subprocess.CalledProcessError) -> bool:
    stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
    return "user.name" in stderr or "user.email" in stderr or exc.returncode == 128


def _git(cmd: list[str], cwd: Path) -> None:
    """Run a git command; turn a failure into a short ``SystemExit`` message."""
    try:
        subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        raise _git_failure(exc) from exc


def _git_failure(exc: subprocess.CalledProcessError) -> SystemExit:
    stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
    cmd = exc.cmd if isinstance(exc.cmd, str) else " ".join(str(c) for c in exc.cmd)
    detail = (stderr or "").strip() or f"exit status {exc.returncode}"
    return SystemExit(f"athenaeum init: `{cmd}` failed:\n{detail}")


def _rollback(root: Path, created: list[Path]) -> None:
    """Remove only what this call created, newest first; never pre-existing content."""

    for p in reversed(created):
        try:
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
        except OSError:
            pass


def init_knowledge_dir(path: Path) -> Path:
    """Create and initialize a knowledge directory at *path*.

    The function is **idempotent**: calling it twice on the same directory
    will not overwrite existing files or lose data.

    If git has no identity, a repo-local ``user.name``/``user.email`` is set on
    the new knowledge repo only (never ``--global``) and a notice is printed to
    stderr. If init fails for any reason, everything this call created is
    removed (a pre-existing directory's content is never touched), so a retry
    starts clean.

    Returns the resolved *path* for convenience.
    """
    path = path.expanduser().resolve()
    # athenaeum#2778: every git call below is on this path, so detect a missing
    # binary BEFORE creating anything (no half-state, no FileNotFoundError).
    if not (path / ".git").exists() and shutil.which("git") is None:
        raise SystemExit(
            "athenaeum init: git is not installed or not on PATH. "
            "Install it (e.g. `apt-get install git`, `brew install git`) and retry."
        )
    created: list[Path] = []
    try:
        _scaffold(path, created)
    except BaseException:
        _rollback(path, created)
        raise
    return path


def _scaffold(path: Path, created: list[Path]) -> None:
    """Do the init work, appending every path this call creates to *created*."""
    # 1. Create directory tree (record each missing ancestor we create)
    for subdir in _SUBDIRS:
        target = path / subdir
        missing = []
        probe = target
        while not probe.exists():
            missing.append(probe)
            probe = probe.parent
        target.mkdir(parents=True, exist_ok=True)
        created.extend(reversed(missing))

    # 2. Copy bundled schema files (skip if already present)
    schema_dest = path / "wiki" / "_schema"
    schema_pkg = importlib.resources.files("athenaeum.schema")
    for fname in _SCHEMA_FILES:
        dest = schema_dest / fname
        if dest.exists():
            continue
        source = schema_pkg / fname
        dest.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        created.append(dest)

    # 3. Create master index (skip if already present)
    index_file = path / "wiki" / "_index.md"
    if not index_file.exists():
        index_file.write_text(_INDEX_CONTENT, encoding="utf-8")
        created.append(index_file)

    # 4. Create default config (skip if already present)
    from athenaeum.config import write_default_config

    before = {p for p in path.iterdir()}
    write_default_config(path)
    created.extend(p for p in path.iterdir() if p not in before)

    # 5. Initialize git repo and create initial commit
    git_dir = path / ".git"
    if not git_dir.exists():
        created.append(git_dir)
        _git(["git", "init"], path)
        _git(["git", "add", "."], path)
        commit = ["git", "commit", "-m", "Initialize knowledge directory"]
        try:
            subprocess.run(commit, cwd=path, check=True, capture_output=True)
        except subprocess.CalledProcessError as exc:
            if not _is_identity_error(exc):
                raise _git_failure(exc) from exc
            # No git identity anywhere: set a REPO-LOCAL one on this new repo
            # only, so a fresh machine needs no unlisted prerequisite.
            import sys

            name, email = "Athenaeum", "athenaeum@localhost"
            try:
                for key, val in (("user.name", name), ("user.email", email)):
                    subprocess.run(
                        ["git", "config", "--local", key, val],
                        cwd=path,
                        check=True,
                        capture_output=True,
                    )
                subprocess.run(commit, cwd=path, check=True, capture_output=True)
            except subprocess.CalledProcessError as exc2:
                raise SystemExit(
                    "Git could not create the initial commit even after setting "
                    "a repo-local identity. Nothing was left behind; fix git "
                    "and retry: athenaeum init"
                ) from exc2
            print(
                "No git identity is configured, so a repo-local one was set on "
                f"the knowledge repo only ({name} <{email}>). Change it with:\n"
                f"  git -C {path} config user.name 'Your Name'\n"
                f"  git -C {path} config user.email 'you@example.com'",
                file=sys.stderr,
            )


def copy_templates(dest: Path, *, force: bool = False) -> tuple[list[str], list[str]]:
    """Copy bundled entity templates into *dest*.

    Returns ``(written, skipped)`` lists of filenames. Without ``force``,
    pre-existing destination files are skipped (not overwritten). With
    ``force``, existing files are overwritten.
    """
    dest = dest.expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)
    template_pkg = importlib.resources.files("athenaeum.templates")
    written: list[str] = []
    skipped: list[str] = []
    for fname in _TEMPLATE_FILES:
        target = dest / fname
        if target.exists() and not force:
            skipped.append(fname)
            continue
        source = template_pkg / fname
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        written.append(fname)
    return written, skipped


def copy_example_rules(dest: Path, *, force: bool = False) -> tuple[list[str], list[str]]:
    """Copy bundled EXAMPLE shape rules into *dest* (issue athenaeum#901).

    Returns ``(written, skipped)`` lists of filenames. Without ``force``,
    pre-existing destination files are skipped (not overwritten). With
    ``force``, existing files are overwritten. Mirrors :func:`copy_templates`
    exactly — same opt-in-copy shape, same idempotence contract. Every
    copied example ships ``mode: observe`` (`docs/design/shape-rules.md` §5); this
    function only copies bytes, it never activates anything.
    """
    dest = dest.expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)
    rules_pkg = importlib.resources.files("athenaeum.rule_examples")
    written: list[str] = []
    skipped: list[str] = []
    for fname in _RULE_EXAMPLE_FILES:
        target = dest / fname
        if target.exists() and not force:
            skipped.append(fname)
            continue
        source = rules_pkg / fname
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        written.append(fname)
    return written, skipped
