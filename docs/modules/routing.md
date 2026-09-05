# Entity and sensitivity routing

**Reference page.** The fuller design records are
[sensitivity-value routing](../design/sensitivity-value-routing.md) and
[sensitivity class vocabulary](../design/sensitivity-class-vocabulary.md); this page is
the operational summary.

## What it does

Two independent routing decisions happen before an observation lands in the wiki.

**Entity routing** decides whether a Tier-2 classification becomes a new page or
folds into one that already exists. `tier1_programmatic_match` (`tiers.py`) matches
raw text against the wiki's name/alias index; a match that clears the junk-name
filter (`resolve_junk_match_names`), the mention-density gate, and the optional
type gate becomes a candidate for a Tier-3 merge instead of a create. On the create
path, `validate_create_name` and `gate_create_name_classifications` run a fixed
sequence of checks against every `is_new=True` classification — junk-shaped names
are rejected, ambiguous or low-specificity names are escalated to a human, and a
name that collides with an existing indexed page is either folded into that page
(when its type is known) or escalated (when it is not). A classification whose name
is a bare email address is resolved to its owning entity or declined outright,
never minted as an address-named page.

**Sensitivity routing** decides whether a matched sensitive value is written into
the wiki at all. `route_sensitive_values` (`sensitivity_routing.py`) runs at the top
of `librarian.process_one`, before Tier 0's passthrough write and before Tier 1/2/3
ever read `raw.content` — so a routed value never reaches an LLM prompt and never
lands in a compiled page. It scans the raw file's body against
`athenaeum.sensitivity.classify`, and every match whose class resolves to the
`route` action is written as its own record to the secret vault (the `excluded`
storage surface) and replaced in the body with a resolvable pointer:

```
[sensitive:<class>:<record_id> — value withheld; resolve via
athenaeum.sensitivity_routing.resolve_sensitive_record()]
```

`resolve_sensitive_record` is the read half: given a pointer's `(sensitivity_class,
record_id)`, it looks up the vault record and returns the original value only if
the caller's audience clears the matched class's `read_policy`.

## What it reads

- The wiki's name/alias index (`EntityIndex`) — for Tier-1 mention matching and the
  create-path uniqueness check.
- The wiki root's `_retired_names.yaml` sidecar — names that a log-demote already
  retired are refused re-creation rather than silently re-minted.
- `sensitivity.routing.enabled` and `sensitivity.routing.classes.<name>.action`
  (`resolve_sensitivity_routing`) — the global on/off switch and the per-class
  `route`/`off` override. Both are resolved fresh per raw file.
- `sensitivity.classes.*` (`athenaeum.sensitivity.available_classes`) — the class
  vocabulary and each class's `read_policy.access`/`audience`, consumed unmodified;
  routing adds no new classification logic of its own.
- `storage.mapping` — to resolve a routed class's vault root; with no explicit
  entry the vault root defaults to the built-in `excluded` adapter.
- `librarian.junk_match_stopwords` / `junk_match_allowlist`, `librarian.type_gate_allowed_types`
  / `type_gate_excluded_keys`, and `librarian.create_name_escalate_max_chars` — the
  entity-routing tuning knobs.

## What it writes

- A vault record per routed sensitive value, under `<excluded-root>/sensitivity/<class>/<record_id>.md`,
  keyed by a `uuid5` derived from `(raw_ref, class, span)` — never from the matched
  value itself, so a value that appears in two different raw files or twice in one
  file mints two separate vault records with no way to recognize they are the same
  secret.
- The redacted raw body, spliced back onto the file's untouched frontmatter
  preamble, in memory for the rest of the sweep — `raw/` itself is never rewritten.
- On the entity side, an `EscalationItem` for every declined or escalated
  classification (ambiguous address, escalated short name, unknown-type collision,
  demoted-name re-mint attempt) so the underlying observation is not silently lost
  when its page is not.

## What it refuses

Sensitivity routing fails closed — `route_sensitive_values` raises
`SensitivityRoutingError` rather than ever returning a partially redacted or
silently unredacted string:

| Condition | Behavior |
|---|---|
| `sensitivity.routing.enabled` unset or `false` | Text returned completely unchanged — dark by default, no behavior change for an unopted-in deployment. |
| A class's resolved action is `"off"` | That class's matches are never routed even while the global switch is on. |
| Two matches share or overlap a character span | Deterministic precedence: sorted by `(span start, class name)`, first-sorted match wins, a later overlapping match is dropped — never left un-redacted. |
| A match has no character span (a hypothetical frontmatter-field match) | `SensitivityRoutingError` — field-based routing is not implemented, and this stage refuses to guess a substitution point. |
| `storage.mapping` routes a class to an adapter that participates in the corpus | `SensitivityRoutingError` — routing a value onto an in-corpus surface is the exact leak this mechanism exists to prevent. |
| Any exception while writing a vault record (disk full, permission error, …) | `SensitivityRoutingError` — the file is left untouched, retried on the next sweep, same as any other Tier-processing failure. |
| A malformed `sensitivity.*` config | `SensitivityRoutingError`, not the lower-level config-error type — every failure in this stage is one exception family. |

`resolve_sensitive_record` (the read path) never raises — every failure mode
below returns `None`, indistinguishably from every other one, so a caller can never
probe the vault by distinguishing "wrong class" from "doesn't exist" from "not
authorized":

| Condition | Result |
|---|---|
| Malformed `record_id` (not exactly 32 lowercase hex chars) or `sensitivity_class` (outside the identifier charset) | `None`. |
| Class not found in `available_classes` | `None`. |
| Caller's audience fails `is_page_authorized` against the class's `read_policy` | `None` — checked before any record file is even read. |
| Resolved record path escapes the vault root | `None` — a `Path.is_relative_to` containment check, defense-in-depth over the charset validation. |
| Record file missing, unreadable, or its stored `sensitivity_class` doesn't match the class the pointer named | `None`. |

Entity routing refuses on a separate set of name-quality and collision gates:

| Condition | Outcome |
|---|---|
| Name is bare issue-reference shaped (`#123`) | Rejected outright — logged, never escalated; unambiguous junk. |
| Name is a single, all-lowercase, whitespace-free token at or under `create_name_escalate_max_chars` (default 7) | Escalated to `_pending_questions.md` rather than minted or dropped silently. |
| Name resolves to a page a prior log-demote retired (`_retired_names.yaml`) | Escalated — the page's content is preserved off-wiki, but nothing is re-minted under the retired name. |
| Name collides with an existing indexed page (by name or alias) whose type is known | Disambiguated — the classification is rewritten to update the existing page instead of minting a duplicate. |
| Name collides with an existing indexed page whose type is unknown | Escalated — folding into an untyped page is not treated as a safe disambiguation. |
| Classification names a bare email address matching two or more tokens | Declined as `"ambiguous-subject"` — no single address to resolve. |
| Classification's bare-email subject does not resolve to an existing entity | Declined with the underlying `resolve_handle_query` reason (`no-match`, `record-without-uid`, `ambiguous`, `orphan-uid`) — never used to mint an address-named page. |
| Name is filename/path-shaped with a known code/config extension | Never becomes an entity at all — the code-artifact gate applies before either routing check runs. |

There is a third sense of "routing" in this codebase — `athenaeum explain-routing`
and `docs/design/routing.md` — that resolves LLM **provider/model/batch** per
prompt knob. It shares the word but not the mechanism with anything on this page;
see that design doc for it.

## See also

- Guides — [Daily operation](../guides/daily-operation.md) · [Answering decisions](../guides/decisions.md)
- Modules — [corrections](corrections.md) · [MCP surface](mcp.md)
- Design — [sensitivity-value routing](../design/sensitivity-value-routing.md) · [sensitivity class vocabulary](../design/sensitivity-class-vocabulary.md) · [security posture](../design/security-posture.md)
- Reference — [configuration](../reference/configuration.md)
