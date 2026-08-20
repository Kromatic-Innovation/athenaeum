# SPDX-License-Identifier: Apache-2.0
"""Per-field surface-divergence registry (issue athenaeum#963).

``bounce-divergence`` (issue athenaeum#853) and ``do-not-email-divergence``
(issue athenaeum#960) are two independent copies of the same shape: scan the
wiki surface for a mark, scan the contacts/excluded surface for a mark, join
on a key, report the set difference. Nothing generalized that shape until
now — a third field would have meant copying one of the two modules again,
exactly the drift ``bounce_join.py`` already warns about in its own
docstring ("two implementations of the same join is exactly the drift this
epic is cleaning up").

This module is the generalization: a small registry of :class:`FieldSpec`
descriptors, one per registered field, each naming:

- ``name`` — the registry key (and the ``--field`` value on
  ``athenaeum surface-divergence``);
- ``wiki_key`` — the wiki frontmatter key the field reads;
- ``join_key`` — how the two surfaces are joined (documentary — the actual
  join lives in each field's ``compute`` callable, since the two registered
  fields join at genuinely different granularity: ``bounced`` is
  address-level via the contacts-surface person record's ``uid``,
  ``do_not_email`` is a direct per-page/per-record ``uid`` set difference);
- ``allowance`` — the tolerated-residual policy, in prose, cited to its
  justifying doc where one exists;
- ``exceeds_allowance`` — the predicate the CLI's failing mode calls.

Adding a field is registering a :class:`FieldSpec` here — no new command, no
copied module. The two registered fields deliberately keep their EXISTING
compute/report/render/dict implementations (:mod:`athenaeum.bounce_divergence`
and :mod:`athenaeum.do_not_email_divergence`) unchanged and wrapped rather
than reimplemented, so ``bounced``'s report numbers and JSON keys are byte-
for-byte what they were before this issue (the AC that protects issue
athenaeum#853's report from regressing).

**Layering:** L4, alongside the two modules it wraps. This module imports
them; neither imports this one back.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

from athenaeum import bounce_divergence, do_not_email_divergence
from athenaeum.bounce_join import WIKI_BOUNCED_FIELD

#: Exit code when a surface could not be read — shared with the two
#: per-field commands this module generalizes, so a consumer sees the same
#: code regardless of which entry point it came through.
EXIT_SURFACE_UNREADABLE = 2

#: Exit code when a registered field diverges beyond its declared allowance.
EXIT_DIVERGED = 3


@dataclass(frozen=True)
class FieldSpec:
    """One registered field's divergence check, allowance policy, and shape."""

    name: str
    wiki_key: str
    join_key: str
    allowance: str
    compute: Callable[[Path, Path, date | None], Any]
    exceeds_allowance: Callable[[Any], bool]
    is_complete: Callable[[Any], bool]
    render_report: Callable[[Any], str]
    report_as_dict: Callable[[Any], dict[str, Any]]


def _bounced_exceeds_allowance(report: bounce_divergence.DivergenceReport) -> bool:
    """``bounced``'s declared allowance (docs/bounce-surface-convergence.md).

    Tolerated: a wiki-surface entry with no pii mark
    (``report.on_wiki_not_marked``) — the documented evidence-class
    asymmetry (the wiki field is a strictly broader union than the
    ``5.x.x``-only pii mark; see "The evidence-class asymmetry" in that
    doc). NOT tolerated, zero allowance: a pii mark with no wiki entry
    (``report.marked_not_on_wiki``) — the direction the doc does not excuse,
    and the one athenaeum#963 registers a failing check for.
    """
    return bool(report.marked_not_on_wiki)


def _do_not_email_exceeds_allowance(
    report: do_not_email_divergence.DoNotEmailDivergenceReport,
) -> bool:
    """``do_not_email``'s declared allowance: zero, in either direction.

    One operator-directed fact with one meaning — unlike ``bounced``, there
    is no evidence-class asymmetry that would excuse either direction of
    disagreement (see :func:`athenaeum.pii.do_not_email_state`'s own
    docstring, which names this module as the guard for exactly this).
    """
    return report.diverged


_REGISTRY: dict[str, FieldSpec] = {}


def _register(spec: FieldSpec) -> None:
    _REGISTRY[spec.name] = spec


_register(
    FieldSpec(
        name="bounced",
        wiki_key=WIKI_BOUNCED_FIELD,
        join_key="uid (address -> contacts-surface person record -> uid -> wiki page)",
        allowance=(
            "wiki-surface entries with no pii mark are TOLERATED (documented "
            "evidence-class asymmetry, docs/bounce-surface-convergence.md); "
            "pii marks with no wiki entry are NOT tolerated (zero)."
        ),
        compute=lambda wiki_root, contacts_root, as_of: bounce_divergence.compute_divergence(
            wiki_root, contacts_root, as_of=as_of
        ),
        exceeds_allowance=_bounced_exceeds_allowance,
        is_complete=lambda report: report.complete,
        render_report=bounce_divergence.render_report,
        report_as_dict=bounce_divergence.report_as_dict,
    )
)

_register(
    FieldSpec(
        name="do_not_email",
        wiki_key="do_not_email",
        join_key="uid",
        allowance="zero divergence tolerated in either direction (one operator fact, one meaning).",
        compute=lambda wiki_root, contacts_root, as_of: (
            do_not_email_divergence.compute_do_not_email_divergence(wiki_root, contacts_root)
        ),
        exceeds_allowance=_do_not_email_exceeds_allowance,
        is_complete=lambda report: report.complete,
        render_report=do_not_email_divergence.render_report,
        report_as_dict=do_not_email_divergence.report_as_dict,
    )
)


def field_names() -> list[str]:
    """Registered field names, sorted — the valid ``--field`` choices."""
    return sorted(_REGISTRY)


def get_field(name: str) -> FieldSpec:
    """The :class:`FieldSpec` for *name*.

    Raises :class:`KeyError` for an unregistered field — the check refuses
    to guess at a field's allowance rather than defaulting to permissive or
    strict (issue athenaeum#963's AC: "the check refuses an unregistered
    field rather than guessing").
    """
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"unregistered surface-divergence field {name!r}; registered "
            f"fields: {field_names()}"
        ) from None


__all__ = [
    "EXIT_DIVERGED",
    "EXIT_SURFACE_UNREADABLE",
    "FieldSpec",
    "field_names",
    "get_field",
]
