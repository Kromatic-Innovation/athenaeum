# SPDX-License-Identifier: Apache-2.0
"""OAuth 2.1 resource-server mode for ``athenaeum serve`` (issue athenaeum#2384).

One class per acceptance criterion:

- :class:`TestSettingsResolveCliOverEnvOverYaml` (AC1) — the four
  ``serve.auth.*`` knobs, per-tier, plus the partial-configuration and
  malformed-value refusals ``build_serve_auth`` exists for.
- :class:`TestAnIssuerLiftsTheNonLoopbackRefusal` (AC2) — both halves: with
  an issuer a routable bind is reached, without one it is still refused,
  asserted end to end through ``main()`` so a lift that is correct in the
  guard but unwired still fails.
- :class:`TestProtectedResourceMetadata` (AC3) — the RFC 9728 document, read
  off a live ASGI app.
- :class:`TestEveryRejectionIsA401WithAChallenge` (AC4) — the six rejection
  cases, each asserted to be a 401 whose ``WWW-Authenticate`` names a
  ``resource_metadata`` URL that really answers.
- :class:`TestAValidTokenReachesTheToolSurface` (AC5).
- :class:`TestScopesSplitReadFromWrite` (AC6).
- :class:`TestTheAudienceIsTheProcessPinNotAClaim` (AC7) — the heart of the
  item. A token stuffed with audience-like claims naming a broader role gets
  the same ``recall`` bytes a bare token does, and an owner-scoped server
  proves those bytes are not simply everything.
- :class:`TestTokensNeverReachLogs` (AC8) — across a failed AND a successful
  call.
- :class:`TestDocsDocumentTheResourceServer` (AC9).

**No network, anywhere.** The fake issuer's key set is served by an in-process
ASGI callable over ``httpx2.ASGITransport``, handed to FastMCP's ``JWTVerifier``
as its HTTP client (``create_server(jwks_http_client=...)``); the MCP client
talks to ``create_server``'s own ``http_app`` over ``httpx2.ASGITransport``
too. Nothing opens a socket, so the suite is safe under ``-n auto`` with no
port allocation. :meth:`_FakeIssuer.fetches` is the positive control that the
key set really was fetched through that transport — a verifier that somehow
never consulted it would make every signature assertion below vacuous.

**No PEM is written by hand.** The signer is generated as a JWK and published
as a JWK; the key set is JSON from end to end, so no PEM banner line appears
in this file at all. That is both what the repo's secret scan requires (its
stock private-key rule fires on a pair of banner lines in one file, with no
key material needed) and simpler than a PEM round-trip would be.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from athenaeum.cli import main
from athenaeum.config import (
    resolve_serve_auth_allowed_identities,
    resolve_serve_auth_issuer,
    resolve_serve_auth_resource,
    resolve_serve_auth_scopes,
)
from athenaeum.serve_auth import (
    DEFAULT_READ_SCOPE,
    DEFAULT_WRITE_SCOPE,
    JWKS_PATH,
    SIGNING_ALGORITHM,
    ServeAuthError,
    build_serve_auth,
)

#: The fake authorization server's issuer identifier. ``.invalid`` is reserved
#: by RFC 2606, so even a bug that bypassed the in-process transport could
#: not reach a real host.
ISSUER = "https://issuer.example.invalid"

#: Another issuer, for the wrong-``iss`` case.
OTHER_ISSUER = "https://attacker.example.invalid"

#: This server's canonical public URL — the audience pin. Deliberately
#: path-less, so the RFC 9728 metadata lands at exactly the
#: ``/.well-known/oauth-protected-resource`` path the acceptance criterion
#: names; :class:`TestProtectedResourceMetadata` covers the path-scoped
#: variant too.
RESOURCE = "https://memory.example.invalid"

#: A DIFFERENT resource, for the wrong-audience case: a token legitimately
#: minted by the same issuer, for a sibling deployment.
OTHER_RESOURCE = "https://other-memory.example.invalid"

#: The identity on the allowlist, and one that is not.
ALLOWED_SUBJECT = "auth0|operator-1"
ALLOWED_EMAIL = "operator@example.invalid"
STRANGER_SUBJECT = "auth0|stranger-9"

#: Where the MCP endpoint is mounted in these tests — ``_cmd_serve``'s own
#: ``DEFAULT_SERVE_PATH_PREFIX``.
MCP_PATH = "/mcp"

#: The key id the fake issuer publishes and signs with.
SIGNING_KID = "fake-issuer-1"

#: A read-scoped tool and a write-scoped one, named once. ``recall`` carries
#: ``readOnlyHint: true`` and ``remember`` ``readOnlyHint: false``, which is
#: what ``create_server`` derives each tool's required scope from.
READ_TOOL = "recall"
WRITE_TOOL = "remember"


def _serve_auth(**overrides: Any):
    """A fully configured :class:`~athenaeum.serve_auth.ServeAuth`."""
    kwargs: dict[str, Any] = {
        "issuer": ISSUER,
        "resource": RESOURCE,
        "allowed_identities": frozenset({ALLOWED_SUBJECT, ALLOWED_EMAIL}),
    }
    kwargs.update(overrides)
    built = build_serve_auth(**kwargs)
    assert built is not None, "sanity: the fixture must configure auth"
    return built


@dataclass
class _FakeIssuer:
    """An in-process authorization server: one signer, one published key set.

    Only the two things a RESOURCE server needs exist here — a signature and a
    key set to check it against. There is no authorization endpoint, no token
    endpoint and no client registry, because athenaeum never talks to any of
    them: that is the whole point of the resource-server role.
    """

    signer: Any
    other_signer: Any
    jwks: dict[str, Any]
    fetches: list[str] = field(default_factory=list)

    async def _app(self, scope: dict, receive: Any, send: Any) -> None:
        self.fetches.append(scope.get("path", ""))
        body = json.dumps(self.jwks).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    def http_client(self) -> Any:
        """A fresh client bound to this issuer's key-set app, no socket.

        A client rather than a transport because that is what FastMCP's
        ``JWTVerifier`` takes, and building one inside ``src/`` would mean
        declaring its HTTP library as an athenaeum runtime dependency for a
        seam only this file uses. FastMCP does not close a client it was
        handed, so a fresh one per server is correct and cheap.
        """
        import httpx2

        return httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=self._app),
            timeout=httpx2.Timeout(10.0),
        )

    def mint(self, *, signer: Any = None, kid: str = SIGNING_KID, **claims: Any) -> str:
        """A signed JWT. *claims* overrides and extends the valid baseline."""
        from joserfc import jwt
        from joserfc.jws import JWSRegistry

        now = int(time.time())
        payload: dict[str, Any] = {
            "iss": ISSUER,
            "aud": RESOURCE + "/",
            "sub": ALLOWED_SUBJECT,
            "iat": now,
            "nbf": now,
            "exp": now + 300,
            "scope": f"{DEFAULT_READ_SCOPE} {DEFAULT_WRITE_SCOPE}",
        }
        payload.update(claims)
        return jwt.encode(
            {"alg": SIGNING_ALGORITHM, "kid": kid},
            payload,
            signer if signer is not None else self.signer,
            registry=JWSRegistry(algorithms=[SIGNING_ALGORITHM]),
        )


@pytest.fixture(scope="module")
def issuer() -> _FakeIssuer:
    """The fake issuer. Module-scoped: RSA generation is the only slow step."""
    pytest.importorskip("fastmcp")
    pytest.importorskip("httpx2")
    jwk = pytest.importorskip("joserfc.jwk")

    signer = jwk.RSAKey.generate_key(2048, private=True)
    other_signer = jwk.RSAKey.generate_key(2048, private=True)
    published = dict(signer.as_dict(private=False))
    published.update(kid=SIGNING_KID, use="sig", alg=SIGNING_ALGORITHM)
    return _FakeIssuer(
        signer=signer,
        other_signer=other_signer,
        jwks={"keys": [published]},
    )


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """A knowledge directory with one open page and one confidential page.

    ``search_backend: keyword`` needs neither a compiled index nor an
    embedder, so ``recall`` answers from the wiki files themselves. Both pages
    mention ``widget`` so one query returns both for the owner — which is what
    makes the audience-pin assertion in AC7 non-vacuous.
    """
    knowledge = tmp_path / "knowledge"
    wiki = knowledge / "wiki"
    wiki.mkdir(parents=True)
    (knowledge / "raw").mkdir()
    (wiki / "open-widget.md").write_text(
        "---\nname: Open Widget\naccess: open\n"
        "description: A widget anybody may read\n---\n\n"
        "The open widget record.\n",
        encoding="utf-8",
    )
    (wiki / "secret-widget.md").write_text(
        "---\nname: Secret Widget\naccess: confidential\n"
        "description: A widget only the owner may read\n---\n\n"
        "The confidential widget record.\n",
        encoding="utf-8",
    )
    (knowledge / "athenaeum.yaml").write_text("search_backend: keyword\n", encoding="utf-8")
    return knowledge


@pytest.fixture(autouse=True)
def _no_ambient_serve_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the knobs under test so an ambient value cannot decide a case."""
    for name in (
        "ATHENAEUM_SERVE_AUTH_ISSUER",
        "ATHENAEUM_SERVE_AUTH_RESOURCE",
        "ATHENAEUM_SERVE_AUTH_ALLOWED_IDENTITIES",
        "ATHENAEUM_SERVE_AUTH_SCOPES",
        "ATHENAEUM_SERVE_TRANSPORT",
        "ATHENAEUM_SERVE_HOST",
        "ATHENAEUM_SERVE_PORT",
        "KNOWLEDGE_RAW_PATH",
        "KNOWLEDGE_WIKI_PATH",
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# The in-process serving harness.
# ---------------------------------------------------------------------------


def _build_app(corpus: Path, issuer: _FakeIssuer, serve_auth: Any, **kwargs: Any) -> Any:
    from athenaeum.mcp_server import create_server

    server = create_server(
        raw_root=corpus / "raw",
        wiki_root=corpus / "wiki",
        cache_dir=corpus.parent / "cache",
        serve_auth=serve_auth,
        jwks_http_client=issuer.http_client() if serve_auth is not None else None,
        **kwargs,
    )
    return server.http_app(path=MCP_PATH)


def _drive(
    corpus: Path,
    issuer: _FakeIssuer,
    serve_auth: Any,
    body: Callable[[Any], Awaitable[Any]],
    **kwargs: Any,
) -> Any:
    """Run *body* against a live ASGI app built from these pins.

    The Starlette lifespan is entered explicitly: FastMCP's streamable-HTTP
    session manager is started there, so an ``ASGITransport`` that skipped it
    would answer the 401 cases correctly and then hang on the first real MCP
    request — a failure mode that would look like a timeout rather than a
    missing setup step.
    """
    app = _build_app(corpus, issuer, serve_auth, **kwargs)

    async def _run() -> Any:
        async with app.router.lifespan_context(app):
            return await body(app)

    return asyncio.run(_run())


@contextlib.asynccontextmanager
async def _raw_client(app: Any) -> Any:
    """An ``httpx`` client speaking to *app* in-process."""
    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://athenaeum.test.invalid",
    ) as client:
        yield client


@contextlib.asynccontextmanager
async def _mcp_client(app: Any, token: str | None) -> Any:
    """A real FastMCP client, over the same in-process transport."""
    import httpx2
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    def _factory(**kwargs: Any) -> Any:
        # `verify` is meaningless for an ASGI transport and `httpx2` rejects
        # it alongside an explicit transport.
        kwargs.pop("verify", None)
        kwargs.setdefault("timeout", httpx2.Timeout(30.0))
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), **kwargs)

    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    transport = StreamableHttpTransport(
        url=f"http://athenaeum.test.invalid{MCP_PATH}",
        headers=headers,
        httpx_client_factory=_factory,
    )
    async with Client(transport) as client:
        yield client


#: Minimal well-typed stand-ins for a required tool parameter, by JSON Schema
#: type. Used only to get PAST FastMCP's argument validation so a scope check
#: is what actually decides the call -- never to exercise a tool's behaviour.
_DUMMY_BY_TYPE: dict[str, Any] = {
    "string": "x",
    "integer": 1,
    "number": 1,
    "boolean": False,
    "array": [],
    "object": {},
}


def _dummy_arguments(tool: Any) -> dict[str, Any]:
    """Well-typed values for *tool*'s REQUIRED parameters, read off its schema.

    Derived rather than hand-listed so a tool that gains a required parameter
    does not quietly turn a scope assertion into a validation-error assertion.
    """
    schema = getattr(tool, "input_schema", None) or {}
    properties = schema.get("properties") or {}
    arguments: dict[str, Any] = {}
    for name in schema.get("required") or []:
        declared = properties.get(name) or {}
        kind = declared.get("type")
        if isinstance(kind, list):
            kind = next((k for k in kind if k != "null"), "string")
        arguments[name] = _DUMMY_BY_TYPE.get(kind, "x")
    return arguments


_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "athenaeum-test", "version": "1"},
    },
}


def _post_initialize(
    corpus: Path, issuer: _FakeIssuer, serve_auth: Any, token: str | None
) -> tuple[int, str]:
    """``POST /mcp`` with (or without) *token*; return (status, WWW-Authenticate).

    ``initialize`` rather than a tool call deliberately: it is the FIRST
    request any MCP client makes, so a rejection here is the rejection a real
    client would actually see, before any session exists.
    """

    async def _body(app: Any) -> tuple[int, str]:
        async with _raw_client(app) as client:
            headers = {
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            }
            if token is not None:
                headers["Authorization"] = f"Bearer {token}"
            response = await client.post(MCP_PATH, headers=headers, json=_INITIALIZE)
            return response.status_code, response.headers.get("www-authenticate", "")

    return _drive(corpus, issuer, serve_auth, _body)


# ---------------------------------------------------------------------------
# AC1 — the four settings, CLI > env > yaml.
# ---------------------------------------------------------------------------


class TestSettingsResolveCliOverEnvOverYaml:
    YAML = {
        "serve": {
            "auth": {
                "issuer": "https://yaml.example.invalid",
                "resource": "https://yaml-resource.example.invalid",
                "allowed_identities": ["yaml-subject"],
                "scopes": {"read": "yaml:read", "write": "yaml:write"},
            }
        }
    }

    def test_nothing_configured_resolves_to_no_auth(self) -> None:
        assert resolve_serve_auth_issuer(None) is None
        assert resolve_serve_auth_resource(None) is None
        assert resolve_serve_auth_allowed_identities(None) is None
        assert resolve_serve_auth_scopes(None) is None
        assert build_serve_auth() is None

    def test_yaml_alone_is_read(self) -> None:
        assert resolve_serve_auth_issuer(self.YAML) == "https://yaml.example.invalid"
        assert (
            resolve_serve_auth_resource(self.YAML) == "https://yaml-resource.example.invalid"
        )
        assert resolve_serve_auth_allowed_identities(self.YAML) == frozenset({"yaml-subject"})
        assert resolve_serve_auth_scopes(self.YAML) == {
            "read": "yaml:read",
            "write": "yaml:write",
        }

    def test_env_beats_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_SERVE_AUTH_ISSUER", "https://env.example.invalid")
        monkeypatch.setenv("ATHENAEUM_SERVE_AUTH_RESOURCE", "https://env-res.example.invalid")
        monkeypatch.setenv("ATHENAEUM_SERVE_AUTH_ALLOWED_IDENTITIES", "env-a, env-b")
        monkeypatch.setenv("ATHENAEUM_SERVE_AUTH_SCOPES", "read=env:read,write=env:write")
        assert resolve_serve_auth_issuer(self.YAML) == "https://env.example.invalid"
        assert resolve_serve_auth_resource(self.YAML) == "https://env-res.example.invalid"
        assert resolve_serve_auth_allowed_identities(self.YAML) == frozenset(
            {"env-a", "env-b"}
        )
        assert resolve_serve_auth_scopes(self.YAML) == {
            "read": "env:read",
            "write": "env:write",
        }

    def test_cli_beats_env_and_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_SERVE_AUTH_ISSUER", "https://env.example.invalid")
        monkeypatch.setenv("ATHENAEUM_SERVE_AUTH_RESOURCE", "https://env-res.example.invalid")
        monkeypatch.setenv("ATHENAEUM_SERVE_AUTH_ALLOWED_IDENTITIES", "env-a")
        monkeypatch.setenv("ATHENAEUM_SERVE_AUTH_SCOPES", "read=env:read,write=env:write")
        assert (
            resolve_serve_auth_issuer(self.YAML, "https://cli.example.invalid")
            == "https://cli.example.invalid"
        )
        assert (
            resolve_serve_auth_resource(self.YAML, "https://cli-res.example.invalid")
            == "https://cli-res.example.invalid"
        )
        assert resolve_serve_auth_allowed_identities(self.YAML, "Cli-A,CLI-B") == frozenset(
            {"cli-a", "cli-b"}
        )
        assert resolve_serve_auth_scopes(self.YAML, "read=cli:read,write=cli:write") == {
            "read": "cli:read",
            "write": "cli:write",
        }

    def test_identities_are_case_folded_and_deduplicated(self) -> None:
        """An allowlist that missed on capitalisation would lock the operator out."""
        assert resolve_serve_auth_allowed_identities(
            None, "Operator@Example.Invalid, operator@example.invalid , "
        ) == frozenset({"operator@example.invalid"})

    @pytest.mark.parametrize(
        "pins",
        [
            pytest.param({"issuer": ISSUER}, id="issuer-without-resource"),
            pytest.param({"resource": RESOURCE}, id="resource-without-issuer"),
            pytest.param(
                {"allowed_identities": frozenset({ALLOWED_SUBJECT})},
                id="identities-alone",
            ),
        ],
    )
    def test_a_partial_block_is_refused(self, pins: dict) -> None:
        """Half-configured auth must not resolve to "no auth".

        That is the dangerous failure: ``None`` lifts nothing and keeps the
        loopback-only refusal, so a typo'd issuer next to a configured
        resource would leave an operator who believes they configured auth
        with a server that never verifies anything.
        """
        with pytest.raises(ServeAuthError):
            build_serve_auth(**pins)

    def test_an_empty_allowlist_is_refused(self) -> None:
        with pytest.raises(ServeAuthError, match="allowed_identities is empty"):
            build_serve_auth(
                issuer=ISSUER, resource=RESOURCE, allowed_identities=frozenset()
            )

    @pytest.mark.parametrize(
        "resource",
        ["memory.example.invalid", "/mcp", "ftp://memory.example.invalid", ""],
    )
    def test_a_resource_that_is_not_an_absolute_http_url_is_refused(
        self, resource: str
    ) -> None:
        with pytest.raises(ServeAuthError):
            build_serve_auth(
                issuer=ISSUER,
                resource=resource,
                allowed_identities=frozenset({ALLOWED_SUBJECT}),
            )

    @pytest.mark.parametrize(
        "scopes",
        [
            pytest.param({"read": "same", "write": "same"}, id="identical-names"),
            pytest.param({"read": "r"}, id="missing-write"),
            pytest.param({"read": "r", "write": "w", "admin": "a"}, id="unknown-role"),
            pytest.param("just-one-name", id="not-a-pair"),
        ],
    )
    def test_a_malformed_scope_pair_is_refused(self, scopes: Any) -> None:
        """A silently-dropped half would make the read scope authorize writes."""
        with pytest.raises(ServeAuthError):
            resolved = (
                scopes
                if isinstance(scopes, dict)
                else resolve_serve_auth_scopes(None, scopes)
            )
            build_serve_auth(
                issuer=ISSUER,
                resource=RESOURCE,
                allowed_identities=frozenset({ALLOWED_SUBJECT}),
                scopes=resolved,
            )

    def test_the_default_scope_names_are_used_when_unset(self) -> None:
        auth = _serve_auth()
        assert auth.read_scope == DEFAULT_READ_SCOPE
        assert auth.write_scope == DEFAULT_WRITE_SCOPE
        assert auth.scopes_supported == sorted([DEFAULT_READ_SCOPE, DEFAULT_WRITE_SCOPE])

    def test_the_jwks_uri_is_derived_from_the_issuer(self) -> None:
        assert _serve_auth().jwks_uri == ISSUER + JWKS_PATH
        assert (
            _serve_auth(issuer="https://tenant.example.invalid/t1/").jwks_uri
            == "https://tenant.example.invalid/t1" + JWKS_PATH
        )

    def test_the_resource_pin_is_rendered_exactly_as_it_is_advertised(self) -> None:
        """The audience has to be ONE string in three places (see
        ``serve_auth._canonical_resource_url``). A path-less pin that lost its
        trailing slash on the way into the metadata document would make every
        correctly-obtained token fail its audience check."""
        from pydantic import AnyHttpUrl

        for given in (RESOURCE, RESOURCE + "/", RESOURCE + "/mcp"):
            pin = _serve_auth(resource=given).resource
            assert pin == str(AnyHttpUrl(pin))


# ---------------------------------------------------------------------------
# AC2 — an issuer lifts the non-loopback bind refusal; nothing else does.
# ---------------------------------------------------------------------------


class _RunRecorder:
    """Stands in for the FastMCP server, recording how ``run`` was called."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.create_kwargs: dict[str, object] = {}

    def run(self, *_args: object, **kwargs: object) -> None:
        self.calls.append(kwargs)
        raise KeyboardInterrupt


@pytest.fixture
def fake_server(monkeypatch: pytest.MonkeyPatch) -> _RunRecorder:
    """Replace ``create_server`` with a recorder, leaving the CLI path real."""
    recorder = _RunRecorder()
    import athenaeum.mcp_server as mcp_mod

    def _fake_create_server(**kwargs: object) -> _RunRecorder:
        recorder.create_kwargs = kwargs
        return recorder

    monkeypatch.setattr(mcp_mod, "create_server", _fake_create_server)
    return recorder


class TestAnIssuerLiftsTheNonLoopbackRefusal:
    AUTH_YAML = (
        "search_backend: keyword\n"
        "serve:\n"
        "  auth:\n"
        f"    issuer: {ISSUER}\n"
        f"    resource: {RESOURCE}\n"
        "    allowed_identities:\n"
        f"      - {ALLOWED_SUBJECT}\n"
    )

    def test_without_an_issuer_a_routable_bind_is_still_refused(
        self, corpus: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The negative control for the lift, and the pre-existing behaviour."""
        import athenaeum.mcp_server as mcp_mod

        def _no_create(**_kwargs: object) -> object:
            raise AssertionError("a refused serve invocation must not build a server")

        monkeypatch.setattr(mcp_mod, "create_server", _no_create)
        rc = main(
            ["serve", "--path", str(corpus), "--transport", "http", "--host", "0.0.0.0"]
        )
        assert rc != 0
        assert "serve.auth.issuer" in capsys.readouterr().err

    def test_with_an_issuer_configured_a_routable_bind_is_reached(
        self, corpus: Path, fake_server: _RunRecorder, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The lift, end to end through ``main()``.

        Asserted on ``server.run``'s own arguments rather than on the absence
        of a message: a guard that stopped refusing but also stopped passing
        the host through would satisfy a weaker assertion.
        """
        (corpus / "athenaeum.yaml").write_text(self.AUTH_YAML, encoding="utf-8")
        rc = main(
            ["serve", "--path", str(corpus), "--transport", "http", "--host", "0.0.0.0"]
        )
        assert rc == 0
        assert fake_server.calls == [
            {
                "transport": "http",
                "host": "0.0.0.0",
                "port": 8000,
                "path": MCP_PATH,
                "host_origin_protection": False,
            }
        ]
        # And the pins really reached the server, not just the guard.
        built = fake_server.create_kwargs["serve_auth"]
        assert built is not None
        assert built.resource == RESOURCE + "/"
        assert "OAuth 2.1 resource server" in capsys.readouterr().err

    def test_the_lift_takes_the_cli_flags_too(
        self, corpus: Path, fake_server: _RunRecorder
    ) -> None:
        rc = main(
            [
                "serve",
                "--path",
                str(corpus),
                "--transport",
                "http",
                "--host",
                "0.0.0.0",
                "--auth-issuer",
                ISSUER,
                "--auth-resource",
                RESOURCE,
                "--auth-allowed-identities",
                ALLOWED_SUBJECT,
            ]
        )
        assert rc == 0
        assert fake_server.calls and fake_server.calls[0]["host"] == "0.0.0.0"

    def test_a_half_configured_block_refuses_before_binding(
        self, corpus: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An issuer with no resource must not lift anything.

        ``socket.socket.bind`` is booby-trapped, so a refusal that happened
        after a listener opened fails this test rather than passing on source
        order.
        """
        import socket

        import athenaeum.mcp_server as mcp_mod

        def _no_bind(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("a refused serve invocation must not bind a socket")

        def _no_create(**_kwargs: object) -> object:
            raise AssertionError("a refused serve invocation must not build a server")

        monkeypatch.setattr(socket.socket, "bind", _no_bind)
        monkeypatch.setattr(mcp_mod, "create_server", _no_create)
        (corpus / "athenaeum.yaml").write_text(
            f"search_backend: keyword\nserve:\n  auth:\n    issuer: {ISSUER}\n",
            encoding="utf-8",
        )
        rc = main(
            ["serve", "--path", str(corpus), "--transport", "http", "--host", "0.0.0.0"]
        )
        assert rc != 0
        assert "serve.auth.resource is required" in capsys.readouterr().err

    def test_an_auth_block_under_stdio_is_reported_inert(
        self, corpus: Path, fake_server: _RunRecorder, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """stdio binds no socket and carries no bearer token.

        Attaching a resource server there would scope-gate every tool against
        a token that structurally cannot exist, so the pins are dropped — out
        loud, because silently ignoring them is how an operator comes to
        believe a local server is authenticated.
        """
        (corpus / "athenaeum.yaml").write_text(self.AUTH_YAML, encoding="utf-8")
        assert main(["serve", "--path", str(corpus)]) == 0
        assert fake_server.create_kwargs["serve_auth"] is None
        assert "ignoring serve.auth.*" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# AC3 — RFC 9728 protected resource metadata.
# ---------------------------------------------------------------------------


class TestProtectedResourceMetadata:
    def _metadata(self, corpus: Path, issuer: _FakeIssuer, resource: str) -> tuple[int, dict]:
        auth = _serve_auth(resource=resource)

        async def _body(app: Any) -> tuple[int, dict]:
            from mcp.server.auth.routes import build_resource_metadata_url
            from pydantic import AnyHttpUrl

            path = str(build_resource_metadata_url(AnyHttpUrl(auth.resource)))
            path = path.split("://", 1)[1].split("/", 1)[1]
            async with _raw_client(app) as client:
                response = await client.get("/" + path)
                return response.status_code, (
                    response.json() if response.status_code == 200 else {}
                )

        return _drive(corpus, issuer, auth, _body)

    def test_the_bare_well_known_path_answers_for_a_path_less_resource(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        """The literal path the acceptance criterion names."""

        async def _body(app: Any) -> tuple[int, dict]:
            async with _raw_client(app) as client:
                response = await client.get("/.well-known/oauth-protected-resource")
                return response.status_code, response.json()

        status, document = _drive(corpus, issuer, _serve_auth(), _body)
        assert status == 200
        assert document["resource"] == RESOURCE + "/"
        assert document["scopes_supported"] == [DEFAULT_READ_SCOPE, DEFAULT_WRITE_SCOPE]
        # `authorization_servers` is a list of one: the configured issuer.
        # Compared slash-insensitively because the MCP SDK's metadata model
        # types it as a pydantic URL, which renders a path-less URL with a
        # trailing slash. The ISSUER is deliberately not normalized by
        # athenaeum -- `iss` is matched against the token verbatim and only
        # the operator knows which form their authorization server mints.
        assert [s.rstrip("/") for s in document["authorization_servers"]] == [ISSUER]

    @pytest.mark.parametrize("configured", [ISSUER, ISSUER + "/"])
    def test_the_issuer_is_advertised_byte_for_byte(
        self, corpus: Path, issuer: _FakeIssuer, configured: str
    ) -> None:
        """athenaeum#2600: no trailing slash is added to, or taken from, the
        configured issuer. Compared on the raw response bytes, not URL objects,
        because RFC 8414 / RFC 9207 match the issuer as an exact string."""

        async def _body(app: Any) -> tuple[int, bytes, dict[str, str]]:
            async with _raw_client(app) as client:
                response = await client.get("/.well-known/oauth-protected-resource")
                return response.status_code, response.content, dict(response.headers)

        status, raw, headers = _drive(
            corpus, issuer, _serve_auth(issuer=configured), _body
        )
        assert status == 200
        assert f'"authorization_servers":["{configured}"]'.encode() in raw
        assert json.loads(raw)["authorization_servers"][0].encode() == configured.encode()
        assert headers["content-length"] == str(len(raw))
        assert headers["cache-control"] == "public, max-age=3600"

    def test_a_cors_preflight_is_not_rewritten(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        """The OPTIONS answer is not JSON and must reach the client intact."""

        async def _body(app: Any) -> tuple[int, bytes]:
            async with _raw_client(app) as client:
                response = await client.options(
                    "/.well-known/oauth-protected-resource",
                    headers={
                        "Origin": "https://client.example.invalid",
                        "Access-Control-Request-Method": "GET",
                    },
                )
                return response.status_code, response.content

        status, raw = _drive(corpus, issuer, _serve_auth(), _body)
        assert status == 200
        assert b"authorization_servers" not in raw

    def test_a_multi_chunk_body_is_rewritten_with_one_start_message(self) -> None:
        """A body streamed in several chunks is buffered, rewritten once, and
        sent behind exactly one ``http.response.start`` whose Content-Length
        matches the bytes actually sent."""
        from athenaeum.mcp_auth import build_auth_provider

        provider = build_auth_provider(_serve_auth())
        document = b'{"resource":"x","authorization_servers":["https://normalized.invalid/"]}'

        async def inner(scope: Any, receive: Any, send: Any) -> None:
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-length", str(len(document)).encode())],
                }
            )
            await send({"type": "http.response.body", "body": document[:10], "more_body": True})
            await send({"type": "http.response.body", "body": document[10:], "more_body": False})

        sent: list[dict[str, Any]] = []

        async def collect(message: dict[str, Any]) -> None:
            sent.append(message)

        wrapped = provider._with_raw_issuer(inner)  # type: ignore[attr-defined]
        asyncio.run(wrapped({"type": "http", "method": "GET"}, None, collect))

        starts = [m for m in sent if m["type"] == "http.response.start"]
        bodies = [m for m in sent if m["type"] == "http.response.body"]
        assert len(starts) == 1
        body = b"".join(m["body"] for m in bodies)
        assert json.loads(body)["authorization_servers"] == [ISSUER]
        assert dict(starts[0]["headers"])[b"content-length"] == str(len(body)).encode()

    @pytest.mark.parametrize("resource", [RESOURCE, RESOURCE + "/mcp"])
    def test_the_resource_field_is_unchanged_by_the_issuer_fix(
        self, corpus: Path, issuer: _FakeIssuer, resource: str
    ) -> None:
        """The pinned resource is still served as FastMCP rendered it: the
        canonical form of the configured pin, which for a path-less origin
        carries the slash a pydantic URL renders."""
        status, document = self._metadata(corpus, issuer, resource)
        assert status == 200
        assert document["resource"] == _serve_auth(resource=resource).resource
        assert document["resource"] == (resource + "/" if resource == RESOURCE else resource)

    def test_a_path_scoped_resource_answers_at_its_rfc_9728_path(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        """RFC 9728 §3.1 inserts the well-known segment between host and path,
        so the recommended ``https://host/mcp`` resource is served at
        ``/.well-known/oauth-protected-resource/mcp`` — not at the bare path.
        Both shapes are configurable, so both are covered."""
        status, document = self._metadata(corpus, issuer, RESOURCE + "/mcp")
        assert status == 200
        assert document["resource"] == RESOURCE + "/mcp"

    def test_the_metadata_resource_is_the_audience_the_verifier_enforces(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        """The two values a client has to agree on, asserted to be one value.

        A token minted for exactly what the metadata advertises must verify.
        This is the assertion that would have caught the trailing-slash
        divergence ``_canonical_resource_url`` exists for.
        """

        async def _body(app: Any) -> tuple[dict, int]:
            async with _raw_client(app) as client:
                document = (
                    await client.get("/.well-known/oauth-protected-resource")
                ).json()
                token = issuer.mint(aud=document["resource"])
                response = await client.post(
                    MCP_PATH,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json, text/event-stream",
                        "Content-Type": "application/json",
                    },
                    json=_INITIALIZE,
                )
                return document, response.status_code

        document, status = _drive(corpus, issuer, _serve_auth(), _body)
        assert status == 200, f"a token minted for {document['resource']!r} was rejected"


# ---------------------------------------------------------------------------
# AC4 — every rejection is a 401 whose challenge points at the metadata.
# ---------------------------------------------------------------------------


class TestEveryRejectionIsA401WithAChallenge:
    def _cases(self, issuer: _FakeIssuer) -> dict[str, str | None]:
        now = int(time.time())
        return {
            "missing-token": None,
            "bad-signature": issuer.mint(signer=issuer.other_signer),
            "expired": issuer.mint(exp=now - 60, iat=now - 600, nbf=now - 600),
            "wrong-issuer": issuer.mint(iss=OTHER_ISSUER),
            "wrong-audience": issuer.mint(aud=OTHER_RESOURCE + "/"),
            "identity-not-allowlisted": issuer.mint(sub=STRANGER_SUBJECT),
        }

    @pytest.mark.parametrize(
        "case",
        [
            "missing-token",
            "bad-signature",
            "expired",
            "wrong-issuer",
            "wrong-audience",
            "identity-not-allowlisted",
        ],
    )
    def test_each_rejection_is_401_naming_the_metadata_url(
        self, case: str, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        token = self._cases(issuer)[case]
        status, challenge = _post_initialize(corpus, issuer, _serve_auth(), token)
        assert status == 401, f"{case} was not refused"
        assert challenge.startswith("Bearer"), f"{case}: no bearer challenge"
        expected = f'resource_metadata="{RESOURCE}/.well-known/oauth-protected-resource"'
        assert expected in challenge, f"{case}: challenge was {challenge!r}"

    def test_the_metadata_url_the_challenge_names_actually_answers(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        """The positive control for the assertion above.

        A challenge that advertised a 404 would satisfy a substring check and
        leave a real client unable to discover anything.
        """

        async def _body(app: Any) -> int:
            async with _raw_client(app) as client:
                return (
                    await client.get("/.well-known/oauth-protected-resource")
                ).status_code

        assert _drive(corpus, issuer, _serve_auth(), _body) == 200

    def test_an_identity_rejection_is_indistinguishable_from_a_bad_token(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        """A 403 here would turn the endpoint into an allowlist oracle.

        A caller able to tell "signature fine, you are not on the list" from
        "signature wrong" can enumerate which subjects the operator trusts.
        """
        stranger = _post_initialize(
            corpus, issuer, _serve_auth(), issuer.mint(sub=STRANGER_SUBJECT)
        )
        forged = _post_initialize(
            corpus, issuer, _serve_auth(), issuer.mint(signer=issuer.other_signer)
        )
        assert stranger == forged

    def test_an_allowlisted_verified_email_is_accepted(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        status, _ = _post_initialize(
            corpus,
            issuer,
            _serve_auth(),
            issuer.mint(
                sub="auth0|unknown-id", email=ALLOWED_EMAIL.upper(), email_verified=True
            ),
        )
        assert status == 200

    def test_a_mixed_case_subject_matches_a_lower_case_allowlist_entry(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        """Regression, caught in review of PR athenaeum#2420.

        `normalize_identities` case-folds the allowlist, so the claim side has
        to fold too or an issuer that mints a mixed-case subject identifier
        (Okta's user ids are the live example) can never be allowlisted at
        all: every valid token from it answers 401 for a reason visible
        nowhere in the operator's configuration. Both directions are covered,
        because folding only one side is exactly the defect.
        """
        status, _ = _post_initialize(
            corpus, issuer, _serve_auth(), issuer.mint(sub=ALLOWED_SUBJECT.upper())
        )
        assert status == 200

    def test_a_lower_case_subject_matches_a_mixed_case_allowlist_entry(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        mixed = _serve_auth(allowed_identities=frozenset({"OKTA|00U1a2B3c4D5e6F7g8H9"}))
        status, _ = _post_initialize(
            corpus, issuer, mixed, issuer.mint(sub="okta|00u1a2b3c4d5e6f7g8h9")
        )
        assert status == 200

    def test_case_folding_does_not_widen_to_a_different_subject(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        """The negative control for the two above.

        Folding both sides must make the comparison case-insensitive, not
        permissive: a subject that differs by more than capitalisation is
        still refused.
        """
        status, _ = _post_initialize(
            corpus, issuer, _serve_auth(), issuer.mint(sub=STRANGER_SUBJECT.upper())
        )
        assert status == 401

    def test_an_unverified_email_is_not_accepted(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        """An unverified address is self-asserted at sign-up.

        Honouring it would let anyone who can register with the issuer claim
        an allowlisted operator's address.
        """
        status, _ = _post_initialize(
            corpus,
            issuer,
            _serve_auth(),
            issuer.mint(sub="auth0|unknown-id", email=ALLOWED_EMAIL, email_verified=False),
        )
        assert status == 401

    def test_the_key_set_was_really_fetched_through_the_in_process_transport(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        """The positive control for every signature assertion in this file.

        If the verifier never consulted the published key set, "bad signature
        is refused" would hold for the wrong reason and "good signature is
        accepted" could not hold at all.
        """
        before = len(issuer.fetches)
        status, _ = _post_initialize(corpus, issuer, _serve_auth(), issuer.mint())
        assert status == 200
        assert issuer.fetches[before:], "the JWKS endpoint was never called"
        assert all(path.endswith(JWKS_PATH) for path in issuer.fetches[before:])

    def test_a_token_for_an_unknown_key_id_is_refused(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        status, _ = _post_initialize(
            corpus, issuer, _serve_auth(), issuer.mint(kid="not-published")
        )
        assert status == 401


# ---------------------------------------------------------------------------
# AC5 — a valid allowlisted token reaches the tool surface.
# ---------------------------------------------------------------------------


class TestAValidTokenReachesTheToolSurface:
    def test_tools_list_answers_200_with_the_full_tool_set(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        async def _body(app: Any) -> list[str]:
            async with _mcp_client(app, issuer.mint()) as client:
                return sorted(tool.name for tool in await client.list_tools())

        names = _drive(corpus, issuer, _serve_auth(), _body)
        assert READ_TOOL in names
        assert WRITE_TOOL in names

    def test_the_served_tool_set_is_the_same_one_an_unauthenticated_server_serves(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        """Auth narrows WHO may call, never WHAT is served.

        Hiding a tool a caller's scope cannot reach would make ``tools/list``
        a function of the token, which both breaks a client's cached tool set
        on a scope change and leaks the scope split into discovery.
        """
        import asyncio as _asyncio

        from athenaeum.mcp_server import create_server

        plain = create_server(
            raw_root=corpus / "raw",
            wiki_root=corpus / "wiki",
            cache_dir=corpus.parent / "cache",
        )
        expected = sorted(tool.name for tool in _asyncio.run(plain.list_tools()))

        async def _body(app: Any) -> list[str]:
            async with _mcp_client(app, issuer.mint()) as client:
                return sorted(tool.name for tool in await client.list_tools())

        assert _drive(corpus, issuer, _serve_auth(), _body) == expected

    def test_no_tool_gained_a_parameter_from_the_auth_pins(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        """The pins are ``create_server`` arguments, so there is nothing a
        caller could pass to widen its own identity, audience or scope. The
        scope guard wraps each tool with ``functools.wraps``, which is what
        keeps the served input schema the wrapped function's own."""
        import asyncio as _asyncio

        from athenaeum.mcp_server import create_server

        def _schemas(server: Any) -> dict[str, Any]:
            return {
                tool.name: tool.parameters
                for tool in _asyncio.run(server.list_tools())
            }

        plain = _schemas(
            create_server(
                raw_root=corpus / "raw",
                wiki_root=corpus / "wiki",
                cache_dir=corpus.parent / "cache",
            )
        )
        guarded = _schemas(
            create_server(
                raw_root=corpus / "raw",
                wiki_root=corpus / "wiki",
                cache_dir=corpus.parent / "cache",
                serve_auth=_serve_auth(),
                jwks_http_client=issuer.http_client(),
            )
        )
        assert plain, "sanity: the unauthenticated server registered no tools"
        assert guarded == plain
        forbidden = {
            "serve_auth",
            "token",
            "access_token",
            "bearer",
            "identity",
            "subject",
            "sub",
            "audience",
            "resource",
            "issuer",
            "scope",
            "scopes",
            "allowed_identities",
        }
        for name, schema in guarded.items():
            properties = set((schema or {}).get("properties") or {})
            assert not (properties & forbidden), f"{name} exposes {properties & forbidden}"


# ---------------------------------------------------------------------------
# AC6 — the read scope and the write scope are different scopes.
# ---------------------------------------------------------------------------


class TestScopesSplitReadFromWrite:
    def _call(
        self,
        corpus: Path,
        issuer: _FakeIssuer,
        token: str,
        tool: str,
        arguments: dict,
        auth: Any = None,
    ) -> tuple[str | None, str | None]:
        """Call *tool*; return (text, error-message) with exactly one set."""

        async def _body(app: Any) -> tuple[str | None, str | None]:
            async with _mcp_client(app, token) as client:
                try:
                    result = await client.call_tool(tool, arguments)
                except Exception as exc:  # noqa: BLE001 - the refusal IS the result
                    return None, str(exc)
                return result.content[0].text, None

        return _drive(corpus, issuer, auth or _serve_auth(), _body)

    def test_a_read_scoped_token_can_recall(self, corpus: Path, issuer: _FakeIssuer) -> None:
        text, error = self._call(
            corpus,
            issuer,
            issuer.mint(scope=DEFAULT_READ_SCOPE),
            READ_TOOL,
            {"query": "widget"},
        )
        assert error is None, error
        assert text is not None and "widget" in text.lower()

    def test_a_read_scoped_token_cannot_remember(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        text, error = self._call(
            corpus,
            issuer,
            issuer.mint(scope=DEFAULT_READ_SCOPE),
            WRITE_TOOL,
            {"content": "A new fact about widgets."},
        )
        assert text is None, f"remember succeeded without the write scope: {text!r}"
        assert error is not None
        assert DEFAULT_WRITE_SCOPE in error
        assert WRITE_TOOL in error
        # And nothing was written -- the refusal precedes the append, so a
        # guard that raised AFTER the write would fail here rather than pass
        # on the error message alone.
        assert not list((corpus / "raw").rglob("*.md"))

    def test_a_write_scoped_token_can_remember(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        text, error = self._call(
            corpus,
            issuer,
            issuer.mint(scope=f"{DEFAULT_READ_SCOPE} {DEFAULT_WRITE_SCOPE}"),
            WRITE_TOOL,
            {"content": "A new fact about widgets."},
        )
        assert error is None, error
        assert text is not None
        assert list((corpus / "raw").rglob("*.md")), "remember wrote nothing"

    def test_a_scopeless_token_can_call_nothing(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        """Fail closed. A token the issuer granted no athenaeum scope to
        authenticates but authorizes nothing."""
        for tool, arguments in ((READ_TOOL, {"query": "widget"}), (WRITE_TOOL, {"content": "x"})):
            text, error = self._call(
                corpus, issuer, issuer.mint(scope=""), tool, arguments
            )
            assert text is None, f"{tool} ran with no scope"
            assert error is not None

    def test_renamed_scopes_are_the_ones_enforced(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        """``serve.auth.scopes`` renames what the token must carry, and the
        default names stop working — otherwise the rename would only ADD an
        accepted scope."""
        auth = _serve_auth(scopes={"read": "mem:read", "write": "mem:write"})
        text, error = self._call(
            corpus, issuer, issuer.mint(scope="mem:read"), READ_TOOL, {"query": "widget"}, auth
        )
        assert error is None, error
        assert text is not None
        text, error = self._call(
            corpus,
            issuer,
            issuer.mint(scope=DEFAULT_READ_SCOPE),
            READ_TOOL,
            {"query": "widget"},
            auth,
        )
        assert text is None, "the default read scope still worked after a rename"
        assert error is not None and "mem:read" in error

    def test_every_registered_tool_is_scope_gated(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        """Derived, not hand-listed: a tool added to ``create_server``
        tomorrow is covered with no edit here. A scopeless token must be
        refused by EVERY tool, which is only true if the gate is applied at
        registration rather than written into individual tool bodies.

        Arguments are synthesized from each tool's own input schema, because
        FastMCP validates arguments BEFORE the body runs: calling every tool
        with ``{}`` would produce a validation error for each tool that has a
        required parameter and prove nothing about the scope gate.
        """

        async def _body(app: Any) -> dict[str, bool]:
            out: dict[str, bool] = {}
            async with _mcp_client(app, issuer.mint(scope="")) as client:
                for tool in await client.list_tools():
                    try:
                        await client.call_tool(tool.name, _dummy_arguments(tool))
                    except Exception as exc:  # noqa: BLE001
                        out[tool.name] = "OAuth scope" in str(exc)
                    else:
                        out[tool.name] = False
            return out

        gated = _drive(corpus, issuer, _serve_auth(), _body)
        assert gated, "sanity: no tools were probed"
        assert all(gated.values()), f"ungated: {sorted(k for k, v in gated.items() if not v)}"


# ---------------------------------------------------------------------------
# AC7 — the audience is the process pin. This is the heart of the item.
# ---------------------------------------------------------------------------


#: Every shape a token could plausibly use to assert a broader role. None of
#: these may change what `recall` returns: the read scope is
#: `create_server(caller_audience=...)`, pinned by the operator's own serve
#: invocation, and nothing reads a claim into it.
BROADER_ROLE_CLAIMS: dict[str, Any] = {
    "audience": "owner",
    "aud": [RESOURCE + "/", "owner", "confidential"],
    "roles": ["owner", "confidential", "personal"],
    "groups": ["owners"],
    "athenaeum_audience": "owner",
    "caller_audience": None,
    "access": "confidential",
    "resource": "https://everything.example.invalid",
}

OPEN_PAGE_MARKER = "The open widget record"
CONFIDENTIAL_PAGE_MARKER = "Secret Widget"


class TestTheAudienceIsTheProcessPinNotAClaim:
    #: The process pin: a restricted reader. `access: open` pages are
    #: readable, everything else is withheld.
    PINNED_AUDIENCE = {"ops"}

    def _recall(
        self, corpus: Path, issuer: _FakeIssuer, token: str, **server_kwargs: Any
    ) -> str:
        async def _body(app: Any) -> str:
            async with _mcp_client(app, token) as client:
                result = await client.call_tool(READ_TOOL, {"query": "widget", "top_k": 10})
                return result.content[0].text

        return _drive(corpus, issuer, _serve_auth(), _body, **server_kwargs)

    def test_an_owner_scoped_server_does_return_the_confidential_page(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        """The positive control, without which every assertion below could
        pass against a ``recall`` that returns nothing at all."""
        rendered = self._recall(corpus, issuer, issuer.mint(), caller_audience=None)
        assert OPEN_PAGE_MARKER in rendered
        assert CONFIDENTIAL_PAGE_MARKER in rendered

    def test_the_pinned_server_withholds_it_from_a_plain_token(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        rendered = self._recall(
            corpus, issuer, issuer.mint(), caller_audience=self.PINNED_AUDIENCE
        )
        assert OPEN_PAGE_MARKER in rendered
        assert CONFIDENTIAL_PAGE_MARKER not in rendered

    def test_a_token_claiming_a_broader_role_still_gets_the_process_pin(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        """THE acceptance criterion.

        A token carrying eight different audience-like claims — including an
        ``aud`` whose list names ``owner`` alongside the correct resource, and
        a ``caller_audience`` claim named after the server argument itself —
        gets byte-identical ``recall`` output to a bare token. Asserted as
        EQUALITY against the plain-token result, not merely as "the secret is
        absent": a change that broke recall for both would be caught by the
        content assertions, and a change that threaded any one claim through
        would be caught by the equality.
        """
        plain = self._recall(
            corpus, issuer, issuer.mint(), caller_audience=self.PINNED_AUDIENCE
        )
        claiming = self._recall(
            corpus,
            issuer,
            issuer.mint(**BROADER_ROLE_CLAIMS),
            caller_audience=self.PINNED_AUDIENCE,
        )
        assert claiming == plain
        assert OPEN_PAGE_MARKER in claiming, "sanity: recall returned nothing at all"
        assert CONFIDENTIAL_PAGE_MARKER not in claiming

    @pytest.mark.parametrize("claim", sorted(BROADER_ROLE_CLAIMS))
    def test_no_single_claim_widens_the_read_scope(
        self, claim: str, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        """Each claim on its own, so a failure names the one that got through
        rather than only reporting that the bundle did."""
        rendered = self._recall(
            corpus,
            issuer,
            issuer.mint(**{claim: BROADER_ROLE_CLAIMS[claim]}),
            caller_audience=self.PINNED_AUDIENCE,
        )
        assert CONFIDENTIAL_PAGE_MARKER not in rendered, f"{claim} widened the read scope"

    def test_a_claim_cannot_widen_the_verified_audience_either(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        """The other sense of "audience": a token whose ``aud`` names a
        DIFFERENT resource is refused even when it also names a broad role.
        An implementation that read the audience out of the claim would accept
        this."""
        status, _ = _post_initialize(
            corpus,
            issuer,
            _serve_auth(),
            issuer.mint(aud=[OTHER_RESOURCE + "/", "owner"], audience="owner"),
        )
        assert status == 401

    def test_the_pinned_audience_is_not_a_tool_parameter(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        """Nothing to pass, so nothing to forge."""

        async def _body(app: Any) -> set[str]:
            async with _mcp_client(app, issuer.mint()) as client:
                tools = await client.list_tools()
                return {
                    name
                    for tool in tools
                    for name in (
                        (getattr(tool, "input_schema", None) or {}).get("properties") or {}
                    )
                }

        properties = _drive(corpus, issuer, _serve_auth(), _body)
        assert properties, "sanity: no input schemas were read"
        assert not ({"audience", "caller_audience", "access", "roles"} & properties)


# ---------------------------------------------------------------------------
# AC8 — tokens never reach logs.
# ---------------------------------------------------------------------------


class _Capture(logging.Handler):
    """Collects fully-formatted records from every logger in the tree."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
        except Exception:  # noqa: BLE001 - a broken format must not hide a leak
            self.lines.append(repr(record.msg) + repr(record.args))


class TestTokensNeverReachLogs:
    #: Every logger a token could plausibly pass through, forced to DEBUG and
    #: made to propagate. FastMCP installs its own handler and sets
    #: `propagate = False`, so capturing the root logger alone would miss the
    #: verifier's own rejection lines -- the most likely place for a token to
    #: leak, and the one this test exists for.
    LOGGERS = (
        "",
        "athenaeum",
        "athenaeum.mcp_auth",
        "fastmcp",
        "fastmcp.server.auth.providers.jwt",
        "mcp",
        "mcp.server.auth",
    )

    def _capture(
        self, corpus: Path, issuer: _FakeIssuer, tokens: dict[str, str]
    ) -> tuple[list[str], dict[str, int]]:
        capture = _Capture()
        capture.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
        restore: list[tuple[logging.Logger, int, bool, list]] = []
        for name in self.LOGGERS:
            target = logging.getLogger(name)
            restore.append((target, target.level, target.propagate, list(target.handlers)))
            target.setLevel(logging.DEBUG)
            target.propagate = True
            target.addHandler(capture)
        try:
            statuses = {
                label: _post_initialize(corpus, issuer, _serve_auth(), token)[0]
                for label, token in tokens.items()
            }
        finally:
            for target, level, propagate, handlers in restore:
                target.removeHandler(capture)
                target.setLevel(level)
                target.propagate = propagate
                target.handlers = handlers
        return capture.lines, statuses

    def test_no_token_substring_appears_across_a_failed_and_a_successful_call(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        """Both outcomes in one capture, because they log on different paths.

        A rejection is logged by the verifier (or, for the allowlist, by
        ``athenaeum.mcp_auth``); an acceptance is logged by the transport. The
        whole token is checked, and so is each of its three segments
        separately: the SIGNATURE segment alone is enough to replay a token
        whose header and payload are public, and a line that printed only the
        payload would still disclose every claim.
        """
        tokens = {
            "rejected": issuer.mint(sub=STRANGER_SUBJECT),
            "accepted": issuer.mint(),
        }
        lines, statuses = self._capture(corpus, issuer, tokens)
        assert statuses == {"rejected": 401, "accepted": 200}, statuses
        assert lines, "sanity: nothing was logged at all, so nothing was checked"
        haystack = "\n".join(lines)
        for label, token in tokens.items():
            assert token not in haystack, f"{label}: the whole token was logged"
            for index, segment in enumerate(token.split(".")):
                assert segment not in haystack, (
                    f"{label}: JWT segment {index} appeared in a log line"
                )

    def test_the_rejection_was_logged_at_all(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        """The positive control for the test above.

        "No token in the logs" is trivially true of a server that logs
        nothing. athenaeum's own allowlist refusal must be visible to an
        operator -- naming the subject it refused, which is a value from their
        own configuration, and nothing else.
        """
        lines, _ = self._capture(
            corpus, issuer, {"rejected": issuer.mint(sub=STRANGER_SUBJECT)}
        )
        refusals = [line for line in lines if "allowed_identities" in line]
        assert refusals, "the allowlist refusal was not logged"
        assert any(STRANGER_SUBJECT in line for line in refusals)

    def test_the_scope_refusal_message_carries_no_credential(self) -> None:
        """The message a client is handed back, not a log line.

        It is also an error channel the caller's own logs capture, so it names
        the tool and the scope and nothing about the token.
        """
        from athenaeum.mcp_server import _insufficient_scope_message

        message = _insufficient_scope_message(WRITE_TOOL, DEFAULT_WRITE_SCOPE)
        assert WRITE_TOOL in message
        assert DEFAULT_WRITE_SCOPE in message
        for leak in (ALLOWED_SUBJECT, ALLOWED_EMAIL, "Bearer", "eyJ"):
            assert leak not in message


# ---------------------------------------------------------------------------
# AC9 — docs/modules/mcp.md.
# ---------------------------------------------------------------------------


class TestDocsDocumentTheResourceServer:
    PAGE = Path(__file__).resolve().parent.parent / "docs" / "modules" / "mcp.md"

    def test_every_setting_is_documented(self) -> None:
        text = self.PAGE.read_text(encoding="utf-8")
        for setting in (
            "serve.auth.issuer",
            "serve.auth.resource",
            "serve.auth.allowed_identities",
            "serve.auth.scopes",
            "ATHENAEUM_SERVE_AUTH_ISSUER",
            "ATHENAEUM_SERVE_AUTH_RESOURCE",
            "ATHENAEUM_SERVE_AUTH_ALLOWED_IDENTITIES",
            "ATHENAEUM_SERVE_AUTH_SCOPES",
            "--auth-issuer",
            "--auth-resource",
            "--auth-allowed-identities",
            "--auth-scopes",
        ):
            assert setting in text, f"{setting} is undocumented"

    def test_the_resource_server_only_boundary_is_stated(self) -> None:
        """Not a keyword check on "OAuth": the page has to say which half of
        the protocol athenaeum implements, because the other half is what an
        operator would otherwise expect it to provide."""
        text = self.PAGE.read_text(encoding="utf-8")
        assert "resource server only" in text.lower()
        for refusal in (
            "never issues",
            "/.well-known/oauth-protected-resource",
            JWKS_PATH,
        ):
            assert refusal in text, f"{refusal!r} is not documented"

    def test_the_audience_pin_is_documented_as_unforgeable(self) -> None:
        text = self.PAGE.read_text(encoding="utf-8").lower()
        assert "one process" in text
        assert "never" in text and "token claim" in text


# ---------------------------------------------------------------------------
# athenaeum#2599 — exp is required, nbf/iat are enforced, lifetime is capped.
# ---------------------------------------------------------------------------


def _without_exp(issuer: _FakeIssuer) -> str:
    """A token minted with the ``exp`` claim absent (not merely ``None``)."""
    from joserfc import jwt
    from joserfc.jws import JWSRegistry

    now = int(time.time())
    payload = {
        "iss": ISSUER,
        "aud": RESOURCE + "/",
        "sub": ALLOWED_SUBJECT,
        "iat": now,
        "nbf": now,
    }
    return jwt.encode(
        {"alg": SIGNING_ALGORITHM, "kid": SIGNING_KID},
        payload,
        issuer.signer,
        registry=JWSRegistry(algorithms=[SIGNING_ALGORITHM]),
    )


def _without_iat(issuer: _FakeIssuer, *, exp_in: int) -> str:
    from joserfc import jwt
    from joserfc.jws import JWSRegistry

    payload = {
        "iss": ISSUER,
        "aud": RESOURCE + "/",
        "sub": ALLOWED_SUBJECT,
        "exp": int(time.time()) + exp_in,
    }
    return jwt.encode(
        {"alg": SIGNING_ALGORITHM, "kid": SIGNING_KID},
        payload,
        issuer.signer,
        registry=JWSRegistry(algorithms=[SIGNING_ALGORITHM]),
    )


class TestTimeClaimsAreEnforced:
    """End to end through the ASGI app: each refusal is a 401 with a challenge."""

    def _status(self, corpus: Path, issuer: _FakeIssuer, token: str, **auth: Any) -> int:
        status, challenge = _post_initialize(corpus, issuer, _serve_auth(**auth), token)
        if status == 401:
            assert challenge.startswith("Bearer")
        return status

    def test_missing_exp_is_refused(self, corpus: Path, issuer: _FakeIssuer) -> None:
        assert self._status(corpus, issuer, _without_exp(issuer)) == 401

    def test_future_nbf_is_refused(self, corpus: Path, issuer: _FakeIssuer) -> None:
        token = issuer.mint(nbf=int(time.time()) + 3600, exp=int(time.time()) + 7200)
        assert self._status(corpus, issuer, token) == 401

    def test_future_iat_is_refused(self, corpus: Path, issuer: _FakeIssuer) -> None:
        token = issuer.mint(iat=int(time.time()) + 3600, nbf=int(time.time()) - 1)
        assert self._status(corpus, issuer, token) == 401

    def test_nbf_within_leeway_is_accepted(self, corpus: Path, issuer: _FakeIssuer) -> None:
        token = issuer.mint(nbf=int(time.time()) + 30, iat=int(time.time()) + 30)
        assert self._status(corpus, issuer, token) == 200

    def test_nbf_beyond_a_configured_smaller_leeway_is_refused(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        token = issuer.mint(nbf=int(time.time()) + 30)
        assert self._status(corpus, issuer, token, clock_leeway=5) == 401

    def test_lifetime_over_the_cap_is_refused(self, corpus: Path, issuer: _FakeIssuer) -> None:
        now = int(time.time())
        token = issuer.mint(iat=now, nbf=now, exp=now + 3600)
        assert self._status(corpus, issuer, token, max_token_lifetime=600) == 401

    def test_lifetime_under_the_cap_is_accepted(self, corpus: Path, issuer: _FakeIssuer) -> None:
        now = int(time.time())
        token = issuer.mint(iat=now, nbf=now, exp=now + 300)
        assert self._status(corpus, issuer, token, max_token_lifetime=600) == 200

    def test_no_iat_with_distant_exp_is_refused_under_a_cap(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        token = _without_iat(issuer, exp_in=3600)
        assert self._status(corpus, issuer, token, max_token_lifetime=600) == 401

    def test_no_iat_with_near_exp_is_accepted_under_a_cap(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        token = _without_iat(issuer, exp_in=300)
        assert self._status(corpus, issuer, token, max_token_lifetime=600) == 200

    def test_a_long_lifetime_is_accepted_when_no_cap_is_set(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        now = int(time.time())
        token = issuer.mint(iat=now, nbf=now, exp=now + 10 * 365 * 86400)
        assert self._status(corpus, issuer, token) == 200

    def test_expired_is_still_refused(self, corpus: Path, issuer: _FakeIssuer) -> None:
        now = int(time.time())
        token = issuer.mint(exp=now - 60, iat=now - 600, nbf=now - 600)
        assert self._status(corpus, issuer, token) == 401

    def test_refusal_is_logged_with_the_reason_and_never_the_token(
        self, corpus: Path, issuer: _FakeIssuer, caplog: pytest.LogCaptureFixture
    ) -> None:
        token = _without_exp(issuer)
        with caplog.at_level("WARNING", logger="athenaeum.mcp_auth"):
            assert self._status(corpus, issuer, token) == 401
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "missing_exp" in text
        assert token not in text
        assert token.split(".")[1] not in text


class TestCheckTimeClaims:
    """The pure function, with an explicit clock."""

    NOW = 1_000_000.0

    def _check(self, claims: dict[str, Any], **kw: Any) -> str | None:
        from athenaeum.serve_auth import check_time_claims

        return check_time_claims(claims, self.NOW, **kw)

    def test_valid_token_passes(self) -> None:
        assert self._check({"exp": self.NOW + 60, "iat": self.NOW, "nbf": self.NOW}) is None

    @pytest.mark.parametrize(
        ("claims", "expected"),
        [
            ({}, "missing_exp"),
            ({"exp": None}, "missing_exp"),
            ({"exp": "9999999999"}, "malformed_exp"),
            ({"exp": True}, "malformed_exp"),
            ({"exp": NOW - 1}, "expired"),
            ({"exp": NOW}, "expired"),
            ({"exp": NOW + 99, "nbf": NOW + 61}, "not_yet_valid"),
            ({"exp": NOW + 99, "iat": NOW + 61}, "issued_in_future"),
            ({"exp": NOW + 99, "nbf": "soon"}, "malformed_time_claim"),
        ],
    )
    def test_refusals(self, claims: dict[str, Any], expected: str) -> None:
        assert self._check(claims) == expected

    def test_leeway_boundary_is_inclusive(self) -> None:
        assert self._check({"exp": self.NOW + 99, "nbf": self.NOW + 60}) is None
        assert self._check({"exp": self.NOW + 99, "nbf": self.NOW + 60}, leeway=0) == (
            "not_yet_valid"
        )

    def test_lifetime_cap_uses_iat_when_present_else_now(self) -> None:
        over = {"exp": self.NOW + 700, "iat": self.NOW}
        assert self._check(over, max_lifetime=600) == "lifetime_exceeds_max"
        assert self._check({"exp": self.NOW + 600, "iat": self.NOW}, max_lifetime=600) is None
        # iat old, exp near: lifetime measured exp - iat, not exp - now.
        assert (
            self._check({"exp": self.NOW + 10, "iat": self.NOW - 1000}, max_lifetime=600)
            == "lifetime_exceeds_max"
        )
        assert self._check({"exp": self.NOW + 700}, max_lifetime=600) == "lifetime_exceeds_max"
        assert self._check({"exp": self.NOW + 500}, max_lifetime=600) is None


class TestTimeClaimSettings:
    YAML = {"serve": {"auth": {"max_token_lifetime": 900, "clock_leeway": 5}}}

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_SERVE_AUTH_MAX_TOKEN_LIFETIME", raising=False)
        monkeypatch.delenv("ATHENAEUM_SERVE_AUTH_CLOCK_LEEWAY", raising=False)

    def test_defaults(self) -> None:
        from athenaeum.config import (
            resolve_serve_auth_clock_leeway,
            resolve_serve_auth_max_token_lifetime,
        )

        assert resolve_serve_auth_max_token_lifetime(None) is None
        assert resolve_serve_auth_clock_leeway(None) == 60

    def test_yaml_env_cli_precedence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from athenaeum.config import (
            resolve_serve_auth_clock_leeway,
            resolve_serve_auth_max_token_lifetime,
        )

        assert resolve_serve_auth_max_token_lifetime(self.YAML) == 900
        assert resolve_serve_auth_clock_leeway(self.YAML) == 5
        monkeypatch.setenv("ATHENAEUM_SERVE_AUTH_MAX_TOKEN_LIFETIME", "120")
        monkeypatch.setenv("ATHENAEUM_SERVE_AUTH_CLOCK_LEEWAY", "0")
        assert resolve_serve_auth_max_token_lifetime(self.YAML) == 120
        assert resolve_serve_auth_clock_leeway(self.YAML) == 0
        assert resolve_serve_auth_max_token_lifetime(self.YAML, 30) == 30
        assert resolve_serve_auth_clock_leeway(self.YAML, 7) == 7

    def test_blank_env_falls_through_to_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from athenaeum.config import resolve_serve_auth_max_token_lifetime

        monkeypatch.setenv("ATHENAEUM_SERVE_AUTH_MAX_TOKEN_LIFETIME", "  ")
        assert resolve_serve_auth_max_token_lifetime(self.YAML) == 900

    @pytest.mark.parametrize("bad", [0, -5, True, 1.5, "abc"])
    def test_invalid_lifetime_is_refused(self, bad: Any) -> None:
        from athenaeum.config import resolve_serve_auth_max_token_lifetime
        from athenaeum.serve_auth import ServeAuthError

        with pytest.raises(ServeAuthError):
            resolve_serve_auth_max_token_lifetime({"serve": {"auth": {"max_token_lifetime": bad}}})

    @pytest.mark.parametrize("bad", [-1, True, 1.5, "abc"])
    def test_invalid_leeway_is_refused(self, bad: Any) -> None:
        from athenaeum.config import resolve_serve_auth_clock_leeway
        from athenaeum.serve_auth import ServeAuthError

        with pytest.raises(ServeAuthError):
            resolve_serve_auth_clock_leeway({"serve": {"auth": {"clock_leeway": bad}}})

    def test_build_serve_auth_carries_and_validates(self) -> None:
        from athenaeum.serve_auth import ServeAuthError

        built = _serve_auth(clock_leeway=3, max_token_lifetime=77)
        assert (built.clock_leeway, built.max_token_lifetime) == (3, 77)
        assert _serve_auth().clock_leeway == 60
        with pytest.raises(ServeAuthError):
            _serve_auth(max_token_lifetime=0)


# ---------------------------------------------------------------------------
# athenaeum#2601 — configurable / discovered JWKS location; kid-refetch throttle.
# ---------------------------------------------------------------------------


class TestJwksLocation:
    ISS = "https://id.example.net"
    PATH_ISS = "https://id.example.net/tenant-a"

    @staticmethod
    def _build(issuer_url: str = ISS, **kw: Any):
        return build_serve_auth(
            issuer=issuer_url,
            resource=RESOURCE,
            allowed_identities=frozenset({ALLOWED_SUBJECT}),
            **kw,
        )

    @staticmethod
    def _fetcher(docs: dict[str, Any]):
        calls: list[str] = []

        def fetch(url: str) -> Any:
            calls.append(url)
            return docs.get(url)

        fetch.calls = calls  # type: ignore[attr-defined]
        return fetch

    def test_default_is_derived_and_never_touches_the_network(self) -> None:
        built = self._build()
        assert built.jwks_uri == "https://id.example.net/.well-known/jwks.json"
        assert built.jwks_source == "derived"

    def test_configured_is_used_verbatim_and_skips_discovery(self) -> None:
        fetch = self._fetcher({})
        built = self._build(
            jwks_uri="https://keys.example.net/oauth2/jwks", discovery_fetcher=fetch
        )
        assert (built.jwks_uri, built.jwks_source) == (
            "https://keys.example.net/oauth2/jwks",
            "configured",
        )
        assert fetch.calls == []

    def test_configured_must_be_an_absolute_http_url(self) -> None:
        from athenaeum.serve_auth import ServeAuthError

        with pytest.raises(ServeAuthError):
            self._build(jwks_uri="keys.example.net/jwks")

    def test_discovered_from_rfc8414_first(self) -> None:
        fetch = self._fetcher(
            {
                self.ISS + "/.well-known/oauth-authorization-server": {
                    "issuer": self.ISS,
                    "jwks_uri": "https://id.example.net/oauth2/jwks",
                }
            }
        )
        built = self._build(discovery_fetcher=fetch)
        assert (built.jwks_uri, built.jwks_source) == (
            "https://id.example.net/oauth2/jwks",
            "discovered",
        )
        assert fetch.calls == [self.ISS + "/.well-known/oauth-authorization-server"]

    def test_falls_through_to_openid_configuration(self) -> None:
        fetch = self._fetcher(
            {
                self.ISS + "/.well-known/openid-configuration": {
                    "issuer": self.ISS,
                    "jwks_uri": "https://id.example.net/keys",
                }
            }
        )
        built = self._build(discovery_fetcher=fetch)
        assert built.jwks_source == "discovered"
        assert len(fetch.calls) == 2

    def test_path_scoped_issuer_uses_rfc8414_insertion_and_oidc_append(self) -> None:
        from athenaeum.serve_auth import discovery_urls

        assert discovery_urls(self.PATH_ISS) == [
            "https://id.example.net/.well-known/oauth-authorization-server/tenant-a",
            "https://id.example.net/tenant-a/.well-known/openid-configuration",
        ]

    @pytest.mark.parametrize(
        "named", ["https://id.example.net/", "https://other.example.net", None]
    )
    def test_issuer_mismatch_refuses_to_start(self, named: Any) -> None:
        from athenaeum.serve_auth import ServeAuthError

        fetch = self._fetcher(
            {
                self.ISS + "/.well-known/oauth-authorization-server": {
                    "issuer": named,
                    "jwks_uri": "https://id.example.net/oauth2/jwks",
                }
            }
        )
        with pytest.raises(ServeAuthError, match="does not equal"):
            self._build(discovery_fetcher=fetch)

    @pytest.mark.parametrize(
        "docs",
        [{}, {"/.well-known/oauth-authorization-server": {"issuer": ISS}}],
    )
    def test_unavailable_or_keyless_discovery_falls_back_to_derived(self, docs: dict) -> None:
        fetch = self._fetcher({self.ISS + k: v for k, v in docs.items()})
        built = self._build(discovery_fetcher=fetch)
        assert (built.jwks_uri, built.jwks_source) == (
            "https://id.example.net/.well-known/jwks.json",
            "derived",
        )

    def test_real_fetcher_returns_none_on_a_malformed_http_response(self) -> None:
        import socket
        import threading

        from athenaeum.serve_auth import fetch_discovery_document

        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)

        def _serve() -> None:
            conn, _ = server.accept()
            conn.recv(1024)
            conn.sendall(b"not http at all\r\n\r\n")
            conn.close()

        thread = threading.Thread(target=_serve, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.getsockname()[1]}/.well-known/x"
            assert fetch_discovery_document(url) is None
        finally:
            server.close()

    def test_real_fetcher_returns_none_when_unreachable(self) -> None:
        from athenaeum.serve_auth import fetch_discovery_document

        assert fetch_discovery_document("http://127.0.0.1:9/.well-known/x") is None


class TestJwksRefetchThrottle:
    def _fetch_count(self, issuer: _FakeIssuer, before: int) -> int:
        return len(issuer.fetches) - before

    def test_n_random_kid_tokens_cause_at_most_one_fetch(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        before = len(issuer.fetches)

        async def _body(app: Any) -> list[int]:
            statuses = []
            async with _raw_client(app) as client:
                for i in range(12):
                    token = issuer.mint(kid=f"random-{i}")
                    r = await client.post(
                        MCP_PATH,
                        headers={
                            "Accept": "application/json, text/event-stream",
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {token}",
                        },
                        json=_INITIALIZE,
                    )
                    statuses.append(r.status_code)
            return statuses

        statuses = _drive(corpus, issuer, _serve_auth(), _body)
        assert statuses == [401] * 12
        assert self._fetch_count(issuer, before) <= 1

    def test_legitimate_traffic_and_rotation_after_the_interval_still_work(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        before = len(issuer.fetches)

        async def _post(client: Any, token: str) -> int:
            r = await client.post(
                MCP_PATH,
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                },
                json=_INITIALIZE,
            )
            return r.status_code

        async def _body(app: Any) -> tuple[int, int, int]:
            async with _raw_client(app) as client:
                first = await _post(client, issuer.mint())  # cold cache: one fetch
                cached = await _post(client, issuer.mint())  # cache hit: no fetch
                unknown = await _post(client, issuer.mint(kid="unknown-kid"))  # throttled
                return first, cached, unknown

        first, cached, unknown = _drive(
            corpus, issuer, _serve_auth(jwks_refetch_interval=3600), _body
        )
        assert (first, cached, unknown) == (200, 200, 401)
        assert self._fetch_count(issuer, before) == 1

    def test_unknown_kid_refetches_again_once_the_interval_has_passed(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        before = len(issuer.fetches)

        async def _body(app: Any) -> None:
            async with _raw_client(app) as client:
                for kid in ("a-1", "a-2"):
                    await client.post(
                        MCP_PATH,
                        headers={
                            "Accept": "application/json, text/event-stream",
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {issuer.mint(kid=kid)}",
                        },
                        json=_INITIALIZE,
                    )
                await asyncio.sleep(1.2)
                await client.post(
                    MCP_PATH,
                    headers={
                        "Accept": "application/json, text/event-stream",
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {issuer.mint(kid='a-3')}",
                    },
                    json=_INITIALIZE,
                )

        _drive(corpus, issuer, _serve_auth(jwks_refetch_interval=1), _body)
        assert self._fetch_count(issuer, before) == 2


class TestJwksSettings:
    YAML = {
        "serve": {"auth": {"jwks_uri": "https://k.example.net/jwks", "jwks_refetch_interval": 5}}
    }

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_SERVE_AUTH_JWKS_URI", raising=False)
        monkeypatch.delenv("ATHENAEUM_SERVE_AUTH_JWKS_REFETCH_INTERVAL", raising=False)

    def test_defaults_and_precedence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from athenaeum.config import (
            resolve_serve_auth_jwks_refetch_interval,
            resolve_serve_auth_jwks_uri,
        )

        assert resolve_serve_auth_jwks_uri(None) is None
        assert resolve_serve_auth_jwks_refetch_interval(None) == 60
        assert resolve_serve_auth_jwks_uri(self.YAML) == "https://k.example.net/jwks"
        assert resolve_serve_auth_jwks_refetch_interval(self.YAML) == 5
        monkeypatch.setenv("ATHENAEUM_SERVE_AUTH_JWKS_URI", "https://env.example.net/jwks")
        monkeypatch.setenv("ATHENAEUM_SERVE_AUTH_JWKS_REFETCH_INTERVAL", "0")
        assert resolve_serve_auth_jwks_uri(self.YAML) == "https://env.example.net/jwks"
        assert resolve_serve_auth_jwks_refetch_interval(self.YAML) == 0
        assert resolve_serve_auth_jwks_uri(self.YAML, "https://cli.example.net/j") == (
            "https://cli.example.net/j"
        )

    def test_invalid_values_are_refused(self) -> None:
        from athenaeum.config import (
            resolve_serve_auth_jwks_refetch_interval,
            resolve_serve_auth_jwks_uri,
        )
        from athenaeum.serve_auth import ServeAuthError

        with pytest.raises(ServeAuthError):
            resolve_serve_auth_jwks_uri({"serve": {"auth": {"jwks_uri": 5}}})
        with pytest.raises(ServeAuthError):
            resolve_serve_auth_jwks_refetch_interval(
                {"serve": {"auth": {"jwks_refetch_interval": -1}}}
            )
