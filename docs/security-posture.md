# Security & Dependency Maintenance Posture

Last reviewed: 2026-05-12. Next scheduled review: **2026-08-06**.

This document records the threat model and dependency-upgrade policy for `athenaeum`. Patterned after the Kromatic games-and-tools workspace posture (origin: `plinkromatic/docs/security-posture.md`), adapted for this repo's published-library shape.

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
> canonically in [`docs/one-way-in-one-way-out.md`](one-way-in-one-way-out.md).
> This section (and §2.1) documents how far that egress half is currently
> *enforced* over the MCP tool surface. Surfaces it does not cover — notably the
> off-corpus contact-data surface — are still governed by the invariant even
> though nothing today refuses a caller that goes around it.

| Surface | Present? | Notes |
|---|---|---|
| User-facing service | **No** | Athenaeum is a library, run by consumers in their own processes. |
| Direct network access | Via consumers' use | When wrapping the Anthropic API, requests go from the consumer's process. |
| Local file/SQLite operations | Yes (chromadb optional extra) | SQLite schema migrations have happened in chromadb minors — hence the hold list. |
| Build-time secret handling | At release time only | Trusted-publishing identity uses GitHub OIDC; no long-lived PyPI token. |
| Read-scoping of recall (athenaeum#312) | Yes (opt-in) | `athenaeum serve --audience <role,…>` (also `ATHENAEUM_AUDIENCE` / `serve.audience`) pins a server process to a restricted read scope so a secondary agent/routine recalls only `access: open` pages and pages whose `audience:` list grants one of its roles; untagged / `confidential` / `personal` pages fail closed (withheld). The audience is pinned by the operator at serve time, not chosen by the `recall` caller, so a restricted agent can't widen its own scope. Enforced inside each backend query (ranking/top-k) and re-checked against fresh on-disk frontmatter at render. Unset = owner = full access. This is NOT a full multi-user auth/ACL system — it is a single-owner read filter for the owner's own secondary agents. |
| Intake-side secret/PII screening (athenaeum#320) | **Shipped** | `remember()`'s write path calls `athenaeum.screening.screen_intake` (`src/athenaeum/mcp_server.py:553`, screener defined at `src/athenaeum/screening.py:212`) to classify sensitive content and resolve the read-time `access:` label BEFORE the single append-only write. Opt-in via a `screening` config resolved by `athenaeum.config.resolve_screening`; `None` (default) preserves prior unscreened behavior. |

### 2.1 MCP tool audience scoping (athenaeum#312 → athenaeum#538)

> Scope note: this section decides *who* may read what **across the 11 MCP
> tools**. It does not, and is not meant to, establish that the MCP surface is
> the only way to reach a store — that is the egress half of the invariant in
> [`docs/one-way-in-one-way-out.md`](one-way-in-one-way-out.md), which this
> scoping enforces for one surface among several.

`caller_audience` (§2, row "Read-scoping of recall") is pinned **once** by the
operator at `athenaeum serve` time and governs the **whole** MCP process, not
just `recall`. Issue athenaeum#538 closed the gap where `recall` was the only one of the
11 registered tools that applied it — a restricted caller could read the same
bytes from a different tool, or mutate the operator's decision queue unchecked.
The decided model (this is the "write it down" outcome athenaeum#538 asked for):

| Tool group | Tools | Restricted (`caller_audience != None`) behavior |
|---|---|---|
| Scoped reads | `recall`, `list_pending_questions`, `list_pending_merges`, `list_pending_decisions`, `read_person` | Fail-closed by the SAME predicate `recall` uses (`is_page_authorized`): an item is withheld unless the caller is authorized for **every** source page behind it. A restricted caller can never obtain page content `recall` would refuse. `read_person` additionally never returns a contact value for a page it withholds — the authorization check runs before contact data is assembled (issue athenaeum#864). |
| Owner-only writes | `resolve_question`, `resolve_merge`, `review_audit_item` | **Fail closed** — adjudicating the operator's contradiction/merge/calibration queue is an owner action. `list_pending_decisions` likewise withholds the `retraction`/`audit` calibration items (no readable source-page path to authorize against). |
| Intentionally open | `remember` | **Not** audience-scoped. Intake is write-only and compiles through the read-time screening path (athenaeum#320); a restricted secondary agent contributing raw memories is the intended use. It cannot read anything back it isn't authorized for. |
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

- Maintenance-posture origin: [plinkromatic#371](https://github.com/Kromatic-Innovation/plinkromatic/issues/371) and `plinkromatic/docs/security-posture.md`.
- Auto-merge workflow: `.github/workflows/dependabot-auto-merge.yml`.
- Dependabot grouping config: `.github/dependabot.yml`.
- Distribution: PyPI (`pyproject.toml` + `hatchling`).
- Releases: `.github/workflows/release.yml` (triggered by `v*` tags).
- Supply-chain hygiene: action SHA pinning (see workflow comments).
