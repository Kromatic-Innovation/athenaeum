# SPDX-License-Identifier: Apache-2.0
"""``SecureEnclaveKeyProvider``: a non-exportable Secure Enclave key adapter
for promptwitness, reached through the signed Swift helper at
``tools/promptwitness-se/`` (athenaeum#2153).

**Module placement note.** The issue body named this module
``src/promptwitness/keys/secure_enclave.py`` — a ``keys/`` package. No such
package exists in this repo, and the one issue that named that shape
before (athenaeum#2152, the ``os-keystore`` adapter) shipped into
``signing.py`` itself rather than creating one. The established pattern
for a backend-specific adapter module here is a flat SIBLING module —
``anchor_rfc3161.py`` next to ``anchor.py`` is the precedent this follows
(see that module's own "Module layout note"). This module is therefore
``secure_enclave.py``, a sibling of ``signing.py``, not a subpackage.

Layering: L1.5, same as ``signing.py`` — depends on
:mod:`promptwitness.signing` and :mod:`promptwitness.ledger` (one way
only), stdlib otherwise. No ``athenaeum`` import anywhere (enforced by
``tests/test_promptwitness_boundary.py``'s AST scan) and no
``cryptography`` import at module level: this module never needs it at
all (unlike ``signing.py``, it has no verification path of its own —
verification goes through :func:`signing.verify_signature`, which already
imports ``cryptography`` lazily).

**What this tier protects, and what it does not.** Non-exportability is
what a Secure Enclave key adds over ``os-keystore``: the key's opaque
``dataRepresentation`` blob can be read straight off disk, but it is
useless without the same physical device's Secure Enclave to unwrap it —
so a signature proves the line was signed ON THIS MACHINE, which an
``os-keystore`` signature cannot. Operator ruling 1
(``docs/design/witness-ledger.md``) declined per-signature user presence,
so this adapter proves nothing about a human being present:
:meth:`SecureEnclaveKeyProvider.presence_required` is ``False``, exactly
like every other adapter in this package. Only ``hardware-presence`` (a
seam only, no adapter shipped for it) would change that.

**Why a Swift helper at all.** CryptoKit's
``SecureEnclave.P256.Signing.PrivateKey`` has no Python (or even C)
binding; the only way to reach it is a small Swift binary. This module
shells out to it exactly the way ``signing._run_keystore_tool`` shells out
to the OS keystore tools — fixed argv, no shell, the payload on stdin
only, ``start_new_session=True``, a bounded timeout, and every failure
mapped onto :class:`ledger.KeyUnavailable`. It reuses
:func:`signing._run_keystore_tool` directly rather than re-implementing
that discipline a second time (the leading underscore marks it
module-private by convention, not a hard package boundary — this is an
intra-package reuse the issue's own brief asks for: "Prefer
importing/reusing signing's existing helpers over copying them").

**"No key yet" is reused, not re-subclassed.** The helper's "no key file"
exit code is mapped onto :class:`signing.KeystoreItemAbsent` directly,
rather than a second, meaning-duplicating subclass here — "the backing
store holds no key item" is exactly as backend-agnostic a claim for this
Swift helper as it is for an OS keystore tool, and `KeystoreItemAbsent` is
already a :class:`ledger.KeyUnavailable` subclass, so every existing
degrade path keeps working with no special case.

**Encoding agreement with the Swift helper (load-bearing).** The public
key is CryptoKit's ``.x963Representation`` — the uncompressed X9.63 point
(``0x04 || X || Y``, 65 bytes for P-256) — base64-encoded on the helper's
stdout, by both its ``create`` and ``pubkey`` subcommands. Specifically
NOT CryptoKit's ``.rawRepresentation``, which is the bare 64-byte
``X || Y`` pair with no leading ``0x04`` tag:
:func:`signing.verify_signature` rebuilds the key with
``EllipticCurvePublicKey.from_encoded_point``, which accepts only the
tagged form, so the untagged one would fail every verification on a real
device while the fake helper the tests run against (which emits the
tagged form) stayed green. A
signature is the DER encoding of the ECDSA signature
(``P256.Signing.ECDSASignature.derRepresentation``), raw bytes on stdout.
Both sides must agree byte-for-byte: a mismatch would silently change
every ``key_id`` this adapter derives (:func:`signing.derive_key_id`
hashes the raw public key bytes), with no error to signal it.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Mapping
from pathlib import Path

from promptwitness import ledger, signing

__all__ = [
    "ALGORITHM",
    "STRENGTH",
    "SecureEnclaveKeyProvider",
    "generate_secure_enclave_key",
    "resolve_helper_path",
    "resolve_key_path",
]

#: What this adapter reports as ``KeyProvider.algorithm()``. CryptoKit's
#: ``SecureEnclave.P256.Signing.PrivateKey.signature(for:)`` hashes with
#: SHA-256 internally before signing (the same semantics as
#: ``cryptography``'s ``ec.ECDSA(hashes.SHA256())``) — this name matches
#: what :func:`signing.verify_signature` is extended to accept.
ALGORITHM = "ecdsa-p256-sha256"

#: The :data:`signing.STRENGTHS` vocabulary entry this adapter claims.
STRENGTH = "hardware-nonexportable"

#: Default subprocess budget, equal to
#: :data:`signing._KEYSTORE_TIMEOUT_DEFAULT` for the identical reason that
#: constant gives: a Secure Enclave call slower than
#: :func:`ledger.append_line`'s own 2s ``sign_timeout`` has already lost
#: the race, so a larger budget here would only keep a doomed subprocess
#: alive.
_TIMEOUT_DEFAULT: float = signing._KEYSTORE_TIMEOUT_DEFAULT

#: The helper's documented "no key file yet" exit code
#: (``tools/promptwitness-se/Sources/promptwitness-se/main.swift``'s
#: ``ExitCode.noKey``). Every other non-zero exit means the helper itself
#: could not say what is there (enclave unavailable, a blob it rejects, a
#: crypto failure) and must never be read as "nothing to lose".
_EXIT_NO_KEY = 2


def resolve_helper_path(
    *,
    override: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the ``promptwitness-se`` helper binary path.

    Precedence mirrors every other resolver in this package:
    ``override`` argument > ``PROMPTWITNESS_SE_HELPER`` env > the bare
    command name ``"promptwitness-se"``, left for :mod:`subprocess` to
    find on ``PATH`` — exactly how ``signing._run_keystore_tool``'s own
    argv (``"security"``, ``"secret-tool"``) already relies on
    :mod:`subprocess` searching ``PATH`` for an unqualified name. No
    hardcoded install location, and nothing machine-specific for the
    public-safe-lint gate to reject.
    """
    if override is not None:
        return Path(override).expanduser()
    environ = env if env is not None else os.environ
    env_path = environ.get("PROMPTWITNESS_SE_HELPER")
    if env_path:
        return Path(env_path).expanduser()
    return Path("promptwitness-se")


def resolve_key_path(
    harness: str,
    *,
    override: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the Secure Enclave key-blob path for *harness*.

    Precedence mirrors :func:`signing.resolve_key_path`: ``override``
    argument > ``PROMPTWITNESS_SE_KEY_PATH`` env >
    ``<ledger_dir>/keys/secure-enclave.key`` under
    :func:`ledger.resolve_ledger_dir`. The file holds the key's opaque
    ``dataRepresentation`` blob, not a PEM — CryptoKit's own encoding,
    round-tripped by this module but never parsed by it.
    """
    if override is not None:
        return Path(override).expanduser()
    environ = env if env is not None else os.environ
    env_path = environ.get("PROMPTWITNESS_SE_KEY_PATH")
    if env_path:
        return Path(env_path).expanduser()
    return ledger.resolve_ledger_dir(harness) / "keys" / "secure-enclave.key"


def _run_helper(
    helper_path: str | os.PathLike[str],
    key_path: str | os.PathLike[str],
    subcommand: str,
    *extra_flags: str,
    input_bytes: bytes | None,
    timeout: float,
) -> bytes:
    """Run the Swift helper's *subcommand*, mapping failures onto
    :class:`ledger.KeyUnavailable` / :class:`signing.KeystoreItemAbsent`
    exactly like :func:`signing._run_keystore_tool` already does for the
    OS-keystore tools — reused directly rather than re-implemented (see
    the module docstring): fixed argv, the payload on stdin only, a
    detached session, a bounded timeout, and child stderr never
    interpolated into the raised message.
    """
    argv = [str(helper_path), subcommand, "--key-file", str(key_path), *extra_flags]
    return signing._run_keystore_tool(
        argv,
        input_bytes=input_bytes,
        timeout=timeout,
        backend="secure-enclave",
        action=subcommand,
        absent_returncodes=(_EXIT_NO_KEY,),
    )


class SecureEnclaveKeyProvider:
    """Secure Enclave-backed ``KeyProvider``, reached through the
    ``tools/promptwitness-se/`` Swift helper (athenaeum#2153).

    Non-exportable: the key's opaque ``dataRepresentation`` blob lives at
    *key_path*, but it cannot be used to sign anywhere except on this
    physical device — only the Secure Enclave that created it can use it.
    ``presence_required()`` is ``False`` (operator ruling 1): nothing here
    proves a human triggered the signature, only that this device's
    enclave did.

    Every method shells out to *helper_path* fresh — no key material is
    ever held by this Python process (the Secure Enclave never releases
    the private key at all; only a signature or the public key crosses
    the process boundary). This is a stronger version of the
    "never cache across calls" discipline :class:`signing.SoftwareFileKeyProvider`
    / :class:`signing.OsKeystoreKeyProvider` already follow: there is no
    key to cache here, only the ability to ask the enclave to act.
    """

    def __init__(
        self,
        *,
        helper_path: str | os.PathLike[str],
        key_path: str | os.PathLike[str],
        timeout: float = _TIMEOUT_DEFAULT,
    ) -> None:
        self.helper_path = Path(helper_path)
        self.key_path = Path(key_path)
        self.timeout = timeout

    def _run(self, subcommand: str, *, input_bytes: bytes | None) -> bytes:
        """The one place every helper invocation goes through, and
        therefore the one place the process-wide "key unavailable"
        warning (:func:`signing._warn_key_unavailable_once`) is emitted —
        mirrors :meth:`signing.OsKeystoreKeyProvider._seed`'s role for the
        OS-keystore tier.
        """
        try:
            return _run_helper(
                self.helper_path,
                self.key_path,
                subcommand,
                input_bytes=input_bytes,
                timeout=self.timeout,
            )
        except ledger.KeyUnavailable as exc:
            signing._warn_key_unavailable_once(
                f"promptwitness: the secure-enclave signing key is unavailable "
                f"({exc}) -- signing lines unsigned for the rest of this process"
            )
            raise

    def _public_key_bytes(self) -> bytes:
        encoded = self._run("pubkey", input_bytes=None)
        try:
            return base64.b64decode(encoded.strip(), validate=True)
        except (ValueError, TypeError) as exc:
            signing._warn_key_unavailable_once(
                "promptwitness: the secure-enclave helper's pubkey output "
                "does not decode as base64 -- signing lines unsigned for "
                "the rest of this process"
            )
            raise ledger.KeyUnavailable(
                "secure-enclave: helper pubkey output is not base64"
            ) from exc

    def key_id(self) -> str:
        return signing.derive_key_id(self._public_key_bytes(), ALGORITHM)

    def algorithm(self) -> str:
        return ALGORITHM

    def strength(self) -> str:
        return STRENGTH

    def presence_required(self) -> bool:
        return False

    def sign(self, payload: bytes) -> bytes:
        """DER-encoded ECDSA signature over *payload*, straight from the
        helper's stdout — never re-encoded or re-hashed here."""
        return self._run("sign", input_bytes=payload)

    def public_key(self) -> bytes:
        """The X9.63 uncompressed point (65 bytes for P-256) —
        CryptoKit's ``.x963Representation``, base64-decoded from the
        helper's stdout. Must stay byte-identical to what the Swift
        helper encodes; see the module docstring's "encoding agreement"
        note — a mismatch silently changes every derived ``key_id``.
        """
        return self._public_key_bytes()


def generate_secure_enclave_key(
    helper_path: str | os.PathLike[str],
    key_path: str | os.PathLike[str],
    *,
    replace: bool = False,
    timeout: float = _TIMEOUT_DEFAULT,
) -> SecureEnclaveKeyProvider:
    """Drive the helper's ``create`` and verify the new key by reading the
    public key back — the same "a store is verified by reading it back"
    discipline :func:`signing.generate_and_store_keystore_key` follows.

    Refuses to replace an existing key unless *replace* is ``True``, for
    the reason :func:`signing.generate_and_store_keystore_key` refuses:
    replacing a non-exportable signing key makes every line it ever
    signed permanently unverifiable, and there is no copy anywhere to
    restore one from.

    The existence probe calls :func:`_run_helper` directly — bypassing
    :meth:`SecureEnclaveKeyProvider._run`'s warning wrap — because a miss
    here is the EXPECTED first-run outcome and must not trip the
    process-wide "key unavailable" warning a real failure deserves; the
    same reasoning :func:`signing.generate_and_store_keystore_key`'s own
    probe gives for calling ``backend.load()`` directly rather than
    through :meth:`signing.OsKeystoreKeyProvider._seed`.

    Only :class:`signing.KeystoreItemAbsent` counts as "nothing to lose".
    Any other :class:`ledger.KeyUnavailable` from the probe means the
    helper could not say what is there and is re-raised rather than
    treated as permission to overwrite — reading a transient failure as
    emptiness is how a default-path ``keygen`` would destroy a live,
    unrecoverable key.

    When *replace* is ``True`` the probe is skipped entirely and
    ``--force`` is passed straight to the helper, which performs its own
    identical refuse-to-overwrite check — ``--force`` is the operator
    saying "I know, do it anyway", so it rotates through an unreadable
    helper too, the same way :func:`signing.generate_and_store_keystore_key`'s
    own ``replace=True`` path does.
    """
    if not replace:
        try:
            _run_helper(helper_path, key_path, "pubkey", input_bytes=None, timeout=timeout)
        except signing.KeystoreItemAbsent:
            pass
        else:
            raise FileExistsError(
                f"a secure-enclave signing key already exists at {key_path}; "
                "refusing to replace it"
            )
    _run_helper(
        helper_path,
        key_path,
        "create",
        *(("--force",) if replace else ()),
        input_bytes=None,
        timeout=timeout,
    )
    provider = SecureEnclaveKeyProvider(
        helper_path=helper_path, key_path=key_path, timeout=timeout
    )
    # Read back to verify the store actually landed and is usable; raises
    # ledger.KeyUnavailable (through _run, with its own single warning) if
    # not. The key IS already written by this point either way.
    provider.public_key()
    return provider
