# SPDX-License-Identifier: Apache-2.0
"""``promptwitness`` console script (athenaeum#2147, athenaeum#2149,
athenaeum#2150, athenaeum#2151, athenaeum#2152, athenaeum#2157,
athenaeum#2155).

Layering: L2 (CLI/presentation — depends on :mod:`promptwitness.ledger`,
:mod:`promptwitness.canon`, :mod:`promptwitness.signing`,
:mod:`promptwitness.anchor`, :mod:`promptwitness.anchor_rfc3161`,
:mod:`promptwitness.verify` and :mod:`promptwitness.demo`).
Standard library only at import time; no ``athenaeum`` import, and no
``cryptography`` or ``opentimestamps`` import either — :mod:`promptwitness.signing`
and :mod:`promptwitness.anchor` each import their optional dependency
lazily, inside the functions that need it, so importing this module costs
nothing on an install without the ``witness-sign`` / ``witness-anchor``
extras. :mod:`promptwitness.anchor_rfc3161` shells out to ``openssl``
rather than importing a third-party library, so it carries no such extra.

``status`` (athenaeum#2147), ``keygen`` (athenaeum#2150, extended with
``--provider os-keystore`` by athenaeum#2152), ``hook claude-code``
(athenaeum#2149), ``anchor`` (athenaeum#2151, extended with the optional
RFC 3161 second source by athenaeum#2157), ``verify`` and ``demo``
(athenaeum#2155) are all implemented here. Each was added the same way: by
registering another subparser in :func:`build_parser`, in this module.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from promptwitness import __version__, anchor_rfc3161, canon, ledger, signing
from promptwitness import anchor as anchor_mod
from promptwitness import demo as demo_mod
from promptwitness import verify as verify_mod
from promptwitness.harness import claude_code

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
    anchor_parser.add_argument(
        "--rfc3161-url",
        default=None,
        help="RFC 3161 TSA URL for an optional, immediate second attestation "
        "source (else PROMPTWITNESS_RFC3161_URL; no default -- an unconfigured "
        "URL means this adapter is not offered at all).",
    )
    anchor_parser.add_argument(
        "--rfc3161-ca-bundle",
        default=None,
        help="CA bundle path for --verify of the RFC 3161 attestation (else "
        "PROMPTWITNESS_RFC3161_CA_BUNDLE; no default).",
    )
    anchor_parser.set_defaults(func=_cmd_anchor)

    verify_parser = sub.add_parser(
        "verify",
        help="Verify a transcript against a witness ledger; exit 0 iff every "
        "human turn is witnessed, else 1.",
    )
    verify_parser.add_argument(
        "--transcript",
        required=True,
        help="Path to a transcript JSON file: a top-level array of "
        "{role, text, session, prompt_id} objects (role 'human' or "
        "anything else for machine-authored; see promptwitness.verify).",
    )
    verify_parser.add_argument(
        "--ledger",
        required=True,
        help="Ledger directory to verify the transcript against.",
    )
    verify_parser.add_argument(
        "--json",
        action="store_true",
        help="Print {ok, verdicts} as one JSON object instead of one line per turn.",
    )
    verify_parser.set_defaults(func=_cmd_verify)

    demo_parser = sub.add_parser(
        "demo",
        help="Render a static HTML side-by-side demo: a verified transcript "
        "next to a tampered one, turn by turn.",
    )
    demo_parser.add_argument(
        "--synthetic",
        action="store_true",
        help="Generate a synthetic five-turn session and two transcripts "
        "(default when --transcript is not given; contains no real prompt "
        "text, safe to publish).",
    )
    demo_parser.add_argument(
        "--transcript",
        default=None,
        help="Render a REAL transcript JSON file instead of synthetic data "
        "(requires --ledger). The output file is written at mode 0600 and a "
        "warning is printed -- never publish it.",
    )
    demo_parser.add_argument(
        "--ledger",
        default=None,
        help="Ledger directory to verify --transcript against (ignored in "
        "synthetic mode).",
    )
    demo_parser.add_argument(
        "--out",
        default="demo.html",
        help="Output HTML file path (default: demo.html).",
    )
    demo_parser.set_defaults(func=_cmd_demo)

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


def _find_latest_attestation_by_source(
    ledger_dir: Any, subject: str, source_matches: Callable[[str], bool]
) -> anchor_mod.Attestation | None:
    """The most recent :class:`~promptwitness.anchor.Attestation` recorded
    for *subject* whose ``source`` satisfies *source_matches*, reconstructed
    from the ledger's own ``anchor`` lines — the ledger is the persisted
    attestation history, so no separate side-state file is needed.

    Shared by :func:`_find_latest_ots_attestation` (exact-match source) and
    :func:`_find_latest_rfc3161_attestation` (prefix-match source) so
    neither copies the scan loop.
    """
    latest: anchor_mod.Attestation | None = None
    for line in ledger.iter_lines(ledger_dir):
        if line.kind != "anchor" or line.extra.get("subject") != subject:
            continue
        raw_att = line.extra.get("attestation")
        if not isinstance(raw_att, dict) or not source_matches(str(raw_att.get("source", ""))):
            continue
        latest = anchor_mod.Attestation.from_dict(raw_att)
    return latest


def _find_latest_ots_attestation(ledger_dir: Any, subject: str) -> anchor_mod.Attestation | None:
    """The most recent OpenTimestamps :class:`~promptwitness.anchor.Attestation`
    previously recorded for *subject*. The OpenTimestamps source
    (``"opentimestamps"``) never varies, so this matches it by equality.
    """
    ots_source = anchor_mod.OpenTimestampsAnchorer.source
    return _find_latest_attestation_by_source(ledger_dir, subject, lambda s: s == ots_source)


def _find_latest_rfc3161_attestation(
    ledger_dir: Any, subject: str
) -> anchor_mod.Attestation | None:
    """The most recent RFC 3161 :class:`~promptwitness.anchor.Attestation`
    previously recorded for *subject*. Unlike the OpenTimestamps source, the
    RFC 3161 source is per-TSA-host (``f"rfc3161:{host}"`` —
    :class:`~promptwitness.anchor_rfc3161.Rfc3161Anchorer`), so an operator
    who changed ``--rfc3161-url`` between runs still has their prior
    attestation located here. Matches by the ``"rfc3161:"`` source PREFIX,
    never by equality against the *current* adapter's ``source``
    (athenaeum#2174 review finding #2: ``anchor --upgrade`` must find the
    proof it should preserve, not just the one pointing at today's TSA).
    """
    return _find_latest_attestation_by_source(
        ledger_dir, subject, lambda s: s.startswith("rfc3161:")
    )


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

    A third, OPTIONAL adapter — :class:`~promptwitness.anchor_rfc3161.Rfc3161Anchorer`
    (athenaeum#2157) — runs only when a TSA URL resolves
    (:func:`~promptwitness.anchor_rfc3161.resolve_rfc3161_url`: ``--rfc3161-url``
    or ``PROMPTWITNESS_RFC3161_URL``). An unconfigured URL means the
    adapter is never constructed and appends no ``anchor`` line and no
    ``results`` entry at all — "not offered", not a ``failed`` attestation.

    ``--upgrade`` for the RFC 3161 adapter mirrors the OpenTimestamps
    semantics above, but locates the prior attestation by source PREFIX
    (:func:`_find_latest_rfc3161_attestation`) rather than equality, since
    the RFC 3161 source is per-TSA-host: when a prior RFC 3161 attestation
    for this subject exists, ``--upgrade`` calls
    :meth:`~promptwitness.anchor_rfc3161.Rfc3161Anchorer.upgrade` (a
    documented no-op — a TSA token is immediate) and records it with
    ``op="upgrade"`` instead of submitting a fresh TSA request, which would
    overwrite the ``.tsr`` file a prior ledger line's ``proof_path`` still
    names (review finding #2 on PR athenaeum#2174). With no prior RFC 3161
    attestation for this subject, ``--upgrade`` anchors as it does today —
    there is nothing to preserve.
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

    rfc3161_url = anchor_rfc3161.resolve_rfc3161_url(args.rfc3161_url)
    rfc3161: anchor_rfc3161.Rfc3161Anchorer | None = None
    rfc3161_att: anchor_mod.Attestation | None = None
    if rfc3161_url:
        rfc3161 = anchor_rfc3161.Rfc3161Anchorer(
            ledger_dir,
            tsa_url=rfc3161_url,
            ca_bundle=anchor_rfc3161.resolve_rfc3161_ca_bundle(args.rfc3161_ca_bundle),
        )
        existing_rfc3161 = (
            _find_latest_rfc3161_attestation(ledger_dir, subject) if args.upgrade else None
        )
        if existing_rfc3161 is not None:
            # A TSA token is immediate -- there is nothing pending to
            # re-submit, and re-anchoring would overwrite the .tsr file a
            # prior ledger line's proof_path still names (review finding
            # #2 on PR athenaeum#2174). upgrade() is a documented no-op.
            rfc3161_att = rfc3161.upgrade(existing_rfc3161)
            rfc3161_op = "upgrade"
        else:
            rfc3161_att = rfc3161.anchor(digest_bytes, subject, timeout=args.timeout)
            rfc3161_op = "anchor"
        _append_anchor_line(
            ledger_dir,
            harness=args.harness,
            subject=subject,
            digest=digest,
            attestation=rfc3161_att,
            op=rfc3161_op,
        )
        results.append(
            {"subject": subject, "op": rfc3161_op, "attestation": rfc3161_att.to_dict()}
        )

    if args.verify:
        verify_result = ots.verify(ots_att, digest_bytes)
        verify_payload = {
            "subject": subject,
            "op": "verify",
            "verify_result": verify_result.to_dict(),
        }
        results.append(verify_payload)
        if rfc3161 is not None and rfc3161_att is not None:
            rfc3161_verify_result = rfc3161.verify(rfc3161_att, digest_bytes)
            results.append(
                {
                    "subject": subject,
                    "op": "verify",
                    "verify_result": rfc3161_verify_result.to_dict(),
                }
            )

    print(json.dumps({"results": results}, sort_keys=True))
    return 0


def _last_turn_key_strength(ledger_dir: Any) -> str:
    """The ``key.strength`` of the most recent ``kind="turn"`` line, or
    ``"none"`` for an empty ledger -- the same value :func:`_cmd_status`
    reports, reused here for the demo footer."""
    strength = "none"
    for line in ledger.iter_lines(ledger_dir):
        if line.kind == "turn":
            strength = line.key.strength
    return strength


def _collect_anchor_rows(ledger_dir: Any) -> list[tuple[str, str, str]]:
    """``(source, status, claimed_time)`` for the most recent attestation
    recorded per anchor ``source`` in *ledger_dir* -- the footer's "Anchors"
    table. Last-write-wins per source, scanning every ``kind="anchor"``
    line in the ledger (there is no separate anchor-status index)."""
    rows: dict[str, tuple[str, str, str]] = {}
    for line in ledger.iter_lines(ledger_dir):
        if line.kind != "anchor":
            continue
        raw_att = line.extra.get("attestation")
        if not isinstance(raw_att, dict):
            continue
        att = anchor_mod.Attestation.from_dict(raw_att)
        rows[att.source] = (att.source, att.status, att.claimed_time)
    return list(rows.values())


def _cmd_verify(args: argparse.Namespace) -> int:
    """``promptwitness verify --transcript ... --ledger ...``: verify a
    transcript against a witness ledger and exit 0 iff every human turn is
    witnessed (athenaeum#2155's own acceptance criterion), else 1."""
    records = verify_mod.load_transcript_records(args.transcript)
    ledger_index = ledger.LedgerIndex.build(args.ledger)
    verdicts = verify_mod.verify_transcript(records, ledger_index)
    ok = verify_mod.all_human_turns_witnessed(verdicts)
    if args.json:
        payload = {"ok": ok, "verdicts": [v.to_dict() for v in verdicts]}
        print(json.dumps(payload, sort_keys=True))
    else:
        for turn_verdict in verdicts:
            print(f"{turn_verdict.index}: {verify_mod.verdict_label(turn_verdict.verdict)}")
        print("ok" if ok else "FAILED: not every human turn was witnessed")
    return 0 if ok else 1


def _cmd_demo(args: argparse.Namespace) -> int:
    """``promptwitness demo``: render the static side-by-side HTML demo.

    Synthetic mode (default, no ``--transcript``) builds a throwaway ledger
    under a fresh temp directory and writes a shareable page with no real
    prompt text. Real-transcript mode (``--transcript`` + ``--ledger``)
    renders the operator's own data, writes the output at mode 0600, and
    prints the required stderr warning -- never publish that file.
    """
    if args.transcript and args.synthetic:
        print(
            json.dumps({"error": "--synthetic and --transcript are mutually exclusive"}),
            file=sys.stderr,
        )
        return 2

    out_path = Path(args.out)

    if args.transcript:
        if not args.ledger:
            print(
                json.dumps({"error": "--ledger is required together with --transcript"}),
                file=sys.stderr,
            )
            return 2
        records = verify_mod.load_transcript_records(args.transcript)
        ledger_index = ledger.LedgerIndex.build(args.ledger)
        verdicts = verify_mod.verify_transcript(records, ledger_index)
        section = demo_mod.DemoSection(
            title="Real transcript", records=records, verdicts=verdicts
        )
        html_text = demo_mod.render_html(
            [section],
            mode="real",
            key_strength=_last_turn_key_strength(args.ledger),
            anchors=_collect_anchor_rows(args.ledger),
        )
        demo_mod.write_demo_html(out_path, html_text, restrict_mode=True)
        print(
            f"warning: {out_path} was rendered from a REAL transcript and ledger "
            "and contains real prompt text. Do not publish or share this file.",
            file=sys.stderr,
        )
        return 0

    with tempfile.TemporaryDirectory(prefix="promptwitness-demo-") as tmp_dir:
        synthetic = demo_mod.generate_synthetic(tmp_dir)
        verdicts_a = verify_mod.verify_transcript(
            synthetic.transcript_a, synthetic.ledger_index
        )
        verdicts_b = verify_mod.verify_transcript(
            synthetic.transcript_b, synthetic.ledger_index
        )
        sections = [
            demo_mod.DemoSection(
                title="Verified transcript",
                records=synthetic.transcript_a,
                verdicts=verdicts_a,
            ),
            demo_mod.DemoSection(
                title="Tampered transcript",
                records=synthetic.transcript_b,
                verdicts=verdicts_b,
            ),
        ]
        att = synthetic.anchor_attestation
        html_text = demo_mod.render_html(
            sections,
            mode="synthetic",
            key_strength=synthetic.key_provider.strength(),
            anchors=[(att.source, att.status, att.claimed_time)],
        )
        demo_mod.write_demo_html(out_path, html_text, restrict_mode=False)
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
