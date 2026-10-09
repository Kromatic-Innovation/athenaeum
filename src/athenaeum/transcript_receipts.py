# SPDX-License-Identifier: Apache-2.0
"""Durable transcript-provenance receipts (issue athenaeum#753, Option A).

athenaeum#752 (``resolutions._transcript_authorizes_correct``) gates the resolver's
destructive ``correct_a``/``correct_b`` delete on whether the winning claim
traces to a genuine human utterance in the origin-session transcript, checked
LIVE at enact time via :func:`athenaeum.transcript_verify.classify_backfill_claim`.
That is the strongest available check, but the transcript is not durable (it
rolls off) and the check only runs while it still exists.

This module adds a **receipt**: a durable, no-plaintext record written at
INTAKE time (while the transcript is still checkable) that lets the gate
authorize a `correct_*` delete after roll-off, and lets it DETECT a transcript
edited after the receipt was written. It does not close the pre-receipt
forgery window (an agent that forges a transcript line before the nightly
intake pass gets the forgery minted as genuine) — see the module's "Known
limits" note and ``docs/design/conflict-resolution.md``.

**No plaintext, ever.** A receipt carries only digests, a channel, and origin
coordinates — never the claim text or the utterance text. Human utterances
are short, so a published digest is dictionary-attack-recoverable; this
module never recommends publishing receipts anywhere outside the knowledge
repo, and git history (the only tamper-evidence layer here — the operator
confirmed the remote cannot refuse force-push) is the log, not a seal.

**One shared claim/digest derivation.** :func:`member_claim_and_origin` is
the SAME function :func:`athenaeum.resolutions._member_origin_and_claim`
delegates to (moved here so the ledger module and the gate agree on what
"the claim" is without either importing the other's heavy module), and
:func:`memory_digest` is the ONE function both the intake-time writer and the
gate's receipt lookup call to turn a claim into its lookup key. If either
side ever derived the claim differently, every receipt would mismatch
silently and the feature would do nothing — this is why both paths route
through these two functions rather than each hashing its own copy.

**Storage.** ``wiki/_transcript_receipts/<YYYY-MM>.jsonl``, monthly
partitions, ``store.append_line_durable`` (``O_APPEND`` + fsync, torn
trailing line tolerated by every reader here) — the same shape as
:mod:`athenaeum.verdicts`. Mutating functions require an ALREADY-ACQUIRED
:class:`athenaeum.runlock.RunLock`, exactly like :mod:`athenaeum.verdicts`'s
single-appender contract; see that module's docstring for why a second
independent ``RunLock(...).acquire()`` from within an already-locked run
would deadlock.

**The sealer seam (operator disposition 2026-10-09, occam session
8fa7ec4a).** Git history is a log, not a cryptographic seal — the knowledge
remote cannot refuse force-push. :class:`Sealer` is a small interface over a
contiguous range of just-appended receipt lines (``seal``) and a way to
check a previously-sealed range (``verify``). :data:`DEFAULT_SEALER` is
:class:`NoopSealer`: it records nothing beyond the ledger line itself. The
two deferred sealers — an operator-held signing key (issue athenaeum#2039)
and external timestamp anchoring (issue athenaeum#2040) — are meant to
implement this interface without this module changing at all.

Layering: L2 (domain/pipeline), mirroring :mod:`athenaeum.verdicts`. Imports
:mod:`athenaeum.atomic_io`/:mod:`athenaeum.store` (L0/L1),
:mod:`athenaeum.runlock` (L0), :mod:`athenaeum.models` (L1, frontmatter
parsing), and :mod:`athenaeum.transcript_verify` (L1, the classification
primitive and its human/tool text extractors — imported, never
reimplemented). Deliberately does NOT import :mod:`athenaeum.resolutions`
(L-undeclared but heavy) to avoid a cycle; `resolutions.py` imports FROM
here instead.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from athenaeum.models import parse_frontmatter
from athenaeum.runlock import RunLock
from athenaeum.store import append_line_durable
from athenaeum.transcript_verify import (
    BackfillClassification,
    _is_user_record,
    _iter_session_records,
    _normalize,
    _user_authored_text,
    classify_backfill_claim,
    default_projects_root,
)

log = logging.getLogger(__name__)

#: Receipt schema version, stamped on every entry.
SCHEMA_VERSION = 1

#: Directory (under ``wiki_root``) holding the monthly receipt partitions.
RECEIPTS_DIRNAME = "_transcript_receipts"


class TranscriptReceiptsError(RuntimeError):
    """Base class for receipt-ledger errors."""


class LockNotHeld(TranscriptReceiptsError):
    """Raised when a mutating call is made without an acquired :class:`RunLock`.

    Mirrors :mod:`athenaeum.verdicts`'s ``LockNotHeld`` — every writer in
    THIS module reuses :mod:`athenaeum.runlock` rather than inventing a
    second lock; the caller must hold the SAME lock the CLI's mutating
    commands already take.
    """


def _require_lock(lock: RunLock) -> None:
    if not getattr(lock, "acquired", False):
        raise LockNotHeld(
            "transcript_receipts: mutating call made without an acquired "
            "RunLock — every writer in this module reuses "
            "athenaeum.runlock.RunLock rather than inventing a second lock "
            "(issue athenaeum#753)."
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Shared claim derivation + digest (addendum item 2)
# ---------------------------------------------------------------------------


def member_claim_and_origin(path: Path) -> tuple[str, str | None, int | None, str]:
    """Read ``(origin_scope, origin_session_id, origin_turn, claim)`` from a member file.

    THE shared derivation :func:`athenaeum.resolutions._member_origin_and_claim`
    delegates to — moved here (issue athenaeum#753 addendum item 2) so the
    intake-time receipt writer and the enact-time gate compute the claim
    identically without either module importing the other's heavier one.
    Reads raw frontmatter directly (no full ``AutoMemoryFile`` discovery pass
    needed for a single known path); falls back to the body text for the
    claim, then to the frontmatter ``name``/``description``. Best-effort: an
    unreadable file returns empty/None fields rather than raising — the
    caller treats that as "cannot verify" (escalate).

    ``origin_scope`` is NEVER stored in frontmatter — it is always the
    member's PARENT DIRECTORY name (``raw/auto-memory/<scope>/<file>.md``).
    """
    origin_scope = path.parent.name
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return origin_scope, None, None, ""
    meta, body = parse_frontmatter(text)
    meta = meta if isinstance(meta, dict) else {}
    origin_session_id = meta.get("originSessionId")
    origin_session_id = str(origin_session_id) if origin_session_id is not None else None
    origin_turn_raw = meta.get("originTurn")
    origin_turn: int | None
    try:
        origin_turn = int(origin_turn_raw) if origin_turn_raw is not None else None
    except (TypeError, ValueError):
        origin_turn = None
    claim = body.strip()
    if not claim:
        name = meta.get("name")
        description = meta.get("description")
        claim = str(description or name or "")
    return origin_scope, origin_session_id, origin_turn, claim


def memory_digest(claim: str) -> str:
    """SHA-256 hex digest of a claim's text — THE shared lookup key.

    Both the intake-time writer (:func:`write_receipt_for_origin`) and the
    enact-time gate's receipt lookup (``resolutions._transcript_authorizes_correct``,
    via :func:`classify_for_correct_gate`) call this SAME function over the
    SAME claim text (from :func:`member_claim_and_origin`) — issue
    athenaeum#753 addendum item 2's guard against silent, divergent hashing.
    Raw claim text (no whitespace normalization): an edited memory produces
    a different digest and therefore invalidates its old receipt, which is
    the intended behaviour.
    """
    return hashlib.sha256(claim.strip().encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Sealer seam (operator disposition: build the seam now, sealers later)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SealRecord:
    """What a :class:`Sealer` produced for one appended range. Opaque to this module."""

    sealer: str
    detail: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Sealer(Protocol):
    """A seam over the receipt ledger: seal a contiguous range, verify a seal.

    Issues athenaeum#2039 (operator-held signing key) and athenaeum#2040
    (external timestamp anchoring) implement this Protocol WITHOUT touching
    this module. ``seal`` is called once per contiguous range of raw JSONL
    lines just appended to one partition; ``verify`` re-checks a
    previously-produced :class:`SealRecord` against that same range.
    """

    def seal(
        self, partition_path: Path, start_line: int, end_line: int, lines: list[str]
    ) -> SealRecord | None: ...

    def verify(
        self, partition_path: Path, start_line: int, end_line: int, seal: SealRecord | None
    ) -> bool: ...


class NoopSealer:
    """Default :class:`Sealer`: records nothing beyond the ledger line itself.

    ``seal`` returns ``None`` (no :class:`SealRecord` is produced or stored
    anywhere) and ``verify`` trivially accepts a ``None`` seal — git history
    of the knowledge repo is the only tamper-evidence layer with this sealer,
    per the operator's 2026-10-09 disposition (the remote cannot refuse
    force-push, so that history is a log, not a cryptographic guarantee).
    """

    name = "noop"

    def seal(
        self, partition_path: Path, start_line: int, end_line: int, lines: list[str]
    ) -> SealRecord | None:
        del partition_path, start_line, end_line, lines
        return None

    def verify(
        self, partition_path: Path, start_line: int, end_line: int, seal: SealRecord | None
    ) -> bool:
        del partition_path, start_line, end_line
        return seal is None


#: The seam's default. Pass a different :class:`Sealer` (athenaeum#2039/#2040)
#: to :func:`append_receipt` to change sealing behavior — selection logic
#: lives OUTSIDE this module, never hard-coded here.
DEFAULT_SEALER: Sealer = NoopSealer()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


@dataclass
class ReceiptEntry:
    """One intake-time transcript-verification receipt (issue athenaeum#753).

    NO PLAINTEXT: every field is either a digest, an origin coordinate, or a
    channel label. Never the claim text, never the utterance text.
    """

    origin_scope: str
    memory_digest: str
    channel: str
    origin_session_id: str | None = None
    origin_turn: int | None = None
    utterance_digest: str = ""
    transcript_prefix_digest: str = ""
    transcript_line_count: int = 0
    recorded_at: str = ""
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "origin_scope": self.origin_scope,
            "memory_digest": self.memory_digest,
            "channel": self.channel,
            "origin_session_id": self.origin_session_id,
            "origin_turn": self.origin_turn,
            "utterance_digest": self.utterance_digest,
            "transcript_prefix_digest": self.transcript_prefix_digest,
            "transcript_line_count": self.transcript_line_count,
            "recorded_at": self.recorded_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ReceiptEntry:
        return cls(
            origin_scope=str(d.get("origin_scope", "")),
            memory_digest=str(d.get("memory_digest", "")),
            channel=str(d.get("channel", "")),
            origin_session_id=d.get("origin_session_id"),
            origin_turn=d.get("origin_turn"),
            utterance_digest=str(d.get("utterance_digest", "")),
            transcript_prefix_digest=str(d.get("transcript_prefix_digest", "")),
            transcript_line_count=int(d.get("transcript_line_count", 0) or 0),
            recorded_at=str(d.get("recorded_at", "")),
            schema_version=int(d.get("schema_version", SCHEMA_VERSION)),
        )


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def ledger_dir(wiki_root: Path) -> Path:
    return Path(wiki_root) / RECEIPTS_DIRNAME


def partition_path(wiki_root: Path, month: str) -> Path:
    """Live partition path for ``month`` (``"YYYY-MM"``)."""
    return ledger_dir(wiki_root) / f"{month}.jsonl"


def _read_jsonl_tolerant(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # torn trailing write or hand-edit; skip
        if isinstance(record, dict):
            out.append(record)
    return out


# ---------------------------------------------------------------------------
# Transcript prefix digest (resumed-session-safe tamper detection)
# ---------------------------------------------------------------------------


def _complete_lines(raw: bytes) -> list[bytes]:
    """Split *raw* on ``b"\\n"`` and drop the final element.

    Counts only COMPLETE, newline-terminated lines: if the file ends with a
    trailing ``\\n`` the split's last element is an empty string and is
    dropped; if the file was torn mid-write the last element is a partial
    line and is ALSO dropped (it may be re-completed on the next flush, and
    counting it now would make a correctly-resumed session look "modified"
    the moment the torn line finishes writing).
    """
    return raw.split(b"\n")[:-1]


def compute_transcript_prefix(path: Path) -> tuple[str, int] | None:
    """``(digest, line_count)`` over every complete line in *path* right now.

    ``digest`` is a SHA-256 of the complete lines joined by ``b"\\n"``.
    Returns ``None`` when *path* does not exist or cannot be read — the
    caller treats that as "no receipt" (never minted from absence).
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    lines = _complete_lines(raw)
    digest = hashlib.sha256(b"\n".join(lines)).hexdigest()
    return digest, len(lines)


def prefix_matches_receipt(path: Path, receipt: ReceiptEntry) -> bool:
    """True iff *path*'s first ``receipt.transcript_line_count`` complete
    lines still hash to ``receipt.transcript_prefix_digest``.

    A session RESUMED (lines appended after the receipt was written) still
    matches — only the first N lines are re-hashed, and appended lines never
    touch the prefix. A transcript with FEWER complete lines than the
    receipt recorded (truncated) counts as a mismatch, same as an edited
    line: the caller cannot re-derive the original prefix to confirm it, so
    refusing is the safe read.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return False
    lines = _complete_lines(raw)
    if len(lines) < receipt.transcript_line_count:
        return False
    prefix = lines[: receipt.transcript_line_count]
    digest = hashlib.sha256(b"\n".join(prefix)).hexdigest()
    return digest == receipt.transcript_prefix_digest


# ---------------------------------------------------------------------------
# Writer — append (single-appender enforced via RunLock)
# ---------------------------------------------------------------------------


def append_receipt(
    wiki_root: Path,
    entry: ReceiptEntry,
    *,
    lock: RunLock,
    sealer: Sealer = DEFAULT_SEALER,
) -> Path:
    """Append *entry* to its month's partition. Returns the partition path.

    Requires an ALREADY-ACQUIRED ``lock`` — raises :class:`LockNotHeld`
    otherwise. Idempotent: if an entry with the SAME
    ``(origin_scope, memory_digest, channel, origin_session_id, origin_turn,
    transcript_prefix_digest, transcript_line_count)`` already exists in any
    partition, nothing is written and the existing partition path is
    returned — a re-run of the same intake pass never duplicates a line.
    Deliberately does NOT key idempotency on ``transcript_prefix_digest``
    alone extending the identity (a tampered transcript re-running the
    writer must NOT silently mint a fresh receipt that launders the old
    mismatch away — the identity key is the ``(origin_scope, memory_digest)``
    pair's FULL content tuple, so a changed prefix digest for the SAME
    ``(scope, digest)`` is a genuinely new entry, append it; the gate below
    always prefers the receipt whose content it can still verify).
    """
    _require_lock(lock)
    month = entry.recorded_at[:7] if entry.recorded_at else _now_iso()[:7]
    path = partition_path(wiki_root, month)

    existing_key = (
        entry.origin_scope,
        entry.memory_digest,
        entry.channel,
        entry.origin_session_id,
        entry.origin_turn,
        entry.transcript_prefix_digest,
        entry.transcript_line_count,
    )
    for existing in iter_receipts(wiki_root):
        key = (
            existing.origin_scope,
            existing.memory_digest,
            existing.channel,
            existing.origin_session_id,
            existing.origin_turn,
            existing.transcript_prefix_digest,
            existing.transcript_line_count,
        )
        if key == existing_key:
            return path

    line = json.dumps(entry.to_dict(), separators=(",", ":")) + "\n"
    append_line_durable(path, line.encode("utf-8"))

    try:
        current_count = len(_read_jsonl_tolerant(path))
        sealer.seal(path, current_count, current_count, [line.rstrip("\n")])
    except Exception as exc:  # noqa: BLE001 — a sealer must never break intake.
        log.warning("transcript_receipts: sealer %r failed (non-fatal): %s", sealer, exc)

    return path


def verify_seals(
    wiki_root: Path,
    month: str,
    start_line: int,
    end_line: int,
    seal: SealRecord | None,
    *,
    sealer: Sealer = DEFAULT_SEALER,
) -> bool:
    """Delegate seal verification for partition ``month``, lines
    ``start_line``..``end_line``, to *sealer*."""
    return sealer.verify(partition_path(wiki_root, month), start_line, end_line, seal)


# ---------------------------------------------------------------------------
# Reader — all partitions, lookup by (origin_scope, memory_digest)
# ---------------------------------------------------------------------------


def iter_receipts(wiki_root: Path) -> list[ReceiptEntry]:
    """Every receipt across every monthly partition."""
    d = ledger_dir(wiki_root)
    if not d.is_dir():
        return []
    out: list[ReceiptEntry] = []
    for p in sorted(d.glob("*.jsonl")):
        for record in _read_jsonl_tolerant(p):
            out.append(ReceiptEntry.from_dict(record))
    return out


def lookup_receipt(wiki_root: Path, origin_scope: str, digest: str) -> ReceiptEntry | None:
    """The most recently recorded receipt for ``(origin_scope, digest)``, or ``None``.

    Looked up by ``(origin_scope, memory_digest)`` — NEVER by frontmatter
    ``originSessionId`` (issue athenaeum#753 addendum item 1), so a member
    whose session was only ever RECOVERED (never written to frontmatter —
    issue athenaeum#2038's gap) can still be found.
    """
    candidates = [
        r
        for r in iter_receipts(wiki_root)
        if r.origin_scope == origin_scope and r.memory_digest == digest
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda r: r.recorded_at)
    return candidates[-1]


# ---------------------------------------------------------------------------
# Writer entrypoint — one memory, called from intake (issue athenaeum#753)
# ---------------------------------------------------------------------------


def _matching_human_utterance(records: list[object], claim: str) -> str | None:
    """The first human-authored record text whose normalized form contains
    *claim*'s normalized form, or ``None``.

    Reuses :func:`athenaeum.transcript_verify._user_authored_text` and
    :func:`athenaeum.transcript_verify._normalize` — imported, not
    reimplemented (issue athenaeum#753 candidate AC).
    """
    needle = _normalize(claim)
    if not needle:
        return None
    for record in records:
        if not _is_user_record(record):
            continue
        text = _user_authored_text(record)
        if text and needle in _normalize(text):
            return _normalize(text)
    return None


def write_receipt_for_origin(
    wiki_root: Path,
    *,
    origin_scope: str,
    origin_session_id: str | None,
    origin_turn: int | None,
    claim: str,
    projects_root: Path | None,
    lock: RunLock,
    sealer: Sealer = DEFAULT_SEALER,
    now: str | None = None,
) -> ReceiptEntry | None:
    """Classify *claim* against its origin transcript and append a receipt.

    Writes NOTHING when the transcript is absent/rolled off — a receipt is
    never minted from absence (``classify_backfill_claim`` returning
    ``"unavailable"``), and nothing when ``origin_session_id`` is unset.
    Otherwise appends exactly one receipt (idempotent — see
    :func:`append_receipt`) carrying the classified ``channel`` (one of
    ``"user-stated"``, ``"agent-observed"``, ``"inferred"``) and returns it.
    """
    if not origin_session_id:
        return None
    root = projects_root if projects_root is not None else default_projects_root()
    transcript_path = root / origin_scope / f"{origin_session_id}.jsonl"

    classification: BackfillClassification = classify_backfill_claim(
        origin_scope,
        origin_session_id,
        origin_turn,
        claim=claim,
        projects_root=projects_root,
    )
    if classification.channel == "unavailable":
        return None

    records = _iter_session_records(root / origin_scope, origin_session_id)
    utterance_digest = ""
    if classification.channel == "user-stated":
        matched = _matching_human_utterance(records, claim)
        if matched:
            utterance_digest = hashlib.sha256(matched.encode("utf-8")).hexdigest()

    prefix = compute_transcript_prefix(transcript_path)
    if prefix is None:
        return None
    prefix_digest, line_count = prefix

    entry = ReceiptEntry(
        origin_scope=origin_scope,
        memory_digest=memory_digest(claim),
        channel=classification.channel,
        origin_session_id=origin_session_id,
        origin_turn=origin_turn,
        utterance_digest=utterance_digest,
        transcript_prefix_digest=prefix_digest,
        transcript_line_count=line_count,
        recorded_at=now if now is not None else _now_iso(),
    )
    append_receipt(wiki_root, entry, lock=lock, sealer=sealer)
    return entry


# ---------------------------------------------------------------------------
# Gate decision table — issue athenaeum#753 (design comment + addendum +
# occam:disposition)
# ---------------------------------------------------------------------------


def classify_for_correct_gate(
    origin_scope: str,
    origin_session_id: str | None,
    origin_turn: int | None,
    claim: str,
    *,
    projects_root: Path | None = None,
    wiki_root: Path | None = None,
    receipts_enabled: bool = False,
) -> tuple[bool, str]:
    """The receipt-aware half of ``resolutions._transcript_authorizes_correct``.

    Decision table (issue athenaeum#753):

    - transcript present, no receipt, or receipt's prefix digest matches:
      **unchanged** — authorize iff ``classify_backfill_claim`` says
      ``"user-stated"``, exactly as athenaeum#752 always did.
    - transcript present, receipt's prefix digest MISMATCHES: **refuse**,
      ref ``"transcript-modified <session>"``.
    - transcript absent, a ``"user-stated"`` receipt matches
      ``(origin_scope, memory_digest(claim))``: **authorize**, ref
      ``"receipt <ref>"``.
    - transcript absent, no receipt, or receipt's channel is not
      ``"user-stated"``: **refuse**, same ``"unavailable <ref>"`` athenaeum#752
      always returned.
    - ``origin_session_id`` absent from frontmatter: receipts are looked up
      by ``(origin_scope, memory_digest)`` regardless (addendum item 1), and
      the receipt's OWN recorded session/turn is used for the live-transcript
      check when one is found.

    This function NEVER reads frontmatter ``source_type`` (the athenaeum#752
    pin extends unchanged to the receipt path: a forged ``source_type`` with
    no receipt and no transcript still refuses, because neither this
    function nor its caller ever looks at that field).

    With ``receipts_enabled=False`` (the default) or ``wiki_root=None``, no
    receipt lookup happens at all and the return is byte-identical to
    athenaeum#752's original ``_transcript_authorizes_correct`` body.
    """
    receipt: ReceiptEntry | None = None
    if receipts_enabled and wiki_root is not None:
        digest = memory_digest(claim)
        receipt = lookup_receipt(wiki_root, origin_scope, digest)

    effective_session = origin_session_id or (receipt.origin_session_id if receipt else None)
    effective_turn = (
        origin_turn if origin_session_id else (receipt.origin_turn if receipt else None)
    )

    if not effective_session:
        return False, "no origin session recorded"

    classification = classify_backfill_claim(
        origin_scope,
        effective_session,
        effective_turn,
        claim=claim,
        projects_root=projects_root,
    )
    channel_ref = f"{classification.channel} {classification.ref}".strip()

    if classification.channel != "unavailable":
        # Transcript present: today's live-classification result wins,
        # UNLESS a receipt exists and its recorded prefix no longer matches
        # — that is the tamper signal.
        if receipt is not None:
            root = projects_root if projects_root is not None else default_projects_root()
            transcript_path = root / origin_scope / f"{effective_session}.jsonl"
            if not prefix_matches_receipt(transcript_path, receipt):
                return False, f"transcript-modified {effective_session}"
        return classification.channel == "user-stated", channel_ref

    # Transcript absent (rolled off / never captured).
    if receipt is not None and receipt.channel == "user-stated":
        ref = (
            f"{effective_session}#turn{effective_turn}"
            if effective_turn is not None
            else str(effective_session)
        )
        return True, f"receipt {ref}"
    return False, channel_ref
