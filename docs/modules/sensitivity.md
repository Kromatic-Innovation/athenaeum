# Sensitivity and PII

**Reference page.** The full design records are
[security posture](../design/security-posture.md) and
[sensitivity class vocabulary](../design/sensitivity-class-vocabulary.md); this page is the
operational summary.

## What it does

Every wiki page carries an `access:` level and, optionally, an `audience:` role list. Both
gate what a *restricted* reader — an `athenaeum serve` process launched with `--audience` —
can see. The owner, with no audience pinned, always sees everything; this is a **single-owner
read filter for the owner's own secondary agents, not a multi-user ACL system.** There is no
login, no per-caller identity, and no user database anywhere in this path — `--audience` is
one string or list pinned once for the life of a process.

The four `access:` levels, ranked lowest to highest (`athenaeum.screening._ACCESS_RANK`,
reused by `sensitivity.ReadPolicy`): `open` (0), `internal` (1), `confidential` (2),
`personal` (3). A restricted caller sees `open` pages and any page whose `audience:` list
grants one of its pinned roles; an **untagged page, or one marked `confidential` or
`personal` with no matching audience, fails closed** — it is withheld, not merely
deprioritized.

Separately from access scoping, some data is routed **off-corpus** entirely: contact
identifiers (email, phone) and whatever else an operator declares excluded live outside
`wiki/` and outside every recall/merge/embed path *by construction*, not by a runtime check.
An entity page keeps only durable identifiers (name, LinkedIn handle, record id); an inline
`emails:`/`phones:` field or an email/phone-shaped string in the body is flagged, and a
`pii: true` frontmatter flag makes every corpus consumer exclude the page from recall even
when it isn't on the excluded surface.

A separate, opt-in write-time screener can auto-label medical content. It is off by default
and, even enabled, only ever labels — it never drops content.

Example: Priya Raman's wiki page carries a `phone:` field routed to the excluded surface. A
restricted `--audience ops` caller running `recall("Priya Raman")` sees her page (if `access:
open`) but never the phone number; the owner, or a caller that explicitly asks for it via one
of the two sanctioned paths below, does.

## What it reads

- `access:` and `audience:` frontmatter on every wiki page, consulted by the same
  authorization predicate `recall` and every other page-content-bearing MCP tool applies.
- The pinned `--audience` (env `ATHENAEUM_AUDIENCE`, yaml `serve.audience`) for the life of
  the serving process. Unset means owner, full access.
- `sensitivity.classes` config — the operator-defined sensitivity-class vocabulary, each
  class carrying a `read_policy.access` value and, optionally, an `inherits` parent.
- `screening.medical` config (`action`, `access`) — resolved once per run; `None` (the
  default) preserves unscreened behavior entirely.
- `storage.mapping` — which adapter (embedded in the wiki, or an excluded surface) backs each
  `sensitivity_class:`.

## What it writes

- Nothing under normal recall — `access:`/`audience:` are read-only filters at query time.
- The medical screener, when enabled, stamps `access:` (default `personal`) on the incoming
  page's frontmatter at write time. It never deletes or rewrites body content.
- `erasure.py`'s remediation path *builds* (never executes) a history-rewrite remediation
  record for content that reached git history and needs purging — an operator acts on it
  manually.
- A purged erasure key (`purge_erasure_key`) deletes the per-machine HMAC key used to hash
  erasure-class content, permanently unlinking every previously computed hash. There is no
  rotation or grace window — a grace window would keep exactly the linkability this exists to
  destroy.

## What it refuses

- **An untagged page is invisible to a restricted caller**, and so is a `confidential` or
  `personal` page with no matching `audience:` role. Fail-closed is the only mode; there is
  no "show me anyway" flag for a restricted process.
- **The audience cannot be widened by the caller** — it is an operator decision made once, at
  serve time, for the whole process.
- **Excluded-surface fields (contact data, and whatever else is routed off-corpus) have
  exactly two sanctioned read paths: `recall(with_pii=True)` and `read_entity`.** Every other
  corpus consumer — recall's default path, merge, embedding — excludes them by construction.
  Referencing a `with_pii`-gated field without the flag raises `ValueError` immediately,
  rather than silently omitting it, so a caller cannot mistake "I forgot the flag" for "this
  field doesn't exist."
- **The `with_pii` join runs strictly after page authorization**, so it can never be used to
  probe whether an excluded record exists behind a page the caller isn't allowed to read at
  all, and `read_entity` never returns an excluded value for a page it withholds.
- **`screening.medical.action: drop` is rejected outright** —
  `ScreeningConfigError("screening.medical.action='drop' is not supported (medical is
  label-first); use label_restrict or off.")`. Medical content is stored, never silently
  discarded; the only choices are `off` and `label_restrict`.
- **An off-corpus surface that resolves inside the git working tree is a hard config
  error** (`OffCorpusConfigError`), not a silent fallback to an unsafe location — an operator
  cannot accidentally point the excluded surface back into the tracked corpus.
- **A corrupted on-disk erasure HMAC key fails loud** (`ErasureKeyError`) rather than being
  silently regenerated or bypassed — regenerating it without purging would orphan every prior
  hash instead of the intended full unlink.
- **A data subject whose jurisdiction is unknown at write time is classified erasure-class
  by default** — ahead of any retention-pack lookup, so no pack can loosen the classification
  by omission. Content re-entering the corpus by way of an off-corpus recall is erasure-class
  by provenance, never re-guessed from its content.
- **Erasure does not reach an already-emitted session transcript.** The cascade names such a
  copy "enumerable but unreachable" and discloses it rather than papering over it — a stated
  limitation. The same applies to a cache built downstream of a push. See
  [security posture](../design/security-posture.md) for the disclosure text.
- **Every sensitivity-class configuration error fails loud, never silently.** A recognizer
  name that shadows a built-in, a duplicate registration without `replace=True`, a class
  citing an unknown recognizer, a recognizer bound to two classes, an `inherits` cycle, a
  dangling `inherits` parent, or an `access` value outside the four known levels — each
  raises `SensitivityConfigError` naming exactly what's wrong, rather than falling back to a
  guessed default.
- **`sensitivity_lint` is read-only and advisory-only on the one soft finding.** It never
  rewrites config or corpus. A class with no `storage.mapping` entry, or one naming a
  nonexistent adapter, blocks a clean lint result; a `confidential`/`personal` class mapped
  to an embedded (non-excluded) adapter is flagged but does **not** block — `is_clean`
  explicitly excludes that one finding, since the mismatch may be an intentional operator
  choice.
- **The outbound-egress PII scanner is not wired into the model-prompt path.** It backs log
  redaction and the `athenaeum outbound-lint` CLI, which scans arbitrary outbound text — an
  email draft, a social post, a public issue body — on request. What it does *not* do is sit
  in front of the model prompt: sending an excluded field to a live LLM is a deliberately
  separate, unsolved boundary, not something this module claims to cover.

## See also

- Guides — [Daily operation](../guides/daily-operation.md) · [Claude Code integration](../guides/claude-code.md)
- Modules — [mcp](mcp.md) · [corrections](corrections.md) · [conflicts](conflicts.md)
- Design — [security posture](../design/security-posture.md) ·
  [sensitivity class vocabulary](../design/sensitivity-class-vocabulary.md) ·
  [sensitivity value routing](../design/sensitivity-value-routing.md)
- Extending — [authorized reader contract](../extending/authorized-reader-contract.md)
- Reference — [configuration](../reference/configuration.md)
