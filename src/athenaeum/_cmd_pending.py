# SPDX-License-Identifier: Apache-2.0
"""``athenaeum {ingest-answers,ingest-merges,reresolve-questions,
dedup-oversize-escalations}`` — pending-sidecar maintenance.

Four subcommands grouped here because each is an idempotent, scheduler-safe
maintenance pass over one of the pending-decision sidecars
(``wiki/_pending_questions.md`` / ``wiki/_pending_merges.md``): archiving
resolved blocks into raw intake or an archive file, self-healing
proposal-less open questions, and (issue athenaeum#1430) collapsing duplicate
oversize-page-family escalations already on disk down to one unanswered
block per entity. All four share the same CLI shape (load config,
optionally build an LLM client via the provider seam, acquire the run lock,
delegate to an L3/L4 function, print a one-line summary) --
``dedup-oversize-escalations`` is the one command in this module that never
builds an LLM client: its cleanup pass is pure deterministic file rewriting.

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
import logging
import sys
from pathlib import Path

from athenaeum._cli_shared import _acquire_or_exit, _add_lock_args
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT


def add_pending_subparsers(subparsers: argparse._SubParsersAction) -> None:
    """Register ``ingest-answers``, ``ingest-merges``, ``reresolve-questions``,
    ``dedup-oversize-escalations`` (issue athenaeum#1430)."""

    # ingest-answers command — convert resolved `[x]` blocks in
    # _pending_questions.md into raw intake files and archive the answered
    # blocks. Idempotent — safe to run from a scheduler.
    ingest_answers_parser = subparsers.add_parser(
        "ingest-answers",
        help="Ingest answered pending questions from _pending_questions.md",
    )
    ingest_answers_parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    # Issue athenaeum#1446: suppress per-malformed-block noise (both the
    # `[warn] ... malformed header` print and the paired `log.warning`
    # calls, plus the rewrite pass's "Preserving malformed block verbatim"
    # line) so a corpus with thousands of malformed blocks doesn't bury the
    # final summary line. Additive only — without the flag, output is
    # byte-for-byte unchanged (see `cmd_ingest_answers`).
    ingest_answers_parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        default=False,
        help="Suppress per-block malformed-block warnings; print only the "
        "final summary line(s) (issue athenaeum#1446).",
    )
    _add_lock_args(ingest_answers_parser)
    ingest_answers_parser.set_defaults(func=cmd_ingest_answers)

    # ingest-merges command (issue athenaeum#299) — move resolved (`[x]`) blocks out
    # of `wiki/_pending_merges.md` into `_pending_merges_archive.md`, mirroring
    # ingest-answers for the questions sidecar. Idempotent — safe to run from
    # a scheduler.
    ingest_merges_parser = subparsers.add_parser(
        "ingest-merges",
        help="Archive resolved pending merges from wiki/_pending_merges.md",
    )
    ingest_merges_parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    _add_lock_args(ingest_merges_parser)
    ingest_merges_parser.set_defaults(func=cmd_ingest_merges)

    # reresolve-questions command (issue athenaeum#188) — re-run the resolver on OPEN,
    # PROPOSAL-LESS pending questions so a prior cap-hit / offline escalation
    # self-heals. Budget-aware + idempotent; offline (no key) is a no-op.
    reresolve_parser = subparsers.add_parser(
        "reresolve-questions",
        help="Re-resolve open proposal-less pending questions "
        "(self-heal transient cap/offline escalations, issue athenaeum#188)",
    )
    reresolve_parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    _add_lock_args(reresolve_parser)
    reresolve_parser.set_defaults(func=cmd_reresolve_questions)

    # dedup-oversize-escalations command (issue athenaeum#1430) -- one-shot cleanup:
    # collapse existing duplicate oversize-page-family escalation blocks
    # (``**Conflict type**: oversize_page``/``oversize_split``/
    # ``oversize_log_demote``) down to one UNANSWERED block per affected
    # entity, archiving the rest via the same rendering ingest-answers uses
    # for a genuinely answered block. Deterministic, no LLM, no network --
    # safe to run from a scheduler like the other two commands above.
    dedup_oversize_parser = subparsers.add_parser(
        "dedup-oversize-escalations",
        help="Collapse duplicate oversize-page-family escalations in "
        "_pending_questions.md to one unanswered block per entity "
        "(issue athenaeum#1430)",
    )
    dedup_oversize_parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    _add_lock_args(dedup_oversize_parser)
    dedup_oversize_parser.set_defaults(func=cmd_dedup_oversize_escalations)


def cmd_ingest_answers(args: argparse.Namespace) -> int:
    """Ingest answered blocks from `_pending_questions.md` as raw intake.

    See :func:`athenaeum.answers.ingest_answers` for the semantics.

    Issue athenaeum#908: BEFORE the legacy question-answer pass, this now also
    applies every pending decision-answer file under ``raw/answers/`` — the
    uniform intake path ``resolve_question`` / ``resolve_merge`` /
    ``review_audit_item`` defer to (see
    :func:`athenaeum.decision_answers.apply_decision_answers`). That step is
    purely mechanical (no LLM client, ever); for ``decision_type: question``
    it only flips the block's checkbox, so the LEGACY ``ingest_answers`` pass
    immediately below it — unchanged — is what actually completes the
    write-back/archival for a question answered this way, in the SAME
    run-locked tick.

    Builds the LLM client via the provider seam (``build_llm_client``, athenaeum#330)
    and passes it to ``ingest_answers`` so free-text answers can use the
    LLM-backed proposer (issue athenaeum#210): a ``claude-cli`` subscription client, or
    an Anthropic SDK client when ``provider: api`` and ``ANTHROPIC_API_KEY`` is
    set. When the key is absent (api backend) or construction fails, the
    annotation fallback is used instead.

    Issue athenaeum#1446: ``--quiet``/``-q`` suppresses the per-malformed-block
    noise (``_parse_block``'s ``[warn] ... malformed header`` print, plus the
    ``log.warning`` calls in ``_parse_block`` and in ``ingest_answers``'s
    rewrite pass) so a corpus with thousands of malformed blocks doesn't bury
    the summary line. Deliberately NOT routed through ``configure_logging`` —
    that helper only distinguishes INFO/DEBUG (``verbose``), has no quiet
    level, and calling it here would also change the unflagged path's output
    format. Instead: the bare ``print`` calls are gated at the call site
    (threaded through as ``quiet=quiet``), and the ``log.warning`` records are
    suppressed by temporarily raising the ``athenaeum.answers`` logger to
    ``ERROR`` for the duration of the ``ingest_answers`` call, restored in a
    ``finally`` so this process-level side effect never leaks past this
    function.
    """
    from athenaeum.answers import ingest_answers
    from athenaeum.config import load_config
    from athenaeum.decision_answers import apply_decision_answers
    from athenaeum.provider import ProviderConfigError, build_llm_client

    target = args.path.expanduser().resolve()
    if not target.exists():
        print(f"Knowledge directory not found: {target}", file=sys.stderr)
        print(
            f"Run 'athenaeum init --path {args.path}' first, then retry.",
            file=sys.stderr,
        )
        return 1

    pending_path = target / "wiki" / "_pending_questions.md"
    raw_root = target / "raw"

    quiet = getattr(args, "quiet", False)  # issue athenaeum#1446

    cfg = load_config(target)

    # Issue athenaeum#210/#330: build the LLM client via the provider seam so free-text
    # answers trigger the LLM-backed source-edit proposer. Returns None for the
    # ``api`` backend with no ANTHROPIC_API_KEY (offline annotation fallback);
    # returns the subscription CLI client for ``claude-cli``. Fail gracefully
    # (None) on any construction error.
    # Issue athenaeum#786: routed via the ``resolve`` knob — this command's only LLM
    # call is ``resolutions.propose_freetext_source_edits`` (knob="resolve"), so
    # ``llm.providers.resolve`` / ``ATHENAEUM_RESOLVE_LLM_PROVIDER`` now let an
    # operator pin free-text answer ingestion to a different provider than the
    # global default. No ``llm.providers.resolve`` key resolves identically to
    # the pre-athenaeum#786 global-only call (AC6).
    anthropic_client = None
    try:
        anthropic_client = build_llm_client(cfg, knob="resolve")
    except ProviderConfigError as exc:
        # Issue athenaeum#540 (M14): a provider MISCONFIGURATION (e.g. a typo in the
        # backend name) is raised loudly by build_llm_client precisely so it
        # never silently falls back to a different backend. Surface it and
        # exit nonzero rather than swallowing it into the offline fallback and
        # exiting 0 — the exact silent-backend-fallback provider.py forbids.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 — a genuine construction error (e.g. no
        # API key for the api backend) is the intended offline fallback: leave
        # the client None and let the caller degrade. Only ProviderConfigError
        # (a misconfig) is fatal, handled above.
        pass

    lock = _acquire_or_exit(target, args, cfg)  # issue athenaeum#309
    if isinstance(lock, int):
        return lock
    try:
        # Issue athenaeum#908: apply pending decision-answer files FIRST, in the same
        # locked tick — deterministic, no LLM call. For a `question` answer
        # this only flips the checkbox; the ingest_answers() pass right
        # below completes the write-back/archival for it, unchanged.
        wiki_root = target / "wiki"
        # Issue athenaeum#712: forward the run lock already held above (`lock =
        # _acquire_or_exit(...)`) so a merge decision applied in this tick can
        # record a verdict-ledger entry when librarian.verdict_ledger_enabled
        # is on — see apply_decision_answers's `lock` docstring.
        decision_report = apply_decision_answers(
            wiki_root, raw_root, config=cfg, lock=lock
        )
        # Issue athenaeum#1446: `--quiet` suppresses the per-block
        # `log.warning` noise `_parse_block`/`ingest_answers` emit by
        # level, not by structurally deleting the calls — restored in
        # `finally` so the mutation never leaks past this one call.
        answers_logger = logging.getLogger("athenaeum.answers")
        prior_level = answers_logger.level
        if quiet:
            answers_logger.setLevel(logging.ERROR)
        try:
            count = ingest_answers(
                pending_path,
                raw_root,
                client=anthropic_client,
                config=cfg,
                quiet=quiet,
            )
        finally:
            if quiet:
                answers_logger.setLevel(prior_level)
    except Exception as exc:  # noqa: BLE001 — surface a clean CLI error
        print(
            f"Fatal error ingesting answers ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 2
    finally:
        lock.release()

    if decision_report.applied or decision_report.skipped:
        print(
            f"Applied {decision_report.applied} decision answer(s), "
            f"{decision_report.skipped} skipped (see log)."
        )
    print(f"Ingested {count} answered question(s).")
    return 0


def cmd_ingest_merges(args: argparse.Namespace) -> int:
    """Archive resolved blocks from `wiki/_pending_merges.md` (issue athenaeum#299).

    See :func:`athenaeum.pending_merges.ingest_resolved_merges` for the
    semantics. Mirrors :func:`cmd_ingest_answers`'s CLI shape.
    """
    from athenaeum.pending_merges import ingest_resolved_merges

    target = args.path.expanduser().resolve()
    if not target.exists():
        print(f"Knowledge directory not found: {target}", file=sys.stderr)
        print(
            f"Run 'athenaeum init --path {args.path}' first, then retry.",
            file=sys.stderr,
        )
        return 1

    merges_path = target / "wiki" / "_pending_merges.md"

    from athenaeum.config import load_config

    lock = _acquire_or_exit(target, args, load_config(target))  # issue athenaeum#309
    if isinstance(lock, int):
        return lock
    try:
        count = ingest_resolved_merges(merges_path)
    except Exception as exc:  # noqa: BLE001 — surface a clean CLI error
        print(
            f"Fatal error ingesting merges ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 2
    finally:
        lock.release()

    print(f"Archived {count} resolved merge(s).")
    return 0


def cmd_reresolve_questions(args: argparse.Namespace) -> int:
    """Re-resolve open, proposal-less pending questions (issue athenaeum#188).

    Mirrors :func:`cmd_ingest_answers`: loads config, builds the LLM client
    via the provider seam (``build_llm_client``, athenaeum#330 — a subscription
    ``claude-cli`` client or an Anthropic SDK client per ``llm.provider``;
    ``None`` when the api backend has no key, where offline is a no-op), and
    delegates to :func:`athenaeum.tiers.reresolve_open_questions`.
    """
    from athenaeum.config import load_config
    from athenaeum.provider import ProviderConfigError, build_llm_client
    from athenaeum.tiers import reresolve_open_questions

    target = args.path.expanduser().resolve()
    if not target.exists():
        print(f"Knowledge directory not found: {target}", file=sys.stderr)
        return 1

    pending_path = target / "wiki" / "_pending_questions.md"
    cfg = load_config(target)

    # Issue athenaeum#330: construct via the provider seam (api key -> SDK client;
    # claude-cli -> subscription CLI client; None when the api backend has no
    # key, preserving the offline no-op below).
    # Issue athenaeum#786: routed via the ``resolve`` knob — ``reresolve_open_questions``
    # only invokes ``resolutions.propose_resolution`` (knob="resolve"). Same
    # rationale as ``cmd_ingest_answers`` above; no ``llm.providers.resolve``
    # key resolves identically to the pre-athenaeum#786 global-only call (AC6).
    anthropic_client = None
    try:
        anthropic_client = build_llm_client(cfg, knob="resolve")
    except ProviderConfigError as exc:
        # Issue athenaeum#540 (M14): a provider MISCONFIGURATION (e.g. a typo in the
        # backend name) is raised loudly by build_llm_client precisely so it
        # never silently falls back to a different backend. Surface it and
        # exit nonzero rather than swallowing it into the offline fallback and
        # exiting 0 — the exact silent-backend-fallback provider.py forbids.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 — a genuine construction error (e.g. no
        # API key for the api backend) is the intended offline fallback: leave
        # the client None and let the caller degrade. Only ProviderConfigError
        # (a misconfig) is fatal, handled above.
        pass

    lock = _acquire_or_exit(target, args, cfg)  # issue athenaeum#309
    if isinstance(lock, int):
        return lock
    try:
        count = reresolve_open_questions(
            pending_path, client=anthropic_client, config=cfg
        )
    except Exception as exc:  # noqa: BLE001 — surface a clean CLI error
        print(
            f"Fatal error re-resolving questions ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 2
    finally:
        lock.release()

    if anthropic_client is None:
        print("No ANTHROPIC_API_KEY; offline — left proposal-less questions as-is.")
    else:
        print(f"Re-resolved {count} proposal-less question(s).")
    return 0


def cmd_dedup_oversize_escalations(args: argparse.Namespace) -> int:
    """Collapse existing duplicate oversize-page-family escalations (issue athenaeum#1430).

    See :func:`athenaeum.tiers.collapse_oversize_escalation_duplicates` for
    the semantics: for every entity with more than one unanswered
    ``oversize_page``/``oversize_split``/``oversize_log_demote`` block in
    ``wiki/_pending_questions.md``, keeps the single newest one and archives
    every other one into ``wiki/_pending_questions_archive.md`` -- never
    deletes. Pure file rewriting: no LLM client, no network call, unlike
    :func:`cmd_ingest_answers`/:func:`cmd_reresolve_questions` above.

    Safe to re-run: idempotent, and finds nothing to do once the corpus is
    already collapsed to one open block per entity.
    """
    from athenaeum.tiers import collapse_oversize_escalation_duplicates

    target = args.path.expanduser().resolve()
    if not target.exists():
        print(f"Knowledge directory not found: {target}", file=sys.stderr)
        print(
            f"Run 'athenaeum init --path {args.path}' first, then retry.",
            file=sys.stderr,
        )
        return 1

    pending_path = target / "wiki" / "_pending_questions.md"

    from athenaeum.config import load_config

    lock = _acquire_or_exit(target, args, load_config(target))  # issue athenaeum#309
    if isinstance(lock, int):
        return lock
    try:
        archived = collapse_oversize_escalation_duplicates(pending_path)
    except Exception as exc:  # noqa: BLE001 — surface a clean CLI error
        print(
            f"Fatal error collapsing oversize escalations ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 2
    finally:
        lock.release()

    print(f"Archived {archived} duplicate oversize-page-family escalation block(s).")
    return 0
