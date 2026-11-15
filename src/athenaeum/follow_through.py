# SPDX-License-Identifier: Apache-2.0
"""Follow-through analyzer: did a session later OPEN the pages pushed to it
(issue athenaeum#2301, epic athenaeum#2295).

``push_metrics.determine_references``'s ``used`` signal records citation or
content reproduction — it cannot tell an ignored pointer (the session never
looked at the page again) apart from a page that was read and silently
applied (the session opened it, acted on it, and never quoted it back). This
module answers the narrower question ``determine_references`` cannot: for
each pushed page, was it OPENED afterward — via the MCP ``recall`` or
``read_entity`` tools, or a file read naming the page — and, if so, did that
open happen AFTER the push or before it. **Only an open that comes after the
push counts as following it**; one that precedes it is coincidence, not
follow-through, and is scored exactly like no open at all.

Four per-page classes, computed by joining ``push_metrics.read_push_records``
to the pushing session's own transcript:

- ``opened_after_push``    — opened (recall / read_entity / file read) after
                              the push.
- ``cited_without_opening`` — not opened, but the id appears as a whole
                              token in the session's own AUTHORED text (user
                              or assistant) — never in a tool result.
- ``neither``               — pushed, never opened, never cited.
- ``opened_without_push``  — opened in a session that never received a push
                              for that id (a different id than the one being
                              scored above; reported separately since its
                              denominator is "opens", not "pushes").

**Deliberately NOT ``push_metrics.determine_references``'s ``referenced_ids``
(a Seer finding on this issue's own PR, confirmed real — see below).** That
function's echo fallback — "an id present only in a tool result, with no page
text to judge it against, keeps the historical 'referenced' verdict rather
than a confident 'not used'" — is the right call for ``used``'s own purpose
(never reverse-converting an indeterminate signal into a confident negative),
but it is the WRONG default here: every MCP-pushed page's own delivery IS a
``tool_result`` containing the literal rendered line ``**Uid:** <uid>``
(:func:`athenaeum.mcp_server._recall_via_backend`'s hit-rendering format), so
scanning for that uid in ``echo_blob`` with no page text available (this
module never passes ``wiki_root``) finds it on EVERY push, whether or not the
session ever did anything with it afterward. Calling ``determine_references``
unmodified would therefore report nearly every un-reopened MCP push as
``cited_without_opening`` instead of ``neither`` — exactly the headline-metric
defect this epic exists to avoid. This module instead calls
:func:`athenaeum.push_metrics.split_transcript_text_by_provenance` (the
author/assistant/echo split ``determine_references`` is itself built from,
extracted so this is reuse, not a second parser) and checks ONLY
``author_blob`` — genuine citation, never an echo of the push's own
rendering. The content-reproduction signal (``distinctive_ngram_overlap``
against real page text) is out of scope here regardless, since it needs
``wiki_root``, which this module never passes.

**No model calls.** Every signal is a structural match over ids, tool names
and timestamps already present in the ledger/transcript — the same
free, judge-free posture ``determine_references`` itself documents.

**Reuse, not a second parser (issue athenaeum#2301's own instruction).** This
module calls :func:`athenaeum.push_metrics.read_push_records`,
:func:`athenaeum.push_metrics.split_transcript_text_by_provenance` (extracted
from ``determine_references`` for this module, see above),
:func:`athenaeum.push_metrics._find_session_transcript`,
:func:`athenaeum.push_metrics._parse_ts`,
:func:`athenaeum.push_metrics._appears_as_whole_token`,
:func:`athenaeum.push_metrics.opaque_push_id_from_filename`, and
:func:`athenaeum.transcript_verify._iter_session_records` — the exact
primitives ``determine_references`` itself is built from — rather than
writing a second transcript reader. It never passes a ``wiki_root``, so it
never resolves or reads real page CONTENT (only ids/filenames/timestamps).

**File-read opens are shape-filtered (a second Seer finding, confirmed
real).** A ``Read`` tool call naming an ORDINARY file — ``main.py``,
``README.md``, a config file read for an unrelated reason — is not a page
open: :func:`athenaeum.push_metrics.opaque_push_id_from_filename` returns any
non-matching filename UNCHANGED (by design, for its own caller), so without a
shape check every such read would mint a non-empty "opened id" and inflate
``opened_without_push``. :func:`_looks_like_wiki_page_filename` admits only
the two filename shapes this corpus actually writes — a compiled entity
(:data:`athenaeum.push_metrics._UID_PREFIX_RE` + ``.md``) or a raw auto-memory
intake file (:data:`athenaeum.intake.RAW_FILE_RE`) — before a file read is
ever treated as an open.

**Opened-after-push is compared at SECOND resolution (a third Seer finding,
confirmed real).** :func:`athenaeum.store.now_iso` stamps every push ``ts``
at second precision (no fractional seconds), but a real transcript
``timestamp`` commonly carries milliseconds — so the very ``recall``
tool_use/tool_result pair that DELIVERED a push can parse as a few hundred
milliseconds "after" that push's own truncated ``ts``, even though it is the
SAME event, producing a false ``opened_after_push`` on the push's own
delivery. :func:`_opened_strictly_after` truncates both sides to whole
seconds before comparing, so a same-second event — including the push's own
originating call — never counts as following itself; a genuinely later open
(a different second) is unaffected.

**Segmentation.** Issue athenaeum#2301 asks for two axes:

- *Session length*, bucketed by the session's USER TURNS — user records
  that carry authored content, not a bare ``tool_result`` return and not an
  ``isMeta`` record (:func:`_count_user_turns`; cutoffs
  :data:`SHORT_SESSION_MAX_USER_TURNS`/:data:`MEDIUM_SESSION_MAX_USER_TURNS`
  by default). Total record count was the first basis and failed on real
  data (athenaeum#2608): tool-heavy sessions never fall below dozens of
  records, so nothing landed in "short". User turns separate sessions that
  a human actually drove from one-shot ones. The cutoffs are DOCUMENTED
  defaults and overridable per run — the ``build_follow_through_report``
  arguments ``short_max_user_turns``/``medium_max_user_turns``, the CLI's
  ``--short-max-user-turns``/``--medium-max-user-turns`` — and the report
  echoes the thresholds it used under ``length_thresholds``.
- *Interactive vs headless*, from the ``entrypoint`` field Claude Code
  stamps on transcript records (athenaeum#2608), through the one documented
  table :data:`ENTRYPOINT_CHANNEL_MAP`. This is a **launch-surface proxy**,
  not a measurement of whether a human was attending: ``sdk-cli`` is
  "headless" because that is how the SDK launches, not because the session
  was observed unattended. A session with no ``entrypoint``, an
  unrecognised value, or records that map to different buckets is
  :data:`CHANNEL_UNKNOWN` — never guessed from session length or any other
  signal (issue athenaeum#2301's explicit instruction).
  :func:`_session_channel_bucket` takes the transcript records.

**Privacy (issue athenaeum#2301 AC).** The report this module builds
(:class:`FollowThroughReport`) carries ONLY counts and rates, bucketed by
class/length/channel — never a session id, a page id, page text, or query
text. Every intermediate id (pushed id, opened id) is consumed internally
and discarded once it has contributed to a counter; none is retained on the
returned object. See ``tests/test_follow_through.py``'s
``TestNoIdentifiersInOutput`` for the scan that enforces this over the
serialized JSON.

Layering: L4 domain/pipeline. Imports :mod:`athenaeum.push_metrics` (L3) and
:mod:`athenaeum.transcript_verify` (L1, via the same private primitive
``push_metrics`` itself calls) — same-or-below, per
``tests/test_layer_boundary.py``. Consumed by
:mod:`athenaeum._cmd_push_metrics` (the ``follow-through`` subcommand). Never
imported by either dependency (one-directional).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from athenaeum import push_metrics, transcript_verify
from athenaeum.intake import RAW_FILE_RE

#: Per-page classes that partition every PUSHED id (mutually exclusive, one
#: per (session, id) pair).
CLASS_OPENED_AFTER_PUSH = "opened_after_push"
CLASS_CITED_WITHOUT_OPENING = "cited_without_opening"
CLASS_NEITHER = "neither"
PUSHED_PAGE_CLASSES: tuple[str, ...] = (
    CLASS_OPENED_AFTER_PUSH,
    CLASS_CITED_WITHOUT_OPENING,
    CLASS_NEITHER,
)

#: A fourth, structurally separate class: an id opened in a session that
#: never pushed it. Its denominator is "ids opened", not "ids pushed", so it
#: is tallied and reported alongside, never folded into the three above.
CLASS_OPENED_WITHOUT_PUSH = "opened_without_push"

#: Session-length buckets, by USER TURNS (see module docstring
#: "Segmentation" and :func:`_count_user_turns`). Upper-exclusive: a session
#: with exactly ``SHORT_SESSION_MAX_USER_TURNS`` user turns is ``medium``,
#: not ``short``. These are the DEFAULTS; a run overrides them via
#: :class:`LengthThresholds` (``build_follow_through_report`` args / CLI flags).
SHORT_SESSION_MAX_USER_TURNS = 5
MEDIUM_SESSION_MAX_USER_TURNS = 25
LENGTH_SHORT = "short"
LENGTH_MEDIUM = "medium"
LENGTH_LONG = "long"
LENGTH_BUCKETS: tuple[str, ...] = (LENGTH_SHORT, LENGTH_MEDIUM, LENGTH_LONG)

CHANNEL_INTERACTIVE = "interactive"
CHANNEL_HEADLESS = "headless"
CHANNEL_UNKNOWN = "unknown"
CHANNEL_BUCKETS: tuple[str, ...] = (CHANNEL_INTERACTIVE, CHANNEL_HEADLESS, CHANNEL_UNKNOWN)

#: ``entrypoint`` transcript value -> channel bucket. THE mapping table: a
#: new entrypoint value is a one-line addition here. A LAUNCH-SURFACE PROXY —
#: it records how the session was started, not whether anyone was watching.
#: Any value absent from this table maps to :data:`CHANNEL_UNKNOWN`.
ENTRYPOINT_CHANNEL_MAP: dict[str, str] = {
    "cli": CHANNEL_INTERACTIVE,
    "claude-desktop": CHANNEL_INTERACTIVE,
    "sdk-cli": CHANNEL_HEADLESS,
}

#: MCP tool names (post `name.rsplit("__", 1)[-1]` un-namespacing — the same
#: convention :mod:`athenaeum.session_recovery` already uses for the
#: identical `mcp__<server>__<tool>` shape) that count as "opened the page".
_TOOL_READ_ENTITY = "read_entity"
_TOOL_RECALL = "recall"
#: Built-in file-read tool names, and the `input` keys that carry the path —
#: mirrors :mod:`athenaeum.session_recovery`'s `_PATH_KEYS` subset relevant to
#: a READ (not a write) tool.
_FILE_READ_TOOL_NAMES = frozenset({"Read"})
_FILE_READ_PATH_KEYS: tuple[str, ...] = ("file_path", "path")


@dataclass(frozen=True)
class LengthThresholds:
    """Upper-exclusive user-turn cutoffs for the length buckets."""

    short_max_user_turns: int = SHORT_SESSION_MAX_USER_TURNS
    medium_max_user_turns: int = MEDIUM_SESSION_MAX_USER_TURNS

    def __post_init__(self) -> None:
        if not 0 < self.short_max_user_turns <= self.medium_max_user_turns:
            raise ValueError(
                "length thresholds must satisfy 0 < short_max_user_turns <= "
                f"medium_max_user_turns (got {self.short_max_user_turns}, "
                f"{self.medium_max_user_turns})"
            )


def _count_user_turns(records: Sequence[object]) -> int:
    """Count a session's user turns: user records with authored content.

    Excludes ``isMeta`` records and records whose content is only
    ``tool_result`` blocks (the tool return Claude Code logs as a user
    record), so tool-heavy sessions are not inflated.
    """
    n = 0
    for rec in records:
        if not isinstance(rec, dict) or rec.get("isMeta"):
            continue
        message = rec.get("message")
        role = message.get("role") if isinstance(message, dict) else None
        if (role or rec.get("type")) != "user":
            continue
        content = message.get("content") if isinstance(message, dict) else rec.get("content")
        if isinstance(content, str):
            if content.strip():
                n += 1
        elif isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") != "tool_result" for b in content
        ):
            n += 1
    return n


def _session_length_bucket(user_turns: int, thresholds: LengthThresholds | None = None) -> str:
    """Bucket one session's user-turn count into :data:`LENGTH_BUCKETS`."""
    t = thresholds or LengthThresholds()
    if user_turns < t.short_max_user_turns:
        return LENGTH_SHORT
    if user_turns < t.medium_max_user_turns:
        return LENGTH_MEDIUM
    return LENGTH_LONG


def _session_channel_bucket(records: Sequence[object]) -> str:
    """Interactive/headless/unknown bucket for one session, from the
    ``entrypoint`` field via :data:`ENTRYPOINT_CHANNEL_MAP` — a launch-surface
    proxy (see module docstring "Segmentation").

    :data:`CHANNEL_UNKNOWN` when no record carries a usable ``entrypoint``,
    when any value is not in the table, or when the values map to different
    buckets. **Never derives the bucket from session length or record
    count.**
    """
    buckets: set[str] = set()
    for rec in records:
        if not isinstance(rec, dict):
            continue
        value = rec.get("entrypoint")
        if not isinstance(value, str) or not value.strip():
            continue
        buckets.add(ENTRYPOINT_CHANNEL_MAP.get(value.strip().lower(), CHANNEL_UNKNOWN))
    if len(buckets) == 1:
        return next(iter(buckets))
    return CHANNEL_UNKNOWN


def _bare_tool_name(name: object) -> str:
    """Strip an MCP server namespace prefix (``mcp__<server>__<tool>``).

    Mirrors :mod:`athenaeum.session_recovery`'s identical
    ``name.rsplit("__", 1)[-1]`` rule, so a tool-name match here can never
    diverge from how this codebase already reads a ``tool_use`` block's name
    elsewhere.
    """
    if not isinstance(name, str) or not name:
        return ""
    return name.rsplit("__", 1)[-1]


def _tool_result_block_text(block: dict[str, Any]) -> str:
    """Plain text of one ``tool_result`` block's ``content``."""
    inner = block.get("content")
    if isinstance(inner, str):
        return inner
    if isinstance(inner, list):
        parts: list[str] = []
        for sub in inner:
            if isinstance(sub, dict):
                text = sub.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(sub, str):
                parts.append(sub)
        return "\n".join(parts)
    return ""


def _looks_like_wiki_page_filename(name: str) -> bool:
    """True when *name* matches one of the two page-filename shapes this
    corpus actually writes (Seer finding on issue athenaeum#2301's own PR,
    confirmed real): a compiled entity (``push_metrics._UID_PREFIX_RE``'s
    ``<8-hex>-`` prefix, plus a ``.md`` suffix) or a raw auto-memory intake
    file (:data:`athenaeum.intake.RAW_FILE_RE`,
    ``<YYYYMMDDTHHMMSSZ>-<8-hex>.md``/``.jsonl``).

    Without this check, an ordinary file a session reads for an unrelated
    reason — ``main.py``, ``README.md``, a config file — would still mint a
    non-empty "opened id" via
    :func:`athenaeum.push_metrics.opaque_push_id_from_filename` (which
    returns any non-matching filename UNCHANGED, by design, for its own
    caller) and inflate :data:`CLASS_OPENED_WITHOUT_PUSH`. A file read is
    only ever a page-open SIGNAL for a filename this corpus could plausibly
    have produced.
    """
    if RAW_FILE_RE.match(name):
        return True
    return bool(push_metrics._UID_PREFIX_RE.match(name)) and name.lower().endswith(".md")


def _opened_strictly_after(open_ts: datetime, push_ts: datetime) -> bool:
    """True iff *open_ts* is strictly after *push_ts*, compared at SECOND
    resolution (Seer finding on issue athenaeum#2301's own PR, confirmed
    real).

    :func:`athenaeum.store.now_iso` stamps every push ``ts`` at second
    precision (no fractional seconds) by design, but a real transcript
    ``timestamp`` commonly carries milliseconds. The ``recall``
    tool_use/tool_result pair that DELIVERED a push can therefore parse as a
    few hundred milliseconds "after" that same push's own second-truncated
    ``ts``, even though it is the identical event — a false
    ``opened_after_push`` on the push's own delivery. Truncating both sides
    to whole seconds before comparing removes that artifact; a genuinely
    later open (landing in a different second) is unaffected. A tie (same
    second) is scored like no open at all, matching this module's existing
    "ties/precedes never count as following" discipline.
    """
    return open_ts.replace(microsecond=0) > push_ts.replace(microsecond=0)


def _pushed_ids_by_session(
    push_records: Iterable[dict[str, Any]],
) -> dict[str, dict[str, datetime]]:
    """``session_id -> {pushed_id: EARLIEST push timestamp}``.

    Mirrors the id-collection loop in
    :func:`athenaeum.push_metrics._determine_references_with_reason` (dedup
    by id, first-seen order) but keyed by session and keeping the PARSED
    timestamp rather than discarding it — that timestamp is what lets this
    module tell an open that precedes its push apart from one that follows
    it. A push record with no parseable ``ts``, or an item with no string
    ``id``, contributes nothing (never a fabricated ordering).
    """
    by_session: dict[str, dict[str, datetime]] = {}
    for rec in push_records:
        session_id = rec.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            continue
        ts = push_metrics._parse_ts(rec.get("ts"))
        if ts is None:
            continue
        items = rec.get("items")
        if not isinstance(items, list):
            continue
        bucket = by_session.setdefault(session_id, {})
        for item in items:
            if not isinstance(item, dict):
                continue
            pid = item.get("id")
            if not isinstance(pid, str) or not pid:
                continue
            existing = bucket.get(pid)
            if existing is None or ts < existing:
                bucket[pid] = ts
    return by_session


def _collect_open_events(
    records: Sequence[object], *, known_ids: Iterable[str]
) -> list[tuple[datetime | None, str]]:
    """``[(open_ts_or_None, opened_id), ...]`` for one session's transcript.

    Three signals, in transcript order:

    - ``read_entity`` tool_use -> its ``uid`` input IS the opened id.
    - a file-read tool_use (:data:`_FILE_READ_TOOL_NAMES`) -> IF the read
      path's filename matches a real page shape
      (:func:`_looks_like_wiki_page_filename` — an ordinary source/config
      file is excluded here, never minted into a fake id), the opened id is
      derived from it via
      :func:`athenaeum.push_metrics.opaque_push_id_from_filename` — the
      SAME derivation the sidecar push path already uses, so a push id and
      an opened-file id can never diverge in how they're computed.
    - ``recall`` tool_use, paired with its own ``tool_result`` (matched by
      ``tool_use_id``) -> every id in *known_ids* (plus every id this scan
      has already independently discovered, earlier in the transcript) that
      appears as a whole token (:func:`athenaeum.push_metrics._appears_as_whole_token`)
      in that result's text is scored as re-opened via ``recall``, at the
      ``recall`` call's own timestamp. A ``recall`` result can only ever be
      checked against ids already known by the time it fires — this scan
      never retroactively discovers an id a LATER read_entity/file-read
      reveals, matching ordinary causality.

    *known_ids* seeds the recall-match set; typically the session's own
    pushed ids (``_pushed_ids_by_session``'s per-session bucket).
    """
    events: list[tuple[datetime | None, str]] = []
    known: set[str] = {k for k in known_ids if k}
    pending_recall: dict[str, datetime | None] = {}

    for rec in records:
        if not isinstance(rec, dict):
            continue
        ts = push_metrics._parse_ts(rec.get("timestamp"))
        message = rec.get("message")
        content = message.get("content") if isinstance(message, dict) else rec.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                bare = _bare_tool_name(block.get("name"))
                raw_input = block.get("input")
                tool_input = raw_input if isinstance(raw_input, dict) else {}
                if bare == _TOOL_READ_ENTITY:
                    uid = tool_input.get("uid")
                    if isinstance(uid, str) and uid:
                        events.append((ts, uid))
                        known.add(uid)
                elif bare in _FILE_READ_TOOL_NAMES:
                    path_val = next(
                        (
                            tool_input.get(key)
                            for key in _FILE_READ_PATH_KEYS
                            if isinstance(tool_input.get(key), str) and tool_input.get(key)
                        ),
                        None,
                    )
                    if isinstance(path_val, str) and path_val:
                        read_name = Path(path_val).name
                        if _looks_like_wiki_page_filename(read_name):
                            pid = push_metrics.opaque_push_id_from_filename(read_name)
                            if pid:
                                events.append((ts, pid))
                                known.add(pid)
                elif bare == _TOOL_RECALL:
                    tool_use_id = block.get("id")
                    if isinstance(tool_use_id, str) and tool_use_id:
                        pending_recall[tool_use_id] = ts
            elif btype == "tool_result":
                tool_use_id = block.get("tool_use_id")
                if isinstance(tool_use_id, str) and tool_use_id in pending_recall:
                    call_ts = pending_recall.pop(tool_use_id)
                    text = _tool_result_block_text(block)
                    if text:
                        for pid in sorted(known):
                            if push_metrics._appears_as_whole_token(pid, text):
                                events.append((call_ts, pid))
    return events


@dataclass
class FollowThroughReport:
    """Counts/rates-only report over one (ledger, transcript-root) pair.

    Every field here is a count, a rate, or a list of per-segment
    counts/rates — never a session id, a page id, page text, or query text
    (issue athenaeum#2301 AC). See :meth:`to_dict` for the documented JSON
    shape.
    """

    sessions_considered: int
    sessions_skipped_no_transcript: int
    pushed_page_total: int
    class_counts: dict[str, int]
    opened_without_push_count: int
    segments: list[dict[str, Any]] = field(default_factory=list)
    unknown_channel_sessions: int = 0
    length_thresholds: LengthThresholds = field(default_factory=LengthThresholds)

    def _channel_note(self) -> str:
        note = (
            "channel_bucket is a launch-surface proxy: it maps the transcript "
            "'entrypoint' field (cli, claude-desktop -> interactive; sdk-cli -> "
            "headless), not observed human attendance"
        )
        if self.unknown_channel_sessions:
            note += (
                f"; {self.unknown_channel_sessions} session(s) are 'unknown' "
                "(entrypoint absent, unrecognised, or conflicting)"
            )
        else:
            note += "; no session is 'unknown'"
        return note

    def to_dict(self) -> dict[str, Any]:
        """The documented JSON output (issue athenaeum#2301 AC: "the command
        ... has a documented JSON output").

        ::

            {
              "v": 1,
              "sessions_considered": int,
              "sessions_skipped_no_transcript": int,
              "pushed_page_total": int,
              "class_counts": {"opened_after_push": int,
                                "cited_without_opening": int,
                                "neither": int},
              "class_rates": {<same 3 keys>: float|null},  # count / pushed_page_total
              "opened_without_push_count": int,
              "channel_note": str,  # channel split is a launch-surface proxy
              "length_thresholds": {"short_max_user_turns": int,
                                     "medium_max_user_turns": int},
              "segments": [
                {"length_bucket": "short"|"medium"|"long",
                 "channel_bucket": "interactive"|"headless"|"unknown",
                 "pushed_page_total": int,
                 "class_counts": {...},
                 "class_rates": {...},
                 "opened_without_push_count": int}
              ]
            }

        ``class_rates`` entries are ``null`` (never ``0.0``) when their
        denominator (``pushed_page_total``) is zero — an empty segment never
        reads as "0% opened", the same "absence is not a measured zero"
        discipline :func:`athenaeum.push_metrics.ReferenceResult.precision`
        already applies.
        """
        return {
            "v": 1,
            "sessions_considered": self.sessions_considered,
            "sessions_skipped_no_transcript": self.sessions_skipped_no_transcript,
            "pushed_page_total": self.pushed_page_total,
            "class_counts": dict(self.class_counts),
            "class_rates": _rates(self.class_counts, self.pushed_page_total),
            "opened_without_push_count": self.opened_without_push_count,
            "channel_note": self._channel_note(),
            "length_thresholds": {
                "short_max_user_turns": self.length_thresholds.short_max_user_turns,
                "medium_max_user_turns": self.length_thresholds.medium_max_user_turns,
            },
            "segments": list(self.segments),
        }


def _rates(counts: dict[str, int], denominator: int) -> dict[str, float | None]:
    if denominator <= 0:
        return {cls: None for cls in counts}
    return {cls: counts.get(cls, 0) / denominator for cls in counts}


def build_follow_through_report(
    *,
    cache_dir: Path | None = None,
    projects_root: Path | None = None,
    short_max_user_turns: int = SHORT_SESSION_MAX_USER_TURNS,
    medium_max_user_turns: int = MEDIUM_SESSION_MAX_USER_TURNS,
) -> FollowThroughReport:
    """Build the follow-through report over the push ledger at *cache_dir*
    and the transcripts under *projects_root*.

    Scope (issue athenaeum#2301): only sessions that appear in the push
    ledger are analyzed — the ledger is the only session index available
    here without a full, unbounded scan of *projects_root* (which could
    include sessions wholly unrelated to this instrument). A session with
    zero push records is therefore out of scope; its opens, if any, are
    never counted — not even as :data:`CLASS_OPENED_WITHOUT_PUSH`, which
    is scoped to ids opened in a session that DID push at least one other
    id. This is a stated limitation, not a silent gap.

    *cache_dir* defaults to :func:`athenaeum.config.resolve_cache_dir`'s
    default (via :func:`athenaeum.push_metrics.read_push_records`).
    *projects_root* defaults to
    :func:`athenaeum.transcript_verify.default_projects_root`.

    *short_max_user_turns* / *medium_max_user_turns* override the
    upper-exclusive user-turn cutoffs of the length buckets (see
    :class:`LengthThresholds`); ``ValueError`` unless
    ``0 < short <= medium``.
    """
    thresholds = LengthThresholds(short_max_user_turns, medium_max_user_turns)
    root = (
        projects_root
        if projects_root is not None
        else transcript_verify.default_projects_root()
    )
    push_records = push_metrics.read_push_records(cache_dir)
    by_session = _pushed_ids_by_session(push_records)

    overall_counts: Counter[str] = Counter()
    overall_opened_without_push = 0
    pushed_page_total = 0
    sessions_considered = 0
    sessions_skipped_no_transcript = 0
    unknown_channel_sessions = 0

    segment_counts: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    segment_opened_without_push: dict[tuple[str, str], int] = defaultdict(int)
    segment_pushed_total: dict[tuple[str, str], int] = defaultdict(int)

    for session_id, pushed_at in by_session.items():
        located = push_metrics._find_session_transcript(session_id, root)
        if located is None:
            sessions_skipped_no_transcript += 1
            continue
        scope_dir, _scope_name = located
        records = transcript_verify._iter_session_records(scope_dir, session_id)
        if not records:
            sessions_skipped_no_transcript += 1
            continue
        sessions_considered += 1

        # Issue athenaeum#2301 Seer finding (confirmed real): do NOT call
        # `determine_references` here. Its echo fallback marks a pushed id
        # "referenced" whenever it appears ANYWHERE in a tool result and no
        # page text is available to judge it against — and this module never
        # supplies page text — which means every MCP push's OWN delivery
        # (literally `**Uid:** <uid>` in the push's own tool_result) would
        # satisfy that fallback regardless of real engagement. Citation here
        # is computed directly against `author_blob` only — genuine
        # user/assistant text, never an echo of the push's own rendering.
        author_blob, _assistant_blob, _echo_blob = push_metrics.split_transcript_text_by_provenance(
            records
        )

        length_bucket = _session_length_bucket(_count_user_turns(records), thresholds)
        channel_bucket = _session_channel_bucket(records)
        if channel_bucket == CHANNEL_UNKNOWN:
            unknown_channel_sessions += 1
        seg_key = (length_bucket, channel_bucket)

        open_events = _collect_open_events(records, known_ids=pushed_at.keys())
        opens_by_id: dict[str, list[datetime]] = defaultdict(list)
        for ts, pid in open_events:
            if ts is not None:
                opens_by_id[pid].append(ts)

        for pid, push_ts in pushed_at.items():
            pushed_page_total += 1
            segment_pushed_total[seg_key] += 1
            opened_after = any(
                _opened_strictly_after(ts, push_ts) for ts in opens_by_id.get(pid, ())
            )
            if opened_after:
                cls = CLASS_OPENED_AFTER_PUSH
            elif push_metrics._appears_as_whole_token(pid, author_blob):
                cls = CLASS_CITED_WITHOUT_OPENING
            else:
                cls = CLASS_NEITHER
            overall_counts[cls] += 1
            segment_counts[seg_key][cls] += 1

        for pid in opens_by_id:
            if pid not in pushed_at:
                overall_opened_without_push += 1
                segment_opened_without_push[seg_key] += 1

    segments: list[dict[str, Any]] = []
    for length_bucket in LENGTH_BUCKETS:
        for channel_bucket in CHANNEL_BUCKETS:
            seg_key = (length_bucket, channel_bucket)
            counts = {cls: segment_counts[seg_key].get(cls, 0) for cls in PUSHED_PAGE_CLASSES}
            total = segment_pushed_total.get(seg_key, 0)
            segments.append(
                {
                    "length_bucket": length_bucket,
                    "channel_bucket": channel_bucket,
                    "pushed_page_total": total,
                    "class_counts": counts,
                    "class_rates": _rates(counts, total),
                    "opened_without_push_count": segment_opened_without_push.get(seg_key, 0),
                }
            )

    class_counts = {cls: overall_counts.get(cls, 0) for cls in PUSHED_PAGE_CLASSES}
    return FollowThroughReport(
        sessions_considered=sessions_considered,
        sessions_skipped_no_transcript=sessions_skipped_no_transcript,
        pushed_page_total=pushed_page_total,
        class_counts=class_counts,
        opened_without_push_count=overall_opened_without_push,
        segments=segments,
        unknown_channel_sessions=unknown_channel_sessions,
        length_thresholds=thresholds,
    )
