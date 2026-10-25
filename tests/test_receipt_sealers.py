# SPDX-License-Identifier: Apache-2.0
"""Tests for the transcript-receipt signing sealer (issue athenaeum#2039).

Covers, one class per acceptance criterion:

* :class:`TestOnePasswordProvider` — the key-provider adapter seam: the key is
  read through a fake ``op`` on ``PATH``, appears ONLY in memory (the
  counter-example that must fail is a provider caching it under a home-dir
  cache), and a missing service-account token raises the named
  :class:`athenaeum.receipt_sealers.KeyUnavailable`. One test per accepted key
  format (OpenSSH, the shape a 1Password **SSH Key** item hands back, and
  PKCS8 PEM).
* :class:`TestSigningSealerRoundTrip` — ``seal`` then ``verify`` accepts; one
  mutation test per field of the signed payload (line content, ``start_line``,
  ``end_line``).
* :class:`TestSelectSealer` — the config seam: default ``NoopSealer``, the
  configured signing sealer, and a RAISE on an unknown sealer or provider.
* :class:`TestReceiptsVerifyCli` — ``athenaeum receipts verify`` over a ledger
  with one sealed-good, one sealed-tampered and one unsealed range, asserting
  partitions and line ranges only and no receipt plaintext.
* :class:`TestNightlyHasNoKey` — the default nightly path: with
  ``receipt_sealer: signing`` configured and NO token in the environment,
  ``write_receipts_for_files`` still appends, warns exactly ONCE for the whole
  run (not once per receipt), and produces no seal.
* :class:`TestLibrarianThreadsTheSelector` — the call-site wiring, so the
  selector is not merely present but actually reached by the nightly.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from athenaeum.config import ReceiptSealerConfigError
from athenaeum.models import AutoMemoryFile
from athenaeum.receipt_sealers import (
    OP_SERVICE_ACCOUNT_TOKEN_ENV,
    KeyUnavailable,
    OnePasswordServiceAccountKeyProvider,
    SigningSealer,
    derive_key_id,
    iter_stored_seals,
    seal_all_unsealed,
    seals_dir,
    select_sealer,
    unsealed_ranges,
    verify_all,
)
from athenaeum.runlock import RunLock
from athenaeum.transcript_receipts import (
    NoopSealer,
    ReceiptEntry,
    append_receipt,
    partition_path,
)

MONTH = "2026-10"
PRIVATE_REF = "op://Agent Tools/Athenaeum Receipt Sealing Key/private key"
PUBLIC_REF = "op://Agent Tools/Athenaeum Receipt Sealing Key/public key"


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _openssh_private(key: Ed25519PrivateKey) -> bytes:
    return key.private_bytes(
        Encoding.PEM, PrivateFormat.OpenSSH, NoEncryption()
    )


def _pkcs8_private(key: Ed25519PrivateKey) -> bytes:
    return key.private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
    )


def _openssh_public(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH)


def _raw_public(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def _fake_op(bin_dir: Path, *, private: bytes, public: bytes) -> Path:
    """A fake ``op`` that serves the two field references and nothing else.

    Kept OUTSIDE the directory the no-disk-cache assertions scan: the fixture
    is allowed to hold the key, the provider is not.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "private.key").write_bytes(private)
    (bin_dir / "public.key").write_bytes(public)
    script = bin_dir / "op"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" != "read" ]; then echo "unexpected verb: $1" >&2; exit 2; fi\n'
        'case "$2" in\n'
        "  *Athenaeum?Receipt?Sealing?Key/private?key)"
        " cat \"$(dirname \"$0\")/private.key\" ;;\n"
        "  *Athenaeum?Receipt?Sealing?Key/public?key)"
        "  cat \"$(dirname \"$0\")/public.key\" ;;\n"
        '  *) echo "no such item or field: $2" >&2; exit 1 ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _provider_env(bin_dir: Path, home: Path, *, token: str | None = "svc-token") -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(home),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "TMPDIR": str(home / "tmp"),
    }
    if token is not None:
        env[OP_SERVICE_ACCOUNT_TOKEN_ENV] = token
    return env


def _files_containing(root: Path, needle: bytes) -> list[Path]:
    """Every regular file under *root* whose bytes contain *needle*."""
    hits: list[Path] = []
    if not root.exists():
        return hits
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            if needle in path.read_bytes():
                hits.append(path)
        except OSError:  # pragma: no cover - unreadable fixture artefact
            continue
    return hits


def _ledger_line(wiki: Path, knowledge_root: Path, digest: str) -> None:
    lock = RunLock(knowledge_root)
    with lock:
        append_receipt(
            wiki,
            ReceiptEntry(
                origin_scope="scopeA",
                memory_digest=digest,
                channel="user-stated",
                recorded_at=f"{MONTH}-09T00:00:00Z",
            ),
            lock=lock,
        )


# ---------------------------------------------------------------------------
# AC1 — the key provider
# ---------------------------------------------------------------------------


class TestOnePasswordProvider:
    @pytest.mark.parametrize("fmt", ["openssh", "pkcs8"])
    def test_signs_through_fake_op_and_never_writes_the_key_to_disk(
        self, tmp_path: Path, fmt: str
    ) -> None:
        """The key reaches memory and nothing else.

        The counter-example the acceptance criterion names — a provider
        caching the key under a home-dir cache — fails here: the scan covers
        the whole fake ``HOME`` (including ``.cache`` and ``TMPDIR``), so any
        write of the key material at all is a hit.
        """
        key = Ed25519PrivateKey.generate()
        private = _openssh_private(key) if fmt == "openssh" else _pkcs8_private(key)
        bin_dir = tmp_path / "fixture-bin"
        home = tmp_path / "home"
        _fake_op(bin_dir, private=private, public=_openssh_public(key))
        provider = OnePasswordServiceAccountKeyProvider(
            PRIVATE_REF,
            public_key_reference=PUBLIC_REF,
            env=_provider_env(bin_dir, home),
        )

        signature = provider.sign(b"payload-bytes")

        assert provider.public_key() == _raw_public(key)
        assert provider.key_id() == derive_key_id(_raw_public(key))
        from athenaeum.receipt_sealers import verify_signature

        assert verify_signature(_raw_public(key), signature, b"payload-bytes")
        # The private material is in the fixture's own file (allowed) and
        # nowhere the provider could have put it.
        assert _files_containing(home, private.strip()) == []
        assert _files_containing(home, bytes(key.private_bytes_raw())) == []

    def test_inline_public_key_needs_no_op_at_all(self, tmp_path: Path) -> None:
        """Verification runs with no credential and no ``op`` binary."""
        key = Ed25519PrivateKey.generate()
        provider = OnePasswordServiceAccountKeyProvider(
            PRIVATE_REF,
            public_key_openssh=_openssh_public(key).decode("ascii"),
            env={"PATH": "/nonexistent"},
        )
        assert provider.public_key() == _raw_public(key)
        assert provider.key_id() == derive_key_id(_raw_public(key))

    def test_missing_service_account_token_raises_key_unavailable(
        self, tmp_path: Path
    ) -> None:
        key = Ed25519PrivateKey.generate()
        bin_dir = tmp_path / "fixture-bin"
        _fake_op(bin_dir, private=_openssh_private(key), public=_openssh_public(key))
        provider = OnePasswordServiceAccountKeyProvider(
            PRIVATE_REF,
            env=_provider_env(bin_dir, tmp_path / "home", token=None),
        )
        with pytest.raises(KeyUnavailable) as excinfo:
            provider.sign(b"payload")
        assert OP_SERVICE_ACCOUNT_TOKEN_ENV in str(excinfo.value)

    def test_missing_op_binary_raises_key_unavailable(self, tmp_path: Path) -> None:
        provider = OnePasswordServiceAccountKeyProvider(
            PRIVATE_REF,
            env={"PATH": str(tmp_path / "empty"), OP_SERVICE_ACCOUNT_TOKEN_ENV: "t"},
        )
        with pytest.raises(KeyUnavailable):
            provider.sign(b"payload")

    def test_unreadable_item_raises_key_unavailable_without_leaking_the_value(
        self, tmp_path: Path
    ) -> None:
        key = Ed25519PrivateKey.generate()
        bin_dir = tmp_path / "fixture-bin"
        _fake_op(bin_dir, private=_openssh_private(key), public=_openssh_public(key))
        provider = OnePasswordServiceAccountKeyProvider(
            "op://Agent Tools/No Such Item/private key",
            env=_provider_env(bin_dir, tmp_path / "home"),
        )
        with pytest.raises(KeyUnavailable) as excinfo:
            provider.sign(b"payload")
        assert "No Such Item" in str(excinfo.value)


# ---------------------------------------------------------------------------
# AC2 — seal / verify round trip, and one mutation per signed field
# ---------------------------------------------------------------------------


class _LocalKeyProvider:
    """A :class:`athenaeum.receipt_sealers.SigningKeyProvider` over an in-test key."""

    def __init__(self, key: Ed25519PrivateKey) -> None:
        self._key = key

    def key_id(self) -> str:
        return derive_key_id(self.public_key())

    def sign(self, payload: bytes) -> bytes:
        return self._key.sign(payload)

    def public_key(self) -> bytes:
        return _raw_public(self._key)


def _sealed_fixture(tmp_path: Path, *, lines: int = 3) -> tuple[Path, SigningSealer, Path]:
    """A ledger with *lines* receipts, all sealed as one range."""
    knowledge_root = tmp_path / "knowledge"
    wiki = knowledge_root / "wiki"
    for n in range(lines):
        _ledger_line(wiki, knowledge_root, f"digest{n}")
    sealer = SigningSealer(_LocalKeyProvider(Ed25519PrivateKey.generate()))
    partition = partition_path(wiki, MONTH)
    content = partition.read_text(encoding="utf-8").splitlines()
    record = sealer.seal(partition, 1, lines, content)
    assert record is not None
    return wiki, sealer, partition


class TestSigningSealerRoundTrip:
    def test_seal_then_verify_accepts(self, tmp_path: Path) -> None:
        wiki, sealer, partition = _sealed_fixture(tmp_path)
        stored = iter_stored_seals(wiki, MONTH)
        assert len(stored) == 1
        assert stored[0].algorithm == "ed25519"
        assert stored[0].key_id == sealer.provider.key_id()
        assert sealer.verify(partition, 1, 3, stored[0].as_seal_record()) is True

    def test_verify_rejects_after_one_line_is_altered(self, tmp_path: Path) -> None:
        wiki, sealer, partition = _sealed_fixture(tmp_path)
        stored = iter_stored_seals(wiki, MONTH)[0]
        lines = partition.read_text(encoding="utf-8").splitlines()
        tampered = json.loads(lines[1])
        tampered["memory_digest"] = "forged"
        lines[1] = json.dumps(tampered, separators=(",", ":"))
        partition.write_text("\n".join(lines) + "\n", encoding="utf-8")
        assert sealer.verify(partition, 1, 3, stored.as_seal_record()) is False

    def test_verify_rejects_a_shifted_start_line(self, tmp_path: Path) -> None:
        wiki, sealer, partition = _sealed_fixture(tmp_path)
        stored = iter_stored_seals(wiki, MONTH)[0]
        assert sealer.verify(partition, 2, 3, stored.as_seal_record()) is False

    def test_verify_rejects_a_shifted_end_line(self, tmp_path: Path) -> None:
        wiki, sealer, partition = _sealed_fixture(tmp_path)
        stored = iter_stored_seals(wiki, MONTH)[0]
        assert sealer.verify(partition, 1, 2, stored.as_seal_record()) is False

    def test_verify_rejects_an_absent_seal(self, tmp_path: Path) -> None:
        """Unsealed is NOT vacuously verified — the whole point of a sealer."""
        wiki, sealer, partition = _sealed_fixture(tmp_path)
        assert sealer.verify(partition, 1, 3, None) is False

    def test_verify_rejects_a_seal_from_a_different_key(self, tmp_path: Path) -> None:
        wiki, sealer, partition = _sealed_fixture(tmp_path)
        stored = iter_stored_seals(wiki, MONTH)[0]
        other = SigningSealer(_LocalKeyProvider(Ed25519PrivateKey.generate()))
        assert other.verify(partition, 1, 3, stored.as_seal_record()) is False

    def test_verify_rejects_a_range_past_the_end_of_the_partition(
        self, tmp_path: Path
    ) -> None:
        """A truncated ledger cannot read as verified."""
        wiki, sealer, partition = _sealed_fixture(tmp_path)
        stored = iter_stored_seals(wiki, MONTH)[0]
        lines = partition.read_text(encoding="utf-8").splitlines()[:2]
        partition.write_text("\n".join(lines) + "\n", encoding="utf-8")
        assert sealer.verify(partition, 1, 3, stored.as_seal_record()) is False

    def test_seal_all_unsealed_coalesces_and_is_idempotent(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        wiki = knowledge_root / "wiki"
        for n in range(4):
            _ledger_line(wiki, knowledge_root, f"digest{n}")
        sealer = SigningSealer(_LocalKeyProvider(Ed25519PrivateKey.generate()))

        first = seal_all_unsealed(wiki, sealer)
        assert [(r.start_line, r.end_line) for r in first.sealed] == [(1, 4)]
        assert first.unavailable is None
        second = seal_all_unsealed(wiki, sealer)
        assert second.sealed == []
        assert second.already_sealed == 1
        assert unsealed_ranges(wiki, MONTH) == []
        assert verify_all(wiki, sealer).ok is True

    def test_seal_store_is_a_subdirectory_so_the_ledger_never_parses_it(
        self, tmp_path: Path
    ) -> None:
        """``iter_receipts`` globs ``*.jsonl`` in the ledger dir itself."""
        from athenaeum.transcript_receipts import iter_receipts

        wiki, _sealer, _partition = _sealed_fixture(tmp_path)
        assert seals_dir(wiki).is_dir()
        assert seals_dir(wiki).parent == partition_path(wiki, MONTH).parent
        assert len(iter_receipts(wiki)) == 3


# ---------------------------------------------------------------------------
# AC3 — select_sealer, three config fixtures
# ---------------------------------------------------------------------------


class TestSelectSealer:
    def test_default_is_noop(self) -> None:
        assert isinstance(select_sealer(None), NoopSealer)
        assert isinstance(select_sealer({}), NoopSealer)
        assert isinstance(select_sealer({"librarian": {}}), NoopSealer)

    def test_signing_builds_the_onepassword_provider(self) -> None:
        sealer = select_sealer(
            {
                "librarian": {
                    "receipt_sealer": "signing",
                    "receipt_signing": {
                        "key_provider": "onepassword",
                        "op_reference": "op://V/I/private key",
                        "public_key_ref": "op://V/I/public key",
                    },
                }
            }
        )
        assert isinstance(sealer, SigningSealer)
        provider = sealer.provider
        assert isinstance(provider, OnePasswordServiceAccountKeyProvider)
        assert provider.op_reference == "op://V/I/private key"
        assert provider.public_key_reference == "op://V/I/public key"

    def test_unknown_sealer_raises(self) -> None:
        with pytest.raises(ReceiptSealerConfigError):
            select_sealer({"librarian": {"receipt_sealer": "signng"}})

    def test_unknown_key_provider_raises(self) -> None:
        with pytest.raises(ReceiptSealerConfigError):
            select_sealer(
                {
                    "librarian": {
                        "receipt_sealer": "signing",
                        "receipt_signing": {"key_provider": "yubikey"},
                    }
                }
            )

    def test_signing_defaults_point_at_the_operator_created_item(self) -> None:
        sealer = select_sealer({"librarian": {"receipt_sealer": "signing"}})
        assert isinstance(sealer, SigningSealer)
        provider = sealer.provider
        assert isinstance(provider, OnePasswordServiceAccountKeyProvider)
        assert provider.op_reference.endswith("/private key")
        assert provider.public_key_reference is not None
        assert provider.public_key_reference.endswith("/public key")


# ---------------------------------------------------------------------------
# AC4 — `athenaeum receipts verify` output
# ---------------------------------------------------------------------------


class TestReceiptsVerifyCli:
    def _fixture(self, tmp_path: Path, secret: str) -> tuple[Path, Ed25519PrivateKey]:
        """Ledger with line 1 sealed-good, line 2 sealed-then-tampered, line 3 unsealed."""
        knowledge_root = tmp_path / "knowledge"
        wiki = knowledge_root / "wiki"
        key = Ed25519PrivateKey.generate()
        sealer = SigningSealer(_LocalKeyProvider(key))
        partition = partition_path(wiki, MONTH)

        _ledger_line(wiki, knowledge_root, f"good-{secret}")
        lines = partition.read_text(encoding="utf-8").splitlines()
        assert sealer.seal(partition, 1, 1, lines[0:1]) is not None

        _ledger_line(wiki, knowledge_root, f"tampered-{secret}")
        lines = partition.read_text(encoding="utf-8").splitlines()
        assert sealer.seal(partition, 2, 2, lines[1:2]) is not None
        record = json.loads(lines[1])
        record["memory_digest"] = f"forged-{secret}"
        lines[1] = json.dumps(record, separators=(",", ":"))
        partition.write_text("\n".join(lines) + "\n", encoding="utf-8")

        _ledger_line(wiki, knowledge_root, f"unsealed-{secret}")

        (knowledge_root / "athenaeum.yaml").write_text(
            "librarian:\n"
            "  receipt_sealer: signing\n"
            "  receipt_signing:\n"
            "    key_provider: onepassword\n"
            f"    public_key_openssh: {_openssh_public(key).decode('ascii')}\n",
            encoding="utf-8",
        )
        return knowledge_root, key

    def test_reports_each_range_by_partition_and_line_only(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from athenaeum.cli import main

        secret = "zzsecretzz"
        knowledge_root, _key = self._fixture(tmp_path, secret)

        code = main(["receipts", "verify", "--path", str(knowledge_root)])
        out = capsys.readouterr().out

        assert code == 3  # a ledger problem, distinct from a command failure
        assert f"FAILED:   {MONTH} lines 2-2" in out
        assert f"unsealed: {MONTH} lines 3-3" in out
        assert "1 verified, 1 failed, 1 unsealed range(s)" in out
        # No receipt content of any kind crosses into the report.
        assert secret not in out
        assert "memory_digest" not in out

    def test_json_output_carries_the_same_three_buckets(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from athenaeum.cli import main

        secret = "zzsecretzz"
        knowledge_root, _key = self._fixture(tmp_path, secret)
        code = main(["receipts", "verify", "--path", str(knowledge_root), "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert code == 3
        assert payload["ok"] is False
        assert payload["verified"] == [{"month": MONTH, "start_line": 1, "end_line": 1}]
        assert payload["failed"] == [{"month": MONTH, "start_line": 2, "end_line": 2}]
        assert payload["unsealed"] == [{"month": MONTH, "start_line": 3, "end_line": 3}]
        assert secret not in json.dumps(payload)

    def test_verify_runs_with_no_credential_and_no_op_binary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The inline public key is what makes unattended verification possible."""
        from athenaeum.cli import main

        knowledge_root, _key = self._fixture(tmp_path, "zzsecretzz")
        monkeypatch.delenv(OP_SERVICE_ACCOUNT_TOKEN_ENV, raising=False)
        monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
        assert main(["receipts", "verify", "--path", str(knowledge_root)]) == 3
        assert "1 verified" in capsys.readouterr().out

    def test_noop_configuration_is_refused_rather_than_silently_clean(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from athenaeum.cli import main

        knowledge_root = tmp_path / "knowledge"
        knowledge_root.mkdir(parents=True)
        (knowledge_root / "athenaeum.yaml").write_text("librarian: {}\n", encoding="utf-8")
        assert main(["receipts", "verify", "--path", str(knowledge_root)]) == 2
        assert "not `signing`" in capsys.readouterr().err

    def test_seal_reports_an_unreachable_key_distinctly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from athenaeum.cli import main

        knowledge_root, _key = self._fixture(tmp_path, "zzsecretzz")
        monkeypatch.delenv(OP_SERVICE_ACCOUNT_TOKEN_ENV, raising=False)
        monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
        code = main(["receipts", "seal", "--path", str(knowledge_root)])
        assert code == 4
        assert "signing key unavailable" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# AC5 — the nightly path has no key
# ---------------------------------------------------------------------------


def _member(tmp_path: Path, name: str, claim: str, session_id: str) -> AutoMemoryFile:
    member = tmp_path / "mem" / f"{name}.md"
    member.parent.mkdir(parents=True, exist_ok=True)
    member.write_text(f"---\nname: {name}\n---\n{claim}\n", encoding="utf-8")
    return AutoMemoryFile(
        path=member,
        origin_scope="scopeA",
        memory_type="feedback",
        name=name,
        origin_session_id=session_id,
        origin_turn=1,
    )


def _transcript(projects_root: Path, session_id: str, text: str) -> None:
    scope = projects_root / "scopeA"
    scope.mkdir(parents=True, exist_ok=True)
    (scope / f"{session_id}.jsonl").write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n",
        encoding="utf-8",
    )


class TestNightlyHasNoKey:
    def test_signing_configured_without_a_token_appends_and_warns_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from athenaeum.transcript_receipts import iter_receipts, write_receipts_for_files

        monkeypatch.delenv(OP_SERVICE_ACCOUNT_TOKEN_ENV, raising=False)
        knowledge_root = tmp_path / "knowledge"
        wiki = knowledge_root / "wiki"
        projects_root = tmp_path / "projects"
        files = []
        for n in range(3):
            claim = f"the operator said thing {n}"
            files.append(_member(tmp_path, f"feedback_{n}", claim, f"sess{n}"))
            _transcript(projects_root, f"sess{n}", claim)

        config: dict[str, Any] = {
            "librarian": {
                "transcript_receipts_enabled": True,
                "receipt_sealer": "signing",
                "receipt_signing": {"key_provider": "onepassword"},
            }
        }
        sealer = select_sealer(config)
        assert isinstance(sealer, SigningSealer)

        lock = RunLock(knowledge_root)
        with caplog.at_level(logging.WARNING), lock:
            stats = write_receipts_for_files(
                wiki,
                files,
                config=config,
                projects_root=projects_root,
                lock=lock,
                sealer=sealer,
            )

        # Intake is never blocked: every receipt line is appended.
        assert int(stats) == 3
        assert len(iter_receipts(wiki)) == 3
        # ...unsealed: no seal record, and no seal store at all.
        assert not seals_dir(wiki).exists()
        assert iter_stored_seals(wiki) == []
        # ...and exactly ONE warning for the whole run, not one per receipt.
        warnings = [
            r for r in caplog.records if r.levelno >= logging.WARNING and "key" in r.getMessage()
        ]
        assert len(warnings) == 1, [r.getMessage() for r in warnings]
        assert "receipts seal" in warnings[0].getMessage()

    def test_sealer_failure_never_propagates_out_of_the_writer(
        self, tmp_path: Path
    ) -> None:
        """A sealer that raises something other than KeyUnavailable is still non-fatal."""
        from athenaeum.transcript_receipts import iter_receipts

        class _Exploding:
            name = "boom"

            def seal(self, *args: object, **kwargs: object) -> None:
                raise RuntimeError("provider exploded")

            def verify(self, *args: object, **kwargs: object) -> bool:
                return False

        knowledge_root = tmp_path / "knowledge"
        wiki = knowledge_root / "wiki"
        lock = RunLock(knowledge_root)
        with lock:
            append_receipt(
                wiki,
                ReceiptEntry(
                    origin_scope="scopeA",
                    memory_digest="d",
                    channel="user-stated",
                    recorded_at=f"{MONTH}-09T00:00:00Z",
                ),
                lock=lock,
                sealer=_Exploding(),  # type: ignore[arg-type]
            )
        assert len(iter_receipts(wiki)) == 1


# ---------------------------------------------------------------------------
# The call-site wiring (a selector nothing reaches is not a feature)
# ---------------------------------------------------------------------------


class TestLibrarianThreadsTheSelector:
    def test_write_transcript_receipts_passes_the_configured_sealer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum import librarian as librarian_mod
        from athenaeum import transcript_receipts as tr_mod

        seen: dict[str, Any] = {}

        def _fake_writer(*args: Any, **kwargs: Any) -> Any:
            seen["sealer"] = kwargs.get("sealer")
            return tr_mod.ReceiptsRunStats(files_seen=0)

        monkeypatch.setattr(tr_mod, "write_receipts_for_files", _fake_writer)

        class _Ctx:
            dry_run = False
            lock = object()
            wiki_root = tmp_path / "wiki"
            projects_root = tmp_path / "projects"
            config: dict[str, Any] = {
                "librarian": {
                    "transcript_receipts_enabled": True,
                    "receipt_sealer": "signing",
                    "receipt_signing": {"key_provider": "onepassword"},
                }
            }
            run_profile: list[Any] = []

        librarian_mod._write_transcript_receipts(_Ctx(), [])  # type: ignore[arg-type]

        assert isinstance(seen.get("sealer"), SigningSealer)

    def test_default_config_still_threads_the_noop_sealer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum import librarian as librarian_mod
        from athenaeum import transcript_receipts as tr_mod

        seen: dict[str, Any] = {}

        def _fake_writer(*args: Any, **kwargs: Any) -> Any:
            seen["sealer"] = kwargs.get("sealer")
            return tr_mod.ReceiptsRunStats(files_seen=0)

        monkeypatch.setattr(tr_mod, "write_receipts_for_files", _fake_writer)

        class _Ctx:
            dry_run = False
            lock = object()
            wiki_root = tmp_path / "wiki"
            projects_root = tmp_path / "projects"
            config: dict[str, Any] = {"librarian": {"transcript_receipts_enabled": True}}
            run_profile: list[Any] = []

        librarian_mod._write_transcript_receipts(_Ctx(), [])  # type: ignore[arg-type]

        assert isinstance(seen.get("sealer"), NoopSealer)


class TestReceiptsSealCliEndToEnd:
    """``athenaeum receipts seal`` with a reachable key, through the real config path.

    The one test that exercises every layer at once: yaml ->
    :func:`athenaeum.receipt_sealers.select_sealer` -> the 1Password provider
    -> ``op read`` (faked on ``PATH``) -> a signature -> a persisted seal ->
    a clean ``receipts verify``.
    """

    def test_seals_every_unsealed_range_then_verifies_clean(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from athenaeum.cli import main

        key = Ed25519PrivateKey.generate()
        bin_dir = tmp_path / "fixture-bin"
        _fake_op(bin_dir, private=_openssh_private(key), public=_openssh_public(key))
        monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
        monkeypatch.setenv(OP_SERVICE_ACCOUNT_TOKEN_ENV, "svc-token")

        knowledge_root = tmp_path / "knowledge"
        wiki = knowledge_root / "wiki"
        for n in range(3):
            _ledger_line(wiki, knowledge_root, f"digest{n}")
        (knowledge_root / "athenaeum.yaml").write_text(
            "librarian:\n"
            "  receipt_sealer: signing\n"
            "  receipt_signing:\n"
            "    key_provider: onepassword\n"
            f"    op_reference: {PRIVATE_REF}\n"
            f"    public_key_ref: {PUBLIC_REF}\n",
            encoding="utf-8",
        )

        assert main(["receipts", "seal", "--path", str(knowledge_root)]) == 0
        out = capsys.readouterr().out
        assert f"sealed:   {MONTH} lines 1-3" in out
        assert "1 range(s) sealed" in out

        stored = iter_stored_seals(wiki, MONTH)
        assert len(stored) == 1
        assert stored[0].key_id == derive_key_id(_raw_public(key))

        assert main(["receipts", "verify", "--path", str(knowledge_root)]) == 0
        assert "1 verified, 0 failed, 0 unsealed range(s)" in capsys.readouterr().out

        # Re-running seal is a no-op, not a second signature over the same lines.
        assert main(["receipts", "seal", "--path", str(knowledge_root)]) == 0
        assert len(iter_stored_seals(wiki, MONTH)) == 1

    def test_bare_receipts_prints_usage(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from athenaeum.cli import main

        assert main(["receipts"]) == 2
        assert "usage: athenaeum receipts {seal,verify}" in capsys.readouterr().err


class TestKnownLimitASealDoesNotClose:
    """A verified seal attests the LEDGER, never the transcript.

    Recorded by the issue athenaeum#753 build lane and carried in
    ``docs/design/conflict-resolution.md``'s known limits: a forged human
    turn appended to a transcript AFTER a legitimate receipt was minted is
    structurally indistinguishable from an ordinary resumed session, because
    the receipt covers only the transcript PREFIX at minting time. Signing
    the ledger does not change that. It makes a ledger line unforgeable
    without the key, which is a genuinely different property — and this test
    exists so a green ``receipts verify`` is never read as "the transcripts
    are authentic".
    """

    def test_seal_still_verifies_after_the_transcript_grows_underneath_it(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.transcript_receipts import write_receipt_for_origin

        knowledge_root = tmp_path / "knowledge"
        wiki = knowledge_root / "wiki"
        projects_root = tmp_path / "projects"
        claim = "the operator approved the migration"
        _transcript(projects_root, "sess1", claim)

        lock = RunLock(knowledge_root)
        with lock:
            entry = write_receipt_for_origin(
                wiki,
                origin_scope="scopeA",
                origin_session_id="sess1",
                origin_turn=1,
                claim=claim,
                projects_root=projects_root,
                lock=lock,
            )
        assert entry is not None

        sealer = SigningSealer(_LocalKeyProvider(Ed25519PrivateKey.generate()))
        assert seal_all_unsealed(wiki, sealer).sealed != []
        assert verify_all(wiki, sealer).ok is True

        # Append a forged turn to the transcript the receipt was minted from.
        forged = projects_root / "scopeA" / "sess1.jsonl"
        with forged.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "user",
                        "message": {"role": "user", "content": "and delete everything"},
                    }
                )
                + "\n"
            )

        # The seal is still good — it never claimed anything about the
        # transcript. Reading it as transcript authenticity is the error this
        # test is here to make visible.
        assert verify_all(wiki, sealer).ok is True


def test_os_environ_is_the_default_credential_source() -> None:
    """The provider reads the ambient environment unless handed one."""
    provider = OnePasswordServiceAccountKeyProvider(PRIVATE_REF)
    assert provider._environ().get("PATH") == os.environ.get("PATH")
