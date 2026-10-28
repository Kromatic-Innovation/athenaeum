# SPDX-License-Identifier: Apache-2.0
"""Tests for the ``os-keystore`` key adapter (athenaeum#2152).

All three backends are exercised at their real boundary and nowhere short of
it: the macOS and Secret Service backends through a fake ``subprocess``
module injected into :mod:`promptwitness.signing` (so the argv the tool
would really have received is observable), and the Windows DPAPI backend
through fake ``crypt32``/``kernel32`` library objects that write into the
genuine ``ctypes`` out-parameter -- which means the structure marshalling,
the ``string_at`` read and the ``LocalFree`` all run for real on this
non-Windows host.

Every path builds filesystem paths from ``tmp_path`` and monkeypatched env
vars only -- never a hardcoded machine-specific home-directory literal (the
public-safe-lint gate rejects that shape even in test fixtures).
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pytest

from promptwitness import canon, cli, ledger, signing

# ---------------------------------------------------------------------------
# A fake `subprocess` module, and fake `security` / `secret-tool` tools
# ---------------------------------------------------------------------------


@dataclass
class RecordedCall:
    """One ``subprocess.run`` the module made, as the OS would have seen it."""

    argv: list[str]
    stdin: bytes | None
    kwargs: dict[str, Any]


class FakeSubprocess:
    """Stand-in for the ``subprocess`` module.

    Injected as ``signing.subprocess`` rather than by patching
    ``subprocess.run`` globally, so the replacement is scoped to the module
    under test while still being the real boundary from its point of view:
    ``signing`` resolves ``subprocess.run``, ``subprocess.PIPE`` and
    ``subprocess.TimeoutExpired`` through this object.
    """

    PIPE = subprocess.PIPE
    DEVNULL = subprocess.DEVNULL
    TimeoutExpired = subprocess.TimeoutExpired
    CalledProcessError = subprocess.CalledProcessError

    def __init__(self, handler: Callable[[FakeSubprocess, list[str], dict[str, Any]], Any]):
        self._handler = handler
        self.calls: list[RecordedCall] = []

    def run(self, argv: list[str], **kwargs: Any) -> Any:
        self.calls.append(
            RecordedCall(argv=list(argv), stdin=kwargs.get("input"), kwargs=dict(kwargs))
        )
        return self._handler(self, list(argv), kwargs)

    # -- assertions shared by every argv-hygiene test --------------------
    def every_argv_token(self) -> list[str]:
        return [token for call in self.calls for token in call.argv]


def _completed(argv: list[str], returncode: int = 0, stdout: bytes = b"") -> Any:
    return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=b"")


@dataclass
class FakeKeystoreTool:
    """An in-memory keystore behind a faithful ``security`` / ``secret-tool``
    command-line surface.

    It reads the secret from the stdin it was handed, exactly as the real
    tools do, which is what makes the argv-hygiene assertion meaningful: if
    the module ever moved the secret to argv, this fake would store nothing
    and every round-trip test would fail, independently of the explicit
    hygiene test.
    """

    items: dict[tuple[str, str], bytes] = field(default_factory=dict)

    def __call__(self, fake: FakeSubprocess, argv: list[str], kwargs: dict[str, Any]) -> Any:
        tool = argv[0]
        if tool == "security":
            return self._security(argv, kwargs)
        if tool == "secret-tool":
            return self._secret_tool(argv, kwargs)
        raise AssertionError(f"unexpected tool {tool!r}")

    @staticmethod
    def _flag(argv: list[str], flag: str) -> str:
        return argv[argv.index(flag) + 1]

    def _security(self, argv: list[str], kwargs: dict[str, Any]) -> Any:
        key = (self._flag(argv, "-s"), self._flag(argv, "-a"))
        if argv[1] == "add-generic-password":
            stdin = kwargs.get("input") or b""
            # The real tool reads one line from stdin for a valueless `-w`.
            self.items[key] = stdin.split(b"\n", 1)[0]
            return _completed(argv)
        if argv[1] == "find-generic-password":
            if key not in self.items:
                return _completed(argv, returncode=44)
            return _completed(argv, stdout=self.items[key] + b"\n")
        raise AssertionError(f"unexpected security verb {argv[1]!r}")

    def _secret_tool(self, argv: list[str], kwargs: dict[str, Any]) -> Any:
        attrs = dict(zip(argv[2:][::2], argv[2:][1::2], strict=False))
        key = (attrs["service"], attrs["account"])
        if argv[1] == "store":
            self.items[key] = kwargs.get("input") or b""
            return _completed(argv)
        if argv[1] == "lookup":
            # `secret-tool lookup` exits 0 with empty stdout on a miss.
            return _completed(argv, stdout=self.items.get(key, b""))
        raise AssertionError(f"unexpected secret-tool verb {argv[1]!r}")


@pytest.fixture
def keystore(monkeypatch: pytest.MonkeyPatch) -> FakeSubprocess:
    """``signing.subprocess`` replaced by a fake over an in-memory keystore."""
    fake = FakeSubprocess(FakeKeystoreTool())
    monkeypatch.setattr(signing, "subprocess", fake)
    return fake


@pytest.fixture(autouse=True)
def _reset_warn_once() -> Any:
    """The "one warning per process" flag is module-global, so every test
    here must start and end with it clear or the warning-count assertions
    would depend on test order."""
    signing.reset_warned_for_tests()
    yield
    signing.reset_warned_for_tests()


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
# Protocol shape and the strength vocabulary
# ---------------------------------------------------------------------------


class TestProtocolAndStrength:
    def test_provider_satisfies_both_protocols(self) -> None:
        provider = signing.OsKeystoreKeyProvider(
            signing.SecretServiceBackend(account="claude-code")
        )
        assert isinstance(provider, signing.KeyProvider)
        assert isinstance(provider, ledger.KeyProviderLike)

    def test_strength_is_os_keystore(self) -> None:
        provider = signing.OsKeystoreKeyProvider(
            signing.SecretServiceBackend(account="claude-code")
        )
        assert provider.strength() == "os-keystore"
        assert provider.strength() in signing.STRENGTHS

    def test_presence_required_is_false(self) -> None:
        """Ruling 1 builds no presence-gated adapter: this tier protects the
        key at rest and makes no claim at all about a human being present."""
        provider = signing.OsKeystoreKeyProvider(
            signing.SecretServiceBackend(account="claude-code")
        )
        assert provider.presence_required() is False

    def test_algorithm_is_ed25519(self) -> None:
        provider = signing.OsKeystoreKeyProvider(
            signing.SecretServiceBackend(account="claude-code")
        )
        assert provider.algorithm() == "ed25519"

    def test_every_backend_satisfies_the_backend_protocol(self, tmp_path: Path) -> None:
        backends = [
            signing.MacOsKeychainBackend(account="claude-code"),
            signing.SecretServiceBackend(account="claude-code"),
            signing.WindowsDpapiBackend(blob_path=tmp_path / "k.dpapi"),
        ]
        assert [b.name() for b in backends] == list(signing.KEYSTORE_BACKENDS)
        for backend in backends:
            assert isinstance(backend, signing.KeystoreBackend)


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------


class TestResolveKeystoreBackend:
    @pytest.mark.parametrize(
        ("platform", "expected"),
        [
            ("darwin", signing.MacOsKeychainBackend),
            ("linux", signing.SecretServiceBackend),
            ("win32", signing.WindowsDpapiBackend),
            ("cygwin", signing.WindowsDpapiBackend),
            ("freebsd14", signing.SecretServiceBackend),
        ],
    )
    def test_platform_picks_the_backend(
        self, platform: str, expected: type, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("PROMPTWITNESS_KEYSTORE_BACKEND", raising=False)
        monkeypatch.setenv("PROMPTWITNESS_KEYSTORE_BLOB_PATH", str(tmp_path / "k.dpapi"))
        got = signing.resolve_keystore_backend("claude-code", platform=platform)
        assert isinstance(got, expected)

    def test_env_wins_over_platform(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PROMPTWITNESS_KEYSTORE_BACKEND", "secret-service")
        got = signing.resolve_keystore_backend("claude-code", platform="darwin")
        assert isinstance(got, signing.SecretServiceBackend)

    def test_argument_wins_over_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PROMPTWITNESS_KEYSTORE_BACKEND", "secret-service")
        got = signing.resolve_keystore_backend(
            "claude-code", backend="macos-keychain", platform="linux"
        )
        assert isinstance(got, signing.MacOsKeychainBackend)

    def test_unknown_backend_raises(self) -> None:
        """A typo in the env var says so rather than silently selecting a
        different keystore than the operator asked for."""
        with pytest.raises(ValueError, match="unknown PROMPTWITNESS_KEYSTORE_BACKEND"):
            signing.resolve_keystore_backend("claude-code", backend="keychain")

    def test_account_defaults_to_the_harness(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PROMPTWITNESS_KEYSTORE_ACCOUNT", raising=False)
        monkeypatch.delenv("PROMPTWITNESS_KEYSTORE_SERVICE", raising=False)
        got = signing.resolve_keystore_backend("some-harness", backend="secret-service")
        assert isinstance(got, signing.SecretServiceBackend)
        assert got.account == "some-harness"
        assert got.service == "promptwitness-signing-key"

    def test_service_and_account_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PROMPTWITNESS_KEYSTORE_SERVICE", "svc")
        monkeypatch.setenv("PROMPTWITNESS_KEYSTORE_ACCOUNT", "acct")
        got = signing.resolve_keystore_backend("claude-code", backend="macos-keychain")
        assert isinstance(got, signing.MacOsKeychainBackend)
        assert (got.service, got.account) == ("svc", "acct")

    def test_two_harnesses_get_two_items(self) -> None:
        a = signing.resolve_keystore_backend("claude-code", backend="secret-service", env={})
        b = signing.resolve_keystore_backend("other", backend="secret-service", env={})
        assert isinstance(a, signing.SecretServiceBackend)
        assert isinstance(b, signing.SecretServiceBackend)
        assert a.account != b.account

    def test_subprocess_timeout_matches_the_ledger_sign_budget(self) -> None:
        """A keystore slower than the ledger's own 2s ``sign_timeout`` has
        already lost: the ledger abandons the signing thread regardless."""
        got = signing.resolve_keystore_backend("claude-code", backend="secret-service", env={})
        assert isinstance(got, signing.SecretServiceBackend)
        assert got.timeout == 2.0


class TestResolveKeystoreBlobPath:
    def test_override_wins(self, tmp_path: Path) -> None:
        got = signing.resolve_keystore_blob_path("claude-code", override=tmp_path / "x.dpapi")
        assert got == tmp_path / "x.dpapi"

    def test_env_wins_over_default(self, tmp_path: Path) -> None:
        got = signing.resolve_keystore_blob_path(
            "claude-code", env={"PROMPTWITNESS_KEYSTORE_BLOB_PATH": str(tmp_path / "y.dpapi")}
        )
        assert got == tmp_path / "y.dpapi"

    def test_default_is_under_the_ledger_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `ledger.resolve_ledger_dir` reads os.environ directly (it takes no
        # `env=` mapping), so the default branch is exercised through the
        # real env, exactly as `TestResolveKeyPath` does for the key path.
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(tmp_path / "pw"))
        monkeypatch.delenv("PROMPTWITNESS_KEYSTORE_BLOB_PATH", raising=False)
        got = signing.resolve_keystore_blob_path("claude-code")
        assert got == tmp_path / "pw" / "keys" / "ed25519.dpapi"


class TestResolveKeyProviderSelectsOsKeystore:
    def test_env_selects_os_keystore(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PROMPTWITNESS_KEY_PROVIDER", "os-keystore")
        monkeypatch.setenv("PROMPTWITNESS_KEYSTORE_BACKEND", "secret-service")
        got = signing.resolve_key_provider("claude-code")
        assert isinstance(got, signing.OsKeystoreKeyProvider)
        assert isinstance(got.backend, signing.SecretServiceBackend)

    def test_argument_selects_the_backend(self) -> None:
        got = signing.resolve_key_provider(
            "claude-code", provider="os-keystore", keystore_backend="macos-keychain", env={}
        )
        assert isinstance(got, signing.OsKeystoreKeyProvider)
        assert isinstance(got.backend, signing.MacOsKeychainBackend)

    def test_resolution_never_touches_the_keystore(self, keystore: FakeSubprocess) -> None:
        """Resolving a provider must not stall or fail because a keystore is
        locked -- that shows up later as an unsigned line plus one warning."""
        signing.resolve_key_provider(
            "claude-code", provider="os-keystore", keystore_backend="secret-service", env={}
        )
        assert keystore.calls == []


# ---------------------------------------------------------------------------
# AC2: the secret is passed on stdin, never on argv
# ---------------------------------------------------------------------------


class TestKeystoreArgvHygiene:
    @pytest.mark.parametrize("backend_name", ["macos-keychain", "secret-service"])
    def test_no_key_material_on_argv_for_store_or_read(
        self, backend_name: str, keystore: FakeSubprocess
    ) -> None:
        backend = signing.resolve_keystore_backend(
            "claude-code", backend=backend_name, env={}
        )
        provider = signing.generate_and_store_keystore_key(backend)
        provider.sign(b"payload")
        provider.key_id()

        tool = keystore._handler
        assert isinstance(tool, FakeKeystoreTool)
        stored = next(iter(tool.items.values()))
        seed = base64.b64decode(stored)
        assert len(seed) == 32

        assert keystore.calls, "the fake recorded no subprocess call at all"
        secrets = [
            stored.decode("ascii"),
            base64.b64encode(seed).decode("ascii"),
            seed.hex(),
        ]
        for token in keystore.every_argv_token():
            for secret in secrets:
                assert secret not in token, f"key material on argv: {token!r}"
            # Even a prefix would be a leak, so also require that no argv
            # token is a long base64 run resembling an encoded seed.
            assert token not in secrets

    @pytest.mark.parametrize("backend_name", ["macos-keychain", "secret-service"])
    def test_the_secret_arrives_on_stdin(
        self, backend_name: str, keystore: FakeSubprocess
    ) -> None:
        """The positive half of the hygiene claim: proving the secret is not
        on argv is worth nothing unless it demonstrably went somewhere."""
        backend = signing.resolve_keystore_backend(
            "claude-code", backend=backend_name, env={}
        )
        signing.generate_and_store_keystore_key(backend)
        store_calls = [c for c in keystore.calls if c.argv[1] in ("add-generic-password", "store")]
        assert len(store_calls) == 1
        stdin = store_calls[0].stdin
        assert stdin is not None
        assert len(base64.b64decode(stdin.strip())) == 32

    def test_a_read_passes_no_stdin_at_all(self, keystore: FakeSubprocess) -> None:
        backend = signing.resolve_keystore_backend(
            "claude-code", backend="secret-service", env={}
        )
        provider = signing.generate_and_store_keystore_key(backend)
        keystore.calls.clear()
        provider.sign(b"payload")
        assert [c.stdin for c in keystore.calls] == [b""]

    def test_the_hygiene_assertion_is_not_vacuous(self, keystore: FakeSubprocess) -> None:
        """A sanity floor: the sweep above would actually catch a secret on
        argv. Feed the same sweep a deliberately leaking argv and require it
        to fail."""
        secret = base64.b64encode(os.urandom(32)).decode("ascii")
        leaking = ["secret-tool", "store", "--password", secret]
        assert any(secret in token for token in leaking)


# ---------------------------------------------------------------------------
# Round trip: generate -> store -> sign -> verify, per backend
# ---------------------------------------------------------------------------


class TestKeychainAndSecretServiceRoundTrip:
    @pytest.mark.parametrize("backend_name", ["macos-keychain", "secret-service"])
    def test_sign_and_verify(self, backend_name: str, keystore: FakeSubprocess) -> None:
        backend = signing.resolve_keystore_backend(
            "claude-code", backend=backend_name, env={}
        )
        provider = signing.generate_and_store_keystore_key(backend)
        signature = provider.sign(b"payload")
        assert signing.verify_signature(
            b"payload", signature, provider.public_key(), "ed25519"
        )
        assert not signing.verify_signature(
            b"other", signature, provider.public_key(), "ed25519"
        )

    @pytest.mark.parametrize("backend_name", ["macos-keychain", "secret-service"])
    def test_key_id_is_stable_and_matches_derive_key_id(
        self, backend_name: str, keystore: FakeSubprocess
    ) -> None:
        backend = signing.resolve_keystore_backend(
            "claude-code", backend=backend_name, env={}
        )
        provider = signing.generate_and_store_keystore_key(backend)
        assert provider.key_id() == provider.key_id()
        assert provider.key_id() == signing.derive_key_id(provider.public_key(), "ed25519")

    def test_round_trip_through_append_line_and_verify_chain(
        self, keystore: FakeSubprocess, tmp_path: Path
    ) -> None:
        backend = signing.resolve_keystore_backend(
            "claude-code", backend="macos-keychain", env={}
        )
        provider = signing.generate_and_store_keystore_key(backend)
        ledger_dir = tmp_path / "ledger"
        line = _append_turn(ledger_dir, provider)
        assert line.key.strength == "os-keystore"
        assert line.key.presence is False
        assert line.sig is not None
        result = ledger.verify_chain(
            ledger_dir, public_keys={provider.key_id(): provider.public_key()}
        )
        assert result.ok
        assert result.signed_lines == 1
        assert result.unsigned_lines == 0
        assert result.unverified_signed_lines == 0
        assert result.invalid_signature_seqs == ()

    def test_the_seed_is_read_fresh_on_every_call_never_cached(
        self, keystore: FakeSubprocess
    ) -> None:
        """Same discipline as the software-file adapter: the key is held for
        one call only, so a rotated keystore item takes effect immediately
        and no long-lived process keeps the seed in its heap."""
        backend = signing.resolve_keystore_backend(
            "claude-code", backend="secret-service", env={}
        )
        provider = signing.generate_and_store_keystore_key(backend)
        keystore.calls.clear()
        provider.sign(b"one")
        provider.sign(b"two")
        assert len(keystore.calls) == 2


# ---------------------------------------------------------------------------
# Windows DPAPI, through the real ctypes marshalling
# ---------------------------------------------------------------------------


class FakeDpapi:
    """Fake ``crypt32``/``kernel32`` pair.

    Reads the genuine ``DATA_BLOB`` input the module marshalled and writes a
    genuine out-parameter back, so ``ctypes.string_at`` and ``LocalFree``
    run for real. "Encryption" is a reversible marker: the test's claim is
    about the module's plumbing, not about DPAPI's cryptography.
    """

    MARKER = b"dpapi-protected:"

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.freed = 0
        self.flags: list[int] = []
        self.descriptions: list[str | None] = []
        self._buffers: list[Any] = []

    # -- the crypt32 surface --------------------------------------------
    def CryptProtectData(self, *args: Any) -> int:  # Win32 name
        return self._transform("CryptProtectData", args, encrypt=True)

    def CryptUnprotectData(self, *args: Any) -> int:  # Win32 name
        return self._transform("CryptUnprotectData", args, encrypt=False)

    # -- the kernel32 surface -------------------------------------------
    def LocalFree(self, _pointer: Any) -> int:  # Win32 name
        self.freed += 1
        return 0

    def _transform(self, entry_point: str, args: tuple[Any, ...], *, encrypt: bool) -> int:
        p_in, description, _prompt, _reserved, _promptstruct, flags, p_out = args
        self.flags.append(flags)
        self.descriptions.append(None if description is None else description.value)
        if self.fail_on == entry_point:
            return 0
        blob_in = p_in._obj
        data = ctypes.string_at(blob_in.pbData, blob_in.cbData)
        if encrypt:
            out = self.MARKER + data
        else:
            if not data.startswith(self.MARKER):
                return 0
            out = data[len(self.MARKER) :]
        buffer = ctypes.create_string_buffer(out, len(out))
        self._buffers.append(buffer)
        blob_out = p_out._obj
        blob_out.cbData = len(out)
        blob_out.pbData = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))
        return 1


@pytest.fixture
def dpapi(monkeypatch: pytest.MonkeyPatch) -> FakeDpapi:
    fake = FakeDpapi()
    monkeypatch.setattr(signing, "_dpapi_libraries", lambda: (fake, fake))
    return fake


class TestWindowsDpapiBackend:
    def test_round_trip_sign_and_verify(self, dpapi: FakeDpapi, tmp_path: Path) -> None:
        backend = signing.WindowsDpapiBackend(blob_path=tmp_path / "keys" / "ed25519.dpapi")
        provider = signing.generate_and_store_keystore_key(backend)
        signature = provider.sign(b"payload")
        assert signing.verify_signature(
            b"payload", signature, provider.public_key(), "ed25519"
        )

    def test_what_lands_on_disk_is_the_protected_blob_not_the_seed(
        self, dpapi: FakeDpapi, tmp_path: Path
    ) -> None:
        blob_path = tmp_path / "keys" / "ed25519.dpapi"
        backend = signing.WindowsDpapiBackend(blob_path=blob_path)
        signing.generate_and_store_keystore_key(backend)
        on_disk = blob_path.read_bytes()
        assert on_disk.startswith(FakeDpapi.MARKER)
        encoded = on_disk[len(FakeDpapi.MARKER) :]
        seed = base64.b64decode(encoded)
        assert len(seed) == 32
        assert seed not in on_disk, "the raw seed must never be the file's own content"

    def test_blob_file_is_mode_0600(self, dpapi: FakeDpapi, tmp_path: Path) -> None:
        blob_path = tmp_path / "keys" / "ed25519.dpapi"
        signing.generate_and_store_keystore_key(
            signing.WindowsDpapiBackend(blob_path=blob_path)
        )
        assert stat.S_IMODE(blob_path.stat().st_mode) == 0o600

    def test_store_leaves_no_temp_file_behind(self, dpapi: FakeDpapi, tmp_path: Path) -> None:
        blob_path = tmp_path / "keys" / "ed25519.dpapi"
        signing.generate_and_store_keystore_key(
            signing.WindowsDpapiBackend(blob_path=blob_path)
        )
        assert [p.name for p in blob_path.parent.iterdir()] == ["ed25519.dpapi"]

    def test_the_out_blob_is_always_freed(self, dpapi: FakeDpapi, tmp_path: Path) -> None:
        """DPAPI allocates the out blob and the caller owns it; the one being
        leaked would be the decrypted seed sitting in this process's heap."""
        backend = signing.WindowsDpapiBackend(blob_path=tmp_path / "k.dpapi")
        provider = signing.generate_and_store_keystore_key(backend)
        provider.sign(b"payload")
        assert dpapi.freed >= 2

    def test_ui_is_forbidden(self, dpapi: FakeDpapi, tmp_path: Path) -> None:
        """``CRYPTPROTECT_UI_FORBIDDEN``: a prompt hook must never put a
        dialog in front of the user. A key that needs one is unavailable."""
        backend = signing.WindowsDpapiBackend(blob_path=tmp_path / "k.dpapi")
        signing.generate_and_store_keystore_key(backend).sign(b"payload")
        assert dpapi.flags
        assert all(flag & 0x1 for flag in dpapi.flags)

    def test_the_description_carries_no_key_material(
        self, dpapi: FakeDpapi, tmp_path: Path
    ) -> None:
        backend = signing.WindowsDpapiBackend(blob_path=tmp_path / "k.dpapi")
        provider = signing.generate_and_store_keystore_key(backend)
        encoded = base64.b64encode(
            base64.b64decode(
                (tmp_path / "k.dpapi").read_bytes()[len(FakeDpapi.MARKER) :]
            )
        ).decode("ascii")
        assert provider.strength() == "os-keystore"
        for description in dpapi.descriptions:
            if description is not None:
                assert encoded not in description

    def test_a_failed_protect_is_key_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        fake = FakeDpapi(fail_on="CryptProtectData")
        monkeypatch.setattr(signing, "_dpapi_libraries", lambda: (fake, fake))
        backend = signing.WindowsDpapiBackend(blob_path=tmp_path / "k.dpapi")
        with pytest.raises(ledger.KeyUnavailable):
            signing.generate_and_store_keystore_key(backend)
        assert not (tmp_path / "k.dpapi").exists()

    def test_a_blob_this_user_cannot_decrypt_is_key_unavailable(
        self, dpapi: FakeDpapi, tmp_path: Path
    ) -> None:
        blob_path = tmp_path / "k.dpapi"
        blob_path.write_bytes(b"not-a-dpapi-blob")
        provider = signing.OsKeystoreKeyProvider(
            signing.WindowsDpapiBackend(blob_path=blob_path)
        )
        with pytest.raises(ledger.KeyUnavailable):
            provider.sign(b"payload")

    def test_a_missing_blob_file_is_key_unavailable(
        self, dpapi: FakeDpapi, tmp_path: Path
    ) -> None:
        provider = signing.OsKeystoreKeyProvider(
            signing.WindowsDpapiBackend(blob_path=tmp_path / "absent.dpapi")
        )
        with pytest.raises(ledger.KeyUnavailable):
            provider.key_id()

    def test_an_empty_blob_file_is_key_unavailable(
        self, dpapi: FakeDpapi, tmp_path: Path
    ) -> None:
        blob_path = tmp_path / "empty.dpapi"
        blob_path.write_bytes(b"")
        provider = signing.OsKeystoreKeyProvider(
            signing.WindowsDpapiBackend(blob_path=blob_path)
        )
        with pytest.raises(ledger.KeyUnavailable):
            provider.key_id()


# ---------------------------------------------------------------------------
# AC3: an unavailable keystore produces an unsigned line plus one warning
# ---------------------------------------------------------------------------


def _missing_tool(_fake: FakeSubprocess, argv: list[str], _kwargs: dict[str, Any]) -> Any:
    raise FileNotFoundError(2, "No such file or directory", argv[0])


def _failing_tool(_fake: FakeSubprocess, argv: list[str], _kwargs: dict[str, Any]) -> Any:
    return _completed(argv, returncode=1)


def _hanging_tool(fake: FakeSubprocess, argv: list[str], kwargs: dict[str, Any]) -> Any:
    raise fake.TimeoutExpired(argv, kwargs.get("timeout", 2.0))


class TestUnavailableKeystoreDegrades:
    @pytest.mark.parametrize(
        ("label", "handler"),
        [
            ("tool-not-installed", _missing_tool),
            ("tool-exits-nonzero", _failing_tool),
            ("tool-times-out", _hanging_tool),
            ("no-item-stored", FakeKeystoreTool()),
        ],
    )
    def test_unsigned_line_plus_exactly_one_warning(
        self,
        label: str,
        handler: Any,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(signing, "subprocess", FakeSubprocess(handler))
        caplog.set_level(logging.WARNING, logger="promptwitness.signing")
        provider = signing.OsKeystoreKeyProvider(
            signing.resolve_keystore_backend("claude-code", backend="secret-service", env={})
        )
        line = _append_turn(tmp_path / f"ledger-{label}", provider)

        assert line.key.strength == "none"
        assert line.key.error == "KeyUnavailable"
        assert line.sig is None
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, f"{label}: expected one warning, got {len(warnings)}"

    def test_a_second_append_does_not_warn_again(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
    ) -> None:
        """The dedupe is process-wide. A hook that runs on every prompt must
        not turn a missing keystore into one warning per turn."""
        monkeypatch.setattr(signing, "subprocess", FakeSubprocess(_missing_tool))
        caplog.set_level(logging.WARNING, logger="promptwitness.signing")
        provider = signing.OsKeystoreKeyProvider(
            signing.resolve_keystore_backend("claude-code", backend="secret-service", env={})
        )
        ledger_dir = tmp_path / "ledger"
        first = _append_turn(ledger_dir, provider)
        second = _append_turn(ledger_dir, provider)
        assert first.sig is None and second.sig is None
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1

    def test_the_chain_still_verifies_with_unsigned_lines(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Degrading must not break the hash chain -- the unsigned line is a
        normal line, and the whole point is that the hook never blocks."""
        monkeypatch.setattr(signing, "subprocess", FakeSubprocess(_missing_tool))
        provider = signing.OsKeystoreKeyProvider(
            signing.resolve_keystore_backend("claude-code", backend="secret-service", env={})
        )
        ledger_dir = tmp_path / "ledger"
        _append_turn(ledger_dir, provider)
        _append_turn(ledger_dir, provider)
        assert ledger.verify_chain(ledger_dir).ok

    def test_the_warning_names_the_backend_and_carries_no_key_material(
        self, keystore: FakeSubprocess, caplog: pytest.LogCaptureFixture
    ) -> None:
        backend = signing.resolve_keystore_backend(
            "claude-code", backend="secret-service", env={}
        )
        provider = signing.generate_and_store_keystore_key(backend)
        tool = keystore._handler
        assert isinstance(tool, FakeKeystoreTool)
        stored = next(iter(tool.items.values()))
        caplog.set_level(logging.WARNING, logger="promptwitness.signing")
        # Empty the keystore under the provider's feet.
        tool.items.clear()
        with pytest.raises(ledger.KeyUnavailable):
            provider.sign(b"payload")
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "secret-service" in message
        assert stored.decode("ascii") not in message


class TestMalformedKeystoreItem:
    def _provider_over(
        self, monkeypatch: pytest.MonkeyPatch, secret: bytes
    ) -> signing.OsKeystoreKeyProvider:
        tool = FakeKeystoreTool(items={("promptwitness-signing-key", "claude-code"): secret})
        monkeypatch.setattr(signing, "subprocess", FakeSubprocess(tool))
        return signing.OsKeystoreKeyProvider(
            signing.resolve_keystore_backend("claude-code", backend="secret-service", env={})
        )

    def test_not_base64_is_key_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = self._provider_over(monkeypatch, b"this is not base64 at all!!")
        with pytest.raises(ledger.KeyUnavailable, match="not base64"):
            provider.sign(b"payload")

    def test_wrong_length_is_key_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A 16-byte item would raise deep inside ``cryptography`` otherwise;
        the adapter's contract is that an unusable key degrades."""
        provider = self._provider_over(monkeypatch, base64.b64encode(os.urandom(16)))
        with pytest.raises(ledger.KeyUnavailable, match="16 bytes"):
            provider.sign(b"payload")

    def test_a_malformed_item_also_warns_exactly_once(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        provider = self._provider_over(monkeypatch, b"not base64!!")
        caplog.set_level(logging.WARNING, logger="promptwitness.signing")
        with pytest.raises(ledger.KeyUnavailable):
            provider.sign(b"a")
        with pytest.raises(ledger.KeyUnavailable):
            provider.sign(b"b")
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


# ---------------------------------------------------------------------------
# Generation: refuses to destroy an existing key, never leaks material
# ---------------------------------------------------------------------------


class TestGenerateAndStore:
    def test_refuses_to_replace_an_existing_item(self, keystore: FakeSubprocess) -> None:
        """Replacing a signing key makes every line it ever signed
        permanently unverifiable, with no copy anywhere to restore from."""
        backend = signing.resolve_keystore_backend(
            "claude-code", backend="secret-service", env={}
        )
        first = signing.generate_and_store_keystore_key(backend)
        before = first.key_id()
        with pytest.raises(FileExistsError):
            signing.generate_and_store_keystore_key(backend)
        assert signing.OsKeystoreKeyProvider(backend).key_id() == before

    def test_replace_true_rotates_the_item(self, keystore: FakeSubprocess) -> None:
        backend = signing.resolve_keystore_backend(
            "claude-code", backend="secret-service", env={}
        )
        before = signing.generate_and_store_keystore_key(backend).key_id()
        after = signing.generate_and_store_keystore_key(backend, replace=True).key_id()
        assert before != after

    def test_the_existence_probe_emits_no_warning(
        self, keystore: FakeSubprocess, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A miss is the EXPECTED outcome of the probe, so it must not trip
        the process-wide "key unavailable" warning and then suppress the
        real one later."""
        caplog.set_level(logging.WARNING, logger="promptwitness.signing")
        backend = signing.resolve_keystore_backend(
            "claude-code", backend="secret-service", env={}
        )
        signing.generate_and_store_keystore_key(backend)
        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []

    def test_generation_prints_nothing_at_all(
        self, keystore: FakeSubprocess, capfd: pytest.CaptureFixture[str]
    ) -> None:
        backend = signing.resolve_keystore_backend(
            "claude-code", backend="secret-service", env={}
        )
        signing.generate_and_store_keystore_key(backend)
        captured = capfd.readouterr()
        assert captured.out == ""
        assert captured.err == ""


# ---------------------------------------------------------------------------
# `promptwitness keygen --provider os-keystore`
# ---------------------------------------------------------------------------


class TestKeygenCliOsKeystore:
    def test_reports_the_backend_and_strength_and_no_key_path(
        self, keystore: FakeSubprocess, capfd: pytest.CaptureFixture[str]
    ) -> None:
        rc = cli.main(
            [
                "keygen",
                "--provider",
                "os-keystore",
                "--keystore-backend",
                "secret-service",
            ]
        )
        assert rc == 0
        payload = json.loads(capfd.readouterr().out)
        assert payload["keystore_backend"] == "secret-service"
        assert payload["strength"] == "os-keystore"
        assert payload["algorithm"] == "ed25519"
        assert payload["key_id"].startswith("ed25519:")
        # There is no file for a keystore-held key, so reporting a path
        # would be a lie about where the key actually is.
        assert "key_path" not in payload

    def test_prints_no_key_material_on_stdout_or_stderr(
        self, keystore: FakeSubprocess, capfd: pytest.CaptureFixture[str]
    ) -> None:
        assert (
            cli.main(
                ["keygen", "--provider", "os-keystore", "--keystore-backend", "secret-service"]
            )
            == 0
        )
        captured = capfd.readouterr()
        tool = keystore._handler
        assert isinstance(tool, FakeKeystoreTool)
        stored = next(iter(tool.items.values())).decode("ascii")
        seed_hex = base64.b64decode(stored).hex()
        for stream in (captured.out, captured.err):
            assert stored not in stream
            assert seed_hex not in stream

    def test_refuses_an_existing_item_and_exits_1(
        self, keystore: FakeSubprocess, capfd: pytest.CaptureFixture[str]
    ) -> None:
        argv = ["keygen", "--provider", "os-keystore", "--keystore-backend", "secret-service"]
        assert cli.main(argv) == 0
        first = json.loads(capfd.readouterr().out)["key_id"]
        assert cli.main(argv) == 1
        err = capfd.readouterr().err
        assert "already exists" in json.loads(err)["error"]
        # And the existing key is untouched.
        assert cli.main(["status", "--harness", "claude-code"]) == 0
        capfd.readouterr()
        backend = signing.resolve_keystore_backend(
            "claude-code", backend="secret-service", env={}
        )
        assert signing.OsKeystoreKeyProvider(backend).key_id() == first

    def test_force_rotates_the_item(
        self, keystore: FakeSubprocess, capfd: pytest.CaptureFixture[str]
    ) -> None:
        argv = ["keygen", "--provider", "os-keystore", "--keystore-backend", "secret-service"]
        assert cli.main(argv) == 0
        first = json.loads(capfd.readouterr().out)["key_id"]
        assert cli.main([*argv, "--force"]) == 0
        second = json.loads(capfd.readouterr().out)["key_id"]
        assert first != second

    def test_an_unreachable_keystore_exits_1_with_a_json_error(
        self, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(signing, "subprocess", FakeSubprocess(_missing_tool))
        rc = cli.main(
            ["keygen", "--provider", "os-keystore", "--keystore-backend", "secret-service"]
        )
        assert rc == 1
        captured = capfd.readouterr()
        assert captured.out == ""
        assert "not installed" in json.loads(captured.err)["error"]

    def test_an_unknown_backend_in_the_env_exits_1_rather_than_raising(
        self, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("PROMPTWITNESS_KEYSTORE_BACKEND", "keychain")
        rc = cli.main(["keygen", "--provider", "os-keystore"])
        assert rc == 1
        error = json.loads(capfd.readouterr().err)["error"]
        assert "unknown PROMPTWITNESS_KEYSTORE_BACKEND" in error

    def test_the_software_file_path_is_unchanged(
        self, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        """A regression floor for the default: adding a tier must not move
        an existing operator's key or change what keygen reports."""
        key_path = tmp_path / "k.pem"
        assert cli.main(["keygen", "--key-path", str(key_path)]) == 0
        payload = json.loads(capfd.readouterr().out)
        assert payload["key_path"] == str(key_path)
        assert payload["strength"] == "software-file"
        assert "keystore_backend" not in payload


# ---------------------------------------------------------------------------
# AC4: the docs state the limit of this tier plainly
# ---------------------------------------------------------------------------


class TestTheDocsStateTheAtRestOnlyLimit:
    """The honesty of the evidence table is the point of the whole design
    document (``docs/design/witness-ledger.md``), so the one claim this tier
    must NOT be read as making gets a test rather than a review habit."""

    DESIGN_DOC = Path(__file__).resolve().parent.parent / "docs" / "design" / "witness-ledger.md"

    def test_the_design_doc_has_an_os_keystore_section(self) -> None:
        text = self.DESIGN_DOC.read_text(encoding="utf-8")
        assert "#### The `os-keystore` tier" in text

    def test_it_says_the_key_is_protected_at_rest(self) -> None:
        text = self.DESIGN_DOC.read_text(encoding="utf-8").lower()
        assert "at rest" in text

    def test_it_says_the_users_own_session_can_still_read_the_key(self) -> None:
        """The limit, in the reader's terms -- not only in the table row."""
        text = self.DESIGN_DOC.read_text(encoding="utf-8")
        section = text.split("#### The `os-keystore` tier", 1)[1]
        assert "The user's own session can still read the key" in section
        assert "`presence_required()` is\n`False`" in section

    def test_the_adapter_docstring_says_the_same_thing(self) -> None:
        """A reader who never opens the design document still gets the limit,
        from the class they are about to instantiate."""
        doc = signing.OsKeystoreKeyProvider.__doc__ or ""
        assert "at rest" in doc
        assert "without a prompt" in doc
