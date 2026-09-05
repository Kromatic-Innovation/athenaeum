# Shape validation

**Reference page.** The full design records are
[shape-rule engine](../design/shape-rules.md) and
[provenance shape](../design/provenance-shape.md); this page is the operational summary.

## What it does

Before a claim is admitted anywhere in athenaeum — raw intake, a wiki page's
frontmatter, an MCP `remember` call, a correction batch — it must pass three
independent shape gates:

- **Frontmatter shape** (`athenaeum.schemas.WikiBase` and its type-specific
  subclasses in `src/athenaeum/schemas.py`) — required identity fields
  (`uid`, `type`, `name`), field-level coercions, and the provenance fields
  below. `extra="allow"`: custom-namespace fields round-trip untouched, but
  the fields this module names are validated.
- **Provenance shape** (`src/athenaeum/provenance.py`) — the `source` and
  `field_sources` frontmatter keys, which attribute a claim (or one field of
  it) to a `<type>:<ref>` source reference.
- **Claim-kind classification** (`src/athenaeum/claim_kind.py`) — a cheap,
  fail-open LLM tag (`claim_kind:`) recording the EPISTEMIC shape of a raw
  memory — `fact`, `observation`, `opinion`, `decision`, `policy`, or
  `definition` — so the resolver can refuse to arbitrate an `opinion` pair by
  source precedence.

A fourth, higher-volume gate — the declarative **shape-rule engine**
(`src/athenaeum/rules.py`, `<knowledge_root>/rules/*.yaml`) — compiles
recognizable raw-intake shapes (a delivery-status monitor row, a CRM export
line) straight into a correction, a drop, or an escalation without an LLM
call. Its rules are schema-validated at load time by the same discipline as
the frontmatter/provenance gates above.

## What it reads

- `<knowledge_root>/rules/*.yaml` — the shape-rule engine's rule files, one
  `ShapeRule` per YAML document, loaded by `athenaeum.rules.load_rules`.
- The `source` / `field_sources` values already on a wiki page's
  frontmatter, when validating an update against the existing shape.
- `models.classify` (env `ATHENAEUM_CLASSIFY_MODEL` > yaml > code default) —
  the model knob `claim_kind.classify_claim_kind` calls through
  `athenaeum.config.resolve_model`.
- The raw memory's own frontmatter, to short-circuit re-classification: a
  file that already carries a valid `claim_kind:` is never re-classified
  (`athenaeum.claim_kind.stamp_claim_kind` is idempotent).

## What it writes

- `claim_kind:` — stamped once into a raw file's frontmatter by
  `stamp_claim_kind`, via the same atomic-write path (`atomic_write_text`)
  every other frontmatter mutation uses.
- `source:` / `field_sources:` — written verbatim as validated. Validation
  never normalizes: `validate_source_value` and `validate_field_sources`
  return the original shape unchanged on success so a scalar `source` stays
  a scalar and a structured one stays structured on disk.
- A shape rule with `disposition: emit` or `disposition: rollup` writes a
  correction record, applied through the exact same corrections machinery
  (`athenaeum.rules.build_correction_record`) a hand-authored adapter batch
  goes through — see [Field corrections](corrections.md).
- A shape rule with `disposition: drop` deletes the source file (recoverable
  from history only); `disposition: retain` and `disposition: preserve`
  leave or relocate the file without compiling it; `disposition:
  fallthrough` writes nothing and defers to the normal tiered pipeline.
- `wiki/_shape_rule_dispositions.jsonl` — a per-record disposition row for
  every raw file the engine considered, whether or not a rule matched.

## What it refuses

| Reason | Trigger |
|---|---|
| `source scalar must be typed '<type>:<ref>' (e.g. 'script:extended-tier-build'); legacy bare-slug form retired...` | A bare-slug `source:` value — the legacy unqualified form was retired once the live tree was migrated. |
| `source scalar must be non-empty and trimmed, got ...` | A `source` string that is empty or carries leading/trailing whitespace. |
| `source ref must not have leading/trailing whitespace, got ...` | The `ref` half of a `<type>:<ref>` scalar is not trimmed. |
| `source type must match [a-z][a-z0-9_-]*, got ...` | `SourceRef.type` fails its regex — e.g. an uppercase or space-containing type. |
| `confidence must be in [0, 1]` | A structured `SourceRef.confidence` outside its valid range. |
| `field_sources must be a dict, got ...` | The whole `field_sources` value is not a mapping. |
| `per-value field_sources[<i>] missing 'value' key` / `missing 'source' key` | A per-value list entry (list-field attribution) is missing one of its two required keys. |
| `per-value field_sources[<i>] has unknown keys: ...` | A per-value entry carries a key besides `value`/`source`. |
| `MCP remember(sources=...) bare-dict shape removed. Use {"_source": ...} for wiki-level, ...` | A `remember` call passes a bare `{type, ref}`-shaped dict instead of an explicit `_source` / `_field_sources` wrapper key. |
| `_asserter must be a dict, got ...` | The `_asserter` provenance extra on a `remember` call is present but not a mapping. |
| `unknown transform function '<fn>' at <path> -- must be one of ...` | A shape rule's `correction:` template calls a transform function outside the closed, enumerated vocabulary. |
| `field predicate must set exactly one of exact/glob/in, got <n>` | A shape rule's `match.fields` predicate sets zero or more than one of its mutually exclusive matchers. |
| `key_fingerprint must be 16 lowercase hex chars, got ...` | A malformed `match.key_fingerprint` in a shape rule. |
| `match.unclaimed rules cannot use match.fields (no record exists to match against)...` | An `unclaimed`-mode rule (matches a filename before any record is parsed) also declares field-level matchers, which have nothing to run against. |
| `disposition '<x>' requires a 'correction' block` | `emit` or `rollup` declared without the `correction:` block they require. |
| `disposition '<x>' must not carry a 'correction' block` | `fallthrough`, `drop`, `retain`, or `preserve` declared alongside a `correction:` block they must not carry. |
| `disposition 'rollup' requires a 'rollup' block` | `rollup` declared without its aggregation config. |
| `match.unclaimed rules cannot use disposition '<x>' (it requires a 'correction' block, which compiles record fields that do not exist...)` | An `unclaimed`-mode rule declares `emit`/`rollup`, which need record fields an unclaimed candidate never has. |
| `correction.target keys ... must be exactly one of {uid}, {type,name}, {type,handle}` | A shape rule's emitted correction targets an entity with a malformed target shape. |
| `correction.source ... asserts precedence above machine tier -- only {...} source types are permitted` | A shape rule tries to emit a correction whose `source` claims a higher-than-machine precedence tier. |

`claim_kind` classification is **fail-open** rather than refusal-based: no
client, an API error, malformed JSON, or a label outside `CLAIM_KINDS`
(`fact`, `observation`, `opinion`, `decision`, `policy`, `definition`) all
resolve to `""` (unclassified) — the claim is admitted exactly as it would
have been before classification existed. `athenaeum.models.parse_claim_kind`
applies the same fail-open rule on read: an out-of-vocabulary value on disk
is logged and treated as unclassified rather than raising.

A malformed shape rule is never fatal to a run: `load_rules` skips it with a
loud `RuleLoadError` rather than raising, so one bad YAML file cannot stop
the pipeline from processing every other rule and every raw file that no
rule claims.

## See also

- Guides — [Daily operation](../guides/daily-operation.md) · [Sidecar](../guides/sidecar.md)
- Modules — [corrections](corrections.md) · [conflicts](conflicts.md) · [intake](intake.md)
- Design — [shape-rule engine](../design/shape-rules.md) · [provenance shape](../design/provenance-shape.md) · [field corrections](../design/field-corrections.md) · [conflict resolution](../design/conflict-resolution.md)
- Reference — [configuration](../reference/configuration.md)
