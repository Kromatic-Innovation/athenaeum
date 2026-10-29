# SPDX-License-Identifier: Apache-2.0
"""``athenaeum receipts attest-readout`` and :mod:`athenaeum.witness_readout`
(issue athenaeum#2160, epic athenaeum#2144).

What is under test, acceptance criterion by acceptance criterion:

* :class:`TestEveryMissReason` — a fixture receipt ledger carrying ONE
  example of each of the eleven
  :data:`athenaeum.witness_attestation.MISS_REASONS` reasons produces the
  exact expected counts in BOTH renderings, and lists every non-``flag-off``
  miss as an incident (AC1).
* :class:`TestExitCode` — clean -> ``0``; each of the three incident sources
  SINGLY -> ``INCIDENT_EXIT`` (AC2).
* :class:`TestNoPlaintext` — no 64-hex run in either rendering, even when
  every receipt carries a real digest and the witness ledger carries real
  line hashes (AC3).
* :class:`TestLikelyFalseNegative` — the ``transcript_origin == "human"`` +
  ``transcript_entrypoint == "cli"`` flag, pinned both ways (AC4).
* :class:`TestNightly` — the librarian end-of-run pass emits ONE WARNING per
  incident class, with its count, and never one per incident (AC5).
* :class:`TestWitnessSide` — unsigned lines, anchor coverage, the
  ``session-close`` deficit, the tolerant hook-error read, and ``--since``.

No network, no key provider, no transcript: the readout's whole contract is
that it reads the receipt ledger and the witness index and nothing else, so
every fixture here is built from those two writers' own public APIs.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import pytest

from athenaeum import witness_readout
from athenaeum.transcript_receipts import ReceiptEntry, append_receipt
from athenaeum.witness_attestation import MISS_REASONS
from athenaeum.witness_readout import (
    INCIDENT_HOOK_ERROR,
    INCIDENT_PROVENANCE_MISS,
    INCIDENT_SESSION_CLOSE_DEFICIT,
    build_readout,
    render_text,
)

MONTH = "2026-10"

#: A 64-hex run — the shape AC3 forbids anywhere in either rendering. Word
#: boundaries are deliberately NOT used: a digest embedded in a longer token
#: would still be a leaked digest.
_HEX64 = re.compile(r"[0-9a-f]{64}", re.IGNORECASE)

#: The eleven reasons, three of them with a `:`-suffixed value attached the
#: way `witness_attestation.attest` attaches one, so the bucketing rule is
#: exercised rather than assumed.
EVERY_REASON: tuple[str, ...] = (
    "ledger-absent",
    "no-entry",
    "kind-not-human:agent",
    "channel-not-interactive:non-interactive",
    "digest-mismatch",
    "session-mismatch",
    "chain-broken",
    "transcript-origin-not-human",
    "transcript-promptsource:absent",
    "sidechain",
    "flag-off",
)


def _librarian_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Only this module's own records — a readout builds a witness index,
    and an unrelated library warning must not read as a readout warning."""
    return [r for r in caplog.records if r.name == "athenaeum.librarian"]


def _librarian_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in _librarian_records(caplog) if r.levelno == logging.WARNING]


class _HeldLock:
    """A stand-in :class:`athenaeum.runlock.RunLock` that reports held.

    ``append_receipt`` refuses to write without one; nothing in this file
    exercises locking itself.
    """

    acquired = True

    def is_held(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _receipt(
    wiki: Path,
    *,
    digest: str,
    channel: str = "user-stated",
    witness_miss: str | None = None,
    witness_tag: str | None = None,
    witness_ref: str | None = None,
    transcript_origin: str | None = None,
    transcript_entrypoint: str | None = None,
    session: str = "11111111-2222-3333-4444-555555555555",
    month: str = MONTH,
) -> ReceiptEntry:
    entry = ReceiptEntry(
        origin_scope="raw/auto-memory",
        memory_digest=digest,
        channel=channel,
        origin_session_id=session,
        origin_turn=1,
        utterance_digest=digest,
        transcript_prefix_digest=digest,
        transcript_line_count=3,
        recorded_at=f"{month}-09T00:00:00Z",
        witness_tag=witness_tag,
        witness_ref=witness_ref,
        witness_miss=witness_miss,
        transcript_origin=transcript_origin,
        transcript_entrypoint=transcript_entrypoint,
    )
    append_receipt(wiki, entry, lock=_HeldLock())
    return entry


def _digest(n: int) -> str:
    """A realistic 64-hex memory digest — the readout must never echo it."""
    return f"{n:064x}"


def _witness_turn(ledger_dir: Path, *, seq_hint: int, sig: bool = False) -> Any:
    """One real ``turn`` witness line, unsigned unless *sig* is set.

    Unsigned is the realistic default: the hook writes with
    ``key_provider: none`` unless an operator configured a signing key, and
    the unsigned-line COUNT is one of the numbers this readout reports.
    """
    from promptwitness import ledger as pw_ledger

    provider = _FakeKeyProvider() if sig else None
    return pw_ledger.append_line(
        ledger_dir,
        kind="turn",
        harness="claude-code",
        harness_version="",
        hook_version="promptwitness test",
        ts=f"{MONTH}-09T00:00:0{seq_hint % 10}.000Z",
        key_provider=provider,
        session="11111111-2222-3333-4444-555555555555",
        turn=seq_hint,
        prompt_id=f"p{seq_hint}",
        prompt_kind="human",
        prompt_digest="sha256:" + _digest(seq_hint),
        prompt_chars=12,
        channel="interactive",
        signals={"entrypoint": "cli"},
    )


class _FakeKeyProvider:
    """Minimal :class:`promptwitness.ledger.KeyProviderLike` — enough for a
    line to come out with a non-``None`` ``sig``, which is the only property
    the unsigned-line count cares about."""

    def key_id(self) -> str:
        return "test-key"

    def algorithm(self) -> str:
        return "test-alg"

    def strength(self) -> str:
        return "software"

    def presence_required(self) -> bool:
        return False

    def sign(self, payload: bytes) -> bytes:
        del payload
        return b"signature-bytes"


def _witness_session_close(
    ledger_dir: Path, *, session: str, turns: int, turns_witnessed: int
) -> None:
    from promptwitness import ledger as pw_ledger

    pw_ledger.append_line(
        ledger_dir,
        kind="session-close",
        harness="claude-code",
        harness_version="",
        hook_version="promptwitness test",
        ts=f"{MONTH}-09T23:00:00.000Z",
        session=session,
        turns=turns,
        last_seq=turns,
        turns_witnessed=turns_witnessed,
    )


def _hook_errors(cache_dir: Path, *names: str, torn: bool = False) -> None:
    """Write ``witness_hook_errors.jsonl`` the way
    ``claude_code_adapter._log_witness_error`` writes it. *torn* appends an
    unterminated partial final line, which the readout must survive."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / witness_readout.HOOK_ERROR_FILENAME
    with path.open("a", encoding="utf-8") as fh:
        for name in names:
            fh.write(json.dumps({"ts": f"{MONTH}-09T00:00:00+00:00", "error": name}) + "\n")
        if torn:
            fh.write('{"ts": "2026-10-09T00:00:00+00:00", "err')


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """``{knowledge, wiki, witness, cache}``, every path under ``tmp_path``.

    Both env overrides are set so neither the witness ledger nor the cache
    dir can reach a real machine path — and so no absolute home-directory
    literal is ever written into this file (``public-safe-lint``).
    """
    knowledge = tmp_path / "knowledge"
    wiki = knowledge / "wiki"
    witness = tmp_path / "witness"
    cache = tmp_path / "cache"
    monkeypatch.setenv("PROMPTWITNESS_DIR", str(witness))
    monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(cache))
    return {"knowledge": knowledge, "wiki": wiki, "witness": witness, "cache": cache}


# ---------------------------------------------------------------------------
# AC1 — every miss reason, both renderings
# ---------------------------------------------------------------------------


class TestEveryMissReason:
    def _ledger(self, world: dict[str, Path]) -> None:
        for n, reason in enumerate(EVERY_REASON):
            _receipt(world["wiki"], digest=_digest(n), witness_miss=reason)
        # Two hits, one per witness_tag, so the tag split is non-trivial.
        _receipt(
            world["wiki"],
            digest=_digest(90),
            channel="witnessed",
            witness_tag="typed",
            witness_ref="pw-0123456789abcdef:4",
        )
        _receipt(
            world["wiki"],
            digest=_digest(91),
            channel="witnessed",
            witness_tag="slash-args",
            witness_ref="pw-0123456789abcdef:5",
        )

    def test_every_reason_in_miss_reasons(self) -> None:
        """The fixture's reason list IS the shipped vocabulary — bucketed,
        so a `:`-suffixed reason is checked against its prefix."""
        buckets = {r.split(":", 1)[0] for r in EVERY_REASON}
        assert buckets == set(MISS_REASONS)

    def test_counts_are_exact(self, world: dict[str, Path]) -> None:
        self._ledger(world)
        summary = build_readout(
            world["wiki"], witness_dir=world["witness"], cache_dir=world["cache"]
        )

        assert summary.user_stated_total == 11
        assert summary.witnessed_total == 2
        assert summary.witnessed_by_tag == {"slash-args": 1, "typed": 1}
        assert summary.miss_counts == {bucket: 1 for bucket in MISS_REASONS}
        assert sum(summary.miss_counts.values()) == 11

        # Ten incidents: every reason except `flag-off`.
        provenance = [i for i in summary.incidents if i.incident_class == INCIDENT_PROVENANCE_MISS]
        assert len(provenance) == 10
        assert {i.reason for i in provenance} == set(EVERY_REASON) - {"flag-off"}
        assert "flag-off" not in {i.reason for i in summary.incidents}

    def test_json_and_text_carry_the_same_numbers(self, world: dict[str, Path]) -> None:
        self._ledger(world)
        summary = build_readout(
            world["wiki"], witness_dir=world["witness"], cache_dir=world["cache"]
        )
        payload = summary.to_dict()
        text = render_text(summary)

        assert payload["user_stated_total"] == 11
        assert payload["witnessed_total"] == 2
        assert payload["witnessed_by_tag"] == {"slash-args": 1, "typed": 1}
        assert payload["miss_counts"] == {bucket: 1 for bucket in MISS_REASONS}
        assert len(payload["incidents"]) == 10
        assert payload["incident_class_counts"] == {INCIDENT_PROVENANCE_MISS: 10}
        assert payload["ok"] is False

        assert "user-stated:      11" in text
        assert "witnessed:        2" in text
        assert "misses:           11" in text
        assert "incidents: 10" in text
        for reason in EVERY_REASON:
            if reason == "flag-off":
                continue
            assert f"reason={reason}" in text
        # And every bucket's own count line is present in the breakdown.
        for bucket in MISS_REASONS:
            assert f"    {bucket}: 1" in text

    def test_cli_emits_both_renderings(
        self, world: dict[str, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        from athenaeum._cmd_receipts import INCIDENT_EXIT
        from athenaeum.cli import main

        self._ledger(world)
        rc = main(["receipts", "attest-readout", "--path", str(world["knowledge"]), "--json"])
        assert rc == INCIDENT_EXIT
        payload = json.loads(capsys.readouterr().out)
        assert payload["user_stated_total"] == 11
        assert len(payload["incidents"]) == 10

        rc = main(["receipts", "attest-readout", "--path", str(world["knowledge"])])
        assert rc == INCIDENT_EXIT
        text = capsys.readouterr().out
        assert "user-stated:      11" in text
        assert "incidents: 10" in text


# ---------------------------------------------------------------------------
# AC2 — exit code, each incident source singly
# ---------------------------------------------------------------------------


class TestExitCode:
    def _run(self, world: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> int:
        from athenaeum.cli import main

        rc = main(["receipts", "attest-readout", "--path", str(world["knowledge"])])
        capsys.readouterr()
        return rc

    def test_clean_exits_zero(
        self, world: dict[str, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        _receipt(
            world["wiki"],
            digest=_digest(1),
            channel="witnessed",
            witness_tag="typed",
            witness_ref="pw-0123456789abcdef:1",
        )
        _receipt(world["wiki"], digest=_digest(2), witness_miss="flag-off")
        _witness_session_close(
            world["witness"], session="s-clean", turns=4, turns_witnessed=4
        )
        assert self._run(world, capsys) == 0

    def test_a_single_miss_exits_the_incident_code(
        self, world: dict[str, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        from athenaeum._cmd_receipts import INCIDENT_EXIT

        _receipt(world["wiki"], digest=_digest(1), witness_miss="no-entry")
        assert self._run(world, capsys) == INCIDENT_EXIT

    def test_a_single_hook_error_exits_the_incident_code(
        self, world: dict[str, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        from athenaeum._cmd_receipts import INCIDENT_EXIT

        _receipt(world["wiki"], digest=_digest(1), witness_miss="flag-off")
        _hook_errors(world["cache"], "OSError")
        assert self._run(world, capsys) == INCIDENT_EXIT

    def test_a_single_deficit_exits_the_incident_code(
        self, world: dict[str, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        from athenaeum._cmd_receipts import INCIDENT_EXIT

        _receipt(world["wiki"], digest=_digest(1), witness_miss="flag-off")
        _witness_session_close(
            world["witness"], session="s-short", turns=5, turns_witnessed=3
        )
        assert self._run(world, capsys) == INCIDENT_EXIT

    def test_the_incident_code_is_distinct_from_the_other_receipts_codes(self) -> None:
        """A nightly wrapper has to tell "provenance needs a human" from
        usage, a seal problem and a missing key."""
        from athenaeum._cmd_receipts import (
            INCIDENT_EXIT,
            KEY_UNAVAILABLE_EXIT,
            VERIFY_PROBLEM_EXIT,
        )

        assert INCIDENT_EXIT not in (0, 1, 2, VERIFY_PROBLEM_EXIT, KEY_UNAVAILABLE_EXIT)


# ---------------------------------------------------------------------------
# AC3 — no digests, no plaintext
# ---------------------------------------------------------------------------


class TestNoPlaintext:
    def test_neither_rendering_contains_a_64_hex_run(self, world: dict[str, Path]) -> None:
        for n, reason in enumerate(EVERY_REASON):
            _receipt(world["wiki"], digest=_digest(n), witness_miss=reason)
        _receipt(
            world["wiki"],
            digest=_digest(50),
            channel="witnessed",
            witness_tag="typed",
            witness_ref="pw-0123456789abcdef:2",
        )
        _witness_turn(world["witness"], seq_hint=1)
        _witness_turn(world["witness"], seq_hint=2, sig=True)
        _witness_session_close(
            world["witness"], session="s-deficit", turns=9, turns_witnessed=2
        )
        _hook_errors(world["cache"], "RuntimeError", "OSError")

        summary = build_readout(
            world["wiki"], witness_dir=world["witness"], cache_dir=world["cache"]
        )
        text = render_text(summary)
        payload = json.dumps(summary.to_dict())

        # Positive control: the fixture really does carry 64-hex digests, so
        # a clean assert below is evidence of filtering, not of empty input.
        assert _HEX64.search(_digest(3))
        assert summary.user_stated_total == 11
        assert not _HEX64.search(text), _HEX64.search(text)
        assert not _HEX64.search(payload), _HEX64.search(payload)

    def test_no_claim_text_field_exists_on_the_summary(self, world: dict[str, Path]) -> None:
        """Structural: the JSON shape has no text-bearing key at all, so a
        future field cannot leak plaintext without failing this test."""
        _receipt(world["wiki"], digest=_digest(1), witness_miss="no-entry")
        payload = build_readout(
            world["wiki"], witness_dir=world["witness"], cache_dir=world["cache"]
        ).to_dict()
        keys = set(payload) | {k for i in payload["incidents"] for k in i}
        assert not keys & {"claim", "text", "utterance", "prompt", "digest", "memory_digest"}


# ---------------------------------------------------------------------------
# AC4 — the likely-false-negative flag
# ---------------------------------------------------------------------------


class TestLikelyFalseNegative:
    def test_human_origin_and_cli_entrypoint_is_flagged(self, world: dict[str, Path]) -> None:
        _receipt(
            world["wiki"],
            digest=_digest(1),
            witness_miss="digest-mismatch",
            witness_ref="pw-0123456789abcdef:7",
            transcript_origin="human",
            transcript_entrypoint="cli",
        )
        summary = build_readout(
            world["wiki"], witness_dir=world["witness"], cache_dir=world["cache"]
        )
        (incident,) = summary.incidents
        assert incident.likely_false_negative is True
        assert incident.reason == "digest-mismatch"
        assert incident.witness_ref == "pw-0123456789abcdef:7"
        assert incident.session == "11111111-2222-3333-4444-555555555555"
        assert "LIKELY-FALSE-NEGATIVE" in render_text(summary)
        assert summary.to_dict()["incidents"][0]["likely_false_negative"] is True

    @pytest.mark.parametrize(
        ("origin", "entrypoint"),
        [
            ("agent", "cli"),
            ("human", "sdk"),
            (None, "cli"),
            ("human", None),
            (None, None),
        ],
    )
    def test_anything_else_is_not_flagged(
        self, world: dict[str, Path], origin: str | None, entrypoint: str | None
    ) -> None:
        """Pinned in the negative direction too: the flag is an AND over two
        fields, and a receipt written before schema v2 carries neither."""
        _receipt(
            world["wiki"],
            digest=_digest(2),
            witness_miss="digest-mismatch",
            transcript_origin=origin,
            transcript_entrypoint=entrypoint,
        )
        summary = build_readout(
            world["wiki"], witness_dir=world["witness"], cache_dir=world["cache"]
        )
        (incident,) = summary.incidents
        assert incident.likely_false_negative is False
        assert "LIKELY-FALSE-NEGATIVE" not in render_text(summary)

    def test_the_flag_needs_no_source_beyond_the_receipt(self) -> None:
        """Both fields are persisted on the receipt itself (schema v2), so
        the readout reads them from a permitted source with no derivation
        and no third data source."""
        assert "transcript_origin" in ReceiptEntry.__dataclass_fields__
        assert "transcript_entrypoint" in ReceiptEntry.__dataclass_fields__
        probe = ReceiptEntry.from_dict(
            {
                "origin_scope": "raw/auto-memory",
                "memory_digest": _digest(3),
                "channel": "user-stated",
                "transcript_origin": "human",
                "transcript_entrypoint": "cli",
            }
        )
        assert (probe.transcript_origin, probe.transcript_entrypoint) == ("human", "cli")


# ---------------------------------------------------------------------------
# AC5 — the nightly's one WARNING per incident class
# ---------------------------------------------------------------------------


class TestNightly:
    def test_one_warning_per_incident_class_with_its_count(
        self,
        world: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from athenaeum.librarian import _maybe_run_nightly_witness_readout

        # Five provenance misses, two hook-error classes, one deficit: eight
        # incidents in three classes. One WARNING per CLASS is the AC.
        for n in range(5):
            _receipt(world["wiki"], digest=_digest(n), witness_miss="no-entry")
        _hook_errors(world["cache"], "OSError", "OSError", "RuntimeError")
        _witness_session_close(
            world["witness"], session="s-short", turns=6, turns_witnessed=2
        )

        monkeypatch.setenv("ATHENAEUM_WITNESS_ATTESTATION_ENABLED", "1")
        ctx = _FakeCtx(world["wiki"])
        with caplog.at_level(logging.INFO, logger="athenaeum.librarian"):
            _maybe_run_nightly_witness_readout(ctx)

        warnings = _librarian_warnings(caplog)
        assert len(warnings) == 3, [r.getMessage() for r in warnings]
        messages = sorted(r.getMessage() for r in warnings)
        assert any(f"5 {INCIDENT_PROVENANCE_MISS} incident(s)" in m for m in messages)
        assert any(f"3 {INCIDENT_HOOK_ERROR} incident(s)" in m for m in messages)
        assert any(f"4 {INCIDENT_SESSION_CLOSE_DEFICIT} incident(s)" in m for m in messages)

    def test_a_clean_night_emits_no_warning(
        self,
        world: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from athenaeum.librarian import _maybe_run_nightly_witness_readout

        _receipt(world["wiki"], digest=_digest(1), witness_miss="flag-off")
        monkeypatch.setenv("ATHENAEUM_WITNESS_ATTESTATION_ENABLED", "1")
        with caplog.at_level(logging.INFO, logger="athenaeum.librarian"):
            _maybe_run_nightly_witness_readout(_FakeCtx(world["wiki"]))
        assert _librarian_warnings(caplog) == []

    def test_gated_off_by_default_and_by_dry_run(
        self,
        world: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from athenaeum.librarian import _maybe_run_nightly_witness_readout

        _receipt(world["wiki"], digest=_digest(1), witness_miss="no-entry")
        monkeypatch.delenv("ATHENAEUM_WITNESS_ATTESTATION_ENABLED", raising=False)
        with caplog.at_level(logging.INFO, logger="athenaeum.librarian"):
            _maybe_run_nightly_witness_readout(_FakeCtx(world["wiki"]))
        assert _librarian_records(caplog) == []

        monkeypatch.setenv("ATHENAEUM_WITNESS_ATTESTATION_ENABLED", "1")
        with caplog.at_level(logging.INFO, logger="athenaeum.librarian"):
            _maybe_run_nightly_witness_readout(_FakeCtx(world["wiki"], dry_run=True))
        assert _librarian_records(caplog) == []

    def test_a_failure_is_non_fatal(
        self,
        world: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A readout must never change a run's exit code."""
        from athenaeum import librarian

        def _boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("readout exploded")

        monkeypatch.setenv("ATHENAEUM_WITNESS_ATTESTATION_ENABLED", "1")
        monkeypatch.setattr(witness_readout, "build_readout", _boom)
        with caplog.at_level(logging.INFO, logger="athenaeum.librarian"):
            librarian._maybe_run_nightly_witness_readout(_FakeCtx(world["wiki"]))
        assert any("witness readout failed (non-fatal)" in r.getMessage() for r in caplog.records)

    def test_the_nightly_calls_the_same_summary_the_cli_calls(self) -> None:
        """Not two implementations: both call ``build_readout``."""
        from athenaeum import _cmd_receipts, librarian

        assert "build_readout" in librarian._maybe_run_nightly_witness_readout.__code__.co_names
        assert "build_readout" in _cmd_receipts._cmd_attest_readout.__code__.co_names


class _FakeCtx:
    """The three ``RunContext`` attributes the readout pass reads.

    Deliberately not a real ``RunContext``: the pass takes no lock and
    touches no pipeline state, and a fake makes that structurally visible.
    """

    def __init__(self, wiki_root: Path, *, dry_run: bool = False) -> None:
        self.wiki_root = wiki_root
        self.dry_run = dry_run
        self.config: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# The witness-index half, the hook-error file, and --since
# ---------------------------------------------------------------------------


class TestWitnessSide:
    def test_unsigned_lines_and_anchor_coverage(self, world: dict[str, Path]) -> None:
        from athenaeum.witness_attestation import AttestationIndex

        _witness_turn(world["witness"], seq_hint=1)
        signed = _witness_turn(world["witness"], seq_hint=2, sig=True)
        _witness_turn(world["witness"], seq_hint=3)

        summary = build_readout(
            world["wiki"], witness_dir=world["witness"], cache_dir=world["cache"]
        )
        assert summary.witness_lines_total == 3
        assert summary.unsigned_lines == 2
        # Nothing anchored yet.
        assert summary.anchored_lines == 0
        assert summary.anchor_coverage == 0.0
        assert summary.anchored_through == {}

        _append_anchor(world["witness"], ledger_id=signed.ledger_id, seq=signed.seq)
        summary = build_readout(
            world["wiki"], witness_dir=world["witness"], cache_dir=world["cache"]
        )
        # The anchor line itself is also a witness line and is itself covered.
        assert summary.anchored_through == {signed.ledger_id: signed.seq}
        assert summary.anchored_lines == signed.seq
        assert summary.anchor_coverage == pytest.approx(signed.seq / summary.witness_lines_total)
        # And the narrow public accessor returns a COPY, not the live map.
        index = AttestationIndex.load(world["witness"])
        got = index.anchored_through()
        got["injected"] = 99
        assert "injected" not in index.anchored_through()

    def test_no_witness_lines_reports_no_coverage_rather_than_zero(
        self, world: dict[str, Path]
    ) -> None:
        summary = build_readout(
            world["wiki"], witness_dir=world["witness"], cache_dir=world["cache"]
        )
        assert summary.witness_lines_total == 0
        assert summary.anchor_coverage is None
        assert "n/a (no witness lines)" in render_text(summary)
        assert summary.ok is True

    def test_session_close_deficit_sums_per_session(self, world: dict[str, Path]) -> None:
        _witness_session_close(world["witness"], session="s-a", turns=5, turns_witnessed=3)
        _witness_session_close(world["witness"], session="s-a", turns=4, turns_witnessed=1)
        _witness_session_close(world["witness"], session="s-b", turns=2, turns_witnessed=2)
        summary = build_readout(
            world["wiki"], witness_dir=world["witness"], cache_dir=world["cache"]
        )
        assert summary.session_close_deficits == {"s-a": 5}
        assert summary.session_close_deficit == 5
        deficits = [
            i for i in summary.incidents if i.incident_class == INCIDENT_SESSION_CLOSE_DEFICIT
        ]
        assert len(deficits) == 1
        assert (deficits[0].session, deficits[0].count) == ("s-a", 5)

    def test_hook_errors_bucket_by_class_and_survive_a_torn_line(
        self, world: dict[str, Path]
    ) -> None:
        _hook_errors(world["cache"], "OSError", "OSError", "RuntimeError", torn=True)
        summary = build_readout(
            world["wiki"], witness_dir=world["witness"], cache_dir=world["cache"]
        )
        assert summary.hook_error_counts == {"OSError": 2, "RuntimeError": 1}
        hook = [i for i in summary.incidents if i.incident_class == INCIDENT_HOOK_ERROR]
        assert [(i.reason, i.count) for i in hook] == [("OSError", 2), ("RuntimeError", 1)]

    def test_hook_error_file_absent_is_not_an_incident(self, world: dict[str, Path]) -> None:
        summary = build_readout(
            world["wiki"], witness_dir=world["witness"], cache_dir=world["cache"]
        )
        assert summary.hook_error_counts == {}
        assert summary.ok is True

    def test_hook_error_cache_dir_defaults_to_the_resolved_cache_dir(
        self, world: dict[str, Path]
    ) -> None:
        """No hardcoded path: with ``cache_dir`` unset the readout resolves
        the same dir ``claude_code_adapter`` writes to."""
        _hook_errors(world["cache"], "OSError")
        summary = build_readout(world["wiki"], witness_dir=world["witness"])
        assert summary.hook_error_counts == {"OSError": 1}

    def test_since_filters_receipts_witness_lines_and_hook_errors(
        self, world: dict[str, Path]
    ) -> None:
        _receipt(world["wiki"], digest=_digest(1), witness_miss="no-entry", month="2026-08")
        _receipt(world["wiki"], digest=_digest(2), witness_miss="no-entry", month=MONTH)
        _hook_errors(world["cache"], "OSError")
        _witness_session_close(world["witness"], session="s-a", turns=3, turns_witnessed=1)

        every = build_readout(
            world["wiki"], witness_dir=world["witness"], cache_dir=world["cache"]
        )
        assert every.user_stated_total == 2
        assert every.session_close_deficit == 2
        assert every.hook_error_counts == {"OSError": 1}

        narrowed = build_readout(
            world["wiki"],
            witness_dir=world["witness"],
            cache_dir=world["cache"],
            since="2026-10",
        )
        assert narrowed.user_stated_total == 1
        assert narrowed.since == "2026-10"
        assert narrowed.to_dict()["since"] == "2026-10"

        future = build_readout(
            world["wiki"],
            witness_dir=world["witness"],
            cache_dir=world["cache"],
            since="2026-12",
        )
        assert future.user_stated_total == 0
        assert future.session_close_deficit == 0
        assert future.hook_error_counts == {}
        assert future.ok is True

    @pytest.mark.parametrize("bad", ["2026", "2026-1", "2026-13", "not-a-month", "202X-01"])
    def test_a_malformed_since_is_a_usage_error(
        self, world: dict[str, Path], bad: str
    ) -> None:
        from athenaeum.cli import main

        with pytest.raises(SystemExit) as excinfo:
            main(
                [
                    "receipts",
                    "attest-readout",
                    "--path",
                    str(world["knowledge"]),
                    "--since",
                    bad,
                ]
            )
        assert excinfo.value.code == 2

    def test_there_is_no_threshold_or_window_knob(self) -> None:
        """Operator ruling 10 removed a tolerated miss rate and a readout
        window from the design; this pins their absence."""
        import inspect

        params = set(inspect.signature(build_readout).parameters)
        assert params == {"wiki_root", "witness_dir", "cache_dir", "since"}
        fields = set(witness_readout.ReadoutSummary.__dataclass_fields__)
        assert not {f for f in fields if "threshold" in f or "rate" in f or "window" in f}


def _append_anchor(ledger_dir: Path, *, ledger_id: str, seq: int) -> None:
    """One ``anchor`` witness line covering ``witness:<ledger_id>:<seq>`` —
    the subject shape ``promptwitness.cli``'s ``anchor`` writes and
    ``AttestationIndex.load`` scans for."""
    from promptwitness import ledger as pw_ledger

    pw_ledger.append_line(
        ledger_dir,
        kind="anchor",
        harness="claude-code",
        harness_version="",
        hook_version="promptwitness test",
        ts=f"{MONTH}-09T12:00:00.000Z",
        extra={
            "subject": f"witness:{ledger_id}:{seq}",
            "digest": "sha256:" + _digest(7),
            "op": "anchor",
            "attestation": {
                "source": "local-clock",
                "status": "complete",
                "claimed_time": f"{MONTH}-09T12:00:00.000Z",
                "proof_path": None,
                "detail": {"from": "test"},
            },
        },
    )
