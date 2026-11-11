# SPDX-License-Identifier: Apache-2.0
"""Serve-time OAuth 2.1 resource-server pins for the MCP server (issue athenaeum#2384).

athenaeum is a **resource server only**. It verifies bearer tokens that an
EXTERNAL authorization server issued; it never issues a token, never stores a
credential, never shows a consent screen, and serves no authorization or
token endpoint. Everything in this module is about reading someone else's
token, not about minting one.

This module owns the resolved shape of the four ``serve.auth.*`` pins —
:class:`ServeAuth` — plus their defaults and the error type the
``config.resolve_serve_auth_*`` resolvers validate against. It is the direct
peer of :mod:`athenaeum.remote_policy` (the athenaeum#2383 serve-time tool /
excluded-field pins) and of :mod:`athenaeum.screening`:
:mod:`athenaeum.config` imports it function-locally from the resolver bodies,
so ``import athenaeum.config`` does not pull it in and the retrieval-path
import budget (``tests/test_import_budget.py``) is unchanged. The FastMCP
plumbing that turns a :class:`ServeAuth` into a live verifier and the RFC 9728
metadata route lives in :mod:`athenaeum.mcp_auth`, which is where the
``fastmcp`` dependency is allowed to appear; nothing here imports it.

**The audience is a PROCESS PIN, never a token claim.** One process serves
exactly one audience: :attr:`ServeAuth.resource` — resolved once, from the
operator's ``athenaeum serve`` invocation, and handed to ``create_server`` as
an ARGUMENT, next to ``caller_audience``, ``screening`` and ``remote_policy``
and for the identical reason (decision D3). Nothing in this module reads an
``aud``, ``audience``, ``roles``, ``groups`` or ``scope`` claim to decide WHAT
a caller may read: a verified, allowlisted identity is served the process's
pinned read scope and nothing wider. A token claiming a broader role gets the
process pin anyway — ``tests/test_mcp_oauth_resource_server.py`` proves that
against live ``recall`` output, because it is the property most easily lost by
a later "just thread the claim through" change.

**Allowlist, not deny-list, in both directions.** An identity must appear in
:attr:`ServeAuth.allowed_identities` (a ``sub`` value, or an email the issuer
marked verified) for its token to be accepted at all, and a tool call must
carry the scope its write/read class requires. Both fail CLOSED: an empty
allowlist is a startup error rather than "anybody the issuer vouches for",
and an unrecognized tool class requires the WRITE scope rather than the
weaker read one.

**Tokens are not loggable.** No function here accepts, formats or returns a
raw token. The identity-rejection path logs the ``sub`` it rejected, never the
credential that carried it.

Layering: L0 — stdlib-only (``dataclasses``/``collections.abc``/
``urllib.parse``), no ``athenaeum`` imports at all, so both the L2 config
resolvers and the L5 MCP server can depend on it without either reaching
through the other.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

#: Default name of the scope a caller needs to invoke a READ tool. Namespaced
#: rather than a bare ``read`` because an authorization server's scope space
#: is global to the token, not to the resource: a bare ``read`` granted for a
#: different resource would otherwise satisfy athenaeum's check too. The
#: operator can rename both halves (``serve.auth.scopes``) to whatever their
#: authorization server actually issues.
DEFAULT_READ_SCOPE = "athenaeum:read"

#: Default name of the scope a caller needs to invoke a WRITE tool — any tool
#: whose MCP ``readOnlyHint`` annotation is false, which is the server's own
#: live read/write classification rather than a second list that could drift
#: from it.
DEFAULT_WRITE_SCOPE = "athenaeum:write"

#: The two keys ``serve.auth.scopes`` accepts, and the only two. Spelled as a
#: mapping rather than an ordered pair so a swapped pair is impossible to
#: express: a two-element list would make ``[write, read]`` a silent privilege
#: inversion, which is the one direction this knob must never fail in.
SCOPE_ROLES: tuple[str, ...] = ("read", "write")

#: Path, relative to the issuer, where athenaeum looks for the authorization
#: server's JSON Web Key Set.
#:
#: This is a CONVENTION, not a configured value, and it is the one assumption
#: this module makes that an operator cannot override. ``serve.auth.issuer``
#: is the only issuer-side setting the acceptance criteria define, so the
#: JWKS location has to be derived from it; this is the path published by the
#: hosted authorization servers that use the plain OIDC default. An issuer
#: that publishes its key set elsewhere (some enterprise IdPs use a
#: tenant-scoped path) is not supported by this issue's setting set, and
#: ``docs/modules/mcp.md`` says so in those words rather than leaving an
#: operator to discover it from a 404.
JWKS_PATH = "/.well-known/jwks.json"

#: The one JWS algorithm athenaeum accepts on a bearer token. Pinned, not
#: configurable: a per-deployment algorithm knob widens the set of signatures
#: that verify, and the whole point of a resource server is that the set is
#: narrow. RS256 is the OIDC-mandated default every authorization server
#: issues, and it is also FastMCP's own ``JWTVerifier`` default, so the pin
#: costs nothing an operator would otherwise have configured. ``none`` and the
#: symmetric ``HS*`` family are unreachable by construction: a shared secret
#: is not something a resource server can hold.
SIGNING_ALGORITHM = "RS256"


class ServeAuthError(ValueError):
    """Raised when the ``serve.auth.*`` configuration is invalid or incomplete.

    Fail-loud, mirroring :class:`athenaeum.remote_policy.RemotePolicyError`
    and :class:`athenaeum.screening.ScreeningConfigError` — the posture every
    serve-time safety pin in this codebase takes. A half-configured resource
    server is the dangerous case, not the annoying one: an issuer with no
    audience to pin, or an audience with no issuer to trust, would otherwise
    resolve to "no auth" and lift the non-loopback bind refusal on a server
    that verifies nothing. ``athenaeum serve`` catches this and exits non-zero
    BEFORE anything binds.
    """


@dataclass(frozen=True)
class ServeAuth:
    """The resolved resource-server pins, fixed for the life of the process.

    Attributes:
        issuer: The authorization server's issuer identifier, as it appears in
            a token's ``iss`` claim. An absolute ``http``/``https`` URL.
        resource: This server's canonical public URL — the value a token's
            ``aud`` claim must carry, the ``resource`` advertised in RFC 9728
            protected resource metadata, and the ONE audience this process
            serves (decision D3: one process per audience).
        allowed_identities: The ``sub`` values and issuer-verified email
            addresses permitted to use this server. Never empty — see
            :func:`build_serve_auth`.
        read_scope: Scope name a READ tool call requires.
        write_scope: Scope name a WRITE tool call requires.
    """

    issuer: str
    resource: str
    allowed_identities: frozenset[str]
    read_scope: str
    write_scope: str

    @property
    def jwks_uri(self) -> str:
        """Where to fetch the issuer's key set — :data:`JWKS_PATH` under *issuer*.

        Derived rather than configured (see :data:`JWKS_PATH`). The issuer's
        own path, if it has one, is preserved: an issuer of
        ``https://example.invalid/tenant-a`` publishes at
        ``https://example.invalid/tenant-a/.well-known/jwks.json``, which is
        what a path-scoped issuer means by the convention.
        """
        return self.issuer.rstrip("/") + JWKS_PATH

    @property
    def scopes_supported(self) -> list[str]:
        """Both scope names, for RFC 9728 ``scopes_supported``.

        Sorted so the advertised metadata is a deterministic function of the
        configuration — a reordering here would otherwise show up as a diff in
        a client's cached discovery document for no reason. De-duplicated only
        as defence in depth; :func:`build_serve_auth` already refuses two
        identical names.
        """
        return sorted({self.read_scope, self.write_scope})

    def identity_of(self, claims: Mapping[str, object]) -> str | None:
        """The allowlisted identity *claims* presents, or ``None`` if none is.

        Two claim shapes are consulted, and only two, because they are the two
        the acceptance criteria name — "a list of ``sub`` values or verified
        emails":

        - ``sub``, the issuer's stable subject identifier;
        - ``email``, but ONLY when the same token carries
          ``email_verified: true``. An unverified email is self-asserted by
          whoever registered the account, so honouring it would let anyone who
          can sign up with the issuer claim an allowlisted operator's address.

        Returns the matching entry (so a caller can log WHICH identity was
        accepted) rather than a bool. Nothing else in the token is consulted:
        a ``roles``, ``groups``, ``audience`` or ``scope`` claim naming a
        broader role has no effect here, which is the property
        ``test_mcp_oauth_resource_server.py`` pins.
        """
        subject = claims.get("sub")
        if isinstance(subject, str) and subject.casefold() in self.allowed_identities:
            return subject.casefold()
        if claims.get("email_verified") is True:
            email = claims.get("email")
            if isinstance(email, str) and email.casefold() in self.allowed_identities:
                return email.casefold()
        return None

    def identity_allowed(self, claims: Mapping[str, object]) -> bool:
        """Whether *claims* presents an allowlisted identity."""
        return self.identity_of(claims) is not None

    def required_scope(self, *, read_only: object) -> str:
        """The scope a tool whose ``readOnlyHint`` is *read_only* requires.

        *read_only* is taken straight from the MCP ``annotations`` dict the
        tool registered with, deliberately — ``create_server`` already states
        each tool's read/write class there, and reading it back is the only
        classification that cannot drift from the live registration. A second
        hand-maintained name list would be wrong the first time a tool is
        added.

        Anything that is not literally ``True`` requires the WRITE scope,
        including a missing annotation and a non-bool value. Fail closed: a
        tool whose class this cannot establish is treated as a mutator.
        """
        return self.read_scope if read_only is True else self.write_scope


def _require_absolute_url(value: str, setting: str) -> str:
    """Return *value* trimmed, or raise if it is not an absolute http(s) URL.

    Both pins are URLs that a token is matched against exactly, so a relative
    or scheme-less value cannot be "mostly right": a token minted for
    ``https://memory.example`` would never match a pin of ``memory.example``,
    and the failure would present as every token being rejected for the wrong
    reason. Rejecting at startup names the setting instead.
    """
    candidate = value.strip()
    if not candidate:
        raise ServeAuthError(f"{setting} is empty; expected an absolute http(s) URL.")
    parts = urlsplit(candidate)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ServeAuthError(
            f"{setting}={value!r} is not an absolute http(s) URL "
            "(e.g. https://memory.example.com/mcp)."
        )
    if parts.query or parts.fragment:
        raise ServeAuthError(
            f"{setting}={value!r} must carry no query string or fragment: it is "
            "matched against a token claim verbatim, and neither is part of an "
            "issuer identifier or a resource identifier."
        )
    return candidate


def _canonical_resource_url(value: str) -> str:
    """``serve.auth.resource``, with an empty path filled in as ``/``.

    Not cosmetic. The resource URL has to be ONE string in three places at
    once: the ``aud`` claim the verifier requires, the ``resource`` field the
    RFC 9728 metadata document advertises, and the identifier a client passes
    as its ``resource`` parameter when it asks the authorization server for a
    token. The middle one goes through the MCP SDK's pydantic ``AnyHttpUrl``
    model, which renders a path-less URL WITH a trailing slash. So a pin of
    ``https://memory.example.com`` would advertise
    ``https://memory.example.com/``, a well-behaved client would request a
    token for that, and the verifier — pinned to the slash-less form — would
    reject every token it got, for a reason visible nowhere in the operator's
    configuration. Normalizing here makes the advertised value and the
    verified value the same string by construction.

    A URL that already has a path (``https://memory.example.com/mcp``, the
    shape the MCP authorization spec actually recommends) is returned
    untouched; ``AnyHttpUrl`` renders those identically.

    The ISSUER deliberately gets no such treatment: ``iss`` is matched
    verbatim against the token, real authorization servers publish it both
    with a trailing slash and without, and only the operator knows which
    their own server mints. Rewriting it would turn a correct configuration
    into one that rejects everything.
    """
    parts = urlsplit(value)
    if parts.path:
        return value
    return value + "/"


def normalize_identities(values: Iterable[object]) -> frozenset[str]:
    """Trim, case-fold and de-duplicate an identity allowlist.

    :meth:`ServeAuth.identity_of` folds the CLAIM side to match, for both
    accepted shapes. Keeping the two sides in step is the whole point: an
    earlier revision folded only the entries and the ``email`` claim, which
    meant an issuer minting a mixed-case subject identifier (Okta's user ids
    are the live example) could never be allowlisted at all — every valid
    token from it got a 401 for a reason visible nowhere in the operator's
    configuration. Caught in review of PR athenaeum#2420.

    Folding ``sub`` does make the allowlist case-INSENSITIVE for subject
    identifiers, which OIDC defines as case-sensitive, so the trade is stated
    rather than assumed: it would matter only for an issuer that mints two
    DISTINCT subjects differing solely in case, which no identity provider
    does — a subject identifier is an opaque unique string, not a name. The
    failure it prevents is real and the failure it admits is hypothetical, and
    an allowlist that silently misses on capitalisation fails by locking the
    operator out of their own server.
    """
    out: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ServeAuthError(
                f"serve.auth.allowed_identities entry {value!r} is not a string; "
                "expected a `sub` value or an email address."
            )
        folded = value.strip().casefold()
        if folded:
            out.add(folded)
    return frozenset(out)


def build_serve_auth(
    *,
    issuer: str | None = None,
    resource: str | None = None,
    allowed_identities: frozenset[str] | None = None,
    scopes: Mapping[str, str] | None = None,
) -> ServeAuth | None:
    """Compose the four resolved pins into a :class:`ServeAuth`, or ``None``.

    ``None`` — returned when NOTHING is configured — is the load-bearing case:
    ``create_server(serve_auth=None)`` is the pre-athenaeum#2384 server, and
    the non-loopback bind refusal the transport child installed stays
    unconditional. A non-``None`` result is what lifts that refusal, which is
    why this function is strict about partial configuration rather than
    filling gaps in:

    - *issuer* without *resource*, or *resource* without *issuer*, is a
      :class:`ServeAuthError`. There is no safe default for either: a verifier
      with no audience accepts a token minted for any other resource that
      shares the issuer (the confused-deputy attack RFC 8707's ``resource``
      parameter exists to prevent), and an audience with no issuer has no key
      set to verify a signature against.
    - An EMPTY *allowed_identities* is a :class:`ServeAuthError`, not "every
      identity the issuer vouches for". The wider reading is the one a typo
      produces, and for a public authorization server it means anyone who can
      sign up. An operator who genuinely wants that has to say so in some
      future setting, loudly; they cannot get it from a blank line.
    - Two identical scope names is a :class:`ServeAuthError`: it would make
      the read scope sufficient for every write tool, silently collapsing the
      distinction the operator configured two names to draw.

    Precedence is resolved BEFORE this function, per pin, by
    ``athenaeum.config.resolve_serve_auth_issuer`` / ``_resource`` /
    ``_allowed_identities`` / ``_scopes`` (CLI > env > yaml each).

    Args:
        issuer: The authorization server's issuer identifier.
        resource: This server's canonical public URL (the audience pin).
        allowed_identities: ``sub`` values / verified emails, already
            normalized by the resolver.
        scopes: ``{"read": ..., "write": ...}``, or ``None`` for the
            :data:`DEFAULT_READ_SCOPE` / :data:`DEFAULT_WRITE_SCOPE` pair.
    """
    configured = (issuer, resource, allowed_identities, scopes)
    if all(pin is None for pin in configured):
        return None

    if issuer is None or resource is None:
        missing = "serve.auth.issuer" if issuer is None else "serve.auth.resource"
        raise ServeAuthError(
            f"{missing} is required once any serve.auth.* setting is given: "
            "athenaeum is a resource server, so it needs BOTH the issuer whose "
            "signatures it trusts and the canonical resource URL it pins every "
            "token's audience to. Set both, or none."
        )

    issuer_url = _require_absolute_url(issuer, "serve.auth.issuer")
    resource_url = _canonical_resource_url(
        _require_absolute_url(resource, "serve.auth.resource")
    )

    # Re-normalized here, not trusted from the resolver -- defence in depth,
    # the same posture the scope pair gets below. The resolver already folds,
    # but `ServeAuth` is constructed in exactly one place and the
    # folded-on-both-sides invariant `ServeAuth.identity_of` relies on is
    # cheaper to hold here than to depend on every caller for. A programmatic
    # caller handing an unfolded set would otherwise reintroduce the lockout
    # this function's sibling docstring describes.
    identities = normalize_identities(allowed_identities or frozenset())
    if not identities:
        raise ServeAuthError(
            "serve.auth.allowed_identities is empty; name at least one `sub` "
            "value or verified email. An empty allowlist is refused rather "
            "than read as 'any identity the issuer vouches for' -- on a public "
            "authorization server that is everyone who can sign up."
        )

    read_scope = DEFAULT_READ_SCOPE
    write_scope = DEFAULT_WRITE_SCOPE
    if scopes is not None:
        # Re-validated here, not trusted from the resolver. Defence in depth,
        # the same posture `build_remote_policy` takes with `serve.profile`:
        # the resolver validates first, so reaching a bad pair here means a
        # programmatic caller built one by hand -- and a half-filled mapping
        # would otherwise be a `KeyError` from a configuration path, or
        # silently drop an unknown third role.
        validated = parse_scope_pair(dict(scopes), "serve.auth.scopes")
        read_scope = validated["read"]
        write_scope = validated["write"]
    if read_scope == write_scope:
        raise ServeAuthError(
            f"serve.auth.scopes names {read_scope!r} for both read and write; "
            "the two must differ or the read scope would authorize every write "
            "tool."
        )

    return ServeAuth(
        issuer=issuer_url,
        resource=resource_url,
        allowed_identities=identities,
        read_scope=read_scope,
        write_scope=write_scope,
    )


def parse_scope_pair(raw: object, source: str) -> dict[str, str]:
    """Validate a ``serve.auth.scopes`` value into ``{"read": ..., "write": ...}``.

    Two spellings, because the setting has to survive three transports with
    very different shapes:

    - a MAPPING (``{read: ..., write: ...}``), which is what the setting looks
      like in ``athenaeum.yaml``;
    - a ``read=<name>,write=<name>`` STRING, which is what fits in a CLI flag
      and an environment variable.

    Both keys are required in both spellings. A single name is refused rather
    than applied to both roles, and an unknown key is refused rather than
    ignored — the failure a silently-dropped ``wrte=`` typo produces is a
    server where every write tool accepts the read scope.

    Args:
        raw: The value as it arrived from one tier.
        source: Which tier it arrived from, for the error message.
    """
    if isinstance(raw, Mapping):
        pairs = {str(name): value for name, value in raw.items()}
    elif isinstance(raw, str):
        pairs = {}
        for chunk in raw.split(","):
            if not chunk.strip():
                continue
            name, separator, value = chunk.partition("=")
            if not separator:
                raise ServeAuthError(
                    f"{source}={raw!r} is not a scope pair; expected "
                    f"`read=<name>,write=<name>`."
                )
            pairs[name.strip().casefold()] = value
    else:
        raise ServeAuthError(
            f"{source}={raw!r} must be a mapping with `read` and `write` keys, "
            "or a `read=<name>,write=<name>` string."
        )

    unknown = sorted(name for name in pairs if name not in SCOPE_ROLES)
    if unknown:
        raise ServeAuthError(
            f"{source} names unknown scope role(s) {unknown}; expected exactly "
            f"{list(SCOPE_ROLES)}."
        )
    out: dict[str, str] = {}
    for role in SCOPE_ROLES:
        scope_name = pairs.get(role)
        if not isinstance(scope_name, str) or not scope_name.strip():
            raise ServeAuthError(
                f"{source} must name a non-empty scope for {role!r}; both "
                f"{list(SCOPE_ROLES)} are required so neither role silently "
                "falls back to the other's name."
            )
        out[role] = scope_name.strip()
    return out
