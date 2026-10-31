# SPDX-License-Identifier: Apache-2.0
"""``athenaeum attribution {show,place}`` -- the human end of the attribution
seam (issue athenaeum#2212).

Design ``docs/design/attribution-verification.md`` §8.1 names exactly these
two commands:

- ``show <suspect-id>`` prints one parked suspect, and is **the only place a
  raw contact value is shown**. It is the "one way out" a human who needs
  the value reads it through. ``athenaeum query``/``recall`` cannot answer
  this question: a suspect is not an entity, and may name a uid with no
  record and no page.
- ``place <suspect-id> --as own|other:<uid>|alias:<relation>|merge:<uid>``
  files a human answer through the SAME placement executor the model's
  answers go through (:func:`athenaeum.pii.place_contact_value`), with
  ``decided_by: human`` and -- per §8.1 -- no calibrated probability, model
  id or prompt sha in the provenance.

**Neither command opens the surface itself.** Both go through
:func:`athenaeum.pii_attribution.read_suspect`, which applies ``pii``'s
fail-closed readable-surface gate first, so an unreachable surface raises
rather than reporting "no such suspect"
(``docs/design/one-way-in-one-way-out.md`` §3: "a caller that opens a store
path itself is a defect, not a shortcut").

Exit codes (see ``docs/reference/exit-codes.md``):

    0  -- success.
    1  -- errors: no such suspect, a malformed ``--as``, an unknown uid, an
          unreadable excluded surface, or a placement the executor declined
          or refused to make.
    2  -- usage (no subcommand given).
    75 -- ``EXIT_LOCK_HELD``: ``place`` could not take the run lock.

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` -- see
``cli.py``'s module docstring.

Layering: L5.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from athenaeum._cli_shared import _acquire_or_exit, _add_lock_args
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT

if TYPE_CHECKING:
    from athenaeum.runlock import RunLock

#: ``--as`` prefixes that take an argument, and the verdict each selects.
#: Design §7.1's outcome table, minus the three verdicts no human answers
#: through this command: ``other-unknown`` is what a suspect already IS,
#: ``non-personal`` is a disposition the deterministic checks own, and
#: ``unclear`` is not an answer.
_AS_FORMS = "own | other:<uid> | alias:<relation> | merge:<uid>"


class _AsFormError(ValueError):
    """A ``--as`` value that is not one of :data:`_AS_FORMS`."""


def _parse_as_form(raw: str) -> tuple[str, str | None, str | None]:
    """Parse ``--as`` into ``(verdict, owner_uid, alias_relation)``.

    Strict, with no normalisation beyond stripping surrounding whitespace: a
    human answer that files the WRONG thing is exactly what this command
    exists to avoid, so an unrecognised form is rejected rather than guessed
    at. The relation vocabulary is :data:`athenaeum.pii.IDENTIFIER_RELATIONS`
    -- the four the design names, read from ``pii`` so the CLI cannot drift
    from the writer.

    Raises:
        _AsFormError: on an unknown form, a missing argument after the
            colon, or a relation outside the vocabulary.
    """
    from athenaeum.pii import IDENTIFIER_RELATIONS

    value = (raw or "").strip()
    if value == "own":
        return "own", None, None
    prefix, _, argument = value.partition(":")
    argument = argument.strip()
    if prefix not in ("other", "alias", "merge"):
        raise _AsFormError(f"unknown --as form {raw!r}; expected one of: {_AS_FORMS}")
    if not argument:
        expected = "a relation" if prefix == "alias" else "a uid"
        raise _AsFormError(f"--as {prefix}: needs {expected}; expected one of: {_AS_FORMS}")
    if prefix == "other":
        return "other-known", argument, None
    if prefix == "merge":
        return "same-entity", argument, None
    if argument not in IDENTIFIER_RELATIONS:
        raise _AsFormError(
            f"unknown identifier relation {argument!r}; expected one of "
            f"{list(IDENTIFIER_RELATIONS)}"
        )
    return "alias", None, argument


def _resolve_roots(args: argparse.Namespace) -> tuple[Path, dict[str, Any] | None, Path]:
    """``(knowledge_root, config, contacts_root)``, config-resolved.

    ``--contacts-root`` is accepted for the same reason
    ``_cmd_pii_restore`` accepts one: an operator inspecting a surface that
    is not the one this machine's config resolves to.
    """
    from athenaeum.config import load_config
    from athenaeum.pii import contacts_surface_root

    knowledge_root = (args.path or DEFAULT_KNOWLEDGE_ROOT).expanduser().resolve()
    config = load_config(knowledge_root)
    contacts_root = (
        args.contacts_root.expanduser().resolve()
        if getattr(args, "contacts_root", None)
        else contacts_surface_root(knowledge_root, config)
    )
    return knowledge_root, config, contacts_root


def _suspect_to_json(suspect: Any) -> dict[str, Any]:
    return {
        "suspect_id": suspect.suspect_id,
        "path": str(suspect.path),
        "value": suspect.value,
        "value_key": suspect.value_key,
        "target_uid": suspect.target_uid,
        "target_name": suspect.target_name,
        "writer": suspect.writer,
        "source": suspect.source_ref,
        "disposition": suspect.disposition,
        "checks": list(suspect.checks),
        "candidates": list(suspect.candidates),
        "shadow": suspect.shadow,
        "judgement": suspect.judgement,
    }


def _render_suspect(suspect: Any) -> str:
    lines = [
        f"## {suspect.suspect_id}",
        f"  value:       {suspect.value}",
        f"  value_key:   {suspect.value_key}",
        f"  target:      {suspect.target_name or '(unnamed)'} (uid={suspect.target_uid})",
        f"  writer:      {suspect.writer or '(unrecorded)'}",
        f"  source:      {suspect.source_ref if suspect.source_ref is not None else '(none)'}",
        f"  disposition: {suspect.disposition or '(unrecorded)'}",
        f"  checks:      {', '.join(suspect.checks) or '(none)'}",
        f"  candidates:  {', '.join(suspect.candidates) or '(none)'}",
        f"  shadow:      {suspect.shadow}",
        f"  path:        {suspect.path}",
    ]
    if suspect.judgement:
        lines.append("  judgement:")
        for key in sorted(suspect.judgement):
            lines.append(f"    {key}: {suspect.judgement[key]}")
    return "\n".join(lines)


def cmd_attribution(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum attribution {show,place}``."""
    target = getattr(args, "attribution_target", None)
    if target == "show":
        return _cmd_show(args)
    if target == "place":
        return _cmd_place(args)
    print(
        f"usage: athenaeum attribution {{show,place}} <suspect-id> [--as {_AS_FORMS}]",
        file=sys.stderr,
    )
    return 2


def _cmd_show(args: argparse.Namespace) -> int:
    from athenaeum.pii import ExcludedSurfaceUnavailable
    from athenaeum.pii_attribution import SuspectIdError, read_suspect

    _knowledge_root, _config, contacts_root = _resolve_roots(args)
    try:
        suspect = read_suspect(contacts_root, args.suspect_id)
    except SuspectIdError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ExcludedSurfaceUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if suspect is None:
        print(
            f"error: no parked suspect {args.suspect_id!r} under {contacts_root}",
            file=sys.stderr,
        )
        return 1
    if args.json:
        sys.stdout.write(json.dumps(_suspect_to_json(suspect)) + "\n")
    else:
        print(_render_suspect(suspect))
    return 0


def _merge_proposer(wiki_root: Path, suspect_id: str, owner_name: str, target_name: str) -> Any:
    """Build the L4 adapter from the executor's ``propose_merge`` onto
    ``pending_merges.write_pending_merge``.

    ``pii`` and ``pii_attribution`` are L3 and cannot reach
    :mod:`athenaeum.pending_merges` (L4), so the adapter lives here, at L5,
    where both are importable. Design §7.1: a ``same-entity`` answer is a
    dedupe question, so it goes to the EXISTING pending-merges queue and no
    contact value is written.

    The block body carries only the executor's ``rationale``, which already
    holds the MASKED value -- ``_pending_merges.md`` lives in the wiki, so
    the unmasked ``value`` the executor also passes is deliberately dropped
    here rather than rendered (design §3).

    ``confidence=1.0`` because this proposal is a human assertion, not a
    model score, matching ``_cmd_merges``'s own hand-filed proposals.
    """
    from athenaeum.models import EntityIndex
    from athenaeum.pending_merges import write_pending_merge

    def _propose(*, target_uid: str, owner_uid: str, value: str, rationale: str) -> Any:
        del value  # never enters the wiki (design §3); the rationale is masked
        index = EntityIndex(wiki_root)
        sources = [
            str(path)
            for path in (index.get_by_uid(target_uid), index.get_by_uid(owner_uid))
            if path is not None
        ]
        return write_pending_merge(
            wiki_root / "_pending_merges.md",
            merge_target_name=owner_name,
            display_name=owner_name,
            sources=sources,
            rationale=(
                f"{rationale} Filed by a human answering attribution suspect "
                f"{suspect_id} (`athenaeum attribution place --as merge:{owner_uid}`); "
                f"the value was headed for {target_name}."
            ),
            draft_merged_body="",
            confidence=1.0,
            write_kind=None,  # derived (athenaeum#748) — never taken from the caller
        )

    return _propose


def _cmd_place(args: argparse.Namespace) -> int:
    from athenaeum import pii, pii_attribution
    from athenaeum.models import EntityIndex
    from athenaeum.pii import ExcludedSurfaceUnavailable
    from athenaeum.store import now_iso

    knowledge_root, config, contacts_root = _resolve_roots(args)
    wiki_root = knowledge_root / "wiki"

    try:
        verdict, owner_uid, alias_relation = _parse_as_form(args.as_)
    except _AsFormError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        suspect = pii_attribution.read_suspect(contacts_root, args.suspect_id)
    except (pii_attribution.SuspectIdError, ExcludedSurfaceUnavailable) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if suspect is None:
        print(
            f"error: no parked suspect {args.suspect_id!r} under {contacts_root}",
            file=sys.stderr,
        )
        return 1

    index = EntityIndex(wiki_root) if wiki_root.is_dir() else None
    owner_name: str | None = None
    if owner_uid is not None:
        owner_page = index.get_by_uid(owner_uid) if index is not None else None
        if owner_page is None:
            print(
                f"error: uid {owner_uid!r} resolves to no wiki page under {wiki_root}; "
                "refusing to file a contact value against a uid that does not exist",
                file=sys.stderr,
            )
            return 1
        owner_meta = pii.read_bounce_record(owner_page)
        raw_name = owner_meta.get("name")
        owner_name = str(raw_name).strip() if isinstance(raw_name, str) else owner_uid

    context = pii_attribution.build_attribution_context(
        knowledge_root, config, entity_index=index, contacts_root=contacts_root
    )

    # Design §7.2's re-check, run HERE rather than injected. A `suspect` or
    # `non-personal` re-check must not block a human answer: the human IS
    # the authority the deterministic checks escalated to (design §8.1), and
    # design §9.4 names `place --as own` as the rollback for a value those
    # same checks withheld — which an injected re-check would simply
    # re-withhold, making the documented rollback impossible. So the checks
    # are run and REPORTED, and the placement proceeds with `recheck=None`,
    # which is exactly the "a caller that has already re-checked this exact
    # (value, uid) pair itself" case `place_contact_value` sanctions.
    recheck_uid = (
        owner_uid if verdict == "other-known" and owner_uid else suspect.target_uid
    )
    recheck = pii_attribution.check_contact_attribution(
        suspect.value,
        target_uid=recheck_uid,
        target_name=owner_name if verdict == "other-known" else suspect.target_name,
        target_type=None,
        source_ref=suspect.source_ref,
        writer=pii_attribution.WRITER_RE_ATTRIBUTION,
        context=context,
    )
    if recheck.disposition != pii_attribution.DISPOSITION_PASS:
        print(
            f"note: the deterministic checks still say "
            f"{recheck.disposition!r} for this value on {recheck_uid} "
            f"({', '.join(c.check_id for c in recheck.checks) or 'no checks'}); "
            "filing anyway, because a human answer is the authority those "
            "checks escalate to.",
            file=sys.stderr,
        )

    lock: "RunLock | int" = _acquire_or_exit(knowledge_root, args, config)
    if isinstance(lock, int):
        return lock
    try:
        placement = pii.place_contact_value(
            contacts_root,
            suspect.value,
            verdict=verdict,
            target_uid=suspect.target_uid,
            target_name=suspect.target_name,
            owner_uid=owner_uid,
            owner_name=owner_name,
            alias_relation=alias_relation,
            original_source=suspect.source_ref,
            writer=pii_attribution.WRITER_RE_ATTRIBUTION,
            # Design §8.1: a human answer is filed "with `decided_by: human`
            # in the provenance and no calibrated probability" — and, by the
            # same reasoning, no model id and no prompt sha: there was no
            # model call to attribute it to.
            model=None,
            prompt_sha256=None,
            calibrated_p=None,
            decided_by="human",
            observed_at=now_iso(),
            apply=True,
            recheck=None,
            propose_merge=(
                _merge_proposer(
                    wiki_root,
                    suspect.suspect_id,
                    owner_name or (owner_uid or ""),
                    suspect.target_name or suspect.target_uid,
                )
                if verdict == "same-entity"
                else None
            ),
        )
    finally:
        lock.release()

    if args.json:
        sys.stdout.write(
            json.dumps(
                {
                    "suspect_id": suspect.suspect_id,
                    "verdict": placement.verdict,
                    "outcome": placement.outcome,
                    "placed_uid": placement.placed_uid,
                    "field": placement.field,
                    "usage_class": placement.usage_class,
                    "path": str(placement.path) if placement.path else None,
                    "detail": placement.detail,
                }
            )
            + "\n"
        )
    else:
        print(
            f"{suspect.suspect_id}: {placement.verdict} -> {placement.outcome}"
            + (f" on {placement.placed_uid}" if placement.placed_uid else "")
            + (f" ({placement.field}, {placement.usage_class})" if placement.field else "")
        )
        if placement.detail:
            print(f"  {placement.detail}")

    if placement.outcome in (
        pii.PLACEMENT_FILED,
        pii.PLACEMENT_PARKED,
        pii.PLACEMENT_MERGE_PROPOSED,
        pii.PLACEMENT_LOGGED,
    ):
        return 0
    print(
        f"error: the answer was not filed ({placement.outcome}): {placement.detail}",
        file=sys.stderr,
    )
    return 1


def add_attribution_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum attribution`` and its two modes on *subparsers*."""
    parser = subparsers.add_parser(
        "attribution",
        help=(
            "Read and answer parked contact-value attribution suspects. "
            "`show` prints one suspect through the excluded-surface read gate "
            "(the only place a raw contact value is shown); `place` files a "
            "human answer through the placement executor."
        ),
    )
    parser.set_defaults(func=cmd_attribution)
    sub = parser.add_subparsers(dest="attribution_target")

    def _add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("suspect_id", help="Suspect id: <value_key>-<target_uid>.")
        p.add_argument(
            "--path",
            type=Path,
            default=DEFAULT_KNOWLEDGE_ROOT,
            help="Knowledge directory (default: ~/knowledge).",
        )
        p.add_argument(
            "--contacts-root",
            type=Path,
            default=None,
            help="Excluded 'pii' surface root, resolved from config by default.",
        )
        p.add_argument(
            "--json",
            action="store_true",
            help="Emit machine-readable JSON instead of plain text.",
        )

    show_p = sub.add_parser(
        "show",
        help=(
            "Print one parked suspect, including its raw contact value. This "
            "is the sanctioned read for that value."
        ),
    )
    _add_common(show_p)

    place_p = sub.add_parser(
        "place",
        help=(
            "File a human answer for one parked suspect through the placement "
            "executor, with decided_by: human and no calibrated probability."
        ),
    )
    _add_common(place_p)
    place_p.add_argument(
        "--as",
        dest="as_",
        required=True,
        metavar="ANSWER",
        help=(
            f"The answer to file: {_AS_FORMS}. `own` confirms the value is "
            "the target's; `other:<uid>` re-attributes it to that uid; "
            "`alias:<relation>` files it as an alias with one of the four "
            "identifier relations; `merge:<uid>` proposes a same-entity merge "
            "and writes no contact value."
        ),
    )
    _add_lock_args(place_p)
