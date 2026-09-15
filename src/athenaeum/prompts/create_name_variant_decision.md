You are deciding whether a newly observed entity should FOLD into an
existing wiki page or MINT its own new page.

The new entity's name is a NAME-STRUCTURE VARIANT of one or more existing
pages already matched in the same source file — for example "Name" next to
"Name (qualifier)", or one name a whole-token prefix of the other. That
shape is ambiguous on its own: it can mean the new observation is more
material about the SAME subject (fold it in), or that it names a
genuinely separate, narrower thing that deserves its own page (mint).

You will be given the new entity's name and what was observed about it,
plus each candidate existing page's uid, name, type, current body size in
characters, and whether folding the new observation into that page would
keep it at or under the operator's page-size threshold.

Fold ONLY when the new observation is clearly about the SAME subject as a
candidate page. A candidate whose fold verdict is over the size threshold
should weigh strongly toward MINT even when the names match, since folding
would immediately leave a page that wants to be split back apart. When the
observation actually describes something narrower or different from every
candidate, or when more than one candidate is plausible and you cannot
tell which, mint a new page instead — a wrong fold silently attaches an
observation to the wrong page's history, and is hard to notice later. A
wrong mint is comparatively cheap: the new page stays reachable and stays
reviewable.

Respond with a single JSON object, nothing else:

    {"decision": "fold", "uid": "<candidate uid>", "reason": "<one sentence>"}

or:

    {"decision": "mint", "reason": "<one sentence>"}

``uid`` must be one of the candidate uids you were given for a "fold"
decision, and must be omitted (or null) for a "mint" decision.
