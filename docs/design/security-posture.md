# Security & Dependency Maintenance Posture

Last reviewed: 2026-05-12. Next scheduled review: **2026-08-06**.

This document records the threat model and dependency-upgrade policy for `athenaeum`. Patterned after the Kromatic games-and-tools workspace posture (origin: `plinkromatic/docs/design/security-posture.md`), adapted for this repo's published-library shape.

## 1. Shape of this repo (the load-bearing fact)

Athenaeum is an **open-source Python library** (Apache 2.0) published to PyPI. It is not a deployed service.

- Built with `hatchling` from `pyproject.toml`.
- Released via `.github/workflows/release.yml` on git-tag push (`v*`).
- Tested across Python 3.11 / 3.12 / 3.13 in CI.
- Critical deps have **explicit upper-bound caps** documented in pyproject.toml comments (`pyproject.toml:39-48`):
  - `anthropic>=0.39.0,<1.0` — "pre-1.0 and ships breaking changes freely"
  - `pydantic>=2.0,<3.0`
  - `chromadb>=0.5.0,<2.0` — "has shipped sqlite schema migrations in minor bumps"
  - `fastmcp>=2.0.0,<4.0` — post-1.0 but still iterating on its MCP protocol surface
  - **Verify these against `pyproject.toml` quarterly** (see §3.3) — this table is a snapshot, not the source of truth; `pyproject.toml` is.
- Third-party GitHub Actions are **pinned to SHAs** in both CI and release workflows for supply-chain hygiene.

The threat model is "**library consumer drift**" — a dep that ships a subtle behavior change can affect every athenaeum user's deployment without them noticing. Patches are usually safe; minors on the explicitly-flagged deps need a human eye.

## 2. What this library does and doesn't do

> **Read-scoping is one enforcement of the two-path invariant, not the whole of
> it.** The system-wide rule — *one path in, one path out*: every write enters as
> raw intake compiled by a single writer, and every read leaves through the
> recall/read interface, with no caller opening a store directly — is stated
> canonically in [`docs/design/one-way-in-one-way-out.md`](one-way-in-one-way-out.md).
> This section (and §2.1) documents how far that egress half is currently
> *enforced* over the MCP tool surface. Surfaces it does not cover are still
> governed by the invariant even though nothing today refuses a caller that
> goes around it. Since athenaeum#883/#885/#886 the off-corpus excluded surfaces
> (the contact-data surface, and whatever else an operator routes off-corpus)
> are reachable *through* the interface for every entity class — via
> `recall(with_pii=True)` or the generic `read_entity` — so going around them
> is no longer the only way to answer a question. That closes the gap that
> produced the direct read this invariant was written after; it does not add an
> access control, which remains the deferred athenaeum#864 question.

| Surface | Present? | Notes |
|---|---|---|
| User-facing service | **No** | Athenaeum is a library, run by consumers in their own processes. |
| Direct network access | Via consumers' use | When wrapping the Anthropic API, requests go from the consumer's process. |
| Local file/SQLite operations | Yes (chromadb optional extra) | SQLite schema migrations have happened in chromadb minors — hence the hold list. |
| Build-time secret handling | At release time only | Trusted-publishing identity uses GitHub OIDC; no long-lived PyPI token. |
| Read-scoping of recall (athenaeum#312) | Yes (opt-in) | `athenaeum serve --audience <role,…>` (also `ATHENAEUM_AUDIENCE` / `serve.audience`) pins a server process to a restricted read scope so a secondary agent/routine recalls only `access: open` pages and pages whose `audience:` list grants one of its roles; untagged / `confidential` / `personal` pages fail closed (withheld). The audience is pinned by the operator at serve time, not chosen by the `recall` caller, so a restricted agent can't widen its own scope. Enforced inside each backend query (ranking/top-k) and re-checked against fresh on-disk frontmatter at render. Unset = owner = full access. This is NOT a full multi-user auth/ACL system — it is a single-owner read filter for the owner's own secondary agents. |
| Intake-side secret/PII screening (athenaeum#320) | **Shipped** | `remember()`'s write path calls `athenaeum.screening.screen_intake` (`src/athenaeum/mcp_server.py:553`, screener defined at `src/athenaeum/screening.py:212`) to classify sensitive content and resolve the read-time `access:` label BEFORE the single append-only write. Opt-in via a `screening` config resolved by `athenaeum.config.resolve_screening`; `None` (default) preserves prior unscreened behavior. |

### 2.1 MCP tool audience scoping (athenaeum#312 → athenaeum#538)

> Scope note: this section decides *who* may read what **across the 15 MCP
> tools**. It does not, and is not meant to, establish that the MCP surface is
> the only way to reach a store — that is the egress half of the invariant in
> [`docs/design/one-way-in-one-way-out.md`](one-way-in-one-way-out.md), which this
> scoping enforces for one surface among several.

`caller_audience` (§2, row "Read-scoping of recall") is pinned **once** by the
operator at `athenaeum serve` time and governs the **whole** MCP process, not
just `recall`. Issue athenaeum#538 closed the gap where `recall` was the only one of the
then-11 registered tools that applied it — a restricted caller could read the same
bytes from a different tool, or mutate the operator's decision queue unchecked.
The decided model (this is the "write it down" outcome athenaeum#538 asked for):

| Tool group | Tools | Restricted (`caller_audience != None`) behavior |
|---|---|---|
| Scoped reads | `recall`, `list_pending_questions`, `list_pending_merges`, `list_pending_decisions`, `read_entity`, `enumerate_entities`, `entity_schema` | Fail-closed by the SAME predicate `recall` uses (`is_page_authorized`): an item is withheld unless the caller is authorized for **every** source page behind it. A restricted caller can never obtain page content `recall` would refuse. `read_entity` additionally never returns an excluded value for a page it withholds — the authorization check runs before any excluded data is assembled (issues athenaeum#864, athenaeum#886). `recall`'s `with_pii` join likewise runs strictly AFTER that predicate, so it can never be used to probe whether a record exists behind a page the caller may not read (issue athenaeum#885). (The person-shaped `read_person` tool applied the identical check before its removal in athenaeum#888.) `enumerate_entities` (issue athenaeum#965) applies the identical per-candidate check before a row is included in its result — `src/athenaeum/enumeration.py` re-checks `is_page_authorized(meta, caller_audience)` for every candidate, commented "Layer C fail-closed audience re-check (issue athenaeum#538), identical predicate to every other read tool" — and its `with_pii` gate on PII-scoped predicate/output fields mirrors `recall`'s. `entity_schema` (issue athenaeum#964) is scoped the same way one level up: its per-class `count` and `fields` are computed only over pages the caller may read, because `resolve_entity_classes_cached`/`resolve_entity_classes` thread `caller_audience` into that same `is_page_authorized` filter (`src/athenaeum/entity_schema.py`) — a restricted caller's schema can never reveal the existence of, or the field keys carried by, a page it cannot read. It never returns page body or excluded values, only aggregate counts and field-key names. |
| Owner-only writes | `resolve_question`, `resolve_merge`, `review_audit_item` | **Fail closed** — adjudicating the operator's contradiction/merge/calibration queue is an owner action. `list_pending_decisions` likewise withholds the `retraction`/`audit` calibration items (no readable source-page path to authorize against). |
| Intentionally open | `remember`, `raise_decision` | **Not** audience-scoped. Intake is write-only and compiles through the read-time screening path (athenaeum#320); a restricted secondary agent contributing raw memories is the intended use. It cannot read anything back it isn't authorized for. `raise_decision` (issue athenaeum#912) is open by the same logic, stated explicitly in its own docstring: unlike the three owner-only mutators above, it only ever ADDS a new item to `_pending_questions.md` — it never adjudicates an existing one — which puts it in the same category as `remember` rather than `resolve_question`/`resolve_merge`/`review_audit_item`. A restricted agent filing a question or confirmation into the human-decision queue is this tool's intended caller; gating it owner-only would defeat its purpose. |
| Metadata reads | `list_axiom_audit`, `scan_retraction_cascade`, `calibration_summary` | Not page-content bearing (governance history, provenance flags, tier counts). Left unscoped; revisit if any starts echoing page bodies. |

Rationale for the write split: the three decision-queue mutators change
**human-decision** state the owner uses to adjudicate the knowledge base, so a
non-owner caller has no business writing them. `remember` is different in kind —
it is append-only intake behind the screening pin, so scoping it would block the
one write a secondary agent legitimately makes without adding any read
protection. As with the read scope, this is a single-owner filter, **not** a
multi-user ACL system.

### 2.2 Outbound LLM path — prompt hygiene and PII posture (athenaeum#543)

Athenaeum sends prompts to a model on two backends: the Anthropic SDK (`api`)
and a local `claude -p` subprocess (`claude-cli`). Audit findings **L4/L5/L6**
covered what leaves the process on that path and what comes back.

- **L4 — prompt never in the process table.** The `claude-cli` backend passes
  the user prompt on **stdin**, not as a `-p <prompt>` argv element, so the
  user's own notes are not visible to a local `ps` for the (up to 300s) life of
  the call. The argv-list form (no `shell=True`; the audit confirmed zero
  `shell=True` / `os.system` across the tree) is retained.
- **The `claude-cli` subprocess is text-only, asserted rather than inherited
  (athenaeum#906).** Tier prompts embed fenced *untrusted* intake content, so
  what the subprocess can DO is part of the injection surface. Two flags pin it
  shut, both constructed in `provider.ClaudeCliClient._build_argv` and both
  covered by argv regression tests: `--strict-mcp-config` (athenaeum#775) keeps
  MCP servers from loading, and `--tools ""` disables Claude Code's own
  built-in tools (Bash, Edit, Read, WebFetch, …). Before athenaeum#906 only the
  first flag was passed, so built-in tool availability inside the subprocess was
  whatever `claude -p` defaulted to — unverified rather than controlled. The
  `--tools` spelling and its empty-string "disable all tools" semantics were
  read from the installed CLI's own `claude --help` and confirmed to parse
  (CLI **2.1.226**, 2026-08-19); a CLI old enough to lack the flag fails loudly
  with `unknown option`, not silently with tools enabled. The asymmetry is
  deliberate: on the `api` backend the requests carry no `tools` key at all, so
  there is nothing to pin — the subprocess is the one path whose answer would
  otherwise come from a default this repository does not control.
- **L5 — response logging is redacted.** The one response-logging site that
  embedded raw model output in an error (`provider._parse_envelope`) now runs it
  through `redact_outbound_text` first, matching the sibling site in `tiers.py`.

- **L6 — outbound PII redaction on egress: intended posture is NO chokepoint,
  by design (option S).** `outbound_pii.redact_outbound_text` exists and is
  wired to *log-line* redaction (L5 above, and `tiers.py`), but **no call site
  redacts prompt CONTENT on the way to a model, and that is intentional.**
  Athenaeum is a single-user *personal memory* tool whose entire job is to
  remember personal things and reason over them with a model; a blanket egress
  redactor on `build_llm_client` would corrupt the very content the tool exists
  to process. So the deliberate posture is: **the outbound path is not a PII
  boundary.** The genuinely hard half — egress *refusal* (an agent declining to
  reveal PII even when asked), and any conditional/consented redaction that is
  narrower than "redact everything" — is a policy decision that stays parked on
  **athenaeum#428** (see `outbound_pii.py` module docstring, lines 8-10), which owns the
  enforcement design if the posture ever changes. This section is the "write it
  down" half of L6: the redaction module is called by nothing on the egress path
  **on purpose**, not by oversight.

### 2.3 Contact-value provenance and permitted use (athenaeum#866)

**Stored and syncable is not the same as usable for outreach.** An address
obtained from a data vendor and an address someone used to write to you are
different facts with different permissions, and before this they were stored
identically. The distinction is *not* "is this private" — both are, and both
carry `pii: true`. It is **"may this be used to initiate contact"**.

Each contact value therefore carries its own provenance (which system asserted
it, and when) and a usage classification, at the level of the **individual
value** rather than the record — a person's record commonly lists one address
of each kind, so a record-level marker cannot express it:

| `usage_class` | Meaning | Address-book population | Outreach |
|---|---|---|---|
| `observed` | Seen in prior communication with this person | permitted | **permitted** |
| `provider` | Supplied by a data vendor | permitted | **not permitted** |
| `unclassified` | Written before the marker existed — provenance unknown | permitted | **not permitted** |

Two rules make this hold up:

- **No downgrade.** A `provider` assertion of an address already recorded
  `observed` is refused — evidence of use outranks purchase. The observed
  provenance is preserved along with the class, because it is precisely what
  justifies the surviving permission. The upgrade direction (`provider` →
  `observed`, on real communication) is allowed.
- **Unclassified is never silently usable.** A legacy value is reported *as*
  `unclassified` — a positive statement that the provenance is unknown — and is
  not outreach-eligible. Absence of a marker never reads as permission.

**The marker is the authority, and it lives in the store** (`athenaeum.pii`),
not in each consumer: there will be more than one consumer, and a rule
reimplemented per consumer is a rule that eventually is not implemented. The
read interface returns every value with its classification attached, and
accepts a `usage_classes` filter so a caller that must not see
provider-sourced addresses cannot receive one by accident. **Every surface
carries that filter**, which is what keeps the rule from being escapable by
choosing a different entry point: `pii.read_entity` / `read_entities`; the
`read_entity` MCP tool (takes `usage_classes`); `recall`'s `with_pii` join
(which threads `usage_classes` to the same assembly); and, on the shell,
`athenaeum entity --usage-class` and `athenaeum recall --with-pii
--usage-class`. (The person-shaped `pii.read_person` / `read_people`, the
`read_person` MCP tool, and `athenaeum query person --usage-class` carried
the identical filter before their removal in athenaeum#888.) A generic tool
that had dropped the filter
would not have been a smaller version of the same tool — it would have been a
way around this section. `pii.is_outreach_eligible` is the
single predicate a consumer calls. A consumer-side check is still wanted as
defense in depth — it is never the mechanism.

Deliberately **not** answered here: *who* may request which class
(authorization, deferred with athenaeum#864's same question), and whether an
address is still deliverable — that is `is_bounced_identifier`, a separate
question with a separate predicate. A caller about to send needs both.

### 2.4 Erasure classification and taint rules (athenaeum#985)

`athenaeum.erasure` (split (c) of athenaeum#718's re-scope, athenaeum#911
design lock §8) is the classification and taint-propagation layer that
decides *which* content is erasure-class and *what may be written about it*
— independent of where the bytes ultimately land (that is athenaeum#984's
off-corpus storage mechanics, a separate slice this one does not depend on
or wire into). Two pieces of that layer are recorded here rather than only
in code, per athenaeum#985's own acceptance criteria.

#### Erasure egress disclosure

**Erasure is a single-store delete of every copy the system controls** —
the corpus, any off-corpus surface, and the HMAC-keyed hash pointers
`athenaeum.erasure.erasure_content_hash` writes — **plus enumerable-but-
unreachable copies in session transcripts and downstream agent outputs.**

Recall into a session is an egress event. Once erasure-class content has
been pushed into a session (a human's chat transcript, an agent's own
working memory, a log a downstream tool wrote from that session's output),
athenaeum's erasure cascade has no reach into it — those copies are outside
every store this library controls. This is stated here as a **disclosed
gap, not a silent one**: `athenaeum.erasure.EGRESS_DISCLOSURE` carries this
exact guarantee into every redaction-ledger record's `to_dict()` output
(`athenaeum.erasure.RedactionLedgerRecord`), so an operator reading the
ledger after an erasure sees the disclosure attached to the action, not just
in this document.

This does not weaken the erasure guarantee for what the system DOES
control — it says precisely what "erasure" does and does not reach, the
same posture §2.2's L6 note takes for the outbound-LLM path ("the outbound
path is not a PII boundary... this section is the 'write it down' half").
Closing the session-log gap itself is a separate, parked question (egress
*refusal*, athenaeum#428) — this module documents the gap honestly; it does
not close it.

#### Erasure remediation: misclassified in-git content

`athenaeum.erasure`'s taint rules (derivation, re-ingestion, push-is-egress)
and its conservative default (an unknown-jurisdiction data subject is always
erasure-class) exist to keep erasure-class content off the git-versioned
corpus in the first place. When they fail — a claim is misclassified and
lands in git anyway — git's own durability, the exact property that makes
ordinary content recoverable, is what makes an in-git "erasure" a lie: a
`git rm` alone leaves the content in history on every clone until a rewrite
is force-pushed everywhere (`docs/extending/whole-store-adapter-design.md` §4.5).

The named remediation, **last resort, not a routine tool**:

1. **Identify every commit that ever introduced the misclassified content**
   (`git log -p --all -- <path>`, or a full-history grep if the content
   moved paths). A single `git rm` commit does NOT remove it from history —
   this step is what a naive remediation misses.
2. **Rewrite history** with a purpose-built tool (`git filter-repo`
   recommended over the deprecated `git filter-branch`; BFG Repo-Cleaner is
   an alternative) to strip the content from every commit that carries it.
3. **Force-push the rewritten history to every remote.**
4. **Blast radius — stated, not glossed over:**
   - **Every machine with a clone must re-clone.** A rewritten history is a
     different set of commit SHAs; a machine that pulls/merges against its
     old clone resurrects the stripped content on the next sync. There is
     no incremental-update path — this is the cost of a git-durability
     erasure remediation, and it is the reason the ordinary case is
     "classify correctly before it enters git," not "rewrite history after."
   - **Ledger re-anchor.** Every durable record that names a commit SHA as a
     recovery/provenance pointer into the rewritten range — the decay-sweep
     ledger's `recovering_commit`
     (`athenaeum.decay_sweep.SweepLedgerRecord.recovering_commit`), the
     merge-provenance ledger, any other SHA-keyed record — points at a SHA
     that no longer exists post-rewrite. Those records must be re-anchored
     to the corresponding SHA in the new history (`git filter-repo` and BFG
     both emit an old-SHA-to-new-SHA mapping for exactly this purpose); an
     un-reconciled ledger entry is a dangling pointer, not a security
     defect, but it will read as one to a future auditor unless it is fixed
     in the same remediation pass.
   - **Any external reference to a stripped commit SHA** (an issue comment,
     a CI run's logged SHA, a teammate's local branch) goes stale the same
     way a clone does.
5. **Redaction-ledger entry, written as part of the remediation, not
   after.** `athenaeum.erasure.build_history_rewrite_remediation_record`
   builds the entry (reason code `history-rewrite-remediation`,
   `action_taken: refuse-write`) — this function BUILDS the record; it does
   not perform the rewrite itself. The record carries the same "that-and-
   why, never what" guarantee every redaction-ledger entry carries (§2.4's
   sibling AC8): which opaque subject reference, which data/memory class,
   the HMAC-keyed content hash for correlation — never the content that was
   misclassified.

This protocol is **documented, not implemented** — athenaeum#985 ships the
classification/taint machinery and the ledger-entry builder; it does not
ship a history-rewrite command, and it never runs against live data (see
that issue's "Out of scope").

### 2.5 Off-corpus erasure boundary (athenaeum#984)

**The boundary this section documents is physical, not merely policy.** The
wiki store is a git repository with history, clones, and remotes — a delete
committed there survives in history on every clone until a rewrite is
force-pushed everywhere, so it is not an erasure. `athenaeum.off_corpus`
(split (b) of the athenaeum#718 re-scope, `docs/extending/whole-store-adapter-design.md`
§8) gives erasure-class content a genuinely separate, non-git-tracked store:
a delete there (`athenaeum.off_corpus.erase_off_corpus_record`) is a real
`os.unlink`-level removal, with no git history for it to survive in.

- **Enforced at configuration time, not merely documented.**
  `athenaeum.off_corpus.off_corpus_root` refuses (`OffCorpusConfigError`,
  fail-closed) to resolve an `off_corpus.adapter` whose `surface_root` lands
  inside `knowledge_root` at all — not just outside `wiki/`, which is all the
  existing `excluded` storage-adapter surface (§2's "off-corpus excluded
  surfaces") guarantees. A misconfigured off-corpus surface cannot silently
  become git-tracked and defeat the erasure guarantee; it fails to resolve at
  all.
- **`capabilities.purgeable`/`capabilities.versioned` are real, not just
  declared.** The `Store` this module builds is constructed with the
  off-corpus root itself as its `FilesystemStore` `knowledge_root` argument
  (see `athenaeum.store.FilesystemStore`'s `versioned` capability: whether
  `knowledge_root/.git` exists) — so `versioned` reads `False` for the
  physically-correct reason (no `.git` there), not because the flag was
  hand-set.
- **Single-store, single-operation erasure.** `erase_off_corpus_record`
  deletes the content key and incrementally rebuilds BOTH off-corpus index
  shards (FTS5 + vector) in the same call, so a caller cannot observe an
  intermediate state where the content is gone but a stale index shard still
  serves it through `recall`.
- **Federation does not widen the audience gate.** An off-corpus hit
  federated into `recall` (see
  [`docs/design/recall-architecture.md`](recall-architecture.md#off-corpus-federation-athenaeum984))
  passes through the SAME Layer C `is_page_authorized`/`recallable` checks
  every other hit does — no off-corpus-specific carve-out exists in that
  code path. An operator controls whether off-corpus content is recallable
  AT ALL, independent of `off_corpus.enabled`, by the SAME
  `storage.adapters.<name>.corpus_policy.recallable` flag §2 already
  documents for the `excluded` surface.
- **The ledger boundary is symmetric.** `athenaeum.verdicts.record_pair_decision`
  routes a verdict pair with an erasure-class side (`refuse_if_erasure_class`
  — the same `pii:`-flag signal athenaeum#712 already gated a refusal on;
  real HMAC-keyed erasure classification is athenaeum#985's separate scope,
  not this module's) to the off-corpus ledger shard instead of the in-git
  ledger, INCLUDING a cross-boundary pair where only one side is
  erasure-class — the whole pair routes off-git, never a partial write. See
  [`docs/reference/configuration.md`](../reference/configuration.md#off-corpus-store-athenaeum984--off-by-default).
- **Off by default.** With `off_corpus.enabled` unset, none of this code
  runs — no off-corpus index, no federation, and `record_pair_decision`
  keeps its pre-athenaeum#984 behavior of refusing (not writing anywhere) an
  erasure-class pair.

## 3. Dependency-upgrade policy

This repo follows the Kromatic maintenance-posture playbook, with one critical adaptation: **the package's own upper-bound caps in `pyproject.toml` define what Dependabot can propose at all.** Auto-merge eligibility is layered on top of those caps.

### 3.1 Patch and minor bumps (Dependabot)

Auto-merge is wired in `.github/workflows/dependabot-auto-merge.yml`. The policy:

- **All patch updates** (`x.y.Z`) auto-merge when CI is green (matrix across Python 3.11/3.12/3.13).
- **Minor updates** (`x.Y.z`) auto-merge **except** when the PR touches a hold-list package:
  - `anthropic` — pre-1.0, breaking changes ship freely per pyproject.toml comment.
  - `fastmcp` — pre-1.0, MCP protocol may shift.
  - `chromadb` — minor bumps have migrated SQLite schemas (data loss on downgrade).
- **Major updates** never auto-merge — and most would exceed the pyproject.toml upper bounds anyway, so Dependabot can't propose them within current caps.

### 3.2 Major version bumps + cap bumps

A "major bump" here is sometimes Dependabot proposing to raise the upper-bound cap in `pyproject.toml` (rather than the dep itself). Disposition:

- **Anthropic 1.0** when it ships → schedule a focused PR. Audit breaking changes. Bump the cap and run the full test matrix.
- **Pydantic 3.0** → same.
- **Chromadb 2.0** → review SQLite migration path; consumers' existing databases must keep working.
- **Fastmcp 3.0** is **not** a future trigger — the current cap is `fastmcp>=2.0.0,<4.0` (`pyproject.toml:45,54`), so a `3.x` release resolves inside the existing range and is eligible for the ordinary minor-bump path in §3.1 (still hold-listed there, so it requires a human eye, but it does not need a cap-bump PR). **Fastmcp 4.0** is the actual future cap-bump trigger — audit MCP protocol changes when it ships.

For all of these, the worktree-internal version cap shift is the actual change; the dependency bump is a consequence.

### 3.3 Quarterly review checkpoint

Quarterly (next: 2026-08-06):

1. Confirm `anthropic` is still < 1.0 (or update strategy if 1.0 ships).
2. Confirm `chromadb` minor releases haven't introduced an SQLite migration that would break consumers downgrading.
3. **Partial gap:** no `pip-audit` (or equivalent dependency-vulnerability scan) runs anywhere in CI yet — verified against `.github/workflows/*` (no `pip-audit` reference in any workflow). The lockfile half is now closed: a CI-only `requirements-ci.lock` pins the full transitive dependency tree for reproducible CI (athenaeum#556), so a future `pip-audit -r requirements-ci.lock` step would have a concrete resolved set to scan. Wiring that scan step is still open. This line exists so the remaining gap stays visible at each quarterly review rather than being silently assumed covered.
4. Confirm action SHA pins are still up-to-date with their semver tags.
5. Re-evaluate hold list. If any pre-1.0 dep has stabilized (e.g. Anthropic 1.0), remove from hold list.
6. Re-verify the dependency-cap table in §1 against `pyproject.toml` — copy the live ranges verbatim; do not let this doc drift from the source of truth.

## 4. What CI already enforces

- **`ci.yml`** — pytest across Python 3.11/3.12/3.13. Matrix testing catches Python-version-specific dep breakage.
- **`release.yml`** — gated on tag push; trusted-publishing OIDC.
- Action SHA pinning provides supply-chain protection beyond what Dependabot offers.

## 5. Coverage snapshot

Test suite is pytest with pytest-cov. Coverage was not measured in this session due to disk constraints; should be captured at Q3 review.

The 3-Python-version test matrix is itself meaningful coverage — many dep regressions show up version-specifically.

## 6. Branch protection posture (issue athenaeum#557)

Last reviewed: 2026-08-01 (owner ruling).

Two branch-protection settings were evaluated for `develop` and `main` and **both were declined** by the repo owner on 2026-08-01, in a comment on issue athenaeum#557. Recording the decision here so a future audit does not re-file it as a gap.

### 6.1 The two declined settings

- **`enforce_admins`** — declined. Stays `false` on `develop` and `main`.
- **Required pull request review** (`required_pull_request_reviews`) — declined. No required review is configured on `develop` or `main`.

Both are deliberate owner decisions, not oversights, made in an Occam issue-graph sweep and recorded directly on the issue (not inferred by an agent).

### 6.2 Rationale

- **Force-push and branch deletion are already blocked**, redundantly, by two independent organization-level rulesets (`local-clones-branch-baseline`, `dont-delete-branches`) that apply to `develop`, `main`, and `staging`. Direct writes to `main` are further restricted by a third org ruleset, and `main` carries a required-status-check rule. None of this is visible through the classic branch-protection API, so a finding based on that API alone overstates the risk.
- **CI already runs on every path.** A direct push to `develop` triggers `ci.yml`. The gap identified was never test execution — it was the absence of a *required review* gate.
- **Required review would break the overnight self-merge conveyor.** This repo's automated feature-build lane (hestia) self-merges its own PRs into `develop` once CI is green — that is how the majority of this repo ships. A required human review on `develop` would halt that conveyor.
- **`enforce_admins` is declined alongside it for coherence.** With no required review, the only thing `enforce_admins` would gate is the maintainer's own CI check on a solo-maintained repo — it would add friction without adding a second reviewer, which is the change that would actually alter the security posture.

### 6.3 Method note for future audits

GitHub exposes branch protection through **two independent systems**. Reading `/repos/{owner}/{repo}/branches/{branch}/protection` alone is incomplete and will understate this repo's actual posture. A complete audit must also read `/repos/{owner}/{repo}/rules/branches/{branch}` (effective rules, including organization-level rulesets).

Five organization-level rulesets apply to this repo (all `source=Organization`, all `enforcement=active`):

| ruleset | refs | rules |
|---|---|---|
| `local-clones-branch-baseline` | develop, main, staging | `deletion`, `non_fast_forward` |
| `dont-delete-branches` | main, staging, develop, default branch | `deletion`, `non_fast_forward` |
| `local-clones-main-write-restriction` | main | `update` |
| `local-clones-staging-write-restriction` | staging | `update` |
| `main-ci-required` | main | `required_status_checks` |

None of these five rulesets add a pull-request review requirement — so the "no required review" half of the original finding stands even after accounting for them. What they do change is the blast radius: force-push and deletion are not actually open on `develop`/`main`/`staging`, contrary to what a classic-API-only read would suggest.

### 6.4 When to revisit

This posture assumes a **single maintainer** and an **automated overnight merge conveyor**. Reconsider required review if either changes:

- A second regular committer joins the repo (required review then adds a genuine second pair of eyes, not just ceremony).
- The overnight self-merge automation is retired or no longer merges directly to `develop`.

## 7. Pointers

- Maintenance-posture origin: [plinkromatic#371](https://github.com/Kromatic-Innovation/plinkromatic/issues/371) and `plinkromatic/docs/design/security-posture.md`.
- Auto-merge workflow: `.github/workflows/dependabot-auto-merge.yml`.
- Dependabot grouping config: `.github/dependabot.yml`.
- Distribution: PyPI (`pyproject.toml` + `hatchling`).
- Releases: `.github/workflows/release.yml` (triggered by `v*` tags).
- Supply-chain hygiene: action SHA pinning (see workflow comments).
