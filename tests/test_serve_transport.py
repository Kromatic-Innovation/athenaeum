# SPDX-License-Identifier: Apache-2.0
"""``athenaeum serve`` transport selection and the loopback bind guard
(issue athenaeum#2382).

Four things are pinned here, in rising order of how much of the stack they
exercise:

1. **stdio is unchanged.** ``serve`` with none of the new flags must still
   call ``server.run()`` with NO arguments. The flags are additive; the
   default path is byte-identical to what predates them.
2. **Precedence.** ``--transport`` / ``--host`` / ``--port`` resolve
   CLI > env > ``athenaeum.yaml`` > default, the shape
   ``config.resolve_audience`` already uses for the sibling ``serve.audience``
   pin. Asserted end to end through ``main()``, not only at the resolver, so
   a resolver that is correct but unwired still fails.
3. **The loopback guard.** This is the security property the HTTP transport
   sits behind: an unauthenticated streamable-HTTP endpoint on a routable
   address publishes the whole knowledge store. The guard is a POSITIVE
   allowlist of loopback address literals, and the refusal happens before
   anything binds — proven by booby-trapping ``socket.socket.bind``, not by
   reading the source order.
4. **It actually serves MCP.** A real ``athenaeum serve --transport http``
   process, bound to 127.0.0.1 on a free port, answered by an in-process
   FastMCP client doing ``initialize``, ``tools/list`` and one ``recall``
   against a temp knowledge directory.

The server for (4) runs as a child process rather than a thread so the test
can terminate it deterministically; the MCP CLIENT is in-process, which is
what the acceptance criterion asks for. The child's environment is scrubbed
of ``KNOWLEDGE_RAW_PATH`` / ``KNOWLEDGE_WIKI_PATH`` — a developer machine
that pins those (they override the roots INDEPENDENTLY of ``--path``, see
``_resolve_serve_roots``) would otherwise have the test recall against the
operator's real corpus and pass for the wrong reason. ``FASTMCP_SHOW_SERVER_
BANNER=false`` is set for the same hermeticity reason and not for tidiness:
FastMCP's banner performs a PyPI version check, which would be an outbound
request from a child of the offline default selection (issue athenaeum#1091).
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from athenaeum import config as config_mod
from athenaeum._cmd_serve import (
    AUTH_ISSUER_SETTING,
    _is_loopback_bind_address,
    _non_loopback_bind_refusal,
    _normalized_bind_host,
)
from athenaeum.cli import main

#: Loopback bind addresses the guard must ACCEPT. Four shapes, each for its
#: own reason: the canonical IPv4 loopback; a second IPv4 loopback address
#: (the whole ``127.0.0.0/8`` block is loopback, not just ``.1``); IPv6
#: loopback bare and bracketed (a bracketed literal is what an operator
#: copies out of a URL or a client configuration); and an IPv4-MAPPED IPv6
#: loopback, which needs its own branch because ``IPv6Address.is_loopback``
#: is True only for ``::1`` itself.
LOOPBACK_ADDRESSES = (
    "127.0.0.1",
    "127.0.0.2",
    "::1",
    "[::1]",
    "::ffff:127.0.0.1",
    "::ffff:7f00:1",
)

#: Addresses the guard must REFUSE. The first four are plainly routable (or
#: wildcard) and are the point of the guard. The rest are the near misses a
#: deny-list or a substring check would wave through:
#:
#: * ``localhost`` — a NAME. Resolving it would make the decision depend on
#:   ``/etc/hosts``, DNS and search domains, so it is refused by the same
#:   rule as any other name, with its own hint in the message.
#: * ``127.0.0.1.example.invalid`` — a hostname that merely BEGINS with the
#:   loopback literal; a ``startswith("127.")`` check accepts it.
#: * ``0177.0.0.1`` — dotted-octal for 127.0.0.1 and accepted as loopback by
#:   ``inet_aton``; Python's ``ipaddress`` rejects the leading zeros outright,
#:   which is the conservative answer.
#: * ``2130706433`` / ``0x7f000001`` — decimal and hex integer forms of
#:   127.0.0.1 that some resolvers accept.
#: * ``""`` — empty, which several socket APIs read as "all interfaces".
NON_LOOPBACK_ADDRESSES = (
    "0.0.0.0",
    "::",
    "[::]",
    "192.168.1.5",
    "10.0.0.1",
    "localhost",
    "127.0.0.1.example.invalid",
    "0177.0.0.1",
    "2130706433",
    "0x7f000001",
    "",
)


def _free_port() -> int:
    """A port nothing is listening on, released before it is returned."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    return port


@pytest.fixture
def knowledge_dir(tmp_path: Path) -> Path:
    """A temp knowledge directory with two wiki pages and a keyword backend.

    ``keyword`` deliberately: it needs no compiled index and no embedder, so
    ``recall`` answers from the wiki files themselves and the HTTP round-trip
    below stays offline.
    """
    knowledge = tmp_path / "knowledge"
    wiki = knowledge / "wiki"
    wiki.mkdir(parents=True)
    (knowledge / "raw").mkdir()
    (wiki / "lean-startup.md").write_text(
        "---\n"
        "name: Lean Startup\n"
        "tags: [methodology]\n"
        "description: Build-measure-learn methodology\n"
        "---\n\n"
        "The Lean Startup methodology.\n",
        encoding="utf-8",
    )
    (wiki / "customer-development.md").write_text(
        "---\n"
        "name: Customer Development\n"
        "tags: [methodology]\n"
        "description: A four-step framework\n"
        "---\n\n"
        "Customer development is a four-step framework.\n",
        encoding="utf-8",
    )
    (knowledge / "athenaeum.yaml").write_text("search_backend: keyword\n", encoding="utf-8")
    return knowledge


class _RunRecorder:
    """Stands in for the FastMCP server, recording how ``run`` was called.

    ``KeyboardInterrupt`` is how ``cmd_serve`` already learns a server has
    stopped, so raising it returns the command cleanly with rc 0 without any
    transport ever starting.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def run(self, *args: object, **kwargs: object) -> None:
        self.calls.append((args, kwargs))
        raise KeyboardInterrupt


@pytest.fixture
def fake_server(monkeypatch: pytest.MonkeyPatch) -> _RunRecorder:
    """Replace ``create_server`` with a recorder, leaving the CLI path real."""
    recorder = _RunRecorder()
    import athenaeum.mcp_server as mcp_mod

    def _fake_create_server(**_kwargs: object) -> _RunRecorder:
        return recorder

    monkeypatch.setattr(mcp_mod, "create_server", _fake_create_server)
    return recorder


@pytest.fixture(autouse=True)
def _no_ambient_serve_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the knobs under test so an ambient value cannot decide a case."""
    for name in (
        "ATHENAEUM_SERVE_TRANSPORT",
        "ATHENAEUM_SERVE_HOST",
        "ATHENAEUM_SERVE_PORT",
        "KNOWLEDGE_RAW_PATH",
        "KNOWLEDGE_WIKI_PATH",
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# AC1 — stdio is the default and is unchanged.
# ---------------------------------------------------------------------------


class TestStdioIsStillTheDefault:
    def test_no_new_flags_runs_stdio_with_no_arguments(
        self, knowledge_dir: Path, fake_server: _RunRecorder
    ) -> None:
        """The pin: ``server.run()``, no transport, no host, no port.

        Asserting the EMPTY call rather than ``transport="stdio"`` is
        deliberate — it is the pre-athenaeum#2382 call verbatim, so an
        accidental ``run(transport=...)`` on the default path fails here even
        though FastMCP would behave identically.
        """
        rc = main(["serve", "--path", str(knowledge_dir)])
        assert rc == 0
        assert fake_server.calls == [((), {})]

    def test_stdio_ignores_a_non_loopback_host(
        self, knowledge_dir: Path, fake_server: _RunRecorder
    ) -> None:
        """``--host`` under stdio is inert, so it must NOT be refused.

        The guard protects a bind. stdio binds nothing, so refusing here
        would be a false positive that breaks an operator who set
        ``serve.host`` once for an occasional HTTP run.
        """
        rc = main(
            ["serve", "--path", str(knowledge_dir), "--host", "0.0.0.0"]
        )
        assert rc == 0
        assert fake_server.calls == [((), {})]


# ---------------------------------------------------------------------------
# AC3 — the loopback guard, at the unit and the command level.
# ---------------------------------------------------------------------------


class TestLoopbackAllowlist:
    @pytest.mark.parametrize("host", LOOPBACK_ADDRESSES)
    def test_loopback_literals_are_allowed(self, host: str) -> None:
        assert _is_loopback_bind_address(host) is True
        assert _non_loopback_bind_refusal(host) is None

    @pytest.mark.parametrize("host", NON_LOOPBACK_ADDRESSES)
    def test_everything_else_is_refused_and_names_the_setting(self, host: str) -> None:
        assert _is_loopback_bind_address(host) is False
        refusal = _non_loopback_bind_refusal(host)
        assert refusal is not None
        # The refusal has to say what is MISSING, not only what is banned:
        # an operator who reads "not a loopback address" learns nothing about
        # how to serve the address they actually want.
        assert AUTH_ISSUER_SETTING in refusal

    @pytest.mark.parametrize(
        ("given", "bound"),
        [
            ("127.0.0.1", "127.0.0.1"),
            ("  127.0.0.1  ", "127.0.0.1"),
            ("[::1]", "::1"),
            ("::1", "::1"),
            ("::ffff:127.0.0.1", "::ffff:127.0.0.1"),
        ],
    )
    def test_an_accepted_host_is_normalised_for_the_socket_layer(
        self, given: str, bound: str
    ) -> None:
        """A bracketed literal is a URL authority, not a bind address.

        ``getaddrinfo`` rejects ``[::1]``, so accepting the form an operator
        copies out of a URL and then passing it through verbatim would turn an
        allowed host into a bind failure.
        """
        assert _normalized_bind_host(given) == bound

    def test_a_hostname_refusal_says_names_are_never_resolved(self) -> None:
        refusal = _non_loopback_bind_refusal("localhost")
        assert refusal is not None
        assert "not an IP address literal" in refusal

    def test_an_address_literal_refusal_omits_the_hostname_hint(self) -> None:
        refusal = _non_loopback_bind_refusal("0.0.0.0")
        assert refusal is not None
        assert "not an IP address literal" not in refusal


class TestNonLoopbackBindIsRefusedBeforeBinding:
    def test_refusal_exits_non_zero_and_binds_nothing(
        self,
        knowledge_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The whole point of the guard, stated as an invariant.

        ``socket.socket.bind`` is booby-trapped and ``create_server`` is
        replaced with something that fails outright, so a refusal that
        happened too LATE — after construction, or after a listener was
        opened — fails this test rather than passing on source order.
        """

        def _no_bind(*_args: object, **_kwargs: object) -> None:
            raise AssertionError(
                "a refused serve invocation must not bind a socket "
                "(issue athenaeum#2382: the refusal precedes the bind)"
            )

        import athenaeum.mcp_server as mcp_mod

        def _no_create(**_kwargs: object) -> object:
            raise AssertionError("a refused serve invocation must not build a server")

        monkeypatch.setattr(socket.socket, "bind", _no_bind)
        monkeypatch.setattr(mcp_mod, "create_server", _no_create)

        rc = main(
            [
                "serve",
                "--path",
                str(knowledge_dir),
                "--transport",
                "http",
                "--host",
                "0.0.0.0",
                "--port",
                "8123",
            ]
        )
        assert rc != 0
        err = capsys.readouterr().err
        assert AUTH_ISSUER_SETTING in err
        assert "0.0.0.0" in err

    @pytest.mark.parametrize(
        ("host", "bound"),
        [("127.0.0.1", "127.0.0.1"), ("::1", "::1"), ("[::1]", "::1")],
    )
    def test_ipv4_and_ipv6_loopback_reach_the_transport(
        self, host: str, bound: str, knowledge_dir: Path, fake_server: _RunRecorder
    ) -> None:
        """Both loopback families are ALLOWED — the other half of the AC.

        A guard that refused everything would satisfy the refusal test above
        on its own; this is the positive control for it.
        """
        rc = main(
            [
                "serve",
                "--path",
                str(knowledge_dir),
                "--transport",
                "http",
                "--host",
                host,
                "--port",
                "8124",
            ]
        )
        assert rc == 0
        # `bound`, not `host`: the bracketed literal is accepted from the
        # operator but normalised before it reaches the socket layer.
        assert fake_server.calls == [
            ((), {"transport": "http", "host": bound, "port": 8124, "path": "/mcp"})
        ]

    def test_a_non_loopback_host_in_yaml_is_refused_too(
        self, knowledge_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard sits after resolution, so it covers every tier.

        A guard wired to the FLAG rather than to the resolved value would
        let ``serve.host: 0.0.0.0`` in ``athenaeum.yaml`` through.
        """
        (knowledge_dir / "athenaeum.yaml").write_text(
            "search_backend: keyword\nserve:\n  transport: http\n  host: 0.0.0.0\n",
            encoding="utf-8",
        )
        import athenaeum.mcp_server as mcp_mod

        def _no_create(**_kwargs: object) -> object:
            raise AssertionError("a refused serve invocation must not build a server")

        monkeypatch.setattr(mcp_mod, "create_server", _no_create)
        assert main(["serve", "--path", str(knowledge_dir)]) != 0


# ---------------------------------------------------------------------------
# AC4 — CLI > env > yaml, end to end through main().
# ---------------------------------------------------------------------------


class TestFlagPrecedence:
    def test_yaml_alone_selects_the_http_transport(
        self, knowledge_dir: Path, fake_server: _RunRecorder
    ) -> None:
        (knowledge_dir / "athenaeum.yaml").write_text(
            "search_backend: keyword\nserve:\n  transport: http\n  host: ::1\n  port: 9100\n",
            encoding="utf-8",
        )
        assert main(["serve", "--path", str(knowledge_dir)]) == 0
        assert fake_server.calls == [
            ((), {"transport": "http", "host": "::1", "port": 9100, "path": "/mcp"})
        ]

    def test_env_beats_yaml(
        self,
        knowledge_dir: Path,
        fake_server: _RunRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (knowledge_dir / "athenaeum.yaml").write_text(
            "search_backend: keyword\nserve:\n  transport: stdio\n  host: ::1\n  port: 9100\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("ATHENAEUM_SERVE_TRANSPORT", "http")
        monkeypatch.setenv("ATHENAEUM_SERVE_HOST", "127.0.0.2")
        monkeypatch.setenv("ATHENAEUM_SERVE_PORT", "9200")
        assert main(["serve", "--path", str(knowledge_dir)]) == 0
        assert fake_server.calls == [
            ((), {"transport": "http", "host": "127.0.0.2", "port": 9200, "path": "/mcp"})
        ]

    def test_cli_beats_env_and_yaml(
        self,
        knowledge_dir: Path,
        fake_server: _RunRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (knowledge_dir / "athenaeum.yaml").write_text(
            "search_backend: keyword\nserve:\n  transport: stdio\n  host: ::1\n  port: 9100\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("ATHENAEUM_SERVE_TRANSPORT", "stdio")
        monkeypatch.setenv("ATHENAEUM_SERVE_HOST", "127.0.0.2")
        monkeypatch.setenv("ATHENAEUM_SERVE_PORT", "9200")
        rc = main(
            [
                "serve",
                "--path",
                str(knowledge_dir),
                "--transport",
                "http",
                "--host",
                "127.0.0.3",
                "--port",
                "9300",
                "--path-prefix",
                "/memory",
            ]
        )
        assert rc == 0
        assert fake_server.calls == [
            ((), {"transport": "http", "host": "127.0.0.3", "port": 9300, "path": "/memory"})
        ]

    def test_resolvers_default_without_config_env_or_cli(self) -> None:
        assert config_mod.resolve_serve_transport(None) == "stdio"
        assert config_mod.resolve_serve_host(None) == "127.0.0.1"
        assert config_mod.resolve_serve_port(None) == 8000

    def test_an_unknown_transport_falls_back_to_stdio(self) -> None:
        """Out-of-vocabulary values fall back, and the fallback is the safe
        direction: a typo can only ever land on the transport that binds
        nothing."""
        assert config_mod.resolve_serve_transport({"serve": {"transport": "htttp"}}) == "stdio"
        assert config_mod.resolve_serve_transport(None, "HTTP") == "http"

    @pytest.mark.parametrize("bad", [0, -1, 65536, 99999, True, "9000", 80.5])
    def test_an_unusable_port_falls_back_to_the_default(self, bad: object) -> None:
        """Port 0, out-of-range, bool and non-int values all fall through.

        Port 0 matters on its own: the kernel would hand out an arbitrary
        free port, so a config file could produce a server at an address
        nobody was told.
        """
        assert config_mod.resolve_serve_port({"serve": {"port": bad}}) == 8000


# ---------------------------------------------------------------------------
# AC5 — construction stays O(1) in corpus size under the HTTP transport too.
# ---------------------------------------------------------------------------


class TestHttpTransportDoesNotScanTheCorpus:
    def test_create_server_does_not_resolve_entity_classes_under_http(
        self, knowledge_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issue athenaeum#1194's invariant, re-stated on the new path.

        The corpus scan was what blew the MCP connect budget. ``create_server``
        is transport-agnostic by design, so this cannot regress from a
        transport change alone — which is exactly why it is worth pinning:
        it is the test that would catch a future "just resolve the classes in
        ``cmd_serve`` for the HTTP case" shortcut.
        """
        import athenaeum.entity_schema as es
        import athenaeum.mcp_server as mcp_mod

        def _boom(*_args: object, **_kwargs: object) -> object:
            raise AssertionError(
                "resolve_entity_classes must not run during create_server "
                "(issue athenaeum#1194: it is a corpus-wide scan on the MCP "
                "handshake path)"
            )

        monkeypatch.setattr(es, "resolve_entity_classes", _boom)

        real_create_server = mcp_mod.create_server
        recorder = _RunRecorder()
        built: list[object] = []

        def _real_then_record(**kwargs: object) -> _RunRecorder:
            built.append(real_create_server(**kwargs))  # type: ignore[arg-type]
            return recorder

        monkeypatch.setattr(mcp_mod, "create_server", _real_then_record)

        rc = main(
            [
                "serve",
                "--path",
                str(knowledge_dir),
                "--transport",
                "http",
                "--host",
                "127.0.0.1",
                "--port",
                "8125",
            ]
        )
        assert rc == 0
        assert len(built) == 1
        assert recorder.calls == [
            ((), {"transport": "http", "host": "127.0.0.1", "port": 8125, "path": "/mcp"})
        ]


# ---------------------------------------------------------------------------
# AC2 — a real HTTP server, answered by an in-process MCP client.
# ---------------------------------------------------------------------------

#: How long the child process gets to bind its port. Generous: it imports
#: athenaeum and FastMCP from cold. Far below any CI job budget, and a miss
#: fails loudly with the child's own output rather than hanging.
_BIND_TIMEOUT_SECONDS = 60.0

#: Ceiling on the whole MCP conversation once the port answers.
_CLIENT_TIMEOUT_SECONDS = 60.0


def _wait_until_listening(port: int, proc: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + _BIND_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(
                f"serve --transport http exited early with {proc.returncode}:\n"
                f"{proc.communicate()[0]}"
            )
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    pytest.fail(f"serve --transport http never bound 127.0.0.1:{port}")


class TestHttpTransportServesMcp:
    def test_initialize_list_tools_and_recall_over_streamable_http(
        self, knowledge_dir: Path, tmp_path: Path
    ) -> None:
        """The acceptance criterion, end to end.

        A real ``athenaeum serve --transport http --port N`` process, and an
        in-process FastMCP client that completes the MCP handshake, lists
        tools and calls ``recall`` against the temp knowledge directory. The
        recall assertion names a page this test wrote, so a server that
        answered from some other corpus (an ambient ``KNOWLEDGE_WIKI_PATH``,
        say) fails rather than passes on a non-empty result.
        """
        from fastmcp import Client

        port = _free_port()
        env = dict(os.environ)
        env.pop("KNOWLEDGE_RAW_PATH", None)
        env.pop("KNOWLEDGE_WIKI_PATH", None)
        env["ATHENAEUM_CACHE_DIR"] = str(tmp_path / "cache")
        env["FASTMCP_SHOW_SERVER_BANNER"] = "false"

        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys; from athenaeum.cli import main; sys.exit(main(sys.argv[1:]))",
                "serve",
                "--path",
                str(knowledge_dir),
                "--transport",
                "http",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            _wait_until_listening(port, proc)

            async def _converse() -> tuple[set[str], str]:
                async with Client(f"http://127.0.0.1:{port}/mcp") as client:
                    # Entering the context block IS the `initialize`
                    # handshake; `is_connected` is false if it did not land.
                    assert client.is_connected()
                    tools = await client.list_tools()
                    result = await client.call_tool("recall", {"query": "lean startup"})
                    texts = " ".join(
                        getattr(block, "text", "") for block in (result.content or [])
                    )
                    return {tool.name for tool in tools}, texts

            tool_names, recall_text = asyncio.run(
                asyncio.wait_for(_converse(), timeout=_CLIENT_TIMEOUT_SECONDS)
            )
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                proc.kill()
                proc.wait(timeout=30)

        assert {"recall", "remember", "entity_schema"} <= tool_names
        assert "Lean Startup" in recall_text
