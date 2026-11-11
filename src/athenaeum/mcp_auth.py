# SPDX-License-Identifier: Apache-2.0
"""FastMCP wiring for athenaeum's OAuth 2.1 resource-server mode (issue athenaeum#2384).

:mod:`athenaeum.serve_auth` owns the resolved SHAPE of the four
``serve.auth.*`` pins and every decision that can be made without a web
framework. This module owns the other half: turning a
:class:`~athenaeum.serve_auth.ServeAuth` into the live FastMCP objects that
verify a bearer token and serve RFC 9728 protected resource metadata. The
split is deliberate and matches :mod:`athenaeum.remote_policy` /
:mod:`athenaeum.mcp_server` — the policy module stays stdlib-only and
unit-testable, and the ``fastmcp`` import is confined to the presentation
layer that already depends on it.

Two FastMCP behaviours are overridden here, each for one acceptance criterion:

1. **The audience is the process pin, not the mount path.** FastMCP derives
   the advertised resource URL by appending the MCP endpoint's path to the
   provider's ``base_url``, so a default build would advertise
   ``<resource>/mcp`` while verifying tokens against ``<resource>`` — two
   different audiences for one process. :class:`_PinnedResourceAuthProvider`
   returns ``serve.auth.resource`` verbatim instead, which makes the verified
   audience, the advertised ``resource``, and the metadata URL in the
   ``WWW-Authenticate`` challenge one value by construction rather than by
   three settings agreeing.
2. **The identity allowlist runs after, and only after, the token verifies.**
   :class:`_IdentityAllowlistVerifier` subclasses FastMCP's ``JWTVerifier``
   rather than wrapping it, so signature / ``iss`` / ``aud`` / ``exp`` are all
   decided by the library's own code path and the allowlist is a strictly
   ADDITIONAL narrowing on top. A non-allowlisted identity returns ``None``
   exactly as a bad signature does, which is what makes it a 401 carrying the
   same ``WWW-Authenticate`` challenge rather than a distinguishable 403 — a
   caller must not be able to probe which subjects are on an operator's
   allowlist.

**No token is ever logged, formatted or returned by anything here.** The
rejection path logs the ``sub`` claim it refused, which is an identifier the
operator put in their own configuration, never the credential that carried
it. FastMCP's own verifier was checked for the same property: every one of
its rejection messages names the derived ``client_id`` and the expected
value, never the token, including the ``ValueError`` path that formats a
malformed-token exception.
``tests/test_mcp_oauth_resource_server.py::TestTokensNeverReachLogs`` is the
standing guard on that, across both a failed and a successful call.

Layering: L5 — presentation, peer of :mod:`athenaeum.mcp_server`. Imports
``fastmcp`` at module scope, so :mod:`athenaeum.mcp_server` imports THIS
module function-locally (from ``create_server``) and the ``fastmcp``
dependency stays optional for every non-serve code path.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from pydantic import AnyHttpUrl

from athenaeum.serve_auth import SIGNING_ALGORITHM, ServeAuth

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastmcp.server.auth import AccessToken

logger = logging.getLogger(__name__)


class _IdentityAllowlistVerifier(JWTVerifier):
    """A ``JWTVerifier`` that additionally requires an allowlisted identity.

    Subclassed, not composed: ``RemoteAuthProvider`` reads
    ``token_verifier.required_scopes`` and ``scopes_supported`` off whatever
    it is handed, and FastMCP's JWKS cache, refresh lock and claim extraction
    are all behaviour worth inheriting rather than re-deriving. The override
    is three lines on top of the library's result.
    """

    def __init__(self, serve_auth: ServeAuth, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._serve_auth = serve_auth

    async def verify_token(self, token: str) -> AccessToken | None:
        """Verify *token*, then require that its identity is allowlisted.

        Returns ``None`` — indistinguishable from an invalid token, by design
        — when the verified claims present no allowlisted ``sub`` or verified
        email. A caller must not be able to tell "your signature is fine but
        you are not on the list" from "your signature is wrong": the first
        answer turns the endpoint into an oracle for the operator's allowlist.
        """
        access = await super().verify_token(token)
        if access is None:
            return None
        identity = self._serve_auth.identity_of(access.claims)
        if identity is None:
            # `sub`, never the token. `%r` on a possibly-absent claim rather
            # than `%s` so a missing subject reads as `None` instead of an
            # empty gap in the line.
            logger.warning(
                "Bearer token rejected: subject %r is not in "
                "serve.auth.allowed_identities",
                access.claims.get("sub"),
            )
            return None
        return access


class _PinnedResourceAuthProvider(RemoteAuthProvider):
    """A ``RemoteAuthProvider`` whose resource URL is the operator's pin.

    ``AuthProvider._get_resource_url`` appends the MCP endpoint path to
    ``base_url``; this returns ``serve.auth.resource`` unchanged for every
    *path*, including ``None``. Everything downstream — the RFC 9728 route's
    own path, the ``resource`` field in its body, and the
    ``resource_metadata=`` parameter FastMCP puts in the ``WWW-Authenticate``
    challenge — is derived from this one method, so overriding it is what
    makes those three agree with the audience the verifier enforces.
    """

    def __init__(self, resource: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._pinned_resource = AnyHttpUrl(resource)

    def _get_resource_url(self, path: str | None = None) -> AnyHttpUrl:
        """The pinned resource URL, whatever endpoint path is mounted."""
        return self._pinned_resource


def build_auth_provider(
    serve_auth: ServeAuth,
    *,
    jwks_http_client: Any | None = None,
) -> RemoteAuthProvider:
    """Build the resource-server auth provider for *serve_auth*.

    The returned provider does three things when ``FastMCP`` is constructed
    with it: it verifies every bearer token (JWKS signature, ``iss``, ``aud``
    against the process pin, ``exp``/``nbf``) and narrows to the identity
    allowlist; it serves the RFC 9728 protected resource metadata route; and
    it makes FastMCP answer 401 with a ``WWW-Authenticate`` challenge naming
    that route.

    Scopes are deliberately NOT required at the transport: ``required_scopes``
    stays empty and the read/write distinction is enforced per tool, in
    ``create_server``, from each tool's own ``readOnlyHint`` annotation. A
    transport-level requirement cannot express it — a single required-scope
    list applies to every request alike, so requiring the read scope would
    401 a write-only token and requiring both would 401 a read-only one. Both
    names are still advertised in ``scopes_supported`` and asked for in the
    challenge, so a client knows what to request.

    Args:
        serve_auth: The resolved pins. Never ``None`` — a caller with no auth
            configured must not build a provider at all.
        jwks_http_client: Optional pre-built async HTTP client for the JWKS
            fetch — FastMCP's ``JWTVerifier`` takes one directly, and this
            passes it straight through. Production passes nothing and FastMCP
            opens its own client per fetch; the test suite passes a client
            bound to an in-process ASGI app, which is how
            ``tests/test_mcp_oauth_resource_server.py`` serves a key set with
            no socket open. The CLIENT rather than a transport, deliberately:
            constructing one here would mean importing FastMCP's HTTP library
            into ``src/``, i.e. declaring a runtime dependency athenaeum does
            not otherwise have, for a seam only tests use. The caller owns the
            client's lifecycle — FastMCP does not close a client it was
            handed. It is an operator-side argument on an operator-side
            object, so — like every other serve-time pin — there is nothing a
            remote caller can pass here.
    """
    verifier = _IdentityAllowlistVerifier(
        serve_auth,
        jwks_uri=serve_auth.jwks_uri,
        issuer=serve_auth.issuer,
        audience=serve_auth.resource,
        algorithm=SIGNING_ALGORITHM,
        http_client=jwks_http_client,
    )
    return _PinnedResourceAuthProvider(
        serve_auth.resource,
        token_verifier=verifier,
        authorization_servers=[AnyHttpUrl(serve_auth.issuer)],
        base_url=AnyHttpUrl(serve_auth.resource),
        scopes_supported=serve_auth.scopes_supported,
        challenge_scopes=serve_auth.scopes_supported,
        resource_name="athenaeum",
    )
