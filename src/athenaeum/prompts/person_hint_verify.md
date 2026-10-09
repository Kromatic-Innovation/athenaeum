This observation was proposed for this page because a name/alias search
matched it to a raw file, not because a reasoning step confirmed the raw
file is about THIS specific person. Before merging, check: is the claim
actually about the person this page describes, or could it be about a
different person who happens to share a name?

If it is NOT about this page's person — a name collision, or the matched
name belongs to someone else in context — do not edit the page. Return
exactly:

```json
{"ops": [], "adds_new_claim": false, "subject_mismatch": true}
```

If it IS this person but the observation records only that they were
present — attended, sat in, joined, were listed, signed off, had nothing to
add — and states no role, decision, action, relationship, or fact of theirs,
that is not a claim worth a page edit. Do not edit the page. Return exactly:

```json
{"ops": [], "adds_new_claim": false, "presence_only": true}
```

Otherwise, proceed with the ordinary merge instructions below.
