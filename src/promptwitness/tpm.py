# SPDX-License-Identifier: Apache-2.0
"""``TpmKeyProvider``: a non-exportable TPM key adapter for promptwitness
(athenaeum#2154) -- Linux through ``tpm2-tools``, Windows through the CNG
"Microsoft Platform Crypto Provider" via PowerShell.

**Module placement note.** The issue body named this module
``src/promptwitness/keys/tpm.py`` -- a ``keys/`` package. No such package
exists in this repo, and both prior issues that named that shape
(athenaeum#2152, athenaeum#2153) shipped flat instead. This module is
therefore ``tpm.py``, a sibling of ``signing.py`` and
:mod:`promptwitness.secure_enclave`, not a subpackage -- see that module's
own "Module placement note" for the precedent (``anchor_rfc3161.py`` next
to ``anchor.py``).

Layering: L1.5, same as ``signing.py`` / :mod:`promptwitness.secure_enclave`
-- depends on :mod:`promptwitness.signing` and :mod:`promptwitness.ledger`
(one way only), stdlib otherwise. No ``athenaeum`` import anywhere
(enforced by ``tests/test_promptwitness_boundary.py``'s AST scan) and no
``cryptography`` import at module level -- every function that needs it
imports it locally.

**Structure: one provider, two backends.** Unlike
:mod:`promptwitness.secure_enclave` (one platform, one adapter class),
this tier has two platforms, so it mirrors ``signing.py``'s own
``OsKeystoreKeyProvider`` + ``KeystoreBackend`` protocol split instead:
:class:`TpmBackend` is a dumb-ish seam that knows nothing about the
strength vocabulary or ``key_id`` -- just "make a key" / "give me the
public key" / "sign this" -- and :class:`TpmKeyProvider` is the ONE class
holding the common logic (``key_id``, ``algorithm``, ``strength``,
``presence_required``, the single process-wide warning, and the X9.63
enforcement below). That is what lets :class:`Tpm2ToolsBackend` (Linux)
and :class:`WindowsPlatformCryptoBackend` (Windows) share one provider and
one set of signing-behaviour tests, exactly as macOS Keychain / Secret
Service / Windows DPAPI share ``OsKeystoreKeyProvider``.

**What this tier protects, and what it does not.** A TPM-backed key is
non-exportable in hardware: the private part never leaves the TPM in
either backend -- only a signature or the public key ever crosses the
process boundary -- so a signature proves the line was signed ON THIS
PHYSICAL DEVICE, the same claim :mod:`promptwitness.secure_enclave` makes
for macOS. Operator ruling 1 (``docs/design/witness-ledger.md``) declined
per-signature user presence, so this adapter proves nothing about a human
being present either: :meth:`TpmKeyProvider.presence_required` is
``False``, exactly like every other adapter in this package. Only
``hardware-presence`` (a seam only, no adapter shipped for it) would
change that.

**The encoding agreement (load-bearing).** Both backends normalize to the
SAME wire contract :mod:`promptwitness.secure_enclave` uses, so
:func:`signing.verify_signature` and ``ledger._verify_line_signature``
(both already dispatch ``"ecdsa-p256-sha256"``, landed with
athenaeum#2153) verify a TPM line with no change on either side:

- **Public key: the 65-byte X9.63 uncompressed point** (``0x04 || X ||
  Y``). ``Tpm2ToolsBackend`` gets a PEM from ``tpm2_readpublic -f pem``
  and converts it with :func:`_pem_public_key_to_x963` (ordinary
  ``cryptography`` PEM parsing -- not a guess).
  ``WindowsPlatformCryptoBackend``'s own PowerShell script (fully owned by
  this module, see :data:`_POWERSHELL_SCRIPT`) assembles the point
  directly from ``ECDsaCng.ExportParameters(false)``'s ``Q.X``/``Q.Y`` and
  emits it base64-encoded, already tagged. Either way,
  :meth:`TpmKeyProvider._public_key_bytes` is the single enforcement
  point -- it asserts 65 bytes and the ``0x04`` tag on EVERY read,
  regardless of which backend produced the bytes, mirroring
  :meth:`promptwitness.secure_enclave.SecureEnclaveKeyProvider._public_key_bytes`
  exactly (including its executable negative control): a backend that
  regressed to an untagged 64-byte point must be refused, not silently
  accepted into a wrong-but-stable ``key_id``.
- **Signature: DER.** Windows' natural output is raw ``r || s`` and the
  Linux tool's is undocumented (see ASSUMPTION 1 below), so both go
  through the SAME :func:`_normalize_signature_to_der` helper, which
  accepts either shape -- the one piece of real,
  shared conversion logic this module adds beyond
  :mod:`promptwitness.secure_enclave`'s precedent, because that module's
  Swift helper already emitted DER directly and never needed one.

**ASSUMPTIONS that could not be verified without real hardware (tracked
for athenaeum#2240's real-hardware signature/non-exportability checks --
read every item below before trusting this adapter against a real TPM):**

1. **``tpm2_sign -f plain``'s exact byte layout for an ECDSA signature --
   NEUTRALIZED, not merely documented.** ``tpm2-tools``' own docs
   (``man/common/signature.md``) say only that ``plain`` "output[s] the
   plain signature data as defined by the used cryptographic algorithm";
   they do not spell out byte order or padding, and a build lane has no
   TPM to settle it with. Rather than bet on the likeliest reading (the
   IEEE P1363 raw ``r || s`` pair, zero-padded to 32 bytes each -- the
   natural reading of the TPM 2.0 spec's ``TPMS_SIGNATURE_ECDSA``
   structure, which is exactly ``{hash, signatureR, signatureS}``),
   :func:`_normalize_signature_to_der` ACCEPTS EITHER that or an
   already-DER signature, which are trivially distinguishable (64 bytes
   exactly, versus a length-checked ``0x30`` SEQUENCE). So this item is no
   longer a correctness risk on either reading; what remains for
   athenaeum#2240 is only the residual possibility of a THIRD encoding
   nobody here anticipated, which would fail loudly on the first real
   signature rather than verify incorrectly.
2. **Exit code 1 as the "no key at this handle yet" signal for
   ``tpm2_readpublic``.** Verified directly from ``tpm2-tools``' own
   ``man/common/returns.md``: exit codes are ``0`` success, ``1`` "general
   non-specific error", ``2`` options-handling, ``3`` authentication,
   ``4`` TCTI, ``5`` unsupported scheme. There is no CODE dedicated to
   "handle not found" the way ``secure_enclave``'s helper has
   ``EXIT_NO_KEY=2`` or ``MacOsKeychainBackend``'s ``errSecItemNotFound``
   has ``44``. Given a bare ``tpm2_readpublic -c <handle>`` call, codes
   2/3/4/5 are already claimed by other failure categories, so code ``1``
   is INFERRED (not confirmed) to be dominated in practice by "no object
   at this handle" -- :data:`_LINUX_READPUBLIC_ABSENT_RETURNCODES` encodes
   this inference. The risk is bounded even if it is wrong: a
   misclassified "cannot tell" that this module treats as "absent" only
   leads :func:`generate_tpm_key` to attempt ``create()`` again, and the
   persistent handle TPM itself enforces -- ``tpm2_evictcontrol`` cannot
   silently clobber an object already resident at a handle -- so the
   worst case is a loud failed ``create()``, never silent data loss.

   **The inference is scoped to ``tpm2_readpublic`` ONLY, and the code
   enforces that scope.** :meth:`Tpm2ToolsBackend.sign` passes no
   ``absent_returncodes`` at all, so a ``tpm2_sign`` failure is always a
   plain :class:`ledger.KeyUnavailable`. Reusing the inference there
   would have bought nothing (no decision needs the distinction on a
   signing call) and cost real accuracy: a transient session/policy/PCR
   failure would reach the ledger line as
   ``key.error: "KeystoreItemAbsent"`` -- "you never created a key" --
   about a key that is perfectly fine. In an audit trail a confident
   wrong diagnosis is worse than a vague true one.
3. **``tpm2_create``'s ``-c`` direct-load.** ``tpm2-tools`` docs say ``-c``
   lets ``tpm2_create`` hand back an already-loaded context "without
   requiring a separate ``tpm2_load`` step, though TPM support for this
   feature varies." This module relies on it (one fewer subprocess call);
   a TPM that does not support it would surface as a loud
   ``ledger.KeyUnavailable`` from :meth:`Tpm2ToolsBackend.create`, not a
   silent partial key.
4. **``ECDsaCng.ExportParameters``/``SignHash`` against the real
   "Microsoft Platform Crypto Provider" KSP specifically** (as opposed to
   the software KSP, which is well-exercised). Some TPM-backed CNG
   providers are pickier than the managed ``ECDsaCng`` wrapper expects and
   need the raw ``CngKeyBlobFormat.EccPublicBlob`` export instead; this
   module uses the higher-level wrapper for a simpler, more auditable
   script and flags the lower-level fallback as a known follow-up.
5. **``ECParameters.Q.X``/``Q.Y`` are exactly 32 bytes each.**
   :data:`_POWERSHELL_SCRIPT`'s ``Write-PointBase64`` copies a fixed 32
   bytes from each coordinate into the X9.63 point. .NET fixes coordinate
   length for a named curve, so this holds by convention for P-256 -- but
   it is a convention, not something this repo can test, and a shorter
   array would make ``[System.Array]::Copy`` throw (caught, exit 1, loud)
   rather than silently emit a malformed point. Listed for completeness:
   lower risk than items 1-4, and the 65-byte/``0x04`` enforcement in
   :meth:`TpmKeyProvider._public_key_bytes` is the backstop either way.
6. **``tpm2_sign``'s message argument is a FILE, not stdin.** Unlike every
   other backend in this package, ``tpm2-tools``' own docs do not document
   stdin support (``-``) for ``tpm2_sign``'s positional message argument --
   only file paths are demonstrated. Guessing stdin support and being
   wrong would hang or crash the signing path outright, which is worse
   than the alternative: :meth:`Tpm2ToolsBackend.sign` writes the payload
   to a mode-0600 file in a private per-call temp directory (torn down in
   the same call) and passes the FILE PATH on argv -- a path is not
   secret, so this does not reopen the "secret on argv" problem the
   stdin-only rule exists to prevent; only the payload BYTES never
   appearing on argv is the actual invariant, and they do not.
"""

from __future__ import annotations

import base64
import contextlib
import os
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol, runtime_checkable

from promptwitness import ledger, signing

__all__ = [
    "ALGORITHM",
    "STRENGTH",
    "TPM_BACKENDS",
    "Tpm2ToolsBackend",
    "TpmBackend",
    "TpmKeyProvider",
    "WindowsPlatformCryptoBackend",
    "generate_tpm_key",
    "resolve_tpm_backend",
]

#: What this adapter reports as ``KeyProvider.algorithm()`` -- the same
#: value :data:`promptwitness.secure_enclave.ALGORITHM` uses, duplicated
#: as a literal rather than imported for the same reason
#: ``signing._ECDSA_P256_ALGORITHM`` duplicates it: importing
#: :mod:`promptwitness.secure_enclave` here (or vice versa) would gain
#: nothing and risks a needless coupling between the two hardware
#: adapters. Kept in sync with a parity assertion in
#: ``tests/test_promptwitness_tpm.py``.
ALGORITHM = "ecdsa-p256-sha256"

#: The :data:`signing.STRENGTHS` vocabulary entry this adapter claims --
#: the same tier :mod:`promptwitness.secure_enclave` claims, on different
#: hardware.
STRENGTH = "hardware-nonexportable"

#: Default subprocess budget, equal to
#: :data:`signing._KEYSTORE_TIMEOUT_DEFAULT` for the identical reason
#: :mod:`promptwitness.secure_enclave` gives: a TPM call slower than
#: :func:`ledger.append_line`'s own 2s ``sign_timeout`` has already lost
#: the race.
_TIMEOUT_DEFAULT: float = signing._KEYSTORE_TIMEOUT_DEFAULT

#: The X9.63 uncompressed-point encoding both backends agree on (see the
#: module docstring). Enforced on every read in
#: :meth:`TpmKeyProvider._public_key_bytes`.
_X963_UNCOMPRESSED_TAG = b"\x04"
_X963_POINT_LEN = 65

#: P-256 coordinate size in bytes -- half of a 64-byte raw ``r || s``
#: IEEE P1363 signature pair.
_P256_COORDINATE_LEN = 32

#: Backend names, as accepted by ``PROMPTWITNESS_TPM_BACKEND`` and
#: :func:`resolve_tpm_backend`'s ``backend=`` argument. Mirrors
#: :data:`signing.KEYSTORE_BACKENDS`.
TPM_BACKENDS: tuple[str, ...] = (
    "tpm2-tools",
    "windows-platform-crypto",
)

#: Default Linux persistent object handle, in the vendor/platform
#: persistent range TPM 2.0 reserves (``0x81000000``-``0x81FFFFFF``).
#: Unlike the os-keystore / secure-enclave adapters' key paths, this is
#: NOT namespaced by harness: the TPM's persistent-handle space is small
#: and shared with anything else on the machine that uses it, so
#: synthesizing a per-harness handle risks colliding with an unrelated
#: object more than it helps. An operator running more than one harness
#: against the same TPM should set ``PROMPTWITNESS_TPM_KEY_ID`` per
#: harness explicitly.
_TPM2_HANDLE_DEFAULT = "0x81010002"

#: ASSUMPTION #2 (see module docstring): the exit code this module treats
#: as "no object at this persistent handle yet" for ``tpm2_readpublic``.
_LINUX_READPUBLIC_ABSENT_RETURNCODES: tuple[int, ...] = (1,)

#: This module's OWN PowerShell script fully controls its exit codes, so
#: unlike the Linux assumption above, this is a design choice, not a
#: guess -- mirrors ``secure_enclave``'s ``EXIT_NO_KEY=2`` for its own
#: helper exactly.
_WINDOWS_ABSENT_EXIT_CODE = 2


@runtime_checkable
class TpmBackend(Protocol):
    """One TPM, reduced to "make a key" / "give me the public key" /
    "sign this". Deliberately NOT a key provider: a backend knows nothing
    about the strength vocabulary, ``key_id``, or the process-wide warning
    -- :class:`TpmKeyProvider` holds all of that, which is what lets both
    platforms share one provider and one test suite for the signing
    behaviour (mirrors :class:`signing.KeystoreBackend`'s own role).

    A backend raises :class:`ledger.KeyUnavailable` (or its
    :class:`signing.KeystoreItemAbsent` subclass for "no key yet") for any
    failure, and logs NOTHING -- the single process-wide warning is
    :class:`TpmKeyProvider`'s job, exactly as :class:`signing.KeystoreBackend`
    documents for itself.
    """

    def name(self) -> str: ...

    def create(self, *, force: bool = False) -> bytes: ...

    def public_key(self) -> bytes: ...

    def sign(self, payload: bytes) -> bytes: ...


def _pem_public_key_to_x963(pem_bytes: bytes) -> bytes:
    """Convert a PEM-encoded EC public key (``tpm2_readpublic -f pem``'s
    output) to the X9.63 uncompressed point. Ordinary ``cryptography`` PEM
    parsing -- not a guess, unlike the signature-format assumptions above.
    Lazy import, matching every other adapter in this package -- and, like
    ``OsKeystoreKeyProvider._load_private_key``, an ABSENT
    ``cryptography`` is a degrade, not a traceback: this package is
    installable without the ``witness-sign`` extra, so a missing wheel
    must reach the caller as :class:`ledger.KeyUnavailable` (an unsigned
    line) rather than an ``ImportError`` escaping into a hook.
    """
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            PublicFormat,
            load_pem_public_key,
        )
    except ImportError as exc:
        signing._warn_key_unavailable_once(
            "promptwitness: the 'cryptography' package is not installed "
            "(install the 'witness-sign' extra) -- signing lines unsigned for "
            "the rest of this process"
        )
        raise ledger.KeyUnavailable("cryptography is not installed") from exc

    try:
        key = load_pem_public_key(pem_bytes)
    except ValueError as exc:
        raise ledger.KeyUnavailable(
            "tpm2-tools: public key output does not parse as PEM"
        ) from exc
    if not isinstance(key, ec.EllipticCurvePublicKey):
        raise ledger.KeyUnavailable(
            f"tpm2-tools: public key is a {type(key).__name__}, not an EC key"
        )
    return key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)


def _normalize_signature_to_der(raw: bytes, *, backend_name: str) -> bytes:
    """Return *raw* as a DER-encoded ECDSA signature, accepting EITHER the
    IEEE P1363 raw ``r || s`` pair or an already-DER signature.

    :func:`signing.verify_signature` and ``ledger._verify_line_signature``
    both require DER. .NET's ``ECDsaCng.SignHash`` returns raw ``r || s``,
    so a conversion is needed at least for Windows.

    **Why this accepts both, rather than asserting the one it expects.**
    ASSUMPTION #1 in the module docstring is that ``tpm2_sign -f plain``
    emits raw ``r || s``. The tool's own documentation does not say, and a
    build lane cannot settle it: there is no TPM here. The two candidate
    encodings are, however, trivially distinguishable -- a P-256 raw pair
    is exactly 64 bytes, and a DER signature is a SEQUENCE (leading
    ``0x30``) whose declared length matches its own size -- so the
    assumption does not have to be load-bearing at all. "Trivially
    distinguishable" is a practical claim, not a literal one: a DER
    signature whose ``r`` and ``s`` both happen to lose several leading
    zero bytes could in principle be exactly 64 bytes and be read as a
    raw pair. That needs roughly 2^-30-scale luck per signature, and the
    failure mode is a signature that does not verify -- loud -- never one
    that verifies wrongly. Accepting both
    costs a dozen lines and makes the Linux path correct whichever
    encoding real hardware turns out to produce.

    That choice is deliberate, and it is the lesson of the sibling
    adapter: athenaeum#2153 shipped a public-key encoding mistake twice,
    invisible to a test suite whose fake tool encoded the same wrong
    assumption, and only a real device would have failed. Where an
    unverifiable assumption can be replaced by a cheap runtime
    discrimination, replace it.

    Anything that is neither shape is :class:`ledger.KeyUnavailable` --
    a tool whose output this module cannot interpret must say so loudly
    rather than hand a malformed signature to the ledger.
    """
    from cryptography.hazmat.primitives.asymmetric.utils import (
        decode_dss_signature,
        encode_dss_signature,
    )

    if len(raw) == 2 * _P256_COORDINATE_LEN:
        r = int.from_bytes(raw[:_P256_COORDINATE_LEN], "big")
        s = int.from_bytes(raw[_P256_COORDINATE_LEN:], "big")
        return encode_dss_signature(r, s)
    if raw[0:1] == b"\x30":
        # A DER SEQUENCE. Round-trip it through decode/encode rather than
        # trusting the leading byte: that both VALIDATES the structure
        # (a malformed body raises here, not later inside the verifier)
        # and re-emits a canonical encoding, so a tool that pads or
        # mis-encodes an integer cannot leak through.
        try:
            r, s = decode_dss_signature(raw)
        except Exception as exc:  # any decode failure means "unusable", not a crash
            raise ledger.KeyUnavailable(
                f"{backend_name}: signature starts as a DER SEQUENCE but does not "
                "decode as one"
            ) from exc
        return encode_dss_signature(r, s)
    raise ledger.KeyUnavailable(
        f"{backend_name}: signature is {len(raw)} bytes and is neither a "
        f"{2 * _P256_COORDINATE_LEN}-byte raw r||s pair nor a DER SEQUENCE"
    )


def _b64decode_wire(encoded: bytes, *, backend_name: str, what: str) -> bytes:
    """Shared base64 decode for the Windows backend's script output --
    never a bare ``binascii.Error`` escaping to the caller."""
    try:
        return base64.b64decode(encoded.strip(), validate=True)
    except (ValueError, TypeError) as exc:
        raise ledger.KeyUnavailable(f"{backend_name}: {what} output is not base64") from exc


# ---------------------------------------------------------------------------
# Linux: tpm2-tools
# ---------------------------------------------------------------------------


class Tpm2ToolsBackend:
    """Linux TPM backend through ``tpm2-tools`` (``tpm2_createprimary`` /
    ``tpm2_create`` / ``tpm2_evictcontrol`` / ``tpm2_readpublic`` /
    ``tpm2_sign``), reached at a fixed persistent object handle.

    Every real tool invocation goes through :meth:`_run`, which reuses
    :func:`signing._run_keystore_tool` directly rather than re-implementing
    its discipline: fixed argv, never a shell, a detached session, a
    bounded timeout, every failure becoming :class:`ledger.KeyUnavailable`.
    Context files (``tpm2-tools`` is inherently file-based -- objects are
    referenced by context file or persistent handle, not by a value that
    can ride on stdin) live in a private per-call temporary directory,
    created and torn down within each method; nothing written there is
    secret (public key material, or opaque wrapped-key context blobs that
    are useless without this exact TPM), except the signing payload
    (see :meth:`sign`'s own note on why that one DOES touch disk, per
    ASSUMPTION #6 in the module docstring).
    """

    def __init__(self, *, handle: str, timeout: float = _TIMEOUT_DEFAULT) -> None:
        self.handle = handle
        self.timeout = timeout

    def name(self) -> str:
        return "tpm2-tools"

    def _run(
        self,
        argv: list[str],
        *,
        action: str,
        input_bytes: bytes | None = None,
        absent_returncodes: tuple[int, ...] = (),
    ) -> bytes:
        return signing._run_keystore_tool(
            argv,
            input_bytes=input_bytes,
            timeout=self.timeout,
            backend=self.name(),
            action=action,
            absent_returncodes=absent_returncodes,
        )

    def public_key(self) -> bytes:
        with tempfile.TemporaryDirectory(prefix="promptwitness-tpm2-") as tmp:
            pub_path = Path(tmp) / "pub.pem"
            self._run(
                ["tpm2_readpublic", "-c", self.handle, "-f", "pem", "-o", str(pub_path)],
                action="read the public key",
                absent_returncodes=_LINUX_READPUBLIC_ABSENT_RETURNCODES,
            )
            pem_bytes = pub_path.read_bytes()
        return _pem_public_key_to_x963(pem_bytes)

    def create(self, *, force: bool = False) -> bytes:
        """Create a new ECC P-256 signing key under the owner hierarchy's
        (deterministic -- TPM 2.0 primaries are reproducible from the same
        hierarchy seed and template) primary, and persist it at
        :attr:`handle`.

        ``force=True`` has to evict whatever already occupies
        :attr:`handle` -- a persistent handle cannot be overwritten without
        first being cleared (ASSUMPTION #2's bounded-risk note describes
        the TPM-enforced side of this same fact). A handle that was already
        empty makes that clear attempt fail harmlessly; the failure is
        swallowed, mirroring every other adapter's ``--force`` contract of
        "I know, do it anyway" rather than re-running the full existence
        probe.

        **The evict happens LAST, immediately before the persist, not
        first.** Seer review on PR athenaeum#2359 caught the obvious
        ordering -- evict, then build the new key -- and it is wrong in a
        way that matters for a key nothing can restore: the three
        generation calls (``tpm2_createprimary``, ``tpm2_create``, and
        reading the result back) can each fail or time out, and in the
        obvious order any of them leaves the operator with the old key
        destroyed and no new key persisted. Building the new key object in
        the TPM first costs nothing -- an unpersisted object is discarded
        when its context file goes -- and shrinks the window in which no
        usable key exists to the evict/persist pair, which is irreducible:
        the TPM offers no atomic replace-at-handle.
        """
        with tempfile.TemporaryDirectory(prefix="promptwitness-tpm2-") as tmp:
            tmp_path = Path(tmp)
            primary_ctx = tmp_path / "primary.ctx"
            key_ctx = tmp_path / "key.ctx"
            pub_file = tmp_path / "key.pub"
            priv_file = tmp_path / "key.priv"
            self._run(
                [
                    "tpm2_createprimary",
                    "-C",
                    "o",
                    "-g",
                    "sha256",
                    "-G",
                    "ecc256",
                    "-c",
                    str(primary_ctx),
                ],
                action="create the primary key",
            )
            self._run(
                [
                    "tpm2_create",
                    "-C",
                    str(primary_ctx),
                    "-g",
                    "sha256",
                    "-G",
                    "ecc256",
                    "-u",
                    str(pub_file),
                    "-r",
                    str(priv_file),
                    "-c",
                    str(key_ctx),
                ],
                action="create the signing key",
            )
            # Only now, with the replacement key already built inside the
            # TPM, is the old one cleared -- see the docstring.
            if force:
                with contextlib.suppress(ledger.KeyUnavailable):
                    self._run(
                        ["tpm2_evictcontrol", "-C", "o", "-c", self.handle],
                        action="clear the existing persistent handle",
                    )
            self._run(
                ["tpm2_evictcontrol", "-C", "o", "-c", str(key_ctx), self.handle],
                action="persist the signing key",
            )
        return self.public_key()

    def sign(self, payload: bytes) -> bytes:
        """DER-encoded ECDSA signature over *payload*.

        ``tpm2_sign``'s message argument is a file path, not a stdin
        stream (ASSUMPTION #6): *payload* is written to a mode-0600 file
        in a private per-call temporary directory and the PATH -- never
        the payload bytes -- goes on argv. ``-f plain`` is ASSUMPTION #1;
        its raw ``r || s`` output is converted to DER by
        :func:`_normalize_signature_to_der`.
        """
        with tempfile.TemporaryDirectory(prefix="promptwitness-tpm2-") as tmp:
            tmp_path = Path(tmp)
            message_path = tmp_path / "message.bin"
            sig_path = tmp_path / "sig.bin"
            fd = os.open(str(message_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
            self._run(
                [
                    "tpm2_sign",
                    "-c",
                    self.handle,
                    "-g",
                    "sha256",
                    "-f",
                    "plain",
                    "-o",
                    str(sig_path),
                    str(message_path),
                ],
                action="sign",
                # NO absent_returncodes here, deliberately. ASSUMPTION #2
                # buys the right to read exit 1 as "no object at this
                # handle" for a bare `tpm2_readpublic`, where that is the
                # dominant cause and the classification feeds a decision
                # that NEEDS it (whether `generate_tpm_key` may overwrite).
                # `tpm2_sign` has neither property: exit 1 is "general
                # non-specific error" and a signing call can fail for
                # session, policy or PCR-state reasons that have nothing
                # to do with the key's existence, and nothing here needs
                # the distinction -- a failed sign is "cannot sign right
                # now" either way. Claiming absence would put
                # `key.error: "KeystoreItemAbsent"` on the ledger line,
                # which tells an operator "you never created a key" about
                # a key that is fine. In an audit trail, a confident
                # wrong diagnosis is worse than a vague true one. (The
                # secure-enclave adapter DOES classify absence on sign,
                # and may: its helper is this repo's own code with a
                # dedicated exit code, not an inference about a
                # third-party tool.)
            )
            raw_sig = sig_path.read_bytes()
        return _normalize_signature_to_der(raw_sig, backend_name=self.name())


# ---------------------------------------------------------------------------
# Windows: CNG "Microsoft Platform Crypto Provider"
# ---------------------------------------------------------------------------

#: This module's OWN PowerShell script (ASSUMPTION #4 in the module
#: docstring covers how far the managed ``ECDsaCng`` wrapper can be
#: trusted against this specific KSP). Parameterized by ``-Action``
#: (``create``/``pubkey``/``sign``), ``-KeyName`` and ``-Force`` -- all
#: non-secret lookup coordinates, so they travel on argv exactly like
#: ``SecretServiceBackend``'s ``service``/``account`` do. The sign
#: payload travels on stdin, read via ``[Console]::OpenStandardInput()``,
#: never interpolated into the script text -- the script text itself is
#: fixed and carries no caller-influenced data at all, which is what the
#: "pass the script on stdin, or via -File" choice in this module's own
#: review brief is for: either way, nothing attacker-influenced ever
#: becomes part of a command STRING.
_POWERSHELL_SCRIPT = r"""
param(
    [Parameter(Mandatory=$true)][string]$Action,
    [Parameter(Mandatory=$true)][string]$KeyName,
    [switch]$Force
)
$ErrorActionPreference = 'Stop'
$ExitNotFound = 2
$ExitFailure = 1

function Write-PointBase64($ecdsaKey) {
    $params = $ecdsaKey.ExportParameters($false)
    $point = New-Object byte[] 65
    $point[0] = 0x04
    [System.Array]::Copy($params.Q.X, 0, $point, 1, 32)
    [System.Array]::Copy($params.Q.Y, 0, $point, 33, 32)
    [Console]::Out.Write([Convert]::ToBase64String($point))
}

try {
    $providerName = "Microsoft Platform Crypto Provider"
    $provider = New-Object System.Security.Cryptography.CngProvider($providerName)

    if ($Action -eq 'create') {
        $creationParams = New-Object System.Security.Cryptography.CngKeyCreationParameters
        $creationParams.Provider = $provider
        $creationParams.ExportPolicy = [System.Security.Cryptography.CngExportPolicies]::None
        $creationParams.KeyUsage = [System.Security.Cryptography.CngKeyUsages]::Signing
        if ($Force) {
            $overwrite = [System.Security.Cryptography.CngKeyCreationOptions]::OverwriteExistingKey
            $creationParams.KeyCreationOptions = $overwrite
        }
        $key = [System.Security.Cryptography.CngKey]::Create(
            [System.Security.Cryptography.CngAlgorithm]::ECDsaP256, $KeyName, $creationParams)
        $ecdsa = New-Object System.Security.Cryptography.ECDsaCng($key)
        Write-PointBase64 $ecdsa
        exit 0
    }

    try {
        $key = [System.Security.Cryptography.CngKey]::Open($KeyName, $provider)
    } catch {
        [Console]::Error.WriteLine("key not found")
        exit $ExitNotFound
    }
    $ecdsa = New-Object System.Security.Cryptography.ECDsaCng($key)

    if ($Action -eq 'pubkey') {
        Write-PointBase64 $ecdsa
        exit 0
    }

    if ($Action -eq 'sign') {
        $stdin = [Console]::OpenStandardInput()
        $ms = New-Object System.IO.MemoryStream
        $stdin.CopyTo($ms)
        $payload = $ms.ToArray()
        $sha = [System.Security.Cryptography.SHA256]::Create()
        $hash = $sha.ComputeHash($payload)
        $sigRaw = $ecdsa.SignHash($hash)
        [Console]::Out.Write([Convert]::ToBase64String($sigRaw))
        exit 0
    }

    [Console]::Error.WriteLine("unknown action: $Action")
    exit 64
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit $ExitFailure
}
"""


class WindowsPlatformCryptoBackend:
    """Windows TPM backend through CNG's "Microsoft Platform Crypto
    Provider", reached via a PowerShell child process running this
    module's own fixed script (:data:`_POWERSHELL_SCRIPT`).

    Non-exportable: the key is created with ``CngExportPolicies.None`` and
    the private handle never leaves the PowerShell child's process -- only
    the base64-encoded public point or signature crosses back over stdout.
    """

    def __init__(
        self,
        *,
        key_name: str,
        powershell_path: str | os.PathLike[str] = "powershell",
        timeout: float = _TIMEOUT_DEFAULT,
    ) -> None:
        self.key_name = key_name
        self.powershell_path = powershell_path
        self.timeout = timeout

    def name(self) -> str:
        return "windows-platform-crypto"

    def _run(
        self,
        action: str,
        *,
        input_bytes: bytes | None,
        extra_args: tuple[str, ...] = (),
    ) -> bytes:
        with tempfile.TemporaryDirectory(prefix="promptwitness-tpmcng-") as tmp:
            script_path = Path(tmp) / "tpm-cng.ps1"
            script_path.write_text(_POWERSHELL_SCRIPT, encoding="utf-8")
            argv = [
                str(self.powershell_path),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
                "-Action",
                action,
                "-KeyName",
                self.key_name,
                *extra_args,
            ]
            return signing._run_keystore_tool(
                argv,
                input_bytes=input_bytes,
                timeout=self.timeout,
                backend=self.name(),
                action=action,
                absent_returncodes=(_WINDOWS_ABSENT_EXIT_CODE,),
            )

    def create(self, *, force: bool = False) -> bytes:
        encoded = self._run(
            "create", input_bytes=None, extra_args=("-Force",) if force else ()
        )
        return _b64decode_wire(encoded, backend_name=self.name(), what="pubkey")

    def public_key(self) -> bytes:
        encoded = self._run("pubkey", input_bytes=None)
        return _b64decode_wire(encoded, backend_name=self.name(), what="pubkey")

    def sign(self, payload: bytes) -> bytes:
        encoded = self._run("sign", input_bytes=payload)
        raw_sig = _b64decode_wire(encoded, backend_name=self.name(), what="signature")
        return _normalize_signature_to_der(raw_sig, backend_name=self.name())


# ---------------------------------------------------------------------------
# TpmKeyProvider -- the common logic over either backend
# ---------------------------------------------------------------------------


class TpmKeyProvider:
    """TPM-backed ``KeyProvider`` (athenaeum#2154), over either
    :class:`Tpm2ToolsBackend` or :class:`WindowsPlatformCryptoBackend` --
    mirrors ``signing.py``'s ``OsKeystoreKeyProvider`` holding the
    cryptography/strength logic while treating its backend as dumb
    storage, except here the backend does the signing itself (the private
    key never leaves the TPM to be handed to this process at all).

    ``presence_required()`` is ``False`` (operator ruling 1): nothing here
    proves a human triggered the signature, only that this device's TPM
    did.
    """

    def __init__(self, backend: TpmBackend) -> None:
        self.backend = backend

    def _run(self, call: Callable[..., bytes], *args: bytes) -> bytes:
        """The one place every backend call goes through, and therefore
        the one place the process-wide "key unavailable" warning
        (:func:`signing._warn_key_unavailable_once`) is emitted -- mirrors
        :meth:`promptwitness.secure_enclave.SecureEnclaveKeyProvider._run`'s
        role."""
        try:
            return call(*args)
        except ledger.KeyUnavailable as exc:
            signing._warn_key_unavailable_once(
                f"promptwitness: the {self.backend.name()} TPM signing key is "
                f"unavailable ({exc}) -- signing lines unsigned for the rest "
                "of this process"
            )
            raise

    def _public_key_bytes(self) -> bytes:
        raw = self._run(self.backend.public_key)
        # The one enforcement point for the encoding agreement the module
        # docstring describes -- mirrors
        # SecureEnclaveKeyProvider._public_key_bytes exactly, including
        # why: without it, a backend that regressed to an untagged point
        # would be accepted silently, producing a wrong-but-stable
        # key_id indistinguishable from a tampered signature.
        if len(raw) != _X963_POINT_LEN or raw[0:1] != _X963_UNCOMPRESSED_TAG:
            signing._warn_key_unavailable_once(
                f"promptwitness: the {self.backend.name()} public key is "
                f"{len(raw)} bytes, not a {_X963_POINT_LEN}-byte X9.63 "
                "uncompressed point -- signing lines unsigned for the rest "
                "of this process"
            )
            raise ledger.KeyUnavailable(
                f"{self.backend.name()}: public key is {len(raw)} bytes, not "
                f"a {_X963_POINT_LEN}-byte X9.63 uncompressed point"
            )
        return raw

    def key_id(self) -> str:
        return signing.derive_key_id(self._public_key_bytes(), ALGORITHM)

    def algorithm(self) -> str:
        return ALGORITHM

    def strength(self) -> str:
        return STRENGTH

    def presence_required(self) -> bool:
        return False

    def sign(self, payload: bytes) -> bytes:
        return self._run(self.backend.sign, payload)

    def public_key(self) -> bytes:
        return self._public_key_bytes()


# ---------------------------------------------------------------------------
# Resolution and keygen
# ---------------------------------------------------------------------------


def _default_tpm_backend_name(platform: str) -> str:
    """The backend this platform implies -- mirrors
    ``signing._default_keystore_backend_name``: Windows platform strings
    are recognized explicitly, everything else (including Linux) falls
    through to ``tpm2-tools``, which is also the right FAILURE for a
    platform with no TPM access at all (an absent ``tpm2_readpublic``
    degrades to an unsigned line with one warning, which beats raising on
    an unrecognized ``sys.platform``)."""
    if platform.startswith("win") or platform == "cygwin":
        return "windows-platform-crypto"
    return "tpm2-tools"


def resolve_tpm_backend(
    harness: str,
    *,
    backend: str | None = None,
    key_id: str | None = None,
    powershell_path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
    timeout: float = _TIMEOUT_DEFAULT,
) -> TpmBackend:
    """Build the :class:`TpmBackend` for *harness* -- mirrors
    :func:`signing.resolve_keystore_backend`.

    Backend precedence: ``backend`` argument > ``PROMPTWITNESS_TPM_BACKEND``
    env > :data:`sys.platform`. Key-identifier precedence: ``key_id``
    argument > ``PROMPTWITNESS_TPM_KEY_ID`` env > a per-backend default --
    the fixed persistent-handle default (:data:`_TPM2_HANDLE_DEFAULT`) for
    ``tpm2-tools``, or a harness-namespaced CNG key-container name for
    ``windows-platform-crypto`` (a plain key-store name has no shared
    numeric-handle-space constraint, so namespacing it by harness is safe,
    unlike the Linux default -- see that constant's own docstring).

    Raises :class:`ValueError` for an unknown backend name.
    """
    environ = env if env is not None else os.environ
    name = backend if backend is not None else environ.get("PROMPTWITNESS_TPM_BACKEND")
    name = name or _default_tpm_backend_name(platform if platform is not None else sys.platform)
    if name not in TPM_BACKENDS:
        raise ValueError(
            f"unknown PROMPTWITNESS_TPM_BACKEND {name!r}; known backends are "
            f"{', '.join(TPM_BACKENDS)}"
        )
    resolved_key_id = key_id if key_id is not None else environ.get("PROMPTWITNESS_TPM_KEY_ID")
    if name == "windows-platform-crypto":
        resolved_key_id = resolved_key_id or f"promptwitness-tpm-{harness}"
        ps_path = (
            powershell_path
            if powershell_path is not None
            else environ.get("PROMPTWITNESS_TPM_POWERSHELL") or "powershell"
        )
        return WindowsPlatformCryptoBackend(
            key_name=resolved_key_id, powershell_path=ps_path, timeout=timeout
        )
    resolved_key_id = resolved_key_id or _TPM2_HANDLE_DEFAULT
    return Tpm2ToolsBackend(handle=resolved_key_id, timeout=timeout)


def generate_tpm_key(backend: TpmBackend, *, replace: bool = False) -> TpmKeyProvider:
    """Drive *backend*'s ``create()`` and verify the new key by reading the
    public key back -- the same discipline
    :func:`promptwitness.secure_enclave.generate_secure_enclave_key` /
    :func:`signing.generate_and_store_keystore_key` follow.

    Refuses to replace an existing key unless *replace* is ``True``:
    replacing a non-exportable TPM signing key makes every line it ever
    signed permanently unverifiable, with no copy anywhere to restore one
    from.

    The existence probe calls ``backend.public_key()`` directly -- never
    through :class:`TpmKeyProvider`, so a miss here (the EXPECTED first-run
    outcome) does not trip the process-wide "key unavailable" warning a
    real failure deserves. Only :class:`signing.KeystoreItemAbsent` counts
    as "nothing to lose"; any other :class:`ledger.KeyUnavailable` means
    the backend could not say what is there and is re-raised rather than
    treated as permission to overwrite.

    When *replace* is ``True`` the probe is skipped entirely and
    ``backend.create(force=True)`` is called directly -- ``--force`` is the
    operator saying "I know, do it anyway", so it rotates through an
    unreadable backend too.
    """
    if not replace:
        try:
            backend.public_key()
        except signing.KeystoreItemAbsent:
            pass
        else:
            raise FileExistsError(
                f"a {backend.name()} TPM signing key already exists; refusing to replace it"
            )
    backend.create(force=replace)
    provider = TpmKeyProvider(backend)
    # Read back to verify the key actually landed and is usable; raises
    # ledger.KeyUnavailable (through _run, with its own single warning) if
    # not. The key IS already created by this point either way.
    provider.public_key()
    return provider
