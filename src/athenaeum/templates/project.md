---
# Scaffold template — copied by `athenaeum init --with-templates` for users to edit (not an LLM-tier schema).
uid: project-REPLACE-ME
type: project
name: REPLACE ME
status: active
start_date: ""
end_date: ""
owner: ""
repo_url: ""
tags: []
# `source:` records where THIS wiki page came from. Scalar form is
# `<type>:<ref>` (e.g. `manual:alice`). Structured form:
#   source:
#     type: manual
#     ref: alice
#     captured_at: 2026-05-08
source: manual:user
# `field_sources:` records per-field origin. Keys are field names;
# values follow the same scalar/structured forms as `source:`.
#   field_sources:
#     status: manual:alice
#     repo_url:
#       type: github
#       ref: api-fetch-2026-05
field_sources: {}
# Audit pass fields (issue athenaeum#1624, `athenaeum audit`). Left absent
# until a pass runs — do not hand-fill:
#   last_audited: 2026-09-15T00:00:00Z   # ISO-8601 UTC, set by `athenaeum audit`
#   audit_version: audit-v1              # the audit prompt/schema version that ran
# Coordinate fields the audit pass may fill when determinable from this
# page's own body and cited sources — never guessed, never overwritten once set:
#   valid_from: ""     # claim validity window, lower bound (open when absent)
#   valid_until: ""    # claim validity window, upper bound (open when absent)
#   claimed_scope: ""  # where the claim APPLIES (dimensions.py's SCOPE dimension)
# When a coordinate above cannot be determined, the audit pass records why
# here instead of guessing or leaving it silently blank — this is what tells
# "checked, undeterminable" apart from "never checked" (no last_audited at all):
#   audit_findings:
#     valid_from: "undeterminable: no dated validity window stated"
---

# REPLACE ME

## What this is

Brief description of the project — goal, scope, current phase.

## Milestones

- [ ] First milestone
- [ ] Next milestone
