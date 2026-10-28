# SPDX-License-Identifier: Apache-2.0
"""Tests for promptwitness.signing (athenaeum#2150).

Every path builds filesystem paths from ``tmp_path`` and monkeypatched env
vars only -- never a hardcoded machine-specific home-directory literal (the
public-safe-lint gate rejects that shape even in test fixtures).
"""

from __future__ import annotations

import base64
import builtins
import json
import logging
import os
import stat
from pathlib import Path

import pytest

from promptwitness import canon, ledger, signing

# ---------------------------------------------------------------------------
# Protocol shape
# ---------------------------------------------------------------------------


class TestProtocolCompatibility:
    def test_key_provider_is_structurally_a_key_provider_like(self) -> None:
        """Every KeyProvider must satisfy ledger.KeyProviderLike (the
        minimal shape the ledger actually calls) -- asserted with
        isinstance, not left to inspection."""
        provider = signing.NoneKeyProvider()
        assert isinstance(provider, signing.KeyProvider)
        assert isinstance(provider, ledger.KeyProviderLike)

    def test_software_file_provider_also_satisfies_both(self, tmp_path: Path) -> None:
        provider = signing.SoftwareFileKeyProvider(tmp_path / "k.pem")
        assert isinstance(provider, signing.KeyProvider)
        assert isinstance(provider, ledger.KeyProviderLike)

    def test_key_provider_has_public_key_that_key_provider_like_lacks(self) -> None:
        assert not hasattr(ledger.KeyProviderLike, "public_key")
        provider = signing.NoneKeyProvider()
        assert provider.public_key() == b""


# ---------------------------------------------------------------------------
# key_id parity with athenaeum.receipt_sealers.derive_key_id
# ---------------------------------------------------------------------------


class TestDeriveKeyIdParity:
    @pytest.mark.parametrize(
        "raw",
        (
            b"\x00" * 32,
            b"\xff" * 32,
            bytes(range(32)),
        ),
    )
    def test_matches_receipt_sealers_derive_key_id(self, raw: bytes) -> None:
        from athenaeum.receipt_sealers import derive_key_id as athenaeum_derive_key_id

        assert signing.derive_key_id(raw, "ed25519") == athenaeum_derive_key_id(raw)

    def test_shape(self) -> None:
        key_id = signing.derive_key_id(b"\x01" * 32, "ed25519")
        assert key_id.startswith("ed25519:")
        assert len(key_id) == len("ed25519:") + 16

    def test_parity_is_not_vacuous(self) -> None:
        from athenaeum.receipt_sealers import derive_key_id as athenaeum_derive_key_id

        raw = b"\x42" * 32
        mine = signing.derive_key_id(raw, "ed25519")
        theirs = athenaeum_derive_key_id(raw)
        assert mine == theirs
        assert mine != signing.derive_key_id(b"\x43" * 32, "ed25519")


# ---------------------------------------------------------------------------
# Strength vocabulary and presence_required
# ---------------------------------------------------------------------------


class TestStrengthVocabulary:
    def test_vocabulary_order(self) -> None:
        assert signing.STRENGTHS == (
            "none",
            "software-file",
            "os-keystore",
            "hardware-nonexportable",
            "hardware-presence",
        )

    def test_none_adapter_presence_required_false(self) -> None:
        assert signing.NoneKeyProvider().presence_required() is False

    def test_software_file_adapter_presence_required_false(self, tmp_path: Path) -> None:
        assert signing.SoftwareFileKeyProvider(tmp_path / "k.pem").presence_required() is False

    def test_none_adapter_strength(self) -> None:
        assert signing.NoneKeyProvider().strength() == "none"

    def test_software_file_adapter_strength(self, tmp_path: Path) -> None:
        assert signing.SoftwareFileKeyProvider(tmp_path / "k.pem").strength() == "software-file"

    def test_none_adapter_never_signs(self) -> None:
        with pytest.raises(ledger.KeyUnavailable):
            signing.NoneKeyProvider().sign(b"payload")


# ---------------------------------------------------------------------------
# software-file round trip
# ---------------------------------------------------------------------------


class TestSoftwareFileRoundTrip:
    def test_generate_write_sign_verify(self, tmp_path: Path) -> None:
        provider = signing.generate_and_write_key(tmp_path / "k.pem")
        sig = provider.sign(b"hello world")
        assert signing.verify_signature(b"hello world", sig, provider.public_key(), "ed25519")

    def test_altering_payload_invalidates_signature(self, tmp_path: Path) -> None:
        provider = signing.generate_and_write_key(tmp_path / "k.pem")
        sig = provider.sign(b"hello world")
        assert not signing.verify_signature(b"hello WORLD", sig, provider.public_key(), "ed25519")

    def test_round_trip_through_append_line_and_verify_chain(self, tmp_path: Path) -> None:
        provider = signing.generate_and_write_key(tmp_path / "k.pem")
        ledger_dir = tmp_path / "ledger"
        line = ledger.append_line(
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
        assert line.key.strength == "software-file"
        assert line.sig is not None
        verification = ledger.verify_chain(
            ledger_dir, public_keys={provider.key_id(): provider.public_key()}
        )
        assert verification.ok
        assert verification.signed_lines == 1
        assert verification.unsigned_lines == 0
        assert verification.unverified_signed_lines == 0
        assert verification.invalid_signature_seqs == ()

    def test_altering_a_single_field_invalidates_the_signature(self, tmp_path: Path) -> None:
        """AC: 'A software-file round-trip verifies. Altering a single
        field invalidates the signature.'"""
        provider = signing.generate_and_write_key(tmp_path / "k.pem")
        ledger_dir = tmp_path / "ledger"
        ledger.append_line(
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
        partition = next(ledger_dir.glob("*.jsonl"))
        raw = partition.read_text(encoding="utf-8")
        tampered = raw.replace('"resumed":false', '"resumed":true')
        assert tampered != raw, "the fixture did not actually change anything"
        partition.write_text(tampered, encoding="utf-8")

        verification = ledger.verify_chain(
            ledger_dir, public_keys={provider.key_id(): provider.public_key()}
        )
        assert verification.invalid_signature_seqs == (1,)


# ---------------------------------------------------------------------------
# Degrade paths: missing key file / cryptography absent
# ---------------------------------------------------------------------------


class TestSoftwareFileDegradePaths:
    def setup_method(self) -> None:
        signing.reset_warned_for_tests()

    def teardown_method(self) -> None:
        signing.reset_warned_for_tests()

    def test_missing_key_file_leads_to_unsigned_line_never_raises(self, tmp_path: Path) -> None:
        provider = signing.SoftwareFileKeyProvider(tmp_path / "does-not-exist.pem")
        ledger_dir = tmp_path / "ledger"
        line = ledger.append_line(
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
        assert line.key.strength == "none"
        assert line.key.error == "KeyUnavailable"
        assert line.sig is None

    def test_cryptography_not_installed_leads_to_unsigned_line_never_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulate ``cryptography`` missing by making every import whose
        top-level name is ``cryptography`` raise ImportError -- cryptography
        IS installed in dev/CI, so this patches the import site rather than
        skipping the test."""
        key_path = tmp_path / "k.pem"
        signing.generate_and_write_key(key_path)  # needs the real library once

        real_import = builtins.__import__

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "cryptography" or name.startswith("cryptography."):
                raise ImportError("simulated: cryptography is not installed")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(builtins, "__import__", fake_import)

        provider = signing.SoftwareFileKeyProvider(key_path)
        ledger_dir = tmp_path / "ledger"
        line = ledger.append_line(
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
        assert line.key.strength == "none"
        assert line.key.error == "KeyUnavailable"
        assert line.sig is None


class TestWarnOnce:
    def setup_method(self) -> None:
        signing.reset_warned_for_tests()

    def teardown_method(self) -> None:
        signing.reset_warned_for_tests()

    def test_exactly_one_warning_per_process_across_two_failures(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        provider = signing.SoftwareFileKeyProvider(tmp_path / "missing.pem")
        caplog.set_level(logging.WARNING, logger="promptwitness.signing")
        with pytest.raises(ledger.KeyUnavailable):
            provider.sign(b"first")
        with pytest.raises(ledger.KeyUnavailable):
            provider.sign(b"second")
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1

    def test_a_second_distinct_provider_instance_still_only_warns_once(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The dedupe is process-wide, not per-instance: a fresh provider
        constructed after the first failure must not re-warn."""
        caplog.set_level(logging.WARNING, logger="promptwitness.signing")
        p1 = signing.SoftwareFileKeyProvider(tmp_path / "missing-1.pem")
        p2 = signing.SoftwareFileKeyProvider(tmp_path / "missing-2.pem")
        with pytest.raises(ledger.KeyUnavailable):
            p1.sign(b"x")
        with pytest.raises(ledger.KeyUnavailable):
            p2.sign(b"y")
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1


# ---------------------------------------------------------------------------
# keygen: mode 0600, never leaks key material
# ---------------------------------------------------------------------------


class TestKeygenFilePermissions:
    def test_generated_key_file_is_mode_0600(self, tmp_path: Path) -> None:
        key_path = tmp_path / "k.pem"
        signing.generate_and_write_key(key_path)
        mode = stat.S_IMODE(key_path.stat().st_mode)
        assert mode == 0o600

    def test_refuses_to_overwrite_an_existing_key(self, tmp_path: Path) -> None:
        key_path = tmp_path / "k.pem"
        signing.generate_and_write_key(key_path)
        with pytest.raises(FileExistsError):
            signing.generate_and_write_key(key_path)


class TestKeygenNeverLeaksKeyMaterial:
    def test_cli_keygen_prints_no_key_material(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """AC: 'Its key material never appears in stdout, stderr or logs; a
        test captures all three.'

        The only BEGIN/END banner pair that may ever exist is the one
        ``cryptography``'s own ``private_bytes`` writes straight to the key
        FILE -- never to stdout, stderr, or a log record."""
        from promptwitness import cli

        caplog.set_level(logging.DEBUG)
        key_path = tmp_path / "k.pem"
        rc = cli.main(["keygen", "--key-path", str(key_path)])
        assert rc == 0

        captured = capsys.readouterr()
        log_text = "\n".join(r.getMessage() for r in caplog.records)

        key_material = key_path.read_bytes()
        assert b"-----BEGIN" in key_material and b"-----END" in key_material

        for surface_name, surface in (
            ("stdout", captured.out),
            ("stderr", captured.err),
            ("logs", log_text),
        ):
            assert "BEGIN" not in surface, f"key banner leaked into {surface_name}"
            assert "END PRIVATE KEY" not in surface, f"key banner leaked into {surface_name}"

    def test_direct_generate_and_write_key_prints_nothing(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.DEBUG)
        signing.generate_and_write_key(tmp_path / "k.pem")
        captured = capsys.readouterr()
        log_text = "\n".join(r.getMessage() for r in caplog.records)
        assert captured.out == ""
        assert captured.err == ""
        assert "BEGIN" not in log_text


# ---------------------------------------------------------------------------
# resolve_key_path / resolve_key_provider
# ---------------------------------------------------------------------------


class TestResolveKeyPath:
    def test_override_wins(self, tmp_path: Path) -> None:
        got = signing.resolve_key_path("claude-code", override=tmp_path / "explicit.pem")
        assert got == tmp_path / "explicit.pem"

    def test_env_wins_over_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PROMPTWITNESS_KEY_PATH", str(tmp_path / "from-env.pem"))
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(tmp_path / "ledgerdir"))
        got = signing.resolve_key_path("claude-code")
        assert got == tmp_path / "from-env.pem"

    def test_default_is_under_ledger_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PROMPTWITNESS_KEY_PATH", raising=False)
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(tmp_path / "ledgerdir"))
        got = signing.resolve_key_path("claude-code")
        assert got == tmp_path / "ledgerdir" / "keys" / "ed25519.pem"


class TestResolveKeyProvider:
    def test_none_returns_none(self) -> None:
        assert signing.resolve_key_provider("claude-code", provider="none") is None

    def test_default_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PROMPTWITNESS_KEY_PROVIDER", raising=False)
        assert signing.resolve_key_provider("claude-code") is None

    def test_env_selects_software_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROMPTWITNESS_KEY_PROVIDER", "software-file")
        got = signing.resolve_key_provider("claude-code", key_path=tmp_path / "k.pem")
        assert isinstance(got, signing.SoftwareFileKeyProvider)
        assert got.key_path == tmp_path / "k.pem"

    def test_argument_wins_over_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PROMPTWITNESS_KEY_PROVIDER", "software-file")
        got = signing.resolve_key_provider("claude-code", provider="none")
        assert got is None

    def test_unknown_provider_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown PROMPTWITNESS_KEY_PROVIDER"):
            signing.resolve_key_provider("claude-code", provider="hardware-presence")


# ---------------------------------------------------------------------------
# verify_chain signature-status extension
# ---------------------------------------------------------------------------


def _append_turn(d: Path, **kwargs: object) -> ledger.WitnessLine:
    defaults: dict[str, object] = {
        "kind": "turn",
        "harness": "claude-code",
        "harness_version": "2.1.308",
        "hook_version": "promptwitness 0.1.0",
        "session": "s1",
        "turn": 0,
        "prompt_id": "p0",
        "prompt_kind": "human",
        "prompt_digest": canon.digest(canon.normalize("hi")),
        "raw_digest": canon.digest("hi"),
        "prompt_chars": 2,
        "channel": "interactive",
        "resumed": False,
        "signals": {"entrypoint": "cli"},
    }
    defaults.update(kwargs)
    return ledger.append_line(d, **defaults)  # type: ignore[arg-type]


class TestVerifyChainSignatureStatus:
    def test_defaults_preserve_pre_2150_behaviour(self, tmp_path: Path) -> None:
        _append_turn(tmp_path, prompt_id="p0", turn=0)
        _append_turn(tmp_path, prompt_id="p1", turn=1)
        v = ledger.verify_chain(tmp_path)
        assert v.ok
        assert v.lines_checked == 2
        assert v.unsigned_lines == 2
        assert v.signed_lines == 0
        assert v.invalid_signature_seqs == ()

    def test_mixed_signed_and_unsigned_lines_are_counted(self, tmp_path: Path) -> None:
        provider = signing.generate_and_write_key(tmp_path / "k.pem")
        _append_turn(tmp_path, prompt_id="p0", turn=0)  # unsigned
        _append_turn(tmp_path, prompt_id="p1", turn=1, key_provider=provider)  # signed
        v = ledger.verify_chain(tmp_path)
        assert v.lines_checked == 2
        assert v.unsigned_lines == 1
        assert v.signed_lines == 1
        assert v.unverified_signed_lines == 1  # no public_keys supplied

    def test_signed_line_verifies_when_public_key_supplied(self, tmp_path: Path) -> None:
        provider = signing.generate_and_write_key(tmp_path / "k.pem")
        _append_turn(tmp_path, prompt_id="p0", turn=0, key_provider=provider)
        v = ledger.verify_chain(tmp_path, public_keys={provider.key_id(): provider.public_key()})
        assert v.ok
        assert v.unverified_signed_lines == 0
        assert v.invalid_signature_seqs == ()

    def test_tampered_signed_line_is_reported_invalid_not_ok_false(self, tmp_path: Path) -> None:
        """Signature validity is reported separately from chain ``ok`` --
        tampering a signed line's own content breaks ITS signature but does
        not, by itself, retroactively break the hash chain that only checks
        ``prev`` on the NEXT line."""
        provider = signing.generate_and_write_key(tmp_path / "k.pem")
        _append_turn(tmp_path, prompt_id="p0", turn=0, key_provider=provider)
        _append_turn(tmp_path, prompt_id="p1", turn=1, key_provider=provider)
        partition = next(tmp_path.glob("*.jsonl"))
        lines = partition.read_text(encoding="utf-8").splitlines()
        import json as _json

        first = _json.loads(lines[0])
        first["prompt_chars"] = 999999
        lines[0] = _json.dumps(first, sort_keys=True, separators=(",", ":"))
        partition.write_text("\n".join(lines) + "\n", encoding="utf-8")

        v = ledger.verify_chain(tmp_path, public_keys={provider.key_id(): provider.public_key()})
        assert 1 in v.invalid_signature_seqs

    def test_unknown_key_id_counts_as_unverified_not_invalid(self, tmp_path: Path) -> None:
        provider = signing.generate_and_write_key(tmp_path / "k.pem")
        _append_turn(tmp_path, prompt_id="p0", turn=0, key_provider=provider)
        other = signing.generate_and_write_key(tmp_path / "other.pem")
        v = ledger.verify_chain(tmp_path, public_keys={other.key_id(): other.public_key()})
        assert v.unverified_signed_lines == 1
        assert v.invalid_signature_seqs == ()


# ---------------------------------------------------------------------------
# key-rotate: a line signed by both keys
# ---------------------------------------------------------------------------


class TestKeyRotate:
    def test_key_rotate_line_verifies_under_both_keys(self, tmp_path: Path) -> None:
        old = signing.generate_and_write_key(tmp_path / "old.pem")
        new = signing.generate_and_write_key(tmp_path / "new.pem")
        line = signing.append_key_rotate_line(
            tmp_path,
            old_provider=old,
            new_provider=new,
            harness="claude-code",
            harness_version="1",
            hook_version="promptwitness 0.1.0",
        )
        assert line.kind == "key-rotate"
        new_ok, prior_ok = signing.verify_key_rotate_signatures(
            line, old_public_key=old.public_key(), new_public_key=new.public_key()
        )
        assert new_ok
        assert prior_ok

    def test_key_rotate_line_signature_verifies_through_verify_chain(
        self, tmp_path: Path
    ) -> None:
        """The sign side and the verify side must agree on which fields the
        signature covers.

        They did not. The appender serializes the payload BEFORE writing
        `sig` and before the `extra_signers` loop writes `prior_sig`, so a
        countersignature is outside the signed bytes by construction.
        `verify_chain` rebuilt the payload dropping only `sig`, so it fed the
        verifier a dict that still contained `prior_sig` -- and a perfectly
        valid `key-rotate` line's own signature could never verify. The
        sibling test below passes no `public_keys`, which is why it did not
        catch this: with no keys supplied, nothing is verified at all.
        """
        old = signing.generate_and_write_key(tmp_path / "old.pem")
        new = signing.generate_and_write_key(tmp_path / "new.pem")
        line = signing.append_key_rotate_line(
            tmp_path,
            old_provider=old,
            new_provider=new,
            harness="claude-code",
            harness_version="1",
            hook_version="promptwitness 0.1.0",
        )

        v = ledger.verify_chain(tmp_path, public_keys={new.key_id(): new.public_key()})

        assert v.invalid_signature_seqs == ()
        assert v.unverified_signed_lines == 0
        assert v.signed_lines == 1
        assert line.kind == "key-rotate"

    def test_a_failed_prior_signer_does_not_invalidate_the_new_key_signature(
        self, tmp_path: Path
    ) -> None:
        """When the OLD key is unavailable, `prior_sig_error` is written --
        also after the payload is serialized.

        A verifier carrying its own field list would exclude `prior_sig` and
        include `prior_sig_error`, so the new key's signature would fail on a
        rotation that is otherwise fine. This is the second shape of the same
        drift, which is why there is now one `ledger.signed_payload`.
        """

        class _UnavailableOldKey:
            def key_id(self) -> str:
                return "ed25519:0000000000000000"

            def algorithm(self) -> str:
                return "ed25519"

            def strength(self) -> str:
                return "software-file"

            def presence_required(self) -> bool:
                return False

            def public_key(self) -> bytes:
                return b"\x00" * 32

            def sign(self, payload: bytes) -> bytes:
                raise ledger.KeyUnavailable("old key is gone")

        new = signing.generate_and_write_key(tmp_path / "new.pem")
        line = signing.append_key_rotate_line(
            tmp_path,
            old_provider=_UnavailableOldKey(),
            new_provider=new,
            harness="claude-code",
            harness_version="1",
            hook_version="promptwitness 0.1.0",
        )

        assert line.to_dict().get("prior_sig") is None
        assert line.to_dict().get("prior_sig_error") == "KeyUnavailable"

        v = ledger.verify_chain(tmp_path, public_keys={new.key_id(): new.public_key()})
        assert v.invalid_signature_seqs == ()

        new_ok, prior_ok = signing.verify_key_rotate_signatures(
            line, old_public_key=b"\x00" * 32, new_public_key=new.public_key()
        )
        assert new_ok
        assert not prior_ok

    def test_key_rotate_line_is_accepted_by_the_chain_verifier(self, tmp_path: Path) -> None:
        old = signing.generate_and_write_key(tmp_path / "old.pem")
        new = signing.generate_and_write_key(tmp_path / "new.pem")
        signing.append_key_rotate_line(
            tmp_path,
            old_provider=old,
            new_provider=new,
            harness="claude-code",
            harness_version="1",
            hook_version="promptwitness 0.1.0",
        )
        assert ledger.verify_chain(tmp_path).ok

    def test_tampering_new_sig_only_breaks_new_verification_only(self, tmp_path: Path) -> None:
        old = signing.generate_and_write_key(tmp_path / "old.pem")
        new = signing.generate_and_write_key(tmp_path / "new.pem")
        line = signing.append_key_rotate_line(
            tmp_path,
            old_provider=old,
            new_provider=new,
            harness="claude-code",
            harness_version="1",
            hook_version="promptwitness 0.1.0",
        )
        d = line.to_dict()
        d["sig"] = base64.b64encode(b"\x00" * 64).decode("ascii")
        tampered = ledger.WitnessLine.from_dict(d)
        new_ok, prior_ok = signing.verify_key_rotate_signatures(
            tampered, old_public_key=old.public_key(), new_public_key=new.public_key()
        )
        assert not new_ok
        assert prior_ok

    def test_tampering_prior_sig_only_breaks_prior_verification_only(self, tmp_path: Path) -> None:
        old = signing.generate_and_write_key(tmp_path / "old.pem")
        new = signing.generate_and_write_key(tmp_path / "new.pem")
        line = signing.append_key_rotate_line(
            tmp_path,
            old_provider=old,
            new_provider=new,
            harness="claude-code",
            harness_version="1",
            hook_version="promptwitness 0.1.0",
        )
        d = line.to_dict()
        d["prior_sig"] = base64.b64encode(b"\x00" * 64).decode("ascii")
        tampered = ledger.WitnessLine.from_dict(d)
        new_ok, prior_ok = signing.verify_key_rotate_signatures(
            tampered, old_public_key=old.public_key(), new_public_key=new.public_key()
        )
        assert new_ok
        assert not prior_ok

    def test_tampering_shared_payload_breaks_both(self, tmp_path: Path) -> None:
        old = signing.generate_and_write_key(tmp_path / "old.pem")
        new = signing.generate_and_write_key(tmp_path / "new.pem")
        line = signing.append_key_rotate_line(
            tmp_path,
            old_provider=old,
            new_provider=new,
            harness="claude-code",
            harness_version="1",
            hook_version="promptwitness 0.1.0",
        )
        d = line.to_dict()
        d["prior_key"] = {**d["prior_key"], "strength": "hardware-presence"}
        tampered = ledger.WitnessLine.from_dict(d)
        new_ok, prior_ok = signing.verify_key_rotate_signatures(
            tampered, old_public_key=old.public_key(), new_public_key=new.public_key()
        )
        assert not new_ok
        assert not prior_ok

    def test_extra_signer_failure_does_not_block_the_append(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A hanging/unavailable OLD key during rotation must not block the
        append -- the 'hook never blocks' rule applies to every signer, not
        just the primary one."""
        new = signing.generate_and_write_key(tmp_path / "new.pem")

        class _BrokenOldProvider:
            def key_id(self) -> str:
                return "ed25519:deadbeefdeadbeef"

            def algorithm(self) -> str:
                return "ed25519"

            def strength(self) -> str:
                return "software-file"

            def presence_required(self) -> bool:
                return False

            def sign(self, payload: bytes) -> bytes:
                raise ledger.KeyUnavailable("old key is gone")

            def public_key(self) -> bytes:
                return b"\x00" * 32

        line = signing.append_key_rotate_line(
            tmp_path,
            old_provider=_BrokenOldProvider(),
            new_provider=new,
            harness="claude-code",
            harness_version="1",
            hook_version="promptwitness 0.1.0",
        )
        assert line.kind == "key-rotate"
        assert line.sig is not None  # the new key still signed fine
        d = line.to_dict()
        assert "prior_sig" not in d
        assert d.get("prior_sig_error") == "KeyUnavailable"


class TestVerifyChainOnUntrustedInput:
    """`verify_chain` reads a file that corruption and tampering both reach.

    It must report what it finds, never raise -- a verifier that crashes on a
    one-byte edit tells an attacker's edit apart from a disk error by
    crashing on both.
    """

    def _append_raw(self, ledger_dir: Path, obj: dict[str, object]) -> None:
        partitions = ledger.list_partitions(ledger_dir)
        assert partitions
        with (ledger_dir / partitions[-1]).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj) + "\n")

    def test_a_signed_line_whose_key_is_not_an_object_does_not_raise(
        self, tmp_path: Path
    ) -> None:
        provider = signing.generate_and_write_key(tmp_path / "k.pem")
        ledger.append_line(
            tmp_path,
            kind="gap",
            harness="claude-code",
            harness_version="1",
            hook_version="promptwitness 0.1.0",
            reason="seed",
            key_provider=provider,
        )
        # `raw.get("key") or {}` returns the STRING here, and `str.get` does
        # not exist -- an AttributeError straight out of the verifier.
        self._append_raw(tmp_path, {"v": 1, "seq": 2, "sig": "AAAA", "key": "not-an-object"})

        v = ledger.verify_chain(tmp_path, public_keys={provider.key_id(): provider.public_key()})

        assert v.lines_checked == 2
        assert v.signed_lines == 2
        assert v.unverified_signed_lines == 1
        assert not v.ok  # the junk line still breaks the hash chain


class TestKeygenNeverDestroysAnExistingKey:
    """Losing a signing key is unrecoverable in a way most data loss is not.

    Every line it ever signed becomes permanently unverifiable, and there is
    no second copy anywhere to restore from -- so `--force` must never reach
    a state where the old key is gone and the new one is not yet written.
    """

    def test_force_replaces_the_key_atomically(self, tmp_path: Path) -> None:
        key_path = tmp_path / "k.pem"
        # Both providers are bound to the same PATH and read it lazily, so
        # compare the ids as captured at the time, not the live calls.
        first_id = signing.generate_and_write_key(key_path).key_id()
        first_bytes = key_path.read_bytes()

        second_id = signing.generate_and_write_key(key_path, replace=True).key_id()

        assert second_id != first_id
        assert key_path.read_bytes() != first_bytes
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
        assert [q.name for q in tmp_path.iterdir()] == ["k.pem"]

    def test_a_failure_mid_generation_leaves_the_existing_key_intact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        key_path = tmp_path / "k.pem"
        original = signing.generate_and_write_key(key_path)
        before = key_path.read_bytes()

        real_replace = os.replace

        def _boom(src: object, dst: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", _boom)
        # Matched on the INJECTED message on purpose: a bare `OSError` would
        # also be satisfied by the `FileExistsError` an unlink-free
        # `O_EXCL` open raises, so the test would pass against the very code
        # it is meant to rule out.
        with pytest.raises(OSError, match="disk full"):
            signing.generate_and_write_key(key_path, replace=True)
        monkeypatch.setattr(os, "replace", real_replace)

        assert key_path.read_bytes() == before
        assert signing.SoftwareFileKeyProvider(key_path).key_id() == original.key_id()
        # And no temp file is left lying around with key material in it.
        assert [p.name for p in tmp_path.iterdir()] == ["k.pem"]

    def test_without_force_an_existing_key_is_still_refused(self, tmp_path: Path) -> None:
        key_path = tmp_path / "k.pem"
        signing.generate_and_write_key(key_path)
        with pytest.raises(FileExistsError):
            signing.generate_and_write_key(key_path)
