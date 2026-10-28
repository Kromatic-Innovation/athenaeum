# SPDX-License-Identifier: Apache-2.0
"""``promptwitness`` console script (athenaeum#2147, athenaeum#2150).

Layering: L2 (CLI/presentation — depends on :mod:`promptwitness.ledger`,
:mod:`promptwitness.signing` and :mod:`promptwitness.canon`). Standard
library only at import time; no ``athenaeum`` import, and no ``cryptography``
import either — :mod:`promptwitness.signing` itself only imports
``cryptography`` lazily inside the functions that need it, so importing this
module (and therefore ``signing``) at module level costs nothing on an
install without the ``witness-sign`` extra.

``status`` (athenaeum#2147), ``keygen`` (athenaeum#2150, extended with
``--provider os-keystore`` by athenaeum#2152) and ``hook claude-code``
(athenaeum#2149) are implemented. ``anchor`` (athenaeum#2151) is out of
scope here — its subcommand is added by that issue, which extends
:func:`build_parser` the same way the three above were added: by
registering another subparser in this module.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from promptwitness import ledger, signing
from promptwitness.harness import claude_code


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
        help="Generate an Ed25519 signing key, in a file (mode 0600) or an OS keystore.",
    )
    keygen.add_argument(
        "--harness",
        default="claude-code",
        help="Harness name the default key path is namespaced under.",
    )
    keygen.add_argument(
        "--provider",
        choices=["software-file", "os-keystore"],
        default="software-file",
        help="Where to keep the key: a PEM file on disk, or the OS keystore "
        "(encrypted at rest; your own session can still read it).",
    )
    keygen.add_argument(
        "--keystore-backend",
        choices=list(signing.KEYSTORE_BACKENDS),
        default=None,
        help="Keystore to use with --provider os-keystore (else "
        "PROMPTWITNESS_KEYSTORE_BACKEND, else this platform's).",
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
        help="Overwrite an existing key instead of refusing.",
    )
    keygen.set_defaults(func=_cmd_keygen)

    hook = sub.add_parser(
        "hook",
        help="Harness hook entry points (athenaeum#2149).",
    )
    hook_sub = hook.add_subparsers(dest="harness_command")
    hook_claude_code = hook_sub.add_parser(
        "claude-code",
        help="Append a witness line for a Claude Code hook event.",
    )
    hook_claude_code.add_argument(
        "--event",
        required=True,
        choices=["session-end"],
        help="Which Claude Code hook fired. Only 'session-end' is implemented "
        "here -- the per-turn 'UserPromptSubmit' path is wired directly "
        "through athenaeum.claude_code_adapter, in-process, not through "
        "this console script.",
    )
    hook_claude_code.set_defaults(func=_cmd_hook_claude_code)

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
    """Generate a new Ed25519 signing key and print where it went plus
    ``{key_id, algorithm, strength}`` -- never the key material itself, on
    stdout, stderr, or (this command logs nothing) a log record.

    ``--provider os-keystore`` stores the seed in the OS keystore instead of
    a PEM file; there is then no ``key_path`` to report, so the payload
    carries ``keystore_backend`` in its place."""
    if args.provider == "os-keystore":
        return _keygen_os_keystore(args)
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

def _cmd_hook_claude_code(args: argparse.Namespace) -> int:
    """``promptwitness hook claude-code --event session-end``: append a
    ``kind="session-close"`` witness line from the process environment (no
    stdin payload is required -- :func:`promptwitness.harness.claude_code.session_close`
    reads ``CLAUDE_CODE_SESSION_ID`` directly). A no-op (exit 0, no line
    written) when that env var is absent.

    Unlike the per-turn hook path wired through ``athenaeum.claude_code_adapter``,
    this standalone console script carries no silent-no-op guard of its
    own: a real failure here propagates as a normal CLI error (non-zero
    exit, traceback on stderr), for an operator invoking it directly.
    """
    if args.event == "session-end":
        claude_code.session_close(os.environ)
        return 0
    return 1


def _keygen_os_keystore(args: argparse.Namespace) -> int:
    """The ``--provider os-keystore`` half of :func:`_cmd_keygen`.

    Both failure modes report a one-line JSON error on stderr and exit 1
    rather than raising: an existing item (replacing a signing key makes
    every line it signed permanently unverifiable) and an unreachable
    keystore (no ``security``/``secret-tool``, a locked keystore, a DPAPI
    refusal)."""
    try:
        backend = signing.resolve_keystore_backend(
            args.harness, backend=args.keystore_backend
        )
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    try:
        provider = signing.generate_and_store_keystore_key(
            backend, replace=bool(args.force)
        )
    except FileExistsError as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    except ledger.KeyUnavailable as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    try:
        key_id = provider.key_id()
    except ledger.KeyUnavailable as exc:
        # The key IS stored at this point (`generate_and_store_keystore_key`
        # reads it back before returning), so say so rather than letting a
        # traceback imply nothing happened.
        print(
            json.dumps(
                {
                    "error": f"the key was stored but could not be read back: {exc}",
                    "keystore_backend": backend.name(),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    payload: dict[str, Any] = {
        "keystore_backend": backend.name(),
        "key_id": key_id,
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
