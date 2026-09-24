You are reviewing ONE attributed paste found on a person's wiki page, under
that page's `## Notes` section. The paste was written as if it were a fact
about this person, but pre-athenaeum#1684 intake copied raw text in unbounded and
some of it is not actually about the page's subject — it is unrelated
internal content (an engineering retro, a deploy note, a meeting-history
dump) that happened to mention their name.

Decide what should happen to this ONE paste. Return exactly one JSON object,
no prose outside it:

```json
{"verdict": "keep", "claim": "", "reason": "one line", "confidence": "high"}
```

- `verdict` is exactly one of `"keep"`, `"rewrite"`, `"remove"`.
  - `keep`: the paste states a genuine, on-topic fact about the page's
    subject, worth keeping as-is.
  - `rewrite`: the paste has a genuine fact buried in it, but the fact needs
    tightening into a short claim, or the paste got a name/detail wrong that
    you can correct from context.
  - `remove`: the paste is not really about the subject — it is
    off-topic content (an internal engineering/ops note, a deploy note, a
    session retrospective, a workshop/mural board prepared for but never
    used, a meeting-history dump) that only mentions their name in passing.
- `claim`: for `rewrite` only, the corrected short claim text to use in
  place of the paste (end it with a `(source: ...)` reference clause if the
  paste names a source). Empty string for `keep`/`remove`.
- `reason`: one sentence, the concrete basis for the verdict — cite what the
  paste is actually about, not a generic label.
- `confidence`: `"high"`, `"medium"`, or `"low"` — how sure you are, given
  what is visible in the paste text alone. Use `"low"` when there is a real
  ambiguity (attendee vs. subject, a hedged program name, a person-vs-company
  title, or any case where you cannot tell from the paste alone whether it
  is really about this page's subject).

Judge only the paste text given to you. Never invent facts not present in
it. If the paste is truncated or you cannot tell what it is about, say so in
`reason` and mark `confidence: "low"`.
