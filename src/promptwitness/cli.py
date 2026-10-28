# SPDX-License-Identifier: Apache-2.0
"""``promptwitness`` console script (athenaeum#2147).

Layering: L2 (CLI/presentation — depends on :mod:`promptwitness.ledger`,
:mod:`promptwitness.canon`, and :mod:`promptwitness.anchor`). Standard
library only at import time; no ``athenaeum`` import.

``status`` (athenaeum#2147) and ``anchor`` (athenaeum#2151) are implemented
in this slice. ``hook claude-code`` (athenaeum#2149) and ``keygen``
(athenaeum#2150) are out of scope here — their subcommands are added by
those issues, which extend :func:`build_parser` the same way ``status`` and
``anchor`` were added: by registering another subparser in this module.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

from promptwitness import __version__, canon, ledger
from promptwitness import anchor as anchor_mod

#: Matches a subject string that names an actual ledger line
#: (``witness:<ledger_id>:<seq>``), as opposed to an arbitrary caller-chosen
#: label. Used by :func:`_digest_for_subject` to resolve the digest from
#: that line's own hash rather than hashing the subject string itself.
_WITNESS_SUBJECT_RE = re.compile(r"^witness:(?P<ledger_id>[^:]+):(?P<seq>\d+)$")


def build_parser() -> argparse.ArgumentParser:
    """Build the real ``argparse`` tree — the single source of truth both
    ``main()`` and ``scripts/gen_cli_reference.py`` consume (mirrors
    ``athenaeum.cli.build_parser()``'s own role for the ``athenaeum``
    console script)."""
    parser = argparse.ArgumentParser(
        prog="promptwitness",
        description="Harness-agnostic prompt witness ledger: hash-chained, no prompt text, ever.",
    )
    sub = parser.add_subparsers(dest="command")

    status = sub.add_parser(
        "status",
        help="Print the ledger head seq, chain_ok, partitions, and key strength.",
    )
    status.add_argument(
        "--harness",
        default="claude-code",
        help="Harness name the ledger directory is namespaced under.",
    )
    status.add_argument(
        "--dir",
        default=None,
        help="Override the ledger directory (else PROMPTWITNESS_DIR / the XDG default).",
    )
    status.set_defaults(func=_cmd_status)

    anchor_parser = sub.add_parser(
        "anchor",
        help="Anchor the ledger head (or --subject) with local-clock and OpenTimestamps.",
    )
    anchor_parser.add_argument(
        "--harness",
        default="claude-code",
        help="Harness name the ledger directory is namespaced under.",
    )
    anchor_parser.add_argument(
        "--dir",
        default=None,
        help="Override the ledger directory (else PROMPTWITNESS_DIR / the XDG default).",
    )
    anchor_parser.add_argument(
        "--subject",
        default=None,
        help="Subject to anchor (default: witness:<ledger_id>:<seq> of the current chain head).",
    )
    anchor_parser.add_argument(
        "--upgrade",
        action="store_true",
        help="Ask the OpenTimestamps calendar whether a prior pending proof for this "
        "subject has since been confirmed in Bitcoin, instead of submitting a new stamp.",
    )
    anchor_parser.add_argument(
        "--verify",
        action="store_true",
        help="Run full Bitcoin verification of the OpenTimestamps attestation after anchoring.",
    )
    anchor_parser.add_argument(
        "--block-source",
        default=None,
        help="Esplora-compatible block explorer URL for --verify, or 'offline' for no "
        "network access at all (else PROMPTWITNESS_OTS_BLOCK_SOURCE / a public default).",
    )
    anchor_parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Per-adapter network timeout in seconds (default: 10.0).",
    )
    anchor_parser.set_defaults(func=_cmd_anchor)

    return parser


def _cmd_status(args: argparse.Namespace) -> int:
    """Print ``{seq, chain_ok, partitions, key_strength}``. Never prints
    digests or any prompt-derived value — only structural ledger state."""
    ledger_dir = ledger.resolve_ledger_dir(args.harness, override=args.dir)
    partitions: list[str] = []
    head_seq: int | None = None
    key_strength = "none"
    if ledger_dir.is_dir():
        partitions = ledger.list_partitions(ledger_dir)
        for line in ledger.iter_lines(ledger_dir):
            head_seq = line.seq
            key_strength = line.key.strength
    verification = ledger.verify_chain(ledger_dir)
    payload: dict[str, Any] = {
        "ledger_dir": str(ledger_dir),
        "seq": head_seq,
        "chain_ok": verification.ok,
        "first_break_seq": verification.first_break_seq,
        "partitions": partitions,
        "key_strength": key_strength,
    }
    print(json.dumps(payload, sort_keys=True))
    return 0


def _digest_for_subject(ledger_dir: Any, subject: str) -> str:
    """The digest *subject* commits to.

    **Invariant: a subject's digest is a function of the subject, never of
    when it was resolved.** For a subject shaped like
    ``witness:<ledger_id>:<seq>`` that names an actual ledger line, the
    digest is that line's own ``line_hash()`` — the same value regardless
    of whether this line is still the chain head (anchoring itself appends
    lines, so by the time a second CLI pass asks, it usually is not).
    Any other subject — including one shaped like a witness subject but
    naming a line that does not exist in THIS ledger — hashes the subject
    string itself via ``canon.digest``, same as always.
    """
    match = _WITNESS_SUBJECT_RE.match(subject)
    if match:
        target_ledger_id = match.group("ledger_id")
        target_seq = int(match.group("seq"))
        for line in ledger.iter_lines(ledger_dir):
            if line.ledger_id == target_ledger_id and line.seq == target_seq:
                return line.line_hash()
    return canon.digest(subject)


def _resolve_subject_and_digest(
    ledger_dir: Any, subject_arg: str | None
) -> tuple[str, str] | tuple[None, None]:
    """``(subject, digest)`` to anchor.

    An explicit ``--subject`` resolves its digest via
    :func:`_digest_for_subject`. The default anchors the chain head:
    ``witness:<ledger_id>:<seq>`` committing to that line's own
    ``line_hash()`` (the design doc's "Subject | Digest" table) — the same
    value :func:`_digest_for_subject` would compute for that same subject
    string later. Returns ``(None, None)`` when no subject was given and
    the ledger is empty — there is nothing to anchor yet.
    """
    if subject_arg:
        return subject_arg, _digest_for_subject(ledger_dir, subject_arg)
    head: ledger.WitnessLine | None = None
    for line in ledger.iter_lines(ledger_dir):
        head = line
    if head is None:
        return None, None
    return f"witness:{head.ledger_id}:{head.seq}", head.line_hash()


def _find_latest_ots_attestation(ledger_dir: Any, subject: str) -> anchor_mod.Attestation | None:
    """The most recent OpenTimestamps :class:`~promptwitness.anchor.Attestation`
    previously recorded for *subject*, reconstructed from the ledger's own
    ``anchor`` lines — the ledger is the persisted attestation history, so
    no separate side-state file is needed.
    """
    latest: anchor_mod.Attestation | None = None
    for line in ledger.iter_lines(ledger_dir):
        if line.kind != "anchor" or line.extra.get("subject") != subject:
            continue
        raw_att = line.extra.get("attestation")
        ots_source = anchor_mod.OpenTimestampsAnchorer.source
        if not isinstance(raw_att, dict) or raw_att.get("source") != ots_source:
            continue
        latest = anchor_mod.Attestation.from_dict(raw_att)
    return latest


def _find_latest_pending_ots_attestation(
    ledger_dir: Any,
) -> tuple[str, str, anchor_mod.Attestation] | None:
    """``(subject, digest, attestation)`` of whichever subject was most
    recently touched by an OpenTimestamps anchor pass AND is still
    ``pending``, scanning every subject in the ledger — not just the
    current chain head.

    Used by ``anchor --upgrade`` with no explicit ``--subject``: anchoring
    itself appends lines, which moves the chain head, so re-deriving "the
    subject to upgrade" from the (now different) head would target a
    subject that was never anchored and could never upgrade (review
    finding #1 on PR athenaeum#2168). Each subject's status is last-write-wins (a
    later ``complete``/``failed`` line for the same subject clears it from
    consideration here); among subjects currently pending, the one with the
    highest ``seq`` — the most recently touched — is returned.
    """
    latest_by_subject: dict[str, tuple[int, str, anchor_mod.Attestation]] = {}
    ots_source = anchor_mod.OpenTimestampsAnchorer.source
    for line in ledger.iter_lines(ledger_dir):
        if line.kind != "anchor":
            continue
        subject = line.extra.get("subject")
        digest = line.extra.get("digest")
        raw_att = line.extra.get("attestation")
        if not subject or not digest or not isinstance(raw_att, dict):
            continue
        if raw_att.get("source") != ots_source:
            continue
        latest_by_subject[subject] = (line.seq, digest, anchor_mod.Attestation.from_dict(raw_att))

    pending = [
        (seq, subject, digest, att)
        for subject, (seq, digest, att) in latest_by_subject.items()
        if att.status == "pending"
    ]
    if not pending:
        return None
    pending.sort(key=lambda item: item[0])
    _, subject, digest, att = pending[-1]
    return subject, digest, att


def _append_anchor_line(
    ledger_dir: Any,
    *,
    harness: str,
    subject: str,
    digest: str,
    attestation: anchor_mod.Attestation,
    op: str,
) -> None:
    ledger.append_line(
        ledger_dir,
        kind="anchor",
        harness=harness,
        harness_version="",
        hook_version=f"promptwitness {__version__}",
        extra={
            "subject": subject,
            "digest": digest,
            "op": op,
            "attestation": attestation.to_dict(),
        },
    )


def _cmd_anchor(args: argparse.Namespace) -> int:
    """Anchor the chain head (or ``--subject``) with every configured
    adapter. :class:`~promptwitness.anchor.LocalClockAnchorer` always runs
    and always appends an ``anchor`` line, even when OpenTimestamps fails
    entirely — the mandatory-fallback acceptance criterion.

    ``--upgrade`` with no ``--subject`` targets whichever subject is still
    ``pending`` from a previous pass (:func:`_find_latest_pending_ots_attestation`)
    — never the current chain head, which anchoring itself just moved
    (review finding #1 on PR athenaeum#2168). ``--upgrade`` against a subject whose
    OpenTimestamps attestation is already ``complete`` is a no-op that
    reports the existing attestation: re-anchoring would silently discard
    the one Bitcoin-confirmed proof this design produces (review finding
    #2; :meth:`~promptwitness.anchor.OpenTimestampsAnchorer.anchor` also
    refuses this at the write site, independently).

    ``--verify`` runs full Bitcoin verification afterward (a read-only
    report — it does not itself append a ledger line).
    """
    ledger_dir = ledger.resolve_ledger_dir(args.harness, override=args.dir)

    subject: str | None
    digest: str | None
    pending: anchor_mod.Attestation | None = None

    found = None
    if args.upgrade and not args.subject:
        found = _find_latest_pending_ots_attestation(ledger_dir)
    if found is not None:
        subject, digest, pending = found
    else:
        subject, digest = _resolve_subject_and_digest(ledger_dir, args.subject)

    if subject is None or digest is None:
        print(
            json.dumps({"error": "ledger is empty; nothing to anchor (pass --subject)"}),
            file=sys.stderr,
        )
        return 1
    digest_bytes = anchor_mod.digest_bytes(digest)

    results: list[dict[str, Any]] = []

    local = anchor_mod.LocalClockAnchorer()
    local_att = local.anchor(digest_bytes, subject, timeout=args.timeout)
    _append_anchor_line(
        ledger_dir,
        harness=args.harness,
        subject=subject,
        digest=digest,
        attestation=local_att,
        op="anchor",
    )
    results.append({"subject": subject, "op": "anchor", "attestation": local_att.to_dict()})

    ots = anchor_mod.OpenTimestampsAnchorer(
        ledger_dir, block_source=anchor_mod.resolve_block_source(args.block_source)
    )
    existing = pending if pending is not None else _find_latest_ots_attestation(ledger_dir, subject)

    if args.upgrade and existing is not None and existing.status == "complete":
        # Already the strongest evidence this design produces -- report it,
        # touch nothing (never re-submit, never overwrite the proof file).
        ots_att = existing
        ots_op = "upgrade"
    elif args.upgrade and existing is not None and existing.status == "pending":
        ots_att = ots.upgrade(existing)
        ots_op = "upgrade"
    else:
        ots_att = ots.anchor(digest_bytes, subject, timeout=args.timeout)
        ots_op = "anchor"
    _append_anchor_line(
        ledger_dir,
        harness=args.harness,
        subject=subject,
        digest=digest,
        attestation=ots_att,
        op=ots_op,
    )
    results.append({"subject": subject, "op": ots_op, "attestation": ots_att.to_dict()})

    if args.verify:
        verify_result = ots.verify(ots_att, digest_bytes)
        verify_payload = {
            "subject": subject,
            "op": "verify",
            "verify_result": verify_result.to_dict(),
        }
        results.append(verify_payload)

    print(json.dumps({"results": results}, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 1
    return int(func(args))


if __name__ == "__main__":
    sys.exit(main())
