# Field corrections

**Reference page.** The full design record is
[field corrections](../design/field-corrections.md); this page is the operational summary.

## What it does

A **correction** is a structured, single-attribute write submitted by a named adapter —
`apollo-enrich`, `gmail-backfill`, a calendar sync — against one wiki entity. Corrections
exist so a mechanical data source can maintain a scalar or list attribute (a job title, a
message count, a LinkedIn URL) without going through the LLM compile path and without ever
becoming a wiki writer itself.

Corrections are batched. Each record names a target, an op, a field, a value, an
`observed_at`, and a source.

## What it reads

- `librarian.corrections.fields` — **the armed-attribute allowlist**. The single surface
  that controls whether *any* adapter may write *any* attribute. Empty by default: a fresh
  install accepts no corrections at all until the operator arms attributes explicitly.
- `storage.mapping` and `storage.excluded_fields` — **where a correction lands**. A
  correction is routed by the same storage settings as every other writer: the page's
  class (a `person` page is class `pii`) must map to a location outside the corpus, and
  the field must be on that class's `excluded_fields` list (the built-in contact fields
  for `pii`, unless you set an explicit list, which replaces them). A field on that list
  is written to the location's record, and otherwise in place on the page. The old
  `librarian.corrections.sensitive_fields` key was removed: a configuration that still
  sets it is refused before any batch is read, with the complete replacement printed.
- `librarian.corrections.schema_slots` — the schema-evolution table, for armed attributes
  that need an explicit slot before they may be written.
- The batch limits: `max_records_per_batch` (5,000), `max_records_per_run` (50,000),
  `max_batch_bytes` (32 MiB), `max_escalations_per_run` (50).

### The armed-attribute vocabulary

Each entry in `librarian.corrections.fields` is keyed by attribute name and takes three
keys:

```yaml
librarian:
  corrections:
    fields:
      current_title:
        shape: scalar            # scalar | list
        writers: [apollo-enrich] # which submitters may write it
        monotone: false          # if true, the value may only move forward
      employment_history:
        shape: list
        writers: [apollo-enrich]
        monotone: false
```

- **`shape`** decides which ops are legal. `scalar` accepts only `set`; `list` accepts only
  `add` and `remove`.
- **`writers`** is a per-attribute allowlist of submitter names. Two adapters may share an
  attribute by both appearing in the list.
- **`monotone`** marks an attribute whose value may only advance.

**The vocabulary is deployment-specific.** These names are not shipped defaults — they are
whatever the operator armed in `athenaeum.yaml`. An adapter author writing against an
existing deployment must **read that deployment's `librarian.corrections.fields` before
inventing an attribute name**, because a near-synonym of an already-armed attribute
(`title` where `current_title` is armed) is not a new field — it is a rejected write.

## What it writes

- The named attribute on the target entity's frontmatter, when every gate below passes.
- An entry in the corrections ledger for **every** record, applied or not.
- A handoff file under `raw/` for each record it refuses, so the refusal is durable and
  triageable rather than lost.

### An escalated correction to a vault field

When a correction is escalated to the decision queue and its field is kept in a vault,
the question block does not carry the value. Its `Value:` line reads `vault:<ref>`, the
`Correction ID:` line is the same `<ref>`, and the `Target:` line names the entity by uid
even if the correction named it by email address. `<ref>` is random; nothing about the
value, the field or the target is used to make it.

The proposed value, with the field, the op, the source and the observation time, is kept
on the target's vault record, in the `pending_corrections` section under `<ref>`, and is
written before the block. Listings of pending questions show the value only to a caller
the entity read would give it to and a redaction marker naming the field to any other
caller. Answering the question reads the value back through the entity read and applies
exactly that value; if it cannot be read the answer is held and nothing is written. Once
the question is ratified or rejected the entry is removed from the vault record. A
correction to a field no rule sends to a vault produces the same block as before, and
blocks written earlier still parse and apply.

## What it refuses

Every refusal produces `disposition: raised-tier` with a reason. **A refused correction is
not silently dropped** — it is ledgered and handed off to the decision queue. The write
simply does not land.

| Reason | Trigger |
|---|---|
| `record is not a valid correction record` | Malformed record. |
| `unknown key(s) on correction record` | Any key outside the accepted set. |
| `missing or malformed target` / `missing or invalid op` / `missing or invalid field` / `missing value` / `missing observed_at` | A required field is absent or the wrong type. |
| `missing source` / `unparseable source` | The source *is* the authorization to write, so an unparseable one escalates rather than falling back to the weakest precedence rank. |
| **`attribute not on the allowlist`** | The attribute has no `librarian.corrections.fields` entry. This is the most common adapter-author failure. |
| **`writer '<x>' not permitted for field '<y>'`** | The attribute is armed, but this submitter is not in its `writers` list. |
| `op '<x>' invalid for scalar attribute` | Anything but `set` on a scalar. |
| `op '<x>' invalid for list attribute` | Anything but `add`/`remove` on a list. |
| `unrecognized shape` | `shape` is neither `scalar` nor `list`. |
| `invalid usage_class` | Not one of the known usage classes. |
| `usage_class is only valid for an add correction on a contact-identifier field routed to an excluded surface` | A usage class declared where it has no meaning. |
| `invalid bucket` | A malformed decay annotation. |
| `target resolves to zero or several entities` | The target is ambiguous or absent. |
| `target page unreadable` / `target page has no frontmatter` | The entity page cannot be parsed. |
| `uid collision constructing new entity` | A generated uid already exists. |

Validation happens **before any side effect**, so a malformed record never partially
applies.

Beyond per-record refusal, the module also stops early on the batch caps above: a run that
exceeds `max_escalations_per_run` carries its batch over rather than continuing.

## See also

- Guides — [Daily operation](../guides/daily-operation.md) · [Answering decisions](../guides/decisions.md)
- Modules — [intake](intake.md) · [conflicts](conflicts.md) · [shape](shape.md) · [sensitivity](sensitivity.md)
- Design — [field corrections](../design/field-corrections.md) · [conflict resolution](../design/conflict-resolution.md) · [provenance shape](../design/provenance-shape.md)
- Extending — [adapter contract](../extending/adapter-contract.md) · [source handles](../extending/source-handles.md)
- Reference — [configuration](../reference/configuration.md)
