# SPDX-License-Identifier: Apache-2.0
"""``athenaeum {ingest-answers,ingest-merges,reresolve-questions}`` —
pending-sidecar maintenance.

Three subcommands grouped here because each is an idempotent, scheduler-safe
maintenance pass over one of the pending-decision sidecars
(``wiki/_pending_questions.md`` / ``wiki/_pending_merges.md``): archiving
resolved blocks into raw intake or an archive file, and self-healing
proposal-less open questions. All three share the same CLI shape (load
config, optionally build an LLM client via the provider seam, acquire the
run lock, delegate to an L3/L4 function, print a one-line summary).

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
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from athenaeum._cli_shared import _acquire_or_exit, _add_lock_args
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT

if TYPE_CHECKING:
    from athenaeum.answers import PendingQuestion

# Issue athenaeum#2518: recovers the canonical-side page id that
# `verdict_effects._apply_duplicate` embeds in its fold question's
# description ("... Proposed canonical side: a (some-page-id)."). This is
# the ONLY place that choice survives once the question is a plain markdown
# block — re-deriving it from the pages' current content would risk
# disagreeing with what was actually proposed and shown to the human.
_FOLD_CANONICAL_RE = re.compile(r"Proposed canonical side:\s*\w+\s*\((?P<id>[^)]+)\)")


def add_pending_subparsers(subparsers: argparse._SubParsersAction) -> None:
    """Register ``ingest-answers``, ``ingest-merges``, ``reresolve-questions``."""

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
    # Issue athenaeum#2076 (AC2): the per-code skipped-count breakdown names
    # only the error code, never the proposal — ``--verbose`` prints, for
    # every `fold_narrows_coordinate` / `fold_widen_impossible` skip, the
    # proposal id, dimension name, and the two values that could not be
    # widened. Additive only: without the flag, output is byte-for-byte
    # unchanged.
    ingest_answers_parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Print per-skip detail (proposal id, dimension, values) for "
        "fold_narrows_coordinate / fold_widen_impossible skips "
        "(issue athenaeum#2076).",
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


def _resolve_fold_question(
    pq: "PendingQuestion",
    *,
    wiki_root: Path,
    config: dict | None,
) -> tuple[str, str]:
    """Approve or reject one answered fold question (issue athenaeum#2518).

    Wired into :func:`athenaeum.answers.ingest_answers` as its
    ``fold_applier`` callback — a `duplicate`-verdict fold question (raised
    by :func:`athenaeum.verdict_effects._apply_duplicate` with
    ``raw_ref=f"comparator:{pair_key}"``, a ref that never resolves to a
    file) is handed to the normal merge machinery
    (:func:`athenaeum.pending_merges.write_pending_merge` +
    :func:`athenaeum.pending_merges.resolve_merge`) instead of
    :func:`athenaeum.answers._writeback_source`, which could never apply it
    — issue athenaeum#1804's exact failure mode for this one block shape.

    Lives here, in this L5 CLI module, rather than in ``athenaeum.answers``
    itself: :mod:`athenaeum.pending_merges` already imports several names
    FROM ``athenaeum.answers`` at module load, so an ``answers ->
    pending_merges`` edge in the other direction would be a 2-node import
    cycle (``tests/test_import_graph_acyclic.py``'s ``ALLOWED_SCCS`` is
    empty — no new cycle is tolerated). ``_cmd_pending`` sits above both
    (L5) and already imports each independently in
    :func:`cmd_ingest_answers`, so it is the one place that can call both
    without inverting either module's documented layering.

    Returns ``(outcome, detail)``:

    - ``"folded"`` — the "approve" answer queued AND resolved the proposal
      in this one call, so the pages are folded in this same
      ``ingest-answers`` pass (never a second merge-queue question for the
      pair — the proposal is written and immediately approved here, both
      ``write_pending_merge`` and ``resolve_merge`` have already run by the
      time this returns).
    - ``"refused"`` — "approve", but one of the normal merge path's own
      safety checks refused (e.g. ``no_git_repo``, ``fold_target_missing``,
      ``fold_widen_impossible``, ``fold_narrows_coordinate``); nothing was
      folded. ``detail`` names the check's ``error_code``.
    - ``"rejected"`` — a well-formed "reject" answer. Routed through the
      SAME ``write_pending_merge``/``resolve_merge`` pair (decision
      ``"reject"``) so the pair gets the usual
      ``merge_rejected_with:`` declaration on the canonical page — the
      existing mechanism that stops the comparator from re-raising the
      identical pair on its next run. No page content changes.
    - ``"held"`` — reserved for a block this function cannot even attempt:
      an unrecognized answer, a malformed pair key, or a page that no
      longer exists under *wiki_root*. Never returned for a well-formed
      "approve" or "reject" (issue athenaeum#2518 AC4).

    Never raises — every failure path is converted to one of the four
    outcomes above.
    """
    from athenaeum.answers import _parse_correction_answer
    from athenaeum.models import parse_frontmatter, slugify
    from athenaeum.pending_merges import (
        _make_id,
        filename_slug_by_uid,
        identity_slug,
        resolve_merge,
        write_pending_merge,
    )
    from athenaeum.verdicts import page_id_for_path

    # "approve"/"accept"/"yes"/... -> "ratify"; "reject"/"decline"/... ->
    # "reject"; anything else (including empty) -> None. Same closed
    # ratify/reject grammar _apply_field_correction already uses — reused
    # verbatim rather than re-deriving a second vocabulary.
    answer_token = _parse_correction_answer(pq)
    if answer_token is None:
        return "held", "answer is not a recognized approve/reject token"

    pair_key = pq.source.removeprefix("comparator:").strip()
    ids = pair_key.split("+", 1)
    if len(ids) != 2 or not all(ids):
        return "held", f"malformed fold pair key: {pq.source!r}"
    id_a, id_b = ids

    def _find_page(page_id: str) -> Path | None:
        for candidate in sorted(wiki_root.rglob("*.md")):
            if candidate.name.startswith("_"):
                continue
            if page_id_for_path(candidate) == page_id:
                return candidate
        return None

    path_a = _find_page(id_a)
    path_b = _find_page(id_b)
    if path_a is None or path_b is None:
        missing = id_a if path_a is None else id_b
        return "held", f"fold page {missing!r} not found under {wiki_root}"

    match = _FOLD_CANONICAL_RE.search(pq.description)
    canonical_id = match.group("id") if match else id_a
    canonical_path, other_path = (
        (path_a, path_b) if canonical_id == id_a else (path_b, path_a)
    )

    canonical_meta, _ = parse_frontmatter(canonical_path.read_text(encoding="utf-8"))
    name = canonical_meta.get("name") if isinstance(canonical_meta, dict) else None
    if not name or not str(name).strip():
        return "held", f"canonical page {canonical_path} has no usable name: frontmatter"
    merge_target_name = str(name)
    # Issue athenaeum#748/#2082 drift guard, same identity check `merges
    # propose-fold` applies at proposal time: the canonical's own filename
    # must identity-resolve to its `name:`/`uid:` or `write_pending_merge`
    # would derive the wrong write_kind.
    if identity_slug(canonical_path, canonical_meta) != slugify(merge_target_name):
        drift_slug = filename_slug_by_uid(canonical_path, canonical_meta)
        if drift_slug is None:
            return "held", (
                f"canonical page {canonical_path.name} identity does not "
                "resolve to its own name/uid frontmatter; refusing to guess "
                "the fold target"
            )
        merge_target_name = drift_slug

    sources = [str(canonical_path), str(other_path)]
    merge_id = _make_id(sources, merge_target_name)
    merges_path = wiki_root / "_pending_merges.md"

    try:
        write_pending_merge(
            merges_path,
            merge_target_name=merge_target_name,
            sources=sources,
            rationale=(
                f'Fold question {pq.id} (athenaeum#2518): operator answered '
                f'"{answer_token}" via ingest-answers.'
            ),
            # The canonical page's current text, verbatim — a fold's value
            # is alias bookkeeping + source tombstoning, never a body
            # rewrite (same default `merges propose-fold` uses with no
            # --draft-file).
            draft_merged_body=canonical_path.read_text(encoding="utf-8"),
            confidence=1.0,
        )
    except ValueError as exc:
        return "held", f"could not queue fold proposal: {exc}"

    decision: Literal["approve", "reject"] = (
        "approve" if answer_token == "ratify" else "reject"
    )
    result = resolve_merge(
        merges_path,
        merge_id,
        decision,
        note=f"{decision} via fold question {pq.id} (athenaeum#2518)",
        wiki_root=wiki_root,
    )
    if not result.get("ok"):
        if result.get("error_code") == "already_resolved":
            # Idempotent re-run posture (issue athenaeum#1804): a prior run
            # already resolved this exact proposal — report the terminal
            # outcome the decision implies rather than refusing again.
            return ("folded" if decision == "approve" else "rejected"), (
                f"merge {merge_id} already resolved"
            )
        return "refused", f"{result.get('error_code')}: {result.get('message')}"
    if decision == "reject":
        return "rejected", f"merge {merge_id} recorded; no pages changed"
    return "folded", f"folded into {canonical_path.name} (merge {merge_id})"


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
    from athenaeum.answers import IngestAnswersReport, ingest_answers
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
    verbose = getattr(args, "verbose", False)  # issue athenaeum#2076

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
        decision_report = apply_decision_answers(wiki_root, raw_root, config=cfg, lock=lock)
        # Issue athenaeum#1446: `--quiet` suppresses the per-block
        # `log.warning` noise `_parse_block`/`ingest_answers` emit by
        # level, not by structurally deleting the calls — restored in
        # `finally` so the mutation never leaks past this one call.
        answers_logger = logging.getLogger("athenaeum.answers")
        prior_level = answers_logger.level
        if quiet:
            answers_logger.setLevel(logging.ERROR)
        report = IngestAnswersReport()  # issue athenaeum#1804
        try:
            count = ingest_answers(
                pending_path,
                raw_root,
                client=anthropic_client,
                config=cfg,
                quiet=quiet,
                report=report,
                # Issue athenaeum#2518: wires the real fold-question apply —
                # see `_resolve_fold_question`'s docstring for why this
                # module, not `athenaeum.answers`, owns that function.
                fold_applier=lambda pq: _resolve_fold_question(
                    pq, wiki_root=wiki_root, config=cfg
                ),
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
        # Issue athenaeum#2069: the per-item ``error_code`` (``fold_narrows_coordinate``,
        # ``fold_widen_impossible``, ``already_resolved``, ...) is already on
        # every skipped outcome (`DecisionAnswerOutcome.error_code`) but was
        # only ever logged at INFO — invisible to anyone reading just this
        # summary line, so a guard refusal and every other skip reason looked
        # identical ("N skipped (see log)"). Break the count down by code
        # instead of asking the operator to go find the log.
        skipped_by_code: dict[str, int] = {}
        for outcome in decision_report.outcomes:
            if outcome.applied:
                continue
            code = outcome.error_code or "unknown"
            skipped_by_code[code] = skipped_by_code.get(code, 0) + 1
        breakdown = ", ".join(f"{code}: {n}" for code, n in sorted(skipped_by_code.items()))
        print(
            f"Applied {decision_report.applied} decision answer(s), "
            f"{decision_report.skipped} skipped"
            + (f" ({breakdown})" if breakdown else " (see log)")
            + "."
        )
        # Issue athenaeum#2076 (AC2): `--verbose` names the proposal, the
        # dimension, and the two values for each `fold_narrows_coordinate` /
        # `fold_widen_impossible` skip — the summary line above only ever
        # carries a per-code count. Additive only: without the flag this
        # block prints nothing, matching the pre-athenaeum#2076 output.
        if verbose:
            for outcome in decision_report.outcomes:
                if outcome.applied:
                    continue
                if outcome.error_code not in (
                    "fold_narrows_coordinate",
                    "fold_widen_impossible",
                ):
                    continue
                print(
                    f"  - {outcome.error_code}: proposal={outcome.decision_id} "
                    f"dimension={outcome.dimension_name!r} "
                    f"value_a={outcome.value_a!r} value_b={outcome.value_b!r}"
                )
    print(f"Ingested {count} answered question(s).")

    # Issue athenaeum#1804: a second summary line separating source files
    # written, held blocks (a write-back authorized but not yet possible),
    # and blocks archived with no write-back attempted, by class.
    # Issue athenaeum#1850: same line also reports ratified field-correction
    # applies/rejects — the "were not applied ... re-submit" sentence this
    # line used to append is gone; a field-correction answer now IS applied.
    no_writeback_total = sum(report.archived_no_writeback.values())
    no_writeback_parts = ", ".join(
        f"{cls}: {n}" for cls, n in sorted(report.archived_no_writeback.items())
    )
    summary = (
        f"Write-back: {report.files_written} source file(s) written, "
        f"{report.held} held, {no_writeback_total} archived without write-back"
    )
    if no_writeback_parts:
        summary += f" ({no_writeback_parts})"
    summary += (
        f", {report.corrections_applied} correction(s) applied, "
        f"{report.corrections_rejected} rejected"
    )
    # Issue athenaeum#2518: fold questions never pass through
    # `_writeback_source`/`archived_no_writeback` — their own counters.
    summary += (
        f", {report.folds_applied} fold(s) applied, "
        f"{report.folds_rejected} fold(s) rejected, "
        f"{report.folds_refused} fold(s) refused."
    )
    print(summary)
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
    from athenaeum.decision_provider import (
        DecisionProviderConfigError,
        build_decision_client,
        preflight_decision_provider,
        resolve_decision_provider,
    )
    from athenaeum.provider import ProviderConfigError, build_llm_client
    from athenaeum.tiers import reresolve_open_questions

    target = args.path.expanduser().resolve()
    if not target.exists():
        print(f"Knowledge directory not found: {target}", file=sys.stderr)
        return 1

    pending_path = target / "wiki" / "_pending_questions.md"
    cfg = load_config(target)

    # Issue athenaeum#1997: resolve + preflight the Jev decision-provider knob
    # ONCE here (never per-contradiction inside propose_resolution) so a
    # missing JEV_API_KEY fails loudly at startup, mirroring the
    # build_llm_client/preflight_provider pattern immediately below. Default
    # (``llm.decision_providers.resolve`` unset) resolves to "none" and
    # ``decision_backend`` stays ``None`` — byte-identical to pre-athenaeum#1997.
    try:
        decision_provider_cfg = resolve_decision_provider(cfg, "resolve")
    except DecisionProviderConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    decision_preflight_err = preflight_decision_provider(decision_provider_cfg.provider)
    if decision_preflight_err:
        print(f"error: {decision_preflight_err}", file=sys.stderr)
        return 1
    decision_backend = build_decision_client(decision_provider_cfg)

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
            pending_path,
            client=anthropic_client,
            config=cfg,
            decision_backend=decision_backend,
            decision_redact_outbound=decision_provider_cfg.redact_outbound,
        )
    except Exception as exc:  # noqa: BLE001 — surface a clean CLI error
        print(
            f"Fatal error re-resolving questions ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 2
    finally:
        lock.release()

    if anthropic_client is None and decision_backend is None:
        print("No ANTHROPIC_API_KEY; offline — left proposal-less questions as-is.")
    else:
        print(f"Re-resolved {count} proposal-less question(s).")
    return 0
