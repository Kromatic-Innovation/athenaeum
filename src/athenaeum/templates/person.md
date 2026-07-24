---
# Scaffold template — copied by `athenaeum init --with-templates` for users to edit (not an LLM-tier schema).
uid: person-REPLACE-ME
type: person
name: REPLACE ME
emails: []
phones: []
current_title: ""
current_company: ""
linkedin_url: ""
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
# `<type>:<ref>` (e.g. `manual:alice` or `linkedin:export-2026-04`).
# Structured form lets you attach more provenance metadata:
#   source:
#     type: manual
#     ref: alice
#     captured_at: 2026-05-08
source: manual:user
# `field_sources:` records where each FIELD on this page came from when
# different fields have different origins (LLM enrichment vs manual edit
# vs an external import). Keys are field names; values follow the same
# scalar/structured forms as `source:`.
#   field_sources:
#     emails: linkedin:export-2026-04
#     current_title:
#       type: apollo
#       ref: bulk-enrich-2026-05
field_sources: {}
---

# REPLACE ME

## What this is

Brief one-sentence description of who this person is and why you're tracking them.

## Notes

- Add observations here. Keep them dated.
