"""``athenaeum demo`` — the one-command front door onto the recall viewer.

Issue athenaeum#1525. :mod:`athenaeum._cmd_viewer` already serves everything
worth looking at; what it does not do is spare the operator the three pieces of
ceremony standing between them and the page: find the Claude session id, pick a
port nothing else has taken, and remember the URL. Mid-demo, each of those is a
chance to fumble in front of an audience, and the first one is worse than a
fumble — ``athenaeum viewer`` needs an EXACT ``--session``, and a wrong id
renders a perfectly well-formed EMPTY page. Empty is indistinguishable from
"recall is broken" to anyone watching, including the operator.

So this command is deliberately thin, and everything in it exists to remove one
of those failure modes:

- **Session id resolves itself.** ``CLAUDE_CODE_SESSION_ID``, then
  ``CLAUDE_SESSION_ID``, then the newest transcript basename under
  ``~/.claude/projects``. The first of those is set inside a Claude Code Bash
  environment, which is what lets a Claude session run ``athenaeum demo`` with
  no arguments and correctly scope to ITSELF. Resolution order matches
  athenaeum's own recorder — if the two ever disagree, the viewer scopes to a
  session the recorder never wrote to.

- **Port never collides.** :data:`_cmd_viewer.DEFAULT_PORT` is tried first so
  the URL is predictable across runs, and a bind failure falls back to an
  OS-assigned free port rather than dying. A demo that refuses to start because
  a viewer is already up is a worse outcome than a demo on an unexpected port.

- **The browser opens, after the socket is bound.** Ordering is load-bearing:
  opening first races the listener and can greet the operator with a connection
  error on a server that is about to work fine.

- **Zero rows warn BEFORE anything paints.** And a probe that FAILED warns
  differently from a probe that legitimately counted zero — see
  :func:`_probe_rows`. Collapsing those two is how an operator gets confident
  advice about a session id that was never actually in question.

This module consumes :mod:`athenaeum._cmd_viewer`'s public helpers
(:func:`~athenaeum._cmd_viewer.make_server`,
:func:`~athenaeum._cmd_viewer.build_viewer_data`) and, through them, the
documented ``push-metrics tail --json`` NDJSON contract (issue athenaeum#1479).
It never opens a ledger file and never imports :mod:`athenaeum.push_metrics` —
the same contract-boundary rule the viewer holds itself to.

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in its
own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser``.
"""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from http.server import HTTPServer
from pathlib import Path

from athenaeum._cmd_viewer import (
    DEFAULT_EDITOR_COMMAND,
    DEFAULT_PORT,
    ViewerContractError,
    build_viewer_data,
    make_server,
    resolve_editor_command,
    warn_if_editor_missing,
)
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT

#: Default Claude Code transcript root. Overridable via the ``--projects-root``
#: flag so the test suite never has to read the operator's real transcripts.
DEFAULT_PROJECTS_ROOT = Path("~/.claude/projects")

#: Environment variables carrying the active session id, in the order
#: athenaeum's own push recorder consults them. Keeping these two sides
#: identical is the whole point: a mismatch scopes the viewer to a session the
#: recorder never wrote.
_SESSION_ID_ENV_VARS = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")


def resolve_session_id(
    *,
    projects_root: Path | None = None,
    environ: dict[str, str] | None = None,
) -> str | None:
    """Best-effort active-session id, or ``None`` if nothing can be resolved.

    Environment first (authoritative — it is the running session saying who it
    is), newest transcript second (an inference, and only right when the newest
    transcript happens to be the caller's own).
    """
    env = os.environ if environ is None else environ
    for var in _SESSION_ID_ENV_VARS:
        value = (env.get(var) or "").strip()
        if value:
            return value

    root = (projects_root or DEFAULT_PROJECTS_ROOT).expanduser()
    newest: Path | None = None
    newest_mtime = float("-inf")
    try:
        candidates = root.glob("*/*.jsonl")
    except OSError:
        return None
    for candidate in candidates:
        try:
            mtime = candidate.stat().st_mtime
        except OSError:
            # A transcript can vanish between glob and stat; skip it rather
            # than fail the whole launch over one unreadable file.
            continue
        if mtime > newest_mtime:
            newest, newest_mtime = candidate, mtime
    return newest.stem if newest is not None else None


def bind_server(
    *,
    session_id: str | None,
    path: Path,
    cache_dir: Path | None,
    port: int,
    editor_command: tuple[str, ...] = DEFAULT_EDITOR_COMMAND,
) -> tuple[HTTPServer, bool]:
    """Bind the viewer server, falling back to an OS-assigned port.

    Returns ``(server, fell_back)``. *fell_back* is ``True`` when *port* was
    unavailable and the OS picked one instead, so the caller can say so rather
    than leaving the operator to wonder why the URL changed.

    ``port=0`` is honored directly and never reported as a fallback — asking
    for an arbitrary port and getting one is the request being satisfied, not a
    degraded outcome.
    """
    try:
        return (
            make_server(
                session_id=session_id,
                path=path,
                cache_dir=cache_dir,
                port=port,
                editor_command=editor_command,
            ),
            False,
        )
    except OSError:
        if port == 0:
            raise
    return (
        make_server(
            session_id=session_id,
            path=path,
            cache_dir=cache_dir,
            port=0,
            editor_command=editor_command,
        ),
        True,
    )


def _probe_rows(*, session_id: str | None, path: Path, cache_dir: Path | None) -> int | None:
    """Row count for *session_id*, or ``None`` when the probe itself failed.

    ``None`` is NOT zero and the caller must not treat it as such. Zero is a
    fact about the session ("nothing recorded — most likely the wrong id");
    ``None`` means the probe never answered, about which the only honest thing
    to say is that the row count is unknown. Folding a failure into ``0`` fires
    confident wrong-session-id advice at an operator whose id was fine, and
    then a correctly-populated view paints underneath it with nothing on screen
    to adjudicate between the two.
    """
    try:
        data = build_viewer_data(session_id=session_id, path=path, cache_dir=cache_dir)
    except (ViewerContractError, OSError, ValueError):
        return None
    return sum(
        len(data.get(bucket) or ())
        for bucket in ("pushed_unbidden", "pulled_deliberately", "overlap")
    )


def _report_rows(rows: int | None, session_id: str) -> None:
    """Say what the probe found, on stderr, before the browser opens."""
    if rows is None:
        print(
            f"warning: could not count recall rows for session {session_id}. "
            "This is a probe failure and says NOTHING either way about the "
            "session id — judge the view on what it paints.",
            file=sys.stderr,
        )
    elif rows == 0:
        print(
            f"warning: session {session_id} has no recall rows yet, so the page "
            "will render EMPTY. That usually means the session id is wrong "
            "rather than that recall is broken — pass --session with a "
            "known-good id. If this session has simply not triggered a recall "
            "yet, rows appear as you work; reload the page.",
            file=sys.stderr,
        )
    else:
        print(f"session {session_id}: {rows} recall rows recorded", file=sys.stderr)


def cmd_demo(args: argparse.Namespace) -> int:
    """``athenaeum demo`` — resolve, bind, announce, open, serve."""
    path = (args.path or DEFAULT_KNOWLEDGE_ROOT).expanduser().resolve()
    session_id = args.session or resolve_session_id(projects_root=args.projects_root)
    if not session_id:
        print(
            "error: could not resolve a Claude session id. Pass --session ID, "
            "set CLAUDE_CODE_SESSION_ID, or check --projects-root.",
            file=sys.stderr,
        )
        return 1

    editor = resolve_editor_command(getattr(args, "editor", None))
    server, fell_back = bind_server(
        session_id=session_id,
        path=path,
        cache_dir=args.cache_dir,
        port=args.port,
        editor_command=editor,
    )
    warn_if_editor_missing(editor)
    host, bound_port = str(server.server_address[0]), server.server_address[1]
    url = f"http://{host}:{bound_port}/"

    if fell_back:
        print(
            f"note: port {args.port} was busy; using {bound_port} instead.",
            file=sys.stderr,
        )
    _report_rows(
        _probe_rows(session_id=session_id, path=path, cache_dir=args.cache_dir), session_id
    )

    # flush=True is load-bearing, not tidiness. stdout is block-buffered when
    # redirected to a file or pipe, and `serve_forever` below blocks forever --
    # so without an explicit flush this line sits in the buffer for the entire
    # life of the server and only appears once it is killed. A caller that
    # captures stdout to find the URL (a Claude session launching this in the
    # background is the motivating case) would see nothing at all and conclude
    # the command hung.
    print(f"athenaeum demo: {url} (Ctrl-C to stop)", flush=True)
    if not args.no_browser:
        # After the bind, never before: opening first races the listener and
        # can show a connection error for a server that is about to be fine.
        # Failure to open is not fatal — the URL is already printed above.
        try:
            webbrowser.open(url)
        except Exception as exc:  # noqa: BLE001 — a headless box must still serve
            print(f"note: could not open a browser ({exc}); open {url} yourself.", file=sys.stderr)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def add_demo_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum demo`` and its flags on *subparsers*."""
    demo_p = subparsers.add_parser(
        "demo",
        help=(
            "Open the recall viewer for the current Claude session — resolves "
            "the session id, picks a free port, and opens a browser."
        ),
        description=(
            "One-command launch of the localhost-only recall viewer (issue "
            "athenaeum#1525). Run with no arguments inside a Claude Code "
            "session and it scopes itself to that session, binds a free port, "
            "and opens the page. Equivalent to `athenaeum viewer --session "
            "<this session>` with the ceremony removed."
        ),
    )
    demo_p.add_argument(
        "--session",
        default=None,
        help=(
            "Session id to scope to. Default: $CLAUDE_CODE_SESSION_ID, then "
            "$CLAUDE_SESSION_ID, then the newest Claude Code transcript."
        ),
    )
    demo_p.add_argument(
        "--path",
        type=Path,
        default=None,
        help=f"Knowledge directory (default: {DEFAULT_KNOWLEDGE_ROOT})",
    )
    demo_p.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory holding the push-metrics ledgers "
        "(default: ATHENAEUM_CACHE_DIR env or ~/.cache/athenaeum)",
    )
    demo_p.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Preferred TCP port on localhost (default: {DEFAULT_PORT}). "
        "Falls back to an OS-assigned free port if this one is busy.",
    )
    demo_p.add_argument(
        "--projects-root",
        type=Path,
        default=None,
        help=f"Claude Code transcript root (default: {DEFAULT_PROJECTS_ROOT})",
    )
    demo_p.add_argument(
        "--editor",
        default=None,
        help="Command used to open a clicked page (default: subl).",
    )
    demo_p.add_argument(
        "--no-browser",
        action="store_true",
        help="Serve without opening a browser (headless/CI).",
    )
    demo_p.set_defaults(func=cmd_demo)
