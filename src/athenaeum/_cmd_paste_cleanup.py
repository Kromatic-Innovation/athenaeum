# SPDX-License-Identifier: Apache-2.0
"""``athenaeum paste-cleanup`` — tier-0 attributed-paste cleanup pass CLI
(issue athenaeum#1717).

Presentation for :mod:`athenaeum.paste_cleanup`. Dry-run by default and
``--apply`` to write, mirroring ``audit``/``_cmd_audit.py``.

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` — see
``cli.py``'s module docstring.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from athenaeum._cli_shared import _acquire_or_exit, _add_lock_args, _resolve_knowledge_root
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT

if TYPE_CHECKING:
    from athenaeum.runlock import RunLock


def _read_uids_file(path: Path) -> list[str]:
    uids = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            uids.append(line)
    return uids


def cmd_paste_cleanup(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum paste-cleanup``."""
    from athenaeum.paste_cleanup import (
        PASTE_CLEANUP_VERSION,
        PasteCleanupReport,
        apply_paste_cleanup_report,
        build_paste_cleanup_report,
        resume_verification,
    )

    knowledge_root = _resolve_knowledge_root(args)
    wiki_root = knowledge_root / "wiki"
    if not wiki_root.is_dir():
        print(f"error: no wiki directory at {wiki_root}", file=sys.stderr)
        return 1

    # Issue athenaeum#1923: --resume-verification only means anything against
    # a prior report -- there is no in-memory report to resume otherwise.
    if args.resume_verification and args.from_report is None:
        print("error: --resume-verification requires --from-report", file=sys.stderr)
        return 1

    uids: list[str] | None = None
    if args.uids is not None:
        uids_path = Path(args.uids)
        if not uids_path.is_file():
            print(f"error: --uids file not found: {uids_path}", file=sys.stderr)
            return 1
        uids = _read_uids_file(uids_path)

    from athenaeum.config import load_config

    config = load_config(knowledge_root)

    report: Any
    client: Any = None
    verify_client: Any = None

    if args.from_report is not None:
        # Issue athenaeum#1903 step 3: replay a prior --json report's
        # verdicts. Mutually exclusive with the proposer/verifier knobs
        # below -- they describe a fresh LLM pass this path never runs.
        conflicting = []
        if args.model is not None:
            conflicting.append("--model")
        if args.verify_model is not None and not args.resume_verification:
            # Issue athenaeum#1923: --resume-verification is the one
            # proposer/verifier knob this replay path legitimately accepts
            # -- it overrides report.verify_model for the resumed verifier
            # calls only, never a fresh proposer pass.
            conflicting.append("--verify-model")
        if args.verify_rule != "sampled":
            conflicting.append("--verify-rule")
        if args.sample is not None:
            conflicting.append("--sample")
        if args.limit is not None:
            conflicting.append("--limit")
        if conflicting:
            print(
                "error: --from-report is mutually exclusive with " + ", ".join(conflicting),
                file=sys.stderr,
            )
            return 1

        from_report_path = Path(args.from_report)
        if not from_report_path.is_file():
            print(f"error: --from-report file not found: {from_report_path}", file=sys.stderr)
            return 1
        try:
            raw_report = json.loads(from_report_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"error: --from-report file is not valid JSON: {exc}", file=sys.stderr)
            return 1
        try:
            report = PasteCleanupReport.from_dict(raw_report)
        except (ValueError, KeyError) as exc:
            print(f"error: --from-report: {exc}", file=sys.stderr)
            return 1

        if uids is not None:
            wanted = set(uids)
            report.proposed = [
                v
                for v in report.proposed
                if v.uid in wanted or any(v.uid.startswith(u) for u in wanted)
            ]

        if args.resume_verification:
            # Issue athenaeum#1923: builds only the 'verify' knob's client --
            # this path makes no proposer calls, so no 'classify' client is
            # needed. Mirrors the fresh-pass branch's classify-knob error
            # wording below, naming 'verify' instead.
            from athenaeum.provider import build_llm_client

            verify_client = build_llm_client(config, knob="verify") or build_llm_client(
                config, knob="classify"
            )
            if verify_client is None:
                print(
                    "error: no LLM client is configured for the 'verify' knob "
                    "(check llm.provider / ANTHROPIC_API_KEY)",
                    file=sys.stderr,
                )
                return 1
    else:
        from athenaeum.config import (
            DEFAULT_CLASSIFY_MODEL,
            DEFAULT_VERIFY_MODEL,
            resolve_model,
        )

        model = args.model or resolve_model(
            "classify", "ATHENAEUM_CLASSIFY_MODEL", DEFAULT_CLASSIFY_MODEL, config
        )
        verify_model = args.verify_model or resolve_model(
            "verify", "ATHENAEUM_VERIFY_MODEL", DEFAULT_VERIFY_MODEL, config
        )

        if not args.mechanical_dry_run:
            from athenaeum.provider import build_llm_client

            client = build_llm_client(config, knob="classify")
            verify_client = build_llm_client(config, knob="verify") or client
            if client is None:
                print(
                    "error: no LLM client is configured for the 'classify' knob "
                    "(check llm.provider / ANTHROPIC_API_KEY)",
                    file=sys.stderr,
                )
                return 1

    lock: "RunLock | None" = None
    if args.apply:
        acquired = _acquire_or_exit(knowledge_root, args, config)
        if isinstance(acquired, int):
            return acquired
        lock = acquired

    try:
        if args.from_report is None:
            report = build_paste_cleanup_report(
                wiki_root,
                client=client,
                verify_client=verify_client,
                model=model,
                verify_model=verify_model,
                verify_rule=args.verify_rule,
                limit=args.limit,
                sample=args.sample,
                seed=args.seed,
                uids=uids,
                config=config,
            )
        elif args.resume_verification:
            # Issue athenaeum#1923: resume BEFORE apply, so a single
            # `--from-report --resume-verification --apply` invocation
            # resumes verification then applies the now-more-complete
            # report in one pass.
            resume_verification(
                report,
                wiki_root,
                verify_client=verify_client,
                verify_model=args.verify_model,
                config=config,
            )

        changed = apply_paste_cleanup_report(report, wiki_root) if args.apply else 0

        if args.json:
            payload = report.to_dict()
            payload["applied"] = args.apply
            payload["files_changed"] = changed
            # Issue athenaeum#1924: already carried by report.to_dict() above
            # (report.apply_skips, populated by apply_paste_cleanup_report
            # just before this), but set explicitly here too so it reads as
            # a deliberate part of THIS payload's apply-result fields,
            # alongside applied/files_changed rather than only implicitly.
            payload["apply_skips"] = report.apply_skips
            payload["from_report"] = str(args.from_report) if args.from_report else None
            payload["resume_verification"] = bool(args.resume_verification)
            sys.stdout.write(json.dumps(payload) + "\n")
            return 0

        mode = "APPLY" if args.apply else "DRY RUN"
        if args.from_report is not None:
            mode = f"REPLAY {mode}"
            if args.resume_verification:
                mode = f"RESUME {mode}"
        print(f"=== athenaeum paste-cleanup ({mode}, {PASTE_CLEANUP_VERSION}) ===")
        # Deliberately UNCHANGED order (summary, then the applied/dry-run
        # confirmation): every existing invocation, --apply or not, keeps
        # byte-identical output to its pre-athenaeum#1924 self unless an
        # apply actually populated report.apply_skips. The new skip tally
        # rides inside render_text() rather than being hand-rendered a
        # second time here, so there is one formatter for it, not two.
        print(report.render_text())
        print(
            f"applied: {changed} file(s) written"
            if args.apply
            else "dry run: nothing written (pass --apply to write)"
        )
        return 0
    finally:
        if lock is not None:
            lock.release()


def add_paste_cleanup_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``paste-cleanup``."""
    parser = subparsers.add_parser(
        "paste-cleanup",
        help="Tier-0 attributed-paste cleanup pass over person pages' ## Notes "
        "bullets (issue athenaeum#1717): a cheap-model proposer classifies "
        "keep/rewrite/remove, a stronger model verifies a sample. A proposal "
        "whose proposer OR verifier call errors (raises, or returns "
        "unparseable output) resolves to hold, never the unverified "
        "proposer verdict (issue athenaeum#1903). Dry-run by default; "
        "--apply writes.",
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write remove/rewrite verdicts. Without this flag the command is a dry-run.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Consider at most N pages this pass.",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=None,
        help="Consider a stratified random sample of N pages instead of the "
        "whole corpus. Combine with --seed for reproducibility.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help="Seed for --sample (default: 0).",
    )
    parser.add_argument(
        "--uids",
        type=Path,
        default=None,
        help="Path to a file listing one page uid per line; consider exactly those pages.",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Override the proposer model (default: the 'classify' knob's resolved model).",
    )
    parser.add_argument(
        "--verify-model",
        type=str,
        default=None,
        dest="verify_model",
        help="Override the verifier model (default: the 'verify' knob's resolved model).",
    )
    parser.add_argument(
        "--verify-rule",
        type=str,
        choices=("sampled", "all"),
        default="sampled",
        dest="verify_rule",
        help="'sampled' verifies every low-confidence proposal plus a fixed "
        "10%% stable sample of the rest; 'all' verifies every proposal "
        "(issue athenaeum#1717's own AC: use 'all' when measured agreement "
        "is below 90%%).",
    )
    parser.add_argument(
        "--from-report",
        type=Path,
        default=None,
        dest="from_report",
        help="Replay a prior --json report's verdicts instead of running a "
        "fresh proposer/verifier pass -- no LLM client is built. Mutually "
        "exclusive with --model/--verify-rule/--sample/--limit (--verify-model "
        "is allowed together with --resume-verification). --uids further "
        "restricts the replayed set. The report's version must match this "
        "build's PASTE_CLEANUP_VERSION. Without --apply, prints the replay "
        "summary and writes nothing. Combine with --resume-verification to "
        "finish verifying bullets a spend ceiling left pending instead of "
        "just replaying as-is. A report entry's operator_claim, when "
        "present, is written verbatim by --apply in preference to the "
        "model's own claim AND verdict (issue athenaeum#1924) -- there is "
        "no separate flag for this; edit the report JSON directly.",
    )
    parser.add_argument(
        "--resume-verification",
        action="store_true",
        dest="resume_verification",
        help="Requires --from-report. Resumes the verifier pass over the "
        "loaded report's bullets that a spend ceiling left "
        "verify_attempted=False, WITHOUT re-running the proposer pass -- "
        "builds only the 'verify' LLM client (issue athenaeum#1923). Runs "
        "before --apply, so --from-report --resume-verification --apply "
        "resumes then applies in one invocation.",
    )
    parser.add_argument(
        "--mechanical-dry-run",
        action="store_true",
        dest="mechanical_dry_run",
        help="Skip building an LLM client entirely. For CI/offline smoke checks only.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of plain text.",
    )
    _add_lock_args(parser)
    parser.set_defaults(func=cmd_paste_cleanup)
