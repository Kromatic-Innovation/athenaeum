# SPDX-License-Identifier: Apache-2.0
"""Tests for athenaeum.witness_attestation (issue athenaeum#2158; signature
verification and anchor source added by issue athenaeum#2333).

Covers the eleven-condition hit rule, the fixed miss vocabulary (one reason
per forgery pin, never more than one), the exactly-one-WARNING logging
contract (including the provenance-incident variant), ``AttestationIndex.
load``'s read-budget/anchor-coverage behaviour, and (athenaeum#2333)
``AttestationIndex.verified_strength``/``anchor_source`` plus the
hit-with-incident ``witness-strength-mismatch`` shape.
``tests/test_transcript_receipts.py`` covers the INTEGRATION through
``write_receipt_for_origin``/``write_receipts_for_files`` (receipt schema,
flag-off byte-identity, the gate regression pin, v1 parse/never-rewrite).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from athenaeum.witness_attestation import MISS_REASONS, AttestationIndex, attest
from promptwitness import canon, ledger
from promptwitness.harness import claude_code
from promptwitness.signing import derive_key_id

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

#: Baseline claim/utterance pair: the claim is a substring of the matched
#: human-authored utterance, exactly how `_matching_human_utterance` finds it.
_CLAIM = "the winning claim"
_UTTERANCE_TEXT = "the user literally said: the winning claim, right here"
_UTTERANCE_DIGEST = hashlib.sha256(canon.normalize(_UTTERANCE_TEXT).encode("utf-8")).hexdigest()


def _append_turn(d: Path, **kwargs: object) -> ledger.WitnessLine:
    defaults: dict[str, object] = {
        "kind": "turn",
        "harness": "claude-code",
        "harness_version": "2.1.308",
        "hook_version": "promptwitness 0.1.0",
        "session": "s1",
        "turn": 1,
        "prompt_id": "p1",
        "prompt_kind": "human",
        "prompt_digest": canon.digest(canon.normalize(_UTTERANCE_TEXT)),
        "raw_digest": canon.digest(_UTTERANCE_TEXT),
        "prompt_chars": len(_UTTERANCE_TEXT),
        "channel": "interactive",
        "resumed": False,
        "signals": {"entrypoint": "cli"},
    }
    defaults.update(kwargs)
    return ledger.append_line(d, **defaults)  # type: ignore[arg-type]


def _good_attest_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "prompt_id": "p1",
        "origin_session_id": "s1",
        "claim": _CLAIM,
        "utterance_digest": _UTTERANCE_DIGEST,
        "matched_record_text": _UTTERANCE_TEXT,
        "transcript_origin_kind": "human",
        "transcript_prompt_source": "typed",
        "transcript_is_sidechain": False,
    }
    kwargs.update(overrides)
    return kwargs


@pytest.fixture(autouse=True)
def _no_append_only_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tamper tests below rewrite a partition in place. On macOS the
    ledger marks new partitions ``uappnd``, which makes that rewrite fail
    with PermissionError; skip the flag so the tests run on every host."""
    monkeypatch.setattr(
        ledger, "_apply_append_only_protection", lambda _p: {"protection": "unsupported"}
    )


def _read_partition_lines(path: Path) -> list[bytes]:
    return [ln for ln in path.read_bytes().split(b"\n") if ln.strip()]


def _single_partition(d: Path) -> Path:
    names = ledger.list_partitions(d)
    assert len(names) == 1
    return d / names[0]


@dataclass
class _StubKeyProvider:
    """A ``promptwitness.ledger.KeyProviderLike``-shaped stub (issue
    athenaeum#2333 tests only): a REAL, in-memory-only Ed25519 keypair that
    can CLAIM any strength label — the shipped adapters only ever claim
    ``"software-file"``/``"os-keystore"``/``"none"``, but these tests
    exercise the claimed-vs-registered COMPARISON, not which adapter
    produced the claim, so a stub lets a fixture match the issue's own
    ``os-keystore``/``hardware-presence`` wording exactly. Never writes a
    PEM anywhere — ``public_key()`` uses ``Encoding.Raw``/``PublicFormat.
    Raw``, never ``private_bytes``, so no key material (public or private)
    ever takes PEM shape in this process."""

    signer: Ed25519PrivateKey
    claimed_strength: str

    def key_id(self) -> str:
        return derive_key_id(self.public_key(), "ed25519")

    def algorithm(self) -> str:
        return "ed25519"

    def strength(self) -> str:
        return self.claimed_strength

    def presence_required(self) -> bool:
        return False

    def sign(self, payload: bytes) -> bytes:
        return bytes(self.signer.sign(payload))

    def public_key(self) -> bytes:
        return self.signer.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def _stub_provider(claimed_strength: str) -> _StubKeyProvider:
    return _StubKeyProvider(Ed25519PrivateKey.generate(), claimed_strength)


def _registry_config(provider: _StubKeyProvider, *, registered_strength: str) -> dict[str, Any]:
    """A ``librarian.witness.keys`` config dict registering *provider*'s
    public key at *registered_strength* — the exact shape
    ``athenaeum.config.resolve_witness_keys`` reads."""
    return {
        "librarian": {
            "witness": {
                "keys": {
                    provider.key_id(): {
                        "public_key": base64.b64encode(provider.public_key()).decode("ascii"),
                        "strength": registered_strength,
                    }
                }
            }
        }
    }


# ---------------------------------------------------------------------------
# The hit path
# ---------------------------------------------------------------------------


class TestHit:
    def test_human_hit_carries_tag_ref_strength(self, tmp_path: Path) -> None:
        line = _append_turn(tmp_path)
        index = AttestationIndex.load(tmp_path)
        outcome = attest(index, **_good_attest_kwargs())  # type: ignore[arg-type]
        assert outcome.hit
        assert outcome.miss_reason is None
        assert outcome.witness_tag == "typed"
        assert outcome.witness_ref == f"{line.ledger_id}:{line.seq}"
        assert outcome.witness_strength == "none"  # unsigned fixture
        assert outcome.witness_anchored is False  # no anchor line in this ledger
        assert outcome.transcript_entrypoint == "cli"

    def test_fallback_lookup_by_session_and_digest_when_no_prompt_id(self, tmp_path: Path) -> None:
        _append_turn(tmp_path, prompt_id=None)
        index = AttestationIndex.load(tmp_path)
        outcome = attest(index, **_good_attest_kwargs(prompt_id=None))  # type: ignore[arg-type]
        assert outcome.hit

    def test_hit_is_anchored_when_an_anchor_line_covers_its_seq(self, tmp_path: Path) -> None:
        line = _append_turn(tmp_path)
        ledger.append_line(
            tmp_path,
            kind="anchor",
            harness="claude-code",
            harness_version="",
            hook_version="promptwitness 0.1.0",
            extra={
                "subject": f"witness:{line.ledger_id}:{line.seq}",
                "digest": line.line_hash(),
                "op": "anchor",
                "attestation": {"status": "complete", "source": "opentimestamps"},
            },
        )
        index = AttestationIndex.load(tmp_path)
        outcome = attest(index, **_good_attest_kwargs())  # type: ignore[arg-type]
        assert outcome.hit
        assert outcome.witness_anchored is True


class TestSlashCommandHit:
    """AC2: a slash-command claim inside <command-args> attests with
    `witness_tag: "slash-args"`; a claim only in the expanded skill text
    (outside the args span) does not.

    The witness line is built through the REAL producer
    (`claude_code.record_turn`, which calls `slash_args_segment`
    internally) rather than a hand-built `Segment` -- `slash_args_segment`
    hashes the RAW `<command-args>` span, not a normalized one (see
    `witness_attestation`'s "Known deviation" docstring note), so a
    hand-computed digest would make this test pass for a reason the real
    producer cannot reach. Driving the actual hook-record path is what
    makes this a genuine control: if the producer's digest formula ever
    changes, this test breaks along with it."""

    _ARGS_TEXT = "fix the typo in README"
    _PROMPT = (
        "<command-message>review</command-message>"
        "<command-name>review</command-name>"
        f"<command-args>{_ARGS_TEXT}</command-args>\n"
        "Also remember the project's general typo-reporting policy."
    )

    def _record_via_real_producer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> ledger.WitnessLine:
        monkeypatch.setenv("PROMPTWITNESS_DIR", str(tmp_path))
        monkeypatch.setattr(claude_code, "_probe_has_tty", lambda pid: True)
        raw = json.dumps({"prompt": self._PROMPT, "session_id": "s1", "prompt_id": "p1"})
        env = {"CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_CODE_SESSION_ATTENDED": "1"}
        return claude_code.record_turn(raw, env)

    def test_claim_inside_command_args_attests_as_slash_args(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        line = self._record_via_real_producer(tmp_path, monkeypatch)
        assert line.prompt_kind == "slash-command"
        assert any(s.kind == "slash-args" for s in line.segments)  # sanity: real producer wrote it
        index = AttestationIndex.load(tmp_path)
        outcome = attest(
            index,
            prompt_id="p1",
            origin_session_id="s1",
            claim="fix the typo in readme",  # case-insensitive substring of _ARGS_TEXT
            utterance_digest=_UTTERANCE_DIGEST,  # irrelevant on the slash-command path
            matched_record_text=self._PROMPT,
            transcript_origin_kind="human",
            transcript_prompt_source="typed",
            transcript_is_sidechain=False,
        )
        assert outcome.hit
        assert outcome.witness_tag == "slash-args"
        assert outcome.witness_ref == f"{line.ledger_id}:{line.seq}"

    def test_claim_only_in_expanded_skill_text_does_not_attest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._record_via_real_producer(tmp_path, monkeypatch)
        index = AttestationIndex.load(tmp_path)
        outcome = attest(
            index,
            prompt_id="p1",
            origin_session_id="s1",
            claim="typo-reporting policy",  # only in the boilerplate, not in <command-args>
            utterance_digest=_UTTERANCE_DIGEST,
            matched_record_text=self._PROMPT,
            transcript_origin_kind="human",
            transcript_prompt_source="typed",
            transcript_is_sidechain=False,
        )
        assert not outcome.hit
        assert outcome.miss_reason == "digest-mismatch"

    def test_a_tampered_transcript_args_span_also_misses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Negative control for the digest-equality check itself: the
        witness line is genuine, but the TRANSCRIPT record's `<command-args>`
        text has been altered since -- this must still miss on
        `digest-mismatch`, proving the comparison is not vacuously true."""
        self._record_via_real_producer(tmp_path, monkeypatch)
        index = AttestationIndex.load(tmp_path)
        tampered_prompt = self._PROMPT.replace(self._ARGS_TEXT, "a totally different instruction")
        outcome = attest(
            index,
            prompt_id="p1",
            origin_session_id="s1",
            claim="a totally different instruction",
            utterance_digest=_UTTERANCE_DIGEST,
            matched_record_text=tampered_prompt,
            transcript_origin_kind="human",
            transcript_prompt_source="typed",
            transcript_is_sidechain=False,
        )
        assert not outcome.hit
        assert outcome.miss_reason == "digest-mismatch"


# ---------------------------------------------------------------------------
# The eleven original forgery pins — each isolates exactly one failing
# condition. The twelfth miss reason, `witness-strength-mismatch`, is NOT
# a forgery pin in this sense: it is reported alongside a HIT, not instead
# of one (see TestWitnessStrengthVerification below).
# ---------------------------------------------------------------------------


class TestForgeryPins:
    def test_no_witness_line_at_all_is_ledger_absent(self, tmp_path: Path) -> None:
        # The directory is never even created.
        index = AttestationIndex.load(tmp_path / "never-created")
        outcome = attest(index, **_good_attest_kwargs())  # type: ignore[arg-type]
        assert not outcome.hit
        assert outcome.miss_reason == "ledger-absent"

    def test_ledger_present_but_no_matching_line_is_no_entry(self, tmp_path: Path) -> None:
        _append_turn(tmp_path, prompt_id="someone-elses-prompt", session="other-session")
        index = AttestationIndex.load(tmp_path)
        outcome = attest(
            index,
            **_good_attest_kwargs(prompt_id="p-does-not-exist"),  # type: ignore[arg-type]
        )
        assert not outcome.hit
        assert outcome.miss_reason == "no-entry"

    def test_headless_line_fails_channel_check(self, tmp_path: Path) -> None:
        _append_turn(tmp_path, channel="headless")
        index = AttestationIndex.load(tmp_path)
        outcome = attest(index, **_good_attest_kwargs())  # type: ignore[arg-type]
        assert not outcome.hit
        assert outcome.miss_reason == "channel-not-interactive:headless"

    def test_bridge_line_fails_channel_check(self, tmp_path: Path) -> None:
        _append_turn(tmp_path, channel="bridge")
        index = AttestationIndex.load(tmp_path)
        outcome = attest(index, **_good_attest_kwargs())  # type: ignore[arg-type]
        assert not outcome.hit
        assert outcome.miss_reason == "channel-not-interactive:bridge"

    def test_task_notification_kind_fails_human_shape(self, tmp_path: Path) -> None:
        _append_turn(tmp_path, prompt_kind="task-notification")
        index = AttestationIndex.load(tmp_path)
        outcome = attest(index, **_good_attest_kwargs())  # type: ignore[arg-type]
        assert not outcome.hit
        assert outcome.miss_reason == "kind-not-human:task-notification"

    def test_subagent_kind_fails_human_shape(self, tmp_path: Path) -> None:
        _append_turn(
            tmp_path,
            prompt_kind="subagent",
            signals={"entrypoint": "cli", "agent_id": "a1"},
        )
        index = AttestationIndex.load(tmp_path)
        outcome = attest(index, **_good_attest_kwargs())  # type: ignore[arg-type]
        assert not outcome.hit
        assert outcome.miss_reason == "kind-not-human:subagent"

    def test_launch_argument_kind_fails_human_shape(self, tmp_path: Path) -> None:
        _append_turn(tmp_path, prompt_kind="launch-argument")
        index = AttestationIndex.load(tmp_path)
        outcome = attest(index, **_good_attest_kwargs())  # type: ignore[arg-type]
        assert not outcome.hit
        assert outcome.miss_reason == "kind-not-human:launch-argument"

    def test_digest_mismatch_on_the_human_path(self, tmp_path: Path) -> None:
        _append_turn(
            tmp_path,
            prompt_digest=canon.digest(canon.normalize("something else entirely")),
        )
        index = AttestationIndex.load(tmp_path)
        outcome = attest(index, **_good_attest_kwargs())  # type: ignore[arg-type]
        assert not outcome.hit
        assert outcome.miss_reason == "digest-mismatch"

    def test_different_session_fails_session_check(self, tmp_path: Path) -> None:
        _append_turn(tmp_path, session="a-different-session")
        index = AttestationIndex.load(tmp_path)
        outcome = attest(index, **_good_attest_kwargs())  # type: ignore[arg-type]
        assert not outcome.hit
        assert outcome.miss_reason == "session-mismatch"

    def test_chain_broken_before_the_line(self, tmp_path: Path) -> None:
        _append_turn(
            tmp_path,
            prompt_id="p0",
            turn=0,
            session="s1",
            prompt_digest=canon.digest(canon.normalize("p0 text")),
        )
        target = _append_turn(tmp_path, prompt_id="p1", turn=1)  # our target, seq 2
        _append_turn(
            tmp_path,
            prompt_id="p2",
            turn=2,
            prompt_digest=canon.digest(canon.normalize("p2 text")),
        )

        partition = _single_partition(tmp_path)
        lines = _read_partition_lines(partition)
        obj = json.loads(lines[0])  # seq == 1 (p0) -- breaks seq 2's prev check
        obj["prompt_chars"] = 999999
        lines[0] = canon.canonical_json(obj)
        partition.write_bytes(b"\n".join(lines) + b"\n")

        index = AttestationIndex.load(tmp_path)
        assert not index.chain_ok
        assert index.first_break_seq == target.seq
        outcome = attest(index, **_good_attest_kwargs())  # type: ignore[arg-type]
        assert not outcome.hit
        assert outcome.miss_reason == "chain-broken"

    def test_transcript_origin_kind_not_human(self, tmp_path: Path) -> None:
        _append_turn(tmp_path)
        index = AttestationIndex.load(tmp_path)
        outcome = attest(
            index,
            **_good_attest_kwargs(transcript_origin_kind="task-notification"),  # type: ignore[arg-type]
        )
        assert not outcome.hit
        assert outcome.miss_reason == "transcript-origin-not-human"

    def test_transcript_prompt_source_absent(self, tmp_path: Path) -> None:
        _append_turn(tmp_path)
        index = AttestationIndex.load(tmp_path)
        outcome = attest(
            index,
            **_good_attest_kwargs(transcript_prompt_source=None),  # type: ignore[arg-type]
        )
        assert not outcome.hit
        assert outcome.miss_reason == "transcript-promptsource:absent"

    def test_transcript_prompt_source_sdk(self, tmp_path: Path) -> None:
        _append_turn(tmp_path)
        index = AttestationIndex.load(tmp_path)
        outcome = attest(
            index,
            **_good_attest_kwargs(transcript_prompt_source="sdk"),  # type: ignore[arg-type]
        )
        assert not outcome.hit
        assert outcome.miss_reason == "transcript-promptsource:sdk"

    def test_is_sidechain_true(self, tmp_path: Path) -> None:
        _append_turn(tmp_path)
        index = AttestationIndex.load(tmp_path)
        outcome = attest(
            index,
            **_good_attest_kwargs(transcript_is_sidechain=True),  # type: ignore[arg-type]
        )
        assert not outcome.hit
        assert outcome.miss_reason == "sidechain"

    def test_flag_off_short_circuits_with_no_io(self, tmp_path: Path) -> None:
        # No ledger directory exists at all -- if `attest` touched the
        # filesystem on this path it would still return cleanly (the dir
        # simply doesn't exist), so this is really pinning "index=None is
        # the only thing consulted", via MISS_REASONS membership + the
        # logging test below proving zero I/O indirectly (no warning, no
        # ledger-absent reason either).
        outcome = attest(None, **_good_attest_kwargs())  # type: ignore[arg-type]
        assert not outcome.hit
        assert outcome.miss_reason == "flag-off"

    def test_every_miss_reason_prefix_is_in_the_declared_vocabulary(self) -> None:
        for reason in (
            "ledger-absent",
            "no-entry",
            "kind-not-human:x",
            "channel-not-interactive:x",
            "digest-mismatch",
            "session-mismatch",
            "chain-broken",
            "transcript-origin-not-human",
            "transcript-promptsource:x",
            "sidechain",
            "flag-off",
            "witness-strength-mismatch:os-keystore",
        ):
            prefix = reason.split(":", 1)[0]
            assert prefix in MISS_REASONS


# ---------------------------------------------------------------------------
# Logging: exactly one WARNING per non-flag-off miss, no text/digest, and
# the provenance-incident variant.
# ---------------------------------------------------------------------------


class TestMissLogging:
    def test_miss_logs_exactly_one_warning_with_reason_and_ref(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _append_turn(tmp_path, session="a-different-session")
        index = AttestationIndex.load(tmp_path)
        with caplog.at_level(logging.WARNING, logger="athenaeum.witness_attestation"):
            outcome = attest(index, **_good_attest_kwargs())  # type: ignore[arg-type]
        assert outcome.miss_reason == "session-mismatch"
        records = [r for r in caplog.records if r.name == "athenaeum.witness_attestation"]
        assert len(records) == 1
        message = records[0].getMessage()
        assert "session-mismatch" in message
        assert "s1" in message  # the session, not the claim or any digest
        assert _CLAIM not in message
        assert _UTTERANCE_DIGEST not in message
        assert "sha256" not in message

    def test_flag_off_logs_nothing(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="athenaeum.witness_attestation"):
            attest(None, **_good_attest_kwargs())  # type: ignore[arg-type]
        assert caplog.records == []

    def test_provenance_incident_variant_still_logs_exactly_once(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Looks human-and-interactive (origin.kind human, witness signals
        # entrypoint "cli") yet still misses on an unrelated condition --
        # the strongest "something is wrong" signal, logged distinctly.
        _append_turn(tmp_path, prompt_digest=canon.digest(canon.normalize("tampered")))
        index = AttestationIndex.load(tmp_path)
        with caplog.at_level(logging.WARNING, logger="athenaeum.witness_attestation"):
            outcome = attest(index, **_good_attest_kwargs())  # type: ignore[arg-type]
        assert outcome.miss_reason == "digest-mismatch"
        records = [r for r in caplog.records if r.name == "athenaeum.witness_attestation"]
        assert len(records) == 1
        assert "PROVENANCE INCIDENT" in records[0].getMessage()

    def test_hit_logs_nothing(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        _append_turn(tmp_path)
        index = AttestationIndex.load(tmp_path)
        with caplog.at_level(logging.WARNING, logger="athenaeum.witness_attestation"):
            outcome = attest(index, **_good_attest_kwargs())  # type: ignore[arg-type]
        assert outcome.hit
        assert caplog.records == []


# ---------------------------------------------------------------------------
# AttestationIndex.load — read budget / anchor coverage
# ---------------------------------------------------------------------------


class TestAttestationIndexLoad:
    def test_ledger_present_false_for_a_directory_with_no_partitions(self, tmp_path: Path) -> None:
        witness_dir = tmp_path / "empty"
        witness_dir.mkdir()
        index = AttestationIndex.load(witness_dir)
        assert index.ledger_present is False

    def test_read_count_is_bounded_by_partitions_not_by_lookups(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """issue athenaeum#2158 read-budget AC: loading the index touches each
        existing partition a small, FIXED number of times (indexing, chain
        verification, anchor scan) regardless of how many memories a run
        later looks up against it -- never once per lookup."""
        for i in range(12):
            _append_turn(tmp_path, prompt_id=f"p{i}", turn=i, session="s1")

        opens = {"count": 0}
        real_read_bytes = Path.read_bytes

        def counting_read_bytes(self: Path) -> bytes:
            if self.is_relative_to(tmp_path) and self.suffix == ".jsonl":
                opens["count"] += 1
            return real_read_bytes(self)

        monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)
        index = AttestationIndex.load(tmp_path)
        loads_opens = opens["count"]
        assert loads_opens > 0
        # One partition exists; loads_opens is the fixed per-partition cost
        # of `load()` itself (indexing + chain-verify + anchor-scan passes).
        opens["count"] = 0
        for i in range(12):
            index.lookup(f"p{i}", "s1", "sha256:unused")
        assert opens["count"] == 0, "a lookup must never re-read the ledger from disk"


class TestChainOkField:
    def test_chain_ok_true_for_a_clean_ledger(self, tmp_path: Path) -> None:
        _append_turn(tmp_path)
        index = AttestationIndex.load(tmp_path)
        assert index.chain_ok is True
        assert index.first_break_seq is None


# ---------------------------------------------------------------------------
# chain_intact_through boundary (PR athenaeum#2177 review finding)
# ---------------------------------------------------------------------------


def _four_turn_ledger(d: Path) -> list[ledger.WitnessLine]:
    """Turns p0..p3 (seq 1..4); only p1 carries the attested digest."""
    out = []
    for i in range(4):
        digest = (
            canon.digest(canon.normalize(_UTTERANCE_TEXT))
            if i == 1
            else canon.digest(canon.normalize(f"p{i} text"))
        )
        out.append(_append_turn(d, prompt_id=f"p{i}", turn=i, prompt_digest=digest))
    return out


def _rewrite(d: Path, edit) -> None:
    """Apply *edit* to the partition's list of raw line objects in place."""
    partition = _single_partition(d)
    objs = [json.loads(ln) for ln in _read_partition_lines(partition)]
    edit(objs)
    partition.write_bytes(b"".join(canon.canonical_json(o) + b"\n" for o in objs))


def _edit_content(position: int):
    def edit(objs: list[dict]) -> None:
        objs[position]["prompt_chars"] = 999999

    return edit


class TestChainIntactThroughBoundary:
    """A content edit to line N surfaces as a break at N+1. Before the fix
    ``seq < first_break_seq`` passed N itself, so the edited line attested."""

    def test_edited_first_line_is_not_intact(self, tmp_path: Path) -> None:
        lines = _four_turn_ledger(tmp_path)
        _rewrite(tmp_path, _edit_content(0))
        index = AttestationIndex.load(tmp_path)
        assert index.first_break_seq == lines[1].seq
        assert index.chain_intact_through(lines[0].seq) is False
        assert index.chain_intact_through(lines[1].seq) is False

    def test_genesis_prev_edit_leaves_nothing_intact(self, tmp_path: Path) -> None:
        lines = _four_turn_ledger(tmp_path)

        def edit(objs: list[dict]) -> None:
            objs[0]["prev"] = "sha256:" + "f" * 64

        _rewrite(tmp_path, edit)
        index = AttestationIndex.load(tmp_path)
        assert index.first_break_seq == lines[0].seq
        assert index.first_break_prev_seq is None
        assert not any(index.chain_intact_through(line.seq) for line in lines)

    def test_edited_middle_line_and_its_boundary(self, tmp_path: Path) -> None:
        lines = _four_turn_ledger(tmp_path)
        _rewrite(tmp_path, _edit_content(2))
        index = AttestationIndex.load(tmp_path)
        assert index.first_break_seq == lines[3].seq
        assert index.chain_intact_through(lines[1].seq) is True  # N-1: positive control
        assert index.chain_intact_through(lines[2].seq) is False  # N, the edited line
        assert index.chain_intact_through(lines[3].seq) is False  # N+1, the break

    def test_edited_last_turn_with_a_successor_is_not_intact(self, tmp_path: Path) -> None:
        """The last TURN line, followed by another line (as a SessionEnd
        anchor line follows it once athenaeum#2156 lands)."""
        lines = _four_turn_ledger(tmp_path)
        ledger.append_line(
            tmp_path,
            kind="anchor",
            harness="claude-code",
            harness_version="",
            hook_version="test",
            extra={"subject": f"witness:{lines[3].ledger_id}:{lines[3].seq}"},
        )
        _rewrite(tmp_path, _edit_content(3))
        index = AttestationIndex.load(tmp_path)
        assert index.chain_intact_through(lines[3].seq) is False

    def test_edited_true_tail_is_undetectable_by_the_chain_alone(self, tmp_path: Path) -> None:
        """Documented limit, pinned so it cannot change silently: the
        ledger's very last line has no successor, so an edit to it does
        not break the chain. Anchoring is what covers the tail."""
        lines = _four_turn_ledger(tmp_path)
        _rewrite(tmp_path, _edit_content(3))
        index = AttestationIndex.load(tmp_path)
        assert index.chain_ok is True
        assert index.chain_intact_through(lines[3].seq) is True

    def test_deleted_line_marks_its_predecessor_broken(self, tmp_path: Path) -> None:
        """Deleting line N+1 surfaces at N+2. A ``seq + 1 < first_break``
        rule would pass N here; the on-disk predecessor rule does not."""
        lines = _four_turn_ledger(tmp_path)
        _rewrite(tmp_path, lambda objs: objs.pop(2))
        index = AttestationIndex.load(tmp_path)
        assert index.first_break_seq == lines[3].seq
        assert index.first_break_prev_seq == lines[1].seq
        assert index.chain_intact_through(lines[0].seq) is True
        assert index.chain_intact_through(lines[1].seq) is False

    def test_unbroken_ledger_is_intact_everywhere(self, tmp_path: Path) -> None:
        lines = _four_turn_ledger(tmp_path)
        index = AttestationIndex.load(tmp_path)
        assert all(index.chain_intact_through(line.seq) for line in lines)

    def test_attest_misses_when_the_attested_line_itself_was_edited(self, tmp_path: Path) -> None:
        """End to end: edit a non-digest field of the attested line (p1),
        which has a successor. The digest still matches, so before the fix
        this was a hit."""
        _four_turn_ledger(tmp_path)
        clean = attest(AttestationIndex.load(tmp_path), **_good_attest_kwargs())  # type: ignore[arg-type]
        assert clean.hit

        _rewrite(tmp_path, _edit_content(1))
        outcome = attest(AttestationIndex.load(tmp_path), **_good_attest_kwargs())  # type: ignore[arg-type]
        assert not outcome.hit
        assert outcome.miss_reason == "chain-broken"


# ---------------------------------------------------------------------------
# Issue athenaeum#2333 (TG5) — signature-strength verification
# ---------------------------------------------------------------------------


class TestWitnessStrengthVerification:
    def test_unsigned_line_verifies_as_none_with_no_incident(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The common case today (no key provider at all): claimed
        strength is already ``"none"``, so no registry lookup happens and
        no incident is logged."""
        _append_turn(tmp_path)
        index = AttestationIndex.load(tmp_path)
        with caplog.at_level(logging.WARNING, logger="athenaeum.witness_attestation"):
            outcome = attest(index, **_good_attest_kwargs())  # type: ignore[arg-type]
        assert outcome.hit
        assert outcome.witness_strength == "none"
        assert outcome.witness_strength_verified == "none"
        assert outcome.miss_reason is None
        assert caplog.records == []

    def test_out_of_vocabulary_claimed_strength_is_unverifiable(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Defensive branch: a claimed strength that is not even in
        :data:`~athenaeum.config.WITNESS_KEY_STRENGTHS` (corrupt/foreign
        ledger data) is treated as unverifiable, same as "no key" -- never
        an ``IndexError``/``ValueError`` from the strength-ordering
        lookup."""
        provider = _stub_provider("quantum-teleportation")  # not a real tier
        _append_turn(tmp_path, key_provider=provider)
        config = _registry_config(provider, registered_strength="os-keystore")
        index = AttestationIndex.load(tmp_path, config=config)
        with caplog.at_level(logging.WARNING, logger="athenaeum.witness_attestation"):
            outcome = attest(index, **_good_attest_kwargs())  # type: ignore[arg-type]
        assert outcome.hit
        assert outcome.witness_strength_verified == "none"
        assert outcome.miss_reason == "witness-strength-mismatch:quantum-teleportation"

    def test_claimed_nonnone_strength_with_no_signature_is_unverifiable(
        self, tmp_path: Path
    ) -> None:
        """Defensive branch: a line whose ``key.strength`` claims something
        other than ``"none"`` but carries no ``sig`` at all (malformed/
        tampered raw ledger data -- the real writer never produces this
        combination, per :class:`~promptwitness.ledger.KeyInfo`'s own
        docstring) is still treated as unverifiable rather than raising."""
        provider = _stub_provider("os-keystore")
        _append_turn(tmp_path, key_provider=provider)
        partition = _single_partition(tmp_path)
        lines = _read_partition_lines(partition)
        obj = json.loads(lines[0])
        del obj["sig"]  # strength still claims "os-keystore"
        lines[0] = canon.canonical_json(obj)
        partition.write_bytes(b"\n".join(lines) + b"\n")

        config = _registry_config(provider, registered_strength="os-keystore")
        index = AttestationIndex.load(tmp_path, config=config)
        outcome = attest(index, **_good_attest_kwargs())  # type: ignore[arg-type]
        assert outcome.hit
        assert outcome.witness_strength_verified == "none"
        assert outcome.miss_reason == "witness-strength-mismatch:os-keystore"

    def test_unregistered_key_downgrades_to_none_with_incident(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """AC: a line claiming ``os-keystore`` strength, signed by a key
        no operator ever registered, yields verified strength ``"none"``
        and an incident -- while the attestation itself is STILL a hit
        (provenance is unaffected)."""
        provider = _stub_provider("os-keystore")
        _append_turn(tmp_path, key_provider=provider)
        index = AttestationIndex.load(tmp_path)  # no config => empty registry
        with caplog.at_level(logging.WARNING, logger="athenaeum.witness_attestation"):
            outcome = attest(index, **_good_attest_kwargs())  # type: ignore[arg-type]
        assert outcome.hit
        assert outcome.witness_strength == "os-keystore"  # self-asserted field: unchanged
        assert outcome.witness_strength_verified == "none"
        assert outcome.miss_reason == "witness-strength-mismatch:os-keystore"
        records = [r for r in caplog.records if r.name == "athenaeum.witness_attestation"]
        assert len(records) == 1
        assert "witness-strength-mismatch:os-keystore" in records[0].getMessage()

    def test_registered_key_at_or_above_claimed_strength_keeps_the_claim(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """AC: a line whose signature verifies under a registered key
        whose registered strength is AT LEAST the claimed strength keeps
        its strength -- no incident."""
        provider = _stub_provider("os-keystore")
        _append_turn(tmp_path, key_provider=provider)
        config = _registry_config(provider, registered_strength="os-keystore")
        index = AttestationIndex.load(tmp_path, config=config)
        with caplog.at_level(logging.WARNING, logger="athenaeum.witness_attestation"):
            outcome = attest(index, **_good_attest_kwargs())  # type: ignore[arg-type]
        assert outcome.hit
        assert outcome.witness_strength_verified == "os-keystore"
        assert outcome.miss_reason is None
        assert caplog.records == []

    def test_registered_key_strictly_above_claimed_strength_keeps_the_claim(
        self, tmp_path: Path
    ) -> None:
        """"At least" means a registered key can be STRONGER than what a
        line claims, too -- still no mismatch."""
        provider = _stub_provider("software-file")
        _append_turn(tmp_path, key_provider=provider)
        config = _registry_config(provider, registered_strength="hardware-presence")
        index = AttestationIndex.load(tmp_path, config=config)
        outcome = attest(index, **_good_attest_kwargs())  # type: ignore[arg-type]
        assert outcome.hit
        assert outcome.witness_strength_verified == "software-file"
        assert outcome.miss_reason is None

    def test_claimed_strength_above_registered_downgrades_with_incident(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """AC: a claimed strength ABOVE the registered strength yields
        ``"none"`` and an incident, even though the key IS registered and
        the signature itself verifies fine."""
        provider = _stub_provider("hardware-presence")
        _append_turn(tmp_path, key_provider=provider)
        config = _registry_config(provider, registered_strength="os-keystore")
        index = AttestationIndex.load(tmp_path, config=config)
        with caplog.at_level(logging.WARNING, logger="athenaeum.witness_attestation"):
            outcome = attest(index, **_good_attest_kwargs())  # type: ignore[arg-type]
        assert outcome.hit
        assert outcome.witness_strength_verified == "none"
        assert outcome.miss_reason == "witness-strength-mismatch:hardware-presence"
        records = [r for r in caplog.records if r.name == "athenaeum.witness_attestation"]
        assert len(records) == 1

    def test_invalid_signature_under_a_registered_key_downgrades_with_incident(
        self, tmp_path: Path
    ) -> None:
        """A registered key whose signature does NOT verify (tampered
        ``sig``) is treated exactly like an unregistered key -- never as a
        chain break (the hash chain itself is untouched)."""
        provider = _stub_provider("os-keystore")
        _append_turn(tmp_path, key_provider=provider)
        partition = _single_partition(tmp_path)
        lines = _read_partition_lines(partition)
        obj = json.loads(lines[0])
        # Flip the signature to another validly-base64 value -- the chain
        # hash is computed over the signed payload, not `sig` itself, so
        # this does not break the chain.
        obj["sig"] = base64.b64encode(b"\x00" * 64).decode("ascii")
        lines[0] = canon.canonical_json(obj)
        partition.write_bytes(b"\n".join(lines) + b"\n")

        config = _registry_config(provider, registered_strength="os-keystore")
        index = AttestationIndex.load(tmp_path, config=config)
        assert index.chain_ok is True
        outcome = attest(index, **_good_attest_kwargs())  # type: ignore[arg-type]
        assert outcome.hit
        assert outcome.witness_strength_verified == "none"
        assert outcome.miss_reason == "witness-strength-mismatch:os-keystore"


# ---------------------------------------------------------------------------
# Issue athenaeum#2333 (TG5) — anchor source
# ---------------------------------------------------------------------------


def _anchor(tmp_path: Path, line: ledger.WitnessLine, *, source: str, status: str) -> None:
    ledger.append_line(
        tmp_path,
        kind="anchor",
        harness="claude-code",
        harness_version="",
        hook_version="promptwitness 0.1.0",
        extra={
            "subject": f"witness:{line.ledger_id}:{line.seq}",
            "digest": line.line_hash(),
            "op": "anchor",
            "attestation": {
                "source": source,
                "status": status,
                "claimed_time": "2026-01-01T00:00:00Z",
                "proof_path": None,
                "detail": {},
            },
        },
    )


class TestWitnessAnchorSource:
    def test_not_anchored_at_all_is_none(self, tmp_path: Path) -> None:
        _append_turn(tmp_path)
        index = AttestationIndex.load(tmp_path)
        outcome = attest(index, **_good_attest_kwargs())  # type: ignore[arg-type]
        assert outcome.witness_anchored is False
        assert outcome.witness_anchor_source is None

    def test_local_clock_only_anchor_is_local_clock_and_not_anchored_for_consumers(
        self, tmp_path: Path
    ) -> None:
        """AC: a local-clock-only anchor yields ``witness_anchor_source ==
        "local-clock"``, which consumers must treat as NOT anchored (the
        boolean ``witness_anchored`` stays ``True`` for compatibility; the
        NEW field is what tells a consumer not to trust it)."""
        line = _append_turn(tmp_path)
        _anchor(tmp_path, line, source="local-clock", status="complete")
        index = AttestationIndex.load(tmp_path)
        outcome = attest(index, **_good_attest_kwargs())  # type: ignore[arg-type]
        assert outcome.witness_anchored is True
        assert outcome.witness_anchor_source == "local-clock"

    def test_non_local_clock_source_is_preferred_over_local_clock(self, tmp_path: Path) -> None:
        line = _append_turn(tmp_path)
        _anchor(tmp_path, line, source="local-clock", status="complete")
        _anchor(tmp_path, line, source="opentimestamps", status="complete")
        index = AttestationIndex.load(tmp_path)
        outcome = attest(index, **_good_attest_kwargs())  # type: ignore[arg-type]
        assert outcome.witness_anchor_source == "opentimestamps"

    def test_pending_non_local_clock_still_beats_complete_local_clock(
        self, tmp_path: Path
    ) -> None:
        """A non-``local-clock`` source, even PENDING, is still preferred
        over ``local-clock`` -- ``local-clock`` is the weakest source
        regardless of the other source's completion status."""
        line = _append_turn(tmp_path)
        _anchor(tmp_path, line, source="local-clock", status="complete")
        _anchor(tmp_path, line, source="opentimestamps", status="pending")
        index = AttestationIndex.load(tmp_path)
        outcome = attest(index, **_good_attest_kwargs())  # type: ignore[arg-type]
        assert outcome.witness_anchor_source == "opentimestamps"

    def test_complete_source_preferred_over_pending_among_non_local_clock(
        self, tmp_path: Path
    ) -> None:
        line = _append_turn(tmp_path)
        _anchor(tmp_path, line, source="local-clock", status="complete")
        _anchor(tmp_path, line, source="rfc3161:tsa.example", status="pending")
        _anchor(tmp_path, line, source="opentimestamps", status="complete")
        index = AttestationIndex.load(tmp_path)
        outcome = attest(index, **_good_attest_kwargs())  # type: ignore[arg-type]
        assert outcome.witness_anchor_source == "opentimestamps"

    def test_a_later_anchor_pass_still_covers_an_earlier_line(self, tmp_path: Path) -> None:
        """The anchor subject names the LATEST line at anchor time; an
        earlier line (seq 1) is still covered by a later pass (anchoring
        seq 2) -- the same coverage rule :meth:`is_anchored` already uses,
        now shared by `anchor_source`."""
        line1 = _append_turn(tmp_path, prompt_id="p1", turn=0)
        line2 = _append_turn(tmp_path, prompt_id="p2", turn=1)
        _anchor(tmp_path, line2, source="opentimestamps", status="complete")
        index = AttestationIndex.load(tmp_path)
        assert index.is_anchored(line1.ledger_id, line1.seq)
        assert index.anchor_source(line1.ledger_id, line1.seq) == "opentimestamps"

    def test_verify_source_in_detail_overrides_source_where_available(
        self, tmp_path: Path
    ) -> None:
        """The issue's "and the verify_source where available" hedge: a
        FUTURE writer that persists ``detail.verify_source`` wins over the
        plain ``source`` label -- never shipped today, but defensively
        honoured."""
        line = _append_turn(tmp_path)
        ledger.append_line(
            tmp_path,
            kind="anchor",
            harness="claude-code",
            harness_version="",
            hook_version="promptwitness 0.1.0",
            extra={
                "subject": f"witness:{line.ledger_id}:{line.seq}",
                "digest": line.line_hash(),
                "op": "anchor",
                "attestation": {
                    "source": "opentimestamps",
                    "status": "complete",
                    "claimed_time": "2026-01-01T00:00:00Z",
                    "proof_path": None,
                    "detail": {"verify_source": "block-explorer:example.com"},
                },
            },
        )
        index = AttestationIndex.load(tmp_path)
        outcome = attest(index, **_good_attest_kwargs())  # type: ignore[arg-type]
        assert outcome.witness_anchor_source == "block-explorer:example.com"

    def test_malformed_anchor_line_falls_back_to_local_clock(self, tmp_path: Path) -> None:
        line = _append_turn(tmp_path)
        ledger.append_line(
            tmp_path,
            kind="anchor",
            harness="claude-code",
            harness_version="",
            hook_version="promptwitness 0.1.0",
            extra={"subject": f"witness:{line.ledger_id}:{line.seq}"},  # no "attestation" at all
        )
        index = AttestationIndex.load(tmp_path)
        outcome = attest(index, **_good_attest_kwargs())  # type: ignore[arg-type]
        assert outcome.witness_anchored is True
        assert outcome.witness_anchor_source == "local-clock"
