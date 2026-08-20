<!-- SPDX-License-Identifier: Apache-2.0 -->

# Sensitivity-value routing — the standing filter at raw-sweep intake

**Status:** DESIGN NOTE, unreviewed. Issue athenaeum#949. Not implemented —
this note is the deliverable; implementation is deliberately deferred to the
follow-on slices listed in §10, filed against this note. A working spike
exists off the delivery path at branch `prototype/949-sensitivity-routing-spike`
and is cited throughout as verification evidence for specific claims — it is
evidence, not a decision, and none of its choices should be read as settled
by virtue of having been built.

Companion to [`docs/sensitivity-class-vocabulary.md`](sensitivity-class-vocabulary.md)
(athenaeum#910 — the class vocabulary this design routes, unchanged),
[`docs/field-corrections.md`](field-corrections.md) §7.1 (the sensitivity
routing language this design must compose with),
[`docs/storage-adapter-contract.md`](storage-adapter-contract.md) (the
`storage.mapping`/excluded-surface seam this design reuses), and
[`docs/security-posture.md`](security-posture.md) (the `access:`/`audience:`
read-policy vocabulary and the excluded-surface read path this design's
pointer must eventually join).

---

## 0. The gap this closes

Measured against `origin/develop` at filing time (issue body, amended after
a Vitruvius Verify pass 2026-08-16): nothing in the raw sweep screens for
sensitivity classes. `screen_intake` (athenaeum#320) runs at `remember()`
write time, not the sweep, covers `medical` only, and only labels
(`access:` stamp) — it never routes. `storage_migrate` is an operator-invoked
one-time CLI (athenaeum#437). Inline findings on the live corpus went
173 → 150 → 209 across three dated measurements with no migration run in
between: intake adds sensitive values continuously; only a human running a
CLI removes them. This design is what turns athenaeum#437's migration into a
one-time event instead of a recurring chore — see §9.

**Placement — decided on the issue, not reopened here.** At intake, during
the librarian's raw sweep, at the compile boundary — before a raw value can
be written into `wiki/` or embedded in a Tier-2/3 prompt sent to the model
provider. The egress argument is the load-bearing one: a post-hoc sweep over
the already-written wiki cannot prevent a value having already been sent to
an LLM provider, because by the time such a sweep runs the value has already
left the host. Only compile-boundary screening closes that. **Verified in
the spike:** a single hook at the top of `librarian.process_one` — before
`tier0_passthrough`, `tier1_programmatic_match`, and both LLM exposures
(Tier 2's classify prompt, Tier 3's `raw.content[:2000]` fallback
observation) — sits upstream of every one of the four tiers, because all
four read the same in-memory `RawFile` object from one dispatch point
(`librarian.py`'s entity-tier loop calls `process_one` once per file; no
tier re-reads raw independently). This confirms one hook location is
structurally sufficient for AC6 — see §6.

---

## 1. AC1 — where the filter runs, the pointer format, raw retention, and composition with athenaeum#910 / field-corrections.md §7.1

**Where it runs.** A single hook at the top of `librarian.process_one`,
before Tier 0's passthrough write and before Tier 1/2/3 read `raw.content`
at all. Scoped to the raw file's **body only**, never its frontmatter
block — see §6 for why splicing a pointer into YAML is unsafe and how the
spike avoids it (parse frontmatter/body once, screen only the body, splice
the redacted body back onto the untouched frontmatter preamble).

**The pointer format (proposed).** A literal string substituted for the
matched span, carrying the class name and a resolvable identifier, naming
the read path rather than only a destination:

```
[sensitive:<class>:<record_id> — value withheld; resolve via
athenaeum.sensitivity_routing.resolve_sensitive_record()]
```

This satisfies the issue's explicit complaint about today's
`INLINE_REDACTION_MARKER` (`storage_migrate.py`) — a single byte-identical
string for every redaction on a page, naming a destination but giving the
reader nothing to act on. The proposed pointer differs per matched value
(distinct `record_id`, §2) and names the function a reader (an agent) calls
to re-request it, closing the operator's stated requirement ("if the agent
doesn't use `with_pii=True` then it should get the redacted block and know
it can make the request again"). The exact literal wording is a reversible,
non-security choice (§9) and is not load-bearing to any of this note's
harder calls.

**Raw retention — see §5 (AC4) for the full argument.** Summary: retention
in `raw/` is accepted, unchanged, and not addressed by this design.

**Composition with athenaeum#910.** This design consumes
`athenaeum.sensitivity.classify(text=..., frontmatter=..., config=...)`
as-is — the recogniser registry, the class vocabulary, and the read-policy
inheritance athenaeum#910 shipped (S1a/S1b, athenaeum#989/#990) are reused
without modification. This design adds nothing to that module; it is a new
**consumer** of `classify()` — the first one, per `sensitivity.py`'s own
docstring ("No production module imports this one yet"). The only new
config surface this design introduces is a **routing** axis
(`sensitivity.routing.*`, §7) layered on top of, and orthogonal to,
athenaeum#910's **classification** axis (`sensitivity.classes.*`): a class
can exist (be detectable, carry a read policy) without ever being routed,
and — the common case immediately after this ships — routing is globally
off by default regardless of which classes exist.

**Composition with `docs/field-corrections.md` §7.1.** §7.1 settled
(athenaeum#872) that CONTACT-value routing (an email/phone tied to an
existing entity, corrected via the field-corrections mechanism) reads and
writes through the shared contact-record path
(`pii.resolve_contact_record_for_uid` / `classify_contact_value` /
`iter_contact_records`), not a private format — "whatever the CONFIGURED
surface's own record shape is." This design's proposal honors the same
underlying principle (write through the configured surface's own
primitives — `storage.surface_root_for_class`, `models.render_frontmatter`,
`atomic_io.atomic_write_text` — never a hardcoded parallel store) but does
**not** propose reusing §7.1's own record SHAPE, because that shape is
uid-keyed by construction and this design's input is structurally different
— see §2 for why, and read that divergence as deliberate, not an oversight
of §7.1's precedent.

---

## 2. AC2 / AC3 — the pointer must resolve, and the uid problem

**AC2's requirement, restated precisely.** A pointer must carry the routed
value's identity and name the read path, so a reader can re-request the
specific value they need without prior knowledge the vault exists, and
distinct values on one page must yield distinguishable pointers (not one
marker for the whole page).

**AC3's constraint, restated precisely, because it is the fact that forces
this section's decision.** The existing excluded-surface read machinery —
`with_pii=True` in `recall`, `mcp_server._excluded_block_for_hit`,
`read_entity`/`read_person` — resolves through
`pii.ExcludedRecordIndex.by_uid(uid)`. It is uid-keyed by construction. A
hit with no `uid` has nothing to join on and the excluded block is simply
absent. §7.1's own routing model is uid+field keyed for the identical
reason. **This stage runs during the raw sweep, before Tier 2/3
classification has decided whether the raw file becomes a page at all, let
alone minted that page's uid** — so for the majority of this stage's own
input (an unstructured note that Tier 2/3 will turn into a *new* entity),
there is no uid to key on yet at the moment redaction has to happen. The
issue names this in almost these words as "the one thing that blocks the
build," because the case it is most likely to matter for — an
operator-defined `secret`/`api_key` class appearing in a retro, a project
note, or a log-derived page — is exactly the shape of raw content that
never carries a `uid` at intake time either.

**The three dispositions the issue poses, evaluated:**

- **(a) Scope the stage to uid-bearing pages.** Rejected as this design's
  proposal. A raw file only carries a pre-existing `uid` when it is
  pre-structured intake hitting Tier-0 passthrough or the handle-upsert
  path. Everything Tier 2/3 would otherwise turn into a *new* entity — the
  bulk of unstructured intake, and plausibly the majority of what an
  operator-defined `secret` class exists to catch — would silently never be
  screened. This does not merely narrow the feature; per the issue's own
  argument, it excludes the case the issue names as most likely to matter.
- **(c) Advisory-only pointer on uid-less pages** (a marker that *says*
  something was withheld, with no way to act on it). Rejected. This
  satisfies AC10 (never leave the value in the clear) but fails AC2
  outright for exactly the pages disposition (a) would have excluded — a
  reader has no way to re-request the value, which is the literal complaint
  this issue exists to fix about today's `INLINE_REDACTION_MARKER`.
  Shipping (c) for the majority case would reproduce the bug at a different
  layer.
- **(b) A new, record-keyed read path, independent of entity uid.**
  **Proposed.** Mint an identifier at redaction time that does not depend
  on any entity ever existing — derived from non-secret metadata available
  at that moment (the raw file's own reference, the sensitivity class name,
  and the matched span's character offsets; **never derived from the
  matched value itself**, so the identifier cannot leak anything about the
  value it names — see §8 AC12). Resolve it later through a **new**
  function, independent of `ExcludedRecordIndex.by_uid`, that looks a
  record up by that identifier directly rather than by joining through an
  entity page.

**Why (b) is the proposed disposition, and why the divergence from the
existing uid-keyed convention is justified rather than a shortcut.** The
existing convention is uid-keyed because every existing consumer of the
excluded surface (contact records, bounce marks) is itself keyed to an
entity that already exists at the time of the read. This stage's job is
structurally earlier in the pipeline than that convention's assumption
holds: it runs before entity identity is decided. A uid-keyed scheme cannot
be retrofitted onto content that has no uid without either delaying
redaction until after Tier 2/3 assigns one (which would require carrying
the *unredacted* value through the LLM classification step — reopening
exactly the egress problem this issue's placement decision exists to
close, since Tier 2/3 is precisely where the value would be sent to the
model) or inventing a second identity scheme anyway. Given that, minting an
independent record-keyed identifier at the moment of redaction is not a
workaround; it is the only ordering that keeps redaction before Tier 2/3
while still producing something resolvable.

**What does NOT change: access-control posture.** The issue's non-goal
explicitly narrows to this: "Changing the read path's access control...
is not in scope; access-control posture is what must not change." This
design's proposed record-keyed read function gates on the SAME
`read_policy.access`/`audience` values athenaeum#910 already resolves for
the matched class — the same vocabulary, the same enforcement point in
kind — so no new access-control mechanism is introduced; only the JOIN KEY
differs (record id instead of uid), for the structural reason above.

**A consequence to flag rather than resolve here.** Disposition (b) means
this design ships with TWO independent read paths onto the excluded
surface family — the existing uid-keyed one and this new record-keyed one
— rather than unifying them (see AC8, §8). That is a real seam this note
is not proposing to close, named explicitly rather than smoothed over.

**Verified in the spike:** a `uuid5` derived from `(raw file reference,
sensitivity class, span start, span end)` — no randomness, no dependency
on the matched value — produced a stable identifier; re-running redaction
over the same raw content produced the identical identifier and
overwrote the same vault path with byte-identical content rather than
creating a duplicate (bears on AC11, §7.1).

---

## 3. AC5 — the disposition of the existing `screen_intake` stage

**Proposed: retained, unchanged, running independently — not superseded,
not merged.** The two stages differ on every axis that would make merging
them coherent:

| | `screen_intake` (athenaeum#320) | This design |
|---|---|---|
| Runs | `remember()` write time, before the raw file exists | Librarian raw sweep, compile-time |
| Vocabulary | Hardcoded, `medical` only | Operator-defined (athenaeum#910's open set) |
| Action | Labels only (`access:` stamp) | Routes + redacts |
| Config axis | `screening.medical.*` | `sensitivity.routing.*` (proposed, §7) |

**Precedence, stated explicitly per the issue's requirement.** Both may
fire on the same raw content without conflict, because they act on
different things: `screen_intake`'s `access:` stamp lands in the raw
file's frontmatter at write time and is read back by `process_one` as
"sticky access" (issue athenaeum#320 §5) — this design's proposed hook
runs on the file's **body**, never touching the frontmatter block (§6), so
the sticky-access stamp survives untouched regardless of what this design
routes. There is no case where the two stages disagree about the same
byte range, because they never examine the same byte range: one reads
frontmatter, the other screens body. No precedence rule beyond "both run,
independently" is needed.

**Why not merge them into one stage anyway** (considered and rejected as a
proposal): `screen_intake`'s medical vocabulary is intentionally hardcoded
and narrow (its own docstring: "deliberately NOT implemented" for
`api_key`/`secret` and other categories) — folding it into the
operator-defined vocabulary would either force every deployment to express
`medical` as a `sensitivity.classes` entry (a breaking config migration for
existing `screening.medical` users) or require this design to special-case
one hardcoded category inside an otherwise-generic mechanism. Leaving both
running independently costs nothing and preserves both existing behavior
and a clean generic mechanism.

---

## 4. AC6 — per-write-path pointer mechanics

**Verified in the spike, not merely asserted:** the entity-tier sweep loop
calls `process_one` exactly once per raw file, and `process_one` is the
single dispatch point every tier passes through — `tier0_passthrough`,
`tier1_programmatic_match`, `tier2_classify`, and `tier3_derive_actions`
all consume the same in-memory `RawFile.content`. This means one hook,
placed before any of the four reads it, is structurally sufficient for all
three shapes the issue names:

- **`tier0_passthrough`** — copies raw to `wiki/` byte-for-byte. With the
  hook running first, it copies the ALREADY-redacted body — string
  substitution on the body text "just works" here, exactly as
  `storage_migrate`'s existing inline-marker substitution does today for
  its own, narrower case. **Verified in the spike:** a pre-structured raw
  file with an email in its body compiled through `tier0_passthrough`
  produced a wiki page containing the pointer, not the value, with the
  frontmatter block byte-identical (proving the body-only scoping does not
  corrupt the passthrough's schema fields).
- **`tier1_programmatic_match`** — a pure read-side matcher against
  existing entity names; it never writes. Matching against the redacted
  body rather than the raw body only matters if a matched value's literal
  text could coincide with an existing entity name, which is not an
  expected collision for the sensitivity classes this issue is scoped to
  (contact identifiers, secrets) — flagged as a low-probability edge case
  this note accepts rather than engineers around.
- **Tier 2/3 (model-generated pages)** — the placement itself is the
  mechanism: because redaction happens before ANY prompt is assembled, the
  matched value is never embedded in `fence_untrusted(...)`'s
  `user_document` block or any other prompt text, so there is no
  "does the model faithfully echo a pointer through generation and a
  truncation bound" problem to solve — the issue correctly identifies a
  model as an unreliable mechanism for preserving an injected token through
  generation, and this design's proposal sidesteps that question entirely
  rather than attempting to solve it. **Verified in the spike:** a mocked
  Tier-2/3 compile over unstructured raw containing an email produced a
  wiki page with no email present, and inspection of the mocked LLM call's
  own request payload confirmed the raw value was never sent in the prompt
  in the first place.

**Scoped explicitly to body text, not frontmatter — a proposed narrowing
this design states rather than leaves implicit.** Splicing a pointer
string into a YAML frontmatter block by character-offset substitution
risks producing invalid YAML (the pointer's own punctuation — a colon, an
em dash — inside an unquoted scalar is not guaranteed safe by construction
across every possible frontmatter value shape). The proposal is therefore:
detect and redact only in the parsed BODY (`models.parse_frontmatter`'s
second return value), leaving the frontmatter block untouched. A future
recognizer that reports a `field`-shaped match (frontmatter data, per
`SensitivityMatch.field` — a contract slot athenaeum#910 already reserved
but no shipped recognizer populates) is out of scope for this design's
proposed mechanism and must fail closed (§6, AC10) rather than be silently
skipped or attempt an unsafe splice.

---

## 5. AC4 — raw-tree observability, stated as an open gap

**The premise, corrected once already on this issue (Vitruvius Verify,
2026-08-16) and restated here because it is load-bearing.**
`_cmd_storage_lint_pii` scans `knowledge_root / "wiki"` only. `raw/` is a
**sibling** of `wiki/`, not a descendant, and has never been scanned.

**What this design does NOT change.** Raw intake is append-only by
contract elsewhere in this codebase (Tier 3's `RawFileOverBudgetError`
partial-progress contract, the sweep loop's stuck-file/quarantine ledgers,
and this design's own idempotency argument in §7.1 all depend on that
contract holding). Retroactively rewriting or scrubbing `raw/` would break
that contract and is explicitly excluded by the issue's own non-goals
("Retroactively scrubbing the existing corpus — athenaeum#437 owns the
current residue"). Once this design's stage runs, a routed value's
original bytes remain in `raw/`, in the clear, **exactly as they do
today, for every raw file, regardless of this change** — this design
neither improves nor worsens raw-tree retention; it only prevents the
value from ALSO landing in `wiki/` or being sent to a model provider.

**Stated plainly, as the issue requires, not smoothed over:** this is a
**real, unresolved measurement gap**, not a solved problem. After this
design ships and is enabled, `lint-pii` will report a clean corpus while
every routed value's original bytes still sit in `raw/`, unmeasured — the
identical shape of blind spot the issue's Vitruvius Verify pass flagged as
"worse than untracked — unobservable." This design does not propose to
close that gap. Proposed disposition: **not addressed in this design or
its follow-on slices (§10)** — a raw-tree lint (scanning `raw/` for the
same sensitivity-class matches this stage detects, reporting rather than
mutating) is a plausible, separate follow-on issue, filed against neither
this note nor athenaeum#437 as things stand today. Leaving this open
rather than asserting a false sense of coverage is the point of stating it
here.

---

## 6. AC10 — fail-closed behavior

**Proposed contract: any failure in this stage must (1) never drop the raw
file's content, and (2) never let it reach `wiki/` in the clear.** Both
requirements are satisfiable by the SAME mechanism: raising an exception
from the routing stage, before any tier has written anything, and letting
it propagate out of `process_one` uncaught.

**Verified in the spike that this mechanism already exists and needs no
new plumbing.** The entity-tier sweep loop already wraps each
`process_one` call in a sequence of `except <SpecificError>` clauses
(`RawFileTooLargeError`, `RawFileOverBudgetError`, `TransientAPIError`)
ending in a generic `except Exception` handler that: leaves the raw file
untouched on disk (never unlinked on this path), logs the failure with the
raw file's reference and the exception's type/message, counts it toward
the existing stuck-file/quarantine ledger, and retries the file on the
next sweep. **This is already exactly AC10's fail-closed contract**, for a
category of failure this design's proposed stage would be one more
instance of, not a new category needing new handling. The proposal is
therefore: raise a dedicated exception type from the routing stage (never
catch it locally inside the hook) and let the existing handler do the
rest — no change to the sweep loop's exception handling is proposed.

**Failure modes proposed to raise, rather than fall through un-redacted:**

1. A malformed `sensitivity.routing` config (surfaced through the routing
   stage's own exception type rather than a lower-level config-error type
   leaking through, so every failure this stage can produce is one
   family).
2. A detected match with no character span (a `field`-shaped match — see
   §4's note that no shipped recognizer produces one today, but the
   contract slot exists) — the stage must refuse to guess a substitution
   point rather than silently skip an un-redactable match.
3. An **unsafe vault surface**: if an operator's `storage.mapping` routes
   the matched class to an adapter that PARTICIPATES in the corpus (a
   misconfiguration — mapping a `secret` class to the default
   `wiki-markdown-embedded` adapter, say), the stage must refuse to write
   there rather than silently routing a "secret" value onto a recallable
   surface — this is the exact failure this whole issue exists to prevent,
   so a misconfiguration here must be loud. Proposed default: when no
   explicit `storage.mapping` entry names the class, resolve to the
   built-in `excluded` adapter directly — NOT the generic storage layer's
   own "undeclared maps to the default wiki surface" behavior, which is
   correct for every OTHER class but wrong here, because "undeclared" must
   mean "safe" for a vault target.
4. Any exception raised while writing the vault record itself (disk full,
   permission error, an adapter-specific failure).

**Message safety, stated as a hard requirement rather than an
implementation detail.** Every exception this stage raises must be
constructed from non-secret metadata only (file reference, class name,
span offsets, exception type name) — never from a matched value or raw
content — because the existing sweep-loop handler logs the exception's
string form verbatim. **Verified in the spike:** deliberately triggering
each of the four failure modes above and inspecting the raised exception's
string form confirmed no synthetic test value appeared in any message.

---

## 7. AC7, AC9, AC11, AC12, AC13, AC8 — the remaining criteria

### 7.1 AC11 — idempotency and re-entrancy

**Proposed: idempotency falls out of two properties held together, not one
new mechanism.** First, `raw/` is append-only and this design's stage never
writes to it — detection always runs against the SAME on-disk raw content
on every sweep, never against a previously-produced (and possibly already
redacted) wiki page. This means re-running a sweep can structurally never
"double-redact" — the source of truth for what counts as sensitive is
always the untouched original. Second, if the identifier minted for a
routed value (§2) is **deterministic** — derived only from stable metadata
(file reference, class, span), never from randomness — then re-processing
the same raw content mints the identical identifier and overwrites the
same vault record with byte-identical content, rather than creating a
duplicate. **Verified in the spike:** running redaction twice over
identical input produced byte-identical output and left exactly one vault
record on disk, not two.

### 7.2 AC7 — precedence for an `off` action and multiply-classified values

**A class with a defined-but-off routing action never routes**, checked
before any vault write is attempted for that class's matches — proposed as
a per-class override sitting on top of a global on/off switch (§7 below in
the config section, not to be confused with this §7.2's AC numbering).

**Multiply-classified values — deterministic, not incidental, as the issue
requires.** athenaeum#910's own design note (§7 Decision D6) already
documents a deliberate escape hatch: two differently-named recognisers
wrapping the same detection logic, each bound to a different class, can
both fire on one value, and `classify()` does not arbitrate between them.
This design's proposal for its OWN precedence question (which class's
record/pointer wins when two matches land on the same or overlapping
spans): sort candidate matches by `(span start, class name)` and keep the
first-sorted match at each position, dropping any later match whose span
overlaps one already kept. This never leaves a value un-redacted — it only
decides which class's vault record and pointer name it when more than one
class's recogniser matched. **Verified in the spike:** two recognisers
bound to two different classes, both matching the identical span,
produced exactly one pointer (the alphabetically-first class), not two
overlapping substitutions and not a crash.

### 7.3 AC9 — usage classification default

**Proposed: routed values are never auto-stamped with a `usage_class`.**
Usage classification (athenaeum#866) is a contact-identifier-specific axis
— whether an email/phone tied to a person may be used for outreach — and
most sensitivity classes this design routes (an operator's own
`secret`/`api_key` class, for instance) are not contact-shaped at all. Even
for the built-in `pii` class's own email/phone matches, this stage has no
owning entity/uid to classify usage AGAINST at raw-sweep time (§2). Per
`field-corrections.md` §7.1's own already-settled rule — "a correction
that omits `usage_class` ... reads back as `unclassified` (never
outreach-eligible) ... the safe direction" — a vault record this design
writes carrying no usage marker at all reads back exactly as
conservatively as an explicit `unclassified` would. This is the
conservative default the issue's own text anticipates ("the conservative
default is almost certainly right; the requirement is that it be chosen")
— chosen here, not inherited by silence.

### 7.4 AC12 — the correlation trade

**Accepted, for a reason already established elsewhere in this codebase,
not a new argument.** A distinguishable pointer per routed value is a
correlatable index into the vault, and the number of pointers on a page
discloses how many distinct values of a class it holds. This is the same
trade `pii.RedactionMarker.value_count` already makes deliberately for
contact redactions. Accepted here for the identical reason: without
distinguishable pointers, AC2's "a reader can re-request the specific
value they need" is unsatisfiable — an agent needing the SECOND of three
redacted values on a page has no way to ask for it from one undifferentiated
marker — and the alternative (one generic marker per page, regardless of
count) is the exact "byte-identical for every value on a page" dead end
the issue's own Pointer Contract section names as today's
`INLINE_REDACTION_MARKER` failure.

### 7.5 AC13 — migration story relative to athenaeum#437

**Proposed: this design is prospective only and does not subsume
athenaeum#437.** It intercepts raw files as they are newly discovered by
the sweep, going forward; it does not scan or rewrite the existing wiki
corpus retroactively. What changes is athenaeum#437's SHAPE, not its
existence: once this stage is enabled and an operator's classes are
defined, the kind of growth athenaeum#437's own tracker measured (173 →
150 → 209 findings with no migration run in between) stops accumulating,
so athenaeum#437's migration becomes a one-time pass rather than a
treadmill — which is this issue's own stated motivation, restated here as
a design commitment rather than an aspiration. athenaeum#437's migration
itself is unaffected by this design and remains necessary for historical
residue.

### 7.6 AC8 — relationship to `pii.RedactionMarker`

**Proposed: two independent contracts in this design, not one contract
rendered two ways — an explicit decision, not an oversight.**
`RedactionMarker` is an API-layer dataclass returned by
`pii.assemble_excluded_read` for a uid-keyed contact field a reader
explicitly asked to see. The pointer this design proposes is a literal
string substituted directly into a page's body text at compile time, for
a value that (per §2) may have no owning uid at all. Unifying them would
require the existing uid-keyed API read layer to also understand
record-id-keyed vault entries (§2's disposition (b)) — a read-path change
well beyond "route at intake." Proposed disposition: **left as an explicit
open question for a follow-on issue**, not resolved by this design. Naming
the read flag on `RedactionMarker` itself (the issue's own suggested
question) is bundled into that same open follow-on rather than decided
here.

---

## 8. Config surface (proposed, reversible)

A new, separate config axis from athenaeum#910's `sensitivity.classes.*`
— this design's routing switch is deliberately NOT a field added onto
`SensitivityClass` itself, so that classification (does this class exist,
what does it detect, what read policy does it carry) and routing
(does a match of this class get intercepted at intake) stay independently
toggleable:

```yaml
sensitivity:
  routing:
    enabled: false        # global switch; false = the whole stage is a
                           # no-op, byte-identical to pre-athenaeum#949
                           # behavior. Proposed default: false (dark by
                           # default — no half-wired state).
    classes:
      pii:
        action: route      # or "off" — per-class override once routing
                            # is globally enabled. Proposed default when a
                            # class block is present but action is unset:
                            # "route" (defining a class and turning routing
                            # on is read as "protect it" unless the
                            # operator explicitly opts a class out).
```

An emergency-kill-switch env var, `ATHENAEUM_SENSITIVITY_ROUTING_ENABLED`
(`true`/`false`), mirroring the `env > yaml > default` precedence every
other knob in `config.py` already follows — proposed so an operator can
force the stage off without an `athenaeum.yaml` edit, matching
`resolve_screening`'s existing precedent for `ATHENAEUM_SCREEN_MEDICAL`.

Both the global default (`false`) and the per-class default (`route`) are
reversible, low-stakes choices, not security-bearing ones — the load-bearing
security decision is §2's disposition (b), not this config's shape.

---

## 9. Not decided here (explicitly out of scope)

- **Implementing any of §1–§8.** This design specifies; §10 lists the
  slices that build it.
- **A raw-tree lint** (§5/AC4) — named as a real gap, not designed here.
- **Unifying the record-keyed read path with `pii.RedactionMarker`**
  (§7.6/AC8) — named as an open question, not designed here.
- **A `storage.mapping` completeness check** ensuring every sensitivity
  class an operator routes has a live, safe adapter mapping beyond the
  fail-closed check at write time (§6) — a possible future hardening slice,
  not proposed here.
- **Any change to `athenaeum.sensitivity`'s classify/recognizer/class
  machinery.** This design is a consumer of athenaeum#910's shipped
  surface, not a change to it.
- **Retroactively scrubbing the existing corpus** — athenaeum#437's job,
  unaffected by this design (§7.5).
- **Changing the excluded-surface read path's access-control posture** —
  explicitly out of scope per the issue's own non-goals; §2 states exactly
  what does and does not change.

---

## 10. Follow-on implementation slices

Filed against `Kromatic-Innovation/athenaeum`, each referencing this note
and athenaeum#949, labeled `feature` + `~planning` (lifecycle labels
`ready`/`building` are the orchestrator's to grant, not filed with these;
review of this note itself, per AC14, is routed separately and is not
gating these filings):

- **athenaeum#1022 — Slice 1: `sensitivity.routing` config resolver.**
  `resolve_sensitivity_routing` in `config.py` (§8's YAML shape,
  `env > yaml > default`, a dedicated config-error type), plus the
  `docs/configuration.md` entry. No behavior change on its own — nothing
  reads this resolver yet.
- **athenaeum#1023 — Slice 2: the routing/redaction mechanism, standalone.**
  Blocked by athenaeum#1022. A new module implementing:
  `sensitivity.classify()` → filtered by per-class routing action →
  deterministic overlap precedence (§7.2) → vault write keyed by the
  record-keyed identifier (§2) → body-only span substitution (§4),
  fail-closed per §6's four failure modes. Unit-tested in isolation
  (fixture knowledge roots only — no live vault call), not yet wired into
  the librarian.
- **athenaeum#1024 — Slice 3: the record-keyed read path.** Blocked by
  athenaeum#1023. `resolve_sensitive_record` (§2's disposition (b)):
  resolves a pointer's `(class, record_id)` back to its value, gated on the
  matched class's `read_policy`, failing closed (returns nothing
  resolvable, never raises with content in the message) for a malformed
  identifier, a path-traversal attempt, a class/record mismatch, or a
  missing record — each an explicit test.
- **athenaeum#1025 — Slice 4: wire into `librarian.process_one`.** Blocked
  by athenaeum#1023 and athenaeum#1024. The actual raw-sweep hook (§0/§4):
  insert before Tier 0's passthrough write, scoped to body text only.
  Integration tests proving redaction happens before `tier0_passthrough`
  writes the wiki, before a Tier-2/3 prompt is assembled (inspecting the
  mocked LLM call payload directly), and that a routing failure propagates
  through the sweep loop's EXISTING generic exception handler untouched
  (§6) — plus the `CHANGELOG.md` entry and version bump for the
  behavior-visible change this slice actually is.

---

## See also

- [`docs/sensitivity-class-vocabulary.md`](sensitivity-class-vocabulary.md)
  — athenaeum#910, the class vocabulary this design consumes unchanged.
- [`docs/field-corrections.md`](field-corrections.md) §7.1 — the
  sensitivity-routing language this design composes with (§1).
- [`docs/storage-adapter-contract.md`](storage-adapter-contract.md) — the
  `storage.mapping`/excluded-surface seam this design reuses (§2, §6).
- [`docs/security-posture.md`](security-posture.md) — the `access:`/
  `audience:` vocabulary this design's proposed read path reuses (§2).
- Prototype branch `prototype/949-sensitivity-routing-spike` — the spike
  cited throughout as verification evidence, not a decision.
