# SPDX-License-Identifier: Apache-2.0
"""``KeyProvider`` protocol, the key-strength vocabulary, and the ``none`` /
``software-file`` signing adapters (athenaeum#2150).

Layering: L1.5 (depends on :mod:`promptwitness.canon` and
:mod:`promptwitness.ledger`, stdlib otherwise). Standard library only at
IMPORT time — no ``athenaeum`` import anywhere (enforced by
``tests/test_promptwitness_boundary.py``'s AST scan) and no ``cryptography``
import at module level either: every function that needs it imports it
locally, so a plain ``pip install athenaeum`` (no ``witness-sign`` extra)
can still import this module and use :class:`NoneKeyProvider` /
:func:`resolve_key_provider` with ``PROMPTWITNESS_KEY_PROVIDER=none``.

This module depends on :mod:`promptwitness.ledger` (one way only) so it can
build on :class:`ledger.KeyInfo` / :class:`ledger.KeyUnavailable` and extend
:func:`ledger.append_line`'s signing seam for key rotation. ``ledger.py``
must never import this module back — it stays usable with no key provider
at all, which is the whole point of the ``key_provider=None`` default
:func:`ledger.append_line` already supports.

**Key-strength vocabulary**, weakest to strongest: ``none`` <
``software-file`` < ``os-keystore`` < ``hardware-nonexportable`` <
``hardware-presence``. Only the first two have adapters here;
``os-keystore`` (athenaeum#2152) and the hardware adapters (athenaeum#2153,
athenaeum#2154) are separate issues. ``hardware-presence`` is a seam only —
operator ruling 1 builds no presence-gated adapter, so both adapters shipped
here return ``presence_required() == False``.

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
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, runtime_checkable

from promptwitness import ledger

__all__ = [
    "STRENGTHS",
    "KeyProvider",
    "NoneKeyProvider",
    "SoftwareFileKeyProvider",
    "derive_key_id",
    "verify_signature",
    "resolve_key_path",
    "resolve_key_provider",
    "generate_and_write_key",
    "append_key_rotate_line",
    "verify_key_rotate_signatures",
]

log = logging.getLogger(__name__)

#: Weakest to strongest (athenaeum#2144 epic body). Only ``none`` and
#: ``software-file`` have adapters in this issue.
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
    Raises :class:`ValueError` for any other unrecognized provider name
    (``os-keystore``/``hardware-nonexportable``/``hardware-presence`` are
    real vocabulary entries but have no adapter in this issue — athenaeum#2152,
    athenaeum#2153, athenaeum#2154 add those).
    """
    environ = env if env is not None else os.environ
    name = provider if provider is not None else environ.get("PROMPTWITNESS_KEY_PROVIDER")
    name = name or _DEFAULT_PROVIDER
    if name == "none":
        return None
    if name == "software-file":
        return SoftwareFileKeyProvider(resolve_key_path(harness, override=key_path, env=environ))
    raise ValueError(
        f"unknown PROMPTWITNESS_KEY_PROVIDER {name!r}; shipped adapters are "
        "'none' and 'software-file'"
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
