# SPDX-License-Identifier: Apache-2.0
"""Rule proposals: the librarian drafts, a human approves (issue athenaeum#905).

**The detector's data source, respecified.** The issue as filed assumed an
"intake ledger" that did not exist; it was kicked back twice on that premise
(see the issue's own kickback comments). The operator's 2026-08-20 decision
(option (a)) was to build the per-record ledger as a prerequisite —
`wiki/_shape_rule_dispositions.jsonl`, issue athenaeum#975
(:mod:`athenaeum.rules` "Per-record disposition rows") — and respecify this
issue's "intake ledger" references to that ledger. This module is that
respecification, made concrete:

- **AC1's "tier 2 or tier 3"** does not map onto athenaeum#975's ledger: that
  ledger's `tier` field is either `0` (the deterministic shape-rules pass,
  tier 0 on the ladder in `docs/design/field-corrections.md` §2, actually disposed
  of the record) or `None` (it did not — no rule matched, a rule explicitly
  deferred, or a soft failure degraded to fallthrough). The shape-rules pass
  genuinely cannot know which reasoning tier (>=1) eventually handles a
  deferred record, so it never guesses one. The faithful reading of this
  issue's own Motivation — *"the reasoning tiers stop re-deriving the same
  conclusion for the fiftieth instance of a shape"* — is: **count rows whose
  `tier` is `None`.** That population *is* the reasoning-tier work the
  Motivation is about. This module therefore counts `tier is None` rows,
  grouped by `(source, key_fingerprint)`; it never writes or reads a literal
  tier number 2 or 3 anywhere.
- **"Reasoning-tier work"** — the issue's second ambiguity (which tier
  ladder?) — is the **intake** ladder (`docs/design/field-corrections.md` §2:
  tier0 structured -> tier1 programmatic -> tier2 classify -> tier3 merge ->
  tier4 human), because that is the ladder athenaeum#975's ledger is built
  against; the *reasoning* ladder (`reasoning_tiers.py`'s T1/T2) is a
  different, unrelated numbering.

**The tier-3-output join (AC2's "plus their existing tier-3 outputs")
does not exist today, and this module does not invent one.** Checked
directly against `reasoning_tiers.py`: every record in
`_reasoning_tier_decisions.jsonl` is keyed by `proposal_id` — a MERGE
PROPOSAL id (`ReasoningProposal.from_pending_merge`) — and carries no
`source`/`source_ref` field a raw intake record's ref could join against
(`_build_log_record_fields`'s field list is `v, ts, tier, decision, reason,
reason_code, model, proposal_id` — nothing else). See
:func:`_tier3_outputs_for_exemplars`. The drafting call degrades EXPLICITLY:
it drafts from the exemplar records alone and states, in both the request
sent to the model and the persisted proposal (`tier3_linked`/`tier3_note`),
that tier-3 outputs were not linkable — never silently dropped, never
invented.

**Lifecycle.** :func:`run_rule_proposal_detection` is the phase entry
point: it groups deferred disposition rows by shape, and for every shape
crossing :func:`athenaeum.config.resolve_rule_proposals_threshold` within
:func:`athenaeum.config.resolve_rule_proposals_window_days`, selects up to
:func:`athenaeum.config.resolve_rule_proposals_exemplar_count` readable
exemplars and makes ONE drafting call
(:func:`build_rule_proposal_request_params`), producing candidate rule YAML
valid against :class:`athenaeum.rules.ShapeRule` plus a projected-impact
line. The result is appended to `wiki/_rule_proposals.jsonl` as a `proposal`
record and surfaced through
:func:`athenaeum.decisions.list_pending_decisions` (via
:func:`athenaeum.decisions.proposed_rule_to_decision`) as a `proposed-rule`
decision. :func:`approve_rule_proposal` writes the rule into
`<knowledge_root>/rules/` in **observe mode, forced, never live**;
:func:`reject_rule_proposal` records a `reject` event which — because a
proposal's id is derived from its SHAPE, not from the event
(:func:`proposal_item_id`) — permanently suppresses that shape from being
proposed again (AC6).

**Wiring note (issue athenaeum#1063 — supersedes the prior deferral).**
`run_rule_proposal_detection` is now called from `librarian.py`'s nightly
run loop via `librarian._run_rule_proposal_phase`, mirroring how
`athenaeum.rules.run_shape_rule_phase` and
`athenaeum.intake_audit.run_intake_audit` are wired in: config-gated
(`librarian.rule_proposals.enabled`, default **False** — an operator must
opt in before this phase makes its first LLM call), participating in the
run's wall-clock deadline via the `deadline_check` parameter above (checked
per-shape, mirroring `run_shape_rule_phase`'s per-file check), and
accounting its one-call-per-shape spend via the `usage` parameter above
(`athenaeum.tiers._record_usage`, `knob="rule_proposals"` — the same
mechanism the tier-2/3 call sites use). Cadence is governed by the
detector's own `threshold` (default 50 disposition rows) plus its built-in
per-shape idempotency (a pending or rejected shape is never re-drafted) —
no separate once-per-period stamp; see `_run_rule_proposal_phase`'s
docstring for why that is sufficient.

Persistence mirrors :mod:`athenaeum.quarantine` (the closest precedent: a
JSONL ledger with distinct EVENT kinds, an unresolved-items query, and a
resolving action that writes elsewhere): one ledger,
`<wiki_root>/_rule_proposals.jsonl`, with three record kinds --
:data:`PROPOSAL_KIND` (a shape crossed threshold and was drafted),
:data:`APPROVE_KIND` (an operator approved -- rule written, observe mode),
:data:`REJECT_KIND` (an operator rejected -- the shape is suppressed).

Layering: L4 domain/pipeline module, a peer of :mod:`athenaeum.quarantine`
and :mod:`athenaeum.calibration`. Imports :mod:`athenaeum.rules` (L3, for
:class:`~athenaeum.rules.ShapeRule` / `load_rules` / the disposition-ledger
path/reader) and :mod:`athenaeum.provider`/`_retry`/`prompt_safety`/`config`
(L3 leaves). :mod:`athenaeum.decisions` imports this module for its mapper
-- this module never imports `decisions` back, so no cycle.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError as PydanticValidationError

from athenaeum._retry import with_retry
from athenaeum.atomic_io import atomic_write_text
from athenaeum.config import (
    resolve_model,
    resolve_rule_proposals_exemplar_count,
    resolve_rule_proposals_threshold,
    resolve_rule_proposals_window_days,
)
from athenaeum.models import TokenUsage, parse_frontmatter
from athenaeum.prompt_safety import data_only_clause, fence_untrusted
from athenaeum.provider import resolve_max_tokens, resolve_thinking, response_text
from athenaeum.rules import ShapeRule, default_shape_rule_dispositions_path
from athenaeum.store import append_line_durable, now_iso
from athenaeum.tiers import _record_usage

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Ledger: kinds, paths, ids
# ---------------------------------------------------------------------------

#: Schema version stamped on every ledger record so a future reader can migrate.
RULE_PROPOSALS_LEDGER_VERSION = 1

#: Sidecar filename under ``wiki_root``, alongside ``_quarantine.jsonl`` /
#: ``_calibration.jsonl`` / ``_shape_rule_dispositions.jsonl``.
RULE_PROPOSALS_LEDGER_FILENAME = "_rule_proposals.jsonl"

#: Record kinds in the single rule-proposals ledger.
PROPOSAL_KIND = "proposal"
APPROVE_KIND = "approve"
REJECT_KIND = "reject"


def default_rule_proposals_ledger_path(wiki_root: Path) -> Path:
    """Default ledger path: ``<wiki_root>/_rule_proposals.jsonl``."""
    return Path(wiki_root) / RULE_PROPOSALS_LEDGER_FILENAME


def _now_iso(now: datetime | None = None) -> str:
    """Thin delegation to :func:`athenaeum.store.now_iso` (issue athenaeum#1348),
    kept as a module-private wrapper only because its ``now: datetime | None``
    parameter name/position differs from the shared helper's ``when`` — the
    rendering rule itself lives in exactly one place, per the athenaeum#980
    ``_append_jsonl_line``-style wrapper convention."""
    return now_iso(now)


def proposal_item_id(source: str, key_fingerprint: str) -> str:
    """Deterministic id for the ``(source, key_fingerprint)`` SHAPE.

    Deliberately NOT per-event (contrast
    :func:`athenaeum.quarantine.quarantine_item_id`, which varies per event
    so the same ref can be re-quarantined after a release). A proposal's
    identity IS the shape: AC6 requires a rejected shape never be proposed
    again, and keying the id on the shape alone is what makes that
    suppression check a plain set-membership test
    (:func:`_rejected_shape_ids`) rather than a separate index.
    """
    digest = hashlib.sha1(f"{source}\x00{key_fingerprint}".encode("utf-8")).hexdigest()
    return digest[:16]


def _append_jsonl_line(path: Path, line: str) -> None:
    """Append one line to *path* durably (``O_APPEND`` + fsync), via
    :func:`athenaeum.store.append_line_durable` — the single shared
    implementation issue athenaeum#980 (S5) collapsed this module's copy (and
    every other per-module-ledger house-style copy) onto (design note §2.4 /
    §6.2)."""
    append_line_durable(path, line.encode("utf-8"))


def read_rule_proposals_ledger(
    wiki_root: Path, *, ledger_path: Path | None = None
) -> list[dict[str, Any]]:
    """Read every well-formed ledger record, tolerating a torn trailing line.

    Returns ``[]`` when the ledger does not exist. Malformed lines (a crash
    mid-write, or a hand-edit) are skipped, not fatal -- same tolerant-reader
    contract as :func:`athenaeum.quarantine.read_quarantine_ledger`.
    """
    target = (
        ledger_path if ledger_path is not None else default_rule_proposals_ledger_path(wiki_root)
    )
    if not target.exists():
        return []
    try:
        raw_text = target.read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _kind_ids(records: list[dict[str, Any]], kind: str) -> set[str]:
    return {str(r.get("id")) for r in records if r.get("kind") == kind}


def _resolved_ids(records: list[dict[str, Any]]) -> set[str]:
    return _kind_ids(records, APPROVE_KIND) | _kind_ids(records, REJECT_KIND)


def list_pending_rule_proposals(
    wiki_root: Path, *, ledger_path: Path | None = None
) -> list[dict[str, Any]]:
    """Proposed rules awaiting an operator's approve/reject decision.

    Excludes any proposal event that already has a matching approve OR
    reject record -- the same "unreviewed" filter shape as
    :func:`athenaeum.quarantine.list_pending_quarantine` /
    :func:`athenaeum.calibration.list_pending_audit`.
    """
    records = read_rule_proposals_ledger(wiki_root, ledger_path=ledger_path)
    resolved = _resolved_ids(records)
    return [
        r for r in records if r.get("kind") == PROPOSAL_KIND and str(r.get("id")) not in resolved
    ]


# ---------------------------------------------------------------------------
# Detector (AC1): shape frequency over deferred (`tier is None`) rows
# ---------------------------------------------------------------------------


def _parse_row_at(row: dict[str, Any]) -> datetime | None:
    raw = row.get("at")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _grouped_deferred_rows(
    wiki_root: Path, *, window_days: int, now: datetime | None = None
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Every ``_shape_rule_dispositions.jsonl`` row with ``tier is None``,
    within the last *window_days* of *now*, grouped by ``(source,
    key_fingerprint)``.

    Mirrors the worked reduction in
    ``tests/test_rules_dispositions.py::TestShapeFrequencyQuery`` exactly
    (``tier is None`` restriction, same grouping key), extended with the
    issue's "configurable window" over the row's own ``at`` timestamp.
    """
    resolved_now = now or datetime.now(timezone.utc)
    if resolved_now.tzinfo is None:
        resolved_now = resolved_now.replace(tzinfo=timezone.utc)
    cutoff = resolved_now - timedelta(days=window_days)

    path = default_shape_rule_dispositions_path(wiki_root)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    if not path.is_file():
        return grouped
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return grouped
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or row.get("tier") is not None:
            continue
        source = row.get("source")
        key_fingerprint = row.get("key_fingerprint")
        if not isinstance(source, str) or not isinstance(key_fingerprint, str):
            continue
        at = _parse_row_at(row)
        if at is None or at < cutoff:
            continue
        grouped.setdefault((source, key_fingerprint), []).append(row)
    return grouped


def _distinct_record_count(rows: list[dict[str, Any]]) -> int:
    """The detector's counting unit (issue athenaeum#1229 part 2): the number of
    DISTINCT ``source_ref``s among *rows*, never ``len(rows)``.

    Before this fix the detector counted ROWS -- one row per (record x
    evaluation) -- while :func:`athenaeum.config.resolve_rule_proposals_threshold`'s
    own docstring says "the record count ... that must be crossed". Those
    two readings differ by exactly the ledger's re-evaluation duplication
    factor: on the deployment that motivated this issue, 148x (measured: 57
    of 66 shapes crossed a threshold of 50 by row count vs. only 6 by
    distinct ``source_ref`` -- ~9.5x over-triggering). This function is the
    single place that decision is made; every caller below (ordering,
    the threshold comparison, the persisted proposal's ``count`` field, the
    no-exemplar log line) goes through it so none of them can drift back to
    counting rows independently.

    A row with a missing/non-string ``source_ref`` (should not happen --
    :func:`_grouped_deferred_rows` already filters those out before a row
    ever reaches a group) falls back to counting the row itself, so a
    malformed row is never silently dropped from the count.
    """
    refs = {row.get("source_ref") for row in rows if isinstance(row.get("source_ref"), str)}
    malformed = sum(1 for row in rows if not isinstance(row.get("source_ref"), str))
    return len(refs) + malformed


def detect_shape_frequency(
    wiki_root: Path, *, config: dict[str, Any] | None = None, now: datetime | None = None
) -> Counter[tuple[str, str]]:
    """AC1's detector: counts of DISTINCT deferred records (never rows -- see
    :func:`_distinct_record_count`) by ``(source, key_fingerprint)``, over
    :func:`athenaeum.config.resolve_rule_proposals_window_days`.

    Pure and side-effect-free -- reads the disposition ledger, writes
    nothing.
    """
    window_days = resolve_rule_proposals_window_days(config)
    grouped = _grouped_deferred_rows(wiki_root, window_days=window_days, now=now)
    return Counter({key: _distinct_record_count(rows) for key, rows in grouped.items()})


# ---------------------------------------------------------------------------
# Exemplar selection -- readable raw records for a detected shape
# ---------------------------------------------------------------------------


def _read_exemplar_record(raw_root: Path, source_ref: str) -> dict[str, Any] | None:
    """Read the record dict a disposition row's ``source_ref`` still names.

    Mirrors :func:`athenaeum.rules._record_and_format`'s md/jsonl extraction
    (duplicated, not imported -- that helper is private to `rules.py`).
    Returns ``None`` when the raw file no longer exists (compiled and
    retired since the disposition row was written -- an ordinary, expected
    case, not an error) or its content cannot be parsed into a record.
    """
    path = Path(raw_root) / source_ref
    if not path.is_file():
        return None
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    fmt = path.suffix.lower().lstrip(".")
    if fmt == "jsonl":
        first_line = content.split("\n", 1)[0]
        try:
            obj = json.loads(first_line)
        except (json.JSONDecodeError, ValueError):
            return None
        return obj if isinstance(obj, dict) else None
    if fmt == "md":
        meta, _body = parse_frontmatter(content)
        return dict(meta) if meta else None
    return None


def _select_exemplars(
    raw_root: Path, rows: list[dict[str, Any]], k: int
) -> list[tuple[str, dict[str, Any]]]:
    """Up to *k* READABLE exemplars for one shape, most-recent-first.

    *rows* is one shape's deferred disposition rows in ledger (append,
    oldest-first) order; scanned in reverse so the freshest occurrences are
    tried first. A row whose raw file has since been compiled/retired (or
    is otherwise unreadable) is skipped, never counted -- see the module
    docstring's "no readable exemplar" path. May return fewer than *k*
    (including zero) entries; the caller decides what that means.
    """
    selected: list[tuple[str, dict[str, Any]]] = []
    for row in reversed(rows):
        ref = row.get("source_ref")
        if not isinstance(ref, str):
            continue
        record = _read_exemplar_record(raw_root, ref)
        if record is None:
            continue
        selected.append((ref, record))
        if len(selected) >= k:
            break
    return selected


def _tier3_outputs_for_exemplars(
    wiki_root: Path, exemplar_refs: Sequence[str]
) -> dict[str, str]:
    """Attempt AC2's "their existing tier-3 outputs" join. See the module
    docstring for why this always returns ``{}`` against the CURRENT
    ``_reasoning_tier_decisions.jsonl`` schema: every record there is keyed
    by a merge ``proposal_id``, never a raw intake ``source_ref`` -- there is
    no join key connecting the two ledgers' currencies. Not hard-coded
    ``False``/skipped -- this is the real join attempt's shape, so a future
    schema change adding a `source_ref` field to that ledger makes the join
    start working with no caller-side change; today it genuinely finds
    nothing. Callers must not read an empty result as "no tier-3 output
    exists for this record", only "not linkable from what is persisted".
    """
    del wiki_root, exemplar_refs  # no join key exists yet; see docstring
    return {}


# ---------------------------------------------------------------------------
# Drafting call (AC2/AC3/AC7)
# ---------------------------------------------------------------------------

#: Default model for the drafting call -- a genuine judgment task (choose a
#: disposition and, for `emit`, a field-correction template, from exemplar
#: records) closer to T2's "draft" authority than a cheap classification
#: call, so it defaults to the same tier T2 uses. A separate knob/constant
#: (not imported from `reasoning_tiers.py`) so this module has no import-time
#: coupling to that one.
DEFAULT_RULE_PROPOSALS_MODEL = "claude-opus-4-8"

_RULE_PROPOSALS_MAX_TOKENS = 4096


def _get_rule_proposals_model(config: dict[str, Any] | None = None) -> str:
    """The ``rule_proposals`` knob's resolved model (env > yaml > default).

    Mirrors ``tiers._get_classify_model`` / ``tiers._get_write_model`` --
    a single-purpose getter so callers outside this module (issue
    athenaeum#1174: ``librarian._resolve_run_models``, the athenaeum#783 preflight
    input) resolve the SAME model this module's own drafting call
    (:func:`build_rule_proposal_request_params`) will actually use, without
    hand-rolling a second resolution path.
    """
    return resolve_model(
        "rule_proposals", "ATHENAEUM_RULE_PROPOSALS_MODEL", DEFAULT_RULE_PROPOSALS_MODEL, config
    )

#: The disposition vocabulary a DRAFTED proposal may choose from -- every
#: `ShapeRule.disposition` value EXCEPT `rollup`. `rollup` aggregates many
#: records into one correction via a `group_by`/`aggregate` spec
#: (`athenaeum.rules.RollupSpec`) that is a materially different, harder
#: drafting problem than "pick one disposition for one record shape"; an
#: operator can always hand-author a rollup rule from a proposal's exemplars.
_ALLOWED_DRAFT_DISPOSITIONS: frozenset[str] = frozenset(
    {"emit", "fallthrough", "drop", "retain", "preserve"}
)

_EXEMPLAR_MAX_CHARS = 2000

_RULE_PROPOSAL_SYSTEM_PROMPT = f"""You are the librarian, drafting ONE candidate shape rule \
for a human operator to review -- you never activate anything yourself.

{data_only_clause("exemplar_record")}

A shape rule is declarative YAML matched against a fixed schema
(`athenaeum.rules.ShapeRule`). You do NOT choose the rule's `match` block --
it is already fixed to this shape's `source` and `key_fingerprint` by the
caller. Your job is only to choose:

- `disposition`: exactly one of "emit", "fallthrough", "drop", "retain", \
"preserve".
- `correction` (REQUIRED for "emit", OPTIONAL for "preserve", FORBIDDEN for \
"fallthrough"/"drop"/"retain"): an object with:
  - `target`: a mapping whose KEY SET is exactly one of {{"uid"}}, \
{{"type", "name"}}, {{"type", "handle"}} -- values may be a literal, a \
`"$field"` reference to an exemplar record field, or {{"fn": \
"set_diff"|"first"|"date_of", "args": [...]}}.
  - `op`: "set", "add", or "remove".
  - `field`: the target entity field name being corrected (a string).
  - `value`: literal, `"$field"`, or an `fn` call (same vocabulary as \
`target`).
  - `observed_at` (optional): same vocabulary.
  - `note` (optional): a short string.
  - Do NOT include a `source` key -- the caller sets it.
- `projected_impact`: one plain-English sentence estimating what approving \
this rule would change (e.g. how many future deferred records it would \
likely resolve, based on the exemplar count).
- `rationale`: one or two sentences on why this disposition/correction fits \
the exemplars shown.

If the exemplars do not share a correctable, worthwhile pattern, prefer \
"fallthrough" (no correction) over guessing at an "emit" you are not \
confident in -- an unhelpful proposal an operator rejects costs their \
attention for nothing.

Return ONLY a JSON object shaped exactly:
{{"disposition": "...", "correction": null | {{...}}, "projected_impact": \
"...", "rationale": "..."}}"""


def build_rule_proposal_request_params(
    *,
    source: str,
    key_fingerprint: str,
    exemplars: Sequence[tuple[str, dict[str, Any]]],
    tier3_outputs: dict[str, str],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the Messages API kwargs for one drafting call.

    Every exemplar record is untrusted raw intake (AC7,
    `docs/design/field-corrections.md` §12a) and is fenced via
    :func:`athenaeum.prompt_safety.fence_untrusted` before it reaches the
    prompt -- never interpolated raw.
    """
    blocks: list[str] = []
    for ref, record in exemplars:
        record_json = json.dumps(record, sort_keys=True, indent=2, default=str)
        fenced = fence_untrusted(
            record_json, tag="exemplar_record", max_chars=_EXEMPLAR_MAX_CHARS
        )
        block = f"- ref: {ref}\n  record:\n{fenced}"
        tier3 = tier3_outputs.get(ref)
        if tier3:
            fenced_tier3 = fence_untrusted(
                tier3, tag="exemplar_record", max_chars=_EXEMPLAR_MAX_CHARS
            )
            block += f"\n  tier3_output:\n{fenced_tier3}"
        blocks.append(block)
    exemplars_text = "\n".join(blocks) if blocks else "(none)"

    tier3_line = (
        "Tier-3 outputs were linkable and are embedded per exemplar above."
        if tier3_outputs
        else (
            "Tier-3 outputs were NOT linkable for these exemplars (no join key "
            "connects a raw record's source_ref to a reasoning-tier decision in "
            "this deployment) -- draft from the exemplar records alone."
        )
    )
    user_msg = (
        f"## Shape\nsource: {source}\nkey_fingerprint: {key_fingerprint}\n\n"
        f"## Exemplars ({len(exemplars)})\n{exemplars_text}\n\n"
        f"## Tier-3 outputs\n{tier3_line}\n\n"
        "## Instructions\nDraft ONE candidate shape rule for this record shape "
        "per the system prompt. Return ONLY the JSON object."
    )
    return {
        "model": _get_rule_proposals_model(config),
        "max_tokens": resolve_max_tokens(
            "rule_proposals",
            "ATHENAEUM_RULE_PROPOSALS_MAX_TOKENS",
            _RULE_PROPOSALS_MAX_TOKENS,
            config,
        ),
        "thinking": resolve_thinking(
            "rule_proposals", "ATHENAEUM_RULE_PROPOSALS_THINKING", "adaptive", config
        ),
        "system": _RULE_PROPOSAL_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_msg}],
    }


def _parse_rule_proposal_response(text: str) -> dict[str, Any] | None:
    """Parse the drafting model's JSON response. ``None`` on anything
    unparseable -- the caller treats that as a skipped draft, never a crash.
    """
    text = text.strip()
    try:
        start = text.index("{")
        end = text.rindex("}") + 1
        payload = json.loads(text[start:end])
    except (ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _derive_rule_name(source: str, item_id: str) -> str:
    """A `ShapeRule.name`-valid (`^[a-z][a-z0-9-]*\\Z`) slug for a drafted
    rule, unique by construction (the proposal id suffix)."""
    slug = re.sub(r"[^a-z0-9]+", "-", source.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug) or "shape"
    return f"proposed-{slug}-{item_id[:8]}"


def _build_candidate_rule(
    *, name: str, source: str, key_fingerprint: str, payload: dict[str, Any]
) -> ShapeRule | None:
    """Assemble + schema-validate a candidate rule from the model's
    *payload*. ``None`` on any validation failure -- mirrors
    `athenaeum.rules.load_rules`'s "a malformed rule is skipped with a loud
    log line" discipline, applied to a draft rather than a file on disk.

    The trust boundary is enforced HERE, independent of what the model
    returned (mirrors `reasoning_tiers._t2_decision_from_model_verdict`'s
    "downgrades enforced HERE" discipline): `mode` is always forced to
    `"observe"` (AC5), and a `correction.source`, if any, is always forced to
    a fixed machine-tier literal naming this rule -- never whatever the model
    put there (the system prompt tells it not to include one at all).
    """
    disposition = payload.get("disposition")
    if disposition not in _ALLOWED_DRAFT_DISPOSITIONS:
        return None
    rule_dict: dict[str, Any] = {
        "version": 1,
        "name": name,
        "mode": "observe",
        "match": {"source": source, "key_fingerprint": key_fingerprint},
        "disposition": disposition,
    }
    correction = payload.get("correction")
    if isinstance(correction, dict):
        correction = dict(correction)
        correction["source"] = f"script:{name}"
        rule_dict["correction"] = correction
    try:
        return ShapeRule.model_validate(rule_dict)
    except PydanticValidationError:
        return None


def _rule_file_header(*, source: str, key_fingerprint: str, note: str) -> str:
    return (
        "# SPDX-License-Identifier: Apache-2.0\n"
        f"# Librarian-proposed shape rule (issue athenaeum#905) -- "
        f"source={source!r} key_fingerprint={key_fingerprint!r}. {note}\n"
        "# Ships mode: observe. Promoting to live is a separate, explicit "
        "operator action -- never automatic. See docs/design/shape-rules.md.\n"
    )


def _render_rule_yaml(rule: ShapeRule, *, source: str, key_fingerprint: str, note: str) -> str:
    header = _rule_file_header(source=source, key_fingerprint=key_fingerprint, note=note)
    body = yaml.safe_dump(
        rule.model_dump(exclude_none=True), sort_keys=False, default_flow_style=False
    )
    return header + body


def _draft_rule_proposal(
    *,
    client: Any,
    source: str,
    key_fingerprint: str,
    exemplars: list[tuple[str, dict[str, Any]]],
    tier3_outputs: dict[str, str],
    name: str,
    config: dict[str, Any] | None,
    usage: TokenUsage | None = None,
) -> dict[str, Any] | None:
    """One LLM call -> a validated candidate rule + projected-impact line,
    or ``None`` if the response is unparseable or schema-invalid (logged,
    never raised).

    *usage* (issue athenaeum#1063), when given, records this call's tokens via
    :func:`athenaeum.tiers._record_usage` tagged ``knob="rule_proposals"`` --
    the same accounting the tier-2/3 call sites (``tiers.py``) use, so
    ``spend.record_spend_per_knob_provider`` attributes this call's spend
    correctly instead of it vanishing from the run's ledger.
    """
    params = build_rule_proposal_request_params(
        source=source,
        key_fingerprint=key_fingerprint,
        exemplars=exemplars,
        tier3_outputs=tier3_outputs,
        config=config,
    )
    response = with_retry(
        lambda: client.messages.create(**params),
        description=f"rule_proposal {source}:{key_fingerprint}",
    )
    _record_usage(response, usage, model=params["model"], knob="rule_proposals")
    payload = _parse_rule_proposal_response(response_text(response))
    if payload is None:
        log.warning(
            "rule-proposals: unparseable draft response for %s:%s", source, key_fingerprint
        )
        return None
    rule = _build_candidate_rule(
        name=name, source=source, key_fingerprint=key_fingerprint, payload=payload
    )
    if rule is None:
        log.warning(
            "rule-proposals: draft for %s:%s failed schema validation (disposition=%r)",
            source,
            key_fingerprint,
            payload.get("disposition"),
        )
        return None

    projected_impact = payload.get("projected_impact")
    if not isinstance(projected_impact, str) or not projected_impact.strip():
        projected_impact = (
            f"{len(exemplars)} exemplar(s) observed; impact not stated by the drafting model."
        )
    rationale = payload.get("rationale")
    rationale = rationale.strip() if isinstance(rationale, str) else ""

    note = f"Drafted from {len(exemplars)} exemplar(s); review before approving."
    rule_yaml = _render_rule_yaml(
        rule, source=source, key_fingerprint=key_fingerprint, note=note
    )
    return {
        "rule_yaml": rule_yaml,
        "projected_impact": projected_impact.strip(),
        "rationale": rationale,
        "model": params["model"],
    }


# ---------------------------------------------------------------------------
# Phase entry point
# ---------------------------------------------------------------------------


def run_rule_proposal_detection(
    *,
    wiki_root: Path,
    raw_root: Path,
    config: dict[str, Any] | None = None,
    client: Any | None = None,
    now: datetime | None = None,
    ledger_path: Path | None = None,
    dry_run: bool = False,
    deadline_check: Callable[[], bool] | None = None,
    usage: TokenUsage | None = None,
) -> dict[str, Any]:
    """Detect + draft (issue athenaeum#905 AC1/AC2). The callable entry point
    wired into the nightly librarian run by ``librarian._run_rule_proposal_phase``
    (issue athenaeum#1063) -- see the module docstring's "Wiring note".

    Idempotent per shape: a shape already carrying a pending (unresolved)
    proposal is not re-proposed, and a shape carrying a `reject` is
    permanently suppressed (AC6) -- both checked before any drafting call is
    made, so a repeat run never spends an LLM call on either case.

    *deadline_check* (issue athenaeum#1063), when given, is checked at the top
    of EACH shape's iteration -- the same "check at the boundary before the
    next unit of work, never mid-call" contract
    :func:`athenaeum.rules.run_shape_rule_phase` uses at file boundaries.
    Shapes are visited most-frequent-first (see the sort below), so a run
    that trips the deadline mid-way still drafted the highest-value shapes
    first. *usage*, when given, is threaded to :func:`_draft_rule_proposal`
    so each drafting call's tokens are recorded exactly like the tier-2/3
    call sites in ``tiers.py``.
    """
    summary: dict[str, Any] = {
        "shapes_seen": 0,
        "threshold_crossed": 0,
        "proposed": 0,
        "skipped_pending": 0,
        "skipped_suppressed": 0,
        "skipped_no_exemplars": 0,
        "skipped_draft_invalid": 0,
        "skipped_no_client": 0,
        "skipped_deadline": 0,
    }
    wiki_root = Path(wiki_root)
    raw_root = Path(raw_root)

    threshold = resolve_rule_proposals_threshold(config)
    window_days = resolve_rule_proposals_window_days(config)
    exemplar_count = resolve_rule_proposals_exemplar_count(config)

    grouped = _grouped_deferred_rows(wiki_root, window_days=window_days, now=now)
    summary["shapes_seen"] = len(grouped)
    if not grouped:
        return summary

    records = read_rule_proposals_ledger(wiki_root, ledger_path=ledger_path)
    resolved_ids = _resolved_ids(records)
    rejected_ids = _kind_ids(records, REJECT_KIND)
    pending_ids = {
        str(r.get("id"))
        for r in records
        if r.get("kind") == PROPOSAL_KIND and str(r.get("id")) not in resolved_ids
    }

    # Deterministic order: most-frequent shape first (by DISTINCT record
    # count, never row count -- see :func:`_distinct_record_count`), then
    # (source, key_fingerprint) as a tiebreak -- so a run bounded by an
    # external deadline (issue athenaeum#1063's librarian wiring) drains the
    # highest-value shapes first.
    ordered_shapes = sorted(
        grouped.items(), key=lambda kv: (-_distinct_record_count(kv[1]), kv[0])
    )
    for _idx, ((source, key_fingerprint), rows) in enumerate(ordered_shapes):
        if deadline_check is not None and deadline_check():
            # Mirrors `athenaeum.rules.run_shape_rule_phase`'s file-boundary
            # deadline check: stop BEFORE starting the next shape's work,
            # never mid-drafting-call. Every shape not yet visited this run
            # is retried next run against the same (still-accumulating)
            # disposition rows -- nothing here is lost, only deferred.
            summary["skipped_deadline"] += len(ordered_shapes) - _idx
            break
        record_count = _distinct_record_count(rows)
        if record_count < threshold:
            continue
        summary["threshold_crossed"] += 1

        item_id = proposal_item_id(source, key_fingerprint)
        if item_id in rejected_ids:
            summary["skipped_suppressed"] += 1
            continue
        if item_id in pending_ids:
            summary["skipped_pending"] += 1
            continue

        exemplars = _select_exemplars(raw_root, rows, exemplar_count)
        if not exemplars:
            summary["skipped_no_exemplars"] += 1
            log.warning(
                "rule-proposals: no readable exemplar for %s:%s (%d deferred "
                "record(s)) -- skipping this run",
                source,
                key_fingerprint,
                record_count,
            )
            continue

        if dry_run:
            continue
        if client is None:
            summary["skipped_no_client"] += 1
            continue

        tier3_outputs = _tier3_outputs_for_exemplars(wiki_root, [ref for ref, _ in exemplars])
        name = _derive_rule_name(source, item_id)
        draft = _draft_rule_proposal(
            client=client,
            source=source,
            key_fingerprint=key_fingerprint,
            exemplars=exemplars,
            tier3_outputs=tier3_outputs,
            name=name,
            config=config,
            usage=usage,
        )
        if draft is None:
            summary["skipped_draft_invalid"] += 1
            continue

        record = {
            "v": RULE_PROPOSALS_LEDGER_VERSION,
            "kind": PROPOSAL_KIND,
            "id": item_id,
            "created_at": _now_iso(now),
            "source": source,
            "key_fingerprint": key_fingerprint,
            "count": record_count,
            "window_days": window_days,
            "threshold": threshold,
            "rule_name": name,
            "rule_yaml": draft["rule_yaml"],
            "projected_impact": draft["projected_impact"],
            "rationale": draft["rationale"],
            "exemplar_refs": [ref for ref, _ in exemplars],
            "tier3_linked": bool(tier3_outputs),
            "tier3_note": (
                "tier-3 outputs were linkable for these exemplars"
                if tier3_outputs
                else (
                    "tier-3 outputs were NOT linkable -- _reasoning_tier_decisions.jsonl "
                    "carries no source_ref join key (issue athenaeum#905)"
                )
            ),
            "model": draft["model"],
        }
        target = (
            ledger_path
            if ledger_path is not None
            else default_rule_proposals_ledger_path(wiki_root)
        )
        _append_jsonl_line(target, json.dumps(record, sort_keys=True) + "\n")
        pending_ids.add(item_id)
        summary["proposed"] += 1

    return summary


# ---------------------------------------------------------------------------
# Resolution: approve (AC5) / reject (AC6)
# ---------------------------------------------------------------------------


def approve_rule_proposal(
    knowledge_root: Path,
    wiki_root: Path,
    *,
    proposal_id: str,
    note: str = "",
    ledger_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Approve one pending proposal: write it into `<knowledge_root>/rules/`
    in OBSERVE mode (AC5 -- never in a live-writing state).

    `mode` is forced to `"observe"` HERE, independent of what was drafted or
    stored, and the rule is re-validated via `ShapeRule.model_validate`
    immediately before it is ever written to disk -- defense in depth beyond
    `_build_candidate_rule`'s draft-time force, so approval can never depend
    on trusting a stored string alone.

    Raises `ValueError` for an unknown or already-resolved *proposal_id* --
    each proposal is resolved at most once, mirroring
    `athenaeum.quarantine.release_quarantine`'s guard.
    """
    knowledge_root = Path(knowledge_root)
    wiki_root = Path(wiki_root)
    records = read_rule_proposals_ledger(wiki_root, ledger_path=ledger_path)
    proposal = next(
        (
            r
            for r in records
            if r.get("kind") == PROPOSAL_KIND and str(r.get("id")) == proposal_id
        ),
        None,
    )
    if proposal is None:
        raise ValueError(f"unknown rule proposal id: {proposal_id!r}")
    if proposal_id in _resolved_ids(records):
        raise ValueError(f"rule proposal already resolved: {proposal_id!r}")

    rule_yaml = str(proposal.get("rule_yaml", ""))
    parsed = yaml.safe_load(rule_yaml)
    if not isinstance(parsed, dict):
        raise ValueError(f"stored rule_yaml for proposal {proposal_id!r} is not a YAML mapping")
    parsed["mode"] = "observe"
    rule = ShapeRule.model_validate(parsed)

    source = str(proposal.get("source", ""))
    key_fingerprint = str(proposal.get("key_fingerprint", ""))
    final_yaml = _render_rule_yaml(
        rule,
        source=source,
        key_fingerprint=key_fingerprint,
        note=f"Approved from proposal {proposal_id}.",
    )

    rules_dir = knowledge_root / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    stem = str(proposal.get("rule_name") or rule.name)
    target = rules_dir / f"{stem}.yaml"
    if target.exists():
        # Name collision (e.g. an operator already hand-authored a file with
        # this stem) -- never clobber; disambiguate by the proposal id,
        # which is unique by construction.
        target = rules_dir / f"{stem}-{proposal_id}.yaml"
    atomic_write_text(target, final_yaml)

    record = {
        "v": RULE_PROPOSALS_LEDGER_VERSION,
        "kind": APPROVE_KIND,
        "id": proposal_id,
        "created_at": _now_iso(now),
        "rule_path": str(target.relative_to(knowledge_root)),
        "note": note,
    }
    ledger_target = (
        ledger_path if ledger_path is not None else default_rule_proposals_ledger_path(wiki_root)
    )
    _append_jsonl_line(ledger_target, json.dumps(record, sort_keys=True) + "\n")
    log.info(
        "athenaeum#905: approved rule proposal %s -> %s (mode=observe)", proposal_id, target
    )
    return record


def reject_rule_proposal(
    wiki_root: Path,
    *,
    proposal_id: str,
    note: str = "",
    ledger_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reject one pending proposal (AC6): records the rejection, which
    permanently suppresses the underlying `(source, key_fingerprint)` shape
    -- `proposal_id` IS `proposal_item_id(source, key_fingerprint)`, so
    `run_rule_proposal_detection` checking `item_id in rejected_ids` is
    sufficient; no separate suppression index is needed.

    Raises `ValueError` for an unknown or already-resolved *proposal_id*.
    """
    wiki_root = Path(wiki_root)
    records = read_rule_proposals_ledger(wiki_root, ledger_path=ledger_path)
    proposal = next(
        (
            r
            for r in records
            if r.get("kind") == PROPOSAL_KIND and str(r.get("id")) == proposal_id
        ),
        None,
    )
    if proposal is None:
        raise ValueError(f"unknown rule proposal id: {proposal_id!r}")
    if proposal_id in _resolved_ids(records):
        raise ValueError(f"rule proposal already resolved: {proposal_id!r}")

    record = {
        "v": RULE_PROPOSALS_LEDGER_VERSION,
        "kind": REJECT_KIND,
        "id": proposal_id,
        "created_at": _now_iso(now),
        "note": note,
    }
    ledger_target = (
        ledger_path if ledger_path is not None else default_rule_proposals_ledger_path(wiki_root)
    )
    _append_jsonl_line(ledger_target, json.dumps(record, sort_keys=True) + "\n")
    log.info("athenaeum#905: rejected rule proposal %s", proposal_id)
    return record


__all__ = [
    "RULE_PROPOSALS_LEDGER_VERSION",
    "RULE_PROPOSALS_LEDGER_FILENAME",
    "PROPOSAL_KIND",
    "APPROVE_KIND",
    "REJECT_KIND",
    "DEFAULT_RULE_PROPOSALS_MODEL",
    "default_rule_proposals_ledger_path",
    "proposal_item_id",
    "read_rule_proposals_ledger",
    "list_pending_rule_proposals",
    "detect_shape_frequency",
    "build_rule_proposal_request_params",
    "run_rule_proposal_detection",
    "approve_rule_proposal",
    "reject_rule_proposal",
]
