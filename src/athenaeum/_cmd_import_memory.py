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
   dollars added on ``api`` — sized from file METADATA ONLY via
   :mod:`athenaeum.memory_discovery`, before any file content is read. The
   prompt says the figure is rough until athenaeum#2323 measures real usage.
3. **Run cap.** 20 files and 100 API calls per run (:data:`DEFAULT_MAX_FILES`
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
``"<scope>/<filename>" -> {size_bytes, mtime, imported_at}``. A file is
PENDING only when no recorded entry matches its CURRENT source size+mtime
(:func:`_pending_files`) — so a changed source file is treated as new again,
and an already-imported, unchanged one is skipped on every later run, cap or
no cap. Staging a file (copying it into
``<knowledge_root>/raw/auto-memory/<scope>/<filename>``, the SAME convention
an operator uses manually today — :mod:`athenaeum.memory_index`'s module
docstring) is content-stable: the destination path is a pure function of
scope+filename, so a retried stage is an idempotent overwrite, never a
second raw member for the same source. The state entries for a batch are
written ONLY after :func:`_default_compile_batch` (or its test substitute)
returns exit code ``0`` — a crash, a raised exception (the catch-all AC8
names, which is also what a missing/expired login surfaces as — this module
has no separate login probe, since any interruption gets the identical
honest "did not complete; nothing marked imported; rerun to resume"
treatment) or a nonzero/degraded exit leaves every file in the batch PENDING
again. Re-running is safe either way: re-staging overwrites the same path,
and the underlying compile pipeline's own incremental/hash-stamped dedup
(the same one ``athenaeum ingest`` already relies on) makes recompiling an
already-fully-compiled raw member a no-op rather than a duplicate page.

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
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from athenaeum._cli_shared import _acquire_or_exit, _add_lock_args, _positive_int
from athenaeum.atomic_io import atomic_write_text
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT, load_config, resolve_cache_dir
from athenaeum.memory_discovery import (
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
    state: dict[str, Any], scope: str, file: MemoryFile, *, imported_at: str
) -> None:
    state["imported"][_record_key(scope, file.path.name)] = {
        "size_bytes": file.size_bytes,
        "mtime": file.mtime,
        "imported_at": imported_at,
    }


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


def _stage_file(knowledge_root: Path, project: ProjectMemory, file: MemoryFile) -> Path:
    """Copy *file* into ``<knowledge_root>/raw/auto-memory/<scope>/<name>``.

    The SAME location an operator's manual symlink convention already uses
    (:mod:`athenaeum.memory_index`'s module docstring) — scoping the real
    compile pipeline to this file via its own existing ``raw/auto-memory``
    intake, never a bespoke second intake path. Content-read happens HERE,
    never before the explicit confirmation that gates every caller of this
    function (AC1/AC5).
    """
    dest = knowledge_root / "raw" / "auto-memory" / project.scope / file.path.name
    content = file.path.read_text(encoding="utf-8")
    atomic_write_text(dest, content)
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
    """
    from athenaeum.librarian import run as librarian_run

    return librarian_run(
        knowledge_root=knowledge_root,
        max_files=max_files,
        max_api_calls=max_api_calls,
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _print_summary(
    selected: Iterable[ProjectMemory],
    pending_count: int,
    already_done: int,
    cost: CostEstimate,
    *,
    max_files: int,
    max_calls: int,
    out: Any,
) -> None:
    names = ", ".join(p.scope for p in selected)
    print(f"Selected projects: {names}", file=out)
    print(
        f"  {cost.project_count} project(s), {pending_count} file(s) pending"
        + (f" ({already_done} already imported)" if already_done else ""),
        file=out,
    )
    if cost.estimated_cost_usd is not None:
        print(
            f"  Estimated cost: ~{cost.estimated_tokens} tokens (~${cost.estimated_cost_usd:.2f}) "
            "— rough, from file size only; not yet measured against a real compile.",
            file=out,
        )
    else:
        print(
            f"  Estimated cost: ~{cost.estimated_tokens} tokens on your Claude Code login "
            "— rough, from file size only; not yet measured against a real compile.",
            file=out,
        )
    print(f"  Run cap: {max_files} files, {max_calls} API calls this run.", file=out)


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
    out: Any = sys.stdout,
) -> int:
    """Orchestrate selection, the cost/cap prompt, and a capped compile batch.

    Pure of real I/O side effects beyond what its injected callables do —
    *picker*/*confirm*/*acquire_lock*/*compile_fn* are every point this
    function touches the outside world (stdin, the run lock, the LLM
    pipeline), so a test can substitute every one of them and exercise the
    whole selection/cap/resume contract with zero real reads, zero real
    locks, and zero real compile calls.
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

    cost = estimate_cost(
        (file for _, file in pending), project_count=len(selected), provider=provider
    )
    _print_summary(
        selected,
        len(pending),
        already_done,
        cost,
        max_files=max_files,
        max_calls=max_calls,
        out=out,
    )

    if not pending:
        print("Nothing left to import for the selected projects.", file=out)
        return 0

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
        batch = pending[:max_files]
        left_after_cap = len(pending) - len(batch)

        for project, file in batch:
            _stage_file(knowledge_root, project, file)

        compile = compile_fn or _default_compile_batch
        try:
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

        from athenaeum.store import now_iso

        imported_at = now_iso(None)
        for project, file in batch:
            _mark_imported(state, project.scope, file, imported_at=imported_at)
        save_state(cache_dir, state)

        remaining = left_after_cap
        print(f"Imported {len(batch)} file(s).", file=out)
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

    knowledge_root = (args.path or DEFAULT_KNOWLEDGE_ROOT).expanduser().resolve()
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
    )
