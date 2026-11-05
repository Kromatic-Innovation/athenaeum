# SPDX-License-Identifier: Apache-2.0
"""``athenaeum push-metrics {baseline,coverage-audit,liveness,record,tail,
sample-for-labelling,score-used}`` — v6 MVP (a), issue athenaeum#711;
``liveness`` added by issue athenaeum#1422, ``record`` by issue
athenaeum#1478, ``tail`` by issue athenaeum#1479, ``sample-for-labelling``
and ``score-used`` by issue athenaeum#2302.

Seven subcommands, the first five over :mod:`athenaeum.push_metrics` and the
last two over :mod:`athenaeum.push_used_scoring`:

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
- ``sample-for-labelling``  stratified sample of pushed pages, by session-
                       length bucket and population, written as a
                       labelling-ready JSONL worksheet (issue athenaeum#2302,
                       part of the athenaeum#2295 epic — this is TOOLING
                       only; it samples and writes empty label slots, it
                       never fills them in). Thin argv parsing over
                       :func:`athenaeum.push_used_scoring.stratified_sample_pushed_pages`.
                       Default ``--output`` is under the cache dir, never
                       inside this repo's working tree.
- ``score-used``       score the `used` heuristic
                       (:func:`athenaeum.push_metrics.determine_references`)
                       against a human-labelled file and report precision,
                       recall, and the relevant/irrelevant breakdown of
                       unused pushes, all with Wilson confidence intervals
                       (issue athenaeum#2302). ``--labels`` is REQUIRED —
                       label files live outside this repo and this
                       subcommand has no default for one. Thin argv parsing
                       over :func:`athenaeum.push_used_scoring.score_against_heuristic`.

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` —
mirrors :mod:`athenaeum._cmd_calibration`'s shape. ``record`` (issue
athenaeum#1478), ``tail`` (issue athenaeum#1479), and
``sample-for-labelling``/``score-used`` (issue athenaeum#2302) are all added
here rather than a new module because ``push-metrics`` already has this one
file — keeps ``push-metrics``'s subcommands cohesive and localised in one
place.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from athenaeum._cli_shared import _resolve_wiki_root
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT

_SUBCOMMANDS = (
    "baseline",
    "coverage-audit",
    "liveness",
    "record",
    "tail",
    "sample-for-labelling",
    "score-used",
)


def cmd_push_metrics(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum push-metrics {baseline,coverage-audit,liveness,
    record,tail,sample-for-labelling,score-used}``."""
    sub = getattr(args, "push_metrics_target", None)
    if sub not in _SUBCOMMANDS:
        print(
            f"usage: athenaeum push-metrics {{{','.join(_SUBCOMMANDS)}}} [...]",
            file=sys.stderr,
        )
        return 2

    if sub == "record":
        return _cmd_push_metrics_record(args)

    if sub == "tail":
        return _cmd_push_metrics_tail(args)

    if sub == "sample-for-labelling":
        return _cmd_push_metrics_sample_for_labelling(args)

    if sub == "score-used":
        return _cmd_push_metrics_score_used(args)

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
        # athenaeum#1592: STALE is a non-PASS state exactly like FAIL — a
        # frozen ledger must exit nonzero for CI/manual invocation the same
        # way an untagged one does, per this issue's AC1 ("FAILS, or reports
        # a distinct non-PASS state"). INCONCLUSIVE keeps exiting 0: not
        # enough signal to assert anything is broken (AC2).
        return (
            1
            if result.outcome in (push_metrics.LIVENESS_FAIL, push_metrics.LIVENESS_STALE)
            else 0
        )

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

    ``--cache-dir`` isolation (issue athenaeum#1512): see
    :func:`_resolve_record_wiki_root` for why this subcommand resolves its
    own ``wiki_root`` instead of the shared :func:`_resolve_wiki_root`.
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

    record_wiki_root = _resolve_record_wiki_root(args)
    wrote = push_metrics.record_hook_push(
        session_id,
        ids,
        query=query,
        backend=backend,
        cache_dir=args.cache_dir,
        wiki_root=record_wiki_root,
    )
    if args.json:
        # athenaeum#1592 AC3: report the resolved destination path so
        # `wrote: true` is verifiable by the caller — same resolution
        # `record_hook_push`/`record_push` actually wrote through
        # (`durable_push_records_path`, issue athenaeum#1591), computed here
        # rather than threaded back out of the write call so a read-only
        # resolution failure can never turn a successful write into a
        # reported failure.
        path = str(
            push_metrics.durable_push_records_path(record_wiki_root, cache_dir=args.cache_dir)
        )
        sys.stdout.write(json.dumps({"wrote": wrote, "path": path}) + "\n")
    return 0


def _resolve_record_wiki_root(args: argparse.Namespace) -> Path:
    """``wiki_root`` resolution for ``push-metrics record`` ONLY (issue
    athenaeum#1512) — every other ``push-metrics`` subcommand keeps using
    the shared :func:`athenaeum._cli_shared._resolve_wiki_root`.

    ``durable_push_records_path`` cannot itself tell "the operator typed
    ``--cache-dir`` to isolate a test/scratch run" apart from "a caller
    resolved ``cache_dir`` eagerly as part of its own plumbing" — both
    arrive as a concrete, non-``None`` :class:`Path`. Its ``new_path``
    branch is governed entirely by ``wiki_root`` / ``--path``, which
    ``--cache-dir`` does not touch. Left alone, ``--cache-dir <scratch>``
    with no ``--path`` override resolves ``wiki_root`` to the live default
    (``~/knowledge/wiki``), and an empty scratch dir makes the legacy
    branch's ``not legacy_path.exists()`` true — so the "isolated" call
    writes straight into the real wiki root. That is the exact incident
    this issue reports (399 rows orphaned by one scratch-dir invocation).

    Chosen semantics ("scope both branches", the issue's own suggested
    option, judged safer than a refusal here because ``record`` is
    documented fire-and-forget and must keep exiting 0 — see this
    subcommand's own docstring): when ``--cache-dir`` was given AND
    ``--path`` was left at its argparse default (i.e. the operator did not
    also scope the wiki root away from the live default), root
    ``wiki_root`` under the SAME explicit ``--cache-dir`` value instead of
    falling through to the live default. ``.expanduser()`` only (no
    ``.resolve()``) deliberately matches :func:`athenaeum.config.resolve_cache_dir`
    exactly, so ``durable_push_records_path``'s ``new_path`` and
    ``legacy_path`` become the IDENTICAL :class:`Path` in this case — the
    two-branch check can no longer matter, because there is only one branch
    left to land in.

    A caller who explicitly passes ``--path`` (to any value, including one
    that happens to equal the default) is left alone: providing ``--path``
    at all is treated as an informed, deliberate choice of wiki root.
    """
    if args.cache_dir is not None and args.path == DEFAULT_KNOWLEDGE_ROOT:
        return args.cache_dir.expanduser()
    return _resolve_wiki_root(args)


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


def _cmd_push_metrics_sample_for_labelling(args: argparse.Namespace) -> int:
    """``athenaeum push-metrics sample-for-labelling`` (issue athenaeum#2302).

    Thin argv parsing over
    :func:`athenaeum.push_used_scoring.stratified_sample_pushed_pages` — all
    the stratification logic lives there. Writes the sampled pages as a
    labelling-ready JSONL worksheet; never fills in ``relevant``/
    ``changed_work`` itself (that is a separate, human workflow — issue
    athenaeum#2311).
    """
    from athenaeum import push_used_scoring

    try:
        pages = push_used_scoring.stratified_sample_pushed_pages(
            cache_dir=args.cache_dir,
            n_per_stratum=args.n_per_stratum,
            wiki_root=_resolve_wiki_root(args),
            short_max=args.short_max,
            medium_max=args.medium_max,
            seed=args.seed,
            exclude_sessions=getattr(args, "exclude_session", None),
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    output_path = (
        args.output.expanduser().resolve()
        if args.output is not None
        else push_used_scoring.default_sample_output_path(args.cache_dir)
    )
    push_used_scoring.write_sample_worksheet(pages, output_path=output_path)

    if args.json:
        sys.stdout.write(
            json.dumps({"sampled_count": len(pages), "output_path": str(output_path)}) + "\n"
        )
    else:
        print(f"sampled {len(pages)} pushed page(s) -> {output_path}")
    return 0


def _cmd_push_metrics_score_used(args: argparse.Namespace) -> int:
    """``athenaeum push-metrics score-used`` (issue athenaeum#2302).

    Thin argv parsing over
    :func:`athenaeum.push_used_scoring.load_labels` and
    :func:`athenaeum.push_used_scoring.score_against_heuristic` — all the
    scoring logic lives there. ``--labels`` is REQUIRED: label files live
    outside this repo, at a path this subcommand never defaults.
    """
    from athenaeum import push_used_scoring

    # Seer finding on athenaeum#2302 PR review: a bad --labels path (the
    # single most likely user error, since label files live outside this
    # repo at a caller-supplied path) raised OSError uncaught; an
    # unsupported --confidence raised ValueError uncaught from the scoring
    # call below. Both are now named CLI errors, matching this file's own
    # `baseline`/`coverage-audit` convention (`except ValueError as exc:
    # print(f"error: {exc}", ...); return 1`) rather than a traceback.
    try:
        labels = push_used_scoring.load_labels(args.labels)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not labels:
        print("error: --labels file contains zero fully-labelled rows", file=sys.stderr)
        return 1

    try:
        report = push_used_scoring.score_against_heuristic(
            labels,
            cache_dir=args.cache_dir,
            projects_root=args.projects_root,
            wiki_root=_resolve_wiki_root(args),
            confidence=args.confidence,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    output_path = (
        args.output.expanduser().resolve()
        if args.output is not None
        else push_used_scoring.default_score_output_path(args.cache_dir)
    )
    push_used_scoring.write_score_report(report, output_path=output_path)

    if args.json:
        sys.stdout.write(json.dumps(report.to_dict()) + "\n")
    else:
        precision = report.precision
        recall = report.recall
        breakdown = report.unused_breakdown
        print(
            f"sample_size: {report.sample_size}\n"
            f"confusion: tp={report.confusion.tp} fp={report.confusion.fp} "
            f"fn={report.confusion.fn} tn={report.confusion.tn}\n"
            f"precision: {precision.rate} "
            f"(n={precision.n}, ci=[{precision.ci_low}, {precision.ci_high}])\n"
            f"recall: {recall.rate} (n={recall.n}, ci=[{recall.ci_low}, {recall.ci_high}])\n"
            f"unused pushes: {breakdown.unused_total}\n"
            f"  relevant but not opened: {breakdown.relevant_but_not_opened.rate} "
            f"(n={breakdown.relevant_but_not_opened.n})\n"
            f"  irrelevant: {breakdown.irrelevant.rate} (n={breakdown.irrelevant.n})\n"
            f"undetermined: {report.undetermined_labels} label(s) across "
            f"{report.undetermined_sessions} session(s) (excluded from the "
            f"figures above, not folded into a heuristic negative)\n"
            f"report written to: {output_path}"
        )
    return 0


def add_push_metrics_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum push-metrics`` and its three modes on ``subparsers``."""
    p_parser = subparsers.add_parser(
        "push-metrics",
        help=(
            "Push-precision + coverage baseline: compute/record the "
            "precision snapshot, sample sessions for a human-reviewed "
            "coverage-audit worksheet (issue athenaeum#711), record a "
            "single hook-path push (issue athenaeum#1478), stream the "
            "documented NDJSON tail contract over the ledgers (issue "
            "athenaeum#1479), and sample/score the `used` heuristic "
            "against human labels (issue athenaeum#2302)."
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

    sample_p = p_sub.add_parser(
        "sample-for-labelling",
        help="Stratified sample of pushed pages, by session-length bucket "
        "and population, written as a labelling-ready JSONL worksheet "
        "(athenaeum#2302). Tooling only — never fills in the relevant/"
        "changed_work judgments itself.",
    )
    _add_common(sample_p)
    sample_p.add_argument(
        "--n-per-stratum",
        type=int,
        default=10,
        help="Max pushed pages to sample from EACH (length, population) "
        "stratum (default: 10).",
    )
    sample_p.add_argument(
        "--short-max",
        type=int,
        default=3,
        help="Push-record count at/below which a session is bucketed "
        "'short' (default: 3).",
    )
    sample_p.add_argument(
        "--medium-max",
        type=int,
        default=10,
        help="Push-record count at/below which a session is bucketed "
        "'medium' (above 'short'); anything higher is 'long' (default: 10).",
    )
    sample_p.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Optional deterministic sample seed (test/repro seam).",
    )
    sample_p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Worksheet output file. Defaults under the cache dir "
        "(athenaeum.push_used_scoring.default_sample_output_path) — "
        "never inside this repo's working tree.",
    )
    sample_p.add_argument(
        "--exclude-session",
        action="append",
        default=None,
        metavar="SESSION_ID",
        help="Exclude a KNOWN-synthetic session id from the sampling pool "
        "(same semantics as `coverage-audit --exclude-session`). Repeatable.",
    )

    score_p = p_sub.add_parser(
        "score-used",
        help="Score the `used` heuristic (push_metrics.determine_references) "
        "against a human-labelled file: precision, recall, and the "
        "relevant/irrelevant breakdown of unused pushes, with Wilson "
        "confidence intervals (athenaeum#2302).",
    )
    _add_common(score_p)
    score_p.add_argument(
        "--labels",
        type=Path,
        required=True,
        metavar="LABELS_PATH",
        help="REQUIRED. Path to a fully-labelled JSONL file (one "
        "{session_id, push_id, relevant, changed_work} object per line). "
        "Label files live OUTSIDE this repo — there is no default.",
    )
    score_p.add_argument(
        "--projects-root",
        type=Path,
        default=None,
        help="Claude Code `~/.claude/projects` root to read transcripts "
        "from when calling determine_references (default: "
        "athenaeum.transcript_verify.default_projects_root — the real "
        "live root; override for a test/repro run against a scratch tree).",
    )
    score_p.add_argument(
        "--confidence",
        type=float,
        default=0.95,
        help="Confidence level for the Wilson score interval: one of "
        "0.80, 0.90, 0.95, 0.98, 0.99 (default: 0.95).",
    )
    score_p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Report output file. Defaults under the cache dir "
        "(athenaeum.push_used_scoring.default_score_output_path) — never "
        "inside this repo's working tree.",
    )
