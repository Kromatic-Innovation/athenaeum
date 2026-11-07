# Trust graph for sources: architecture spec (athenaeum#2145)

- **Status:** design of record, decided 2026-11-05. Open questions in the original draft are resolved in section 8.
- **Authority:** operator ruling 2026-11-05 on athenaeum#2145.
- **Code baseline:** `origin/develop` at `7c6a1577`; every `path:line` citation in this document was re-verified against `origin/develop` at `40668992` (issue athenaeum#2329) and corrected where it had drifted — see that issue's lane report for the old→new list.
- **Related:** epic athenaeum#2144 (witness ledger), `docs/design/attribution-verification.md` (athenaeum#2192), `docs/design/conflict-resolution.md` §11, `docs/design/field-corrections.md` §6, north-star §2.2, §2.6, §2.7, §2.10 and §2.12.

---

## 0. One-paragraph design

The trust graph is an **override layer over the existing 9-tier ladder, not a replacement for it**. It does not assign every source a new number. A deployment declares a small set of **domain-scoped pairwise edges** in config, each saying "in domain D, source A outranks source B". A domain is selected by (entity type, field). Where no edge applies, the comparison falls back to today's `precedence.source_rank`, so behaviour stays byte-identical. Edges between the same pair that conflict yield **incomparable**, which escalates. It never silently picks a winner, which follows the "undeclared is incomparable, never lesser" rule in `asserter_authority.py:34-41`. Attestation strength from athenaeum#2144 is used **only to break ties or to demote**. It is joined in at decision time through `witness_ref` and is never persisted as a weight. It is only trusted after athenaeum verifies it itself (see §3 and §4).

## 1. Problem, and the decision it serves

Today a claim's trustworthiness is one global rank per source type. That rank is `precedence.SOURCE_PRECEDENCE_TIERS` (`src/athenaeum/precedence.py:39-49`), read through `source_rank` (`precedence.py:66-85`). It is mirrored as prose in the `SOURCE-PRECEDENCE TAXONOMY` block of `resolutions._RESOLVE_SYSTEM` (`src/athenaeum/resolutions.py:608-629`), and four drift-guarded copies are bound by `tests/test_precedence.py` (`resolutions.py:424-446`). Under that model a `user:` claim beats an `api:` claim on every subject. The operator's framing in athenaeum#2145 contradicts this: a government database beats the user on his own payout figures.

Below is each place where a trust weight would change behaviour today, with what it would change.

| # | Seam | What it decides today | Would a trust edge change it? |
|---|---|---|---|
| 1 | **Tier-0 field corrections.** `corrections.decide_verdict` (`src/athenaeum/corrections.py:880`). Rank compare at `:915-929`. Called from `process_correction_record` (`:1148`) at `:1643`. Entity type (`resolution.entity_type`, `:1514`/`:1522`) and field (`write_field`, `:1553`) are both in scope at the call site. | apply / defer / escalate on a single field value, by `source_rank` alone, then an `observed_at` tie-break. | **Yes. This is the primary seam.** It is deterministic, it already holds the (entity type, field) domain key, and it is where "the government database beats the user on payout" would actually flip a verdict. |
| 2 | **Tier-0 list removal.** The `op == "remove"` branch in `process_correction_record` (`corrections.py:1719-1741`). | Removal is allowed when `incoming_rank <= existing_rank`. Monotone unset needs rank 1. | Yes, the same compare. The **monotone-unset guard stays keyed on rank 1** (`:1730-1734`, `:919-922`). An edge never grants monotone unset. |
| 3 | **LLM resolver.** `_RESOLVE_SYSTEM` (`resolutions.py:449`), taxonomy at `:608-629`. The per-pair user message is built by `_build_user_message` (`:1116`), which already passes each member's `source` and every `field_sources` key (`:1177-1214`). | keep_* / correct_* winner selection, done by the model. | Yes, but only by handing it data. Matching edges are rendered into the per-pair user message. The deployment's edges never go into the shared system prompt. This **changes LLM behaviour**, so it needs a paid eval (§6, TG4). |
| 4 | **Auto-supersession.** `supersession.decide_supersession` (`src/athenaeum/supersession.py:451`). Routes (b) and (c) at `:538-551` use `asserter_authority.compare_authority` (`asserter_authority.py:143`). | Whether an asserter **may** supersede, by declared grants. | **No, by design.** Authority (what an asserter may do) and trust (which claim is likely correct) are separate axes. See the boundaries in §2. |
| 5 | **Recall ranking.** RRF fusion, `search.reciprocal_rank_fusion` (`src/athenaeum/search.py:3439-3530`). `mcp_server.py:1031-1041` reads `source_type` only to render a `**Source:**` display line. | Relevance only. No source or precedence term affects ordering. | **No seam, and none should be added.** North-star §2.6 ("facts, not verdicts") says to expose provenance as fields and not to reorder by trust. A possible later slice could surface the effective comparison as a fact. That is deferred (§6). |
| 6 | **Attribution verification.** `pii_attribution._check_c3`, C3 source context (`src/athenaeum/pii_attribution.py:1070`). The tier seam is design §6.4. | **Whose** contact value it is. | No. Attribution decides which entity a value belongs to. The trust graph decides which of several competing values for the same (entity, field) wins. A value only reaches the trust comparison after attribution has placed it. |
| 7 | **Adjudication provenance.** athenaeum#2163 (open, ~planning). | Resolution records point at the witness line of the human answer. | Later, as a consumer only. Once athenaeum#2163 lands, a human adjudication's attestation is input like any other `witnessed:` claim. The trust graph adds nothing to athenaeum#2163. |

**Where the operator's three examples actually land:**

- "The operator is the best source on facts about himself." No edge is needed. `user:` and `witnessed:` are already rank 1 (`precedence.py:40`). The trust graph must simply not break this. It is a default-preserving invariant, not a feature.
- "A government DB beats the operator on payout figures." This is an edge in domain `(person, <payout field>)`: `api:<govdb>` over `user`. It is a **demotion of `user:` within one domain**, and it flips verdicts at seams 1 and 3.
- "Claude's inference beats the operator's opinion on code Claude wrote." The opinion half is already handled: opinion pairs go to `attribute_both` and are never resolved by precedence (`resolutions.py` prose; conflict-resolution.md §13). The factual half is an edge in a domain such as `(tool | project, <code-detail field>)`: `agent-observed` over `user`. Whether "code Claude wrote" can be expressed as (entity type, field) is open question OQ2.

## 2. Boundaries

### 2.1 What the trust graph owns

- The **edge vocabulary and resolver**: a pure function `compare_sources(a, b, domain, edges) -> outranks | outranked | equal | incomparable`, with a reason. It is four outcomes, mirroring `asserter_authority.AUTHORITY_RELATIONS` (`asserter_authority.py:83-85`).
- The **config shape** for edges, the resolver for it, and the validation.
- The **wiring** into seams 1, 2 and 3, behind a default-off flag.
- **The attestation tie-break policy**: how verified attestation affects a comparison that is otherwise equal. It does not own producing the attestation.
- A **dry-run flip report**: what would change if the edges were enabled.

### 2.2 What it does not own

| Concern | Owner | Why separate |
|---|---|---|
| The 9-tier default ladder, and its four drift-guarded copies | `precedence.py` and `resolutions._RESOLVE_SYSTEM`, locked design (athenaeum#797, athenaeum#2159) | The trust graph falls back to it unchanged. No fifth hand-edited copy of the tier list is created. |
| Producing witness lines, signatures and anchors | promptwitness (`src/promptwitness/`, moving to `Kromatic-Innovation/promptwitness`) | Producer side. The trust graph never imports `promptwitness`. |
| Joining witness lines to utterances, and writing receipts (`witness_strength`, `witness_anchored`) | `src/athenaeum/witness_attestation.py` (athenaeum#2158) and `transcript_receipts.py` | Intake attestation. TG5 hardens its outputs. The trust graph only reads them, through an adapter. |
| Asserter authority (may act) | `asserter_authority.py` (athenaeum#715), consumed by `supersession.py` | A different question: permission, not likelihood of being correct. Merging the two would let a trust edge confer destructive rights. |
| Whose value it is | attribution verification (athenaeum#2192) | It runs before the trust graph. See seam 6. |
| Which entity a claim is about | `dimensions.SUBJECT` (`src/athenaeum/dimensions.py:441-447`), a **minted entity id** (`subject_population.py` docstring) | This is not the trust graph's "subject domain". A domain is a *class* of facts: entity type plus field. A subject is *which thing*. Keep them separate. |
| Reordering recall | nobody (north-star §2.6) | See seam 5. |

### 2.3 Data model sketch

```yaml
# athenaeum.yaml  (yaml only: like the grant graph, it has no sane env encoding)
librarian:
  source_trust_enabled: false        # master flag, default off
  source_trust_edges:                # default [] (inert)
    - domain: {entity_type: person, field: payout_amount, evidence_kind: document}
      over:   "api:govdb"            # source selector: type, or type:ref
      under:  "user"                 # source selector
      note:   "a documentary government ledger beats self-report on payout figures"
    - domain: {entity_type: [tool, project], field: implementation_detail}
      over:   "agent-observed"
      under:  "user"
      # no evidence_kind: applies regardless of evidence kind (including unknown)
```

- **Where the edges live: in config.** They are deployment data, not shipped code. Athenaeum ships to PyPI with no operator identity (`owner.py` docstring; north-star §2.10). The key name follows the existing graph-in-config family, `librarian.authority_grant_implications` (`config.resolve_authority_grant_implications`, `src/athenaeum/config.py:7915`): yaml only, default empty, defensive parsing. The resolver is `config.resolve_source_trust_edges`, and every nested segment is read inline (the parity-gate convention from athenaeum#2144's config notes).
- **Source identity.** The `SourceRef` type token from `provenance.parse_source`, optionally narrowed to `type:ref`. **Every selector of type `api` must name a ref.** This narrows the rules escalation described in §4, F5, but does not close it.
- **Domain.** `entity_type` is the page's `type:`. `field` is the frontmatter field, or the `field_sources` key on the LLM path. `evidence_kind` (OQ2) is the claim's evidence kind, from the closed vocabulary `document` / `verbal` / `authored-by-agent` (see §2.3.1). All three keys accept a scalar or a list. An absent key means "any" — including an absent `evidence_kind`, which is behaviour-identical to the (entity type, field) design with no evidence-kind selector. This is **not** the `subject` dimension.
- **Attestation strength.** It is **computed at decision time, never persisted as a weight**. The join is memory `witness_ref` (written by `frontmatter_edit.witnessed_source_fields`, `src/athenaeum/frontmatter_edit.py:130-174`, which writes **no** strength) to the receipt fields (`transcript_receipts.py:297-300`), read through a small athenaeum adapter. A persisted weight would go stale when keys rotate or anchors upgrade.
- **Comparison semantics.** Collect the edges whose domain matches and that relate A and B in either direction. If exactly one direction is found, return outranks or outranked. If both directions are found, return incomparable, which maps to `escalate` at seam 1. If none is found, fall back to `source_rank` and today's logic. **Edges are not transitive in v1.** No closure is taken over A>B, B>C, because an implied A>C in a domain the operator never wrote is a silent wrong weighting. A later slice can add closure, cycle-safe like `grant_closure` (`asserter_authority.py:118-140`), if the operator wants it.

### 2.3.1 Evidence-kind vocabulary, provenance, and the unknown rule (OQ2)

**Vocabulary.** An operator edge's `evidence_kind` selector is a closed, three-member vocabulary: `document`, `verbal`, `authored-by-agent`. Config load (`config.resolve_source_trust_edges`, following the same "every nested segment read inline" convention as `config.resolve_authority_grant_implications`, `src/athenaeum/config.py:7915`) rejects any `evidence_kind` value outside this set. `unknown` is a **claim-side value only** — a claim whose evidence kind cannot be determined at a given seam carries `unknown`, but an edge may never *name* `unknown`; config load rejects an edge that tries to.

**Unknown-kind matching rule.** An edge that names one or more evidence kinds does **not** match a claim whose evidence kind is `unknown`. Only an edge whose `evidence_kind` key is absent (meaning "any") matches an unknown-kind claim. Justification: an operator who wrote `evidence_kind: document` authorized that edge over documentary evidence specifically — not over evidence this build failed to classify. The safe direction on an unclassifiable claim is to fall back to the ordinary 9-tier ladder (by `compare_sources` finding no matching edge, per §2.3's comparison semantics), never to let a narrower-scoped override apply to a wider, unverified population by accident.

**Where each kind comes from, at each seam.** This uses §1/§2.1's canonical numbering (seam 1: tier-0 field corrections; seam 2: tier-0 list removal; seam 3: the LLM resolver) — the three the trust graph wires into (§2.1: "The wiring into seams 1, 2 and 3"). None of the three thread an evidence-kind-shaped value through today. Each is examined below, in that order; where no field exists, the claim's evidence kind is `unknown` at that seam until the named follow-up lands — no field is invented and presented as already wired. The attestation tie-break is examined separately, after the three: it is not one of these three wiring seams — it is the TG6 consumer §2.1 scopes as "The attestation tie-break policy".

- **Seam 1 — tier-0 field corrections (`src/athenaeum/corrections.py`, fed by compiled records from `src/athenaeum/rules.py`).** The compiled correction dict (`rules.py:918-927`) carries `record`, `target`, `op`, `field`, `value`, `source`, `observed_at`, `note` — no evidence-kind-shaped field — and neither `decide_verdict` (`corrections.py:880`) nor `process_correction_record` (`corrections.py:1148`) ever reads `meta.get("source_type")` (zero occurrences of `source_type` in `corrections.py`). The *existing* claim's frontmatter may already carry `source_type` (one of `models.SOURCE_TYPES`: `user-stated`, `agent-observed`, `external`, `document`, `inferred`, `model-prior`, `witnessed` — `src/athenaeum/models.py:86-96`), but nothing threads it into the comparison. **Evidence-kind at seam 1 is `unknown` today**, for both sides of every correction. Named follow-up (TG2/TG3): add an `evidence_kind` field to `rules.CorrectionSpec` (`src/athenaeum/rules.py:545`) and the compiled dict at `rules.py:918-927`, sourced from the rule author's declared evidence kind (a shape rule inherently knows whether it transcribes a document, a verbal utterance, or an agent's own observation); read `meta.get("source_type")` for the incumbent claim in `corrections.decide_verdict`/`process_correction_record` and map it through the table below; do the same for any CLI/API ad-hoc correction-submission path.
- **Seam 2 — tier-0 list removal (`src/athenaeum/corrections.py`, the `op == "remove"` branch in `process_correction_record`, `:1719-1741`).** Removal is allowed when `incoming_rank <= existing_rank` (`:1735`). The existing side's source for the specific list value comes from `_existing_list_source_for_value` (`corrections.py:827-832`), which returns only that `field_sources.<field>` entry's `source` key (or the page-level `source:` as a fallback) — never a `source_type`/evidence-kind-shaped key. The incoming side only ever supplies the same compiled `source` field as seam 1 (`rules.py:918-927`); there is no separate record shape for `remove`. **Evidence-kind at seam 2 is `unknown` today**, for both sides, for the same reason as seam 1 — no field carries it. Named follow-up: the same additions proposed for seam 1 (`evidence_kind` on `rules.CorrectionSpec`/the compiled dict, and reading `meta.get("source_type")` off the matched `field_sources` entry here) close this seam too, since both `process_correction_record` branches share the one compiled-record shape.
- **Seam 3 — LLM resolver (`src/athenaeum/resolutions.py`).** `_read_member_sources` (`resolutions.py:1091-1113`) reads only `source` and `field_sources` off a member's frontmatter; it never reads `source_type`, so `_build_user_message` (`resolutions.py:1116`) renders no evidence kind into the per-pair message today. (The module's only two `source_type` reads, at `:2597` and `:2664`, belong to the unrelated `_transcript_authorizes_correct` auto-apply gate, which deliberately does NOT trust a self-declared `source_type` — see that function's docstring — and never feeds the resolver's message.) **Evidence-kind at seam 3 is `unknown` today.** Named follow-up (TG4): extend `_read_member_sources` to also return `meta.get("source_type")`, map it through the table below, and render the result into each member's block in `_build_user_message`.

**TG6 attestation consumer (`src/athenaeum/witness_attestation.py`) — not one of the three wiring seams above.** This one differs: it is derivable **today, with no new field**, because of what attestation *is*. The promptwitness ledger only ever witnesses a session utterance — there is no ledger line for ingesting a document, or for an agent's own unwitnessed observation. Concretely, `frontmatter_edit.witnessed_source_fields` (`src/athenaeum/frontmatter_edit.py:130-174`) writes `source_type: witnessed` (`:169`; `WITNESSED_SOURCE_TYPE = "witnessed"` at `:72`) onto exactly the claims `AttestationIndex`/`AttestationOutcome` (`witness_attestation.py:184`, `:434-441`) can ever attach a `witness_ref` / `witness_strength` / `witness_anchored` to, and `models.py`'s channel-split comment (`models.py:66`, `:78-85`) defines both `user-stated` and `witnessed` as a human utterance in a session — verbal by definition. **Evidence-kind for the attestation tie-break is therefore `verbal` by construction** for every claim it can reach: TG6's rule is simply "a claim eligible for the attestation tie-break has `evidence_kind: verbal`", with no new field required.

**Candidate mapping (for the TG2/TG3/TG4 follow-ups above — not implemented anywhere today):**

| existing `source_type` (`models.SOURCE_TYPES`, `models.py:86-96`) | closed `evidence_kind` |
|---|---|
| `document` | `document` |
| `user-stated`, `witnessed` | `verbal` |
| `agent-observed`, `inferred`, `model-prior` (= `models.AI_ATTRIBUTED_SOURCE_TYPES`, `models.py:104-106`) | `authored-by-agent` |
| `external` | no clean mapping — the follow-up that wires this seam must decide; until it does, `unknown` |
| absent / unparseable | `unknown` |

### 2.4 Repository split

The trust graph belongs in **athenaeum**. It is a store-integrity rule over athenaeum's own precedence seams, and like athenaeum#2192 §2.3 no sibling repository decides between competing values. **Nothing goes into promptwitness.** promptwitness produces attestations and has no notion of sources, domains or precedence. Putting trust policy there would break its zero-athenaeum-knowledge boundary (athenaeum#2144: "no operator paths ... no Kromatic identifiers"). Any slice that does need a promptwitness change targets `Kromatic-Innovation/promptwitness` and is blocked_by `Kromatic-Innovation/promptwitness#1`. None of the proposed slices needs one.

### 2.5 Principles applied

- **North-star §2.7: rules are data, and humans adopt them.** Edges are operator-authored config. The librarian never invents an edge. A future "propose an edge from repeated corrections" slice would go through the existing proposal queue.
- **North-star §2.2: measured, not climbed.** The LLM path changes only after a paid eval shows it helps.
- **North-star §2.6: facts, not verdicts.** Recall is not reordered.
- **Derive, don't transcribe.** No new copy of the tier list is created, and the fallback calls `source_rank` itself. The resolver-path rendering is generated from the same edge data the deterministic path reads.
- **Prompt text is content.** The one sentence the system prompt needs ("honor DOMAIN TRUST OVERRIDES when present") is a change to the shipped prompt. It goes through `prompt_registry`, the golden and `docs/design/prompts.md`, and is never assembled inline. The per-pair override block is a data fragment assembled from variables. That is the permitted inline case, and its justification is commented at the site.
- **Incomparable, never silently decided.** This follows `asserter_authority.py:34-41`.

## 3. The promptwitness dependency, precisely

Most of athenaeum#2144's athenaeum-side data path **has already shipped**. athenaeum#2147, athenaeum#2149, athenaeum#2150, athenaeum#2151, athenaeum#2152, athenaeum#2156, athenaeum#2040, athenaeum#2157, athenaeum#2158, athenaeum#2159 and athenaeum#2160 are all closed. "Depends on promptwitness" therefore means four narrower things.

| Gap | Evidence | Effect on the trust graph | Blocks |
|---|---|---|---|
| **G1. Strength is self-asserted.** | `AttestationIndex.load` calls `ledger.verify_chain(witness_dir)` with no `public_keys` (`src/athenaeum/witness_attestation.py:222-223`). `verify_chain` then does **no signature checking** (`src/promptwitness/ledger.py`, `verify_chain` docstring). `witness_strength` is copied straight from `line.key.strength` (`witness_attestation.py:438`). | Any consumer of `witness_strength` trusts a field that the line's writer chose for itself. | TG6 must wait for **TG5**, which is athenaeum-side. promptwitness already accepts `public_keys`, so no promptwitness change is needed. |
| **G2. "Anchored" includes local-clock.** | Every anchor pass appends a `local-clock` anchor line (`src/athenaeum/witness_anchor.py:206-208`; `src/promptwitness/cli.py:606-615`). `AttestationIndex.load` counts **any** `kind == "anchor"` line (`witness_attestation.py:225-237`). `witness_anchored` is a bool with no source (`:439`). | `witness_anchored=True` can mean only that the machine claimed a time. athenaeum#2144's own table says this proves "Anything: nothing." | TG6 must wait for **TG5**, which records the anchor *source* (best non-`local-clock`) and, where available, the `verify_source`. |
| **G3. No host data.** | The ledger is not enabled on the host until athenaeum#2161 (open, ~operator, needs:host-write), which builds on athenaeum#2235 (open, ready). | Every claim in production has attestation `none`. TG6 is inert until then, though it can be built and tested on fixtures. | TG6 *ships* without it. *Enabling it on host* is blocked_by athenaeum#2161. That is an operator step, not a code edge. |
| **G4. The hardware strengths do not exist.** | `promptwitness.signing.STRENGTHS` lists `hardware-nonexportable` and `hardware-presence`, but its comment says "the two hardware tiers do not [have adapters] yet" (`src/promptwitness/signing.py:100-110`). The adapters are athenaeum#2153 and athenaeum#2154 (ready), with device checks athenaeum#2239 and athenaeum#2240 (~human). Ruling 1 declines presence. | Any rule keyed on hardware strength is untestable against real data. And even hardware does **not** prove the hook signed (athenaeum#2144 table). | **None.** TG7 (the hardware-strength weight this gap would have blocked) is **dropped per OQ1** (§6, §8 ruling 1: attestation never overrides rank), so athenaeum#2153, athenaeum#2154, athenaeum#2239 and athenaeum#2240 no longer gate anything in this epic. G4 remains a true statement about promptwitness's current state — it just no longer blocks a proposed slice. |

**Extraction (athenaeum#2162: `Kromatic-Innovation/promptwitness#1`, then athenaeum#2292, then athenaeum#2293) is a non-event for the trust graph** as long as trust code never imports `promptwitness`. Trust code reads only receipt fields, through an athenaeum adapter. If an ordering over strengths is needed, it lives in that adapter, built from the strings, with a test that checks membership against `promptwitness.signing.STRENGTHS`. That test sits in athenaeum's test suite and survives athenaeum#2293, because athenaeum depends on the published package.

**What can start now, with attestation fixed at `none`:** the design doc (TG1), the edge model and pure resolver (TG2), the tier-0 wiring with a dry-run report (TG3), the LLM-path rendering (TG4, after an eval) and the attestation hardening (TG5). **Only TG6 consumes attestation. No proposed slice needs promptwitness work that has not shipped** — TG7, the one slice that would have, is dropped per OQ1 (§6, §8).

## 4. Failure classes and how each is detected

| # | Failure class | Detection / guard |
|---|---|---|
| F1 | **Silent wrong weighting.** An edge matches a domain broader than intended (for example `field` omitted means "any"), or an implied transitive edge flips verdicts nobody wrote. | No transitivity in v1. TG3 ships a **dry-run flip report** with the flag off: every correction whose verdict *would* change, with the matching edge. Enabling is gated on the operator reading it (the athenaeum#2146 dry-run-count precedent). Each verdict's `reason` names the edge, so it is auditable after the fact. |
| F2 | **Contradictory edges** (A>B and B>A in overlapping domains). | They resolve to `incomparable`, which maps to `escalate`. A load-time lint warns about overlapping contradictory edges. Neither direction is chosen silently. |
| F3 | **Default drift.** The trust layer quietly changes behaviour with no edges configured. | A property test: with `edges=[]`, or with the flag off, `compare_sources` agrees with `source_rank` for every token pair in `SOURCE_PRECEDENCE_TIERS`. The test is parsed from the tuple, not transcribed. `decide_verdict` output is byte-identical over the existing test corpus. |
| F4 | **Self-asserted attestation strength** (G1). | TG5 verifies signatures against pinned public keys. Effective strength is `none` unless the signature verifies under a key whose registered strength is at least the claimed strength. A mismatch (claimed above registered) is surfaced as a provenance incident through the existing athenaeum#2158 miss path. |
| F5 | **Machine-tier escalation through rules.** `rules.MACHINE_TIER_SOURCE_TYPES = {script, api}` (`src/athenaeum/rules.py:148-152`). An edge `api:<vendor> over user` would let an adopted shape rule that declares `source: api:<vendor>` beat `user:` in that domain. **This is not closed today.** The general compile path emits the rule's declared source verbatim (`rules.py:924`, `"source": spec.source`). Only the preserved-log path swaps `ref` for a log pointer (`rules.py:2445-2465`). | `api` selectors must name a ref, which narrows the problem but does not close it. **TG3 must add an explicit guard:** an edge never promotes a rule-compiled correction over a `user:` or `witnessed:` incumbent. TG3 has to identify or add a structured rule-origin marker on the correction record. Today it carries only a free-text `note` ("compiled by shape rule ...", `rules.py:915-916`), which is not a safe key. A test must cover both emit paths. |
| F6 | **Stale attestation.** Keys rotate, anchors upgrade from pending to confirmed, or a chain break is found later. | Attestation is joined at decision time, not persisted. TG6 re-reads the receipt each time. `chain_intact_through` (`witness_attestation.py`) failing yields effective strength `none`. |
| F7 | **Local-clock counted as an anchor** (G2). | TG5 replaces the bool with an anchor source. TG6 treats `local-clock` as no anchor. |
| F8 | **Opinion resolved by trust.** | Opinion pairs keep going to `attribute_both`. TG4's rendering is suppressed when both sides are `claim_kind: opinion`. There is a test for it. |
| F9 | **Monotone-unset bypass.** An edge lets a non-user source clear a field. | The guard stays keyed on `source_rank == 1` (`corrections.py:919-922`, `:1730-1734`). Edges are never consulted for monotone ops. There is a test for it. |
| F10 | **LLM ignores or over-applies overrides.** | A paid eval with a fixed pair set (TG4) measures win-selection accuracy against hand labels with and without the override block before the flag can be enabled. `source_precedence_used` (`resolutions.py:654`) must cite the edge, and its absence is counted. |
| F11 | **Drift-guard erosion.** Someone "simplifies" by editing the tier list. | It stays out of scope. The trust graph never edits `SOURCE_PRECEDENCE_TIERS`. The existing `tests/test_precedence.py` binding is unchanged. |

**Test oracles later slices must pin (F3, F5, F8, F9).** These four failure classes above are singled out because each names an invariant a later slice's test suite must encode, not merely a risk to keep in mind:

- **F3 (default drift).** With `source_trust_edges=[]` or the flag off, `compare_sources` must agree with `source_rank` for every token pair in `SOURCE_PRECEDENCE_TIERS`, and `decide_verdict` output must stay byte-identical over the existing test corpus.
- **F5 (machine-tier escalation through rules).** An edge must never let a rule-compiled correction (no structured rule-origin marker exists yet on the correction record) outrank a `user:` or `witnessed:` incumbent; a test must cover both the general compile path (`rules.py:924`) and the preserved-log path (`rules.py:2445-2465`).
- **F8 (opinion resolved by trust).** An edge-driven override must never fire when both sides of a pair are `claim_kind: opinion` — those stay on `attribute_both`.
- **F9 (monotone-unset bypass).** No edge may ever grant monotone unset; that guard stays keyed on `source_rank == 1` alone (`corrections.py:919-922`, `:1730-1734`), with edges never consulted for monotone ops.

**Stale comment found:** `corrections.py:900-903` says "`user` occupies precedence rank 1 alone". Since athenaeum#2159 `witnessed` shares rank 1. The logic is unaffected because it compares ranks, but TG3 should correct the comment, since it is exactly the kind of transcribed claim that becomes a wrong test oracle.

## 5. Rejected alternatives

1. **A single global source ranking** (re-tune the 9 tiers). Rejected because the operator's own examples need the same pair to rank in opposite orders in different domains, which no total order can express.
2. **Numeric Bayesian trust weights per (source, domain).** Rejected because self-reported or guessed numbers do not rank correctness (conflict-resolution.md, five-verdict comparator section: 0.84 and 0.82 were both wrong, 0.77 was right). Pairwise edges are auditable. Weights are not.
   - **There is a tension the operator should rule on (OQ3).** The recorded operator principle `wiki/auto-bayesian-prompt.md` ("Bayesian trust evaluation doesn't require infrastructure. It's a prompt instruction to the librarian.") argues against numeric weights. It argues just as hard that the **LLM path (TG4) should be primary** and that config machinery should stay minimal.
   - The case for deterministic edges is narrower than "deterministic is better". The tier-0 field-correction applier cannot call an LLM (`precedence.py` module docstring, field-corrections.md §6.1), so a deterministic mirror is needed **only for seams 1 and 2**.
   - This spec does not decide between deterministic-first and prompt-first. OQ3 does.
3. **Replacing the tiers with a full graph that must cover every pair.** Rejected because it forces the operator to author O(n²) edges, discards a locked and drift-guarded design, and makes "no edge" undefined. An override layer with fallback keeps the default intact.
4. **Putting edges into `_RESOLVE_SYSTEM` prose.** Rejected because edges are deployment data (PyPI, §2.10), and doing so would churn the golden and the prompt cache on every edit and create a fifth drift copy.
5. **Folding trust into `asserter_authority`.** Rejected because permission and correctness are different axes, and it would let a trust edge confer supersession rights (`supersession.py:538-551`).
6. **Keying domain on `dimensions.SUBJECT`.** Rejected because subject is a per-thing minted id, and trust is about classes of facts. Keying on subject would need one edge per person.
7. **Giving `witnessed` its own tier above `user:`.** It was already declined by ruling 9 (athenaeum#2159). Whether attestation can ever *promote* is OQ1.
8. **Persisting attestation strength into memory frontmatter.** Rejected because it goes stale on key rotation or anchor upgrade, and it becomes a second source of truth beside the receipt.


## 6. Child slices (as decided)

Each slice is roughly one session. Epic athenaeum#2145 is `should`. "Edges" means native `blocked_by`. Slice scope and acceptance criteria are in each child issue.

- **TG1. Design doc `docs/design/trust-graph.md`.** should, ready. Edges: none. Defines the evidence-kind vocabulary and its source.
- **TG2. `trust_graph` module: edge config resolver and the pure four-outcome `compare_sources`, with evidence-kind support.** should, ready. blocked_by TG1.
- **TG3. Wire trust edges into tier-0 field corrections behind a default-off flag, with a dry-run flip report.** Includes the shape-rule `api:<vendor>` guard (F5) and the stale-comment fix at `corrections.py:900-903`. should, ~operator. blocked_by TG2.
- **TG4. LLM resolver: render matching trust edges into the per-pair message.** could, ~planning. blocked_by TG2. Its paid eval and pass bar are decided later by the operator.
- **TG5. Witness attestation: verify signatures against operator-declared keys and record the anchor source.** Filed under epic athenaeum#2144, should, ready. Edges: none. No promptwitness change.
- **TG6. Trust tie-break and demote-on-broken-chain on verified attestation.** could, ready. blocked_by TG3 and TG5. Enabling it on host additionally waits on athenaeum#2161 (operator step, not a native edge).

**Dropped:** TG7 (weight for hardware-nonexportable attestation), per OQ1 (attestation never overrides rank).

**Deferred, not filed:** surface the effective trust comparison as a recall or `read_entity` fact (north-star §2.6); edge proposals from correction history (librarian proposes, human adopts, §2.7).

**Edge graph**

```
TG1 ──> TG2 ──> TG3 ──┐
           └──> TG4   ├──> TG6   (enable-on-host waits on athenaeum#2161 → athenaeum#2235)
TG5 ──────────────────┘
```

No slice touches promptwitness, so nothing is blocked_by `Kromatic-Innovation/promptwitness#1` or athenaeum#2293.

## 7. Migration and rollback (the brief's "open questions" item is §8)

- Everything is behind `librarian.source_trust_enabled` (default false) and `source_trust_edges` (default `[]`). Either one off gives byte-identical behaviour, pinned by the F3 tests. Rollback is turning the flag off. Verdicts already applied under an edge are ordinary corrections with an edge-naming reason, so they can be found and reverted through the existing correction history.
- TG5 is an additive receipt field. Old receipts read as unverified, which is the safe direction.
- The 9-tier ladder and its drift guards are never edited, so there is no lock-discipline change on the tier list itself. TG3 and TG4 change *how the ladder is consulted*, and that is why they carry ~operator and eval gates.


## 8. Resolved decisions (operator rulings, 2026-11-05)

Rulings: https://github.com/Kromatic-Innovation/athenaeum/issues/2145#issuecomment-6667213406 and https://github.com/Kromatic-Innovation/athenaeum/issues/2145#issuecomment-6666833163

1. **OQ1: attestation is tie-break plus demote-on-broken-chain only.** It never overrides rank and never promotes across tiers. Consequence: TG6 is scoped to tie-break and demote, and TG7 (a hardware-strength weight) is dropped and not filed.
2. **OQ2: the v1 domain key is entity type + field + an evidence-kind selector.** Evidence kinds are such things as `document`, `verbal` and `authored-by-agent`. Consequences:
   - The domain in §2.3 becomes `{entity_type, field, evidence_kind}`. Each key accepts a scalar or a list, and an absent key means "any". This is still not the `subject` dimension.
   - TG1 must define the evidence-kind vocabulary and where each kind comes from at the decision seams (a concrete field on the correction record or on memory frontmatter, with the code seam cited), and the rule for an unknown kind.
   - TG2's comparison and config must take the evidence kind as an input, match on it, and validate it against the vocabulary.
   - With no `evidence_kind` in any edge, behaviour is identical to the (entity type, field) design.
3. **OQ3: deterministic first.** TG2 then TG3 are the build path. TG4 (LLM resolver) is filed at `moscow:could`, lifecycle `~planning`. Its paid eval and pass bar are decided later by the operator.
4. **OQ4: TG5 is filed under epic athenaeum#2144 (promptwitness)** at `moscow:should`, not under athenaeum#2145.

