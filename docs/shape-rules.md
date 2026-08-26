<!-- SPDX-License-Identifier: Apache-2.0 -->

# The shape-rule engine — declarative YAML rules that compile foreign shapes

**Status:** MVP. Issues athenaeum#901 (engine: `emit`, `fallthrough`),
athenaeum#903 (`drop`, `retain`, `rollup` + compiled-exempt retirement),
athenaeum#837 (`preserve` — the log-shaped intake family) and athenaeum#1132 (`preserve`
routed through a storage adapter, so its target can live outside the
knowledge git repo). All six dispositions now ship. Automatic rule generation
is a later slice, not this one.

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
| `source` | the raw file's OWNING `raw/<source>/` directory name (issue athenaeum#974: this is still true for a file discovered one level below the source directory — see §3.3) | exact string |
| `format` | the raw file's extension (`md` \| `jsonl`) | `md` \| `jsonl` |
| `filename_glob` | the raw file's filename | glob pattern |
| `key_fingerprint` | the matched RECORD's top-level key set (§3.1) | 16 lowercase hex chars |
| `fields` | individual record field values, top-level OR nested (§3.2) | `{<field>: <predicate>}` |
| `unclaimed` | whether the candidate is an audit-unclaimed file rather than an ordinary raw file (§3.4) | `true` \| `false` (default `false`) |

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

**Nested keys (issue athenaeum#974):** a `fields` key may address a value nested
below the record root with a dotted path — `"a.b"` resolves
`record["a"]["b"]`, one level or as many as the path names:

```yaml
fields:
  session.log_group: {glob: "hestia-lanes-*"}
```

Resolution order is backward-compatible by construction: an EXACT top-level
key always wins first, dots and all — a pre-athenaeum#974 rule's plain (non-dotted)
key resolves exactly as it always did, and even the rare top-level key that
itself happens to contain a literal `.` still resolves as that key, never
reinterpreted as a path. Only when the key is not itself a literal top-level
key AND contains a `.` is it walked as a nested path. A missing key at any
level (or a non-mapping value partway down the path) is "absent from the
record" — no match, exactly like a missing top-level key. See
`athenaeum.rules.resolve_field_path`.

### 3.3 Nested source subdirectories (issue athenaeum#974)

`discover_raw_files` looks one level below `raw/<source>/` in addition to
directly inside it — a source that organises its own drops into
subdirectories (e.g. `raw/hestia/hestia-lanes-974/<file>.md`) is still
discovered, without turning discovery into an unbounded recursive walk.

A file discovered this way still carries its TOP-LEVEL source directory
name as `RawFile.source` — never `<source>/<subdir>` — so `match.source`
means exactly what it always meant: "which `raw/<source>/` tree", not
"which exact directory". Combined with §3.2's nested-key `fields`, this is
what makes a rule like

```yaml
match:
  source: hestia
  fields:
    session.log_group: {glob: "hestia-lanes-*"}
```

able to reach a record living at `raw/hestia/hestia-lanes-974/<file>.md`
whose frontmatter nests `log_group` one level below the record root.

One exception: a source directory that is itself a configured
`recall.extra_intake_roots` entry (default `raw/auto-memory`) is never
descended into here — that tree already has its own dedicated discovery
function (`discover_auto_memory_files`) and frontmatter schema, so this
descent would otherwise double-discover every auto-memory file.

### 3.4 Matching audit-unclaimed files (issue athenaeum#1133)

The unrecognised-raw-intake audit (issue athenaeum#836, `athenaeum.intake_audit`)
finds files neither `discover_raw_files` nor `discover_auto_memory_files`
would ever claim — wrong extension, or a filename that misses a naming
convention — and, by default, only ever **raises a pending decision**
about them (`_pending_questions.md`). It never disposes of them. This is
the alternative path: an operator rule that opts in with `match: {unclaimed:
true, ...}` can give such files a real disposition instead of only a
notification.

```yaml
version: 1
name: drop-empty-exports
mode: observe
match:
  unclaimed: true
  source: daily-activity
  filename_glob: "*.txt"
disposition: drop
```

**Opt-in is explicit, never inferred.** A plain rule (`unclaimed` omitted
or `false`) matches only ordinary claimed candidates, exactly as before;
an `unclaimed: true` rule matches *only* audit-unclaimed candidates. A
rule can never match both kinds — this is a hard partition, not a
preference.

**What is legal against an unclaimed candidate:** `source`,
`filename_glob` — both work off the file's path, which is all that is
known about it. **What is illegal, rejected at rule LOAD time with an
actionable error:**

- `fields` — an unclaimed file has no parseable record (no frontmatter, no
  first-line JSON) to match fields against.
- `key_fingerprint` — same reason: no record, no key set to fingerprint.
- `format` — typed as `md` \| `jsonl` only, so it can never equal an
  unclaimed file's actual extension; it would silently never match.

**Dispositions.** `drop`, `retain`, and `preserve` all work exactly as
they do for an ordinary raw file — see §5. `fallthrough` is legal too (a
no-op: the file is simply left for the intake audit's usual pending-decision
path). `emit` and `rollup` are rejected at load time — both require a
`correction` block that compiles record fields, and an unclaimed candidate
has none. A `preserve` rule may still carry a `correction` referencing a
`$field`, but resolving it against an empty record degrades safely to
`transform-error` (the file is left untouched) — almost certainly a rule
author's mistake, since there is no field to reference.

**Default behaviour is unchanged.** With no `unclaimed: true` rule loaded
(or no rule at all), an audit-unclaimed candidate is evaluated, matches
nothing, and is left exactly where the intake audit would have found it —
same pending-decision flow as before this issue. Resolving that pending
decision remains available and is unaffected by this mechanism; the two
are independent ways of answering the same underlying question.

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

### 4.2 Optional decay annotations (`bucket`, `valid_until` — issue athenaeum#904)

A `correction:` block may additionally carry `bucket` (`daily` / `weekly` /
`durable`) and/or `valid_until` — the same OPTIONAL, ride-alongside shape
`usage_class` already uses (`field-corrections.md` §7.1): they apply to the
correction's TARGET entity page regardless of what `field`/`value` the
correction itself is proposing, rather than going through the
`field`/value allowlist+precedence machinery. See
`docs/provenance-shape.md` §8.8 for the full frontmatter contract these
feed.

```yaml
disposition: emit
correction:
  target: {type: person, handle: {email: "$email"}}
  op: set
  field: bounced
  value: "$status_date"
  source: "script:delivery-monitor"
  observed_at: "$observed_at"
  bucket: daily              # optional, one of daily | weekly | durable
  valid_until: "$expires_at" # optional SUGGESTION -- never overrides an
                              # explicit valid_until already on the page
```

`bucket` is a plain **literal**, not a `$field`/function expression — unlike
`value`/`observed_at`/`note`, it is validated against the closed enum at
RULE-LOAD time (before any record is ever processed), because a rule's
decay classification is a rule-authoring decision ("records this rule
matches are daily status"), not something computed per record. `valid_until`
may be `$field`-interpolated like `value`. Both are omitted from the emitted
correction record entirely when the rule does not set them — a rule
authored before athenaeum#904 existed emits a byte-identical record.

---

## 5. Dispositions (`emit`, `fallthrough`, `drop`, `retain`, `rollup`, `preserve`)

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

### `drop` (athenaeum#903)

An **audited discard** of an information-free record — the 91% of daily
contact-sync volume whose payload is a `skip_*` no-op, where the producer
itself concluded nothing happened.

`drop` carries no `correction:` block. The raw file is retired through the
same two-commit convention `emit` uses: the content is **committed first**,
then `git rm`'d, so the discard stays **recoverable from history**. This is
the difference between an audited discard and a deletion, and it is the
whole reason `drop` is a disposition rather than an `unlink`. The ledger's
per-rule counter says how many were discarded and by which rule.

### `retain` (athenaeum#903)

The file is a **long-lived source document** — a daily journal, an operator's
running log — not intake to be compiled into prose.

`retain` carries no `correction:` block. The file is **not deleted** and
**not compiled**: it is marked *compiled-exempt*, and discovery skips it on
every subsequent run. That removes a standing source of wasted budget (a
source document otherwise rediscovered and re-considered every run), without
the `ephemeral: true` alternative's cost of dropping the content entirely.

The exempt manifest is `compiled-exempt.json` **under the knowledge root**,
keyed by `source/filename` — deliberately in the knowledge git repo rather
than the cache dir. The two records fail differently: losing a cache entry
costs a re-read, whereas losing an exemption would silently resurrect a
preserved source document into the wiki one cache wipe after the operator
asked for the opposite. "Permanently skips" cannot rest on a cache.

### `preserve` (athenaeum#837)

The file is a **log** — a source artifact to be kept whole, not intake, and
not a claim. `preserve` **moves** it out of raw intake into an
operator-configured preserved area, and (optionally) compiles a fact from it
that points *back* at the moved file as its provenance.

This is the operator decision of 2026-08-14 on athenaeum#837:

> *"These logs are like a daily diary. We don't need the blow-by-blow into the
> wiki. We need to retain the log as an artifact and point any facts that we do
> ingest to that log as the source."*

So the answer to a log-shaped family is **not** "cluster it better" — a log is
a source document. It is kept whole, kept out of the wiki, and referenced by
whatever the librarian legitimately learns from it.

**Configure the area first — the feature is opt-in twice over.** A `preserve`
rule is inert until an operator routes it somewhere. There are two ways to do
that; a given deployment picks one:

```yaml
# <knowledge_root>/athenaeum.yaml — local, in-repo area (the original form)
librarian:
  preserved_log_dir: logs
```

```yaml
# <knowledge_root>/athenaeum.yaml — routed through a storage adapter (athenaeum#1132),
# which may point outside the knowledge git repo entirely
librarian:
  preserved_log_adapter: mural-archive
storage:
  adapters:
    mural-archive:
      backing_store: filesystem
      surface_root: /var/lib/athenaeum-archive   # absolute -- outside the repo
```

**Where an artifact actually lands is a routing decision, not a property of
`preserve` itself.** `librarian.preserved_log_dir` names a folder *under the
knowledge root* — relative, versioned by the same git repo as everything
else, and unchanged since athenaeum#837. `librarian.preserved_log_adapter` names a
registered `storage.adapters.<name>` (the same seam
[`storage-adapter-contract.md`](storage-adapter-contract.md) and
[`whole-store-adapter-design.md`](whole-store-adapter-design.md) document) and
routes through it instead — its `surface_root` may be absolute, so the
artifact can live on a different filesystem or mount than the knowledge repo,
which is what makes a large corpus (hundreds of megabytes of exported board
JSON, for example) preservable without committing it into a repo whose value
is being small and diffable. Precedence when both are set: **the adapter
wins**, and a warning names the shadowed directory — never a silent pick.
Unconfigured (neither key set), a matching record is tallied
`preserve-unconfigured` and falls through to the reasoning tiers with the raw
file untouched — never a silent move to a guessed location. A
`preserved_log_dir` value that is absolute, or that escapes the knowledge
root via `..`, is refused with a warning — that key's contract is "a
directory under the knowledge root"; an operator who wants to land outside it
uses `preserved_log_adapter` instead. A `preserved_log_adapter` value naming
an adapter that is not registered raises loudly — never a silent fallback to
the directory.

**Fail-closed ordering, and why EXDEV is the expected case, not an edge
case.** The adapter-routed path never moves the raw file directly: it reads
the source bytes, writes them to the adapter's surface first (an exclusive
create — a same-named destination refuses rather than clobbering), and only
removes the source once that write has actually succeeded. A routed adapter
is routinely on a different filesystem than `raw/` — the mural corpus that
motivated athenaeum#1132 is exactly that case — so a cross-device write failure is
caught the same way any other write failure is: the raw file is left exactly
where it was, and the record is tallied `preserve-failed`, same as a failed
move on the local-directory path.

**Why a move, and not `retain`.** `retain` (above) marks a file exempt *where
it lies*, which is the weaker guarantee: the file stays in the intake tree, so
every future mechanism that walks `raw/` must remember to consult the exempt
manifest — and that manifest fails open by design. Moving the file makes the
guarantee structural: a preserved log is not *skipped by* discovery, it is
**not discoverable**, because `intake.discover_raw_files` only ever walks
`raw/`.

For the same reason `preserve` does **not** write an exempt row. The exempt key
is `source/filename`, so exempting it would suppress a future, genuinely-new
file that happened to reuse the name — which is exactly what a daily log writer
emitting `today.jsonl` every day does. The move is the mechanism; the manifest
is not involved.

Layout under the area mirrors intake's own — `<preserved_dir>/<source>/<filename>`
— so a log's origin survives the move, and a same-named file from a later run is
suffixed (`today-1.jsonl`) rather than clobbered. Preservation that overwrites
is not preservation.

**The optional `correction:` block — provenance, not a second source.**
`preserve` is the one disposition where `correction:` is optional. Without it,
the log is simply moved. With it, the fact is compiled *and* its `source` is
rewritten to point at the preserved artifact:

```yaml
disposition: preserve
correction:
  target: {type: person, handle: {email: "$email"}}
  op: set
  field: bounced
  value: "$status_date"
  source: "script:delivery-log"      # machine tier, validated at load
```

compiles a bounce fact whose source becomes the **structured** form:

```json
{"type": "script",
 "ref": "preserved-log:logs/delivery/20260815T000000Z-aa.jsonl#L1",
 "notes": "compiled by shape rule delivery-log@1 from a preserved log (asserted as delivery-log)"}
```

The `type` is deliberately preserved rather than replaced. An unknown source
type silently falls to the rank-9 default (`precedence.source_rank`), so
overwriting the whole scalar with `preserved-log:…` would quietly demote every
fact a log produces below the machine tier the load-time guard exists to
enforce. Keeping `type` and putting the pointer in `ref` — which is what `ref`
is for — preserves the rank *and* resolves to the artifact.

The locator after `#` is honest about what the extractor matched (§3.1): a raw
file yields exactly one record, so the path plus that record's position locates
it completely — `L1` for a `.jsonl`, `frontmatter` for a `.md`. When the
extractor grows to multi-record files, that field carries the record index.

**The pointer's scheme never varies by where the artifact was routed
(athenaeum#1132) — only the path segment does.** `preserved-log:` names the
provenance *kind*, which is the same fact regardless of backend; a
`preserved_log_adapter`-routed artifact still produces a `preserved-log:`
pointer, not a second scheme. What changes is the path segment: when the
destination resolves under the knowledge root (`preserved_log_dir`, or an
adapter whose `surface_root` happens to be relative) it is the same
knowledge-root-relative form as before; when it does not (an out-of-repo
adapter surface) it is an absolute POSIX path instead — the leading `/`
disambiguates the two forms with no new field needed.

**Ordering.** The correction is built *before* the move, so a transform that
cannot resolve leaves the raw file exactly where it was (tallied
`transform-error`) rather than stranding a half-moved log. A move that fails
tallies `preserve-failed` and writes no fact.

**Indexing is decided explicitly, not by accident.** The preserved area is
**not** embedded into the recall corpus by this change. The corpus is built
from `wiki_root` (plus explicitly-named extra roots); the preserved area lives
under the knowledge root and outside it, so nothing indexes it as prose. A
preserved log is reachable as *provenance* — via the pointer on the facts it
produced — not as retrievable prose. Whether to index preserved logs directly
is a separate, deliberate decision, not a side effect of where the files landed.

### `rollup` (athenaeum#903)

N matching records aggregate into **one** correction record, following the
event-stream pattern in [`field-corrections.md`](field-corrections.md) §12.
That section is explicit about what may cross from an event stream into an
entity record — *"a small rollup — last-event date, a windowed count"* — and
the `rollup:` block is that sentence as a closed vocabulary:

```yaml
disposition: rollup
rollup:
  group_by: "$person_uid"   # records with equal keys collapse into one correction
  aggregate: count          # count -> group size; last -> max of `of`
  # of: "$ts"               # required by `last`, forbidden by `count`
correction:
  target: {uid: "$person_uid"}
  op: set
  field: interaction_count
  value: 0                  # REPLACED by the computed aggregate
  source: "script:event-stream"
```

The group's correction is built from the `correction:` block against the
group's first record (for `target`/`field`/`source`), with `value` replaced
by the aggregate. There is deliberately **no** substitution token like
`$$rollup`: a substitution token is a templating language in miniature, and
athenaeum#901's "no templating language" guarantee is worth keeping literally
true. Every member of a written group is retired exactly as `emit` retires a
compiled file.

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

Disposition vocabulary this phase's ledger uses: `emit` / `fallthrough` /
`drop` / `retain` / `rollup` (a `live`-mode rule that actually acted), the
matching `observed-*` forms (an `observe`-mode rule, or a `--dry-run`
invocation, that only computed what it would have done), and
`transform-error` (an `emit`/`rollup` rule matched but a value expression
failed to resolve — degrades to fallthrough, raw file untouched).

**The denominator invariant (athenaeum#903).** Alongside `records_total`, each
line carries `records_seen` — the count of records that rule actually
matched, tracked **independently** of the per-disposition tallies. The two
must be equal: dispositions sum to records seen, for every rule, in every
run. Counting them separately is the point — if both came off the same
increment the invariant would be a tautology and could never catch a
disposition that forgot to tally. A violation is logged at ERROR with both
figures.

### 6.1 Per-record disposition rows (athenaeum#975)

`_shape_rules_applied.jsonl` above is a per-`(rule, mode)` **aggregate**: it
answers "how often did this rule fire", not "which record got what
treatment". `wiki/_shape_rule_dispositions.jsonl` is the per-record
complement — same `_`-prefixed, wiki-root, append-only-JSONL discipline
(:func:`athenaeum.rules.append_shape_rule_disposition_row`,
:func:`athenaeum.rules.default_shape_rule_dispositions_path`), so it can
never become a claim or enter the embedded index. Every candidate the phase
evaluates gets exactly one row, **including the ones no rule matched**
(`rule_id: null`, `disposition: "no-match"`) — those are the shapes a
frequency detector (athenaeum#905) actually needs to see:

```json
{"schema_version":1,"at":"2026-08-15T03:00:00Z","source":"delivery-monitor","source_ref":"delivery-monitor/20260815T030000Z-9f3ac1d2.jsonl","key_fingerprint":"a5149e5b057b68f7","tier":0,"rule_id":"example-contact-bounce@1","disposition":"emit"}
```

`key_fingerprint` is the same top-level-key-set fingerprint
(`record_key_fingerprint`) the match spec uses — never a raw value.
`source`/`source_ref` come from the raw file's own `source` (the raw source
directory, what a frequency query groups by) and `ref` (`source/filename`),
both already non-sensitive.

**`tier`** encodes whether the shape-rules pass — the deterministic, no-LLM
layer, tier 0 on the ladder in `field-corrections.md` §2 — actually disposed
of the record:

| `tier` | When |
|---|---|
| `0` | `emit` / `drop` / `retain` / `preserve` / `rollup`, and their `observed-*` forms — this pass resolved it. |
| `null` | `no-match` (no rule matched at all), `fallthrough` / `observed-fallthrough`, or a soft failure (`transform-error`, `preserve-unconfigured`, `preserve-failed`) — deferred to the reasoning ladder (tier ≥1), which this pass cannot know in advance. `null` is deliberate: a guessed tier number would be a lie. |

Rows are written unless `dry_run` is set, mirroring the aggregate ledger's
own dry-run behaviour. **Forward-only:** this ledger starts accumulating
from the run it first ships in — no backfill of historical intake is
attempted.

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

- **Automatic rule generation** — see §10 below (issue athenaeum#905): the
  librarian PROPOSES a rule for a human to approve; nothing here or in
  athenaeum#901/athenaeum#903 auto-writes a rule.
- **A configured preserved-log AREA, and moving a retained file into it** —
  athenaeum#837. `retain` (above) marks a file compiled-exempt *in place*;
  relocating it under an operator-configured preserved area, and carrying a
  source pointer back to it from any fact ingested out of it, is that issue's
  remaining scope.
- **Any change to the correction applier, allowlist, precedence, routing, or
  delta gate** — none; the engine's only interface to that machinery is
  writing a file in the format it already scans for.

---

## 10. Rule proposals — the librarian proposes, a human approves (athenaeum#905)

`athenaeum.rule_proposals` closes the loop §6.1's per-record disposition
ledger opened: it detects when the reasoning tiers keep re-deriving the same
conclusion for one record shape, drafts a candidate rule from real
exemplars, and puts it in front of an operator — never activating anything
by itself.

**The detector (AC1) counts rows whose `tier` is `null`** in
`_shape_rule_dispositions.jsonl`, grouped by `(source, key_fingerprint)`,
over a configurable window (`librarian.rule_proposals.window_days`, default
30). This is a deliberate **respecification** of the issue text: AC1 as
filed said "restricted to those handled at tier 2 or tier 3", written
against an "intake ledger" that did not exist when the issue was drafted.
The ledger the operator chose to build instead (§6.1, issue athenaeum#975) only
ever knows `tier: 0` (the deterministic shape-rules pass resolved it) or
`tier: null` (it did not — deferred to the reasoning ladder, tier >=1,
which this pass cannot know in advance). `tier is null` — "the shape-rules
pass could not handle this" — is the faithful reading of the issue's own
Motivation ("the reasoning tiers stop re-deriving the same conclusion for
the fiftieth instance of a shape"); no code anywhere encodes a literal tier
2 or 3. "The reasoning tiers" here means the **intake** ladder
(`docs/field-corrections.md` §2: tier0 structured -> tier1 programmatic ->
tier2 classify -> tier3 merge -> tier4 human) — the ladder §6.1's ledger is
built against — not the unrelated T1/T2 numbering in `reasoning_tiers.py`.

**Crossing the threshold** (`librarian.rule_proposals.threshold`, default
50) selects up to `librarian.rule_proposals.exemplar_count` (K, default 5)
READABLE raw records for that shape — a deferred row's raw file may since
have been compiled and retired, so unreadable rows are skipped, most-recent
first; if zero exemplars are readable, no proposal is drafted this run (it
is retried on a later run against fresh disposition rows). One drafting call
is then made per shape, per AC2's "one language-model call over K exemplars
plus their existing tier-3 outputs".

**The tier-3-output join does not exist.** `_reasoning_tier_decisions.jsonl`
(`reasoning_tiers.py`) is keyed by `proposal_id` — a MERGE proposal id — and
carries no `source`/`source_ref` field a raw intake record could join
against. `athenaeum.rule_proposals._tier3_outputs_for_exemplars` is the real
join attempt, not a stub: it always returns `{}` against today's ledger
schema, and the drafting call is told explicitly, in the prompt AND in the
persisted proposal's `tier3_linked`/`tier3_note` fields, that tier-3 outputs
were not linkable — it drafts from the exemplar records alone rather than
inventing a linkage.

**The draft (AC3)** is a JSON response (disposition + an optional
`correction` block, matching every disposition except `rollup` — see the
module docstring for why rollup is excluded from drafting) that code
assembles into a full `ShapeRule` (the `match` block — `source` +
`key_fingerprint` — is fixed by the detector, never left to the model) and
re-validates via `ShapeRule.model_validate` before it is ever persisted. A
draft that fails validation is skipped (logged), never stored. Every
exemplar record embedded in the prompt is fenced via
`athenaeum.prompt_safety.fence_untrusted` (AC7) — raw intake is untrusted
input (`docs/field-corrections.md` §12a).

**The proposal surfaces via `list_pending_decisions`** (AC4) as a
`type: "proposed-rule"` item (`athenaeum.decisions.proposed_rule_to_decision`),
owner-only (same withholding as `retraction`/`audit`/`quarantine` — see
`athenaeum.decisions.list_pending_decisions`'s audience-scoping docstring).

**Approve** (AC5, `athenaeum.rule_proposals.approve_rule_proposal`) writes
the rule into `<knowledge_root>/rules/` with `mode` forced to `"observe"` —
independent of, and re-validated after, whatever the drafting call
produced — never in a live-writing state. **Reject** (AC6,
`reject_rule_proposal`) records the rejection; because a proposal's id is
derived from its shape alone (`proposal_item_id(source, key_fingerprint)`),
a rejected shape is permanently suppressed — the next detection run skips it
without spending another drafting call.

**Persistence**: one ledger, `wiki/_rule_proposals.jsonl`, with `proposal` /
`approve` / `reject` record kinds — same shape as `wiki/_quarantine.jsonl`
(§ athenaeum#898).

**Wired into the nightly `athenaeum run` loop (issue athenaeum#1063), OFF by
default.** `athenaeum.rule_proposals.run_rule_proposal_detection` is now
called from `librarian.py`'s `_run_rule_proposal_phase`, run immediately
before the finalize phase (after the auto-memory block, so this run's own
newly-deferred disposition rows are already visible to the detector). The
call site is config-gated OFF by default —
**set `librarian.rule_proposals.enabled: true` in `athenaeum.yaml` (or the
env var `ATHENAEUM_RULE_PROPOSALS_ENABLED=1`) to turn it on.** Left off, the
phase is a complete no-op: no client is built, the disposition ledger is
never read, and no LLM call — hence no new spend — is ever made. Default OFF
is deliberate: this wiring adds a NEW unattended language-model call to the
nightly run, real recurring spend an operator must opt into rather than
discover behind a detector issue.

Once enabled, cadence is governed entirely by the detector's own
`librarian.rule_proposals.threshold` (default 50) plus its built-in
per-shape idempotency (a shape already carrying a pending or rejected
proposal is never re-drafted) — there is no separate once-per-period stamp,
mirroring `run_shape_rule_phase`, which has no such guard beyond its own
runtime share. The phase participates in the run's wall-clock deadline
(`ctx.run_deadline`, issue athenaeum#396) directly — skipped entirely if
already expired, and re-checked before each shape's drafting call — and
each drafting call's tokens are recorded into the run's spend ledger tagged
`knob="rule_proposals"`, the same accounting the tier-2/3 call sites use
(see `librarian._run_rule_proposal_phase`'s docstring for the full
rationale, including why it does NOT carve out its own runtime share the
way `_run_shape_rule_phase` does).
