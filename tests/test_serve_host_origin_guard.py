# SPDX-License-Identifier: Apache-2.0
"""Host/Origin guard on ``athenaeum serve --transport http`` (athenaeum#2605).

Unauthenticated loopback serving turns FastMCP's strict Host/Origin guard on
(DNS-rebinding protection); an authenticated server leaves it off so tunnel
ingress, which carries the public hostname, is not answered with 421. The
behavioural tests build the real ASGI app with the kwargs ``cmd_serve`` itself
computes (``http_host_origin_kwargs``), so they exercise the shipped value.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from athenaeum._cmd_serve import http_host_origin_kwargs
from athenaeum.cli import main
from athenaeum.mcp_server import create_server
from athenaeum.serve_auth import build_serve_auth

MCP_PATH = "/mcp"
PUBLIC_HOST = "memory.example.invalid"
ISSUER = "https://issuer.example.invalid"
RESOURCE = f"https://{PUBLIC_HOST}"
INIT_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    knowledge = tmp_path / "knowledge"
    (knowledge / "wiki").mkdir(parents=True)
    (knowledge / "raw").mkdir()
    (knowledge / "athenaeum.yaml").write_text("search_backend: keyword\n", encoding="utf-8")
    return knowledge


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
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


def _post(corpus: Path, *, authenticated: bool, headers: dict[str, str]) -> int:
    """POST an MCP initialize to the real app and return the status code."""
    serve_auth = (
        build_serve_auth(
            issuer=ISSUER, resource=RESOURCE, allowed_identities=frozenset({"op-1"})
        )
        if authenticated
        else None
    )
    server = create_server(
        raw_root=corpus / "raw",
        wiki_root=corpus / "wiki",
        cache_dir=corpus.parent / "cache",
        serve_auth=serve_auth,
    )
    app = server.http_app(path=MCP_PATH, **http_host_origin_kwargs(authenticated=authenticated))

    async def _run() -> int:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)),
                base_url="http://127.0.0.1:8765",
            ) as client:
                resp = await client.post(
                    MCP_PATH, json=INIT_BODY, headers={**MCP_HEADERS, **headers}
                )
                return resp.status_code

    return asyncio.run(_run())


class TestUnauthenticatedLoopbackGuard:
    @pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765", "localhost"])
    def test_loopback_hosts_succeed(self, corpus: Path, host: str) -> None:
        assert _post(corpus, authenticated=False, headers={"Host": host}) == 200

    @pytest.mark.parametrize("host", ["evil.example", "evil.example:8765", PUBLIC_HOST])
    def test_a_rebound_host_is_refused(self, corpus: Path, host: str) -> None:
        assert _post(corpus, authenticated=False, headers={"Host": host}) == 421

    def test_a_foreign_origin_is_refused(self, corpus: Path) -> None:
        status = _post(
            corpus,
            authenticated=False,
            headers={"Host": "127.0.0.1:8765", "Origin": "https://evil.example"},
        )
        assert status == 403

    def test_a_loopback_origin_succeeds(self, corpus: Path) -> None:
        status = _post(
            corpus,
            authenticated=False,
            headers={"Host": "localhost:8765", "Origin": "http://localhost:8765"},
        )
        assert status == 200


class TestAuthenticatedServerAcceptsTunnelIngress:
    def test_public_host_reaches_the_auth_layer_not_the_guard(self, corpus: Path) -> None:
        """Tunnel ingress: a non-loopback Host gets the 401 challenge, not 421."""
        status = _post(corpus, authenticated=True, headers={"Host": PUBLIC_HOST})
        assert status == 401

    def test_negative_control_the_guard_would_have_refused_it(self, corpus: Path) -> None:
        """Same request with the guard forced on is 421, so the 401 above is meaningful."""
        assert _post(corpus, authenticated=False, headers={"Host": PUBLIC_HOST}) == 421


class TestCmdServeWiring:
    def test_no_auth_passes_protection_and_logs_on(
        self, corpus: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        calls: list[dict[str, Any]] = []

        class _Rec:
            def run(self, *_a: object, **kw: Any) -> None:
                calls.append(kw)
                raise KeyboardInterrupt

        import athenaeum.mcp_server as mcp_mod

        monkeypatch.setattr(mcp_mod, "create_server", lambda **_kw: _Rec())
        assert main(["serve", "--path", str(corpus), "--transport", "http"]) == 0
        assert calls and calls[0]["host_origin_protection"] is True
        assert "Host/Origin guard: on" in capsys.readouterr().err

    def test_helper_arms(self) -> None:
        assert http_host_origin_kwargs(authenticated=False) == {"host_origin_protection": True}
        assert http_host_origin_kwargs(authenticated=True) == {"host_origin_protection": False}

    def test_the_installed_fastmcp_accepts_the_guard_keyword(self) -> None:
        """The package floor guarantees the keyword; no runtime probe backs it."""
        import inspect

        from fastmcp import FastMCP

        assert "host_origin_protection" in inspect.signature(FastMCP.run_http_async).parameters
