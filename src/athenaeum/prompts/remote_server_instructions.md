Knowledge memory server powered by Athenaeum — the operator's long-term memory of the people, companies, projects, decisions and principles they work with.

Call `recall` before answering a question this memory may already cover: who someone is, what was decided and why, where a project stands, what the operator prefers. Nothing from this memory reaches you on its own — what you have not asked for, you do not have. One short `recall` costs a single tool call, and an empty result is itself an answer: it tells you the memory does not cover the question.

Searching and reading:

- `recall(query=...)` ranks pages by relevance. Narrow it with `recall(type="person")` when you already know what kind of thing you are looking for.
- `entity_schema` reports the kinds of things this deployment holds and the fields you can query on. Call it before guessing a type name.
- `enumerate_entities(entity_type=..., predicates=...)` answers "every X that matches Y" exhaustively — no query text, no ranking. Use it instead of `recall` when you want a complete set rather than the best matches.
- `read_entity(uid=...)` reads one page you already hold a uid for. It is also the sanctioned way to resolve that page's excluded fields — a person's contact data, and whatever else the operator routes off-corpus; `recall(with_pii=True)` is the same read while you are still searching. Never try to open an excluded surface any other way.

Contributing back:

- `remember` appends what you learned to raw intake for later compilation. Save what the operator would want again later — decisions and their rationale, people and their roles, project status — and not your own commentary.
- `raise_decision` files a question for the operator when you hit a contradiction, or a choice that is theirs to make rather than yours.
- `list_pending_decisions`, `list_pending_questions` and `list_pending_merges` show what is already waiting on the operator. Here they are read-only: adjudicating that queue is the operator's own action, and the tools for it are not served on this server.
