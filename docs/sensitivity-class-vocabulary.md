<!-- SPDX-License-Identifier: Apache-2.0 -->

# Sensitivity-class vocabulary — config-defined classes, shipped recognisers, one path

**Status:** DESIGN LOCK. Issue athenaeum#910. Not yet implemented — the
implementation slices this design locks are listed in §9 and are filed
against this note by the orchestrator, not by this design itself.

From the 2026-08-14 intake-architecture review (Vitruvius Specify) that also
produced [`docs/whole-store-adapter-design.md`](whole-store-adapter-design.md)
(athenaeum#911) and the sensitivity-routing language in
[`docs/field-corrections.md`](field-corrections.md) §7.1.

Companion to [`docs/storage-adapter-contract.md`](storage-adapter-contract.md)
(the `storage.mapping` seam this design routes through, unchanged),
[`docs/field-corrections.md`](field-corrections.md) §7.1 (the sensitivity
routing this design gives a vocabulary to), and
[`docs/security-posture.md`](security-posture.md) §2.1/§2.3 (the `access:` /
`audience:` read-policy vocabulary this design reuses rather than invents).

---

## 1. The problem, and why it is a compatibility boundary

Athenaeum ships exactly one sensitivity class today: `pii`
(`PII_ENTITY_CLASS = "pii"`, `src/athenaeum/pii.py:164`). It is not
deployment configuration — it is a Python module constant, imported by name
throughout the codebase (`src/athenaeum/pii.py:201`, `:245`, `:257`, and
every corpus-scan and migration site that follows). A deployment operating
under a regulatory regime athenaeum has never heard of — HIPAA, a
classified/secret/top-secret clearance gradation, a data-residency tier —
has no way to name its own class. It can *route* an existing class
differently (`storage.mapping` already generalizes that half, see §2.2), but
it cannot introduce a new one without patching this repository.

Athenaeum#910 names the fix precisely: **the vocabulary — which sensitivity
classes exist, what detects membership in each, and what read policy each
carries — must be open and shipped-with-defaults. The *handling* — routing
through `storage.mapping`, enforcing corpus exclusion by construction,
read-time `access:`/`audience:` gating — stays exactly as automatic as it is
today.** Nothing about *how* a sensitivity class is enforced changes; only
*how a class comes to exist* does.

This is filed design-first because three things this note specifies are a
**compatibility boundary for every downstream deployment**:

1. **The config surface** — the YAML shape a deployment writes once and
   never has to touch again as athenaeum's own vocabulary evolves.
2. **The recogniser registration contract** — the Python protocol a
   deployment's own detection code must implement to plug in. A breaking
   change here breaks every third-party recogniser at once, not one
   deployment's config.
3. **The class-to-storage mapping** — already a live, external seam
   (`storage.mapping`, documented and consumed by five modules per
   `docs/whole-store-adapter-design.md` §2.2). This design must extend it,
   not fork it, or every existing `storage.mapping.pii: excluded` entry in a
   deployed `athenaeum.yaml` becomes ambiguous about which mechanism it means.

Getting any of the three wrong ships a surface that has to be broken again
to fix, which is exactly the cost this design-first filing is meant to
avoid paying twice.

---

## 2. Inventory — what is hardcoded today, and the seam already there to extend

### 2.1 The hardcoded class references

| Reference | Where | What it hardcodes |
|---|---|---|
| `PII_ENTITY_CLASS = "pii"` | `src/athenaeum/pii.py:164` | The one sensitivity class name every other constant and function in this module is built around. |
| `DEFAULT_EXCLUDED_READ_MAPPING = {"person": PII_ENTITY_CLASS}` | `src/athenaeum/pii.py:201` | The one shipped page-class → surface-class non-identity entry (`docs/storage-adapter-contract.md`'s "Page class vs surface class" section). A second regulatory class needs a second entry, and today that means editing this module. |
| `contacts_surface_root()` | `src/athenaeum/pii.py:234` | A convenience wrapper hardcoded to `PII_ENTITY_CLASS` — there is no equivalent for a second class without a second hand-written function. |
| `is_pii_class_excluded()` | `src/athenaeum/pii.py:248` | Same shape: a boolean predicate name-bound to one class. |
| `_EMAIL_RE`, `_PHONE_RE` | `src/athenaeum/pii.py:322`, `:329` | Compiled module-level regexes — the only two detection "recognisers" that exist today, and they are not a registry: they are private module attributes three other modules import directly. |
| `find_inline_emails()` / `find_inline_phones()` | `src/athenaeum/pii.py:602`, `:612` | The de facto detector functions. Consumed directly (not through any lookup) by `src/athenaeum/storage_migrate.py:69-70` (the athenaeum#479/#502 migration sweep, whose own docstring at `storage_migrate.py:12-20` already calls this shape "DETECTOR-DRIVEN" — the word this design promotes to a contract) and by `src/athenaeum/outbound_pii.py:60-64` (re-imported for the outbound-draft lint, with a comment at `outbound_pii.py:47-52` explicitly justifying the direct import as "one definition... rather than a second, driftable copy" — the right instinct, applied to the wrong mechanism: a shared *function* is fine; three modules each knowing the literal name `find_inline_emails` is the part a registry replaces). |
| `CONTACT_IDENTIFIER_FIELDS = ("emails", "former_emails", "alt_emails")` | `src/athenaeum/pii.py:1693` | Hardcodes which frontmatter fields the `pii` class's contact recognisers look at. A `hipaa` class would need its own field allowlist with no shared mechanism to declare it. |
| `USAGE_CLASSES` (`observed`/`provider`/`unclassified`) | `src/athenaeum/pii.py:1731` | **A different axis, not sensitivity class** — worth naming so this design does not conflate them. `usage_class` is a per-*value* permission (may this specific address be used for outreach), scoped entirely inside the `pii` class per `docs/security-posture.md` §2.3. Sensitivity class (this design) is which regulatory bucket a fact belongs to at all. A `hipaa` class gets its own read policy (§4); it does not need a `usage_class`-shaped table unless a follow-on slice decides that regime needs one too. |
| **A "street address" recogniser** | — (searched, not found) | Athenaeum#910's own summary states "shipped recognisers cover email, phone and street address." A repo-wide search (`rg -i 'street|postal|zip.code'` across `src/athenaeum/`) finds no such detector. **This design corrects that premise**: athenaeum ships two recognisers today, not three. Street-address detection is real future scope, not existing behavior — see §5 and slice S2 in §9. Stating it as already-shipped would misrepresent the starting point to a reader who did not check. |

### 2.2 The `storage.mapping` seam `pii: excluded` already routes through

This is the seam athenaeum#910 explicitly says the new vocabulary must reuse,
and it already generalizes exactly the half it needs to: an *entity class
name* (a string) resolves to a *storage adapter* (a surface + corpus
policy), never a hardcoded path.

- `resolve_storage_mapping(config)` — `src/athenaeum/config.py:2347-2372` —
  reads `storage.mapping` from `athenaeum.yaml`, returns an entity-class →
  adapter-name `dict[str, str]`, empty when unset.
- `resolve_adapter_for_class(entity_class, config)` —
  `src/athenaeum/storage.py:286-310` — resolves that name to a
  `StorageAdapter` (its `corpus_policy`), raising `StorageConfigError`
  (`storage.py:72-80`) on an unknown adapter name rather than silently
  routing into the default corpus.
- `is_excluded(entity_class, config)` — `src/athenaeum/storage.py:357-360`
  — the boolean gate every corpus consumer checks.
- `register_adapter()` — `src/athenaeum/storage.py:179-201` — the
  **code**-registration half: built-in adapters
  (`_BUILTIN_ADAPTERS`, `storage.py:150-153`) can never be shadowed
  (`storage.py:192-195`), a config-defined adapter of the same name always
  wins over a code-registered one (`storage.py:264-283`,
  `docs/storage-adapter-contract.md` "Extending: add a surface with no core
  change").

Today exactly one class name (`"pii"`) is ever passed to
`resolve_adapter_for_class`, and it is passed by hardcoded literal
(`pii.py:257`'s `is_excluded(PII_ENTITY_CLASS, config)`), not by a name an
operator chose. **The mapping mechanism is already class-name-agnostic; only
the caller is hardcoded to one name.** This is the load-bearing finding: this
design does not touch `storage.py` or `storage.mapping` at all (§3.3, §7
Decision D3) — it only removes the hardcoded caller.

### 2.3 Screening (athenaeum#320) and `field-corrections.md` §7.1 — classification is already deployment configuration, twice

Two existing seams already establish "classification is config, not code," and
this design is the third instance of the same principle, not a new one:

- **Screening** (`src/athenaeum/screening.py`, issue athenaeum#320) classifies
  raw intake at write time and stamps a read-time `access:` label
  (`docs/security-posture.md:49`). Its config resolver,
  `resolve_screening()` (`src/athenaeum/config.py:1815-1868`), is the
  concrete pattern this design's own resolver copies (§3.1). Screening's
  detection is **keyword + regex, deliberately not ML/NER**
  (`screening.py:26-28`) "so the boundary is auditable and diff-reviewable"
  — the same posture this design's recognisers inherit (§3.2). Screening
  ships exactly one category (`medical`) today and is explicit that its
  own vocabulary — protected characteristics, financial account, API
  key/secret — is "deliberately NOT implemented here" (`screening.py:14-16`).
  Athenaeum#910 is, among other things, the generalization screening's own
  docstring already gestures at.
- **`docs/field-corrections.md` §7.1** ("Sensitivity routing") already states
  the principle this design formalizes: *"The sensitivity classification is
  deployment configuration, not a constant in this repo"*
  (`field-corrections.md:577`). It also already establishes that a
  contact-value correction may declare a **usage classification** — a
  different, per-value axis (§2.1 above) — and that the record-shape
  question for the excluded surface is settled to read/write through the
  *same* contact-record path regardless of which surface is configured
  (`field-corrections.md:589-597`, citing issue athenaeum#872). This design's
  §6 read-policy inheritance and §7 migration story both lean on that same
  "same path, configured surface" posture.

### 2.4 The config.py idiomatic pattern this design's config surface must fit

`src/athenaeum/config.py`'s module docstring states the factoring rule this
design is bound by: *"a new knob is not done until it is added here as a
`resolve_*` function AND documented in `docs/configuration.md`"*
(`config.py:14-18`), and the `_DEFAULTS` seeding rule at `config.py:165-172`:
**seed a key in `_DEFAULTS` only when that dict is its single source of
truth** — a resolver with its own module-level default (or one merged in
from the owning domain module) must **not** also be seeded, or the seed
becomes silently authoritative and the code default becomes unreachable
(the athenaeum#187 regression the comment cites by name).

The representative example this design copies is the **raw-primitives /
validated-dataclass split** already used for `storage.adapters`:

- `resolve_storage_adapters(config)` (`config.py:2375-2401`) returns raw,
  still-unvalidated per-adapter `dict[str, Any]` blocks — config.py's job
  stops at "read the YAML shape defensively, drop malformed entries."
- `athenaeum.storage._adapter_from_config()` (`storage.py:240-262`) is where
  those raw dicts become validated `StorageAdapter` objects, and where a
  malformed entry raises `StorageConfigError` **at build time, with the
  adapter name in the message** — not at config.py's read time.

And the **operator-override / module-owned-default split** used for
`excluded_read_mapping`: `resolve_excluded_read_mapping()`
(`config.py:2404-2437`) returns **only what the operator configured** (empty
when unset), explicitly *not* seeding the shipped default — the shipped
`{"person": "pii"}` entry lives in `pii.DEFAULT_EXCLUDED_READ_MAPPING`
(`pii.py:201`) and is merged in by the *consuming* module, not by config.py
(`config.py:2415-2419` states this precedent by name: *"this resolver
reports only what the OPERATOR configured — `resolve_storage_mapping`'s
precedent"*). §3.1 below follows this exact split for the same reason
`resolve_excluded_read_mapping` does: the shipped defaults are not something
config.py owns, they are something the owning domain module (`sensitivity.py`,
§3.2) ships and merges.

---

## 3. The three design surfaces

### 3.1 (a) The class-definition config surface

A new top-level YAML key, `sensitivity:`, sibling to `storage:` and
`screening:` — **not** nested under either, because it answers a different
question than both (§2.2, §2.3): `storage.mapping` says *where a class's
bytes live*; `screening` says *what to do at intake write time for one
built-in category*; `sensitivity.classes` says *what classes exist, what
detects them, and what read policy each carries*.

```yaml
sensitivity:
  classes:
    pii:                                # shipped, see §5 for full expression
      recognizers: [email, phone]
      read_policy:
        access: personal

    hipaa:                              # deployment-defined
      recognizers: [hipaa-identifier]   # a code-registered recognizer (§3.2);
                                         # this repo ships none
      read_policy:
        access: confidential
        audience: [compliance]

    classified:                         # deployment-defined, no auto-detection —
      recognizers: []                   # operator-tagged only (§3.2's empty-list case)
      read_policy:
        access: personal
        audience: [cleared]

    secret:
      inherits: classified              # §4 — defaults unset read_policy fields
      read_policy:
        audience: [cleared-secret]      # overrides just this field

    top-secret:
      inherits: secret
      read_policy:
        audience: [cleared-top-secret]

storage:
  mapping:                              # UNCHANGED seam (§2.2) — the class name
    pii: excluded                       # IS the storage.mapping key. No new
    hipaa: hipaa-vault                  # routing table is introduced.
    classified: gov-classified-store
    secret: gov-classified-store
    top-secret: gov-classified-store

  adapters:
    hipaa-vault:
      backing_store: markdown
      surface_root: hipaa
      corpus_policy: {embedded: false, recallable: false, merge_eligible: false}
    gov-classified-store:
      backing_store: markdown
      surface_root: classified
      corpus_policy: {embedded: false, recallable: false, merge_eligible: false}
```

`resolve_sensitivity_classes(config)` — new, in `src/athenaeum/config.py`,
placed beside `resolve_storage_mapping`/`resolve_storage_adapters` — follows
the raw-primitives split of §2.4 exactly: reads `config["sensitivity"]["classes"]`
defensively (non-dict/blank entries dropped, same posture as
`resolve_storage_adapters`), returns `dict[str, dict[str, Any]]` of
still-unvalidated per-class blocks, and is **not** seeded in `_DEFAULTS`
(§2.4's rule) because the shipped default (§5) has its own single source of
truth in a new `sensitivity.py` module, mirroring exactly how
`pii.DEFAULT_EXCLUDED_READ_MAPPING` (not `_DEFAULTS`) is the shipped-default
home for `excluded_read_mapping`.

A new `sensitivity.py` module (L3 — see §3.2's layering note) owns:

- `available_classes(config) -> dict[str, SensitivityClass]` — merges, in
  precedence order lowest to highest: built-in shipped classes
  (`_BUILTIN_CLASSES`, a module constant — §5), then
  `resolve_sensitivity_classes(config)`'s operator entries — mirroring
  `storage.available_adapters()`'s built-in/code/config precedence
  (`storage.py:264-283`) with the "code" tier omitted, because a class is
  pure declared policy with nothing to register in code (§7 Decision D2).
  A config entry reusing a built-in class name **overrides** it (an operator
  may redefine `pii`'s read policy or recognisers), consistent with
  `docs/storage-adapter-contract.md`'s "config wins" rule for adapters —
  the one difference from `storage.py` being that overriding a *class* name
  is permitted (there is no name-shadow protection here), because unlike a
  storage adapter's `backing_store`/`corpus_policy` a class carries no
  physical-layer invariant a shadow could silently violate.
- Validation raising a new `SensitivityConfigError(ValueError)` (mirrors
  `StorageConfigError`, `storage.py:72-80`, and `ScreeningConfigError`,
  `screening.py:81-83`) at build time, never a silent fallback — same fail-
  loud posture as both existing config-error classes.

### 3.2 (b) The recogniser registration contract

**The anti-special-casing requirement, stated as a protocol.** A recogniser
detects a *shape* (email-looking, phone-looking, a deployment's own
HIPAA-identifier pattern); a class config (§3.1) declares which recognisers'
matches count toward it. This decouples detection (code, mechanical, the
same posture screening's docstring already commits to — "transparent keyword
+ regex... auditable and diff-reviewable," `screening.py:26-28`) from
classification (config, deployment policy) — which is what makes "email,
phone, and a deployment's HIPAA pattern all classify the same way" true by
construction rather than by convention.

```python
# src/athenaeum/sensitivity.py — new module, L3 (peer to pii.py/screening.py;
# imports athenaeum.storage L1/L2, athenaeum.config L2, athenaeum.atomic_io L0)

@dataclass(frozen=True)
class SensitivityMatch:
    recognizer: str                    # stable recognizer name, e.g. "email"
    value: str                         # the matched substring/value
    field: str | None = None           # frontmatter field name, when matched
                                        # off structured data rather than body text
    span: tuple[int, int] | None = None  # text offset, when matched in body text

class SensitivityRecognizer(Protocol):
    name: str                          # stable id — the string a class's
                                        # `recognizers:` list names

    def detect(
        self, *, text: str, frontmatter: Mapping[str, Any] | None
    ) -> list[SensitivityMatch]:
        """Pure, offline, deterministic. No I/O, no LLM call — same posture
        as athenaeum.screening (screening.py:26-28) and athenaeum.outbound_pii
        (outbound_pii.py:14-15, 'a pure, offline, deterministic text lint')."""
        ...

def register_recognizer(
    recognizer: SensitivityRecognizer, *, replace: bool = False
) -> None:
    """The code extension point — mirrors storage.register_adapter's shape
    (storage.py:179-201) exactly: a built-in recognizer name can never be
    shadowed; re-registering a custom name raises unless replace=True."""
    ...

def available_recognizers(config: dict[str, Any] | None) -> dict[str, SensitivityRecognizer]:
    """Built-ins ∪ code-registered. UNLIKE available_classes, config cannot
    define a recognizer — detection is behavior, not data, so it can only
    be code (§7 Decision D2's converse)."""
    ...
```

**Shipped recognisers use the identical registration call — this is the
whole point.** `sensitivity.py` registers its own built-ins at import time:

```python
# inside src/athenaeum/sensitivity.py, at module scope
register_recognizer(_EmailRecognizer())   # wraps pii.find_inline_emails, pii.py:602
register_recognizer(_PhoneRecognizer())   # wraps pii.find_inline_phones, pii.py:612
```

exactly the call a deployment makes for its own `hipaa-identifier`
recogniser — `register_recognizer(MyHipaaRecognizer())` — from wherever that
deployment's own import happens (the same "adding a surface is config + a
`register_adapter()` call, no core change" shape
`docs/storage-adapter-contract.md`'s "Extending" section already documents
for adapters, applied to recognisers). There is no `if built_in` branch
anywhere in this contract; `_EmailRecognizer`/`_PhoneRecognizer` are
ordinary `SensitivityRecognizer` implementations that happen to ship in this
repo, wrapping the existing `find_inline_emails`/`find_inline_phones`
functions (§2.1) rather than re-implementing detection — the functions
themselves are untouched, only how a class binds to them changes.

**Binding is a partition, not an overlapping tag set (§7 Decision D6).** A
recognizer name may appear under at most one class's `recognizers:` list in
a given resolved config; `available_classes()` raises `SensitivityConfigError`
on a recognizer bound to two classes. A detected match therefore always has
exactly one destination class — there is no "which class wins" question to
answer downstream.

**Empty `recognizers: []` is honoured literally** (mirroring
`storage.excluded_fields`'s empty-list-is-a-statement rule,
`docs/storage-adapter-contract.md:171-173`): a class with no recognisers is
never auto-detected, only reached by explicit operator/agent tagging (e.g. a
correction's `usage_class`-style manual field, or a future `athenaeum tag`
verb — out of scope here). `classified` in §3.1's example is exactly this
case: no regex can decide "this is classified," only a human can.

**Where recognisers run** is unchanged pipeline shape, not new plumbing:
`screen_intake`'s call site (`mcp_server.py:553`, before the append-only
write) and `storage_migrate`'s whole-page detector sweep
(`storage_migrate.py:19`) are the two existing points that already run
detection against text/frontmatter; a follow-on slice (§9, S3) threads
`sensitivity.classify()` through both rather than each continuing to import
`find_inline_emails`/`find_inline_phones` by name.

### 3.3 (c) The class-to-storage-surface mapping

**No new mapping is introduced.** A sensitivity class name **is** an entity
class name in `storage.mapping`'s existing sense — `sensitivity.classes.hipaa`
and `storage.mapping.hipaa` name the same string on purpose. Resolving where
a class's excluded record lives is, unchanged, `resolve_adapter_for_class`
(`storage.py:286-310`) → `surface_root_for_class` (`storage.py:362`), exactly
as `pii.contacts_surface_root()` (`pii.py:234-245`) already calls it for the
one class that exists today. §2.2 already showed the mapping mechanism was
class-name-agnostic; this design's only change to that call graph is that
the class name it passes is no longer the literal `PII_ENTITY_CLASS`
constant but whichever name `sensitivity.classes` iterates.

`storage.excluded_read_mapping` (page `type:` → surface class,
`docs/storage-adapter-contract.md`'s "Page class vs surface class") and
`storage.excluded_fields` (which frontmatter fields on an excluded record are
data) are likewise **reused unmodified** — a `hipaa` class that stores on
its own page type declares its own `excluded_read_mapping`/`excluded_fields`
entries the same way `person: pii` does today (`pii.py:201`,
`docs/storage-adapter-contract.md:118-125`).

---

## 4. Read-policy inheritance and override

Every class's `read_policy` is `{access: <level>, audience: <roles>}` —
**the same vocabulary athenaeum#312 already ships**, not a new one:
`access` is one of the four `_ACCESS_RANK` levels (`open` / `internal` /
`confidential` / `personal`, `screening.py:71-77`), and `audience` is the
same opaque-role-list mechanism `docs/security-posture.md` §2.1 already
documents for `athenaeum serve --audience`. A sensitivity class's read
policy is enforced at the exact points that vocabulary is already enforced
today (recall render, MCP audience scoping) — this design adds no new
enforcement point, only a new place those two fields can be set from.

**Inheritance (`inherits: <parent-class-name>`) is field-default-fill only.**
An unset field on a child's `read_policy` takes the parent's resolved value
(after the parent's own inheritance, if any, is resolved first — a chain,
not required to be one level); an explicitly set field on the child always
wins, in either direction (tighter or looser than the parent). `secret`
inheriting `classified` and only overriding `audience` (§3.1's example)
picks up `access: personal` from the parent without repeating it; nothing
stops a class from inheriting `classified` and setting `access: open` if an
operator genuinely configures that.

See §7 Decision D4 for why this does **not** enforce a monotonic-restriction
floor (the `more_restrictive` shape `screening.py:85` already has, but for a
different purpose).

**Interaction with `storage.mapping`.** Read policy and storage routing are
independent config surfaces that both key off the same class name
(§3.3), and nothing ties them together structurally — a class's `read_policy`
governs what `recall`/MCP audience-scoping does with a page once resolved;
`storage.mapping` governs which surface the page physically lives on and
whether it joins the corpus at all (`docs/storage-adapter-contract.md`'s
three orthogonal `corpus_policy` bits). A class that maps to a
non-`recallable` surface is unreachable through recall regardless of its
`read_policy.access`; `read_policy` only matters for a class whose surface
is at least reachable through the read interface (`recall(with_pii=True)` /
`read_entity`, the athenaeum#883/#885/#886 surfaces
`docs/security-posture.md:29-33` names).

---

## 5. Shipped classes, expressed in the new vocabulary

**Today's one shipped class, in full:**

```yaml
sensitivity:
  classes:
    pii:
      recognizers: [email, phone]
      read_policy:
        access: personal          # matches security-posture.md §2.1's
                                   # owner-only-for-restricted-caller posture

storage:
  mapping:
    pii: excluded                 # UNCHANGED — pii.py:257's
                                   # is_pii_class_excluded() literal today
```

This is the `_BUILTIN_CLASSES` module constant §3.1 references — the shipped
default that ships when a deployment writes no `sensitivity:` config at all,
merged in by `sensitivity.available_classes()` exactly as
`pii.DEFAULT_EXCLUDED_READ_MAPPING` is merged in today (§2.4). **A fresh
install's behavior is unchanged**: `email`/`phone` recognisers exist,
register at import time, and feed the `pii` class, which routes through
`storage.mapping.pii` (or the identity default if unset) precisely as
`PII_ENTITY_CLASS`-literal code does before this design lands. Nothing about
how the `pii` class is handled changes — per athenaeum#910's own "out of
scope: changing how any currently shipped class is handled."

**The one class the issue's own summary claims is shipped but is not
(§2.1):** a `street-address` recognizer. Expressing it in the new vocabulary
is future work (§9, S2), and until it ships, `pii`'s `recognizers:` list
above stays `[email, phone]` — a two-item list, honestly stated, not a
three-item list copied from the issue text.

---

## 6. Migration story — a corpus classified under an earlier vocabulary

This design borrows its answer directly from an existing, already-settled
precedent rather than inventing a new one: `docs/field-corrections.md` §13,
**"Adoption is forward-only"** — *"A deployment adopting this contract is
changing its write paths, not its existing content... there is no
retroactive cleanup pass and none should be built."* The same posture
applies here, for the same reason: a page's `type:` (or an excluded record's
surface class) is a fact written once at compile time, and rewriting it
retroactively is a bulk edit, not a config change.

**What follows concretely:**

1. **Renaming or retiring a class in `sensitivity.classes` never rewrites
   existing content.** A page compiled under an old vocabulary keeps
   whatever class name it was written with.
2. **`storage.mapping` must keep routing every class name any existing
   content still carries, even after that name is removed from
   `sensitivity.classes`.** This is not a new rule — it already follows from
   `resolve_adapter_for_class`'s existing fail-loud behavior
   (`storage.py:296-310`): a class name with no `storage.mapping` entry falls
   through to the identity/default resolution, but a class name mapped to an
   adapter that no longer exists raises `StorageConfigError` loudly rather
   than silently losing the surface. Removing a `sensitivity.classes` entry
   is safe (it only stops new detection); removing the matching
   `storage.mapping` entry while old content still carries that class name
   is what an operator must not do, and this design adds no enforcement
   beyond the existing loud raise to catch it — a follow-on slice (§9, S5)
   is where a `storage.mapping` completeness check against the corpus could
   be added if this proves to be a footgun in practice.
3. **A genuine bulk reclassification — "move every page tagged `hipaa`
   under the old rules to `hipaa-v2`" — is not a mechanism this design
   ships.** It is exactly the shape of work Lane C's field corrections
   already solve: a correction batch (`docs/field-corrections.md` §3-§4)
   with `op: set`, `field: type` (or whatever field carries the class),
   applied deterministically at tier 0 through the existing conflict
   resolution and audit ledger. A deployment wanting a migration pass writes
   one; this design does not need to invent a second bulk-edit mechanism
   when field-corrections.md already ships one that fits.
4. **A recognizer whose detection pattern changes (tightened, loosened, or a
   fixed false-positive) does not retroactively reclassify already-compiled
   pages either** — the same forward-only rule, applied to code rather than
   config. A detection change affects only newly-compiled intake from that
   point forward.

---

## 7. Decisions

> **Decision D1 — recognisers detect shapes; classes declare which
> recognisers feed them (many-to-one, not recogniser-owns-class).**
> Rejected: a recognizer directly names its destination class (today's
> implicit shape — `find_inline_emails`'s callers all assume `pii`). Rejected
> because it re-couples detection to policy, the exact coupling athenaeum#910
> exists to remove: a deployment wanting phone-shaped matches routed
> differently from email-shaped matches would have to fork detection code
> rather than edit two lines of YAML.

> **Decision D2 — a sensitivity class is pure config (name + recognizer list
> + read policy); only recognisers get a code-registration path.**
> Rejected: a symmetric `register_class()` mirroring `register_adapter`'s
> three-tier built-in/code/config precedence. Rejected because a class has no
> behavior to implement — it is a name, a list of recogniser names, and a
> read policy, all expressible as data — so a code path would buy nothing a
> YAML block does not already give the operator. This matches
> `docs/storage-adapter-contract.md`'s own precedent: the storage *adapter*
> is code because it does I/O; the *mapping* from class to adapter is pure
> config because it does not.

> **Decision D3 — class-to-storage routing stays exclusively in
> `storage.mapping`; no `storage_adapter:` field is added under
> `sensitivity.classes`.** Rejected: letting a class config name its adapter
> directly (`sensitivity.classes.hipaa.storage_adapter: hipaa-vault`).
> Rejected because it is exactly the forked seam
> `docs/whole-store-adapter-design.md` §6.1 D5 already warns against for a
> different layer ("two ways to reach the store... invisible in tests
> because both paths work") — here it would be two ways to answer "where
> does this class live," one of which the issue itself explicitly says must
> not exist ("through the SAME `storage.mapping` seam that routes `pii:
> excluded` today").

> **Decision D4 — read-policy inheritance is default-fill only; it does not
> enforce a monotonic-restriction floor on children.** Rejected: reusing
> `screening.more_restrictive()` (`screening.py:85`) as an inheritance
> ceiling, so `secret` could never resolve looser than `classified`.
> Rejected for two reasons: first, `more_restrictive` today arbitrates a
> single escalation decision inside one classifier, not a cross-class
> config-inheritance ceiling, and stretching it to a second purpose is a
> bigger behavioral claim than this design needs to make. Second, and more
> load-bearing: `storage.mapping` already lets an operator route `secret` to
> a *weaker* surface directly (map it to the plain `wiki-markdown-embedded`
> adapter, say, by mistake or on purpose) with nothing in this design or any
> existing one stopping it — enforcing a ceiling on `read_policy` alone while
> `storage.mapping` has no matching ceiling would be a false sense of
> security on the layer that matters less. A real floor, if a deployment
> needs one, is a lint over the *resolved* `(read_policy, storage adapter)`
> pair together — worth a follow-on slice (§9, S5), not worth pretending
> this design already provides it by constraining one field.

> **Decision D5 — no retroactive reclassification tooling ships with this
> design; migration is forward-only (§6), reusing field-corrections.md's
> correction batches as the bulk-edit mechanism for anyone who wants one.**
> Rejected: a dedicated `athenaeum reclassify` bulk-rewrite command.
> Rejected because it duplicates a mechanism `docs/field-corrections.md`
> already ships end to end (conflict resolution, idempotency, audit ledger,
> fallthrough) for exactly this shape of change — a field-level correction
> applied at scale — and building a second one would be the "proliferating
> typed interfaces" anti-pattern `field-corrections.md` §1.1 already rejects
> in its own domain.

> **Decision D6 — a recognizer binds to at most one class in a resolved
> config; classification is a partition.** Rejected: letting one recognizer
> feed several classes (e.g. `email` matches counted toward both `pii` and a
> deployment's own `contact-data` class). Rejected because it reopens the
> "which class does a match belong to" question every downstream consumer
> (routing, read policy) would then have to re-resolve per match rather than
> per recognizer-name, for no expressed use case in the issue. A deployment
> wanting the same *shape* to feed two policies can register two thin
> recognisers wrapping the same detection function under two names — cheap,
> and it keeps the partition invariant intact.

---

## 8. Not decided here (explicitly out of scope, per athenaeum#910)

- **Implementing `sensitivity.py`, `resolve_sensitivity_classes`, or any
  recognizer.** All of §3 is a specification for §9's slices to build.
- **Changing how the `pii` class is handled today.** §5 shows it expressed
  in the new vocabulary; its behavior — recognisers, routing, read posture —
  is byte-identical.
- **The storage adapter's own generalization to a full `Store` protocol.**
  Tracked separately in `docs/whole-store-adapter-design.md` (athenaeum#911);
  this design's §3.3 depends only on the seam as it exists today
  (`surface_root_for_class` returning a `Path`), and nothing here is
  affected by whether that seam later grows a `Store` abstraction
  underneath it — the class-name-to-adapter-name resolution this design
  reuses does not change shape either way.
- **A hard read-policy floor across `(read_policy, storage.mapping)`
  pairs.** Named as a possible follow-on lint in Decision D4, not specified.
- **A street-address recognizer's actual pattern/implementation.** Named as
  future scope in §5/§9; this note does not draft its regex or NER
  approach.
- **Authorization — who may configure `sensitivity.classes` or read a given
  class's `audience:` role.** Same deferred question
  `docs/security-posture.md` §2.3 already defers for `usage_class` (athenaeum#864).

---

## 9. Follow-on implementation slices

- **S1 — `sensitivity.py` module + config resolver.** `SensitivityMatch`,
  `SensitivityRecognizer` protocol, `register_recognizer`/
  `available_recognizers`, `SensitivityClass`/`available_classes`,
  `SensitivityConfigError`; `resolve_sensitivity_classes` in `config.py`;
  built-in `email`/`phone` recognisers wrapping the existing
  `find_inline_emails`/`find_inline_phones`; the shipped `pii` class from
  §5. No caller migrated yet.
- **S2 — street-address recognizer.** The recogniser athenaeum#910's own
  summary describes as already shipped (§2.1) but is not; implement and
  register it through the S1 contract, bound to `pii` by default per §5.
- **S3 — migrate `screen_intake` and `storage_migrate`'s detector sweep onto
  `sensitivity.classify()`.** Replaces the direct
  `find_inline_emails`/`find_inline_phones` imports at
  `storage_migrate.py:69-70` and `outbound_pii.py:60-64` with the registry
  call, proving the "shipped and deployment-defined use the identical path"
  requirement end to end rather than by design-note assertion only.
- **S4 — `docs/configuration.md` entry.** Per `config.py`'s own factoring
  rule (§2.4): "a key in code and not in that table is drift." Adds a
  `## Sensitivity classes (athenaeum#910)` section alongside the existing
  `## Intake screening (athenaeum#320)` one.
- **S5 — `storage.mapping` completeness lint (optional, MoSCoW: could).**
  A corpus-scan check that every class name any excluded record carries has
  a live `storage.mapping` entry, catching the failure mode §6 point 2
  describes before it becomes a `StorageConfigError` at read time. Also the
  natural home for Decision D4's deferred `(read_policy, storage.mapping)`
  floor lint, if a later review decides it is worth building.

---

## See also

- [`docs/storage-adapter-contract.md`](storage-adapter-contract.md) — the
  `storage.mapping` seam this design routes through unchanged (§3.3).
- [`docs/field-corrections.md`](field-corrections.md) §7.1 (sensitivity
  routing as deployment config), §13 (adoption is forward-only, the
  precedent §6 borrows).
- [`docs/security-posture.md`](security-posture.md) §2.1 (audience scoping),
  §2.3 (contact-value usage classification — the adjacent, distinct axis
  named in §2.1).
- [`docs/whole-store-adapter-design.md`](whole-store-adapter-design.md) —
  the sibling design-first note from the same 2026-08-14 review; its §6.1
  D5 "extend, never fork" is the same principle Decision D3 applies here.
- [`docs/configuration.md`](configuration.md) — `## Intake screening
  (athenaeum#320)` is the section this design's own config entry (§9, S4)
  will sit beside.
