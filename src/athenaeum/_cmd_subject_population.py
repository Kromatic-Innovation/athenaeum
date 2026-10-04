# SPDX-License-Identifier: Apache-2.0
"""``athenaeum subject-population`` — operator CLI over
:mod:`athenaeum.subject_population` (issue athenaeum#1944).

athenaeum#1244's ``subject`` backfill needed a real operator-facing entry
point before the live corpus pass in athenaeum#1945 could run at all:
:mod:`athenaeum.subject_population` is library-only (see its own "CLI
surface: deliberately none" note) and that note explicitly asks a future
issue to pick a name OTHER than ``subject`` — ``tests/test_subject_backfill.py``
pins that literal token absent from the top-level subcommand choices. This
module is that future issue, named ``subject-population``.

**Dry run by default.** The default mode (no ``--from-report``) runs the
LLM-backed collection pass (:func:`athenaeum.subject_population.
build_subject_population_report`) and writes one JSONL row per decision to
``--report`` as soon as it is made — it NEVER writes to the wiki itself,
regardless of any other flag. Writing to the wiki happens only in a
SEPARATE invocation: ``--from-report PATH --apply``, which replays a
previously-collected report at zero LLM spend and constructs no LLM
client at all. The two modes are mutually exclusive by construction
(``--apply`` without ``--from-report``, or ``--from-report`` without
``--apply``, both refuse before doing anything) — there is deliberately no
"collect and apply in one shot" path, because the live collection pass is
the one that spends real tokens and the live apply is the one that touches
the git-tracked knowledge store; keeping them separate invocations is what
lets an interrupted collection run be ``--resume``-d or inspected before
anything is ever written.

**Spend discipline.** Before the collection pass starts, this command
refuses (fail-closed, no override flag) unless the ``classify`` knob
resolves to the ``claude-cli`` subscription provider
(:func:`athenaeum.provider.resolve_provider`) — the live run must never
spend metered API dollars. Once running, :func:`athenaeum.subject_population.
build_subject_population_report`'s ``ceiling_check`` hook is wired to
:func:`athenaeum.spend.ceiling_tripped` against this run's own
:class:`~athenaeum.models.TokenUsage`, checked before every page that would
otherwise reach the confirmer; a trip stops the run cleanly (non-zero exit,
the report already written so far is resumable via ``--resume``).

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` — see
``cli.py``'s module docstring.

Layering: L5 (presentation). Imports only stdlib plus sibling L5 helpers
(``_cli_shared``) and, lazily inside the handler functions (this module's
own convention, matching every other ``_cmd_*``), the L2-L4 modules the
two modes actually need (``athenaeum.config``, ``athenaeum.provider``,
``athenaeum.spend``, ``athenaeum.quiesce``, ``athenaeum.wiki_dedupe``,
``athenaeum.subject_population``).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from athenaeum._cli_shared import _acquire_or_exit, _add_lock_args, _positive_int
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT

if TYPE_CHECKING:
    from athenaeum.runlock import RunLock
    from athenaeum.subject_population import PageDecision


def _default_report_path() -> Path:
    """``~/.cache/athenaeum/1944/subject-population-<UTC ts>.jsonl`` (the
    issue's own default — host-side, never committed, one file per run)."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return (
        Path.home() / ".cache" / "athenaeum" / "1944" / f"subject-population-{ts}.jsonl"
    )


def _resolve_knowledge_root(args: argparse.Namespace) -> Path:
    return (getattr(args, "path", None) or DEFAULT_KNOWLEDGE_ROOT).expanduser().resolve()


def _read_report_rows(path: Path) -> list["PageDecision"]:
    """Thin re-export of :func:`athenaeum.subject_population.
    read_decision_report` under this module's existing private name --
    that module owns the JSONL row shape (:func:`~athenaeum.
    subject_population.decision_to_row` / ``decision_from_row``) as the
    single source of truth, shared with
    :mod:`athenaeum.coordinate_coverage`'s ``--pairs-from-report`` reader.
    """
    from athenaeum.subject_population import read_decision_report

    return read_decision_report(path)


def _git_uncommitted_targets(knowledge_root: Path, paths: list[Path]) -> list[Path]:
    """Return the subset of *paths* ``git status --porcelain`` reports as
    having any local change (modified, staged, or untracked) under
    *knowledge_root*. A ``git`` invocation failure (missing binary, not a
    repo) fails CLOSED -- every existing path is reported dirty, never
    silently treated as clean.
    """
    existing = [p for p in paths if p.exists()]
    if not existing:
        return []
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--", *[str(p) for p in existing]],
            cwd=str(knowledge_root),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return list(existing)
    if result.returncode != 0:
        return list(existing)
    dirty: set[Path] = set()
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        # Porcelain v1 short format: two status chars, a space, then the
        # path (rename entries use "orig -> new"; the destination is what
        # matters here, and it is always the text after "-> ").
        rel = line[3:]
        if " -> " in rel:
            rel = rel.split(" -> ", 1)[1]
        dirty.add((knowledge_root / rel.strip()).resolve())
    return [p for p in existing if p.resolve() in dirty]


def cmd_subject_population(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum subject-population`` to one of its two mutually
    exclusive modes."""
    if args.from_report is not None:
        if not args.apply:
            print(
                "error: --from-report requires --apply (a report with no "
                "--apply is just a file to read with any other tool; this "
                "command has nothing else to do with it)",
                file=sys.stderr,
            )
            return 2
        return _cmd_apply_from_report(args)
    if args.apply:
        print(
            "error: --apply requires --from-report -- apply is always a "
            "replay of a previously-collected report, never inline with "
            "collection",
            file=sys.stderr,
        )
        return 2
    return _cmd_collect(args)


def _cmd_collect(args: argparse.Namespace) -> int:
    knowledge_root = _resolve_knowledge_root(args)
    wiki_root = knowledge_root / "wiki"
    if not wiki_root.is_dir():
        print(f"error: no wiki directory at {wiki_root}", file=sys.stderr)
        return 1

    from athenaeum.config import load_config
    from athenaeum.provider import ProviderConfigError, build_llm_client, resolve_provider

    config = load_config(knowledge_root)

    try:
        provider = resolve_provider(config, knob="classify")
    except ProviderConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if provider != "claude-cli":
        print(
            "error: athenaeum subject-population refuses to run unless the "
            "'classify' knob resolves to the claude-cli (subscription) "
            f"provider; resolved {provider!r}. There is no override flag -- "
            "the live confirmer pass must never spend metered API dollars.",
            file=sys.stderr,
        )
        return 1

    client = build_llm_client(config, knob="classify")
    if client is None:
        print(
            "error: no LLM client could be constructed for the 'classify' "
            "knob (is the claude-cli binary installed and logged in?)",
            file=sys.stderr,
        )
        return 1

    from athenaeum.models import TokenUsage
    from athenaeum.search import embed_texts
    from athenaeum.spend import ceiling_tripped
    from athenaeum.subject_population import (
        SubjectRegistry,
        build_subject_population_report,
        build_tier2_confirm,
        decision_to_row,
    )

    usage = TokenUsage()
    confirm = build_tier2_confirm(client, config=config, usage=usage)

    # Per-process memo keyed on the EXACT (already-normalized) text
    # resolve_same_subject passes the embedder -- resolve_same_subject
    # re-embeds the whole pool for every candidate; this is the only
    # change needed to avoid paying for the same name's vector twice
    # within one run (entity_resolution.py itself is unchanged).
    embed_cache: dict[str, list[float]] = {}

    def memoized_embed(texts: list[str]) -> "list[list[float]] | None":
        missing = [t for t in texts if t not in embed_cache]
        if missing:
            vectors = embed_texts(missing)
            if vectors is None:
                return None
            for text, vector in zip(missing, vectors):
                embed_cache[text] = vector
        return [embed_cache[t] for t in texts]

    report_path = (
        args.resume.expanduser().resolve()
        if args.resume
        else (args.report.expanduser().resolve() if args.report else _default_report_path())
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)

    prior_decisions = _read_report_rows(report_path) if args.resume else []
    write_mode = "a" if args.resume else "w"

    def ceiling_check() -> str | None:
        return ceiling_tripped(
            usage,
            provider=provider,
            config=config,
            cache_dir=args.cache_dir,
        )

    registry = SubjectRegistry()

    with report_path.open(write_mode, encoding="utf-8") as fh:

        def on_decision(decision: "PageDecision") -> None:
            fh.write(json.dumps(decision_to_row(decision)) + "\n")
            fh.flush()

        report = build_subject_population_report(
            wiki_root,
            embedder=memoized_embed,
            confirm=confirm,
            config=config,
            registry=registry,
            types=args.types,
            limit=args.limit,
            on_decision=on_decision,
            prior_decisions=prior_decisions,
            ceiling_check=ceiling_check,
        )

    confirmer_calls = sum(1 for d in report.decisions if d.confirmer_ran)

    if args.json:
        payload: dict[str, Any] = dict(report.counts())
        payload.update(
            {
                "stopped_reason": report.stopped_reason,
                "stopped_due_to_ceiling": report.stopped_due_to_ceiling,
                "report_path": str(report_path),
                "confirmer_calls": confirmer_calls,
                "tokens_used": usage.billable_tokens,
            }
        )
        sys.stdout.write(json.dumps(payload) + "\n")
    else:
        counts = report.counts()
        print(f"scanned: {counts['scanned']} page(s) under {wiki_root}")
        print(f"  matched_existing_subject: {counts['matched_existing_subject']}")
        print(f"  minted_new_subject: {counts['minted_new_subject']}")
        print(f"  undeterminable: {counts['undeterminable']}")
        print(f"confirmer calls: {confirmer_calls}, tokens used: {usage.billable_tokens}")
        print(f"report written to: {report_path}")
        if report.stopped_reason:
            print(f"STOPPED EARLY: {report.stopped_reason}")
            print(f"resumable: athenaeum subject-population --resume {report_path}")

    if report.stopped_due_to_ceiling:
        return 3
    return 0


def _cmd_apply_from_report(args: argparse.Namespace) -> int:
    knowledge_root = _resolve_knowledge_root(args)
    if not (knowledge_root / ".git").exists():
        print(f"error: {knowledge_root} is not a git repository", file=sys.stderr)
        return 1

    lock: "RunLock | int" = _acquire_or_exit(knowledge_root, args, config=None)
    if isinstance(lock, int):
        return lock

    try:
        report_path = args.from_report.expanduser().resolve()
        decisions = _read_report_rows(report_path)
        if not decisions:
            print(f"error: no decisions found in report {report_path}", file=sys.stderr)
            return 1

        target_paths = sorted({d.path for d in decisions})
        dirty = _git_uncommitted_targets(knowledge_root, target_paths)
        if dirty:
            print(
                "error: refusing to apply -- the following target page(s) "
                "have uncommitted changes: "
                + ", ".join(str(p) for p in dirty),
                file=sys.stderr,
            )
            return 1

        from athenaeum.quiesce import read_quiesce_state

        if read_quiesce_state(knowledge_root) is None:
            print(
                "warning: no active `athenaeum quiesce` sentinel for "
                f"{knowledge_root} -- applying while the live store may "
                "still be written by another process.",
                file=sys.stderr,
            )

        from athenaeum.subject_population import (
            SUBJECT_REGISTRY_FILENAME,
            SubjectPopulationReport,
            SubjectRegistry,
            apply_subject_population,
        )

        wiki_root = knowledge_root / "wiki"
        registry = SubjectRegistry.load(wiki_root / SUBJECT_REGISTRY_FILENAME)
        for decision in decisions:
            if decision.reason == "minted":
                registry.seed_minted(
                    decision.subject, decision.uid, confirmer_ran=decision.confirmer_ran
                )
            elif decision.reason == "matched":
                registry.record_match(
                    decision.subject, decision.uid, confirmer_ran=decision.confirmer_ran
                )

        replay_report = SubjectPopulationReport(scanned=len(decisions), decisions=decisions)
        changed = apply_subject_population(replay_report, registry, wiki_root=wiki_root)

        if args.json:
            sys.stdout.write(
                json.dumps(
                    {
                        "applied_from": str(report_path),
                        "decisions_replayed": len(decisions),
                        "files_changed": changed,
                    }
                )
                + "\n"
            )
        else:
            print(f"applied from: {report_path}")
            print(f"decisions replayed: {len(decisions)}")
            print(f"files changed: {changed}")
            print("not committed -- the operator commits.")
        return 0
    finally:
        if not isinstance(lock, int):
            lock.release()


def add_subject_population_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum subject-population`` (issue athenaeum#1944).

    Deliberately NOT named ``subject`` -- see this module's docstring and
    ``tests/test_subject_backfill.py::test_subject_is_not_a_known_top_level_command``.
    """
    from athenaeum.wiki_dedupe import DEDUPE_CANDIDATE_TYPES

    parser = subparsers.add_parser(
        "subject-population",
        help="Meaning-based subject: population over the comparator-eligible "
        "wiki pages (concept/reference/principle) -- the operator CLI over "
        "athenaeum.subject_population (issue athenaeum#1944). Default is "
        "dry run (streams a JSONL report); apply happens separately, from "
        "that report, via --from-report PATH --apply.",
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge).",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Where to stream the JSONL decision report (default: "
        "~/.cache/athenaeum/1944/subject-population-<UTC timestamp>.jsonl). "
        "Ignored when --resume is given (the resumed path IS the report "
        "path).",
    )
    parser.add_argument(
        "--resume",
        type=Path,
        default=None,
        metavar="PATH",
        help="Resume a previously-interrupted collection run: read PATH's "
        "existing decision rows, skip those uids, and append new decisions "
        "to the SAME file. Produces a report identical to an uninterrupted "
        "run given the same embedder/confirmer answers.",
    )
    parser.add_argument(
        "--types",
        nargs="+",
        choices=sorted(DEDUPE_CANDIDATE_TYPES),
        default=None,
        help="Restrict the pass to this subset of comparator-eligible "
        "types (default: all three).",
    )
    parser.add_argument(
        "--limit",
        type=_positive_int,
        default=None,
        help="Stop after this many NEWLY-decided pages this run (a "
        "deliberate, zero-error pause -- never counts a replayed "
        "--resume row).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply a report's decisions to the wiki. Requires "
        "--from-report; refused otherwise. Never used with a fresh "
        "collection run -- apply always replays a report, at zero LLM "
        "spend.",
    )
    parser.add_argument(
        "--from-report",
        type=Path,
        default=None,
        metavar="PATH",
        help="Replay PATH's decisions (zero LLM spend, no LLM client "
        "constructed). Requires --apply; refused otherwise. Refuses unless "
        "--path is a git repository, the run lock can be acquired, and no "
        "target page has uncommitted changes. Warns (does not refuse) when "
        "no `athenaeum quiesce` sentinel is active. Never commits -- the "
        "operator commits.",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory holding the spend ledger consulted for the "
        "per-day ceiling (default: ATHENAEUM_CACHE_DIR env or "
        "~/.cache/athenaeum). Collection mode only.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable JSON summary instead of plain text.",
    )
    _add_lock_args(parser)
    parser.set_defaults(func=cmd_subject_population)
