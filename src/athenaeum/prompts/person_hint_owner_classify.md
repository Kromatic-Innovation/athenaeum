One entry in the candidate list below is marked `(owner)`. It is the person
this knowledge base belongs to — the one who runs this install. It is offered
on EVERY file, whether or not the owner is named in the text, because notes
about the owner are very often written in the first person ("I", "my", "me")
or about "the user", with no name at all.

Treat a statement as being about the owner when the text says it of:
- the writer in the first person ("I prefer…", "my team…", "I decided…");
- "the user" or "the owner" when the note records a person's own statements
  or choices (an assistant memory note, a journal entry, a chat log);
- the owner by name or by one of their aliases.

Statements about OTHER people, about the world, or about a tool or project
that merely appear in a note the owner wrote are NOT about the owner — emit
nothing for the owner on their account. A generic first person inside a
quotation, an email from someone else, or pasted third-party text is not the
owner speaking.

For each such claim, emit ONE object in the SAME JSON array as any other
entity, using the `candidate_uid` shape:

```json
{"candidate_uid": "<the owner's uid from the list below>", "observations": "<the claim, stated as a fact about the owner in the third person>"}
```

Rules:
- Restate the claim in the third person and as a fact about the owner ("Prefers
  concise status updates."), not as "I …". Keep one object per file for the
  owner; put several claims in the one `observations` string.
- A preference, habit, taste, or standing instruction ("I prefer", "I always",
  "I never", "don't ever", "please always") is a PREFERENCE. Begin that claim
  with the word `Preference:` so the writer files it under the owner page's
  Preferences section.
- A passing mention or a first-person sentence that asserts nothing durable
  ("I'm back", "thanks") is NOT a claim — emit nothing for the owner.
- Never invent a `candidate_uid` that is not in the list below.
- Never re-extract the owner as a NEW entity (a `name`/`entity_type` object).
- When the file says nothing about the owner, omit the owner; do not emit a
  placeholder.
