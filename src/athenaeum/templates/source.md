---
# Scaffold template — copied by `athenaeum init --with-templates` for users to edit (not an LLM-tier schema).
uid: source-REPLACE-ME
type: source
name: REPLACE ME
url: ""
author: ""
published_at: ""
medium: ""
tags: []
# `source:` records where THIS wiki page came from. For a `type: source`
# entity this is usually `manual:user` (you decided to record this
# reference). Scalar form is `<type>:<ref>`. Structured form:
#   source:
#     type: manual
#     ref: alice
#     captured_at: 2026-05-08
source: manual:user
# `field_sources:` records per-field origin. Useful when the URL came
# from one place but the author or published_at came from another.
#   field_sources:
#     author: manual:alice
#     published_at:
#       type: web-fetch
#       ref: meta-tag-2026-05
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

A reference (article, book, podcast, talk, paper). One-sentence summary.

## Key takeaways

- Bullet the things you'd want to recall later.
