# SPDX-License-Identifier: Apache-2.0
"""External anchoring: the ``Anchorer`` protocol, the mandatory
``LocalClockAnchorer``, and the ``OpenTimestampsAnchorer`` (athenaeum#2151).

Layering: L1.5 (depends only on :mod:`promptwitness.canon`, stdlib; does
**not** import :mod:`promptwitness.ledger` -- the CLI layer wires anchoring
to the ledger). Standard library only at import time -- ``opentimestamps``
is imported lazily, inside the methods that need it, never at module scope
-- so a bare ``import promptwitness.anchor`` costs nothing when the
``witness-anchor`` extra is not installed (``tests/test_promptwitness_boundary.py``'s
runtime check covers this file too).

See ``docs/design/witness-ledger.md`` ("Anchoring") and athenaeum#2144
operator ruling 3 for the full design and the block-source policy this
module implements.

**``Attestation``** is what one anchorer's :meth:`Anchorer.anchor` (or
:meth:`Anchorer.upgrade`) pass produces; **``VerifyResult``** is what
:meth:`Anchorer.verify` produces. Both record their own ``source`` /
``verify_source`` on every call -- never inferred later from context.

**Failure never raises.** Network failure, a timeout, or a missing optional
dependency degrades to a ``status: "failed"`` :class:`Attestation` (for
:class:`OpenTimestampsAnchorer`) or a ``"complete-unverified-block"``
:class:`VerifyResult` (for a verification whose block source could not be
reached) -- never an exception. :class:`LocalClockAnchorer` cannot fail: it
reads only the local clock and (best-effort) the boot id.

**``OpenTimestampsAnchorer``'s real-library surface is deliberately small.**
Every name this module imports from ``opentimestamps`` is listed here, and
nowhere else, so the in-test stub
(``tests/test_promptwitness_anchor.py``'s ``_install_fake_opentimestamps``)
can mirror exactly this list:

- ``opentimestamps.calendar.RemoteCalendar`` -- ``.submit(digest, timeout=)``,
  ``.get_timestamp(commitment, timeout=)``.
- ``opentimestamps.calendar.CommitmentNotFoundError``.
- ``opentimestamps.core.timestamp.Timestamp`` -- ``.all_attestations()``,
  ``.serialize(ctx)``, ``.deserialize(ctx, initial_msg)`` (classmethod),
  ``.merge(other)``, ``.msg``, ``.ops``.
- ``opentimestamps.core.notary.PendingAttestation`` (``.uri``),
  ``BitcoinBlockHeaderAttestation`` (``.height``).
- ``opentimestamps.core.serialize.BytesSerializationContext`` (``.getbytes()``),
  ``BytesDeserializationContext(buf)``.

**Single-calendar choice.** The reference ``ots`` client submits to every
configured calendar in parallel and merges the results for redundancy. This
adapter tries calendars *in order* and keeps the first success -- simpler,
and sufficient for "public calendars, no account needed, recorded like the
stamp source" -- recording every calendar it tried in
``Attestation.detail["calendars_tried"]``.

**Full Bitcoin verification never imports a Bitcoin library.** A block
header is a fixed 80 bytes; :func:`_parse_block_header` reads the merkle
root and timestamp with :mod:`struct` directly, so verification adds no new
declared dependency (``scripts/check_declared_imports.py``) beyond the
stdlib ``urllib.request`` HTTP call, itself routed through the
:data:`OpenTimestampsAnchorer._http_get` seam so tests can feed it recorded
bytes (or assert it was never called, for ``--block-source offline``)
without monkeypatching ``urllib`` globally.
"""

from __future__ import annotations

import os
import re
import struct
import time
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

__all__ = [
    "DEFAULT_BLOCK_SOURCE",
    "DEFAULT_CALENDARS",
    "Attestation",
    "VerifyResult",
    "Anchorer",
    "LocalClockAnchorer",
    "OpenTimestampsAnchorer",
    "resolve_block_source",
    "digest_bytes",
    "subject_safe",
]

#: A single Esplora-compatible public block explorer -- an operator can
#: override per call (``--block-source``) or per environment
#: (``PROMPTWITNESS_OTS_BLOCK_SOURCE``); see :func:`resolve_block_source`.
#: No account or API key needed; GET-only, two endpoints
#: (``/block-height/<n>``, ``/block/<hash>/header``).
DEFAULT_BLOCK_SOURCE = "https://blockstream.info/api"

#: Default public OpenTimestamps calendars (no account needed), per the
#: issue body and ``docs/design/witness-ledger.md``.
DEFAULT_CALENDARS: tuple[str, ...] = (
    "https://a.pool.opentimestamps.org",
    "https://b.pool.opentimestamps.org",
    "https://a.pool.eternitywall.com",
)

_SUBJECT_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")
_SHA256_PREFIX = "sha256:"


def subject_safe(subject: str) -> str:
    """Filesystem-safe form of *subject* for ``anchors/<subject-safe>.ots``."""
    return _SUBJECT_SAFE_RE.sub("_", subject)


def resolve_block_source(override: str | None = None) -> str:
    """Precedence: *override* (``--block-source``) > ``PROMPTWITNESS_OTS_BLOCK_SOURCE``
    env > :data:`DEFAULT_BLOCK_SOURCE`. ``"offline"`` is a sentinel at any
    level -- it means "do no network access at all", not a URL.
    """
    if override:
        return override
    env = os.environ.get("PROMPTWITNESS_OTS_BLOCK_SOURCE")
    if env:
        return env
    return DEFAULT_BLOCK_SOURCE


def digest_bytes(digest: str) -> bytes:
    """``"sha256:<hex>"`` (or bare ``<hex>``) -> raw 32 bytes."""
    hex_part = digest[len(_SHA256_PREFIX) :] if digest.startswith(_SHA256_PREFIX) else digest
    return bytes.fromhex(hex_part)


def _now_iso_ms(when: datetime | None = None) -> str:
    dt = when if when is not None else datetime.now(timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# Attestation / VerifyResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Attestation:
    """What one :meth:`Anchorer.anchor` (or :meth:`Anchorer.upgrade`) pass
    produced. Recorded verbatim into the ledger's ``anchor`` line
    (``promptwitness.cli``'s ``_cmd_anchor``), so every field here must be
    JSON-safe (:func:`promptwitness.canon.canonical_json`-serializable).
    """

    source: str
    status: str  # "complete" | "pending" | "failed"
    claimed_time: str
    proof_path: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "status": self.status,
            "claimed_time": self.claimed_time,
            "proof_path": self.proof_path,
            "detail": dict(self.detail),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Attestation:
        return cls(
            source=str(d.get("source", "")),
            status=str(d.get("status", "")),
            claimed_time=str(d.get("claimed_time", "")),
            proof_path=d.get("proof_path"),
            detail=dict(d.get("detail") or {}),
        )


@dataclass(frozen=True)
class VerifyResult:
    """What :meth:`Anchorer.verify` produced. ``verify_source`` is recorded
    on every call, success or failure -- e.g. ``"block-explorer:<host>"``,
    ``"local-clock"``, or ``"offline"``.
    """

    status: str  # "complete" | "pending" | "complete-unverified-block" | "failed"
    verify_source: str
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "verify_source": self.verify_source,
            "detail": dict(self.detail),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> VerifyResult:
        return cls(
            status=str(d.get("status", "")),
            verify_source=str(d.get("verify_source", "")),
            detail=dict(d.get("detail") or {}),
        )


@runtime_checkable
class Anchorer(Protocol):
    """One anchoring backend. ``source`` identifies it on every
    :class:`Attestation` it produces (``"local-clock"``,
    ``"opentimestamps"``, or ``"rfc3161:<tsa-host>"`` --
    :class:`~promptwitness.anchor_rfc3161.Rfc3161Anchorer`, athenaeum#2157).
    """

    source: str

    def anchor(self, digest: bytes, subject: str, *, timeout: float = 10.0) -> Attestation: ...

    def upgrade(self, att: Attestation) -> Attestation: ...

    def verify(self, att: Attestation, digest: bytes) -> VerifyResult: ...


# ---------------------------------------------------------------------------
# LocalClockAnchorer -- mandatory, always runs, never fails
# ---------------------------------------------------------------------------


def _read_boot_id() -> str | None:
    """Best-effort Linux boot id (``/proc/sys/kernel/random/boot_id``);
    ``None`` on any platform or error -- this is a fallback marker, not
    evidence, so a missing boot id degrades the ``detail`` payload, never
    the attestation itself.
    """
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip() or None
    except OSError:
        return None


class LocalClockAnchorer:
    """The mandatory fallback (operator ruling, athenaeum#2144): wall-clock
    UTC, ``time.monotonic_ns()``, and the boot id where available. Always
    ``status: "complete"`` -- it cannot fail -- but carries no independent
    weight (``detail["weight"]``): it proves nothing about *when* a line
    was written, only what the machine's own clock claims.
    """

    source = "local-clock"

    def anchor(self, digest: bytes, subject: str, *, timeout: float = 10.0) -> Attestation:
        # not needed: the local clock attests to "now", not to any digest
        del digest, subject, timeout
        return Attestation(
            source=self.source,
            status="complete",
            claimed_time=_now_iso_ms(),
            proof_path=None,
            detail={
                "monotonic_ns": time.monotonic_ns(),
                "boot_id": _read_boot_id(),
                "weight": "none-independent",
            },
        )

    def upgrade(self, att: Attestation) -> Attestation:
        return att  # no-op: there is nothing to upgrade about a clock reading

    def verify(self, att: Attestation, digest: bytes) -> VerifyResult:
        del digest
        return VerifyResult(
            status="complete",
            verify_source=self.source,
            detail={"weight": "none-independent"},
        )


# ---------------------------------------------------------------------------
# Block header parsing -- stdlib struct, no python-bitcoinlib dependency
# ---------------------------------------------------------------------------


def _parse_block_header(raw_hex: str) -> tuple[bytes, int]:
    """``(merkle_root, n_time)`` from an 80-byte Bitcoin block header hex
    string (the Esplora ``/block/<hash>/header`` response shape).

    Wire layout, little-endian: ``version(4) + prev_block(32) +
    merkle_root(32) + time(4) + bits(4) + nonce(4)``. Hand-parsed with
    :mod:`struct` rather than depending on ``python-bitcoinlib`` for a
    fixed, well-documented 80-byte struct -- ``opentimestamps`` itself
    depends on that library transitively for its own tree walk, but this
    adapter's verification path does not import it directly, so it adds no
    new entry to ``pyproject.toml`` (``scripts/check_declared_imports.py``).
    The merkle root is returned in the same (internal, unreversed) byte
    order ``opentimestamps``'s commitment digests use, so no byte-swap is
    needed before comparing the two.
    """
    raw = bytes.fromhex(raw_hex)
    if len(raw) != 80:
        raise ValueError(f"expected an 80-byte block header, got {len(raw)} bytes")
    merkle_root = raw[36:68]
    (n_time,) = struct.unpack("<I", raw[68:72])
    return merkle_root, n_time


def _default_http_get(url: str, timeout: float) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read()


# ---------------------------------------------------------------------------
# OpenTimestampsAnchorer
# ---------------------------------------------------------------------------


def _classify_timestamp(
    ts: Any, pending_cls: type[Any], complete_cls: type[Any] | None = None
) -> tuple[str | None, str]:
    """Walk ``ts.all_attestations()`` for the first attestation found.

    Returns ``(commitment_hex, status)`` where ``commitment_hex`` is the
    message at that attestation's tree node -- the commitment to re-query on
    a later :meth:`OpenTimestampsAnchorer.upgrade` pass. ``status`` is
    ``"complete"`` for a *complete_cls* match, ``"pending"`` for a
    *pending_cls* match, else ``"failed"`` (an unrecognized or empty tree --
    should not happen against a real calendar, but the reader must degrade,
    never crash, the same discipline ``ledger.iter_lines`` already applies).
    """
    for msg, attestation in ts.all_attestations():
        if complete_cls is not None and isinstance(attestation, complete_cls):
            return msg.hex(), "complete"
        if isinstance(attestation, pending_cls):
            return msg.hex(), "pending"
    return None, "failed"


def _find_subtree(ts: Any, target_msg: bytes) -> Any | None:
    """Depth-first search of *ts* for the node whose ``.msg == target_msg``."""
    if ts.msg == target_msg:
        return ts
    for sub in ts.ops.values():
        found = _find_subtree(sub, target_msg)
        if found is not None:
            return found
    return None


def _find_bitcoin_attestation(
    ts: Any, complete_cls: type[Any]
) -> tuple[int | None, bytes | None]:
    """``(height, commitment_msg)`` of the first *complete_cls* attestation
    found in *ts*, or ``(None, None)`` if the proof is still only pending."""
    for msg, attestation in ts.all_attestations():
        if isinstance(attestation, complete_cls):
            return attestation.height, msg
    return None, None


class OpenTimestampsAnchorer:
    """OpenTimestamps adapter: public-calendar stamping, upgrade, and full
    Bitcoin verification against a configurable block source (athenaeum#2144
    operator ruling 3). See the module docstring for the exact real-library
    surface touched and the single-calendar simplification.

    Proofs are stored at ``<ledger_dir>/anchors/<subject-safe>.ots``.
    """

    source = "opentimestamps"

    def __init__(
        self,
        ledger_dir: str | os.PathLike[str],
        *,
        calendars: Sequence[str] = DEFAULT_CALENDARS,
        block_source: str | None = None,
        http_get: Callable[[str, float], bytes] | None = None,
    ) -> None:
        self._ledger_dir = Path(ledger_dir)
        self._calendars = tuple(calendars)
        self._block_source = block_source if block_source is not None else resolve_block_source()
        #: HTTP seam for block-explorer calls (not calendar calls -- those
        #: go through the lazily-imported ``RemoteCalendar``). Tests inject
        #: a fake to feed recorded bytes, or a spy to assert it was never
        #: called (``--block-source offline``).
        self._http_get: Callable[[str, float], bytes] = http_get or _default_http_get

    def _proof_path(self, subject: str) -> Path:
        return self._ledger_dir / "anchors" / f"{subject_safe(subject)}.ots"

    # -- anchor ----------------------------------------------------------

    def anchor(self, digest: bytes, subject: str, *, timeout: float = 10.0) -> Attestation:
        try:
            from opentimestamps.calendar import RemoteCalendar
            from opentimestamps.core.notary import (
                BitcoinBlockHeaderAttestation,
                PendingAttestation,
            )
            from opentimestamps.core.serialize import (
                BytesDeserializationContext,
                BytesSerializationContext,
            )
            from opentimestamps.core.timestamp import Timestamp
        except Exception as exc:  # noqa: BLE001 - any import failure degrades, never raises
            return Attestation(
                source=self.source,
                status="failed",
                claimed_time=_now_iso_ms(),
                proof_path=None,
                detail={"reason": "import-failed", "error": _short(exc)},
            )

        proof_path = self._proof_path(subject)

        # Defence in depth (review finding #2 on athenaeum#2151/PR athenaeum#2168):
        # never clobber an existing proof whose recorded attestation is
        # already Bitcoin-confirmed -- it is the strongest evidence this
        # design produces, and a confirmed proof cannot be regenerated.
        # `promptwitness.cli._cmd_anchor` already avoids calling `anchor()`
        # for a subject it knows is complete; this is the second,
        # independent guard at the write site itself, since `anchor` is a
        # public method any caller could reach directly.
        if proof_path.is_file():
            try:
                existing_ts = Timestamp.deserialize(
                    BytesDeserializationContext(proof_path.read_bytes()), digest
                )
                existing_commitment_hex, existing_status = _classify_timestamp(
                    existing_ts, PendingAttestation, BitcoinBlockHeaderAttestation
                )
            except Exception:  # noqa: BLE001 - unreadable file: fall through, let submit overwrite it
                existing_status, existing_commitment_hex = None, None
            if existing_status == "complete":
                return Attestation(
                    source=self.source,
                    status="complete",
                    claimed_time=_now_iso_ms(),
                    proof_path=str(proof_path.relative_to(self._ledger_dir)),
                    detail={
                        "reason": "existing-proof-already-complete",
                        "commitment_hex": existing_commitment_hex,
                    },
                )

        tried: list[str] = []
        last_error: str | None = None
        for url in self._calendars:
            tried.append(url)
            try:
                cal = RemoteCalendar(url)
                ts = cal.submit(digest, timeout=timeout)
            except Exception as exc:  # noqa: BLE001 - this calendar failed, try the next one
                last_error = _short(exc)
                continue

            commitment_hex, status = _classify_timestamp(ts, PendingAttestation)
            if commitment_hex is None:
                last_error = "calendar response carried no recognizable attestation"
                continue

            ctx = BytesSerializationContext()
            ts.serialize(ctx)
            try:
                proof_path.parent.mkdir(parents=True, exist_ok=True)
                proof_path.write_bytes(ctx.getbytes())
            except OSError as exc:  # full disk / unwritable dir: never raise
                return Attestation(
                    source=self.source,
                    status="failed",
                    claimed_time=_now_iso_ms(),
                    proof_path=None,
                    detail={
                        "reason": "proof-write-failed",
                        "calendar": url,
                        "calendars_tried": tried,
                        "error": _short(exc),
                    },
                )

            return Attestation(
                source=self.source,
                status=status,
                claimed_time=_now_iso_ms(),
                proof_path=str(proof_path.relative_to(self._ledger_dir)),
                detail={
                    "calendar": url,
                    "calendars_tried": tried,
                    "digest_hex": digest.hex(),
                    "commitment_hex": commitment_hex,
                },
            )

        return Attestation(
            source=self.source,
            status="failed",
            claimed_time=_now_iso_ms(),
            proof_path=None,
            detail={
                "reason": "all-calendars-failed",
                "calendars_tried": tried,
                "error": last_error,
            },
        )

    # -- upgrade -----------------------------------------------------------

    def upgrade(self, att: Attestation) -> Attestation:
        """Ask the calendar that issued *att* whether its pending commitment
        has since been confirmed in Bitcoin. Replaces the stored proof only
        once the new proof's Bitcoin attestation verifies (:meth:`verify`)
        -- a calendar response that cannot be confirmed leaves the existing
        (still-pending) proof untouched, never overwritten with something
        unverified.
        """
        if att.source != self.source or att.status != "pending" or not att.proof_path:
            return att  # nothing to upgrade

        calendar_url = att.detail.get("calendar")
        commitment_hex = att.detail.get("commitment_hex")
        digest_hex = att.detail.get("digest_hex")
        if not calendar_url or not commitment_hex or not digest_hex:
            return att

        try:
            from opentimestamps.calendar import CommitmentNotFoundError, RemoteCalendar
            from opentimestamps.core.notary import (
                BitcoinBlockHeaderAttestation,
                PendingAttestation,
            )
            from opentimestamps.core.serialize import (
                BytesDeserializationContext,
                BytesSerializationContext,
            )
            from opentimestamps.core.timestamp import Timestamp
        except Exception:  # noqa: BLE001 - dependency vanished since anchor(); leave the proof alone
            return att

        try:
            cal = RemoteCalendar(calendar_url)
            new_sub_ts = cal.get_timestamp(bytes.fromhex(commitment_hex), timeout=10.0)
        except CommitmentNotFoundError:
            return att  # still pending -- no change
        except Exception:  # noqa: BLE001 - network failure: never raise, never overwrite
            return att

        _, new_status = _classify_timestamp(
            new_sub_ts, PendingAttestation, BitcoinBlockHeaderAttestation
        )
        if new_status != "complete":
            return att  # "replaces a pending proof only once the new proof verifies"

        digest = digest_bytes(digest_hex)
        proof_path = self._ledger_dir / att.proof_path
        try:
            full_ts = Timestamp.deserialize(
                BytesDeserializationContext(proof_path.read_bytes()), digest
            )
            target = _find_subtree(full_ts, bytes.fromhex(commitment_hex))
            if target is None:
                return att
            target.merge(new_sub_ts)
        except Exception:  # noqa: BLE001 - malformed stored proof: never raise, never overwrite
            return att

        # Verify the MERGED IN-MEMORY tree, not the (still unmodified) file
        # on disk -- the new attestation has not been persisted yet, and
        # must not be, unless this check passes.
        verify_result = self._verify_timestamp_tree(full_ts, BitcoinBlockHeaderAttestation)
        if verify_result.status != "complete":
            return att  # new proof doesn't verify (yet); keep the old pending proof

        ctx = BytesSerializationContext()
        full_ts.serialize(ctx)
        try:
            proof_path.write_bytes(ctx.getbytes())
        except OSError:  # full disk / unwritable file: never raise, keep the old proof
            return att

        return Attestation(
            source=self.source,
            status="complete",
            claimed_time=_now_iso_ms(),
            proof_path=att.proof_path,
            detail={**dict(att.detail), "upgraded_at": _now_iso_ms()},
        )

    # -- verify --------------------------------------------------------

    def verify(self, att: Attestation, digest: bytes) -> VerifyResult:
        """Full Bitcoin verification. ``--block-source offline`` (or
        ``PROMPTWITNESS_OTS_BLOCK_SOURCE=offline``) makes no network access
        at all -- it confirms only that the stored proof's attestation kind
        (never read over the network) and reports ``"pending"`` or
        ``"complete-unverified-block"``. Online, an unreachable block
        source degrades to ``"complete-unverified-block"`` with the
        attempted ``verify_source`` -- never an exception.
        """
        if self._block_source == "offline":
            status = "complete-unverified-block" if att.status == "complete" else "pending"
            return VerifyResult(status=status, verify_source="offline")

        host = urlparse(self._block_source).hostname or self._block_source
        verify_source = f"block-explorer:{host}"

        if att.status != "complete" or not att.proof_path:
            return VerifyResult(status=att.status, verify_source=verify_source)

        try:
            from opentimestamps.core.notary import BitcoinBlockHeaderAttestation
            from opentimestamps.core.serialize import BytesDeserializationContext
            from opentimestamps.core.timestamp import Timestamp
        except Exception as exc:  # noqa: BLE001
            return VerifyResult(
                status="complete-unverified-block",
                verify_source=verify_source,
                detail={"reason": "import-failed", "error": _short(exc)},
            )

        try:
            proof_path = self._ledger_dir / att.proof_path
            full_ts = Timestamp.deserialize(
                BytesDeserializationContext(proof_path.read_bytes()), digest
            )
        except Exception as exc:  # noqa: BLE001 - malformed/missing proof: never raise
            return VerifyResult(
                status="complete-unverified-block",
                verify_source=verify_source,
                detail={"reason": "proof-unreadable", "error": _short(exc)},
            )

        return self._verify_timestamp_tree(full_ts, BitcoinBlockHeaderAttestation)

    def _verify_timestamp_tree(self, full_ts: Any, complete_cls: type[Any]) -> VerifyResult:
        """The shared core of :meth:`verify`: given an already-deserialized
        (or freshly-merged, in-memory) ``Timestamp`` tree, find its Bitcoin
        attestation and check it against the configured block source.
        Factored out so :meth:`upgrade` can verify a merged tree BEFORE
        persisting it, without writing to disk first and reading it back.
        """
        height, commitment = _find_bitcoin_attestation(full_ts, complete_cls)

        if self._block_source == "offline":
            status = "complete-unverified-block" if height is not None else "pending"
            return VerifyResult(status=status, verify_source="offline")

        host = urlparse(self._block_source).hostname or self._block_source
        verify_source = f"block-explorer:{host}"

        if height is None:
            return VerifyResult(status="pending", verify_source=verify_source)

        base = self._block_source.rstrip("/")
        try:
            block_hash_raw = self._http_get(f"{base}/block-height/{height}", 10.0)
            block_hash = block_hash_raw.decode("ascii").strip()
            header_raw = self._http_get(f"{base}/block/{block_hash}/header", 10.0)
            header_hex = header_raw.decode("ascii").strip()
            merkle_root, n_time = _parse_block_header(header_hex)
        except Exception as exc:  # noqa: BLE001 - blocked socket, timeout, bad response: never raise
            return VerifyResult(
                status="complete-unverified-block",
                verify_source=verify_source,
                detail={"reason": "block-source-unreachable", "error": _short(exc)},
            )

        if merkle_root != commitment:
            return VerifyResult(
                status="failed",
                verify_source=verify_source,
                detail={"reason": "merkleroot-mismatch"},
            )

        return VerifyResult(
            status="complete",
            verify_source=verify_source,
            detail={"block_height": height, "block_time": n_time},
        )


def _short(exc: BaseException) -> str:
    """``"<ExceptionType>: <message>"``, capped so a verbose network/library
    error can never approach the ledger's no-prompt-text length budget
    (``ledger.find_oversized_fields``'s 71-char digest-length floor)."""
    return f"{type(exc).__name__}: {exc}"[:200]
