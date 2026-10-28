# SPDX-License-Identifier: Apache-2.0
"""Harness-agnostic per-turn verification of a transcript against a witness
ledger (athenaeum#2155).

Layering: L1.5 (depends only on :mod:`promptwitness.ledger` and
:mod:`promptwitness.canon`, stdlib otherwise). Standard library only at
import time -- no ``athenaeum`` import anywhere (enforced by
``tests/test_promptwitness_boundary.py``).

**Transcript record shape.** This module does not know about any particular
harness's native transcript format (Claude Code's JSONL, or anyone else's);
that is deliberate -- :func:`verify_transcript` is the harness-agnostic
library entry point the issue specifies. A transcript record is any
``Mapping`` with:

- ``role`` -- ``"human"`` for a prompt a person is claimed to have typed;
  anything else (``"machine"``, ``"task-notification"``, ...) is treated as
  machine-authored and never looked up in the ledger.
- ``text`` -- the exact prompt text, before normalization.
- ``session`` -- the session id the ledger line was recorded under
  (optional; needed only for the digest-fallback lookup below).
- ``prompt_id`` -- the id the harness's hook recorded on the witness line
  for this turn (optional; preferred lookup key when present).

:func:`load_transcript_records` reads this shape from a JSON file (a
top-level array of objects with those four keys) -- the format
``promptwitness verify --transcript`` and ``promptwitness demo
--transcript`` (real-transcript mode) both read.

**Verdict vocabulary** (the issue's own badge enumeration). The literal
string a :class:`TurnVerdict` carries in ``verdict`` is the terse,
machine-stable form; :func:`verdict_label` renders the issue's fuller
display text for the HTML demo:

======================================= ===============================
``TurnVerdict.verdict``                 :func:`verdict_label` output
======================================= ===============================
``"witnessed"``                         ``"witnessed"``
``"not-witnessed(inserted)"``           ``"not witnessed (inserted)"``
``"digest-mismatch(edited)"``           ``"digest mismatch (edited)"``
``"machine-authored"``                  ``"machine-authored (task-notification)"`` [#]_
``"headless"``                          ``"headless"``
``"bridge"``                            ``"bridge"``
``"chain-broken-at-seq-<N>"``           ``"chain broken at seq <N>"``
======================================= ===============================

.. [#] The parenthetical names the matched witness line's actual
   ``prompt_kind`` (``task-notification``, ``subagent``,
   ``launch-argument``, ...) via :attr:`TurnVerdict.detail` --
   :func:`verdict_label` takes an optional *detail* argument for this. A
   transcript record whose own ``role`` is already non-human never reaches
   the ledger at all (see :func:`verify_transcript` step 1) and always
   renders the default ``"(task-notification)"`` (``detail=""``); a
   ``role="human"`` record whose MATCHED LINE turns out not to be
   witnessable-human (step 3 below) carries the line's real kind instead.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from promptwitness import canon, ledger

__all__ = [
    "VERDICT_WITNESSED",
    "VERDICT_MACHINE_AUTHORED",
    "VERDICT_NOT_WITNESSED_INSERTED",
    "VERDICT_DIGEST_MISMATCH_EDITED",
    "VERDICT_HEADLESS",
    "VERDICT_BRIDGE",
    "TurnVerdict",
    "chain_broken_verdict",
    "is_chain_broken_verdict",
    "verdict_label",
    "verify_transcript",
    "all_human_turns_witnessed",
    "load_transcript_records",
]

VERDICT_WITNESSED = "witnessed"
VERDICT_MACHINE_AUTHORED = "machine-authored"
VERDICT_NOT_WITNESSED_INSERTED = "not-witnessed(inserted)"
VERDICT_DIGEST_MISMATCH_EDITED = "digest-mismatch(edited)"
VERDICT_HEADLESS = "headless"
VERDICT_BRIDGE = "bridge"

_CHAIN_BROKEN_PREFIX = "chain-broken-at-seq-"

#: Full display text for the HTML demo's badges, keyed by the terse
#: ``TurnVerdict.verdict`` string -- the issue's own badge enumeration,
#: copied verbatim. ``"chain broken at seq N"`` is not a fixed-N key here;
#: :func:`verdict_label` renders it from :data:`_CHAIN_BROKEN_PREFIX`.
_VERDICT_LABELS: Mapping[str, str] = {
    VERDICT_WITNESSED: "witnessed",
    VERDICT_NOT_WITNESSED_INSERTED: "not witnessed (inserted)",
    VERDICT_DIGEST_MISMATCH_EDITED: "digest mismatch (edited)",
    VERDICT_MACHINE_AUTHORED: "machine-authored (task-notification)",
    VERDICT_HEADLESS: "headless",
    VERDICT_BRIDGE: "bridge",
}


def chain_broken_verdict(first_break_seq: int) -> str:
    """The terse verdict string for a witness line at or after a hash-chain
    break first detected at *first_break_seq*."""
    return f"{_CHAIN_BROKEN_PREFIX}{first_break_seq}"


def is_chain_broken_verdict(verdict: str) -> bool:
    """``True`` iff *verdict* is one :func:`chain_broken_verdict` produced --
    the public test a sibling module (``promptwitness.demo``'s badge-class
    lookup) uses instead of reaching into :data:`_CHAIN_BROKEN_PREFIX`."""
    return verdict.startswith(_CHAIN_BROKEN_PREFIX)


def verdict_label(verdict: str, detail: str = "") -> str:
    """The issue's full display text for a terse ``TurnVerdict.verdict``
    string, for the HTML demo's badges. Returns *verdict* unchanged for any
    value this module did not itself produce (defensive; every value
    :func:`verify_transcript` can emit is handled).

    *detail* (normally ``turn_verdict.detail``) overrides the parenthetical
    for :data:`VERDICT_MACHINE_AUTHORED` with the matched line's actual
    ``prompt_kind`` (``"subagent"``, ``"launch-argument"``, ...) when given;
    the default ``""`` preserves the original ``"(task-notification)"``
    text for every existing caller that does not pass it.
    """
    if verdict == VERDICT_MACHINE_AUTHORED and detail:
        return f"machine-authored ({detail})"
    if verdict in _VERDICT_LABELS:
        return _VERDICT_LABELS[verdict]
    if verdict.startswith(_CHAIN_BROKEN_PREFIX):
        seq = verdict[len(_CHAIN_BROKEN_PREFIX) :]
        return f"chain broken at seq {seq}"
    return verdict


@dataclass(frozen=True)
class TurnVerdict:
    """One transcript record's verification result.

    ``index`` is the record's 0-based position in the *transcript_records*
    sequence passed to :func:`verify_transcript` -- the row this verdict
    aligns with in the demo's side-by-side rendering. ``is_human`` is
    ``True`` iff the record's own ``role`` was ``"human"``; it is what
    :func:`all_human_turns_witnessed` filters on, not the verdict string
    itself -- a ``role="human"`` record is held to the witnessed bar no
    matter what verdict it carries (including :data:`VERDICT_MACHINE_AUTHORED`,
    when the ledger's own evidence contradicts the transcript's claim), while
    a genuinely non-human record (``is_human=False``) always carries
    :data:`VERDICT_MACHINE_AUTHORED` and is exempt from the bar.

    ``detail`` is ``""`` except for a :data:`VERDICT_MACHINE_AUTHORED`
    verdict reached via a ledger-side ``prompt_kind`` mismatch (not via a
    non-human ``role``), where it carries that line's actual ``prompt_kind``
    (``"task-notification"``, ``"subagent"``, ``"launch-argument"``, ...)
    for :func:`verdict_label` to render.
    """

    index: int
    is_human: bool
    verdict: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "is_human": self.is_human,
            "verdict": self.verdict,
            "detail": self.detail,
        }


def _lookup_witness_line(
    ledger_index: ledger.LedgerIndex,
    *,
    prompt_id: str | None,
    session: str | None,
    text: str,
) -> ledger.WitnessLine | None:
    """Preferred lookup by ``prompt_id``; falls back to ``(session,
    prompt_digest)`` when no ``prompt_id`` is given or it is not found --
    the same two-key shape :class:`~promptwitness.ledger.LedgerIndex` itself
    indexes by."""
    if prompt_id:
        line = ledger_index.by_prompt_id(str(prompt_id))
        if line is not None:
            return line
    if session:
        digest = canon.digest(canon.normalize(text))
        candidates = ledger_index.by_session_digest(str(session), digest)
        if candidates:
            return candidates[0]
    return None


def verify_transcript(
    transcript_records: Sequence[Mapping[str, Any]],
    ledger_index: ledger.LedgerIndex,
) -> list[TurnVerdict]:
    """Verify every record in *transcript_records* against *ledger_index*.

    For each record, in order:

    1. ``role != "human"`` -> :data:`VERDICT_MACHINE_AUTHORED`, no ledger
       lookup at all (a machine-authored turn is never held to the
       witnessed bar).
    2. No matching witness line found (by ``prompt_id``, then by
       ``(session, prompt_digest)``) -> :data:`VERDICT_NOT_WITNESSED_INSERTED`
       -- this is what a turn inserted into the transcript after the fact,
       with no corresponding ledger line, looks like.
    3. A matching line whose ``channel`` is ``"headless"`` or ``"bridge"``
       -> :data:`VERDICT_HEADLESS` / :data:`VERDICT_BRIDGE` (operator
       ruling: neither channel is ever human, independent of digest match).
    4. A matching line that is not witnessable-human by
       :meth:`~promptwitness.ledger.LedgerIndex.is_witnessable_human`
       (``channel != "interactive"``, or ``prompt_kind`` not in ``("human",
       "slash-command")`` -- a task-notification, subagent, or
       launch-argument line, for instance) -> :data:`VERDICT_MACHINE_AUTHORED`
       with ``detail`` set to the line's actual ``prompt_kind``, so a
       transcript record that CLAIMS ``role="human"`` for a line the ledger
       itself never recorded as human cannot verify as witnessed just
       because some digest happens to match (review finding #2 on PR
       athenaeum#2175: ``by_prompt_id``/``by_session_digest`` index every
       ``kind == "turn"`` line regardless of kind, so this check cannot be
       skipped). Counted as ``is_human=True`` -- fail safe: the transcript's
       own claim governs whether this turn is held to the witnessed bar,
       not the ledger's (possibly wrong, possibly tampered) say-so.
    5. The matching line's ``prompt_digest`` does not equal
       ``canon.digest(canon.normalize(record["text"]))``
       -> :data:`VERDICT_DIGEST_MISMATCH_EDITED` -- the record was found,
       but its text was edited after the witness line was written.
    6. The ledger's hash chain is broken at this line's ``seq``, at any
       later ``seq``, or at the ``seq`` immediately preceding the break in
       on-disk order (:attr:`~promptwitness.ledger.LedgerIndex.
       chain_verification`) -> :func:`chain_broken_verdict`. The
       "immediately preceding" half matters: per
       :class:`~promptwitness.ledger.ChainVerification`'s own docstring, a
       content mutation of line N (anything other than N's own ``prev``)
       surfaces as a break reported at N+1, so a verifier that only flagged
       ``seq >= first_break_seq`` would exempt the one line that was
       actually tampered -- the exact forgery this function exists to catch
       (review finding #1 on PR athenaeum#2175). Fail safe: a false "chain
       broken" costs a second look; a false "witnessed" is a forged
       provenance claim.
    7. Otherwise -> :data:`VERDICT_WITNESSED`.
    """
    verdicts: list[TurnVerdict] = []
    for index, record in enumerate(transcript_records):
        role = str(record.get("role") or "")
        text = str(record.get("text") or "")
        if role != "human":
            verdicts.append(
                TurnVerdict(index=index, is_human=False, verdict=VERDICT_MACHINE_AUTHORED)
            )
            continue

        session = record.get("session")
        prompt_id = record.get("prompt_id")
        line = _lookup_witness_line(
            ledger_index,
            prompt_id=str(prompt_id) if prompt_id else None,
            session=str(session) if session else None,
            text=text,
        )
        if line is None:
            verdicts.append(
                TurnVerdict(index=index, is_human=True, verdict=VERDICT_NOT_WITNESSED_INSERTED)
            )
            continue
        if line.channel == "headless":
            verdicts.append(TurnVerdict(index=index, is_human=True, verdict=VERDICT_HEADLESS))
            continue
        if line.channel == "bridge":
            verdicts.append(TurnVerdict(index=index, is_human=True, verdict=VERDICT_BRIDGE))
            continue
        if not ledger.LedgerIndex.is_witnessable_human(line):
            verdicts.append(
                TurnVerdict(
                    index=index,
                    is_human=True,
                    verdict=VERDICT_MACHINE_AUTHORED,
                    detail=str(line.prompt_kind or ""),
                )
            )
            continue

        expected_digest = canon.digest(canon.normalize(text))
        if line.prompt_digest != expected_digest:
            verdicts.append(
                TurnVerdict(index=index, is_human=True, verdict=VERDICT_DIGEST_MISMATCH_EDITED)
            )
            continue

        chain = ledger_index.chain_verification
        if chain is not None and not chain.ok and chain.first_break_seq is not None:
            is_suspect = line.seq >= chain.first_break_seq or (
                chain.first_break_prev_seq is not None
                and line.seq == chain.first_break_prev_seq
            )
            if is_suspect:
                verdicts.append(
                    TurnVerdict(
                        index=index,
                        is_human=True,
                        verdict=chain_broken_verdict(chain.first_break_seq),
                    )
                )
                continue

        verdicts.append(TurnVerdict(index=index, is_human=True, verdict=VERDICT_WITNESSED))
    return verdicts


def all_human_turns_witnessed(verdicts: Sequence[TurnVerdict]) -> bool:
    """``True`` iff every human-role record in *verdicts* carries
    :data:`VERDICT_WITNESSED` -- the exit-code rule ``promptwitness
    verify`` implements. A transcript with no human turns at all is
    vacuously witnessed."""
    return all(v.verdict == VERDICT_WITNESSED for v in verdicts if v.is_human)


def load_transcript_records(path: str | Path) -> list[dict[str, Any]]:
    """Load a transcript JSON file: a top-level array of objects, each with
    ``role``/``text``/``session``/``prompt_id`` (the last two optional) --
    see the module docstring's "Transcript record shape". Raises
    :class:`ValueError` for anything else (not a JSON array, or an element
    that is not an object), so a malformed file fails loudly at the CLI
    rather than silently verifying nothing.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{path}: expected a top-level JSON array of transcript records")
    records: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(f"{path}: every transcript record must be a JSON object")
        records.append(
            {
                "role": str(item.get("role") or "human"),
                "text": str(item.get("text") or ""),
                "session": item.get("session"),
                "prompt_id": item.get("prompt_id"),
            }
        )
    return records
