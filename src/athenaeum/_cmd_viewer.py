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

**Localhost-only, and read-only with respect to the corpus and the ledgers.**
The HTTP server always binds ``127.0.0.1`` explicitly (never ``0.0.0.0`` or the
empty-string wildcard). Two ``GET`` routes serve the static page and its JSON
data feed.

Since issue athenaeum#1528 there is also ONE ``POST`` route, ``/open``, which
launches the operator's editor on a page they clicked. This module used to
promise "no route that accepts a body or mutates anything"; that sentence is
no longer true, and is corrected here rather than left standing. Nothing is
written to the corpus or the ledgers — the side effect is launching a local
process — but launching a process is emphatically not nothing, so it is
fenced:

- **a nonce**, minted per server start and embedded in the served page. Any
  web page in the browser can send a request to ``127.0.0.1``; the same-origin
  policy stops it *reading* our page, so it cannot learn the nonce.
- **an ``Origin`` check** when the header is present, rejecting anything that
  is not this server.
- **path confinement** via :func:`athenaeum.viewer_corpus.resolve_path`, which
  resolves symlinks before testing containment in the knowledge root.
- **argv invocation**, never a shell string, so a filename cannot become
  syntax.

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
import hmac
import json
import secrets
import shlex
import shutil
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib import resources
from pathlib import Path
from typing import Any

from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT
from athenaeum.viewer_corpus import build_uid_index, load_page_info, resolve_path

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

#: Editor the ``/open`` route launches. Sublime Text's CLI by default because
#: that is what this deployment uses; ``--editor`` overrides it, and the value
#: is split into an argv list so it never reaches a shell.
DEFAULT_EDITOR_COMMAND: tuple[str, ...] = ("subl",)

#: Token in the static page that :meth:`_ViewerRequestHandler._serve_html`
#: swaps for the live nonce.
#:
#: The name deliberately avoids the project's own env-var prefix.
#: ``scripts/check_env_docs.py`` scans ``src/`` for tokens carrying that prefix
#: and treats each as an environment variable requiring documentation, so
#: naming this placeholder after the project made it surface in the generated
#: configuration reference as though it were a config knob. Caught by CI
#: (``tests/test_env_docs.py``); worth a comment because the obvious name is
#: the wrong one, and because writing the bad spelling out even inside a
#: comment is enough to trip the same scanner.
NONCE_PLACEHOLDER = b"__VIEWER_NONCE_PLACEHOLDER__"


def _allowed_origins(server_address: Any) -> frozenset[str]:
    """Origins the ``/open`` route accepts.

    Both spellings of loopback, because a browser sends whichever the user
    typed and treats them as distinct origins.
    """
    try:
        port = int(server_address[1])
    except (TypeError, ValueError, IndexError):
        return frozenset()
    return frozenset(
        {
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
        }
    )


class ViewerContractError(RuntimeError):
    """Raised when the ``push-metrics tail --json`` subprocess itself fails
    (nonzero exit or unparsable NDJSON) — surfaced as an HTTP 502, since the
    viewer has no independent way to answer without that contract."""


def _tail_argv(*, session_id: str | None, path: Path, cache_dir: Path | None) -> list[str]:
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


#: The four ways a page can appear in a session. Values double as the CSS class
#: the page uses, so a rename is one edit rather than two that can drift.
CLASSIFICATION_PUSHED = "pushed"
CLASSIFICATION_PUSHED_RECALLED = "pushed-recalled"
CLASSIFICATION_BREADCRUMB = "breadcrumb"
CLASSIFICATION_PULLED_COLD = "pulled-cold"


def classify(
    item_id: str,
    *,
    pushed_ids: set[str],
    pulled_ids: set[str],
    breadcrumb_ids: set[str],
) -> str:
    """Which of the four states one page is in for this session.

    The order of these tests IS the definition, not an implementation detail:

    1. pushed AND pulled - the sidecar surfaced it and the session went and
       read it. The one unambiguously good outcome.
    2. pushed only - offered, not taken up. Neutral rather than a failure: a
       cheap offer that goes unused is the system working as designed.
    3. pulled, not pushed, but related to something pushed - the push started a
       thread the session followed. Credited to the sidecar.
    4. pulled, not pushed, unrelated - the session found it alone. The miss,
       and the only state that should alarm anyone.

    A page can be both pushed and related-to-something-pushed; (1)/(2) win,
    because having been pushed outright is the stronger statement about it.

    "Pulled" here means an explicit recall landed on the page - a push record
    with no ``source`` key, available live. It is NOT the session-end reference
    determination, which cannot populate mid-session. The page's legend has to
    say so, or light green reads as a claim about usefulness that this data
    does not support.
    """
    was_pushed = item_id in pushed_ids
    was_pulled = item_id in pulled_ids
    if was_pushed and was_pulled:
        return CLASSIFICATION_PUSHED_RECALLED
    if was_pushed:
        return CLASSIFICATION_PUSHED
    if item_id in breadcrumb_ids:
        return CLASSIFICATION_BREADCRUMB
    return CLASSIFICATION_PULLED_COLD


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
    last_turn_record = next(
        (
            rec
            for rec in reversed(records)
            if rec.get("record_type") == "push" and rec.get("source") in _UNBIDDEN_SOURCES
        ),
        None,
    )

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
        # Retained verbatim (issue athenaeum#1528 AC7): `athenaeum demo`'s row
        # probe counts these three, and any external reader of the payload
        # predates the unified list below.
        "pushed_unbidden": _rows(unbidden),
        "pulled_deliberately": _rows(deliberate),
        "overlap": overlap_rows,
        "pushed_ids": sorted(unbidden),
        "pulled_ids": sorted(deliberate),
        "all_items": {**deliberate, **unbidden},
        "last_turn_record": last_turn_record,
    }


def enrich_payload(payload: dict[str, Any], *, wiki_root: Path) -> dict[str, Any]:
    """Join the shaped payload against the local corpus (issue athenaeum#1528).

    Adds the unified ``pages`` list and the ``last_turn`` panel. Kept separate
    from :func:`shape_viewer_payload` so the ledger-shaping logic stays a pure
    function of the records and can be tested without a corpus on disk.
    """
    index = build_uid_index(wiki_root)
    all_items: dict[str, dict[str, Any]] = payload.pop("all_items", {})
    pushed_ids = set(payload.pop("pushed_ids", []))
    pulled_ids = set(payload.pop("pulled_ids", []))
    last_turn_record = payload.pop("last_turn_record", None)

    info = {uid: load_page_info(uid, index) for uid in all_items}

    # One hop, same session: a pulled page counts as breadcrumbed only if some
    # page PUSHED in this session names it in `related:`. Transitive closure
    # over a 25k-page corpus would make nearly everything a breadcrumb and the
    # colour would stop carrying information.
    breadcrumb_ids: set[str] = set()
    for uid in pushed_ids:
        page = info.get(uid)
        if page is not None:
            breadcrumb_ids.update(page.related)
    breadcrumb_ids -= pushed_ids

    def _page_row(uid: str) -> dict[str, Any]:
        row = dict(all_items[uid])
        page = info[uid]
        row.update(page.to_dict())
        row["id"] = uid
        row["classification"] = classify(
            uid,
            pushed_ids=pushed_ids,
            pulled_ids=pulled_ids,
            breadcrumb_ids=breadcrumb_ids,
        )
        row["referenced"] = _referenced_flag(
            uid,
            has_reference_record=bool(payload.get("has_reference_determination")),
            referenced_ids=set(),
        )
        return row

    # Sorted by classification (most interesting first), then by display name so
    # the list is stable across reloads rather than reshuffling under the eye.
    order = {
        CLASSIFICATION_PULLED_COLD: 0,
        CLASSIFICATION_PUSHED_RECALLED: 1,
        CLASSIFICATION_BREADCRUMB: 2,
        CLASSIFICATION_PUSHED: 3,
    }
    pages = sorted(
        (_page_row(uid) for uid in all_items),
        key=lambda r: (order.get(r["classification"], 9), (r.get("name") or "").lower(), r["id"]),
    )

    last_turn: dict[str, Any] = {"present": False}
    if last_turn_record is not None:
        turn_items = []
        for item in last_turn_record.get("items") or []:
            uid = item.get("id") if isinstance(item, dict) else None
            if not uid:
                continue
            row = dict(item)
            page = info.get(uid) or load_page_info(uid, index)
            row.update(page.to_dict())
            row["classification"] = classify(
                uid,
                pushed_ids=pushed_ids,
                pulled_ids=pulled_ids,
                breadcrumb_ids=breadcrumb_ids,
            )
            turn_items.append(row)
        last_turn = {
            "present": True,
            "ts": last_turn_record.get("ts", ""),
            "backend": last_turn_record.get("backend", ""),
            "query_hash": last_turn_record.get("query_hash", ""),
            "token_cost": last_turn_record.get("token_cost", 0),
            "items": turn_items,
            # Push records carry only a query HASH (athenaeum#711 - the raw
            # query text is deliberately never written), so the topics the
            # sidecar extracted are NOT recoverable from the ledger. Rendered
            # as an explicit not-instrumented state rather than an empty box,
            # which would read as "the sidecar thought nothing".
            "topics": None,
            "topics_status": "not_instrumented",
        }

    payload["pages"] = pages
    payload["last_turn"] = last_turn
    return payload


def build_viewer_data(
    *, session_id: str | None, path: Path, cache_dir: Path | None = None
) -> dict[str, Any]:
    """End-to-end: run the contract, shape the payload, join it to the corpus."""
    records = _run_tail_contract(session_id=session_id, path=path, cache_dir=cache_dir)
    payload = shape_viewer_payload(session_id=session_id, records=records)
    return enrich_payload(payload, wiki_root=Path(path) / "wiki")


def _load_static_html() -> bytes:
    """Read the packaged ``index.html`` via ``importlib.resources`` — never a
    hardcoded filesystem ``Path`` literal, so this works the same whether
    athenaeum is running from a source checkout or an installed wheel."""
    resource = resources.files("athenaeum.viewer_static").joinpath("index.html")
    return resource.read_bytes()


class _ViewerRequestHandler(BaseHTTPRequestHandler):
    """Two read-only ``GET`` routes plus the guarded ``POST /open``.

    Per-server configuration (``session_id``/``knowledge_path``/``cache_dir``)
    is injected via subclassing in :func:`_make_handler_class` rather than
    constructor arguments, because :class:`http.server.HTTPServer` always
    instantiates its handler class with a fixed ``(request, client_address,
    server)`` signature.
    """

    session_id: str | None = None
    knowledge_path: Path = DEFAULT_KNOWLEDGE_ROOT
    cache_dir: Path | None = None
    nonce: str = ""
    editor_command: tuple[str, ...] = DEFAULT_EDITOR_COMMAND

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

    def do_POST(self) -> None:  # stdlib-mandated method name
        if self.path in ("/open", "/api/open"):
            self._serve_open()
        else:
            self.send_error(404, "not found")

    def _reject(self, code: int, reason: str) -> None:
        body = json.dumps({"ok": False, "error": reason}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_open(self) -> None:
        """Launch the editor on one clicked page. Every refusal is tested."""
        origin = self.headers.get("Origin")
        if origin and origin not in _allowed_origins(self.server.server_address):
            self._reject(403, "cross-origin request refused")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._reject(400, "bad content length")
            return
        if length <= 0 or length > 4096:
            self._reject(400, "bad content length")
            return
        try:
            request = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._reject(400, "malformed JSON body")
            return
        if not isinstance(request, dict):
            self._reject(400, "malformed JSON body")
            return
        # compare_digest, not ==: a plain comparison leaks the nonce's length
        # and shared prefix through timing, and this route launches a process.
        supplied = request.get("nonce")
        if not isinstance(supplied, str) or not hmac.compare_digest(supplied, self.nonce):
            self._reject(403, "bad or missing nonce")
            return
        uid = request.get("uid")
        if not isinstance(uid, str):
            self._reject(400, "uid must be a string")
            return
        target = resolve_path(uid, wiki_root=Path(self.knowledge_path) / "wiki")
        if target is None:
            self._reject(404, "no page for that id inside the knowledge root")
            return
        try:
            subprocess.Popen(
                [*self.editor_command, str(target)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, ValueError) as exc:
            self._reject(500, f"could not launch editor: {exc}")
            return
        body = json.dumps({"ok": True, "path": str(target)}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_html(self) -> None:
        body = _load_static_html().replace(NONCE_PLACEHOLDER, self.nonce.encode("utf-8"))
        self.send_response(200)
        # The page holds the nonce; a cache would outlive the server that
        # minted it and hand a stale one to the next run.
        self.send_header("Cache-Control", "no-store")
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


def resolve_editor_command(value: str | None) -> tuple[str, ...]:
    """Split an ``--editor`` string into an argv tuple.

    ``shlex`` rather than ``str.split`` so a quoted path containing spaces
    survives, and a tuple rather than a string so the value can never be
    handed to a shell downstream.
    """
    if not value:
        return DEFAULT_EDITOR_COMMAND
    parts = tuple(shlex.split(value))
    return parts or DEFAULT_EDITOR_COMMAND


def warn_if_editor_missing(editor_command: tuple[str, ...]) -> bool:
    """Warn at STARTUP when the editor is not on PATH; return whether it is.

    Checked up front rather than on first click, because the failure it
    prevents is silent: the operator clicks a row mid-demo, nothing opens, and
    nothing on screen distinguishes "editor not installed" from "the click
    handler is broken".
    """
    if not editor_command:
        return False
    if shutil.which(editor_command[0]) is not None:
        return True
    print(
        f"warning: editor {editor_command[0]!r} is not on PATH -- clicking a page "
        "will report an error instead of opening it. Pass --editor to name a "
        "different one.",
        file=sys.stderr,
    )
    return False


def _make_handler_class(
    *,
    session_id: str | None,
    path: Path,
    cache_dir: Path | None,
    nonce: str = "",
    editor_command: tuple[str, ...] = DEFAULT_EDITOR_COMMAND,
) -> type[_ViewerRequestHandler]:
    """Bind per-server config onto a fresh handler subclass (see
    :class:`_ViewerRequestHandler`'s docstring for why)."""

    class _BoundHandler(_ViewerRequestHandler):
        pass

    _BoundHandler.session_id = session_id
    _BoundHandler.knowledge_path = path
    _BoundHandler.cache_dir = cache_dir
    _BoundHandler.nonce = nonce
    _BoundHandler.editor_command = editor_command
    return _BoundHandler


def make_server(
    *,
    session_id: str | None,
    path: Path,
    cache_dir: Path | None = None,
    port: int = DEFAULT_PORT,
    nonce: str | None = None,
    editor_command: tuple[str, ...] = DEFAULT_EDITOR_COMMAND,
) -> HTTPServer:
    """Build (but do not start) the localhost-only viewer server.

    Always binds ``127.0.0.1`` explicitly -- never ``0.0.0.0`` or ``""`` --
    so the AC's "localhost-only" bind is a fact about the bound address, not
    merely a claim in the help text. Pass ``port=0`` to let the OS assign a
    free port; read it back via ``server.server_address[1]``.

    *nonce* defaults to a fresh 256-bit token per server, which is what makes
    the ``/open`` route safe to expose: it is embedded in the served page, and
    the same-origin policy prevents any other site from reading it back out.
    """
    handler_cls = _make_handler_class(
        session_id=session_id,
        path=path,
        cache_dir=cache_dir,
        nonce=secrets.token_urlsafe(32) if nonce is None else nonce,
        editor_command=editor_command,
    )
    return HTTPServer(("127.0.0.1", port), handler_cls)


def cmd_viewer(args: argparse.Namespace) -> int:
    """``athenaeum viewer`` -- serve until interrupted (Ctrl-C)."""
    path = (args.path or DEFAULT_KNOWLEDGE_ROOT).expanduser().resolve()
    editor = resolve_editor_command(getattr(args, "editor", None))
    server = make_server(
        session_id=args.session,
        path=path,
        cache_dir=args.cache_dir,
        port=args.port,
        editor_command=editor,
    )
    warn_if_editor_missing(editor)
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
    viewer_p.add_argument(
        "--editor",
        default=None,
        help="Command used to open a clicked page (default: subl). Split with "
        "shell-like quoting and executed as an argv list, never via a shell.",
    )
    viewer_p.set_defaults(func=cmd_viewer)
