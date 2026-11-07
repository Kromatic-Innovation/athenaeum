# SPDX-License-Identifier: Apache-2.0
"""Tests for the TPM key adapter (athenaeum#2154).

Neither a real TPM nor a real Windows host is available here, so every
test below runs against a FAKE tool: real, executable scripts, run as
genuine child processes (never a mocked ``subprocess.run``, per AC1's own
"or mocked subprocess" being the FALLBACK, not the default -- real
executables are strictly better where they are cheap, per the issue
brief). The Linux fakes implement ``tpm2_createprimary``/``tpm2_create``/
``tpm2_evictcontrol``/``tpm2_readpublic``/``tpm2_sign`` well enough to
back a real ``cryptography`` P-256 key with a file-based "NV store"; the
fake ``powershell`` implements the same four verbs the real
``_POWERSHELL_SCRIPT`` would run, over a file-based per-``KeyName`` store.
Both fakes deliberately emit the RAW ``r || s`` signature form (never
DER), so a sign -> verify round trip here genuinely exercises
``tpm.py``'s own ``_raw_rs_signature_to_der`` conversion rather than
asserting it only in the abstract.

Every path builds filesystem paths from ``tmp_path`` and monkeypatched
env vars only -- never a hardcoded machine-specific home-directory
literal (the public-safe-lint gate rejects that shape even in test
fixtures).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from promptwitness import canon, ledger, signing, tpm


def _append_turn(ledger_dir: Path, provider: ledger.KeyProviderLike) -> ledger.WitnessLine:
    return ledger.append_line(
        ledger_dir,
        kind="turn",
        harness="claude-code",
        harness_version="1",
        hook_version="promptwitness 0.1.0",
        session="s1",
        turn=0,
        prompt_id="p0",
        prompt_kind="human",
        prompt_digest=canon.digest(canon.normalize("hi")),
        raw_digest=canon.digest("hi"),
        prompt_chars=2,
        channel="interactive",
        resumed=False,
        key_provider=provider,
    )


# ---------------------------------------------------------------------------
# Real, executable fake tpm2-tools (Linux)
# ---------------------------------------------------------------------------

_FAKE_ARG_HELPERS = '''
import os
import sys
from pathlib import Path

STORE = Path(os.environ["FAKE_TPM2_STORE"])


def _opt(argv, flag):
    return argv[argv.index(flag) + 1] if flag in argv else None


def _trailing_positional(argv, flags_with_value):
    i = 0
    positional = None
    while i < len(argv):
        if argv[i] in flags_with_value:
            i += 2
            continue
        positional = argv[i]
        i += 1
    return positional
'''

_FAKE_TPM2_CREATEPRIMARY = f"""#!{sys.executable}
{_FAKE_ARG_HELPERS}
argv = sys.argv[1:]
ctx = _opt(argv, "-c")
if ctx is None:
    sys.exit(64)
Path(ctx).write_bytes(b"fake-primary-ctx")
sys.exit(0)
"""

_FAKE_TPM2_CREATE = f"""#!{sys.executable}
{_FAKE_ARG_HELPERS}
from cryptography.hazmat.primitives.asymmetric import ec

argv = sys.argv[1:]
pub_file = _opt(argv, "-u")
priv_file = _opt(argv, "-r")
key_ctx = _opt(argv, "-c")
if None in (pub_file, priv_file, key_ctx):
    sys.exit(64)
priv = ec.generate_private_key(ec.SECP256R1())
raw = priv.private_numbers().private_value.to_bytes(32, "big")
Path(priv_file).write_bytes(raw)
Path(pub_file).write_bytes(b"fake-pub-blob")
# The fake "loaded key context" IS the raw scalar -- good enough for
# tpm2_evictcontrol to persist it, which is all this fake needs.
Path(key_ctx).write_bytes(raw)
sys.exit(0)
"""

_FAKE_TPM2_EVICTCONTROL = f"""#!{sys.executable}
{_FAKE_ARG_HELPERS}
argv = sys.argv[1:]
c_val = _opt(argv, "-c")
positional = _trailing_positional(argv, {{"-C", "-c"}})
if positional:
    # Persist mode: c_val is a key-context file, positional is the target handle.
    target = STORE / positional
    if target.exists():
        sys.stderr.write("handle already has a persistent object\\n")
        sys.exit(1)
    target.write_bytes(Path(c_val).read_bytes())
    sys.exit(0)
else:
    # Clear mode: c_val IS the handle to remove.
    target = STORE / c_val
    if not target.exists():
        sys.stderr.write("nothing to clear\\n")
        sys.exit(1)
    target.unlink()
    sys.exit(0)
"""

_FAKE_TPM2_READPUBLIC = f"""#!{sys.executable}
{_FAKE_ARG_HELPERS}
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

argv = sys.argv[1:]
handle = _opt(argv, "-c")
out = _opt(argv, "-o")
target = STORE / handle
if not target.exists():
    sys.stderr.write("handle not found\\n")
    sys.exit(1)
raw = target.read_bytes()
priv = ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())
pem = priv.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
)
Path(out).write_bytes(pem)
sys.exit(0)
"""

_FAKE_TPM2_SIGN = f"""#!{sys.executable}
{_FAKE_ARG_HELPERS}
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

argv = sys.argv[1:]
handle = _opt(argv, "-c")
sig_out = _opt(argv, "-o")
message_path = _trailing_positional(argv, {{"-c", "-g", "-f", "-o"}})
target = STORE / handle
if not target.exists():
    sys.stderr.write("handle not found\\n")
    sys.exit(1)
raw = target.read_bytes()
priv = ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())
payload = Path(message_path).read_bytes()
der_sig = priv.sign(payload, ec.ECDSA(hashes.SHA256()))
r, s = decode_dss_signature(der_sig)
# Plain/raw r||s -- deliberately NOT DER, so tpm.py's own conversion is
# genuinely exercised rather than assumed.
Path(sig_out).write_bytes(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
sys.exit(0)
"""

_FAKE_TPM2_TOOLS = {
    "tpm2_createprimary": _FAKE_TPM2_CREATEPRIMARY,
    "tpm2_create": _FAKE_TPM2_CREATE,
    "tpm2_evictcontrol": _FAKE_TPM2_EVICTCONTROL,
    "tpm2_readpublic": _FAKE_TPM2_READPUBLIC,
    "tpm2_sign": _FAKE_TPM2_SIGN,
}


def write_fake_tpm2_tools(bin_dir: Path) -> None:
    """Write every fake ``tpm2_*`` executable into *bin_dir*."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name, source in _FAKE_TPM2_TOOLS.items():
        path = bin_dir / name
        path.write_text(source, encoding="utf-8")
        path.chmod(0o700)


@pytest.fixture
def tpm2_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put the fake ``tpm2_*`` suite on ``PATH`` and give it a private
    file-based "NV store" directory. Returns the store directory (tests
    use it only to simulate a pre-existing/absent key by poking files
    directly, never to bypass the module under test for the happy path)."""
    bin_dir = tmp_path / "bin"
    write_fake_tpm2_tools(bin_dir)
    store_dir = tmp_path / "tpm-store"
    store_dir.mkdir()
    monkeypatch.setenv("FAKE_TPM2_STORE", str(store_dir))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return store_dir


def _hanging_tool(bin_dir: Path, name: str) -> None:
    """Overwrite one fake tool with a script that reads stdin forever --
    used to exercise the timeout path without relying on a mock."""
    path = bin_dir / name
    path.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n", encoding="utf-8")
    path.chmod(0o700)


# ---------------------------------------------------------------------------
# Real, executable fake "powershell" (Windows)
# ---------------------------------------------------------------------------

_FAKE_POWERSHELL_SOURCE = f"""#!{sys.executable}
# Fake 'powershell' stub for tests -- see
# tests/test_promptwitness_tpm.py for why. Mimics the
# WindowsPlatformCryptoBackend contract (base64 X9.63 pubkey, base64 raw
# r||s signature, exit 2 for "key not found") without a real CNG/TPM --
# backed by a real cryptography P-256 key, stored per -KeyName under
# FAKE_TPM_STORE_DIR.
import base64
import os
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_NOT_FOUND = 2
EXIT_USAGE = 64


def _parse(argv):
    action = None
    key_name = None
    force = False
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "-Action":
            i += 1
            action = argv[i]
        elif tok == "-KeyName":
            i += 1
            key_name = argv[i]
        elif tok == "-Force":
            force = True
        i += 1
    return action, key_name, force


def _store_path(key_name):
    return Path(os.environ["FAKE_TPM_STORE_DIR"]) / key_name


def _point_b64(raw_scalar):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    priv = ec.derive_private_key(int.from_bytes(raw_scalar, "big"), ec.SECP256R1())
    pub = priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return base64.b64encode(pub).decode("ascii")


def main():
    action, key_name, force = _parse(sys.argv[1:])
    if action is None or key_name is None:
        return EXIT_USAGE
    path = _store_path(key_name)

    if action == "create":
        if path.exists() and not force:
            sys.stderr.write("key already exists\\n")
            return EXIT_FAILURE
        from cryptography.hazmat.primitives.asymmetric import ec

        priv = ec.generate_private_key(ec.SECP256R1())
        raw = priv.private_numbers().private_value.to_bytes(32, "big")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        sys.stdout.write(_point_b64(raw))
        return EXIT_OK

    if not path.exists():
        sys.stderr.write("key not found\\n")
        return EXIT_NOT_FOUND
    raw = path.read_bytes()

    if action == "pubkey":
        sys.stdout.write(_point_b64(raw))
        return EXIT_OK

    if action == "sign":
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

        payload = sys.stdin.buffer.read()
        priv = ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())
        der_sig = priv.sign(payload, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der_sig)
        raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        sys.stdout.write(base64.b64encode(raw_sig).decode("ascii"))
        return EXIT_OK

    sys.stderr.write("unknown action\\n")
    return EXIT_USAGE


sys.exit(main())
"""


def write_fake_powershell(path: Path) -> None:
    path.write_text(_FAKE_POWERSHELL_SOURCE, encoding="utf-8")
    path.chmod(0o700)


@pytest.fixture
def fake_powershell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    store_dir = tmp_path / "cng-store"
    store_dir.mkdir()
    monkeypatch.setenv("FAKE_TPM_STORE_DIR", str(store_dir))
    ps_path = tmp_path / "fake-powershell"
    write_fake_powershell(ps_path)
    return ps_path


def _hanging_powershell(tmp_path: Path) -> Path:
    path = tmp_path / "fake-hanging-powershell"
    path.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n", encoding="utf-8")
    path.chmod(0o700)
    return path


# ---------------------------------------------------------------------------
# A recording proxy over the REAL subprocess module (argv hygiene)
# ---------------------------------------------------------------------------


@dataclass
class RecordedCall:
    argv: list[str]
    stdin: bytes | None
    kwargs: dict[str, Any] = field(default_factory=dict)


class RecordingSubprocess:
    """Forwards every call to the REAL ``subprocess`` module while
    recording it exactly as ``signing._run_keystore_tool`` made it --
    mirrors ``tests/test_promptwitness_secure_enclave.py``'s own proxy of
    the same name."""

    PIPE = subprocess.PIPE
    DEVNULL = subprocess.DEVNULL
    TimeoutExpired = subprocess.TimeoutExpired
    CalledProcessError = subprocess.CalledProcessError

    def __init__(self) -> None:
        self.calls: list[RecordedCall] = []

    def run(self, argv: list[str], **kwargs: Any) -> Any:
        self.calls.append(
            RecordedCall(argv=list(argv), stdin=kwargs.get("input"), kwargs=dict(kwargs))
        )
        return subprocess.run(argv, **kwargs)

    def every_argv_token(self) -> list[str]:
        return [token for call in self.calls for token in call.argv]


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> RecordingSubprocess:
    rec = RecordingSubprocess()
    monkeypatch.setattr(signing, "subprocess", rec)
    return rec


@pytest.fixture(autouse=True)
def _reset_warn_once() -> Any:
    """The "one warning per process" flag is module-global and shared with
    every other adapter in ``signing``, so every test here must start and
    end with it clear or the warning-count assertions would depend on test
    order across the whole suite."""
    signing.reset_warned_for_tests()
    yield
    signing.reset_warned_for_tests()


# ---------------------------------------------------------------------------
# Protocol shape and the strength vocabulary
# ---------------------------------------------------------------------------


class _StubBackend:
    """A minimal, in-process ``TpmBackend`` -- used only to test
    :class:`tpm.TpmKeyProvider`'s own logic (the X9.63 enforcement, the
    warning wrap) independently of either real backend's subprocess
    plumbing."""

    def __init__(self, *, pubkey: bytes = b"\x04" + b"\x00" * 64, fail: Exception | None = None):
        self._pubkey = pubkey
        self._fail = fail

    def name(self) -> str:
        return "stub"

    def create(self, *, force: bool = False) -> bytes:
        if self._fail:
            raise self._fail
        return self._pubkey

    def public_key(self) -> bytes:
        if self._fail:
            raise self._fail
        return self._pubkey

    def sign(self, payload: bytes) -> bytes:
        if self._fail:
            raise self._fail
        return b"\x00" * 8


class TestProtocolAndStrength:
    def test_provider_satisfies_both_protocols(self) -> None:
        provider = tpm.TpmKeyProvider(_StubBackend())
        assert isinstance(provider, signing.KeyProvider)
        assert isinstance(provider, ledger.KeyProviderLike)

    def test_backend_satisfies_tpm_backend_protocol(self) -> None:
        assert isinstance(_StubBackend(), tpm.TpmBackend)

    def test_strength_is_hardware_nonexportable(self) -> None:
        provider = tpm.TpmKeyProvider(_StubBackend())
        assert provider.strength() == "hardware-nonexportable"
        assert provider.strength() == tpm.STRENGTH
        assert provider.strength() in signing.STRENGTHS

    def test_presence_required_is_false(self) -> None:
        provider = tpm.TpmKeyProvider(_StubBackend())
        assert provider.presence_required() is False

    def test_algorithm_is_ecdsa_p256_sha256(self) -> None:
        provider = tpm.TpmKeyProvider(_StubBackend())
        assert provider.algorithm() == "ecdsa-p256-sha256"
        assert provider.algorithm() == tpm.ALGORITHM


class TestAlgorithmConstantParity:
    def test_matches_secure_enclave_and_signing_private_constant(self) -> None:
        from promptwitness import secure_enclave

        assert tpm.ALGORITHM == secure_enclave.ALGORITHM == signing._ECDSA_P256_ALGORITHM


# ---------------------------------------------------------------------------
# The wire-format negative control (the whole point of the module)
# ---------------------------------------------------------------------------


class TestWireFormatRegressionIsRefused:
    def test_untagged_64_byte_point_is_refused(self) -> None:
        backend = _StubBackend(pubkey=b"\x00" * 64)
        provider = tpm.TpmKeyProvider(backend)
        with pytest.raises(ledger.KeyUnavailable) as excinfo:
            provider.public_key()
        assert "64 bytes" in str(excinfo.value)
        assert not isinstance(excinfo.value, signing.KeystoreItemAbsent)

    def test_wrong_tag_byte_is_refused(self) -> None:
        backend = _StubBackend(pubkey=b"\x05" + b"\x00" * 64)
        provider = tpm.TpmKeyProvider(backend)
        with pytest.raises(ledger.KeyUnavailable):
            provider.public_key()

    def test_key_id_refuses_rather_than_returning_a_wrong_stable_value(self) -> None:
        backend = _StubBackend(pubkey=b"\x00" * 64)
        provider = tpm.TpmKeyProvider(backend)
        with pytest.raises(ledger.KeyUnavailable):
            provider.key_id()

    def test_the_negative_control_is_not_vacuous(self) -> None:
        """A floor on the control itself: a genuinely correct point must
        NOT be refused, so the refusal above is about the shape, not a
        bug that refuses everything."""
        good = b"\x04" + b"\x00" * 64
        provider = tpm.TpmKeyProvider(_StubBackend(pubkey=good))
        assert provider.public_key() == good


# ---------------------------------------------------------------------------
# Raw r||s -> DER conversion: the real work in this module
# ---------------------------------------------------------------------------


class TestNormalizeSignatureToDer:
    """Both accepted input shapes, and the rejection of anything else.

    `tpm2_sign -f plain`'s byte layout is undocumented and unverifiable
    without a TPM (ASSUMPTION 1 in `tpm.py`), so the module accepts either
    raw `r || s` or already-DER rather than betting on one. These tests
    are what make that claim true rather than aspirational.
    """

    def _key_and_der(self) -> tuple[object, bytes]:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec

        priv = ec.generate_private_key(ec.SECP256R1())
        return priv, priv.sign(b"payload", ec.ECDSA(hashes.SHA256()))

    def test_raw_rs_round_trips_through_der(self) -> None:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

        priv, der_sig = self._key_and_der()
        r, s = decode_dss_signature(der_sig)
        raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")

        converted = tpm._normalize_signature_to_der(raw, backend_name="test")
        assert converted == der_sig
        # And the converted DER signature genuinely verifies.
        priv.public_key().verify(  # type: ignore[attr-defined]
            converted, b"payload", ec.ECDSA(hashes.SHA256())
        )

    def test_already_der_passes_through_and_still_verifies(self) -> None:
        """The other half of the tolerance: if `tpm2_sign -f plain` turns
        out to emit DER, the adapter is correct with no change."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec

        priv, der_sig = self._key_and_der()
        converted = tpm._normalize_signature_to_der(der_sig, backend_name="test")
        assert converted == der_sig
        priv.public_key().verify(  # type: ignore[attr-defined]
            converted, b"payload", ec.ECDSA(hashes.SHA256())
        )

    def test_a_der_sequence_that_does_not_decode_is_refused(self) -> None:
        """A leading 0x30 is not enough -- the body is decoded, so a tool
        that mis-encodes an integer fails here rather than handing the
        ledger a signature that will never verify."""
        with pytest.raises(ledger.KeyUnavailable) as excinfo:
            tpm._normalize_signature_to_der(b"\x30\x07not-der", backend_name="test")
        assert "DER" in str(excinfo.value)

    def test_neither_shape_is_refused(self) -> None:
        with pytest.raises(ledger.KeyUnavailable) as excinfo:
            tpm._normalize_signature_to_der(b"\x01" * 10, backend_name="test")
        assert "neither" in str(excinfo.value)

    def test_all_zero_r_and_s_round_trips(self) -> None:
        """A boundary case: r=0, s=0 is not a valid signature but must not
        crash the conversion itself -- DER encoding of (0, 0) is well
        defined even though no real signature would ever be this."""
        converted = tpm._normalize_signature_to_der(b"\x00" * 64, backend_name="test")
        assert isinstance(converted, bytes)


# ---------------------------------------------------------------------------
# resolve_tpm_backend
# ---------------------------------------------------------------------------


class TestResolveTpmBackend:
    def test_linux_default_is_tpm2_tools(self) -> None:
        backend = tpm.resolve_tpm_backend("claude-code", env={}, platform="linux")
        assert isinstance(backend, tpm.Tpm2ToolsBackend)
        assert backend.handle == tpm._TPM2_HANDLE_DEFAULT

    def test_windows_default_is_platform_crypto(self) -> None:
        backend = tpm.resolve_tpm_backend("claude-code", env={}, platform="win32")
        assert isinstance(backend, tpm.WindowsPlatformCryptoBackend)
        assert backend.key_name == "promptwitness-tpm-claude-code"

    def test_cygwin_is_also_windows(self) -> None:
        backend = tpm.resolve_tpm_backend("claude-code", env={}, platform="cygwin")
        assert isinstance(backend, tpm.WindowsPlatformCryptoBackend)

    def test_backend_argument_wins_over_platform(self) -> None:
        backend = tpm.resolve_tpm_backend(
            "claude-code", backend="windows-platform-crypto", env={}, platform="linux"
        )
        assert isinstance(backend, tpm.WindowsPlatformCryptoBackend)

    def test_env_selects_backend(self) -> None:
        backend = tpm.resolve_tpm_backend(
            "claude-code",
            env={"PROMPTWITNESS_TPM_BACKEND": "windows-platform-crypto"},
            platform="linux",
        )
        assert isinstance(backend, tpm.WindowsPlatformCryptoBackend)

    def test_backend_argument_wins_over_env(self) -> None:
        backend = tpm.resolve_tpm_backend(
            "claude-code",
            backend="tpm2-tools",
            env={"PROMPTWITNESS_TPM_BACKEND": "windows-platform-crypto"},
            platform="linux",
        )
        assert isinstance(backend, tpm.Tpm2ToolsBackend)

    def test_unknown_backend_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown PROMPTWITNESS_TPM_BACKEND"):
            tpm.resolve_tpm_backend("claude-code", backend="quantum-dongle", env={})

    def test_key_id_argument_wins(self) -> None:
        backend = tpm.resolve_tpm_backend(
            "claude-code", backend="tpm2-tools", key_id="0x81020003", env={}
        )
        assert backend.handle == "0x81020003"

    def test_key_id_env_wins_over_default(self) -> None:
        backend = tpm.resolve_tpm_backend(
            "claude-code",
            backend="tpm2-tools",
            env={"PROMPTWITNESS_TPM_KEY_ID": "0x81030004"},
        )
        assert backend.handle == "0x81030004"

    def test_key_id_argument_wins_over_env(self) -> None:
        backend = tpm.resolve_tpm_backend(
            "claude-code",
            backend="tpm2-tools",
            key_id="0x81040005",
            env={"PROMPTWITNESS_TPM_KEY_ID": "0x81030004"},
        )
        assert backend.handle == "0x81040005"

    def test_windows_key_id_is_namespaced_by_harness(self) -> None:
        a = tpm.resolve_tpm_backend("harness-a", backend="windows-platform-crypto", env={})
        b = tpm.resolve_tpm_backend("harness-b", backend="windows-platform-crypto", env={})
        assert a.key_name != b.key_name

    def test_powershell_path_argument_wins(self, tmp_path: Path) -> None:
        backend = tpm.resolve_tpm_backend(
            "claude-code",
            backend="windows-platform-crypto",
            powershell_path=tmp_path / "pwsh",
            env={},
        )
        assert backend.powershell_path == tmp_path / "pwsh"

    def test_powershell_path_env_wins_over_default(self, tmp_path: Path) -> None:
        backend = tpm.resolve_tpm_backend(
            "claude-code",
            backend="windows-platform-crypto",
            env={"PROMPTWITNESS_TPM_POWERSHELL": str(tmp_path / "pwsh2")},
        )
        assert backend.powershell_path == str(tmp_path / "pwsh2")


class TestResolveKeyProviderSelectsTpm:
    def test_env_selects_tpm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PROMPTWITNESS_KEY_PROVIDER", "tpm")
        got = signing.resolve_key_provider("claude-code")
        assert isinstance(got, tpm.TpmKeyProvider)

    def test_argument_selects_tpm(self) -> None:
        got = signing.resolve_key_provider(
            "claude-code", provider="tpm", tpm_backend="tpm2-tools", env={}
        )
        assert isinstance(got, tpm.TpmKeyProvider)
        assert isinstance(got.backend, tpm.Tpm2ToolsBackend)

    def test_resolution_never_touches_a_tool(
        self, recorder: RecordingSubprocess
    ) -> None:
        """Resolving a provider must not stall or fail because the TPM is
        unavailable -- that shows up later as an unsigned line plus one
        warning."""
        signing.resolve_key_provider(
            "claude-code", provider="tpm", tpm_backend="tpm2-tools", env={}
        )
        assert recorder.calls == []

    def test_unknown_provider_error_message_lists_tpm(self) -> None:
        with pytest.raises(ValueError, match="'tpm'"):
            signing.resolve_key_provider("claude-code", provider="bogus", env={})


# ---------------------------------------------------------------------------
# Linux: happy path, over the real fake tpm2-tools suite
# ---------------------------------------------------------------------------


class TestLinuxGenerateSignVerifyRoundTrip:
    def test_sign_and_verify(self, tpm2_env: Path) -> None:
        backend = tpm.Tpm2ToolsBackend(handle="0x81010099")
        provider = tpm.generate_tpm_key(backend)
        signature = provider.sign(b"payload")
        assert signing.verify_signature(
            b"payload", signature, provider.public_key(), "ecdsa-p256-sha256"
        )
        assert not signing.verify_signature(
            b"other", signature, provider.public_key(), "ecdsa-p256-sha256"
        )

    def test_sign_and_verify_when_the_tool_emits_der_instead(
        self, tpm2_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other half of ASSUMPTION 1's neutralization, end to end.

        `tpm2_sign -f plain`'s byte layout is undocumented, so
        `_normalize_signature_to_der` accepts raw `r || s` OR DER. The
        default fake emits raw; this one emits DER, through the SAME real
        backend and the SAME real verifier -- so whichever encoding real
        hardware produces, there is a passing end-to-end test for it, not
        just a unit test of the helper.
        """
        sign_tool = tpm2_env.parent / "bin" / "tpm2_sign"
        source = sign_tool.read_text(encoding="utf-8")
        der_source = source.replace(
            'Path(sig_out).write_bytes(r.to_bytes(32, "big") + s.to_bytes(32, "big"))',
            "Path(sig_out).write_bytes(der_sig)",
        )
        assert der_source != source, "the DER-emitting patch did not apply"
        sign_tool.write_text(der_source, encoding="utf-8")
        sign_tool.chmod(0o700)

        provider = tpm.generate_tpm_key(tpm.Tpm2ToolsBackend(handle="0x810100a1"))
        signature = provider.sign(b"payload")
        assert signing.verify_signature(
            b"payload", signature, provider.public_key(), "ecdsa-p256-sha256"
        )
        assert not signing.verify_signature(
            b"other", signature, provider.public_key(), "ecdsa-p256-sha256"
        )

    def test_public_key_is_the_raw_x963_point(self, tpm2_env: Path) -> None:
        provider = tpm.generate_tpm_key(tpm.Tpm2ToolsBackend(handle="0x8101009a"))
        pub = provider.public_key()
        assert len(pub) == 65
        assert pub[0:1] == b"\x04"

    def test_key_id_is_stable_and_matches_derive_key_id(self, tpm2_env: Path) -> None:
        provider = tpm.generate_tpm_key(tpm.Tpm2ToolsBackend(handle="0x8101009b"))
        assert provider.key_id() == provider.key_id()
        assert provider.key_id() == signing.derive_key_id(
            provider.public_key(), "ecdsa-p256-sha256"
        )

    def test_key_id_matches_an_independently_computed_digest(self, tpm2_env: Path) -> None:
        provider = tpm.generate_tpm_key(tpm.Tpm2ToolsBackend(handle="0x8101009c"))
        expected = "ecdsa-p256-sha256:" + hashlib.sha256(provider.public_key()).hexdigest()[:16]
        assert provider.key_id() == expected

    def test_two_handles_produce_different_ids(self, tpm2_env: Path) -> None:
        a = tpm.generate_tpm_key(tpm.Tpm2ToolsBackend(handle="0x8101009d"))
        b = tpm.generate_tpm_key(tpm.Tpm2ToolsBackend(handle="0x8101009e"))
        assert a.key_id() != b.key_id()

    def test_round_trip_through_append_line_and_verify_chain(self, tpm2_env: Path) -> None:
        provider = tpm.generate_tpm_key(tpm.Tpm2ToolsBackend(handle="0x8101009f"))
        ledger_dir = tpm2_env.parent / "ledger"
        line = _append_turn(ledger_dir, provider)
        assert line.key.strength == "hardware-nonexportable"
        assert line.key.presence is False
        assert line.sig is not None
        result = ledger.verify_chain(
            ledger_dir, public_keys={provider.key_id(): provider.public_key()}
        )
        assert result.ok
        assert result.signed_lines == 1
        assert result.unverified_signed_lines == 0
        # The load-bearing assertion: `ok` reports the hash chain only, and
        # an unverifiable signature would land here, not silently pass.
        assert result.invalid_signature_seqs == ()

    def test_verify_chain_reports_a_tampered_tpm_line_as_invalid(self, tpm2_env: Path) -> None:
        """Positive control for the assertion above."""
        provider = tpm.generate_tpm_key(tpm.Tpm2ToolsBackend(handle="0x810100a0"))
        ledger_dir = tpm2_env.parent / "ledger"
        _append_turn(ledger_dir, provider)
        path = next(ledger_dir.rglob("*.jsonl"))
        raw = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        raw["prompt_chars"] = raw["prompt_chars"] + 1
        path.write_text(json.dumps(raw, sort_keys=True) + "\n", encoding="utf-8")
        result = ledger.verify_chain(
            ledger_dir, public_keys={provider.key_id(): provider.public_key()}
        )
        assert result.invalid_signature_seqs != ()


class TestLinuxFailureModes:
    def test_missing_tools_is_key_unavailable(self, tmp_path: Path) -> None:
        backend = tpm.Tpm2ToolsBackend(handle="0x81010001")
        with pytest.raises(ledger.KeyUnavailable):
            backend.public_key()

    def test_handle_not_found_is_the_absent_subclass(self, tpm2_env: Path) -> None:
        backend = tpm.Tpm2ToolsBackend(handle="0x810100b0")
        with pytest.raises(signing.KeystoreItemAbsent):
            backend.public_key()

    def test_nonzero_exit_elsewhere_is_plain_key_unavailable(
        self, tpm2_env: Path, tmp_path: Path
    ) -> None:
        # Break tpm2_sign specifically so create() still succeeds but
        # sign() fails for a reason that is NOT "handle not found". Exit
        # code 3 ("authentication error" per tpm2-tools' own
        # man/common/returns.md) is used rather than 1: code 1 is the
        # module's own documented ASSUMPTION #2 for "handle not found" on
        # THIS specific call, so asserting the non-absent path needs a
        # code that is unambiguously NOT that signal.
        bin_dir = tmp_path / "bin"
        provider = tpm.generate_tpm_key(tpm.Tpm2ToolsBackend(handle="0x810100b1"))
        (bin_dir / "tpm2_sign").write_text(
            f"#!{sys.executable}\nimport sys\nsys.stderr.write('boom\\n')\nsys.exit(3)\n",
            encoding="utf-8",
        )
        (bin_dir / "tpm2_sign").chmod(0o700)
        with pytest.raises(ledger.KeyUnavailable) as excinfo:
            provider.sign(b"payload")
        assert not isinstance(excinfo.value, signing.KeystoreItemAbsent)

    def test_timeout_is_key_unavailable(self, tpm2_env: Path, tmp_path: Path) -> None:
        _hanging_tool(tmp_path / "bin", "tpm2_readpublic")
        backend = tpm.Tpm2ToolsBackend(handle="0x810100b2", timeout=0.2)
        with pytest.raises(ledger.KeyUnavailable):
            backend.public_key()

    def test_malformed_pem_is_key_unavailable(self, tpm2_env: Path, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        (bin_dir / "tpm2_readpublic").write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            "out = sys.argv[sys.argv.index('-o') + 1]\n"
            "open(out, 'w').write('not-a-pem')\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        (bin_dir / "tpm2_readpublic").chmod(0o700)
        backend = tpm.Tpm2ToolsBackend(handle="0x810100b3")
        with pytest.raises(ledger.KeyUnavailable, match="PEM"):
            backend.public_key()

    def test_a_non_ec_pem_is_key_unavailable(self, tpm2_env: Path, tmp_path: Path) -> None:
        """A valid PEM that is nonetheless not an EC key (an RSA key, say)
        must be refused just as loudly as a malformed PEM."""
        bin_dir = tmp_path / "bin"
        (bin_dir / "tpm2_readpublic").write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            "from cryptography.hazmat.primitives.asymmetric import rsa\n"
            "from cryptography.hazmat.primitives.serialization import ("
            "Encoding, PublicFormat)\n"
            "out = sys.argv[sys.argv.index('-o') + 1]\n"
            "priv = rsa.generate_private_key(public_exponent=65537, key_size=1024)\n"
            "pem = priv.public_key().public_bytes("
            "Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)\n"
            "open(out, 'wb').write(pem)\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        (bin_dir / "tpm2_readpublic").chmod(0o700)
        backend = tpm.Tpm2ToolsBackend(handle="0x810100b5")
        with pytest.raises(ledger.KeyUnavailable, match="not an EC key"):
            backend.public_key()

    def test_the_chain_still_verifies_with_unsigned_lines(self, tmp_path: Path) -> None:
        backend = tpm.Tpm2ToolsBackend(handle="0x810100b4")
        provider = tpm.TpmKeyProvider(backend)
        ledger_dir = tmp_path / "ledger"
        _append_turn(ledger_dir, provider)
        _append_turn(ledger_dir, provider)
        assert ledger.verify_chain(ledger_dir).ok


class TestSignNeverClaimsTheKeyIsAbsent:
    """`tpm2_sign` exit 1 is NOT read as "no key at this handle".

    ASSUMPTION #2 buys the right to read exit 1 as "absent" for a bare
    `tpm2_readpublic`, where the classification feeds a decision that
    needs it (whether `generate_tpm_key` may overwrite). Reusing that
    inference on a signing call would put
    `key.error: "KeystoreItemAbsent"` on a ledger line -- "you never
    created a key" -- for a key that exists and whose signing attempt
    failed for some unrelated session/policy/PCR reason. These tests pin
    that `sign` makes no such claim.
    """

    def test_sign_failure_at_exit_1_is_plain_key_unavailable(
        self, tpm2_env: Path
    ) -> None:
        provider = tpm.generate_tpm_key(tpm.Tpm2ToolsBackend(handle="0x810100b1"))
        # The key genuinely exists; only `tpm2_sign` fails, with the same
        # exit code `tpm2_readpublic` uses for "no object at this handle".
        sign_tool = tpm2_env.parent / "bin" / "tpm2_sign"
        sign_tool.write_text(
            f"#!{sys.executable}\nimport sys\n"
            "sys.stderr.write('policy session required\\n')\nsys.exit(1)\n",
            encoding="utf-8",
        )
        sign_tool.chmod(0o700)

        with pytest.raises(ledger.KeyUnavailable) as excinfo:
            provider.sign(b"payload")
        assert not isinstance(excinfo.value, signing.KeystoreItemAbsent), (
            "a tpm2_sign failure was reported as 'you never created a key'"
        )

    def test_readpublic_at_exit_1_is_still_the_absent_subclass(
        self, tpm2_env: Path
    ) -> None:
        """The other side of the scope line: the inference still applies
        where it was argued for, so the test above is pinning a
        distinction rather than the absence of one."""
        backend = tpm.Tpm2ToolsBackend(handle="0x810100b2")
        with pytest.raises(signing.KeystoreItemAbsent):
            backend.public_key()


class TestLinuxArgvHygiene:
    def test_sign_payload_is_never_on_argv(
        self, tpm2_env: Path, recorder: RecordingSubprocess
    ) -> None:
        provider = tpm.generate_tpm_key(tpm.Tpm2ToolsBackend(handle="0x810100c0"))
        recorder.calls.clear()
        marker = "super-secret-payload-marker-xyz"
        provider.sign(marker.encode("ascii"))
        sign_calls = [c for c in recorder.calls if "tpm2_sign" in c.argv]
        assert len(sign_calls) == 1
        for token in recorder.every_argv_token():
            assert marker not in token

    def test_every_call_is_detached_from_the_controlling_terminal(
        self, tpm2_env: Path, recorder: RecordingSubprocess
    ) -> None:
        provider = tpm.generate_tpm_key(tpm.Tpm2ToolsBackend(handle="0x810100c1"))
        provider.sign(b"payload")
        assert recorder.calls
        assert all(c.kwargs.get("start_new_session") is True for c in recorder.calls)

    def test_the_hygiene_assertion_is_not_vacuous(self) -> None:
        secret = "super-secret-payload-marker-xyz"
        leaking = ["tpm2_sign", "-c", "0x81010002", secret]
        assert any(secret in token for token in leaking)


# ---------------------------------------------------------------------------
# Windows: happy path, over the real fake powershell
# ---------------------------------------------------------------------------


class TestWindowsGenerateSignVerifyRoundTrip:
    def test_sign_and_verify(self, fake_powershell: Path) -> None:
        backend = tpm.WindowsPlatformCryptoBackend(
            key_name="pw-test-1", powershell_path=fake_powershell
        )
        provider = tpm.generate_tpm_key(backend)
        signature = provider.sign(b"payload")
        assert signing.verify_signature(
            b"payload", signature, provider.public_key(), "ecdsa-p256-sha256"
        )
        assert not signing.verify_signature(
            b"other", signature, provider.public_key(), "ecdsa-p256-sha256"
        )

    def test_public_key_is_the_raw_x963_point(self, fake_powershell: Path) -> None:
        provider = tpm.generate_tpm_key(
            tpm.WindowsPlatformCryptoBackend(key_name="pw-test-2", powershell_path=fake_powershell)
        )
        pub = provider.public_key()
        assert len(pub) == 65
        assert pub[0:1] == b"\x04"

    def test_key_id_matches_an_independently_computed_digest(self, fake_powershell: Path) -> None:
        provider = tpm.generate_tpm_key(
            tpm.WindowsPlatformCryptoBackend(key_name="pw-test-3", powershell_path=fake_powershell)
        )
        expected = "ecdsa-p256-sha256:" + hashlib.sha256(provider.public_key()).hexdigest()[:16]
        assert provider.key_id() == expected

    def test_round_trip_through_append_line_and_verify_chain(
        self, fake_powershell: Path, tmp_path: Path
    ) -> None:
        provider = tpm.generate_tpm_key(
            tpm.WindowsPlatformCryptoBackend(
                key_name="pw-test-4", powershell_path=fake_powershell
            )
        )
        ledger_dir = tmp_path / "ledger"
        _append_turn(ledger_dir, provider)
        result = ledger.verify_chain(
            ledger_dir, public_keys={provider.key_id(): provider.public_key()}
        )
        assert result.ok
        assert result.invalid_signature_seqs == ()


class TestWindowsFailureModes:
    def test_missing_powershell_is_key_unavailable(self, tmp_path: Path) -> None:
        backend = tpm.WindowsPlatformCryptoBackend(
            key_name="pw-missing", powershell_path=tmp_path / "does-not-exist"
        )
        with pytest.raises(ledger.KeyUnavailable):
            backend.public_key()

    def test_key_not_found_is_the_absent_subclass(self, fake_powershell: Path) -> None:
        backend = tpm.WindowsPlatformCryptoBackend(
            key_name="pw-never-created", powershell_path=fake_powershell
        )
        with pytest.raises(signing.KeystoreItemAbsent):
            backend.public_key()

    def test_timeout_is_key_unavailable(self, tmp_path: Path) -> None:
        backend = tpm.WindowsPlatformCryptoBackend(
            key_name="pw-hang", powershell_path=_hanging_powershell(tmp_path), timeout=0.2
        )
        with pytest.raises(ledger.KeyUnavailable):
            backend.sign(b"payload")

    def test_generic_nonzero_exit_is_not_the_absent_subclass(self, tmp_path: Path) -> None:
        """Symmetry with the Linux side: only the script's OWN dedicated
        absent code means absent; a generic failure must not."""
        failing = tmp_path / "fake-failing-powershell"
        failing.write_text(
            f"#!{sys.executable}\nimport sys\n"
            "sys.stderr.write('boom\\n')\nsys.exit(1)\n",
            encoding="utf-8",
        )
        failing.chmod(0o700)
        backend = tpm.WindowsPlatformCryptoBackend(
            key_name="pw-generic-failure", powershell_path=failing
        )
        with pytest.raises(ledger.KeyUnavailable) as excinfo:
            backend.public_key()
        assert not isinstance(excinfo.value, signing.KeystoreItemAbsent)

    def test_malformed_output_is_key_unavailable(self, tmp_path: Path) -> None:
        bad = tmp_path / "fake-garbage-powershell"
        bad.write_text(f"#!{sys.executable}\nprint('not-base64!!')\n", encoding="utf-8")
        bad.chmod(0o700)
        backend = tpm.WindowsPlatformCryptoBackend(key_name="pw-garbage", powershell_path=bad)
        with pytest.raises(ledger.KeyUnavailable, match="not base64"):
            backend.public_key()


class TestWindowsArgvHygiene:
    def test_sign_payload_is_on_stdin_never_argv(
        self, fake_powershell: Path, recorder: RecordingSubprocess
    ) -> None:
        provider = tpm.generate_tpm_key(
            tpm.WindowsPlatformCryptoBackend(key_name="pw-argv-1", powershell_path=fake_powershell)
        )
        recorder.calls.clear()
        marker = b"super-secret-payload-marker-xyz"
        provider.sign(marker)
        sign_calls = [c for c in recorder.calls if "sign" in c.argv]
        assert len(sign_calls) == 1
        assert sign_calls[0].stdin == marker
        for token in recorder.every_argv_token():
            assert marker.decode("ascii") not in token

    def test_key_name_is_on_argv_but_is_not_secret(
        self, fake_powershell: Path, recorder: RecordingSubprocess
    ) -> None:
        """Lookup coordinates travel on argv, exactly like
        SecretServiceBackend's service/account -- only the payload must
        avoid it."""
        tpm.generate_tpm_key(
            tpm.WindowsPlatformCryptoBackend(key_name="pw-argv-2", powershell_path=fake_powershell)
        )
        assert any("pw-argv-2" in token for token in recorder.every_argv_token())

    def test_every_call_is_detached_from_the_controlling_terminal(
        self, fake_powershell: Path, recorder: RecordingSubprocess
    ) -> None:
        provider = tpm.generate_tpm_key(
            tpm.WindowsPlatformCryptoBackend(key_name="pw-argv-3", powershell_path=fake_powershell)
        )
        provider.sign(b"payload")
        assert recorder.calls
        assert all(c.kwargs.get("start_new_session") is True for c in recorder.calls)

    def test_the_hygiene_assertion_is_not_vacuous(self) -> None:
        secret = base64.b64encode(b"\x00" * 32).decode("ascii")
        leaking = ["powershell", "-Command", f"Sign {secret}"]
        assert any(secret in token for token in leaking)


# ---------------------------------------------------------------------------
# One warning per process
# ---------------------------------------------------------------------------


class TestOneWarningPerProcess:
    def test_two_failures_warn_exactly_once(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="promptwitness.signing")
        backend = tpm.Tpm2ToolsBackend(handle="0x810100d0")
        provider = tpm.TpmKeyProvider(backend)
        with pytest.raises(ledger.KeyUnavailable):
            provider.public_key()
        with pytest.raises(ledger.KeyUnavailable):
            provider.sign(b"payload")
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1

    def test_the_warning_names_the_backend_and_carries_no_key_material(
        self, tpm2_env: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        provider = tpm.generate_tpm_key(tpm.Tpm2ToolsBackend(handle="0x810100d1"))
        # Clear the handle out from under the provider -- the enclave/TPM
        # becomes unavailable mid-process.
        store = tpm2_env
        for item in store.iterdir():
            item.unlink()
        caplog.set_level(logging.WARNING, logger="promptwitness.signing")
        # `ledger.KeyUnavailable`, not `signing.KeystoreItemAbsent`: a
        # `tpm2_sign` failure never claims the key is absent, even when
        # the handle really is empty, because exit 1 cannot tell the two
        # apart -- see TestSignNeverClaimsTheKeyIsAbsent.
        with pytest.raises(ledger.KeyUnavailable) as excinfo:
            provider.sign(b"payload")
        assert not isinstance(excinfo.value, signing.KeystoreItemAbsent)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "tpm2-tools" in warnings[0].getMessage()


# ---------------------------------------------------------------------------
# generate_tpm_key: refuse-to-replace, force rotation, no spurious warnings
# ---------------------------------------------------------------------------


class TestGenerateTpmKey:
    def test_refuses_to_replace_an_existing_key(self, tpm2_env: Path) -> None:
        backend = tpm.Tpm2ToolsBackend(handle="0x810100e0")
        first = tpm.generate_tpm_key(backend)
        before = first.key_id()
        with pytest.raises(FileExistsError):
            tpm.generate_tpm_key(backend)
        assert tpm.TpmKeyProvider(backend).key_id() == before

    def test_replace_true_rotates_the_key(self, tpm2_env: Path) -> None:
        backend = tpm.Tpm2ToolsBackend(handle="0x810100e1")
        before = tpm.generate_tpm_key(backend).key_id()
        after = tpm.generate_tpm_key(backend, replace=True).key_id()
        assert before != after

    def test_the_existence_probe_emits_no_warning(
        self, tpm2_env: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="promptwitness.signing")
        tpm.generate_tpm_key(tpm.Tpm2ToolsBackend(handle="0x810100e2"))
        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []

    def test_generation_prints_nothing_at_all(
        self, tpm2_env: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        tpm.generate_tpm_key(tpm.Tpm2ToolsBackend(handle="0x810100e3"))
        captured = capfd.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_windows_refuses_to_replace_an_existing_key(self, fake_powershell: Path) -> None:
        backend = tpm.WindowsPlatformCryptoBackend(
            key_name="pw-replace-1", powershell_path=fake_powershell
        )
        first = tpm.generate_tpm_key(backend)
        before = first.key_id()
        with pytest.raises(FileExistsError):
            tpm.generate_tpm_key(backend)
        assert tpm.TpmKeyProvider(backend).key_id() == before

    def test_windows_replace_true_rotates_the_key(self, fake_powershell: Path) -> None:
        backend = tpm.WindowsPlatformCryptoBackend(
            key_name="pw-replace-2", powershell_path=fake_powershell
        )
        before = tpm.generate_tpm_key(backend).key_id()
        after = tpm.generate_tpm_key(backend, replace=True).key_id()
        assert before != after
