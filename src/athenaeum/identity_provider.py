# SPDX-License-Identifier: Apache-2.0
"""Turn the context of a request into a principal (issue athenaeum#3171).

Layering: L3 (imports :mod:`athenaeum.config`, L2, :mod:`athenaeum.models`, L1,
and :mod:`athenaeum.serve_auth`, L0).

Design: ``docs/design/security-posture.md`` ("Identity providers").

An :class:`IdentityProvider` has one method, :meth:`~IdentityProvider.principal`,
which returns an identity key -- the tuple shape
:func:`athenaeum.models.asserter_identity_key` produces and
:func:`athenaeum.access.can` takes -- or ``None`` when the request carries no
identity. Two providers ship:

* ``local`` returns the install's owner: the key of ``owner.asserter``, or
  ``None`` when no owner is configured.
* ``oidc`` returns the principal of a bearer token the network server has
  ALREADY verified. The token-to-key mapping is
  :func:`athenaeum.serve_auth.verified_asserter` fed to
  :func:`~athenaeum.models.asserter_identity_key`, so a token and an
  ``asserter`` block for the same person give equal keys, for the ``(iss,
  sub)`` shape and for the Microsoft Entra tenant-scoped shape alike.

What a provider is not. It stores no credential, issues no token and calls no
identity provider's management interface; the network server stays a resource
server only. ``oidc`` reads the verified identity and no role, group, audience
or scope claim. It never sees an unverified token: the only way to build an
:class:`IdentityContext` carrying claims is :func:`request_principal`, which
takes the access token the verifier returned and does nothing for ``None``.

The provider is chosen once, at startup, by :func:`startup_provider_name` --
``oidc`` when the server is configured as a resource server, ``local``
otherwise (a stdio server and the command line). It is never a tool
parameter, so a caller cannot pick who it is.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from athenaeum.config import resolve_owner_asserter
from athenaeum.models import asserter_identity_key
from athenaeum.serve_auth import verified_asserter

__all__ = [
    "BUILTIN_IDENTITY_PROVIDERS",
    "IdentityContext",
    "IdentityProvider",
    "LocalIdentityProvider",
    "OidcIdentityProvider",
    "get_identity_provider",
    "register_identity_provider",
    "request_principal",
    "startup_provider_name",
]


@dataclass(frozen=True)
class IdentityContext:
    """What a provider may look at.

    ``claims`` are the claims of a token that passed verification, or ``None``
    when the request carries none. ``config`` is the resolved configuration
    mapping, which ``local`` reads the owner from.
    """

    claims: Mapping[str, object] | None = None
    config: dict[str, Any] | None = None


@runtime_checkable
class IdentityProvider(Protocol):
    """One method: the identity key of the request's principal, or ``None``."""

    def principal(self, context: IdentityContext) -> tuple[str, ...] | None: ...


class LocalIdentityProvider:
    """The install's owner. ``None`` when no owner is configured."""

    def principal(self, context: IdentityContext) -> tuple[str, ...] | None:
        key = asserter_identity_key(resolve_owner_asserter(context.config))
        return tuple(key) or None


class OidcIdentityProvider:
    """The principal of an already-verified bearer token.

    Reads ``iss``, ``sub`` and the Entra ``tid`` / ``oid`` claims through
    :func:`athenaeum.serve_auth.verified_asserter` and nothing else.
    """

    def principal(self, context: IdentityContext) -> tuple[str, ...] | None:
        if context.claims is None:
            return None
        key = asserter_identity_key(verified_asserter(context.claims))
        return tuple(key) or None


#: Names a registration can never take over.
BUILTIN_IDENTITY_PROVIDERS: tuple[str, ...] = ("local", "oidc")

_BUILTINS: dict[str, IdentityProvider] = {
    "local": LocalIdentityProvider(),
    "oidc": OidcIdentityProvider(),
}
_REGISTERED: dict[str, IdentityProvider] = {}


def register_identity_provider(name: str, provider: IdentityProvider) -> None:
    """Register *provider* under *name*.

    Raises ``ValueError`` for a built-in name (``local``, ``oidc``) or an empty
    one; re-registering a non-built-in name replaces it.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("identity provider name must be a non-empty string")
    if name in BUILTIN_IDENTITY_PROVIDERS:
        raise ValueError(f"identity provider {name!r} is built in and cannot be replaced")
    if not isinstance(provider, IdentityProvider):
        raise ValueError(f"identity provider {name!r} has no principal(context) method")
    _REGISTERED[name] = provider


def get_identity_provider(name: str) -> IdentityProvider:
    """The provider registered or built in under *name* (``ValueError`` if none)."""
    provider = _BUILTINS.get(name) or _REGISTERED.get(name)
    if provider is None:
        known = ", ".join(sorted({*_BUILTINS, *_REGISTERED}))
        raise ValueError(f"unknown identity provider {name!r}: expected one of {known}")
    return provider


def startup_provider_name(*, resource_server: bool) -> str:
    """The provider a process uses, decided once when it starts.

    ``oidc`` for a network server configured as an OAuth resource server,
    ``local`` for everything else: a stdio server and the command line.
    """
    return "oidc" if resource_server else "local"


def request_principal(
    provider: IdentityProvider,
    access_token: Any | None,
    *,
    config: dict[str, Any] | None = None,
) -> tuple[str, ...] | None:
    """The principal of one request, asking *provider* only about verified tokens.

    *access_token* is what the verifier handed back for the request (an object
    with a ``claims`` mapping), or ``None`` when the token failed verification
    or the request carried none. ``None`` never reaches the provider's
    ``claims``: the provider is handed a context with no claims. A provider is
    therefore never given the claims of a token that failed verification.
    """
    claims = getattr(access_token, "claims", None) if access_token is not None else None
    return provider.principal(IdentityContext(claims=claims, config=config))
