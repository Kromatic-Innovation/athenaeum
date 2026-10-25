# SPDX-License-Identifier: Apache-2.0
"""Real :class:`athenaeum.transcript_receipts.Sealer` implementations and the
selection seam around them (issue athenaeum#2039).

:mod:`athenaeum.transcript_receipts` defines the ``Sealer`` Protocol and ships
:class:`~athenaeum.transcript_receipts.NoopSealer` as its default, and says
explicitly that SELECTION lives outside that module. This module is that
outside: it adds the first real sealer and the one function
(:func:`select_sealer`) that turns config into a sealer. Nothing here is
imported by ``transcript_receipts`` — the dependency runs one way only, which
is what lets a later sealer (external timestamp anchoring, issue
athenaeum#2040) land here without touching the ledger module at all.

**Why a signature at all.** With ``NoopSealer`` the git history of the
knowledge repo is the only tamper evidence for a receipt, and the operator
has confirmed the remote cannot refuse a force-push — so that history is a
log, not a seal. A signature produced with a key the nightly process cannot
read makes a model-authored receipt distinguishable from a human-confirmed
one: forging the ledger now requires the key, not merely write access to the
repo.

**Key placement is an adapter seam, not a hard-coded custody.** Operator
ruling 2026-10-25: :class:`SigningKeyProvider` is the seam;
:class:`OnePasswordServiceAccountKeyProvider` is the FIRST adapter behind it.
A hardware key, a KMS, or an agent-held key are later adapters implementing
the same three methods — never variants of this one, and never a branch
inside it.

**The nightly never needs the key.** :class:`SigningSealer` degrades to
exactly ``NoopSealer`` behaviour when the provider cannot produce a signature
(no service-account token in the environment, ``op`` absent, item
unreadable): ``seal`` logs ONE warning for the whole process and returns
``None`` thereafter, so intake still appends its receipt lines and a nightly
run with ``receipt_sealer: signing`` set but no credential is not blocked and
not noisy. Sealing is then a supervised or batched step —
``athenaeum receipts seal`` — and verification
(``athenaeum receipts verify``) runs anywhere the PUBLIC key is configured.

**Where seals live.** ``<ledger_dir>/_seals/<YYYY-MM>.jsonl``, one JSON object
per sealed range, ``O_APPEND`` + fsync via
:func:`athenaeum.store.append_line_durable` — the same durability shape as the
receipt ledger itself. Deliberately a SUBDIRECTORY: the ledger's own readers
glob ``<ledger_dir>/*.jsonl``, so a sibling ``2026-10.seals.jsonl`` would be
parsed as receipts.

**No plaintext, ever** — the ledger module's rule applies here unchanged. A
seal record carries a key id, an algorithm, a signature, a partition month, a
line range and a digest. :func:`verify_all`'s report is partitions and line
ranges only; no receipt content crosses into it.

Layering: L2, mirroring :mod:`athenaeum.transcript_receipts`. Imports
:mod:`athenaeum.transcript_receipts` (L2, for the ``Sealer`` Protocol and
``SealRecord``), :mod:`athenaeum.config` (L2, the five resolvers) and
:mod:`athenaeum.store` (L1, the durable append). ``cryptography`` is imported
LAZILY, inside the functions that sign or verify, so importing this module
costs nothing on an install without the optional ``signing`` extra.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from athenaeum.config import (
    resolve_receipt_sealer,
    resolve_receipt_signing_key_provider,
    resolve_receipt_signing_op_reference,
    resolve_receipt_signing_public_key_openssh,
    resolve_receipt_signing_public_key_ref,
)
from athenaeum.store import append_line_durable
from athenaeum.transcript_receipts import (
    DEFAULT_SEALER,
    Sealer,
    SealRecord,
    ledger_dir,
    partition_path,
)

if TYPE_CHECKING:  # pragma: no cover - annotation-only
    from collections.abc import Iterator

log = logging.getLogger(__name__)

#: Signature algorithm every provider in this module produces.
ALGORITHM = "ed25519"

#: ``SealRecord.sealer`` for :class:`SigningSealer`.
SIGNING_SEALER_NAME = "signing"

#: Subdirectory of the receipt ledger directory holding seal records. A
#: SUBDIRECTORY, not a sibling file: ``transcript_receipts.iter_receipts``
#: globs ``*.jsonl`` in the ledger directory itself.
SEALS_DIRNAME = "_seals"

#: The environment variable a 1Password service account authenticates with.
#: Its ABSENCE is the signal that this process has no key access at all.
OP_SERVICE_ACCOUNT_TOKEN_ENV = "OP_SERVICE_ACCOUNT_TOKEN"

#: Seconds to wait on one ``op read``. A supervised seal run can afford this;
#: an unattended nightly never reaches it (no token -> no subprocess).
OP_READ_TIMEOUT_SECONDS = 30


class KeyUnavailable(RuntimeError):
    """The signing key cannot be reached from this process.

    Raised by a :class:`SigningKeyProvider` when the credential is absent,
    the backing tool is missing, or the item cannot be read.
    :class:`SigningSealer` treats this as "seal nothing, warn once" rather
    than an error, which is what keeps the unattended nightly path working
    with no key (issue athenaeum#2039). It is NOT raised for a malformed key
    that IS readable — that is a real configuration fault and propagates.
    """


# ---------------------------------------------------------------------------
# The adapter seam
# ---------------------------------------------------------------------------


@runtime_checkable
class SigningKeyProvider(Protocol):
    """Where the signing key lives — the adapter seam for key placement.

    Three methods, no storage assumptions. ``public_key`` returns the RAW
    32-byte Ed25519 public key (not an OpenSSH line, not PEM) so every
    consumer agrees on one encoding; ``key_id`` is DERIVED from those bytes
    (see :func:`derive_key_id`) rather than stored, so a provider cannot
    label a key as something it is not.
    """

    def key_id(self) -> str: ...

    def sign(self, payload: bytes) -> bytes: ...

    def public_key(self) -> bytes: ...


def derive_key_id(public_key_raw: bytes) -> str:
    """``"ed25519:"`` + the first 16 hex of ``sha256`` over the raw 32 bytes.

    Derived, never stored (operator ruling 2026-10-25): two providers handed
    the same key agree on its id without coordinating, and a provider cannot
    mislabel the key it actually holds.
    """
    return f"{ALGORITHM}:{hashlib.sha256(public_key_raw).hexdigest()[:16]}"


def _load_private_key(material: bytes) -> Any:
    """Parse *material* as an Ed25519 private key, OpenSSH or PKCS8 PEM.

    OpenSSH first because that is what a 1Password **SSH Key** item hands
    back (``-----BEGIN OPENSSH PRIVATE KEY-----``); PKCS8 PEM is accepted
    too so a later rotation to a PEM-bearing item needs no code change.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        load_pem_private_key,
        load_ssh_private_key,
    )

    key: Any
    if b"OPENSSH PRIVATE KEY" in material:
        key = load_ssh_private_key(material, password=None)
    else:
        key = load_pem_private_key(material, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(
            f"receipt signing key is {type(key).__name__}, not Ed25519; "
            "regenerate the item with an Ed25519 key"
        )
    return key


def _load_public_key(material: bytes) -> bytes:
    """Parse *material* (an ``ssh-ed25519 ...`` line or a PEM block) to raw bytes."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PublicFormat,
        load_pem_public_key,
        load_ssh_public_key,
    )

    text = material.strip()
    key: Any
    if text.startswith(b"ssh-"):
        key = load_ssh_public_key(text)
    else:
        key = load_pem_public_key(text)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError(
            f"receipt signing public key is {type(key).__name__}, not Ed25519"
        )
    return key.public_bytes(Encoding.Raw, PublicFormat.Raw)


def _raw_public_bytes(private_key: Any) -> bytes:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    return private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


class OnePasswordServiceAccountKeyProvider:
    """Ed25519 key read from 1Password through the ``op`` CLI.

    Reads ``op://<vault>/<item>/<field>`` with a service-account token taken
    from the ENVIRONMENT (:data:`OP_SERVICE_ACCOUNT_TOKEN_ENV`). The private
    key is read inside :meth:`sign`, used, and dropped — it is never written
    to disk, never cached on the instance, and never logged. That is the
    whole point of this adapter: an unattended process without the token
    cannot produce a seal, so a seal is evidence of a supervised step.

    Only the PUBLIC key is memoised (it is not secret, and ``verify`` over a
    whole ledger would otherwise shell out once per range).

    Accepts a 1Password **SSH Key** item, whose ``private key`` field arrives
    in OpenSSH format and whose ``public key`` field arrives as an
    ``ssh-ed25519 AAAA...`` line, and also a PKCS8 PEM item — one parse
    attempt per format, no configuration knob (see :func:`_load_private_key`).
    """

    def __init__(
        self,
        op_reference: str,
        *,
        public_key_reference: str | None = None,
        public_key_openssh: str | None = None,
        op_binary: str = "op",
        env: dict[str, str] | None = None,
    ) -> None:
        self.op_reference = op_reference
        self.public_key_reference = public_key_reference
        self.public_key_openssh = public_key_openssh
        self.op_binary = op_binary
        self._env = env
        self._public_key_raw: bytes | None = None

    # -- credential plumbing ------------------------------------------------

    def _environ(self) -> dict[str, str]:
        return dict(os.environ) if self._env is None else dict(self._env)

    def _op_read(self, reference: str) -> bytes:
        """``op read <reference>`` -> field bytes. Raises :class:`KeyUnavailable`."""
        env = self._environ()
        token = env.get(OP_SERVICE_ACCOUNT_TOKEN_ENV, "").strip()
        if not token:
            raise KeyUnavailable(
                f"{OP_SERVICE_ACCOUNT_TOKEN_ENV} is not set; this process has no "
                "1Password service-account credential and cannot read the "
                "receipt signing key"
            )
        binary = shutil.which(self.op_binary, path=env.get("PATH"))
        if binary is None:
            raise KeyUnavailable(
                f"the {self.op_binary!r} CLI is not on PATH; cannot read the "
                "receipt signing key"
            )
        try:
            proc = subprocess.run(  # fixed argv, never a shell
                [binary, "read", reference],
                capture_output=True,
                check=False,
                env=env,
                timeout=OP_READ_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise KeyUnavailable(f"could not run {self.op_binary!r}: {exc}") from exc
        if proc.returncode != 0:
            # stderr may name the vault/item but never the secret value.
            detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
            raise KeyUnavailable(
                f"{self.op_binary} read failed for {reference!r} "
                f"(exit {proc.returncode}): {detail[-1] if detail else 'no detail'}"
            )
        return proc.stdout.strip() + b"\n"

    # -- SigningKeyProvider -------------------------------------------------

    def public_key(self) -> bytes:
        """Raw 32-byte Ed25519 public key.

        Resolution order, cheapest first, so verification can run with NO
        1Password access at all: an inline configured OpenSSH line, then a
        configured ``op://`` public-key field, then derived from the private
        key (which needs the credential).
        """
        if self._public_key_raw is not None:
            return self._public_key_raw
        if self.public_key_openssh:
            raw = _load_public_key(self.public_key_openssh.encode("utf-8"))
        elif self.public_key_reference:
            raw = _load_public_key(self._op_read(self.public_key_reference))
        else:
            raw = _raw_public_bytes(_load_private_key(self._op_read(self.op_reference)))
        self._public_key_raw = raw
        return raw

    def key_id(self) -> str:
        return derive_key_id(self.public_key())

    def sign(self, payload: bytes) -> bytes:
        """Sign *payload*, holding the private key for this call only."""
        private_key = _load_private_key(self._op_read(self.op_reference))
        try:
            return bytes(private_key.sign(payload))
        finally:
            del private_key


# ---------------------------------------------------------------------------
# Payload derivation — one function, both sides
# ---------------------------------------------------------------------------


def lines_digest(lines: list[str]) -> str:
    """``sha256`` over the newline-terminated *lines* of a range."""
    h = hashlib.sha256()
    for line in lines:
        h.update(line.rstrip("\n").encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def seal_payload(partition_name: str, start_line: int, end_line: int, lines: list[str]) -> bytes:
    """The bytes a seal signs, and the ONE place they are derived.

    ``sha256`` over the partition's ledger-relative path (its file name —
    partitions are flat in one directory, so the name IS the relative path,
    and an absolute path would make a seal unverifiable after a move), the
    line range, and :func:`lines_digest` of the range's content. Signing a
    digest rather than the lines keeps the payload a fixed 32 bytes however
    long the range is.

    Both :meth:`SigningSealer.seal` and :meth:`SigningSealer.verify` call
    this — the same single-derivation discipline
    :func:`athenaeum.transcript_receipts.memory_digest` documents, and for
    the same reason: two copies would drift and every seal would silently
    fail to verify.
    """
    body = "\n".join(
        [partition_name, str(start_line), str(end_line), lines_digest(lines)]
    ).encode("utf-8")
    return hashlib.sha256(body).digest()


# ---------------------------------------------------------------------------
# Seal storage
# ---------------------------------------------------------------------------


def seals_dir(wiki_root: Path) -> Path:
    return ledger_dir(wiki_root) / SEALS_DIRNAME


def seals_partition_path(wiki_root: Path, month: str) -> Path:
    return seals_dir(wiki_root) / f"{month}.jsonl"


@dataclass(frozen=True)
class StoredSeal:
    """One persisted seal: a partition month, a line range, and the signature."""

    month: str
    start_line: int
    end_line: int
    key_id: str
    algorithm: str
    signature: str
    payload_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "month": self.month,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "key_id": self.key_id,
            "algorithm": self.algorithm,
            "signature": self.signature,
            "payload_digest": self.payload_digest,
        }

    @classmethod
    def from_dict(cls, record: dict[str, Any]) -> StoredSeal:
        return cls(
            month=str(record.get("month", "")),
            start_line=int(record.get("start_line", 0)),
            end_line=int(record.get("end_line", 0)),
            key_id=str(record.get("key_id", "")),
            algorithm=str(record.get("algorithm", "")),
            signature=str(record.get("signature", "")),
            payload_digest=str(record.get("payload_digest", "")),
        )

    def as_seal_record(self) -> SealRecord:
        return SealRecord(sealer=SIGNING_SEALER_NAME, detail=self.to_dict())


def _read_jsonl_tolerant(path: Path) -> list[dict[str, Any]]:
    """Every parseable JSON object in *path*; a torn trailing line is skipped.

    Same tolerance rule as every other append-only reader in this codebase
    (see :func:`athenaeum.store.append_line_durable`'s docstring for why a
    torn TRAILING line is the only corruption shape possible).
    """
    if not path.is_file():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def iter_stored_seals(wiki_root: Path, month: str | None = None) -> list[StoredSeal]:
    """Persisted seals, for one *month* or across every month."""
    d = seals_dir(wiki_root)
    if not d.is_dir():
        return []
    paths = [seals_partition_path(wiki_root, month)] if month else sorted(d.glob("*.jsonl"))
    out: list[StoredSeal] = []
    for path in paths:
        out.extend(StoredSeal.from_dict(r) for r in _read_jsonl_tolerant(path))
    return out


def append_stored_seal(wiki_root: Path, seal: StoredSeal) -> Path:
    path = seals_partition_path(wiki_root, seal.month)
    line = json.dumps(seal.to_dict(), separators=(",", ":")) + "\n"
    append_line_durable(path, line.encode("utf-8"))
    return path


# ---------------------------------------------------------------------------
# The sealer
# ---------------------------------------------------------------------------


@dataclass
class SigningSealer:
    """A :class:`~athenaeum.transcript_receipts.Sealer` that signs line ranges.

    ``seal`` signs :func:`seal_payload` over the range, persists a
    :class:`StoredSeal` next to the ledger, and returns the matching
    :class:`~athenaeum.transcript_receipts.SealRecord`. ``verify`` re-reads
    the range from the LIVE partition, recomputes the payload and checks the
    signature against the provider's public key — so altering any line in a
    sealed range, or its boundaries, fails verification.

    **Unavailable key is not an error.** Any :class:`KeyUnavailable` from the
    provider makes ``seal`` log ONE warning per instance and return ``None``
    for the rest of the run, so an unattended nightly with
    ``receipt_sealer: signing`` configured and no credential appends its
    receipt lines exactly as it would under ``NoopSealer``, with one line of
    explanation rather than one per receipt.
    """

    provider: SigningKeyProvider
    verify_public_key: bytes | None = None
    name: str = field(default=SIGNING_SEALER_NAME, init=False)
    _warned: bool = field(default=False, init=False, repr=False)

    # -- Sealer -------------------------------------------------------------

    def seal(
        self, partition_path: Path, start_line: int, end_line: int, lines: list[str]
    ) -> SealRecord | None:
        payload = seal_payload(partition_path.name, start_line, end_line, lines)
        try:
            signature = self.provider.sign(payload)
            key_id = self.provider.key_id()
        except KeyUnavailable as exc:
            if not self._warned:
                self._warned = True
                log.warning(
                    "receipt_sealers: signing key unavailable, appending receipts "
                    "UNSEALED (run `athenaeum receipts seal` where the key is "
                    "reachable): %s",
                    exc,
                )
            return None
        stored = StoredSeal(
            month=partition_path.stem,
            start_line=start_line,
            end_line=end_line,
            key_id=key_id,
            algorithm=ALGORITHM,
            signature=base64.b64encode(signature).decode("ascii"),
            payload_digest=hashlib.sha256(payload).hexdigest(),
        )
        append_stored_seal(_wiki_root_for(partition_path), stored)
        return stored.as_seal_record()

    def verify(
        self, partition_path: Path, start_line: int, end_line: int, seal: SealRecord | None
    ) -> bool:
        """``True`` only when *seal* is a signature over the range's LIVE content.

        An absent seal is ``False``, not vacuously true: an unsealed range is
        exactly what this sealer exists to distinguish from a sealed one.
        """
        if seal is None:
            return False
        detail = seal.detail if isinstance(seal.detail, dict) else {}
        encoded = detail.get("signature")
        if not isinstance(encoded, str) or detail.get("algorithm") != ALGORITHM:
            return False
        try:
            signature = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            return False
        lines = read_range(partition_path, start_line, end_line)
        if lines is None:
            return False
        payload = seal_payload(partition_path.name, start_line, end_line, lines)
        try:
            public_raw = (
                self.verify_public_key
                if self.verify_public_key is not None
                else self.provider.public_key()
            )
        except KeyUnavailable:
            # No public key reachable: report "not verified", never "verified".
            return False
        return verify_signature(public_raw, signature, payload)


def verify_signature(public_key_raw: bytes, signature: bytes, payload: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        Ed25519PublicKey.from_public_bytes(public_key_raw).verify(signature, payload)
    except (InvalidSignature, ValueError):
        return False
    return True


def _wiki_root_for(partition: Path) -> Path:
    """``<wiki>/_transcript_receipts/<month>.jsonl`` -> ``<wiki>``.

    The ``Sealer`` Protocol hands a sealer the PARTITION path and nothing
    else, so the seal store is derived from it rather than configured
    separately — one fewer knob that could point the two stores at
    different roots.
    """
    return partition.parent.parent


def read_range(partition: Path, start_line: int, end_line: int) -> list[str] | None:
    """Physical lines ``start_line..end_line`` (1-based, inclusive), or ``None``.

    ``None`` when the partition is missing or the range runs past its end —
    both of which mean "cannot verify", never "verified".
    """
    if start_line < 1 or end_line < start_line or not partition.is_file():
        return None
    try:
        text = partition.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = text.splitlines()
    if end_line > len(lines):
        return None
    return lines[start_line - 1 : end_line]


# ---------------------------------------------------------------------------
# Selection seam
# ---------------------------------------------------------------------------


def select_sealer(config: dict[str, Any] | None) -> Sealer:
    """The sealer this configuration asks for. DEFAULT: ``NoopSealer``.

    ``librarian.receipt_sealer`` picks the sealer and
    ``librarian.receipt_signing.*`` configures the signing one's key
    provider. An unknown value for either RAISES
    (:class:`athenaeum.config.ReceiptSealerConfigError`) — a typo in a
    security knob must never silently fall back to not sealing, which is the
    one failure mode an operator could not detect from the ledger.
    """
    name = resolve_receipt_sealer(config)
    if name == "noop":
        return DEFAULT_SEALER
    provider_name = resolve_receipt_signing_key_provider(config)
    # resolve_* already rejected every other value; this stays exhaustive so a
    # future provider cannot be added to config.py without a branch here.
    assert provider_name == "onepassword", provider_name
    provider = OnePasswordServiceAccountKeyProvider(
        resolve_receipt_signing_op_reference(config),
        public_key_reference=resolve_receipt_signing_public_key_ref(config),
        public_key_openssh=resolve_receipt_signing_public_key_openssh(config),
    )
    return SigningSealer(provider)


# ---------------------------------------------------------------------------
# Ledger-wide seal / verify (the CLI's two verbs)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RangeRef:
    """A partition month and an inclusive physical line range within it."""

    month: str
    start_line: int
    end_line: int

    def __str__(self) -> str:
        return f"{self.month} lines {self.start_line}-{self.end_line}"

    def to_dict(self) -> dict[str, Any]:
        return {"month": self.month, "start_line": self.start_line, "end_line": self.end_line}


@dataclass(frozen=True)
class SealAllReport:
    sealed: list[RangeRef] = field(default_factory=list)
    already_sealed: int = 0
    unavailable: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sealed": [r.to_dict() for r in self.sealed],
            "already_sealed": self.already_sealed,
            "unavailable": self.unavailable,
        }


@dataclass(frozen=True)
class VerifyReport:
    verified: list[RangeRef] = field(default_factory=list)
    failed: list[RangeRef] = field(default_factory=list)
    unsealed: list[RangeRef] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed and not self.unsealed

    def to_dict(self) -> dict[str, Any]:
        return {
            "verified": [r.to_dict() for r in self.verified],
            "failed": [r.to_dict() for r in self.failed],
            "unsealed": [r.to_dict() for r in self.unsealed],
            "ok": self.ok,
        }


def _months(wiki_root: Path) -> list[str]:
    d = ledger_dir(wiki_root)
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.jsonl"))


def _physical_line_count(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        return len(path.read_text(encoding="utf-8").splitlines())
    except OSError:
        return 0


def _sealed_line_numbers(seals: list[StoredSeal]) -> set[int]:
    covered: set[int] = set()
    for seal in seals:
        covered.update(range(seal.start_line, seal.end_line + 1))
    return covered


def unsealed_ranges(wiki_root: Path, month: str) -> list[RangeRef]:
    """Contiguous runs of *month*'s lines no stored seal covers."""
    total = _physical_line_count(partition_path(wiki_root, month))
    covered = _sealed_line_numbers(iter_stored_seals(wiki_root, month))
    out: list[RangeRef] = []
    start: int | None = None
    for line_no in range(1, total + 1):
        if line_no in covered:
            if start is not None:
                out.append(RangeRef(month, start, line_no - 1))
                start = None
            continue
        if start is None:
            start = line_no
    if start is not None:
        out.append(RangeRef(month, start, total))
    return out


def seal_all_unsealed(wiki_root: Path, sealer: SigningSealer) -> SealAllReport:
    """Seal every unsealed range in every partition. Needs a reachable key.

    No :class:`~athenaeum.runlock.RunLock` is required: this appends only to
    the seal store, never to the receipt ledger, so it does not participate
    in the ledger's single-appender contract. It signs ranges that are
    already on disk, so a concurrent intake appending NEW lines simply
    leaves them for the next run.
    """
    sealed: list[RangeRef] = []
    already = 0
    for month in _months(wiki_root):
        already += len(iter_stored_seals(wiki_root, month))
        partition = partition_path(wiki_root, month)
        for ref in unsealed_ranges(wiki_root, month):
            lines = read_range(partition, ref.start_line, ref.end_line)
            if lines is None:
                continue
            record = sealer.seal(partition, ref.start_line, ref.end_line, lines)
            if record is None:
                return SealAllReport(
                    sealed=sealed,
                    already_sealed=already,
                    unavailable=(
                        "signing key unavailable — nothing further was sealed; run "
                        "where the 1Password service-account token is set"
                    ),
                )
            sealed.append(ref)
    return SealAllReport(sealed=sealed, already_sealed=already)


def verify_all(wiki_root: Path, sealer: SigningSealer) -> VerifyReport:
    """Check every stored seal, and report every range that has none.

    Reports partitions and LINE RANGES only — never receipt content. Needs
    only the PUBLIC key, so it runs anywhere that is configured.
    """
    verified: list[RangeRef] = []
    failed: list[RangeRef] = []
    unsealed: list[RangeRef] = []
    for month in _months(wiki_root):
        partition = partition_path(wiki_root, month)
        for seal in iter_stored_seals(wiki_root, month):
            ref = RangeRef(month, seal.start_line, seal.end_line)
            if sealer.verify(partition, seal.start_line, seal.end_line, seal.as_seal_record()):
                verified.append(ref)
            else:
                failed.append(ref)
        unsealed.extend(unsealed_ranges(wiki_root, month))
    return VerifyReport(verified=verified, failed=failed, unsealed=unsealed)


def iter_months(wiki_root: Path) -> Iterator[str]:
    """Every receipt partition month present under *wiki_root*."""
    yield from _months(wiki_root)


__all__ = [
    "ALGORITHM",
    "OP_SERVICE_ACCOUNT_TOKEN_ENV",
    "SEALS_DIRNAME",
    "SIGNING_SEALER_NAME",
    "KeyUnavailable",
    "OnePasswordServiceAccountKeyProvider",
    "RangeRef",
    "SealAllReport",
    "SigningKeyProvider",
    "SigningSealer",
    "StoredSeal",
    "VerifyReport",
    "append_stored_seal",
    "derive_key_id",
    "iter_months",
    "iter_stored_seals",
    "lines_digest",
    "read_range",
    "seal_all_unsealed",
    "seal_payload",
    "seals_dir",
    "seals_partition_path",
    "select_sealer",
    "unsealed_ranges",
    "verify_all",
    "verify_signature",
]
