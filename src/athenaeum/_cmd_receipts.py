# SPDX-License-Identifier: Apache-2.0
"""``athenaeum receipts {seal,verify,anchor,attest-readout}`` — transcript-receipt
sealing, anchoring and witness-readout CLI (issues athenaeum#2039,
athenaeum#2040 and athenaeum#2160).

- ``seal``    sign every unsealed range in every receipt partition
              (:func:`athenaeum.receipt_sealers.seal_all_unsealed`). Needs the
              configured key provider to be REACHABLE, so this is a
              supervised or batched step, never part of the unattended
              nightly — which is exactly what makes a seal evidence that a
              credentialed step happened.
- ``verify``  check every stored seal against the live partition content and
              report every range that has no seal
              (:func:`athenaeum.receipt_sealers.verify_all`). Needs only the
              PUBLIC key, so it runs anywhere that is configured.
- ``anchor``  anchor every receipt partition that changed since its own last
              anchor, plus the witness chain head, upgrade pending
              OpenTimestamps proofs, and mirror the resulting witness
              ``anchor`` lines into ``wiki/_witness_anchors/``
              (:func:`athenaeum.witness_anchor.run_nightly_anchor`). This is
              the same pass the nightly librarian run performs at the end of
              its own run, offered here so an operator can run it on demand.
              Unlike ``seal``/``verify`` it needs NO key — it needs the run
              LOCK, because it writes into the knowledge repo.

- ``attest-readout``
              report witnessed hits and every provenance miss as an
              INCIDENT, from the receipt ledger and the witness index only
              (:func:`athenaeum.witness_readout.build_readout`). Read-only:
              it needs no key and takes NO run lock, which is why it is the
              one verb here that cannot return ``EXIT_LOCK_HELD``. Exits
              :data:`INCIDENT_EXIT` when any incident exists, so a nightly
              or a wrapper can gate on it.

**No plaintext in any output.** Both verbs report a partition month and a
line range and nothing else — receipt content never reaches this layer. The
receipt ledger carries only digests to begin with, but a human utterance is
short enough that a published digest is dictionary-attack-recoverable, so the
no-plaintext rule extends to anything derived from one. ``attest-readout``
holds to the same rule: counts, enum labels, session ids and
``witness_ref`` coordinates only (``tests/test_witness_readout.py::
TestNoPlaintext``).

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from athenaeum._cli_shared import (
    _acquire_or_exit,
    _add_lock_args,
    _resolve_knowledge_root,
    _resolve_wiki_root,
)
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT, load_config

#: Exit code for `verify` when any seal failed or any range is unsealed. A
#: distinct non-zero code (not 1) so a wrapper can tell "the ledger has a
#: problem" from "the command itself failed".
VERIFY_PROBLEM_EXIT = 3

#: Exit code for `seal` when the key provider could not be reached. Not an
#: error in the usual sense — the operator ran it on a machine without the
#: credential — so it is reported distinctly rather than as a crash.
KEY_UNAVAILABLE_EXIT = 4

#: Exit code for `attest-readout` when ANY incident exists — a non-`flag-off`
#: provenance miss, a hook-error class, or a non-zero `session-close` deficit
#: (issue athenaeum#2160, operator ruling 10: a miss is an incident, not a
#: rate). Distinct from `2` (usage), `3` (a ledger seal problem) and `4` (no
#: key), so a nightly wrapper can tell "provenance needs a human" from the
#: three conditions that already had codes. There is deliberately no
#: threshold: one incident is enough.
INCIDENT_EXIT = 5


def _sealer_for(args: argparse.Namespace) -> object:
    """Build the configured sealer, refusing a `noop` one for these verbs."""
    from athenaeum.receipt_sealers import SigningSealer, select_sealer

    config = load_config(_resolve_knowledge_root(args))
    sealer = select_sealer(config)
    if not isinstance(sealer, SigningSealer):
        print(
            "receipts: librarian.receipt_sealer is not `signing`; there is "
            "nothing to seal or verify. Set it (and the receipt_signing "
            "block) before running this command.",
            file=sys.stderr,
        )
        return None
    return sealer


def _cmd_seal(args: argparse.Namespace) -> int:
    from athenaeum.receipt_sealers import seal_all_unsealed

    sealer = _sealer_for(args)
    if sealer is None:
        return 2
    report = seal_all_unsealed(_resolve_wiki_root(args), sealer)  # type: ignore[arg-type]

    if args.json:
        sys.stdout.write(json.dumps(report.to_dict()) + "\n")
    else:
        for ref in report.sealed:
            print(f"sealed:   {ref}")
        print(
            f"receipts seal: {len(report.sealed)} range(s) sealed, "
            f"{report.already_sealed} already sealed"
        )
        if report.unavailable:
            print(f"receipts seal: {report.unavailable}", file=sys.stderr)
    return KEY_UNAVAILABLE_EXIT if report.unavailable else 0


def _cmd_verify(args: argparse.Namespace) -> int:
    from athenaeum.receipt_sealers import verify_all

    sealer = _sealer_for(args)
    if sealer is None:
        return 2
    report = verify_all(_resolve_wiki_root(args), sealer)  # type: ignore[arg-type]

    if args.json:
        sys.stdout.write(json.dumps(report.to_dict()) + "\n")
        return 0 if report.ok else VERIFY_PROBLEM_EXIT

    for ref in report.failed:
        print(f"FAILED:   {ref}")
    for ref in report.unsealed:
        print(f"unsealed: {ref}")
    print(
        f"receipts verify: {len(report.verified)} verified, "
        f"{len(report.failed)} failed, {len(report.unsealed)} unsealed range(s)"
    )
    return 0 if report.ok else VERIFY_PROBLEM_EXIT


def _cmd_anchor(args: argparse.Namespace) -> int:
    """Run the anchoring pass under the run lock (issue athenaeum#2040).

    Takes the SAME ``<knowledge_root>/.athenaeum.lock`` the nightly run
    takes, because it writes into the knowledge repo — the anchor-record
    store and the off-machine mirror. Needs no signing key at all: a
    ``local-clock`` attestation is mandatory and cannot fail, so this
    command always produces a record, with or without network.
    """
    from athenaeum.witness_anchor import run_nightly_anchor
    from athenaeum.witness_attestation import resolve_default_witness_dir

    knowledge_root = _resolve_knowledge_root(args)
    config = load_config(knowledge_root)
    acquired = _acquire_or_exit(knowledge_root, args, config)
    if isinstance(acquired, int):
        return acquired
    try:
        report = run_nightly_anchor(
            resolve_default_witness_dir(),
            _resolve_wiki_root(args),
            config=config,
            lock=acquired,
        )
    finally:
        acquired.release()

    if args.json:
        sys.stdout.write(json.dumps(report.to_dict()) + "\n")
        return 0
    for month in report.anchored:
        print(f"anchored:  {month}")
    for month in report.imported:
        print(f"imported:  {month} (attestation already held for this digest)")
    for month in report.unchanged:
        print(f"unchanged: {month}")
    if report.head_subject:
        print(f"head:      {report.head_subject}")
    for subject in report.upgraded:
        print(f"upgraded:  {subject}")
    print(
        f"receipts anchor: {len(report.anchored)} anchored, "
        f"{len(report.imported)} imported, {len(report.unchanged)} unchanged, "
        f"{len(report.upgraded)} upgraded, {report.mirrored} record(s) mirrored"
    )
    return 0


def _month(value: str) -> str:
    """Argparse type for ``--since YYYY-MM``.

    Validated loudly rather than silently ignored: a mistyped window would
    otherwise quietly widen the readout to every month, which is the one
    failure mode an operator would not notice in the output.
    """
    text = value.strip()
    if len(text) != 7 or text[4] != "-" or not text[:4].isdigit() or not text[5:].isdigit():
        raise argparse.ArgumentTypeError(f"expected a YYYY-MM month, got {value!r}")
    if not 1 <= int(text[5:]) <= 12:
        raise argparse.ArgumentTypeError(f"month out of range in {value!r}")
    return text


def _cmd_attest_readout(args: argparse.Namespace) -> int:
    """Report witnessed hits and every miss as an incident (athenaeum#2160).

    Read-only, so it takes NO run lock (hence no ``_acquire_or_exit`` call
    and no entry in ``docs/reference/exit-codes.md``'s ``EXIT_LOCK_HELD``
    producer list) and needs no key. Both renderings come off ONE
    :class:`athenaeum.witness_readout.ReadoutSummary`, so ``--json`` and the
    text form cannot disagree about a number.
    """
    from athenaeum.witness_readout import build_readout, render_text

    summary = build_readout(_resolve_wiki_root(args), since=getattr(args, "since", None))
    if args.json:
        sys.stdout.write(json.dumps(summary.to_dict()) + "\n")
    else:
        sys.stdout.write(render_text(summary))
    return 0 if summary.ok else INCIDENT_EXIT


def _cmd_receipts_dispatch(args: argparse.Namespace) -> int:
    target = getattr(args, "receipts_target", None)
    if target == "seal":
        return _cmd_seal(args)
    if target == "verify":
        return _cmd_verify(args)
    if target == "anchor":
        return _cmd_anchor(args)
    if target == "attest-readout":
        return _cmd_attest_readout(args)
    print(
        "usage: athenaeum receipts {seal,verify,anchor,attest-readout} [...]",
        file=sys.stderr,
    )
    return 2


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of plain text.",
    )


def add_receipts_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum receipts`` and its subcommands on *subparsers*."""
    r_parser = subparsers.add_parser(
        "receipts",
        help=(
            "Seal, verify and anchor the transcript-receipt ledger. `seal` "
            "signs every unsealed line range and needs the configured "
            "signing key to be reachable; `verify` checks every seal against "
            "the live ledger and needs only the public key; `anchor` "
            "externally timestamps every changed partition and mirrors the "
            "anchor records off-machine, and needs the run lock; "
            "`attest-readout` reports witnessed hits and every provenance "
            "miss as an incident, read-only. Output is partitions, line "
            "ranges, counts and enum labels only, never receipt content."
        ),
    )
    r_parser.set_defaults(func=_cmd_receipts_dispatch)
    r_sub = r_parser.add_subparsers(dest="receipts_target")

    seal_p = r_sub.add_parser(
        "seal",
        help=(
            "Sign every unsealed range in every receipt partition. Requires "
            "a reachable signing key, so this is a supervised or batched "
            "step rather than part of the nightly run."
        ),
    )
    _add_common(seal_p)
    seal_p.set_defaults(func=_cmd_seal)

    verify_p = r_sub.add_parser(
        "verify",
        help=(
            "Report unsealed ranges and seals that no longer verify, by "
            "partition and line range. Needs only the public key."
        ),
    )
    _add_common(verify_p)
    verify_p.set_defaults(func=_cmd_verify)

    anchor_p = r_sub.add_parser(
        "anchor",
        help=(
            "Anchor every receipt partition that changed since its own last "
            "anchor and the witness chain head, upgrade pending "
            "OpenTimestamps proofs, and mirror the anchor records into "
            "wiki/_witness_anchors/ so they leave the machine with the "
            "pushed knowledge repo. Takes the run lock; needs no signing "
            "key. The nightly run does this itself at the end of its run."
        ),
    )
    _add_common(anchor_p)
    _add_lock_args(anchor_p)
    anchor_p.set_defaults(func=_cmd_anchor)

    readout_p = r_sub.add_parser(
        "attest-readout",
        help=(
            "Report witnessed hits and every provenance miss as an "
            "incident, from the receipt ledger and the witness index only. "
            "Also reports the unsigned-line count, anchor coverage, the "
            "witness hook-error classes and the session-close deficit. "
            "Read-only: no key, no run lock. Exits 5 when any incident "
            "exists; there is no tolerated miss rate and no window beyond "
            "--since."
        ),
    )
    _add_common(readout_p)
    readout_p.add_argument(
        "--since",
        type=_month,
        default=None,
        help=(
            "Only count records from this YYYY-MM month onwards (receipt "
            "partition month / witness line month). Default: every month."
        ),
    )
    readout_p.set_defaults(func=_cmd_attest_readout)


__all__ = ["add_receipts_subparser"]
