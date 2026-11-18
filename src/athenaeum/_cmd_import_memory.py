# SPDX-License-Identifier: Apache-2.0
"""``athenaeum import-memory`` — opt-in compile of existing Claude Code memory
files found across projects (issue athenaeum#2316).

**Design per the 2026-11-05 operator rulings on the issue** (it was kicked
back for ambiguity the same day, then re-scoped; the rulings answer the four
decisions the kickback named):

1. **Host command.** A new subcommand — this module — never ``athenaeum
   init``. :mod:`athenaeum.init`'s own factoring rule forbids it gaining
   per-run pipeline logic, and this command is exactly that: interactive,
   resumable, and LLM-spending. ``athenaeum init`` only *offers* it (one
   print line in :func:`athenaeum._cmd_lifecycle.cmd_init`) and never runs
   it.
2. **Cost estimate.** Matches the provider — tokens on ``claude-cli``,
   dollars added on ``api`` — computed before any file content is read, via
   :mod:`athenaeum.memory_discovery`. Per FILE, not per byte (athenaeum#2733:
   the athenaeum#2323 real-login run measured about 2,800 tokens per file over
   two classify calls, 38x the old bytes/4 figure), for the files THIS run
   will process after ``--max-files`` and the call cap; a capped run also
   names the total pending. Large-file scaling is unmeasured and the prompt
   says so.
3. **Run cap.** 20 files and 100 API calls per run (a file costs two calls,
   so the call cap also bounds the batch) (:data:`DEFAULT_MAX_FILES`
   / :data:`DEFAULT_MAX_CALLS`, matching athenaeum#2307's cap), raised via
   ``--max-files``/``--max-calls``. Resumable: a cap-stopped run's remaining
   work is reported, and a plain re-run picks it up (:func:`run_import`'s
   state-file skip set).
4. **Project choice.** An interactive picker (nothing selected by default)
   or the repeatable ``--project`` flag, which bypasses the picker entirely
   — the scripts/tests path.
5. **Verification.** This module makes real compile calls in production
   (:func:`_default_compile_batch` wraps :func:`athenaeum.librarian.run`,
   the SAME pipeline ``athenaeum ingest``/``athenaeum run`` use) but every
   test in this repo substitutes a fake via :func:`run_import`'s
   ``compile_fn`` parameter — no test here makes a real compile call.

**Off by default (AC1/AC5/AC6).** Discovery
(:func:`athenaeum.memory_discovery.discover_project_memories`) never reads a
file's content. :func:`select_projects` returns an EMPTY selection for both
the interactive picker (blank input) and an absent ``--project`` — nothing
preselected. :func:`run_import` only ever stages/reads files belonging to a
SELECTED project; an unchosen project's files are never touched — see
``tests/test_cmd_import_memory.py::TestUnchosenProjectsNeverRead`` for the
fixture-proven counter-example AC6 requires.

**Resumable, not duplicating (AC3/AC8).** :func:`load_state`/:func:`save_state`
persist a ``<cache_dir>/import_memory_state.json`` map of
``"<scope>/<filename>" -> {size_bytes, mtime, imported_at[, refused][, held]}``
(``refused`` holds the relevance-policy reason when the gate refused the file,
athenaeum#2645; ``held`` says the content reached a compiled page while the
librarian kept the raw file queued, athenaeum#2734). A file is
PENDING only when no recorded entry matches its CURRENT source size+mtime
(:func:`_pending_files`) — so a changed source file is treated as new again,
and an already-imported, unchanged one is skipped on every later run, cap or
no cap. Staging a file (copying it into
``<knowledge_root>/raw/auto-memory/<scope>/<filename>``, the SAME convention
an operator uses manually today — :mod:`athenaeum.memory_index`'s module
docstring) is content-stable: the destination path is a pure function of
scope+filename, so a retried stage is an idempotent overwrite, never a
second raw member for the same source. State entries are written ONLY when
:func:`_default_compile_batch` (or its test substitute) returns exit code
``0`` AND the staged file is actually gone afterward (see
:func:`run_import`'s post-compile reconciliation) — a crash, a raised
exception (the catch-all AC8 names), a nonzero/degraded exit, a run in which
EVERY classify call failed (:class:`_ClassifyTally`; the classify steps
fail open, so without this a missing login looked like a clean run,
athenaeum#2735), OR a file
intake silently dropped despite an overall exit ``0`` (``MEMORY.md``, a
name+frontmatter intake does not recognise, an ephemeral-classified file)
leaves that file PENDING again. Re-running is safe either way: re-staging
overwrites the same path,
and the underlying compile pipeline's own incremental/hash-stamped dedup
(the same one ``athenaeum ingest`` already relies on) makes recompiling an
already-fully-compiled raw member a no-op rather than a duplicate page.

**Login probe (athenaeum#2735).** Before any file is staged or read,
:func:`check_provider_usable` confirms the configured provider can run. On
``claude-cli`` that is the binary's presence
(:func:`athenaeum.provider.preflight_provider`) plus ``claude auth status``,
which reports the login locally and bills nothing. A missing login stops the
run with one line naming the fix; nothing is staged or marked imported.

**Held-but-compiled files (athenaeum#2734).** ``retire.py`` keeps a raw file
in the staging queue when its cluster is held (every multi-member cluster is,
until the comparator lane has a live caller, athenaeum#1946), so by presence
alone a held file looks "not compiled". But its content already reached a
``wiki/auto-*.md`` page, whose body carries a ``## From `<scope>/<file>```
header per member (a member whose paragraphs all duplicate an earlier
member's gets no header, so :class:`_CompiledPages` also accepts a file whose
every paragraph is on a page). Only pages written by THIS compile count. A
staged file that remains AND reached a page is recorded in the state file with
a ``held`` reason, so it is never restaged or reclassified. The state file is
the place (rather than the librarian) because "which source files has the user
asked import-memory to handle" is this command's own bookkeeping: the
librarian knows clusters, not import-memory's source files, and the comparator
hold stays exactly as it is.

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser``.

Layering: L5. Imports :mod:`athenaeum._cli_shared` (run-lock),
:mod:`athenaeum.atomic_io` (L0), :mod:`athenaeum.config` (L2),
:mod:`athenaeum.memory_discovery` (L1); :func:`_default_compile_batch` takes
a function-local import of :mod:`athenaeum.librarian` (L4) and
:func:`_cmd_import_memory` one of :mod:`athenaeum.provider` (L3), so neither
cost is paid by a caller that only wants the pure selection/state helpers.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from athenaeum._cli_shared import (
    _acquire_or_exit,
    _add_lock_args,
    _positive_int,
    _resolve_knowledge_root,
)
from athenaeum.atomic_io import atomic_write_text
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT, load_config, resolve_cache_dir
from athenaeum.memory_discovery import (
    CALLS_PER_FILE,
    CostEstimate,
    MemoryFile,
    ProjectMemory,
    discover_project_memories,
    estimate_cost,
)

if TYPE_CHECKING:
    from athenaeum.runlock import RunLock

#: Run cap (issue athenaeum#2316 ruling 3 — same cap as athenaeum#2307).
#: Raised via ``--max-files``/``--max-calls``.
DEFAULT_MAX_FILES = 20
DEFAULT_MAX_CALLS = 100

#: Resumable-state filename under the cache dir (mirrors
#: :mod:`athenaeum.batch_state`'s cache-dir-not-wiki-root placement: this
#: state is machine-local and mutable, never git-tracked corpus content).
STATE_FILENAME = "import_memory_state.json"

#: Exit code for "the run did not complete cleanly; nothing in this batch
#: was marked imported" (AC8). Distinct from the generic argparse/usage
#: error (1) only in spirit — both are plain ``1`` today; kept as a named
#: constant so a future split (mirroring EXIT_LOCK_HELD's own history) has
#: one call site to change.
EXIT_INCOMPLETE = 1

#: Seconds allowed for ``claude auth status`` (a local read, no model call).
_LOGIN_PROBE_TIMEOUT = 20

_CLASSIFY_LOGGERS = ("athenaeum.claim_kind", "athenaeum.decay_bucket")


def add_import_memory_subparser(subparsers: "argparse._SubParsersAction") -> None:
    """Register ``athenaeum import-memory`` and its flags on *subparsers*."""
    parser = subparsers.add_parser(
        "import-memory",
        help="Opt-in: discover existing Claude Code memory across projects and "
        "offer to compile it (issue athenaeum#2316). Off by default — nothing is "
        "read without an explicit yes, and only the projects you select.",
    )
    parser.add_argument(
        "--path",
        "--knowledge-root",
        dest="path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge).",
    )
    parser.add_argument(
        "--project",
        dest="projects",
        action="append",
        default=None,
        metavar="SCOPE",
        help="Select a discovered project by its scope name (the directory "
        "name under the Claude Code projects root). Repeatable. Given at "
        "least once, this bypasses the interactive picker entirely — the "
        "scripts/tests path (issue athenaeum#2316 ruling 4). Omitted: an "
        "interactive picker runs, with nothing selected by default.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the 'proceed?' confirmation after the cost/cap summary "
        "prints. Does NOT select any project by itself — selection still "
        "requires --project or an interactive pick.",
    )
    parser.add_argument(
        "--max-files",
        dest="max_files",
        type=_positive_int,
        default=DEFAULT_MAX_FILES,
        metavar="N",
        help=f"Cap files compiled this run (default {DEFAULT_MAX_FILES}, issue "
        "athenaeum#2316 ruling 3 — same cap as athenaeum#2307). Raise it for a "
        "larger batch; re-running with no flag change resumes where the cap "
        "stopped.",
    )
    parser.add_argument(
        "--max-calls",
        dest="max_calls",
        type=_positive_int,
        default=DEFAULT_MAX_CALLS,
        metavar="N",
        help=f"Cap API calls this run (default {DEFAULT_MAX_CALLS}, same ruling "
        "as --max-files).",
    )
    parser.add_argument(
        "--projects-root",
        dest="projects_root",
        type=Path,
        default=None,
        metavar="DIR",
        help="Override the Claude Code projects root to scan (default: "
        "~/.claude/projects, honoring CLAUDE_CONFIG_DIR). For tests/scripts.",
    )
    parser.add_argument(
        "--cache-dir",
        dest="cache_dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="Cache directory holding the resumable import-memory state "
        "(default: ATHENAEUM_CACHE_DIR env, else ~/.cache/athenaeum).",
    )
    _add_lock_args(parser)
    parser.set_defaults(func=_cmd_import_memory)


# ---------------------------------------------------------------------------
# Pure selection logic (no I/O beyond the injected picker)
# ---------------------------------------------------------------------------


def _interactive_picker(discovered: list[ProjectMemory]) -> list[str]:
    """Default picker: list discovered projects, read a comma-separated pick.

    Blank input (including EOF/empty stdin) selects nothing — the safe
    default (AC4). Never called when ``--project`` was given at least once.
    """
    if not discovered:
        print("No Claude Code memory discovered in any project.")
        return []
    print("Discovered Claude Code memory in these projects:")
    for i, project in enumerate(discovered, start=1):
        print(f"  [{i}] {project.scope}  ({project.file_count} files)")
    print("Nothing is selected by default.")
    try:
        raw = input("Select projects to import (comma-separated numbers, blank = none): ")
    except EOFError:
        raw = ""
    chosen: list[str] = []
    for token in raw.strip().split(","):
        token = token.strip()
        if not token.isdigit():
            continue
        idx = int(token)
        if 1 <= idx <= len(discovered):
            chosen.append(discovered[idx - 1].scope)
    return chosen


def select_projects(
    discovered: list[ProjectMemory],
    cli_projects: list[str] | None,
    *,
    picker: Callable[[list[ProjectMemory]], list[str]] | None = None,
) -> tuple[list[ProjectMemory], list[str]]:
    """Resolve the selected subset of *discovered* (AC4).

    Returns ``(selected, unknown_names)``. ``--project`` (*cli_projects*),
    when given at least once, is authoritative and skips the interactive
    picker entirely; any name that does not match a discovered scope is
    reported in *unknown_names* rather than silently ignored. With
    *cli_projects* empty/``None``, *picker* (default
    :func:`_interactive_picker`) is called with the full discovered list and
    decides the selection — an empty return means nothing is selected,
    which is the correct outcome for blank input, not an error.
    """
    by_scope = {p.scope: p for p in discovered}
    if cli_projects:
        selected: list[ProjectMemory] = []
        unknown: list[str] = []
        for name in cli_projects:
            if name in by_scope:
                selected.append(by_scope[name])
            else:
                unknown.append(name)
        return selected, unknown
    chosen_names = (picker or _interactive_picker)(discovered)
    selected = [by_scope[name] for name in chosen_names if name in by_scope]
    return selected, []


def _confirm(prompt_fn: Callable[[str], str] = input) -> bool:
    try:
        answer = prompt_fn("Proceed? [y/N]: ")
    except EOFError:
        answer = ""
    return answer.strip().lower() in ("y", "yes")


# ---------------------------------------------------------------------------
# Provider usability (athenaeum#2735)
# ---------------------------------------------------------------------------


def check_provider_usable(provider: str) -> str | None:
    """Return a one-line stop message when *provider* cannot run, else ``None``.

    Runs before any file is read. ``claude-cli``: the binary must exist
    (:func:`athenaeum.provider.preflight_provider`) and ``claude auth status``
    must report a login — a local read, never a billed model call. A probe
    that cannot answer (timeout, spawn error, unparseable output) is treated
    as usable: the all-classify-calls-failed check in :func:`run_import` is
    the backstop, and a flaky probe must not block a working login.
    """
    from athenaeum.provider import preflight_provider

    error = preflight_provider(provider)
    if error is not None:
        return error
    if provider != "claude-cli":
        return None
    binary = os.environ.get("ATHENAEUM_CLAUDE_CLI_BIN") or "claude"
    fix = (
        "claude-cli provider selected but Claude Code is not logged in. "
        "Run `claude auth login`, then re-run athenaeum import-memory."
    )
    try:
        proc = subprocess.run(
            [binary, "auth", "status"],
            capture_output=True,
            text=True,
            timeout=_LOGIN_PROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return fix
    try:
        status = json.loads(proc.stdout or "")
    except json.JSONDecodeError:
        return None
    if isinstance(status, dict) and status.get("loggedIn") is False:
        return fix
    return None


class _ClassifyTally(logging.Handler):
    """Counts classify outcomes during one compile.

    ``claim_kind`` / ``decay_bucket`` fail open: a failed call logs a warning
    and leaves the file unclassified, so the compile still exits 0. This
    handler reads those two loggers' own records (a failure warning, a
    ``stamped`` info line) so the caller can tell "every classify call
    failed" from a healthy run (athenaeum#2735).
    """

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.failed = 0
        self.succeeded = 0

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if "classify call failed" in message:
            self.failed += 1
        elif ": stamped " in message:
            self.succeeded += 1

    @property
    def all_failed(self) -> bool:
        return self.failed > 0 and self.succeeded == 0


@contextmanager
def _tally_classify_outcomes() -> Iterator[_ClassifyTally]:
    tally = _ClassifyTally()
    loggers = [logging.getLogger(name) for name in _CLASSIFY_LOGGERS]
    saved = [lg.level for lg in loggers]
    for lg in loggers:
        lg.addHandler(tally)
        if lg.getEffectiveLevel() > logging.INFO:
            lg.setLevel(logging.INFO)
    try:
        yield tally
    finally:
        for lg, level in zip(loggers, saved):
            lg.removeHandler(tally)
            lg.setLevel(level)


# ---------------------------------------------------------------------------
# Resumable state (AC3/AC8) — <cache_dir>/import_memory_state.json
# ---------------------------------------------------------------------------


def _state_path(cache_dir: Path) -> Path:
    return Path(cache_dir) / STATE_FILENAME


def load_state(cache_dir: Path) -> dict[str, Any]:
    """Load the resumable state, fail-open to an empty state (never raises).

    Mirrors :mod:`athenaeum.batch_state`'s fail-open contract: a missing,
    empty, or corrupt state file degrades to "nothing imported yet" rather
    than breaking the run — the cost of a lost state is re-attempting a
    compile the underlying pipeline will no-op on anyway (see the module
    docstring), never data loss.
    """
    path = _state_path(cache_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {"version": 1, "imported": {}}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"version": 1, "imported": {}}
    if not isinstance(data, dict) or not isinstance(data.get("imported"), dict):
        return {"version": 1, "imported": {}}
    return data


def save_state(cache_dir: Path, state: dict[str, Any]) -> None:
    atomic_write_text(
        _state_path(cache_dir), json.dumps(state, indent=2, sort_keys=True) + "\n"
    )


def _record_key(scope: str, filename: str) -> str:
    return f"{scope}/{filename}"


def _is_imported(state: dict[str, Any], scope: str, file: MemoryFile) -> bool:
    record = state["imported"].get(_record_key(scope, file.path.name))
    if not isinstance(record, dict):
        return False
    return (
        record.get("size_bytes") == file.size_bytes and record.get("mtime") == file.mtime
    )


def _mark_imported(
    state: dict[str, Any],
    scope: str,
    file: MemoryFile,
    *,
    imported_at: str,
    refused_reason: str | None = None,
    held_reason: str | None = None,
) -> None:
    record: dict[str, Any] = {
        "size_bytes": file.size_bytes,
        "mtime": file.mtime,
        "imported_at": imported_at,
    }
    if refused_reason is not None:
        # athenaeum#2645: handled because the relevance gate refused it, not
        # because it compiled. The reason names the deciding rule.
        record["refused"] = refused_reason
    if held_reason is not None:
        # athenaeum#2734: the content reached a compiled page, but the
        # librarian keeps the raw file queued (comparator hold).
        record["held"] = held_reason
    state["imported"][_record_key(scope, file.path.name)] = record


def _relevance_refusal(staged: Path, policy: Any) -> str | None:
    """The relevance gate's refusal reason for a staged copy, or ``None``.

    Same decision the compile pipeline makes (``filter_relevance_never``):
    refused only when EVERY declared item is covered by a ``never`` rule.
    Unreadable staged files are not refused.
    """
    from athenaeum.relevance_policy import filter_relevance_never

    class _Staged:
        path = staged

    _kept, refused = filter_relevance_never([_Staged], policy)
    return refused[0][1].reason if refused else None


#: Merged-page section header :func:`athenaeum.merge.synthesize_body` writes
#: per member: ``## From `<scope>/<filename>```.
_MEMBER_HEADER_RE = re.compile(r"^## From `([^`\n]+)`", re.MULTILINE)

#: Recorded on a held-but-compiled file's state entry (athenaeum#2734).
HELD_REASON = (
    "content compiled into a wiki page; raw file kept queued by the "
    "comparator hold (athenaeum#1946)"
)


#: Footnote markers (``[^src-1]``) the merge stamps onto the surviving copy of
#: a paragraph; stripping them restores the member's own text.
_FOOTNOTE_MARKER_RE = re.compile(r"\[\^[^\]\s]+\]")

#: Slack for coarse filesystem timestamps when asking "was this page written
#: by the compile that just ran".
_MTIME_SLACK_SECONDS = 2.0


def _canonical_paragraphs(text: str) -> set[str]:
    """Whitespace-canonical, marker-free paragraphs of *text* (the unit
    :func:`athenaeum.merge.synthesize_body` dedupes on)."""
    plain = _FOOTNOTE_MARKER_RE.sub("", text)
    return {
        canonical
        for para in re.split(r"\n\s*\n", plain)
        if (canonical := " ".join(para.split()))
    }


class _CompiledPages:
    """What the ``wiki/auto-*.md`` pages written since *since* say they hold.

    ``refs`` is every ``<scope>/<filename>`` a page cites in a ``## From``
    header; ``paragraphs`` is every canonical paragraph on those pages. Only
    pages modified at or after *since* count, so a stale page from an earlier
    run cannot vouch for a file this run's compile dropped (a miss leaves the
    file pending, the safe direction).
    """

    def __init__(self, wiki_root: Path, since: float) -> None:
        from athenaeum.merge import AUTO_WIKI_PREFIX

        self.refs: set[str] = set()
        self.paragraphs: set[str] = set()
        for page in wiki_root.glob(f"{AUTO_WIKI_PREFIX}*.md"):
            try:
                if page.stat().st_mtime < since:
                    continue
                text = page.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            self.refs.update(_MEMBER_HEADER_RE.findall(text))
            self.paragraphs |= _canonical_paragraphs(text)

    def holds(self, key: str, staged: Path) -> bool:
        """True when the content of the staged file *key* reached a page.

        Either its section header is on a page, or — when the merge dropped
        the whole section because every paragraph duplicated an earlier
        member's, so no header was written — every paragraph of its body is
        on a page.
        """
        if key in self.refs:
            return True
        from athenaeum.models import parse_frontmatter

        try:
            _meta, body = parse_frontmatter(staged.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            return False
        mine = _canonical_paragraphs(body)
        return bool(mine) and mine <= self.paragraphs


def _pending_files(
    selected: list[ProjectMemory], state: dict[str, Any]
) -> list[tuple[ProjectMemory, MemoryFile]]:
    """Every selected file not already recorded as imported (AC3/AC8 resume)."""
    pending: list[tuple[ProjectMemory, MemoryFile]] = []
    for project in selected:
        for file in project.files:
            if not _is_imported(state, project.scope, file):
                pending.append((project, file))
    return pending


# ---------------------------------------------------------------------------
# Staging + compile
# ---------------------------------------------------------------------------


def _staged_path(knowledge_root: Path, project: ProjectMemory, file: MemoryFile) -> Path:
    """Where *file* lands under ``<knowledge_root>/raw/auto-memory/<scope>/``.

    Shared by :func:`_stage_file` (the write) and :func:`run_import`'s
    post-compile reconciliation (the read) so both agree on the same path
    without recomputing it differently in two places.
    """
    return knowledge_root / "raw" / "auto-memory" / project.scope / file.path.name


def _stage_file(knowledge_root: Path, project: ProjectMemory, file: MemoryFile) -> Path:
    """Copy *file* into ``<knowledge_root>/raw/auto-memory/<scope>/<name>``.

    The SAME location an operator's manual symlink convention already uses
    (:mod:`athenaeum.memory_index`'s module docstring) — scoping the real
    compile pipeline to this file via its own existing ``raw/auto-memory``
    intake, never a bespoke second intake path. Content-read happens HERE,
    never before the explicit confirmation that gates every caller of this
    function (AC1/AC5).

    Restores the SOURCE's original ``mtime`` on the staged copy after the
    write. :func:`athenaeum.atomic_io.atomic_write_text` always stamps a
    fresh destination inode with "now" (same-dir temp + ``os.replace``),
    which is right for its original pending-sidecar use but wrong here:
    :func:`athenaeum.session_recovery.written_at_from_frontmatter`'s own
    docstring documents that the mtime fallback it feeds works only because
    "the symlink-bridged intake tree preserves" the native file's mtime —
    this module is the first caller that stages a real COPY rather than a
    symlink, so without this restore every staged file's provenance-
    recovery fallback would silently degrade to "now" instead of the
    memory's real write time. Restoring it also keeps a scope an operator
    has ALREADY bridged via ``examples/claude-code/setup-symlinks.sh``
    self-consistent: there, staging's destination and the discovery source
    are the same inode, so leaving the reset mtime in place would make the
    next run's discovery mismatch the state entry THIS run just recorded
    (which captured the pre-staging mtime) and re-stage forever.
    """
    dest = _staged_path(knowledge_root, project, file)
    content = file.path.read_text(encoding="utf-8")
    atomic_write_text(dest, content)
    os.utime(dest, (file.mtime, file.mtime))
    return dest


def _default_compile_batch(
    knowledge_root: Path, *, max_files: int, max_api_calls: int
) -> int:
    """Invoke the real compile pipeline (issue athenaeum#2316 ruling 5).

    A thin wrapper around :func:`athenaeum.librarian.run` — the SAME
    pipeline ``athenaeum ingest``/``athenaeum run`` already use — so
    production callers of :func:`run_import` get a real compile. Every test
    in this repo substitutes a fake via ``run_import``'s ``compile_fn``
    parameter instead of calling this function, so no test here makes a
    real compile call.

    Passes ``raw_root``/``wiki_root`` explicitly, derived from
    *knowledge_root* the SAME way :func:`athenaeum._cmd_run.cmd_run` and
    :func:`athenaeum.drain.run_drain` already do (``<knowledge_root>/raw``,
    ``<knowledge_root>/wiki``). Without them, ``librarian.run`` falls back
    to its own module-level ``DEFAULT_RAW_ROOT``/``DEFAULT_WIKI_ROOT`` —
    the operator's home knowledge base — rather than the root *knowledge_root*
    names, so a run against a throwaway/test root would silently compile the
    default corpus instead (issue athenaeum#2582).
    """
    from athenaeum.librarian import run as librarian_run

    return librarian_run(
        raw_root=knowledge_root / "raw",
        wiki_root=knowledge_root / "wiki",
        knowledge_root=knowledge_root,
        max_files=max_files,
        max_api_calls=max_api_calls,
        # athenaeum#2736: the staged copies have no originating transcript.
        staged_import=True,
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _print_summary(
    selected: Iterable[ProjectMemory],
    pending_count: int,
    already_done: int,
    cost: CostEstimate,
    held_done: int = 0,
    *,
    max_files: int,
    max_calls: int,
    out: Any,
    batch: Sequence[tuple[ProjectMemory, MemoryFile]] = (),
) -> None:
    names = ", ".join(p.scope for p in selected)
    print(f"Selected projects: {names}", file=out)
    print(
        f"  {cost.project_count} project(s), {pending_count} file(s) pending"
        + (f" ({already_done} already imported)" if already_done else "")
        + (f" ({held_done} of those held for the comparator)" if held_done else ""),
        file=out,
    )
    login = (
        f"(~${cost.estimated_cost_usd:.2f})"
        if cost.estimated_cost_usd is not None
        else "on your Claude Code login"
    )
    print(
        f"  Estimated cost for this run: ~{cost.estimated_tokens:,} tokens {login} "
        f"for {cost.file_count} file(s), about {cost.estimated_api_calls} API calls "
        "— rough: about 2,800 tokens per file, measured on small files; larger "
        "files cost more (not yet measured).",
        file=out,
    )
    if cost.total_pending > cost.file_count:
        print(
            f"  This run covers {cost.file_count} of {cost.total_pending} pending "
            f"file(s); the other {cost.total_pending - cost.file_count} wait for "
            "later runs (file/call cap).",
            file=out,
        )
    print(f"  Run cap: {max_files} files, {max_calls} API calls this run.", file=out)
    if batch:
        _print_batch_files(batch, out=out)


#: How many file names the confirmation summary lists per project before it
#: collapses the rest into a count (athenaeum#2737).
PROMPT_FILES_PER_PROJECT = 5


def _print_batch_files(
    batch: Sequence[tuple[ProjectMemory, MemoryFile]],
    *,
    out: Any,
    per_project: int = PROMPT_FILES_PER_PROJECT,
) -> None:
    """List the file names this run will read, grouped by project.

    The user is consenting to which memory files are read, so the summary
    names them rather than counting them (athenaeum#2737). A project with
    more than *per_project* files in the batch shows the first
    *per_project* names plus a count of the rest.
    """
    grouped: dict[str, list[str]] = {}
    for project, file in batch:
        grouped.setdefault(project.scope, []).append(file.path.name)
    print("  Files this run will read:", file=out)
    for scope, names in grouped.items():
        print(f"    {scope}:", file=out)
        for name in names[:per_project]:
            print(f"      {name}", file=out)
        if len(names) > per_project:
            print(f"      ... and {len(names) - per_project} more", file=out)


def run_import(
    *,
    knowledge_root: Path,
    cache_dir: Path,
    provider: str,
    discovered: list[ProjectMemory],
    cli_projects: list[str] | None,
    auto_yes: bool,
    max_files: int,
    max_calls: int,
    picker: Callable[[list[ProjectMemory]], list[str]] | None = None,
    confirm: Callable[[], bool] | None = None,
    acquire_lock: Callable[[], "RunLock | int"] | None = None,
    compile_fn: Callable[..., int] | None = None,
    provider_check: Callable[[str], str | None] | None = None,
    out: Any = sys.stdout,
) -> int:
    """Orchestrate selection, the cost/cap prompt, and a capped compile batch.

    Pure of real I/O side effects beyond what its injected callables do —
    *picker*/*confirm*/*acquire_lock*/*compile_fn* are every point this
    function touches the outside world (stdin, the run lock, the LLM
    pipeline), so a test can substitute every one of them and exercise the
    whole selection/cap/resume contract with zero real reads, zero real
    locks, and zero real compile calls. *provider_check* (default: none —
    :func:`_cmd_import_memory` passes :func:`check_provider_usable`) returns a
    one-line stop message when the provider cannot run; it fires before the
    prompt and before any file is staged (athenaeum#2735).
    """
    selected, unknown = select_projects(discovered, cli_projects, picker=picker)
    if unknown:
        known = ", ".join(p.scope for p in discovered) or "(none discovered)"
        print(
            f"error: unknown --project value(s): {', '.join(unknown)}. "
            f"Discovered projects: {known}",
            file=sys.stderr,
        )
        return EXIT_INCOMPLETE
    if not selected:
        print("No projects selected; nothing was read.", file=out)
        return 0

    state = load_state(cache_dir)
    pending = _pending_files(selected, state)
    total_files = sum(p.file_count for p in selected)
    already_done = total_files - len(pending)
    held_done = sum(
        1
        for project in selected
        for file in project.files
        if _is_imported(state, project.scope, file)
        and "held" in state["imported"][_record_key(project.scope, file.path.name)]
    )

    # Files this run will actually process: the file cap AND the call cap
    # (each file costs CALLS_PER_FILE classify calls). The cost line prices
    # exactly this batch, not every pending file (athenaeum#2733).
    batch_size = min(max_files, max(1, max_calls // CALLS_PER_FILE))
    cost = estimate_cost(
        (file for _, file in pending[:batch_size]),
        project_count=len(selected),
        provider=provider,
        total_pending=len(pending),
    )
    _print_summary(
        selected,
        len(pending),
        already_done,
        cost,
        held_done,
        max_files=max_files,
        max_calls=max_calls,
        out=out,
        batch=pending[:batch_size],
    )

    if not pending:
        print("Nothing left to import for the selected projects.", file=out)
        return 0

    if provider_check is not None:
        problem = provider_check(provider)
        if problem is not None:
            print(f"import-memory: {problem} Nothing was read.", file=sys.stderr)
            return EXIT_INCOMPLETE

    if not auto_yes:
        confirmed = (confirm or _confirm)()
        if not confirmed:
            print("Declined — nothing was read.", file=out)
            return 0

    lock: "RunLock | int | None" = None
    if acquire_lock is not None:
        lock = acquire_lock()
        if isinstance(lock, int):
            return lock

    try:
        batch = pending[:batch_size]
        left_after_cap = len(pending) - len(batch)

        for project, file in batch:
            _stage_file(knowledge_root, project, file)

        compile = compile_fn or _default_compile_batch
        compile_started = time.time()
        try:
            with _tally_classify_outcomes() as tally:
                exit_code = compile(
                    knowledge_root, max_files=len(batch), max_api_calls=max_calls
                )
        except Exception as exc:  # noqa: BLE001 — AC8: report, never crash opaque
            print(
                f"import-memory: run did not complete ({type(exc).__name__}: {exc}). "
                f"{len(batch)} file(s) were not marked imported; rerun athenaeum "
                "import-memory to resume — restaging and recompiling an "
                "already-compiled file is a safe no-op.",
                file=sys.stderr,
            )
            return EXIT_INCOMPLETE

        if exit_code != 0:
            print(
                f"import-memory: compile exited {exit_code}; {len(batch)} file(s) "
                "were not marked imported. Rerun athenaeum import-memory to resume.",
                file=sys.stderr,
            )
            return EXIT_INCOMPLETE

        if tally.all_failed:
            print(
                f"import-memory: every classify call failed ({tally.failed}); the "
                "LLM provider is not answering (for claude-cli, check `claude auth "
                f"status`). {len(batch)} file(s) were not marked imported; fix the "
                "provider and rerun athenaeum import-memory to retry them.",
                file=sys.stderr,
            )
            return EXIT_INCOMPLETE

        from athenaeum.store import now_iso

        # exit_code == 0 means the compile call succeeded overall — it does
        # NOT mean every staged file in this batch was actually compiled.
        # Intake silently `continue`s past a file named ``MEMORY.md``, one
        # whose name and frontmatter both miss the recognised-type
        # convention, one that fails to read, or one `classify_ephemeral`
        # drops — none of that surfaces as a nonzero exit code or a raised
        # exception, so a batch can succeed overall while leaving one or
        # more of its own files untouched. Reconcile against the SAME
        # observable signal the move-then-retire pass's own caller already
        # uses elsewhere in this codebase (a before/after presence diff,
        # issue athenaeum#895's "left the intake queue" invariant): a
        # staged file that is GONE after a successful compile was actually
        # processed (compiled-and-retired, or merged); one still on disk
        # was not, for any of the reasons above, and must stay PENDING so
        # the next run retries it instead of silently losing it forever.
        # This can only ever UNDER-approximate what was processed, same as
        # the precedent it follows — never the wrong direction.
        imported_at = now_iso(None)
        # athenaeum#2645: the relevance gate leaves a refused file on disk
        # (never deleted), so by presence alone it looks "not compiled" and
        # would be restaged -- and re-refused -- every run, consuming the cap.
        # Consult the same gate on the staged copy: a refusal is HANDLED,
        # recorded with its reason, and never restaged until the source changes.
        relevance_policy: Any = None
        try:
            from athenaeum.relevance_policy import load_relevance_policy

            relevance_policy = load_relevance_policy(knowledge_root / "wiki")
        except Exception:  # noqa: BLE001 — malformed policy: treat as not refused
            relevance_policy = None
        skipped: list[tuple[ProjectMemory, MemoryFile]] = []
        refused_count = 0
        processed_count = 0
        held_count = 0
        compiled: _CompiledPages | None = None
        for project, file in batch:
            staged = _staged_path(knowledge_root, project, file)
            if staged.exists():
                reason = _relevance_refusal(staged, relevance_policy)
                if reason is None:
                    if compiled is None:
                        compiled = _CompiledPages(
                            knowledge_root / "wiki",
                            compile_started - _MTIME_SLACK_SECONDS,
                        )
                    if compiled.holds(_record_key(project.scope, file.path.name), staged):
                        _mark_imported(
                            state,
                            project.scope,
                            file,
                            imported_at=imported_at,
                            held_reason=HELD_REASON,
                        )
                        held_count += 1
                        continue
                    skipped.append((project, file))
                    continue
                _mark_imported(
                    state,
                    project.scope,
                    file,
                    imported_at=imported_at,
                    refused_reason=reason,
                )
                refused_count += 1
                continue
            _mark_imported(state, project.scope, file, imported_at=imported_at)
            processed_count += 1
        save_state(cache_dir, state)

        remaining = left_after_cap
        if processed_count == 0 and held_count and not (refused_count or skipped):
            print(
                f"Nothing new imported: all {held_count} pending file(s) are held "
                "for the comparator (already compiled into a page); recorded so "
                "they are not restaged.",
                file=out,
            )
        else:
            print(f"Imported {processed_count} file(s).", file=out)
            if held_count:
                print(
                    f"{held_count} file(s) held for the comparator (already "
                    "compiled into a page); recorded so they are not restaged.",
                    file=out,
                )
        if refused_count:
            print(
                f"{refused_count} file(s) refused by the relevance policy; "
                "recorded as handled and not restaged.",
                file=out,
            )
        if skipped:
            print(
                f"{len(skipped)} file(s) were staged but not compiled (dropped "
                "or deferred during intake); not marked imported. Rerun "
                "athenaeum import-memory to retry them.",
                file=out,
            )
        if remaining:
            print(
                f"{remaining} file(s) left for the next run (cap reached).",
                file=out,
            )
        return 0
    finally:
        if lock is not None and not isinstance(lock, int):
            lock.release()


def _cmd_import_memory(args: argparse.Namespace) -> int:
    from athenaeum.provider import resolve_provider

    knowledge_root = _resolve_knowledge_root(args)
    cache_dir = resolve_cache_dir(getattr(args, "cache_dir", None))
    cache_dir.mkdir(parents=True, exist_ok=True)
    config = load_config(knowledge_root) if knowledge_root.is_dir() else None
    provider = resolve_provider(config)

    discovered = discover_project_memories(args.projects_root)

    def _do_acquire() -> "RunLock | int":
        return _acquire_or_exit(knowledge_root, args, config)

    return run_import(
        knowledge_root=knowledge_root,
        cache_dir=cache_dir,
        provider=provider,
        discovered=discovered,
        cli_projects=args.projects,
        auto_yes=args.yes,
        max_files=args.max_files,
        max_calls=args.max_calls,
        acquire_lock=_do_acquire,
        provider_check=check_provider_usable,
    )
