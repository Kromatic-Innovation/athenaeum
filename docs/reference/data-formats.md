# Data formats

**Reference page.** The on-disk shapes: what raw intake looks like, what a compiled wiki
page looks like, and what a run leaves behind.


**Raw intake** lives in `raw/{source}/*.md` with the naming convention
`{timestamp}-{uuid8}.md` (e.g., `20240406T120000Z-aabb0011.md`). Each file is
a plain markdown document containing observations, notes, or session
transcripts. The `{source}` directory identifies the origin (e.g.,
`sessions`, `imports`).

**Raw intake frontmatter.** A raw file may optionally open with a YAML
frontmatter block of its own, separate from (and read before) whatever a
compiled wiki page ends up carrying. Two keys in that block change how the
librarian resolves the file's merge target:

- **`uid`** — a binding key. When present and it matches an existing wiki
  page's own `uid`, the librarian merges this file's content onto that
  page directly and skips name/alias resolution entirely — a page whose
  own `name`/`aliases` no longer match the file's text (or that several
  other pages share a name with) is still reached unambiguously. A `uid`
  that matches no existing page falls back to ordinary name resolution. A
  `uid` that resolves to a page whose `name` differs from the file's own
  is still honored — the binding wins, the mismatch is only counted.
  Writers that set `uid` today: a pre-structured seed carrying `type` +
  `name` (the shape `tier0_passthrough`/the handle-upsert path promote
  verbatim), and an enrichment/observation adapter that already knows the
  uid of the page its draft is about.
- **`name`** — the subject's display name, read alongside `uid` above and
  used as a fallback label when the bound page itself is missing one.

Neither key is required; the overwhelming majority of raw intake (free-form
notes and session transcripts) carries no frontmatter at all and is
classified by the LLM tiers exactly as before.

**Wiki entity pages** live in `wiki/` with YAML frontmatter:

```yaml
---
uid: a1b2c3d4
type: person
name: Alice Zhang
aliases: [Alice]
access: internal
tags: [active]
created: '2024-04-06'
updated: '2024-04-06'
---
```

Entities are indexed in `wiki/_index.md` grouped by type. Conflicts requiring
human review are appended to `wiki/_pending_questions.md`. Each run logs
token usage and estimated costs at the end.

**Parent/child page relation.** A page may be the sub-page of exactly one
other page, up to two levels deep (a page that already has a parent cannot
itself gain children, and vice versa). The child carries a top-level
`parent:` key naming the parent's `uid`; the parent carries one `related`
row per child:

```yaml
# child page
---
uid: b2c3d4e5
type: reference
name: Acme client-count figures
parent: a1b2c3d4
---
```

```yaml
# parent page
---
uid: a1b2c3d4
type: company
name: Acme Corp
related:
  - uid: b2c3d4e5
    role: child
    via: manual
---
```

`via` records how the relation was set: `split` (a by-product of the
oversize-page split, `tiers.py`), `attach`/`fold` (a recompile or fold
decision), or `manual` (`athenaeum relate`, below). A page split before
this relation existed still reads as `via: split` — its `related` rows use
the older `split-into`/`split-from` roles, which are read-compatible but
never written going forward.

The writer (`src/athenaeum/page_relations.py`) renders the relation into
both bodies: a "Sub-pages" section on the parent listing each child as a
wikilink with its one-line `description:`, and a first-line breadcrumb on
the child linking back to the parent. Set or remove the relation by hand
with:

```bash
athenaeum relate --child <uid> --parent <uid>   # attach (via: manual)
athenaeum relate --child <uid> --detach         # remove
```

`athenaeum doctor` reports the count of one-sided pairs — a `parent:` key
with no matching `related` row on the named parent, or the reverse.

**Degraded runs.** When a run exhausts its API call budget (see
`ATHENAEUM_MAX_API_CALLS` above), it writes a `wiki/_deferred_work.md`
manifest itemizing the raw files it did not process and ends with a
warning-level `Done (DEGRADED — budget exhausted)` summary line — the
machine-greppable signal that intake was deferred rather than completed.
The deferred files stay on disk and are picked up automatically by the
next run. The manifest is overwritten on every budget-tripped run and
cleared by the next clean run (full, merge-only, or cluster-only).
The cap is enforced at the entity-tier loop; merge-phase and re-resolve
calls count toward the budget but do not themselves stop the run, so a
merge-heavy run can overshoot the cap before enforcement kicks in.
A degraded run still exits `0` by default; pass `athenaeum run
--strict-budget` to make a budget-tripped run exit nonzero instead —
opt-in, for exit-code-based alerting.

## Relevance policy (`wiki/_schema/relevance-policy.md`)

An operator-owned file in the `wiki/_schema/` family (alongside
`observation-filter.md`, `types.md` and `tags.md`) that declares what this
deployment should and should not remember. It is **optional and not created by
`athenaeum init`**: when the file is absent the policy is empty and nothing
changes. This release only defines, validates and reports the policy
(athenaeum#2643, part of athenaeum#2598); no write path enforces it yet.

Rules are one markdown table, read top to bottom:

```markdown
| Verdict | Type | Tag | Subject |
|---------|------|-----|---------|
| never   |      | personal | |
| always  |      | personal/travel | |
```

- **Verdict** — `always` or `never` (capture), or `route` (destination).
- **Type** / **Tag** — a value from `types.md` / `tags.md`. Blank or `-` means
  "any". A Tag rule also covers its `/`-separated descendants, so `personal`
  covers `personal/travel`. The tag must itself be listed in `tags.md`.
- **Subject** — optional entity uid; blank or `-` means any subject.
- A rule needs at least one of Type, Tag or Subject; naming several means all
  must match.
- **Store** (optional fifth column) — where a `route` rule sends matching
  facts: a `store_id` (`st_` plus 12 lowercase base32 characters) or the
  reserved word `personal`. Never an adapter name. Required on a `route` row,
  blank on `always` / `never`. A table without the column is valid and has no
  routes.

**`route` verdict.** A `route` row records a destination, and nothing acts on
it yet: capture behaves exactly as without it. Capture is the last matching
`always` / `never` row; destination is the last matching `route` row; neither
kind of row affects the other's answer, so `never` still wins over `route`.
A `route` row's tag is not offered to the classifier as a category.

```markdown
| Verdict | Type | Tag | Subject | Store |
|---------|------|-----|---------|-------|
| never   |      | personal | | |
| route   |      | hr | | st_h4r5n2kd6wq3 |
| route   |      | hr/health | | personal |
```

The `Store` cell is part of the policy hash, so changing a destination changes
the `relevance_policy=` token. A `Store` that is not `personal`, is not shaped
like a `store_id`, or (when the caller knows the install's stores) names a
store the install does not map fails validation, naming the rule by number and
line.

**Order matters: the last matching rule wins.** Put a general rule first and
its exceptions after it: in the example every `personal` fact is `never`
except `personal/travel`, which resolves to `always`. A fact no rule matches
has no policy and behaves as before.

An unknown verdict, type or tag, or a rule with no key, fails validation with a
message naming the rule by number and line. `athenaeum relevance-policy`
(add `--json` for machine output) and the read-only `relevance_policy` MCP tool
list the effective rules in order. When the file exists the librarian run
summary reports `relevance_policy=<8-hex hash>` next to `schema_fragments=`
(`invalid` if the file fails validation).

**Two effects of `always`.** An `always` row is (1) a capture hint: the
classifier is told this deployment wants matching facts kept; and (2)
**protection from removal**: a wiki page whose last matching rule is `always`
(same `type` / `tag` / `subject` matching, last match wins) is exempt from
every command that removes pages. To protect every `person` page carrying a
tag:

```markdown
| Verdict | Type | Tag | Subject |
|---------|------|-----|---------|
| always  | person | some/tag | |
```

- `athenaeum retire-pages` refuses the whole run when any named uid is
  protected, naming each protected uid and the rule, and writes nothing (a dry
  run reports it too). `--force-retire` retires them anyway; the override is recorded
  in the retire commit message and as a `removal-override` record in
  `wiki/_relevance_policy_audit.jsonl`.
- `athenaeum decay-sweep` skips a protected page and counts it in its
  `protected by policy` line.
- The audit (`athenaeum audit`) never lists a protected page as a retirement
  candidate and reports `protected by policy: N` in its summary.

With no policy file every removal command behaves as before. A policy file that
fails validation stops `retire-pages` and `decay-sweep`. `athenaeum
relevance-policy report` also lists how many live pages each `always` rule
protects.

**Enforcement of `never`.** Before any tier runs (including the deterministic
passthrough and structured-upsert paths), the librarian, the batch transport
and the auto-memory phase check each fact's *declared* `type`, `tags` and
`uid` frontmatter against the policy. A fact whose last matching rule is
`never` is not compiled; it is recorded in the drop ledger (`athenaeum drops
list`, stage `other`, reason `relevance-policy: <verdict> <keys> (rule N, line
L)`), and the raw note is deleted on processing like any other consumed raw.
A raw that mixes items (a `.jsonl` file's records, or several concatenated
`---` frontmatter documents) is judged per item: only the covered items are
dropped. Auto-memory files are never deleted: a covered one is skipped each
run. With no policy file nothing changes. A policy file that fails validation
stops the run before any raw is touched rather than silently ignoring a `never`
rule. The policy applies forward only.

**Facts that declare nothing.** Most facts carry no `type` or `tags`, so a `never`
rule keyed on a tag is also enforced after Tier-2 classify. When the policy has
a `never` rule that names a tag, the classify prompt lists the policy's tags as
*categories* and asks for a `category` and a `confidence` (`high` / `low`) per
entity. A `never` match the classifier is sure about is dropped. A match it is
unsure about (a `low` or missing confidence) is sent to the existing reasoning
tier (the `reasoning_t2` model knob; no new knob), which picks one category
from the policy's list or none. If the reasoning tier is also unsure, the fact
is **dropped, not held**, with drop-ledger reason `relevance-unsure: <verdict>
<keys> (rule N, line L)`. A reasoning-tier call that fails outright is not
"unsure": the raw stays on disk and the run retries it. A fact that would land
on an existing page is judged by that page's own `type`, `tags` and `uid`
before Tier-3 runs, so a covered fact is caught on an update as well as on a
create. A raw carrying `explicit_remember: true` is exempt from all of this.
The synchronous and Batch API transports share one decision for each step.

**`always` is a hint, not a guarantee.** When the policy has an `always` rule,
the classify prompt carries a short "Relevance hints" block, placed beside the
observation filter, that lists those rules and tells the classifier a fact fitting
one leans toward being kept. It is advice to the classifier and nothing more:
the classifier may still leave a matching fact out, and an `always` rule
exempts nothing. A fact an `always` rule covers is still subject to the
ephemeral-detail filter in `observation-filter.md`, to decay, and to pruning.
With no `always` rule the prompt is unchanged. (Where an `always` rule is a
more specific exception to a `never` rule, it also stays a choosable category
for the `never` check above.)

**Stale `observation-filter.md` on older installs.** `athenaeum init` copies the
bundled `observation-filter.md` into `wiki/_schema/` once and never overwrites
it. An install created before the filter was last revised may therefore carry an
older copy, and `always` hints sit beside whatever that file says. If the
classifier is keeping or dropping things you do not expect, compare your
`wiki/_schema/observation-filter.md` with the bundled one.

**Existing pages: `athenaeum relevance-policy report`.** A new `never` rule
applies forward only: it never touches a page that is already in the wiki. To
see which existing pages a rule would have covered, run `athenaeum
relevance-policy report` (add `--json` for machine output). For every
top-level `wiki/*.md` page it reads the frontmatter `type`, `tags` and `uid`,
matches them the way the gate matches a raw note, and lists each page whose
deciding rule is a `never` rule, grouped by rule. A page that a later `always`
exception rescues is not listed. The command is read-only: it writes nothing and
needs no run lock. What to do about a listed page is your call. The same
report also lists the existing pages each `always` rule protects from
retirement, with a per-rule count (`protected` and `protected_counts` in the
`--json` output).

## Per-type librarian guidance (`wiki/_schema/_type-guidance-<type>.md`)

Two optional, operator-written files in `wiki/_schema/` tell the librarian how
to write and merge pages of one type. Neither is created by `athenaeum init`;
with neither present nothing changes and every prompt is byte-identical to a
wiki without them.

- **`_entity-template-<type>.md`** replaces `_entity-template.md` when the
  librarian creates a page of `<type>`. It is not layered on top: the create
  prompt carries one template. Without it, the global template is used.
- **`_type-guidance-<type>.md`** is free-text guidance added to the create
  prompt and to every merge prompt for pages of `<type>`, including the
  full-echo and append fallbacks.

A merge picks the guidance by the `type:` of the page being merged into, not by
the type the new observation was classified as. `<type>` must be listed in
`types.md` and be lowercase letters, digits, `-` and `_`. A file for any other
type is ignored with a warning. Each file is capped at 8,192 characters
(truncated with a warning) and has `<user_document>` fence markers neutralised,
the same as `observation-filter.md`.

For example, an invented household wiki that declares `recipe` and `preference`
in `types.md` could hold:

```markdown
<!-- wiki/_schema/_type-guidance-preference.md -->
Keep each stated rule in the owner's own words. "Dice the shallots very fine"
stays as written; never generalise it to "likes small pieces".
If an observation is about a different person's preferences, escalate rather
than adding it here.
```

```markdown
<!-- wiki/_schema/_entity-template-recipe.md -->
## Ingredients

## Oven method

## Stovetop method
```

**Scope.** Only unstructured raws that go through Tier-2 classify and then a
Tier-3 create or merge, on both the synchronous and Batch API transports. The
guidance does not reach the Tier-2 classify prompt (`observation-filter.md` is
still the only classify fragment), raws written verbatim by passthrough or
structured upsert, the auto-memory cluster merge, or page recompile. Guidance
cannot change which page an observation is merged into; that is decided before
any merge prompt runs. At most it can lead the model to escalate.

**Content boundary.** The prompt places a fixed precedence clause, shipped with
the package, ahead of the guidance. It tells the model the guidance may say
where content goes, what to keep apart, what to preserve and when to refuse,
but cannot override the librarian's own rules: keep existing content, copy
anchors verbatim, cite with footnotes, and follow the `ESCALATE:` and
`ADDS_NEW_CLAIM` contracts. It can never authorise rewording, summarising or
paraphrasing source text. Write guidance that stays inside that boundary;
anything outside it is ignored.

**Attribution.** When any such file is in use, the `librarian-run-summary` line
carries `type_guidance=<type>:<8-hex hash>,...`, one entry per declared type
with a file. The hash covers both files for that type, so an edit to either
shows up in the run log. The token is omitted when no file exists.

## Data lifecycle and upgrade impact

> **Upgrade impact (0.10.0) — `athenaeum run` now MOVES and DELETES raw
> auto-memory by default.** `raw/auto-memory/` is an *expiring intake queue*,
> not a permanent store. As of 0.10.0, once the librarian has compiled a
> cluster into its canonical `wiki/auto-<topic>.md` entry and the
> contradiction detector has run clean, the move-then-retire pass (issue )
> **moves** each non-contradictory raw fact into the wiki entry (as an
> origin-traced footnote) and **`git rm`s the raw file** so it no longer
> re-enters the nightly loop. This is on by default. If you upgrade and run
> without reading this, your raw auto-memory files will start disappearing
> from the working tree — recoverable, but only from git history.

**What is moved vs. held.** Only non-contradictory clusters are retired.
A cluster is **held** in the queue (never deleted) when the detector flags a
contradiction, when the detection degraded (offline / API error / unparseable
response), or when a member is referenced by an open entry in
`_pending_questions.md` / `_pending_merges.md`. When in doubt, the pass keeps
the raw.

**Recovery is git-only.** The pass refuses to run when `knowledge_root` is not
a git repo, and it never hard-`unlink`s. Each retirement lands as two commits
in your knowledge repo:

- **Commit A — provenance snapshot.** The raw intake about to be retired is
  committed first (scoped `git add` of exactly those files) so every deleted
  file is recoverable from history.
- **Commit B — move + delete together.** The wiki updates (new footnotes,
  `retired: true` marker) and the raw `git rm`s land in a single commit, so the
  fact is never simultaneously absent from both the raw file and the wiki.

To recover a retired raw file, find commit B (or A) in your knowledge repo and
`git show`/`git checkout` the path. **Warning:** because recovery depends
entirely on git history, anything that rewrites or discards that history can
lose retired raw permanently — `git gc` pruning unreachable objects, a
squash/rebase that collapses the snapshot commits, or simply never committing
(running on a dirty repo) / never pushing to a backup remote. If you rely on
retired-raw recovery, keep the knowledge repo's history intact and pushed.

**Pushing after every run (opt-in, issue ).** A scheduled nightly run
commits locally, but does not push by default — so origin silently drifts and
the git-only recovery story only holds on the machine that ran the librarian.
Two ways to turn on a post-run push so origin stays current:

```bash
athenaeum run --push               # one run: push after this run
```

```yaml
# athenaeum.yaml — persistent opt-in
librarian:
  push_after_run: true
  # Optional; defaults are origin + the current branch's upstream.
  # push_remote: origin
  # push_branch: develop
```

The `--push` CLI flag overrides the yaml toggle. When enabled, athenaeum
invokes `git push` (using the operator's ambient git auth — credential helper
/ SSH; no tokens or secrets are handled by athenaeum) after a successful run
that produced at least one commit. `--dry-run` never pushes; a run with no
new commits never pushes. A push failure is reported as a non-fatal warning
(distinct log line `athenaeum-push-failed:`) — commits remain local and the
next run retries (`git push` is idempotent).

**`--dry-run`** computes the exact same plan and logs a structured report
without moving, deleting, or committing anything — use it to preview what a
run would retire.

**Disabling it.** Move-then-retire stays on by default, but you can turn it
off two ways:

```bash
athenaeum run --no-retire          # one run: skip the retire pass entirely
```

```yaml
# athenaeum.yaml — persistent opt-out
librarian:
  retire: false
```

The `--no-retire` CLI flag overrides the yaml toggle. When disabled, raw
auto-memory is neither moved into the wiki nor `git rm`'d; it stays in the
intake queue and is re-examined on every run.


## Spend ledger (`spend.jsonl`)

Every pipeline run that made model calls appends one JSON line to
`spend.jsonl` in the cache directory (`~/.cache/athenaeum/spend.jsonl` by
default; `ATHENAEUM_SPEND_LEDGER` or `spend.ledger_path` relocates it). Each
line is written whole, with `O_APPEND` and an `fsync`, so a reader can tail the
file while athenaeum writes to it. A reader should skip a torn last line rather
than fail on it.

The file is athenaeum's own usage ledger. Another tool that wants athenaeum's
spend, such as a scheduler's cost report, should read this file at report time.
Athenaeum writes nothing to any other ledger.

**Fields an external reader may rely on.** These names are stable across
schema versions. Any other field in a record is internal detail and may change
without notice.

| Field | Meaning |
|---|---|
| `ts` | When the run's record was written (ISO-8601, UTC). |
| `v` | Schema version of the record. The current version is 5. |
| `provider` | Transport that served the run: `anthropic` (metered API), `claude-cli` (subscription) or `jev`. |
| `billing_mode` | `api` or `subscription`. Decides what `estimated_cost_usd` means (see the rules below). |
| `run_type` | Which kind of run spent it: `librarian` (interactive and session-end compiles), `librarian-nightly` (a scheduled nightly), `librarian-triggered` (`athenaeum ingest --if-triggered` when a trigger fired), `answers`, `query-topics`, and others. Every value starting `librarian` belongs to the librarian compile. |
| `models` | Sorted list of the model ids the run used. |
| `api_calls` | Number of model calls the run made. |
| `input_tokens` | Uncached input tokens. |
| `output_tokens` | Output tokens. |
| `cache_read_input_tokens` | Input tokens read from the prompt cache. |
| `cache_creation_input_tokens` | Input tokens written to the prompt cache. |
| `tokens_by_model` | The same token counts split per model id, so a row can be repriced per model. |
| `estimated_cost_usd` | Dollar cost of the run. Money only on `billing_mode: "api"` rows; always `0.0` on subscription rows. |
| `notional_usd` | What the same tokens would cost at API rates. Never money. |
| `account` | Optional, from schema v5. The operator's label for who paid (see below). Absent when no label is configured. |

**Rules a reader must follow.**

- `estimated_cost_usd` is money **only** on rows with `billing_mode: "api"`.
  Sum it across those rows to get dollars spent.
- Subscription rows (`billing_mode: "subscription"`) measure volume, not
  money. Their `notional_usd` is a counterfactual price for that volume and is
  **never** money. Never add it to a dollar total.
- One record is one **run**, not one model call. `api_calls` carries the call
  count. A run that mixed providers writes one record per provider, and the
  run's call count sits on one of those records only, so summing `api_calls`
  over every record still gives the true total.
- Records written before schema v5 have no `account` key. Treat a missing
  `account` as "not labelled", never as a particular account.

**Labelling who paid.** Several tools can share one API key or one
subscription, and then the provider's per-key usage page cannot separate
athenaeum's spend from theirs. Set an account label and every new record
carries it:

```yaml
# athenaeum.yaml
spend:
  account: team-api-key
```

`ATHENAEUM_SPEND_ACCOUNT` sets the same label from the environment and wins
over the yaml key. The label is an opaque name you choose. Athenaeum never
derives it from the key or credential. It must be 1 to 64 letters, digits,
`.`, `_` or `-`; any other value is dropped with a warning, and the run still
records its spend without a label. One label covers the whole installation.
Existing records are not relabelled.

## See also

- [Intake](../modules/intake.md) — what may be written to `raw/`, and what is refused.
- [Librarian](../modules/librarian.md) — how raw becomes a wiki page.
- [Shape](../modules/shape.md) — the rules a claim must satisfy.
- [Upgrading](../guides/upgrading.md) — what a version bump does to an existing wiki.
