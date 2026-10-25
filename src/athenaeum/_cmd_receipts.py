# SPDX-License-Identifier: Apache-2.0
"""``athenaeum receipts {seal,verify}`` — transcript-receipt sealing CLI (issue athenaeum#2039).

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

**No plaintext in either output.** Both verbs report a partition month and a
line range and nothing else — receipt content never reaches this layer. The
receipt ledger carries only digests to begin with, but a human utterance is
short enough that a published digest is dictionary-attack-recoverable, so the
no-plaintext rule extends to anything derived from one.

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from athenaeum._cli_shared import _resolve_knowledge_root, _resolve_wiki_root
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT, load_config

#: Exit code for `verify` when any seal failed or any range is unsealed. A
#: distinct non-zero code (not 1) so a wrapper can tell "the ledger has a
#: problem" from "the command itself failed".
VERIFY_PROBLEM_EXIT = 3

#: Exit code for `seal` when the key provider could not be reached. Not an
#: error in the usual sense — the operator ran it on a machine without the
#: credential — so it is reported distinctly rather than as a crash.
KEY_UNAVAILABLE_EXIT = 4


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


def _cmd_receipts_dispatch(args: argparse.Namespace) -> int:
    target = getattr(args, "receipts_target", None)
    if target == "seal":
        return _cmd_seal(args)
    if target == "verify":
        return _cmd_verify(args)
    print("usage: athenaeum receipts {seal,verify} [...]", file=sys.stderr)
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
            "Seal and verify the transcript-receipt ledger. `seal` signs "
            "every unsealed line range and needs the configured signing key "
            "to be reachable; `verify` checks every seal against the live "
            "ledger and needs only the public key. Output is partitions and "
            "line ranges only, never receipt content."
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


__all__ = ["add_receipts_subparser"]
