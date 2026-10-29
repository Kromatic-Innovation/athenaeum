# SPDX-License-Identifier: Apache-2.0
"""Real :class:`athenaeum.transcript_receipts.Sealer` implementations and the
selection seams around them (issues athenaeum#2039 and athenaeum#2040).

:mod:`athenaeum.transcript_receipts` defines the ``Sealer`` Protocol and ships
:class:`~athenaeum.transcript_receipts.NoopSealer` as its default, and says
explicitly that SELECTION lives outside that module. This module is that
outside: it holds the two real sealers and the two functions that turn config
into one (:func:`select_sealer`, :func:`select_anchorers`). Nothing here is
imported by ``transcript_receipts`` — the dependency runs one way only, which
is what let the second sealer (external timestamp anchoring,
:class:`AnchoringSealer`, issue athenaeum#2040) land here with
``transcript_receipts.py`` unchanged, exactly as its ``Sealer`` docstring
promised.

**Two sealers, two questions.** :class:`SigningSealer` answers "who sealed
this range" with a signature from a key the nightly cannot read;
:class:`AnchoringSealer` answers "by when did this content exist" with an
external timestamp attestation. They are independent — sign without
anchoring, anchor without signing, or both — which is why anchorer selection
is its OWN function rather than another branch inside ``select_sealer``.
Unlike the signing sealer, the anchoring one is never called from
``append_receipt``: one network round trip per receipt is not a cost intake
can pay, and the whole-partition digest it commits to would be invalidated by
the very next appended line (see :class:`AnchoringSealer`).

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
    LockNotHeld,
    Sealer,
    SealRecord,
    ledger_dir,
    partition_path,
)

if TYPE_CHECKING:  # pragma: no cover - annotation-only
    from collections.abc import Callable, Iterator

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


#: DER encoding of the RFC 8410 Ed25519 algorithm OID, ``1.3.101.112``, as it
#: appears inside a ``OneAsymmetricKey``'s ``privateKeyAlgorithm`` SEQUENCE.
_ED25519_OID_DER = b"\x06\x03\x2b\x65\x70"


def _read_der_tlv(data: bytes, offset: int) -> tuple[int, bytes, int]:
    """Read one DER tag-length-value at *offset*; return ``(tag, value, next)``.

    A deliberately minimal reader: definite-length only, which is all DER
    permits, and used on a structure that is 83 bytes long. It exists because
    :func:`_ed25519_seed_from_one_asymmetric_key` has to look at the ASN.1
    that ``cryptography`` refuses to parse, and ``cryptography`` exposes no
    ASN.1 API to borrow.
    """
    if offset + 2 > len(data):
        raise ValueError("truncated DER")
    tag = data[offset]
    first = data[offset + 1]
    cursor = offset + 2
    if first & 0x80:
        width = first & 0x7F
        if width == 0 or width > 4 or cursor + width > len(data):
            raise ValueError("unsupported DER length")
        length = int.from_bytes(data[cursor : cursor + width], "big")
        cursor += width
    else:
        length = first
    if cursor + length > len(data):
        raise ValueError("truncated DER value")
    return tag, data[cursor : cursor + length], cursor + length


def _ed25519_seed_from_one_asymmetric_key(material: bytes) -> bytes | None:
    """The 32-byte Ed25519 seed inside a v2 PKCS8 PEM, or ``None``.

    ``op read`` on the bare ``private key`` field of a 1Password **SSH Key**
    item returns the RFC 5958 **v2** ``OneAsymmetricKey`` encoding — the
    private key PLUS the optional ``publicKey`` attribute, 168 bytes of PEM
    over 83 bytes of DER — and ``cryptography`` rejects it outright with
    ``ASN.1 parsing error: extra data`` (issue athenaeum#2142). Its own
    ``private_bytes(PKCS8)`` only ever emits v1, so nothing in the library
    round-trips the shape that 1Password actually serves.

    Returns ``None`` — rather than raising — for anything that is not a v2
    Ed25519 ``OneAsymmetricKey``, so the caller can re-raise
    ``cryptography``'s own, more informative error for a key that is simply
    malformed. A real configuration fault must still propagate.
    """
    try:
        der = base64.b64decode(
            b"".join(
                line.strip()
                for line in material.splitlines()
                if line.strip() and not line.strip().startswith(b"-----")
            ),
            validate=True,
        )
    except (ValueError, TypeError):
        return None
    try:
        tag, body, _ = _read_der_tlv(der, 0)
        if tag != 0x30:  # SEQUENCE
            return None
        tag, version, cursor = _read_der_tlv(body, 0)
        if tag != 0x02 or version != b"\x01":  # INTEGER, v2
            return None
        tag, algorithm, cursor = _read_der_tlv(body, cursor)
        if tag != 0x30 or _ED25519_OID_DER not in algorithm:
            return None
        tag, private_key, _ = _read_der_tlv(body, cursor)
        if tag != 0x04:  # OCTET STRING
            return None
        tag, seed, _ = _read_der_tlv(private_key, 0)
    except ValueError:
        return None
    # RFC 8410: the privateKey OCTET STRING wraps a CurvePrivateKey, itself an
    # OCTET STRING of exactly the 32 seed bytes.
    if tag != 0x04 or len(seed) != 32:
        return None
    return seed


def _load_private_key(material: bytes) -> Any:
    """Parse *material* as an Ed25519 private key, OpenSSH or PKCS8 PEM.

    Three encodings, because which one a 1Password **SSH Key** item hands
    back depends on the ``op://`` reference (issue athenaeum#2142):

    * an ``OPENSSH PRIVATE KEY`` PEM block — what ``op read`` returns for a
      reference carrying ``?ssh-format=openssh``. Tried first.
    * a plain ``PRIVATE KEY`` PEM block holding the RFC 5958 **v1**
      ``PrivateKeyInfo`` — a plain PKCS8 item, and what ``cryptography``'s own
      ``private_bytes(PKCS8)`` emits.
    * the same PEM header holding the RFC 5958 **v2** ``OneAsymmetricKey``,
      which is what ``op read`` returns for the bare ``private key`` field.
      ``cryptography`` rejects that one, so the seed is read out of the DER by
      :func:`_ed25519_seed_from_one_asymmetric_key` and reloaded. Only reached
      after the v1 parse has failed, so it costs a correct key nothing.

    All three are accepted rather than one being mandated by configuration:
    the shape depends on vendor query syntax the operator may legitimately
    change on either side of a rotation, and a reference that merely LOOKS
    right producing a parse error at first ``seal`` is the failure mode this
    exists to remove.
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
        try:
            key = load_pem_private_key(material, password=None)
        except ValueError:
            seed = _ed25519_seed_from_one_asymmetric_key(material)
            if seed is None:
                raise
            key = Ed25519PrivateKey.from_private_bytes(seed)
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

    Accepts a 1Password **SSH Key** item, whose ``public key`` field arrives
    as an ``ssh-ed25519 AAAA...`` line and whose ``private key`` field
    arrives as PKCS8 PEM — in the RFC 5958 *v2* ``OneAsymmetricKey`` form,
    not OpenSSH, unless the reference carries ``?ssh-format=openssh``
    (issue athenaeum#2142). Also accepts a plain PKCS8 PEM item. One parse
    attempt per format, no configuration knob (see
    :func:`_load_private_key`).
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
        """Sign *payload*, holding the private key for this call only.

        Refuses outright when an explicitly configured public key does not
        belong to the private key just read (Seer review on PR
        athenaeum#2137). Without the check a mismatched pair signs happily
        and records a ``key_id`` for a key that did not sign anything —
        every seal then fails verification for a reason the output cannot
        explain. The whole claim that a provider "cannot mislabel the key it
        actually holds" rests on this comparison.
        """
        private_key = _load_private_key(self._op_read(self.op_reference))
        try:
            if self.public_key_openssh or self.public_key_reference:
                derived = _raw_public_bytes(private_key)
                if derived != self.public_key():
                    raise ValueError(
                        "receipt signing config is inconsistent: the configured "
                        "public key does not belong to the private key at "
                        f"{self.op_reference!r}. Point both at the same item."
                    )
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
    #: Set once the provider has failed. Both halves matter: it suppresses
    #: the repeat warnings, AND it stops re-running ``op read`` once per
    #: receipt for a failure that cannot change within a run (Seer review on
    #: PR athenaeum#2137 — a readable-but-malformed key is deterministic, so
    #: retrying it is pure subprocess cost).
    _failure: str | None = field(default=None, init=False, repr=False)

    @property
    def last_error(self) -> str | None:
        """Why this sealer stopped sealing, or ``None`` if it has not.

        Lets a SUPERVISED ``athenaeum receipts seal`` report the actual
        reason instead of a generic "unavailable" — the nightly path only
        ever needs the one log line.
        """
        return self._failure

    def _give_up(self, reason: str, remedy: str) -> SealRecord | None:
        """Record *reason*, warn ONCE, and seal nothing for the rest of the run."""
        self._failure = reason
        log.warning(
            "receipt_sealers: %s — appending receipts UNSEALED (%s)", reason, remedy
        )
        return None

    # -- Sealer -------------------------------------------------------------

    def seal(
        self, partition_path: Path, start_line: int, end_line: int, lines: list[str]
    ) -> SealRecord | None:
        if self._failure is not None:
            return None
        payload = seal_payload(partition_path.name, start_line, end_line, lines)
        try:
            signature = self.provider.sign(payload)
            key_id = self.provider.key_id()
        except KeyUnavailable as exc:
            return self._give_up(
                f"signing key unavailable ({exc})",
                "run `athenaeum receipts seal` where the key is reachable",
            )
        except Exception as exc:  # noqa: BLE001 — a sealer must never break intake.
            # A readable-but-unusable key (wrong algorithm, malformed field,
            # mismatched pair) is a real configuration fault, but it is still
            # not intake's problem to raise on: the ledger line is appended
            # either way, and `receipts verify` will report the whole range
            # unsealed. What this must NOT do is retry per receipt.
            return self._give_up(
                f"signing key unusable ({type(exc).__name__}: {exc})",
                "fix librarian.receipt_signing, then run `athenaeum receipts seal`",
            )
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
                        f"{sealer.last_error or 'signing key unavailable'} — "
                        "nothing further was sealed"
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


# ---------------------------------------------------------------------------
# Anchoring (issue athenaeum#2040) — the second real Sealer, its selection
# seam, and its own record store
# ---------------------------------------------------------------------------

#: :attr:`athenaeum.transcript_receipts.SealRecord.sealer` for every record
#: :class:`AnchoringSealer` produces. Distinct from
#: :data:`SIGNING_SEALER_NAME` because the two sealers answer different
#: questions ("who sealed this" vs "by when did this exist") and a ledger
#: can carry both.
ANCHOR_SEALER_NAME = "anchor"

#: Anchor records live in their OWN subdirectory of ``_seals/``, for the
#: same glob-collision reason ``_seals/`` is itself a subdirectory of the
#: ledger (see this module's docstring): ``iter_stored_seals`` globs
#: ``_seals/*.jsonl``, so a sibling ``_seals/2026-10.anchors.jsonl`` would
#: be parsed as a signing seal.
ANCHORS_DIRNAME = "anchors"


def anchors_dir(wiki_root: Path) -> Path:
    return seals_dir(wiki_root) / ANCHORS_DIRNAME


def anchors_partition_path(wiki_root: Path, month: str) -> Path:
    return anchors_dir(wiki_root) / f"{month}.jsonl"


def require_lock(lock: Any) -> None:
    """Refuse a ``wiki/`` write without an ALREADY-ACQUIRED lock.

    Mirrors :mod:`athenaeum.transcript_receipts`'s own ``_require_lock``
    and reuses its :class:`~athenaeum.transcript_receipts.LockNotHeld` —
    anchoring writes into the SAME knowledge repo the receipt ledger lives
    in, so it reuses that lock rather than inventing a second one
    (issue athenaeum#2040: "every ``wiki/`` write happens under an
    acquired ``RunLock``").
    """
    if not getattr(lock, "acquired", False):
        raise LockNotHeld(
            "receipt_sealers: anchoring write attempted without an acquired "
            "RunLock — the nightly anchoring path reuses "
            "athenaeum.runlock.RunLock rather than inventing a second lock "
            "(issue athenaeum#2040)."
        )


@dataclass(frozen=True)
class StoredAnchor:
    """One persisted anchor record: a partition month, the line range it
    covers, the subject/digest that was anchored, and every
    :class:`~promptwitness.anchor.Attestation` obtained for it.

    ``attestations`` is a LIST because one anchoring pass runs several
    adapters over the same digest (``local-clock`` always, plus whichever
    network adapters are configured), and all of them attest to the same
    subject. ``op`` records how the record was produced: ``"anchor"`` for a
    stamp this pass obtained, ``"import"`` for one lifted from an
    attestation the in-session/SessionEnd path (issue athenaeum#2156) had
    already obtained for the SAME digest.
    """

    month: str
    start_line: int
    end_line: int
    subject: str
    digest: str
    op: str
    attestations: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "month": self.month,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "subject": self.subject,
            "digest": self.digest,
            "op": self.op,
            "attestations": [dict(att) for att in self.attestations],
        }

    @classmethod
    def from_dict(cls, record: dict[str, Any]) -> StoredAnchor:
        raw_atts = record.get("attestations")
        return cls(
            month=str(record.get("month", "")),
            start_line=int(record.get("start_line", 0)),
            end_line=int(record.get("end_line", 0)),
            subject=str(record.get("subject", "")),
            digest=str(record.get("digest", "")),
            op=str(record.get("op", "")),
            attestations=[dict(a) for a in raw_atts if isinstance(a, dict)]
            if isinstance(raw_atts, list)
            else [],
        )

    def as_seal_record(self) -> SealRecord:
        return SealRecord(sealer=ANCHOR_SEALER_NAME, detail=self.to_dict())


def iter_stored_anchors(wiki_root: Path, month: str | None = None) -> list[StoredAnchor]:
    """Persisted anchor records, for one *month* or across every month."""
    d = anchors_dir(wiki_root)
    if not d.is_dir():
        return []
    paths = [anchors_partition_path(wiki_root, month)] if month else sorted(d.glob("*.jsonl"))
    out: list[StoredAnchor] = []
    for path in paths:
        out.extend(StoredAnchor.from_dict(r) for r in _read_jsonl_tolerant(path))
    return out


def append_stored_anchor(wiki_root: Path, record: StoredAnchor, *, lock: Any) -> Path:
    """Append *record* under ``_seals/anchors/``. Requires an acquired *lock*."""
    require_lock(lock)
    path = anchors_partition_path(wiki_root, record.month)
    line = json.dumps(record.to_dict(), separators=(",", ":")) + "\n"
    append_line_durable(path, line.encode("utf-8"))
    return path


def receipts_subject(month: str, line_count: int) -> str:
    """``receipts:<YYYY-MM>:<line_count>`` — the anchor subject for one
    receipt partition, byte-identical to the one
    :func:`athenaeum.witness_anchor.run_session_end_anchor` builds, so an
    attestation either path obtained is findable by the other.
    """
    return f"receipts:{month}:{line_count}"


def partition_digest(lines: list[str]) -> str:
    """``"sha256:<hex>"`` over *lines* — the digest an anchor commits to.

    :func:`lines_digest` returns the bare hex; every anchoring caller needs
    the ``sha256:``-prefixed form :func:`promptwitness.anchor.digest_bytes`
    parses, so the prefix is applied in exactly one place rather than at
    each call site (the same single-derivation rule
    :func:`seal_payload` documents).
    """
    return f"sha256:{lines_digest(lines)}"


def select_anchorers(config: dict[str, Any] | None, ledger_dir: Path | None = None) -> list[Any]:
    """The :class:`~promptwitness.anchor.Anchorer` list this configuration
    asks for, in the order they should be submitted to.

    Selection is a SEPARATE function from :func:`select_sealer`, which is
    unchanged: a sealer and an anchorer set are independent choices (an
    operator can sign without anchoring, anchor without signing, or do
    both), so folding anchoring into ``select_sealer``'s single return
    value would make the two mutually exclusive.

    ``librarian.receipt_anchor.adapters`` names them
    (:func:`athenaeum.config.resolve_receipt_anchor_adapters`, which
    already guarantees ``"local-clock"`` is present — the mandatory
    fallback that always runs and never fails). ``rfc3161`` is dropped
    silently when no TSA URL is configured, matching
    ``promptwitness.anchor_rfc3161``'s own "no TSA is free to nominate on
    an operator's behalf" stance; every other unknown value has already
    raised in the resolver.

    *ledger_dir* is where the network adapters persist their proof files
    (``<ledger_dir>/anchors/<subject-safe>.ots``); it defaults to the
    witness ledger directory, so ``select_anchorers(config)`` is the whole
    call for the ordinary nightly path.
    """
    from athenaeum.config import (
        resolve_receipt_anchor_adapters,
        resolve_receipt_anchor_ots_block_source,
        resolve_receipt_anchor_ots_calendars,
        resolve_receipt_anchor_rfc3161_url,
    )
    from athenaeum.witness_attestation import resolve_default_witness_dir
    from promptwitness import anchor as anchor_mod
    from promptwitness import anchor_rfc3161

    names = resolve_receipt_anchor_adapters(config)
    proof_dir = ledger_dir if ledger_dir is not None else resolve_default_witness_dir()
    rfc3161_url = resolve_receipt_anchor_rfc3161_url(config)

    anchorers: list[Any] = []
    for name in names:
        if name == "local-clock":
            anchorers.append(anchor_mod.LocalClockAnchorer())
        elif name == "opentimestamps":
            anchorers.append(
                anchor_mod.OpenTimestampsAnchorer(
                    proof_dir,
                    calendars=resolve_receipt_anchor_ots_calendars(config),
                    block_source=resolve_receipt_anchor_ots_block_source(config),
                )
            )
        elif name == "rfc3161" and rfc3161_url:
            anchorers.append(
                anchor_rfc3161.Rfc3161Anchorer(
                    proof_dir,
                    tsa_url=rfc3161_url,
                    ca_bundle=anchor_rfc3161.resolve_rfc3161_ca_bundle(None),
                )
            )
    return anchorers


@dataclass
class AnchoringSealer:
    """A :class:`~athenaeum.transcript_receipts.Sealer` that ANCHORS a
    receipt partition's content digest instead of signing it.

    ``seal(partition, 1, N, lines)`` anchors :func:`partition_digest` over
    the whole partition with every configured anchorer and returns the
    matching :class:`~athenaeum.transcript_receipts.SealRecord`
    (``sealer="anchor"``, ``detail`` carrying every attestation);
    ``verify`` re-reads the range from the LIVE partition, recomputes the
    digest and re-runs each anchorer's own ``verify``, recording its
    ``verify_source`` on :attr:`last_verify_results`.

    **Never called from ``append_receipt``.** One network round trip per
    receipt is not a cost intake can pay, and the subject this sealer
    anchors (``receipts:<month>:<line_count>``) commits to the WHOLE
    partition anyway, so a per-range call would stamp a digest that the
    very next appended line invalidates. The callers are the nightly
    anchoring path and ``athenaeum receipts anchor``
    (:mod:`athenaeum.witness_anchor`), both of which hold the run lock.
    ``athenaeum.transcript_receipts`` is therefore unchanged by this
    issue, exactly as its ``Sealer`` docstring promised.

    **The lock is a constructor field, not a ``seal`` argument.** The
    ``Sealer`` Protocol's signature has no lock parameter — changing it
    would mean editing ``transcript_receipts`` — so the lock this sealer's
    ``wiki/`` writes require is held here and checked on every write
    (:func:`require_lock`, raising
    :class:`~athenaeum.transcript_receipts.LockNotHeld`).

    **Network failure is not an error.** Every anchorer promptwitness
    ships degrades to a ``status: "failed"`` attestation rather than
    raising, and ``local-clock`` cannot fail at all, so ``seal`` always
    returns a record: an offline nightly records the local-clock stamp and
    the failed OpenTimestamps attempt, and the next pass retries because a
    ``pending``/``failed`` record is not a complete one.
    """

    anchorers: list[Any]
    lock: Any
    wiki_root: Path
    timeout: float = 10.0
    #: Optional sink for each attestation :meth:`seal` obtains, called as
    #: ``record_attestation(subject, digest, attestation)``. The nightly
    #: path passes the witness-ledger appender here, so one anchoring pass
    #: lands in BOTH stores: the ``anchor`` line in the local witness
    #: ledger (which is what the off-machine mirror then carries) and the
    #: anchor record under ``_seals/anchors/`` (which is what makes a
    #: re-run a no-op). A callback rather than a ``ledger_dir`` field
    #: because appending a witness line is L4 work
    #: (:mod:`athenaeum.witness_anchor`) and this module is L2 — the
    #: dependency may only run the other way.
    record_attestation: Callable[[str, str, Any], None] | None = None
    name: str = field(default=ANCHOR_SEALER_NAME, init=False)
    #: Every :class:`~promptwitness.anchor.VerifyResult` the last
    #: :meth:`verify` call produced, as dicts — each one carrying its own
    #: ``verify_source``, which is the whole point of recording them: a
    #: bare ``True``/``False`` would discard *how* the check was made.
    last_verify_results: list[dict[str, Any]] = field(default_factory=list, init=False)

    # -- Sealer -------------------------------------------------------------

    def seal(
        self, partition_path: Path, start_line: int, end_line: int, lines: list[str]
    ) -> SealRecord | None:
        return self.seal_with_op(partition_path, start_line, end_line, lines, op="anchor")

    def seal_with_op(
        self,
        partition_path: Path,
        start_line: int,
        end_line: int,
        lines: list[str],
        *,
        op: str,
        attestations: list[dict[str, Any]] | None = None,
    ) -> SealRecord | None:
        """:meth:`seal`, plus the IMPORT path.

        With *attestations* given, no anchorer is called at all AND
        :attr:`record_attestation` is not invoked: the record is written
        from attestations some other pass already obtained for this exact
        digest, whose ``anchor`` lines therefore already exist (issue
        athenaeum#2040 — "an athenaeum#2156 attestation for the same
        digest is imported, not re-stamped"). A re-stamp would spend a
        calendar round trip to learn something already known, and would
        record a LATER claimed time for a digest that demonstrably existed
        earlier, which is strictly worse evidence.
        """
        require_lock(self.lock)
        digest = partition_digest(lines)
        subject = receipts_subject(partition_path.stem, end_line)
        if attestations is None:
            from promptwitness import anchor as anchor_mod

            digest_raw = anchor_mod.digest_bytes(digest)
            obtained = []
            for anchorer in self.anchorers:
                att = anchorer.anchor(digest_raw, subject, timeout=self.timeout)
                if self.record_attestation is not None:
                    self.record_attestation(subject, digest, att)
                obtained.append(att.to_dict())
        else:
            obtained = [dict(att) for att in attestations]
        record = StoredAnchor(
            month=partition_path.stem,
            start_line=start_line,
            end_line=end_line,
            subject=subject,
            digest=digest,
            op=op,
            attestations=obtained,
        )
        append_stored_anchor(self.wiki_root, record, lock=self.lock)
        return record.as_seal_record()

    def verify(
        self, partition_path: Path, start_line: int, end_line: int, seal: SealRecord | None
    ) -> bool:
        """``True`` only when *seal* anchors the range's LIVE content.

        An absent seal is ``False``, not vacuously true — the same stance
        :meth:`SigningSealer.verify` takes. Every anchorer whose source
        appears in the record re-runs its own ``verify``, and each
        :class:`~promptwitness.anchor.VerifyResult` (``verify_source``
        included) is recorded on :attr:`last_verify_results`.

        A ``pending`` OpenTimestamps attestation does NOT fail
        verification: pending is the normal state for the hours between a
        calendar submission and its Bitcoin confirmation, and the record
        is upgraded by the next nightly pass. What fails is a changed
        range, a missing range, a record with no attestations at all, or
        an attestation that claimed ``complete`` and no longer verifies.
        """
        self.last_verify_results = []
        if seal is None:
            return False
        detail = seal.detail if isinstance(seal.detail, dict) else {}
        raw_atts = detail.get("attestations")
        if not isinstance(raw_atts, list) or not raw_atts:
            return False
        lines = read_range(partition_path, start_line, end_line)
        if lines is None:
            return False
        if partition_digest(lines) != detail.get("digest"):
            return False

        from promptwitness import anchor as anchor_mod

        digest_raw = anchor_mod.digest_bytes(partition_digest(lines))
        by_source = {getattr(a, "source", ""): a for a in self.anchorers}
        ok = False
        for raw in raw_atts:
            if not isinstance(raw, dict):
                return False
            att = anchor_mod.Attestation.from_dict(raw)
            anchorer = by_source.get(att.source)
            if anchorer is None:
                # No configured adapter can speak for this source any more
                # (the operator dropped it from `adapters`). Recorded as
                # unverified rather than silently ignored or counted as a
                # pass — "cannot verify" is never "verified".
                self.last_verify_results.append(
                    {
                        "status": "failed",
                        "verify_source": "unconfigured",
                        "detail": {"source": att.source},
                    }
                )
                continue
            result = anchorer.verify(att, digest_raw)
            self.last_verify_results.append(result.to_dict())
            if att.status == "complete" and result.status not in (
                "complete",
                "complete-unverified-block",
            ):
                return False
            if result.status in ("complete", "complete-unverified-block"):
                ok = True
        return ok


__all__ = [
    "ALGORITHM",
    "ANCHORS_DIRNAME",
    "ANCHOR_SEALER_NAME",
    "OP_SERVICE_ACCOUNT_TOKEN_ENV",
    "SEALS_DIRNAME",
    "SIGNING_SEALER_NAME",
    "AnchoringSealer",
    "KeyUnavailable",
    "OnePasswordServiceAccountKeyProvider",
    "RangeRef",
    "SealAllReport",
    "SigningKeyProvider",
    "SigningSealer",
    "StoredAnchor",
    "StoredSeal",
    "VerifyReport",
    "anchors_dir",
    "anchors_partition_path",
    "append_stored_anchor",
    "append_stored_seal",
    "derive_key_id",
    "iter_months",
    "iter_stored_anchors",
    "iter_stored_seals",
    "lines_digest",
    "partition_digest",
    "read_range",
    "receipts_subject",
    "require_lock",
    "seal_all_unsealed",
    "seal_payload",
    "seals_dir",
    "seals_partition_path",
    "select_anchorers",
    "select_sealer",
    "unsealed_ranges",
    "verify_all",
    "verify_signature",
]
