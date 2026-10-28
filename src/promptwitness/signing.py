# SPDX-License-Identifier: Apache-2.0
"""``KeyProvider`` protocol, the key-strength vocabulary, and the ``none`` /
``software-file`` / ``os-keystore`` signing adapters (athenaeum#2150,
athenaeum#2152).

Layering: L1.5 (depends on :mod:`promptwitness.canon` and
:mod:`promptwitness.ledger`, stdlib otherwise). Standard library only at
IMPORT time — no ``athenaeum`` import anywhere (enforced by
``tests/test_promptwitness_boundary.py``'s AST scan) and no ``cryptography``
import at module level either: every function that needs it imports it
locally, so a plain ``pip install athenaeum`` (no ``witness-sign`` extra)
can still import this module and use :class:`NoneKeyProvider` /
:func:`resolve_key_provider` with ``PROMPTWITNESS_KEY_PROVIDER=none``.
``ctypes`` (the Windows DPAPI backend) is likewise imported inside the
functions that use it, so the cost lands only on the platform that needs
it.

This module depends on :mod:`promptwitness.ledger` (one way only) so it can
build on :class:`ledger.KeyInfo` / :class:`ledger.KeyUnavailable` and extend
:func:`ledger.append_line`'s signing seam for key rotation. ``ledger.py``
must never import this module back — it stays usable with no key provider
at all, which is the whole point of the ``key_provider=None`` default
:func:`ledger.append_line` already supports.

**Key-strength vocabulary**, weakest to strongest: ``none`` <
``software-file`` < ``os-keystore`` < ``hardware-nonexportable`` <
``hardware-presence``. The first three have adapters here; the hardware
adapters (athenaeum#2153, athenaeum#2154) are separate issues.
``hardware-presence`` is a seam only — operator ruling 1 builds no
presence-gated adapter, so every adapter shipped here returns
``presence_required() == False``.

**``os-keystore`` protects the key AT REST and nothing else** — see
:class:`OsKeystoreKeyProvider`. The seed is encrypted by the OS, so a stolen
disk or backup yields nothing usable; but the user's own session reads it
back without a prompt, so any process running as that user can obtain a
signature exactly as the hook does. That is the deliberate trade: no added
per-turn friction, no presence claim. ``docs/design/witness-ledger.md``
states the same limit in the evidence table.

**``KeyProvider`` vs ``ledger.KeyProviderLike``.** ``ledger.KeyProviderLike``
is the minimal shape the ledger actually calls (``key_id``/``algorithm``/
``strength``/``presence_required``/``sign``). :class:`KeyProvider` here is
the fuller protocol the issue specifies, adding ``public_key`` — needed by
verification and by :func:`derive_key_id`, neither of which the ledger
itself needs. Every :class:`KeyProvider` is structurally a
``KeyProviderLike`` (see ``tests/test_promptwitness_signing.py``'s
``TestProtocolCompatibility``, which asserts this with ``isinstance``
rather than leaving it to inspection); the reverse is not required.

**``key_id`` shape.** Same shape as ``athenaeum.receipt_sealers.
derive_key_id`` (``"<alg>:" + sha256(raw public key).hexdigest()[:16]``),
reimplemented here rather than imported (the package boundary forbids any
``athenaeum`` import) and bound to it with a parity test, exactly the
pattern ``promptwitness.canon.normalize`` uses against
``transcript_verify._normalize``.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import logging
import os
import subprocess
import sys
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from promptwitness import ledger

__all__ = [
    "KEYSTORE_BACKENDS",
    "STRENGTHS",
    "KeyProvider",
    "KeystoreBackend",
    "KeystoreItemAbsent",
    "MacOsKeychainBackend",
    "NoneKeyProvider",
    "OsKeystoreKeyProvider",
    "SecretServiceBackend",
    "SoftwareFileKeyProvider",
    "WindowsDpapiBackend",
    "append_key_rotate_line",
    "derive_key_id",
    "generate_and_store_keystore_key",
    "generate_and_write_key",
    "resolve_key_path",
    "resolve_key_provider",
    "resolve_keystore_backend",
    "resolve_keystore_blob_path",
    "verify_key_rotate_signatures",
    "verify_signature",
]

log = logging.getLogger(__name__)

#: Weakest to strongest (athenaeum#2144 epic body). ``none``,
#: ``software-file`` and ``os-keystore`` have adapters in this module; the
#: two hardware tiers do not yet.
STRENGTHS: tuple[str, ...] = (
    "none",
    "software-file",
    "os-keystore",
    "hardware-nonexportable",
    "hardware-presence",
)

_SOFTWARE_FILE_ALGORITHM = "ed25519"


@runtime_checkable
class KeyProvider(Protocol):
    """The full signing seam (athenaeum#2150's own protocol).

    A superset of :class:`ledger.KeyProviderLike` — adds :meth:`public_key`,
    which verification and :func:`derive_key_id` need but the ledger's own
    append path does not.
    """

    def key_id(self) -> str: ...

    def algorithm(self) -> str: ...

    def strength(self) -> str: ...

    def presence_required(self) -> bool: ...

    def sign(self, payload: bytes) -> bytes: ...

    def public_key(self) -> bytes: ...


def derive_key_id(public_key_raw: bytes, algorithm: str) -> str:
    """``"<algorithm>:" + sha256(public_key_raw).hexdigest()[:16]``.

    Byte-identical in shape to ``athenaeum.receipt_sealers.derive_key_id``
    (which hardcodes ``algorithm="ed25519"`` via its own module constant) —
    see ``tests/test_promptwitness_signing.py``'s
    ``TestDeriveKeyIdParity``, which calls both functions over the same raw
    key bytes and asserts equality, rather than one re-exporting the other
    (the package boundary forbids any ``athenaeum`` import here).
    """
    return f"{algorithm}:{hashlib.sha256(public_key_raw).hexdigest()[:16]}"


def verify_signature(
    payload: bytes, signature: bytes, public_key_raw: bytes, algorithm: str
) -> bool:
    """``True`` iff *signature* over *payload* verifies under *public_key_raw*.

    Lazy ``cryptography`` import — never at module level. Only
    ``"ed25519"`` is implemented (the only algorithm either shipped adapter
    produces); any other algorithm name returns ``False`` rather than
    raising, since an unrecognized algorithm cannot be verified either way.
    """
    if algorithm != _SOFTWARE_FILE_ALGORITHM:
        return False
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(public_key_raw).verify(signature, payload)
    except InvalidSignature:
        return False
    except ValueError:
        # Malformed public key bytes (wrong length, etc.) -- not verifiable.
        return False
    return True


# ---------------------------------------------------------------------------
# "none" adapter
# ---------------------------------------------------------------------------


class NoneKeyProvider:
    """The ``none`` adapter: no signature, the hash chain alone.

    Exists for API completeness and so :meth:`presence_required` can be
    tested directly (AC: "a test asserts both shipped adapters return
    False"). The practical selection path is different: ``append_line``
    already treats ``key_provider=None`` as "write an unsigned line with
    ``key.strength: none`` and no ``sig``, no error" — exactly the ``none``
    adapter's semantics with no extra code — so :func:`resolve_key_provider`
    returns ``None`` (not an instance of this class) for
    ``PROMPTWITNESS_KEY_PROVIDER=none``. Calling :meth:`sign` on an actual
    instance raises :class:`ledger.KeyUnavailable`: there is no key to sign
    with, and the ledger's own unsigned-degrade path treats that exactly
    like any other unavailable key.
    """

    def key_id(self) -> str:
        return "none:0000000000000000"

    def algorithm(self) -> str:
        return "none"

    def strength(self) -> str:
        return "none"

    def presence_required(self) -> bool:
        return False

    def sign(self, payload: bytes) -> bytes:
        raise ledger.KeyUnavailable("the none adapter never signs")

    def public_key(self) -> bytes:
        return b""


# ---------------------------------------------------------------------------
# "software-file" adapter
# ---------------------------------------------------------------------------

#: Guards the "exactly one warning per process" AC. Module-level (not
#: per-instance): the AC is about the whole process, and a hook may
#: construct a fresh :class:`SoftwareFileKeyProvider` on every invocation.
_warn_lock = threading.Lock()
_warned_key_unavailable = False


def _warn_key_unavailable_once(message: str) -> None:
    global _warned_key_unavailable
    with _warn_lock:
        if _warned_key_unavailable:
            return
        _warned_key_unavailable = True
    log.warning(message)


def reset_warned_for_tests() -> None:
    """Reset the process-wide "already warned" flag.

    Test-only seam: without this, the second test in a session to exercise
    the missing-key/no-``cryptography`` path would see zero log records
    because the first test already tripped the module-level flag. Not
    exported from ``__all__`` -- it is a test fixture hook, not public API.
    """
    global _warned_key_unavailable
    with _warn_lock:
        _warned_key_unavailable = False


class SoftwareFileKeyProvider:
    """Ed25519 key read from a PEM file on disk, mode 0600.

    The private key is read fresh on every call that needs it (never cached
    on the instance across calls, mirroring
    ``OnePasswordServiceAccountKeyProvider``'s own "held for this call only"
    discipline) and ``cryptography`` is imported lazily inside
    :meth:`_load_private_key`, never at module import time.

    A missing key file, or ``cryptography`` not installed, surfaces as
    :class:`ledger.KeyUnavailable` from every method that needs the key —
    caught by ``ledger._apply_key_and_sig``'s broad ``except Exception``,
    which degrades the line to unsigned rather than blocking the hook. This
    class itself logs exactly one warning for the whole process the first
    time that happens (see :func:`_warn_key_unavailable_once`); the ledger's
    append path logs nothing additional.
    """

    def __init__(self, key_path: str | os.PathLike[str]) -> None:
        self.key_path = Path(key_path)

    def _load_private_key(self) -> object:
        try:
            from cryptography.hazmat.primitives.serialization import load_pem_private_key
        except ImportError as exc:
            _warn_key_unavailable_once(
                "promptwitness: the 'cryptography' package is not installed "
                "(install the 'witness-sign' extra) -- signing lines "
                "unsigned for the rest of this process"
            )
            raise ledger.KeyUnavailable("cryptography is not installed") from exc
        try:
            material = self.key_path.read_bytes()
        except OSError as exc:
            _warn_key_unavailable_once(
                f"promptwitness: signing key file {self.key_path} is unreadable "
                f"({exc.__class__.__name__}) -- signing lines unsigned for the "
                "rest of this process"
            )
            raise ledger.KeyUnavailable(f"key file unreadable: {self.key_path}") from exc
        try:
            key = load_pem_private_key(material, password=None)
        except ValueError as exc:
            _warn_key_unavailable_once(
                f"promptwitness: signing key file {self.key_path} does not parse "
                "as a PEM private key -- signing lines unsigned for the rest of "
                "this process"
            )
            raise ledger.KeyUnavailable(f"key file does not parse: {self.key_path}") from exc
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        if not isinstance(key, Ed25519PrivateKey):
            raise ledger.KeyUnavailable(
                f"signing key at {self.key_path} is {type(key).__name__}, not Ed25519"
            )
        return key

    def _public_bytes(self) -> bytes:
        # Load the private key FIRST: _load_private_key already guards every
        # cryptography import it needs and turns a missing install or a
        # missing file into KeyUnavailable. Importing Encoding/PublicFormat
        # here only after that succeeds means an absent `cryptography` is
        # never raised as a bare, uncaught ImportError from this method.
        private_key = self._load_private_key()
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        return private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)  # type: ignore[attr-defined]

    def key_id(self) -> str:
        return derive_key_id(self._public_bytes(), _SOFTWARE_FILE_ALGORITHM)

    def algorithm(self) -> str:
        return _SOFTWARE_FILE_ALGORITHM

    def strength(self) -> str:
        return "software-file"

    def presence_required(self) -> bool:
        return False

    def sign(self, payload: bytes) -> bytes:
        private_key = self._load_private_key()
        return bytes(private_key.sign(payload))  # type: ignore[attr-defined]

    def public_key(self) -> bytes:
        return self._public_bytes()


def generate_and_write_key(
    key_path: str | os.PathLike[str], *, replace: bool = False
) -> SoftwareFileKeyProvider:
    """Generate a fresh Ed25519 keypair and write the PRIVATE key to
    *key_path*, mode 0600, PKCS8 PEM.

    The PEM bytes come straight from ``cryptography``'s own
    ``private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())`` —
    never a hand-written ``-----BEGIN ...-----`` literal — so the only
    ``BEGIN``/``END`` banner pair in this process's output is the one
    ``cryptography`` itself produces, written straight to a file, never to
    stdout/stderr/a log record (``tests/test_promptwitness_signing.py``'s
    ``TestKeygenNeverLeaksKeyMaterial`` captures all three and asserts
    neither banner appears in any of them).

    Opened with ``O_CREAT | O_EXCL`` plus an explicit ``0o600`` mode (and a
    belt-and-suspenders ``os.chmod`` afterward, since the ``os.open`` mode
    argument is still subject to the process umask on POSIX) -- refuses to
    silently overwrite an existing key. Returns a provider already bound to
    the new file so a caller (the ``keygen`` CLI command) can report the new
    key's id without a second disk read.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    path = Path(key_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    private_key = Ed25519PrivateKey.generate()
    pem_bytes = private_key.private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
    )
    # Written to a sibling temp file and then `os.replace`d into position,
    # never generated on top of the target. `replace=True` (the `keygen
    # --force` path) would otherwise have to unlink the old key FIRST, and a
    # failure between the unlink and a successful write destroys it -- for a
    # signing key that is unrecoverable: every line it ever signed becomes
    # permanently unverifiable, and unlike most data loss there is no copy
    # anywhere to restore from. `os.replace` is atomic on POSIX and on
    # Windows, so there is no instant at which the key is absent. The temp
    # file is a sibling so the rename stays within one filesystem.
    tmp_path = path.with_name(f"{path.name}.new-{os.getpid()}")
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        try:
            os.write(fd, pem_bytes)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(tmp_path, 0o600)
        if replace:
            os.replace(tmp_path, path)
        else:
            # Preserve the refuse-to-overwrite contract: `link` fails with
            # `FileExistsError` if the target exists, and never clobbers.
            os.link(tmp_path, path)
            os.unlink(tmp_path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
    os.chmod(path, 0o600)
    del private_key, pem_bytes
    return SoftwareFileKeyProvider(path)


# ---------------------------------------------------------------------------
# "os-keystore" adapter (athenaeum#2152)
# ---------------------------------------------------------------------------

#: The 32-byte Ed25519 seed is what goes into the keystore, base64 (ASCII)
#: so all three backends can treat it as a text secret -- the macOS and
#: Secret Service command-line tools are line-oriented, and DPAPI does not
#: care either way. The seed, not a PKCS8 PEM: it is the smallest complete
#: representation, and keeping it out of PEM form means this module never
#: builds a ``BEGIN``/``END`` banner pair anywhere.
_OS_KEYSTORE_ALGORITHM = "ed25519"

#: Keychain / Secret Service item coordinates. ``service`` identifies the
#: application, ``account`` namespaces by harness, so two harnesses on one
#: machine get two keys rather than silently sharing one.
_KEYSTORE_SERVICE_DEFAULT = "promptwitness-signing-key"

#: Default subprocess budget, deliberately equal to
#: :func:`ledger.append_line`'s own ``sign_timeout`` default. A keystore that
#: needs longer than the ledger is willing to wait has already lost the race:
#: the ledger abandons the signing thread and degrades the line to unsigned,
#: so a larger budget here would only keep a doomed child process alive.
_KEYSTORE_TIMEOUT_DEFAULT = 2.0

#: Backend names, as accepted by ``PROMPTWITNESS_KEYSTORE_BACKEND`` and
#: :func:`resolve_keystore_backend`'s ``backend=`` argument.
KEYSTORE_BACKENDS: tuple[str, ...] = (
    "macos-keychain",
    "secret-service",
    "windows-dpapi",
)


class KeystoreItemAbsent(ledger.KeyUnavailable):
    """The keystore answered, and it holds no signing key item.

    A SUBCLASS of :class:`ledger.KeyUnavailable`, so every degrade path keeps
    treating it as "cannot sign right now" with no special case. What it adds
    is the distinction :func:`generate_and_store_keystore_key` needs: "there
    is nothing here" is safe to write over, while "I could not tell you what
    is here" (a locked keychain, a timeout, a tool that is not installed, a
    blob this user cannot decrypt) is not. Collapsing the two is how a
    `keygen` without ``--force`` would overwrite a live signing key during a
    transient keystore failure -- and every line that key ever signed becomes
    permanently unverifiable, with no copy anywhere to restore from. Reported
    on the ledger line as ``key.error: "KeystoreItemAbsent"``, which also
    tells an operator "you never created a key" rather than "something is
    wrong".
    """


@runtime_checkable
class KeystoreBackend(Protocol):
    """One OS keystore, reduced to "put a secret in" / "get it back out".

    Deliberately NOT a key provider: a backend knows nothing about Ed25519,
    signing, or the strength vocabulary. :class:`OsKeystoreKeyProvider`
    holds the cryptography and treats the backend as dumb storage, which is
    what lets all three platforms share one provider and one test for the
    signing behaviour.

    A backend raises :class:`ledger.KeyUnavailable` when the keystore is
    absent, locked, slow, or simply has no item -- and logs NOTHING. The
    single process-wide warning is the provider's job (see
    :meth:`OsKeystoreKeyProvider._seed`), because
    :func:`generate_and_store_keystore_key` probes for an existing secret on
    purpose and must not emit a warning for a miss it expects.
    """

    def name(self) -> str: ...

    def store(self, secret: bytes) -> None: ...

    def load(self) -> bytes: ...


def _run_keystore_tool(
    argv: list[str],
    *,
    input_bytes: bytes | None,
    timeout: float,
    backend: str,
    action: str,
    absent_returncodes: tuple[int, ...] = (),
) -> bytes:
    """Run *argv*, feeding *input_bytes* on **stdin**, and return stdout.

    The one place this module shells out, so the "no secret on argv" rule
    has exactly one enforcement point. Every caller passes key material as
    *input_bytes* and never inside *argv*; ``tests/
    test_promptwitness_signing.py``'s ``TestKeystoreArgvHygiene`` asserts
    that by inspecting the real ``subprocess`` boundary, not this function's
    signature.

    Why stdin rather than a temp file: argv is world-readable on every
    platform (``ps``, ``/proc/<pid>/cmdline``), and a temp file would put
    the seed back on disk unencrypted, which is the exact thing the
    ``os-keystore`` tier exists to stop. A pipe is readable only by the two
    processes on its ends.

    Any failure -- tool missing, non-zero exit, timeout -- becomes
    :class:`ledger.KeyUnavailable`, except an exit code in
    *absent_returncodes*, which is the tool's way of saying "no such item"
    and becomes :class:`KeystoreItemAbsent`. Nothing here logs: see
    :class:`KeystoreBackend`.

    ``start_new_session=True`` puts the child in its own session with NO
    controlling terminal. Two reasons, and the first is not cosmetic: a
    keystore tool that wants a passphrase reaches for ``/dev/tty`` rather
    than stdin, so a child that inherits the operator's terminal can sit at
    a prompt until the timeout fires. Detached, it cannot -- it either reads
    the pipe or fails cleanly. The second is that a per-prompt hook must
    never be able to grab the terminal out from under the session it is
    observing.
    """
    try:
        # Fixed argv, never a shell, and the secret rides on stdin only.
        completed = subprocess.run(
            argv,
            input=input_bytes if input_bytes is not None else b"",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise ledger.KeyUnavailable(
            f"{backend}: {argv[0]} is not installed (cannot {action} the signing key)"
        ) from exc
    except OSError as exc:
        raise ledger.KeyUnavailable(f"{backend}: cannot run {argv[0]} ({exc})") from exc
    except subprocess.TimeoutExpired as exc:
        raise ledger.KeyUnavailable(
            f"{backend}: {argv[0]} exceeded {timeout}s trying to {action} the signing key"
        ) from exc
    if completed.returncode in absent_returncodes:
        raise KeystoreItemAbsent(f"{backend}: no signing key item found")
    if completed.returncode != 0:
        # stderr is NOT included: a keystore tool echoes back the arguments
        # it was given, and while this module never puts the secret there,
        # a message built from a child's output is not ours to vouch for.
        raise ledger.KeyUnavailable(
            f"{backend}: {argv[0]} exited {completed.returncode} trying to {action} "
            "the signing key"
        )
    return bytes(completed.stdout)


class MacOsKeychainBackend:
    """macOS Keychain, through ``security(1)``.

    **The store goes through ``security -i``, and that is load-bearing.** The
    obvious shape, ``add-generic-password ... -w <secret>``, is the recipe
    every macOS note writes down and it puts the seed on argv, which this
    tier exists to avoid. The next obvious shape, a valueless ``-w`` with the
    secret on stdin, trades that for a worse problem: a valueless ``-w``
    makes ``security`` PROMPT, and a passphrase prompt reads ``/dev/tty``,
    not stdin -- so against a terminal it blocks until the timeout and
    against a pipe its behaviour is not something this module should be
    betting a signing key on.

    ``security -i`` reads *commands* from stdin until EOF. So the command
    text carries the known-good ``-w <value>`` form, no prompt is involved,
    and the secret still never touches argv: it arrives over a pipe,
    invisible to ``ps`` and ``/proc/<pid>/cmdline``. ``-U`` updates an
    existing item rather than failing on a duplicate.

    Because interactive mode's exit code is not a contract this module can
    lean on, :func:`generate_and_store_keystore_key` reads the item back and
    compares it after every store -- which is the right discipline for all
    three backends anyway, and is what turns "did the write land?" from an
    assumption into a checked fact.

    The read stays a direct ``find-generic-password -w`` invocation (the
    secret is only ever on the way OUT there, and exit 44 is
    ``errSecItemNotFound`` -- a genuinely absent item, distinct from a
    keystore that could not answer).
    """

    def __init__(
        self,
        *,
        service: str = _KEYSTORE_SERVICE_DEFAULT,
        account: str,
        timeout: float = _KEYSTORE_TIMEOUT_DEFAULT,
    ) -> None:
        self.service = service
        self.account = account
        self.timeout = timeout

    def name(self) -> str:
        return "macos-keychain"

    def store(self, secret: bytes) -> None:
        # The command text (secret included) travels on stdin, never argv.
        # Base64 needs no quoting -- `+`, `/` and `=` are not special to
        # `security`'s own argument splitter, and there is no whitespace.
        command = (
            b"add-generic-password -a "
            + self.account.encode("utf-8")
            + b" -s "
            + self.service.encode("utf-8")
            + b" -U -w "
            + secret
            + b"\n"
        )
        _run_keystore_tool(
            ["security", "-i"],
            input_bytes=command,
            timeout=self.timeout,
            backend=self.name(),
            action="store",
        )

    def load(self) -> bytes:
        out = _run_keystore_tool(
            [
                "security",
                "find-generic-password",
                "-a",
                self.account,
                "-s",
                self.service,
                "-w",
            ],
            input_bytes=None,
            timeout=self.timeout,
            backend=self.name(),
            action="read",
            # 44 is `errSecItemNotFound`.
            absent_returncodes=(44,),
        )
        secret = out.strip()
        if not secret:
            raise KeystoreItemAbsent(f"{self.name()}: no signing key item found")
        return secret


class SecretServiceBackend:
    """Linux Secret Service (gnome-keyring, KWallet, …) through ``secret-tool``.

    ``secret-tool store`` reads the secret from stdin by design; the
    attribute pairs on argv are lookup coordinates, never key material.
    """

    def __init__(
        self,
        *,
        service: str = _KEYSTORE_SERVICE_DEFAULT,
        account: str,
        timeout: float = _KEYSTORE_TIMEOUT_DEFAULT,
    ) -> None:
        self.service = service
        self.account = account
        self.timeout = timeout

    def name(self) -> str:
        return "secret-service"

    def store(self, secret: bytes) -> None:
        _run_keystore_tool(
            [
                "secret-tool",
                "store",
                "--label",
                f"{self.service} ({self.account})",
                "service",
                self.service,
                "account",
                self.account,
            ],
            # No trailing newline: `secret-tool` stores stdin verbatim, and
            # `lookup` would hand the newline straight back.
            input_bytes=secret,
            timeout=self.timeout,
            backend=self.name(),
            action="store",
        )

    def load(self) -> bytes:
        out = _run_keystore_tool(
            [
                "secret-tool",
                "lookup",
                "service",
                self.service,
                "account",
                self.account,
            ],
            input_bytes=None,
            timeout=self.timeout,
            backend=self.name(),
            action="read",
            # libsecret exits 1 from `lookup` when nothing matches.
            absent_returncodes=(1,),
        )
        secret = out.strip()
        if not secret:
            # Older `secret-tool lookup` exits 0 with empty output when
            # nothing matches, so a zero exit is not evidence of an item.
            raise KeystoreItemAbsent(f"{self.name()}: no signing key item found")
        return secret


def _dpapi_libraries() -> tuple[Any, Any]:  # pragma: no cover - Windows only
    """``(crypt32, kernel32)`` via ``ctypes.windll``.

    The seam the DPAPI tests replace. Everything else in
    :func:`_dpapi_transform` -- the ``DATA_BLOB`` marshalling, the out-param
    read, the ``LocalFree`` -- runs for real against the fakes, so the
    marshalling is covered on a non-Windows host; only this three-line
    platform binding is not.
    """
    import ctypes

    windll = ctypes.windll  # type: ignore[attr-defined]  # Windows-only attribute
    return windll.crypt32, windll.kernel32


def _dpapi_transform(data: bytes, *, encrypt: bool) -> bytes:
    """``CryptProtectData`` / ``CryptUnprotectData`` over *data*.

    Both calls take the same argument shape, so one function covers both
    directions. ``LocalFree`` is called on the output blob in a ``finally``:
    DPAPI allocates it with ``LocalAlloc`` and the caller owns it, and the
    one being leaked would be the DECRYPTED seed sitting in this process's
    heap until exit.
    """
    import ctypes

    class _DataBlob(ctypes.Structure):
        _fields_ = (
            ("cbData", ctypes.c_uint32),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        )

    crypt32, kernel32 = _dpapi_libraries()
    entry_point = "CryptProtectData" if encrypt else "CryptUnprotectData"
    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = _DataBlob(
        len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))
    )
    blob_out = _DataBlob()
    description = ctypes.c_wchar_p("promptwitness signing key") if encrypt else None
    #: `CRYPTPROTECT_UI_FORBIDDEN` (0x1): never put a UI prompt in front of a
    #: prompt hook. A key that needs a dialog is an unavailable key here.
    ok = getattr(crypt32, entry_point)(
        ctypes.byref(blob_in),
        description,
        None,
        None,
        None,
        0x1,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise ledger.KeyUnavailable(f"windows-dpapi: {entry_point} returned failure")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


#: ``O_BINARY`` is Windows-only and absent elsewhere, hence the ``getattr``.
#: Without it the CRT opens the descriptor in TEXT mode and ``os.write``
#: translates every ``0x0A`` in the DPAPI ciphertext into ``0x0D 0x0A``, so
#: the blob that comes back off disk is not the blob that was protected and
#: ``CryptUnprotectData`` fails -- on the one platform the backend exists
#: for, and only for ciphertext that happens to contain a newline byte.
_DPAPI_BLOB_OPEN_FLAGS = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
)


class WindowsDpapiBackend:
    """Windows DPAPI: the seed is encrypted with ``CryptProtectData`` and the
    protected blob is kept in a file.

    DPAPI has no store of its own -- it hands back ciphertext bound to the
    user account, and where that lives is the caller's problem. So unlike
    the other two backends this one owns a file path; what sits on disk is
    the protected blob, never the seed, and nothing is ever passed on argv
    because nothing is ever executed.
    """

    def __init__(self, *, blob_path: str | os.PathLike[str]) -> None:
        self.blob_path = Path(blob_path)

    def name(self) -> str:
        return "windows-dpapi"

    def store(self, secret: bytes) -> None:
        protected = _dpapi_transform(secret, encrypt=True)
        path = self.blob_path
        path.parent.mkdir(parents=True, exist_ok=True)
        # Same atomic-replace discipline as `generate_and_write_key`: a
        # sibling temp file at 0600, fsynced, then `os.replace`d, so an
        # interrupted write can never leave a truncated blob in position --
        # which for a signing key means every line it signed becomes
        # unverifiable, with no copy anywhere to restore from.
        tmp_path = path.with_name(f"{path.name}.new-{os.getpid()}")
        fd = os.open(str(tmp_path), _DPAPI_BLOB_OPEN_FLAGS, 0o600)
        try:
            try:
                os.write(fd, protected)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise
        os.chmod(path, 0o600)

    def load(self) -> bytes:
        try:
            protected = self.blob_path.read_bytes()
        except FileNotFoundError as exc:
            # No file is the one unambiguous "there is no key here".
            raise KeystoreItemAbsent(
                f"{self.name()}: no protected blob at {self.blob_path}"
            ) from exc
        except OSError as exc:
            # A permission error or an I/O fault is NOT evidence of absence;
            # overwriting on the strength of it would destroy a live key.
            raise ledger.KeyUnavailable(
                f"{self.name()}: protected blob unreadable at {self.blob_path}"
            ) from exc
        if not protected:
            raise KeystoreItemAbsent(
                f"{self.name()}: protected blob at {self.blob_path} is empty"
            )
        # A blob that will not decrypt stays a plain KeyUnavailable: it is
        # most likely a real key belonging to another user profile, which is
        # exactly what must not be overwritten.
        return _dpapi_transform(protected, encrypt=False).strip()


def resolve_keystore_blob_path(
    harness: str,
    *,
    override: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the Windows DPAPI protected-blob path for *harness*.

    Precedence mirrors :func:`resolve_key_path`: ``override`` argument >
    ``PROMPTWITNESS_KEYSTORE_BLOB_PATH`` env > ``<ledger_dir>/keys/
    ed25519.dpapi``. Built from :func:`ledger.resolve_ledger_dir`, never a
    home-directory literal.
    """
    if override is not None:
        return Path(override).expanduser()
    environ = env if env is not None else os.environ
    env_path = environ.get("PROMPTWITNESS_KEYSTORE_BLOB_PATH")
    if env_path:
        return Path(env_path).expanduser()
    return ledger.resolve_ledger_dir(harness) / "keys" / "ed25519.dpapi"


def _default_keystore_backend_name(platform: str) -> str:
    """The backend this platform implies.

    ``darwin`` and the Windows platform strings are recognized explicitly;
    everything else falls through to Secret Service, which is the right
    answer for Linux and the right FAILURE for anything else -- an absent
    ``secret-tool`` degrades to an unsigned line with one warning, which
    beats raising on an unrecognized ``sys.platform``.
    """
    if platform == "darwin":
        return "macos-keychain"
    if platform.startswith("win") or platform == "cygwin":
        return "windows-dpapi"
    return "secret-service"


def resolve_keystore_backend(
    harness: str,
    *,
    backend: str | None = None,
    service: str | None = None,
    account: str | None = None,
    blob_path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
    timeout: float = _KEYSTORE_TIMEOUT_DEFAULT,
) -> KeystoreBackend:
    """Build the :class:`KeystoreBackend` for *harness*.

    Backend precedence: ``backend`` argument >
    ``PROMPTWITNESS_KEYSTORE_BACKEND`` env > :data:`sys.platform`. The item
    coordinates follow the same shape: ``service``/``account`` arguments >
    ``PROMPTWITNESS_KEYSTORE_SERVICE``/``PROMPTWITNESS_KEYSTORE_ACCOUNT`` env
    > :data:`_KEYSTORE_SERVICE_DEFAULT` and *harness*. Namespacing the
    account by harness means two harnesses on one machine hold two keys, so
    a ledger's ``key.id`` stays stable per harness.

    Raises :class:`ValueError` for an unknown backend name -- a typo in an
    env var should say so, not silently fall back to a different keystore.
    """
    environ = env if env is not None else os.environ
    name = backend if backend is not None else environ.get("PROMPTWITNESS_KEYSTORE_BACKEND")
    name = name or _default_keystore_backend_name(
        platform if platform is not None else sys.platform
    )
    if name not in KEYSTORE_BACKENDS:
        raise ValueError(
            f"unknown PROMPTWITNESS_KEYSTORE_BACKEND {name!r}; known backends are "
            f"{', '.join(KEYSTORE_BACKENDS)}"
        )
    if name == "windows-dpapi":
        return WindowsDpapiBackend(
            blob_path=resolve_keystore_blob_path(harness, override=blob_path, env=environ)
        )
    resolved_service = (
        service
        if service is not None
        else environ.get("PROMPTWITNESS_KEYSTORE_SERVICE") or _KEYSTORE_SERVICE_DEFAULT
    )
    resolved_account = (
        account
        if account is not None
        else environ.get("PROMPTWITNESS_KEYSTORE_ACCOUNT") or harness
    )
    if name == "macos-keychain":
        return MacOsKeychainBackend(
            service=resolved_service, account=resolved_account, timeout=timeout
        )
    return SecretServiceBackend(
        service=resolved_service, account=resolved_account, timeout=timeout
    )


class OsKeystoreKeyProvider:
    """Ed25519 seed held in an OS keystore; signing happens in THIS process.

    **What this tier protects, and what it does not.** The seed is encrypted
    at rest by the OS, so a stolen disk or a backup does not yield a usable
    signing key -- and that is the whole of it. The user's own session can
    read the item back without a prompt (that is what makes it free of
    per-turn friction), so any process running as the same user can ask for
    a signature exactly as the hook does. ``presence_required()`` is
    therefore ``False``: nothing here proves a human was present. Only the
    hardware tiers change that picture.

    The seed is read from the keystore fresh on every call that needs it and
    never cached on the instance, the same discipline
    :class:`SoftwareFileKeyProvider` follows for its key file. One appended
    line therefore costs two keystore reads (``key_id`` then ``sign``);
    both are local IPC, and a keystore slow enough for that to matter is one
    the ledger's 2s ``sign_timeout`` was going to abandon anyway.
    """

    def __init__(self, backend: KeystoreBackend) -> None:
        self.backend = backend

    def _seed(self) -> bytes:
        """The raw 32-byte Ed25519 seed, or :class:`ledger.KeyUnavailable`.

        The one place the process-wide warning is emitted (shared with
        :class:`SoftwareFileKeyProvider` through
        :func:`_warn_key_unavailable_once`, so an "unavailable keystore
        produces one warning" guarantee holds for the process, not per
        provider instance or per appended line).
        """
        try:
            encoded = self.backend.load()
        except ledger.KeyUnavailable as exc:
            _warn_key_unavailable_once(
                f"promptwitness: the {self.backend.name()} signing key is unavailable "
                f"({exc}) -- signing lines unsigned for the rest of this process"
            )
            raise
        try:
            seed = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            _warn_key_unavailable_once(
                f"promptwitness: the {self.backend.name()} signing key item does not "
                "decode as base64 -- signing lines unsigned for the rest of this process"
            )
            raise ledger.KeyUnavailable(
                f"{self.backend.name()}: signing key item is not base64"
            ) from exc
        if len(seed) != 32:
            _warn_key_unavailable_once(
                f"promptwitness: the {self.backend.name()} signing key item is "
                f"{len(seed)} bytes, not a 32-byte Ed25519 seed -- signing lines "
                "unsigned for the rest of this process"
            )
            raise ledger.KeyUnavailable(
                f"{self.backend.name()}: signing key item is {len(seed)} bytes, "
                "not a 32-byte Ed25519 seed"
            )
        return seed

    def _load_private_key(self) -> object:
        seed = self._seed()
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        except ImportError as exc:
            _warn_key_unavailable_once(
                "promptwitness: the 'cryptography' package is not installed "
                "(install the 'witness-sign' extra) -- signing lines unsigned for "
                "the rest of this process"
            )
            raise ledger.KeyUnavailable("cryptography is not installed") from exc
        return Ed25519PrivateKey.from_private_bytes(seed)

    def _public_bytes(self) -> bytes:
        private_key = self._load_private_key()
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        return private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)  # type: ignore[attr-defined]

    def key_id(self) -> str:
        return derive_key_id(self._public_bytes(), _OS_KEYSTORE_ALGORITHM)

    def algorithm(self) -> str:
        return _OS_KEYSTORE_ALGORITHM

    def strength(self) -> str:
        return "os-keystore"

    def presence_required(self) -> bool:
        return False

    def sign(self, payload: bytes) -> bytes:
        private_key = self._load_private_key()
        return bytes(private_key.sign(payload))  # type: ignore[attr-defined]

    def public_key(self) -> bytes:
        return self._public_bytes()


def generate_and_store_keystore_key(
    backend: KeystoreBackend, *, replace: bool = False
) -> OsKeystoreKeyProvider:
    """Generate a fresh Ed25519 key and put its seed in *backend*.

    Refuses to overwrite an existing item unless *replace* is true, for the
    reason :func:`generate_and_write_key` refuses: replacing a signing key
    makes every line it ever signed permanently unverifiable, and there is
    no copy anywhere to restore from.

    **Only :class:`KeystoreItemAbsent` counts as "nothing to lose".** Any
    other :class:`ledger.KeyUnavailable` from the probe -- a locked keychain,
    a timeout, a tool that is not installed, a blob this user cannot decrypt
    -- means the keystore could not say what it holds, and is re-raised.
    Treating "I cannot tell" as "it is empty" is how a transient failure
    turns into an unrecoverable one.

    **The store is verified by reading it back.** `security -i`'s per-command
    exit status is not a contract this module leans on, and the same question
    ("did the write actually land?") applies to every backend; comparing the
    item against what was just written answers it rather than assuming it. A
    mismatch raises :class:`ledger.KeyUnavailable` instead of returning a
    provider that cannot sign.

    The probe deliberately calls the backend directly rather than through
    :meth:`OsKeystoreKeyProvider._seed` -- a miss here is the EXPECTED
    outcome and must not emit the process-wide "key unavailable" warning.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    if not replace:
        try:
            existing = backend.load()
        except KeystoreItemAbsent:
            existing = b""
        if existing:
            raise FileExistsError(
                f"a {backend.name()} signing key already exists; refusing to replace it"
            )
    private_key = Ed25519PrivateKey.generate()
    seed = private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    encoded = base64.b64encode(seed)
    try:
        backend.store(encoded)
        try:
            read_back = backend.load()
        except ledger.KeyUnavailable as exc:
            # Including KeystoreItemAbsent: the item was just written, so
            # "there is nothing here" means the write did not land, and
            # attributing it to absence would send the operator looking in
            # the wrong place.
            raise ledger.KeyUnavailable(
                f"{backend.name()}: the signing key did not read back as stored ({exc})"
            ) from exc
        if read_back != encoded:
            raise ledger.KeyUnavailable(
                f"{backend.name()}: the signing key did not read back as stored"
            )
    finally:
        del seed, encoded, private_key
    return OsKeystoreKeyProvider(backend)


# ---------------------------------------------------------------------------
# Selection (env / override)
# ---------------------------------------------------------------------------

_DEFAULT_PROVIDER = "none"


def resolve_key_path(
    harness: str,
    *,
    override: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the ``software-file`` key path for *harness*.

    Precedence: ``override`` argument > ``PROMPTWITNESS_KEY_PATH`` env >
    ``<ledger_dir>/keys/ed25519.pem`` under :func:`ledger.resolve_ledger_dir`
    for *harness* -- mirrors ``resolve_ledger_dir``'s own override-arg >
    env > computed-default precedence. Built from
    :func:`ledger.resolve_ledger_dir` / :data:`os.environ`, never from a
    hardcoded home-directory literal (the public-safe-lint gate rejects
    that shape even in test fixtures).
    """
    if override is not None:
        return Path(override).expanduser()
    environ = env if env is not None else os.environ
    env_path = environ.get("PROMPTWITNESS_KEY_PATH")
    if env_path:
        return Path(env_path).expanduser()
    return ledger.resolve_ledger_dir(harness) / "keys" / "ed25519.pem"


def resolve_key_provider(
    harness: str,
    *,
    provider: str | None = None,
    key_path: str | os.PathLike[str] | None = None,
    keystore_backend: str | None = None,
    env: Mapping[str, str] | None = None,
) -> ledger.KeyProviderLike | None:
    """Resolve a key provider from config, the ``PROMPTWITNESS_KEY_PROVIDER``
    / ``PROMPTWITNESS_KEY_PATH`` env precedent :func:`ledger.resolve_ledger_dir`
    sets for ``PROMPTWITNESS_DIR``.

    Precedence: ``provider`` argument > ``PROMPTWITNESS_KEY_PROVIDER`` env >
    default ``"none"``. Returns ``None`` for ``"none"`` -- not a
    :class:`NoneKeyProvider` instance -- so the caller can hand the result
    straight to :func:`ledger.append_line`'s ``key_provider=`` parameter and
    get exactly today's unsigned-line behaviour with zero extra code path.

    ``"os-keystore"`` builds an :class:`OsKeystoreKeyProvider` over the
    backend :func:`resolve_keystore_backend` picks for this platform. It is
    constructed WITHOUT touching the keystore: resolution must not fail, or
    even stall, because a keystore is locked -- an unavailable keystore
    shows up later as an unsigned line plus one warning, which is the whole
    point of the degrade path.

    Raises :class:`ValueError` for any other unrecognized provider name
    (``hardware-nonexportable``/``hardware-presence`` are real vocabulary
    entries but have no adapter yet — athenaeum#2153, athenaeum#2154 add
    those).
    """
    environ = env if env is not None else os.environ
    name = provider if provider is not None else environ.get("PROMPTWITNESS_KEY_PROVIDER")
    name = name or _DEFAULT_PROVIDER
    if name == "none":
        return None
    if name == "software-file":
        return SoftwareFileKeyProvider(resolve_key_path(harness, override=key_path, env=environ))
    if name == "os-keystore":
        return OsKeystoreKeyProvider(
            resolve_keystore_backend(harness, backend=keystore_backend, env=environ)
        )
    raise ValueError(
        f"unknown PROMPTWITNESS_KEY_PROVIDER {name!r}; shipped adapters are "
        "'none', 'software-file' and 'os-keystore'"
    )


# ---------------------------------------------------------------------------
# Key rotation — a line signed by both the old and the new key
# ---------------------------------------------------------------------------


def append_key_rotate_line(
    ledger_dir: str | os.PathLike[str],
    *,
    old_provider: ledger.KeyProviderLike,
    new_provider: ledger.KeyProviderLike,
    harness: str,
    harness_version: str,
    hook_version: str,
    ts: str | None = None,
    sign_timeout: float = 2.0,
) -> ledger.WitnessLine:
    """Append a ``kind="key-rotate"`` line signed by both *old_provider* and
    *new_provider*.

    **Reversible default (recorded for the lane report).** ``append_line``
    takes exactly one ``key_provider`` -- the primary signer, whose claim
    goes in the standard ``key``/``sig`` fields exactly like any other line
    (so every existing reader that only knows about ``key``/``sig`` still
    sees a validly-signed line, by the NEW key). The old key's claim rides
    in two new, additive-only fields threaded through ``extra`` and the new
    ``extra_signers`` parameter on :func:`ledger.append_line`:
    ``prior_key`` (the old key's ``KeyInfo``, merged into the line via
    ``extra`` so it is covered by the NEW key's own signature -- the old
    custody claim cannot be forged onto an unrelated line) and ``prior_sig``
    (the OLD key's signature, added by ``extra_signers={"prior_sig":
    old_provider}``, over the exact same canonical payload the new key
    signs). Both signatures therefore cover byte-identical bytes; see
    :func:`verify_key_rotate_signatures`.

    This needed one small, additive extension to ``ledger.append_line``
    (a new ``extra_signers`` keyword, default ``None``, no effect on any
    existing caller) rather than a bespoke parallel append path, so the
    existing lock/tail/gap-repair machinery is reused unchanged.
    """
    old_key_info = ledger.KeyInfo(
        id=old_provider.key_id(),
        alg=old_provider.algorithm(),
        strength=old_provider.strength(),
        presence=old_provider.presence_required(),
    )
    return ledger.append_line(
        ledger_dir,
        kind="key-rotate",
        harness=harness,
        harness_version=harness_version,
        hook_version=hook_version,
        ts=ts,
        key_provider=new_provider,
        sign_timeout=sign_timeout,
        extra={"prior_key": old_key_info.to_dict()},
        extra_signers={"prior_sig": old_provider},
    )


def verify_key_rotate_signatures(
    line: ledger.WitnessLine,
    *,
    old_public_key: bytes,
    new_public_key: bytes,
) -> tuple[bool, bool]:
    """``(new_sig_ok, prior_sig_ok)`` for a ``kind="key-rotate"`` line.

    Both signatures cover exactly what :func:`promptwitness.ledger.signed_payload`
    defines -- the same function the appender and :func:`ledger.verify_chain`
    use, deliberately rather than a local field list. This verifier had its
    own copy of the rule, which omitted ``prior_sig_error``: when the prior
    signer FAILED, that field is written after the payload is serialized, so
    a local list would include it and the new key's signature would then fail
    to verify on a line that is perfectly valid. One definition, three
    callers. Returns ``(False, False)`` shapes gracefully (never raises) when
    a field is missing or a signature does not decode.
    """
    d = line.to_dict()
    payload = ledger.signed_payload(d)

    new_ok = False
    if line.sig is not None:
        try:
            new_ok = verify_signature(
                payload, base64.b64decode(line.sig), new_public_key, line.key.alg or ""
            )
        except (ValueError, TypeError):
            new_ok = False

    prior_ok = False
    prior_sig = d.get("prior_sig")
    prior_key = d.get("prior_key") or {}
    if prior_sig is not None:
        try:
            prior_ok = verify_signature(
                payload, base64.b64decode(prior_sig), old_public_key, prior_key.get("alg") or ""
            )
        except (ValueError, TypeError):
            prior_ok = False

    return new_ok, prior_ok
