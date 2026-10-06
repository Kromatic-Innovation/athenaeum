# SPDX-License-Identifier: Apache-2.0
"""Per-call MCP bridge for the ``claude -p`` tool-passthrough path (issue athenaeum#1951).

**Layering: L2.** Module scope imports stdlib only (``socket``, ``threading``,
``json``, ``sys``, ``argparse``, ``tempfile`` is not needed here) — the
``mcp`` SDK is imported lazily, inside :func:`main`, the CHILD process entry
point only. This keeps the module importable (and its :class:`ToolBridgeHost`
usable from :mod:`athenaeum.provider`, an L3 service that may import an L2
module) in a process that has never installed the optional ``mcp`` package,
and keeps the layer boundary/acyclic-import tests (`tests/test_layer_boundary.py`,
`tests/test_import_graph_acyclic.py`) satisfied without a special case.

Two halves, run in two different processes:

- **Host** (:class:`ToolBridgeHost`): a context manager used by
  ``ClaudeCliClient.run_tool_loop`` (``provider.py``) in the MAIN process.
  Binds a private Unix socket, serves it on a daemon thread, and forwards
  each request to the harness's own in-process ``tool_executor`` callable.
  Pure stdlib — no MCP protocol knowledge at all. The wire format is one
  JSON line in (``{"tool": <name>, "input": <dict>}``), one JSON line out
  (``{"text": <str>}``).
- **Child** (:func:`main`, run as ``python -m athenaeum.cli_tool_bridge``):
  a real ``mcp.server.lowlevel.Server`` speaking MCP over stdio to the
  ``claude -p`` subprocess that spawned it (per its generated
  ``--mcp-config``). ``tools/list`` serves the tool specs verbatim;
  ``tools/call`` opens a fresh connection to the Host's socket for every
  call and relays the result back as a single text content block.

  Handlers are registered through the ``mcp`` 2.x ``on_list_tools`` /
  ``on_call_tool`` **constructor callbacks** (athenaeum#1954). The pre-2.0
  SDK registered them as ``@server.list_tools()`` / ``@server.call_tool()``
  decorators; ``mcp`` 2.x removed those methods from ``Server`` entirely, so
  that spelling raises ``AttributeError`` at bridge start-up rather than
  failing a protocol round trip. The 2.x callbacks also differ in shape: each
  receives ``(ServerRequestContext, params)`` and returns a full
  ``types.ListToolsResult`` / ``types.CallToolResult`` rather than a bare
  list. Neither difference changes what ``claude -p`` can act on: 2.x's
  result models serialize some additive fields 1.x never sent
  (``ttlMs``/``cacheScope``/``resultType``), which a client that does not
  know them ignores -- it is not a protocol change the harness has to
  follow.

  One behaviour 2.x does NOT carry over for free, and which is restored
  explicitly below: the pre-2.0 ``@server.call_tool()`` decorator defaulted
  to ``validate_input=True`` and ran ``jsonschema.validate`` against the
  tool's served ``inputSchema`` BEFORE dispatching, turning a
  non-conforming call into an ``is_error`` result without ever contacting
  the Host. 2.x's ``on_call_tool`` has no equivalent on the stdio transport
  (``Server``'s ``get_tool_input_schema`` hook is read only by
  ``Mcp-Param-*`` header validation on the Streamable-HTTP path), so
  :func:`main` performs that validation itself. Without it the port would
  silently widen the Host's ``tool_executor`` contract to inputs it
  previously never saw.

Neither half ever raises out of a tool call: a socket failure on the child
side becomes an ``error: tool bridge unavailable: <ExcClass>`` text result
(never a raised exception back to ``claude -p``), and an executor exception
on the host side becomes the same ``error: tool {name!r} raised ...`` string
:func:`tests.evals.rollout.run_api_tool_loop` already produces for a
misbehaving executor — so the model sees an ordinary tool_result either way.
"""

from __future__ import annotations

import argparse
import json
import socket
import threading
from collections.abc import Callable
from typing import Any

#: Byte length read per recv() call on the bridge socket. Messages here are
#: short (a tool name + small JSON input/output) so one short read plus a
#: newline-delimited framing is sufficient; never a large file transfer.
_RECV_CHUNK = 65536


def _read_line(conn: socket.socket) -> str:
    """Read one newline-terminated line from *conn*, blocking until it
    arrives or the peer closes. Raises ``OSError``/``ConnectionError`` on a
    transport failure -- never swallowed here, the caller decides how to
    report it."""
    buf = b""
    while b"\n" not in buf:
        chunk = conn.recv(_RECV_CHUNK)
        if not chunk:
            if buf:
                break
            raise ConnectionError("bridge peer closed before sending a line")
        buf += chunk
    line, _, _ = buf.partition(b"\n")
    return line.decode("utf-8")


class ToolBridgeHost:
    """Context manager: binds *socket_path*, serves on a daemon thread,
    forwards each request to *tool_executor*.

    ``tool_executor(name, input) -> str`` is called exactly once per
    incoming request and is never allowed to abort the bridge -- any
    exception it raises is caught and turned into the same error string
    :func:`tests.evals.rollout.run_api_tool_loop` produces for a
    misbehaving executor on the api-mode path, so the two transports report
    an executor failure identically.
    """

    def __init__(
        self,
        socket_path: str,
        tool_executor: Callable[[str, dict[str, Any]], str],
    ) -> None:
        self._socket_path = socket_path
        self._tool_executor = tool_executor
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def __enter__(self) -> "ToolBridgeHost":
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self._socket_path)
        # 0600: this process and nothing else on the host may connect.
        import os

        os.chmod(self._socket_path, 0o600)
        server.listen(8)
        server.settimeout(0.5)
        self._server = server
        thread = threading.Thread(target=self._serve, daemon=True)
        self._thread = thread
        thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._server is not None:
            self._server.close()

    def _serve(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                # Socket closed from __exit__ while accept() was blocked.
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            try:
                line = _read_line(conn)
                request = json.loads(line)
                name = str(request["tool"])
                tool_input = request.get("input") or {}
                try:
                    text = self._tool_executor(name, tool_input)
                except Exception as exc:  # noqa: BLE001 -- never-raise contract
                    text = f"error: tool {name!r} raised {exc.__class__.__name__}: {exc}"
                conn.sendall((json.dumps({"text": text}) + "\n").encode("utf-8"))
            except Exception:  # noqa: BLE001 -- a malformed/aborted request must not crash the host
                try:
                    conn.sendall(
                        (
                            json.dumps({"text": "error: malformed tool bridge request"}) + "\n"
                        ).encode("utf-8")
                    )
                except OSError:
                    pass


def _call_bridge(socket_path: str, tool_name: str, tool_input: dict[str, Any]) -> str:
    """Child-side: one request/response round trip to the Host's socket.
    Never raises -- a transport failure becomes the error text itself."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.connect(socket_path)
            conn.sendall(
                (json.dumps({"tool": tool_name, "input": tool_input}) + "\n").encode("utf-8")
            )
            line = _read_line(conn)
            response = json.loads(line)
            return str(response.get("text", ""))
    except Exception as exc:  # noqa: BLE001 -- contract: never raise, see module docstring
        return f"error: tool bridge unavailable: {exc.__class__.__name__}"


def main(argv: list[str] | None = None) -> int:
    """Child entry point: ``python -m athenaeum.cli_tool_bridge``.

    Run exactly once per ``claude -p`` tool-passthrough spawn, launched by
    the ``claude`` binary itself per the generated ``--mcp-config``. Imports
    ``mcp`` lazily (module scope stays stdlib-only, see the module
    docstring) and serves a real MCP stdio server whose tool list and
    descriptions come verbatim from *tools_path*'s JSON.
    """
    import asyncio

    parser = argparse.ArgumentParser(prog="python -m athenaeum.cli_tool_bridge")
    parser.add_argument("--socket", required=True)
    parser.add_argument("--tools", required=True)
    parser.add_argument("--server-name", required=True)
    args = parser.parse_args(argv)

    with open(args.tools, encoding="utf-8") as f:
        specs: list[dict[str, Any]] = json.load(f)
    # Each entry carries BOTH the served (bare) name the child exposes to
    # claude -p and the original spec name the Host's tool_executor expects.
    # They differ exactly when the spec names DO carry an ``mcp__<server>__``
    # prefix -- `served` is then the bare trailing segment
    # (``mcp__athenaeum__recall`` -> ``recall``) -- and are identical in the
    # native-arm shape, where the specs are bare already. (Corrected in
    # athenaeum#1954: this comment previously stated the condition inverted.
    # See ``provider._resolve_tool_naming``, which is authoritative.)
    spec_name_by_served = {str(s["served_name"]): str(s["spec_name"]) for s in specs}
    # The schemas exactly as served by `tools/list` below -- validation must
    # judge a call against what the model was actually shown, never against
    # some other copy of the spec.
    input_schema_by_served = {
        str(s["served_name"]): dict(s.get("input_schema") or {}) for s in specs
    }

    # `mcp` hard-declares `jsonschema>=4.20.0`, so it is guaranteed present
    # wherever this child-only code path runs; it is declared alongside `mcp`
    # in pyproject anyway, since this module imports it directly.
    import jsonschema
    import mcp.server.stdio
    import mcp.types as types
    from mcp.server.context import ServerRequestContext
    from mcp.server.lowlevel import Server

    async def handle_list_tools(
        _ctx: ServerRequestContext[None],
        _params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=str(spec["served_name"]),
                    description=str(spec.get("description", "")),
                    input_schema=input_schema_by_served[str(spec["served_name"])],
                )
                for spec in specs
            ]
        )

    async def handle_call_tool(
        _ctx: ServerRequestContext[None],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        arguments = params.arguments or {}
        # Input validation, preserving the pre-2.0 decorator's default
        # behaviour verbatim -- same trigger (a KNOWN tool whose served
        # schema the arguments violate), same message, same is_error=True,
        # and the Host is never contacted. See the module docstring for why
        # this is open-coded rather than delegated to the SDK.
        schema = input_schema_by_served.get(params.name)
        if schema is not None:
            try:
                jsonschema.validate(instance=arguments, schema=schema)
            except jsonschema.ValidationError as exc:
                return types.CallToolResult(
                    content=[
                        types.TextContent(
                            type="text", text=f"Input validation error: {exc.message}"
                        )
                    ],
                    is_error=True,
                )
        spec_name = spec_name_by_served.get(params.name, params.name)
        text = _call_bridge(args.socket, spec_name, arguments)
        # is_error stays False for the bridge's OWN error strings (a dead
        # socket, a raising executor): the never-raise contract (see the
        # module docstring) is that the model sees an ordinary tool_result
        # carrying the error TEXT, identically to the api-mode path in
        # tests.evals.rollout.run_api_tool_loop. Only a validation failure
        # above is a protocol-level error, which is what 1.x did too.
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)])

    server: Server[None] = Server(
        args.server_name,
        on_list_tools=handle_list_tools,
        on_call_tool=handle_call_tool,
    )

    async def _run() -> None:
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
