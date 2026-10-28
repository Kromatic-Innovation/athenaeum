# SPDX-License-Identifier: Apache-2.0
"""Tests for :mod:`promptwitness.anchor_rfc3161` (athenaeum#2157).

Mirrors ``tests/test_promptwitness_anchor.py``'s conventions: the
``openssl`` subprocess and the TSA HTTP POST are both seams
(``run``/``http_post``) that tests inject directly, so no test here ever
shells out to a real ``openssl`` binary or touches the network -- except
``TestRealOpensslBinaryMissing.test_bogus_binary_name_degrades_to_failed``,
which deliberately exercises the REAL ``subprocess.run`` path with a bogus
binary name, the same way ``tests/test_promptwitness_anchor.py``'s
``TestMissingDependency`` exercises a real "not installed" condition
rather than simulating it.

The three acceptance criteria (issue athenaeum#2157):

1. A stamp and verify succeed against a recorded TSA response fixture --
   ``TestAnchorAndVerifyWithRecordedFixture``.
2. A missing ``openssl ts`` produces a ``failed`` attestation, not a crash
   -- ``TestOpensslMissingDegradesToFailed``.
3. An unconfigured URL means the adapter is not offered -- ``TestResolveRfc3161Url``
   (the resolver itself) and ``TestCliNotOfferedWithoutUrl`` /
   ``TestCliOfferedWithUrl`` (the real behavioral pin, through
   ``promptwitness.cli.main``).
"""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from promptwitness import anchor, anchor_rfc3161, cli, ledger

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "promptwitness_anchor_rfc3161"
_TSA_REPLY_FIXTURE: dict[str, Any] = json.loads(
    (FIXTURES_DIR / "tsa_reply.json").read_text(encoding="utf-8")
)
_RECORDED_REPLY_BYTES = base64.b64decode(_TSA_REPLY_FIXTURE["reply_b64"])
_TSA_HOST = _TSA_REPLY_FIXTURE["tsa_host"]
_TSA_URL = f"https://{_TSA_HOST}/tsa"


def _fake_run_ok(
    args: list[str], data: bytes | None, timeout: float
) -> subprocess.CompletedProcess[bytes]:
    """Behaves like a real ``openssl ts -query`` (writes a request to
    stdout, exits 0), ``openssl ts -reply -in <path> -text`` (prints a
    granted PKIStatus, exits 0 -- the reply-validation layer added for
    athenaeum#2174 review finding #1), and ``openssl ts -verify`` (prints
    ``Verification: OK``, exits 0) -- the shape AC1 needs."""
    del data, timeout
    if "-query" in args:
        assert "-digest" in args
        return subprocess.CompletedProcess(
            args, returncode=0, stdout=b"FAKE-TS-QUERY-BYTES", stderr=b""
        )
    if "-reply" in args:
        assert "-in" in args
        return subprocess.CompletedProcess(
            args, returncode=0, stdout=b"Status: Granted.\n", stderr=b""
        )
    if "-verify" in args:
        assert "-CAfile" in args
        return subprocess.CompletedProcess(
            args, returncode=0, stdout=b"Verification: OK\n", stderr=b""
        )
    raise AssertionError(f"unexpected openssl invocation: {args}")


def _fake_http_post_factory(
    posted: dict[str, Any], reply: bytes = _RECORDED_REPLY_BYTES
) -> Any:
    def _fake_http_post(url: str, data: bytes, timeout: float) -> bytes:
        posted["url"] = url
        posted["data"] = data
        posted["timeout"] = timeout
        return reply

    return _fake_http_post


# ---------------------------------------------------------------------------
# AC1: stamp and verify succeed against a recorded TSA response fixture
# ---------------------------------------------------------------------------


class TestAnchorAndVerifyWithRecordedFixture:
    def test_stamp_and_verify_succeed(self, tmp_path: Path) -> None:
        posted: dict[str, Any] = {}
        ca_bundle = tmp_path / "ca-bundle.pem"
        ca_bundle.write_text("-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")

        rfc = anchor_rfc3161.Rfc3161Anchorer(
            tmp_path,
            tsa_url=_TSA_URL,
            ca_bundle=str(ca_bundle),
            run=_fake_run_ok,
            http_post=_fake_http_post_factory(posted),
        )
        digest = anchor.digest_bytes("sha256:" + "ab" * 32)

        att = rfc.anchor(digest, "witness:pw-x:1")

        assert att.status == "complete"
        assert att.source == f"rfc3161:{_TSA_HOST}"
        assert rfc.source == f"rfc3161:{_TSA_HOST}"
        assert posted["url"] == _TSA_URL
        assert posted["data"] == b"FAKE-TS-QUERY-BYTES"

        proof_path = tmp_path / "anchors" / "witness_pw-x_1.tsr"
        assert att.proof_path == "anchors/witness_pw-x_1.tsr"
        assert proof_path.is_file()
        assert proof_path.read_bytes() == _RECORDED_REPLY_BYTES

        result = rfc.verify(att, digest)
        assert result.status == "complete"
        assert result.verify_source == f"rfc3161-ca:{ca_bundle.name}"


# ---------------------------------------------------------------------------
# AC2: a missing `openssl ts` produces a `failed` attestation, not a crash
# ---------------------------------------------------------------------------


class TestOpensslMissingDegradesToFailed:
    def test_run_seam_raising_file_not_found(self, tmp_path: Path) -> None:
        def _raising_run(
            args: list[str], data: bytes | None, timeout: float
        ) -> subprocess.CompletedProcess[bytes]:
            raise FileNotFoundError("[Errno 2] No such file or directory: 'openssl'")

        rfc = anchor_rfc3161.Rfc3161Anchorer(tmp_path, tsa_url=_TSA_URL, run=_raising_run)
        att = rfc.anchor(b"\x00" * 32, "witness:pw-x:1")

        assert att.status == "failed"
        assert att.detail["reason"] == "openssl-missing"
        assert att.proof_path is None

    def test_real_subprocess_with_a_bogus_binary_name(self, tmp_path: Path) -> None:
        """No injected ``run`` -- this is the REAL ``subprocess.run`` path,
        with a binary name chosen to not exist on any machine, proving the
        degrade against a genuine ``FileNotFoundError`` rather than a
        simulated one."""
        rfc = anchor_rfc3161.Rfc3161Anchorer(
            tmp_path, tsa_url=_TSA_URL, openssl="definitely-not-a-real-binary-athenaeum-2157"
        )
        att = rfc.anchor(b"\x00" * 32, "witness:pw-x:1")

        assert att.status == "failed"
        assert att.detail["reason"] == "openssl-missing"
        assert att.proof_path is None

    def test_verify_with_missing_openssl_also_degrades(self, tmp_path: Path) -> None:
        ca_bundle = tmp_path / "ca.pem"
        ca_bundle.write_text("fake\n")

        def _raising_run(
            args: list[str], data: bytes | None, timeout: float
        ) -> subprocess.CompletedProcess[bytes]:
            raise FileNotFoundError("no openssl")

        rfc = anchor_rfc3161.Rfc3161Anchorer(
            tmp_path, tsa_url=_TSA_URL, ca_bundle=str(ca_bundle), run=_raising_run
        )
        att = anchor.Attestation(
            source=rfc.source, status="complete", claimed_time="t", proof_path="anchors/x.tsr"
        )
        result = rfc.verify(att, b"\x00" * 32)

        assert result.status == "failed"
        assert result.detail["reason"] == "openssl-missing"


class TestQueryAndPostAndWriteFailuresDegrade:
    def test_nonzero_query_exit_is_query_failed(self, tmp_path: Path) -> None:
        def _fake_run(
            args: list[str], data: bytes | None, timeout: float
        ) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(args, returncode=1, stdout=b"", stderr=b"bad digest")

        rfc = anchor_rfc3161.Rfc3161Anchorer(tmp_path, tsa_url=_TSA_URL, run=_fake_run)
        att = rfc.anchor(b"\x00" * 32, "witness:pw-x:1")

        assert att.status == "failed"
        assert att.detail["reason"] == "query-failed"

    def test_http_post_failure_is_post_failed_never_raises(self, tmp_path: Path) -> None:
        def _raising_http_post(url: str, data: bytes, timeout: float) -> bytes:
            raise OSError("network unreachable (simulated)")

        rfc = anchor_rfc3161.Rfc3161Anchorer(
            tmp_path, tsa_url=_TSA_URL, run=_fake_run_ok, http_post=_raising_http_post
        )
        att = rfc.anchor(b"\x00" * 32, "witness:pw-x:1")

        assert att.status == "failed"
        assert att.detail["reason"] == "post-failed"
        assert att.proof_path is None

    def test_verify_with_a_failed_openssl_exit_is_verify_failed(self, tmp_path: Path) -> None:
        ca_bundle = tmp_path / "ca.pem"
        ca_bundle.write_text("fake\n")

        def _fake_run(
            args: list[str], data: bytes | None, timeout: float
        ) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(
                args, returncode=1, stdout=b"", stderr=b"Verification: FAILED\n"
            )

        rfc = anchor_rfc3161.Rfc3161Anchorer(
            tmp_path, tsa_url=_TSA_URL, ca_bundle=str(ca_bundle), run=_fake_run
        )
        att = anchor.Attestation(
            source=rfc.source, status="complete", claimed_time="t", proof_path="anchors/x.tsr"
        )
        result = rfc.verify(att, b"\x00" * 32)

        assert result.status == "failed"
        assert result.detail["reason"] == "verify-failed"


# ---------------------------------------------------------------------------
# Reply validation (athenaeum#2174 review finding #1): a reply that isn't a
# granted RFC 3161 TimeStampResp must never become a "complete" attestation,
# and a bad reply must never clobber a GOOD proof already on disk.
# ---------------------------------------------------------------------------


class TestReplyValidationDegradesToFailed:
    def test_empty_reply_is_empty_reply_and_leaves_no_tsr(self, tmp_path: Path) -> None:
        def _empty_http_post(url: str, data: bytes, timeout: float) -> bytes:
            return b""

        rfc = anchor_rfc3161.Rfc3161Anchorer(
            tmp_path, tsa_url=_TSA_URL, run=_fake_run_ok, http_post=_empty_http_post
        )
        att = rfc.anchor(b"\x00" * 32, "witness:pw-x:1")

        assert att.status == "failed"
        assert att.detail["reason"] == "empty-reply"
        assert att.proof_path is None
        proof_path = tmp_path / "anchors" / "witness_pw-x_1.tsr"
        assert not proof_path.exists()
        assert not proof_path.with_name(proof_path.name + ".tmp").exists()

    def test_non_der_reply_is_reply_not_der_and_leaves_no_tsr(self, tmp_path: Path) -> None:
        def _html_http_post(url: str, data: bytes, timeout: float) -> bytes:
            return b"<html>503 Service Unavailable</html>"

        rfc = anchor_rfc3161.Rfc3161Anchorer(
            tmp_path, tsa_url=_TSA_URL, run=_fake_run_ok, http_post=_html_http_post
        )
        att = rfc.anchor(b"\x00" * 32, "witness:pw-x:1")

        assert att.status == "failed"
        assert att.detail["reason"] == "reply-not-der"
        assert att.proof_path is None
        proof_path = tmp_path / "anchors" / "witness_pw-x_1.tsr"
        assert not proof_path.exists()
        assert not proof_path.with_name(proof_path.name + ".tmp").exists()

    def test_rejected_pkistatus_is_reply_not_granted_and_does_not_clobber_good_proof(
        self, tmp_path: Path
    ) -> None:
        proof_path = tmp_path / "anchors" / "witness_pw-x_1.tsr"
        proof_path.parent.mkdir(parents=True)
        proof_path.write_bytes(b"GOOD-EXISTING-PROOF-BYTES")

        def _fake_run_rejected(
            args: list[str], data: bytes | None, timeout: float
        ) -> subprocess.CompletedProcess[bytes]:
            if "-query" in args:
                return subprocess.CompletedProcess(
                    args, returncode=0, stdout=b"FAKE-TS-QUERY-BYTES", stderr=b""
                )
            if "-reply" in args:
                return subprocess.CompletedProcess(
                    args, returncode=1, stdout=b"", stderr=b"Status: Rejection.\n"
                )
            raise AssertionError(f"unexpected openssl invocation: {args}")

        def _bad_but_der_shaped_http_post(url: str, data: bytes, timeout: float) -> bytes:
            return b"\x30BAD-BUT-DER-SHAPED-REPLY"

        rfc = anchor_rfc3161.Rfc3161Anchorer(
            tmp_path,
            tsa_url=_TSA_URL,
            run=_fake_run_rejected,
            http_post=_bad_but_der_shaped_http_post,
        )
        att = rfc.anchor(b"\x00" * 32, "witness:pw-x:1")

        assert att.status == "failed"
        assert att.detail["reason"] == "reply-not-granted"
        assert att.proof_path is None
        # The clobber pin: a bad reply must never overwrite a GOOD proof
        # already on disk at the same path.
        assert proof_path.read_bytes() == b"GOOD-EXISTING-PROOF-BYTES"
        assert not proof_path.with_name(proof_path.name + ".tmp").exists()

    def test_openssl_missing_at_reply_stage_is_openssl_missing(self, tmp_path: Path) -> None:
        def _fake_run_missing_at_reply_stage(
            args: list[str], data: bytes | None, timeout: float
        ) -> subprocess.CompletedProcess[bytes]:
            if "-query" in args:
                return subprocess.CompletedProcess(
                    args, returncode=0, stdout=b"FAKE-TS-QUERY-BYTES", stderr=b""
                )
            if "-reply" in args:
                raise FileNotFoundError("[Errno 2] No such file or directory: 'openssl'")
            raise AssertionError(f"unexpected openssl invocation: {args}")

        def _der_shaped_http_post(url: str, data: bytes, timeout: float) -> bytes:
            return b"\x30GOOD-DER-SHAPED-REPLY"

        rfc = anchor_rfc3161.Rfc3161Anchorer(
            tmp_path,
            tsa_url=_TSA_URL,
            run=_fake_run_missing_at_reply_stage,
            http_post=_der_shaped_http_post,
        )
        att = rfc.anchor(b"\x00" * 32, "witness:pw-x:1")

        assert att.status == "failed"
        assert att.detail["reason"] == "openssl-missing"
        assert att.proof_path is None
        proof_path = tmp_path / "anchors" / "witness_pw-x_1.tsr"
        assert not proof_path.exists()
        assert not proof_path.with_name(proof_path.name + ".tmp").exists()


# ---------------------------------------------------------------------------
# Verify with no CA bundle configured -- always `failed`, never an exception
# ---------------------------------------------------------------------------


class TestVerifyWithoutCaBundle:
    def test_unconfigured_bundle_fails_cleanly(self, tmp_path: Path) -> None:
        rfc = anchor_rfc3161.Rfc3161Anchorer(tmp_path, tsa_url=_TSA_URL)
        att = anchor.Attestation(
            source=rfc.source, status="complete", claimed_time="t", proof_path="anchors/x.tsr"
        )
        result = rfc.verify(att, b"\x00" * 32)

        assert result.status == "failed"
        assert result.verify_source == "rfc3161-ca:unconfigured"
        assert result.detail["reason"] == "no-ca-bundle"

    def test_no_proof_still_fails_cleanly_when_bundle_is_configured(self, tmp_path: Path) -> None:
        ca_bundle = tmp_path / "ca.pem"
        ca_bundle.write_text("fake\n")
        rfc = anchor_rfc3161.Rfc3161Anchorer(tmp_path, tsa_url=_TSA_URL, ca_bundle=str(ca_bundle))
        att = anchor.Attestation(source=rfc.source, status="failed", claimed_time="t")
        result = rfc.verify(att, b"\x00" * 32)

        assert result.status == "failed"
        assert result.verify_source == f"rfc3161-ca:{ca_bundle.name}"
        assert result.detail["reason"] == "no-proof"


class TestUpgradeIsANoOp:
    def test_upgrade_returns_the_same_attestation_unchanged(self, tmp_path: Path) -> None:
        rfc = anchor_rfc3161.Rfc3161Anchorer(tmp_path, tsa_url=_TSA_URL)
        att = anchor.Attestation(source=rfc.source, status="complete", claimed_time="t")
        assert rfc.upgrade(att) == att


# ---------------------------------------------------------------------------
# AC3: an unconfigured URL means the adapter is not offered
# ---------------------------------------------------------------------------


class TestResolveRfc3161Url:
    def test_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PROMPTWITNESS_RFC3161_URL", "https://env.example/tsa")
        assert anchor_rfc3161.resolve_rfc3161_url("https://override.example/tsa") == (
            "https://override.example/tsa"
        )

    def test_env_wins_over_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PROMPTWITNESS_RFC3161_URL", "https://env.example/tsa")
        assert anchor_rfc3161.resolve_rfc3161_url(None) == "https://env.example/tsa"

    def test_none_when_unconfigured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PROMPTWITNESS_RFC3161_URL", raising=False)
        assert anchor_rfc3161.resolve_rfc3161_url(None) is None


class TestResolveRfc3161CaBundle:
    def test_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PROMPTWITNESS_RFC3161_CA_BUNDLE", "/env/ca.pem")
        assert anchor_rfc3161.resolve_rfc3161_ca_bundle("/override/ca.pem") == "/override/ca.pem"

    def test_env_wins_over_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PROMPTWITNESS_RFC3161_CA_BUNDLE", "/env/ca.pem")
        assert anchor_rfc3161.resolve_rfc3161_ca_bundle(None) == "/env/ca.pem"

    def test_none_when_unconfigured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PROMPTWITNESS_RFC3161_CA_BUNDLE", raising=False)
        assert anchor_rfc3161.resolve_rfc3161_ca_bundle(None) is None


class TestCliNotOfferedWithoutUrl:
    """The real behavioral pin for AC3: with no URL configured, the CLI's
    ``results`` carry no ``rfc3161:*`` attestation at all -- not a
    ``failed`` one -- and the ledger gains no extra ``anchor`` line for it.
    """

    def test_anchor_without_url_produces_no_rfc3161_attestation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("PROMPTWITNESS_RFC3161_URL", raising=False)

        rc = cli.main(["anchor", "--dir", str(tmp_path), "--subject", "x"])

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        rfc_results = [
            r
            for r in payload["results"]
            if r.get("attestation", {}).get("source", "").startswith("rfc3161:")
        ]
        assert rfc_results == []

        # Exactly local-clock + opentimestamps -- no third line for rfc3161.
        lines = list(ledger.iter_lines(tmp_path))
        assert [line.kind for line in lines] == ["anchor", "anchor"]


class TestCliOfferedWithUrl:
    """The positive counterpart: with the env var set (and the adapter's
    own subprocess/HTTP seams stubbed at the module level, since the CLI
    constructs ``Rfc3161Anchorer`` with no seam override of its own), the
    same call DOES produce exactly one ``rfc3161:*`` attestation."""

    def test_anchor_with_url_produces_exactly_one_rfc3161_attestation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("PROMPTWITNESS_RFC3161_URL", _TSA_URL)
        monkeypatch.setattr(anchor_rfc3161, "_default_run", _fake_run_ok)
        monkeypatch.setattr(
            anchor_rfc3161, "_default_http_post", _fake_http_post_factory({})
        )

        rc = cli.main(["anchor", "--dir", str(tmp_path), "--subject", "x"])

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        rfc_results = [
            r
            for r in payload["results"]
            if r.get("attestation", {}).get("source", "").startswith("rfc3161:")
        ]
        assert len(rfc_results) == 1
        assert rfc_results[0]["attestation"]["status"] == "complete"
        assert rfc_results[0]["attestation"]["source"] == f"rfc3161:{_TSA_HOST}"

        lines = list(ledger.iter_lines(tmp_path))
        assert [line.kind for line in lines] == ["anchor", "anchor", "anchor"]

    def test_verify_flag_adds_an_rfc3161_ca_verify_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ca_bundle = tmp_path / "ca.pem"
        ca_bundle.write_text("fake\n")
        monkeypatch.setenv("PROMPTWITNESS_RFC3161_URL", _TSA_URL)
        monkeypatch.setenv("PROMPTWITNESS_RFC3161_CA_BUNDLE", str(ca_bundle))
        monkeypatch.setattr(anchor_rfc3161, "_default_run", _fake_run_ok)
        monkeypatch.setattr(
            anchor_rfc3161, "_default_http_post", _fake_http_post_factory({})
        )

        rc = cli.main(
            [
                "anchor",
                "--dir",
                str(tmp_path),
                "--subject",
                "x",
                "--verify",
                "--block-source",
                "offline",
            ]
        )

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        rfc_verify_results = [
            r
            for r in payload["results"]
            if r.get("op") == "verify"
            and r.get("verify_result", {}).get("verify_source", "").startswith("rfc3161-ca:")
        ]
        assert len(rfc_verify_results) == 1
        assert rfc_verify_results[0]["verify_result"]["status"] == "complete"
        assert rfc_verify_results[0]["verify_result"]["verify_source"] == (
            f"rfc3161-ca:{ca_bundle.name}"
        )

    def test_upgrade_does_not_reanchor_or_overwrite_the_tsr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Finding #2 on PR athenaeum#2174: ``anchor --upgrade`` must find the
        prior RFC 3161 attestation (matched by the ``rfc3161:`` source
        PREFIX, since the source is per-TSA-host) and call ``upgrade()``
        instead of submitting a fresh TSA request -- re-anchoring would
        overwrite the ``.tsr`` file a prior ledger line's ``proof_path``
        still names."""
        monkeypatch.setenv("PROMPTWITNESS_RFC3161_URL", _TSA_URL)
        monkeypatch.setattr(anchor_rfc3161, "_default_run", _fake_run_ok)
        posted_calls: list[bytes] = []

        def _spy_http_post(url: str, data: bytes, timeout: float) -> bytes:
            posted_calls.append(data)
            return _RECORDED_REPLY_BYTES

        monkeypatch.setattr(anchor_rfc3161, "_default_http_post", _spy_http_post)

        rc = cli.main(["anchor", "--dir", str(tmp_path), "--subject", "x"])
        assert rc == 0
        capsys.readouterr()  # discard the first pass's stdout
        assert len(posted_calls) == 1

        proof_path = tmp_path / "anchors" / "x.tsr"
        original_bytes = proof_path.read_bytes()
        assert original_bytes == _RECORDED_REPLY_BYTES

        rc = cli.main(["anchor", "--dir", str(tmp_path), "--subject", "x", "--upgrade"])
        assert rc == 0

        # No second TSA POST -- upgrade() is a documented no-op.
        assert len(posted_calls) == 1

        payload = json.loads(capsys.readouterr().out)
        rfc_results = [
            r
            for r in payload["results"]
            if r.get("attestation", {}).get("source", "").startswith("rfc3161:")
        ]
        assert len(rfc_results) == 1
        assert rfc_results[0]["op"] == "upgrade"
        assert rfc_results[0]["attestation"]["status"] == "complete"
        assert rfc_results[0]["attestation"]["proof_path"] == "anchors/x.tsr"

        # The proof file on disk is byte-identical to the first anchor pass.
        assert proof_path.read_bytes() == original_bytes
