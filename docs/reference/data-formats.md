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

- **Verdict** — `always` or `never`.
- **Type** / **Tag** — a value from `types.md` / `tags.md`. Blank or `-` means
  "any". A Tag rule also covers its `/`-separated descendants, so `personal`
  covers `personal/travel`. The tag must itself be listed in `tags.md`.
- **Subject** — optional entity uid; blank or `-` means any subject.
- A rule needs at least one of Type, Tag or Subject; naming several means all
  must match.

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
needs no run lock. What to do about a listed page is your call.

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


## See also

- [Intake](../modules/intake.md) — what may be written to `raw/`, and what is refused.
- [Librarian](../modules/librarian.md) — how raw becomes a wiki page.
- [Shape](../modules/shape.md) — the rules a claim must satisfy.
- [Upgrading](../guides/upgrading.md) — what a version bump does to an existing wiki.
