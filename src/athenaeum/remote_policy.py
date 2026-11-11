# SPDX-License-Identifier: Apache-2.0
"""Serve-time remote-profile pins for the MCP server (issue athenaeum#2383).

Exposing a personal corpus to a third-party host is a different egress from
the operator's own model calls (``docs/design/security-posture.md`` §2.1/§2.2
L6). Two things that are safe on the operator's own machine are not safe
there: the owner-only decision-queue mutators, and the ``with_pii`` /
``include_excluded`` read that resolves a page to its off-corpus excluded
record.

This module owns the resolved shape of those two pins —
:class:`RemotePolicy` — plus the closed vocabularies and the error type the
``config.resolve_serve_*`` resolvers validate against. It is the peer of
:mod:`athenaeum.screening` (which owns ``resolve_screening``'s vocabulary and
:class:`~athenaeum.screening.ScreeningConfigError`) and
:mod:`athenaeum.dimensions`: :mod:`athenaeum.config` imports it
function-locally from the resolver bodies, so ``import athenaeum.config``
does not pull it in and the retrieval-path import budget
(``tests/test_import_budget.py``) is unchanged.

**Unforgeable from the client side.** A :class:`RemotePolicy` is built once,
by the operator's ``athenaeum serve`` invocation, and handed to
``create_server`` as an ARGUMENT — exactly where ``caller_audience`` and
``screening`` already sit, and deliberately NOT as a tool parameter. A remote
caller therefore has nothing to pass that could widen its own tool set or
re-enable an excluded-field read; the pins are not in any tool's input
schema at all.

**Allowlist, not refusal.** A tool excluded by :meth:`RemotePolicy.tool_enabled`
is never registered, so it is ABSENT from ``tools/list`` rather than present
and refusing. A caller cannot distinguish "this deployment does not have that
tool" from "that tool is pinned off", and an agent planning over the served
tool set never proposes a call that can only fail.

Layering: L0 — stdlib-only (``dataclasses``/``collections.abc``), no
``athenaeum`` imports at all, so both the L2 config resolvers and the L5 MCP
server can depend on it without either reaching through the other.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass, field

#: The one profile name :data:`VALID_SERVE_PROFILES` recognizes today.
REMOTE_PROFILE = "remote"

#: Closed vocabulary for ``serve.profile``. A profile is a PRESET over the
#: individual pins, never a third kind of thing: everything it turns on is
#: also settable on its own, so an operator can diverge from the preset in
#: either direction without a new profile name.
VALID_SERVE_PROFILES: frozenset[str] = frozenset({REMOTE_PROFILE})

#: Closed vocabulary for ``serve.excluded_fields``. ``on`` is the historical
#: behaviour (a ``with_pii=True`` caller gets excluded values); ``off``
#: withholds them. Spelled as words rather than a bool so a mistyped value is
#: a loud startup error instead of YAML's permissive truthiness quietly
#: picking a side on a PII-egress boundary.
VALID_EXCLUDED_FIELDS_VALUES: tuple[str, ...] = ("off", "on")

#: The three owner-only decision-queue mutators the ``remote`` preset turns
#: OFF (decision D5). Adjudicating the operator's contradiction/merge/
#: calibration queue is an owner action — see ``security-posture.md`` §2.1's
#: "Owner-only writes" row, which already fails these closed for a RESTRICTED
#: caller. The remote profile goes one step further and does not serve them.
REMOTE_PROFILE_DISABLED_TOOLS: frozenset[str] = frozenset(
    {"resolve_question", "resolve_merge", "review_audit_item"}
)

#: The two mutators the ``remote`` preset deliberately leaves ON (decision
#: D5). Both only ever ADD to the operator's queue — ``remember`` appends raw
#: intake behind the screening pin, ``raise_decision`` files a new pending
#: question — which is ``security-posture.md`` §2.1's "Intentionally open"
#: row. A remote agent contributing memories and raising questions is the
#: whole point of serving it. Named explicitly (rather than left implicit in
#: "everything not disabled") because it is a DECISION, and because
#: ``tests/test_mcp_remote_profile.py`` asserts it against the live set.
REMOTE_PROFILE_ENABLED_MUTATORS: frozenset[str] = frozenset(
    {"remember", "raise_decision"}
)

#: ``serve.excluded_fields`` under the ``remote`` preset: withheld.
REMOTE_PROFILE_EXCLUDED_FIELDS = False


class RemotePolicyError(ValueError):
    """Raised when the ``serve.profile`` / ``serve.tools`` /
    ``serve.excluded_fields`` configuration is invalid or unsatisfiable.

    Fail-loud, mirroring
    :class:`athenaeum.screening.ScreeningConfigError`'s posture for the other
    serve-time safety pin: a mis-set value here would otherwise serve a
    silently WIDER surface than the operator asked for, which is the one
    direction a config error must never fail in. ``athenaeum serve`` catches
    it and exits non-zero.
    """


@dataclass(frozen=True)
class RemotePolicy:
    """The resolved serve-time pins, fixed for the life of the server process.

    Attributes:
        allowed_tools: An explicit ``serve.tools`` ALLOWLIST. ``None`` (the
            default) means no allowlist was given — every tool the profile
            does not disable is served. An EMPTY frozenset is a real,
            different answer ("serve nothing"), which is why this is
            ``None``-able rather than defaulting to the empty set.
        disabled_tools: Names the active profile turns off
            (:data:`REMOTE_PROFILE_DISABLED_TOOLS` for ``remote``). Kept
            separate from *allowed_tools* rather than pre-subtracted into one
            set because the two have different provenance and different
            shapes: a profile SUBTRACTS a named few from whatever the
            deployment registers (so it never has to enumerate — and drift
            from — the full tool set), while an operator's ``serve.tools``
            ADDS a positive ceiling. Both compose: a tool is served only if
            it clears both.
        excluded_fields: ``True`` = the historical behaviour (``with_pii`` /
            ``include_excluded`` resolve excluded values). ``False`` =
            withheld.
        profile: The validated :data:`VALID_SERVE_PROFILES` name this policy
            was built from, or ``None`` when the pins were set individually
            with no profile (issue athenaeum#2387). Kept ALONGSIDE the folded
            pins rather than discarded once folded, because the profile name
            answers a question the folded pins cannot: not "is this tool
            served" but "which host posture is this process serving", which
            is what selects the served ``instructions`` and ``recall``
            description in ``athenaeum.mcp_server.create_server``. It is
            deliberately not a fourth pin -- nothing reads it to decide
            whether a tool is registered or a field resolved -- and it is
            declared LAST so the three original fields keep their positions
            for any caller that constructs this by position.
    """

    allowed_tools: frozenset[str] | None = None
    disabled_tools: frozenset[str] = field(default_factory=frozenset)
    excluded_fields: bool = True
    profile: str | None = None

    def tool_enabled(self, name: str) -> bool:
        """Whether *name* should be REGISTERED on this server process.

        Order matters only for clarity, not for the result: a disabled tool
        stays disabled even if an operator's allowlist names it, because the
        profile's subtraction is the safety floor and the allowlist is a
        ceiling. Both are applied; neither can widen the other.
        """
        if name in self.disabled_tools:
            return False
        if self.allowed_tools is None:
            return True
        return name in self.allowed_tools

    def validate_tool_names(self, registered: Collection[str]) -> None:
        """Raise :class:`RemotePolicyError` if a pin names an unknown tool.

        *registered* is every tool name the server WOULD register with no
        policy at all — collected at registration time rather than from a
        constant in this module, so a tool added or renamed in
        ``create_server`` can never drift from what an allowlist is checked
        against.

        A typo in ``serve.tools`` is the failure this exists for: without
        this check ``serve.tools: [recal]`` silently serves nothing but
        whatever else was listed, and the operator sees a working server
        missing the one tool they care about. It fails startup instead, and
        the message names both the unknown entries and the valid set.
        """
        known = frozenset(registered)
        if not known:
            raise RemotePolicyError(
                "no tool names were collected to validate the serve-time "
                "pins against; this is an internal error in create_server, "
                "not a configuration problem."
            )
        unknown = sorted(name for name in self._named_tools() if name not in known)
        if unknown:
            raise RemotePolicyError(
                f"serve-time tool pin names unknown tool(s) {unknown}; this "
                f"deployment serves {sorted(known)}."
            )

    def _named_tools(self) -> Iterable[str]:
        """Every tool name either pin mentions, for validation."""
        if self.allowed_tools is not None:
            yield from self.allowed_tools
        yield from self.disabled_tools


def build_remote_policy(
    *,
    profile: str | None = None,
    tools: frozenset[str] | None = None,
    excluded_fields: bool | None = None,
) -> RemotePolicy | None:
    """Compose the three resolved pins into a :class:`RemotePolicy`, or ``None``.

    ``None`` — returned when nothing is pinned at all — is the load-bearing
    case: ``create_server(remote_policy=None)`` is byte-identical to the
    pre-athenaeum#2383 server, so an install that sets none of the new keys
    serves the same ``tools/list`` and the same tool output as before.

    Precedence is resolved BEFORE this function, per pin, by
    ``athenaeum.config.resolve_serve_profile`` / ``resolve_serve_tools`` /
    ``resolve_serve_excluded_fields`` (CLI > env > yaml each). What is left
    here is how a PROFILE layers with the individual pins: the profile
    supplies defaults, and an explicitly-set individual pin overrides it.
    That is why *excluded_fields* is tri-state — ``None`` means "not set, let
    the profile decide", which a plain ``bool`` could not express.

    Args:
        profile: A validated :data:`VALID_SERVE_PROFILES` member, or ``None``.
        tools: An explicit allowlist, or ``None`` for no allowlist.
        excluded_fields: ``True``/``False`` to pin it explicitly, ``None`` to
            take the profile's default (and the historical ``True`` with no
            profile).

    The *profile* name is carried through onto the returned policy
    (issue athenaeum#2387) as well as folded into the pins. That does not
    change the ``None`` return above: a call with no profile, no tools and no
    ``excluded_fields`` still returns ``None``, so an install that sets none
    of the new keys is unchanged -- there is no policy object for a
    ``profile`` field to appear on.
    """
    if profile is None and tools is None and excluded_fields is None:
        return None
    if profile is not None and profile not in VALID_SERVE_PROFILES:
        # Defence in depth: the resolver validates first, so reaching this
        # means a programmatic caller built a policy by hand.
        raise RemotePolicyError(
            f"serve.profile={profile!r} is invalid; expected one of "
            f"{sorted(VALID_SERVE_PROFILES)}."
        )
    disabled: frozenset[str] = frozenset()
    profile_excluded_fields = True
    if profile == REMOTE_PROFILE:
        disabled = REMOTE_PROFILE_DISABLED_TOOLS
        profile_excluded_fields = REMOTE_PROFILE_EXCLUDED_FIELDS
    return RemotePolicy(
        allowed_tools=tools,
        disabled_tools=disabled,
        excluded_fields=(
            profile_excluded_fields if excluded_fields is None else excluded_fields
        ),
        profile=profile,
    )
