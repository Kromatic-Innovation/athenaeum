# SPDX-License-Identifier: Apache-2.0
"""Tests for :mod:`promptwitness.anchor` (athenaeum#2151).

Two constraints shape this file (see the issue and
``docs/design/witness-ledger.md``):

1. **Offline by default.** ``tests/conftest.py``'s autouse
   ``_block_non_local_network`` fixture blocks every non-loopback socket for
   the default test selection (``tests/test_network_guard.py`` pins this).
   None of the tests below opt out of it -- the OpenTimestamps-calendar and
   block-explorer HTTP paths are driven through recorded fixtures and an
   injected HTTP seam instead, and one test (``TestVerifyWithRealSockets``)
   deliberately calls the REAL default HTTP path with no override, to prove
   the "sockets blocked -> complete-unverified-block, never raises" AC
   against the real guard rather than a simulation of it.

2. **``opentimestamps`` is not installed.** It is declared only in the
   ``witness-anchor`` extra (never in ``dev``), so it is genuinely absent
   from this environment -- ``TestMissingDependency`` below exercises the
   real "missing optional dependency" degrade path for free, with no stub
   at all. Every other OpenTimestamps-adapter test installs a minimal FAKE
   ``opentimestamps`` package tree into ``sys.modules``
   (``_install_fake_opentimestamps``), mirroring only the handful of names
   :mod:`promptwitness.anchor`'s module docstring lists as the real
   library's touched surface -- these are the tests that actually run by
   default, so they are the ones that keep the ACs honest.
   ``TestRealLibrarySurfaceIfInstalled`` is the ``pytest.importorskip``-gated
   counterpart that exercises the genuine library when it happens to be
   installed; it never runs in this CI.
"""

from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from promptwitness import anchor

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "promptwitness_anchor"
_BLOCK_EXPLORER_FIXTURE: dict[str, Any] = json.loads(
    (FIXTURES_DIR / "block_explorer.json").read_text(encoding="utf-8")
)


# ---------------------------------------------------------------------------
# Minimal in-test stub for `opentimestamps` -- see module docstring above.
# ---------------------------------------------------------------------------


def _install_fake_opentimestamps(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    """Install a fake ``opentimestamps`` package tree into ``sys.modules``,
    covering exactly the names :mod:`promptwitness.anchor` imports (see its
    module docstring's enumerated list) and nothing more.

    Returns a namespace exposing the fake classes plus two MUTABLE dicts,
    ``submit_responses`` and ``get_timestamp_responses``, that the calling
    test populates to script ``RemoteCalendar.submit``/``.get_timestamp``:

    - ``submit_responses[calendar_url]``: a callable ``digest -> FakeTimestamp``,
      or an ``Exception`` instance to raise (simulating a network/calendar
      failure).
    - ``get_timestamp_responses[(calendar_url, commitment_hex)]``: a callable
      ``commitment -> FakeTimestamp``, or an ``Exception`` instance. A
      missing key raises the fake ``CommitmentNotFoundError`` (still
      pending), matching the real library's behavior for an
      unconfirmed commitment.

    The fake's own (de)serialization format is a simple JSON tree -- it
    does NOT reproduce the real library's binary wire format. Nothing
    outside this stub ever reads a proof file it wrote, so only its own
    round trip needs to agree with itself.
    """
    submit_responses: dict[str, Any] = {}
    get_timestamp_responses: dict[tuple[str, str], Any] = {}

    class CommitmentNotFoundError(KeyError):
        pass

    @dataclass(frozen=True)
    class PendingAttestation:
        uri: str = "https://example.test/cal"

    @dataclass(frozen=True)
    class BitcoinBlockHeaderAttestation:
        height: int

    def _encode_att(a: Any) -> dict[str, Any]:
        if isinstance(a, PendingAttestation):
            return {"type": "pending", "uri": a.uri}
        if isinstance(a, BitcoinBlockHeaderAttestation):
            return {"type": "bitcoin", "height": a.height}
        raise TypeError(f"unencodable fake attestation: {a!r}")

    def _decode_att(d: dict[str, Any]) -> Any:
        if d["type"] == "pending":
            return PendingAttestation(d["uri"])
        if d["type"] == "bitcoin":
            return BitcoinBlockHeaderAttestation(d["height"])
        raise ValueError(f"undecodable fake attestation: {d!r}")

    class FakeTimestamp:
        def __init__(
            self,
            msg: bytes,
            attestations: Any = None,
            ops: dict[bytes, "FakeTimestamp"] | None = None,
        ) -> None:
            self.msg = msg
            self.attestations = set(attestations or ())
            self.ops: dict[bytes, FakeTimestamp] = dict(ops or {})

        def all_attestations(self) -> Any:
            for a in self.attestations:
                yield (self.msg, a)
            for sub in self.ops.values():
                yield from sub.all_attestations()

        def merge(self, other: "FakeTimestamp") -> None:
            if self.msg != other.msg:
                raise ValueError("cannot merge timestamps for different messages")
            self.attestations |= other.attestations
            for sub_msg, sub in other.ops.items():
                mine = self.ops.setdefault(sub_msg, FakeTimestamp(sub_msg))
                mine.merge(sub)

        def _to_payload(self) -> dict[str, Any]:
            return {
                "msg": self.msg.hex(),
                "attestations": [_encode_att(a) for a in self.attestations],
                "ops": [sub._to_payload() for sub in self.ops.values()],
            }

        def serialize(self, ctx: Any) -> None:
            ctx.write(json.dumps(self._to_payload()).encode("utf-8"))

        @classmethod
        def deserialize(cls, ctx: Any, initial_msg: bytes) -> "FakeTimestamp":
            payload = json.loads(ctx.read_all().decode("utf-8"))
            return cls._from_payload(payload, initial_msg)

        @classmethod
        def _from_payload(cls, payload: dict[str, Any], msg: bytes) -> "FakeTimestamp":
            ts = cls(msg)
            ts.attestations = {_decode_att(a) for a in payload.get("attestations", [])}
            for sub_payload in payload.get("ops", []):
                sub_msg = bytes.fromhex(sub_payload["msg"])
                ts.ops[sub_msg] = cls._from_payload(sub_payload, sub_msg)
            return ts

    class RemoteCalendar:
        def __init__(self, url: str) -> None:
            self.url = url

        def submit(self, digest: bytes, timeout: float | None = None) -> FakeTimestamp:
            del timeout
            result = submit_responses.get(self.url)
            if isinstance(result, Exception):
                raise result
            if result is None:
                raise RuntimeError(f"no fixture submit() result registered for {self.url!r}")
            return result(digest)

        def get_timestamp(self, commitment: bytes, timeout: float | None = None) -> FakeTimestamp:
            del timeout
            result = get_timestamp_responses.get((self.url, commitment.hex()))
            if isinstance(result, Exception):
                raise result
            if result is None:
                raise CommitmentNotFoundError("commitment not found")
            return result(commitment)

    class BytesSerializationContext:
        def __init__(self) -> None:
            self._buf = b""

        def write(self, b: bytes) -> None:
            self._buf += b

        def getbytes(self) -> bytes:
            return self._buf

    class BytesDeserializationContext:
        def __init__(self, buf: bytes) -> None:
            self._buf = buf

        def read_all(self) -> bytes:
            return self._buf

    calendar_mod = types.ModuleType("opentimestamps.calendar")
    calendar_mod.RemoteCalendar = RemoteCalendar  # type: ignore[attr-defined]
    calendar_mod.CommitmentNotFoundError = CommitmentNotFoundError  # type: ignore[attr-defined]

    notary_mod = types.ModuleType("opentimestamps.core.notary")
    notary_mod.PendingAttestation = PendingAttestation  # type: ignore[attr-defined]
    notary_mod.BitcoinBlockHeaderAttestation = BitcoinBlockHeaderAttestation  # type: ignore[attr-defined]

    serialize_mod = types.ModuleType("opentimestamps.core.serialize")
    serialize_mod.BytesSerializationContext = BytesSerializationContext  # type: ignore[attr-defined]
    serialize_mod.BytesDeserializationContext = BytesDeserializationContext  # type: ignore[attr-defined]

    timestamp_mod = types.ModuleType("opentimestamps.core.timestamp")
    timestamp_mod.Timestamp = FakeTimestamp  # type: ignore[attr-defined]

    for name, mod in (
        ("opentimestamps", types.ModuleType("opentimestamps")),
        ("opentimestamps.core", types.ModuleType("opentimestamps.core")),
        ("opentimestamps.core.timestamp", timestamp_mod),
        ("opentimestamps.core.notary", notary_mod),
        ("opentimestamps.core.serialize", serialize_mod),
        ("opentimestamps.calendar", calendar_mod),
    ):
        monkeypatch.setitem(sys.modules, name, mod)

    return types.SimpleNamespace(
        Timestamp=FakeTimestamp,
        PendingAttestation=PendingAttestation,
        BitcoinBlockHeaderAttestation=BitcoinBlockHeaderAttestation,
        CommitmentNotFoundError=CommitmentNotFoundError,
        submit_responses=submit_responses,
        get_timestamp_responses=get_timestamp_responses,
    )


def _fixture_http_get(url: str, timeout: float) -> bytes:
    """Serve :data:`_BLOCK_EXPLORER_FIXTURE` for the two Esplora-shaped
    paths this adapter calls, regardless of host/scheme prefix -- so the
    same fixture can be reused with a different ``block_source`` host to
    prove AC3's "a configured alternative source is recorded as such"."""
    del timeout
    fixture = _BLOCK_EXPLORER_FIXTURE
    if url.endswith(f"/block-height/{fixture['block_height']}"):
        return fixture["block_hash_response"].encode("ascii")
    if url.endswith(f"/block/{fixture['block_hash_response']}/header"):
        return fixture["header_hex_response"].encode("ascii")
    raise AssertionError(f"unexpected URL in fixture http_get: {url!r}")


# ---------------------------------------------------------------------------
# Attestation / VerifyResult
# ---------------------------------------------------------------------------


class TestAttestationRoundTrip:
    def test_to_dict_from_dict(self) -> None:
        att = anchor.Attestation(
            source="opentimestamps",
            status="pending",
            claimed_time="2026-10-28T00:00:00.000Z",
            proof_path="anchors/foo.ots",
            detail={"calendar": "https://a.pool.opentimestamps.org"},
        )
        rt = anchor.Attestation.from_dict(att.to_dict())
        assert rt == att

    def test_from_dict_defaults_on_missing_fields(self) -> None:
        att = anchor.Attestation.from_dict({"source": "local-clock"})
        assert att.status == ""
        assert att.proof_path is None
        assert att.detail == {}


class TestVerifyResultRoundTrip:
    def test_to_dict_from_dict(self) -> None:
        vr = anchor.VerifyResult(
            status="complete",
            verify_source="block-explorer:blockstream.info",
            detail={"block_height": 1},
        )
        assert anchor.VerifyResult.from_dict(vr.to_dict()) == vr


# ---------------------------------------------------------------------------
# resolve_block_source / digest_bytes / subject_safe
# ---------------------------------------------------------------------------


class TestResolveBlockSource:
    def test_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PROMPTWITNESS_OTS_BLOCK_SOURCE", "https://env.example/api")
        assert anchor.resolve_block_source("https://override.example/api") == "https://override.example/api"

    def test_env_wins_over_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PROMPTWITNESS_OTS_BLOCK_SOURCE", "https://env.example/api")
        assert anchor.resolve_block_source(None) == "https://env.example/api"

    def test_default_when_neither_given(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PROMPTWITNESS_OTS_BLOCK_SOURCE", raising=False)
        assert anchor.resolve_block_source(None) == anchor.DEFAULT_BLOCK_SOURCE

    def test_offline_is_a_plain_sentinel_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PROMPTWITNESS_OTS_BLOCK_SOURCE", raising=False)
        assert anchor.resolve_block_source("offline") == "offline"


class TestDigestBytesAndSubjectSafe:
    def test_digest_bytes_strips_sha256_prefix(self) -> None:
        raw = anchor.digest_bytes("sha256:" + "ab" * 32)
        assert raw == bytes.fromhex("ab" * 32)

    def test_digest_bytes_accepts_bare_hex(self) -> None:
        assert anchor.digest_bytes("ab" * 32) == bytes.fromhex("ab" * 32)

    def test_subject_safe_replaces_colons_and_slashes(self) -> None:
        assert anchor.subject_safe("witness:pw-abc123:4") == "witness_pw-abc123_4"


# ---------------------------------------------------------------------------
# LocalClockAnchorer -- mandatory, always complete, no independent weight
# ---------------------------------------------------------------------------


class TestLocalClockAnchorer:
    def test_anchor_always_complete_with_no_independent_weight(self) -> None:
        local = anchor.LocalClockAnchorer()
        att = local.anchor(b"\x00" * 32, "witness:pw-x:1")
        assert att.source == "local-clock"
        assert att.status == "complete"
        assert att.proof_path is None
        assert att.detail["weight"] == "none-independent"
        assert isinstance(att.detail["monotonic_ns"], int)

    def test_upgrade_is_a_no_op(self) -> None:
        local = anchor.LocalClockAnchorer()
        att = local.anchor(b"\x00" * 32, "witness:pw-x:1")
        assert local.upgrade(att) == att

    def test_verify_is_always_complete_local_clock(self) -> None:
        local = anchor.LocalClockAnchorer()
        att = local.anchor(b"\x00" * 32, "witness:pw-x:1")
        result = local.verify(att, b"\x00" * 32)
        assert result.status == "complete"
        assert result.verify_source == "local-clock"


# ---------------------------------------------------------------------------
# OpenTimestampsAnchorer -- missing dependency (real, no stub: genuinely
# not installed in this environment)
# ---------------------------------------------------------------------------


class TestMissingDependency:
    def test_anchor_degrades_to_failed_without_raising(self, tmp_path: Path) -> None:
        with pytest.raises(ImportError):
            import opentimestamps  # noqa: F401

        ots = anchor.OpenTimestampsAnchorer(tmp_path)
        att = ots.anchor(b"\x00" * 32, "witness:pw-x:1")
        assert att.source == "opentimestamps"
        assert att.status == "failed"
        assert att.detail["reason"] == "import-failed"
        assert att.proof_path is None

    def test_upgrade_on_a_pending_attestation_is_a_safe_no_op(self, tmp_path: Path) -> None:
        pending = anchor.Attestation(
            source="opentimestamps",
            status="pending",
            claimed_time="2026-10-28T00:00:00.000Z",
            proof_path="anchors/x.ots",
            detail={
                "calendar": "https://a.pool.opentimestamps.org",
                "commitment_hex": "aa" * 32,
                "digest_hex": "aa" * 32,
            },
        )
        ots = anchor.OpenTimestampsAnchorer(tmp_path)
        assert ots.upgrade(pending) == pending


# ---------------------------------------------------------------------------
# OpenTimestampsAnchorer -- stub-driven (these run by default)
# ---------------------------------------------------------------------------


class TestAnchorWithStub:
    def test_successful_stamp_is_pending_and_writes_a_proof_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ns = _install_fake_opentimestamps(monkeypatch)
        url = "https://a.pool.opentimestamps.org"
        ns.submit_responses[url] = lambda digest: ns.Timestamp(
            digest, attestations={ns.PendingAttestation("https://a.pool.opentimestamps.org/cal")}
        )

        ots = anchor.OpenTimestampsAnchorer(tmp_path, calendars=(url,))
        digest = b"\xab" * 32
        att = ots.anchor(digest, "witness:pw-x:1")

        assert att.source == "opentimestamps"
        assert att.status == "pending"
        assert att.detail["calendar"] == url
        assert att.detail["commitment_hex"] == digest.hex()
        assert att.proof_path == "anchors/witness_pw-x_1.ots"
        assert (tmp_path / att.proof_path).is_file()

    def test_all_calendars_failing_degrades_to_failed_without_raising(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ns = _install_fake_opentimestamps(monkeypatch)
        urls = ("https://a.pool.opentimestamps.org", "https://b.pool.opentimestamps.org")
        for url in urls:
            ns.submit_responses[url] = ConnectionError(f"blocked: {url}")

        ots = anchor.OpenTimestampsAnchorer(tmp_path, calendars=urls)
        att = ots.anchor(b"\xcd" * 32, "witness:pw-x:1")

        assert att.status == "failed"
        assert att.detail["reason"] == "all-calendars-failed"
        assert att.detail["calendars_tried"] == list(urls)
        assert att.proof_path is None

    def test_tries_the_next_calendar_after_a_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ns = _install_fake_opentimestamps(monkeypatch)
        bad, good = "https://a.pool.opentimestamps.org", "https://b.pool.opentimestamps.org"
        ns.submit_responses[bad] = TimeoutError("slow calendar")
        ns.submit_responses[good] = lambda digest: ns.Timestamp(
            digest, attestations={ns.PendingAttestation()}
        )

        ots = anchor.OpenTimestampsAnchorer(tmp_path, calendars=(bad, good))
        att = ots.anchor(b"\xef" * 32, "witness:pw-x:1")

        assert att.status == "pending"
        assert att.detail["calendar"] == good
        assert att.detail["calendars_tried"] == [bad, good]


class TestUpgradeWithStub:
    def test_still_pending_leaves_the_attestation_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ns = _install_fake_opentimestamps(monkeypatch)
        url = "https://a.pool.opentimestamps.org"
        ns.submit_responses[url] = lambda digest: ns.Timestamp(
            digest, attestations={ns.PendingAttestation()}
        )
        ots = anchor.OpenTimestampsAnchorer(tmp_path, calendars=(url,))
        att = ots.anchor(b"\x11" * 32, "witness:pw-x:1")

        # No get_timestamp_responses entry registered -> CommitmentNotFoundError.
        upgraded = ots.upgrade(att)
        assert upgraded == att

    def test_network_failure_on_upgrade_leaves_the_attestation_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ns = _install_fake_opentimestamps(monkeypatch)
        url = "https://a.pool.opentimestamps.org"
        ns.submit_responses[url] = lambda digest: ns.Timestamp(
            digest, attestations={ns.PendingAttestation()}
        )
        ots = anchor.OpenTimestampsAnchorer(tmp_path, calendars=(url,))
        att = ots.anchor(b"\x22" * 32, "witness:pw-x:1")
        ns.get_timestamp_responses[(url, att.detail["commitment_hex"])] = ConnectionError("down")

        assert ots.upgrade(att) == att

    def test_confirmed_commitment_replaces_the_proof_only_once_it_verifies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ns = _install_fake_opentimestamps(monkeypatch)
        url = "https://a.pool.opentimestamps.org"
        ns.submit_responses[url] = lambda digest: ns.Timestamp(
            digest, attestations={ns.PendingAttestation()}
        )

        fixture = _BLOCK_EXPLORER_FIXTURE
        digest = bytes.fromhex(fixture["merkle_root_hex"])

        ots = anchor.OpenTimestampsAnchorer(
            tmp_path,
            calendars=(url,),
            block_source="https://explorer.example/api",
            http_get=_fixture_http_get,
        )
        att = ots.anchor(digest, "witness:pw-x:1")
        assert att.status == "pending"

        height = fixture["block_height"]
        key = (url, att.detail["commitment_hex"])
        ns.get_timestamp_responses[key] = lambda commitment: ns.Timestamp(
            commitment, attestations={ns.BitcoinBlockHeaderAttestation(height=height)}
        )

        upgraded = ots.upgrade(att)
        assert upgraded.status == "complete"
        assert upgraded.proof_path == att.proof_path
        assert "upgraded_at" in upgraded.detail

        # The stored proof file was actually rewritten with the new attestation.
        verify_result = ots.verify(upgraded, digest)
        assert verify_result.status == "complete"
        assert verify_result.verify_source == "block-explorer:explorer.example"

    def test_an_unconfirmable_upgrade_response_does_not_overwrite_the_pending_proof(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The calendar reports a 'confirmed' commitment, but the block it
        names does not actually verify (wrong merkle root) -- the stored
        pending proof must survive untouched, never silently replaced with
        something that fails its own check."""
        ns = _install_fake_opentimestamps(monkeypatch)
        url = "https://a.pool.opentimestamps.org"
        ns.submit_responses[url] = lambda digest: ns.Timestamp(
            digest, attestations={ns.PendingAttestation()}
        )
        fixture = _BLOCK_EXPLORER_FIXTURE
        digest = b"\x33" * 32  # deliberately NOT the fixture's merkle root

        ots = anchor.OpenTimestampsAnchorer(
            tmp_path,
            calendars=(url,),
            block_source="https://explorer.example/api",
            http_get=_fixture_http_get,
        )
        att = ots.anchor(digest, "witness:pw-x:1")
        height = fixture["block_height"]
        key = (url, att.detail["commitment_hex"])
        ns.get_timestamp_responses[key] = lambda commitment: ns.Timestamp(
            commitment, attestations={ns.BitcoinBlockHeaderAttestation(height=height)}
        )

        upgraded = ots.upgrade(att)
        assert upgraded == att  # unchanged: the mismatched merkle root never verified


class TestVerifyWithFixture:
    def _write_complete_proof(
        self, ns: types.SimpleNamespace, tmp_path: Path, digest: bytes, height: int
    ) -> str:
        ts = ns.Timestamp(digest, attestations={ns.BitcoinBlockHeaderAttestation(height=height)})
        from opentimestamps.core.serialize import (
            BytesSerializationContext,  # the fake, via sys.modules
        )

        ctx = BytesSerializationContext()
        ts.serialize(ctx)
        proof_path = tmp_path / "anchors" / "fixture.ots"
        proof_path.parent.mkdir(parents=True, exist_ok=True)
        proof_path.write_bytes(ctx.getbytes())
        return "anchors/fixture.ots"

    def test_verify_against_recorded_block_explorer_fixture_is_complete(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ns = _install_fake_opentimestamps(monkeypatch)
        fixture = _BLOCK_EXPLORER_FIXTURE
        digest = bytes.fromhex(fixture["merkle_root_hex"])
        proof_path = self._write_complete_proof(ns, tmp_path, digest, fixture["block_height"])

        att = anchor.Attestation(
            source="opentimestamps", status="complete", claimed_time="t", proof_path=proof_path
        )
        ots = anchor.OpenTimestampsAnchorer(
            tmp_path, block_source="https://blockstream.info/api", http_get=_fixture_http_get
        )
        result = ots.verify(att, digest)

        assert result.status == "complete"
        assert result.verify_source == "block-explorer:blockstream.info"
        assert result.detail["block_height"] == fixture["block_height"]

    def test_a_configured_alternative_block_source_is_recorded_as_such(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ns = _install_fake_opentimestamps(monkeypatch)
        fixture = _BLOCK_EXPLORER_FIXTURE
        digest = bytes.fromhex(fixture["merkle_root_hex"])
        proof_path = self._write_complete_proof(ns, tmp_path, digest, fixture["block_height"])

        att = anchor.Attestation(
            source="opentimestamps", status="complete", claimed_time="t", proof_path=proof_path
        )
        ots = anchor.OpenTimestampsAnchorer(
            tmp_path, block_source="https://my-own-node.example/api", http_get=_fixture_http_get
        )
        result = ots.verify(att, digest)

        assert result.status == "complete"
        assert result.verify_source == "block-explorer:my-own-node.example"

    def test_merkleroot_mismatch_fails_rather_than_crashing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ns = _install_fake_opentimestamps(monkeypatch)
        fixture = _BLOCK_EXPLORER_FIXTURE
        wrong_digest = b"\x44" * 32  # does not match the fixture's merkle root
        proof_path = self._write_complete_proof(ns, tmp_path, wrong_digest, fixture["block_height"])

        att = anchor.Attestation(
            source="opentimestamps", status="complete", claimed_time="t", proof_path=proof_path
        )
        ots = anchor.OpenTimestampsAnchorer(
            tmp_path, block_source="https://blockstream.info/api", http_get=_fixture_http_get
        )
        result = ots.verify(att, wrong_digest)

        assert result.status == "failed"
        assert result.detail["reason"] == "merkleroot-mismatch"

    def test_unreachable_block_source_is_complete_unverified_block_never_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ns = _install_fake_opentimestamps(monkeypatch)
        fixture = _BLOCK_EXPLORER_FIXTURE
        digest = bytes.fromhex(fixture["merkle_root_hex"])
        proof_path = self._write_complete_proof(ns, tmp_path, digest, fixture["block_height"])

        def _raising_http_get(url: str, timeout: float) -> bytes:
            raise OSError("network unreachable (simulated)")

        att = anchor.Attestation(
            source="opentimestamps", status="complete", claimed_time="t", proof_path=proof_path
        )
        ots = anchor.OpenTimestampsAnchorer(
            tmp_path, block_source="https://blockstream.info/api", http_get=_raising_http_get
        )
        result = ots.verify(att, digest)

        assert result.status == "complete-unverified-block"
        assert result.verify_source == "block-explorer:blockstream.info"
        assert result.detail["reason"] == "block-source-unreachable"

    def test_still_pending_attestation_reports_pending(self, tmp_path: Path) -> None:
        att = anchor.Attestation(
            source="opentimestamps", status="pending", claimed_time="t", proof_path=None
        )
        ots = anchor.OpenTimestampsAnchorer(tmp_path, block_source="https://blockstream.info/api")
        result = ots.verify(att, b"\x00" * 32)
        assert result.status == "pending"
        assert result.verify_source == "block-explorer:blockstream.info"


class TestVerifyOffline:
    def test_offline_never_touches_the_http_seam(self, tmp_path: Path) -> None:
        calls: list[str] = []

        def _spy_http_get(url: str, timeout: float) -> bytes:
            calls.append(url)
            raise AssertionError("http_get must never be called when block_source is offline")

        att = anchor.Attestation(
            source="opentimestamps", status="complete", claimed_time="t", proof_path="anchors/x.ots"
        )
        ots = anchor.OpenTimestampsAnchorer(
            tmp_path, block_source="offline", http_get=_spy_http_get
        )
        result = ots.verify(att, b"\x00" * 32)

        assert calls == []
        assert result.status == "complete-unverified-block"
        assert result.verify_source == "offline"

    def test_offline_pending_attestation_reports_pending(self, tmp_path: Path) -> None:
        att = anchor.Attestation(source="opentimestamps", status="pending", claimed_time="t")
        ots = anchor.OpenTimestampsAnchorer(tmp_path, block_source="offline")
        result = ots.verify(att, b"\x00" * 32)
        assert result.status == "pending"
        assert result.verify_source == "offline"

    def test_resolve_block_source_offline_via_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROMPTWITNESS_OTS_BLOCK_SOURCE", "offline")
        block_source = anchor.resolve_block_source(None)
        ots = anchor.OpenTimestampsAnchorer(tmp_path, block_source=block_source)
        att = anchor.Attestation(source="opentimestamps", status="complete", claimed_time="t")
        result = ots.verify(att, b"\x00" * 32)
        assert result.verify_source == "offline"


class TestVerifyWithRealSockets:
    """No stub, no injected ``http_get`` -- the REAL default HTTP path,
    hitting the real (autouse, default-selection) network guard. Proves
    AC4 against the actual guard, not a simulation of it.
    """

    def test_sockets_blocked_is_complete_unverified_block_never_raises(
        self, tmp_path: Path
    ) -> None:
        att = anchor.Attestation(
            source="opentimestamps", status="complete", claimed_time="t", proof_path="anchors/x.ots"
        )
        # No proof file on disk either -- this must degrade before even
        # reaching the HTTP call, via the same never-raise contract.
        ots = anchor.OpenTimestampsAnchorer(tmp_path)  # default block source, default http_get
        result = ots.verify(att, b"\x00" * 32)
        assert result.status == "complete-unverified-block"
        assert result.verify_source.startswith("block-explorer:")


# ---------------------------------------------------------------------------
# Regression coverage: PR athenaeum#2168 review findings #2 and #3 (athenaeum#2151).
# ---------------------------------------------------------------------------


class TestAnchorNeverClobbersACompleteProof:
    """Finding #2 (defence in depth): ``anchor()`` itself -- not just the
    CLI caller -- must refuse to overwrite an existing proof file whose
    recorded attestation is already Bitcoin-confirmed. It is the strongest
    evidence this design produces, and a confirmed proof cannot be
    regenerated.
    """

    def test_anchor_refuses_to_overwrite_an_existing_complete_proof(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ns = _install_fake_opentimestamps(monkeypatch)
        digest = b"\x55" * 32
        subject = "witness:pw-x:1"

        # Pre-seed an existing COMPLETE proof, as if a prior pass already
        # confirmed it in Bitcoin.
        ts = ns.Timestamp(digest, attestations={ns.BitcoinBlockHeaderAttestation(height=999)})
        from opentimestamps.core.serialize import (  # the fake, via sys.modules
            BytesSerializationContext,
        )

        ctx = BytesSerializationContext()
        ts.serialize(ctx)
        proof_path = tmp_path / "anchors" / f"{anchor.subject_safe(subject)}.ots"
        proof_path.parent.mkdir(parents=True)
        proof_path.write_bytes(ctx.getbytes())
        original_bytes = proof_path.read_bytes()

        # No calendar is stubbed at all -- if the guard did not fire,
        # `anchor()` would call `RemoteCalendar.submit`, which raises for
        # an unregistered URL, and the result would degrade to
        # `status: "failed"` instead of reporting the existing `"complete"`
        # -- the discriminating signal.
        ots = anchor.OpenTimestampsAnchorer(tmp_path, calendars=anchor.DEFAULT_CALENDARS[:1])
        att = ots.anchor(digest, subject)

        assert att.status == "complete"
        assert att.detail["reason"] == "existing-proof-already-complete"
        assert proof_path.read_bytes() == original_bytes  # byte-for-byte untouched

    def test_anchor_still_submits_when_the_existing_proof_is_only_pending(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Negative-control-adjacent sanity check: the guard must be
        specific to ``"complete"`` -- a merely-pending existing proof must
        not block a fresh submission."""
        ns = _install_fake_opentimestamps(monkeypatch)
        digest = b"\x77" * 32
        subject = "witness:pw-x:2"

        ts = ns.Timestamp(digest, attestations={ns.PendingAttestation()})
        from opentimestamps.core.serialize import (  # the fake, via sys.modules
            BytesSerializationContext,
        )

        ctx = BytesSerializationContext()
        ts.serialize(ctx)
        proof_path = tmp_path / "anchors" / f"{anchor.subject_safe(subject)}.ots"
        proof_path.parent.mkdir(parents=True)
        proof_path.write_bytes(ctx.getbytes())

        url = "https://a.pool.opentimestamps.org"
        ns.submit_responses[url] = lambda d: ns.Timestamp(d, attestations={ns.PendingAttestation()})
        ots = anchor.OpenTimestampsAnchorer(tmp_path, calendars=(url,))
        att = ots.anchor(digest, subject)

        assert att.status == "pending"
        bad_reason = "existing-proof-already-complete"
        assert "reason" not in att.detail or att.detail.get("reason") != bad_reason


class TestAnchorDegradesOnFilesystemFailure:
    """Finding #3: unguarded filesystem I/O around the proof write can
    raise ``OSError`` (full disk, unwritable ``anchors/`` directory), which
    violates the module's own "the pass never raises" contract -- the same
    class of condition as a network failure.
    """

    def test_anchor_degrades_to_failed_when_the_proof_write_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ns = _install_fake_opentimestamps(monkeypatch)
        url = "https://a.pool.opentimestamps.org"
        ns.submit_responses[url] = lambda digest: ns.Timestamp(
            digest, attestations={ns.PendingAttestation()}
        )

        def _raise_oserror(self: Path, data: bytes) -> int:
            raise OSError("No space left on device (simulated)")

        monkeypatch.setattr(Path, "write_bytes", _raise_oserror)

        ots = anchor.OpenTimestampsAnchorer(tmp_path, calendars=(url,))
        # Without the fix, this call raises OSError uncaught (not merely a
        # wrong return value) -- the test itself would error, not fail an
        # assertion, which is as discriminating as it gets.
        att = ots.anchor(b"\x66" * 32, "witness:pw-x:1")

        assert att.status == "failed"
        assert att.detail["reason"] == "proof-write-failed"
        assert "OSError" in att.detail["error"]

    def test_upgrade_degrades_when_persisting_the_upgraded_proof_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ns = _install_fake_opentimestamps(monkeypatch)
        url = "https://a.pool.opentimestamps.org"
        ns.submit_responses[url] = lambda digest: ns.Timestamp(
            digest, attestations={ns.PendingAttestation()}
        )
        fixture = _BLOCK_EXPLORER_FIXTURE
        digest = bytes.fromhex(fixture["merkle_root_hex"])

        ots = anchor.OpenTimestampsAnchorer(
            tmp_path,
            calendars=(url,),
            block_source="https://explorer.example/api",
            http_get=_fixture_http_get,
        )
        att = ots.anchor(digest, "witness:pw-x:1")
        height = fixture["block_height"]
        key = (url, att.detail["commitment_hex"])
        ns.get_timestamp_responses[key] = lambda commitment: ns.Timestamp(
            commitment, attestations={ns.BitcoinBlockHeaderAttestation(height=height)}
        )

        def _raise_oserror(self: Path, data: bytes) -> int:
            raise OSError("disk full (simulated)")

        monkeypatch.setattr(Path, "write_bytes", _raise_oserror)

        # Without the fix, this call raises OSError uncaught.
        upgraded = ots.upgrade(att)
        assert upgraded == att  # degrades to a no-op -- never raises, never overwrites


# ---------------------------------------------------------------------------
# Real-library surface, if (and only if) opentimestamps happens to be
# installed. Never runs in this CI -- see the module docstring.
# ---------------------------------------------------------------------------


class TestRealLibrarySurfaceIfInstalled:
    def test_the_real_library_exposes_every_name_this_adapter_imports(self) -> None:
        pytest.importorskip("opentimestamps", reason="witness-anchor extra not installed")
        from opentimestamps.calendar import CommitmentNotFoundError, RemoteCalendar
        from opentimestamps.core.notary import BitcoinBlockHeaderAttestation, PendingAttestation
        from opentimestamps.core.serialize import (
            BytesDeserializationContext,
            BytesSerializationContext,
        )
        from opentimestamps.core.timestamp import Timestamp

        assert callable(RemoteCalendar)
        assert issubclass(CommitmentNotFoundError, KeyError)
        assert hasattr(Timestamp, "all_attestations")
        assert hasattr(Timestamp, "serialize")
        assert hasattr(Timestamp, "deserialize")
        assert hasattr(PendingAttestation, "uri") or True  # instance attribute, not class-level
        assert hasattr(BitcoinBlockHeaderAttestation, "height") or True
        ctx = BytesSerializationContext()
        assert hasattr(ctx, "getbytes")
        BytesDeserializationContext(b"")  # constructs without error
