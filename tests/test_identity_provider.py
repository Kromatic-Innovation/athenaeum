# SPDX-License-Identifier: Apache-2.0
"""Identity providers: request context -> principal (issue athenaeum#3171)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from athenaeum import identity_provider as ip
from athenaeum.models import asserter_identity_key
from athenaeum.serve_auth import verified_asserter
from tests.test_mcp_oauth_resource_server import (
    ALLOWED_SUBJECT,
    ISSUER,
    OTHER_ISSUER,
    _FakeIssuer,
    _mcp_client,
    _raw_client,
    _serve_auth,
)
from tests.test_mcp_oauth_resource_server import (
    _no_ambient_serve_auth_env as _env_fixture,
)
from tests.test_mcp_oauth_resource_server import (
    corpus as _corpus_fixture,
)
from tests.test_mcp_oauth_resource_server import (
    issuer as _issuer_fixture,
)

_no_ambient_serve_auth_env = _env_fixture
corpus = _corpus_fixture
issuer = _issuer_fixture

TENANT = "11111111-2222-3333-4444-555555555555"
OBJECT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _oidc(claims: dict[str, Any] | None, config: dict[str, Any] | None = None):
    return ip.get_identity_provider("oidc").principal(
        ip.IdentityContext(claims=claims, config=config)
    )


class TestRegistry:
    def test_builtin_names_cannot_be_shadowed(self) -> None:
        class Mine:
            def principal(self, context: ip.IdentityContext):
                return ("x",)

        for name in ("local", "oidc"):
            with pytest.raises(ValueError, match="built in"):
                ip.register_identity_provider(name, Mine())
        assert isinstance(ip.get_identity_provider("local"), ip.LocalIdentityProvider)
        assert isinstance(ip.get_identity_provider("oidc"), ip.OidcIdentityProvider)

    def test_register_and_resolve_a_custom_provider(self) -> None:
        class Mine:
            def principal(self, context: ip.IdentityContext):
                return ("custom", "who")

        ip.register_identity_provider("example-custom", Mine())
        got = ip.get_identity_provider("example-custom")
        assert got.principal(ip.IdentityContext()) == ("custom", "who")

    def test_rejects_bad_registrations_and_unknown_names(self) -> None:
        with pytest.raises(ValueError):
            ip.register_identity_provider("", ip.LocalIdentityProvider())
        with pytest.raises(ValueError, match="principal"):
            ip.register_identity_provider("no-method", object())  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="unknown identity provider"):
            ip.get_identity_provider("nope")


class TestLocal:
    def test_returns_owner_key(self) -> None:
        asserter = {"iss": "https://idp.example.org", "sub": "owner-1"}
        cfg = {"owner": {"asserter": asserter}}
        got = ip.get_identity_provider("local").principal(ip.IdentityContext(config=cfg))
        assert got == asserter_identity_key(asserter) == ("https://idp.example.org", "owner-1")

    def test_entra_owner_key(self) -> None:
        asserter = {
            "iss": "https://login.example.org/v2.0",
            "sub": "pairwise",
            "provider_ids": {"entra_tid": TENANT, "entra_oid": OBJECT},
        }
        got = ip.get_identity_provider("local").principal(
            ip.IdentityContext(config={"owner": {"asserter": asserter}})
        )
        assert got == asserter_identity_key(asserter)

    @pytest.mark.parametrize("cfg", [None, {}, {"owner": {}}, {"owner": {"uid": "x"}}])
    def test_none_without_an_owner(self, cfg: Any) -> None:
        assert ip.get_identity_provider("local").principal(ip.IdentityContext(config=cfg)) is None


class TestOidcMatchesAsserterKey:
    def test_iss_sub_shape(self) -> None:
        asserter = {"iss": "https://idp.example.org", "sub": "person-7"}
        token = {"iss": "https://idp.example.org", "sub": "person-7", "email": "p@example.org"}
        assert _oidc(token) == asserter_identity_key(asserter)

    def test_entra_tenant_scoped_shape(self) -> None:
        asserter = {
            "iss": "https://login.example.org/v2.0",
            "sub": "app-pairwise-A",
            "provider_ids": {"entra_tid": TENANT, "entra_oid": OBJECT},
        }
        token = {
            "iss": "https://login.example.org/v2.0",
            "sub": "app-pairwise-B",  # differs per app; must not matter
            "tid": TENANT,
            "oid": OBJECT,
        }
        got = _oidc(token)
        assert got == asserter_identity_key(asserter)
        assert got == ("https://login.example.org/v2.0", "entra", TENANT, OBJECT)

    def test_only_one_entra_identifier_falls_back_to_iss_sub(self) -> None:
        token = {"iss": "https://idp.example.org", "sub": "s", "tid": TENANT}
        assert _oidc(token) == ("https://idp.example.org", "s")

    def test_no_identity_is_none(self) -> None:
        assert _oidc(None) is None
        assert _oidc({}) is None
        assert _oidc({"iss": "https://idp.example.org"}) is None
        assert _oidc({"iss": "https://idp.example.org", "sub": 7}) is None

    def test_extra_roles_groups_aud_scope_yield_the_same_principal(self) -> None:
        bare = {"iss": "https://idp.example.org", "sub": "person-7"}
        stuffed = {
            **bare,
            "roles": ["admin", "owner"],
            "groups": ["everyone", "admins"],
            "aud": ["https://other.example.org"],
            "audience": "everything",
            "scp": "write admin",
            "scope": "read write admin",
            "email": "owner@example.org",
            "email_verified": True,
        }
        assert _oidc(stuffed) == _oidc(bare) == ("https://idp.example.org", "person-7")
        assert verified_asserter(stuffed) == verified_asserter(bare)


class TestStartupChoice:
    def test_stdio_and_cli_local_network_oidc(self) -> None:
        assert ip.startup_provider_name(resource_server=False) == "local"
        assert ip.startup_provider_name(resource_server=True) == "oidc"

    def test_no_provider_choice_in_any_tool_schema(self, corpus: Path) -> None:
        from athenaeum.mcp_server import create_server

        server = create_server(
            raw_root=corpus / "raw", wiki_root=corpus / "wiki", cache_dir=corpus.parent / "cache"
        )
        assert server.identity_provider_name == "local"  # type: ignore[attr-defined]
        import asyncio

        tools = asyncio.run(server.list_tools())
        for tool in tools:
            props = {name.lower() for name in tool.parameters.get("properties", {})}
            assert not {"provider", "identity_provider", "principal", "identity"} & props

    def test_network_server_uses_oidc(self, corpus: Path, issuer: _FakeIssuer) -> None:
        from athenaeum.mcp_server import create_server

        server = create_server(
            raw_root=corpus / "raw",
            wiki_root=corpus / "wiki",
            cache_dir=corpus.parent / "cache",
            serve_auth=_serve_auth(),
            jwks_http_client=issuer.http_client(),
        )
        assert server.identity_provider_name == "oidc"  # type: ignore[attr-defined]


class TestVerifiedTokensOnly:
    def _whoami_app(self, corpus: Path, issuer: _FakeIssuer, spy: list) -> Any:
        from athenaeum.mcp_server import create_server

        server = create_server(
            raw_root=corpus / "raw",
            wiki_root=corpus / "wiki",
            cache_dir=corpus.parent / "cache",
            serve_auth=_serve_auth(),
            jwks_http_client=issuer.http_client(),
        )

        @server.tool()
        def whoami() -> str:
            return json.dumps(server.current_principal())  # type: ignore[attr-defined]

        return server.http_app(path="/mcp")

    @pytest.fixture
    def spy(self, monkeypatch: pytest.MonkeyPatch) -> list:
        seen: list = []
        real = ip.OidcIdentityProvider.principal

        def _principal(self: Any, context: ip.IdentityContext):
            seen.append(context.claims)
            return real(self, context)

        monkeypatch.setattr(ip.OidcIdentityProvider, "principal", _principal)
        return seen

    def test_verified_token_reaches_the_provider(
        self, corpus: Path, issuer: _FakeIssuer, spy: list
    ) -> None:
        import asyncio

        app = self._whoami_app(corpus, issuer, spy)

        async def run() -> Any:
            async with app.router.lifespan_context(app):
                async with _mcp_client(app, issuer.mint(roles=["admin"])) as client:
                    return await client.call_tool("whoami", {})

        result = asyncio.run(run())
        assert json.loads(result.content[0].text) == [ISSUER, ALLOWED_SUBJECT]
        assert len(spy) == 1

    @pytest.mark.parametrize("failure", ["bad_signature", "wrong_issuer", "expired", "none"])
    def test_failed_verification_never_reaches_a_provider(
        self, corpus: Path, issuer: _FakeIssuer, spy: list, failure: str
    ) -> None:
        import asyncio
        import time

        app = self._whoami_app(corpus, issuer, spy)
        tokens = {
            "bad_signature": issuer.mint(signer=issuer.other_signer),
            "wrong_issuer": issuer.mint(iss=OTHER_ISSUER),
            "expired": issuer.mint(exp=int(time.time()) - 3600),
            "none": None,
        }

        async def run() -> int:
            async with app.router.lifespan_context(app):
                async with _raw_client(app) as client:
                    headers = {"Accept": "application/json, text/event-stream"}
                    if tokens[failure] is not None:
                        headers["Authorization"] = f"Bearer {tokens[failure]}"
                    resp = await client.post(
                        "/mcp",
                        headers=headers,
                        json={
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {"name": "whoami", "arguments": {}},
                        },
                    )
                    return resp.status_code

        assert asyncio.run(run()) == 401
        assert spy == []

    def test_request_principal_does_not_pass_claims_without_a_token(self) -> None:
        seen: list = []

        class Spy:
            def principal(self, context: ip.IdentityContext):
                seen.append(context.claims)
                return None

        assert ip.request_principal(Spy(), None) is None
        assert seen == [None]
