# SPDX-License-Identifier: Apache-2.0
"""``athenaeum {ingest-answers,ingest-merges,reresolve-questions}`` — pending-sidecar maintenance.

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
import sys
from pathlib import Path

from athenaeum._cli_shared import _acquire_or_exit, _add_lock_args
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT


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
    _add_lock_args(ingest_answers_parser)
    ingest_answers_parser.set_defaults(func=cmd_ingest_answers)

    # ingest-merges command (issue #299) — move resolved (`[x]`) blocks out
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

    # reresolve-questions command (issue #188) — re-run the resolver on OPEN,
    # PROPOSAL-LESS pending questions so a prior cap-hit / offline escalation
    # self-heals. Budget-aware + idempotent; offline (no key) is a no-op.
    reresolve_parser = subparsers.add_parser(
        "reresolve-questions",
        help="Re-resolve open proposal-less pending questions "
        "(self-heal transient cap/offline escalations, issue #188)",
    )
    reresolve_parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    _add_lock_args(reresolve_parser)
    reresolve_parser.set_defaults(func=cmd_reresolve_questions)


def cmd_ingest_answers(args: argparse.Namespace) -> int:
    """Ingest answered blocks from `_pending_questions.md` as raw intake.

    See :func:`athenaeum.answers.ingest_answers` for the semantics.

    Builds the LLM client via the provider seam (``build_llm_client``, #330)
    and passes it to ``ingest_answers`` so free-text answers can use the
    LLM-backed proposer (issue #210): a ``claude-cli`` subscription client, or
    an Anthropic SDK client when ``provider: api`` and ``ANTHROPIC_API_KEY`` is
    set. When the key is absent (api backend) or construction fails, the
    annotation fallback is used instead.
    """
    from athenaeum.answers import ingest_answers
    from athenaeum.config import load_config
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

    cfg = load_config(target)

    # Issue #210/#330: build the LLM client via the provider seam so free-text
    # answers trigger the LLM-backed source-edit proposer. Returns None for the
    # ``api`` backend with no ANTHROPIC_API_KEY (offline annotation fallback);
    # returns the subscription CLI client for ``claude-cli``. Fail gracefully
    # (None) on any construction error.
    anthropic_client = None
    try:
        anthropic_client = build_llm_client(cfg)
    except ProviderConfigError as exc:
        # Issue #540 (M14): a provider MISCONFIGURATION (e.g. a typo in the
        # backend name) is raised loudly by build_llm_client precisely so it
        # never silently falls back to a different backend. Surface it and
        # exit nonzero rather than swallowing it into the offline fallback and
        # exiting 0 — the exact silent-backend-fallback provider.py forbids.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception:
        # API key for the api backend) is the intended offline fallback: leave
        # the client None and let the caller degrade. Only ProviderConfigError
        # (a misconfig) is fatal, handled above.
        pass

    lock = _acquire_or_exit(target, args, cfg)  # issue #309
    if isinstance(lock, int):
        return lock
    try:
        count = ingest_answers(
            pending_path, raw_root, client=anthropic_client, config=cfg
        )
    except Exception as exc:
        print(
            f"Fatal error ingesting answers ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 2
    finally:
        lock.release()

    print(f"Ingested {count} answered question(s).")
    return 0


def cmd_ingest_merges(args: argparse.Namespace) -> int:
    """Archive resolved blocks from `wiki/_pending_merges.md` (issue #299).

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

    lock = _acquire_or_exit(target, args, load_config(target))  # issue #309
    if isinstance(lock, int):
        return lock
    try:
        count = ingest_resolved_merges(merges_path)
    except Exception as exc:
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
    """Re-resolve open, proposal-less pending questions (issue #188).

    Mirrors :func:`cmd_ingest_answers`: loads config, builds the LLM client
    via the provider seam (``build_llm_client``, #330 — a subscription
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

    # Issue #330: construct via the provider seam (api key -> SDK client;
    # claude-cli -> subscription CLI client; None when the api backend has no
    # key, preserving the offline no-op below).
    anthropic_client = None
    try:
        anthropic_client = build_llm_client(cfg)
    except ProviderConfigError as exc:
        # Issue #540 (M14): a provider MISCONFIGURATION (e.g. a typo in the
        # backend name) is raised loudly by build_llm_client precisely so it
        # never silently falls back to a different backend. Surface it and
        # exit nonzero rather than swallowing it into the offline fallback and
        # exiting 0 — the exact silent-backend-fallback provider.py forbids.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception:
        # API key for the api backend) is the intended offline fallback: leave
        # the client None and let the caller degrade. Only ProviderConfigError
        # (a misconfig) is fatal, handled above.
        pass

    lock = _acquire_or_exit(target, args, cfg)  # issue #309
    if isinstance(lock, int):
        return lock
    try:
        count = reresolve_open_questions(
            pending_path, client=anthropic_client, config=cfg
        )
    except Exception as exc:
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
