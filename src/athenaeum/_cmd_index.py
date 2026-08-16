# SPDX-License-Identifier: Apache-2.0
"""``athenaeum {reindex,ingest,session-end,compile,registry}`` — index/compile pipeline.

Five subcommands grouped here because each is a deterministic (mostly no-LLM)
operation over the compiled wiki / search index: rebuild the search index
(``reindex``, aliased ``rebuild-index``), on-demand incremental compile
(``ingest``), the change-gated compile+reindex composition used by the
SessionEnd hook (``session-end``), a historical as-of recompile (``compile``),
and the source-handle registry builder (``registry``). None of these run the
full librarian pipeline (that is ``athenaeum run``, see ``_cmd_run.py``).

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` (or a small same-domain group module like this
one) and registers via ``add_<name>_subparser`` — this is where a NEW
subcommand goes, not inline in ``cli.py``'s ``main()``. This module may
import library modules (L4/L3) but ``cli.py`` only imports the
``add_*_subparser`` entry points, kept lazy/local to keep top-level import
cost down.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from athenaeum._cli_shared import _acquire_or_exit, _add_lock_args, _iso_date
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT, resolve_cache_dir
from athenaeum.logconf import configure_logging

if TYPE_CHECKING:
    from athenaeum.reasoning_triggers import TriggerDecision
    from athenaeum.runlock import RunLock


def add_index_subparsers(subparsers: argparse._SubParsersAction) -> None:
    """Register ``reindex``/``rebuild-index``, ``compile``, ``registry``,
    ``ingest``, ``session-end``."""

    # reindex command (issue athenaeum#349) — rebuild the search index out-of-band.
    # ``rebuild-index`` is kept as a back-compat alias for the athenaeum#348 spelling;
    # both dispatch to the identical handler (no duplicated index engine).
    rebuild_parser = subparsers.add_parser(
        "reindex",
        aliases=["rebuild-index"],
        help="Rebuild the search index (FTS5 or vector, per config). "
        "--incremental (default) applies only the athenaeum#348 hash-diff delta; "
        "--full rebuilds from scratch.",
    )
    rebuild_parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    rebuild_parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory (default: ~/.cache/athenaeum)",
    )
    rebuild_parser.add_argument(
        "--backend",
        choices=["fts5", "vector"],
        default=None,
        help="Override configured backend (default: read from athenaeum.yaml)",
    )
    reindex_mode = rebuild_parser.add_mutually_exclusive_group()
    reindex_mode.add_argument(
        "--incremental",
        action="store_true",
        help=(
            "Apply only the changed/added/deleted hash-diff delta (issue "
            "athenaeum#348). This is the DEFAULT; the flag makes it explicit."
        ),
    )
    reindex_mode.add_argument(
        "--full",
        action="store_true",
        help=(
            "Wipe and fully rebuild instead of applying only the "
            "changed/added/deleted delta (issue athenaeum#348). Use for seeding or "
            "after an embedding-model change; default is incremental."
        ),
    )
    rebuild_parser.add_argument(
        "--as-of",
        dest="as_of",
        type=_iso_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="Issue athenaeum#308: build an as-of index reflecting the wiki as it stood "
        "on this date (pages outside their [valid_from, valid_until] window then "
        "are excluded). Always a full build; point --cache-dir at a scratch "
        "directory so the live index is not overwritten, then "
        "`recall --cache-dir <that>`. Unset = today (the normal live index).",
    )
    _add_lock_args(rebuild_parser)
    rebuild_parser.set_defaults(func=cmd_rebuild_index)

    # compile command (issue athenaeum#359) — compile-as-of. Recompiles a HISTORICAL
    # wiki snapshot as it would have stood on --as-of, into a scratch --out
    # dir. Distinct from slice 3's read-time `recall/reindex --as-of` filter:
    # this RE-RUNS the deterministic C3 blend (resurrecting members expired
    # now but valid then), never touching the live wiki or raw tree.
    compile_parser = subparsers.add_parser(
        "compile",
        help="Issue athenaeum#359: recompile a historical wiki snapshot as-of a past "
        "date into a scratch --out dir (compile-as-of). Distinct from the "
        "read-time `recall/reindex --as-of` filter — this re-runs the C3 "
        "blend so members expired now but valid then are re-included. "
        "Deterministic (no LLM); never mutates the live wiki or raw tree.",
    )
    compile_parser.add_argument(
        "--path",
        "--knowledge-root",
        dest="path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge).",
    )
    compile_parser.add_argument(
        "--as-of",
        dest="as_of",
        type=_iso_date,
        required=True,
        metavar="YYYY-MM-DD",
        help="Historical date to recompile as of (inclusive). Members whose "
        "validity window had closed on this date (or that carry a tombstone) "
        "are excluded; members expired now but valid then are re-included. "
        "Rewind is valid-time, not transaction-time (see docs §8.7).",
    )
    compile_parser.add_argument(
        "--out",
        dest="out",
        type=Path,
        required=True,
        metavar="DIR",
        help="Scratch directory to write the recompiled wiki into. MUST NOT "
        "be the live wiki/ directory.",
    )
    compile_parser.set_defaults(func=cmd_compile_as_of)

    # registry command (issue athenaeum#453) — compile the source-handle registry.
    # A deterministic, LLM-free read of the wiki tree that emits registry.json
    # (entity uid → handle set) for the fact-mining adapters to consume. Emits
    # a well-formed (possibly empty) registry regardless of how many handles
    # are populated, so the operator-only seed (athenaeum#454) is never a precondition.
    registry_parser = subparsers.add_parser(
        "registry",
        help="Issue athenaeum#453: compile the source-handle registry.json (entity uid "
        "→ handle set) from wiki entity frontmatter. Deterministic, no LLM; "
        "emits a well-formed registry even when no handles are populated yet.",
    )
    registry_parser.add_argument(
        "--path",
        "--knowledge-root",
        dest="path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge).",
    )
    registry_parser.add_argument(
        "--out",
        dest="out",
        type=Path,
        default=None,
        metavar="FILE",
        help="Where to write registry.json (default: <knowledge-root>/"
        "registry.json).",
    )
    registry_parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print the registry JSON to stdout instead of writing a file.",
    )
    registry_parser.set_defaults(func=cmd_registry)

    # ingest command (issue athenaeum#349) — on-demand compile of new/changed raw
    # intake into the wiki. The manual escape hatch that makes a just-
    # remembered fact recallable now, decoupled from the nightly `run`.
    ingest_parser = subparsers.add_parser(
        "ingest",
        help="Compile new/changed raw intake into the wiki on demand "
        "(issue athenaeum#349). --incremental (default) compiles only files new/"
        "changed since the last ingest; --full recompiles.",
    )
    ingest_parser.add_argument(
        "--path",
        "--knowledge-root",
        dest="path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge). "
        "--knowledge-root is an alias, matching `run`.",
    )
    ingest_mode = ingest_parser.add_mutually_exclusive_group()
    ingest_mode.add_argument(
        "--incremental",
        dest="incremental",
        action="store_true",
        default=None,
        help="Compile only raw files new/changed since the last successful "
        "ingest (tracked via a content-hash stamp). This is the DEFAULT.",
    )
    ingest_mode.add_argument(
        "--full",
        dest="incremental",
        action="store_false",
        help="Recompile all pending raw intake, ignoring the ingest stamp.",
    )
    ingest_parser.add_argument(
        "--session",
        type=str,
        default=None,
        help="Scope the new/changed detection to one originSessionId "
        "(the SessionEnd use-case, issue athenaeum#350).",
    )
    ingest_parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory holding the ingest stamp manifest "
        "(default: ~/.cache/athenaeum)",
    )
    ingest_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the compile without writing files, committing, or "
        "updating the ingest stamp.",
    )
    ingest_parser.add_argument(
        "--if-triggered",
        action="store_true",
        help="Additive control signal (issue athenaeum#909): before compiling, "
        "evaluate the configured reasoning-tier triggers (backlog file "
        "count / bytes, elapsed interval, nightly backstop — "
        "librarian.reasoning_triggers.*) against LIVE state. When none "
        "fired, does NOT compile — prints the same one-line JSON summary "
        "carrying trigger=\"none\" and exits 0 (cheap, side-effect-free: "
        "no lock taken). When one fired, runs the normal incremental "
        "ingest exactly as without this flag, with the firing trigger's "
        "name in the summary, and — on a clean non-dry-run completion — "
        "advances the reasoning-trigger last-run stamp used by the "
        "elapsed-interval and nightly-backstop checks. This adds a "
        "control signal to the existing on-demand poke; it is not a "
        "second way for data to enter, and it never forces a full "
        "recompile (always --incremental's budgeted, resumable path).",
    )
    ingest_parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging",
    )
    _add_lock_args(ingest_parser)
    ingest_parser.set_defaults(func=cmd_ingest)

    # session-end command (issue athenaeum#350) — the change-gated compile-then-index
    # composition the cwc SessionEnd hook and the nightly-after-librarian path
    # invoke as ONE command: incremental `ingest` of this session's new raw,
    # then (only when the compile actually ran) an incremental `reindex` so the
    # freshly-compiled wiki pages become recallable. An idle SessionEnd is a
    # fast no-op with zero LLM cost and no reindex.
    session_end_parser = subparsers.add_parser(
        "session-end",
        help="Change-gated ingest + reindex for SessionEnd (issue athenaeum#350): "
        "compile this session's new raw intake, then refresh the index — a "
        "fast no-op (no LLM, no reindex) when nothing changed.",
    )
    session_end_parser.add_argument(
        "--path",
        "--knowledge-root",
        dest="path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge). "
        "--knowledge-root is an alias, matching `run`/`ingest`.",
    )
    session_end_mode = session_end_parser.add_mutually_exclusive_group()
    session_end_mode.add_argument(
        "--incremental",
        dest="incremental",
        action="store_true",
        default=None,
        help="Compile only raw new/changed since the last ingest and apply the "
        "athenaeum#348 index delta. This is the DEFAULT.",
    )
    session_end_mode.add_argument(
        "--full",
        dest="incremental",
        action="store_false",
        help="Force a full recompile of all pending raw intake AND a full "
        "index rebuild (operator escape hatch).",
    )
    session_end_parser.add_argument(
        "--session",
        type=str,
        default=None,
        help="Scope the new/changed detection to one originSessionId "
        "(the SessionEnd use-case).",
    )
    session_end_parser.add_argument(
        "--backend",
        choices=["fts5", "vector"],
        default=None,
        help="Override configured search backend (default: read from "
        "athenaeum.yaml)",
    )
    session_end_parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory holding the ingest + index manifests "
        "(default: ~/.cache/athenaeum)",
    )
    session_end_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the compile without writing files, committing, updating the "
        "ingest stamp, or reindexing.",
    )
    session_end_parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging",
    )
    _add_lock_args(session_end_parser)
    session_end_parser.set_defaults(func=cmd_session_end)


def _reindex_summary(
    command: str,
    backend: str,
    mode: str,
    pages: int,
    t0: float,
    exit_code: int,
) -> None:
    """Print the one-line JSON reindex summary (issue athenaeum#349, counts+duration)."""
    import json
    import time

    print(
        json.dumps(
            {
                "command": command,
                "backend": backend,
                "mode": mode,
                "pages": pages,
                "duration_ms": int((time.monotonic() - t0) * 1000),
                "exit_code": exit_code,
            }
        )
    )


def cmd_compile_as_of(args: argparse.Namespace) -> int:
    """Issue athenaeum#359: recompile a historical wiki snapshot as-of a past date.

    Re-runs the deterministic C3 blend (no LLM) with ``--as-of`` threaded into
    the per-member active predicate, writing to ``--out``. Never mutates the
    live wiki or raw tree. Distinct from slice 3's read-time filter — see
    :func:`athenaeum.merge.compile_as_of`.
    """
    from athenaeum.config import load_config
    from athenaeum.merge import compile_as_of

    knowledge_root = args.path.expanduser().resolve()
    wiki_root = knowledge_root / "wiki"
    if not wiki_root.exists():
        print(f"Wiki directory not found: {wiki_root}", file=sys.stderr)
        return 1

    as_of = args.as_of
    out_dir = args.out.expanduser().resolve()
    if out_dir == wiki_root.expanduser().resolve():
        print(
            "--out must not be the live wiki directory; point it at a scratch "
            f"path (got {out_dir})",
            file=sys.stderr,
        )
        return 1

    cfg = load_config(knowledge_root)
    try:
        entries = compile_as_of(knowledge_root, as_of, out_dir, config=cfg)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(
        f"Recompiled {len(entries)} wiki entr"
        f"{'y' if len(entries) == 1 else 'ies'} as of "
        f"{as_of.isoformat()} into {out_dir} "
        "(compile-as-of; live wiki untouched)"
    )
    return 0


def cmd_registry(args: argparse.Namespace) -> int:
    """Issue athenaeum#453: compile the source-handle registry.json.

    Deterministic, LLM-free read of the wiki tree into an ``entity uid →
    handle set`` index. A missing wiki dir or zero populated handles is not
    an error — it yields a well-formed empty registry (issue athenaeum#453/#454: the
    seed lands later and must not gate the builder).
    """
    from athenaeum.atomic_io import atomic_write_text
    from athenaeum.registry import build_registry, render_registry

    knowledge_root = args.path.expanduser().resolve()
    wiki_root = knowledge_root / "wiki"
    registry = build_registry(wiki_root)
    text = render_registry(registry)

    if args.stdout:
        sys.stdout.write(text)
        return 0

    out_path = (
        args.out.expanduser().resolve()
        if args.out is not None
        else knowledge_root / "registry.json"
    )
    atomic_write_text(out_path, text)
    print(
        f"Wrote {registry['entity_count']} entit"
        f"{'y' if registry['entity_count'] == 1 else 'ies'} with source handles "
        f"to {out_path}"
    )
    return 0


def cmd_rebuild_index(args: argparse.Namespace) -> int:
    import time

    from athenaeum.config import (
        load_config,
        resolve_embedding_model,
        resolve_extra_intake_roots,
        resolve_index_globs,
        resolve_reindex_full_rehash_max_age_days,
    )
    from athenaeum.search import build_fts5_index, build_vector_index

    # Issue athenaeum#349: `reindex` is the canonical name; `rebuild-index` is a
    # back-compat alias routed here. Report whichever the user invoked.
    command = getattr(args, "command", "reindex") or "reindex"
    t0 = time.monotonic()

    knowledge_root = args.path.expanduser().resolve()
    wiki_root = knowledge_root / "wiki"
    cache_dir = resolve_cache_dir(args.cache_dir).resolve()

    if not wiki_root.exists():
        print(f"Wiki directory not found: {wiki_root}", file=sys.stderr)
        return 1

    cfg = load_config(knowledge_root)
    if args.backend is not None:
        backend = args.backend
    else:
        backend = cfg.get("search_backend", "fts5")

    extra_roots = resolve_extra_intake_roots(knowledge_root, cfg)
    include_globs, exclude_globs = resolve_index_globs(cfg)
    embedding_model = resolve_embedding_model(cfg)
    full_rehash_max_age_days = resolve_reindex_full_rehash_max_age_days(
        knowledge_root, cfg
    )
    incremental = not getattr(args, "full", False)

    # Issue athenaeum#308: an as-of index reflects a past date's validity windows and is
    # always a FULL build (a historical snapshot has no manifest to diff), so it
    # reports "full" regardless of the incremental default.
    as_of = getattr(args, "as_of", None)
    as_of_note = f" as of {as_of.isoformat()}" if as_of is not None else ""
    mode = "full" if (not incremental or as_of is not None) else "incremental"

    cache_dir.mkdir(parents=True, exist_ok=True)

    # Rebuilding the index always writes — acquire the run lock so it can't
    # race a concurrent `run` rebuilding the same index (issue athenaeum#309).
    lock = _acquire_or_exit(knowledge_root, args, cfg)
    if isinstance(lock, int):
        return lock
    try:
        if backend == "vector":
            try:
                count = build_vector_index(
                    wiki_root,
                    cache_dir,
                    extra_roots=extra_roots,
                    incremental=incremental,
                    include_globs=include_globs,
                    exclude_globs=exclude_globs,
                    embedding_model=embedding_model,
                    as_of=as_of,
                    full_rehash_max_age_days=full_rehash_max_age_days,
                    config=cfg,
                )
            except ImportError as exc:
                print(f"Vector backend unavailable: {exc}", file=sys.stderr)
                print("Install with: pip install athenaeum[vector]", file=sys.stderr)
                _reindex_summary(command, backend, mode, 0, t0, 1)
                return 1
            print(
                f"Vector index rebuilt{as_of_note} ({mode}): {count} pages "
                f"(wiki + {len(extra_roots)} extra root(s))"
            )
            _reindex_summary(command, backend, mode, count, t0, 0)
            return 0

        if backend == "fts5":
            count = build_fts5_index(
                wiki_root,
                cache_dir,
                extra_roots=extra_roots,
                incremental=incremental,
                include_globs=include_globs,
                exclude_globs=exclude_globs,
                as_of=as_of,
                full_rehash_max_age_days=full_rehash_max_age_days,
                config=cfg,
            )
            print(
                f"FTS5 index rebuilt{as_of_note} ({mode}): {count} pages "
                f"(wiki + {len(extra_roots)} extra root(s))"
            )
            _reindex_summary(command, backend, mode, count, t0, 0)
            return 0

        print(f"Unknown search backend: {backend}", file=sys.stderr)
        _reindex_summary(command, backend, mode, 0, t0, 1)
        return 1
    finally:
        lock.release()


def cmd_ingest(args: argparse.Namespace) -> int:
    """On-demand compile of new/changed raw intake (issue athenaeum#349).

    Thin CLI wrapper over :func:`athenaeum.librarian.ingest` — the single
    reusable incremental-ingest engine the SessionEnd path (athenaeum#350) also calls.
    Acquires the shared run lock (single-flight, athenaeum#309) around the compile,
    prints a one-line JSON summary (counts + duration), and exits non-zero
    when the underlying compile fails.

    ``--if-triggered`` (issue athenaeum#909) is an ADDITIVE control signal on this
    SAME command, not a second entry point — see :func:`_evaluate_ingest_trigger`.
    When given and no configured trigger fired, this function returns BEFORE
    the lock-acquire block below: no lock, no ``ingest()`` call, no mutation.
    """
    import json

    from athenaeum.config import load_config
    from athenaeum.librarian import DEFAULT_KNOWLEDGE_ROOT, ingest

    configure_logging(verbose=getattr(args, "verbose", False))

    knowledge_root = (
        args.path.expanduser().resolve() if args.path else DEFAULT_KNOWLEDGE_ROOT
    )
    raw_root = knowledge_root / "raw"
    wiki_root = knowledge_root / "wiki"
    # Default incremental: neither flag → None → True; --full → False.
    incremental = True if args.incremental is None else args.incremental

    # Issue athenaeum#909 (AC4/D7): a triggered run is a budgeted, resumable,
    # INCREMENTAL run — never a full recompile. ``--full`` and
    # ``--if-triggered`` are independent argparse flags (not a mutually
    # exclusive group — ``--full`` is meaningful without ``--if-triggered``
    # too), so this combination is otherwise silently reachable. Reject it
    # loudly rather than either silently downgrading ``--full`` (surprising:
    # the operator explicitly asked for it) or silently honoring it (the one
    # thing every trigger must never do).
    if getattr(args, "if_triggered", False) and not incremental:
        print(
            "error: --if-triggered cannot be combined with --full — a "
            "triggered run is always incremental (issue athenaeum#909); pass "
            "--full-compile to `athenaeum run` for an explicit full "
            "reconciliation instead.",
            file=sys.stderr,
        )
        return 1

    cfg = load_config(knowledge_root)

    # Issue athenaeum#909: on-demand (no --if-triggered, the pre-existing default
    # behaviour, UNCHANGED) always compiles. With --if-triggered, evaluate the
    # configured triggers against LIVE state FIRST — cheap (one directory
    # listing + one small stamp read), side-effect-free, and deliberately
    # BEFORE the lock-acquire below so a "nothing fired" evaluation never
    # contends for the run lock at all.
    trigger_reason = "on-demand"
    if getattr(args, "if_triggered", False):
        decision = _evaluate_ingest_trigger(raw_root, cfg, args.cache_dir)
        trigger_reason = decision.reason
        if not decision.fired:
            noop_summary: dict[str, object] = {
                "command": "ingest",
                "mode": "incremental" if incremental else "full",
                "new_or_changed": 0,
                "compiled": 0,
                "noop": True,
                "duration_ms": 0,
                "exit_code": 0,
                "trigger": "none",
            }
            if args.session is not None:
                noop_summary["session"] = args.session
            print(json.dumps(noop_summary))
            return 0

    # Issue athenaeum#309 single-flight: a real compile mutates wiki/ and shares the
    # nightly-run lock. A --dry-run reads only, so it does not take the lock.
    lock: RunLock | int | None = None
    if not args.dry_run:
        lock = _acquire_or_exit(knowledge_root, args, cfg)
        if isinstance(lock, int):
            return lock
    try:
        result = ingest(
            raw_root=raw_root,
            wiki_root=wiki_root,
            knowledge_root=knowledge_root,
            incremental=incremental,
            session=args.session,
            cache_dir=args.cache_dir,
            config=cfg,
            dry_run=args.dry_run,
            install_signal_handlers=not args.dry_run,
        )
    except Exception as exc:  # noqa: BLE001 — surface a clean JSON error line
        print(
            json.dumps(
                {
                    "command": "ingest",
                    "error": f"{type(exc).__name__}: {exc}",
                    "exit_code": 1,
                }
            )
        )
        return 1
    finally:
        if lock is not None and not isinstance(lock, int):
            lock.release()

    summary = result.summary()
    if getattr(args, "if_triggered", False):
        summary["trigger"] = trigger_reason
        # Issue athenaeum#909 (D4): advance the reasoning-trigger last-run stamp
        # on a clean, real (non-dry-run) completion, regardless of exit
        # code detail beyond "no exception" — a triggered run that ran to
        # completion (even a compile-internal noop) resets BOTH the
        # elapsed-interval and nightly-backstop clocks. A dry-run never
        # stamps, mirroring every other stamp in this module.
        if not args.dry_run and result.exit_code == 0:
            _record_ingest_trigger_completion(args.cache_dir)
    print(json.dumps(summary))
    return result.exit_code


def _evaluate_ingest_trigger(
    raw_root: Path, config: dict[str, Any], cache_dir: Path | None
) -> "TriggerDecision":
    """Gather live state and evaluate the reasoning-tier triggers (issue athenaeum#909).

    The ONLY I/O :func:`athenaeum.reasoning_triggers.evaluate_triggers` itself
    never performs: a raw-intake backlog scan (file count + byte size, both
    via :mod:`athenaeum.intake`) and a read of the last-completed-triggered-
    run stamp. Cheap (one directory listing, one small JSON file) and
    read-only — safe to call before the run lock is even considered.
    ``cache_dir`` MUST be the same ``--cache-dir`` value the real ingest call
    (and :func:`_record_ingest_trigger_completion`) uses, or this reads a
    stamp from the wrong location and evaluates against a stale/empty
    baseline every time.
    """
    from datetime import datetime, timezone

    from athenaeum.intake import discover_raw_backlog_bytes, discover_raw_files
    from athenaeum.librarian import REASONING_TRIGGER_STAMP_NAME, _load_timestamp_stamp
    from athenaeum.reasoning_triggers import evaluate_triggers

    backlog_files = len(discover_raw_files(raw_root, config))
    backlog_bytes = discover_raw_backlog_bytes(raw_root, config)
    stamp_path = resolve_cache_dir(cache_dir) / REASONING_TRIGGER_STAMP_NAME
    last_run = _load_timestamp_stamp(stamp_path)
    since_last_run = (
        None if last_run is None else datetime.now(timezone.utc) - last_run
    )
    return evaluate_triggers(
        backlog_files=backlog_files,
        backlog_bytes=backlog_bytes,
        since_last_run=since_last_run,
        on_demand=False,
        config=config,
    )


def _record_ingest_trigger_completion(cache_dir: Path | None) -> None:
    """Advance the reasoning-trigger last-run stamp (issue athenaeum#909).

    Best-effort: a write failure is logged and swallowed, never fails the
    (already-successful) ingest it is recording — mirrors every other
    cache-dir stamp writer in this codebase.
    """
    import logging

    from athenaeum.librarian import REASONING_TRIGGER_STAMP_NAME, _write_timestamp_stamp

    log = logging.getLogger(__name__)
    stamp_path = resolve_cache_dir(cache_dir) / REASONING_TRIGGER_STAMP_NAME
    try:
        from datetime import datetime, timezone

        _write_timestamp_stamp(stamp_path, datetime.now(timezone.utc))
    except Exception as exc:  # noqa: BLE001 — must not break a successful ingest
        log.warning("reasoning-trigger stamp write failed (non-fatal): %s", exc)


def cmd_session_end(args: argparse.Namespace) -> int:
    """Change-gated SessionEnd ingest + reindex (issue athenaeum#350).

    Thin CLI wrapper over :func:`athenaeum.librarian.session_end` — the single
    reusable composition the cwc SessionEnd hook and the nightly-after-librarian
    path invoke. Acquires the shared run lock (single-flight, athenaeum#309) ONCE around
    both the compile and the reindex, prints a one-line JSON summary (nested
    ingest counts + reindex pages + duration), and exits non-zero when the
    underlying compile fails.
    """
    import json

    from athenaeum.config import load_config
    from athenaeum.librarian import (
        DEFAULT_KNOWLEDGE_ROOT,
        SESSION_END_MAX_API_CALLS,
        SESSION_END_MAX_FILES,
        session_end,
        session_end_max_runtime,
    )

    configure_logging(verbose=getattr(args, "verbose", False))

    knowledge_root = (
        args.path.expanduser().resolve() if args.path else DEFAULT_KNOWLEDGE_ROOT
    )
    raw_root = knowledge_root / "raw"
    wiki_root = knowledge_root / "wiki"
    incremental = True if args.incremental is None else args.incremental

    # Kill switch (athenaeum#379): the compile/detect pass is the expensive, unattended
    # ``claude -p`` fan-out — honour the disabled flag BEFORE the lock or any
    # work, so 'athenaeum disable' (or --compile) prevents the next pass with
    # no pkill needed. Emit a JSON no-op line so callers tailing the pipe see it.
    from athenaeum.killswitch import current_state, is_disabled

    if is_disabled("compile", cache_dir=args.cache_dir):
        state = current_state(args.cache_dir)
        print(
            json.dumps(
                {
                    "command": "session-end",
                    "noop": True,
                    "reason": "disabled",
                    "scope": state.scope,
                    "source": state.source,
                }
            )
        )
        sys.stdout.flush()
        return 0

    cfg = load_config(knowledge_root)

    # Issue athenaeum#309 single-flight: the compile + reindex both mutate on-disk state
    # (wiki/ and the index) and share the nightly-run lock. A --dry-run reads
    # only, so it does not take the lock.
    lock: RunLock | int | None = None
    if not args.dry_run:
        lock = _acquire_or_exit(knowledge_root, args, cfg)
        if isinstance(lock, int):
            return lock
    # Issue athenaeum#896: derive the INNER wall-clock deadline from the
    # SessionEnd wrapper's OUTER kill timeout (``KNOWLEDGE_REBUILD_TIMEOUT``,
    # default 900s) instead of falling through to the nightly-run
    # ``DEFAULT_MAX_RUNTIME`` (3600s) — 4x the outer default, which meant the
    # graceful-stop path could never win the race against the wrapper's
    # external ``timeout``. ``max_files``/``max_api_calls`` are likewise
    # explicit, session-scoped-incremental-sized caps rather than the
    # nightly-run defaults (see the constants' docstrings in librarian.py).
    try:
        result = session_end(
            raw_root=raw_root,
            wiki_root=wiki_root,
            knowledge_root=knowledge_root,
            incremental=incremental,
            session=args.session,
            cache_dir=args.cache_dir,
            config=cfg,
            backend=args.backend,
            dry_run=args.dry_run,
            install_signal_handlers=not args.dry_run,
            max_runtime=session_end_max_runtime(cfg),
            max_files=SESSION_END_MAX_FILES,
            max_api_calls=SESSION_END_MAX_API_CALLS,
        )
    except Exception as exc:  # noqa: BLE001 — surface a clean JSON error line
        print(
            json.dumps(
                {
                    "command": "session-end",
                    "error": f"{type(exc).__name__}: {exc}",
                    "exit_code": 1,
                }
            )
        )
        return 1
    finally:
        if lock is not None and not isinstance(lock, int):
            lock.release()

    print(json.dumps(result.summary()))
    # Issue athenaeum#370: the summary line is the only stdout; flush both streams so a
    # caller tailing the pipe sees the result immediately (the reindex can run
    # for minutes and the run otherwise looks like a silent hang).
    sys.stdout.flush()
    sys.stderr.flush()
    return result.exit_code
