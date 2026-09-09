# SPDX-License-Identifier: Apache-2.0
"""``athenaeum viewer`` — minimal, read-only, localhost-only view of pushed
vs. pulled vs. used recall, for one session (issue athenaeum#1480).

``docs/north-star.md`` says a memory system is judged at its recall moments,
not its ingestion moments — but until this command existed, seeing a recall
moment meant reading ``_push_records.jsonl`` by hand. This command serves one
HTML page with three columns:

- **pushed unbidden**   — hook/sidecar-sourced push records (``source`` key
                           present): context the passive recall path injected
                           without being asked.
- **pulled deliberately** — push records with no ``source`` key: an explicit
                           MCP ``recall`` call.
- **overlap**           — ids appearing in both: the passive path having
                           independently surfaced something the session also
                           went and pulled for itself.

Per row: id, tier, scope, memory tier, estimated token cost, and whether
reference determination marked the id referenced (``yes`` / ``no`` /
``pending`` — see :func:`_referenced_flag`; AC6's "no reference
determination yet" case is a real third state here, never collapsed into
``no``).

**Consumes the contract, not a private back door (AC4).** This module never
imports :mod:`athenaeum.push_metrics` and never opens a ledger file. Every
byte of data it renders comes from running ``python -m athenaeum.cli
push-metrics tail --json`` (issue athenaeum#1479's documented NDJSON
contract) as a subprocess and parsing its stdout — exactly the surface any
external consumer would use. Being the contract's first consumer is what
keeps the contract honest: a private shortcut here would let a future
regression in the CLI contract ship invisibly, because the one built-in
consumer would keep working off the bypassed internals instead of noticing.
See ``tests/test_cmd_viewer.py::test_viewer_never_opens_ledger_file_directly``
for the test that enforces this mechanically (it patches ``open`` in THIS
process and proves the viewer still returns correct data — which is only
possible if the actual read happens inside the spawned subprocess).

**Localhost-only, read-only, no write path.** The HTTP server always binds
``127.0.0.1`` explicitly (never ``0.0.0.0`` or the empty-string wildcard) and
serves exactly two ``GET`` routes: the static page and its JSON data feed.
There is no route that accepts a body or mutates anything.

**Zero new dependencies.** stdlib ``http.server`` plus one static HTML file
(``athenaeum/viewer_static/index.html``, loaded via ``importlib.resources``)
containing vanilla JS. No framework, no template engine, no new
``pyproject.toml`` entry — see that module's docstring for why the wheel
already ships it.

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` —
mirrors :mod:`athenaeum._cmd_serve`'s shape.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib import resources
from pathlib import Path
from typing import Any

from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT

#: Default TCP port. Arbitrary but fixed, purely a convenience default —
#: `--port 0` (OS-assigned, read back via the bound socket) is what tests use
#: to avoid ever colliding with a real listener.
DEFAULT_PORT = 8756

#: ``PushRecord.source`` values that mean "pushed unbidden" (issue
#: athenaeum#1479's documented reader rule, reproduced here rather than
#: imported — this module deliberately never imports
#: :mod:`athenaeum.push_metrics`; see the module docstring's AC4 section).
#: A push record with NO ``source`` key at all is the third case: pulled
#: deliberately (an explicit MCP ``recall`` call).
_UNBIDDEN_SOURCES = ("hook", "sidecar")


class ViewerContractError(RuntimeError):
    """Raised when the ``push-metrics tail --json`` subprocess itself fails
    (nonzero exit or unparsable NDJSON) — surfaced as an HTTP 502, since the
    viewer has no independent way to answer without that contract."""


def _tail_argv(
    *, session_id: str | None, path: Path, cache_dir: Path | None
) -> list[str]:
    """Build the ``python -m athenaeum.cli push-metrics tail --json`` argv.

    Uses ``sys.executable -m athenaeum.cli`` rather than the ``athenaeum``
    console script so this works identically in an editable/test checkout
    that has not (re)installed the console-script entry point, and so the
    subprocess runs under the exact same interpreter (and therefore the same
    installed athenaeum) as the viewer itself.
    """
    argv = [
        sys.executable,
        "-m",
        "athenaeum.cli",
        "push-metrics",
        "tail",
        "--json",
        "--path",
        str(path),
    ]
    if cache_dir is not None:
        argv += ["--cache-dir", str(cache_dir)]
    if session_id:
        argv += ["--session", session_id]
    return argv


def _run_tail_contract(
    *, session_id: str | None, path: Path, cache_dir: Path | None
) -> list[dict[str, Any]]:
    """Invoke the documented NDJSON contract and parse its stdout.

    Read-only, single drain (no ``--follow``): one HTTP request maps to one
    subprocess invocation, so the page always reflects the ledgers as of the
    moment it was loaded/refreshed — good enough for a manual "reload to see
    what's new" viewer, and simpler than holding a long-lived streaming
    connection open per browser tab.
    """
    argv = _tail_argv(session_id=session_id, path=path, cache_dir=cache_dir)
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ViewerContractError(
            f"push-metrics tail exited {result.returncode}: {result.stderr.strip()}"
        )
    records: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ViewerContractError(
                f"push-metrics tail emitted a non-JSON line: {line!r}"
            ) from exc
        if isinstance(row, dict):
            records.append(row)
    return records


def _referenced_flag(
    item_id: str, *, has_reference_record: bool, referenced_ids: set[str]
) -> bool | None:
    """``True``/``False`` once a reference-determination record exists for
    this session; ``None`` when none has landed yet (AC6) — the caller must
    render that as a distinct "pending" state, never as an empty/misleading
    "0% referenced" figure."""
    if not has_reference_record:
        return None
    return item_id in referenced_ids


def _row(
    meta: dict[str, Any], *, has_reference_record: bool, referenced_ids: set[str]
) -> dict[str, Any]:
    return {
        "id": meta.get("id", ""),
        "tier": meta.get("tier", ""),
        "scope": meta.get("scope", ""),
        "memory_tier": meta.get("memory_tier", ""),
        "token_cost": meta.get("token_cost", 0),
        "referenced": _referenced_flag(
            meta.get("id", ""),
            has_reference_record=has_reference_record,
            referenced_ids=referenced_ids,
        ),
    }


def shape_viewer_payload(
    *, session_id: str | None, records: list[dict[str, Any]]
) -> dict[str, Any]:
    """Fold the shaped ``tail --json`` records into the three-column view.

    ``unbidden``/``deliberate`` are keyed by item id so a later push record
    for the same id (the ledgers are append-only) overwrites the earlier
    metadata with the freshest — records arrive newest-last per the tail
    contract, so a plain dict assignment in iteration order already does
    this correctly.
    """
    unbidden: dict[str, dict[str, Any]] = {}
    deliberate: dict[str, dict[str, Any]] = {}
    has_reference_record = False
    referenced_ids: set[str] = set()

    for rec in records:
        record_type = rec.get("record_type")
        if record_type == "push":
            bucket = unbidden if rec.get("source") in _UNBIDDEN_SOURCES else deliberate
            for item in rec.get("items", []):
                item_id = item.get("id") if isinstance(item, dict) else None
                if item_id:
                    bucket[item_id] = item
        elif record_type == "reference":
            has_reference_record = True
            referenced_ids.update(rec.get("referenced_ids") or [])

    overlap_ids = sorted(set(unbidden) & set(deliberate))

    def _rows(bucket: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            _row(
                bucket[item_id],
                has_reference_record=has_reference_record,
                referenced_ids=referenced_ids,
            )
            for item_id in sorted(bucket)
        ]

    overlap_rows = [
        _row(
            unbidden.get(item_id) or deliberate[item_id],
            has_reference_record=has_reference_record,
            referenced_ids=referenced_ids,
        )
        for item_id in overlap_ids
    ]

    return {
        "session_id": session_id or "",
        "has_reference_determination": has_reference_record,
        "pushed_unbidden": _rows(unbidden),
        "pulled_deliberately": _rows(deliberate),
        "overlap": overlap_rows,
    }


def build_viewer_data(
    *, session_id: str | None, path: Path, cache_dir: Path | None = None
) -> dict[str, Any]:
    """End-to-end: run the contract, shape the three-column payload."""
    records = _run_tail_contract(session_id=session_id, path=path, cache_dir=cache_dir)
    return shape_viewer_payload(session_id=session_id, records=records)


def _load_static_html() -> bytes:
    """Read the packaged ``index.html`` via ``importlib.resources`` — never a
    hardcoded filesystem ``Path`` literal, so this works the same whether
    athenaeum is running from a source checkout or an installed wheel."""
    resource = resources.files("athenaeum.viewer_static").joinpath("index.html")
    return resource.read_bytes()


class _ViewerRequestHandler(BaseHTTPRequestHandler):
    """Two read-only ``GET`` routes: the static page and its JSON data feed.

    Per-server configuration (``session_id``/``knowledge_path``/``cache_dir``)
    is injected via subclassing in :func:`_make_handler_class` rather than
    constructor arguments, because :class:`http.server.HTTPServer` always
    instantiates its handler class with a fixed ``(request, client_address,
    server)`` signature.
    """

    session_id: str | None = None
    knowledge_path: Path = DEFAULT_KNOWLEDGE_ROOT
    cache_dir: Path | None = None

    server_version = "athenaeum-viewer/1"

    def log_message(self, format: str, *args: Any) -> None:  # stdlib-mandated signature
        # Quiet by default -- a local read-only viewer has no operational
        # need to spam access logs to stderr on every browser request.
        pass

    def do_GET(self) -> None:  # stdlib-mandated method name
        if self.path in ("/", "/index.html"):
            self._serve_html()
        elif self.path in ("/data.json", "/api/data.json"):
            self._serve_data()
        else:
            self.send_error(404, "not found")

    def _serve_html(self) -> None:
        body = _load_static_html()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_data(self) -> None:
        try:
            payload = build_viewer_data(
                session_id=self.session_id,
                path=self.knowledge_path,
                cache_dir=self.cache_dir,
            )
        except ViewerContractError as exc:
            body = json.dumps({"error": str(exc)}).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _make_handler_class(
    *, session_id: str | None, path: Path, cache_dir: Path | None
) -> type[_ViewerRequestHandler]:
    """Bind per-server config onto a fresh handler subclass (see
    :class:`_ViewerRequestHandler`'s docstring for why)."""

    class _BoundHandler(_ViewerRequestHandler):
        pass

    _BoundHandler.session_id = session_id
    _BoundHandler.knowledge_path = path
    _BoundHandler.cache_dir = cache_dir
    return _BoundHandler


def make_server(
    *,
    session_id: str | None,
    path: Path,
    cache_dir: Path | None = None,
    port: int = DEFAULT_PORT,
) -> HTTPServer:
    """Build (but do not start) the localhost-only viewer server.

    Always binds ``127.0.0.1`` explicitly -- never ``0.0.0.0`` or ``""`` --
    so the AC's "localhost-only" bind is a fact about the bound address, not
    merely a claim in the help text. Pass ``port=0`` to let the OS assign a
    free port; read it back via ``server.server_address[1]``.
    """
    handler_cls = _make_handler_class(session_id=session_id, path=path, cache_dir=cache_dir)
    return HTTPServer(("127.0.0.1", port), handler_cls)


def cmd_viewer(args: argparse.Namespace) -> int:
    """``athenaeum viewer`` -- serve until interrupted (Ctrl-C)."""
    path = (args.path or DEFAULT_KNOWLEDGE_ROOT).expanduser().resolve()
    server = make_server(
        session_id=args.session,
        path=path,
        cache_dir=args.cache_dir,
        port=args.port,
    )
    host, port = str(server.server_address[0]), server.server_address[1]
    print(f"athenaeum viewer listening on http://{host}:{port}/ (Ctrl-C to stop)")
    if not args.session:
        print(
            "warning: no --session given -- this view includes every session "
            "in the ledger, including this viewer's own future recall "
            "activity if it triggers any",
            file=sys.stderr,
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def add_viewer_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum viewer`` and its flags on *subparsers*."""
    viewer_p = subparsers.add_parser(
        "viewer",
        help=(
            "Serve a localhost-only, read-only page showing pushed-unbidden "
            "vs. pulled-deliberately vs. overlap recall for one session "
            "(issue athenaeum#1480)."
        ),
    )
    viewer_p.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    viewer_p.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory holding the push-metrics ledgers "
        "(default: ATHENAEUM_CACHE_DIR env or ~/.cache/athenaeum)",
    )
    viewer_p.add_argument(
        "--session",
        type=str,
        default=None,
        help="Scope the view to one consuming session id. Strongly "
        "recommended: without it, the view includes every session in the "
        "ledger, and if the viewer's own process ever triggers a recall "
        "call its own activity would appear mixed in.",
    )
    viewer_p.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"TCP port to bind on localhost (default: {DEFAULT_PORT}). "
        "Pass 0 to let the OS assign a free port.",
    )
    viewer_p.set_defaults(func=cmd_viewer)
