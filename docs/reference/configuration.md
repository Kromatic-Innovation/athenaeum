# Configuration Reference

This page is GENERATED from `src/athenaeum/config.py` by `scripts/gen_config_reference.py` — do not hand-edit it. CI (`tests/test_generated_docs_parity.py`) regenerates it and fails the build on any diff. To change an entry, change the resolver's own docstring (or its default/precedence, if that's what actually changed) and regenerate:

```
python scripts/gen_config_reference.py
```

Every `resolve_*` function in `athenaeum.config` appears below, grouped by the top-level `athenaeum.yaml` section it reads (or under **Other** when it reads no yaml key of its own). `athenaeum.yaml` lives at the knowledge root (`<knowledge_root>/athenaeum.yaml`, default `~/knowledge/athenaeum.yaml`). An em dash (—) means that layer does not exist for a given knob.

Defaults were captured by actually invoking each resolver with no config and no `ATHENAEUM_*` environment variables set, except for a small, explicit set of resolvers whose signature takes a required non-config argument (a model knob, a retention family, a recall backend) or whose return value is a filesystem path relative to a knowledge root — those are hand-annotated because invoking them generically would either be meaningless (no single "the" default) or bake this generator's own machine into a committed file.

## Models

All model values are free-form model-id strings passed to the Anthropic SDK. One row per model-choosing knob passed to `config.resolve_model` — see `resolve_model` above for the shared resolution mechanism. Each Default value below is read live from the knob's own module, never a hand-typed copy.

| Knob | Env var | YAML key | Default | Used by |
|---|---|---|---|---|
| Classifier | `ATHENAEUM_CLASSIFY_MODEL` | `models.classify` | `claude-haiku-4-5-20251001` | Tier-2 classifier and the C4 contradiction detector — one knob by design. |
| Writer | `ATHENAEUM_WRITE_MODEL` | `models.write` | `claude-sonnet-5` | Tier-3 wiki writer. |
| Topic extractor | `ATHENAEUM_TOPIC_MODEL` | `models.topic` | `claude-haiku-4-5-20251001` | `athenaeum query-topics` recall query rewriting. |
| Resolver | `ATHENAEUM_RESOLVE_MODEL` | `models.resolve` | `claude-opus-5` | Contradiction resolver (proposes a winner once the detector flags a conflict). |
| Reasoning tier 1 | `ATHENAEUM_REASONING_T1_MODEL` | `models.reasoning_t1` | `claude-haiku-4-5-20251001` | First-pass model for the reasoning-tier chain. |
| Reasoning tier 2 | `ATHENAEUM_REASONING_T2_MODEL` | `models.reasoning_t2` | `claude-opus-4-8` | Escalation model for the reasoning-tier chain. |
| Rule proposals | `ATHENAEUM_RULE_PROPOSALS_MODEL` | `models.rule_proposals` | `claude-opus-4-8` | Rule-proposal drafting call. |

## `athenaeum`

### `resolve_wiki_dedupe_min_body_chars`

- **YAML path:** `athenaeum.wiki_dedupe.discover_wiki_dedupe_candidates`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `0`
- **Precedence:** `athenaeum.yaml` > code default

Resolve the wiki-dedupe eligibility body-length floor.

``athenaeum.wiki_dedupe.discover_wiki_dedupe_candidates`` excludes a
candidate page whose body (post-frontmatter, stripped) is shorter than
this many characters. Rationale, from 's corpus-level
characterization (counts only, see the issue and the module docstring
of ``wiki_dedupe.py`` for the full measurement): 's
chunk-and-mean-pool fix only DILUTES a structurally uniform lede's
contribution to a page's pooled vector when there is a SECOND chunk to
average it against. A page short enough to fit in one
`athenaeum.wiki_dedupe._CHUNK_CHARS`-sized chunk gets a mean-pool
of exactly one vector -- a mathematical no-op, byte-identical to the
pre- whole-page embedding -- so this provides it
ZERO protection against boilerplate-lede dominance. A two-chunk page
(one lede chunk + one body chunk, the modal shape in the operator's
live corpus) gets only 50% dilution, the weakest non-zero case. This
floor lets an operator exclude that short end of the eligible
population from vector-similarity candidacy entirely -- a
deterministic, LLM-free gate -- rather than trust a similarity score
computed over one or two chunks where the boilerplate signal
dominates.

DEFAULT 0 (OFF): whether a given corpus's short pages are actually
driving over-clustering is corpus-specific and was NOT re-measured
against the live embedder (blocked in the environment that produced
this fix -- see the issue). Shipping a non-zero default would silently
change which pages are eligible for merge/dedup comparison across the
operator's whole corpus without a live re-measurement to justify a
specific cutoff. Operators opt in via ``athenaeum.yaml`` once they can
re-measure cluster composition at a chosen floor. No seed in
``_DEFAULTS`` so the code default stays reachable.
``bool`` (an ``int`` subclass) and non-int / ``<= 0`` yaml values fall
through to 0 (off) -- mirrors `resolve_min_cluster_cohesion`'s
coercion contract.

### `resolve_dimensions`

- **YAML path:** `athenaeum.yaml`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `DimensionRegistry(dimensions=(Dimension(name='recorded-time', kind='interval', null_means='unknown', values=None, separates=False, applies_to={}, state='enforced', origin='builtin', since=None, coverage_threshold=1.0), Dimension(name='observed-time', kind='interval', null_means='unknown', values=None, separates=False, applies_to={}, state='enforced', origin='builtin', since=None, coverage_threshold=1.0), Dimension(name='valid-time', kind='interval', null_means='universal', values=None, separates=True, applies_to={}, state='enforced', origin='builtin', since=None, coverage_threshold=1.0), Dimension(name='scope', kind='hierarchy', null_means='universal', values=None, separates=True, applies_to={}, state='enforced', origin='builtin', since=None, coverage_threshold=1.0), Dimension(name='subject', kind='identity', null_means='unknown', values=None, separates=True, applies_to={}, state='enforced', origin='builtin', since=None, coverage_threshold=1.0), Dimension(name='memory-class', kind='enum', null_means='unknown', values=('axiom', 'decision', 'entity', 'fact', 'guideline', 'procedure', 'reference'), separates=True, applies_to={}, state='backfill', origin='builtin', since=None, coverage_threshold=1.0)))`
- **Precedence:** `athenaeum.yaml` > code default

Resolve the ``dimensions:`` config block into a validated registry.

Returns a `athenaeum.dimensions.DimensionRegistry` — always
non-empty: the six kernel dimensions (recorded-time, observed-time,
valid-time, scope, subject, memory-class) are builtin and present
regardless of config. ``dimensions:`` in ``athenaeum.yaml`` is a list of
ADDITIONAL, deployment-declared dimensions (``engagement``, ``repo``,
``maturity``,...) layered on top; a fresh install with no ``dimensions:``
key gets the kernel-only registry, and ``athenaeum run`` behaves
unchanged either way (nothing in the librarian pipeline consults a
deployment dimension's ``applies_to`` unless one is declared).

No env var: ``dimensions:`` is a structural block (a list of typed
entries), not a scalar knob — there is no single value an env override
could sensibly replace. Raises
`athenaeum.dimensions.DimensionRegistryError` on a malformed
entry (unknown ``kind``/``null_means``/``state``, non-kebab-case name,
duplicate name, an ``enum`` kind missing ``values``, or a name colliding
with a kernel dimension) — a mis-declared dimension is a real config
error, not something to silently drop, matching ``resolve_screening``'s
fail-loud posture for a structural (not scalar) knob.

### `resolve_google_contact_keys`

- **YAML path:** `athenaeum.yaml`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `[]`
- **Precedence:** `athenaeum.yaml` > code default

Resolve extra Google-contact dedup join-key field-names.

The dedupe merge always treats the generic ``google_contact`` frontmatter
field as a join/merge key. Some operators carry the same Google contact id
under additional namespace-specific field names (e.g. a separate field per
Google Workspace account). Those EXTRA field names are operator-specific
and must never be hardcoded in shipped source -- they come entirely from
``athenaeum.yaml``:
```
dedupe:
  google_contact_keys:
    - google_contact_<namespace>
```

Returns the configured list of extra field names (the base
``google_contact`` key is implicit and not included here). Returns an
empty list when unset -- a fresh install dedups on the generic
``google_contact`` key only, with no personal namespace literal in source.
No seed in ``_DEFAULTS``.

### `resolve_min_merge_confidence`

- **YAML path:** `athenaeum.yaml`
- **Environment variable:** `ATHENAEUM_MIN_MERGE_CONFIDENCE`
- **CLI flag:** —
- **Default:** `0.0`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the resolver merge-proposal confidence floor.

A second, opt-in gate on the merge-proposal path: a proposal whose resolver
confidence is strictly below this floor is suppressed before it reaches
``_pending_merges.md``. Complements `resolve_max_merge_sources` — the
size cap catches the degenerate over-clusters by shape, this lets an operator
additionally keep low-confidence small merges out of the human queue.

DEFAULT 0.0 (OFF): a baked-in confidence floor is a corpus-specific product
call (what confidence a human wants to review is deployment-dependent), so it
ships disabled and is opt-in via ``athenaeum.yaml`` — mirroring
`resolve_min_cluster_cohesion`. Env ``ATHENAEUM_MIN_MERGE_CONFIDENCE`` >
yaml ``librarian.min_merge_confidence`` > this default. No seed in
``_DEFAULTS``. (M1): a parsed env value is authoritative
over yaml — ``ATHENAEUM_MIN_MERGE_CONFIDENCE=0`` (or negative) disables the
floor even when yaml sets one, instead of silently falling through. A
malformed env value logs a WARNING (M2, via `_env_number`) and falls
back to yaml. A ``bool`` / non-numeric / ``<= 0`` yaml value falls through
to 0.0 (off).

### `resolve_model_rates`

- **YAML path:** `athenaeum.yaml`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `{}`
- **Precedence:** `athenaeum.yaml` > code default

Resolve the per-MTok pricing table from ``athenaeum.yaml``'s ``pricing:``
section.

``pricing.<prefix>: [input_usd_per_mtok, output_usd_per_mtok]`` — the SAME
longest-prefix-match convention `athenaeum.models._rates_for_model`
already uses for the code-default table, so a dated model id
(``claude-sonnet-4-6-20260301``) still resolves via the shortest prefix
that matches. Only a yaml layer exists for this knob — no env-var
override (a whole pricing TABLE is not the single scalar the existing
``ATHENAEUM_*`` env convention fits) and, deliberately, no per-prefix code
default merged in HERE: this function returns EXACTLY what
``athenaeum.yaml`` says, nothing more. See
`athenaeum.models.configure_model_rates` for what an empty return
does at the call site (falls back to the code-default table WHOLESALE,
not per missing prefix) and the issue's "Design decision" for why a
per-prefix merge (yaml overlaying the code table, which stays the floor
for anything yaml omits) was rejected: an omission in yaml would keep
silently reading the code default — the invisible second source of truth
the startup preflight (`preflight_model_rates`) exists to kill
for a model a run actually resolves to.

Schema contract ("Schema note for the implementer"): **one rate per
prefix, no mode dimension.** A prefix key cannot express a time-boxed
promo rate (Sonnet 5's introductory $2/$10 through 2026-08-31 — Occam
decision 2026-07-31, deliberately not encoded: a prefix-keyed rate cannot
expire, so a promo would go silently wrong the day it ends) or a
per-request-mode rate (Opus 5's ``speed: "fast"`` $10/$50 — athenaeum does
not use fast mode). Both are explicitly out of scope for; if either is
ever needed, it is a schema change here (e.g. a mode-keyed sub-block), not
a workaround layered on top of this function.

Malformed entries WARN and are REJECTED (excluded from the returned
dict — treated as unset for that prefix), mirroring
`athenaeum.provider.resolve_max_tokens`'s malformed-override
convention rather than inventing a new one: wrong arity (not exactly 2
elements), a non-numeric element, a ``bool`` element (``bool`` is an
``int`` subclass — ``[true, false]`` must not silently become
``[1.0, 0.0]``), or a negative rate.

### `resolve_owner`

- **YAML path:** `athenaeum.yaml`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `None`
- **Precedence:** `athenaeum.yaml` > code default

Resolve the workspace owner identity from config.

The owner is the single canonical person the knowledge base belongs to.
Athenaeum ships to PyPI, so the owner identity must NEVER be hardcoded in
source — it comes entirely from ``athenaeum.yaml``:
```
owner:
  uid: <owner-person-uid>                # canonical owner person UID
  google_contact: people/<contact-id>    # owner Google contact id
  aliases: ["<your_user_handle>", ...]   # optional name/handle aliases
```

Aliases used for name matching must be FULL names (≥2 tokens); a
single-token alias is ignored for name matching so it cannot absorb
every stranger who shares that one name.

Returns a normalized dict ``{"uid", "google_contact", "aliases"}`` when at
least one usable field is set, else ``None``. A ``None`` return makes every
owner-aware behavior (auto-bind, owner join keys, ``user_*`` routing) inert
so the package works for any user with no owner configured. No default is
seeded into ``_DEFAULTS`` — an unset owner is genuinely empty.

### `resolve_pull_before_run`

- **YAML path:** `athenaeum.yaml`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `False`
- **Precedence:** `athenaeum.yaml` > code default

Resolve the pre-run ``git pull`` opt-in.

Symmetric to `resolve_push_after_run`: with
``librarian.pull_before_run: true`` (or the ``athenaeum run --pull`` CLI
override), the librarian invokes ``git pull --ff-only --autostash`` on
the knowledge repo BEFORE the run starts, so the run compiles against
origin's latest instead of a possibly-stale local checkout. Default OFF:
a fresh install must never side-effect an operator's git remote, and
athenaeum itself handles no credentials — pulls (like pushes) rely
entirely on the operator's ambient git auth (credential helper / SSH).

There is no shipped nightly cron wrapper in this repo, so pull and push
both stay independently opt-in via yaml/CLI rather than being bundled
into an assumed scheduler script. An operator wanting full bidirectional
sync sets both ``pull_before_run: true`` and ``push_after_run: true`` in
``athenaeum.yaml``. Non-bool yaml values fall through to the default
(off).

## `erasure`

### `resolve_retention_pack_selection`

- **YAML path:** `erasure.retention_pack`
- **Environment variable:** `ATHENAEUM_RETENTION_PACK`
- **CLI flag:** —
- **Default:** `'us-default'`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve which retention pack is ACTIVE (AC9).

Precedence: env ``ATHENAEUM_RETENTION_PACK`` > yaml ``erasure.retention_pack``
> default ``"us-default"``. This is the SELECTION axis only — it names
which pack (of `athenaeum.erasure.available_retention_packs`'s
result) governs; the pack's own rule table is a separate axis
(`resolve_retention_pack_overrides`), mirroring how
`resolve_sensitivity_routing` keeps "is a class routed" separate
from `resolve_sensitivity_classes`' "what does the class contain."
An empty/whitespace-only override at either tier is treated as unset.

### `resolve_retention_pack_overrides`

- **YAML path:** `erasure.retention_packs`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `{}`
- **Precedence:** `athenaeum.yaml` > code default

Resolve ``erasure.retention_packs.<name>`` operator-authored pack overrides/additions.

Returns the RAW (still-unvalidated) per-pack mapping dicts keyed by pack
name — `athenaeum.erasure.available_retention_packs` validates each
and builds the `athenaeum.erasure.RetentionPack` objects, exactly
mirroring `resolve_sensitivity_classes`'s split with
`athenaeum.sensitivity.available_classes`. Returns an EMPTY dict
when unset — the two packaged packs (``us-default``, ``eu-gdpr``) are
still resolved regardless, from ``src/athenaeum/retention_packs/*.yaml``,
not from this function — so this resolver is NOT seeded in
``_DEFAULTS`` ('s rule: seeding here would make the
packaged-file default unreachable). Non-string keys and non-mapping
values are dropped defensively; a malformed entry surfaces loudly later,
at pack-build time, with the pack name in the message.

## `librarian`

### `resolve_audit_sample_rate_t1_rejects`

- **YAML path:** `librarian.audit_sample_rate_t1_rejects`
- **Environment variable:** `ATHENAEUM_AUDIT_SAMPLE_RATE_T1_REJECTS`
- **CLI flag:** —
- **Default:** `0.075`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the share of T1 rejects sampled for human audit.

The calibration loop catches false-REJECTS: a random share of T1's reject
verdicts is surfaced for a human to confirm or overturn. Env
``ATHENAEUM_AUDIT_SAMPLE_RATE_T1_REJECTS`` > yaml
``librarian.audit_sample_rate_t1_rejects`` > default ``0.075`` (7.5%,
the midpoint of the settled 5-10% band). Clamped to ``[0.0, 1.0]``.

### `resolve_audit_sample_rate_t2_approvals`

- **YAML path:** `librarian.audit_sample_rate_t2_approvals`
- **Environment variable:** `ATHENAEUM_AUDIT_SAMPLE_RATE_T2_APPROVALS`
- **CLI flag:** —
- **Default:** `0.075`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the share of T2 approvals sampled for human audit.

The calibration loop catches false-APPROVES: a random share of T2's
approve verdicts is surfaced for a human to confirm or overturn. Env
``ATHENAEUM_AUDIT_SAMPLE_RATE_T2_APPROVALS`` > yaml
``librarian.audit_sample_rate_t2_approvals`` > default ``0.075`` (7.5%,
the midpoint of the settled 5-10% band). Clamped to ``[0.0, 1.0]``.

### `resolve_authority_grant_implications`

- **YAML path:** `librarian.authority_grant_implications`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `{}`
- **Precedence:** `athenaeum.yaml` > code default

Resolve the grant-implication map used to order asserter authority.

Authority here is a PARTIAL ORDER over the grants an asserter
declares, never a chain of ranks. This map is what makes the order
non-trivial: it declares which grants IMPLY which others, so that an
asserter holding ``admin`` compares as strictly greater than one holding
only ``reader`` without either having to enumerate the other's grants.

```yaml
librarian:
  authority_grant_implications:
    admin: [editor]
    editor: [reader]
```

Yaml only -- an implication graph is not an emergency override and has no
sane env-var encoding. Default ``{}``: with no declared implications the
order degenerates to plain set inclusion over declared grants, which is
still a correct partial order (just a flatter one). Non-string keys,
non-list values, and non-string members are dropped defensively;
`athenaeum.asserter_authority.grant_closure` is cycle-safe, so a
malformed cyclic map cannot hang a run.

### `resolve_authority_manifest_path`

- **YAML path:** `librarian.authority_manifest_path`
- **Environment variable:** `ATHENAEUM_AUTHORITY_MANIFEST`
- **CLI flag:** —
- **Default:** `<knowledge_root>/authority-manifest.yaml`
- **Precedence:** environment variable > `athenaeum.yaml` (relative to `knowledge_root`) > code default

Resolve the authority manifest path.

The authority manifest maps authoritative LIVE sources (skill files, code
paths, config) to the topics/slugs they own, so the librarian can detect a
memory that merely duplicates content a live source already owns. Mirrors
the module's standard precedence (env > yaml > default), matching
`resolve_spend_ledger_path`'s "explicit path override" shape:

- ``ATHENAEUM_AUTHORITY_MANIFEST`` env — explicit path (highest).
- ``librarian.authority_manifest_path`` yaml — relative values are
 resolved against ``knowledge_root``; absolute values pass through.
- default: ``<knowledge_root>/authority-manifest.yaml`` — a sibling of
 ``athenaeum.yaml`` at the knowledge root, following the same "config
 lives at the root of the knowledge tree" convention.

Does not check for existence — callers (`athenaeum.authority.
load_authority_manifest`) handle a missing file as "no manifest configured"
(empty, not an error). No seed in ``_DEFAULTS`` so this code
default stays reachable.

### `resolve_auto_supersession_enabled`

- **YAML path:** `librarian.auto_supersession_enabled`
- **Environment variable:** `ATHENAEUM_AUTO_SUPERSESSION_ENABLED`
- **CLI flag:** —
- **Default:** `False`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the auto-supersession opt-in. DEFAULT OFF.

Auto-supersession RETIRES a claim -- the one genuinely destructive effect
in the comparator's verdict set -- so it ships behind its own switch
*inside* the already-off `resolve_comparator_enabled` gate rather
than riding on it. An operator who wants the comparator's verdicts
without any automatic retirement turns this off and every contradiction
routes to the decision queue instead.

Env ``ATHENAEUM_AUTO_SUPERSESSION_ENABLED``
(``1``/``true``/``yes``/``on``, case-insensitive) > yaml
``librarian.auto_supersession_enabled`` > default ``False``. No seed in
``_DEFAULTS``.

### `resolve_batch_lease_seconds`

- **YAML path:** `librarian.batch_lease_seconds`
- **Environment variable:** `ATHENAEUM_BATCH_LEASE_SECONDS`
- **CLI flag:** —
- **Default:** `259200.0`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the pending-batch raw-file lease in seconds (default 72h).

`athenaeum.batch_state.record_handle` leases the raw files a
submitted-but-uncollected batch was built from for this many seconds, and
the entity-phase claim loop skips a leased file until the lease expires —
so a submit-and-exit run cannot have its own intake rediscovered and
re-submitted, at full price, by the next run:
```
librarian:
  batch_lease_seconds: 259200   # seconds; <= 0 disables leasing
```

Precedence: ``ATHENAEUM_BATCH_LEASE_SECONDS`` env, then
``librarian.batch_lease_seconds`` yaml, then
`DEFAULT_BATCH_LEASE_SECONDS` (72h). ``bool`` and non-numeric values
fall through to the default. A value ``<= 0`` disables leasing entirely
(returns ``None``) — the explicit operator opt-out, matching the
``max_runtime`` escape-hatch convention and mirroring
`resolve_lock_break_stale_after`'s shape exactly.

### `resolve_comparator_enabled`

- **YAML path:** `librarian.comparator_enabled`
- **Environment variable:** `ATHENAEUM_COMPARATOR_ENABLED`
- **CLI flag:** —
- **Default:** `False`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the five-verdict comparator opt-in. DEFAULT OFF.

Gates the comparator subsystem (`athenaeum.comparator`): with this
off, nothing changes for any existing operator. As of 's
cut-over, this IS a live pipeline gate:
`athenaeum.wiki_dedupe.propose_wiki_page_merges` (called from
`athenaeum.librarian._run_wiki_dedup_phase` every run) checks this
first and returns immediately when it is off — the wiki-page dedup pass
is otherwise a no-op, old algorithm and new both, since the old
confidence/suppression-gate algorithm that pass used to run
unconditionally was DELETED (not merely branched around) as part of the
cut-over; there is exactly one implementation, gated by this one knob.
``athenaeum merges recompare`` (`athenaeum._cmd_merges`) remains
the other live reader, unchanged. Mirrors
`resolve_verdict_ledger_enabled`'s shape exactly: env
``ATHENAEUM_COMPARATOR_ENABLED`` (``1``/``true``/``yes``/``on``,
case-insensitive) > yaml ``librarian.comparator_enabled`` > default
``False``. No seed in ``_DEFAULTS``. Non-bool yaml
values and unrecognized env strings fall through to off.

### `resolve_compatible_recheck_days`

- **YAML path:** `librarian.compatible_recheck_days`
- **Environment variable:** `ATHENAEUM_COMPATIBLE_RECHECK_DAYS`
- **CLI flag:** —
- **Default:** `183`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the ``compatible`` TTL re-check age, in days.

A ``compatible`` content relation is the one verdict that says "these two
coexist" WITHOUT a coordinate separating them, so it is the one most
likely to be falsified by later writes: the subject drifts and the two
pages start answering the same question. asks for a TTL
re-check "for high-write subjects -- default: re-compare after 6 months
or 20 content-adjacent writes"; this is the 6 months, as ``183`` days.
Either trigger firing is enough (see
`resolve_compatible_recheck_writes`).

Env ``ATHENAEUM_COMPATIBLE_RECHECK_DAYS`` > yaml
``librarian.compatible_recheck_days`` > ``183``.

### `resolve_compatible_recheck_writes`

- **YAML path:** `librarian.compatible_recheck_writes`
- **Environment variable:** `ATHENAEUM_COMPATIBLE_RECHECK_WRITES`
- **CLI flag:** —
- **Default:** `20`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the ``compatible`` TTL re-check write count.

The "or 20 content-adjacent writes" half of the TTL above: once either
side of a ``compatible`` pair has accumulated this many content-changing
writes since the verdict was recorded, the pair is re-compared even if
`resolve_compatible_recheck_days` has not elapsed.

Env ``ATHENAEUM_COMPATIBLE_RECHECK_WRITES`` > yaml
``librarian.compatible_recheck_writes`` > ``20``.

### `resolve_corrections_fields`

- **YAML path:** `librarian.corrections.fields`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `{}`
- **Precedence:** `athenaeum.yaml` > code default

Resolve ``librarian.corrections.fields`` — the attribute allowlist
that bounds what a correction may write CHEAPLY at tier 0
(`docs/design/field-corrections.md` §6.3).

Maps an attribute name to ``{"shape": "scalar"|"list", "writers":
[...], "monotone": bool}``. **Empty by default** (§10.3) — with no
config, no attribute is allowlisted, so every correction takes the
reasoning-tier fallthrough (§8) and nothing is written cheaply. A fresh
deployment cannot have its wiki written by a mechanical writer until an
operator opts in per-attribute. Malformed entries (non-string attribute
name, non-dict definition) are dropped defensively rather than raised —
a config typo degrades to "this attribute reasons instead of writing
cheaply," never a crash.

### `resolve_corrections_max_batch_bytes`

- **YAML path:** `librarian.corrections.max_batch_bytes`
- **Environment variable:** `ATHENAEUM_CORRECTIONS_MAX_BATCH_BYTES`
- **CLI flag:** —
- **Default:** `33554432`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

§10.2 ``librarian.corrections.max_batch_bytes`` (default 32 MiB).

Caps the on-disk size of a single correction batch file
`athenaeum.corrections.run_correction_phase` will process in one
pass; an oversize batch is carried over rather than read in full.

### `resolve_corrections_max_escalations_per_run`

- **YAML path:** `librarian.corrections.max_escalations_per_run`
- **Environment variable:** `ATHENAEUM_CORRECTIONS_MAX_ESCALATIONS_PER_RUN`
- **CLI flag:** —
- **Default:** `50`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

§10.2 ``librarian.corrections.max_escalations_per_run`` (default 50).

Caps how many correction conflicts the librarian's escalation phase may
push onto ``_pending_questions.md`` in a single run, tracked per
(submitter, field) so the operator sees which target tripped the flood
guard rather than an undifferentiated count.

### `resolve_corrections_max_records_per_batch`

- **YAML path:** `librarian.corrections.max_records_per_batch`
- **Environment variable:** `ATHENAEUM_CORRECTIONS_MAX_RECORDS_PER_BATCH`
- **CLI flag:** —
- **Default:** `5000`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

§10.2 ``librarian.corrections.max_records_per_batch`` (default 5,000).

Caps how many records `athenaeum.corrections.run_correction_phase`
reads out of a single correction batch file in one pass; the remainder
is carried over to a later run rather than processed in the same pass.

### `resolve_corrections_max_records_per_run`

- **YAML path:** `librarian.corrections.max_records_per_run`
- **Environment variable:** `ATHENAEUM_CORRECTIONS_MAX_RECORDS_PER_RUN`
- **CLI flag:** —
- **Default:** `50000`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

§10.2 ``librarian.corrections.max_records_per_run`` (default 50,000).

Caps how many correction records `athenaeum.corrections.run_correction_phase`
applies across ALL batch files in one run; once the cap is hit, every
remaining batch is left untouched and carried over to the next run.

### `resolve_corrections_runtime_share`

- **YAML path:** `librarian.corrections.runtime_share`
- **Environment variable:** `ATHENAEUM_CORRECTIONS_RUNTIME_SHARE`
- **CLI flag:** —
- **Default:** `0.05`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

§10.2 ``librarian.corrections.runtime_share`` (default 0.05).

Mirrors `athenaeum.librarian.librarian_entity_runtime_share`'s
coercion rules: only ``0 < share < 1`` reserves anything; a bool
(int-subclass guard), non-numeric, or out-of-range value falls back to
the default rather than disabling the reserve — unlike the entity
share, an operator who sets this key at all almost certainly wants SOME
reserve, so a malformed value should not silently zero it out.

### `resolve_corrections_schema_slots`

- **YAML path:** `librarian.corrections.schema_slots`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `{}`
- **Precedence:** `athenaeum.yaml` > code default

Resolve ``librarian.corrections.schema_slots`` — the §7.2 schema-
evolution table (`docs/design/field-corrections.md` §7.2) for attributes that
ARE on the `resolve_corrections_fields` allowlist but that the
deployment's schema has no dedicated slot for.

Maps an attribute name to one of three shapes deciding which §7.2
disposition an allowlisted-but-slot-less attribute takes:

- ``{"alias_of": "<other-field>"}`` — a slot exists under a different
 name; the write is transparently redirected there.
- ``{"propose_amendment": true}`` — no slot, and the deployment wants a
 human-decision schema-amendment proposal (``held-schema-proposal``,
 recorded on `_pending_questions.md`).
- ``{"prose": true}`` — no slot, one-off; recorded as body prose on the
 entity (``recorded-as-prose``).

An allowlisted attribute with NO entry here writes directly as ordinary
frontmatter (schemas.py's per-type models already tolerate unknown keys
via ``extra="allow"``, the same mechanism source-handle keys use) — §7.2
only fires when the deployment explicitly asks for non-default routing.
Empty by default, same rationale as `resolve_corrections_sensitive_fields`.

### `resolve_corrections_sensitive_fields`

- **YAML path:** `librarian.corrections.sensitive_fields`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `{}`
- **Precedence:** `athenaeum.yaml` > code default

Resolve ``librarian.corrections.sensitive_fields`` — the §7.1
sensitivity-routing table (`docs/design/field-corrections.md` §7.1).

Maps an attribute name to a `athenaeum.storage` entity-CLASS name
(resolved through the existing ``storage.mapping`` adapter layer,
— reused rather than reinvented) that a fact bearing on that attribute
is routed to, REGARDLESS of the destination a correction named. Empty by
default: **sensitivity classification is deployment configuration**,
never shipped in this repo (docs/design/field-corrections.md §7.1,
out-of-scope list).

### `resolve_decisions_max_sources_per_merge`

- **YAML path:** `librarian.decisions_max_sources_per_merge`
- **Environment variable:** `ATHENAEUM_DECISIONS_MAX_SOURCES_PER_MERGE`
- **CLI flag:** —
- **Default:** `20`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the decisions-view per-merge source fan-out cap.

The ``decisions`` view (`athenaeum.decisions.merge_to_decision`)
rendered EVERY source of a pending merge with no cap — a merge proposal
with a very large source list (or the pathological over-cluster shape
 targets on the write path) would blow out a single decision item's
payload. This bounds the rendered source list to this many entries, with
the remainder surfaced as an accurate ``sources_omitted`` count rather
than silently dropped.

Precedence: ``ATHENAEUM_DECISIONS_MAX_SOURCES_PER_MERGE`` env > yaml
``librarian.decisions_max_sources_per_merge`` > ``20``. See
`_resolve_positive_int_knob` for the coercion contract (``bool`` /
non-int / ``<= 0`` values fall through to the default).

### `resolve_decisions_page_limit`

- **YAML path:** `librarian.decisions_page_limit`
- **Environment variable:** `ATHENAEUM_DECISIONS_PAGE_LIMIT`
- **CLI flag:** —
- **Default:** `50`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the decisions-view MCP read-path page size.

The ``list_pending_decisions`` and ``list_pending_questions`` MCP tools
returned every pending item in one unbounded JSON array — against the
live corpus that was 11,355,998 bytes across 8,632 items, which breaks
the MCP stdio transport (``Connection closed``). This bounds the number
of top-level items those tools return per call, with the remainder
reachable via ``offset``/``limit`` paging. It is the sibling cap to
`resolve_decisions_max_sources_per_merge`, which
bounds the number of sources rendered WITHIN a single merge item; this
one bounds the number of items in the top-level list.

Precedence: ``ATHENAEUM_DECISIONS_PAGE_LIMIT`` env > yaml
``librarian.decisions_page_limit`` > ``50``. See
`_resolve_positive_int_knob` for the coercion contract (``bool`` /
non-int / ``<= 0`` values fall through to the default).

### `resolve_delta_enabled`

- **YAML path:** `librarian.delta`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `True`
- **Precedence:** `athenaeum.yaml` > code default

Resolve the delta-scoped-compile opt-in (PR2) from ``librarian.delta``.

When TRUE (the default), the deterministic ``client=None`` compile path
(session_end / ingest tier0) may scope the cluster + merge passes to only
the changed files and their affected clusters instead of re-clustering and
re-merging the whole auto-memory corpus. This is a pure SPEED optimization
that is proven byte-equivalent to the whole-corpus path
(``tests/test_delta_compile_equivalence.py``); the nightly LLM ``run`` (a
live client with cross-scope contradiction detection) always stays
whole-corpus regardless of this flag. Set ``librarian.delta.enabled: false``
to force the whole-corpus path everywhere. ``bool`` yaml values are honored;
anything else falls through to the TRUE default.

### `resolve_live_delta_enabled`

- **YAML path:** `librarian.delta.live_client`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `True`
- **Precedence:** `athenaeum.yaml` > code default

Resolve the live-client delta-scoped-compile opt-in from
``librarian.delta.live_client`` (slice D of).

When TRUE (the default), the nightly LLM ``run`` (a live client) MAY also
take the delta-scoped cluster + merge path — previously (PR2) delta
was gated to the deterministic ``client is None`` path ONLY (fallback
trigger D5). The live-client delta path is additionally gated by
``full_compile_due`` (the periodic whole-corpus reconciliation, see
`athenaeum.config.resolve_full_compile_every_days`) regardless of
this flag — see `athenaeum.librarian._compile_auto_memory`. Set
``librarian.delta.live_client: false`` to keep the nightly run
whole-corpus-only (the pre- behaviour) even when a live client is
present. ``bool`` yaml values are honored; anything else falls through to
the TRUE default.

### `resolve_delta_max_affected_clusters`

- **YAML path:** `librarian.delta.max_affected_clusters`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `8`
- **Precedence:** `athenaeum.yaml` > code default

Resolve the delta closure's affected-cluster cap (PR2, default 8).

When the change-closure fixpoint pulls in MORE than this many clusters, the
delta is no longer a small local update — the run falls back to a full
whole-corpus compile (fallback trigger D2) rather than churning most of the
corpus through the "delta" path. ``librarian.delta.max_affected_clusters``;
``bool`` and non-positive / non-int values fall through to the default.

### `resolve_delta_max_affected_members`

- **YAML path:** `librarian.delta.max_affected_members`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `200`
- **Precedence:** `athenaeum.yaml` > code default

Resolve the delta closure's pooled-member cap (PR2, default 200).

Companion to `resolve_delta_max_affected_clusters`: when the pool of
files entering the delta re-cluster exceeds this many members, fall back to
a full compile (fallback trigger D2). Bounds the worst-case
re-cluster cost so a pathological closure can never do MORE work than a full
run. ``librarian.delta.max_affected_members``; ``bool`` and non-positive /
non-int values fall through to the default.

### `resolve_dimension_registry_epoch`

- **YAML path:** `librarian.dimensions_registry_epoch`
- **Environment variable:** `ATHENAEUM_DIMENSION_REGISTRY_EPOCH`
- **CLI flag:** —
- **Default:** `1`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the dimension-registry epoch, for the verdict-ledger basis.

Bump ``librarian.dimensions_registry_epoch`` in ``athenaeum.yaml``
whenever a dimension's definition changes in a way that should
invalidate verdicts justified by the old definition (AC:
"Both must appear in the ledger basis of any verdict written after this
issue" — see `athenaeum.verdicts.Basis`). Namespaced under
``librarian.*`` alongside its sibling knobs
(``verdict_ledger_enabled``, ``verdict_epoch_batch_interval_days``), same
helper/coercion contract. Precedence:
``ATHENAEUM_DIMENSION_REGISTRY_EPOCH`` env > yaml
``librarian.dimensions_registry_epoch`` > ``1``. No seed in ``_DEFAULTS``

### `resolve_dimension_tree_epoch`

- **YAML path:** `librarian.dimensions_tree_epoch`
- **Environment variable:** `ATHENAEUM_DIMENSION_TREE_EPOCH`
- **CLI flag:** —
- **Default:** `1`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the scope-tree epoch, for the verdict-ledger basis.

Bump ``librarian.dimensions_tree_epoch`` in ``athenaeum.yaml`` on a
scope-tree reorg (renamed subtree) so this's targeted stale-marking
(`athenaeum.verdicts.select_stale_for_tree_epoch_bump`) can
invalidate exactly the verdicts whose basis coordinates touch the
renamed subtree. Precedence: ``ATHENAEUM_DIMENSION_TREE_EPOCH`` env >
yaml ``librarian.dimensions_tree_epoch`` > ``1``. No seed in ``_DEFAULTS``

### `resolve_drain_warn_days`

- **YAML path:** `librarian.drain_warn_days`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `3`
- **Precedence:** `athenaeum.yaml` > code default

Resolve the backlog-drain ETA warning threshold in days (default 3) from ``librarian.drain_warn_days``.

At the end of any run that leaves raw intake undrained (and in ``athenaeum
status``), the backlog-drain advisor (`athenaeum.drain_advisor.build_advisory`)
projects time-to-drain from observed throughput and emits a WARNING — naming
the one-command ``athenaeum drain`` remedy — only when that projection
EXCEEDS this many days. Below the threshold the run stays silent. Lives
directly under ``librarian`` (a run-cadence advisory, not a delta/merge
knob). ``bool`` and non-positive / non-int values fall through to the
default.

### `resolve_ephemeral_scopes`

- **YAML path:** `librarian.ephemeral_scopes`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `['*hestia-routine*', '*var-folders*', '*private-tmp*', '*-cctest-*']`
- **Precedence:** `athenaeum.yaml` > code default

Resolve glob patterns for throwaway auto-memory scope dirs.

Returns the operator's ``librarian.ephemeral_scopes`` list when set
(authoritative -- it REPLACES the defaults so an operator owns the full
set), else the built-in `_DEFAULT_EPHEMERAL_SCOPES`. A present-but-
empty list disables scope-based ephemeral classification entirely.

### `resolve_full_compile_every_days`

- **YAML path:** `librarian.full_compile_every_days`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `7`
- **Precedence:** `athenaeum.yaml` > code default

Resolve the periodic whole-corpus reconciliation cadence (default 7 days) from ``librarian.full_compile_every_days``.

The live-client delta path is a corpus-consistency optimization
over the auto-memory C2-C4 compile; this cadence is its backstop. When the
last successful whole-corpus compile (`athenaeum.librarian.
_load_full_compile_stamp`) is at least this many days old — or there has
never been one — the next run forces a whole-corpus compile regardless of
the delta gate, re-entering any TTL-decayed auto-suppressions and
resolving drift a delta pass could not see. Note this key lives directly
under ``librarian``, NOT under ``librarian.delta`` (it also bounds the
non-live delta path indirectly via the stamp, but is a run-cadence
setting, not a delta-mechanism setting). ``bool`` and non-positive /
non-int values fall through to the default.

### `resolve_heartbeat_interval`

- **YAML path:** `librarian.heartbeat_interval`
- **Environment variable:** `ATHENAEUM_HEARTBEAT_INTERVAL`
- **CLI flag:** —
- **Default:** `60.0`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the progress-heartbeat emit interval (seconds).

The dark-zone phases (the entity phase,; T3 merge; C4
contradiction detection; the wiki-dedup pass; the
re-resolve pass) emit a periodic ``librarian-heartbeat`` progress line via
`athenaeum.progress.PhaseHeartbeat`
so a stall in one of these phases is visible in the log and detectable by
a watchdog. This resolves how often (in seconds) a slow/wedged phase
emits a tick:
```
librarian:
  heartbeat_interval: 60   # seconds; <= 0 = emit every tick
```

Precedence: ``ATHENAEUM_HEARTBEAT_INTERVAL`` env, then
``librarian.heartbeat_interval`` yaml, then ``60.0`` (default). ``bool``
and non-numeric values fall through to the default. A value ``<= 0``
means "emit every tick" and returns ``0.0`` (NOT the default — 0 is a
valid, distinct configuration, unlike ``resolve_lock_timeout``'s
fail-fast collapse).

### `resolve_ingestion_gate_enabled`

- **YAML path:** `librarian.ingestion_gate_enabled`
- **Environment variable:** `ATHENAEUM_INGESTION_GATE_ENABLED`
- **CLI flag:** —
- **Default:** `False`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve whether the ingestion gate is enforced.

OFF by default — this is a new, additive gate (part 3 of) that can
BLOCK ingestion when push-metrics precision instrumentation looks
unhealthy, so it must not change behavior for any existing operator
until they opt in (DoD: "lands dark behind a documented config key
defaulting to off"). Precedence: ``ATHENAEUM_INGESTION_GATE_ENABLED`` env
> ``librarian.ingestion_gate_enabled`` yaml > ``False``. Any env value
other than a falsey token (``0`` / ``false`` / ``no`` / ``off``,
case-insensitive) is truthy; a non-bool yaml value falls through to the
default. No seed in ``_DEFAULTS`` — mirrors
`resolve_push_metrics_enabled`'s shape, inverted default.

### `resolve_intake_runtime_floor`

- **YAML path:** `librarian.intake_runtime_floor`
- **Environment variable:** `ATHENAEUM_INTAKE_RUNTIME_FLOOR`
- **CLI flag:** —
- **Default:** `0.0`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve ``librarian.intake_runtime_floor``.

Reserves a MINIMUM share of ``max_runtime`` for the intake path that
feeds C4 (auto-memory C2 cluster / C3 merge / C4 contradiction-detect /
resolve) — the phase `athenaeum.librarian.librarian_entity_runtime_share`
 *caps* the entity phase against, but never itself
*guarantees* anything to the phases after it. needs an
honest per-contract LLM schema-mismatch rate and cannot compute one: the
resolution contract had 7 observations because the resolver made ~1 call
on the 2026-08-06 run, and the entity phase's wall-clock overrun (93.6%
of a 3944s window on 3 files) is why. This floor is the lever an operator
can arm to reserve intake a guaranteed minimum of the SAME wall-clock
window ``entity_runtime_share`` already caps the entity phase against —
`athenaeum.librarian._arm_run_deadline` combines the two by taking
the EARLIER (tighter) of the two candidate entity deadlines, so whichever
constraint binds actually wins.

**Unit (deliberate choice):** a fraction of ``max_runtime``
WALL-CLOCK, mirroring ``entity_runtime_share`` exactly — not an LLM-call
count, even though calls are the resource ultimately counts.
The nightly window itself is wall-clock, the motivating
data (entity consuming 93.6% of wall-clock while nowhere near
``max_api_calls``) is a wall-clock-shaped failure, and the entity phase
already stops independently on the run-level call ceiling
(``ctx.usage.api_calls >= ctx.max_api_calls``) regardless of this floor —
a calls-based floor would duplicate a cap that already exists. What nothing
guarantees today is that the entity phase leaves intake any WALL-CLOCK
TIME to spend its own calls in; that is exactly what this floor reserves.

DEFAULT 0.0 (OFF, AC4): arming this needs a value
chosen against measured figures ('s own review) — an operator
decision, out of scope for this issue. No seed in ``_DEFAULTS``
so the code default stays reachable. With the key unset, phase scheduling
is byte-for-byte identical to before this issue.

Only ``0 < floor < 1`` reserves anything (AC6: a non-positive or malformed
value falls through to disabled, matching `resolve_max_merge_sources`'s
own "0 disables" convention — env authoritative including a parsed zero or
negative value, via `_env_number`, which WARNs on a genuinely
malformed value rather than swallowing it silently). AC7: a floor ``>= 1.0``
(reserving the WHOLE window or more) is REFUSED, not clamped — it falls
through to disabled exactly like any other out-of-range value, mirroring
`athenaeum.librarian.librarian_entity_runtime_share`'s own
``0 < share < 1`` guard. Refusing (rather than clamping to some
less-than-1 ceiling) means a misconfigured floor can never invert the
starvation this issue fixes by starving the ENTITY phase instead — the
reserve simply does not arm, which is the same as never having set the
key.

Env ``ATHENAEUM_INTAKE_RUNTIME_FLOOR`` > yaml
``librarian.intake_runtime_floor`` > this default (``0.0``).

### `resolve_lock_break_stale_after`

- **YAML path:** `librarian.lock_break_stale_after`
- **Environment variable:** `ATHENAEUM_LOCK_BREAK_STALE_AFTER`
- **CLI flag:** —
- **Default:** `21600.0`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the auto-break staleness threshold in seconds (default 6h).

A contended `athenaeum.runlock.RunLock.acquire` auto-breaks a
wedged-but-alive holder's lock — WITHOUT requiring a human to pass
``--force`` — once the holder's heartbeat age exceeds this many seconds.
Six hours is comfortably above any healthy librarian run (and well below
the pathological multi-hour wedge seen in); operators can
lower it once the librarian reliably refreshes the lock heartbeat:
```
librarian:
  lock_break_stale_after: 21600   # seconds; <= 0 disables auto-break
```

Precedence: ``ATHENAEUM_LOCK_BREAK_STALE_AFTER`` env, then
``librarian.lock_break_stale_after`` yaml, then ``21600.0`` (6h). ``bool``
and non-numeric values fall through to the default. A value ``<= 0``
disables auto-break entirely (returns ``None``).

### `resolve_lock_heartbeat_interval`

- **YAML path:** `librarian.lock_heartbeat_interval`
- **Environment variable:** `ATHENAEUM_LOCK_HEARTBEAT_INTERVAL`
- **CLI flag:** —
- **Default:** `30.0`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the run lock's guaranteed background bump interval (default 30s).

`athenaeum.runlock.RunLock` starts a daemon thread the moment a
lock is acquired that refreshes the lockfile's ``heartbeat`` line every
this-many seconds, independent of whatever the caller's own run loop is
doing — see that module's "Staleness contract" docstring section for the
full reasoning (this closes the gap where the ONLY bumps came from
caller-driven phase/file-boundary ticks, which could go tens of minutes
between bumps on one long phase even while fully healthy):
```
librarian:
  lock_heartbeat_interval: 30   # seconds
```

Precedence: ``ATHENAEUM_LOCK_HEARTBEAT_INTERVAL`` env, then
``librarian.lock_heartbeat_interval`` yaml, then ``30.0`` — matching
`athenaeum.runlock.HEARTBEAT_INTERVAL_SECONDS`, duplicated here as a
literal rather than imported (this module stays L2 and does not import
``athenaeum.runlock`` at module or function scope, mirroring how
``resolve_lock_break_stale_after``/``resolve_lock_warn_stale_after`` above
hardcode their own defaults instead of reaching into ``runlock``). ``bool``
and non-numeric values fall through to the default. Unlike
``lock_break_stale_after``/``lock_warn_stale_after`` there is no
``<= 0``-disables convention here — a non-positive value falls back to
the default instead, so a stray ``0`` in config can never silently turn
the heartbeat thread off (a live lock should always get one).

### `resolve_lock_timeout`

- **YAML path:** `librarian.lock_timeout`
- **Environment variable:** `ATHENAEUM_LOCK_TIMEOUT`
- **CLI flag:** —
- **Default:** `0.0`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the default run-lock wait (seconds) from env > yaml > 0.

The single-machine run lock (`athenaeum.runlock`) fails fast by default
when another ``athenaeum run`` (or other mutating command) already holds
``<knowledge_root>/.athenaeum.lock``. Operators who prefer a mutating
command to WAIT rather than exit — e.g. a manual run overlapping the nightly
cron — can set a default block window:
```
librarian:
  lock_timeout: 300   # seconds; 0 = fail-fast (default)
```

Precedence: ``ATHENAEUM_LOCK_TIMEOUT`` env, then ``librarian.lock_timeout``
yaml, then ``0`` (fail-fast). The per-command ``--wait`` flag overrides this.
No seed in ``_DEFAULTS`` so the code default stays reachable. ``bool``
and non-numeric / negative values fall through to 0.0.

### `resolve_lock_warn_stale_after`

- **YAML path:** `librarian.lock_warn_stale_after`
- **Environment variable:** `ATHENAEUM_LOCK_WARN_STALE_AFTER`
- **CLI flag:** —
- **Default:** `7200.0`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the loud-warning staleness threshold in seconds (default 2h).

A contended `athenaeum.runlock.RunLock.acquire` logs a prominent
"likely wedged" warning naming the holder once its heartbeat age exceeds
this many seconds — independent of (and typically lower than) the
auto-break threshold, so an operator gets an early heads-up even when
auto-break has not yet fired:
```
librarian:
  lock_warn_stale_after: 7200   # seconds; <= 0 disables the warning
```

Precedence: ``ATHENAEUM_LOCK_WARN_STALE_AFTER`` env, then
``librarian.lock_warn_stale_after`` yaml, then ``7200.0`` (2h). ``bool``
and non-numeric values fall through to the default. A value ``<= 0``
disables the warning entirely (returns ``None``).

### `resolve_max_merge_sources`

- **YAML path:** `librarian.max_merge_sources`
- **Environment variable:** `ATHENAEUM_MAX_MERGE_SOURCES`
- **CLI flag:** —
- **Default:** `5`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the resolver merge-proposal source-count cap.

The resolver's merge-proposal path (``propose_merge`` → ``_pending_merges.md``)
had no size cap, so a degenerate over-cluster — 1,600+ source memories folded
into one proposed page at ~0.33 confidence — was emitted (and re-emitted every
run) to the human queue. A merge above this many sources is by definition not
the pairwise / small-group refinement a merge proposal is for, so it is
suppressed before it reaches ``_pending_merges.md`` (neither proposed nor
escalated as a pending question).

DEFAULT 5 (active) — tightened from 25 (settled design). A merge
PROPOSAL is a pairwise / small-group refinement; a fold of more than ~5
sources is not that shape, and complete-linkage means the members of
a genuine small merge are mutually similar, so 5 sits well inside the
legitimate-merge margin while excluding the observed 1,600-1,700-source
degenerates decisively. (The wider size-25 cap still governs the pooled
contradiction-cluster path via `athenaeum.cross_scope.resolve_cluster_size_cap`
— this cap is specifically the merge-PROPOSAL fan-in.)
Env ``ATHENAEUM_MAX_MERGE_SOURCES`` > yaml ``librarian.max_merge_sources`` >
this default; ``0`` (or negative) disables the cap. No seed in ``_DEFAULTS``
 so the code default stays reachable. ``bool`` and non-numeric yaml
values fall through to the default.

### `resolve_memory_tier_sweep_enabled`

- **YAML path:** `librarian.memory_tier_sweep_enabled`
- **Environment variable:** `ATHENAEUM_MEMORY_TIER_SWEEP_ENABLED`
- **CLI flag:** —
- **Default:** `False`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve whether the automatic memory-tier sweep runs.

OFF by default — a new, additive librarian phase
(`athenaeum.librarian._run_memory_tier_sweep_phase`) that can
rewrite a page's ``memory_tier:`` frontmatter field (demote hot -> warm,
promote warm -> hot; see `athenaeum.memory_tiers`), so it must not
change the nightly run's behavior for any existing operator until they
opt in (DoD: "lands dark behind a documented config key defaulting to
off"). Precedence: ``ATHENAEUM_MEMORY_TIER_SWEEP_ENABLED`` env >
``librarian.memory_tier_sweep_enabled`` yaml > ``False``. Any env value
other than a falsey token (``0`` / ``false`` / ``no`` / ``off``,
case-insensitive) is truthy; a non-bool yaml value falls through to the
default. No seed in ``_DEFAULTS`` — mirrors
`resolve_ingestion_gate_enabled`'s shape.

### `resolve_merge_body_preview_chars`

- **YAML path:** `librarian.merge_body_preview_chars`
- **Environment variable:** `ATHENAEUM_MERGE_BODY_PREVIEW_CHARS`
- **CLI flag:** —
- **Default:** `2000`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the ``list_pending_merges`` draft-body preview cap.

Complements the write-path suppression in `resolve_max_merge_sources`
: that gate keeps a degenerate over-cluster from ever reaching
``_pending_merges.md``, but a single legitimate-looking proposal can still
carry an oversized ``draft_merged_body`` (the withdrawn runaway that
prompted this issue had a ~878 KB draft). The raw MCP tool returned that
body in full, unbounded, on every ``list_pending_merges`` call — this caps
it to a bounded preview by default. The full body stays retrievable via
``list_pending_merges(full_body=True)`` for a caller that actually needs it
(e.g. immediately before ``resolve_merge`` writes it to disk).

Precedence: ``ATHENAEUM_MERGE_BODY_PREVIEW_CHARS`` env > yaml
``librarian.merge_body_preview_chars`` > ``2000``. See
`_resolve_positive_int_knob` for the coercion contract (``bool`` /
non-int / ``<= 0`` values fall through to the default).

### `resolve_merge_worthiness_gate_enabled`

- **YAML path:** `librarian.merge_worthiness_gate_enabled`
- **Environment variable:** `ATHENAEUM_MERGE_WORTHINESS_GATE_ENABLED`
- **CLI flag:** —
- **Default:** `False`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve ``librarian.merge_worthiness_gate_enabled``. DEFAULT OFF.

Gates the deterministic, zero-LLM merge-worthiness containment check in
`athenaeum.tiers.check_merge_worthiness_gate`: when armed, a
Tier-3 update whose raw file offers no fact absent from the target
entity's existing page is suppressed before the merge prompt is built
or any model call is made. Checked at the call site in
`athenaeum.tiers.tier3_derive_actions` (mirroring how
`athenaeum.merge.merge_clusters_to_wiki` gates the reasoning-tier
screen) so a disabled knob costs one bool call and nothing else.

Mirrors `resolve_reasoning_tier_auditing_enabled`'s precedence
contract exactly: env ``ATHENAEUM_MERGE_WORTHINESS_GATE_ENABLED``
(``1``/``true``/``yes``/``on``, case-insensitive) > yaml
``librarian.merge_worthiness_gate_enabled`` (bool only; non-bool falls
through) > default ``False``. No seed in ``_DEFAULTS``. Default OFF is
deliberate: a false suppression permanently destroys a fact (raw files
are unlinked after processing, with no re-derivation path), so the gate
stays opt-in until an operator turns it on — production merge behavior
is byte-identical to today until then.

### `resolve_min_cluster_cohesion`

- **YAML path:** `librarian.min_cluster_cohesion`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `0.0`
- **Precedence:** `athenaeum.yaml` > code default

Resolve the cluster-cohesion floor from ``librarian.min_cluster_cohesion``.

The cross-scope ``similarity`` clustering path over-clusters: single-linkage
chains a coherent source doc together with vaguely-similar operational
session-notes from many OTHER scopes into one LOW-COHESION blend page. The
floor lets the merge pass refuse to materialize such a cluster into a
durable ``wiki/auto-*.md`` page: a cluster whose ``cluster_centroid_score``
(mean intra-cluster cosine) is strictly BELOW this floor AND which spans at
least `resolve_min_cluster_cohesion_scopes` distinct origin scopes is
suppressed. Its raw members stay in place (not retired) for a coherent
cluster to pick up later.

DEFAULT 0.0 (OFF): athenaeum ships to PyPI, and the clean ~0.47 cohesion gap
is specific to one corpus -- a baked-in non-zero floor could suppress
legitimate clusters in a corpus with a different cohesion distribution.
Operators opt in via ``athenaeum.yaml``. No seed in ``_DEFAULTS`` so
the code default stays reachable. ``bool`` (an ``int`` subclass) and
non-numeric / negative yaml values fall through to 0.0 (off).

### `resolve_min_cluster_cohesion_scopes`

- **YAML path:** `librarian.min_cluster_cohesion_scopes`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `4`
- **Precedence:** `athenaeum.yaml` > code default

Resolve the distinct-origin-scope floor for the cohesion gate.

The cohesion floor (`resolve_min_cluster_cohesion`) only suppresses a
cluster that ALSO spans at least this many distinct ``origin_scopes`` -- the
cross-scope over-cluster signature. Gating on scope count too prevents
false-suppression of a low-cohesion SINGLE-scope cluster (legitimately
diverse intake from one project) or a small 2-3 scope coherent cluster.

DEFAULT 4: observed over-clusters span 8-17 origin scopes while legitimate
auto-memory pages span 1-3, so a floor of 4 sits in the clean margin. No
seed in ``_DEFAULTS``. ``bool`` and non-int / ``< 2`` yaml values
fall through to the default.

### `resolve_min_merge_mean_similarity`

- **YAML path:** `librarian.min_merge_mean_similarity`
- **Environment variable:** `ATHENAEUM_MIN_MERGE_MEAN_SIMILARITY`
- **CLI flag:** —
- **Default:** `0.6`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the merge-proposal mean-pairwise-similarity floor.

A merge proposal whose cluster mean pairwise cosine
(``cluster_centroid_score``) is strictly below this floor is suppressed
before it reaches ``_pending_merges.md``. This is the ACTIVE-by-default
cohesion gate the settled design calls for: unlike the corpus-specific
`resolve_min_cluster_cohesion` (which suppresses durable wiki pages
and so ships OFF), the merge-PROPOSAL path is a human review queue — a
low-mean-similarity fold is noise there regardless of corpus, so a modest
floor ships on.

DEFAULT 0.6 (ACTIVE) — a genuine small merge's members are mutually
similar; 0.6 sits below tight near-duplicate clusters (~0.7+) while
excluding the vague ~0.33-mean over-clusters. Complements the complete-
linkage MIN-pairwise gate (a chain can have high mean but a sub-threshold
min) and the size cap. Env ``ATHENAEUM_MIN_MERGE_MEAN_SIMILARITY`` > yaml
``librarian.min_merge_mean_similarity`` > this default; ``0`` (or negative)
disables the floor. No seed in ``_DEFAULTS`` so the code default
stays reachable. ``bool`` and non-numeric yaml values fall through to the
default.

### `resolve_name_collision_automerge_enabled`

- **YAML path:** `librarian.name_collision_automerge`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `False`
- **Precedence:** `athenaeum.yaml` > code default

Resolve the name-collision auto-merge opt-in. DEFAULT OFF.

Gates only the UNAMBIGUOUS-collision auto-merge branch of
`athenaeum.name_collisions.resolve_name_collisions` — with this
off (the default), every collision the nightly scan finds still writes
a proposal block to ``_pending_merges.md`` (see
`resolve_name_collision_scan_enabled`), it just never
self-approves.

This default is a deliberate, reasoned choice, not an oversight: was split on 2026-08-31 from the one-time destructive
repair sweep over collisions ALREADY PRESENT in the operator's live
corpus, which is — ``~operator``-gated and blocked
by this issue. Shipping auto-merge ON by default here would make the
very next nightly run perform exactly that unattended sweep, defeating
the split -> was meant to create. So the
auto-merge path is fully built and fully tested (see
`athenaeum.name_collisions` and its test suite) and ships OFF: an
operator who explicitly sets ``librarian.name_collision_automerge:
true`` gets auto-merge of the unambiguous subset only (an ambiguous
collision always queues for human review regardless of this flag), and
every auto-merge is reversible via ``git revert``/``git show`` by
construction (the same ``fold-into-existing`` write path
already made recoverable).

Precedence: yaml ``librarian.name_collision_automerge`` (a plain
``bool``) overrides the ``False`` default; anything else (missing,
non-bool) falls through to ``False``.

### `resolve_name_collision_scan_enabled`

- **YAML path:** `librarian.name_collision_scan`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `True`
- **Precedence:** `athenaeum.yaml` > code default

Resolve the nightly name-collision scan opt-out. DEFAULT ON.

Gates `athenaeum.librarian._run_name_collision_phase` /
`athenaeum.name_collisions.resolve_name_collisions` — a
deterministic, zero-cost, exact-``name:``-match scan over ``wiki/*.md``
(no LLM, no vectors, no network). Unlike `resolve_comparator_enabled`'s
expensive comparator pass, there is no cost reason to ship this off by
default; ``librarian.name_collision_scan: false`` exists only as an
operator escape hatch. Mirrors `resolve_delta_enabled`'s shape:
yaml ``librarian.name_collision_scan`` (a plain ``bool``) overrides the
``True`` default; anything else (missing, non-bool) falls through to
``True``.

### `resolve_operational_markers`

- **YAML path:** `librarian.operational_markers`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `[]`
- **Precedence:** `athenaeum.yaml` > code default

Resolve content markers for operational auto-memory families.

These are lower-cased substrings; the classifier requires a MULTI-SIGNAL
match (>= 2 distinct markers present) before it will drop an intake on
markers alone, so a single incidental word can never clobber a legit
architecture note. DEFAULT-EMPTY: a fresh install never classifies on
markers -- only the operator opts in via ``librarian.operational_markers``.
No seed in ``_DEFAULTS``.

### `resolve_page_flag_bytes`

- **YAML path:** `librarian.page_flag_bytes`
- **Environment variable:** `ATHENAEUM_PAGE_FLAG_BYTES`
- **CLI flag:** —
- **Default:** `16384`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the wiki-page flag-for-split size threshold in bytes.

Precedence: ``ATHENAEUM_PAGE_FLAG_BYTES`` env > ``librarian.page_flag_bytes``
yaml > ``16384``. A page over this is flagged more loudly (and logged during
``athenaeum run``) as one that should be broken into linked sub-entities.
Kept comfortably below the tier-3 merge body cap so flagging precedes any
hard merge-budget pressure. See `_resolve_positive_int_knob`.

### `resolve_page_warn_bytes`

- **YAML path:** `librarian.page_warn_bytes`
- **Environment variable:** `ATHENAEUM_PAGE_WARN_BYTES`
- **CLI flag:** —
- **Default:** `8192`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the wiki-page soft-warn size threshold in bytes.

Precedence: ``ATHENAEUM_PAGE_WARN_BYTES`` env > ``librarian.page_warn_bytes``
yaml > ``8192``. A page whose UTF-8 size (frontmatter + body) exceeds this
is surfaced in ``status`` as a warn-level oversized page — a nudge to split,
never a block. See `_resolve_positive_int_knob` for the coercion
contract.

### `resolve_preserved_log_adapter`

- **YAML path:** `librarian.preserved_log_adapter`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `None`
- **Precedence:** `athenaeum.yaml` > code default

Resolve the preserved-log routing adapter from
``librarian.preserved_log_adapter``.

Names a registered ``storage.adapters.<name>`` (see
`athenaeum.storage`) that the `preserve` disposition
(`athenaeum.rules`) should route through INSTEAD of the local,
in-repo area `resolve_preserved_log_dir` names — the seam that lets
a preserved log land outside the knowledge git repo (a different
filesystem, a mounted corpus, an operator-defined adapter whose
``surface_root`` is absolute), which ``preserved_log_dir`` structurally
cannot do. When both keys are set, the adapter wins and the rules engine
logs a warning that it shadows ``preserved_log_dir`` — see
`athenaeum.rules`'s `preserve` branch.

This resolver only reads the operator's raw string; it does **not**
validate that the named adapter actually exists — that check belongs to
`athenaeum.storage` (`athenaeum.storage.available_adapters`),
which this L2 config module must not import (it would cycle back:
``storage.py`` already imports ``config.py`` to resolve its own adapter
definitions). The caller resolving an unknown adapter name raises
`athenaeum.storage.StorageConfigError` loudly rather than
silently falling back to the local directory.

Returns ``None`` when unset or blank — DEFAULT-NONE, matching
`resolve_preserved_log_dir`: a fresh install has no adapter
override configured. No seed in ``_DEFAULTS``.

### `resolve_preserved_log_dir`

- **YAML path:** `librarian.preserved_log_dir`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `None`
- **Precedence:** `athenaeum.yaml` > code default

Resolve the preserved-log area from ``librarian.preserved_log_dir``.

A **preserved log is a source document, not intake.** The operator names a
folder under the knowledge root here — e.g. ``preserved_log_dir: logs`` —
and declares that its contents are artifacts to be kept whole, referenced
as provenance, and never compiled into wiki prose. The `preserve`
disposition (`athenaeum.rules`) MOVES a matching raw file into it.

Why a directory OUTSIDE ``raw/`` rather than another flag on a file that
stays put. `retain` already covers "mark it exempt where it
lies", and that is the weaker guarantee: the file remains in the intake
tree, so every future mechanism that walks ``raw/`` must remember to
consult the exempt manifest, and a manifest that fails open (by design —
see `athenaeum.compiled_exempt`) silently re-offers it. Moving the
file makes the guarantee structural instead of advisory: a preserved log
is not skipped by discovery, it is *not discoverable*, because
`athenaeum.intake.discover_raw_files` only ever walks ``raw/``.

Returns the operator's value as a **relative POSIX path string**, or
``None`` when unset or unusable. Rejected (with a ``log.warning``, never a
raise — an unusable value must not take the nightly run down): an absolute
path, and any value escaping the knowledge root via ``..``.

This key is deliberately scoped to the LOCAL, in-repo case only — an
operator who needs a preserved artifact to land outside the knowledge
root uses ``librarian.preserved_log_adapter`` instead (see
`resolve_preserved_log_adapter`), which routes
through a registered `athenaeum.storage` adapter whose resolved root
may be absolute. Before the absolute/escaping rejection here was
reasoned as a blanket prohibition — "outside the repo it is neither
versioned nor recoverable" — but S3 replaced the old
``.git``-existence gate with a declared, per-store
``Store.capabilities.versioned``/recoverability capability that a
non-git surface can satisfy on its own terms. "Outside the repo" is
therefore no longer categorically unrecoverable, it is a property of
whichever store an artifact is routed through — checked there, not
assumed impossible here. This resolver still refuses an absolute or
escaping value, but now simply because THIS key's contract is "a
directory under the knowledge root": a scoping rule, not a
recoverability argument.

DEFAULT-NONE: a fresh install has no preserved area, so a `preserve` rule
is inert until an operator configures one (the feature is opt-in twice
over — the area AND a rule). No seed in ``_DEFAULTS``.

### `resolve_push_after_run`

- **YAML path:** `librarian.push_after_run`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `False`
- **Precedence:** `athenaeum.yaml` > code default

Resolve the post-run ``git push`` opt-in.

Closes the move-then-retire recovery gap: a scheduled nightly ``athenaeum
run`` commits locally but, without this opt-in, never pushes — so the
git-only retired-raw recovery story only holds on the machine that ran
the librarian. With ``librarian.push_after_run: true`` (or the
``athenaeum run --push`` CLI override), the librarian invokes ``git push``
after a successful run that produced at least one commit, using the
operator's ambient git credentials. Default OFF: no push without explicit
opt-in, and athenaeum itself handles no tokens/secrets. No seed in
``_DEFAULTS``. Non-bool yaml values fall through to off.

### `resolve_push_branch`

- **YAML path:** `librarian.push_branch`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `None`
- **Precedence:** `athenaeum.yaml` > code default

Resolve the post-run push branch from ``librarian.push_branch``.

Returns ``None`` when unset (the librarian will push the knowledge repo's
current branch, which is what nightly schedulers expect). A non-string
or empty yaml value also returns ``None``.

### `resolve_push_remote`

- **YAML path:** `librarian.push_remote`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `'origin'`
- **Precedence:** `athenaeum.yaml` > code default

Resolve the post-run push remote from ``librarian.push_remote``.

Defaults to ``origin`` — the conventional name the knowledge repo's
remote will carry on every operator we ship to. A non-string or empty
yaml value falls through to the default.

### `resolve_raw_file_max_api_calls`

- **YAML path:** `librarian.raw_file_max_api_calls`
- **Environment variable:** `ATHENAEUM_RAW_FILE_MAX_API_CALLS`
- **CLI flag:** —
- **Default:** `60`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the per-raw-file LLM-call bound (recalibrated).

Precedence: ``ATHENAEUM_RAW_FILE_MAX_API_CALLS`` env >
``librarian.raw_file_max_api_calls`` yaml > ``60``. Checked
INCREMENTALLY by `athenaeum.tiers.tier3_derive_actions`, after each
entity action a raw file drives, against the running count of LLM calls
THAT ONE FILE has consumed so far (``usage.api_calls`` before the file
started vs. now) — see `athenaeum.models.RawFileOverBudgetError`'s
docstring for why the check moved from "once, after the whole file" to
"after every action".

The original ``8`` default assumed an ordinary file costs roughly 1-3
calls (tier-2 classify plus one tier-3 action or two). Measured reality
on the live deployment (2026-08-15/16 nightly logs, api provider) put an
ordinary file at **20-46 calls** — un-batched ``tier3_write`` spends one
call per entity action, and a file with several entities easily clears a
dozen — so ``8`` sat 3-6x below the median file and rejected normal
input rather than catching loopers. ``60`` covers the measured
distribution with headroom while still catching a file whose action set
genuinely loops. See `_resolve_positive_int_knob` for the coercion
contract.

### `resolve_raw_file_max_bytes`

- **YAML path:** `librarian.raw_file_max_bytes`
- **Environment variable:** `ATHENAEUM_RAW_FILE_MAX_BYTES`
- **CLI flag:** —
- **Default:** `5242880`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the per-raw-file byte bound in bytes.

Precedence: ``ATHENAEUM_RAW_FILE_MAX_BYTES`` env > ``librarian.raw_file_max_bytes``
yaml > ``5242880`` (5 MiB). Enforced by `athenaeum.models.RawFile.content`
— a raw intake file over this is refused BEFORE it is read into memory or
handed to the classifier (`athenaeum.models.RawFileTooLargeError`).
The default sits comfortably below the 9.7MB dry-run artifact that
motivated this bound (it accounted for 93% of timed entity-phase LLM
calls for roughly three months) while staying generous for a legitimately
large note or document dump — ordinary `remember`-authored intake is KB-
sized. See `_resolve_positive_int_knob` for the coercion contract.

### `resolve_raw_file_max_runtime_seconds`

- **YAML path:** `librarian.raw_file_max_runtime_seconds`
- **Environment variable:** `ATHENAEUM_RAW_FILE_MAX_RUNTIME_SECONDS`
- **CLI flag:** —
- **Default:** `900`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the per-raw-file wall-clock bound, in seconds.

, recalibrated.

Precedence: ``ATHENAEUM_RAW_FILE_MAX_RUNTIME_SECONDS`` env >
``librarian.raw_file_max_runtime_seconds`` yaml > ``900``. Checked
alongside `resolve_raw_file_max_api_calls`, incrementally, after
each entity action — the wall-clock spent inside ONE file's processing
so far, compared against this bound.

The original ``120`` default assumed a single file's tier-2/tier-3
round trip(s) stayed well under it. Measured reality on the live
deployment (2026-08-15/16 nightly logs, api provider) put an ordinary
file at **300-690 seconds** — in line with the same un-batched
per-action call pattern that drove the call-count recalibration above —
so ``120`` rejected normal input long before it caught anything
pathological. ``900`` covers the measured distribution with headroom
while still catching a file that genuinely hangs or loops. See
`_resolve_positive_int_knob` for the coercion contract.

### `resolve_raw_retention_max_file_bytes`

- **YAML path:** `librarian.raw_retention.max_file_bytes`
- **Environment variable:** `ATHENAEUM_RAW_RETENTION_MAX_FILE_BYTES`
- **CLI flag:** —
- **Default:** `None`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the per-file raw-intake size ceiling in bytes
from ``librarian.raw_retention.max_file_bytes``.

A single file anywhere under `raw/<source>/` at or above this many bytes
is reported as an oversize file (`raw-oversize-file`, see
`athenaeum.intake.check_raw_retention`) — the file is never
blocked, moved, or exempted, only named in the run summary. ``None``
(the default — key unset) DISABLES this check entirely, matching a
fresh install imposing no limit.

Precedence: ``ATHENAEUM_RAW_RETENTION_MAX_FILE_BYTES`` env >
``librarian.raw_retention.max_file_bytes`` yaml > disabled. A malformed
env value WARNs and falls through (see `_env_number`); ``bool``
(an ``int`` subclass) and non-int / non-positive values — env OR yaml —
fall through to disabled, same as an unset key.

### `resolve_raw_retention_max_source_bytes`

- **YAML path:** `librarian.raw_retention.max_source_bytes`
- **Environment variable:** `ATHENAEUM_RAW_RETENTION_MAX_SOURCE_BYTES`
- **CLI flag:** —
- **Default:** `None`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the per-source aggregate raw-intake size ceiling in bytes
 from ``librarian.raw_retention.max_source_bytes``.

The SUM of every file's on-disk size anywhere under one
`raw/<source>/` tree, at or above this many bytes, is reported as an
oversize source (`raw-oversize-source`, see
`athenaeum.intake.check_raw_retention`) — nothing is blocked,
moved, or exempted. This is the dimension a per-file-only limit cannot
substitute for: it is what catches many individually-small files
aggregating past a ceiling (the corpus that motivated this issue was
943 MB across 2,247 files, ~420 KB average — a per-file threshold sized
for git hygiene would not have fired on a single one of them). ``None`` (the default — key unset) DISABLES
this check entirely.

Precedence: ``ATHENAEUM_RAW_RETENTION_MAX_SOURCE_BYTES`` env >
``librarian.raw_retention.max_source_bytes`` yaml > disabled. Same
malformed-env / bool-rejection / non-positive-falls-through-to-disabled
contract as `resolve_raw_retention_max_file_bytes`.

### `resolve_reasoning_tier_auditing_enabled`

- **YAML path:** `librarian.reasoning_tier_auditing_enabled`
- **Environment variable:** `ATHENAEUM_REASONING_TIER_AUDITING_ENABLED`
- **CLI flag:** —
- **Default:** `False`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the T1 reasoning-tier screen's opt-in. DEFAULT OFF.

**Re-read as T1-ONLY as of.** Before this single
key gated BOTH the harmless T1 screen (reject-or-pass-up, no write
authority) and T2's UNREVIEWED AUTO-APPLY (safe-class merges written to
the wiki with no human review) together — one key arming two very
different blast radii. split them: this key now gates ONLY

- the T1 reasoning screen in the merge path
 (`athenaeum.merge.merge_clusters_to_wiki`) — a confident T1 reject
 drops a merge proposal before it reaches the human queue.

T2's auto-apply authority now requires its OWN, separate, explicit
opt-in — see `resolve_reasoning_tier_t2_auto_apply_enabled`, which
defaults OFF independent of this key's value (AC3). The
calibration display surface (``athenaeum calibration summary`` / the
``calibration_summary`` MCP tool) checks BOTH keys via
`resolve_reasoning_tier_any_screen_enabled`, not this function
alone, so it stays accurate for a (T1 off, T2 on) config too.

**Migration note for an existing config (AC4/AC5):** a
config that already sets ``librarian.reasoning_tier_auditing_enabled:
true`` keeps its T1 screen exactly as before, but as of this change no
longer also arms T2's auto-apply — T2 now requires the new key below to
be set as well. This is a change in what the EXISTING key's value means,
and it changes it in the safe direction only: it can only ever REMOVE
auto-apply authority an old config previously had, never grant new
authority a config didn't already have. To restore the exact
pre- combined behavior, add ONE line:
``librarian.reasoning_tier_t2_auto_apply_enabled: true``. See
``docs/reference/configuration.md``'s "Reasoning-tier screening" section for the
full migration story.

Env ``ATHENAEUM_REASONING_TIER_AUDITING_ENABLED`` (``1``/``true``/``yes``/``on``,
case-insensitive) > yaml ``librarian.reasoning_tier_auditing_enabled`` >
default ``False``. No seed in ``_DEFAULTS``. Default OFF is
deliberate: wiring the T1 screen changes what reaches the human merge
queue, so it stays opt-in until an operator turns it on — production merge
behavior is byte-identical to today until then. Non-bool yaml values and
unrecognized env strings fall through to off.

### `resolve_reasoning_tier_t2_auto_apply_enabled`

- **YAML path:** `librarian.reasoning_tier_t2_auto_apply_enabled`
- **Environment variable:** `ATHENAEUM_REASONING_TIER_T2_AUTO_APPLY_ENABLED`
- **CLI flag:** —
- **Default:** `False`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve T2's unreviewed-auto-apply opt-in. DEFAULT OFF.

Split out of `resolve_reasoning_tier_auditing_enabled`: T1 (a harmless reject-or-pass-up screen with no write
authority) and T2 (which can auto-apply a safe-class merge into the wiki
with NO human review, via ``pending_merges.resolve_merge(...,
auto_applied=True)``) used to share one flag. This key is T2's OWN,
independent opt-in — resolved separately from, and NOT implied by,
`resolve_reasoning_tier_auditing_enabled` (T1's key). A config can
set either key alone, both, or neither; T1 being on does not turn T2 on,
and T2 being on does not require T1 (see
`athenaeum.merge.merge_clusters_to_wiki` — T2's screen call is
gated by this value directly, exactly as T1's is gated by its own).

Env ``ATHENAEUM_REASONING_TIER_T2_AUTO_APPLY_ENABLED``
(``1``/``true``/``yes``/``on``, case-insensitive) > yaml
``librarian.reasoning_tier_t2_auto_apply_enabled`` > default ``False``.
No seed in ``_DEFAULTS``. **Default OFF regardless of
the T1 key's value (AC3)** — an operator who already
has ``reasoning_tier_auditing_enabled: true`` in a live config does NOT
get T2 auto-apply for free; it must be armed explicitly. Non-bool yaml
values and unrecognized env strings fall through to off.

### `resolve_reasoning_trigger_backlog_bytes`

- **YAML path:** `librarian.reasoning_triggers.backlog_bytes`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `None`
- **Precedence:** `athenaeum.yaml` > code default

Resolve the backlog-depth-by-byte-count trigger threshold
from ``librarian.reasoning_triggers.backlog_bytes``.

``M bytes`` is the LITERAL on-disk size of pending raw intake (sum of
`athenaeum.intake.discover_raw_files`'s files' ``stat.st_size``,
via `athenaeum.intake.discover_raw_backlog_bytes`) — not a cost or
token estimate. When the backlog reaches or exceeds this many bytes, the
backlog-depth trigger fires (see `athenaeum.reasoning_triggers`).
``None`` (the default — key unset) DISABLES this trigger entirely.
``bool`` and non-positive / non-int values fall through to disabled, same
as an unset key.

### `resolve_reasoning_trigger_backlog_files`

- **YAML path:** `librarian.reasoning_triggers.backlog_files`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `None`
- **Precedence:** `athenaeum.yaml` > code default

Resolve the backlog-depth-by-file-count trigger threshold
from ``librarian.reasoning_triggers.backlog_files``.

When the pending-reasoning raw-intake backlog (`athenaeum.intake.
discover_raw_files`) reaches or exceeds this many files, the backlog-depth
trigger fires (see `athenaeum.reasoning_triggers`). ``None`` (the
default — key unset) DISABLES this trigger entirely; it never fires on
file count. ``bool`` and non-positive / non-int values fall through to
disabled, same as an unset key.

### `resolve_reasoning_trigger_interval_hours`

- **YAML path:** `librarian.reasoning_triggers.interval_hours`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `None`
- **Precedence:** `athenaeum.yaml` > code default

Resolve the elapsed-interval trigger threshold in hours
from ``librarian.reasoning_triggers.interval_hours``.

When at least this many hours have elapsed since the last completed
triggered reasoning run (see `athenaeum.reasoning_triggers` and the
reasoning-trigger last-run stamp), the interval trigger fires regardless
of backlog depth — so a quiet night still gets a bounded, incremental
look rather than going dark until the nightly backstop. ``None`` (the
default — key unset) DISABLES this trigger entirely. ``bool`` and
non-positive / non-int values fall through to disabled, same as an unset
key.

### `resolve_reasoning_trigger_nightly_backstop_hours`

- **YAML path:** `librarian.reasoning_triggers.nightly_backstop_hours`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `24`
- **Precedence:** `athenaeum.yaml` > code default

Resolve the nightly-backstop trigger threshold in hours (default 24) from ``librarian.reasoning_triggers.nightly_backstop_hours``.

Unlike the other three reasoning triggers, the backstop is always ON —
tying reasoning to a single nightly window is exactly the failure mode
 removes (a bad night goes invisible for 24h). The backstop fires
when at least this many hours have elapsed since the last completed
triggered reasoning run AND no other trigger fired this evaluation (see
`athenaeum.reasoning_triggers`) — it is the demoted fallback, not the
primary path. ``bool`` and non-positive / non-int values fall through to
the default.

### `resolve_reindex_full_rehash_max_age_days`

- **YAML path:** `librarian.reindex.full_rehash_max_age_days`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `7.0`
- **Precedence:** `athenaeum.yaml` > code default

Resolve the periodic full-re-hash backstop age in days (default 7).

The stat pre-filter reuses a stored content hash whenever a file's
``(mtime_ns, size)`` match the manifest, so a content edit that preserved
BOTH would slip past until a full re-hash. On an INCREMENTAL build, when the
manifest has not recorded a full re-hash within this many days, the search
backend re-reads and re-hashes EVERY file for one build (still applying the
change delta incrementally — no full re-embed / FTS5 rebuild). Read from
``librarian.reindex.full_rehash_max_age_days``.

``0`` or negative => always re-hash; a very large value => effectively never.
``bool`` (an ``int`` subclass) and non-numeric values fall through to the
default so ``full_rehash_max_age_days: yes`` cannot read as ``1``.

### `resolve_retention_destination`

- **YAML path:** `librarian.retention.families.<family>.destination` > `librarian.retention.defaults.destination`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `'in-repo'`
- **Precedence:** per-family `athenaeum.yaml` > shared-defaults `athenaeum.yaml` > code default

Resolve *family*'s retention destination from
``librarian.retention.families.<family>.destination`` >
``librarian.retention.defaults.destination`` > the stated default
``"in-repo"``.

Returns ``"in-repo"``, ``"pii-vault"``, or ``"adapter:<name>"`` verbatim
(the adapter name is validated by the caller that resolves it to an
actual root -- `athenaeum.retention_policy` -- via
`athenaeum.storage.available_adapters`, mirroring the `preserve`
disposition's fail-loud-on-unknown-adapter contract; this resolver only
reads the operator's string, same division of labor as
`resolve_preserved_log_adapter`). A value matching neither
``"in-repo"``, ``"pii-vault"`` nor the ``"adapter:"`` prefix -- at either
level -- WARNs and falls through.

### `resolve_retention_max_bytes`

- **YAML path:** `librarian.retention.families.<family>.max_bytes` > `librarian.retention.defaults.max_bytes`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `1048576` (1 MiB)
- **Precedence:** per-family `athenaeum.yaml` > shared-defaults `athenaeum.yaml` > code default

Resolve *family*'s truncation bound in bytes from
``librarian.retention.families.<family>.max_bytes`` >
``librarian.retention.defaults.max_bytes`` > the stated default
``1048576`` (1 MiB, "sized for a git-tracked file" per the issue).

Resolvable independent of `resolve_retention_policy` -- a caller
enforcing ``truncate-top`` needs this regardless of how the policy
itself resolved. ``bool`` (an ``int`` subclass) and non-int /
non-positive values -- at either level -- fall through, same as every
other numeric resolver here.

### `resolve_retention_policy`

- **YAML path:** `librarian.retention.families.<family>.policy` > `librarian.retention.defaults.policy`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `'truncate-top'`
- **Precedence:** per-family `athenaeum.yaml` > shared-defaults `athenaeum.yaml` > code default

Resolve *family*'s retention policy from
``librarian.retention.families.<family>.policy`` >
``librarian.retention.defaults.policy`` > the stated default
``"truncate-top"`` -- but ONLY once ``librarian.retention`` exists at
all; absent that block entirely, returns ``None`` (AC1).

Returns one of ``"truncate-top"``, ``"never-truncate"``,
``"librarian-decides"``, or ``None``. A value outside that vocabulary --
at either the family or defaults level -- WARNs and falls through to the
next level, same malformed-value-falls-through idiom as every other
resolver in this module; it is never allowed to silently disable
enforcement by being mistaken for ``None``.

### `resolve_retire`

- **YAML path:** `librarian.retire`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `True`
- **Precedence:** `athenaeum.yaml` > code default

Resolve the move-then-retire opt-out from yaml ``librarian.retire``.

The move-then-retire pass moves non-contradictory raw
auto-memory into the wiki and ``git rm``s it. It is DEFAULT-ON
(owner-confirmed): only ``librarian.retire: false`` in ``athenaeum.yaml``
turns it off, and the ``athenaeum run --no-retire`` CLI flag overrides to
off at the call site. No seed in ``_DEFAULTS`` — the default
lives here in code so it stays reachable. Non-bool yaml values fall through
to the default (on).

### `resolve_rule_proposals_enabled`

- **YAML path:** `librarian.rule_proposals.enabled`
- **Environment variable:** `ATHENAEUM_RULE_PROPOSALS_ENABLED`
- **CLI flag:** —
- **Default:** `False`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

``librarian.rule_proposals.enabled`` (default False). DEFAULT OFF.

: gates the wiring of
`athenaeum.rule_proposals.run_rule_proposal_detection` into the
nightly ``athenaeum run`` loop (``librarian._run_rule_proposal_phase``).
With this off (the default), the phase returns immediately -- zero LLM
calls, no client constructed, no disposition-ledger read.

Mirrors `resolve_verdict_ledger_enabled`'s shape: env
``ATHENAEUM_RULE_PROPOSALS_ENABLED`` (``1``/``true``/``yes``/``on``,
case-insensitive) > yaml ``librarian.rule_proposals.enabled`` > default
``False``. Default OFF is deliberate: this wiring adds a NEW unattended
language-model call to the nightly run -- real recurring spend an
operator must opt into rather than discover behind a detector issue (see
's own text). Set ``librarian.rule_proposals.enabled: true``
(or the env var) to turn it on. Non-bool yaml values and unrecognized env
strings fall through to off.

### `resolve_rule_proposals_exemplar_count`

- **YAML path:** `librarian.rule_proposals.exemplar_count`
- **Environment variable:** `ATHENAEUM_RULE_PROPOSALS_EXEMPLAR_COUNT`
- **CLI flag:** —
- **Default:** `5`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

``librarian.rule_proposals.exemplar_count`` (default 5).

 AC2's "K exemplars" -- how many readable raw records
of a detected shape are embedded in the one drafting call.

### `resolve_rule_proposals_threshold`

- **YAML path:** `librarian.rule_proposals.threshold`
- **Environment variable:** `ATHENAEUM_RULE_PROPOSALS_THRESHOLD`
- **CLI flag:** —
- **Default:** `50`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

``librarian.rule_proposals.threshold`` (default 50).

 AC1/AC2, respecified by part 2: the
count of DISTINCT RECORDS (by ``source_ref``, never disposition ROWS --
see `athenaeum.rule_proposals._distinct_record_count`, the single
function that makes this concrete) -- grouped by ``(source,
key_fingerprint)``, restricted to rows the shape-rules pass deferred to
the reasoning ladder (``tier is None`` in
``_shape_rule_dispositions.jsonl``; see `athenaeum.rule_proposals`)
-- that must be crossed within `resolve_rule_proposals_window_days`
before the librarian drafts a candidate rule for that shape.

Counting rows instead of records is a real, ~9.5x-consequential
difference in practice: before the ledger deduped re-evaluations at
write time (part 1), a handful of records
re-evaluated on every nightly run alone crossed a threshold of 50 by
row count while their DISTINCT record count stayed far below it (57 of
66 shapes crossed by row count vs. 6 by distinct record on the
deployment that motivated). This docstring's "record count"
was always the intent; `athenaeum.rule_proposals` now measures it,
not rows.

### `resolve_rule_proposals_window_days`

- **YAML path:** `librarian.rule_proposals.window_days`
- **Environment variable:** `ATHENAEUM_RULE_PROPOSALS_WINDOW_DAYS`
- **CLI flag:** —
- **Default:** `30`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

``librarian.rule_proposals.window_days`` (default 30).

's "configurable window": the detector only counts
``_shape_rule_dispositions.jsonl`` rows whose ``at`` timestamp falls
within this many days of "now".

### `resolve_scope_aware_recall_enabled`

- **YAML path:** `librarian.scope_aware_recall_enabled`
- **Environment variable:** `ATHENAEUM_SCOPE_AWARE_RECALL_ENABLED`
- **CLI flag:** —
- **Default:** `False`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the scope-aware recall opt-in. DEFAULT OFF.

Gates the READ side of the ``scope`` dimension in ``recall``
(`athenaeum.mcp_server.recall_search`): with this off, a caller-supplied
query scope is accepted but never changes which hits are returned —
byte-identical to today. With this on AND a query scope supplied, hits are
narrowed via `athenaeum.scope_resolution.resolve_most_specific` so a
general claim is dropped in favor of an in-scope more-specific one (or
dropped entirely when its ``claimed_scope`` does not contain the query
scope at all). This is the read-side counterpart to
`athenaeum.verdict_effects.write_refines_declaration`, which already
writes the ``refines:`` edges this reads.

Mirrors `resolve_auto_supersession_enabled`'s shape exactly: env
``ATHENAEUM_SCOPE_AWARE_RECALL_ENABLED`` (``1``/``true``/``yes``/``on``,
case-insensitive) > yaml ``librarian.scope_aware_recall_enabled`` >
default ``False``. No seed in ``_DEFAULTS``.

### `resolve_shape_rules_dispositions_retention_days`

- **YAML path:** `librarian.shape_rules.dispositions_retention_days`
- **Environment variable:** `ATHENAEUM_SHAPE_RULES_DISPOSITIONS_RETENTION_DAYS`
- **CLI flag:** —
- **Default:** `30`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

``librarian.shape_rules.dispositions_retention_days`` (default 30).

How many days of ``wiki/_shape_rule_dispositions.jsonl`` rows
`athenaeum.rules.prune_shape_rule_dispositions` keeps at the tail of
every shape-rules phase -- the ledger's retention policy, and what gives
it a bounded steady state instead of monotonic growth.

 split this out of
`resolve_rule_proposals_window_days`, which had
doing double duty. Those are two different questions: ``window_days`` is
a READ window (how far back the detector counts), this is a
RETENTION policy (how much history the file physically carries). Coupled,
narrowing the detector's window silently deleted ledger history, and an
operator who disabled ``librarian.rule_proposals`` outright still had
retention governed by a key belonging to a phase they had turned off.
The default matches 's effective behaviour (``window_days``
also defaults to 30), so this split is a no-op for existing deployments.

### `resolve_shape_rules_log_no_match`

- **YAML path:** `librarian.shape_rules.log_no_match`
- **Environment variable:** `ATHENAEUM_SHAPE_RULES_LOG_NO_MATCH`
- **CLI flag:** —
- **Default:** `False`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

``librarian.shape_rules.log_no_match`` (default False). DEFAULT OFF.

: whether the shape-rules pass writes a per-record
``disposition: "no-match"`` row to ``wiki/_shape_rule_dispositions.jsonl``
for every candidate no rule claimed. On a real deployment those rows were
99.8% of a 341 MB ledger (1,485,942 of 1,488,689 rows over 9 days) --
a negative result, regenerated on every nightly pass and re-derivable at
any time by re-running the phase, sitting inside a git repo whose stated
value is being small and diffable.

**Default OFF is safe only because the sole consumer is also off by
default.** ``no-match`` rows carry ``tier: None``, which is exactly what
`athenaeum.rule_proposals._grouped_deferred_rows` reads for
's shape-frequency detector -- so these rows are NOT inert.
But that detector is reached only through
``librarian._run_rule_proposal_phase``, itself gated on
`resolve_rule_proposals_enabled` (also default False), which
returns before any disposition-ledger read when off. With both at their
defaults, suppressing the write loses nothing.

**An operator turning on ``librarian.rule_proposals.enabled`` must turn
this on too**, and must then wait
`resolve_rule_proposals_window_days` (default 30 days) for the
detector to accumulate enough history to propose anything -- the ledger
holds no ``no-match`` history from the period this was off. That coupling
is deliberately NOT expressed as a derived default: every resolver in
this module resolves to a literal, and a config value whose default is
another config value would make "enable detection" quietly mean "wait a
month" with nothing in config to show for it.

Mirrors `resolve_rule_proposals_enabled`'s shape: env
``ATHENAEUM_SHAPE_RULES_LOG_NO_MATCH`` (``1``/``true``/``yes``/``on``,
case-insensitive) > yaml ``librarian.shape_rules.log_no_match`` > default
``False``. Non-bool yaml values and unrecognized env strings fall through
to off.

### `resolve_shape_rules_max_records_per_run`

- **YAML path:** `librarian.shape_rules.max_records_per_run`
- **Environment variable:** `ATHENAEUM_SHAPE_RULES_MAX_RECORDS_PER_RUN`
- **CLI flag:** —
- **Default:** `50000`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

``librarian.shape_rules.max_records_per_run`` (default 50,000).

Run-level cap on candidate raw files the engine evaluates against rules
in one run. Mirrors ``librarian.corrections.max_records_per_run``
(§10.2) — once the cap is hit, remaining candidates are left untouched
for the next run (never dropped, never partially processed).

### `resolve_shape_rules_runtime_share`

- **YAML path:** `librarian.shape_rules.runtime_share`
- **Environment variable:** `ATHENAEUM_SHAPE_RULES_RUNTIME_SHARE`
- **CLI flag:** —
- **Default:** `0.05`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

``librarian.shape_rules.runtime_share`` (default 0.05).

Fraction of ``librarian.max_runtime`` the shape-rule phase may spend,
mirroring `resolve_corrections_runtime_share`'s mechanism exactly
(own env var, own yaml key, same coercion rules: only ``0 < share < 1``
reserves anything).

### `resolve_sibling_widening_budget`

- **YAML path:** `librarian.sibling_widening_budget`
- **Environment variable:** `ATHENAEUM_SIBLING_WIDENING_BUDGET`
- **CLI flag:** —
- **Default:** `25`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the per-run budget for sibling-scope widening probes.

The sibling-widening instrument deliberately spends Gate-2 calls on pairs
Gate 1 ALREADY settled as DISTINCT, to catch convergent local practice
that would otherwise stay permanently fragmented across sibling scopes.
That is unbounded LLM cost by construction, so this requires it be
"bounded by a documented budget" -- this is that budget, counted in
``content_relation`` calls per run. Set it to ``0``... you cannot: a
``<= 0`` value falls through to the default per
`_resolve_positive_int_knob`. Disable the instrument by leaving
`resolve_comparator_enabled` off, or set the budget to ``1``.

Env ``ATHENAEUM_SIBLING_WIDENING_BUDGET`` > yaml
``librarian.sibling_widening_budget`` > ``25``.

### `resolve_sibling_widening_classes`

- **YAML path:** `librarian.sibling_widening_classes`
- **Environment variable:** `ATHENAEUM_SIBLING_WIDENING_CLASSES`
- **CLI flag:** —
- **Default:** `['axiom', 'guideline', 'procedure']`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the "guideline-like" memory classes for sibling widening.

Scope-separated DISTINCTs are only probed for convergence in classes
where two sibling scopes independently arriving at the same rule is a
real, recurring pattern worth unifying -- ``guideline``, ``procedure``,
``axiom``. A scope-separated pair of ``entity`` pages is two different
entities and probing it is pure cost.

Env ``ATHENAEUM_SIBLING_WIDENING_CLASSES`` (comma-separated) > yaml
``librarian.sibling_widening_classes`` (a list) > the default set.
Values outside `athenaeum.memory_class.MEMORY_CLASSES` are dropped;
an empty result falls through to the default.

### `resolve_sibling_widening_min_similarity`

- **YAML path:** `librarian.sibling_widening_min_similarity`
- **Environment variable:** `ATHENAEUM_SIBLING_WIDENING_MIN_SIMILARITY`
- **CLI flag:** —
- **Default:** `0.85`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the "top band" similarity floor for sibling widening.

Only TOP-BAND-similarity, scope-separated DISTINCTs get the extra
memoized ``content_relation`` call. Similarity's only job here is
PROPOSING which pairs to spend the budget on -- exactly as it proposes
merge candidates elsewhere -- and it never reaches a verdict, so this is
a candidate-generation knob and NOT one of the confidence thresholds
 bans (those attach a scalar to a VERDICT; see
``docs/reference/configuration.md``).

Env ``ATHENAEUM_SIBLING_WIDENING_MIN_SIMILARITY`` > yaml
``librarian.sibling_widening_min_similarity`` > ``0.85``. A parsed env
value is authoritative over yaml (M1); a ``bool`` /
non-numeric / out-of-``(0, 1]`` yaml value falls through to the default.

### `resolve_standing_state_claim_kinds`

- **YAML path:** `librarian.standing_state_claim_kinds`
- **Environment variable:** `ATHENAEUM_STANDING_STATE_CLAIM_KINDS`
- **CLI flag:** —
- **Default:** `['decision', 'fact', 'policy']`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve which `athenaeum.models.CLAIM_KINDS` count as STANDING STATE.

's first auto-supersession precondition is "it is a
standing-state fact" -- a claim about a state that holds until something
changes it, so that a later claim about the same coordinates genuinely
REPLACES it. The default set is ``fact``, ``decision``, ``policy``.

The three excluded kinds are excluded on purpose, and an operator
widening this set should know why:

- ``observation`` is a point-in-time event. A later observation does not
 retire an earlier one; both happened.
- ``opinion`` is evaluative -- two asserters may hold different, both-valid
 opinions. already routes an opinion pair to ``attribute_both``
 rather than a precedence winner; auto-retiring one would contradict that.
- ``definition`` is timeless.

An UNCLASSIFIED claim (``claim_kind`` absent -- the fail-open ``""`` of
`athenaeum.models.parse_claim_kind`) is never standing-state here.
That is deliberate fail-CLOSED behaviour for a destructive action: the
rest of the codebase fails open on an unclassified claim because the
consequence is only a missed optimisation, whereas here the consequence
is retiring a claim nobody classified.

Env ``ATHENAEUM_STANDING_STATE_CLAIM_KINDS`` (comma-separated) > yaml
``librarian.standing_state_claim_kinds`` (a list) > the default set.
Values outside `athenaeum.models.CLAIM_KINDS` are dropped; an empty
result falls through to the default rather than disabling every
precondition silently.

### `resolve_supersession_asserter_weekly_max`

- **YAML path:** `librarian.supersession_asserter_weekly_max`
- **Environment variable:** `ATHENAEUM_SUPERSESSION_ASSERTER_WEEKLY_MAX`
- **CLI flag:** —
- **Default:** `10`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the PER-ASSERTER weekly self-revision cap.

"An asserter whose same-asserter auto-supersessions of single-source
facts exceed 10/week corpus-wide has condition (a) suspended pending
review" -- this is the 10. Unlike the per-claim limit, exceeding this
suspends condition (a) for that asserter ENTIRELY (every claim), because
the failure it catches is one sloppy or compromised writer drifting the
corpus a little in many places rather than oscillating in one.

Env ``ATHENAEUM_SUPERSESSION_ASSERTER_WEEKLY_MAX`` > yaml
``librarian.supersession_asserter_weekly_max`` > ``10``.

### `resolve_supersession_claim_window_max`

- **YAML path:** `librarian.supersession_claim_window_max`
- **Environment variable:** `ATHENAEUM_SUPERSESSION_CLAIM_WINDOW_MAX`
- **CLI flag:** —
- **Default:** `3`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the PER-CLAIM self-revision cap inside the window.

The ORDINAL of the auto-supersession that must queue-flag rather than
auto-apply: at the default ``3``, the first two same-asserter
self-revisions of one claim inside
`resolve_supersession_self_revision_window_days` auto-apply and the
third does not. Counting is over the audit trail
(`athenaeum.supersession`'s ledger), not over frontmatter.

Env ``ATHENAEUM_SUPERSESSION_CLAIM_WINDOW_MAX`` > yaml
``librarian.supersession_claim_window_max`` > ``3``.

### `resolve_supersession_self_revision_window_days`

- **YAML path:** `librarian.supersession_self_revision_window_days`
- **Environment variable:** `ATHENAEUM_SUPERSESSION_SELF_REVISION_WINDOW_DAYS`
- **CLI flag:** —
- **Default:** `90`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the PER-CLAIM self-revision rate-limit window, in days.

"The third auto-supersession of the same claim by the same asserter
within 90 days queue-flags instead of auto-applying" -- this is the 90.
Per-claim limits catch OSCILLATION (one asserter flip-flopping a single
fact); the per-asserter limit
(`resolve_supersession_asserter_weekly_max`) catches diffuse drift.

Env ``ATHENAEUM_SUPERSESSION_SELF_REVISION_WINDOW_DAYS`` > yaml
``librarian.supersession_self_revision_window_days`` > ``90``. See
`_resolve_positive_int_knob` for the coercion contract.

### `resolve_verdict_epoch_batch_interval_days`

- **YAML path:** `librarian.verdict_epoch_batch_interval_days`
- **Environment variable:** `ATHENAEUM_VERDICT_EPOCH_BATCH_INTERVAL_DAYS`
- **CLI flag:** —
- **Default:** `30`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the comparator-epoch batching interval in days.

"Epoch bumps are batched (default monthly) and the batching interval is
a documented config key" — this is that key. Precedence:
``ATHENAEUM_VERDICT_EPOCH_BATCH_INTERVAL_DAYS`` env > yaml
``librarian.verdict_epoch_batch_interval_days`` > ``30``. See
`_resolve_positive_int_knob` for the coercion contract (``bool`` /
non-int / ``<= 0`` values fall through to the default).

### `resolve_verdict_ledger_enabled`

- **YAML path:** `librarian.verdict_ledger_enabled`
- **Environment variable:** `ATHENAEUM_VERDICT_LEDGER_ENABLED`
- **CLI flag:** —
- **Default:** `False`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the verdict-ledger opt-in. DEFAULT OFF.

Gates the ENTIRE verdict-ledger subsystem (`athenaeum.verdicts`):
with this off, ``athenaeum run`` never touches ``wiki/_verdicts/`` (no
new file, no new run-summary phase, no exit-code change — byte-identical
to before), and a merge approve/reject via ``athenaeum
ingest-answers`` never writes a verdict entry. Mirrors
`resolve_reasoning_tier_auditing_enabled`'s shape exactly: env
``ATHENAEUM_VERDICT_LEDGER_ENABLED`` (``1``/``true``/``yes``/``on``,
case-insensitive) > yaml ``librarian.verdict_ledger_enabled`` > default
``False``. No seed in ``_DEFAULTS``. Default OFF is
deliberate — the comparator that would populate the ledger with real
verdicts does not exist yet (a separate, future child of);
turning this on before then only exercises the store/schema/epoch
machinery via the merge approve/reject decisions the pipeline already
makes. Non-bool yaml values and unrecognized env strings fall through to
off.

## `memory_tiers`

### `resolve_memory_tier_demote_after_days`

- **YAML path:** `memory_tiers.demote_after_days`
- **Environment variable:** `ATHENAEUM_MEMORY_TIER_DEMOTE_AFTER_DAYS`
- **CLI flag:** —
- **Default:** `60`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the age-without-use / precision-grace window in days.

Shared threshold `athenaeum.memory_tiers.evaluate_tier_movement`
uses for two of its three automatic hot -> warm demotion triggers: a hot
claim with no usage record at all after this many days, or a hot claim
that HAS been pushed but never referenced and whose last push is older
than this many days. The third trigger (class-default: superseded/
deprecated) is unconditional and ignores this knob.

Precedence: ``ATHENAEUM_MEMORY_TIER_DEMOTE_AFTER_DAYS`` env >
``memory_tiers.demote_after_days`` yaml > ``60``. A malformed env value
WARNs and falls through (see `_env_number`); a non-int / ``<= 0``
yaml value falls through to the default. No seed in ``_DEFAULTS``

## `models`

### `resolve_model`

- **YAML path:** `models.<knob>`
- **Environment variable:** caller-supplied (one per knob, e.g. `ATHENAEUM_WRITE_MODEL`)
- **CLI flag:** —
- **Default:** caller-supplied
- **Precedence:** environment variable > `athenaeum.yaml` > caller-supplied default

Resolve a model id from env > yaml ``models.<knob>`` > code default.

Mirrors `athenaeum.librarian.librarian_max_api_calls`:
the env var wins over the yaml key so an operator can swap a model for a
single run without editing config, and the yaml key is read only when
the operator actually set it — no seed in ``_DEFAULTS``.
Non-string or blank yaml values fall through to *default*. The
contradiction-resolver model routes through here too, via
`athenaeum.resolutions._get_model` (knob ``resolve``); that
wrapper threads the legacy ``resolve.model`` yaml key in as *default*
so it sits below ``models.resolve`` but above the code default.

## `off_corpus`

### `resolve_off_corpus_enabled`

- **YAML path:** `off_corpus.enabled`
- **Environment variable:** `ATHENAEUM_OFF_CORPUS_ENABLED`
- **CLI flag:** —
- **Default:** `False`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the off-corpus indexable-store master switch. DEFAULT OFF.

Gates the ENTIRE off-corpus subsystem (`athenaeum.off_corpus`): with
this off, `athenaeum.librarian.reindex` never touches a second
index, ``recall`` never federates a second result set, and
`athenaeum.verdicts.record_pair_decision` keeps its pre-
behavior of refusing (not writing) an erasure-class pair — byte-identical
to before this issue existed. Mirrors `resolve_verdict_ledger_enabled`'s
shape exactly: env ``ATHENAEUM_OFF_CORPUS_ENABLED`` (``1``/``true``/``yes``/``on``,
case-insensitive) > yaml ``off_corpus.enabled`` > default ``False``. No seed
in ``_DEFAULTS`` ('s precedent). Non-bool yaml values and
unrecognized env strings fall through to off.

## `owner`

### `resolve_owner_asserter`

- **YAML path:** `owner.asserter`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `None`
- **Precedence:** `athenaeum.yaml` > code default

Return the owner's OIDC ``asserter`` identity block, or ``None``.

Read from ``owner.asserter`` in ``athenaeum.yaml``. Used by
``repair --backfill-sources`` to stamp ``on_behalf_of`` on a
``user-stated`` upgrade WHEN a durable identity is configured. Transcripts
carry no OIDC identity, so an unset block leaves ``on_behalf_of`` absent
(the fallback). Returns the raw dict unchanged for
`athenaeum.models.asserter_identity_key` to key on; a non-dict or
empty block is inert.

## `person_registry`

### `resolve_person_registry_root`

- **YAML path:** `person_registry.root`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `<knowledge_root>/wiki`
- **Precedence:** `athenaeum.yaml` (relative to `knowledge_root`) > code default

Resolve the on-disk root `athenaeum.person_registry.PersonRegistry`
scans for ``type: person`` pages.

Default: ``<knowledge_root>/wiki`` — the SAME directory
`athenaeum.models.EntityIndex` already scans. This is deliberate
backward compatibility: demotes ``type: person`` pages out of
the general entity-index NAME keys (see
`athenaeum.models.DEMOTED_NAME_MATCH_TYPES`), but does not itself
move a single file — the one-time physical relocation of person pages in
a live corpus is (blocked by). Until that
relocation runs, every person page still lives under ``wiki/``, so this
resolver has to keep pointing there for `athenaeum.person_registry.PersonRegistry`
to find anything on an unmigrated corpus. Once physically moves
person pages elsewhere, set ``person_registry.root`` (relative paths
resolve against *knowledge_root*; an absolute path is used as-is) to
repoint this WITHOUT a code change.

No env override: unlike a run-level budget knob, this is a structural
corpus-layout fact an operator sets once (if ever), not a per-invocation
dial — mirrors `resolve_authority_grant_implications`'s yaml-only
posture for the same reason.

## `push_budget`

### `resolve_push_token_budget`

- **YAML path:** `push_budget.tokens_per_turn`
- **Environment variable:** `ATHENAEUM_PUSH_TOKEN_BUDGET`
- **CLI flag:** —
- **Default:** `1200`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the unprompted push budget in tokens-per-turn.

The one documented dial for how much recall pushes into a turn
unprompted (the "hot" retrieval-cost tier only — see
`athenaeum.memory_tiers`, deliberately "the entire expensive-and-
noisy dial" per that issue's AC). Enforced at
`athenaeum.mcp_server._recall_via_backend`'s ``unprompted=True``
path (`athenaeum.memory_tiers.select_for_push`): hits are ranked
by relevance x tier x coordinate-fit and greedily included, in that
order, while the running token total (`athenaeum.push_metrics.estimate_tokens`)
stays within this budget — a hit that would exceed it is skipped, never
truncated.

Precedence: ``ATHENAEUM_PUSH_TOKEN_BUDGET`` env > ``push_budget.tokens_per_turn``
yaml > ``1200``. A malformed env value WARNs and falls through (see
`_env_number`); a non-int / ``<= 0`` yaml value falls through to
the default. No seed in ``_DEFAULTS``.

## `push_metrics`

### `resolve_push_metrics_enabled`

- **YAML path:** `push_metrics.enabled`
- **Environment variable:** `ATHENAEUM_PUSH_METRICS_ENABLED`
- **CLI flag:** —
- **Default:** `True`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve whether push-precision/coverage instrumentation runs.

ON by default: it is passive measurement — one small append-only JSONL
row per recall push and per session-end reference determination, both
under the cache dir, never inside the wiki corpus — and the whole point
of the v6 memory-model epic's precision baseline is that it starts
recording BEFORE any later slice changes what recall pushes. Precedence:
``ATHENAEUM_PUSH_METRICS_ENABLED`` env > ``push_metrics.enabled`` yaml >
``True``. Any env value other than a falsey token (``0`` / ``false`` /
``no`` / ``off``, case-insensitive) is truthy; a non-bool yaml value falls
through to the default. No seed in ``_DEFAULTS`` — mirrors
`resolve_spend_ledger_enabled`'s shape exactly.

## `recall`

### `resolve_index_globs (exclude_globs)`

- **YAML path:** `recall.exclude_globs`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `None` (unset — index everything)
- **Precedence:** `athenaeum.yaml` > code default

Resolve ``(include_globs, exclude_globs)`` for corpus scoping.

COULD-tier footprint/relevance knob. Default (unset) returns
``(None, None)`` — index everything — because the Apollo contact wikis
are legitimate name-recall targets and must stay indexed by default.

### `resolve_extra_intake_roots`

- **YAML path:** `recall.extra_intake_roots`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `[]` (yaml default is `["raw/auto-memory"]`; relative entries resolve against `knowledge_root` and entries that aren't real directories are dropped with a warning)
- **Precedence:** `athenaeum.yaml` > code default

Resolve configured extra intake roots to absolute `Path` values.

Values under ``recall.extra_intake_roots`` that are relative are
resolved against ``knowledge_root``; absolute paths are passed through.
Missing directories are dropped (with a warning) — a half-initialized
knowledge base (no ``raw/auto-memory`` yet) should not break index
rebuild, but operators should see a diagnostic when a configured
root is typo'd or unmounted. Returns an empty list when no extras
are configured.

### `resolve_index_globs (include_globs)`

- **YAML path:** `recall.include_globs`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `None` (unset — index everything)
- **Precedence:** `athenaeum.yaml` > code default

Resolve ``(include_globs, exclude_globs)`` for corpus scoping.

COULD-tier footprint/relevance knob. Default (unset) returns
``(None, None)`` — index everything — because the Apollo contact wikis
are legitimate name-recall targets and must stay indexed by default.

### `resolve_recall_relevance_floor`

- **YAML path:** `recall.relevance_floor.<backend>` (see docstring — the `unprompted` call path resolves a sibling, push-specific key)
- **Environment variable:** per-backend (see docstring)
- **CLI flag:** —
- **Default:** `None` (no floor — today's behavior, unchanged, until an operator opts in)
- **Precedence:** environment variable > `athenaeum.yaml` > `None`

Resolve the recall relevance floor for one (backend, call path).

``None`` — the value at every precedence level, for every backend and
both call paths, until an operator opts in — means NO FLOOR: today's
behavior, unchanged. That is the acceptance criterion this function
exists to satisfy: merging it changes no existing recall output.

This is the MECHANISM, not the tuning. Two of the issue's four open
design questions are settled here at the mechanism-SHAPE level (not the
value level), and recorded rather than left ambiguous:

* absolute vs. relative-to-the-result-set threshold -> ABSOLUTE. Each
 hit's own score is compared against a fixed configured number.
* per-backend vs. one normalized cross-backend confidence -> PER-BACKEND.
 FTS5's ``rank`` and the keyword scorer's additive score are different
 scales with different "better" directions (see
 `athenaeum.search.meets_relevance_floor`), so this resolves and
 compares in each backend's own units rather than inventing a
 normalization this issue does not scope. A normalized layer could
 still be built on top later without reshaping this resolver.

The other two open questions are genuinely NOT decided here: whether the
push path should in practice be set stricter than explicit recall (this
function only makes that independently *settable* via ``unprompted``,
AC3 — it expresses no opinion on which, if either, should be stricter),
and whether a below-floor hit should be suppressed or surfaced as
low-confidence (`athenaeum.search.meets_relevance_floor` only
supports suppression — marking is a rendering decision for the
follow-up).

Precedence per (backend, path): env var > ``recall.relevance_floor``
yaml > ``None``. Vars: ``ATHENAEUM_RECALL_MIN_SCORE_FTS5`` /
``ATHENAEUM_RECALL_MIN_SCORE_KEYWORD`` for an explicit ``recall_search``
call; ``ATHENAEUM_RECALL_PUSH_MIN_SCORE_FTS5`` /
``ATHENAEUM_RECALL_PUSH_MIN_SCORE_KEYWORD`` for the ``unprompted=True``
push path. YAML shape:
```
recall:
  relevance_floor:
    fts5: -6.0
    keyword: 12.0
    push:
      fts5: -3.0
      keyword: 20.0
```

An unrecognized ``backend_name`` (e.g. ``"vector"``, not named here's acceptance criteria) always resolves to ``None``: no
floor is applied regardless of config. A malformed env value WARNs and
falls through to yaml/default (see `_env_number`); a non-numeric
yaml value is ignored the same way.

## `screening`

### `resolve_screening`

- **YAML path:** `screening.medical.action`
- **Environment variable:** `ATHENAEUM_SCREEN_MEDICAL`
- **CLI flag:** —
- **Default:** `{'medical': {'action': 'off', 'access': 'personal'}}`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve intake-screening settings for ``remember``.

Returns ``{"medical": {"action", "access"}}``. This first slice screens
only the ``medical`` category; the action is one of ``off`` (default) /
``label_restrict``. Precedence per the module convention (env > yaml >
default, no seed in ``_DEFAULTS`` so the code default stays reachable):
``ATHENAEUM_SCREEN_MEDICAL`` env > ``screening.medical.action`` yaml >
``off``.

Raises `athenaeum.screening.ScreeningConfigError` on an invalid or
unsupported setting (unknown action, ``drop`` on medical, or a bad access
level) so a mis-configured operator gets a clear signal at serve time
rather than a silent no-op. ``label_restrict`` is inert until content
actually matches, so an ``off``/unset install never touches intake.

## `sensitivity`

### `resolve_sensitivity_classes`

- **YAML path:** `sensitivity.classes`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `{}`
- **Precedence:** `athenaeum.yaml` > code default

Resolve the ``sensitivity.classes`` class-definition blocks (S1b).

Returns the raw (still-unvalidated) per-class mapping dicts keyed by class
name; `athenaeum.sensitivity.available_classes` validates each,
resolves ``inherits`` chains, and builds the
`athenaeum.sensitivity.SensitivityClass` objects. Returns an EMPTY
dict when unset — the shipped built-in ``pii`` class (defined in
`athenaeum.sensitivity._BUILTIN_CLASSES`, not here — this dict's
own source-of-truth rule, §2.4 of ``docs/design/sensitivity-class-vocabulary.md``)
is still resolved by ``available_classes`` regardless, so this resolver is
NOT seeded in ``_DEFAULTS``: seeding it here would make the code default
unreachable, the exact regression that rule exists to
prevent. Non-string keys and non-mapping values are dropped defensively
(a malformed entry is surfaced loudly later, at build time, with the
class name in the message — same posture as `resolve_storage_adapters`).

### `resolve_sensitivity_routing`

- **YAML path:** `sensitivity.routing`
- **Environment variable:** `ATHENAEUM_SENSITIVITY_ROUTING_ENABLED`
- **CLI flag:** —
- **Default:** `{'classes': {}, 'enabled': False}`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the ``sensitivity.routing`` config surface (design note §8).

Slice 1/4 of 's design note (`docs/design/sensitivity-value-routing.md`).

Returns ``{"enabled": bool, "classes": {<name>: {"action": "route"|"off"}}}``.
A separate axis from `resolve_sensitivity_classes` ('s
own ``sensitivity.classes.*`` — the *definition* of a class): this block
decides whether a matched class gets intercepted at intake, so a class
can be defined without being routed. This slice adds no behavior on its
own — nothing reads this resolver yet (for the slices that do).

Precedence per the module convention (env > yaml > default, no seed in
``_DEFAULTS`` so the code default stays reachable):
``ATHENAEUM_SENSITIVITY_ROUTING_ENABLED`` env (``true``/``false``,
case-insensitive) > ``sensitivity.routing.enabled`` yaml > ``False``
(dark by default — the whole stage is a no-op, byte-identical to
pre- behavior, until an operator opts in).

Each entry under ``sensitivity.routing.classes.<name>`` may set
``action`` to ``"route"`` or ``"off"``; when a class block is present but
``action`` is unset, it defaults to ``"route"`` (defining a class and
turning routing on is read as "protect it" unless the operator
explicitly opts the class out).

Raises `SensitivityRoutingConfigError` on a malformed ``enabled``
value (yaml value that isn't a bool, or an env value that isn't
``true``/``false``) or an unknown per-class ``action`` — fail loud, no
silent fallback, matching `athenaeum.screening.ScreeningConfigError`
/ `athenaeum.storage.StorageConfigError`'s existing posture.

## `serve`

### `resolve_audience`

- **YAML path:** `serve.audience`
- **Environment variable:** `ATHENAEUM_AUDIENCE`
- **CLI flag:** —
- **Default:** `None`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the serve-time read-scope audience pin.

Returns the role set this ``serve`` / ``recall`` process is pinned to, or
``None`` for the owner / default caller (FULL access — every page,
untagged included). ``None`` keeps existing single-user installs unchanged.

Precedence follows the repo convention CLI > env > yaml > default:

- ``cli_value`` — the ``--audience`` flag's comma-separated value.
- ``ATHENAEUM_AUDIENCE`` — comma-separated env var.
- ``serve.audience`` — a yaml list (or comma string).
- ``None`` — owner, unfiltered.

An explicitly EMPTY value at any tier (blank flag, ``ATHENAEUM_AUDIENCE=``,
empty yaml list) resolves to ``None`` = owner: to RESTRICT a caller you must
name at least one non-empty role. Role ids are opaque, case-folded, and
whitespace-trimmed; athenaeum assigns them no meaning (they map onto the
operator's external RBAC). No seed in ``_DEFAULTS``.

## `source_dir`

### `resolve_non_intake_sources`

- **YAML path:** `source_dir.name`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `set()`
- **Precedence:** `athenaeum.yaml` > code default

Resolve `raw/<source>/` dirs excluded from entity intake.

A source directory named here is skipped WHOLE by
`athenaeum.intake.discover_raw_files` — none of its files become
entity intake. This is for a tool that writes its own OPERATIONAL
artifacts into ``raw/<source>/`` (the same tree ``remember``-authored
content uses): action logs, launchd logs, state dumps. Those match the
``*.md`` / ``*.jsonl`` glob and are not correction-batch envelopes, so
without this knob they enter ``tier2_classify`` → ``tier3_write`` as if
they were memory content, and a multi-megabyte log gets read whole and
handed to the classifier.

Generalizes the hardcoded ``source == "answers"`` skip, which stays as-is: this is a SECOND, operator-controlled
mechanism alongside it, so the next occurrence is a config change rather
than another patch to ``discover_raw_files``.

Matched against ``source_dir.name`` exactly (no globbing, no case folding
— a directory name on disk is what it is). DEFAULT-EMPTY: a fresh install
excludes nothing, so unconfigured discovery is byte-identical to
pre- behaviour. No seed in ``_DEFAULTS``.

## `spend`

### `resolve_spend_accounting_timezone`

- **YAML path:** `spend.accounting_timezone`
- **Environment variable:** `ATHENAEUM_SPEND_ACCOUNTING_TIMEZONE`
- **CLI flag:** —
- **Default:** the host's system-local timezone (see `_system_local_timezone`)
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the timezone the per-day spend ceilings account against.

Every per-day ceiling (`resolve_spend_max_tokens_per_day`,
`resolve_spend_max_usd_per_day`, and the weekly-percent derivation
in `resolve_spend_max_pct_per_day`) is enforced by
`athenaeum.spend.ceiling_tripped` against
`athenaeum.spend.spend_today`'s "since the start of the accounting
day" window — this is the knob that decides where that day BEGINS.

**Why this defaults to the system's local timezone, not UTC**: a per-day ceiling is an OPERATOR budget — "how much may be
spent today" means the operator's today. A UTC-midnight default silently
opens the accounting window mid-evening for any operator west of UTC:
for an operator in US Eastern time (UTC-4/-5), UTC midnight lands at
20:00/19:00 local — squarely inside a typical evening working session —
so that session can exhaust the WHOLE day's ceiling before a scheduled
job firing after local midnight (but still inside the SAME UTC calendar
day) ever gets a fresh window. That was observed in production: a
nightly librarian run at 02:16 local inherited a ceiling an evening
session had already exhausted three hours earlier, and compiled zero
entities on every observed night. Defaulting to UTC would leave this
starvation in place until an operator discovers the config key and sets
it themselves — exactly what makes it a bug rather than a setting. An
operator who already runs in UTC sees zero behavior change (their local
day already equals the UTC day).

Precedence: ``ATHENAEUM_SPEND_ACCOUNTING_TIMEZONE`` env >
``spend.accounting_timezone`` yaml > the system's local timezone (see
`_system_local_timezone`). Both the env var and the yaml key take
an IANA zone name (e.g. ``America/New_York``). A name
`zoneinfo.ZoneInfo` cannot resolve — a typo, or a name absent
from the running system's tzdata — WARNs and falls back to UTC rather
than raising: a malformed timezone string must never crash a run any
more than a malformed number does elsewhere in this module (see
`_env_number`).

### `resolve_spend_ledger_enabled`

- **YAML path:** `spend.ledger_enabled`
- **Environment variable:** `ATHENAEUM_SPEND_LEDGER_ENABLED`
- **CLI flag:** —
- **Default:** `True`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve whether the spend ledger is written (env > yaml > True).

The durable LLM-spend ledger (``~/.cache/athenaeum/spend.jsonl``) is ON by
default — it is append-only, crash-safe, and records only counts (never
content or credentials), so the cost is negligible. Precedence:
``ATHENAEUM_SPEND_LEDGER_ENABLED`` env > ``spend.ledger_enabled`` yaml >
``True``. Any env value other than a falsey token (``0`` / ``false`` /
``no`` / ``off``, case-insensitive) is truthy; a non-bool yaml value falls
through to the default. No seed in ``_DEFAULTS``.

### `resolve_spend_ledger_path`

- **YAML path:** `spend.ledger_path`
- **Environment variable:** `ATHENAEUM_SPEND_LEDGER`
- **CLI flag:** —
- **Default:** `None`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve an explicit spend-ledger path override (env > yaml > None).

``None`` means "use the default" — ``<cache_dir>/spend.jsonl`` under
``~/.cache/athenaeum`` (see `athenaeum.spend.default_ledger_path`).
Precedence: ``ATHENAEUM_SPEND_LEDGER`` env > ``spend.ledger_path`` yaml >
``None``. Chiefly a test/relocation seam. No seed in ``_DEFAULTS``.

### `resolve_spend_max_pct_per_day`

- **YAML path:** `spend.max_pct_per_day`
- **Environment variable:** `ATHENAEUM_SPEND_MAX_PCT_PER_DAY`
- **CLI flag:** —
- **Default:** `None`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the max-percent-of-weekly-allowance-per-day knob.

Paired with `resolve_spend_weekly_token_limit`: this is the percentage
taken OF that weekly figure to produce a daily subscription token ceiling.
On its own (the weekly limit unset) it does nothing — there is no
denominator to apply a percentage to, so setting only one of the two knobs
leaves behavior unchanged, exactly like every other ceiling's opt-in
contract. Precedence: ``ATHENAEUM_SPEND_MAX_PCT_PER_DAY`` env >
``spend.max_pct_per_day`` yaml > ``None`` (no ceiling).

### `resolve_spend_max_tokens_per_day`

- **YAML path:** `spend.max_tokens_per_day`
- **Environment variable:** `ATHENAEUM_SPEND_MAX_TOKENS_PER_DAY`
- **CLI flag:** —
- **Default:** `None`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the per-day SUBSCRIPTION token ceiling (env > yaml > None).

Summed across every ledger record on the subscription path since the
start of the current ACCOUNTING day (— see
`resolve_spend_accounting_timezone`; UTC by default only when the
operator's own local timezone is UTC), plus the current run's accrued
tokens. Precedence: ``ATHENAEUM_SPEND_MAX_TOKENS_PER_DAY`` env >
``spend.max_tokens_per_day`` yaml > ``None`` (no ceiling).

### `resolve_spend_max_tokens_per_run`

- **YAML path:** `spend.max_tokens_per_run`
- **Environment variable:** `ATHENAEUM_SPEND_MAX_TOKENS_PER_RUN`
- **CLI flag:** —
- **Default:** `None`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the per-run SUBSCRIPTION token ceiling (env > yaml > None).

A run served by the ``claude-cli`` provider consumes subscription quota
rather than dollars, so its ceiling is a TOKEN count. When set and the
run-level total tokens reach it, the pass stops early and loudly (the
remaining intake defers to the next run, exactly like the ``max_api_calls``
budget). Precedence: ``ATHENAEUM_SPEND_MAX_TOKENS_PER_RUN`` env >
``spend.max_tokens_per_run`` yaml > ``None`` (no ceiling).

### `resolve_spend_max_usd_per_day`

- **YAML path:** `spend.max_usd_per_day`
- **Environment variable:** `ATHENAEUM_SPEND_MAX_USD_PER_DAY`
- **CLI flag:** —
- **Default:** `None`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the per-day API DOLLAR ceiling (env > yaml > None).

Summed across every ledger record on the metered API path since the
start of the current ACCOUNTING day (— see
`resolve_spend_accounting_timezone`; UTC by default only when the
operator's own local timezone is UTC), plus the current run's accrued
USD. Precedence: ``ATHENAEUM_SPEND_MAX_USD_PER_DAY`` env >
``spend.max_usd_per_day`` yaml > ``None`` (no ceiling).

### `resolve_spend_max_usd_per_run`

- **YAML path:** `spend.max_usd_per_run`
- **Environment variable:** `ATHENAEUM_SPEND_MAX_USD_PER_RUN`
- **CLI flag:** —
- **Default:** `None`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the per-run API DOLLAR ceiling (env > yaml > None).

A run served by the metered ``anthropic`` API path is constrained in real
dollars. When set and the run's estimated USD reaches it, the pass stops
early and loudly. Precedence: ``ATHENAEUM_SPEND_MAX_USD_PER_RUN`` env >
``spend.max_usd_per_run`` yaml > ``None`` (no ceiling).

### `resolve_spend_warning_threshold_pct`

- **YAML path:** `spend.warning_threshold_pct`
- **Environment variable:** `ATHENAEUM_SPEND_WARNING_THRESHOLD_PCT`
- **CLI flag:** —
- **Default:** `75.0`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the spend-headroom warning threshold, as a percent of either
API dollar cap.

Unlike the ceilings above, this knob is NOT opt-in — it always resolves to
a usable value (`DEFAULT_SPEND_WARNING_THRESHOLD_PCT` when unset),
because the warning it gates is meant to fire by default the first time a
run gets close to a ceiling the operator already configured; there is
nothing to warn about when neither ``max_usd_per_run`` nor
``max_usd_per_day`` is set (see `athenaeum.spend.spend_headroom`,
which reports a distinct "not configured" state for that case rather than
reading as 0% or 100% consumed). Precedence:
``ATHENAEUM_SPEND_WARNING_THRESHOLD_PCT`` env > ``spend.warning_threshold_pct``
yaml > ``75.0``. A ``bool`` / non-numeric / ``<= 0`` value (env or yaml)
falls through to the default — a zero/negative threshold would warn on
every run, including one that spent nothing. No seed in ``_DEFAULTS``
, matching every other spend knob in this module.

### `resolve_spend_weekly_token_limit`

- **YAML path:** `spend.weekly_token_limit`
- **Environment variable:** `ATHENAEUM_SPEND_WEEKLY_TOKEN_LIMIT`
- **CLI flag:** —
- **Default:** `None`
- **Precedence:** environment variable > `athenaeum.yaml` > code default

Resolve the operator-declared SUBSCRIPTION weekly token limit.

Claude Code subscription limits are rolling-window and are not exposed to
athenaeum as a readable quota, so there is no denominator to derive a
percentage ceiling from until the operator states one. This value is that
denominator — combined with `resolve_spend_max_pct_per_day` it
produces an effective daily subscription token ceiling of
``weekly_token_limit / 7 * (max_pct_per_day / 100)`` (see
`athenaeum.spend.ceiling_tripped`). On its own (the other knob
unset) it does nothing — strictly opt-in, like every other ceiling.
Precedence: ``ATHENAEUM_SPEND_WEEKLY_TOKEN_LIMIT`` env >
``spend.weekly_token_limit`` yaml > ``None`` (no ceiling).

## `storage`

### `resolve_off_corpus_adapter_name`

- **YAML path:** `storage.adapters`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `None`
- **Precedence:** `athenaeum.yaml` > code default

Resolve the ``storage.adapters`` entry name that backs the off-corpus
purgeable surface. yaml-only, no env var — a physical
surface name is not the shape of knob this repo's env-var convention
covers (mirrors ``storage.mapping``/``storage.adapters`` themselves,
which are also yaml-only). Returns ``None`` when unset; a ``None`` name
with `resolve_off_corpus_enabled` true is a configuration error
`athenaeum.off_corpus` raises loudly (D6: fail closed, loudly) —
this resolver itself stays defensive/non-raising like every other
resolver in this module.

### `resolve_storage_adapters`

- **YAML path:** `storage.adapters`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `{}`
- **Precedence:** `athenaeum.yaml` > code default

Resolve the ``storage.adapters`` custom-adapter definitions.

Returns the raw (still-primitive) per-adapter mapping dicts keyed by adapter
name; `athenaeum.storage.available_adapters` validates each and builds
the `athenaeum.storage.StorageAdapter` objects. Returns an EMPTY
dict when unset — the built-in ``wiki-markdown-embedded`` and ``excluded``
adapters are always available regardless. Non-string keys and non-mapping
values are dropped defensively (a malformed entry is surfaced loudly later,
at build time, with the adapter name in the message).

### `resolve_excluded_fields_config`

- **YAML path:** `storage.excluded_fields`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `{}`
- **Precedence:** `athenaeum.yaml` > code default

Resolve ``storage.excluded_fields`` — surface class → data-field names.

The explicit operator override at the top of
`athenaeum.pii.resolve_excluded_fields`'s resolution order: it names
which frontmatter fields on an excluded record of a given SURFACE class
(the ``storage.mapping`` key, e.g. ``pii`` — not a wiki page's ``type:``)
hold data rather than the record's own bookkeeping.

Returns an EMPTY dict when unset — the code default that leaves ``pii`` on
its built-in `athenaeum.pii.CONTACT_DATA_FIELDS` allowlist and every
other class on the denylist-complement, so an unconfigured base is
byte-identical (``resolve_storage_mapping``'s precedent: no seed in
``_DEFAULTS``, so this default stays reachable).

A class mapped to an EMPTY list is honoured literally as "this class has no
data fields" — that is an operator saying so, which is a different
statement from not configuring the class at all, and collapsing the two
would make the override unable to express it. Non-string keys, non-list
values, and blank/non-string field names are dropped defensively.

### `resolve_excluded_read_mapping`

- **YAML path:** `storage.excluded_read_mapping`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `{}`
- **Precedence:** `athenaeum.yaml` > code default

Resolve ``storage.excluded_read_mapping`` — page ``type:`` → surface class.

The operator override for the mapping ``recall`` consults to know WHICH
excluded surface a hit joins to. It is a different table from
``storage.mapping``: that one maps a class onto a storage ADAPTER, this one
maps a wiki page's ``type:`` onto the SURFACE CLASS whose excluded record
holds that page's excluded fields. The two names are distinct and the
distinction is load-bearing — a page is ``type: person`` while its record
lives on the ``pii`` surface.

Returns an EMPTY dict when unset. The identity default plus the single
shipped non-identity entry (``person: pii``) lives in
`athenaeum.pii.DEFAULT_EXCLUDED_READ_MAPPING`, not here, so this
resolver reports only what the OPERATOR configured —
``resolve_storage_mapping``'s precedent. Non-string keys/values and blank
entries are dropped defensively.

### `resolve_storage_mapping`

- **YAML path:** `storage.mapping`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `{}`
- **Precedence:** `athenaeum.yaml` > code default

Resolve the ``storage.mapping`` entity-class → adapter-name table.

Maps a wiki frontmatter ``type`` (``person``, ``pii``, …) onto the name of
a storage adapter (``wiki-markdown-embedded``, ``excluded``, or a custom
one). Returns an EMPTY dict when unset — the code default that keeps every
class on the default wiki surface, so an unconfigured base is byte-identical
(no seed in ``_DEFAULTS`` so this default stays reachable).
Non-string keys/values and blank entries are dropped defensively.

### `resolve_pii_scan_exclude`

- **YAML path:** `storage.pii_scan_exclude`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `[]`
- **Precedence:** `athenaeum.yaml` > code default

Resolve ``storage.pii_scan_exclude`` — extra PII-scan filename exclusions.

``storage lint-pii`` walks every file under ``wiki/`` (and, separately,
``raw/``) looking for inline emails/phones. A handful of files are
machine-generated audit logs whose content is regenerated wholesale on a
schedule — ``_shape_rule_dispositions.jsonl`` is the confirmed case: a
341+ MB log of epoch-millisecond timestamps that the phone-axis detector
misreads by the hundred-thousand, and whose distinct-value set never
stabilises (fresh timestamps nightly), so no allowlist entry can ever
absorb it. This is the OPERATOR'S list of ADDITIONAL filenames (matched
by name only, not full path) to exclude beyond the shipped default —
mirrors `resolve_google_contact_keys`'s shape exactly: the code
default (`athenaeum.pii.DEFAULT_PII_SCAN_EXCLUDE_FILENAMES`) is
additive and lives there, not here, so an unconfigured base still
protects itself with no seed in ``_DEFAULTS``.
Returns an empty list when unset. Non-string entries and blank entries
are dropped defensively.

```
storage:
  pii_scan_exclude:
    - _some_other_machine_log.jsonl
```

## `vector`

### `resolve_embedding_model`

- **YAML path:** `vector.embedding_model`
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `None`
- **Precedence:** `athenaeum.yaml` > code default

Resolve the configured vector embedding model (seam).

Returns ``None`` when unset so the VectorBackend uses its documented
default (``all-MiniLM-L6-v2``) unchanged.

## Other (no yaml key of its own)

### `resolve_cache_dir`

- **YAML path:** —
- **Environment variable:** `ATHENAEUM_CACHE_DIR`
- **CLI flag:** `--cache-dir` (several subcommands)
- **Default:** `~/.cache/athenaeum`
- **Precedence:** explicit argument (CLI `--cache-dir`) > environment variable > code default

Resolve the athenaeum cache dir: ``arg > ATHENAEUM_CACHE_DIR env > default``.

Returns an ``expanduser``-ed path (``~`` expanded); callers that need a
fully-resolved absolute path call ``.resolve`` on the result. This is the
one place ``~/.cache/athenaeum`` is defaulted and the env override applied.

### `resolve_reasoning_tier_any_screen_enabled`

- **YAML path:** —
- **Environment variable:** —
- **CLI flag:** —
- **Default:** `False`
- **Precedence:** code default only (no yaml key or env var of its own)

Whether EITHER reasoning-tier screen is armed.

``resolve_reasoning_tier_auditing_enabled(config) or
resolve_reasoning_tier_t2_auto_apply_enabled(config)`` — the calibration
display surface (``athenaeum calibration summary``, the
``calibration_summary`` / ``review_audit_item`` MCP tools) uses this
rather than the T1 key alone, so a (T1 off, T2 on) config — unusual, but
not forbidden — still shows T2's sampled audit items instead of a
misleading "tier auditing not enabled" that would hide an ACTIVELY
auto-applying tier from the one loop meant to catch it being wrong.

## Other environment variables

`ATHENAEUM_*` variables read somewhere in `src/` that are NOT sourced from a `config.py` `resolve_*` function — either a resolver in a different module (e.g. `cross_scope.py`, `clusters.py`, `batch_state.py`), a per-knob model-routing variable, or a variable read directly outside the resolver layer. Listed here (rather than omitted) so this page stays the complete answer to "what `ATHENAEUM_*` variables exist" — the same guarantee `scripts/check_env_docs.py` enforces in both directions.

| Env var | Referenced in |
|---|---|
| `ATHENAEUM_BATCH_MODE` | `src/athenaeum/_cmd_run.py`, `src/athenaeum/batch.py`, `src/athenaeum/config.py`, `src/athenaeum/librarian.py`, `src/athenaeum/provider.py` |
| `ATHENAEUM_CLAIM_KIND_MAX_TOKENS` | `src/athenaeum/claim_kind.py` |
| `ATHENAEUM_CLAIM_KIND_THINKING` | `src/athenaeum/claim_kind.py` |
| `ATHENAEUM_CLASSIFY_MAX_TOKENS` | `src/athenaeum/memory_class_backfill.py`, `src/athenaeum/page_description.py`, `src/athenaeum/tiers.py` |
| `ATHENAEUM_CLASSIFY_MODEL` | `src/athenaeum/claim_kind.py`, `src/athenaeum/comparator.py`, `src/athenaeum/config.py`, `src/athenaeum/contradictions.py`, `src/athenaeum/librarian.py`, `src/athenaeum/memory_class_backfill.py`, `src/athenaeum/page_description.py`, `src/athenaeum/tiers.py` |
| `ATHENAEUM_CLASSIFY_RETRY_MAX_TOKENS` | `src/athenaeum/tiers.py` |
| `ATHENAEUM_CLASSIFY_THINKING` | `src/athenaeum/memory_class_backfill.py`, `src/athenaeum/page_description.py`, `src/athenaeum/tiers.py` |
| `ATHENAEUM_CLAUDE_CLI_BIN` | `src/athenaeum/provider.py` |
| `ATHENAEUM_CLAUDE_CLI_TIMEOUT` | `src/athenaeum/provider.py` |
| `ATHENAEUM_COMPARATOR_CONTENT_RELATION_MAX_TOKENS` | `src/athenaeum/comparator.py`, `src/athenaeum/shadow_parity.py` |
| `ATHENAEUM_COMPARATOR_CONTENT_RELATION_THINKING` | `src/athenaeum/comparator.py` |
| `ATHENAEUM_CONTRADICTION_DETECT_MAX_TOKENS` | `src/athenaeum/contradictions.py`, `src/athenaeum/shadow_parity.py` |
| `ATHENAEUM_CONTRADICTION_DETECT_THINKING` | `src/athenaeum/contradictions.py` |
| `ATHENAEUM_CROSS_SCOPE_MODE` | `src/athenaeum/config.py`, `src/athenaeum/cross_scope.py`, `src/athenaeum/merge.py` |
| `ATHENAEUM_DISABLED` | `src/athenaeum/context.py`, `src/athenaeum/killswitch.py` |
| `ATHENAEUM_ENTITY_RUNTIME_SHARE` | `src/athenaeum/librarian.py` |
| `ATHENAEUM_FREETEXT_EDIT_MAX_TOKENS` | `src/athenaeum/resolutions.py` |
| `ATHENAEUM_FREETEXT_EDIT_THINKING` | `src/athenaeum/resolutions.py` |
| `ATHENAEUM_LLM_PROVIDER` | `src/athenaeum/config.py`, `src/athenaeum/drain.py`, `src/athenaeum/librarian.py`, `src/athenaeum/provider.py` |
| `ATHENAEUM_MAX_API_CALLS` | `src/athenaeum/_cmd_run.py`, `src/athenaeum/librarian.py` |
| `ATHENAEUM_MAX_FILES` | `src/athenaeum/_cmd_run.py`, `src/athenaeum/config.py`, `src/athenaeum/librarian.py` |
| `ATHENAEUM_MAX_RUNTIME` | `src/athenaeum/_cmd_run.py`, `src/athenaeum/drain.py`, `src/athenaeum/librarian.py` |
| `ATHENAEUM_MERGE_CREATE_MAX_TOKENS` | `src/athenaeum/tiers.py` |
| `ATHENAEUM_MERGE_CREATE_THINKING` | `src/athenaeum/tiers.py` |
| `ATHENAEUM_MERGE_FULL_MAX_TOKENS` | `src/athenaeum/tiers.py` |
| `ATHENAEUM_MERGE_FULL_THINKING` | `src/athenaeum/tiers.py` |
| `ATHENAEUM_MERGE_PATCH_MAX_TOKENS` | `src/athenaeum/tiers.py` |
| `ATHENAEUM_MERGE_PATCH_THINKING` | `src/athenaeum/tiers.py` |
| `ATHENAEUM_NOT_A_CONFLICT_TTL_DAYS` | `src/athenaeum/fingerprint.py` |
| `ATHENAEUM_PUSH_FAILURE_ALERT_THRESHOLD` | `src/athenaeum/librarian.py` |
| `ATHENAEUM_QUARANTINE_THRESHOLD` | `src/athenaeum/librarian.py` |
| `ATHENAEUM_REASONING_T1_LLM_PROVIDER` | `src/athenaeum/provider.py` |
| `ATHENAEUM_REASONING_T1_MAX_TOKENS` | `src/athenaeum/reasoning_tiers.py` |
| `ATHENAEUM_REASONING_T1_MODEL` | `src/athenaeum/reasoning_tiers.py` |
| `ATHENAEUM_REASONING_T1_THINKING` | `src/athenaeum/reasoning_tiers.py` |
| `ATHENAEUM_REASONING_T2_MAX_TOKENS` | `src/athenaeum/reasoning_tiers.py` |
| `ATHENAEUM_REASONING_T2_MODEL` | `src/athenaeum/reasoning_tiers.py` |
| `ATHENAEUM_REASONING_T2_THINKING` | `src/athenaeum/reasoning_tiers.py` |
| `ATHENAEUM_RECALL_MIN_SCORE_FTS5` | `src/athenaeum/config.py` |
| `ATHENAEUM_RECALL_MIN_SCORE_KEYWORD` | `src/athenaeum/config.py` |
| `ATHENAEUM_RECALL_PUSH_MIN_SCORE_FTS5` | `src/athenaeum/config.py` |
| `ATHENAEUM_RECALL_PUSH_MIN_SCORE_KEYWORD` | `src/athenaeum/config.py` |
| `ATHENAEUM_RECOVERY_YIELD_THRESHOLD` | `src/athenaeum/recovery_yield.py` |
| `ATHENAEUM_RESOLVED_SIMILARITY_THRESHOLD` | `src/athenaeum/fingerprint.py` |
| `ATHENAEUM_RESOLVE_AUTO_APPLY` | `src/athenaeum/resolutions.py` |
| `ATHENAEUM_RESOLVE_AUTO_APPLY_THRESHOLD` | `src/athenaeum/config.py`, `src/athenaeum/resolutions.py` |
| `ATHENAEUM_RESOLVE_FULL_BODY_TOKEN_CAP` | `src/athenaeum/resolutions.py` |
| `ATHENAEUM_RESOLVE_LLM_PROVIDER` | `src/athenaeum/_cmd_pending.py` |
| `ATHENAEUM_RESOLVE_MAX_PER_RUN` | `src/athenaeum/config.py`, `src/athenaeum/resolutions.py` |
| `ATHENAEUM_RESOLVE_MAX_TOKENS` | `src/athenaeum/resolutions.py` |
| `ATHENAEUM_RESOLVE_MODEL` | `src/athenaeum/config.py`, `src/athenaeum/resolutions.py` |
| `ATHENAEUM_RESOLVE_THINKING` | `src/athenaeum/provider.py`, `src/athenaeum/resolutions.py` |
| `ATHENAEUM_ROTATION_RETENTION` | `src/athenaeum/clusters.py`, `src/athenaeum/config.py` |
| `ATHENAEUM_RULE_PROPOSALS_MAX_TOKENS` | `src/athenaeum/rule_proposals.py` |
| `ATHENAEUM_RULE_PROPOSALS_MODEL` | `src/athenaeum/rule_proposals.py` |
| `ATHENAEUM_RULE_PROPOSALS_THINKING` | `src/athenaeum/rule_proposals.py` |
| `ATHENAEUM_RUN_TYPE` | `src/athenaeum/_cmd_run.py`, `src/athenaeum/librarian.py`, `src/athenaeum/spend.py` |
| `ATHENAEUM_SCHEMA_OBSERVATIONS_ENABLED` | `src/athenaeum/llm_schemas.py` |
| `ATHENAEUM_SESSION_END_RUNTIME_MARGIN` | `src/athenaeum/librarian.py` |
| `ATHENAEUM_STUCK_FILE_BACKOFF_BASE_SECONDS` | `src/athenaeum/librarian.py` |
| `ATHENAEUM_STUCK_FILE_THRESHOLD` | `src/athenaeum/librarian.py` |
| `ATHENAEUM_TIER4_DEDUP` | `src/athenaeum/tiers.py` |
| `ATHENAEUM_TOPIC_LLM_PROVIDER` | `src/athenaeum/query_topics.py` |
| `ATHENAEUM_TOPIC_MAX_TOKENS` | `src/athenaeum/query_topics.py` |
| `ATHENAEUM_TOPIC_MODEL` | `src/athenaeum/config.py`, `src/athenaeum/query_topics.py` |
| `ATHENAEUM_TOPIC_THINKING` | `src/athenaeum/query_topics.py` |
| `ATHENAEUM_WRITE_MODEL` | `src/athenaeum/config.py`, `src/athenaeum/drain_advisor.py`, `src/athenaeum/librarian.py`, `src/athenaeum/tiers.py` |
| `ATHENAEUM_ZERO_YIELD_ALERT_THRESHOLD` | `src/athenaeum/librarian.py`, `src/athenaeum/recovery_yield.py` |
