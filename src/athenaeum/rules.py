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
import os
import re
import shutil
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError

from athenaeum.atomic_io import atomic_write_text
from athenaeum.compiled_exempt import mark_exempt
from athenaeum.config import (
    resolve_preserved_log_dir,
    resolve_shape_rules_max_records_per_run,
)
from athenaeum.corrections import compute_correction_id
from athenaeum.intake import discover_raw_files
from athenaeum.models import RawFile, RawFileTooLargeError, parse_frontmatter
from athenaeum.provenance import parse_source

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
    anything. All present keys must hold (AND) for the rule to match."""

    model_config = ConfigDict(extra="forbid")

    source: str | None = None
    format: Literal["md", "jsonl"] | None = None
    filename_glob: str | None = None
    key_fingerprint: str | None = None
    fields: dict[str, FieldPredicate] = Field(default_factory=dict)

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

    def matches(self, *, raw: RawFile, record: dict[str, Any], fmt: str) -> bool:
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
            if field_name not in record or not predicate.matches(record[field_name]):
                return False
        return True


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
    """

    model_config = ConfigDict(extra="forbid")

    target: dict[str, Any]
    op: Literal["set", "add", "remove"]
    field: str
    value: Any
    source: str
    observed_at: Any = None
    note: Any = None

    @field_validator("field")
    @classmethod
    def _validate_field_name(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("correction.field must be a non-empty string")
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
        observed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
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
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    lines = [json.dumps(envelope, sort_keys=True)]
    lines.extend(json.dumps(r, sort_keys=True) for r in records)
    atomic_write_text(out_path, "\n".join(lines) + "\n")
    return out_path


def retire_compiled_raw_file(knowledge_root: Path, raw_path: Path, *, rule_tag: str) -> bool:
    """Retire a raw intake file the shape-rule engine has fully compiled
    into a correction batch (`emit`, live mode) — `git rm` after a
    provenance-snapshot commit, recoverable from git history, never
    hard-deleted. Same two-commit pattern as
    `corrections.retire_batch`/`adapter-contract.md` §4.5, with its OWN
    commit wording (see module docstring "Decisions" for why this is not a
    call to `corrections.retire_batch`).

    Best-effort: falls back to a plain unlink outside a git repo (test
    fixtures), same fallback `retire_batch` uses. Returns `True` on success.
    """
    return _retire_raw_file(
        knowledge_root,
        raw_path,
        snapshot_reason="before compile",
        retire_reason=f"compiled into a correction batch by {rule_tag}",
    )


def drop_raw_file(knowledge_root: Path, raw_path: Path, *, rule_tag: str) -> bool:
    """Retire a raw intake file a `drop` rule judged information-free
    (issue athenaeum#903) — the SAME two-commit provenance-snapshot-then-`git rm`
    convention :func:`retire_compiled_raw_file` uses, with drop wording.

    The distinction from `emit`'s retirement is the reason, not the mechanism,
    and the mechanism is the point: a `drop` is an **audited discard**, never a
    hard delete. The content is committed before it is removed, so it stays
    recoverable from history (athenaeum#903 AC: "the discard is recoverable from
    history") and the audit counter in the ledger says how many were discarded
    and by which rule.
    """
    return _retire_raw_file(
        knowledge_root,
        raw_path,
        snapshot_reason="before drop",
        retire_reason=f"dropped as information-free by {rule_tag}",
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


def _retire_raw_file(
    knowledge_root: Path,
    raw_path: Path,
    *,
    snapshot_reason: str,
    retire_reason: str,
) -> bool:
    """Shared two-commit retirement: snapshot the content, then `git rm` it.

    Committing BEFORE the removal is what makes every retirement recoverable —
    a file that was never committed would be unrecoverable once unlinked, which
    is the difference between an audited discard and a deletion.
    """
    if not raw_path.exists():
        return True
    if (knowledge_root / ".git").is_dir():
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
        if rm_result.returncode == 0:
            _git(
                knowledge_root,
                "commit",
                "-m",
                f"shape-rules: {rel} {retire_reason}",
            )
            return True
    try:
        raw_path.unlink()
        return True
    except OSError:
        log.warning("shape-rules: failed to retire raw file %s", raw_path)
        return False


# ---------------------------------------------------------------------------
# §5.3-pattern audit ledger
# ---------------------------------------------------------------------------

SHAPE_RULES_LEDGER_FILENAME = "_shape_rules_applied.jsonl"


def default_shape_rules_ledger_path(wiki_root: Path) -> Path:
    return wiki_root / SHAPE_RULES_LEDGER_FILENAME


def _append_jsonl_line(path: Path, line: str) -> None:
    """Same append-only-JSONL discipline as
    `corrections._append_jsonl_line` / `provenance._append_jsonl_line`: a
    single small `O_APPEND` write is atomic on local filesystems."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def append_shape_rules_ledger(wiki_root: Path, record: dict[str, Any]) -> None:
    path = default_shape_rules_ledger_path(wiki_root)
    _append_jsonl_line(path, json.dumps(record, sort_keys=True) + "\n")


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
) -> dict[str, Any]:
    """Load rules, evaluate every candidate raw file
    `intake.discover_raw_files` returns against them (first-match-wins), and
    for each match either EMIT a correction batch (live mode) or record the
    disposition without writing (observe mode, or an actual `fallthrough`
    rule) — then append one ledger line per `(rule, mode)` pairing with its
    denominator, tagged `rule@version`.

    A record's disposition tally distinguishes `emit`/`fallthrough` (a
    LIVE-mode rule that actually acted) from `observed-emit`/
    `observed-fallthrough` (an OBSERVE-mode rule, or `--dry-run`, that only
    computed what it would have done) from `transform-error` (an `emit`
    rule matched but a value expression failed to resolve — degrades to
    fallthrough, the original raw file is left untouched).

    Returns a summary dict: `rules_loaded`, `rules_skipped_malformed`,
    `files_evaluated`, `files_matched`, `dispositions` (disposition ->
    count across the whole run).
    """
    summary: dict[str, Any] = {
        "rules_loaded": 0,
        "rules_skipped_malformed": 0,
        "files_evaluated": 0,
        "files_matched": 0,
        "dispositions": {},
    }
    rules, load_errors = load_rules(knowledge_root)
    summary["rules_loaded"] = len(rules)
    summary["rules_skipped_malformed"] = len(load_errors)
    if not rules:
        return summary

    max_records = resolve_shape_rules_max_records_per_run(config)
    candidates = discover_raw_files(raw_root, config)

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

    def _tally(rule_tag: str, mode: str, disposition: str) -> None:
        counts = tallies.setdefault((rule_tag, mode), {})
        counts[disposition] = counts.get(disposition, 0) + 1
        summary["dispositions"][disposition] = summary["dispositions"].get(disposition, 0) + 1

    evaluated = 0
    for raw in candidates:
        if deadline_check is not None and deadline_check():
            break
        if evaluated >= max_records:
            break
        evaluated += 1
        summary["files_evaluated"] += 1

        record, fmt = _record_and_format(raw)
        matched_rule = next(
            (r for r in rules if r.match.matches(raw=raw, record=record, fmt=fmt)), None
        )
        if matched_rule is None:
            continue
        summary["files_matched"] += 1
        rule_tag = matched_rule.qualified_name
        _seen_key = (rule_tag, matched_rule.mode)
        counts_seen[_seen_key] = counts_seen.get(_seen_key, 0) + 1

        is_live = matched_rule.mode == "live" and not dry_run

        if matched_rule.disposition == "fallthrough":
            disposition = "fallthrough" if is_live else "observed-fallthrough"
            _tally(rule_tag, matched_rule.mode, disposition)
            # Nothing written -- the file is left exactly as discovery
            # found it, for the ordinary tiered ladder to process normally.
            continue

        # Issue athenaeum#903: an audited discard of an information-free record.
        # Never a hard delete -- the content is committed before it is removed,
        # so it stays recoverable from history.
        if matched_rule.disposition == "drop":
            if not is_live:
                _tally(rule_tag, matched_rule.mode, "observed-drop")
                continue
            drop_raw_file(knowledge_root, raw.path, rule_tag=rule_tag)
            _tally(rule_tag, matched_rule.mode, "drop")
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
                _tally(rule_tag, matched_rule.mode, "observed-retain")
                continue
            mark_exempt(knowledge_root, [raw.ref])
            _tally(rule_tag, matched_rule.mode, "retain")
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
                _tally(rule_tag, matched_rule.mode, "observed-preserve")
                continue
            preserved_dir = resolve_preserved_log_dir(config)
            if preserved_dir is None:
                log.warning(
                    "shape-rules: %s matched %s as log-shaped but no "
                    "preserved-log area is configured "
                    "(librarian.preserved_log_dir) -- falling through to the "
                    "reasoning tiers, raw file untouched",
                    rule_tag,
                    raw.ref,
                )
                _tally(rule_tag, matched_rule.mode, "preserve-unconfigured")
                continue
            # Build the correction BEFORE moving: a transform that cannot
            # resolve must leave the raw file exactly where it was, so the
            # record still reaches the tiers. Moving first would strand it.
            corr_record: dict[str, Any] | None = None
            if matched_rule.correction is not None:
                try:
                    corr_record = build_correction_record(
                        matched_rule.correction, record, rule_tag=rule_tag
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
                    _tally(rule_tag, matched_rule.mode, "transform-error")
                    continue
            dest = preserve_raw_file(
                knowledge_root,
                raw.path,
                preserved_dir=preserved_dir,
                source=raw.source,
                rule_tag=rule_tag,
            )
            if dest is None:
                _tally(rule_tag, matched_rule.mode, "preserve-failed")
                continue
            if corr_record is not None:
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
                declared = parse_source(matched_rule.correction.source)
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
            _tally(rule_tag, matched_rule.mode, "preserve")
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
                _tally(rule_tag, matched_rule.mode, "transform-error")
                continue
            rollup_groups.setdefault(
                (rule_tag, matched_rule.mode, _group_key_repr(group_key)), []
            ).append((matched_rule, record, raw))
            _tally(
                rule_tag,
                matched_rule.mode,
                "rollup" if is_live else "observed-rollup",
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
            _tally(rule_tag, matched_rule.mode, "transform-error")
            continue

        if matched_rule.mode != "live" or dry_run:
            _tally(rule_tag, matched_rule.mode, "observed-emit")
            continue

        batch_path = write_correction_batch(
            raw_root=raw_root,
            source=raw.source,
            submitter=f"shape-rule:{rule_tag}",
            records=[corr_record],
        )
        retire_compiled_raw_file(knowledge_root, raw.path, rule_tag=rule_tag)
        _tally(rule_tag, matched_rule.mode, "emit")
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
                    "run_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "rule": rule_tag,
                    "mode": mode,
                    "records_seen": counts_seen.get((rule_tag, mode), records_total),
                    "records_total": records_total,
                    "dispositions": counts,
                },
            )

    return summary
