# SPDX-License-Identifier: Apache-2.0
"""Witness-attestation readout: witnessed hits, and every miss as an INCIDENT
(issue athenaeum#2160, epic athenaeum#2144).

Operator ruling 10 (2026-10-28) settled the shape of this report: the miss
counter stays, but a provenance miss is an INCIDENT, never a rate to
tolerate. There is therefore deliberately **no threshold and no readout
window** here beyond the caller's own ``since`` month filter — a tolerated
miss rate was explicitly removed from the design, and adding one back is a
product change, not a tuning knob.

**Two data sources, and only two: the receipt ledger and the witness
index.** No transcript is opened, no claim text is read, and nothing this
module emits is derived from a digest. That is not a convention — a human
utterance is short enough that a published digest is dictionary-attack
recoverable (see :mod:`athenaeum.transcript_receipts`'s own no-plaintext
note), so the readout's output is counts, enum labels, session ids,
``witness_ref`` coordinates and nothing else. Pinned by
``tests/test_witness_readout.py::TestNoPlaintext``, which asserts no 64-hex
run appears in either rendering.

**What it reports** (the issue's list, in order):

1. the ``user-stated`` receipt total;
2. the ``witnessed`` total, split by ``witness_tag`` (``typed`` /
   ``slash-args``);
3. misses broken down by reason — bucketed by the prefix BEFORE the first
   ``":"``, because three of the eleven
   :data:`athenaeum.witness_attestation.MISS_REASONS` values carry a
   ``:``-suffixed value (``kind-not-human:<kind>``,
   ``channel-not-interactive:<ch>``, ``transcript-promptsource:<value>``)
   and an unbucketed breakdown would have unboundedly many keys;
4. provenance incidents — every miss other than ``flag-off``, each with its
   full reason, its session and its ``witness_ref``. A miss whose receipt
   carries ``transcript_origin == "human"`` and
   ``transcript_entrypoint == "cli"`` is flagged ``likely_false_negative``:
   both fields are persisted on the receipt itself as of schema v2
   (:class:`athenaeum.transcript_receipts.ReceiptEntry`), so this flag is
   read from the permitted sources with no derivation;
5. the unsigned-line count (``WitnessLine.sig is None``) and the anchor
   coverage, both from the witness index;
6. the ``witness_hook_errors.jsonl`` count broken down by error class, each
   class one incident. The file is written by
   :func:`athenaeum.claude_code_adapter._log_witness_error`, so the cache
   dir is resolved through the same :func:`athenaeum.config.
   resolve_cache_dir` that module uses rather than by a second path
   literal, and it is read TOLERANTLY — a torn last line from a hook that
   died mid-append must not crash a readout;
7. the ``session-close`` deficit, per ENDED session (one that has a
   ``kind="session-close"`` witness line, written by
   :func:`promptwitness.harness.claude_code.session_close`): the number of
   that session's witness-ELIGIBLE turn lines minus the number that are
   witnessABLE-human. Readable from the ledger alone, which is why
   "hook-eligible transcript records minus witnessed turns" needs no
   transcript read.

**Two corrections to the obvious readings of (1) and (7)**, both found by
the review on PR athenaeum#2186 and both load-bearing for ruling 10 — a
readout that fires on a healthy night makes the incident channel noise,
which defeats "misses should be zero absent an incident" just as surely as
a readout that misses a real one.

*The receipt ledger is append-only, so it holds history, not state.*
:func:`athenaeum.transcript_receipts.iter_receipts` returns every receipt
LINE ever written, and
:func:`~athenaeum.transcript_receipts.write_receipt_for_origin` appends a
NEW line whenever a memory's channel or utterance digest changes — so a
memory whose ``user-stated`` miss was later resolved to a ``witnessed`` hit
has BOTH lines on disk. This module therefore collapses to the CURRENT
receipt per memory before counting anything, latest-wins, matching
:func:`athenaeum.transcript_receipts.lookup_receipt`'s own selection rule
(sort by ``recorded_at``, take the last). The collapse key adds
``origin_session_id`` to that function's ``(origin_scope, memory_digest)``
pair — strictly finer, so it can only keep MORE incidents, which is the
conservative direction for a provenance report and matches
``lookup_receipt``'s own warning that a receipt minted for one session must
never speak for another.

*``turns - turns_witnessed`` is not the deficit.*
:func:`~promptwitness.harness.claude_code.session_close` counts ``turns``
as EVERY ``kind="turn"`` line for the session and ``turns_witnessed`` as
the :meth:`~promptwitness.ledger.LedgerIndex.is_witnessable_human` subset
of the same lines, so their difference is "turn lines that are not
interactive-and-human" — which a healthy session routinely has:
``record_turn`` writes a line for every ``UserPromptSubmit``, including a
``<system-reminder>`` (``system``), a finished subagent's
``<task-notification>``, a subagent prompt, and the first turn of a plain
``claude "<prompt>"`` TTY launch (``launch-argument``). Each of those is
by design, not a miss. This module instead counts the deficit over the
witness-ELIGIBLE population — interactive turn lines whose kind is not in
:data:`NEVER_WITNESSED_KINDS` — minus the witnessable ones, which is zero
on a healthy session and non-zero exactly when a turn the design DOES
attest was not attested. The close line's own ``turns``/``turns_witnessed``
snapshot is still reported (``session_close_turns`` /
``session_close_turns_witnessed``, taken latest-wins per session because
each close line is a CUMULATIVE re-scan of the whole ledger, not an
increment — summing them double-counts a resumed session) but it does not
drive an incident. See this module's own ``REVERSIBLE DEFAULT`` note below.

**REVERSIBLE DEFAULT (recorded for the lane report).** The issue's literal
wording for (7) is "hook-eligible transcript records minus witnessed
turns", and the lane brief spelled that as ``turns - turns_witnessed``. The
evidence above shows that formula fires on healthy sessions, so what ships
is the witness-eligible population instead. Both inputs stay in the output,
so an operator can see the literal pair as well as the eligible-population
deficit, and reverting is a one-line change in :func:`_witness_side`.

**Three incident CLASSES**, and that granularity is load-bearing: the
nightly librarian logs one WARNING per class with its count
(:func:`athenaeum.librarian._maybe_run_nightly_witness_readout`), not one
per incident, so a bad night is a handful of lines rather than one per
memory.

**Month filtering.** ``since`` is a ``YYYY-MM`` string compared
lexicographically against each record's own month, derived exactly as the
existing writers derive theirs: a receipt's month is
``ReceiptEntry.recorded_at[:7]`` (the derivation
:func:`athenaeum.transcript_receipts.append_receipt` uses to pick a
partition) and a witness line's month is ``WitnessLine.ts[:7]`` (the
derivation :func:`athenaeum.witness_anchor` uses to pick a mirror
partition). No second derivation is invented here.

Layering: L3 service. Imports :mod:`athenaeum.transcript_receipts` (L2),
:mod:`athenaeum.witness_attestation` (L2), :mod:`athenaeum.config` (L2) and
:mod:`promptwitness.ledger` / :mod:`promptwitness.harness.claude_code`
(for the ``WRAPPERS`` kind table), and nothing above — which is what lets both
the L5 CLI (``athenaeum receipts attest-readout``) and the L4 nightly
librarian call the SAME summary rather than each computing its own.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from athenaeum import transcript_receipts, witness_attestation
from athenaeum.config import resolve_cache_dir
from promptwitness import ledger
from promptwitness.harness import claude_code

__all__ = [
    "HOOK_ERROR_FILENAME",
    "NEVER_WITNESSED_KINDS",
    "INCIDENT_CLASSES",
    "INCIDENT_HOOK_ERROR",
    "INCIDENT_PROVENANCE_MISS",
    "INCIDENT_SESSION_CLOSE_DEFICIT",
    "Incident",
    "ReadoutSummary",
    "build_readout",
    "render_text",
]

#: Incident class: a receipt whose ``witness_miss`` is anything other than
#: ``flag-off``. One incident per receipt (ruling 10 — every miss is an
#: incident, there is no rate).
INCIDENT_PROVENANCE_MISS = "provenance-miss"

#: Incident class: one error class seen in ``witness_hook_errors.jsonl``.
#: One incident per CLASS, carrying that class's count — the file records an
#: exception class name per failed hook invocation, and a hook that fails
#: the same way a hundred times is one problem.
INCIDENT_HOOK_ERROR = "hook-error"

#: Incident class: one ENDED session with witness-eligible turn lines that
#: are not witnessable-human. See the module docstring's second correction
#: for why this is NOT ``turns - turns_witnessed``.
INCIDENT_SESSION_CLOSE_DEFICIT = "session-close-deficit"

#: Every incident class, in the order the readout renders them.
INCIDENT_CLASSES: tuple[str, ...] = (
    INCIDENT_PROVENANCE_MISS,
    INCIDENT_HOOK_ERROR,
    INCIDENT_SESSION_CLOSE_DEFICIT,
)

#: The hook-error ledger's filename under the resolved cache dir. Same
#: literal :func:`athenaeum.claude_code_adapter._log_witness_error` writes;
#: the DIRECTORY is resolved, never hardcoded.
HOOK_ERROR_FILENAME = "witness_hook_errors.jsonl"

#: The miss reason that is explicitly NOT an incident: attestation was
#: turned off, so there was never a check to miss.
_NOT_AN_INCIDENT = "flag-off"

#: Prompt kinds the witness design deliberately NEVER attests, so a turn
#: line carrying one is **not** witness-eligible and must not count toward
#: the ``session-close`` deficit (review of PR athenaeum#2186, finding 1b).
#:
#: Derived from :data:`promptwitness.harness.claude_code.WRAPPERS`' own
#: ``human=False`` rows rather than retyped, plus the two override kinds
#: :func:`~promptwitness.harness.claude_code.classify_prompt_kind` applies
#: ahead of that table (``launch-argument``, ``subagent``). ``unknown`` is
#: deliberately ABSENT: an interactive prompt the classifier could not
#: recognise is exactly a turn that should have been witnessed and was not,
#: which is the failure shape the deficit exists to surface.
NEVER_WITNESSED_KINDS: frozenset[str] = frozenset(
    {rule.kind for rule in claude_code.WRAPPERS if not rule.human}
    | {"launch-argument", "subagent"}
)


def _read_jsonl_tolerant(path: Path) -> list[dict[str, Any]]:
    """Every well-formed JSON object line in *path*; missing file -> ``[]``.

    A module-local copy on purpose: this is the repo's established posture
    for a tolerantly-read sidecar ledger (``transcript_receipts.py``,
    ``receipt_sealers.py`` and ``verdicts.py`` each hold their own), and
    importing another module's private helper would couple this module to
    a name that is not part of that module's API.
    """
    if not path.exists():
        return []
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            continue  # torn trailing write from a hook that died mid-append
        if isinstance(record, dict):
            out.append(record)
    return out


def _reason_bucket(reason: str) -> str:
    """The stable breakdown key for a miss reason.

    Three of the eleven :data:`athenaeum.witness_attestation.MISS_REASONS`
    values carry a ``:``-suffixed value, so the prefix before the first
    ``":"`` is the only bucketing that keeps the breakdown's key set
    bounded and comparable night to night.
    """
    return reason.split(":", 1)[0]


def _in_window(month: str, since: str | None) -> bool:
    """``YYYY-MM`` lexicographic compare — correct because the format is
    fixed-width and zero-padded. An empty/unparseable month is KEPT: a
    record the readout cannot date is a record it must not silently drop."""
    if since is None:
        return True
    if len(month) != 7:
        return True
    return month >= since


@dataclass(frozen=True)
class Incident:
    """One incident, in one of the three :data:`INCIDENT_CLASSES`.

    ``count`` is 1 for a provenance miss (one receipt, one incident) and the
    class/session total for a hook-error class or a session-close deficit.
    Carries no digest and no text — ``reason`` is an enum label, ``session``
    an origin session id, ``witness_ref`` a ``<ledger_id>:<seq>`` coordinate.
    """

    incident_class: str
    reason: str
    session: str | None = None
    witness_ref: str | None = None
    count: int = 1
    likely_false_negative: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "incident_class": self.incident_class,
            "reason": self.reason,
            "session": self.session,
            "witness_ref": self.witness_ref,
            "count": self.count,
            "likely_false_negative": self.likely_false_negative,
        }


@dataclass(frozen=True)
class ReadoutSummary:
    """The whole readout, computed once and rendered two ways.

    ONE dataclass with a :meth:`to_dict`, mirroring
    :class:`athenaeum.transcript_receipts.ReceiptsRunStats` and
    :class:`athenaeum.receipt_sealers.VerifyReport`: the text rendering
    (:func:`render_text`) is produced from this object, never computed
    independently, so the two renderings cannot disagree about a number.
    """

    since: str | None = None
    user_stated_total: int = 0
    witnessed_total: int = 0
    witnessed_by_tag: dict[str, int] = field(default_factory=dict)
    miss_counts: dict[str, int] = field(default_factory=dict)
    unsigned_lines: int = 0
    witness_lines_total: int = 0
    anchored_lines: int = 0
    anchored_through: dict[str, int] = field(default_factory=dict)
    hook_error_counts: dict[str, int] = field(default_factory=dict)
    session_close_deficits: dict[str, int] = field(default_factory=dict)
    session_close_turns: int = 0
    session_close_turns_witnessed: int = 0
    superseded_receipt_lines: int = 0
    incidents: tuple[Incident, ...] = ()

    @property
    def session_close_deficit(self) -> int:
        """Total witness-eligible-but-unwitnessed turns across every ended
        session. NOT ``session_close_turns - session_close_turns_witnessed``
        — see the module docstring's second correction."""
        return sum(self.session_close_deficits.values())

    @property
    def anchor_coverage(self) -> float | None:
        """Anchored witness lines / witness lines in window; ``None`` when
        there are no lines at all (a zero would read as "nothing is
        anchored", which is a different claim from "there is nothing")."""
        if not self.witness_lines_total:
            return None
        return self.anchored_lines / self.witness_lines_total

    @property
    def ok(self) -> bool:
        """``True`` when there is no incident of any class."""
        return not self.incidents

    def incident_class_counts(self) -> dict[str, int]:
        """``{incident_class: total count}``, classes with none omitted —
        the nightly's one-WARNING-per-class input."""
        out: dict[str, int] = {}
        for incident in self.incidents:
            out[incident.incident_class] = out.get(incident.incident_class, 0) + incident.count
        return {k: out[k] for k in INCIDENT_CLASSES if k in out}

    def to_dict(self) -> dict[str, Any]:
        return {
            "since": self.since,
            "user_stated_total": self.user_stated_total,
            "witnessed_total": self.witnessed_total,
            "witnessed_by_tag": dict(self.witnessed_by_tag),
            "miss_counts": dict(self.miss_counts),
            "unsigned_lines": self.unsigned_lines,
            "witness_lines_total": self.witness_lines_total,
            "anchored_lines": self.anchored_lines,
            "anchor_coverage": self.anchor_coverage,
            "anchored_through": dict(self.anchored_through),
            "hook_error_counts": dict(self.hook_error_counts),
            "session_close_deficit": self.session_close_deficit,
            "session_close_deficits": dict(self.session_close_deficits),
            # The close lines' own cumulative snapshot, latest-wins per
            # session. Reported, never an incident driver.
            "session_close_turns": self.session_close_turns,
            "session_close_turns_witnessed": self.session_close_turns_witnessed,
            "superseded_receipt_lines": self.superseded_receipt_lines,
            "incidents": [i.to_dict() for i in self.incidents],
            "incident_class_counts": self.incident_class_counts(),
            "ok": self.ok,
        }


def _receipt_key(entry: transcript_receipts.ReceiptEntry) -> tuple[str, str, str | None]:
    """The identity of the memory a receipt line speaks for.

    ``lookup_receipt``'s ``(origin_scope, memory_digest)`` plus
    ``origin_session_id``: strictly finer, so collapsing on it can only keep
    MORE incidents than that function would — the conservative direction
    here, and it respects that function's own rule that a receipt minted for
    one session must never speak for another.
    """
    return (entry.origin_scope, entry.memory_digest, entry.origin_session_id)


def _current_receipts(
    wiki_root: Path, since: str | None
) -> tuple[list[transcript_receipts.ReceiptEntry], int]:
    """``(current receipt per memory, superseded line count)``.

    The ledger is append-only, so it carries HISTORY: a memory whose miss
    was later resolved has both lines on disk. Latest-wins by
    ``recorded_at``, with on-disk order as the tie-break (``iter_receipts``
    returns partitions sorted and lines in append order, and this loop keeps
    the later of two equal timestamps) — the same selection
    :func:`athenaeum.transcript_receipts.lookup_receipt` makes.

    The window is applied BEFORE the collapse, so the result is "the newest
    receipt for this memory *within the window*" rather than a line that the
    window excluded.
    """
    newest: dict[tuple[str, str, str | None], transcript_receipts.ReceiptEntry] = {}
    superseded = 0
    for entry in transcript_receipts.iter_receipts(wiki_root):
        if not _in_window(entry.recorded_at[:7], since):
            continue
        key = _receipt_key(entry)
        kept = newest.get(key)
        if kept is None:
            newest[key] = entry
            continue
        superseded += 1
        if entry.recorded_at >= kept.recorded_at:
            newest[key] = entry
    return list(newest.values()), superseded


def _receipt_side(
    wiki_root: Path, since: str | None
) -> tuple[int, int, dict[str, int], dict[str, int], list[Incident], int]:
    """Receipt-ledger half: totals, tag split, miss breakdown, incidents.

    Counts the CURRENT receipt per memory, never every historical line —
    otherwise a miss that has since been resolved to a ``witnessed`` hit
    would still be reported as a live incident.
    """
    user_stated = 0
    witnessed = 0
    by_tag: dict[str, int] = {}
    miss_counts: dict[str, int] = {}
    incidents: list[Incident] = []

    entries, superseded = _current_receipts(wiki_root, since)
    for entry in entries:
        if entry.channel == "user-stated":
            user_stated += 1
        elif entry.channel == "witnessed":
            witnessed += 1
            tag = entry.witness_tag or "untagged"
            by_tag[tag] = by_tag.get(tag, 0) + 1
        reason = entry.witness_miss
        if not reason:
            continue
        bucket = _reason_bucket(reason)
        miss_counts[bucket] = miss_counts.get(bucket, 0) + 1
        if bucket == _NOT_AN_INCIDENT:
            continue
        incidents.append(
            Incident(
                incident_class=INCIDENT_PROVENANCE_MISS,
                reason=reason,
                session=entry.origin_session_id,
                witness_ref=entry.witness_ref,
                likely_false_negative=(
                    entry.transcript_origin == "human" and entry.transcript_entrypoint == "cli"
                ),
            )
        )
    return user_stated, witnessed, by_tag, miss_counts, incidents, superseded


def _is_witness_eligible(line: ledger.WitnessLine) -> bool:
    """Was the witness design in a position to attest this turn at all?

    ``interactive`` channel (headless and bridge are never human — the
    operator ruling :meth:`~promptwitness.ledger.LedgerIndex.
    is_witnessable_human` cites) and a kind outside
    :data:`NEVER_WITNESSED_KINDS`. Strictly WIDER than
    ``is_witnessable_human``, by exactly one case: ``prompt_kind ==
    "unknown"`` — an interactive prompt whose leading ``<tag`` the
    classifier did not recognise, which is the one shape that means "a turn
    the design attests went unattested".
    """
    return (
        line.kind == "turn"
        and line.channel == "interactive"
        and line.prompt_kind not in NEVER_WITNESSED_KINDS
    )


def _witness_side(
    witness_dir: Path, since: str | None
) -> tuple[int, int, int, dict[str, int], dict[str, int], int, int]:
    """Witness-index half: unsigned lines, anchor coverage, deficits.

    One pass over the ledger. Deficits are reported only for ENDED sessions
    — a session with no ``session-close`` line is still in flight — and are
    computed over the witness-ELIGIBLE population rather than from the close
    line's ``turns``/``turns_witnessed`` pair (module docstring, second
    correction). That pair is still summed and returned, latest-wins per
    session: each close line re-scans the whole ledger, so it is a
    cumulative snapshot and adding two of them double-counts a resumed
    session.
    """
    index = witness_attestation.AttestationIndex.load(witness_dir)
    anchored_through = index.anchored_through()
    unsigned = 0
    total = 0
    anchored = 0
    eligible: dict[str, int] = {}
    witnessable: dict[str, int] = {}
    # session -> (seq of the close line this snapshot came from, turns, witnessed)
    snapshots: dict[str, tuple[int, int, int]] = {}

    for line in ledger.iter_lines(witness_dir):
        if not _in_window(str(line.ts)[:7], since):
            continue
        total += 1
        if line.sig is None:
            unsigned += 1
        if index.is_anchored(line.ledger_id, line.seq):
            anchored += 1
        session = line.session
        if line.kind == "turn" and session:
            if _is_witness_eligible(line):
                eligible[session] = eligible.get(session, 0) + 1
            if ledger.LedgerIndex.is_witnessable_human(line):
                witnessable[session] = witnessable.get(session, 0) + 1
            continue
        if line.kind != "session-close" or not session:
            continue
        held = snapshots.get(session)
        if held is None or line.seq >= held[0]:
            snapshots[session] = (line.seq, line.turns or 0, line.turns_witnessed or 0)

    deficits: dict[str, int] = {}
    for session in snapshots:
        deficit = eligible.get(session, 0) - witnessable.get(session, 0)
        if deficit > 0:
            deficits[session] = deficit
    turns_total = sum(snap[1] for snap in snapshots.values())
    witnessed_total = sum(snap[2] for snap in snapshots.values())
    return (
        unsigned,
        total,
        anchored,
        anchored_through,
        deficits,
        turns_total,
        witnessed_total,
    )


def _hook_error_counts(cache_dir: Path, since: str | None) -> dict[str, int]:
    """``{exception class name: count}`` from ``witness_hook_errors.jsonl``."""
    counts: dict[str, int] = {}
    for record in _read_jsonl_tolerant(cache_dir.joinpath(HOOK_ERROR_FILENAME)):
        ts = record.get("ts")
        if not _in_window(str(ts)[:7] if isinstance(ts, str) else "", since):
            continue
        name = record.get("error")
        key = name if isinstance(name, str) and name else "unknown"
        counts[key] = counts.get(key, 0) + 1
    return counts


def build_readout(
    wiki_root: Path,
    *,
    witness_dir: Path | None = None,
    cache_dir: Path | None = None,
    since: str | None = None,
) -> ReadoutSummary:
    """Build the readout over the receipt ledger and the witness index.

    Args:
        wiki_root: the knowledge repo's ``wiki/`` directory — the receipt
            ledger's root (:func:`athenaeum.transcript_receipts.ledger_dir`).
        witness_dir: the witness ledger directory; defaults to
            :func:`athenaeum.witness_attestation.resolve_default_witness_dir`.
        cache_dir: the athenaeum cache dir holding
            ``witness_hook_errors.jsonl``; defaults to
            :func:`athenaeum.config.resolve_cache_dir` — the same resolution
            ``claude_code_adapter`` uses when it WRITES that file.
        since: ``YYYY-MM``; records in earlier months are excluded.

    Reads nothing else. In particular it never opens a transcript, so no
    plaintext can reach the summary even by accident.
    """
    resolved_witness = (
        witness_dir
        if witness_dir is not None
        else witness_attestation.resolve_default_witness_dir()
    )
    resolved_cache = cache_dir if cache_dir is not None else resolve_cache_dir(None)

    user_stated, witnessed, by_tag, miss_counts, incidents, superseded = _receipt_side(
        wiki_root, since
    )
    (
        unsigned,
        total,
        anchored,
        anchored_through,
        deficits,
        close_turns,
        close_witnessed,
    ) = _witness_side(resolved_witness, since)
    hook_errors = _hook_error_counts(resolved_cache, since)

    for error_class in sorted(hook_errors):
        incidents.append(
            Incident(
                incident_class=INCIDENT_HOOK_ERROR,
                reason=error_class,
                count=hook_errors[error_class],
            )
        )
    for session in sorted(deficits):
        incidents.append(
            Incident(
                incident_class=INCIDENT_SESSION_CLOSE_DEFICIT,
                reason="eligible-turns-not-witnessed",
                session=session,
                count=deficits[session],
            )
        )

    return ReadoutSummary(
        since=since,
        user_stated_total=user_stated,
        witnessed_total=witnessed,
        witnessed_by_tag=by_tag,
        miss_counts=miss_counts,
        unsigned_lines=unsigned,
        witness_lines_total=total,
        anchored_lines=anchored,
        anchored_through=anchored_through,
        hook_error_counts=hook_errors,
        session_close_deficits=deficits,
        session_close_turns=close_turns,
        session_close_turns_witnessed=close_witnessed,
        superseded_receipt_lines=superseded,
        incidents=tuple(incidents),
    )


def render_text(summary: ReadoutSummary) -> str:
    """Render *summary* as plain text. Every number comes off the summary
    object, so the text and ``--json`` renderings cannot drift apart."""
    lines: list[str] = []
    window = summary.since if summary.since else "all months"
    lines.append(f"witness attest-readout ({window})")
    lines.append(f"  user-stated:      {summary.user_stated_total}")
    lines.append(f"  witnessed:        {summary.witnessed_total}")
    for tag in sorted(summary.witnessed_by_tag):
        lines.append(f"    {tag}: {summary.witnessed_by_tag[tag]}")
    lines.append(f"  misses:           {sum(summary.miss_counts.values())}")
    for reason in sorted(summary.miss_counts):
        lines.append(f"    {reason}: {summary.miss_counts[reason]}")
    coverage = summary.anchor_coverage
    coverage_text = "n/a (no witness lines)" if coverage is None else f"{coverage:.1%}"
    lines.append(f"  unsigned lines:   {summary.unsigned_lines}/{summary.witness_lines_total}")
    lines.append(
        f"  anchor coverage:  {summary.anchored_lines}/{summary.witness_lines_total}"
        f" ({coverage_text})"
    )
    for lid in sorted(summary.anchored_through):
        lines.append(f"    {lid} anchored through seq {summary.anchored_through[lid]}")
    lines.append(f"  hook errors:      {sum(summary.hook_error_counts.values())}")
    for error_class in sorted(summary.hook_error_counts):
        lines.append(f"    {error_class}: {summary.hook_error_counts[error_class]}")
    lines.append(
        f"  session-close:    {summary.session_close_turns} turn(s), "
        f"{summary.session_close_turns_witnessed} witnessed (cumulative snapshot)"
    )
    lines.append(f"  session-close deficit: {summary.session_close_deficit}")
    for session in sorted(summary.session_close_deficits):
        lines.append(f"    {session}: {summary.session_close_deficits[session]}")
    if summary.superseded_receipt_lines:
        lines.append(
            f"  superseded receipt lines (not counted): "
            f"{summary.superseded_receipt_lines}"
        )

    class_counts = summary.incident_class_counts()
    if not summary.incidents:
        lines.append("incidents: none")
    else:
        lines.append(f"incidents: {len(summary.incidents)}")
        for incident_class in INCIDENT_CLASSES:
            if incident_class in class_counts:
                lines.append(f"  {incident_class}: {class_counts[incident_class]}")
        for incident in summary.incidents:
            flag = " LIKELY-FALSE-NEGATIVE" if incident.likely_false_negative else ""
            lines.append(
                f"  INCIDENT {incident.incident_class} reason={incident.reason} "
                f"session={incident.session} witness_ref={incident.witness_ref} "
                f"count={incident.count}{flag}"
            )
    return "\n".join(lines) + "\n"
