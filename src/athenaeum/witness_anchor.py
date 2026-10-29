# SPDX-License-Identifier: Apache-2.0
"""The two witness-anchor steps: SessionEnd (issue athenaeum#2156) and
nightly (issue athenaeum#2040).

Layering: L4 (domain/pipeline). Imports :mod:`athenaeum.receipt_sealers`
(L2) for receipt-partition digesting, the anchorer selection seam and the
anchor-record store, and ``promptwitness.*`` directly (anchoring + ledger
primitives). Deliberately kept OUT of
:mod:`athenaeum.claude_code_adapter` (L5), which calls into this module
with a single delegation: that module's own AC2 guard
(``tests/test_claude_code_adapter.py::TestAC2NoRetrievalLogic``) forbids
any arithmetic expression (``ast.BinOp``) anywhere in its source, and a
wall-clock deadline computation — the whole point of
:func:`run_session_end_anchor` — is unavoidably arithmetic.

**Two callers, two lock postures, one module.** The design's lock rule
(``docs/design/witness-ledger.md``, "Anchoring") splits the anchor paths
by what they are allowed to write, not by what they anchor:

- :func:`run_session_end_anchor` (and the hook's detached spawn) hold NO
  run lock, so they record anchors ONLY as ``anchor`` lines in the local
  witness ledger, under a hard 10 second wall-clock budget.
- :func:`run_nightly_anchor` holds the run lock, so it is the one path
  that may write into ``wiki/``: the anchor-record store
  (``_transcript_receipts/_seals/anchors/``) and the off-machine mirror
  (``_witness_anchors/``, :func:`mirror_witness_anchors`) — the only
  defence against deleting the whole ledger, because those records leave
  the machine with the pushed knowledge repo. It also imports, rather
  than re-stamps, an attestation the lock-free paths already obtained for
  the same digest, and upgrades pending OpenTimestamps proofs.

**What this answers.** Both ``session-end`` modes in ``_cmd_index.py`` run
an anchor step, before either branch, that anchors two subjects as
``anchor`` lines in the local witness ledger (see
``docs/design/witness-ledger.md`` "Anchoring" and the epic's own
Subject/Digest/When table):

- ``witness:<ledger_id>:<seq>`` — the chain head, every SessionEnd.
- ``receipts:<YYYY-MM>:<line_count>`` — each receipt partition's
  ``receipt_sealers.lines_digest`` over ALL complete lines, read-only,
  only when it changed since its own last anchor.

No run lock is taken by THAT step (the design's lock rule: the hook path
and SessionEnd both record anchors lock-free). Nothing under ``wiki/`` is
written by it — every receipt-partition read is a plain
``Path.read_text`` and the digest is computed purely in memory. The
nightly step below is the one that writes, and it requires the lock.

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

import json
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
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

__all__ = [
    "SESSION_END_ANCHOR_BUDGET_SECONDS",
    "WITNESS_ANCHORS_DIRNAME",
    "WITNESS_ANCHOR_PROOFS_DIRNAME",
    "MIRROR_RECORD_KEYS",
    "NightlyAnchorReport",
    "mirror_witness_anchors",
    "run_nightly_anchor",
    "run_session_end_anchor",
    "witness_anchor_proofs_dir",
    "witness_anchors_dir",
    "witness_anchors_partition_path",
]


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


# ---------------------------------------------------------------------------
# Nightly anchoring + off-machine mirror (issue athenaeum#2040)
# ---------------------------------------------------------------------------

#: The off-machine mirror's directory, directly under ``wiki_root`` (NOT
#: under the receipt ledger): these records exist precisely so that
#: deleting the whole receipt ledger -- or the whole witness ledger --
#: stays detectable, so they must not live inside either of them.
WITNESS_ANCHORS_DIRNAME = "_witness_anchors"

#: ``witness:<ledger_id>:<seq>`` -- the chain-head anchor subject format.
#: Duplicated rather than imported for the same reason
#: ``athenaeum.witness_attestation`` duplicates it: the producer
#: (``promptwitness.cli._WITNESS_SUBJECT_RE``) is private to a
#: presentation-layer module this one must not reach into.
_WITNESS_SUBJECT_RE = re.compile(r"^witness:(?P<ledger_id>[^:]+):(?P<seq>\d+)$")

#: Proof files (``.ots`` / ``.tsr``) copied alongside the mirror, by their
#: own basename, so an attestation's ``proof_path`` resolves here too.
WITNESS_ANCHOR_PROOFS_DIRNAME = "proofs"

#: The ONLY keys a mirror record carries (issue athenaeum#2040):
#: ``ledger_id``, ``seq``, ``line_hash`` and the attestations. Nothing
#: derived from a prompt -- no ``prompt_digest``, no ``raw_digest``, no
#: ``session``, no ``prompt_chars`` -- ever crosses into the pushed
#: knowledge repo. A short human utterance's digest is
#: dictionary-recoverable, which is the whole reason the witness ledger
#: lives outside the repo in the first place
#: (``docs/design/witness-ledger.md``, "Ledger").
MIRROR_RECORD_KEYS = ("ledger_id", "seq", "line_hash", "attestations")


def witness_anchors_dir(wiki_root: Path) -> Path:
    return Path(wiki_root) / WITNESS_ANCHORS_DIRNAME


def witness_anchors_partition_path(wiki_root: Path, month: str) -> Path:
    return witness_anchors_dir(wiki_root) / "{}.jsonl".format(month)


def witness_anchor_proofs_dir(wiki_root: Path) -> Path:
    return witness_anchors_dir(wiki_root) / WITNESS_ANCHOR_PROOFS_DIRNAME


@dataclass
class NightlyAnchorReport:
    """What one :func:`run_nightly_anchor` pass did. Partitions and
    subjects only -- never receipt or prompt content, the same
    no-plaintext rule :mod:`athenaeum.receipt_sealers`'s own reports
    follow.
    """

    anchored: list[str] = field(default_factory=list)
    imported: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    head_subject: str | None = None
    upgraded: list[str] = field(default_factory=list)
    mirrored: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "anchored": list(self.anchored),
            "imported": list(self.imported),
            "unchanged": list(self.unchanged),
            "head_subject": self.head_subject,
            "upgraded": list(self.upgraded),
            "mirrored": self.mirrored,
        }


def _ledger_attestations_by_digest(ledger_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """Every NON-``failed`` attestation the LOCAL witness ledger holds,
    grouped by the digest it commits to, newest-per-source (issue
    athenaeum#2040's import path).

    The in-session and SessionEnd paths (issue athenaeum#2156) anchor a
    receipt partition's digest without any lock and record the result ONLY
    as ``anchor`` lines in the witness ledger. When the nightly then finds
    an attestation for the SAME digest, re-stamping it would spend a
    calendar round trip to learn something already known AND record a
    later ``claimed_time`` for content demonstrably older than that --
    strictly worse evidence. So the nightly imports instead.

    Keyed on the DIGEST, not the subject: that is what the attestation
    actually commits to, and it is identical across the two paths because
    both derive it through
    :func:`athenaeum.receipt_sealers.partition_digest`. Built in ONE ledger
    pass for the whole run rather than one pass per partition -- the
    ledger is append-only and grows for the life of the machine, so a
    per-partition scan would make the nightly's cost quadratic in a file
    nobody ever truncates.
    """
    from promptwitness import ledger as pw_ledger

    newest: dict[str, dict[str, dict[str, Any]]] = {}
    for line in pw_ledger.iter_lines(ledger_dir):
        if line.kind != "anchor":
            continue
        digest = line.extra.get("digest")
        raw_att = line.extra.get("attestation")
        if not isinstance(digest, str) or not isinstance(raw_att, dict):
            continue
        source = str(raw_att.get("source", ""))
        if not source or str(raw_att.get("status", "")) == "failed":
            # A FAILED attempt is not evidence of anything, so importing it
            # would be importing nothing -- and would mark the digest as
            # handled, which is how an offline night's failure becomes
            # permanent (Seer review on PR athenaeum#2185, finding 2, one
            # layer over from :func:`_anchored_sources`).
            continue
        newest.setdefault(digest, {})[source] = dict(raw_att)
    return {digest: list(by_source.values()) for digest, by_source in newest.items()}


def _anchor_subject_with(
    ledger_dir: Path,
    anchorers: list[Any],
    *,
    subject: str,
    digest: str,
    timeout: float,
) -> list[dict[str, Any]]:
    """Anchor *subject* with each of *anchorers* and append one witness
    ``anchor`` line per attestation. Returns the attestations.

    The nightly's counterpart to :func:`_anchor_subject_in_process`, which
    the lock-free SessionEnd path uses: that one selects adapters from
    promptwitness's own env resolvers under a wall-clock deadline, this
    one takes the adapter list ``librarian.receipt_anchor.adapters``
    configured for the nightly
    (:func:`athenaeum.receipt_sealers.select_anchorers`) and bounds each
    call with a plain per-adapter *timeout* -- the nightly holds the run
    lock, so a slow calendar costs the run time it is budgeted for, not
    SessionEnd's 10 seconds.
    """
    from promptwitness import anchor as anchor_mod

    digest_raw = anchor_mod.digest_bytes(digest)
    out: list[dict[str, Any]] = []
    for anchorer in anchorers:
        att = anchorer.anchor(digest_raw, subject, timeout=timeout)
        _append_anchor_line(
            ledger_dir, subject=subject, digest=digest, attestation=att, op="anchor"
        )
        out.append(att.to_dict())
    return out


def _upgrade_pending(
    ledger_dir: Path, anchorers: list[Any]
) -> list[str]:
    """Upgrade every still-``pending`` attestation in the witness ledger
    whose source one of *anchorers* can speak for, and return the subjects
    upgraded.

    OpenTimestamps is ``pending`` from submission until its calendar's
    Bitcoin transaction confirms -- hours, routinely. Upgrading is
    therefore a nightly job rather than part of the anchoring pass that
    created the pending stamp: this is the only place in athenaeum that
    turns a submitted stamp into a complete Bitcoin attestation. Status is
    LAST-WRITE-WINS per ``(subject, source)``, so a subject already
    upgraded on an earlier run is not re-offered.
    """
    from promptwitness import anchor as anchor_mod
    from promptwitness import ledger as pw_ledger

    by_source = {getattr(a, "source", ""): a for a in anchorers}
    latest: dict[tuple[str, str], tuple[str, anchor_mod.Attestation]] = {}
    for line in pw_ledger.iter_lines(ledger_dir):
        if line.kind != "anchor":
            continue
        subject = line.extra.get("subject")
        digest = line.extra.get("digest")
        raw_att = line.extra.get("attestation")
        if not isinstance(subject, str) or not isinstance(digest, str):
            continue
        if not isinstance(raw_att, dict):
            continue
        source = str(raw_att.get("source", ""))
        latest[(subject, source)] = (digest, anchor_mod.Attestation.from_dict(raw_att))

    upgraded: list[str] = []
    for (subject, source), (digest, att) in sorted(latest.items()):
        if att.status != "pending":
            continue
        anchorer = by_source.get(source)
        if anchorer is None:
            continue
        new_att = anchorer.upgrade(att)
        if new_att.status == att.status:
            # Still pending. Append NOTHING: a line per nightly per
            # not-yet-confirmed stamp would grow the ledger (and the
            # off-machine mirror) without recording anything new, and a
            # calendar can legitimately stay pending for hours.
            continue
        _append_anchor_line(
            ledger_dir, subject=subject, digest=digest, attestation=new_att, op="upgrade"
        )
        upgraded.append(subject)
    return upgraded


def _mirror_record(line: Any, attestations: list[dict[str, Any]]) -> dict[str, Any]:
    """One mirror record for witness ``anchor`` *line*, carrying exactly
    :data:`MIRROR_RECORD_KEYS` and nothing else.
    """
    return {
        "ledger_id": line.ledger_id,
        "seq": line.seq,
        "line_hash": line.line_hash(),
        "attestations": attestations,
    }


def mirror_witness_anchors(ledger_dir: Path, wiki_root: Path, *, lock: Any) -> int:
    """Mirror every NEW witness ``anchor`` line into
    ``wiki/_witness_anchors/<YYYY-MM>.jsonl``, with its proof file copied
    under ``proofs/``. Returns how many records were written.

    **This is the only defence against deleting the whole ledger.**
    Everything else the witness design builds -- the hash chain,
    signatures, in-session anchors -- lives in files the same OS user can
    remove (``docs/design/witness-ledger.md``, "Security boundary",
    attack window (b)). These records leave the machine with the pushed
    knowledge repo, so a ledger that is gone can still be shown to have
    existed, up to the last mirrored ``seq``, with an attestation for
    when.

    **No plaintext and no prompt-derived field**, ever: a record carries
    exactly :data:`MIRROR_RECORD_KEYS`. Partition month comes from the
    anchor line's own ``ts``, so a record lands in the same month the
    anchor was made.

    Requires an ALREADY-ACQUIRED *lock*
    (:func:`athenaeum.receipt_sealers.require_lock`): this writes into the
    knowledge repo, which has exactly one appender at a time.
    Already-mirrored lines are identified by ``(ledger_id, seq)``, so a
    re-run mirrors nothing new.
    """
    from athenaeum.receipt_sealers import require_lock
    from athenaeum.store import append_line_durable
    from promptwitness import ledger as pw_ledger

    require_lock(lock)

    existing: dict[str, set[tuple[str, int]]] = {}
    mirror_dir = witness_anchors_dir(wiki_root)
    if mirror_dir.is_dir():
        for path in sorted(mirror_dir.glob("*.jsonl")):
            seen: set[tuple[str, int]] = set()
            try:
                raw_text = path.read_text(encoding="utf-8")
            except OSError:
                raw_text = ""
            for raw_line in raw_text.splitlines():
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    record = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    seen.add((str(record.get("ledger_id", "")), int(record.get("seq", -1))))
            existing[path.stem] = seen

    written = 0
    for line in pw_ledger.iter_lines(ledger_dir):
        if line.kind != "anchor":
            continue
        raw_att = line.extra.get("attestation")
        if not isinstance(raw_att, dict):
            continue
        month = str(line.ts)[:7]
        if (line.ledger_id, line.seq) in existing.get(month, set()):
            continue
        record = _mirror_record(line, [dict(raw_att)])
        path = witness_anchors_partition_path(wiki_root, month)
        append_line_durable(
            path, (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
        )
        existing.setdefault(month, set()).add((line.ledger_id, line.seq))
        _copy_proof(ledger_dir, wiki_root, raw_att.get("proof_path"))
        written += 1
    return written


def _copy_proof(ledger_dir: Path, wiki_root: Path, proof_path: Any) -> None:
    """Copy one attestation's proof file under ``_witness_anchors/proofs/``.

    *proof_path* is ledger-relative (``anchors/<subject-safe>.ots``) and is
    copied by BASENAME, leaving the attestation's own recorded
    ``proof_path`` untouched -- the attestation is evidence and is
    mirrored verbatim; rewriting a field inside it to suit the mirror's
    layout would make the mirrored copy differ from the one the local
    ledger recorded. Best-effort: a proof that is missing (the stamp
    failed, or it is a ``local-clock`` attestation, which has none) leaves
    the record itself perfectly valid.
    """
    if not isinstance(proof_path, str) or not proof_path:
        return
    src = Path(ledger_dir) / proof_path
    if not src.is_file():
        return
    dest_dir = witness_anchor_proofs_dir(wiki_root)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest_dir / Path(proof_path).name)
    except OSError:
        return


def _anchored_sources(wiki_root: Path, month: str, digest: str) -> set[str]:
    """Which anchor SOURCES already hold a non-``failed`` attestation for
    *digest*, unioned across every record stored for it.

    Digest equality alone is not "already anchored" (Seer review on PR
    athenaeum#2185, finding 2). An offline pass records the mandatory
    ``local-clock`` stamp AND a ``status: "failed"`` OpenTimestamps
    attempt; keying the skip on the digest alone would then make the
    failure permanent — the partition would read as anchored forever and
    would never be offered to a calendar again, which is the opposite of
    what this module's own "the next pass retries" contract says.

    Unioned across records because evidence ACCUMULATES: a first pass may
    hold the local-clock stamp and a failed calendar attempt, and a later
    retry the calendar success. Either record alone is incomplete; together
    they cover both sources.
    """
    from athenaeum import receipt_sealers

    covered: set[str] = set()
    for record in receipt_sealers.iter_stored_anchors(wiki_root, month):
        if record.digest != digest:
            continue
        for att in record.attestations:
            if str(att.get("status", "")) != "failed":
                covered.add(str(att.get("source", "")))
    return covered


def _head_and_new_content(ledger_dir: Path) -> tuple[str | None, str | None, bool]:
    """``(subject, digest, has_new_content)`` for the current chain head.

    *has_new_content* is ``True`` when a NON-``anchor`` line exists whose
    ``seq`` is past the highest head seq any ``anchor`` line has ever
    committed to — i.e. there is witness content (a turn, a session close,
    a key rotation, a gap) that no anchor yet bounds.

    **Measured against the anchored HEAD SEQ, not against "the newest
    anchor line".** Those differ, and the difference is the whole
    criterion: anchoring a ``witness:<ledger_id>:<seq>`` subject bounds
    every line up to that ``seq`` (that is what a hash chain buys), while
    a ``receipts:<YYYY-MM>:<n>`` anchor line bounds a receipt partition
    and says nothing about any witness line. Step 1 of the nightly writes
    exactly those receipt anchor lines, so a "newer than the last anchor
    line" test would read a genuine day of turns as already covered the
    moment step 1 ran. Same derivation as
    ``athenaeum.witness_attestation.AnchorIndex``'s ``anchored_through``.

    Why the nightly gates head anchoring on it (Seer review on PR
    athenaeum#2185, finding 1): anchoring is how a line is shown to have
    existed by some time, so with nothing appended since the last anchor
    there is nothing new to bound. Without the gate, a quiet day still
    produced an ``anchor`` line, a mirror record, a commit and a push
    carrying no information — and since anchoring itself appends lines, the
    head always *looks* different from the one anchored last night even when
    no session ever ran. Computed in the SAME pass as the head read rather
    than a second scan, for the reason
    :func:`_ledger_attestations_by_digest` gives.
    """
    from promptwitness import ledger as pw_ledger

    head: Any = None
    anchored_through: dict[str, int] = {}
    last_content: dict[str, int] = {}
    for line in pw_ledger.iter_lines(ledger_dir):
        head = line
        if line.kind == "anchor":
            match = _WITNESS_SUBJECT_RE.match(str(line.extra.get("subject", "")))
            if match is None:
                continue
            lid = match.group("ledger_id")
            seq = int(match.group("seq"))
            if seq > anchored_through.get(lid, -1):
                anchored_through[lid] = seq
        elif line.seq > last_content.get(line.ledger_id, -1):
            last_content[line.ledger_id] = line.seq
    if head is None:
        return None, None, False
    subject = "witness:{}:{}".format(head.ledger_id, head.seq)
    new_content = last_content.get(head.ledger_id, -1) > anchored_through.get(
        head.ledger_id, -1
    )
    return subject, head.line_hash(), new_content


def run_nightly_anchor(
    ledger_dir: Path,
    wiki_root: Path,
    *,
    config: dict[str, Any] | None,
    lock: Any,
) -> NightlyAnchorReport:
    """The nightly anchoring pass (issue athenaeum#2040), and the body of
    ``athenaeum receipts anchor``.

    Under an ALREADY-ACQUIRED *lock*, in this order:

    1. **Receipt partitions.** Every partition that does not already hold
       a non-``failed`` attestation from every configured source for its
       current :func:`~athenaeum.receipt_sealers.partition_digest` is
       anchored once, through
       :class:`~athenaeum.receipt_sealers.AnchoringSealer`, which records
       each attestation in BOTH stores (the anchor record under
       ``_seals/anchors/`` and an ``anchor`` line in the witness ledger).
       A partition that has not changed and whose stamps all landed is
       left alone, so a re-run makes no new stamp; one whose calendar
       attempt FAILED is retried, because digest equality alone would make
       an offline night's failure permanent (:func:`_anchored_sources`).
       When the local witness ledger already holds an attestation for that
       exact digest -- the in-session/SessionEnd path (issue
       athenaeum#2156) got there first -- it is IMPORTED rather than
       re-stamped (:func:`_ledger_attestations_by_digest`).
    2. **The witness chain head**, once per pass, with the configured
       adapters, and only when there is new witness content to bound
       (:func:`_head_and_new_content`). Read after step 1, so a turn the
       (lock-free) hook appended while step 1 ran is covered; read ONCE,
       because anchoring appends ``anchor`` lines and therefore moves the
       head, and a loop that re-read it would chase its own tail forever.
    3. **Pending OpenTimestamps proofs** are upgraded
       (:func:`_upgrade_pending`) -- the one place in athenaeum that turns
       a submitted stamp into a complete Bitcoin attestation.
    4. **The off-machine mirror** (:func:`mirror_witness_anchors`), LAST,
       so it carries this pass's own new anchor lines too.

    Never raises on a network failure: every promptwitness anchorer
    degrades to a ``failed`` attestation, and ``local-clock`` cannot fail.
    It DOES raise :class:`~athenaeum.transcript_receipts.LockNotHeld` when
    *lock* is not acquired -- that is a programming error, not a runtime
    condition, and silently skipping the writes would hide it.
    """
    from athenaeum import receipt_sealers
    from athenaeum.config import resolve_receipt_anchor_timeout_seconds

    receipt_sealers.require_lock(lock)

    timeout = resolve_receipt_anchor_timeout_seconds(config)
    anchorers = receipt_sealers.select_anchorers(config, ledger_dir)

    def _record(subject: str, digest: str, attestation: Any) -> None:
        """Every attestation the sealer obtains also becomes an ``anchor``
        line in the local witness ledger — the same shape the lock-free
        athenaeum#2156 paths write. Two consequences, both wanted: the
        off-machine mirror (step 4) carries receipt anchors as well as
        chain-head ones, and a later pass can FIND this attestation by its
        digest (:func:`_ledger_attestations_by_digest`) instead of
        re-stamping it.
        """
        _append_anchor_line(
            ledger_dir, subject=subject, digest=digest, attestation=attestation, op="anchor"
        )

    sealer = receipt_sealers.AnchoringSealer(
        anchorers=anchorers,
        lock=lock,
        wiki_root=wiki_root,
        timeout=timeout,
        record_attestation=_record,
    )

    report = NightlyAnchorReport()
    configured_sources = {getattr(a, "source", "") for a in anchorers}

    # 1. Receipt partitions. The attestation map is read BEFORE any
    #    anchoring appends to the ledger: this pass's own stamps are
    #    stamps, not imports.
    attestations_by_digest = _ledger_attestations_by_digest(ledger_dir)

    for month in receipt_sealers.iter_months(wiki_root):
        partition = receipt_sealers.partition_path(wiki_root, month)
        try:
            lines = partition.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        if not lines:
            continue
        digest = receipt_sealers.partition_digest(lines)
        covered = _anchored_sources(wiki_root, month, digest)
        if covered and configured_sources <= covered:
            report.unchanged.append(month)
            continue
        imported = attestations_by_digest.get(digest) or []
        if configured_sources <= {str(att.get("source", "")) for att in imported}:
            # Import only when the held attestations cover EVERY configured
            # source. A partial import (say local-clock only, because the
            # lock-free pass's calendar call failed) would record the
            # digest as handled and the missing source would never be
            # attempted again.
            sealer.seal_with_op(
                partition, 1, len(lines), lines, op="import", attestations=imported
            )
            report.imported.append(month)
        else:
            sealer.seal(partition, 1, len(lines), lines)
            report.anchored.append(month)

    # 2. The witness chain head, read HERE rather than at the top of the
    #    pass: the hook appends turns lock-free, so a turn that arrived
    #    while step 1 ran is covered by anchoring the head now and would
    #    not be by anchoring a head read earlier. Read ONCE either way —
    #    anchoring appends lines and so moves the head, and a loop that
    #    re-read it would chase its own tail forever. Skipped entirely when
    #    no non-anchor line was appended since the last anchor: there is
    #    then nothing new to bound.
    head_subject, head_digest, new_content = _head_and_new_content(ledger_dir)
    if head_subject is not None and head_digest is not None and new_content:
        _anchor_subject_with(
            ledger_dir,
            anchorers,
            subject=head_subject,
            digest=head_digest,
            timeout=timeout,
        )
        report.head_subject = head_subject

    # 3. Pending OpenTimestamps proofs.
    report.upgraded.extend(_upgrade_pending(ledger_dir, anchorers))

    # 4. The off-machine mirror, last.
    report.mirrored = mirror_witness_anchors(ledger_dir, wiki_root, lock=lock)
    return report
