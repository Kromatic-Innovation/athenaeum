# SPDX-License-Identifier: Apache-2.0
"""Cross-project Claude Code memory discovery, metadata-only (issue athenaeum#2316).

Before this module, athenaeum had no routine that looked across
``~/.claude/projects/*/memory/`` for Claude Code memory files a user already
has on disk — only per-scope ``MEMORY.md`` index maintenance
(:mod:`athenaeum.memory_index`) for scopes an operator had *already*
symlinked into ``raw/auto-memory/`` (the 2026-11-05 kickback's finding on
this issue). This module is the discovery routine that finding said was
missing: it walks every project directory Claude Code itself writes memory
into and reports what is there, in counts and bytes, **without reading any
file's content**.

That "metadata only" property is load-bearing, not an optimization: AC1/AC5
of athenaeum#2316 require that nothing is read from a user's existing memory
until they explicitly agree to the compile, and discovery runs *before* that
agreement (it is what the agreement prompt is built from). Every function
here calls at most :meth:`pathlib.Path.stat` on a candidate file — never
``.read_text()``/``.read_bytes()``/``open()``. :func:`estimate_cost` prices
the discovered byte counts with a documented rough heuristic for exactly the
same reason (ruling 2 on the issue: "sized from file metadata only, before
any memory content is read").

**Where "Claude Code memory" lives.** A project's memory is
``~/.claude/projects/<scope>/memory/*.md`` — the same per-project directory
:mod:`athenaeum.subject_population` already describes as "outside the
``~/knowledge`` git working tree", and the same root
:mod:`athenaeum.transcript_verify` resolves as :func:`default_projects_root`
(honoring ``CLAUDE_CONFIG_DIR``). This module reuses that resolver rather
than hard-coding a second ``~/.claude`` constant, so a custom
``CLAUDE_CONFIG_DIR`` is honored here exactly as it already is for transcript
verification.

**Layering:** L1 (data model — reads on-disk shape/metadata only). Module
scope imports only :mod:`athenaeum.transcript_verify` (L1) plus stdlib;
:func:`estimate_cost` takes a function-local import of
:mod:`athenaeum.models` (also L1) for its blended-rate dollar estimate, so no
caller pays for that import unless it actually asks for a dollar figure.
Never imports :mod:`athenaeum.config`, :mod:`athenaeum.provider`, or any L2+
module — a caller passes the resolved provider string in, this module never
resolves one itself.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

#: Rough, deliberately conservative bytes-per-token ratio for an UNREAD
#: markdown memory file (issue athenaeum#2316 ruling 2). English markdown
#: prose averages well under 4 bytes/token for most tokenizers, so this
#: errs toward OVER-estimating tokens (and therefore cost) rather than
#: under-estimating it — the safe direction for a figure shown to a user
#: immediately before they agree to spend their plan quota. Explicitly a
#: rough figure until athenaeum#2323 measures real usage against a live
#: compile.
BYTES_PER_TOKEN_ESTIMATE = 4


@dataclass(frozen=True)
class MemoryFile:
    """One discovered memory file's path and METADATA — never its content."""

    path: Path
    size_bytes: int
    mtime: float


@dataclass(frozen=True)
class ProjectMemory:
    """One project's ``memory/`` directory and the files discovered in it."""

    scope: str
    memory_dir: Path
    files: tuple[MemoryFile, ...]

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def total_bytes(self) -> int:
        return sum(f.size_bytes for f in self.files)


def discover_project_memories(projects_root: Path | None = None) -> list[ProjectMemory]:
    """Discover every ``<projects_root>/<scope>/memory/*.md`` file, metadata only.

    Returns one :class:`ProjectMemory` per scope directory that has a
    ``memory/`` subdirectory containing at least one ``.md`` file, sorted by
    scope name for deterministic output (the picker and every cost-estimate
    rendering depend on a stable order). A scope with no ``memory/``
    subdirectory, or an empty one, is omitted entirely rather than reported
    with a zero count — there is nothing for the opt-in prompt to offer.

    *projects_root* defaults to
    :func:`athenaeum.transcript_verify.default_projects_root` (honors
    ``CLAUDE_CONFIG_DIR``); tests and the ``--projects-root`` CLI flag inject
    a synthetic tree so this never touches a real operator's filesystem
    unasked.

    Every unreadable entry (a permission error, a symlink that no longer
    resolves, a directory removed between glob and stat) is skipped rather
    than raising — discovery is advisory and best-effort, like every other
    filesystem walk in this codebase that scans operator-controlled
    directories outside athenaeum's own tree.
    """
    from athenaeum.transcript_verify import default_projects_root

    root = (projects_root or default_projects_root()).expanduser()
    try:
        scope_dirs = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return []

    results: list[ProjectMemory] = []
    for scope_dir in scope_dirs:
        memory_dir = scope_dir / "memory"
        if not memory_dir.is_dir():
            continue
        try:
            md_paths = sorted(memory_dir.glob("*.md"))
        except OSError:
            continue
        files: list[MemoryFile] = []
        for md_path in md_paths:
            try:
                st = md_path.stat()
            except OSError:
                continue
            files.append(MemoryFile(path=md_path, size_bytes=st.st_size, mtime=st.st_mtime))
        if not files:
            continue
        results.append(
            ProjectMemory(scope=scope_dir.name, memory_dir=memory_dir, files=tuple(files))
        )
    results.sort(key=lambda p: p.scope)
    return results


@dataclass(frozen=True)
class CostEstimate:
    """A pre-read cost estimate (issue athenaeum#2316 ruling 2).

    ``estimated_cost_usd`` is ``None`` on the ``claude-cli`` (subscription)
    path — mirroring :mod:`athenaeum.spend`'s provider split exactly: a
    subscription figure is reported in tokens only, never blended into a
    dollar total. It is populated only when *provider* is the metered
    ``api`` path.
    """

    file_count: int
    project_count: int
    estimated_tokens: int
    estimated_cost_usd: float | None


def estimate_cost(
    files: Iterable[MemoryFile], *, project_count: int, provider: str
) -> CostEstimate:
    """Estimate the cost of compiling *files*, from metadata already in hand.

    Never touches the filesystem — *files* is whatever the caller already
    discovered (typically the PENDING subset after a resumed run's state
    exclusion). *provider* is an :func:`athenaeum.provider.resolve_provider`
    value: ``"claude-cli"`` reports tokens only (``estimated_cost_usd=None``,
    the subscription path); anything else (the metered ``api`` path) also
    reports a dollar figure, priced at the ACTIVE blended fallback rate
    (:func:`athenaeum.models.cost_for_token_bucket` with no model tag — the
    honest choice here, since no model is chosen yet at discovery time).
    """
    files = list(files)
    total_bytes = sum(f.size_bytes for f in files)
    tokens = total_bytes // BYTES_PER_TOKEN_ESTIMATE
    cost_usd: float | None = None
    if provider != "claude-cli":
        from athenaeum.models import cost_for_token_bucket

        cost_usd = round(cost_for_token_bucket(None, {"input": tokens}), 2)
    return CostEstimate(
        file_count=len(files),
        project_count=project_count,
        estimated_tokens=tokens,
        estimated_cost_usd=cost_usd,
    )
