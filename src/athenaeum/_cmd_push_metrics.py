# SPDX-License-Identifier: Apache-2.0
"""``athenaeum push-metrics {baseline,coverage-audit,liveness,record,tail}``
— v6 MVP (a), issue athenaeum#711; ``liveness`` added by issue
athenaeum#1422, ``record`` by issue athenaeum#1478, ``tail`` by issue
athenaeum#1479.

Five subcommands over :mod:`athenaeum.push_metrics`:

- ``baseline``       compute precision + coverage over a stated window and
                       write/append a dated snapshot into
                       ``docs/measurements/memory-model-measurements.md`` — unless
                       ``--dry-run`` was passed (inspect only, never write),
                       or the window has zero reference-determination
                       records, in which case the write is REFUSED (issue
                       athenaeum#795: precision is not computable against a
                       dead instrument, so there is nothing meaningful to
                       persist — a prior version wrote a placeholder here
                       unconditionally).
- ``coverage-audit``  sample N sessions' push records and emit a FILE
                       worksheet of the structural facts derivable from
                       hash-only records (candidate-pool size, tier/scope
                       concentration, window-mate filter removal, policy-set
                       bounds) — never a per-candidate relevance marking or a
                       measured miss rate (issue athenaeum#1036).
- ``liveness``        read-only assertion (issue athenaeum#1422):
                       PASS/FAIL/INCONCLUSIVE on whether the most recent
                       :data:`athenaeum.push_metrics.LIVENESS_WINDOW` rows
                       include at least one ``"source":"sidecar"`` row.
                       Exits non-zero on FAIL for CI/manual invocation; the
                       automatic path this issue's AC5 requires is
                       :func:`athenaeum.librarian.session_end`, which calls
                       the same underlying assertion on every invocation —
                       see that function's docstring for why THAT call site
                       (not a scheduled GitHub Actions job) is the one that
                       actually has the ledger to read.
- ``record``          the hook-path push-recording entry point (issue
                       athenaeum#1478): the per-turn ``UserPromptSubmit``
                       recall hook (``code-workspace-config#3227``, a separate
                       repo, out of this repo's scope) calls this,
                       fire-and-forget, with the session id and the ids it
                       actually injected, so that recall moment stops being
                       invisible to the push ledger. Thin argv/stdin parsing
                       over :func:`athenaeum.push_metrics.record_hook_push` —
                       all the recording logic lives there.
- ``tail``             stream NDJSON — one shaped object per push record and
                       per reference-determination record, newest-last,
                       optionally filtered by ``--session``/``--since`` and
                       optionally following the ledgers as they grow
                       (``--follow``) (issue athenaeum#1479). Read-only;
                       never mutates the ledgers. This is the documented
                       public contract over the ledgers — see
                       docs/reference/configuration.md ("push-metrics tail —
                       the public NDJSON contract") for the exact ``--json``
                       shape, schema version, and compatibility note. Thin
                       argv parsing + output shaping over
                       :func:`athenaeum.push_metrics.tail_records` — all the
                       draining/filtering/follow logic lives there.

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` —
mirrors :mod:`athenaeum._cmd_calibration`'s shape. ``record`` (issue
athenaeum#1478) and ``tail`` (issue athenaeum#1479) are both added here
rather than a new module because ``push-metrics`` already has this one file
— keeps ``push-metrics``'s subcommands cohesive and localised in one place.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from athenaeum._cli_shared import _resolve_wiki_root
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT


def cmd_push_metrics(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum push-metrics {baseline,coverage-audit,liveness,record,tail}``."""
    sub = getattr(args, "push_metrics_target", None)
    if sub not in ("baseline", "coverage-audit", "liveness", "record", "tail"):
        print(
            "usage: athenaeum push-metrics "
            "{baseline,coverage-audit,liveness,record,tail} [...]",
            file=sys.stderr,
        )
        return 2

    if sub == "record":
        return _cmd_push_metrics_record(args)

    if sub == "tail":
        return _cmd_push_metrics_tail(args)

    from athenaeum import push_metrics

    if sub == "liveness":
        window = args.window if args.window is not None else push_metrics.LIVENESS_WINDOW
        result = push_metrics.check_sidecar_liveness(
            cache_dir=args.cache_dir,
            wiki_root=_resolve_wiki_root(args),
            window=window,
        )
        if args.json:
            sys.stdout.write(json.dumps(result.to_dict()) + "\n")
        else:
            print(result.message)
        return 1 if result.outcome == push_metrics.LIVENESS_FAIL else 0

    if sub == "baseline":
        since = None
        if getattr(args, "since", None):
            from athenaeum.spend import parse_since

            since = parse_since(args.since)

        try:
            baseline = push_metrics.compute_baseline(
                since=since,
                cache_dir=args.cache_dir,
                exclude_sessions=getattr(args, "exclude_session", None),
                wiki_root=_resolve_wiki_root(args),
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        docs_path = args.docs_path.expanduser().resolve()
        dry_run = getattr(args, "dry_run", False)

        if not dry_run:
            try:
                push_metrics.write_snapshot(baseline, docs_path=docs_path)
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1

        if args.json:
            payload = baseline.to_dict()
            payload["dry_run"] = dry_run
            sys.stdout.write(json.dumps(payload) + "\n")
        else:
            precision_str = (
                f"{baseline.precision:.4f}"
                if baseline.precision is not None
                else "n/a — accrues as sessions run"
            )
            excluded_sessions_str = (
                ",".join(baseline.excluded_sessions) if baseline.excluded_sessions else "none"
            )
            snapshot_line = (
                "dry run: no snapshot written (pass without --dry-run to write)"
                if dry_run
                else f"snapshot written to: {docs_path}"
            )
            print(
                f"window: {baseline.start} .. {baseline.end}\n"
                f"sessions: {baseline.session_count}\n"
                f"push_records: {baseline.push_record_count}\n"
                f"reference_records: {baseline.reference_record_count}\n"
                f"precision: {precision_str}\n"
                f"excluded_sessions: {excluded_sessions_str}\n"
                f"excluded_push_records: {baseline.excluded_push_record_count}\n"
                f"excluded_reference_records: {baseline.excluded_reference_record_count}\n"
                f"athenaeum_version: {baseline.athenaeum_version}\n"
                f"git_sha: {baseline.git_sha}\n"
                f"{snapshot_line}"
            )
        return 0

    # sub == "coverage-audit"
    wiki_root = _resolve_wiki_root(args)
    try:
        worksheet = push_metrics.build_coverage_worksheet(
            n=args.n,
            wiki_root=wiki_root,
            cache_dir=args.cache_dir,
            seed=getattr(args, "seed", None),
            exclude_sessions=getattr(args, "exclude_session", None),
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    output_path = args.output.expanduser().resolve()
    push_metrics.write_coverage_worksheet(worksheet, output_path=output_path)

    if args.json:
        sys.stdout.write(json.dumps(worksheet) + "\n")
    else:
        excluded_sessions_str = (
            ",".join(worksheet["excluded_sessions"]) if worksheet["excluded_sessions"] else "none"
        )
        print(
            f"sampled {worksheet['sampled_session_count']} session(s) -> "
            f"{output_path}\n"
            f"excluded_sessions: {excluded_sessions_str}\n"
            f"excluded_push_records: {worksheet['excluded_push_records']}\n"
            "This worksheet reports structural facts only (candidate-pool "
            "size, tier/scope concentration, window-mate filter removal, "
            "policy-set bounds) — see the worksheet's own 'limitation' "
            "field for why a measured coverage miss rate is not "
            "recoverable from hash-only push records."
        )
    return 0


def _cmd_push_metrics_record(args: argparse.Namespace) -> int:
    """``athenaeum push-metrics record`` — the hook-path push-recording
    entry point (issue athenaeum#1478).

    Fire-and-forget by design (this issue's own decided approach, option
    2): the caller — the per-turn ``UserPromptSubmit`` recall hook,
    ``code-workspace-config#3227``, out of this repo's scope — is specified
    to invoke this in the background without checking its output, so this
    subcommand always exits ``0``. A recording failure is never fully
    silent (see :func:`athenaeum.push_metrics.record_hook_push`'s own
    ``log.debug`` contract), but it is also never surfaced as a nonzero
    exit a fire-and-forget caller was never going to inspect.

    Accepts either argv flags or a JSON stdin payload (``--stdin-json``,
    mirroring ``athenaeum context --stdin-json``'s established hook-input
    convention): ``{"session_id": ..., "ids": [...], "query": ...,
    "backend": ...}``. When ``--stdin-json`` is passed, its ``ids`` array
    (when present) REPLACES any ``--id`` flags rather than merging with
    them, and its ``session_id``/``query``/``backend`` keys take precedence
    over the matching flags only when non-empty — so a caller can mix a
    fixed ``--session-id`` flag with a per-call stdin ``ids`` array if it
    wants to.
    """
    from athenaeum import push_metrics

    session_id = args.session_id
    ids = list(args.id or [])
    query = args.query or ""
    backend = args.backend or ""

    if args.stdin_json:
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            payload = {}
        session_id = payload.get("session_id") or session_id
        payload_ids = payload.get("ids")
        if isinstance(payload_ids, list):
            ids = [str(i) for i in payload_ids]
        query = payload.get("query") or query
        backend = payload.get("backend") or backend

    if not session_id:
        session_id = push_metrics.resolve_session_id()

    wrote = push_metrics.record_hook_push(
        session_id,
        ids,
        query=query,
        backend=backend,
        cache_dir=args.cache_dir,
        wiki_root=_resolve_wiki_root(args),
    )
    if args.json:
        sys.stdout.write(json.dumps({"wrote": wrote}) + "\n")
    return 0


def _print_tail_row(rec: dict) -> None:
    """Plain-text rendering of one shaped tail record (see
    :func:`athenaeum.push_metrics.tail_records`) — a one-line-per-record
    summary; ``--json`` is the documented, stable machine-readable shape."""
    if rec["record_type"] == "push":
        source = rec.get("source") or "recall"
        print(
            f"push  session={rec['session_id']} ts={rec['ts']} source={source} "
            f"backend={rec['backend']} pushed={rec['pushed_count']} "
            f"token_cost={rec['token_cost']}"
        )
    else:
        print(
            f"ref   session={rec['session_id']} ts={rec['ts']} "
            f"pushed={rec['pushed_count']} referenced={rec['referenced_count']} "
            f"precision={rec['precision']}"
        )


def _cmd_push_metrics_tail(args: argparse.Namespace) -> int:
    """``athenaeum push-metrics tail`` — the documented NDJSON contract over
    the push / reference-determination ledgers (issue athenaeum#1479).

    Read-only, thin argv parsing + output shaping over
    :func:`athenaeum.push_metrics.tail_records` — all the
    draining/filtering/follow logic lives there.
    """
    from athenaeum import push_metrics

    since = None
    if getattr(args, "since", None):
        from athenaeum.spend import parse_since

        since = parse_since(args.since)

    records = push_metrics.tail_records(
        cache_dir=args.cache_dir,
        wiki_root=_resolve_wiki_root(args),
        session_id=getattr(args, "session", None),
        since=since,
        follow=getattr(args, "follow", False),
    )
    try:
        for rec in records:
            if args.json:
                sys.stdout.write(json.dumps(rec, separators=(",", ":")) + "\n")
            else:
                _print_tail_row(rec)
            sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    return 0


def add_push_metrics_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum push-metrics`` and its three modes on ``subparsers``."""
    p_parser = subparsers.add_parser(
        "push-metrics",
        help=(
            "Push-precision + coverage baseline: compute/record the "
            "precision snapshot, sample sessions for a human-reviewed "
            "coverage-audit worksheet (issue athenaeum#711), record a "
            "single hook-path push (issue athenaeum#1478), and stream the "
            "documented NDJSON tail contract over the ledgers (issue "
            "athenaeum#1479)."
        ),
    )
    p_parser.set_defaults(func=cmd_push_metrics)
    p_sub = p_parser.add_subparsers(dest="push_metrics_target")

    def _add_common(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--path",
            type=Path,
            default=DEFAULT_KNOWLEDGE_ROOT,
            help="Knowledge directory (default: ~/knowledge)",
        )
        parser.add_argument(
            "--cache-dir",
            type=Path,
            default=None,
            help="Cache directory holding the push-metrics ledgers "
            "(default: ATHENAEUM_CACHE_DIR env or ~/.cache/athenaeum)",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit machine-readable JSON instead of plain text.",
        )

    baseline_p = p_sub.add_parser(
        "baseline",
        help="Compute precision + coverage over a window; write the dated "
        "snapshot to docs/measurements/memory-model-measurements.md. Refuses to write "
        "(exit 1) when the window has zero reference-determination records. "
        "See --dry-run to inspect without writing.",
    )
    _add_common(baseline_p)
    baseline_p.add_argument(
        "--since",
        default=None,
        help="Window lower bound: relative (7d/24h/30m/2w) or absolute "
        "ISO-8601. Default: the whole ledger (instrument-enabled to now).",
    )
    baseline_p.add_argument(
        "--docs-path",
        type=Path,
        default=Path("docs/measurements/memory-model-measurements.md"),
        help="Where the snapshot section is written/appended "
        "(default: docs/measurements/memory-model-measurements.md).",
    )
    baseline_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and display the baseline without writing to "
        "--docs-path. This is the read-only way to check whether a baseline "
        "is computable (issue athenaeum#795) — combine with --json for a "
        "read-only machine-readable inspection. Note: --json alone does NOT "
        "suppress the write; use --dry-run for that.",
    )
    baseline_p.add_argument(
        "--exclude-session",
        action="append",
        default=None,
        metavar="SESSION_ID",
        help="Exclude a KNOWN-synthetic session id (e.g. one that ran the "
        "test suite and leaked fixture pushes into the ledger, issue "
        "athenaeum#791) from the precision/session counts. Repeatable. "
        "Excluded sessions and their record counts are always reported, "
        "never silently dropped. Accepts the full session id or an "
        "unambiguous prefix of exactly one known session id (issue "
        "athenaeum#987); a value matching zero or multiple known session "
        "ids is a hard error (exit 1), never a silent zero-effect success.",
    )

    coverage_p = p_sub.add_parser(
        "coverage-audit",
        help="Sample N sessions' push records into a worksheet of the "
        "structural facts hash-only records support (candidate-pool size, "
        "tier/scope concentration, filter removal, policy-set bounds) — "
        "never a per-candidate marking or a measured miss rate "
        "(athenaeum#1036).",
    )
    _add_common(coverage_p)
    coverage_p.add_argument(
        "--n",
        type=int,
        default=10,
        help="Number of sessions to sample (default: 10).",
    )
    coverage_p.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Optional deterministic sample seed (test/repro seam).",
    )
    coverage_p.add_argument(
        "--output",
        type=Path,
        default=Path("coverage-audit-worksheet.json"),
        help="Worksheet output file (default: ./coverage-audit-worksheet.json).",
    )
    coverage_p.add_argument(
        "--exclude-session",
        action="append",
        default=None,
        metavar="SESSION_ID",
        help="Exclude a KNOWN-synthetic session id (same semantics as "
        "`baseline --exclude-session`, issue athenaeum#791) from being "
        "sampled and from other sessions' candidate lists. Repeatable. "
        "Excluded sessions and their record counts are always reported, "
        "never silently dropped. Accepts the full session id or an "
        "unambiguous prefix of exactly one known session id (issue "
        "athenaeum#987); a value matching zero or multiple known session "
        "ids is a hard error (exit 1), never a silent zero-effect success.",
    )

    liveness_p = p_sub.add_parser(
        "liveness",
        help="Read-only assertion (issue athenaeum#1422): PASS if any of the "
        "most recent --window rows is sidecar-tagged, FAIL (exit 1) if "
        "--window rows exist and none is, INCONCLUSIVE (exit 0) if fewer "
        "than --window rows are recorded (including an absent ledger).",
    )
    _add_common(liveness_p)
    liveness_p.add_argument(
        "--window",
        type=int,
        default=None,
        help="Row-count window to check (default: "
        "athenaeum.push_metrics.LIVENESS_WINDOW).",
    )
    record_p = p_sub.add_parser(
        "record",
        help="Record one hook-path push (issue athenaeum#1478): the "
        "fire-and-forget entry point the per-turn UserPromptSubmit recall "
        "hook calls with the session id and the ids it actually injected. "
        "Writes a push record tagged source=hook, distinct from an "
        "explicit MCP `recall` push (no source key) and the `athenaeum "
        "context` sidecar adapter (source=sidecar, issue athenaeum#1362). "
        "Always exits 0 — see this subcommand's own docstring.",
    )
    _add_common(record_p)
    record_p.add_argument(
        "--session-id",
        default=None,
        help="Consuming session id. Falls back to CLAUDE_CODE_SESSION_ID / "
        "CLAUDE_SESSION_ID (push_metrics.resolve_session_id) when omitted "
        "and --stdin-json was not passed, or its payload carried none.",
    )
    record_p.add_argument(
        "--id",
        action="append",
        default=None,
        metavar="PUSHED_ID",
        help="One id actually injected into the turn. Repeatable. Replaced "
        "entirely (not merged) when --stdin-json supplies an `ids` array.",
    )
    record_p.add_argument(
        "--query",
        default=None,
        help="Optional raw query text for this push — only its hash is "
        "ever retained.",
    )
    record_p.add_argument(
        "--backend",
        default=None,
        help="Optional retrieval-backend label, recorded as-is.",
    )
    record_p.add_argument(
        "--stdin-json",
        action="store_true",
        help='Read {"session_id": ..., "ids": [...], "query": ..., '
        '"backend": ...} from stdin (hook-input shape, mirrors '
        "`athenaeum context --stdin-json`).",
    )

    tail_p = p_sub.add_parser(
        "tail",
        help="Stream NDJSON — one object per push record and per "
        "reference-determination record, newest-last (issue athenaeum#1479). "
        "Read-only; never mutates the ledgers. See "
        "docs/reference/configuration.md ('push-metrics tail — the public "
        "NDJSON contract') for the documented --json record shape, schema "
        "version, and compatibility note.",
    )
    _add_common(tail_p)
    tail_p.add_argument(
        "--session",
        default=None,
        metavar="SESSION_ID",
        help="Only emit records for this consuming session id (exact "
        "match). Without this, a viewer process that itself calls recall "
        "observes its own pushes mixed into the stream.",
    )
    tail_p.add_argument(
        "--follow",
        action="store_true",
        help="After draining the ledgers, keep polling and emit records "
        "appended afterward, like `tail -f`. Without it, drain and exit. "
        "Runs until interrupted (e.g. Ctrl-C).",
    )
    tail_p.add_argument(
        "--since",
        default=None,
        help="Only emit records timestamped at/after this bound: relative "
        "(7d/24h/30m/2w) or absolute ISO-8601. Default: the whole ledger.",
    )
