# SPDX-License-Identifier: Apache-2.0
"""Push-precision + coverage instrumentation (issue athenaeum#711, v6 MVP slice a).

The v6 memory-model epic's definition of done requires push precision to
"improve over a baseline recorded BEFORE any of this ships." That baseline
does not exist yet — nothing today records what ``recall`` pushes into a
session, so there is no way to compute precision (referenced / pushed) or a
coverage-floor miss rate. This module is the FIRST instrument: it ships
before any ranking/tiering change so the pre-change regime gets measured.

Three pieces:

- **Push records** (:func:`record_push`): one row per rendered recall hit,
  written the instant :func:`athenaeum.mcp_server._recall_via_backend`
  assembles the block a session receives. Durable, JSONL, append-only, under
  the cache dir — never inside the wiki corpus, so a push record can never
  become a claim or enter the embedded index. Contains ONLY ids, tiers,
  scopes, counts, and an estimated token cost — no claim content, no personal
  data. Person-entity ids are the opaque ``uid`` frontmatter field, never the
  filename (which embeds a name-derived slug, e.g. ``abc12345-jane-doe.md``).
- **Reference determination** (:func:`determine_references`): at session end,
  scans the ORIGINATING session's transcript (the same read-only surface
  :mod:`athenaeum.transcript_verify` uses) for each pushed id, and writes one
  reference-determination record marking which pushed ids were actually
  referenced afterward. ``precision = referenced / pushed`` per session.
- **Coverage audit** (:func:`build_coverage_worksheet`): samples N sessions'
  push records and emits a worksheet of the STRUCTURAL facts derivable from
  hash-only records — candidate-pool size, tier/scope concentration, and how
  much of a session's window-mate pool a tier/scope filter removes — plus the
  policy-set bounds those facts imply. It does NOT emit a per-candidate
  relevance-marking column or a ``coverage_miss_rate`` figure: push records
  retain only a query HASH (never the raw query text, by athenaeum#711
  design), so nothing recoverable exists to judge whether a candidate was
  actually relevant, and a "miss rate" computed anyway would be a policy-set
  bracket dressed as a measurement, not a measurement. See athenaeum#1036
  (the ruling that withdrew per-candidate marking from this module's scope)
  for the full rationale.

**Why reference-determination needed a small new hook instead of reusing
:mod:`athenaeum.transcript_verify` as-is:** ``verify_user_stated`` and
``classify_backfill_claim`` both answer "did the USER say this claim" (they
only match *user-authored* text, deliberately excluding tool output — see
their docstrings). Reference-determination asks a different question — "did
the CONSUMING SESSION make any use of this pushed id afterward" — which must
also match tool/assistant text quoting the pushed id or page name (an agent
that reads a recalled fact and acts on it never necessarily repeats it back
as if the user said it). Reusing the user-only matcher would systematically
undercount precision. :func:`determine_references` therefore calls the
already-public :func:`athenaeum.transcript_verify._iter_session_records`
(the one-session-one-file read primitive both surfaces share) and applies its
own whole-record text scan — the smallest new logic, not a parallel
transcript reader.

Layering: L3 service. Imports :mod:`athenaeum.config` (cache dir + the
enable/disable accessor) and :mod:`athenaeum.transcript_verify` (read-only
transcript access) at L2/L3. Consumed by :mod:`athenaeum.mcp_server` (write
side, push records), :mod:`athenaeum.librarian` (``session_end``, reference
determination), and :mod:`athenaeum._cmd_push_metrics` (the CLI baseline +
coverage-audit commands). Never imports either back.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from athenaeum.config import resolve_cache_dir
from athenaeum.store import append_line_durable, now_iso

log = logging.getLogger(__name__)

#: Schema version stamped on every push / reference record.
SCHEMA_VERSION = 1

#: Filenames under the cache dir. Never under the wiki/raw corpus (issue
#: athenaeum#711 acceptance: "written to a durable, machine-readable location outside
#: the wiki corpus, so they never become claims and never enter the embedded
#: index") — same discipline as ``spend.jsonl`` / ``detection_incomplete.json``.
PUSH_RECORDS_FILENAME = "_push_records.jsonl"
REFERENCE_RECORDS_FILENAME = "_push_references.jsonl"

#: Char-per-token heuristic, matching the existing convention in
#: ``athenaeum.resolutions`` (``_CHARS_PER_TOKEN = 4``, "roughly 4 chars/token
#: for English markdown"). No tokenizer call is made at push time — the
#: render loop has no LLM in the loop — so this is an ESTIMATE, always
#: reported as such (``token_cost_estimated: true``), never presented as a
#: measured usage figure.
_CHARS_PER_TOKEN = 4

#: Environment variables carrying the consuming Claude Code session's id, in
#: PRECEDENCE order (issue athenaeum#734). Claude Code exports
#: ``CLAUDE_CODE_SESSION_ID`` to the stdio MCP servers it spawns;
#: ``CLAUDE_SESSION_ID`` — the name earlier code (``mcp_server``,
#: ``query_topics``) read — is set by NOTHING in the runtime, so the
#: ``if session_id:`` guard was always false and zero push records were ever
#: written. The older name is kept as a fallback so any environment that does
#: export it keeps working. Asserted explicitly in a test, so reading a name
#: nothing exports becomes a visible diff rather than a silent no-op.
SESSION_ID_ENV_VARS: tuple[str, ...] = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")


def resolve_session_id() -> str:
    """Resolve the consuming session's id from the environment, or ``""``.

    Reads :data:`SESSION_ID_ENV_VARS` in precedence order and returns the first
    non-empty value, else the empty string. This is the ONE place the session-id
    variable name is resolved (issue athenaeum#734): every call site — the
    ``mcp_server`` push-record path and ``query_topics`` spend recording — routes
    through it, so a future rename is a single-line change here rather than a
    silent divergence across sites (the exact defect athenaeum#734 fixes).
    """
    for name in SESSION_ID_ENV_VARS:
        value = os.environ.get(name)
        if value:
            return value
    return ""


def _append_line(path: Path, line: str) -> None:
    """Append one line to *path* durably (``O_APPEND`` + fsync), via
    :func:`athenaeum.store.append_line_durable` — the single shared
    implementation issue athenaeum#980 (S5) collapsed this module's copy onto
    (design note §2.4 / §6.2)."""
    append_line_durable(path, line.encode("utf-8"))


def push_records_path(cache_dir: Path | None = None) -> Path:
    """Resolve the push-records ledger path: ``<cache_dir>/_push_records.jsonl``."""
    return resolve_cache_dir(cache_dir) / PUSH_RECORDS_FILENAME


def durable_push_records_path(wiki_root: Path, *, cache_dir: Path | None = None) -> Path:
    """The R3 ``operational``/``store-durable`` location (design note §5.2
    table row 8; issue athenaeum#980 AC4): ``<wiki_root>/_push_records.jsonl``.

    Same legacy-fallback contract as :func:`athenaeum.spend.durable_ledger_path`:
    an existing installation's populated ``<cache_dir>/_push_records.jsonl``
    keeps resolving there until migrated; a fresh or already-migrated store
    resolves to the new, behind-the-seam location.
    """
    new_path = Path(wiki_root) / PUSH_RECORDS_FILENAME
    legacy_path = push_records_path(cache_dir)
    if new_path.exists() or not legacy_path.exists():
        return new_path
    return legacy_path


def reference_records_path(cache_dir: Path | None = None) -> Path:
    """Resolve the reference-determination ledger path."""
    return resolve_cache_dir(cache_dir) / REFERENCE_RECORDS_FILENAME


# ---------------------------------------------------------------------------
# Redaction — ids only, never content, never a name-derived slug
# ---------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """Char/4 token estimate (see module docstring). Never negative."""
    return max(0, len(text) // _CHARS_PER_TOKEN)


def opaque_push_id(filename: str, fm: dict[str, object] | None) -> str:
    """Return the id to record for one pushed page — NEVER a name-derived slug.

    - When the page carries a frontmatter ``uid`` (every compiled wiki entity
      does — person, project, concept, anything), that opaque hex id is
      returned. This is the ONLY safe id for a person page: the on-disk
      filename is ``<uid>-<slugified-name>.md`` (see
      :meth:`athenaeum.models.WikiEntity.filename`), so using the filename
      would leak a name-derived slug into a supposedly content-free record.
    - When there is no ``uid`` (a raw/auto-memory intake hit, not yet
      compiled into an entity), the indexed *filename* is used instead. Raw
      intake filenames are timestamp+hash (e.g.
      ``20260802T023311Z-3f0ea402.md``), never name-derived, so this fallback
      carries no PII.
    """
    if fm:
        uid = fm.get("uid")
        if isinstance(uid, str) and uid.strip():
            return uid.strip()
    return filename


#: Matches the leading ``<8-hex-uid-prefix>-`` an entity's on-disk filename
#: carries (``athenaeum.models.WikiEntity.filename``: ``<uid>-<slug>.md``).
#: A raw-intake filename (``<timestamp>Z-<hash>.md``, e.g.
#: ``20260802T023311Z-3f0ea402.md``) never matches: its 9th character is
#: ``T``, never ``-``. Mirrors the pre-convergence shell hook's
#: ``_pm_opaque_push_id`` case pattern (issue athenaeum#1343, issue athenaeum#1362).
_UID_PREFIX_RE = re.compile(r"^[0-9a-f]{8}-")


def opaque_push_id_from_filename(filename: str) -> str:
    """Return a PII-safe push id derived from a filename ALONE — no frontmatter.

    For a caller that only has an index row (issue athenaeum#1362's sidecar
    path: the FTS5 ``wiki`` table has no ``uid`` column, so
    :func:`opaque_push_id` — which requires *fm* to find one — is not
    usable there). Reproduces the pre-convergence shell hook's
    ``_pm_opaque_push_id`` behaviour in Python:

    - A compiled entity's filename (``<8-hex-uid-prefix>-<slug>.md``) is
      truncated to just the 8-hex-char uid prefix, so the name-derived slug
      never reaches the ledger.
    - Anything else (a raw-intake filename, which is never name-derived) is
      recorded whole, same as :func:`opaque_push_id`'s no-``uid`` branch.

    Prefer :func:`opaque_push_id` when frontmatter is available (the MCP
    ``recall`` path) — it is exact rather than pattern-matched. This
    function exists only for a caller with no frontmatter to consult.
    """
    if _UID_PREFIX_RE.match(filename):
        return filename[:8]
    return filename


# ---------------------------------------------------------------------------
# Push records
# ---------------------------------------------------------------------------


@dataclass
class PushedItem:
    """One pushed page, as it will appear in a push record's ``items`` list.

    Every field is an id, a classification token, or a count — never content.

    ``tier`` is the ACCESS tier (frontmatter ``access:`` -> ``open``/
    ``internal``), not the retrieval-cost tier — kept as-is for backward
    compatibility with every existing reader of this field. ``memory_tier``
    (issue athenaeum#1345 AC7) is the separate retrieval-cost classification
    (:func:`athenaeum.memory_tiers.resolve_tier` -> ``hot``/``warm``/
    ``cold``/``refused``); additive, defaulted to ``""`` so a pre-existing
    direct construction (e.g. a test fixture) that doesn't pass it keeps
    working unchanged.
    """

    id: str
    tier: str
    scope: str
    token_cost: int
    memory_tier: str = ""


@dataclass
class PushRecord:
    """One push event: everything ``recall`` rendered into one response.

    Fields are exactly the issue athenaeum#711 acceptance list: session id, timestamp,
    the pushed claim/page ids, tier, matched scope, and token cost of the
    pushed block — plus a ``query`` HASH (never the raw query text, which can
    carry PII) so a later reproducibility check can correlate two pushes of
    the same query without storing content.

    ``source`` (issue athenaeum#1362, additive, SCHEMA_VERSION unchanged —
    same precedent as ``PushedItem.memory_tier``): ``"sidecar"`` for a row
    written by :func:`athenaeum.context.record_context_push` (the
    ``athenaeum context`` CLI adapter's unprompted push path); left at its
    default ``""`` for the MCP ``recall`` path, which must keep OMITTING the
    key entirely rather than writing an explicit ``"recall"`` value — every
    row written before this issue has no ``source`` key at all, and the
    documented reader rule (``docs/reference/configuration.md``) is "key absent, or
    any other value, means an explicit ``recall`` push" — changing that
    default would reinterpret every historical row.
    """

    session_id: str
    ts: str
    query_hash: str
    backend: str
    items: list[PushedItem] = field(default_factory=list)
    source: str = ""

    @property
    def total_token_cost(self) -> int:
        return sum(item.token_cost for item in self.items)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "v": SCHEMA_VERSION,
            "session_id": self.session_id,
            "ts": self.ts,
            "query_hash": self.query_hash,
            "backend": self.backend,
            "items": [
                {
                    "id": it.id,
                    "tier": it.tier,
                    "scope": it.scope,
                    "token_cost": it.token_cost,
                    "memory_tier": it.memory_tier,
                }
                for it in self.items
            ],
            "pushed_count": len(self.items),
            "token_cost": self.total_token_cost,
            "token_cost_estimated": True,
        }
        if self.source:
            d["source"] = self.source
        return d


def _query_hash(query: str) -> str:
    """Stable, content-free digest of a query string (never the raw text)."""
    import hashlib

    return hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]


def build_push_record(
    *,
    session_id: str,
    query: str,
    backend: str,
    hits: list[tuple[str, dict[str, object], str]],
    memory_tier_by_filename: dict[str, str] | None = None,
) -> PushRecord:
    """Build a :class:`PushRecord` from rendered recall hits.

    Args:
        session_id: the consuming session's id (``CLAUDE_CODE_SESSION_ID``, or
            the MCP caller's session id when available; resolved via
            :func:`resolve_session_id`). Never empty-string silently accepted
            by the writer — see :func:`record_push`.
        query: the raw recall query text. Only its hash is retained.
        backend: the search backend name actually used (``keyword`` /
            ``fts5`` / ``vector``) — the retrieval mechanism tier.
        hits: ``(filename, fm, snippet_text)`` for each hit actually
            RENDERED into the response (post Layer-C authorization / policy
            filtering — a hit dropped before rendering was never pushed).
            ``snippet_text`` is used ONLY to size the token-cost estimate; it
            is never retained on the record.
        memory_tier_by_filename: optional ``{filename: resolved memory_tier}``
            map (issue athenaeum#1345 AC7) — the retrieval-cost classification
            (``hot``/``warm``/``cold``/``refused``), distinct from ``tier``
            below (the ACCESS tier). A filename absent from the map (or the
            map itself being ``None``) records ``""`` (additive default,
            never a fabricated guess).

            **Deliberately a caller-supplied map, not a same-module
            :func:`athenaeum.memory_tiers.resolve_tier` call**, even though
            that would read more directly as "populate from frontmatter
            here": :mod:`athenaeum.memory_tiers` already imports FROM this
            module (``opaque_push_id``, used by ``run_tier_sweep``) and from
            :mod:`athenaeum.usage_report` (which itself imports FROM this
            module) — so a same-module import of ``athenaeum.memory_tiers``
            would close a 3-node cycle ``{memory_tiers, push_metrics,
            usage_report}`` that ``tests/test_import_graph_acyclic.py``
            hard-fails on (the allowed-SCC baseline has been pinned empty
            since issue athenaeum#640; ANY new cycle is a regression).
            Verified empirically while implementing this issue. The one
            production caller (``athenaeum.mcp_server._recall_via_backend``)
            already computes ``memory_tiers.resolve_tier(fm, config=config)``
            per hit (issue athenaeum#718, unconditionally, before this
            function is ever called) — reusing that already-resolved value
            here is strictly cheaper than a second call, not just
            cycle-avoiding.
    """
    tier_map = memory_tier_by_filename or {}
    items: list[PushedItem] = []
    for filename, fm, snippet_text in hits:
        pid = opaque_push_id(filename, fm)
        access = fm.get("access") if fm else None
        tier = str(access).strip() if isinstance(access, str) and access.strip() else "internal"
        scope = "owner"
        roles = fm.get("audience") if fm else None
        if isinstance(roles, list) and roles:
            scope = ",".join(sorted(str(r) for r in roles if r))
        elif tier == "open":
            scope = "open"
        items.append(
            PushedItem(
                id=pid,
                tier=tier,
                scope=scope,
                token_cost=estimate_tokens(snippet_text),
                memory_tier=tier_map.get(filename, ""),
            )
        )
    return PushRecord(
        session_id=session_id,
        ts=now_iso(),
        query_hash=_query_hash(query),
        backend=backend,
        items=items,
    )


def record_push(
    record: PushRecord,
    *,
    cache_dir: Path | None = None,
    wiki_root: Path | None = None,
    config: dict[str, Any] | None = None,
) -> bool:
    """Append one push record to the durable ledger. Best-effort.

    No-ops (returns ``False``) when instrumentation is disabled
    (:func:`athenaeum.config.resolve_push_metrics_enabled`) or the record has
    no session id or no pushed items (nothing to measure). Every failure is
    swallowed and logged at warning level — a ledger write must NEVER break
    or slow the live recall path, but a silent failure here would produce the
    same "reads as zero forever" hazard athenaeum#568 fixed for the spend ledger.

    *wiki_root*, when supplied, resolves the ledger behind the seam (issue
    athenaeum#980 AC4) via :func:`durable_push_records_path`; omitted,
    resolution is unchanged from before that issue.
    """
    try:
        from athenaeum.config import resolve_push_metrics_enabled

        if not resolve_push_metrics_enabled(config):
            return False
        if not record.session_id or not record.items:
            return False
        path = (
            durable_push_records_path(wiki_root, cache_dir=cache_dir)
            if wiki_root is not None
            else push_records_path(cache_dir)
        )
        _append_line(path, json.dumps(record.to_dict(), separators=(",", ":")) + "\n")
        return True
    except Exception as exc:  # noqa: BLE001 — must never break recall
        log.warning(
            "push-metrics record write FAILED (%s): %s — this session's push "
            "will be invisible to the baseline/coverage commands",
            type(exc).__name__,
            exc,
        )
        return False


def read_push_records(
    cache_dir: Path | None = None, *, wiki_root: Path | None = None
) -> list[dict[str, Any]]:
    """Read every push record. Tolerates a torn trailing line; never raises.

    *wiki_root*, when supplied, resolves via :func:`durable_push_records_path`
    (issue athenaeum#980 AC4) — the SAME resolution :func:`record_push` uses, so a
    read against a given store always finds exactly what the matching write
    produced (never split across two locations). Omitted, resolution is
    unchanged from before that issue.
    """
    path = (
        durable_push_records_path(wiki_root, cache_dir=cache_dir)
        if wiki_root is not None
        else push_records_path(cache_dir)
    )
    return _read_jsonl(path)


def read_reference_records(cache_dir: Path | None = None) -> list[dict[str, Any]]:
    """Read every reference-determination record. Tolerates a torn trailing
    line; never raises. Public counterpart to :func:`read_push_records`
    (issue athenaeum#968): both are the sanctioned way to read these ledgers —
    :mod:`athenaeum.usage_report` and :mod:`athenaeum.ingestion_gate` use
    this instead of reaching for the private :func:`_read_jsonl` helper.
    """
    return _read_jsonl(reference_records_path(cache_dir))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


# ---------------------------------------------------------------------------
# Reference determination (session end)
# ---------------------------------------------------------------------------


@dataclass
class ReferenceResult:
    """Per-session reference-determination outcome."""

    session_id: str
    ts: str
    pushed_ids: list[str]
    referenced_ids: list[str]

    @property
    def precision(self) -> float | None:
        """``referenced / pushed``, or ``None`` when nothing was pushed."""
        if not self.pushed_ids:
            return None
        return len(self.referenced_ids) / len(self.pushed_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": SCHEMA_VERSION,
            "session_id": self.session_id,
            "ts": self.ts,
            "pushed_count": len(self.pushed_ids),
            "referenced_count": len(self.referenced_ids),
            "referenced_ids": sorted(self.referenced_ids),
            "precision": self.precision,
        }


def _find_session_transcript(
    session_id: str, projects_root: Path
) -> tuple[Path, str] | None:
    """Locate ``<projects_root>/<scope>/<session_id>.jsonl`` by scanning scopes.

    Push records carry only a session id (mirroring ``CLAUDE_CODE_SESSION_ID``,
    the ambient identifier the rest of athenaeum already keys telemetry on —
    see ``query_topics.py`` and ``docs/reference/configuration.md`` "Ambient telemetry
    variable"), never the Claude Code project-path-hash "scope" directory
    name. There is no existing session-id -> scope registry anywhere in this
    codebase (``transcript_verify.verify_user_stated`` always takes scope as
    a caller-supplied argument), so this scans each scope directory for the
    one file named ``<session_id>.jsonl``. Session ids are UUIDs — a
    collision across two scopes is not a realistic concern. Returns
    ``(scope_dir, scope_name)`` for the first match, or ``None``.
    """
    if not projects_root.is_dir():
        return None
    target = f"{session_id}.jsonl"
    try:
        scopes = sorted(p for p in projects_root.iterdir() if p.is_dir())
    except OSError:
        return None
    for scope_dir in scopes:
        if (scope_dir / target).is_file():
            return scope_dir, scope_dir.name
    return None


def determine_references(
    session_id: str,
    *,
    cache_dir: Path | None = None,
    projects_root: Path | None = None,
    wiki_root: Path | None = None,
) -> ReferenceResult | None:
    """Determine which of *session_id*'s pushed ids were referenced afterward.

    Reads this session's push records (from the ledger) and its transcript
    (read-only, via the same one-session-one-file primitive
    ``transcript_verify`` uses), then marks a pushed id "referenced" when it
    appears — as a whole-token substring match — anywhere in ANY transcript
    record's text (user, assistant, or tool-result), not just user-authored
    text. See the module docstring for why this must differ from
    ``verify_user_stated``.

    Returns ``None`` when there are no push records for this session (nothing
    to determine) or the transcript cannot be located (rolled off / never
    existed — an honest "cannot determine", never a fabricated 0 or 1).

    *wiki_root* (issue athenaeum#980 AC4): forwarded to :func:`read_push_records`.
    """
    from athenaeum.transcript_verify import _iter_session_records, default_projects_root

    records = [
        r
        for r in read_push_records(cache_dir, wiki_root=wiki_root)
        if r.get("session_id") == session_id
    ]
    if not records:
        return None

    pushed_ids: list[str] = []
    for rec in records:
        for item in rec.get("items", []):
            pid = item.get("id")
            if isinstance(pid, str) and pid and pid not in pushed_ids:
                pushed_ids.append(pid)
    if not pushed_ids:
        return None

    root = projects_root if projects_root is not None else default_projects_root()
    located = _find_session_transcript(session_id, root)
    if located is None:
        return None
    scope_dir, _scope_name = located

    transcript_records = _iter_session_records(scope_dir, session_id)
    if not transcript_records:
        return None

    haystacks: list[str] = []
    for trec in transcript_records:
        if not isinstance(trec, dict):
            continue
        message = trec.get("message")
        content = message.get("content") if isinstance(message, dict) else trec.get("content")
        if isinstance(content, str):
            haystacks.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    txt = block.get("text") or block.get("content")
                    if isinstance(txt, str):
                        haystacks.append(txt)
                    inner = block.get("content")
                    if isinstance(inner, list):
                        for sub in inner:
                            if isinstance(sub, dict):
                                t = sub.get("text")
                                if isinstance(t, str):
                                    haystacks.append(t)
                elif isinstance(block, str):
                    haystacks.append(block)
        tur = trec.get("toolUseResult")
        if isinstance(tur, str):
            haystacks.append(tur)
        elif isinstance(tur, dict):
            for key in ("stdout", "text", "content"):
                v = tur.get(key)
                if isinstance(v, str):
                    haystacks.append(v)
    blob = "\n".join(haystacks)

    referenced = [pid for pid in pushed_ids if pid in blob]
    return ReferenceResult(
        session_id=session_id,
        ts=now_iso(),
        pushed_ids=pushed_ids,
        referenced_ids=referenced,
    )


def record_reference_result(
    result: ReferenceResult,
    *,
    cache_dir: Path | None = None,
) -> bool:
    """Append one reference-determination record. Best-effort, never raises."""
    try:
        path = reference_records_path(cache_dir)
        _append_line(path, json.dumps(result.to_dict(), separators=(",", ":")) + "\n")
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "push-metrics reference-determination write FAILED (%s): %s",
            type(exc).__name__,
            exc,
        )
        return False


def run_reference_determination(
    session_id: str,
    *,
    cache_dir: Path | None = None,
    projects_root: Path | None = None,
    config: dict[str, Any] | None = None,
    wiki_root: Path | None = None,
) -> ReferenceResult | None:
    """Determine + durably record one session's reference outcome. Best-effort.

    The single entry point :func:`athenaeum.librarian.session_end` calls.
    Returns ``None`` (no-op, nothing written) when instrumentation is
    disabled, there is no session id, or :func:`determine_references` itself
    returns ``None``. Never raises — a reference-determination failure must
    not break ``session_end``.

    *wiki_root* (issue athenaeum#980 AC4): forwarded to :func:`determine_references`
    for the push-records read half only — the reference-determination WRITE
    below stays under *cache_dir* (``_push_references.jsonl`` is not one of
    the artifacts §5.2's table names for relocation).
    """
    try:
        from athenaeum.config import resolve_push_metrics_enabled

        if not resolve_push_metrics_enabled(config):
            return None
        if not session_id:
            return None
        result = determine_references(
            session_id, cache_dir=cache_dir, projects_root=projects_root, wiki_root=wiki_root
        )
        if result is None:
            return None
        record_reference_result(result, cache_dir=cache_dir)
        return result
    except Exception as exc:  # noqa: BLE001 — must never break session_end
        log.warning(
            "push-metrics reference-determination FAILED (%s): %s",
            type(exc).__name__,
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# Baseline (precision + coverage over a window)
# ---------------------------------------------------------------------------


@dataclass
class BaselineWindow:
    """Precision + coverage figures computed over a stated window.

    ``precision`` is ``None`` when the window has zero reference-determination
    records — the honest state before ANY session has run with instrumentation
    on, never a fabricated ``0.0`` or ``1.0``.

    ``excluded_sessions``/``excluded_push_record_count``/
    ``excluded_reference_record_count`` (issue athenaeum#791) report the
    operator-supplied ``exclude_sessions`` denylist's effect: which of the
    requested session ids actually had records inside this window, and how
    many push/reference records those sessions contributed. Always present
    (empty/zero when no exclusion was requested) so a contaminated window
    that WAS cleaned is visible in the output, not indistinguishable from a
    window that was never contaminated.
    """

    start: str
    end: str
    session_count: int
    push_record_count: int
    reference_record_count: int
    precision: float | None
    athenaeum_version: str
    git_sha: str
    excluded_sessions: tuple[str, ...] = ()
    excluded_push_record_count: int = 0
    excluded_reference_record_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "window": {"start": self.start, "end": self.end},
            "sessions": self.session_count,
            "push_records": self.push_record_count,
            "reference_records": self.reference_record_count,
            "precision": self.precision,
            "athenaeum_version": self.athenaeum_version,
            "git_sha": self.git_sha,
            "excluded_sessions": list(self.excluded_sessions),
            "excluded_push_records": self.excluded_push_record_count,
            "excluded_reference_records": self.excluded_reference_record_count,
        }


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _get_version() -> str:
    from athenaeum import __version__

    return __version__


def _get_git_sha(repo_root: Path | None = None) -> str:
    """Best-effort short git SHA of the running checkout. ``"unknown"`` if none."""
    import subprocess

    cwd = repo_root if repo_root is not None else Path(__file__).resolve().parents[2]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        sha = out.stdout.strip()
        return sha if sha else "unknown"
    except Exception:  # noqa: BLE001 — best-effort, never break the baseline run
        return "unknown"


def _resolve_exclude_sessions(
    requested: Iterable[str], known_session_ids: Iterable[str]
) -> set[str]:
    """Resolve operator-supplied ``--exclude-session`` values to full session ids.

    Issue athenaeum#987: a bare exact-match denylist let a session-id PREFIX
    (`d5774338-7d8b` instead of the full `d5774338-7d8b-4152-a252-248d156f95ef`)
    match nothing and still exit 0 — a contaminated baseline published as if
    filtered. Each *requested* value must resolve to exactly one entry in
    *known_session_ids*:

    - an exact match, resolved as-is; else
    - an unambiguous prefix (exactly one known id starts with it), resolved
      to that id; else
    - a hard error — ``ValueError`` — when the value matches zero known ids
      (the athenaeum#987 silent-no-op case) or more than one (ambiguous:
      resolving it would silently pick a session the operator didn't name).

    Silent zero-match success is impossible: every requested value either
    resolves to exactly one real session or raises.
    """
    known = sorted({s for s in known_session_ids if s})
    resolved: set[str] = set()
    for value in requested:
        if not value:
            continue
        if value in known:
            resolved.add(value)
            continue
        matches = [sid for sid in known if sid.startswith(value)]
        if len(matches) == 1:
            resolved.add(matches[0])
        elif len(matches) == 0:
            raise ValueError(
                f"--exclude-session {value!r} matches no known session id "
                "(pass the full id or an unambiguous prefix)"
            )
        else:
            preview = ", ".join(matches[:5])
            if len(matches) > 5:
                preview += f", +{len(matches) - 5} more"
            raise ValueError(
                f"--exclude-session {value!r} is ambiguous: matches "
                f"{len(matches)} known session ids ({preview})"
            )
    return resolved


def compute_baseline(
    *,
    since: datetime | None = None,
    cache_dir: Path | None = None,
    repo_root: Path | None = None,
    exclude_sessions: Iterable[str] | None = None,
    wiki_root: Path | None = None,
) -> BaselineWindow:
    """Compute the push-precision baseline over ``[since, now]``.

    ``since=None`` means "the whole ledger" — the honest window when
    instrumentation has been on since it first shipped and no operator narrower
    window was requested. Never estimates a pre-instrumentation figure: a
    push record cannot exist before this instrument shipped, so a window that
    predates the ledger's first record simply contains zero records — reported
    truthfully, not backfilled.

    ``exclude_sessions`` (issue athenaeum#791): an explicit, operator-supplied
    denylist of KNOWN-synthetic session ids — e.g. a session id an operator
    has confirmed ran the test suite and leaked fixture pushes into the live
    ledger (the athenaeum#791 evidence: 75 of 120 push records at filing, all
    from one such session). Deliberately a denylist, not a heuristic: nothing
    here infers "synthetic" from record CONTENT (a push id being a bare
    filename rather than a uid is also the normal shape for a legitimate
    raw-intake page — see :func:`opaque_push_id` — so guessing from that shape
    would risk excluding real data). Excluded sessions' push and
    reference-determination records are dropped from ``session_count``,
    ``push_record_count``, ``reference_record_count``, and ``precision`` —
    but never silently: the excluded session ids and record counts are always
    on the returned :class:`BaselineWindow`, so a cleaned window still shows
    that it needed cleaning.

    Each requested value is resolved via :func:`_resolve_exclude_sessions`
    against every session id in the FULL ledger (push and reference records,
    not just the ``[since, now]`` window — a session that's real but simply
    has no records in this window is a legitimate no-op, not a typo). It may
    be the full session id or an unambiguous prefix of exactly one known id
    (issue athenaeum#987); a value matching zero or more-than-one known
    session ids raises ``ValueError`` rather than silently excluding nothing.

    *wiki_root* (issue athenaeum#980 AC4): forwarded to :func:`read_push_records`
    for the push-records half of this window. The reference-determination
    ledger (``_push_references.jsonl``) is a separate artifact §5.2's table
    does not name, so it keeps resolving under *cache_dir* unchanged.
    """
    now = datetime.now(tz=timezone.utc)
    pushes = read_push_records(cache_dir, wiki_root=wiki_root)
    refs = _read_jsonl(reference_records_path(cache_dir))
    known_session_ids = {
        sid for r in (pushes + refs) if isinstance(sid := r.get("session_id"), str) and sid
    }
    exclude_set = _resolve_exclude_sessions(exclude_sessions or (), known_session_ids)

    def _in_window(ts_raw: Any) -> bool:
        ts = _parse_ts(ts_raw)
        if ts is None:
            return False
        if since is not None and ts < since:
            return False
        return True

    pushes_in = [r for r in pushes if _in_window(r.get("ts"))]
    refs_in = [r for r in refs if _in_window(r.get("ts"))]

    excluded_pushes = [r for r in pushes_in if r.get("session_id") in exclude_set]
    excluded_refs = [r for r in refs_in if r.get("session_id") in exclude_set]
    if exclude_set:
        pushes_in = [r for r in pushes_in if r.get("session_id") not in exclude_set]
        refs_in = [r for r in refs_in if r.get("session_id") not in exclude_set]

    found_excluded_sessions = sorted(
        {
            sid
            for r in (excluded_pushes + excluded_refs)
            if isinstance(sid := r.get("session_id"), str) and sid
        }
    )

    sessions = {r.get("session_id") for r in pushes_in if r.get("session_id")}

    precision: float | None = None
    if refs_in:
        total_pushed = sum(int(r.get("pushed_count", 0) or 0) for r in refs_in)
        total_referenced = sum(int(r.get("referenced_count", 0) or 0) for r in refs_in)
        precision = (total_referenced / total_pushed) if total_pushed else None

    if since is not None:
        start_str = now_iso(since)
    else:
        start_str = "(instrument-enabled)"
    return BaselineWindow(
        start=start_str,
        end=now_iso(now),
        session_count=len(sessions),
        push_record_count=len(pushes_in),
        reference_record_count=len(refs_in),
        precision=precision,
        athenaeum_version=_get_version(),
        git_sha=_get_git_sha(repo_root),
        excluded_sessions=tuple(found_excluded_sessions),
        excluded_push_record_count=len(excluded_pushes),
        excluded_reference_record_count=len(excluded_refs),
    )


_SNAPSHOT_HEADER = """# Memory model measurements

Durable home for v6 (dimensional memory model) measurement artifacts.
Each `##` section is produced by one epic child issue and states, inline,
the reproducible command that generated it. This file is committed —
`docs/memory-model.md` (the design lock) is never touched by any command
that writes here.
"""

_SNAPSHOT_SECTION_HEADING = "## Push-precision and coverage baseline"


def render_snapshot_section(baseline: BaselineWindow, *, coverage_note: str) -> str:
    """Render one dated ``## Push-precision and coverage baseline`` entry.

    Machine-readable: every field the issue's acceptance criteria name
    (window start/end, #sessions, #push records, precision point estimate,
    coverage miss rate, athenaeum version + git SHA) appears as a `key:
    value` line so a later agent can parse this without an LLM.

    ``excluded_sessions``/``excluded_push_records``/``excluded_reference_records``
    (issue athenaeum#791) always appear — ``none``/``0`` when no
    ``--exclude-session`` was passed — so a snapshot that DID need to drop a
    known-synthetic session is visibly different from one that never had to,
    rather than the exclusion being invisible in the committed history.
    """
    precision_str = (
        f"{baseline.precision:.4f}"
        if baseline.precision is not None
        else "n/a — accrues as sessions run"
    )
    excluded_sessions_str = (
        ",".join(baseline.excluded_sessions) if baseline.excluded_sessions else "none"
    )
    lines = [
        _SNAPSHOT_SECTION_HEADING,
        "",
        f"### Snapshot {baseline.end}",
        "",
        "Reproduce with: `athenaeum push-metrics baseline`",
        "",
        f"- window_start: {baseline.start}",
        f"- window_end: {baseline.end}",
        f"- sessions: {baseline.session_count}",
        f"- push_records: {baseline.push_record_count}",
        f"- reference_records: {baseline.reference_record_count}",
        f"- precision: {precision_str}",
        f"- coverage_miss_rate: {coverage_note}",
        f"- excluded_sessions: {excluded_sessions_str}",
        f"- excluded_push_records: {baseline.excluded_push_record_count}",
        f"- excluded_reference_records: {baseline.excluded_reference_record_count}",
        f"- athenaeum_version: {baseline.athenaeum_version}",
        f"- git_sha: {baseline.git_sha}",
        "",
    ]
    return "\n".join(lines)


_COVERAGE_PENDING_NOTE = (
    "not measurable — push records retain only a query hash by design "
    "(athenaeum#711), so relevance is not recoverable and no miss rate can "
    "be computed; `athenaeum push-metrics coverage-audit` reports the "
    "structural facts that ARE derivable (candidate-pool size, tier/scope "
    "concentration, window-mate filter removal) plus policy-set bounds, "
    "never a measured rate (athenaeum#1036)"
)


def write_snapshot(
    baseline: BaselineWindow,
    *,
    docs_path: Path,
    coverage_note: str = _COVERAGE_PENDING_NOTE,
) -> Path:
    """Idempotently write/append the dated snapshot to *docs_path*.

    - File absent: write the header + one ``## Push-precision and coverage
      baseline`` section containing this one dated snapshot.
    - File present, no existing baseline section: append the section at EOF.
    - File present, baseline section exists: append a new dated ``###
      Snapshot <timestamp>`` sub-entry inside the existing section (never
      replaces an earlier snapshot, never corrupts the file — each run adds
      one dated entry, re-running is always safe).

    Uses :func:`athenaeum.atomic_io.atomic_write_text` for the whole-file
    replace so a crash mid-write can never leave a torn file.

    Raises:
        ValueError: when *baseline* has zero reference-determination records
            (``reference_record_count == 0``) — precision is not computable,
            so the only thing there is to write is a placeholder. Issue
            athenaeum#795: a prior version wrote that placeholder
            unconditionally, and a run against a dead instrument (zero
            reference records) silently appended a meaningless dated entry
            into a tracked docs file. Writing nothing is the correct outcome
            here; nothing is written before this is raised. Callers that
            only want to INSPECT a baseline (valid or not) without writing —
            the exact read-only check the athenaeum#711 incident needed —
            should not call this function at all; see the CLI's
            ``--dry-run``.
    """
    if baseline.reference_record_count == 0:
        raise ValueError(
            "refusing to write snapshot: reference_records=0 in this window, "
            "so precision is not computable — nothing meaningful to record "
            "(athenaeum#795)"
        )

    from athenaeum.atomic_io import atomic_write_text

    new_entry = render_snapshot_section(baseline, coverage_note=coverage_note)

    if not docs_path.is_file():
        content = _SNAPSHOT_HEADER + "\n" + new_entry
        atomic_write_text(docs_path, content)
        return docs_path

    existing = docs_path.read_text(encoding="utf-8")
    if _SNAPSHOT_SECTION_HEADING not in existing:
        sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
        content = existing + sep + new_entry
        atomic_write_text(docs_path, content)
        return docs_path

    # Section exists: append just the dated sub-entry (skip the repeated
    # section heading) right after the existing section's heading line so
    # snapshots accumulate newest-appended, oldest-first is unnecessary —
    # order of appearance simply mirrors run order.
    sub_entry_lines = new_entry.splitlines()[1:]  # drop the "## ..." heading
    sub_entry = "\n".join(sub_entry_lines).lstrip("\n")
    heading_idx = existing.index(_SNAPSHOT_SECTION_HEADING)
    insert_at = heading_idx + len(_SNAPSHOT_SECTION_HEADING)
    content = existing[:insert_at] + "\n\n" + sub_entry + existing[insert_at:]
    atomic_write_text(docs_path, content)
    return docs_path


# ---------------------------------------------------------------------------
# Coverage audit worksheet
# ---------------------------------------------------------------------------


def sample_sessions(
    cache_dir: Path | None,
    n: int,
    *,
    seed: int | None = None,
    exclude_sessions: Iterable[str] | None = None,
    wiki_root: Path | None = None,
) -> list[str]:
    """Return up to *n* distinct session ids from the push-records ledger.

    Deterministic when *seed* is given (test seam); otherwise a fresh random
    sample each call. Sessions are sampled, not the raw record list, so a
    chatty session's many pushes don't crowd out the sample.

    ``exclude_sessions`` (issue athenaeum#986, same denylist semantics as
    :func:`compute_baseline`'s ``exclude_sessions`` — athenaeum#791): known-
    synthetic session ids are removed from the sampling pool entirely, so an
    excluded session can never be drawn into the sample.

    *wiki_root* (issue athenaeum#980 AC4): forwarded to :func:`read_push_records`.
    """
    exclude_set = {s for s in (exclude_sessions or ()) if s}
    records = read_push_records(cache_dir, wiki_root=wiki_root)
    sessions = sorted(
        {
            sid
            for r in records
            if isinstance(sid := r.get("session_id"), str) and sid and sid not in exclude_set
        }
    )
    if len(sessions) <= n:
        return sessions
    rng = random.Random(seed)
    return sorted(rng.sample(sessions, n))


#: Opens every worksheet (issue athenaeum#1036 AC3): states, in plain
#: language, what this artifact cannot establish and why — so a reader of
#: the worksheet file carries the caveat without needing to know this issue
#: exists.
LIMITATION_STATEMENT = (
    "This worksheet cannot establish a measured coverage-floor miss rate. "
    "Push records store a query HASH, never the raw query text — a "
    "deliberate design decision (athenaeum#711) that keeps free-text "
    "content out of the ledger. Without the query, there is no way to "
    "re-run a session's search against the live index or ask a human "
    "reviewer whether a specific unpushed candidate was actually relevant "
    "to what that session was looking for — so no per-candidate relevance "
    "marking is possible, and no true miss rate can be recovered from these "
    "records. What follows are the structural facts that ARE derivable from "
    "hash-only records — candidate-pool size, tier/scope concentration, and "
    "how much of a session's window-mate pool a tier/scope filter removes — "
    "plus the policy-set bounds those facts imply. Every bound below is "
    "labelled policy-set, never measured (athenaeum#1036)."
)


def _tier_scope_concentration(items: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Structural fact: how concentrated the pushed set is in one tier/scope
    pairing (issue athenaeum#1036 AC2).

    ``degenerate`` is ``True`` when the sample contains at most one distinct
    tier/scope pairing — the case where a window-mate filter keyed on
    tier/scope pairing cannot discriminate at all (every window-mate item
    trivially shares the one pairing present), so any filter-removal figure
    computed over it would look like ordinary filter behaviour while
    actually measuring nothing. Callers must report this flag rather than
    silently averaging over it (athenaeum#1036 AC5).
    """
    counts: Counter[tuple[str, str]] = Counter()
    for item in items:
        counts[(str(item.get("tier", "")), str(item.get("scope", "")))] += 1
    total = sum(counts.values())
    if total == 0:
        return {
            "total_pushed_items": 0,
            "top_pairing": None,
            "top_pairing_item_count": 0,
            "share_of_pushed_items": None,
            "distinct_pairing_count": 0,
            "degenerate": False,
            "degenerate_note": None,
        }
    (top_tier, top_scope), top_count = counts.most_common(1)[0]
    distinct = len(counts)
    degenerate = distinct <= 1
    return {
        "total_pushed_items": total,
        "top_pairing": {"tier": top_tier, "scope": top_scope},
        "top_pairing_item_count": top_count,
        "share_of_pushed_items": top_count / total,
        "distinct_pairing_count": distinct,
        "degenerate": degenerate,
        "degenerate_note": (
            "every pushed item in this sample shares a single tier/scope "
            "pairing — the window-mate filter cannot discriminate on "
            "tier/scope here, so any filter-removal figure reported "
            "alongside this is an artifact of the degenerate distribution, "
            "not evidence of filter behaviour"
            if degenerate
            else None
        ),
    }


def _policy_set_bounds(pushed_count: int, candidate_pool_size: int) -> dict[str, Any]:
    """Policy-set endpoints of the miss-rate interval this design implies —
    explicitly labelled as policy-set, never measured, both endpoints named
    (issue athenaeum#1036 AC2).
    """
    denom = pushed_count + candidate_pool_size
    upper = (candidate_pool_size / denom) if denom else None
    return {
        "lower_bound": 0.0,
        "lower_bound_label": "none of the candidate-pool ids were relevant-missed",
        "upper_bound": upper,
        "upper_bound_label": (
            "every candidate-pool id was relevant-missed"
            if upper is not None
            else "undefined — pushed_count and candidate_pool_size are both 0"
        ),
        "note": "policy-set, not measured — see the worksheet's top-level 'limitation' field",
    }


def build_coverage_worksheet(
    *,
    n: int,
    wiki_root: Path,
    cache_dir: Path | None = None,
    seed: int | None = None,
    search_backend: str = "fts5",
    exclude_sessions: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Build the coverage-audit worksheet payload for *n* sampled sessions.

    Issue athenaeum#1036 (operator ruling, option (c)): this worksheet
    reports STRUCTURAL facts derivable from hash-only push records — it does
    NOT emit a per-candidate relevance-marking column or a
    ``coverage_miss_rate`` figure. Push records retain only a query HASH,
    never the raw query text (deliberate athenaeum#711 design), so nothing
    recoverable exists to judge whether a candidate page was relevant to the
    session that didn't push it. See :data:`LIMITATION_STATEMENT` (also the
    worksheet's own ``limitation`` field) for the full statement.

    Per sampled session, the candidate pool is the set of pages pushed to
    ANY OTHER session **in the sample window** (issue athenaeum#986 — the
    other ``n - 1`` sampled sessions only, never the whole ledger corpus)
    that **share at least one tier/scope pairing** with this session's own
    pushed set — the honest structural signal available without storing raw
    query text. A page pushed only to a session outside the sample, or
    pushed with a tier/scope pairing this session never received, is never a
    candidate.

    The payload's ``structural_summary`` reports, over the whole sample:
    tier/scope concentration of the pushed set (:func:`_tier_scope_concentration`,
    flagging a degenerate single-pairing distribution rather than silently
    averaging over it), how much of each session's window-mate pool the
    tier/scope filter removes (aggregate + per-session range), and the
    policy-set miss-rate bounds those two facts imply
    (:func:`_policy_set_bounds`). Each session entry additionally carries its
    own ``candidate_pool_size`` and ``filter_removed_fraction``.

    ``exclude_sessions`` (issue athenaeum#986, same semantics as
    :func:`compute_baseline`'s ``exclude_sessions`` — athenaeum#791): known-
    synthetic session ids are dropped from the sampling pool AND from every
    other sampled session's candidate source, so a fixture/test session can
    never contaminate the worksheet. Excluded session ids actually present
    in the ledger, and how many push records they contributed, are always
    reported on the returned payload (``excluded_sessions`` /
    ``excluded_push_records``) — empty/zero when no exclusion was requested.

    Each requested ``exclude_sessions`` value is resolved via
    :func:`_resolve_exclude_sessions` against every session id in the full
    push-records ledger: the full session id, or an unambiguous prefix of
    exactly one known id (issue athenaeum#987). A value matching zero or
    more-than-one known session ids raises ``ValueError`` rather than
    silently excluding nothing.
    """
    records = read_push_records(cache_dir, wiki_root=wiki_root)
    known_session_ids = {
        sid for r in records if isinstance(sid := r.get("session_id"), str) and sid
    }
    exclude_set = _resolve_exclude_sessions(exclude_sessions or (), known_session_ids)
    excluded_records = (
        [r for r in records if r.get("session_id") in exclude_set] if exclude_set else []
    )
    if exclude_set:
        records = [r for r in records if r.get("session_id") not in exclude_set]

    session_ids = sample_sessions(
        cache_dir, n, seed=seed, exclude_sessions=exclude_set, wiki_root=wiki_root
    )

    by_session: dict[str, list[dict[str, Any]]] = {sid: [] for sid in session_ids}
    for rec in records:
        sid = rec.get("session_id")
        if sid in by_session:
            by_session[sid].append(rec)

    # Per-session pushed ids, tier/scope pairs, and raw pushed items
    # (undeduplicated — concentration is a fact about pushed ITEMS),
    # restricted to the sample window (the sampled sessions only — never
    # the whole ledger).
    session_pushed_ids: dict[str, set[str]] = {}
    session_pairs: dict[str, set[tuple[str, str]]] = {}
    session_items: dict[str, list[dict[str, Any]]] = {}
    for sid in session_ids:
        pushed: set[str] = set()
        pairs: set[tuple[str, str]] = set()
        items: list[dict[str, Any]] = []
        for rec in by_session[sid]:
            for item in rec.get("items", []):
                pid = item.get("id")
                if isinstance(pid, str):
                    pushed.add(pid)
                    pairs.add((str(item.get("tier", "")), str(item.get("scope", ""))))
                    items.append(item)
        session_pushed_ids[sid] = pushed
        session_pairs[sid] = pairs
        session_items[sid] = items

    all_sampled_items = [item for sid in session_ids for item in session_items[sid]]
    concentration = _tier_scope_concentration(all_sampled_items)

    sessions_out: list[dict[str, Any]] = []
    total_before_filter = 0
    total_after_filter = 0
    per_session_removed_fractions: list[float] = []
    for sid in session_ids:
        own_pushed = session_pushed_ids[sid]
        own_pairs = session_pairs[sid]
        before_filter: set[str] = set()
        after_filter: set[str] = set()
        for other_sid in session_ids:
            if other_sid == sid:
                continue
            for item in session_items[other_sid]:
                pid = item.get("id")
                if not isinstance(pid, str) or pid in own_pushed:
                    continue
                before_filter.add(pid)
                pair = (str(item.get("tier", "")), str(item.get("scope", "")))
                if pair in own_pairs:
                    after_filter.add(pid)
        pushed_ids = sorted(own_pushed)
        candidate_ids = sorted(after_filter)
        removed_fraction = (
            1 - (len(after_filter) / len(before_filter)) if before_filter else None
        )
        if removed_fraction is not None:
            per_session_removed_fractions.append(removed_fraction)
        total_before_filter += len(before_filter)
        total_after_filter += len(after_filter)
        sessions_out.append(
            {
                "session_id": sid,
                "pushed": pushed_ids,
                "pushed_count": len(pushed_ids),
                "candidates_not_pushed": candidate_ids,
                "candidate_pool_size": len(candidate_ids),
                "window_mate_pool_before_filter": len(before_filter),
                "filter_removed_fraction": removed_fraction,
            }
        )

    aggregate_removed_fraction = (
        1 - (total_after_filter / total_before_filter) if total_before_filter else None
    )
    pushed_total = sum(s["pushed_count"] for s in sessions_out)
    candidate_total = sum(s["candidate_pool_size"] for s in sessions_out)

    found_excluded_sessions = sorted(
        {sid for r in excluded_records if isinstance(sid := r.get("session_id"), str) and sid}
    )

    return {
        "v": SCHEMA_VERSION,
        "generated": now_iso(),
        "wiki_root": str(wiki_root),
        "search_backend": search_backend,
        "sampled_session_count": len(session_ids),
        "limitation": LIMITATION_STATEMENT,
        "structural_summary": {
            "tier_scope_concentration": concentration,
            "window_mate_filter": {
                "before_filter_id_count": total_before_filter,
                "after_filter_id_count": total_after_filter,
                "removed_fraction": aggregate_removed_fraction,
                "removed_fraction_range": {
                    "min": min(per_session_removed_fractions)
                    if per_session_removed_fractions
                    else None,
                    "max": max(per_session_removed_fractions)
                    if per_session_removed_fractions
                    else None,
                },
            },
            "policy_set_miss_rate_bounds": _policy_set_bounds(pushed_total, candidate_total),
        },
        "sessions": sessions_out,
        "excluded_sessions": found_excluded_sessions,
        "excluded_push_records": len(excluded_records),
    }


def write_coverage_worksheet(worksheet: dict[str, Any], *, output_path: Path) -> Path:
    """Write the worksheet as a durable JSON file (never console-only)."""
    from athenaeum.atomic_io import atomic_write_text

    atomic_write_text(
        output_path, json.dumps(worksheet, indent=2, sort_keys=False) + "\n"
    )
    return output_path
