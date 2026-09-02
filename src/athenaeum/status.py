# SPDX-License-Identifier: Apache-2.0
"""Athenaeum status — read-only snapshot of a knowledge base, L4 domain/pipeline.

Contract: computes a point-in-time :class:`StatusInfo` snapshot (raw-intake
backlog, entity counts, pending-question count, oversized-page scan, drain
ETA advisory, schema-fragment attribution) by READING the knowledge
directory and git log — it never mutates anything. Factoring rule: any
check here must stay side-effect-free and cheap enough to run between
librarian runs (it backs both the ``athenaeum status`` CLI command and the
MCP status surface); a check that needs to WRITE (e.g. actually retiring a
stale page, actually running a drain) belongs in the module that owns that
mutation (``retire.py``, ``athenaeum.drain``), not here — this module may
only advise, never act.

SCC membership (L4 domain/pipeline). Issue athenaeum#545 hoisted ``discover_raw_files``
to the :mod:`athenaeum.intake` leaf, so ``status.py`` now imports it from
``intake`` (not ``librarian``) at TOP level, and its top-level
``athenaeum.tiers.schema_fragment_state`` import is a normal downward
dependency. ``status.py`` no longer imports ``librarian`` at all, so the former
librarian<->status cycle is GONE: ``librarian.py``'s function-local
``scan_page_sizes`` import is now a one-way edge.

``status.py`` formerly participated in a PRE-EXISTING residual SCC that athenaeum#545 did
NOT target (out of its named scope): ``{librarian, drain, status}``, because it
function-locally imported ``athenaeum.drain`` (backlog-drain advisor) while
``drain`` function-locally imports ``librarian`` and ``librarian`` function-
locally imports ``status`` (``scan_page_sizes``). Issue athenaeum#640 dissolved that
cycle by hoisting ``build_advisory`` DOWN to the :mod:`athenaeum.drain_advisor`
leaf: ``status.py`` now function-locally imports it from ``drain_advisor`` (a
low leaf that imports none of these three), so it no longer reaches up into the
``drain`` orchestrator. The full-graph SCC is now empty and
``tests/test_import_graph_acyclic.py`` pins the baseline at ``[]``.

Issue athenaeum#899's zero-yield counter reads :mod:`athenaeum.zero_yield` (a small L2
leaf that owns the persisted-state sidecar only) at TOP level rather than
reading it via ``librarian.py`` — the same "hoist the shared bit to a leaf
both sides import" shape as ``discover_raw_files`` above, and for the same
reason: ``status.py`` importing ``librarian.py`` would reopen the
``{librarian, drain, status}`` cycle this docstring just finished describing
the dissolution of.

Issue athenaeum#1283's consecutive-refusal counter reads
:mod:`athenaeum.run_summary_log` (a true L2 leaf per its OWN module
docstring's layering note — it imports only ``config``/``store`` and
explicitly never ``librarian``) at TOP level, for the identical reason: it is
already the leaf both ``librarian.py`` (the writer) and this module (a
reader) import, so no cycle opens. Verified against
``tests/test_import_graph_acyclic.py``, which walks function-local imports
too and pins the full-graph SCC baseline at ``[]`` — this module's existing
function-local imports of ``athenaeum.drain_advisor`` and
``athenaeum.verdicts`` a few lines down are NOT the same situation:
``drain_advisor``/``verdicts`` are deferred because eager-importing them
(or a module they transitively touch) would reopen a residual SCC; adding
``run_summary_log`` at the top does not, so it is not deferred.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import TypedDict, cast

from athenaeum.config import (
    load_config,
    resolve_cache_dir,
    resolve_page_flag_bytes,
    resolve_page_warn_bytes,
)
from athenaeum.intake import discover_raw_files
from athenaeum.models import parse_frontmatter
from athenaeum.run_summary_log import read_refusal_streak
from athenaeum.tiers import schema_fragment_state
from athenaeum.zero_yield import load_state as load_zero_yield_state

log = logging.getLogger(__name__)


class StatusInfo(TypedDict):
    """Shape of the dict returned by :func:`status`.

    Public API — downstream tooling (dashboards, CI gates) can import this
    for type-checked access to the status payload. Keys are only ever ADDED
    here (never renamed/removed) so existing consumers stay valid.
    """

    raw_pending: int
    entity_count: int
    entities_by_type: dict[str, int]
    last_commit_date: str
    last_commit_message: str
    pending_questions: int
    # Issue athenaeum#310: wiki entity pages over the soft size thresholds, each a
    # ``(filename, byte_size)`` tuple sorted largest-first. ``pages_warn`` and
    # ``pages_flag`` are disjoint — a page over the flag threshold appears in
    # ``pages_flag`` only.
    pages_warn: list[tuple[str, int]]
    pages_flag: list[tuple[str, int]]
    # Issue athenaeum#470: backlog-drain ETA advisory — a human sentence projecting
    # time-to-drain and naming the ``athenaeum drain`` remedy, or ``None`` when
    # the backlog is empty or its projected ETA is at/below
    # ``librarian.drain_warn_days``. Surfaces the same signal the end-of-run
    # WARNING emits, so status/MCP surfaces show it BETWEEN runs.
    drain_advisory: str | None
    # Issue athenaeum#567: live-vs-default state of each operator-tunable schema fragment,
    # as ``{filename: (sha256_hex, is_default)}`` from
    # :func:`athenaeum.tiers.schema_fragment_state`. Surfaces which fragment
    # bytes are in play between runs, the same attribution the run-summary line
    # records; a fragment matching its bundled default has ``is_default=True``.
    schema_fragments: dict[str, tuple[str, bool]]
    # Issue athenaeum#899: the persisted CONSECUTIVE zero-yield run count — how many
    # runs in a row, up to and including the most recently finalized one,
    # spent LLM calls, committed zero files, and made no progress against the
    # deferred set. ``0`` when the most recent run was not zero-yield (or no
    # run has ever finalized). Read directly from :mod:`athenaeum.zero_yield`'s
    # sidecar, the same persisted state the librarian finalize phase writes.
    zero_yield_consecutive: int
    # Issue athenaeum#1283: the persisted CONSECUTIVE athenaeum#1135 zero-progress-
    # refusal count — how many runs in a row, up to and including the most
    # recently finalized one, stopped early for a resource reason (budget/
    # deadline/spend-ceiling) and committed zero files. Deliberately a
    # SEPARATE counter from ``zero_yield_consecutive`` above, not merged into
    # it: the athenaeum#899 zero-yield predicate requires ``api_calls > 0`` (or
    # ``attempted_calls > 0``), which a spend-exhausted refusal that made
    # ZERO calls never satisfies — that gap is the whole reason this issue
    # exists. ``0`` when the most recent run was not a refusal (or no run has
    # ever finalized, or the ledger predates athenaeum#1283 and cannot speak to
    # it — see ``run_summary_log.refusal_in_record``). Read from the athenaeum#1102
    # run-summary ledger via ``run_summary_log.read_refusal_streak``, the
    # same durable record ``RunContext.emit_run_summary`` already writes —
    # no new state file.
    librarian_refusal_consecutive: int
    # Issue athenaeum#1283: the most recent refusal's detail dict
    # (``{"reason": <ctx.entity_exit_reason>, "files": 0}``), or ``None``
    # when ``librarian_refusal_consecutive`` is 0. Carried alongside the
    # count so a caller can name WHY (budget / deadline / spend-ceiling)
    # without a second ledger read.
    librarian_refusal_reason: dict[str, object] | None
    # Issue athenaeum#712: per-branch verdict-ledger duty cycle
    # (nights-in-wave / nights, target <=0.25 — reporting only, enforcing the
    # target is out of scope), or ``None`` when the ledger has never been
    # materialized (``librarian.verdict_ledger_enabled`` off, or a run has
    # never touched it) — the common case, and the ONLY case while the flag
    # is off, so status output is unaffected until an operator opts in.
    verdict_ledger_duty_cycle: dict[str, float] | None


def scan_page_sizes(
    wiki_root: Path,
    warn_bytes: int,
    flag_bytes: int,
) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """Bucket oversized wiki entity pages (issue athenaeum#310).

    Walks the same ``wiki/*.md`` set the entity count walks — skipping
    ``_``-prefixed files and non-entity pages (no frontmatter ``name``) — and
    measures each page's UTF-8 size (frontmatter + body) in bytes. Returns
    ``(pages_warn, pages_flag)`` where each list holds ``(filename, byte_size)``
    tuples sorted largest-first. A page whose size exceeds ``flag_bytes`` lands
    only in ``pages_flag`` (not also in ``pages_warn``); a page over
    ``warn_bytes`` but at/under ``flag_bytes`` lands in ``pages_warn``.

    Guards an inverted config: if ``flag_bytes <= warn_bytes`` (which would make
    the flag bucket no stricter than warn and invert severity), ``flag_bytes``
    is clamped up to ``warn_bytes`` and a single ``WARNING`` names the
    misconfiguration. Aside from that guard it is observational — it reads,
    measures, and reports; it never modifies any file.
    """
    if flag_bytes <= warn_bytes:
        log.warning(
            "page_flag_bytes (%d) <= page_warn_bytes (%d): flag threshold "
            "clamped up to warn; fix librarian.page_flag_bytes / "
            "page_warn_bytes so flag > warn",
            flag_bytes,
            warn_bytes,
        )
        flag_bytes = max(flag_bytes, warn_bytes)

    pages_warn: list[tuple[str, int]] = []
    pages_flag: list[tuple[str, int]] = []
    if not wiki_root.exists():
        return pages_warn, pages_flag
    for fpath in sorted(wiki_root.glob("*.md")):
        if fpath.name.startswith("_"):
            continue
        try:
            text = fpath.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, _ = parse_frontmatter(text)
        if not meta or not meta.get("name"):
            continue
        size = len(text.encode("utf-8"))
        if size > flag_bytes:
            pages_flag.append((fpath.name, size))
        elif size > warn_bytes:
            pages_warn.append((fpath.name, size))
    pages_warn.sort(key=lambda item: item[1], reverse=True)
    pages_flag.sort(key=lambda item: item[1], reverse=True)
    return pages_warn, pages_flag


def status(knowledge_root: Path) -> StatusInfo:
    """Gather status information about a knowledge base."""
    wiki_root = knowledge_root / "wiki"
    raw_root = knowledge_root / "raw"

    # Resolved once and reused (the page-size thresholds below read it too).
    config = load_config(knowledge_root)

    # Raw files pending. Issue athenaeum#843: pass `config` so an operator's
    # `librarian.non_intake_sources` exclusions are honoured here too — a
    # backlog count that included dirs the librarian will never process would
    # report work that is never going to drain.
    raw_files = discover_raw_files(raw_root, config)
    raw_pending = len(raw_files)

    # Entity counts
    entities_by_type: dict[str, int] = {}
    entity_count = 0
    if wiki_root.exists():
        for fpath in sorted(wiki_root.glob("*.md")):
            if fpath.name.startswith("_"):
                continue
            try:
                text = fpath.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            meta, _ = parse_frontmatter(text)
            if not meta or not meta.get("name"):
                continue
            entity_count += 1
            # meta is dict[str, object] (arbitrary YAML scalars); ``type``
            # is one of the identity fields parse_frontmatter coerces to
            # str when the YAML loader produced an int (e.g. a bare
            # numeric type name), so it is str in practice. Cast rather
            # than assert: the original code used the raw value as a dict
            # key unconditionally (no crash for other hashable types), and
            # a cast preserves that permissiveness while satisfying the
            # dict[str, int] annotation.
            etype = cast(str, meta.get("type", "unknown"))
            entities_by_type[etype] = entities_by_type.get(etype, 0) + 1

    # Last git commit
    last_commit_date = ""
    last_commit_message = ""
    if (knowledge_root / ".git").exists():
        result = subprocess.run(
            ["git", "log", "-1", "--format=%ai|||%s"],
            cwd=str(knowledge_root),
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split("|||", 1)
            last_commit_date = parts[0].strip()
            last_commit_message = parts[1].strip() if len(parts) > 1 else ""

    # Pending questions
    pending_questions = 0
    pq_path = wiki_root / "_pending_questions.md"
    if pq_path.exists():
        text = pq_path.read_text(encoding="utf-8")
        pending_questions = text.count("## [")

    # Oversized wiki pages (issue athenaeum#310). Thresholds come from config so an
    # operator can tune them; the scan is warn-only and never mutates anything.
    warn_bytes = resolve_page_warn_bytes(config)
    flag_bytes = resolve_page_flag_bytes(config)
    pages_warn, pages_flag = scan_page_sizes(wiki_root, warn_bytes, flag_bytes)

    # Backlog-drain ETA advisor (issue athenaeum#470): surface the same projection the
    # end-of-run WARNING emits, so status/MCP surfaces show it between runs.
    # Best-effort — a ledger/estimator hiccup must never break status.
    drain_advisory: str | None = None
    try:
        from athenaeum import spend as _spend
        from athenaeum.config import resolve_drain_warn_days
        from athenaeum.drain_advisor import build_advisory

        advisory = build_advisory(
            backlog=raw_pending,
            ledger_records=_spend.read_ledger(
                _spend.resolve_ledger_path(config, wiki_root=wiki_root)
            ),
            warn_days=resolve_drain_warn_days(config),
            config=config,
        )
        if advisory is not None:
            drain_advisory = advisory.summary
    except Exception as exc:  # noqa: BLE001 — advisor must never break status
        log.debug(
            "status: backlog-drain advisor skipped (%s): %s",
            type(exc).__name__,
            exc,
        )

    # Issue athenaeum#567: schema-fragment divergence — which operator-tunable fragments
    # differ from the bundled default. Observational (read-only); mirrors the
    # attribution the librarian run-summary line records.
    schema_fragments = schema_fragment_state(wiki_root)

    # Issue athenaeum#899: the persisted consecutive-zero-yield run count. Read-only
    # (this module never writes the sidecar — the librarian finalize phase
    # does); :func:`athenaeum.zero_yield.load_state` already fails open to
    # ``0`` on a missing/corrupt sidecar, so no additional try/except is
    # needed here. Lives under the CACHE dir, not ``wiki_root`` (see
    # :mod:`athenaeum.zero_yield`'s module docstring for why) — resolved the
    # same way the librarian finalize phase resolves it (``cache_dir=None``:
    # ``ATHENAEUM_CACHE_DIR`` env, else the packaged default), so the read
    # and write sides always agree on the same file.
    zero_yield_consecutive = load_zero_yield_state(resolve_cache_dir())["consecutive"]

    # Issue athenaeum#1283: the persisted consecutive athenaeum#1135 refusal count.
    # Read-only, same discipline as the zero-yield read above: this module
    # never writes the ledger (``RunContext.emit_run_summary`` does), and
    # ``read_refusal_streak`` already fails open to ``(0, None)`` on a
    # missing/corrupt ledger, so no additional try/except is needed here —
    # matches the zero-yield read's own bare (no try/except) shape.
    librarian_refusal_consecutive, librarian_refusal_reason = read_refusal_streak(
        cache_dir=resolve_cache_dir()
    )

    # Issue athenaeum#712: verdict-ledger duty-cycle report. Only computed when
    # the ledger has actually been materialized (flag-off / never-run leaves
    # this None, so status output is unaffected until an operator opts in via
    # librarian.verdict_ledger_enabled). Best-effort — a read hiccup here must
    # never break status, same discipline as the drain advisory above.
    verdict_ledger_duty_cycle: dict[str, float] | None = None
    try:
        from athenaeum.verdicts import duty_cycle_report, ledger_exists

        if ledger_exists(wiki_root):
            report = duty_cycle_report(wiki_root)
            if report:
                verdict_ledger_duty_cycle = report
    except Exception as exc:  # noqa: BLE001 — must never break status
        log.debug(
            "status: verdict-ledger duty-cycle report skipped (%s): %s",
            type(exc).__name__,
            exc,
        )

    return {
        "raw_pending": raw_pending,
        "entity_count": entity_count,
        "entities_by_type": entities_by_type,
        "last_commit_date": last_commit_date,
        "last_commit_message": last_commit_message,
        "pending_questions": pending_questions,
        "pages_warn": pages_warn,
        "pages_flag": pages_flag,
        "drain_advisory": drain_advisory,
        "schema_fragments": schema_fragments,
        "zero_yield_consecutive": zero_yield_consecutive,
        "librarian_refusal_consecutive": librarian_refusal_consecutive,
        "librarian_refusal_reason": librarian_refusal_reason,
        "verdict_ledger_duty_cycle": verdict_ledger_duty_cycle,
    }


def format_status(info: StatusInfo) -> str:
    """Format status dict as human-readable output."""
    lines = ["Athenaeum Status", "=" * 40]

    lines.append(f"Raw files pending:    {info['raw_pending']}")
    lines.append(f"Wiki entities:        {info['entity_count']}")

    if info["entities_by_type"]:
        for etype in sorted(info["entities_by_type"]):
            lines.append(f"  {etype}: {info['entities_by_type'][etype]}")

    lines.append(f"Pending questions:    {info['pending_questions']}")

    # Issue athenaeum#470: backlog-drain ETA advisory. Use ``.get`` so pre-athenaeum#470 status
    # dicts (missing the key) still format cleanly; shown only when set (a
    # non-empty backlog projected to exceed librarian.drain_warn_days).
    drain_advisory = info.get("drain_advisory")
    if drain_advisory:
        lines.append(f"Backlog drain:        {drain_advisory}")

    # Issue athenaeum#899: consecutive-zero-yield alarm. Use ``.get`` so pre-athenaeum#899
    # status dicts (missing the key) still format cleanly; shown only when
    # non-zero (mirrors the drain-advisory "only when actionable" pattern
    # above) so a healthy operator's status output stays quiet.
    zero_yield_consecutive = info.get("zero_yield_consecutive", 0)
    if zero_yield_consecutive:
        lines.append(
            f"Zero-yield runs:      {zero_yield_consecutive} consecutive"
        )

    # Issue athenaeum#1283: the athenaeum#1135 zero-progress-refusal streak. Use
    # ``.get`` so pre-athenaeum#1283 status dicts (missing the key) still format
    # cleanly. Deliberately shown starting at a streak of 1 — unlike the
    # zero-yield line above (which is itself an unconditional count once
    # non-zero) and unlike the starvation WARNING's streak-of-3 threshold,
    # this line's whole reason for existing is that a run that refused all
    # work on an exhausted budget must NOT read as healthy even once; the
    # issue's motivation is exactly a single such run reading as healthy.
    # The prefix mirrors ``run_summary_log.REFUSAL_ALERT_PREFIX`` so a grep
    # for that token also finds this line.
    librarian_refusal_consecutive = info.get("librarian_refusal_consecutive", 0)
    if librarian_refusal_consecutive:
        _refusal_reason = info.get("librarian_refusal_reason") or {}
        _reason_token = _refusal_reason.get("reason") if isinstance(
            _refusal_reason, dict
        ) else None
        lines.append(
            "librarian-run-refusal: "
            f"{librarian_refusal_consecutive} consecutive run(s) refused to "
            f"do any work (reason={_reason_token or 'unknown'}) — issue athenaeum#1283"
        )

    # Issue athenaeum#712: verdict-ledger duty cycle (nights-in-wave / nights,
    # target <=25%), one line per branch with an open/closed comparator
    # epoch. ``.get`` keeps a pre-athenaeum#712 status dict formatting cleanly;
    # ``None``/empty (the flag-off default) shows nothing.
    verdict_duty_cycle = info.get("verdict_ledger_duty_cycle")
    if verdict_duty_cycle:
        lines.append("Verdict ledger duty cycle:")
        for branch in sorted(verdict_duty_cycle):
            lines.append(f"  {branch}: {verdict_duty_cycle[branch]:.0%}")

    # Issue athenaeum#310: oversized-page summary. Use ``.get`` so pre-athenaeum#310 status
    # dicts (missing these keys) still format cleanly.
    pages_warn = info.get("pages_warn", [])
    pages_flag = info.get("pages_flag", [])
    lines.append(f"Oversized pages (warn/flag): {len(pages_warn)}/{len(pages_flag)}")
    for name, size in pages_flag:
        lines.append(f"  [flag] {name} ({size} bytes)")
    for name, size in pages_warn:
        lines.append(f"  [warn] {name} ({size} bytes)")

    # Issue athenaeum#567: one divergence line per operator-tunable schema fragment —
    # ``default`` when it matches the bundled copy, ``edited (sha8 …)`` when the
    # live bytes differ. ``.get`` keeps pre-athenaeum#567 status dicts formatting cleanly.
    schema_fragments = info.get("schema_fragments") or {}
    if schema_fragments:
        lines.append("Schema fragments:")
        for name in sorted(schema_fragments):
            sha_hex, is_default = schema_fragments[name]
            detail = "default" if is_default else f"edited (sha8 {sha_hex[:8]})"
            lines.append(f"  {name}: {detail}")

    if info["last_commit_date"]:
        lines.append(f"Last commit:          {info['last_commit_date']}")
        lines.append(f"  {info['last_commit_message']}")
    else:
        lines.append("Last commit:          (no git history)")

    return "\n".join(lines)
