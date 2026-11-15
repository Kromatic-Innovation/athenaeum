The `candidate_uid` objects above carry the same two relevance fields as every
other entity (see "Relevance categories" in this message). Add `"category"` and
`"confidence"` to each `candidate_uid` object, judged from its `observations`
alone and exactly as described there:

```json
{"candidate_uid": "<uid>", "observations": "<the claim>", "category": "<category or null>", "confidence": "high"}
```

Being about a known person, including the owner, does not exempt a claim from
the categories: the claim's own subject matter decides. Do not leave either
field out.
