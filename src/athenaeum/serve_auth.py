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

**Clients, too, are an allowlist when the operator pins them** (issue
athenaeum#2602). :attr:`ServeAuth.allowed_clients` is ``None`` unless
``serve.auth.allowed_clients`` is set, and then a token must name a listed
client in ``azp`` or ``client_id`` -- a token naming neither fails closed.
An entry may carry ``read_only: true``, which withholds the WRITE scope from
that client even when its token carries it. The cap only ever NARROWS: it
removes a scope, and nothing here can add one a token lacks.

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

#: Default clock-skew allowance, in seconds, applied to ``nbf`` / ``iat`` (a
#: token minted by an issuer whose clock runs slightly ahead of this host's
#: must not be refused for it). Configurable via ``serve.auth.clock_leeway``;
#: 60 s is the conventional JWT-library default.
DEFAULT_CLOCK_LEEWAY = 60


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
        clock_leeway: Seconds of clock skew tolerated on ``nbf`` / ``iat``.
        max_token_lifetime: Optional ceiling, in seconds, on how long a token
            may be valid; ``None`` means no ceiling beyond the mandatory
            ``exp``. See :func:`check_time_claims`.
        allowed_clients: Client ids (a token's ``azp`` or ``client_id``)
            permitted to use this server, or ``None`` when no client
            allowlist is configured -- in which case the client is not
            consulted at all. Never empty when set. See :meth:`client_of`.
        read_only_clients: The subset of *allowed_clients* capped to the
            read scope. Empty unless some entry sets ``read_only: true``.
    """

    issuer: str
    resource: str
    allowed_identities: frozenset[str]
    read_scope: str
    write_scope: str
    clock_leeway: int = DEFAULT_CLOCK_LEEWAY
    max_token_lifetime: int | None = None
    allowed_clients: frozenset[str] | None = None
    read_only_clients: frozenset[str] = frozenset()

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

    def time_claim_refusal(self, claims: Mapping[str, object], now: float) -> str | None:
        """:func:`check_time_claims` under this configuration's pins."""
        return check_time_claims(
            claims,
            now,
            leeway=self.clock_leeway,
            max_lifetime=self.max_token_lifetime,
        )

    def identity_allowed(self, claims: Mapping[str, object]) -> bool:
        """Whether *claims* presents an allowlisted identity."""
        return self.identity_of(claims) is not None

    def client_of(self, claims: Mapping[str, object]) -> str | None:
        """The client id *claims* names, or ``None`` when it names none usably.

        Two claims are read: ``azp`` (the OpenID Connect authorized party)
        and ``client_id`` (RFC 9068's access-token claim). Different
        authorization servers emit one or the other. When BOTH are present
        and disagree the token names no single client, so the answer is
        ``None`` -- which, under a configured allowlist, refuses it. A
        non-string or blank value counts as absent.

        Matching is exact. Client ids are opaque and case-sensitive, and a
        Client ID Metadata Document id is a URL whose path is
        case-sensitive, so nothing is folded.
        """
        named: set[str] = set()
        for claim in ("azp", "client_id"):
            value = claims.get(claim)
            if isinstance(value, str) and value.strip():
                named.add(value.strip())
        if len(named) != 1:
            return None
        return next(iter(named))

    def client_refusal(self, claims: Mapping[str, object]) -> str | None:
        """Why *claims* fails the client allowlist, or ``None`` if it passes.

        Always ``None`` when :attr:`allowed_clients` is unset: no allowlist
        means the client is not consulted, which is the pre-athenaeum#2602
        behaviour. Otherwise ``"client_not_identified"`` for a token that
        names no single client (fail closed) and ``"client_not_allowlisted"``
        for one naming a client that is not listed.
        """
        if self.allowed_clients is None:
            return None
        client = self.client_of(claims)
        if client is None:
            return "client_not_identified"
        if client not in self.allowed_clients:
            return "client_not_allowlisted"
        return None

    def client_is_read_only(self, claims: Mapping[str, object]) -> bool:
        """Whether *claims* names a client capped to the read scope."""
        if not self.read_only_clients:
            return False
        client = self.client_of(claims)
        return client is not None and client in self.read_only_clients

    def capped_scopes(
        self, claims: Mapping[str, object], scopes: Iterable[str]
    ) -> list[str]:
        """*scopes* with the write scope removed when the client is read-only.

        Strictly narrowing: the result is always a subset of *scopes*, so the
        cap can withhold a scope the token carries but never grant one it
        lacks. Order is preserved, so an uncapped token's scopes come back
        unchanged.
        """
        if not self.client_is_read_only(claims):
            return list(scopes)
        return [scope for scope in scopes if scope != self.write_scope]

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


def _numeric_claim(claims: Mapping[str, object], name: str) -> float | None | bool:
    """A NumericDate claim as a float, ``None`` if absent, ``False`` if malformed.

    RFC 7519 NumericDate is a JSON number. A bool is not one (``True`` would
    otherwise pass as ``1``), and a string is not coerced.
    """
    if name not in claims or claims[name] is None:
        return None
    value = claims[name]
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return float(value)


def check_time_claims(
    claims: Mapping[str, object],
    now: float,
    *,
    leeway: int = DEFAULT_CLOCK_LEEWAY,
    max_lifetime: int | None = None,
) -> str | None:
    """Why a signature-verified token's time claims are unacceptable, or ``None``.

    Run AFTER the library has verified signature / ``iss`` / ``aud``, because
    FastMCP's own check only looks at ``exp`` when it is present and never
    looks at ``nbf``. Fails closed; the returned reason is a short stable
    identifier (never a claim value, never the token) for the audit log:

    - ``missing_exp`` / ``malformed_exp`` -- a token that never expires turns
      one leak into permanent access, so ``exp`` is required;
    - ``expired`` -- ``exp`` is not after *now* (no leeway on the expiry side:
      leeway only forgives an issuer clock running AHEAD);
    - ``not_yet_valid`` -- ``nbf`` later than *now* + *leeway*;
    - ``issued_in_future`` -- ``iat`` later than *now* + *leeway*;
    - ``lifetime_exceeds_max`` -- only when *max_lifetime* is set: ``exp - iat``
      over the cap, or, when ``iat`` is absent, ``exp - now`` over the cap.
      The lifetime cap is compared exactly, without leeway.

    A malformed ``nbf`` / ``iat`` (present but not a number) is refused too.
    """
    exp = _numeric_claim(claims, "exp")
    if exp is None:
        return "missing_exp"
    if exp is False:
        return "malformed_exp"
    nbf = _numeric_claim(claims, "nbf")
    iat = _numeric_claim(claims, "iat")
    if nbf is False or iat is False:
        return "malformed_time_claim"
    assert isinstance(exp, float)
    if exp <= now:
        return "expired"
    horizon = now + leeway
    if nbf is not None and nbf > horizon:
        return "not_yet_valid"
    if iat is not None and iat > horizon:
        return "issued_in_future"
    if max_lifetime is not None:
        lifetime = exp - (iat if iat is not None else now)
        if lifetime > max_lifetime:
            return "lifetime_exceeds_max"
    return None


def parse_seconds(raw: object, source: str, *, minimum: int) -> int:
    """Validate a whole-number seconds setting, raising :class:`ServeAuthError`.

    Accepts an int or a base-10 integer string (env var spelling); refuses a
    bool (``true`` would otherwise read as ``1``), a float and anything below
    *minimum*.
    """
    value: object = raw
    if isinstance(raw, str):
        try:
            value = int(raw.strip())
        except ValueError:
            value = raw
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ServeAuthError(
            f"{source}={raw!r} must be a whole number of seconds >= {minimum}."
        )
    return value


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


def normalize_clients(values: Iterable[object]) -> tuple[frozenset[str], frozenset[str]]:
    """Parse a client allowlist into ``(allowed, read_only)`` id sets.

    Each entry is either a client id string (full access) or a mapping
    ``{"id": <client id>, "read_only": <bool>}``. Ids are trimmed but NOT
    case-folded -- see :meth:`ServeAuth.client_of`. A client listed twice
    is read-only if ANY of its entries says so: the narrower reading wins
    rather than whichever entry happens to come last.

    Raises :class:`ServeAuthError` on an entry of any other shape, a mapping
    with an unknown key, a missing or blank id, or a ``read_only`` that is
    not a bool. A typo in a security setting is an error, never ignored.
    """
    allowed: set[str] = set()
    read_only: set[str] = set()
    for value in values:
        client_id: object
        capped: object
        if isinstance(value, str):
            client_id, capped = value, False
        elif isinstance(value, Mapping):
            unknown = sorted(str(key) for key in value if key not in ("id", "read_only"))
            if unknown:
                raise ServeAuthError(
                    f"serve.auth.allowed_clients entry {dict(value)!r} has unknown "
                    f"key(s) {unknown}; expected `id` and optionally `read_only`."
                )
            client_id = value.get("id")
            capped = value.get("read_only", False)
            if not isinstance(capped, bool):
                raise ServeAuthError(
                    f"serve.auth.allowed_clients entry {dict(value)!r}: `read_only` "
                    "must be true or false."
                )
        else:
            raise ServeAuthError(
                f"serve.auth.allowed_clients entry {value!r} is neither a client "
                "id string nor a mapping with an `id` key."
            )
        if not isinstance(client_id, str) or not client_id.strip():
            raise ServeAuthError(
                f"serve.auth.allowed_clients entry {value!r} names no client id."
            )
        allowed.add(client_id.strip())
        if capped:
            read_only.add(client_id.strip())
    return frozenset(allowed), frozenset(read_only)


def build_serve_auth(
    *,
    issuer: str | None = None,
    resource: str | None = None,
    allowed_identities: frozenset[str] | None = None,
    scopes: Mapping[str, str] | None = None,
    clock_leeway: int | None = None,
    max_token_lifetime: int | None = None,
    allowed_clients: Iterable[object] | None = None,
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
        clock_leeway: Seconds of ``nbf`` / ``iat`` skew tolerated; ``None``
            takes :data:`DEFAULT_CLOCK_LEEWAY`. Non-negative.
        max_token_lifetime: Optional token-lifetime ceiling in seconds;
            positive. Neither knob counts as "auth configured" on its own.
        allowed_clients: Optional client allowlist (issue athenaeum#2602),
            entries as :func:`normalize_clients` accepts. ``None`` leaves the
            client unconsulted. Setting it DOES count as "auth configured",
            so it cannot be given without the issuer and resource, and an
            empty list is a :class:`ServeAuthError` -- it would refuse every
            token, locking the operator out of their own server.
    """
    configured = (issuer, resource, allowed_identities, scopes, allowed_clients)
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

    leeway = (
        DEFAULT_CLOCK_LEEWAY
        if clock_leeway is None
        else parse_seconds(clock_leeway, "serve.auth.clock_leeway", minimum=0)
    )
    lifetime_cap = (
        None
        if max_token_lifetime is None
        else parse_seconds(max_token_lifetime, "serve.auth.max_token_lifetime", minimum=1)
    )

    clients: frozenset[str] | None = None
    read_only_clients: frozenset[str] = frozenset()
    if allowed_clients is not None:
        clients, read_only_clients = normalize_clients(allowed_clients)
        if not clients:
            raise ServeAuthError(
                "serve.auth.allowed_clients is set but empty, which would refuse "
                "every token. List at least one client id, or remove the key to "
                "accept any client."
            )

    return ServeAuth(
        issuer=issuer_url,
        resource=resource_url,
        allowed_identities=identities,
        read_scope=read_scope,
        write_scope=write_scope,
        clock_leeway=leeway,
        max_token_lifetime=lifetime_cap,
        allowed_clients=clients,
        read_only_clients=read_only_clients,
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
