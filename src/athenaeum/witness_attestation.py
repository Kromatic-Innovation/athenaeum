# SPDX-License-Identifier: Apache-2.0
"""Attest a ``user-stated`` transcript utterance against the witness ledger
(issue athenaeum#2158, epic athenaeum#2144).

A ``user-stated`` classification (:func:`athenaeum.transcript_verify.
classify_backfill_records`) has no witness outside the session transcript,
which the model's own Bash tool can edit. This module cross-checks such a
claim against the hash-chained ``promptwitness`` ledger (``promptwitness.
ledger``, written by the ``UserPromptSubmit`` hook at a layer the Bash tool
does not normally touch — see ``docs/design/witness-ledger.md``'s security
boundary, reproduced in ``docs/design/conflict-resolution.md`` §14) and
upgrades the receipt's channel to ``witnessed`` on a hit.

**This does not prove a human typed the prompt.** It proves only that a
witness-ledger line exists whose digest, channel, chain position and
transcript cross-fields all agree with the claimed transcript record — see
the module-level "Known limits" note in ``docs/design/conflict-resolution.md``
§14 for exactly what each signal does and does not establish. A miss is an
INCIDENT (operator ruling 10), not a tolerated rate: every miss other than
``flag-off`` is logged at WARNING, and a miss that otherwise looks
human-and-interactive (``transcript_origin == "human"`` and
``transcript_entrypoint == "cli"``) is logged as a provenance incident.

**Hit rule (AND over every condition — both sides must agree):**

1. a witness line is found (by ``prompt_id``, falling back to
   ``(origin_session_id, utterance_digest)``);
2. ``prompt_kind == "human"``, or ``"slash-command"`` with a recorded
   ``slash-args`` segment, and the line carries no ``signals.agent_id``;
3. ``channel == "interactive"``;
4. the line's ``session`` matches the claim's origin session;
5. digest equality — ``human``: ``prompt_digest == "sha256:" +
   utterance_digest`` (normalized, matching :func:`promptwitness.harness.
   claude_code.record_turn`'s own ``prompt_digest`` formula); ``slash-command``:
   the ``slash-args`` segment's digest equals the digest
   :func:`promptwitness.harness.claude_code.slash_args_segment` computes
   when re-run on the TRANSCRIPT record's own text — called directly, never
   re-derived (see "Known deviation" below) — and the claim lies inside
   that span (so a claim that only appears in expanded skill boilerplate
   outside ``<command-args>`` does not attest);
6. the hash chain is intact through that line's ``seq``: no break was
   found, or the line sits before the first break AND is not the line
   physically preceding it (:attr:`promptwitness.ledger.ChainVerification.
   first_break_prev_seq`). A content edit to line N surfaces as a break at
   N+1, so N itself must count as broken; the predecessor is taken from
   on-disk order rather than ``seq + 1`` so a deleted line's gap is handled
   too. Same rule as :func:`promptwitness.verify.verify_transcript`. Known
   limit: an edit to the ledger's very LAST line has no successor to
   expose it, so the chain alone reports it intact; tail tamper-evidence
   is what anchoring provides (``witness_anchored`` on the outcome);
7. the transcript record's ``origin.kind`` is ``"human"`` when present;
8. the transcript record's ``promptSource`` is ``"typed"`` or ``"queued"``
   (absent fails);
9. the transcript record's ``isSidechain`` is not ``True``.

Each failing condition reports exactly one of the eleven
:data:`MISS_REASONS` values — never more than one per call, so a reason is
always individually attributable to the single condition that produced it.

Layering: L2. Imports :mod:`promptwitness` (``ledger``, ``canon``,
``harness.claude_code``) and :mod:`athenaeum.config`, nothing heavier — no
``athenaeum.transcript_verify``, no ``athenaeum.models``. Callers hand this
module plain strings/primitives already extracted from a transcript record
(never a raw record ``dict``), which is what keeps that boundary real
rather than aspirational.

**Reversible default (recorded for the lane report).** ``transcript_
entrypoint`` has no defined source in the issue text beyond "a label fed to
the readout". This module derives it from the WITNESS line's own
``signals.entrypoint`` (the same ``CLAUDE_CODE_ENTRYPOINT`` signal
``promptwitness.harness.claude_code.detect_channel`` already records) when
a witness line was found, and ``None`` otherwise — never from the
transcript record, which carries no such field. If a future issue defines
a different source, this is the one place to change.

**Known deviation from the issue's literal wording (digest contract defect
— flagged to the epic owner, not a judgment call this module gets to make
silently).** The issue specifies the slash-command digest check as
``sha256(_normalize(<command-args> text))`` — NORMALIZED. The SHIPPED
producer, ``promptwitness.harness.claude_code.slash_args_segment``
(athenaeum#2149, pinned by
``tests/test_promptwitness_harness_claude_code.py::
TestSlashArgsSegment::test_command_args_present_yields_one_segment_with_its_own_digest``),
computes ``canon.digest(content)`` over the RAW, un-normalized
``<command-args>`` span — unlike ``record_turn``'s own ``prompt_digest``
field, which IS normalized. Implementing the issue's literal formula would
make the slash-args hit path unreachable for almost every real slash
command (it would only fire when the args text happened to already be
lowercase, single-spaced, with no leading/trailing whitespace). This
module therefore compares against ``slash_args_segment``'s digest directly
— calling the real producer function on the transcript record's text,
never re-deriving its formula as a second copy — which is the strict
direction (it can only REJECT a line the normalized form would have
admitted, never admit one the normalized form would reject). Widening this
to accept EITHER formula is explicitly not an option: this is a provenance
check, and tolerating two formulas is the permissive direction. See
``CHANGELOG.md``'s entry for this issue for the same note, and the lane
report for the written-up defect this traces to.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from re import compile as _compile

from promptwitness import canon, ledger
from promptwitness.harness import claude_code

log = logging.getLogger(__name__)

__all__ = [
    "MISS_REASONS",
    "DEFAULT_HARNESS",
    "AttestationOutcome",
    "AttestationIndex",
    "attest",
    "resolve_default_witness_dir",
]

#: The exact eleven-value miss vocabulary (issue athenaeum#2158). A
#: ``kind-not-human:<kind>``, ``channel-not-interactive:<ch>`` or
#: ``transcript-promptsource:<value>`` reason carries a ``:``-suffixed
#: value not enumerated here — this set lists only the fixed prefixes/values.
MISS_REASONS: frozenset[str] = frozenset(
    {
        "ledger-absent",
        "no-entry",
        "kind-not-human",
        "channel-not-interactive",
        "digest-mismatch",
        "session-mismatch",
        "chain-broken",
        "transcript-origin-not-human",
        "transcript-promptsource",
        "sidechain",
        "flag-off",
    }
)

#: The only harness this module resolves a witness ledger for today —
#: mirrors the only harness :mod:`promptwitness.harness.claude_code` adapts.
DEFAULT_HARNESS = "claude-code"

#: ``witness:<ledger_id>:<seq>`` — the anchor subject format
#: ``promptwitness.cli``'s ``anchor``/``anchor --upgrade`` commands write
#: (see ``promptwitness/cli.py::_WITNESS_SUBJECT_RE``). Duplicated here
#: (not imported) because that name is private to ``cli.py``.
_WITNESS_SUBJECT_RE = _compile(r"^witness:(?P<ledger_id>[^:]+):(?P<seq>\d+)$")


def resolve_default_witness_dir() -> Path:
    """The witness ledger directory for :data:`DEFAULT_HARNESS`.

    Thin wrapper over :func:`promptwitness.ledger.resolve_ledger_dir` so
    every athenaeum caller shares one harness-name literal.
    """
    return ledger.resolve_ledger_dir(DEFAULT_HARNESS)


@dataclass(frozen=True)
class AttestationOutcome:
    """The result of one :func:`attest` call.

    On a hit, every ``witness_*`` field is populated and ``miss_reason`` is
    ``None``. On a miss, ``miss_reason`` is one of the eleven
    :data:`MISS_REASONS`-shaped values and every ``witness_*`` field is
    ``None``. ``transcript_entrypoint`` is populated whenever a witness
    line was found, hit or miss (see the module docstring's "Reversible
    default" note) — it is the one field that can be non-``None`` on a miss.
    """

    hit: bool
    witness_tag: str | None = None
    witness_ref: str | None = None
    witness_strength: str | None = None
    witness_anchored: bool | None = None
    miss_reason: str | None = None
    transcript_entrypoint: str | None = None


class AttestationIndex:
    """One-run index over a witness ledger directory (issue athenaeum#2158).

    Built ONCE per run by :meth:`load` and held by the caller (mirrors how
    :class:`athenaeum.transcript_receipts.ReceiptRun` already holds the
    receipt ledger's own index) — never rebuilt per memory. Wraps
    :class:`promptwitness.ledger.LedgerIndex` (the ``prompt_id`` /
    ``(session, digest)`` lookup) and :func:`promptwitness.ledger.
    verify_chain` (the hash-chain + signature check), plus a small
    anchor-coverage scan this module owns (the public ``ledger`` API has no
    "does an anchor cover seq N" query).
    """

    def __init__(
        self,
        *,
        ledger_index: ledger.LedgerIndex,
        ledger_present: bool,
        chain_ok: bool,
        first_break_seq: int | None,
        anchored_through: Mapping[str, int],
        first_break_prev_seq: int | None = None,
    ) -> None:
        self._ledger_index = ledger_index
        self.ledger_present = ledger_present
        self.chain_ok = chain_ok
        self.first_break_seq = first_break_seq
        self.first_break_prev_seq = first_break_prev_seq
        self._anchored_through: dict[str, int] = dict(anchored_through)

    @classmethod
    def load(cls, witness_dir: Path) -> AttestationIndex:
        """Build the index: one :class:`~promptwitness.ledger.LedgerIndex`
        pass, one :func:`~promptwitness.ledger.verify_chain` pass, one
        anchor-coverage pass — each a single read per existing partition,
        and none of the three scales with how many memories this run
        processes (issue athenaeum#2158's read-budget AC)."""
        partitions = ledger.list_partitions(witness_dir)
        ledger_index = ledger.LedgerIndex.build(witness_dir)
        verification = ledger.verify_chain(witness_dir)
        anchored_through: dict[str, int] = {}
        for line in ledger.iter_lines(witness_dir):
            if line.kind != "anchor":
                continue
            subject = line.extra.get("subject") if isinstance(line.extra, Mapping) else None
            if not isinstance(subject, str):
                continue
            match = _WITNESS_SUBJECT_RE.match(subject)
            if not match:
                continue
            lid = match.group("ledger_id")
            seq = int(match.group("seq"))
            if seq > anchored_through.get(lid, -1):
                anchored_through[lid] = seq
        return cls(
            ledger_index=ledger_index,
            ledger_present=bool(partitions),
            chain_ok=verification.ok,
            first_break_seq=verification.first_break_seq,
            anchored_through=anchored_through,
            first_break_prev_seq=verification.first_break_prev_seq,
        )

    def lookup(
        self, prompt_id: str | None, origin_session_id: str, utterance_digest_sha: str
    ) -> ledger.WitnessLine | None:
        """By ``prompt_id`` first, falling back to ``(session, digest)``."""
        if prompt_id:
            line = self._ledger_index.by_prompt_id(prompt_id)
            if line is not None:
                return line
        matches = self._ledger_index.by_session_digest(origin_session_id, utterance_digest_sha)
        return matches[0] if matches else None

    def chain_intact_through(self, seq: int) -> bool:
        """See hit-rule item 6 in the module docstring. A content edit to
        line N breaks the chain at N+1, so the line physically preceding
        the first break is itself suspect, not just the break and after."""
        if self.first_break_seq is None:
            return True
        return seq < self.first_break_seq and seq != self.first_break_prev_seq

    def is_anchored(self, ledger_id: str, seq: int) -> bool:
        return seq <= self._anchored_through.get(ledger_id, -1)

    def anchored_through(self) -> dict[str, int]:
        """``{ledger_id: highest anchored seq}`` — a COPY (issue athenaeum#2160).

        The narrow public read of the anchor-coverage map built by
        :meth:`load`. :meth:`is_anchored` answers the per-line question;
        the readout (:mod:`athenaeum.witness_readout`) needs the map itself
        to report coverage per ledger, and a reader of that report must not
        be able to mutate this index's state, hence the copy.
        """
        return dict(self._anchored_through)


def _log_miss(
    reason: str,
    *,
    witness_ref: str | None,
    origin_session_id: str,
    entrypoint: str | None,
    transcript_origin_kind: str | None,
) -> None:
    """Exactly one WARNING per non-``flag-off`` miss (issue athenaeum#2158 AC) —
    the reason and ``witness_ref``/session only, never claim text or a
    digest. A miss that otherwise looks human-and-interactive
    (``transcript_origin_kind == "human"`` and the witness line's own
    recorded entrypoint is ``"cli"``) is logged as a PROVENANCE INCIDENT —
    still one call, a different message, never a second log line."""
    if reason == "flag-off":
        return
    if transcript_origin_kind == "human" and entrypoint == "cli":
        log.warning(
            "witness_attestation: PROVENANCE INCIDENT miss=%s witness_ref=%s session=%s",
            reason,
            witness_ref,
            origin_session_id,
        )
    else:
        log.warning(
            "witness_attestation: miss=%s witness_ref=%s session=%s",
            reason,
            witness_ref,
            origin_session_id,
        )


def attest(
    index: AttestationIndex | None,
    *,
    prompt_id: str | None,
    origin_session_id: str,
    claim: str,
    utterance_digest: str,
    matched_record_text: str,
    transcript_origin_kind: str | None,
    transcript_prompt_source: str | None,
    transcript_is_sidechain: bool,
) -> AttestationOutcome:
    """Attest one ``user-stated`` claim against the witness ledger.

    Args:
        index: the run's :class:`AttestationIndex`, or ``None`` when
            ``librarian.witness_attestation_enabled`` resolves off (the
            caller is responsible for that resolution — this function
            itself does no config lookup, so ``index=None`` is the ONLY
            way to reach the ``"flag-off"`` outcome and costs no I/O).
        prompt_id: the matched transcript record's own ``promptId``, or
            ``None``.
        origin_session_id: the claim's origin session id.
        claim: the original claim text (used only for the slash-command
            "lies inside the args" substring check — never hashed here,
            never logged).
        utterance_digest: hex SHA-256 (no ``"sha256:"`` prefix) of the
            NORMALIZED matched human-authored text — the same value
            :mod:`athenaeum.transcript_receipts` stamps as
            ``ReceiptEntry.utterance_digest``.
        matched_record_text: the matched record's full extracted text
            (:func:`athenaeum.transcript_verify._user_authored_text`'s
            return value) — used only to re-extract the ``<command-args>``
            span for a slash-command witness line.
        transcript_origin_kind: the matched record's ``origin.kind`` when
            an ``origin`` object is present, else ``None``.
        transcript_prompt_source: the matched record's ``promptSource``,
            or ``None`` when absent.
        transcript_is_sidechain: the matched record's ``isSidechain``,
            coerced to ``bool``.
    """
    if index is None:
        return AttestationOutcome(hit=False, miss_reason="flag-off")

    def miss(
        reason: str, line: ledger.WitnessLine | None, entrypoint: str | None
    ) -> AttestationOutcome:
        witness_ref = f"{line.ledger_id}:{line.seq}" if line is not None else None
        _log_miss(
            reason,
            witness_ref=witness_ref,
            origin_session_id=origin_session_id,
            entrypoint=entrypoint,
            transcript_origin_kind=transcript_origin_kind,
        )
        return AttestationOutcome(hit=False, miss_reason=reason, transcript_entrypoint=entrypoint)

    if not index.ledger_present:
        return miss("ledger-absent", None, None)

    sha_digest = "sha256:" + utterance_digest
    line = index.lookup(prompt_id, origin_session_id, sha_digest)
    if line is None:
        return miss("no-entry", None, None)

    signals = line.signals if isinstance(line.signals, Mapping) else {}
    entrypoint = signals.get("entrypoint")
    entrypoint = entrypoint if isinstance(entrypoint, str) else None
    agent_id = signals.get("agent_id")

    slash_segment = None
    if agent_id:
        return miss(f"kind-not-human:{line.prompt_kind}", line, entrypoint)
    if line.prompt_kind == "human":
        pass
    elif line.prompt_kind == "slash-command":
        slash_segment = next((s for s in line.segments if s.kind == "slash-args"), None)
        if slash_segment is None:
            return miss("kind-not-human:slash-command", line, entrypoint)
    else:
        return miss(f"kind-not-human:{line.prompt_kind}", line, entrypoint)

    if line.channel != "interactive":
        return miss(f"channel-not-interactive:{line.channel}", line, entrypoint)

    if line.session != origin_session_id:
        return miss("session-mismatch", line, entrypoint)

    if slash_segment is not None:
        # DEVIATION FROM THE ISSUE'S LITERAL WORDING, documented (see the
        # module docstring's "Known deviation" note): compare against the
        # shipped producer's RAW digest, via the producer function itself
        # -- never a re-derived formula -- so this stays correct if the
        # producer's own formula ever changes.
        record_segment = claude_code.slash_args_segment(matched_record_text)
        if record_segment is None:
            return miss("digest-mismatch", line, entrypoint)
        if slash_segment.digest != record_segment.digest:
            return miss("digest-mismatch", line, entrypoint)
        args_text = matched_record_text[record_segment.start : record_segment.end]
        if canon.normalize(claim) not in canon.normalize(args_text):
            return miss("digest-mismatch", line, entrypoint)
        witness_tag = "slash-args"
    else:
        if line.prompt_digest != sha_digest:
            return miss("digest-mismatch", line, entrypoint)
        witness_tag = "typed"

    if not index.chain_intact_through(line.seq):
        return miss("chain-broken", line, entrypoint)

    if transcript_origin_kind is not None and transcript_origin_kind != "human":
        return miss("transcript-origin-not-human", line, entrypoint)

    if transcript_prompt_source not in ("typed", "queued"):
        value = transcript_prompt_source if transcript_prompt_source is not None else "absent"
        return miss(f"transcript-promptsource:{value}", line, entrypoint)

    if transcript_is_sidechain:
        return miss("sidechain", line, entrypoint)

    return AttestationOutcome(
        hit=True,
        witness_tag=witness_tag,
        witness_ref=f"{line.ledger_id}:{line.seq}",
        witness_strength=line.key.strength,
        witness_anchored=index.is_anchored(line.ledger_id, line.seq),
        transcript_entrypoint=entrypoint,
    )
