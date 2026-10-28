# SPDX-License-Identifier: Apache-2.0
"""SessionEnd witness-anchor step (issue athenaeum#2156).

Layering: L4 (domain/pipeline). Imports :mod:`athenaeum.receipt_sealers`
(L2) for read-only receipt-partition digesting, and ``promptwitness.*``
directly (anchoring + ledger primitives). Deliberately kept OUT of
:mod:`athenaeum.claude_code_adapter` (L5), which calls into this module
with a single delegation: that module's own AC2 guard
(``tests/test_claude_code_adapter.py::TestAC2NoRetrievalLogic``) forbids
any arithmetic expression (``ast.BinOp``) anywhere in its source, and a
wall-clock deadline computation — the whole point of this module — is
unavoidably arithmetic.

**What this answers.** Both ``session-end`` modes in ``_cmd_index.py`` run
an anchor step, before either branch, that anchors two subjects as
``anchor`` lines in the local witness ledger (see
``docs/design/witness-ledger.md`` "Anchoring" and the epic's own
Subject/Digest/When table):

- ``witness:<ledger_id>:<seq>`` — the chain head, every SessionEnd.
- ``receipts:<YYYY-MM>:<line_count>`` — each receipt partition's
  ``receipt_sealers.lines_digest`` over ALL complete lines, read-only,
  only when it changed since its own last anchor.

No run lock is taken (the design's lock rule: the hook path and
SessionEnd both record anchors lock-free; only the nightly path, under
its lock, mirrors them into ``wiki/``). Nothing under ``wiki/`` is ever
written by this module — every receipt-partition read is a plain
``Path.read_text`` and the digest is computed purely in memory.

**The 10 second budget.** :func:`run_session_end_anchor` takes a
:func:`time.monotonic`-based deadline and never lets network-bound work
start once it has passed: if there is no budget at all
(``budget_seconds <= 0``), the whole job is handed off to a detached
``promptwitness anchor`` process instead (safe for the head subject only —
see :func:`_spawn_detached_head_anchor`'s docstring for why receipts
anchoring is simply skipped, never detached, in the same situation). A
hung calendar cannot push SessionEnd past the budget either way: the
remaining budget is re-read from the deadline before EACH network adapter
call (not once per subject), a network adapter whose worst case no longer
fits in what remains is skipped rather than started, and the loop re-checks
the deadline before starting each subsequent subject. OpenTimestamps tries
its calendars one after another with the same per-call *timeout*, so it is
handed the remaining budget divided by its calendar count — its worst case
(every calendar hanging) then still ends at the deadline.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

#: SessionEnd anchor step's total wall-clock budget, in seconds (issue
#: athenaeum#2156). The step takes no run lock, so this bound is the ONLY
#: thing standing between a hung calendar and SessionEnd itself hanging.
SESSION_END_ANCHOR_BUDGET_SECONDS = 10.0

#: Floor on the per-adapter timeout handed to a network call that starts
#: very close to the deadline — never 0 or negative, which some HTTP
#: clients treat as "no timeout at all" rather than "fail immediately".
_MIN_ADAPTER_TIMEOUT_SECONDS = 0.5

__all__ = ["SESSION_END_ANCHOR_BUDGET_SECONDS", "run_session_end_anchor"]


def _spawn_detached_head_anchor(ledger_dir: Path) -> None:
    """Spawn a fully detached ``promptwitness anchor`` process anchoring
    the current chain head (issue athenaeum#2156). Mirrors
    ``athenaeum.claude_code_adapter._spawn_detached_anchor`` exactly —
    duplicated rather than imported, since that module must not be
    imported FROM here (it already imports this module the other way;
    see this module's own docstring). Safe to delegate for the head
    subject specifically: ``promptwitness anchor`` with no ``--subject``
    recomputes the SAME digest (the head line's own hash) this module
    would have computed itself. It is NOT safe to delegate a receipts
    subject this way — see :func:`run_session_end_anchor`'s docstring.
    """
    subprocess.Popen(
        [sys.executable, "-m", "promptwitness.cli", "anchor", "--dir", str(ledger_dir)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _head_subject_and_digest(ledger_dir: Path) -> tuple[str, str] | tuple[None, None]:
    """``(subject, digest)`` for the current witness chain head, or
    ``(None, None)`` for an empty ledger.

    Mirrors ``promptwitness.cli._resolve_subject_and_digest``'s default
    (no explicit ``--subject``) branch exactly: the subject is
    ``witness:<ledger_id>:<seq>`` and the digest is that line's own
    ``line_hash()``. Duplicated rather than imported from ``cli.py`` — a
    presentation-layer promptwitness module this one, a domain module,
    should not reach into.
    """
    from promptwitness import ledger as pw_ledger

    head: pw_ledger.WitnessLine | None = None
    for line in pw_ledger.iter_lines(ledger_dir):
        head = line
    if head is None:
        return None, None
    return "witness:{}:{}".format(head.ledger_id, head.seq), head.line_hash()


def _append_anchor_line(
    ledger_dir: Path, *, subject: str, digest: str, attestation: Any, op: str
) -> None:
    """Append one ``kind="anchor"`` witness-ledger line — the exact shape
    ``promptwitness.cli._append_anchor_line`` writes for the
    ``promptwitness anchor`` CLI command, duplicated here for the same
    layering reason as :func:`_head_subject_and_digest`.
    """
    from promptwitness import __version__ as pw_version
    from promptwitness import ledger as pw_ledger

    pw_ledger.append_line(
        ledger_dir,
        kind="anchor",
        harness="claude-code",
        harness_version="",
        hook_version="promptwitness {}".format(pw_version),
        extra={
            "subject": subject,
            "digest": digest,
            "op": op,
            "attestation": attestation.to_dict(),
        },
    )


def _anchor_subject_in_process(
    ledger_dir: Path,
    *,
    subject: str,
    digest: str,
    deadline: float,
    monotonic: Callable[[], float],
) -> None:
    """Anchor *subject* (a ``"sha256:<hex>"`` *digest* it commits to) with
    every adapter promptwitness itself knows how to offer: the mandatory
    ``LocalClockAnchorer`` (always, never fails), ``OpenTimestampsAnchorer``,
    and ``Rfc3161Anchorer`` when a TSA URL resolves.

    Deadline-based, not timeout-based: each network adapter's *timeout* is
    computed from ``deadline - monotonic()`` immediately before that
    adapter starts (:func:`_adapter_timeout`), so time a slow OpenTimestamps
    pass spent is no longer available to RFC 3161 after it. An adapter that
    no longer fits is skipped and writes no line. The local-clock anchorer
    does no I/O and always runs.

    Adapter selection and config come from promptwitness's OWN env-based
    resolvers (``anchor.resolve_block_source``,
    ``anchor_rfc3161.resolve_rfc3161_url``) — the same ones the
    ``promptwitness anchor`` CLI command uses — never from
    ``athenaeum.config``'s ``librarian.receipt_anchor.*`` yaml resolvers,
    which are the NIGHTLY anchoring path's config surface (athenaeum#2040),
    a separate, out-of-scope caller.
    """
    from promptwitness import anchor as anchor_mod
    from promptwitness import anchor_rfc3161

    digest_bytes = anchor_mod.digest_bytes(digest)

    local = anchor_mod.LocalClockAnchorer()
    local_att = local.anchor(digest_bytes, subject, timeout=_MIN_ADAPTER_TIMEOUT_SECONDS)
    _append_anchor_line(
        ledger_dir, subject=subject, digest=digest, attestation=local_att, op="anchor"
    )

    # OpenTimestampsAnchorer.anchor() tries each calendar in turn, each with
    # the full per-call timeout, so its worst case is timeout x calendars.
    ots_timeout = _adapter_timeout(
        deadline, monotonic, sequential_calls=len(anchor_mod.DEFAULT_CALENDARS)
    )
    if ots_timeout is not None:
        ots = anchor_mod.OpenTimestampsAnchorer(
            ledger_dir, block_source=anchor_mod.resolve_block_source()
        )
        ots_att = ots.anchor(digest_bytes, subject, timeout=ots_timeout)
        _append_anchor_line(
            ledger_dir, subject=subject, digest=digest, attestation=ots_att, op="anchor"
        )

    rfc3161_url = anchor_rfc3161.resolve_rfc3161_url(None)
    if rfc3161_url:
        rfc_timeout = _adapter_timeout(deadline, monotonic, sequential_calls=1)
        if rfc_timeout is None:
            return
        rfc = anchor_rfc3161.Rfc3161Anchorer(
            ledger_dir,
            tsa_url=rfc3161_url,
            ca_bundle=anchor_rfc3161.resolve_rfc3161_ca_bundle(None),
        )
        rfc_att = rfc.anchor(digest_bytes, subject, timeout=rfc_timeout)
        _append_anchor_line(
            ledger_dir, subject=subject, digest=digest, attestation=rfc_att, op="anchor"
        )


def _receipts_partition_unchanged(ledger_dir: Path, month: str, digest: str) -> bool:
    """``True`` when *digest* (this pass's freshly-computed, read-only
    ``receipt_sealers.lines_digest`` over partition *month*) equals the
    digest recorded by that partition's own most recent ``anchor`` line
    (issue athenaeum#2156's "an unchanged receipt partition is not
    re-anchored" AC). No prior anchor for this month at all counts as
    "changed" (``False``) — there is nothing to compare against yet.
    """
    from promptwitness import ledger as pw_ledger

    prefix = "receipts:{}:".format(month)
    last_digest: str | None = None
    for line in pw_ledger.iter_lines(ledger_dir):
        if line.kind != "anchor":
            continue
        subject = line.extra.get("subject")
        if isinstance(subject, str) and subject.startswith(prefix):
            last_digest = line.extra.get("digest")
    return last_digest == digest


def _adapter_timeout(
    deadline: float, monotonic: Callable[[], float], *, sequential_calls: int
) -> float | None:
    """Per-call *timeout* for a network adapter about to start, read from
    the clock NOW, or ``None`` when the adapter should not start at all.

    *sequential_calls* is how many network calls the adapter may make one
    after another with that same timeout; the remaining budget is split
    across them so their sum still ends at *deadline*. ``None`` when the
    split would fall below :data:`_MIN_ADAPTER_TIMEOUT_SECONDS` — starting
    a call with a floored timeout is exactly how the budget gets overrun.
    """
    per_call = (deadline - monotonic()) / max(sequential_calls, 1)
    if per_call < _MIN_ADAPTER_TIMEOUT_SECONDS:
        return None
    return per_call


def run_session_end_anchor(
    ledger_dir: Path,
    wiki_root: Path,
    *,
    budget_seconds: float = SESSION_END_ANCHOR_BUDGET_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Run the SessionEnd anchor step (issue athenaeum#2156): anchor the
    chain head, then every receipt partition under *wiki_root* whose
    digest changed since its own last anchor — read-only; nothing under
    ``wiki/`` is ever written.

    Computes its own deadline, exactly *budget_seconds* (default
    :data:`SESSION_END_ANCHOR_BUDGET_SECONDS`) from the moment this
    function starts, measured through *monotonic* (default
    :func:`time.monotonic`; injectable so tests can drive a fake clock
    deterministically — the acceptance criterion this exists for is "a
    hung calendar does not push session-end past its budget", which needs
    to be provable without an actual multi-second sleep in the test
    suite). If there is no budget at all (``budget_seconds <= 0``), the
    whole job is handed off to a detached ``promptwitness
    anchor`` process instead (:func:`_spawn_detached_head_anchor`) — safe
    here because the chain-head subject's digest is a pure function of
    the subject string, recomputed identically by that CLI command.
    Receipt-partition work past the deadline is simply SKIPPED for this
    pass, never detached: the correct digest for a receipts subject can
    only be computed here, where *wiki_root* is known, and a skip (unlike
    an anchor) leaves nothing recorded to compare against, so it is
    retried cleanly at the next SessionEnd.
    """
    if budget_seconds <= 0:
        _spawn_detached_head_anchor(ledger_dir)
        return
    deadline = monotonic() + budget_seconds

    head_subject, head_digest = _head_subject_and_digest(ledger_dir)
    if head_subject is not None and head_digest is not None:
        _anchor_subject_in_process(
            ledger_dir,
            subject=head_subject,
            digest=head_digest,
            deadline=deadline,
            monotonic=monotonic,
        )

    from athenaeum import receipt_sealers
    from promptwitness import anchor as anchor_mod

    n_calendars = len(anchor_mod.DEFAULT_CALENDARS)
    for month in receipt_sealers.iter_months(wiki_root):
        # Stop once OpenTimestamps would no longer fit, not merely once the
        # deadline has passed: a receipts subject that got only its
        # local-clock line would read as "unchanged" next SessionEnd and
        # never be offered to OpenTimestamps at all.
        if _adapter_timeout(deadline, monotonic, sequential_calls=n_calendars) is None:
            break
        partition = receipt_sealers.partition_path(wiki_root, month)
        try:
            lines = partition.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        if not lines:
            continue
        digest = "sha256:{}".format(receipt_sealers.lines_digest(lines))
        if _receipts_partition_unchanged(ledger_dir, month, digest):
            continue
        subject = "receipts:{}:{}".format(month, len(lines))
        _anchor_subject_in_process(
            ledger_dir,
            subject=subject,
            digest=digest,
            deadline=deadline,
            monotonic=monotonic,
        )
