# SPDX-License-Identifier: Apache-2.0
"""``promptwitness`` console script (athenaeum#2147, athenaeum#2150).

Layering: L2 (CLI/presentation — depends on :mod:`promptwitness.ledger`,
:mod:`promptwitness.signing` and :mod:`promptwitness.canon`). Standard
library only at import time; no ``athenaeum`` import, and no ``cryptography``
import either — :mod:`promptwitness.signing` itself only imports
``cryptography`` lazily inside the functions that need it, so importing this
module (and therefore ``signing``) at module level costs nothing on an
install without the ``witness-sign`` extra.

``status`` (athenaeum#2147) and ``keygen`` (athenaeum#2150) are implemented
in this slice. ``hook claude-code`` (athenaeum#2149) and ``anchor``
(athenaeum#2151) are out of scope here — their subcommands are added by
those issues, which extend :func:`build_parser` the same way ``status`` and
``keygen`` were added: by registering another subparser in this module.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from promptwitness import ledger, signing


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

    keygen = sub.add_parser(
        "keygen",
        help="Generate a software-file Ed25519 signing key, mode 0600.",
    )
    keygen.add_argument(
        "--harness",
        default="claude-code",
        help="Harness name the default key path is namespaced under.",
    )
    keygen.add_argument(
        "--key-path",
        default=None,
        help="Override the key file path (else PROMPTWITNESS_KEY_PATH, else "
        "<ledger_dir>/keys/ed25519.pem).",
    )
    keygen.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing key file instead of refusing.",
    )
    keygen.set_defaults(func=_cmd_keygen)

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


def _cmd_keygen(args: argparse.Namespace) -> int:
    """Generate a new ``software-file`` Ed25519 key and print ``{key_path,
    key_id, algorithm, strength}`` -- never the key material itself, on
    stdout, stderr, or (this command logs nothing) a log record."""
    key_path = signing.resolve_key_path(args.harness, override=args.key_path)
    if key_path.exists():
        if not args.force:
            print(
                json.dumps({"error": f"key already exists at {key_path}"}, sort_keys=True),
                file=sys.stderr,
            )
            return 1
    # `--force` does NOT unlink first: `generate_and_write_key(replace=True)`
    # writes a sibling temp file and `os.replace`s it into position, so a
    # failure mid-generation leaves the existing key intact. Losing a signing
    # key makes every line it signed permanently unverifiable.
    provider = signing.generate_and_write_key(key_path, replace=bool(args.force))
    payload: dict[str, Any] = {
        "key_path": str(key_path),
        "key_id": provider.key_id(),
        "algorithm": provider.algorithm(),
        "strength": provider.strength(),
    }
    print(json.dumps(payload, sort_keys=True))
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
