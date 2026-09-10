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

- **``--list-sessions`` finds a session worth demoing (issue athenaeum#1531).**
  ``athenaeum demo`` self-scopes to the session it is launched from, which is
  fine for the common case but wrong for two real ones: showing a session with
  actual traffic (the one you are sitting in may have a handful of rows; the
  operator needs to know another has hundreds), and showing the ``referenced``
  column at all (it only populates once reference determination runs at
  session END, so demonstrating it needs an already-finished session).
  ``--list-sessions`` prints every session with recall activity, newest
  activity first, with row counts and whether each has ended, then exits 0
  without binding a port or starting a server — the picking stays a manual
  ``--session <id>`` paste; see the module docstring's "Out of scope" note in
  the issue for why interactive selection is deliberately not built.

  The project column comes from the transcript's own ``cwd`` field — NEVER
  from un-mangling the ``~/.claude/projects/<mangled-path>/`` directory name.
  That un-mangling is lossy: a real directory name can itself contain dashes
  (``~/local-deploys/hestia`` mangles to
  ``-Users-x-local-deploys-hestia``, which a naive dash-to-slash reversal
  turns into the wrong path, ``~/local/deploys/hestia``). See
  :func:`_transcript_cwd`.

This module consumes :mod:`athenaeum._cmd_viewer`'s public helpers
(:func:`~athenaeum._cmd_viewer.make_server`,
:func:`~athenaeum._cmd_viewer.build_viewer_data`) and its
``push-metrics tail --json`` subprocess runner
(:func:`~athenaeum._cmd_viewer._run_tail_contract`) and, through them, the
documented NDJSON contract (issue athenaeum#1479). It never opens a ledger
file and never imports :mod:`athenaeum.push_metrics` — the same
contract-boundary rule the viewer holds itself to. (``--list-sessions`` does
open ONE file per session directly: the Claude Code transcript itself, to read
its ``cwd`` field — that is not a ledger and is outside the push-metrics
contract entirely.)

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in its
own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from datetime import datetime, timezone
from http.server import HTTPServer
from pathlib import Path
from typing import Any

from athenaeum._cmd_viewer import (
    DEFAULT_EDITOR_COMMAND,
    DEFAULT_PORT,
    ViewerContractError,
    _run_tail_contract,
    build_viewer_data,
    make_server,
    resolve_editor_command,
    warn_if_editor_missing,
)
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT

#: Default number of sessions ``--list-sessions`` prints (AC2). A sensible
#: bound rather than the whole ledger — an operator picking a demo session
#: cares about recent activity, not every session ever recorded.
DEFAULT_LIST_SESSIONS_LIMIT = 20

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


# ---------------------------------------------------------------------------
# --list-sessions (issue athenaeum#1531)
# ---------------------------------------------------------------------------


def _parse_tail_ts(ts: object) -> datetime | None:
    """Parse one tail record's ``ts`` into a timezone-AWARE UTC datetime.

    The live ledger holds a mix of shapes right now: second-precision
    ``Z``-suffixed (``2026-09-09T22:21:04Z``), microsecond-precision
    ``Z``-suffixed (``2026-08-27T17:36:16.292160Z``), and older records with
    no ``Z`` at all (naive). ``datetime.fromisoformat`` alone yields a mix of
    aware and naive datetimes across those, and comparing an aware value to a
    naive one raises ``TypeError`` — which would crash exactly the "order by
    last activity" sort this feeds (AC2), and only on a ledger old enough to
    hold both shapes. Every value returned here is aware and normalized to
    UTC so sorting is always safe. Returns ``None`` for a missing/unparsable
    timestamp — the caller sorts those first (oldest), never dropping the
    record.
    """
    if not isinstance(ts, str) or not ts.strip():
        return None
    text = ts.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _find_transcript(session_id: str, projects_root: Path) -> Path | None:
    """Locate ``<projects_root>/*/<session_id>.jsonl``, or ``None``.

    A session's scope directory name is not known up front (that is exactly
    the un-mangling trap this feature exists to avoid — see module
    docstring), so this globs for the session id's own filename rather than
    trying to derive the directory. The session id is a UUID minted by
    Claude Code, so more than one match is not expected; the first is used.
    """
    try:
        candidates = sorted(projects_root.glob(f"*/{session_id}.jsonl"))
    except OSError:
        return None
    return candidates[0] if candidates else None


def _transcript_cwd(transcript_path: Path) -> str | None:
    """Best-effort ``cwd`` for a transcript, or ``None`` if never found.

    THE load-bearing rule (issue athenaeum#1531 AC4): this reads the
    transcript's own ``cwd`` field. It never derives a project path by
    un-mangling the ``~/.claude/projects/<mangled-path>/`` directory name —
    that reversal is lossy, because a real directory name can itself contain
    dashes (``~/local-deploys/hestia`` mangles to
    ``-Users-x-local-deploys-hestia``; naive dash-to-slash reversal produces
    the wrong path, ``~/local/deploys/hestia``).

    ``cwd`` is NOT on the first few header records (which carry only
    ``sessionId``/``type``/``mode``) — this scans line by line until it finds
    one, then stops; it does not read the whole file into memory first, and
    it does not give up after the first line. Malformed lines are skipped
    rather than aborting the scan.
    """
    try:
        with transcript_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                cwd = record.get("cwd")
                if isinstance(cwd, str) and cwd.strip():
                    return cwd.strip()
    except OSError:
        return None
    return None


def _shorten_home(path_str: str) -> str:
    """Render an absolute path with the operator's home directory as ``~``.

    Purely cosmetic (matches the issue's own example table); a path outside
    the home directory is returned unchanged.
    """
    home = str(Path.home())
    if home and (path_str == home or path_str.startswith(home + os.sep)):
        return "~" + path_str[len(home) :]
    return path_str


#: Visible placeholder for a session whose transcript cannot be found at all
#: (AC5) — never a blank/empty project column, which would be indistinguishable
#: from a project genuinely named the empty string.
_NO_TRANSCRIPT_PLACEHOLDER = "(no transcript found)"

#: Visible placeholder for a session whose transcript WAS found but carries no
#: ``cwd`` on any record (e.g. an unusually short or truncated transcript).
#: Kept distinct from :data:`_NO_TRANSCRIPT_PLACEHOLDER` so the two failure
#: modes are not conflated in the output.
_NO_CWD_PLACEHOLDER = "(cwd not found in transcript)"


def _project_label(session_id: str, projects_root: Path) -> str:
    """Resolve the project column for one session id (AC4, AC5)."""
    transcript = _find_transcript(session_id, projects_root)
    if transcript is None:
        return _NO_TRANSCRIPT_PLACEHOLDER
    cwd = _transcript_cwd(transcript)
    if cwd is None:
        return _NO_CWD_PLACEHOLDER
    return _shorten_home(cwd)


def _aggregate_sessions(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Group shaped tail records (push + reference) by ``session_id``.

    Returns ``{session_id: {"rows": int, "last_ts": datetime | None, "ended":
    bool}}``. ``rows`` sums ``pushed_count`` across that session's PUSH
    records only ("injected items across the session", per the issue) —
    reference records carry no comparable count and are excluded from it.
    ``ended`` is ``True`` iff at least one reference-determination record
    exists for the session (AC3) — the whole point of this column, and the
    precondition for the viewer's ``referenced`` column.
    """
    sessions: dict[str, dict[str, Any]] = {}
    for rec in records:
        session_id = rec.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            continue
        entry = sessions.setdefault(session_id, {"rows": 0, "last_ts": None, "ended": False})
        ts = _parse_tail_ts(rec.get("ts"))
        if ts is not None and (entry["last_ts"] is None or ts > entry["last_ts"]):
            entry["last_ts"] = ts
        if rec.get("record_type") == "push":
            pushed_count = rec.get("pushed_count")
            if isinstance(pushed_count, int):
                entry["rows"] += pushed_count
        elif rec.get("record_type") == "reference":
            entry["ended"] = True
    return sessions


def _positive_limit(value: str) -> int:
    """argparse ``type=`` for ``--limit``: a POSITIVE integer, or a clear
    error (issue athenaeum#1531 review finding).

    Two silent-failure modes this closes, neither of which argparse's plain
    ``type=int`` catches on its own:

    - ``0`` is falsy in Python, so a caller reading it back with
      ``value or DEFAULT`` silently substitutes the default -- an explicit
      request for zero rows would render as the full default list instead,
      with no error. This module deliberately does not read ``--limit`` that
      way (see :func:`cmd_list_sessions`) precisely so this type function is
      the ONE place a bad value gets caught.
    - A negative value passes ``int()`` fine but reaches ``list[:N]``
      slicing downstream, where ``[:-5]`` silently drops the 5 MOST RECENT
      entries rather than erroring -- the opposite of what an operator
      asking for a short list wants, and worse than doing nothing.

    Neither is defined as meaningful by issue athenaeum#1531's AC2 ("a
    --limit (sensible default)"), so both are refused outright rather than
    guessed at (e.g. treating 0 as "unlimited") -- an explicit value is
    never silently replaced by a different one.
    """
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be an integer, got {value!r}") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {parsed}")
    return parsed


def cmd_list_sessions(args: argparse.Namespace) -> int:
    """``athenaeum demo --list-sessions`` (issue athenaeum#1531).

    Prints every session with recall activity, newest last-activity first,
    then exits 0 WITHOUT binding a port or starting a server (AC1). Sourced
    exclusively from ``push-metrics tail --json`` (AC7) via
    :func:`athenaeum._cmd_viewer._run_tail_contract` — the same subprocess
    contract runner the viewer itself uses; no ledger file is ever opened
    in-process.
    """
    path = (args.path or DEFAULT_KNOWLEDGE_ROOT).expanduser().resolve()
    projects_root = (args.projects_root or DEFAULT_PROJECTS_ROOT).expanduser()

    # `getattr(..., None) or DEFAULT` looks equivalent but is not: `0` is
    # falsy, so that idiom would silently REPLACE an explicit `--limit 0`
    # with the default -- the exact silent-substitution this command's own
    # design principle (see _report_rows) exists to avoid elsewhere. Only a
    # genuinely ABSENT limit (attribute missing, or None -- the shape a
    # caller that skips argparse, e.g. a test, is expected to pass) falls
    # through to the default; any supplied value, including 0 or negative,
    # is validated explicitly instead of being coerced.
    #
    # The contract (issue athenaeum#1531 review finding): `--limit` must be a
    # POSITIVE integer. Zero is not defined as "unlimited" -- it is refused,
    # same as a negative value -- because a negative limit silently drops the
    # N MOST RECENT sessions via Python's `list[:-N]` slicing, which is the
    # opposite of what an operator asking for a short list wants, with no
    # error at all. :func:`_positive_limit` already enforces this at argparse
    # parse time for the normal CLI path; this is the same check applied
    # again for a caller that builds its own ``Namespace`` and skips argparse
    # (e.g. a unit test), so the contract holds either way.
    limit = getattr(args, "limit", None)
    if limit is None:
        limit = DEFAULT_LIST_SESSIONS_LIMIT
    elif limit <= 0:
        print(
            f"error: --limit must be a positive integer, got {limit}",
            file=sys.stderr,
        )
        return 1

    try:
        records = _run_tail_contract(session_id=None, path=path, cache_dir=args.cache_dir)
    except ViewerContractError as exc:
        # Distinct from the empty-ledger case below (AC6): a probe FAILURE
        # says nothing about whether sessions exist, so it must not render
        # as "no sessions with recall activity yet" — the same None-vs-zero
        # discipline _report_rows already applies to a single-session probe.
        print(f"error: could not read push-metrics ledgers: {exc}", file=sys.stderr)
        return 1

    if not records:
        print("no sessions with recall activity yet", file=sys.stderr)
        return 0

    sessions = _aggregate_sessions(records)
    ordered = sorted(
        sessions.items(),
        key=lambda kv: kv[1]["last_ts"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )[:limit]

    header = f"{'session id':<38}{'rows':>6}  {'last activity':<22}{'ended':>6}  project"
    print(header)
    for session_id, info in ordered:
        last_ts = info["last_ts"]
        last_str = last_ts.strftime("%Y-%m-%dT%H:%M:%SZ") if last_ts is not None else "unknown"
        ended_str = "yes" if info["ended"] else "no"
        project = _project_label(session_id, projects_root)
        print(
            f"{session_id:<38}{info['rows']:>6}  {last_str:<22}{ended_str:>6}  {project}"
        )
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """``athenaeum demo`` — resolve, bind, announce, open, serve."""
    if getattr(args, "list_sessions", False):
        return cmd_list_sessions(args)

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
    demo_p.add_argument(
        "--list-sessions",
        action="store_true",
        help="Print sessions with recall activity (newest last-activity "
        "first), including row counts and whether each has ended, then exit "
        "0 without binding a port or starting a server (issue athenaeum#1531). "
        "Use this to find a session worth demoing, then pass its id via "
        "--session.",
    )
    demo_p.add_argument(
        "--limit",
        type=_positive_limit,
        default=DEFAULT_LIST_SESSIONS_LIMIT,
        help="With --list-sessions, the maximum number of sessions to print. "
        "Must be a positive integer -- 0 and negative values are rejected "
        "(a negative value would silently drop the N MOST RECENT sessions "
        "via list slicing, the opposite of a short list) "
        f"(default: {DEFAULT_LIST_SESSIONS_LIMIT}). Has no effect otherwise.",
    )
    demo_p.set_defaults(func=cmd_demo)
