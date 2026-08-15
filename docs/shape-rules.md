<!-- SPDX-License-Identifier: Apache-2.0 -->

# The shape-rule engine — declarative YAML rules that compile foreign shapes

**Status:** MVP. Issue athenaeum#901. Two dispositions ship in this slice (`emit`,
`fallthrough`); `drop`/`retain`/`rollup` and compiled-exempt retirement are a
later slice (athenaeum#903). Automatic rule generation is also a later slice, not
this one.

Companion to [`field-corrections.md`](field-corrections.md), which this
document assumes throughout — the shape-rule engine's `emit` disposition
writes the exact conformance format that document specifies, and every
correction the engine emits is applied by the exact same unmodified
machinery an external writer's own batch would go through.

---

## 1. What this is

Some raw intake has a recognisable, stable shape: a delivery-status monitor's
bounce record, a CRM export row, a third-party enrichment payload. A human
operator who has seen the shape once can write down, once, "when a record
looks like this, it means that" — and never pay an LLM call to re-derive the
same conclusion on the next ten thousand records shaped identically.

A **shape rule** is that lookup entry, expressed as data:

```yaml
version: 1
name: example-contact-bounce
mode: observe
match:
  source: delivery-monitor
  format: jsonl
  fields:
    status:
      exact: bounced
disposition: emit
correction:
  target:
    type: person
    handle:
      email: "$email"
  op: set
  field: bounced
  value: "$status_date"
  source: "script:example-contact-bounce"
  observed_at: "$observed_at"
  note: "auto-compiled from a delivery-status monitor record"
```

Rules are **data, not code**. There is no `eval`, no templating language, and
the transform vocabulary is closed and enumerated (§4). A rule that tries to
step outside that vocabulary fails schema validation before it ever runs —
the failure is at `athenaeum run` start, not at 3am against real records.

---

## 2. Where rules live, and how they load

Rules load from `<knowledge_root>/rules/*.yaml` and are pydantic-schema-
validated at the start of every `athenaeum run` (`athenaeum.rules.load_rules`).

A malformed rule — bad YAML, a schema violation, an unknown transform
function — is **skipped with a loud `log.error` line**, never silently
dropped and never a fatal error for the run. Its would-be-matching files are
simply never evaluated against it, so they take the ordinary tiered ladder
exactly as if the rule did not exist. Fix the rule and it participates on the
next run.

**Rules are never shipped as engine defaults.** `athenaeum` itself ships zero
rules; `<knowledge_root>/rules/` does not exist until an operator creates it
or runs `athenaeum init --with-rules` (§7), which copies **example** files an
operator is expected to read, adapt, and (per §5) graduate out of observe
mode deliberately.

---

## 3. Match

Every key in `match:` is optional; an omitted key matches anything. All
present keys must hold (logical AND) for the rule to match a candidate raw
file. Rules are evaluated in filename-sorted order and the **first match
wins** — a candidate file matches at most one rule per run.

| Key | Matches against | Shape |
|---|---|---|
| `source` | the raw file's `raw/<source>/` directory name | exact string |
| `format` | the raw file's extension (`md` \| `jsonl`) | `md` \| `jsonl` |
| `filename_glob` | the raw file's filename | glob pattern |
| `key_fingerprint` | the matched RECORD's top-level key set (§3.1) | 16 lowercase hex chars |
| `fields` | individual record field values (§3.2) | `{<field>: <predicate>}` |

### 3.1 The "record" a rule matches against

- A `.md` raw file's record is its **frontmatter dict** — body text is not
  matchable by a field predicate.
- A `.jsonl` raw file's record is its **first line**, parsed as a JSON
  object — mirrors the field-correction envelope's own "read the first
  line" streaming discipline. A multi-record-per-file foreign export is a
  known MVP limitation, not a silent gap.
- Anything else (unparseable content, an empty file, a non-object first
  line) yields an empty record `{}`. No field predicate or fingerprint can
  match an empty record, so the file falls straight through to the ordinary
  tiered ladder — conformance sets *how deep*, never *whether*
  (`field-corrections.md` §1.1), one layer up.

`key_fingerprint` is `sha256(canonical_json(sorted(record.keys())))[:16]` —
the SAME construction `field-corrections.md` §5.2 uses for `correction_id`,
computed once per candidate record by `athenaeum.rules.record_key_fingerprint`.
It lets a rule assert "this is shaped like the contact-sync export" without
hand-coding key order.

### 3.2 Field predicates

Exactly one of three forms per field — matching the acceptance criterion's
"exact / glob / list membership" vocabulary literally:

```yaml
fields:
  status: {exact: "bounced"}
  filename: {glob: "*.csv"}
  category: {in: ["cold", "warm"]}
```

A field absent from the record never matches, regardless of predicate.

---

## 4. Transform — field interpolation, closed function vocabulary

The `correction:` block (required when `disposition: emit`, forbidden when
`disposition: fallthrough`) is a **template over the matched record**,
resolved by `athenaeum.rules.resolve_value_expr` — a small, fixed, code-owned
interpreter over already-`yaml.safe_load`'d data. **Nothing in a rule is ever
`eval`'d or run through a templating language.**

Every value in `target` / `value` / `observed_at` / `note` is exactly one of:

1. **A literal** — any YAML scalar, list, or mapping with no `$field` string
   and no `fn` key. Returned unchanged.
2. **A whole-value field reference** — a string of the exact form `"$name"`,
   substituted with `record["name"]` verbatim (any type: string, number,
   list, ...). This is deliberately **whole-value only** — you cannot embed
   a field inside a larger literal string (`"prefix $name suffix"` is a
   literal string containing a dollar sign, not a substitution). Partial
   in-string interpolation is one step from a templating language; whole-
   value substitution is the narrowest thing that still satisfies "field
   interpolation".
3. **A function call** — `{"fn": "<name>", "args": [...]}`, where `<name>`
   is one of the three closed-vocabulary functions below and each arg is
   itself one of these three forms (function calls nest).

### 4.1 The closed function vocabulary

| Function | Signature | Behaviour |
|---|---|---|
| `first` | `first(list)` | First element, or `null` if the list is empty. |
| `set_diff` | `set_diff(a, b)` | Elements of list `a` not present in list `b` (value-equality via `repr`, matching `field-corrections.md` §4's `add` dedupe key). |
| `date_of` | `date_of(value)` | Normalizes a date-ish string to `YYYY-MM-DD`. Accepts bare `YYYY-MM-DD` and any `datetime.fromisoformat`-parseable string (including a trailing `Z`). |

**An unknown function name fails schema validation** at rule-load time —
`athenaeum.rules._validate_no_unknown_fn` walks every value-bearing field
looking for `{"fn": ...}` nodes and rejects anything outside this table
before the rule is ever matched against a record.

A transform failure at MATCH time (a referenced field is absent, `date_of`
gets unparseable input, a function receives the wrong argument shape) is
never a crash and never a bad write: the matched record's disposition
degrades to `transform-error` in the ledger and the original raw file is
left untouched for the reasoning tiers — `field-corrections.md` §1.1's
"nothing is rejected, only fallthrough" doctrine, applied to the compiler.

---

## 5. Dispositions (this slice: `emit`, `fallthrough`)

### `emit`

Resolves the `correction:` block against the matched record into one
`field-corrections.md` §3.2 correction record, writes it as a ONE-record
correction batch into `raw/<source>/<timestamp>-<uuid8>.jsonl` — the SAME
`raw/<source>/` directory the matched file itself came from, so a human
scanning `raw/` sees the compiled correction alongside its origin — and, in
`live` mode, retires the original raw file (`git rm` after a provenance
commit, recoverable from history, never hard-deleted).

That batch is picked up by the field-correction machinery
(`field-corrections.md`, `athenaeum.corrections`) **completely unchanged** —
this engine's only interface to that machinery is writing a file in the
format it already scans for. No new discovery path, no new applier, no
allowlist/precedence/routing/delta-gate change.

### `fallthrough`

Explicitly leaves the record for the reasoning tiers. Nothing is written;
the raw file is left exactly as discovery found it. A rule with
`disposition: fallthrough` carries no `correction:` block — its only job is
to be *counted* (the ledger records that this shape was recognised and
deliberately deferred, distinguishing "we've seen this and chose not to
auto-compile it" from "nothing recognised this at all").

### The machine-tier guard

`correction.source` must be a **literal** string (never a `$field`
reference or function call) that parses to a `script:` or `api:` `SourceRef`
— the two "machine tier" precedence types
(`field-corrections.md` §6.1: `api` rank 3, `script` rank 7). **A rule
asserting any other precedence tier — including `user:` — fails schema
validation at load time.** This is what stops a rule from ever granting
itself human-tier precedence over an incumbent value; it is enforced before
any record is processed, not per-record, which is exactly why the source
must be literal.

---

## 6. Observe mode and the audit ledger

Every rule ships **`mode: observe` by default** — the required first state
for any new or edited rule. In observe mode the engine computes exactly what
it WOULD have done (which record matched, which disposition would have
fired) and **ledgers it**, while writing nothing else: no correction batch,
no raw-file retirement, no other side effect. An operator reviews the
ledger, and only then edits the rule to `mode: live`.

Every run appends one line per `(rule, mode)` pairing that had at least one
match to `wiki/_shape_rules_applied.jsonl` — append-only JSONL, same
discipline as `wiki/_corrections_applied.jsonl`
(`field-corrections.md` §5.3):

```json
{"schema_version":1,"run_at":"2026-08-15T03:00:00Z","rule":"example-contact-bounce@1","mode":"observe","records_total":42,"dispositions":{"observed-emit":40,"transform-error":2}}
```

**The line carries a denominator** (`records_total`), matching the
`field-corrections.md` §5.3 pattern: `dispositions` must sum to
`records_total`, so a streaming bug that silently drops a record is
detectable the same way it is for the correction ledger. Every line is
tagged `rule@version` (`ShapeRule.qualified_name`) — the audit key
downstream tooling (athenaeum#902/#903) keys off, and the same tag a rule's
compiled `correction.note` carries by default.

Disposition vocabulary this phase's ledger uses: `emit` / `fallthrough` (a
`live`-mode rule that actually acted), `observed-emit` /
`observed-fallthrough` (an `observe`-mode rule, or a `--dry-run` invocation,
that only computed what it would have done), and `transform-error` (an
`emit` rule matched but a value expression failed to resolve — degrades to
fallthrough).

---

## 7. Example rules — packaged, never engine defaults

`athenaeum` ships example rule files inside the wheel
(`src/athenaeum/rule_examples/*.yaml`) but **never loads them automatically**
— `athenaeum.rules.load_rules` only ever reads
`<knowledge_root>/rules/*.yaml`, and that directory does not exist on a
fresh knowledge root. To install the examples:

```
athenaeum init --with-rules
# or, on an existing knowledge root:
athenaeum init --with-rules --path ~/knowledge
```

This copies the packaged examples into `<knowledge_root>/rules/` (skipping
any file that already exists, unless `--force`), mirroring
`athenaeum init --with-templates`'s existing copy-in mechanism exactly
(`athenaeum.init.copy_example_rules`, `importlib.resources.files`). Every
packaged example ships `mode: observe` — installing them changes nothing
about what gets written until an operator reviews the ledger and edits a
copy to `mode: live`.

---

## 8. Phase ordering and volume bounds

The engine runs in the deterministic phase slot inside `athenaeum run`,
**immediately before** the field-correction phase
(`librarian._run_shape_rule_phase`, then `_run_correction_phase`) — a
compiled batch must be visible to the correction phase's own fresh scan of
`raw_root` later in the SAME run, not only starting next run. Both phases
make zero LLM calls and run before the entity tiers.

Per-run volume is bounded exactly like the field-correction phase, under its
own `librarian.shape_rules.*` config namespace — see
[`configuration.md`](configuration.md#shape-rule-engine-librarianshape_rules-athenaeum901)
for the full knob table (`max_records_per_run`, `runtime_share`).

---

## 9. Not decided here (later slices)

- **`drop` / `retain` / `rollup` dispositions, and compiled-exempt
  retirement** — athenaeum#903.
- **Automatic rule generation** — a separate slice in this batch, not
  addressed here or by athenaeum#901.
- **Any change to the correction applier, allowlist, precedence, routing, or
  delta gate** — none; the engine's only interface to that machinery is
  writing a file in the format it already scans for.
