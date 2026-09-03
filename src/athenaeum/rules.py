# SPDX-License-Identifier: Apache-2.0
"""Shape-rule engine (issue athenaeum#901): declarative YAML rules that
recognise a foreign record shape and compile it into a
`docs/field-corrections.md`-conformant correction batch.

Rules are DATA, not code. They live at `<knowledge-root>/rules/*.yaml`, are
loaded and pydantic-schema-validated at run start (:func:`load_rules`), and
a malformed rule is skipped with a loud log line — its matching files then
take the ordinary tiered ladder, exactly as if no rule existed for them.

**Match** (:class:`MatchSpec`): source directory, format, filename glob,
record key-fingerprint (:func:`record_key_fingerprint`), and per-field
predicates (:class:`FieldPredicate` — exact / glob / list membership).

**Transform** (:class:`CorrectionSpec`, :func:`resolve_value_expr`): field
interpolation (`"$name"` — whole-value substitution only) plus EXACTLY the
closed function vocabulary in :data:`KNOWN_FUNCTIONS` (`set_diff`, `first`,
`date_of`). An unknown function name fails validation
(:func:`_validate_no_unknown_fn`). **Nothing here is ever `eval`'d or run
through a templating language** — :func:`resolve_value_expr` is a small,
fixed, code-owned interpreter over already-`yaml.safe_load`'d data.

**Dispositions** (`emit`/`fallthrough` are athenaeum#901; `drop`/`retain`/
`rollup` athenaeum#903; `preserve` athenaeum#837):
`emit` writes a correction batch in the ONE conformance format
(`docs/field-corrections.md` §3.2), consumed by the existing correction
machinery (:mod:`athenaeum.corrections`) with NO CHANGES to it — the batch
lands in the SAME ordinary `raw/<source>/` tree that machinery already
scans. `fallthrough` explicitly leaves the record for the reasoning tiers
(it is simply not intercepted).

**Guard:** a rule's asserted `correction.source` is capped at machine tier
(`script:` / `api:`, :data:`MACHINE_TIER_SOURCE_TYPES`) — validated at rule
LOAD time, which is why `source` must be a literal (never a `$field`
reference or function call; see "Decisions" below).

**Observe mode:** every rule ships `mode: observe` by default — "the
required first state for any new or edited rule". Observe mode computes and
ledgers (:func:`append_shape_rules_ledger`) what the rule WOULD have done
while writing nothing else (no correction batch, no retirement).

Layering: L3. Imports :mod:`athenaeum.intake` and :mod:`athenaeum.corrections`
(both L2) plus :mod:`athenaeum.models`, :mod:`athenaeum.provenance`,
:mod:`athenaeum.config`, :mod:`athenaeum.atomic_io`. Neither `intake` nor
`corrections` imports this module, so no cycle: this module is a CONSUMER of
both, not a peer either needs to reach back into.

**Decisions this MVP makes** (documented here, not silently baked in — same
convention `corrections.py`'s module docstring uses):

- **Record extraction.** For a `.md` raw file, the matchable "record" is its
  FRONTMATTER dict (`models.parse_frontmatter`'s `meta`) — body text is not
  matchable by field predicates. For a `.jsonl` raw file, the record is the
  FIRST LINE parsed as a JSON object — mirrors
  `corrections.parse_batch_envelope`'s own "read the first line" streaming
  discipline. A multi-record-per-file foreign export is a documented
  limitation, not a silent gap. Anything else (unparseable content, empty
  file, a non-object first line) yields an empty record `{}`: no field
  predicate or key-fingerprint can match it, so the file falls through to
  the ordinary tiered ladder untouched — `docs/field-corrections.md` §1.1's
  "conformance sets HOW DEEP, never WHETHER" rule, applied one layer up.
- **Field interpolation is whole-value only.** `"$name"` substitutes
  `record["name"]` verbatim (any type). Embedding a field inside a larger
  literal string (`"prefix $name suffix"`) is NOT supported — that would
  require a string-scanning mini-parser, which is one step from a
  templating language. Whole-value substitution is the narrowest thing that
  still satisfies "field interpolation" and keeps the "no templating
  language" guarantee unambiguous.
- **`correction.source` must be a literal string**, never a `$field`
  reference or function call. The machine-tier guard is enforced at rule
  LOAD time (schema validation, "run start") so it can reject a
  precedence-escalation attempt before any record is ever processed; a
  dynamic source would defeat that entirely.
- **First-match-wins**, rules evaluated in the sorted-by-filename order
  `load_rules` returns. A record matches at most one rule's disposition per
  run.
- **A compiled batch lands in the raw file's OWN `<source>/` directory**,
  not a dedicated `raw/shape-rules/` directory — keeps the compiled
  correction grouped with where the original foreign record arrived, and
  needs no new non-intake-sources config to stay invisible to the ordinary
  ladder (a `.jsonl` batch is already skip-by-shape there).
- **The per-run bound counts candidate files EVALUATED against rules**
  (matched or not), not just ones that emitted — the conservative reading
  of "mirrors the existing correction bounds", since evaluation itself
  (a raw-file read) is the actual per-run cost this phase incurs.
- **Retirement of a compiled raw file uses its own git-commit wording**
  (:func:`retire_compiled_raw_file`) rather than reusing
  `corrections.retire_batch` verbatim — that function's commit messages are
  batch-specific ("field-correction batch retired"), which would misdescribe
  an ordinary raw-intake file compiled away by a shape rule in `git log`.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import re
import shutil
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, NamedTuple

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError

from athenaeum.atomic_io import atomic_write_text
from athenaeum.compiled_exempt import mark_exempt
from athenaeum.config import (
    resolve_preserved_log_adapter,
    resolve_preserved_log_dir,
    resolve_shape_rules_dispositions_retention_days,
    resolve_shape_rules_log_no_match,
    resolve_shape_rules_max_records_per_run,
)
from athenaeum.corrections import compute_correction_id
from athenaeum.intake import discover_raw_files, discover_shape_rule_extra_intake_files
from athenaeum.models import (
    MEMORY_BUCKETS,
    RawFile,
    RawFileTooLargeError,
    parse_frontmatter,
)
from athenaeum.provenance import parse_source
from athenaeum.storage import StorageConfigError, available_adapters, resolve_store_for_class
from athenaeum.store import (
    FilesystemStore,
    Store,
    StoreConflictError,
    StoreKey,
    append_line_durable,
    now_iso,
)

log = logging.getLogger(__name__)

#: The closed transform-function vocabulary (issue athenaeum#901 AC). An
#: unknown function name anywhere in a rule's `value`/`target`/`observed_at`/
#: `note` fails schema validation — see :func:`_validate_no_unknown_fn`.
KNOWN_FUNCTIONS: frozenset[str] = frozenset({"set_diff", "first", "date_of"})

#: Source-type tokens a rule's `correction.source` may assert (the "machine
#: tier" guard). Matches `precedence.SOURCE_PRECEDENCE_TIERS`' `api` (rank 3)
#: and `script` (rank 7) tokens exactly — a rule can never assert `user:` or
#: any other tier's precedence.
MACHINE_TIER_SOURCE_TYPES: frozenset[str] = frozenset({"script", "api"})

#: The three §3.3 target shapes a correction's `target` mapping's KEY SET
#: must match exactly (values may be literal or `$field`/`fn` expressions).
_TARGET_KEY_SHAPES: tuple[frozenset[str], ...] = (
    frozenset({"uid"}),
    frozenset({"type", "name"}),
    frozenset({"type", "handle"}),
)

_FIELD_REF_RE = re.compile(r"^\$([A-Za-z_][A-Za-z0-9_]*)\Z")


# ---------------------------------------------------------------------------
# Closed function vocabulary -- validation-time check
# ---------------------------------------------------------------------------


def _validate_no_unknown_fn(node: Any, *, path: str = "") -> None:
    """Recursively walk a parsed-YAML value looking for `{"fn": ...}`
    function-call nodes and reject any whose `fn` is outside
    :data:`KNOWN_FUNCTIONS`. Raises :class:`ValueError` (caught by the
    calling pydantic `field_validator`, becoming a normal
    `pydantic.ValidationError`) — this is the single choke point every
    value-bearing `CorrectionSpec` field routes through, so "unknown
    function name fails validation" holds uniformly rather than per-field.
    """
    if isinstance(node, dict):
        if "fn" in node:
            fn = node.get("fn")
            if fn not in KNOWN_FUNCTIONS:
                raise ValueError(
                    f"unknown transform function {fn!r} at "
                    f"{path or '<root>'} -- must be one of "
                    f"{sorted(KNOWN_FUNCTIONS)}"
                )
            args = node.get("args", [])
            if not isinstance(args, list):
                raise ValueError(f"fn {fn!r} args at {path} must be a list")
            for i, a in enumerate(args):
                _validate_no_unknown_fn(a, path=f"{path}.args[{i}]")
            extra = set(node.keys()) - {"fn", "args"}
            if extra:
                raise ValueError(
                    f"fn node at {path} has unexpected keys: {sorted(extra)}"
                )
        else:
            for k, v in node.items():
                _validate_no_unknown_fn(v, path=f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _validate_no_unknown_fn(v, path=f"{path}[{i}]")
    # Scalars need no further check -- `$field` syntax has no parse failure
    # mode (any string is either a literal or a reference), validated at
    # resolve time instead (a missing referenced field raises there).


# ---------------------------------------------------------------------------
# Match: field predicates
# ---------------------------------------------------------------------------


class FieldPredicate(BaseModel):
    """One field-match predicate — exactly one of `exact` / `glob` / `in`
    (issue athenaeum#901 AC: "field predicates (exact, glob, list membership)").
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    exact: Any = None
    glob: str | None = None
    in_: list[Any] | None = Field(default=None, alias="in")

    @model_validator(mode="after")
    def _exactly_one(self) -> "FieldPredicate":
        set_count = sum(x is not None for x in (self.exact, self.glob, self.in_))
        if set_count != 1:
            raise ValueError(
                "field predicate must set exactly one of exact/glob/in, "
                f"got {set_count}"
            )
        if self.glob is not None and not isinstance(self.glob, str):
            raise ValueError("field predicate 'glob' must be a string")
        if self.in_ is not None and not isinstance(self.in_, list):
            raise ValueError("field predicate 'in' must be a list")
        return self

    def matches(self, value: Any) -> bool:
        if self.glob is not None:
            return isinstance(value, str) and fnmatch.fnmatchcase(value, self.glob)
        if self.in_ is not None:
            return value in self.in_
        return value == self.exact


class MatchSpec(BaseModel):
    """`match:` block. Every key is optional; an omitted key matches
    anything. All present keys must hold (AND) for the rule to match.

    ``unclaimed`` (issue athenaeum#1133) opts a rule INTO matching candidates
    from :func:`athenaeum.intake_audit.discover_unclaimed_shape_rule_candidates`
    -- files the intake audit (issue athenaeum#836) reports as unrecognised,
    which by definition have neither a parseable frontmatter/JSONL record
    nor an extension `discover_raw_files` would ever claim. Explicit opt-in,
    never inferred from `format: null`: `format` is already optional for
    ORDINARY rules (any `.md`/`.jsonl` candidate, unclaimed or not), so
    treating an absent `format` as "this rule wants unclaimed files" would
    misfire on every existing rule that simply doesn't care about format.
    Default `False` so every rule written before this issue keeps matching
    only ordinary (claimed) candidates, unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    source: str | None = None
    format: Literal["md", "jsonl"] | None = None
    filename_glob: str | None = None
    key_fingerprint: str | None = None
    fields: dict[str, FieldPredicate] = Field(default_factory=dict)
    unclaimed: bool = False

    @field_validator("key_fingerprint")
    @classmethod
    def _validate_fingerprint(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not re.match(r"^[0-9a-f]{16}\Z", v):
            raise ValueError(
                f"key_fingerprint must be 16 lowercase hex chars, got {v!r}"
            )
        return v

    @model_validator(mode="after")
    def _validate_unclaimed_compat(self) -> "MatchSpec":
        # Issue athenaeum#1133 AC4: a `.txt`/`.json` unclaimed candidate has no
        # parseable record -- `fields`/`key_fingerprint` would resolve
        # `(False, None)` against `{}` forever and the rule would silently
        # never match. `format` is a permanently-unsatisfiable trap for the
        # same reason: it is typed `Literal["md","jsonl"] | None`, so it can
        # never equal an unclaimed file's actual extension. All three are
        # caught HERE, at load time, rather than left to silently never
        # match at evaluation time.
        if self.unclaimed:
            if self.fields:
                raise ValueError(
                    "match.unclaimed rules cannot use match.fields (no "
                    "record exists to match against) -- use "
                    "match.filename_glob instead"
                )
            if self.key_fingerprint is not None:
                raise ValueError(
                    "match.unclaimed rules cannot use match.key_fingerprint "
                    "(no record exists to match against) -- use "
                    "match.filename_glob instead"
                )
            if self.format is not None:
                raise ValueError(
                    "match.unclaimed rules cannot use match.format (an "
                    "unclaimed candidate's extension is never .md/.jsonl -- "
                    "that is why it is unclaimed) -- use "
                    "match.filename_glob instead"
                )
        return self

    def matches(
        self,
        *,
        raw: RawFile,
        record: dict[str, Any],
        fmt: str,
        is_unclaimed: bool = False,
    ) -> bool:
        # Issue athenaeum#1133: a hard partition, not a documentation hint --
        # without it, a rule with an otherwise-empty `match:` block (every
        # key above is optional) would match every candidate of BOTH kinds.
        if self.unclaimed != is_unclaimed:
            return False
        if self.source is not None and raw.source != self.source:
            return False
        if self.format is not None and fmt != self.format:
            return False
        if self.filename_glob is not None and not fnmatch.fnmatchcase(
            raw.path.name, self.filename_glob
        ):
            return False
        if (
            self.key_fingerprint is not None
            and record_key_fingerprint(record) != self.key_fingerprint
        ):
            return False
        for field_name, predicate in self.fields.items():
            found, value = resolve_field_path(record, field_name)
            if not found or not predicate.matches(value):
                return False
        return True


def resolve_field_path(record: dict[str, Any], field_name: str) -> tuple[bool, Any]:
    """Resolve a `match.fields` key against *record*, returning
    `(found, value)`.

    Issue athenaeum#974 AC1: a `match.fields` key may now reach a NESTED
    frontmatter/JSON key one level (or more) below the record root, using a
    dotted path (`"a.b"` -> ``record["a"]["b"]``). This codebase has NO
    existing precedent for nested-key resolution (grepped for
    dotted-path/nested-key helpers before writing this — none found), so the
    syntax is a fresh choice, not a match to something already idiomatic
    here. Dotted-path is the conventional external choice for this exact
    problem (jq/dpath-style addressing) and — the deciding property — it
    cannot collide with an ordinary top-level frontmatter key: those are
    plain identifiers, and a literal `.` inside a YAML frontmatter key would
    itself be unusual. That makes it the conservative, reversible pick this
    issue's design-judgement note asks for when no repo precedent exists.

    **Backward compatibility (non-negotiable, per issue athenaeum#974):** an
    EXACT top-level key match always wins first, dots and all. Only when
    *field_name* is not itself a literal top-level key AND contains at least
    one `.` does this walk the dotted path into nested dicts. This means:

    - every pre-existing rule's plain (non-dotted) `fields` key resolves
      exactly as it always did — a single top-level ``record[field_name]``
      lookup, unchanged;
    - the vanishingly rare top-level key that happens to itself contain a
      literal `.` still resolves as that exact top-level key first, so this
      change cannot silently reinterpret an existing rule's meaning;
    - a genuinely nested field (absent at top level) resolves via the
      dotted path, e.g. `"metadata.log_group"` -> `record["metadata"]["log_group"]`.

    Returns `(False, None)` if the path cannot be walked (a missing key at
    any level, or a non-dict value in the middle of the path) — the caller
    treats that exactly like "field absent from record" always has: no
    match, never a crash.
    """
    if field_name in record:
        return True, record[field_name]
    if "." in field_name:
        current: Any = record
        for part in field_name.split("."):
            if not isinstance(current, dict) or part not in current:
                return False, None
            current = current[part]
        return True, current
    return False, None


def record_key_fingerprint(record: dict[str, Any]) -> str:
    """Stable, order-independent fingerprint of a record's TOP-LEVEL KEY SET
    (not values) — lets a rule match "records shaped like X" without hand
    coding key order. Same construction as
    :func:`athenaeum.corrections.compute_correction_id` (sha256 of a
    canonical-JSON payload, hex, truncated to 16 chars) for a consistent
    audit-id convention across the two modules.
    """
    payload = json.dumps(sorted(record.keys()), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Transform: the closed-vocabulary interpreter
# ---------------------------------------------------------------------------


class ShapeRuleTransformError(Exception):
    """Raised by :func:`resolve_value_expr` (or a function in
    :data:`KNOWN_FUNCTIONS`) when a value expression cannot be resolved
    against a matched record — a missing `$field`, wrong-shaped function
    args, an unparseable `date_of` input. The caller treats this as a
    per-record FALLTHROUGH, never a crash and never a silently-wrong write:
    `docs/field-corrections.md` §1.1's "nothing is rejected, every failure
    to conform is a fallthrough" doctrine, applied to the compiler as well
    as the applier.
    """


def _fn_first(args: list[Any]) -> Any:
    if len(args) != 1:
        raise ShapeRuleTransformError(f"first() takes exactly 1 arg, got {len(args)}")
    (seq,) = args
    if not isinstance(seq, list):
        raise ShapeRuleTransformError(
            f"first() arg must be a list, got {type(seq).__name__}"
        )
    return seq[0] if seq else None


def _fn_set_diff(args: list[Any]) -> Any:
    if len(args) != 2:
        raise ShapeRuleTransformError(
            f"set_diff() takes exactly 2 args, got {len(args)}"
        )
    a, b = args
    if not isinstance(a, list) or not isinstance(b, list):
        raise ShapeRuleTransformError("set_diff() args must both be lists")
    b_keys = {repr(x) for x in b}
    return [x for x in a if repr(x) not in b_keys]


def _fn_date_of(args: list[Any]) -> Any:
    if len(args) != 1:
        raise ShapeRuleTransformError(f"date_of() takes exactly 1 arg, got {len(args)}")
    (value,) = args
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if not isinstance(value, str) or not value.strip():
        raise ShapeRuleTransformError(
            f"date_of() arg must be a non-empty string, got {value!r}"
        )
    text = value.strip()
    try:
        if re.match(r"^\d{4}-\d{2}-\d{2}\Z", text):
            return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
        return datetime.fromisoformat(normalized).date().isoformat()
    except ValueError as exc:
        raise ShapeRuleTransformError(f"date_of() could not parse {value!r}: {exc}") from exc


_FUNCTIONS: dict[str, Callable[[list[Any]], Any]] = {
    "first": _fn_first,
    "set_diff": _fn_set_diff,
    "date_of": _fn_date_of,
}
assert frozenset(_FUNCTIONS) == KNOWN_FUNCTIONS, (
    "the _FUNCTIONS implementation table must match the closed vocabulary "
    "exactly -- an implemented function missing from KNOWN_FUNCTIONS could "
    "never be validated in, and one listed but unimplemented would raise a "
    "confusing KeyError instead of ShapeRuleTransformError at resolve time"
)


def resolve_value_expr(expr: Any, record: dict[str, Any]) -> Any:
    """Resolve one value-expression node against a matched *record*.

    A node is exactly one of:

    - a literal scalar/list/dict with no `$field` string and no `fn` key —
      returned structurally unchanged (containers are rebuilt, not shared,
      so a caller cannot mutate the rule's own parsed YAML through it);
    - a string of the EXACT form `"$name"` — whole-value substitution of
      `record["name"]` (raises :class:`ShapeRuleTransformError` if absent);
    - `{"fn": "<name>", "args": [...]}` — calls the closed-vocabulary
      function on the recursively-resolved args.

    This is a fixed, code-owned interpreter over already-`yaml.safe_load`'d
    data — no string template syntax, no partial-string substitution, and
    nothing here is ever passed to `eval`/`exec`/a templating engine.
    """
    if isinstance(expr, str):
        m = _FIELD_REF_RE.match(expr)
        if m:
            name = m.group(1)
            if name not in record:
                raise ShapeRuleTransformError(
                    f"referenced field {name!r} is absent from the record"
                )
            return record[name]
        return expr
    if isinstance(expr, dict):
        if "fn" in expr:
            fn_name = expr["fn"]
            fn = _FUNCTIONS.get(fn_name)
            if fn is None:
                # Unreachable via a schema-validated ShapeRule (validation
                # already rejects this at load time) -- defensive only, for
                # a caller that resolves a hand-built dict directly.
                raise ShapeRuleTransformError(f"unknown function {fn_name!r}")
            args = [resolve_value_expr(a, record) for a in expr.get("args", [])]
            return fn(args)
        return {k: resolve_value_expr(v, record) for k, v in expr.items()}
    if isinstance(expr, list):
        return [resolve_value_expr(v, record) for v in expr]
    return expr


# ---------------------------------------------------------------------------
# Rule schema
# ---------------------------------------------------------------------------


class CorrectionSpec(BaseModel):
    """The `emit` disposition's correction-record template
    (`docs/field-corrections.md` §3.2/§3.3). Every field except `op`/
    `field`/`source` may be a literal, a `$field` reference, or a closed-
    vocabulary function call — resolved per-record by
    :func:`resolve_value_expr` (see :func:`build_correction_record`).

    ``bucket`` / ``valid_until`` (issue athenaeum#904, AC2) are OPTIONAL sibling
    annotations on the correction record — same shape as ``usage_class``
    (`corrections.py`'s `ALLOWED_RECORD_KEYS`): they ride ALONGSIDE the
    record's `field`/`value` payload rather than going through the
    `field`/value allowlist+precedence machinery themselves. A rule that
    corrects one attribute on a matched record's target entity can ALSO tag
    that whole entity's page with a decay bucket / suggested expiry —
    decoupled from which specific attribute the correction is fixing. See
    ``corrections.process_correction_record`` for how these two are applied.
    """

    model_config = ConfigDict(extra="forbid")

    target: dict[str, Any]
    op: Literal["set", "add", "remove"]
    field: str
    value: Any
    source: str
    observed_at: Any = None
    note: Any = None
    bucket: str | None = None
    valid_until: Any = None

    @field_validator("field")
    @classmethod
    def _validate_field_name(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("correction.field must be a non-empty string")
        return v

    @field_validator("bucket")
    @classmethod
    def _validate_bucket(cls, v: str | None) -> str | None:
        """Reject-at-the-boundary (issue athenaeum#904 design constraint), enforced at
        RULE-LOAD time — the earliest possible boundary, before any record is
        ever processed. ``bucket`` is deliberately a plain literal (unlike
        `value`/`observed_at`/`note`, which may be `$field`-interpolated) —
        a rule's decay classification is a rule-authoring decision ("records
        this rule matches are daily status"), not a per-record computed
        value.
        """
        if v is None:
            return None
        if v not in MEMORY_BUCKETS:
            raise ValueError(
                f"correction.bucket {v!r} must be one of {sorted(MEMORY_BUCKETS)}"
            )
        return v

    @field_validator("valid_until")
    @classmethod
    def _validate_valid_until(cls, v: Any) -> Any:
        _validate_no_unknown_fn(v, path="valid_until")
        return v

    @field_validator("target")
    @classmethod
    def _validate_target(cls, v: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(v, dict) or not v:
            raise ValueError("correction.target must be a non-empty mapping")
        keys = frozenset(v.keys())
        if keys not in _TARGET_KEY_SHAPES:
            raise ValueError(
                f"correction.target keys {sorted(keys)} must be exactly one "
                "of {uid}, {type,name}, {type,handle} "
                "(docs/field-corrections.md §3.3)"
            )
        _validate_no_unknown_fn(v, path="target")
        return v

    @field_validator("value")
    @classmethod
    def _validate_value(cls, v: Any) -> Any:
        _validate_no_unknown_fn(v, path="value")
        return v

    @field_validator("observed_at")
    @classmethod
    def _validate_observed_at(cls, v: Any) -> Any:
        _validate_no_unknown_fn(v, path="observed_at")
        return v

    @field_validator("note")
    @classmethod
    def _validate_note(cls, v: Any) -> Any:
        _validate_no_unknown_fn(v, path="note")
        return v

    @field_validator("source")
    @classmethod
    def _validate_source_machine_tier(cls, v: str) -> str:
        """Guard: `source` is a LITERAL (never `$field`/`fn` — see module
        docstring "Decisions") and must parse to a machine-tier type
        (`script:` / `api:`). A rule asserting source above machine tier —
        unparseable, or any other precedence tier including `user:` — fails
        validation (issue athenaeum#901 AC).
        """
        if not isinstance(v, str):
            raise ValueError("correction.source must be a literal string")
        try:
            ref = parse_source(v)
        except ValueError as exc:
            raise ValueError(
                f"correction.source {v!r} is not a valid SourceRef: {exc}"
            ) from exc
        if ref is None or ref.type not in MACHINE_TIER_SOURCE_TYPES:
            raise ValueError(
                f"correction.source {v!r} asserts precedence above machine "
                f"tier -- only {sorted(MACHINE_TIER_SOURCE_TYPES)} source "
                "types are permitted (docs/field-corrections.md §6.1)"
            )
        return v


#: Dispositions that compile a correction record and therefore REQUIRE a
#: `correction` block. Every other disposition must not carry one (issue
#: athenaeum#901 for `emit`, athenaeum#903 for `rollup`).
_CORRECTION_REQUIRED: frozenset[str] = frozenset({"emit", "rollup"})

#: Dispositions that MAY carry a `correction` block but do not require one
#: (issue athenaeum#837). `preserve` is the only one: a preserved log is kept for
#: its own sake, and whether the librarian also learns a fact FROM it is a
#: separate, optional question. Without a `correction` the log is simply moved
#: and preserved; with one, the fact is compiled AND carries a source pointer
#: back to the preserved artifact (the operator decision of 2026-08-14 — the
#: log survives as the fact's provenance rather than the fact being asserted
#: with none).
_CORRECTION_OPTIONAL: frozenset[str] = frozenset({"preserve"})

#: Every terminal disposition a matched record can reach. `transform-error` is
#: NOT here: it is a degradation of `emit` to fallthrough, tallied under its own
#: name so a rule failing to resolve is visible rather than hidden inside the
#: fallthrough count (issue athenaeum#901).
TERMINAL_DISPOSITIONS: frozenset[str] = frozenset(
    {"emit", "fallthrough", "drop", "retain", "rollup", "preserve"}
)


class RollupSpec(BaseModel):
    """The `rollup` disposition's aggregation (issue athenaeum#903).

    `docs/field-corrections.md` §12 is explicit about what may cross the
    boundary from an event stream into an entity record: *"a small rollup —
    last-event date, a windowed count"*, and nothing else. This spec is that
    sentence expressed as a closed vocabulary:

    - `group_by` — a value expression resolved per record. Records whose keys
      compare equal collapse into ONE correction. (The natural key is whatever
      identifies the entity the events are about.)
    - `aggregate` — `count` (how many records in the group) or `last` (the
      maximum value of `of` across the group, i.e. the last-event date).
    - `of` — required by `last`, forbidden by `count`: the value expression
      naming the per-record event timestamp.

    The group's correction record is built from the rule's `correction` block
    against the group's FIRST record (for `target` / `field` / `source` /
    `note`), with `value` REPLACED by the computed aggregate. Deliberately no
    new reserved token like `$$rollup`: a substitution token is a templating
    language in miniature, and athenaeum#901's "no templating language" AC is a
    property worth keeping literally true.
    """

    model_config = ConfigDict(extra="forbid")

    group_by: Any
    aggregate: Literal["count", "last"]
    of: Any = None

    @model_validator(mode="after")
    def _validate_of_pairing(self) -> "RollupSpec":
        if self.aggregate == "last" and self.of is None:
            raise ValueError(
                "rollup.aggregate 'last' requires 'of' (the value expression "
                "naming the per-record event timestamp)"
            )
        if self.aggregate == "count" and self.of is not None:
            raise ValueError("rollup.aggregate 'count' must not carry an 'of'")
        return self

    @field_validator("group_by", "of")
    @classmethod
    def _validate_no_unknown_functions(cls, v: Any) -> Any:
        _validate_no_unknown_fn(v, path="rollup")
        return v


class ShapeRule(BaseModel):
    """One `<knowledge-root>/rules/*.yaml` file's full contents."""

    model_config = ConfigDict(extra="forbid")

    version: int
    name: str
    mode: Literal["observe", "live"] = "observe"
    match: MatchSpec
    disposition: Literal["emit", "fallthrough", "drop", "retain", "rollup", "preserve"]
    correction: CorrectionSpec | None = None
    rollup: RollupSpec | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not isinstance(v, str) or not re.match(r"^[a-z][a-z0-9-]*\Z", v or ""):
            raise ValueError(
                f"name must match [a-z][a-z0-9-]*, got {v!r}"
            )
        return v

    @field_validator("version")
    @classmethod
    def _validate_version(cls, v: int) -> int:
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise ValueError(f"version must be a positive integer, got {v!r}")
        return v

    @model_validator(mode="after")
    def _validate_disposition_correction_pairing(self) -> "ShapeRule":
        # Issue athenaeum#901: `emit` compiles ONE record into ONE correction.
        # Issue athenaeum#903: `rollup` aggregates N records into ONE correction, so
        # it needs the same `correction` block PLUS a `rollup` block saying how
        # the N collapse. `fallthrough`/`drop`/`retain` write no correction at
        # all, so carrying one is a rule the operator has mis-written — caught
        # at load time rather than silently ignored per record.
        # Issue athenaeum#837: `preserve` is the one disposition where a correction
        # is OPTIONAL — see `_CORRECTION_OPTIONAL`.
        if self.disposition in _CORRECTION_REQUIRED and self.correction is None:
            raise ValueError(
                f"disposition {self.disposition!r} requires a 'correction' block"
            )
        if (
            self.disposition not in _CORRECTION_REQUIRED
            and self.disposition not in _CORRECTION_OPTIONAL
            and self.correction is not None
        ):
            raise ValueError(
                f"disposition {self.disposition!r} must not carry a "
                "'correction' block"
            )
        if self.disposition == "rollup" and self.rollup is None:
            raise ValueError("disposition 'rollup' requires a 'rollup' block")
        if self.disposition != "rollup" and self.rollup is not None:
            raise ValueError(
                f"disposition {self.disposition!r} must not carry a 'rollup' block"
            )
        # Issue athenaeum#1133: `emit`/`rollup` both require a `correction`
        # block whose whole purpose is compiling record fields -- meaningless
        # against an unclaimed candidate's `{}` record. Rejected at load
        # time; the coherent rule is "an `unclaimed` rule may only assert
        # things that don't require record content" (drop/retain/preserve/
        # fallthrough).
        if self.match.unclaimed and self.disposition in _CORRECTION_REQUIRED:
            raise ValueError(
                f"match.unclaimed rules cannot use disposition "
                f"{self.disposition!r} (it requires a 'correction' block, "
                "which compiles record fields that do not exist for an "
                "unclaimed candidate) -- use drop/retain/preserve/"
                "fallthrough instead"
            )
        return self

    @property
    def qualified_name(self) -> str:
        """`name@version` — the audit tag every ledger line carries (issue
        athenaeum#901 AC: "ledger lines ... tagged rule@version")."""
        return f"{self.name}@{self.version}"


@dataclass
class RuleLoadError:
    path: Path
    reason: str


def load_rules(knowledge_root: Path) -> tuple[list[ShapeRule], list[RuleLoadError]]:
    """Load + schema-validate every `<knowledge_root>/rules/*.yaml` file.

    A malformed rule (bad YAML, schema violation) is SKIPPED with a loud
    `log.error` line and recorded in the returned error list — never raises,
    never partially loads a bad rule. `yaml.safe_load` only: a rule file is
    DATA, never executed (no `eval`, no template engine).

    Returns `(rules, errors)`, both sorted by filename (deterministic
    first-match-wins order for :func:`run_shape_rule_phase`).
    """
    rules_dir = knowledge_root / "rules"
    rules: list[ShapeRule] = []
    errors: list[RuleLoadError] = []
    if not rules_dir.is_dir():
        return rules, errors
    for path in sorted(rules_dir.glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError, UnicodeDecodeError) as exc:
            log.error("shape-rules: MALFORMED rule %s SKIPPED -- %s", path, exc)
            errors.append(RuleLoadError(path=path, reason=str(exc)))
            continue
        if not isinstance(raw, dict):
            reason = f"top-level YAML must be a mapping, got {type(raw).__name__}"
            log.error("shape-rules: MALFORMED rule %s SKIPPED -- %s", path, reason)
            errors.append(RuleLoadError(path=path, reason=reason))
            continue
        try:
            rule = ShapeRule.model_validate(raw)
        except PydanticValidationError as exc:
            log.error("shape-rules: MALFORMED rule %s SKIPPED -- %s", path, exc)
            errors.append(RuleLoadError(path=path, reason=str(exc)))
            continue
        rules.append(rule)
    return rules, errors


# ---------------------------------------------------------------------------
# Record extraction from a raw intake file
# ---------------------------------------------------------------------------


def _record_and_format(raw: RawFile) -> tuple[dict[str, Any], str]:
    """Extract the matchable `record` dict and `format` token from a raw
    intake file. See the module docstring "Decisions" section for the exact
    md/jsonl extraction rules and the "empty record on anything else" default.
    """
    fmt = raw.path.suffix.lower().lstrip(".")
    try:
        content = raw.content
    except (OSError, UnicodeDecodeError, RawFileTooLargeError):
        return {}, fmt
    if fmt == "jsonl":
        first_line = content.split("\n", 1)[0]
        try:
            obj = json.loads(first_line)
        except (json.JSONDecodeError, ValueError):
            return {}, fmt
        return (obj if isinstance(obj, dict) else {}), fmt
    if fmt == "md":
        meta, _body = parse_frontmatter(content)
        return (dict(meta) if meta else {}), fmt
    return {}, fmt


# ---------------------------------------------------------------------------
# emit: building + writing the compiled correction batch
# ---------------------------------------------------------------------------


def build_correction_record(
    spec: CorrectionSpec, record: dict[str, Any], *, rule_tag: str, schema_version: int = 1
) -> dict[str, Any]:
    """Resolve *spec* against *record* into an effective §3.2 correction
    record dict (`record`/`correction_id` included). Raises
    :class:`ShapeRuleTransformError` if any value expression cannot
    resolve — the caller treats that as a per-record fallthrough.
    """
    target = resolve_value_expr(spec.target, record)
    if not isinstance(target, dict) or not target:
        raise ShapeRuleTransformError(
            f"resolved target is not a non-empty mapping: {target!r}"
        )
    value = resolve_value_expr(spec.value, record)
    if spec.observed_at is not None:
        observed_at = resolve_value_expr(spec.observed_at, record)
    else:
        observed_at = now_iso()
    note = resolve_value_expr(spec.note, record) if spec.note is not None else None
    if not note:
        note = f"compiled by shape rule {rule_tag}"

    out: dict[str, Any] = {
        "record": "correction",
        "target": target,
        "op": spec.op,
        "field": spec.field,
        "value": value,
        "source": spec.source,
        "observed_at": observed_at,
        "note": str(note),
    }
    # Issue athenaeum#904 (AC2): optional decay annotations, riding alongside the
    # field/value payload exactly like `usage_class` does — omitted entirely
    # when the rule's `correction:` block does not set them, so a rule
    # authored before this issue existed emits a byte-identical record.
    if spec.bucket is not None:
        out["bucket"] = spec.bucket
    if spec.valid_until is not None:
        out["valid_until"] = resolve_value_expr(spec.valid_until, record)
    out["correction_id"] = compute_correction_id(
        schema_version=schema_version,
        target=target,
        op=spec.op,
        field_name=spec.field,
        value=value,
    )
    return out


def _git(knowledge_root: Path, *args: str) -> "subprocess.CompletedProcess[bytes]":
    return subprocess.run(
        ["git", *args], cwd=knowledge_root, capture_output=True, check=False
    )


def write_correction_batch(
    *, raw_root: Path, source: str, submitter: str, records: list[dict[str, Any]]
) -> Path:
    """Write ONE correction batch (`docs/field-corrections.md` §3.2) for the
    given effective correction records into
    `raw/<source>/<timestamp>-<uuid8>.jsonl` — the SAME ordinary intake tree
    the existing correction machinery already scans
    (`corrections.find_correction_batches`, `intake.discover_raw_files`'s
    skip). No new discovery path, no change to that machinery.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    uuid8 = uuid.uuid4().hex[:8]
    target_dir = raw_root / source
    target_dir.mkdir(parents=True, exist_ok=True)
    out_path = target_dir / f"{timestamp}-{uuid8}.jsonl"
    envelope = {
        "record": "batch",
        "schema_version": 1,
        "submitter": submitter,
        "batch_id": f"{timestamp}-{uuid8}",
        "created_at": now_iso(),
    }
    lines = [json.dumps(envelope, sort_keys=True)]
    lines.extend(json.dumps(r, sort_keys=True) for r in records)
    atomic_write_text(out_path, "\n".join(lines) + "\n")
    return out_path


def retire_compiled_raw_file(
    knowledge_root: Path,
    raw_path: Path,
    *,
    rule_tag: str,
    store: Store | None = None,
) -> bool:
    """Retire a raw intake file the shape-rule engine has fully compiled
    into a correction batch (`emit`, live mode) — `git rm` after a
    provenance-snapshot commit, recoverable from git history, never
    hard-deleted. Same two-commit pattern as
    `corrections.retire_batch`/`adapter-contract.md` §4.5, with its OWN
    commit wording (see module docstring "Decisions" for why this is not a
    call to `corrections.retire_batch`).

    Refuses (returns `False`) against a store that is not versioned (design
    note §4.4 R1; issue athenaeum#978) — no unlink fallback outside a git
    repo. *store* is injectable for tests; defaults to a
    :class:`~athenaeum.store.FilesystemStore` over *knowledge_root*. Returns
    `True` on success.
    """
    return _retire_raw_file(
        knowledge_root,
        raw_path,
        snapshot_reason="before compile",
        retire_reason=f"compiled into a correction batch by {rule_tag}",
        store=store,
    )


def drop_raw_file(
    knowledge_root: Path,
    raw_path: Path,
    *,
    rule_tag: str,
    store: Store | None = None,
) -> bool:
    """Retire a raw intake file a `drop` rule judged information-free
    (issue athenaeum#903) — the SAME two-commit provenance-snapshot-then-`git rm`
    convention :func:`retire_compiled_raw_file` uses, with drop wording.

    The distinction from `emit`'s retirement is the reason, not the mechanism,
    and the mechanism is the point: a `drop` is an **audited discard**, never a
    hard delete. The content is committed before it is removed, so it stays
    recoverable from history (athenaeum#903 AC: "the discard is recoverable from
    history") and the audit counter in the ledger says how many were discarded
    and by which rule.

    Refuses (returns `False`) against a store that is not versioned (design
    note §4.4 R1; issue athenaeum#978) — no unlink fallback outside a git
    repo. *store* is injectable for tests; defaults to a
    :class:`~athenaeum.store.FilesystemStore` over *knowledge_root*.
    """
    return _retire_raw_file(
        knowledge_root,
        raw_path,
        snapshot_reason="before drop",
        retire_reason=f"dropped as information-free by {rule_tag}",
        store=store,
    )


#: Scheme prefix on a correction's ``source`` when the fact was compiled FROM a
#: preserved log (issue athenaeum#837). Chosen to read as a URI scheme so a
#: consumer can dispatch on it without parsing prose, and to be greppable in a
#: page's ``field_sources`` — "which facts came out of a log?" is one grep.
PRESERVED_LOG_SOURCE_SCHEME = "preserved-log"


def preserved_log_source_pointer(
    knowledge_root: Path, preserved_path: Path, *, fmt: str
) -> str:
    """The provenance pointer a fact compiled from a preserved log carries.

    ``preserved-log:<path-relative-to-knowledge-root>#<locator>`` — the
    operator decision of 2026-08-14 in issue athenaeum#837: *"point any facts
    that we do ingest to that log as the source"*, so the artifact IS the
    provenance rather than the fact being asserted with none.

    The locator is honest about what the extractor actually matched
    (:func:`_record_and_format`): a raw file yields exactly ONE record, so the
    path plus that record's position within the file locates it completely —
    ``L1`` for a ``.jsonl`` (the engine reads the first line), ``frontmatter``
    for a ``.md``. When the extractor grows to multi-record files this is the
    field that carries the record index; the format is deliberately shaped to
    take one now rather than needing a second pointer scheme later.

    The path segment varies by DESTINATION, not by backend (issue athenaeum#1132,
    AC4): the scheme names the provenance *kind*, which stays
    ``preserved-log`` no matter what storage the artifact was routed through
    (varying it per backend would be exactly the "second concept" this
    pointer format is designed to avoid). When *preserved_path* resolves
    under *knowledge_root* (the local ``librarian.preserved_log_dir`` case,
    or a ``librarian.preserved_log_adapter`` whose adapter happens to resolve
    inside the repo) the segment is the existing knowledge-root-relative
    form, byte-identical to pre-athenaeum#1132 output. When it does not — an
    out-of-repo adapter surface, the case this issue adds — the
    ``relative_to`` call below raises ``ValueError`` and the ``except``
    branch falls back to *preserved_path*'s own absolute POSIX form. That
    fallback existed before this issue only as an accidental leak (nothing
    could previously make ``preserved_path`` land outside ``knowledge_root``
    at all); it is now the INTENDED, tested outside-repo pointer shape — the
    leading ``/`` is what disambiguates a relative, in-repo segment from an
    absolute, routed one, with no new field needed.
    """
    try:
        rel = preserved_path.resolve().relative_to(knowledge_root.resolve()).as_posix()
    except (ValueError, OSError):
        rel = preserved_path.as_posix()
    locator = {"jsonl": "L1", "md": "frontmatter"}.get(fmt, "")
    return (
        f"{PRESERVED_LOG_SOURCE_SCHEME}:{rel}#{locator}"
        if locator
        else f"{PRESERVED_LOG_SOURCE_SCHEME}:{rel}"
    )


def preserve_raw_file(
    knowledge_root: Path,
    raw_path: Path,
    *,
    preserved_dir: str,
    source: str,
    rule_tag: str,
) -> Path | None:
    """MOVE a log-shaped raw file out of raw intake into the preserved area.

    The move — not a flag on a file that stays put — is what the athenaeum#837
    operator decision asks for, and it is the stronger guarantee: a preserved
    log is not *skipped by* discovery, it is outside the tree discovery walks
    (:func:`athenaeum.intake.discover_raw_files` only ever walks ``raw/``), so
    no future caller has to remember to consult an exempt manifest.

    Layout under the preserved area mirrors intake's own
    ``<preserved_dir>/<source>/<filename>``, so a log's origin survives the
    move — a bare flat dump of filenames would lose which tool wrote it.

    Returns the destination path, or ``None`` if the move could not be made
    (the caller then falls through and leaves the raw file untouched — never
    a half-move, and never a silent loss).
    """
    if not raw_path.exists():
        return None
    dest_dir = knowledge_root / preserved_dir / source
    dest = dest_dir / raw_path.name
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.warning(
            "shape-rules: %s could not create preserved-log area %s — "
            "leaving %s in raw intake",
            rule_tag,
            dest_dir,
            raw_path.name,
        )
        return None
    # A same-named log from an earlier run must never be clobbered: the whole
    # contract is that a preserved artifact SURVIVES. Suffix instead.
    if dest.exists():
        stem, suffix = raw_path.stem, raw_path.suffix
        for n in range(1, 1000):
            candidate = dest_dir / f"{stem}-{n}{suffix}"
            if not candidate.exists():
                dest = candidate
                break
        else:
            log.warning(
                "shape-rules: %s found no free filename for %s under %s — "
                "leaving it in raw intake",
                rule_tag,
                raw_path.name,
                dest_dir,
            )
            return None
    if (knowledge_root / ".git").is_dir():
        try:
            rel_src = str(raw_path.resolve().relative_to(knowledge_root.resolve()))
            rel_dst = str(dest.resolve().relative_to(knowledge_root.resolve()))
        except (ValueError, OSError):
            rel_src, rel_dst = str(raw_path), str(dest)
        # `git add` first: an intake file may be untracked, and `git mv` on an
        # untracked path fails. Staging makes the move recoverable from history
        # exactly like a retirement is.
        _git(knowledge_root, "add", "--", rel_src)
        moved = _git(knowledge_root, "mv", "-f", "--", rel_src, rel_dst)
        if moved.returncode == 0:
            _git(
                knowledge_root,
                "commit",
                "-m",
                f"shape-rules: {rel_src} preserved as a source document "
                f"by {rule_tag} -> {rel_dst}",
            )
            return dest
    try:
        shutil.move(str(raw_path), str(dest))
        return dest
    except OSError:
        log.warning(
            "shape-rules: %s failed to move %s into the preserved-log area",
            rule_tag,
            raw_path,
        )
        return None


class PreserveViaStoreOutcome(NamedTuple):
    """Return contract for :func:`preserve_raw_file_via_store` (issue
    athenaeum#1132, QA follow-up). See that function's docstring for what
    each field means and why a plain ``Path | None`` stopped being enough --
    ``preserve`` vs. ``preserve-orphaned-source`` is a real, ledger-visible
    distinction the caller must not collapse.
    """

    dest_path: Path | None
    freshly_written: bool
    orphaned: bool


def _store_object_matches(store: Store, key: StoreKey, data: bytes) -> bool:
    """Whether the object already at *key* is byte-identical to *data*
    (issue athenaeum#1132, QA follow-up) -- the ONLY signal
    :func:`preserve_raw_file_via_store` accepts as proof that a
    `StoreConflictError` collision is a convergent retry rather than a
    genuine, unrelated conflict. Fails closed (returns ``False``, never
    raises) when the existing object cannot even be read -- an unreadable
    destination is never treated as a match.
    """
    try:
        existing = store.read(key)
    except (OSError, KeyError):
        return False
    return existing == data


def _unlink_quietly(path: Path) -> bool:
    """``Path.unlink()``, returning ``False`` instead of raising on
    ``OSError`` -- the non-versioned-store removal step
    :func:`preserve_raw_file_via_store` uses, where a failed removal is a
    reportable (`preserve-orphaned-source`), not fatal, outcome."""
    try:
        path.unlink()
        return True
    except OSError:
        return False


def preserve_raw_file_via_store(
    knowledge_root: Path,
    raw_path: Path,
    *,
    adapter_name: str,
    source: str,
    rule_tag: str,
    store: Store,
) -> PreserveViaStoreOutcome:
    """MOVE a log-shaped raw file into a configured storage adapter's surface
    (issue athenaeum#1132) — the ``librarian.preserved_log_adapter``-routed
    counterpart to :func:`preserve_raw_file`, which stays byte-identical for
    the local, in-repo ``librarian.preserved_log_dir`` case.

    **Fail-closed ordering (AC5): put-then-remove, never move-then-confirm.**
    The source bytes are read and handed to :meth:`~athenaeum.store.Store.put`
    with ``expect=None`` (exclusive create) BEFORE the source is touched at
    all. Only once ``put`` returns successfully — directly, or via the
    convergence path below — is the source removed, never the reverse, so a
    failed or unconfirmed write can never lose the artifact. ``put`` can
    raise ``OSError`` — notably ``EXDEV``, since a routed adapter is
    routinely on a different filesystem than ``raw/`` (the mural corpus that
    motivates this issue is exactly that case) — and this function does NOT
    catch that: the caller (`run_shape_rule_phase`'s `preserve` branch)
    catches it, tallies the existing `preserve-failed` disposition, logs,
    and leaves the raw file untouched. Unlike :func:`preserve_raw_file`,
    there is no filename-suffix retry on a destination collision.

    **Convergence on a `StoreConflictError` collision (QA follow-up on issue
    athenaeum#1132).** A collision at the exact destination key is NOT
    automatically a genuine conflict: it is also exactly what a RETRY looks
    like after a prior call's `put` succeeded but the source removal below
    failed (the "orphaned source" case) — `discover_raw_files` re-surfaces
    the still-present source on the next run, and the same rule re-attempts
    the same `put` with the same bytes. The only safe way to tell these two
    cases apart is to compare content: if the existing object at *dest_key*
    is byte-identical to *raw_path*'s current content, this is that
    convergence case, and the call proceeds to (re-)attempt removal instead
    of failing. If the content differs, or the existing object cannot be
    read at all, this is treated as a genuine, unrelated collision and the
    original :class:`~athenaeum.store.StoreConflictError` propagates
    unchanged — fail-closed, matching the pre-existing behavior exactly. A
    NAME match alone is never sufficient; weakening this to skip the content
    comparison would let a genuinely different, unrelated artifact silently
    block (or worse, appear to satisfy) preservation.

    Removal of the (now-durably-copied) source mirrors
    :func:`_retire_raw_file`'s existing git-recoverable pattern when
    *knowledge_root* is a versioned store (`store.capabilities.versioned`):
    the source's own content is snapshotted and then `git rm` + committed, so
    its disappearance is itself recorded in git history — identical to how
    `drop`/`emit` retirement already works. When *knowledge_root* is NOT a
    git repo, a plain ``unlink()`` is used instead: unlike
    :func:`_retire_raw_file`'s own refusal in that case, there is no data-loss
    risk here to refuse against — the artifact is already confirmed durable
    at the adapter's surface by the time removal is attempted, so recovering
    the SOURCE's deletion via git is a nice-to-have, not a safety requirement.

    **Ledger honesty (QA follow-up on issue athenaeum#1132).** This function
    never itself tallies anything — it reports outcome via the returned
    :class:`PreserveViaStoreOutcome` so the caller can distinguish full
    success from a durably-written-but-not-yet-retired artifact. Tallying
    the latter as plain `preserve` would be a live lie: the source stays in
    `raw/`, `discover_raw_files` re-surfaces it, and the NEXT `put` attempt
    at the same key would previously have hard-failed forever (no
    suffix-retry) with no path back to a converged, honest state — exactly
    the permanently-stuck-with-a-self-contradicting-ledger failure this
    return contract and the convergence path above exist to prevent.

    Returns a :class:`PreserveViaStoreOutcome`:

    - ``dest_path`` — the resolved absolute destination (for
      :func:`preserved_log_source_pointer`), or ``None`` if *raw_path* did
      not exist to begin with (mirrors :func:`preserve_raw_file`'s early
      return; ``freshly_written``/``orphaned`` are both ``False`` in that
      case).
    - ``freshly_written`` — ``True`` only when THIS call's own `put`
      actually wrote the artifact (as opposed to finding it already present
      via the convergence path). The caller uses this to avoid re-emitting
      a correction record/batch for an artifact a PRIOR call already
      compiled a fact from.
    - ``orphaned`` — ``True`` when the artifact is durably written (fresh or
      converged) but this call could not remove the source. The caller
      tallies `preserve-orphaned-source` rather than `preserve` in that case
      — never a false terminal success.
    """
    if not raw_path.exists():
        return PreserveViaStoreOutcome(dest_path=None, freshly_written=False, orphaned=False)
    dest_key = StoreKey(surface=adapter_name, key=f"{source}/{raw_path.name}")
    local_path_for = store.capabilities.local_path_for
    if local_path_for is None:  # pragma: no cover - every adapter in this repo is filesystem-backed
        raise StorageConfigError(
            f"storage adapter {adapter_name!r} has no local_path_for -- "
            "librarian.preserved_log_adapter requires a filesystem-backed "
            "adapter (issue athenaeum#1132)"
        )
    dest_path = local_path_for(dest_key)
    data = raw_path.read_bytes()
    freshly_written = True
    try:
        store.put(dest_key, data, expect=None)  # may raise; caller handles fail-closed
    except StoreConflictError:
        if not _store_object_matches(store, dest_key, data):
            raise  # genuine collision (or unreadable) -- fail closed, unchanged
        freshly_written = False
        log.info(
            "shape-rules: %s found %s already durably preserved at %s "
            "(byte-identical) -- converging a prior run's orphaned source "
            "instead of failing (issue athenaeum#1132)",
            rule_tag,
            raw_path,
            dest_path,
        )
    if store.capabilities.versioned:
        removed = _retire_raw_file(
            knowledge_root,
            raw_path,
            snapshot_reason=f"before preservation via storage adapter {adapter_name!r}",
            retire_reason=(
                f"preserved via storage adapter {adapter_name!r} by {rule_tag}"
            ),
            store=store,
        )
    else:
        removed = _unlink_quietly(raw_path)
    if not removed:
        log.warning(
            "shape-rules: %s preserved %s to %s via storage adapter %r but "
            "could not remove the source -- tallying preserve-orphaned-source "
            "rather than preserve; the artifact is safely durable, not lost, "
            "but the raw copy was left in place for inspection and this run "
            "will retry removal on its next pass (issue athenaeum#1132)",
            rule_tag,
            raw_path,
            dest_path,
            adapter_name,
        )
    return PreserveViaStoreOutcome(
        dest_path=dest_path, freshly_written=freshly_written, orphaned=not removed
    )


def _retire_raw_file(
    knowledge_root: Path,
    raw_path: Path,
    *,
    snapshot_reason: str,
    retire_reason: str,
    store: Store | None = None,
) -> bool:
    """Shared two-commit retirement: snapshot the content, then `git rm` it.

    Committing BEFORE the removal is what makes every retirement recoverable —
    a file that was never committed would be unrecoverable once unlinked, which
    is the difference between an audited discard and a deletion.

    Refuses (returns ``False``) against a store that is not versioned
    (design note §4.4 R1; issue athenaeum#978): the plain-``unlink`` fallback
    this used to fall through to when ``knowledge_root`` was not a git repo
    is REMOVED — that was exactly the silent degradation to an unrecoverable
    delete R1 prohibits, "documented as a best-effort fallback for test
    fixtures" until a non-git surface became the normal case. A ``git rm``
    that itself fails (rather than "no git repo" at all) also now refuses
    rather than falling through to ``unlink`` — the old fallback covered
    both cases identically, so removing it removes both. *store* is
    injectable for tests; defaults to a
    :class:`~athenaeum.store.FilesystemStore` over *knowledge_root*.
    """
    if not raw_path.exists():
        return True
    store = store if store is not None else FilesystemStore(knowledge_root, {})
    if not store.capabilities.versioned:
        log.warning(
            "shape-rules: store at %s is not versioned — refusing to retire "
            "%s (recovery is git-only)",
            knowledge_root,
            raw_path,
        )
        return False
    try:
        rel = str(raw_path.resolve().relative_to(knowledge_root.resolve()))
    except ValueError:
        rel = str(raw_path)
    _git(knowledge_root, "add", "--", rel)
    staged = _git(knowledge_root, "diff", "--cached", "--quiet")
    if staged.returncode != 0:
        _git(
            knowledge_root,
            "commit",
            "-m",
            f"shape-rules: raw-intake provenance snapshot {snapshot_reason} ({rel})",
        )
    rm_result = _git(knowledge_root, "rm", "--quiet", "-f", "--", rel)
    if rm_result.returncode != 0:
        log.warning(
            "shape-rules: git rm failed for %s — refusing to retire (no "
            "unlink fallback)",
            raw_path,
        )
        return False
    _git(
        knowledge_root,
        "commit",
        "-m",
        f"shape-rules: {rel} {retire_reason}",
    )
    return True


# ---------------------------------------------------------------------------
# §5.3-pattern audit ledger
# ---------------------------------------------------------------------------

SHAPE_RULES_LEDGER_FILENAME = "_shape_rules_applied.jsonl"


def default_shape_rules_ledger_path(wiki_root: Path) -> Path:
    return wiki_root / SHAPE_RULES_LEDGER_FILENAME


def _append_jsonl_line(path: Path, line: str) -> None:
    """Append one line to *path* durably (``O_APPEND`` + fsync), via
    :func:`athenaeum.store.append_line_durable` — the single shared
    implementation issue athenaeum#980 (S5) collapsed this module's copy onto
    (design note §2.4 / §6.2)."""
    append_line_durable(path, line.encode("utf-8"))


def append_shape_rules_ledger(wiki_root: Path, record: dict[str, Any]) -> None:
    path = default_shape_rules_ledger_path(wiki_root)
    _append_jsonl_line(path, json.dumps(record, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# Per-record disposition rows (issue athenaeum#975, athenaeum#905 prerequisite)
# ---------------------------------------------------------------------------
#
# `_shape_rules_applied.jsonl` above is a per-`(rule, mode)` AGGREGATE: it
# answers "how often did this rule fire", not "which record got what
# treatment". This ledger is the per-record complement -- one row per
# candidate the shape-rules pass evaluates, including the ones no rule
# matched at all, so athenaeum#905's shape-frequency detector has a real per-record
# data source (athenaeum#923: "everything is dispositioned and the
# disposition is audited"). Same `_`-prefixed, wiki-root, append-only-JSONL
# discipline as the aggregate above -- every corpus walker in this repo
# skips `_`-prefixed files, so this file can never become a claim or enter
# the embedded index.

SHAPE_RULE_DISPOSITIONS_FILENAME = "_shape_rule_dispositions.jsonl"


def default_shape_rule_dispositions_path(wiki_root: Path) -> Path:
    return wiki_root / SHAPE_RULE_DISPOSITIONS_FILENAME


def append_shape_rule_disposition_row(wiki_root: Path, row: dict[str, Any]) -> None:
    path = default_shape_rule_dispositions_path(wiki_root)
    _append_jsonl_line(path, json.dumps(row, sort_keys=True) + "\n")


#: Dispositions the shape-rules pass -- the deterministic, no-LLM layer,
#: tier 0 on the ladder in `docs/field-corrections.md` §2 -- actually
#: resolved a record with. Everything else (no rule matched at all, a rule
#: that explicitly deferred via `fallthrough`/`observed-fallthrough`, or a
#: soft failure that degrades to fallthrough -- `transform-error`,
#: `preserve-unconfigured`, `preserve-failed`) is tier `None`: "not handled
#: here", deferred to the reasoning ladder (tier >=1). This pass genuinely
#: does not know which reasoning tier will handle a deferred record, so
#: `None` is the honest encoding rather than a guessed number.
_TIER_0_DISPOSITIONS: frozenset[str] = frozenset(
    {
        "emit",
        "observed-emit",
        "drop",
        "observed-drop",
        "retain",
        "observed-retain",
        "preserve",
        "observed-preserve",
        "rollup",
        "observed-rollup",
    }
)


def _disposition_tier(disposition: str) -> int | None:
    """Map a shape-rules disposition to its reasoning-ladder tier -- see
    :data:`_TIER_0_DISPOSITIONS`."""
    return 0 if disposition in _TIER_0_DISPOSITIONS else None


def _shape_rule_disposition_row(
    *, raw: RawFile, record: dict[str, Any], rule_id: str | None, disposition: str
) -> dict[str, Any]:
    """Build one `_shape_rule_dispositions.jsonl` row.

    `key_fingerprint` is the record's top-level KEY SET fingerprint
    (:func:`record_key_fingerprint`) -- never raw values, per athenaeum#975 AC2.
    `source`/`source_ref` come from `RawFile.source`/`RawFile.ref`, both
    already non-sensitive (a raw source directory name and a
    `source/filename` ref) -- no raw record VALUE ever lands in a row.
    """
    return {
        "schema_version": 1,
        "at": now_iso(),
        "source": raw.source,
        "source_ref": raw.ref,
        "key_fingerprint": record_key_fingerprint(record),
        "tier": _disposition_tier(disposition),
        "rule_id": rule_id,
        "disposition": disposition,
    }


# ---------------------------------------------------------------------------
# Dedupe-at-write + retention (issue athenaeum#1229)
# ---------------------------------------------------------------------------
#
# A real deployment's `_shape_rule_dispositions.jsonl` grew to 1,440,670 rows
# in 9 days across only 9,737 distinct `(source_ref, rule_id, disposition)`
# tuples -- every re-evaluation of an already-seen record on every nightly
# run appended ANOTHER row, a 148x duplication factor that both crossed
# GitHub's 100 MB blob limit (breaking the knowledge store's git remote) and
# inflated athenaeum#905's rule-proposal detector (see that module's
# `_grouped_deferred_rows`/`detect_shape_frequency`).
#
# Two independent, additive fixes -- (1) does not make (2) unnecessary, or
# vice versa, per the issue's own "Retention alone is NOT sufficient" note:
#
# 1. Dedupe-at-write (:func:`_load_existing_disposition_keys`,
#    :func:`_append_disposition_row_deduped`): a row whose full
#    `(source, key_fingerprint, source_ref, rule_id, disposition)` tuple is
#    already recorded for that record is never appended again -- this is
#    what collapses 1,440,670 rows to ~9,737 on the measured corpus. The key
#    set is built ONCE per `run_shape_rule_phase` call, not re-read per
#    candidate: O(current ledger size), never O(evaluations this run). Once
#    dedupe has taken effect the ledger size converges toward the corpus's
#    distinct-tuple count, so this up-front read stays cheap on every
#    subsequent run too -- it scales with the corpus, not with run count
#    (the exact axis the pre-fix ledger grew unboundedly along). Migrating
#    an already-oversized pre-fix ledger is explicitly out of scope (see the
#    module's "Per-record disposition rows" section above); a deployment
#    that upgrades onto one pays a one-time larger read on its first
#    post-fix run.
# 2. Retention (:func:`prune_shape_rule_dispositions`): rows older than
#    `librarian.shape_rules.dispositions_retention_days` are dropped from
#    the ledger outright, independent of whether dedupe ever fires for a
#    given record. Needed because dedupe alone still lets the file grow
#    without bound as genuinely NEW distinct records accumulate over months;
#    retention alone still reaches roughly 1.1 GB at the observed pre-dedupe
#    rate over a 30-day window (the issue's own arithmetic) -- both together
#    are what bounds the file.
#
# Issue athenaeum#1274 added a third lever, and re-keyed the second:
#
# 3. Suppress-at-write (`librarian.shape_rules.log_no_match`, DEFAULT OFF,
#    :func:`athenaeum.config.resolve_shape_rules_log_no_match`): the
#    `no-match` rows dedupe cannot help with -- a corpus of genuinely
#    distinct unclaimed records has a genuinely distinct row each, and on
#    the measured deployment they were 1,485,942 of 1,488,689 rows (99.8%)
#    -- are not written at all unless asked for. They are only ever read by
#    athenaeum#905's detector via `_grouped_deferred_rows`'s `tier is None`
#    filter, and that detector is unreachable while
#    `librarian.rule_proposals.enabled` is off (its own default): the phase
#    returns before any ledger read. Turning the detector on therefore means
#    turning this on too -- see the resolver's docstring for why the
#    dependency is documented rather than wired as a derived default.
#
# ...and the retention window above moved off `librarian.rule_proposals.
# window_days` onto its own `librarian.shape_rules.dispositions_retention_
# days` (same default, 30, so no deployment's behaviour changes). A READ
# window and a RETENTION policy are different questions: coupled, narrowing
# the detector's window silently deleted ledger history, and an operator who
# disabled `rule_proposals` outright still had retention governed by a key
# belonging to a phase they had turned off.


def _disposition_row_key(row: dict[str, Any]) -> tuple[str, str, str, str | None, str]:
    """The dedupe-at-write identity for one disposition row: everything that
    makes two evaluations of the SAME record with the SAME outcome
    indistinguishable, deliberately excluding `at` and `schema_version` (a
    re-evaluation's timestamp is exactly the field that must NOT make a row
    look new)."""
    return (
        row["source"],
        row["key_fingerprint"],
        row["source_ref"],
        row["rule_id"],
        row["disposition"],
    )


def _load_existing_disposition_keys(
    wiki_root: Path,
) -> set[tuple[str, str, str, str | None, str]]:
    """Read `_shape_rule_dispositions.jsonl` ONCE, up front, into the
    dedupe-at-write key set :func:`_append_disposition_row_deduped` consults
    before appending each new row (issue athenaeum#1229).

    Cost: a single sequential read + JSON-parse per existing line, done once
    per :func:`run_shape_rule_phase` call -- see the "Dedupe-at-write +
    retention" section above for why this stays cheap run over run rather
    than growing with evaluation count the way the pre-fix ledger did.
    Fails open per-line (malformed JSON, a non-dict row, or a row missing
    one of the key fields is skipped rather than aborting the read) and
    fails open on a missing/unreadable file (empty set) -- mirroring
    :func:`athenaeum.rule_proposals._grouped_deferred_rows`'s own tolerance
    for a hand-edited or partially-written ledger.
    """
    keys: set[tuple[str, str, str, str | None, str]] = set()
    path = default_shape_rule_dispositions_path(wiki_root)
    if not path.is_file():
        return keys
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return keys
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        source = row.get("source")
        key_fingerprint = row.get("key_fingerprint")
        source_ref = row.get("source_ref")
        rule_id = row.get("rule_id")
        disposition = row.get("disposition")
        if not (
            isinstance(source, str)
            and isinstance(key_fingerprint, str)
            and isinstance(source_ref, str)
            and isinstance(disposition, str)
            and (rule_id is None or isinstance(rule_id, str))
        ):
            continue
        keys.add((source, key_fingerprint, source_ref, rule_id, disposition))
    return keys


def _append_disposition_row_deduped(
    wiki_root: Path,
    row: dict[str, Any],
    existing_keys: set[tuple[str, str, str, str | None, str]],
) -> None:
    """Append *row* to the disposition ledger unless its
    :func:`_disposition_row_key` is already present in *existing_keys* --
    the dedupe-at-write guard (issue athenaeum#1229). *existing_keys* is
    mutated in place so a SECOND evaluation of the same record within the
    SAME run (e.g. a rollup group's members sharing a key -- not expected
    today, but never assumed away) is caught too, not only cross-run
    duplicates.
    """
    key = _disposition_row_key(row)
    if key in existing_keys:
        return
    existing_keys.add(key)
    append_shape_rule_disposition_row(wiki_root, row)


def prune_shape_rule_dispositions(
    wiki_root: Path, *, window_days: int, now: datetime | None = None
) -> int:
    """Drop `_shape_rule_dispositions.jsonl` rows older than *window_days*
    (issue athenaeum#1229 part 3).

    Bounds the ledger independently of dedupe-at-write (see the "Dedupe-at-
    write + retention" section above for why both are needed): nothing
    downstream ever reads a row past `librarian.rule_proposals.window_days`
    (:func:`athenaeum.rule_proposals._grouped_deferred_rows` applies the
    identical cutoff at READ time), so a row this old is dead weight the
    ledger has been carrying for nothing.

    A row whose `at` is missing or unparseable is KEPT, not dropped --
    fail-open, mirroring :func:`athenaeum.rule_proposals._parse_row_at`'s
    own tolerance: a malformed timestamp is a reason to leave a row alone,
    never a license to silently delete it.

    Returns the number of rows dropped. Rewrites the file atomically
    (:func:`athenaeum.atomic_io.atomic_write_text`) ONLY when at least one
    row was actually dropped -- an unchanged ledger is left byte-identical
    rather than churned every run.
    """
    path = default_shape_rule_dispositions_path(wiki_root)
    if not path.is_file():
        return 0
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0
    resolved_now = now or datetime.now(timezone.utc)
    if resolved_now.tzinfo is None:
        resolved_now = resolved_now.replace(tzinfo=timezone.utc)
    cutoff = resolved_now - timedelta(days=window_days)

    kept_lines: list[str] = []
    dropped = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError:
            kept_lines.append(stripped)
            continue
        at_raw = row.get("at") if isinstance(row, dict) else None
        at: datetime | None = None
        if isinstance(at_raw, str):
            try:
                at = datetime.strptime(at_raw, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                at = None
        if at is not None and at < cutoff:
            dropped += 1
            continue
        kept_lines.append(stripped)

    if dropped == 0:
        return 0
    new_text = "".join(f"{line}\n" for line in kept_lines)
    atomic_write_text(path, new_text)
    return dropped


# ---------------------------------------------------------------------------
# One-time positive-only prune (issue athenaeum#1274 AC3/AC4)
# ---------------------------------------------------------------------------
#
# `prune_shape_rule_dispositions` above is the ONGOING age-based retention
# pass (issue athenaeum#1229) -- it runs every nightly and drops rows past
# `dispositions_retention_days` regardless of their disposition. This is a
# DIFFERENT, one-time operation that athenaeum#1274's AC3/AC4 ask for:
# collapsing an already-oversized pre-fix ledger (341 MB / 1,488,689 rows on
# the deployment that motivated the issue, 99.8% `no-match`) down to its
# positive-disposition records, regardless of age. The two are complementary,
# not redundant -- see that function's docstring for why retention alone
# does not undo a ledger that grew before this issue's fixes ever ran.
#
# **Not run automatically anywhere in this codebase.** This is a live-store
# mutation an operator invokes once, deliberately, via
# `athenaeum storage prune-dispositions --apply` (`_cmd_storage.py`) -- see
# that command for why: the target file lives under `~/knowledge`, not this
# repo, and a prune racing the nightly's own concurrent appends is exactly
# the hazard athenaeum#1274's own proposal names ("not something to do
# mid-ingest"). The CLI wraps this function in
# :class:`athenaeum.runlock.RunLock`; this function itself does no locking
# -- it is a pure transform over a text blob, unit-testable without a live
# store or a lock.
#
# **What counts as "positive."** Default-deny on deletion: the ONLY
# disposition value this function ever drops is the literal string
# `"no-match"`. Everything else -- `preserve`, `observed-preserve`, any
# OTHER disposition (`fallthrough`, `observed-fallthrough`,
# `transform-error`, ...; see `_TIER_0_DISPOSITIONS` above), an
# unrecognised future disposition value, a row with a missing/non-string
# `disposition` field, or a line that fails to parse as JSON at all
# (malformed/truncated, e.g. an interrupted write) -- is treated as a
# POSITIVE and kept. This is deliberately broader than the issue's own
# "preserve + observed-preserve" framing: `athenaeum.rule_proposals.
# _grouped_deferred_rows` (the ledger's only consumer -- confirmed by a
# call-site search of every reader of `default_shape_rule_dispositions_path`
# in this package, not by inference; see `docs/configuration.md`'s
# `log_no_match` entry) reads every `tier is None` row, not only `no-match`
# ones. Dropping a `fallthrough`-shaped row this function has never observed
# in the wild would still be an unreviewed, silent loss of data a live
# consumer depends on. Keeping everything but the one named, understood,
# 99.8%-of-the-file value is the safe default.


class DispositionPruneMismatchError(RuntimeError):
    """Raised when the prune's own re-parse of the content it is about to
    write disagrees with what the scan pass promised -- see
    :func:`prune_shape_rule_dispositions_to_positive`'s docstring, "The
    guard". The prune is aborted; nothing is written."""


@dataclass(frozen=True)
class DispositionPruneReport:
    """Result of :func:`prune_shape_rule_dispositions_to_positive`, dry-run
    or applied. Every count is over the ledger as read this call."""

    total_records: int
    #: Per-disposition-value counts across every row that parsed as a JSON
    #: object with a string `disposition` and was NOT `"no-match"`.
    #: `"<missing-disposition>"` buckets a parsed row whose `disposition`
    #: field is missing or non-string -- kept as a positive (see the module
    #: note above) but not a real disposition value.
    histogram: dict[str, int]
    #: Lines that failed to parse as a JSON object at all -- kept as
    #: positives (fail-open), counted separately since they carry no
    #: `disposition` to bucket into `histogram`.
    malformed_lines: int
    no_match_count: int
    #: `total_records - no_match_count` -- every row this prune keeps.
    positive_count: int
    current_bytes: int
    #: Size of the file if `positive_count` rows were written, one JSON
    #: object per line -- what `--apply` would produce.
    projected_bytes: int
    #: Rows this prune drops (== `no_match_count`).
    rows_dropped: int
    #: Whether this report reflects a write that actually happened.
    applied: bool


def _classify_disposition_ledger_text(
    text: str,
) -> tuple[list[str], dict[str, int], int, int]:
    """One classification pass over disposition-ledger *text*: which lines
    survive a positive-only prune, plus the stats
    :func:`prune_shape_rule_dispositions_to_positive` reports.

    Returns `(kept_lines, histogram, malformed_lines, no_match_count)`.
    `kept_lines` holds the ORIGINAL (stripped) line text unmodified for
    every row this function does not drop -- never a re-serialization, so a
    kept row's on-disk bytes (field order, whitespace) are byte-identical to
    what was there before. Blank lines are skipped entirely (never counted,
    never kept) -- the ledger writer never emits them, and dropping a
    hand-edited file's stray blank lines here is harmless.

    Deliberately factored out of the public function so it can run TWICE in
    one prune -- once over the original text (the scan), once over the
    freshly-built output (the guard's re-parse) -- with identical logic both
    times. See :func:`prune_shape_rule_dispositions_to_positive`'s
    docstring for why re-running the SAME classifier over the constructed
    output, rather than trusting the first pass's bookkeeping, is the point.
    """
    kept_lines: list[str] = []
    histogram: dict[str, int] = {}
    malformed = 0
    no_match = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError:
            malformed += 1
            kept_lines.append(stripped)
            continue
        if not isinstance(row, dict):
            malformed += 1
            kept_lines.append(stripped)
            continue
        disposition = row.get("disposition")
        if disposition == "no-match":
            no_match += 1
            continue
        key = disposition if isinstance(disposition, str) else "<missing-disposition>"
        histogram[key] = histogram.get(key, 0) + 1
        kept_lines.append(stripped)
    return kept_lines, histogram, malformed, no_match


def prune_shape_rule_dispositions_to_positive(
    wiki_root: Path, *, apply: bool = False
) -> DispositionPruneReport:
    """One-time prune of `_shape_rule_dispositions.jsonl` to its positive
    (non-`no-match`) records (issue athenaeum#1274 AC3/AC4).

    Dry-run by default (`apply=False`): reads the ledger and returns a
    :class:`DispositionPruneReport` with no write. `apply=True` writes
    atomically (:func:`athenaeum.atomic_io.atomic_write_text`) -- the file
    on disk is always either the complete pre-prune content or the complete
    post-prune content, never torn by an interrupted write.

    **The guard (AC4 made mechanical).** A row this function drops is gone
    from the live tree (though always recoverable from git history -- AC3;
    this function does not touch git itself). AC4 asks for the positive
    count to be "verified by count before and after," not merely assumed.
    So after building the candidate output, this function re-runs the SAME
    classifier (:func:`_classify_disposition_ledger_text`) over the text it
    is about to write and confirms that re-parse reports exactly
    `positive_count` surviving rows and ZERO `no-match` rows. Any
    disagreement -- structurally impossible given the same classifier ran
    both times, and exactly what a construction bug (a dropped line, a line
    built from the wrong row) would produce -- raises
    :class:`DispositionPruneMismatchError` and writes NOTHING. This mirrors
    `run_shape_rule_phase`'s own `counts_seen` vs. `tallies` invariant,
    built independently for the identical reason: catch a bug that would
    otherwise cancel itself out in a single bookkeeping pass.

    Returns a report with `applied=False` (no write, even under
    `apply=True`) when the ledger file is missing, or when there is nothing
    to prune (zero `no-match` rows) -- an already-pruned or freshly-pruned
    ledger is left byte-identical rather than churned.
    """
    path = default_shape_rule_dispositions_path(wiki_root)
    if not path.is_file():
        return DispositionPruneReport(
            total_records=0,
            histogram={},
            malformed_lines=0,
            no_match_count=0,
            positive_count=0,
            current_bytes=0,
            projected_bytes=0,
            rows_dropped=0,
            applied=False,
        )
    text = path.read_text(encoding="utf-8")
    current_bytes = len(text.encode("utf-8"))

    kept_lines, histogram, malformed, no_match_count = _classify_disposition_ledger_text(text)
    total_records = sum(histogram.values()) + malformed + no_match_count
    positive_count = total_records - no_match_count
    new_text = "".join(f"{line}\n" for line in kept_lines)
    projected_bytes = len(new_text.encode("utf-8"))

    # The guard: re-classify the constructed output independently rather
    # than trusting the scan pass's own bookkeeping (see docstring).
    verify_kept, _verify_hist, _verify_malformed, verify_no_match = (
        _classify_disposition_ledger_text(new_text)
    )
    if len(verify_kept) != positive_count or verify_no_match != 0:
        raise DispositionPruneMismatchError(
            f"prune guard: re-parse of the constructed output reports "
            f"{len(verify_kept)} surviving row(s) and {verify_no_match} "
            f"no-match row(s); expected {positive_count} and 0. Refusing "
            f"to write {path} -- nothing was written."
        )

    report = DispositionPruneReport(
        total_records=total_records,
        histogram=histogram,
        malformed_lines=malformed,
        no_match_count=no_match_count,
        positive_count=positive_count,
        current_bytes=current_bytes,
        projected_bytes=projected_bytes,
        rows_dropped=no_match_count,
        applied=False,
    )
    if not apply or no_match_count == 0:
        return report
    atomic_write_text(path, new_text)
    return DispositionPruneReport(
        total_records=report.total_records,
        histogram=report.histogram,
        malformed_lines=report.malformed_lines,
        no_match_count=report.no_match_count,
        positive_count=report.positive_count,
        current_bytes=report.current_bytes,
        projected_bytes=report.projected_bytes,
        rows_dropped=report.rows_dropped,
        applied=True,
    )


# ---------------------------------------------------------------------------
# Rollup aggregation (issue athenaeum#903)
# ---------------------------------------------------------------------------


def _group_key_repr(key: Any) -> str:
    """A stable string form of a resolved `group_by` value, for dict keying.

    `json.dumps(sort_keys=True)` so a dict key groups by VALUE rather than by
    identity, and two records whose keys differ only in mapping order collapse
    together as the operator intends. Unserialisable values degrade to `repr`,
    which still groups equal values equally.
    """
    try:
        return json.dumps(key, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(key)


def _compute_rollup_aggregate(
    spec: "RollupSpec", records: list[dict[str, Any]]
) -> Any:
    """Reduce a rollup group's records to the ONE value that crosses into the
    entity record (`docs/field-corrections.md` §12).

    `count` -> the number of records in the group (a windowed count).
    `last`  -> the maximum resolved `of` across the group (a last-event date).

    Raises :class:`ShapeRuleTransformError` if `last`'s `of` cannot resolve for
    any member, so the caller degrades the whole group to the reasoning tiers
    rather than writing a correction computed from a partial group.
    """
    if spec.aggregate == "count":
        return len(records)
    values = [resolve_value_expr(spec.of, record) for record in records]
    comparable = [v for v in values if v is not None]
    if not comparable:
        raise ShapeRuleTransformError(
            "rollup 'last' resolved no non-null values across the group"
        )
    try:
        return max(comparable)
    except TypeError as exc:
        # Mixed, mutually incomparable types (a str beside an int). Refusing is
        # right: a silently coerced max would be an arbitrary answer.
        raise ShapeRuleTransformError(
            f"rollup 'last' values are not mutually comparable: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# §10.1-style phase orchestration
# ---------------------------------------------------------------------------


def run_shape_rule_phase(
    *,
    raw_root: Path,
    wiki_root: Path,
    knowledge_root: Path,
    config: dict[str, Any] | None,
    deadline_check: Callable[[], bool] | None = None,
    dry_run: bool = False,
    store: Store | None = None,
    unclaimed_candidates: list[RawFile] | None = None,
) -> dict[str, Any]:
    """Load rules, evaluate every candidate raw file from
    `intake.discover_raw_files` PLUS `intake.discover_shape_rule_extra_intake_files`
    against them (first-match-wins), and for each match either EMIT a
    correction batch (live mode) or record the disposition without writing
    (observe mode, or an actual `fallthrough` rule) — then append one
    ledger line per `(rule, mode)` pairing with its denominator, tagged
    `rule@version`.

    Issue athenaeum#1096: `discover_raw_files` deliberately never descends into a
    source directory that is itself a configured `recall.extra_intake_roots`
    entry (default `raw/auto-memory`) -- that exemption is correct for
    INTAKE (see that function's docstring) but left the shape-rule phase
    unable to see a tree like `raw/auto-memory/hestia-lanes/`, so a
    `preserve` rule targeting it could never match. This phase, and only
    this phase, also sources candidates from
    :func:`intake.discover_shape_rule_extra_intake_files` -- files one level
    below an extra-intake-root source directory -- so shape rules can
    evaluate that tree while intake discovery stays exactly as it was.

    A record's disposition tally distinguishes `emit`/`fallthrough` (a
    LIVE-mode rule that actually acted) from `observed-emit`/
    `observed-fallthrough` (an OBSERVE-mode rule, or `--dry-run`, that only
    computed what it would have done) from `transform-error` (an `emit`
    rule matched but a value expression failed to resolve — degrades to
    fallthrough, the original raw file is left untouched).

    Issue athenaeum#975: alongside that per-`(rule, mode)` aggregate, this also
    appends one PER-RECORD disposition row (:func:`append_shape_rule_disposition_row`,
    `wiki_root/_shape_rule_dispositions.jsonl`) for every candidate this phase
    evaluates -- unless `dry_run` is set, mirroring the aggregate's own
    dry-run behaviour.

    Issue athenaeum#1274: the ones no rule matched (`rule_id: null`,
    `disposition: "no-match"`) are the exception -- 99.8% of the measured
    ledger, written only when `librarian.shape_rules.log_no_match` is on
    (DEFAULT OFF). Their count lands in the summary as
    `no_match_rows_suppressed` when it is off.

    Returns a summary dict: `rules_loaded`, `rules_skipped_malformed`,
    `files_evaluated`, `files_matched`, `dispositions` (disposition ->
    count across the whole run).

    *store* (issue athenaeum#1132) is the injectable-for-tests
    :class:`~athenaeum.store.Store` a `preserve` rule routes through when
    `librarian.preserved_log_adapter` names an adapter -- same shape as
    :mod:`athenaeum.quarantine`'s `store=` parameter. When omitted, a
    :class:`~athenaeum.store.Store` covering every adapter this *config*
    makes available is built via
    :func:`athenaeum.storage.resolve_store_for_class`. Not consulted at all
    when no matched rule's disposition is `preserve`, or when `preserve`
    resolves to the local `librarian.preserved_log_dir` area (which never
    touches this parameter -- :func:`preserve_raw_file` is unchanged).

    *unclaimed_candidates* (issue athenaeum#1133) is the CALLER-resolved output of
    :func:`athenaeum.intake_audit.discover_unclaimed_shape_rule_candidates`
    -- files the intake audit (issue athenaeum#836) reports as unrecognised,
    appended to the ordinary candidate list exactly as
    `intake.discover_shape_rule_extra_intake_files`'s output already is.
    Not resolved by this function itself: :mod:`athenaeum.intake_audit` is
    Layering L4 and this module is L3, so importing it here would invert
    the documented acyclic boundary -- see that function's docstring. Each
    candidate from this list is evaluated with `is_unclaimed=True`
    (:meth:`MatchSpec.matches`'s hard partition), so only a rule whose
    `match.unclaimed` is `True` can ever match it, and vice versa. `None`
    (the default) preserves this function's pre-athenaeum#1133 candidate set
    verbatim.
    """
    summary: dict[str, Any] = {
        "rules_loaded": 0,
        "rules_skipped_malformed": 0,
        "files_evaluated": 0,
        "files_matched": 0,
        "dispositions": {},
        "disposition_rows_pruned": 0,
    }
    rules, load_errors = load_rules(knowledge_root)
    summary["rules_loaded"] = len(rules)
    summary["rules_skipped_malformed"] = len(load_errors)
    if not rules:
        return summary

    # Lazily resolved (issue athenaeum#1132, QA follow-up): building this
    # unconditionally meant a config with NO preserve+adapter rule at all --
    # e.g. an unrelated, malformed `storage.adapters` entry -- could abort
    # the entire shape-rules run via `resolve_store_for_class`'s validation,
    # a config error that was inert before this feature existed. Resolved
    # (and cached) only the first time a `preserve` rule actually needs to
    # route through an adapter, inside the loop below.
    resolved_store: Store | None = None
    max_records = resolve_shape_rules_max_records_per_run(config)
    # Issue athenaeum#1096: the extra-intake-root subtree (default
    # `raw/auto-memory`) is appended AFTER ordinary intake candidates, not
    # merged in place -- this phase's own first-match-wins / max-records
    # ordering is otherwise unspecified and untested; appending is the
    # minimal change that makes the extra-intake tree reachable at all
    # without disturbing the existing candidate order for every other
    # source.
    # Issue athenaeum#1133: `unclaimed_candidates` is appended last, same
    # convention, and each is paired with `is_unclaimed=True` so the
    # per-candidate loop below can enforce `MatchSpec`'s hard partition.
    candidates: list[tuple[RawFile, bool]] = [
        (r, False)
        for r in (
            discover_raw_files(raw_root, config)
            + discover_shape_rule_extra_intake_files(raw_root, config)
        )
    ]
    if unclaimed_candidates:
        candidates.extend((r, True) for r in unclaimed_candidates)

    # Issue athenaeum#1274: resolved once per run, not per candidate.
    log_no_match = resolve_shape_rules_log_no_match(config)
    # Issue athenaeum#1229: dedupe-at-write. Built ONCE, up front -- see the
    # "Dedupe-at-write + retention" section above `_disposition_row_key` for
    # why this is cheap even on a long-lived corpus. `None` in dry-run: no
    # row is ever appended on that path either, so there is nothing to
    # dedupe against and no reason to pay the read.
    existing_disposition_keys: set[tuple[str, str, str, str | None, str]] | None = (
        None if dry_run else _load_existing_disposition_keys(wiki_root)
    )

    # Per-(rule, mode) tallies -- keyed so an observe-mode pass and a
    # live-mode pass for the SAME rule (an operator edited it mid-run
    # history) never merge into one ledger line's denominator.
    tallies: dict[tuple[str, str], dict[str, int]] = {}

    # Issue athenaeum#903: `rollup` members, keyed by (rule, mode, group key). The
    # aggregate is not knowable until the scan finishes, so members accumulate
    # here and are compiled into one correction per group afterwards.
    rollup_groups: dict[
        tuple[str, str, str], list[tuple[ShapeRule, dict[str, Any], RawFile]]
    ] = {}

    # Issue athenaeum#903: records SEEN per (rule, mode) -- the denominator the
    # ledger's dispositions must sum to. Counted independently of `_tally` on
    # purpose: if both came off the same increment the invariant would be a
    # tautology and could never catch a disposition that forgot to tally.
    counts_seen: dict[tuple[str, str], int] = {}

    def _tally(
        rule_tag: str,
        mode: str,
        disposition: str,
        *,
        raw: RawFile,
        record: dict[str, Any],
    ) -> None:
        counts = tallies.setdefault((rule_tag, mode), {})
        counts[disposition] = counts.get(disposition, 0) + 1
        summary["dispositions"][disposition] = summary["dispositions"].get(disposition, 0) + 1
        # Issue athenaeum#975: every disposition this loop reaches funnels through
        # here except the `matched_rule is None` early-continue (handled
        # separately below) -- so a per-record row is appended in exactly one
        # place, structurally, rather than at each of the dispositions' many
        # call sites.
        if not dry_run:
            assert existing_disposition_keys is not None
            _append_disposition_row_deduped(
                wiki_root,
                _shape_rule_disposition_row(
                    raw=raw, record=record, rule_id=rule_tag, disposition=disposition
                ),
                existing_disposition_keys,
            )

    evaluated = 0
    for raw, is_unclaimed in candidates:
        if deadline_check is not None and deadline_check():
            break
        if evaluated >= max_records:
            break
        evaluated += 1
        summary["files_evaluated"] += 1

        record, fmt = _record_and_format(raw)
        matched_rule = next(
            (
                r
                for r in rules
                if r.match.matches(raw=raw, record=record, fmt=fmt, is_unclaimed=is_unclaimed)
            ),
            None,
        )
        if matched_rule is None:
            # Issue athenaeum#975: the interesting shapes for athenaeum#905's detector are
            # precisely the ones no rule claims -- so this candidate still gets
            # a disposition row, just with no rule/tier to attribute it to.
            #
            # Issue athenaeum#1274: ...but only when something is going to read
            # it. These rows were 99.8% of a 341 MB ledger, and their sole
            # consumer (that same athenaeum#905 detector) is itself off by
            # default -- so `librarian.shape_rules.log_no_match` gates the
            # write, default OFF. See the "Dedupe-at-write + retention"
            # section above, lever 3.
            if not dry_run and log_no_match:
                assert existing_disposition_keys is not None
                _append_disposition_row_deduped(
                    wiki_root,
                    _shape_rule_disposition_row(
                        raw=raw, record=record, rule_id=None, disposition="no-match"
                    ),
                    existing_disposition_keys,
                )
            elif not dry_run:
                # Counted only on a real run. A dry run writes NO disposition
                # row of any kind (athenaeum#975's own behaviour, unchanged),
                # so attributing its silence to athenaeum#1274's suppression
                # would misreport why the ledger is empty.
                summary["no_match_rows_suppressed"] = (
                    summary.get("no_match_rows_suppressed", 0) + 1
                )
            continue
        summary["files_matched"] += 1
        rule_tag = matched_rule.qualified_name
        _seen_key = (rule_tag, matched_rule.mode)
        counts_seen[_seen_key] = counts_seen.get(_seen_key, 0) + 1

        is_live = matched_rule.mode == "live" and not dry_run

        if matched_rule.disposition == "fallthrough":
            disposition = "fallthrough" if is_live else "observed-fallthrough"
            _tally(rule_tag, matched_rule.mode, disposition, raw=raw, record=record)
            # Nothing written -- the file is left exactly as discovery
            # found it, for the ordinary tiered ladder to process normally.
            continue

        # Issue athenaeum#903: an audited discard of an information-free record.
        # Never a hard delete -- the content is committed before it is removed,
        # so it stays recoverable from history.
        if matched_rule.disposition == "drop":
            if not is_live:
                _tally(rule_tag, matched_rule.mode, "observed-drop", raw=raw, record=record)
                continue
            drop_raw_file(knowledge_root, raw.path, rule_tag=rule_tag)
            _tally(rule_tag, matched_rule.mode, "drop", raw=raw, record=record)
            log.info(
                "shape-rules: %s dropped %s as information-free "
                "(recoverable from git history)",
                rule_tag,
                raw.ref,
            )
            continue

        # Issue athenaeum#903: a long-lived SOURCE DOCUMENT. Marked compiled-exempt
        # in the manifest under the knowledge root; the file itself is NOT
        # deleted and NOT compiled. Discovery skips it from the next run on.
        if matched_rule.disposition == "retain":
            if not is_live:
                _tally(rule_tag, matched_rule.mode, "observed-retain", raw=raw, record=record)
                continue
            mark_exempt(knowledge_root, [raw.ref])
            _tally(rule_tag, matched_rule.mode, "retain", raw=raw, record=record)
            log.info(
                "shape-rules: %s retained %s as a preserved source document "
                "(compiled-exempt; file left in place)",
                rule_tag,
                raw.ref,
            )
            continue

        # Issue athenaeum#837: a LOG-SHAPED family. The file is MOVED out of raw
        # intake into the operator-configured preserved area and kept whole as
        # a source artifact; if the rule also carries a `correction`, the fact
        # it compiles points BACK at the moved log as its provenance.
        #
        # Why no `mark_exempt` here, unlike `retain`: the move already puts the
        # file outside the only tree `discover_raw_files` walks, so exemption
        # would be redundant — and worse than redundant, it would be WRONG. The
        # exempt key is `source/filename`, so exempting it would suppress a
        # FUTURE, genuinely-new file that happens to reuse the name (a daily
        # log writer emitting `today.md` every day is exactly that shape). The
        # move is the mechanism; the manifest is not involved.
        if matched_rule.disposition == "preserve":
            if not is_live:
                _tally(rule_tag, matched_rule.mode, "observed-preserve", raw=raw, record=record)
                continue
            preserved_dir = resolve_preserved_log_dir(config)
            preserved_adapter_name = resolve_preserved_log_adapter(config)
            if preserved_adapter_name is not None and preserved_dir is not None:
                # AC1 (issue athenaeum#1132): both configured -- the adapter wins.
                # Loud, not silent: an operator who set both almost certainly
                # meant to migrate off the local directory, and a silent
                # shadow would be surprising.
                log.warning(
                    "shape-rules: %s has BOTH librarian.preserved_log_dir=%r "
                    "and librarian.preserved_log_adapter=%r configured -- the "
                    "adapter wins and shadows the directory (issue athenaeum#1132)",
                    rule_tag,
                    preserved_dir,
                    preserved_adapter_name,
                )
            if preserved_adapter_name is None and preserved_dir is None:
                log.warning(
                    "shape-rules: %s matched %s as log-shaped but no "
                    "preserved-log area is configured "
                    "(librarian.preserved_log_dir / "
                    "librarian.preserved_log_adapter) -- falling through to "
                    "the reasoning tiers, raw file untouched",
                    rule_tag,
                    raw.ref,
                )
                _tally(rule_tag, matched_rule.mode, "preserve-unconfigured", raw=raw, record=record)
                continue
            if preserved_adapter_name is not None:
                # Fail closed, loudly (AC1): an adapter name that does not
                # resolve must never silently fall back to the directory.
                known_adapters = available_adapters(config)
                if preserved_adapter_name not in known_adapters:
                    raise StorageConfigError(
                        f"librarian.preserved_log_adapter="
                        f"{preserved_adapter_name!r} names an unknown storage "
                        f"adapter; known adapters: {sorted(known_adapters)} "
                        "(issue athenaeum#1132)"
                    )
            # Build the correction BEFORE moving: a transform that cannot
            # resolve must leave the raw file exactly where it was, so the
            # record still reaches the tiers. Moving first would strand it.
            corr_record: dict[str, Any] | None = None
            corr_spec = matched_rule.correction
            if corr_spec is not None:
                try:
                    corr_record = build_correction_record(
                        corr_spec, record, rule_tag=rule_tag
                    )
                except ShapeRuleTransformError as exc:
                    log.warning(
                        "shape-rules: rule %s matched %s but its transform "
                        "failed (%s) -- falling through to the reasoning "
                        "tiers, raw file untouched",
                        rule_tag,
                        raw.ref,
                        exc,
                    )
                    _tally(rule_tag, matched_rule.mode, "transform-error", raw=raw, record=record)
                    continue
            if preserved_adapter_name is not None:
                if resolved_store is None:
                    # Resolve (and cache) on first actual need, not up
                    # front -- see the lazy-resolution note above
                    # `max_records` (issue athenaeum#1132, QA follow-up).
                    resolved_store = (
                        store
                        if store is not None
                        else resolve_store_for_class(None, config, knowledge_root)
                    )
                try:
                    outcome = preserve_raw_file_via_store(
                        knowledge_root,
                        raw.path,
                        adapter_name=preserved_adapter_name,
                        source=raw.source,
                        rule_tag=rule_tag,
                        store=resolved_store,
                    )
                except (OSError, StoreConflictError) as exc:
                    # AC5 fail-closed: `put` refused or failed (a genuine,
                    # content-mismatched collision, or EXDEV/another
                    # physical write error -- the expected case for a
                    # routed adapter on a different filesystem, not an edge
                    # case). The raw file was never touched. Reuse the
                    # existing `preserve-failed` tag -- this is the same
                    # outcome the local-directory path already reports on a
                    # failed move.
                    log.warning(
                        "shape-rules: %s failed to preserve %s via storage "
                        "adapter %r (%s) -- raw file untouched",
                        rule_tag,
                        raw.ref,
                        preserved_adapter_name,
                        exc,
                    )
                    _tally(rule_tag, matched_rule.mode, "preserve-failed", raw=raw, record=record)
                    continue
                dest = outcome.dest_path
                freshly_written = outcome.freshly_written
                orphaned = outcome.orphaned
            else:
                assert preserved_dir is not None  # guaranteed by the check above
                dest = preserve_raw_file(
                    knowledge_root,
                    raw.path,
                    preserved_dir=preserved_dir,
                    source=raw.source,
                    rule_tag=rule_tag,
                )
                # The local-directory path has no convergence concept (a
                # collision is always suffixed into a fresh, unique
                # destination -- never a retry against an existing key), so
                # a successful move is always "fresh" and never orphaned.
                freshly_written = True
                orphaned = False
            if dest is None:
                _tally(rule_tag, matched_rule.mode, "preserve-failed", raw=raw, record=record)
                continue
            # Only emit the correction on the call that actually WROTE the
            # artifact (issue athenaeum#1132, QA follow-up): a converged retry
            # (`freshly_written=False`) means a PRIOR run already durably
            # wrote this artifact and, if it carried a correction, already
            # compiled and wrote that batch -- re-emitting it here would be
            # a duplicate fact, not a fix.
            if freshly_written and corr_record is not None and corr_spec is not None:
                # Point the fact at the artifact WITHOUT disturbing its
                # precedence. The declared `source` is capped at machine tier
                # and validated at LOAD time; an unknown source type silently
                # falls to the rank-9 default (`precedence.source_rank`), so
                # replacing the whole scalar would quietly demote every fact a
                # preserved log produces. Keeping `type` and putting the
                # pointer in `ref` -- which is what `ref` is for -- preserves
                # the rank and still resolves to the log.
                pointer = preserved_log_source_pointer(
                    knowledge_root, dest, fmt=fmt
                )
                declared = parse_source(corr_spec.source)
                corr_record["source"] = {
                    "type": declared.type if declared else "script",
                    "ref": pointer,
                    "notes": (
                        f"compiled by shape rule {rule_tag} from a preserved "
                        f"log (asserted as {declared.ref})"
                        if declared
                        else f"compiled by shape rule {rule_tag} from a preserved log"
                    ),
                }
                write_correction_batch(
                    raw_root=raw_root,
                    source=raw.source,
                    submitter=f"shape-rule:{rule_tag}",
                    records=[corr_record],
                )
            if orphaned:
                # Ledger honesty (issue athenaeum#1132, QA follow-up): the
                # artifact is durably written, but the source is still in
                # raw/ -- tallying this as terminal `preserve` would
                # contradict the ledger AND permanently jam the file (the
                # next run's `put` would collide against its own prior
                # write with no way to distinguish "already done" from "in
                # conflict" if this call didn't already resolve that via
                # content comparison). `_disposition_tier` gives this the
                # same `None` (deferred) tier as `preserve-unconfigured` /
                # `preserve-failed` by simply not being in
                # `_TIER_0_DISPOSITIONS`.
                _tally(
                    rule_tag,
                    matched_rule.mode,
                    "preserve-orphaned-source",
                    raw=raw,
                    record=record,
                )
                log.info(
                    "shape-rules: %s preserved %s to %s via storage adapter "
                    "%r, but the source could not be removed -- tallied "
                    "preserve-orphaned-source, will retry removal on the "
                    "next run%s",
                    rule_tag,
                    raw.ref,
                    dest,
                    preserved_adapter_name,
                    " (fact already compiled with a source pointer back to it)"
                    if corr_record is not None
                    else "",
                )
                continue
            _tally(rule_tag, matched_rule.mode, "preserve", raw=raw, record=record)
            log.info(
                "shape-rules: %s preserved %s as a log artifact at %s%s",
                rule_tag,
                raw.ref,
                dest,
                " (fact compiled with a source pointer back to it)"
                if corr_record is not None
                else "",
            )
            continue

        # Issue athenaeum#903: N records aggregate into ONE correction. Records are
        # accumulated here and compiled after the scan -- the aggregate is not
        # knowable until every member of the group has been seen.
        if matched_rule.disposition == "rollup":
            assert matched_rule.rollup is not None  # guaranteed by schema
            try:
                group_key = resolve_value_expr(matched_rule.rollup.group_by, record)
            except ShapeRuleTransformError as exc:
                log.warning(
                    "shape-rules: rule %s matched %s but its rollup group_by "
                    "failed to resolve (%s) -- falling through to the "
                    "reasoning tiers",
                    rule_tag,
                    raw.ref,
                    exc,
                )
                _tally(rule_tag, matched_rule.mode, "transform-error", raw=raw, record=record)
                continue
            rollup_groups.setdefault(
                (rule_tag, matched_rule.mode, _group_key_repr(group_key)), []
            ).append((matched_rule, record, raw))
            _tally(
                rule_tag,
                matched_rule.mode,
                "rollup" if is_live else "observed-rollup",
                raw=raw,
                record=record,
            )
            continue

        # disposition == "emit"
        assert matched_rule.correction is not None  # guaranteed by schema
        try:
            corr_record = build_correction_record(
                matched_rule.correction, record, rule_tag=rule_tag
            )
        except ShapeRuleTransformError as exc:
            log.warning(
                "shape-rules: rule %s matched %s but its transform failed "
                "(%s) -- falling through to the reasoning tiers",
                rule_tag,
                raw.ref,
                exc,
            )
            _tally(rule_tag, matched_rule.mode, "transform-error", raw=raw, record=record)
            continue

        if matched_rule.mode != "live" or dry_run:
            _tally(rule_tag, matched_rule.mode, "observed-emit", raw=raw, record=record)
            continue

        batch_path = write_correction_batch(
            raw_root=raw_root,
            source=raw.source,
            submitter=f"shape-rule:{rule_tag}",
            records=[corr_record],
        )
        retire_compiled_raw_file(knowledge_root, raw.path, rule_tag=rule_tag)
        _tally(rule_tag, matched_rule.mode, "emit", raw=raw, record=record)
        log.info(
            "shape-rules: %s compiled %s into correction batch %s",
            rule_tag,
            raw.ref,
            batch_path,
        )

    # Issue athenaeum#903: compile each accumulated rollup group into ONE correction.
    # Observe-mode groups are computed and ledgered above but never written here
    # -- observe mode writes nothing, by definition.
    summary["rollups_written"] = 0
    for (rule_tag, mode, _group_key), members in sorted(rollup_groups.items()):
        if mode != "live" or dry_run or not members:
            continue
        matched_rule, first_record, first_raw = members[0]
        assert matched_rule.rollup is not None  # guaranteed by schema
        try:
            aggregate = _compute_rollup_aggregate(
                matched_rule.rollup, [rec for _r, rec, _raw in members]
            )
            corr_record = build_correction_record(
                matched_rule.correction,  # type: ignore[arg-type]  # schema-guaranteed
                first_record,
                rule_tag=rule_tag,
            )
        except ShapeRuleTransformError as exc:
            log.warning(
                "shape-rules: rule %s matched %d record(s) but its rollup "
                "failed to compile (%s) -- the raw files are left for the "
                "reasoning tiers",
                rule_tag,
                len(members),
                exc,
            )
            continue
        # The aggregate REPLACES the templated value: §12 lets exactly one
        # thing cross the boundary -- "a last-event date, a windowed count".
        corr_record["value"] = aggregate
        corr_record["note"] = (
            f"rollup of {len(members)} record(s) by shape rule {rule_tag}"
        )
        batch_path = write_correction_batch(
            raw_root=raw_root,
            source=first_raw.source,
            submitter=f"shape-rule:{rule_tag}",
            records=[corr_record],
        )
        for _rule, _rec, member_raw in members:
            retire_compiled_raw_file(knowledge_root, member_raw.path, rule_tag=rule_tag)
        summary["rollups_written"] += 1
        log.info(
            "shape-rules: %s rolled %d record(s) up into correction batch %s",
            rule_tag,
            len(members),
            batch_path,
        )

    if not dry_run:
        for (rule_tag, mode), counts in tallies.items():
            records_total = sum(counts.values())
            # Issue athenaeum#903's denominator invariant: the per-disposition counts
            # must sum to the records this rule SAW. It is asserted here, at the
            # one place the ledger line is built, so a future disposition that
            # forgets to tally is caught by every run rather than by review.
            if records_total != counts_seen.get((rule_tag, mode), records_total):
                log.error(
                    "shape-rules: denominator invariant violated for %s (%s): "
                    "dispositions sum to %d but %d record(s) were seen "
                    "(issue athenaeum#903)",
                    rule_tag,
                    mode,
                    records_total,
                    counts_seen.get((rule_tag, mode), records_total),
                )
            append_shape_rules_ledger(
                wiki_root,
                {
                    "schema_version": 1,
                    "run_at": now_iso(),
                    "rule": rule_tag,
                    "mode": mode,
                    "records_seen": counts_seen.get((rule_tag, mode), records_total),
                    "records_total": records_total,
                    "dispositions": counts,
                },
            )

        # Issue athenaeum#1229 part 3: bound the ledger independently of
        # dedupe-at-write above -- see the "Dedupe-at-write + retention"
        # section for why retention alone and dedupe alone are each
        # insufficient on their own. Issue athenaeum#1274 re-keyed the window
        # from `librarian.rule_proposals.window_days` (a READ window doing
        # double duty) onto retention's own key, same default.
        _retention_days = resolve_shape_rules_dispositions_retention_days(config)
        _pruned = prune_shape_rule_dispositions(wiki_root, window_days=_retention_days)
        if _pruned:
            summary["disposition_rows_pruned"] = _pruned
            log.info(
                "shape-rules: pruned %d disposition row(s) older than %d day(s) "
                "(librarian.shape_rules.dispositions_retention_days, "
                "issue athenaeum#1274)",
                _pruned,
                _retention_days,
            )
        _suppressed = summary.get("no_match_rows_suppressed", 0)
        if _suppressed:
            # Breadcrumb for an operator staring at an empty athenaeum#905
            # detector: name the key that has to be flipped.
            log.info(
                "shape-rules: suppressed %d no-match disposition row(s) "
                "(librarian.shape_rules.log_no_match is off; enable it if "
                "librarian.rule_proposals.enabled is on -- issue athenaeum#1274)",
                _suppressed,
            )

    return summary
