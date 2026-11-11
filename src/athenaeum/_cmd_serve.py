# SPDX-License-Identifier: Apache-2.0
"""``athenaeum serve`` — start the MCP memory server.

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` — this
is where a NEW subcommand goes, not inline in ``cli.py``'s ``main()``. This
module may import library modules (L4/L3) but ``cli.py`` only imports the
``add_*_subparser`` entry point, kept lazy/local to keep top-level import cost
down.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT
from athenaeum.logconf import configure_logging


def add_serve_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum serve`` and its flags on *subparsers*."""
    serve_parser = subparsers.add_parser("serve", help="Start the MCP memory server")
    serve_parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge). The raw/wiki roots "
        "default to <path>/raw and <path>/wiki; the KNOWLEDGE_RAW_PATH / "
        "KNOWLEDGE_WIKI_PATH environment variables override them individually "
        "(drop-in parity with the legacy knowledge-mcp server, issue athenaeum#355).",
    )
    serve_parser.add_argument(
        "--audience",
        type=str,
        default=None,
        help="Issue athenaeum#312: pin this server to a restricted read scope. "
        "Comma-separated role/group ids (e.g. 'operations,voltaire'). The "
        "recall tool then returns only pages tagged for one of these roles "
        "(plus 'access: open' pages); untagged/confidential/personal pages "
        "are withheld. Unset = owner = full access. Overrides "
        "ATHENAEUM_AUDIENCE and serve.audience in athenaeum.yaml.",
    )
    # Issue athenaeum#2383: the three serve-time remote-profile pins. Help text
    # carries no issue ref on purpose -- `scripts/gen_cli_reference.py` strips
    # the ref out of `docs/reference/cli.md` and would leave a dangling "Issue
    # :" prefix on the operator-facing row.
    serve_parser.add_argument(
        "--profile",
        type=str,
        default=None,
        help="Apply a serve-time profile preset. "
        "'remote' pins this server for exposure to a third-party host: the "
        "three owner-only decision-queue mutators (resolve_question, "
        "resolve_merge, review_audit_item) are not registered at all, while "
        "remember and raise_decision stay available, and excluded fields are "
        "withheld (see --excluded-fields). Overrides ATHENAEUM_SERVE_PROFILE "
        "and serve.profile in athenaeum.yaml. Unset = no preset = unchanged "
        "behaviour.",
    )
    serve_parser.add_argument(
        "--tools",
        type=str,
        default=None,
        help="Pin this server to an ALLOWLIST of MCP "
        "tools. Comma-separated tool names (e.g. 'recall,read_entity'). A "
        "tool outside the list is never registered, so it is absent from "
        "tools/list rather than refusing when called; a name that is not a "
        "tool of this deployment fails startup. Composes with --profile (a "
        "tool must clear both). Overrides ATHENAEUM_SERVE_TOOLS and "
        "serve.tools in athenaeum.yaml. Unset = no allowlist.",
    )
    serve_parser.add_argument(
        "--excluded-fields",
        type=str,
        default=None,
        choices=["off", "on"],
        help="Whether this server may resolve excluded "
        "(off-corpus) fields such as a person's contact data. 'on' is the "
        "historical behaviour -- recall(with_pii=True) and "
        "read_entity(include_excluded=True) return the values. 'off' "
        "withholds them: recall returns a withheld notice instead, "
        "read_entity returns none, and enumerate_entities refuses a "
        "PII-scoped predicate or output field. Defaults to 'off' under "
        "--profile remote, otherwise 'on'. Overrides "
        "ATHENAEUM_SERVE_EXCLUDED_FIELDS and serve.excluded_fields in "
        "athenaeum.yaml.",
    )
    serve_parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory holding the compiled index (default: "
        "ATHENAEUM_CACHE_DIR env, else ~/.cache/athenaeum). Issue athenaeum#521: serve "
        "previously hardcoded ~/.cache/athenaeum and ignored ATHENAEUM_CACHE_DIR, "
        "so recall could serve a stale/empty index when the compiler wrote "
        "elsewhere.",
    )
    serve_parser.set_defaults(func=cmd_serve)


def _resolve_serve_roots(target: Path) -> tuple[Path, Path]:
    """Resolve the raw/wiki roots for ``athenaeum serve``.

    Defaults to ``<target>/raw`` and ``<target>/wiki``. When set, the
    ``KNOWLEDGE_RAW_PATH`` / ``KNOWLEDGE_WIKI_PATH`` environment variables
    override the respective root INDEPENDENTLY. This preserves drop-in parity
    with the legacy standalone ``knowledge-mcp`` server this command supersedes
    (issue athenaeum#355): an existing MCP config (or ``start.sh``) that pins those env
    vars keeps working unchanged after the cwc copy is removed. ``--path`` is
    still where config (``athenaeum.yaml``) and extra intake roots resolve, so
    the common case (both under ``~/knowledge``) is unaffected.
    """
    raw_env = os.environ.get("KNOWLEDGE_RAW_PATH")
    wiki_env = os.environ.get("KNOWLEDGE_WIKI_PATH")
    raw_root = Path(raw_env).expanduser().resolve() if raw_env else target / "raw"
    wiki_root = Path(wiki_env).expanduser().resolve() if wiki_env else target / "wiki"
    return raw_root, wiki_root


def cmd_serve(args: argparse.Namespace) -> int:
    from athenaeum.config import (
        load_config,
        resolve_audience,
        resolve_cache_dir,
        resolve_extra_intake_roots,
        resolve_screening,
        resolve_serve_excluded_fields,
        resolve_serve_profile,
        resolve_serve_tools,
    )
    from athenaeum.mcp_server import create_server
    from athenaeum.remote_policy import RemotePolicyError, build_remote_policy

    # Issue athenaeum#540 (M25): the MCP server process previously configured NO logging
    # at all — every mcp_server log line was dropped. Configure the shared
    # ISO-dated, name-tagged, run-id format here so the server's lines are
    # attributable, same as the CLI's.
    configure_logging(verbose=getattr(args, "verbose", False))

    target = args.path.expanduser().resolve()
    raw_root, wiki_root = _resolve_serve_roots(target)

    if not target.exists():
        print(f"Knowledge directory not found: {target}")
        print(f"Run 'athenaeum init --path {args.path}' first, then retry.")
        return 1

    cfg = load_config(target)
    backend = cfg.get("search_backend", "fts5")
    # Issue athenaeum#521 (H9): route serve's cache dir through the shared resolver so
    # ATHENAEUM_CACHE_DIR (and --cache-dir) are honoured — previously hardcoded,
    # so recall served a stale/empty index when the compiler wrote elsewhere.
    cache_dir = resolve_cache_dir(getattr(args, "cache_dir", None))
    extra_roots = resolve_extra_intake_roots(target, cfg)

    # Issue athenaeum#312: resolve the serve-time read-scope pin (CLI > env > yaml).
    # None = owner = full access (existing single-user behavior).
    caller_audience = resolve_audience(cfg, getattr(args, "audience", None))
    if caller_audience is not None:
        print(
            "[audience] recall restricted to roles: "
            f"{', '.join(sorted(caller_audience))} "
            "(untagged/confidential/personal pages withheld)",
            file=sys.stderr,
        )

    # Issue athenaeum#320: resolve intake screening (env > yaml > off). Fails fast with
    # a clear message on a mis-set screening block rather than serving with a
    # silently inert classifier.
    from athenaeum.screening import ScreeningConfigError

    try:
        screening = resolve_screening(cfg)
    except ScreeningConfigError as exc:
        print(f"[screening] invalid configuration: {exc}", file=sys.stderr)
        return 1
    if screening["medical"]["action"] != "off":
        print(
            "[screening] medical intake → "
            f"{screening['medical']['action']} "
            f"(access: {screening['medical']['access']})",
            file=sys.stderr,
        )

    # Issue athenaeum#2383: resolve the serve-time remote-profile pins (CLI >
    # env > yaml, per pin). Fails fast on an invalid value for the same reason
    # `resolve_screening` above does -- the only direction a mis-set safety pin
    # must never fail in is "wider than the operator asked for".
    try:
        remote_policy = build_remote_policy(
            profile=resolve_serve_profile(cfg, getattr(args, "profile", None)),
            tools=resolve_serve_tools(cfg, getattr(args, "tools", None)),
            excluded_fields=resolve_serve_excluded_fields(
                cfg, getattr(args, "excluded_fields", None)
            ),
        )
    except RemotePolicyError as exc:
        print(f"[remote-policy] invalid configuration: {exc}", file=sys.stderr)
        return 1
    if remote_policy is not None:
        print(
            "[remote-policy] excluded fields: "
            f"{'on' if remote_policy.excluded_fields else 'off (withheld)'}"
            + (
                ""
                if remote_policy.allowed_tools is None
                else f"; tool allowlist: {', '.join(sorted(remote_policy.allowed_tools))}"
            )
            + (
                ""
                if not remote_policy.disabled_tools
                else "; tools not served: "
                f"{', '.join(sorted(remote_policy.disabled_tools))}"
            ),
            file=sys.stderr,
        )

    # Warn on config/cache mismatch. The recall tool silently returns zero
    # hits when the configured backend's index is missing, so users with
    # `search_backend: vector` but an fts5-only cache (common when you flip
    # backends in athenaeum.yaml but forget to rebuild) see recall "work"
    # but return nothing. Catch that up front.
    _warn_if_backend_cache_missing(backend, cache_dir)

    # The tool allowlist is validated INSIDE create_server, against the tools
    # it actually registered -- the only drift-free place to check a name
    # (issue athenaeum#2383).
    try:
        server = create_server(
            raw_root=raw_root,
            wiki_root=wiki_root,
            search_backend=backend,
            cache_dir=cache_dir,
            extra_roots=extra_roots,
            caller_audience=caller_audience,
            screening=screening,
            remote_policy=remote_policy,
            config=cfg,
        )
    except RemotePolicyError as exc:
        print(f"[remote-policy] invalid configuration: {exc}", file=sys.stderr)
        return 1
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    return 0


def _warn_if_backend_cache_missing(backend: str, cache_dir: Path) -> None:
    """Print a warning if the configured backend has no cache on disk.

    The keyword backend has no cache. FTS5 expects ``wiki-index.db``;
    vector expects ``wiki-vectors/``. When either is missing, recall
    silently returns empty — the warning tells the user to run
    ``athenaeum rebuild-index``.
    """
    if backend == "keyword":
        return
    if backend == "fts5":
        if not (cache_dir / "wiki-index.db").is_file():
            print(
                f"[warn] search_backend=fts5 but no index at "
                f"{cache_dir / 'wiki-index.db'}.\n"
                f"       Run `athenaeum rebuild-index --path <knowledge>` "
                f"before relying on recall.",
                file=sys.stderr,
            )
        return
    if backend == "vector":
        if not (cache_dir / "wiki-vectors").is_dir():
            print(
                f"[warn] search_backend=vector but no index at "
                f"{cache_dir / 'wiki-vectors'}.\n"
                f"       Run `athenaeum rebuild-index --path <knowledge>` "
                f"before relying on recall.",
                file=sys.stderr,
            )
        return
    print(
        f"[warn] unknown search_backend {backend!r}; "
        f"recall will fail until this is fixed in athenaeum.yaml.",
        file=sys.stderr,
    )
