# Attribution verification: checking whose contact value it is before filing it

**Status:** DESIGN, accepted for build under epic athenaeum#2192. It implements
north-star §2.12 ("Filing is verification") and the amended §2.2 resolution
rule (measured tiers, calibrated thresholds, human last) for one value class:
contact values (email addresses and phone numbers) on person records. Both
sections were ratified on 2026-10-30 and are in `docs/north-star.md`
(athenaeum#2208). The child issues are listed in §11.

## 1. The problem in one paragraph

Every writer that puts a contact value on a person's record assumes the value
is that person's. Nothing checks. An operator audit of the shared-address
population on one live deployment found hundreds of (address, uid) pairs where
one address sat on two or more uids. Dozens were third-party addresses filed
on the wrong person: an introducer's address on the introducee, a referrer's
address on everyone they referred, a platform reply-relay sender on every
contact that arrived through that platform, the deal contact's address on a
page named after their company, a campaign page holding every recipient, and
an organiser's page holding their colleagues. The rest were legitimate:
role inboxes on their owner's own domain, and one person recorded under two
surnames. One misfiled value carried the `observed` usage class, which makes
an address outreach-eligible. Outreach to the introducee would have gone to
the introducer.

The values came in through four writers, none of which has an attribution
check, and stayed because page deletion does not clean up the contact record.

## 2. Boundaries

### 2.1 What this design owns

- One verification seam that every contact-value writer calls, including the
  librarian's own maintenance paths (§4).
- The ordered deterministic check catalogue (§5).
- Escalation of what the checks cannot settle to a measured model tier, and
  filing of the result: re-attribution, alias, same-entity merge proposal, or
  confirmation (§6, §7).
- The human queue for what stays below calibrated confidence (§8).
- Observe mode first, then enforce, with the flip gated on measurement (§9).
- Three writer-side defects: the correspondent shortcut, prose-compile address
  leakage, and contact records orphaned by page removal (§10).
- A read-only surface lint and attribution report (§10.4).
- The calibration eval that sets tiers and thresholds (§12).

### 2.2 What it does not own

| Concern | Owner | Why it is separate |
|---|---|---|
| Fold carrying classification entries for unlisted identifiers | athenaeum#2196 | A fold-correctness bug that predates attribution. It changes `contact_record_fold.py` and the corrections `remove` branch, which the seam is then wired into. It lands first. |
| Data that does not fit the configured page types (schema proposals), and `##` section drop | athenaeum#2197 | North-star §2.7, not §2.12. It shares no code with this design. Its only coupling is that org-named "person" pages distort its misfit counts, and the target-type check (§5, D3) is what identifies them. |
| Producer-side provenance (correspondent name, header role, message id, mentioned people) | voltaire#338 (closed) | A producer courtesy under north-star §2.9. Richer provenance makes the checks cheaper (§5, D5 and C3). It never replaces them. |
| Importer fixes (first-address-on-the-deal-box aliasing, referrer addresses in free text, relay deny-lists) | the importer's own repository | Producer side. This design catches what an importer gets wrong. It does not fix the importer. |
| Running the report over a real store, working the suspect list, and changing existing values | operator | Capability ships here; its use on a live deployment is operator-side and untracked. |
| Provider adapters beyond Anthropic | athenaeum#1101 (wont) | The tier seam is provider-agnostic (§6.4). Only Anthropic models are measured for now. |
| Raw addresses embedded in existing escalation descriptions | follow-up issue | `librarian._record_correspondent_contact` writes `ref.address!r` into `_pending_questions.md`, which lives in the wiki. Child A fixes the two sites it touches. A repository-wide sweep is a separate issue. |

### 2.3 Repository split

Everything lives in `athenaeum`. The seam is a store-integrity rule, and the
store is athenaeum's. No sibling repository holds a contact-value writer that
bypasses athenaeum: the importers and the inbox agent write raw intake, and
athenaeum files it. The one cross-repository contract, the frontmatter fields
a producer may stamp, is already owned by voltaire#338 and the
field-corrections envelope.

## 3. Principles applied

- **One way in** (north-star §2.1, `docs/design/one-way-in-one-way-out.md`
  §4.1). The seam lives in `athenaeum.pii`, the only module that may touch the
  excluded contacts surface. No caller opens the surface itself, and the
  librarian is not an exception.
- **Filing is verification** (§2.12). Every writer calls the seam, including
  migrations, folds, repairs, the prose compile, and the re-attribution write
  itself.
- **Deterministic first, then the measured tier, then a human** (§2.2). A
  model call happens only on a value the deterministic checks marked
  `suspect`. Which model answers is read from measurement, never climbed by
  price.
- **Nothing is refused at the gate** (§2.2, §2.3). A failed check dispositions
  the value: it is placed, recorded as non-personal, or parked for placement.
  It is never dropped. Quarantine is for what cannot be placed, never instead
  of placing it.
- **A model never grants outreach eligibility.** A value placed by reasoning
  is filed `unclassified`. Only header-verified correspondence earns
  `observed` (`pii.USAGE_CLASS_OBSERVED`), and that happens in the existing
  correspondent path, not here.
- **No contact value enters the wiki.** Suspects, the attribution log and
  verdict memos hold values, so they live on the excluded surface. Anything
  written to the wiki (queue items, run summaries, ledgers) carries masked
  values, uids and check ids only.

## 4. The seam

### 4.1 Signature

```python
# athenaeum.pii
def check_contact_attribution(
    value: str,
    *,
    target_uid: str,
    target_name: str | None,
    target_type: str | None,
    source_ref: str | dict | None,
    writer: str,                      # "observed-contact", "correction-add",
                                      # "pii-migration", "contact-fold",
                                      # "prose-compile", "re-attribution"
    context: AttributionContext,      # indexes built once per run (§5.2)
) -> AttributionVerdict: ...
```

`AttributionVerdict` is a frozen dataclass:

- `disposition`: one of `pass`, `non-personal`, `suspect`.
- `checks`: the ordered list of check ids that fired, each with its evidence
  (uids, the label found, the matched name). Evidence carries uids and labels,
  never the value.
- `candidates`: uids the evidence points at as possible owners.
- `value_key`: a keyed hash of the normalised value, for logs and memos.

`non-personal` replaces the earlier draft's `refuse`. The value is
dispositioned as a non-personal identifier for this target (a relay sender,
an operator mailer address, a role inbox at an unrelated domain): it is
recorded in the attribution log and never filed as this person's contact
value. That is a disposition, not a refusal (§3).

The seam is pure apart from appending to the attribution log. It never writes
a contact record. Writers keep their own write logic and act on the verdict
according to the mode (§9).

### 4.2 Call sites

Each writer calls the seam immediately before it would put a value on a
record, and passes its own `writer` id so the log can report per writer.

| Writer | Call site | What `suspect` means there (enforce mode) |
|---|---|---|
| Correspondent recording | `pii.record_observed_contact_value`, before the resolve, mint and classify branches | No mint, no classify. The value is parked with the raw file as its source. |
| Corrections list-add | `corrections.process_correction_record`, the `op == "add"` branch, after the delta gate and before apply | The record result is `escalated` with reason `attribution-suspect`. The value is parked. |
| PII migration, bulk and `--page` | `storage_migrate.plan_pii_migration`, per extracted token, before the excluded-record build | The token is still redacted from the page. It is parked, not added to the page owner's record. `PiiMigrationPlan` gains `parked_tokens`. |
| Contact-record fold | `contact_record_fold.merge_contact_values`, per value in `missing_values(absorbed, canonical)` | The value is not carried onto the canonical record. It is parked with the absorbed record as its source. |
| Prose compile | the scrub helper of §10.2, per address token in compiled output | The token never reaches the body (scrubbed in every mode). It is parked against its likely owner. |
| Re-attribution | the placement executor of §7, before writing to the new owner | The placement is abandoned and the item goes to the human queue (§8). |

`plan_pii_migration` is pure and writes nothing. The verdict therefore only
shapes the plan, and the caller (`_cmd_storage`) writes the park entries
when it applies the plan. The same applies to `merge_contact_values`, which
returns its parked values to `fold_contact_record`.

The fold call is why athenaeum#2196 lands first: once that fix makes the fold
stop carrying orphaned facts, every remaining carried value is a listed value,
and listed values are what the seam checks.

### 4.3 Where suspects and verdicts live

All of it is on the excluded contacts surface, reached through
`pii.contacts_surface_root`, and none of it is embedded, recalled or committed
to the wiki:

- `<contacts_root>/_attribution/suspects/<value_key>-<target_uid>.md`, one per
  parked (value, target) pair. It holds the value, the target uid, the writer,
  the source ref, the fired checks and the candidates. Re-parking the same pair
  merges into the same file, so parking is idempotent.
- `<contacts_root>/_attribution_log.jsonl`. One row per verdict, carrying
  `value_key`, uids, writer, disposition, check ids and mode, but no value.
  This is the observe-mode evidence base and the lint's input.
- `<contacts_root>/_attribution_memo.jsonl`. Escalation verdicts memoised by
  `(value_key, target_uid, source_ref, prompt_sha256, model)`, so one triple is
  never re-spent. Same semantics as the athenaeum#712 verdict ledger, kept on
  the excluded surface because its subject is a value.

`value_key` is an HMAC of the normalised value under a per-deployment key
stored on the excluded surface. A plain hash of an email address can be
reversed by dictionary attack, and the log rows must be safe to read without
the surface.

## 5. Deterministic check catalogue

### 5.1 Order and cost

The checks run cheapest first. A `non-personal` result stops the run. A
`suspect` result does not stop the run: every remaining check still runs, so
the escalation sees all the evidence. A value with no fired check passes.

| Id | Check | Disposition | Cost | Child |
|---|---|---|---|---|
| D1 | **Non-personal identifier.** Two tiers (§17, decision 3). *D1-narrow*: relay and notification local-part prefixes (the `noreply` family plus `librarian.attribution.relay_local_prefixes`), the operator's own addresses and mailer domains (`librarian.attribution.operator_mailer_domains`) on a non-owner page, and service identifiers (`pii.is_service_address`). D1-narrow is enforced in every mode. *D1-domain*: whole-domain relay entries (`librarian.attribution.relay_domains`), logged as `non-personal` but observe-only until a narrowed list exists, because a whole-domain entry over-denies that domain's real employees. Role local parts (`info`, `hello`, `hi`, `team`, `contact`) pass when the domain matches the target's own or employer domain, and are `suspect` otherwise. On a non-person target, role addresses pass. | `non-personal` or `suspect` | O(1), set lookups | B |
| D2 | **Held elsewhere on the surface.** Another uid's contact record lists the value (`pii.resolve_contact_records` deduped by uid, the walk `record_observed_contact_value` already does). Records whose uid has no page do not count (§10.3). | `suspect` | O(1), index lookup | B |
| D3 | **Target is not a person.** The target page's `type` is not `person`, or its name matches a company or organisation page's name or alias in `EntityIndex`. | `suspect` | O(1) | B |
| D4 | **Fan-out.** The value is already on two or more uids, whoever holds it. | `suspect` | O(1), from D2's walk | B |
| C1 | **Inline on another page.** The value appears as a raw token in another uid's page body. An inverted index (value_key to uids) is built once per run from the same scan the PII lint performs, never a per-token search. | `suspect` | O(1) per value after an O(corpus) build per run | C |
| C2 | **Local part names someone else.** The local part's name tokens match another known person's full name, or their first-initial-plus-surname form (`flast`, `first.last`, `firstlast`), and do not match the target's name or aliases. A first name alone never fires, because first names collide too often to be evidence. A local part with no name signal passes. | `suspect` | O(1) per value against a name index built once per run | C |
| C3 | **Source context.** When the cited source still exists, the check fires if the value is absent from it. It also fires if the value appears within a short window of an attribution label (`referred by`, `introduced by`, `cc`, `via`, `on behalf of`, `forwarded`, `from:` in a quoted block) or of another known person's name. The label list is config data, not code. | `suspect` | O(source length) | C |

D-checks and the fan-out need only the contacts index and `EntityIndex`, which
every run already builds. The C-checks need a per-run index or a source read,
so they ship separately (child C) and run after the D-checks.

### 5.2 `AttributionContext`

Built once per run, or once per CLI invocation, and passed to every call:

- the `ExcludedRecordIndex` (exists);
- `EntityIndex` (exists);
- the operator's addresses. The logic is lifted from `dedupe._owner_addresses`
  into `pii` or `config`, because `pii` is L3 and must not import the L4
  `dedupe`;
- the non-personal pattern set: built-ins plus the three D1 config keys
  (§5.1): `librarian.attribution.relay_local_prefixes` (D1-narrow local-part
  prefixes), `librarian.attribution.operator_mailer_domains` (D1-narrow
  operator mailer domains) and `librarian.attribution.relay_domains`
  (D1-domain, observe-only whole-domain relay entries);
- the attribution-label list: built-ins plus
  `librarian.attribution.context_labels`;
- (child C) the inline-value inverted index and the person-name index.

A writer that runs outside a librarian pass, such as a CLI migration, builds
the context itself through one constructor,
`pii.build_attribution_context(knowledge_root, config)`.

### 5.3 Expected deterministic share

On the audited population, more than 95% of live writes are expected to be
decided without a model. Most values pass every check, and the relay and
operator addresses are settled by D1. This is an expectation for planning, not
an acceptance threshold. Observe mode measures the real share (§9).

## 6. Escalation contract

### 6.1 When it runs

Only on a `suspect` verdict. A `pass` or `non-personal` verdict makes no model
call. A memo hit (§4.3) makes no model call either.

### 6.2 Input

```python
@dataclass(frozen=True)
class AttributionQuestion:
    value: str                          # the contact value under question
    target: EntityBrief                 # uid, name, type, aliases, employer/domain hints
    holders: tuple[EntityBrief, ...]    # other uids that hold or name the value (D2, D4, C1)
    candidates: tuple[EntityBrief, ...] # uids the C2/C3 evidence points at
    checks: tuple[CheckEvidence, ...]   # what fired, and why
    source_excerpt: str | None          # a window of the cited source around the value
```

The excerpt is a window of at most a few hundred tokens around each occurrence
of the value. It is fenced with `prompt_safety.fence_untrusted`, because raw
intake and page bodies are untrusted data. The model sees the address in the
clear. That is the same egress the compile already performs on the raw note
the address came from, so this design adds no new class of egress.

### 6.3 Output

```python
@dataclass(frozen=True)
class AttributionJudgement:
    verdict: str            # "own" | "other-known" | "other-unknown" | "alias"
                            # | "same-entity" | "non-personal" | "unclear"
    owner_uid: str | None   # set for "other-known" and "same-entity"
    alias_relation: str | None   # for "alias": "role-inbox" | "shared" | "former-name"
                                 # | "personal-alias"
    quote: str | None       # the excerpt span the verdict rests on
    score: float            # the raw confidence signal (§6.5)
    model: str
    prompt_sha256: str
```

`unclear`, unparseable output, an API failure and a missing client all yield
`unclear` with `score = 0`. Escalation **fails closed**: nothing is written,
the item stays parked, and it goes to the human queue (§8). This is the
opposite of `claim_kind.classify_claim_kind`, which fails open, because an
unclassified claim is harmless and a wrongly placed address is not.

### 6.4 The tier seam

One function, `pii_attribution.judge_contact_attribution(question, client,
config) -> AttributionJudgement`, in a new module beside `pii` (L3, the same
layering as `claim_kind`). It follows the `claim_kind` call pattern: the
model comes from `config.resolve_model`, the call is wrapped in
`_retry.with_retry`, the output is strict JSON parsed with
`json_utils.extract_json_object`, and thinking is resolved per knob.

- **Model resolution, per question class.** Env `ATHENAEUM_ATTRIBUTION_MODEL`
  wins. Then yaml `models.attribution_<class>`, then yaml
  `models.attribution`. Then the shipped calibration record's recommended
  model for that class (§12.4). The last fallback is
  `config.DEFAULT_CLASSIFY_MODEL`. No code path escalates to a more expensive
  model because a cheaper one was unsure. If the class's measured tier is not
  confident enough, the item goes to a human, not to a bigger model.
- **Provider-agnostic.** The function takes an `athenaeum.provider.LLMBackend`,
  the existing text-backend protocol, so a non-Anthropic backend needs no
  change here. Only Anthropic models are measured (athenaeum#1101 is wont).
  A typed backend such as `decision_provider.DecisionBackend` (Choice
  question) could be added later as a second implementation behind the same
  function. It is not built now.
- **Prompt text lives in a file.** The prompt goes in
  `src/athenaeum/prompts/contact_attribution_judge.md`, loaded with
  `importlib.resources`, the convention `pii_hazard_classifier` and
  `tiers` (person-hint prompts) already use. It is registered in
  `prompt_registry`, with a golden under `tests/data/prompts/` and a
  regenerated `docs/design/prompts.md`. The prompt's sha256 is part of every
  judgement and every calibration record, so a prompt edit makes the
  calibration stale (§9.3).

### 6.5 The confidence signal: measured, not trusted

`docs/design/contradiction-detection.md` records why the five-verdict
comparator uses no confidence thresholds: self-reported model confidence did
not rank correctness (two wrong merge proposals at 0.84 and 0.82, a correct
one at 0.77). The amended §2.2 asks for calibrated thresholds. The two are
compatible only if the threshold is applied to a score whose relationship to
correctness has been **measured**, and only if the design survives that
measurement showing the score carries no information.

So `score` is any scalar, and the calibration record maps it to an empirical
probability of being correct. The eval (§12) decides, per question class, which
of three signals is used:

1. **Self-reported confidence** (0 to 1, asked for in the JSON). Used if its
   discrimination clears the bar (§12.3: AUROC at least 0.75 on held-out
   cases).
2. **Agreement over K = 3 calls**, with the candidate order permuted between
   calls. The newer models reject sampling parameters
   (`models._SAMPLING_PARAMS_REJECTED_PREFIXES`), so a temperature cannot be
   set, and the permutation is what makes the three calls independent. The
   score is the share of calls agreeing with the majority verdict. Used if
   signal 1 fails the bar and this one passes it.
3. **Class-level.** The score is a constant, so the threshold degenerates to
   "this class auto-files or it does not". The class auto-files only if its
   accuracy, at the lower confidence bound, clears the target (§12.3).
   Otherwise every item in the class goes to a human.

The fallback is honest: when the model cannot rank its own answers, the
system stops pretending it can, and the per-class accuracy alone decides.

### 6.6 Question classes

There is one prompt and one call per suspect, and the verdict enum covers
ownership, owner identification and alias in a single answer. A *question
class*, for tier choice and calibration, is keyed by the evidence that
triggered escalation, because difficulty and error cost follow the trigger:

| Class | Triggering checks | Typical real pattern |
|---|---|---|
| `held-elsewhere` | D2, D4, C1 | Referrer or partner address copied onto many contacts, or a value that sits inline on its real owner's page |
| `names-other` | C2 | The local part names the introducer or a colleague |
| `source-context` | C3 | "Referred by", cc lists, intro emails, recipient lists |
| `non-person-target` | D3 | The deal contact's address on a page named after their company |
| `role-unmatched` | D1 (`suspect` branch) | A role inbox at an unrelated domain |

When several classes fire, the class is the first one in this table's order.
The order runs from strongest to weakest ownership evidence, so the class
reflects the most informative signal.

## 7. Filing the result

### 7.1 Outcomes

At or above the class threshold (§8), the placement executor
`pii.place_contact_value(...)` acts on the verdict:

| Verdict | Filing |
|---|---|
| `own` | Filed on the target as originally intended, `unclassified`, unless the writer had already established a stronger class through its own evidence. The correspondent path's header-verified `observed` is the only such case. |
| `other-known` | Re-attributed: filed on `owner_uid`'s record, `unclassified`, with `field_sources` provenance naming the original source and the escalation (§7.2). Nothing is filed on the original target. |
| `other-unknown` | Not filed on anyone. Recorded as a suspect with verdict `other-unknown`, so a later page for the real owner can claim it through D2/C1 evidence. This is the only outcome that leaves a value parked after a confident answer, because there is nowhere correct to put it yet (§2.3 quarantine). |
| `alias` | Filed on the target in `alt_emails` (for phones, `phones`), with a `field_sources` entry and a per-identifier fact entry in a new `identifier_relation` field: `{identifier, relation, source, decided_by}`. |
| `same-entity` | No contact write. A merge proposal (target, `owner_uid`) goes to the existing pending-merges queue through `pending_merges.write_pending_merge`, because one person under two names is a dedupe question, not an alias. The value follows the merge through the existing fold. |
| `non-personal` | Recorded in the log. Not filed. |

`identifier_relation` joins the per-identifier fact fields
(`contact_record_fold._PER_IDENTIFIER_FACT_FIELDS`), so folds carry it and
athenaeum#2196's orphan rules cover it.

### 7.2 The write path

Every filing goes back through `check_contact_attribution` with
`writer="re-attribution"` before it writes. That re-check stops a placement
that would itself create a fresh conflict. It does not loop: a re-attribution
re-check that returns `suspect` goes straight to the human queue and never
back to the model.

The write itself uses the `pii` record primitives directly (the mint and
classify logic of `record_observed_contact_value`, generalised to take a
usage class and a target field). It does **not** go through the corrections
applier. `contact_record_fold`'s module docstring gives the precedent and the
reason: `process_correction_record` is gated by the deployment's
`librarian.corrections.fields` allowlist, which is empty by default, and it
resolves a uid's record first-match-wins. Routing re-attribution through it
would no-op on a stock configuration and could land on the wrong copy of a
duplicated record. The provenance shape is still the corrections applier's
list shape, `{"value": ..., "source": ...}`, so readers see one format.

Provenance on a placed value:

```yaml
field_sources:
  emails:
    - value: <value>
      source:
        type: attribution
        original_source: <the writer's source ref>
        original_target: <uid it was headed for>
        writer: <writer id>
        verdict: other-known
        model: <model id>
        prompt_sha256: <sha>
        calibrated_p: 0.97
```

## 8. The human queue and the threshold

### 8.1 What reaches a human

An item goes to the human queue only when one of these holds:

- the calibrated probability (§12.4) for its class is below that class's
  threshold;
- the judgement is `unclear` or failed (fail-closed, §6.3);
- the re-attribution re-check (§7.2) returned `suspect`;
- the class has no valid calibration record (stale or missing, §9.3) and the
  mode is `enforce`.

It is raised as an `EscalationItem` with `conflict_type="ambiguous"` through
the existing `tiers.tier4_escalate` path, so it appears in
`decisions.list_pending_decisions`. The description names the target, the
candidates and holders by name and uid, the fired checks, and the model's
verdict and calibrated probability. It shows the value **masked** (local part
reduced to its first character plus the domain), and points to the suspect id.
A human who needs the value reads it through the one way out (`read_entity`
or `athenaeum attribution show <suspect-id>`). `_pending_questions.md` lives in
the wiki, so the raw value never goes there.

A human answer is filed through the same placement executor, with
`decided_by: human` in the provenance and no calibrated probability.

### 8.2 Thresholds

There is one threshold per (question class, model, prompt sha). Thresholds
are read from the shipped calibration record (§12.4). A deployment can
override them in `librarian.attribution.thresholds.<class>`. A threshold is a
calibrated probability, not a raw score.

### 8.3 Tracking calibration over time

- **Audit sampling.** A deterministic share of above-threshold placements is
  sampled into the calibration ledger for human audit. This extends
  `calibration.sample_tier_decision`, which today is hard-wired to T1 rejects
  and T2 approves through `calibration._WATCHED_VERDICT` and per-tier rate
  resolvers. The extension adds tier `attribution` with the watched verdicts
  `other-known`, `alias` and `own`, plus a rate knob
  `librarian.audit_sample_rate_attribution` (default 0.10). An audit review
  confirms or overturns. An overturn is a calibration signal, and it also
  re-files the value through the placement executor with the human's answer.
- **Threshold proposals.** `auto_apply_proposals` already drafts
  threshold-change proposals from audited overturn rates into the decisions
  queue, for a human to approve. Attribution reuses that rail with its own
  action ids, in both directions. It proposes widening when overturns are
  rare and narrowing when they exceed the class's target error rate.
  Narrowing is new to that rail, which today only widens. A human approves
  every change. Nothing tunes itself silently.
- **Re-measurement.** Any model or prompt change makes the calibration record
  stale (§9.3). Re-running the eval is the way back.

## 9. Observe, then enforce

### 9.1 Modes

`librarian.attribution.mode` takes three values: `observe`, `enforce` and
`auto`.

- **`observe`.** Every writer calls the seam and logs the verdict. Writers then
  behave exactly as they do today. Suspects are parked as **shadow** entries
  (marked `shadow: true`). Escalation runs on them and its proposed placement
  is logged, but nothing is filed. The one exception is the prose-compile
  scrub (§10.2), which removes non-subject addresses from compiled bodies in
  every mode. Bodies should never hold addresses, and the scrubbed tokens are
  parked, not dropped. D1-narrow values (§5.1) are also never filed on their
  target, in every mode, from child B onward (§17, decision 3).
- **`enforce`.** A `suspect` value is parked and not written to its target. A
  `non-personal` value is not written to its target. Escalation places what it
  can, and the rest goes to the human queue.
- **`auto`** (the shipped default once child F lands). Enforce for a question
  class only when BOTH hold (§17, decision 1): a valid calibration record
  exists for the model and prompt that class resolves to, and the operator has
  acknowledged a clean observe report for this deployment covering that class
  (§9.3). Otherwise observe for that class. A fresh install therefore observes,
  and so does the stock Anthropic configuration until its operator has
  reviewed and acknowledged an observe report on their own store.

Until child F lands, the knob does not exist and the code observes, apart from
D1-narrow, which child B enforces in every mode (§17, decision 3).

### 9.2 What observe mode produces

The attribution log gives each deployment, per writer and per check:

- pass, non-personal and suspect counts;
- the deterministic share;
- shadow escalation verdicts and their calibrated probabilities;
- the would-be human-queue share.

`contact-surface-lint --attribution` (§10.4) summarises it. This is the
evidence an operator uses to judge whether enforcement on their store would
behave as the eval predicts.

### 9.3 The flip, and staleness

A calibration record is valid for a class when all of these hold:

- its `model` equals the model that class resolves to;
- its `prompt_sha256` equals the live prompt's;
- it passes the bar in §12.3.

An observe report is the record `contact-surface-lint --attribution`
(§10.4) persists on the excluded surface: per-class shadow counts and the
model and prompt sha it ran under, never a value. The operator acknowledges a
report as clean with `athenaeum attribution ack-observe-report <report-id>`.
An acknowledgement is valid for a class when the report covers that class
under the same model and prompt sha the class resolves to now.

`auto` checks both at run start through a preflight, and logs the per-class
mode chosen. A stale record or a stale acknowledgement reverts that class to observe and
logs a warning.
It never silently keeps enforcing on a model that was never measured.

### 9.4 Rollback

Set `librarian.attribution.mode: observe`. Everything a run enforced is
reversible from the attribution log and the suspect files:

- a parked value can be filed on its original target with `athenaeum
  attribution place <suspect-id> --as own`;
- a placed value carries provenance naming its original target and source.

D1-narrow's day-one enforcement (§5.1, §9.1) is the one exception that runs
ahead of the mode flip above, and it is held to the same guarantee by the
same mechanism: a value D1-narrow blocks is never filed on the target, but it
is parked as a non-shadow suspect entry with disposition `non-personal`
(operator ruling 2026-10-30; §15). The entry is never lost, and it is
reversible exactly like any other parked value — `athenaeum attribution place
<suspect-id> --as own` files it on the target it was withheld from.

No value is ever deleted by this design.

## 10. Writer-side defects

### 10.1 The correspondent shortcut (child A)

`librarian._pick_correspondent_entity` returns the only created person page
whenever exactly one was created, without checking its name. For an intro
email, the correspondent (the sender) is the introducer, who usually already
has a page. The one page created is the introducee's.
`_record_correspondent_contact` then records the introducer's address on the
introducee as `observed`.

The fix is deterministic, local to `librarian.py`, and independent of the
seam:

1. If `compile_routing.resolve_correspondent_page` resolves the address to an
   existing page, no created page is the correspondent. Record nothing here;
   the address already has its owner.
2. Otherwise, if `correspondent_name` is present, require the exact
   case-insensitive name match (or an alias match) even when only one page was
   created. This is the rule that already applies to the several-pages case.
3. Otherwise (no name, one created page), require the local part to
   name-match the created page under C2's full-name or `flast` rule.
   Otherwise decline and escalate.
4. Every escalation `_record_correspondent_contact` raises masks the address
   (§8.1) in both its description and its `entity_name`, instead of
   embedding `ref.address!r`. One escalation description — shared by the
   three decline paths above — sets `entity_name=ref.display_name or
   masked`, so `entity_name` falls back to the masked address, never the raw
   one, when the raw note gives no display name.

This is the one live defect that produces outreach-eligible misattribution,
so it ships first and does not wait for athenaeum#2196.

### 10.2 Prose-compile scrub (child G)

The tier-3 create and merge prompts can write addresses mentioned in a note
into the body of whichever page they compile. The next PII migration pass
then files those addresses as the page owner's. `batch.py`'s batched
result-handling path does not call `tiers.tier3_create` or
`tiers.tier3_merge`; its unbatched fallback loops at finalize do call both,
alongside the synchronous path in `tiers.py`. All three converge on the same
three shared helpers: `tier3_create` returns through
`tiers.tier3_entity_from_response`, and `tier3_merge` returns through
`tiers.parse_merge_ops_response` and `tiers.tier3_merge_full`, and so does
the batched path's own result parsing. The scrub therefore sits on the
output of those three helpers (or immediately after each call in both
callers), so every caller gets it regardless of transport.

`scrub_compiled_contact_tokens(body, *, subject_uid, source_ref, context)`:

- finds address and phone tokens with the existing detectors
  (`pii.find_inline_emails` and the phone detector);
- removes every token from the body. Tokens are replaced with nothing, or
  with a neutral marker where removal would break a sentence. The
  name-field carve-out of `storage_migrate.plan_pii_migration` (athenaeum#1461)
  is respected;
- sends each removed token through `check_contact_attribution`, with
  `writer="prose-compile"` and the raw file as source, against the subject.
  In enforce mode, a `pass` is filed on the subject's record (`unclassified`,
  never `observed`). A `suspect` is parked with its candidates, so
  re-attribution can file it on its real owner. In observe mode, every token
  is parked as a shadow entry.

### 10.3 Contact records orphaned by page removal (child H)

Two code paths remove a page and leave its contact record live:

- `decay_sweep.archive_via_two_commit_git_rm`, called by
  `retire_pages.apply_retirement` and by the decay sweep;
- `tiers._perform_oversize_log_demote`, which moves an oversized page out of
  the wiki.

Neither touches the contacts surface. The merge paths already fold absorbed
records (athenaeum#2184), and `oversize_split.split_pages` keeps the hub page's
uid, so neither of those orphans anything.

The fix makes both removal paths call one helper, which folds the removed
uid's records into a named successor through
`contact_record_fold.fold_contact_record` when the removal names one (a
retirement that records a merge target). A log demote moves the page to the
preserved-log area, which is not an entity, so it has no successor. When there
is no successor, the helper retires the record
with `pii.retire_contact_record`, which renames it out of every read, never
deletes it, and logs the retirement. `repair --contact-records` gains a
report-and-retire mode for records whose uid has no page and no merge record
(today reported and left alone), with the same retire semantics, dry-run by
default.

A record whose uid has no page does not count as "held elsewhere" for D2,
so orphans stop blocking correct writes. The lint (§10.4) reports them.

### 10.4 `contact-surface-lint` (child I)

`ExcludedRecordIndex._warn_collisions` (through `pii._log_collision_summary`)
points at a `contact-surface-lint` command that does not exist. Child I builds
it as a read-only CLI. It prints counts and uids only, never values, and
reports:

- records whose uid has no live page;
- values held by two or more uids, with the per-value provenance source type
  (`field_sources` type, or `unsourced`);
- `observed` values whose cited source no longer contains the value;
- with `--attribution`, the seam run in report mode over every
  (value, uid) pair on the surface, with counts per check and per question
  class. Optionally (`--escalate`, opt-in, budget-capped by the existing
  `librarian_max_api_calls`) it also runs the shadow escalation. This is the
  backfill report. Working its output on a real store is operator-side
  (§2.2).

## 11. Child issues

Each child is one PR, tests use synthetic fixture stores and `example.org` or
`example.com` values only, and no test makes a live model call. Letters are
stable names for this document. Issue numbers are on the epic.

| Child | Scope | Blocked by |
|---|---|---|
| A | Correspondent-shortcut fix and masked escalation text (§10.1) | none |
| B | The seam, `AttributionVerdict` and `AttributionContext`, checks D1 to D4 (D1-narrow enforced from day one), the suspect store and attribution log (§4.3), wired in observe mode into the four store writers (§4.2: correspondent recording, corrections list-add, PII migration bulk and `--page`, fold) | athenaeum#2196 |
| C | Context checks C1 to C3, with their per-run indexes | B |
| D | Calibration eval: dataset generator, `judge_contact_attribution` with prompt file and registry entry, metrics, threshold derivation, shipped calibration record (§12) | B |
| E | Escalation and placement in observe (shadow) mode: memo, question classes, the placement executor for every outcome in §7, `identifier_relation`, masked human-queue items, `attribution show` and `place` CLIs | A, C, D |
| F | Enforce: the `librarian.attribution.mode` knob with `observe`, `enforce` and `auto`, per-writer enforce branches (including the scrub's), the calibration and observe-report preflight, `auto` as the shipped default | E, G, I |
| G | Prose-compile scrub at `tier3_entity_from_response`, `parse_merge_ops_response` and `tier3_merge_full` (§10.2) | B |
| H | Orphan contact records on page removal, and repair's report-and-retire mode (§10.3) | athenaeum#2196 |
| I | `contact-surface-lint`, including `--attribution` report mode (§10.4) | C, E, H |
| J | Calibration over time: the `attribution` audit tier in the calibration ledger, and threshold proposals on the `auto_apply_proposals` rail (§8.3) | E |

athenaeum#2197 is blocked by child B only, for the reason given in §2.2.

## 12. The calibration eval (child D)

### 12.1 Dataset

The dataset lives at `tests/evals/data/attribution/`. It is **synthetic**:
invented names, `example.org` and `example.com` domains, and invented
organisations. It is generated by a checked-in script from templates of the
real patterns, so it can grow without hand-labelling. Each pattern template
yields both positive and negative variants, and some cases are hand-written
adversarial ones.

| Pattern | Gold verdict |
|---|---|
| Introducer's address on the introducee, intro email excerpt | `other-known` (the introducer) |
| "Referred by: <addr>" field on the referred contact | `other-known` or `other-unknown` |
| Referred person's address on the referrer (reverse direction) | `other-known` |
| One referrer address copied onto many contacts (fan-out) | `other-known` |
| Platform reply-relay sender on a contact | `non-personal` |
| Operator mailer address on a contact | `non-personal` |
| Deal contact's address on a company-named page | `other-known` (the person) |
| Campaign or project page holding recipients | `other-known` per recipient |
| Organiser's page holding colleagues' addresses | `other-known` |
| Role inbox on the person's own domain | `alias` (`role-inbox`) |
| Role inbox at an unrelated domain | `other-unknown` or `non-personal` |
| Same person under two surnames, or a former name | `same-entity` or `alias` (`former-name`) |
| Local part with a surname initial that does not match the page | `own` or `other-known`, depending on the excerpt |
| Correct but unusual address (nickname, personal domain) | `own` |

Each case has a target brief, holders, candidates, the checks that would fire,
a source excerpt, and the gold label. There are at least 50 cases per question
class (§6.6), so at least 250 in all, with a fixed-seed stratified split into
60% fit and 40% held out. Cases with no real-world counterpart are allowed,
but they never make up more than 20% of a class.

Real labelled cases from an operator's deployment are the seed for the
templates. They are never committed. A deployment can run the same harness
over a private, untracked case file through `--cases <path>` to check that the
synthetic numbers transfer to it. That run is operator-side.

### 12.2 Harness

The harness reuses `tests/evals/harness.py`: it is marked `pytest.mark.eval`,
deselected by default, and runs through the existing `evals.yml`
`workflow_dispatch` job, with record and replay. It compares tiers the way
`tests/evals/tier_compare.py` does: each candidate model is passed through
`config={"models": {"attribution": <model>}}`, with the same guard against an
env-pinned model. The candidates are configured, not hard-coded. The default
set is the classify, write and verify knobs' defaults: today Haiku 4.5, Sonnet
5 and Opus 5. Each tier is run once with self-reported confidence. Then K = 3
permuted calls are made for agreement, but only for classes where signal 1
fails the bar (§6.5), which keeps the cost bounded. The metric code has
offline unit tests on canned responses.

### 12.3 Metrics and the pass bar

The harness reports these for each (tier, question class, signal):

- accuracy, with its 95% Wilson interval;
- discrimination, as AUROC of the score against correctness;
- a reliability table over five equal-count bins (predicted probability
  against observed accuracy);
- ECE over those bins;
- the threshold derived on the fit split, and accuracy above it on the
  held-out split with its Wilson lower bound;
- the abstain share, meaning the share below threshold, which is the expected
  human-queue share;
- cost per case, from the existing `TokenUsage` accounting.

A class **passes** for a tier when every condition below holds on the
held-out split:

1. The Wilson 95% lower bound of accuracy among cases at or above threshold is
   at least the target (`librarian.attribution.accuracy_floor`, default
   **0.95**). This is one floor for every outcome: confirming a value as the
   target's own and re-attributing it are held to the same bar (§17,
   decision 2).
2. ECE is at most 0.05 for signals 1 and 2. Signal 3 has no ECE.
3. The abstain share is at most 0.30. A class that would send more than this
   to humans is reported as failing, so a human decides whether to accept the
   load.
4. No case produces an `observed` usage class. This is a structural test, not
   a measurement.

### 12.4 Choosing the tier and storing the result

For each class, the recommended tier is the one that passes with the lowest
abstain share. Ties go to the lower cost per case. That is the §2.2 rule:
accuracy first, then human load, and price only as a tie-breaker, never as an
escalation ladder. If no tier passes, the class has no recommended tier, and
in `auto` mode it stays observe.

The run writes `src/athenaeum/calibration_data/contact_attribution.yaml`,
loaded with `importlib.resources` like `retention_packs/`, with one entry per
class:

```yaml
- question_class: held-elsewhere
  model: <model id>
  prompt_sha256: <sha>
  signal: self-reported | agreement-k3 | class-level
  bins: [[0.0, 0.42, 0.61], [0.42, 0.70, 0.83], ...]  # lo, hi, observed accuracy
  threshold: 0.93            # a calibrated probability
  n_fit: 60
  n_holdout: 40
  holdout_accuracy_above: 0.98
  holdout_wilson_lb: 0.95
  ece: 0.03
  auroc: 0.81
  abstain_share: 0.18
  dataset_sha256: <sha>
  measured_at: <date>
  passes: true
```

A short `docs/measurements/attribution-calibration-<date>.md` records each
run. Child D lands the harness and a first record. If no live run is possible
from the build environment, it lands the harness with `passes: false` entries,
and `auto` then observes until someone dispatches the eval.

## 13. Cost model

Repository rates per million tokens (`models._MODEL_RATES_USD_PER_MTOK`):

| Model | Input | Output |
|---|---|---|
| Haiku 4.5 | $1 | $5 |
| Sonnet 5 | $3 | $15 |
| Opus 5 | $5 | $25 |

One escalation call is about 2,000 input tokens (prompt, briefs, excerpt) and
about 250 output tokens. That is roughly $0.003 on Haiku, $0.01 on Sonnet and
$0.016 on Opus. Agreement scoring (K = 3) triples the cost for the classes
that need it.

| Load | Calls per month | Haiku | Sonnet | Opus |
|---|---|---|---|---|
| Steady state, all writers, live (order 10 to 50 suspects a month after the deterministic pass; up to 150 calls with K = 3) | 10 to 150 | under $0.50 | under $1.50 | under $2.50 |
| Observe-mode shadow escalation | same as above | same | same | same |
| One-time report with `--escalate` over a whole surface (order 10⁴ records, 5 to 10% suspect) | order 10³, once | about $3 (up to $9 with K = 3) | about $10 (up to $30) | about $16 (up to $48) |
| Calibration eval run (at least 250 cases × 3 tiers, K = 3 on a subset) | about 750 to 1,500 per run | | | about $15 to $40 per full run, all three tiers together |
| Human queue | a few items a month, at a target abstain share of at most 30% of suspects | | | |

Even at the most expensive tier, the steady-state cost is noise next to the
compile's write-knob spend. The design is human-attention-bound, not
spend-bound. That is the point of §2.2, and why the abstain share is part of
the pass bar.

## 14. Failure classes the design must survive

| Failure | How it is contained |
|---|---|
| A model confidently places an address on the wrong person | Placements are checked against the calibrated floor. Re-placement goes back through the seam. Placed values are `unclassified`, so never outreach-eligible. Audit sampling and overturns re-file (§8.3). |
| The model's confidence carries no information | The eval detects it (AUROC). The class falls back to agreement scoring or a class-level decision (§6.5). |
| A model or prompt change after calibration | The record is bound to the model and prompt sha. `auto` reverts that class to observe (§9.3). |
| A model call fails, times out, or returns garbage | Fail closed: parked, human queue (§6.3). |
| Deterministic false positives flood escalation | Observe mode measures the rate per check before enforcement. C2 never fires on a first name alone. D-checks and C-checks ship separately, so a noisy check can be tuned or disabled without touching the others. |
| A value is lost | Every non-filed value is parked or logged, never dropped. Migration still redacts, but parks. The scrub parks what it removes (§10.2). |
| A raw value leaks into the wiki or git | Values live only on the excluded surface. Logs carry an HMAC `value_key`. Queue items are masked (§4.3, §8.1). |
| Bulk migration becomes O(n²) | C1 and C2 indexes are built once per run (§5.1). |
| Re-attribution loops | A re-check that returns `suspect` goes to a human, never back to the model (§7.2). |
| An internal writer bypasses the seam | The writer inventory in §4.2 is pinned by a test that asserts every listed call site calls the seam. Child B adds it with a membership assertion over the listed writers, and children E and G extend it. |

## 15. Migration and rollback

- No schema migration. New files appear on the excluded surface on first use.
  `identifier_relation` is additive and optional. `PiiMigrationPlan.parked_tokens`
  is additive.
- Observe mode changes no write. The only behaviour changes before child F
  are child A's shortcut fix, child B's D1-narrow enforcement, child G's
  scrub (which parks what it removes), and child H's retire-on-removal
  (rename, never delete). All four are reversible from their logs. Child B's
  D1-narrow enforcement does not file a blocked value on the target, but
  parks it as a non-shadow suspect entry with disposition `non-personal`
  (operator ruling 2026-10-30; §9.1, §9.4) — the named mechanism that keeps
  it from being lost and keeps `athenaeum attribution place <suspect-id>
  --as own` able to file it later.
- Rollback from enforce is a config change (§9.4).

## 16. Rejected alternatives

- **Quarantine only, no re-attribution.** This was the first draft's shape:
  flag the suspect, ask a human. It contradicts §2.12, which requires a failed
  check to be filed where it belongs. At the audited volume it would also put
  dozens of items in front of a human that a measured tier can place.
- **Route every placement through the corrections applier.** That would be one
  write path in name, but it is gated by an allowlist that is empty by
  default and resolves duplicated records first-match-wins. The fold module
  already rejected it for the same reasons (§7.2).
- **A price ladder: Haiku, then Sonnet when unsure, then Opus.** This is
  explicitly retired by the amended §2.2. Self-reported unsureness is the
  signal the comparator already found unreliable, and escalating on it spends
  more to get a confidence number that still has not been measured.
- **Gate inside `ExcludedRecordIndex` or at file-write time.** A write-time hook
  sees a record, not the writer's evidence: no source ref, no target name, no
  writer id. The checks need exactly that context, which only exists at the
  call site.
- **Use the five-verdict comparator.** The comparator compares two claims about
  one subject. Attribution asks which subject a value belongs to. The
  comparator's no-thresholds stance is honoured instead: thresholds here exist
  only on measured, calibrated scores (§6.5).

## 17. Risks and open questions

**Risks**

1. **The confidence signal may be uninformative for every tier.** Then every
   class falls to class-level decisions, and classes whose accuracy sits below
   the floor go to humans wholesale. Mitigation: §6.5's fallbacks, and the
   abstain share in the pass bar makes the cost visible before enforcement.
2. **Synthetic cases may not transfer to a real store.** Mitigation: the
   private `--cases` run, and observe mode's shadow verdicts on the live store
   before any enforcement.
3. **C3 depends on the cited source still existing.** Processed raw files are
   deleted from the tree, so many historical values have no live source.
   Mitigation: C3 only fires when the source exists. Absence of a source is
   not evidence.
4. **Masking in the queue makes some questions harder to answer.**
   Mitigation: `attribution show` reads the value through the one way out.

**Operator decisions (2026-10-30)**

1. **`auto` needs both measurements.** A class enforces only when it passes the
   synthetic calibration eval AND the deployment has an operator-acknowledged
   clean observe report on its real store (§9.1, §9.3).
2. **One accuracy floor.** 0.95, Wilson lower bound, for every outcome. There is
   no stricter floor for re-attribution than for confirming a value (§12.3).
3. **Narrow non-personal shapes enforce from child B.** Relay local-part
   prefixes and the operator's own addresses and mailer domains are never
   filed on a non-owner target, in every mode. Whole-domain relay entries stay
   observe-only until a narrowed list exists, because they over-deny real
   employees of the relay's domain (§5.1, D1).
