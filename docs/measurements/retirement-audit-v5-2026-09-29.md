# Retirement audit-v5 follow-through — retire counts (athenaeum#1888)

The full-corpus audit (#1631, `audit-v5`, completed 2026-09-24) flagged **2,616**
unique pages as retirement candidates — about 10% of the corpus at the time.
Retiring a page is a destructive corpus change, so #1631 reserved the actual
retire step for this follow-up: a sampled precision measurement first, then an
operator-set retire policy per type, then a behind-a-tag retire run.

## Precision sample

A stratified, seeded sample of 60 candidates (drawn across every candidate
type) was reviewed by the operator against the page content directly. Counts
by type (sample n / operator retire / operator keep / held for a second
pass):

| type | sample n | retire | keep | held |
|---|---|---|---|---|
| person | 26 | 9 | 17 | 0 |
| company | 15 | 12 | 3 | 0 |
| project | 4 | 4 | 0 | 0 |
| reference | 3 | 3 | 0 | 0 |
| principle | 3 | 3 | 0 | 0 |
| tool | 3 | 3 | 0 | 0 |
| incident | 3 | 0 | 0 | 3 |
| concept | 3 | 0 | 0 | 3 |

person and company sample rows were ultimately decided by the type-wide rule
below rather than individually; the sample validated that the rule's
predictions matched the operator's own read of those pages.

## Retire policy per type (operator-set)

- **person** — keep only if the page carries an identifiable email present on
  the excluded PII surface (used for future enrichment); otherwise retire.
- **company** — keep if the same email test passes, OR the page is
  substantive (contains a dated engagement/CRM record, or is otherwise
  content-bearing) AND relates to at least one surviving (non-retiring)
  person. A substantive page with no surviving person relation goes to a
  **review** bucket rather than being retired or kept outright — a
  data-quality gap to close later, not evidence the page is wrong.
- **project / reference / concept / principle / tool** — each candidate was
  read individually against a "no independent, durable content beyond a bare
  mention or a broken citation" test; candidates that failed the test
  (no independent content) retire, the rest keep. This produced a full-delete
  outcome for principle and concept, and a near-full-delete outcome for
  project, reference, and tool, each with the specific held-back exceptions
  noted below.
- **incident** — reviewed individually rather than by blanket rule: a
  time-bound, no-longer-actionable incident with no durable fact decays/is
  removed outright; a durable observation with no other home is preserved by
  moving its substance into a tracked issue before the page is retired; an
  incident whose content mirrors a still-open GitHub issue's own tracked
  state is reduced to a minimal pointer (with a decay field) rather than
  retired, since the issue remains the live source of truth.

Two exceptions were carried by name to the operator rather than silently
resolved under the blanket rule: one `tool` candidate with substantial real
content (held out of this retire run, filed as a separate follow-up issue to
migrate its facts before it is reclassified), and one `reference` candidate
that was unusually large and content-bearing (reviewed by hand and ruled
retire — its subject had become unimportant and its content non-independent
on inspection, despite the size).

## Retire counts (this run)

Behind rollback tag `pre-retire-2026-09-29` (current HEAD tagged and pushed
before the first write), with both the nightly maintenance sweep and the
reasoning-triggers agent paused for the duration and a repo-local writer lock
held per slice:

| type | retired |
|---|---|
| person | 584 |
| company | 25 |
| project | 29 |
| reference | 23 |
| concept | 18 |
| principle | 3 |
| tool | 4 |
| incident | 1 |
| **total** | **687** |

Plus two incident pages moved rather than counted above: one had its detail
folded into a comment on its associated GitHub issue and was reduced to a
minimal pointer page (not retired — the issue stays open); one had its
observation substance filed into a new tracked issue and was then retired in
its own slice, after the issue existed. A folded project candidate's mention
was preserved as a one-line addition to the company record it concerned,
citing the same source footnote the project page carried, rather than kept
as its own page.

Four archived redirect/pointer pages included in the reference and concept
counts above were checked for inbound wikilinks before deletion; none were
found (their merge-target pages already carry the supersession record in
frontmatter), so no repoint was necessary.

## Kept / review counts (not retired)

| type | kept | review (deferred) |
|---|---|---|
| person | 1,078 | — |
| company | 199 | 645 |

The 645-row company review bucket (substantive pages with no surviving
person relation) was explicitly kept rather than retired — the operator
judged the missing person relation a data-quality gap to backfill, not a
retire signal — and is tracked as a separate relation-backfill follow-up.

## Missing pages (candidates with no page found on disk)

3 person and 1 company candidate resolved to no page on disk at
classification time and were excluded from both the retire and keep counts
above (not double-counted, not retired).

## Rollback

`pre-retire-2026-09-29` tags the commit immediately before the first retire
write, pushed to origin. Each retired page's content also remains individually
recoverable from its own per-slice provenance-snapshot commit (the commit
immediately before that slice's archive commit).
