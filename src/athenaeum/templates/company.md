---
# Scaffold template — copied by `athenaeum init --with-templates` for users to edit (not an LLM-tier schema).
uid: company-REPLACE-ME
type: company
name: REPLACE ME
hq_location: ""
industry: ""
website: ""
linkedin_url: ""
employee_count: 0
# Source-handle registry keys (issue #453, epic #422). These map an entity
# to the corpus handles the fact-mining pipeline resolves against, and are
# indexed into `registry.json` by `athenaeum registry`. Leave empty until
# real handles are known; the index builder tolerates the empty case. See
# docs/source-handles.md for the field-by-field contract. No client PII
# belongs in this public template — populate on private wiki pages only.
domains: []
alt_emails: []
slack_channels: []
slack_user_ids: []
partner_domains: []
drive_folder_ids: []
mural_board_ids: []
handles_verified: ""
tags: []
# `source:` records where THIS wiki page came from. Scalar form is
# `<type>:<ref>` (e.g. `manual:alice` or `apollo:org-enrich-2026-05`).
# Structured form lets you attach more provenance metadata:
#   source:
#     type: apollo
#     ref: org-enrich-2026-05
#     captured_at: 2026-05-08
source: manual:user
# `field_sources:` records per-field origin when fields come from
# different places. Keys are field names; values follow the same
# scalar/structured forms as `source:`.
#   field_sources:
#     industry: apollo:org-enrich-2026-05
#     employee_count:
#       type: linkedin
#       ref: scrape-2026-04
field_sources: {}
---

# REPLACE ME

## What this is

Brief description of the company and why it matters to you.

## Notes

- Observations, contacts, deal history, etc.
