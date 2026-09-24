A cheaper model already proposed a verdict for this attributed paste (shown
below). Your job is to check that verdict, not to re-derive one from
scratch unless you disagree.

Return exactly one JSON object, no prose outside it:

```json
{"verdict": "keep", "claim": "", "reason": "one line", "agree": true}
```

- If you agree with the proposed verdict, set `"agree": true` and repeat its
  `verdict`/`claim`/`reason` (you may tighten the `reason`, but keep the
  same fields the first pass used: `verdict` one of `keep`/`rewrite`/
  `remove`, `claim` populated only for `rewrite`).
- If you disagree, set `"agree": false` and give your own `verdict`/`claim`/
  `reason` following the same rules the first pass used (`keep` for a
  genuine on-topic fact, `rewrite` for a fact that needs tightening or a
  correction, `remove` for off-topic content that only mentions the
  subject's name in passing).
- Judge only the paste text given to you. Never invent facts not present in
  it.
