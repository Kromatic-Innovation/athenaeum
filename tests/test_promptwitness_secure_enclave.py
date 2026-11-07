# SPDX-License-Identifier: Apache-2.0
"""Tests for the Secure Enclave key adapter (athenaeum#2153).

The real Swift helper (``tools/promptwitness-se/``) cannot run here --
there is no macOS Secure Enclave in this container -- so every test below
runs against a FAKE helper: a real, executable Python script, run as a
genuine child process (never a mocked ``subprocess.run``, per the issue's
own AC2), that implements the real ``create``/``sign``/``pubkey`` contract
over an actual ``cryptography`` ECDSA P-256 key. Its wire format matches
the Swift helper byte-for-byte: base64 raw X9.63 public key, a DER
signature on stdout, exit code 2 for "no key file yet" -- so a sign ->
verify round trip here actually verifies, and ``key_id`` is stable and
reproducible exactly as it would be against the real helper.

Every path builds filesystem paths from ``tmp_path`` and monkeypatched env
vars only -- never a hardcoded machine-specific home-directory literal
(the public-safe-lint gate rejects that shape even in test fixtures).
"""

from __future__ import annotations

import base64
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from promptwitness import canon, ledger, secure_enclave, signing


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
# A real, executable fake helper (AC2: not only a mocked subprocess.run)
# ---------------------------------------------------------------------------

_STUB_SOURCE = f"""#!{sys.executable}
# Fake promptwitness-se helper for tests -- see
# tests/test_promptwitness_secure_enclave.py for why this is a real
# executable rather than a mocked subprocess call. Wire-format-compatible
# with the real Swift helper: base64 raw X9.63 public key, DER signature
# on stdout, exit code 2 for "no key file yet".
import os
import sys
import base64
from pathlib import Path

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_NO_KEY = 2
EXIT_USAGE = 64


def _parse(rest):
    key_file = None
    force = False
    i = 0
    while i < len(rest):
        if rest[i] == "--key-file":
            i += 1
            key_file = rest[i]
        elif rest[i] == "--force":
            force = True
        i += 1
    return key_file, force


def _b64_pubkey(raw_scalar):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    priv = ec.derive_private_key(int.from_bytes(raw_scalar, "big"), ec.SECP256R1())
    pub = priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return base64.b64encode(pub).decode("ascii")


def main():
    args = sys.argv[1:]
    if not args:
        return EXIT_USAGE
    sub, rest = args[0], args[1:]
    key_file, force = _parse(rest)
    if key_file is None:
        return EXIT_USAGE

    if sub == "create":
        if os.path.exists(key_file) and not force:
            sys.stderr.write("a key file already exists; refusing to replace it\\n")
            return EXIT_FAILURE
        from cryptography.hazmat.primitives.asymmetric import ec

        priv = ec.generate_private_key(ec.SECP256R1())
        raw = priv.private_numbers().private_value.to_bytes(32, "big")
        Path(key_file).parent.mkdir(parents=True, exist_ok=True)
        Path(key_file).write_bytes(raw)
        os.chmod(key_file, 0o600)
        print(_b64_pubkey(raw))
        return EXIT_OK

    if not os.path.exists(key_file):
        sys.stderr.write("no key file\\n")
        return EXIT_NO_KEY
    raw = Path(key_file).read_bytes()

    if sub == "pubkey":
        print(_b64_pubkey(raw))
        return EXIT_OK

    if sub == "sign":
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec

        payload = sys.stdin.buffer.read()
        priv = ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())
        sig = priv.sign(payload, ec.ECDSA(hashes.SHA256()))
        sys.stdout.buffer.write(sig)
        return EXIT_OK

    sys.stderr.write("unknown subcommand\\n")
    return EXIT_USAGE


sys.exit(main())
"""


def _write_fake_helper(tmp_path: Path) -> Path:
    """Write the fake helper script into *tmp_path* and make it executable."""
    path = tmp_path / "fake-promptwitness-se"
    path.write_text(_STUB_SOURCE, encoding="utf-8")
    path.chmod(0o700)
    return path


@pytest.fixture
def helper(tmp_path: Path) -> Path:
    return _write_fake_helper(tmp_path)


# ---------------------------------------------------------------------------
# A recording proxy over the REAL subprocess module
# ---------------------------------------------------------------------------


@dataclass
class RecordedCall:
    """One ``subprocess.run`` the module made, as the OS would have seen it."""

    argv: list[str]
    stdin: bytes | None
    kwargs: dict[str, Any] = field(default_factory=dict)


class RecordingSubprocess:
    """Forwards every call to the REAL ``subprocess`` module while recording
    it exactly as ``signing._run_keystore_tool`` made it.

    Unlike ``tests/test_promptwitness_os_keystore.py``'s ``FakeSubprocess``
    (which substitutes an in-memory tool for the OS), this proxy never
    fakes the child process: ``run`` really execs the fake helper script,
    so the argv-hygiene assertions below inspect the genuine ``subprocess``
    boundary against a genuinely running executable (AC2), not a
    simulation of one.
    """

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
    """``signing.subprocess`` replaced by a recording proxy over the real
    module -- `secure_enclave._run_helper` reuses `signing._run_keystore_tool`,
    which resolves `subprocess.run`/`PIPE`/`TimeoutExpired` through
    `signing`'s own module globals, so patching it there is the correct
    seam regardless of which module calls in."""
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


def _missing_helper(tmp_path: Path) -> Path:
    """A helper path that genuinely does not exist."""
    return tmp_path / "does-not-exist-promptwitness-se"


def _nonzero_helper(tmp_path: Path) -> Path:
    """A real, executable script that always exits 1 with a stderr line."""
    path = tmp_path / "fake-failing-se"
    path.write_text(
        f"#!{sys.executable}\nimport sys\nsys.stderr.write('boom\\n')\nsys.exit(1)\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def _hanging_helper(tmp_path: Path) -> Path:
    """A real, executable script that reads stdin forever -- used to
    exercise the timeout path without relying on a mock."""
    path = tmp_path / "fake-hanging-se"
    path.write_text(
        f"#!{sys.executable}\nimport time\ntime.sleep(60)\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


# ---------------------------------------------------------------------------
# Protocol shape and the strength vocabulary
# ---------------------------------------------------------------------------


class TestProtocolAndStrength:
    def test_provider_satisfies_both_protocols(self, tmp_path: Path) -> None:
        provider = secure_enclave.SecureEnclaveKeyProvider(
            helper_path=tmp_path / "helper", key_path=tmp_path / "key"
        )
        assert isinstance(provider, signing.KeyProvider)
        assert isinstance(provider, ledger.KeyProviderLike)

    def test_strength_is_hardware_nonexportable(self, tmp_path: Path) -> None:
        provider = secure_enclave.SecureEnclaveKeyProvider(
            helper_path=tmp_path / "helper", key_path=tmp_path / "key"
        )
        assert provider.strength() == "hardware-nonexportable"
        assert provider.strength() == secure_enclave.STRENGTH
        assert provider.strength() in signing.STRENGTHS

    def test_presence_required_is_false(self, tmp_path: Path) -> None:
        """Ruling 1 builds no presence-gated adapter: non-exportability
        proves the line was signed on this device, nothing about who
        triggered it."""
        provider = secure_enclave.SecureEnclaveKeyProvider(
            helper_path=tmp_path / "helper", key_path=tmp_path / "key"
        )
        assert provider.presence_required() is False

    def test_algorithm_is_ecdsa_p256_sha256(self, tmp_path: Path) -> None:
        provider = secure_enclave.SecureEnclaveKeyProvider(
            helper_path=tmp_path / "helper", key_path=tmp_path / "key"
        )
        assert provider.algorithm() == "ecdsa-p256-sha256"
        assert provider.algorithm() == secure_enclave.ALGORITHM


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


class TestResolveHelperPath:
    def test_override_wins(self, tmp_path: Path) -> None:
        got = secure_enclave.resolve_helper_path(override=tmp_path / "x")
        assert got == tmp_path / "x"

    def test_env_wins_over_default(self, tmp_path: Path) -> None:
        got = secure_enclave.resolve_helper_path(
            env={"PROMPTWITNESS_SE_HELPER": str(tmp_path / "y")}
        )
        assert got == tmp_path / "y"

    def test_override_wins_over_env(self, tmp_path: Path) -> None:
        got = secure_enclave.resolve_helper_path(
            override=tmp_path / "x",
            env={"PROMPTWITNESS_SE_HELPER": str(tmp_path / "y")},
        )
        assert got == tmp_path / "x"

    def test_default_is_the_bare_command_name(self) -> None:
        """No hardcoded install location: the bare name lets `subprocess`
        search `PATH`, exactly like `signing._run_keystore_tool`'s own
        argv (`"security"`, `"secret-tool"`) already relies on it to."""
        got = secure_enclave.resolve_helper_path(env={})
        assert got == Path("promptwitness-se")


class TestResolveKeyPath:
    def test_override_wins(self, tmp_path: Path) -> None:
        got = secure_enclave.resolve_key_path("claude-code", override=tmp_path / "k")
        assert got == tmp_path / "k"

    def test_env_wins_over_default(self, tmp_path: Path) -> None:
        got = secure_enclave.resolve_key_path(
            "claude-code", env={"PROMPTWITNESS_SE_KEY_PATH": str(tmp_path / "k2")}
        )
        assert got == tmp_path / "k2"

    def test_default_is_under_the_ledger_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `ledger.resolve_ledger_dir` reads `os.environ` directly (it
        # takes no `env=` mapping), so the default branch is exercised
        # through the real env, exactly as
        # `test_promptwitness_os_keystore.py`'s equivalent test does.
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(tmp_path / "pw"))
        monkeypatch.delenv("PROMPTWITNESS_SE_KEY_PATH", raising=False)
        got = secure_enclave.resolve_key_path("claude-code")
        assert got == tmp_path / "pw" / "keys" / "secure-enclave.key"


class TestResolveKeyProviderSelectsSecureEnclave:
    def test_env_selects_secure_enclave(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PROMPTWITNESS_KEY_PROVIDER", "secure-enclave")
        got = signing.resolve_key_provider("claude-code")
        assert isinstance(got, secure_enclave.SecureEnclaveKeyProvider)

    def test_argument_selects_secure_enclave(self, tmp_path: Path) -> None:
        got = signing.resolve_key_provider(
            "claude-code",
            provider="secure-enclave",
            se_helper=tmp_path / "helper",
            key_path=tmp_path / "key",
            env={},
        )
        assert isinstance(got, secure_enclave.SecureEnclaveKeyProvider)
        assert got.helper_path == tmp_path / "helper"
        assert got.key_path == tmp_path / "key"

    def test_resolution_never_touches_the_helper(
        self, recorder: RecordingSubprocess, tmp_path: Path
    ) -> None:
        """Resolving a provider must not stall or fail because the Secure
        Enclave is unavailable -- that shows up later as an unsigned line
        plus one warning."""
        signing.resolve_key_provider(
            "claude-code",
            provider="secure-enclave",
            se_helper=tmp_path / "helper",
            key_path=tmp_path / "key",
            env={},
        )
        assert recorder.calls == []


# ---------------------------------------------------------------------------
# Happy path: generate -> sign -> verify, over the real fake-helper process
# ---------------------------------------------------------------------------


class TestGenerateSignVerifyRoundTrip:
    def test_sign_and_verify(
        self, helper: Path, tmp_path: Path, recorder: RecordingSubprocess
    ) -> None:
        key_path = tmp_path / "keys" / "se.key"
        provider = secure_enclave.generate_secure_enclave_key(helper, key_path)
        signature = provider.sign(b"payload")
        assert signing.verify_signature(
            b"payload", signature, provider.public_key(), "ecdsa-p256-sha256"
        )
        assert not signing.verify_signature(
            b"other", signature, provider.public_key(), "ecdsa-p256-sha256"
        )

    def test_public_key_is_the_raw_x963_point(self, helper: Path, tmp_path: Path) -> None:
        provider = secure_enclave.generate_secure_enclave_key(helper, tmp_path / "se.key")
        pub = provider.public_key()
        assert len(pub) == 65
        assert pub[0:1] == b"\x04"

    def test_key_id_is_stable_and_matches_derive_key_id(
        self, helper: Path, tmp_path: Path
    ) -> None:
        provider = secure_enclave.generate_secure_enclave_key(helper, tmp_path / "se.key")
        assert provider.key_id() == provider.key_id()
        assert provider.key_id() == signing.derive_key_id(
            provider.public_key(), "ecdsa-p256-sha256"
        )
        assert provider.key_id().startswith("ecdsa-p256-sha256:")

    def test_round_trip_through_append_line_and_verify_chain(
        self, helper: Path, tmp_path: Path
    ) -> None:
        provider = secure_enclave.generate_secure_enclave_key(helper, tmp_path / "se.key")
        ledger_dir = tmp_path / "ledger"
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

    def test_two_keys_produce_different_ids(self, helper: Path, tmp_path: Path) -> None:
        a = secure_enclave.generate_secure_enclave_key(helper, tmp_path / "a.key")
        b = secure_enclave.generate_secure_enclave_key(helper, tmp_path / "b.key")
        assert a.key_id() != b.key_id()


# ---------------------------------------------------------------------------
# AC2: the payload (and any key material) never appears on argv
# ---------------------------------------------------------------------------


class TestArgvHygiene:
    def test_sign_payload_is_on_stdin_never_argv(
        self, helper: Path, tmp_path: Path, recorder: RecordingSubprocess
    ) -> None:
        provider = secure_enclave.generate_secure_enclave_key(helper, tmp_path / "se.key")
        recorder.calls.clear()
        marker = b"super-secret-payload-marker-xyz"
        provider.sign(marker)

        sign_calls = [c for c in recorder.calls if "sign" in c.argv]
        assert len(sign_calls) == 1
        assert sign_calls[0].stdin == marker
        for token in recorder.every_argv_token():
            assert marker.decode("ascii") not in token

    def test_key_material_never_appears_on_argv(
        self, helper: Path, tmp_path: Path, recorder: RecordingSubprocess
    ) -> None:
        key_path = tmp_path / "se.key"
        secure_enclave.generate_secure_enclave_key(helper, key_path)
        raw_scalar = key_path.read_bytes()
        secrets = [raw_scalar.hex(), base64.b64encode(raw_scalar).decode("ascii")]
        for token in recorder.every_argv_token():
            for secret in secrets:
                assert secret not in token, f"key material on argv: {token!r}"

    def test_every_call_is_detached_from_the_controlling_terminal(
        self, helper: Path, tmp_path: Path, recorder: RecordingSubprocess
    ) -> None:
        """A helper that somehow wanted a prompt must not be able to grab
        the terminal out from under the session it is observing."""
        provider = secure_enclave.generate_secure_enclave_key(helper, tmp_path / "se.key")
        provider.sign(b"payload")
        assert recorder.calls
        assert all(c.kwargs.get("start_new_session") is True for c in recorder.calls)

    def test_the_hygiene_assertion_is_not_vacuous(self) -> None:
        """A sanity floor: the sweep above would actually catch a secret on
        argv if one were there."""
        secret = base64.b64encode(b"\x00" * 32).decode("ascii")
        leaking = ["fake-se", "sign", "--payload", secret]
        assert any(secret in token for token in leaking)


# ---------------------------------------------------------------------------
# Failure paths: every one becomes ledger.KeyUnavailable (or its absent
# subclass), never a crash, never a bare exception type the caller cannot
# recognize.
# ---------------------------------------------------------------------------


class TestHelperFailureModes:
    def test_missing_helper_binary_is_key_unavailable(
        self, tmp_path: Path, recorder: RecordingSubprocess
    ) -> None:
        provider = secure_enclave.SecureEnclaveKeyProvider(
            helper_path=_missing_helper(tmp_path), key_path=tmp_path / "se.key"
        )
        with pytest.raises(ledger.KeyUnavailable):
            provider.public_key()

    def test_helper_exits_nonzero_is_key_unavailable(
        self, tmp_path: Path, recorder: RecordingSubprocess
    ) -> None:
        # A key file must exist so the stub reaches its generic failure
        # path rather than the "no key" short-circuit.
        key_path = tmp_path / "se.key"
        key_path.write_bytes(b"\x01" * 32)
        provider = secure_enclave.SecureEnclaveKeyProvider(
            helper_path=_nonzero_helper(tmp_path), key_path=key_path
        )
        with pytest.raises(ledger.KeyUnavailable) as excinfo:
            provider.public_key()
        assert not issubclass(type(excinfo.value), signing.KeystoreItemAbsent)

    def test_helper_timeout_is_key_unavailable(
        self, tmp_path: Path, recorder: RecordingSubprocess
    ) -> None:
        provider = secure_enclave.SecureEnclaveKeyProvider(
            helper_path=_hanging_helper(tmp_path),
            key_path=tmp_path / "se.key",
            timeout=0.2,
        )
        with pytest.raises(ledger.KeyUnavailable):
            provider.sign(b"payload")

    def test_no_key_yet_is_the_absent_subclass_not_plain_key_unavailable(
        self, helper: Path, tmp_path: Path
    ) -> None:
        """The distinction the overwrite guard depends on: exit code 2
        ("no key file yet") must be reported as
        `signing.KeystoreItemAbsent`, reused directly rather than given a
        second, meaning-duplicating subclass here."""
        provider = secure_enclave.SecureEnclaveKeyProvider(
            helper_path=helper, key_path=tmp_path / "never-created.key"
        )
        with pytest.raises(signing.KeystoreItemAbsent):
            provider.public_key()

    def test_a_helper_that_cannot_answer_is_not_reported_as_absent(
        self, tmp_path: Path, recorder: RecordingSubprocess
    ) -> None:
        key_path = tmp_path / "se.key"
        key_path.write_bytes(b"\x01" * 32)
        provider = secure_enclave.SecureEnclaveKeyProvider(
            helper_path=_nonzero_helper(tmp_path), key_path=key_path
        )
        with pytest.raises(ledger.KeyUnavailable) as excinfo:
            provider.sign(b"payload")
        assert not isinstance(excinfo.value, signing.KeystoreItemAbsent)

    def test_malformed_pubkey_output_is_key_unavailable(self, tmp_path: Path) -> None:
        """The helper exits 0 but prints garbage: the Python side must not
        propagate a bare base64 `binascii.Error`."""
        bad_helper = tmp_path / "fake-garbage-se"
        bad_helper.write_text(
            f"#!{sys.executable}\nprint('not-base64!!')\n", encoding="utf-8"
        )
        bad_helper.chmod(0o700)
        key_path = tmp_path / "se.key"
        key_path.write_bytes(b"\x01" * 32)
        provider = secure_enclave.SecureEnclaveKeyProvider(
            helper_path=bad_helper, key_path=key_path
        )
        with pytest.raises(ledger.KeyUnavailable, match="not base64"):
            provider.public_key()

    def test_the_chain_still_verifies_with_unsigned_lines(self, tmp_path: Path) -> None:
        """Degrading must never break the hash chain."""
        provider = secure_enclave.SecureEnclaveKeyProvider(
            helper_path=_missing_helper(tmp_path), key_path=tmp_path / "se.key"
        )
        ledger_dir = tmp_path / "ledger"
        _append_turn(ledger_dir, provider)
        _append_turn(ledger_dir, provider)
        assert ledger.verify_chain(ledger_dir).ok


# ---------------------------------------------------------------------------
# AC3-equivalent: an unavailable Secure Enclave produces exactly one
# process-wide warning, never one per call.
# ---------------------------------------------------------------------------


class TestOneWarningPerProcess:
    def test_two_failures_warn_exactly_once(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="promptwitness.signing")
        provider = secure_enclave.SecureEnclaveKeyProvider(
            helper_path=_missing_helper(tmp_path), key_path=tmp_path / "se.key"
        )
        with pytest.raises(ledger.KeyUnavailable):
            provider.public_key()
        with pytest.raises(ledger.KeyUnavailable):
            provider.sign(b"payload")
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1

    def test_the_warning_names_secure_enclave_and_carries_no_key_material(
        self, helper: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        key_path = tmp_path / "se.key"
        provider = secure_enclave.generate_secure_enclave_key(helper, key_path)
        raw_scalar = key_path.read_bytes()
        key_path.unlink()  # the enclave/key becomes unavailable mid-process

        caplog.set_level(logging.WARNING, logger="promptwitness.signing")
        with pytest.raises(signing.KeystoreItemAbsent):
            provider.sign(b"payload")
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "secure-enclave" in message
        assert raw_scalar.hex() not in message

    def test_malformed_output_also_warns_exactly_once(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        bad_helper = tmp_path / "fake-garbage-se"
        bad_helper.write_text(
            f"#!{sys.executable}\nprint('not-base64!!')\n", encoding="utf-8"
        )
        bad_helper.chmod(0o700)
        key_path = tmp_path / "se.key"
        key_path.write_bytes(b"\x01" * 32)
        provider = secure_enclave.SecureEnclaveKeyProvider(
            helper_path=bad_helper, key_path=key_path
        )
        caplog.set_level(logging.WARNING, logger="promptwitness.signing")
        with pytest.raises(ledger.KeyUnavailable):
            provider.public_key()
        with pytest.raises(ledger.KeyUnavailable):
            provider.key_id()
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1


# ---------------------------------------------------------------------------
# generate_secure_enclave_key: refuse-to-replace, force rotation, the
# existence probe never warns on an expected miss.
# ---------------------------------------------------------------------------


class TestGenerateSecureEnclaveKey:
    def test_refuses_to_replace_an_existing_key(self, helper: Path, tmp_path: Path) -> None:
        key_path = tmp_path / "se.key"
        first = secure_enclave.generate_secure_enclave_key(helper, key_path)
        before = first.key_id()
        with pytest.raises(FileExistsError):
            secure_enclave.generate_secure_enclave_key(helper, key_path)
        assert secure_enclave.SecureEnclaveKeyProvider(
            helper_path=helper, key_path=key_path
        ).key_id() == before

    def test_replace_true_rotates_the_key(self, helper: Path, tmp_path: Path) -> None:
        key_path = tmp_path / "se.key"
        before = secure_enclave.generate_secure_enclave_key(helper, key_path).key_id()
        after = secure_enclave.generate_secure_enclave_key(
            helper, key_path, replace=True
        ).key_id()
        assert before != after

    def test_the_existence_probe_emits_no_warning(
        self, helper: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A miss on the FIRST create is the EXPECTED outcome, and must not
        trip the process-wide warning that a real failure deserves."""
        caplog.set_level(logging.WARNING, logger="promptwitness.signing")
        secure_enclave.generate_secure_enclave_key(helper, tmp_path / "se.key")
        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []

    def test_generation_prints_nothing_at_all(
        self, helper: Path, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        secure_enclave.generate_secure_enclave_key(helper, tmp_path / "se.key")
        captured = capfd.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_force_still_rotates_through_an_unreadable_helper(
        self, tmp_path: Path, recorder: RecordingSubprocess
    ) -> None:
        """`--force` is the operator saying "I know, do it anyway", so the
        existence probe is skipped entirely -- the guard protects the
        DEFAULT, it does not override an explicit instruction. A helper
        that always fails still fails the `create` call itself here (it
        is not a REAL Secure Enclave, so this asserts the probe-skip, not
        a successful rotation)."""
        key_path = tmp_path / "se.key"
        key_path.write_bytes(b"\x01" * 32)
        with pytest.raises(ledger.KeyUnavailable):
            secure_enclave.generate_secure_enclave_key(
                _nonzero_helper(tmp_path), key_path, replace=True
            )
        # No probe call was made before the (failing) create: exactly one
        # subprocess call happened, not two.
        assert len(recorder.calls) == 1
        assert "create" in recorder.calls[0].argv

    def test_an_unreadable_helper_refuses_rather_than_overwriting(
        self, helper: Path, tmp_path: Path, recorder: RecordingSubprocess
    ) -> None:
        """The distinction the overwrite guard depends on: a probe that
        cannot answer is NOT evidence the key is absent."""
        key_path = tmp_path / "se.key"
        secure_enclave.generate_secure_enclave_key(helper, key_path)
        stored_before = key_path.read_bytes()

        with pytest.raises(ledger.KeyUnavailable):
            secure_enclave.generate_secure_enclave_key(_nonzero_helper(tmp_path), key_path)
        assert key_path.read_bytes() == stored_before, "the existing signing key was touched"
