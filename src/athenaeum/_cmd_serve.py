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
import ipaddress
import os
import sys
from pathlib import Path

from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT, SERVE_TRANSPORTS
from athenaeum.logconf import configure_logging

#: Default URL path the streamable-HTTP endpoint is mounted at (issue
#: athenaeum#2382). CLI-only, deliberately: ``--transport``/``--host``/
#: ``--port`` each resolve CLI > env > yaml because an operator plausibly
#: pins them per deployment, whereas the mount path is a property of the
#: client registration and has no yaml key or env var in this issue's scope.
DEFAULT_SERVE_PATH_PREFIX = "/mcp"

#: The ``athenaeum.yaml`` setting an operator must supply before athenaeum
#: will bind anything other than a loopback address (issue athenaeum#2382).
#: NOTHING in this module reads it yet: the OAuth 2.1 resource-server
#: settings are the auth child's (athenaeum#2384) to add, and until they
#: exist the refusal below is unconditional for a non-loopback address. The
#: name is named here anyway so the refusal tells the operator what is
#: missing rather than only what is forbidden.
AUTH_ISSUER_SETTING = "serve.auth.issuer"


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
    serve_parser.add_argument(
        "--transport",
        choices=SERVE_TRANSPORTS,
        default=None,
        help="MCP transport (default: stdio). 'stdio' speaks MCP over this "
        "process's own stdin/stdout and binds no socket -- the historical, "
        "unchanged behaviour. 'http' serves FastMCP's streamable HTTP "
        "transport on --host/--port under --path-prefix. HTTP is opt-in "
        "because it binds a listening socket, and while no auth issuer is "
        "configured it refuses any non-loopback --host before binding. "
        "Overrides ATHENAEUM_SERVE_TRANSPORT and serve.transport in "
        "athenaeum.yaml.",
    )
    serve_parser.add_argument(
        "--host",
        type=str,
        default=None,
        help="Bind address for --transport http (default: 127.0.0.1). While "
        "no auth issuer is configured only a loopback address literal is "
        "accepted: any 127.0.0.0/8 address, the IPv6 loopback [::1] (brackets "
        "optional), or an IPv4-mapped IPv6 loopback. Every other address, and "
        "every hostname (names are never resolved), is refused with a "
        "non-zero exit before anything binds. Ignored under stdio. Overrides "
        "ATHENAEUM_SERVE_HOST and serve.host in athenaeum.yaml.",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="TCP port for --transport http (default: 8000, FastMCP's own "
        "default). Ignored under stdio. Overrides ATHENAEUM_SERVE_PORT and "
        "serve.port in athenaeum.yaml.",
    )
    serve_parser.add_argument(
        "--path-prefix",
        type=str,
        default=DEFAULT_SERVE_PATH_PREFIX,
        help="URL path the streamable HTTP endpoint is mounted at (default: "
        "/mcp). Ignored under stdio. CLI-only: unlike --transport, --host and "
        "--port this knob has no environment variable and no athenaeum.yaml "
        "key.",
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


def _parse_bind_address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse *host* as an IP address literal, or ``None`` if it is not one.

    Accepts the bracketed IPv6 form (``[::1]``) a URL or a copy-pasted
    endpoint carries, since that is what an operator reads off a client
    configuration. A name is NEVER resolved: resolution would make the
    loopback decision depend on ``/etc/hosts``, DNS and search domains, so a
    hostname that resolves to ``127.0.0.1`` today could resolve elsewhere
    tomorrow with no change to athenaeum's configuration. Anything that is
    not an address literal returns ``None`` and is therefore not loopback --
    which is also what makes a name that merely LOOKS loopback
    (``127.0.0.1.example.invalid``) and a non-literal numeric form
    (``0177.0.0.1``, which the dotted-octal reading would make loopback)
    refusals rather than accidental allowances.
    """
    candidate = host.strip()
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


def _is_loopback_bind_address(host: str) -> bool:
    """True only for an address literal that is definitely loopback.

    A positive allowlist, deliberately -- the question asked is "is this one
    of the addresses we permit", never "does this look dangerous". The three
    accepted shapes are IPv4 loopback (``127.0.0.0/8``), IPv6 loopback
    (``::1``, bare or bracketed) and an IPv4-MAPPED IPv6 loopback
    (``::ffff:127.0.0.1``). The last needs its own branch:
    ``IPv6Address.is_loopback`` is ``True`` only for ``::1`` itself, so
    ``::ffff:127.0.0.1`` -- which binds the IPv4 loopback interface -- would
    otherwise be refused as if it were routable. Every other input,
    ``0.0.0.0`` and ``::`` included, is False.
    """
    address = _parse_bind_address(host)
    if address is None:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped.is_loopback
    return address.is_loopback


def _normalized_bind_host(host: str) -> str:
    """The form of *host* the socket layer can actually bind.

    :func:`_parse_bind_address` deliberately accepts the BRACKETED IPv6
    literal, because that is the shape an operator copies out of a URL or a
    client configuration -- but ``[::1]`` is a URL authority, not an address:
    ``getaddrinfo`` rejects it, so passing it through would turn an accepted
    host into an ugly bind failure. Normalising through the parsed literal
    hands ``::1`` to uvicorn instead. A value that does not parse is returned
    trimmed and unchanged; that path is unreachable while the loopback guard
    holds, and guessing at a non-literal is not this function's business.
    """
    address = _parse_bind_address(host)
    return str(address) if address is not None else host.strip()


def _non_loopback_bind_refusal(host: str) -> str | None:
    """The refusal message for binding *host*, or ``None`` if it may be bound.

    This is the security property ``--transport http`` exists behind: an
    unauthenticated streamable-HTTP endpoint on a routable address publishes
    the whole knowledge store to whoever can reach it. The refusal is
    evaluated BEFORE ``server.run`` is called, so a refused invocation never
    opens a listening socket, and it names the missing setting
    (:data:`AUTH_ISSUER_SETTING`) rather than only reporting that the address
    is not allowed. Once that setting exists (athenaeum#2384) the auth child
    lifts the refusal for a configured issuer; until then it holds for every
    non-loopback address.
    """
    if _is_loopback_bind_address(host):
        return None
    message = (
        f"[transport] refusing to bind {host!r}: with no auth issuer configured, "
        f"`athenaeum serve --transport http` accepts only a loopback bind address. "
        f"Nothing was bound.\n"
        f"       Set `{AUTH_ISSUER_SETTING}` in athenaeum.yaml to serve a "
        f"non-loopback address, or bind a loopback one: 127.0.0.0/8 "
        f"(e.g. 127.0.0.1), ::1 or [::1], or an IPv4-mapped loopback such as "
        f"::ffff:127.0.0.1."
    )
    if _parse_bind_address(host) is None:
        message += (
            f"\n       {host!r} is not an IP address literal: the bind address is "
            f"matched against a loopback allowlist and no name is ever resolved, "
            f"so even `localhost` is refused -- pass 127.0.0.1 or ::1 instead."
        )
    return message


def cmd_serve(args: argparse.Namespace) -> int:
    from athenaeum.config import (
        load_config,
        resolve_audience,
        resolve_cache_dir,
        resolve_extra_intake_roots,
        resolve_screening,
        resolve_serve_excluded_fields,
        resolve_serve_host,
        resolve_serve_port,
        resolve_serve_profile,
        resolve_serve_tools,
        resolve_serve_transport,
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

    # Issue athenaeum#2382: resolve the transport (CLI > env > yaml) and, for
    # the HTTP transport, settle the bind address BEFORE any other work --
    # earliest possible failure, and in particular before `server.run` could
    # open a listening socket.
    transport = resolve_serve_transport(cfg, getattr(args, "transport", None))
    serve_host = resolve_serve_host(cfg, getattr(args, "host", None))
    serve_port = resolve_serve_port(cfg, getattr(args, "port", None))
    path_prefix = getattr(args, "path_prefix", DEFAULT_SERVE_PATH_PREFIX)
    if transport == "http":
        refusal = _non_loopback_bind_refusal(serve_host)
        if refusal is not None:
            print(refusal, file=sys.stderr)
            return 1
        serve_host = _normalized_bind_host(serve_host)

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
        if transport == "http":
            # A bare IPv6 literal needs bracketing to be a valid URL
            # authority, and this line is what an operator copies into a
            # client's server configuration.
            authority = f"[{serve_host}]" if ":" in serve_host else serve_host
            print(
                f"[transport] streamable HTTP on "
                f"http://{authority}:{serve_port}{path_prefix}",
                file=sys.stderr,
            )
            server.run(
                transport="http",
                host=serve_host,
                port=serve_port,
                path=path_prefix,
            )
        else:
            # stdio stays byte-identical to the pre-athenaeum#2382 call: no
            # transport argument, no host/port, nothing bound.
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
